"""One-shot model calls, dispatched to a runtime provider.

``provider`` selects the runner: a provider whose descriptor names an ACP
``AgentSpec`` (see :mod:`ciao.providers.acp.oneshot`), otherwise ``opencode``.

Calls are intentionally bare: custom system prompt, no filesystem
settings/skills, no tools, no MCP discovery. Titles, insights,
and similar routines never need the agent tool surface.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from ciao import provider_registry
from ciao.tool_path import prepend_engine_path

logger = logging.getLogger(__name__)


class OneShotError(RuntimeError):
    """A one-shot model call failed with whatever detail was available.

    ``detail`` carries the composed upstream error (status / subtype /
    stop_reason / body) so callers -- the titler, ``job_runs`` -- can
    surface it instead of a bare "One-shot query failed". ``transient``
    marks failures worth retrying: empty-body / gateway errors succeed on a
    second try, whereas auth / subscription / bad-model errors will fail
    again.
    """

    def __init__(
        self, detail: str, *, status: int | None = None, transient: bool = False
    ) -> None:
        super().__init__(detail or "one-shot query failed")
        self.detail = detail or "one-shot query failed"
        self.status = status
        self.transient = transient


async def _run_opencode_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None,
    cwd: Path | None,
) -> str:
    from ciao.models import AgentRequest, ResultEvent
    from ciao.providers.opencode import OpencodeProvider

    # One-shot routines have a no-tools contract. Keep the server in a fresh
    # empty directory so opencode cannot discover project instructions,
    # configs, or MCP registrations from the workspace. Its deny-all session
    # permission rules also prevent read/glob/grep/list from reaching files.
    with tempfile.TemporaryDirectory(prefix="ciaobot-opencode-oneshot-") as isolated:
        opencode = OpencodeProvider(
            Path(isolated),
            developer_instructions=system_prompt,
            tools_enabled=False,
        )
        try:
            async for event in opencode.run_streaming(
                AgentRequest(
                    prompt=prompt,
                    model=model,
                    mode="auto",
                    provider="opencode",
                    extra_env=env or {},
                ),
                lambda _handle: None,
            ):
                if isinstance(event, ResultEvent):
                    if event.is_error:
                        detail = (event.result or "").strip() or "opencode one-shot failed"
                        # opencode reports a rejected prompt or a dead server as a
                        # terminal ResultEvent; it does not distinguish retriable
                        # upstream flakes, so do not double-retry here.
                        raise OneShotError(detail, transient=False)
                    return event.result
            return ""
        finally:
            await opencode.delete_current_session()
            await opencode.disconnect()


async def run_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None = None,
    timeout_s: float = 120.0,
    provider: str = "claude",
    cwd: Path | None = None,
    max_retries: int = 1,
    retry_backoff_s: float = 0.5,
    max_turns: int = 2,
) -> str:
    """Run a single-turn model call and return the assistant's text.

    Empty string is a valid return (the model had nothing to say); the
    caller decides how to handle it. ``timeout_s`` wraps each attempt via
    :func:`asyncio.wait_for`.

    ``max_turns`` is still accepted for the callers that count their attempts
    (the behavioral eval's budget). No runner reads it now: an ACP one-shot is one
    prompt on one session.

    On a transient failure the call is retried up to ``max_retries`` times with
    exponential backoff; a non-transient failure and a timeout are raised at once.
    On failure an :class:`OneShotError` carrying the upstream ``detail`` is raised
    so callers can log the real cause instead of a generic string.
    """
    # Chats can run with Claude Code's fast-mode suffix ("[1m]"), but the
    # one-shot API path rejects annotated model ids ("There's an issue with
    # the selected model (claude-opus-4-8[1m])"). Background calls never
    # need fast mode; use the base model.
    if model.endswith("[1m]"):
        model = model[: -len("[1m]")]

    descriptor = provider_registry.get(provider)
    acp_spec = descriptor.acp_spec() if descriptor is not None else None
    if acp_spec is not None and descriptor is not None:
        # An ACP agent runs its own one-shot session; its no-tools contract is its ``oneshot_meta``.
        provider_cls: Any = descriptor.factory()
        session_meta = provider_cls.oneshot_meta()

        async def _attempt() -> str:
            from ciao.providers.acp.oneshot import run_acp_oneshot

            acp_env = {**provider_cls.spawn_env(), **dict(env or {})}
            acp_env["PATH"] = prepend_engine_path(acp_env.get("PATH"))
            return await run_acp_oneshot(
                acp_spec,
                prompt,
                system_prompt=system_prompt,
                model=model,
                env=acp_env,
                session_meta=session_meta,
            )
    elif provider == "opencode":
        async def _attempt() -> str:
            one_shot_env = dict(env or {})
            # The engine's own bin dir leads PATH so a ``ciao`` the one-shot's
            # opencode server launches is this engine's CLI, not a stale install
            # earlier on the user's PATH (#989).
            one_shot_env["PATH"] = prepend_engine_path(one_shot_env.get("PATH"))
            return await _run_opencode_oneshot(
                prompt,
                system_prompt=system_prompt,
                model=model,
                env=one_shot_env,
                cwd=cwd,
            )
    else:
        raise ValueError(f"Unknown one-shot provider '{provider}'")

    attempts = max(0, int(max_retries)) + 1
    last_exc: OneShotError | None = None
    for attempt_no in range(attempts):
        try:
            return await asyncio.wait_for(_attempt(), timeout=timeout_s)
        except OneShotError as exc:
            last_exc = exc
            if not exc.transient or attempt_no >= attempts - 1:
                raise
            delay = retry_backoff_s * (2 ** attempt_no)
            logger.info(
                "one-shot %s (model=%s) attempt %d/%d failed transiently (%s); "
                "retrying in %.1fs",
                provider, model, attempt_no + 1, attempts, exc.detail, delay,
            )
            await asyncio.sleep(delay)
    # Loop only exits via return or raise; this satisfies type-checkers.
    assert last_exc is not None
    raise last_exc
