"""Deterministic worklist, run budget and serialization for nightly curation.

The ``system-memory-curation`` schedule prompt asks an agent to walk its
passes every night. Most of those passes answer a question no model is needed
for — is the queue empty, is a region at 85%, is the weekly marker older than
seven days, is the log over 64KB — and the agent was answering them by reading
the files itself, one tool call at a time, on a workspace that usually has
nothing to do.

This module answers those questions in code. Three things come out of it:

* a **worklist**: one item per pass that actually has work, each carrying the
  stable keys of the things to work on. An empty worklist is the common case,
  and it is now computable without a model turn;
* a **budget**: a run takes at most ``max_items`` keys and lives at most
  ``max_seconds``. What the budget leaves behind is recorded, so the next run
  resumes instead of starting over at the top of pass 1;
* a **lease**: one curation run per vault at a time, so two runs cannot
  consolidate the same regions in each other's half-light.

Why a lease rather than a lock held for the run's duration: a curation run is
an agent turn spread over many separate CLI processes, so no single process
lives long enough to hold an ``flock``. The lease is a TTL record instead,
written under a short flock, and a run that crashes releases it by expiring.
The TTL *is* the elapsed-time budget — one number, so a run cannot outlive the
serialization it promised.

State lives at ``<vault>/Workspace/Curation-State.json``, beside the receipt
journal, for the same reason that one is there: the cursor must survive a
reboot, which a lock file under the system temp root would not. The flock file
itself stays outside the vault (see :func:`ciao.memory_receipts.lock_path_for`)
so no ``*.lock`` pollutes user-owned content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ciao import skill_proposals
from ciao.entity_types import EntityTypeRegistry

logger = logging.getLogger(__name__)


STATE_RELATIVE = "Workspace/Curation-State.json"
CURATION_LOG_RELATIVE = "Workspace/Curation-Log.md"
WEEKLY_REVIEW_LOG_RELATIVE = "Workspace/Weekly-Review-Log.md"
PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"
LEARNINGS_RELATIVE = "Workspace/Learnings.md"
# The skill-proposal queue's location, restated from the module that owns it so
# the worklist and the queue cannot point at different folders.
SKILL_PROPOSALS_RELATIVE = "/".join(skill_proposals.QUEUE_REL)

STATE_VERSION = 1

# The skill's own thresholds, restated once in code so the agent no longer has
# to evaluate them. Changing one here changes it for every workspace; the skill
# text quotes them, it does not own them.
WEEKLY_PASS_DAYS = 7
REGION_CONSOLIDATION_PCT = 85.0
LOG_ROTATION_BYTES = 64 * 1024
LEARNING_PROMOTE_COUNT = 3
LEARNING_PRUNE_DAYS = 30

# Pass ids, in the order the skill runs them. The budget spends its items in
# this order, so what a short run drops is always the tail of the night's work.
PASS_PROPOSALS = "proposals"
PASS_REGIONS = "regions"
PASS_AUDIT = "audit"
PASS_STALE_NOTE = "stale_note"
PASS_CATEGORIES = "categories"
PASS_LEARNINGS = "learnings"
PASS_LEARNINGS_CLEANUP = "learnings_cleanup"
PASS_HYGIENE = "hygiene"
PASS_GUIDE = "guide"
PASS_LOGS = "logs"
PASS_SKILL_PROPOSALS = "skill_proposals"

PASS_ORDER: tuple[str, ...] = (
    PASS_PROPOSALS,
    PASS_REGIONS,
    PASS_AUDIT,
    # Right after the region audit and for the same reason: both are "verify,
    # don't guess" work over facts somebody has been asserting, so a run that
    # reaches one of them has budget left for the other. Nothing about the
    # stale pass is weekly — a note goes stale on a clock of its own, and the
    # marker gates only the two hygiene checks and the guide review.
    PASS_STALE_NOTE,
    PASS_CATEGORIES,
    PASS_LEARNINGS,
    # Immediately after the learnings pass, and that ordering is the whole
    # design: cleanup only ever removes an entry whose findings are durably
    # settled, so a run that reaches it has already passed the pass that proposes
    # and decides. Putting it earlier would let it judge a proposal the same run
    # has not looked at yet.
    PASS_LEARNINGS_CLEANUP,
    PASS_HYGIENE,
    PASS_GUIDE,
    PASS_LOGS,
    PASS_SKILL_PROPOSALS,
)

# The two weekly checks that must both succeed before `last_full_pass` may
# advance. `vault-index --write` and the scoped `os-audit` are the ones the
# skill calls required; a run that skipped or failed either leaves the weekly
# pass due, which is the whole point of the marker.
HYGIENE_INDEX_KEY = "hygiene:vault-index"
HYGIENE_AUDIT_KEY = "hygiene:os-audit"
REQUIRED_HYGIENE_KEYS: frozenset[str] = frozenset({HYGIENE_INDEX_KEY, HYGIENE_AUDIT_KEY})

DEFAULT_MAX_ITEMS = 25
DEFAULT_MAX_SECONDS = 1800.0

# How many stale notes one run may plan, however many are due.
#
# The pass sits ahead of the weekly hygiene keys in :data:`PASS_ORDER`, and the
# budget is a whole-run allowance, so an uncapped backlog spends it: a first run
# against a vault that has never been verified plans 25 notes, the two required
# hygiene checks are never reached, `last_full_pass` cannot advance, and the
# backlog is still there tomorrow — a vault that is one large migration away from
# being cared for stays uncared for. A cap bounds what one night can be asked
# for, and because a note whose verdict lands re-stamps its own `updated:` the
# backlog drains rather than recurring: tonight's five are gone tomorrow.
#
# Small on purpose. A verification is a model read of the note plus its sources
# and a journaled write, so five is a night's work for a human-sized pass, and
# the constant exists to keep that number honest rather than to fill the budget.
STALE_NOTE_MAX_ITEMS = 5

# How many settled learnings one night may retire, however many are due.
#
# A removal is not a model turn — it is one parse, one fold of the proposal queue
# and one splice — but the *keys* it consumes are the same keys every other pass
# spends, and a workspace that let a large backlog drain at full speed would take
# the whole night's budget on pass seven of nine and never reach the required
# weekly hygiene keys, so `last_full_pass` could not advance and the backlog
# would still be there tomorrow. The cap is what keeps this pass a pass rather
# than a takeover.
#
# Ten is a judgement about urgency, not about safety: the removals are reversible
# and the suppression store makes a second run a no-op, so the only cost of
# being slow is that the Active list stays a little longer than it needs to.
LEARNINGS_CLEANUP_MAX_ITEMS = 10


class CurationBusy(RuntimeError):
    """Another curation run holds this vault's lease.

    Fatal to the run that raised it. A caller that caught this and curated
    anyway would reintroduce exactly the overlap the lease exists to prevent:
    two runs consolidating the same region from two stale reads.
    """


# ── Stable keys ───────────────────────────────────────────────────────────


def item_key(pass_id: str, subject: str) -> str:
    """A short, stable id for one unit of work inside a pass.

    Derived from the subject's text rather than its position, because the
    proposal queue shrinks as the run works it: a positional cursor would
    resume by skipping the items that moved up. An edited subject hashes
    differently and is correctly re-planned as new work.
    """
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:12]
    return f"{pass_id}:{digest}"


# ── Worklist ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorklistItem:
    """One pass that has work, with the keys of the things to work on."""

    pass_id: str
    label: str
    reason: str
    keys: tuple[str, ...]
    weekly: bool = False

    @property
    def count(self) -> int:
        return len(self.keys)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass": self.pass_id,
            "label": self.label,
            "reason": self.reason,
            "count": self.count,
            "keys": list(self.keys),
            "weekly": self.weekly,
        }


@dataclass(frozen=True, slots=True)
class Worklist:
    """Everything the night has to do, computed without a model."""

    items: tuple[WorklistItem, ...]
    weekly_due: bool
    last_full_pass: str
    generated_at: str
    notes: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def total_keys(self) -> int:
        return sum(item.count for item in self.items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "empty": self.empty,
            "weekly_due": self.weekly_due,
            "last_full_pass": self.last_full_pass,
            "generated_at": self.generated_at,
            "total": self.total_keys,
            "items": [item.as_dict() for item in self.items],
            "notes": list(self.notes),
        }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def read_last_full_pass(curation_log: Path) -> str:
    """The ``last_full_pass: YYYY-MM-DD`` marker, or "" when unusable.

    Deliberately a narrow scan of the frontmatter block rather than a YAML
    parse: the log's body is agent-written prose that has no business being
    parsed, and a malformed marker must read as "never ran" (weekly pass due)
    rather than raise on the one run that would have fixed it.
    """
    text = _read_text(curation_log)
    if not text.startswith("---"):
        return ""
    _, _, rest = text.partition("\n")
    block, sep, _ = rest.partition("\n---")
    if not sep:
        return ""
    for line in block.splitlines():
        key, _, value = line.partition(":")
        if key.strip() != "last_full_pass":
            continue
        stamp = value.strip().strip("\"'")
        try:
            date.fromisoformat(stamp)
        except ValueError:
            return ""
        return stamp
    return ""


def weekly_pass_due(last_full_pass: str, today: date) -> bool:
    """True when the weekly passes are due.

    Age, not weekday: a server that was powered off on the chosen day must not
    permanently miss its weekly care.
    """
    if not last_full_pass:
        return True
    try:
        marker = date.fromisoformat(last_full_pass)
    except ValueError:
        return True
    return (today - marker) >= timedelta(days=WEEKLY_PASS_DAYS)


def _proposal_subject(bullet: Any, dup: int) -> str:
    """Identity of one queued proposal row, as :func:`item_key` hashes it.

    The bullet text alone is not an identity. Two actionable rows carrying the
    same sentence — a different kind, or the same kind bound for a different
    destination — hashed to one key, so recording the first one done made
    `build_worklist` filter out every other row sharing it: curation reported
    an empty queue with unprocessed proposals still in it.

    The basis is the queue's own identity tuple (kind, text, source, and
    :func:`ciao.proposal_tracking.walk_proposal_queue`'s duplicate ordinal,
    which is what already tells two byte-identical bullets apart) widened with
    the destination, because where a row is filed is part of the work even when
    everything else about it matches.
    """
    return "\x00".join((bullet.kind, bullet.target, bullet.source, str(dup), bullet.text))


def _proposal_items(vault_root: Path) -> list[WorklistItem]:
    from ciao.proposal_tracking import walk_proposal_queue

    try:
        text = (vault_root / PROPOSALS_RELATIVE).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    # `[memory]`/`[profile]` rows stay queued for the user by contract, so they
    # are not work: counting them would make every workspace with one pending
    # cross-project fact look busy forever, and the nightly run would spend a
    # model turn re-reading a row it is forbidden to act on. They are skipped
    # after the walk, not before it, so leaving one out cannot renumber a later
    # duplicate's ordinal.
    keys = tuple(
        item_key(PASS_PROPOSALS, _proposal_subject(entry.bullet, entry.dup))
        for entry in walk_proposal_queue("", PROPOSALS_RELATIVE, text)
        if entry.bullet.kind not in {"memory", "profile"}
    )
    if not keys:
        return []
    return [
        WorklistItem(
            pass_id=PASS_PROPOSALS,
            label="File queued facts into their destinations",
            reason=f"{len(keys)} routable proposal(s) pending",
            keys=keys,
        )
    ]


def _region_items(
    guide_path: Path,
    *,
    memory_char_limit: int,
    user_char_limit: int,
    today: date,
) -> list[WorklistItem]:
    from ciao.memory_tool import memory_status

    status = memory_status(
        guide_path,
        memory_char_limit=memory_char_limit,
        user_char_limit=user_char_limit,
    )
    due: list[str] = []
    reasons: list[str] = []
    regions = status.get("regions", {})
    for name in ("memory", "profile"):
        region = regions.get(name) or {}
        pct = float(region.get("pct") or 0.0)
        expired = int(region.get("expired_count") or 0)
        if pct >= REGION_CONSOLIDATION_PCT:
            due.append(name)
            reasons.append(f"ciao:{name} at {pct:.0f}% of cap")
        elif expired:
            due.append(name)
            reasons.append(f"ciao:{name} has {expired} expired entr(ies)")
    if not due:
        return []
    return [
        WorklistItem(
            pass_id=PASS_REGIONS,
            label="Consolidate the bounded memory regions",
            reason="; ".join(reasons),
            keys=tuple(item_key(PASS_REGIONS, name) for name in due),
        )
    ]


def _audit_items(guide_path: Path, *, workspace_dir: Path, today: date) -> list[WorklistItem]:
    from ciao.memory_audit import audit_entries
    from ciao.memory_tool import read_region

    region_entries = {name: read_region(guide_path, name)[0] for name in ("memory", "profile")}
    report = audit_entries(region_entries, workspace_dir=workspace_dir, today=today)
    findings: list[str] = []
    for section in (
        "aging_state_entries",
        "event_shaped_entries",
        "superseded_state_candidates",
    ):
        for row in report.get(section) or []:
            findings.append(f"{section}:{row.get('region', '')}:{row.get('excerpt', '')}")
    if not findings:
        return []
    return [
        WorklistItem(
            pass_id=PASS_AUDIT,
            label="Re-verify aging and event-shaped memory entries",
            reason=f"{len(findings)} entr(ies) flagged by memory-audit",
            keys=tuple(item_key(PASS_AUDIT, finding) for finding in findings),
        )
    ]


def _vault_relative(rendered: str, prefix: Path) -> str:
    """One rendered entry path as the vault-relative spelling everything keys on.

    :func:`ciao.memory_audit.find_stale_notes` reports the path
    :func:`ciao.vault_index.scan_vault` rendered, which carries the render
    prefix; the worklist key, :mod:`ciao.note_verification`'s check state and the
    note-edit sidecar all key on the vault-relative POSIX path instead. A
    rendered path that is not under the prefix (a hand-built entry in a test) is
    taken as already relative — the same total answer
    :func:`ciao.vault_index._strip_prefix` gives, rather than refusing a
    selection because a path was spelled unusually.
    """
    try:
        return Path(rendered).relative_to(prefix).as_posix()
    except ValueError:
        return Path(rendered).as_posix()


def _stale_note_items(
    *,
    vault_root: Path,
    registry: EntityTypeRegistry,
    path_prefix: Path | None = None,
    today: date,
) -> tuple[list[WorklistItem], str]:
    """The stale notes due to be checked, and a note for the worklist about the rest.

    The selection is the audit's, not a second one:
    :func:`ciao.memory_audit.find_stale_notes` already owns the thresholds, the
    exempt event types, the alias resolution and the frontmatter-then-mtime date
    rule, and three other surfaces (memory-audit, the Memory Map, the review
    queue's ``unverified`` signal) call it. A fourth copy here would be a fourth
    thing to keep in agreement, so this pass *calls* it — including with
    ``registry``, so a custom category's own ``stale_after_days`` reaches the
    verdict exactly as it reaches the map's flag.

    What this adds is the two filters the queue adds and the audit does not, and
    the worklist's own bookkeeping:

    * :func:`ciao.vault_review.never_queued` — a note the review queue would
      never show a person (``Workspace/`` files, templates, completed projects)
      is not a question to put in tonight's worklist, for the same reason the
      Memory Map leaves its ``stale`` flag off it: a list that counts a note as
      unchecked while the surface it sends you to can never show it is two lists
      disagreeing.
    * :func:`ciao.note_verification.should_check` — a note whose check is still
      in its cooldown, or still waiting on the proposal that check pinned, is not
      a question either. The audit cannot know this: it measures a note's
      ``updated:`` against its horizon and has never heard of the check state,
      so a verdict that came back ``unverified`` (which writes nothing, and
      therefore leaves ``updated:`` exactly where it was) would be listed again
      the next night, come back ``already_checked``, and take the first slot
      again the night after. Asking is answered; re-asking is the bug. The
      predicate is the *same* one :func:`ciao.note_verification.verify_note`
      short-circuits on, so the plan cannot list a note the operation would
      refuse to judge.

    Oldest first, because that is the order
    :func:`ciao.memory_audit.find_stale_notes` already sorts in (``age``
    descending, then path), and inheriting it rather than re-sorting is what
    makes the plan reproducible: two runs over the same vault produce the same
    list in the same order, and a short budget drops the *youngest* stale note
    rather than an arbitrary one.

    The cap is applied *after* the cooldown filter, not before it. Capping first
    would be the same starvation with a smaller constant: the cooled-down note
    that is still the oldest would consume a slot every night and nothing behind
    it would ever be planned. Filtering first means the five items are five
    notes with a question actually waiting for them. The two ways a note can be
    left out — a check that already settles it, and the cap — are reported back
    as one worklist note rather than passing for a queue of five.

    The reason carries the note's current
    :func:`ciao.memory_receipts.content_revision` — the exact value the managed
    operation's ``expected_revision`` has to be. The operation refuses a verdict
    about text its caller never read, and until the plan carried the revision the
    only way to obtain it was to reimplement the hash, which an agent will guess
    wrong: every call would come back ``conflict`` or ``failed`` and nothing
    would ever be verified. So the plan states it, and computing it is the only
    body read here — the flagged notes' own bytes, which
    :func:`ciao.vault_index.scan_vault` has already read once for their
    frontmatter. No note is judged and nothing is written; that is the managed
    ``verify_note`` operation's work (#726-D), and this pass exists so the
    nightly run knows which notes to hand it without spending a model turn on
    the selection.
    """
    from ciao import memory_receipts as mr
    from ciao import note_verification as nv
    from ciao.memory_audit import find_stale_notes
    from ciao.vault_index import VAULT_RENDER_PREFIX, scan_vault

    root = Path(vault_root)
    if not root.is_dir():
        return [], ""
    prefix = VAULT_RENDER_PREFIX if path_prefix is None else Path(path_prefix)
    try:
        entries = scan_vault(root, path_prefix=prefix, registry=registry)
        findings = find_stale_notes(
            entries,
            vault_root=root,
            # The same prefix `scan_vault` rendered, passed on rather than left
            # to the default: a drifted prefix makes every mtime stat miss
            # silently, which reads as "no note is stale" instead of an error.
            path_prefix=prefix,
            today=today,
            registry=registry,
        )
    except Exception:  # noqa: BLE001 — an advisory pass must not fail the plan
        logger.warning("curation: stale-note scan failed", exc_info=True)
        return [], ""
    from ciao.vault_review import never_queued

    flagged = findings.get("stale_notes") or []
    items: list[WorklistItem] = []
    settled = 0
    capped = False
    for finding in flagged:
        rendered = str(finding.get("path") or "")
        if not rendered or never_queued(rendered):
            continue
        relative = _vault_relative(rendered, prefix)
        try:
            text = (root / relative).read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            # A note this pass cannot read is one the operation would report
            # `failed`, so planning it would spend a budget item on an input
            # nobody can judge — and reporting a revision for text we never read
            # would be the one thing worse.
            logger.debug("curation: stale note %s is not readable UTF-8", relative)
            continue
        revision = mr.content_revision(text)
        if not nv.should_check(root, relative, revision, today=today):
            settled += 1
            continue
        if len(items) >= STALE_NOTE_MAX_ITEMS:
            capped = True
            break
        age = int(finding.get("age_days") or 0)
        horizon = int(finding.get("threshold_days") or 0)
        items.append(
            WorklistItem(
                pass_id=PASS_STALE_NOTE,
                label=str(finding.get("title") or "") or relative,
                reason=(
                    f"unverified for {age}d against a {horizon}d horizon; "
                    f"revision {revision}"
                ),
                keys=(item_key(PASS_STALE_NOTE, relative),),
            )
        )
    if capped:
        return items, (
            f"more than {STALE_NOTE_MAX_ITEMS} notes are due; this run plans the "
            f"{STALE_NOTE_MAX_ITEMS} oldest of {len(flagged)} flagged, and the rest "
            "wait for the next run"
        )
    if settled:
        return items, (
            f"{settled} of {len(flagged)} flagged note(s) already have a check that "
            "settles them — inside its cooldown, or waiting on a proposal for a "
            "person to decide"
        )
    return items, ""


def _learning_items(vault_root: Path, *, today: date) -> list[WorklistItem]:
    from ciao.learning_records import SECTION_ACTIVE, parse_learnings

    text = _read_text(vault_root / LEARNINGS_RELATIVE)
    if not text:
        return []
    # Read through the canonical model, not a second regex: the writer mints the
    # lines this pass counts, so a pass that read them its own way would be
    # reasoning about a shape nothing produces. Only the Active section is work —
    # entries already under `## Promoted / Resolved` are decided, and re-planning
    # them every night is how a promoted learning gets promoted twice. The parser
    # says which section an entry is in, so that no longer depends on a heading
    # being spelled exactly `## Promoted`.
    document = parse_learnings(text, workspace=vault_root.name)
    subjects: list[str] = []
    reasons: list[str] = []
    promote = prune = 0
    for entry in document.entries:
        record = entry.record
        if record is None or entry.section != SECTION_ACTIVE or record.count is None:
            continue
        if record.count >= LEARNING_PROMOTE_COUNT:
            subjects.append(f"promote:{record.key}")
            promote += 1
            continue
        if record.count == 1 and record.last_seen is not None:
            if (today - record.last_seen) > timedelta(days=LEARNING_PRUNE_DAYS):
                subjects.append(f"prune:{record.key}")
                prune += 1
    if not subjects:
        return []
    if promote:
        reasons.append(f"{promote} entr(ies) at x{LEARNING_PROMOTE_COUNT} or more")
    if prune:
        reasons.append(f"{prune} x1 entr(ies) older than {LEARNING_PRUNE_DAYS} days")
    return [
        WorklistItem(
            pass_id=PASS_LEARNINGS,
            label="Promote or prune recurring learnings",
            reason="; ".join(reasons),
            keys=tuple(item_key(PASS_LEARNINGS, subject) for subject in subjects),
        )
    ]


def _learnings_cleanup_items(
    vault_root: Path,
    *,
    config: Any,
    today: date,
) -> tuple[list[WorklistItem], str]:
    """The settled learnings this night may retire, and a note about the rest.

    The only pass here that removes a line the owner wrote, and therefore the
    only one that needs a settlement to be *durable* before it acts:
    :func:`ciao.learnings_cleanup.plan_cleanup` is called, and it answers per
    entry from the proposal queue and the upstream draft sidecar — never from a
    cache, never from a previous night's plan. Two runs in a row therefore see
    the same answers, and a run that arrives before the settlement is written
    removes nothing.

    It plans the removals and the worklist item **names the mode that performs
    them**: ``ciao learnings-cleanup --apply-settled``, which retires exactly the
    rows this plan proposed, unattended, capped at the same
    :data:`LEARNINGS_CLEANUP_MAX_ITEMS`, and writes its receipt before it writes
    the document. So the pass plans, the command that carries out the plan is one
    the worklist already says out loud, and the rows this pass does *not* propose
    are still the attended ``--apply --approval-file`` workflow rather than
    something a flag decided. The deciding is a fold over the queue and the
    draft sidecar either way; splitting it from the splicing is what keeps an
    unattended run from being the thing that judges.

    The parent rule is kept verbatim: an entry whose finding is pending, whose
    proposal is still implementing, or that no proposal has ever linked consumes
    **no** key, so the same entry cannot take a slot every night. A backlog is
    reported through the worklist note rather than through a growing key list, for
    the reason the stale-note pass reports its cap the same way.

    ``today`` is threaded through rather than read so the plan the tests compare
    is the plan a run on another day would produce.
    """
    from ciao import learnings_cleanup

    root = Path(vault_root)
    try:
        plan = learnings_cleanup.plan_cleanup(
            root,
            workspace=root.name,
            config=config,
            today=today,
            max_removals=LEARNINGS_CLEANUP_MAX_ITEMS,
        )
    except Exception:  # noqa: BLE001 — an advisory pass must not fail the plan
        logger.warning("curation: learnings cleanup plan failed", exc_info=True)
        return [], ""
    if plan.blocked:
        return [], f"learnings cleanup did not run: {plan.blocked}"

    counts = plan.counts
    reasons = [
        f"{counts['active']} active entr(y/ies)",
        f"{counts['remove']} settled and removable",
        f"{counts['keep']} kept",
    ]
    if counts["conflict"]:
        reasons.append(f"{counts['conflict']} line(s) this code cannot read")
    if counts["routes"]:
        # Named apart from the kept count on purpose: an entry routed to a
        # packaged skill is waiting on whoever maintains that skill, and presenting
        # it beside this workspace's own backlog would read as work the nightly run
        # could do. It is reported, never planned.
        reasons.append(
            f"{counts['routes']} routed upstream, waiting on another maintainer"
        )
    if plan.over_cap:
        reasons.append("over this run's cap")
    item: WorklistItem | None = None
    if plan.removals:
        # The command is in the reason, not only in the docs: the agent reading
        # this row is the one that has to run the mode that carries out the plan,
        # and a worklist that made it look up which flag retires a settled entry
        # is a worklist whose eligible rows stay eligible forever.
        reasons.append(
            "retire them with `ciao learnings-cleanup --apply-settled` (no "
            "approval file; the rows the reconciliation kept are not its business)"
        )
        item = WorklistItem(
            pass_id=PASS_LEARNINGS_CLEANUP,
            label="Retire the learnings whose findings are durably settled",
            reason="; ".join(reasons),
            keys=tuple(
                item_key(PASS_LEARNINGS_CLEANUP, f"retire:{row.learning_id}")
                for row in plan.removals
            ),
        )
    notes: list[str] = []
    if plan.over_cap:
        notes.append(
            f"more than {LEARNINGS_CLEANUP_MAX_ITEMS} settled learnings are due; "
            f"this run plans {len(plan.removals)} and the rest wait for the next "
            "one, so the other keys keep their budget"
        )
    if counts["conflict"]:
        notes.append(
            f"{counts['conflict']} Active line(s) could not be read and are left "
            "exactly as written; `ciao learnings-cleanup` names them"
        )
    if plan.diagnostics:
        notes.append(
            f"{len(plan.diagnostics)} parsing issue(s) in the learnings document; "
            "the affected lines are kept as written"
        )
    return ([item] if item is not None else []), " ".join(notes)


def _category_cluster_items(
    vault_root: Path, *, registry: EntityTypeRegistry
) -> list[WorklistItem]:
    """File a ``[category]`` proposal per unlisted ``type:`` with a cluster.

    The one pass here that WRITES rather than reads, and that is the point: the
    trigger is a count over the notes on disk, so nothing about it needs a model
    and the queue is where the decision belongs. The skill's "do not touch
    vocabulary proposals" clause therefore stays true for the agent — this is
    the vault asking, not the agent noticing.

    Keys are the category ids, so a cluster that resolves the same way two
    nights running is one item rather than a new one each time, and a cluster
    the owner resolves makes its item disappear from the next worklist.

    ``registry`` is the AGENT vault root's category list, not one loaded from
    *vault_root*: on an install that has not re-rooted, ``entity-types.yaml`` is
    a different directory from the notes, and reading the notes root left every
    category the owner had just accepted looking unlisted — queued again the next
    night, then refused as a duplicate on accept. It arrives from
    :func:`build_worklist` rather than being loaded here for the same reason the
    accept resolves its root through the config.
    """
    from ciao.vocabulary_proposals import (
        CATEGORY_CLUSTER_THRESHOLD,
        generate_category_proposals,
    )

    root = Path(vault_root)
    try:
        queued = generate_category_proposals(root, registry=registry)
    except Exception:  # noqa: BLE001 — an advisory pass must not fail the plan
        logger.warning("curation: category cluster scan failed", exc_info=True)
        return []
    if not queued:
        return []
    return [
        WorklistItem(
            pass_id=PASS_CATEGORIES,
            label="Propose categories for the note clusters the vault is using",
            reason=(
                f"{len(queued)} unlisted type(s) used by "
                f"{CATEGORY_CLUSTER_THRESHOLD} or more notes"
            ),
            keys=tuple(
                item_key(PASS_CATEGORIES, candidate["type_id_suggestion"])
                for candidate in queued
            ),
        )
    ]


def _hygiene_items(*, weekly_due: bool) -> list[WorklistItem]:
    if not weekly_due:
        return []
    return [
        WorklistItem(
            pass_id=PASS_HYGIENE,
            label="Refresh the vault index and run the scoped audit",
            reason="the weekly pass is due",
            keys=(HYGIENE_INDEX_KEY, HYGIENE_AUDIT_KEY),
            weekly=True,
        )
    ]


def _guide_items(*, weekly_due: bool) -> list[WorklistItem]:
    """Weekly review of the workspace guide body.

    ``curation-begin`` cannot mechanically judge whether a sentence in the
    guide body is misplaced, stale, or bloated, so this pass is a model-judged
    weekly check rather than a deterministic finding. It is gated on the same
    ``weekly_due`` rule as the vault-hygiene pass so a powered-off server does
    not permanently miss its guide care, and it stays separate from the
    required hygiene keys: an over-budget run that never reaches the guide must
    not be told it completed a review it skipped.
    """
    if not weekly_due:
        return []
    return [
        WorklistItem(
            pass_id=PASS_GUIDE,
            label="Review the workspace guide body for misplacement, drift, and bloat",
            reason="the weekly pass is due",
            keys=(item_key(PASS_GUIDE, "guide-body"),),
            weekly=True,
        )
    ]


def _log_items(vault_root: Path) -> list[WorklistItem]:
    oversized: list[str] = []
    for relative in (CURATION_LOG_RELATIVE, WEEKLY_REVIEW_LOG_RELATIVE):
        path = vault_root / relative
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > LOG_ROTATION_BYTES:
            oversized.append(relative)
    if not oversized:
        return []
    return [
        WorklistItem(
            pass_id=PASS_LOGS,
            label="Rotate the oversized logs",
            reason=", ".join(f"{name} over {LOG_ROTATION_BYTES // 1024}KB" for name in oversized),
            keys=tuple(item_key(PASS_LOGS, name) for name in oversized),
        )
    ]


def _skill_proposal_items(vault_root: Path) -> list[WorklistItem]:
    """The pass reports the queue's *open* records, not the files in the folder.

    A settled proposal stays on disk — the decision is the record — so counting
    every ``*.md`` here would put an answered question back in tonight's
    worklist, every night, with nothing to distinguish it from a new one. The
    queue's owner decides what is still open, so the worklist cannot drift from
    the review surface about which rows are waiting.
    """
    names = skill_proposals.open_queue_names(vault_root)
    if not names:
        return []
    return [
        WorklistItem(
            pass_id=PASS_SKILL_PROPOSALS,
            label="Resolve the skill proposals queue",
            reason=f"{len(names)} proposal(s) waiting on a decision",
            keys=tuple(item_key(PASS_SKILL_PROPOSALS, name) for name in names),
        )
    ]


def build_worklist(
    *,
    vault_root: Path,
    guide_path: Path,
    category_registry: EntityTypeRegistry,
    config: Any = None,
    workspace_dir: Path | None = None,
    path_prefix: Path | None = None,
    today: date | None = None,
    done_keys: frozenset[str] | set[str] | None = None,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
) -> Worklist:
    """Compute tonight's work from files alone.

    ``category_registry`` is the category list the cluster pass measures the
    notes against, and it is required rather than loaded from ``vault_root``
    because the two are not the same directory: the registry belongs to the
    agent vault root, the notes to this workspace's root. The caller resolves it
    through the config (the CLI does) so a category the owner accepted cannot be
    re-proposed as unlisted.

    ``config`` is the workspace registry, and it is optional only because two
    passes need it and one does not: the skill-proposal queue and the upstream
    draft sidecar are addressed through it, so the cleanup pass has nothing to
    read without it. When it is absent the cleanup pass is skipped and says so in
    the notes rather than reporting a clean workspace — a pass that silently
    found nothing must never look like a pass that found nothing to do.

    ``done_keys`` are the keys an earlier run of the same night already
    finished; they are removed here rather than inside each pass so a pass whose
    every item is done disappears from the worklist entirely, and a workspace
    whose remaining work is all done reports ``empty``.

    ``path_prefix`` is the stale-note pass's one extra input and is optional for
    every other pass, which is why it defaults: it must be the prefix
    :func:`ciao.vault_index.scan_vault` rendered the note paths under — the same
    value has to reach both, or every mtime probe misses silently and the vault
    reports nothing stale. Left unset, it takes the render prefix
    :func:`ciao.vault_index.scan_vault` defaults to.
    """
    from ciao.memory_tool import DEFAULT_MEMORY_CHAR_LIMIT, DEFAULT_USER_CHAR_LIMIT

    vault_root = Path(vault_root)
    guide_path = Path(guide_path)
    workspace_dir = Path(workspace_dir) if workspace_dir is not None else guide_path.parent
    today = today or datetime.now(UTC).date()
    done = frozenset(done_keys or ())
    memory_limit = memory_char_limit if memory_char_limit is not None else DEFAULT_MEMORY_CHAR_LIMIT
    user_limit = user_char_limit if user_char_limit is not None else DEFAULT_USER_CHAR_LIMIT

    last_full_pass = read_last_full_pass(vault_root / CURATION_LOG_RELATIVE)
    weekly_due = weekly_pass_due(last_full_pass, today)
    notes: list[str] = []
    if not last_full_pass:
        notes.append("no usable last_full_pass marker; the weekly pass counts as due")

    collected: list[WorklistItem] = []
    # Collected FIRST because it is the one pass that writes: the bullets it
    # files have to be on disk before `_proposal_items` reads the queue, or the
    # run that proposes a category would not count the row it just proposed. The
    # order the worklist is PRESENTED in is :data:`PASS_ORDER` either way.
    collected.extend(_category_cluster_items(vault_root, registry=category_registry))
    collected.extend(_proposal_items(vault_root))
    collected.extend(
        _region_items(
            guide_path,
            memory_char_limit=memory_limit,
            user_char_limit=user_limit,
            today=today,
        )
    )
    collected.extend(_audit_items(guide_path, workspace_dir=workspace_dir, today=today))
    # The stale pass is not given the done keys: one filter, here, removes a
    # finished key from every pass at once, and a pass that filtered as well
    # would only be able to drop an item the outer filter drops anyway.
    stale_items, stale_note = _stale_note_items(
        vault_root=vault_root,
        registry=category_registry,
        path_prefix=path_prefix,
        today=today,
    )
    collected.extend(stale_items)
    if stale_note:
        notes.append(stale_note)
    collected.extend(_learning_items(vault_root, today=today))
    if config is None:
        notes.append(
            "learnings cleanup was not planned: this worklist was built without a "
            "workspace registry, and the settlement fold needs one"
        )
    else:
        cleanup_items, cleanup_note = _learnings_cleanup_items(
            vault_root, config=config, today=today
        )
        collected.extend(cleanup_items)
        if cleanup_note:
            notes.append(cleanup_note)
    collected.extend(_hygiene_items(weekly_due=weekly_due))
    collected.extend(_guide_items(weekly_due=weekly_due))
    collected.extend(_log_items(vault_root))
    collected.extend(_skill_proposal_items(vault_root))

    order = {pass_id: index for index, pass_id in enumerate(PASS_ORDER)}
    remaining: list[WorklistItem] = []
    for item in sorted(collected, key=lambda i: order.get(i.pass_id, len(PASS_ORDER))):
        keys = tuple(key for key in item.keys if key not in done)
        if not keys:
            continue
        remaining.append(
            WorklistItem(
                pass_id=item.pass_id,
                label=item.label,
                reason=item.reason,
                keys=keys,
                weekly=item.weekly,
            )
        )
    return Worklist(
        items=tuple(remaining),
        weekly_due=weekly_due,
        last_full_pass=last_full_pass,
        generated_at=_now_iso(),
        notes=tuple(notes),
    )


# ── Budget ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunBudget:
    """What one run may spend before it must stop and hand over."""

    max_items: int = DEFAULT_MAX_ITEMS
    max_seconds: float = DEFAULT_MAX_SECONDS

    def as_dict(self) -> dict[str, Any]:
        return {"max_items": self.max_items, "max_seconds": self.max_seconds}


@dataclass(frozen=True, slots=True)
class RunPlan:
    """The slice of the worklist this run may take, and what it leaves."""

    planned: tuple[WorklistItem, ...]
    deferred: tuple[WorklistItem, ...]
    budget: RunBudget

    @property
    def planned_count(self) -> int:
        return sum(item.count for item in self.planned)

    @property
    def deferred_count(self) -> int:
        return sum(item.count for item in self.deferred)

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": [item.as_dict() for item in self.planned],
            "deferred": [item.as_dict() for item in self.deferred],
            "planned_count": self.planned_count,
            "deferred_count": self.deferred_count,
            "budget": self.budget.as_dict(),
        }


def plan_run(worklist: Worklist, budget: RunBudget | None = None) -> RunPlan:
    """Split the worklist into what fits the budget and what waits.

    Splits *within* a pass when the budget runs out mid-pass, so a queue of 200
    proposals makes progress every night instead of being deferred whole
    forever. Order is :data:`PASS_ORDER`, so what waits is always the tail.
    """
    budget = budget or RunBudget()
    allowance = max(0, budget.max_items)
    planned: list[WorklistItem] = []
    deferred: list[WorklistItem] = []
    for item in worklist.items:
        if allowance <= 0:
            deferred.append(item)
            continue
        take = item.keys[:allowance]
        rest = item.keys[allowance:]
        allowance -= len(take)
        planned.append(
            WorklistItem(
                pass_id=item.pass_id,
                label=item.label,
                reason=item.reason,
                keys=take,
                weekly=item.weekly,
            )
        )
        if rest:
            deferred.append(
                WorklistItem(
                    pass_id=item.pass_id,
                    label=item.label,
                    reason="budget reached; resumes next run",
                    keys=rest,
                    weekly=item.weekly,
                )
            )
    return RunPlan(planned=tuple(planned), deferred=tuple(deferred), budget=budget)


# ── Persisted state ───────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class CurationState:
    """The cursor and the last run's outcome, as stored on disk."""

    done_keys: dict[str, str] = field(default_factory=dict)
    lease: dict[str, Any] = field(default_factory=dict)
    last_run: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "done_keys": dict(self.done_keys),
            "lease": dict(self.lease),
            "last_run": dict(self.last_run),
        }


