"""BaseProvider host-contract defaults and built-in overrides (#3201, slice S0).

The base defaults are inert (no work, nothing started, fresh containers per
call). Claude and OpenCode ``is_connected`` follow their own live-connection
state.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.providers.base import ActiveHandle, BaseProvider
from ciao.providers.claude import ClaudeProvider
from ciao.providers.opencode import OpencodeProvider


class _Minimal(BaseProvider):
    def run_streaming(
        self,
        request: Any,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[Any, None]:
        raise AssertionError("defaults must not start a turn")

    async def disconnect(self) -> None:
        raise AssertionError("defaults must not disconnect")


async def test_base_defaults_are_inert(tmp_path: Path) -> None:
    p = _Minimal(tmp_path)
    assert p.is_connected is False
    assert await _Minimal.read_thread(tmp_path, "s", runtime_root=Path("/runtime")) == {}
    assert await p.abort_session("s") is None


async def test_base_containers_are_fresh_per_call(tmp_path: Path) -> None:
    p = _Minimal(tmp_path)
    first = await _Minimal.read_thread(tmp_path, "s", runtime_root=Path("/runtime"))
    first["x"] = 1
    assert await _Minimal.read_thread(tmp_path, "s", runtime_root=Path("/runtime")) == {}


def test_is_connected_is_read_only(tmp_path: Path) -> None:
    with pytest.raises(AttributeError):
        setattr(_Minimal(tmp_path), "is_connected", True)


@pytest.mark.parametrize("client", [None, object()])
@pytest.mark.parametrize("connected", [True, False])
def test_claude_is_connected_follows_flag(
    tmp_path: Path, client: object | None, connected: bool
) -> None:
    p = ClaudeProvider(tmp_path)
    p._client = client
    p._connected = connected
    assert p.is_connected is connected


@pytest.mark.parametrize("returncode", [None, 0])
@pytest.mark.parametrize("has_client", [True, False])
@pytest.mark.parametrize("has_process", [True, False])
def test_opencode_is_connected_follows_its_server(
    tmp_path: Path, returncode: int | None, has_client: bool, has_process: bool
) -> None:
    p = OpencodeProvider(tmp_path)
    p._client = object() if has_client else None
    p._process = SimpleNamespace(returncode=returncode) if has_process else None
    assert p.is_connected is (has_client and has_process and returncode is None)


