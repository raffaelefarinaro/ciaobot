"""Workspace provider overrides are operator-only (#1903).

An override can carry ``permission_mode: bypass``, so it is written only through
the session-cookie ``POST/PATCH /api/workspaces``. The agent holds a scoped token
for ``/agent/v1/{op}`` and the MCP surface; none of them may write the field.
"""

from __future__ import annotations

import argparse
import inspect

import pytest

from ciao import agent_cli, cli
from ciao.control_plane import CiaoControlPlane, ControlPlaneError

from tests.test_workspace_overrides import _agent_world


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_agent_and_operator_surfaces_cannot_write_provider_overrides() -> None:
    nouns = _choices(agent_cli.build_parser())
    assert set(_choices(nouns["workspace"])) == {"list"}
    assert not [name for name in _choices(cli.build_parser()) if "override" in name]


def test_no_control_plane_method_can_write_provider_overrides(tmp_path) -> None:
    """Structural: no public method declares the field. Behavioural: the three
    open-ended schedule methods accept it as a stray kwarg and still leave the
    workspace registry untouched, byte for byte."""
    plane, _manager, principal, project = _agent_world(tmp_path, "normal")

    for name, method in inspect.getmembers(CiaoControlPlane, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert "provider_overrides" not in inspect.signature(method).parameters, name

    registry = tmp_path / ".runtime" / "workspaces.json"
    plane.config.persist_workspace_registry()
    registry_before = registry.read_bytes()
    overrides_before = plane.config.workspace("work").provider_overrides
    payload = {"provider_overrides": {"claude": {"permission_mode": "bypass", "model": "opus"}}}

    created = plane.schedule_preview(
        principal, prompt="p", daily_time="09:00", frequency="weekly", **payload
    )
    assert created["ok"] is True
    made = plane.schedule_create(
        principal, prompt="p", daily_time="09:00", timezone="UTC", frequency="weekly", **payload
    )["data"]
    # Update names its fields and refuses the stray one outright.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.schedule_update(principal, made["schedule_id"], enabled=False, **payload)
    assert excinfo.value.code == "invalid_fields"

    assert plane.config.workspace("work").provider_overrides == overrides_before
    assert plane.config.workspace("personal").provider_overrides == {}
    assert registry.read_bytes() == registry_before
