"""Deterministic worklist, run budget and serialization for nightly curation.

``ciao/stock/skills/memory-curation/SKILL.md`` asks an agent to walk nine
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
* a **lease**: one curation run per vault at a time, and archive-time memory
  writes stand down while it is held.

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

logger = logging.getLogger(__name__)


STATE_RELATIVE = "Workspace/Curation-State.json"
CURATION_LOG_RELATIVE = "Workspace/Curation-Log.md"
WEEKLY_REVIEW_LOG_RELATIVE = "Workspace/Weekly-Review-Log.md"
PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"
LEARNINGS_RELATIVE = "Workspace/Learnings.md"
SKILL_PROPOSALS_RELATIVE = "Workspace/Skill-Proposals"

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
PASS_LEARNINGS = "learnings"
PASS_HYGIENE = "hygiene"
PASS_LOGS = "logs"
PASS_SKILL_PROPOSALS = "skill_proposals"

PASS_ORDER: tuple[str, ...] = (
    PASS_PROPOSALS,
    PASS_REGIONS,
    PASS_AUDIT,
    PASS_LEARNINGS,
    PASS_HYGIENE,
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


def _proposal_items(vault_root: Path) -> list[WorklistItem]:
    from ciao.memory_proposals import list_proposals

    rows = list_proposals(vault_root / PROPOSALS_RELATIVE)
    if not rows:
        return []
    # `[memory]`/`[profile]` rows stay queued for the user by contract, so they
    # are not work: counting them would make every workspace with one pending
    # cross-project fact look busy forever, and the nightly run would spend a
    # model turn re-reading a row it is forbidden to act on.
    actionable = [row for row in rows if row.get("kind") not in {"memory", "profile"}]
    if not actionable:
        return []
    keys = tuple(item_key(PASS_PROPOSALS, row.get("text", "")) for row in actionable)
    return [
        WorklistItem(
            pass_id=PASS_PROPOSALS,
            label="File queued facts into their destinations",
            reason=f"{len(actionable)} routable proposal(s) pending",
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


def _learning_items(vault_root: Path, *, today: date) -> list[WorklistItem]:
    from ciao.memory_proposals import _LEARNING_LINE_RE

    text = _read_text(vault_root / LEARNINGS_RELATIVE)
    if not text:
        return []
    # Only the Active section is work. Entries already under
    # `## Promoted / Resolved` are decided, and re-planning them every night is
    # how a promoted learning gets promoted twice.
    active = text.partition("\n## Promoted")[0]
    subjects: list[str] = []
    reasons: list[str] = []
    promote = prune = 0
    for line in active.splitlines():
        match = _LEARNING_LINE_RE.match(line)
        if match is None:
            continue
        count = int(match.group("count"))
        if count >= LEARNING_PROMOTE_COUNT:
            subjects.append(f"promote:{match.group('key')}")
            promote += 1
            continue
        if count == 1:
            try:
                last_seen = date.fromisoformat(match.group("last"))
            except ValueError:
                continue
            if (today - last_seen) > timedelta(days=LEARNING_PRUNE_DAYS):
                subjects.append(f"prune:{match.group('key')}")
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
    directory = vault_root / SKILL_PROPOSALS_RELATIVE
    try:
        names = sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix == ".md")
    except OSError:
        return []
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
    workspace_dir: Path | None = None,
    today: date | None = None,
    done_keys: frozenset[str] | set[str] | None = None,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
) -> Worklist:
    """Compute tonight's work from files alone.

    ``done_keys`` are the keys an earlier run of the same night already
    finished; they are removed here rather than at plan time so a pass whose
    every item is done disappears from the worklist entirely, and a workspace
    whose remaining work is all done reports ``empty``.
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
    collected.extend(_learning_items(vault_root, today=today))
    collected.extend(_hygiene_items(weekly_due=weekly_due))
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


def curation_in_progress(vault_root: Path, *, now: datetime | None = None) -> bool:
    """Whether a curation run currently owns this vault's memory.

    Archive-time auto-apply calls this and stands down while it is true: the
    run is mid-consolidation, holding region text it read minutes ago, and an
    append landing underneath it is either lost to the rewrite or duplicated by
    it. The fact is not dropped — it is queued as an ordinary proposal, which
    is the path uncertain facts already take.
    """
    try:
        return active_lease(vault_root, now=now) is not None
    except Exception:  # noqa: BLE001 — a gate that fails must not break archiving
        logger.exception("curation lease check failed for %s", vault_root)
        return False


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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release the lease and record what the run actually did.

    ``live_keys`` are the keys still present in the freshly rebuilt worklist.
    Done keys outside that set are dropped: the work they named is gone from
    the vault, so keeping them would grow the cursor without bound and, worse,
    would suppress a later item that hashed to the same subject again.
    """
    now = now or datetime.now(UTC)
    with _state_lock(vault_root):
        state = _load(state_path(vault_root))
        held = _live_lease(state, now)
        if holder and held is not None and held.get("holder") != holder:
            raise CurationBusy("this run no longer holds the curation lease")
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
    return summary


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
    """
    if not hygiene_complete(vault_root):
        return False
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
    return True
