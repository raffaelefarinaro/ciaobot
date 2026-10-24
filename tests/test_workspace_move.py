"""`ciao workspace-move`: the plan, the path rewrites and the detached job."""

from __future__ import annotations

import errno
import json
import os
import io
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import workspace_move
from ciao.agent_paths import claude_project_slug
from ciao.server_host import (
    BUNDLE_ID,
    HOST_PROTOCOL,
    HOST_REVISION,
    REQUIRED_BUNDLE_FILES,
    SCHEMA_VERSION,
    HostOwnership,
)
from ciao.web.routes_api import (
    workspace_move_dirs_endpoint,
    workspace_move_plan_endpoint,
    workspace_move_status_endpoint,
)


def _workspace(root: Path) -> Path:
    (root / ".runtime" / "migration").mkdir(parents=True)
    (root / ".env").write_text("CIAO_WORKSPACE=.\nPWA_AUTH_TOKEN=secret\n", encoding="utf-8")
    return root


def _plan(source: Path, target: Path | str, **kwargs: Any) -> workspace_move.MovePlan:
    defaults: dict[str, Any] = {
        "registered": source,
        # A real, executable python: the host checks that the interpreter it
        # is given exists and is runnable, so the plan checks the same thing
        # before it lifts a refusal.
        "python": sys.executable,
        "platform": "darwin",
        "state_dir": source.parent / "state",
    }
    return workspace_move.plan(source, str(target), **{**defaults, **kwargs})


# ── plan ─────────────────────────────────────────────────────────────


def test_plan_refuses_a_protected_destination_or_source_without_a_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Desktop").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("ciao.setup_status.sys.platform", "darwin")

    plain = _workspace(home / "old")
    result = _plan(plain, home / "Documents" / "new", host=None)
    assert any("background job" in r for r in result.refusals)

    protected = _workspace(home / "Desktop" / "old")
    result = _plan(protected, home / "new", host=None)
    assert any(r.startswith("The workspace is inside ~/Desktop") for r in result.refusals)

    assert not any("background job" in r for r in _plan(plain, home / "new", host=None).refusals)


@pytest.mark.skipif(sys.platform == "win32", reason="the run-move argv is a POSIX launchd command")
def test_plan_warns_and_allows_a_protected_folder_through_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Desktop").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("ciao.setup_status.sys.platform", "darwin")

    plain = _workspace(home / "old")
    result = _plan(plain, home / "Documents" / "new", host=_verified_host())
    assert result.ok, result.refusals
    assert any(
        "Documents" in w and "if access is denied" in w for w in result.warnings
    ), result.warnings
    assert not any("asks once" in w for w in result.warnings)

    protected = _workspace(home / "Desktop" / "old")
    result = _plan(protected, home / "new", host=_verified_host())
    assert result.ok, result.refusals
    assert any(
        "Desktop" in w and "if access is denied" in w for w in result.warnings
    ), result.warnings


def test_plan_refuses_a_protected_folder_for_a_host_without_the_move_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("ciao.setup_status.sys.platform", "darwin")

    plain = _workspace(home / "old")
    result = _plan(plain, home / "Documents" / "new", host=_verified_host(host_protocol=1))
    assert not result.ok
    assert any("background job" in r for r in result.refusals), result.refusals


def test_plan_refuses_a_protected_folder_for_an_interpreter_the_host_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host's own parser demands a python executable, so an interpreter it
    # would refuse cannot run the move: the plan and the job must agree.
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("ciao.setup_status.sys.platform", "darwin")

    plain = _workspace(home / "old")
    result = _plan(
        plain,
        home / "Documents" / "new",
        host=_verified_host(),
        python="/opt/ciao/bin/ciao",
    )
    assert not result.ok
    assert any("background job" in r for r in result.refusals), result.refusals


def test_plan_refuses_a_protected_folder_for_an_interpreter_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host refuses a python that is not there to run, so the plan must
    # keep refusing too: a plan that lifted the refusal for a move the host
    # would refuse to launch sends the operator to a prompt that never
    # appears.
    #
    # The path is named like a python on purpose: a name the host's *name*
    # rule would accept, so the refusal can only come from the check that the
    # file is there and runnable.
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("ciao.setup_status.sys.platform", "darwin")

    plain = _workspace(home / "old")
    result = _plan(
        plain,
        home / "Documents" / "new",
        host=_verified_host(),
        python=str(tmp_path / "bin" / "python"),
    )
    assert not result.ok
    assert any("background job" in r for r in result.refusals), result.refusals


