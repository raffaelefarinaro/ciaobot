"""Chat broker serialization + replay behaviour."""

from __future__ import annotations

import asyncio

import pytest

from ciao.models import (
    AssistantTextDelta,
    PermissionRequestEvent,
    ResultEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.web.chat_broker import (
    ChatStream,
    ChatStreamBroker,
    event_to_json,
    extract_file_touch,
)


def test_event_to_json_text_delta() -> None:
    assert event_to_json(AssistantTextDelta(type="assistant", text="hi")) == {
        "type": "text_delta",
        "text": "hi",
    }


def test_event_to_json_tool_use_omits_empty_input() -> None:
    assert event_to_json(ToolUseEvent(type="assistant", tool_name="Read")) == {
        "type": "tool_use",
        "tool_name": "Read",
    }


def test_event_to_json_tool_use_includes_input_when_present() -> None:
    payload = event_to_json(
        ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls")
    )
    assert payload == {"type": "tool_use", "tool_name": "Bash", "tool_input": "ls"}


def test_event_to_json_tool_use_tags_file_writes() -> None:
    """Write/Edit/MultiEdit/NotebookEdit tool calls carry a `file_touch`
    payload so the PWA can render an inline preview card. Non-mutating tools
    (Read/Grep/Bash) and unrelated tools must not get tagged."""
    payload = event_to_json(
        ToolUseEvent(
            type="assistant",
            tool_name="Write",
            tool_input="memory-vault/personal/Ideas/foo.md",
        )
    )
    assert payload["file_touch"] == {
        "file_path": "memory-vault/personal/Ideas/foo.md",
        "action": "written",
    }

    edit_payload = event_to_json(
        ToolUseEvent(type="assistant", tool_name="Edit", tool_input="/tmp/x.py")
    )
    assert edit_payload["file_touch"] == {
        "file_path": "/tmp/x.py",
        "action": "edited",
    }

    read_payload = event_to_json(
        ToolUseEvent(type="assistant", tool_name="Read", tool_input="/tmp/x.py")
    )
    assert "file_touch" not in read_payload


def test_extract_file_touch_dict_input() -> None:
    """Reload-side: ``_extract_assistant_blocks`` passes the raw SDK input dict.
    Picks ``file_path`` for Claude, ``path`` for Pi, and ``notebook_path``
    for NotebookEdit."""
    assert extract_file_touch("Write", {"file_path": "a.md", "content": "..."}) == {
        "file_path": "a.md",
        "action": "written",
    }
    assert extract_file_touch("MultiEdit", {"file_path": "b.py", "edits": []}) == {
        "file_path": "b.py",
        "action": "edited",
    }
    assert extract_file_touch("edit", {"path": "web/src/App.vue", "edits": []}) == {
        "file_path": "web/src/App.vue",
        "action": "edited",
    }
    assert extract_file_touch(
        "NotebookEdit", {"notebook_path": "n.ipynb", "cell_id": "c1"}
    ) == {"file_path": "n.ipynb", "action": "edited"}
    assert extract_file_touch("Bash", {"command": "ls"}) is None
    assert extract_file_touch("Write", {}) is None
    assert extract_file_touch("Write", None) is None


def test_event_to_json_permission_request_includes_request_id() -> None:
    """Client needs ``request_id`` to reply with the matching
    ``permission_response`` — otherwise it can't correlate approvals to
    prompts and late answers could resolve a stale request."""
    payload = event_to_json(
        PermissionRequestEvent(
            type="system",
            message="Allow Bash?",
            tool_name="Bash",
            tool_input="rm -rf /tmp/x",
            request_id="req-123",
        )
    )
    assert payload == {
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "rm -rf /tmp/x",
        "message": "Allow Bash?",
        "request_id": "req-123",
    }


def test_event_to_json_result_includes_session_id() -> None:
    assert event_to_json(
        ResultEvent(
            type="result",
            result="done",
            session_id="sess-1",
            is_error=False,
            effective_model="sonnet",
            usage={"input_tokens": "10"},
        )
    ) == {
        "type": "result",
        "text": "done",
        "is_error": False,
        "effective_model": "sonnet",
        "usage": {"input_tokens": "10"},
        "session_id": "sess-1",
    }


def test_event_to_json_thinking() -> None:
    assert event_to_json(ThinkingEvent(type="assistant", text="hmm")) == {
        "type": "thinking",
        "text": "hmm",
    }


def test_event_to_json_threads_subagent_attribution_fields() -> None:
    """Stream events fired from inside a Task subagent must round-trip
    parent_tool_use_id (and tool_use_id for tool calls) so the client can
    label the activity line in the trace. Missing the IDs would collapse
    parent and subagent work into one ambiguous timeline.
    """
    text = event_to_json(
        AssistantTextDelta(
            type="assistant",
            text="from subagent",
            parent_tool_use_id="toolu_parent_42",
        )
    )
    assert text == {
        "type": "text_delta",
        "text": "from subagent",
        "parent_tool_use_id": "toolu_parent_42",
    }

    tool = event_to_json(
        ToolUseEvent(
            type="assistant",
            tool_name="Bash",
            tool_input="ls",
            tool_use_id="toolu_child_99",
            parent_tool_use_id="toolu_parent_42",
        )
    )
    assert tool == {
        "type": "tool_use",
        "tool_name": "Bash",
        "tool_input": "ls",
        "tool_use_id": "toolu_child_99",
        "parent_tool_use_id": "toolu_parent_42",
    }

    thinking = event_to_json(
        ThinkingEvent(
            type="assistant",
            text="reasoning",
            parent_tool_use_id="toolu_parent_42",
        )
    )
    assert thinking == {
        "type": "thinking",
        "text": "reasoning",
        "parent_tool_use_id": "toolu_parent_42",
    }

    # Parent's own events must not gain the field — the wire payload should
    # match the legacy two-key shape so older clients keep working.
    parent_only = event_to_json(
        ToolUseEvent(type="assistant", tool_name="Read", tool_input="/etc/hosts")
    )
    assert parent_only == {
        "type": "tool_use",
        "tool_name": "Read",
        "tool_input": "/etc/hosts",
    }


@pytest.mark.asyncio
async def test_stream_replay_and_live_events() -> None:
    stream = ChatStream("hi")
    stream.publish({"type": "text_delta", "text": "one"})

    received: list[dict] = []

    async def consume() -> None:
        async for payload in stream.subscribe():
            received.append(payload)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the subscriber register

    stream.publish({"type": "text_delta", "text": "two"})
    stream.finish()

    await task

    assert received == [
        {"type": "text_delta", "text": "one"},
        {"type": "text_delta", "text": "two"},
    ]


def test_broker_get_returns_none_when_stream_done() -> None:
    broker = ChatStreamBroker()
    stream = ChatStream("hi")
    broker.register("c1", stream)
    assert broker.get("c1") is stream
    stream.finish()
    assert broker.get("c1") is None


def test_resolve_permission_strips_event_from_replay_buffer() -> None:
    """A subscriber connecting after the user has answered must NOT replay
    the prompt — otherwise reopening the chat re-shows an Approve/Deny
    card for an already-resolved request."""
    stream = ChatStream("hi")
    stream.publish({"type": "text_delta", "text": "thinking..."})
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "ls",
        "message": "Approve use of Bash?",
        "request_id": "req-A",
    })
    stream.publish({"type": "text_delta", "text": "more"})

    removed = stream.resolve_permission("req-A")
    assert removed is True

    replay = stream.buffered_events()
    assert all(
        ev.get("type") != "permission_request" for ev in replay
    ), f"permission_request leaked into replay: {replay}"
    # The surrounding text events still replay so the trace stays intact.
    assert {"type": "text_delta", "text": "thinking..."} in replay
    assert {"type": "text_delta", "text": "more"} in replay


