"""Where a chat in a linked project worked: toplevel, branch, worktree, exists.

Hints come from a turn's file touches and shell directories; every git call is
async and bounded, so nothing here blocks the event loop.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ciao.git_proc import run_git
from ciao.models import ToolUseEvent

GIT_TIMEOUT_S = 2.0
MAX_HINTS = 8
REFRESH_TIMEOUT_S = 5.0


def tool_event_hints(event: ToolUseEvent) -> list[str]:
    """Absolute directories one tool call touched or ran in."""
    if event.type == "tool_result":
        return []
    hints: list[str] = []
    for touch in event.file_touches or []:
        if not isinstance(touch, dict) or touch.get("action") == "surfaced":
            continue
        fp = touch.get("file_path")
        if isinstance(fp, str) and Path(fp).is_absolute():
            hints.append(str(Path(fp).parent))
    hints.extend(event.work_dirs or [])
    return hints


async def git_location(directory: Path) -> dict[str, Any] | None:
    """Git facts for the nearest existing directory at or above ``directory``."""
    try:
        d = directory
        while not d.is_dir():
            if d.parent == d:
                return None
            d = d.parent
        rc, out, _ = await run_git(
            d,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
            timeout=GIT_TIMEOUT_S,
        )
        lines = out.splitlines()
        if rc != 0 or len(lines) < 3:
            return None
        top, git_dir, common = (line.strip() for line in lines[:3])
        brc, bout, _ = await run_git(
            Path(top), "branch", "--show-current", timeout=GIT_TIMEOUT_S
        )
        branch = bout.strip() if brc == 0 else ""
        common_resolved = Path(common).resolve()
        return {
            "path": str(Path(top).resolve()),
            "branch": branch,
            "is_worktree": Path(git_dir).resolve() != common_resolved,
            "exists": True,
            "common": str(common_resolved),
        }
    except OSError:
        return None


def public(loc: Mapping[str, Any]) -> dict[str, Any]:
    return {k: loc[k] for k in ("path", "branch", "is_worktree", "exists")}


async def resolve_chat_location(
    *, repo_path: str, hints: Sequence[str], previous: Mapping[str, Any]
) -> dict[str, Any]:
    """The chat's location in the linked repo (or one of its worktrees), or ``{}``."""
    if not repo_path:
        return {}
    main = await git_location(Path(repo_path))
    if main is None:
        return {}
    common = main["common"]
    for hint in list(dict.fromkeys(reversed(hints)))[:MAX_HINTS]:
        loc = await git_location(Path(hint))
        if loc is not None and loc["common"] == common:
            return public(loc)
    prev_path = previous.get("path")
    if prev_path:
        gone = {**previous, "exists": False}
        if not Path(str(prev_path)).is_dir():
            return gone
        loc = await git_location(Path(str(prev_path)))
        if loc is not None and loc["common"] == common:
            return public(loc)
        return gone
    return {}
