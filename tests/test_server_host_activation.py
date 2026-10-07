"""E2 activation: setup and registration select the verified native host.

Part of #1008 (child E2). B2 taught the renderer to build a hosted plist from a
:class:`ciao.server_host.HostOwnership` snapshot; this file pins the wiring that
finally feeds it one. The rules under test are the whole of E2:

* only a snapshot returned by ``verify_owned_host`` selects the hosted service,
  and the plist is the exact ``host_service_argv`` plus ``ExitTimeOut`` 45;
* the served interpreter is the engine the direct path already uses, never the
  ``ciao`` console the release installer passes as ``--python``;
* an absent, foreign or unverified host falls back to the direct shape and
  never silently claims a host;
* an already-hosted definition this workspace owns is preserved — but only
  while the host executable it names still exists, so a dead definition is
  rewritten to the working direct shape instead of being kept forever;
* the render-time ``ValueError`` surfaces as a ``RuntimeError`` message, never a
  traceback, because the setup callers already catch it.

No live launchctl, ``open``, AX, TCC or ``~/Applications`` is touched: the
verified snapshot is injected, and every path is a pytest tmpdir.
"""

from __future__ import annotations

import plistlib
import shutil
import sys
from pathlib import Path, PurePosixPath

import pytest

from ciao import cli
from ciao.server_host import (
    BUNDLE_ID,
    EXIT_TIMEOUT_SECONDS,
    HOST_PROTOCOL,
    HOST_REVISION,
    REQUIRED_BUNDLE_FILES,
    SCHEMA_VERSION,
    ServerHostError,
    HostOwnership,
    host_service_argv,
)


def _host_bundle(tmp_path: Path, *, name: str = "Host") -> Path:
    """A ``Ciaobot Server.app`` whose executable is a real regular file.

    The E2 preservation guard only cares that the argv[0] the hosted definition
    names exists; the bundle itself is never inspected without a record. A
    scratch directory is enough, so no live ``~/Applications`` is touched.
    """
    bundle = tmp_path / name / "Ciaobot Server.app"
    executable = bundle / "Contents" / "MacOS" / "CiaobotServerHost"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    return bundle


def _verified_host(bundle_path: str | None = None) -> HostOwnership:
    """A snapshot as ``verify_owned_host`` would return it (B1's own shape)."""
    digest = "a" * 64
    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=bundle_path or "/Users/me/Applications/Ciaobot Server.app",
        bundle_id=BUNDLE_ID,
        host_revision=HOST_REVISION,
        host_protocol=HOST_PROTOCOL,
        executable_sha256=digest,
        per_arch_cdhashes={"arm64": "b" * 40, "x86_64": "c" * 40},
        bundle_files={name: digest for name in sorted(REQUIRED_BUNDLE_FILES)},
    )


#: A POSIX engine interpreter, so the hosted argv renders on every runner.
_ENGINE_PYTHON = "/opt/ciaobot/tool/bin/python"

#: Tests that build a macOS-shaped ``Ciaobot Server.app`` under ``tmp_path`` and
#: render its hosted argv. On Windows ``tmp_path`` is a backslash path, which
#: ``host_service_argv`` refuses by design (the same string names a different
#: file on POSIX and Windows), so these model a macOS launchd definition that
#: cannot exist on Windows and are skipped there. The platform-neutral
#: recognition, direct-shape and refusal tests keep running everywhere.
macos_host_shape_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="builds a macOS-shaped host bundle and renders its hosted argv",
)


@pytest.fixture(autouse=True)
def _darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verified host is a macOS fact; force the branch everywhere."""
    monkeypatch.setattr(sys, "platform", "darwin")


def _load_plist(path: Path) -> dict:
    with path.open("rb") as handle:
        return plistlib.load(handle)


def _hosted_expected(host: HostOwnership, python: str) -> list[str]:
    return list(host_service_argv(PurePosixPath(host.bundle_path), PurePosixPath(python)))


def test_verified_host_helper_verifies_the_installer_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default bundle — the E1 installer's target — is the one verified."""
    from ciao.server_host import default_bundle_path

    seen: list[Path] = []

    def _capture(bundle: Path, **_: object) -> HostOwnership:
        seen.append(bundle)
        raise ServerHostError("no record", code="missing_record")

    monkeypatch.setattr(cli, "verify_owned_host", _capture)

    assert cli._verified_host_if_installed() is None
    assert seen == [default_bundle_path()]


