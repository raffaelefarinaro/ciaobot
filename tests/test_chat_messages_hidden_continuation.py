"""Hidden continuations never show as a user row and never shift turn_index.

The legacy code-mode continuation is no longer sent, but transcripts written
before code mode became automatic still hold it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.cli_envelopes import LEGACY_CODE_MODE_CONTINUE_PROMPT, RELOAD_CONTINUE_PROMPT
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import transcript_service
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages

CAPSULE = "[CIAO_CONTEXT_BEGIN]\nctx\n[CIAO_CONTEXT_END]\n\n"


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
    def __init__(self, type_: str, content: str) -> None:
        self.type = type_
        self.message = {"role": type_, "content": [{"type": "text", "text": content}]}


def _request(
    pcm: ProjectChatManager,
    config: CiaoConfig,
    chat_id: str,
    *,
    query_string: bytes = b"",
) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages",
        "query_string": query_string,
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=config),
        ),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_a_legacy_code_mode_continuation_keeps_turn_markers_aligned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript written while code mode was a switch still hides its continuation."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Linked", workspace="work")
    chat = pcm.create_chat(project.project_id, title="code-markers")
    cid = chat.chat_id

    chat.session_id = "bbec684a-36a8-5c2a-97b7-ccb38d8129d8"
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "initial"),
            _Msg("assistant", "a1"),
            _Msg("user", CAPSULE + LEGACY_CODE_MODE_CONTINUE_PROMPT),
            _Msg("assistant", "a2"),
            _Msg("user", "second"),
            _Msg("assistant", "a3"),
        ]
    )
    monkeypatch.setattr("ciao.transcripts.get_session_messages", fake_sdk.get_session_messages)

    response = await chat_messages(_request(pcm, pcm._config, cid))
    messages = json.loads(response.body)

    assert LEGACY_CODE_MODE_CONTINUE_PROMPT not in response.body.decode()
    users = [(m["content"], m["turn_index"]) for m in messages if m["role"] == "user"]
    assert users == [("initial", 0), ("second", 1)]


def test_archived_transcript_drops_the_continuation_user_row(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    text = f"""---
provider: claude
session_id: sess-archived
---

# Chat Transcript (claude)

## Turn 1

- Time: 2026-06-19T10:00:00Z
- Input kind: text
- Mode: auto
- Effective model: opus
- Images: 0

### User

```text
{CAPSULE}{LEGACY_CODE_MODE_CONTINUE_PROMPT}
```

### Assistant

