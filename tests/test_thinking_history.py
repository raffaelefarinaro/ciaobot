"""Reasoning ('thinking') blocks in the /messages history rebuild.

Regression coverage for the sync bug where reasoning either vanished on
reload (Anthropic extended-thinking blocks were silently dropped) or was
promoted into the final answer bubble. `_extract_assistant_blocks` must
surface thinking as its own kind so the renderer tags it `_thinking`,
matching the live stream.
"""

from __future__ import annotations

from ciao.web.routes_api import _extract_assistant_blocks


def test_thinking_block_is_classified_as_thinking() -> None:
    message = {
        "content": [
            {"type": "thinking", "thinking": "Let me reason about this."},
            {"type": "text", "text": "Here is the answer."},
        ]
    }
    blocks = _extract_assistant_blocks(message)
    assert blocks == [
        {"kind": "thinking", "text": "Let me reason about this."},
        {"kind": "text", "text": "Here is the answer."},
    ]


def test_thinking_preserves_order_relative_to_tools_and_text() -> None:
    message = {
        "content": [
            {"type": "thinking", "thinking": "First I think."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
            {"type": "text", "text": "Final."},
        ]
    }
    kinds = [b["kind"] for b in _extract_assistant_blocks(message)]
    assert kinds == ["thinking", "tool_use", "text"]


def test_redacted_thinking_without_text_is_dropped() -> None:
    message = {
        "content": [
            {"type": "redacted_thinking", "data": "encrypted-blob"},
            {"type": "text", "text": "Answer."},
        ]
    }
    blocks = _extract_assistant_blocks(message)
    assert blocks == [{"kind": "text", "text": "Answer."}]


def test_empty_thinking_is_dropped() -> None:
    message = {
        "content": [
            {"type": "thinking", "thinking": "   "},
            {"type": "text", "text": "Answer."},
        ]
    }
    blocks = _extract_assistant_blocks(message)
    assert blocks == [{"kind": "text", "text": "Answer."}]