def test_plan_accepts_a_new_folder_and_an_empty_one(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    assert _plan(source, tmp_path / "new").ok
    (tmp_path / "empty").mkdir()
    assert _plan(source, tmp_path / "empty").ok


@pytest.mark.parametrize(
    ("target", "kwargs", "needle"),
    [
        ("full", {}, "not empty"),
        ("old/inner", {}, "inside the current workspace"),
        ("old", {}, "already in"),
        ("new", {"registered": None}, "No Ciaobot service"),
        ("new", {"registered": Path("/elsewhere")}, "runs the workspace at"),
        ("new", {"platform": "linux", "admin": False}, "the administrator moves"),
        ("new", {"platform": "freebsd14"}, "not supported"),
        ("new", {"update_in_flight": True}, "engine update"),
        ("new", {"engine_running": False}, "not running"),
        ("missing/new", {}, "does not exist"),
        ("relative", {}, "absolute"),
    ],
)
def test_plan_refusals(tmp_path: Path, target: str, kwargs: dict[str, Any], needle: str) -> None:
    source = _workspace(tmp_path / "old")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x", encoding="utf-8")
    raw = target if target == "relative" else str(tmp_path / target)
    result = _plan(source, raw, **kwargs)
    assert not result.ok
    assert any(needle in refusal for refusal in result.refusals), result.refusals


def test_plan_accepts_linux_as_root(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    assert _plan(source, tmp_path / "new", platform="linux", admin=True).ok


def test_plan_refuses_a_folder_the_service_account_cannot_reach(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    result = _plan(source, tmp_path / "new", platform="linux", admin=True, service_can_reach=lambda _p: False)
    assert any("service account cannot open" in r for r in result.refusals)


def test_plan_refuses_an_engine_python_inside_the_workspace(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    result = _plan(source, tmp_path / "new", python=str(source / ".venv" / "bin" / "python"))
    assert any("Python inside the workspace" in r for r in result.refusals)


def test_plan_warns_when_opencode_chats_lose_their_session(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    (source / ".runtime" / "web_projects.json").write_text(
        json.dumps(
            {
                "chats": {
                    "c-claude": {"provider": "claude", "title": "Claude"},
                    "c-open": {"provider": "opencode", "title": "OpenCode"},
                }
            }
        ),
        encoding="utf-8",
    )
    result = _plan(source, tmp_path / "new")
    assert result.ok
    assert any(
        "OpenCode chats continue from their transcript" in warning
        for warning in result.warnings
    ), result.warnings


def test_plan_skips_the_opencode_warning_without_such_a_chat(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    runtime = source / ".runtime"

    def warnings() -> list[str]:
        return _plan(source, tmp_path / "new").warnings

    # No file yet.
    assert not any("OpenCode" in warning for warning in warnings())

    (runtime / "web_projects.json").write_text("{", encoding="utf-8")
    assert not any("OpenCode" in warning for warning in warnings())

    (runtime / "web_projects.json").write_text(
        json.dumps({"chats": {"c1": {"provider": "claude"}}}),
        encoding="utf-8",
    )
    assert not any("OpenCode" in warning for warning in warnings())


def test_plan_warns_about_what_it_does_not_repair(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    (source / ".venv").mkdir()
    (source / ".runtime" / "schedules.json").write_text(
        json.dumps([{"prompt": f"read {source}/notes.md"}]), encoding="utf-8"
    )
    result = _plan(source, tmp_path / "new")
    assert result.ok
    assert any(".venv" in w for w in result.warnings)
    assert any("automation prompts" in w for w in result.warnings)


def test_plan_refuses_a_second_move_while_one_runs(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    state = tmp_path / "state"
    workspace_move.write_operation(
        workspace_move.MoveOperation("1", "moving", str(source), "/x", "", ""), state
    )
    handle = workspace_move._lock(state)
    try:
        assert any("already in progress" in r for r in _plan(source, tmp_path / "new").refusals)
    finally:
        handle.close()


def test_plan_treats_a_fresh_queued_record_as_running(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    now = workspace_move._now()
    workspace_move.write_operation(
        workspace_move.MoveOperation("1", "queued", str(source), "/x", now, now), tmp_path / "state"
    )
    assert any("already in progress" in r for r in _plan(source, tmp_path / "new").refusals)


def test_plan_ignores_a_job_that_died_before_anything_moved(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    workspace_move.write_operation(
        workspace_move.MoveOperation("1", "draining", str(source), "/x", "", ""), tmp_path / "state"
    )
    assert _plan(source, tmp_path / "new").ok


def test_plan_refuses_after_a_job_that_died_mid_move(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    workspace_move.write_operation(
        workspace_move.MoveOperation("1", "rewriting", str(source), "/x", "", ""), tmp_path / "state"
    )
    refusals = _plan(source, tmp_path / "new").refusals
    assert any("stopped during 'rewriting'" in r for r in refusals), refusals


# ── rewrites ─────────────────────────────────────────────────────────


def test_rewrite_prefix_matches_whole_components_only() -> None:
    assert workspace_move.rewrite_prefix("/a/ciao", "/a/ciao", "/b/c") == "/b/c"
    assert workspace_move.rewrite_prefix("/a/ciao/x/y", "/a/ciao", "/b/c") == "/b/c/x/y"
    assert workspace_move.rewrite_prefix("/a/ciaobot/x", "/a/ciao", "/b/c") == "/a/ciaobot/x"
    assert workspace_move.rewrite_prefix("relative/x", "/a/ciao", "/b/c") == "relative/x"


def test_rewrite_workspace_state(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = _workspace(tmp_path / "new")
    (new / ".env").write_text(
        f"CIAO_WORKSPACE=.\nCIAO_VAULT_ROOT={old}/memory-vault\nOTHER={old}bot/x\n# {old}\n",
        encoding="utf-8",
    )
    runtime = new / ".runtime"
    (runtime / "web_projects.json").write_text(
        json.dumps({"chats": {"c": {"helper": {"archive_path": f"{old}/Logs/c.md"}, "title": str(old)}}}),
        encoding="utf-8",
    )
    vault = old / "personal" / "memory-vault"
    from ciao import vault_migration

    old_receipt = vault_migration.receipt_path(runtime, vault)
    old_receipt.write_text(json.dumps({"vault_root": str(vault)}), encoding="utf-8")

    workspace_move.rewrite_workspace_state(new, str(old), str(new))

    env = (new / ".env").read_text(encoding="utf-8")
    assert f"CIAO_VAULT_ROOT={new}/memory-vault" in env
    assert f"OTHER={old}bot/x" in env
    assert f"# {old}" in env
    projects = json.loads((runtime / "web_projects.json").read_text(encoding="utf-8"))
    assert projects["chats"]["c"]["helper"]["archive_path"] == f"{new}/Logs/c.md"
    assert projects["chats"]["c"]["title"] == str(new)
    new_vault = new / "personal" / "memory-vault"
    new_vault.mkdir(parents=True)
    rekeyed = vault_migration.receipt_path(runtime, new_vault)
    assert rekeyed.is_file() and not old_receipt.exists()
    assert json.loads(rekeyed.read_text(encoding="utf-8"))["vault_root"] == str(new_vault)


def test_links_are_repointed_after_the_folder_moves(tmp_path: Path) -> None:
    from ciao.os_support import links

    old, new = tmp_path / "old", tmp_path / "new"
    (old / "skills" / "remember").mkdir(parents=True)
    (old / ".claude" / "skills").mkdir(parents=True)
    # Absolute, as sync-skills writes command mirrors (a junction on Windows).
    links.link_dir(old / "skills" / "remember", old / ".claude" / "skills" / "remember")
    os.rename(old, new)

    assert workspace_move.relink_symlinks(new, str(old), str(new)) == 1
    assert links.points_to(new / ".claude" / "skills" / "remember", new / "skills" / "remember")


def test_claude_sessions_are_copied_and_trust_rekeyed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old, new = tmp_path / "old", tmp_path / "new"
    (new / "personal").mkdir(parents=True)
    projects = home / ".claude" / "projects"
    for root in (old, old / "personal"):
        (projects / claude_project_slug(root)).mkdir(parents=True)
        (projects / claude_project_slug(root) / "s.jsonl").write_text("{}", encoding="utf-8")
    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(old): {"trusted": True}, "/other": {}}}), encoding="utf-8"
    )

    copied = workspace_move.copy_claude_sessions(old, new, home=home)
    workspace_move.rekey_claude_json(str(old), str(new), home=home)

    assert sorted(copied) == sorted([claude_project_slug(new), claude_project_slug(new / "personal")])
    assert (projects / claude_project_slug(old) / "s.jsonl").is_file()
    assert (projects / claude_project_slug(new / "personal") / "s.jsonl").is_file()
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert data["projects"][str(new)] == {"trusted": True}
    assert str(old) in data["projects"]


# ── the detached job ─────────────────────────────────────────────────


class _Host:
    def __init__(self, *, start_ok: bool = True, stops: bool = True) -> None:
        self.calls: list[str] = []
        self.up = True
        self.start_ok = start_ok
        self.stops = stops

    def engine_port(self) -> int:
        return 8443

    def stop_engine(self, wait: Any = None) -> bool:
        self.calls.append("stop")
        if self.stops:
            self.up = False
        return wait() if wait is not None else True

    def start_engine(self) -> Any:
        self.calls.append("start")
        self.up = True
        ok = self.start_ok
        self.start_ok = True  # the rollback's start succeeds
        return SimpleNamespace(ok=ok, message="" if ok else "launchctl failed")


class _Service:
    def __init__(self, path: Path, workspace: Path) -> None:
        self.path = path
        path.write_text(str(workspace), encoding="utf-8")

    def workspace(self) -> Path | None:
        return Path(self.path.read_text(encoding="utf-8"))

    def backup(self, dest: Path) -> None:
        dest.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")

    def repoint(self, old: Path, new: Path) -> None:
        self.path.write_text(str(new), encoding="utf-8")

    def restore(self, backup: Path) -> None:
        self.path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")

    def preflight(self) -> None:
        pass


def _run(
    tmp_path: Path,
    host: _Host,
    *,
    drained: bool = True,
    service_type: type[_Service] = _Service,
    prepare: Any = None,
    post_override: Callable[[str], dict[str, Any]] | None = None,
    get_override: Callable[[str], dict[str, Any] | None] | None = None,
    platform: str | None = None,
) -> tuple[workspace_move.MoveOperation, _Service, list[str]]:
    source = _workspace(tmp_path / "old")
    if prepare is not None:
        prepare(source)
    target = tmp_path / "new"
    state = tmp_path / "state"
    state.mkdir()
    service = service_type(tmp_path / "definition", source)
    op = workspace_move.MoveOperation("op1", "queued", str(source), str(target), "", "", port=8443)
    posts: list[str] = []
    clock = iter(range(0, 100_000, 10))

    def post(url: str) -> dict[str, Any]:
        posts.append(url)
        if post_override is not None:
            return post_override(url)
        return {"active_chat_ids": [] if drained else ["busy"]}

    def get(url: str) -> dict[str, Any] | None:
        if get_override is not None:
            return get_override(url)
        return {"overall_ready": True} if host.up else None

    result = workspace_move.run_move(
        op,
        state_dir=state,
        host=host,
        service=service,
        get=get,
        post=post,
        sleep=lambda _s: None,
        clock=lambda: float(next(clock)),
        claude_home=tmp_path / "home",
        platform=platform,
    )
    return result, service, posts


def test_run_move_moves_repoints_and_restarts(tmp_path: Path) -> None:
    host = _Host()
    result, service, _posts = _run(tmp_path, host)
    assert result.phase == "done", result.error
    assert not (tmp_path / "old").exists()
    assert (tmp_path / "new" / ".env").is_file()
    assert service.workspace() == tmp_path / "new"
    assert host.calls == ["stop", "start"]
    assert workspace_move.read_operation(tmp_path / "state").phase == "done"


def test_protected_destination_probe_denial_does_not_drain_or_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import workspace_access

    denial = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: denial)
    source = _workspace(tmp_path / "old")
    target = tmp_path / "new"
    target.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    service = _Service(tmp_path / "definition", source)
    host = _Host()

    def deny_probe(_directory: Path) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(workspace_move, "_probe_destination_access", deny_probe)
    op = workspace_move.MoveOperation("op1", "queued", str(source), str(target), "", "", port=8443)
    result = workspace_move.run_move(
        op,
        state_dir=state,
        host=host,
        service=service,
        get=lambda _url: {"overall_ready": True},
        post=lambda _url: {"active_chat_ids": []},
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
        platform="darwin",
    )

    assert result.phase == "failed"
    assert result.denied is True
    assert "System Settings" in result.error
    assert host.calls == []
    assert source.is_dir()
    assert service.workspace() == source


def test_destination_probe_eacces_is_not_recorded_as_privacy_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import workspace_access

    denial = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: denial)

    def deny_probe(_directory: Path) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(workspace_move, "_probe_destination_access", deny_probe)
    source = _workspace(tmp_path / "old")
    state = tmp_path / "state"
    state.mkdir()
    service = _Service(tmp_path / "definition", source)
    host = _Host()
    result = workspace_move.run_move(
        workspace_move.MoveOperation(
            "op1", "queued", str(source), str(tmp_path / "new"), "", "", port=8443
        ),
        state_dir=state,
        host=host,
        service=service,
        get=lambda _url: {"overall_ready": True},
        post=lambda _url: {"active_chat_ids": []},
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
        platform="darwin",
    )

    assert result.phase == "failed"
    assert result.denied is False
    assert "Permission denied" in result.error
    assert not denial.exists()
    assert host.calls == []
    assert service.workspace() == source


def test_run_move_rolls_back_when_the_engine_does_not_start(tmp_path: Path) -> None:
    host = _Host(start_ok=False)
    result, service, _posts = _run(tmp_path, host)
    assert result.phase == "rolled_back"
    assert "launchctl failed" in result.error
    assert (tmp_path / "old" / ".env").is_file()
    assert not (tmp_path / "new").exists()
    assert service.workspace() == tmp_path / "old"
    assert host.calls == ["stop", "start", "stop", "start"]


def test_engine_exit_78_aborts_readiness_wait_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import macos_service

    exit_codes = iter([0, 78])
    monkeypatch.setattr(macos_service, "server_last_exit_code", lambda: next(exit_codes))
    host = _Host()
    starts = 0

    def status(_url: str) -> dict[str, Any] | None:
        nonlocal starts
        if not host.up:
            return None
        starts = host.calls.count("start")
        return {"overall_ready": starts > 1}

    result, service, _posts = _run(
        tmp_path, host, get_override=status, platform="darwin"
    )

    assert result.phase == "rolled_back", result.error
    assert result.denied is True
    assert result.startup_exit_code == 78
    assert "System Settings" in result.error
    assert service.workspace() == tmp_path / "old"
    assert host.calls == ["stop", "start", "stop", "start"]
    persisted = workspace_move.read_operation(tmp_path / "state")
    assert persisted is not None
    assert persisted.startup_exit_code == 78
    assert persisted.denied is True


def test_stale_denial_does_not_fail_a_new_successful_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import macos_service, workspace_access

    denial = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: denial)
    workspace_access.record_denial("previous attempt", denial)
    # The forward wait reads launchd's last exit code, and that read has no
    # uid to build the domain from on Windows. The stale denial is the subject.
    monkeypatch.setattr(macos_service, "server_last_exit_code", lambda: 0)

    result, _service, _posts = _run(tmp_path, _Host(), platform="darwin")

    assert result.phase == "done", result.error
    assert result.denied is False


def test_recorded_startup_denial_survives_rollback_and_access_record_clearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import workspace_access

    denial = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: denial)
    host = _Host()
    starts = 0

    def status(_url: str) -> dict[str, Any] | None:
        nonlocal starts
        if not host.up:
            return None
        starts = host.calls.count("start")
        if starts == 1:
            workspace_access.record_denial("macOS denied destination", denial)
            return {"overall_ready": False}
        workspace_access.clear_denial(denial)
        return {"overall_ready": True}

    monkeypatch.setattr(
        "ciao.macos_service.server_last_exit_code",
        lambda: 0,
    )
    result, _service, _posts = _run(
        tmp_path, host, get_override=status, platform="darwin"
    )
    persisted = workspace_move.read_operation(tmp_path / "state")

    assert result.phase == "rolled_back"
    assert result.denied is True
    assert "macOS denied destination" in result.error
    assert not denial.exists()
    assert persisted is not None
    assert persisted.denied is True
    assert "macOS denied destination" in persisted.error


def test_rollback_wait_ignores_forward_denial_until_source_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import macos_service, workspace_access

    denial = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: denial)
    exit_codes = iter([0, 78])
    monkeypatch.setattr(
        macos_service, "server_last_exit_code", lambda: next(exit_codes)
    )
    host = _Host()
    rollback_polls = 0

    def status(_url: str) -> dict[str, Any] | None:
        nonlocal rollback_polls
        if not host.up:
            return None
        starts = host.calls.count("start")
        if starts == 1:
            return {"overall_ready": False}
        rollback_polls += 1
        if rollback_polls < 3:
            return {"overall_ready": False}
        workspace_access.clear_denial(denial)
        return {"overall_ready": True}

    result, service, _posts = _run(
        tmp_path, host, get_override=status, platform="darwin"
    )
    persisted = workspace_move.read_operation(tmp_path / "state")

    assert result.phase == "rolled_back", result.error
    assert result.denied is True
    assert result.startup_exit_code == 78
    assert "System Settings" in result.error
    assert service.workspace() == tmp_path / "old"
    assert rollback_polls == 3
    assert not denial.exists()
    assert persisted is not None
    assert persisted.phase == "rolled_back"
    assert persisted.denied is True
    assert persisted.startup_exit_code == 78
    assert result.error == persisted.error


def test_run_move_leaves_everything_when_chats_never_drain(tmp_path: Path) -> None:
    host = _Host()
    result, _service, posts = _run(tmp_path, host, drained=False)
    assert result.phase == "failed"
    assert "still running" in result.error
    assert (tmp_path / "old" / ".env").is_file()
    assert host.calls == []
    assert posts[-1].endswith("/api/admin/drain/cancel")


def test_run_move_drain_poll_timeout_names_the_step(tmp_path: Path) -> None:
    host = _Host()

    def override(url: str) -> dict[str, Any]:
        if url.endswith("/drain/cancel"):
            return {}
        raise TimeoutError("timed out")

    result, _service, posts = _run(tmp_path, host, post_override=override)
    assert result.phase == "failed"
    assert "did not answer the drain status poll within 30s" in result.error
    assert "nothing was moved" in result.error
    assert result.error != "timed out"
    assert host.calls == []
    assert (tmp_path / "old" / ".env").is_file()
    assert posts[-1].endswith("/api/admin/drain/cancel")


def test_run_move_drain_refused_reports_the_engine_reason(tmp_path: Path) -> None:
    host = _Host()

    def override(url: str) -> dict[str, Any]:
        if url.endswith("/drain/cancel"):
            return {}
        raise urllib.error.HTTPError(
            url,
            409,
            "Conflict",
            None,  # type: ignore[arg-type]
            io.BytesIO(b'{"error": "a restart is already draining"}'),
        )

    result, _service, _posts = _run(tmp_path, host, post_override=override)
    assert result.phase == "failed"
    assert "refused the drain status poll (HTTP 409: a restart is already draining)" in result.error


def test_run_move_failed_cancel_is_recorded(tmp_path: Path) -> None:
    host = _Host()

    def override(url: str) -> dict[str, Any]:
        if url.endswith("/drain/cancel"):
            raise TimeoutError("timed out")
        raise urllib.error.URLError(TimeoutError())

    result, _service, _posts = _run(tmp_path, host, post_override=override)
    assert result.phase == "failed"
    assert (
        "reopening chats also failed (the running engine did not answer the drain cancel within 30s"
        in result.error
    )


def test_drain_post_uses_the_drain_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[float] = []

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_open(_request: Any, timeout: float) -> _Response:
        timeouts.append(timeout)
        return _Response()

    monkeypatch.setattr(workspace_move._LOCAL_OPENER, "open", fake_open)
    workspace_move._post_json("http://127.0.0.1:1/api/admin/drain")
    workspace_move._get_json("http://127.0.0.1:1/api/startup-status")
    assert timeouts == [30.0, 5.0]


def test_run_move_restarts_the_engine_when_it_will_not_stop(tmp_path: Path) -> None:
    host = _Host(stops=False)
    result, _service, _posts = _run(tmp_path, host)
    assert result.phase == "failed"
    assert host.calls == ["stop", "start"]
    assert (tmp_path / "old").is_dir()


class _HalfRepointService(_Service):
    """Rewrites the definition, then fails to register it (the Windows shape)."""

    def repoint(self, old: Path, new: Path) -> None:
        super().repoint(old, new)
        raise RuntimeError("schtasks /Create failed")


def test_run_move_restores_a_definition_the_repoint_half_wrote(tmp_path: Path) -> None:
    host = _Host()
    result, service, _posts = _run(tmp_path, host, service_type=_HalfRepointService)
    assert result.phase == "rolled_back", result.error
    assert service.workspace() == tmp_path / "old"
    assert (tmp_path / "old" / ".env").is_file()


class _StopsOnceHost(_Host):
    """Stops for the move, but not for the rollback."""

    def stop_engine(self, wait: Any = None) -> bool:
        if "stop" in self.calls:
            self.stops = False
        return super().stop_engine(wait)


def test_run_move_does_not_move_back_under_an_engine_that_will_not_stop(tmp_path: Path) -> None:
    host = _StopsOnceHost(start_ok=False)
    result, _service, _posts = _run(tmp_path, host)
    assert result.phase == "rollback_failed"
    assert "did not stop" in result.error
    assert (tmp_path / "new" / ".env").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics")
def test_run_move_waits_for_the_engine_to_release_its_lock(tmp_path: Path) -> None:
    from ciao.os_support.locks import lock_exclusive

    held: list[Any] = []

    def hold_lock(source: Path) -> None:
        handle = (source / ".runtime" / "server.lock").open("a+")
        lock_exclusive(handle.fileno(), blocking=False)
        held.append(handle)

    host = _Host()
    try:
        result, _service, _posts = _run(tmp_path, host, prepare=hold_lock)
    finally:
        for handle in held:
            handle.close()
    assert result.phase == "failed"
    assert host.calls == ["stop", "start"]
    assert (tmp_path / "old" / ".env").is_file()


# ── routes ───────────────────────────────────────────────────────────


def _client(tmp_path: Path, *, peer: str) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/workspace-move", workspace_move_status_endpoint, methods=["GET"]),
            Route("/api/workspace-move/dirs", workspace_move_dirs_endpoint, methods=["GET"]),
            Route("/api/workspace-move/plan", workspace_move_plan_endpoint, methods=["POST"]),
        ]
    )
    app.state.config = SimpleNamespace(workspace_root=str(tmp_path), pwa_port=8443)
    return TestClient(app, base_url="http://localhost:8443", client=(peer, 50000))


def test_routes_are_for_this_computer_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_move, "default_state_dir", lambda: tmp_path / "state")
    remote = _client(tmp_path, peer="100.64.0.7")
    status = remote.get("/api/workspace-move").json()
    assert status["local"] is False
    assert remote.get("/api/workspace-move/dirs").status_code == 403
    assert remote.post("/api/workspace-move/plan", json={"target": "/x"}).status_code == 403

    local = _client(tmp_path, peer="127.0.0.1")
    assert local.get("/api/workspace-move").json()["local"] is True
    listing = local.get("/api/workspace-move/dirs", params={"path": str(tmp_path)})
    assert listing.status_code == 200
    assert listing.json()["path"] == str(tmp_path.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="the plan's service read is the macOS one here")
