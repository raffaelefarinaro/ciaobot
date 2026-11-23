"""Shared brains: Git checkouts a workspace links for read access.

The user clones a team's notes folder; Ciaobot only links it (by absolute path),
names it, reads its state and keeps it current with a fast-forward-only pull.
The engine never clones, never connects to a URL itself and never rewrites a
folder: no reset, rebase, stash, checkout, clean or push. Every update first
re-checks that the folder is still a real checkout of its own (not a symlink,
not a parent repository's subfolder) and still outside the install and vaults.
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
from ciao.config import SHARED_BRAIN_NAME_PATTERN, SharedBrainLink
from ciao.git_proc import EXACT_BYTES, GIT_TIMEOUT_DETAIL, run_git
from ciao.os_support.child_env import child_env
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
NAME_PATTERN = SHARED_BRAIN_NAME_PATTERN
OPERATOR_ONLY_KEYS = ("shared_brains",)

Outcome = Literal["updated", "up_to_date", "skipped", "failed"]

_NAME_RE = re.compile(NAME_PATTERN)
#: Prepended to every git call here. ``core.hooksPath`` points at the null device
#: so a checkout's own hooks never run; the credential helper is emptied so no
#: helper from the checkout's config runs; maintenance and gc never start.
_HARDEN: tuple[str, ...] = (
    "-c", "protocol.ext.allow=never",
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "maintenance.auto=false",
    "-c", "gc.auto=0",
    "-c", "credential.helper=",
)
#: Settings a checkout's own config (local or worktree scope) may hold. Anything
#: else can run a program, redirect a URL or read more config on git's behalf, and
#: the folder is refused. The allowlist is deliberate: a denylist missed
#: worktree-scoped and insteadOf keys. Real-world LFS clones (filter.lfs.*) are
#: refused by this list; that is the trade-off.
_SAFE_KEYS = frozenset({
    "core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
    "core.ignorecase", "core.precomposeunicode", "core.symlinks",
    "remote.origin.url", "remote.origin.fetch", "fetch.prune",
})
_SAFE_PREFIXES = ("branch.", "user.", "init.", "pull.", "extensions.")

_locks: dict[str, asyncio.Lock] = {}
_last_update: dict[str, dict[str, Any]] = {}
_pending: dict[str, asyncio.Task[Any]] = {}
_cursor = 0


def _git_env(folder: Path) -> dict[str, str]:
    """Child environment for git on a shared brain.

    ``GIT_CEILING_DIRECTORIES`` stops discovery at the folder's parent, so a
    folder whose own ``.git`` is gone is not read as part of a parent repository.
    ``GIT_SSH_COMMAND`` outranks a checkout's ``core.sshCommand``; prompts are off.
    """
    env = child_env()
    # An inherited GIT_DIR, GIT_WORK_TREE, GIT_CONFIG_* and friends would point
    # git at another repository or config than the folder being updated.
    for name in [key for key in env if key.startswith("GIT_")]:
        del env[name]
    env["GIT_CEILING_DIRECTORIES"] = str(folder.parent)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    env["GIT_ASKPASS"] = ""
    env.pop("SSH_ASKPASS", None)
    return env


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
            env=_git_env(path),
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


async def _git_async(folder: Path, *args: str, timeout: float = GIT_TIMEOUT_S) -> tuple[int, str, str]:
    return await run_git(
        folder, *_HARDEN, *args, timeout=timeout, env=_git_env(folder)
    )


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
    shown = redact_text(remote_url.strip())
    if "://" in text:
        rest = text.split("://", 1)[1]
        authority, _, path = rest.partition("/")
        host = authority.rsplit("@", 1)[-1]
    else:
        # scp-like: [user@]host:owner/repo
        rest = text.rsplit("@", 1)[-1]
        host, sep, path = rest.partition(":")
        if not sep:
            raise ValueError(f"unrecognised remote address: {shown}")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")
    if not host or not path:
        raise ValueError(f"unrecognised remote address: {shown}")
    return f"{host}/{path}"


def overlaps_protected(config: Any, path: str) -> bool:
    """True when ``path`` (after following symlinks) is, holds, or sits inside the
    install, a vault or the runtime directory. Pure path arithmetic, no git call."""
    target = Path(os.path.realpath(path))
    protected = [Path(config.workspace_root), Path(config.state_path).parent]
    for name in config.workspace_names():
        try:
            protected.append(Path(config.workspace_vault_root(name)))
        except ValueError:
            continue
    for root in protected:
        root = root.resolve()
        if target == root or target.is_relative_to(root) or root.is_relative_to(target):
            return True
    return False


def validate_brain_path(config: Any, raw: str) -> str:
    """The resolved checkout path, or ValueError when it cannot be a shared brain."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise ValueError("Shared brain path contains a control character")
    resolved = validate_repo_path(raw)
    if not resolved:
        return ""
    if overlaps_protected(config, resolved):
        raise ValueError(
            f"Shared brain folder must be outside the Ciaobot install and its vaults: {resolved}"
        )
    return resolved


def _first_unsafe_key(listing: str) -> str | None:
    """The first local or worktree setting outside the allowlist, or None.

    Global, system and command-line scopes are not the folder's own and are skipped.
    """
    for line in listing.splitlines():
        scope, _, entry = line.partition("\t")
        if scope not in ("local", "worktree"):
            continue
        key = entry.split("=", 1)[0]
        lowered = key.lower()
        if lowered in _SAFE_KEYS or lowered.startswith(_SAFE_PREFIXES):
            continue
        return key
    return None


