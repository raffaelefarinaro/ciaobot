"""Project + chat hierarchy manager for the PWA."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import mimetypes
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

if TYPE_CHECKING:
    from ciao.mcp_server import CiaoMcpService

RESTART_DRAIN_MESSAGE = (
    "Ciaobot is waiting for active chats to finish before restarting"
)
_FILE_REF_PREFIX = "ciao-drop:"
_FILE_REF_TTL_SECONDS = 10 * 60
_FILE_REF_MAX_ENTRIES = 256
_FILE_REF_PATTERN = re.compile(r"ciao-drop:(drop_[0-9a-f]{32})")


class RestartDrainingError(RuntimeError):
    """Raised when a new turn is rejected because a server restart is draining."""

    def __init__(self, message: str = RESTART_DRAIN_MESSAGE) -> None:
        super().__init__(message)


class AgentSurfaceUnavailableError(RuntimeError):
    """Raised when a turn cannot be built because the control plane is down.

    The Ciaobot agent surface is the only agent-facing control surface, so
    there is nothing to degrade to: a turn dispatched without it would run an
    agent that cannot see or change anything in Ciaobot. ``_drive``'s error
    handler publishes this as a normal failed turn, so the user sees why
    instead of getting a silently crippled answer.
    """


class UnknownModelError(ValueError):
    """A model id that is not in the configured set.

    Raised by ``ProjectChatManager._validate_configured_model``. Distinct
    from the other ``ValueError``s ``create_chat`` raises (unknown provider,
    bucket, control surface) so the MCP tool boundary can translate only
    the model failure to ``invalid_model`` and leave the rest as
    ``invalid_request`` (#259).
    """

try:  # pragma: no cover - Ciaobot targets Unix; fallback keeps imports portable.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

import yaml

from ciao import job_runs, subagent_tracking
from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV
from ciao.archive_jobs import ArchiveJob
from ciao.config import (
    CLAUDE_MODELS,
    GWS_DEFAULT_PROFILE,
    MAX_IMAGE_SIZE_BYTES,
    BridgeConfig,
)
from ciao.context.capsule import (
    build_context_capsule,
)
from ciao.context.capsule import (
    context_digest as stable_context_digest,
)
from ciao.model_tiers import is_tier
from ciao.models import (
    THINKING_LEVELS,
    AgentRequest,
    BridgeMode,
    ChatContext,
    ImageAttachment,
    PermissionRequestEvent,
    StreamEvent,
    ToolUseEvent,
)
from ciao.provider_service import ProviderService, capabilities_for, supported_providers
from ciao.providers.claude import get_session_info
from ciao.providers.opencode import OpencodeProvider, QuestionResponseResult
from ciao.schedules import ScheduleEntry, ScheduleStore
from ciao.sessions import StateStore
from ciao.subagent_tracking import SubagentInfo
from ciao.transcripts import (
    TranscriptStore,
    TurnJournal,
    _claude_projects_dir,
    _global_session_matches,
)
from ciao.web import chat_service
from ciao.web.chat_broker import (
    ChatStream,
    ChatStreamBroker,
    EventsHub,
    edit_pending_list,
    remove_pending_list,
    reorder_pending_list,
)
from ciao.web.archive_pipeline import ArchivePipeline
from ciao.web.chat_streaming import ChatStreaming
from ciao.web.chat_streaming import StreamOutcome as _StreamOutcome
from ciao.web.memory_pass import MemoryPassCoordinator, is_memory_pass_chat
from ciao.web.schedule_dispatch import ScheduleDispatcher
from ciao.web.document_conversion import convert_document, is_anydoc_document
from ciao.web.file_snapshots import SnapshotStore
from ciao.web.subagent_watchers import (
    NUDGE_DECLINED,
    NUDGE_REPORTED,
    NUDGE_SENT,
    NUDGE_SUPERSEDED,
    NudgeOutcome,
    SubagentWatchers,
)
from ciao.workspace_guide import guide_path as workspace_guide_path

logger = logging.getLogger(__name__)

_ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# How long the PWA gets to answer a model-capability question (image input
# against a non-vision model) before the turn closes with the system bubble
# instead. Shorter than the permission-gate timeout on purpose: the user is
# answering one click, not a review of tool input.
CAPABILITY_QUESTION_TIMEOUT_S = 30

# User-visible copy when the pre-flight closes a turn because the model
# cannot see the attached images. Kept in one place so the backend bubble
# and the docs stay in sync.
_CAPABILITY_IMAGE_MSG = (
    "Image input not sent — this model can't see images. "
    "Pick a model that supports images and re-send."
)

_RETRY_INTERVAL_SECONDS = 60 * 60
_RETRY_CONNECTION_INTERVAL_SECONDS = 30
_RETRY_STATUSES = {"pending", "stopped", ""}
# Shortest synthesis-nudge reply that still earns an unread badge and a push.
# Anything shorter is the model's own bookkeeping ("ok", "done."). Applies only
# to the nudge drain, never to a reply the user asked for: see
# ProjectChatManager._is_worth_announcing_nudge_reply.
_NUDGE_ANNOUNCE_MIN_CHARS = 4

# How long a result announce may stay parked once the synthesis nudge has been
# delivered and the between-turns drain owns it. The drain releases the entry on
# every way its event stream can end; this covers the one way it cannot see — a
# CLI that is alive and simply never answers (issue #437).
#
# The trade-off runs both ways and neither end is free: too short and a
# genuinely slow synthesis turn gets the interim "I'll report back" push moments
# before its own real one; too long and a notification the user needed arrives
# after it stopped being useful. Five minutes is well past any synthesis turn
# observed in practice (they follow subagents that have already finished, so the
# parent only has to write a report) while still being inside the window where
# someone might act on the result.
_PARKED_ANNOUNCE_DEADLINE_SECONDS = 300.0

# The synthesis nudge's outcome vocabulary is defined with its only caller,
# ciao/web/subagent_watchers.py, and re-exported here: the nudge itself stays
# in this class (it is assembled from provider and drain state this class
# owns), and importers of these names do not move.
# Patterns an unattended parent emits while still waiting on its background
# subagents. A run that ended on one of these never synthesized its agents'
# results — the follow-up turn died before producing a report — so the run is
# not done even though the turn completed "cleanly". Matched loosely against
# the whole flattened reply (case-insensitive, anchored on the waiting
# phrase) so wording changes do not defeat the guard.
_INTERIM_SUBAGENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bwaiting on\b",
        r"\bwaiting for\b.*\bsubagent",
        r"\bstill running\b.*\bsubagent",
        r"\bshall report back\b",
        r"\bwill report back\b",
        r"\breport back once\b",
    )
)
# Prompt used to resume a session after a mid-response connection drop. The
# original prompt is NOT replayed — the partial turn already ran (and may have
# executed tools), so we resume the existing session and ask it to continue,
# matching the "send 'continue' to resume" idiom the interrupted-turn banner
# already tells users about.
_RESUME_CONTINUE_PROMPT = "continue"
# Cap on consecutive resume-continue retries for a mid-response drop so a
# persistently flaky connection cannot loop forever burning quota. Once hit,
# the turn is left for the user to continue manually.
_MAX_CONNECTION_DROP_RETRIES = 6

# Injected into the parent turn when its background subagents all finish. The
# CLI does not auto-continue a parent turn after a background `Agent` dispatch
# completes (see ciao/system_prompt.md), so without this nudge the chat stays
# stuck on the interim "I'll report back when they finish" message. The nudge
# is delivered on the persistent client so the already-running between-turns
# drain captures and publishes the synthesis turn like a normal reply. The text
# is owned by ciao/subagent_tracking.py, which also has to recognize it (the
# /messages renderer collapses it into a system line rather than showing a user
# bubble nobody typed).
_SUBAGENT_SYNTHESIS_NUDGE = subagent_tracking.SUBAGENT_SYNTHESIS_NUDGE
_LEGACY_MODEL_BUCKETS = {"work", "personal"}
# Coalescing window for background command runs (ciao/background.py): a
# batch of scripts that finishes together should produce one wake turn, not N.
_BACKGROUND_WAKE_WINDOW_SECONDS = 5.0
# Log-tail budget per finished run in the wake prompt. The full log path is
# always included, so this only has to be enough to decide whether to read it.
_BACKGROUND_WAKE_TAIL_LINES = 50
# ── Idle provider reaping ────────────────────────────────────────────────
# `self._providers` is keyed by chat id and, before this, was only ever
# emptied by a lifecycle event (session reset, handover, archive, delete).
# That is fine for Claude, whose provider is an in-process SDK client, but
# opencode runs **one `opencode serve` process per chat** — see the module
# docstring in ciao/providers/opencode.py for why the control plane's per-chat
# MCP token forces that. Without a reaper, a day of touching chats leaves a
# server process, a port, an SSE stream and a stderr reader alive for every
# one of them until the app restarts.
#
# A provider is only reclaimed when the chat has been quiet for the timeout
# AND has no work in flight (`active_chat_ids`, a between-turns drain, a retry
# loop or a pending background wake). Reclaiming is cheap to undo: the chat's
# `session_id` is persisted, so the next turn reconnects and resumes rather
# than starting a new conversation — the same path a token rotation already
# takes (ciao/providers/opencode.py::_ensure_server,
# ciao/providers/claude.py::_ensure_connected).
_PROVIDER_IDLE_TIMEOUT_SECONDS = 900.0
# How often the sweep runs. Well under the timeout so a provider is reclaimed
# within roughly one interval of becoming eligible, and far above any per-turn
# cadence so an idle install is not woken constantly.
_PROVIDER_REAP_INTERVAL_SECONDS = 120.0
# How many sweeps may try to disconnect the same provider before the sweep
# gives up on it. A `disconnect()` that raises leaves the process it was meant
# to end possibly alive, so the reference must not simply be dropped — the
# shutdown hook reads `self._providers` and is the last chance to finish the
# job. But a provider that fails forever (a wedged `process.wait()`, a
# transport that raises on every close) would be pinned in that map forever,
# which is the same leak from the other end, and it would be handed back to
# the chat's next turn. So the retry is bounded: `MAX - 1` further sweeps,
# then the reference is dropped with an ERROR naming the chat, and the sweep
# does NOT report it as reclaimed.
_PROVIDER_DISCONNECT_MAX_ATTEMPTS = 3


_ANTHROPIC_MODEL_BUCKETS = {"work", "anthropic"}


@contextmanager
def _state_file_lock(path: Path) -> Iterator[None]:
    """Serialize read/merge/write cycles across overlapping server processes."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Legacy IDs from the removed auto-imported Claude Code CLI view.
_CC_CLI_PROJECT_ID = "proj-cc-cli"
_CC_CHAT_PREFIX = "chat-cc-"


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


# One-shot titler budget. Hosted models answer in a couple of seconds; a slow
# local backend simply times out and the deterministic fallback applies, the
# same trade the schedule attention classifier makes with a longer window.
_TITLE_LLM_TIMEOUT_S = 45.0


# ── Data models ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class ProjectInfo:
    project_id: str
    name: str
    workspace: str  # "personal" | "work"
    context: str = ""
    created_at: str = ""
    order: int = 0
    vault_folder: str = ""  # e.g. "store-intelligence-platform"
    # Marks a project the app owns rather than one the user made. "memory" is
    # the per-workspace Memory project the end-of-conversation pass runs in.
    # Empty for every ordinary project, so this is not a behaviour switch for
    # an existing install.
    kind: str = ""
    # Runtime-only: relative path to the canonical vault doc (e.g.
    # "memory-vault/personal/projects/active/ciao-improvements/README.md"). Not
    # persisted in JSON; recomputed on every vault discovery pass.
    vault_doc_path: str = ""

    @property
    def is_auto(self) -> bool:
        return self.name == "General" or self.name == "Claude Code CLI"

    @property
    def is_system(self) -> bool:
        return self.kind == "memory" or self.name == "Claude Code CLI"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "workspace": self.workspace,
            "context": self.context,
            "created_at": self.created_at,
            "order": self.order,
            "vault_folder": self.vault_folder,
            "kind": self.kind,
            "vault_doc_path": self.vault_doc_path,
            "is_system": self.is_system,
            "is_auto": self.is_auto,
        }


@dataclass(slots=True)
class ChatInfo:
    chat_id: str
    project_id: str
    title: str = "New Chat"
    model: str = "opus"
    # Routing key for ProviderService: which CLI runs the turn.
    provider: str = "claude"
    mode: BridgeMode = "auto"
    # Provider-native thinking/reasoning level (see ciao.models.THINKING_LEVELS).
    # Empty = provider default. Reset on handover: levels aren't portable
    # across providers.
    thinking_level: str = ""
    session_id: str = ""
    # SDK session ids this chat rotated through earlier in the SAME
    # conversation (autocompact, or a resume-failure fallback that forks a
    # new session) — oldest first. `/messages` walks these plus the current
    # `session_id` to render continuous history across the rotation, since
    # each SDK session file only holds the turns written after it started.
    # Cleared (not carried forward) by explicit resets: new_session() and
    # handover_chat() intentionally start a new conversation/session lineage.
    previous_session_ids: list[str] = field(default_factory=list)
    created_at: str = ""
    archived: bool = False
    last_activity_at: str = ""
    # Cross-device read tracking. Set by `mark_read` (via POST
    # /api/chats/{id}/read). A chat is considered unread when
    # `last_activity_at > last_read_at`.
    last_read_at: str = ""
    # Truncated text of the last assistant reply, set alongside
    # `last_activity_at` wherever a turn finishes with real output. Mirrors
    # `last_activity_at` so the sidebar's unread tile can show "what finished"
    # without holding the full transcript in memory.
    last_snippet: str = ""
    # Bounded tail of the final assistant response for automation predicates
    # that must not decide from the 280-character display snippet.
    last_response: str = ""
    last_response_status: str = ""
    # Monotonic counter of user turns initiated for this chat. Used as the
    # key when recording image attachments so we can re-emit them alongside
    # the replayed SDK session history (which strips attachments).
    user_turn_count: int = 0
    # Map of user-turn index → list of image ref filenames (relative to
    # media_root). JSON round-trip turns int keys into strings, so lookups
    # must tolerate both.
    user_turn_images: dict = field(default_factory=dict)
    # Map of user-turn index (as str) → {sent_at, completed_at, duration_ms}.
    # Drives the per-message footer in the PWA (time of send, agent latency).
    # Recorded at the orchestration layer so it stays provider-agnostic.
    user_turn_timings: dict = field(default_factory=dict)
    # Map of user-turn index (as str) → True for turns fired by an automation
    # rather than typed by the user. Without it an automation run renders as
    # an ordinary user bubble, so neither the reader nor the model can tell the
    # difference (the model narrated "even though you're actively messaging me"
    # while replying to its own recurring prompt). Absent key = interactive turn, so
    # pre-feature chats degrade to today's behaviour.
    user_turn_unattended: dict = field(default_factory=dict)
    # Relative workspace path to the archived markdown transcript.
    # Set when archive_chat() succeeds; cleared on new_session().
    archive_path: str = ""
    # Transient UI flag: "pending" while an auto-title generation is in
    # flight, "ready" otherwise. Not persisted — reset to "ready" on load.
    title_status: str = "ready"
    # Deferred retry state for provider quota/session-limit failures. Pending
    # retries are replayed hourly until they succeed, the user stops them, or
    # the chat is archived/deleted.
    retry_status: str = ""
    retry_prompt: str = ""
    retry_image_refs: list[str] = field(default_factory=list)
    retry_next_at: str = ""
    retry_last_error: str = ""
    retry_attempts: int = 0
    retry_interval_seconds: int = _RETRY_INTERVAL_SECONDS
    # Visible messages preserved when the chat is handed to a fresh provider
    # session. They are prepended by /messages so the same chat does not lose
    # pre-handover history after reload.
    handover_messages: list[dict] = field(default_factory=list)
    # True until the first post-handover turn successfully seeds the new
    # provider with `handover_messages` inside the hidden Ciaobot context block.
    #
    # Naming note: "handover" here is PROVIDER-session context carry-over — set
    # by provider switches, forks/continues, and by the workspace re-rooting
    # (`workspace_reroot.flag_stranded_sessions`) for chats whose old session
    # was stranded by the move. It has nothing to do with the multi-device
    # host/client role handover in `ciao/node_state.py`; only the word collides.
    # The key is persisted in `.runtime/web_projects.json`, so it cannot be
    # renamed without breaking existing installs.
    handover_context_pending: bool = False
    # Stable routing facts are sent once per provider session. A changed
    # project/workspace digest or a new provider session re-sends them.
    context_digest: str = ""
    context_session_id: str = ""
    # Raw AskUserQuestion JSON (`{"questions": [...]}`) when the model paused
    # this chat on a question the user hasn't answered yet. Set when the
    # headless CLI fires AskUserQuestion (which we interrupt so it can't
    # auto-answer); cleared on the next user send. Persisted and surfaced in
    # `to_dict` so the PWA can rebuild its interactive picker after a reload
    # instead of showing the dead `{"questions": ...}` trace row.
    pending_question: str = ""
    # Raw PermissionRequestEvent fields (JSON: request_id/tool_name/message/
    # tool_input) when the model is blocked mid-turn on an unanswered
    # Approve/Deny prompt. Unlike `pending_question`, this does not pause the
    # turn across a reconnect — it exists so a chat sitting in the background
    # (not the currently open WS stream) still shows up as needing attention
    # in-app instead of only firing an OS push. Cleared on answer
    # (respond_permission) or when the turn ends by any other path (the
    # `finally` in `_drive`'s turn loop), since no permission can outlive its
    # turn.
    pending_permission: str = ""
    # User messages that were queued (mode="queue") while a turn was running
    # and then parked when that turn paused on an AskUserQuestion. The pause
    # tears down the stream (and its in-memory pending queue), so they are
    # stashed here and re-seeded into the next stream — the user's answer turn
    # — so they still flush as follow-ups instead of being silently dropped.
    # Each entry is {"id": str, "text": str, "images": list[str]}.
    pending_queue: list[dict] = field(default_factory=list)
    # Provider-neutral conversation fork lineage. Forks are normal chats with
    # a fresh provider session; these fields only preserve their relationship
    # to the source conversation and stable root-relative title numbering.
    forked_from_chat_id: str = ""
    forked_from_turn_index: int | None = None
    fork_root_chat_id: str = ""
    fork_index: int = 0
    fork_base_title: str = ""
    # Backlink to the schedule that created or drives this chat. Stamped in
    # prepare_schedule_chat for both branches (web_project_id spawns a new
    # chat per run, web_chat_id reuses a fixed chat). Lets the PWA show a
    # "triggered by schedule X" banner on the chat that survives later runs
    # (a project-bound schedule is 1:many with chats, so the link can't live
    # only on the automation side). Empty for interactive chats.
    schedule_id: str = ""
    schedule_title: str = ""
    # Server-owned lifecycle metadata for chats created by the proposal review
    # UI. Resolution helpers may auto-archive only after their target proposal
    # IDs have durably left the queue; discussion helpers always remain manual.
    helper: dict = field(default_factory=dict)
    # What the post-archive pipeline is doing, or did. Archiving a chat kicks
    # off insights extraction, a project-doc fold, a trajectory and memory
    # proposals (ciao/insights.py:extract_and_append), and until now none of
    # that was visible anywhere in the app. Lives on the chat rather than in
    # job_runs because it has to survive a restart and the run-log's own
    # rotation: an archived chat opened next month should still be able to say
    # what Ciaobot took from it.
    #
    # {"state": "running"|"done", "step": "<job id>",
    #  "steps": {"<job id>": {"status": ..., "extra": {...}}},
    #  "started_at": iso, "updated_at": iso}
    postprocess: dict = field(default_factory=dict)

    def to_dict(self, *, local: bool | None = None) -> dict:
        d = {
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "title": self.title,
            "model": self.model,
            "provider": self.provider,
            "mode": self.mode,
            "thinking_level": self.thinking_level,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "archived": self.archived,
            "last_activity_at": self.last_activity_at,
            "last_read_at": self.last_read_at,
            "last_snippet": self.last_snippet,
            "title_status": self.title_status,
            "pending_question": self.pending_question,
            "pending_permission": self.pending_permission,
            "forked_from_chat_id": self.forked_from_chat_id,
            "forked_from_turn_index": self.forked_from_turn_index,
            "fork_root_chat_id": self.fork_root_chat_id,
            "fork_index": self.fork_index,
            "fork_base_title": self.fork_base_title,
            "schedule_id": self.schedule_id,
            "schedule_title": self.schedule_title,
            "helper": dict(self.helper),
            "retry": {
                "status": self.retry_status,
                "next_at": self.retry_next_at,
                "last_error": self.retry_last_error,
                "attempts": self.retry_attempts,
                "interval_seconds": self.retry_interval_seconds,
            } if self.retry_status else None,
        }
        if self.archive_path:
            d["archive_path"] = self.archive_path
        if self.postprocess:
            d["postprocess"] = dict(self.postprocess)
        if local is not None:
            d["local"] = local
        return d


@dataclass(slots=True, frozen=True)
class ArchiveOutcome:
    """Result of archiving a chat.

    Carries enough metadata for the route handler to dispatch a
    background insights extraction without re-loading the transcript or
    re-reading the JSONL (the JSONL is deleted as part of archiving).
    """

    path: Path
    session_id: str
    turn_count: int
    filtered_jsonl: str | None


# ── Manager ──────────────────────────────────────────────────────────────


