"""Shared brains: Git checkouts a workspace links for read access.

The user clones a team's notes folder; Ciaobot only links it (by absolute path),
names it, reads its state and keeps it current with a fast-forward-only pull.
The engine never clones, never connects to a URL itself and never rewrites a
folder: no reset, rebase, stash, checkout, clean or push.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import job_runs
from ciao.config import SharedBrainLink
from ciao.git_proc import EXACT_BYTES, GIT_TIMEOUT_DETAIL, run_git
from ciao.redact import redact_text
from ciao.repo_link import validate_repo_path

logger = logging.getLogger(__name__)

UPDATE_INTERVAL_S = 1800
FIRST_UPDATE_DELAY_S = 60
FETCH_TIMEOUT_S = 60.0
GIT_TIMEOUT_S = 5.0
MAX_REASON_CHARS = 300
MAX_BRAINS_PER_WORKSPACE = 5
MAX_BRAINS_PER_PASS = 20
NAME_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$"
OPERATOR_ONLY_KEYS = ("shared_brains",)

Outcome = Literal["updated", "up_to_date", "skipped", "failed"]

_NAME_RE = re.compile(NAME_PATTERN)
#: Hardening every git call in this module starts with. ``core.hooksPath`` points
#: at the null device so a checkout's own hooks never run.
_HARDEN: tuple[str, ...] = (
    "-c", "protocol.ext.allow=never",
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
)

_locks: dict[str, asyncio.Lock] = {}
_last_update: dict[str, dict[str, str]] = {}
_tasks: set[asyncio.Task[Any]] = set()
_cursor = 0


def _git(path: Path, *args: str) -> tuple[int, str]:
    """(returncode, stdout) of a quick sync git call; (1, "") on spawn failure or timeout."""
    try:
        proc = subprocess.run(
            ["git", *EXACT_BYTES, *_HARDEN, "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _origin_url(path: str) -> str:
    code, out = _git(Path(path), "remote", "get-url", "origin")
    return out.strip() if code == 0 else ""


def canonical_identity(remote_url: str) -> str:
    """``host/owner/repo`` for a remote address; the same repository gives the same string."""
    text = remote_url.strip().lower()
    if not text:
        raise ValueError("shared brain has no remote called origin")
    if "://" in text:
        rest = text.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1]
    else:
        # scp-like: [user@]host:owner/repo
        rest = text.rsplit("@", 1)[-1]
        host, sep, path = rest.partition(":")
        if not sep:
            raise ValueError(f"unrecognised remote address: {remote_url.strip()}")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not host or not path:
        raise ValueError(f"unrecognised remote address: {remote_url.strip()}")
    return f"{host}/{path}"


def validate_brain_path(config: Any, raw: str) -> str:
    """The resolved checkout path, or ValueError when it cannot be a shared brain."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Shared brain path contains a control character")
    resolved = validate_repo_path(raw)
    if not resolved:
        return ""
    target = Path(resolved)
    protected = [Path(config.workspace_root), Path(config.state_path).parent]
    for name in config.workspace_names():
        try:
            protected.append(Path(config.workspace_vault_root(name)))
        except ValueError:
            continue
    for root in protected:
        root = root.resolve()
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            raise ValueError(
                f"Shared brain folder must be outside the Ciaobot install and its vaults: {resolved}"
            )
    return resolved


def _derived_name(identity: str, taken: set[str]) -> str:
    repo = identity.rsplit("/", 1)[-1]
    base = re.sub(r"[^a-z0-9-]+", "-", repo).strip("-")[:32].strip("-") or "brain"
    candidate, n = base, 2
    while candidate in taken:
        suffix = f"-{n}"
        candidate = f"{base[: 32 - len(suffix)].rstrip('-')}{suffix}"
        n += 1
    return candidate


def resolve_links(
    config: Any,
    workspace: str,
    raw: list[dict[str, str]],
    existing: list[SharedBrainLink],
) -> list[SharedBrainLink]:
    """The whole new list for one workspace. Git runs only for rows that are new."""
    if len(raw) > MAX_BRAINS_PER_WORKSPACE:
        raise ValueError(f"A workspace can link at most {MAX_BRAINS_PER_WORKSPACE} shared brains")
    by_path = {link.path: link for link in existing}
    names: set[str] = set()
    identities: dict[str, str] = {}
    paths: set[str] = set()
    out: list[SharedBrainLink] = []
    for row in raw:
        requested = str(row.get("name") or "").strip()
        raw_path = str(row.get("path", ""))
        key = os.path.realpath(os.path.expanduser(raw_path.strip()))
        if key in paths:
            raise ValueError(f"Shared brain folder is listed twice: {key}")
        paths.add(key)
        kept = by_path.get(key)
        if kept is not None:
            if requested and requested != kept.name:
                raise ValueError(
                    f"Shared brain '{kept.name}' is already linked; remove and add it again to rename it"
                )
            names.add(kept.name)
            identities[kept.identity] = kept.name
            out.append(kept)
            continue
        path = validate_brain_path(config, raw_path)
        try:
            identity = canonical_identity(_origin_url(path))
        except ValueError as exc:
            raise ValueError(f"Shared brain {path} has no remote called origin: {exc}") from exc
        if identity in identities:
            raise ValueError(
                f"{path} is the same repository as shared brain '{identities[identity]}'"
            )
        if requested:
            if not _NAME_RE.match(requested):
                raise ValueError("shared brain name must be 1 to 32 lowercase letters, digits or dashes")
            if requested in names:
                raise ValueError(f"shared brain name '{requested}' is already used in this workspace")
            name = requested
        else:
            name = _derived_name(identity, names)
        names.add(name)
        identities[identity] = name
        out.append(SharedBrainLink(name=name, path=path, identity=identity))
    return out


