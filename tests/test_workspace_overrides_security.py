"""Workspace provider overrides are operator-only (#1903).

An override can carry ``permission_mode: bypass``, so it is written only through
the session-cookie ``POST/PATCH /api/workspaces``. The agent holds a scoped token
for ``/agent/v1/{op}`` and the MCP surface; none of them may write the field.
"""

from __future__ import annotations

import argparse
import inspect

from ciao import agent_cli, cli
from ciao.control_plane import CiaoControlPlane

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


def test_every_mutating_workspace_method_leaves_overrides_alone(tmp_path) -> None:
    """No agent-reachable ControlPlane method can change stored overrides."""
    plane, manager, principal, project = _agent_world(tmp_path, "normal")
    before = {"claude": {"permission_mode": "bypass"}}
    work = plane.config.workspace("work")
    payload = {"provider_overrides": {"claude": {"permission_mode": "bypass", "model": "opus"}}}
    mutators = [
        n for n in dir(CiaoControlPlane)
        if n.startswith(("workspace_", "task_", "schedule_", "project_", "chat_"))
        and not n.startswith("_")
    ]
    assert mutators
    for name in mutators:
        method = getattr(plane, name)
        for args in ((principal,), ("work",), (principal, "x"), ("work", "x")):
            try:
                result = method(*args, **payload)
                if inspect.isawaitable(result):
                    result.close()
            except Exception:  # noqa: BLE001 - only the stored overrides matter
                pass
    assert work.provider_overrides == before
    assert plane.config.workspace("personal").provider_overrides == {}