def test_setup_with_verified_host_renders_host_argv_and_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified host + the installer's ``--python $ciao`` renders the host
    serving the *engine interpreter*, with ExitTimeOut 45."""
    host = _verified_host()
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: host)
    # The engine interpreter behind the console; pinned so a Windows runner's
    # `C:\...\python.exe` is not what the host is asked to serve.
    monkeypatch.setattr(sys, "executable", _ENGINE_PYTHON)
    agents = tmp_path / "LaunchAgents"

    # The release installer passes `--python "$ciao"` (the console entry point).
    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path="/opt/ciaobot/bin/ciao",
    )

    data = _load_plist(agents / "com.ciao.server.plist")
    assert data["ProgramArguments"] == _hosted_expected(host, _ENGINE_PYTHON)
    assert data["ProgramArguments"][0].endswith("CiaobotServerHost")
    assert data["ProgramArguments"][3] == _ENGINE_PYTHON
    assert data["ProgramArguments"][3] != "/opt/ciaobot/bin/ciao"
    assert data["ExitTimeOut"] == EXIT_TIMEOUT_SECONDS == 45
    # The workspace facts a hosted definition still needs survive.
    assert data["EnvironmentVariables"]["CIAO_WORKSPACE"] == str(
        (tmp_path / "ws").resolve()
    )


def test_setup_with_verified_host_and_explicit_python_serves_that_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit python interpreter is served as given, not replaced."""
    host = _verified_host()
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: host)
    agents = tmp_path / "LaunchAgents"

    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path="/opt/ciaobot/venv/bin/python3.12",
    )

    data = _load_plist(agents / "com.ciao.server.plist")
    assert data["ProgramArguments"] == _hosted_expected(
        host, "/opt/ciaobot/venv/bin/python3.12"
    )
    assert data["ExitTimeOut"] == 45


@macos_host_shape_only
def test_setup_rerun_on_a_live_hosted_workspace_stays_hosted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted definition this workspace owns whose host still exists is a
    faithful no-op, not a silent downgrade, when no host is selected this run."""
    agents = tmp_path / "LaunchAgents"
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(parents=True)
    agents.mkdir(parents=True)
    bundle = _host_bundle(tmp_path)

    # An existing hosted definition for this workspace, written the way the
    # renderer writes one, naming a host executable that is really there.
    cli._write_launchd_plist(
        workspace=workspace,
        launch_agents_dir=agents,
        port=8443,
        host=_verified_host(str(bundle)),
        host_python="/opt/ciaobot/venv/bin/python",
    )
    plist = agents / "com.ciao.server.plist"
    before = plist.read_bytes()
    assert _load_plist(plist)["ExitTimeOut"] == 45

    # No verified host this run: `verify_owned_host` refuses, as an absent or
    # foreign host would.
    def _refuse(*_a: object, **_k: object) -> HostOwnership:
        raise ServerHostError("no record", code="missing_record")

    monkeypatch.setattr(cli, "verify_owned_host", _refuse)

    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path=sys.executable,
    )

    assert plist.read_bytes() == before, "a live hosted definition was downgraded"
    assert _load_plist(plist)["ExitTimeOut"] == 45


@macos_host_shape_only
def test_setup_rerun_on_a_dead_hosted_workspace_writes_the_direct_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kept hosted definition whose host executable is gone has no repair
    path unless setup rewrites it: setup must write the direct shape again."""
    agents = tmp_path / "LaunchAgents"
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(parents=True)
    agents.mkdir(parents=True)
    bundle = _host_bundle(tmp_path)

    cli._write_launchd_plist(
        workspace=workspace,
        launch_agents_dir=agents,
        port=8443,
        host=_verified_host(str(bundle)),
        host_python="/opt/ciaobot/venv/bin/python",
    )
    plist = agents / "com.ciao.server.plist"
    assert _load_plist(plist)["ExitTimeOut"] == 45

    # The host bundle is deleted or moved (and its record with it): the hosted
    # definition now names nothing launchd can run.
    shutil.rmtree(bundle)

    def _refuse(*_a: object, **_k: object) -> HostOwnership:
        raise ServerHostError("no record", code="missing_record")

    monkeypatch.setattr(cli, "verify_owned_host", _refuse)

    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path="/opt/ciaobot/venv/bin/python3.12",
    )

    data = _load_plist(plist)
    assert data["ProgramArguments"] == [
        "/opt/ciaobot/venv/bin/python3.12",
        "-m",
        "ciao.cli",
        "run",
    ]
    assert "ExitTimeOut" not in data, "a dead hosted definition was kept"


def test_setup_on_a_direct_workspace_stays_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No host, no existing definition: the direct shape is unchanged."""
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: None)
    agents = tmp_path / "LaunchAgents"

    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path="/opt/ciaobot/venv/bin/python3.12",
    )

    data = _load_plist(agents / "com.ciao.server.plist")
    assert data["ProgramArguments"] == [
        "/opt/ciaobot/venv/bin/python3.12",
        "-m",
        "ciao.cli",
        "run",
    ]
    assert "ExitTimeOut" not in data


def test_setup_with_a_foreign_or_unverified_host_stays_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host the record does not prove is not claimed: the direct shape is
    written, with no ExitTimeOut and no ``CiaobotServerHost`` in the argv."""
    def _refuse(*_a: object, **_k: object) -> HostOwnership:
        raise ServerHostError("not ours", code="not_owned")

    monkeypatch.setattr(cli, "verify_owned_host", _refuse)
    agents = tmp_path / "LaunchAgents"

    cli.setup_workspace(
        tmp_path / "ws",
        launch_agents_dir=agents,
        python_path="/opt/ciaobot/venv/bin/python3.12",
    )

    data = _load_plist(agents / "com.ciao.server.plist")
    assert data["ProgramArguments"] == [
        "/opt/ciaobot/venv/bin/python3.12",
        "-m",
        "ciao.cli",
        "run",
    ]
    assert "ExitTimeOut" not in data
    assert "CiaobotServerHost" not in " ".join(data["ProgramArguments"])


