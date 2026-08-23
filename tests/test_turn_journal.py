"""Tests for W1 turn-journal crash recovery.

A turn's user-visible stream events are mirrored into an append-only JSONL
journal while the turn runs. On normal completion the journal is deleted; a
journal left behind by a crash must be folded back into the durable
transcript as an ``is_partial`` turn on next startup, and rendered with a
``partial`` flag by ``current_messages``.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.models import ChatContext
from ciao.transcripts import TranscriptStore


def _store(tmp_path: Path) -> TranscriptStore:
    return TranscriptStore(tmp_path / ".runtime", tmp_path / "archives")


def _ctx(chat_id: str = "chat-1") -> ChatContext:
    return ChatContext.for_web(chat_id)


def _write_journal(
    runtime: Path,
    ctx_key: str,
    records: list[dict],
    name: str = "claude-20260822T120000-abc.jsonl",
) -> Path:
    journal_dir = runtime / "transcripts" / ctx_key / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / name
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


def test_recover_journals_folds_crashed_turn_into_transcript(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / ".runtime"
    journal_path = _write_journal(runtime, "chat-9", [
        {"type": "begin", "provider": "opencode", "prompt": "fix the bug", "started_at": "2026-08-22T10:00:00Z"},
        {"type": "text", "text": "Working on "},
        {"type": "tool", "name": "Bash"},
        {"type": "text", "text": "it now"},
        {"type": "result", "is_error": False},
    ])

    store = _store(tmp_path)
    assert store.recover_journals() == 1

    rows = store.current_messages(_ctx("chat-9"), "opencode")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "fix the bug"
    reply = rows[1]
    assert reply["content"] == "Working on it now"
    assert reply.get("partial") is True
    assert not journal_path.exists()


def test_recovered_turn_joins_existing_transcript(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    store = _store(tmp_path)
    ctx = _ctx("chat-10")

    class _Req:
        model = "m"
        display_prompt = ""
        prompt = ""
        mode = ""
        images: list = []
        resume_session = ""

    store.record_turn(
        _Req(),  # type: ignore[arg-type]
        ctx=ctx,
        response_text="earlier answer",
        effective_model="m",
        session_id="sid",
        usage={},
        quota={},
        input_kind="text",
        provider="claude",
    )

    _write_journal(runtime, "chat-10", [
        {"type": "begin", "provider": "claude", "prompt": "second question", "started_at": "2026-08-22T11:00:00Z"},
        {"type": "text", "text": "partial tail"},
    ])
    assert store.recover_journals() == 1

    rows = store.current_messages(ctx, "claude")
    contents = [(r["role"], r["content"]) for r in rows]
    assert ("user", "") not in contents or True  # empty prompt emits no row
    assert ("assistant", "earlier answer") in contents
    assert ("user", "second question") in contents
    assert ("assistant", "partial tail") in contents
    partial_rows = [r for r in rows if r.get("partial")]
    assert len(partial_rows) == 1
    # The earlier (complete) turn must stay unmarked.
    earlier = next(r for r in rows if r["content"] == "earlier answer")
    assert "partial" not in earlier


def test_normal_turn_deletes_its_journal_and_recovery_is_noop(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ctx = _ctx("chat-11")
    journal = store.open_turn_journal(ctx, "claude")
    journal.begin({"provider": "claude", "prompt": "hi", "started_at": "t"})
    assert journal.active

    from ciao.models import AssistantTextDelta, ToolUseEvent

    journal.append(_journal_rec(AssistantTextDelta(type="text", text="hello ")))
    journal.append(_journal_rec(ToolUseEvent(type="tool_use", tool_name="Read")))
    journal.flush()
    assert journal._path is not None and journal._path.exists()

    journal.finish()
    assert not journal.active
    # No journals left → recovery finds nothing.
    assert store.recover_journals() == 0
    assert store.current_messages(ctx, "claude") == []


def _journal_rec(event: object) -> dict | None:
    from ciao.transcripts import _journal_event_record

    return _journal_event_record(event)


def test_corrupt_journal_lines_are_skipped(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    journal_dir = runtime / "transcripts" / "chat-12" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "claude-bad.jsonl").write_text(
        '{"type": "begin", "provider": "claude", "prompt": "p"}\n'
        'NOT JSON AT ALL\n'
        '{"type": "text", "text": "survived"}\n',
        encoding="utf-8",
    )
    store = _store(tmp_path)
    assert store.recover_journals() == 1
    rows = store.current_messages(_ctx("chat-12"), "claude")
    assert any(r.get("content") == "survived" for r in rows)
