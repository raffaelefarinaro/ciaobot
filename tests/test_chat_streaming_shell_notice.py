"""The drain loop records a finished shell before it can supersede the drain, so a notice is never lost."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, AsyncGenerator

import pytest

from ciao.models import AssistantTextDelta, ShellFinishedEvent
from ciao.web.chat_streaming import ChatStreaming


class _Host:
    """The drain's host: a provider that yields the given events, and a foreground turn that owns the broker."""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.noted: list[tuple[str, Any]] = []
        foreground = SimpleNamespace(background=False)
        self._broker = SimpleNamespace(get=lambda chat_id: foreground)
        self._providers = {"chat-1": SimpleNamespace(drain_events=self._drain)}

    async def _drain(self) -> AsyncGenerator[Any, None]:
        for event in self._events:
            yield event

    def __getattr__(self, name: str) -> Any:
        # The drain's teardown calls host hooks this test does not exercise; they do nothing here.
        return lambda *args, **kwargs: None

    def _note_shell_finished(self, chat_id: str, event: Any) -> None:
        self.noted.append((chat_id, event))


def _shell_end(call_id: str) -> ShellFinishedEvent:
    return ShellFinishedEvent(type="shell_finished", tool_use_id=call_id, command="make", exit_code=0)


@pytest.mark.asyncio
async def test_a_shell_notice_pulled_while_a_foreground_turn_takes_over_is_still_recorded() -> None:
    # The first event is the notice; the foreground turn owns the broker, so the drain is superseded
    # at the next check. The notice must already be recorded: a queue item pulled here is never re-queued.
    # The text event after the first notice is where the drain is superseded and stops.
    text = AssistantTextDelta(type="text_delta", text="foreground")
    host = _Host([_shell_end("call-1"), text, _shell_end("call-2")])
    streaming = ChatStreaming(host)  # type: ignore[arg-type]
    await streaming.drain_between_turns("chat-1", "project-1")
    assert [event.tool_use_id for _, event in host.noted] == ["call-1"]
