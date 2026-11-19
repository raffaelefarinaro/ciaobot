"""BaseProvider host-contract defaults and built-in overrides (#3201, slice S0).

The base defaults are inert (no work, nothing started, fresh containers per
call). OpenCode ``is_connected`` follows its own live-connection state.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest

from ciao.providers.base import ActiveHandle, BaseProvider


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


