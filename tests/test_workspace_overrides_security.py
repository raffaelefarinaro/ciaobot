"""Workspace provider overrides are operator-only (#1903).

An override can carry ``permission_mode: bypass``, so it is written only through
the session-cookie ``POST/PATCH /api/workspaces``. The agent holds a scoped token
for ``/agent/v1/{op}`` and the MCP surface; none of them may write the field.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ciao import agent_cli, cli

_CIAO = Path(__file__).resolve().parents[1] / "ciao"


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_agent_and_operator_surfaces_cannot_write_provider_overrides() -> None:
    nouns = _choices(agent_cli.build_parser())
    assert set(_choices(nouns["workspace"])) == {"list"}
    assert not [name for name in _choices(cli.build_parser()) if "override" in name]
    for relative in ("agent_cli.py", "control_plane.py", "mcp_server.py"):
        source = (_CIAO / relative).read_text(encoding="utf-8")
        assert "provider_overrides" not in source, relative
