"""The file kernel: lock, atomic replace, content revision, portable names."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ciao import file_kernel as fk


def test_content_revision_is_stable_and_text_sensitive() -> None:
    assert fk.content_revision("a\n") == fk.content_revision("a\n")
    assert fk.content_revision("a\n") != fk.content_revision("a")


def test_atomic_write_replaces_whole_file_and_cleans_up_on_refusal(tmp_path: Path) -> None:
    target = tmp_path / "Workspace" / "Unsure.md"
    fk.write_queue_atomically(target, "one\n")
    assert target.read_text(encoding="utf-8") == "one\n"

    class Refuse:
        def __enter__(self) -> None:
            raise RuntimeError("refused")

        def __exit__(self, *exc: object) -> None:
            return None

    with pytest.raises(RuntimeError):
        fk.write_queue_atomically(target, "two\n", publish_guard=Refuse)
    assert target.read_text(encoding="utf-8") == "one\n"
    assert not list(target.parent.glob("*.tmp"))


def test_lock_is_reentrant_in_one_thread_and_times_out_for_another(tmp_path: Path) -> None:
    target = tmp_path / "note.md"
    with fk.queue_lock(target):
        with fk.queue_lock(target):
            pass
        failure: list[BaseException] = []

        def other() -> None:
            try:
                with fk.queue_lock(target, timeout_s=0.2):
                    pass
            except fk.QueueLockError as exc:
                failure.append(exc)

        worker = threading.Thread(target=other)
        worker.start()
        worker.join()
        assert len(failure) == 1
    with fk.queue_lock(target, timeout_s=0.2):
        pass


def test_the_lock_file_lives_outside_the_vault(tmp_path: Path) -> None:
    target = tmp_path / "vault" / "note.md"
    target.parent.mkdir()
    with fk.queue_lock(target):
        pass
    assert not list(target.parent.glob("*.lock"))
    assert fk.lock_path_for(str(target.resolve())).parent != target.parent


def test_portable_name_problem() -> None:
    assert fk.portable_name_problem("Sofia Rossi") is None
    assert fk.portable_name_problem("People") is None
    for bad in ("a:b", "x?", "trail.", "trail ", "CON", "con.md"):
        assert isinstance(fk.portable_name_problem(bad), str), bad
    assert fk.safe_note_stem('a/b:c?"d') == "a b c d"
