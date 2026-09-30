"""Review drafts for lessons whose target this workspace does not own (#728-D).

The routing contract has four destinations for a reusable lesson, and three of
them are not an edit to a file this workspace owns:

* an **owned** skill → :mod:`ciao.skill_proposals`, which owns a canonical
  source, its revision and its per-finding settlement;
* a **packaged / mirrored / shared** skill → a GitHub issue on whichever project
  actually maintains it. Never an edit: a stock copy under ``.claude/skills`` is
  refreshed on every sync, so a local edit is discarded, and the install-wide
  ``skills-src/`` mirror is nobody's to write;
* **no** fitting skill → a new owned skill, which does not exist yet and so
  cannot be pointed at by the edit-only filer.

The last two are recorded here as ``[review]`` drafts. There is deliberately no
new top-level proposal kind: the decision is a *routing* decision, which is what
``[review]`` already means, and its accept is already manual. What is new is the
sidecar beside the queue bullet, because a draft is not one line — it names a
packaged skill, sometimes an owning repository and version, a **sanitized**
lesson for a public issue, and the private provenance the sanitized version was
derived from. The bullet carries an id; this module owns everything behind it.

**One draft per distinct change, not per sighting.** Identity is derived from
``(workspace, target, change)`` — the target the lesson concerns and the change
itself — so a nightly run that reaches the same conclusion from a different
transcript folds into the record that is already queued rather than opening a
second row a person then has to reject twice. Evidence accumulates on the record;
the change text is what is compared.

**A draft is not a filing.** :func:`file_draft` writes a record and a queue
bullet, both local. :func:`approve_draft` is the only thing that reaches GitHub,
and it refuses when the caller says it is unattended — the same rule the
unattended capsule and :data:`ciao.memory_policy.UNATTENDED_DEFERRED_ACTIONS`
already state for any public action. It **searches before it creates**: a lesson
somebody else already reported upstream should be linked, not duplicated, and a
retry after a network failure re-runs the search rather than filing a second
issue for the same finding. A GitHub request that fails or comes back ambiguous
leaves the draft exactly as it was, pending, and says why.

**Sanitized, and the sanitization is not optional.** A public issue body must be
reproducible by a stranger and must not carry anything private: a verbatim
transcript excerpt, a chat or archive path, a person, a vault path, a credential.
:func:`sanitize_lesson` is the function that decides, it refuses rather than
guesses, and the **local record keeps the private provenance** either way
(:attr:`UpstreamDraft.private_evidence`) — so refusing to publish is never the
same as losing the finding.

The stock catalog is read, never written. There is no code path in this module
that opens a packaged, mirrored or shared skill for writing, and
``tests/test_upstream_drafts.py`` asserts the shipped files are byte-identical
after a full draft/approve/reject cycle.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ciao.learning_records import LEARNINGS_RELATIVE, normalized_statement
from ciao.memory_proposals import (
    MemoryProposal,
    append_proposals,
    read_decisions,
    record_dismissal,
    record_promotion,
)
from ciao.memory_receipts import queue_lock, write_queue_atomically

if TYPE_CHECKING:  # ``CiaoConfig`` is only ever a type here; duck-typed below.
    from ciao.config import CiaoConfig

logger = logging.getLogger(__name__)

#: The queue bullet's kind. ``[review]`` and not a new kind: the decision this
#: row carries is a routing decision, which is what ``[review]`` already is, and
#: its accept is already manual, so a one-click "apply" on a public issue can
#: never be wired to it by accident.
DRAFT_KIND = "review"

#: Where the sidecars live, beside the queue they belong to. A directory rather
#: than one file because a workspace accumulates drafts and a settled one stays
#: on disk as a record of the decision.
SIDECAR_RELATIVE = ("Workspace", "Skill-Drafts")

#: The queue file the bullet is appended to.
_QUEUE_RELATIVE = ("Workspace", "Memory-Proposals.md")

#: The sidecar's own version, so a reader that meets a shape it was not written
#: for refuses rather than guessing. A draft read as the wrong thing is a public
#: issue filed from a record nobody wrote.
SIDECAR_SCHEMA = 1

#: The two destinations a draft can name.
UPSTREAM_ISSUE = "upstream_issue"
NEW_SKILL = "new_skill"
TARGETS: tuple[str, ...] = (UPSTREAM_ISSUE, NEW_SKILL)

# ── Draft lifecycles ────────────────────────────────────────────────────────
#
# Three lifecycles, because there is nothing an approval adds. `pending` is the
# whole of an unattended run's reach and the state a person finds a draft in
# before deciding. `filed` records the URL, so a local "submitted" is
# distinguishable from a lesson that is actually installed — filing upstream does
# not deploy anything locally. `rejected` is final: a draft a person turned down
# is not re-opened by the next pass, and re-filing it is refused by name.
#
# There is deliberately no `approved` state. A person's yes is not durable
# evidence worth keeping: it either produces a URL or it does not, and a draft
# whose filing failed is retried from `pending` because the retry re-runs the
# search — which is what stops a second issue for the same finding. A middle
# state nobody reads would only be a way for a row to look answered while nothing
# had been published.

DRAFT_PENDING = "pending"
DRAFT_FILED = "filed"
DRAFT_REJECTED = "rejected"
DRAFT_LIFECYCLES: tuple[str, ...] = (
    DRAFT_PENDING,
    DRAFT_FILED,
    DRAFT_REJECTED,
)
#: A lifecycle that is an answer rather than a wait. What a settled draft carries
#: is not the same thing as a lesson that landed: `filed` means the issue exists,
#: and the lesson is still not in any local skill.
SETTLED_DRAFTS = frozenset({DRAFT_FILED, DRAFT_REJECTED})

#: The keys one ``origins`` entry in a filed draft may carry. Deliberately the
#: same set the skill-proposal filer accepts, and for the same reason: a link is
#: machine identity, and a key this code does not understand is a field whose loss
#: nobody would notice until a learning had been declared settled on a partial
#: read. So an unknown key fails the entry rather than being skipped.
_ORIGIN_FIELDS = frozenset({"learning_id", "finding", "source_revision", "summary"})

#: What a public body may not contain. Each pattern is a class of private thing
#: rather than one spelling, and the check refuses the whole body rather than
#: redacting it: a partially redacted issue is one whose author no longer knows
#: what they published, and the local record already holds the private text.
_PRIVATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a chat or archive path", re.compile(r"(?:^|[\s(])(?:logs|Chats|transcripts)/")),
    (
        "a vault path",
        re.compile(r"(?:^|[\s(])memory-vault/"),
    ),
    (
        "an absolute home path",
        re.compile(r"(?:^|[\s(])/(?:Users|home)/"),
    ),
    (
        "a transcript excerpt marker",
        re.compile(r"\bturn \d+\b|\bexcerpt\b|verbatim", re.IGNORECASE),
    ),
    (
        "a credential",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|"
            r"AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9-]{10,})\b"
        ),
    ),
    (
        "an email address",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    ),
    (
        "a home-relative placeholder",
        re.compile(
            r"<\s*(?:the\s+)?(?:user|owner|company|customer|client|account|repo)s?\b",
            re.IGNORECASE,
        ),
    ),
)


class DraftError(ValueError):
    """Base for the refusals this module raises, so a caller catches one name."""


class DraftRefused(DraftError):
    """A proposed draft is not one that may be filed or acted on."""


class UnattendedRefused(DraftRefused):
    """An unattended run asked to reach GitHub.

    Opening or commenting on a public issue is already
    :data:`ciao.memory_policy.UNATTENDED_DEFERRED_ACTIONS`; this is the
    enforcement, so the deferral is a raised error rather than a prompt nobody
    reads.
    """


class SanitizeRefused(DraftError):
    """A lesson body carries something that may not be published.

    Raised by :func:`sanitize_lesson`, naming the pattern. The finding is not
    lost — the caller keeps the private text in
    :attr:`UpstreamDraft.private_evidence` and files a body that is reproducible
    without the private part.
    """


@dataclass(frozen=True, slots=True)
class UpstreamDraft:
    """One lesson routed to a destination this workspace does not own.

    ``body`` is the **sanitized**, publishable lesson for an upstream issue: what
    the reader did, what went wrong, and the instruction that would have
    prevented it, with nothing private in it. ``private_evidence`` is the local
    provenance — the excerpts, paths and learning links the sanitized body was
    derived from — and it never leaves this workspace.

    ``skill`` names the packaged/mirrored skill an issue concerns, or the
    proposed name for a new one. ``repository`` and ``version`` are what the
    issue body has to state to be actionable ("against ``owner/repo`` at
    ``v1.2.3``"), and are empty when the owner is not identifiable — which is
    itself an attended question, never a guess at a public repository.

    ``lifecycle`` is the one a person moved it to. Nothing here moves it but
    :func:`approve_draft` and :func:`reject_draft`, so an unattended run can
    leave a draft and cannot retire one.
    """

    id: str
    workspace: str
    target: str
    skill: str
    title: str
    body: str
    lifecycle: str = DRAFT_PENDING
    repository: str = ""
    version: str = ""
    change: str = ""
    private_evidence: str = ""
    issue_url: str = ""
    updated_at: str = ""
    reason: str = ""
    origins: tuple[dict[str, str], ...] = ()

    @property
    def settled(self) -> bool:
        """Whether a person has answered this draft."""
        return self.lifecycle in SETTLED_DRAFTS

    @property
    def key(self) -> tuple[str, str, str]:
        """The identity of the *change*, for merge dedupe.

        The target and the change, normalized — not the sighting. Two passes that
        reached the same conclusion about the same skill from different
        transcripts are one row a person decides once; a change to a different
        skill, or a different change to the same one, is a different row.
        """
        return (
            self.target,
            normalized_statement(self.skill),
            normalized_statement(self.change or self.title),
        )

    def to_dict(self) -> dict[str, Any]:
        """The mapping stored in the sidecar, with blanks omitted."""
        raw: dict[str, Any] = {
            "schema": SIDECAR_SCHEMA,
            "id": self.id,
            "workspace": self.workspace,
            "target": self.target,
            "skill": self.skill,
            "lifecycle": self.lifecycle,
            "repository": self.repository,
            "version": self.version,
            "title": self.title,
            "body": self.body,
            "change": self.change,
            "private_evidence": self.private_evidence,
            "issue_url": self.issue_url,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "origins": [dict(origin) for origin in self.origins],
        }
        return {name: value for name, value in raw.items() if value}


# ── Identity ────────────────────────────────────────────────────────────────


def draft_id(workspace: str, target: str, skill: str, change: str) -> str:
    """The stable identity of one routed change in one workspace.

    Derived from the pair that says *what* is being proposed and never from when
    it was seen, so a re-run folds into the record already on disk. Normalized
    the way :attr:`UpstreamDraft.key` is, so a re-worded-but-identical change is
    the same row.
    """
    basis = "\x00".join((
        workspace,
        target,
        normalized_statement(skill),
        normalized_statement(change),
    ))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def sidecar_dir(config: CiaoConfig, workspace: str) -> Path:
    """The sidecar directory for one workspace, derived from the registry."""
    return Path(config.workspace_vault_root(workspace)).joinpath(*SIDECAR_RELATIVE)


def sidecar_path(config: CiaoConfig, workspace: str, draft: str) -> Path:
    """The one file that holds ``draft``'s record in ``workspace``."""
    _check_draft_name(draft)
    return sidecar_dir(config, workspace) / f"{draft}.json"


def queue_path(config: CiaoConfig, workspace: str) -> Path:
    """The review queue the ``[review]`` bullet is appended to."""
    return Path(config.workspace_vault_root(workspace)).joinpath(*_QUEUE_RELATIVE)


# ── Sanitization ────────────────────────────────────────────────────────────


def sanitize_lesson(text: str) -> str:
    """The publishable form of ``text``, or :class:`SanitizeRefused`.

    What a public issue may carry is a lesson a stranger can reproduce: the
    situation, the wrong result, and the instruction that would have avoided it.
    What it may not carry is this workspace's private material, and the patterns
    are checked against the whole body rather than redacted out of it — a body
    that needed editing is a body whose author would have to guess what was
    published, and the private text is kept in
    :attr:`UpstreamDraft.private_evidence` regardless.

    Raises :class:`SanitizeRefused` naming the first pattern matched, with the
    body left untouched for the caller to rephrase. Nothing is sent anywhere by
    this function; it is a gate, not a filter.
    """
    body = text.strip()
    if not body:
        raise SanitizeRefused("the lesson body is empty: an issue with no body is not a report")
    for label, pattern in _PRIVATE_PATTERNS:
        match = pattern.search(body)
        if match is not None:
            raise SanitizeRefused(
                f"the lesson body carries {label} ({match.group(0).strip()!r}); a "
                "public issue must be reproducible from the instruction alone. "
                "Rephrase it without the private part and file that — the local "
                "record keeps the private evidence either way"
            )
    return body


# ── Filing ──────────────────────────────────────────────────────────────────


def file_draft(
    config: CiaoConfig,
    workspace: str,
    *,
    target: str,
    skill: str,
    title: str,
    change: str,
    body: str = "",
    repository: str = "",
    version: str = "",
    private_evidence: str = "",
    origins: Sequence[dict[str, str]] = (),
) -> UpstreamDraft:
    """File one deduplicated ``[review]`` draft, and return what is stored.

    One record per ``(target, skill, change)`` in the workspace, so a second
    pass that reaches the same conclusion updates the record rather than opening
    a row a person would have to answer twice. Evidence and learning links
    accumulate; ``lifecycle`` does not move, because filing is a question and
    only :func:`approve_draft` / :func:`reject_draft` answer one. A settled
    record keeps accumulating evidence without re-entering the queue, and a
    re-filing of a rejected change is refused by name rather than silently
    reopening.

    ``body`` is validated with :func:`sanitize_lesson` when there is one. A draft
    with no body is a new-skill proposal, whose whole point is that the
    instruction is not a public artifact yet.

    A record that is gone but whose decision survives in the sidecar is written
    back *settled*, for the reason :func:`decided_with` gives: the sidecar is
    what outlives the row, and a rejection a person gave is exactly the answer
    the next pass must not re-ask.

    The bullet is appended only when the record is not already queued, and the
    append is a no-op for a queue that already holds the identical bullet — so a
    re-run cannot stack two rows for one finding however it interleaves with the
    sidecar write.
    """
    if target not in TARGETS:
        raise DraftRefused(
            f"unknown draft target {target!r}: expected one of {', '.join(TARGETS)}"
        )
    if not skill.strip():
        raise DraftRefused("a draft names the skill it is about; the name is empty")
    if not title.strip():
        raise DraftRefused("a draft needs a title a person can triage by")
    if not change.strip():
        raise DraftRefused(
            "a draft needs the change it proposes: without it there is nothing to "
            "approve, file or create"
        )
    if body:
        body = sanitize_lesson(body)
    links = _validated_origins(origins)
    identity = draft_id(workspace, target, skill, change)

    path = sidecar_path(config, workspace, identity)
    with queue_lock(path):
        existing = read_sidecar(path, workspace)
        incoming = UpstreamDraft(
            id=identity,
            workspace=workspace,
            target=target,
            skill=skill.strip(),
            title=title.strip(),
            body=body,
            change=change.strip(),
            repository=repository.strip(),
            version=version.strip(),
            private_evidence=private_evidence.strip(),
            origins=links,
        )
        if existing is None and decided_with(config, workspace, identity):
            # The record is gone but the decision is not: a settled draft whose
            # sidecar somebody deleted would otherwise be re-queued by the next
            # pass, and a rejection a person gave is exactly the thing that must
            # not come back. So the record is written settled, and the evidence
            # this pass brought is added to it rather than re-asked.
            incoming = replace(
                incoming,
                lifecycle=DRAFT_REJECTED,
                reason="reconstructed from the decision sidecar: the record was "
                "deleted after it was decided",
            )
            merged = incoming
        else:
            merged = _merge(existing, incoming)
        write_queue_atomically(path, render_sidecar(merged))
        if not merged.settled and not _bullet_present(config, workspace, identity):
            _append_bullet(config, workspace, merged)
    logger.info("Filed %s draft %s in %s", target, identity, workspace)
    return merged


def _append_bullet(
    config: CiaoConfig, workspace: str, draft: UpstreamDraft
) -> None:
    """Append the ``[review]`` bullet for ``draft`` to the workspace's queue.

    The payload is the draft id, the way a ``[note_edit]`` row carries its
    sidecar id: one line cannot hold a target, a sanitized body and a private
    provenance, so the line names the record and the record holds the rest.
    The text says which destination it is, because that is the decision a person
    is being asked for and it has to survive in the queue itself.
    """
    summary = draft.body or draft.change
    text = f"[{_target_label(draft.target)} {draft.skill}] {summary} — {draft.title}"
    append_proposals(
        [
            MemoryProposal(
                target=DRAFT_KIND,
                text=text,
                # The id appears twice on purpose and the two copies do different
                # jobs. The bracket payload is the row's address, so a parser and
                # a human both reach the record by it; the source label is the
                # decision key, so the settlement matches this row and not some
                # other row whose text happens to mention the id.
                source_section=f"skill-draft:{draft.id}",
                payload=draft.id,
            )
        ],
        config.workspace_vault_root(workspace),
    )


def _target_label(target: str) -> str:
    """The words the queue bullet uses for a draft's destination."""
    if target == NEW_SKILL:
        return "new skill"
    return "upstream issue"


def _bullet_present(config: CiaoConfig, workspace: str, identity: str) -> bool:
    """Whether the queue already holds this draft's bullet."""
    from ciao.proposal_kinds import parse_bullet

    path = queue_path(config, workspace)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return any(
        (bullet := parse_bullet(line)) is not None and bullet.target == identity
        for line in lines
    )


def _merge(
    existing: UpstreamDraft | None, incoming: UpstreamDraft
) -> UpstreamDraft:
    """What the sidecar holds after ``incoming`` is merged into ``existing``.

    Identity, lifecycle, the recorded issue URL and any reason are never taken
    from an incoming record: those are decisions, and a filing must not undo one
    nor lose the URL a person already published. A field the incoming record
    leaves empty does not blank the stored one, so a stub write cannot erase a
    good body. Evidence and learning links accumulate, deduped.
    """
    if existing is None:
        return replace(incoming, updated_at=_now())
    return replace(
        existing,
        # A new private-evidence note joins the old one rather than replacing
        # it: a re-run usually sees a different transcript excerpt, and losing
        # the first one would lose the sighting that led here.
        private_evidence=_join_notes(
            existing.private_evidence, incoming.private_evidence
        ),
        body=incoming.body or existing.body,
        change=incoming.change or existing.change,
        repository=incoming.repository or existing.repository,
        version=incoming.version or existing.version,
        title=incoming.title or existing.title,
        skill=incoming.skill or existing.skill,
        origins=_merge_origins(existing.origins, incoming.origins),
        updated_at=_now(),
    )


def _join_notes(existing: str, incoming: str) -> str:
    """Two evidence notes, one field, with a repeat dropped."""
    left = existing.strip()
    right = incoming.strip()
    if not right or right == left:
        return left
    if not left:
        return right
    return f"{left}\n{right}"


def _merge_origins(
    existing: tuple[dict[str, str], ...], incoming: tuple[dict[str, str], ...]
) -> tuple[dict[str, str], ...]:
    """Learning links, deduped by ``(learning_id, finding)`` in stored order."""
    merged = list(existing)
    seen = {
        (item.get("learning_id", ""), normalized_statement(item.get("finding", "")))
        for item in existing
    }
    for item in incoming:
        key = (item.get("learning_id", ""), normalized_statement(item.get("finding", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(item))
    return tuple(merged)


def _validated_origins(
    origins: Sequence[dict[str, str]],
) -> tuple[dict[str, str], ...]:
    """The learning links as stored, refusing one this code would half-read.

    Same rule the skill-proposal filer follows and for the same reason: a link
    that is dropped on the next write is a lesson quietly declared settled. An
    unknown key fails the entry rather than being skipped, and ``learning_id`` is
    required because a link naming no learning is not a link.
    """
    links: list[dict[str, str]] = []
    for index, item in enumerate(origins):
        if not isinstance(item, dict):
            raise DraftRefused(f"origins[{index}] must be an object")
        unknown = sorted(set(item) - _ORIGIN_FIELDS)
        if unknown:
            raise DraftRefused(
                f"origins[{index}] has unknown field(s) {', '.join(unknown)}"
            )
        learning_id = str(item.get("learning_id") or "").strip()
        if not learning_id:
            raise DraftRefused(f'origins[{index}] needs a non-empty "learning_id"')
        links.append({
            name: " ".join(str(value).split())
            for name, value in item.items()
            if str(value).strip()
        })
    return tuple(links)


# ── Reading ─────────────────────────────────────────────────────────────────


def read_sidecar(path: Path, workspace: str) -> UpstreamDraft | None:
    """One record, or ``None`` when there is nothing usable to read.

    A file this cannot speak for at all — missing, unreadable, not JSON, an
    unknown schema, or a record naming another workspace — reads as ``None``
    rather than raising. A queue you cannot show is worse than one row short, and
    the writer that finds this writes a fresh record; the two are equivalent for
    the caller, which is the point.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != SIDECAR_SCHEMA:
        return None
    stored_workspace = str(raw.get("workspace") or "")
    if stored_workspace and stored_workspace != workspace:
        # A learning id is workspace-scoped, so a record claiming another
        # workspace's is a link this vault cannot honour. Refusing beats
        # rewriting it under this one, which would make a foreign id look
        # verified here.
        return None
    lifecycle = str(raw.get("lifecycle") or DRAFT_PENDING)
    if lifecycle not in DRAFT_LIFECYCLES:
        return None
    target = str(raw.get("target") or "")
    if target not in TARGETS:
        return None
    skill = str(raw.get("skill") or "")
    identity = str(raw.get("id") or "")
    if not skill or not identity:
        return None
    origins = raw.get("origins")
    return UpstreamDraft(
        id=identity,
        workspace=stored_workspace or workspace,
        target=target,
        skill=skill,
        title=str(raw.get("title") or ""),
        body=str(raw.get("body") or ""),
        lifecycle=lifecycle,
        repository=str(raw.get("repository") or ""),
        version=str(raw.get("version") or ""),
        change=str(raw.get("change") or ""),
        private_evidence=str(raw.get("private_evidence") or ""),
        issue_url=str(raw.get("issue_url") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        reason=str(raw.get("reason") or ""),
        origins=tuple(
            dict(item) for item in origins if isinstance(item, dict)
        ) if isinstance(origins, list) else (),
    )


def render_sidecar(draft: UpstreamDraft) -> str:
    """The record's file text: a pure, deterministic function of the record.

    JSON rather than Markdown because every field here is machine identity — a
    workspace, a lifecycle, a URL — and none of it should be readable by a
    regex that has to guess where a heading ends. The private evidence is in
    here too, which is the point: the file is the local record, it lives in the
    user's own vault, and it is what a re-run folds into.
    """
    return json.dumps(draft.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def read_records(config: CiaoConfig, workspace: str) -> list[UpstreamDraft]:
    """Every draft in ``workspace``, settled ones included, by id.

    Settled records are read because the fold that answers "was this learning
    dealt with?" needs them: a learning split across two drafts is only answered
    when both are, and one that is gone is not an answer.
    """
    directory = sidecar_dir(config, workspace)
    if not directory.is_dir():
        return []
    found = [
        draft
        for path in sorted(directory.glob("*.json"))
        if (draft := read_sidecar(path, workspace)) is not None
    ]
    found.sort(key=lambda item: item.id)
    return found


def read_queue(config: CiaoConfig, workspace: str) -> list[UpstreamDraft]:
    """Every draft in ``workspace`` still awaiting a decision, by id."""
    return [draft for draft in read_records(config, workspace) if not draft.settled]


def find_draft(config: CiaoConfig, draft_id: str) -> UpstreamDraft | None:
    """The open draft with this id, across every registered workspace.

    ``None`` when nothing open has it, which is the idempotent case: a second
    approval of an already-filed row has nothing left to file.
    """
    for workspace in config.workspace_names():
        for draft in read_queue(config, workspace):
            if draft.id == draft_id:
                return draft
    return None


def find_record(config: CiaoConfig, draft_id: str) -> UpstreamDraft | None:
    """The draft with this id whatever its lifecycle, across every workspace.

    :func:`find_draft` is what a decision asks, and its ``None`` is the honest
    "nothing to decide". This is what a *refusal* asks, so that trying to file
    a change somebody already rejected says so by name rather than reporting an
    unknown id — the two answers lead to different fixes, and a caller told
    "no such draft" would go looking for a typo rather than for the rejection.
    """
    for workspace in config.workspace_names():
        for draft in read_records(config, workspace):
            if draft.id == draft_id:
                return draft
    return None


# ── GitHub ──────────────────────────────────────────────────────────────────
#
# Two thin wrappers over `gh`, both taking the operator's own auth so the app
# never handles a token. They are module-level names rather than lambdas so a
# caller (and a test) can pass a different implementation, and so the subprocess
# boundary is one place rather than scattered through the decision logic.


def _run_gh(args: Sequence[str], timeout: float = 30.0) -> str:
    """Run one ``gh`` call and return stdout, or raise ``OSError``."""
    try:
        completed = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise OSError(
            "the GitHub CLI (gh) is not installed, so nothing was filed"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"GitHub did not answer within {timeout:g}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "unknown error"
        raise OSError(f"gh failed: {detail}") from exc
    return completed.stdout


def search_existing_issues(*, title: str, body: str) -> list[str]:
    """The URLs of open upstream issues that already look like this change.

    The search is deliberately narrow — the title's distinctive words, in the
    issue list — because a wide query that returns several plausible hits is
    exactly the ambiguity :func:`approve_draft` holds on rather than guessing
    at. An empty list means "nothing found", not "nothing exists": a repository
    the operator cannot read looks identical from here, and creating a duplicate
    in a private fork is the outcome the search is there to prevent.

    Raises ``OSError`` when ``gh`` could not be reached or refused, which
    :func:`approve_draft` turns into a pending hold rather than a filing.
    """
    words = [word for word in re.findall(r"[A-Za-z0-9_-]{4,}", title)][:6]
    if not words:
        return []
    query = " ".join(words)
    stdout = _run_gh([
        "issue",
        "list",
        "--state",
        "open",
        "--search",
        query,
        "--limit",
        "20",
        "--json",
        "url,title",
    ])
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [
        str(row.get("url"))
        for row in rows
        if isinstance(row, dict) and str(row.get("url") or "").strip()
    ]


def create_issue(*, title: str, body: str, repository: str = "") -> str:
    """Create one upstream issue and return its URL.

    ``repository`` is passed through only when the draft named one. Guessing a
    public repository is exactly what the plan forbids, so an empty value means
    ``gh``'s own default target (the repository of the current directory), and
    the attended caller is the one that has to be standing there.
    """
    args = ["issue", "create", "--title", title, "--body", body]
    if repository.strip():
        args += ["--repo", repository.strip()]
    return _run_gh(args).strip()


# ── Decisions ───────────────────────────────────────────────────────────────


def approve_draft(
    config: CiaoConfig,
    draft_id: str,
    *,
    unattended: bool = False,
    reason: str = "",
    search: Any = None,
    create: Any = None,
) -> UpstreamDraft | None:
    """Act on an approved draft: link a matching issue, or file a new one.

    **Attended only.** ``unattended=True`` raises :class:`UnattendedRefused`
    before anything is read or written. Opening a public issue is already
    :data:`ciao.memory_policy.UNATTENDED_DEFERRED_ACTIONS`, and an unattended run
    that prepares the draft is the whole of what it may do — so the enforcement
    belongs here, where the request would be made, and not in a prompt nobody
    reads.

    **Search before create.** :func:`search_existing_issues` is asked first and
    a hit is *linked*, not duplicated: a lesson somebody else already reported
    upstream needs a link, not a second issue saying the same thing. Only when
    the search returns nothing is :func:`create_issue` called, and the URL it
    returns is recorded on the record, so a retry — or a person asking six weeks
    later — can see what was actually published.

    A ``gh`` failure or an ambiguous result returns the draft **unchanged and
    still pending**, with the reason recorded, because "we could not reach
    GitHub" is not an answer and a second run has to be able to try again. The
    search runs on every attempt, so a retry after a partial success links the
    issue the first attempt created rather than opening another.

    A draft a person already rejected is refused here: rejection is the answer,
    and a draft is not re-filed behind it.
    """
    if unattended:
        raise UnattendedRefused(
            "opening or commenting on a public GitHub issue is an attended action: "
            "an unattended run may prepare the draft and report it under **What "
            "needs you**, and a person approves the filing"
        )
    # Resolved at call time rather than captured as default arguments, so the
    # subprocess boundary is one module-level name a caller (or a test) can
    # replace instead of a value frozen at import.
    search = search or search_existing_issues
    create = create or create_issue
    draft = find_draft(config, draft_id)
    if draft is None:
        settled = find_record(config, draft_id)
        if settled is not None and settled.lifecycle == DRAFT_REJECTED:
            raise DraftRefused(
                f"draft {draft_id} was rejected "
                f"({settled.reason or 'no reason recorded'}); a rejected change "
                "is not re-filed"
            )
        return None
    if draft.target != UPSTREAM_ISSUE:
        raise DraftRefused(
            f"draft {draft_id} proposes a {draft.target}, not an upstream issue: "
            "use create_new_skill to act on it, so the creation is verified "
            "before the row is settled"
        )
    body = sanitize_lesson(draft.body)
    title = draft.title.strip()
    if not title:
        raise DraftRefused(f"draft {draft_id} has no title to file an issue under")

    hits = search(title=title, body=body)
    if len(hits) > 1:
        # Ambiguous on purpose. Two issues that both look like this finding means
        # this code cannot say which one is the match, and picking either would
        # attach the record to the wrong discussion.
        return _hold(
            config,
            draft,
            DRAFT_PENDING,
            reason=(
                f"{len(hits)} open issues already match this change; link it by "
                "hand in an attended turn, or close the duplicate first"
            ),
        )
    if hits:
        return _settle(
            config,
            draft,
            DRAFT_FILED,
            issue_url=hits[0],
            reason=reason or "linked to the existing upstream issue",
        )
    try:
        url = create(title=title, body=body, repository=draft.repository)
    except OSError as exc:
        return _hold(
            config,
            draft,
            DRAFT_PENDING,
            reason=f"could not reach GitHub: {exc}. Nothing was filed; retry later",
        )
    if not url:
        return _hold(
            config,
            draft,
            DRAFT_PENDING,
            reason="GitHub returned no URL for the new issue. Nothing was filed; "
            "retry, and the retry searches before it creates",
        )
    return _settle(config, draft, DRAFT_FILED, issue_url=url, reason=reason)


def reject_draft(
    config: CiaoConfig, draft_id: str, reason: str = "", *, via: str = "pwa"
) -> UpstreamDraft | None:
    """Turn an open draft down, and say so in the decisions sidecar.

    A rejection is the one answer that retires a finding without touching a
    file, and it is deliberately a person-only step: the unattended rule says a
    run that cannot ask must defer, and "this does not look worth filing" is a
    judgement about somebody else's repository. Once rejected, the change is not
    re-offered — :func:`file_draft` keeps a settled record settled and
    :func:`approve_draft` refuses it by name.
    """
    draft = find_draft(config, draft_id)
    if draft is None:
        return None
    return _settle(config, draft, DRAFT_REJECTED, reason=reason, via=via)


def _hold(
    config: CiaoConfig, draft: UpstreamDraft, lifecycle: str, *, reason: str
) -> UpstreamDraft:
    """Record a reason and keep the row open."""
    return _settle(
        config,
        draft,
        lifecycle,
        reason=reason,
        via="upstream",
        record_decision=False,
    )


def _settle(
    config: CiaoConfig,
    draft: UpstreamDraft,
    lifecycle: str,
    *,
    issue_url: str = "",
    reason: str = "",
    via: str = "pwa",
    record_decision: bool = True,
) -> UpstreamDraft:
    """Write the decision, then flip the record. Order is the safety property.

    The sidecar row is what outlives the row: a settled draft may be deleted
    later, and the next pass must not re-ask what was already answered. It goes
    in BEFORE the lifecycle flips, so a sidecar that could not be written leaves
    the draft open and visible rather than answered and unrecorded.
    """
    if lifecycle not in DRAFT_LIFECYCLES:
        raise DraftRefused(
            f"{lifecycle!r} is not a draft lifecycle: expected one of "
            f"{', '.join(DRAFT_LIFECYCLES)}"
        )
    path = sidecar_path(config, draft.workspace, draft.id)
    with queue_lock(path):
        stored = read_sidecar(path, draft.workspace) or draft
        settled = replace(
            stored,
            lifecycle=lifecycle,
            issue_url=issue_url or stored.issue_url,
            reason=reason or stored.reason,
            updated_at=_now(),
        )
        if record_decision:
            _record_decision(config, settled, via=via)
        write_queue_atomically(path, render_sidecar(settled))
    _remove_bullet(config, settled)
    logger.info("Draft %s is now %s", settled.id, lifecycle)
    return settled


def _record_decision(
    config: CiaoConfig, draft: UpstreamDraft, *, via: str
) -> None:
    """One sidecar row per learning link, keyed so it cannot read as a memory fact.

    The same durable record the memory queue keeps, under a synthetic
    ``skill-draft:<id>`` text: a routing decision about a lesson is not a
    remembered fact, and two queues writing the same wording would let one
    dismiss the other's rows.
    """
    record = record_promotion if draft.lifecycle == DRAFT_FILED else record_dismissal
    decisions = queue_path(config, draft.workspace)
    for origin in draft.origins or (None,):
        record(
            decisions,
            text=decision_text(draft.id),
            kind="skill",
            via=via,
            outcome=draft.reason,
            proposal_id=draft.id,
            learning_id=origin.get("learning_id", "") if origin else "",
            finding=origin.get("finding", "") if origin else "",
        )


def decision_text(draft_id: str) -> str:
    """The synthetic sidecar text a draft's decision is recorded under."""
    return f"skill-draft:{draft_id}"


def _remove_bullet(config: CiaoConfig, draft: UpstreamDraft) -> None:
    """Take a settled draft's ``[review]`` bullet out of the queue.

    Through the shared remover, never a hand edit of the file: the queue is
    line-oriented Markdown that three readers parse, and a bullet removed by
    editing is a bullet those readers disagree about. A bullet that is not there
    — a second settle, or a queue the person already cleared — is not an error.
    """
    from ciao.memory_proposals import remove_proposal_by_substring

    path = queue_path(config, draft.workspace)
    try:
        remove_proposal_by_substring(path, decision_text(draft.id))
    except OSError:  # pragma: no cover - an unreadable queue stays as it is
        logger.exception("Could not clear draft %s from %s", draft.id, path)


def decided_with(config: CiaoConfig, workspace: str, draft_id: str) -> bool:
    """Whether the decisions sidecar already records a decision for this draft.

    Read through :func:`ciao.memory_proposals.read_decisions` rather than
    ``was_dismissed``, for the reason :mod:`ciao.skill_proposals` gives: that
    helper gives up on a workspace with no ``Memory-Proposals.md`` yet, which is
    exactly the install where a draft is the only thing in the queue and the
    decision would then be invisible to the pass that must honour it.
    """
    wanted = decision_text(draft_id)
    try:
        rows = read_decisions(queue_path(config, workspace))
    except OSError:  # pragma: no cover - an unreadable sidecar decides nothing
        return False
    return any(row.get("text") == wanted for row in rows)


# ── New-skill creation ──────────────────────────────────────────────────────


def create_new_skill(
    config: CiaoConfig,
    draft_id: str,
    *,
    content: str,
    sync: Any = None,
) -> UpstreamDraft | None:
    """Create the owned skill a new-skill draft proposes, then settle the row.

    The attended half of the new-skill path, and it is a different operation
    from :func:`approve_draft` on purpose: an issue filed upstream is a thing
    that happened, while a skill created locally is a file somebody's next
    session will load, so the row is only settled once the creation is **read
    back and checked**.

    The checks, in order, and what each is for:

    1. :func:`ciao.skills_inventory.create_owned_skill` refuses a name that
       exists as anything — a directory, a symlink, an installed copy. A
       creation that overwrote one would be an edit wearing a creation's
       permissions, so the collision is the caller's problem to resolve by
       choosing another name, not something this file works around.
    2. The readback is the resolver's, so the caller holds a revision of the
       bytes that are actually on disk rather than of the text it submitted.
    3. ``sync`` is called, because a skill that exists only in ``skills/`` is not
       a skill the providers can see. It is last and its failure is honest:
       the creation stands, and the row says the sync did not finish.

    A refusal raises and settles nothing. A successful creation settles the row
    ``filed`` — the local word for "the thing this row proposed now exists" — with
    the created path as its URL, so the record and the vault cannot disagree.
    """
    from ciao.skills_inventory import MAX_SKILL_BYTES, create_owned_skill

    draft = find_draft(config, draft_id)
    if draft is None:
        settled = find_record(config, draft_id)
        if settled is not None and settled.lifecycle == DRAFT_REJECTED:
            raise DraftRefused(
                f"draft {draft_id} was rejected "
                f"({settled.reason or 'no reason recorded'}); a rejected change "
                "is not re-created"
            )
        return None
    if draft.target != NEW_SKILL:
        raise DraftRefused(
            f"draft {draft_id} proposes an upstream issue, not a new skill: "
            "approve it to file the issue"
        )

    created = create_owned_skill(config, draft.workspace, draft.skill, content)
    note = f"created skills/{created.skill.name}/SKILL.md"
    if created.over_budget:
        note += (
            f"; it is {len(created.skill.content.encode('utf-8'))} bytes, over the "
            f"{MAX_SKILL_BYTES}-byte budget every load pays, so it is worth trimming"
        )
    synced = _sync_after_create(config, draft.workspace, sync)
    if not synced:
        note += "; the sync did not complete, so a provider may not see it yet"
    return _settle(
        config,
        draft,
        DRAFT_FILED,
        issue_url=str(created.skill.path),
        reason=note,
        via="new-skill",
    )


def _sync_after_create(
    config: CiaoConfig, workspace: str, sync: Any
) -> bool:
    """Run the workspace sync so the new skill is visible, and report honestly.

    A failure here is not a reason to un-create the file: the skill is on disk
    and the next sync will pick it up. So the return is a bool the caller turns
    into a sentence on the record, not an exception that would leave a created
    skill with an open row nobody can explain.

    The sync's own progress lines are captured rather than forwarded. It is a
    CLI-shaped function that prints, and the command calling it has a contract
    of its own — ``--json`` has to emit exactly one JSON document — so its
    stdout belongs in the log, where a failure is readable, rather than in the
    middle of the caller's output.
    """
    import contextlib
    import io

    def _run() -> None:
        if sync is not None:
            sync()
            return
        from ciao.sync_skills import sync_workspace_skills

        sync_workspace_skills(config.agent_root(workspace), workspace_name=workspace)

    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            _run()
    except Exception:  # noqa: BLE001 — a created skill is not undone by a failed sync
        logger.exception("Skill sync failed after creating a skill in %s", workspace)
        return False
    said = captured.getvalue().strip()
    if said:
        logger.info("Skill sync after creating a skill in %s: %s", workspace, said)
    return True


# ── The routing contract, as code ───────────────────────────────────────────


def route_for_skill(config: CiaoConfig, workspace: str, name: str) -> str:
    """Which routing path a skill named ``name`` belongs to in ``workspace``.

    The decision a pass is asked to make by reading, answered here by looking, so
    the three answers cannot drift: ``owned`` (there is a canonical source this
    workspace may edit), ``upstream`` (the name exists only as an installed,
    mirrored or shared copy, so the change belongs to whoever maintains it), or
    ``new`` (the name is not there at all, so the lesson needs a skill that does
    not exist yet).

    Answering this by reading the filesystem is the point. A model told "if the
    skill is stock, file upstream" has to work out which of three directories a
    name lives in, and a stock copy is installed under the very path a pass would
    otherwise edit. It is advisory — ``ciao skill-draft-add`` prints it as a note
    rather than refusing, because a workspace that forked a packaged skill owns
    the fork and may still want the change reported upstream.
    """
    from ciao.skills_inventory import resolve_owned_skill

    try:
        resolve_owned_skill(config, workspace, name)
    except ValueError:
        pass
    else:
        return "owned"
    if _name_exists_anywhere(config, workspace, name):
        return "upstream"
    return "new"


def _name_exists_anywhere(config: CiaoConfig, workspace: str, name: str) -> bool:
    """Whether ``name`` exists as anything other than an owned source."""
    if not name or name in {".", ".."} or name.startswith("."):
        return False
    if "/" in name or "\\" in name:
        return False
    try:
        root = config.agent_root(workspace)
    except (AttributeError, ValueError):  # pragma: no cover - config guard
        return False
    return any(
        (root / mirror / "skills" / name).exists() for mirror in (".claude", ".agents")
    )


def learnings_present(config: CiaoConfig, workspace: str) -> bool:
    """Whether this workspace holds a learnings document at all.

    The gate on the lesson-routing half of the memory pass: a workspace with no
    ``Workspace/Learnings.md`` has nothing for an inventory candidate to be a
    candidate *for*, and listing its whole catalog then would be the pass walking
    a catalog it was told not to walk.
    """
    try:
        path = Path(config.workspace_vault_root(workspace)).joinpath(LEARNINGS_RELATIVE)
    except (AttributeError, ValueError):  # pragma: no cover - config guard
        return False
    return path.is_file()


# ── Internals ───────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check_draft_name(draft: str) -> None:
    """One plain file name, or refuse before it reaches the filesystem."""
    if not draft or draft in {".", ".."} or draft.startswith("."):
        raise DraftRefused(f"{draft!r} is not a draft id")
    if "/" in draft or "\\" in draft:
        raise DraftRefused(f"{draft!r} is not one file under the sidecar directory")
