"""`ciao workspace-move`: the plan, the path rewrites and the detached job."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import workspace_move
from ciao.agent_paths import claude_project_slug
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
        "python": "/opt/ciao/bin/python",
        "platform": "darwin",
        "state_dir": source.parent / "state",
    }
    return workspace_move.plan(source, str(target), **{**defaults, **kwargs})


# ── plan ─────────────────────────────────────────────────────────────


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
        return {"active_chat_ids": [] if drained else ["busy"]}

    def get(url: str) -> dict[str, Any] | None:
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


def test_run_move_rolls_back_when_the_engine_does_not_start(tmp_path: Path) -> None:
    host = _Host(start_ok=False)
    result, service, _posts = _run(tmp_path, host)
    assert result.phase == "rolled_back"
    assert "launchctl failed" in result.error
    assert (tmp_path / "old" / ".env").is_file()
    assert not (tmp_path / "new").exists()
    assert service.workspace() == tmp_path / "old"
    assert host.calls == ["stop", "start", "stop", "start"]


def test_run_move_leaves_everything_when_chats_never_drain(tmp_path: Path) -> None:
    host = _Host()
    result, _service, posts = _run(tmp_path, host, drained=False)
    assert result.phase == "failed"
    assert "still running" in result.error
    assert (tmp_path / "old" / ".env").is_file()
    assert host.calls == []
    assert posts[-1].endswith("/api/admin/drain/cancel")


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
