"""Where a chat in a linked project worked: toplevel, branch, worktree, exists.

Hints come from a turn's file touches and shell directories; every git call is
async and bounded, so nothing here blocks the event loop.
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ciao.git_proc import run_git, run_git_bounded, run_git_stream
from ciao.models import ToolUseEvent

GIT_TIMEOUT_S = 2.0
MAX_HINTS = 8
REFRESH_TIMEOUT_S = 5.0
WORKTREE_CHANGES_MAX_BYTES = 256 * 1024
WORKTREE_CHANGES_MAX_FILES = 200
WORKTREE_CHANGES_TIMEOUT_S = 5.0
_TRACKED_PATH_BATCH_BYTES = 16 * 1024
_TRACKED_PATH_BATCH_FILES = 128


async def _check_tracked_paths(checkout: Path, env: dict[str, str]) -> None:
    """Stream index entries and inspect filter attributes in bounded batches."""
    pending = bytearray()
    paths: list[bytes] = []
    batch_bytes = 0

    async def check_batch() -> None:
        nonlocal batch_bytes
        if not paths:
            return
        result = await run_git_bounded(
            checkout, "-c", "core.fsmonitor=false", "check-attr", "-z", "--stdin", "filter",
            timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=WORKTREE_CHANGES_MAX_BYTES,
            env=env, stdin=b"\0".join(paths) + b"\0",
        )
        if result.returncode != 0 or result.truncated:
            raise RuntimeError("git read failed")
        values = result.stdout.split(b"\0")[2::3]
        if any(value not in (b"unspecified", b"unset") for value in values if value):
            raise UnsupportedCheckout
        paths.clear()
        batch_bytes = 0

    async def consume(chunk: bytes) -> None:
        nonlocal batch_bytes
        pending.extend(chunk)
        while (end := pending.find(b"\0")) >= 0:
            record = bytes(pending[:end])
            del pending[:end + 1]
            tab = record.find(b"\t")
            if tab < 0:
                raise RuntimeError("git read failed")
            if record.startswith(b"160000 "):
                raise UnsupportedCheckout
            path = record[tab + 1:]
            if batch_bytes + len(path) + 1 > _TRACKED_PATH_BATCH_BYTES:
                await check_batch()
            paths.append(path)
            batch_bytes += len(path) + 1
            if len(paths) >= _TRACKED_PATH_BATCH_FILES:
                await check_batch()
        if len(pending) > 64 * 1024:
            raise RuntimeError("git read failed")

    result = await run_git_stream(
        checkout, "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z",
        timeout=WORKTREE_CHANGES_TIMEOUT_S, max_stderr=64 * 1024, env=env,
        on_stdout=consume,
    )
    if result.returncode != 0 or result.truncated or pending:
        raise RuntimeError("git read failed")
    await check_batch()


def _read_bounded_regular_file(path: Path, limit: int) -> bytes | None:
    """Read a regular metadata file without following links or opening FIFOs."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise UnsupportedCheckout
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsupportedCheckout from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise UnsupportedCheckout
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise UnsupportedCheckout
        return bytes(data)
    finally:
        os.close(fd)
class UnsupportedCheckout(Exception):
    """The checkout uses Git state this read cannot safely isolate."""


