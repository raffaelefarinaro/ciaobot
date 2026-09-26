"""Tests for the Ciaobot.app → terminal-engine classifier (#576).

The classifier runs on the install path, on the machine that is being
migrated, before anything has been touched. So the two properties that matter
most are the ones a reader cannot see in the code: it decides host from client
from the raw state, and it never writes a byte while deciding.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from ciao import engine_migration
from ciao.engine_migration import classify, main

PORT = 9555


def _fixture(
    tmp_path: Path,
    *,
    program: str | None = None,
    node_state: object = ...,
    write_plist: bool = True,
) -> tuple[Path, Path]:
    """A LaunchAgents dir, a workspace with `.env`, and a plist pointing into
    a (fake) Ciaobot.app by default."""
    agents = tmp_path / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    workspace = tmp_path / "workspace"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / ".env").write_text(f"PWA_PORT={PORT}\n", encoding="utf-8")
    if program is None:
        program = str(_app_program(tmp_path))
    if write_plist:
        (agents / "com.ciao.server.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.ciao.server",
                    "ProgramArguments": [program, "run"],
                    "EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)},
                    "WorkingDirectory": str(workspace),
                }
            )
        )
    return agents, workspace


def _app_program(tmp_path: Path) -> Path:
    """A real executable inside a fake Ciaobot.app, since `desktop_stale` is
    exactly the case where the path is not there."""
    program = (
        tmp_path
        / "Ciaobot.app"
        / "Contents"
        / "Resources"
        / "ciao-runtime"
        / "bin"
        / "ciao"
    )
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    return program


def _write_node_state(workspace: Path, state: object) -> None:
    text = state if isinstance(state, str) else json.dumps(state)
    (workspace / ".runtime" / "node_state.json").write_text(text, encoding="utf-8")


def test_classify_none_without_plist(tmp_path: Path) -> None:
    agents, _ = _fixture(tmp_path, write_plist=False)

    result = classify(agents)

    assert result.kind == "none"


def test_classify_engine_for_non_app_program(tmp_path: Path) -> None:
    # An engine this installer (or `ciao setup`) already placed is not a desktop
    # install: there is nothing to migrate, and treating it as one would take a
    # working install through a pointless hand-off.
    agents, _ = _fixture(tmp_path, program=str(tmp_path / ".local" / "bin" / "ciao"))

    result = classify(agents)

    assert result.kind == "engine"
    assert result.plist_program.endswith(".local/bin/ciao")


def test_classify_desktop_host_when_node_state_absent(tmp_path: Path) -> None:
    # No node state at all is the host path: the desktop shell writes one as
    # soon as it knows, and its absence means nobody else is using this Mac's
    # runtime root.
    agents, _ = _fixture(tmp_path)

    result = classify(agents)

    assert result.kind == "desktop_host"
    assert result.node_role == ""
    assert result.app_bundle.endswith("Ciaobot.app")
    assert result.port == PORT


def test_classify_desktop_host_for_host_role(tmp_path: Path) -> None:
    for role in ("host", "active"):
        agents, workspace = _fixture(tmp_path / role)
        _write_node_state(workspace, {"role": role})

        result = classify(agents)

        assert result.kind == "desktop_host", role
        assert result.node_role == "host", role


def test_classify_desktop_client_with_host_url(tmp_path: Path) -> None:
    agents, workspace = _fixture(tmp_path)
    _write_node_state(workspace, {"role": "standby", "host_url": "https://mini.ts.net"})

    result = classify(agents)

    assert result.kind == "desktop_client"
    assert result.node_role == "client"
    assert result.host_url == "https://mini.ts.net"


@pytest.mark.parametrize(
    "state",
    [
        "{not json at all",
        {"role": "weird"},
        {"role": "client"},
        [{"role": "host"}],
    ],
    ids=["unparseable", "unknown-role", "client-without-url", "json-list"],
)
def test_classify_invalid_cases(tmp_path: Path, state: object) -> None:
    # Every one of these is a Mac whose runtime root nobody can account for.
    # Guessing "host" would create a second writer; guessing "client" would
    # strand the user with no engine anywhere. So all of them fail closed.
    agents, workspace = _fixture(tmp_path)
    _write_node_state(workspace, state)

    result = classify(agents)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"


def test_classify_stale_when_app_program_missing(tmp_path: Path) -> None:
    # Ciaobot.app was deleted and left its plist behind. There is no live app to
    # take the engine from, so this is an ordinary install, not a refusal.
    agents, _ = _fixture(
        tmp_path,
        program=str(tmp_path / "Applications" / "Ciaobot.app" / "Contents" / "MacOS" / "ciao"),
    )

    result = classify(agents)

    assert result.kind == "desktop_stale"
    assert result.app_bundle.endswith("Ciaobot.app")


def test_classify_never_writes(tmp_path: Path) -> None:
    # NodeStateManager *creates* a host state when the file is missing, so a
    # classifier that went through it would silently turn "no state" into "this
    # Mac is a host" on disk. That is the one write this must never make, and
    # the assertion is on the filesystem rather than on the return value.
    agents, workspace = _fixture(tmp_path)
    before = sorted(p.name for p in workspace.rglob("*"))
    node = workspace / ".runtime" / "node_state.json"

    assert classify(agents).kind == "desktop_host"

    assert sorted(p.name for p in workspace.rglob("*")) == before
    assert not node.exists()

    # And with a state present, the file is byte-identical afterwards.
    _write_node_state(workspace, {"role": "host", "host_url": ""})
    raw = node.read_bytes()
    before = sorted(p.name for p in workspace.rglob("*"))

    assert classify(agents).kind == "desktop_host"

    assert node.read_bytes() == raw
    assert sorted(p.name for p in workspace.rglob("*")) == before


def test_main_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agents, _ = _fixture(tmp_path)

    assert main(["classify", "--json", "--launch-agents-dir", str(agents)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "desktop_host"
    assert payload["port"] == PORT
    assert set(payload) == set(engine_migration.asdict(engine_migration.classify(agents)))
