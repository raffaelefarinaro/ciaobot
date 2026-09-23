"""Archiving a workspace keeps its files intact and out of the agent's reach.

Removing a workspace used to MOVE its projects and notes into the primary
workspace, which mixed e.g. Work notes into Personal memory with no way back.
It is now archived like a completed project: unregistered, its folder moved
byte for byte to ``<install>/.archived-workspaces/<name>-<stamp>/``, its chats
archived, its schedules taken with it, and its notes dropped from search and
the shared index. A restore puts the files, the registry entry and the
schedules back; vault-backed projects are rediscovered from the notes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import fts_search, vault_index, vault_lint
from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import (
    archive_workspace_setting,
    list_archived_workspaces,
    list_workspaces,
    restore_archived_workspace,
)
from ciao.workspace_archive import (
    ARCHIVE_DIR_NAME,
    WorkspaceArchiveError,
    archive_root,
    list_archives,
    plan_archive,
)


# ── fixtures ───────────────────────────────────────────────────────────────


def _reroot(tmp_path: Path) -> None:
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    reset_reroot_cache()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # The search database must be this test's, never the operator's.
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    reset_reroot_cache()
    yield
    reset_reroot_cache()


def _config(tmp_path: Path, *, rerooted: bool) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if rerooted:
        _reroot(tmp_path)
        roots = {name: f"{name}/memory-vault" for name in ("personal", "work")}
    else:
        roots = {name: f"memory-vault/{name}" for name in ("personal", "work")}
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root=roots["personal"]),
            "work": WorkspaceConfig(
                name="work",
                vault_root=roots["work"],
                gws_profile="work",
                color="cyan",
                default_provider="claude",
            ),
        },
    )


def _manager(tmp_path: Path, config: CiaoConfig) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _app(tmp_path: Path, *, rerooted: bool):
    config = _config(tmp_path, rerooted=rerooted)
    for name in ("personal", "work"):
        vault = Path(config.workspace_vault_root(name))
        (vault / "People").mkdir(parents=True, exist_ok=True)
        (vault / "People" / f"{name}-friend.md").write_text(
            f"---\ntype: person\n---\n# Friend of {name}\n\nquokka {name}\n",
            encoding="utf-8",
        )
    if rerooted:
        # Agent assets that belong to the work root and must travel with it.
        work_root = tmp_path / "work"
        (work_root / "AGENTS.md").write_text("# work guide\n", encoding="utf-8")
        (work_root / ".claude" / "skills").mkdir(parents=True)
        (work_root / ".claude" / "skills" / "linked").symlink_to(tmp_path / "personal")
    pcm = _manager(tmp_path, config)
    store = ScheduleStore(tmp_path / ".runtime", workspace_names=config.workspace_names)
    schedules = ScheduleManager(store)
    app = Starlette(
        routes=[
            Route("/api/workspaces", list_workspaces, methods=["GET"]),
            Route("/api/workspaces/archived", list_archived_workspaces, methods=["GET"]),
            Route(
                "/api/workspaces/archived/restore",
                restore_archived_workspace,
                methods=["POST"],
            ),
            Route(
                "/api/workspaces/{name}/archive",
                archive_workspace_setting,
                methods=["POST"],
            ),
            Route("/api/workspaces/{name}", archive_workspace_setting, methods=["DELETE"]),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    app.state.schedule_manager = schedules
    return TestClient(app), config, pcm, store


def _tree(root: Path) -> dict[str, str]:
    """Every entry under ``root`` with a content hash (or link target)."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            out[rel] = "link:" + str(path.readlink())
        elif path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            out[rel] = "dir"
    return out


def _index_all(config: CiaoConfig) -> Path:
    db = fts_search.get_db_path(config.state_path.parent)
    conn = sqlite3.connect(db)
    try:
        fts_search.init_db(conn)
        for root, _name, _prefix in config.vault_scan_targets():
            fts_search.index_vault(conn, root, path_base=config.workspace_root)
    finally:
        conn.close()
    return db


def _search_paths(db: Path, query: str) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [row["path"] for row in fts_search.search_vault(conn, query, limit=50)]
    finally:
        conn.close()


# ── archive: per-root (fresh / re-rooted) layout ─────────────────────────────


