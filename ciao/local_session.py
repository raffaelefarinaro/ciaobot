"""Per-device working-branch flow with direct or agent-merged handover.

Every Ciaobot instance works on its own ``dev/<device_name>`` branch, cut from
``origin/main`` and reused across restarts (unmerged work is preserved). When
the user clicks "commit to main" in Settings, Ciaobot commits + pushes the branch,
then tries to merge it into ``main``:

- clean merge -> pushed to ``main`` directly (plain git), and the device branch
  is re-pointed at the merged ``main`` so work continues there;
- conflicting merge -> the merge is aborted and an interactive Claude Code chat
  is opened in Ciaobot to resolve it (see ``MERGE_PROMPT``), so questions
  surface with push notifications and the user answers in that chat.

The git helpers here are unit-tested; the conflict merge runs as a normal PWA
chat dispatched from the route layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BASE = "origin/main"
BRANCH_PREFIX = "dev"
BACKUP_PUSH_INTERVAL = 30  # seconds between background backup pushes


def device_branch_name(device_name: str) -> str:
    """The branch a device works on, e.g. ``dev/laptop``."""
    return f"{BRANCH_PREFIX}/{device_name}"


# The merge prompt dispatched into a chat when an automatic merge conflicts.
# Filled with the branch via str.format / replace.
MERGE_PROMPT = """\
A local dev session on branch `{branch}` could not be merged into `main`
automatically (merge conflict). Merge it for me, here, in this chat.

Steps:
1. `git fetch origin`, then `git checkout main` and `git pull --no-rebase`.
2. Merge the branch: `git merge --no-ff origin/{branch}`.
3. Resolve conflicts with judgement, NOT blindly:
   - `memory-vault/**`: keep BOTH sides' content (union the notes; never drop entries).
   - `.runtime/schedules.json`: union the schedule entries.
   - If a conflict is ambiguous or risky (you might drop real work), STOP and ask me with
     AskUserQuestion before deciding.
