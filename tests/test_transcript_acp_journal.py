"""History for an ACP chat is rendered from its session journal, not the provider store (#3137)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import provider_registry
from ciao.provider_registry import ProviderDescriptor
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.session import SessionJournal, journal_file
from ciao.providers.acp.spec import AgentSpec
from ciao.web.transcript_service import _assemble_chat_messages, _tool_icon
from tests.test_chat_subagents import _make_manager

SPEC = AgentSpec(id="fake", binary="unused")


class FakeJournalProvider(AcpProvider):
    """Only ``read_thread`` matters here: the journal is written by the test."""

    spec = SPEC


def _chat_manager(tmp_path: Path, *, provider: str, session_id: str):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("acp", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="acp")
    chat.provider = provider
    chat.session_id = session_id
    pcm._save()
    return pcm, chat


@pytest.fixture
def acp_chat(tmp_path, monkeypatch):
    descriptor = ProviderDescriptor(
        id="fake",
        label="Fake agent",
        short_label="Fake",
        cli_label="Fake",
        factory_path="tests.test_transcript_acp_journal:FakeJournalProvider",
        acp_spec_path="tests.test_transcript_acp_journal:SPEC",
    )
    monkeypatch.setattr(provider_registry, "_DESCRIPTORS", (descriptor,))
    monkeypatch.setattr(provider_registry, "_BY_ID", {"fake": descriptor})
    pcm, chat = _chat_manager(tmp_path, provider="fake", session_id="sess-1")
    config = SimpleNamespace(
        workspace_root=tmp_path / "ws", state_path=tmp_path / ".runtime" / "state.json"
    )
    return pcm, chat, config


def _journal(config: Any, session_id: str) -> SessionJournal:
    return SessionJournal(journal_file(config.state_path.parent, "fake", session_id))


def _pairs(rows: list[dict]) -> list[tuple[str, str]]:
    return [(row["role"], row["content"]) for row in rows]


@pytest.mark.asyncio
async def test_acp_history_renders_user_assistant_tool_rows_in_order(acp_chat) -> None:
    pcm, chat, config = acp_chat
    journal = _journal(config, "sess-1")
    journal.user("hello")
    journal.event({"type": "text_delta", "text": "Hi"})
    journal.event({"type": "text_delta", "text": " there"})
    # Subagent text belongs to the subagent's own tool tree, not to the parent's reply.
    journal.event({"type": "text_delta", "text": "inner", "parent_tool_use_id": "tool-9"})
    journal.event({"type": "tool_use", "tool_name": "Bash", "tool_input": "ls", "tool_use_id": "t1"})
    journal.event({"type": "tool_result", "tool_name": "Bash", "tool_use_id": "t1"})
    journal.event({"type": "text_delta", "text": "Done"})
    journal.event({"type": "result", "text": "Hi thereDone", "is_error": False})
    journal.user("second")
    journal.event({"type": "text_delta", "text": "ok"})
    journal.event({"type": "result", "text": "ok", "is_error": False})

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _pairs(rows) == [
        ("user", "hello"),
        ("assistant", "Hi there"),
        ("system", f"{_tool_icon('Bash')} Bash ls"),
        ("assistant", "Done"),
        ("user", "second"),
        ("assistant", "ok"),
    ]
    assert rows[0]["turn_index"] == 0
    assert rows[4]["turn_index"] == 1
    assert rows[2]["tool_name"] == "_activity"


@pytest.mark.asyncio
async def test_acp_history_stitches_previous_session_ids(acp_chat) -> None:
    pcm, chat, config = acp_chat
    chat.previous_session_ids = ["sess-0"]
    first = _journal(config, "sess-0")
    first.user("first")
    first.event({"type": "text_delta", "text": "one"})
    first.event({"type": "result", "text": "one", "is_error": False})
    second = _journal(config, "sess-1")
    second.user("second")
    second.event({"type": "text_delta", "text": "two"})
    second.event({"type": "result", "text": "two", "is_error": False})

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _pairs(rows) == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
        ("assistant", "two"),
    ]
    assert [row["turn_index"] for row in rows if row["role"] == "user"] == [0, 1]


@pytest.mark.asyncio
async def test_acp_history_falls_back_to_local_transcript_without_journal(
    acp_chat, monkeypatch
) -> None:
    pcm, chat, config = acp_chat
    local = [
        {"role": "user", "content": "local question", "turn_index": 0},
        {"role": "assistant", "content": "local answer"},
    ]
    monkeypatch.setattr(
        pcm._transcripts, "current_messages", lambda _ctx, _provider: list(local)
    )

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _pairs(rows) == [("user", "local question"), ("assistant", "local answer")]


@pytest.mark.asyncio
async def test_acp_history_strips_context_capsule_from_user_rows(acp_chat) -> None:
    pcm, chat, config = acp_chat
    journal = _journal(config, "sess-1")
    journal.user(
        "[CIAO_CONTEXT_BEGIN]\nworkspace=/secret/ws-path\n[CIAO_CONTEXT_END]\n\nreal question"
    )
    journal.event({"type": "result", "text": "", "is_error": False})

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _pairs(rows) == [("user", "real question")]
    assert all("CIAO_CONTEXT" not in row["content"] for row in rows)
    assert all("/secret/ws-path" not in row["content"] for row in rows)