def test_archive_moves_the_whole_agent_root_intact(tmp_path):
    client, config, pcm, _store = _app(tmp_path, rerooted=True)
    work_root = tmp_path / "work"
    before = _tree(work_root)

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200, response.json()
    archived = response.json()["archived"]
    folder = archive_root(config) / archived["id"]
    assert folder.parent == tmp_path / ARCHIVE_DIR_NAME
    assert archived["id"].startswith("work-")
    assert not work_root.exists()
    assert _tree(folder / "work") == before, "the archived root changed on the way"
    # Nothing was merged into the primary workspace.
    personal_vault = Path(config.workspace_vault_root("personal"))
    assert not (personal_vault / "People" / "work-friend.md").exists()

    metadata = json.loads((folder / "archive.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "archived"
    assert metadata["name"] == "work"
    assert metadata["layout"] == "per-root"
    assert metadata["original_path"] == "work"
    assert metadata["workspace"]["color"] == "cyan"
    assert metadata["workspace"]["gws_profile"] == "work"
    assert metadata["workspace"]["default_provider"] == "claude"
    assert metadata["archived_at"].endswith("Z")


def test_archive_unregisters_the_workspace(tmp_path):
    client, config, pcm, _store = _app(tmp_path, rerooted=True)

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200
    assert config.workspace("work") is None
    assert [w["name"] for w in response.json()["workspaces"]] == ["personal"]
    registry = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    assert [entry["name"] for entry in registry] == ["personal"]
    # Gone from every scan target and agent root: nothing enumerates it now.
    assert all(name != "work" for _root, name, _p in config.vault_scan_targets())
    assert all(name != "work" for _root, name in config.agent_root_targets())
    assert all(p.workspace != "work" for p in pcm.list_projects())


def test_archive_drops_the_workspace_from_search(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    db = _index_all(config)
    assert any("work" in p for p in _search_paths(db, "quokka"))

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200
    assert response.json()["archived"]["search_rows_removed"] >= 1
    paths = _search_paths(db, "quokka")
    assert paths, "the primary workspace's notes must stay searchable"
    assert not any(p.startswith("work/") for p in paths)
    # And a later full index pass does not bring them back.
    _index_all(config)
    assert not any(p.startswith("work/") for p in _search_paths(db, "quokka"))


def test_archive_archives_the_workspace_chats(tmp_path):
    client, config, pcm, _store = _app(tmp_path, rerooted=True)
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    chat = pcm.create_chat(general.project_id, title="work chat")
    personal_general = next(p for p in pcm.list_projects("personal") if p.is_auto)
    kept = pcm.create_chat(personal_general.project_id, title="personal chat")

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200
    assert response.json()["archived"]["projects"] >= 1
    assert pcm.get_chat(chat.chat_id) is None
    assert general.project_id not in {p.project_id for p in pcm.list_projects()}
    # The other workspace's chats are untouched and NOT repointed anywhere.
    assert pcm.get_chat(kept.chat_id) is not None


def test_archive_refuses_while_a_chat_in_the_workspace_is_running(tmp_path, monkeypatch):
    client, config, pcm, _store = _app(tmp_path, rerooted=True)
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    chat = pcm.create_chat(general.project_id, title="busy")
    monkeypatch.setattr(pcm, "active_chat_ids", lambda: [chat.chat_id])

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 409
    assert "still working" in response.json()["error"]
    assert config.workspace("work") is not None
    assert (tmp_path / "work").is_dir()
    assert pcm.get_chat(chat.chat_id) is not None


def test_archive_takes_the_workspace_schedules_and_stops_its_routines(tmp_path):
    client, config, pcm, store = _app(tmp_path, rerooted=True)
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    mine = store.create(
        daily_time_utc="08:00", prompt="standup", model="", mode="auto",
        chat_id=0, workspace="work",
    )
    by_project = store.create(
        daily_time_utc="09:00", prompt="digest", model="", mode="auto",
        chat_id=0, web_project_id=general.project_id,
    )
    other = store.create(
        daily_time_utc="10:00", prompt="personal", model="", mode="auto",
        chat_id=0, workspace="personal",
    )
    system_store = ScheduleStore(
        tmp_path / ".runtime", include_system=True, workspace_names=config.workspace_names
    )
    assert any(e.schedule_id.endswith("@work") for e in system_store.list_entries())

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200
    assert response.json()["archived"]["schedules"] == 2
    remaining = {e.schedule_id for e in store.list_entries()}
    assert remaining == {other.schedule_id}
    # Per-workspace system routines fan out over the registry, so they stop.
    assert not any(e.schedule_id.endswith("@work") for e in system_store.list_entries())
    assert any(e.schedule_id.endswith("@personal") for e in system_store.list_entries())
    folder = archive_root(config) / response.json()["archived"]["id"]
    metadata = json.loads((folder / "archive.json").read_text(encoding="utf-8"))
    assert {s["schedule_id"] for s in metadata["schedules"]} == {
        mine.schedule_id,
        by_project.schedule_id,
    }


@pytest.mark.parametrize("rerooted", [True, False])
def test_the_primary_and_the_last_workspace_are_refused(tmp_path, rerooted):
    client, config, _pcm, _store = _app(tmp_path, rerooted=rerooted)

    refused = client.post("/api/workspaces/personal/archive")
    assert refused.status_code == 400
    assert "primary" in refused.json()["error"]
    assert config.workspace("personal") is not None

    assert client.post("/api/workspaces/work/archive").status_code == 200
    last = client.post("/api/workspaces/personal/archive")
    assert last.status_code == 400
    assert "last" in last.json()["error"]


def test_unknown_workspace_is_404(tmp_path):
    client, _config, _pcm, _store = _app(tmp_path, rerooted=True)
    assert client.post("/api/workspaces/nope/archive").status_code == 404


def test_delete_is_an_alias_that_archives(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)

    response = client.delete("/api/workspaces/work")

    assert response.status_code == 200
    assert "archived" in response.json()
    assert list(archive_root(config).glob("work-*/work/memory-vault/People/work-friend.md"))


# ── archive: shared (not re-rooted) layout ───────────────────────────────────


def test_shared_layout_moves_only_the_workspace_folder_and_rebuilds_index(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=False)
    shared = Path(config.vault_root)
    (shared / "INDEX.md").write_text(
        "stale index naming work/People/work-friend.md\n", encoding="utf-8"
    )
    work_vault = shared / "work"
    before = _tree(work_vault)
    db = _index_all(config)
    assert any(p.startswith("memory-vault/work/") for p in _search_paths(db, "quokka"))

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200, response.json()
    archived = response.json()["archived"]
    assert archived["index_rebuilt"] is True
    folder = archive_root(config) / archived["id"]
    assert folder.parent == tmp_path / ARCHIVE_DIR_NAME
    assert not work_vault.exists()
    assert _tree(folder / "work") == before
    metadata = json.loads((folder / "archive.json").read_text(encoding="utf-8"))
    assert metadata["layout"] == "shared"
    assert metadata["original_path"] == "memory-vault/work"
    index = (shared / "INDEX.md").read_text(encoding="utf-8")
    assert "work-friend" not in index
    assert "personal-friend" in index
    paths = _search_paths(db, "quokka")
    assert paths and not any(p.startswith("memory-vault/work/") for p in paths)
    # The shared vault's other contents stay put.
    assert (shared / "personal" / "People" / "personal-friend.md").is_file()


def test_shared_layout_refuses_a_vault_outside_its_standard_folder(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=False)
    pinned = tmp_path / "legacy-work"
    (pinned / "People").mkdir(parents=True)
    config.workspaces["work"] = WorkspaceConfig(name="work", vault_root=str(pinned))

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 409
    assert "vault-relocate" in response.json()["error"]
    assert config.workspace("work") is not None
    assert pinned.is_dir()
    assert not archive_root(config).exists()


def test_shared_layout_refuses_the_workspace_that_owns_the_whole_vault(tmp_path):
    config = _config(tmp_path, rerooted=False)
    config.workspaces["work"] = WorkspaceConfig(name="work", vault_root=str(config.vault_root))
    with pytest.raises(WorkspaceArchiveError) as excinfo:
        plan_archive(config, "work")
    assert excinfo.value.status == 409
    assert "whole shared vault" in excinfo.value.message


def test_shared_layout_refuses_a_vault_outside_the_install(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-external-vault"
    (outside / "work").mkdir(parents=True)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=outside,
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root=str(outside / "personal")),
            "work": WorkspaceConfig(name="work", vault_root=str(outside / "work")),
        },
    )
    with pytest.raises(WorkspaceArchiveError) as excinfo:
        plan_archive(config, "work")
    assert "outside the install" in excinfo.value.message
    assert (outside / "work").is_dir()


def test_per_root_layout_refuses_a_vault_outside_its_root(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    outside = tmp_path.parent / f"{tmp_path.name}-external"
    outside.mkdir()
    config.workspaces["work"] = WorkspaceConfig(name="work", vault_root=str(outside))

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 409
    assert config.workspace("work") is not None
    assert (tmp_path / "work").is_dir()


def test_a_symlinked_workspace_folder_is_refused(tmp_path):
    config = _config(tmp_path, rerooted=False)
    real = tmp_path / "elsewhere"
    real.mkdir()
    shared = Path(config.vault_root)
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "work").symlink_to(real)
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root=str(shared / "work")
    )
    with pytest.raises(WorkspaceArchiveError):
        plan_archive(config, "work")


