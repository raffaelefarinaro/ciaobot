from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

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


# --- #3131 L1: `ciao service install` (install planning) --------------------


def test_install_script_is_constant_and_targets_unit_path():
    from ciao.linux_service import INSTALL_SCRIPT, installed_unit_python

    assert "{" not in INSTALL_SCRIPT and "}" not in INSTALL_SCRIPT
    # The suite's conftest monkeypatches UNIT_PATH per test, so compare against
    # the module's def-time default (the real /etc/... unit) instead.
    unit_path = installed_unit_python.__defaults__[0]
    assert str(unit_path) in INSTALL_SCRIPT
    assert unit_path == Path("/etc/systemd/system/ciaobot.service")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_passes_the_unit_only_on_stdin(tmp_path):
    from ciao.linux_service import INSTALL_SCRIPT, plan_install

    workspace = tmp_path / 'ws $ %x "q'
    python = Path("/opt/venv/bin/python")
    plan = plan_install(
        workspace=workspace, user="ciaobot", home=tmp_path, python=python,
        unit_path=tmp_path / "absent.service",
    )
    assert plan.commands[0][0] == ("sudo", "sh", "-c", INSTALL_SCRIPT)
    assert plan.commands[0][1] == render_service(
        workspace=workspace, user="ciaobot", home=tmp_path, python=python,
    )
    assert plan.commands[1][1] is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_refuses_a_source_install_unit(tmp_path):
    from ciao.linux_service import UnitConflict, plan_install

    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=tmp_path / "srv/ciaobot", user="ciaobot", home=Path("/home/ciaobot"),
        python=Path("/opt/ciaobot/venv/bin/python"),
    ), encoding="utf-8")
    with pytest.raises(UnitConflict):
        plan_install(
            workspace=tmp_path / "ws", user="ciaobot", home=Path("/home/ciaobot"),
            python=Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python"),
            unit_path=unit,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_replaces_its_own_unit_without_flag(tmp_path, monkeypatch):
    # Same python plus the same recorded workspace and user: not a conflict.
    from ciao.linux_service import plan_install

    unit = tmp_path / "ciaobot.service"
    python = Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python")
    workspace = Path("/srv/ciaobot")
    unit.write_text(render_service(
        workspace=workspace, user="ciaobot", home=Path("/home/ciaobot"),
        python=python,
    ), encoding="utf-8")
    _trust_own_uid(monkeypatch, unit)
    plan = plan_install(
        workspace=workspace, user="ciaobot", home=Path("/home/ciaobot"),
        python=python, unit_path=unit,
    )
    assert len(plan.commands) == 4


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_replace_overrides_conflict(tmp_path):
    from ciao.linux_service import plan_install

    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=Path("/opt/ciaobot/venv/bin/python"),
    ), encoding="utf-8")
    plan = plan_install(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python"),
        replace=True, unit_path=unit,
    )
    assert len(plan.commands) == 4


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_conflicts_when_the_same_python_repoints_the_workspace(tmp_path, monkeypatch):
    # Same interpreter, but the existing unit records another workspace:
    # overwrite would repoint that install, so refuse and name both paths.
    from ciao.linux_service import UnitConflict, plan_install

    unit = tmp_path / "ciaobot.service"
    python = Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python")
    unit.write_text(render_service(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=python,
    ), encoding="utf-8")
    _trust_own_uid(monkeypatch, unit)
    with pytest.raises(UnitConflict) as conflict:
        plan_install(
            workspace=Path("/srv/elsewhere"), user="ciaobot",
            home=Path("/home/ciaobot"), python=python, unit_path=unit,
        )
    message = str(conflict.value)
    assert "/srv/ciaobot" in message and "/srv/elsewhere" in message
    assert "CIAO_WORKSPACE" in message
    assert "--replace" in message


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_conflicts_when_the_same_python_changes_the_user(tmp_path, monkeypatch):
    from ciao.linux_service import UnitConflict, plan_install

    unit = tmp_path / "ciaobot.service"
    python = Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python")
    unit.write_text(render_service(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=python,
    ), encoding="utf-8")
    _trust_own_uid(monkeypatch, unit)
    with pytest.raises(UnitConflict) as conflict:
        plan_install(
            workspace=Path("/srv/ciaobot"), user="otherbot",
            home=Path("/home/otherbot"), python=python, unit_path=unit,
        )
    message = str(conflict.value)
    assert "User=" in message and "ciaobot" in message and "otherbot" in message


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_plan_install_conflicts_on_an_existing_garbled_unit(tmp_path, monkeypatch):
    """A unit whose interpreter cannot be read is not "probably ours".

    Its workspace and user lines parse here, so only the interpreter check can
    raise: dropping that because `installed` is None would silently plan over
    a unit this machine cannot vouch for.
    """
    from ciao.linux_service import UnitConflict, plan_install

    unit = tmp_path / "ciaobot.service"
    unit.write_text(
        '[Service]\nUser=ciaobot\nEnvironment="CIAO_WORKSPACE=/srv/ciaobot"\n'
        'ExecStart="/opt/venv/bin/python -m ciao.cli run\n',  # a quote that never closes
        encoding="utf-8",
    )
    _trust_own_uid(monkeypatch, unit)
    with pytest.raises(UnitConflict, match="cannot be read"):
        plan_install(
            workspace=Path("/srv/ciaobot"), user="ciaobot",
            home=Path("/home/ciaobot"),
            python=Path("/home/ciaobot/.local/share/uv/tools/ciaobot/bin/python"),
            unit_path=unit,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks exist everywhere but the dangling case is POSIX-shaped")
def test_plan_install_treats_a_dangling_symlink_as_existing(tmp_path):
    from ciao.linux_service import UnitConflict, plan_install

    unit = tmp_path / "ciaobot.service"
    unit.symlink_to(tmp_path / "gone.service")  # dangling: exists() is False
    with pytest.raises(UnitConflict):
        plan_install(
            workspace=tmp_path / "ws", user="ciaobot", home=tmp_path,
            python=Path("/opt/venv/bin/python"), unit_path=unit,
        )


@pytest.mark.skipif(sys.platform == "win32", reason="os.lstat permission semantics")
def test_plan_install_refuses_a_unit_it_cannot_stat(tmp_path, monkeypatch):
    from ciao import linux_service
    from ciao.linux_service import plan_install

    unit = tmp_path / "ciaobot.service"
    unit.write_text("[Service]\nUser=ciaobot\n", encoding="utf-8")
    real_lstat = linux_service.os.lstat

    def denied(path, *args, **kwargs):
        if Path(path) == unit:
            raise PermissionError(13, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(linux_service.os, "lstat", denied)
    with pytest.raises(ValueError, match="permission denied"):
        plan_install(
            workspace=tmp_path / "ws", user="ciaobot", home=tmp_path,
            python=Path("/opt/venv/bin/python"), unit_path=unit,
        )


def test_run_plan_runs_the_commands_in_order():
    from ciao.linux_service import INSTALL_SCRIPT, ServicePlan, run_plan

    commands = (
        (("sudo", "sh", "-c", INSTALL_SCRIPT), "[Unit]\n"),
        (("sudo", "systemctl", "daemon-reload"), None),
        (("sudo", "systemctl", "enable", "ciaobot.service"), None),
        (("sudo", "systemctl", "restart", "ciaobot.service"), None),
    )
    seen: list[tuple[tuple[str, ...], str | None]] = []
    run_plan(ServicePlan(unit="[Unit]\n", commands=commands), run=lambda argv, stdin: seen.append((argv, stdin)))
    assert seen == list(commands)
    # The unit travels exactly once: on the first step's stdin, never again.
    assert [stdin for _, stdin in seen] == ["[Unit]\n", None, None, None]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
def test_no_start_omits_restart(tmp_path):
    from ciao.linux_service import INSTALL_SCRIPT, plan_install

    def plan(**overrides):
        kwargs = dict(
            workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
            python=Path("/opt/venv/bin/python"), unit_path=tmp_path / "absent.service",
        )
        kwargs.update(overrides)
        return plan_install(**kwargs)

    quiet = plan(start=False)
    assert [argv for argv, _ in quiet.commands] == [
        ("sudo", "sh", "-c", INSTALL_SCRIPT),
        ("sudo", "systemctl", "daemon-reload"),
        ("sudo", "systemctl", "enable", "ciaobot.service"),
    ]
    assert all("restart" not in argv for argv, _ in quiet.commands)
    started = plan()
    assert started.commands[-1][0] == ("sudo", "systemctl", "restart", "ciaobot.service")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths in rendered unit")
@pytest.mark.parametrize("python", [
    Path("/opt/cia venv/bin/python"),
    Path("/opt/cia venv $HOME/bin/python"),
    Path("/opt/cia venv %n $BIN/bin/python"),
    Path('/opt/cia "venv"/bin/python'),
    Path("/opt/cia venv$/bin/python"),
])
def test_installed_unit_python_round_trips_quoting(python, tmp_path):
    from ciao.linux_service import installed_unit_python

    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=python,
    ), encoding="utf-8")
    assert installed_unit_python(unit) == python


