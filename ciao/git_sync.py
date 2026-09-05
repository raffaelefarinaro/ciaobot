"""Git sync helpers for the Ciaobot server.

Handles pulling latest changes on startup and merging before push.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

# Startup sync is awaited inline in `_run_server_locked`, BEFORE the server
# binds, so its ceiling is the user's worst-case cold start staring at a blank
# app. It deliberately does not use `local_session.GIT_NETWORK_TIMEOUT` (60s):
# that ceiling is right for the background loops, which can afford to wait out
# a slow remote because nothing is blocked on them. Here an unreachable remote
# must give up fast.
#
# The trade is real and worth stating: nothing else pulls on its own. The
# branch-backup loop only PUSHES, and fetch/merges solely when a push comes
# back non-fast-forward, so a startup pull killed at this ceiling means the
# checkout does not see the remote's new commits until the next boot or an
# explicit Settings → "Sync with Remote". Raise this rather than tighten it if
# real pulls start timing out.
GIT_STARTUP_TIMEOUT = 10.0

logger = logging.getLogger(__name__)

# The one list of paths a workspace snapshot must never pick up. `cli.py`
# writes it into a fresh workspace's .gitignore and `_ensure_sync_ignores`
# repairs workspaces that predate that guard, so the two MUST agree: a shorter
# repair list means `git add -A` stages provider credentials
# (`.claude/.credentials.json`) on exactly the old workspaces the repair exists
# for. It lives here rather than in `cli.py` because this module imports
# nothing from `ciao`, so either side can depend on it.
#
# No `.codex/` entry: codex is retired (`sync_skills` only prunes what older
# versions left behind, it never writes there), so ignoring it would be dead
# config. A stale `.codex/auth.json` is still caught — by `_protected_path`,
# which matches credential filenames wherever they sit, ignored or not.
WORKSPACE_GITIGNORE_ENTRIES = (
    ".env",
    ".envrc",
    ".direnv/",
    "secrets/",
    ".runtime/",
    ".claude/",
    ".agents/",
    ".opencode/",
    "opencode.json",
    "*.log",
)
_PROTECTED_CONFIG_NAMES = frozenset({".npmrc", ".netrc", ".pypirc"})
# Directory names that hold credentials wherever they sit in the tree. Matched
# per path segment, not as a prefix: `add -A` sweeps untracked files, so a
# nested `sub/secrets/token` reaches the index the same as a root-level one.
_PROTECTED_DIR_SEGMENTS = frozenset(
    {"secrets", ".ssh", ".aws", ".gnupg", ".direnv"}
)
# direnv keeps exported credentials in `.envrc`, which `add -A` sweeps up like
# any other untracked file. It is not covered by the `.env` / `.env.*` rules
# below — the name has no dot before `rc` — so it needs naming outright.
_PROTECTED_ENV_NAMES = frozenset({".envrc", ".envrc.local", ".env.local"})
# Credential filenames, matched wherever they sit. A directory-level guard is
# not enough on its own: .gitignore does not hide files git already tracks, so
# an older workspace that committed `.codex/auth.json` before any of this
# existed still shows it as modified, and `add -A` would stage and push the
# token. Matching the filename catches it regardless of which provider
# directory it lives in or whether that directory is ignored.
_PROTECTED_KEY_NAMES = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "credentials",
        "credentials.json",
        ".credentials.json",
        "auth.json",
        ".auth.json",
        "token.json",
    }
)


# One path per guard entry, used to ask git whether that guard actually bites.
# The probe is the worst thing the entry is there to hide, so a guard that no
# longer covers it is treated as absent.
_IGNORE_PROBES: tuple[tuple[str, str], ...] = (
    (".env", ".env"),
    (".envrc", ".envrc"),
    (".direnv/", ".direnv/dump"),
    ("secrets/", "secrets/token.json"),
    (".runtime/", ".runtime/state.json"),
    (".claude/", ".claude/.credentials.json"),
    (".agents/", ".agents/notes.md"),
    (".opencode/", ".opencode/auth.json"),
    ("opencode.json", "opencode.json"),
    ("*.log", "ciao.log"),
)


async def _ensure_sync_ignores(workspace: Path) -> None:
    """Keep startup snapshots from picking up credentials or runtime state.

    Effectiveness is asked of git, not inferred from the text of `.gitignore`.
    A line being *present* is not the same as the path being *ignored*: a later
    negation re-includes it, and

        .claude/
        !.claude/.credentials.json

    reads as fully guarded to a membership check while git happily reports the
    token as unignored — `add -A` then stages it and the backup loop pushes it
    to origin. `git check-ignore` answers the question the guard is actually
    asking. Re-appending a failed entry repairs it because gitignore resolves
    by last match, so the appended rule outranks the earlier negation.
    """
    path = workspace / ".gitignore"
    missing: list[str] = []
    for entry, probe in _IGNORE_PROBES:
        rc, _, _ = await _git(workspace, "check-ignore", "-q", "--no-index", probe)
        if rc == 0:
            continue
        if rc > 1:
            # check-ignore failed rather than answering (128 = not a work tree,
            # a broken .gitignore). Fall back to the membership check instead
            # of appending an entry on every startup forever.
            try:
                present = {
                    line.strip()
                    for line in path.read_text(encoding="utf-8").splitlines()
                }
            except OSError:
                present = set()
            if entry in present:
                continue
        missing.append(entry)
    if not missing:
        return
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        prefix = existing if existing.endswith("\n") or not existing else existing + "\n"
        path.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("Startup sync: could not enforce credential ignores", exc_info=True)


def _protected_path(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1].lower() if parts else ""
    env_template = name in {".env.example", ".env.sample", ".env.template", ".env.schema"}
    if any(part.lower() in _PROTECTED_DIR_SEGMENTS for part in parts[:-1]):
        return True
    return (
        (name == ".env" or (name.startswith(".env.") and not env_template))
        or name in _PROTECTED_ENV_NAMES
        or name in _PROTECTED_CONFIG_NAMES
        or name in _PROTECTED_KEY_NAMES
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _porcelain_paths(status_out: str) -> list[str]:
    """Every path in `git status --porcelain -z` output.

    Renames appear as two NUL-separated fields (``old`` then ``new``); both
    sides are returned so a rename *into* a protected location is caught even
    though the destination basename alone would not look protected.
    """
    paths: list[str] = []
    fields = status_out.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        if len(field) < 3:
            continue
        status = field[:2]
        path = field[3:]
        if not path:
            continue
        paths.append(path)
        if "R" in status and i < len(fields) and fields[i]:
            paths.append(fields[i])
            i += 1
    return paths


async def _git(workspace: Path, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if timeout is not None:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (-1, "", "git command timed out")
    else:
        stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace").rstrip(),
        stderr.decode(errors="replace").rstrip(),
    )


async def _is_git_repo(workspace: Path) -> bool:
    rc, out, _ = await _git(workspace, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


async def sync_workspace(workspace: Path) -> str | None:
    """Pull latest changes. Called on startup.

    Returns a status message to send to the user, or None if nothing changed.
    """
    if not await _is_git_repo(workspace):
        return None

    # Existing workspaces predate the setup guard. Repair it before status so
    # newly ignored credential files never reach `git add -A`.
    await _ensure_sync_ignores(workspace)

    # Auto-commit local changes so pull can handle them cleanly. Untracked
    # files are staged too (`add -A`, unlike the historical `add -u`): notes,
    # logs, and whole project folders that were never added by hand would
    # otherwise sit out of every automatic backup indefinitely — the only
    # path that commits anything (Settings "Sync with Remote") is manual, so
    # an untracked file could live forever with "everything says it backed
    # up" (the 2026-08-30 work-notes gap). `add -A` never stages gitignored
    # paths, so runtime scratch and caches (`.runtime/`, `Logs/Chats/`) are
    # untouched.
    # `-uall`, not the default `-unormal`: git collapses a wholly untracked
    # directory to a single `?? .codex/` entry, which hides every filename
    # inside it from `_protected_path` — so an untracked credential file was
    # invisible to the guard and `add -A` staged it anyway. Listing untracked
    # files individually is what makes the filename check reachable.
    rc, status_out, _ = await _git(workspace, "status", "--porcelain", "-z", "-uall")
    has_changes = rc == 0 and bool(status_out.strip())

    if has_changes:
        changed_paths = _porcelain_paths(status_out)
        protected = sorted(path for path in changed_paths if _protected_path(path))
        if protected:
            detail = ", ".join(protected)
            logger.warning("Startup sync: refusing to stage protected files: %s", detail)
            return f"Startup sync: refusing to auto-commit protected files: {detail}"
        await _git(workspace, "add", "-A")
        rc, out, err = await _git(
            workspace, "commit", "-m", "auto-commit before startup sync",
        )
        if rc != 0:
            # git commit reports many failures on stdout ("nothing added to
            # commit", hook output), so include both streams.
            detail = "\n".join(part for part in (err.strip(), out.strip()) if part)
            logger.warning("Startup sync: auto-commit failed: %s", detail or f"exit {rc}")
            return f"Startup sync: failed to auto-commit local changes.\n{detail}"

    # A fresh branch may not have an upstream yet (the backup-push loop sets
    # one with ``push -u`` once it succeeds). A bare ``git pull`` then
    # hard-fails with "no tracking information"; skip it rather than surface
    # that as a startup error.
    rc, _, _ = await _git(workspace, "rev-parse", "--abbrev-ref", "@{u}")
    if rc != 0:
        logger.info("Startup sync: branch has no upstream yet; skipping pull.")
        return None

    # Pull (merge-based, handles merge commits cleanly).
    rc, pull_out, pull_err = await _git(workspace, "pull", timeout=GIT_STARTUP_TIMEOUT)

    if rc != 0:
        logger.warning("Startup sync: pull failed: %s", pull_err)
        return f"Startup sync: pull failed.\n{pull_err}"

    # Only notify if new commits were pulled
    if "Already up to date" in pull_out:
        logger.info("Startup sync: already up to date.")
        return None

    logger.info("Startup sync: %s", pull_out)
    return f"Startup sync: pulled latest changes.\n{pull_out}"
