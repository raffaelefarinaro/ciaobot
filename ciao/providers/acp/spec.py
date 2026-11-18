"""Per-agent data for the ACP client layer.

Everything that differs between ACP agents (binary, argv, env, capability
opt-ins, how Ciaobot's modes map onto the agent's config options) is data on an
``AgentSpec``. No code outside this data names an agent class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ciao.models import BridgeMode


@dataclass(frozen=True, slots=True)
class ModeChoice:
    """One config option the agent must hold for a Ciaobot mode, with preferred values in order."""

    config_id: str  # e.g. "mode", "collaboration_mode"
    values: tuple[str, ...]  # first value the agent currently offers wins


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str  # provider id: "claude" | "codex" | "opencode"
    binary: str  # resolved with ciao.tool_path.resolve_tool at spawn
    args: tuple[str, ...] = ()  # e.g. ("acp",) for opencode
    env: Mapping[str, str] = field(default_factory=dict)  # fixed extra env for the agent
    client_meta: Mapping[str, Any] = field(default_factory=dict)  # clientCapabilities._meta opt-ins
    modes: Mapping[BridgeMode, tuple[ModeChoice, ...]] = field(default_factory=dict)
    model_config_id: str = "model"
    thought_config_id: str = ""  # "" = no thinking control
    quiet_period_s: float = 5.0  # late-turn close after this much silence
    initialize_timeout_s: float = 30.0
