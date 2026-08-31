"""Regressions found reviewing the v0.13.1 release branch.

Each test pins one behaviour that was wrong on the branch and is fixed here:
the archive lock's lifetime, and how durable turn metadata is paired onto the
rows rendered from a provider session file.
"""

from __future__ import annotations

import asyncio

import pytest

from ciao.web.routes_api import _overlay_transcript_metadata


# ── Metadata overlay: pair from the newest turn, not the oldest ──────────


def _assistant(name: str) -> dict:
    return {"role": "assistant", "content": name}


def _user(name: str) -> dict:
    return {"role": "user", "content": name}


def test_overlay_aligns_on_the_newest_turn_when_the_session_is_shorter() -> None:
    """A resumed chat's JSONL covers fewer turns than the transcript store.

    Pairing the two lists from the front then attributed every token count and
    context % to the wrong turn from the very first row on — a resumed or
    handed-over chat showed turn 1's usage on turn 4.
    """
    entries = [_user("q3"), _assistant("a3"), _user("q4"), _assistant("a4")]
    metadata = [
        {"role": "assistant", "usage": {"total": "1"}, "effective_model": "m1"},
        {"role": "assistant", "usage": {"total": "2"}, "effective_model": "m2"},
        {"role": "assistant", "usage": {"total": "3"}, "effective_model": "m3"},
        {"role": "assistant", "usage": {"total": "4"}, "effective_model": "m4"},
    ]

    _overlay_transcript_metadata(entries, metadata)

    assert entries[1]["usage"] == {"total": "3"}
    assert entries[3]["usage"] == {"total": "4"}


def test_overlay_leaves_the_unmatched_head_alone() -> None:
    """Fewer session rows than transcript turns: the newest ones pair up.

    The complement of the live-turn case above. Here the session is the short
    list (it starts at the resume point), so the rows the two lists share are
    the newest ones and the transcript's older turns have nothing to attach to.
    """
    entries = [_user("q2"), _assistant("a2")]
    metadata = [
        {"role": "assistant", "usage": {"total": "1"}},
        {"role": "assistant", "usage": {"total": "2"}},
    ]

    _overlay_transcript_metadata(entries, metadata)

    assert entries[1]["usage"] == {"total": "2"}


def test_overlay_leaves_a_live_turn_without_metadata() -> None:
    """A turn in flight makes the session LONGER than the transcript.

    `record_turn` only runs when a turn finishes, so mid-turn the session
    already carries the live assistant text while the transcript still ends at
    the previous completed turn. Matching tails there would shift every usage
    record forward by one and hang the previous turn's token count on the reply
    still being written.
    """
    entries = [
        _user("q1"), _assistant("a1"),
        _user("q2"), _assistant("a2"),
        _user("q3"), _assistant("live"),
    ]
    metadata = [
        {"role": "assistant", "usage": {"total": "1"}},
        {"role": "assistant", "usage": {"total": "2"}},
    ]

    _overlay_transcript_metadata(entries, metadata)

    assert entries[1]["usage"] == {"total": "1"}
    assert entries[3]["usage"] == {"total": "2"}
    assert "usage" not in entries[5]


def test_overlay_with_no_metadata_is_a_no_op() -> None:
    entries = [_user("q1"), _assistant("a1")]
    _overlay_transcript_metadata(entries, [])
    assert "usage" not in entries[1]


# ── Archive lock: refcounted, so a waiter never loses its lock ───────────


class _FakeManager:
    """Just enough of ProjectChatManager to exercise `archive_chat`."""

    from ciao.web.project_chats import ProjectChatManager

    archive_chat = ProjectChatManager.archive_chat

    def __init__(self, running: list[int], peak: list[int]) -> None:
        self._archive_locks: dict[str, asyncio.Lock] = {}
        self._archive_lock_users: dict[str, int] = {}
        self._chats = {"c1": type("C", (), {"archived": False})()}
        self._running = running
        self._peak = peak

    async def _archive_chat_unlocked(self, chat_id: str):
        self._running.append(1)
        self._peak.append(len(self._running))
        # A suspension point: without real serialization a second caller runs
        # its body here, concurrently with this one.
        await asyncio.sleep(0.03)
        self._running.pop()
        return None


async def test_a_third_archive_never_overlaps_a_woken_waiter() -> None:
    """The lock must survive a release that still has a waiter queued.

    `Lock.release()` clears `_locked` before the woken waiter resumes, so the
    old `if not lock.locked(): pop()` teardown dropped the dict entry while a
    waiter was still on it — and the next caller to arrive `setdefault`'d a
    *fresh* lock and ran alongside that waiter. Reproducing it needs exactly
    that ordering: A holds the lock, B queues behind it, and C arrives after A
    has released (and popped) while B is still inside its own body.
    """
    running: list[int] = []
    peak: list[int] = []
    manager = _FakeManager(running, peak)

    first = asyncio.create_task(manager.archive_chat("c1"))
    await asyncio.sleep(0)
    second = asyncio.create_task(manager.archive_chat("c1"))
    await asyncio.sleep(0.04)
    third = asyncio.create_task(manager.archive_chat("c1"))

    await asyncio.gather(first, second, third)

    assert max(peak) == 1
    # Fully drained, so nothing leaks per chat id.
    assert manager._archive_locks == {}
    assert manager._archive_lock_users == {}


async def test_archive_lock_is_dropped_after_a_single_call() -> None:
    manager = _FakeManager([], [])
    await manager.archive_chat("c1")
    assert manager._archive_locks == {}
    assert manager._archive_lock_users == {}