def test_plan_route_returns_the_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_move, "registered_workspace", lambda: tmp_path)
    monkeypatch.setattr(workspace_move, "update_in_flight", lambda: False)
    monkeypatch.setattr(workspace_move, "engine_running", lambda: True)
    # The route asks the machine it runs on; on Linux the plan refuses the
    # platform before anything else.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(workspace_move, "default_state_dir", lambda: tmp_path / "state")
    body = _client(tmp_path, peer="127.0.0.1").post(
        "/api/workspace-move/plan", json={"target": str(tmp_path / "inside")}
    ).json()
    assert body["ok"] is False
    assert any("inside the current workspace" in r for r in body["refusals"])


# ── Linux: the systemd unit ──────────────────────────────────────────


_UNIT = """[Service]
Type=simple
User=ciaobot
WorkingDirectory=/srv/ciao%%x
Environment="CIAO_WORKSPACE=/srv/ciao%x"
Environment="HOME=/var/lib/ciaobot"
Environment="PATH=/opt/ciaobot/venv/bin:/var/lib/ciaobot/.local/bin:/usr/bin"
ExecStart="/opt/ciaobot/venv/bin/python" -m ciao.cli run
"""


def _calls() -> tuple[list[tuple[str, ...]], Any]:
    calls: list[tuple[str, ...]] = []

    def systemctl(*args: str) -> Any:
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return calls, systemctl


