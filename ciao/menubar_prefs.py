"""Persisted preferences for the macOS menu bar companion."""

from __future__ import annotations

import json
from pathlib import Path

_DEFAULTS = {"notifications_enabled": True}


def prefs_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "menubar_prefs.json"


def read_prefs(workspace: Path) -> dict[str, object]:
    path = prefs_path(workspace)
    if not path.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        return dict(_DEFAULTS)
    merged: dict[str, object] = dict(_DEFAULTS)
    merged.update(data)
    return merged


def notifications_enabled(workspace: Path) -> bool:
    return bool(read_prefs(workspace).get("notifications_enabled", True))


def set_notifications_enabled(workspace: Path, enabled: bool) -> None:
    path = prefs_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_prefs(workspace)
    data["notifications_enabled"] = bool(enabled)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
