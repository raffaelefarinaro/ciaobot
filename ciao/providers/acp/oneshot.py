"""One-shot model calls over ACP: a throwaway session in an empty directory, unattended (#3137).

``ciao.providers.oneshot.run_oneshot`` dispatches here for a provider whose descriptor names an
``AgentSpec``. The agent's own tools stay available to it, but every permission request is
rejected, and the session runs in a directory that holds nothing the agent could read.
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
from collections.abc import Mapping
from contextlib import aclosing
from pathlib import Path
from types import MappingProxyType
from typing import Any

from acp.exceptions import RequestError

from ciao.models import AssistantTextDelta, ResultEvent
from ciao.providers.acp.connection import AcpConnection, AgentStartError
from ciao.providers.acp.permissions import PermissionBroker
from ciao.providers.acp.provider import AgentClient
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.spec import AgentSpec
from ciao.providers.oneshot import OneShotError
from ciao.redact import redact_text

logger = logging.getLogger(__name__)

_NO_META: Mapping[str, Any] = MappingProxyType({})


async def run_acp_oneshot(
    spec: AgentSpec,
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: Mapping[str, str],
    session_meta: Mapping[str, Any] = _NO_META,
) -> str:
    """Return the agent's top-level text for one prompt.

    The agent runs in a fresh empty directory, in plan mode when it offers one. A turn that ends
    in an error stop reason raises ``OneShotError`` with a fixed detail: the adapter's own text
    never reaches the caller, and the reason is logged without its value.
    """
    with tempfile.TemporaryDirectory(prefix="ciaobot-acp-oneshot-") as root:
        work = Path(root) / "work"
        work.mkdir()
        client = AgentClient()  # type: ignore[abstract]
        try:
            connection = await AcpConnection.open(spec, cwd=work, env=env, client=client)
        except AgentStartError as exc:
            logger.warning("%s one-shot agent did not start: %s", spec.id, redact_text(str(exc)))
            raise OneShotError(f"{spec.id} agent did not start.", transient=False) from None
        client.connection = connection
        session: AcpSession | None = None
        try:
            broker = PermissionBroker(lambda _event: None)
            client.broker = broker
            try:
                session, _replaced = await AcpSession.open(
                    connection,
                    spec,
                    cwd=work,
                    resume=None,
                    additional_dirs=[],
                    journal_dir=Path(root) / "journal",
                    broker=broker,
                    session_meta=session_meta,
                )
                client.session = session
                await session.apply(mode="plan", model=model, thinking="")
                text: list[str] = []
                blocks: list[dict[str, Any]] = [
                    {"type": "text", "text": f"{system_prompt}\n\n{prompt}"},
                ]
                async with aclosing(session.prompt(
                    blocks, display=prompt, unattended=True
                )) as stream:
                    async for event in stream:
                        if isinstance(event, AssistantTextDelta) and event.parent_tool_use_id is None:
                            text.append(event.text)
                        elif isinstance(event, ResultEvent) and event.is_error:
                            raise OneShotError(
                                f"{spec.id} one-shot ended without a reply.", transient=False
                            )
                return "".join(text)
            except RequestError as exc:
                logger.warning("%s one-shot request failed (%s).", spec.id, type(exc).__name__)
                raise OneShotError(f"{spec.id} one-shot request failed.", transient=False) from None
        finally:
            client.session = None
            client.broker = None
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
            await connection.close()
