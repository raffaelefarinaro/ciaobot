"""Workspace rename: registry key, references, directory move, refusals."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.background import BackgroundRun, BackgroundRunStore
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.import_store import ImportStore, engine_store_path
from ciao.schedules import ScheduleStore
from ciao.web.project_chats import ProjectInfo
from ciao.webhooks import WebhookStore
from ciao.workspace_rename import WorkspaceRenameBusy, rename_workspace
from ciao.workspace_reroot import write_receipt


class _Projects:
    """The slice of ProjectChatManager a rename touches."""

    def __init__(self) -> None:
        self._projects: dict[str, ProjectInfo] = {}
        self.saved_reasons: list[str] = []
        self.busy: dict[str, list[str]] = {}

    def workspace_busy_chat_ids(self, workspace: str) -> list[str]:
        return list(self.busy.get(workspace, []))

    def _save(self, *, reason: str = "registry_mutation") -> None:
        self.saved_reasons.append(reason)


def _shared_config(tmp_path: Path, names: list[str]) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            name: WorkspaceConfig(
                name=name, vault_root=f"memory-vault/{name}"
            )
            for name in names
        },
    )


def _rerooted_config(tmp_path: Path, vault_roots: dict[str, str]) -> CiaoConfig:
    runtime_root = tmp_path / ".runtime"
    write_receipt(
        runtime_root, {"status": "migrated", "workspaces": list(vault_roots)}
    )
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime_root / "state.json",
        media_root=runtime_root / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=root)
            for name, root in vault_roots.items()
        },
    )


def _stores(config: CiaoConfig, tmp_path: Path):
    runtime = tmp_path / ".runtime"
    schedules = ScheduleStore(
        runtime, include_system=True, workspace_names=config.workspace_names
    )
    webhooks = WebhookStore(runtime / "webhooks.json")
    imports = ImportStore(engine_store_path(config))
    runs = BackgroundRunStore(runtime)
    return schedules, webhooks, imports, runs


def _seed_all(config: CiaoConfig, tmp_path: Path, projects: _Projects):
    """One row per store filed under 'personal'; returns the ids/secrets."""
    projects._projects["proj-1"] = ProjectInfo(
        project_id="proj-1", name="Work", workspace="personal"
    )
    schedules, webhooks, imports, runs = _stores(config, tmp_path)
    user = schedules.create(
        daily_time_utc="10:00",
        prompt="morning brief",
        model="",
        mode="auto",
        chat_id=0,
        workspace="personal",
        title="Brief",
    )
    system_path = tmp_path / ".runtime" / "system_schedules_state.json"
    system_path.write_text(
        json.dumps(
            {
                "schedules": {
                    "system-memory-curation@personal": {
                        "workspace": "personal",
                        "enabled": False,
                        "model": "sonnet",
                    }
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    trigger, secret = webhooks.create(
        name="hook", workspace="personal", instructions="do the thing"
    )
    trigger = webhooks.update(
        trigger.trigger_id, expected_revision=trigger.revision, enabled=True
    )
    batch = imports.create(
        workspace="personal",
        sources=[{"provider": "claude_code", "source_id": "sess-1"}],
    )
    runs.replace(
        BackgroundRun(
            run_id="run-1",
            parent_chat_id="chat-1",
            project_id="proj-1",
            workspace="personal",
            label="job",
            cmd=["echo", "hi"],
            cwd="",
            status="ok",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    return {
        "schedule_id": user.schedule_id,
        "trigger_id": trigger.trigger_id,
        "revision": trigger.revision,
        "secret": secret,
        "batch_id": batch.batch_id,
    }


def test_rename_rewrites_registry_projects_schedules_webhooks_imports_and_runs(
    tmp_path: Path,
) -> None:
    config = _shared_config(tmp_path, ["personal", "work"])
    config.persist_workspace_registry()
    projects = _Projects()
    ids = _seed_all(config, tmp_path, projects)
    runtime = tmp_path / ".runtime"
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    result = rename_workspace(
        config,
        old="personal",
        new="santo",
        projects=projects,
        schedules=schedules,
        webhooks=webhooks,
        imports=imports,
        runs=runs,
    )

    assert result == {"from": "personal", "to": "santo"}
    stored = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert sorted(entry["name"] for entry in stored) == ["santo", "work"]
    personal_entry = next(e for e in stored if e["name"] == "santo")
    assert personal_entry["vault_root"] == "memory-vault/personal"
    assert projects._projects["proj-1"].workspace == "santo"
    assert projects._projects["proj-1"].project_id == "proj-1"
    assert projects.saved_reasons == ["workspace_rename"]

    schedules, webhooks, imports, runs = _stores(config, tmp_path)
    user_rows = [
        entry
        for entry in schedules.list_entries()
        if entry.schedule_id == ids["schedule_id"]
    ]
    assert len(user_rows) == 1
    assert user_rows[0].workspace == "santo"
    assert user_rows[0].schedule_id == ids["schedule_id"]
    system_state = json.loads(
        (runtime / "system_schedules_state.json").read_text(encoding="utf-8")
    )["schedules"]
    assert "system-memory-curation@personal" not in system_state
    moved = system_state["system-memory-curation@santo"]
    assert moved["workspace"] == "santo"
    assert moved["enabled"] is False
    assert moved["model"] == "sonnet"

    trigger = webhooks.get(ids["trigger_id"])
    assert trigger.workspace == "santo"
    assert trigger.revision == ids["revision"] + 1
    assert webhooks.authenticate(ids["trigger_id"], ids["secret"]) is not None

    batch = imports.get(ids["batch_id"])
    assert batch.workspace == "santo"
    assert batch.destination == "personal"

    run = runs.get("run-1")
    assert run is not None
    assert run.workspace == "santo"
    assert not (tmp_path / "santo").exists()


def test_rerooted_rename_moves_the_agent_root_and_the_vault_prefix(
    tmp_path: Path,
) -> None:
    config = _rerooted_config(
        tmp_path, {"personal": "personal", "work": "work"}
    )
    assert (
        config.workspaces["personal"].vault_root == "personal/memory-vault"
    )
    (tmp_path / "personal" / "memory-vault").mkdir(parents=True)
    (tmp_path / "personal" / "sub").mkdir(parents=True)
    config.persist_workspace_registry()
    projects = _Projects()
    _stores(config, tmp_path)[3].replace(
        BackgroundRun(
            run_id="run-1",
            parent_chat_id="chat-1",
            project_id="proj-1",
            workspace="personal",
            label="job",
            cmd=["echo", "hi"],
            cwd=str(tmp_path / "personal" / "sub"),
            status="ok",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    result = rename_workspace(
        config,
        old="personal",
        new="santo",
        projects=projects,
        schedules=schedules,
        webhooks=webhooks,
        imports=imports,
        runs=runs,
    )

    assert result == {"from": "personal", "to": "santo"}
    assert (tmp_path / "santo").is_dir()
    assert (tmp_path / "santo" / "memory-vault").is_dir()
    assert not (tmp_path / "personal").exists()
    assert config.workspaces["santo"].vault_root == "santo/memory-vault"
    run = runs.get("run-1")
    assert run is not None
    assert run.cwd == (tmp_path / "santo" / "sub").as_posix()


def test_absolute_vault_root_is_not_rewritten(tmp_path: Path) -> None:
    external = tmp_path / "ext-vault"
    external.mkdir()
    config = _rerooted_config(
        tmp_path, {"personal": str(external), "work": "work"}
    )
    (tmp_path / "personal").mkdir()
    config.persist_workspace_registry()
    projects = _Projects()
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    rename_workspace(
        config,
        old="personal",
        new="santo",
        projects=projects,
        schedules=schedules,
        webhooks=webhooks,
        imports=imports,
        runs=runs,
    )

    assert config.workspaces["santo"].vault_root == str(external)
    assert (tmp_path / "santo").is_dir()
    assert not (tmp_path / "personal").exists()


def test_invalid_name_collision_archived_name_and_busy_chat_write_nothing(
    tmp_path: Path,
) -> None:
    config = _shared_config(tmp_path, ["personal", "work"])
    archive_dir = tmp_path / ".archived-workspaces" / "ghost-20240101-000000"
    archive_dir.mkdir(parents=True)
    (archive_dir / "archive.json").write_text(
        json.dumps({"name": "ghost"}) + "\n", encoding="utf-8"
    )
    config.persist_workspace_registry()
    runtime = tmp_path / ".runtime"
    projects = _Projects()
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    def attempt(new: str) -> None:
        before = (runtime / "workspaces.json").read_bytes()
        with pytest.raises(ValueError):
            rename_workspace(
                config,
                old="personal",
                new=new,
                projects=projects,
                schedules=schedules,
                webhooks=webhooks,
                imports=imports,
                runs=runs,
            )
        assert (runtime / "workspaces.json").read_bytes() == before
        assert sorted(config.workspace_names()) == ["personal", "work"]

    attempt("bad name!")
    attempt("WORK")
    attempt("ghost")

    projects.busy["personal"] = ["chat-1"]
    attempt("santo")
    assert projects.busy["personal"] == ["chat-1"]

    projects.busy.clear()
    runs.replace(
        BackgroundRun(
            run_id="run-busy",
            parent_chat_id="chat-1",
            project_id="proj-1",
            workspace="personal",
            label="job",
            cmd=["sleep", "60"],
            cwd="",
            status="queued",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    before = (runtime / "workspaces.json").read_bytes()
    with pytest.raises(WorkspaceRenameBusy):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )
    assert (runtime / "workspaces.json").read_bytes() == before
    assert sorted(config.workspace_names()) == ["personal", "work"]


def test_failed_persist_after_the_directory_rename_moves_it_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _rerooted_config(
        tmp_path, {"personal": "personal", "work": "work"}
    )
    (tmp_path / "personal").mkdir()
    config.persist_workspace_registry()
    projects = _Projects()
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    def _boom(self: CiaoConfig) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(CiaoConfig, "persist_workspace_registry", _boom)
    with pytest.raises(OSError):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )

    assert (tmp_path / "personal").is_dir()
    assert not (tmp_path / "santo").exists()
    assert sorted(config.workspace_names()) == ["personal", "work"]


def test_renaming_personal_changes_primary_workspace(tmp_path: Path) -> None:
    config = _shared_config(tmp_path, ["personal", "work"])
    assert config.primary_workspace() == "personal"
    config.persist_workspace_registry()
    projects = _Projects()
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    rename_workspace(
        config,
        old="personal",
        new="santo",
        projects=projects,
        schedules=schedules,
        webhooks=webhooks,
        imports=imports,
        runs=runs,
    )

    assert config.primary_workspace() == "work"


def _rollback_files(tmp_path: Path, config: CiaoConfig) -> dict[str, Path]:
    runtime = tmp_path / ".runtime"
    return {
        "registry": runtime / "workspaces.json",
        "schedules": runtime / "schedules.json",
        "system": runtime / "system_schedules_state.json",
        "webhooks": runtime / "webhooks.json",
        "imports": engine_store_path(config),
        "runs": runtime / "background" / "state.json",
    }


def _assert_rolled_back(
    config: CiaoConfig,
    tmp_path: Path,
    projects: _Projects,
    ids: dict[str, object],
    before: dict[str, bytes],
) -> None:
    """Every persisted and in-memory reference is back on the old workspace."""
    from ciao.import_store import ImportStore, engine_store_path
    from ciao.schedules import ScheduleStore
    from ciao.webhooks import WebhookStore

    runtime = tmp_path / ".runtime"
    for key, path in _rollback_files(tmp_path, config).items():
        assert path.read_bytes() == before[key], key
    assert sorted(config.workspace_names()) == ["personal", "work"]
    assert config.workspace("santo") is None
    assert projects._projects["proj-1"].workspace == "personal"

    schedules = ScheduleStore(
        runtime, include_system=True, workspace_names=config.workspace_names
    )
    schedule_id = str(ids["schedule_id"])
    user_rows = [
        entry
        for entry in schedules.list_entries()
        if entry.schedule_id == schedule_id
    ]
    assert len(user_rows) == 1
    assert user_rows[0].workspace == "personal"
    system_state = json.loads(
        (runtime / "system_schedules_state.json").read_text(encoding="utf-8")
    )["schedules"]
    assert "system-memory-curation@santo" not in system_state
    assert system_state["system-memory-curation@personal"]["workspace"] == "personal"

    webhooks = WebhookStore(runtime / "webhooks.json")
    trigger_id = str(ids["trigger_id"])
    trigger = webhooks.get(trigger_id)
    assert trigger.workspace == "personal"
    assert trigger.revision == ids["revision"]
    assert webhooks.authenticate(trigger_id, str(ids["secret"])) is not None

    imports = ImportStore(engine_store_path(config))
    batch = imports.get(str(ids["batch_id"]))
    assert batch.workspace == "personal"

    run = _stores(config, tmp_path)[3].get("run-1")
    assert run is not None
    assert run.workspace == "personal"
    assert run.cwd == ""
    assert not (tmp_path / "santo").exists()


def test_failed_project_save_restores_registry(tmp_path: Path) -> None:
    """A project save failure leaves the registry and every file as they were."""
    config = _shared_config(tmp_path, ["personal", "work"])
    config.persist_workspace_registry()
    projects = _Projects()
    ids = _seed_all(config, tmp_path, projects)
    before = {
        key: path.read_bytes()
        for key, path in _rollback_files(tmp_path, config).items()
    }

    def _boom(*, reason: str = "registry_mutation") -> None:
        raise OSError("disk full")

    projects._save = _boom  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=_stores(config, tmp_path)[0],
            webhooks=_stores(config, tmp_path)[1],
            imports=_stores(config, tmp_path)[2],
            runs=_stores(config, tmp_path)[3],
        )

    _assert_rolled_back(config, tmp_path, projects, ids, before)


def test_failed_schedule_write_restores_projects_and_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule failure after the project save restores projects too."""
    config = _shared_config(tmp_path, ["personal", "work"])
    config.persist_workspace_registry()
    projects = _Projects()
    ids = _seed_all(config, tmp_path, projects)
    before = {
        key: path.read_bytes()
        for key, path in _rollback_files(tmp_path, config).items()
    }
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    def _boom(self: ScheduleStore, old: str, new: str) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(ScheduleStore, "rename_workspace", _boom)
    with pytest.raises(OSError, match="disk full"):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )

    _assert_rolled_back(config, tmp_path, projects, ids, before)