```text
continued reply
```
"""
    rows = pcm._parse_transcript_messages(text)
    assert [(r["role"], r["content"]) for r in rows] == [("assistant", "continued reply")]


# --------------------------------------------------- completed prior answers
ANSWER_ONE = "I've built the invented preview. Open Workspace/preview.html."
NARRATION = "Now let me finish the housekeeping."
ANSWER_TWO = "Nothing in the code changed since my last message."
COMPLETED_AT = "2026-11-09T13:31:41Z"


def _completion_timings() -> dict:
    return {"0": {"completed_at": COMPLETED_AT, "duration_ms": 23000}}


def _claude_blocks(*blocks: dict) -> SimpleNamespace:
    return SimpleNamespace(
        type="assistant",
        message={"role": "assistant", "content": list(blocks)},
    )


def _claude_tool(name: str, path: Path, tool_id: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": {"file_path": str(path)},
    }


def _claude_continuation(
    tmp_path: Path, prompt: str, *, capsule: bool
) -> list:
    hidden = (CAPSULE + prompt) if capsule else prompt
    return [
        _Msg("user", "Build the invented preview."),
        _Msg("assistant", ANSWER_ONE),
        _Msg("user", hidden),
        _claude_blocks(
            {"type": "text", "text": NARRATION},
            _claude_tool("Read", tmp_path / "notes.md", "tu-read"),
            _claude_tool("Write", tmp_path / "preview.html", "tu-write"),
        ),
        _Msg("assistant", ANSWER_TWO),
        _Msg("user", "Thanks, next."),
    ]


def _claude_expected(tmp_path: Path) -> list[dict]:
    return [
        {"role": "user", "content": "Build the invented preview.", "turn_index": 0},
        {"role": "assistant", "content": ANSWER_ONE, "phase": "final_answer"},
        {"role": "assistant", "content": NARRATION},
        {
            "role": "system",
            "content": f"\U0001F4D6 Read {tmp_path / 'notes.md'}",
            "tool_name": "_activity",
        },
        {
            "role": "system",
            "tool_name": "_filecard",
            "content": "preview.html",
            "file_path": "preview.html",
            "action": "written",
            "tool": "Write",
        },
        {
            "role": "assistant",
            "content": ANSWER_TWO,
            "sent_at": COMPLETED_AT,
            "duration_ms": 23000,
        },
        {"role": "user", "content": "Thanks, next.", "turn_index": 1},
    ]


async def _claude_rows(
    pcm: ProjectChatManager,
    cid: str,
    monkeypatch: pytest.MonkeyPatch,
    messages: list,
    *,
    query_string: bytes = b"",
) -> list[dict]:
    monkeypatch.setattr(
        transcript_service,
        "_read_session_segment",
        lambda _sid, _dirs: messages,
    )
    response = await chat_messages(
        _request(pcm, pcm._config, cid, query_string=query_string)
    )
    return json.loads(response.body)


@pytest.mark.parametrize("prompt", [LEGACY_CODE_MODE_CONTINUE_PROMPT, RELOAD_CONTINUE_PROMPT])
@pytest.mark.parametrize("capsule", [False, True])
@pytest.mark.asyncio
async def test_claude_hidden_continuation_marks_completed_answer_without_adding_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
    capsule: bool,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Claude", workspace="work")
    chat = pcm.create_chat(project.project_id, title="claude-continue")
    chat.session_id = "5ae8436a-7f55-582e-8ef3-c24a8a4a9b6b"
    chat.user_turn_count = 2
    chat.user_turn_timings = _completion_timings()
    messages = _claude_continuation(tmp_path, prompt, capsule=capsule)

    rows = await _claude_rows(pcm, chat.chat_id, monkeypatch, messages)

    assert rows == _claude_expected(tmp_path)
    assert len(rows) == 7
    assert prompt not in json.dumps(rows)

    envelope = await _claude_rows(
        pcm,
        chat.chat_id,
        monkeypatch,
        messages,
        query_string=b"offset=0&limit=7",
    )
    assert envelope["total"] == 7
    assert [item["i"] for item in envelope["items"]] == list(range(7))


def test_mark_preceding_answer_respects_user_and_commentary_boundaries() -> None:
    mark = transcript_service._mark_preceding_answer

    def row(role: str, content: str = "", **extra: object) -> dict:
        return {"role": role, "content": content, **extra}

    def activity(text: str) -> dict:
        return row("system", text, tool_name="_activity")

    def thinking(text: str) -> dict:
        return row("system", text, tool_name="_thinking")

    def filecard(path: str) -> dict:
        return row("system", path, tool_name="_filecard")

    def answer() -> dict:
        return row("assistant", ANSWER_ONE)

    def tagged() -> dict:
        return {**answer(), "phase": "final_answer"}

    def older() -> dict:
        return row("assistant", "An older answer.")

    def comments() -> dict:
        return row("assistant", NARRATION, phase="commentary")

    def empty() -> dict:
        return row("assistant", "   ")

    def tool_backed() -> dict:
        return row("assistant", "Read b.md", tool_name="_activity")

    # Each case builds its actual and expected rows independently, so a helper
    # that mutates an input row (say, the commentary note) cannot mutate the
    # expectation it is compared against.
    cases: list[tuple[Callable[[], list[dict]], Callable[[], list[dict]]]] = [
        (lambda: [], lambda: []),
        (
            lambda: [row("user", "q"), activity("Read a.md"), thinking("hmm"),
                     filecard("a.md")],
            lambda: [row("user", "q"), activity("Read a.md"), thinking("hmm"),
                     filecard("a.md")],
        ),
        (
            lambda: [answer(), row("user", "next"), activity("Read a.md")],
            lambda: [answer(), row("user", "next"), activity("Read a.md")],
        ),
        (
            lambda: [older(), comments(), activity("Read a.md")],
            lambda: [older(), comments(), activity("Read a.md")],
        ),
        (
            lambda: [answer(), activity("Read a.md"), thinking("hmm"),
                     filecard("a.md"), empty(), tool_backed()],
            lambda: [tagged(), activity("Read a.md"), thinking("hmm"),
                     filecard("a.md"), empty(), tool_backed()],
        ),
        (
            lambda: [older(), answer(), activity("Read a.md")],
            lambda: [older(), tagged(), activity("Read a.md")],
        ),
    ]
    for build_rows, build_expected in cases:
        rows = build_rows()
        expected = build_expected()
        mark(rows)
        assert rows == expected
        # Idempotent: snapshot the exact rows, then a second call must leave
        # them identical rather than tagging an older note or appending a row.
        after_first = json.loads(json.dumps(rows))
        mark(rows)
        assert rows == after_first
    # An older note before the eligible answer stays untagged.
    rows = [older(), answer(), activity("Read a.md")]
    mark(rows)
    assert "phase" not in rows[0]
    assert rows[1]["phase"] == "final_answer"


@pytest.mark.parametrize("separate_answers", [False, True])
@pytest.mark.asyncio
async def test_consecutive_hidden_continuations_mark_answers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    separate_answers: bool,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Consecutive", workspace="work")
    chat = pcm.create_chat(project.project_id, title="consecutive")
    chat.user_turn_count = 2
    chat.user_turn_timings = _completion_timings()
    second_answer = "The second completed reply."
    if separate_answers:
        claude_messages = [
            _Msg("user", "First question."),
            _Msg("assistant", ANSWER_ONE),
            _Msg("user", LEGACY_CODE_MODE_CONTINUE_PROMPT),
            _Msg("assistant", second_answer),
            _Msg("user", RELOAD_CONTINUE_PROMPT),
            _claude_blocks(
                {"type": "text", "text": NARRATION},
                _claude_tool("Read", tmp_path / "notes.md", "tu-read"),
            ),
            _Msg("assistant", ANSWER_TWO),
            _Msg("user", "Next question."),
        ]
        expected_shape = [
            ("user", None, None, 0, "First question."),
            ("assistant", None, "final_answer", None, ANSWER_ONE),
            ("assistant", None, "final_answer", None, second_answer),
            ("assistant", None, None, None, NARRATION),
            ("system", "_activity", None, None, None),
            ("assistant", None, None, None, ANSWER_TWO),
            ("user", None, None, 1, "Next question."),
        ]
        expected_assistant_content = [ANSWER_ONE, second_answer, NARRATION, ANSWER_TWO]
    else:
        claude_messages = [
            _Msg("user", "First question."),
            _Msg("assistant", ANSWER_ONE),
            _Msg("user", LEGACY_CODE_MODE_CONTINUE_PROMPT),
            _Msg("user", RELOAD_CONTINUE_PROMPT),
            _claude_blocks(
                {"type": "text", "text": NARRATION},
                _claude_tool("Read", tmp_path / "notes.md", "tu-read"),
            ),
            _Msg("assistant", ANSWER_TWO),
            _Msg("user", "Next question."),
        ]
        expected_shape = [
            ("user", None, None, 0, "First question."),
            ("assistant", None, "final_answer", None, ANSWER_ONE),
            ("assistant", None, None, None, NARRATION),
            ("system", "_activity", None, None, None),
            ("assistant", None, None, None, ANSWER_TWO),
            ("user", None, None, 1, "Next question."),
        ]
        expected_assistant_content = [ANSWER_ONE, NARRATION, ANSWER_TWO]

    chat.session_id = "43af4230-3a25-5e66-a7af-5a6934cfe641"
    rows = await _claude_rows(pcm, chat.chat_id, monkeypatch, claude_messages)

    def shape(r: dict) -> tuple[object, ...]:
        # Tool-row content differs by provider ("Read ..." vs "read ..."), so
        # the provider-independent shape pins role, tool_name, phase and
        # turn_index, with exact content for user/assistant rows.
        return (
            r["role"],
            r.get("tool_name"),
            r.get("phase"),
            r.get("turn_index"),
            r["content"] if r["role"] in ("user", "assistant") else None,
        )

    assert len(rows) == len(expected_shape)
    assert [shape(r) for r in rows] == expected_shape
    assert [r["content"] for r in rows if r["role"] == "assistant"] == expected_assistant_content
    assert [(r["content"], r["turn_index"]) for r in rows if r["role"] == "user"] == [
        ("First question.", 0),
        ("Next question.", 1),
    ]
    assert LEGACY_CODE_MODE_CONTINUE_PROMPT not in json.dumps(rows)
    assert RELOAD_CONTINUE_PROMPT not in json.dumps(rows)
    assert next(r for r in rows if r["content"] == ANSWER_TWO)["sent_at"] == COMPLETED_AT


@pytest.mark.asyncio
async def test_hidden_continuation_without_preceding_answer_adds_no_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("NoAnswer", workspace="work")
    chat = pcm.create_chat(project.project_id, title="no-answer")
    chat.user_turn_count = 2
    chat.session_id = "4e2e8992-2b0b-5768-a5a6-b020917e8689"
    messages = [
        _Msg("user", "First question."),
        _Msg("user", LEGACY_CODE_MODE_CONTINUE_PROMPT),
        _Msg("assistant", ANSWER_TWO),
        _Msg("user", "Next question."),
    ]
    rows = await _claude_rows(pcm, chat.chat_id, monkeypatch, messages)

    assert rows == [
        {"role": "user", "content": "First question.", "turn_index": 0},
        {"role": "assistant", "content": ANSWER_TWO},
        {"role": "user", "content": "Next question.", "turn_index": 1},
    ]
    assert not any(r.get("phase") for r in rows)
    assert LEGACY_CODE_MODE_CONTINUE_PROMPT not in json.dumps(rows)


def _journal_chat() -> SimpleNamespace:
    return SimpleNamespace(
        user_turn_images={},
        user_turn_timings={},
        user_turn_unattended={},
        user_turn_origin={},
    )


def _render_journal(records: list[dict]) -> list[dict]:
    return transcript_service._render_acp_journal(
        records, _journal_chat(), start_user_idx=0, workspace_root=None, agent_root=None
    )


@pytest.mark.parametrize("prompt", [RELOAD_CONTINUE_PROMPT, LEGACY_CODE_MODE_CONTINUE_PROMPT])
@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "with-capsule"])
def test_acp_journal_skips_a_hidden_continuation(prompt: str, wrapped: bool) -> None:
    hidden_text = (CAPSULE + prompt) if wrapped else prompt
    rows = _render_journal([
        {"type": "user", "text": "one"},
        {"type": "text_delta", "text": ANSWER_ONE},
        {"type": "result"},
        {"type": "user", "text": hidden_text},
        {"type": "text_delta", "text": "Resumed."},
    ])

    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"]
    assert rows[0] == {
        "role": "user", "content": "one", "turn_index": 0,
    }
    # The answer before the hidden prompt is final, so it does not fold into Activity.
    assert rows[1] == {"role": "assistant", "content": ANSWER_ONE, "phase": "final_answer"}
    assert rows[2] == {"role": "assistant", "content": "Resumed."}
    assert all(prompt not in str(r.get("content") or "") for r in rows)


def test_acp_journal_hidden_continuation_does_not_shift_the_next_turn_index() -> None:
    rows = _render_journal([
        {"type": "user", "text": RELOAD_CONTINUE_PROMPT},
        {"type": "user", "text": "a normal prompt"},
    ])

    assert rows == [{"role": "user", "content": "a normal prompt", "turn_index": 0}]


def test_acp_journal_renders_a_normal_prompt_that_mentions_the_continuation() -> None:
    text = "please say " + RELOAD_CONTINUE_PROMPT
    rows = _render_journal([{"type": "user", "text": text}])

    assert rows == [{"role": "user", "content": text, "turn_index": 0}]


def test_acp_journal_without_a_hidden_marker_renders_unchanged() -> None:
    # An old journal row carries no turn_origin or other marker field; with no
    # hidden prompt in it, nothing is skipped and the rows are exactly as before.
    rows = _render_journal([
        {"type": "user", "text": "first", "turn_origin": ""},
        {"type": "text_delta", "text": "first answer"},
        {"type": "user", "text": "second"},
        {"type": "text_delta", "text": "second answer"},
    ])

    assert rows == [
        {"role": "user", "content": "first", "turn_index": 0},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second", "turn_index": 1},
        {"role": "assistant", "content": "second answer"},
    ]
