from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ciao import cli
from ciao.linux_service import render_service


def test_linux_setup_preserves_configuration_without_desktop_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CIAO_ENGINE_PATH", raising=False)
    workspace = tmp_path / "workspace"
    args = ["setup", "--workspace", str(workspace), "--port", "8544"]
    assert cli.main(args) == 0
    config = (workspace / ".env").read_text()
    assert "PWA_PORT=8544" in config
    assert "PWA_AUTH_REQUIRED=true" in config
    assert (workspace / ".env").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "LaunchAgents").exists()
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / "Applications").exists()
    assert (workspace / "personal" / "CLAUDE.md").is_file()
    assert "launchctl" not in capsys.readouterr().out
    assert cli.main(args[:-1] + ["9999"]) == 0
    assert (workspace / ".env").read_text() == config
    assert "http://localhost:8544/" in capsys.readouterr().out


def test_linux_rejects_launchd_before_creating_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    workspace = tmp_path / "workspace"
    assert cli.main(["setup", "--workspace", str(workspace), "--load-launchd"]) == 2
    assert not workspace.exists()


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
    assert "EnvironmentFile=" not in unit  # dotenv owns parsing of the workspace file


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