def test_a_workspace_with_no_folder_yet_archives_its_registry_entry(tmp_path):
    config = _config(tmp_path, rerooted=True)
    target = plan_archive(config, "work")
    assert not target.source.exists()
    from ciao.workspace_archive import move_to_archive

    archived = move_to_archive(config, target)
    folder = archive_root(config) / archived["id"]
    assert (folder / "archive.json").is_file()
    assert archived["content_dir"] == ""


# ── the archive stays out of every scanner ───────────────────────────────────


def test_scanners_skip_the_archive_even_when_the_install_is_the_vault(tmp_path):
    """Existing-folder setup can make the install root the vault itself."""
    archived_note = tmp_path / ARCHIVE_DIR_NAME / "work-20260101-000000" / "work" / "Secret.md"
    archived_note.parent.mkdir(parents=True)
    archived_note.write_text("---\ntype: note\n---\n# Secret\n\nquokka archived\n", encoding="utf-8")
    (tmp_path / "Visible.md").write_text("---\ntype: note\n---\n# Visible\n\nquokka live\n", encoding="utf-8")

    entries = vault_index.scan_vault(tmp_path)
    assert [Path(e.path).name for e in entries] == ["Visible.md"]

    conn = sqlite3.connect(tmp_path / "fts.db")
    try:
        fts_search.init_db(conn)
        fts_search.index_vault(conn, tmp_path, path_base=tmp_path)
        paths = [row["path"] for row in fts_search.search_vault(conn, "quokka")]
    finally:
        conn.close()
    assert paths == ["Visible.md"]
    assert vault_lint._is_excluded(archived_note.relative_to(tmp_path))


