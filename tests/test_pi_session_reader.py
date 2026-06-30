"""Tests for the Pi RPC transcript reader used by chat_messages."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ciao.web.routes_api import _read_pi_session_messages


def _write_session(chat_dir: Path, name: str, entries: list[dict]) -> Path:
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / name
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def _msg(role: str, content, **extra) -> dict:
    payload = {"role": role, "content": content, **extra}
    return {"type": "message", "id": f"e-{role}-{id(content)}", "parentId": None, "timestamp": "2026-05-19T11:00:00Z", "message": payload}


def test_empty_when_no_chat_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    assert _read_pi_session_messages("chat-missing") == []


def test_empty_when_no_jsonl(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    (tmp_path / "chat-x").mkdir()
    assert _read_pi_session_messages("chat-x") == []


def test_reads_user_and_assistant_text(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "hello"),
        _msg("assistant", [{"type": "text", "text": "hi there"}]),
    ])
    out = _read_pi_session_messages("chat-1")
    assert out == [
        {"role": "user", "content": "hello", "turn_index": 0},
        {"role": "assistant", "content": "hi there"},
    ]


def test_user_content_array_with_text_blocks(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]),
    ])
    out = _read_pi_session_messages("chat-1")
    assert out == [{"role": "user", "content": "line one\nline two", "turn_index": 0}]


def test_tool_calls_collapse_into_activity_group(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "do stuff"),
        _msg("assistant", [
            {"type": "toolCall", "id": "t1", "name": "Read", "arguments": {"file_path": "/a.py"}},
            {"type": "toolCall", "id": "t2", "name": "Grep", "arguments": {"pattern": "foo"}},
            {"type": "text", "text": "done"},
        ]),
    ])
    out = _read_pi_session_messages("chat-1")
    # The two tool calls fold into one _activity bubble, then the trailing
    # text becomes its own assistant bubble.
    assert len(out) == 3
    assert out[0] == {"role": "user", "content": "do stuff", "turn_index": 0}
    assert out[1]["role"] == "system"
    assert out[1]["tool_name"] == "_activity"
    assert "Read" in out[1]["content"]
    assert "/a.py" in out[1]["content"]
    assert "Grep" in out[1]["content"]
    assert out[2] == {"role": "assistant", "content": "done"}


def test_file_writes_become_standalone_filecards(monkeypatch, tmp_path) -> None:
    """Write/Edit/MultiEdit/NotebookEdit don't fold into _activity. They produce
    standalone _filecard entries the PWA can render as inline preview cards."""
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "draft and save"),
        _msg("assistant", [
            {"type": "toolCall", "id": "t1", "name": "Read", "arguments": {"file_path": "/a.py"}},
            {"type": "toolCall", "id": "t2", "name": "write", "arguments": {"path": "memory-vault/personal/Ideas/draft.md", "content": "..."}},
            {"type": "text", "text": "done"},
        ]),
    ])
    out = _read_pi_session_messages("chat-1")
    # user → _activity(Read) → _filecard(Write) → assistant(done)
    assert len(out) == 4
    assert out[1]["tool_name"] == "_activity"
    assert "Read" in out[1]["content"]
    assert out[2]["tool_name"] == "_filecard"
    assert out[2]["file_path"] == "memory-vault/personal/Ideas/draft.md"
    assert out[2]["action"] == "written"
    assert out[2]["tool"] == "write"
    assert out[3] == {"role": "assistant", "content": "done"}


def test_thinking_blocks_are_hidden(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("assistant", [
            {"type": "thinking", "thinking": "internal deliberation"},
            {"type": "text", "text": "answer"},
        ]),
    ])
    out = _read_pi_session_messages("chat-1")
    assert out == [{"role": "assistant", "content": "answer"}]


def test_unknown_roles_are_skipped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "go"),
        _msg("branchSummary", "branched"),
        _msg("compactionSummary", "compacted"),
        _msg("custom", "noise"),
        _msg("assistant", [{"type": "text", "text": "ok"}]),
    ])
    out = _read_pi_session_messages("chat-1")
    assert out == [
        {"role": "user", "content": "go", "turn_index": 0},
        {"role": "assistant", "content": "ok"},
    ]


def test_non_message_entries_skipped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    chat_dir = tmp_path / "chat-1"
    chat_dir.mkdir()
    (chat_dir / "s1.jsonl").write_text(
        json.dumps({"type": "session_header", "version": 3}) + "\n"
        + json.dumps({"type": "model_change", "provider": "ollama", "modelId": "x"}) + "\n"
        + json.dumps({"type": "message", "id": "m1", "parentId": None, "timestamp": "t",
                      "message": {"role": "user", "content": "hi"}}) + "\n"
        + "not-json garbage\n"
    )
    out = _read_pi_session_messages("chat-1")
    assert out == [{"role": "user", "content": "hi", "turn_index": 0}]


def test_picks_most_recently_modified_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    chat_dir = tmp_path / "chat-1"
    _write_session(chat_dir, "old.jsonl", [_msg("user", "old turn")])
    time.sleep(0.01)
    _write_session(chat_dir, "new.jsonl", [_msg("user", "new turn")])
    out = _read_pi_session_messages("chat-1")
    assert out == [{"role": "user", "content": "new turn", "turn_index": 0}]


def test_turn_index_increments_per_user_bubble(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "one"),
        _msg("assistant", [{"type": "text", "text": "ack"}]),
        _msg("user", "two"),
        _msg("assistant", [{"type": "text", "text": "ack2"}]),
    ])
    out = _read_pi_session_messages("chat-1")
    user_entries = [m for m in out if m["role"] == "user"]
    assert [u["turn_index"] for u in user_entries] == [0, 1]


def test_pi_reader_restores_pwa_image_refs_from_turn_map(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path))
    _write_session(tmp_path / "chat-1", "s1.jsonl", [
        _msg("user", "first with image"),
        _msg("assistant", [{"type": "text", "text": "ack"}]),
        _msg("user", "second no image"),
    ])
    out = _read_pi_session_messages(
        "chat-1",
        user_turn_images={"0": ["web_abc.png"]},
    )
    assert out[0] == {
        "role": "user",
        "content": "first with image",
        "turn_index": 0,
        "images": ["web_abc.png"],
    }
    assert "images" not in out[2]
