"""Bounded CLI listings: `chats list`, `chat get` and `memory-proposals`.

A single tool result stays in the agent's context for the rest of the run, so
these listings return one page (or a capped message tail) by default and say
how to get the rest.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ciao.control_plane as control_plane_module
from ciao import cli
from ciao.control_plane import CiaoControlPlane
from tests.test_agent_surface import _client, _post, _token
from tests.test_mcp_server import _service

COMPACT_KEYS = {
    "chat_id",
    "title",
    "project",
    "provider",
    "archived",
    "last_activity_at",
    "last_response",
}


class _ChatsPcm:
    """Fifty chats in one project, with activity rising by index."""

    def __init__(self, count: int = 50) -> None:
        self.projects = {
            "project-1": SimpleNamespace(project_id="project-1", name="Ciaobot", workspace="personal"),
        }
        self.chats: dict[str, SimpleNamespace] = {}
        for index in range(count):
            chat_id = f"chat-{index:02d}"
            chat = SimpleNamespace(
                chat_id=chat_id,
                project_id="project-1",
                title=f"Chat {index}",
                archived=False,
                provider="claude",
                mode="auto",
                created_at=f"2026-10-01T00:{index:02d}:00Z",
                last_activity_at=f"2026-10-02T00:{index:02d}:00Z",
                last_response=("reply " * 80) if index == count - 1 else "short reply",
                last_response_status="success",
                pending_question="",
                pending_permission="",
            )
            chat.to_dict = lambda local=True, _c=chat: {"chat_id": _c.chat_id, "title": _c.title}
            self.chats[chat_id] = chat

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [p for p in self.projects.values() if workspace is None or p.workspace == workspace]

    def get_chat(self, chat_id: str):
        return self.chats.get(chat_id)

    def list_chats(self, project_id: str | None = None):
        return [c for c in self.chats.values() if project_id is None or c.project_id == project_id]

    def is_session_local(self, _chat) -> bool:
        return True

    def get_active_stream(self, _chat_id):
        return None


def _chat_service(tmp_path: Path, count: int = 50):
    service, _fake = _service(tmp_path)
    pcm = _ChatsPcm(count)
    plane = CiaoControlPlane(
        SimpleNamespace(workspace=lambda name: object() if name == "personal" else None),
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
    )
    service.bind(plane)
    return service, pcm


def test_chats_list_returns_one_newest_first_page(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chats_list", {"limit": 20, "compact": True})
    data = response.json()["data"]
    chats = data["chats"]
    assert len(chats) == 20
    assert chats[0]["chat_id"] == "chat-49"
    assert chats[-1]["chat_id"] == "chat-30"
    assert data["total"] == 50
    assert data["truncated"] is True
    assert data["next_offset"] == 20
    assert set(chats[1]) == COMPACT_KEYS


def test_compact_rows_cap_the_last_response(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chats_list", {"limit": 20, "compact": True})
    newest = response.json()["data"]["chats"][0]
    assert len(newest["last_response"]) == 200
    assert newest["last_response_truncated"] is True
    assert newest["project"] == "Ciaobot"
    assert set(response.json()["data"]["chats"][1]) == COMPACT_KEYS


def test_the_last_page_carries_no_next_offset(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chats_list", {"limit": 20, "offset": 40, "compact": True})
    data = response.json()["data"]
    assert len(data["chats"]) == 10
    assert data["total"] == 50
    assert "truncated" not in data
    assert "next_offset" not in data


def test_full_rows_keep_the_old_row_shape_under_pagination(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chats_list", {"limit": 20})
    rows = response.json()["data"]["chats"]
    assert len(rows) == 20
    assert {"active_turn", "needs_attention", "last_response_status"} <= set(rows[0])
    assert "last_response_truncated" not in rows[0]


def test_no_limit_is_the_unpaged_legacy_list(tmp_path: Path) -> None:
    """PWA and MCP callers pass no limit and still get every row, unwrapped."""
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chats_list", {})
    data = response.json()["data"]
    assert isinstance(data, list)
    assert len(data) == 50


def test_out_of_range_pages_are_refused(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    token = _token(service, "chat-00")
    with _client(service) as client:
        too_big = _post(client, token, "chats_list", {"limit": 201})
        negative = _post(client, token, "chats_list", {"limit": 5, "offset": -1})
    assert too_big.json()["error"]["code"] == "invalid_request"
    assert negative.json()["error"]["code"] == "invalid_request"


def _long_history(count: int) -> list[dict]:
    rows = [{"role": "user", "content": f"message {i}"} for i in range(count)]
    rows[-1] = {"role": "assistant", "content": "x" * 5000}
    return rows


def test_chat_get_returns_recent_messages_with_truncation_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_messages(_pcm, _config, _chat):
        return _long_history(15)

    monkeypatch.setattr(control_plane_module, "_assemble_chat_messages", fake_messages)
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(
            client,
            _token(service, "chat-00"),
            "chat_get",
            {"chat_id": "chat-00", "messages": 10, "message_chars": 2000},
        )
    data = response.json()["data"]
    assert data["chat_id"] == "chat-00"
    assert data["messages_total"] == 15
    assert data["messages_truncated"] is True
    assert len(data["messages"]) == 10
    assert data["messages"][0]["content"] == "message 5"
    newest = data["messages"][-1]
    assert len(newest["content"]) == 2000
    assert newest["truncated"] is True
    assert newest["content_chars"] == 5000
    assert "truncated" not in data["messages"][0]


def test_chat_get_full_returns_every_message_untruncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_messages(_pcm, _config, _chat):
        return _long_history(15)

    monkeypatch.setattr(control_plane_module, "_assemble_chat_messages", fake_messages)
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chat_get", {"chat_id": "chat-00", "messages": 0})
    data = response.json()["data"]
    assert len(data["messages"]) == 15
    assert len(data["messages"][-1]["content"]) == 5000
    assert "truncated" not in data["messages"][-1]
    assert "messages_truncated" not in data


def test_chat_get_without_messages_is_the_metadata_only_shape(tmp_path: Path) -> None:
    service, _pcm = _chat_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service, "chat-00"), "chat_get", {"chat_id": "chat-00"})
    data = response.json()["data"]
    assert "messages" not in data
    assert data["chat_id"] == "chat-00"


def _proposal_queue(vault: Path, count: int) -> None:
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text(
        "\n".join(f"- [memory] Proposal number {i:03d} about the workspace" for i in range(count)) + "\n",
        encoding="utf-8",
    )


def _proposal_args(tmp_path: Path, count: int) -> list[str]:
    """An explicit vault root and workspace, so the queue is the one written here."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = tmp_path / "vault"
    _proposal_queue(vault, count)
    return ["--workspace", str(workspace), "--vault-root", str(vault)]


def test_memory_proposals_pages_with_next_offset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _proposal_args(tmp_path, 45)
    assert cli.main(["memory-proposals", *base, "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first["proposals"]) == 20
    assert first["total"] == 45
    assert first["truncated"] is True
    assert first["next_offset"] == 20

    assert cli.main(["memory-proposals", *base, "--json", "--offset", "40"]) == 0
    last = json.loads(capsys.readouterr().out)
    assert [row["text"].split()[2] for row in last["proposals"]] == ["040", "041", "042", "043", "044"]
    assert "next_offset" not in last
    assert "truncated" not in last


def test_memory_proposals_text_output_names_the_next_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _proposal_args(tmp_path, 25)
    assert cli.main(["memory-proposals", *base, "--limit", "10"]) == 0
    out = capsys.readouterr().out
    assert out.count("- [memory]") == 10
    assert "--offset 10" in out


def test_memory_proposals_rejects_a_limit_above_the_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _proposal_args(tmp_path, 3)
    assert cli.main(["memory-proposals", *base, "--limit", "500"]) == 2
    assert "between 1 and 200" in capsys.readouterr().err
