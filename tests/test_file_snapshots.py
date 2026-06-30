"""Tests for the per-chat per-file snapshot store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao.web.file_snapshots import SnapshotStore, MAX_SNAPSHOT_BYTES


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(tmp_path)


def _run(coro):
    return asyncio.run(coro)


def test_capture_writes_blob_and_meta(tmp_path: Path, store: SnapshotStore) -> None:
    target = tmp_path / "vault" / "note.md"
    target.parent.mkdir(parents=True)
    target.write_text("first version\n")

    meta = _run(store.capture(
        chat_id="chat-1",
        file_path=str(target),
        action="written",
        tool="Write",
    ))

    assert meta is not None
    assert meta.seq == 1
    assert meta.action == "written"
    assert meta.tool == "Write"
    assert meta.size == len("first version\n")
    assert not meta.truncated

    listed = store.list_snapshots(chat_id="chat-1", file_path=str(target))
    assert len(listed) == 1
    assert listed[0]["seq"] == 1

    content_meta = store.read_snapshot(
        chat_id="chat-1", file_path=str(target), seq=1,
    )
    assert content_meta is not None
    content, meta_dict = content_meta
    assert content == b"first version\n"
    assert meta_dict["seq"] == 1


def test_repeated_capture_dedups_identical_content(tmp_path: Path, store: SnapshotStore) -> None:
    """If the file content hasn't changed since the previous snapshot, the
    store should NOT write a new one. This is the path that catches our
    own hook firing on the ToolUseEvent before the CLI has actually run
    the edit — without dedup we'd record N copies of the pre-edit state."""
    target = tmp_path / "note.md"
    target.write_text("same content\n")

    m1 = _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))
    m2 = _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))

    assert m1 is not None and m2 is not None
    assert m1.seq == 1
    assert m2.seq == 1  # dedup returned the existing seq
    listed = store.list_snapshots(chat_id="c", file_path=str(target))
    assert len(listed) == 1


def test_capture_records_each_distinct_revision(tmp_path: Path, store: SnapshotStore) -> None:
    target = tmp_path / "note.md"

    target.write_text("v1\n")
    _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))
    target.write_text("v2\n")
    _run(store.capture(chat_id="c", file_path=str(target), action="edited", tool="Edit"))
    target.write_text("v3\n")
    _run(store.capture(chat_id="c", file_path=str(target), action="edited", tool="Edit"))

    listed = store.list_snapshots(chat_id="c", file_path=str(target))
    assert [m["seq"] for m in listed] == [1, 2, 3]
    assert [m["action"] for m in listed] == ["written", "edited", "edited"]

    # Each seq round-trips its content.
    for seq, expected in [(1, b"v1\n"), (2, b"v2\n"), (3, b"v3\n")]:
        rs = store.read_snapshot(chat_id="c", file_path=str(target), seq=seq)
        assert rs is not None
        content, _ = rs
        assert content == expected


def test_missing_file_returns_none(tmp_path: Path, store: SnapshotStore) -> None:
    meta = _run(store.capture(
        chat_id="c",
        file_path=str(tmp_path / "does-not-exist.md"),
        action="written",
        tool="Write",
    ))
    assert meta is None
    assert store.list_snapshots(chat_id="c", file_path=str(tmp_path / "does-not-exist.md")) == []


def test_oversized_file_records_truncated_marker(tmp_path: Path, store: SnapshotStore) -> None:
    """Anything bigger than MAX_SNAPSHOT_BYTES records a truncated meta but no
    blob: the History tab still shows the entry; the Diff/Preview tabs note
    the size limit. Keeps a runaway-generated 50 MB file from filling disk."""
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))

    meta = _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))
    assert meta is not None
    assert meta.truncated
    assert meta.size == MAX_SNAPSHOT_BYTES + 1
    rs = store.read_snapshot(chat_id="c", file_path=str(target), seq=1)
    assert rs is not None
    content, _ = rs
    assert content == b""


def test_list_files_for_chat_sorts_by_recent(tmp_path: Path, store: SnapshotStore) -> None:
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("a1")
    b.write_text("b1")
    _run(store.capture(chat_id="c", file_path=str(a), action="written", tool="Write"))
    _run(store.capture(chat_id="c", file_path=str(b), action="written", tool="Write"))
    b.write_text("b2")
    _run(store.capture(chat_id="c", file_path=str(b), action="edited", tool="Edit"))

    files = store.list_files_for_chat("c")
    assert len(files) == 2
    # `b` was touched most recently, so it sorts first.
    assert files[0]["file_path"] == str(b)
    assert files[0]["snapshots"] == 2
    assert files[1]["file_path"] == str(a)
    assert files[1]["snapshots"] == 1


def test_delete_chat_removes_all_snapshots(tmp_path: Path, store: SnapshotStore) -> None:
    target = tmp_path / "note.md"
    target.write_text("v1")
    _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))
    assert store.list_snapshots(chat_id="c", file_path=str(target))

    store.delete_chat("c")
    assert store.list_snapshots(chat_id="c", file_path=str(target)) == []
    assert store.list_files_for_chat("c") == []


def test_quoted_path_stays_in_single_directory(tmp_path: Path, store: SnapshotStore) -> None:
    """The store URL-encodes the file path into one flat directory component.
    A nested vault path like `memory-vault/personal/Ideas/foo.md` must NOT spawn three
    nested directories under the chat folder, otherwise a malicious path
    could traverse out via `../`."""
    target = tmp_path / "deep" / "nested" / "note.md"
    target.parent.mkdir(parents=True)
    target.write_text("v1")

    _run(store.capture(chat_id="c", file_path=str(target), action="written", tool="Write"))

    chat_dir = tmp_path / "c"
    # Exactly one immediate child: the URL-encoded path component.
    children = list(chat_dir.iterdir())
    assert len(children) == 1
    assert children[0].is_dir()
    assert "/" not in children[0].name  # encoded form
    # And the file path round-trips on the listing.
    files = store.list_files_for_chat("c")
    assert files[0]["file_path"] == str(target)
