from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.auth import AuthMiddleware, make_serializer
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import search_chats
from ciao.web import transcript_service
from ciao.web.transcript_service import search_workspace_chats, _read_search_file


class _TranscriptStore:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self._runtime_root = next(iter(paths.values())).parent
        self.parsed_count = 0
        self.lookups: list[str] = []

    def current_path(self, context, provider):
        self.lookups.append(context.key)
        return self.paths[context.key]

    def current_messages_from_data(self, data):
        self.parsed_count += 1
        return [
            {"role": role, "content": text}
            for turn in data.get("turns", [])
            for role, text in (("user", turn.get("prompt", "")), ("assistant", turn.get("response", "")))
            if text
        ]


class _SearchConfig:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root
        self.vault_root = root / "vault"
        self.logs_root = root / "Logs"

    def workspace(self, name: str):
        return SimpleNamespace(vault_root=f"vault/{name}") if name == "work" else None

    def workspace_vault_root(self, name: str):
        return self.vault_root / name

    def agent_root(self, name: str):
        return self.workspace_root / name

    def chats_root(self, _name: str):
        return self.logs_root / "Chats"


def test_workspace_search_returns_visible_text_and_excludes_helpers(tmp_path: Path) -> None:
    first_path = tmp_path / "first.json"
    first_path.write_text(json.dumps({"turns": [{"prompt": "A literal <b>match</b> here"}]}))
    helper_path = tmp_path / "helper.json"
    helper_path.write_text(json.dumps({"turns": [{"prompt": "match hidden"}]}))
    first = SimpleNamespace(
        chat_id="one", project_id="normal", provider="claude", archived=False,
        archive_path="", helper={}, title="A saved chat", created_at="2026-01-01",
        last_activity_at="2026-01-02",
    )
    helper = SimpleNamespace(**{**vars(first), "chat_id": "helper", "project_id": "normal",
                                "title": "Internal", "helper": {"kind": "memory_pass"}})
    project = SimpleNamespace(workspace="work", kind="", is_system=False, is_auto=False, name="Project")
    manager = SimpleNamespace(
        _chats={"one": first, "helper": helper},
        _projects={"normal": project},
        _config=_SearchConfig(tmp_path),
        _transcripts=_TranscriptStore({"one": first_path, "helper": helper_path}),
        workspace_scope=lambda workspace: ({"normal"}, {"one", "helper"}),
        _parse_transcript_messages=lambda _text: [],
    )

    result = search_workspace_chats(manager, "work", "MATCH", None)

    assert [item["chat_id"] for item in result["items"]] == ["one"]
    assert result["items"][0]["snippet"] == "A literal <b>match</b> here"
    assert "path" not in result["items"][0]
    assert manager._transcripts.lookups == ["one"]


def test_search_file_refuses_symlinks_and_non_regular_files(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("hello")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    assert _read_search_file(real, 8) == b"hello"
    assert _read_search_file(link, 8) is None
    assert _read_search_file(fifo, 8) is None

    link_dir = tmp_path / "linked-dir"
    link_dir.symlink_to(tmp_path, target_is_directory=True)
    assert _read_search_file(link_dir / "real.txt", 8) is None


def test_search_file_rejects_a_symlink_swap_at_open(tmp_path: Path, monkeypatch) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("secret")
    other = tmp_path / "other.txt"
    other.write_text("outside")
    original_open = os.open
    swapped = False

    def swap_on_final_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == victim.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            victim.unlink()
            victim.symlink_to(other)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_on_final_open)
    assert _read_search_file(victim, 32) is None
    assert swapped


def test_search_pages_are_bounded_and_cursor_is_query_bound(tmp_path: Path) -> None:
    chats = {}
    paths = {}
    for index in range(31):
        chat_id = f"chat-{index:02}"
        path = tmp_path / f"{chat_id}.json"
        path.write_text(json.dumps({"turns": [{"prompt": "needle"}]}))
        paths[chat_id] = path
        chats[chat_id] = SimpleNamespace(
            chat_id=chat_id, project_id="project", provider="claude", archived=False,
            archive_path="", helper={}, title="Saved", created_at="2026-01-01",
            last_activity_at=f"2026-01-{index + 1:02}",
        )
    project = SimpleNamespace(workspace="work", kind="", is_system=False, is_auto=False, name="Project")
    manager = SimpleNamespace(
        _chats=chats, _projects={"project": project}, _transcripts=_TranscriptStore(paths),
        _config=_SearchConfig(tmp_path),
        workspace_scope=lambda workspace: ({"project"}, set(chats)),
        _parse_transcript_messages=lambda _text: [],
    )

    first = search_workspace_chats(manager, "work", "needle", None)
    second = search_workspace_chats(manager, "work", "needle", first["next_cursor"])

    assert len(first["items"]) == 30
    assert first["next_cursor"]
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None
    try:
        search_workspace_chats(manager, "work", "another", first["next_cursor"])
    except ValueError:
        pass
    else:
        raise AssertionError("cursor must be bound to its normalized query")


