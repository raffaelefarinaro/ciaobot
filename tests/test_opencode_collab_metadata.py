"""The live collab tree must not read child transcripts unless asked (#1652).

``read_live_collab_tree`` runs on a 3s timer (the subagent watcher, the
schedule waiter) and on the sidebar's poll. Those callers only need a count
or a status, and the count comes from ``/api/session/active`` — but the read
used to gather every child's full message history on every call, so a chat
with 66 children re-read 66 transcripts every few seconds.

The transcript is only needed by the subagent detail view, which asks for it
with ``?agent_id=``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.providers.opencode import OpencodeProvider


class _Response:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _RecordingClient:
    """A fake OpenCode server that records every path it is asked for."""

    def __init__(self, children: list[dict], active: dict) -> None:
        self.children = children
        self.active = active
        self.get_calls: list[str] = []

    async def get(self, path: str, *, params=None):
        self.get_calls.append(path)
        if path == "/api/session":
            return _Response({"data": self.children})
        if path == "/api/session/active":
            return _Response({"data": self.active})
        if path.endswith("/message"):
            return _Response({
                "data": [],
                "cursor": {"previous": None, "next": None},
            })
        return _Response({"data": {}})

    async def post(self, path: str, json=None):  # pragma: no cover - unused
        raise AssertionError(f"unexpected POST {path}")


def _provider(tmp_path: Path, client: _RecordingClient) -> OpencodeProvider:
    provider = OpencodeProvider(tmp_path)
    provider._client = client  # type: ignore[assignment]
    provider._session_id = "ses_parent"
    return provider


def _children() -> list[dict]:
    return [
        {"id": "ses_child_a", "parentID": "ses_parent", "title": "First"},
        {"id": "ses_child_b", "parentID": "ses_parent", "title": "Second"},
    ]


def _active() -> dict:
    return {"ses_child_a": {"type": "running"}}


@pytest.mark.asyncio
async def test_live_collab_tree_skips_child_history_by_default(tmp_path: Path) -> None:
    """The poll path must not read a transcript to answer a count."""
    client = _RecordingClient(_children(), _active())
    provider = _provider(tmp_path, client)

    tree = await provider.read_live_collab_tree()

    assert not [path for path in client.get_calls if path.endswith("/message")]
    assert len(tree) == 2
    for entry in tree:
        assert "messages" not in entry
    # Activity still comes from the live process map.
    active = {entry["info"]["id"]: entry["active"] for entry in tree}
    assert active == {"ses_child_a": True, "ses_child_b": False}


@pytest.mark.asyncio
async def test_live_collab_tree_reads_history_when_asked(tmp_path: Path) -> None:
    """The detail view asks for one transcript, and gets it."""
    client = _RecordingClient(_children(), _active())
    provider = _provider(tmp_path, client)

    tree = await provider.read_live_collab_tree(include_messages=True)

    message_reads = [path for path in client.get_calls if path.endswith("/message")]
    assert sorted(message_reads) == [
        "/api/session/ses_child_a/message",
        "/api/session/ses_child_b/message",
    ]
    for entry in tree:
        assert "messages" in entry
        assert entry["messages"] == []