def test_installed_unit_python_missing_or_garbled_is_none(tmp_path):
    from ciao.linux_service import installed_unit_python

    assert installed_unit_python(tmp_path / "absent.service") is None
    unit = tmp_path / "ciaobot.service"
    unit.write_text("[Service]\nUser=ciaobot\n", encoding="utf-8")
    assert installed_unit_python(unit) is None
    unit.write_text('[Service]\nExecStart="/opt/venv/bin/python -m ciao.cli run\n', encoding="utf-8")
    assert installed_unit_python(unit) is None  # a quote that never closes
    unit.write_text("[Service]\nExecStart=\n", encoding="utf-8")
    assert installed_unit_python(unit) is None


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd is not installed")
def test_systemd_accepts_planned_unit(tmp_path):
    from ciao.linux_service import plan_install

    plan = plan_install(
        workspace=tmp_path / 'workspace "quoted" %n', user="nobody",
        home=tmp_path, python=Path(sys.executable),
        unit_path=tmp_path / "absent.service",
    )
    unit = tmp_path / "ciaobot.service"
    unit.write_text(plan.unit, encoding="utf-8")
    result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- #3131 L1: `ciao service install` (CLI) ---------------------------------


def _act_as_linux_account(monkeypatch, tmp_path):
    """Run `ciao service install` as user ciaobot (uid 1000), never root."""
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    accounts = {1000: SimpleNamespace(pw_name="ciaobot", pw_dir=str(tmp_path / "home"))}
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=lambda uid: accounts[uid]))


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_dry_run_runs_nothing(capsys, monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **kwargs: calls.append(list(argv)))
    _act_as_linux_account(monkeypatch, tmp_path)

    assert cli.main(["service", "install", "--workspace", str(tmp_path / "ws"), "--dry-run"]) == 0
    assert calls == []
    out = capsys.readouterr().out
    assert out.startswith("[Unit]\n")
    would_run = [line for line in out.splitlines() if line.startswith("# would run: ")]
    assert len(would_run) == 4
    assert would_run[0].startswith("# would run: sudo sh -c ")
    assert "# would run: sudo systemctl daemon-reload" in would_run
    assert "# would run: sudo systemctl enable ciaobot.service" in would_run
    assert "# would run: sudo systemctl restart ciaobot.service" in would_run


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid does not exist")
def test_cli_service_install_refuses_root(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(["service", "install"]) == 2
    assert "not root" in capsys.readouterr().err


def test_cli_service_install_refuses_non_linux(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli.main(["service", "install"]) == 2
    assert "Linux only" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_conflict_exits_3(monkeypatch, capsys, tmp_path):
    from ciao import linux_service

    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=Path("/srv/ciaobot"), user="ciaobot", home=Path("/home/ciaobot"),
        python=Path("/opt/ciaobot/venv/bin/python"),
    ), encoding="utf-8")
    monkeypatch.setattr(linux_service, "UNIT_PATH", unit)
    _act_as_linux_account(monkeypatch, tmp_path)

    assert cli.main(["service", "install", "--workspace", str(tmp_path / "ws")]) == 3
    err = capsys.readouterr().err
    assert "/opt/ciaobot/venv/bin/python" in err
    assert "docs/LINUX.md#existing-source-installs" in err
    assert "--replace" in err


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_reports_a_failed_step(capsys, monkeypatch, tmp_path):
    """A step dying mid-plan must name the command and the half-applied risk.

    The fake runner succeeds twice and fails on the third step (the enable),
    so the message names its argv prefix and the returned code instead of
    exiting silently.
    """
    from ciao.linux_service import INSTALL_SCRIPT

    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if len(calls) == 3:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    _act_as_linux_account(monkeypatch, tmp_path)

    assert cli.main([
        "service", "install", "--workspace", str(tmp_path / "ws"), "--no-start",
    ]) == 1
    assert [argv[:3] for argv, _ in calls] == [
        ["sudo", "sh", "-c"],
        ["sudo", "systemctl", "daemon-reload"],
        ["sudo", "systemctl", "enable"],
    ]
    assert calls[0][0][3] == INSTALL_SCRIPT
    assert calls[0][1]["check"] is True
    err = capsys.readouterr().err
    assert "sudo systemctl enable" in err
    assert "exit code 1" in err
    assert "half applied" in err
    assert "systemctl status ciaobot" in err


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_plumbing_reaches_plan(monkeypatch, capsys, tmp_path):
    """--replace and --no-start must reach plan_install, not be dropped by the CLI."""
    from ciao import linux_service

    seen: list[dict[str, object]] = []
    real_plan = linux_service.plan_install

    def spy(**kwargs):
        seen.append(dict(kwargs))
        return real_plan(**kwargs)

    monkeypatch.setattr(linux_service, "plan_install", spy)
    _act_as_linux_account(monkeypatch, tmp_path)

    argv = ["service", "install", "--workspace", str(tmp_path / "ws")]
    assert cli.main([*argv, "--replace", "--no-start", "--dry-run"]) == 0
    assert seen[-1]["replace"] is True
    assert seen[-1]["start"] is False
    assert cli.main([*argv, "--dry-run"]) == 0
    assert seen[-1]["replace"] is False
    assert seen[-1]["start"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_defaults_to_the_units_workspace(monkeypatch, capsys, tmp_path):
    from ciao import linux_service

    # The installed unit records the workspace, so --workspace can be omitted.
    python = Path(sys.prefix) / "bin" / "python"
    unit = tmp_path / "ciaobot.service"
    unit.write_text(render_service(
        workspace=tmp_path / "recorded", user="ciaobot",
        home=Path("/home/ciaobot"), python=python,
    ), encoding="utf-8")
    _trust_own_uid(monkeypatch, unit)
    monkeypatch.setattr(linux_service, "UNIT_PATH", unit)
    _act_as_linux_account(monkeypatch, tmp_path)

    seen: list[dict[str, object]] = []
    real_plan = linux_service.plan_install

    def spy(**kwargs):
        seen.append(dict(kwargs))
        return real_plan(**kwargs)

    monkeypatch.setattr(linux_service, "plan_install", spy)
    assert cli.main(["service", "install", "--dry-run"]) == 0
    assert seen[-1]["workspace"] == tmp_path / "recorded"


@pytest.mark.skipif(sys.platform == "win32", reason="os.geteuid, pwd and POSIX paths")
def test_cli_service_install_without_workspace_or_unit_exits_2(monkeypatch, capsys, tmp_path):
    from ciao import linux_service

    # conftest's UNIT_PATH points at a path no test writes a unit to, so no
    # workspace is recorded anywhere and the only way out is --workspace.
    monkeypatch.setattr(linux_service, "UNIT_PATH", tmp_path / "absent.service")
    _act_as_linux_account(monkeypatch, tmp_path)
    assert cli.main(["service", "install"]) == 2
    assert "pass --workspace" in capsys.readouterr().err


def test_cli_desktop_service_alias_has_no_install(monkeypatch, capsys):
    # `install` is the Linux systemd verb: the deprecated macOS-only alias
    # keeps offering only the macOS verbs.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    with pytest.raises(SystemExit) as exc:
        cli.main(["desktop-service", "install", "--dry-run"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_service_group_help_is_not_macos_only(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "400")
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    service_line = next(
        line for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("service ")
    )
    assert "macOS" in service_line
    assert "Linux" in service_line


def _installer_stub(monkeypatch, *, fetch_error=None):
    """Replace the network fetch; record what was asked for and what was run."""
    from ciao import linux_service

    calls = {"fetched": [], "ran": []}

    def fetch(url, dest):
        calls["fetched"].append(url)
        if fetch_error is not None:
            raise fetch_error
        dest.write_text("#!/bin/sh\n", encoding="utf-8")

    def run(argv):
        calls["ran"].append(list(argv))
        return calls.get("exit", 0)

    return linux_service, calls, fetch, run


def test_linux_update_runs_the_latest_installer_with_passthrough_args(monkeypatch):
    linux_service, calls, fetch, run = _installer_stub(monkeypatch)

    assert linux_service.run_update(["--no-service"], fetch=fetch, run=run) == 0

    assert calls["fetched"] == [linux_service.LATEST_INSTALLER_URL]
    assert linux_service.LATEST_INSTALLER_URL.startswith("https://github.com/")
    assert linux_service.LATEST_INSTALLER_URL.endswith("/releases/latest/download/install.sh")
    [argv] = calls["ran"]
    assert argv[:1] == ["sh"]
    assert argv[1].endswith("install.sh")
    assert argv[2:] == ["--no-service"]


def test_linux_update_propagates_child_exit(monkeypatch):
    linux_service, calls, fetch, run = _installer_stub(monkeypatch)
    calls["exit"] = 3

    assert linux_service.run_update([], fetch=fetch, run=run) == 3


def test_linux_update_download_failure_is_reported_not_run(monkeypatch, capsys):
    linux_service, calls, fetch, run = _installer_stub(
        monkeypatch, fetch_error=OSError("no route to github")
    )

    assert linux_service.run_update([], fetch=fetch, run=run) == 1

    assert calls["ran"] == []
    assert "could not download the installer" in capsys.readouterr().err


def test_downloader_refuses_a_non_https_installer(tmp_path):
    from ciao import linux_service

    with pytest.raises(ValueError):
        linux_service._download("http://example.invalid/install.sh", tmp_path / "install.sh")


def test_cli_update_on_linux_runs_the_installer(monkeypatch):
    from ciao import engine_update, linux_service

    seen = []
    monkeypatch.setattr(cli.sys, "platform", "linux")

    def installer(argv):
        seen.append(("installer", list(argv)))
        return 0

    monkeypatch.setattr(linux_service, "run_update", installer)
    monkeypatch.setattr(
        engine_update, "main", lambda argv: pytest.fail("receiver must not run for install flags")
    )

    assert cli.main(["update", "--no-service"]) == 0
    assert cli.main(["update"]) == 0
    assert seen == [("installer", ["--no-service"]), ("installer", [])]


def test_cli_update_on_linux_keeps_the_receiver_verbs(monkeypatch):
    # Precedence: `status`, `run-apply` and `run-recover` stay with
    # engine_update, the update record and the hop's detached job. Dropping
    # this check sends them to the installer instead.
    from ciao import engine_update, linux_service

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        linux_service, "run_update", lambda argv: pytest.fail("installer must not run")
    )
    received = []
    monkeypatch.setattr(engine_update, "main", lambda argv: received.append(list(argv)) or 0)

    for argv in (["status"], ["status", "--json"], ["run-apply", "--operation", "x"],
                 ["run-recover", "--operation", "x"]):
        assert cli.main(["update", *argv]) == 0
    assert received == [
        ["status"], ["status", "--json"],
        ["run-apply", "--operation", "x"], ["run-recover", "--operation", "x"],
    ]


def test_cli_update_on_macos_is_not_the_installer(monkeypatch):
    # The Linux dispatch is platform-gated: a Mac keeps engine_update for
    # every `ciao update` verb, as before.
    from ciao import engine_update, linux_service

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(
        linux_service, "run_update", lambda argv: pytest.fail("installer must not run on macOS")
    )
    received = []
    monkeypatch.setattr(engine_update, "main", lambda argv: received.append(list(argv)) or 0)

    assert cli.main(["update", "--no-service"]) == 0
    assert received == [["--no-service"]]
