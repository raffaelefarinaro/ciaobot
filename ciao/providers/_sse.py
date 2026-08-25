"""Minimal SSE decoder for opencode's ``/event`` stream.

Adapted from ``httpx-sse`` (MIT, Florimond Manca) and the Stainless-generated
``opencode-sdk-python`` decoder. Spec-compliant handling of multi-line
``data:`` fields, ``\\r\\n`` line endings, ``event:`` / ``id:`` / ``retry:``
fields, and comment lines (``:``), none of which opencode currently emits but
all of which the SSE spec allows and a future build could send.

The decoder consumes raw bytes from ``httpx.Response.aiter_bytes()`` (async)
or ``iter_bytes()`` (sync), accumulates chunks until a blank-line terminator,
and yields one :class:`ServerSentEvent` per dispatch.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

__all__ = ["ServerSentEvent", "SSEDecoder"]


@dataclass
class ServerSentEvent:
    """One decoded SSE dispatch."""

    event: str | None
    data: str
    id: str | None
    retry: int | None

    def json(self) -> Any:
        return json.loads(self.data)


class SSEDecoder:
    """Incremental SSE decoder.

    Feed it raw bytes from an HTTP stream; it yields :class:`ServerSentEvent`
    objects as they complete. The decoder is stateful — one instance per
    stream — and not thread-safe.
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._data: list[str] = []
        self._last_event_id: str | None = None
        self._retry: int | None = None

    # ------------------------------------------------------------------ sync

    def iter_bytes(self, iterator: Iterator[bytes]) -> Iterator[ServerSentEvent]:
        for chunk in self._iter_chunks(iterator):
            for raw_line in chunk.splitlines():
                sse = self.decode(raw_line.decode("utf-8"))
                if sse:
                    yield sse

    # ----------------------------------------------------------------- async

    async def aiter_bytes(self, iterator: AsyncIterator[bytes]) -> AsyncIterator[ServerSentEvent]:
        async for chunk in self._aiter_chunks(iterator):
            for raw_line in chunk.splitlines():
                sse = self.decode(raw_line.decode("utf-8"))
                if sse:
                    yield sse

    # -------------------------------------------------------------- internals

    @staticmethod
    def _iter_chunks(iterator: Iterator[bytes]) -> Iterator[bytes]:
        data = b""
        for chunk in iterator:
            for line in chunk.splitlines(keepends=True):
                data += line
                if data.endswith((b"\r\r", b"\n\n", b"\r\n\r\n")):
                    yield data
                    data = b""
        if data:
            yield data

    @staticmethod
    async def _aiter_chunks(iterator: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        data = b""
        async for chunk in iterator:
            for line in chunk.splitlines(keepends=True):
                data += line
                if data.endswith((b"\r\r", b"\n\n", b"\r\n\r\n")):
                    yield data
                    data = b""
        if data:
            yield data

    def decode(self, line: str) -> ServerSentEvent | None:
        """Decode one line; returns an event only on the blank-line terminator."""
        if not line:
            if not self._event and not self._data and not self._last_event_id and self._retry is None:
                return None
            sse = ServerSentEvent(
                event=self._event,
                data="\n".join(self._data),
                id=self._last_event_id,
                retry=self._retry,
            )
            self._event = None
            self._data = []
            self._retry = None
            return sse

        if line.startswith(":"):
            return None

        fieldname, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]

        if fieldname == "event":
            self._event = value
        elif fieldname == "data":
            self._data.append(value)
        elif fieldname == "id":
            if "\0" not in value:
                self._last_event_id = value
        elif fieldname == "retry":
            try:
                self._retry = int(value)
            except (TypeError, ValueError):
                pass

        return None