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

# Workspace roots holding user data rather than app source.
_VAULT_ROOT = "memory-vault"
_SECRETS_ROOT = "secrets"
_USER_DATA_ROOTS = (_VAULT_ROOT, _SECRETS_ROOT)

# Suffixes a test-*named* file may carry to earn the fixture exemption. A
# `test_config.json` is far more likely to be a real config someone named badly
# than a fixture, so it stays in scope for the scanner.
_TEST_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue"}


def _is_test_fixture(rel_path: str) -> bool:
    """Whether a workspace-relative path is exempt from the secret scan.

    Test fixtures may legitimately carry mock keys and certs. Two carve-outs
    keep that from becoming a hole: the exemption never applies under a
    user-data root (a `tests/` folder in someone's vault is their notes, not
    pytest), and a merely test-*named* file has to be source to qualify —
    `memory-vault/.../tests/credentials.json` and a stray `test_config.json`
    are real credentials.
    """
    parts = Path(rel_path).parts
    if not parts or parts[0] in _USER_DATA_ROOTS:
        return False
    if "tests" in parts or "__tests__" in parts:
        return True
    name = parts[-1]
    return (name.startswith("test_") or name.endswith("_test.py")) and (
        Path(name).suffix in _TEST_SOURCE_SUFFIXES
    )


def _is_nested_git_checkout(path: Path, workspace: Path) -> bool:
    """Whether ``path`` is a nested checkout that the workspace does not own.

    A workspace can contain agent/developer worktrees. Git reports a changed
    worktree as one directory entry, so expanding it here would scan its
    virtualenvs, caches, and generated files as if they were workspace files.
    Those files are governed by the nested checkout's own Git metadata and
    must not participate in the workspace secret preflight.
    """
    return path != workspace and (path / ".git").exists()


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


_DIVERGED_BACKUP_MARKER = "[diverged-backup] "


def is_diverged_backup(detail: str) -> bool:
    """True when a ``push_branch`` *success* detail is a diverged-backup fallback.

    Set when ``<branch>`` and ``origin/<branch>`` have diverged with a real
    merge conflict: ``push_branch`` aborts the merge and pushes the current
    commit to ``backup/<branch>-<sha>`` instead of returning a bare error
    (issue #187). The branch-backup loop checks this to surface the backup
    ref and back off, instead of retrying a merge that will conflict the
    same way every 30 seconds.
    """
    return (detail or "").startswith(_DIVERGED_BACKUP_MARKER)


def backup_ref_name(branch: str, short_sha: str) -> str:
    """The remote ref name a diverged commit gets backed up to.

    ``backup/<branch>-<short_sha>``: derived from the HEAD sha, so repeated
    pushes of the same commit target an existing ref (a no-op) and new commits
    get new refs. This keeps the backup-ref pile bounded by the number of
    distinct commits, not by the number of ticks.
    """
    return f"backup/{branch}-{short_sha}"


