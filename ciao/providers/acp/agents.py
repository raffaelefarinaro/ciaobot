"""Real ACP agent specs and the provider classes that run them (#3138, #3139).

Every agent Ciaobot runs over ACP has one ``AgentSpec`` here and one ``AcpProvider`` subclass that
sets it. The ``claude`` provider runs ``claude-agent-acp``. Ciaobot's disable options (the trim
layer, the workspace's extra denies and the claude.ai connectors switch) reach the adapter as
``_meta.claudeCode.options``, which the adapter spreads into the SDK options. Everything else
the CLI owns, so nothing else is sent.

Login stays in the CLI: ``codex_auth_command`` only builds the command the Settings terminal
runs, and Ciaobot never reads or stores the credentials it writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ciao import claude_sessions
from ciao.claude_trim import claude_settings_layer
from ciao.models import AgentRequest
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

CLAUDE = AgentSpec(
    id="claude",
    binary="claude-agent-acp",
    env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"},
    # The claude.ai connectors switch travels only in _meta.claudeCode.options.env (see below).
    unset_env=("ENABLE_CLAUDEAI_MCP_SERVERS",),
    client_meta={},
    modes={
        "plan": (ModeChoice("mode", ("plan",)),),
        "normal": (ModeChoice("mode", ("default",)),),
        "auto": (ModeChoice("mode", ("auto", "acceptEdits")),),
        "bypass": (ModeChoice("mode", ("bypassPermissions",)),),
    },
    model_config_id="model",
    thought_config_id="effort",
    slash_capsule_as_block=True,
)

# A one-shot call has no tools, no skills, no setting sources and no MCP servers.
CLAUDE_ONESHOT_OPTIONS: Mapping[str, Any] = MappingProxyType({
    "tools": [],
    "skills": [],
    "settingSources": [],
    "strictMcpConfig": True,
})

_CONNECTORS_ENV_KEY = "ENABLE_CLAUDEAI_MCP_SERVERS"
_CONNECTORS_OFF_ENV = {_CONNECTORS_ENV_KEY: "false"}


def claude_session_options(request: AgentRequest) -> dict[str, Any]:
    """The ``_meta.claudeCode.options`` dict for a chat: its disable options only.

    - ``disallowedTools``: the request's denies (trim tool and skill denies, then the
      workspace's extra denies, in that order).
    - ``settings``: the trim layer as an object. A string would be read by the adapter as a
      file path relative to the working directory.
    - ``env``: ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` only when the chat's connectors are off.

    An option with no value is left out, so an unchanged chat sends the same dict every turn.
    """
    options: dict[str, Any] = {}
    if request.disallowed_tools:
        options["disallowedTools"] = list(request.disallowed_tools)
    settings = (
        request.claude_settings
        if request.claude_settings is not None
        else claude_settings_layer({})
    )
    if settings:
        options["settings"] = dict(settings)
    if request.extra_env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false":
        options["env"] = dict(_CONNECTORS_OFF_ENV)
    return options


def _claude_cli_env() -> dict[str, str]:
    """Point the adapter at the user's own ``claude``, and its login, when one resolves."""
    from ciao.setup_status import claude_cli_path

    path = claude_cli_path()
    return {"CLAUDE_CODE_EXECUTABLE": path} if path else {}


class ClaudeAcpProvider(AcpProvider):
    spec = CLAUDE

    def session_meta(self, request: AgentRequest) -> dict[str, Any]:
        """Ciaobot's disable options for this chat, as ``_meta`` on session/new, load and resume."""
        return {"claudeCode": {"options": claude_session_options(request)}}

    @classmethod
    def oneshot_meta(cls) -> dict[str, Any]:
        return {"claudeCode": {"options": dict(CLAUDE_ONESHOT_OPTIONS)}}

    @classmethod
    def spawn_env(cls) -> dict[str, str]:
        """The user's own ``claude`` as ``CLAUDE_CODE_EXECUTABLE``, when one resolves.

        Credentials are not added here: the engine environment already reaches the adapter, and
        the CLI handles its own login.
        """
        return _claude_cli_env()

    @classmethod
    def process_env(cls, request: AgentRequest) -> dict[str, str]:
        # The connectors switch travels only in _meta.claudeCode.options.env. A process
        # env value would outlive a reload, since the connection is reused across it.
        env = super().process_env(request)
        env.pop(_CONNECTORS_ENV_KEY, None)
        return env

    @classmethod
    async def delete_session(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> bool | None:
        # The ACP journal and the CLI's own session file both go. Either one deleting counts.
        journal = await super().delete_session(workspace_root, session_id, runtime_root=runtime_root)
        try:
            # A git subprocess and file removal: run in a worker thread, not on the event loop.
            cli_file = await asyncio.to_thread(
                claude_sessions.delete_session_file, session_id, str(workspace_root)
            )
        except OSError:
            # Neither the id nor the OSError text is logged: the id comes from the adapter, and
            # the error text carries the path.
            logger.warning("Could not delete a Claude session file; it was left in place.")
            cli_file = False
        return True if (journal or cli_file) else None


def claude_auth_command(*, device_auth: bool = False) -> list[str]:
    """The interactive Claude Code login, as argv. ``device_auth`` has no Claude equivalent."""
    del device_auth
    from ciao.setup_status import claude_cli_path

    binary = claude_cli_path()
    if not binary:
        raise FileNotFoundError("Claude CLI not found")
    return [binary, "auth", "login"]


CODEX = AgentSpec(
    id="codex",
    binary="codex-acp",
    env={},  # codex-acp uses the codex it bundles; CODEX_PATH stays unset.
    client_meta={},  # No subagents opt-in: a Codex subagent is a plain tool call (plan Q6).
    modes={
        "plan": (
            ModeChoice("mode", ("read-only",)),
            ModeChoice("collaboration_mode", ("plan",)),
        ),
        "normal": (
            ModeChoice("mode", ("workspace-write",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
        "auto": (
            ModeChoice("mode", ("agent",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
        "bypass": (
            ModeChoice("mode", ("agent-full-access",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
    },
    model_config_id="model",
    thought_config_id="reasoning_effort",
)


class CodexAcpProvider(AcpProvider):
    spec = CODEX


def codex_binary() -> str | None:
    """The ``codex`` CLI on the terminal PATH, or ``None``. The one lookup Codex code uses."""
    return resolve_tool("codex")


def codex_acp_binary() -> str | None:
    """The ``codex-acp`` adapter on the terminal PATH, or ``None``."""
    return resolve_tool("codex-acp")


def codex_auth_command(*, device_auth: bool = False) -> list[str]:
    """``codex login`` with the resolved binary, for the Settings terminal.

    Raises ``FileNotFoundError`` when ``codex`` is not on the terminal PATH.
    """
    codex = codex_binary()
    if codex is None:
        raise FileNotFoundError("codex was not found on PATH.")
    argv = [codex, "login"]
    if device_auth:
        argv.append("--device-auth")
    return argv
