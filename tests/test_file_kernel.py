"""The file kernel: lock, atomic replace, content revision, portable names."""

from __future__ import annotations

import os
import stat
import sys
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


pytestmark_posix = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


@pytestmark_posix
def test_a_link_at_any_temp_name_is_never_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    folder = tmp_path / "vault" / "Workspace"
    folder.mkdir(parents=True)
    target = folder / "Unsure.md"
    target.write_text("one\n", encoding="utf-8")
    # The old fixed temp name is never used again.
    os.symlink(outside, folder / ".Unsure.md.write.tmp")
    fk.write_queue_atomically(target, "two\n")
    assert outside.read_text(encoding="utf-8") == "keep\n"
    # A link planted at the very name the write picks makes it fail, not follow.
    monkeypatch.setattr(fk.secrets, "token_hex", lambda n: "f" * (2 * n))
    os.symlink(outside, folder / f".Unsure.md.{'f' * 16}.tmp")
    with pytest.raises(OSError):
        fk.write_queue_atomically(target, "three\n")
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert target.read_text(encoding="utf-8") == "two\n"
    assert not target.is_symlink()


@pytestmark_posix
def test_a_folder_that_is_a_link_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Unsure.md").write_text("theirs\n", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()
    os.symlink(elsewhere, vault / "Workspace")
    with pytest.raises(OSError):
        fk.write_queue_atomically(vault / "Workspace" / "Unsure.md", "mine\n")
    assert (elsewhere / "Unsure.md").read_text(encoding="utf-8") == "theirs\n"
    assert sorted(p.name for p in elsewhere.iterdir()) == ["Unsure.md"]


@pytestmark_posix
def test_a_folder_swapped_for_a_link_before_the_rename_is_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    folder = vault / "Workspace"
    folder.mkdir(parents=True)
    target = folder / "Unsure.md"
    target.write_text("one\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    class Swap:
        def __enter__(self) -> None:
            folder.rename(vault / "Workspace.moved")
            os.symlink(elsewhere, folder)

        def __exit__(self, *exc: object) -> None:
            return None

    with pytest.raises(fk.FolderChangedError):
        fk.write_queue_atomically(target, "two\n", publish_guard=Swap)
    assert list(elsewhere.iterdir()) == []
    moved = vault / "Workspace.moved"
    assert (moved / "Unsure.md").read_text(encoding="utf-8") == "one\n"
    assert not list(moved.glob("*.tmp"))


@pytestmark_posix
def test_the_replaced_file_keeps_its_mode(tmp_path: Path) -> None:
    target = tmp_path / "Workspace" / "Unsure.md"
    fk.write_queue_atomically(target, "one\n")
    target.chmod(0o600)
    fk.write_queue_atomically(target, "two\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.chmod(0o640)
    fk.write_queue_atomically(target, "three\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


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
