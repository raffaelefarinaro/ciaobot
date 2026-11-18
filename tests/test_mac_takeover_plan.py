"""``ciao mac-takeover plan`` classifies a 1.x install without touching it (#3129).

Every fixture lives under ``tmp_path`` with a fake plist and injected probes;
nothing here talks to launchd or the real home.
"""
from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao import cli, mac_takeover
from ciao.macos_service import RestartBlockersUnavailable

LABEL = "com.ciao.server.t3129"
PYTHON_ARGV_TAIL = ["-m", "ciao.cli", "run"]


class Machine:
    """One fake Mac: a home, a LaunchAgents dir and a workspace."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.agents = home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.workspace = home / "Ciaobot"
        (self.workspace / ".runtime").mkdir(parents=True)
        (self.workspace / ".runtime" / "workspaces.json").write_text("{}")
        self.env_root = home / ".local" / "share" / "uv" / "tools" / "ciaobot"
        (self.env_root / "bin").mkdir(parents=True)
        (self.env_root / "bin" / "python").write_text("")
        self.state = home / ".local" / "state" / "ciaobot"
        self.state.mkdir(parents=True)

    def write_plist(
        self,
        argv: list[str] | None = None,
        env: dict[str, str] | None = None,
        label: str = LABEL,
        **extra: Any,
    ) -> Path:
        document: dict[str, Any] = {
            "Label": label,
            "ProgramArguments": argv
            if argv is not None
            else [str(self.env_root / "bin" / "python"), *PYTHON_ARGV_TAIL],
            "EnvironmentVariables": {
                "CIAO_WORKSPACE": str(self.workspace),
                "CIAO_RUNTIME_ROOT": str(self.workspace / ".runtime"),
                "CIAO_PORT": "8443",
                "PATH": "/usr/bin",
                **(env or {}),
            },
            **extra,
        }
        path = self.agents / f"{label}.plist"
        path.write_bytes(plistlib.dumps(document))
        return path

    def write_receipt(self, python: Path | None = None, **extra: Any) -> Path:
        path = self.state / "install-receipt.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "version": "1.10.0",
                    "python": str(python or self.env_root / "bin" / "python"),
                    **extra,
                }
            )
        )
        return path

    def shim(self, text: str | None = None, link: bool = False) -> Path:
        shim = self.home / ".local" / "bin" / "ciao"
        shim.parent.mkdir(parents=True, exist_ok=True)
        if link:
            (self.env_root / "bin" / "ciao").write_text("")
            shim.symlink_to(self.env_root / "bin" / "ciao")
        else:
            shim.write_text(text if text is not None else f"#!/bin/sh\n{mac_takeover.SHIM_MARKER}\n")
        return shim


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Machine:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # The real account home is elsewhere, so this home counts as isolated.
    monkeypatch.setattr(mac_takeover, "_real_home", lambda: tmp_path / "real-home")
    monkeypatch.setattr(mac_takeover.sys, "platform", "darwin")
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: None)
    monkeypatch.setattr(mac_takeover, "read_lock_owner", lambda runtime: None)
    monkeypatch.setattr(mac_takeover, "restart_blockers", lambda port: [])
    return Machine(home)


def run_plan(machine: Machine, label: str = LABEL) -> mac_takeover.TakeoverPlan:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 113, "", "")

    return mac_takeover.plan(launch_agents_dir=machine.agents, label=label, runner=runner)


def serve(
    monkeypatch: pytest.MonkeyPatch,
    machine: Machine,
    *,
    version: str = "1.10.0",
    pid: int = 4242,
    lock_pid: int | None = 4242,
    lock_workspace: str | None = None,
) -> None:
    monkeypatch.setattr(
        mac_takeover,
        "engine_identity",
        lambda port: {"startup_id": "abc", "pid": pid, "version": version},
    )
    owner = (
        None
        if lock_pid is None
        else {"pid": lock_pid, "workspace": lock_workspace or str(machine.workspace)}
    )
    monkeypatch.setattr(mac_takeover, "read_lock_owner", lambda runtime: owner)


def installer_install(machine: Machine) -> None:
    machine.write_plist()
    machine.write_receipt()
    machine.shim()


# --- isolation guard -----------------------------------------------------------


def test_isolated_home_with_live_label_is_refused(machine: Machine) -> None:
    installer_install(machine)
    result = run_plan(machine, label=mac_takeover.SERVER_LABEL)
    assert (result.action, result.reason) == ("refuse", "isolated_home_needs_test_label")


def test_test_label_on_real_home_is_refused(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer_install(machine)
    monkeypatch.setattr(mac_takeover, "_real_home", lambda: machine.home)
    result = run_plan(machine)
    assert (result.action, result.reason) == ("refuse", "test_label_needs_isolated_home")


def test_live_label_on_real_home_passes_the_guard(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mac_takeover, "_real_home", lambda: machine.home)
    machine.write_plist(label=mac_takeover.SERVER_LABEL)
    machine.write_receipt()
    assert run_plan(machine, label=mac_takeover.SERVER_LABEL).action == "takeover"


def test_non_macos_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    result = mac_takeover.plan(launch_agents_dir=tmp_path, label=LABEL)
    assert (result.action, result.reason) == ("refuse", "unsupported_platform")


# --- journal and the no-op answers --------------------------------------------


def make_journal(machine: Machine, name: str, state: str) -> None:
    directory = mac_takeover.state_dir() / "takeover" / name
    directory.mkdir(parents=True)
    (directory / "journal.json").write_text(json.dumps({"state": state}))


def test_unfinished_journal_resumes_before_anything_else(machine: Machine) -> None:
    # No plist, a garbage plist would also refuse: resume still wins.
    make_journal(machine, "20261118T100000Z", "stopped")
    (machine.agents / f"{LABEL}.plist").write_text("not a plist")
    result = run_plan(machine)
    assert (result.action, result.resume_id) == ("resume", "20261118T100000Z")


def test_finished_and_rolled_back_journals_do_not_resume(machine: Machine) -> None:
    make_journal(machine, "20261118T100000Z", "finished")
    make_journal(machine, "20261118T110000Z", "rolled_back")
    assert run_plan(machine).action == "none"


def test_unreadable_journal_counts_as_unfinished(machine: Machine) -> None:
    directory = mac_takeover.state_dir() / "takeover" / "20261118T100000Z"
    directory.mkdir(parents=True)
    (directory / "journal.json").write_text("{")
    assert run_plan(machine).action == "resume"


def test_no_plist_is_nothing_to_do(machine: Machine) -> None:
    result = run_plan(machine)
    assert (result.action, result.reason) == ("none", "")


def test_no_plist_with_pointer_reads_already_app(machine: Machine) -> None:
    engine = mac_takeover.state_dir()
    engine.mkdir(parents=True)
    (engine / "workspace.json").write_text(
        json.dumps({"schema": 1, "workspace": str(machine.workspace), "runtime_root": "x"})
    )
    result = run_plan(machine)
    assert (result.action, result.reason) == ("none", "already_app")


# --- plist, workspace, registry -----------------------------------------------


def test_garbage_plist_is_unreadable(machine: Machine) -> None:
    (machine.agents / f"{LABEL}.plist").write_text("not a plist")
    assert run_plan(machine).reason == "unreadable_definition"


def test_plist_with_another_label_is_unreadable(machine: Machine) -> None:
    path = machine.write_plist()
    document = plistlib.loads(path.read_bytes())
    document["Label"] = "com.example.other"
    path.write_bytes(plistlib.dumps(document))
    assert run_plan(machine).reason == "unreadable_definition"


def test_oversized_plist_is_unreadable(machine: Machine) -> None:
    (machine.agents / f"{LABEL}.plist").write_bytes(b"x" * (300 * 1024))
    assert run_plan(machine).reason == "unreadable_definition"


def test_process_environment_never_supplies_the_workspace(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = machine.write_plist()
    document = plistlib.loads(path.read_bytes())
    del document["EnvironmentVariables"]["CIAO_WORKSPACE"]
    path.write_bytes(plistlib.dumps(document))
    monkeypatch.setenv("CIAO_WORKSPACE", str(machine.workspace))
    assert run_plan(machine).reason == "no_workspace"


def test_workspace_directory_must_exist(machine: Machine) -> None:
    machine.write_plist(env={"CIAO_WORKSPACE": str(machine.home / "gone")})
    assert run_plan(machine).reason == "no_registry"


def test_runtime_root_directory_must_exist(machine: Machine) -> None:
    machine.write_plist(env={"CIAO_RUNTIME_ROOT": str(machine.home / "no-runtime")})
    assert run_plan(machine).reason == "no_registry"


def test_registry_must_parse(machine: Machine) -> None:
    machine.write_plist()
    (machine.workspace / ".runtime" / "workspaces.json").write_text("{not json")
    assert run_plan(machine).reason == "no_registry"


def test_missing_registry_refuses(machine: Machine) -> None:
    machine.write_plist()
    (machine.workspace / ".runtime" / "workspaces.json").unlink()
    assert run_plan(machine).reason == "no_registry"


# --- program kinds -------------------------------------------------------------


def test_legacy_desktop_runtime_is_refused(machine: Machine) -> None:
    machine.write_plist(
        argv=[
            str(machine.home / "Applications/Ciaobot.app/Contents/Resources/ciao-runtime/bin/python"),
            *PYTHON_ARGV_TAIL,
        ]
    )
    assert run_plan(machine).reason == "legacy_desktop"


@pytest.mark.parametrize(
    "argv",
    [
        ["/bin/sh", "-c", "ciao run"],
        ["relative/python", "-m", "ciao.cli", "run"],
        ["/x/python", "-m", "ciao.cli", "run", "--extra"],
        ["/x/python", "-m", "other", "run"],
        [],
    ],
)
def test_unknown_programs_are_refused(machine: Machine, argv: list[str]) -> None:
    machine.write_plist(argv=argv)
    assert run_plan(machine).reason == "unknown_program"


def test_hosted_shape_is_recognised(machine: Machine) -> None:
    host = machine.home / "Applications" / "Ciaobot Server.app"
    (host / "Contents" / "MacOS").mkdir(parents=True)
    machine.write_plist(
        argv=[
            str(host / "Contents/MacOS/Ciaobot Server"),
            "serve",
            "--python",
            str(machine.env_root / "bin" / "python"),
        ]
    )
    result = run_plan(machine)
    assert (result.action, result.program_kind) == ("takeover", "server_host")


@pytest.mark.parametrize(
    "tail",
    [
        ["serve", "--python", "/x/python", "--extra"],
        ["serve", "--python"],
        ["run-move", "--python", "/x/python"],
        ["serve", "--python", "relative/python"],
    ],
)
def test_hosted_parser_refuses_extra_or_odd_args(machine: Machine, tail: list[str]) -> None:
    machine.write_plist(argv=["/Apps/Ciaobot Server.app/Contents/MacOS/Ciaobot Server", *tail])
    assert run_plan(machine).reason == "unknown_program"


def test_hosted_parser_refuses_a_wrong_executable_name(machine: Machine) -> None:
    machine.write_plist(argv=["/Apps/Other/Contents/MacOS/Other", "serve", "--python", "/x/python"])
    assert run_plan(machine).reason == "unknown_program"


def test_plain_python_without_receipt_has_no_trash(machine: Machine) -> None:
    machine.write_plist()
    result = run_plan(machine)
    assert (result.program_kind, result.trash) == ("python", ())


def test_ciao_console_shape_is_recognised(machine: Machine) -> None:
    machine.write_plist(argv=[str(machine.env_root / "bin" / "ciao"), "run"])
    machine.write_receipt()
    assert run_plan(machine).program_kind == "installer_env"


# --- data location, env, port --------------------------------------------------


@pytest.mark.parametrize("key", ["CIAO_VAULT_ROOT", "CIAO_MEMORY_DIR", "CIAO_VAULT_DIR", "CIAO_WORKSPACES"])
def test_data_location_only_in_the_plist_is_refused(machine: Machine, key: str) -> None:
    machine.write_plist(env={key: str(machine.home / "elsewhere")})
    assert run_plan(machine).reason == "custom_data_location"


def test_data_location_also_in_dotenv_is_allowed(machine: Machine) -> None:
    machine.write_plist(env={"CIAO_VAULT_ROOT": str(machine.home / "vault")})
    (machine.workspace / ".env").write_text(f"CIAO_VAULT_ROOT={machine.home / 'vault'}\n")
    result = run_plan(machine)
    assert (result.action, result.dropped_env) == ("takeover", ())


def test_other_plist_environment_is_reported_as_dropped(machine: Machine) -> None:
    machine.write_plist(env={"LC_ALL": "C", "LANG": "C", "HOME": "/h", "HTTPS_PROXY": "x", "CIAO_DEBUG": "1"})
    assert run_plan(machine).dropped_env == ("CIAO_DEBUG", "HTTPS_PROXY")


def test_dotenv_port_that_differs_from_the_plist_is_refused(machine: Machine) -> None:
    machine.write_plist()
    (machine.workspace / ".env").write_text("PWA_PORT=9001\n")
    # The plist's CIAO_PORT is overridden by the .env in discover_runtime, so
    # they agree: no refusal. A plist port the .env does not match refuses.
    assert run_plan(machine).action == "takeover"
    (machine.workspace / ".env").write_text("")
    machine.write_plist(env={"CIAO_PORT": "9002"})
    result = run_plan(machine)
    assert (result.reason, result.port) == ("port_mismatch", 9002)


# --- liveness, version, chats --------------------------------------------------


def test_listener_without_a_matching_lock_is_foreign(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, lock_pid=None)
    assert run_plan(machine).reason == "foreign_listener"


def test_listener_with_another_pid_is_foreign(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, pid=4242, lock_pid=77)
    assert run_plan(machine).reason == "foreign_listener"


def test_listener_for_another_workspace_is_foreign(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, lock_workspace=str(machine.home / "other"))
    assert run_plan(machine).reason == "foreign_listener"


def test_listener_that_reports_no_pid_is_foreign(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: {"startup_id": "a"})
    monkeypatch.setattr(
        mac_takeover, "read_lock_owner", lambda runtime: {"pid": 1, "workspace": str(machine.workspace)}
    )
    assert run_plan(machine).reason == "foreign_listener"


def test_matching_listener_records_version_and_chats(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, version="1.10.2")
    monkeypatch.setattr(mac_takeover.ciao, "__version__", "1.11.0")
    monkeypatch.setattr(mac_takeover, "restart_blockers", lambda port: [{"chat_id": "a"}, {"chat_id": "b"}])
    result = run_plan(machine)
    assert (result.action, result.old_version, result.old_running, result.active_chats) == (
        "takeover",
        "1.10.2",
        True,
        2,
    )


def test_newer_old_engine_is_a_downgrade(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, version="2.0.1")
    monkeypatch.setattr(mac_takeover.ciao, "__version__", "2.0.0")
    result = run_plan(machine)
    assert (result.reason, result.old_version) == ("downgrade", "2.0.1")


def test_same_version_is_not_a_downgrade(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, version="2.0.0")
    monkeypatch.setattr(mac_takeover.ciao, "__version__", "2.0.0")
    assert run_plan(machine).action == "takeover"


def test_engine_older_than_1_10_is_refused(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine, version="1.9.0")
    monkeypatch.setattr(mac_takeover.ciao, "__version__", "2.0.0")
    result = run_plan(machine)
    assert (result.reason, result.old_version) == ("engine_too_old", "1.9.0")


def test_stopped_engine_falls_back_to_the_receipt_version(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    machine.write_receipt(version="1.8.1")
    monkeypatch.setattr(mac_takeover.ciao, "__version__", "2.0.0")
    result = run_plan(machine)
    assert (result.reason, result.old_version) == ("engine_too_old", "1.8.1")


def test_unreadable_restart_blockers_refuse(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    serve(monkeypatch, machine)

    def boom(port: int) -> list[dict[str, Any]]:
        raise RestartBlockersUnavailable("HTTP 500")

    monkeypatch.setattr(mac_takeover, "restart_blockers", boom)
    assert run_plan(machine).reason == "blockers_unavailable"


def test_running_job_without_an_answering_engine_is_running(machine: Machine) -> None:
    machine.write_plist()

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, "gui/501/x = {\n\tpid = 999\n}\n", "")

    result = mac_takeover.plan(launch_agents_dir=machine.agents, label=LABEL, runner=runner)
    assert result.old_running is True


def test_stopped_job_is_not_running(machine: Machine) -> None:
    machine.write_plist()
    assert run_plan(machine).old_running is False


# --- trash candidates ----------------------------------------------------------


def proofs(result: mac_takeover.TakeoverPlan) -> dict[str, str]:
    return {Path(item.path).name: item.proof for item in result.trash}


def test_installer_env_offers_env_receipt_and_shim(machine: Machine) -> None:
    installer_install(machine)
    result = run_plan(machine)
    assert result.program_kind == "installer_env"
    assert proofs(result) == {
        "ciaobot": "receipt_env",
        "install-receipt.json": "receipt",
        "ciao": "installer_shim",
    }
    assert result.left_in_place == ()


def test_uv_symlink_shim_is_offered(machine: Machine) -> None:
    machine.write_plist()
    machine.write_receipt()
    machine.shim(link=True)
    assert proofs(run_plan(machine))["ciao"] == "uv_tool_symlink"


def test_shim_without_the_marker_is_not_a_candidate(machine: Machine) -> None:
    machine.write_plist()
    machine.write_receipt()
    machine.shim(text="#!/bin/sh\necho mine\n")
    assert "ciao" not in proofs(run_plan(machine))


def test_symlink_to_elsewhere_is_not_a_candidate(machine: Machine) -> None:
    machine.write_plist()
    machine.write_receipt()
    target = machine.home / "mine"
    target.write_text("")
    shim = machine.home / ".local" / "bin" / "ciao"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(target)
    assert "ciao" not in proofs(run_plan(machine))


def test_receipt_naming_another_env_offers_nothing(machine: Machine) -> None:
    machine.write_plist()
    other = machine.home / "other-env" / "bin" / "python"
    other.parent.mkdir(parents=True)
    other.write_text("")
    machine.write_receipt(python=other)
    machine.shim()
    result = run_plan(machine)
    assert (result.program_kind, result.trash) == ("python", ())


def test_env_not_named_ciaobot_is_not_offered(machine: Machine) -> None:
    renamed = machine.env_root.with_name("custom")
    machine.env_root.rename(renamed)
    machine.env_root = renamed
    machine.write_plist()
    machine.write_receipt()
    assert run_plan(machine).trash == ()


def test_receipt_uv_is_offered_only_under_the_state_dir(machine: Machine) -> None:
    machine.write_plist()
    inside = machine.state / "uv"
    inside.write_text("")
    machine.write_receipt(uv=str(inside))
    assert [i.path for i in run_plan(machine).trash].count(str(inside)) == 1
    outside = machine.home / "uv"
    outside.write_text("")
    machine.write_receipt(uv=str(outside))
    assert str(outside) not in [i.path for i in run_plan(machine).trash]


def test_checkout_is_never_trashed(machine: Machine) -> None:
    repo = machine.home / "src" / "ciaobot"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("")
    machine.write_plist(argv=[str(repo / ".venv/bin/python"), *PYTHON_ARGV_TAIL])
    machine.write_receipt(python=repo / ".venv/bin/python")
    machine.shim()
    result = run_plan(machine)
    assert (result.program_kind, result.trash, result.left_in_place) == ("checkout", (), (str(repo),))


def test_hosted_install_offers_host_bundle_and_record(machine: Machine) -> None:
    host = machine.home / "Applications" / "Ciaobot Server.app"
    (host / "Contents" / "MacOS").mkdir(parents=True)
    (machine.state / "server-host.json").write_text(json.dumps({"bundle_path": str(host)}))
    machine.write_plist(
        argv=[str(host / "Contents/MacOS/Ciaobot Server"), "serve", "--python", str(machine.env_root / "bin/python")]
    )
    result = run_plan(machine)
    assert proofs(result) == {
        "Ciaobot Server.app": "server_host_program",
        "server-host.json": "server_host_record",
    }


def test_host_bundle_elsewhere_is_not_offered(machine: Machine) -> None:
    other = machine.home / "Downloads" / "Ciaobot Server.app"
    (other / "Contents" / "MacOS").mkdir(parents=True)
    # The canonical bundle exists too; the plist's program is not in it.
    (machine.home / "Applications" / "Ciaobot Server.app").mkdir(parents=True)
    machine.write_plist(
        argv=[str(other / "Contents/MacOS/Ciaobot Server"), "serve", "--python", str(machine.env_root / "bin/python")]
    )
    assert run_plan(machine).trash == ()


def test_host_record_naming_another_bundle_is_not_offered(machine: Machine) -> None:
    host = machine.home / "Applications" / "Ciaobot Server.app"
    (host / "Contents" / "MacOS").mkdir(parents=True)
    (machine.state / "server-host.json").write_text(json.dumps({"bundle_path": "/elsewhere"}))
    machine.write_plist(
        argv=[str(host / "Contents/MacOS/Ciaobot Server"), "serve", "--python", str(machine.env_root / "bin/python")]
    )
    assert proofs(run_plan(machine)) == {"Ciaobot Server.app": "server_host_program"}


# --- _safe ---------------------------------------------------------------------


def test_safe_accepts_an_unrelated_path_under_home(machine: Machine) -> None:
    target = machine.home / "tools" / "ciaobot"
    target.mkdir(parents=True)
    assert mac_takeover._safe(target, [machine.workspace]) is True


def test_safe_drops_the_protected_path_itself(machine: Machine) -> None:
    assert mac_takeover._safe(machine.workspace, [machine.workspace]) is False


def test_safe_drops_a_path_inside_a_protected_one(machine: Machine) -> None:
    inner = machine.workspace / "notes"
    inner.mkdir()
    assert mac_takeover._safe(inner, [machine.workspace]) is False


def test_safe_drops_an_ancestor_of_a_protected_path(machine: Machine) -> None:
    parent = machine.home / "tools"
    workspace = parent / "ciaobot" / "ws"
    workspace.mkdir(parents=True)
    assert mac_takeover._safe(parent / "ciaobot", [workspace]) is False


def test_safe_drops_home_and_paths_outside_it(machine: Machine, tmp_path: Path) -> None:
    assert mac_takeover._safe(machine.home, []) is False
    outside = tmp_path / "outside"
    outside.mkdir()
    assert mac_takeover._safe(outside, []) is False


def test_safe_refuses_relative_paths(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    (machine.home / "relative").mkdir()
    monkeypatch.chdir(machine.home)
    assert mac_takeover._safe(Path("relative"), []) is False


def test_safe_refuses_a_top_level_symlink_unless_allowed(machine: Machine) -> None:
    target = machine.home / "real"
    target.mkdir()
    link = machine.home / "link"
    link.symlink_to(target)
    assert mac_takeover._safe(link, []) is False
    assert mac_takeover._safe(link, [], allow_symlink=True) is True


def test_safe_resolves_symlinked_protected_paths(machine: Machine) -> None:
    real = machine.home / "real-ws"
    real.mkdir()
    link = machine.home / "ws-link"
    link.symlink_to(real)
    assert mac_takeover._safe(real, [link]) is False


def test_workspace_inside_the_env_drops_the_env_candidate(machine: Machine) -> None:
    machine.workspace = machine.env_root / "ws"
    (machine.workspace / ".runtime").mkdir(parents=True)
    (machine.workspace / ".runtime" / "workspaces.json").write_text("{}")
    installer_install(machine)
    result = run_plan(machine)
    assert "ciaobot" not in proofs(result)
    assert "install-receipt.json" in proofs(result)


def test_vault_in_dotenv_is_protected(machine: Machine) -> None:
    vault = machine.env_root / "vault"
    vault.mkdir()
    (machine.workspace / ".env").write_text(f"CIAO_VAULT_ROOT={vault}\n")
    installer_install(machine)
    assert "ciaobot" not in proofs(run_plan(machine))


# --- read-only -----------------------------------------------------------------


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p): (p.lstat().st_size, p.lstat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def test_plan_writes_nothing(machine: Machine) -> None:
    installer_install(machine)
    before = snapshot(machine.home)
    run_plan(machine)
    assert snapshot(machine.home) == before


# --- CLI -----------------------------------------------------------------------


def test_cli_prints_the_plan_as_json(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    installer_install(machine)
    code = cli.main(
        ["mac-takeover", "plan", "--json", "--launch-agents-dir", str(machine.agents), "--label", LABEL]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["action"] == "takeover"
    assert payload["workspace"] == str(machine.workspace)
    assert {item["proof"] for item in payload["trash"]} == {"receipt_env", "receipt", "installer_shim"}
    assert mac_takeover.TakeoverPlan(
        **{**payload, "trash": tuple(mac_takeover.TrashCandidate(**t) for t in payload["trash"]),
           "left_in_place": tuple(payload["left_in_place"]), "dropped_env": tuple(payload["dropped_env"])}
    ).trash


def test_cli_exits_3_with_a_reason_on_refusal(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    installer_install(machine)
    code = cli.main(
        ["mac-takeover", "plan", "--json", "--launch-agents-dir", str(machine.agents)]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["reason"] == "isolated_home_needs_test_label"


def test_cli_exits_1_on_an_unexpected_error(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(mac_takeover, "plan", boom)
    code = cli.main(["mac-takeover", "plan", "--json", "--label", LABEL])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["action"] == "error"


def test_module_keeps_its_import_boundary() -> None:
    source = Path(mac_takeover.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "server_host",
        "install_receipt",
        "install_discovery",
        "native_",
        "engine_migration",
        "desktop_install",
    ):
        assert f"import {forbidden}" not in source
        assert f"from ciao.{forbidden}" not in source
        assert f"from ciao import {forbidden}" not in source
