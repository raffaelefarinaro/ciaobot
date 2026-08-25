"""Failed ephemeral opencode spawns must be negative-cached.

The classmethod read paths (`read_thread`, `read_collab_tree`) back PWA
polls: `/subagents` every ~15s (4s while a turn streams) plus transcript
replay. When `opencode serve` cannot start at all, `_EphemeralServer` waits
out the full health deadline per call — so an uncached failure meant one
doomed server spawn per poll, stacking dying processes while opencode was
broken (observed as two serve processes spinning at ~155% CPU). Failures
now cache briefly longer than successful reads; this pins that behavior.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.providers.opencode import (
    _COLLAB_CACHE,
    _READ_CACHE_TTL,
    _READ_FAILURE_CACHE_TTL,
    _THREAD_CACHE,
    OpencodeProvider,
)


class _DeadEphemeralServer:
    """`_EphemeralServer` stand-in whose server never becomes healthy."""

    spawns = 0

    def __init__(self, workspace_root: Path) -> None:
        _DeadEphemeralServer.spawns += 1

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeClient:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def get(self, path: str) -> Any:
        # /children returns the child list; per-child message reads return
        # none — one payload shape per endpoint, like the real server.
        body = self._payload if path.endswith("/children") else []
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: body,
        )
        return response


class _HealthyEphemeralServer:
    """`_EphemeralServer` stand-in answering every read from one payload."""

    spawns = 0

    def __init__(self, payload: Any) -> None:
        _HealthyEphemeralServer.spawns += 1
        self._client = _FakeClient(payload)

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    _THREAD_CACHE.clear()
    _COLLAB_CACHE.clear()
    yield


def test_read_failure_ttl_exceeds_success_ttl() -> None:
    """A failed spawn is worth remembering longer than a successful read."""
    assert _READ_FAILURE_CACHE_TTL > _READ_CACHE_TTL


def test_failed_spawn_is_negative_cached_for_collab_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _DeadEphemeralServer.spawns = 0
    monkeypatch.setattr(
        "ciao.providers.opencode._EphemeralServer", _DeadEphemeralServer
    )

    first = asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses1"))
    second = asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses1"))

    assert first == []
    assert second == []
    # One doomed spawn for the burst of polls, not one per poll.
    assert _DeadEphemeralServer.spawns == 1


def test_failed_spawn_is_negative_cached_for_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _DeadEphemeralServer.spawns = 0
    monkeypatch.setattr(
        "ciao.providers.opencode._EphemeralServer", _DeadEphemeralServer
    )

    first = asyncio.run(OpencodeProvider.read_thread(tmp_path, "ses1"))
    second = asyncio.run(OpencodeProvider.read_thread(tmp_path, "ses1"))

    assert first == {}
    assert second == {}
    assert _DeadEphemeralServer.spawns == 1


def test_successful_collab_read_still_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative cache must not change the success-path hit behavior."""
    payload = [{"id": "ses_child", "parentID": "ses_parent"}]
    _HealthyEphemeralServer.spawns = 0
    monkeypatch.setattr(
        "ciao.providers.opencode._EphemeralServer",
        lambda root: _HealthyEphemeralServer(payload),
    )

    first = asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses_parent"))
    second = asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses_parent"))

    assert first == [{"info": payload[0], "messages": []}]
    assert second == first
    assert _HealthyEphemeralServer.spawns == 1


def test_failure_cache_expires_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the TTL passes, reads try again — and find opencode healthy."""
    _DeadEphemeralServer.spawns = 0
    monkeypatch.setattr(
        "ciao.providers.opencode._EphemeralServer", _DeadEphemeralServer
    )
    asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses1"))
    assert _COLLAB_CACHE[(str(tmp_path), "ses1")][1] == _READ_FAILURE_CACHE_TTL

    # Age the entry past its own TTL, then let opencode work again.
    stamp, _ttl, value = _COLLAB_CACHE[(str(tmp_path), "ses1")]
    _COLLAB_CACHE[(str(tmp_path), "ses1")] = (
        stamp - _READ_FAILURE_CACHE_TTL - 1,
        _READ_FAILURE_CACHE_TTL,
        value,
    )
    payload: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "ciao.providers.opencode._EphemeralServer",
        lambda root: _HealthyEphemeralServer(payload),
    )
    result = asyncio.run(OpencodeProvider.read_collab_tree(tmp_path, "ses1"))

    assert result == []
    stamp2, ttl2, _value2 = _COLLAB_CACHE[(str(tmp_path), "ses1")]
    assert ttl2 == _READ_CACHE_TTL