def test_failed_webhook_write_restores_schedules_and_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A webhook failure after schedules were rewritten restores them exactly."""
    config = _shared_config(tmp_path, ["personal", "work"])
    config.persist_workspace_registry()
    projects = _Projects()
    ids = _seed_all(config, tmp_path, projects)
    before = {
        key: path.read_bytes()
        for key, path in _rollback_files(tmp_path, config).items()
    }
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    original = WebhookStore.rename_workspace

    def _write_then_fail(self: WebhookStore, old: str, new: str) -> int:
        original(self, old, new)
        raise OSError("disk full")

    monkeypatch.setattr(WebhookStore, "rename_workspace", _write_then_fail)
    with pytest.raises(OSError, match="disk full"):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )

    # The revision bump from the half-applied webhook write is undone as well:
    # the secret still verifies at the original revision.
    _assert_rolled_back(config, tmp_path, projects, ids, before)


def test_failed_import_write_restores_webhooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import failure after webhooks were rewritten restores them exactly."""
    config = _shared_config(tmp_path, ["personal", "work"])
    config.persist_workspace_registry()
    projects = _Projects()
    ids = _seed_all(config, tmp_path, projects)
    before = {
        key: path.read_bytes()
        for key, path in _rollback_files(tmp_path, config).items()
    }
    schedules, webhooks, imports, runs = _stores(config, tmp_path)

    original = ImportStore.rename_workspace

    def _write_then_fail(self: ImportStore, old: str, new: str) -> int:
        original(self, old, new)
        raise OSError("disk full")

    monkeypatch.setattr(ImportStore, "rename_workspace", _write_then_fail)
    with pytest.raises(OSError, match="disk full"):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )

    _assert_rolled_back(config, tmp_path, projects, ids, before)