def test_forget_subtree_refuses_prefixes_that_are_not_one_subtree(tmp_path):
    conn = sqlite3.connect(tmp_path / "fts.db")
    try:
        fts_search.init_db(conn)
        conn.execute("INSERT INTO vault_fts (path, title, body) VALUES ('a/x.md','x','y')")
        conn.commit()
        assert fts_search.forget_subtree(conn, "") == 0
        assert fts_search.forget_subtree(conn, fts_search.NO_MATCH_KEY_PREFIX) == 0
        assert fts_search.forget_subtree(conn, "a_/") == 0, "LIKE wildcards must be escaped"
        assert fts_search.forget_subtree(conn, "a/") == 1
    finally:
        conn.close()


# ── restore ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rerooted", [True, False])
def test_restore_round_trip(tmp_path, rerooted):
    client, config, pcm, store = _app(tmp_path, rerooted=rerooted)
    store.create(
        daily_time_utc="08:00", prompt="standup", model="", mode="auto",
        chat_id=0, workspace="work",
    )
    source = tmp_path / "work" if rerooted else Path(config.vault_root) / "work"
    before = _tree(source)
    registry_before = next(
        entry
        for entry in json.loads(
            json.dumps(
                client.get("/api/workspaces").json()["workspaces"]
            )
        )
        if entry["name"] == "work"
    )

    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    listing = client.get("/api/workspaces/archived").json()["archived"]
    assert [item["id"] for item in listing] == [archived["id"]]
    assert listing[0]["restorable"] is True
    assert listing[0]["color"] == "cyan"
    assert listing[0]["schedules"] == 1

    restored = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert restored.status_code == 200, restored.json()
    assert restored.json()["restored"]["schedules"] == 1
    assert _tree(source) == before
    entry = next(w for w in restored.json()["workspaces"] if w["name"] == "work")
    assert entry == registry_before
    assert [e.workspace for e in store.list_entries()] == ["work"]
    assert any(p.workspace == "work" and p.is_auto for p in pcm.list_projects("work"))
    # The archive folder is cleaned up and the list is empty again.
    assert not (archive_root(config) / archived["id"]).exists()
    assert client.get("/api/workspaces/archived").json()["archived"] == []


