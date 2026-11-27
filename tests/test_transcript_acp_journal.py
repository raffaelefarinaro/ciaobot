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


@pytest.mark.asyncio
async def test_acp_history_numbers_user_rows_by_engine_turn_when_journal_lacks_early_prompts(
    acp_chat, monkeypatch
) -> None:
    """A journal rebuilt from a loadSession replay holds no user prompts for the turns it
    replayed (``user_message_chunk`` is silent), so its first user record is not turn 0.
    The rows must still carry the engine's turn index: the live ``user_echo`` of a turn is
    deduped by it, and a mismatch rendered that turn's user bubble twice and hung an older
    turn's send time and duration on its reply."""
    pcm, chat, config = acp_chat
    chat.user_turn_count = 4
    chat.user_turn_timings = {
        str(turn): {
            "sent_at": f"2026-11-27T1{turn}:00:00Z",
            "completed_at": f"2026-11-27T1{turn}:00:30Z",
            "duration_ms": 1000 * (turn + 1),
        }
        for turn in range(4)
    }
    durable = [
        row
        for turn, (prompt, reply) in enumerate(
            [("first", "one"), ("second", "two"), ("third", "three"), ("fourth", "four")]
        )
        for row in (
            {"role": "user", "content": prompt, "sent_at": f"2026-11-27T1{turn}:00:00Z"},
            {"role": "assistant", "content": reply},
        )
    ]
    monkeypatch.setattr(
        pcm._transcripts, "current_messages", lambda _ctx, _provider: list(durable)
    )
    journal = _journal(config, "sess-1")
    # Turns 0 and 1, as a replay rebuilt them: their events, without their prompts.
    for reply in ("one", "two"):
        journal.event({"type": "text_delta", "text": reply})
        journal.event({"type": "result", "text": reply, "is_error": False})
    # Turns 2 and 3, journaled live.
    for prompt, reply in (("third", "three"), ("fourth", "four")):
        journal.user(prompt)
        journal.event({"type": "text_delta", "text": reply})
        journal.event({"type": "result", "text": reply, "is_error": False})

    rows = await _assemble_chat_messages(pcm, config, chat)

    users = [row for row in rows if row["role"] == "user"]
    assert [(row["content"], row["turn_index"]) for row in users] == [
        ("third", 2),
        ("fourth", 3),
    ]
    assert [row["sent_at"] for row in users] == ["2026-11-27T12:00:00Z", "2026-11-27T13:00:00Z"]
    replies = {row["content"]: row for row in rows if row["role"] == "assistant"}
    assert replies["three"]["duration_ms"] == 3000
    assert replies["four"]["duration_ms"] == 4000



def _durable_turns(prompts: list[str], errors: tuple[int, ...] = ()) -> list[dict]:
    rows: list[dict] = []
    for turn, prompt in enumerate(prompts):
        rows.append({"role": "user", "content": prompt, "sent_at": f"sent-{turn}"})
        reply: dict = {"role": "assistant", "content": f"reply-{turn}", "usage": {"tok": str(turn)}}
        if turn in errors:
            reply["is_error"] = True
        rows.append(reply)
    return rows


def _counted_chat(acp_chat, monkeypatch, durable_prompts, count, errors=()):
    pcm, chat, config = acp_chat
    chat.user_turn_count = count
    chat.user_turn_timings = {
        str(turn): {"sent_at": f"s{turn}", "completed_at": f"c{turn}", "duration_ms": 1000 * (turn + 1)}
        for turn in range(count)
    }
    durable = _durable_turns(durable_prompts, errors)
    monkeypatch.setattr(pcm._transcripts, "current_messages", lambda _ctx, _provider: list(durable))
    return pcm, chat, config, _journal(config, "sess-1")


def _replayed(journal: SessionJournal, turns: int) -> None:
    """Turns as a load replay rebuilt them: their replies, without their prompts."""
    for turn in range(turns):
        journal.event({"type": "text_delta", "text": f"reply-{turn}"})
        journal.event({"type": "result", "text": "", "is_error": False})


def _live_turn(journal: SessionJournal, prompt: str, turn: int, *, done: bool = True) -> None:
    journal.user(prompt)
    journal.event({"type": "text_delta", "text": f"reply-{turn}"})
    if done:
        journal.event({"type": "result", "text": "", "is_error": False})


def _user_turns(rows: list[dict]) -> list[tuple[str, Any]]:
    return [(row["content"], row.get("turn_index")) for row in rows if row["role"] == "user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("done", [True, False], ids=["finished", "running"])
async def test_rebuilt_journal_start_matches_a_repeated_prompt_from_the_end(
    acp_chat, monkeypatch, done
) -> None:
    """Turn 1 and turn 6 are both 'continue'. Matching from the start paired the live prompt
    with turn 1, so it took turn 1's index, usage and duration."""
    prompts = ["a", "continue", "b", "c", "d", "e", "continue"]
    durable = prompts if done else prompts[:-1]
    pcm, chat, config, journal = _counted_chat(acp_chat, monkeypatch, durable, 7)
    _replayed(journal, 6)
    _live_turn(journal, "continue", 6, done=done)

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _user_turns(rows) == [("continue", 6)]
    if done:
        reply = [row for row in rows if row["role"] == "assistant"][-1]
        assert reply["content"] == "reply-6"
        assert reply["duration_ms"] == 7000
        assert reply["usage"] == {"tok": "6"}


@pytest.mark.asyncio
async def test_rebuilt_journal_start_without_a_durable_transcript_takes_the_last_turns(
    acp_chat, monkeypatch
) -> None:
    pcm, chat, config, journal = _counted_chat(acp_chat, monkeypatch, [], 4)
    _replayed(journal, 2)
    _live_turn(journal, "q", 2)
    _live_turn(journal, "r", 3)

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _user_turns(rows) == [("q", 2), ("r", 3)]


@pytest.mark.asyncio
async def test_rebuilt_journal_start_keeps_a_failed_send_the_journal_never_got(
    acp_chat, monkeypatch
) -> None:
    pcm, chat, config, journal = _counted_chat(
        acp_chat, monkeypatch, ["p0", "p1", "p2", "p3"], 4, errors=(1,)
    )
    _replayed(journal, 1)
    _live_turn(journal, "p2", 2)
    _live_turn(journal, "p3", 3)

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _user_turns(rows) == [("p1", 1), ("p2", 2), ("p3", 3)]


@pytest.mark.asyncio
async def test_full_journal_with_repeated_prompts_pairs_each_turn_with_its_own(
    acp_chat, monkeypatch
) -> None:
    prompts = ["continue", "continue", "x", "continue", "yes", "yes"]
    pcm, chat, config, journal = _counted_chat(acp_chat, monkeypatch, prompts, 6)
    for turn, prompt in enumerate(prompts):
        _live_turn(journal, prompt, turn)

    rows = await _assemble_chat_messages(pcm, config, chat)

    assert _user_turns(rows) == [(prompt, turn) for turn, prompt in enumerate(prompts)]
    replies = [row for row in rows if row["role"] == "assistant"]
    assert [row["usage"]["tok"] for row in replies] == [str(turn) for turn in range(6)]
    assert [row["duration_ms"] for row in replies] == [1000 * (turn + 1) for turn in range(6)]
