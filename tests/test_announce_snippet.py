"""The final-answer excerpt behind unread previews and result pushes (#2139).

The Home unread card and the push body must show the same text the chat shows
as the turn's answer: the assistant text after the turn's last top-level tool
call, rendered as plain text before it is truncated. The durable transcript,
the provider handover and the schedule classifier keep the complete turn text,
so these tests pin both halves of that split.
"""

from __future__ import annotations

import time

import pytest

from ciao.models import AssistantTextDelta, ResultEvent, ThinkingEvent, ToolUseEvent
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