def test_restore_refuses_a_taken_name(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    # Someone created a new workspace with the same name (different case).
    config.workspaces["Work"] = WorkspaceConfig(name="Work", vault_root="Work/memory-vault")

    listing = client.get("/api/workspaces/archived").json()["archived"]
    assert listing[0]["restorable"] is False
    assert "already exists" in listing[0]["blocked_reason"]
    refused = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert refused.status_code == 409
    assert "already exists" in refused.json()["error"]
    assert (archive_root(config) / archived["id"] / "work").is_dir()


def test_restore_refuses_when_the_folder_came_back_by_hand(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    (tmp_path / "work").mkdir()

    refused = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert refused.status_code == 409
    assert "already exists" in refused.json()["error"]
    assert config.workspace("work") is None


def test_restore_refuses_an_archive_from_the_other_layout(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=False)
    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    _reroot(tmp_path)

    listing = list_archives(config)
    assert listing[0]["restorable"] is False
    refused = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})
    assert refused.status_code == 409
    assert "by hand" in refused.json()["error"]


@pytest.mark.parametrize("bad", ["", "../x", "work", "work-20260101-000000/../../etc"])
def test_restore_rejects_bad_ids(tmp_path, bad):
    client, _config, _pcm, _store = _app(tmp_path, rerooted=True)
    response = client.post("/api/workspaces/archived/restore", json={"id": bad})
    assert response.status_code in (400, 404)


def test_primary_is_reported_so_the_ui_can_hide_its_archive_button(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    assert client.get("/api/workspaces").json()["primary"] == "personal"


# ── review follow-ups ────────────────────────────────────────────────────────


def test_a_failed_move_keeps_the_chats_and_schedules(tmp_path, monkeypatch):
    client, config, pcm, store = _app(tmp_path, rerooted=True)
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    chat = pcm.create_chat(general.project_id, title="work chat")
    mine = store.create(
        daily_time_utc="08:00", prompt="standup", model="", mode="auto",
        chat_id=0, workspace="work",
    )

    def _refuse(*_a, **_kw):
        raise OSError("busy")

    monkeypatch.setattr("ciao.workspace_archive.os.rename", _refuse)
    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 500
    assert config.workspace("work") is not None
    assert (tmp_path / "work").is_dir()
    assert pcm.get_chat(chat.chat_id) is not None
    assert general.project_id in {p.project_id for p in pcm.list_projects("work")}
    assert {e.schedule_id for e in store.list_entries()} == {mine.schedule_id}
    root = archive_root(config)
    assert not root.exists() or not any(root.iterdir())


def test_archive_waits_for_a_running_archive_job(tmp_path):
    client, config, pcm, _store = _app(tmp_path, rerooted=True)
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    chat = pcm.create_chat(general.project_id, title="postprocessing")

    class _Running:
        def done(self) -> bool:
            return False

    pcm._archive_tasks[chat.chat_id] = _Running()  # type: ignore[assignment]
    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 409
    assert "being archived" in response.json()["error"]
    assert (tmp_path / "work").is_dir()


def test_shared_layout_restores_a_custom_vault_folder_name(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=False)
    shared = Path(config.vault_root)
    (shared / "work").rename(shared / "client-a")
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root="memory-vault/client-a", color="cyan"
    )
    before = _tree(shared / "client-a")

    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    assert not (shared / "client-a").exists()
    restored = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert restored.status_code == 200, restored.json()
    assert not (shared / "work").exists()
    assert _tree(shared / "client-a") == before
    assert Path(config.workspace_vault_root("work")) == shared / "client-a"


@pytest.mark.parametrize("content_dir", ["..", "../personal", "/etc", "a/b"])
def test_restore_refuses_an_unsafe_content_dir(tmp_path, content_dir):
    client, config, _pcm, _store = _app(tmp_path, rerooted=True)
    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    meta_path = archive_root(config) / archived["id"] / "archive.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    metadata["content_dir"] = content_dir
    meta_path.write_text(json.dumps(metadata), encoding="utf-8")

    refused = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert refused.status_code == 409
    assert config.workspace("work") is None
    assert (tmp_path / "personal").is_dir()


def test_shared_layout_refuses_a_vault_that_is_its_own_repository(tmp_path):
    client, config, _pcm, _store = _app(tmp_path, rerooted=False)
    (Path(config.vault_root) / ".git").mkdir()

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 409
    assert "own repository" in response.json()["error"]
    assert (Path(config.vault_root) / "work").is_dir()


# ── metadata and registry I/O failures ───────────────────────────────────────


def _work_schedule(store: ScheduleStore):
    return store.create(
        daily_time_utc="08:00", prompt="standup", model="", mode="auto",
        chat_id=0, workspace="work",
    )