def test_search_count_and_byte_budgets_continue_without_skipping_candidate(tmp_path: Path, monkeypatch) -> None:
    chats = {}
    paths = {}
    for index in range(101):
        chat_id = f"count-{index:03}"
        path = tmp_path / f"{chat_id}.json"
        path.write_text(json.dumps({"turns": [{"prompt": "target" if index == 100 else "other"}]}))
        paths[chat_id] = path
        chats[chat_id] = SimpleNamespace(
            chat_id=chat_id, project_id="project", provider="claude", archived=False,
            archive_path="", helper={}, handover_messages=[], title="Saved", created_at="2026-01-01",
            last_activity_at=f"2026-01-01T00:{100-index:03}:00",
        )
    for index in range(120):
        chat_id = f"helper-{index:03}"
        chats[chat_id] = SimpleNamespace(
            chat_id=chat_id, project_id="project", provider="claude", archived=False,
            archive_path="", helper={"kind": "internal"}, handover_messages=[], title="Internal",
            created_at="2026-01-01", last_activity_at=f"2027-01-01T00:{index:03}:00",
        )
    project = SimpleNamespace(workspace="work", kind="", is_system=False, is_auto=False, name="Project")
    manager = SimpleNamespace(_chats=chats, _projects={"project": project},
        _config=_SearchConfig(tmp_path),
        _transcripts=_TranscriptStore(paths), workspace_scope=lambda _w: ({"project"}, set(manager._chats)),
        _parse_transcript_messages=lambda _text: [])
    first = search_workspace_chats(manager, "work", "target", None)
    second = search_workspace_chats(manager, "work", "target", first["next_cursor"])
    assert first["items"] == [] and first["next_cursor"]
    assert [item["chat_id"] for item in second["items"]] == ["count-100"]

    # Sixteen maximum-sized files exhaust exactly 8 MiB. The next candidate
    # is kept at the head of the next cursor page rather than advanced over.
    files = {}
    byte_chats = {}
    for index in range(17):
        chat_id = f"bytes-{index:02}"
        path = tmp_path / f"{chat_id}.json"
        if index < 16:
            path.write_text(json.dumps({"turns": [{"prompt": "x" * (512 * 1024)}]}))
        else:
            path.write_text(json.dumps({"turns": [{"prompt": "needle"}]}))
        files[chat_id] = path
        byte_chats[chat_id] = SimpleNamespace(
            chat_id=chat_id, project_id="project", provider="claude", archived=False,
            archive_path="", helper={}, handover_messages=[], title="Needle title" if index == 0 else "Large" if index < 16 else "Saved",
            created_at="2026-01-01", last_activity_at=f"2026-02-{17-index:02}",
        )
    manager._chats = byte_chats
    manager._transcripts = _TranscriptStore(files)
    original_read = transcript_service._read_search_file
    reads: list[tuple[int, int]] = []

    def bounded_read(path: Path, limit: int):
        raw = original_read(path, limit)
        reads.append((limit, len(raw) if raw is not None else 0))
        return raw

    monkeypatch.setattr(transcript_service, "_read_search_file", bounded_read)
    first_bytes = search_workspace_chats(manager, "work", "needle", None)
    assert all(limit <= 512 * 1024 for limit, _ in reads)
    assert sum(min(size, limit) for limit, size in reads) <= 8 * 1024 * 1024
    assert manager._transcripts.parsed_count == 0
    second_bytes = search_workspace_chats(manager, "work", "needle", first_bytes["next_cursor"])
    assert manager._transcripts.parsed_count == 1
    assert [item["chat_id"] for item in first_bytes["items"]] == ["bytes-00"]
    assert first_bytes["items"][0]["snippet"] == ""
    assert first_bytes["skipped_large_transcripts"]
    assert [item["chat_id"] for item in second_bytes["items"]] == ["bytes-16"]