def state_path(vault_root: Path) -> Path:
    return Path(vault_root) / STATE_RELATIVE


def _load(path: Path) -> CurationState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return CurationState()
    if not isinstance(raw, dict):
        return CurationState()
    done = raw.get("done_keys")
    lease = raw.get("lease")
    last_run = raw.get("last_run")
    return CurationState(
        done_keys={str(k): str(v) for k, v in done.items()} if isinstance(done, dict) else {},
        lease=dict(lease) if isinstance(lease, dict) else {},
        last_run=dict(last_run) if isinstance(last_run, dict) else {},
    )


def _store(path: Path, state: CurationState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.write.tmp")
    tmp.write_text(json.dumps(state.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_state(vault_root: Path) -> CurationState:
    """Read the persisted cursor. Never raises: a corrupt file reads as empty."""
    return _load(state_path(vault_root))


@contextmanager
def _state_lock(vault_root: Path) -> Iterator[None]:
    """Serialize a read-modify-write of the state file across processes.

    The lease decision *is* a read-modify-write — two runs that both read "no
    lease" and both write their own would each believe they hold it — so every
    mutation below happens inside this, not merely the file replacement.
    """
    import fcntl

    from ciao.memory_receipts import lock_path_for

    path = state_path(vault_root)
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    lock_path = lock_path_for(key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


# ── Lease ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Lease:
    """A held curation lease."""

    holder: str
    started_at: str
    expires_at: str
    vault_root: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "holder": self.holder,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
        }


def _live_lease(state: CurationState, now: datetime) -> dict[str, Any] | None:
    lease = state.lease
    if not lease or not lease.get("holder"):
        return None
    expires = _parse_iso(str(lease.get("expires_at", "")))
    # An unreadable expiry is treated as expired rather than as forever: a
    # hand-edited or truncated record must not be able to wedge every future
    # nightly run out of its own vault.
    if expires is None or expires <= now:
        return None
    return dict(lease)


def active_lease(vault_root: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The live lease on this vault, or None. Read-only and lock-free."""
    return _live_lease(load_state(vault_root), now or datetime.now(UTC))


def begin_run(
    vault_root: Path,
    *,
    holder: str = "",
    ttl_s: float = DEFAULT_MAX_SECONDS,
    now: datetime | None = None,
) -> Lease:
    """Take this vault's curation lease, or raise :class:`CurationBusy`."""
    now = now or datetime.now(UTC)
    holder = holder or f"{socket.gethostname()}:{os.getpid()}"
    with _state_lock(vault_root):
        state = _load(state_path(vault_root))
        held = _live_lease(state, now)
        if held is not None:
            raise CurationBusy(
                f"a curation run started {held.get('started_at', 'recently')} by "
                f"{held.get('holder', 'another run')} holds this vault until "
                f"{held.get('expires_at', 'its lease expires')}"
            )
        lease = Lease(
            holder=holder,
            started_at=now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            expires_at=(now + timedelta(seconds=max(1.0, ttl_s)))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            vault_root=Path(vault_root),
        )
        state.lease = lease.as_dict()
        _store(state_path(vault_root), state)
    return lease


def renew_run(
    vault_root: Path,
    *,
    holder: str,
    ttl_s: float = DEFAULT_MAX_SECONDS,
    now: datetime | None = None,
) -> bool:
    """Extend the lease held by ``holder``. False when it is no longer theirs."""
    now = now or datetime.now(UTC)
    with _state_lock(vault_root):
        state = _load(state_path(vault_root))
        held = _live_lease(state, now)
        if held is None or held.get("holder") != holder:
            return False
        state.lease["expires_at"] = (
            (now + timedelta(seconds=max(1.0, ttl_s)))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        _store(state_path(vault_root), state)
    return True


def record_done(
    vault_root: Path,
    keys: list[str] | tuple[str, ...],
    *,
    holder: str = "",
    today: date | None = None,
) -> int:
    """Mark keys finished so a later run does not redo them.

    Rejects keys when ``holder`` is given and no longer owns the lease: a run
    whose lease expired has lost its serialization, and letting it keep
    stamping work done would tell the next run that items nobody verified were
    handled.
    """
    stamp = (today or datetime.now(UTC).date()).isoformat()
    with _state_lock(vault_root):
        state = _load(state_path(vault_root))
        if holder:
            held = _live_lease(state, datetime.now(UTC))
            if held is None or held.get("holder") != holder:
                raise CurationBusy("this run no longer holds the curation lease")
        added = 0
        for key in keys:
            if key not in state.done_keys:
                added += 1
            state.done_keys[key] = stamp
        _store(state_path(vault_root), state)
    return added


def end_run(
    vault_root: Path,
    *,
    holder: str = "",
    status: str = "ok",
    planned: int = 0,
    completed: int = 0,
    deferred: int = 0,
    reasons: list[str] | tuple[str, ...] = (),
    live_keys: frozenset[str] | set[str] | None = None,
    advance_marker: bool = False,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release the lease and record what the run actually did.

    ``live_keys`` are the keys still present in the freshly rebuilt worklist.
    Done keys outside that set are dropped: the work they named is gone from
    the vault, so keeping them would grow the cursor without bound and, worse,
    would suppress a later item that hashed to the same subject again.

    ``advance_marker`` stamps ``last_full_pass`` here rather than in the
    caller, under the same lock and *after* the ownership check. Stamping it
    outside let a run whose lease had expired — one this call is about to
    reject — still suppress the weekly hygiene passes for seven days. Returned
    as ``full_pass_advanced``; the weekly checks gate it exactly as
    :func:`advance_full_pass` does.
    """
    now = now or datetime.now(UTC)
    with _state_lock(vault_root):
        state = _load(state_path(vault_root))
        held = _live_lease(state, now)
        if holder and held is not None and held.get("holder") != holder:
            raise CurationBusy("this run no longer holds the curation lease")
        advanced = False
        if advance_marker and status == "ok" and REQUIRED_HYGIENE_KEYS <= set(state.done_keys):
            _write_full_pass_marker(Path(vault_root), today=today)
            advanced = True
        if live_keys is not None:
            state.done_keys = {k: v for k, v in state.done_keys.items() if k in live_keys}
        summary = {
            "status": status,
            "finished_at": _now_iso(),
            "planned": planned,
            "completed": completed,
            "deferred": deferred,
            "reasons": list(reasons),
        }
        state.last_run = summary
        state.lease = {}
        _store(state_path(vault_root), state)
    return {**summary, "full_pass_advanced": advanced}


def hygiene_complete(vault_root: Path) -> bool:
    """Whether both required weekly checks are recorded as done."""
    done = set(load_state(vault_root).done_keys)
    return REQUIRED_HYGIENE_KEYS <= done


def advance_full_pass(
    vault_root: Path,
    *,
    today: date | None = None,
) -> bool:
    """Stamp ``last_full_pass`` — but only when both weekly checks succeeded.

    The skill already said the marker may only advance after a reliable index
    refresh and audit; saying it is not enforcing it, and a run that reported a
    scan error and stamped anyway made the weekly pass silently skip a week.
    Returns False and leaves the marker alone when either check is missing.

    Ownership-blind on purpose: this is the standalone entry point. The nightly
    CLI goes through ``end_run(advance_marker=True)`` instead, which stamps the
    marker only after it has confirmed the caller still holds the lease.
    """
    if not hygiene_complete(vault_root):
        return False
    _write_full_pass_marker(Path(vault_root), today=today)
    return True


def _write_full_pass_marker(vault_root: Path, *, today: date | None = None) -> None:
    """Write today's ``last_full_pass`` into the curation log's frontmatter."""
    stamp = (today or datetime.now(UTC).date()).isoformat()
    log = Path(vault_root) / CURATION_LOG_RELATIVE
    text = _read_text(log)
    if text.startswith("---"):
        _, _, rest = text.partition("\n")
        block, sep, body = rest.partition("\n---")
        if sep:
            lines = [
                line
                for line in block.splitlines()
                if line.partition(":")[0].strip() != "last_full_pass"
            ]
            lines.append(f"last_full_pass: {stamp}")
            text = "---\n" + "\n".join(lines) + "\n---" + body
        else:
            text = f"---\nlast_full_pass: {stamp}\n---\n\n{text}"
    else:
        text = f"---\nlast_full_pass: {stamp}\n---\n\n{text}" if text else (
            f"---\nlast_full_pass: {stamp}\n---\n\n# Curation log\n"
        )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
