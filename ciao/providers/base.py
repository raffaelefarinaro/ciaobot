"""Shared provider helpers."""

from __future__ import annotations

import asyncio
import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.models import (
    AgentRequest,
    ResultEvent,
    StreamEvent,
)


# One cap for background work (agents, shells, monitors): a watcher keeps
# polling, and a provider with outstanding background work stays protected
# from the idle reap, reload and restart gate, for up to this long. A task
# that never reports a terminal state is forgotten after it.
BACKGROUND_WORK_MAX_S = 24 * 3600


@dataclass(slots=True)
class ActiveHandle:
    """Base handle for an active provider operation."""

    async def stop(self) -> None:
        """Stop the active operation."""


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Provider features consumed by Ciaobot outside the streaming loop."""

    resume: bool = False
    fork: bool = False
    images: bool = False
    stop: bool = False
    permissions: bool = False
    structured_questions: bool = False
    dynamic_models: bool = False
    thinking_levels: bool = False
    usage: bool = False
    quota: bool = False
    background_subagents: bool = False
    session_history: bool = False
    schedule_unattended: bool = False


@dataclass(frozen=True, slots=True)
class QuestionResponseResult:
    """Outcome of one native-form reply/cancel operation."""

    ok: bool
    error: str = ""
    retryable: bool = True
    delivered_as_message: bool = False
    expired: bool = False
    # The request was already answered (another tab won, or V2 returned 409):
    # nothing was applied for this reply; the caller should clear its card.
    already_resolved: bool = False


def build_prompt(request: AgentRequest) -> str:
    """Build the shared prompt text for a request.

    Each entry carries the absolute on-disk path. `original_filename` is the
    media-root ref (`web_<hash>.png`) for uploads replayed from a chat, which
    looks like a relative path but resolves nowhere from the agent's cwd:
    agents saw the image, tried to Read the bare name, got "file does not
    exist", and concluded the attachment had failed. The path makes a Read
    work, and the note says it is unnecessary.

    Every line must stay in `N. text` form: `_IMAGE_MANIFEST_RE` in
    ciao/web/routes_api.py strips this block from the replayed user bubble.
    """
    if not request.images:
        return request.prompt

    lines = [request.prompt, "", "[INCOMING IMAGES]"]
    for index, image in enumerate(request.images, start=1):
        summary = f"{index}. {image.original_filename} (attached in this message; file: {image.path})"
        if image.caption:
            summary = f"{summary} - caption: {image.caption}"
        lines.append(summary)
    return "\n".join(lines).strip()


_NATIVE_SLASH = re.compile(r"^/[\w:-]+(?:\s|$)")


def native_slash_prompt(request: AgentRequest) -> tuple[str, str] | None:
    """Split a native slash command from the context capsule Ciaobot put in front of it.

    Returns ``(command_text, capsule)`` when the prompt starts with the recorded
    ``injected_context_prefix`` and what follows it is a slash command; None
    otherwise. Trust comes from the recorded prefix only: capsule-shaped text the
    user typed is never split off, so it stays user text.
    """
    capsule = request.injected_context_prefix
    if not capsule or not request.prompt.startswith(capsule):
        return None
    command = request.prompt[len(capsule):]
    if not _NATIVE_SLASH.match(command):
        return None
    return command, capsule


def prepend_stable_context(request: AgentRequest) -> None:
    """Re-send stable routing context after a provider replaces a session."""
    prefix = request.stable_context_prefix
    if not prefix or request.prompt.startswith(prefix):
        return
    request.prompt = prefix + request.prompt
    # The restored capsule is engine-owned as well, so the trusted boundary in
    # ``injected_context_prefix`` moves with the prompt instead of being
    # recomputed from delimiters later.
    request.injected_context_prefix = prefix + request.injected_context_prefix


class CleanupTimedOut(TimeoutError):
    """A session cleanup ran out of time. The caller skips the chat's remaining session ids."""