@pytest.mark.skipif(sys.platform == "win32", reason="systemd units hold POSIX paths")
def test_linux_unit_reads_and_repoints_the_workspace(tmp_path: Path) -> None:
    unit = tmp_path / "ciaobot.service"
    unit.write_text(_UNIT, encoding="utf-8")
    calls, systemctl = _calls()
    definition = workspace_move.LinuxServiceDefinition(unit, systemctl=systemctl)
    assert definition.workspace() == Path("/srv/ciao%x")
    assert definition.home() == Path("/var/lib/ciaobot")

    backup = tmp_path / "backup"
    definition.backup(backup)
    definition.repoint(Path("/srv/ciao%x"), Path("/data/ciao%x"))

    text = unit.read_text(encoding="utf-8")
    assert "WorkingDirectory=/data/ciao%%x\n" in text
    assert 'Environment="CIAO_WORKSPACE=/data/ciao%x"' in text
    assert "/opt/ciaobot/venv/bin/python" in text
    assert definition.workspace() == Path("/data/ciao%x")
    assert calls == [("daemon-reload",)]

    definition.restore(backup)
    assert unit.read_text(encoding="utf-8") == _UNIT
    assert calls == [("daemon-reload",), ("daemon-reload",)]


@pytest.mark.skipif(sys.platform == "win32", reason="systemd units hold POSIX paths")
def test_linux_unit_refuses_a_repoint_that_changes_nothing(tmp_path: Path) -> None:
    unit = tmp_path / "ciaobot.service"
    unit.write_text(_UNIT, encoding="utf-8")
    _calls_list, systemctl = _calls()
    definition = workspace_move.LinuxServiceDefinition(unit, systemctl=systemctl)
    with pytest.raises(workspace_move.MoveError):
        definition.repoint(Path("/elsewhere"), Path("/data/x"))
    assert unit.read_text(encoding="utf-8") == _UNIT


