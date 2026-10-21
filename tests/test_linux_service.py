from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ciao import cli
from ciao.linux_service import render_service
from ciao.os_support.limits import SERVER_NOFILE_TARGET
from ciao.os_support.private import is_private


def test_linux_setup_preserves_configuration_without_desktop_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    workspace = tmp_path / "workspace"
    args = ["setup", "--workspace", str(workspace), "--port", "8544"]
    assert cli.main(args) == 0
    config = (workspace / ".env").read_text()
    assert "PWA_PORT=8544" in config
    assert "PWA_AUTH_REQUIRED" not in config
    assert is_private(workspace / ".env")
    assert not (tmp_path / "LaunchAgents").exists()
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / "Applications").exists()
    assert (workspace / "personal" / "AGENTS.md").is_file()
    assert "launchctl" not in capsys.readouterr().out
    assert cli.main(args[:-1] + ["9999"]) == 0
    assert (workspace / ".env").read_text() == config
    assert "http://localhost:8544/" in capsys.readouterr().out


def test_linux_rejects_launchd_before_creating_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    workspace = tmp_path / "workspace"
    assert cli.main(["setup", "--workspace", str(workspace), "--load-launchd"]) == 2
    assert not workspace.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="a systemd unit names POSIX paths (/srv/ciao), which are not absolute on Windows")
def test_service_keeps_virtualenv_and_escapes_systemd_expansions(tmp_path):
    python = tmp_path / "venv %n $HOME" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to("/usr/bin/python3")
    unit = render_service(
        workspace=Path('/srv/ciao "personal" %n'), user="ciaobot",
        home=Path("/var/lib/ciaobot"), python=python,
    )
    assert f'ExecStart="{str(python).replace("%", "%%").replace("$", "$$")}" -m ciao.cli run' in unit
    # WorkingDirectory= is literal: systemd-analyze rejects a quoted value as
    # "not absolute", so quotes render raw and only % is escaped. Backslashes
    # cannot be represented and are refused below.
    assert 'WorkingDirectory=/srv/ciao "personal" %%n' in unit
    assert 'Environment="HOME=/var/lib/ciaobot"' in unit
    assert "KillMode=control-group" in unit
    assert f"LimitNOFILE={SERVER_NOFILE_TARGET}" in unit
    assert "EnvironmentFile=" not in unit  # dotenv owns parsing of the workspace file


@pytest.mark.skipif(sys.platform == "win32", reason="a systemd unit names POSIX paths (/usr/bin), which are not absolute on Windows")
def test_service_path_lists_each_directory_once(tmp_path):
    # #1208: the interpreter's folder is often /usr/bin already, and the unit PATH
    # is composed through dedupe_path like every other PATH this package hands on.
    python = Path("/usr/bin/python3")
    unit = render_service(
        workspace=tmp_path / "workspace", user="ciaobot",
        home=Path("/var/lib/ciaobot"), python=python,
    )
    assert 'Environment="PATH=/usr/bin:/var/lib/ciaobot/.local/bin:/usr/local/bin:/bin"' in unit


@pytest.mark.parametrize("overrides", [
    {"user": "root"}, {"user": "bad\nExecStart=oops"},
    {"workspace": Path("relative")}, {"home": Path("/home/bad\npath")},
    {"workspace": Path("/srv/bad\\path")},
])
def test_service_refuses_invalid_values(overrides):
    kwargs = dict(workspace=Path("/srv/ciao"), user="ciaobot", home=Path("/home/ciaobot"), python=Path("/opt/ciao/bin/python"))
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        render_service(**kwargs)


@pytest.mark.skipif(sys.platform == "win32", reason="a systemd unit names POSIX paths (/srv/ciao), which are not absolute on Windows")
def test_cli_renders_only_a_unit(capsys):
    assert cli.main([
        "linux-service", "--workspace", "/srv/ciao", "--user", "ciaobot",
        "--home", "/var/lib/ciaobot", "--python", "/opt/ciao/bin/python",
    ]) == 0
    assert capsys.readouterr().out.startswith("[Unit]\n")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd is not installed")
def test_systemd_accepts_rendered_unit(tmp_path):
    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=tmp_path / 'workspace "quoted" %n', user="nobody",
        home=tmp_path, python=Path(sys.executable),
    ))
    result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _write_unit(path, workspace, *, mode=0o644):
    path.write_text(
        render_service(workspace=workspace, user="ciaobot", home=Path("/home/ciaobot"), python=Path("/usr/bin/python3")),
        encoding="utf-8",
    )
    path.chmod(mode)


def _trust_own_uid(monkeypatch, path):
    monkeypatch.setattr("ciao.linux_service.TRUSTED_UNIT_UID", path.stat().st_uid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_unit_workspace_round_trips_render_service(tmp_path, monkeypatch):
    from ciao.linux_service import unit_workspace

    unit = tmp_path / "ciaobot.service"
    workspace = tmp_path / 'ws%x"q'
    _write_unit(unit, workspace)
    _trust_own_uid(monkeypatch, unit)

    assert unit_workspace(unit) == workspace


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_unit_workspace_refuses_a_file_not_owned_by_the_trusted_uid(tmp_path, monkeypatch):
    from ciao.linux_service import unit_workspace

    unit = tmp_path / "ciaobot.service"
    _write_unit(unit, tmp_path / "ws")
    monkeypatch.setattr("ciao.linux_service.TRUSTED_UNIT_UID", unit.stat().st_uid + 1)

    assert unit_workspace(unit) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
@pytest.mark.parametrize("mode", [0o664, 0o646])
def test_unit_workspace_refuses_group_or_world_writable(tmp_path, monkeypatch, mode):
    from ciao.linux_service import unit_workspace

    unit = tmp_path / "ciaobot.service"
    _write_unit(unit, tmp_path / "ws", mode=mode)
    _trust_own_uid(monkeypatch, unit)

    assert unit_workspace(unit) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_unit_workspace_missing_or_without_the_line_is_none(tmp_path, monkeypatch):
    from ciao.linux_service import unit_workspace

    unit = tmp_path / "ciaobot.service"
    assert unit_workspace(unit) is None
    unit.write_text("[Service]\nEnvironment=\"HOME=/home/x\"\n", encoding="utf-8")
    unit.chmod(0o644)
    _trust_own_uid(monkeypatch, unit)
    assert unit_workspace(unit) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")
def test_unit_workspace_takes_the_last_assignment_and_ignores_relative_paths(tmp_path, monkeypatch):
    from ciao.linux_service import unit_workspace

    unit = tmp_path / "ciaobot.service"
    unit.write_text(
        'Environment="CIAO_WORKSPACE=/srv/first"\nEnvironment=CIAO_WORKSPACE=/srv/second\n',
        encoding="utf-8",
    )
    unit.chmod(0o644)
    _trust_own_uid(monkeypatch, unit)
    assert unit_workspace(unit) == Path("/srv/second")

    unit.write_text('Environment="CIAO_WORKSPACE=/srv/first"\nEnvironment="CIAO_WORKSPACE=rel/dir"\n', encoding="utf-8")
    assert unit_workspace(unit) is None
