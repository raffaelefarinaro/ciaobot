"""The turn footer's token/context data on reload.

Claude chats render history from the SDK session JSONL, which carries no
token usage — the durable transcript records it (including the CLI's
context-window %). The assembly path must overlay that metadata the same
way the opencode branch does, or every reloaded Claude turn shows only
"13:13 · 1m 36s" in its footer while opencode turns show
"Tokens N in · N out · X% ctx".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao.web import routes_api as ra


def _chat(**overrides) -> SimpleNamespace:
    base = dict(
        chat_id="chat-1",
        session_id="sess-1",
        previous_session_ids=[],
        provider="claude",
        archived=False,
        archive_path="",
        handover_messages=[],
        user_turn_images={},
        user_turn_timings={},
        user_turn_unattended={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _pcm(transcript_rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        _transcripts=SimpleNamespace(
            current_messages=lambda _ctx, _provider: transcript_rows,
        ),
        _agent_root_for_chat=lambda _chat_id: "/tmp",
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(workspace_root="/tmp")


def _sdk_turn(user_text: str, answer_text: str) -> list:
    """One turn of SDK-shaped session entries (user + assistant)."""
    return [
        SimpleNamespace(type="user", message={"content": user_text}),
        SimpleNamespace(
            type="assistant",
            message={"content": [{"type": "text", "text": answer_text}]},
        ),
    ]


@pytest.mark.asyncio
async def test_claude_history_rows_carry_transcript_usage(monkeypatch) -> None:
    transcript_rows = [
        {"role": "user", "content": "hello", "turn_index": 0, "sent_at": "2026-08-31T11:11:39Z"},
        {
            "role": "assistant",
            "content": "The answer",
            "sent_at": "2026-08-31T11:13:15Z",
            "effective_model": "claude-opus-5",
            "usage": {
                "input_tokens": "14",
                "output_tokens": "5842",
                "context_pct": "95.7%",
            },
        },
    ]
    monkeypatch.setattr(
        ra, "_read_session_segment", lambda _sid, _dirs: _sdk_turn("hello", "The answer")
    )

    rows = await ra._assemble_chat_messages(
        _pcm(transcript_rows), _config(), _chat()
    )

    assistant = [r for r in rows if r.get("role") == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].get("usage", {}).get("context_pct") == "95.7%"
    assert assistant[0].get("usage", {}).get("output_tokens") == "5842"
    assert assistant[0].get("effective_model") == "claude-opus-5"


@pytest.mark.asyncio
async def test_claude_history_usage_lands_on_the_turns_last_assistant_row(
    monkeypatch,
) -> None:
    """A Claude turn renders several assistant bubbles (multi-text turns);
    the footer reads the turn's meta off the LAST one."""
    transcript_rows = [
        {"role": "user", "content": "hi", "turn_index": 0},
        {
            "role": "assistant",
            "content": "second part",
            "usage": {"input_tokens": "6", "output_tokens": "571", "context_pct": "22.3%"},
        },
    ]
    segment = [
        SimpleNamespace(type="user", message={"content": "hi"}),
        SimpleNamespace(
            type="assistant",
            message={"content": [{"type": "text", "text": "first part"}]},
        ),
        SimpleNamespace(
            type="assistant",
            message={"content": [{"type": "text", "text": "second part"}]},
        ),
    ]
    monkeypatch.setattr(ra, "_read_session_segment", lambda _sid, _dirs: segment)

    rows = await ra._assemble_chat_messages(_pcm(transcript_rows), _config(), _chat())

    assistant = [r for r in rows if r.get("role") == "assistant"]
    assert [r["content"] for r in assistant] == ["first part", "second part"]
    assert "usage" not in assistant[0]
    assert assistant[-1].get("usage", {}).get("context_pct") == "22.3%"


@pytest.mark.asyncio
async def test_claude_history_without_transcript_still_renders(monkeypatch) -> None:
    """No durable transcript (pruned runtime) must not break history."""
    monkeypatch.setattr(
        ra, "_read_session_segment", lambda _sid, _dirs: _sdk_turn("hello", "Answer")
    )

    rows = await ra._assemble_chat_messages(_pcm([]), _config(), _chat())

    assert [r["content"] for r in rows if r.get("role") == "assistant"] == ["Answer"]


def test_archived_transcript_parses_usage_section() -> None:
    from ciao.web.project_chats import ProjectChatManager

    md = """# Chat

## Turn 1

- Time: 2026-08-31T11:13:15Z

### User

```text
hello
```

### Assistant

```text
The answer.
```

### Usage

- cache_creation_input_tokens: 140052
- cache_read_input_tokens: 811277
- context_pct: 95.7%
- input_tokens: 14
- output_tokens: 5842

### Quota

- rateLimitType: seven_day
- status: allowed_warning

## Turn 2

- Time: 2026-08-31T11:15:06Z

### User

```text
again
```

### Assistant

```text
More.
```
"""
    pcm = ProjectChatManager.__new__(ProjectChatManager)
    rows = pcm._parse_transcript_messages(md)

    assistant = [r for r in rows if r["role"] == "assistant"]
    assert [r["content"] for r in assistant] == ["The answer.", "More."]
    assert assistant[0]["usage"] == {
        "cache_creation_input_tokens": "140052",
        "cache_read_input_tokens": "811277",
        "context_pct": "95.7%",
        "input_tokens": "14",
        "output_tokens": "5842",
    }
    # A turn without a Usage section simply carries none.
    assert "usage" not in assistant[1]
    # Quota lines are not leaked into usage.
    assert "rateLimitType" not in assistant[0]["usage"]