def test_linux_host_drives_systemctl_and_reads_the_port(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "ws")
    (workspace / ".env").write_text("PWA_PORT=9443\n", encoding="utf-8")
    calls, systemctl = _calls()
    host = workspace_move.LinuxEngineHost(workspace, systemctl=systemctl)
    assert host.engine_port() == 9443
    assert host.start_engine().ok is True
    assert host.stop_engine(wait=lambda: True) is True
    assert calls == [("start", "ciaobot.service"), ("stop", "ciaobot.service")]


@pytest.mark.skipif(sys.platform == "win32", reason="systemd units hold POSIX paths")
def test_linux_unit_repoints_once_when_the_new_path_ends_with_the_old(tmp_path: Path) -> None:
    from ciao.linux_service import render_service

    unit = tmp_path / "ciaobot.service"
    unit.write_text(
        render_service(
            workspace=Path("/srv/ciaobot"),
            user="ciaobot",
            home=Path("/var/lib/ciaobot"),
            python=Path("/opt/ciaobot/venv/bin/python"),
        ),
        encoding="utf-8",
    )
    _calls_list, systemctl = _calls()
    definition = workspace_move.LinuxServiceDefinition(unit, systemctl=systemctl)
    definition.repoint(Path("/srv/ciaobot"), Path("/home/srv/ciaobot"))
    text = unit.read_text(encoding="utf-8")
    assert "WorkingDirectory=/home/srv/ciaobot\n" in text
    assert 'Environment="CIAO_WORKSPACE=/home/srv/ciaobot"' in text
    assert definition.workspace() == Path("/home/srv/ciaobot")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX accounts")
