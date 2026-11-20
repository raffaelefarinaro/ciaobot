"""Per-agent data for the ACP client layer.

Everything that differs between ACP agents (binary, argv, env, capability
opt-ins, how Ciaobot's modes map onto the agent's config options) is data on an
``AgentSpec``. No code outside this data names an agent class.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ciao.models import BridgeMode


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """One config option the agent must hold for a Ciaobot mode, with preferred values in order."""

    config_id: str  # e.g. "mode", "collaboration_mode"
    values: tuple[str, ...]  # first value the agent currently offers wins


@dataclass(frozen=True, slots=True)
class VersionGate:
    """Refuses an agent whose version is below the floor, before any session opens.

    ``supported`` judges the version text the agent reports (in ``agentInfo``, or from
    ``probe`` when it reports none). ``refusal`` is the whole user-facing text of a refusal:
    the adapter's own version text never reaches the PWA.
    """

    supported: Callable[[str], bool]
    refusal: str
    probe: Callable[[str], str]  # binary path -> version text, "" when unreadable


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str  # provider id: "claude" | "codex" | "opencode"
    binary: str  # resolved with ciao.tool_path.resolve_tool at spawn
    args: tuple[str, ...] = ()  # e.g. ("acp",) for opencode
    # Fixed extra env for the agent, layered over the engine env. The engine env is the
    # process environment minus ``CIAO_*`` and the PWA token; provider credentials pass
    # through, and the CLI decides what it uses. The ``open(env=...)`` argument overrides this.
    env: Mapping[str, str] = field(default_factory=dict)
    # Variables kept out of the inherited engine env for this agent (for example a switch that
    # must travel only in the session's ``_meta``, not in the process env).
    unset_env: tuple[str, ...] = ()
    client_meta: Mapping[str, Any] = field(default_factory=dict)  # clientCapabilities._meta opt-ins
    # The agent resumes the session on its own when a background shell it started ends, with a
    # reply that carries no prompt from Ciaobot (captured for OpenCode and Claude). Ciaobot then
    # records that reply as the shell's one turn instead of sending its own wake.
    continues_after_shell: bool = False
    # Extension notifications the agent sends WITHOUT the leading underscore that ACP reserves
    # for extensions. The SDK routes only ``_``-prefixed methods to ``ext_notification``, so each
    # name listed here is routed there under the name as sent (see ``connection.connect_client``).
    extension_notifications: tuple[str, ...] = ()
    modes: Mapping[BridgeMode, tuple[ModeChoice, ...]] = field(default_factory=dict)
    model_config_id: str = "model"
    thought_config_id: str = ""  # "" = no thinking control
    # A native slash command is sent as its own text block, with the context capsule
    # as a second block. For an agent whose command parser keeps only the first text
    # block (claude-agent-acp), a capsule in front of the command stops it being a command.
    slash_capsule_as_block: bool = False
    quiet_period_s: float = 5.0  # late-turn close after this much silence
    initialize_timeout_s: float = 30.0
    # None: any version starts. Otherwise the start is refused before a session opens.
    version_gate: VersionGate | None = None