def _refusal_message(key: str) -> str:
    return (
        "this folder's git settings can run programs; use a fresh clone "
        f"(refused setting: {redact_text(key)})"
    )


def _require_own_git_dir(path: str) -> None:
    """A linked worktree or submodule keeps ``.git`` as a pointer file; refuse it."""
    if not (Path(path) / ".git").is_dir():
        raise ValueError("this folder is a linked worktree or submodule; use a fresh clone")


def _refuse_program_config(path: str) -> None:
    """Refuse a new link whose own git config (any local or worktree scope) is outside the allowlist."""
    _require_own_git_dir(path)
    code, listing = _git(Path(path), "config", "--list", "--show-scope")
    if code != 0:
        raise ValueError(f"could not read the git settings of {path}")
    key = _first_unsafe_key(listing)
    if key is not None:
        raise ValueError(_refusal_message(key))


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
        _refuse_program_config(path)
        try:
            identity = canonical_identity(_origin_url(path))
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
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


def _path_problem(config: Any, path: str) -> str:
    """Why the folder cannot be used right now, or "" (no git call).

    ``path`` is the stored absolute path, not resolved, so a symlink swapped in
    after linking shows up as ``moved``.
    """
    if os.path.islink(path) or os.path.realpath(path) != path:
        return "moved"
    if overlaps_protected(config, path):
        return "outside_install"
    if not os.path.isdir(path):
        return "missing"
    git_entry = os.path.join(path, ".git")
    if os.path.exists(git_entry) and not os.path.isdir(git_entry):
        return "not_a_checkout"
    return ""


def _is_own_checkout(folder: Path, top: str) -> bool:
    return bool(top.strip()) and os.path.realpath(top.strip()) == os.path.realpath(folder)


def brain_state(link: SharedBrainLink, config: Any) -> dict[str, Any]:
    """Live state of one linked folder, read synchronously with short git calls."""
    state: dict[str, Any] = {
        "name": link.name,
        "identity": link.identity,
        "path": link.path,
        "origin_changed": False,
        "missing": False,
        "problem": "",
        "branch": "",
        "head": "",
        "head_date": "",
        "dirty": False,
        "upstream": False,
        "fetched_at": fetched_at(link.path),
        "last_update": _last_update.get(_lock_key(link.path)),
    }
    problem = _path_problem(config, link.path)
    if problem:
        state["problem"] = problem
        state["missing"] = problem == "missing"
        return state
    folder = Path(link.path)
    code, top = _git(folder, "rev-parse", "--show-toplevel")
    if code != 0 or not _is_own_checkout(folder, top):
        state["problem"] = "not_a_checkout"
        return state
    code, listing = _git(folder, "config", "--list", "--show-scope")
    if code != 0 or _first_unsafe_key(listing) is not None:
        state["problem"] = "unsafe_config"
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
    """Absolute path with ``~`` expanded but symlinks NOT followed: a swapped link must show."""
    return os.path.abspath(os.path.expanduser(path))


async def update_brain(path: str, config: Any) -> dict[str, Any]:
    """Fetch and fast-forward one folder, one case from the matrix (see the issue)."""
    key = _lock_key(path)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        result = await _update_unlocked(config, Path(key))
    _last_update[key] = result
    return result


async def _update_unlocked(config: Any, folder: Path) -> dict[str, Any]:
    if not folder.is_dir():
        return _result("failed", "missing")
    problem = _path_problem(config, str(folder))
    if problem:
        return _result("failed", problem)
    code, top, _ = await _git_async(folder, "rev-parse", "--show-toplevel")
    if code != 0 or not _is_own_checkout(folder, top):
        return _result("failed", "not_a_checkout")
    code, listing, _ = await _git_async(folder, "config", "--list", "--show-scope")
    if code != 0 or _first_unsafe_key(listing) is not None:
        return _result("failed", "unsafe_config")
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


def schedule_update(path: str, config: Any) -> None:
    """Start one background update for ``path`` unless one is already pending."""
    key = _lock_key(path)
    if key in _pending:
        return
    task = asyncio.create_task(update_brain(key, config))
    _pending[key] = task
    task.add_done_callback(lambda _done: _pending.pop(key, None))


async def _run_pass(config: Any, paths: list[str]) -> dict[str, int]:
    """Update each path once, one after another; a failure counts and moves on."""
    counts: dict[str, int] = {}
    for path in paths:
        try:
            outcome = (await update_brain(path, config))["outcome"]
        except Exception:
            logger.exception("Shared brain update failed")
            outcome = "failed"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def _linked_paths(config: Any) -> list[str]:
    """Distinct linked paths the loop should update; links into the install or a vault are kept but skipped."""
    seen: dict[str, None] = {}
    for workspace in getattr(config, "workspaces", {}).values():
        for link in getattr(workspace, "shared_brains", []):
            if not overlaps_protected(config, link.path):
                seen[link.path] = None
    return list(seen)


def _forget_unlinked(keep: set[str]) -> None:
    """Drop lock and result entries for folders no workspace links any more.

    A lock that is held belongs to a running update and stays.
    """
    for key in [k for k in _locks if k not in keep and not _locks[k].locked()]:
        _locks.pop(key, None)
    for key in [k for k in _last_update if k not in keep]:
        _last_update.pop(key, None)


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
                paths = _linked_paths(config)
                _forget_unlinked({_lock_key(p) for p in paths})
                counts = await _run_pass(config, _next_batch(paths))
                run.extra["outcomes"] = counts
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Shared brain update loop failed")
        await asyncio.sleep(UPDATE_INTERVAL_S)