def test_linux_unit_home_falls_back_to_the_service_account(tmp_path: Path) -> None:
    import getpass
    import pwd

    me = getpass.getuser()
    unit = tmp_path / "ciaobot.service"
    unit.write_text(f"[Service]\nUser={me}\nWorkingDirectory=/srv/ciaobot\n", encoding="utf-8")
    definition = workspace_move.LinuxServiceDefinition(unit)
    assert definition.home() == Path(pwd.getpwnam(me).pw_dir)


def test_start_route_refuses_on_linux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.web.routes_api import workspace_move_start_endpoint

    monkeypatch.setattr(sys, "platform", "linux")
    app = Starlette(routes=[Route("/api/workspace-move", workspace_move_start_endpoint, methods=["POST"])])
    app.state.config = SimpleNamespace(workspace_root=str(tmp_path), pwa_port=8443)
    client = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000))
    response = client.post("/api/workspace-move", json={"target": str(tmp_path.parent / "new")})
    assert response.status_code == 403
    assert "administrator" in response.json()["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="systemd units hold POSIX paths")
def test_linux_unit_finds_drop_ins_that_name_the_workspace(tmp_path: Path) -> None:
    unit = tmp_path / "ciaobot.service"
    unit.write_text(_UNIT, encoding="utf-8")
    drop_ins = tmp_path / "ciaobot.service.d"
    drop_ins.mkdir()
    (drop_ins / "override.conf").write_text('[Service]\nEnvironment="CIAO_WORKSPACE=/srv/ciao%x"\n', encoding="utf-8")
    (drop_ins / "limits.conf").write_text("[Service]\nLimitNOFILE=8192\n", encoding="utf-8")
    definition = workspace_move.LinuxServiceDefinition(unit, systemctl=_calls()[1])
    assert definition.drop_ins_naming(Path("/srv/ciao%x")) == [drop_ins / "override.conf"]


# ── refusals the service makes, and the rollback that follows ────────


class _RefusingService(_Service):
    def preflight(self) -> None:
        raise workspace_move.MoveError("Windows refused to update the Ciaobot logon task")


def test_run_move_stops_before_moving_when_the_definition_cannot_change(tmp_path: Path) -> None:
    host = _Host()
    result, service, _posts = _run(tmp_path, host, service_type=_RefusingService)
    assert result.phase == "failed"
    assert "refused to update" in result.error
    assert (tmp_path / "old" / ".env").is_file() and not (tmp_path / "new").exists()
    assert service.workspace() == tmp_path / "old"
    assert host.calls == ["stop", "start"]


class _RepointAndRestoreFail(_Service):
    def repoint(self, old: Path, new: Path) -> None:
        raise workspace_move.MoveError("Access is denied.")

    def restore(self, backup: Path) -> None:
        raise workspace_move.MoveError("Access is denied.")


def test_rollback_keeps_going_when_a_step_fails(tmp_path: Path) -> None:
    host = _Host()
    result, _service, _posts = _run(tmp_path, host, service_type=_RepointAndRestoreFail)
    assert result.phase == "rollback_failed"
    assert "restoring the service definition" in result.error
    # The folder still went back and the engine was started last.
    assert (tmp_path / "old" / ".env").is_file() and not (tmp_path / "new").exists()
    assert host.calls[-1] == "start"


def test_rollback_moves_the_folder_back_before_rewriting_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows links (hard links, junctions) need their target to exist, so the
    # paths under the old root must be real again before they are rewritten.
    seen: list[tuple[str, bool]] = []
    real = workspace_move.rewrite_workspace_state

    def recording(root: Path, old: str, new: str) -> None:
        seen.append((new, Path(new).is_dir()))
        real(root, old, new)

    monkeypatch.setattr(workspace_move, "rewrite_workspace_state", recording)
    result, _service, _posts = _run(tmp_path, _Host(start_ok=False))
    assert result.phase == "rolled_back"
    assert seen[-1] == (str(tmp_path / "old"), True)


class _RefusingHost(_Host):
    """Will not start again after a preflight refusal."""

    def start_engine(self) -> Any:
        self.calls.append("start")
        return SimpleNamespace(ok=False, message="schtasks /Run failed")


def test_a_refusal_says_when_the_engine_did_not_start_again(tmp_path: Path) -> None:
    host = _RefusingHost()
    result, _service, _posts = _run(tmp_path, host, service_type=_RefusingService)
    assert result.phase == "failed"
    assert "refused to update" in result.error
    assert "did not start again: schtasks /Run failed" in result.error


class _RollbackStopRaisesHost(_Host):
    def stop_engine(self, wait: Any = None) -> bool:
        if "stop" in self.calls:
            raise OSError("launchctl is gone")
        return super().stop_engine(wait)


def test_a_rollback_stop_that_raises_is_recorded(tmp_path: Path) -> None:
    result, _service, _posts = _run(tmp_path, _RollbackStopRaisesHost(start_ok=False))
    assert result.phase == "rollback_failed"
    assert "launchctl is gone" in result.error
    assert (tmp_path / "new" / ".env").is_file()


def test_rollback_does_not_start_the_engine_at_a_missing_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == tmp_path / "old":
            raise OSError("The process cannot access the file")
        real_rename(src, dst)

    monkeypatch.setattr(workspace_move.os, "rename", rename)
    host = _Host(start_ok=False)
    result, service, _posts = _run(tmp_path, host)
    assert result.phase == "rollback_failed"
    assert "moving the folder back" in result.error
    assert "not starting the engine" in result.error
    assert service.workspace() == tmp_path / "old"
    assert host.calls == ["stop", "start", "stop"]


def test_windows_preflight_explains_a_refused_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import windows_service

    def refuse(_definition: Path, name: str = "") -> None:
        raise windows_service.WindowsServiceError("schtasks /Create failed: Access is denied.")

    monkeypatch.setattr(windows_service, "register_task", refuse)
    definition = workspace_move.WindowsServiceDefinition(tmp_path / "task.xml")
    with pytest.raises(workspace_move.MoveError, match="elevated shell"):
        definition.preflight()


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits, not root",
)
@pytest.mark.parametrize("kind", ["mac", "linux"])
def test_preflight_refuses_a_definition_folder_it_cannot_write(tmp_path: Path, kind: str) -> None:
    folder = tmp_path / "defs"
    folder.mkdir()
    path = folder / "definition"
    path.write_text("x", encoding="utf-8")
    definition: Any = (
        workspace_move.MacServiceDefinition(path)
        if kind == "mac"
        else workspace_move.LinuxServiceDefinition(path, systemctl=lambda *_a: None)
    )
    definition.preflight()
    folder.chmod(0o500)
    try:
        with pytest.raises(workspace_move.MoveError, match="cannot rewrite"):
            definition.preflight()
    finally:
        folder.chmod(0o700)