async def read_worktree_changes(
    checkout: Path, linked_repo: Path, *, dedicated: bool
) -> dict[str, Any]:
    """Bound the complete metadata and diff read, including preflight work."""
    try:
        return await asyncio.wait_for(
            _read_worktree_changes(checkout, linked_repo, dedicated=dedicated),
            timeout=WORKTREE_CHANGES_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError("git read failed") from exc


async def _read_worktree_changes(
    checkout: Path, linked_repo: Path, *, dedicated: bool
) -> dict[str, Any]:
    """Read bounded patches without using repository-provided Git configuration."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"})

    async def read(cwd: Path, *args: str, cap: int = 64 * 1024) -> bytes:
        result = await run_git_bounded(cwd, "-c", "core.fsmonitor=false", *args,
                                       timeout=WORKTREE_CHANGES_TIMEOUT_S,
                                       max_output=cap, env=env)
        if result.returncode != 0 or result.truncated:
            raise RuntimeError("git read failed")
        return result.stdout

    try:
        if not checkout.is_dir() or not linked_repo.is_dir():
            raise UnsupportedCheckout
        checkout, linked_repo = checkout.resolve(), linked_repo.resolve()
        source = (await read(checkout, "rev-parse", "--path-format=absolute", "--show-toplevel",
                             "--absolute-git-dir", "--git-common-dir", "--show-object-format")).decode().splitlines()
        linked = (await read(linked_repo, "rev-parse", "--path-format=absolute", "--show-toplevel",
                             "--absolute-git-dir", "--git-common-dir")).decode().splitlines()
        top, git_dir, common, object_format = source[:4]
        linked_top, _, linked_common = linked[:3]
        if (Path(top).resolve() != checkout or Path(linked_top).resolve() != linked_repo
                or Path(common).resolve() != Path(linked_common).resolve()):
            raise UnsupportedCheckout
        is_worktree = Path(git_dir).resolve() != Path(common).resolve()
        if (dedicated and (not is_worktree or checkout == linked_repo)) or (
            not dedicated and checkout != linked_repo
        ):
            raise UnsupportedCheckout
        if dedicated:
            registered = await read(linked_repo, "worktree", "list", "--porcelain", "-z", cap=64 * 1024)
            records = registered.split(b"\0")
            if not any(record == b"worktree " + os.fsencode(checkout) for record in records):
                raise UnsupportedCheckout
        git_dir_path = Path(git_dir)
        index = git_dir_path / "index"
        if not index.is_file() or list(git_dir_path.glob("sharedindex.*")):
            raise UnsupportedCheckout
        objects = Path(common) / "objects"
        alternates = await asyncio.to_thread(
            _read_bounded_regular_file, objects / "info/alternates", WORKTREE_CHANGES_MAX_BYTES
        )
        if alternates and alternates.strip():
            raise UnsupportedCheckout
        if list((objects / "pack").glob("*.promisor")):
            raise UnsupportedCheckout
        for setting in ("core.sparseCheckout", "index.sparse"):
            configured = await run_git_bounded(checkout, "-c", "core.fsmonitor=false", "config", "--bool", "--get", setting,
                                                timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=512, env=env)
            if configured.returncode == 0 and configured.stdout.strip() == b"true":
                raise UnsupportedCheckout
            if configured.returncode not in (0, 1) or configured.truncated:
                raise RuntimeError("git read failed")
        partial = await run_git_bounded(checkout, "-c", "core.fsmonitor=false", "config", "--get", "extensions.partialClone",
                                        timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=512, env=env)
        if partial.returncode == 0 and partial.stdout.strip():
            raise UnsupportedCheckout
        if partial.returncode not in (0, 1) or partial.truncated:
            raise RuntimeError("git read failed")
        head = (await read(checkout, "rev-parse", "--verify", "HEAD", cap=256)).decode().strip()
        if not head:
            raise UnsupportedCheckout
        await _check_tracked_paths(checkout, env)

        with tempfile.TemporaryDirectory(prefix="ciao-git-read-") as temp:
            isolated = Path(temp) / "gitdir"
            init = await run_git_bounded(checkout, "init", "--bare", "--template=", f"--object-format={object_format}", str(isolated),
                                         timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=1024, env=env)
            if init.returncode:
                raise RuntimeError("git read failed")
            with (isolated / "config").open("a", encoding="utf-8", newline="") as config_file:
                config_file.write("\n[core]\n\tbare = false\n")
            isolated_env = dict(env, GIT_DIR=str(isolated), GIT_WORK_TREE=str(checkout), GIT_INDEX_FILE=str(index),
                                GIT_OBJECT_DIRECTORY=str(objects), GIT_COMMON_DIR=str(isolated))
            info = isolated / "info"
            info.mkdir(exist_ok=True)
            exclude = Path(common) / "info/exclude"
            exclude_contents = await asyncio.to_thread(
                _read_bounded_regular_file, exclude, WORKTREE_CHANGES_MAX_BYTES
            )
            if exclude_contents is not None:
                (info / "exclude").write_bytes(exclude_contents)
            remaining, truncated = WORKTREE_CHANGES_MAX_BYTES, False

            async def patch(*args: str) -> str:
                nonlocal remaining, truncated
                if remaining == 0:
                    truncated = True
                    return ""
                result = await run_git_bounded(checkout, "--git-dir", str(isolated), "--work-tree", str(checkout), *args,
                                               timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=remaining, env=isolated_env)
                remaining = max(0, remaining - len(result.stdout) - len(result.stderr))
                truncated |= result.truncated
                if result.returncode == -1 and not result.truncated:
                    raise RuntimeError("git read failed")
                if result.returncode not in (0, -1):
                    raise RuntimeError("git read failed")
                return result.stdout.decode("utf-8", errors="replace")

            common_args = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--ignore-submodules=all")
            staged = await patch("diff", "--cached", *common_args, head, "--")
            unstaged = await patch("diff", *common_args, "--")
            files = await run_git_bounded(checkout, "--git-dir", str(isolated), "--work-tree", str(checkout),
                                          "ls-files", "--others", "--exclude-standard", "-z",
                                          timeout=WORKTREE_CHANGES_TIMEOUT_S, max_output=max(0, min(remaining, 64 * 1024)), env=isolated_env)
            if files.returncode == -1 and not files.truncated:
                raise RuntimeError("git read failed")
            if files.returncode not in (0, -1):
                raise RuntimeError("git read failed")
            file_records = files.stdout.split(b"\0")
            if file_records and file_records[-1] and not files.stdout.endswith(b"\0"):
                file_records.pop()  # A capped final path without its NUL is incomplete.
            names = [name.decode("utf-8", errors="replace") for name in file_records if name]
            truncated |= files.truncated or len(names) > WORKTREE_CHANGES_MAX_FILES
            final_source = (await read(checkout, "rev-parse", "--path-format=absolute", "--show-toplevel",
                                       "--absolute-git-dir", "--git-common-dir", "--show-object-format")).decode().splitlines()[:4]
            final_linked = (await read(linked_repo, "rev-parse", "--path-format=absolute", "--show-toplevel",
                                       "--absolute-git-dir", "--git-common-dir")).decode().splitlines()[:3]
            if final_source != source[:4] or final_linked != linked[:3]:
                return {"state": "unavailable", "reason": "unsupported_checkout"}
            return {"state": "ready", "source": "worktree" if dedicated else "linked_checkout",
                    "staged": staged, "unstaged": unstaged, "untracked": names[:WORKTREE_CHANGES_MAX_FILES],
                    "truncated": truncated}
    except UnsupportedCheckout:
        return {"state": "unavailable", "reason": "unsupported_checkout"}
    except (OSError, asyncio.TimeoutError, RuntimeError, ValueError, IndexError) as exc:
        raise RuntimeError("git read failed") from exc


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
