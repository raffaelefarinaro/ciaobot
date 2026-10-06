"""Provider-neutral application control plane for PWA, MCP, and CLI adapters.

The existing managers remain the owners of Ciaobot state and invariants.  This
module supplies a small, typed boundary around them so an agent-facing
transport never needs a browser cookie, a localhost curl command, or direct
knowledge of ``.runtime`` JSON layouts.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from ciao import vault_index
from ciao.async_reads import keyed_lock, run_read
from ciao.background import BackgroundRun, BackgroundRunError, TAIL_LINES
from ciao.fts_search import (
    get_db_path,
    index_vault,
    init_db,
    record_search_hits,
    search_vault,
    vault_key_prefix,
)
from ciao.memory_tool import memory_status as memory_status_payload
from ciao.memory_tool import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    resolve_region,
    update_region,
)
from ciao.schedules import (
    DEFAULT_INTERVAL_MINUTES,
    FREQUENCIES,
    INTERVAL_FREQUENCY,
    ScheduleEntry,
    compute_next_run,
    is_interval,
    normalize_interval_minutes,
    publish_automations_changed,
    stamp_fallback_project,
    wall_clock_time_error,
)
from ciao.task_attempts import (
    HANDOFF_ATTEMPTS,
    MAX_INSTRUCTIONS_CHARS,
    RESUMABLE_STATES,
    PreviousAttempt,
    TaskAttempt,
    TaskAttemptError,
    TaskAttemptStore,
    build_prompt,
    build_resume_prompt,
    task_delegation_helper,
)
from ciao.task_board import Actor, TaskBoardError, TaskBoardStore, TaskDocument
from ciao.task_log import render_item, upsert_item
from ciao.web.routes_webhooks import webhook_store
from ciao.webhooks import (
    WebhookStore,
    WebhookStoreError,
    WebhookTrigger,
)
from ciao.workspace_guide import guide_path

logger = logging.getLogger(__name__)

# A GWS health reading older than this is treated as stale: the monitor
# preserves prior state when probes are unavailable or checks are disabled, so
# a cached "valid" reading can outlive the token it described. Mirrors the
# 900s check interval in ``ciao.main`` plus a small grace period.
_GWS_HEALTH_STALE_AFTER = 1200.0

#: Heavy/hidden subtrees skipped by the ``file_surface`` suggestions walk so a
#: missing-path miss does not scan the whole workspace (install root) or let a
#: vendored file outrank a real workspace artifact.
_SUGGESTION_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".runtime", ".venv", ".mypy_cache", ".pytest_cache", "node_modules", "Logs", "__pycache__"}
)


def _suggestion_score(wanted_stem: str, stem: str) -> int:
    """A higher-is-better similarity of ``stem`` to ``wanted_stem``.

    Exact name wins, then substring containment either way, then edit-distance
    similarity so a typo like ``reprot.md`` vs ``report.md`` outranks an
    unrelated filename. Everything ranks (never ``None``) so the caller keeps a
    full suggestion list; only the *ordering* separates a genuine match from
    an unrelated fallback.
    """
    if not wanted_stem or not stem:
        return -2000
    if stem == wanted_stem:
        return 1000
    if wanted_stem in stem:
        return 700 - (len(stem) - len(wanted_stem))
    if stem in wanted_stem:
        return 600 - (len(wanted_stem) - len(stem))
    ratio = difflib.SequenceMatcher(None, wanted_stem, stem).ratio()
    if ratio >= 0.8:
        return int(500 * ratio)
    # Unrelated fallback, ranked below any edit-similar or substring match but
    # still present so a miss returns a full suggestion list.
    return -1000 - (len(stem) or 1)

@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    """Identity and scope attached to one managed provider process."""

    token_id: str
    chat_id: str
    project_id: str
    workspace: str
    provider: str
    # Only ``chat`` is ever issued. The type used to also allow ``automation``,
    # which nothing minted and nothing branched on, and a third value
    # ``handoff`` was smuggled past this annotation by a cast so a gate could
    # test for it. That gate never fired, because the sole issuing call site
    # hardcodes ``chat``; the handoff primitive has since been deleted
    # entirely. Keeping this a one-value Literal means any future restricted
    # role has to change the issuing path to type-check, instead of adding a
    # check that silently never runs.
    role: Literal["chat"] = "chat"

    def to_claims(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "AgentPrincipal":
        """Rebuild a principal from token claims.

        Anything other than ``chat`` in the claim is normalised away rather
        than trusted: a token is the one input here that does not come from
        our own call sites, so an unrecognised role must not become a
        privilege the rest of the code then reasons about.
        """
        return cls(
            token_id=str(claims.get("token_id") or ""),
            chat_id=str(claims.get("chat_id") or ""),
            project_id=str(claims.get("project_id") or ""),
            workspace=str(claims.get("workspace") or ""),
            provider=str(claims.get("provider") or ""),
        )


# One-release compatibility alias for the pre-S6 ``McpPrincipal`` name. No
# external consumer is known (the MCP surface is gone), but keeping the alias
# costs nothing and lets a downstream that imported it upgrade without a code
# change.
McpPrincipal = AgentPrincipal


# Permission modes ordered weakest to strongest, so a child chat's requested
# mode can be compared against its ceiling instead of being overwritten by it.
# ``plan`` is read-only, ``normal`` asks before acting, ``auto`` acts with safer
# defaults, ``bypass`` skips approvals. Mirrors ``BridgeMode`` in ciao.models
# and the SDK mapping in ciao.providers.claude.
_MODE_RANK: dict[str, int] = {"plan": 0, "normal": 1, "auto": 2, "bypass": 3}


class ControlPlaneError(ValueError):
    """Stable application error returned by MCP adapters."""

    suggestions: list[str] | None = None

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        suggestions = getattr(self, "suggestions", None)
        if suggestions:
            payload["suggestions"] = suggestions
        return payload


def _ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


# ---- Task operations (#1021, child B3 of #973) ------------------------
#
# ``ciao/task_board.py`` owns the storage contract for one workspace's task
# records: a typed error with a ``code``, and a rule that only a user may mark a
# task done. This block is the application layer over it — project membership
# (the store keeps the string without judging it) and the translation of a store
# refusal into an application error code a transport can act on.


#: One store refusal becomes one application error code. The codes are what the
#: agent CLI's ``error.code`` and the session routes' status mapping read, so
#: this table is the only place a ``TaskBoardError.code`` is given a meaning;
#: a store code with no row here surfaces as ``task_read_failed`` rather than a
#: silent default.
_TASK_ERROR_CODES: dict[str, tuple[str, bool]] = {
    # TaskBoardError.code -> (ControlPlaneError code, retryable)
    "not_found": ("task_not_found", False),
    # Stale means the caller must re-read and re-plan, never retry the same
    # revision: retryable in the sense that re-reading makes the edit succeed.
    "revision_conflict": ("task_revision_conflict", True),
    "invalid_task": ("task_invalid", False),
    "unsafe_path": ("task_invalid", False),
    "unsupported_schema": ("task_unsupported_schema", False),
    "completion_requires_user": ("task_completion_requires_user", False),
    "read_failed": ("task_read_failed", True),
}

#: The same translation for the delegation child's attempt store. Kept as its own
#: table rather than merged into the one above because the codes are different
#: facts: a ``TaskBoardError`` is about a record on disk, a ``TaskAttemptError``
#: is about a turn this engine launched.
_ATTEMPT_ERROR_CODES: dict[str, tuple[str, bool]] = {
    "not_found": ("task_attempt_not_found", False),
    "invalid_attempt": ("task_invalid", False),
    "read_failed": ("task_read_failed", True),
}


def _task_error(exc: TaskBoardError) -> ControlPlaneError:
    """Re-raise one store refusal as the control plane's typed error."""
    code, retryable = _TASK_ERROR_CODES.get(exc.code, ("task_read_failed", True))
    return ControlPlaneError(code, str(exc), retryable=retryable)


def _attempt_error(exc: TaskAttemptError) -> ControlPlaneError:
    """Re-raise one attempt-store refusal as the control plane's typed error."""
    code, retryable = _ATTEMPT_ERROR_CODES.get(exc.code, ("task_read_failed", True))
    return ControlPlaneError(code, str(exc), retryable=retryable)


def _chat_paused_on_question(chat: Any) -> bool:
    """Whether a chat's last turn ended paused on a question card.

    Only a card without a native ``request_id`` pauses by ending the stream:
    the drive loop stops the provider and waits for the answer. A native
    (opencode) form keeps an attended turn open until it is answered, and the
    card stays saved until the reply is acknowledged, so a native card on a
    stream that has already ended is a leftover from a lost turn.
    """
    raw = getattr(chat, "pending_question", "") if chat is not None else ""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return True
    return not (isinstance(payload, dict) and payload.get("request_id"))


def _chat_has_native_question(chat: Any) -> bool:
    """Whether a chat has a native (opencode, ``request_id``) question card saved.

    Such a card owns the chat until its reply is acknowledged:
    ``start_stream`` refuses a new turn while it is saved, so a task cannot
    be reviewed or sent back past it, whatever the turn's result said.
    """
    raw = getattr(chat, "pending_question", "") if chat is not None else ""
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("request_id"))


def _task_row_with_attempt(
    document: TaskDocument,
    live: TaskAttempt | None,
    current: TaskAttempt | None,
) -> dict[str, Any]:
    """One task as a board row, plus the three delegation facts it draws.

    A free function taking the two attempt lookups separately, because the board
    reads them once for a whole workspace and the single-task path reads them for
    one row — the derivation must be one thing, or a card's badge would depend on
    which read produced it.

    ``attempt_state`` is the badge, and it reports the current attempt rather
    than the live one: ``failed``, ``stopped`` and ``interrupted`` are settled
    states the user has to see, and a board showing only live attempts would go
    blank the moment a turn ended. ``live_attempt_id`` is non-empty only while an
    attempt actually holds the task, which is what tells the board whether stop and
    detach are available. ``changed_since_delegated`` compares the revision the
    attempt was handed against the record now — the attempt was rebound to the
    revision the hand-off left behind, so only a *later* edit trips this.

    An unlinked task never reports it, and that guard is not cosmetic: a
    ``detach`` clears the linkage and *that* moves the revision, so comparing
    unconditionally would make the gesture the user just performed read as "the
    result was reached against an older description" on the very card that has no
    longer delegated anything.
    """
    payload = _task_payload(document)
    linked = document.record.attempt_id is not None
    payload["attempt_state"] = current.state if current is not None else ""
    # The agent's own word on the current attempt (#1064), and the engine's note
    # when it gave none: what the card says under its badge.
    payload["attempt_outcome"] = current.outcome if current is not None else ""
    payload["attempt_summary"] = current.summary if current is not None else ""
    payload["attempt_detail"] = current.detail if current is not None else ""
    payload["live_attempt_id"] = live.attempt_id if live is not None else ""
    payload["changed_since_delegated"] = bool(
        linked and current is not None and current.task_revision != document.revision
    )
    return payload


def _attempt_payload(attempt: TaskAttempt, task: dict[str, Any] | None = None) -> dict[str, Any]:
    """One attempt as a transport payload, with the task row it belongs to.

    The task is included rather than looked up again by the caller: the board
    draws a badge and a link from one answer, and a second read could be of a
    task that moved on between the two.
    """
    payload: dict[str, Any] = {"attempt": attempt.to_dict(), "chat_id": attempt.chat_id}
    if task is not None:
        payload["task"] = task
    return payload


def _utc_now() -> datetime:
    """The store's clock: an aware UTC moment, never a naive one."""
    return datetime.now(UTC)


# ---- Webhook triggers (#1039, child A6 of #974) ---------------------------
#
# ``ciao/webhooks.py`` owns the trigger store: a typed error with a ``code``, a
# one-time secret returned only by ``create``/``rotate_secret``, and an
# optimistic ``expected_revision`` on every edit. This block is the application
# layer over it — workspace resolution and the transport-visible error codes.


#: One store refusal becomes one application error code, as ``_TASK_ERROR_CODES``
#: does for the task store. A store code with no row here surfaces as
#: ``webhook_store_unreadable`` rather than a silent default, because every code
#: in this table is one the agent CLI's ``error.code`` and a session route's
#: status mapping can already act on.
_WEBHOOK_ERROR_CODES: dict[str, tuple[str, bool]] = {
    # WebhookStoreError.code -> (ControlPlaneError code, retryable)
    "not_found": ("webhook_not_found", False),
    # Stale means re-read and re-plan, never resend the same revision: retryable
    # in the sense that reading again makes the edit succeed.
    "revision_conflict": ("webhook_revision_conflict", True),
    "invalid_trigger": ("webhook_invalid", False),
    "unsafe_path": ("webhook_invalid", False),
    "unsupported_schema": ("webhook_unsupported_schema", False),
    "corrupt_store": ("webhook_store_unreadable", False),
}


def _webhook_error(exc: WebhookStoreError) -> ControlPlaneError:
    """Re-raise one store refusal as the control plane's typed error."""
    code, retryable = _WEBHOOK_ERROR_CODES.get(
        exc.code, ("webhook_store_unreadable", False)
    )
    return ControlPlaneError(code, str(exc), retryable=retryable)


def _webhook_revision(value: Any) -> int:
    """The revision the caller read, as the integer the store checks against.

    The CLI and the MCP schema both carry a revision as a string (a task's
    revision is a file digest, so its surface never had an integer), and the
    store refuses anything that is not a real ``int`` of at least 1. Parsing it
    here means a non-numeric revision answers ``webhook_invalid`` — the same
    answer an out-of-range one gets — instead of leaking a ``ValueError`` into
    the generic ``invalid_request`` envelope.
    """
    if isinstance(value, bool):
        raise ControlPlaneError(
            "webhook_invalid", "expected_revision must be an integer of at least 1."
        )
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ControlPlaneError(
            "webhook_invalid",
            "expected_revision must be an integer of at least 1.",
        ) from exc


def _task_payload(document: TaskDocument, *, include_body: bool = False) -> dict[str, Any]:
    """One task as a transport payload.

    ``revision`` is the SHA-256 of the file's exact bytes: every write has to
    present it back, which is what makes a stale edit a refusal instead of a
    lost update. The body is omitted from a list row (it is Markdown prose, and
    a board renders many cards) and present on a get, where the caller asked
    for this one task.
    """
    record = document.record
    payload: dict[str, Any] = {
        "id": record.id,
        "title": record.title,
        "status": record.status,
        "project_id": record.project_id,
        "due": record.due,
        "assignee": record.assignee,
        "chat_id": record.chat_id,
        "attempt_id": record.attempt_id,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "revision": document.revision,
        "relative_path": document.relative_path,
    }
    if include_body:
        payload["body"] = document.body
    return payload


# ---- Note verification (#726-D) --------------------------------------------
#
# The `verify_note` operation is the one managed path from a stale note to a
# verdict, so its boundaries are stated once here rather than re-derived at each
# call site.


MAX_VERIFY_PAYLOAD_BYTES = 512 * 1024
"""Largest payload document one verification will read.

A bound on work one agent call can ask of the server, not a statement about how
big a note may be: the payload carries a verdict's text and its citations, and
an agent that has none should be told to cite a source rather than allowed to
paste a vault into an argument.
"""

MAX_VERIFY_NOTE_BYTES = 512 * 1024
"""Largest note body one verification will judge.

The same ceiling as the payload, and for the same reason: a note nobody could
read in full has not been verified by anyone, so a verdict about it is not a
verdict. A note over it is reported ``unverified`` — the honest ``unknown`` —
rather than applied, which is the direction the parent's contract requires and
the one that leaves the note itself untouched.
"""


