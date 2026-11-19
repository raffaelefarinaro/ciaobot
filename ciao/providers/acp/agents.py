"""Real ACP agent specs and the provider classes that run them (#3139).

Every agent Ciaobot runs over ACP has one ``AgentSpec`` here and one ``AcpProvider`` subclass that
sets it. Login stays in the CLI: ``codex_auth_command`` only builds the command the Settings
terminal runs, and Ciaobot never reads or stores the credentials it writes.
"""

from __future__ import annotations

from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.tool_path import resolve_tool

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
