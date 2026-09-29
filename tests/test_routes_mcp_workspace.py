from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao.web.routes_mcp import _workspace_of, _write_workspace_of


def _request(workspace: str, registered: set[str]) -> SimpleNamespace:
    config = SimpleNamespace(workspace=lambda name: object() if name in registered else None)
    return SimpleNamespace(
        query_params={"workspace": workspace} if workspace else {},
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
    )


def test_a_read_accepts_an_unregistered_workspace() -> None:
    assert _workspace_of(_request("ghost", {"work"})) == "ghost"


def test_a_write_refuses_an_unregistered_workspace() -> None:
    with pytest.raises(ValueError, match="unknown workspace 'ghost'"):
        _write_workspace_of(_request("ghost", {"work"}))


def test_a_write_accepts_a_registered_or_absent_workspace() -> None:
    assert _write_workspace_of(_request("work", {"work"})) == "work"
    assert _write_workspace_of(_request("", {"work"})) == ""