class BaseProvider(ABC):
    """Abstract provider interface."""

    capabilities = ProviderCapabilities()
    # Set by the host: asked when the provider's own continuation after a background shell starts
    # speaking; returns the held shell notice that reply answers, or None. Providers without one ignore it.
    continuation_hook: Callable[[list[Any]], str | None] | None = None

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        self.workspace_root = workspace_root
        self.config = config

    @property
    def response_session_id(self) -> str:
        """Session used to fence replies when requests are not held locally."""
        return ""

    @property
    def current_session_id(self) -> str | None:
        """Session id currently known to this adapter; None when unknown."""
        return None

    def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
        return False

    def permission_request_session(self, request_id: str) -> str:
        return ""

    def tool_use_id_for_request(self, request_id: str, session_id: str = "") -> str:
        return ""

    def has_question_request(self, request_id: str, session_id: str = "") -> bool:
        return False

    def question_request_session(self, request_id: str) -> str:
        return ""

    async def respond_permission(
        self, request_id: str, approved: bool, reason: str = "", *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        return QuestionResponseResult(
            False, "Permission responses are not supported by this provider", False
        )

    async def respond_question(
        self, request_id: str, answers: Mapping[str, Sequence[str]], *,
        cancel: bool = False, session_id: str = "",
    ) -> QuestionResponseResult:
        return QuestionResponseResult(
            False, "Native question responses are not supported by this provider", False
        )

    @classmethod
    async def native_title_candidates(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> tuple[str, ...]:
        """Read candidate titles in provider-preference order; empty means unavailable.

        A stateless read of the provider's persisted session, usable when no
        live provider instance exists. Adapters only translate their native
        storage into ordered candidate strings; the host validates them.
        ``runtime_root`` is the chat's runtime directory (``config.state_path.parent``).
        """
        return ()

    @classmethod
    async def delete_session(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> bool | None:
        """Reclaim a persisted session; False requests a cleanup warning.

        True is affirmative confirmation. None supplies no confirmation signal:
        an unsupported adapter or a best-effort cleanup with adapter-owned
        diagnostics. It is not a claim that anything was deleted. An adapter
        whose cleanup ran out of time raises :class:`CleanupTimedOut`.

        ``runtime_root`` is the same runtime directory the live provider writes
        its own state under (``config.state_path.parent``). It is required with
        no default, so a caller cannot silently address the workspace instead.
        """
        return None

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Optional dynamic model catalog; empty when none is exposed."""
        return []

    def agent_accepts_images(self) -> bool | None:
        """Whether the live agent takes image input, as it stated at its handshake.

        None when no agent is connected or it states nothing. Only an explicit False
        is a refusal; the preflight treats every other answer as capable.
        """
        return None

    @classmethod
    def invalidate_model_catalog(cls) -> None:
        """Drop any cached model catalog so the next ``model_catalog`` probes again."""
        return None

    @classmethod
    def model_catalog_epoch(cls) -> int:
        """Return the generation for host-side model catalog consistency checks."""
        return 0

    @classmethod
    def register_model_catalog_mutation(cls, task: asyncio.Task[Any]) -> None:
        """Keep a provider catalog mutation visible to concurrent catalog readers."""
        return None

    @classmethod
    def unregister_model_catalog_mutation(cls, task: asyncio.Task[Any]) -> None:
        """Release a provider catalog mutation after its worker actually completes."""
        return None

    @classmethod
    async def wait_for_model_catalog_mutations(cls) -> bool:
        """Wait for relevant auth changes before reading this provider's status."""
        return True

    @abstractmethod
    def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one request and stream normalized events.

        Declared as a plain ``def`` returning an ``AsyncGenerator`` (not
        ``async def``) so the type matches concrete ``async def ... yield``
        overrides; an ``async def`` body without a ``yield`` would type as a
        coroutine that *returns* a generator instead.
        """
        ...

    async def disconnect(self) -> None:
        """Discard any provider-side connection state."""

    @property
    def can_drain(self) -> bool:
        """True when the adapter permits a between-turn reader right now.

        Dynamic eligibility, not a static capability or a generic
        connectivity probe: OpenCode also requires a session and outstanding
        background work, Claude additionally requires a connected client.
        Eligibility stays independent of ``cli_connected`` and of the
        capabilities; the host decides whether to start a reader.
        """
        return False

    @property
    def cli_connected(self) -> bool:
        """True while this adapter's persistent CLI subprocess owner is alive.

        Asymmetric no-owner semantics: an adapter that tracks no long-lived
        subprocess inherits True, so only an owner an adapter actually keeps
        reports a loss. The answer says nothing about any server being
        reachable — OpenCode reports no owner, not server liveness.
        """
        return True

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Yield between-turn provider events until cancelled.

        Adapters without a between-turn reader inherit this empty generator,
        so calling it does no construction, startup, foreground-stream or
        other work. An adapter with a reader overrides it; the caller owns
        exclusivity and must cancel a running drain and await its full
        unwind — cancellation alone does not establish exclusive ownership.
        """
        return
        yield  # unreachable: gives this body a yield so calling it builds an async generator

    @property
    def background_work_outstanding(self) -> bool:
        """True while background work this provider's process owns is running."""
        return False

    @property
    def is_connected(self) -> bool:
        """True while a live provider-side connection exists.

        Read paths (MCP status) may query it without
        starting anything. Distinct from ``cli_connected``, which answers
        "does the CLI subprocess owner still live" and is True by default.
        """
        return False

    @classmethod
    async def read_thread(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> dict[str, Any]:
        """Session metadata plus message history for transcript replay; ``{}`` when none.

        ``runtime_root`` is ``config.state_path.parent``; see :meth:`delete_session`.
        """
        return {}

    async def abort_session(self, session_id: str) -> ResultEvent | None:
        """Abort a provider-side session; a no-op for adapters without one.

        Returns the partial result of a late turn the abort stopped, when it had text, so the
        caller can keep it in the transcript. Nothing is returned for a session with none.
        """
        return None

    @property
    def seconds_since_activity(self) -> float:
        """Seconds since the provider last heard from its server."""
        return math.inf
