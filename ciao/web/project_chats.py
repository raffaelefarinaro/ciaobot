"""Project + chat hierarchy manager for the PWA."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator

import yaml

from ciao import job_runs, subagent_tracking
from ciao.config import BridgeConfig
from ciao.error_log import clear_error_log, tail_error_log
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    ChatContext,
    ImageAttachment,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    THINKING_LEVELS,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.model_tiers import CODEX_FABLE_THINKING_LEVEL, canonical_tier
from ciao.providers.ollama import (
    is_local_ollama_model,
    is_ollama_model,
)
from ciao.providers.codex import (
    CodexProvider,
    codex_collab_tree_counts,
)
from ciao.providers.routing import intended_backend, routing_env_for_model
from ciao.provider_service import ProviderService, supported_providers
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore, _claude_projects_dir
from ciao.web.chat_broker import ChatStream, ChatStreamBroker, EventsHub
from ciao.web.commands import expand_slash_command
from ciao.web.file_snapshots import SnapshotStore

logger = logging.getLogger(__name__)

_ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_ALLOWED_VOICE_EXTENSIONS = {".webm", ".ogg", ".oga", ".mp3", ".m4a", ".wav"}

# Project-files surface (list + upload). Mirrors the union of the read-only
# workspace-file/image allowlists plus the new binary one (PDF, ZIP, office
# docs). Kept in sync intentionally: anything we let users upload, we also
# need to be able to serve back via one of the workspace endpoints.
_PROJECT_TEXT_EXTS = frozenset({
    ".md", ".markdown", ".txt",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".css", ".html", ".json",
    ".yaml", ".yml", ".toml",
    ".sh", ".rs", ".go", ".java", ".xml", ".sql",
    ".cfg", ".ini", ".log", ".csv", ".excalidraw",
})
_PROJECT_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico",
})
_PROJECT_BINARY_EXTS = frozenset({
    ".pdf", ".zip", ".docx", ".xlsx", ".pptx",
})
_PROJECT_UPLOAD_EXTS = _PROJECT_TEXT_EXTS | _PROJECT_IMAGE_EXTS | _PROJECT_BINARY_EXTS
_PROJECT_UPLOAD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
_RETRY_INTERVAL_SECONDS = 60 * 60
_RETRY_STATUSES = {"pending", "stopped", ""}

# Injected into the parent turn when its background subagents all finish. The
# CLI does not auto-continue a parent turn after a background `Agent` dispatch
# completes (see ciao/system_prompt.md), so without this nudge the chat stays
# stuck on the interim "I'll report back when they finish" message. The nudge
# is delivered on the persistent client so the already-running between-turns
# drain captures and publishes the synthesis turn like a normal reply.
_SUBAGENT_SYNTHESIS_NUDGE = (
    "The background agent(s) you dispatched have now finished. Review their "
    "results (read their transcripts or output as needed) and post your "
    "consolidated final report for this task now. Do not dispatch new "
    "background agents to answer this. If you already posted the final "
    "report, reply with a brief confirmation instead of repeating it."
)
_HANDOVER_ROLES = {"user", "assistant", "system"}
_HANDOVER_MAX_MESSAGES = 80
_HANDOVER_MAX_CHARS = 60_000
_LEGACY_MODEL_BUCKETS = {"work", "personal"}
_ANTHROPIC_MODEL_BUCKETS = {"work", "anthropic"}
_OLLAMA_MODEL_BUCKETS = {"personal", "ollama"}


def _classify_file(path: Path) -> str:
    """Map a file path to one of: ``markdown | image | text | binary``.

    Anything outside the three allowlists falls back to ``binary`` so the UI
    can show it greyed-out with a download fallback. The file may not be
    representable by any of our viewers, but we still list it.
    """
    ext = path.suffix.lower()
    if ext in {".md", ".markdown"}:
        return "markdown"
    if ext in _PROJECT_IMAGE_EXTS:
        return "image"
    if ext in _PROJECT_TEXT_EXTS:
        return "text"
    return "binary"

# Legacy IDs from the removed auto-imported Claude Code CLI view.
_CC_CLI_PROJECT_ID = "proj-cc-cli"
_CC_CHAT_PREFIX = "chat-cc-"

# A vault_folder must be a single directory name under projects/active/ or
# projects/completed/. Reject path separators, parent-directory traversal,
# leading dots, and non-printable characters. Names are free-form (lowercase
# kebab-case is preferred but not enforced); see README "Project naming
# convention".
_VAULT_FOLDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_after(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _provider_label(provider: str) -> str:
    labels = {
        "claude": "Claude",
        "codex": "Codex",
    }
    return labels.get(provider, provider or "Provider")


def _normalize_handover_messages(messages: list[dict] | None) -> list[dict]:
    """Sanitize visible chat rows for cross-provider handover.

    The browser sends its normalized visible messages. Keep only the fields
    needed to reconstruct UI history and provider context, then cap by count
    and total characters so a long chat cannot explode the next prompt.
    """
    rows: list[dict] = []
    total_chars = 0
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role", "")).strip().lower()
        if role not in _HANDOVER_ROLES:
            continue
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        entry: dict = {
            "role": role,
            "content": content,
        }
        timestamp = str(raw.get("timestamp", "") or raw.get("sent_at", "")).strip()
        if timestamp:
            entry["timestamp"] = timestamp
        tool_name = str(raw.get("tool_name", "")).strip()
        if tool_name:
            entry["tool_name"] = tool_name
        if bool(raw.get("is_error")):
            entry["is_error"] = True
        images = raw.get("images")
        if isinstance(images, list):
            refs = [str(ref) for ref in images if str(ref)]
            if refs:
                entry["images"] = refs
        file_path = str(raw.get("file_path", "")).strip()
        if file_path:
            entry["file_path"] = file_path
        action = str(raw.get("action", "")).strip()
        if action:
            entry["action"] = action
        tool = str(raw.get("tool", "")).strip()
        if tool:
            entry["tool"] = tool
        rows.append(entry)
        total_chars += len(content)
        while (
            len(rows) > _HANDOVER_MAX_MESSAGES
            or total_chars > _HANDOVER_MAX_CHARS
        ) and rows:
            removed = rows.pop(0)
            total_chars -= len(str(removed.get("content", "")))
    return rows


def _handover_marker(
    *,
    old_provider: str,
    old_model: str,
    new_provider: str,
    new_model: str,
) -> dict:
    return {
        "role": "system",
        "content": (
            "Handed over from "
            f"{_provider_label(old_provider)} / {old_model} to "
            f"{_provider_label(new_provider)} / {new_model}."
        ),
        "timestamp": _now_iso(),
    }


def _is_retryable_quota_error(text: str) -> bool:
    low = (text or "").lower()
    if "reached your session usage limit" in low:
        return True
    if "429" not in low and "too many requests" not in low:
        return False
    return any(needle in low for needle in ("usage limit", "rate limit", "quota", "session"))


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _uuid8() -> str:
    return uuid.uuid4().hex[:8]


_TITLE_SYSTEM_PROMPT = (
    "You generate very short titles for chat conversations. "
    "Reply with ONLY the title, 3 to 6 words, no quotes, no trailing punctuation, "
    "no emoji, in the same language as the user's first message. "
    "Capture the topic, not the meta (don't say 'chat about', 'help with', etc)."
)


def _fallback_title(user_text: str) -> str | None:
    """Deterministic fallback title derived from the user's first message.

    Used when the OpenRouter call fails or no API key is configured, so the
    sidebar never stays stuck on "New Chat" indefinitely.
    """
    snippet = (user_text or "").strip()
    if not snippet:
        return None
    # First line only, strip surrounding quotes.
    snippet = snippet.splitlines()[0].strip().strip('"').strip("'").strip()
    if not snippet:
        return None
    # Cap at ~6 words or 60 chars.
    words = snippet.split()
    if len(words) > 6:
        snippet = " ".join(words[:6])
    snippet = snippet.rstrip(".!?:,")
    if len(snippet) > 60:
        snippet = snippet[:57].rstrip() + "..."
    return snippet or None


def _clean_title(raw: str, user_snippet: str) -> str | None:
    """Strip quotes, take first line, cap length. Fallback on empty."""
    title = (raw or "").strip().strip('"').strip("'").strip()
    if not title:
        return _fallback_title(user_snippet)
    title = title.splitlines()[0].strip().rstrip(".!?:,")
    if len(title) > 60:
        title = title[:57].rstrip() + "..."
    return title or _fallback_title(user_snippet)


def resolve_title_model(config, workspace: str | None = None) -> str:
    """Pick the model for chat title generation.

    When the operator has not set an explicit override (Settings → Models →
    Chat titles = Automatic), use the haiku-tier model for the chat's
    workspace routing bucket. Callers without workspace context fall back to
    ``config.title_model``.
    """
    if config.title_model_override:
        return config.title_model_override
    if workspace is not None:
        return config.haiku_model_for_workspace(workspace)
    return config.title_model


async def _generate_chat_title(
    user_text: str,
    assistant_text: str = "",
    *,
    model: str = "haiku",
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 15.0,
    provider: str = "claude",
) -> str | None:
    """Summarize the first user message into a short chat title.

    Prefers the local Apple Intelligence CLI (`apfel`) when available,
    falling back to `run_oneshot` using the Claude SDK. The `env` dict
    is forwarded to the SDK query so Ollama env-injection keeps working.

    No cost tracking: both paths run the same upstream model,
    so there's no separate bill to log.

    Falls back to a deterministic truncation when both paths fail so the
    sidebar never gets stuck on "New Chat".
    """
    from ciao.providers.oneshot import run_oneshot

    user_snippet = (user_text or "").strip()[:1000]
    if not user_snippet:
        return None

    assistant_snippet = (assistant_text or "").strip()[:1000]
    if assistant_snippet:
        user_prompt = (
            "First user message:\n"
            f"{user_snippet}\n\n"
            "Assistant reply (for context):\n"
            f"{assistant_snippet}\n\n"
            "Title:"
        )
    else:
        user_prompt = f"User message:\n{user_snippet}\n\nTitle:"

    if shutil.which("apfel") is not None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "apfel",
                "-q",
                "-s",
                _TITLE_SYSTEM_PROMPT,
                user_prompt,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd is not None else None,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            if proc.returncode == 0:
                text = stdout.decode().strip()
                if text:
                    return _clean_title(text, user_snippet)
            else:
                logger.info(
                    "apfel title generation exited with %d: %s",
                    proc.returncode,
                    stderr.decode().strip(),
                )
        except Exception as exc:
            logger.info("apfel title spawn failed (%s); falling back", exc)

    # "apfel" is a routing sentinel meaning "use the on-device CLI above",
    # not a real Claude/Ollama model id. If the binary is missing or its
    # subprocess didn't produce a title, run_oneshot must never see it
    # literally — that always fails ("There's an issue with the selected
    # model (apfel)"). Substitute the standard fallback model instead.
    fallback_model = model if model != "apfel" else "haiku"
    try:
        text = await run_oneshot(
            user_prompt,
            system_prompt=_TITLE_SYSTEM_PROMPT,
            model=fallback_model,
            env=env,
            timeout_s=timeout_s,
            provider=provider,
            cwd=cwd,
        )
        if text:
            return _clean_title(text, user_snippet)
    except Exception as exc:
        logger.info(
            "Title generation via %s %s failed: %s",
            provider,
            fallback_model or "account default",
            exc,
        )
        return _fallback_title(user_snippet)

    return _fallback_title(user_snippet)


def _safe_validate(path: Path, root: Path, allowed_ext: set[str], max_bytes: int) -> None:
    """Validate a file path is safe to use."""
    resolved = path.resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        raise ValueError("File path escaped media root")
    if path.suffix.lower() not in allowed_ext:
        raise ValueError(f"Unsupported extension: {path.suffix}")
    if path.exists() and path.is_symlink():
        raise ValueError("Symlinks not allowed")
    if path.exists() and path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise ValueError("File too large")


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
    # Runtime-only: relative path to the canonical vault doc (e.g.
    # "memory-vault/personal/projects/active/ciao-improvements/README.md"). Not
    # persisted in JSON; recomputed on every vault discovery pass.
    vault_doc_path: str = ""

    @property
    def is_auto(self) -> bool:
        return self.name == "General" or self.name == "Claude Code CLI"

    @property
    def is_system(self) -> bool:
        return self.name == "Claude Code CLI"

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "workspace": self.workspace,
            "context": self.context,
            "created_at": self.created_at,
            "order": self.order,
            "vault_folder": self.vault_folder,
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
    # Routing key for ProviderService. Public builds currently accept
    # "claude"; backend choice is handled by model/model_bucket routing.
    provider: str = "claude"
    # Claude routing bucket chosen in the picker: "work" pins the Anthropic
    # subscription upstream (aliases stay aliases), "personal" pins Ollama
    # routing (aliases resolve to tier models). Empty = legacy auto: the
    # project's workspace decides. Only meaningful when provider == "claude".
    model_bucket: str = ""
    mode: BridgeMode = "auto"
    # Provider-native thinking/reasoning level (see ciao.models.THINKING_LEVELS).
    # Empty = provider default. Reset on handover: levels aren't portable
    # across providers.
    thinking_level: str = ""
    session_id: str = ""
    created_at: str = ""
    archived: bool = False
    last_activity_at: str = ""
    # Cross-device read tracking. Set by `mark_read` (via POST
    # /api/chats/{id}/read). A chat is considered unread when
    # `last_activity_at > last_read_at`.
    last_read_at: str = ""
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
    handover_context_pending: bool = False
    # Raw AskUserQuestion JSON (`{"questions": [...]}`) when the model paused
    # this chat on a question the user hasn't answered yet. Set when the
    # headless CLI fires AskUserQuestion (which we interrupt so it can't
    # auto-answer); cleared on the next user send. Persisted and surfaced in
    # `to_dict` so the PWA can rebuild its interactive picker after a reload
    # instead of showing the dead `{"questions": ...}` trace row.
    pending_question: str = ""

    def to_dict(self, *, local: bool | None = None) -> dict:
        d = {
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "title": self.title,
            "model": self.model,
            "provider": self.provider,
            "model_bucket": self.model_bucket,
            "mode": self.mode,
            "thinking_level": self.thinking_level,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "archived": self.archived,
            "last_activity_at": self.last_activity_at,
            "last_read_at": self.last_read_at,
            "title_status": self.title_status,
            "pending_question": self.pending_question,
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


@dataclass(slots=True)
class ScheduleRunOutcome:
    completed: bool = False
    is_error: bool = False
    permission_requested: bool = False
    question_requested: bool = False
    stream_error: bool = False
    retry_pending: bool = False
    final_text: str = ""
    archived_to: str = ""
    # True when the run dispatched background subagents that had not finished
    # by the time we stopped waiting. Such a run is not "done" yet, so it must
    # stay visible rather than auto-archive on a half-complete result.
    subagents_pending: bool = False


def _schedule_run_clean(outcome: ScheduleRunOutcome) -> bool:
    return (
        outcome.completed
        and not outcome.is_error
        and not outcome.permission_requested
        and not outcome.question_requested
        and not outcome.stream_error
        and not outcome.retry_pending
        and not outcome.subagents_pending
    )


def _should_auto_archive_schedule_run(
    entry: object, outcome: ScheduleRunOutcome, *, needs_user: bool = False
) -> bool:
    archive_policy = getattr(entry, "archive_policy", "manual")
    if archive_policy == "auto":
        return _schedule_run_clean(outcome) and not needs_user
    return False


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
        self._providers: dict[str, ProviderService] = {}
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
        self.notify_result_cb = None
        # `notify_permission(chat_id, tool_name, message, request_id)` fires
        # whenever the Auto-mode classifier asks the user to approve a tool.
        # The PWA turn is blocked until the answer lands, so unlike the
        # result push this fires immediately (no delay) and only skips when
        # the chat is focused in the foreground.
        self.notify_permission_cb = None
        # `notify_question(chat_id, question_text)` fires when the model uses
        # AskUserQuestion. The headless CLI auto-cancels with empty answers,
        # so we notify the user so they can answer in the next turn.
        self.notify_question_cb = None
        # Per-chat pending push tasks. Pushes are scheduled with a short
        # delay (CIAO_PUSH_DELAY_SECONDS, default 30s) so that reading the
        # chat on any device within the window suppresses the buzz. New
        # replies to the same chat cancel the previous timer and start a
        # new one (coalesce rapid replies into a single push).
        self._pending_push: dict[str, asyncio.Task] = {}
        # Per-chat background subagent completion watchers. Each active turn
        # may spawn subagents; we keep at most one watcher per chat so rapid
        # successive turns do not accumulate overlapping pollers.
        self._pending_subagent_watchers: dict[str, asyncio.Task] = {}
        # Per-chat between-turns SDK drain tasks (see _drain_between_turns).
        # At most one per chat; cancelled before a new user turn starts so
        # the drain never competes with receive_response for SDK messages.
        self._between_turn_drains: dict[str, asyncio.Task] = {}
        # Last announced running-background-subagent count per chat. Feeds
        # the /ws/events connect snapshot so a fresh client can paint the
        # "N agents running" indicator without waiting for the next change.
        self._background_agents_last: dict[str, int] = {}
        # Latest result (text, is_error) captured by the between-turns drain
        # for a chat, i.e. the CLI's post-subagent synthesis turn. The
        # schedule pipeline reads this after background subagents settle so
        # the auto-archive classifier judges the real summary instead of the
        # interim "dispatched, will report" parent message.
        self._last_drain_result: dict[str, tuple[str, bool]] = {}
        # Per-chat deferred quota retry loops. Each loop sleeps until the
        # chat's retry_next_at, tries the saved prompt if idle, then repeats
        # hourly until success/stop/archive/delete.
        self._retry_tasks: dict[str, asyncio.Task] = {}
        # In-memory perf-clock per active turn, keyed by (chat_id, turn_index).
        # Used to compute agent latency (duration_ms) when the ResultEvent
        # arrives. Cleared as soon as the turn finishes — wall-clock ISO
        # timestamps are the persisted record on `user_turn_timings`.
        self._turn_perf_started: dict[tuple[str, int], float] = {}
        try:
            self._push_delay_seconds = max(
                0, int(os.environ.get("CIAO_PUSH_DELAY_SECONDS", "30"))
            )
        except ValueError:
            self._push_delay_seconds = 30
        self._load()
        self._migrate_remove_claude_code_cli_project()
        self._migrate_drop_qn_prefix()
        self._ensure_defaults()
        self._discover_vault_projects()
        # Sweep any empty chats left over from a previous run (user closed the
        # tab before typing, server crashed mid-compose, etc.). An "empty"
        # chat has no messages, no SDK session, and still the default title —
        # it's indistinguishable from no chat at all, so don't leave it in
        # the sidebar.
        self._cleanup_empty_chats()
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
                model_bucket=cd.get("model_bucket", ""),
                mode=cd.get("mode", self._config.claude_mode),
                thinking_level=cd.get("thinking_level", ""),
                session_id=cd.get("session_id", ""),
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
                user_turn_count=cd.get("user_turn_count", 0),
                user_turn_images=dict(cd.get("user_turn_images", {})),
                user_turn_timings=dict(cd.get("user_turn_timings", {})),
                archive_path=cd.get("archive_path", ""),
                retry_status=cd.get("retry_status", "") if cd.get("retry_status", "") in _RETRY_STATUSES else "",
                retry_prompt=cd.get("retry_prompt", ""),
                retry_image_refs=list(cd.get("retry_image_refs", [])),
                retry_next_at=cd.get("retry_next_at", ""),
                retry_last_error=cd.get("retry_last_error", ""),
                retry_attempts=int(cd.get("retry_attempts", 0) or 0),
                retry_interval_seconds=int(cd.get("retry_interval_seconds", _RETRY_INTERVAL_SECONDS) or _RETRY_INTERVAL_SECONDS),
                handover_messages=_normalize_handover_messages(
                    list(cd.get("handover_messages", []))
                ),
                handover_context_pending=bool(cd.get("handover_context_pending", False)),
                pending_question=cd.get("pending_question", ""),
            )
        logger.info(
            "Restored %d project(s) and %d chat(s)",
            len(self._projects),
            len(self._chats),
        )

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

    def _save(self) -> None:
        if not self._path:
            return
        payload = {
            "version": 1,
            "projects": {
                pid: {
                    "name": p.name,
                    "workspace": p.workspace,
                    "context": p.context,
                    "created_at": p.created_at,
                    "order": p.order,
                    "vault_folder": p.vault_folder,
                }
                for pid, p in self._projects.items()
            },
            "chats": {
                cid: {
                    "project_id": c.project_id,
                    "title": c.title,
                    "model": c.model,
                    "provider": c.provider,
                    "model_bucket": c.model_bucket,
                    "mode": c.mode,
                    "thinking_level": c.thinking_level,
                    "session_id": c.session_id,
                    "created_at": c.created_at,
                    "archived": c.archived,
                    "last_activity_at": c.last_activity_at,
                    "last_read_at": c.last_read_at,
                    "user_turn_count": c.user_turn_count,
                    "user_turn_images": c.user_turn_images,
                    "user_turn_timings": c.user_turn_timings,
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
                    "pending_question": c.pending_question,
                }
                for cid, c in self._chats.items()
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ── Defaults and auto-discovery ──────────────────────────────────────

    def _workspace_names(self) -> tuple[str, ...]:
        """Return configured logical chat workspaces in config order."""
        names = tuple(self._config.workspace_names())
        return names or ("personal", "work")

    def _is_known_workspace(self, workspace: str) -> bool:
        return workspace in self._workspace_names()

    def _workspace_vault_root(self, workspace: str) -> Path:
        """Return the vault root for one logical workspace.

        Legacy ``personal``/``work`` workspace configs store ``vault_root`` as
        the workspace name and are rooted under ``CIAO_VAULT_ROOT``. Custom
        workspace roots are workspace-relative unless absolute.
        """
        workspace_config = self._config.workspace(workspace)
        raw_root = workspace_config.vault_root if workspace_config else workspace
        root = Path(raw_root).expanduser()
        if root.is_absolute():
            return root.resolve()
        if workspace in {"personal", "work"} and raw_root == workspace:
            return (self._config.vault_root / workspace).resolve()
        return (self._config.workspace_root / root).resolve()

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
                pid = f"proj-{_uuid8()}"
                general = ProjectInfo(
                    project_id=pid,
                    name="General",
                    workspace=ws,
                    created_at=_now_iso(),
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
        vault_root = str(self._config.vault_root)

        if vault_mode == "existing":
            title = "Connect Existing Vault 👋"
            user_msg = (
                f"Welcome to Ciaobot. You are Ciaobot, the user's personal agentic assistant.\n\n"
                f"The user has completed setup and pointed me to an **existing notes folder** at:\n"
                f"`{vault_root}`\n\n"
                f"Your task is to onboard the user and adapt this existing folder into what Ciaobot requires:\n"
                f"1. **Analyze Folder**: Scan the existing vault directory to see what directories and files are present.\n"
                f"2. **Structure Verification**: Check if the standard directories (`personal/`, `work/`, `Templates/`) exist. If not, plan to create them.\n"
                f"3. **Hygiene & Scaffolding**: Verify if `CLAUDE.md` (defining identity, memory, styles) and `MEMORY.md` exist. If missing, plan to create them using clean Markdown structures (no em-dashes, no horizontal rules `---` as section dividers).\n"
                f"4. **Workspace Git Check**: Verify the workspace is a git repository with a `.gitignore` covering `.env` and `.runtime/`, and that the vault is inside a git repo (the workspace repo by default, or its own when it lives elsewhere). If not, fix it (`git init`, append the missing entries) and report what you did.\n"
                f"5. **Onboarding Interview**: Ask the user 2-3 important questions to collect basic info (their name, their role/work context, key people, and active projects) to populate `CLAUDE.md` and `MEMORY.md` correctly.\n"
                f"6. **Capabilities Tour**: Once the interview is done, point the user to the in-app product tour (Settings → Home → Replay tour if they skipped it) and offer a short guided tour of what Ciaobot can do (use the `ciao-capabilities` skill). Mention they can ask \"what can Ciaobot do?\" in any chat, anytime.\n\n"
                f"Introduce yourself to the user, tell them you've scanned their vault at `{vault_root}`, outline your findings, and ask the first onboarding questions to fill out their profile."
            )
            assistant_msg = (
                f"Hello! I am Ciaobot, your agentic second brain. 👋\n\n"
                f"I've initialized our session and connected to your existing folder at `{vault_root}`. "
                f"I'm ready to inspect your vault, organize it into Ciaobot's structure, and bootstrap our core notes. "
                f"You can also ask me **\"what can Ciaobot do?\"** anytime for a tour of the app. "
                f"A **product tour** overlay should appear on first launch — use **Settings → Home → Replay tour** if you skipped it. "
                f"To get started, tell me: **What is your name, and what is your primary focus (work/personal) right now?**"
            )
        else:
            title = "Welcome to Ciaobot! 👋"
            user_msg = (
                f"Welcome to Ciaobot. You are Ciaobot, the user's personal agentic assistant.\n\n"
                f"The user has completed setup and initialized a **new vault folder from scratch** at:\n"
                f"`{vault_root}`\n\n"
                f"Your task is to bootstrap the vault structure and core documentation:\n"
                f"1. **Create Directory Structure**: Plan to create: `personal/`, `work/`, and `Templates/` (scaffold markdown templates for logs, projects, and people).\n"
                f"2. **Generate Core Files**: Plan to generate clean initial templates for `CLAUDE.md` (defining instructions, memory rules, styles) and `MEMORY.md`.\n"
                f"3. **Workspace Git Check**: Verify the workspace is a git repository with a `.gitignore` covering `.env` and `.runtime/`, and that the vault is inside a git repo (the workspace repo by default, or its own when it lives elsewhere). If not, fix it (`git init`, append the missing entries) and report what you did.\n"
                f"4. **Onboarding Interview**: Ask the user 2-3 important questions to collect basic info (their name, GWS profiles, key projects) to customize `CLAUDE.md` and `MEMORY.md`.\n"
                f"5. **Capabilities Tour**: Once the interview is done, point the user to the in-app product tour (Settings → Home → Replay tour if they skipped it) and offer a short guided tour of what Ciaobot can do (use the `ciao-capabilities` skill). Mention they can ask \"what can Ciaobot do?\" in any chat, anytime.\n\n"
                f"Introduce yourself to the user, explain that you are starting fresh at `{vault_root}`, and ask the first onboarding questions to bootstrap your profile."
            )
            assistant_msg = (
                f"Hello! I am Ciaobot, your agentic second brain. 👋\n\n"
                f"Welcome! I've initialized our workspace at `{vault_root}` from scratch. "
                f"I'm ready to create our core structure (`personal/`, `work/`, `Templates/`) and customize our settings. "
                f"You can also ask me **\"what can Ciaobot do?\"** anytime for a tour of the app. "
                f"A **product tour** overlay should appear on first launch — use **Settings → Home → Replay tour** if you skipped it. "
                f"To begin, tell me: **What is your name, and what is your primary focus (work/personal) right now?**"
            )

        chat = self.create_chat(
            project_id,
            title=title,
            model=self._config.claude_default_model,
        )
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
                fallback = entry / f"{entry.name}.md"
                readme = fallback if fallback.exists() else None
            else:
                pass
            out.append((entry.name, entry, readme))
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

        # Determine workspace
        workspace = "personal"
        if "[CONTEXT: work" in text or "work -- Workspace" in text:
            workspace = "work"

        # Determine project name
        project_name = "General"
        project_match = re.search(r'\[Project:\s*["\']?(.*?)["\']?\]', text)
        if project_match:
            project_name = project_match.group(1).strip()

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

    def _discover_archived_chats(self) -> None:
        """Scan the vault's archived transcripts and import missing chats."""
        chats_root = self._config.vault_root / "Logs" / "Chats"
        if not chats_root.is_dir():
            return

        # Map of (name, workspace) -> project_id for active projects
        project_map = {}
        for p in self._projects.values():
            project_map[(p.name.lower(), p.workspace)] = p.project_id

        # General project IDs
        general_ids = {}
        for ws in self._workspace_names():
            gen = next(
                (p for p in self._projects.values() if p.workspace == ws and p.name == "General"),
                None
            )
            if gen:
                general_ids[ws] = gen.project_id

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
            transcripts = []
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
            proj_id = project_map.get((proj_name.lower(), ws)) or general_ids.get(ws, "")

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
            self._save()

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
        unbound_by_name: dict[str, dict[str, ProjectInfo]] = {
            ws: {} for ws in workspace_names
        }
        for p in self._projects.values():
            if p.workspace in unbound_by_name and not p.vault_folder:
                unbound_by_name[p.workspace][p.name] = p

        # ── Discover ─────────────────────────────────────────────────────
        for ws in workspace_names:
            for stem, entry_path, readme in per_workspace_entries[ws]:
                if stem in existing_stems_by_ws[ws]:
                    # Already in our index — refresh the vault doc path so the
                    # Files section and canonical-doc link stay accurate even
                    # if the readme moved.
                    existing = next(
                        (p for p in self._projects.values()
                         if p.vault_folder == stem and p.workspace == ws),
                        None,
                    )
                    if existing is not None and readme is not None:
                        existing.vault_doc_path = self._display_path(readme)
                    continue
                name, context = self._read_project_metadata(readme, stem)

                existing = unbound_by_name[ws].get(name) or unbound_by_name[ws].get(stem)
                if existing:
                    existing.vault_folder = stem
                    if readme is not None:
                        existing.vault_doc_path = self._display_path(readme)
                    if not existing.context and context:
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

                pid = f"proj-{_uuid8()}"
                project = ProjectInfo(
                    project_id=pid,
                    name=name,
                    workspace=ws,
                    context=context,
                    created_at=_now_iso(),
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
        pid = f"proj-{_uuid8()}"
        project = ProjectInfo(
            project_id=pid,
            name=name,
            workspace=workspace,
            context=context,
            created_at=_now_iso(),
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
        if context is not None:
            project.context = context
        if vault_folder is not None:
            # Reject anything that could escape projects/active/<folder>/.
            # Empty string clears the binding; a non-empty value must be a
            # single safe folder name (no separators, no traversal, no NUL).
            if vault_folder and not _VAULT_FOLDER_RE.fullmatch(vault_folder):
                raise ValueError(
                    f"Invalid vault_folder {vault_folder!r}: "
                    "must match [A-Za-z0-9._-]+ with no path separators."
                )
            project.vault_folder = vault_folder
        self._save()
        self._events.publish({
            "type": "project_updated",
            "project": project.to_dict(),
        })
        return project

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
            if not _VAULT_FOLDER_RE.fullmatch(vault_folder):
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
        if not _VAULT_FOLDER_RE.fullmatch(stem):
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
        if project.is_auto:
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
        project = self._projects.pop(project_id, None)
        if project is None:
            return False
        # Archive and remove all chats in this project
        for cid in list(self._chats):
            if self._chats[cid].project_id == project_id:
                self._archive_and_remove_chat(cid)
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

        # Codex threads are owned by Codex and can be resumed by id through
        # app-server. There is no public local thread-file contract to probe.
        if chat.provider == "codex":
            return True

        # Default / "claude" provider
        projects_dir = _claude_projects_dir(self._config.workspace_root)
        if (projects_dir / f"{chat.session_id}.jsonl").exists():
            return True

        # Fallback search across all projects folders to handle workspace folder changes/mismatch
        try:
            claude_projects_root = Path.home() / ".claude" / "projects"
            if claude_projects_root.exists():
                if any(claude_projects_root.glob(f"*/{chat.session_id}.jsonl")):
                    return True
        except Exception:
            pass

        return False

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
        model_bucket: str | None = None,
    ) -> ChatInfo:
        if project_id not in self._projects:
            raise ValueError(f"Project '{project_id}' not found")
        if provider is not None and provider not in supported_providers():
            raise ValueError(f"Unknown provider '{provider}'")
        if not self._model_bucket_allowed(model_bucket):
            raise ValueError(f"Unknown model bucket '{model_bucket}'")
        # Sweep any other empty chats before creating a new one. Opening a
        # fresh "New Chat" signals the user has moved on from whatever they
        # had open and never sent, so we don't let empty shells pile up.
        self._cleanup_empty_chats()
        cid = f"chat-{_uuid8()}"
        # Per-workspace default: personal projects can default to Ollama
        # models, work to Anthropic, etc. Explicit ``model`` arg wins.
        project = self._projects.get(project_id)
        workspace = project.workspace if project else None
        default_model = self._config.default_model_for_workspace(workspace)
        chat_model = model or default_model
        chat_provider = provider
        if not chat_provider:
            chat_provider = self._config.default_provider_for_workspace(workspace)
        # Claude chats record the bucket explicitly: the workspace only
        # preselects (personal → "personal"), but an explicit picker choice
        # wins and survives project moves. Personal-bucket alias defaults
        # are resolved to their Ollama tier model at creation so the picker
        # shows the model that will actually run.
        chat_bucket = ""
        if chat_provider == "claude":
            chat_bucket = self._effective_bucket(model_bucket or "", project_id)
            if (self._bucket_routes_to_ollama(chat_bucket) or chat_bucket == "openrouter") and model is None:
                chat_model = self._resolve_claude_model(
                    chat_model, chat_bucket, project_id
                )
        chat = ChatInfo(
            chat_id=cid,
            project_id=project_id,
            title=title,
            model=chat_model,
            provider=chat_provider,
            model_bucket=chat_bucket,
            mode=mode or self._config.claude_mode,
            created_at=_now_iso(),
        )
        if chat_provider == "codex" and canonical_tier(chat_model) == "fable":
            chat.thinking_level = CODEX_FABLE_THINKING_LEVEL
        self._chats[cid] = chat
        self._save()
        return chat

    def _is_empty_chat(self, chat: ChatInfo) -> bool:
        """An empty chat is one the user abandoned before sending anything.

        Criteria: default title, no user turns recorded, no SDK session
        attached, not archived, and not a retired imported CLI record. Active
        broker stream is also a bail-out signal: it means a turn is in flight,
        so user_turn_count may just not have been bumped yet.
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
            provider = self._providers.pop(cid, None)
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

    def rename_chat(self, chat_id: str, title: str) -> ChatInfo | None:
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        chat.title = title
        self._save()
        return chat

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
        model_bucket: str | None = None,
    ) -> ChatInfo | None:
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        was_codex_fable = (
            chat.provider == "codex" and canonical_tier(chat.model) == "fable"
        )
        if provider is not None and provider not in supported_providers():
            raise ValueError(f"Unknown provider '{provider}'")
        if not self._model_bucket_allowed(model_bucket):
            raise ValueError(f"Unknown model bucket '{model_bucket}'")
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
        moved_from: str | None = None
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
            moved_from = chat.project_id
            chat.project_id = project_id
        if title is not None:
            chat.title = title
        if model is not None or provider is not None or model_bucket is not None:
            new_model = model if model is not None else chat.model
            new_provider = provider if provider is not None else chat.provider
            new_bucket = model_bucket if model_bucket is not None else chat.model_bucket
            # Cross-provider switches mid-chat would silently break: the
            # spawned CLI subprocess has provider/routing state baked in at
            # spawn time, so swapping providers, buckets, or moving between
            # Anthropic and Ollama endpoints within ClaudeProvider would
            # point the next API call at the wrong upstream. Reject when the
            # chat already has history.
            changed = (
                new_model != chat.model
                or new_provider != chat.provider
                or new_bucket != chat.model_bucket
            )
            if changed and (
                chat.user_turn_count > 0 or chat.session_id
            ) and self._is_cross_provider_switch(
                chat.provider, chat.model, new_provider, new_model,
                project_id=chat.project_id,
                old_bucket=chat.model_bucket,
                new_bucket=new_bucket,
            ):
                raise ValueError(
                    "Can't switch providers or model backends once a chat has "
                    "started. Same-backend model swaps are fine; use handover "
                    "to continue this chat with another provider, or close this "
                    "chat and start a new one."
                )
            chat.model = new_model
            chat.provider = new_provider
            chat.model_bucket = new_bucket if new_provider == "claude" else ""
        if mode is not None:
            chat.mode = mode  # type: ignore[assignment]
        is_codex_fable = (
            chat.provider == "codex" and canonical_tier(chat.model) == "fable"
        )
        if is_codex_fable:
            chat.thinking_level = CODEX_FABLE_THINKING_LEVEL
        elif was_codex_fable and thinking_level is None:
            # Leaving the Fable preset restores the target model's default
            # effort unless the caller explicitly chose another level.
            chat.thinking_level = ""
        elif thinking_level is not None:
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
                    turns_data.append({
                        "role": "assistant",
                        "content": assistant_content,
                        "timestamp": timestamp,
                    })
                    
        return turns_data

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
            model_bucket=chat.model_bucket,
        )
        new_chat.thinking_level = chat.thinking_level
        
        # Seed handover messages
        new_chat.handover_messages = _normalize_handover_messages(parsed_messages)
        new_chat.handover_context_pending = True
        
        self._save()
        return new_chat

    def handover_chat(
        self,
        chat_id: str,
        *,
        provider: str,
        model: str,
        messages: list[dict] | None = None,
        model_bucket: str = "",
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

        old_provider = chat.provider
        old_model = chat.model
        rows = _normalize_handover_messages(messages)
        rows.append(
            _handover_marker(
                old_provider=old_provider,
                old_model=old_model,
                new_provider=provider,
                new_model=clean_model,
            )
        )
        if not self._model_bucket_allowed(model_bucket):
            raise ValueError(f"Unknown model bucket '{model_bucket}'")
        chat.handover_messages = rows
        chat.handover_context_pending = True
        chat.provider = provider
        chat.model = clean_model
        # Bucket only applies to Claude; explicit choice wins, otherwise
        # the workspace preselects on the next resolution ("" = auto).
        chat.model_bucket = model_bucket if provider == "claude" else ""
        # Thinking levels are provider-native and don't carry across, except
        # that the Codex Fable preset is defined as Sol with Ultra effort.
        chat.thinking_level = (
            CODEX_FABLE_THINKING_LEVEL
            if provider == "codex" and canonical_tier(clean_model) == "fable"
            else ""
        )
        chat.session_id = ""
        chat.last_activity_at = _now_iso()

        ctx = ChatContext.for_web(chat_id)
        self._state.reset_active_session(ctx)
        self._cancel_between_turns_drain(chat_id)
        provider_service = self._providers.pop(chat_id, None)
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

    def delete_chat(self, chat_id: str) -> bool:
        chat = self._chats.pop(chat_id, None)
        if chat is None:
            return False
        task = self._retry_tasks.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._cancel_between_turns_drain(chat_id)
        self._last_drain_result.pop(chat_id, None)
        provider = self._providers.pop(chat_id, None)
        if provider:
            asyncio.ensure_future(provider.disconnect())
        if chat.session_id and chat.provider == "claude":
            self._transcripts.delete_sdk_session_blob(
                self._config.workspace_root, chat.session_id
            )
        self._unlink_chat_images(chat)
        # Drop file snapshots so we don't accumulate dead history forever.
        # Archive intentionally keeps them: archived chats are read-only but
        # their history viewer should still work.
        self._snapshots.delete_chat(chat_id)
        self._save()
        self._events.publish({
            "type": "chat_deleted",
            "chat_id": chat_id,
            "project_id": chat.project_id,
            "reason": "user",
        })
        return True

    # ── Session management ───────────────────────────────────────────────

    def archive_chat(self, chat_id: str) -> ArchiveOutcome | None:
        """Archive a chat's transcript and mark it as archived.

        Also disconnects any live SDK provider and deletes the Claude Code
        session JSONL blob to reclaim disk space. The markdown transcript in
        the vault is the durable record.

        Returns an ArchiveOutcome carrying the archive path plus a
        pre-filtered JSONL string captured before blob deletion, so the
        route handler can dispatch post-archive insights extraction
        without racing against the disk reclaim.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        ctx = ChatContext.for_web(chat_id)
        # Capture turn count before archive_session consumes the in-memory
        # transcript; capture the filtered JSONL before blob deletion.
        turn_count = self._transcripts.peek_turn_count(ctx, chat.provider)
        filtered_jsonl: str | None = None
        if chat.session_id and chat.provider == "claude":
            from ciao.insights import filter_session_jsonl
            try:
                filtered_jsonl = filter_session_jsonl(
                    self._config.workspace_root, chat.session_id
                )
            except Exception:  # noqa: BLE001 — never fail archive over insights prep
                logger.exception(
                    "Failed to pre-filter JSONL for chat %s", chat_id
                )
                filtered_jsonl = None
        elif chat.provider == "codex":
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
        if chat.retry_status:
            self._clear_chat_retry(chat)
        self._cancel_pending_push(chat_id)
        self._cancel_between_turns_drain(chat_id)
        provider = self._providers.pop(chat_id, None)
        if provider:
            asyncio.ensure_future(provider.disconnect())
        if chat.session_id and chat.provider == "claude":
            self._transcripts.delete_sdk_session_blob(
                self._config.workspace_root, chat.session_id
            )
        self._unlink_chat_images(chat)
        chat.archived = True
        if result is not None:
            try:
                chat.archive_path = str(result.relative_to(self._config.workspace_root))
            except ValueError:
                chat.archive_path = str(result)
        self._save()
        if result is None:
            return None
        return ArchiveOutcome(
            path=result,
            session_id=chat.session_id,
            turn_count=turn_count,
            filtered_jsonl=filtered_jsonl,
        )

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
    ) -> None:
        config = self._config
        trajectory_meta = {
            "context": project_meta.context if project_meta else "",
            "project_id": chat_meta.project_id if chat_meta else "",
            "chat_id": chat_id,
            "task_summary": chat_meta.title if chat_meta else "",
            "workspace": project_meta.workspace if project_meta else "",
        }
        trajectories_enabled = (
            getattr(config, "trajectories_enabled", True)
            and outcome.filtered_jsonl is not None
            and outcome.session_id != ""
        )
        run_insights = (
            getattr(config, "insights_enabled", False)
            and outcome.filtered_jsonl
            and outcome.turn_count >= getattr(config, "insights_size_gate_turns", 0)
        )
        if run_insights:
            from ciao.insights import extract_and_append, resolve_insights_model

            workspace = project_meta.workspace if project_meta else None
            insights_model = (
                chat_meta.model
                if chat_meta is not None and chat_meta.provider == "codex"
                else resolve_insights_model(config, workspace)
            )
            asyncio.create_task(
                extract_and_append(
                    archive_path=outcome.path,
                    filtered_jsonl=outcome.filtered_jsonl,
                    config=config,
                    model=insights_model,
                    session_id=outcome.session_id,
                    trajectory_meta=trajectory_meta,
                    trajectories_enabled=trajectories_enabled,
                    workspace_root=config.workspace_root,
                    vault_root=config.vault_root,
                    provider=chat_meta.provider if chat_meta else "claude",
                )
            )
        elif trajectories_enabled:
            from ciao.trajectory_builder import build_and_persist_trajectory

            try:
                build_and_persist_trajectory(
                    session_id=outcome.session_id,
                    filtered_jsonl=outcome.filtered_jsonl or "",
                    archive_path=outcome.path,
                    workspace_root=config.workspace_root,
                    **trajectory_meta,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Inline trajectory write failed for chat %s", chat_id
                )

        # Index the newly archived file in the FTS5 database
        try:
            import sqlite3
            from ciao.fts_search import get_db_path, init_db, index_file

            db_path = get_db_path()
            conn = sqlite3.connect(db_path)
            init_db(conn)
            index_file(conn, config.vault_root, outcome.path)
            conn.close()
        except Exception:  # noqa: BLE001
            logger.exception(
                "FTS search: failed to index archived file %s for chat %s",
                outcome.path,
                chat_id,
            )

    def new_session(self, chat_id: str) -> ChatInfo | None:
        """Archive current transcript and start a fresh session."""
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        # Archive existing transcript
        ctx = ChatContext.for_web(chat_id)
        self._transcripts.archive_session(
            ctx=ctx,
            active_model=chat.model,
            last_effective_model=chat.model,
            session_id=chat.session_id,
            provider=chat.provider,
        )
        # Delete the SDK session blob for the now-archived session.
        if chat.session_id and chat.provider == "claude":
            self._transcripts.delete_sdk_session_blob(
                self._config.workspace_root, chat.session_id
            )
        # Drop attached images: they belong to the archived transcript.
        self._unlink_chat_images(chat)
        # Reset session
        chat.session_id = ""
        chat.archived = False
        chat.archive_path = ""
        chat.handover_messages = []
        chat.handover_context_pending = False
        if chat.retry_status:
            self._clear_chat_retry(chat)
        self._state.reset_active_session(ctx)
        # Disconnect old provider so a fresh one is created
        self._cancel_between_turns_drain(chat_id)
        provider = self._providers.pop(chat_id, None)
        if provider:
            asyncio.ensure_future(provider.disconnect())
        self._save()
        return chat

    # ── Provider management ──────────────────────────────────────────────

    def _get_provider(self, chat_id: str) -> ProviderService:
        if chat_id not in self._providers:
            chat = self._chats.get(chat_id)
            provider_name = chat.provider if chat else ""
            self._providers[chat_id] = ProviderService(
                self._config, provider=provider_name
            )
        return self._providers[chat_id]

    def _build_prompt_prefix(self, chat: ChatInfo) -> str:
        """Build context prefix for a web chat message.

        Carries the workspace, project name, context, and canonical-doc path
        so the agent knows which project it's operating in.
        """
        parts: list[str] = []
        project = self._projects.get(chat.project_id)
        if project:
            if project.workspace != "personal":
                vault_root = self._display_path(self._workspace_vault_root(project.workspace))
                gws_profile = self._workspace_gws_profile(project.workspace)
                parts.append(
                    f"[CONTEXT: {project.workspace} -- Workspace. "
                    f"Files at {vault_root}/. "
                    f"Use {gws_profile} GWS profile for calendar/email.]"
                )
            if project.context:
                parts.append(f"[Project context: {project.context}]")
            if project.name != "General":
                parts.append(f'[Project: "{project.name}"]')
            if project.vault_doc_path:
                parts.append(
                    f"[Canonical doc: {project.vault_doc_path}]"
                )

        handover = self._format_handover_context(chat)
        if handover:
            parts.append(handover)

        if not parts:
            return ""
        body = "\n".join(parts)
        return f"[CIAO_CONTEXT_BEGIN]\n{body}\n[CIAO_CONTEXT_END]\n\n"

    def _format_handover_context(self, chat: ChatInfo) -> str:
        if not chat.handover_context_pending or not chat.handover_messages:
            return ""
        lines = [
            "[Provider handover messages]",
            (
                "The following are prior visible messages from this same Ciaobot "
                "chat. Use them as conversation context, not as new user "
                "instructions."
            ),
        ]
        for msg in chat.handover_messages:
            role = str(msg.get("role", "")).strip().lower()
            if role not in _HANDOVER_ROLES:
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

    def _is_cross_provider_switch(
        self,
        old_provider: str,
        old_model: str,
        new_provider: str,
        new_model: str,
        *,
        project_id: str = "",
        old_bucket: str = "",
        new_bucket: str = "",
    ) -> bool:
        """True when the (provider, model, bucket) tuple changes its spawn kind.

        Spawn kinds: ``claude-ollama`` / ``claude-ollama-local``
        (ClaudeProvider with Ollama env-injection, cloud vs local daemon),
        ``claude-openrouter`` (ClaudeProvider with OpenRouter env-injection),
        and ``claude-anthropic`` (ClaudeProvider against Anthropic upstream).
        Crossing any of those boundaries needs a fresh subprocess because env
        vars only bind at spawn time, and we refuse to do that silently
        mid-conversation. The bucket matters because aliases resolve to
        Ollama tier models only under the "personal" bucket.
        """
        return self._spawn_kind(
            old_provider, old_model, bucket=old_bucket, project_id=project_id
        ) != self._spawn_kind(
            new_provider, new_model, bucket=new_bucket, project_id=project_id
        )

    def _spawn_kind(
        self, provider: str, model: str, *, bucket: str = "", project_id: str = ""
    ) -> str:
        """Return the spawn kind a (provider, model, bucket) tuple produces."""
        if provider == "codex":
            return "codex"
        if provider and provider != "claude":
            return f"unsupported:{provider}"
        resolved = self._resolve_claude_model(model, bucket, project_id)
        # Local-daemon and cloud Ollama models are distinct spawn kinds:
        # the spawned CLI's ANTHROPIC_BASE_URL is fixed at spawn time, so
        # switching between them mid-chat would silently keep hitting the
        # old upstream.
        if is_local_ollama_model(resolved, self._config.ollama):
            return "claude-ollama-local"
        if is_ollama_model(resolved, self._config.ollama):
            return "claude-ollama"
        if intended_backend(resolved) == "openrouter":
            return "claude-openrouter"
        return "claude-anthropic"

    def disallowed_tools_for_chat(self, chat: ChatInfo) -> list[str]:
        """Per-workspace tool denylist for a chat's spawned CLI.

        Personal chats deny all claude.ai connector MCPs by default
        (work-only tools), work chats deny nothing by default. Both are
        overridable via ``CIAO_DISALLOWED_TOOLS_PERSONAL`` /
        ``CIAO_DISALLOWED_TOOLS_WORK``.
        """
        if chat.provider != "claude":
            return []
        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else None
        return self._config.disallowed_tools_for_workspace(workspace)

    def schedule_default_model(self, project_id: str | None) -> str:
        """Pick the default model for a new schedule.

        Mirrors ``create_chat``'s per-workspace default lookup so a
        schedule attached to a personal project can default to an
        Ollama model, while a work schedule defaults to Anthropic.
        Falls back to the global ``claude_default_model`` when the
        project is unknown.
        """
        project = self._projects.get(project_id) if project_id else None
        workspace = project.workspace if project else None
        return self._config.default_model_for_workspace(workspace)

    def schedule_default_provider(self, project_id: str | None) -> str:
        project = self._projects.get(project_id) if project_id else None
        workspace = project.workspace if project else None
        return self._config.default_provider_for_workspace(workspace)

    def refresh_workspaces(self) -> None:
        self._ensure_defaults()
        self._discover_vault_projects()

    def _workspace_gws_profile(self, workspace: str | None) -> str:
        workspace_config = self._config.workspace(workspace)
        if workspace_config and workspace_config.gws_profile:
            return workspace_config.gws_profile
        return self._config.gws_default_profile

    def _workspace_model_bucket(self, workspace: str | None) -> str:
        workspace_config = self._config.workspace(workspace)
        if workspace_config and workspace_config.model_bucket:
            return workspace_config.model_bucket
        if workspace_config:
            if workspace_config.default_provider == "openrouter":
                return "openrouter"
            if workspace_config.default_provider == "ollama":
                return "ollama"
            if workspace_config.default_provider == "claude":
                return "work"
        if workspace == "work":
            return "work"
        return "personal"

    def _configured_model_buckets(self) -> set[str]:
        buckets = set(_LEGACY_MODEL_BUCKETS | _ANTHROPIC_MODEL_BUCKETS | _OLLAMA_MODEL_BUCKETS)
        if self._config.openrouter.available:
            buckets.add("openrouter")
        for workspace in self._config.workspaces.values():
            if workspace.model_bucket:
                buckets.add(workspace.model_bucket)
            if workspace.default_provider == "openrouter":
                buckets.add("openrouter")
            if workspace.default_provider == "ollama":
                buckets.add("ollama")
            if workspace.default_provider == "claude":
                buckets.add("work")
        return buckets

    def _model_bucket_allowed(self, bucket: str | None) -> bool:
        if bucket is None or bucket == "":
            return True
        return bucket in self._configured_model_buckets()

    def _bucket_routes_to_ollama(self, bucket: str) -> bool:
        return bucket in _OLLAMA_MODEL_BUCKETS

    def _effective_bucket(self, bucket: str, project_id: str) -> str:
        """Resolve a chat's Claude bucket: explicit choice wins, else the
        project's workspace preselects a configured routing bucket."""
        if bucket and self._model_bucket_allowed(bucket):
            return bucket
        project = self._projects.get(project_id)
        return self._workspace_model_bucket(project.workspace if project else None)

    def _resolve_claude_model(self, model: str, bucket: str, project_id: str) -> str:
        """Resolve picker aliases to Ollama or OpenRouter tier models."""
        from ciao.model_tiers import tier_model

        effective = self._effective_bucket(bucket, project_id)
        if effective == "openrouter":
            target = tier_model(
                model,
                haiku=self._config.openrouter.haiku_model,
                sonnet=self._config.openrouter.sonnet_model,
                opus=self._config.openrouter.opus_model,
                fable=self._config.openrouter.fable_model,
            )
            if target != model:
                return target
            return model

        if not self._bucket_routes_to_ollama(effective):
            return model
        target = tier_model(
            model,
            haiku=self._config.ollama.haiku_model,
            sonnet=self._config.ollama.sonnet_model,
            opus=self._config.ollama.opus_model,
            fable=self._config.ollama.fable_model,
        )
        if target and is_ollama_model(target, self._config.ollama):
            return target
        return model

    def _runtime_model_for_chat(self, chat: ChatInfo) -> str:
        """Resolve the model the provider should actually run for a chat."""
        if chat.provider != "claude":
            return chat.model
        return self._resolve_claude_model(
            chat.model, chat.model_bucket, chat.project_id
        )

    def _thinking_level_for_chat(self, chat: ChatInfo) -> str:
        """Return the chat's thinking level, or "" when stale.

        A persisted level can stop matching its provider (e.g. data written
        before a guard fix). Dispatch falls back to the provider default
        instead of failing the turn.
        """
        if chat.provider == "codex" and canonical_tier(chat.model) == "fable":
            return CODEX_FABLE_THINKING_LEVEL
        if chat.thinking_level in THINKING_LEVELS.get(chat.provider, ()):
            return chat.thinking_level
        return ""

    def _build_extra_env(self, chat: ChatInfo) -> dict[str, str]:
        """Build extra environment variables for the provider.

        For Ollama-routed models (allowlisted in ``CiaoConfig.ollama``),
        also overrides ``ANTHROPIC_*`` so the spawned ``claude`` CLI hits
        the local Ollama daemon instead of api.anthropic.com.
        """
        env: dict[str, str] = {}
        project = self._projects.get(chat.project_id)
        env["CIAO_WORKSPACE"] = str(self._config.workspace_root)
        workspace = project.workspace if project else ""
        env["GWS_PROFILE"] = self._workspace_gws_profile(workspace)
        env["CIAO_ACTIVE_WORKSPACE"] = workspace or self._config.gws_default_profile
        if project:
            env["CIAO_ACTIVE_PROJECT"] = project.project_id
        env["CIAO_MODEL"] = chat.model
        env["CIAO_PROVIDER"] = chat.provider
        env["CIAO_MODEL_BUCKET"] = chat.model_bucket or ""
        if chat.provider == "claude":
            env.update(
                routing_env_for_model(self._runtime_model_for_chat(chat), self._config)
            )
        env["CIAO_CHAT_ID"] = chat.chat_id
        return env

    def _effective_mode_for_chat(self, chat: ChatInfo) -> BridgeMode:
        """Pick the runtime permission mode for ``chat``.

        Auto mode relies on Anthropic's server-side classifier to decide
        which tool calls run silently and which escalate. Ollama-routed
        chats hit ollama.com / the local daemon, neither of which expose
        that classifier, so the SDK falls back to prompting via
        ``can_use_tool`` for *every* tool call. The PWA shows that as a
        wall of "Approve use of Bash?" cards which is the opposite of
        what auto mode is for.

        With the tier-remap env now pointing the classifier
        (``CLAUDE_CODE_AUTO_MODE_MODEL`` / ``_BG_CLASSIFIER_MODEL``) at an
        Ollama-served haiku-tier model, auto mode works on Ollama-routed
        chats. Other modes (``plan``, ``bypass``, ``normal``) pass through
        unchanged: ``plan`` still works (no classifier needed), ``bypass``
        is already what we want, and ``normal`` is an explicit user opt-in
        to be asked every time.

        Legacy: ``CIAO_OLLAMA_AUTO_CLASSIFIER`` is no longer read; auto mode
        is always live for Ollama-routed chats. Remove it from your ``.env``.
        """
        return chat.mode

    # ── Streaming chat ───────────────────────────────────────────────────

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        chat = self._chats.get(chat_id)
        if chat is None:
            raise ValueError(f"Chat '{chat_id}' not found")
        if chat.archived:
            raise ValueError("Cannot send messages to an archived chat")

        provider = self._get_provider(chat_id)
        prefix = self._build_prompt_prefix(chat)
        provider_prompt = prompt
        if chat.provider == "codex":
            provider_prompt = (
                expand_slash_command(prompt, self._config.workspace_root) or prompt
            )
        full_prompt = prefix + provider_prompt if prefix else provider_prompt
        display_prompt = prefix + prompt if prefix else prompt
        handover_context_sent = bool(
            chat.handover_context_pending and chat.handover_messages
        )

        request = AgentRequest(
            prompt=full_prompt,
            model=self._runtime_model_for_chat(chat),
            provider=chat.provider,
            mode=self._effective_mode_for_chat(chat),
            display_prompt=display_prompt,
            resume_session=chat.session_id or None,
            images=images or [],
            extra_env=self._build_extra_env(chat),
            disallowed_tools=self.disallowed_tools_for_chat(chat),
            thinking_level=self._thinking_level_for_chat(chat),
        )

        response_text = ""
        effective_model = chat.model
        usage: dict[str, str] = {}
        quota: dict[str, str] = {}
        cost_usd: float = 0.0
        had_error = False
        tool_events: list[dict[str, Any]] = []

        async for event in provider.execute_streaming(request):
            yield event
            # Persist session_id as soon as the SDK exposes it. This way a
            # dropped WebSocket (app close, network blip) mid-stream still
            # leaves the chat recoverable: on reload, GET /messages can
            # replay turns from the SDK session file.
            sdk_sid = provider.current_session_id
            if sdk_sid and sdk_sid != chat.session_id:
                chat.session_id = sdk_sid
                self._save()
            if isinstance(event, ResultEvent):
                response_text = event.result
                had_error = bool(event.is_error)
                effective_model = event.effective_model or chat.model
                if chat.provider == "codex" and not chat.model and effective_model:
                    chat.model = effective_model
                    self._save()
                usage = event.usage
                quota = event.quota
                cost_usd = event.cost_usd or 0.0
                if event.session_id and event.session_id != chat.session_id:
                    chat.session_id = event.session_id
                    self._save()
                # Native questions are cleared by respond_question() (or a
                # later user send). An unanswered question from an interrupted
                # scheduled run stays available for continuation in the PWA.
            elif isinstance(event, ToolUseEvent):
                tool_events.append({
                    "id": event.tool_use_id or "",
                    "name": event.tool_name,
                    "input": {"summary": event.tool_input},
                })

        # Record transcript turn
        if handover_context_sent and not had_error:
            self.mark_handover_context_used(chat_id)

        ctx = ChatContext.for_web(chat_id)
        self._transcripts.record_turn(
            request,
            ctx=ctx,
            response_text=response_text,
            effective_model=effective_model,
            session_id=chat.session_id or None,
            usage=usage,
            quota=quota,
            input_kind="text",
            context_label=chat.title,
            provider=chat.provider,
            tool_events=tool_events,
        )

        # Update global cost
        if cost_usd > 0:
            self._state.add_cost(cost_usd)
        if usage:
            self._state.set_usage(usage)
        if quota:
            self._state.set_quota(quota)

        # Update session in state store
        self._state.update_session(chat.session_id or None, ctx)

    def get_active_stream(self, chat_id: str) -> ChatStream | None:
        """Return the in-flight ChatStream for this chat, if any."""
        return self._broker.get(chat_id)

    def queue_message(
        self,
        chat_id: str,
        text: str,
        images: list[ImageAttachment] | None = None,
    ) -> bool:
        """Append a user message to the active stream's pending queue.

        Returns True if queued, False if there's no active stream (caller
        should fall through to `start_stream`).
        """
        stream = self._broker.get(chat_id)
        if stream is None or stream.background:
            # Background drain streams have no drive loop to flush a queue;
            # the caller starts a real turn instead (which cancels the drain).
            return False
        image_refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                image_refs.append(str(ref))
        stream.enqueue(text, image_refs)
        stream.publish({
            "type": "queued",
            "text": text,
            "images": image_refs,
        })
        return True

    async def steer_stream(
        self,
        chat_id: str,
        text: str,
        images: list[ImageAttachment] | None = None,
    ) -> bool:
        """Inject a user message into the active SDK turn.

        Returns True if the message reached the live client, False otherwise
        (caller should fall back to queuing).
        """
        stream = self._broker.get(chat_id)
        if stream is None or stream.background:
            # No live turn to steer into during a background drain; the
            # caller falls through to queue → start_stream.
            return False
        provider = self._providers.get(chat_id)
        if provider is None:
            return False
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
        prefix = self._build_prompt_prefix(chat)
        full_prompt = prefix + text if prefix else text
        request = AgentRequest(
            prompt=full_prompt,
            model=self._runtime_model_for_chat(chat),
            provider=chat.provider,
            mode=self._effective_mode_for_chat(chat),
            resume_session=chat.session_id or None,
            images=images or [],
            extra_env=self._build_extra_env(chat),
            disallowed_tools=self.disallowed_tools_for_chat(chat),
            thinking_level=self._thinking_level_for_chat(chat),
        )
        ok = await provider.steer(request)
        if not ok:
            return False
        image_refs: list[str] = []
        for img in images or []:
            ref = getattr(img, "ref", None) or getattr(img, "original_filename", None)
            if ref:
                image_refs.append(str(ref))
        # Bump the chat's user-turn counter so image-replay for history lines up
        # with the additional turn we just injected.
        if image_refs:
            turn_index = chat.user_turn_count
            chat.user_turn_count = turn_index + 1
            chat.user_turn_images[str(turn_index)] = list(image_refs)
            now = _now_iso()
            chat.last_activity_at = now
            chat.last_read_at = now  # user sending = implicitly read
            self._save()
        stream.publish({
            "type": "steered",
            "text": text,
            "images": image_refs,
        })
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

    @property
    def background_agent_counts(self) -> dict[str, int]:
        """Last announced running-background-subagent count per chat (>0 only)."""
        return {cid: n for cid, n in self._background_agents_last.items() if n > 0}

    def set_chat_retry(
        self,
        chat_id: str,
        prompt: str,
        *,
        image_refs: list[str] | None = None,
        reason: str = "manual",
        next_at: str | None = None,
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
        chat.retry_next_at = next_at or _iso_after(chat.retry_interval_seconds)
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
        chat.retry_next_at = _iso_after(chat.retry_interval_seconds)
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
                due = _parse_iso(chat.retry_next_at)
                delay = 0.0
                if due is not None:
                    delay = max(0.0, (due - datetime.now(UTC)).total_seconds())
                if delay > 0:
                    await asyncio.sleep(delay)
                chat = self._chats.get(chat_id)
                if chat is None or chat.archived or chat.retry_status != "pending":
                    return
                if self._broker.get(chat_id) is not None:
                    chat.retry_next_at = _iso_after(chat.retry_interval_seconds)
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
    def _result_snippet(text: str, limit: int = 140) -> str:
        flat = " ".join((text or "").strip().splitlines()).strip()
        if len(flat) > limit:
            flat = flat[: limit - 3] + "..."
        return flat

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
        if existing is not None and existing.background:
            # A between-turns drain stream is live. The user's send starts a
            # real turn: cancel the drain (its cleanup finishes the stream)
            # and fall through — the new stream replaces it in the broker.
            self._cancel_between_turns_drain(chat_id)
            self._broker.clear(chat_id, existing)
        elif existing is not None:
            logger.debug("Chat %s already has an active stream; reusing", chat_id)
            return existing

        if not is_retry:
            chat_for_retry = self._chats.get(chat_id)
            if chat_for_retry is not None and chat_for_retry.retry_status == "pending":
                self._clear_chat_retry(chat_for_retry)

        from ciao.web.chat_broker import event_to_json

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
            # A new user turn answers (or supersedes) any paused question, so
            # the persisted picker state no longer applies.
            chat_meta.pending_question = ""
            turn_index = chat_meta.user_turn_count
            chat_meta.user_turn_count = turn_index + 1
            if image_refs:
                chat_meta.user_turn_images[str(turn_index)] = list(image_refs)
            sent_at_iso = _now_iso()
            chat_meta.last_activity_at = sent_at_iso
            chat_meta.last_read_at = sent_at_iso  # user sending = implicitly read
            chat_meta.user_turn_timings[str(turn_index)] = {"sent_at": sent_at_iso}
            self._turn_perf_started[(chat_id, turn_index)] = time.perf_counter()

        # First buffered event: echo the user prompt so any client subscribing
        # later (fresh connect, reconnect) can render it without relying on
        # `/api/chats/{id}/messages` — which may race the SDK's session-file
        # write or, for a brand-new session, have nothing yet.
        echo_payload: dict = {
            "type": "user_echo",
            "text": prompt,
            "images": image_refs,
        }
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
        # would have, but the cheap Ollama free-tier title model
        # absorbs that cost easily and we can always rename manually.
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

        async def _drive() -> None:
            # Loop across the initial turn plus any queued follow-ups. Each
            # pass runs a full stream_chat() call; we reuse the same ChatStream
            # so attached WS clients see one continuous event flow (no broker
            # churn, no need to resubscribe mid-way).
            #
            # Auto-title generation runs separately as its own task fired
            # right after the user echo (see start_stream above), so this
            # loop no longer threads title state through.
            current_prompt = prompt
            current_images = images
            # Track the turn_index of the *current* in-flight prompt so we can
            # stamp completed_at / duration_ms onto the right ChatInfo record
            # when the ResultEvent arrives. Reassigned to the new turn_index
            # for each queued follow-up.
            current_turn_index = turn_index
            last_assistant_text = ""
            had_error = False
            had_provider_progress = False
            # A between-turns drain and receive_response() consume from the
            # same SDK stream and must never run concurrently. The cancel in
            # start_stream is fire-and-forget; await the task here so the
            # drain has fully unwound before the first provider call.
            await self._await_between_turns_drain(chat_id)
            try:
                while True:
                    turn_assistant_text = ""
                    question_paused = False
                    try:
                        async for event in self.stream_chat(
                            chat_id, current_prompt, images=current_images
                        ):
                            payload = event_to_json(event)
                            if (
                                payload
                                and isinstance(event, ResultEvent)
                                and current_turn_index is not None
                            ):
                                completed_at = _now_iso()
                                started_perf = self._turn_perf_started.pop(
                                    (chat_id, current_turn_index), None
                                )
                                duration_ms: int | None = None
                                if started_perf is not None:
                                    duration_ms = int(
                                        (time.perf_counter() - started_perf) * 1000
                                    )
                                cm = self._chats.get(chat_id)
                                if cm is not None:
                                    rec = cm.user_turn_timings.setdefault(
                                        str(current_turn_index), {}
                                    )
                                    rec["completed_at"] = completed_at
                                    if duration_ms is not None:
                                        rec["duration_ms"] = duration_ms
                                    sent_at_rec = rec.get("sent_at", "")
                                    self._save()
                                else:
                                    sent_at_rec = ""
                                payload["completed_at"] = completed_at
                                if sent_at_rec:
                                    payload["sent_at"] = sent_at_rec
                                if duration_ms is not None:
                                    payload["duration_ms"] = duration_ms
                            if payload:
                                stream.publish(payload)
                            if isinstance(event, (AssistantTextDelta, ThinkingEvent, ToolUseEvent, PermissionRequestEvent)):
                                had_provider_progress = True
                            if isinstance(event, PermissionRequestEvent):
                                # Turn is blocked on the user. Notify the
                                # push manager so a backgrounded/locked
                                # device gets the Approve/Deny prompt.
                                self._notify_permission(chat_id, event)
                                if unattended:
                                    self.respond_permission(
                                        chat_id,
                                        request_id=event.request_id,
                                        approved=False,
                                        reason=(
                                            "Scheduled runs cannot wait for "
                                            "interactive approval."
                                        ),
                                    )
                            if isinstance(event, ToolUseEvent) and event.tool_name == "AskUserQuestion" and event.tool_input.strip():
                                # The headless CLI can't render the SDK's
                                # interactive picker. Left alone it auto-cancels
                                # the question with empty answers and keeps
                                # generating a self-answered continuation that
                                # pollutes the session; a PreToolUse "defer"
                                # hook is no better — the CLI surfaces the
                                # deferred tool to the model as an internal
                                # error and it chatters a fallback (verified
                                # live, claude-agent-sdk 0.2.93). Interrupting
                                # the turn is the only clean stop: generation
                                # halts right at the question. So notify the
                                # user, persist the question so a reloaded PWA
                                # can rebuild the picker, interrupt, then stop
                                # consuming. The CLI records an interrupt
                                # sentinel that /messages already strips, and
                                # the user's answer starts a fresh resumed turn.
                                question_payload = event.tool_input
                                if event.request_id:
                                    try:
                                        parsed_question = json.loads(event.tool_input)
                                    except (TypeError, json.JSONDecodeError):
                                        parsed_question = {"questions": []}
                                    if not isinstance(parsed_question, dict):
                                        parsed_question = {"questions": []}
                                    parsed_question["request_id"] = event.request_id
                                    question_payload = json.dumps(
                                        parsed_question, ensure_ascii=False
                                    )
                                self._notify_question(chat_id, question_payload)
                                cm_q = self._chats.get(chat_id)
                                if cm_q is not None:
                                    cm_q.pending_question = question_payload
                                    self._save()
                                # Codex exposes a native blocking app-server
                                # request. Keep consuming the turn while the
                                # PWA answers that request in-band. Claude's
                                # SDK picker still requires the interrupt and
                                # next-turn answer flow documented above.
                                if event.request_id:
                                    if unattended:
                                        q_provider = self._providers.get(chat_id)
                                        if q_provider is not None:
                                            try:
                                                await q_provider.stop_active()
                                            except Exception:
                                                logger.exception(
                                                    "interrupt unattended question failed for chat %s",
                                                    chat_id,
                                                )
                                        question_paused = True
                                        break
                                    continue
                                q_provider = self._providers.get(chat_id)
                                if q_provider is not None:
                                    try:
                                        await q_provider.stop_active()
                                    except Exception:
                                        logger.exception(
                                            "interrupt after AskUserQuestion failed for chat %s",
                                            chat_id,
                                        )
                                question_paused = True
                                break
                            if isinstance(event, ToolUseEvent):
                                # Schedule a debounced file snapshot for
                                # Write/Edit/MultiEdit/NotebookEdit. The
                                # ToolUseEvent fires *before* the CLI executes
                                # the tool, so a 1.5s delay lets the actual
                                # write land first. Bursts collapse — only the
                                # last edit per file in a quick cluster ends
                                # up captured. payload["file_touch"] is the
                                # already-normalised metadata set by
                                # event_to_json.
                                touch = payload.get("file_touch") if payload else None
                                if isinstance(touch, dict):
                                    fp = touch.get("file_path") or ""
                                    if fp:
                                        try:
                                            self._snapshots.schedule_capture(
                                                chat_id=chat_id,
                                                file_path=fp,
                                                action=touch.get("action", "touched"),
                                                tool=event.tool_name,
                                            )
                                        except Exception:
                                            logger.exception(
                                                "schedule_capture failed for %s",
                                                fp,
                                            )
                            if isinstance(event, ResultEvent):
                                if event.is_error:
                                    had_error = True
                                    if (
                                        not had_provider_progress
                                        and _is_retryable_quota_error(event.result or "")
                                    ):
                                        self.set_chat_retry(
                                            chat_id,
                                            current_prompt,
                                            image_refs=self._image_refs(current_images),
                                            reason=event.result or "quota limit",
                                        )
                                        stream.publish({
                                            "type": "chat_retry",
                                            "status": "pending",
                                        })
                                else:
                                    turn_assistant_text = event.result or ""
                    except Exception as exc:
                        # A user-initiated stop may surface here (if the SDK
                        # raises rather than yielding a terminal ResultEvent)
                        # or as an is_error=True result below. Either path is
                        # intentional, not a real failure, so fall through to
                        # the drain-pending step below instead of breaking —
                        # queued follow-ups should still be sent.
                        if stream.user_stopped:
                            logger.info("Stream stopped by user for chat %s", chat_id)
                        else:
                            logger.exception("Stream error for chat %s", chat_id)
                            error_msg = str(exc)
                            stderr = getattr(exc, "stderr", None)
                            if stderr:
                                error_msg = f"{error_msg}\n{stderr}"
                            stream.publish({"type": "error", "message": error_msg})
                            had_error = True
                            if not had_provider_progress and _is_retryable_quota_error(error_msg):
                                self.set_chat_retry(
                                    chat_id,
                                    current_prompt,
                                    image_refs=self._image_refs(current_images),
                                    reason=error_msg,
                                )
                                stream.publish({
                                    "type": "chat_retry",
                                    "status": "pending",
                                })
                            break

                    if turn_assistant_text:
                        last_assistant_text = turn_assistant_text

                    if question_paused:
                        break

                    pending = stream.drain_pending()
                    # A user-initiated stop produces an error-shaped ResultEvent
                    # (is_error=True). Treat that as intentional: consume the
                    # flag, reset had_error so the loop can start a new turn
                    # with whatever the user queued, and only bail if there's
                    # nothing pending.
                    if stream.user_stopped:
                        stream.user_stopped = False
                        if pending:
                            had_error = False
                    if not pending or had_error:
                        break

                    combined_text = "\n\n".join(
                        p["text"].strip()
                        for p in pending
                        if p.get("text", "").strip()
                    )
                    merged_image_refs: list[str] = []
                    merged_images: list[ImageAttachment] = []
                    for p in pending:
                        for ref in p.get("images") or []:
                            attachment = self.resolve_image_ref(ref)
                            if attachment:
                                merged_images.append(attachment)
                                merged_image_refs.append(ref)
                    if not combined_text:
                        break

                    # Bump user-turn counter so image replay from history lines
                    # up. Capture turn_index2 first so we can attach it to the
                    # user_echo payload for client-side dedup.
                    turn_index2: int | None = None
                    sent_at_iso2: str = ""
                    chat_meta2 = self._chats.get(chat_id)
                    if chat_meta2 is not None:
                        chat_meta2.pending_question = ""
                        turn_index2 = chat_meta2.user_turn_count
                        chat_meta2.user_turn_count = turn_index2 + 1
                        if merged_image_refs:
                            chat_meta2.user_turn_images[str(turn_index2)] = list(
                                merged_image_refs
                            )
                        sent_at_iso2 = _now_iso()
                        chat_meta2.last_activity_at = sent_at_iso2
                        chat_meta2.last_read_at = sent_at_iso2  # user sending = implicitly read
                        chat_meta2.user_turn_timings[str(turn_index2)] = {
                            "sent_at": sent_at_iso2,
                        }
                        self._turn_perf_started[(chat_id, turn_index2)] = (
                            time.perf_counter()
                        )
                        self._save()

                    # Echo the queued follow-up as a user bubble so any client
                    # that didn't render queued chips still sees the turn.
                    followup_echo: dict = {
                        "type": "user_echo",
                        "text": combined_text,
                        "images": merged_image_refs,
                    }
                    if turn_index2 is not None:
                        followup_echo["turn_index"] = turn_index2
                    if sent_at_iso2:
                        followup_echo["sent_at"] = sent_at_iso2
                    stream.publish(followup_echo)

                    current_prompt = combined_text
                    current_images = merged_images or None
                    current_turn_index = turn_index2
            finally:
                # Drop any perf-clock entry that didn't get consumed by a
                # ResultEvent (errored / aborted turn) so the dict stays bounded.
                if current_turn_index is not None:
                    self._turn_perf_started.pop((chat_id, current_turn_index), None)
                # Always clean up the per-chat stream entry first so subsequent
                # sends can start a new one immediately.
                stream.finish()
                self._broker.clear(chat_id, stream)
                # Tell awareness subscribers the stream is no longer active.
                self._events.publish({
                    "type": "chat_streaming_done",
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "is_error": had_error,
                })
                # Background subagents can outlive the parent turn. Start a
                # lightweight watcher so the UI gets notified when they finish,
                # instead of leaving the chat stuck on "I'll compile once the
                # agents report back".
                chat_for_watcher = self._chats.get(chat_id)
                if (
                    chat_for_watcher is not None
                    and chat_for_watcher.session_id
                    and chat_for_watcher.provider in {"claude", "codex"}
                ):
                    self._start_subagent_watcher(chat_id, project_id)
                    # Keep the SDK pipe drained while the client idles: a
                    # finishing background subagent triggers a CLI-initiated
                    # parent turn whose events would otherwise rot in the
                    # transport buffer (and its stale ResultMessage would
                    # truncate the next turn). The drain also gives the PWA
                    # a live view of that follow-up turn.
                    if chat_for_watcher.provider == "claude":
                        self._start_between_turns_drain(chat_id, project_id)
                # Successful turn(s): announce result ready (drives unread
                # badges + in-app toast on clients that aren't focused on
                # this chat) and dispatch web push (decoupled from any WS).
                if not had_error and last_assistant_text:
                    snippet = self._result_snippet(last_assistant_text)
                    chat_now = self._chats.get(chat_id)
                    if chat_now is not None:
                        if chat_now.retry_status == "pending" and is_retry:
                            self._clear_chat_retry(chat_now)
                        chat_now.last_activity_at = _now_iso()
                        self._save()
                    title = chat_now.title if chat_now else "Ciaobot"
                    self._events.publish({
                        "type": "chat_result_ready",
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "title": title,
                        "snippet": snippet,
                    })
                    # Schedule the push with a small delay. If the user reads
                    # the chat on any device in the window (via /api/chats/
                    # {id}/read), the pending task is cancelled and no push
                    # fires. New replies to the same chat cancel and restart
                    # the timer (see _schedule_push).
                    self._schedule_push(chat_id, title, snippet)

        asyncio.create_task(_drive())
        return stream

    async def _auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None:
        new_title: str | None = None
        try:
            new_title = await self.auto_title_if_default(
                chat_id, user_text, assistant_text
            )
        except Exception:
            logger.exception("Auto-title failed for %s", chat_id)
        # Always clear the pending shimmer and emit a ready event, even if
        # title generation produced nothing (e.g. user renamed mid-flight,
        # or all fallbacks returned None). Leaving title_status="pending"
        # would hang the shimmer in the sidebar indefinitely.
        chat = self._chats.get(chat_id)
        if chat is None:
            return
        chat.title_status = "ready"
        self._events.publish({
            "type": "chat_title",
            "chat_id": chat_id,
            "title": new_title or chat.title,
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
        chat.last_read_at = _now_iso()
        self._save()
        self._cancel_pending_push(chat_id)
        self._events.publish({
            "type": "chat_read",
            "chat_id": chat_id,
            "last_read_at": chat.last_read_at,
        })
        return chat

    def mark_all_read(self) -> list[str]:
        """Mark every non-archived unread chat as read. Returns the list of
        chat_ids that were touched. Emits one `chat_read` event per chat so
        WS handlers can update incrementally.
        """
        now = _now_iso()
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
        return touched

    # ── Delayed push scheduler ───────────────────────────────────────────

    def _cancel_pending_push(self, chat_id: str) -> None:
        task = self._pending_push.pop(chat_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_push(self, chat_id: str, title: str, snippet: str) -> None:
        """Queue a delayed push for this chat. Cancels any prior pending
        task for the same chat so rapid successive replies coalesce into a
        single push fired after the last reply settles.
        """
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

    def _start_subagent_watcher(self, chat_id: str, project_id: str) -> None:
        """Replace any existing subagent watcher for this chat with a new one."""
        old = self._pending_subagent_watchers.get(chat_id)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(self._watch_subagent_completion(chat_id, project_id))
        self._pending_subagent_watchers[chat_id] = task

    def _publish_subagent_count(self, chat_id: str, project_id: str, count: int) -> None:
        self._background_agents_last[chat_id] = count
        self._events.publish({
            "type": "chat_subagents_ready",
            "chat_id": chat_id,
            "project_id": project_id,
            "remaining": count,
        })

    async def _watch_subagent_completion(self, chat_id: str, project_id: str) -> None:
        """Watch the session JSONL until background subagents finish.

        The SDK's ``list_subagents`` enumerates transcript *files*, which
        persist after completion, so its count never drops. The parent
        session JSONL is the reliable signal: async Agent dispatches are
        recorded with ``toolUseResult.isAsync`` and each completion appends a
        ``<task-notification>`` envelope (see ciao/subagent_tracking.py).

        Emits ``chat_subagents_ready`` whenever the running count changes and
        schedules a delayed push when the last one completes.
        """
        chat = self._chats.get(chat_id)
        if chat is None or not chat.session_id:
            return
        if chat.provider == "codex":
            await self._watch_codex_subagent_completion(chat_id, project_id)
            return
        path = subagent_tracking.find_parent_session_file(
            chat.session_id, self._config.workspace_root
        )
        if path is None:
            return

        last_count = -1
        last_size = -1
        # Background agents can run for a long while; poll cheaply (a stat
        # per tick, a re-parse only when the file grew) with a wide horizon.
        deadline = time.perf_counter() + 3600
        try:
            while time.perf_counter() < deadline:
                try:
                    size = path.stat().st_size
                except OSError:
                    break
                if size != last_size:
                    last_size = size
                    count = subagent_tracking.parse_session_subagents(
                        path
                    ).running_background
                    if count != last_count:
                        self._publish_subagent_count(chat_id, project_id, count)
                        if count == 0 and last_count > 0:
                            chat_now = self._chats.get(chat_id)
                            if chat_now is not None:
                                chat_now.last_activity_at = _now_iso()
                                self._save()
                            # Poke the parent to synthesize a final report. The
                            # CLI won't auto-continue the turn on its own, so
                            # without this the chat sits on the interim
                            # "I'll report back" message forever. When the
                            # nudge lands on the live client the between-turns
                            # drain publishes the reply (and its own push); we
                            # only fall back to a bare push if the nudge could
                            # not be delivered.
                            nudged = await self._nudge_synthesis_after_subagents(
                                chat_id
                            )
                            if not nudged:
                                title = chat_now.title if chat_now else "Ciaobot"
                                self._schedule_push(
                                    chat_id, title, "Background agents finished"
                                )
                        last_count = count
                    if count == 0:
                        break
                await asyncio.sleep(3)
        finally:
            self._background_agents_last.pop(chat_id, None)
            # Clean up our slot when the watcher exits.
            current = self._pending_subagent_watchers.get(chat_id)
            if current is asyncio.current_task():
                self._pending_subagent_watchers.pop(chat_id, None)

    async def _watch_codex_subagent_completion(
        self, chat_id: str, project_id: str
    ) -> None:
        """Poll app-server thread state while Codex collaboration children run."""
        last_count = -1
        deadline = time.perf_counter() + 3600
        try:
            while time.perf_counter() < deadline:
                chat = self._chats.get(chat_id)
                if chat is None or chat.provider != "codex" or not chat.session_id:
                    break
                thread = await CodexProvider.read_thread(
                    self._config.workspace_root, chat.session_id
                )
                if thread is None:
                    break
                tree = await CodexProvider.read_collab_tree(
                    self._config.workspace_root, thread
                )
                count, had_subagents = codex_collab_tree_counts(tree)
                if count != last_count:
                    self._publish_subagent_count(chat_id, project_id, count)
                    if count == 0 and last_count > 0:
                        chat.last_activity_at = _now_iso()
                        self._save()
                        self._schedule_push(
                            chat_id, chat.title or "Ciaobot", "Background agents finished"
                        )
                    last_count = count
                if not had_subagents or count == 0:
                    break
                await asyncio.sleep(3)
        finally:
            self._background_agents_last.pop(chat_id, None)
            current = self._pending_subagent_watchers.get(chat_id)
            if current is asyncio.current_task():
                self._pending_subagent_watchers.pop(chat_id, None)

    async def _nudge_synthesis_after_subagents(self, chat_id: str) -> bool:
        """Ask the parent to post a final report once its subagents finish.

        A background ``Agent`` dispatch ends the parent turn immediately and
        the CLI does not resume it when the subagent completes, so the chat
        would otherwise stay on the interim "I'll report back" message. We
        inject a synthesis prompt on the persistent client; the between-turns
        drain (started alongside this watcher) consumes the resulting turn and
        publishes it like any other reply. Returns True when the nudge reached
        a live client, False otherwise (caller falls back to a plain push).
        """
        provider = self._providers.get(chat_id)
        if provider is None or not provider.can_drain:
            return False
        # A user send since the turn ended cancels the drain; don't inject into
        # a live user turn or a chat with no drain to capture the reply.
        drain = self._between_turn_drains.get(chat_id)
        if drain is None or drain.done():
            return False
        existing = self._broker.get(chat_id)
        if existing is not None and not existing.background:
            return False
        chat = self._chats.get(chat_id)
        if chat is None:
            return False
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
            return await provider.steer(request)
        except Exception:  # noqa: BLE001 — a failed nudge must not kill the watcher
            logger.exception(
                "Subagent synthesis nudge failed for chat %s", chat_id
            )
            return False

    # ── Between-turns SDK drain ──────────────────────────────────────────

    def _cancel_between_turns_drain(self, chat_id: str) -> None:
        """Fire-and-forget cancel; pair with _await_between_turns_drain."""
        task = self._between_turn_drains.get(chat_id)
        if task is not None and not task.done():
            task.cancel()

    async def _await_between_turns_drain(self, chat_id: str) -> None:
        """Wait until any drain task for this chat has fully unwound."""
        task = self._between_turn_drains.pop(chat_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — drain errors must not kill the turn
            pass

    def _start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        provider_service = self._providers.get(chat_id)
        if provider_service is None or not provider_service.can_drain:
            return
        self._cancel_between_turns_drain(chat_id)
        task = asyncio.create_task(self._drain_between_turns(chat_id, project_id))
        self._between_turn_drains[chat_id] = task

    async def _drain_between_turns(self, chat_id: str, project_id: str) -> None:
        """Consume and publish SDK events that arrive with no turn active.

        When a background subagent completes, the CLI injects a
        task-notification; a follow-up parent turn then arrives either run by
        the CLI on its own (CLI-version dependent — observed not to happen
        reliably) or requested by the completion watcher's synthesis nudge
        (``_nudge_synthesis_after_subagents``). This loop forwards those
        events to a broker stream (so open chat sockets render them live) and
        announces the follow-up's result like a normal turn (unread badge,
        toast, delayed push). Each such turn gets its own background
        ChatStream so replay stays turn-shaped.
        """
        from ciao.web.chat_broker import event_to_json

        provider_service = self._providers.get(chat_id)
        if provider_service is None:
            return
        stream: ChatStream | None = None

        def close_stream(had_error: bool) -> None:
            nonlocal stream
            if stream is None:
                return
            stream.finish()
            self._broker.clear(chat_id, stream)
            self._events.publish({
                "type": "chat_streaming_done",
                "chat_id": chat_id,
                "project_id": project_id,
                "is_error": had_error,
            })
            stream = None

        try:
            async for event in provider_service.drain_events():
                payload = event_to_json(event)
                if payload is None:
                    continue
                if stream is None:
                    # Only open a visible stream when a real event arrives —
                    # most drains sit idle until cancelled by the next turn.
                    stream = ChatStream(background=True)
                    self._broker.register(chat_id, stream)
                    self._events.publish({
                        "type": "chat_streaming_started",
                        "chat_id": chat_id,
                        "project_id": project_id,
                    })
                stream.publish(payload)
                if isinstance(event, PermissionRequestEvent):
                    self._notify_permission(chat_id, event)
                if isinstance(event, ResultEvent):
                    text = event.result or ""
                    is_error = bool(event.is_error)
                    # Record the synthesis result so the schedule pipeline can
                    # feed it to the auto-archive classifier once subagents
                    # settle (see _await_schedule_subagents / dispatch_schedule).
                    self._last_drain_result[chat_id] = (text, is_error)
                    close_stream(is_error)
                    if not is_error and text:
                        chat_now = self._chats.get(chat_id)
                        if chat_now is not None:
                            chat_now.last_activity_at = _now_iso()
                            self._save()
                        title = chat_now.title if chat_now else "Ciaobot"
                        snippet = self._result_snippet(text)
                        self._events.publish({
                            "type": "chat_result_ready",
                            "chat_id": chat_id,
                            "project_id": project_id,
                            "title": title,
                            "snippet": snippet,
                        })
                        self._schedule_push(chat_id, title, snippet)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a broken drain must not crash the app
            logger.exception("Between-turns drain failed for chat %s", chat_id)
        finally:
            close_stream(False)

    def _notify_permission(
        self, chat_id: str, event: PermissionRequestEvent
    ) -> None:
        """Fire the configured permission-push callback, if any.

        Callback errors are swallowed: a broken push subscription or a transient
        send failure must never kill the turn (the user can still answer via
        the in-app bubble on their current device).
        """
        cb = self.notify_permission_cb
        if cb is None:
            return
        try:
            cb(chat_id, event.tool_name, event.message, event.request_id)
        except Exception:
            logger.exception("notify_permission_cb failed for %s", chat_id)

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
                lines = [q.get("question", "") for q in questions if q.get("question")]
                body = "\n".join(lines) if lines else question_json
        except Exception:
            pass
        try:
            cb(chat_id, body)
        except Exception:
            logger.exception("notify_question_cb failed for %s", chat_id)

    def respond_permission(
        self,
        chat_id: str,
        *,
        request_id: str,
        approved: bool,
        reason: str = "",
    ) -> bool:
        """Deliver the user's allow/deny answer to the provider's gate.

        Returns True if the answer matched a pending prompt. False means the
        chat has no provider yet, or the request id is stale (late tap after
        the turn ended, duplicate delivery, etc.). Either case is benign;
        the caller just ignores the reply.

        Also strips the buffered ``permission_request`` from the active
        broker stream so a later reconnect (chat reopened, second tab,
        flaky network) doesn't replay the prompt as a phantom approval
        card. We do this even when the gate has nothing pending: stale
        replies still indicate the user has dealt with the prompt, and
        the buffered event should not pop back up.
        """
        # Strip from replay buffer first so even a stale-id reply (gate
        # already drained on turn teardown) cleans up the recorded event.
        stream = self._broker.get(chat_id)
        if stream is not None:
            stream.resolve_permission(request_id)
        provider_service = self._providers.get(chat_id)
        if provider_service is None:
            return False
        provider = provider_service.provider
        if provider is None:
            return False
        # Pi provider uses extension_ui_response instead of SDK permission gates
        if hasattr(provider, "send_permission_response"):
            return provider.send_permission_response(request_id, approved)
        gate = provider.permission_gate
        return gate.answer(request_id, approved=approved, reason=reason)

    def respond_question(
        self,
        chat_id: str,
        *,
        request_id: str,
        answers: dict[str, list[str]],
    ) -> bool:
        """Deliver an answer to a provider-native user-input request."""
        provider_service = self._providers.get(chat_id)
        if provider_service is None or provider_service.provider is None:
            return False
        responder = getattr(
            provider_service.provider, "send_question_response", None
        )
        if not callable(responder):
            return False
        accepted = bool(responder(request_id, answers))
        if accepted:
            chat = self._chats.get(chat_id)
            if chat is not None:
                chat.pending_question = ""
                self._save()
        return accepted

    async def stop_chat(self, chat_id: str) -> bool:
        # Mark the active stream as user-stopped so the drive loop flushes
        # any queued follow-up messages instead of dropping them. A stop is
        # intentional, not an error.
        stream = self._broker.get(chat_id)
        if stream is not None:
            stream.user_stopped = True
            if stream.background:
                # No active handle exists between turns; stopping means
                # ending the drain (its cleanup finishes the stream).
                await self._await_between_turns_drain(chat_id)
                return True
        provider = self._providers.get(chat_id)
        if provider is None:
            return False
        return await provider.stop_active()

    # ── Auto-title generation ────────────────────────────────────────────

    async def auto_title_if_default(
        self, chat_id: str, user_text: str, assistant_text: str = ""
    ) -> str | None:
        """If chat title is still the default, ask Claude for a short title.

        Returns the new title or None if nothing was changed.

        ``assistant_text`` is optional: when supplied (legacy path that
        waited for the first assistant reply) the titler gets both
        sides of the exchange. Current callers fire right after the
        user echo, so it's typically empty — `_generate_chat_title`
        handles user-only input fine.
        """
        chat = self._chats.get(chat_id)
        if chat is None or chat.title != "New Chat":
            return None

        project = self._projects.get(chat.project_id)
        workspace = project.workspace if project else None
        from ciao.providers.ollama import is_local_ollama_model
        from ciao.providers.routing import resolve_with_fallback

        requested = (
            chat.model if chat.provider == "codex"
            else resolve_title_model(self._config, workspace)
        )
        if chat.provider == "codex":
            title_model = requested
            title_env = self._build_extra_env(chat)
        elif self._config.title_model_override:
            title_model = requested
            from ciao.providers.routing import routing_routine_env_for_model

            title_env = routing_routine_env_for_model(requested, self._config)
        else:
            title_model, title_env, note = resolve_with_fallback(
                requested,
                self._config,
                default_model="haiku",
            )
            if note:
                logger.info("Title generation %s", note)

        # Local-daemon models may need to cold-load weights (Ollama unloads
        # after ~5 min idle) and may spend tokens thinking before the title,
        # so they get a much longer leash than the 15s cloud default. The
        # call is fire-and-forget, so a slow title doesn't block anything.
        title_timeout = (
            90.0 if is_local_ollama_model(title_model, self._config.ollama) else 15.0
        )

        async with job_runs.track(
            "title", "Title generation", model=title_model,
            extra={"chat_id": chat_id},
        ) as run:
            title_kwargs: dict[str, object] = {
                "model": title_model,
                "cwd": self._config.workspace_root,
                "env": title_env,
                "timeout_s": title_timeout,
            }
            # Keep the established Claude/Ollama call signature intact for
            # integrations that wrap the title helper. Codex is the only
            # provider that needs an explicit dispatch hint here.
            if chat.provider == "codex":
                title_kwargs["provider"] = "codex"
            title = await _generate_chat_title(
                user_text,
                assistant_text,
                **title_kwargs,
            )
            if not title:
                run.status = "error"
                run.error = "title model returned no title (timeout/failure)"
                return None

            # Re-check: user may have renamed while we were generating.
            chat = self._chats.get(chat_id)
            if chat is None or chat.title != "New Chat":
                run.skip("user renamed during generation")
                return None
            chat.title = title
            self._save()
            run.extra["title"] = title
            return title

    # ── Schedule dispatch ────────────────────────────────────────────────

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = 900.0
    ) -> tuple[bool, bool]:
        """Block until the schedule chat's background subagents finish.

        A schedule turn can delegate to background subagents (e.g. memory
        curation dispatches the memory agent) and return before they finish.
        The archive decision must not run against that half-complete state, so
        we poll the parent session JSONL — the reliable running-count signal
        (see ciao/subagent_tracking.py) — until it drains.

        Returns ``(settled, had_async)``: ``settled`` is True when no
        subagents remain running (or none were ever tracked), False when the
        timeout elapses with agents still running; ``had_async`` is True when
        the session ever dispatched a background subagent. Errors resolve to
        ``(True, False)`` so a tracking failure never blocks the pipeline.
        """
        chat = self._chats.get(chat_id)
        if chat is None or not chat.session_id:
            return True, False
        if chat.provider == "codex":
            deadline = time.perf_counter() + timeout_s
            had_async = False
            running = 0
            while time.perf_counter() < deadline:
                thread = await CodexProvider.read_thread(
                    self._config.workspace_root, chat.session_id
                )
                if thread is None:
                    return True, had_async
                tree = await CodexProvider.read_collab_tree(
                    self._config.workspace_root, thread
                )
                running, had_now = codex_collab_tree_counts(tree)
                had_async = had_async or had_now
                if running == 0:
                    return True, had_async
                await asyncio.sleep(3)
            return running == 0, had_async
        if chat.provider != "claude":
            return True, False
        try:
            path = subagent_tracking.find_parent_session_file(
                chat.session_id, self._config.workspace_root
            )
        except Exception:  # noqa: BLE001
            logger.exception("Subagent wait: session file lookup failed for %s", chat_id)
            return True, False
        if path is None:
            return True, False

        deadline = time.perf_counter() + timeout_s
        last_size = -1
        running = 0
        had_async = False
        while time.perf_counter() < deadline:
            try:
                size = path.stat().st_size
            except OSError:
                return True, had_async
            if size != last_size:
                last_size = size
                try:
                    state = subagent_tracking.parse_session_subagents(path)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Subagent wait: parse failed for %s", chat_id
                    )
                    return True, had_async
                running = state.running_background
                if not had_async:
                    had_async = any(
                        info.is_async for info in state.subagents.values()
                    )
                if running == 0:
                    return True, had_async
            await asyncio.sleep(3)
        if running:
            logger.warning(
                "Schedule chat %s still has %d background subagent(s) after %.0fs; "
                "keeping chat visible",
                chat_id,
                running,
                timeout_s,
            )
        return running == 0, had_async

    async def _wait_for_drain_result(
        self, chat_id: str, *, timeout_s: float = 180.0
    ) -> tuple[str, bool] | None:
        """Wait for the between-turns drain to record a post-subagent result.

        Returns ``(text, is_error)`` from the CLI's synthesis turn, or None if
        none arrived within ``timeout_s``. Callers pop the slot beforehand so a
        stale result from an earlier turn is never returned. Exits early as
        soon as a result lands; the timeout only bounds the rare case where the
        CLI never emits a synthesis turn after the subagent completes.
        """
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            result = self._last_drain_result.get(chat_id)
            if result is not None:
                return result
            await asyncio.sleep(1)
        return None

    async def _schedule_run_needs_user(self, entry: object, outcome: ScheduleRunOutcome) -> bool:
        """Return True when an auto-archive schedule result deserves attention.

        Conservative default: if the classifier cannot produce strict JSON,
        keep the chat visible.
        """
        title = str(getattr(entry, "prompt", "")).split("\n", 1)[0].strip()
        payload = {
            "schedule_id": getattr(entry, "schedule_id", ""),
            "title": title,
            "final_output": outcome.final_text[-6000:],
        }
        system_prompt = (
            "You decide whether the user needs to see a scheduled routine result in the chat interface. "
            "Return only JSON: {\"needs_user\": boolean, \"reason\": string}. "
            "needs_user=false when the run is routine maintenance, even if it updated files, triaged proposals, "
            "or created file stubs automatically (e.g. routine memory curation, git syncs, daily logs, baseline bumps). "
            "Set needs_user=true ONLY when there is an actual problem, error, warning, unresolved conflict, "
            "a specific question/decision asked of the user, or a new external finding that requires their direct "
            "intervention or judgment to proceed."
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
        try:
            from ciao.insights import resolve_insights_model
            from ciao.providers.routing import resolve_with_fallback

            # Route through the shared resolver (same as ciao/insights.py) so
            # the classifier lands on an available backend. The raw
            # ``_ollama_env`` path had no fallback: when the intended backend
            # was unreachable it passed an id the Anthropic subscription can't
            # serve, the one-shot raised, and the run was kept visible instead
            # of auto-archived.
            project_id = getattr(entry, "web_project_id", None)
            project = self._projects.get(project_id) if project_id else None
            workspace = project.workspace if project else None
            fixed_chat_id = getattr(entry, "web_chat_id", None)
            fixed_chat = self._chats.get(fixed_chat_id) if fixed_chat_id else None
            classifier_provider = (
                fixed_chat.provider if fixed_chat is not None
                else getattr(entry, "provider", "")
                or self.schedule_default_provider(project_id)
            )
            if classifier_provider == "codex":
                model = (
                    fixed_chat.model if fixed_chat is not None
                    else getattr(entry, "model", "")
                    or self.schedule_default_model(project_id)
                )
                env = (
                    self._build_extra_env(fixed_chat)
                    if fixed_chat is not None
                    else {"CIAO_PROVIDER": "codex"}
                )
                note = None
            else:
                insights_model = resolve_insights_model(self._config, workspace)
                model, env, note = resolve_with_fallback(
                    insights_model,
                    self._config,
                    default_model=insights_model,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Schedule attention classifier setup failed; keeping chat visible")
            return True
        tracked_provider = (
            "codex" if classifier_provider == "codex"
            else ("routed" if env else "claude")
        )
        async with job_runs.track(
            "schedule_attention_classifier",
            "Schedule attention classifier",
            model=model,
            provider=tracked_provider,
            extra={
                "schedule_id": payload["schedule_id"],
                "workspace": workspace or "",
            },
        ) as run:
            if note:
                run.extra["fallback_note"] = note
                logger.info("Schedule attention classifier %s", note)
            try:
                from ciao.providers.oneshot import run_oneshot

                text = await run_oneshot(
                    user_prompt,
                    system_prompt=system_prompt,
                    model=model,
                    env=env,
                    timeout_s=60.0,
                    provider=classifier_provider,
                    cwd=self._config.workspace_root,
                )
                raw = text.strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```(?:json)?\s*", "", raw)
                    raw = re.sub(r"\s*```$", "", raw)
                verdict = json.loads(raw)
                needs_user = bool(verdict.get("needs_user", True))
                run.extra["needs_user"] = needs_user
                reason = str(verdict.get("reason", "")).strip()
                if reason:
                    run.extra["reason"] = reason[:500]
                return needs_user
            except Exception as exc:  # noqa: BLE001
                run.status = "error"
                run.error = str(exc)[:1000]
                logger.exception(
                    "Schedule attention classifier failed with model %s; keeping chat visible",
                    model,
                )
                return True

    def prepare_schedule_chat(
        self,
        entry,  # ScheduleEntry
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
    ) -> str | None:
        """Create/resolve the target chat for a schedule dispatch.

        Returns the chat_id or None if the target can't be resolved.
        This is synchronous so callers can get the chat_id before the
        async stream starts.

        ``provider`` applies only when this dispatch creates a new chat
        (web_project_id path). For fixed-chat schedules (web_chat_id),
        the existing chat's provider is honoured.
        """
        from datetime import UTC, datetime

        web_project_id = getattr(entry, "web_project_id", None)
        web_chat_id = getattr(entry, "web_chat_id", None)

        if web_project_id:
            project = self._projects.get(web_project_id)
            if project is None:
                project = self._resolve_schedule_project(web_project_id, entry)
            if project is None:
                logger.warning("Schedule target project %s not found, skipping", web_project_id)
                return None
            title_base = prompt.split("\n")[0].strip().rstrip(".")
            if len(title_base) > 40:
                title_base = title_base[:37] + "..."
            date_str = datetime.now(UTC).strftime("%b %d")
            title = f"{title_base} - {date_str}"
            chat = self.create_chat(
                project.project_id,
                title=title,
                model=model,
                mode=mode,
                provider=provider or None,
            )
            return chat.chat_id
        elif web_chat_id:
            chat = self._chats.get(web_chat_id)
            if chat is None:
                logger.warning("Schedule target chat %s not found, skipping", web_chat_id)
                return None
            chat.model = model
            chat.mode = mode
            return web_chat_id
        elif getattr(entry, "scope", "") == "system":
            project = self._resolve_schedule_project("", entry)
            if project is None:
                logger.warning("System schedule %s has no default project, skipping", getattr(entry, "schedule_id", ""))
                return None
            title_base = prompt.split("\n")[0].strip().rstrip(".")
            if len(title_base) > 40:
                title_base = title_base[:37] + "..."
            date_str = datetime.now(UTC).strftime("%b %d")
            title = f"{title_base} - {date_str}"
            chat = self.create_chat(
                project.project_id,
                title=title,
                model=model,
                mode=mode,
                provider=provider or None,
            )
            return chat.chat_id
        else:
            logger.warning("Schedule has no web target, skipping")
            return None

    def chat_stream_active(self, chat_id: str) -> bool:
        """True when the chat has a live user-visible turn in flight.

        Between-turns drain streams don't count: they are background
        housekeeping that a new prompt is allowed to replace.
        """
        existing = self._broker.get(chat_id)
        return existing is not None and not existing.background

    async def dispatch_loop(self, entry, prompt: str) -> dict[str, str]:
        """Dispatch one loop iteration into the loop's fixed chat.

        Unlike schedules, loops never override the chat's model or mode:
        each iteration runs with whatever the user configured on the chat.
        Returns a status dict: "ok", "error", "busy" (active turn already
        in flight), or "missing-chat".
        """
        chat_id = entry.web_chat_id
        if self._chats.get(chat_id) is None:
            logger.warning("Loop target chat %s not found, skipping", chat_id)
            return {"status": "missing-chat"}
        if self.chat_stream_active(chat_id):
            return {"status": "busy", "chat_id": chat_id}
        is_error = False
        try:
            stream = self.start_stream(chat_id, prompt, unattended=True)
            async for payload in stream.subscribe():
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "error":
                    is_error = True
                elif event_type == "result":
                    is_error = bool(payload.get("is_error"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Loop dispatch to %s failed", chat_id)
            return {"status": "error", "chat_id": chat_id}
        return {"status": "error" if is_error else "ok", "chat_id": chat_id}

    async def dispatch_schedule(
        self,
        entry,  # ScheduleEntry
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
        *,
        target_chat_id: str | None = None,
    ) -> dict[str, str]:
        """Dispatch a schedule and return metadata (chat_id, archived_to)."""
        web_project_id = getattr(entry, "web_project_id", None)
        web_chat_id = getattr(entry, "web_chat_id", None)

        target_id = target_chat_id or self.prepare_schedule_chat(
            entry, prompt, model, mode, provider,
        )
        if target_id is None:
            return {}

        result: dict[str, str] = {"chat_id": target_id}
        outcome = ScheduleRunOutcome()

        # Job-run recording: this method swallows its own errors (the broad
        # except below sets outcome.stream_error and continues) and has a
        # single exit, so we time it here and record once before returning.
        _sched_perf = time.perf_counter()
        _sched_started = datetime.now(UTC)
        _sched_schedule_id = getattr(entry, "schedule_id", "") or ""

        # Save original model/mode for fixed-chat dispatches
        orig_model = orig_mode = None
        if not web_project_id and web_chat_id:
            chat = self._chats.get(target_id)
            if chat:
                orig_model, orig_mode = chat.model, chat.mode

        # Substitute error-log placeholder for weekly maintenance schedules
        had_error_placeholder = "{{ERROR_LOG}}" in prompt
        if had_error_placeholder:
            errors = await asyncio.to_thread(
                tail_error_log, self._config.workspace_root, 200
            )
            prompt = prompt.replace(
                "{{ERROR_LOG}}",
                errors or "(no errors logged this week)",
            )
        # Richer variant: error log + failed background-job runs
        had_issue_placeholder = "{{ISSUE_REPORT}}" in prompt
        if had_issue_placeholder:
            from ciao.debug_report import build_issue_report

            issue_report = await asyncio.to_thread(
                build_issue_report, self._config.workspace_root
            )
            prompt = prompt.replace(
                "{{ISSUE_REPORT}}", issue_report["report_text"]
            )

        try:
            stream = self.start_stream(target_id, prompt, unattended=True)
            async for payload in stream.subscribe():
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type")
                if event_type == "permission_request":
                    outcome.permission_requested = True
                elif (
                    event_type == "tool_use"
                    and payload.get("tool_name") == "AskUserQuestion"
                ):
                    outcome.question_requested = True
                elif event_type == "chat_retry":
                    if (payload.get("status") or "") == "pending":
                        outcome.retry_pending = True
                elif event_type == "error":
                    outcome.stream_error = True
                elif event_type == "result":
                    outcome.completed = True
                    outcome.is_error = bool(payload.get("is_error"))
                    outcome.final_text = str(payload.get("text") or "")
            # Clear only after a clean run: a failed triage must not wipe
            # the backlog it never processed.
            if (
                (had_error_placeholder or had_issue_placeholder)
                and _schedule_run_clean(outcome)
            ):
                await asyncio.to_thread(
                    clear_error_log, self._config.workspace_root
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            outcome.stream_error = True
            logger.exception("Schedule dispatch to %s failed", target_id)
        finally:
            if orig_model is not None and not web_project_id and web_chat_id:
                chat = self._chats.get(target_id)
                if chat:
                    chat.model = orig_model
                    chat.mode = orig_mode  # type: ignore[assignment]

        chat_state = self._chats.get(target_id)
        if chat_state and chat_state.retry_status == "pending":
            outcome.retry_pending = True

        # A clean parent turn may still have live background subagents (e.g.
        # curation delegating to the memory agent). Wait for them to finish
        # before the archive decision so the classifier judges the completed
        # result — not an interim "dispatched, will report later" message. If
        # they don't settle in time, mark the run pending so it stays visible.
        if _schedule_run_clean(outcome):
            # Drop any stale synthesis result before waiting so we only pick up
            # the turn that runs when *these* subagents finish. The drain that
            # captures it was started by start_stream's completion handler.
            self._last_drain_result.pop(target_id, None)
            settled, had_async = await self._await_schedule_subagents(target_id)
            if not settled:
                outcome.subagents_pending = True
            elif had_async and chat_state is not None and chat_state.provider == "claude":
                # Background subagents finished: the CLI runs a synthesis turn
                # whose result the between-turns drain records. Feed that real
                # summary to the archive classifier instead of the interim
                # parent message. Bounded; exits as soon as the result lands.
                synth = await self._wait_for_drain_result(target_id)
                if synth is not None:
                    synth_text, synth_error = synth
                    if synth_text:
                        outcome.final_text = synth_text
                    if synth_error:
                        outcome.is_error = True
            self._last_drain_result.pop(target_id, None)

        needs_user = False
        if getattr(entry, "archive_policy", "manual") == "auto" and _schedule_run_clean(outcome):
            needs_user = await self._schedule_run_needs_user(entry, outcome)

        if _should_auto_archive_schedule_run(entry, outcome, needs_user=needs_user):
            chat_meta = self._chats.get(target_id)
            project_meta = (
                self._projects.get(chat_meta.project_id) if chat_meta else None
            )
            try:
                archive_outcome = self.archive_chat(target_id)
            except Exception:  # noqa: BLE001
                logger.exception("Auto-archive failed for schedule chat %s", target_id)
                archive_outcome = None
            if archive_outcome is not None:
                try:
                    self.run_archive_postprocess(
                        target_id, archive_outcome, chat_meta, project_meta
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Auto-archive postprocess failed for schedule chat %s",
                        target_id,
                    )
                outcome.archived_to = str(archive_outcome.path)
                result["archived_to"] = str(archive_outcome.path)
            else:
                logger.warning(
                    "Auto-archive requested but archive_chat returned None for %s",
                    target_id,
                )

        if outcome.stream_error or outcome.is_error:
            _sched_status = "error"
            _sched_error = (outcome.final_text or "stream error")[:1000]
        elif outcome.permission_requested or outcome.question_requested or outcome.retry_pending:
            _sched_status = "skipped"
            _sched_error = None
        else:
            _sched_status = "ok"
            _sched_error = None
        job_runs.record_run(job_runs.JobRun(
            job="schedule_dispatch",
            label="Scheduled dispatch",
            category="content",
            started_at=_sched_started.isoformat(),
            ended_at=datetime.now(UTC).isoformat(),
            duration_ms=int((time.perf_counter() - _sched_perf) * 1000),
            status=_sched_status,
            model=model,
            provider=provider or "claude",
            error=_sched_error,
            extra={
                "schedule_id": _sched_schedule_id,
                "chat_id": target_id,
                "archived_to": outcome.archived_to,
                "permission_requested": outcome.permission_requested,
                "question_requested": outcome.question_requested,
                "retry_pending": outcome.retry_pending,
            },
        ))
        return result

    def _resolve_schedule_project(
        self, stale_id: str, entry: object
    ) -> ProjectInfo | None:
        """Resolve a stale web_project_id to a local project.

        schedules.json is shared via git but project IDs are per-instance
        (regenerated on each fresh init). When the ID doesn't match, infer
        the workspace from the schedule ID convention and fall back to the
        matching General project.
        """
        # Prefer the explicit workspace field; it survives the per-device id
        # regeneration that makes web_project_id go stale. Fall back to the
        # "sched-work*" naming convention for entries created before the field
        # existed (work schedules whose id lacks that prefix, e.g. the morning
        # action briefing, would otherwise misroute to personal).
        workspace = (getattr(entry, "workspace", "") or "").strip().lower()
        if not self._is_known_workspace(workspace):
            schedule_id = getattr(entry, "schedule_id", "") or ""
            workspace = "work" if schedule_id.startswith("sched-work") else "personal"
        for p in self._projects.values():
            if p.workspace == workspace and p.name == "General":
                logger.info(
                    "Resolved stale project %s -> %s (%s General)",
                    stale_id, p.project_id, workspace,
                )
                return p
        return None

    # ── Voice ────────────────────────────────────────────────────────────

    async def transcribe_voice(self, audio_path: Path) -> tuple[str, float]:
        """Transcribe an audio file. Returns (text, cost_usd).

        Engine selection follows ``config.transcription_engine``: ``local``
        runs mlx-whisper on-device (free); anything else uses the OpenAI
        cloud API. If the local engine fails or is not installed, it raises
        a ValueError.
        """
        from ciao.voice import (
            MlxWhisperTranscriber,
            VoiceTranscriber,
            mlx_whisper_available,
        )

        if self._config.transcription_engine == "local":
            if mlx_whisper_available():
                try:
                    transcriber = MlxWhisperTranscriber(
                        self._config.transcription_local_model
                    )
                    text = await transcriber.transcribe(audio_path)
                    return text, 0.0
                except Exception as exc:
                    raise ValueError(
                        f"Local voice transcription failed: {exc}. "
                        "Ensure mlx-whisper is properly configured or change the engine in Settings → Models."
                    ) from exc
            else:
                raise ValueError(
                    "Local voice transcription is selected but mlx-whisper is not installed. "
                    "Install the dependency or change the engine to Cloud in Settings → Models."
                )

        if not self._config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for voice transcription")
        transcriber = VoiceTranscriber(self._config)
        text = await transcriber.transcribe(audio_path)
        # Estimate cost from file duration (rough: file_size / ~16000 bytes per second for OGG)
        try:
            size = audio_path.stat().st_size
            duration_sec = max(size / 16000, 1.0)
        except OSError:
            duration_sec = 1.0
        cost = duration_sec / 60 * 0.003
        return text, cost

    async def synthesize_speech(self, text: str) -> tuple[bytes, str, float]:
        """Read a message aloud. Returns (audio_bytes, mime_type, cost_usd).

        Engine selection follows ``config.tts_engine``: ``local`` runs
        Kokoro on-device via kokoro-onnx (free); anything else uses the
        OpenAI cloud API. Markdown is reduced to speakable text first.
        """
        from ciao.voice import (
            KokoroSpeaker,
            OpenAISpeaker,
            kokoro_available,
            speech_text,
        )

        spoken = speech_text(text)
        if not spoken:
            raise ValueError("Nothing to read aloud in this message")

        if self._config.tts_engine == "local":
            if kokoro_available():
                try:
                    speaker = KokoroSpeaker(self._config.tts_local_voice)
                    audio = await speaker.speak(spoken)
                    return audio, speaker.mime_type, 0.0
                except Exception as exc:
                    raise ValueError(
                        f"Local speech synthesis failed: {exc}. "
                        "Ensure kokoro-onnx is properly configured or change the engine in Settings → Models."
                    ) from exc
            else:
                raise ValueError(
                    "Local speech synthesis is selected but kokoro-onnx is not installed. "
                    "Install the dependency or change the engine to Cloud in Settings → Models."
                )

        if not self._config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for speech synthesis")
        speaker = OpenAISpeaker(self._config)
        audio = await speaker.speak(spoken)
        # Estimate cost from text length (rough: ~1000 chars per spoken
        # minute at ~$0.015/min for gpt-4o-mini-tts).
        cost = len(spoken) / 1000 * 0.015
        return audio, speaker.mime_type, cost

    def save_voice_upload(self, data: bytes, filename: str) -> Path:
        """Save an uploaded voice file and return its path."""
        ext = Path(filename).suffix.lower() or ".webm"
        if ext not in _ALLOWED_VOICE_EXTENSIONS:
            raise ValueError(f"Unsupported voice format: {ext}")
        target = self._config.media_root / f"web_voice_{_uuid8()}{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if len(data) > self._config.max_voice_size_bytes:
            target.unlink(missing_ok=True)
            raise ValueError("Voice file too large")
        return target

    # ── Project files ────────────────────────────────────────────────────

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
                "kind": _classify_file(resolved),
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
        entries.

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
        if ext not in _PROJECT_UPLOAD_EXTS:
            raise ValueError(f"unsupported file type: {ext or '(none)'}")
        if len(data) > _PROJECT_UPLOAD_MAX_BYTES:
            raise ValueError("file too large")
        # Collision: foo.png -> foo-2.png -> foo-3.png ...
        target = vault_dir / base
        if target.exists():
            stem = Path(base).stem
            n = 2
            while True:
                candidate = vault_dir / f"{stem}-{n}{ext}"
                if not candidate.exists():
                    target = candidate
                    break
                n += 1
        target.write_bytes(data)
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
            "kind": _classify_file(resolved),
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
        ref = f"web_{_uuid8()}{ext}"
        target = self._config.media_root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if len(data) > self._config.max_image_size_bytes:
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
        provider = self._providers.pop(chat_id, None)
        if provider:
            asyncio.ensure_future(provider.disconnect())
