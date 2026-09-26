"""The kept half of ``ciao.insights``: the transcript filter, the one retry
policy, and the locator for an insights section an older build appended.

The one-shot extraction prompts and their helpers were deleted in #627 along
with the pipeline that called them. What is left is still load-bearing:
:func:`filter_session_jsonl` feeds the trajectory, :func:`call_with_retry` is
the retry policy every one-shot in the app shares, and
:func:`locate_insights_section` authenticates a crashed append from before that
change so its manifest still resumes.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao import insights


# ── filter_session_jsonl ────────────────────────────────────────────────


def _project_dir(workspace_root: Path) -> Path:
    """Mirror the directory layout that `_claude_projects_dir` resolves to."""
    slug = str(workspace_root).replace("/", "-").lstrip("-")
    return Path.home() / ".claude" / "projects" / f"-{slug}"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_filter_returns_none_when_jsonl_missing(tmp_path: Path) -> None:
    out = insights.filter_session_jsonl(tmp_path, "missing-session")
    assert out is None


def test_filter_returns_none_for_empty_session_id(tmp_path: Path) -> None:
    assert insights.filter_session_jsonl(tmp_path, "") is None


def test_filter_keeps_user_assistant_text_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-abc"
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi back"},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 2
    assert lines[0]["idx"] == 1
    assert lines[0]["type"] == "user"
    assert lines[1]["idx"] == 2
    assert lines[1]["type"] == "assistant"


def test_filter_truncates_read_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-trunc"
    long_body = "x" * 5000
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "tu_1",
             "input": {"file_path": "/big.txt"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "is_error": False, "content": long_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert "[truncated, total=5000 chars]" in result_block["content"]
    assert len(result_block["content"]) < 1000


def test_filter_keeps_edit_and_bash_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-edit"
    edit_body = "long edit output: " + ("y" * 1000)
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "id": "tu_e",
             "input": {"file_path": "/foo.py", "old_string": "a", "new_string": "b"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_e",
             "is_error": False, "content": edit_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["content"] == edit_body
    # Tool input also kept in full for Edit
    use_block = lines[0]["content"][0]
    assert use_block["input"]["new_string"] == "b"


def test_filter_keeps_errors_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-err"
    err_body = "error: " + ("z" * 5000)
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "tu_r",
             "input": {"file_path": "/missing.txt"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_r",
             "is_error": True, "content": err_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["is_error"] is True
    assert result_block["content"] == err_body


def test_filter_drops_sidechain_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-sc"
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "isSidechain": True, "message": {"content": "subagent"}},
        {"type": "user", "message": {"content": "main"}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 1
    assert lines[0]["content"][0]["text"] == "main"


def test_filter_flags_unattended_user_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An automation turn carries the capsule marker and is flagged unattended.

    The marker is what lets extraction tell a real user turn from the machinery
    that fired it, so a system-schedule chat can still surface a genuine user
    statement without lifting the schedule's own rules as facts.
    """
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-unatt"
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    from ciao.memory_policy import UNATTENDED_CAPSULE_GUIDANCE

    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": (
            "[CIAO_CONTEXT_BEGIN]\n<ciao-context>\n"
            f"{UNATTENDED_CAPSULE_GUIDANCE}\n</ciao-context>\n"
            "[CIAO_CONTEXT_END]\n\nRun the nightly curation pass."
        )}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Curation done."},
        ]}},
        {"type": "user", "message": {"content": "Actually, remember: never deploy on Fridays."}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[0]["unattended"] is True
    assert "unattended" not in lines[1]
    assert "unattended" not in lines[2]


# ── locate_insights_section ───────────────────────────────────────────


def test_locate_returns_none_without_marker() -> None:
    assert insights.locate_insights_section("# chat\n\n## Turn 1\n\nhi\n") is None


def test_locate_finds_legacy_appended_section() -> None:
    text = "# chat\n\n## Turn 1\n\nhi\n\n## Session insights\n\n## Errors\n- x\n"
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_rejects_marker_quoted_mid_transcript() -> None:
    """A turn heading after the marker proves the marker is quoted content.

    Curation chats quote insights sections verbatim; the old substring check
    treated those archives as already processed and skipped extraction.
    """
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n"
        "## Decisions\n- old bullet\n\n## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_prefers_last_marker_over_quoted_one() -> None:
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Turn 2\n\nbye\n\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert "real" in text[location[1]:]
    assert "old" not in text[location[1]:]


def test_locate_trusts_the_stamp() -> None:
    """A stamped section at end of file is authoritative."""
    text = (
        "# chat\n\n## Turn 1\n\nhi\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[0]:].startswith("<!-- ciao:session-insights -->")


def test_has_insights_section_ignores_quoted_marker(tmp_path: Path) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text(
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Turn 2\n\nbye\n",
        encoding="utf-8",
    )
    assert not insights._has_insights_section(archive)


def test_locate_rejects_stamp_quoted_inside_code_fence() -> None:
    """A stamped section pasted into a chat turn is fenced, hence quoted.

    Rendered archives fence quoted transcript text; without the fence check
    the stamp fast path would trust the quoted copy and skip extraction.
    """
    text = (
        "# chat\n\n## Turn 1\n\nlook at this archive:\n\n"
        "```text\n<!-- ciao:session-insights -->\n## Session insights\n\n"
        "## Decisions\n- old reviewed bullet\n```\n\n"
        "## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_ignores_stamp_mentioned_in_prose() -> None:
    """A stamp in prose, not adjacent to a header, never binds to one."""
    text = (
        "# chat\n\n## Turn 1\n\nthe stamp is <!-- ciao:session-insights --> ok\n\n"
        "## Turn 2\n\nbye\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert "## Errors" in text[location[1]:]
    # The real appended stamp wins, not the prose mention.
    assert text[location[0]:].startswith("<!-- ciao:session-insights -->\n## Session")


def test_locate_rejects_marker_quoted_in_last_turn() -> None:
    """A quote in the final turn is followed by trailers, not another turn."""
    text = (
        "# chat\n\n## Turn 1\n\nhi\n\n## Turn 2\n\nquoting:\n\n"
        "## Session insights\n\n## Decisions\n- old bullet\n\n"
        "### Usage\n- tokens\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_requires_stamped_marker_to_be_line_anchored() -> None:
    """A prose stamp cannot bind to a following legacy-looking heading."""
    text = (
        "# chat\n\n## Turn 1\n\nquoted stamp: "
        "<!-- ciao:session-insights -->\n## Session insights\n\n"
        "## Decisions\n- quoted\n\n## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_accepts_stamped_crlf_archive() -> None:
    """Archives written on Windows retain the same marker semantics."""
    text = (
        "# chat\r\n\r\n## Turn 1\r\n\r\nhi\r\n\r\n"
        "<!-- ciao:session-insights -->\r\n## Session insights  \r\n\r\n"
        "## Errors\r\n- real\r\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_survives_unbalanced_fence_in_transcript() -> None:
    """A stray line-start ``` inside a quoted turn must not hide the section.

    Rendered archives embed turn text verbatim inside ```text fences, so a
    message containing an odd number of line-start fences (a truncated code
    block, a chat about Markdown) is routine. Prefix fence *parity* would
    flip there and make the real appended stamp read as quoted content —
    re-running extraction and appending a duplicate section on every pass.
    """
    text = (
        "# chat\n\n## Turn 1\n\n```text\nlook:\n```python\nprint('unclosed')\n```\n\n"
        "## Turn 2\n\nbye\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_rejects_marker_quoted_before_subagents_block() -> None:
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Subagents\n\n#### Turn 1\n\nsub\n"
    )
    assert insights.locate_insights_section(text) is None


# ── call_with_retry: the one retry policy ─────────────────────────────


def test_insights_timeout_is_generous() -> None:
    # The old flat 120s was below the 214-253s this path really takes.
    assert insights._DEFAULT_TIMEOUT_S > 200


def test_context_overflow_is_distinguished_from_a_transient_timeout() -> None:
    overflow = Exception(
        "API Error 400 Message too long: 262183 > 125952 maximum context length"
    )
    assert insights.is_context_overflow(overflow)
    assert insights.is_context_overflow(Exception("context_length_exceeded"))
    # Transient failures must stay retryable.
    assert not insights.is_context_overflow(asyncio.TimeoutError())
    assert not insights.is_context_overflow(Exception("429 rate limit"))


def test_terminal_failure_flag_only_trips_on_explicit_false() -> None:
    from ciao.providers.oneshot import OneShotError

    assert insights.is_terminal_failure(OneShotError("bad key", transient=False))
    # Retryable, and anything without the flag stays retryable (safe default).
    assert not insights.is_terminal_failure(OneShotError("empty body", transient=True))
    assert not insights.is_terminal_failure(asyncio.TimeoutError())
    assert not insights.is_terminal_failure(Exception("subprocess died"))


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the retry wait without patching `asyncio.sleep` process-wide.

    `insights.asyncio` *is* the asyncio module, so setattr'ing `sleep` on it
    would make every `asyncio.sleep` in the process a no-op for the duration
    of the test. Zeroing the delay constant is the narrow equivalent.
    """
    monkeypatch.setattr(insights, "_RETRY_DELAY_S", 0)


class _Terminal(Exception):
    """A provider rejection the provider already classified as non-transient."""

    transient = False


def test_call_with_retry_succeeds_first_time() -> None:
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        return "output"

    outcome = asyncio.run(insights.call_with_retry(call, label="test call"))
    assert (outcome.output, outcome.error, outcome.attempts) == ("output", "", 1)
    assert outcome.gave_up == ""
    assert calls == 1


def test_call_with_retry_retries_a_transient_failure_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError()
        return "second try"

    outcome = asyncio.run(insights.call_with_retry(call, label="test call"))
    assert outcome.output == "second try"
    assert outcome.attempts == 2
    assert outcome.gave_up == ""
    assert calls == 2


def test_call_with_retry_gives_up_after_two_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    outcome = asyncio.run(insights.call_with_retry(call, label="test call"))
    assert outcome.gave_up == "failed-twice"
    assert outcome.attempts == 2
    assert outcome.error == "boom"
    assert calls == 2


def test_call_with_retry_never_retries_a_terminal_rejection() -> None:
    """Quota/auth/bad-model fail identically on a second call.

    Re-sending costs another rejected request plus the 30s wait, once per
    archive across a whole backfill run.
    """
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        raise _Terminal("over quota")

    outcome = asyncio.run(insights.call_with_retry(call, label="test call"))
    assert outcome.gave_up == "terminal"
    assert outcome.attempts == 1
    assert outcome.error == "over quota"
    assert calls == 1


def test_call_with_retry_never_retries_a_context_overflow() -> None:
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("prompt is too long for this model")

    outcome = asyncio.run(insights.call_with_retry(call, label="test call"))
    assert outcome.gave_up == "context-overflow"
    assert calls == 1


def test_call_with_retry_can_be_told_to_retry_an_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag still exists for a caller that wants the old behaviour.

    No production path sets it today — text mode used to, by accident of the
    policy existing in three copies, and now refuses an overflow like the JSONL
    path does.
    """
    _no_sleep(monkeypatch)
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("prompt is too long for this model")

    outcome = asyncio.run(
        insights.call_with_retry(call, label="test call", check_context_overflow=False)
    )
    assert outcome.gave_up == "failed-twice"
    assert calls == 2


def test_retry_outcome_distinguishes_never_asked_from_answered_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the outcome is typed at all.

    Both cases used to surface as an empty string plus a log line, so a caller
    could not tell "the account is over quota, the model was never asked" from
    "the model answered nothing twice".
    """
    _no_sleep(monkeypatch)

    async def refused() -> str:
        raise _Terminal("over quota")

    async def empty() -> str:
        return ""

    never_asked = asyncio.run(insights.call_with_retry(refused, label="test call"))
    answered_nothing = asyncio.run(insights.call_with_retry(empty, label="test call"))

    assert never_asked.output == answered_nothing.output == ""
    assert never_asked.gave_up == "terminal"
    assert answered_nothing.gave_up == ""