def test_setup_render_refusal_is_a_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host given with a non-python interpreter surfaces as RuntimeError."""
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: _verified_host())
    agents = tmp_path / "LaunchAgents"

    with pytest.raises(RuntimeError, match="interpreter"):
        cli.setup_workspace(
            tmp_path / "ws",
            launch_agents_dir=agents,
            python_path="/opt/tools/run-engine",
        )


def test_existing_hosted_definition_recognition_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown/legacy argv keeps today's behavior: not hosted, no raise."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(parents=True)

    plist = agents / "com.ciao.server.plist"
    for arguments in (
        ["/opt/ciao/bin/ciao", "run"],  # legacy console
        ["/opt/python3-intel64", "-m", "ciao.cli", "run"],  # legacy python
        [],  # no program
        "not-a-sequence",  # malformed
    ):
        plist.write_bytes(
            plistlib.dumps({"ProgramArguments": arguments, "WorkingDirectory": str(workspace)})
        )
        assert cli._existing_hosted_definition_serves(agents, workspace) is False

    hosted_argv = list(
        host_service_argv(
            PurePosixPath(_verified_host().bundle_path),
            PurePosixPath("/opt/ciaobot/venv/bin/python"),
        )
    )
    # A hosted argv whose EnvironmentVariables is not a dict names no workspace.
    for environment in ("not-a-dict", ["CIAO_WORKSPACE"], 7):
        plist.write_bytes(
            plistlib.dumps(
                {"ProgramArguments": hosted_argv, "EnvironmentVariables": environment}
            )
        )
        assert cli._existing_hosted_definition_serves(agents, workspace) is False
    # A plist whose top level is not a dict is not a definition at all.
    plist.write_bytes(plistlib.dumps(hosted_argv))
    assert cli._existing_hosted_definition_serves(agents, workspace) is False

    # A hosted definition for a *different* workspace is not ours either.
    plist.write_bytes(
        plistlib.dumps(
            {
                "ProgramArguments": list(
                    host_service_argv(
                        PurePosixPath(_verified_host().bundle_path),
                        PurePosixPath("/opt/ciaobot/venv/bin/python"),
                    )
                ),
                "EnvironmentVariables": {"CIAO_WORKSPACE": str(tmp_path / "other")},
            }
        )
    )
    assert cli._existing_hosted_definition_serves(agents, workspace) is False


@macos_host_shape_only
def test_existing_hosted_definition_is_preserved_only_while_its_host_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The N1 guard: existence of the named host executable decides preservation.

    Recognition is still offline and record-free — no ``verify_owned_host`` is
    called — but a hosted argv whose ``ProgramArguments[0]`` is not a regular
    file is not preserved, so setup can write the direct shape again.
    """
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(parents=True)
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(parents=True)

    def _forbid_verify(*_a: object, **_k: object) -> HostOwnership:
        raise AssertionError("recognition must not call verify_owned_host")

    monkeypatch.setattr(cli, "verify_owned_host", _forbid_verify)

    bundle = _host_bundle(tmp_path)
    argv = list(
        host_service_argv(
            PurePosixPath(str(bundle)),
            PurePosixPath("/opt/ciaobot/venv/bin/python"),
        )
    )
    plist = agents / "com.ciao.server.plist"
    plist.write_bytes(
        plistlib.dumps({"ProgramArguments": argv, "EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)}})
    )
    # The host executable exists: keep it.
    assert cli._existing_hosted_definition_serves(agents, workspace) is True

    # The executable is gone (bundle deleted or half-installed): stop keeping it.
    (bundle / "Contents" / "MacOS" / "CiaobotServerHost").unlink()
    assert cli._existing_hosted_definition_serves(agents, workspace) is False

    # A directory where a regular executable file is expected is not one either.
    executable = bundle / "Contents" / "MacOS" / "CiaobotServerHost"
    executable.mkdir()
    assert cli._existing_hosted_definition_serves(agents, workspace) is False


def test_register_launchd_service_selects_the_verified_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ciao service start --workspace`` registration renders the same hosted
    definition, serving the engine interpreter."""
    host = _verified_host()
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: host)
    monkeypatch.setattr(sys, "executable", _ENGINE_PYTHON)
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")

    cli._register_launchd_service(workspace)

    agents = Path(cli.default_launch_agents_dir())
    data = _load_plist(agents / "com.ciao.server.plist")
    assert data["ProgramArguments"] == _hosted_expected(host, _ENGINE_PYTHON)
    assert data["ExitTimeOut"] == 45


def test_register_launchd_service_render_refusal_is_a_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "verify_owned_host", lambda *a, **k: _verified_host())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".env").write_text("PWA_PORT=9555\n", encoding="utf-8")
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setenv("CIAO_ENGINE_PATH", "/opt/tools/run-engine")

    with pytest.raises(RuntimeError, match="interpreter"):
        cli._register_launchd_service(workspace)
