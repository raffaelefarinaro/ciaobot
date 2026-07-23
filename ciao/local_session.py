"""Current-branch git sync flow for the workspace repo.

Ciaobot never creates or switches local branches: it works on whatever branch
the workspace checkout is currently on. When the user clicks "Sync with
Remote" in Settings, Ciaobot commits pending work, pulls from origin
(merge-based), and pushes the branch back:

- clean pull -> pushed to origin directly (plain git);
- conflicting pull -> an interactive Claude Code chat is opened in Ciaobot to
  resolve it (see ``MERGE_PROMPT``), so questions surface with push
  notifications and the user answers in that chat.

Workspaces that are not git repositories (or have no ``origin`` remote) skip
all of this gracefully. The git helpers here are unit-tested; the conflict
resolution runs as a normal PWA chat dispatched from the route layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_PUSH_INTERVAL = 30  # seconds between background backup pushes


# The prompt dispatched into a chat when an automatic pull/merge conflicts.
# Filled with the branch via str.replace.
MERGE_PROMPT = """\
A git conflict occurred on branch `{branch}` of this workspace during remote synchronization.
Please resolve the conflicts for me, here, in this chat.

Steps:
1. Identify the conflicting files via `git status`.
2. Inspect the conflict markers and resolve them with judgment:
   - `memory-vault/**`: keep BOTH sides' content (union the notes; never drop entries).
   - `.runtime/schedules.json`: union the schedule entries.
   - If a conflict is ambiguous or risky (you might drop real work), STOP and ask me with
     AskUserQuestion before deciding.
3. Stage the resolved files: `git add <file>`.
4. Commit the resolved changes: `git commit -m "resolve sync conflicts"`.
5. Push the branch: `git push origin {branch}`.
6. Do NOT restart or redeploy the service.

Report what you resolved and any decisions you made.
"""


async def _git(workspace: Path, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    if timeout is not None:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (-1, "", "git command timed out")
    else:
        out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


def _git_sync(workspace: Path, *args: str) -> tuple[int, str]:
    """Synchronous git for the quick read helpers (branch name, etc.)."""
    import subprocess

    try:
        r = subprocess.run(
            ["git", *args], cwd=str(workspace), capture_output=True, text=True
        )
    except OSError as exc:
        return 1, str(exc)
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def is_git_repo(workspace: Path) -> bool:
    """True when ``workspace`` is inside a git work tree."""
    rc, _ = _git_sync(Path(workspace), "rev-parse", "--git-dir")
    return rc == 0


def workspace_branch(workspace: Path) -> str | None:
    """The branch the workspace checkout is on.

    Returns ``None`` when the workspace is not a git repository or the
    checkout is on a detached HEAD.
    """
    rc, out = _git_sync(Path(workspace), "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or out == "HEAD":
        return None
    return out


def has_origin_remote(workspace: Path) -> bool:
    """True when the workspace repo has an ``origin`` remote configured."""
    rc, _ = _git_sync(Path(workspace), "remote", "get-url", "origin")
    return rc == 0


def repo_toplevel(path: Path) -> Path | None:
    """Root of the git work tree containing ``path``, or None outside git."""
    rc, out = _git_sync(Path(path), "rev-parse", "--show-toplevel")
    if rc != 0 or not out:
        return None
    return Path(out)


def sync_root(config) -> Path:
    """The repo root that git sync and branch backup should operate on.

    Sync targets the repo containing the vault root: with the default layout
    (vault inside the workspace repo) that resolves to the workspace root,
    while a vault living elsewhere in its own repo is synced there. A missing
    or non-git vault falls back to the workspace root.
    """
    vault = getattr(config, "vault_root", None)
    if vault is not None:
        vault = Path(vault)
        if vault.is_dir():
            toplevel = repo_toplevel(vault)
            if toplevel is not None:
                return toplevel
    return Path(config.workspace_root)


# ── sync flow ────────────────────────────────────────────────────────────────


async def push_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Push the working branch for backup (sets upstream)."""
    rc, out, err = await _git(workspace, "push", "-u", "origin", branch, timeout=10.0)
    if rc != 0:
        return False, err or out
    return True, out or "pushed"


async def commit_pending(workspace: Path, *, branch: str) -> bool:
    """Stage and commit any dirty working-tree state. Returns True if it
    created a commit, False if the tree was already clean."""
    await _git(workspace, "add", "-A")
    _, status, _ = await _git(workspace, "status", "--porcelain")
    if not status.strip():
        return False
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    await _git(workspace, "commit", "-m", f"{branch} session commit {ts}")
    return True


