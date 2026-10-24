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
# One install has a handful of workspaces; anything beyond this is stale roots
# from renames or moves that nobody will read again.
_MAX_ROOTS = 32
_cache: dict[Path, tuple[float, tuple[RuntimeModel, ...]]] = {}
_lock = threading.Lock()
# Bumped whenever the Claude account identity may change. A capture holds the
# generation it started under, so a connection that finishes after a logout
# cannot repopulate evidence for the account that just signed out.
_generation = 0


def account_generation() -> int:
    with _lock:
        return _generation


def _prune(now: float) -> None:
    for key in [k for k, (observed_at, _) in _cache.items() if now - observed_at >= _TTL_SECONDS]:
        _cache.pop(key, None)


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


def record_claude_models(
    root: Path, info: object, *, expected_generation: int | None = None
) -> None:
    key = root.resolve()
    parsed = parse_claude_models(info)
    with _lock:
        if expected_generation is not None and expected_generation != _generation:
            # The account changed while this connection was opening; its
            # metadata describes the previous one.
            return
        now = time.monotonic()
        _prune(now)
        if parsed is None:
            _cache.pop(key, None)
            return
        if key not in _cache and len(_cache) >= _MAX_ROOTS:
            oldest = min(_cache, key=lambda item: _cache[item][0])
            _cache.pop(oldest, None)
        _cache[key] = (now, parsed)


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


def invalidate_claude_account_evidence() -> None:
    """Drop every root and fence captures that started before this call.

    A chat connection already in flight may finish after a logout and try to
    record the previous account's models; bumping the generation before the
    awaited sign-out work makes those captures reject themselves.
    """
    global _generation
    with _lock:
        _generation += 1
        _cache.clear()
