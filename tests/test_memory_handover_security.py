"""The Memory handover setting is operator-only.

The agent holds only a scoped token for ``/agent/v1/{op}``; the setting lives in
the workspace registry under ``.runtime/`` and changes only through the
session-cookie ``PATCH /api/workspaces/{name}``. These tests pin that no agent
verb, operator CLI, MCP tool or control-plane op can write it.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from pathlib import Path

from ciao import agent_cli, cli
from ciao import curation_run
from ciao.execution_modes import is_credential_adjacent

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


def test_workspace_registry_is_credential_adjacent() -> None:
    registry = Path("/home/user/workspace/.runtime/workspaces.json")
    assert is_credential_adjacent(registry)


def test_corrupt_state_blocks_handover_accept_and_dismiss_before_queue_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("- [memory] Keep this queued.\n", encoding="utf-8")
    state = curation_run.state_path(vault)
    state.write_text('{"version":', encoding="utf-8")
    queue_before = queue.read_bytes()
    state_before = state.read_bytes()
    registered = SimpleNamespace(memory_handover="all")
    monkeypatch.setattr(cli, "trusted_registry", lambda *_args: SimpleNamespace(
        workspace=lambda _name: registered
    ))
    monkeypatch.setattr(
        "ciao.proposal_predictions.handover_allows",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        "ciao.proposal_predictions.latest_predictions",
        lambda _queue: {"row": {"verdict": "accept"}},
    )
    monkeypatch.setattr(
        "ciao.proposal_predictions.prediction_key",
        lambda *_args: "row",
    )
    bullet = SimpleNamespace(kind="memory", decider="", text="Keep this queued.")

    for verdict in ("accept", "dismiss"):
        refusal = cli._handover_gate(vault, "work", queue, bullet, verdict)
        assert "curation state is uncertain" in refusal

    assert queue.read_bytes() == queue_before
    assert state.read_bytes() == state_before


def test_corrupt_state_blocks_real_handover_commands_without_receipt_or_guide_changes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from ciao.proposal_predictions import record_prediction

    for verdict, handover, command in (
        ("accept", "all", "memory-proposal-accept"),
        ("dismiss", "dismiss", "memory-proposal-dismiss"),
    ):
        root = tmp_path / verdict
        vault = root / "memory-vault" / "client-a"
        queue = vault / "Workspace" / "Memory-Proposals.md"
        queue.parent.mkdir(parents=True)
        (root / ".runtime").mkdir(parents=True)
        guide = root / "CLAUDE.md"
        guide.write_text("# Workspace\n\nKeep this guide unchanged.\n", encoding="utf-8")
        queue.write_text("- [memory] Keep this queued.\n", encoding="utf-8")
        state = curation_run.state_path(vault)
        state.write_text('{"version":', encoding="utf-8")
        (root / ".runtime" / "workspaces.json").write_text(
            json.dumps(
                {
                    "work": {
                        "name": "work",
                        "vault_root": str(vault),
                        "memory_handover": handover,
                    }
                }
            ),
            encoding="utf-8",
        )
        record_prediction(
            queue,
            kind="memory",
            text="Keep this queued.",
            verdict=verdict,
            reason="synthetic prediction",
        )
        before = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        monkeypatch.setattr(
            "ciao.workspace_discovery.discover_workspace", lambda _base: root
        )
        monkeypatch.setenv("CIAO_WORKSPACE", str(root))
        args = [
            command,
            "Keep this queued.",
            "--workspace",
            str(root),
            "--vault-root",
            str(vault),
        ]
        if verdict == "dismiss":
            args.append("--handover")

        assert cli.main(args) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "curation state is uncertain" in captured.err
        after = {
            path.relative_to(root): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }
        assert after == before