async def sync_branch(workspace: Path, *, branch: str) -> dict:
    """Commit pending work, pull from origin, and push the current branch.

    Never creates or switches branches. Returns one of:
      {"ok": True, "merged": True, "deploy_needed": False, "pushed": True, "detail": str}
      {"ok": True, "merged": False, "conflict": True, "branch": branch}
      {"ok": False, "step": str, "error": str}

    A conflicting pull is left in place (conflict markers in the tree) so the
    conflict chat dispatched by the route layer can resolve it.
    """
    await commit_pending(workspace, branch=branch)
    await _git(workspace, "fetch", "origin", timeout=10.0)
    # Pull only when the branch already exists on origin; a fresh branch has
    # nothing to merge and a bare pull would fail on missing upstream.
    rc_ref, _, _ = await _git(workspace, "rev-parse", "--verify", f"origin/{branch}")
    if rc_ref == 0:
        rc_pull, _, _ = await _git(
            workspace, "pull", "--no-rebase", "origin", branch, timeout=10.0
        )
        if rc_pull != 0:
            return {"ok": True, "merged": False, "conflict": True, "branch": branch}
    ok, detail = await push_branch(workspace, branch=branch)
    if not ok:
        return {"ok": False, "step": "push", "error": detail}
    return {
        "ok": True,
        "merged": True,
        "deploy_needed": False,
        "pushed": True,
        "detail": detail,
    }