async def push_backup_ref(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Push the current HEAD commit to a per-commit backup ref on origin.

    Writes ``backup/<branch>-<short_sha>`` pointing at the current HEAD,
    without touching the shared ``<branch>`` ref. This is the safe recovery
    when ``<branch>`` and ``origin/<branch>`` have diverged non-linearly: it
    preserves local state off-device with no rebase, no force-push, and no
    merge. Idempotent: the ref name is derived from the HEAD sha, so repeated
    ticks for the same commit hit an existing ref (a no-op push) and only new
    commits create new refs.
    """
    rc_s, short_out, short_err = await _git(
        workspace, "rev-parse", "--short=12", "HEAD"
    )
    short = (short_out or short_err).strip()
    if rc_s != 0 or not short:
        return False, f"could not resolve HEAD short sha for backup ref: {short_err or short_out}"
    rc_f, full_out, full_err = await _git(workspace, "rev-parse", "HEAD")
    full = (full_out or full_err).strip()
    if rc_f != 0 or not full:
        return False, f"could not resolve HEAD sha for backup ref: {full_err or full_out}"
    ref = backup_ref_name(branch, short)
    rc, out, err = await _git(
        workspace, "push", "origin", f"{full}:refs/heads/{ref}", timeout=10.0
    )
    if rc != 0:
        return False, err or out
    return True, f"backed up to origin/{ref}"


async def push_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Push the working branch for backup (sets upstream).

    On a non-fast-forward rejection, fetches and merges ``origin/<branch>``
    then retries. When that merge hits a real conflict, aborts it — verifying
    the abort actually succeeded, so a mid-merge working tree is never left
    behind — and falls back to pushing the current commit to a per-commit
    ``backup/<branch>-<sha>`` ref rather than returning a bare error (see
    :func:`is_diverged_backup`). This is deliberately not a rebase or a
    force-push: the shared branch is left exactly as diverged as it was, and
    a human resolves it later; the fallback only guarantees local state made
    it off-device.
    """
    rc, out, err = await _git(workspace, "push", "-u", "origin", branch, timeout=10.0)
    if rc != 0:
        detail = err or out
        nff_markers = (
            "non-fast-forward",
            "behind its remote counterpart",
            "fetch first",
            "updates were rejected",
            "[rejected]",
        )
        if any(marker in detail.lower() for marker in nff_markers):
            logger.info(
                "Push rejected (non-fast-forward) for branch '%s'; attempting auto-merge with origin/%s",
                branch,
                branch,
            )
            await _git(workspace, "fetch", "origin", timeout=10.0)
            rc_m, out_m, err_m = await _git(
                workspace, "merge", "--no-edit", f"origin/{branch}"
            )
            if rc_m != 0:
                conflict_detail = err_m or out_m
                rc_abort, out_abort, err_abort = await _git(workspace, "merge", "--abort")
                if rc_abort != 0:
                    # Can't confirm the working tree came back clean — don't
                    # risk pushing anything from a repo that may still be
                    # mid-merge.
                    return (
                        False,
                        f"Push rejected (non-fast-forward) and auto-merge hit "
                        f"conflict on origin/{branch}: {conflict_detail}; "
                        f"merge --abort also failed ({err_abort or out_abort}) "
                        f"— working tree may still be mid-merge",
                    )
                bok, bdetail = await push_backup_ref(workspace, branch=branch)
                if bok:
                    logger.warning(
                        "Branch '%s' diverged from origin/%s with a real merge "
                        "conflict; %s",
                        branch, branch, bdetail,
                    )
                    return (
                        True,
                        f"{_DIVERGED_BACKUP_MARKER}branch '{branch}' diverged "
                        f"from origin/{branch} (non-fast-forward merge "
                        f"conflict): {conflict_detail}; {bdetail}",
                    )
                return (
                    False,
                    f"Push rejected (non-fast-forward) and auto-merge hit "
                    f"conflict on origin/{branch}: {conflict_detail}; "
                    f"backup-ref fallback also failed: {bdetail}",
                )
            rc2, out2, err2 = await _git(
                workspace, "push", "-u", "origin", branch, timeout=10.0
            )
            if rc2 != 0:
                return False, err2 or out2
            return True, out2 or "pushed after merging origin"
        return False, detail
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
                if _is_nested_git_checkout(p, self.workspace):
                    continue
                for dirpath, dirnames, filenames in os.walk(p):
                    current = Path(dirpath)
                    dirnames[:] = [
                        dirname
                        for dirname in dirnames
                        if not _is_nested_git_checkout(current / dirname, self.workspace)
                    ]
                    for fname in filenames:
                        changed_paths.append(current / fname)
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
            elif rel_path.startswith(f"{_VAULT_ROOT}/"):
                categories["vault"].append(rel_path)
            elif rel_path.startswith("scripts/"):
                categories["scripts"].append(rel_path)
            elif rel_path in (".env", "pyproject.toml", "package.json", "package-lock.json", ".gitignore") or rel_path.startswith((f"{_SECRETS_ROOT}/", "web/package", "web/tsconfig", "web/vite.config")):
                categories["config"].append(rel_path)
            else:
                categories["other"].append(rel_path)

            if not _is_test_fixture(rel_path):
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

        # Block env-style files (except template/example files)
        if (name.startswith(".env") or name.endswith(".env")) and not name.startswith((".env.example", ".env.sample", ".env.template", ".env.schema")):
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

        # Suspicious file names (warnings). The trailing boundary is what keeps
        # `secretary.md` quiet; there is deliberately no leading boundary, so
        # `mysecrets.txt` and `dbpassword.json` still warn.
        if name in ("config.json", "credentials.json", "settings.yaml") or re.search(r"(secrets?|passwords?)(?:[\W_]|$)", name):
            warnings.append(f"Suspicious file name '{p.name}' could contain configuration or credentials.")

        return blockers, warnings
