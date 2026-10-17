"""Link a project to a local git checkout: validation, live state, candidates."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from ciao.git_proc import EXACT_BYTES

#: Where `candidates_payload` looks for checkouts, one level deep. Hardcoded on
#: purpose (no env var, no setting).
REPO_ROOTS: tuple[str, ...] = ("~/repos", "~/code", "~/src", "~/dev", "~/Developer", "~/projects")
GIT_TIMEOUT_S = 5.0
MAX_CANDIDATES = 500


def _git(path: Path, *args: str) -> tuple[int, str]:
    """(returncode, stdout) of `git -C path ...`; (1, "") on spawn failure or timeout."""
    try:
        proc = subprocess.run(
            ["git", *EXACT_BYTES, "-C", str(path), *args],
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


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def validate_repo_path(raw: str) -> str:
    """Return git's resolved toplevel for `raw`; "" for empty input (unlink).

    Raises ValueError when the path is not the top of an existing checkout.
    """
    text = raw.strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"Repository path must be absolute: {text}")
    if not path.exists():
        raise ValueError(f"Repository path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Repository path is not a directory: {path}")
    code, out = _git(path, "rev-parse", "--show-toplevel")
    if code != 0 or not out.strip():
        raise ValueError(f"Not a git repository: {path}")
    top = Path(out.strip())
    if not _same(top, path):
        raise ValueError(f"Repository path must be the top of the git checkout: {path} is inside {top}")
    return str(top.resolve())


def repo_state(path: str) -> dict[str, Any]:
    """Live git state of a linked checkout: `{path, branch, dirty, missing}`."""
    p = Path(path)
    if not p.is_dir():
        return {"path": path, "branch": "", "dirty": False, "missing": True}
    _code, out = _git(p, "branch", "--show-current")
    branch = out.strip()
    code, status = _git(p, "--no-optional-locks", "status", "--porcelain")
    dirty = code == 0 and bool(status.strip())
    return {"path": path, "branch": branch, "dirty": dirty, "missing": False}


def default_roots() -> list[Path]:
    return [Path(r).expanduser() for r in REPO_ROOTS]


def candidates(roots: list[Path] | None = None) -> list[dict[str, str]]:
    """Git checkouts one level under `roots`. No git subprocess: link time validates."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for root in roots or default_roots():
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            if not (child.is_dir() and (child / ".git").exists()):
                continue
            resolved = child.resolve()
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            found.append({"path": str(resolved), "name": child.name, "root": str(root)})
            if len(found) >= MAX_CANDIDATES:
                return found
    return found


def candidates_payload() -> dict[str, Any]:
    roots = default_roots()
    return {"roots": [str(r) for r in roots if r.is_dir()], "candidates": candidates(roots)}
