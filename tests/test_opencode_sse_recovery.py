"""Tests for opencode mid-turn SSE recovery (W5).

A dropped /event stream after the prompt was accepted must not fail the
turn: the provider re-subscribes a bounded number of times, then falls back
to polling the message list and replaying settled parts idempotently through
the same accumulator.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from ciao.models import AgentRequest
from ciao.providers.opencode import OpencodeProvider
# Same-directory import: tests/ is not a package (no __init__.py), and pytest
# puts this directory on sys.path under its default import mode.
from test_opencode_provider import _FakeEventStream, _provider


def _sse(payload: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(payload)}\n\n"


_DELTA = _sse({
    "type": "message.part.delta",
    "properties": {"sessionID": "s1", "partID": "p1", "field": "text", "delta": "Hello "},
})
_PART_FULL = _sse({
    "type": "message.part.updated",
    "properties": {
        "sessionID": "s1",
        "part": {"type": "text", "id": "p1", "text": "Hello recovered world"},
    },
})
_IDLE = _sse({"type": "session.idle", "properties": {"sessionID": "s1"}})


class _FlakyStream(_FakeEventStream):
    def __init__(self, lines: list[str], fail_after: int) -> None:
        super().__init__(lines)
        self._fail_after = fail_after

    async def aiter_bytes(self):
        for i, line in enumerate(self._lines):
            if i >= self._fail_after:
                raise httpx.ReadError("stream dropped mid-turn")
            yield line.encode("utf-8")


class _RecoveryClient:
    """Scripted per-attempt stream behaviour plus a message-list read."""

    def __init__(self, attempts: list[Any], messages: list[dict[str, Any]]) -> None:
        self._attempts = attempts
        self._messages = messages
        self.stream_calls = 0
        self.get_calls: list[str] = []

    def stream(self, _method: str, _path: str):
        spec = self._attempts[min(self.stream_calls, len(self._attempts) - 1)]
        self.stream_calls += 1
        if isinstance(spec, Exception):
            raise spec
        return spec

    async def get(self, path: str):
        self.get_calls.append(path)
        if path.endswith("/message"):
            class _Messages:
                status_code = 200

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return self._messages

            response = _Messages()
            response._messages = self._messages  # type: ignore[attr-defined]
            return response

        class _Other:
            status_code = 404
            text = ""

            def json(self):
                return {}

        return _Other()

    async def post(self, _path: str, json=None):
        class _Accepted:
            status_code = 200
            text = ""

        return _Accepted()


def _wire(provider: OpencodeProvider, monkeypatch: pytest.MonkeyPatch, client) -> None:
    async def fake_server(_request):
        return client

    async def fake_session(_request):
        return "s1"

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)
    # Keep the test fast: instant backoff + tiny poll cadence/window.
    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(
        "ciao.providers.opencode._OPENCODE_RECOVERY_POLL_S", 0.0
    )
    monkeypatch.setattr(
        "ciao.providers.opencode._OPENCODE_RECOVERY_WINDOW_S", 1.0
    )


_REQUEST = AgentRequest(prompt="hi", model="", mode="bypass", provider="opencode")


@pytest.mark.asyncio
async def test_dropped_stream_reconnects_and_finishes_cleanly(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    # Attempt 1 accepts the prompt, streams a delta, then dies. Attempt 2
    # replays the settled full part (idempotent suffix) and sees idle.
    client = _RecoveryClient(
        [
            _FlakyStream([_DELTA], fail_after=1),
            _FakeEventStream([_PART_FULL, _IDLE]),
        ],
        messages=[],
    )
    _wire(provider, monkeypatch, client)

    events = [
        event async for event in provider.run_streaming(_REQUEST, lambda _h: None)
    ]

    assert client.stream_calls == 2
    result = events[-1]
    assert result.type == "result"
    assert not result.is_error
    texts = "".join(e.text for e in events if e.type == "text")
    assert texts == "Hello recovered world"
    assert result.result == "Hello recovered world"


@pytest.mark.asyncio
async def test_exhausted_reconnects_reconcile_via_message_poll(
    tmp_path, monkeypatch
) -> None:
    provider = _provider(tmp_path)
    messages = [
        {
            "info": {"role": "assistant"},
            "parts": [{"type": "text", "id": "p1", "text": "Hello recovered world"}],
        }
    ]
    # Every stream attempt dies after the prompt lands; the message poll
    # then quiesces on its second read and replays the settled part.
    client = _RecoveryClient(
        [
            _FlakyStream([_DELTA], fail_after=1),
            httpx.ConnectError("server gone"),
            httpx.ConnectError("server gone"),
        ],
        messages=messages,
    )
    _wire(provider, monkeypatch, client)

    events = [
        event async for event in provider.run_streaming(_REQUEST, lambda _h: None)
    ]

    assert client.stream_calls == 3
    assert "/session/s1/message" in client.get_calls
    result = events[-1]
    assert result.type == "result"
    assert not result.is_error
    assert result.result == "Hello recovered world"
    assert provider._turn_recovered_via_poll is True
    # A clean poll reconciliation is not a fallback answer.
    assert result.fallback_final is False


@pytest.mark.asyncio
async def test_failure_before_prompt_still_hard_fails(tmp_path, monkeypatch) -> None:
    provider = _provider(tmp_path)
    client = _RecoveryClient(
        [httpx.ConnectError("never came up")],
        messages=[],
    )
    _wire(provider, monkeypatch, client)

    events = [
        event async for event in provider.run_streaming(_REQUEST, lambda _h: None)
    ]

    result = events[-1]
    assert result.is_error
    assert "opencode connection failed" in result.result


@pytest.mark.asyncio
async def test_poll_reconciliation_ignores_earlier_turns(tmp_path, monkeypatch) -> None:
    """A mid-turn drop must not replay the whole session as this turn's text.

    ``GET /session/{id}/message`` returns every message ever sent, and
    ``_reset_turn_state`` clears the per-part emitted counts at the start of
    each turn — so replaying all assistant messages re-emitted turns 1..N as
    the current turn's answer, which ``record_turn`` then persisted.
    """
    provider = _provider(tmp_path)
    messages = [
        {"info": {"id": "u1", "role": "user"}, "parts": [
            {"type": "text", "id": "up1", "text": "first question"},
        ]},
        {"info": {"id": "a1", "role": "assistant"}, "parts": [
            {"type": "text", "id": "old1", "text": "ANSWER FROM TURN ONE"},
        ]},
        {"info": {"id": "u2", "role": "user"}, "parts": [
            {"type": "text", "id": "up2", "text": "second question"},
        ]},
        {"info": {"id": "a2", "role": "assistant"}, "parts": [
            {"type": "text", "id": "p1", "text": "Hello recovered world"},
        ]},
    ]
    client = _RecoveryClient(
        [
            _FlakyStream([_DELTA], fail_after=1),
            httpx.ConnectError("server gone"),
            httpx.ConnectError("server gone"),
        ],
        messages=messages,
    )
    _wire(provider, monkeypatch, client)

    events = [
        event async for event in provider.run_streaming(_REQUEST, lambda _h: None)
    ]

    texts = "".join(e.text for e in events if e.type == "text")
    assert "ANSWER FROM TURN ONE" not in texts
    assert texts == "Hello recovered world"
    result = events[-1]
    assert result.type == "result"
    assert result.result == "Hello recovered world"


@pytest.mark.asyncio
async def test_poll_reconciliation_anchors_on_the_live_user_message(
    tmp_path, monkeypatch
) -> None:
    """The anchor is the turn's own user message, not just the newest one.

    If another client prompted the same session after us, the trailing user
    message is not ours; the id learned from ``message.updated`` is.
    """
    provider = _provider(tmp_path)
    messages = [
        {"info": {"id": "u1", "role": "user"}, "parts": []},
        {"info": {"id": "a1", "role": "assistant"}, "parts": [
            {"type": "text", "id": "old1", "text": "ANSWER FROM TURN ONE"},
        ]},
        {"info": {"id": "u2", "role": "user"}, "parts": []},
        {"info": {"id": "a2", "role": "assistant"}, "parts": [
            {"type": "text", "id": "new1", "text": "ours"},
        ]},
        {"info": {"id": "u3", "role": "user"}, "parts": []},
    ]
    provider._user_message_id = "u2"

    assert [part["id"] for part in provider._turn_assistant_parts(messages)] == ["new1"]


@pytest.mark.asyncio
async def test_rejected_prompt_yields_exactly_one_terminal_error(
    tmp_path, monkeypatch
) -> None:
    """A rejected prompt must not be followed by an empty success result.

    The rejection used to yield its own ResultEvent while ``run_streaming``
    still yielded its unconditional closing one — an empty result with
    ``is_error=False`` that landed last and overwrote the error in the PWA.
    """
    provider = _provider(tmp_path)

    class _RejectingClient(_RecoveryClient):
        async def post(self, _path: str, json=None):
            class _Rejected:
                status_code = 400
                text = "model not configured"

            return _Rejected()

    client = _RejectingClient([_FakeEventStream([_IDLE])], messages=[])
    _wire(provider, monkeypatch, client)

    events = [
        event async for event in provider.run_streaming(_REQUEST, lambda _h: None)
    ]

    results = [event for event in events if event.type == "result"]
    assert len(results) == 1
    assert results[0].is_error is True
    assert "opencode rejected the prompt" in results[0].result
    assert "model not configured" in results[0].result