def test_resolve_permission_returns_false_for_unknown_id() -> None:
    stream = ChatStream("hi")
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "",
        "message": "?",
        "request_id": "req-X",
    })
    assert stream.resolve_permission("req-Y") is False
    # Original event still present.
    assert any(
        ev.get("type") == "permission_request"
        for ev in stream.buffered_events()
    )


def test_resolve_permission_handles_empty_id() -> None:
    """Empty id is the "stale reply, no provider" path — it must be a no-op
    rather than wiping every buffered permission_request."""
    stream = ChatStream("hi")
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "",
        "message": "?",
        "request_id": "req-X",
    })
    assert stream.resolve_permission("") is False
    assert len(stream.buffered_events()) == 1


def test_subscribe_emits_keepalive_during_idle_gap(monkeypatch) -> None:
    """A stream with no events for the keepalive interval yields a keepalive
    frame so the WebSocket has traffic and a dead socket surfaces promptly.
    Regression: a background tool (e.g. a dynamic workflow) can leave the
    parent turn silent for tens of seconds, killing an idle socket."""
    import ciao.web.chat_broker as broker

    monkeypatch.setattr(broker, "STREAM_KEEPALIVE_SECONDS", 0.05)

    async def _run() -> None:
        stream = ChatStream("hi")
        agen = stream.subscribe()
        # No event published yet: the first yield must be a keepalive,
        # emitted after the (patched, tiny) idle timeout.
        first = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert first == {"type": "keepalive"}
        # A real event still flows through afterwards.
        stream.publish({"type": "text_delta", "text": "yo"})
        nxt = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert nxt == {"type": "text_delta", "text": "yo"}
        # finish() ends iteration even though keepalives were interleaving.
        stream.finish()
        with pytest.raises(StopAsyncIteration):
            # Drain any pending keepalive, then expect the sentinel stop.
            for _ in range(50):
                item = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
                if item != {"type": "keepalive"}:
                    raise AssertionError(f"unexpected post-finish item: {item}")

    asyncio.run(_run())


def test_events_hub_subscribe_emits_keepalive(monkeypatch) -> None:
    """The global events hub also keepalives so /ws/events doesn't die idle
    and miss the chat_streaming_done recovery signal."""
    import ciao.web.chat_broker as broker

    monkeypatch.setattr(broker, "STREAM_KEEPALIVE_SECONDS", 0.05)

    async def _run() -> None:
        hub = broker.EventsHub()
        agen = hub.subscribe()
        first = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
        assert first == {"type": "keepalive"}
        hub.publish({"type": "chat_streaming_done", "chat_id": "c1"})
        # Skip any interleaved keepalives to find the real event.
        for _ in range(50):
            item = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
            if item.get("type") == "chat_streaming_done":
                break
        else:
            raise AssertionError("never received chat_streaming_done")
        await agen.aclose()

    asyncio.run(_run())
