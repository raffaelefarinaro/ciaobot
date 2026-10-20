from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ciao import repo_location
from ciao.models import ToolUseEvent, tool_work_dirs

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    (path / "f.txt").write_text("x")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    return path.resolve()


def _abs(*parts: str) -> str:
    """An absolute path on this platform (a drive-less "/a" is relative on Windows)."""
    return os.path.abspath(os.path.join(os.sep, *parts))


def test_tool_work_dirs_reads_cd_git_c_and_workdir() -> None:
    ab, cd, x, w = _abs("a", "b"), _abs("c d"), _abs("x"), _abs("w")
    assert tool_work_dirs("Bash", {"command": f"cd {ab} && git -C '{cd}' status"}) == [ab, cd]
    assert tool_work_dirs("Bash", {"command": f"git -C {x}; ls"}) == [x]
    assert tool_work_dirs("Bash", {"command": "cd x && ls"}) == []
    assert tool_work_dirs("Read", {"command": f"cd {ab}"}) == []
    assert tool_work_dirs("bash", {"command": "ls", "workdir": w}) == [w]


@needs_git
@pytest.mark.asyncio
async def test_resolve_prefers_latest_worktree_hint_of_the_linked_repo(
    tmp_path: Path,
) -> None:
    main = _init_repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", str(wt), "-b", "feat")
    wt = wt.resolve()
    result = await repo_location.resolve_chat_location(
        repo_path=str(main), hints=[str(main), str(wt)], previous={}
    )
    assert result == {
        "path": str(wt),
        "branch": "feat",
        "is_worktree": True,
        "exists": True,
    }


@needs_git
@pytest.mark.asyncio
async def test_resolve_ignores_hints_from_other_repos(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    other = _init_repo(tmp_path / "other")
    result = await repo_location.resolve_chat_location(
        repo_path=str(main), hints=[str(other)], previous={}
    )
    assert result == {}


@needs_git
@pytest.mark.asyncio
async def test_resolve_marks_removed_worktree_not_existing(tmp_path: Path) -> None:
    main = _init_repo(tmp_path / "main")
    previous = {
        "path": str(tmp_path / "gone"),
        "branch": "feat",
        "is_worktree": True,
        "exists": True,
    }
    result = await repo_location.resolve_chat_location(
        repo_path=str(main), hints=[], previous=previous
    )
    assert result["exists"] is False
    assert result["path"] == previous["path"]


def test_tool_event_hints_skip_surfaced_results_and_relative_paths() -> None:
    event = ToolUseEvent(
        type="tool_use",
        tool_name="Bash",
        file_touches=[
            {"file_path": os.path.join(_abs("a", "b"), "c.txt"), "action": "write"},
            {"file_path": os.path.join(_abs("s"), "surfaced.md"), "action": "surfaced"},
            {"file_path": "rel/d.txt", "action": "write"},
        ],
        work_dirs=["/w"],
    )
    assert repo_location.tool_event_hints(event) == [_abs("a", "b"), "/w"]
    result = ToolUseEvent(
        type="tool_result", tool_name="Bash", file_touches=event.file_touches, work_dirs=["/w"]
    )
    assert repo_location.tool_event_hints(result) == []