def test_search_drops_chat_moved_during_bounded_read(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "chat.json"
    path.write_text(json.dumps({"turns": [{"prompt": "needle"}]}))
    chat = SimpleNamespace(chat_id="chat", project_id="project", provider="claude", archived=False,
        archive_path="", helper={}, handover_messages=[], title="Saved", created_at="date", last_activity_at="date")
    project = SimpleNamespace(workspace="work", kind="", is_system=False, is_auto=False, name="Project")
    manager = SimpleNamespace(_chats={"chat": chat}, _projects={"project": project},
        _config=_SearchConfig(tmp_path),
        _transcripts=_TranscriptStore({"chat": path}), workspace_scope=lambda _w: ({"project"}, {"chat"}),
        _parse_transcript_messages=lambda _text: [])
    original_read = __import__("ciao.web.transcript_service", fromlist=["_read_search_file"])._read_search_file

    def move_during_read(*args):
        data = original_read(*args)
        project.workspace = "other"
        return data

    monkeypatch.setattr("ciao.web.transcript_service._read_search_file", move_during_read)
    assert search_workspace_chats(manager, "work", "needle", None)["items"] == []


def test_search_rechecks_roots_and_internal_eligibility_after_read(tmp_path: Path, monkeypatch) -> None:
    _client, active, _archived, manager = _search_client(tmp_path)
    original_read = transcript_service._read_search_file

    with monkeypatch.context() as scoped:
        def hide_chat(*args):
            data = original_read(*args)
            active.helper = {"kind": "memory_pass"}
            return data

        scoped.setattr(transcript_service, "_read_search_file", hide_chat)
        hidden_chat = search_workspace_chats(manager, "work", "active needle", None)
        assert active.chat_id not in {item["chat_id"] for item in hidden_chat["items"]}

    active.helper = {}
    with monkeypatch.context() as scoped:
        def move_workspace_root(*args):
            data = original_read(*args)
            manager._config.workspaces["work"].vault_root = "memory-vault/other"
            return data

        scoped.setattr(transcript_service, "_read_search_file", move_workspace_root)
        moved_root = search_workspace_chats(manager, "work", "active needle", None)
        assert active.chat_id not in {item["chat_id"] for item in moved_root["items"]}


def _search_client(tmp_path: Path):
    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    for workspace in ("work", "other"):
        (install / "memory-vault" / workspace).mkdir(parents=True)
    config = CiaoConfig(pwa_auth_token="test-token", workspace_root=install,
        state_path=runtime / "state.json", media_root=runtime / "media", vault_root=install / "memory-vault",
        workspaces={"work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
                    "other": WorkspaceConfig(name="other", vault_root="memory-vault/other")})
    manager = ProjectChatManager(config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(runtime, runtime / "transcripts"), path=runtime / "web_projects.json")
    project = manager.create_project("Project", workspace="work")
    active = manager.create_chat(project.project_id, title="Active")
    current = manager._transcripts.current_path(__import__("ciao.models", fromlist=["ChatContext"]).ChatContext.for_web(active.chat_id), "claude")
    current.parent.mkdir(parents=True)
    current.write_text(json.dumps({"turns": [{"prompt": "active needle"}]}))
    archived = manager.create_chat(project.project_id, title="Archived")
    archive_path = config.chats_root("work") / archived.chat_id / "claude" / "saved.md"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_text(manager._transcripts._render_markdown({
        "provider": "claude", "started_at": "now", "ended_at": "now",
        "turns": [{"timestamp": "now", "prompt": "archived needle", "response": "reply"}],
    }))
    archived.archived = True
    archived.archive_path = str(archive_path)
    mismatched = manager.create_chat(project.project_id, title="Wrong archive owner")
    mismatched.archived = True
    mismatched.archive_path = str(archive_path)
    hidden = manager.create_chat(project.project_id, title="Internal")
    hidden.helper = {"kind": "memory_pass"}
    hidden_path = manager._transcripts.current_path(__import__("ciao.models", fromlist=["ChatContext"]).ChatContext.for_web(hidden.chat_id), "claude")
    hidden_path.parent.mkdir(parents=True)
    hidden_path.write_text(json.dumps({"turns": [{"prompt": "needle"}]}))
    other_project = manager.create_project("Other", workspace="other")
    other_chat = manager.create_chat(other_project.project_id, title="Other workspace")
    other_path = manager._transcripts.current_path(
        __import__("ciao.models", fromlist=["ChatContext"]).ChatContext.for_web(other_chat.chat_id), "claude")
    other_path.parent.mkdir(parents=True)
    other_path.write_text(json.dumps({"turns": [{"prompt": "needle"}]}))
    serializer = make_serializer("search-test-secret")
    app = Starlette(routes=[Route("/api/chats/search", search_chats, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.config = config
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    return client, active, archived, manager


def test_search_route_scopes_active_and_archived_content_and_validates_request(tmp_path: Path) -> None:
    client, active, archived, manager = _search_client(tmp_path)
    response = client.post("/api/chats/search", json={"workspace": "work", "q": "needle"},
                           headers={"origin": "http://testserver"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {item["chat_id"] for item in response.json()["items"]} == {active.chat_id, archived.chat_id}
    assert any(item["archived"] and item["snippet"].startswith("archived needle") for item in response.json()["items"])
    assert client.post("/api/chats/search", json={"workspace": "missing", "q": "ok"}).status_code == 404
    assert client.post("/api/chats/search", json={"workspace": "work", "q": "x"}).status_code == 400
    assert client.post("/api/chats/search", json={"workspace": "work", "q": " ab "}).status_code == 200
    assert client.post("/api/chats/search", json={"workspace": "work", "q": "x" * 201}).status_code == 400
    assert client.post("/api/chats/search", json={"workspace": "work", "q": "x" * 200}).status_code == 200
    assert client.post("/api/chats/search", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/chats/search", json=["work", "needle"]).status_code == 400
    assert client.post("/api/chats/search", json={"workspace": "work", "q": "needle", "cursor": "bad"}).status_code == 400
    assert client.post("/api/chats/search?q=needle&workspace=work").status_code == 400
    assert client.post("/api/chats/search", content=b"x" * 4097,
                       headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/api/chats/search", json={"workspace": "work", "q": "needle"},
                       headers={"origin": "http://evil.example"}).status_code == 403
    anonymous = TestClient(client.app, raise_server_exceptions=False)
    assert anonymous.post("/api/chats/search", json={"workspace": "work", "q": "needle"}).status_code == 401

    # Use the real current transcript projection and its cleanup rules: modern
    # and legacy context headers and the reload continuation stay hidden.
    current = manager._transcripts.current_path(
        __import__("ciao.models", fromlist=["ChatContext"]).ChatContext.for_web(active.chat_id), "claude")
    current.write_text(json.dumps({"turns": [
        {"prompt": "[CIAO_CONTEXT_BEGIN]\n[Project: secret]\n[CIAO_CONTEXT_END]\n\nvisible needle"},
        {"prompt": "[Project context: legacy secret]\n\nsecond visible needle"},
        {"prompt": "[Ciaobot] MCP servers, plugins and skills were reloaded and the session resumed. Continue the user's request."},
    ]}))
    cleaned = search_workspace_chats(manager, "work", "needle", None)
    active_item = next(item for item in cleaned["items"] if item["chat_id"] == active.chat_id)
    assert "secret" not in active_item["snippet"]
    assert active_item["snippet"].startswith("visible needle")
    assert active.chat_id not in {item["chat_id"] for item in search_workspace_chats(manager, "work", "MCP", None)["items"]}

    # Bad JSON in the bounded current file must not erase separately persisted
    # handover rows from the searchable projection.
    active.handover_messages = [{"role": "assistant", "content": "persisted handover phrase"}]
    current.write_text("{")
    handover = search_workspace_chats(manager, "work", "handover phrase", None)
    assert any(item["chat_id"] == active.chat_id for item in handover["items"])

    active.handover_messages = [
        {"role": "user", "content": "[Project context: handover secret]\n\nordinary words"},
        {"role": "user", "content": "[Ciaobot] MCP servers, plugins and skills were reloaded and the session resumed. Continue the user's request."},
    ]
    assert active.chat_id not in {item["chat_id"] for item in
                                  search_workspace_chats(manager, "work", "handover secret", None)["items"]}
    assert active.chat_id not in {item["chat_id"] for item in
                                  search_workspace_chats(manager, "work", "MCP servers", None)["items"]}

    active.handover_messages = [{"role": "assistant", "content": "oversized " + "x" * (512 * 1024) + " hiddenneedle"}]
    oversized = search_workspace_chats(manager, "work", "hiddenneedle", None)
    assert active.chat_id not in {item["chat_id"] for item in oversized["items"]}
    assert oversized["skipped_large_transcripts"]