async def resync_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Bring the current branch up to its origin counterpart without losing work.

    Used after the conflict-resolution chat has pushed the branch, and by the
    Settings sync flow. Commits pending work first (the live PWA workspace is
    almost always dirty), then *merges* ``origin/<branch>`` rather than
    resetting, so local commits are never discarded.
    """
    rc, _, err = await _git(workspace, "fetch", "origin", timeout=10.0)
    if rc != 0:
        return False, f"fetch failed: {err}"
    await commit_pending(workspace, branch=branch)
    rc_ref, _, _ = await _git(workspace, "rev-parse", "--verify", f"origin/{branch}")
    if rc_ref != 0:
        return True, "no remote branch to sync from"
    rc, out, err = await _git(workspace, "merge", "--no-edit", f"origin/{branch}")
    if rc != 0:
        await _git(workspace, "merge", "--abort")
        return False, f"resync hit conflict on {branch}: {err or out}"
    return True, "resynced"


# ── manager ──────────────────────────────────────────────────────────────────


class LocalSessionManager:
    """Wires the git-sync helpers for the /api/local routes.

    One per process; every instance has one (no primary/secondary split). The
    working branch is resolved dynamically from the checkout — Ciaobot never
    creates or switches branches.
    """

    def __init__(self, *, workspace: Path, runtime_root: Path, dev_mode: bool = False) -> None:
        self.workspace = Path(workspace)
        self.dev_mode = dev_mode

    @property
    def branch(self) -> str | None:
        return workspace_branch(self.workspace)

    def status(self) -> dict:
        repo = is_git_repo(self.workspace)
        branch = workspace_branch(self.workspace) if repo else None
        dirty = False
        if repo:
            rc, out = _git_sync(self.workspace, "status", "--porcelain")
            dirty = rc == 0 and bool(out.strip())
        return {
            "git_repo": repo,
            "branch": branch,
            "dirty": dirty,
            "dev_mode": self.dev_mode,
        }

    async def commit_and_sync(self) -> dict:
        """Commit the session and sync the current branch with origin."""
        branch = workspace_branch(self.workspace)
        if branch is None:
            return {
                "ok": False,
                "step": "branch",
                "error": "workspace is not a git repository (or is on a detached HEAD)",
            }
        return await sync_branch(self.workspace, branch=branch)

    async def resync(self) -> dict:
        branch = workspace_branch(self.workspace)
        if branch is None:
            return {
                "ok": False,
                "detail": "workspace is not a git repository (or is on a detached HEAD)",
            }
        ok, detail = await resync_branch(self.workspace, branch=branch)
        return {"ok": ok, "detail": detail}

    async def preflight(self) -> dict:
        """Run a git preflight check for dirty changes, file categories, and secrets."""
        br = workspace_branch(self.workspace)
        rc, out, err = await _git(self.workspace, "status", "--porcelain")
        if rc != 0:
            return {
                "branch": br,
                "dirty": False,
                "changed_files": {"code": [], "vault": [], "scripts": [], "config": [], "other": []},
                "deploy_needed": False,
                "blockers": [f"git status failed: {err or out}"],
                "warnings": [],
            }

        # Parse dirty files
        raw_files = set()
        for line in out.splitlines():
            if not line:
                continue
            status_prefix = line[:2]
            file_part = line[3:].strip()
            if " -> " in file_part:
                parts = file_part.split(" -> ")
                file_part = parts[-1].strip()
            if file_part.startswith('"') and file_part.endswith('"'):
                file_part = file_part[1:-1]
            if 'D' in status_prefix:
                continue
            raw_files.add(file_part)

        # Expand untracked directories
        changed_paths = []
        for f in raw_files:
            p = self.workspace / f
            if p.is_dir():
                for dirpath, _, filenames in os.walk(p):
                    for fname in filenames:
                        changed_paths.append(Path(dirpath) / fname)
            elif p.is_file():
                changed_paths.append(p)

        blockers = []
        warnings = []

        categories: dict[str, list[str]] = {
            "code": [],
            "vault": [],
            "scripts": [],
            "config": [],
            "other": [],
        }

        for p in changed_paths:
            try:
                rel_path = str(p.relative_to(self.workspace))
            except ValueError:
                continue

            # Categorize
            if rel_path.startswith("ciao/") or (rel_path.startswith("web/") and not rel_path.startswith(("web/package", "web/tsconfig", "web/vite.config"))):
                categories["code"].append(rel_path)
            elif rel_path.startswith("memory-vault/"):
                categories["vault"].append(rel_path)
            elif rel_path.startswith("scripts/"):
                categories["scripts"].append(rel_path)
            elif rel_path in (".env", "pyproject.toml", "package.json", "package-lock.json", "skills/skills-lock.json", ".gitignore") or rel_path.startswith(("secrets/", "web/package", "web/tsconfig", "web/vite.config")):
                categories["config"].append(rel_path)
            else:
                categories["other"].append(rel_path)

            # Scan for secrets (skip test files to prevent mock test keys/certs from blocking commits)
            if not rel_path.startswith("tests/"):
                file_blockers, file_warnings = self._scan_file_for_secrets(p)
                blockers.extend(file_blockers)
                warnings.extend(file_warnings)

        return {
            "branch": br,
            "dirty": len(changed_paths) > 0,
            "changed_files": categories,
            "deploy_needed": False,
            "blockers": blockers,
            "warnings": warnings,
        }

    def _scan_file_for_secrets(self, p: Path) -> tuple[list[str], list[str]]:
        blockers = []
        warnings: list[str] = []
        name = p.name.lower()

        # Block env-style files
        if name.startswith(".env") or name.endswith(".env"):
            blockers.append(f"Blocked file '{p.name}': .env configuration files containing credentials must not be tracked.")
            return blockers, warnings

        # Block key/credential files by extension
        if name.endswith((".pem", ".key", ".p12", ".pfx")):
            blockers.append(f"Blocked file '{p.name}': Cryptographic key files must not be tracked.")
            return blockers, warnings

        try:
            if not p.is_file():
                return blockers, warnings
            size = p.stat().st_size
        except OSError:
            return blockers, warnings

        if size > 2 * 1024 * 1024:
            return blockers, warnings

        # Read contents to check for secrets
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return blockers, warnings

        # Google Cloud Service Account JSON check. The markers are built from
        # fragments so this scanner file's own source does not contain the
        # contiguous literals (otherwise it self-trips when it scans itself).
        _SA = "service" + "_account"
        _PK = "private" + "_key"
        _CE = "client" + "_email"
        if _SA in content and _PK in content and _CE in content:
            blockers.append(f"Blocked file '{p.name}': High-confidence Google Cloud Service Account credential detected.")

        # Private key check (PEM). Fragments for the same self-trigger reason.
        _BEGIN = ("-" * 5) + "BEGIN"
        _PEM_TAIL = "PRIVATE KEY" + ("-" * 5)
        if _BEGIN in content and _PEM_TAIL in content:
            blockers.append(f"Blocked file '{p.name}': High-confidence private key structure detected.")

        # OpenAI key check
        openai_keys = re.findall(r"sk-[a-zA-Z0-9-]{40,}", content)
        if openai_keys:
            blockers.append(f"Blocked file '{p.name}': High-confidence OpenAI API key detected.")

        # Slack token check
        slack_tokens = re.findall(r"xox[bapr]-[0-9]{12}-[0-9]{12}-[a-zA-Z0-9]{24}", content)
        if slack_tokens:
            blockers.append(f"Blocked file '{p.name}': High-confidence Slack API token detected.")

        # Suspicious file names (warnings)
        if name in ("config.json", "credentials.json", "settings.yaml") or "password" in name or "secret" in name:
            warnings.append(f"Suspicious file name '{p.name}' could contain configuration or credentials.")

        return blockers, warnings
