"""EventsHub: attach-before-iterate delivery and forced resync on overflow."""

from __future__ import annotations

import asyncio

from ciao.web.chat_broker import EventsHub


async def _take(sub, n: int) -> list[dict]:
    out: list[dict] = []
    async for payload in sub:
        out.append(payload)
        if len(out) == n:
            break
    return out


async def test_event_published_after_attach_but_before_iteration_is_delivered() -> None:
    hub = EventsHub()
    sub = hub.attach()
    # The /ws/events route builds and sends its snapshot between attach() and
    # the first iteration; a done event published in that gap must survive.
    hub.publish({"type": "chat_streaming_done", "chat_id": "c1"})

    assert await asyncio.wait_for(_take(sub, 1), 1) == [
        {"type": "chat_streaming_done", "chat_id": "c1"}
    ]
    sub.close()
    assert hub.subscriber_count == 0


async def test_overflow_ends_the_stream_instead_of_dropping_one_event() -> None:
    hub = EventsHub()
    sub = hub.attach()
    for i in range(257):
        hub.publish({"type": "x", "i": i})

    # The backlog is discarded and the stream ends, so the socket reconnects and
    # takes a fresh snapshot; the hub no longer holds the subscriber.
    assert await asyncio.wait_for(_take(sub, 1), 1) == []
    assert hub.subscriber_count == 0


async def test_close_detaches_the_subscriber() -> None:
    hub = EventsHub()
    sub = hub.attach()
    assert hub.subscriber_count == 1
    sub.close()
    sub.close()
    assert hub.subscriber_count == 0
