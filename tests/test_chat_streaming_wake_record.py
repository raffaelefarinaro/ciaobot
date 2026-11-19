"""A turn a provider runs on its own after background work is filed for any provider (#3137)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import ResultEvent
from ciao.web.chat_streaming import ChatStreaming


class _Transcripts:
    def __init__(self) -> None:
        self.turns: list[tuple[Any, dict[str, Any]]] = []

    def record_turn(self, request: Any, **kwargs: Any) -> None:
        self.turns.append((request, kwargs))


def _streaming(provider: str) -> tuple[ChatStreaming, _Transcripts]:
    transcripts = _Transcripts()
    chat = SimpleNamespace(
        provider=provider, model="m", mode="auto", session_id="sess-1", title="Chat",
    )
    host = SimpleNamespace(_chats={"chat-1": chat}, _transcripts=transcripts)
    return ChatStreaming(host), transcripts  # type: ignore[arg-type]


@pytest.mark.parametrize("provider", ["opencode", "claude", "fake"])
def test_wake_turn_recorded_for_any_provider_with_origin_and_notice(provider: str) -> None:
    streaming, transcripts = _streaming(provider)
    event = ResultEvent(type="result", result="BGDONE", origin="task-notification")
    event.wake_notice = "A background shell finished."

    streaming._record_wake_turn("chat-1", event)

    assert len(transcripts.turns) == 1
    request, kwargs = transcripts.turns[0]
    assert request.prompt == "A background shell finished."
    assert kwargs["origin"] == "background_run"
    assert kwargs["input_kind"] == "task-notification"
    assert kwargs["provider"] == provider
    assert kwargs["response_text"] == "BGDONE"


@pytest.mark.parametrize(
    ("origin", "notice", "text"),
    [
        ("", "A background shell finished.", "BGDONE"),  # not a wake turn
        ("task-notification", "", "BGDONE"),  # the missed wake item has no notice
        ("task-notification", "A background shell finished.", "   "),  # nothing answered
    ],
)
def test_wake_turn_without_origin_notice_or_answer_records_nothing(
    origin: str, notice: str, text: str
) -> None:
    streaming, transcripts = _streaming("claude")
    event = ResultEvent(type="result", result=text, origin=origin)
    event.wake_notice = notice

    streaming._record_wake_turn("chat-1", event)

    assert transcripts.turns == []
