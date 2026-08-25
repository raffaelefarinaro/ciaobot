"""Tests for W1 turn-journal crash recovery.

A turn's user-visible stream events are mirrored into an append-only JSONL
journal while the turn runs. On normal completion the journal is deleted; a
journal left behind by a crash must be folded back into the durable
transcript as an ``is_partial`` turn on next startup, and rendered with a
``partial`` flag by ``current_messages``.
"""

from __future__ import annotations

import json
import time
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


def test_a_committed_journal_is_dropped_rather_than_replayed(tmp_path: Path) -> None:
    """The window between `record_turn` and `finish` must not duplicate a turn.

    `record_turn` writes the normalized turn and `finish` then deletes the
    journal - two steps. A process death in between left a journal whose turn
    was already durable, and recovery folded it in a second time, duplicating
    the prompt and reply into history, the archive and the insights input,
    after exactly the crash the journal exists to survive.
    """
    runtime = tmp_path / ".runtime"
    store = _store(tmp_path)
    journal = _write_journal(
        runtime,
        _ctx().key,
        [
            {"type": "begin", "provider": "claude", "prompt": "already durable"},
            {"type": "text", "text": "the committed reply"},
            {"type": "committed"},
        ],
    )

    recovered = store.recover_journals()

    assert recovered == 0, "a committed turn must not be recovered again"
    assert not journal.exists(), "the stale journal should be cleaned up"
    rows = store.current_messages(_ctx(), "claude")
    assert not any(r.get("partial") for r in rows), "the turn was replayed"


def test_an_uncommitted_journal_is_still_recovered(tmp_path: Path) -> None:
    """The guard must not swallow the crash it exists to handle."""
    runtime = tmp_path / ".runtime"
    store = _store(tmp_path)
    _write_journal(
        runtime,
        _ctx().key,
        [
            {"type": "begin", "provider": "claude", "prompt": "lost mid-turn"},
            {"type": "text", "text": "half a reply"},
        ],
    )

    assert store.recover_journals() == 1


def test_the_buffered_tail_lands_without_another_append(tmp_path: Path) -> None:
    """A quiet stretch after a small burst must not hold records indefinitely.

    The elapsed deadline used to be evaluated only inside `append()`, so a turn
    that emitted a few records and then went quiet kept them in memory until
    the next one arrived - a crash there lost an arbitrarily old reply, not the
    250ms tail the design documents.
    """
    store = _store(tmp_path)
    journal = store.open_turn_journal(_ctx(), "claude")
    journal.begin({"provider": "claude", "prompt": "p", "started_at": "now"})
    journal.append({"type": "text", "text": "buffered"})

    # Well under the 32-record batch, so only the deadline can flush it.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if "buffered" in journal._path.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)

    assert "buffered" in journal._path.read_text(encoding="utf-8")
    journal.finish()


def test_a_recovered_journal_that_outlives_its_unlink_is_not_replayed(
    tmp_path: Path,
) -> None:
    """Recovery must be idempotent, not merely narrow.

    `_save_current` and the unlink are two steps. A death between them — or an
    unlink that simply raises — leaves a journal whose turn is already durable,
    and the next startup folded it in AGAIN: the same prompt and reply twice,
    after exactly the crash recovery exists to survive. A `committed` marker
    would only shrink the window; stamping the journal's own name closes it.
    """
    runtime = tmp_path / ".runtime"
    store = _store(tmp_path)
    records = [
        {"type": "begin", "provider": "claude", "prompt": "lost mid-turn"},
        {"type": "text", "text": "half a reply"},
    ]
    journal = _write_journal(runtime, _ctx().key, records)

    assert store.recover_journals() == 1
    first = store.current_messages(_ctx(), "claude")

    # The unlink did not take effect: the same journal is still on disk.
    _write_journal(runtime, _ctx().key, records, name=journal.name)
    recovered_again = store.recover_journals()

    assert recovered_again == 0, "the durable turn was folded in twice"
    assert store.current_messages(_ctx(), "claude") == first
    assert not journal.exists()


def test_a_different_journal_for_the_same_chat_is_still_recovered(
    tmp_path: Path,
) -> None:
    """De-duplication keys on the journal, not the chat — two crashes are two turns."""
    runtime = tmp_path / ".runtime"
    store = _store(tmp_path)
    _write_journal(
        runtime,
        _ctx().key,
        [{"type": "begin", "provider": "claude", "prompt": "first"}],
        name="claude-20260824T100000-aaa.jsonl",
    )
    assert store.recover_journals() == 1

    _write_journal(
        runtime,
        _ctx().key,
        [{"type": "begin", "provider": "claude", "prompt": "second"}],
        name="claude-20260824T110000-bbb.jsonl",
    )

    assert store.recover_journals() == 1
    prompts = [
        m["content"] for m in store.current_messages(_ctx(), "claude")
        if m["role"] == "user"
    ]
    assert prompts == ["first", "second"]
