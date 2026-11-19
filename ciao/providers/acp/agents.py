"""Claude Code over ACP: its spec, Ciaobot's Claude disable options, and its login command (#3138).

The ``claude`` provider runs ``claude-agent-acp``. Ciaobot's disable options (the trim layer,
the workspace's extra denies and the claude.ai connectors switch) reach the adapter as
``_meta.claudeCode.options``, which the adapter spreads into the SDK options. Everything else
the CLI owns, so nothing else is sent.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ciao.claude_trim import claude_settings_layer
from ciao.models import AgentRequest
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.spec import AgentSpec, ModeChoice

CLAUDE = AgentSpec(
    id="claude",
    binary="claude-agent-acp",
    env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"},
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
# Anthropic credentials the SDK path passed to the CLI. Named here, not matched by suffix.
_ANTHROPIC_CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")


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
        """The user's CLI path, and the Anthropic credentials the SDK path passed through.

        ``_terminal_env`` strips ``*_API_KEY`` and ``*_TOKEN`` from the engine's environment,
        so these three are added back by name, and nothing else is.
        """
        env = _claude_cli_env()
        for key in _ANTHROPIC_CREDENTIAL_KEYS:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

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
        # The ACP journal goes first; the CLI's own session file goes too, which the
        # SDK-era deleter still removes until C2a replaces it. Either one deleting counts.
        from ciao.providers.claude import ClaudeProvider

        journal = await super().delete_session(workspace_root, session_id, runtime_root=runtime_root)
        cli_file = await ClaudeProvider.delete_session(
            workspace_root, session_id, runtime_root=runtime_root
        )
        return True if (journal or cli_file) else None

    @classmethod
    async def native_title_candidates(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> tuple[str, ...]:
        # The CLI writes the session title to its own session file, which the SDK-era reader
        # still reads. C2a replaces that reader with one that needs no SDK.
        from ciao.providers.claude import ClaudeProvider

        return await ClaudeProvider.native_title_candidates(
            workspace_root, session_id, runtime_root=runtime_root
        )


def claude_auth_command(*, device_auth: bool = False) -> list[str]:
    """The interactive Claude Code login, as argv. ``device_auth`` has no Claude equivalent."""
    del device_auth
    from ciao.setup_status import claude_cli_path

    binary = claude_cli_path()
    if not binary:
        raise FileNotFoundError("Claude CLI not found")
    return [binary, "auth", "login"]
