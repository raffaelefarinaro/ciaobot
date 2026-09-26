"""Tests for the Ciaobot.app → terminal-engine classifier (#576).

The classifier runs on the install path, on the machine that is being
migrated, before anything has been touched. So the two properties that matter
most are the ones a reader cannot see in the code: it decides host from client
from the raw state, and it never writes a byte while deciding.
"""

from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from ciao import engine_migration
from ciao.engine_migration import (
    Classification,
    _client_host_url,
    check_client_url,
    classify,
    main,
)

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


def test_existing_malformed_plist_is_invalid(tmp_path: Path) -> None:
    # A plist that is there and cannot be parsed is not "no engine": it is a
    # service definition whose program, workspace and state nobody can read, and
    # `none` would send the installer straight on to ordinary setup over the top
    # of it.
    for name, body in (
        ("not-a-plist", b"this is not a plist at all\n"),
        ("truncated", plistlib.dumps({"Label": "com.ciao.server"})[:40]),
    ):
        agents = tmp_path / name / "home/Library/LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.ciao.server.plist").write_bytes(body)

        result = classify(agents)

        assert result.kind == "desktop_invalid", name
        assert result.node_role == "invalid", name


def test_existing_plist_without_a_program_is_invalid(tmp_path: Path) -> None:
    # It parses, so it is not malformed - but it names no program, so there is no
    # engine to take over and no way to tell whose service this is. Reading it
    # as "nothing to migrate" would install over a job launchd may be running.
    agents = tmp_path / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps({"Label": "com.ciao.server"})
    )

    result = classify(agents)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"


def test_classify_defaults_to_the_users_own_launch_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The installer passes the directory explicitly, but the module is also run
    # by hand, and the default has to be the one the engine itself uses.
    agents, _ = _fixture(tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(agents))

    assert classify().kind == "desktop_host"


def test_missing_runtime_root_is_invalid(tmp_path: Path) -> None:
    # The engine is live and the runtime root it is supposed to own is not there.
    # Reading that as "no node file, therefore a host" is the guess that hands a
    # second writer to a runtime root nobody has looked at.
    agents, workspace = _fixture(tmp_path)
    (workspace / ".runtime").rmdir()

    result = classify(agents)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"
    # The workspace it did name is still reported: `--as-host` needs it.
    assert result.workspace == str(workspace)


def test_classify_is_invalid_without_a_workspace(tmp_path: Path) -> None:
    # No `CIAO_WORKSPACE` and no `WorkingDirectory` means the runtime root
    # resolves against whatever directory the classifier happened to run in,
    # which is not a fact about this Mac.
    agents = tmp_path / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.ciao.server",
                "ProgramArguments": [str(_app_program(tmp_path)), "run"],
            }
        )
    )

    result = classify(agents)

    assert result.kind == "desktop_invalid"


@pytest.mark.parametrize(
    "host_url",
    [
        "https://",
        "https:///app",
        "https://:8443",
        "not a url",
        "//mini.ts.net",
        "ftp://mini.ts.net",
        "http://exa mple.com",
        "https://user:secret@mini.ts.net",
        "https://mini.ts.net:99999",
    ],
)
def test_client_url_requires_host(tmp_path: Path, host_url: str) -> None:
    # A prefix check accepts every one of these, and a state file can hold any of
    # them. None of them is an address the user could open, so a client whose
    # state names one is not a client this migration can hand over to.
    agents, workspace = _fixture(tmp_path)
    _write_node_state(workspace, {"role": "standby", "host_url": host_url})

    result = classify(agents)

    assert result.kind == "desktop_invalid", host_url
    assert result.host_url == "", host_url


def test_unreadable_node_state_is_invalid(tmp_path: Path) -> None:
    # A node_state.json that cannot be read is not a host that has not written
    # one yet. It is the file that says which writer this Mac is, and a file
    # that cannot be read says nothing - which is a decision for the user, not
    # for this module.
    agents, workspace = _fixture(tmp_path)
    node = workspace / ".runtime" / "node_state.json"
    # A directory where the file is expected: unreadable for every user,
    # including root, so the case does not depend on who runs the tests.
    node.mkdir()

    result = classify(agents)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"
    assert not node.is_file()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root reads a file whatever its mode says",
)
def test_node_state_without_permission_is_invalid(tmp_path: Path) -> None:
    agents, workspace = _fixture(tmp_path)
    _write_node_state(workspace, {"role": "host"})
    node = workspace / ".runtime" / "node_state.json"
    node.chmod(0o000)
    try:
        result = classify(agents)
    finally:
        node.chmod(0o644)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"


