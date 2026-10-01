from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import macos_service, service_backend, windows_service
from ciao.service_backend import (
    LinuxBackend,
    MacOSBackend,
    UnsupportedPlatformError,
    WindowsBackend,
    current_backend,
)


def test_current_backend_is_macos_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    backend = current_backend()

    assert isinstance(backend, MacOSBackend)
    assert backend.name == "macos"


@pytest.mark.parametrize("platform", ["linux", "linux2"])
def test_current_backend_is_linux_on_linux(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    backend = current_backend()

    assert isinstance(backend, LinuxBackend)
    assert backend.name == "linux"


def test_current_backend_is_windows_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    backend = current_backend()

    assert isinstance(backend, WindowsBackend)
    assert backend.name == "windows"


def test_current_backend_reads_platform_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    first = current_backend()
    monkeypatch.setattr(sys, "platform", "linux")
    second = current_backend()

    assert isinstance(first, MacOSBackend)
    assert isinstance(second, LinuxBackend)
    assert type(first) is not type(second)


@pytest.mark.parametrize("platform", ["cygwin", "freebsd13", "freebsd14"])
def test_unsupported_platform_raises(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(UnsupportedPlatformError, match=platform) as excinfo:
        current_backend()
    # The message names every supported platform, so it cannot quietly go stale.
    assert "macOS, Linux and Windows" in str(excinfo.value)
    assert issubclass(UnsupportedPlatformError, RuntimeError)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_agents_dir_honours_override_on_both_backends(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    backend = current_backend()
    real_default = Path.home() / "Library" / "LaunchAgents"

    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path))
    assert backend.agents_dir() == tmp_path
    # The live dir ignores the override: it is the operator's real install.
    assert backend.live_agents_dir() == real_default

    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    assert backend.agents_dir() == real_default
    assert backend.live_agents_dir() == real_default


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX launchd/systemd backends")
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_is_live_agents_dir(
    platform: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    live = home / "Library" / "LaunchAgents"
    live.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "platform", platform)
    backend = current_backend()

    assert backend.is_live_agents_dir(backend.live_agents_dir())

    link = tmp_path / "link-to-live"
    link.symlink_to(live, target_is_directory=True)
    assert backend.is_live_agents_dir(link)

    assert not backend.is_live_agents_dir(tmp_path / "x")


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX launchd/systemd backends")
def test_macos_bootout_agent_runs_exact_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, *args, **kwargs):
        calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(macos_service.subprocess, "run", fake_run)

    MacOSBackend().bootout_agent("com.ciao.menubar")

    assert calls == [
        (
            ["launchctl", "bootout", f"gui/{os.getuid()}/com.ciao.menubar"],
            {"check": False, "capture_output": True},
        )
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX launchd/systemd backends")
def test_macos_bootout_agent_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, *args, **kwargs):
        raise OSError("no launchctl here")

    monkeypatch.setattr(macos_service.subprocess, "run", fake_run)

    assert MacOSBackend().bootout_agent("com.ciao.menubar") is None


def test_macos_load_agent_runs_unload_probe_then_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, *args, **kwargs):
        calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(macos_service.subprocess, "run", fake_run)
    definition = tmp_path / "com.ciao.server.plist"

    assert MacOSBackend().load_agent(definition) == 7

    assert [call[0] for call in calls] == [
        ["launchctl", "unload", str(definition)],
        ["launchctl", "load", "-w", str(definition)],
    ]
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert calls[0][1]["check"] is False
    # The probe's stderr is the only thing redirected away; load keeps its own.
    assert calls[1][1] == {"check": False}


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX launchd/systemd backends")
def test_macos_schedule_server_handoff_script_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plist = home / "Library" / "LaunchAgents" / "com.ciao.server.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("plist", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command, *args, **kwargs):
        calls.append((list(command), kwargs))
        return subprocess.Popen  # never used; only the recording matters

    monkeypatch.setattr(macos_service.subprocess, "Popen", fake_popen)

    expected = (
        f"sleep 3; /bin/launchctl load -w '{plist}' 2>/dev/null; "
        f"/bin/launchctl kickstart gui/{os.getuid()}/com.ciao.server 2>/dev/null; exit 0"
    )

    assert MacOSBackend().schedule_server_handoff() is True
    assert calls == [
        (
            ["/bin/sh", "-c", expected],
            {
                "start_new_session": True,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]


def test_macos_schedule_server_handoff_without_a_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def fail_popen(*args, **kwargs):
        raise AssertionError("Popen must not run when the plist is missing")

    monkeypatch.setattr(macos_service.subprocess, "Popen", fail_popen)

    assert MacOSBackend().schedule_server_handoff() is False


def test_macos_schedule_server_handoff_swallows_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plist = home / "Library" / "LaunchAgents" / "com.ciao.server.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("plist", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    def fail_popen(*args, **kwargs):
        raise OSError("cannot spawn")

    monkeypatch.setattr(macos_service.subprocess, "Popen", fail_popen)

    assert MacOSBackend().schedule_server_handoff() is False


def test_linux_bootout_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_run(*args, **kwargs):
        raise AssertionError("Linux must never run launchctl")

    monkeypatch.setattr(subprocess, "run", fail_run)

    assert LinuxBackend().bootout_agent("com.ciao.menubar") is None


def test_linux_load_and_handoff_raise_unsupported(tmp_path: Path) -> None:
    backend = LinuxBackend()

    with pytest.raises(UnsupportedPlatformError):
        backend.load_agent(tmp_path / "com.ciao.server.plist")
    with pytest.raises(UnsupportedPlatformError):
        backend.schedule_server_handoff()


def test_windows_dirs_honour_the_override_only_in_agents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = WindowsBackend()
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path))
    assert backend.agents_dir() == tmp_path
    # The live dir is the operator's real install, so the override cannot reach it.
    assert backend.live_agents_dir() == local / "Ciaobot" / "service"

    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    assert backend.agents_dir() == local / "Ciaobot" / "service"


