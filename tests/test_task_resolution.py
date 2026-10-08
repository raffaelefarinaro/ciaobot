"""Tests for ``ciao.task_resolution`` (#1152).

Pure string handling, so no store or clock is involved: completions are
appended, parsed and stripped as text, and a duplicate id is refused without
touching the input.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
