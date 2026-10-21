"""The Memory handover setting is operator-only.

The agent holds only a scoped token for ``/agent/v1/{op}``; the setting lives in
the workspace registry under ``.runtime/`` and changes only through the
session-cookie ``PATCH /api/workspaces/{name}``. These tests pin that no agent
verb, operator CLI, MCP tool or control-plane op can write it.
"""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath

from ciao import agent_cli, cli
from ciao.execution_modes import CREDENTIAL_DENY_PATTERNS

_CIAO = Path(__file__).resolve().parents[1] / "ciao"


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def test_agent_surface_has_no_verb_that_writes_workspace_settings() -> None:
    parser = agent_cli.build_parser()
    nouns = _choices(parser)
    assert "workspace" in nouns
    assert set(_choices(nouns["workspace"])) == {"list"}


def test_no_operator_cli_sets_the_handover() -> None:
    commands = _choices(cli.build_parser())
    assert not [name for name in commands if "handover" in name]
    for relative in ("agent_cli.py", "control_plane.py", "mcp_server.py"):
        source = (_CIAO / relative).read_text(encoding="utf-8")
        assert "memory_handover" not in source, relative


def test_workspace_registry_is_under_the_runtime_deny_pattern() -> None:
    registry = PurePosixPath("/home/user/workspace/.runtime/workspaces.json")
    assert any(registry.match(pattern) for pattern in CREDENTIAL_DENY_PATTERNS)