4. Commit the merge and `git push` `main`.
5. Re-point this device's branch at the merged main so work continues there:
   `git checkout {branch}` then `git merge --no-edit origin/main` (this
   fast-forwards now that the branch's work is in main). Do this BEFORE step 6.
6. Do NOT restart or redeploy the service. Workspace merges do not deploy app code;
   app upgrades happen through the package install/upgrade path.

Report what you merged and any decisions you made.
"""


MERGE_PROMPT_MAIN = """\
A git conflict occurred on the `main` branch of this workspace during remote synchronization.
Please resolve the conflicts for me, here, in this chat.

Steps:
1. Identify the conflicting files via `git status`.
2. Inspect the conflict markers and resolve them with judgment:
   - `memory-vault/**`: keep BOTH sides' content (union the notes; never drop entries).
   - `.runtime/schedules.json`: union the schedule entries.
   - If a conflict is ambiguous or risky (you might drop real work), STOP and ask me with
     AskUserQuestion before deciding.
3. Stage the resolved files: `git add <file>`.
4. Commit the resolved changes: `git commit -m "resolve conflicts on main"`.
5. Push the branch: `git push origin main`.
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

    r = subprocess.run(
        ["git", *args], cwd=str(workspace), capture_output=True, text=True
    )
    return r.returncode, (r.stdout.strip() or r.stderr.strip())


def current_branch(workspace: Path) -> str:
    _, out = _git_sync(Path(workspace), "rev-parse", "--abbrev-ref", "HEAD")
    return out


# ── branch lifecycle ───────────────────────────────────────────────────────


async def ensure_device_branch(
    workspace: Path, *, device_name: str, base: str = DEFAULT_BASE, direct_main: bool = False
) -> str:
    """Make sure the checkout is on this device's ``dev/<device>`` branch or ``main``.

    Fetches, then:
    - if direct_main is True, checkout main if not already there;
    - if already on the branch -> keep it (preserve unmerged work across
      restarts; never reset);
    - else -> create it from ``base`` (origin/main) and check it out.

    Returns the branch name.
    """
    if direct_main:
        await _git(workspace, "fetch", "origin", timeout=10.0)
        if current_branch(workspace) != "main":
            rc, _, err = await _git(workspace, "checkout", "main")
            if rc != 0:
                raise RuntimeError(f"could not checkout main: {err}")
        return "main"

    name = device_branch_name(device_name)
    await _git(workspace, "fetch", "origin", timeout=10.0)
    if current_branch(workspace) == name:
        return name
    rc, _, err = await _git(workspace, "checkout", "-B", name, base)
    if rc != 0:
        # base may not exist yet (fresh repo); fall back to a plain branch.
        rc2, _, err2 = await _git(workspace, "checkout", "-B", name)
        if rc2 != 0:
            raise RuntimeError(f"could not create branch {name}: {err or err2}")
    return name


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


async def try_merge_to_main(workspace: Path, *, branch: str) -> dict:
    """Commit + push the branch, then try to merge it into ``main``.

    Returns one of:
      {"ok": True, "merged": True, "deploy_needed": bool, "pushed": bool, "detail": str}
      {"ok": True, "merged": False, "conflict": True, "branch": branch}
      {"ok": False, "step": str, "error": str}
    """
    if branch == "main":
        await commit_pending(workspace, branch="main")
        await _git(workspace, "fetch", "origin", timeout=10.0)
        rc_pull, pull_out, pull_err = await _git(workspace, "pull", "--no-rebase", timeout=10.0)
        if rc_pull != 0:
            return {"ok": True, "merged": False, "conflict": True, "branch": "main"}
        rc, out, err = await _git(workspace, "push", "origin", "main", timeout=10.0)
        if rc != 0:
            return {"ok": False, "step": "push main", "error": err or out}
        return {
            "ok": True,
            "merged": True,
            "deploy_needed": False,
            "pushed": True,
            "detail": out or "pushed",
        }

    await commit_pending(workspace, branch=branch)
    ok, detail = await push_branch(workspace, branch=branch)
    if not ok:
        return {"ok": False, "step": "push", "error": detail}

    await _git(workspace, "fetch", "origin", timeout=10.0)
    rc, _, err = await _git(workspace, "checkout", "main")
    if rc != 0:
        # No local main yet -> create it tracking origin/main.
        rc, _, err = await _git(workspace, "checkout", "-B", "main", "origin/main")
        if rc != 0:
            await _git(workspace, "checkout", branch)
            return {"ok": False, "step": "checkout main", "error": err}

    # Ensure local main tracks origin/main if it exists.
    rc_track, _, _ = await _git(workspace, "rev-parse", "--abbrev-ref", "main@{u}")
    if rc_track != 0:
        rc_ref, _, _ = await _git(workspace, "rev-parse", "--verify", "origin/main")
        if rc_ref == 0:
            await _git(workspace, "branch", "--set-upstream-to=origin/main", "main")

    rc_pull, pull_out, pull_err = await _git(workspace, "pull", "--no-rebase", timeout=10.0)
    if rc_pull != 0:
        await _git(workspace, "merge", "--abort")
        await _git(workspace, "checkout", branch)
        return {"ok": False, "step": "pull main", "error": pull_err or pull_out}

    _, before, _ = await _git(workspace, "rev-parse", "HEAD")

    rc, _out, _err = await _git(workspace, "merge", "--no-ff", "--no-edit", branch)
    if rc != 0:
        # Conflict (or other merge failure) -> abort and hand back to a chat.
        await _git(workspace, "merge", "--abort")
        await _git(workspace, "checkout", branch)
        return {"ok": True, "merged": False, "conflict": True, "branch": branch}

    # Clean merge: push main, then re-point the device branch at the merged
    # main so work continues. Workspace merges never imply an app deploy: after
    # the public-package split, app code lives in the installed package, not in
    # the workspace repo.
    deploy_needed = False
    rc, out, err = await _git(workspace, "push", "origin", "main", timeout=10.0)
    if rc != 0:
        # Push failed: revert the local merge to keep local main clean.
        await _git(workspace, "reset", "--hard", before)
        await _git(workspace, "checkout", branch)
        return {"ok": False, "step": "push main", "error": err or out}

    await _git(workspace, "checkout", "-B", branch, "HEAD")
    return {
        "ok": True,
        "merged": True,
        "deploy_needed": deploy_needed,
        "pushed": True,
        "detail": out or "pushed",
    }


async def resync_to_main(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Bring the device branch up to the latest ``main`` without losing work.

    Used after the conflict-resolution chat has merged and pushed ``main`` (the
    device's own commits are already there, so this fast-forwards) and by the
    Settings "Sync to main" button.

    It *merges* ``origin/main`` into the branch rather than resetting the branch
    to it. That matters for the live PWA workspace:

    - the working tree is almost always dirty (memory-vault notes, snapshots),
      and a ``checkout -B branch origin/main`` would abort rather than clobber
      tracked changes, silently leaving the branch behind;
    - the device may hold commits not yet on main (e.g. a snapshot taken after
      the last handback push); a force re-point would discard them.

    Committing pending work first, then merging, keeps both safe: the normal
    post-handback case fast-forwards cleanly, and any genuinely new local work
    is preserved.
    """
    if branch == "main":
        rc, _, err = await _git(workspace, "fetch", "origin", timeout=10.0)
        if rc != 0:
            return False, f"fetch failed: {err}"
        await commit_pending(workspace, branch="main")
        rc, out, err = await _git(workspace, "merge", "--no-edit", "origin/main")
        if rc != 0:
            await _git(workspace, "merge", "--abort")
            return False, f"resync hit conflict on main: {err or out}"
        return True, "resynced"

    rc, _, err = await _git(workspace, "fetch", "origin", timeout=10.0)
    if rc != 0:
        return False, f"fetch failed: {err}"
    # Get onto the device branch. The merge chat leaves the checkout on ``main``;
    # if the branch is gone entirely, recreate it from the merged ``origin/main``.
    if current_branch(workspace) != branch:
        rc, _, err = await _git(workspace, "checkout", branch)
        if rc != 0:
            rc, _, err = await _git(workspace, "checkout", "-B", branch, "origin/main")
            if rc != 0:
                return False, f"resync {branch} failed: {err}"
            return True, "resynced"
    # Commit any dirty state so the merge has a clean tree and nothing is lost.
    await commit_pending(workspace, branch=branch)
    rc, out, err = await _git(workspace, "merge", "--no-edit", "origin/main")
    if rc != 0:
        await _git(workspace, "merge", "--abort")
        return (
            False,
            f"resync hit a conflict merging origin/main into {branch}; "
            f"resolve it via Commit to main: {err or out}",
        )
    return True, "resynced"


# ── manager ──────────────────────────────────────────────────────────────────


class LocalSessionManager:
    """Wires the branch helpers for the /api/local routes.

    One per process; every instance has one (no primary/secondary split).
    """

    def __init__(self, *, workspace: Path, runtime_root: Path, device_name: str, direct_main: bool = False, dev_mode: bool = False) -> None:
        self.workspace = Path(workspace)
        self.device_name = device_name
        self.direct_main = direct_main
        self.dev_mode = dev_mode
        self.branch = "main" if direct_main else device_branch_name(device_name)

    def status(self) -> dict:
        br = current_branch(self.workspace)
        _, dirty = _git_sync(self.workspace, "status", "--porcelain")
        return {
            "device_name": self.device_name,
            "device_branch": self.branch,
            "branch": br,
            "on_device_branch": br == self.branch,
            "dirty": bool(dirty.strip()),
            "direct_main": self.direct_main,
            "dev_mode": self.dev_mode,
        }

    async def commit_to_main(self) -> dict:
        """Commit the session and try to land it on ``main``."""
        return await try_merge_to_main(self.workspace, branch=self.branch)

    async def resync(self) -> dict:
        ok, detail = await resync_to_main(self.workspace, branch=self.branch)
        return {"ok": ok, "detail": detail}

    async def preflight(self) -> dict:
        """Run a git preflight check for dirty changes, file categories, and secrets."""
        br = current_branch(self.workspace)
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
        warnings = []
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
