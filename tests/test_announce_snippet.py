"""The final-answer excerpt behind unread previews and result pushes (#2139).

The Home unread card and the push body must show the same text the chat shows
as the turn's answer: the assistant text after the turn's last top-level tool
call, rendered as plain text before it is truncated. The durable transcript,
the provider handover and the schedule classifier keep the complete turn text,
so these tests pin both halves of that split.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ciao.models import AssistantTextDelta, ResultEvent, ThinkingEvent, ToolUseEvent
from ciao.providers.opencode import OpencodeProvider
from ciao.transcripts import FinalAnswerTracker
from ciao.web.chat_broker import event_to_json
from ciao.web.chat_streaming import _final_answer_text
from ciao.web.project_chats import ProjectChatManager, _reply_plain_text

INTERIM = "Interim **working** narration."
FINAL = "Final **answer** in `file.md`."
WHOLE_TURN = f"{INTERIM}\n\n{FINAL}"


# ── Plain-text rendering ───────────────────────────────────────────────────


def test_snippet_strips_markdown_to_plain_text() -> None:
    text = (
        "# Summary\n\n"
        "The run is **done**: see `lesson-34.md` and "
        "[the log](https://example.test/log).\n"
        "![chart](chart.png)\n\n"
        "> quoted &amp; noted\n"
        "- first item\n"
        "1. second item\n\n"
        "```bash\nls -la\n```\n"
    )

    assert ProjectChatManager._result_snippet(text) == (
        "Summary The run is done: see lesson-34.md and the log. "
        "chart quoted & noted first item second item ls -la"
    )


def test_snippet_strips_star_and_underscore_emphasis() -> None:
    assert ProjectChatManager._result_snippet(
        "It is *important*, _quiet_, and __bold__."
    ) == "It is important, quiet, and bold."
    assert ProjectChatManager._result_snippet(
        "**bold _inner_ and *star***"
    ) == "bold inner and star"
    assert ProjectChatManager._result_snippet("(see _the log_)") == "(see the log)"


def test_snippet_keeps_underscored_identifiers_and_arithmetic() -> None:
    text = "Run snake_case_name, my_file_v2.md and a_b_c: 2 * 3 * 4 = 24."

    assert ProjectChatManager._result_snippet(text) == text


def test_snippet_renders_before_truncating_and_caps_at_the_limit() -> None:
    assert ProjectChatManager._result_snippet("First line\n\nSecond line") == (
        "First line Second line"
    )
    # The markers must not spend the budget: ten visible characters fit.
    assert ProjectChatManager._result_snippet("**abcdefghij**", limit=10) == (
        "abcdefghij"
    )
    assert ProjectChatManager._result_snippet("**abcdefghijk**", limit=10) == (
        "abcdefg..."
    )
    assert ProjectChatManager._result_snippet("") == ""


def test_snippet_default_cap_sizes_the_two_line_card() -> None:
    capped = ProjectChatManager._result_snippet("word " * 100)

    assert len(capped) == 280
    assert capped.endswith("...")


def test_error_snippets_are_plain_text_too() -> None:
    assert ProjectChatManager._result_snippet(
        "API Error: **quota** exceeded on `plan-x`"
    ) == "API Error: quota exceeded on plan-x"


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_replies_have_no_excerpt(text: str) -> None:
    assert ProjectChatManager._result_snippet(text) == ""


def test_snippet_leaves_temporary_paths_in_prose_alone() -> None:
    # A path is prose, not code: nothing here may be mistaken for a marker.
    text = "Wrote /tmp/a/b_c.txt and kept dir_name/file_v2.md."
    assert ProjectChatManager._result_snippet(text) == text


def test_snippet_keeps_escaped_markdown_literal() -> None:
    assert ProjectChatManager._result_snippet(
        r"Escaped \*literal\* and **bold**"
    ) == "Escaped *literal* and bold"
    assert ProjectChatManager._result_snippet(
        r"*real* and \*not\* and **bold \*inner\* text**"
    ) == "real and *not* and bold *inner* text"


def test_snippet_escaped_code_and_link_markers_stay_literal() -> None:
    assert ProjectChatManager._result_snippet(
        r"\`not code\` and *real*"
    ) == "`not code` and real"
    assert ProjectChatManager._result_snippet(
        r"\[not a link\](x) and [a link](https://x)"
    ) == "[not a link](x) and a link"


def test_snippet_escaped_code_span_keeps_its_backslash() -> None:
    # A backslash inside a code span is literal, so the closing backtick still
    # closes the span and the emphasis after it renders (reviewer case).
    assert ProjectChatManager._result_snippet("`x\\` **bold**") == "x\\ bold"


def test_snippet_uses_commonmark_text_for_control_bytes() -> None:
    # CommonMark replaces NUL with U+FFFD, and that normalization is the
    # contract now. What this pins is that the text is not aliased into a
    # code-span placeholder: the old renderer turned this into "a and a".
    assert ProjectChatManager._result_snippet("`a` and \x000\x00") == (
        "a and \ufffd0\ufffd"
    )
    # A private-use character in the message is ordinary text.
    assert ProjectChatManager._result_snippet("`a` and \ue000x") == "a and \ue000x"


def _elapsed(text: str) -> float:
    start = time.perf_counter()
    _reply_plain_text(text)
    return time.perf_counter() - start


def test_snippet_scales_linearly_on_malformed_markdown() -> None:
    """A hostile reply must not cost quadratic time in the excerpt walk.

    This is a scaling guard, not a latency budget: 8x the input may not cost
    more than ~16x the time (plus a floor for scheduler noise). Unmatched
    brackets, half-formed links and repeated code spans all produce large
    token lists, where a quadratic string walk would blow past that bound.
    """
    small_brackets = "[" * 2_000
    large_brackets = "[" * 16_000
    small_links = "[a](b) [" * 2_000
    large_links = "[a](b) [" * 16_000
    small_spans = "`x` " * 2_000
    large_spans = "`x` " * 16_000

    assert _reply_plain_text(large_brackets) == large_brackets
    assert _reply_plain_text(large_links) == "a [" * 16_000
    assert _reply_plain_text(large_spans) == " ".join(["x"] * 16_000)

    small = _elapsed(small_brackets) + _elapsed(small_links) + _elapsed(small_spans)
    large = _elapsed(large_brackets) + _elapsed(large_links) + _elapsed(large_spans)
    assert large <= 16 * small + 1.0


# ── The turn's final answer segment ────────────────────────────────────────


def test_result_event_exposes_final_text_metadata() -> None:
    assert ResultEvent(type="result", result="whole").final_text is None
    assert ResultEvent(
        type="result", result="whole", final_text="last"
    ).final_text == "last"


def test_result_payload_does_not_publish_final_text() -> None:
    payload = event_to_json(
        ResultEvent(type="result", result="whole", final_text="last")
    )

    assert payload is not None
    assert payload["text"] == "whole"
    assert "final_text" not in payload


def test_final_answer_tracker_resets_on_tool_use_but_not_thinking() -> None:
    tracker = FinalAnswerTracker()
    tracker.feed(AssistantTextDelta(type="text", text="before"))
    tracker.feed(ThinkingEvent(type="thinking", text="reasoning"))
    tracker.feed(AssistantTextDelta(type="text", text=" after"))

    # Thinking does not end the response bubble, so the text stays one block.
    assert tracker.text == "before after"
    assert tracker.split is False

    tracker.feed(ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls"))
    assert tracker.split is True
    assert tracker.text == ""

    tracker.feed(AssistantTextDelta(type="text", text=FINAL))
    assert tracker.split is True
    assert tracker.text == FINAL

    # A call's completion frame marks the trace line; it does not end the
    # response bubble, so a late one cannot clear the answer.
    tracker.feed(
        ToolUseEvent(
            type="tool_result", tool_name="Bash", tool_use_id="tool-1"
        )
    )
    assert tracker.text == FINAL


def test_final_answer_tracker_ignores_subagent_text() -> None:
    tracker = FinalAnswerTracker()
    tracker.feed(AssistantTextDelta(type="text", text="parent "))
    tracker.feed(
        AssistantTextDelta(
            type="text", text="subagent chatter", parent_tool_use_id="tool-1"
        )
    )
    tracker.feed(
        ToolUseEvent(
            type="assistant",
            tool_name="Task",
            tool_input="explore",
            parent_tool_use_id="tool-1",
        )
    )

    assert tracker.text == "parent"
    assert tracker.split is False


def test_final_answer_tracker_joins_many_fragments() -> None:
    tracker = FinalAnswerTracker()
    fragments = [f"part {index}. " for index in range(5_000)]
    for fragment in fragments:
        tracker.feed(AssistantTextDelta(type="text", text=fragment))

    assert tracker.text == "".join(fragments).strip()


def test_final_answer_text_prefers_provider_metadata() -> None:
    split_tracker = FinalAnswerTracker()
    split_tracker.feed(AssistantTextDelta(type="text", text=INTERIM))
    split_tracker.feed(
        ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls")
    )
    split_tracker.feed(AssistantTextDelta(type="text", text=FINAL))

    assert _final_answer_text(WHOLE_TURN, FINAL, split_tracker) == FINAL
    assert _final_answer_text(WHOLE_TURN, "", split_tracker) == ""


def test_final_answer_text_uses_the_streamed_post_tool_segment() -> None:
    tracker = FinalAnswerTracker()
    tracker.feed(AssistantTextDelta(type="text", text=INTERIM))
    tracker.feed(ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls"))
    tracker.feed(AssistantTextDelta(type="text", text=FINAL))

    assert _final_answer_text(WHOLE_TURN, None, tracker) == FINAL
    # A Claude-shaped result already carries only the final block; the tracked
    # text is the same block, so the answer does not change.
    assert _final_answer_text(FINAL, None, tracker) == FINAL


def test_final_answer_text_falls_back_to_the_terminal_result() -> None:
    unsplit = FinalAnswerTracker()
    unsplit.feed(AssistantTextDelta(type="text", text="hello"))

    # No tool boundary: the streamed text is the whole reply, not a later block.
    assert _final_answer_text("hello", None, unsplit) == "hello"
    assert _final_answer_text("done", None, FinalAnswerTracker()) == "done"
    # A turn that streamed nothing but its terminal text still has an answer.
    assert _final_answer_text("", None, unsplit) == "hello"


# ── OpenCode: the provider names the part after the last tool call ─────────

SESSION = "ses_2139"


def _text_part(part_id: str, text: str) -> dict[str, object]:
    return {"type": "text", "id": part_id, "text": text}


def _tool_call_part(call_id: str = "call_1") -> dict[str, object]:
    return {
        "type": "tool",
        "callID": call_id,
        "tool": "shell",
        "state": {"status": "running", "input": {"command": "pwd"}},
    }


def _seed(provider: OpencodeProvider, *parts: dict[str, object]) -> None:
    for part in parts:
        provider._part_updated({"part": part})


def _opencode(tmp_path: Path) -> OpencodeProvider:
    from tests.test_opencode_provider import _provider as make

    return make(tmp_path)


def test_opencode_answer_final_text_follows_the_last_tool_call(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )

    assert provider._answer_text() == WHOLE_TURN
    assert provider._answer_final_text() == FINAL


def test_opencode_answer_final_text_is_empty_when_a_tool_ends_the_turn(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(provider, _text_part("p1", INTERIM), _tool_call_part())

    assert provider._answer_text() == INTERIM
    assert provider._answer_final_text() == ""


def test_opencode_answer_final_text_joins_the_post_tool_text_parts(
    tmp_path: Path,
) -> None:
    """Text separated only by thinking is one displayed answer block."""
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", "First **part**."),
        {"type": "reasoning", "id": "r1", "text": "weighing it"},
        _text_part("p3", "Second **part**."),
    )

    assert provider._answer_text() == (
        f"{INTERIM}\n\nFirst **part**.\n\nSecond **part**."
    )
    assert provider._answer_final_text() == "First **part**.\n\nSecond **part**."


def test_opencode_tool_result_does_not_end_the_answer(tmp_path: Path) -> None:
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )
    provider._event_to_stream(
        {
            "type": "session.tool.success",
            "data": {"sessionID": SESSION, "id": "call_1"},
        }
    )

    assert provider._answer_final_text() == FINAL


def test_opencode_replayed_tool_call_keeps_the_later_answer(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )
    # The same call replayed by a duplicate frame must not move the boundary.
    provider._event_to_stream(
        {
            "type": "session.tool.called",
            "data": {
                "sessionID": SESSION,
                "id": "call_1",
                "name": "shell",
                "input": {},
            },
        }
    )

    assert provider._answer_final_text() == FINAL


def _message(role: str, message_id: str, parts: list[dict]) -> dict[str, object]:
    return {"info": {"role": role, "id": message_id, "type": "message"}, "parts": parts}


def test_opencode_empty_recovery_projection_keeps_the_live_answer(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )

    replayed = provider._apply_recovered_parts(
        [_message("user", "user-1", []), _message("assistant", "m1", [])],
        authoritative=True,
    )

    assert replayed == []
    assert provider._answer_final_text() == FINAL


@pytest.mark.asyncio
async def test_opencode_missing_user_row_projection_keeps_the_live_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.providers import opencode as opencode_module

    provider = _opencode(tmp_path)
    provider._reset_turn_state()
    provider._user_message_id = "user-1"
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )
    # A projection with assistant rows but no row carrying this turn's prompt
    # id is not ours: replaying it would replace the live order with a guess.
    other = [_message("assistant", "m1", [_text_part("p9", "Other turn")])]

    async def foreign_messages(_client, _session_id):
        return other

    async def no_active(_client):
        return set()

    times = iter([0.0, 10**9])
    monkeypatch.setattr(opencode_module, "_read_v2_messages", foreign_messages)
    monkeypatch.setattr(opencode_module, "_read_active_sessions", no_active)
    monkeypatch.setattr(
        opencode_module, "time", SimpleNamespace(monotonic=lambda: next(times))
    )

    replayed = [
        event async for event in provider._reconcile_interrupted_turn(None, SESSION)
    ]

    assert replayed == []
    assert provider._answer_final_text() == FINAL


def test_opencode_valid_recovery_projection_replaces_the_live_order(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    # The live order ends on a tool frame, so it has no answer block yet.
    _seed(provider, _text_part("p2", FINAL), _tool_call_part())
    assert provider._answer_final_text() == ""

    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message(
                "assistant",
                "m1",
                [
                    _text_part("p1", INTERIM),
                    _tool_call_part(),
                    _text_part("p2", FINAL),
                ],
            ),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == FINAL


def test_opencode_live_and_projected_text_part_ids_agree() -> None:
    """The superset rule compares ids from both streams, so they must agree.

    A live text event is keyed by `_v2_part_key` (assistant message + ordinal),
    while a recovery poll's text part is id'd by `_projected_message`; both
    build the same `"{assistantMessageID}:text:{ordinal}"` string.
    """
    from ciao.providers.opencode import _projected_message, _v2_part_key

    projected = _projected_message(
        {
            "type": "assistant",
            "id": "msg_x",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ],
        }
    )

    assert projected is not None
    assert projected["parts"][1]["id"] == _v2_part_key(
        {"assistantMessageID": "msg_x", "ordinal": 1}, "text"
    )


def test_opencode_incomplete_recovery_projection_keeps_the_live_answer(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    # Text part ids are the ones both sides use: the live key `_v2_part_key`
    # and the projected part id from `_projected_message` are the same
    # `"{assistantMessageID}:text:{ordinal}"` string.
    _seed(
        provider,
        _text_part("msg_1:text:0", INTERIM),
        _tool_call_part(),
        _text_part("msg_2:text:0", FINAL),
    )

    # The poll has the user row and the narration part, but not the tool or
    # the final answer: it is incomplete for this turn and must not replace
    # the live order, or the narration would become the answer.
    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message("assistant", "m1", [_text_part("msg_1:text:0", INTERIM)]),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == FINAL

    # A later poll with every live text part is complete and is adopted.
    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message(
                "assistant",
                "m1",
                [
                    _text_part("msg_1:text:0", INTERIM),
                    _tool_call_part(),
                    _text_part("msg_2:text:0", FINAL),
                ],
            ),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == FINAL


def test_opencode_superset_recovery_projection_adds_the_missed_parts(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )

    # A strict superset carries parts missed during the SSE drop, so recovery
    # adopts it and the answer gains the text the live stream never saw.
    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message(
                "assistant",
                "m1",
                [
                    _text_part("p1", INTERIM),
                    _tool_call_part(),
                    _text_part("p2", FINAL),
                    {"type": "reasoning", "id": "r1", "text": "weighing it"},
                    _text_part("p3", "Added during the gap."),
                ],
            ),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == f"{FINAL}\n\nAdded during the gap."


def test_opencode_projection_adopted_when_live_saw_no_text_parts(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    # The live stream saw the tool frame but none of the text parts.
    _seed(provider, _tool_call_part())

    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message(
                "assistant",
                "m1",
                [
                    _text_part("p1", INTERIM),
                    _tool_call_part(),
                    _text_part("p2", FINAL),
                ],
            ),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == FINAL


@pytest.mark.asyncio
async def test_opencode_incomplete_recovery_timeout_keeps_the_live_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.providers import opencode as opencode_module

    provider = _opencode(tmp_path)
    provider._reset_turn_state()
    provider._user_message_id = "user-1"
    _seed(
        provider,
        _text_part("msg_1:text:0", INTERIM),
        _tool_call_part(),
        _text_part("msg_2:text:0", FINAL),
    )
    incomplete = [
        _message("user", "user-1", []),
        _message("assistant", "m1", [_text_part("msg_1:text:0", INTERIM)]),
    ]

    async def incomplete_messages(_client, _session_id):
        return incomplete

    async def still_running(_client):
        return {SESSION}

    times = iter([0.0, 10**9])
    monkeypatch.setattr(opencode_module, "_read_v2_messages", incomplete_messages)
    monkeypatch.setattr(opencode_module, "_read_active_sessions", still_running)
    monkeypatch.setattr(
        opencode_module, "time", SimpleNamespace(monotonic=lambda: next(times))
    )

    replayed = [
        event async for event in provider._reconcile_interrupted_turn(None, SESSION)
    ]

    assert replayed == []
    assert provider._poll_error
    assert provider._answer_final_text() == FINAL


def test_opencode_recovery_replay_joins_the_post_tool_parts(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    provider._user_message_id = "user-1"
    _seed(provider, _text_part("p2", "First part."))

    provider._apply_recovered_parts(
        [
            _message("user", "user-1", []),
            _message(
                "assistant",
                "m1",
                [
                    _text_part("p1", INTERIM),
                    _tool_call_part(),
                    _text_part("p2", "First part."),
                    {"type": "reasoning", "id": "r1", "text": "weighing it"},
                    _text_part("p3", "Second part."),
                ],
            ),
        ],
        authoritative=True,
    )

    assert provider._answer_final_text() == "First part.\n\nSecond part."


@pytest.mark.asyncio
async def test_opencode_recovery_timeout_after_an_empty_poll_keeps_the_live_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.providers import opencode as opencode_module

    provider = _opencode(tmp_path)
    provider._reset_turn_state()
    provider._user_message_id = "user-1"
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )

    async def empty_messages(_client, _session_id):
        return []

    async def no_active(_client):
        return set()

    times = iter([0.0, 10**9])
    monkeypatch.setattr(opencode_module, "_read_v2_messages", empty_messages)
    monkeypatch.setattr(opencode_module, "_read_active_sessions", no_active)
    # Replace the module's clock, not the shared `time` module: asyncio uses
    # the latter for its own scheduling.
    monkeypatch.setattr(
        opencode_module, "time", SimpleNamespace(monotonic=lambda: next(times))
    )

    replayed = [
        event async for event in provider._reconcile_interrupted_turn(None, SESSION)
    ]

    assert replayed == []
    assert provider._poll_error
    assert provider._answer_final_text() == FINAL


def test_opencode_wake_result_carries_the_part_after_the_tool(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", FINAL),
    )

    event = provider._wake_result(SESSION, "")

    assert event.result == WHOLE_TURN
    assert event.final_text == FINAL
    assert event.origin == "task-notification"


def test_opencode_wake_result_joins_the_post_tool_parts(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(
        provider,
        _text_part("p1", INTERIM),
        _tool_call_part(),
        _text_part("p2", "First part."),
        {"type": "reasoning", "id": "r1", "text": "weighing it"},
        _text_part("p3", "Second part."),
    )

    event = provider._wake_result(SESSION, "")

    assert event.result == f"{INTERIM}\n\nFirst part.\n\nSecond part."
    assert event.final_text == "First part.\n\nSecond part."


def test_opencode_wake_result_is_explicitly_empty_after_a_trailing_tool(
    tmp_path: Path,
) -> None:
    provider = _opencode(tmp_path)
    _seed(provider, _text_part("p1", INTERIM), _tool_call_part())

    event = provider._wake_result(SESSION, "")

    assert event.result == INTERIM
    assert event.final_text == ""


@pytest.mark.asyncio
async def test_normal_terminal_result_carries_final_text_from_the_v2_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_opencode_provider import _run_fixture_turn

    events = await _run_fixture_turn(
        _opencode(tmp_path),
        monkeypatch,
        "turn_with_tool.jsonl",
        "ses_003133027ffeJooFKUT3slZ0al",
    )

    result = events[-1]
    assert result.type == "result"
    assert result.result == "DONE"
    # The fixture's only text part is the final one, so both agree.
    assert result.final_text == "DONE"


def _sse(kind: str, **data: object) -> str:
    return json.dumps(
        {"id": f"e_{kind}", "type": kind, "data": {"sessionID": SESSION, **data}}
    )


@pytest.mark.asyncio
async def _run_opencode_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: list[str],
) -> tuple[OpencodeProvider, list]:
    from ciao.models import AgentRequest
    from tests.test_opencode_provider import _FakeServerClient

    provider = _opencode(tmp_path)
    client = _FakeServerClient([f"data: {line}\n\n" for line in lines])

    async def fake_server(_request):
        return client

    async def fake_session(_request):
        return SESSION

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)
    request = AgentRequest(prompt="hi", model="", mode="bypass", provider="opencode")
    events = [
        event async for event in provider.run_streaming(request, lambda _h: None)
    ]
    return provider, events


@pytest.mark.asyncio
async def test_opencode_narration_then_tool_is_not_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real provider -> drive path: a trailing tool means no answer block."""
    from tests.test_chat_streaming_boundary import _DriveHost, _drive_with

    from ciao.web.chat_broker import ChatStream

    lines = [
        _sse("session.execution.started"),
        _sse("session.text.started", assistantMessageID="msg_a", ordinal=0),
        _sse(
            "session.text.delta",
            assistantMessageID="msg_a",
            ordinal=0,
            delta="Narration.",
        ),
        _sse(
            "session.text.ended",
            assistantMessageID="msg_a",
            ordinal=0,
            text="Narration.",
        ),
        _sse(
            "session.tool.input.started",
            assistantMessageID="msg_t",
            id="call_1",
            name="shell",
        ),
        _sse(
            "session.tool.called",
            assistantMessageID="msg_t",
            id="call_1",
            input={"command": "pwd"},
        ),
        _sse(
            "session.tool.success",
            assistantMessageID="msg_t",
            id="call_1",
            content=[{"type": "text", "text": "/w"}],
        ),
        _sse("session.execution.succeeded"),
    ]
    _, events = await _run_opencode_events(tmp_path, monkeypatch, lines)

    result = events[-1]
    assert result.result == "Narration."
    assert result.final_text == ""

    host = _DriveHost(events, tmp_path)
    host.chat.last_snippet = "Earlier drive answer."
    await _drive_with(host, ChatStream(prompt_text="hi"))

    assert host.announcements == []
    assert host.chat.last_snippet == "Earlier drive answer."
    assert host.chat.last_response == "Narration."