def test_classify_never_raises_under_an_existing_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A surprise inside a real plist must not read as `none`: that answer skips
    # the migration path entirely and installs as if this Mac had nothing to
    # hand over. The plist is what decides, and when it is there the answer is
    # "undecidable".
    agents, _ = _fixture(tmp_path)

    def boom(*args: object, **kwargs: object) -> Classification:
        raise RuntimeError("the filesystem gave up")

    monkeypatch.setattr(engine_migration, "_classify", boom)

    result = classify(agents)

    assert result.kind == "desktop_invalid"
    assert result.node_role == "invalid"


def test_classify_never_raises_without_a_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no plist at all there is genuinely nothing to migrate, so the same
    # surprise degrades to `none` - the one answer that is right here.
    agents = tmp_path / "home/Library/LaunchAgents"
    agents.mkdir(parents=True)

    def boom(*args: object, **kwargs: object) -> Classification:
        raise RuntimeError("the filesystem gave up")

    monkeypatch.setattr(engine_migration, "_classify", boom)

    assert classify(agents).kind == "none"


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


def test_classify_stale_does_not_need_a_runtime_root(tmp_path: Path) -> None:
    # The bundle is gone, so nothing is running and nothing is being handed
    # over: whether the runtime root survived is not this migration's business,
    # and failing closed here would strand a user who deleted the app on purpose.
    agents, workspace = _fixture(
        tmp_path,
        program=str(tmp_path / "Applications" / "Ciaobot.app/Contents/MacOS/ciao"),
    )
    (workspace / ".runtime").rmdir()

    assert classify(agents).kind == "desktop_stale"


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


def test_main_readable_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The one-line form is for a person reading a terminal, so the kind comes
    # first and the workspace second - the two things anyone asks it for.
    agents, workspace = _fixture(tmp_path)

    assert main(["classify", "--launch-agents-dir", str(agents)]) == 0

    assert capsys.readouterr().out == f"kind=desktop_host workspace={workspace}\n"


@pytest.mark.parametrize(
    "url",
    [
        "https://mini.ts.net",
        "http://192.168.1.4:8443",
        "https://h.example/ciao",
        # Surrounding whitespace is what a copy-paste leaves behind, and the
        # trimmed value is the address.
        "  https://h.example  ",
    ],
)
def test_check_client_url_accepts_an_address(url: str) -> None:
    assert check_client_url(url) == url.strip()


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        # A prefix is not an address, and a prefix check accepts all of these.
        pytest.param("https://", id="scheme-only"),
        pytest.param("https:///ciao", id="empty-host"),
        pytest.param("https://:8443", id="port-without-host"),
        pytest.param("ftp://h.example", id="other-scheme"),
        # Whitespace inside the value cannot survive the one-line round trip
        # between this module and the shell that reads the answer back.
        pytest.param("https://h.example/a b", id="whitespace"),
        pytest.param("https://h.exa\nmple", id="newline"),
        # Credentials would be printed at the user and written into the receipt.
        pytest.param("https://user:secret@h.example", id="credentials"),
        pytest.param("https://[::1", id="unparseable"),
    ],
)
def test_check_client_url_refuses_what_is_not_one(url: str) -> None:
    assert check_client_url(url) == ""


def test_check_client_url_matches_the_state_file_rule(tmp_path: Path) -> None:
    # One rule, not two: whatever this accepts has to be exactly what a client's
    # `host_url` may be in a state file, or the same Mac would be a client with
    # `--as-client` and undecidable without it.
    agents, workspace = _fixture(tmp_path)

    for state_url, override_url in (
        ("https://mini.ts.net", "https://mini.ts.net"),
        ("https://", "https://"),
        ("https://user:secret@h.example", "https://user:secret@h.example"),
    ):
        _write_node_state(workspace, {"role": "standby", "host_url": state_url})
        assert check_client_url(override_url) == _client_host_url(state_url)


def test_main_check_client_url(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    # The installer branches on the exit status and prints the value, so the two
    # answers have to be told apart by the status rather than by parsing prose.
    assert main(["check-client-url", "https://mini.ts.net"]) == 0
    assert capsys.readouterr().out == "https://mini.ts.net\n"

    assert main(["check-client-url", "https://"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not an address" in captured.err
