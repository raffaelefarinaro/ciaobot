"""BaseProvider host-contract defaults and built-in overrides (#3201, slice S0).

The base defaults are inert (no work, nothing started, fresh containers per
call). Claude and OpenCode ``is_connected`` follow their own live-connection
state, OpenCode's ``interrupted_background_work`` is the shutdown payload, and
``live_for_session`` is the guard hosts use for a connected, session-bound
provider.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.providers.base import ActiveHandle, BaseProvider, live_for_session
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


class _Bound(_Minimal):
    def __init__(self, workspace_root: Path, connected: bool, session: str | None) -> None:
        super().__init__(workspace_root)
        self._c = connected
        self._s = session

    @property
    def is_connected(self) -> bool:
        return self._c

    @property
    def current_session_id(self) -> str | None:
        return self._s


async def test_base_defaults_are_inert(tmp_path: Path) -> None:
    p = _Minimal(tmp_path)
    assert p.is_connected is False
    assert await _Minimal.read_thread(tmp_path, "s") == {}
    assert await _Minimal.read_collab_tree(tmp_path, "s") == []
    assert await p.read_live_collab_tree() == []
    assert await p.read_live_collab_tree(include_messages=True, only_child_id="c") == []
    assert await p.refresh_background_shells() == 0
    assert p.background_count == 0
    assert p.background_shell_rows() == []
    assert p.background_child_ids() == []
    assert await p.abort_session("s") is None
    assert p.interrupted_background_work() is None


async def test_base_containers_are_fresh_per_call(tmp_path: Path) -> None:
    p = _Minimal(tmp_path)
    first = await _Minimal.read_thread(tmp_path, "s")
    first["x"] = 1
    assert await _Minimal.read_thread(tmp_path, "s") == {}
    rows = p.background_shell_rows()
    rows.append({"id": "a"})
    assert p.background_shell_rows() == []
    ids = p.background_child_ids()
    ids.append("a")
    assert p.background_child_ids() == []
    tree = await _Minimal.read_collab_tree(tmp_path, "s")
    tree.append({})
    assert await _Minimal.read_collab_tree(tmp_path, "s") == []


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


def test_opencode_interrupted_background_work_payload(tmp_path: Path) -> None:
    p = OpencodeProvider(tmp_path)
    assert p.interrupted_background_work() == {"children": [], "shells": []}
    p._background_children = {"ses_b", "ses_a"}
    p._background_shells = {"sh2", "sh1"}
    p.background_shell_rows = lambda: [
        {"id": "sh1", "command": "sleep 1", "started_at": 1},
        {"id": "sh2", "command": "sleep 2", "started_at": 2},
    ]
    assert p.interrupted_background_work() == {
        "children": ["ses_a", "ses_b"],
        "shells": [
            {"id": "sh1", "command": "sleep 1"},
            {"id": "sh2", "command": "sleep 2"},
        ],
    }


def test_live_for_session_truth_table(tmp_path: Path) -> None:
    assert live_for_session(object(), "s") is False
    assert live_for_session(SimpleNamespace(is_connected=True, current_session_id="s"), "s") is False
    assert live_for_session(_Bound(tmp_path, False, "s"), "s") is False
    assert live_for_session(_Bound(tmp_path, True, "other"), "s") is False
    assert live_for_session(_Bound(tmp_path, True, None), "s") is False
    assert live_for_session(_Bound(tmp_path, True, "s"), None) is False
    assert live_for_session(_Bound(tmp_path, True, None), None) is True
    assert live_for_session(_Bound(tmp_path, True, "s"), "s") is True
