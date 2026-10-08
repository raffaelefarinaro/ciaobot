"""Tests for ``ciao.task_resolution`` (#1152).

Pure string handling, so no store or clock is involved: completions are
appended, parsed and stripped as text, and a duplicate id is refused without
touching the input.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ciao.task_attempts import build_prompt
from ciao.task_log import LOG_CLOSE, LOG_OPEN, render_item, upsert_item
from ciao.task_resolution import (
    CLOSE,
    HEADING,
    OPEN,
    Completion,
    append_completion,
    parse_completions,
    replace_resolution,
    strip_completions,
)


def _prompt(body: str) -> str:
    return build_prompt(
        title="T",
        status="backlog",
        due="",
        project_id="",
        task_id="8" * 32,
        task_revision="rev",
        relative_path="Workspace/Tasks/x.md",
        body=body,
    )


def _log_section() -> str:
    body = upsert_item(
        "Description.",
        "b" * 32,
        render_item(
            attempt_id="b" * 32,
            state="running",
            outcome="",
            summary="Did delegated work.",
            detail="",
            created_at="2026-10-08T10:00:00+00:00",
            ended_at="2026-10-08T10:01:00+00:00",
            chat_id="c",
            chat_title="t",
            archive_path="",
        ),
    )
    return body[body.index(LOG_OPEN) :]


def test_append_parse_and_strip_round_trip() -> None:
    description = "Do the thing."
    first = Completion(
        id="0" * 32,
        completed_at=datetime(2026, 10, 7, 8, 0, 0, tzinfo=UTC),
        resolution="First pass",
    )
    body = append_completion(description, first)
    # A hand edit inside the engine section: a stray note before the first
    # item, not an item itself.
    body = body.replace(HEADING, HEADING + "\n\nA user note inside the section.")
    second = Completion(
        id="1" * 32,
        completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
        resolution="Fixed the retry\nacross two modules\n\nwith a follow-up note.",
        attempt_id="2" * 32,
    )
    body = append_completion(body, second)

    found = parse_completions(body)
    assert [item.id for item in found] == ["1" * 32, "0" * 32]
    assert found[0].completed_at == datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
    assert found[0].resolution == (
        "Fixed the retry\nacross two modules\n\nwith a follow-up note."
    )
    assert found[0].attempt_id == "2" * 32
    assert found[0].edited_at is None
    assert found[1].resolution == "First pass"
    assert found[1].attempt_id == ""
    # The hand-edited line survives the rewrite without becoming an item.
    assert "A user note inside the section." in body
    assert len(parse_completions(body)) == 2
    # Stripping leaves the description only.
    assert strip_completions(body) == description + "\n"
    assert OPEN in body and CLOSE in body


def test_replace_resolution_does_not_change_completed_at() -> None:
    done_at = datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC)
    completion = Completion(
        id="3" * 32, completed_at=done_at, resolution="Original"
    )
    body = append_completion("Desc.", completion)

    edited = replace_resolution(
        body,
        "3" * 32,
        "Reworded\nwith detail.",
        datetime(2026, 10, 9, 9, 30, 0, tzinfo=UTC),
    )
    found = parse_completions(edited)
    assert len(found) == 1
    assert found[0].id == "3" * 32
    assert found[0].completed_at == done_at
    assert found[0].resolution == "Reworded\nwith detail."
    assert found[0].edited_at == datetime(2026, 10, 9, 9, 30, 0, tzinfo=UTC)


def test_duplicate_completion_id_is_refused() -> None:
    completion = Completion(
        id="4" * 32,
        completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
        resolution="Once",
    )
    body = append_completion("Desc.", completion)
    with pytest.raises(ValueError):
        append_completion(body, completion)
    assert [item.id for item in parse_completions(body)] == ["4" * 32]


def test_resolution_with_section_markers_round_trips_and_strips_cleanly() -> None:
    resolution = (
        "Fixed the leak\n"
        f"A literal {OPEN} is just prose here\n"
        f"and so is {CLOSE}\n"
        "PAST RESOLUTION TEXT stays history."
    )
    completion = Completion(
        id="6" * 32,
        completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
        resolution=resolution,
        attempt_id="7" * 32,
    )
    body = append_completion("Do the work.", completion)
    # Exactly one section: the markers inside the resolution are escaped.
    assert body.count(OPEN) == 1
    assert body.count(CLOSE) == 1
    found = parse_completions(body)
    assert len(found) == 1
    assert found[0].resolution == resolution

    stripped = strip_completions(body)
    assert stripped.strip() == "Do the work."
    assert "PAST RESOLUTION TEXT" not in stripped

    prompt = build_prompt(
        title="T",
        status="backlog",
        due="",
        project_id="",
        task_id="8" * 32,
        task_revision="rev",
        relative_path="Workspace/Tasks/x.md",
        body=body,
    )
    assert "PAST RESOLUTION TEXT" not in prompt
    assert "Do the work." in prompt

    # Including after a resolution edit: markers stay unambiguous.
    edited = replace_resolution(
        body,
        "6" * 32,
        f"Edited {CLOSE} tail",
        datetime(2026, 10, 9, 9, 30, 0, tzinfo=UTC),
    )
    edited_found = parse_completions(edited)
    assert len(edited_found) == 1
    assert edited_found[0].resolution == f"Edited {CLOSE} tail"
    assert edited_found[0].completed_at == datetime(
        2026, 10, 8, 8, 0, 0, tzinfo=UTC
    )
    assert "tail" not in strip_completions(edited)


def test_completion_before_delegation_log_does_not_leak_into_prompt() -> None:
    resolution = (
        "SECRET PAST RESOLUTION\n"
        f"Mention {LOG_OPEN} literally\n"
        f"and {LOG_CLOSE} too."
    )
    completion = Completion(
        id="a" * 32,
        completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
        resolution=resolution,
    )
    section = append_completion("Description.", completion)
    section = section[len("Description.") :].lstrip()

    # Completion history first, delegation log second.
    first = append_completion("Description.", completion).rstrip()
    first = f"{first}\n\n{_log_section().strip()}\n"
    # Delegation log first, completion history second.
    log_first = _log_section()
    second = f"Description.\n\n{log_first.strip()}\n\n{section.strip()}\n"

    for body in (first, second):
        found = parse_completions(body)
        assert len(found) == 1
        assert found[0].resolution == resolution
        assert "SECRET PAST RESOLUTION" not in strip_completions(body)
        prompt = _prompt(body)
        assert "SECRET PAST RESOLUTION" not in prompt
        assert "Description." in prompt


def test_literal_completion_item_marker_round_trips_and_edits() -> None:
    literal = "<!-- completion:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa -->"
    completion = Completion(
        id="c" * 32,
        completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
        resolution=f"Fixed {literal} here",
    )
    body = append_completion("Desc.", completion)
    found = parse_completions(body)
    assert len(found) == 1
    assert found[0].id == "c" * 32
    assert found[0].resolution == f"Fixed {literal} here"

    edited = replace_resolution(
        body,
        "c" * 32,
        f"Edited {literal} again",
        datetime(2026, 10, 9, 9, 30, 0, tzinfo=UTC),
    )
    refound = parse_completions(edited)
    assert len(refound) == 1
    assert refound[0].id == "c" * 32
    assert refound[0].resolution == f"Edited {literal} again"
    assert refound[0].completed_at == datetime(
        2026, 10, 8, 8, 0, 0, tzinfo=UTC
    )
    assert "again" not in strip_completions(edited)


def test_marker_escape_is_injective_for_raw_escaped_and_doubled() -> None:
    cases = [
        OPEN,
        CLOSE,
        f"note {OPEN} inline",
        "tail " + CLOSE,
        "&lt;!-- ciao:task-completions --&gt;",
        "&lt;!-- /ciao:task-completions --&gt;",
        "&amp;lt;!-- ciao:task-completions --&gt;",
        "&amp;lt;!-- /ciao:task-completions --&gt;",
        "&amp;amp;lt;!-- ciao:task-completions --&gt;",
        f"mix {OPEN} then &lt;!-- ciao:task-completions --&gt; "
        "then &amp;lt;!-- ciao:task-completions --&gt; "
        f"then {CLOSE} end",
        "&amp;copy; stays unrelated",
    ]
    for index, resolution in enumerate(cases):
        completion = Completion(
            id=f"{0xB0 + index:032x}",
            completed_at=datetime(2026, 10, 8, 8, 0, 0, tzinfo=UTC),
            resolution=resolution,
        )
        body = append_completion("D.", completion)
        assert body.count(OPEN) == 1
        assert body.count(CLOSE) == 1
        found = parse_completions(body)
        assert len(found) == 1
        assert found[0].resolution == resolution

        rewritten = replace_resolution(
            body,
            completion.id,
            resolution + " v2",
            datetime(2026, 10, 9, 9, 30, 0, tzinfo=UTC),
        )
        refound = parse_completions(rewritten)
        assert len(refound) == 1
        assert refound[0].resolution == resolution + " v2"
        assert refound[0].completed_at == completion.completed_at
