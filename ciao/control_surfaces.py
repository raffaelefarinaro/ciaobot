"""Persistence and resolution of measured agent control-surface decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DECISION_FILENAME = "control_surface_decision.json"


def decision_path(config_or_workspace: Any) -> Path:
    state_path = getattr(config_or_workspace, "state_path", None)
    if state_path is not None:
        return Path(state_path).parent / DECISION_FILENAME
    return Path(config_or_workspace).expanduser().resolve() / ".runtime" / DECISION_FILENAME


def load_decision(config_or_workspace: Any) -> dict[str, Any]:
    path = decision_path(config_or_workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_auto_surface(config: Any, provider: str) -> str:
    """Return a promoted provider winner, failing safely to legacy."""
    payload = load_decision(config)
    providers = payload.get("providers")
    record = providers.get(provider) if isinstance(providers, dict) else None
    winner = record.get("winner") if isinstance(record, dict) else None
    return winner if winner in {"legacy", "mcp"} else "legacy"


def write_decision(workspace: Path, payload: dict[str, Any]) -> Path:
    path = decision_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path
