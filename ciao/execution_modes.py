"""Bridge-visible execution modes and provider-specific mappings."""

from __future__ import annotations

from dataclasses import dataclass

from ciao.models import BridgeMode


@dataclass(frozen=True, slots=True)
class ModeOption:
    """One selectable execution mode."""

    value: BridgeMode
    label: str
    description: str


MODE_OPTIONS: list[ModeOption] = [
    ModeOption("normal", "Normal", "Standard interactive behavior"),
    ModeOption("plan", "Plan", "Planning or read-only behavior"),
    ModeOption("auto", "Auto", "Automatic execution with safer defaults"),
    ModeOption("bypass", "Bypass", "Skip approvals with broadest access"),
]


def normalize_claude_mode(raw: str) -> BridgeMode:
    """Map Claude CLI permission values into bridge modes."""
    value = raw.strip()
    if value == "plan":
        return "plan"
    if value == "auto":
        return "auto"
    if value == "bypassPermissions":
        return "bypass"
    return "normal"
