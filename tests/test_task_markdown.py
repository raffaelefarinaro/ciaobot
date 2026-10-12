"""Shared mechanics for the task Markdown sections."""

from __future__ import annotations

import re

from ciao._task_markdown import split_marked_items


def test_split_marked_items_keeps_stray_text_and_multiline_items() -> None:
    authoritative_id = "a" * 32
    entries = split_marked_items(
        "## Section\nA hand-written note\n"
        "- First item <!-- completion:"
        + "b" * 32
        + " --> <!-- completion:"
        + authoritative_id
        + " --> <!-- edited:2026-10-09T12:00:00+00:00 -->\n  continuation\n"
        "- Second item without a readable mark",
        re.compile(
            r"<!-- completion:([0-9a-f]{32}) -->"
            r"\s*(?:<!-- edited:([^<>]+?) -->)?\s*$"
        ),
    )

    assert entries == [
        ("", "## Section\nA hand-written note"),
        (
            authoritative_id,
            "- First item <!-- completion:"
            + "b" * 32
            + " --> <!-- completion:"
            + authoritative_id
            + " --> <!-- edited:2026-10-09T12:00:00+00:00 -->\n  continuation",
        ),
        ("", "- Second item without a readable mark"),
    ]