def test_the_mac_move_job_shares_the_engine_bundle_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import plistlib

    from ciao import macos_service

    engine = tmp_path / "com.ciao.server.plist"
    engine.write_bytes(plistlib.dumps({"Label": "com.ciao.server", "AssociatedBundleIdentifiers": ["local.ciaobot.server"]}))
    agents = tmp_path / "agents"
    agents.mkdir()
    engine.rename(agents / engine.name)
    monkeypatch.setattr(macos_service, "default_launch_agents_dir", lambda: agents)
    calls: list[list[str]] = []
    monkeypatch.setattr(macos_service, "_launchctl", lambda args, **_kw: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    op = workspace_move.MoveOperation("op1", "queued", "/a", "/b", "", "", port=8443, python="/py")
    workspace_move._spawn_mac_job(op, tmp_path)
    job = plistlib.loads((tmp_path / workspace_move.JOB_PLIST_NAME).read_bytes())
    assert job["AssociatedBundleIdentifiers"] == ["local.ciaobot.server"]
    assert calls[-1][0] == "bootstrap"


# ── the move runs through the verified host ──────────────────────────


#: The host the move runs under. The bundle path is a POSIX path, because the
#: host's own argv rules are POSIX paths and a Windows ``tmp_path`` is not one.
_HOST_BUNDLE = "/Users/me/Applications/Ciaobot Server.app"
#: The interpreter the host would run. It has to be a real, executable python:
#: the host's own parser checks that, so a path that does not exist would be
#: refused at launch - and the move checks the same thing before it lifts the
#: refusal, so the plan and the job can never disagree.
_HOST_PYTHON = sys.executable


def _verified_host(*, host_protocol: int = HOST_PROTOCOL) -> HostOwnership:
    """A snapshot as ``verify_owned_host`` would return it."""
    digest = "a" * 64
    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=_HOST_BUNDLE,
        bundle_id=BUNDLE_ID,
        host_revision=HOST_REVISION,
        host_protocol=host_protocol,
        executable_sha256=digest,
        per_arch_cdhashes={"arm64": "b" * 40, "x86_64": "c" * 40},
        bundle_files={name: digest for name in sorted(REQUIRED_BUNDLE_FILES)},
    )