def _payload_text(raw: Any, field: str) -> str:
    """One payload field as text, or ``""`` when it is absent or not a string.

    Total on purpose: the fields a verdict does not need (``before``/``after``
    for a re-stamp, say) are legitimately absent, and a payload that omits one
    is a request whose outcome the service already knows how to refuse. What is
    **not** tolerated is a field of the wrong *type* carrying prose: that reads
    as an empty field here and would be a silent substitution at the write, so
    a non-string where text belongs is refused by the callers below rather than
    coerced.
    """
    if field not in raw:
        return ""
    value = raw[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ControlPlaneError(
            "payload_invalid", f"'{field}' must be a string, not {type(value).__name__}."
        )
    return value


def _verification_request(raw: dict[str, Any], *, nv: Any, workspace: str) -> Any:
    """One payload document as a :class:`ciao.note_verification.VerificationRequest`.

    The request's own fields are the contract; this maps the wire spelling onto
    them and refuses the three things a payload could ask for that the service
    has no business being asked: another workspace, a verdict outside the
    service's four outcomes, and a coverage value it does not have. The
    remaining judgement — whether complete coverage plus a citation naming the
    note may re-stamp it, whether every evidence row carries a source — belongs
    to :func:`ciao.note_verification.plan_note_verification` and is not
    duplicated here.
    """
    _refuse_foreign_workspace(raw, workspace=workspace)
    outcome = _payload_text(raw, "outcome").strip()
    if not outcome:
        raise ControlPlaneError(
            "payload_invalid",
            "'outcome' is required: one of "
            f"{', '.join(nv.OUTCOMES)}.",
        )
    if outcome not in nv.OUTCOMES:
        # Refused before the service rather than by it, so the caller is told
        # the payload is wrong instead of receiving a verdict-shaped `failed`
        # it would reasonably record as "this note could not be checked".
        raise ControlPlaneError(
            "payload_invalid",
            f"unknown verification outcome {outcome!r}; expected one of "
            f"{', '.join(nv.OUTCOMES)}.",
        )
    coverage = _payload_text(raw, "coverage").strip() or nv.COVERAGE_PARTIAL
    if coverage not in (nv.COVERAGE_COMPLETE, nv.COVERAGE_PARTIAL):
        raise ControlPlaneError(
            "payload_invalid",
            f"unknown coverage {coverage!r}; expected "
            f"{nv.COVERAGE_COMPLETE!r} or {nv.COVERAGE_PARTIAL!r}.",
        )
    before = _payload_text(raw, "before")
    after = _payload_text(raw, "after")
    evidence = nv.Evidence.from_mappings(raw.get("evidence"))
    edit = None
    if before or after:
        # Only a payload that actually carries a replacement is an edit. A
        # `still_valid` re-stamp derives its text from the note itself, and
        # passing an empty `NoteEdit` there would be a request to replace the
        # note with nothing, which is a deletion the service refuses — for a
        # verdict that never asked for one.
        edit = nv.NoteEdit(before=before, after=after)
    return nv.VerificationRequest(
        workspace=workspace,
        relative_path=_payload_text(raw, "relative_path").strip(),
        expected_revision=_payload_text(raw, "expected_revision").strip(),
        outcome=outcome,
        edit=edit,
        evidence=evidence,
        coverage=coverage,
        reason=_payload_text(raw, "reason").strip(),
    )


def _refuse_foreign_workspace(raw: dict[str, Any], *, workspace: str) -> None:
    """Refuse a payload naming a workspace other than the caller's own.

    The same refusal shape `schedule` uses for a cross-workspace `workspace`
    argument, and the reason it holds for both request shapes: the check state and
    the note-edit sidecar are filed per workspace, so pairing one workspace's name
    with another workspace's vault records a verdict in a vault nobody claimed and
    pins the wrong note's cooldown.
    """
    named = _payload_text(raw, "workspace")
    if named and named != workspace:
        raise ControlPlaneError(
            "workspace_forbidden",
            f"This provider process is scoped to workspace '{workspace}'; a "
            "verification payload may not name another.",
        )


def _entry_verification_request(
    raw: dict[str, Any], *, ev: Any, workspace: str
) -> Any:
    """One payload document as an :class:`ciao.entry_verification.EntryVerificationRequest`,
    or ``None`` when it is about a whole note.

    The same payload file, and the same four outcomes, with one extra field: an
    ``entry`` identity. Which request a payload is depends on that one field and
    nothing else, so a caller cannot end up with a whole-note verdict filed under
    an entry's identity or the other way round — and the entry's fingerprint is
    required alongside it, because an entry selector that cannot say WHICH version
    of the fact was read is a selector that could name any version.

    ``before``/``after`` change meaning with the selector, deliberately and in the
    direction of the smaller unit: for a whole-note request they are the note's
    full text, and for an entry request they are the **entry's** text, because a
    whole-note replacement in an entry payload would be the caller rewriting every
    other fact to correct one.
    """
    identity = _payload_text(raw, "entry").strip()
    if not identity:
        return None
    _refuse_foreign_workspace(raw, workspace=workspace)
    fingerprint = _payload_text(raw, "entry_fingerprint").strip()
    if not fingerprint:
        raise ControlPlaneError(
            "payload_invalid",
            "'entry' needs 'entry_fingerprint': an entry selector that cannot say "
            "which version of the fact was read is a selector that could name any "
            "version.",
        )
    return _entry_request(
        raw, ev=ev, workspace=workspace, identity=identity, fingerprint=fingerprint
    )


def _entry_request(
    raw: dict[str, Any], *, ev: Any, workspace: str, identity: str, fingerprint: str
) -> Any:
    """The entry-level request body, once the selector has been validated."""
    outcome = _payload_text(raw, "outcome").strip()
    if not outcome:
        raise ControlPlaneError(
            "payload_invalid",
            "'outcome' is required: one of " + ", ".join(ev.OUTCOMES) + ".",
        )
    if outcome not in ev.OUTCOMES:
        raise ControlPlaneError(
            "payload_invalid",
            f"unknown verification outcome {outcome!r}; expected one of "
            f"{', '.join(ev.OUTCOMES)}.",
        )
    coverage = _payload_text(raw, "coverage").strip() or ev.COVERAGE_PARTIAL
    if coverage not in (ev.COVERAGE_COMPLETE, ev.COVERAGE_PARTIAL):
        raise ControlPlaneError(
            "payload_invalid",
            f"unknown coverage {coverage!r}; expected "
            f"{ev.COVERAGE_COMPLETE!r} or {ev.COVERAGE_PARTIAL!r}.",
        )
    before = _payload_text(raw, "before")
    after = _payload_text(raw, "after")
    edit = ev.EntryEdit(before=before, after=after) if before or after else None
    return ev.EntryVerificationRequest(
        workspace=workspace,
        relative_path=_payload_text(raw, "relative_path").strip(),
        identity=identity,
        entry_fingerprint=fingerprint,
        expected_revision=_payload_text(raw, "expected_revision").strip(),
        outcome=outcome,
        edit=edit,
        evidence=ev.Evidence.from_mappings(raw.get("evidence")),
        coverage=coverage,
        reason=_payload_text(raw, "reason").strip(),
    )


def _entry_digest(request: Any) -> str:
    """A short, stable id for the question an entry payload asks.

    The same reasoning as :func:`_verification_digest`, and for the same reason:
    the off-loop read coalesces by key, so the key has to carry the whole verdict
    and not only the entry it is about. Over the fields that decide the answer —
    outcome, coverage, evidence and the edit's two images — and not over the
    request's identity, which the key already spells out.
    """
    edit = request.edit
    body = {
        "outcome": str(request.outcome or ""),
        "coverage": str(request.coverage or ""),
        "evidence": [row.as_dict() for row in request.evidence],
        "before": "" if edit is None else edit.before,
        "after": "" if edit is None else edit.after,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _verification_digest(request: Any) -> str:
    """A short, stable id for *the question this payload asks*.

    The off-loop read is coalesced by key, so the key has to carry the whole
    verdict and not only the note it is about. Keyed on the note and the expected
    revision alone, two calls about the same text are the same key however
    differently they are answered: a second caller's `retire` joins an in-flight
    `still_valid`, is handed the first caller's reply, and its own outcome,
    evidence and before/after are never evaluated by anything. That is a wrong
    verdict reported as a correct one, which is worse than the duplicate write
    coalescing exists to avoid.

    Over the fields that decide the answer — outcome, coverage, evidence and the
    edit's two images — and not over the request's identity (the note, the
    revision, the workspace), which the key already spells out. Sorted keys, so
    two payloads that differ only in field order are the same question.
    """
    edit = request.edit
    body = {
        "outcome": str(request.outcome or ""),
        "coverage": str(request.coverage or ""),
        "evidence": [row.as_dict() for row in request.evidence],
        "before": "" if edit is None else edit.before,
        "after": "" if edit is None else edit.after,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]


def _oversized_note(vault_root: Path, request: Any, *, nr: Any, mr: Any) -> dict[str, Any] | None:
    """The ``unverified`` report for a note too large to judge, else ``None``.

    A size, not a read: the point is to refuse *before* pulling a multi-megabyte
    body into a worker, and ``stat`` is the only way to know. The path is
    resolved through ``note_receipts`` first so the size checked is a file
    inside this vault, not whatever a ``..`` in the payload would have named.
    """
    from ciao import note_verification as nv

    try:
        target = nr.resolve_note_path(vault_root, request.relative_path)
    except mr.MemoryReceiptError:
        # `verify_note` reports an unresolvable path precisely; leave that
        # verdict to it rather than answering the same question worse here.
        return None
    try:
        size = target.stat().st_size
    except OSError as exc:
        return _verification_reply(nv.FAILED, f"the note could not be measured: {exc}")
    if size <= MAX_VERIFY_NOTE_BYTES:
        return None
    return _verification_reply(
        nv.UNVERIFIED,
        f"the note is {size} bytes, over the {MAX_VERIFY_NOTE_BYTES}-byte cap "
        "for one verification, so this pass cannot say whether it is still true; "
        "nothing was written and no check was recorded",
    )


def _file_review_proposal(
    result: Any,
    *,
    request: Any,
    config: Any,
    workspace: str,
    today: date,
    nv: Any,
    nep: Any,
    mr: Any,
) -> tuple[dict[str, Any] | None, str]:
    """File the one ``note_edit`` proposal a ``needs_review`` verdict becomes.

    This is the wiring #726-C was written for and could not have: the kind was
    complete and nothing in production produced one, so a refused verdict
    recorded a check and asked nobody. The operation carried on the record is
    the *plan's*, not the caller's request — a payload asking to retire a note
    reaches ``needs_review`` under the outcome the rule actually reached, and
    filing that as anything else would queue an operation nobody decided on.

    The before/after images are the caller's own exact text, which is what a
    reviewer is entitled to see; a re-stamp's date is today's, fixed here rather
    than read off the clock at accept time, so a card previewed before midnight
    and clicked after it applies the same bytes it advertised.

    Nothing is applied. A retirement is a human click all the way down, and this
    module cannot reach a delete primitive any more than the service can.

    Returns the filed record and the reason it could not be filed (empty on
    success). The verdict stands and its check is recorded either way, so a
    failure has to reach the caller: the note was judged, and nobody was asked.
    """
    operation = {
        nv.RETIRE: nep.RETIRE,
        nv.STILL_VALID: nep.RESTAMP,
        nv.UPDATE: nep.REPLACE,
    }.get(str(request.outcome or "").strip(), "")
    if not operation:
        # Unreachable through `verify_note` (a `needs_review` is one of those
        # three outcomes) and refused rather than filed as something plausible.
        return None, (
            f"a needs_review verdict with outcome {request.outcome!r} has no "
            "note-edit operation to file; nothing was queued"
        )
    before = request.edit.before if request.edit is not None else ""
    after = request.edit.after if request.edit is not None else ""
    try:
        proposal = nep.file_note_edit(
            config,
            workspace=workspace,
            relative_path=request.relative_path,
            expected_revision=request.expected_revision,
            operation=operation,
            before=before,
            after=after,
            outcome=str(request.outcome).strip(),
            coverage=request.coverage,
            evidence=request.evidence,
            # The rule's own reason when it gave one: it says which gap sent
            # this to a person, and the reviewer's card carries it verbatim.
            reason=result.message or request.reason,
            today=today,
        )
    except (
        nep.NoteEditError,
        nv.NoteCheckRefused,
        mr.MemoryReceiptError,
        # A journal it would not write, a lock this thread could not take, and a
        # filesystem that said no. `QueueLockError` is not a
        # `MemoryReceiptError` and an `OSError` is neither, so both escaped the
        # caller's report and reached the operation as an `internal_error` —
        # after the check and its cooldown were already recorded. The note was
        # judged and nobody was asked, and the agent was told the call failed
        # rather than told a verdict exists with a filing error against it.
        mr.QueueLockError,
        OSError,
    ) as exc:
        return None, (
            f"the note-edit proposal could not be filed: {exc}. The verdict is "
            f"recorded, but nobody has been asked about {request.relative_path}."
        )
    return {
        "id": proposal.id,
        "relative_path": proposal.relative_path,
        "operation": proposal.operation,
        "proposal_id": proposal.proposal_id,
        # A sidecar with no queue row is litter the next pass overwrites: the
        # proposal is on file but nothing is asking the owner, and reporting
        # that as a working row would be the one wrong answer here.
        "queued": bool(proposal.proposal_id),
    }, ""


def _entry_verification_reply(
    status: str,
    message: str,
    *,
    receipt_id: str = "",
    check: Any = None,
    proposal: dict[str, Any] | None = None,
    proposal_error: str = "",
) -> dict[str, Any]:
    """One entry verification's whole report, in the shape every exit shares.

    The same flat shape as :func:`_verification_reply` and the same key set, with
    the check's own subject fields swapped for an entry's — an agent reading either
    reply must not have to learn two shapes, and a missing `proposal` must still
    mean "nothing was filed" rather than "the reply was cut off".
    """
    from ciao import entry_verification as ev

    return {
        "status": status,
        "receipt_id": receipt_id,
        "message": message,
        "scope": "entry",
        "check": (
            None
            if check is None
            else {
                "identity": check.identity,
                "note_path": check.note_path,
                "content_fingerprint": check.content_fingerprint,
                "outcome": check.outcome,
                "checked_at": check.checked_at.isoformat(),
                "retry_after": check.retry_after.isoformat(),
                "coverage": check.coverage,
                "reason": check.reason,
                "proposal_id": check.proposal_id,
                "evidence": [row.as_dict() for row in check.evidence],
            }
        ),
        "proposal": proposal,
        "proposal_error": proposal_error,
        "auto_applied": status == ev.APPLIED,
    }


def _file_entry_review_proposal(
    result: Any,
    *,
    request: Any,
    note_text: str,
    config: Any,
    workspace: str,
    today: date,
    ev: Any,
    nep: Any,
    mr: Any,
) -> tuple[dict[str, Any] | None, str]:
    """File the one entry ``note_edit`` proposal an entry verdict becomes.

    The entry-level twin of :func:`_file_review_proposal`, and the same three
    things it gets right: the operation comes from the outcome the *rule* reached
    rather than the one the caller asked for, nothing is applied, and a filing
    that fails is reported rather than swallowed — the fact was judged and nobody
    was asked.

    What it composes is the ``after`` image, and it composes it through
    :func:`ciao.note_receipts.compose_entry_edit` over the note's own bytes: the
    entry's span, the caller's replacement for an update, this verification's date
    for a re-stamp, and nothing at all for a retirement. ``file_note_edit`` then
    measures the span again from the note and refuses if the entry is not the one
    the verdict was about, so a stale payload cannot file a splice at offsets that
    now mean other words.
    """
    from ciao import note_receipts as nr

    operation = {
        ev.RETIRE: nep.RETIRE_ENTRY,
        ev.STILL_VALID: nep.RESTAMP_ENTRY,
        ev.UPDATE: nep.REPLACE_ENTRY,
    }.get(str(request.outcome or "").strip(), "")
    if not operation:
        return None, (
            f"a needs_review verdict with outcome {request.outcome!r} has no "
            "entry-edit operation to file; nothing was queued"
        )
    entry = nr.find_entry(
        note_text,
        identity=request.identity,
        note_path=request.relative_path,
        workspace=workspace,
    )
    if entry is None:
        return None, (
            f"{request.relative_path} holds no entry with identity "
            f"{str(request.identity)[:12]}, so no entry edit could be composed for "
            f"it; nothing was queued about {request.relative_path}"
        )
    if entry.fingerprint != str(request.entry_fingerprint or "").strip():
        return None, (
            f"the entry {str(request.identity)[:12]} in {request.relative_path} is "
            "not the text this verdict was reached about, so no entry edit was "
            f"composed; nothing was queued about {request.relative_path}"
        )
    if operation == nep.RESTAMP_ENTRY:
        replacement: str | None = ev.stamp_entry(entry.text, today.isoformat())
    elif operation == nep.REPLACE_ENTRY:
        replacement = request.edit.after if request.edit is not None else None
    else:
        replacement = None
    after, refusal = nr.compose_entry_edit(
        note_text, entry, replacement=replacement, delete=operation == nep.RETIRE_ENTRY
    )
    if refusal:
        return None, (
            f"the entry edit could not be composed ({refusal}); nothing was queued "
            f"about {request.relative_path}"
        )
    try:
        proposal = nep.file_note_edit(
            config,
            workspace=workspace,
            relative_path=request.relative_path,
            expected_revision=request.expected_revision,
            operation=operation,
            before=note_text,
            after=after,
            outcome=str(request.outcome).strip(),
            coverage=request.coverage,
            evidence=request.evidence,
            reason=result.message or request.reason,
            today=today,
            entry_identity=entry.identity,
            entry_fingerprint=entry.fingerprint,
        )
    except (
        nep.NoteEditError,
        ev.EntryCheckRefused,
        mr.MemoryReceiptError,
        mr.QueueLockError,
        OSError,
    ) as exc:
        return None, (
            f"the entry-edit proposal could not be filed: {exc}. The verdict is "
            f"recorded, but nobody has been asked about entry "
            f"{str(request.identity)[:12]} of {request.relative_path}."
        )
    return {
        "id": proposal.id,
        "relative_path": proposal.relative_path,
        "operation": proposal.operation,
        "proposal_id": proposal.proposal_id,
        "entry_identity": proposal.entry_identity,
        "entry_span": list(proposal.entry_span),
        "queued": bool(proposal.proposal_id),
    }, ""


def _verification_reply(
    status: str,
    message: str,
    *,
    receipt_id: str = "",
    check: Any = None,
    proposal: dict[str, Any] | None = None,
    proposal_error: str = "",
) -> dict[str, Any]:
    """One verification's whole report, in the shape every exit shares.

    Flat on purpose: the caller is an agent deciding what to do next, and the
    distinction it must not get wrong is ``applied`` (the note was written) from
    ``needs_review`` (a human now has a row) from ``unverified`` (nobody could
    tell). Every key is always present, because an agent reading a missing
    ``proposal`` cannot tell "nothing was filed" from "the reply was cut off".
    The check's evidence travels too: the run's log needs the citations, and the
    check state is where they survive the turn.
    """
    from ciao import note_verification as nv

    return {
        "status": status,
        "receipt_id": receipt_id,
        "message": message,
        "check": (
            None
            if check is None
            else {
                "relative_path": check.relative_path,
                "content_revision": check.content_revision,
                "outcome": check.outcome,
                "checked_at": check.checked_at.isoformat(),
                "retry_after": check.retry_after.isoformat(),
                "coverage": check.coverage,
                "reason": check.reason,
                "proposal_id": check.proposal_id,
                "evidence": [row.as_dict() for row in check.evidence],
            }
        ),
        "proposal": proposal,
        "proposal_error": proposal_error,
        "auto_applied": status == nv.APPLIED,
    }


class CiaoControlPlane:
    """Application operations shared by agent-facing transports."""

    def __init__(
        self,
        config: Any,
        *,
        project_chat_manager: Any,
        schedule_manager: Any,
        app_settings: Any | None = None,
        startup_tracker: Any | None = None,
        connection_tracker: Any | None = None,
        background_runner: Any | None = None,
    ) -> None:
        self.config = config
        self.pcm = project_chat_manager
        self.schedules = schedule_manager
        self.app_settings = app_settings
        self.startup_tracker = startup_tracker
        self._deferred_actions: dict[str, dict[str, Any]] = {}
        # Optional: the app-wide ConnectionTracker, used by file_surface to
        # report real connected clients instead of a per-turn stream proxy.
        self.connection_tracker = connection_tracker
        # Optional: the BackgroundRunner backing the background_run_* tools.
        # Unset on legacy-only instances and in most tests.
        self.background = background_runner
        #: The turn watchers this process is waiting on, held strongly.
        #: See :meth:`_schedule_watch` — the loop keeps only a weak reference, and
        #: a collected watcher would never settle its attempt.
        self._watchers: set[asyncio.Task[None]] = set()
        #: Current stream and its task revision per workspace/attempt. A new
        #: stream supersedes the old watcher even before its callback has run.
        #: See :meth:`_watch_turn` — a turn in a delegated chat is announced once
        #: per turn, and the paths that start one may announce it too.
        self._watching: dict[tuple[str, str], tuple[Any, str]] = {}
        #: The loop ``tasks_changed`` is published on. A task write made in a
        #: worker thread (the routes run store calls via ``asyncio.to_thread``)
        #: is marshalled back here, because ``EventsHub``'s queues want the loop.
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        #: Workspaces with a ``tasks_changed`` already scheduled, so the several
        #: writes one gesture makes (a delegation writes the attempt, links the
        #: task and binds the revision) reach clients as one event.
        self._tasks_changed_pending: set[str] = set()
        self._tasks_changed_guard = threading.Lock()
        self._subscribe_turn_watch()
        self._subscribe_chat_ended()

    def _defer_until_chat_idle(
        self,
        principal: AgentPrincipal,
        action: str,
        operation: Callable[[], Any],
    ) -> dict[str, Any]:
        """Return before applying a mutation that would tear down its own tool caller."""
        action_id = f"action-{uuid.uuid4().hex[:8]}"
        record = {
            "action_id": action_id,
            "action": action,
            "chat_id": principal.chat_id,
            "token_id": principal.token_id,
            "status": "queued",
            "requested_at": datetime.now(UTC).isoformat(),
            "completed_at": "",
            "error": "",
        }
        self._deferred_actions[action_id] = record

        async def _run() -> None:
            try:
                while principal.chat_id in self.pcm.active_chat_ids():
                    await asyncio.sleep(0.25)
                record["status"] = "running"
                value = operation()
                if hasattr(value, "__await__"):
                    value = await value
                if hasattr(value, "to_dict"):
                    value = value.to_dict()
                record["result"] = value
                record["status"] = "completed"
            except Exception as exc:  # noqa: BLE001 - deferred boundary is fail-safe
                record["status"] = "failed"
                record["error"] = str(exc)
            finally:
                record["completed_at"] = datetime.now(UTC).isoformat()

        asyncio.create_task(_run(), name=action_id)
        return _ok({
            "deferred": True,
            **{key: value for key, value in record.items() if key != "token_id"},
        })

    # ---- scope ---------------------------------------------------------

    def _workspace(self, principal: AgentPrincipal, requested: str = "") -> str:
        workspace = requested.strip() or principal.workspace
        if not workspace:
            raise ControlPlaneError("workspace_required", "No active workspace is available.")
        if workspace != principal.workspace:
            raise ControlPlaneError(
                "workspace_forbidden",
                f"This provider process is scoped to workspace '{principal.workspace}'.",
            )
        if self.config.workspace(workspace) is None:
            raise ControlPlaneError("workspace_not_found", f"Workspace '{workspace}' was not found.")
        return workspace

    def _project(self, principal: AgentPrincipal, project_id: str) -> Any:
        project = self.pcm.get_project(project_id)
        if project is None:
            raise ControlPlaneError("project_not_found", f"Project '{project_id}' was not found.")
        try:
            self._workspace(principal, project.workspace)
        except ControlPlaneError as exc:
            if exc.code == "workspace_forbidden":
                # A foreign-but-existent id must read exactly like a nonexistent
                # one: otherwise the two codes form an existence oracle over
                # project ids in other workspaces.
                raise ControlPlaneError("project_not_found", f"Project '{project_id}' was not found.") from exc
            raise
        return project

    def _resolve_project_id(self, principal: AgentPrincipal, ref: str) -> str:
        """Resolve a non-empty project id-or-case-insensitive-name to an exact id.

        Shared by any tool that accepts a project reference, so a caller never
        has to pre-resolve an id via ``projects_list`` for the common case."""
        exact = self.pcm.get_project(ref)
        if exact is not None:
            exact_id: str = exact.project_id
            return exact_id
        matches = [
            p for p in self.pcm.list_projects(principal.workspace)
            if p.name.casefold() == ref.casefold()
        ]
        if len(matches) == 1:
            match_id: str = matches[0].project_id
            return match_id
        if len(matches) > 1:
            raise ControlPlaneError(
                "project_ambiguous",
                f"'{ref}' matches more than one project; use its exact id instead.",
            )
        raise ControlPlaneError("project_not_found", f"Project '{ref}' was not found.")

    def _resolve_project(self, principal: AgentPrincipal, ref: str | None) -> Any:
        """Resolve a project by exact id, case-insensitive name, or the
        caller's current project when ``ref`` is omitted or self-referential."""
        value = (ref or "").strip()
        if not value or value.lower() in {"this", "this project", "current", "self"}:
            if not principal.project_id:
                raise ControlPlaneError(
                    "project_required",
                    "No project given and no active project to default to; pass a project id or name.",
                )
            return self._project(principal, principal.project_id)
        return self._project(principal, self._resolve_project_id(principal, value))

    def _resolve_chat_id(self, principal: AgentPrincipal, ref: str | None) -> str:
        """Resolve a chat ID, defaulting to principal.chat_id when ref is omitted,
        empty, or self-referential ('this', 'this chat', 'current', 'self')."""
        value = (ref or "").strip()
        if not value or value.lower() in {"this", "this chat", "current", "self"}:
            if not principal.chat_id:
                raise ControlPlaneError(
                    "chat_required",
                    "No chat ID given and no active chat to default to.",
                )
            return principal.chat_id
        return value

    def _workspace_chats(self, principal: AgentPrincipal) -> list[Any]:
        """Every chat whose owning project sits in the principal's workspace."""
        lister = getattr(self.pcm, "list_chats", None)
        if not callable(lister):
            return []
        rows: list[Any] = []
        for chat in lister():
            project = self.pcm.get_project(chat.project_id)
            if project is not None and project.workspace == principal.workspace:
                rows.append(chat)
        return rows

    def _chat_by_title(self, principal: AgentPrincipal, ref: str) -> Any | None:
        """Resolve an unambiguous active chat *title* inside this workspace.

        The id is what the surface documents, but an agent that has just read a
        chat list back reaches for the title it saw — the single most common
        argument mistake on the CLI surface. Resolution is deliberately narrow:
        case-insensitive, exact (no prefix or substring), archived chats
        excluded, and scoped by ``_workspace_chats`` to the principal's own
        workspace, so it can never name a chat the caller could not already
        address by id. Anything else returns ``None`` and the caller raises the
        same ``chat_not_found`` it raised before.
        """
        wanted = ref.strip().casefold()
        if not wanted:
            return None
        matches = [
            chat
            for chat in self._workspace_chats(principal)
            if not getattr(chat, "archived", False)
            and str(getattr(chat, "title", "") or "").strip().casefold() == wanted
        ]
        return matches[0] if len(matches) == 1 else None

    def _resolve_project_in_workspace(
        self, principal: AgentPrincipal, ref: str, workspace: str
    ) -> Any:
        """Resolve a project reference against one explicit workspace.

        Like ``_resolve_project`` but both the name lookup and the ownership
        check target ``workspace`` instead of the caller's own (used by
        ``schedule_update`` to resolve a project inside its settled
        destination workspace).
        """
        exact = self.pcm.get_project(ref)
        if exact is not None:
            if exact.workspace != workspace:
                raise ControlPlaneError(
                    "workspace_mismatch",
                    f"Project '{ref}' lives in workspace '{exact.workspace}', not '{workspace}'.",
                )
            return exact
        matches = [
            p for p in self.pcm.list_projects(workspace)
            if p.name.casefold() == ref.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ControlPlaneError(
                "project_ambiguous",
                f"'{ref}' matches more than one project; use its exact id instead.",
            )
        raise ControlPlaneError("project_not_found", f"Project '{ref}' was not found.")

    def _chat_scope(self, principal: AgentPrincipal, chat_id: str | None = None) -> tuple[Any, Any]:
        """Resolve a chat plus the project that owns it, in one authorization pass.

        Callers that need the workspace should take the project from here rather
        than looking it up again: ``_chat`` already resolves and authorizes it.
        """
        resolved_id = self._resolve_chat_id(principal, chat_id)
        chat = self.pcm.get_chat(resolved_id)
        if chat is None:
            # Not an id: try an unambiguous active title in this workspace.
            # Ambiguous or unknown stays ``chat_not_found`` so the error code
            # the telemetry and the callers already know does not fork.
            chat = self._chat_by_title(principal, resolved_id)
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{resolved_id}' was not found.")
        return chat, self._project(principal, chat.project_id)

    def _chat(self, principal: AgentPrincipal, chat_id: str | None = None) -> Any:
        return self._chat_scope(principal, chat_id)[0]

    def _chat_id(self, principal: AgentPrincipal, chat_id: str | None = None) -> str:
        """Authorize a chat reference and return its real id.

        ``_chat`` resolves ``""``/``"this"``/``"self"`` internally but returns
        only the chat, so callers that echo the id back had to remember a second
        line to recover it. Use this when the chat object itself is not needed.
        """
        return str(self._chat(principal, chat_id).chat_id)

    def chat_mode(self, principal: AgentPrincipal) -> str:
        chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        return str(getattr(chat, "mode", "auto") or "auto")

    def _child_mode(self, principal: AgentPrincipal, requested: str | None) -> str:
        """Hold an MCP-created child at or below its caller's permission ceiling.

        The child starts its first turn immediately, so accepting a *stronger*
        mode from model-authored tool arguments would let a normal/auto chat
        manufacture a bypass session without an operator approval. The provider
        enforces the returned mode as its session permission rules; keeping the
        clamp here also covers provider child chats.

        A ceiling, not a pin. This used to return ``parent_mode`` outright and
        ignore ``requested``, which blocked *de-escalation* as well as
        escalation: from a ``bypass`` chat, ``chat_update(chat_id=<other>,
        mode="normal")`` raised the target to ``bypass`` instead of clamping it,
        and in an ``auto`` chat a ``mode="plan"`` downgrade was written back as
        ``auto`` while the response still reported success — a requested
        restriction silently discarded. A weaker request is always honoured;
        only an upward one is clamped.
        """
        parent_mode = self.chat_mode(principal)
        if parent_mode not in _MODE_RANK:
            parent_mode = "normal"
        # An unrecognised request carries no rank to compare, so it cannot be
        # honoured safely; fall back to the ceiling rather than guessing.
        if not requested or requested not in _MODE_RANK:
            return parent_mode
        if _MODE_RANK[requested] <= _MODE_RANK[parent_mode]:
            return requested
        logger.warning(
            "Clamping child chat mode %r to ceiling %r for %s",
            requested,
            parent_mode,
            principal.chat_id or "unscoped MCP session",
        )
        return parent_mode

    def _vault_root(self, principal: AgentPrincipal) -> Path:
        # The task service resolves a workspace name to its root through
        # `workspace_vault_root` too, so there is one resolver behind both
        # callers and no second spelling of the fallback to drift from it.
        return self.workspace_vault_root(self._workspace(principal))

    def _unattended_turn(self, principal: AgentPrincipal) -> bool:
        """Whether the turn asking right now was fired by a schedule, not a person.

        The chat records the flag per turn, and only an unattended turn ever gets
        a key written — so this reads the CURRENT turn, not the most recent
        unattended one. ``max(...)`` answered "has this chat ever run
        unattended", and one scheduled turn then refused every later attended
        action in that chat, permanently.

        A chat this process cannot see is attended: an unattributable turn is a
        turn nobody fired automatically, and a caller's provenance label must not
        claim a schedule wrote something a person asked for.
        """
        chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if chat is None:
            return False
        current_turn = str(max(0, int(chat.user_turn_count) - 1))
        return bool(chat.user_turn_unattended.get(current_turn))

    @staticmethod
    def _safe_relative(root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or "\x00" in relative_path:
            raise ControlPlaneError("invalid_path", "Use a relative path inside the active root.")
        target = (root / raw).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ControlPlaneError("path_forbidden", "The path resolves outside the active root.")
        if must_exist and not target.exists():
            raise ControlPlaneError("file_not_found", f"'{relative_path}' was not found.")
        return target

    # ---- context/status -----------------------------------------------

    def context_get(self, principal: AgentPrincipal) -> dict[str, Any]:
        chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        project = self.pcm.get_project(principal.project_id) if principal.project_id else None
        return _ok({
            "workspace": principal.workspace,
            "project": project.to_dict() if project else None,
            "chat": chat.to_dict(local=self.pcm.is_session_local(chat)) if chat else None,
            "provider": principal.provider,
            "role": principal.role,
        })

    def system_status_get(self, principal: AgentPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        return _ok({
            "version": __import__("ciao").__version__,
            "workspace_root": str(self.config.workspace_root),
            "vault_root": str(self._vault_root(principal)),
            "active_chat_ids": self.pcm.active_chat_ids(),
            "startup": self.startup_tracker.to_dict() if self.startup_tracker else None,
        })

    def gws_status(self, principal: AgentPrincipal) -> dict[str, Any]:
        """Report Google Workspace connection status for the active workspace.

        Resolves the workspace's linked ``gws_profile`` the same way the runtime
        does (the operator-level default only counts when it names an account
        that actually exists), then reports whether credentials are present and
        whether the periodic health monitor's last reading says the token is
        valid. Read-only and cheap: it never runs ``gws auth status`` itself, so
        the agent can answer "is Google connected?" without a subprocess.
        """
        workspace = self._workspace(principal)
        from ciao import gws_auth

        profile = str(
            getattr(self.config.workspace(workspace), "gws_profile", "") or ""
        ).strip()
        if not profile:
            from ciao.config import GWS_DEFAULT_PROFILE

            profile = (
                GWS_DEFAULT_PROFILE
                if GWS_DEFAULT_PROFILE in gws_auth.known_profiles(self.config)
                else ""
            )
        if not profile:
            return _ok(
                {
                    "profile": "",
                    "configured": False,
                    "connected": False,
                    "needs_relogin": False,
                }
            )

        config_dir = gws_auth.profile_config_dir(self.config, profile)
        credentials_present = False
        if config_dir is not None:
            credentials_present = any(
                (config_dir / name).is_file()
                for name in ("credentials.json", "credentials.enc")
            )
        health = gws_auth.read_health_cache(
            Path(self.config.state_path).parent
        ).get(profile, {})
        token_valid = (
            bool(health.get("token_valid")) if "token_valid" in health else None
        )
        # The health monitor preserves prior state when a probe is unavailable,
        # so a cached reading
        # can outlive the token it described. Only a fresh, confirmed valid
        # reading establishes a connection; a stale one is reported as unknown
        # rather than assumed good.
        checked_at = health.get("checked_at")
        fresh = (
            isinstance(checked_at, (int, float))
            and (time.time() - float(checked_at)) <= _GWS_HEALTH_STALE_AFTER
        )
        connected = bool(credentials_present and token_valid is True and fresh)
        # The health monitor debounces a single invalid reading (notify_threshold
        # consecutive invalid runs) before treating a login as dead. Mirror that:
        # only a confirmed invalid state (notified_invalid) surfaces as
        # needs_relogin, so a transient reading does not trigger a false
        # re-authentication prompt.
        needs_relogin = bool(
            credentials_present
            and token_valid is False
            and health.get("notified_invalid")
        )
        return _ok(
            {
                "profile": profile,
                "configured": credentials_present,
                "connected": connected,
                "token_valid": token_valid,
                "needs_relogin": needs_relogin,
                "token_error": str(health.get("token_error") or ""),
                "checked_at": checked_at,
                "stale": bool(credentials_present and not fresh),
            }
        )

    def memory_status(self, principal: AgentPrincipal) -> dict[str, Any]:
        """Report native guide memory usage without copying its contents."""
        workspace = self._workspace(principal)
        guide = guide_path(self.config.agent_root(workspace))
        return _ok(memory_status_payload(
            guide,
        ))

    def memory_update(
        self,
        principal: AgentPrincipal,
        region: str,
        *,
        action: Literal["add", "replace", "remove"],
        entry: str = "",
        match: str = "",
    ) -> dict[str, Any]:
        """Apply one bounded edit to the native guide's memory region."""
        from ciao.memory_tool import MemoryLockError

        workspace = self._workspace(principal)
        if action not in {"add", "replace", "remove"}:
            raise ControlPlaneError("invalid_action", "action must be add, replace, or remove.")
        canonical = resolve_region(region)
        limit = (
            DEFAULT_MEMORY_CHAR_LIMIT
            if canonical == "memory"
            else DEFAULT_USER_CHAR_LIMIT
        )
        guide = guide_path(self.config.agent_root(workspace))
        try:
            vault_root = Path(self.config.workspace_vault_root(workspace))
        except (AttributeError, ValueError):
            vault_root = None
        try:
            result = update_region(
                guide,
                canonical,
                action=action,
                entry=entry,
                match=match,
                char_limit=limit,
                actor="agent",
                source="mcp",
                workspace=workspace,
                vault_root=vault_root,
            )
        except MemoryLockError as exc:
            # Retryable, and explicitly not a success: the region is unchanged.
            raise ControlPlaneError(
                "memory_update_locked", f"memory is busy; retry: {exc}", retryable=True
            ) from exc
        except ValueError as exc:
            raise ControlPlaneError("memory_update_invalid", str(exc)) from exc
        return _ok(result)

    # ---- note verification ---------------------------------------------

    async def verify_note(self, principal: AgentPrincipal, *, payload_file: str = "") -> dict[str, Any]:
        """Record one stale note's verdict through the managed verification service.

        The attended/scoped operation #726-D exists so the nightly run stops
        hand-editing notes. Every argument arrives as a **payload file** inside
        the caller's workspace, never as argv prose: a verification carries a
        note's full before/after text and a list of citations, all of it
        arbitrary user prose that a shell would mangle (``$()``, backticks,
        quotes) or expose in the process table. The document is the same shape
        :class:`ciao.note_verification.VerificationRequest` already defines, so
        nothing here re-decides what a verdict means.

        What this method adds is scoping, and the one write #726-C was waiting
        for:

        * the payload path is resolved **inside this caller's workspace root**
          and refused if it escapes, so an agent cannot read a document belonging
          to another workspace to fabricate a verdict about this one;
        * a ``workspace`` in the payload naming anything but the caller's own is
          refused, because the check state and the note-edit sidecar are filed
          per workspace and a cross-workspace pair would record a verdict about
          the wrong vault;
        * the service's own vault, path, revision and symlink confinement
          applies unchanged (:func:`ciao.note_verification.verify_note` is
          called, not reimplemented);
        * a ``needs_review`` verdict is filed as exactly one typed ``note_edit``
          proposal, which pins the check to the queue row's id. Before this
          wiring, that verdict recorded a check and asked nobody.

        Retirement is **not** applied here and cannot be: it reaches the same
        ``needs_review`` a note with no frontmatter reaches, and its proposal is
        a human click all the way down.

        **The same call, one entry.** A payload carrying an ``entry`` identity and
        the ``entry_fingerprint`` it was read at is a request about one Markdown
        list item rather than the whole note, and it runs the same pipeline one
        level in: the same payload confinement, the same workspace scope, the same
        coalesced bounded read, the same ``source`` from the turn, and the same
        "a ``needs_review`` verdict files exactly one proposal" wiring — filing a
        ``replace_entry`` / ``restamp_entry`` / ``retire_entry`` instead of the
        whole-note kinds. The service is :func:`ciao.entry_verification.verify_entry`
        (called, not reimplemented) and the reply carries ``scope: "entry"`` so a
        caller reading both shapes of reply is never guessing which one it got.

        ``before``/``after`` change meaning with the selector, in the direction of
        the smaller unit: they are the *entry's* text for an entry request, because
        a whole-note replacement in an entry payload is the caller rewriting every
        other fact in the file to correct one.

        The verification and the filing are one bounded off-loop read
        (:func:`ciao.async_reads.run_read`), coalesced by workspace, note, expected
        revision **and the payload's own content** — two agents judging the same
        revision of the same note in the same workspace share one read rather than
        racing each other to write it, but only when they are asking the same
        question. Coalescing on the note alone let a second caller's `retire`
        join an in-flight `still_valid` and be answered with the first caller's
        verdict, so its own payload was never evaluated at all.

        ``source`` is derived from the turn, not fixed. A receipt says who
        decided, and an interactive verification journaled as the nightly job is a
        false provenance line: the unattended schedule gets ``curation``, an
        attended chat gets ``chat``.

        The caps are refusals shaped as ``unverified``, not as ``applied``: a
        payload or a note too large to be the thing the caller says it read is
        an input we cannot settle, and reporting it as a completed verification
        would pin a verdict about text nobody showed the service.
        """
        from ciao import entry_verification as ev
        from ciao import memory_receipts as mr
        from ciao import note_edit_proposals as nep
        from ciao import note_receipts as nr
        from ciao import note_verification as nv

        workspace = self._workspace(principal)
        raw_payload = self._verification_payload(principal, payload_file, workspace=workspace)
        # One payload file, two requests, and the `entry` field is the only thing
        # that says which: an identity plus a fingerprint names one fact inside one
        # note, and its absence means the note itself. Decided here rather than in
        # the service so a caller cannot get a whole-note verdict filed under an
        # entry's identity, or the other way round.
        entry_request = _entry_verification_request(raw_payload, ev=ev, workspace=workspace)
        if entry_request is not None:
            return await self._verify_one_entry(
                entry_request,
                principal=principal,
                raw_payload=raw_payload,
                workspace=workspace,
                nep=nep,
                nr=nr,
                ev=ev,
                nv=nv,
            )
        request = _verification_request(raw_payload, nv=nv, workspace=workspace)
        # The same answer `note_verification` and `note_edit_proposals` resolve
        # the vault through, so the service, the check state and the sidecar
        # cannot end up describing three different vaults. `CiaoConfig` always
        # answers it, so this is the call and nothing else.
        vault_root = Path(self.config.workspace_vault_root(workspace))
        today = date.today()
        source = "curation" if self._unattended_turn(principal) else "chat"

        def _run() -> dict[str, Any]:
            """Verify, then file the proposal a refused verdict becomes.

            Nested rather than split so the check state and the sidecar that
            pins it are written under one bounded read: a caller that filed
            outside the read could interleave a second verdict for the same
            revision between the two writes.
            """
            oversized = _oversized_note(vault_root, request, nr=nr, mr=mr)
            if oversized is not None:
                return oversized
            result = nv.verify_note(
                request,
                vault_root=vault_root,
                config=self.config,
                actor="agent",
                source=source,
                today=today,
            )
            if result.status != nv.NEEDS_REVIEW:
                return _verification_reply(
                    result.status,
                    result.message,
                    receipt_id=result.receipt_id,
                    check=result.check,
                )
            filed, filing_error = _file_review_proposal(
                result,
                request=request,
                config=self.config,
                workspace=workspace,
                today=today,
                nv=nv,
                nep=nep,
                mr=mr,
            )
            # The check the reply carries is re-read, not the row
            # `verify_note` returned: filing is what pins it, so the
            # `proposal_id` holding this note off a second proposal is written
            # after that result was built, and reporting the pre-filing row
            # would tell the agent a verdict nobody is waiting on.
            pinned = (
                nv.read_note_checks(vault_root).get(result.check.relative_path)
                if result.check is not None
                else None
            )
            return _verification_reply(
                result.status,
                result.message,
                receipt_id=result.receipt_id,
                check=result.check if pinned is None else pinned,
                proposal=filed,
                proposal_error=filing_error,
            )

        # Same key shape as `update_tasks`: the install's runtime directory is
        # the one part of an identity a workspace, a note and a revision cannot
        # supply, so two installs in one process never share a read. The payload's
        # own digest is the rest: the note and the revision say *which text* is
        # being judged, and the digest says *what is being claimed about it*, so
        # two callers only share a read when they are asking the same question.
        key = (
            f"verify-note:{self._search_runtime_dir()}:"
            f"{workspace}:{request.relative_path}:{request.expected_revision}:"
            f"{_verification_digest(request)}"
        )
        reported: dict[str, Any] = await run_read(key, _run, coalesce=True)
        return _ok(reported)

    async def _verify_one_entry(
        self,
        request: Any,
        *,
        principal: AgentPrincipal,
        raw_payload: dict[str, Any],
        workspace: str,
        nep: Any,
        nr: Any,
        ev: Any,
        nv: Any,
    ) -> dict[str, Any]:
        """Run one entry verdict through the same pipeline as one note's.

        The whole-note path above, in the order it does everything: read the note's
        exact bytes, call the entry service, and file the one proposal a refused
        verdict becomes — all three inside one bounded off-loop read, so a second
        caller cannot interleave a different verdict for the same entry between the
        check being recorded and the proposal that pins it.

        The read is coalesced on the entry's identity AND its fingerprint AND a
        digest of the whole payload, not on the note: two agents judging the same
        fact at the same revision in the same workspace share one read only when
        they are asking the same question, and the answer to "is this entry still
        what I read it as" is not the answer to "is this note still what I read it
        as".
        """
        from ciao import memory_receipts as mr

        del raw_payload  # the request already carries everything the read needs
        vault_root = Path(self.config.workspace_vault_root(workspace))
        today = date.today()
        source = "curation" if self._unattended_turn(principal) else "chat"

        def _run() -> dict[str, Any]:
            try:
                target = nr.resolve_note_path(vault_root, request.relative_path)
                note_text = target.read_bytes().decode("utf-8")
            except (mr.MemoryReceiptError, OSError, UnicodeDecodeError) as exc:
                return _entry_verification_reply(
                    ev.FAILED, f"the note could not be read: {exc}"
                )
            result = ev.verify_entry(
                request,
                vault_root=vault_root,
                config=self.config,
                actor="agent",
                source=source,
                today=today,
            )
            if result.status != ev.NEEDS_REVIEW:
                return _entry_verification_reply(
                    result.status,
                    result.message,
                    receipt_id=result.receipt_id,
                    check=result.check,
                )
            filed, filing_error = _file_entry_review_proposal(
                result,
                request=request,
                note_text=note_text,
                config=self.config,
                workspace=workspace,
                today=today,
                ev=ev,
                nep=nep,
                mr=mr,
            )
            if filed is None or not filed.get("queued"):
                # The verdict stands and its check is recorded, but nothing is
                # waiting on it: a proposal that was not filed, or filed with no
                # queue row, is a verdict nobody is ever asked about. A pending
                # check's whole suppression is that pending proposal, so release
                # the cooldown instead of leaving the entry settled against a
                # question that does not exist — otherwise the fact is not planned
                # again for a month and nothing re-files it.
                ev.release_entry_check(vault_root, request.identity, today=today)
            # The check the reply carries is re-read, not the row `verify_entry`
            # returned: filing is what pins it, and reporting the pre-filing row
            # would tell the agent a verdict nobody is waiting on.
            pinned = ev.read_entry_checks(vault_root).get(request.identity)
            return _entry_verification_reply(
                result.status,
                result.message,
                receipt_id=result.receipt_id,
                check=result.check if pinned is None else pinned,
                proposal=filed,
                proposal_error=filing_error,
            )

        key = (
            f"verify-entry:{self._search_runtime_dir()}:{workspace}:"
            f"{request.relative_path}:{request.identity}:"
            f"{request.entry_fingerprint}:{request.expected_revision}:"
            f"{_entry_digest(request)}"
        )
        reported: dict[str, Any] = await run_read(key, _run, coalesce=True)
        return _ok(reported)

    def _verification_payload(
        self, principal: AgentPrincipal, payload_file: str, *, workspace: str
    ) -> dict[str, Any]:
        """The one JSON document this call carries its verdict in, or a refusal.

        Confined to the caller's own workspace root, the same root
        :meth:`file_surface` validates against, and capped before the bytes are
        read. The cap is a bound on work an agent can ask the server to do with
        one call, not a bound on note size: it is here because the payload
        arrives as an arbitrary document and nothing else bounds it.
        """
        if not payload_file.strip():
            raise ControlPlaneError(
                "payload_required",
                "A verification needs a payload file: --payload-file <file>. "
                "The verdict's text and citations never travel as arguments.",
            )
        root = Path(self.config.agent_root(workspace)).resolve()
        target = self._safe_relative(root, payload_file, must_exist=True)
        if not target.is_file():
            raise ControlPlaneError("payload_invalid", "The payload must be a file.")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise ControlPlaneError(
                "payload_unreadable", f"The payload file could not be read: {exc}"
            ) from exc
        if size > MAX_VERIFY_PAYLOAD_BYTES:
            raise ControlPlaneError(
                "payload_too_large",
                f"The payload is {size} bytes, over the {MAX_VERIFY_PAYLOAD_BYTES}-byte "
                "cap for one verification. Put the evidence in the vault and cite it.",
            )
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            raise ControlPlaneError(
                "payload_unreadable", f"The payload file is not readable UTF-8: {exc}"
            ) from exc
        except ValueError as exc:
            raise ControlPlaneError(
                "payload_invalid", f"The payload is not valid JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ControlPlaneError(
                "payload_invalid", "The payload must be a JSON object."
            )
        return raw

    # ---- memory proposals ----------------------------------------------

    # Memory-proposal list/dismiss moved out of the MCP surface to the CLI
    # (`ciao memory-proposals`, `ciao memory-proposal-dismiss`) so the nightly
    # curation agent can review and resolve the queue through one tool instead
    # of a synchronous MCP round-trip.

    # ---- workspaces ----------------------------------------------------

    def workspaces_list(self, principal: AgentPrincipal) -> dict[str, Any]:
        """All configured logical workspaces, not just the active one."""
        from ciao.workspaces import workspace_to_dict

        return _ok(
            {"workspaces": [workspace_to_dict(item, self.config) for item in self.config.workspaces.values()]}
        )

    # ---- vault ---------------------------------------------------------

    def _entity_index_root(self, principal: AgentPrincipal) -> Path:
        """The vault whose INDEX.md covers this chat. See vault_index_refresh."""
        workspace = self._workspace(principal)
        if workspace:
            try:
                return Path(self.config.agent_vault_root(workspace))
            except (AttributeError, ValueError):
                pass
        return Path(self.config.vault_root)

    def _index_stamp(self, principal: AgentPrincipal) -> str:
        """Workspace to stamp on scanned entries, or "" to infer from the path.

        Empty before the re-rooting: the shared vault holds every workspace, so
        the first-path-segment inference is what labels them. The workspace name
        afterwards, because a root's vault holds exactly one workspace and its
        first segment is a folder name.
        """
        workspace = self._workspace(principal)
        if not workspace:
            return ""
        try:
            rooted = Path(self.config.agent_vault_root(workspace)) != Path(
                self.config.vault_root
            )
        except (AttributeError, ValueError):
            return ""
        return workspace if rooted else ""

    def _search_key_base(self) -> Path:
        """The install root, which every stored search key is relative to.

        Falls back to the configured vault root's parent, which is the install
        root in both layouts: before the re-rooting the vault sits directly under
        it, and after it ``config.vault_root`` still names that same path even
        though each root now owns its own vault. The fallback exists because
        several call sites build a minimal config stub.
        """
        base = getattr(self.config, "workspace_root", None)
        if base:
            return Path(base)
        return Path(self.config.vault_root).parent

    def _search_runtime_dir(self) -> Path | None:
        """The install runtime directory that owns this install's search index.

        Every control-plane entry point resolves its database through this, so
        the MCP tools cannot disagree with the CLI or startup indexing about
        which database belongs to this install. Falls back to the install root's
        ``.runtime`` when a minimal config stub has no ``state_path`` — the same
        directory the server uses.
        """
        state_path = getattr(self.config, "state_path", None)
        if state_path:
            return Path(state_path).parent
        base = self._search_key_base()
        return base / ".runtime"

    async def vault_search(self, principal: AgentPrincipal, query: str, limit: int = 10) -> dict[str, Any]:
        """Search this workspace's notes, and only this workspace's notes.

        Keys are stored relative to the install root so two agent roots holding
        a vault of the same name cannot overwrite each other's rows, and the
        result set is filtered to this vault's prefix. Both halves are needed:
        the isolation used to be a side effect of the index prune deleting every
        other root's rows on each pass, which also meant switching workspace
        re-indexed the whole vault.

        Scope is resolved on the calling thread so a bad principal fails fast;
        the incremental index pass and the search itself run in a bounded worker,
        which opens and closes its own SQLite connection there so a connection
        never crosses threads. Identical in-flight searches coalesce.
        """
        root = self._vault_root(principal)
        base = self._search_key_base()
        runtime_dir = self._search_runtime_dir() or (base / ".runtime")
        bounded_limit = max(1, min(50, int(limit)))

        def _search() -> list[dict[str, str]]:
            # Install-owned database (SYS-03), resolved inside the worker so the
            # connection and its path never cross threads.
            db_path = get_db_path(self._search_runtime_dir())
            conn = sqlite3.connect(db_path)
            try:
                # Serialize the write phase per database file. Distinct query
                # keys run concurrently, but SQLite takes one file-level write
                # lock, so two index passes against this database would race it
                # and fail with "database is locked" once a scan outlasts the
                # connection timeout. The search that follows is read-only and
                # does not need the lock.
                with keyed_lock(f"fts-index:{db_path}"):
                    init_db(conn)
                    index_vault(conn, root, path_base=base)
                rows = search_vault(
                    conn,
                    query,
                    limit=bounded_limit,
                    path_prefix=vault_key_prefix(root, base),
                )
            finally:
                conn.close()
            # Retrieval telemetry for the decay-by-disuse audit: which notes
            # recall actually uses. Best-effort, and appended here so the whole
            # read (SQLite plus this write) stays in one worker.
            record_search_hits(runtime_dir, query, [row["path"] for row in rows])
            return rows

        key = f"vault_search:{root}:{base}:{query}:{bounded_limit}"
        rows = await run_read(key, _search)
        return _ok(rows)

    async def vault_index_refresh(self, principal: AgentPrincipal) -> dict[str, Any]:
        """Rebuild the entity index covering this chat, and its search index.

        The index root is ``agent_vault_root(workspace)``, which is correct in
        both layouts and for the same reason each time: it is the vault whose
        INDEX.md this chat's entity lookup reads. Before the re-rooting that is
        the ONE shared index, holding every workspace's prefixed paths and
        filtered per workspace at read time; after it, this root's own index,
        which needs no prefix because the root holds one vault.

        Deliberately not ``_workspace_vault_root``: before the migration that is
        a subtree of the shared vault, and writing an index there produced one
        whose paths no filter recognised while leaving the real one stale.

        The FTS index stays workspace-scoped: it backs ``vault_search``, whose
        isolation boundary is ``_vault_root(principal)``.

        The scan and both SQLite index passes run together in a bounded worker;
        the worker opens and closes its own connection so it never crosses
        threads. Scope is resolved on the calling thread first.
        """
        search_root = self._vault_root(principal)
        index_root = self._entity_index_root(principal)
        stamp = self._index_stamp(principal)
        base = self._search_key_base()
        runtime_dir = self._search_runtime_dir()

        def _refresh() -> tuple[int, int, int]:
            entries = vault_index.scan_vault(index_root, workspace=stamp)
            vault_index.write_index_file(entries, index_root / "INDEX.md")
            # Install-owned database (SYS-03), resolved inside the worker.
            db_path = get_db_path(runtime_dir)
            conn = sqlite3.connect(db_path)
            try:
                # Same database-file write lock as vault_search: an index
                # refresh and a search can land on the same SQLite file, so the
                # write phase is serialized per database.
                with keyed_lock(f"fts-index:{db_path}"):
                    init_db(conn)
                    indexed, removed = index_vault(conn, search_root, path_base=base)
            finally:
                conn.close()
            return len(entries), indexed, removed

        key = f"vault_index_refresh:{index_root}:{search_root}:{base}:{stamp}"
        notes, indexed, removed = await run_read(key, _refresh)
        return _ok({"notes": notes, "fts_indexed": indexed, "fts_removed": removed})

    # ---- vault review --------------------------------------------------

    def vault_review(self, principal: AgentPrincipal, action: str = "list", *, path: str = "", candidate_id: str = "", disposition: str = "", confirm: str = "") -> dict[str, Any]:
        """Inspect candidates or record an explicit, scoped note disposition.

        Destructive operations are intentionally separate from ``decide`` and
        require a candidate generated from the current content hash.

        ``complete`` and ``restore_completed`` are the project-completion pair,
        reachable here for the same reason they are reachable from the panel:
        a candidate payload now advertises ``completable``, and an agent that
        could see a note was a project with somewhere to go had no way to close
        it out.
        """
        from ciao import vault_review as review

        workspace = self._workspace(principal)
        resolver = getattr(self.config, "workspace_vault_root", None)
        root = Path(resolver(workspace) if callable(resolver) else self._vault_root(principal)).resolve()
        # Every mutating action, and nothing else: an unattended schedule may
        # list and inspect, never dispose of a note. `complete` joins the set
        # rather than sitting outside it — it is the destructive class (a
        # folder move, a backlink rewrite across the vault, a terminal ledger
        # row) and the panel already requires an attended, confirmed click —
        # and `restore_completed` joins it because `restore` from the trash is
        # guarded on the same grounds, an undo of a disposition being exactly
        # as much of a decision as the disposition.
        if action in review.ATTENDED_ONLY_ACTIONS:
            if self._unattended_turn(principal):
                raise ControlPlaneError("unattended_forbidden", "Vault review mutations require an attended turn.")
        # `restore_completed` belongs here rather than below for the same reason
        # `restore` does: a completed project left the queue, so the candidate
        # scan below cannot resolve its id, and the restore repoints links on the
        # way back rather than disposing of a candidate.
        if action in {"restore", "delete", "restore_completed"}:
            try:
                if action == "restore":
                    result = review.restore_note(root, candidate_id)
                elif action == "delete":
                    result = review.delete_permanently(root, candidate_id, confirm=confirm)
                else:
                    result = review.restore_completed(root, candidate_id, workspace=workspace)
                review.generate_candidates(root, workspace=workspace, write_queue=True)
            except (ValueError, OSError) as exc:
                raise ControlPlaneError("vault_review_invalid", str(exc)) from exc
            return _ok(result)
        # Validated before the queue is regenerated below: `record_decision`
        # raises on a bad disposition, but by then `write_queue=True` has
        # already rewritten `Workspace/Vault-Review.md`, so a rejected call
        # still did work. Naming the valid set also makes the error actionable
        # for an agent following a stale instruction.
        if action == "decide" and disposition not in review.DECISION_DISPOSITIONS:
            raise ControlPlaneError(
                "vault_review_invalid",
                f"disposition must be one of {sorted(review.DECISION_DISPOSITIONS)}.",
            )
        # `list` and `inspect` are declared read-only to the MCP host, so they
        # must not refresh the queue projection either.
        #
        # Generated at the full ceiling so an id or path the PWA shows (it lists
        # the whole queue) resolves here too; only the `list` reply is trimmed
        # to the short batch an agent works through.
        candidates = review.generate_candidates(
            root,
            workspace=workspace,
            max_candidates=review.MAX_CANDIDATES_CEILING,
            write_queue=action not in {"list", "inspect"},
        )
        by_id = {item.candidate_id: item for item in candidates}
        if action == "list":
            return _ok({"candidates": [item.as_dict() for item in candidates[: review.MAX_CANDIDATES]]})
        item = by_id.get(candidate_id)
        if item is None and path:
            item = next((candidate for candidate in candidates if candidate.path == path), None)
        if item is None:
            raise ControlPlaneError("candidate_not_found", "Review candidate was not found or has changed.")
        try:
            if action == "inspect":
                return _ok(item.as_dict())
            if action == "decide":
                result = review.record_decision(root, item, disposition)
                review.generate_candidates(root, workspace=workspace, write_queue=True)
                return _ok(result)
            if action == "trash":
                result = review.trash_note(root, item)
                review.generate_candidates(root, workspace=workspace, write_queue=True)
                return _ok(result)
            if action == "complete":
                # The candidate comes off `generate_candidates`, so it carries
                # the `vault_root` the completion's destination check needs; the
                # review module refuses when it does not rather than answering
                # from the path alone.
                result = review.complete_project_note(root, item)
                review.generate_candidates(root, workspace=workspace, write_queue=True)
                return _ok(result)
        except (ValueError, OSError) as exc:
            raise ControlPlaneError("vault_review_invalid", str(exc)) from exc
        raise ControlPlaneError(
            "invalid_action",
            "action must be list, inspect, decide, trash, restore, complete, restore_completed, or delete.",
        )

    # ---- projects/chats ------------------------------------------------

    def projects_list(self, principal: AgentPrincipal, include_completed: bool = False) -> dict[str, Any]:
        workspace = self._workspace(principal)
        data: dict[str, Any] = {
            "active": [item.to_dict() for item in self.pcm.list_projects(workspace)]
        }
        if include_completed:
            data["completed"] = self.pcm.list_completed_projects(workspace)
        return _ok(data)

    def project_get(self, principal: AgentPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        return _ok(project.to_dict())

    def project_create(self, principal: AgentPrincipal, name: str, context: str = "") -> dict[str, Any]:
        workspace = self._workspace(principal)
        clean_name = name.strip()
        if not clean_name:
            raise ControlPlaneError("invalid_name", "Project name is required.")
        return _ok(self.pcm.create_project(clean_name, workspace, context).to_dict())

    def project_update(
        self,
        principal: AgentPrincipal,
        project_id: str = "",
        *,
        name: str | None = None,
        context: str | None = None,
        vault_folder: str | None = None,
    ) -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        item = self.pcm.update_project(
            project.project_id, name=name, context=context, vault_folder=vault_folder
        )
        if item is None:
            raise ControlPlaneError("project_not_found", f"Project '{project.project_id}' was not found.")
        return _ok(item.to_dict())

    def project_complete(self, principal: AgentPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        pid = project.project_id
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == pid:
            return self._defer_until_chat_idle(
                principal,
                "project_complete",
                lambda: self.pcm.complete_project(pid),
            )
        return _ok(self.pcm.complete_project(pid))

    def project_restore(self, principal: AgentPrincipal, stem: str) -> dict[str, Any]:
        workspace = self._workspace(principal)
        return _ok(self.pcm.restore_project(workspace, stem))

    def project_delete(self, principal: AgentPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        pid = project.project_id
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == pid:
            return self._defer_until_chat_idle(
                principal,
                "project_delete",
                lambda: {
                    "deleted": self.pcm.delete_project(pid),
                    "project_id": pid,
                },
            )
        return _ok({"deleted": self.pcm.delete_project(pid), "project_id": pid})

    def chats_list(self, principal: AgentPrincipal, project_id: str = "") -> dict[str, Any]:
        if project_id:
            # Accept a project name as well as an id (same resolver
            # `chat_create` uses), scoped to the principal's own workspace.
            project = self._project(principal, self._resolve_project_id(principal, project_id))
            chats = self.pcm.list_chats(project.project_id)
        else:
            chats = self._workspace_chats(principal)
        return _ok([self._chat_review_dict(chat) for chat in chats])

    def chat_get(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        return _ok(self._chat_review_dict(chat))

    def _chat_review_dict(self, chat: Any) -> dict[str, Any]:
        """Add reliable state needed by unattended chat cleanup routines."""
        result = cast(
            dict[str, Any], chat.to_dict(local=self.pcm.is_session_local(chat))
        )
        result["last_response"] = getattr(chat, "last_response", "")
        result["last_response_status"] = getattr(chat, "last_response_status", "")
        get_active_stream = getattr(self.pcm, "get_active_stream", None)
        result["active_turn"] = (
            get_active_stream(chat.chat_id) is not None
            if get_active_stream is not None
            else False
        )
        result["needs_attention"] = bool(
            getattr(chat, "pending_question", "")
            or getattr(chat, "pending_permission", "")
        )
        return result

    def chat_create(
        self,
        principal: AgentPrincipal,
        project_id: str | None = None,
        *,
        title: str = "New Chat",
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        requested_mode = mode
        mode = self._child_mode(principal, mode)
        chat = self.pcm.create_chat(
            project.project_id, title=title, provider=provider, model=model, mode=mode
        )
        result = chat.to_dict(local=True)
        if requested_mode and requested_mode != mode:
            result["mode_clamped"] = True
            result["requested_mode"] = requested_mode
        text = (prompt or "").strip()
        if text:
            if self.pcm.queue_message(chat.chat_id, text):
                result["send_status"] = "queued"
            else:
                self.pcm.start_stream(chat.chat_id, text)
                result["send_status"] = "started"
        return _ok(result)

    def chat_update(
        self,
        principal: AgentPrincipal,
        chat_id: str,
        *,
        title: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        thinking_level: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if project_id is not None:
            # A name resolves the same way it does on `chat_create`; the
            # ownership check still runs against the resolved id.
            project_id = self._resolve_project_id(principal, project_id)
            self._project(principal, project_id)
        requested_mode = mode
        if mode is not None:
            # A normal/auto MCP caller must not upgrade its own or another
            # chat to bypass through the auto-approved metadata tool. Keep the
            # same ceiling used for newly-created child chats.
            mode = self._child_mode(principal, mode)
        updated = self.pcm.update_chat(
            chat_id,
            title=title,
            provider=provider,
            model=model,
            mode=mode,
            thinking_level=thinking_level,
            project_id=project_id,
        )
        if updated is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        result = updated.to_dict(local=self.pcm.is_session_local(updated))
        if requested_mode and requested_mode != mode:
            result["mode_clamped"] = True
            result["requested_mode"] = requested_mode
        return _ok(result)

    def chat_send(self, principal: AgentPrincipal, chat_id: str, prompt: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        chat_id = chat.chat_id
        if chat.archived:
            raise ControlPlaneError("chat_archived", "Cannot send to an archived chat.")
        text = prompt.strip()
        if not text:
            raise ControlPlaneError("empty_prompt", "Prompt is required.")
        if self.pcm.queue_message(chat_id, text):
            return _ok({"chat_id": chat_id, "status": "queued"})
        self.pcm.start_stream(chat_id, text)
        return _ok({"chat_id": chat_id, "status": "started"})

    def chat_continue(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        chat = self.pcm.continue_archived_chat(chat.chat_id)
        return _ok(chat.to_dict(local=True))

    def chat_retry(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        stream = self.pcm.try_chat_retry_now(chat_id)
        return _ok({"chat_id": chat_id, "status": "started" if stream else "not_pending"})

    def chat_retry_update(
        self,
        principal: AgentPrincipal,
        chat_id: str,
        action: Literal["set", "stop", "try_now"],
        prompt: str = "",
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if action == "set":
            chat = self.pcm.set_chat_retry(chat_id, prompt, image_refs=[], reason="mcp")
            if chat is None:
                raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
            return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))
        if action == "stop":
            chat = self.pcm.stop_chat_retry(chat_id)
            if chat is None:
                raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
            return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))
        if action == "try_now":
            return self.chat_retry(principal, chat_id)
        raise ControlPlaneError("invalid_action", "action must be set, stop, or try_now.")

    def chat_new_session(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal, "chat_new_session", lambda: self.pcm.new_session(chat_id)
            )
        chat = self.pcm.new_session(chat_id)
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        return _ok(chat.to_dict(local=True))

    def chat_handover(
        self,
        principal: AgentPrincipal,
        chat_id: str,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_handover",
                lambda: self.pcm.handover_chat(
                    chat_id,
                    provider=provider.strip(),
                    model=model.strip(),
                    messages=[row for row in (messages or []) if isinstance(row, dict)],
                ),
            )
        chat = self.pcm.handover_chat(
            chat_id,
            provider=provider.strip(),
            model=model.strip(),
            messages=[row for row in (messages or []) if isinstance(row, dict)],
        )
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))

    async def chat_archive(self, principal: AgentPrincipal, chat_id: str = "") -> dict[str, Any]:
        target_id = chat_id.strip()
        if not target_id or target_id.lower() in {"this", "this chat", "current", "self"}:
            target_id = principal.chat_id
        chat = self._chat(principal, target_id)
        # `_chat` also accepts a title, so take the id back from the chat it
        # resolved rather than from what the caller typed.
        target_id = str(chat.chat_id)
        project = self._project(principal, chat.project_id)

        async def _archive() -> dict[str, Any]:
            outcome = await self.pcm.archive_chat(target_id)
            if outcome is not None:
                self.pcm.run_archive_postprocess(target_id, outcome, chat, project)
            return {
                "chat_id": target_id,
                "archived_to": str(outcome.path) if outcome else None,
            }

        if target_id == principal.chat_id:
            # Archiving the calling chat tears down its own tool caller, so it
            # still waits for this turn to finish before anything happens.
            return self._defer_until_chat_idle(principal, "chat_archive", _archive)
        return _ok(await _archive())

    def chat_delete(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_delete",
                lambda: {"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)},
            )
        return _ok({"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)})

    async def chat_stop(self, principal: AgentPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            raise ControlPlaneError(
                "self_stop_forbidden",
                "The current turn cannot stop itself through MCP; use the PWA stop control.",
            )
        return _ok({"chat_id": chat_id, "stopped": await self.pcm.stop_chat(chat_id)})

    # ---- tasks -----------------------------------------------------------
    #
    # One workspace-scoped task service with two callers. The agent operations
    # (``ciao task …``) enter through the ``task_*`` wrappers below, which scope
    # the call to the principal's own workspace and act as ``actor="agent"``;
    # the session routes in ``ciao/web/routes_tasks.py`` enter through the same
    # ``workspace_task_*`` methods with a validated workspace name and
    # ``actor="user"``, because a signed-in browser is the person the store's
    # completion rule protects. Nothing else may reach the store, and the root
    # it is built from is always the workspace's own vault — never a root a
    # request supplied.

    def workspace_vault_root(self, workspace: str) -> Path:
        """The authoritative vault root of one registered workspace.

        The one place a workspace *name* becomes a path. ``_vault_root`` is this
        behind ``_workspace``; both the agent wrappers and the session routes
        resolve through here, which is what keeps an HTTP-supplied root out of
        the task store: a route may only name a workspace, never a directory.
        """
        name = str(workspace or "").strip()
        if not name or self.config.workspace(name) is None:
            raise ControlPlaneError("workspace_not_found", f"Workspace '{name}' was not found.")
        resolver = getattr(self.pcm, "_workspace_vault_root", None)
        if callable(resolver):
            try:
                return Path(resolver(name)).resolve()
            except (AttributeError, ValueError, OSError) as exc:
                raise ControlPlaneError(
                    "workspace_unavailable", f"Workspace '{name}' has no usable vault: {exc}"
                ) from exc
        return Path(self.config.vault_root).resolve()

    def _task_store(self, workspace: str) -> TaskBoardStore:
        """One workspace's task store, with the runtime dir beside the state file."""
        return TaskBoardStore(
            workspace=workspace,
            vault_root=self.workspace_vault_root(workspace),
            runtime_dir=Path(self.config.state_path).parent,
            clock=_utc_now,
            on_change=lambda: self._tasks_changed(workspace),
        )

    def _tasks_changed(self, workspace: str) -> None:
        """Tell open clients that *workspace*'s board or an attempt on it moved.

        The one choke point for ``tasks_changed`` on ``/ws/events``: both stores
        call it after every write that landed, so a route, an agent's
        ``ciao task`` call and a turn settling at its end all announce through
        the same place. The event carries only the workspace name — the same
        name ``/api/tasks?workspace=`` takes — and the client re-reads.

        Safe from any thread and never raises: it runs inside a store write that
        has already landed, and failing to announce must not turn that into an
        error. Writes made in one loop callback coalesce into one event.
        """
        hub = getattr(self.pcm, "events", None)
        if hub is None:
            # A chat-manager fake without an events hub: nothing listens.
            return
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = self._loop
        if loop is None or loop.is_closed():
            # Subscribers live on the engine's loop; without one nobody listens.
            return
        with self._tasks_changed_guard:
            if workspace in self._tasks_changed_pending:
                return
            self._tasks_changed_pending.add(workspace)
        try:
            loop.call_soon_threadsafe(self._flush_tasks_changed, hub, workspace)
        except RuntimeError:
            with self._tasks_changed_guard:
                self._tasks_changed_pending.discard(workspace)

    def _flush_tasks_changed(self, hub: Any, workspace: str) -> None:
        with self._tasks_changed_guard:
            self._tasks_changed_pending.discard(workspace)
        self._publish_tasks_changed(hub, workspace)

    @staticmethod
    def _publish_tasks_changed(hub: Any, workspace: str) -> None:
        try:
            hub.publish({"type": "tasks_changed", "workspace": workspace})
        except Exception:  # noqa: BLE001 — an announcement never fails a write
            logger.exception("tasks: could not publish tasks_changed for %s", workspace)

    def _task_call(self, workspace: str, call: Callable[[TaskBoardStore], Any]) -> Any:
        """One store call for *workspace*, its typed refusal translated into ours.

        The single boundary: a ``TaskBoardError`` never escapes past this, so
        neither the agent envelope nor a session route can see a store code the
        error table above does not name.
        """
        self._migrate_task_schema(workspace)
        try:
            return call(self._task_store(workspace))
        except TaskBoardError as exc:
            raise _task_error(exc) from exc

    def _migrate_task_schema(self, workspace: str) -> None:
        """Rewrite *workspace*'s schema-1 task files to schema 2, once per process.

        The first task call for a workspace does it (#1069), so a board is never
        read in the old shape. An attempt bound to a file's old bytes follows the
        rewrite: the migration is not an edit to the task, and leaving the binding
        behind would read as "changed since delegated". Best effort; a file it
        cannot upgrade lists as an unsupported-schema row that says why.
        """
        done: set[str] = self.__dict__.setdefault("_task_schema_checked", set())
        if workspace in done:
            return
        done.add(workspace)
        try:
            migrated = self._task_store(workspace).migrate_schema_1()
        except (TaskBoardError, OSError):
            logger.exception("tasks: could not migrate %s's task files to schema 2", workspace)
            return
        if not migrated:
            return
        logger.info("tasks: migrated %d task file(s) in %s to schema 2", len(migrated), workspace)
        try:
            store = self._attempt_store(workspace)
            for task_id, old, new in migrated:
                for attempt in store.list_for_task(task_id):
                    if attempt.task_revision == old:
                        store.bind_revision(attempt.attempt_id, new)
        except (TaskAttemptError, OSError):
            logger.exception("tasks: could not rebind attempts after migrating %s", workspace)

    def _task_project(self, workspace: str, ref: str) -> str:
        """The exact project id a task may name inside *workspace*.

        Same name-or-id resolution the project surfaces use, and a project in
        another workspace reads exactly like a nonexistent one so the two codes
        never become an existence oracle over other workspaces' project ids.
        """
        value = str(ref or "").strip()
        resolved = ""
        exact = self.pcm.get_project(value)
        if exact is not None and getattr(exact, "workspace", "") == workspace:
            resolved = str(exact.project_id)
        if not resolved:
            matches = [
                project
                for project in self.pcm.list_projects(workspace)
                if str(getattr(project, "name", "")).casefold() == value.casefold()
            ]
            if len(matches) == 1:
                resolved = str(matches[0].project_id)
            elif len(matches) > 1:
                raise ControlPlaneError(
                    "project_ambiguous",
                    f"'{value}' matches more than one project; use its exact id instead.",
                )
        if not resolved:
            raise ControlPlaneError("project_not_found", f"Project '{value}' was not found.")
        return resolved

    def workspace_task_list(self, workspace: str) -> list[dict[str, Any]]:
        """One workspace's board rows, valid tasks first, then unreadable files.

        A file that is not a readable task is never dropped: it comes back as a
        row carrying ``code`` (and no task fields), so a malformed file cannot
        masquerade as an empty or healthy board.

        Every readable row carries the same attempt facts a single read does, so a
        board draws a delegation badge from one request per workspace rather than
        one per card. The attempts are one document, read once here.
        """
        result = self._task_call(workspace, lambda store: store.list())
        live: dict[str, TaskAttempt] = self._attempt_call(
            workspace, lambda store: store.live_by_task()
        )
        current: dict[str, TaskAttempt] = self._attempt_call(
            workspace, lambda store: store.newest_by_task()
        )
        return [
            _task_row_with_attempt(
                document,
                live.get(document.record.id),
                current.get(document.record.id),
            )
            for document in result.tasks
        ] + [
            {
                "id": entry.relative_path.rsplit("/", 1)[-1].removesuffix(".md"),
                "path": entry.relative_path,
                "code": entry.code,
                "message": entry.message,
            }
            for entry in result.invalid
        ]

    def workspace_task_get(self, workspace: str, task_id: str) -> dict[str, Any]:
        """One task as source currently reads it, body and attempt facts included.

        The attempt fields are on the read as well as the list because the
        board's editor is where a delegated task's badge and link belong: a card
        that could only be decorated from the list would show a delegated task as
        an ordinary one for as long as the dialog was open.
        """
        document = self._task_call(workspace, lambda store: store.get(str(task_id or "").strip()))
        return self._task_with_attempt(workspace, document, include_body=True)

    def workspace_task_create(
        self,
        workspace: str,
        *,
        title: str,
        body: str = "",
        project_id: str | None = None,
        due: str | None = None,
    ) -> dict[str, Any]:
        """Create one task in one workspace and return it as stored."""
        project = self._task_project(workspace, project_id) if project_id else None
        document = self._task_call(
            workspace,
            lambda store: store.create(
                title=str(title or ""),
                body=str(body or ""),
                project_id=project,
                due=due or None,
            ),
        )
        return _task_payload(document, include_body=True)

    def workspace_task_update(
        self,
        workspace: str,
        task_id: str,
        *,
        expected_revision: str,
        changes: Mapping[str, object],
        body: str | None = None,
        actor: Actor = "agent",
    ) -> dict[str, Any]:
        """Apply managed edits to one task at the revision the caller read.

        The store owns the edit contract — which fields exist, which value
        shapes are legal, and the rule that only a user may mark a task done.
        This layer adds the one thing a pure file store cannot know: that a
        ``project_id`` names a live project in this same workspace.

        The answer carries the attempt facts, because an edit is exactly what can
        make a delegated task ``changed_since_delegated``: the user editing a task
        the agent is working on is the case that flag exists for.
        """
        planned: dict[str, object] = {}
        for key, value in dict(changes or {}).items():
            if key == "project_id":
                # An id or a name in this workspace, or `None` to clear it. A
                # non-string is refused by the resolver's own str() rather than
                # reaching the store as a value it would coerce differently.
                planned[key] = self._task_project(workspace, str(value)) if value else None
            else:
                planned[key] = value
        document = self._task_call(
            workspace,
            lambda store: store.update(
                str(task_id or "").strip(),
                expected_revision=str(expected_revision or ""),
                changes=planned,
                body=body,
                actor=actor,
            ),
        )
        return self._task_with_attempt(workspace, document, include_body=True)

    def workspace_task_action(
        self,
        workspace: str,
        action: str,
        task_id: str,
        *,
        expected_revision: str,
        status: str | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        due: str | None = None,
        actor: Actor = "agent",
    ) -> dict[str, Any]:
        """One board gesture: ``move``, ``complete`` or ``reassign``.

        ``complete`` is an ordinary status edit to ``done``, so an agent caller
        is refused by the store (``completion_requires_user``) exactly as it is
        through ``task_update``; the gesture exists so the surface has a verb for
        "mark this done" rather than a free-text status the caller must know.

        A ``complete`` on a task whose attempt has a **result waiting for review**
        is the one gesture that reaches past the linkage, and only for a user: see
        :meth:`workspace_task_complete_reviewed`. A turn still in flight is not
        reviewable, so a live ``running``/``needs_you`` attempt is still refused
        here and has to be stopped or detached first.
        """
        verb = str(action or "").strip()
        if verb == "move":
            target = str(status or "").strip()
            if not target:
                raise ControlPlaneError("invalid_action", "move requires a target status.")
            changes: dict[str, object] = {"status": target}
        elif verb == "complete":
            changes = {"status": "done"}
        elif verb == "reassign":
            who = str(assignee or "").strip()
            if not who:
                raise ControlPlaneError("invalid_action", "reassign requires an assignee.")
            changes = {"assignee": who}
        else:
            raise ControlPlaneError(
                "invalid_action", "action must be move, complete, or reassign."
            )
        if project_id is not None:
            changes["project_id"] = project_id
        if due is not None:
            changes["due"] = due
        if verb == "complete":
            reviewed = self.workspace_task_complete_reviewed(
                workspace,
                task_id,
                expected_revision=expected_revision,
                changes=changes,
                actor=actor,
            )
            if reviewed is not None:
                return reviewed
        return self.workspace_task_update(
            workspace,
            task_id,
            expected_revision=expected_revision,
            changes=changes,
            actor=actor,
        )

    def workspace_task_complete_reviewed(
        self,
        workspace: str,
        task_id: str,
        *,
        expected_revision: str,
        changes: dict[str, object],
        actor: Actor,
    ) -> dict[str, Any] | None:
        """Close a task whose delegated turn has a result waiting, in one gesture.

        ``None`` means "this task is not in that situation" and the caller should
        complete it the ordinary way; a refusal raises, exactly as the ordinary
        completion does.

        The case is the review. A ``ready_for_review`` attempt is still *live*, so
        the store refuses a completion over a live linkage — correct in general,
        since a turn still writing is not one to close behind its back. But
        ``ready_for_review`` is not "still writing": the turn ended, the result is
        there, and the user is looking at it and deciding. Forcing that decision
        through Detach first is a gesture that reads as "discard the attempt": it
        loses how the turn ended. So approving Done *is* the release — a
        revision-checked ``unlink``, the completion, and then the attempt marked
        released, one gesture — and the attempt stays in history as the
        ``ready_for_review`` it was.

        Three writes, in that order, and the order is the contract:

        1. ``unlink`` at the revision the caller read. A stale one is refused
           before anything is written, which is why this is not a race with the
           review badge the watcher may have written after the board was drawn.
        2. The completion itself. A refusal here for any other reason (a project
           that has since been deleted, say) would leave a task unlinked and
           *not* done — released from its attempt with nothing having happened —
           so the linkage is put back before the refusal is raised. The record
           goes back as it was, including the status and assignee the linkage
           found, and a re-link that cannot be applied is logged rather than
           swallowed over.
        3. ``release`` on the attempt, which is what frees the task to be
           delegated again. It cannot fail on its own facts, and it is last
           because it is the only step that may not be undone: a completed task
           is the user's decision, and re-opening it behind their back would be a
           worse answer than a stale marker.

        Only for ``actor="user"``: the store's own
        ``completion_requires_user`` rule stands, and an agent approving a review
        is refused there rather than reaching this at all. And only for a
        *review-ready* attempt: ``running`` and ``needs_you`` have no result yet, so
        those still need Stop or Detach first.

        ``changes`` is the caller's own edit dict, passed through rather than
        rebuilt: a completion that also carried a project or a due date must still
        apply it, and this path is not a second, narrower completion.
        """
        if actor != "user":
            return None
        clean = str(task_id or "").strip()
        document: TaskDocument = self._task_call(
            workspace, lambda store: store.get(clean)
        )
        linked = document.record.attempt_id
        if linked is None:
            return None
        attempt: TaskAttempt = self._attempt_call(
            workspace, lambda store: store.get(linked)
        )
        if attempt.state != "ready_for_review" or not attempt.is_live:
            return None
        released = self._task_call(
            workspace,
            lambda store: store.unlink(clean, expected_revision=expected_revision),
        )
        try:
            self.workspace_task_update(
                workspace,
                clean,
                expected_revision=released.revision,
                changes=changes,
                actor=actor,
            )
        except ControlPlaneError:
            # The unlink landed and the completion did not, so the task is free of
            # its attempt and still open: a card nothing owns, with a result the
            # user was looking at. Put the linkage back rather than hand that out.
            self._relink_after_refusal(
                workspace, released, attempt, document.record.status, document.record.assignee
            )
            raise
        self._release_attempt(workspace, attempt.attempt_id)
        self._learn_from_approved(workspace, attempt, document.record.title)
        # Read again rather than returning the completion's own row: the reply is
        # what a board paints, and a completed card still naming a live attempt
        # would keep drawing Stop and Detach over a task with no chat to open.
        done: TaskDocument = self._task_call(workspace, lambda store: store.get(clean))
        return self._task_with_attempt(workspace, done, include_body=True)

    def _learn_from_approved(self, workspace: str, attempt: TaskAttempt, title: str) -> None:
        """Archive an approved task's chat and run a procedure-focused memory pass.

        The user approving a delegated result says this conversation is how that
        kind of work is done correctly (#1069). Archiving it runs the ordinary
        memory pass with an ``approved_task`` focus: the pass extracts the
        procedure and files skill proposals or new-skill drafts, and facts as
        usual — all waiting in To decide for the user. Archiving is the natural
        end of an approved task's chat, and the attempt is already released, so
        the archive hook leaves the task alone.

        Scheduled on the engine's loop, never awaited: an approval must not wait
        on a transcript render, and nothing here may fail the approval. The
        ``/complete`` route runs the approval in a worker thread
        (``asyncio.to_thread``), where there is no running loop, so the work is
        handed to the loop this plane was built on — the same one
        ``tasks_changed`` is published on. Without one (a test calling the
        service outside any loop) it does nothing.
        """
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            loop, on_loop = self._loop, False
        if loop is None or loop.is_closed():
            return
        focus = {
            "focus": "approved_task",
            "task_title": title,
            "task_summary": attempt.summary,
        }

        async def _run() -> None:
            try:
                chat = self.pcm.get_chat(attempt.chat_id)
                if chat is None:
                    return
                project = self.pcm.get_project(str(getattr(chat, "project_id", "") or ""))
                if getattr(chat, "archived", False):
                    archive_path = str(getattr(chat, "archive_path", "") or "")
                    if archive_path:
                        path = Path(archive_path)
                        if not path.is_absolute():
                            path = Path(self.config.workspace_root) / path
                        self.pcm.enqueue_memory_pass(
                            chat, project, path,
                            project.vault_doc_path if project is not None and not project.is_auto else "",
                            focus,
                        )
                    return
                outcome = await self.pcm.archive_chat(attempt.chat_id)
                if outcome is not None:
                    self.pcm.run_archive_postprocess(
                        attempt.chat_id, outcome, chat, project, focus
                    )
            except Exception:  # noqa: BLE001 — the approval already succeeded
                logger.exception(
                    "tasks: could not learn from the approved task in chat %s", attempt.chat_id
                )

        def _spawn() -> None:
            task = loop.create_task(_run(), name=f"task-learn-{attempt.attempt_id[:8]}")
            self._watchers.add(task)
            task.add_done_callback(self._watchers.discard)

        if on_loop:
            _spawn()
            return
        try:
            loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            logger.warning(
                "tasks: the loop closed before the approved task in chat %s could be archived",
                attempt.chat_id,
            )

    def _relink_after_refusal(
        self,
        workspace: str,
        released: TaskDocument,
        attempt: TaskAttempt,
        status: str,
        assignee: str,
    ) -> None:
        """Restore the linkage a refused completion took away, never raising.

        The record goes back as :meth:`TaskBoardStore.link` found it — the status
        and assignee the linkage write itself had made — so a retry sees the same
        live task it would have seen without the refused gesture.

        The attempt is rebound to the revision the re-link left behind, for the same
        reason the review badge is followed by a rebind: the write moved the task's
        revision, and an attempt left pointing at the older one would report "changed
        since delegated" on a description nobody edited. Best effort, like the badge's.

        Best effort for the refusal itself too: it is being raised to the caller
        either way, and a second failure here must not replace the one that explains
        what the user did. It is logged, and the record it cannot restore is a task
        with no attempt behind it — which the user can still complete, reassign or
        delegate.
        """
        try:
            relinked: TaskDocument = self._task_call(
                workspace,
                lambda store: store.link(
                    released.record.id,
                    expected_revision=released.revision,
                    chat_id=attempt.chat_id,
                    attempt_id=attempt.attempt_id,
                    status=status,
                    assignee=assignee,
                ),
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: task %s could not be re-linked to attempt %s after a "
                "refused completion",
                released.record.id,
                attempt.attempt_id,
            )
            return
        try:
            self._attempt_call(
                workspace, lambda store: store.bind_revision(attempt.attempt_id, relinked.revision)
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: attempt %s could not be rebound after a re-linked task",
                attempt.attempt_id,
            )

    def workspace_task_delete(
        self, workspace: str, task_id: str, *, expected_revision: str
    ) -> dict[str, Any]:
        """Remove one task record from one workspace, revision-checked.

        User-only, and deliberately not an agent operation: there is no
        ``ciao task delete`` verb and no operation entry for one. A record is
        the user's own Markdown file, so removing it is a decision the person
        who owns the vault makes, exactly as completion is.
        """
        clean = str(task_id or "").strip()
        self._task_call(
            workspace,
            lambda store: store.delete(
                clean, expected_revision=str(expected_revision or "")
            ),
        )
        return {"id": clean, "deleted": True}

    # ---- delegation (#1033, child B5 of #973) -------------------------
    #
    # Four rules this block exists to hold, and each is a refusal somebody could
    # otherwise have taken:
    #
    # * **No implicit bypass.** The turn is launched through
    #   ``pcm.start_stream(chat_id, prompt)`` with the *default*
    #   ``unattended=False``, exactly as A4's webhook dispatch does.
    #   ``_effective_mode_for_chat`` turns that flag into ``bypass``, so an
    #   approval card raised here is an ordinary Needs-you card answered in the
    #   ordinary chat. The board is a user action, but the constraint is the same:
    #   nobody asked this turn to run unwatched.
    # * **One live attempt per task, and exactly one chat.** ``start`` holds the
    #   attempt store's workspace lock across its read-then-write, so a double
    #   click, a second tab and an agent/UI race all answer with the same attempt
    #   and this method creates no second chat. ``created`` in the reply says which
    #   of the two happened, because only the creating call may start a turn.
    # * **Durable before the side effect.** The attempt and the task's
    #   ``chat_id``/``attempt_id`` linkage are both written before
    #   ``start_stream``. A crash in that window leaves a record naming a chat
    #   nobody ran, which the store derives as ``interrupted`` and never replays;
    #   the other order would leave an orphan chat with nothing pointing at it.
    # * **A finished turn is not a finished task.** The watcher settles
    #   ``ready_for_review``, and it is the *user's* gesture that moves the card
    #   to Done. ``task_board`` still refuses an agent completion, so no path
    #   through this block can close a task on the agent's own authority.

    def _attempt_store(self, workspace: str) -> TaskAttemptStore:
        """One workspace's attempt store, beside the board's lock in the runtime dir."""
        return TaskAttemptStore(
            workspace=workspace,
            runtime_dir=Path(self.config.state_path).parent,
            clock=_utc_now,
            on_change=lambda: self._tasks_changed(workspace),
        )

    def _attempt_call(self, workspace: str, call: Callable[[TaskAttemptStore], Any]) -> Any:
        """One attempt-store call for *workspace*, its refusal translated into ours.

        The single boundary for ``TaskAttemptError``, exactly as
        :meth:`_task_call` is for ``TaskBoardError``.
        """
        try:
            return call(self._attempt_store(workspace))
        except TaskAttemptError as exc:
            raise _attempt_error(exc) from exc

    def _attempt_live(self, workspace: str, task_id: str) -> TaskAttempt | None:
        """The live attempt on one task, or ``None``."""
        live: TaskAttempt | None = self._attempt_call(
            workspace, lambda store: store.get_live(task_id)
        )
        return live

    def _attempt_current(self, workspace: str, task_id: str) -> TaskAttempt | None:
        """One task's current attempt, live or settled, or ``None``.

        The badge's read. A settled attempt no longer holds the task, but its state
        is what the card has to say — ``failed``, ``stopped`` and ``interrupted``
        are precisely the three a user needs to look at — so this is the newest
        attempt rather than the live one.
        """
        rows = self._attempt_call(
            workspace, lambda store: store.list_for_task(task_id)
        )
        return rows[0] if rows else None

    def _delegation_project(
        self, workspace: str, requested: str | None, task_project: str | None
    ) -> tuple[str, str]:
        """The project a delegation runs in, and why it is that one.

        A caller may name a project (an id or a name in *this* workspace, resolved
        by the same helper every other task surface uses, so a project in another
        workspace reads exactly like a nonexistent one). Otherwise the task's own
        ``project_id`` decides, and a task with none goes to the workspace's
        General — the same host the browser's own buttons use, so a delegated chat
        lands where the user would have put it.

        Refuses rather than falls back: a named project that is gone or foreign
        must not silently run the work in General, because the one thing a user
        must be able to trust is that deleting a project stops work arriving in it.
        """
        if requested:
            return self._task_project(workspace, requested), "requested"
        if task_project:
            return self._task_project(workspace, task_project), "task"
        for project in self.pcm.list_projects(workspace):
            if getattr(project, "name", "") == "General":
                return str(project.project_id), "general"
        raise ControlPlaneError(
            "project_not_found",
            f"workspace {workspace!r} has no General project to run a delegated task in.",
        )

    def _task_with_attempt(
        self, workspace: str, document: TaskDocument, *, include_body: bool = False
    ) -> dict[str, Any]:
        """One task payload carrying the attempt facts a board draws.

        See :func:`_task_row_with_attempt`, which is the whole of the derivation;
        this is that function with the store read done for one task.
        """
        payload = _task_row_with_attempt(
            document,
            self._attempt_live(workspace, document.record.id),
            self._attempt_current(workspace, document.record.id),
        )
        if include_body:
            payload["body"] = document.body
        return payload

    def workspace_task_delegate(
        self,
        workspace: str,
        task_id: str,
        *,
        expected_revision: str,
        project_id: str | None = None,
        actor: Actor = "user",
        instructions: str = "",
    ) -> dict[str, Any]:
        """Hand one task to the agent as an ordinary chat, exactly once.

        The whole sequence, and the order is the contract:

        1. Read the task at ``expected_revision``. A stale one is a
           ``task_revision_conflict`` with nothing started — the plan was made
           against a description that has moved — and a task in *Done* is an
           ``invalid_task``, because delegating it would reopen it.
        2. If a live attempt already exists, hand it back and **stop**. No chat is
           created and no turn is started: a second delegation of a delegated task
           is a race, not a request for two turns.
        3. Resolve the project (see :meth:`_delegation_project`) and build the
           prompt from the task's own fields. The only caller text is
           *instructions*, the user's note for this hand-over, quoted in its own
           fence after the description; it cannot replace the task.
        4. Create **one ordinary chat** in that project, titled for the task and
           stamped with a ``task_delegation`` helper naming the task, its revision
           and the attempt — the provenance that lets the board link back to it
           after a reload or a restart.
        5. Persist the attempt **and** the task's linkage, before the turn. The
           attempt store's ``start`` is what makes step 2 atomic; the board's
           ``link`` is revision-checked at the revision this call just read.
        6. ``start_stream(chat_id, prompt)`` with **no** ``unattended``.

        Anything that refuses at or after step 4 settles the attempt
        ``interrupted`` and raises, because from here on this engine cannot say
        what the turn did — a chat exists and nothing will report its outcome. That
        is the state a crash in this window leaves too, and it is never replayed
        without a person asking.

        ``actor`` is recorded for the caller, not used to weaken anything: the
        store refuses an agent completion either way, and this method never
        completes a task.

        A ``done`` task is refused with ``invalid_task``. ``link`` hands the task
        over as ``in_progress``/``agent``, so delegating a finished one would
        silently reopen it — and an agent delegating over MCP could undo a
        completion the user made by hand. Reopening is the user's own move out of
        *Done*, never a side effect of asking for work.
        """
        clean = str(task_id or "").strip()
        note = str(instructions or "").strip()
        if len(note) > MAX_INSTRUCTIONS_CHARS:
            raise ControlPlaneError(
                "invalid_task",
                f"delegation instructions are limited to {MAX_INSTRUCTIONS_CHARS} "
                "characters; put longer guidance in the task's description.",
            )
        document = self._task_call(workspace, lambda store: store.get(clean))
        revision = document.revision
        if str(expected_revision or "").strip() != revision:
            raise ControlPlaneError(
                "task_revision_conflict",
                "the task changed since this delegation was planned; nothing was started",
                retryable=True,
            )
        if document.record.status == "done":
            raise ControlPlaneError(
                "invalid_task",
                "a task in Done cannot be delegated: move it out of Done first.",
            )
        existing = self._attempt_live(workspace, clean)
        if existing is not None:
            # The one-live-attempt rule answered with the attempt that already
            # exists. Nothing is created and nothing is sent: the second caller
            # asked for a delegation and got the delegation that is already
            # running, which is what a double click meant. The revision check
            # above already ran, so a stale board is refused before this.
            return {
                **_attempt_payload(
                    existing, self._task_with_attempt(workspace, document)
                ),
                "created": False,
                "project_id": document.record.project_id or "",
                "changed_since_delegated": bool(
                    existing.task_revision != document.revision
                ),
            }
        record = document.record
        project, origin = self._delegation_project(workspace, project_id, record.project_id)
        prompt = build_prompt(
            title=record.title,
            status=record.status,
            due=record.due or "",
            project_id=record.project_id or "",
            task_id=record.id,
            task_revision=revision,
            relative_path=document.relative_path,
            body=document.body,
            previous_attempts=self._previous_attempts(workspace, record.id),
            instructions=note,
        )
        attempt_id = uuid.uuid4().hex
        # Two revisions are in play from here, and they are not the same one. The
        # chat's helper stamp is written at `create_chat` below, so it can only
        # carry `revision` — the revision this call read and built the prompt from,
        # which is the revision the agent is *handed*. The attempt itself is
        # rebound to `linked.revision` after the linkage write, because that is the
        # revision the record now stands at. Deliberate, not an oversight: the
        # stamp says which revision was delegated, and the attempt's binding says
        # which revision it is judged against, and the two answer different
        # questions.
        try:
            chat = self.pcm.create_chat(
                project,
                title=record.title,
                helper=task_delegation_helper(
                    task_id=record.id, task_revision=revision, attempt_id=attempt_id
                ),
            )
        except Exception as exc:  # noqa: BLE001 — a refusal is an outcome, not a crash
            raise ControlPlaneError(
                "chat_create_failed",
                f"the delegated chat could not be created ({exc})",
                retryable=True,
            ) from exc
        started = self._attempt_call(
            workspace,
            lambda store: store.start(
                task_id=record.id,
                task_revision=revision,
                chat_id=chat.chat_id,
                attempt_id=attempt_id,
                state="running",
            ),
        )
        attempt = started.attempt
        if not started.created:
            # A live attempt appeared between the read above and this write, so
            # `start` answered with the attempt that already owns the task. The
            # chat made above is then an orphan nothing points at: it is left
            # empty rather than given a turn, and the caller is handed the
            # attempt that is actually running.
            return {
                **_attempt_payload(
                    attempt, self._task_with_attempt(workspace, document)
                ),
                "created": False,
                "project_id": document.record.project_id or "",
                "changed_since_delegated": bool(
                    attempt.task_revision != document.revision
                ),
            }
        # The chat's provenance was stamped at creation with the attempt id minted
        # above, so it names the attempt that ended up owning it — there is no
        # second write to restamp it, and `ProjectChatManager.update_chat` has no
        # `helper` parameter to restamp it with. The stamp is exact or the chat
        # creation failed, and the store's `start` records the same id or answers
        # with the attempt that already owns the task (leaving this chat empty).
        try:
            linked = self._task_call(
                workspace,
                lambda store: store.link(
                    record.id,
                    expected_revision=revision,
                    chat_id=chat.chat_id,
                    attempt_id=attempt.attempt_id,
                ),
            )
        except ControlPlaneError:
            # The linkage is what tells the board an attempt owns this task, and
            # without it a settled attempt would be invisible. The attempt stays
            # as history; the chat is left empty rather than run.
            self._settle_interrupted(
                workspace, attempt.attempt_id, "the task could not be linked to the attempt"
            )
            raise
        # The linkage write moved the task's revision, so the attempt is rebound
        # to the revision the record now stands at. Without this the attempt would
        # read as "changed since delegated" the instant it was made, and the flag
        # would carry no information at all. What the hand-off changed — status,
        # assignee, linkage — is not what that flag is about; the body and title
        # the agent was handed are untouched by it.
        bound = self._attempt_call(
            workspace,
            lambda store: store.bind_revision(attempt.attempt_id, linked.revision),
        )
        stream, refusal = self._launch_turn(chat.chat_id, prompt)
        if stream is None:
            self._settle_interrupted(
                workspace,
                attempt.attempt_id,
                f"the turn could not be started ({refusal})",
            )
            raise ControlPlaneError(
                "task_launch_failed",
                f"the delegated chat exists but its turn could not be started ({refusal}); "
                "open the chat to see it, and resume or retry the attempt.",
                retryable=True,
            )
        self._watch_turn(workspace, attempt.attempt_id, record.id, chat.chat_id, stream)
        # The attempt's line in the task's own log, so the vault records it from
        # the first moment. Its write moves the revision again, so the reply is
        # built from what it left behind.
        logged = self._record_log(workspace, attempt.attempt_id)
        if logged is not None:
            linked, bound = logged
        return {
            # `bound`, not `attempt`: the reply must carry the revision the record
            # now stands at, or a caller that stored this payload would compare it
            # against the task and read "changed since delegated" on an attempt that
            # was made a moment ago.
            **_attempt_payload(bound, self._task_with_attempt(workspace, linked)),
            "project_id": project,
            "project_origin": origin,
            "created": True,
        }

    # ---- the agent's report and the task's own log (#1064) -------------

    def _chat_facts(self, chat_id: str, chat: Any = None) -> tuple[str, str]:
        """``(title, archive_path)`` for a chat, or empty strings for one gone."""
        if chat is None:
            try:
                chat = self.pcm.get_chat(chat_id)
            except Exception:  # noqa: BLE001 — an unreadable chat is just untitled
                chat = None
        if chat is None:
            return ("", "")
        return (
            str(getattr(chat, "title", "") or ""),
            str(getattr(chat, "archive_path", "") or "") if getattr(chat, "archived", False) else "",
        )

    def _previous_attempts(self, workspace: str, task_id: str) -> tuple[PreviousAttempt, ...]:
        """The settled attempts a new one is told about, newest first. Never raises."""
        try:
            rows = self._attempt_call(workspace, lambda store: store.list_for_task(task_id))
        except ControlPlaneError:
            logger.exception("delegation: could not read the history of task %s", task_id)
            return ()
        handed: list[PreviousAttempt] = []
        for row in rows:
            if row.is_live:
                continue
            title, archive_path = self._chat_facts(row.chat_id)
            handed.append(
                PreviousAttempt(
                    state=row.state,
                    outcome=row.outcome,
                    summary=row.summary,
                    detail=row.detail,
                    created_at=row.created_at,
                    ended_at=row.ended_at,
                    chat_id=row.chat_id,
                    chat_title=title,
                    archive_path=archive_path,
                )
            )
            if len(handed) >= HANDOFF_ATTEMPTS:
                break
        return tuple(handed)

    def _record_log(
        self, workspace: str, attempt_id: str, *, chat: Any = None
    ) -> tuple[TaskDocument, TaskAttempt] | None:
        """Write one attempt's line into its task's log. Best effort; never raises.

        The write is the engine's, made as ``user`` the way the review flag is,
        and it moves the task's revision. The attempt follows that revision only
        when it was bound to the one this write read: a log line is not an edit to
        the task, but an edit the user made before it still is, and rebinding over
        it would erase "changed since delegated". One retry on a conflict, because
        the user saving the description at the same moment is the likely cause.
        """
        for _ in range(2):
            try:
                attempt = self._attempt_call(workspace, lambda store: store.get(attempt_id))
                document = self._task_call(workspace, lambda store: store.get(attempt.task_id))
                title, archive_path = self._chat_facts(attempt.chat_id, chat)
                item = render_item(
                    attempt_id=attempt.attempt_id,
                    state=attempt.state,
                    outcome=attempt.outcome,
                    summary=attempt.summary,
                    detail=attempt.detail,
                    created_at=attempt.created_at,
                    ended_at=attempt.ended_at,
                    chat_id=attempt.chat_id,
                    chat_title=title,
                    archive_path=archive_path,
                )
                body = upsert_item(document.body, attempt.attempt_id, item)
                if body == document.body:
                    return (document, attempt)
                written = self._task_call(
                    workspace,
                    lambda store: store.update(
                        attempt.task_id,
                        expected_revision=document.revision,
                        changes={},
                        body=body,
                        actor="user",
                    ),
                )
                if attempt.task_revision == document.revision:
                    attempt = self._attempt_call(
                        workspace,
                        lambda store: store.bind_revision(attempt.attempt_id, written.revision),
                    )
                return (written, attempt)
            except ControlPlaneError as exc:
                if exc.code == "task_revision_conflict":
                    continue
                logger.exception("delegation: could not log attempt %s in its task", attempt_id)
                return None
        logger.warning("delegation: attempt %s was not logged; the task kept changing", attempt_id)
        return None

    def workspace_task_report(
        self,
        workspace: str,
        task_id: str,
        *,
        outcome: str,
        summary: str,
        chat_id: str,
    ) -> dict[str, Any]:
        """Record the agent's report on the attempt that holds *task_id*.

        Only the chat working on the task may report on it: the report is that
        agent's word about its own work, and a report from any other chat would
        be one agent speaking for another. It never moves the task — how the card
        reads is decided when the turn ends — and it never completes it.
        """
        clean = str(task_id or "").strip()
        live = self._attempt_live(workspace, clean)
        if live is None or live.chat_id != chat_id:
            raise ControlPlaneError(
                "task_report_not_holder",
                "only the chat working on this task can report on it; this chat does "
                "not hold a live attempt for it.",
            )
        reported = self._attempt_call(
            workspace, lambda store: store.report(live.attempt_id, outcome, summary)
        )
        logged = self._record_log(workspace, reported.attempt_id)
        document = logged[0] if logged is not None else self._task_call(
            workspace, lambda store: store.get(clean)
        )
        if logged is not None:
            reported = logged[1]
        return _attempt_payload(reported, self._task_with_attempt(workspace, document))

    def _subscribe_chat_ended(self) -> None:
        """Follow archived and deleted chats, so a task is not left pointing at one."""
        subscribe = getattr(self.pcm, "on_chat_ended", None)
        if not callable(subscribe):
            return
        try:
            subscribe(self._on_chat_ended)
        except Exception:  # noqa: BLE001 — a missing hook is not a broken engine
            logger.exception("delegation: could not subscribe to archived chats")

    def _on_chat_ended(self, chat_id: str, chat: Any, how: str) -> None:
        """A delegated chat was archived or deleted: settle what it was holding.

        A result the agent called done is left for the user to approve — the
        transcript is in the vault and the chat opens read-only. Anything else the
        chat was holding cannot go on in it, so the attempt is settled
        ``interrupted`` with the reason; it keeps the task linked, and the card
        offers to continue in a new chat that is handed what this one did. Either
        way the task's log records where the transcript went. Never raises.
        """
        try:
            helper = getattr(chat, "helper", None)
            if not isinstance(helper, dict) or helper.get("kind") != "task_delegation":
                return
            project = self.pcm.get_project(str(getattr(chat, "project_id", "") or ""))
            workspace = str(getattr(project, "workspace", "") or "")
            task_id = str(helper.get("task_id") or "")
            if not workspace or not task_id:
                return
            live = self._attempt_call(workspace, lambda store: store.get_live(task_id))
            if live is None or live.chat_id != chat_id:
                return
            if live.state != "ready_for_review":
                self._attempt_call(
                    workspace,
                    lambda store: store.finish(
                        live.attempt_id, "interrupted", detail=f"the chat was {how}"
                    ),
                )
            self._record_log(workspace, live.attempt_id, chat=chat)
        except ControlPlaneError as exc:
            logger.info("delegation: %s chat %s left nothing to settle (%s)", how, chat_id, exc)
        except Exception:  # noqa: BLE001 — an archive must never fail on this
            logger.exception("delegation: could not settle the attempt for %s chat %s", how, chat_id)

    def _launch_turn(self, chat_id: str, prompt: str) -> tuple[Any, str]:
        """``start_stream`` with the default attendance: the stream, or ``(None, why)``.

        The single call site for a delegated turn, and the reason this block is
        small: ``unattended`` is never named here, so
        ``ProjectChatManager._effective_mode_for_chat`` cannot force ``bypass`` and
        an approval card raised by the turn surfaces as an ordinary Needs-you card.
        The recording fake in ``tests/test_task_delegation.py`` asserts the kwargs
        are empty, so a later "just this once" cannot add the flag quietly.

        The refusal's own text is returned rather than swallowed, because it is what
        the ``interrupted`` record's detail has to carry: "the turn could not be
        started" on its own tells the operator nothing about which turn or why.
        """
        try:
            return self.pcm.start_stream(chat_id, prompt), ""
        except Exception as exc:  # noqa: BLE001 — a failed launch is an outcome
            logger.exception("delegation: could not start the turn in chat %s", chat_id)
            return None, str(exc) or type(exc).__name__

    def _settle_interrupted(self, workspace: str, attempt_id: str, detail: str) -> None:
        """Record one attempt ``interrupted``, never raising.

        Called on the paths where the turn's outcome is already lost. Settling
        must not turn one recorded failure into an exception the caller sees as a
        different failure, so a store refusal here is logged and the interrupted
        state is left to the derivation the next read applies anyway.
        """
        try:
            self._attempt_call(
                workspace, lambda store: store.finish(attempt_id, "interrupted", detail=detail)
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: attempt %s could not be recorded as interrupted", attempt_id
            )
            return
        self._record_log(workspace, attempt_id)

    def _watch_turn(
        self, workspace: str, attempt_id: str, task_id: str, chat_id: str, stream: Any
    ) -> str:
        """Settle one attempt when its turn ends, and nothing else.

        A long task must not hold a file lock, and a settling write that failed
        while the user is reading their board is not worth a retry loop. So the
        watcher runs as a background asyncio task: it awaits the stream, then
        writes once. A crash before that write leaves ``running``, which the store
        derives as ``interrupted`` at the next read — the honest answer for a turn
        whose outcome nobody recorded.

        Four outcomes, and only one of them is a success the user has to see:
        ``ready_for_review`` when the turn ended with an answer,
        ``needs_you`` when it ended waiting on a question or an approval card,
        ``failed`` when the provider ended it in an error, and ``stopped`` when
        the user pressed Stop. None of them is ``done`` — that is the user's
        gesture through the same completion route as any other task.

        Idempotent per stream, so the announcement and explicit attach share
        one watcher while a new stream supersedes its predecessor. Two watchers on
        one stream would settle the same attempt twice and flag the same task for
        review twice, and a delegation that started the turn itself already
        announced that turn to :meth:`_on_chat_turn_started`.
        """
        key = (workspace, attempt_id)
        current = self._watching.get(key)
        if current is not None and current[0] is stream:
            return current[1]
        try:
            live = self._attempt_call(workspace, lambda store: store.get(attempt_id))
            document = self._task_call(workspace, lambda store: store.get(task_id))
            if not live.is_live or document.record.attempt_id != attempt_id:
                return ""
            self._attempt_call(workspace, lambda store: store.continue_turn(attempt_id))
            self._watching[key] = (stream, document.revision)
            if document.record.status == "in_review":
                # The conversation went on, so the result is not the one under
                # review any more: the card goes back to In progress.
                try:
                    cleared = self._task_call(
                        workspace,
                        lambda store: store.update(
                            task_id, expected_revision=document.revision,
                            changes={"status": "in_progress"}, actor="user",
                        ),
                    )
                    # Follow only our own write, not an edit the agent never received.
                    if live.task_revision == document.revision:
                        self._attempt_call(
                            workspace, lambda store: store.bind_revision(attempt_id, cleared.revision)
                        )
                    document = cleared
                except ControlPlaneError:
                    # A concurrent edit refuses the badge write, not the watcher.
                    # The running lifecycle still makes the old review inactionable.
                    logger.exception("delegation: could not retire review for task %s", task_id)
            self._schedule_watch(workspace, attempt_id, task_id, chat_id, stream, document.revision)
            return str(document.revision)
        except Exception:  # noqa: BLE001 — a watcher that never ran is recoverable
            logger.exception(
                "delegation: could not watch the turn for attempt %s", attempt_id
            )
            return ""

    def _subscribe_turn_watch(self) -> None:
        """Ask the chat manager to announce the turns a person starts.

        The one place the board learns that a delegated chat went on without it:
        the user answering an approval card in the composer, or typing a follow-up
        into a chat a **Send update** was just sent in. Both start a turn the
        delegation's own watcher knows nothing about, and both are the same event —
        "the conversation in this delegated chat continued" — so they are answered
        by one subscription rather than one special case each.

        Best effort and never fatal: a manager that cannot announce turns (a test
        fake, a legacy build) simply does not re-attach, and the paths that start
        a turn themselves still attach their own watcher.
        """
        subscribe = getattr(self.pcm, "on_turn_started", None)
        if not callable(subscribe):
            return
        try:
            subscribe(self._on_chat_turn_started)
        except Exception:  # noqa: BLE001 — a missing hook is not a broken engine
            logger.exception("delegation: could not subscribe to chat turn starts")

    def _delegated_chat(self, chat_id: str) -> tuple[str, str, str]:
        """``(workspace, task_id, attempt_id)`` for a chat this engine delegated.

        The provenance stamp is the only thing that says a chat belongs to a task —
        the title is prose a user can retype — and a chat whose stamp is absent,
        malformed or points at a chat this manager no longer holds is simply not a
        delegation to follow. Empty strings say so; nothing here is allowed to
        refuse a turn, because it runs inside the turn's own start.
        """
        chat = self.pcm.get_chat(chat_id)
        helper = getattr(chat, "helper", None)
        if not isinstance(helper, dict) or helper.get("kind") != "task_delegation":
            return ("", "", "")
        project = self.pcm.get_project(str(getattr(chat, "project_id", "") or ""))
        return (
            str(getattr(project, "workspace", "") or ""),
            str(helper.get("task_id") or ""),
            str(helper.get("attempt_id") or ""),
        )

    def _on_chat_turn_started(self, chat_id: str, stream: Any) -> None:
        """Re-attach a delegated attempt to the turn that has just begun in its chat.

        The conversation continuing is the user's own act, and the attempt on the
        card is the one whose chat that is: the badge should follow the chat, or a
        turn answered in the chat would leave the card describing a moment that has
        already moved on. The accepted turn becomes running before its watcher
        can settle, and :meth:`_settle_from_result` refuses an attempt that has
        stopped holding its task, so a watcher left over from a released attempt
        cannot flag a card another one now owns.

        Silently a no-op for a chat with nothing delegated to it, which is almost
        every chat, and for an attempt that is settled or released: continuing a
        dead turn is ``resume`` and replacing it is ``retry``, and neither is
        something a keystroke in the chat may do.
        """
        try:
            workspace, task_id, attempt_id = self._delegated_chat(chat_id)
            if not workspace or not task_id or not attempt_id:
                return
            live = self._attempt_call(workspace, lambda store: store.get_live(task_id))
            if live is None or live.chat_id != chat_id or live.attempt_id != attempt_id:
                return
        except ControlPlaneError as exc:
            # A stamp this plane cannot resolve is not a delegation to follow, and a
            # store read that failed is not a turn to break: both are logged and the
            # turn runs. The attempt's own state is still whatever it was, which is
            # the honest answer for a turn nothing recorded.
            logger.info("delegation: chat %s has no attempt to re-attach (%s)", chat_id, exc)
            return
        except Exception:  # noqa: BLE001 — a re-attach must never fail the turn
            logger.exception("delegation: could not re-attach the attempt for chat %s", chat_id)
            return
        self._watch_turn(workspace, attempt_id, task_id, chat_id, stream)

    def _schedule_watch(
        self,
        workspace: str,
        attempt_id: str,
        task_id: str,
        chat_id: str,
        stream: Any,
        task_revision: str,
    ) -> None:
        """Attach the settling coroutine to the running loop, or settle inline.

        There is one event loop in this engine, and this method is called from it
        (``start_stream`` creates an asyncio task and is only legal there), so the
        watcher is a sibling task on that loop. A test that calls the service
        outside a loop gets the settlement applied synchronously instead, which is
        the same state change without an await point.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop, so nothing can watch the turn. Settle it here rather than
            # leave it `running`: an attempt nobody is watching has no turn whose
            # outcome this engine is still waiting for, and the derivation would
            # say `interrupted` on the next read anyway.
            self._settle_interrupted(
                workspace, attempt_id, "no running loop to watch the turn"
            )
            return
        watcher = loop.create_task(
            self._await_turn(workspace, attempt_id, task_id, chat_id, stream),
            name=f"task-attempt-{attempt_id[:8]}",
        )
        # A strong reference, as the asyncio docs advise: the loop only keeps a
        # weak one, and a watcher that is garbage-collected mid-flight would drop
        # the coroutine that settles the attempt — leaving a turn that finished
        # recorded as `running`. The set is pruned on completion, so it holds the
        # watcher a turn is actually waiting on rather than every turn this process
        # ever delegated.
        self._watchers.add(watcher)
        # Cleanup belongs to this stream only: an older callback cannot remove
        # the current turn's marker after a finish/start interleaving.
        key = (workspace, attempt_id)
        self._watching[key] = (stream, task_revision)

        def _release(finished: asyncio.Task[None]) -> None:
            if self._is_current_turn(workspace, attempt_id, stream):
                self._watching.pop(key)
            self._watchers.discard(finished)

        watcher.add_done_callback(_release)

    def _is_current_turn(self, workspace: str, attempt_id: str, stream: Any) -> bool:
        current = self._watching.get((workspace, attempt_id))
        return current is not None and current[0] is stream

    async def _await_turn(
        self, workspace: str, attempt_id: str, task_id: str, chat_id: str, stream: Any
    ) -> None:
        """Consume the turn's stream and settle the attempt from its final turn once it ends."""
        result: dict[str, Any] = {}
        seen_result = False
        try:
            async for event in stream.subscribe():
                kind = event.get("type")
                if kind == "user_echo":
                    # A queued follow-up runs as a new turn on the same stream,
                    # announced by its own echo. The attempt is settled from the
                    # turn that ended the stream, so an earlier turn's result
                    # (a Stop, say) must not decide it (#1096).
                    result = {}
                    seen_result = False
                elif kind == "result":
                    result = dict(event)
                    seen_result = True
        except asyncio.CancelledError:
            # Shutdown. Left `running`, which the next process reads as
            # `interrupted`: the turn's fate is genuinely unknown and a
            # cancellation is not an outcome.
            raise
        except Exception:  # noqa: BLE001 — an unknown outcome is `interrupted`
            logger.exception("delegation: the turn for attempt %s failed to stream", attempt_id)
            if self._is_current_turn(workspace, attempt_id, stream):
                self._settle_interrupted(workspace, attempt_id, "the turn's outcome is unknown")
            return
        if not self._is_current_turn(workspace, attempt_id, stream):
            return
        if not seen_result:
            # A turn paused on a question card ends its stream without a result
            # (the drive loop stops the provider and waits for the answer), so a
            # chat with a question card up is a paused turn, not a lost one. A
            # permission card does not count: that pause keeps the stream open, and
            # the card stays saved after the stream ends until the answer is
            # confirmed, so here it can only be a leftover. A native (opencode)
            # question card does not count either, for the same reason.
            try:
                waiting = _chat_paused_on_question(self.pcm.get_chat(chat_id))
            except Exception:  # noqa: BLE001 — a chat store that cannot answer is unknown
                logger.exception("delegation: could not read chat %s after the turn", chat_id)
                waiting = False
            if waiting:
                self._settle_from_result(
                    workspace, attempt_id, task_id, chat_id, stream, {}, paused_on_question=True
                )
                return
            # The stream ended without a result event. Whatever the turn did — a
            # provider drop, a cancel the manager swallowed, a subscription that
            # closed early — this engine never saw an answer, so there is nothing
            # to review. `ready_for_review` here would put an empty result in front
            # of the user as a finished one, which is the one reading that cannot
            # be recovered from by looking at the chat.
            self._settle_interrupted(
                workspace, attempt_id, "the turn ended without a result"
            )
            return
        try:
            native_question = _chat_has_native_question(self.pcm.get_chat(chat_id))
        except Exception:  # noqa: BLE001 — a chat store that cannot answer is unknown
            logger.exception("delegation: could not read chat %s after the turn", chat_id)
            native_question = False
        self._settle_from_result(
            workspace, attempt_id, task_id, chat_id, stream, result,
            paused_on_question=native_question,
        )

    def _settle_from_result(
        self,
        workspace: str,
        attempt_id: str,
        task_id: str,
        chat_id: str,
        stream: Any,
        result: Mapping[str, Any],
        *,
        paused_on_question: bool = False,
    ) -> None:
        """Write the attempt's end state from the turn's own result event.

        A watcher can outlive the attempt it watches. The user can detach the task,
        stop the turn, or retry it while the provider is still winding down, and
        the stream this coroutine is reading ends afterwards either way — so every
        write here is conditional on this attempt still being the one that holds
        the task. A watcher that settled a task another attempt now owns would
        flag somebody else's work for review, and one that settled a detached task
        would put a badge on a card nobody delegated any more.

        ``paused_on_question`` is the caller's word that the turn ended paused on a
        question card (no result, see ``_await_turn``). It is also set when a native
        (opencode) question card is still saved after a result: that card blocks every
        new turn in the chat until it is answered, so the task must wait on the user
        rather than go to review. A permission card is not counted: it keeps the stream
        open while it is really up, and one still saved after a result is a leftover
        that would hold a finished, reported turn at ``needs_you`` (#1097).
        """
        try:
            stopped = bool(result.get("stopped"))
            errored = bool(result.get("is_error"))
            text = str(result.get("text") or "")
        except Exception:  # noqa: BLE001 — a malformed event is still just an outcome
            stopped, errored, text = False, False, ""
        try:
            live = self._attempt_call(workspace, lambda store: store.get_live(task_id))
            if live is None or live.attempt_id != attempt_id or live.chat_id != chat_id:
                return
        except ControlPlaneError:
            logger.exception("delegation: could not read attempt %s to settle it", attempt_id)
            return
        # The agent's own report decides what a turn that ended cleanly means
        # (#1064): only "done" is a result to review. A turn that ended with
        # nothing reported is not taken as finished — it waits on the user, and
        # the card calls it unfinished.
        if stopped:
            state, detail = "stopped", "the turn was stopped"
        elif errored:
            state, detail = "failed", text[:400] or "the turn ended in an error"
        elif paused_on_question:
            state, detail = "needs_you", ""
        elif live.outcome == "done":
            state, detail = "ready_for_review", ""
        elif live.outcome in ("blocked", "needs_input"):
            state, detail = "needs_you", ""
        else:
            state, detail = "needs_you", "the turn ended without a report from the agent"
        try:
            settled = self._attempt_call(
                workspace,
                lambda store: store.finish(attempt_id, state, detail=detail),
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: attempt %s ended as %s but could not be recorded",
                attempt_id,
                state,
            )
            return
        # A turn the agent reported done puts the task in front of the user for
        # review, which is the In review column (#1069). Only for a turn that
        # actually produced something to review: a `needs_you` attempt has no
        # result yet, so "review" on it would ask the user to review nothing.
        if settled.state == "ready_for_review":
            self._mark_review_ready(workspace, task_id, attempt_id)
        self._record_log(workspace, attempt_id)

    def _mark_review_ready(self, workspace: str, task_id: str, attempt_id: str) -> None:
        """Flag the task's result as waiting to be reviewed, and rebind the attempt.

        Two refusals this method is built around, and neither is cosmetic:

        * **The task must still belong to this attempt.** The ``in_review`` status
          is on the task record, so a watcher whose attempt has been detached or
          superseded would be writing to a card it no longer owns — flagging the
          new attempt's work for review, or a task nobody delegated any more.
        * **The badge write moves the task's revision, and the attempt has to
          follow it.** ``changed_since_delegated`` compares the revision the
          attempt was handed against the record now, so a review flag the watcher
          wrote itself would otherwise make *every* finished turn read "changed
          since delegated" — which is a warning that must mean the user edited the
          task under the agent, and there is no edit here.

        Best effort by design: a write that fails here must not turn a finished
        turn into an error, and the attempt's own state already says the result is
        waiting. A task the user edited out from under the turn is simply not
        flagged — the store refuses the edit shape and the log says why, rather
        than the watcher's failure becoming the user's error.
        """
        try:
            document = self._task_call(workspace, lambda store: store.get(task_id))
            if document.record.attempt_id != attempt_id:
                logger.info(
                    "delegation: task %s is linked to %s now, so attempt %s does not "
                    "flag it for review",
                    task_id,
                    document.record.attempt_id or "nothing",
                    attempt_id,
                )
                return
            if document.record.status == "in_review":
                return
            flagged = self._task_call(
                workspace,
                lambda store: store.update(
                    task_id,
                    expected_revision=document.revision,
                    changes={"status": "in_review"},
                    actor="user",
                ),
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: could not flag task %s for review after attempt %s",
                task_id,
                attempt_id,
            )
            return
        try:
            self._attempt_call(
                workspace,
                lambda store: store.bind_revision(attempt_id, flagged.revision),
            )
        except ControlPlaneError:
            logger.exception(
                "delegation: could not rebind attempt %s to the revision its review "
                "flag left behind",
                attempt_id,
            )

    def workspace_task_attempts(self, workspace: str, task_id: str) -> dict[str, Any]:
        """One task's whole attempt history, live attempt first.

        The live attempt plus every settled one, because a retry is a new row and
        the row before it is the record of what was tried.
        """
        clean = str(task_id or "").strip()
        document = self._task_call(workspace, lambda store: store.get(clean))
        history = self._attempt_call(
            workspace, lambda store: store.list_for_task(clean)
        )
        return {
            "task": self._task_with_attempt(workspace, document, include_body=True),
            "attempts": [attempt.to_dict() for attempt in history],
        }

    def workspace_task_send_update(
        self,
        workspace: str,
        task_id: str,
        attempt_id: str,
        *,
        expected_revision: str,
        message: str,
    ) -> dict[str, Any]:
        """Hand the edited task to the attempt's own chat, and rebind the attempt.

        The **Send update** gesture, and the two things it owes the card afterwards.
        Both matter, and the second is the one a composer send cannot do:

        1. **One ordinary message in the attempt's own chat.** Never a second
           delegation: that would mint a new attempt, re-quote the description from
           scratch and leave the result waiting for review looking abandoned. Sent
           through :meth:`_launch_turn`, so ``unattended`` is not named here either
           and an approval card the turn raises is an ordinary Needs-you card.
        2. **The rebind.** The attempt is re-stamped to the revision this message
           was made at, exactly as the delegation path re-stamps it after its
           linkage write. That is what makes ``changed_since_delegated`` return to
           ``false`` and the button retire instead of offering the same update for
           ever. **The order is the contract:** a send that was refused rebinds
           nothing, because a cleared flag over an update the agent never saw is the
           one thing this gesture must not be able to say.

        ``expected_revision`` is required and checked before anything is sent. The
        message claims to carry the task as it stands, and a description edited
        since the preview was read is not that — so a stale board is refused with
        the ordinary 409 and nothing goes to the agent.

        Only an attempt that still holds the task may be updated. A settled one is
        refused: continuing a turn that did not finish is ``resume`` and replacing
        it is ``retry``, and a keystroke in a chat is neither. An attempt id sent
        under another task's URL is ``task_attempt_not_found``, the same answer as
        one nobody has.

        **Queuing is the ordinary case, not an edge.** An edit made *under* a
        running agent is what this gesture exists for, so a turn is usually already
        in flight in that chat — and ``start_stream`` hands back a turn already
        running rather than dropping the message on the floor, which would leave the
        rebind claiming an update nobody received. So the message is queued into the
        running turn exactly as the composer queues it, and that turn runs it as its
        own follow-up and ends only afterwards: which is what settles the attempt
        with the answer to the update in it. A queued follow-up needs no watcher of
        its own; a turn this call started is attached through
        :meth:`_watch_turn` like any other.

        The message text travels with the request rather than being rebuilt here. It
        is an ordinary attended message the user composed and was shown in full
        before sending, so what is sent is what was approved; what this method
        checks is the revision it was made at.
        """
        clean_task = str(task_id or "").strip()
        clean_attempt = str(attempt_id or "").strip()
        text = str(message or "").strip()
        if not text:
            raise ControlPlaneError("invalid_task", "An update must carry a message.")
        document = self._task_call(workspace, lambda store: store.get(clean_task))
        if str(expected_revision or "").strip() != document.revision:
            raise ControlPlaneError(
                "task_revision_conflict",
                "the task changed since this update was planned; nothing was sent",
                retryable=True,
            )
        attempt = self._attempt_call(workspace, lambda store: store.get(clean_attempt))
        if attempt.task_id != clean_task:
            raise ControlPlaneError(
                "task_attempt_not_found",
                f"no delegation attempt {clean_attempt} on task {clean_task}",
            )
        if not attempt.is_live:
            raise ControlPlaneError(
                "invalid_action",
                f"attempt {attempt.attempt_id} is {attempt.state!r} and no longer holds "
                "this task; resume or retry it rather than sending it an update.",
            )
        if document.record.attempt_id != attempt.attempt_id:
            # The store's live set and the record's linkage disagree, so the task
            # belongs to some other attempt now. Answered like `resume` answers it:
            # the update is not the way to take a card back.
            raise ControlPlaneError(
                "task_revision_conflict",
                "the task is linked to a different attempt now; retry it instead.",
                retryable=True,
            )
        queued = bool(self.pcm.queue_message(attempt.chat_id, text))
        started: Any = None
        if not queued:
            active = self.pcm.get_active_stream(attempt.chat_id)
            stream, refusal = self._launch_turn(attempt.chat_id, text)
            if stream is None:
                raise ControlPlaneError(
                    "task_launch_failed",
                    f"the update could not be sent to the delegated chat ({refusal}); "
                    "nothing was sent, and the task still reads as changed since delegated.",
                    retryable=True,
                )
            if stream is active:
                # `start_stream` returned the turn already running instead of
                # starting one, which it does before it queues anything: the message
                # went nowhere. Refusing here is what keeps a rebind from claiming an
                # update the agent never saw.
                raise ControlPlaneError(
                    "task_update_busy",
                    "the delegated chat is finishing a turn and cannot take the update "
                    "yet; send it again once that turn has settled.",
                    retryable=True,
                )
            started = stream
        turn_revision = document.revision
        if started is not None:
            turn_revision = self._watch_turn(
                workspace, attempt.attempt_id, clean_task, attempt.chat_id, started
            ) or document.revision
            # Starting the continuation retires the previous review badge, a
            # revision-safe service write that the accepted update also carries.
            document = self._task_call(workspace, lambda store: store.get(clean_task))
        bound = self._attempt_call(
            workspace,
            lambda store: store.bind_revision(attempt.attempt_id, turn_revision),
        )
        return {
            # `bound`, for the same reason the delegation reply carries `bound`: the
            # answer's `changed_since_delegated` has to be the one this call just made
            # false, not the one the row carried a moment ago.
            **_attempt_payload(bound, self._task_with_attempt(workspace, document)),
            "updated": True,
            # Which of the two it was, so a caller can say what it did: the message is
            # accepted either way, but a queued one is still waiting behind a turn.
            "queued": queued,
        }

    async def workspace_task_attempt_action(
        self,
        workspace: str,
        attempt_id: str,
        action: str,
        *,
        task_id: str = "",
        actor: Actor = "user",
    ) -> dict[str, Any]:
        """One lifecycle gesture on one attempt: ``stop``, ``resume``, ``retry`` or ``detach``.

        ``task_id`` is the route's own path segment, and it is checked rather than
        ignored: the attempt id is 32 hex of the user's own, so a URL naming task A
        with task B's attempt would otherwise stop B's turn. A mismatch is
        ``task_attempt_not_found`` — the same answer as an id nobody has, because
        from this URL there is no such attempt.

        - ``stop`` ends the running turn. ``pcm.stop_chat`` is the manager's own
          Stop, and it is ``async``, so it is awaited: an un-awaited call would
          return a coroutine nobody runs, the provider turn would keep going, and
          the attempt would be recorded ``stopped`` over a turn still writing. The
          task keeps its linkage, so completion and reassignment stay refused until
          the user detaches. A Stop is not undoable, which is why this whole
          operation is annotated ``_DESTRUCTIVE`` on the agent surface.
        - ``resume`` continues the *same* chat with the *same* attempt: the
          attempt is recorded ``running`` before the turn, exactly as a delegation
          is, so a crash in this window is ``interrupted`` again rather than a
          duplicate turn. Only a settled attempt that did not finish is resumable —
          a ``ready_for_review`` result is waiting for the user's decision, and
          continuing it is the reviewer's call, not a retry.
        - ``retry`` starts a **new** attempt: a new attempt id, a new chat, the
          task re-linked, and the previous attempt left as history. That is the
          difference the pair exists to make — ``resume`` continues one chat,
          ``retry`` starts another attempt, and neither re-runs the first.
        - ``detach`` releases the task: the turn is stopped if it is running, the
          linkage is cleared, and a live attempt with nothing finished settles
          ``stopped``. An attempt that *has* finished — a ``ready_for_review``
          result — keeps the state it finished as: the release is about the task
          no longer belonging to it, not about how the turn ended, and rewriting
          the review the user is looking at to ``stopped`` would lose it. The
          attempt stays in history either way.

        ``actor`` is the caller, and nothing here completes a task: the user's own
        completion (see :meth:`workspace_task_complete_reviewed`) is what that
        gesture makes possible, and only a user can take it.
        """
        verb = str(action or "").strip()
        clean = str(attempt_id or "").strip()
        attempt = self._attempt_call(workspace, lambda store: store.get(clean))
        named_task = str(task_id or "").strip()
        if named_task and attempt.task_id != named_task:
            # The two ids disagree, so this URL names no attempt: answered like a
            # nonexistent one rather than acting on the attempt the caller named
            # under somebody else's card.
            raise ControlPlaneError(
                "task_attempt_not_found",
                f"no delegation attempt {clean} on task {named_task}",
            )
        if verb == "stop":
            return await self._attempt_stop(workspace, attempt, actor=actor)
        if verb == "resume":
            return self._attempt_resume(workspace, attempt, actor=actor)
        if verb == "retry":
            return self._attempt_retry(workspace, attempt, actor=actor)
        if verb == "detach":
            return await self._attempt_detach(workspace, attempt, actor=actor)
        raise ControlPlaneError(
            "invalid_action", "action must be stop, resume, retry, or detach."
        )

    def _attempt_task(self, workspace: str, attempt: TaskAttempt) -> TaskDocument:
        """The task record one attempt belongs to, or a typed not-found.

        An attempt whose task file was deleted is ``task_not_found``, not a
        success: there is no card left to stop, resume or detach, and reporting
        otherwise would draw a badge on a task that is not there.
        """
        document: TaskDocument = self._task_call(
            workspace, lambda store: store.get(attempt.task_id)
        )
        return document

    async def _stop_turn(self, chat_id: str) -> None:
        """Stop one chat's turn, awaiting the manager's own Stop, or noting its absence.

        ``ProjectChatManager.stop_chat`` is ``async``: it waits for the provider to
        acknowledge the interrupt before force-closing, and that wait is the whole
        of what makes a Stop a Stop. Called without awaiting it, the coroutine is
        never run and the turn keeps running while the attempt is recorded
        ``stopped`` over it.

        A manager with no ``stop_chat`` is not a refusal: the attempt still settles,
        and the turn's own end settles it again. Losing the manager would be the
        wrong trade for the record, so this is logged rather than raised.

        Queued follow-ups are parked on the chat rather than run (``park_queue``):
        a board Stop or Detach ends the delegated work, while a composer Stop
        does not (#1103).
        """
        stop = getattr(self.pcm, "stop_chat", None)
        if not callable(stop):
            logger.warning("delegation: the chat manager cannot stop chat %s", chat_id)
            return
        try:
            await stop(chat_id, park_queue=True)
        except Exception:  # noqa: BLE001 — a failed stop is not a failed gesture
            logger.exception("delegation: could not stop the turn in chat %s", chat_id)

    async def _attempt_stop(
        self, workspace: str, attempt: TaskAttempt, *, actor: Actor
    ) -> dict[str, Any]:
        """Stop the running turn and settle the attempt ``stopped``."""
        if not attempt.is_live:
            raise ControlPlaneError(
                "invalid_action",
                f"attempt {attempt.attempt_id} is already {attempt.state!r}; there is "
                "nothing running to stop.",
            )
        document = self._attempt_task(workspace, attempt)
        if attempt.state == "running":
            await self._stop_turn(attempt.chat_id)
        settled = self._attempt_call(
            workspace,
            lambda store: store.finish(
                attempt.attempt_id, "stopped", detail=f"stopped by the {actor}"
            ),
        )
        # The task comes back with the attempt, as it does for detach and resume.
        # A Stop leaves the linkage in place but still moves the record (the attempt
        # is settled), and the board draws the badge, Stop and Detach from that
        # record — so a reply without it would leave a card reading "Running" over
        # a turn the user just ended until they reloaded by hand.
        return _attempt_payload(settled, self._task_with_attempt(workspace, document))

    def _attempt_resume(
        self, workspace: str, attempt: TaskAttempt, *, actor: Actor
    ) -> dict[str, Any]:
        """Continue the same chat under the same attempt."""
        if attempt.state not in RESUMABLE_STATES:
            raise ControlPlaneError(
                "invalid_action",
                f"attempt {attempt.attempt_id} is {attempt.state!r}; only an attempt "
                "that did not finish can be resumed.",
            )
        document = self._attempt_task(workspace, attempt)
        if document.record.attempt_id != attempt.attempt_id:
            raise ControlPlaneError(
                "task_revision_conflict",
                "the task is linked to a different attempt now; retry it instead.",
                retryable=True,
            )
        chat = self.pcm.get_chat(attempt.chat_id)
        if chat is None or getattr(chat, "archived", False):
            # An archived chat's provider session has been reclaimed and a deleted
            # one is gone: there is no conversation left to continue. A new attempt
            # in a new chat, handed what this one did, is the way on.
            raise ControlPlaneError(
                "attempt_chat_archived",
                "this attempt's chat is "
                + ("deleted" if chat is None else "archived")
                + "; continue in a new chat instead (retry), which is handed what it did.",
            )
        prompt = build_resume_prompt(
            title=document.record.title,
            state=attempt.state,
            task_id=document.record.id,
            detail=attempt.detail,
        )
        running = self._attempt_call(
            workspace, lambda store: store.reopen(attempt.attempt_id)
        )
        stream, refusal = self._launch_turn(attempt.chat_id, prompt)
        if stream is None:
            self._settle_interrupted(
                workspace,
                attempt.attempt_id,
                f"the resumed turn could not be started ({refusal})",
            )
            raise ControlPlaneError(
                "task_launch_failed",
                f"the resumed turn could not be started ({refusal}); the attempt is "
                "recorded as interrupted.",
                retryable=True,
            )
        self._watch_turn(
            workspace, running.attempt_id, document.record.id, attempt.chat_id, stream
        )
        return {
            **_attempt_payload(
                running, self._task_with_attempt(workspace, document)
            ),
            "resumed": True,
        }

    def _attempt_retry(
        self, workspace: str, attempt: TaskAttempt, *, actor: Actor
    ) -> dict[str, Any]:
        """Start a new attempt for the same task, in a fresh chat.

        Delegation again, from the task's *current* revision: the previous
        attempt stays as history and its chat is left exactly as it is. That is
        the difference from :meth:`_attempt_resume`, which continues one chat.

        Refused while the attempt still holds the task, which is what an attempt
        that has not been released is: starting another one under a card an agent
        is still working on would be a second turn nobody asked for. A released
        attempt holds nothing, so retrying it is the ordinary delegation of a free
        task and goes through the same path.
        """
        if attempt.is_live:
            raise ControlPlaneError(
                "invalid_action",
                f"attempt {attempt.attempt_id} is {attempt.state!r}; stop or detach it "
                "before starting another.",
            )
        document = self._attempt_task(workspace, attempt)
        outcome = self.workspace_task_delegate(
            workspace,
            document.record.id,
            expected_revision=document.revision,
            project_id=None,
            actor=actor,
        )
        return {**outcome, "retried": True}

    def _release_attempt(self, workspace: str, attempt_id: str) -> TaskAttempt | None:
        """Mark one attempt released, or ``None`` if the store could not.

        Best effort, and never raising: both callers reach this *after* the task
        has been released on its own record — a detach the user performed, or a
        completion they approved — and reporting a failure for a write the gesture
        had already made would tell them the opposite of what happened. A store
        refusal is logged and the attempt keeps reading as live until the next
        read, which is recoverable; a gesture reported as failed that actually
        succeeded is not.
        """
        try:
            freed: TaskAttempt = self._attempt_call(
                workspace, lambda store: store.release(attempt_id)
            )
            return freed
        except ControlPlaneError:
            logger.exception(
                "delegation: attempt %s could not be recorded as released", attempt_id
            )
            return None

    async def _attempt_detach(
        self, workspace: str, attempt: TaskAttempt, *, actor: Actor
    ) -> dict[str, Any]:
        """Release the task from the attempt, stopping the turn first if needed.

        A finished attempt is left exactly as it finished. ``ready_for_review`` is
        the review the user is looking at, and rewriting it to ``stopped`` — as
        this used to, because ``ready_for_review`` is a live state — would throw
        away the one record saying how that turn ended, because the user released
        the card rather than because the turn failed. A settled attempt was never
        rewritten either: it does not hold the task, so there is nothing to stop
        and nothing to say.

        What a released attempt *is* marked with is the release itself, and it has
        to be: ``ready_for_review`` is a live state, so an attempt that keeps
        reading as one after the task is unlinked still answers ``get_live``, and
        the task it no longer belongs to can then never be delegated again. So a
        ``ready_for_review`` attempt is marked released once the unlink has landed
        — the order matters, because a release that failed to unlink would leave a
        live attempt nothing holds, and a live attempt nothing holds is the stuck
        state this exists to end.

        The card leaves In review with it: a task in that column with no attempt
        behind it asks the user to review a result they have already released, so
        it goes back to In progress here and nowhere else.
        """
        document = self._attempt_task(workspace, attempt)
        if attempt.state == "running":
            await self._stop_turn(attempt.chat_id)
        # Only a live state is rewritten, and `ready_for_review` is not one of the
        # states a detach may overwrite: it is the review the user is looking at.
        # `running` and `needs_you` have nothing finished to lose, so a detach
        # settles them; a result that already arrived is left as the record of how
        # the turn ended.
        if attempt.state in ("running", "needs_you"):
            self._attempt_call(
                workspace,
                lambda store: store.finish(
                    attempt.attempt_id, "stopped", detail=f"detached by the {actor}"
                ),
            )
        if document.record.attempt_id == attempt.attempt_id:
            document = self._task_call(
                workspace,
                lambda store: store.unlink(
                    document.record.id, expected_revision=document.revision
                ),
            )
            if document.record.status == "in_review":
                document = self._task_call(
                    workspace,
                    lambda store: store.update(
                        document.record.id,
                        expected_revision=document.revision,
                        changes={"status": "in_progress"},
                        actor="user",
                    ),
                )
        settled = self._attempt_call(workspace, lambda store: store.get(attempt.attempt_id))
        if settled.is_live:
            # Re-read rather than judged on the attempt passed in: `running` and
            # `needs_you` were just settled to `stopped`, so only a finished result
            # is left holding the task, and asking the store is what keeps the two
            # from disagreeing.
            freed = self._release_attempt(workspace, attempt.attempt_id)
            if freed is not None:
                settled = freed
        return _attempt_payload(settled, self._task_with_attempt(workspace, document))

    # ---- agent wrappers for delegation --------------------------------

    def task_delegate(
        self,
        principal: AgentPrincipal,
        task_id: str,
        *,
        expected_revision: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Hand a task in the calling chat's workspace to the agent.

        The agent may delegate a task it was asked to work on, but the turn it
        creates is an ordinary attended chat: approval cards surface as Needs-you
        and the task is completed by the user, never here.
        """
        return _ok(
            self.workspace_task_delegate(
                self._workspace(principal),
                task_id,
                expected_revision=expected_revision,
                project_id=project_id,
                actor="agent",
            )
        )

    async def task_attempt_action(
        self, principal: AgentPrincipal, attempt_id: str, action: str
    ) -> dict[str, Any]:
        """Stop, resume, retry or detach one delegation attempt in this workspace.

        ``async`` because ``stop`` and ``detach`` stop a real turn, and the chat
        manager's own Stop is a coroutine — awaiting it is the difference between
        ending a turn and dropping an un-run coroutine. ``_invoke`` awaits an
        awaitable result, so the envelope is unchanged for every caller.
        """
        return _ok(
            await self.workspace_task_attempt_action(
                self._workspace(principal), attempt_id, action, actor="agent"
            )
        )

    def task_report(
        self, principal: AgentPrincipal, task_id: str, *, outcome: str, summary: str
    ) -> dict[str, Any]:
        """Report how far this chat got on the task it was handed (#1064)."""
        return _ok(
            self.workspace_task_report(
                self._workspace(principal),
                task_id,
                outcome=outcome,
                summary=summary,
                chat_id=principal.chat_id,
            )
        )

    def task_list(self, principal: AgentPrincipal) -> list[dict[str, Any]]:
        """Every task in the calling chat's workspace.

        A list rather than an envelope on purpose: ``_invoke`` wraps a
        non-dict result as ``{"ok": true, "data": …}``, and a board is a list
        of rows.
        """
        return self.workspace_task_list(self._workspace(principal))

    def task_get(self, principal: AgentPrincipal, task_id: str) -> dict[str, Any]:
        """One task in the calling chat's workspace."""
        return _ok(self.workspace_task_get(self._workspace(principal), task_id))

    def task_create(
        self,
        principal: AgentPrincipal,
        *,
        title: str,
        body: str = "",
        project_id: str | None = None,
        due: str | None = None,
    ) -> dict[str, Any]:
        """File a task in the calling chat's workspace."""
        return _ok(
            self.workspace_task_create(
                self._workspace(principal),
                title=title,
                body=body,
                project_id=project_id,
                due=due,
            )
        )

    def task_update(
        self,
        principal: AgentPrincipal,
        task_id: str,
        *,
        expected_revision: str,
        changes: Mapping[str, object],
        body: str | None = None,
    ) -> dict[str, Any]:
        """Edit one task at the revision it was read at. The agent cannot mark it done."""
        return _ok(
            self.workspace_task_update(
                self._workspace(principal),
                task_id,
                expected_revision=expected_revision,
                changes=changes,
                body=body,
                actor="agent",
            )
        )

    def task_action(
        self,
        principal: AgentPrincipal,
        action: str,
        task_id: str,
        *,
        expected_revision: str,
        status: str | None = None,
        assignee: str | None = None,
        project_id: str | None = None,
        due: str | None = None,
    ) -> dict[str, Any]:
        """Move, complete or reassign one task; an agent completion is refused."""
        return _ok(
            self.workspace_task_action(
                self._workspace(principal),
                action,
                task_id,
                expected_revision=expected_revision,
                status=status,
                assignee=assignee,
                project_id=project_id,
                due=due,
                actor="agent",
            )
        )

    # ---- webhook triggers -------------------------------------------------
    #
    # Child A6 of #974: the management half of the webhook feature on the agent
    # surface. The store, the receiver and the dispatcher are A1–A4's
    # (``ciao/webhooks.py``, ``ciao/web/routes_hooks.py``,
    # ``ciao/webhook_dispatch.py``) and the session routes in
    # ``ciao/web/routes_webhooks.py`` are A2's; this block adds no store, route
    # or dispatch logic of its own. It is the same shape as the task block
    # above: ``workspace_webhook_*`` carries the workspace name a session route
    # would pass, and the ``webhook_*`` wrappers below scope it to the calling
    # chat's own workspace for the agent operations.
    #
    # Two properties this layer owns and the store does not. **The workspace is
    # resolved, not trusted**: an unregistered name is refused here, and a
    # trigger found by id has to belong to that workspace, so a trigger in
    # another workspace answers exactly like one that does not exist — the same
    # non-oracle the task surface keeps. And **the one-time secret is returned to
    # the caller that asked for it and nothing else**: the agent is the operator
    # acting through a shell, so the value belongs in the envelope, and it is
    # never written to a log, an error message or a ``repr``. The receiver is
    # the only other thing that ever sees it.

    def _registered_workspace(self, workspace: str) -> str:
        """One registered workspace's name, or ``workspace_not_found``.

        The same rule ``_require_registered_workspace`` applies in the session
        routes: the store validates the *shape* of a workspace name and nothing
        else, so refusing an unregistered one here is what keeps a trigger from
        being configured for a name that would never resolve to a live target.
        """
        name = str(workspace or "").strip()
        if not name or self.config.workspace(name) is None:
            raise ControlPlaneError(
                "workspace_not_found", f"Workspace '{name}' was not found."
            )
        return name

    def _webhook_store(self) -> WebhookStore:
        """The engine's trigger store, over the one place that builds the path.

        ``webhook_store`` is the routes' own constructor, imported rather than
        re-spelled: the archive hook, the receiver and this block must all be
        reading and revoking the same file.
        """
        return webhook_store(self.config)

    def _webhook_call(self, call: Callable[[WebhookStore], Any]) -> Any:
        """One store call, its typed refusal translated into ours.

        The single boundary, as ``_task_call`` is for the task store: no
        ``WebhookStoreError.code`` reaches a transport that
        ``_WEBHOOK_ERROR_CODES`` does not name.
        """
        try:
            return call(self._webhook_store())
        except WebhookStoreError as exc:
            raise _webhook_error(exc) from exc

    def _webhook_of(self, workspace: str, trigger_id: str) -> WebhookTrigger:
        """One trigger that belongs to *workspace*, or ``webhook_not_found``.

        A trigger in another workspace reads exactly like a nonexistent one:
        the id would otherwise be an existence oracle over other workspaces'
        triggers, and a managed chat could point at a foreign configuration.
        """
        clean = str(trigger_id or "").strip()
        trigger: WebhookTrigger = self._webhook_call(lambda store: store.get(clean))
        if trigger.workspace != workspace:
            raise ControlPlaneError(
                "webhook_not_found", f"Webhook trigger '{clean}' was not found."
            )
        return trigger

    def workspace_webhook_list(self, workspace: str) -> list[dict[str, Any]]:
        """One workspace's triggers, public records only.

        Never a verifier and never a secret: a stored record has no field that
        could carry one.
        """
        scope = self._registered_workspace(workspace)
        rows = self._webhook_call(lambda store: store.list(scope))
        return [row.to_dict() for row in rows]

    def workspace_webhook_create(
        self,
        workspace: str,
        *,
        name: str,
        instructions: str,
        project_id: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Store a new trigger in one workspace and return it with its secret.

        The trigger is stored **disabled**: configuring one is not making it
        callable, so an agent that creates a trigger for the user has not handed
        a stranger a working credential until ``workspace_webhook_update``
        enables it on purpose. ``project_id`` is stored as given — the store
        checks its shape and dispatch is what resolves it against the workspace
        — and ``mode`` is the trigger's own permission mode for the turn it
        launches, never a sender's choice. Which modes are legal is the store's
        rule, not this layer's: ``normal``, ``auto`` and ``plan``, anything else
        ``webhook_invalid``.
        """
        scope = self._registered_workspace(workspace)
        arguments: dict[str, Any] = {
            "name": str(name or ""),
            "workspace": scope,
            "instructions": str(instructions or ""),
        }
        if project_id is not None:
            arguments["project_id"] = project_id
        if mode is not None:
            arguments["mode"] = mode
        trigger, secret = self._webhook_call(
            lambda store: store.create(**arguments)
        )
        return {"trigger": trigger.to_dict(), "secret": secret}

    def workspace_webhook_update(
        self,
        workspace: str,
        trigger_id: str,
        *,
        expected_revision: str,
        name: str | None = None,
        instructions: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Edit one trigger of one workspace at the revision the caller read.

        Only the three fields the store can change are parameters: the target
        and the mode are not, because retargeting a trigger or changing the mode
        its events run under is a trust change rather than an edit. Omitting a
        field leaves it alone; ``enabled`` is the operator's off switch and it
        reaches an event already in the receipt journal too.
        """
        scope = self._registered_workspace(workspace)
        clean = self._webhook_of(scope, trigger_id)
        updated: WebhookTrigger = self._webhook_call(
            lambda store: store.update(
                clean.trigger_id,
                expected_revision=_webhook_revision(expected_revision),
                name=name,
                instructions=instructions,
                enabled=enabled,
            )
        )
        return updated.to_dict()

    def workspace_webhook_rotate(
        self, workspace: str, trigger_id: str, *, expected_revision: str
    ) -> dict[str, Any]:
        """Replace one trigger's secret and return it with the new one.

        Rotation **revokes on rotate**: the previous secret stops authorizing
        the moment this succeeds, so whoever else held it is refused from the
        next request on. It is the recovery for a lost or exposed secret, and it
        preserves ``enabled`` in both directions — rotating is not a way to
        enable a disabled trigger, and a secret is not consent to run anything.
        """
        scope = self._registered_workspace(workspace)
        clean = self._webhook_of(scope, trigger_id)
        trigger, secret = self._webhook_call(
            lambda store: store.rotate_secret(
                clean.trigger_id,
                expected_revision=_webhook_revision(expected_revision),
            )
        )
        return {"trigger": trigger.to_dict(), "secret": secret}

    def workspace_webhook_delete(
        self, workspace: str, trigger_id: str, *, expected_revision: str
    ) -> dict[str, Any]:
        """Delete one trigger of one workspace and destroy its verifier."""
        scope = self._registered_workspace(workspace)
        clean = self._webhook_of(scope, trigger_id)
        self._webhook_call(
            lambda store: store.delete(
                clean.trigger_id,
                expected_revision=_webhook_revision(expected_revision),
            )
        )
        return {"trigger_id": clean.trigger_id, "deleted": True}

    def webhook_list(self, principal: AgentPrincipal) -> list[dict[str, Any]]:
        """Every webhook trigger in the calling chat's workspace.

        A list rather than an envelope, as ``task_list`` is: ``_invoke`` wraps a
        non-dict result as ``{"ok": true, "data": …}``.
        """
        return self.workspace_webhook_list(self._workspace(principal))

    def webhook_create(
        self,
        principal: AgentPrincipal,
        *,
        name: str,
        instructions: str,
        project_id: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Configure a trigger in the calling chat's workspace; created disabled."""
        return _ok(
            self.workspace_webhook_create(
                self._workspace(principal),
                name=name,
                instructions=instructions,
                project_id=project_id,
                mode=mode,
            )
        )

    def webhook_update(
        self,
        principal: AgentPrincipal,
        trigger_id: str,
        *,
        expected_revision: str,
        name: str | None = None,
        instructions: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Edit one trigger at the revision it was read at."""
        return _ok(
            self.workspace_webhook_update(
                self._workspace(principal),
                trigger_id,
                expected_revision=expected_revision,
                name=name,
                instructions=instructions,
                enabled=enabled,
            )
        )

    def webhook_rotate(
        self, principal: AgentPrincipal, trigger_id: str, *, expected_revision: str
    ) -> dict[str, Any]:
        """Replace one trigger's secret; the old one stops working at once."""
        return _ok(
            self.workspace_webhook_rotate(
                self._workspace(principal),
                trigger_id,
                expected_revision=expected_revision,
            )
        )

    def webhook_delete(
        self, principal: AgentPrincipal, trigger_id: str, *, expected_revision: str
    ) -> dict[str, Any]:
        """Delete one trigger and its verifier, revision-checked."""
        return _ok(
            self.workspace_webhook_delete(
                self._workspace(principal),
                trigger_id,
                expected_revision=expected_revision,
            )
        )

    # ---- background command runs ----------------------------------------

    def _background_runner(self) -> Any:
        if self.background is None:
            raise ControlPlaneError(
                "unavailable",
                "Background command runs are not available on this server.",
                retryable=True,
            )
        return self.background

    def _background_payload(
        self, run: BackgroundRun, *, tail: list[str] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "label": run.label,
            "status": run.status,
            "exit_code": run.exit_code,
            "pid": run.pid,
            "cmd": list(run.cmd),
            "cwd": run.cwd,
            "timeout_s": run.timeout_s,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "error": run.error,
            "log_path": str(self._background_runner().log_path(run.run_id)),
        }
        if tail is not None:
            payload["last_lines"] = tail
        return payload

    def _owned_run(self, principal: AgentPrincipal, run_id: str) -> BackgroundRun:
        """Resolve a run the calling chat owns, or raise ``run_not_found``.

        A run belongs to exactly the chat that started it. A run owned by
        another chat reports as *not found* rather than *forbidden*: the run id
        is otherwise an existence oracle for work in chats the caller cannot
        see. The workspace check is defence in depth for a stale record whose
        chat has since moved.
        """
        value = (run_id or "").strip()
        if not value:
            raise ControlPlaneError("run_required", "run_id is required.")
        run: BackgroundRun | None = self._background_runner().get(value)
        if (
            run is None
            or run.parent_chat_id != principal.chat_id
            or (run.workspace and run.workspace != principal.workspace)
        ):
            raise ControlPlaneError("run_not_found", f"Run '{value}' was not found.")
        return run

    async def background_run_start(
        self,
        principal: AgentPrincipal,
        *,
        cmd: Any,
        cwd: str = "",
        env: dict[str, Any] | None = None,
        timeout_s: int = 1800,
        label: str = "",
    ) -> dict[str, Any]:
        """Start a command in a tracked background subprocess.

        The run is attributed to the calling chat and inherits its workspace,
        so ``background_run_status``/``_cancel`` from any other chat cannot see
        or touch it. Nothing here builds a shell command line: ``cmd`` is an
        argv list handed to ``create_subprocess_exec``.
        """
        chat, project = self._chat_scope(principal, "")
        workspace = self._workspace(principal, project.workspace)
        try:
            run: BackgroundRun = await self._background_runner().start_run(
                parent_chat_id=chat.chat_id,
                project_id=project.project_id,
                workspace=workspace,
                cmd=cmd,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
                label=label,
            )
        except BackgroundRunError as exc:
            raise ControlPlaneError(exc.code, str(exc)) from exc
        return _ok(self._background_payload(run))

    def background_run_status(
        self, principal: AgentPrincipal, run_id: str, lines: int = TAIL_LINES
    ) -> dict[str, Any]:
        run = self._owned_run(principal, run_id)
        tail = self._background_runner().tail(run.run_id, max(1, min(int(lines), 500)))
        return _ok(self._background_payload(run, tail=tail))

    async def background_run_cancel(
        self, principal: AgentPrincipal, run_id: str
    ) -> dict[str, Any]:
        run = self._owned_run(principal, run_id)
        try:
            updated: BackgroundRun = await self._background_runner().cancel(run.run_id)
        except BackgroundRunError as exc:
            raise ControlPlaneError(exc.code, str(exc)) from exc
        return _ok(
            self._background_payload(
                updated, tail=self._background_runner().tail(updated.run_id)
            )
        )

    # ---- schedules ----------------------------------------------------

    def _schedule_payload(self, entry: ScheduleEntry) -> dict[str, Any]:
        data = asdict(entry)
        next_run = compute_next_run(entry)
        data["next_run"] = next_run.isoformat() if next_run else None
        # Name the target project here so every schedule tool reports it the
        # same way; enriching at the call site left it missing from list/update.
        if entry.web_project_id:
            project = self.pcm.get_project(entry.web_project_id)
            data["project_name"] = getattr(project, "name", "") or ""
        return data

    def schedules_list(self, principal: AgentPrincipal) -> dict[str, Any]:
        workspace = self._workspace(principal)
        rows = [
            self._schedule_payload(entry)
            for entry in self.schedules.list_entries()
            if not entry.workspace or entry.workspace == workspace
        ]
        return _ok(rows)

    def schedule_preview(self, principal: AgentPrincipal, **values: Any) -> dict[str, Any]:
        """Validate schedule fields and resolve workspace/project targets.

        ``project_id`` defaults to the caller's active project (same as
        ``chat_create``) so MCP-created schedules land in the chat's
        workspace+project instead of an unscoped Personal fallback.

        An explicit ``workspace`` may only restate the principal's own. Unlike a
        chat turn, a schedule is auto-approved model input that persists a
        prompt and runs unattended — and unattended dispatch forces the target
        chat into bypass — so honoring a foreign workspace would let an
        injected or compromised managed chat execute there with that
        workspace's guide, integrations, and filesystem authority, without
        operator approval. Giving a second workspace an automation is operator
        business, done from a chat scoped to it.

        A schedule binds to a project (+ workspace), never to a chat: a
        ``chat_id`` is refused outright (issue #407).
        """
        # One boundary for explicit and omitted targets alike: ``_workspace``
        # refuses any name other than the principal's own and still validates
        # that the scoped workspace exists.
        workspace = self._workspace(principal, str(values.get("workspace") or ""))

        chat_ref = values.get("chat_id")
        project_ref = values.get("project_id")

        # A schedule binds to a project (+ workspace), never to a chat. A
        # chat-bound run posts into one conversation forever: runs are invisible
        # unless the operator watches that chat, and an archived target resumes
        # a reclaimed provider session and fails silently on every later run
        # (issue #407). A project run opens a fresh chat each time, which is
        # also what makes runs visible in the sidebar.
        if chat_ref:
            raise ControlPlaneError(
                "chat_binding_unsupported",
                "Schedules bind to a project, not a chat. Pass project_id "
                "(each run then opens a fresh chat in it, visible in the "
                "sidebar) — chat_id bindings fail silently once their target "
                "chat is archived.",
            )

        # Inherit the active project when omitted — preferred for vault-aware
        # automation and keeps the schedule in the same workspace as this chat.
        project = self._resolve_project(principal, project_ref)

        web_project_id: str | None = None
        if project is not None:
            web_project_id = project.project_id
            # Re-stamp from the resolved project (same as the HTTP route).
            workspace = self._workspace(principal, project.workspace)

        frequency = str(values.get("frequency") or "weekly")
        if frequency not in FREQUENCIES:
            raise ControlPlaneError(
                "invalid_frequency",
                f"frequency must be one of {', '.join(sorted(FREQUENCIES))}.",
            )
        # Interval cadence needs minutes, not a time of day.
        interval_minutes = 0
        if frequency == INTERVAL_FREQUENCY:
            # `or DEFAULT` would rescue a rejected 0 into a valid cadence, so
            # only an absent value falls back.
            supplied_interval = values.get("interval_minutes")
            if supplied_interval is None:
                supplied_interval = DEFAULT_INTERVAL_MINUTES
            try:
                interval_minutes = normalize_interval_minutes(supplied_interval)
            except ValueError as exc:
                raise ControlPlaneError("invalid_interval", str(exc)) from exc

        now = datetime.now(UTC).isoformat(timespec="seconds")
        entry = ScheduleEntry(
            schedule_id="preview",
            daily_time_utc=str(values.get("daily_time") or values.get("daily_time_utc") or "09:00"),
            prompt=str(values.get("prompt") or "preview"),
            chat_id=0,
            created_at=now,
            model=str(values.get("model") or ""),
            provider=str(values.get("provider") or ""),
            # "" means inherit, exactly as an empty model/provider does: the
            # permission mode is resolved from the operator's per-provider pin
            # at dispatch. The MCP schedule tool exposes no `mode` at all, so
            # the old "auto" default was an unconditional override that made the
            # dispatcher's inheritance dead for every model-created routine.
            mode=str(values.get("mode") or ""),
            timezone_name=str(values.get("timezone") or values.get("timezone_name") or "UTC"),
            days_of_week=list(values.get("days_of_week") or []),
            frequency=frequency,
            interval_minutes=interval_minutes,
            day_of_month=values.get("day_of_month"),
            run_at_date=values.get("run_at_date"),
            web_chat_id=None,
            web_project_id=web_project_id,
            web_project_name=getattr(project, "name", "") if project is not None else "",
            workspace=workspace,
            archive_policy=str(values.get("archive_policy") or "manual"),
            title=str(values.get("title") or ""),
        )
        # Same gate `schedule_update` applies, on the create door. A model
        # emitting `daily_time: "9:30"` (no leading zero) or "25:00" otherwise
        # got a stored entry that `tick()` compares against "%H:%M" and never
        # matches — reported as healthy by `next_run` and silently dead.
        time_error = wall_clock_time_error(entry)
        if time_error:
            raise ControlPlaneError("invalid_time", time_error)
        return _ok(self._schedule_payload(entry))

    def schedule_create(self, principal: AgentPrincipal, **values: Any) -> dict[str, Any]:
        preview = self.schedule_preview(principal, **values)["data"]
        entry = self.schedules.create(
            daily_time_utc=preview["daily_time_utc"],
            prompt=preview["prompt"],
            model=preview["model"],
            provider=preview["provider"],
            mode=preview["mode"],
            chat_id=0,
            timezone_name=preview["timezone_name"],
            days_of_week=preview["days_of_week"],
            frequency=preview["frequency"],
            interval_minutes=preview["interval_minutes"],
            day_of_month=preview["day_of_month"],
            run_at_date=preview["run_at_date"],
            web_chat_id=preview["web_chat_id"],
            web_project_id=preview["web_project_id"],
            web_project_name=preview["web_project_name"],
            workspace=preview["workspace"],
            archive_policy=preview["archive_policy"],
            title=preview["title"],
            description=str(values.get("description") or ""),
        )
        # Where a chat-bound entry re-homes once its chat is deleted; only
        # capturable while that chat still exists. See stamp_fallback_project.
        if stamp_fallback_project(entry, self.pcm):
            self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok(self._schedule_payload(entry))

    def _schedule(self, principal: AgentPrincipal, schedule_id: str) -> ScheduleEntry:
        entry = next((item for item in self.schedules.list_entries() if item.schedule_id == schedule_id), None)
        if entry is None:
            raise ControlPlaneError("schedule_not_found", f"Schedule '{schedule_id}' was not found.")
        if entry.workspace and entry.workspace != principal.workspace:
            raise ControlPlaneError("workspace_forbidden", "Schedule belongs to another workspace.")
        resolved: ScheduleEntry = entry
        return resolved

    def schedule_update(self, principal: AgentPrincipal, schedule_id: str, **changes: Any) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" and any(key not in {"enabled", "workspace", "model", "provider", "archive_policy", "provider_model", "archivePolicy"} for key in changes):
            raise ControlPlaneError("system_schedule_read_only", "System schedules only allow enabled/workspace/model/provider/archive_policy changes.")
        # Where this schedule's run bindings actually live: the bound project's
        # own workspace when there is one (an entry written before the workspace
        # field existed carries none of its own), else the stored field.
        bound = self.pcm.get_project(entry.web_project_id) if entry.web_project_id else None
        origin = str(getattr(bound, "workspace", "") or entry.workspace or principal.workspace)
        # Settle the destination workspace *before* resolving the project, and
        # validate it here: dispatch-time routing and provider/model inheritance
        # both hang off this field, so silently storing garbage would strand the
        # schedule. The destination is scoped like every other tool too: an
        # unattended run executes its prompt in bypass inside the target
        # workspace, so a move there is operator business, not something a
        # managed chat can talk a token into.
        if changes.get("workspace") is not None:
            target = self._workspace(principal, str(changes["workspace"]).strip().lower())
            changes["workspace"] = target
        else:
            target = origin

        project: Any | None = None
        if changes.get("chat_id") is not None:
            # Same stance ``schedule_preview`` draws on create (issue #407): a
            # schedule binds to a project, never to a chat — a chat-bound run
            # posts into one conversation forever and fails silently once that
            # chat is archived.
            raise ControlPlaneError(
                "chat_binding_unsupported",
                "Schedules bind to a project, not a chat. Pass project_id "
                "(each run then opens a fresh chat in it, visible in the "
                "sidebar) — chat_id bindings fail silently once their target "
                "chat is archived.",
            )
        if changes.get("project_id"):
            # Resolve the reference inside the *destination* workspace, exactly
            # as ``schedule_preview`` does. Going through
            # ``_resolve_project_id``/``_project`` searched and authorized
            # against the caller's own workspace instead, so
            # ``workspace="work"`` plus a project that lives in `work` failed
            # with project_not_found/workspace_forbidden before the new
            # workspace was ever considered — cross-workspace targeting worked
            # on create but never on update.
            project = self._resolve_project_in_workspace(
                principal, str(changes["project_id"]), target
            )
            changes["project_id"] = project.project_id
            # Keep workspace aligned with the new target (same as the HTTP API).
            changes.setdefault("workspace", project.workspace)
            # A legacy chat-bound row re-pointed at a project drops the chat
            # binding; set on `normalized` below, whose None values survive
            # (unlike `changes`, filtered above).
        aliases = {"daily_time": "daily_time_utc", "timezone": "timezone_name", "chat_id": "web_chat_id", "project_id": "web_project_id"}
        normalized = {aliases.get(key, key): value for key, value in changes.items() if value is not None}
        if project is not None:
            # The recorded name is what survives per-instance project-id
            # regeneration (dispatch re-homes by it), so leaving the previous
            # project's name behind would let a moved schedule silently re-home
            # onto the wrong project.
            normalized["web_project_name"] = project.name
            # A legacy chat-bound row re-pointed at a project must drop the
            # chat binding: keeping both leaves web_chat_id in control of
            # dispatch-time inheritance (schedule_effective_routing reads
            # provider/model off the old chat) while prepare_schedule_chat
            # creates each run in the project — so runs land in fresh chats
            # with the old chat's engine, and behavior flips again when that
            # chat later disappears. The workspace-move branch below clears
            # web_chat_id only when target != origin; supplying a project
            # target is the same migration regardless of workspace.
            if entry.web_chat_id:
                normalized["web_chat_id"] = None
        if target != origin:
            # Reassigning the workspace has to re-point the run target too:
            # dispatch prioritises web_chat_id/web_project_id and never checks
            # them against entry.workspace, so a bare ``workspace="work"``
            # update left the schedule listed under `work` while every
            # unattended run still created chats in — or posted into — the old
            # workspace's project/chat: a silent cross-workspace write.
            if normalized.get("web_chat_id"):
                # Same boundary ``schedule_create`` draws: a chat binding cannot
                # cross workspaces — and cannot exist at all on this surface,
                # which refuses ``chat_id`` outright — so this only fires for a
                # legacy row that was chat-bound before that stance. Point the
                # caller at project_id instead of accepting the move.
                raise ControlPlaneError(
                    "chat_binding_unsupported",
                    "This schedule is bound to a chat in its current workspace; "
                    "chat bindings are no longer supported. Pass project_id to "
                    "choose where its runs land.",
                )
            if entry.web_chat_id or entry.web_project_id:
                normalized["web_chat_id"] = None
                if "web_project_id" not in normalized:
                    if entry.scope == "system":
                        # System rows persist only SYSTEM_STATE_FIELDS and
                        # resolve their project from the packaged definition
                        # plus the workspace at dispatch, so clearing suffices.
                        normalized["web_project_id"] = None
                        normalized["web_project_name"] = ""
                    else:
                        # A user schedule with neither binding is skipped
                        # outright at dispatch ("Schedule has no web target"),
                        # so a bare move still has to name a destination: the
                        # target workspace's General project, which is what an
                        # unqualified cross-workspace target means.
                        general = next(
                            (
                                item for item in self.pcm.list_projects(target)
                                if item.name == "General"
                            ),
                            None,
                        )
                        if general is None:
                            raise ControlPlaneError(
                                "project_required",
                                f"Workspace '{target}' has no General project to move this "
                                "schedule into; pass project_id naming a project in it.",
                            )
                        normalized["web_project_id"] = general.project_id
                        normalized["web_project_name"] = general.name
        known = set(ScheduleEntry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown schedule fields: {', '.join(unknown)}")
        if "frequency" in normalized and normalized["frequency"] not in FREQUENCIES:
            raise ControlPlaneError(
                "invalid_frequency",
                f"frequency must be one of {', '.join(sorted(FREQUENCIES))}.",
            )
        if "interval_minutes" in normalized:
            try:
                normalized["interval_minutes"] = normalize_interval_minutes(
                    normalized["interval_minutes"]
                )
            except ValueError as exc:
                raise ControlPlaneError("invalid_interval", str(exc)) from exc
        updated = replace(entry, **normalized)
        # Switching to interval without naming a cadence would leave 0 stored,
        # which interval_delta floors to one minute -- far faster than asked.
        if is_interval(updated) and not updated.interval_minutes:
            updated.interval_minutes = DEFAULT_INTERVAL_MINUTES
        # The mirror of the REST route's guard. Moving an interval entry (or a
        # migrated loop) to a wall-clock cadence leaves daily_time_utc empty,
        # and compute_next_run cannot parse it -- the automation would report
        # as enabled and never dispatch.
        time_error = wall_clock_time_error(updated)
        if time_error:
            raise ControlPlaneError("invalid_time", time_error)
        # A changed chat/project binding moves where this entry re-homes.
        stamp_fallback_project(updated, self.pcm)
        self.schedules.replace(updated)
        publish_automations_changed(self.pcm)
        return _ok(self._schedule_payload(updated))

    _RUN_REFUSALS: dict[str, tuple[str, str]] = {
        "busy": (
            "schedule_busy",
            "The target chat has a turn in flight; retry when it finishes.",
        ),
        "missing-chat": (
            "schedule_target_missing",
            "The target chat no longer exists and could not be re-homed.",
        ),
    }

    def _raise_if_run_refused(self, result: dict[str, Any]) -> dict[str, Any]:
        """Turn a non-dispatching ``dispatch_now`` outcome into an error.

        An interval entry refuses rather than queues, and reports that through
        ``status`` rather than by raising. Wrapping it in ``_ok`` told the
        model the run had started when nothing was dispatched — the REST twin
        answers 409 for exactly these two, so the tool surface has to agree.
        """
        refusal = self._RUN_REFUSALS.get(str(result.get("status") or ""))
        if refusal is not None:
            raise ControlPlaneError(refusal[0], refusal[1])
        return result

    async def schedule_run(self, principal: AgentPrincipal, schedule_id: str) -> dict[str, Any]:
        self._schedule(principal, schedule_id)
        result = self._raise_if_run_refused(await self.schedules.dispatch_now(schedule_id))
        publish_automations_changed(self.pcm)
        return _ok(result)

    def schedule_delete(self, principal: AgentPrincipal, schedule_id: str) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" or not entry.removable:
            raise ControlPlaneError("schedule_not_removable", "This schedule cannot be removed.")
        deleted = self.schedules.delete(schedule_id)
        publish_automations_changed(self.pcm)
        return _ok({"deleted": deleted, "schedule_id": schedule_id})

    # ---- workspace files/assets ---------------------------------------

    def workspace_file_read(self, principal: AgentPrincipal, path: str) -> dict[str, Any]:
        root = Path(self.config.workspace_root).resolve()
        target = self._safe_relative(root, path, must_exist=True)
        if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
            raise ControlPlaneError("unsupported_file", "File must be a text file no larger than 2 MiB.")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ControlPlaneError("binary_file", "Binary files are not returned through MCP.") from exc
        return _ok({"path": target.relative_to(root).as_posix(), "content": content})

    def workspace_file_write(self, principal: AgentPrincipal, path: str, content: str) -> dict[str, Any]:
        root = Path(self.config.workspace_root).resolve()
        target = self._safe_relative(root, path)
        if target.is_relative_to(Path(self.config.state_path).parent.resolve()):
            raise ControlPlaneError("runtime_file_forbidden", "Runtime stores must be changed through their domain tools.")
        if len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise ControlPlaneError("file_too_large", "File exceeds the 2 MiB MCP write limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="")
        return _ok({"path": target.relative_to(root).as_posix(), "size": len(content.encode('utf-8'))})

    def file_surface(self, principal: AgentPrincipal, path: str) -> dict[str, Any]:
        """Validate a workspace file exists so the PWA can open it in the pinned
        preview panel. The actual surfacing happens client-side, keyed off this
        tool call showing up in the turn's trace — see extract_file_touches in
        ciao/web/chat_broker.py. Pin delivery does not read either field below;
        neither one proves the panel opened or failed to open.

        ``viewers`` is how many `/ws/chat/{chat_id}` sockets are open for this
        chat right now, from the connection tracker. It reflects real client
        presence and is independent of whether a turn is streaming.

        ``stream_state`` is ``"active"`` when a turn is currently streaming for
        this chat, or ``"none"`` otherwise. It says nothing about whether a
        client is attached to that turn.

        On a ``file_not_found`` miss the error carries ``error.suggestions`` —
        up to three nearest existing paths under the workspace root, ranked by
        basename similarity to the requested path — so the caller can offer a
        correction instead of guessing.
        """
        workspace = self._workspace(principal)
        # Root at the principal's own agent root, not the install root: sibling
        # workspaces live beside it, and both the existence check and the
        # suggestion walk would otherwise read across the workspace boundary.
        # Pre-reroot this resolves to the install root itself, so nothing
        # changes there; post-reroot it is the workspace's own directory.
        root = Path(self.config.agent_root(workspace)).resolve()
        try:
            target = self._safe_relative(root, path, must_exist=True)
        except ControlPlaneError as exc:
            if exc.code == "file_not_found":
                suggestions = self._file_surface_suggestions(root, path)
                if suggestions:
                    exc.suggestions = suggestions
            raise
        if not target.is_file():
            raise ControlPlaneError("unsupported_file", "Only an existing file can be surfaced.")
        viewers, stream_state = self._file_surface_signal(principal.chat_id)
        return _ok(
            {
                "path": target.relative_to(root).as_posix(),
                "viewers": viewers,
                "stream_state": stream_state,
            }
        )

    def _file_surface_suggestions(self, root: Path, requested: str) -> list[str]:
        """Nearest existing paths under ``root`` for a missing ``file_surface`` path.

        Ranks every file under the workspace root by basename-stem similarity to
        the requested path and returns the top three relative paths, so the
        caller can offer a concrete correction when a surfaced path did not exist.
        """
        wanted_stem = Path(requested).stem.lower()
        candidates: list[tuple[int, str]] = []
        # Walk with `os.walk` so heavy/hidden subtrees are pruned *before* they
        # are descended into (``rglob`` would still traverse them), keeping the
        # scan bounded on the event loop. Any OSError mid-walk degrades to the
        # candidates collected so far rather than replacing the intended
        # file_not_found with an unrelated error.
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SUGGESTION_SKIP_DIRS and not d.startswith(".")
                ]
                for name in filenames:
                    stem = os.path.splitext(name)[0].lower()
                    score = _suggestion_score(wanted_stem, stem)
                    if score is None:
                        continue
                    rel = Path(os.path.relpath(os.path.join(dirpath, name), root)).as_posix()
                    candidates.append((score, rel))
        except OSError:
            logger.debug("file_surface suggestions: workspace walk failed", exc_info=True)
        candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return [path for _score, path in candidates[:3]]

    def _file_surface_signal(self, chat_id: str) -> tuple[int, str]:
        """Client-presence and stream-state signal for ``file_surface``.

        ``viewers`` used to be ``ChatStream.subscriber_count``, a value
        scoped to one turn, sampled once, to answer a question scoped to one
        connection ("is a client attached to this chat"). That mismatch made
        it flaky by construction, not just wrong on one code path:

        - Every turn boundary has a real gap. `_attach_streams`
          (ciao/web/routes_chat.py) polls the broker for a new stream every
          `_ATTACH_POLL_SECONDS` (0.5s, routes_chat.py:57), so a healthy,
          fully-attached client reads 0 subscribers on the new stream for up
          to half a second after it is registered, before flipping to 1 with
          no state change on the client's end. Observed in production: two
          `file_surface` calls minutes apart on one unbroken connection
          returned 0, 0, then a third returned 1 — consistent with sampling
          landing in that gap twice, then past it.
        - A worse, longer-lived version of the same mismatch: a client can be
          stuck relaying a superseded `ChatStream` that was replaced in the
          broker without `finish()` being called on it, because
          `_attach_streams` only re-polls after its current stream forward
          returns (see the orphaned-stream note on `ChatStreamBroker.register`,
          ciao/web/chat_broker.py). That client shows 0 subscribers on the
          new stream indefinitely, not just for one poll window.

        Both are symptoms of the same design error: a per-turn object cannot
        answer a per-connection question. Counting live `/ws/chat/{chat_id}`
        sockets via the connection tracker fixes this structurally — the
        socket's lifetime already matches the question being asked, so
        neither turn boundaries nor broker replacement can make it flicker.
        """
        viewers = 0
        if chat_id and self.connection_tracker is not None:
            viewers = self.connection_tracker.chat_client_count(chat_id)
        stream_state = "none"
        if chat_id:
            try:
                stream = self.pcm.get_active_stream(chat_id)
            except Exception:
                stream = None
            if stream is not None:
                stream_state = "active"
        return viewers, stream_state

    # ---- adversarial review ---------------------------------------------

    async def sync_skills(self, principal: AgentPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.sync_skills import sync_workspace_skills

        result = await asyncio.to_thread(
            sync_workspace_skills,
            self.config.workspace_root,
        )
        return _ok(asdict(result))
