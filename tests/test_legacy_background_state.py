"""Startup removes the retired `ciao run` runner's state folder (#2225)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ciao.main import remove_legacy_background_state


def test_removes_the_folder_and_its_logs(tmp_path: Path) -> None:
    legacy = tmp_path / "background"
    legacy.mkdir()
    (legacy / "state.json").write_text("{}")
    (legacy / "bg-1.log").write_text("done")
    (tmp_path / "state.json").write_text("{}")

    assert remove_legacy_background_state(tmp_path) is True
    assert not legacy.exists()
    assert (tmp_path / "state.json").exists()


def test_missing_folder_is_a_no_op(tmp_path: Path) -> None:
    assert remove_legacy_background_state(tmp_path) is False
    assert remove_legacy_background_state(tmp_path) is False


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_removes_only_the_link(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "background").symlink_to(target, target_is_directory=True)

    assert remove_legacy_background_state(runtime) is True
    assert not (runtime / "background").exists()
    assert (target / "keep.txt").read_text() == "keep"


def test_a_file_named_background_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "background").write_text("not ours")
    assert remove_legacy_background_state(tmp_path) is False
    assert (tmp_path / "background").read_text() == "not ours"
