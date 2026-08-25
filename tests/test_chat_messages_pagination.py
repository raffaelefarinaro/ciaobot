"""Tests for paginated /messages history (W4 backend).

The endpoint keeps its legacy flat-array shape when called without paging
params, and serves a ``{items,total,offset,limit,hasMore,nextOffset}``
envelope from the newest end when either param is present. Oversized
``_thinking`` rows are pruned with a lazy marker in envelope mode only, and
the full unpruned row stays fetchable from the part endpoint by index.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_message_part, chat_messages


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


class _Msg:
    def __init__(self, type_: str, content) -> None:
        self.type = type_
        self.message = {"role": type_, "content": content}


def _text_msg(type_: str, text: str) -> _Msg:
    return _Msg(type_, [{"type": "text", "text": text}])


def _thinking_msg(thinking: str) -> _Msg:
    return _Msg("assistant", [{"type": "thinking", "thinking": thinking}])


def _request(
    pcm: ProjectChatManager,
    config: CiaoConfig,
    chat_id: str,
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages",
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "query_string": query_string,
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=config),
        ),
    }
    return Request(scope)


def _part_request(
    pcm: ProjectChatManager,
    config: CiaoConfig,
    chat_id: str,
    query_string: bytes,
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages/part",
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "query_string": query_string,
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=config),
        ),
    }
    return Request(scope)


_SIX = [
    msg
    for n in range(3)
    for msg in (_text_msg("user", f"question {n}"), _text_msg("assistant", f"answer {n}"))
]

_SEGMENTS = {"sess-a": _SIX}


@pytest.mark.asyncio
async def test_no_params_keeps_legacy_flat_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Legacy shape", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="legacy-shape")
    chat.session_id = "sess-a"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: _SEGMENTS[session_id]),
    )

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    rows = json.loads(response.body.decode())

    assert isinstance(rows, list)
    assert [r["content"] for r in rows] == [
        "question 0", "answer 0", "question 1", "answer 1",
        "question 2", "answer 2",
    ]
    assert all("i" not in r and "lazy" not in r for r in rows)


@pytest.mark.asyncio
async def test_envelope_serves_newest_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Envelope tail", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="envelope-tail")
    chat.session_id = "sess-a"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: _SEGMENTS[session_id]),
    )

    response = await chat_messages(
        _request(pcm, pcm._config, chat.chat_id, b"limit=2")
    )
    body = json.loads(response.body.decode())

    assert body["total"] == 6
    assert body["offset"] == 0
    assert body["limit"] == 2
    assert body["hasMore"] is True
    assert body["nextOffset"] == 2
    # Newest end first window: the last two rows of the conversation.
    assert [item["i"] for item in body["items"]] == [4, 5]
    assert [item["content"] for item in body["items"]] == ["question 2", "answer 2"]


@pytest.mark.asyncio
async def test_envelope_older_page_ends_with_has_more_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Envelope older", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="envelope-older")
    chat.session_id = "sess-a"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: _SEGMENTS[session_id]),
    )

    response = await chat_messages(
        _request(pcm, pcm._config, chat.chat_id, b"offset=2&limit=8")
    )
    body = json.loads(response.body.decode())

    assert [item["i"] for item in body["items"]] == [0, 1, 2, 3]
    assert body["hasMore"] is False
    assert body["nextOffset"] is None


_LONG_THINKING = "x" * 1200


@pytest.mark.asyncio
async def test_envelope_prunes_long_thinking_with_lazy_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = {"sess-think": [_thinking_msg(_LONG_THINKING)]}
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Lazy think", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="lazy-think")
    chat.session_id = "sess-think"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: segments[session_id]),
    )

    response = await chat_messages(
        _request(pcm, pcm._config, chat.chat_id, b"limit=10")
    )
    body = json.loads(response.body.decode())
    row = body["items"][0]

    assert row["tool_name"] == "_thinking"
    assert row["lazy"] is True
    assert row["full_length"] == 1200
    assert "chars hidden" in row["content"]
    assert len(row["content"]) < 1200

    # Legacy mode must stay verbatim: no pruning, no lazy flag.
    legacy = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    rows = json.loads(legacy.body.decode())
    assert rows[0]["content"] == _LONG_THINKING
    assert "lazy" not in rows[0]


@pytest.mark.asyncio
async def test_part_endpoint_returns_unpruned_row_by_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = {
        "sess-part": [_text_msg("user", "go"), _thinking_msg(_LONG_THINKING)],
    }
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Part fetch", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="part-fetch")
    chat.session_id = "sess-part"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: segments[session_id]),
    )

    response = await chat_message_part(
        _part_request(pcm, pcm._config, chat.chat_id, b"i=1")
    )
    row = json.loads(response.body.decode())

    assert row["i"] == 1
    assert row["tool_name"] == "_thinking"
    assert row["content"] == _LONG_THINKING
    assert "lazy" not in row


@pytest.mark.asyncio
async def test_part_endpoint_rejects_bad_indices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    segments = {"sess-part2": [_text_msg("user", "only one")]}
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Part bounds", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="part-bounds")
    chat.session_id = "sess-part2"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(get_session_messages=lambda session_id, directory: segments[session_id]),
    )

    missing = await chat_message_part(
        _part_request(pcm, pcm._config, chat.chat_id, b"i=99")
    )
    assert missing.status_code == 404

    invalid = await chat_message_part(
        _part_request(pcm, pcm._config, chat.chat_id, b"i=abc")
    )
    assert invalid.status_code == 400