def _spawn_mac_job_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, op: workspace_move.MoveOperation
) -> dict[str, Any]:
    """Spawn the macOS move job and read back the plist launchd was handed."""
    import plistlib

    from ciao import macos_service

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(macos_service, "default_launch_agents_dir", lambda: agents)
    monkeypatch.setattr(
        macos_service,
        "_launchctl",
        lambda args, **_kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(macos_service, "_getuid", lambda: 501)
    workspace_move._spawn_mac_job(op, tmp_path)
    job: dict[str, Any] = plistlib.loads(
        (tmp_path / workspace_move.JOB_PLIST_NAME).read_bytes()
    )
    return job


@pytest.mark.skipif(sys.platform == "win32", reason="the run-move argv is a POSIX launchd command")
def test_the_mac_move_job_runs_through_the_verified_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_move, "_verified_host_if_installed", lambda: _verified_host())
    op = workspace_move.MoveOperation(
        "op1", "queued", "/a", "/b", "", "", port=8443, python=_HOST_PYTHON
    )

    job = _spawn_mac_job_plist(tmp_path, monkeypatch, op)

    assert job["ProgramArguments"] == [
        f"{_HOST_BUNDLE}/Contents/MacOS/Ciaobot Server",
        "run-move",
        "--python",
        _HOST_PYTHON,
        "--operation",
        "op1",
        "--state-dir",
        str(tmp_path),
    ]


def test_the_mac_move_job_stays_a_bare_interpreter_without_a_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_move, "_verified_host_if_installed", lambda: None)
    op = workspace_move.MoveOperation(
        "op1", "queued", "/a", "/b", "", "", port=8443, python=_HOST_PYTHON
    )

    job = _spawn_mac_job_plist(tmp_path, monkeypatch, op)

    assert job["ProgramArguments"] == [
        _HOST_PYTHON,
        "-I",
        "-m",
        "ciao.workspace_move",
        "run",
        "--operation",
        "op1",
        "--state-dir",
        str(tmp_path),
    ]


def test_the_mac_move_job_stays_a_bare_interpreter_for_an_old_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A host too old to speak protocol 2 has no ``run-move`` operation.
    monkeypatch.setattr(
        workspace_move, "_verified_host_if_installed", lambda: _verified_host(host_protocol=1)
    )
    op = workspace_move.MoveOperation(
        "op1", "queued", "/a", "/b", "", "", port=8443, python=_HOST_PYTHON
    )

    job = _spawn_mac_job_plist(tmp_path, monkeypatch, op)

    assert job["ProgramArguments"][0] == _HOST_PYTHON
    assert "run-move" not in job["ProgramArguments"]


# ── a denied folder is a first-class outcome ─────────────────────────


def test_a_denied_folder_is_recorded_and_named_in_the_move_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import workspace_access

    record = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: record)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == tmp_path / "new":
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(workspace_move.os, "rename", rename)
    result, _service, _posts = _run(tmp_path, _Host(), platform="darwin")

    assert result.phase == "rolled_back", result.error
    # The folder is named, and the refusal is not a bare traceback.
    assert str(tmp_path / "new") in result.error
    assert "Traceback" not in result.error
    # The denial is recorded where `ciao service status` reads it.
    recorded = record.read_text(encoding="utf-8")
    assert str(tmp_path / "new") in recorded
    assert "Files and Folders" in recorded
    # And the record says it was a denial, not a failed move.
    assert result.denied is True
    assert workspace_move.read_operation(tmp_path / "state").denied is True


def test_a_denied_move_exits_with_the_denial_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import workspace_access
    from ciao.workspace_access import ACCESS_DENIED_EXIT

    record = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: record)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == tmp_path / "new":
            raise PermissionError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(workspace_move.os, "rename", rename)
    state = tmp_path / "state"
    state.mkdir()
    source = _workspace(tmp_path / "old")
    op = workspace_move.MoveOperation(
        "op1", "queued", str(source), str(tmp_path / "new"), "", "", port=8443
    )
    workspace_move.write_operation(op, state)
    monkeypatch.setattr(workspace_move, "current_service_definition", lambda: _Service(tmp_path / "definition", source))
    monkeypatch.setattr(workspace_move, "current_engine_host", lambda _ws: _Host())
    monkeypatch.setattr(
        workspace_move,
        "run_move",
        lambda recorded, **_kw: workspace_move.MoveOperation(
            recorded.id, "rolled_back", recorded.source, recorded.target, "", "",
            error="macOS denied Ciaobot Server access", denied=True,
        ),
    )

    assert workspace_move.main(["run", "--operation", "op1", "--state-dir", str(state)]) == ACCESS_DENIED_EXIT


def test_a_plain_permission_error_is_not_a_privacy_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # EACCES is an ordinary mode-bit denial, not a privacy grant the user
    # refused: it is reported as itself and nothing is recorded.
    from ciao import workspace_access

    record = tmp_path / "denied.txt"
    monkeypatch.setattr(workspace_access, "denial_path", lambda: record)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == tmp_path / "new":
            raise PermissionError(errno.EACCES, "Permission denied")
        real_rename(src, dst)

    monkeypatch.setattr(workspace_move.os, "rename", rename)
    result, _service, _posts = _run(tmp_path, _Host(), platform="darwin")

    assert result.phase == "rolled_back", result.error
    assert "Permission denied" in result.error
    assert not record.exists()


def test_plan_warns_about_a_cloud_synced_destination(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "old")
    (Path.home() / "Dropbox").mkdir(parents=True)
    plan = _plan(source, Path.home() / "Dropbox" / "moved", platform="linux", admin=True)
    assert plan.ok
    assert any("Dropbox" in w for w in plan.warnings)


def test_status_reports_cloud_sync_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workspace_move, "default_state_dir", lambda: tmp_path / "state")
    status = _client(tmp_path, peer="127.0.0.1").get("/api/workspace-move").json()
    assert status["cloud_sync_warning"] == ""