@pytest.mark.asyncio
async def test_opencode_two_answer_parts_announce_the_joined_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider names every post-tool text part; the preview joins them."""
    from tests.test_chat_streaming_boundary import _DriveHost, _drive_with

    from ciao.web.chat_broker import ChatStream

    lines = [
        _sse("session.execution.started"),
        _sse("session.text.started", assistantMessageID="msg_1", ordinal=0),
        _sse(
            "session.text.delta",
            assistantMessageID="msg_1",
            ordinal=0,
            delta="Narration.",
        ),
        _sse(
            "session.text.ended",
            assistantMessageID="msg_1",
            ordinal=0,
            text="Narration.",
        ),
        _sse(
            "session.tool.input.started",
            assistantMessageID="msg_t",
            id="call_1",
            name="shell",
        ),
        _sse(
            "session.tool.called",
            assistantMessageID="msg_t",
            id="call_1",
            input={"command": "pwd"},
        ),
        _sse("session.text.started", assistantMessageID="msg_2", ordinal=0),
        _sse(
            "session.text.delta",
            assistantMessageID="msg_2",
            ordinal=0,
            delta="First part.",
        ),
        _sse(
            "session.text.ended",
            assistantMessageID="msg_2",
            ordinal=0,
            text="First part.",
        ),
        _sse("session.reasoning.started", assistantMessageID="msg_3", ordinal=0),
        _sse(
            "session.reasoning.delta",
            assistantMessageID="msg_3",
            ordinal=0,
            delta="weighing it",
        ),
        _sse(
            "session.reasoning.ended",
            assistantMessageID="msg_3",
            ordinal=0,
            text="weighing it",
        ),
        _sse("session.text.started", assistantMessageID="msg_4", ordinal=0),
        _sse(
            "session.text.delta",
            assistantMessageID="msg_4",
            ordinal=0,
            delta="Second part.",
        ),
        _sse(
            "session.text.ended",
            assistantMessageID="msg_4",
            ordinal=0,
            text="Second part.",
        ),
        _sse("session.execution.succeeded"),
    ]
    _, events = await _run_opencode_events(tmp_path, monkeypatch, lines)

    result = events[-1]
    assert result.result == "Narration.\n\nFirst part.\n\nSecond part."
    assert result.final_text == "First part.\n\nSecond part."

    host = _DriveHost(events, tmp_path)
    await _drive_with(host, ChatStream(prompt_text="hi"))

    assert host.announcements == [
        ("chat-1", "project-1", "Contract", "First part. Second part.")
    ]
    assert host.chat.last_snippet == "First part. Second part."
    assert host.chat.last_response == "Narration.\n\nFirst part.\n\nSecond part."