class ProjectChatManager:
    """Manages projects and chats for the PWA web interface."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        state_store: StateStore,
        transcript_store: TranscriptStore,
        path: Path | None = None,
    ) -> None:
        self._config = config
        self._state = state_store
        self._transcripts = transcript_store
        self._path = path
        self._projects: dict[str, ProjectInfo] = {}
        self._chats: dict[str, ChatInfo] = {}
        self._file_refs: dict[str, tuple[str, str, float]] = {}
        # Canonical-doc frontmatter memo, keyed by path -> ((mtime_ns, size),
        # (name, context)). Vault discovery re-reads every project doc on each
        # list_projects() call, so without this the sidebar would parse the
        # whole active tree's YAML on every refresh.
        self._doc_meta_cache: dict[str, tuple[tuple[int, int], tuple[str, str]]] = {}
        # Snapshot of this manager's own last-seen state.  It intentionally
        # excludes records written by another process after this manager was
        # created: _save() diffs against this snapshot and applies only local
        # mutations to the latest on-disk registry.  That prevents an old
        # process draining during an upgrade from erasing chats created by
        # the replacement process.
        self._last_local_payload: dict[str, Any] = {
            "version": 1,
            "projects": {},
            "chats": {},
        }
        self._providers: dict[str, ProviderService] = {}
        # Monotonic stamp of the last time each chat's provider was handed
        # out, plus the single sweep task that reclaims the idle ones. See
        # `_PROVIDER_IDLE_TIMEOUT_SECONDS` for why this exists; the task is
        # started lazily on first use so a manager built in a test (or any
        # process with no running loop) never creates one it does not need.
        self._provider_last_used: dict[str, float] = {}
        # Consecutive failed disconnect attempts per chat, kept only while a
        # provider is being retried (see `_PROVIDER_DISCONNECT_MAX_ATTEMPTS`)
        # and cleared by `_pop_provider`, so it cannot outlive the provider it
        # counts for or be inherited by a recycled chat id.
        self._provider_disconnect_failures: dict[str, int] = {}
        self._provider_reaper: asyncio.Task | None = None
        self._provider_idle_timeout = _PROVIDER_IDLE_TIMEOUT_SECONDS
        self._provider_reap_interval = _PROVIDER_REAP_INTERVAL_SECONDS
        # Fold turn journals left behind by a crashed process into their
        # transcripts as is_partial turns before anything reads history.
        try:
            transcript_store.recover_journals()
        except Exception:  # noqa: BLE001 — recovery must never block startup
            logger.exception("Turn journal recovery failed")
        # Bound by main.py when the embedded MCP control plane is enabled.
        # Tests and legacy-only instances intentionally leave it unset.
        self._mcp_service: Optional["CiaoMcpService"] = None
        self._broker = ChatStreamBroker()
        self._events = EventsHub()
        # Per-(chat, file) content snapshots taken on Write/Edit/MultiEdit/
        # NotebookEdit. Backs the file viewer's History and Diff tabs and the
        # `restore` action. See ciao/web/file_snapshots.py for the storage
        # layout and dedup behaviour. The runtime root is wherever the
        # state file lives — `.runtime/` by default, but overridable via
        # ``CIAO_RUNTIME_ROOT`` for ops.
        snapshots_dir = Path(config.state_path).parent / "snapshots"
        self._snapshots = SnapshotStore(snapshots_dir)
        # Optional callbacks set by the web app (push, focus tracking).
        # `notify_result(chat_id, snippet)` is called when a turn finishes
        # successfully; the app uses it to dispatch web push to unfocused
        # subscribers. Kept as an injection point so the manager has no
        # direct dependency on Starlette state.
        self.notify_result_cb: Optional[Callable[[str, str, str], None]] = None
        # `notify_permission(chat_id, tool_name, message, request_id)` fires
        # whenever the Auto-mode classifier asks the user to approve a tool.
        # The PWA turn is blocked until the answer lands, so unlike the
        # result push this fires immediately (no delay) and only skips when
        # the chat is focused in the foreground.
        self.notify_permission_cb: Optional[Callable[[str, str, str, str], None]] = None
        # `notify_question(chat_id, question_text)` fires when the model uses
        # AskUserQuestion. The headless CLI auto-cancels with empty answers,
        # so we notify the user so they can answer in the next turn.
        self.notify_question_cb: Optional[Callable[[str, str], None]] = None
        # Fired after a read mutation so the macOS companion and remote PWA
        # service workers can dismiss already-delivered OS notifications for
        # that chat.
        self.clear_notifications_cb: Optional[Callable[[str], None]] = None
        # Per-chat pending push tasks. Pushes are scheduled with a short
        # delay (30s) so that reading the
        # chat on any device within the window suppresses the buzz. New
        # replies to the same chat cancel the previous timer and start a
        # new one (coalesce rapid replies into a single push).
        self._pending_push: dict[str, asyncio.Task] = {}
        self._archive_locks: dict[str, asyncio.Lock] = {}
        # Callers currently holding or waiting on each archive lock, so
        # the lock is only dropped once the last one is done with it.
        self._archive_lock_users: dict[str, int] = {}
        # Background subagent watching, and the CLI-task wake bookkeeping that
        # comes with it, live in ciao/web/subagent_watchers.py. It owns the
        # three dictionaries that used to sit here — the live watcher task per
        # chat, the last count published per chat, and the wakes already sent —
        # and reaches back into this class only through SubagentWatcherHost.
        # `self` is that host; the properties further down keep the old
        # attribute names pointing at its state. Annotated explicitly: passing
        # `self` to a collaborator that reads `_subagents` would otherwise make
        # mypy resolve this assignment's own type through the cycle.
        self._subagents: SubagentWatchers = SubagentWatchers(self)
        self._streaming = ChatStreaming(self)
        # Scheduled dispatch is a separate lifecycle with its own typed host
        # seam; this manager remains the coordinator for the target chat and
        # archive policies it calls back into.
        self._schedule_dispatcher: ScheduleDispatcher = ScheduleDispatcher(self)
        # Archive post-processing state, manifests, retries, and completion
        # hooks are owned together; the manager keeps only coordinating seams.
        self._archive_pipeline: ArchivePipeline = ArchivePipeline(self)
        # The end-of-conversation memory pass, as a normal chat in the
        # workspace's Memory project. Inert while ciao.web.memory_pass's
        # MEMORY_PASS_CHATS is False; see that module for the lifecycle.
        self._memory_pass = MemoryPassCoordinator(self)
        # Result announces parked while the synthesis nudge decides whether it
        # will speak instead. See `_park_result_announce`. chat_id ->
        # (token, project_id, title, snippet).
        self._parked_result_announce: dict[str, tuple[int, str, str, str]] = {}
        # Monotonic id per park, so a late releaser (the deadline task, a
        # superseded watcher) can prove the entry it is about to act on is
        # still the one it parked.
        self._parked_announce_seq = 0
        # Per-chat deadline task armed once the drain owns a parked announce.
        # Held so the drain going away (a new user turn, delete, archive) can
        # cancel it: the deadline's whole premise is "the drain still owns this
        # and will never release it", and that premise dies with the drain.
        self._parked_announce_deadlines: dict[str, asyncio.Task] = {}
        # Finished background command runs waiting to wake the chat that
        # started them, keyed by chat id. Held for
        # _BACKGROUND_WAKE_WINDOW_SECONDS so a batch of scripts that finishes
        # together produces one wake turn, not four.
        self._background_wake_pending: dict[str, list[dict[str, Any]]] = {}
        self._background_wake_tasks: dict[str, asyncio.Task] = {}
        # Bound by main.py so a wake dropped by the restart drain can mark its
        # runs for replay on the next start instead of vanishing.
        self._background_runner: Any = None
        # Bound by main.py right after the manager and store exist. dispatch_schedule
        # stamps a failed run's last_status on the stored row so the Automations
        # sidebar flags it for attention (issue #407) — without a store there is
        # nowhere durable to write, and tests build managers without one.
        self.schedule_store: ScheduleStore | None = None
        # A requested server restart drains existing chat work before uvicorn
        # shuts down. Once draining begins, ongoing streams (including their
        # already-queued follow-ups) may finish, but idle chats must not start
        # new turns or the server could race a fresh provider request.
        self._restart_draining = False
        # Per-chat deferred quota retry loops. Each loop sleeps until the
        # chat's retry_next_at, tries the saved prompt if idle, then repeats
        # hourly until success/stop/archive/delete.
        self._retry_tasks: dict[str, asyncio.Task] = {}
        # Strong references to detached background tasks. asyncio keeps only a
        # weak reference to a running task, so a fire-and-forget
        # ``create_task(...)`` whose result nobody holds can be collected
        # mid-flight, and any exception it raised is reported as "Task
        # exception was never retrieved" at GC time instead of being logged.
        self._detached_tasks: set[asyncio.Task[object]] = set()
        # Chats with a native-title poll in flight. Both the first-message and
        # the end-of-turn trigger want to title the same chat, and each poll
        # costs real provider reads (an opencode read spawns a throwaway
        # `opencode serve`), so the second trigger joins the first instead of
        # racing it.
        self._titling: set[str] = set()
        self._runtime_root = Path(config.state_path).parent
        # The loop the manager was constructed on, so job-run events arriving
        # from a worker thread can be marshalled back onto it before touching
        # EventsHub (whose asyncio.Queue wants the loop thread).
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._push_delay_seconds = 30
        self._load()
        self._migrate_remove_claude_code_cli_project()
        self._migrate_drop_qn_prefix()
        self._ensure_defaults()
        self._discover_vault_projects()
        self._recover_orphaned_active_chats()
        self._reconcile_half_archived_chats()
        self._rehome_orphaned_chats()
        # NOTE: the automatic empty-chat sweep is intentionally disabled. It
        # raced the just-created chat on every new-chat POST (create_chat
        # swept the empty chat the user had just opened), closing the panel
        # behind a stale /api/chats poll and causing the "flash". Users can
        # still delete an empty chat by hand via DELETE ?only_if_empty=1.
        self._ensure_retry_tasks()

    # ── Persistence ──────────────────────────────────────────────────────


    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load web projects from %s: %s", self._path, exc)
            return
        for pid, pd in data.get("projects", {}).items():
            self._projects[pid] = ProjectInfo(
                project_id=pid,
                name=pd["name"],
                workspace=pd["workspace"],
                context=pd.get("context", ""),
                created_at=pd.get("created_at", ""),
                order=pd.get("order", 0),
                vault_folder=pd.get("vault_folder", ""),
                kind=pd.get("kind", ""),
            )
        for cid, cd in data.get("chats", {}).items():
            chat_model = cd.get("model", self._config.claude_default_model)
            self._chats[cid] = ChatInfo(
                chat_id=cid,
                project_id=cd["project_id"],
                title=cd.get("title", "New Chat"),
                model=chat_model,
                # Migration: legacy chats without a `provider` key default to "claude".
                provider=cd.get("provider") or "claude",
                # Migration: legacy chats without a bucket stay "" (auto:
                # project workspace decides routing).
                mode=cd.get("mode", self._config.claude_mode),
                thinking_level=cd.get("thinking_level", ""),
                session_id=cd.get("session_id", ""),
                previous_session_ids=list(cd.get("previous_session_ids", [])),
                created_at=cd.get("created_at", ""),
                archived=cd.get("archived", False),
                last_activity_at=cd.get("last_activity_at", cd.get("created_at", "")),
                # Migration: existing chats have no last_read_at. Default to
                # last_activity_at so we don't surface the entire history as
                # unread on first boot after upgrade.
                last_read_at=cd.get(
                    "last_read_at",
                    cd.get("last_activity_at", cd.get("created_at", "")),
                ),
                last_snippet=cd.get("last_snippet", ""),
                last_response=cd.get("last_response", ""),
                last_response_status=cd.get("last_response_status", ""),
                user_turn_count=cd.get("user_turn_count", 0),
                user_turn_images=dict(cd.get("user_turn_images", {})),
                user_turn_timings=dict(cd.get("user_turn_timings", {})),
                user_turn_unattended=dict(cd.get("user_turn_unattended", {})),
                archive_path=cd.get("archive_path", ""),
                retry_status=cd.get("retry_status", "") if cd.get("retry_status", "") in _RETRY_STATUSES else "",
                retry_prompt=cd.get("retry_prompt", ""),
                retry_image_refs=list(cd.get("retry_image_refs", [])),
                retry_next_at=cd.get("retry_next_at", ""),
                retry_last_error=cd.get("retry_last_error", ""),
                retry_attempts=int(cd.get("retry_attempts", 0) or 0),
                retry_interval_seconds=int(cd.get("retry_interval_seconds", _RETRY_INTERVAL_SECONDS) or _RETRY_INTERVAL_SECONDS),
                handover_messages=chat_service._normalize_handover_messages(
                    list(cd.get("handover_messages", []))
                ),
                handover_context_pending=bool(cd.get("handover_context_pending", False)),
                context_digest=cd.get("context_digest", ""),
                context_session_id=cd.get("context_session_id", ""),
                pending_question=cd.get("pending_question", ""),
                pending_permission=cd.get("pending_permission", ""),
                pending_queue=list(cd.get("pending_queue", [])),
                forked_from_chat_id=cd.get("forked_from_chat_id", ""),
                forked_from_turn_index=cd.get("forked_from_turn_index"),
                fork_root_chat_id=cd.get("fork_root_chat_id", ""),
                fork_index=int(cd.get("fork_index", 0) or 0),
                fork_base_title=cd.get("fork_base_title", ""),
                schedule_id=cd.get("schedule_id", ""),
                schedule_title=cd.get("schedule_title", ""),
                helper=chat_service._normalize_chat_helper(cd.get("helper")),
                # A pipeline recorded as "running" cannot still be running: the
                # task died with the previous process. Restore it as done so the
                # chat reports what it managed to finish instead of pulsing
                # forever on a spinner nothing will ever clear.
                postprocess=chat_service._restored_postprocess(cd.get("postprocess")),
            )
        logger.info(
            "Restored %d project(s) and %d chat(s)",
            len(self._projects),
            len(self._chats),
        )
        self._last_local_payload = copy.deepcopy(self._state_payload())

    def _migrate_remove_claude_code_cli_project(self) -> None:
        """Remove the retired CLI-import project from persisted PWA state."""
        retired_project_ids = {
            pid
            for pid, project in self._projects.items()
            if pid == _CC_CLI_PROJECT_ID or project.name == "Claude Code CLI"
        }
        for pid in retired_project_ids:
            self._projects.pop(pid, None)
        removed_chats = [
            cid
            for cid, chat in self._chats.items()
            if chat.project_id in retired_project_ids or cid.startswith(_CC_CHAT_PREFIX)
        ]
        for cid in removed_chats:
            self._chats.pop(cid, None)
        if retired_project_ids or removed_chats:
            logger.info(
                "Removed %d retired Claude Code CLI project(s) and %d imported chat(s)",
                len(retired_project_ids),
                len(removed_chats),
            )
            self._save()

    def _state_payload(self) -> dict[str, Any]:
        """Serialize this manager's in-memory project/chat view."""
        return {
            "version": 1,
            "projects": {
                pid: {
                    "name": p.name,
                    "workspace": p.workspace,
                    "context": p.context,
                    "created_at": p.created_at,
                    "order": p.order,
                    "vault_folder": p.vault_folder,
                    "kind": p.kind,
                }
                for pid, p in self._projects.items()
            },
            "chats": {
                cid: {
                    "project_id": c.project_id,
                    "title": c.title,
                    "model": c.model,
                    "provider": c.provider,
                    "mode": c.mode,
                    "thinking_level": c.thinking_level,
                    "session_id": c.session_id,
                    "previous_session_ids": c.previous_session_ids,
                    "created_at": c.created_at,
                    "archived": c.archived,
                    "last_activity_at": c.last_activity_at,
                    "last_read_at": c.last_read_at,
                    "last_snippet": c.last_snippet,
                    "last_response": c.last_response,
                    "last_response_status": c.last_response_status,
                    "user_turn_count": c.user_turn_count,
                    "user_turn_images": c.user_turn_images,
                    "user_turn_timings": c.user_turn_timings,
                    "user_turn_unattended": c.user_turn_unattended,
                    "archive_path": c.archive_path,
                    "retry_status": c.retry_status,
                    "retry_prompt": c.retry_prompt,
                    "retry_image_refs": c.retry_image_refs,
                    "retry_next_at": c.retry_next_at,
                    "retry_last_error": c.retry_last_error,
                    "retry_attempts": c.retry_attempts,
                    "retry_interval_seconds": c.retry_interval_seconds,
                    "handover_messages": c.handover_messages,
                    "handover_context_pending": c.handover_context_pending,
                    "context_digest": c.context_digest,
                    "context_session_id": c.context_session_id,
                    "pending_question": c.pending_question,
                    "pending_permission": c.pending_permission,
                    "pending_queue": c.pending_queue,
                    "forked_from_chat_id": c.forked_from_chat_id,
                    "forked_from_turn_index": c.forked_from_turn_index,
                    "fork_root_chat_id": c.fork_root_chat_id,
                    "fork_index": c.fork_index,
                    "fork_base_title": c.fork_base_title,
                    "schedule_id": c.schedule_id,
                    "schedule_title": c.schedule_title,
                    "helper": c.helper,
                    "postprocess": c.postprocess,
                }
                for cid, c in self._chats.items()
            },
        }

    @staticmethod
    def _merge_local_map(
        latest: dict[str, Any],
        current: dict[str, Any],
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply this process's record/field delta onto the latest disk map.

        A simple dictionary union is insufficient because it resurrects
        records another process deleted.  Replacing whole records is also
        unsafe because concurrent changes to different fields of one chat
        would clobber each other.  Diffing against the process-local baseline
        gives us the intended mutation set without requiring every caller to
        mark dirty fields manually.
        """
        merged = {
            str(key): dict(value)
            for key, value in latest.items()
            if isinstance(value, dict)
        }

        for key in baseline.keys() - current.keys():
            merged.pop(key, None)

        missing = object()
        for key, record in current.items():
            if not isinstance(record, dict):
                continue
            before = baseline.get(key)
            if not isinstance(before, dict):
                merged[key] = dict(record)
                continue

            changed = {
                field: value
                for field, value in record.items()
                if before.get(field, missing) != value
            }
            removed_fields = before.keys() - record.keys()
            if not changed and not removed_fields:
                continue

            target_source = merged.get(key)
            if not isinstance(target_source, dict):
                # A concurrent delete followed by a genuine local mutation
                # revives the record with its complete prior shape rather
                # than an invalid partial row.
                target_source = before
            target = dict(target_source)
            target.update(changed)
            for field in removed_fields:
                target.pop(field, None)
            merged[key] = target

        return merged

    def _read_latest_payload(self) -> dict[str, Any]:
        if not self._path or not self._path.exists():
            return {"version": 1, "revision": 0, "projects": {}, "chats": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Refusing to overwrite unreadable chat registry %s: %s", self._path, exc)
            raise
        if not isinstance(payload, dict):
            raise ValueError(f"Chat registry {self._path} is not a JSON object")
        return payload

    @staticmethod
    def _mutation_summary(
        baseline: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, list[str]]:
        before = {str(key): value for key, value in baseline.items()}
        after = {str(key): value for key, value in current.items()}
        return {
            "added": sorted(after.keys() - before.keys()),
            "updated": sorted(
                key for key in after.keys() & before.keys() if after[key] != before[key]
            ),
            "deleted": sorted(before.keys() - after.keys()),
        }

    @staticmethod
    def _has_mutations(summary: dict[str, list[str]]) -> bool:
        return any(summary.get(kind) for kind in ("added", "updated", "deleted"))

    def _append_registry_audit(
        self,
        *,
        revision: int,
        reason: str,
        project_mutations: dict[str, list[str]],
        chat_mutations: dict[str, list[str]],
    ) -> None:
        if not self._path:
            return
        if not (
            self._has_mutations(project_mutations)
            or self._has_mutations(chat_mutations)
        ):
            return
        audit_path = self._path.with_name(f"{self._path.stem}.audit.jsonl")
        event = {
            "timestamp": chat_service._now_iso(),
            "pid": os.getpid(),
            "revision": revision,
            "reason": reason,
            "projects": project_mutations,
            "chats": chat_mutations,
        }
        try:
            with audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # The registry remains authoritative. An audit failure must not
            # turn a successful user mutation into a reported API failure.
            logger.exception("Failed to append registry audit %s", audit_path)

    def _audited_chat_status(self, chat_id: str) -> str:
        """Return ``present``, ``deleted``, or ``unknown`` from the audit log."""

        if not self._path:
            return "unknown"
        audit_path = self._path.with_name(f"{self._path.stem}.audit.jsonl")
        if not audit_path.exists():
            return "unknown"
        status = "unknown"
        try:
            with audit_path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    changes = event.get("chats")
                    if not isinstance(changes, dict):
                        continue
                    if chat_id in changes.get("deleted", []):
                        status = "deleted"
                    elif chat_id in changes.get("added", []) or chat_id in changes.get(
                        "updated", []
                    ):
                        status = "present"
        except OSError:
            logger.exception("Failed to read registry audit %s", audit_path)
        return status

    def _save(self, *, reason: str = "registry_mutation") -> None:
        if not self._path:
            return
        current = self._state_payload()
        baseline = self._last_local_payload
        baseline_projects = baseline.get("projects")
        baseline_chats = baseline.get("chats")
        current_projects = current.get("projects")
        current_chats = current.get("chats")
        project_mutations = self._mutation_summary(
            baseline_projects if isinstance(baseline_projects, dict) else {},
            current_projects if isinstance(current_projects, dict) else {},
        )
        chat_mutations = self._mutation_summary(
            baseline_chats if isinstance(baseline_chats, dict) else {},
            current_chats if isinstance(current_chats, dict) else {},
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _state_file_lock(self._path):
            latest = self._read_latest_payload()
            latest_projects = latest.get("projects")
            latest_chats = latest.get("chats")
            revision = int(latest.get("revision", 0) or 0) + 1
            payload = {
                "version": 1,
                "revision": revision,
                "projects": self._merge_local_map(
                    latest_projects if isinstance(latest_projects, dict) else {},
                    current_projects if isinstance(current_projects, dict) else {},
                    baseline_projects if isinstance(baseline_projects, dict) else {},
                ),
                "chats": self._merge_local_map(
                    latest_chats if isinstance(latest_chats, dict) else {},
                    current_chats if isinstance(current_chats, dict) else {},
                    baseline_chats if isinstance(baseline_chats, dict) else {},
                ),
            }
            tmp = self._path.with_name(
                f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            try:
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                tmp.replace(self._path)
                self._append_registry_audit(
                    revision=revision,
                    reason=reason,
                    project_mutations=project_mutations,
                    chat_mutations=chat_mutations,
                )
            finally:
                tmp.unlink(missing_ok=True)
        # Deep copy, not the payload itself: ``_state_payload`` embeds the live
        # container objects (``user_turn_timings``, ``user_turn_images``,
        # ``handover_messages``, ...), so keeping a reference makes the baseline
        # alias current state. The field-level diff in ``_merge_local_map`` then
        # sees ``before == value`` for every in-place mutation and drops it, and
        # the value never reaches disk. That silently lost per-turn send times
        # and image refs whenever they were the only change in a save.
        self._last_local_payload = copy.deepcopy(current)

    # ── Defaults and auto-discovery ──────────────────────────────────────

    def _workspace_names(self) -> tuple[str, ...]:
        """Return configured logical chat workspaces in config order."""
        names = tuple(self._config.workspace_names())
        return names or ("personal", "work")

    def _is_known_workspace(self, workspace: str) -> bool:
        return workspace in self._workspace_names()

    def _workspace_vault_root(self, workspace: str) -> Path:
        """Return the vault root for one logical workspace.

        Thin wrapper over ``CiaoConfig.workspace_vault_root``, which owns the
        registry and the legacy-path handling.
        """
        return self._config.workspace_vault_root(workspace)

    def _workspace_vault_display(self, workspace: str) -> str:
        """The workspace's vault as a path the model can use verbatim.

        Relative to the provider's cwd (`config.workspace_root`) when it sits
        underneath it, which is the normal layout — so the model gets
        `memory-vault/work`, not an absolute path it would have to trim.
        """
        if not workspace or not self._is_known_workspace(workspace):
            return ""
        try:
            root = self._workspace_vault_root(workspace)
        except ValueError:
            return ""
        try:
            return str(root.relative_to(Path(self._config.workspace_root)))
        except ValueError:
            return str(root)

    def _entity_index_is_per_root(self, workspace: str = "") -> bool:
        """Whether ``_entity_index_root`` resolved a per-root index.

        A per-root index covers exactly one workspace, so its entries need no
        prefix filtering; a shared one still does. Derived from the same
        ``agent_root`` receipt the root itself comes from, so the two answers
        cannot disagree.
        """
        if not workspace:
            return False
        try:
            return Path(self._config.agent_root(workspace)) != Path(
                self._config.workspace_root
            )
        except (AttributeError, ValueError):
            return False

    def _entity_index_root(self, workspace: str = "") -> Path:
        """Return the root that owns the vault entity index.

        Entity hints resolve against the INDEX.md that covers this chat, which
        is ``agent_vault_root(workspace)``: the ONE shared index before the
        re-rooting, and this root's own index after it. Deliberately not
        ``_workspace_vault_root`` — before the migration that is a subtree of the
        shared vault holding no index at all, which reads as "no entities" rather
        than failing. Workspace scoping within a shared index is still applied
        inside ``find_entities`` via its ``workspace`` argument.
        """
        if workspace:
            try:
                return self._config.agent_vault_root(workspace)
            except (AttributeError, ValueError):
                logger.debug("could not resolve the agent vault root for %r", workspace)
        return Path(self._config.vault_root)

    def _ensure_defaults(self) -> None:
        """Ensure each workspace has its auto-managed `General` project.

        General is pinned to ``order=0`` and bound to the vault folder
        ``projects/active/general/`` (created on demand below). It's where
        ad-hoc chats land and where scheduled automations run.

        Legacy migration: an older build of ciao maintained a separate
        ``Automations`` project per workspace. Any leftover ``Automations``
        project found at boot has its chats re-parented onto ``General`` and
        is then deleted, so schedule dispatch and the sidebar converge on a
        single home.
        """
        for ws in self._workspace_names():
            general = next(
                (
                    p
                    for p in self._projects.values()
                    if p.workspace == ws and p.name == "General"
                ),
                None,
            )
            if general is None:
                pid = chat_service._stable_vault_project_id(ws, "general")
                general = ProjectInfo(
                    project_id=pid,
                    name="General",
                    workspace=ws,
                    created_at=chat_service._now_iso(),
                    order=0,
                    vault_folder="general",
                )
                self._projects[pid] = general
            else:
                if general.order != 0:
                    general.order = 0
                if not general.vault_folder:
                    general.vault_folder = "general"
            self._ensure_general_vault_folder(ws)

        # Re-parent any leftover Automations chats onto General, then drop the
        # Automations project. One-shot migration: idempotent once the
        # Automations rows are gone.
        for ws in self._workspace_names():
            general = next(
                p
                for p in self._projects.values()
                if p.workspace == ws and p.name == "General"
            )
            for pid, proj in list(self._projects.items()):
                if proj.workspace != ws or proj.name != "Automations":
                    continue
                moved = 0
                for chat in self._chats.values():
                    if chat.project_id == pid:
                        chat.project_id = general.project_id
                        moved += 1
                self._projects.pop(pid, None)
                logger.info(
                    "Migrated Automations project (%s, %s): moved %d chat(s) to General",
                    ws, pid, moved,
                )
                self._events.publish({"type": "project_deleted", "project_id": pid})

        if not self._chats:
            # The onboarding chat lands in the first configured workspace —
            # single-workspace registries from the wizard carry the name the
            # user chose; the legacy fallback keeps this on "personal".
            ws_names = self._workspace_names()
            first_ws = ws_names[0] if ws_names else "personal"
            general = next(
                (
                    p
                    for p in self._projects.values()
                    if p.workspace == first_ws and p.name == "General"
                ),
                None,
            )
            if general is not None:
                self._create_onboarding_chat(general.project_id)

        self._save()

    def _create_onboarding_chat(self, project_id: str) -> None:
        import os
        vault_mode = os.environ.get("CIAO_VAULT_MODE", "scratch").strip().lower()
        project = self._projects.get(project_id)
        workspace_name = project.workspace if project is not None else "personal"
        vault_root = str(
            self._workspace_vault_root(project.workspace)
            if project is not None
            else self._config.vault_root
        )

        if vault_mode == "existing":
            title = "Connect Existing Vault 👋"
            user_msg = (
                f"Welcome to Ciaobot. You are Ciaobot, the user's personal agentic assistant.\n\n"
                f"The user has completed setup and pointed me to an **existing notes folder** at:\n"
                f"`{vault_root}`\n\n"
                f"This is logical workspace **{workspace_name}**. Do not create a second personal/work split inside it.\n\n"
                f"Your task is to onboard the user and adapt this existing folder into the current Ciaobot vault layout:\n"
                f"1. **Inventory first**: Scan the vault and report its top-level files and folders, separating user notes from Ciaobot-managed files (`.env`, `.runtime/`, `.claude/`, `AGENTS.md`). Do not assume an unfamiliar folder is disposable.\n"
                f"2. **Current structure**: The required vault roots are `MEMORY.md`, generated `INDEX.md`, `projects/active/`, `projects/completed/`, and `Logs/Chats/`. `Workspace/` is for cross-project learnings and memory proposals. Entity folders such as `People/`, `Ideas/`, `Resources/`, `Places/`, and `Documents/` are created only when useful. `Templates/` and `personal/`/`work/` are not required by the current layout.\n"
                f"3. **Preserve before reorganizing**: Existing files and content are the source of truth. Never delete or overwrite them. Reorganize only when the classification is clear: active projects go under `projects/active/<slug>/`, completed projects under `projects/completed/<slug>/`, people under `People/`, and reusable cross-project lessons under `Workspace/Learnings.md`. Leave ambiguous or unsupported material in place and report it. Use the existing Git history as the rollback point and keep a concise curation summary.\n"
                f"4. **Core-file hygiene**: Preserve an existing `MEMORY.md`; create it only if missing. Preserve the existing `AGENTS.md` and add any missing bounded regions without replacing user instructions: `<!-- ciao:memory:start cap=3000 -->` / `<!-- ciao:memory:end -->` and `<!-- ciao:profile:start cap=1375 -->` / `<!-- ciao:profile:end -->`.\n"
                f"5. **Initial memory curation**: Ask the user 2-3 important questions about their name, role, key people, and active projects. Then run an initial curation in this chat: search for duplicates, update the relevant project canonical docs, create durable person/entity notes only for confirmed facts, put reusable lessons in `Workspace/Learnings.md`, and put uncertain cross-project facts in `Workspace/Memory-Proposals.md`. Identity and communication style belong in the `ciao:profile` region; cross-project preferences and environment facts belong in `ciao:memory`; project-specific facts do not belong in bounded memory.\n"
                f"6. **Verify**: After the curation, run `ciao vault-index --write`, `ciao vault-lint`, and `ciao os-audit --json` when available. Report what was created, moved, left untouched, and any unresolved findings.\n"
                f"7. **Capabilities tour**: Once the interview and initial curation are done, offer a short guided tour of what Ciaobot can do (use the `ciao-capabilities` skill). Mention they can ask \"what can Ciaobot do?\" in any chat, anytime.\n\n"
                f"Introduce yourself to the user, tell them you've scanned their vault at `{vault_root}`, outline your findings, and ask the first onboarding questions to fill out their profile."
            )
            assistant_msg = (
                f"Hello! I am Ciaobot, your agentic second brain. 👋\n\n"
                f"I've connected workspace **{workspace_name}** to your existing folder at `{vault_root}`. "
                f"I'll first inspect what is already there, then help curate the clear, durable knowledge into Ciaobot's current structure while preserving the rest. "
                f"You can also ask me **\"what can Ciaobot do?\"** anytime for a tour of the app. "
                f"To get started, tell me: **What is your name, and what is your primary focus or life area right now?**"
            )
        else:
            title = "Welcome to Ciaobot! 👋"
            user_msg = (
                f"Welcome to Ciaobot. You are Ciaobot, the user's personal agentic assistant.\n\n"
                f"The user has completed setup and initialized a **new vault folder from scratch** at:\n"
                f"`{vault_root}`\n\n"
                f"This is logical workspace **{workspace_name}**. Do not create a second personal/work split inside it.\n\n"
                f"Your task is to bootstrap the current vault structure and core documentation:\n"
                f"1. **Current structure**: Use `MEMORY.md`, generated `INDEX.md`, `projects/active/`, `projects/completed/`, and `Logs/Chats/`. Create `Workspace/`, `People/`, `Ideas/`, `Resources/`, `Places/`, or `Documents/` only when the user's confirmed knowledge needs them. Do not create `personal/`, `work/`, or `Templates/` as required directories.\n"
                f"2. **Core files**: Setup has already seeded the workspace-level `AGENTS.md` and the vault-level `MEMORY.md`, `INDEX.md`, and General project. Preserve them and add only missing content. `AGENTS.md` must contain both bounded regions with their exact fenced markers: `<!-- ciao:memory:start cap=3000 -->` / `<!-- ciao:memory:end -->` and `<!-- ciao:profile:start cap=1375 -->` / `<!-- ciao:profile:end -->`.\n"
                f"3. **Onboarding interview and curation**: Ask the user 2-3 important questions about their name, role, key people, and active projects. Then route confirmed facts correctly: identity/style to the `ciao:profile` region, cross-project preferences/environment to `ciao:memory`, project facts to project canonical docs, people to `People/`, and reusable lessons to `Workspace/Learnings.md`. Put uncertain durable facts in `Workspace/Memory-Proposals.md` for review.\n"
                f"4. **Verify**: Run `ciao vault-index --write`, `ciao vault-lint`, and `ciao os-audit --json` when available, then report the resulting structure.\n"
                f"5. **Capabilities tour**: Once the interview and initial curation are done, offer a short guided tour of what Ciaobot can do (use the `ciao-capabilities` skill). Mention they can ask \"what can Ciaobot do?\" in any chat, anytime.\n\n"
                f"Introduce yourself to the user, explain that you are starting logical workspace **{workspace_name}** at `{vault_root}`, and ask the first onboarding questions to bootstrap their profile."
            )
            assistant_msg = (
                f"Hello! I am Ciaobot, your agentic second brain. 👋\n\n"
                f"Welcome! I've initialized logical workspace **{workspace_name}** at `{vault_root}` from scratch. "
                f"I'm ready to customize the current vault structure and curate your durable knowledge with you. "
                f"You can also ask me **\"what can Ciaobot do?\"** anytime for a tour of the app. "
                f"To begin, tell me: **What is your name, and what is your primary focus or life area right now?**"
            )

        # The haiku tier alias resolves against whichever provider the
        # workspace uses, so onboarding needs no backend of its own.
        chat = self.create_chat(project_id, title=title, model="haiku")
        chat.handover_context_pending = True
        chat.handover_messages = [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ]

    def _ensure_general_vault_folder(self, workspace: str) -> None:
        """Create ``projects/active/general/general.md`` if it doesn't exist.

        The PWA Files surface only lights up when the vault folder is present.
        The same-named ``.md`` is the project's main doc by convention; we
        seed a minimal frontmatter so vault tooling (INDEX, search) picks it
        up. Idempotent.
        """
        root = self._vault_active_root(workspace)
        folder = root / "general"
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create General vault folder %s: %s", folder, exc)
            return
        target = folder / "general.md"
        if target.exists():
            return
        # Don't clobber an existing README; the discovery code falls back to
        # `<stem>/<stem>.md` only when no README is present, but the user
        # still expects a same-named doc to exist per the convention.
        body = (
            "---\n"
            "name: General\n"
            f"workspace: {workspace}\n"
            "type: project\n"
            "status: active\n"
            "tags: [project, general]\n"
            "---\n\n"
            "# General\n\n"
            "Catch-all home for ad-hoc chats and scheduled automations.\n"
        )
        try:
            target.write_text(body, encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not seed %s: %s", target, exc)

    def _vault_active_root(self, workspace: str) -> Path:
        """Return the active/ directory for the given workspace, no existence check.

        Workspace roots come from ``CiaoConfig.workspaces``. Legacy
        ``personal`` and ``work`` keep their historical location under
        ``CIAO_VAULT_ROOT``.
        """
        return self._workspace_vault_root(workspace) / "projects" / "active"

    def _vault_completed_root(self, workspace: str) -> Path:
        return self._workspace_vault_root(workspace) / "projects" / "completed"

    def _display_path(self, path: Path) -> str:
        """Return a UI/file-viewer path for workspace or external vault files."""
        try:
            return str(path.relative_to(self._config.workspace_root))
        except ValueError:
            return str(path)

    def _iter_vault_entries(self, workspace: str, root: Path) -> list[tuple[str, Path, Path | None]]:
        """Yield ``(stem, entry_path, readme_path)`` for each project under ``root``.

        Both workspaces use the same convention: a project is a directory.
        Readme is ``<entry>/README.md`` if present, else ``<entry>/<entry>.md``,
        else ``None``. Personal single-file projects (``Projects/active/Foo.md``)
        used to be supported; they're auto-promoted to ``Foo/Foo.md`` at startup
        so this discovery path stays uniform.

        Hidden entries (``.``-prefixed) and ``.gitkeep`` are skipped.
        """
        out: list[tuple[str, Path, Path | None]] = []
        if not root.is_dir():
            return out
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or entry.name == ".gitkeep":
                continue
            if not entry.is_dir():
                continue
            readme = entry / "README.md"
            if not readme.exists():
                # Fall back to <entry>/<entry>.md, the convention for projects
                # promoted from the old single-file form. Either provides the
                # frontmatter we read below.
                readme = entry / f"{entry.name}.md"
            out.append((entry.name, entry, readme if readme.exists() else None))
        return out

    def _promote_single_file_personal_projects(self) -> None:
        """Auto-promote any stray ``Projects/active/<stem>.md`` into folder form.

        Single-file personal projects used to be a supported shape. They
        exposed no Files section (no folder to host attachments) and forced
        every consumer of the vault to handle dual-form. We've normalised
        every existing project to ``<stem>/<stem>.md``; this helper keeps
        that invariant true even if a stray ``.md`` ever lands at the top
        of ``Projects/active/`` or ``Projects/completed/`` again (e.g. a
        chat asks Claude to create a project file directly). Runs on
        every manager init: cheap iterdir, idempotent.
        """
        for root in (
            self._vault_active_root("personal"),
            self._vault_completed_root("personal"),
        ):
            if not root.is_dir():
                continue
            for entry in list(root.iterdir()):
                if not entry.is_file() or entry.suffix != ".md":
                    continue
                if entry.name.startswith(".") or entry.name == ".gitkeep":
                    continue
                stem = entry.stem
                target_dir = root / stem
                target = target_dir / f"{stem}.md"
                # Refuse to clobber an existing folder/file.
                if target.exists():
                    logger.warning(
                        "Cannot promote %s: %s already exists. Resolve manually.",
                        entry, target,
                    )
                    continue
                if target_dir.exists() and not target_dir.is_dir():
                    logger.warning(
                        "Cannot promote %s: %s exists and is not a directory.",
                        entry, target_dir,
                    )
                    continue
                target_dir.mkdir(parents=True, exist_ok=True)
                entry.rename(target)
                logger.info("Promoted single-file personal project %s -> %s", entry, target)

    @staticmethod
    def _safe_yaml_frontmatter(text: str, source: Path) -> dict | None:
        """Parse YAML frontmatter with a tolerant fallback.

        Telegram transcripts (and other auto-generated archive files) often
        write unquoted strings that contain colons, asterisks, or en-dashes
        into single-value fields like ``context:``. ``yaml.safe_load`` rejects
        those, which then swallows the whole transcript on read.

        Fallback strategy: if strict parsing fails, locate the ``context:``
        line and recover it as a plain string so the transcript still
        indexes. The other fields default safely.
        """
        try:
            fm = yaml.safe_load(text)
            if isinstance(fm, dict):
                return fm
        except Exception:
            pass
        # Tolerant recovery: pull `context:` (the most failure-prone field
        # in transcript frontmatter) as raw text and merge it with an empty
        # dict. Other fields fall back to defaults at the call site.
        m = re.search(r"^context:\s*(.+?)\s*$", text, flags=re.MULTILINE)
        if not m:
            return None
        # Strip a single leading/trailing quote if present.
        raw = m.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        return {"context": raw}

    def _read_project_metadata(
        self, readme: Path | None, fallback_name: str
    ) -> tuple[str, str]:
        """Parse ``name`` and ``description`` from the readme's YAML
        frontmatter. Returns ``(name, context)`` with sensible fallbacks when
        the readme is missing or its frontmatter is unparseable.
        """
        if readme is None or not readme.exists():
            return fallback_name, ""
        try:
            text = readme.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read readme %s: %s", readme, exc)
            return fallback_name, ""
        if not text.startswith("---"):
            return fallback_name, ""
        end = text.find("---", 3)
        if end <= 0:
            return fallback_name, ""
        try:
            fm = yaml.safe_load(text[3:end])
        except Exception as exc:
            logger.warning("Failed to parse frontmatter in %s: %s", readme, exc)
            return fallback_name, ""
        if not isinstance(fm, dict):
            return fallback_name, ""
        name = fm.get("name") or fm.get("title") or fallback_name
        context = fm.get("description", "") or ""
        return str(name), str(context)

    def _read_project_metadata_cached(
        self, readme: Path | None, fallback_name: str
    ) -> tuple[str, str]:
        """``_read_project_metadata`` memoised on the readme's (mtime, size).

        Discovery runs on every ``list_projects`` call, and the sidebar calls
        that often. Keying on the stat stamp means a doc edited by hand, by an
        agent, or by ``_write_project_context`` is still picked up on the next
        pass - the write changes the stamp, which invalidates the entry.
        """
        if readme is None:
            return fallback_name, ""
        try:
            stat = readme.stat()
        except OSError:
            return fallback_name, ""
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = self._doc_meta_cache.get(str(readme))
        if cached is not None and cached[0] == stamp:
            return cached[1]
        result = self._read_project_metadata(readme, fallback_name)
        self._doc_meta_cache[str(readme)] = (stamp, result)
        return result

    def _project_doc_file(self, project: ProjectInfo) -> Path | None:
        """Absolute path to *project*'s canonical doc, or ``None``.

        Re-derives the path from ``vault_folder`` using the same convention as
        ``_iter_vault_entries`` (README.md first, then ``<stem>/<stem>.md``)
        rather than resolving ``vault_doc_path``, which is a display string
        that may be absolute for vaults outside the workspace root.
        """
        folder_name = project.vault_folder
        if not folder_name or not chat_service._VAULT_FOLDER_RE.fullmatch(folder_name):
            return None
        try:
            active_root = self._vault_active_root(project.workspace).resolve()
            folder = (active_root / folder_name).resolve()
        except (OSError, ValueError):
            return None
        # A symlinked project folder could otherwise point the write anywhere.
        if not folder.is_relative_to(active_root) or not folder.is_dir():
            return None
        for candidate in (folder / "README.md", folder / f"{folder_name}.md"):
            if candidate.is_file():
                return candidate
        return None

    def _write_project_context(self, project: ProjectInfo) -> bool:
        """Push ``project.context`` into the canonical doc's ``description:``.

        The doc is the source of truth for context - it is what the archive
        time fold in ``project_doc_update`` and hand edits write to - so an
        edit made in the PWA has to land there or the two silently diverge,
        which is exactly what this pairs with the discovery-side sync to stop.

        Best effort: a project with no vault folder (or no doc inside it)
        keeps its context in the projects registry alone, as before.
        """
        doc = self._project_doc_file(project)
        if doc is None:
            return False
        try:
            current = doc.read_text(encoding="utf-8")
            updated = chat_service._set_frontmatter_description(current, project.context)
            if updated is None or updated == current:
                return False
            doc.write_text(updated, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to sync context into %s: %s", doc, exc)
            return False
        logger.info("Synced project context into %s", doc)
        return True

    def _parse_transcript_file(self, path: Path) -> dict | None:
        """Parse frontmatter and first prompt context from a transcript markdown file."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to read transcript %s: %s", path, exc)
            return None

        # Parse frontmatter
        if not text.startswith("---"):
            return None
        end = text.find("---", 3)
        if end <= 0:
            return None
        fm = self._safe_yaml_frontmatter(text[3:end], path)
        if fm is None:
            logger.warning("Failed to parse frontmatter in %s", path)
            return None
        if not isinstance(fm, dict):
            return None

        workspace, project_name = self._extract_context_target(text)

        title = fm.get("context") or fm.get("title") or "Archived Chat"
        model = fm.get("active_model") or fm.get("selected_model") or fm.get("last_effective_model") or "opus"
        provider = fm.get("provider") or "claude"
        session_id = fm.get("session_id") or ""

        created_at_val = fm.get("started") or ""
        if isinstance(created_at_val, datetime):
            created_at = created_at_val.isoformat().replace("+00:00", "Z")
        else:
            created_at = str(created_at_val)

        ended_at_val = fm.get("ended") or created_at_val or ""
        if isinstance(ended_at_val, datetime):
            ended_at = ended_at_val.isoformat().replace("+00:00", "Z")
        else:
            ended_at = str(ended_at_val)

        return {
            "workspace": workspace,
            "project_name": project_name,
            "title": title,
            "model": model,
            "provider": provider,
            "session_id": session_id,
            "created_at": created_at,
            "ended_at": ended_at,
        }

    def _extract_context_target(self, text: str) -> tuple[str, str]:
        """Extract logical workspace and project reference from hidden context."""

        known = self._workspace_names()
        workspace = "personal" if "personal" in known else known[0]
        context_match = re.search(r"\[CONTEXT:\s*([A-Za-z0-9._-]+)\b", text)
        if context_match and context_match.group(1) in known:
            workspace = context_match.group(1)
        else:
            for candidate in known:
                if re.search(
                    rf"(?:^|[/\\]){re.escape(candidate)}[/\\]projects[/\\]",
                    text,
                    flags=re.IGNORECASE,
                ):
                    workspace = candidate
                    break

        project_name = "General"
        project_match = re.search(r'\[Project:\s*["\']?(.*?)["\']?\]', text)
        if project_match:
            project_name = project_match.group(1).strip()
        else:
            canonical_match = re.search(
                r"(?:^|[/\\])projects[/\\]active[/\\]([^/\\\]\s]+)",
                text,
                flags=re.IGNORECASE,
            )
            if canonical_match:
                project_name = canonical_match.group(1).strip()
        return workspace, project_name

    def _project_reference_map(self) -> dict[tuple[str, str], str]:
        """Index both human project names and canonical vault-folder slugs."""

        project_map: dict[tuple[str, str], str] = {}
        for project in self._projects.values():
            name_key = chat_service._project_reference_key(project.name)
            if name_key:
                project_map.setdefault((name_key, project.workspace), project.project_id)
        # A canonical vault slug wins over a colliding display-name alias.
        for project in self._projects.values():
            folder_key = chat_service._project_reference_key(project.vault_folder)
            if folder_key:
                project_map[(folder_key, project.workspace)] = project.project_id
        return project_map

    def _resolve_project_reference(self, workspace: str, value: str) -> str:
        project_id = self._project_reference_map().get(
            (chat_service._project_reference_key(value), workspace), ""
        )
        if project_id:
            return project_id
        general = next(
            (
                project.project_id
                for project in self._projects.values()
                if project.workspace == workspace and project.name == "General"
            ),
            "",
        )
        return general

    def _discover_archived_chats(self) -> None:
        """Scan the vault's archived transcripts and import missing chats."""
        chats_root = self._config.logs_root / "Chats"
        if not chats_root.is_dir():
            return

        new_chats_discovered = False
        pruned_chats = False

        # 1. Prune archived chats whose transcript files no longer exist
        for cid, chat in list(self._chats.items()):
            if chat.archived and chat.archive_path:
                full_path = self._config.workspace_root / chat.archive_path
                if not full_path.exists():
                    self._chats.pop(cid)
                    pruned_chats = True
                    logger.info("Pruned archived chat %s (transcript file no longer exists)", cid)

        # 2. Discover new archived chats from transcripts
        for chat_dir in chats_root.iterdir():
            if not chat_dir.is_dir() or not chat_dir.name.startswith("chat-"):
                continue
            chat_id = chat_dir.name

            # Skip if already in database
            if chat_id in self._chats:
                continue

            # Find all markdown transcripts in provider subdirectories
            transcripts: list[Path] = []
            for sub in chat_dir.iterdir():
                if sub.is_dir():
                    transcripts.extend(sub.glob("*.md"))

            if not transcripts:
                continue

            # Use the latest transcript file (sorted by name/timestamp)
            transcripts.sort()
            transcript_path = transcripts[-1]

            metadata = self._parse_transcript_file(transcript_path)
            if not metadata:
                continue

            ws = metadata["workspace"]
            proj_name = metadata["project_name"]
            proj_id = self._resolve_project_reference(ws, proj_name)

            if not proj_id:
                # If no project ID could be resolved, skip
                continue

            # Reconstruct archive path relative to workspace root
            try:
                rel_archive_path = str(transcript_path.relative_to(self._config.workspace_root))
            except ValueError:
                rel_archive_path = str(transcript_path)

            chat_info = ChatInfo(
                chat_id=chat_id,
                project_id=proj_id,
                title=metadata["title"],
                model=metadata["model"],
                provider=metadata["provider"],
                session_id=metadata["session_id"],
                created_at=metadata["created_at"],
                archived=True,
                last_activity_at=metadata["ended_at"],
                last_read_at=metadata["ended_at"],
                archive_path=rel_archive_path,
            )
            self._chats[chat_id] = chat_info
            new_chats_discovered = True
            logger.info("Imported archived chat %s under project %s", chat_id, proj_id)

        if new_chats_discovered or pruned_chats:
            self._save(reason="archived_transcript_discovery")

    def _claude_session_exists(
        self, session_id: str, *, agent_root: Path | None = None
    ) -> bool:
        if not session_id:
            return False
        # Claude session ids are UUIDs; anything else (e.g. an opencode
        # ``ses_*`` id) can never have a matching ``<session>.jsonl``, so
        # skip the filesystem probe entirely.
        try:
            uuid.UUID(session_id)
        except ValueError:
            return False
        root = agent_root if agent_root is not None else self._config.workspace_root
        projects_dir = _claude_projects_dir(root)
        if (projects_dir / f"{session_id}.jsonl").exists():
            return True
        # The cross-cwd fallback stays for every caller. The projects dir is a
        # slug of the cwd, so a session recorded under a different cwd is only
        # findable this way. It is served from a cached directory listing (see
        # transcripts._global_session_matches) so N probes cost one walk,
        # not N — the uncached glob used to stall the event loop for minutes
        # on installs with hundreds of stale project slug dirs. A miss
        # refreshes the listing (rate-limited) so a session created under
        # another cwd inside the TTL window is not reported absent.
        try:
            return bool(_global_session_matches(session_id))
        except OSError:
            return False

    def _recover_orphaned_active_chats(self) -> None:
        """Rebuild missing active rows from surviving runtime transcripts.

        Recovery is intentionally evidence-based. A Claude transcript must
        still have its provider session blob, while other providers require an
        audit record showing that the chat previously existed and was not
        explicitly deleted. This avoids reviving old transcripts left behind
        by versions that did not fully clean up deletions.
        """

        recovered = 0
        for (
            context_key,
            provider_name,
            transcript,
        ) in self._transcripts.all_current_transcripts():
            if not context_key.startswith("chat-") or context_key in self._chats:
                continue
            if not isinstance(transcript, dict):
                continue
            turns = transcript.get("turns")
            if not isinstance(turns, list) or not turns:
                continue

            provider = str(transcript.get("provider") or provider_name or "claude")
            session_id = str(transcript.get("session_id") or "")
            audit_status = self._audited_chat_status(context_key)
            if audit_status == "deleted":
                continue
            if provider == "claude":
                # No chat in hand here, only a transcript row, so there is no
                # workspace to resolve an agent root from. Defaults to
                # workspace_root, which is what every root resolves to until the
                # re-rooting release.
                if (
                    not self._claude_session_exists(session_id)
                    and audit_status != "present"
                ):
                    continue
            elif audit_status != "present":
                continue

            prompts = "\n".join(
                str(turn.get("prompt") or "")
                for turn in turns
                if isinstance(turn, dict)
            )
            workspace, project_ref = self._extract_context_target(prompts)
            project_id = self._resolve_project_reference(workspace, project_ref)
            if not project_id:
                continue

            valid_turns = [turn for turn in turns if isinstance(turn, dict)]
            if not valid_turns:
                continue
            first_prompt = str(valid_turns[0].get("prompt") or "")
            visible_prompt = re.sub(
                r"(?s)^\[CIAO_CONTEXT_BEGIN\].*?\[CIAO_CONTEXT_END\]\s*",
                "",
                first_prompt,
            ).strip()
            title = str(transcript.get("context_label") or "").strip()
            if not title or title == "New Chat":
                # Input is always non-empty, so the fallback returns a str.
                title = cast(str, chat_service._fallback_title(visible_prompt or "Recovered Chat"))
            created_at = str(transcript.get("started_at") or "") or chat_service._now_iso()
            updated_at = str(transcript.get("updated_at") or created_at)
            mode: BridgeMode = cast(
                BridgeMode, str(valid_turns[-1].get("mode") or self._config.claude_mode)
            )
            if mode not in {"normal", "plan", "auto", "bypass"}:
                mode = self._config.claude_mode

            chat = ChatInfo(
                chat_id=context_key,
                project_id=project_id,
                title=title,
                model=str(
                    transcript.get("selected_model")
                    or self._config.claude_default_model
                ),
                provider=provider,
                mode=mode,
                session_id=session_id,
                created_at=created_at,
                archived=False,
                last_activity_at=updated_at,
                last_read_at=updated_at,
                user_turn_count=len(valid_turns),
            )
            self._chats[context_key] = chat
            recovered += 1
            logger.warning(
                "Recovered orphaned active chat %s under project %s from runtime transcript",
                context_key,
                project_id,
            )
            self._events.publish({"type": "chat_created", "chat": chat.to_dict()})

        if recovered:
            self._save(reason="orphaned_active_chat_recovery")

    def _reconcile_half_archived_chats(self) -> None:
        """Heal chats stuck in a provably-impossible half-archived state.

        A chat that was archived (its transcript moved to the vault and, for
        Claude, its SDK session blob deleted) can end up back with
        ``archived=False`` if an older ``web_projects.json`` was reloaded
        after a crash/restart — the archive side effects already happened but
        the registry flag reverted. ``new_session`` now refuses to resurrect
        archived chats in place, so this can no longer be *created*, but
        existing corrupt rows never self-correct: the chat reappears in the
        sidebar and menu bar indefinitely (an "archived chat that came back").

        The reconciled state is unambiguous, so the guard has no false
        positives: a live chat always has a current transcript, and a fresh
        ``new_session`` resets ``session_id`` to "" (excluded below). Only a
        reverted archive leaves a non-empty ``session_id`` whose backing data
        is already gone while an archive sits in the vault.
        """
        healed = 0
        for chat_id, chat in self._chats.items():
            if chat.archived or not chat.session_id:
                continue
            ctx = ChatContext.for_web(chat_id)
            if self._transcripts.current_path(ctx, chat.provider).exists():
                continue  # live transcript -> genuinely active, leave alone
            archive_dir = self._transcripts.archive_dir(ctx, chat.provider)
            if not archive_dir.is_dir() or not any(archive_dir.glob("*.md")):
                continue  # never archived -> not the corrupt state
            if chat.provider == "claude" and self._claude_session_exists(
                chat.session_id, agent_root=self._agent_root_for_chat(chat.chat_id)
            ):
                continue  # session blob still present -> not archived
            chat.archived = True
            if not chat.archive_path:
                latest = max(
                    archive_dir.glob("*.md"), key=lambda p: p.name, default=None
                )
                if latest is not None:
                    try:
                        chat.archive_path = str(
                            latest.relative_to(self._config.workspace_root)
                        )
                    except ValueError:
                        chat.archive_path = str(latest)
            healed += 1
            logger.warning(
                "Reconciled half-archived chat %s: archive present but "
                "registry showed active; marking archived.",
                chat_id,
            )
        if healed:
            self._save(reason="half_archived_reconciliation")

    def _general_project_for(self, workspace: str) -> "ProjectInfo | None":
        for project in self._projects.values():
            if project.workspace == workspace and project.name == "General":
                return project
        return None

    def _rehome_orphaned_chats(self) -> None:
        """Re-parent chats whose project no longer exists onto a valid General.

        A chat whose ``project_id`` doesn't resolve to a live project (e.g. it
        was created in the throwaway bootstrap workspace before setup, or its
        project/workspace was removed) is invisible in the PWA — which nests
        chats under project → workspace — yet still shows in the menu bar,
        which lists chats flat. That split leaves the chat unreachable for the
        user. Re-home it to the General project of a configured workspace so it
        becomes reachable (and archivable) instead of stranded.
        """
        workspaces = self._workspace_names()
        if not workspaces:
            return
        fallback = self._general_project_for(workspaces[0])
        rehomed = 0
        for chat_id, chat in self._chats.items():
            if chat.project_id in self._projects:
                continue
            target = fallback
            if target is None:
                continue
            logger.warning(
                "Re-homing orphaned chat %s (project %s no longer exists) -> %s",
                chat_id, chat.project_id, target.project_id,
            )
            chat.project_id = target.project_id
            rehomed += 1
        if rehomed:
            self._save(reason="orphaned_chat_rehome")

    def _discover_vault_projects(self) -> None:
        """Auto-discover projects from each workspace's ``projects/active/`` tree.

        Both workspaces use the folder convention: a project is
        ``<workspace_root>/projects/active/<stem>/`` with an optional
        ``README.md`` and/or ``<stem>.md`` carrying the frontmatter. Personal
        single-file projects (``Projects/active/Foo.md``) used to exist; we
        run a migration on every init to promote any stray ones into folder
        form so this discovery path stays uniform.

        Also prunes auto-discovered projects whose vault entry has been
        deleted, as long as the project has zero chats. This lets the user
        clean up a misnamed project by simply deleting the folder/file — the
        PWA entry disappears on the next sidebar fetch. Projects with any
        chats (active or archived) are preserved so vault moves don't discard
        history.
        """
        # Promote any leftover single-file personal projects before we look
        # at the tree: keeps discovery and the Files section happy without
        # any conditional branching downstream.
        self._promote_single_file_personal_projects()

        # Build the union of stems present across configured workspaces' active dirs.
        # Used for pruning orphan PWA projects whose vault entry has been
        # removed. Pruning is workspace-scoped to avoid cross-workspace clashes.
        per_workspace_entries: dict[str, list[tuple[str, Path, Path | None]]] = {}
        per_workspace_stems: dict[str, set[str]] = {}
        workspace_names = self._workspace_names()
        for ws in workspace_names:
            root = self._vault_active_root(ws)
            entries = self._iter_vault_entries(ws, root)
            per_workspace_entries[ws] = entries
            per_workspace_stems[ws] = {stem for stem, _, _ in entries}

        # ── Prune ────────────────────────────────────────────────────────
        orphan_ids = [
            pid
            for pid, proj in self._projects.items()
            if proj.vault_folder
            and proj.vault_folder not in per_workspace_stems.get(proj.workspace, set())
        ]
        for pid in orphan_ids:
            has_any_chats = any(c.project_id == pid for c in self._chats.values())
            if has_any_chats:
                continue
            proj = self._projects.pop(pid, None)
            if proj is None:
                continue
            logger.info(
                "Pruned orphan vault project %s (entry '%s' no longer exists)",
                proj.name,
                proj.vault_folder,
            )
            self._events.publish({
                "type": "project_deleted",
                "project_id": pid,
            })
        if orphan_ids:
            self._save()

        existing_stems_by_ws: dict[str, set[str]] = {
            ws: set() for ws in workspace_names
        }
        for p in self._projects.values():
            if p.vault_folder and p.workspace in existing_stems_by_ws:
                existing_stems_by_ws[p.workspace].add(p.vault_folder)

        # Index manually-created projects (no vault_folder yet) by name so we
        # can adopt a matching vault entry instead of creating a duplicate.
        # Scoped per-workspace because work and personal can share names.
        # The Memory project is not a candidate: it is app-owned and has no
        # vault entry, so a vault folder that happens to share its name would
        # bind the system project to the wrong doc.
        unbound_by_name: dict[str, dict[str, ProjectInfo]] = {
            ws: {} for ws in workspace_names
        }
        for p in self._projects.values():
            if p.kind == "memory":
                continue
            if p.workspace in unbound_by_name and not p.vault_folder:
                unbound_by_name[p.workspace][p.name] = p

        # ── Discover ─────────────────────────────────────────────────────
        for ws in workspace_names:
            for stem, entry_path, readme in per_workspace_entries[ws]:
                if stem in existing_stems_by_ws[ws]:
                    # Already in our index — refresh the vault doc path so the
                    # Files section and canonical-doc link stay accurate even
                    # if the readme moved, and re-read the doc's description so
                    # a context edited in the file (by hand, or by the archive
                    # time insights fold) reaches the injected preamble. This
                    # branch used to skip the readme entirely, which is how the
                    # two drifted apart with nothing to pull them back.
                    existing = next(
                        (p for p in self._projects.values()
                         if p.vault_folder == stem and p.workspace == ws),
                        None,
                    )
                    if existing is None or readme is None:
                        continue
                    changed = False
                    doc_path = self._display_path(readme)
                    if existing.vault_doc_path != doc_path:
                        existing.vault_doc_path = doc_path
                        changed = True
                    _, context = self._read_project_metadata_cached(readme, stem)
                    # An empty description never clears a context typed before
                    # the doc grew one; the next save pushes it into the file.
                    if context and context != existing.context:
                        existing.context = context
                        changed = True
                    # Only publish on a real change: this runs on every
                    # list_projects() call, and an unconditional event would
                    # have every sidebar refresh look like a project edit.
                    if changed:
                        self._events.publish({
                            "type": "project_updated",
                            "project": existing.to_dict(),
                        })
                    continue
                name, context = self._read_project_metadata_cached(readme, stem)

                existing = unbound_by_name[ws].get(name) or unbound_by_name[ws].get(stem)
                if existing:
                    existing.vault_folder = stem
                    if readme is not None:
                        existing.vault_doc_path = self._display_path(readme)
                    # First bind is the one moment the registry wins: a context
                    # typed in the PWA before the folder existed is deliberate,
                    # and the doc it's adopting was most likely scaffolded. Push
                    # it into the doc rather than dropping it, so the two are
                    # already in agreement by the time the doc-wins rule above
                    # takes over on every later pass.
                    if existing.context:
                        self._write_project_context(existing)
                    elif context:
                        existing.context = context
                    logger.info(
                        "Linked vault entry '%s' to existing %s project %s (%s)",
                        stem, ws, existing.name, existing.project_id,
                    )
                    self._events.publish({
                        "type": "project_updated",
                        "project": existing.to_dict(),
                    })
                    continue

                pid = chat_service._stable_vault_project_id(ws, stem)
                project = ProjectInfo(
                    project_id=pid,
                    name=name,
                    workspace=ws,
                    context=context,
                    created_at=chat_service._now_iso(),
                    order=len(self._projects),
                    vault_folder=stem,
                    vault_doc_path=self._display_path(readme) if readme is not None else "",
                )
                self._projects[pid] = project
                logger.info("Auto-discovered %s project: %s", ws, name)
                self._events.publish({
                    "type": "project_created",
                    "project": project.to_dict(),
                })

        self._discover_archived_chats()
        self._save()

    # ── Project CRUD ─────────────────────────────────────────────────────

    def list_projects(self, workspace: str | None = None) -> list[ProjectInfo]:
        # Re-run vault auto-discovery on every list call. Without this, a
        # work project folder created mid-session (e.g. via a chat asking
        # Claude to set up a new project) doesn't show up in the sidebar
        # until the server restarts — and even then, the project_created
        # event published during init fires before any WS client has
        # subscribed, so the browser still misses it until a hard refetch.
        # Cost: one iterdir() on memory-vault/work/projects/active/, which
        # is negligible for realistic vault sizes.
        self._discover_vault_projects()
        projects = list(self._projects.values())
        if workspace:
            projects = [p for p in projects if p.workspace == workspace]
        projects.sort(key=lambda p: (p.workspace, p.order, p.name))
        return projects

    def get_project(self, project_id: str) -> ProjectInfo | None:
        return self._projects.get(project_id)

    def create_project(
        self,
        name: str,
        workspace: str,
        context: str = "",
    ) -> ProjectInfo:
        pid = f"proj-{chat_service._uuid8()}"
        project = ProjectInfo(
            project_id=pid,
            name=name,
            workspace=workspace,
            context=context,
            created_at=chat_service._now_iso(),
            order=len(self._projects),
        )
        self._projects[pid] = project
        self._save()
        self._events.publish({
            "type": "project_created",
            "project": project.to_dict(),
        })
        return project

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        context: str | None = None,
        vault_folder: str | None = None,
    ) -> ProjectInfo | None:
        project = self._projects.get(project_id)
        if project is None:
            return None
        if name is not None:
            project.name = name
        context_changed = context is not None and context != project.context
        if context is not None:
            project.context = context
        if vault_folder is not None:
            # Reject anything that could escape projects/active/<folder>/.
            # Empty string clears the binding; a non-empty value must be a
            # single safe folder name (no separators, no traversal, no NUL).
            if vault_folder and not chat_service._VAULT_FOLDER_RE.fullmatch(vault_folder):
                raise ValueError(
                    f"Invalid vault_folder {vault_folder!r}: "
                    "must match [A-Za-z0-9._-]+ with no path separators."
                )
            project.vault_folder = vault_folder
            # Recompute the canonical doc pointer from the new binding so it
            # can't go stale after a rename (issue #421). vault_doc_path is
            # otherwise only refreshed by the discovery pass in
            # list_projects(), while get_project()/project_get serve the
            # stored value directly. Clears to "" when the binding is
            # cleared or no canonical doc exists under the new folder yet.
            doc = self._project_doc_file(project)
            project.vault_doc_path = self._display_path(doc) if doc is not None else ""
        # Mirror the context into the canonical doc's frontmatter so the two
        # can't drift. Deliberately after the vault_folder branch, so a call
        # that rebinds and re-describes in one go writes to the new doc.
        if context_changed:
            self._write_project_context(project)
        self._save()
        self._events.publish({
            "type": "project_updated",
            "project": project.to_dict(),
        })
        return project

    def reorder_projects(
        self, workspace: str, ordered_ids: list[str]
    ) -> list[ProjectInfo]:
        """Persist a new sidebar order for *workspace*'s projects.

        ``ordered_ids`` is the desired top-to-bottom sequence. Projects in the
        workspace that are omitted keep their existing relative order after the
        listed ones. Each project's ``order`` is rewritten to its final index
        so the ``workspaceProjects`` sort (order, then name) reflects the drag.
        Ids for other workspaces or unknown ids are ignored.
        """
        ws_projects = [p for p in self._projects.values() if p.workspace == workspace]
        by_id = {p.project_id: p for p in ws_projects}
        seen: set[str] = set()
        sequence: list[ProjectInfo] = []
        for pid in ordered_ids:
            project = by_id.get(pid)
            if project is not None and pid not in seen:
                sequence.append(project)
                seen.add(pid)
        # Anything not named stays, in its current order, after the listed set.
        for project in sorted(ws_projects, key=lambda p: (p.order, p.name)):
            if project.project_id not in seen:
                sequence.append(project)
        # General is auto-managed and re-pinned to order 0 at every boot
        # (_ensure_defaults); keep it first here so a reorder doesn't snap back.
        sequence.sort(key=lambda p: p.name != "General")
        for index, project in enumerate(sequence):
            project.order = index
        self._save()
        self._events.publish({
            "type": "projects_reordered",
            "workspace": workspace,
            "order": [p.project_id for p in sequence],
        })
        return sequence

    def complete_project(self, project_id: str) -> dict:
        """Move a project's vault entry to completed/, then delete the PWA project.

        Both workspaces share the same convention: a vault entry is a folder
        ``projects/active/<stem>/`` that gets moved to
        ``projects/completed/<stem>/``. After the move, ``status: active`` in
        the main project markdown's frontmatter is rewritten to
        ``status: completed``.

        Returns a dict with ``ok``, ``vault_moved`` (bool), and ``vault_folder`` (str | None).
        """
        if project_id == _CC_CLI_PROJECT_ID:
            raise ValueError("The Claude Code CLI project cannot be completed.")
        project = self._projects.get(project_id)
        if project is None:
            raise ValueError("Project not found.")

        vault_moved = False
        vault_folder = project.vault_folder or None

        if vault_folder and self._is_known_workspace(project.workspace):
            # Defence in depth: even though update_project validates
            # vault_folder, double-check before any filesystem operation.
            if not chat_service._VAULT_FOLDER_RE.fullmatch(vault_folder):
                raise ValueError(
                    f"Invalid vault_folder {vault_folder!r} stored on project."
                )
            active_root = self._vault_active_root(project.workspace).resolve()
            completed_root = self._vault_completed_root(project.workspace).resolve()

            src = (active_root / vault_folder).resolve()
            dst = (completed_root / vault_folder).resolve()

            if src.exists() and src.is_dir():
                # Refuse to act if the resolved paths escape their roots
                # (handles symlinks pointing outside the vault).
                if not src.is_relative_to(active_root) or not dst.is_relative_to(completed_root):
                    raise ValueError(
                        f"vault_folder {vault_folder!r} resolves outside the projects tree."
                    )
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))

                # Update status frontmatter in the main project markdown
                # (<dst>/<stem>.md). Falls back to README.md if that's where
                # the frontmatter lives.
                for candidate in (dst / f"{vault_folder}.md", dst / "README.md"):
                    if candidate.exists():
                        text = candidate.read_text()
                        text = re.sub(r"(?m)^(status:\s*)active\s*$", r"\1completed", text)
                        candidate.write_text(text)
                        break
                vault_moved = True

        # Use the internal remover: by this point the vault entry has been
        # moved (or was already absent), so the public delete_project guard
        # against vault-backed deletion would either misfire or block us.
        self._remove_project(project_id)
        return {"ok": True, "vault_moved": vault_moved, "vault_folder": vault_folder}

    def list_completed_projects(self, workspace: str | None = None) -> list[dict]:
        """List completed projects by scanning the ``projects/completed/`` tree.

        Completed projects are not PWA projects: ``complete_project`` deletes
        the PWA entry and leaves only the vault folder under ``completed/``.
        This is a read-only scan of those folders, returning the metadata the
        restore UI needs. Pass ``workspace`` to scope to one workspace; omit
        to list both.

        Each entry is ``{stem, name, context, workspace, vault_doc_path}``.
        """
        workspaces = self._workspace_names() if workspace is None else (workspace,)
        out: list[dict] = []
        for ws in workspaces:
            if not self._is_known_workspace(ws):
                continue
            root = self._vault_completed_root(ws)
            for stem, _entry_path, readme in self._iter_vault_entries(ws, root):
                name, context = self._read_project_metadata(readme, stem)
                out.append({
                    "stem": stem,
                    "name": name,
                    "context": context,
                    "workspace": ws,
                    "vault_doc_path": self._display_path(readme) if readme is not None else "",
                })
        out.sort(key=lambda d: (d["workspace"], d["name"].lower()))
        return out

    def restore_project(self, workspace: str, stem: str) -> dict:
        """Restore a completed project: move its folder back to ``active/``.

        Reverses ``complete_project``: moves ``completed/<stem>/`` to
        ``active/<stem>/`` and flips the main markdown's ``status: completed``
        frontmatter back to ``status: active``. Auto-discovery then recreates
        the PWA project (with a fresh ``project_id``) and publishes
        ``project_created``. The originally-archived chats are not reattached:
        they stayed archived under their old project_id when the project was
        completed.

        Returns ``{ok, workspace, stem, project}`` where ``project`` is the
        recreated project dict (or ``None`` if discovery somehow missed it).
        """
        if not self._is_known_workspace(workspace):
            raise ValueError("Invalid workspace.")
        if not chat_service._VAULT_FOLDER_RE.fullmatch(stem):
            raise ValueError(f"Invalid project folder {stem!r}.")

        completed_root = self._vault_completed_root(workspace).resolve()
        active_root = self._vault_active_root(workspace).resolve()
        src = (completed_root / stem).resolve()
        dst = (active_root / stem).resolve()

        if not (src.exists() and src.is_dir()):
            raise ValueError(f"Completed project {stem!r} not found.")
        # Refuse to act if either resolved path escapes its root (symlinks).
        if not src.is_relative_to(completed_root) or not dst.is_relative_to(active_root):
            raise ValueError(
                f"Project folder {stem!r} resolves outside the projects tree."
            )
        if dst.exists():
            raise ValueError(
                f"An active project folder named {stem!r} already exists."
            )

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))

        # Flip status frontmatter back to active in the main markdown.
        for candidate in (dst / f"{stem}.md", dst / "README.md"):
            if candidate.exists():
                text = candidate.read_text()
                text = re.sub(r"(?m)^(status:\s*)completed\s*$", r"\1active", text)
                candidate.write_text(text)
                break

        # Force auto-discovery so the PWA project is recreated and a
        # project_created event reaches subscribed WS clients.
        self.list_projects(workspace)
        project = next(
            (p for p in self._projects.values()
             if p.vault_folder == stem and p.workspace == workspace),
            None,
        )
        return {
            "ok": True,
            "workspace": workspace,
            "stem": stem,
            "project": project.to_dict() if project else None,
        }

    def delete_project(self, project_id: str) -> bool:
        project = self._projects.get(project_id)
        if project is None:
            return False
        if project.is_auto or project.kind == "memory":
            raise ValueError(
                f"The {project.name} project is auto-managed and cannot be deleted."
            )
        if project.vault_folder:
            # Block deletion of vault-backed projects: auto-discovery would be
            # immediately re-create them on the next list_projects() call,
            # leaving the user with a project that won't stay deleted.
            # Use complete_project to move the vault entry to completed/, or
            # remove the vault entry directly and refresh.
            ws_root = self._display_path(self._vault_active_root(project.workspace))
            raise ValueError(
                f"Project '{project.name}' is backed by vault entry "
                f"'{project.vault_folder}'. Use Complete to move it to "
                f"completed/, or remove {ws_root}/"
                f"{project.vault_folder} (folder or .md) and refresh."
            )
        return self._remove_project(project_id)

    def _remove_project(self, project_id: str) -> bool:
        """Internal removal: pops the project, archives its chats, persists,
        and publishes ``project_deleted``. Skips the vault-backed guard so
        ``complete_project`` can call this after moving the vault entry."""
        if project_id not in self._projects:
            return False
        # Archive and remove all chats in this project. The project itself is
        # only dropped once every chat is gone: if one chat fails to archive,
        # the chats already archived are saved as removed and the project
        # stays, with the rest of its chats, so the removal can be retried.
        try:
            for cid in list(self._chats):
                if self._chats[cid].project_id == project_id:
                    self._archive_and_remove_chat(cid)
        except Exception:
            self._save()
            raise
        self._projects.pop(project_id, None)
        self._save()
        self._events.publish({
            "type": "project_deleted",
            "project_id": project_id,
        })
        return True

    # ── Chat CRUD ────────────────────────────────────────────────────────

    def list_chats(self, project_id: str | None = None) -> list[ChatInfo]:
        chats = list(self._chats.values())
        if project_id:
            chats = [c for c in chats if c.project_id == project_id]
        chats.sort(key=lambda c: c.created_at)
        return chats

    def is_session_local(self, chat: ChatInfo) -> bool:
        """Check if the session file for a chat exists on this machine."""
        if not chat.session_id:
            return True  # new chat, no session yet, treat as local

        # Only Claude has a local session-file contract we can probe
        # (a ``<session>.jsonl`` under ``.claude/projects``). Every other
        # non-Claude provider owns its sessions and resumes
        # them by id through its own server, so treat those as local.
        if chat.provider in ("", "claude"):
            return self._claude_session_exists(
                chat.session_id, agent_root=self._agent_root_for_chat(chat.chat_id)
            )
        return True

    def list_chats_dicts(self, project_id: str | None = None) -> list[dict]:
        """Return chat dicts with a ``local`` flag indicating session availability."""
        return [
            c.to_dict(local=self.is_session_local(c))
            for c in self.list_chats(project_id)
        ]

    def get_chat(self, chat_id: str) -> ChatInfo | None:
        return self._chats.get(chat_id)

    def create_chat(
        self,
        project_id: str,
        title: str = "New Chat",
        model: str | None = None,
        mode: str | None = None,
        provider: str | None = None,
        helper: dict | None = None,
    ) -> ChatInfo:
        if project_id not in self._projects:
            raise ValueError(f"Project '{project_id}' not found")
        if provider is not None and provider not in supported_providers():
            raise ValueError(f"Unknown provider '{provider}'")
        # Resolve the effective model/provider before any side effects, so a
        # rejected model can't leave unrelated empty chats deleted (#259).
        project = self._projects.get(project_id)
        workspace = project.workspace if project else None
        chat_provider = provider
        if not chat_provider:
            chat_provider = self._config.default_provider_for_workspace(workspace)
        # The provider's operator default (Settings → Models tab); the
        # workspace no longer pins a model. An explicit ``model`` arg wins.
        # Passing chat_provider resolves against that provider's own operator
        # default when it differs from the workspace's default provider.
        default_model = self._config.default_model_for_workspace(
            workspace, chat_provider
        )
        chat_model = model or default_model
        chat_model = self._resolve_and_validate_chat_model(
            chat_model, chat_provider, project_id
        )
        # The empty-chat sweep was removed from create_chat: it deleted the
        # brand-new chat the user had just opened (racing the POST's own
        # response), which closed the panel and caused the "new chat flashes
        # and then opens" bug. Empty chats now live until the user deletes
        # them explicitly.
        cid = f"chat-{chat_service._uuid8()}"
        # Per-provider default thinking level for new chats; a missing entry
        # leaves it to the provider default ("" = auto).
        default_thinking = (self._config.provider_default_thinking or {}).get(
            chat_provider, ""
        )
        chat = ChatInfo(
            chat_id=cid,
            project_id=project_id,
            title=title,
            model=chat_model,
            provider=chat_provider,
            mode=cast(BridgeMode, mode or self._config.default_mode_for_provider(chat_provider)),
            thinking_level=default_thinking,
            created_at=chat_service._now_iso(),
            helper=chat_service._normalize_chat_helper(helper),
        )
        self._chats[cid] = chat
        self._save()
        self._events.publish({"type": "chat_created", "chat": chat.to_dict(local=True)})
        return chat

    def is_empty_chat(self, chat_id: str) -> bool:
        """Public form of `_is_empty_chat`, for the conditional-delete route.

        The PWA needs this verdict to discard an abandoned draft on close, and
        cannot compute it: `user_turn_count` is not in any payload it receives.
        """
        chat = self._chats.get(chat_id)
        return chat is not None and self._is_empty_chat(chat)

    def _is_empty_chat(self, chat: ChatInfo) -> bool:
        """An empty chat is one the user abandoned before sending anything.

        Criteria: default title, no user turns recorded, no SDK session
        attached, not archived, and not a retired imported CLI record. Active
        broker stream is also a bail-out signal: it means a turn is in flight,
        so user_turn_count may just not have been bumped yet. Unsent composer
        text counts as content too — the user typed it, and deleting the chat
        strands it in a localStorage key nothing can reach again.
        """
        if chat.archived:
            return False
        if chat.project_id == _CC_CLI_PROJECT_ID:
            return False
        if chat.chat_id.startswith(_CC_CHAT_PREFIX):
            return False
        if chat.title != "New Chat":
            return False
        if chat.session_id:
            return False
        if chat.user_turn_count > 0:
            return False
        if self._broker.get(chat.chat_id) is not None:
            return False
        return True

    def _cleanup_empty_chats(self, except_chat_id: str | None = None) -> list[str]:
        """Delete any empty chats. Returns the list of deleted chat_ids.

        Emits a ``chat_deleted`` event per removed chat so open tabs can
        drop the entry from the sidebar without refetching.
        """
        empty_ids = [
            cid
            for cid, chat in self._chats.items()
            if cid != except_chat_id and self._is_empty_chat(chat)
        ]
        for cid in empty_ids:
            chat = self._chats.pop(cid, None)
            if chat is None:
                continue
            # No session, no images, no transcript -> nothing else to clean
            # up. Still cancel any in-flight provider just in case.
            self._cancel_between_turns_drain(cid)
            provider = self._pop_provider(cid)
            if provider:
                asyncio.ensure_future(provider.disconnect())
            logger.info("Cleaned up empty chat %s", cid)
            self._events.publish({
                "type": "chat_deleted",
                "chat_id": cid,
                "project_id": chat.project_id,
                "reason": "empty",
            })
        if empty_ids:
            self._save()
        return empty_ids
    def update_chat(
        self,
        chat_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        project_id: str | None = None,
        thinking_level: str | None = None,
    ) -> ChatInfo | None:
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        if mode is not None and mode not in {"normal", "plan", "auto", "bypass"}:
            # Reject unknown modes so a buggy client can't put a chat into a
            # state the next turn can't dispatch (the SDK mode mapper falls
            # back to bypassPermissions for anything outside the table, which
            # would silently downgrade security).
            raise ValueError(
                f"Unknown mode '{mode}' (allowed: normal, plan, auto, bypass)"
            )
        if provider is not None and provider not in supported_providers():
            raise ValueError(f"Unknown provider '{provider}'")
        target_provider = provider or chat.provider
        if thinking_level is not None:
            # Validate against the provider the chat will end up on, so a
            # combined provider+thinking PATCH checks the right level set.
            target_provider = provider if provider is not None else chat.provider
            allowed = THINKING_LEVELS.get(target_provider, ())
            if thinking_level and thinking_level not in allowed:
                raise ValueError(
                    f"Unknown thinking level '{thinking_level}' for provider "
                    f"'{target_provider}' (allowed: {', '.join(allowed)})"
                )

        target_project_id = project_id if project_id is not None else chat.project_id
        if project_id is not None and project_id != chat.project_id:
            if chat.archived:
                raise ValueError("Cannot move an archived chat")
            target = self._projects.get(project_id)
            if target is None:
                raise ValueError(f"Project '{project_id}' not found")
            current = self._projects.get(chat.project_id)
            if current is not None and target.workspace != current.workspace:
                raise ValueError(
                    "Cannot move chat across workspaces "
                    f"({current.workspace} → {target.workspace})"
                )

        changes_model = model is not None or provider is not None
        new_model = model if model is not None else chat.model
        if changes_model:
            new_model = self._resolve_and_validate_chat_model(
                new_model, target_provider, target_project_id
            )

        moved_from: str | None = None
        if project_id is not None and project_id != chat.project_id:
            moved_from = chat.project_id
            chat.project_id = project_id
        if title is not None:
            chat.title = title
        if changes_model:
            new_provider = provider if provider is not None else chat.provider
            # Cross-provider switches mid-chat would silently break: the
            # each provider runs its own CLI with its own auth and its own
            # session, so swapping providers mid-chat would continue the
            # conversation against a process that never saw it. Reject when the
            # chat already has history. A model swap within one provider is
            # fine.
            changed = (
                new_model != chat.model
                or new_provider != chat.provider
            )
            if changed and (
                chat.user_turn_count > 0 or chat.session_id
            ) and self._is_cross_provider_switch(chat.provider, new_provider):
                raise ValueError(
                    "Can't switch providers once a chat has started. Model "
                    "swaps within the same provider are fine; use handover to "
                    "continue this chat with another provider, or close this "
                    "chat and start a new one."
                )
            chat.model = new_model
            chat.provider = new_provider
        if mode is not None:
            chat.mode = mode  # type: ignore[assignment]
        if thinking_level is not None:
            chat.thinking_level = thinking_level
        self._save()
        if moved_from is not None:
            self._events.publish({
                "type": "chat_moved",
                "chat_id": chat_id,
                "project_id": chat.project_id,
                "old_project_id": moved_from,
            })
        return chat

    def _parse_transcript_messages(self, text: str) -> list[dict]:
        """Extract user and assistant messages from transcript markdown."""
        turns_data = []
        parts = re.split(r'^## Turn \d+', text, flags=re.MULTILINE)

        for part in parts[1:]:
            user_match = re.search(r'### User\s*\n\s*```text\n(.*?)\n```', part, re.DOTALL)
            assistant_match = re.search(r'### Assistant\s*\n\s*```text\n(.*?)\n```', part, re.DOTALL)

            time_match = re.search(r'-\s*Time:\s*([^\n]+)', part)
            timestamp = time_match.group(1).strip() if time_match else ""

            usage = self._parse_transcript_usage(part)

            if user_match:
                user_content = user_match.group(1)
                user_content = re.sub(r'(?s)^\[CIAO_CONTEXT_BEGIN\].*?\[CIAO_CONTEXT_END\]\s*', '', user_content)
                if user_content.strip():
                    turns_data.append({
                        "role": "user",
                        "content": user_content,
                        "timestamp": timestamp,
                    })

            if assistant_match:
                assistant_content = assistant_match.group(1)
                if assistant_content.strip():
                    row = {
                        "role": "assistant",
                        "content": assistant_content,
                        "timestamp": timestamp,
                    }
                    if usage:
                        row["usage"] = usage
                    turns_data.append(row)

        return turns_data

    @staticmethod
    def _parse_transcript_usage(part: str) -> dict[str, str]:
        """Parse the archived turn's ``### Usage`` section into a dict.

        The archive renders each persisted turn's usage dict as ``- key:
        value`` lines, so an archived chat can serve the same token counts
        (and the context %) the live transcript carried. Everything else in
        the section is preserved as-is; a missing or empty section yields {}.
        """
        section = re.search(
            r'### Usage\s*\n(.*?)(?=^### |\Z)', part, re.DOTALL | re.MULTILINE
        )
        if not section:
            return {}
        usage: dict[str, str] = {}
        for line in section.group(1).splitlines():
            item = re.match(r'^-\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+)$', line.strip())
            if item:
                usage[item.group(1)] = item.group(2).strip()
        return usage

    def continue_archived_chat(self, chat_id: str) -> ChatInfo:
        """Create a new active chat continuing from an archived one.
        
        Reads the archived transcript from the vault, parses the message
        history, and seeds the new chat's handover context.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            raise ValueError("Archived chat not found")
        if not chat.archived:
            raise ValueError("Chat is not archived")
        if not chat.archive_path:
            raise ValueError("Transcript file path is not set")
            
        full_path = self._config.workspace_root / chat.archive_path
        if not full_path.exists():
            raise ValueError(f"Transcript file not found at {chat.archive_path}")
            
        try:
            text = full_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Failed to read transcript file: {exc}")
            
        parsed_messages = self._parse_transcript_messages(text)
        if not parsed_messages:
            raise ValueError("No message history found in transcript")
            
        # Create a new active chat in the same project, title, model, and provider
        new_chat = self.create_chat(
            project_id=chat.project_id,
            title=chat.title,
            model=chat.model,
            mode=chat.mode,
            provider=chat.provider,
        )
        new_chat.thinking_level = chat.thinking_level
        
        # Seed handover messages
        new_chat.handover_messages = chat_service._normalize_handover_messages(
            parsed_messages,
            max_messages=chat_service._PROVIDER_HANDOVER_MAX_MESSAGES,
            max_chars=chat_service._PROVIDER_HANDOVER_MAX_CHARS,
        )
        new_chat.handover_context_pending = True
        
        self._save()
        return new_chat

    def fork_chat(
        self,
        chat_id: str,
        *,
        messages: list[dict],
        turn_index: int,
    ) -> ChatInfo:
        """Create a fresh chat from visible history through one final answer."""
        source = self._chats.get(chat_id)
        if source is None:
            raise KeyError("Source chat not found")
        if source.project_id not in self._projects:
            raise ValueError("Source project not found")
        if not isinstance(turn_index, int) or isinstance(turn_index, bool) or turn_index < 0:
            raise ValueError("Fork turn must be a non-negative integer")

        clean_rows = chat_service._clean_handover_messages(messages)
        if not clean_rows:
            raise ValueError("Fork history must be non-empty")
        if clean_rows[-1].get("role") != "assistant" or clean_rows[-1].get("is_error"):
            raise ValueError("Fork history must end with a final assistant answer")
        user_positions = [
            index for index, row in enumerate(clean_rows) if row.get("role") == "user"
        ]
        expected_turn = len(user_positions) - 1
        if expected_turn < 0 or turn_index != expected_turn:
            raise ValueError("Fork turn does not match the selected answer")

        selected_rows = clean_rows[user_positions[-1]:]
        selected_chars = sum(
            len(str(row.get("content", ""))) for row in selected_rows
        )
        if (
            len(selected_rows) > chat_service._FORK_MAX_MESSAGES
            or selected_chars > chat_service._FORK_MAX_CHARS
        ):
            raise ValueError("The selected turn is too large to fork")

        rows = list(clean_rows)
        total_chars = sum(len(str(row.get("content", ""))) for row in rows)
        truncated = False
        while (
            len(rows) > chat_service._FORK_MAX_MESSAGES
            or total_chars > chat_service._FORK_MAX_CHARS
        ):
            removed = rows.pop(0)
            total_chars -= len(str(removed.get("content", "")))
            truncated = True
        if truncated:
            rows.insert(0, {
                "role": "system",
                "content": (
                    "Earlier conversation history was omitted when this fork "
                    "was created."
                ),
            })

        if source.fork_root_chat_id:
            root_chat_id = source.fork_root_chat_id
            base_title = source.fork_base_title or source.title
        else:
            root_chat_id = source.chat_id
            base_title = source.title
        next_index = 1 + max(
            (
                chat.fork_index
                for chat in self._chats.values()
                if chat.fork_root_chat_id == root_chat_id
            ),
            default=0,
        )

        fork = ChatInfo(
            chat_id=f"chat-{chat_service._uuid8()}",
            project_id=source.project_id,
            title=f"{base_title} · Fork {next_index}",
            model=source.model,
            mode=source.mode,
            provider=source.provider,
            thinking_level=source.thinking_level,
            created_at=chat_service._now_iso(),
            handover_messages=rows,
            handover_context_pending=True,
            forked_from_chat_id=source.chat_id,
            forked_from_turn_index=turn_index,
            fork_root_chat_id=root_chat_id,
            fork_index=next_index,
            fork_base_title=base_title,
        )
        copied_turn_index = 0
        for row in rows:
            if row.get("role") != "user":
                continue
            copied_refs: list[str] = []
            for ref in row.get("images", []):
                attachment = self.resolve_image_ref(str(ref))
                if attachment is None:
                    continue
                duplicate = self.save_image_upload(
                    attachment.path.read_bytes(), attachment.original_filename
                )
                copied_refs.append(duplicate.path.name)
            if copied_refs:
                row["images"] = copied_refs
                fork.user_turn_images[str(copied_turn_index)] = copied_refs
            else:
                row.pop("images", None)
            copied_turn_index += 1
        fork.user_turn_count = copied_turn_index

        self._chats[fork.chat_id] = fork
        try:
            self._save()
        except Exception:
            self._chats.pop(fork.chat_id, None)
            self._unlink_chat_images(fork)
            raise
        # Announce the new chat so other tabs/devices (and this tab if a
        # racing syncLatest clobbered the optimistic push) render it without
        # waiting for the 15s poll. A fork starts no streaming turn, so no
        # chat_result_ready refetch would otherwise restore it (#fork-list-sync).
        self._events.publish({"type": "chat_created", "chat": fork.to_dict(local=True)})
        return fork

    def handover_chat(
        self,
        chat_id: str,
        *,
        provider: str,
        model: str,
        messages: list[dict] | None = None,
    ) -> ChatInfo | None:
        """Switch a started chat to a new provider via explicit handover.

        This intentionally bypasses `update_chat`'s cross-provider guard by
        resetting the active provider session and preserving visible messages
        as a handover context pack for the next turn.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        if chat.archived:
            raise ValueError("Cannot hand over an archived chat")
        if provider not in supported_providers():
            raise ValueError(f"Unknown provider '{provider}'")
        clean_model = (model or "").strip()
        if not clean_model:
            raise ValueError("Model is required")
        if self._broker.get(chat_id) is not None:
            raise ValueError("Cannot hand over while a turn is running")

        resolved_model = self._resolve_and_validate_chat_model(
            clean_model, provider, chat.project_id
        )

        self._revoke_mcp_chat(chat_id)

        old_provider = chat.provider
        old_model = chat.model
        rows = chat_service._normalize_handover_messages(
            messages,
            max_messages=chat_service._PROVIDER_HANDOVER_MAX_MESSAGES,
            max_chars=chat_service._PROVIDER_HANDOVER_MAX_CHARS,
        )
        rows.append(
            chat_service._handover_marker(
                old_provider=old_provider,
                old_model=old_model,
                new_provider=provider,
                new_model=resolved_model,
            )
        )
        chat.handover_messages = rows
        chat.handover_context_pending = True
        chat.provider = provider
        chat.model = resolved_model
        # Thinking levels are provider-native and don't carry across a handover.
        chat.thinking_level = ""
        chat.session_id = ""
        chat.context_digest = ""
        chat.context_session_id = ""
        # Provider switch: the new provider has its own session numbering, so
        # the old lineage doesn't apply. Visible history instead carries over
        # via `handover_messages` above.
        chat.previous_session_ids = []
        chat.last_activity_at = chat_service._now_iso()

        ctx = ChatContext.for_web(chat_id)
        self._state.reset_active_session(ctx)
        self._cancel_between_turns_drain(chat_id)
        provider_service = self._pop_provider(chat_id)
        if provider_service:
            asyncio.ensure_future(provider_service.disconnect())
        self._save()
        return chat

    def mark_handover_context_used(self, chat_id: str) -> None:
        chat = self._chats.get(chat_id)
        if chat is None or not chat.handover_context_pending:
            return
        chat.handover_context_pending = False
        self._save()

    async def _reclaim_provider_sessions_async(
        self,
        chat: ChatInfo,
        session_ids: list[str] | None = None,
        *,
        agent_root: Path | None = None,
    ) -> None:
        """Drop provider-side session blobs/threads for abandoned chats.

        Claude deletes the SDK JSONL blob. OpenCode deletes its persisted
        session through ``DELETE /api/session/{id}``.
        Provider cleanup is fail-open: the Ciaobot archive remains durable even
        when an external provider is unavailable.

        Both providers store a session under the agent root the chat ran in —
        the Claude SDK by hashing that path into its projects directory, and
        opencode because its server is started there. Reclaiming against
        ``workspace_root`` therefore found nothing for a workspace-scoped chat
        and leaked every blob and session it was meant to drop.

        ``agent_root`` is that root, and a caller that has already dropped the
        chat from the registry MUST pass it: ``_agent_root_for_chat`` resolves
        through ``self._chats``, so on the delete path (which pops the row
        before scheduling this cleanup) it would silently fall back to the
        PRIMARY workspace's root and reclaim nothing — the same leak, now
        reported as a success.
        """
        raw_ids = (
            session_ids
            if session_ids is not None
            else [*chat.previous_session_ids, chat.session_id]
        )
        root = (
            agent_root
            if agent_root is not None
            else self._agent_root_for_chat(chat.chat_id)
        )
        seen: set[str] = set()
        for sid in raw_ids:
            sid = str(sid or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                if chat.provider == "claude":
                    deleted = self._transcripts.delete_sdk_session_blob(root, sid)
                elif chat.provider == "opencode":
                    deleted = await OpencodeProvider.delete_thread(root, sid)
                else:
                    continue
            except Exception:  # noqa: BLE001 — provider cleanup is fail-open
                logger.exception(
                    "Failed to reclaim %s session %s for chat %s",
                    chat.provider,
                    sid,
                    chat.chat_id,
                )
                continue
            if chat.provider == "opencode" and not deleted:
                logger.warning(
                    "Provider returned no cleanup confirmation for %s session %s "
                    "(chat %s)",
                    chat.provider,
                    sid,
                    chat.chat_id,
                )

    async def _disconnect_provider(
        self, chat_id: str, provider: ProviderService | None
    ) -> bool:
        """Close a chat's provider before deleting its provider-side session.

        Returns whether the provider is actually gone. A failure is still
        swallowed — cleanup must not block the lifecycle write that scheduled
        it — but it is no longer invisible: the idle sweep drops its only
        reference to the provider the moment it pops it from
        ``self._providers``, and an opencode server that refused to terminate
        would then stay alive with nothing left to retry the teardown, exactly
        the process leak the sweep exists to prevent. Callers on the
        lifecycle paths are free to ignore the answer; the sweep is not (see
        ``reap_idle_providers``).
        """

        if provider is None:
            return True
        try:
            await provider.disconnect()
        except Exception:  # noqa: BLE001 — cleanup must not block lifecycle writes
            logger.exception("Failed to disconnect provider for chat %s", chat_id)
            return False
        return True

    def _schedule_provider_cleanup(
        self,
        chat: ChatInfo,
        provider: ProviderService | None,
        session_ids: list[str] | None = None,
        *,
        agent_root: Path | None = None,
    ) -> None:
        """Disconnect then reclaim provider storage for sync lifecycle calls.

        ``agent_root`` is forwarded to the reclaim; see its docstring for why a
        caller that has already unregistered the chat has to resolve it first.
        """

        if provider is None and not any(
            str(session_id or "")
            for session_id in (
                session_ids
                if session_ids is not None
                else [*chat.previous_session_ids, chat.session_id]
            )
        ):
            return

        async def cleanup() -> None:
            await self._disconnect_provider(chat.chat_id, provider)
            await self._reclaim_provider_sessions_async(
                chat, session_ids, agent_root=agent_root
            )

        asyncio.ensure_future(cleanup())

    def delete_chat(self, chat_id: str) -> bool:
        # Resolved before the row leaves the registry: the cleanup below runs
        # after the pop (and asynchronously), and `_agent_root_for_chat` reads
        # `self._chats`, so resolving it there lands on the primary workspace's
        # root and leaves this chat's blob or opencode session behind.
        agent_root = self._agent_root_for_chat(chat_id)
        chat = self._chats.pop(chat_id, None)
        if chat is None:
            return False
        self._revoke_mcp_chat(chat_id)
        ctx = ChatContext.for_web(chat_id)
        task = self._retry_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._cancel_between_turns_drain(chat_id)
        self._streaming.discard_drain_result(chat_id)
        provider = self._pop_provider(chat_id)
        self._schedule_provider_cleanup(chat, provider, agent_root=agent_root)
        # Explicit deletion is a tombstone, not merely a sidebar mutation.
        # Remove every recovery signal so startup repair cannot revive it.
        self._state.delete_context(ctx)
        self._transcripts.delete_current(ctx, chat.provider)
        self._unlink_chat_images(chat)
        # Drop file snapshots so we don't accumulate dead history forever.
        # Archive intentionally keeps them: archived chats are read-only but
        # their history viewer should still work.
        self._snapshots.delete_chat(chat_id)
        # Deleting an archived chat must cancel/tombstone its pending archive
        # job: a running task would otherwise finish and write derived memory
        # for a chat that no longer exists, and a startup resume could revive
        # it. The tombstone is durable even if the in-process task is mid-write.
        # The row is already out of `self._chats`, so hand it over explicitly.
        self._cancel_archive_job(chat_id, chat)
        self._delete_archived_transcript(chat_id)
        self._save(reason="user_chat_delete")
        self._events.publish({
            "type": "chat_deleted",
            "chat_id": chat_id,
            "project_id": chat.project_id,
            "reason": "user",
        })
        return True

    async def _maybe_archive_proposal_helper(self, chat_id: str) -> bool:
        """Archive a clean resolution helper once all owned proposals are gone."""
        chat = self._chats.get(chat_id)
        if chat is None or chat.archived:
            return False
        helper = chat_service._normalize_chat_helper(chat.helper)
        if helper.get("archive_policy") != "when_resolved":
            return False
        if (
            self._broker.get(chat_id) is not None
            or chat.last_response_status != "success"
            or not chat.last_response.strip()
            or chat.pending_question
            or chat.pending_permission
            or chat.retry_status
            or self._subagents.running_count(chat_id) > 0
        ):
            return False
        try:
            from ciao.proposal_tracking import pending_proposal_ids

            pending = pending_proposal_ids(self._config)
        except Exception:  # noqa: BLE001 — uncertainty must keep the chat visible
            logger.exception("Could not verify proposal helper %s", chat_id)
            return False
        pending_bases = {pid.split(":", 1)[0] for pid in pending}
        if any(
            pid in pending or pid.split(":", 1)[0] in pending_bases
            for pid in helper["proposal_ids"]
        ):
            return False
        project = self._projects.get(chat.project_id)
        outcome = await self.archive_chat(chat_id)
        if outcome is not None:
            self.run_archive_postprocess(chat_id, outcome, chat, project)
        return bool(chat.archived)

    # ── Memory pass ──────────────────────────────────────────────────────
    #
    # Thin seams over ``self._memory_pass`` so the archive pipeline and main.py
    # talk to the manager, not to the collaborator directly.

    def enqueue_memory_pass(
        self,
        source: ChatInfo,
        project: ProjectInfo | None,
        archive_path: Path,
        doc_path: str,
    ) -> str | None:
        return self._memory_pass.enqueue(source, project, archive_path, doc_path)

    async def resume_memory_passes(self) -> None:
        self._memory_pass.resume()

    async def _memory_pass_turn_finished(self, chat_id: str) -> bool:
        try:
            finished: bool = await self._memory_pass.on_turn_finished(chat_id)
        except Exception:  # noqa: BLE001 — a pass must not break the turn
            logger.exception("Memory pass turn-end handling failed for %s", chat_id)
            return False
        return finished

    # ── Session management ───────────────────────────────────────────────

    def _read_archive_inputs(
        self, chat_id: str, ctx: ChatContext, chat: ChatInfo, agent_root: Path
    ) -> tuple[int, str | None, Path | None]:
        """Disk half of archiving one chat, safe to run off the event loop.

        Everything here is file I/O keyed by this chat's own context and session
        id — read the turn count and the filtered JSONL, then render and write
        the markdown archive. It touches no shared in-memory state and no
        asyncio primitives, which is what lets ``archive_chat`` hand it to a
        worker thread. ``agent_root`` is resolved by the caller on the loop for
        that reason.

        Ordering matters: the turn count has to be taken before
        ``archive_session`` consumes the in-progress transcript, and the
        filtered JSONL before the caller deletes the session blob.

        The Claude SDK writes a chat's session blob under the agent root the
        chat actually ran in, so that root — not ``workspace_root`` — is what
        finds it. Resolving it against the install root instead returned None
        for every workspace-scoped chat, and a None here is indistinguishable
        from "nothing to extract": ``run_archive_postprocess`` skipped insights,
        the project-doc fold, the trajectory and memory proposals in silence,
        with no job run and no log line.
        """
        turn_count = self._transcripts.peek_turn_count(ctx, chat.provider)
        filtered_jsonl: str | None = None
        if chat.session_id and chat.provider == "claude":
            from ciao.insights import filter_session_jsonl
            try:
                filtered_jsonl = filter_session_jsonl(
                    self._config.workspace_root,
                    chat.session_id,
                    agent_root=agent_root,
                )
            except Exception:  # noqa: BLE001 — never fail archive over insights prep
                logger.exception(
                    "Failed to pre-filter JSONL for chat %s", chat_id
                )
                filtered_jsonl = None
        elif chat.provider == "opencode":
            filtered_jsonl = self._transcripts.current_filtered_jsonl(
                ctx, chat.provider
            ) or None
        result = self._transcripts.archive_session(
            ctx=ctx,
            active_model=chat.model,
            last_effective_model=chat.model,
            session_id=chat.session_id,
            provider=chat.provider,
        )
        return turn_count, filtered_jsonl, result

    async def archive_chat(self, chat_id: str) -> ArchiveOutcome | None:
        """Serialize concurrent archive requests for one chat."""
        lock = self._archive_locks.setdefault(chat_id, asyncio.Lock())
        # Refcounted rather than `if not lock.locked()`: `Lock.release()`
        # clears `_locked` before the woken waiter actually resumes, so the
        # releasing caller saw the lock as free while a waiter was still queued
        # on it and dropped the entry. A third `archive_chat` then setdefault'd
        # a *fresh* lock and ran concurrently with that waiter — losing exactly
        # the serialization this lock provides. Counting holders is unaffected
        # by when the waiter wakes.
        self._archive_lock_users[chat_id] = (
            self._archive_lock_users.get(chat_id, 0) + 1
        )
        try:
            async with lock:
                chat = self._chats.get(chat_id)
                if chat is None or chat.archived:
                    return None
                return await self._archive_chat_unlocked(chat_id)
        finally:
            remaining = self._archive_lock_users.get(chat_id, 1) - 1
            if remaining <= 0:
                self._archive_lock_users.pop(chat_id, None)
                self._archive_locks.pop(chat_id, None)
            else:
                self._archive_lock_users[chat_id] = remaining

    async def _archive_chat_unlocked(self, chat_id: str) -> ArchiveOutcome | None:
        """Archive a chat's transcript and mark it as archived.

        Also disconnects any live provider and reclaims provider-side session
        storage (Claude SDK JSONL blob or an opencode session). The markdown
        transcript in the vault is the durable record.

        Returns the archive path plus a pre-filtered JSONL string captured
        before blob deletion, so the caller can dispatch post-archive insights
        extraction without racing against the disk reclaim. None means the chat
        does not exist, or had nothing to write.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        self._revoke_mcp_chat(chat_id)
        ctx = ChatContext.for_web(chat_id)
        # Reading a large session JSONL and rendering the markdown transcript
        # is the expensive part. On the loop that froze every other request and
        # every streaming turn until it finished, so it runs in a worker
        # thread. Awaited before anything else happens, so the chat_archived
        # event still fires in the same place it always did.
        agent_root = self._agent_root_for_chat(chat_id)
        turn_count, filtered_jsonl, result = await asyncio.to_thread(
            self._read_archive_inputs, chat_id, ctx, chat, agent_root
        )
        # The await above is a suspension point, so the chat may have been
        # deleted while the transcript was being written. Marking a row that is
        # no longer in the registry archived (and publishing chat_archived for
        # it) would resurrect a deleted chat in the PWA sidebar.
        if self._chats.get(chat_id) is not chat:
            return None
        if chat.retry_status:
            self._clear_chat_retry(chat)
        self._cancel_pending_push(chat_id)
        self._cancel_between_turns_drain(chat_id)
        provider = self._pop_provider(chat_id)
        await self._disconnect_provider(chat_id, provider)
        await self._reclaim_provider_sessions_async(chat)
        self._unlink_chat_images(chat)
        chat.archived = True
        if result is not None:
            try:
                chat.archive_path = str(result.relative_to(self._config.workspace_root))
            except ValueError:
                chat.archive_path = str(result)
        self._save()
        self._events.publish({
            "type": "chat_archived",
            "chat_id": chat_id,
            "project_id": chat.project_id,
            "archive_path": chat.archive_path,
        })
        if result is None:
            return None
        return ArchiveOutcome(
            path=result,
            session_id=chat.session_id,
            turn_count=turn_count,
            filtered_jsonl=filtered_jsonl,
        )

    # ── Archive pipeline seams ────────────────────────────────────────────

    def _archive_pipeline_for(self) -> ArchivePipeline:
        """Return the archive collaborator, including for ``__new__`` fixtures."""
        try:
            return self._archive_pipeline
        except AttributeError:
            pipeline = ArchivePipeline(self)
            self._archive_pipeline = pipeline
            return pipeline

    @property
    def _postprocessing(self) -> set[str]:
        return self._archive_pipeline_for().postprocessing

    @_postprocessing.setter
    def _postprocessing(self, value: set[str]) -> None:
        state = self._archive_pipeline_for().postprocessing
        state.clear()
        state.update(value)

    @property
    def _archive_jobs(self) -> dict[str, ArchiveJob]:
        return self._archive_pipeline_for().jobs

    @_archive_jobs.setter
    def _archive_jobs(self, value: dict[str, ArchiveJob]) -> None:
        jobs = self._archive_pipeline_for().jobs
        jobs.clear()
        jobs.update(value)

    @property
    def _archive_tasks(self) -> dict[str, asyncio.Task[object]]:
        return self._archive_pipeline_for().tasks

    @_archive_tasks.setter
    def _archive_tasks(self, value: dict[str, asyncio.Task[object]]) -> None:
        tasks = self._archive_pipeline_for().tasks
        tasks.clear()
        tasks.update(value)

    def attach_job_runs_publisher(self) -> None:
        """Route live archive job events into the manager."""
        self._archive_pipeline_for().attach_job_runs_publisher()

    def _on_job_event(self, event: dict[str, object]) -> None:
        return self._archive_pipeline_for()._on_job_event(event)

    def _apply_job_event(self, chat_id: str, event: dict[str, object]) -> None:
        return self._archive_pipeline_for()._apply_job_event(chat_id, event)

    def _publish_postprocess(self, chat: ChatInfo) -> None:
        return self._archive_pipeline_for()._publish_postprocess(chat)

    def postprocessing_chat_ids(self) -> list[str]:
        return self._archive_pipeline_for().postprocessing_chat_ids()

    def _begin_postprocess(self, chat_id: str, expected: list[str]) -> None:
        return self._archive_pipeline_for()._begin_postprocess(chat_id, expected)

    def _end_postprocess(self, chat_id: str) -> None:
        return self._archive_pipeline_for()._end_postprocess(chat_id)

    async def _tracked_postprocess(
        self, chat_id: str, coro: Coroutine[object, object, object]
    ) -> None:
        return await self._archive_pipeline_for()._tracked_postprocess(chat_id, coro)

    def retry_insights(self, chat_id: str) -> str:
        return self._archive_pipeline_for().retry_insights(chat_id)

    def retry_archive_steps(self, chat_id: str) -> dict[str, object]:
        return self._archive_pipeline_for().retry_archive_steps(chat_id)

    def archive_job_view(self, chat_id: str) -> dict[str, object] | None:
        return self._archive_pipeline_for().archive_job_view(chat_id)

    def _delete_archived_transcript(self, chat_id: str) -> None:
        return self._archive_pipeline_for()._delete_archived_transcript(chat_id)

    def _cancel_archive_job(
        self, chat_id: str, chat: ChatInfo | None = None
    ) -> None:
        return self._archive_pipeline_for()._cancel_archive_job(chat_id, chat)

    def _archive_path_for_chat(self, chat: ChatInfo) -> Path:
        return self._archive_pipeline_for()._archive_path_for_chat(chat)

    def _job_inputs(
        self,
        chat: ChatInfo,
        project: ProjectInfo | None,
        *,
        filtered_jsonl: str = "",
        session_id: str = "",
        text_mode: bool = False,
    ) -> dict[str, object]:
        return self._archive_pipeline_for()._job_inputs(
            chat,
            project,
            filtered_jsonl=filtered_jsonl,
            session_id=session_id,
            text_mode=text_mode,
        )

    def _insights_model_for(self, chat: ChatInfo, workspace: str) -> str:
        return self._archive_pipeline_for()._insights_model_for(chat, workspace)

    def _persist_job_inputs(
        self, job: ArchiveJob, inputs: dict[str, object]
    ) -> None:
        return self._archive_pipeline_for()._persist_job_inputs(job, inputs)

    def _restore_job_inputs(
        self, chat: ChatInfo, project: ProjectInfo | None, job: ArchiveJob
    ) -> dict[str, object]:
        return self._archive_pipeline_for()._restore_job_inputs(chat, project, job)

    def _new_job_for_chat(
        self, chat: ChatInfo, inputs: dict[str, object]
    ) -> ArchiveJob:
        return self._archive_pipeline_for()._new_job_for_chat(chat, inputs)

    def _resume_job(
        self, chat_id: str, archive_path: Path
    ) -> tuple[ArchiveJob, dict[str, object]] | tuple[None, None]:
        return self._archive_pipeline_for()._resume_job(chat_id, archive_path)

    def _launch_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: dict[str, object],
        *,
        stages: list[str] | None = None,
    ) -> None:
        return self._archive_pipeline_for()._launch_job(
            chat_id, job, inputs, stages=stages
        )

    async def _run_job(
        self,
        chat_id: str,
        job: ArchiveJob,
        inputs: dict[str, object],
        *,
        stages: list[str] | None = None,
    ) -> None:
        return await self._archive_pipeline_for()._run_job(
            chat_id, job, inputs, stages=stages
        )

    def _overlay_job_postprocess(self, chat_id: str, job: ArchiveJob) -> None:
        return self._archive_pipeline_for()._overlay_job_postprocess(chat_id, job)

    async def resume_interrupted_jobs(self, *, max_concurrency: int = 2) -> int:
        return await self._archive_pipeline_for().resume_interrupted_jobs(
            max_concurrency=max_concurrency
        )

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
    ) -> None:
        return self._archive_pipeline_for().run_archive_postprocess(
            chat_id, outcome, chat_meta, project_meta
        )

    def _run_archive_index_best_effort(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        return self._archive_pipeline_for()._run_archive_index_best_effort(
            chat_id, outcome, operation
        )

    def _make_archive_index_operation(
        self, outcome: ArchiveOutcome
    ) -> Callable[[], None]:
        return self._archive_pipeline_for()._make_archive_index_operation(outcome)

    async def _index_archive_file_off_loop(
        self, chat_id: str, outcome: ArchiveOutcome, operation: Callable[[], None]
    ) -> None:
        return await self._archive_pipeline_for()._index_archive_file_off_loop(
            chat_id, outcome, operation
        )

    def new_session(self, chat_id: str) -> ChatInfo | None:
        """Archive current transcript and start a fresh session."""
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        if chat.archived:
            # Never resurrect an archived chat in place: this clears its
            # archived flag and archive_path, leaving an empty active chat
            # that reappears in the sidebar/menu bar (an archive "comes back"
            # with no transcript). Continuing an archived chat is
            # continue_archived_chat()'s job — it spawns a fresh chat and
            # leaves the archived one untouched.
            raise ValueError("Cannot start a new session in an archived chat")
        self._revoke_mcp_chat(chat_id)
        # Archive existing transcript
        ctx = ChatContext.for_web(chat_id)
        self._transcripts.archive_session(
            ctx=ctx,
            active_model=chat.model,
            last_effective_model=chat.model,
            session_id=chat.session_id,
            provider=chat.provider,
        )
        # Reclaim provider sessions for the archived transcript, plus any
        # earlier ones this chat rotated through (autocompact/resume-fallback)
        # before this reset — they're all being abandoned together.
        session_ids = [*chat.previous_session_ids, chat.session_id]
        # Drop attached images: they belong to the archived transcript.
        self._unlink_chat_images(chat)
        # Reset session
        chat.session_id = ""
        chat.previous_session_ids = []
        chat.context_digest = ""
        chat.context_session_id = ""
        chat.archived = False
        chat.archive_path = ""
        chat.handover_messages = []
        chat.handover_context_pending = False
        # A fresh session abandons any question the old one paused on, along
        # with follow-ups parked for that answer turn — they must not leak
        # into the new conversation.
        chat.pending_question = ""
        chat.pending_permission = ""
        chat.pending_queue = []
        chat.last_response = ""
        chat.last_response_status = ""
        chat.helper = {}
        if chat.retry_status:
            self._clear_chat_retry(chat)
        self._state.reset_active_session(ctx)
        # Disconnect old provider so a fresh one is created
        self._cancel_between_turns_drain(chat_id)
        provider = self._pop_provider(chat_id)
        self._schedule_provider_cleanup(chat, provider, session_ids)
        self._save()
        return chat

    # ── Provider management ──────────────────────────────────────────────

    def _get_provider(self, chat_id: str) -> ProviderService:
        if chat_id not in self._providers:
            chat = self._chats.get(chat_id)
            provider_name = chat.provider if chat else ""
            agent_root = self._agent_root_for_chat(chat_id)
            self._providers[chat_id] = ProviderService(
                self._config,
                provider=provider_name,
                agent_root=agent_root,
                workspace=self._workspace_for_chat(chat_id),
            )
        # Stamped on every hand-out, not just on creation: a long conversation
        # reuses one ProviderService for its whole life, so creation time says
        # nothing about whether the chat is still in use.
        self._provider_last_used[chat_id] = time.monotonic()
        self._ensure_provider_reaper()
        return self._providers[chat_id]

    def _pop_provider(self, chat_id: str) -> ProviderService | None:
        """Detach a chat's provider and forget its idle stamp.

        Every lifecycle path that used to call ``self._providers.pop`` goes
        through here so the stamp map cannot outlive the provider it describes
        (a recycled chat id would otherwise inherit a stale last-used time).
        Disconnecting is still the caller's job — some do it inline, some hand
        it to ``_schedule_provider_cleanup``.
        """
        self._provider_last_used.pop(chat_id, None)
        self._provider_disconnect_failures.pop(chat_id, None)
        return self._providers.pop(chat_id, None)

    # ── Idle provider reaping ────────────────────────────────────────────

    def _ensure_provider_reaper(self) -> None:
        """Start the idle sweep once, if there is a loop to run it on.

        Called from ``_get_provider`` rather than ``__init__`` because managers
        are constructed in tests and CLI paths with no running event loop, and
        an install that never opens a chat needs no sweep at all.
        """
        if self._provider_reaper is not None and not self._provider_reaper.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._provider_reaper = loop.create_task(
            self._provider_reap_loop(), name="provider-idle-reaper"
        )

    async def _provider_reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self._provider_reap_interval)
            try:
                await self.reap_idle_providers()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a sweep failure must not kill the loop
                logger.exception("Idle provider sweep failed")

    def _provider_is_busy(self, chat_id: str, active: set[str]) -> bool:
        """Whether a chat has work that a provider teardown would interrupt.

        ``active_chat_ids`` covers live streams, running background subagents
        and pending subagent watchers. The rest are per-chat tasks that hold a
        provider without necessarily opening a broker stream.
        """
        if chat_id in active:
            return True
        if self._streaming.has_live_drain(chat_id):
            return True
        for tasks in (self._retry_tasks, self._background_wake_tasks):
            task = tasks.get(chat_id)
            if task is not None and not task.done():
                return True
        if self._background_wake_pending.get(chat_id):
            return True
        # A parked question or approval card is held by the provider session
        # (for opencode, by the server process itself), and the reply routes
        # back through it. The stream may already be closed, so this is not
        # covered by `active_chat_ids`: tearing the provider down here would
        # strand the card the user is looking at.
        #
        # `chat.pending_queue` is deliberately NOT part of this test. It is
        # persisted chat state, not provider state: `start_stream` re-seeds it
        # on the next turn (see `_park_pending_for_retry`), so a teardown
        # cannot lose it. Treating it as busy would pin the provider forever
        # for a chat that parked messages and was never opened again — the
        # exact leak this sweep exists to close. The cases where a parked
        # queue really does need its provider (a paused question, an armed
        # retry) are already covered above.
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
        return bool(
            getattr(chat, "pending_question", "")
            or getattr(chat, "pending_permission", "")
        )

    async def reap_idle_providers(self, *, force: bool = False) -> list[str]:
        """Disconnect providers for chats idle past the timeout.

        Returns the chat ids reclaimed — only those whose ``disconnect()``
        actually returned. A provider whose disconnect raised is NOT in that
        list: it is kept for a bounded number of later sweeps instead (see
        ``_PROVIDER_DISCONNECT_MAX_ATTEMPTS``). ``force`` ignores the timeout
        (but not the busy check) and exists for tests and for an explicit
        operator sweep; it is never used by the periodic loop.

        Only the provider is released. The chat row, its ``session_id`` and its
        transcript are untouched, so the next turn reconnects and resumes — see
        ``_PROVIDER_IDLE_TIMEOUT_SECONDS``.
        """
        if not self._providers:
            return []
        now = time.monotonic()
        active = set(self.active_chat_ids())
        reclaimed: list[str] = []
        for chat_id in list(self._providers):
            if self._provider_is_busy(chat_id, active):
                # Refresh the stamp so a chat that was busy for the whole
                # window is not reclaimed the instant its work finishes.
                self._provider_last_used[chat_id] = now
                # A provider that is serving work again is healthy; earlier
                # failed teardowns say nothing about the next one, and letting
                # them accumulate across weeks of use would spend the retry
                # budget before the teardown that matters.
                self._provider_disconnect_failures.pop(chat_id, None)
                continue
            last_used = self._provider_last_used.get(chat_id)
            if last_used is None:
                # A provider with no stamp predates the reaper or was attached
                # by another path; adopt it now rather than reclaiming a chat
                # that may have been used a second ago.
                self._provider_last_used[chat_id] = now
                continue
            if not force and now - last_used < self._provider_idle_timeout:
                continue
            failures = self._provider_disconnect_failures.get(chat_id, 0)
            provider = self._pop_provider(chat_id)
            self._cancel_between_turns_drain(chat_id)
            try:
                disconnected = await self._disconnect_provider(chat_id, provider)
            except asyncio.CancelledError:
                # Shutdown cancelled the sweep mid-disconnect. `disconnect()`
                # awaits on both providers (the SDK transport, and
                # `process.terminate()/wait()` for opencode), and
                # `CancelledError` is a BaseException, so
                # `_disconnect_provider`'s `except Exception` does not absorb
                # it. The provider is already out of the map by now, so the
                # shutdown hook's snapshot would miss it and leave exactly the
                # half-closed transport that hook exists to prevent. Put it
                # back — `stop_provider_reaper` is awaited before that
                # snapshot is taken, so the hook still finishes the job.
                self._providers[chat_id] = provider  # type: ignore[assignment]
                self._provider_last_used[chat_id] = now
                self._provider_disconnect_failures[chat_id] = failures
                raise
            if not disconnected:
                # `disconnect()` raised. The provider is out of the map, so
                # dropping it here would leave (for opencode) a serve process
                # holding a port, an SSE stream and a stderr reader with no
                # reference left for the shutdown hook to retry — and the
                # sweep would report it as reclaimed on top of that.
                failures += 1
                if failures < _PROVIDER_DISCONNECT_MAX_ATTEMPTS:
                    # Put it back, keeping the STALE last-used stamp so the
                    # next sweep retries immediately rather than waiting out
                    # another full idle timeout, and record the attempt so the
                    # retry is bounded.
                    self._providers[chat_id] = provider  # type: ignore[assignment]
                    self._provider_last_used[chat_id] = last_used
                    self._provider_disconnect_failures[chat_id] = failures
                    logger.warning(
                        "Idle provider for chat %s failed to disconnect "
                        "(attempt %d/%d); keeping it for another sweep",
                        chat_id,
                        failures,
                        _PROVIDER_DISCONNECT_MAX_ATTEMPTS,
                    )
                else:
                    # Out of attempts. Holding it forever would pin a provider
                    # that never closes and hand it to the chat's next turn, so
                    # the reference goes — loudly, and NOT as a reclaim: this
                    # is the one case where a process may have been left
                    # behind, and the log is what says so.
                    logger.error(
                        "Giving up on the idle provider for chat %s after %d "
                        "failed disconnects; a provider process may have been "
                        "left running",
                        chat_id,
                        failures,
                    )
                continue
            reclaimed.append(chat_id)
        if reclaimed:
            logger.info(
                "Reclaimed %d idle provider(s) after %.0fs: %s",
                len(reclaimed),
                self._provider_idle_timeout,
                ", ".join(reclaimed),
            )
        return reclaimed

    async def stop_provider_reaper(self) -> None:
        """Cancel the sweep task. Called from the server's shutdown hook."""
        task = self._provider_reaper
        self._provider_reaper = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — teardown is fail-safe
            # Swallowed, but not silently: a sweep that died mid-disconnect
            # left providers behind, and that must not look like a clean stop.
            logger.exception("Idle provider reaper did not stop cleanly")

    def _workspace_for_chat(self, chat_id: str) -> str:
        """The logical workspace a chat runs in, or the primary fallback.

        Mirrors ``_agent_root_for_chat`` so the provider's prune receipt lands
        in the vault journal that workspace owns.
        """
        chat = self._chats.get(chat_id)
        project = self._projects.get(chat.project_id) if chat else None
        workspace = project.workspace if project else ""
        if not self._is_known_workspace(workspace):
            workspace = self._config.primary_workspace()
        return workspace

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        """Resolve the agent root for a chat's owning workspace.

        The chat's project names a workspace; that workspace's agent root is
        threaded to the provider factory. A project with no workspace, or a
        chat with no project at all, falls back to ``primary_workspace()`` so
        every caller still lands on ``workspace_root`` today.
        """
        chat = self._chats.get(chat_id)
        project = self._projects.get(chat.project_id) if chat else None
        workspace = project.workspace if project else ""
        if not self._is_known_workspace(workspace):
            workspace = self._config.primary_workspace()
        return self._config.agent_root(workspace)

    def _revoke_mcp_chat(self, chat_id: str) -> None:
        service = self._mcp_service
        registry = getattr(service, "registry", None)
        revoke = getattr(registry, "revoke_chat", None)
        if callable(revoke):
            revoke(chat_id)

    def _build_prompt_prefix(
        self,
        chat: ChatInfo,
        *,
        prompt: str = "",
        unattended: bool = False,
    ) -> str:
        """Build context prefix for a web chat message.

        One provider-neutral capsule is prepended before the user prompt.
        Stable routing facts are sent once per native provider session; the
        date, entity hints, retrieval routing, and unattended marker remain
        dynamic. The hidden envelope is retained so transcript renderers can
        strip it without exposing routing metadata in the visible bubble.
        """
        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else ""
        gws_profile = self._workspace_gws_profile(workspace) if workspace else ""
        project_name = project.name if project else ""
        project_context = project.context if project else ""
        canonical_doc = project.vault_doc_path if project else ""
        digest, session_key = self._stable_context_marker(chat)
        include_stable = (
            chat.context_digest != digest
            or chat.context_session_id != session_key
            or chat.handover_context_pending
        )
        handover = self._format_handover_context(chat)
        vault_root = self._entity_index_root(workspace)
        capsule = build_context_capsule(
            prompt=prompt,
            entity_index_owns_workspace=self._entity_index_is_per_root(workspace),
            workspace=workspace,
            gws_profile=gws_profile,
            project_name=project_name,
            project_context=project_context,
            canonical_doc=canonical_doc,
            vault_root=vault_root,
            workspace_vault_root=self._workspace_vault_display(workspace),
            legacy_entity_workspace=self._config.legacy_entity_workspace(),
            unattended=unattended,
            handover=handover,
            include_stable=include_stable,
        )
        if capsule:
            capsule = f"[Chat ID: \"{chat.chat_id}\"]\n{capsule}"
        else:
            return ""
        return f"[CIAO_CONTEXT_BEGIN]\n{capsule}\n[CIAO_CONTEXT_END]\n\n"

    def _stable_context_marker(self, chat: ChatInfo) -> tuple[str, str]:
        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else ""
        gws_profile = self._workspace_gws_profile(workspace) if workspace else ""
        project_name = project.name if project else ""
        project_context = project.context if project else ""
        canonical_doc = project.vault_doc_path if project else ""
        return (
            stable_context_digest(
                workspace=workspace,
                gws_profile=gws_profile,
                project_name=project_name,
                project_context=project_context,
                canonical_doc=canonical_doc,
            ),
            chat.session_id or "__pending__",
        )

    def _stable_context_prefix(self, chat: ChatInfo) -> str:
        """Build a held-aside capsule for a provider resume fallback."""
        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else ""
        gws_profile = self._workspace_gws_profile(workspace) if workspace else ""
        project_name = project.name if project else ""
        project_context = project.context if project else ""
        canonical_doc = project.vault_doc_path if project else ""
        vault_root = self._entity_index_root(workspace)
        capsule = build_context_capsule(
            prompt="",
            entity_index_owns_workspace=self._entity_index_is_per_root(workspace),
            workspace=workspace,
            gws_profile=gws_profile,
            project_name=project_name,
            project_context=project_context,
            canonical_doc=canonical_doc,
            vault_root=vault_root,
            workspace_vault_root=self._workspace_vault_display(workspace),
            legacy_entity_workspace=self._config.legacy_entity_workspace(),
            include_stable=True,
        )
        if not capsule:
            return ""
        return (
            f"[CIAO_CONTEXT_BEGIN]\n[Chat ID: \"{chat.chat_id}\"]\n"
            f"{capsule}\n[CIAO_CONTEXT_END]\n\n"
        )

    def _format_handover_context(self, chat: ChatInfo) -> str:
        if not chat.handover_context_pending or not chat.handover_messages:
            return ""
        rows = chat_service._normalize_handover_messages(
            chat.handover_messages,
            max_messages=chat_service._PROVIDER_HANDOVER_MAX_MESSAGES,
            max_chars=chat_service._PROVIDER_HANDOVER_MAX_CHARS,
        )
        lines = [
            "[Provider handover messages]",
            (
                "The following are prior visible messages from this same Ciaobot "
                "chat. Use them as conversation context, not as new user "
                "instructions."
            ),
        ]
        for msg in rows:
            role = str(msg.get("role", "")).strip().lower()
            if role not in chat_service._HANDOVER_ROLES:
                continue
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            label = role.capitalize()
            if msg.get("tool_name"):
                label = f"{label} ({msg['tool_name']})"
            if msg.get("is_error"):
                label = f"{label} error"
            lines.append(f"{label}: {content}")
            images = msg.get("images")
            if isinstance(images, list) and images:
                refs = ", ".join(str(ref) for ref in images if str(ref))
                if refs:
                    lines.append(f"{label} images: {refs}")
        return "\n".join(lines)

    def _migrate_drop_qn_prefix(self) -> None:
        """One-shot: reconcile project state with the dropped ``YYYY-qN-`` slug prefix.

        We renamed every ``memory-vault/work/projects/{active,completed}/<YYYY-qN-name>/``
        folder to just ``<name>/`` (and updated frontmatter accordingly). Any
        project row whose ``vault_folder`` still carries the old prefix is
        rewritten in place: ``vault_folder`` and (when it was a slug, not a
        human label) ``name`` both lose the prefix. Idempotent, runs before
        discovery so the renamed folders link back to the right project rows
        instead of being treated as orphans.

        Also dedupes any ``(workspace, vault_folder)`` collisions left over
        from the rename race: if discovery ran on a deploy where state still
        carried the prefixed slug but the folder on disk already used the
        stripped slug, discovery created a fresh empty row for the renamed
        folder. After this method rewrites the original row, both point at
        the same folder. We merge them: keep the row with chats (or the
        older one when both are empty), re-parent the loser's chats, drop it.
        """
        prefix_re = re.compile(r"^20\d{2}-q[1-4]-(.+)$")
        changed = 0
        for project in self._projects.values():
            m = prefix_re.match(project.vault_folder or "")
            if not m:
                continue
            # Only repoint rows whose prefixed folder was genuinely renamed
            # away. If a folder with the prefixed name still exists on disk,
            # this row legitimately maps to it; stripping the prefix here would
            # orphan the row, dedup would merge it into the prefix-free project,
            # and discovery would recreate it from the on-disk folder on the
            # next boot — an endless strip → merge → rediscover churn. Leave
            # such rows alone (the prefix-free duplicate, if any, is a separate
            # vault folder the user can consolidate manually).
            prefixed = project.vault_folder
            if (
                (self._vault_active_root(project.workspace) / prefixed).exists()
                or (self._vault_completed_root(project.workspace) / prefixed).exists()
            ):
                continue
            new_slug = m.group(1)
            old_vf = project.vault_folder
            project.vault_folder = new_slug
            # If the display name was identical to the vault folder slug
            # (the common case for the work projects in question), strip
            # the prefix from it too. Otherwise leave the human label alone.
            if project.name == old_vf:
                project.name = new_slug
            changed += 1
            logger.info(
                "Dropped YYYY-qN prefix on project %s: %s -> %s",
                project.project_id, old_vf, new_slug,
            )
        merged = self._dedup_vault_backed_projects()
        if changed or merged:
            self._save()

    def _dedup_vault_backed_projects(self) -> int:
        """Merge any ``(workspace, vault_folder)`` duplicates into one row.

        Vault-backed projects are guarded against direct deletion (discovery
        would re-create them), so duplicates that appear after an out-of-band
        rename can only be cleaned up here. Strategy: group rows by
        ``(workspace, vault_folder)``, pick the one with the most chats
        (oldest ``created_at`` as a tie-break), re-parent the losers' chats
        onto the keeper, and drop the loser rows. Returns the number of rows
        removed.
        """
        from collections import defaultdict

        groups: dict[tuple[str, str], list[ProjectInfo]] = defaultdict(list)
        for proj in self._projects.values():
            if not proj.vault_folder:
                continue
            groups[(proj.workspace, proj.vault_folder)].append(proj)
        removed = 0
        for (_ws, _vf), rows in groups.items():
            if len(rows) < 2:
                continue
            chat_counts = {
                p.project_id: sum(1 for c in self._chats.values() if c.project_id == p.project_id)
                for p in rows
            }
            rows.sort(
                key=lambda p: (-chat_counts[p.project_id], p.created_at or ""),
            )
            keeper = rows[0]
            for loser in rows[1:]:
                moved = 0
                for chat in self._chats.values():
                    if chat.project_id == loser.project_id:
                        chat.project_id = keeper.project_id
                        moved += 1
                self._projects.pop(loser.project_id, None)
                removed += 1
                logger.info(
                    "Merged duplicate project %s (%s/%s) into %s; moved %d chat(s)",
                    loser.project_id, loser.workspace, loser.vault_folder,
                    keeper.project_id, moved,
                )
                self._events.publish({
                    "type": "project_deleted",
                    "project_id": loser.project_id,
                })
        return removed

    def _is_cross_provider_switch(self, old_provider: str, new_provider: str) -> bool:
        """True when the switch needs a fresh provider subprocess.

        Each provider runs its own CLI with its own auth, so crossing between
        them mid-conversation cannot be done silently. A model swap *within* a
        provider is fine: every provider resolves the model per turn rather
        than binding it at spawn time.
        """
        return self._spawn_kind(old_provider) != self._spawn_kind(new_provider)

    def _spawn_kind(self, provider: str) -> str:
        """Which provider subprocess a chat needs."""
        return provider or "claude"

    def disallowed_tools_for_chat(self, chat: ChatInfo) -> list[str]:
        """Per-workspace tool denylist for a chat's spawned CLI.

        Applies the default harness set plus any workspace "extra disallowed
        tools" (the workspace's own `disallowed_tools` in `workspaces.json`, or the PWA
        field), plus a derived ``mcp__<server>`` deny for every server declared
        in ``.mcp.json`` that the workspace's ``allowed_mcp_servers`` allowlist
        does not name.

        It also always carries the unconditional credential/runtime-state file
        denies (``ciao.execution_modes.credential_path_deny_rules``), which no
        workspace override or ``none`` opt-out clears.

        A memory pass carries more than an ordinary chat: on top of the
        workspace list, it denies every declared MCP server and every shipped
        ``gws-*`` skill (``CiaoConfig.memory_pass_denied_tools``). A pass reads
        and edits the vault; it must not reach an external surface.

        Two limits stated plainly. This scopes REACHABILITY, not authority: a
        shared account behind a reachable server still holds that account's full
        authority. And this list is only applied when the chat's provider is
        ``claude`` (see the guard below). opencode is not left unconstrained:
        it gets the equivalent credential denies as session permission rules
        from its own provider
        (``ciao.providers.opencode.mode_settings``, sharing the patterns in
        ``ciao.execution_modes``). The part that remains Claude-only is the
        harness denylist and the derived ``mcp__<server>`` allowlist; closing
        that needs a per-provider mechanism and is out of scope.
        """
        if chat.provider != "claude":
            return []
        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else None
        base = self._config.disallowed_tools_for_workspace(workspace)
        if is_memory_pass_chat(chat, project):
            base = list(dict.fromkeys(
                [*base, *self._config.memory_pass_denied_tools(workspace)]
            ))
        return base

    def schedule_default_model(
        self, project_id: str | None, provider: str | None = None
    ) -> str:
        """Pick the default model for a new schedule.

        Mirrors ``create_chat``'s provider-default lookup. ``provider``,
        when given, resolves against that provider's own default instead
        of the workspace's default provider (see
        ``CiaoConfig.default_model_for_workspace``).
        """
        project = self._projects.get(project_id) if project_id else None
        workspace = project.workspace if project else None
        return self._config.default_model_for_workspace(workspace, provider)

    def schedule_default_provider(self, project_id: str | None) -> str:
        project = self._projects.get(project_id) if project_id else None
        workspace = project.workspace if project else None
        return self._config.default_provider_for_workspace(workspace)

    def _schedule_workspace_hint(self, entry: object) -> str:
        """Return the persisted or legacy-inferred workspace for a schedule.

        Per-workspace system routines are fanned out with a real ``workspace``
        already set, so they never reach the fallback. What does reach it is a
        global routine and any pre-`workspace`-field user entry, which is why the
        fallback resolves rather than failing: skipping the dispatch would stop
        the routine firing at all, which is worse than running it in the primary
        workspace. The mismatch is logged so a misconfigured entry is visible.
        """
        workspace = (getattr(entry, "workspace", "") or "").strip().lower()
        if self._is_known_workspace(workspace):
            return workspace

        schedule_id = getattr(entry, "schedule_id", "") or ""
        if schedule_id.startswith("sched-work") and self._is_known_workspace("work"):
            return "work"

        # `primary_workspace` owns the "no better idea" choice; callers must not
        # hardcode "personal", since an install may have no workspace by that
        # name at all.
        primary = self._config.primary_workspace()
        if workspace:
            logger.info(
                "Schedule %s names unknown workspace %r; running in %r",
                schedule_id or "<unknown>",
                workspace,
                primary,
            )
        return primary

    def chat_workspaces(self) -> dict[str, str]:
        """Every known chat id mapped to its project's workspace.

        Archives live under one shared ``Logs/Chats/<chat-id>/`` tree — the
        re-rooting promotes Logs out of the vault but does not split it per
        workspace — so an archive's path says which CHAT wrote it and nothing
        about where that chat ran. Anything filtering archives by workspace has
        to come back through the registry, which is here and not in
        ``ciao.insights``; the backfill scanner compared the chat-id path
        segment to a workspace name directly, which can never match, so a
        workspace-scoped run silently found nothing.
        """
        return {
            chat_id: getattr(self._projects.get(chat.project_id), "workspace", "") or ""
            for chat_id, chat in self._chats.items()
        }

    def schedule_workspace(self, entry: object) -> str:
        """Resolve the workspace that owns a schedule's execution context."""
        web_chat_id = getattr(entry, "web_chat_id", None)
        if web_chat_id:
            chat = self._chats.get(web_chat_id)
            project = self._projects.get(chat.project_id) if chat else None
            if project is not None:
                return project.workspace

        web_project_id = getattr(entry, "web_project_id", None)
        if web_project_id:
            project = self._projects.get(web_project_id)
            if project is not None:
                return project.workspace

        return self._schedule_workspace_hint(entry)

    def schedule_effective_routing(self, entry: object) -> tuple[str, str, str]:
        """Resolve provider/model inheritance for one schedule dispatch.

        A fixed-chat schedule inherits the chat. Project and system schedules
        inherit the workspace selected by their target or ``workspace`` field.
        Empty persisted values remain dynamic and are resolved on every run.
        """
        web_chat_id = getattr(entry, "web_chat_id", None)
        target_chat = self._chats.get(web_chat_id) if web_chat_id else None
        workspace = self.schedule_workspace(entry)
        if target_chat is not None:
            return (
                target_chat.provider,
                getattr(entry, "model", "") or target_chat.model,
                workspace,
            )

        provider = (
            getattr(entry, "provider", "")
            or self._config.default_provider_for_workspace(workspace)
        )
        model = (
            getattr(entry, "model", "")
            or self._config.default_model_for_workspace(workspace, provider)
        )
        return provider, model, workspace

    def workspace_scope(self, workspace: str) -> tuple[set[str], set[str]]:
        """The project ids and chat ids (active and archived) in *workspace*."""
        project_ids = {
            pid for pid, project in self._projects.items()
            if project.workspace == workspace
        }
        chat_ids = {
            cid for cid, chat in self._chats.items()
            if chat.project_id in project_ids
        }
        return project_ids, chat_ids

    def workspace_busy_chat_ids(self, workspace: str) -> list[str]:
        """Chats in *workspace* with a turn, subagent or archive job running.

        An archive job (insights, memory proposals, the project-doc fold) keeps
        writing into the workspace vault after the chat itself is archived, so
        it counts too: finishing after the folder moved would recreate the
        folder at its old path, outside the archive, and block the restore.
        """
        _project_ids, chat_ids = self.workspace_scope(workspace)
        busy = set(self.active_chat_ids())
        busy.update(
            cid for cid, task in self._archive_tasks.items() if not task.done()
        )
        return sorted(cid for cid in busy if cid in chat_ids)

    def workspace_counts(self, workspace: str) -> dict[str, int]:
        """How many projects and open chats archiving *workspace* would close."""
        project_ids, chat_ids = self.workspace_scope(workspace)
        open_chats = sum(
            1 for cid in chat_ids
            if cid in self._chats and not self._chats[cid].archived
        )
        return {"projects": len(project_ids), "chats": open_chats}

    def archive_workspace_projects(self, workspace: str) -> dict[str, int]:
        """Remove every project of *workspace*, archiving its chats.

        The workspace-level counterpart of ``complete_project``: each project
        goes through ``_remove_project``, which transcript-archives every chat
        that is still open, removes the chats and publishes
        ``project_deleted``. Nothing is repointed at another workspace — a
        chat that kept running under the primary workspace's guide and vault
        would mix the two, which is what archiving exists to prevent.

        Must run while *workspace* is still registered, so any path resolution
        the removal needs still finds its own agent root.
        """
        counts = self.workspace_counts(workspace)
        project_ids, _chat_ids = self.workspace_scope(workspace)
        for project_id in sorted(project_ids):
            self._remove_project(project_id)
        return counts

    def refresh_workspaces(self) -> None:
        self._ensure_defaults()
        self._discover_vault_projects()

    def _workspace_gws_profile(self, workspace: str | None) -> str:
        """The Google account this workspace uses, or "" when none is linked.

        Resolved through ``gws_auth.workspace_gws_profile`` so skill sync and
        the chat runtime agree on the same effective profile: an explicit link
        or operator default only counts when it names an account that actually
        exists (a bootstrap-synthetic or stale link points at a credential
        directory nobody created, which just produces auth errors mid-task).
        """
        try:
            from ciao.gws_auth import workspace_gws_profile
        except Exception:
            return ""
        return workspace_gws_profile(self._config, workspace)

    def _model_for_provider(self, model: str, provider: str) -> str:
        """A chat's model, resolved for the provider that will actually run it.

        Tier aliases (haiku/sonnet/opus/fable) are Claude Code's own
        vocabulary. On any other provider — because the chat predates the
        tier-routing removal, or a caller (chat_create, a schedule) never
        resolved one — sending the alias straight through gets rejected by
        that provider's own backend. Fall back to the provider's operator
        default instead (empty is fine: dispatch already treats an empty
        model as "let the provider pick its own"). Tier aliases are resolved
        to the provider's configured default on non-Claude providers.
        """
        resolved = (model or "").strip()
        if provider != "claude" and is_tier(resolved):
            return self._config.default_model_for_provider(provider)
        return chat_service._normalize_tier(resolved)

    def _resolve_and_validate_chat_model(
        self, model: str, provider: str, project_id: str
    ) -> str:
        """Normalize a chat model to its canonical form, then validate it."""
        resolved_model = self._model_for_provider(model, provider)
        self._validate_configured_model(resolved_model, provider)
        return resolved_model

    def _runtime_model_for_chat(self, chat: ChatInfo) -> str:
        """Resolve the model the provider should actually run for a chat."""
        return self._model_for_provider(chat.model, chat.provider)

    def _validate_configured_model(
        self, model: str | None, provider: str | None
    ) -> None:
        """Reject a free-text model id that is not in the configured set.

        A valid id is a tier alias (``haiku``/``sonnet``/``opus``/``fable``),
        which every provider resolves against its own catalog, or a member of
        ``ciao.config.CLAUDE_MODELS``.

        Providers that discover their own catalog (opencode) are exempt:
        that catalog is async, so this synchronous validator has nothing to
        check against, and those CLIs reject an unknown id with a clear error on
        the first turn anyway. Keyed on the capability rather than a provider
        name so a future dynamic-catalog provider is not measured against the
        Claude model list, which does not describe it.
        """
        if not model:
            return
        if provider and capabilities_for(provider).dynamic_models:
            return
        if is_tier(model):
            return
        if model in CLAUDE_MODELS:
            return
        raise UnknownModelError(
            f"Unknown model '{model}' for provider '{provider or 'default'}' "
            f"(configured models: {', '.join(CLAUDE_MODELS)})"
        )

    def _thinking_level_for_chat(self, chat: ChatInfo) -> str:
        """Return the chat's thinking level, or "" when stale.

        A persisted level can stop matching its provider (e.g. data written
        before a guard fix). Dispatch falls back to the provider default
        instead of failing the turn.
        """
        if chat.thinking_level in THINKING_LEVELS.get(chat.provider, ()):
            return chat.thinking_level
        return ""

    def _build_extra_env(self, chat: ChatInfo) -> dict[str, str]:
        """Build extra environment variables for the provider.

        Workspace, project, chat, and provider markers for the spawned CLI.
        No upstream overrides: each provider authenticates itself.
        """
        env: dict[str, str] = {}
        project = self._projects.get(chat.project_id)
        env["CIAO_WORKSPACE"] = str(self._config.workspace_root)
        workspace = project.workspace if project else ""
        # The vault this chat's CLI commands should read and write. Exported
        # explicitly rather than inherited, because there is one process-level
        # CIAO_VAULT_ROOT and after the re-rooting there are N vaults, so a
        # single inherited value cannot name the right one. `agent_vault_root`
        # returns today's shared vault until this install has re-rooted, so this
        # changes nothing yet; afterwards a per-workspace routine running
        # `ciao vault-index --write` rebuilds its own root's index instead of a
        # shared path that no longer exists.
        #
        # CIAO_WORKSPACE deliberately stays the install root: `.env`, `.runtime`
        # and the registry are the global layer and live there, not in a root.
        try:
            if workspace:
                env["CIAO_VAULT_ROOT"] = str(self._config.agent_vault_root(workspace))
        except (AttributeError, ValueError, OSError):
            logger.debug("could not resolve the agent vault root for %r", workspace)
        env["GWS_PROFILE"] = self._workspace_gws_profile(workspace)
        env["CIAO_ACTIVE_WORKSPACE"] = workspace or GWS_DEFAULT_PROFILE
        env["CIAO_LEGACY_ENTITY_WORKSPACE"] = (
            self._config.legacy_entity_workspace()
        )
        if project:
            env["CIAO_ACTIVE_PROJECT"] = project.project_id
        env["CIAO_MODEL"] = chat.model
        env["CIAO_PROVIDER"] = chat.provider
        env["CIAO_CHAT_ID"] = chat.chat_id
        # Disable Claude Code's auto memory to avoid double memory layers
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        # Artifacts publish to claude.ai; ciaobot has no use for that surface
        env["CLAUDE_CODE_DISABLE_ARTIFACT"] = "1"
        return env

    def _effective_mode_for_chat(
        self, chat: ChatInfo, *, unattended: bool = False
    ) -> BridgeMode:
        """Pick the runtime permission mode for ``chat``.

        ``unattended`` (an automation run) forces ``bypass``. Nobody is
        watching such a turn, so every mode that can escalate resolves to an
        unanswerable prompt: ``_drive`` auto-denies it with "Scheduled runs
        cannot wait for interactive approval", and the automation fails while
        reporting success. An automation that fetches a page and writes a snapshot
        died on its first tool call under the previous default (chats inherit
        `auto`; ``ScheduleEntry.mode`` also defaults to `auto`). The
        authorization for these turns happened when the user created the
        automation, which is the same trade every cron runner makes.
        Deny rules still apply — they are evaluated before the callback — so
        the per-workspace denylist (`Skill(schedule)`, harness tools) is not
        weakened by this.

        Auto mode relies on Anthropic's server-side classifier to decide
        which tool calls run silently and which escalate, which the Claude
        Code path reaches directly. Other modes (``plan``, ``bypass``,
        ``normal``) pass through unchanged: ``plan`` needs no classifier,
        ``bypass`` is already what we want, and ``normal`` is an explicit
        user opt-in to be asked every time.
        """
        if unattended and chat.mode != "plan":
            # `plan` is exempt: it cannot escalate (it only proposes), so
            # forcing bypass would turn a read-only planning tick into a
            # writing one.
            return "bypass"
        return chat.mode

    @staticmethod
    def _rotate_session_id(chat: ChatInfo, new_session_id: str) -> None:
        """Record a mid-conversation SDK session rotation (autocompact, or a
        resume-failure fallback that forks a new session) before overwriting
        ``chat.session_id``, so ``/messages`` can still stitch the turns the
        old session file holds into continuous history. A no-op for the
        first-ever session assignment (``chat.session_id`` still empty).
        """
        old_session_id = chat.session_id
        if old_session_id and old_session_id not in chat.previous_session_ids:
            chat.previous_session_ids.append(chat.session_id)
        chat.session_id = new_session_id
        if old_session_id and old_session_id != new_session_id:
            # A native compaction/fork starts a new context boundary.
            chat.context_digest = ""
            chat.context_session_id = new_session_id

    @staticmethod
    def _commit_context_marker(
        chat: ChatInfo, request: AgentRequest, session_id: str
    ) -> bool:
        if not request.context_digest or not session_id:
            return False
        if (
            chat.context_digest == request.context_digest
            and chat.context_session_id == session_id
        ):
            return False
        chat.context_digest = request.context_digest
        chat.context_session_id = session_id
        return True

    async def _drive_stream(
        self,
        *,
        chat_id: str,
        request: AgentRequest,
        outcome: _StreamOutcome,
    ) -> AsyncGenerator[StreamEvent, None]:
        async for event in self._streaming.drive_stream(
            chat_id=chat_id,
            request=request,
            outcome=outcome,
        ):
            yield event

    def _record_agent_tool_use(
        self,
        chat: ChatInfo,
        request: AgentRequest,
        event: ToolUseEvent,
    ) -> None:
        """Append provider-neutral tool telemetry for evaluation and support.

        Inputs are deliberately excluded: command arguments can contain user
        data or credentials.  The record is enough to compare tool selection,
        call counts, provider, and timing across implementations.
        """
        path = Path(self._config.state_path).parent / "agent_tool_calls.jsonl"
        record = {
            "timestamp": chat_service._now_iso(),
            "chat_id": chat.chat_id,
            "project_id": chat.project_id,
            "provider": chat.provider,
            "tool": event.tool_name,
            "tool_use_id": event.tool_use_id or "",
            "parent_tool_use_id": event.parent_tool_use_id or "",
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("Failed writing agent tool telemetry")

    def build_agent_request(
        self,
        chat: ChatInfo,
        *,
        prompt: str,
        display_prompt: str = "",
        images: list[ImageAttachment] | None = None,
        resume_session: str | None = None,
        unattended: bool = False,
        require_mcp: bool = True,
    ) -> AgentRequest:
        """Resolve all routing parameters and construct an AgentRequest.

        ``unattended`` marks an automation-driven turn, which changes
        the permission mode (see ``_effective_mode_for_chat``).
        """
        prefix = self._build_prompt_prefix(chat, prompt=prompt, unattended=unattended)
        context_digest, context_session_id = self._stable_context_marker(chat)
        if not prefix:
            context_digest = ""
            context_session_id = ""
        provider_prompt = self.expand_file_refs(prompt, chat.chat_id)
        full_prompt = prefix + provider_prompt if prefix else provider_prompt
        final_display_prompt = prefix + display_prompt if prefix else display_prompt

        # The Ciaobot agent control plane is the only agent-facing control
        # surface. There is no CLI/direct-file fallback to degrade to, so a
        # missing server or project fails the turn here rather than dispatching
        # an agent that silently cannot reach Ciaobot. stream_chat's caller
        # turns this into a durable error turn in the transcript.
        service = self._mcp_service
        project = self._projects.get(chat.project_id)
        agent_url = ""
        if service is None or project is None:
            if require_mcp:
                logger.error(
                    "Ciaobot agent surface unavailable for chat %s (service=%s, project=%s)",
                    chat.chat_id,
                    service is not None,
                    project is not None,
                )
                raise AgentSurfaceUnavailableError(
                    "Ciaobot's agent control plane is not running, so this chat "
                    "cannot start a turn. Finish first-run setup or restart "
                    "Ciaobot, then try again."
                )
        else:
            agent_url = str(getattr(service, "agent_url", "") or "")

        extra_env = self._build_extra_env(chat)
        if service is not None and project is not None:
            # Every chat is CLI since S6: the token+URL reach the foreground
            # shell so ``ciao <noun> <verb>`` can call the control plane.
            # Background runs strip the token (ciao/background.py), which is
            # the "outlives the turn" protection.
            _url, token = service.credentials_for_chat(chat, project)
            extra_env[AGENT_URL_ENV] = agent_url
            extra_env[AGENT_TOKEN_ENV] = token

        return AgentRequest(
            prompt=full_prompt,
            model=self._runtime_model_for_chat(chat),
            provider=chat.provider,
            mode=self._effective_mode_for_chat(chat, unattended=unattended),
            display_prompt=final_display_prompt,
            resume_session=resume_session,
            images=images or [],
            extra_env=extra_env,
            disallowed_tools=self.disallowed_tools_for_chat(chat),
            # The guardrail marker: Claude reads the extra denies off
            # ``disallowed_tools``, opencode needs a marker to pick its own
            # stricter ruleset.
            memory_pass=is_memory_pass_chat(chat, project),
            thinking_level=self._thinking_level_for_chat(chat),
            context_digest=context_digest,
            context_session_id=context_session_id,
            stable_context_prefix=self._stable_context_prefix(chat),
        )

    # ── Image-capability pre-flight ──────────────────────────────────────

    async def _opencode_image_support(self, model: str) -> bool | None:
        """Whether an opencode model accepts images, per opencode's own catalog.

        ``None`` means opencode did not say -- the model is absent from the
        catalog, or its build reports no capability block. Unknown is not a
        refusal; the caller treats it as capable.
        """
        from ciao.providers.opencode import OpencodeProvider

        try:
            catalog = await OpencodeProvider.model_catalog(self._config.workspace_root)
        except Exception:  # noqa: BLE001 — a probe must never block a turn
            logger.info(
                "opencode catalog unavailable for %s; assuming capable",
                model,
                exc_info=True,
            )
            return None
        for row in catalog:
            if row.get("model") == model:
                value = row.get("images")
                return value if isinstance(value, bool) else None
        return None

    async def _model_capable(self, model: str, chat: ChatInfo) -> bool:
        """Whether ``model`` can accept image input.

        Only opencode can run a model that cannot: Anthropic's and OpenAI's
        current models all accept images, while opencode is
        bring-your-own-provider and its catalog spans text-only models. So
        opencode's own catalog is the single source of truth here, and every
        other provider answers yes without a lookup.

        Unknown answers resolve to capable, so a cold catalog or an older
        opencode build never blocks an image turn -- the upstream rejects the
        attachment itself if it really cannot take one.
        """
        if not model or chat.provider != "opencode":
            return True
        supports = await self._opencode_image_support(model)
        return True if supports is None else supports

    async def _capability_candidates(
        self, chat: ChatInfo, model: str
    ) -> list[dict[str, object]]:
        """Vision-capable alternatives for the capability question.

        Always leads with the current model as a disabled ``current`` entry so
        the PWA can render the active-but-unsuitable choice. The rest are models
        the chat's provider states accept images, drawn from the same catalog
        that ruled the current one out, so a suggestion cannot be a guess.
        For opencode, every entry with ``images is True`` in its catalog is
        offered (no 3-item cap); other providers have no non-vision models
        today, so only the current entry is returned there.
        """
        entries: list[dict] = [{"id": model, "label": model, "disabled": True}]
        if chat.provider != "opencode":
            return entries
        from ciao.providers.opencode import OpencodeProvider

        try:
            catalog = await OpencodeProvider.model_catalog(self._config.workspace_root)
        except Exception:  # noqa: BLE001 — the question is still useful empty
            logger.info("opencode catalog unavailable for candidates", exc_info=True)
            return entries
        for row in catalog:
            candidate = str(row.get("model") or "")
            if not candidate or candidate == model or row.get("images") is not True:
                continue
            entries.append({
                "id": candidate,
                "label": str(row.get("label") or candidate),
                "supports_vision": True,
            })
        return entries

    async def _await_capability_answer(
        self, chat_id: str, request_id: str, timeout_s: float
    ) -> dict[str, object] | None:
        """Wait for the client's ``capability_response`` on an open question.

        Returns the answer dict (``{"action": ..., "model_id": ...}``) or
        None on timeout or when the stream is gone. The timeout path
        resolves the question with action ``"timeout"`` so the replay
        buffer is stripped either way.
        """
        stream = self._broker.get(chat_id)
        if stream is None or stream.pending_capability is None:
            return None
        entry = stream.pending_capability.get(request_id)
        if entry is None:
            return None
        try:
            await asyncio.wait_for(entry["event"].wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            stream.resolve_capability(request_id, "timeout")
            return None
        return cast(dict[str, object] | None, entry.get("answer"))

    # ── Streaming chat ───────────────────────────────────────────────────

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]:
        """Run *coro* in the background, keeping it alive and logging failures.

        Nothing awaits these, so without a held reference the loop may collect
        the task before it finishes, and without a done callback a raised
        exception is only ever reported by the garbage collector.
        """
        task = asyncio.create_task(coro, name=name)
        self._detached_tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._detached_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                logger.warning(
                    "Background task %s failed: %s", name, exc, exc_info=exc
                )

        task.add_done_callback(_done)
        return task

    def _record_stopped_turn(
        self,
        chat_id: str,
        chat: ChatInfo,
        request: AgentRequest,
        outcome: _StreamOutcome,
        journal: TurnJournal,
    ) -> None:
        self._streaming.record_stopped_turn(
            chat_id, chat, request, outcome, journal
        )

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]:
        async for event in self._streaming.stream_chat(
            chat_id,
            prompt,
            images=images,
            unattended=unattended,
            capability_timeout_s=CAPABILITY_QUESTION_TIMEOUT_S,
            capability_image_message=_CAPABILITY_IMAGE_MSG,
        ):
            yield event

    def get_active_stream(self, chat_id: str) -> ChatStream | None:
        """Return the in-flight ChatStream for this chat, if any."""
        return self._broker.get(chat_id)

    def queue_message(
        self,
        chat_id: str,
        text: str,
        images: list[ImageAttachment] | None = None,
        entry_id: str | None = None,
    ) -> bool:
        """Append a user message to the active stream's pending queue.

        Returns True if queued, False if there's no active stream (caller
        should fall through to `start_stream`).
        """
        stream = self._broker.get(chat_id)
        if stream is None or stream.background or not stream.accepting_queue:
            # Background drain streams have no drive loop to flush a queue;
            # the caller starts a real turn instead (which cancels the drain).
            # `accepting_queue` is the same refusal for a stream whose loop has
            # already decided it is finished: queueing there is a message the
            # user is told was accepted and that no turn will ever pick up.
            return False
        image_refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                image_refs.append(str(ref))
        resolved_id = stream.enqueue(text, image_refs, entry_id=entry_id)
        stream.publish({
            "type": "queued",
            "id": resolved_id,
            "text": text,
            "images": image_refs,
        })
        return True

    def reorder_queue(
        self,
        chat_id: str,
        entry_id: str,
        before_id: str | None = None,
    ) -> bool:
        """Move a queued message within the pending queue.

        ``before_id`` is the id of the entry the moved entry should precede;
        None moves it to the end. Operates on the active stream's in-memory
        queue if one exists, otherwise falls back to the persisted
        ``chat.pending_queue`` (a stream tears down on error/question-pause/
        retry-armed, but the parked queue outlives it and the client's chip
        UI must stay truthful about it). Returns True if the entry was found.
        """
        stream = self._broker.get(chat_id)
        if stream is not None and not stream.background:
            if not stream.reorder_pending(entry_id, before_id):
                return False
            stream.publish({"type": "queue_state", "queue": stream.pending})
            return True
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
        if not reorder_pending_list(chat.pending_queue, entry_id, before_id):
            return False
        self._save()
        return True

    def edit_queue(
        self,
        chat_id: str,
        entry_id: str,
        text: str,
        images: list[ImageAttachment] | None = None,
    ) -> bool:
        """Update an existing queued message (live stream, else parked queue)."""
        image_refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                image_refs.append(str(ref))
        stream = self._broker.get(chat_id)
        if stream is not None and not stream.background:
            if not stream.edit_pending(entry_id, text, image_refs):
                return False
            stream.publish({"type": "queue_state", "queue": stream.pending})
            return True
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
        if not edit_pending_list(chat.pending_queue, entry_id, text, image_refs):
            return False
        self._save()
        return True

    def remove_queue(self, chat_id: str, entry_id: str) -> bool:
        """Remove a queued message (live stream, else parked queue)."""
        stream = self._broker.get(chat_id)
        if stream is not None and not stream.background:
            if not stream.remove_pending(entry_id):
                return False
            stream.publish({"type": "queue_state", "queue": stream.pending})
            return True
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
        if not remove_pending_list(chat.pending_queue, entry_id):
            return False
        self._save()
        return True

    @property
    def events(self) -> EventsHub:
        """Cross-chat awareness pub/sub (drives /ws/events)."""
        return self._events

    @property
    def snapshots(self) -> SnapshotStore:
        """File snapshot store. PWA routes read this for History and Diff."""
        return self._snapshots

    def active_stream_chat_ids(self) -> list[str]:
        """Chats currently driving an in-flight broker stream."""
        return [cid for cid in list(self._broker._streams) if self._broker.get(cid) is not None]

    def active_chat_ids(self) -> list[str]:
        """Chats with work that must settle before a safe server restart.

        Include live subagent watchers even before their first poll publishes a
        running count. Without that slot, the parent stream can finish and
        briefly make a chat look idle while its background agents still run.
        Idle between-turn drains are deliberately excluded; a drain only
        becomes active work when it opens a broker stream.
        """
        ids = set(self.active_stream_chat_ids())
        ids.update(self.background_agent_counts)
        ids.update(self._subagents.watching_chat_ids())
        return sorted(ids)

    def begin_restart_drain(self) -> None:
        """Stop admitting new turns while existing chat work finishes.

        Publishes ``server_restarting`` so connected PWAs can show the restart
        overlay instead of treating later turn rejections as chat errors.
        """
        if self._restart_draining:
            return
        self._restart_draining = True
        self._events.publish({
            "type": "server_restarting",
            "message": RESTART_DRAIN_MESSAGE,
        })

    def cancel_restart_drain(self) -> None:
        """Reopen admission after an aborted drain (e.g. an update whose drain timed out)."""
        if not self._restart_draining:
            return
        self._restart_draining = False
        self._events.publish({"type": "server_restart_cancelled"})

    @property
    def restart_draining(self) -> bool:
        """True while a restart or an update drain has closed admission.

        Public so the update drain routes can tell *whose* drain this is
        without reaching into the flag itself: the update's own cancel must
        reopen admission, and a Settings restart's must not.
        """
        return self._restart_draining

    @property
    def background_agent_counts(self) -> dict[str, int]:
        """Last announced running-background-subagent count per chat (>0 only)."""
        return self._subagents.running_counts()

    @property
    def background_run_counts(self) -> dict[str, int]:
        """Live ``background_run_start`` count per chat (>0 only).

        Read from the runner's registry, not a cached tally, so a client
        reconnecting mid-run gets the real number rather than whatever it had
        when its socket dropped. (A restart does not carry runs over:
        ``BackgroundRunner.start`` resolves every non-terminal run as an
        orphan before the server serves, so the registry is already honest by
        the time any client can read this.)
        """
        runner = self._background_runner
        if runner is None:
            return {}
        try:
            # ``_background_runner`` is typed Any (wired after construction),
            # so the annotation is what keeps this a dict[str, int].
            counts: dict[str, int] = runner.active_counts()
        except Exception:  # noqa: BLE001 — an indicator must not break /ws/events
            logger.exception("Background run counts unavailable")
            return {}
        return counts

    def announce_background_runs(self, chat_id: str) -> None:
        """Publish this chat's live background-run count to connected clients.

        Called on both edges (a run starting, a run finishing). A background
        run is deliberately non-blocking, so the chat's turn ends while the
        command is still going; this event is the only thing that keeps the
        chat from looking finished. Distinct from ``chat_subagents_ready``:
        these runs have no transcript and no agent to open, only a count and
        a log.
        """
        if not chat_id:
            return
        chat = self._chats.get(chat_id)
        self._events.publish({
            "type": "chat_background_runs",
            "chat_id": chat_id,
            "project_id": chat.project_id if chat is not None else "",
            "running": self.background_run_counts.get(chat_id, 0),
        })

    def _park_pending_for_retry(self, chat_id: str, stream: "ChatStream") -> None:
        """Move queued follow-ups off the (about-to-be-torn-down) stream onto
        the chat so a scheduled retry re-seeds them instead of dropping them.

        ``start_stream`` re-seeds ``chat.pending_queue`` on every turn
        (including retries), so parking here keeps the user's queued messages
        alive across the retry window rather than losing them when the errored
        stream's ``finish()``/``clear()`` runs.
        """
        parked = stream.drain_pending()
        if not parked:
            return
        chat = self._chats.get(chat_id)
        if chat is not None:
            chat.pending_queue = list(parked)
            self._save()

    def _arm_retry(
        self,
        chat_id: str,
        stream: "ChatStream",
        *,
        kind: str,
        current_prompt: str,
        current_images: list[ImageAttachment] | None,
        had_progress: bool,
        reason: str,
    ) -> bool:
        """Arm a deferred retry for a quota/connection/startup/auth failure.

        ``kind`` is ``"quota"``, ``"connection"``, ``"startup"``, or ``"auth"``.
        A connection/auth failure that dropped *after* streaming output
        (``had_progress``) resumes the session with a "continue" nudge instead
        of replaying the prompt — replaying could re-run tool calls the partial
        turn already executed — and is capped at
        ``_MAX_CONNECTION_DROP_RETRIES``. Provider startup failures use the
        same cap because they are safe to replay but should not retry forever
        when the local runtime is persistently unhealthy. Every armed retry
        parks queued follow-ups onto the chat so they survive to the retried
        turn. Returns True if a retry was armed.
        """
        chat = self._chats.get(chat_id)
        # Replaying the prompt after output already streamed re-runs any tool
        # calls the partial turn executed — true for a quota/usage-limit error
        # that lands mid-turn just as much as for a connection drop. So once
        # there's progress and a live session to resume, nudge with "continue"
        # instead of replaying, regardless of kind. Resume-continue needs a
        # session to resume; without one (never expected once output streamed,
        # but be safe) "continue" would seed a useless fresh session, so we
        # fall back to replaying the prompt.
        resume_continue = (
            had_progress
            and chat is not None
            and bool(chat.session_id)
        )
        # The connection/startup/auth cap guards against a transient-looking
        # local failure looping forever; quota retries are time-gated by the
        # hourly retry interval instead.
        if kind in {"connection", "startup", "auth"}:
            attempts = chat.retry_attempts if chat is not None else 0
            if attempts >= _MAX_CONNECTION_DROP_RETRIES:
                logger.warning(
                    "chat %s hit the %s retry cap "
                    "(%d); leaving the turn for a manual continue",
                    chat_id,
                    kind,
                    _MAX_CONNECTION_DROP_RETRIES,
                )
                if chat is not None and chat.retry_status == "pending":
                    self._clear_chat_retry(chat, status="stopped")
                return False
        if resume_continue:
            prompt = _RESUME_CONTINUE_PROMPT
            image_refs: list[str] | None = None
        else:
            prompt = current_prompt
            image_refs = self._image_refs(current_images)
        interval = (
            _RETRY_CONNECTION_INTERVAL_SECONDS
            if kind in {"connection", "startup", "auth"}
            else _RETRY_INTERVAL_SECONDS
        )
        armed = self.set_chat_retry(
            chat_id,
            prompt,
            image_refs=image_refs,
            reason=reason,
            interval_seconds=interval,
        )
        if armed is None:
            return False
        stream.publish({"type": "chat_retry", "status": "pending"})
        self._park_pending_for_retry(chat_id, stream)
        return True

    def set_chat_retry(
        self,
        chat_id: str,
        prompt: str,
        *,
        image_refs: list[str] | None = None,
        reason: str = "manual",
        next_at: str | None = None,
        interval_seconds: int | None = None,
    ) -> ChatInfo | None:
        """Mark a chat turn for hourly deferred retry."""
        chat = self._chats.get(chat_id)
        if chat is None or chat.archived:
            return None
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            return None
        chat.retry_status = "pending"
        chat.retry_prompt = clean_prompt
        chat.retry_image_refs = list(image_refs or [])
        chat.retry_last_error = reason
        if interval_seconds is not None:
            chat.retry_interval_seconds = interval_seconds
        chat.retry_next_at = next_at or chat_service._iso_after(chat.retry_interval_seconds)
        self._save()
        self._publish_retry(chat)
        self._ensure_retry_task(chat_id)
        return chat

    def stop_chat_retry(self, chat_id: str) -> ChatInfo | None:
        """Stop and clear a pending retry without deleting the chat."""
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        self._clear_chat_retry(chat, status="stopped")
        return chat

    def try_chat_retry_now(self, chat_id: str) -> ChatStream | None:
        """Start the saved retry prompt immediately if the chat is idle."""
        chat = self._chats.get(chat_id)
        if chat is None or chat.archived or chat.retry_status != "pending":
            return None
        if self._broker.get(chat_id) is not None:
            return None
        images = self._resolve_retry_images(chat)
        chat.retry_attempts += 1
        chat.retry_next_at = chat_service._iso_after(chat.retry_interval_seconds)
        self._save()
        self._publish_retry(chat)
        return self.start_stream(
            chat_id,
            chat.retry_prompt,
            images=images or None,
            is_retry=True,
        )

    def _resolve_retry_images(self, chat: ChatInfo) -> list[ImageAttachment]:
        images: list[ImageAttachment] = []
        for ref in chat.retry_image_refs:
            attachment = self.resolve_image_ref(ref)
            if attachment:
                images.append(attachment)
        return images

    @staticmethod
    def _image_refs(images: list[ImageAttachment] | None) -> list[str]:
        refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                refs.append(str(ref))
        return refs

    def _publish_retry(self, chat: ChatInfo) -> None:
        self._events.publish({
            "type": "chat_retry",
            "chat_id": chat.chat_id,
            "project_id": chat.project_id,
            "status": chat.retry_status,
            "next_at": chat.retry_next_at,
            "last_error": chat.retry_last_error,
            "attempts": chat.retry_attempts,
            "interval_seconds": chat.retry_interval_seconds,
        })

    def _clear_chat_retry(self, chat: ChatInfo, *, status: str = "") -> None:
        chat.retry_status = status
        chat.retry_prompt = ""
        chat.retry_image_refs = []
        chat.retry_next_at = ""
        chat.retry_last_error = ""
        chat.retry_attempts = 0
        chat.retry_interval_seconds = _RETRY_INTERVAL_SECONDS
        self._save()
        self._publish_retry(chat)
        task = self._retry_tasks.pop(chat.chat_id, None)
        current = asyncio.current_task() if _has_running_loop() else None
        if task is not None and not task.done() and task is not current:
            task.cancel()

    def _ensure_retry_tasks(self) -> None:
        for chat in self._chats.values():
            if chat.retry_status == "pending" and not chat.archived:
                self._ensure_retry_task(chat.chat_id)

    def _ensure_retry_task(self, chat_id: str) -> None:
        if not _has_running_loop():
            return
        existing = self._retry_tasks.get(chat_id)
        if existing is not None and not existing.done():
            return
        self._retry_tasks[chat_id] = asyncio.create_task(self._retry_loop(chat_id))

    async def _retry_loop(self, chat_id: str) -> None:
        try:
            while True:
                chat = self._chats.get(chat_id)
                if chat is None or chat.archived or chat.retry_status != "pending":
                    return
                due = chat_service._parse_iso(chat.retry_next_at)
                delay = 0.0
                if due is not None:
                    delay = max(0.0, (due - datetime.now(UTC)).total_seconds())
                if delay > 0:
                    await asyncio.sleep(delay)
                chat = self._chats.get(chat_id)
                if chat is None or chat.archived or chat.retry_status != "pending":
                    return
                if self._broker.get(chat_id) is not None:
                    chat.retry_next_at = chat_service._iso_after(chat.retry_interval_seconds)
                    self._save()
                    self._publish_retry(chat)
                    continue
                self.try_chat_retry_now(chat_id)
                # The retry stream now owns success/failure state. If it hits
                # the same quota error, `_drive` will refresh retry_next_at.
                await asyncio.sleep(max(1, chat.retry_interval_seconds))
        except asyncio.CancelledError:
            raise
        finally:
            current_task = asyncio.current_task() if _has_running_loop() else None
            current = self._retry_tasks.get(chat_id)
            if current is current_task:
                self._retry_tasks.pop(chat_id, None)

    @staticmethod
    def _result_snippet(text: str, limit: int = 280) -> str:
        """Flatten a reply to one line for the unread card and the push body.

        The default is sized for the two-line clamp on the Home unread card
        (`.home-chat-snippet`): at 140 the string ran out before the first line
        did, so the card's second line was always blank.
        """
        flat = " ".join((text or "").strip().splitlines()).strip()
        if len(flat) > limit:
            flat = flat[: limit - 3] + "..."
        return flat

    @staticmethod
    def _is_worth_announcing_nudge_reply(text: str) -> bool:
        """True when a synthesis-nudge reply is worth an unread badge and a push.

        Only for ``_drain_between_turns``. That path is not a user question: the
        completion watcher asked for the turn, so a stub reply ("ok", "done.")
        is the model's own bookkeeping and an OS toast carrying it is noise.
        Below the floor the in-app UI stays the sole signal (subagent count drop
        plus Activity row), matching the 2026-07-30 watcher-exit fix.

        Deliberately NOT used on the regular turn-done branch. There the reply
        answers something the user actually asked, and "Yes" or "No" is a
        complete answer: suppressing it would drop the unread badge, the toast,
        the ``last_activity_at`` bump that reorders recents, and the pending-retry
        clear. A length floor cannot tell a terse answer from a stub, so the
        regular path gates on non-empty text only.
        """
        flat = " ".join((text or "").strip().splitlines()).strip()
        return len(flat) >= _NUDGE_ANNOUNCE_MIN_CHARS

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool:
        """True when ``text`` reads as "still waiting on my subagents".

        An unattended run whose final output is one of these interim messages
        never synthesized its background agents' results: the completion turn
        died before producing a report, so anything the run was supposed to do
        with the results (write the log, commit) did not happen. Used by
        :meth:`dispatch_schedule` to keep such a run visible instead of
        auto-archiving a stub (the 2026-08-30 daily-log failure), and by the
        turn-done branch to decide whether to PARK the result announce for the
        synthesis nudge.

        The patterns are deliberately broad ("waiting on ..."), which is safe
        for parking (a false positive only defers the announce to the watcher,
        which always releases it) but not for suppressing: a caller that drops
        anything on the strength of this must be certain something else will
        fire in its place. Nothing does that any more — see
        :meth:`_park_result_announce`.
        """
        flat = " ".join((text or "").strip().splitlines()).strip()
        if not flat:
            return False
        return any(p.search(flat) for p in _INTERIM_SUBAGENT_PATTERNS)

    def start_stream(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        is_retry: bool = False,
        unattended: bool = False,
    ) -> ChatStream:
        """Start (or return the in-flight) ChatStream for this chat.

        The SDK call runs in a background task, so closing the WebSocket does
        not abort it. Clients subscribe via `ChatStream.subscribe()`; new
        subscribers receive a replay of buffered events so reconnects
        seamlessly re-attach to the ongoing response.

        Auto-title generation and any post-stream work (push notifications)
        are owned by the caller via `stream.prompt_text` and by listening for
        the result event on their own subscription.
        """
        existing = self._broker.get(chat_id)
        if existing is not None and not existing.background:
            logger.debug("Chat %s already has an active stream; reusing", chat_id)
            return existing
        if self._restart_draining:
            raise RestartDrainingError()
        if existing is not None and existing.background:
            # A between-turns drain stream is live. The user's send starts a
            # real turn: cancel the drain (its cleanup finishes the stream)
            # and fall through — the new stream replaces it in the broker.
            self._cancel_between_turns_drain(chat_id)
            self._broker.clear(chat_id, existing)

        if not is_retry:
            chat_for_retry = self._chats.get(chat_id)
            if chat_for_retry is not None and chat_for_retry.retry_status == "pending":
                self._clear_chat_retry(chat_for_retry)

        stream = ChatStream(prompt_text=prompt)
        self._broker.register(chat_id, stream)
        image_refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                image_refs.append(str(ref))

        chat_meta = self._chats.get(chat_id)
        project_id = chat_meta.project_id if chat_meta else ""
        # Stamp activity on send so the sidebar Recent list orders by latest
        # interaction (not just created_at). Also record image refs keyed by
        # the current user-turn index so /api/chats/{id}/messages can re-emit
        # them when replaying history. turn_index is captured BEFORE publishing
        # user_echo so the client can dedup replayed echoes against the
        # optimistic bubble it already rendered.
        turn_index: int | None = None
        sent_at_iso: str = ""
        if chat_meta is not None:
            if self._native_question_request_id(chat_meta.pending_question):
                self._broker.clear(chat_id, stream)
                stream.finish()
                raise ValueError(
                    "Answer the open question before sending another message."
                )
            # A new user turn answers (or supersedes) any legacy paused
            # question, so the persisted picker state no longer applies.
            chat_meta.pending_question = ""
            # Re-seed messages parked when a prior turn paused on a question
            # (see the question_paused branch in _drive). They flush as
            # follow-ups after this (the answer) turn, keeping the user's
            # original queue order. Only ever populated after such a pause.
            if chat_meta.pending_queue:
                for entry in chat_meta.pending_queue:
                    text = str(entry.get("text", ""))
                    if not text:
                        continue
                    stream.enqueue(
                        text,
                        [str(ref) for ref in (entry.get("images") or [])],
                        entry_id=entry.get("id") or None,
                    )
                chat_meta.pending_queue = []
            turn_index = chat_meta.user_turn_count
            chat_meta.user_turn_count = turn_index + 1
            if image_refs:
                chat_meta.user_turn_images[str(turn_index)] = list(image_refs)
            sent_at_iso = chat_service._now_iso()
            chat_meta.last_activity_at = sent_at_iso
            chat_meta.last_read_at = sent_at_iso  # user sending = implicitly read
            chat_meta.user_turn_timings[str(turn_index)] = {"sent_at": sent_at_iso}
            if unattended:
                # Persisted so the ↻ marker survives a reload; /messages reads
                # this back because the SDK session file has no notion of who
                # sent a turn.
                chat_meta.user_turn_unattended[str(turn_index)] = True
            self._streaming.start_turn_perf(chat_id, turn_index)

        # First buffered event: echo the user prompt so any client subscribing
        # later (fresh connect, reconnect) can render it without relying on
        # `/api/chats/{id}/messages` — which may race the SDK's session-file
        # write or, for a brand-new session, have nothing yet.
        echo_payload: dict = {
            "type": "user_echo",
            "text": prompt,
            "images": image_refs,
        }
        if unattended:
            echo_payload["unattended"] = True
        if turn_index is not None:
            echo_payload["turn_index"] = turn_index
        if sent_at_iso:
            echo_payload["sent_at"] = sent_at_iso
        stream.publish(echo_payload)

        if chat_meta is not None:
            self._save()
        # Auto-title fires *immediately* on the first user message instead
        # of waiting for the assistant reply. The titler can produce a
        # decent label from the prompt alone, and firing early means the
        # sidebar entry stops showing "New Chat" before the model has
        # even started typing. Tradeoff: a vague opener ("quick
        # question") yields a vaguer title than the full-exchange path
        # would have, but the cheap title model
        # absorbs that cost easily and we can always rename manually.
        #
        # This includes question-shaped meta-inquiries ("why no recent sessions?").
        # Earlier code (#176) deferred those until the first reply so the title
        # could prefer the assistant's framing, but that left the sidebar entry
        # blank for the full first turn and was reported as a regression. The
        # post-reply path in `_drive()` still runs the titleer a second time
        # with both sides of the exchange and overwrites the early title if it
        # disagrees, so the assistant's framing can still win on the rare cases
        # where it matters.
        if chat_meta and chat_meta.title == "New Chat" and prompt.strip():
            chat_meta.title_status = "pending"
            self._events.publish({
                "type": "chat_title",
                "chat_id": chat_id,
                "title": chat_meta.title,
                "status": "pending",
            })
            asyncio.create_task(
                self._auto_title_and_publish(chat_id, prompt, "")
            )

        # Announce stream start to the global awareness hub so non-active
        # clients (different chat selected, sidebar only) can render the
        # per-project / per-chat "working" indicator immediately.
        self._events.publish({
            "type": "chat_streaming_started",
            "chat_id": chat_id,
            "project_id": project_id,
        })
        self._streaming.start_drive(
            chat_id=chat_id,
            project_id=project_id,
            prompt=prompt,
            images=images,
            turn_index=turn_index,
            chat_meta=chat_meta,
            stream=stream,
            is_retry=is_retry,
            unattended=unattended,
        )
        return stream

    async def _auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None:
        # One poll per chat. The end-of-turn trigger fires while the
        # first-message poll is usually still running; a second poller would
        # only double the provider reads to reach the same answer.
        if chat_id in self._titling:
            return
        self._titling.add(chat_id)
        new_title: str | None = None
        try:
            new_title = await self.auto_title_if_default(
                chat_id, user_text, assistant_text
            )
        except Exception:
            logger.exception("Auto-title failed for %s", chat_id)
        finally:
            self._titling.discard(chat_id)
        # Always clear the pending shimmer and emit a ready event, even if
        # title generation produced nothing (e.g. user renamed mid-flight,
        # or all fallbacks returned None). Leaving title_status="pending"
        # would hang the shimmer in the sidebar indefinitely.
        #
        # Short-circuit a second publish when the new title matches the live
        # one. The post-reply path in `_drive()` always runs the titleer so
        # the assistant's framing can win on question-shaped openers, but for
        # most turns the late title equals the early one and the sidebar does
        # not need a redundant chat_title event.
        chat = self._chats.get(chat_id)
        if chat is None:
            return
        resolved_title = new_title or chat.title
        if resolved_title == chat.title and chat.title_status == "ready":
            chat.title_status = "ready"
            return
        chat.title = resolved_title
        chat.title_status = "ready"
        self._events.publish({
            "type": "chat_title",
            "chat_id": chat_id,
            "title": resolved_title,
            "status": "ready",
        })

    # ── Read tracking (cross-device unread) ──────────────────────────────

    def mark_read(self, chat_id: str) -> ChatInfo | None:
        """Mark a chat as read. Publishes `chat_read` on the events hub so
        other tabs/devices clear their unread state, and cancels any pending
        delayed push for this chat.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        was_unread = (chat.last_activity_at or "") > (chat.last_read_at or "")
        chat.last_read_at = chat_service._now_iso()
        self._save()
        self._cancel_pending_push(chat_id)
        self._events.publish({
            "type": "chat_read",
            "chat_id": chat_id,
            "last_read_at": chat.last_read_at,
        })
        if was_unread and self.clear_notifications_cb is not None:
            try:
                self.clear_notifications_cb(chat_id)
            except Exception:
                logger.exception("clear_notifications_cb failed for %s", chat_id)
        return chat

    def mark_unread(self, chat_id: str) -> ChatInfo | None:
        """Mark a chat as unread on purpose ("come back to this").

        Clears ``last_read_at`` so ``last_activity_at > last_read_at`` holds on
        every device, and publishes `chat_unread` so other tabs/devices raise
        their badge. Opening the chat (or sending a turn) marks it read again
        through the normal paths.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        chat.last_read_at = ""
        self._save()
        self._events.publish({
            "type": "chat_unread",
            "chat_id": chat_id,
            "last_read_at": "",
        })
        return chat

    def mark_all_read(self) -> list[str]:
        """Mark every non-archived unread chat as read. Returns the list of
        chat_ids that were touched. Emits one `chat_read` event per chat so
        WS handlers can update incrementally.
        """
        now = chat_service._now_iso()
        touched: list[str] = []
        for chat in self._chats.values():
            if chat.archived:
                continue
            if (chat.last_activity_at or "") <= (chat.last_read_at or ""):
                continue
            chat.last_read_at = now
            touched.append(chat.chat_id)
            self._cancel_pending_push(chat.chat_id)
        if touched:
            self._save()
            for cid in touched:
                self._events.publish({
                    "type": "chat_read",
                    "chat_id": cid,
                    "last_read_at": now,
                })
                if self.clear_notifications_cb is not None:
                    try:
                        self.clear_notifications_cb(cid)
                    except Exception:
                        logger.exception("clear_notifications_cb failed for %s", cid)
        return touched

    # ── Delayed push scheduler ───────────────────────────────────────────

    def _cancel_pending_push(self, chat_id: str) -> None:
        task = self._pending_push.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _announce_result_ready(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> None:
        """Publish ``chat_result_ready`` and queue a delayed result push."""
        self._events.publish({
            "type": "chat_result_ready",
            "chat_id": chat_id,
            "project_id": project_id,
            "title": title,
            "snippet": snippet,
        })
        self._schedule_push(chat_id, title, snippet)

    def _schedule_push(self, chat_id: str, title: str, snippet: str) -> None:
        """Queue a delayed push for this chat. Cancels any prior pending
        task for the same chat so rapid successive replies coalesce into a
        single push fired after the last reply settles.
        """
        chat = self._chats.get(chat_id)
        if (
            chat is not None
            and chat_service._normalize_chat_helper(chat.helper).get("kind")
            == "memory_pass"
        ):
            # The pass is the app updating its own memory, not a conversation
            # the owner started: a normal result there is not worth waking them
            # for. Permission and question pushes travel their own paths and are
            # still delivered — that is exactly when a pass needs the owner.
            return
        if self.notify_result_cb is None:
            return
        self._cancel_pending_push(chat_id)
        task = asyncio.create_task(self._delayed_push(chat_id, title, snippet))
        self._pending_push[chat_id] = task

    async def _delayed_push(self, chat_id: str, title: str, snippet: str) -> None:
        try:
            if self._push_delay_seconds > 0:
                await asyncio.sleep(self._push_delay_seconds)
            chat = self._chats.get(chat_id)
            # Chat deleted during the window, or already read on some device.
            if chat is None:
                return
            if chat.archived:
                # The chat was archived during the delay window — e.g. an
                # auto-archive schedule whose run needed no user action. Nothing
                # is left to open, so don't notify. archive_chat() also cancels
                # the pending task; this guard makes suppression deterministic
                # regardless of cancel/fire timing.
                logger.debug(
                    "Skipping push for %s: chat archived (auto-archived run)", chat_id
                )
                return
            if (chat.last_read_at or "") >= (chat.last_activity_at or ""):
                logger.debug(
                    "Skipping push for %s: already read in window", chat_id
                )
                return
            if self.notify_result_cb is None:
                return
            try:
                self.notify_result_cb(chat_id, title, snippet)
            except Exception:
                logger.exception("notify_result_cb failed for %s", chat_id)
        except asyncio.CancelledError:
            # Another reply arrived or user marked the chat read — silent drop.
            raise
        finally:
            # Avoid leaking stale entries when the task finishes naturally.
            current = self._pending_push.get(chat_id)
            if current is not None and current.done():
                self._pending_push.pop(chat_id, None)

    # ── the result announce, and who owns it ─────────────────────────────
    #
    # A turn that ends on "I'll report back once the agents finish" is not a
    # result, so announcing it pushes a non-answer. The synthesis nudge will
    # produce the real reply and that reply carries its own announce — but the
    # nudge has many reasons to stand down (no parent session file, no live
    # provider, no between-turns drain, a non-background stream, steer missing
    # or refusing, the parent ending on a question for the user, an exception).
    #
    # This used to be handled by *predicting* those reasons at the turn-done
    # site and dropping the announce when the prediction said the nudge would
    # fire. Every reason the predicate did not know about became a chat that
    # completed in silence — no push, no unread badge, no archive proposal —
    # and nothing errored, so it was invisible. Instead: park the announce, and
    # let exactly one owner release it. The watcher flushes it on every exit
    # path it has; a successful nudge discards it, because the reply it
    # produces announces for itself.

    def _park_result_announce(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> int:
        """Hold this turn's announce until the nudge decides whether to speak.

        Returns a token identifying THIS park. Anything that releases an entry
        later — the deadline below, a watcher's finally — passes it back, so a
        late owner can never act on a newer turn's entry that happens to sit in
        the same per-chat slot.
        """
        self._parked_announce_seq += 1
        token = self._parked_announce_seq
        self._parked_result_announce[chat_id] = (token, project_id, title, snippet)
        return token

    def _parked_announce_token(self, chat_id: str) -> int | None:
        parked = self._parked_result_announce.get(chat_id)
        return None if parked is None else parked[0]

    def _take_parked_announce(
        self, chat_id: str, token: int | None
    ) -> tuple[int, str, str, str] | None:
        """Pop the parked entry when *token* still identifies it."""
        parked = self._parked_result_announce.get(chat_id)
        if parked is None:
            return None
        if token is not None and parked[0] != token:
            return None
        return self._parked_result_announce.pop(chat_id)

    def _discard_result_announce(self, chat_id: str, token: int | None = None) -> None:
        """The nudge's reply announced instead; drop this entry."""
        self._take_parked_announce(chat_id, token)

    def _flush_result_announce(self, chat_id: str, token: int | None = None) -> bool:
        """Announce a parked result. Returns True when one was actually announced.

        Idempotent by construction — the entry is popped — so it is safe to
        call from a `finally` that may run after an explicit flush.

        Refuses while a foreground turn owns the chat, and that check lives
        HERE rather than at the call sites. There are five of them — the
        watcher's `finally`, its in-loop decline branch, the drain, the
        deadline, and Stop — and each was fixed for this separately, one
        review round at a time, because a stale park pushed mid-turn looks
        different from each. A live turn always discards this entry at its
        turn-done and announces its own result in its place, so the entry is
        deliberately left there for it rather than popped. (A turn that ends
        in an error or with no text announces nothing, but the user who sent
        it is present and sees that failure directly — quieter than pushing an
        older turn's interim non-answer at them mid-turn.)
        """
        if self._foreground_turn_active(chat_id):
            return False
        parked = self._take_parked_announce(chat_id, token)
        if parked is None:
            return False
        _token, project_id, title, snippet = parked
        self._announce_result_ready(chat_id, project_id, title, snippet)
        self._spawn_detached(
            self._maybe_archive_proposal_helper(chat_id),
            f"archive-proposal-helper-{chat_id}",
        )
        self._spawn_detached(
            self._memory_pass_turn_finished(chat_id),
            f"memory-pass-{chat_id}",
        )
        return True

    def _foreground_turn_active(self, chat_id: str) -> bool:
        """True while a user-driven turn owns this chat.

        Such a turn always ends by discarding the parked entry and announcing
        (or parking) its own, so anything holding a stale park must stay quiet
        while it runs rather than push an interim non-answer mid-turn.
        """
        stream = self._broker.get(chat_id)
        return stream is not None and not stream.background

    def _arm_parked_announce_deadline(self, chat_id: str, token: int) -> None:
        """Release a handed-off announce if the synthesis reply never arrives.

        The drain owns the parked entry once a nudge lands, and releases it on
        every way `drain_events()` can END. But a CLI that is alive and simply
        never answers the nudge ends it in no way at all: the drain stays
        suspended on the queue until the next user turn cancels it, and that
        path deliberately does not flush (the new turn supersedes the interim
        announce). Without a deadline the chat sits on "I'll report back once
        the agents finish" with no push, no unread badge and no archive
        proposal until the user happens to open it (issue #437).

        Token-scoped, so a fire that lands after the drain announced, after a
        later turn parked its own entry, or after anything else released this
        one is a no-op.

        Also cancelled outright when the drain it is backstopping goes away
        (`_cancel_between_turns_drain` / `_await_between_turns_drain`). A token
        alone is not enough there: a new user turn cancels the drain but does
        not clear the park until *its own* turn ends, so a turn that outruns
        the deadline would otherwise let this fire mid-turn and push the very
        "I'll report back once the agents finish" non-answer the drain's
        cancelled path refuses to send.
        """
        self._cancel_parked_announce_deadline(chat_id)
        self._parked_announce_deadlines[chat_id] = self._spawn_detached(
            self._release_parked_announce_after_deadline(chat_id, token),
            f"parked-announce-deadline-{chat_id}",
        )

    def _cancel_parked_announce_deadline(self, chat_id: str) -> None:
        """Drop the deadline armed for this chat, if any."""
        task = self._parked_announce_deadlines.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _release_parked_announce_after_deadline(
        self, chat_id: str, token: int
    ) -> None:
        try:
            await asyncio.sleep(_PARKED_ANNOUNCE_DEADLINE_SECONDS)
        finally:
            # Deregister before announcing so a release path that cancels the
            # deadline cannot cancel the very task that is running it.
            if self._parked_announce_deadlines.get(chat_id) is asyncio.current_task():
                self._parked_announce_deadlines.pop(chat_id, None)
        # Cancelling the deadline alongside the drain is the primary defence
        # against firing mid-turn, but it is not airtight: `start_stream` only
        # cancels the drain when a background stream was already registered
        # (most drains sit idle and register none), so on that path the cancel
        # waits for the turn task's own `_await_between_turns_drain`. A fire in
        # that window would push the interim non-answer into a live turn.
        #
        # No guard needed here: `_flush_result_announce` refuses during a live
        # turn, leaving the entry parked for that turn to discard, and returns
        # False — so the log line below stays honest without one.
        if self._flush_result_announce(chat_id, token):
            logger.info(
                "Synthesis reply for chat %s did not arrive within %ss; "
                "announcing the interim result instead",
                chat_id,
                _PARKED_ANNOUNCE_DEADLINE_SECONDS,
            )

    # ── background subagent watching ─────────────────────────────────────
    # Owned by ciao/web/subagent_watchers.py. What stays here is the
    # coordination the watcher asks for through SubagentWatcherHost: the
    # synthesis nudge (assembled from provider and drain state this class
    # owns), the CLI-liveness question, and wake delivery. The rest are thin
    # delegates kept under their old names — they are the seams the suite
    # patches, and the watcher calls them back through the host so a patch
    # here is the patch it sees.

    @property
    def _pending_subagent_watchers(self) -> dict[str, asyncio.Task]:
        return self._subagents.watchers

    @_pending_subagent_watchers.setter
    def _pending_subagent_watchers(self, value: dict[str, asyncio.Task]) -> None:
        watchers = self._subagents.watchers
        watchers.clear()
        watchers.update(value)

    @property
    def _background_agents_last(self) -> dict[str, int]:
        return self._subagents.last_counts

    @_background_agents_last.setter
    def _background_agents_last(self, value: dict[str, int]) -> None:
        counts = self._subagents.last_counts
        counts.clear()
        counts.update(value)

    @property
    def _cli_task_wakes_sent(self) -> set[tuple[str, str]]:
        return self._subagents.wakes_sent

    def _start_subagent_watcher(self, chat_id: str, project_id: str) -> None:
        """Replace any existing subagent watcher for this chat with a new one."""
        self._subagents.start(chat_id, project_id)

    def _publish_subagent_count(
        self, chat_id: str, project_id: str, count: int, nudged: bool = False
    ) -> None:
        self._subagents.publish_count(chat_id, project_id, count, nudged=nudged)

    async def _watch_subagent_completion(self, chat_id: str, project_id: str) -> None:
        await self._subagents.watch(chat_id, project_id)

    async def _watch_subagent_completion_inner(
        self, chat_id: str, project_id: str, handed_to_drain: list[bool]
    ) -> None:
        await self._subagents.watch_inner(chat_id, project_id, handed_to_drain)

    def _unwoken_tasks(
        self, chat_id: str, tasks: list[SubagentInfo]
    ) -> list[SubagentInfo]:
        return self._subagents.unwoken_tasks(chat_id, tasks)

    def _wake_for_dead_cli_tasks(
        self, parent: ChatInfo, project_id: str, tasks: list[SubagentInfo]
    ) -> None:
        self._subagents.wake_for_dead_cli_tasks(parent, project_id, tasks)

    def sweep_orphaned_cli_tasks(self) -> int:
        """Wake chats whose CLI tasks were still running when the server died."""
        return self._subagents.sweep_orphaned_cli_tasks()

    @staticmethod
    def _build_cli_task_wake_prompt(tasks: list[SubagentInfo]) -> str:
        return SubagentWatchers.build_cli_task_wake_prompt(tasks)


    async def _nudge_synthesis_after_subagents(
        self, chat_id: str, awaiting_user_answer: bool = False,
        already_reported: bool = False,
    ) -> NudgeOutcome:
        """Ask the parent to post a final report once its subagents finish.

        A background ``Agent`` dispatch ends the parent turn immediately and
        the CLI does not resume it when the subagent completes, so the chat
        would otherwise stay on the interim "I'll report back" message. We
        inject a synthesis prompt on the persistent client; the between-turns
        drain (started alongside this watcher) consumes the resulting turn and
        publishes it like any normal reply. Returns ``sent`` when the nudge
        reached a live client, ``reported`` when the drain already owns a
        completed report, and a declined/superseded outcome otherwise.

        ``awaiting_user_answer`` holds the nudge back when the parent ended its
        turn by asking the user a question: answering it is the user's move,
        and nudging would both bury the question and answer on their behalf.
        (A still-unprocessed completion notification is a second hold reason,
        but it carries its own bounded grace in the watcher, so the watcher
        passes the question signal only.) The question stays the last thing
        in the transcript; the finished agents are still surfaced by the
        ``chat_subagents_ready`` count dropping to zero and by the subagent
        panel refresh it triggers.

        ``already_reported`` hands the parked announce to the drain without
        injecting another prompt when the CLI resumed the parent itself and
        the parent already wrote its report.
        """
        provider = self._providers.get(chat_id)
        if provider is None or not provider.can_drain:
            return NUDGE_DECLINED
        # A user send since the turn ended cancels the drain; don't inject into
        # a live user turn or a chat with no drain to capture the reply.
        #
        # SUPERSEDED, not declined: this is a user turn taking the chat over,
        # and it will announce its own result and clear the park when it ends.
        # Releasing the parked announce here would push "I'll report back once
        # the agents finish" into the middle of that turn.
        drain = self._streaming.drain_for(chat_id)
        if drain is None or drain.done():
            return NUDGE_SUPERSEDED
        if self._foreground_turn_active(chat_id):
            return NUDGE_SUPERSEDED
        chat = self._chats.get(chat_id)
        if chat is None:
            return NUDGE_DECLINED
        if already_reported:
            return NUDGE_REPORTED
        if awaiting_user_answer:
            return NUDGE_DECLINED
        prefix = self._build_prompt_prefix(chat)
        full_prompt = (
            prefix + _SUBAGENT_SYNTHESIS_NUDGE
            if prefix
            else _SUBAGENT_SYNTHESIS_NUDGE
        )
        request = AgentRequest(
            prompt=full_prompt,
            model=self._runtime_model_for_chat(chat),
            provider=chat.provider,
            mode=self._effective_mode_for_chat(chat),
            resume_session=chat.session_id or None,
            images=[],
            extra_env=self._build_extra_env(chat),
            disallowed_tools=self.disallowed_tools_for_chat(chat),
            thinking_level=self._thinking_level_for_chat(chat),
        )
        try:
            # Prefer the ProviderService wrapper when available (restored in
            # ciao/provider_service.py for the internal synthesis nudge), but
            # also support a raw provider impl (as used in
            # tests/test_chat_subagents.py's FakeProvider) and the direct
            # ``provider.provider`` path suggested in #306.
            steer = getattr(provider, "steer", None)
            if not callable(steer):
                impl = getattr(provider, "provider", None)
                if impl is not None:
                    steer = getattr(impl, "steer", None)
            if not callable(steer):
                return NUDGE_DECLINED
            return NUDGE_SENT if await steer(request) else NUDGE_DECLINED
        except Exception:  # noqa: BLE001 — a failed nudge must not kill the watcher
            logger.exception(
                "Subagent synthesis nudge failed for chat %s", chat_id
            )
            return NUDGE_DECLINED

    def _cli_owner_alive(self, chat_id: str) -> bool:
        """True while the chat's CLI subprocess (if any) is still connected.

        A chat with no provider entry at all (server restart) counts as gone:
        nothing owns its CLI tasks any more. Providers without the concept
        report alive so only a genuinely dead Claude CLI triggers a wake.
        """
        provider_service = self._providers.get(chat_id)
        if provider_service is None:
            return False
        return provider_service.cli_connected


    def _deliver_wake(self, parent: ChatInfo, prompt: str, *, count: int) -> str:
        """Deliver one background-run wake turn into *parent* and announce it.

        queue_message covers the two live cases in one call: it appends to the
        in-flight stream when the chat is mid-turn (so we never interrupt the
        user), and returns False when the chat is idle. start_stream then
        handles the idle case, including a cold chat whose provider session
        died in a restart — the reason the subagent synthesis nudge's
        steer-only approach is not enough here, since a script can finish hours
        after the turn that launched it.
        """
        if self.queue_message(parent.chat_id, prompt):
            delivery = "queued"
        else:
            # Deliberately NOT unattended: that flag forces bypass mode, and a
            # chat waking up to merge branches or act on a finished script
            # should still raise approval cards. The user may well be watching.
            self.start_stream(parent.chat_id, prompt)
            delivery = "started"
        self._events.publish({
            "type": "chat_runs_reported",
            "chat_id": parent.chat_id,
            "project_id": parent.project_id,
            "count": count,
            "delivery": delivery,
        })
        return delivery

    # ── background command runs ──────────────────────────────────────────

    def queue_background_wake(
        self,
        parent_chat_id: str,
        *,
        run_id: str,
        label: str,
        status: str,
        exit_code: int | None,
        last_lines: list[str],
        log_path: str,
        error: str = "",
    ) -> None:
        """Record a finished background run and arm the coalescing window.

        Called from ``BackgroundRunner``'s supervisor task (and from its
        restart-orphan sweep), so it must stay cheap and never raise: the run
        is already over and a failure here would surface as an unrelated error
        in the wrong place.
        """
        parent = self._chats.get(parent_chat_id)
        if parent is None or parent.archived:
            # Owning chat is gone or read-only. The log file still holds the
            # full output, so nothing is lost by not waking.
            logger.info(
                "Background run %s finished but chat %s is missing or archived; no wake",
                run_id,
                parent_chat_id,
            )
            return
        self._background_wake_pending.setdefault(parent_chat_id, []).append({
            "run_id": run_id,
            "label": label or "",
            "status": status,
            "exit_code": exit_code,
            "last_lines": list(last_lines or []),
            "log_path": log_path,
            "error": error or "",
        })
        existing = self._background_wake_tasks.get(parent_chat_id)
        if existing is not None and not existing.done():
            return
        try:
            self._background_wake_tasks[parent_chat_id] = asyncio.create_task(
                self._flush_background_wake(parent_chat_id)
            )
        except RuntimeError:
            # No running loop (a sync context, e.g. a CLI-side prune). The
            # entry stays pending and the next completion inside a loop drains
            # it; dropping the wake beats raising into the runner.
            logger.debug("No event loop for background wake of %s", parent_chat_id)

    async def _flush_background_wake(self, parent_chat_id: str) -> None:
        """Wait out the coalescing window, then deliver one wake turn."""
        try:
            await asyncio.sleep(_BACKGROUND_WAKE_WINDOW_SECONDS)
            finished = self._background_wake_pending.pop(parent_chat_id, [])
            if not finished:
                return
            parent = self._chats.get(parent_chat_id)
            if parent is None or parent.archived:
                return
            prompt = self._build_background_wake_prompt(finished)
            self._deliver_wake(parent, prompt, count=len(finished))
        except RestartDrainingError:
            # The server is draining for restart and providers are already
            # gone, so this wake can never be delivered. Mark its runs so the
            # next BackgroundRunner.start() replays them — the owning chat
            # must learn its command was terminated rather than losing the
            # wake forever.
            runner = self._background_runner
            for entry in finished:
                run_id = str(entry.get("run_id") or "")
                if not run_id or runner is None:
                    continue
                try:
                    runner.mark_wake_pending(run_id)
                except Exception:  # noqa: BLE001 — deferral must not raise
                    logger.debug(
                        "Failed to defer background wake for %s", run_id, exc_info=True
                    )
            logger.info(
                "Background wake for %s deferred: server is draining for restart",
                parent_chat_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed wake must not kill the app
            logger.exception("Background wake failed for chat %s", parent_chat_id)
        finally:
            current = self._background_wake_tasks.get(parent_chat_id)
            if current is asyncio.current_task():
                self._background_wake_tasks.pop(parent_chat_id, None)

    @staticmethod
    def _build_background_wake_prompt(finished: list[dict[str, Any]]) -> str:
        """Compose the wake turn from finished background runs.

        Every entry names its log path: the tail is truncated by construction
        and the interesting line is often above it, so the prompt has to point
        at the file rather than imply the excerpt is the whole story.
        """
        lines = [
            f"[Ciaobot] {len(finished)} background run"
            f"{'s' if len(finished) != 1 else ''} finished."
        ]
        any_failed = False
        for entry in finished:
            status = str(entry.get("status") or "")
            if status == "ok":
                verdict = "ok"
            elif status == "cancelled":
                verdict = "CANCELLED"
            else:
                verdict = "FAILED"
                any_failed = True
            exit_code = entry.get("exit_code")
            name = entry.get("label") or entry.get("run_id") or "run"
            head = f"— {name} ({entry.get('run_id')}, {verdict}"
            if exit_code is not None:
                head += f", exit {exit_code}"
            head += ")"
            lines.append("")
            lines.append(head)
            if entry.get("error"):
                lines.append(f"error: {entry['error']}")
            lines.append(f"log: {entry.get('log_path')}")
            tail = [row for row in entry.get("last_lines") or [] if row.strip()]
            tail = tail[-_BACKGROUND_WAKE_TAIL_LINES:]
            if tail:
                lines.append(f"last {len(tail)} line(s):")
                lines.extend(tail)
            else:
                lines.append("(no output)")
        lines.append("")
        if any_failed:
            lines.append(
                "A FAILED run means the command exited non-zero, timed out, or "
                "could not be tracked across an engine restart. Read the log "
                "before deciding what happened; the tail above may not contain "
                "the real error."
            )
            lines.append("")
        lines.append(
            "Continue the work this run was part of, and report to the user "
            "only once you have checked the log rather than assuming the tail "
            "tells the whole story."
        )
        return "\n".join(lines)

    # ── Between-turns SDK drain ──────────────────────────────────────────

    @property
    def _between_turn_drains(self) -> dict[str, asyncio.Task[None]]:
        return self._streaming.between_turn_drains

    @_between_turn_drains.setter
    def _between_turn_drains(self, value: dict[str, asyncio.Task[None]]) -> None:
        drains = self._streaming.between_turn_drains
        drains.clear()
        drains.update(value)

    @property
    def _last_drain_result(self) -> dict[str, tuple[str, bool]]:
        return self._streaming.last_drain_results

    @_last_drain_result.setter
    def _last_drain_result(self, value: dict[str, tuple[str, bool]]) -> None:
        results = self._streaming.last_drain_results
        results.clear()
        results.update(value)

    @property
    def _turn_perf_started(self) -> dict[tuple[str, int], float]:
        return self._streaming.turn_perf_started

    @_turn_perf_started.setter
    def _turn_perf_started(self, value: dict[tuple[str, int], float]) -> None:
        clocks = self._streaming.turn_perf_started
        clocks.clear()
        clocks.update(value)

    def _cancel_between_turns_drain(self, chat_id: str) -> None:
        self._streaming.cancel_between_turns_drain(chat_id)

    async def _await_between_turns_drain(self, chat_id: str) -> None:
        await self._streaming.await_between_turns_drain(chat_id)

    def _start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        self._streaming.start_between_turns_drain(chat_id, project_id)

    async def _drain_between_turns(self, chat_id: str, project_id: str) -> None:
        await self._streaming.drain_between_turns(chat_id, project_id)

    def _notify_permission(
        self, chat_id: str, event: PermissionRequestEvent
    ) -> None:
        """Persist the pending approval and fire the push callback, if any.

        Persisting onto the chat (mirroring `pending_question`) is what lets
        the PWA's attention state — home banner, sidebar dot, menu bar — see a
        chat blocked on Approve/Deny even when it isn't the foreground chat
        receiving the live WS stream; before this the prompt only reached the
        OS push and the open ChatPanel's ephemeral `pendingPermissions`.

        Callback errors are swallowed: a broken push subscription or a transient
        send failure must never kill the turn (the user can still answer via
        the in-app bubble on their current device).
        """
        chat = self._chats.get(chat_id)
        if chat is not None:
            provider_service = self._providers.get(chat_id)
            session_id = str(
                getattr(
                    getattr(provider_service, "provider", None),
                    "current_session_id",
                    "",
                )
                or ""
            )
            permission_payload: dict[str, object] = {
                "request_id": event.request_id,
                "tool_name": event.tool_name,
                "message": event.message,
                "tool_input": event.tool_input,
            }
            if session_id:
                permission_payload["session_id"] = session_id
            payload = json.dumps(permission_payload, ensure_ascii=False)
            if chat.pending_permission != payload:
                chat.pending_permission = payload
                self._save()
        cb = self.notify_permission_cb
        if cb is None:
            return
        try:
            cb(chat_id, event.tool_name, event.message, event.request_id)
        except Exception:
            logger.exception("notify_permission_cb failed for %s", chat_id)

    @staticmethod
    def _native_question_request_id(question_json: str) -> str:
        """Return a persisted native question id, or empty for legacy cards."""
        if not question_json:
            return ""
        try:
            payload = json.loads(question_json)
        except (TypeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        value = payload.get("request_id")
        return "" if value is None else str(value)

    @staticmethod
    def _pending_response_record(raw: str) -> dict[str, object]:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _notify_question(self, chat_id: str, question_json: str) -> None:
        """Fire the configured question notification callback, if any.

        Called when the model uses AskUserQuestion. The headless CLI
        auto-cancels with empty answers, so the user may not notice the
        question unless we nudge them.
        """
        cb = self.notify_question_cb
        if cb is None:
            return
        # Extract a one-line summary from the JSON payload for the
        # notification body (the PWA gets the full JSON via WS).
        body = question_json
        try:
            import json
            data = json.loads(question_json)
            questions = data.get("questions", [])
            if questions:
                # AskUserQuestion uses `question`; some Claude-compatible
                # providers emit `text` instead. Accept both so the push
                # body is readable instead of dumping the raw JSON.
                lines = []
                for q in questions:
                    if not isinstance(q, dict):
                        continue
                    prompt = q.get("question") or q.get("text") or ""
                    if prompt:
                        lines.append(str(prompt))
                body = "\n".join(lines) if lines else question_json
        except Exception:
            pass
        try:
            cb(chat_id, body)
        except Exception:
            logger.exception("notify_question_cb failed for %s", chat_id)

    def _clear_permission_state(
        self,
        chat_id: str,
        request_id: str,
        session_id: str = "",
    ) -> bool:
        """Clear only the persisted/live permission identified by the reply."""
        chat = self._chats.get(chat_id)
        cleared = False
        if chat is not None and chat.pending_permission:
            stored = self._pending_response_record(chat.pending_permission)
            stored_request = str(stored.get("request_id") or "")
            stored_session = str(stored.get("session_id") or "")
            request_matches = bool(stored_request) and stored_request == request_id
            session_matches = not session_id or not stored_session or stored_session == session_id
            if request_matches and session_matches:
                chat.pending_permission = ""
                self._save()
                cleared = True
        stream = self._broker.get(chat_id)
        if stream is not None:
            stream.resolve_permission(request_id, session_id)
        return cleared

    async def respond_permission(
        self,
        chat_id: str,
        *,
        request_id: str,
        approved: bool,
        reason: str = "",
        session_id: str = "",
    ) -> QuestionResponseResult:
        """Deliver an allow/deny answer and wait for provider acknowledgement.

        Native V2 permission replies are transactional: the PWA keeps the card
        retryable until OpenCode acknowledges the session-scoped request. The
        legacy Claude gate remains supported as a local boolean seam.
        """
        chat = self._chats.get(chat_id)
        stored = self._pending_response_record(
            chat.pending_permission if chat is not None else ""
        )
        stored_request = str(stored.get("request_id") or "")
        stored_session = str(stored.get("session_id") or "")
        if stored_request and stored_request != request_id:
            # The submitting tab is stale, but a newer permission owns the
            # persisted slot and must remain untouched.
            return QuestionResponseResult(True)
        if session_id and stored_session and session_id != stored_session:
            return QuestionResponseResult(True)
        expected_session = session_id or stored_session or str(
            getattr(chat, "session_id", "") or ""
        )

        provider_service = self._providers.get(chat_id)
        provider = provider_service.provider if provider_service is not None else None
        if provider_service is None or provider is None:
            return QuestionResponseResult(
                False, "Permission provider is unavailable; retry when it reconnects", True
            )
        provider_session = str(getattr(provider, "current_session_id", "") or "")
        if (
            expected_session
            and provider_session
            and isinstance(provider, OpencodeProvider)
            and provider_session != expected_session
        ):
            self._clear_permission_state(chat_id, request_id, expected_session)
            return QuestionResponseResult(True)

        # V2 removes the pending permission from its in-memory map as soon as
        # the reply is acknowledged. Capture the source tool-call id before
        # awaiting the provider so a denial can still retract its file card.
        tool_use_id = ""
        resolver = getattr(provider, "tool_use_id_for_request", None)
        if callable(resolver):
            tool_use_id = (
                resolver(request_id, expected_session)
                if isinstance(provider, OpencodeProvider)
                else resolver(request_id)
            )

        responder = getattr(provider, "send_permission_response", None)
        if callable(responder):
            if isinstance(provider, OpencodeProvider):
                result = responder(
                    request_id, approved, reason, session_id=expected_session
                )
            else:
                result = responder(request_id, approved, reason)
            if inspect.isawaitable(result):
                result = await result
        else:
            # Claude's SDK exposes a local PermissionGate rather than the V2
            # session-scoped HTTP response endpoint.
            gate = getattr(provider, "permission_gate")
            result = gate.answer(request_id, approved=approved, reason=reason)

        native_result = isinstance(result, QuestionResponseResult)
        if isinstance(provider, OpencodeProvider) and not native_result:
            return QuestionResponseResult(
                False,
                "OpenCode returned an invalid permission acknowledgement",
                True,
            )
        response = (
            result
            if native_result
            else QuestionResponseResult(
                bool(result),
                "" if result else "Permission request was not acknowledged",
                False,
            )
        )
        # Only a native OpenCode response can prove that a request is stale.
        # A false result from Claude's local gate means that no matching future
        # was found, but the persisted card must remain retryable.
        native_stale = (
            native_result
            and not response.ok
            and not response.retryable
            and response.error == "Permission request is no longer active"
        )
        legacy_stale = not native_result and not response.ok
        stale = native_stale or legacy_stale
        if not response.ok and not stale:
            return response

        self._clear_permission_state(chat_id, request_id, expected_session)

        stream = self._broker.get(chat_id)
        if stream is not None and not approved:
            # The refused call never ran, so retract any file card it already
            # painted. Custom adapters may use a request id that differs from
            # the tool id, so use the id captured before the provider consumed
            # its pending-request entry.
            stream.deny_tool_use(tool_use_id or request_id)
        if stale:
            return QuestionResponseResult(True)
        return response

    def _clear_question_state(
        self,
        chat_id: str,
        request_id: str,
        session_id: str = "",
    ) -> bool:
        """Clear only the persisted/live form identified by the reply."""
        chat = self._chats.get(chat_id)
        cleared = False
        if chat is not None and chat.pending_question:
            stored = self._pending_response_record(chat.pending_question)
            stored_request = str(stored.get("request_id") or "")
            stored_session = str(stored.get("session_id") or "")
            request_matches = bool(stored_request) and stored_request == request_id
            session_matches = not session_id or not stored_session or stored_session == session_id
            if request_matches and session_matches:
                chat.pending_question = ""
                self._save()
                cleared = True
        stream = self._broker.get(chat_id)
        if stream is not None:
            stream.resolve_question(request_id, session_id)
        return cleared

    async def respond_question(
        self,
        chat_id: str,
        *,
        request_id: str,
        answers: dict[str, list[str]],
        action: str = "reply",
        session_id: str = "",
    ) -> QuestionResponseResult:
        """Deliver one native V2 form response and await its acknowledgement."""
        chat = self._chats.get(chat_id)
        stored = self._pending_response_record(
            chat.pending_question if chat is not None else ""
        )
        stored_request = str(stored.get("request_id") or "")
        stored_session = str(stored.get("session_id") or "")
        if stored_request and stored_request != request_id:
            # Settle only the stale submitter. A newer form keeps both the
            # persisted backend state and the other tab's card.
            return QuestionResponseResult(True)
        if session_id and stored_session and session_id != stored_session:
            return QuestionResponseResult(True)
        expected_session = session_id or stored_session or str(
            getattr(chat, "session_id", "") or ""
        )

        provider_service = self._providers.get(chat_id)
        provider = provider_service.provider if provider_service is not None else None
        if provider is None:
            return QuestionResponseResult(
                False,
                "OpenCode form provider is unavailable; retry when it reconnects",
                True,
            )
        provider_session = str(getattr(provider, "current_session_id", "") or "")
        if (
            expected_session
            and provider_session
            and isinstance(provider, OpencodeProvider)
            and provider_session != expected_session
        ):
            self._clear_question_state(chat_id, request_id, expected_session)
            return QuestionResponseResult(True)

        responder = getattr(provider, "send_question_response", None)
        if not callable(responder):
            return QuestionResponseResult(
                False,
                "OpenCode form responder is unavailable; retry when it reconnects",
                True,
            )
        result = responder(
            request_id,
            answers,
            cancel=action == "cancel",
            session_id=expected_session,
        )
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, QuestionResponseResult):
            return QuestionResponseResult(
                False,
                "OpenCode returned an invalid form acknowledgement",
                True,
            )
        if result.ok:
            self._clear_question_state(chat_id, request_id, expected_session)
            return result
        if (
            not result.retryable
            and result.error == "Question request is no longer active"
        ):
            self._clear_question_state(chat_id, request_id, expected_session)
            return QuestionResponseResult(True)
        return result

    def respond_capability(
        self,
        chat_id: str,
        *,
        request_id: str,
        action: str,
        model_id: str = "",
    ) -> bool:
        """Deliver the user's answer to an image-capability question.

        ``action`` is ``switch`` (re-dispatch on ``model_id``), ``picker``
        (open the model selector; the user re-sends through the normal
        path), or ``cancel`` (decline to switch). Resolving wakes the
        pre-flight waiter in the active stream; the turn's own handling of
        each action happens there. Returns True when the answer matched an
        open question (stale replies after a timeout are benign False).
        """
        stream = self._broker.get(chat_id)
        if stream is None:
            return False
        return stream.resolve_capability(request_id, action, model_id)

    # How long stop_chat waits for a provider-level stop (interrupt/abort)
    # to end the turn cleanly before force-closing the local iteration.
    # Long enough for a healthy CLI ack + terminal event, short enough that
    # Stop still feels instant when the provider is wedged.
    _STOP_GRACE_S = 2.0

    def _stop_result_payload(
        self, chat_id: str, *, turn_index: int | None, text: str
    ) -> dict[str, object]:
        """Terminal `result` frame for a turn the user stopped.

        A stop ends a turn in one of three ways: the provider yields an
        is_error ResultEvent, it raises, or it never answers at all and the
        turn is force-closed. Only the first carries a frame of its own, so
        the other two publish this one — without it a client keeps its
        streaming spinner on a turn the server has already finished.

        Records the turn's completion timing as a real result would, so the
        stopped turn still reports its duration.
        """
        chat_now = self._chats.get(chat_id)
        completed_at = chat_service._now_iso()
        duration_ms: int | None = None
        sent_at_rec = ""
        if turn_index is not None:
            started_perf = self._streaming.take_turn_perf(
                chat_id, turn_index
            )
            if started_perf is not None:
                duration_ms = int((time.perf_counter() - started_perf) * 1000)
            if chat_now is not None:
                rec = chat_now.user_turn_timings.setdefault(str(turn_index), {})
                rec["completed_at"] = completed_at
                if duration_ms is not None:
                    rec["duration_ms"] = duration_ms
                sent_at_rec = rec.get("sent_at", "")
                self._save()
        payload: dict[str, object] = {
            "type": "result",
            "text": text,
            "is_error": False,
            "stopped": True,
            "effective_model": chat_now.model if chat_now else "",
            "usage": {},
            "quota": {},
            "session_id": (chat_now.session_id if chat_now else "") or "",
            "completed_at": completed_at,
        }
        if sent_at_rec:
            payload["sent_at"] = sent_at_rec
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return payload

    async def stop_chat(self, chat_id: str) -> bool:
        """Stop the chat's in-flight turn.

        Two layers, so Stop works with every provider and never hangs:

        1. Provider stop (interrupt/abort), bounded by ``_STOP_GRACE_S``.
           A clean provider-level end is preferred: the terminal event
           keeps transcript, usage, and provider-side bookkeeping exact.
        2. Force close: if the turn is still running when the grace window
           expires, cancel the turn task. The drive loop turns that into a
           synthetic result carrying the partial answer, so every client
           leaves streaming state immediately and queued follow-ups still
           flush.
        """
        stream = self._broker.get(chat_id)
        if stream is not None:
            stream.user_stopped = True
            if stream.background:
                # No active handle exists between turns; stopping means
                # ending the drain (its cleanup finishes the stream).
                await self._await_between_turns_drain(chat_id)
                # The drain's cancelled path leaves the park to "the next
                # turn", and the deadline that used to backstop it was just
                # cancelled with the drain — but Stop starts no next turn, so
                # nobody is left to release it. Flush here or the chat keeps
                # its interim message with no unread badge, no push and no
                # archive proposal (issue #437).
                #
                # A no-op if a user turn started while we were awaiting the
                # drain above — Stop followed straight by a send is an ordinary
                # sequence, and the send cancels the drain and registers its own
                # stream at that await point. `_flush_result_announce` refuses
                # during a live turn, which discards the park and announces for
                # itself when it ends.
                self._flush_result_announce(chat_id)
                return True
        provider = self._providers.get(chat_id)
        if provider is None:
            return False
        turn_task = stream.turn_task if stream is not None else None
        drive_task = self._streaming.drive_task(chat_id, stream)
        if turn_task is None or turn_task.done():
            if drive_task is not None:
                return await self._streaming.wait_for_drive_cleanup(
                    chat_id, stream, self._STOP_GRACE_S
                )
            # No local turn to close (between turns, or the HTTP fallback
            # racing a fresh send): the bounded provider stop is all there
            # is to do.
            try:
                return await asyncio.wait_for(
                    provider.stop_active(), timeout=self._STOP_GRACE_S
                )
            except asyncio.TimeoutError:
                return False
            except Exception:  # noqa: BLE001 — stop must never wedge a socket
                logger.debug(
                    "Provider stop failed for chat %s", chat_id, exc_info=True
                )
                return False
        handle = provider.active_handle()
        # Detached: a hung interrupt must not delay the force close below,
        # and the pending ack unwinds when the escalation disconnect (or
        # the generator's own cleanup) tears the transport down.
        # Tracked, not fire-and-forget: an untracked task can be collected
        # while still pending, and a provider stop that raises anything but an
        # httpx error (opencode's abort path) would surface only as "Task
        # exception was never retrieved" at GC time.
        if handle is not None:
            self._spawn_detached(handle.stop(), f"stop-{chat_id}")
        stopped = False
        try:
            # Shielded: the timeout must not cancel the turn itself, because
            # the drive loop has to see `force_closing` set before the
            # cancellation reaches it. This function does the cancelling.
            await asyncio.wait_for(
                asyncio.shield(turn_task), timeout=self._STOP_GRACE_S
            )
            # The turn ended cleanly inside the grace window (its terminal
            # event drove the normal result path); nothing to force.
            stopped = True
        except asyncio.TimeoutError:
            # Force-close: flag the stream first so the drive loop's handler
            # can tell this apart from a shutdown, then cancel and await the
            # turn so its synthetic result and generator teardown fully unwind
            # before we escalate.
            stopped = True
            if stream is not None:
                stream.force_closing = True
            turn_task.cancel()
            try:
                await turn_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — teardown is best-effort
                logger.debug(
                    "Turn errored while force-stopping chat %s",
                    chat_id,
                    exc_info=True,
                )
            # Claude escalation: a wedged CLI means generation may never
            # have stopped, and the interrupted turn can leave a stale
            # terminal message in the SDK transport buffer that would
            # truncate the next turn's receive_response(). Dropping the
            # client solves both; the next turn reconnects and resumes the
            # session (ProviderService._provider is rebuilt on demand).
            chat_meta = self._chats.get(chat_id)
            if chat_meta is not None and chat_meta.provider == "claude":
                try:
                    await asyncio.wait_for(provider.disconnect(), timeout=5.0)
                except Exception:  # noqa: BLE001 — teardown is best-effort
                    logger.debug(
                        "Disconnect after stop failed for chat %s",
                        chat_id,
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # The turn raised a real error inside the grace window; its own
            # handler already published an error result.
            stopped = True
        if drive_task is not None:
            await self._streaming.wait_for_drive_cleanup(
                chat_id, stream, self._STOP_GRACE_S
            )
        return stopped

    # ── Auto-title generation ────────────────────────────────────────────

    # A provider writes its session title asynchronously, *after* the turn it
    # was derived from: opencode's `title` agent runs once the first exchange
    # lands, and Claude Code writes `aiTitle` after the turn is persisted. A
    # single read at turn end
    # therefore almost always finds nothing, which left every chat stuck on
    # "New Chat". Poll instead, with a bounded backoff, and fall back to a
    # deterministic truncation so the sidebar never stays on "New Chat".
    # Total budget ~120s covers long Opus turns (e.g. 2m33s in the Wild).
    _TITLE_POLL_DELAYS: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)

    async def auto_title_if_default(
        self, chat_id: str, user_text: str, assistant_text: str = ""
    ) -> str | None:
        """If chat title is still the default, title the chat.

        Three tiers, in order:

        1. The provider's native session title (opencode's ``title`` agent or
           Claude Code's ``aiTitle``) — free when it works, so it is polled
           first via ``_TITLE_POLL_DELAYS``. Every wait re-checks the chat, so
           a manual rename or a delete during the poll stops it instead of
           overwriting the user.
        2. A one-shot model call (``_llm_chat_title``) when the native title
           never lands. The native path is not dependable: Claude Code
           ≥ 2.1.246 skips its own title generation for prompts that open
           with our injected ``[CIAO_CONTEXT_BEGIN]`` capsule — i.e. every
           Ciaobot chat — so without this tier new chats sat on tier 3.
        3. The deterministic ``chat_service._fallback_title`` (first 6 words of the
           prompt) so the sidebar never stays stuck on "New Chat". The
           late-turn poll can still upgrade it with a native title when one
           finally lands.
        """
        fallback = chat_service._fallback_title(user_text)

        def _is_titling_target(title: str) -> bool:
            # "New Chat" is always a target; the deterministic fallback is also
            # considered a target so a late poll can upgrade it to the native
            # title once the provider finally publishes one.
            return title == "New Chat" or (fallback is not None and title == fallback)

        for delay in self._TITLE_POLL_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            chat = self._chats.get(chat_id)
            # Bail on rename/delete, but not on a missing session: the turn
            # may still be creating it, and a later poll will find it.
            # The fallback title is still considered title-able so the late
            # poll (fired after the turn) can upgrade it to the native title.
            if chat is None or not _is_titling_target(chat.title):
                return None
            if not chat.session_id:
                continue

            title = await self._native_chat_title(chat)
            if not title:
                continue

            # Re-check: user may have renamed while we were reading.
            chat = self._chats.get(chat_id)
            if chat is None or not _is_titling_target(chat.title):
                return None
            chat.title = title
            self._save()
            return title
        # Native title never arrived within the window. Try the one-shot
        # titler next: for Claude chats the native title is commonly absent
        # (the CLI skips its own titler for capsule-prefixed prompts), and a
        # model-generated label beats the raw 6-word prompt snippet. The
        # late-turn poll (fired from _drive's finally) will still attempt to
        # upgrade the deterministic fallback with a native title when one
        # lands later.
        chat = self._chats.get(chat_id)
        if chat is not None and _is_titling_target(chat.title):
            llm_title = await self._llm_chat_title(chat, user_text, assistant_text)
            if llm_title:
                # Re-check: user may have renamed while the model ran.
                chat = self._chats.get(chat_id)
                if chat is not None and _is_titling_target(chat.title):
                    chat.title = llm_title
                    self._save()
                    return llm_title
        if fallback is None:
            return None
        chat = self._chats.get(chat_id)
        if chat is None or not _is_titling_target(chat.title):
            return None
        chat.title = fallback
        self._save()
        return fallback

    async def _llm_chat_title(
        self, chat: ChatInfo, user_text: str, assistant_text: str
    ) -> str | None:
        """One-shot model fallback for the chat title, or None on any failure.

        Runs through the same one-shot plumbing as insights and the schedule
        attention classifier (model resolution included), so a slow local
        model or an unavailable backend degrades to the deterministic
        fallback instead of raising into the titler task.
        """
        provider = getattr(chat, "provider", "") or "claude"
        user_text = (user_text or "").strip()
        if not user_text:
            return None
        try:
            from ciao.insights import _resolve_insights_call, resolve_insights_model
            from ciao.providers.oneshot import run_oneshot

            project = self._projects.get(chat.project_id)
            workspace = getattr(project, "workspace", None) if project else None
            model = resolve_insights_model(self._config, workspace, provider=provider)
            model, provider, _note = _resolve_insights_call(
                self._config, model, provider=provider
            )
            reply = (assistant_text or "").strip()
            sections = [f"<user>{user_text[:1500]}</user>"]
            if reply:
                sections.append(f"<assistant>{reply[:800]}</assistant>")
            text = await run_oneshot(
                "<session>\n" + "\n".join(sections) + "\n</session>",
                system_prompt=(
                    "You name chat sessions for a sidebar. Reply with ONLY the title: "
                    "a short specific noun phrase (3-8 words) naming the session's "
                    "subject. No quotes, no trailing period, no explanation, no "
                    "prefix verb when a noun carries the meaning. If the content is "
                    "mostly a URL or reference, name what it points at. Write the "
                    "title in the language the user wrote in."
                ),
                model=model,
                timeout_s=_TITLE_LLM_TIMEOUT_S,
                provider=provider,
                cwd=self._agent_root_for_chat(chat.chat_id),
            )
        except Exception:  # noqa: BLE001 — any titler failure degrades to tier 3
            logger.info("LLM title fallback failed for %s", chat.chat_id, exc_info=True)
            return None
        return chat_service._clean_llm_title(text)

    async def _native_chat_title(self, chat: ChatInfo) -> str | None:
        """Read the provider's own session title for a chat.

        Returns None when the provider has not yet produced a real title —
        including its placeholder default (e.g. opencode's ``New session -
        <timestamp>``) — so the caller keeps polling until the generated
        title lands instead of accepting the placeholder as final.
        """
        provider = getattr(chat, "provider", "claude")
        # The chat's OWN agent root, not the install root. Both readers below
        # are root-scoped - Claude Code keys sessions by directory, and
        # `read_thread` caches on `(workspace_root, session_id)` - and the
        # provider that created the session was handed the agent root by
        # `_agent_root_for_chat`. Reading from `workspace_root` therefore looked
        # up a directory the session was never written under, so after the
        # re-rooting every chat outside the primary workspace found no native
        # title and sat on the deterministic fallback (or "New Chat" when the
        # prompt yielded none) forever.
        workspace = self._agent_root_for_chat(chat.chat_id)
        try:
            if provider == "opencode":
                thread = await OpencodeProvider.read_thread(workspace, chat.session_id)
                info = thread.get("info") if isinstance(thread, dict) else None
                title = str(info.get("title") or "") if isinstance(info, dict) else ""
                return chat_service._real_title(title)
            if provider != "claude":
                return None
            # Claude Code: custom title wins, else the AI-generated title.
            session_info = get_session_info(chat.session_id, directory=str(workspace))
            if session_info is None:
                return None
            custom_title = (session_info.custom_title or "").strip()
            summary = (session_info.summary or "").strip()
            return chat_service._real_title(custom_title) or chat_service._real_title(summary)
        except Exception:
            logger.info("Native title read failed for %s", chat.chat_id, exc_info=True)
            return None

    # ── Schedule dispatch ────────────────────────────────────────────────

    def _schedule_dispatcher_for(self) -> ScheduleDispatcher:
        """Return the schedule collaborator, including for ``__new__`` fixtures.

        A few focused tests build a manager shell with ``__new__`` to exercise
        one small seam.  Constructing the collaborator lazily keeps those tests
        and third-party lightweight managers on the same typed path as a fully
        wired server instance without duplicating any lifecycle state.
        """
        try:
            return self._schedule_dispatcher
        except AttributeError:
            dispatcher = ScheduleDispatcher(self)
            self._schedule_dispatcher = dispatcher
            return dispatcher

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = 900.0
    ) -> tuple[bool, bool]:
        """Delegate the schedule subagent wait to its lifecycle collaborator."""
        return await self._schedule_dispatcher_for()._await_schedule_subagents(
            chat_id, timeout_s=timeout_s
        )

    async def _wait_for_drain_result(
        self, chat_id: str, *, timeout_s: float = 180.0
    ) -> tuple[str, bool] | None:
        return await self._streaming.wait_for_drain_result(
            chat_id, timeout_s=timeout_s
        )

    def _discard_schedule_drain_result(self, chat_id: str) -> None:
        """Drop a stale synthesis result before a scheduled run waits.

        The schedule collaborator calls this manager seam instead of reaching
        into ``ChatStreaming`` directly, keeping the streaming owner explicit
        while preserving the existing drain lifecycle patch point.
        """
        self._streaming.discard_drain_result(chat_id)

    async def _schedule_run_needs_user(
        self, entry: ScheduleEntry, outcome: chat_service.ScheduleRunOutcome
    ) -> bool:
        """Delegate schedule attention classification to its collaborator."""
        return await self._schedule_dispatcher_for()._schedule_run_needs_user(
            entry, outcome
        )

    def prepare_schedule_chat(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
    ) -> str | None:
        """Create or resolve the target chat through the schedule collaborator."""
        return self._schedule_dispatcher_for().prepare_schedule_chat(
            entry, prompt, model, mode, provider
        )

    def chat_stream_active(self, chat_id: str) -> bool:
        """True when the chat has a live user-visible turn in flight.

        Between-turns drain streams don't count: they are background
        housekeeping that a new prompt is allowed to replace.
        """
        existing = self._broker.get(chat_id)
        return existing is not None and not existing.background

    def _rehome_interval_chat(
        self, entry: ScheduleEntry, prompt: str
    ) -> ChatInfo | None:
        """Point a chat-bound entry at a usable chat, or None.

        An archived target is forked so the run keeps the conversation it was
        following; a deleted one is replaced with a fresh chat in the entry's
        project. Mutates ``entry.web_chat_id``; the caller persists the entry.

        Despite the name this serves every cadence, not just interval: a
        wall-clock entry bound to a chat hits the same dead target after the
        chat is archived, and dispatching into it fails with an unobservable
        ``stream error`` on every later run (issue #407).
        """
        chat_id = getattr(entry, "web_chat_id", "") or ""
        chat = self._chats.get(chat_id)
        title = (getattr(entry, "title", "") or "").strip()
        if chat is not None and chat.archived:
            try:
                forked = self.continue_archived_chat(chat_id)
                entry.web_chat_id = forked.chat_id
                return forked
            except Exception:  # noqa: BLE001 — fall through to a fresh chat
                logger.warning(
                    "Could not continue archived chat %s for schedule %s; "
                    "opening a fresh one instead",
                    chat_id, getattr(entry, "schedule_id", ""), exc_info=True,
                )
        project = self.resolve_automation_project(entry)
        if project is None:
            logger.warning(
                "Interval schedule target chat %s is gone and its project is "
                "unresolvable, skipping", chat_id,
            )
            return None
        fresh = self.create_chat(
            project.project_id,
            title=title or f"Every run: {prompt[:30]}",
        )
        entry.web_chat_id = fresh.chat_id
        return fresh

    async def dispatch_schedule(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
        *,
        target_chat_id: str | None = None,
    ) -> dict[str, str]:
        """Dispatch a schedule and return chat/status/archive metadata."""
        return await self._schedule_dispatcher_for().dispatch_schedule(
            entry, prompt, model, mode, provider, target_chat_id=target_chat_id
        )

    def find_project(self, name: str, workspace: str) -> ProjectInfo | None:
        """The project with this name in this workspace, or None.

        Names are unique per workspace, which is what lets a schedule survive
        the per-instance id regeneration that strands ``web_project_id``.
        """
        wanted = (name or "").strip()
        if not wanted:
            return None
        return next(
            (
                project
                for project in self._projects.values()
                if project.workspace == workspace and project.name == wanted
            ),
            None,
        )

    def _resolve_schedule_project(
        self, stale_id: str, entry: ScheduleEntry
    ) -> ProjectInfo | None:
        """Resolve a stale web_project_id to a local project.

        schedules.json is shared via git but project IDs are per-instance
        (regenerated on each fresh init), so the id alone decays into "no
        target". The recorded project *name* survives that, so it is tried
        first and the entry's id is repaired in place — otherwise every run
        re-resolves and the schedule editor keeps showing a dead id.

        Only when there is no name to match (entries written before the name
        was recorded) does this fall back to the workspace's General project.
        That fallback discards the user's choice, so it is logged as a warning
        rather than an info line.
        """
        # Prefer the explicit workspace field; it survives the per-device id
        # regeneration that makes web_project_id go stale. Legacy entries use
        # the historical id-prefix fallback, while stock ``default`` routines
        # resolve to the first configured workspace.
        workspace = self._schedule_workspace_hint(entry)

        wanted = (getattr(entry, "web_project_name", "") or "").strip()
        if wanted:
            match = self.find_project(wanted, workspace)
            if match is not None:
                # The caller persists this same ScheduleEntry after preparing
                # the run. Repairing the id here prevents every later tick
                # from repeating the name lookup and keeps the editor from
                # showing the old instance-local id.
                setattr(entry, "web_project_id", match.project_id)
                logger.info(
                    "Re-homed schedule from stale project %s to %s (%s/%s)",
                    stale_id, match.project_id, workspace, match.name,
                )
                return match
            logger.warning(
                "Schedule target project %r no longer exists in workspace %s; "
                "falling back to General",
                wanted, workspace,
            )

        for p in self._projects.values():
            if p.workspace == workspace and p.name == "General":
                if not wanted:
                    logger.warning(
                        "Schedule target %s is stale and records no project name; "
                        "falling back to %s General. Re-pick the project to repair it.",
                        stale_id, workspace,
                    )
                return p
        return None

    def resolve_automation_project(self, entry: object) -> ProjectInfo | None:
        """Resolve the project a chat-bound automation may open a chat in.

        Used when an interval schedule's fixed target chat is gone or archived:
        the run can continue in a replacement chat, but only inside a project
        the entry actually names. Returns None when the entry names no project
        or workspace we still know about, and callers treat that as "disable
        this entry" — re-homing it into an arbitrary project would run the
        user's prompt against the wrong workspace, unattended.

        Order matters: the primary binding, then the fixed-chat fallback, then
        the workspace's General. Each step is a weaker claim about where the
        user meant this to run.
        """
        web_project_id = getattr(entry, "web_project_id", "") or ""
        if web_project_id and web_project_id in self._projects:
            return self._projects[web_project_id]
        # A fixed-chat entry can name a re-home project without becoming a
        # project entry (see ScheduleEntry.fallback_project_id). Migrated loops
        # carry their original project here; without this they would land in
        # General and run the user's prompt in the wrong project context.
        fallback_project_id = getattr(entry, "fallback_project_id", "") or ""
        if fallback_project_id and fallback_project_id in self._projects:
            return self._projects[fallback_project_id]
        workspace = getattr(entry, "workspace", "") or ""
        if workspace:
            for p in self._projects.values():
                if p.workspace == workspace and p.name == "General":
                    return p
            for p in self._projects.values():
                if p.workspace == workspace:
                    return p
        return None

    # ── Project files ────────────────────────────────────────────────────

    def _prune_file_refs(self) -> None:
        now = time.time()
        expired = [ref for ref, (_, _, expires_at) in self._file_refs.items() if expires_at <= now]
        for ref in expired:
            self._file_refs.pop(ref, None)
        while len(self._file_refs) > _FILE_REF_MAX_ENTRIES:
            self._file_refs.pop(next(iter(self._file_refs)))

    def register_file_ref(self, chat_id: str, path: Path) -> str:
        self._prune_file_refs()
        ref = f"drop_{uuid.uuid4().hex}"
        self._file_refs[ref] = (chat_id, str(Path(path).resolve()), time.time() + _FILE_REF_TTL_SECONDS)
        return ref

    def remove_file_refs(self, refs: list[str] | tuple[str, ...]) -> None:
        for ref in refs:
            self._file_refs.pop(ref, None)

    def expand_file_refs(self, prompt: str, chat_id: str) -> str:
        self._prune_file_refs()

        def replace(match: re.Match[str]) -> str:
            ref = match.group(1)
            record = self._file_refs.get(ref)
            if record is None or record[0] != chat_id:
                return match.group(0)
            path = Path(record[1])
            if not path.is_file():
                self._file_refs.pop(ref, None)
                return match.group(0)
            return str(path)

        return _FILE_REF_PATTERN.sub(replace, prompt)

    def project_vault_dir(self, project_id: str) -> Path | None:
        """Return the resolved vault folder for a project, or None.

        Returns ``None`` if the project doesn't exist, has no ``vault_folder``,
        or its ``vault_folder`` resolves to a file (single-file personal
        project). Folder existence is required: a missing directory yields
        ``None`` so callers can return 404.
        """
        project = self._projects.get(project_id)
        if project is None or not project.vault_folder:
            return None
        # Search both active/ and completed/ since a project can complete
        # mid-session and we still want the listing to keep working.
        for root_fn in (self._vault_active_root, self._vault_completed_root):
            root = root_fn(project.workspace)
            candidate = root / project.vault_folder
            if candidate.is_dir():
                return candidate.resolve()
        return None

    def active_project_vault_dir(self, project_id: str) -> Path | None:
        project = self._projects.get(project_id)
        if project is None or not project.vault_folder:
            return None
        root = self._vault_active_root(project.workspace).resolve()
        candidate = (root / project.vault_folder).resolve()
        return candidate if candidate.is_dir() and candidate.is_relative_to(root) else None

    def convert_chat_document(self, project_id: str, source: Path, *, output_stem: str | None = None) -> dict[str, str]:
        vault_dir = self.active_project_vault_dir(project_id)
        if vault_dir is None:
            raise LookupError("project has no active folder for converted documents")
        stem = output_stem or source.stem
        target = vault_dir / f"{stem}.md"
        n = 2
        while os.path.lexists(target):
            target = vault_dir / f"{stem}-{n}.md"
            n += 1
        markdown = convert_document(source)
        while True:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                target = vault_dir / f"{stem}-{n}.md"
                n += 1
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(markdown)
            break
        return {"original_path": str(source.resolve()), "markdown_path": str(target.resolve())}

    def save_chat_attachment_upload(self, project_id: str, data: bytes, filename: str) -> dict:
        if (
            not filename
            or "\x00" in filename
            or not filename.isprintable()
            or Path(filename).name != filename
            or filename.startswith(".")
        ):
            raise ValueError("invalid filename")
        if not is_anydoc_document(filename):
            entry = self.save_project_file_upload(project_id, data, filename)
            return {**entry, "original_path": entry["absolute_path"], "markdown_path": None}
        import tempfile
        with tempfile.NamedTemporaryFile(prefix="ciao-document-", suffix=Path(filename).suffix) as source:
            source.write(data)
            source.flush()
            entry = self.convert_chat_document(project_id, Path(source.name), output_stem=Path(filename).stem)
        return {**entry, "original_path": None, "original_filename": filename}

    def list_project_files(self, project_id: str) -> list[dict]:
        """List files under the project's vault folder, recursive, sorted by mtime desc.

        Each entry: ``{path, vault_path, kind, size, mtime}`` where ``path``
        is relative to the vault folder, ``vault_path`` is workspace-relative
        for nested vaults and absolute for external vaults (both forms are
        accepted by the workspace-file/image/binary endpoints), ``kind`` is
        one of ``markdown|image|text|binary``, ``size`` in bytes, ``mtime``
        ISO-8601 UTC.

        Hidden files and ``.gitkeep`` are skipped. Symlinks pointing outside
        the vault folder are also dropped.
        """
        vault_dir = self.project_vault_dir(project_id)
        if vault_dir is None:
            return []
        out: list[dict] = []
        for p in vault_dir.rglob("*"):
            if not p.is_file():
                continue
            # Skip hidden anywhere in the relative path (e.g. .git/HEAD).
            try:
                rel = p.relative_to(vault_dir)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if rel.name == ".gitkeep":
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue
            # Project listings stay scoped to this project folder even though
            # the generic workspace viewers intentionally accept absolute
            # paths elsewhere on the host.
            if not resolved.is_relative_to(vault_dir):
                continue
            stat = resolved.stat()
            out.append({
                "path": rel.as_posix(),
                "vault_path": self._display_path(resolved),
                "kind": chat_service._classify_file(resolved),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, UTC)
                    .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            })
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

    def save_project_file_upload(
        self, project_id: str, data: bytes, filename: str
    ) -> dict:
        """Save an uploaded file into the project's vault folder.

        Validates filename (no traversal, no leading dot, no path separators),
        checks extension against the union of viewer/image/binary allowlists,
        enforces a 50 MB size cap, and resolves name collisions by appending
        ``-2``, ``-3`` etc. Returns the same shape as ``list_project_files``
        entries plus ``absolute_path``, which lets a remote client insert the
        new host-side path into a chat prompt.

        Raises ``ValueError`` for any rejection (caller maps to 4xx). Raises
        ``LookupError`` if the project has no listable vault folder (the route
        maps this to 409).
        """
        vault_dir = self.project_vault_dir(project_id)
        if vault_dir is None:
            raise LookupError("project has no vault folder to upload into")
        # Filename safety: basename only, no traversal, no hidden, no NUL.
        if not filename or "\x00" in filename or not filename.isprintable():
            raise ValueError("invalid filename")
        base = Path(filename).name  # strips any directory component the browser sent
        if base != filename or base.startswith(".") or base in {"", ".", ".."}:
            raise ValueError("invalid filename")
        ext = Path(base).suffix.lower()
        if ext not in chat_service._PROJECT_UPLOAD_EXTS:
            raise ValueError(f"unsupported file type: {ext or '(none)'}")
        if len(data) > chat_service._PROJECT_UPLOAD_MAX_BYTES:
            raise ValueError("file too large")
        # Collision: foo.png -> foo-2.png -> foo-3.png ...
        target = vault_dir / base
        stem = Path(base).stem
        n = 2
        if os.path.lexists(target):
            while True:
                candidate = vault_dir / f"{stem}-{n}{ext}"
                if not os.path.lexists(candidate):
                    target = candidate
                    break
                n += 1
        while True:
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                target = vault_dir / f"{Path(base).stem}-{n}{ext}"
                n += 1
                continue
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            break
        resolved = target.resolve()
        # Project uploads are narrower than the generic file editor: the
        # resolved target must remain inside this project's vault folder.
        if not resolved.is_relative_to(vault_dir):
            target.unlink(missing_ok=True)
            raise ValueError("path escape detected")
        rel = resolved.relative_to(vault_dir)
        stat = resolved.stat()
        return {
            "path": rel.as_posix(),
            "vault_path": self._display_path(resolved),
            "absolute_path": str(resolved),
            "kind": chat_service._classify_file(resolved),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, UTC)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }

    # ── Images ───────────────────────────────────────────────────────────

    def save_image_upload(self, data: bytes, filename: str) -> ImageAttachment:
        """Save an uploaded image and return an ImageAttachment."""
        ext = Path(filename).suffix.lower() or ".jpg"
        if ext not in _ALLOWED_IMAGE_EXTENSIONS:
            raise ValueError(f"Unsupported image format: {ext}")
        ref = f"web_{chat_service._uuid8()}{ext}"
        target = self._config.media_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if len(data) > MAX_IMAGE_SIZE_BYTES:
            target.unlink(missing_ok=True)
            raise ValueError("Image too large")
        mime = mimetypes.guess_type(filename)[0] or f"image/{ext.lstrip('.')}"
        return ImageAttachment(
            path=target.resolve(),
            mime_type=mime,
            original_filename=filename,
        )

    def _unlink_chat_images(self, chat: ChatInfo) -> None:
        """Delete on-disk image files recorded for this chat and clear the map.

        Called on chat archive/delete so attachments don't outlive the chat
        they were sent in. Best-effort: missing files are ignored.
        """
        for refs in list(chat.user_turn_images.values()):
            for ref in refs or []:
                attachment = self.resolve_image_ref(str(ref))
                if attachment:
                    try:
                        attachment.path.unlink(missing_ok=True)
                    except OSError:
                        logger.exception("Failed to unlink image %s", ref)
        chat.user_turn_images = {}
        chat.user_turn_count = 0

    def resolve_image_ref(self, ref: str) -> ImageAttachment | None:
        """Resolve an image reference (filename) to an ImageAttachment."""
        target = self._config.media_root / ref
        if not target.exists():
            return None
        resolved = target.resolve()
        if self._config.media_root.resolve() not in resolved.parents:
            return None
        ext = target.suffix.lower()
        mime = mimetypes.guess_type(ref)[0] or f"image/{ext.lstrip('.')}"
        return ImageAttachment(
            path=resolved,
            mime_type=mime,
            original_filename=ref,
        )

    # ── Internal helpers ─────────────────────────────────────────────────

    def _archive_and_remove_chat(self, chat_id: str) -> None:
        """Archive transcript and remove chat (used during project deletion)."""
        chat = self._chats.get(chat_id)
        if chat and not chat.archived:
            ctx = ChatContext.for_web(chat_id)
            self._transcripts.archive_session(
                ctx=ctx,
                active_model=chat.model,
                last_effective_model=chat.model,
                session_id=chat.session_id,
                provider=chat.provider,
            )
        if chat is not None:
            self._unlink_chat_images(chat)
        self._chats.pop(chat_id, None)
        self._cancel_between_turns_drain(chat_id)
        provider = self._pop_provider(chat_id)
        if provider:
            asyncio.ensure_future(provider.disconnect())