def fetched_at(path: str) -> str | None:
    """ISO UTC mtime of ``<path>/.git/FETCH_HEAD``, or None. One stat, no subprocess."""
    git_dir = Path(path) / ".git"
    if not git_dir.is_dir():
        return None
    try:
        mtime = (git_dir / "FETCH_HEAD").stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def brain_state(link: SharedBrainLink) -> dict[str, Any]:
    """Live state of one linked folder, read synchronously with short git calls."""
    state: dict[str, Any] = {
        "name": link.name,
        "identity": link.identity,
        "path": link.path,
        "origin_changed": False,
        "missing": False,
        "branch": "",
        "head": "",
        "head_date": "",
        "dirty": False,
        "upstream": False,
        "fetched_at": fetched_at(link.path),
        "last_update": _last_update.get(link.path),
    }
    folder = Path(link.path)
    if not folder.is_dir():
        state["missing"] = True
        return state
    try:
        origin_now = canonical_identity(_origin_url(link.path))
    except ValueError:
        origin_now = ""
    state["origin_changed"] = origin_now != link.identity
    _code, branch = _git(folder, "branch", "--show-current")
    state["branch"] = branch.strip()
    code, head = _git(folder, "log", "-1", "--format=%h%x00%cs")
    if code == 0 and head.strip():
        sha, _, day = head.strip().partition("\x00")
        state["head"], state["head_date"] = sha, day
    code, status = _git(folder, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    state["dirty"] = code == 0 and bool(status.strip())
    code, _ = _git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    state["upstream"] = code == 0
    return state


def _result(outcome: Outcome, reason: str = "", detail: str = "") -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason": reason,
        "detail": redact_text(detail)[:MAX_REASON_CHARS],
        "at": _now(),
    }


def _lock_key(path: str) -> str:
    return str(Path(path).resolve())


async def update_brain(path: str) -> dict[str, Any]:
    """Fetch and fast-forward one folder, one case from the matrix (see the issue)."""
    key = _lock_key(path)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        result = await _update_unlocked(Path(key))
    _last_update[key] = result
    return result


async def _git_async(folder: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    return await run_git(folder, *_HARDEN, *args, timeout=timeout)


async def _update_unlocked(folder: Path) -> dict[str, Any]:
    if not folder.is_dir():
        return _result("failed", "missing")
    code, branch, _ = await _git_async(folder, "branch", "--show-current")
    if code != 0 or not branch.strip():
        return _result("skipped", "detached")
    code, _, _ = await _git_async(
        folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    if code != 0:
        return _result("skipped", "no_upstream")
    code, _, err = await _git_async(
        folder, "fetch", "--no-tags", "--no-recurse-submodules", timeout=FETCH_TIMEOUT_S
    )
    if code == -1 and err == GIT_TIMEOUT_DETAIL:
        return _result("failed", "timeout", err)
    if code != 0:
        return _result("failed", "fetch_failed", err)
    code, counts, err = await _git_async(
        folder, "rev-list", "--left-right", "--count", "HEAD...@{u}"
    )
    parts = counts.split()
    if code != 0 or len(parts) != 2:
        return _result("failed", "fetch_failed", err)
    ahead, behind = int(parts[0]), int(parts[1])
    if behind == 0:
        return _result("up_to_date")
    if ahead > 0:
        return _result("failed", "diverged")
    code, status, _ = await _git_async(
        folder, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"
    )
    if code != 0 or status.strip():
        return _result("skipped", "local_changes")
    code, _, err = await _git_async(folder, "merge", "--ff-only", "@{u}")
    if code != 0:
        return _result("failed", "merge_failed", err)
    return _result("updated")


def schedule_update(path: str) -> None:
    """Start one update in the background; the task is kept until it finishes."""
    task = asyncio.create_task(update_brain(path))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_pass(paths: list[str]) -> dict[str, int]:
    """Update each path once, one after another; a failure counts and moves on."""
    counts: dict[str, int] = {}
    for path in paths:
        try:
            outcome = (await update_brain(path))["outcome"]
        except Exception:
            logger.exception("Shared brain update failed")
            outcome = "failed"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _linked_paths(config: Any) -> list[str]:
    seen: dict[str, None] = {}
    for workspace in getattr(config, "workspaces", {}).values():
        for link in getattr(workspace, "shared_brains", []):
            seen[link.path] = None
    return list(seen)


def _next_batch(paths: list[str]) -> list[str]:
    """Up to MAX_BRAINS_PER_PASS paths, continuing where the last pass stopped."""
    global _cursor
    if not paths:
        return []
    _cursor %= len(paths)
    count = min(MAX_BRAINS_PER_PASS, len(paths))
    batch = [paths[(_cursor + i) % len(paths)] for i in range(count)]
    _cursor = (_cursor + count) % len(paths)
    return batch


async def update_loop(config: Any) -> None:
    """Background loop: every UPDATE_INTERVAL_S, update the linked folders once each."""
    await asyncio.sleep(FIRST_UPDATE_DELAY_S)
    while True:
        try:
            async with job_runs.track(
                "shared_brain_update", "Shared brain update", category="system",
            ) as run:
                counts = await _run_pass(_next_batch(_linked_paths(config)))
                run.extra["outcomes"] = counts
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Shared brain update loop failed")
        await asyncio.sleep(UPDATE_INTERVAL_S)