def test_windows_is_live_agents_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    backend = WindowsBackend()

    assert backend.is_live_agents_dir(backend.live_agents_dir())
    assert not backend.is_live_agents_dir(tmp_path)


def test_windows_load_agent_registers_then_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An InteractiveToken task only starts at the next logon, so load starts it now."""
    calls: list[str] = []
    monkeypatch.setattr(windows_service, "register_task", lambda *a, **k: calls.append("register"))
    monkeypatch.setattr(windows_service, "start_task", lambda *a, **k: calls.append("start"))
    definition = tmp_path / "Ciaobot-Engine.xml"

    assert WindowsBackend().load_agent(definition) == 0

    assert calls == ["register", "start"]


def test_windows_load_agent_prints_the_failure_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise windows_service.WindowsServiceError("schtasks /Create failed: denied")

    monkeypatch.setattr(windows_service, "register_task", refuse)

    assert WindowsBackend().load_agent(tmp_path / "t.xml") == 1

    assert "schtasks /Create failed: denied" in capsys.readouterr().err


def test_windows_bootout_ignores_another_label(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object):
        raise AssertionError("only the server task is ever deleted")

    monkeypatch.setattr(windows_service, "unregister_task", fail)

    assert WindowsBackend().bootout_agent("com.ciao.menubar") is None


def test_windows_bootout_deletes_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        windows_service, "unregister_task", lambda *a, **k: calls.append("unregister")
    )

    assert WindowsBackend().bootout_agent("com.ciao.server") is None

    assert calls == ["unregister"]


@pytest.mark.parametrize(
    "failure",
    [windows_service.WindowsServiceError("gone"), OSError("no schtasks here")],
)
def test_windows_bootout_swallows_failures(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Protocol promises bootout never raises: setup uninstalls through it."""

    def fail(*args: object, **kwargs: object):
        raise failure

    monkeypatch.setattr(windows_service, "unregister_task", fail)

    assert WindowsBackend().bootout_agent("com.ciao.server") is None


def test_windows_handoff_without_a_task_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object):
        raise AssertionError("no helper may be spawned when the task is absent")

    monkeypatch.setattr(windows_service, "task_exists", lambda *a, **k: False)
    monkeypatch.setattr(windows_service, "spawn_delayed_start", fail)

    assert WindowsBackend().schedule_server_handoff() is False


def test_windows_handoff_swallows_a_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object):
        raise windows_service.WindowsServiceError("timed out")

    monkeypatch.setattr(windows_service, "task_exists", fail)
    monkeypatch.setattr(
        windows_service,
        "spawn_delayed_start",
        lambda *a, **k: pytest.fail("no helper may be spawned after a failed probe"),
    )

    assert WindowsBackend().schedule_server_handoff() is False


@pytest.mark.parametrize("spawned", [True, False])
def test_windows_handoff_returns_the_spawn_result(
    spawned: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows_service, "task_exists", lambda *a, **k: True)
    monkeypatch.setattr(windows_service, "spawn_delayed_start", lambda *a, **k: spawned)

    assert WindowsBackend().schedule_server_handoff() is spawned


@pytest.mark.parametrize("module", ["ciao/cli.py", "ciao/web/routes_api.py"])
def test_no_module_outside_service_modules_hardcodes_the_launchagents_path(
    module: str,
) -> None:
    """The seam only stays a seam if no call site rebuilds the path or argv."""
    source = (Path(service_backend.__file__).parent.parent / module).read_text(
        encoding="utf-8"
    )

    for forbidden in (
        '"Library" / "LaunchAgents"',
        '"launchctl", "bootout"',
        '"launchctl", "load"',
        '"launchctl", "unload"',
        "/bin/launchctl",
    ):
        assert forbidden not in source, f"{module} still hardcodes {forbidden!r}"