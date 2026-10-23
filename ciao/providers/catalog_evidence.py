"""Process-local, bounded evidence from Claude SDK initialize responses."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RuntimeModel:
    id: str
    label: str


@dataclass(frozen=True)
class CatalogEvidence:
    status: Literal["observed", "unknown"]
    source: Literal["claude_initialize", "opencode_catalog"] | None
    models: tuple[RuntimeModel, ...]
    reason: Literal["not_observed", "expired", "invalid_response", "no_models_reported"] | None


_TTL_SECONDS = 300.0
_cache: dict[Path, tuple[float, tuple[RuntimeModel, ...]]] = {}
_lock = threading.Lock()


def parse_claude_models(info: object) -> tuple[RuntimeModel, ...] | None:
    if not isinstance(info, dict) or "models" not in info:
        return None
    rows = info["models"]
    if not isinstance(rows, list) or len(rows) > 256:
        return None
    result: list[RuntimeModel] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        model_id, label = row.get("value"), row.get("displayName")
        if not isinstance(model_id, str) or not isinstance(label, str):
            return None
        if not model_id or not label or len(model_id) > 256 or len(label) > 256:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in model_id + label):
            return None
        if model_id not in seen:
            seen.add(model_id)
            result.append(RuntimeModel(model_id, label))
    return tuple(result)


def record_claude_models(root: Path, info: object) -> None:
    key = root.resolve()
    parsed = parse_claude_models(info)
    with _lock:
        if parsed is None:
            _cache.pop(key, None)
        else:
            _cache[key] = (time.monotonic(), parsed)


def claude_catalog_evidence(root: Path) -> CatalogEvidence:
    key = root.resolve()
    with _lock:
        item = _cache.get(key)
        if item is None:
            return CatalogEvidence("unknown", None, (), "not_observed")
        observed_at, models = item
        if time.monotonic() - observed_at >= _TTL_SECONDS:
            _cache.pop(key, None)
            return CatalogEvidence("unknown", None, (), "expired")
    if not models:
        return CatalogEvidence("unknown", "claude_initialize", (), "no_models_reported")
    return CatalogEvidence("observed", "claude_initialize", models, None)


def invalidate_claude_catalog(root: Path | None = None) -> None:
    with _lock:
        if root is None:
            _cache.clear()
        else:
            _cache.pop(root.resolve(), None)