def _assert_archive_undone(tmp_path, config, store, schedule_id, before):
    assert config.workspace("work") is not None
    assert _tree(tmp_path / "work") == before
    assert {e.schedule_id for e in store.list_entries()} == {schedule_id}
    root = archive_root(config)
    assert not root.is_dir() or not any(root.iterdir())


def test_archive_folder_creation_failure_puts_the_schedules_back(tmp_path):
    client, config, _pcm, store = _app(tmp_path, rerooted=True)
    mine = _work_schedule(store)
    before = _tree(tmp_path / "work")
    # A file where the archive root should be: mkdir fails with an OSError.
    archive_root(config).write_text("not a directory", encoding="utf-8")

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 500
    assert "archive folder" in response.json()["error"]
    assert config.workspace("work") is not None
    assert _tree(tmp_path / "work") == before
    assert {e.schedule_id for e in store.list_entries()} == {mine.schedule_id}


def test_first_metadata_write_failure_puts_the_schedules_back(tmp_path, monkeypatch):
    client, config, _pcm, store = _app(tmp_path, rerooted=True)
    mine = _work_schedule(store)
    before = _tree(tmp_path / "work")

    def _disk_full(*_a, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("ciao.workspace_archive._write_metadata", _disk_full)
    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 500
    assert "archive.json" in response.json()["error"]
    _assert_archive_undone(tmp_path, config, store, mine.schedule_id, before)


def test_final_metadata_write_failure_moves_the_folder_back(tmp_path, monkeypatch):
    from ciao import workspace_archive

    client, config, _pcm, store = _app(tmp_path, rerooted=True)
    mine = _work_schedule(store)
    before = _tree(tmp_path / "work")
    real_write = workspace_archive._write_metadata
    calls = {"n": 0}

    def _second_fails(folder, metadata):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(13, "Permission denied")
        real_write(folder, metadata)

    monkeypatch.setattr("ciao.workspace_archive._write_metadata", _second_fails)
    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 500
    assert "left where it was" in response.json()["error"]
    _assert_archive_undone(tmp_path, config, store, mine.schedule_id, before)


def test_a_raw_oserror_from_the_move_still_puts_the_schedules_back(tmp_path, monkeypatch):
    client, config, _pcm, store = _app(tmp_path, rerooted=True)
    mine = _work_schedule(store)

    def _boom(*_a, **_kw):
        raise OSError("unexpected")

    monkeypatch.setattr("ciao.workspace_archive.move_to_archive", _boom)
    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 500
    assert "could not archive 'work'" in response.json()["error"]
    assert config.workspace("work") is not None
    assert {e.schedule_id for e in store.list_entries()} == {mine.schedule_id}


@pytest.mark.parametrize("rerooted", [True, False])
def test_restore_registry_save_failure_leaves_the_archive_restorable(
    tmp_path, monkeypatch, rerooted
):
    client, config, _pcm, store = _app(tmp_path, rerooted=rerooted)
    _work_schedule(store)
    source = tmp_path / "work" if rerooted else Path(config.vault_root) / "work"
    before = _tree(source)
    archived = client.post("/api/workspaces/work/archive").json()["archived"]
    folder = archive_root(config) / archived["id"]
    registry_path = tmp_path / ".runtime" / "workspaces.json"
    registry_before = registry_path.read_text(encoding="utf-8")
    names_before = set(config.workspaces)

    real_persist = CiaoConfig.persist_workspace_registry
    failing = {"on": True}

    def _disk_full(self: CiaoConfig) -> None:
        if failing["on"]:
            raise OSError(28, "No space left on device")
        real_persist(self)

    monkeypatch.setattr(CiaoConfig, "persist_workspace_registry", _disk_full)
    refused = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert refused.status_code == 500
    assert "registry could not be saved" in refused.json()["error"]
    assert set(config.workspaces) == names_before
    assert not source.exists()
    assert (folder / "archive.json").is_file()
    assert registry_path.read_text(encoding="utf-8") == registry_before
    assert store.list_entries() == []
    listing = client.get("/api/workspaces/archived").json()["archived"]
    assert listing[0]["restorable"] is True, listing

    failing["on"] = False
    restored = client.post("/api/workspaces/archived/restore", json={"id": archived["id"]})

    assert restored.status_code == 200, restored.json()
    assert restored.json()["restored"]["schedules"] == 1
    assert _tree(source) == before
