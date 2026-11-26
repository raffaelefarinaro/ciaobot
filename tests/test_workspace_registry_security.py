"""The workspace registry is credential-adjacent, and no agent verb writes workspace settings."""

from __future__ import annotations

import argparse
from pathlib import Path

from ciao import agent_cli
from ciao.execution_modes import is_credential_adjacent


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


def test_workspace_registry_is_credential_adjacent() -> None:
    registry = Path("/home/user/workspace/.runtime/workspaces.json")
    assert is_credential_adjacent(registry)