def test_failed_run_write_restores_everything_including_rerooted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run failure after every other store restores all of them on re-root."""
    from ciao.background import BackgroundRunStore

    config = _rerooted_config(
        tmp_path, {"personal": "personal", "work": "work"}
    )
    (tmp_path / "personal" / "memory-vault").mkdir(parents=True)
    (tmp_path / "personal" / "sub").mkdir(parents=True)
    config.persist_workspace_registry()
    projects = _Projects()
    schedules, webhooks, imports, runs = _stores(config, tmp_path)
    runs.replace(
        BackgroundRun(
            run_id="run-1",
            parent_chat_id="chat-1",
            project_id="proj-1",
            workspace="personal",
            label="job",
            cmd=["echo", "hi"],
            cwd=str(tmp_path / "personal" / "sub"),
            status="ok",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    runs.replace(
        BackgroundRun(
            run_id="run-2",
            parent_chat_id="chat-2",
            project_id="proj-1",
            workspace="other",
            label="job",
            cmd=["echo", "hi"],
            cwd="",
            status="ok",
            started_at="2026-01-01T00:00:00Z",
        )
    )
    before: dict[str, bytes | None] = {}
    for key, path in _rollback_files(tmp_path, config).items():
        before[key] = path.read_bytes() if path.is_file() else None

    original = BackgroundRunStore.replace
    calls: list[str] = []

    def _write_first_then_fail(self: BackgroundRunStore, run: BackgroundRun) -> None:
        calls.append(run.run_id)
        original(self, run)
        if len(calls) == 1:
            raise OSError("disk full")

    monkeypatch.setattr(BackgroundRunStore, "replace", _write_first_then_fail)
    with pytest.raises(OSError, match="disk full"):
        rename_workspace(
            config,
            old="personal",
            new="santo",
            projects=projects,
            schedules=schedules,
            webhooks=webhooks,
            imports=imports,
            runs=runs,
        )

    for key, path in _rollback_files(tmp_path, config).items():
        if before[key] is None:
            assert not path.exists(), key
        else:
            assert path.read_bytes() == before[key], key
    assert sorted(config.workspace_names()) == ["personal", "work"]
    assert (tmp_path / "personal" / "sub").is_dir()
    assert not (tmp_path / "santo").exists()
    assert config.workspaces["personal"].vault_root == "personal/memory-vault"
    fresh = BackgroundRunStore(tmp_path / ".runtime")
    first = fresh.get("run-1")
    assert first is not None
    assert first.workspace == "personal"
    assert first.cwd == str(tmp_path / "personal" / "sub")
    second = fresh.get("run-2")
    assert second is not None
    assert second.workspace == "other"
