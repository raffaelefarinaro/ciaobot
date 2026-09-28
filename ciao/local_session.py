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

Every public mutation below — ``commit_pending``, ``push_branch``,
``push_backup_ref``, ``sync_branch``, ``resync_branch``, and the manager
methods that drive them — runs inside ``ciao.git_mutation.repository_mutation``,
so two Ciaobot operations never interleave git commands in one checkout (#674).
Two consequences run through the code below:

- No stage/status/commit/fetch failure is reported as success. The old
  ``commit_pending`` ignored every return code and returned True, so a failed
  commit read as a committed session and sync carried on to push; it now raises
  :class:`GitOperationError` and the flows that call it convert that into the
  envelope they already return.
- A preexisting ``index.lock`` or an in-progress merge/rebase is refused up
  front (``ensure_mutable``) instead of raced, and never deleted — that file
  may belong to a real git operation elsewhere on a shared volume. The refusal
  is an ordinary failure result in each flow's own shape, never an exception
  escaping to a route.

An operation that *this* code started may leave its own merge state alone:
the preflight is checked at the outer entry, before the body runs.

Two of the operations below exist for the unattended path only: ``commit_scoped``
stages an explicit list of paths instead of the whole tree, and
``preflight_scoped`` scans only what that list may contain. The scope itself
lives in :mod:`ciao.backup_scope`. ``commit_pending`` — the manual "stage
everything" the user asked for — is untouched, and so is
``LocalSessionManager.preflight``, which still reports on the whole tree.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from ciao import backup_scope
from ciao.git_mutation import RepositoryBusyError, ensure_mutable, repository_mutation
from ciao.git_proc import GIT_TIMEOUT_DETAIL, run_git, run_git_sync

logger = logging.getLogger(__name__)

BACKUP_PUSH_INTERVAL = 30  # seconds between background backup pushes

# Network git operations (push/fetch/pull) get a generous ceiling. 10s proved
# too tight: a momentary network stall (e.g. DNS resolution over flaky Wi-Fi,
# 2026-09-05 02:26) kills the push with "git command timed out". The next
# 30s tick self-heals, but each false error row lands in the triage report.
# 60s bounds the wait without letting a hung remote stall the loop.
GIT_NETWORK_TIMEOUT = 60.0

# Workspace roots holding user data rather than app source.
_VAULT_ROOT = "memory-vault"
_SECRETS_ROOT = "secrets"
_USER_DATA_ROOTS = (_VAULT_ROOT, _SECRETS_ROOT)

# Suffixes a test-*named* file may carry to earn the fixture exemption. A
# `test_config.json` is far more likely to be a real config someone named badly
# than a fixture, so it stays in scope for the scanner.
_TEST_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue"}

#: ``step`` reported when the repository is not ours to mutate — a preexisting
#: ``index.lock`` or an in-progress merge/rebase. Distinct from a failing git
#: step so a caller can tell "someone else is working here" from "our command
#: failed".
PREFLIGHT_STEP = "preflight"


class GitOperationError(RuntimeError):
    """A git step a caller was entitled to expect to succeed did not.

    ``step`` is the failing step (``add``, ``status``, ``commit``,
    ``preflight``) and ``detail`` is git's own stderr, so a caller can report
    which step failed without re-running anything. Raised by
    :func:`commit_pending` — the one public mutation whose contract is "return
    whether a commit was created", which leaves a bool with nowhere to put a
    failure. Every other public mutation keeps its existing return shape and
    converts this into it.
    """

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _busy_envelope(exc: RepositoryBusyError) -> dict:
    """A preflight refusal in the shape ``sync_branch`` always returned."""
    return {"ok": False, "step": PREFLIGHT_STEP, "error": exc.detail}


def _is_test_fixture(rel_path: str) -> bool:
    """Whether a workspace-relative path is exempt from the secret scan.

    Test fixtures may legitimately carry mock keys and certs. Two carve-outs
    keep that from becoming a hole: the exemption never applies under a
    user-data root (a `tests/` folder in someone's vault is their notes, not
    pytest), and a merely test-*named* file has to be source to qualify —
    `memory-vault/.../tests/credentials.json` and a stray `test_config.json`
    are real credentials.

    The user-data root is matched at ANY depth, not only as the first segment:
    after the per-workspace re-rooting a vault is
    ``<install>/<workspace>/memory-vault``, so a first-segment test would hand
    the exemption to every ``<workspace>/memory-vault/tests/`` folder — a real
    gap, since that is where the notes now live.
    """
    parts = Path(rel_path).parts
    if not parts or any(part in _USER_DATA_ROOTS for part in parts):
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
    rc, out, err = await run_git(workspace, *args, timeout=timeout)
    return (rc, out.strip(), err.strip())


# Failure details that will not self-heal at the normal backup cadence.
# Credentials cannot be re-entered (there is no TTY to prompt under launchd),
# and an unreachable remote answers no faster for being asked every 30s.
_AUTH_MARKERS = (
    "could not read username",
    "authentication failed",
    "invalid username or token",
    "permission denied (publickey",
)


def backoff_reason(detail: str) -> str | None:
    """Why a failed backup push should drop to the slow cadence, or None.

    Returns ``"auth"`` for a credential failure and ``"unreachable"`` for a
    timeout. Before issue #470 only the auth markers were recognised, so a
    timeout — the shape an unreachable remote takes — never engaged the
    backoff and got retried every 30 seconds indefinitely.
    """
    lowered = (detail or "").lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return "auth"
    if GIT_TIMEOUT_DETAIL in lowered:
        return "unreachable"
    return None


def _git_sync(workspace: Path, *args: str) -> tuple[int, str]:
    """Synchronous git for the quick read helpers (branch name, etc.)."""
    rc, out, err = run_git_sync(Path(workspace), *args)
    return rc, (out.strip() or err.strip())


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


async def _push_backup_ref(workspace: Path, *, branch: str) -> tuple[bool, str]:
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
        workspace, "push", "origin", f"{full}:refs/heads/{ref}", timeout=GIT_NETWORK_TIMEOUT
    )
    if rc != 0:
        return False, err or out
    return True, f"backed up to origin/{ref}"


async def push_backup_ref(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Serialized entry to the per-commit backup-ref push.

    Same behaviour as :func:`_push_backup_ref`, with the repository mutation
    lock held and a foreign git operation refused rather than raced.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            return False, exc.detail
        return await _push_backup_ref(workspace, branch=branch)


async def _push_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Body of :func:`push_branch`; assumes the mutation lock is held.

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
    rc, out, err = await _git(workspace, "push", "-u", "origin", branch, timeout=GIT_NETWORK_TIMEOUT)
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
            await _git(workspace, "fetch", "origin", timeout=GIT_NETWORK_TIMEOUT)
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
                bok, bdetail = await _push_backup_ref(workspace, branch=branch)
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
                workspace, "push", "-u", "origin", branch, timeout=GIT_NETWORK_TIMEOUT
            )
            if rc2 != 0:
                return False, err2 or out2
            return True, out2 or "pushed after merging origin"
        return False, detail
    return True, out or "pushed"


async def push_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Push the working branch for backup, serialized against other mutations.

    Takes this repository's mutation lock, so a manual sync, a resync, and the
    background backup loop can never interleave git operations in one checkout.
    A foreign git operation (preexisting ``index.lock``, an in-progress
    merge/rebase) is refused here rather than raced.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            return False, exc.detail
        return await _push_branch(workspace, branch=branch)


async def _commit_pending(workspace: Path, *, branch: str) -> bool:
    """Body of :func:`commit_pending`; assumes the mutation lock is held.

    Staging is still ``git add -A`` (the manual-sync semantics this function
    has always had). Whether anything is pending is decided by
    ``git diff --cached --quiet`` rather than by ``git status --porcelain``:
    after ``add -A`` the index is the authority, and an ignored or untracked
    file that staging refused to pick up must not read as pending work and
    trigger an empty commit.
    """
    rc, out, err = await _git(workspace, "add", "-A")
    if rc != 0:
        raise GitOperationError("add", err or out)
    rc_diff, _out_diff, err_diff = await _git(workspace, "diff", "--cached", "--quiet")
    if rc_diff == 0:
        return False
    if rc_diff > 1:
        raise GitOperationError("status", err_diff or _out_diff)
    from datetime import UTC, datetime

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    rc, out, err = await _git(workspace, "commit", "-m", f"{branch} session commit {ts}")
    if rc != 0:
        raise GitOperationError("commit", err or out)
    return True


async def commit_pending(workspace: Path, *, branch: str) -> bool:
    """Stage and commit any dirty working-tree state. Returns True if it
    created a commit, False if the tree was already clean.

    Raises :class:`GitOperationError` when ``add``/``status``/``commit`` fails.
    This is the one public mutation whose return value cannot carry a failure —
    a bool has nowhere to put one — so it raises instead of reporting a commit
    that never happened. Every other public operation keeps its existing
    envelope and converts this into it.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            raise GitOperationError(PREFLIGHT_STEP, exc.detail) from exc
        return await _commit_pending(workspace, branch=branch)


# ── scoped commit (unattended backup) ────────────────────────────────────────

#: Pathspecs per ``git add`` invocation. A backup hands git every file in the
#: scope, and a long-lived vault carries tens of thousands of them, while argv
#: is bounded (``ARG_MAX``). The commit stays a single call regardless: a
#: batched commit would be several commits wearing one message.
_ADD_BATCH = 256


def _clean_relpath(raw: str) -> str | None:
    """``raw`` as a relpath safe to hand git as a pathspec, or None.

    Absolute paths and ``..`` are refused rather than normalized away: the
    point of a scoped commit is that it cannot name a file outside the tree it
    is staging, and a pathspec that resolves outside it is the one way that
    guarantee could be lost.
    """
    candidate = PurePosixPath(raw.replace(os.sep, "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    text = candidate.as_posix()
    return text if text and text != "." else None


async def _ignored_paths(workspace: Path, relpaths: Sequence[str]) -> set[str]:
    """Which of ``relpaths`` git would refuse to stage, spelled as given.

    ``git add`` rejects the *whole* batch when any single pathspec is ignored
    ("The following paths are ignored by one of your .gitignore files"), so one
    ordinary file is enough to fail a scoped commit over a batch of notes: the
    scaffolded vault ``.gitignore`` ignores ``.DS_Store``, ``git status`` never
    reports an ignored file, but ``_expand_status_paths`` walks an untracked
    directory entry (``?? notes/``) and enumerates the ignored files inside it
    (#685).

    Keyed on the printed set, never on the returncode: ``check-ignore`` exits 0
    when it printed something, 1 when it printed nothing, and 128 when it could
    not answer at all, and which of those means what has varied across git
    versions. A git that fails outright prints nothing, which leaves the
    caller's list untouched and leaves ``add`` free to refuse it exactly as it
    did before.

    Tracked-but-ignored paths are *not* printed (git omits them without
    ``--no-index``) and so stay in the batch, which is the point: a file added
    before a ``.gitignore`` line is still tracked, and asking for it stages it.
    """
    if not relpaths:
        return set()
    rc, out, err = await asyncio.to_thread(
        run_git_sync,
        Path(workspace),
        "check-ignore",
        "-z",
        "--stdin",
        stdin="\0".join(relpaths) + "\0",
    )
    if rc not in (0, 1):
        logger.info("git check-ignore failed in %s: %s", workspace, err.strip() or rc)
    return {rel for rel in out.split("\0") if rel}


async def _known_paths(workspace: Path, relpaths: Sequence[str]) -> list[str]:
    """The subset of ``relpaths`` git can be asked about, deduplicated.

    A pathspec that neither exists on disk nor is tracked makes ``git add``
    fail outright ("pathspec did not match any files"), so a caller whose list
    came from a status that has since moved on would get an error where the
    honest answer is "nothing to do". One ``ls-files`` answers the tracked
    half; the filesystem answers the rest, off the event loop. Gitignored
    pathspecs are then dropped, because ``add`` refuses a whole batch that
    contains one — see :func:`_ignored_paths`.
    """
    cleaned = [
        rel for raw in dict.fromkeys(relpaths) if (rel := _clean_relpath(str(raw)))
    ]
    if not cleaned:
        return []
    root = Path(workspace)
    rc, out, _err = await asyncio.to_thread(
        run_git_sync, root, "ls-files", "-z", "--", *cleaned
    )
    tracked = set(out.split("\0")) if rc == 0 else set()
    known = [rel for rel in cleaned if rel in tracked or (root / rel).exists()]
    ignored = await _ignored_paths(root, known)
    return [rel for rel in known if rel not in ignored]


async def _commit_scoped(workspace: Path, *, relpaths: Sequence[str], message: str) -> bool:
    """Body of :func:`commit_scoped`; assumes the mutation lock is held."""
    paths = await _known_paths(workspace, relpaths)
    if not paths:
        return False
    for start in range(0, len(paths), _ADD_BATCH):
        batch = paths[start : start + _ADD_BATCH]
        rc, out, err = await _git(workspace, "add", "-A", "--", *batch)
        if rc != 0:
            raise GitOperationError("add", err or out)
    # Scoped, not repository-wide: a change the user staged on their own must
    # not make this look like pending work and then get pulled into the commit.
    rc_diff, out_diff, err_diff = await _git(
        workspace, "diff", "--cached", "--quiet", "--", *paths
    )
    if rc_diff == 0:
        return False
    if rc_diff > 1:
        raise GitOperationError("status", err_diff or out_diff)
    # A pathspec implies --only: git builds the commit from the working tree of
    # these paths, so a deleted note is recorded as a deletion and every other
    # entry in the index — including the user's own staged file — is left alone.
    rc, out, err = await _git(workspace, "commit", "-m", message, "--", *paths)
    if rc != 0:
        raise GitOperationError("commit", err or out)
    return True


async def commit_scoped(
    workspace: Path, *, branch: str, relpaths: Sequence[str], message: str
) -> bool:
    """Commit exactly ``relpaths``, and nothing else. True if it created a
    commit, False when the scoped tree was already clean.

    The unattended counterpart to :func:`commit_pending`. Staging is
    ``git add -A -- <paths>`` over the given pathspecs, never a blanket ``-A``,
    and the commit is path-limited as well — so an unrelated change the user
    had already staged stays staged and uncommitted, a secret outside the
    scope cannot ride along, and a repository that is really a developer
    checkout gains no unattended behaviour at all. ``relpaths`` must already
    have been through :func:`ciao.backup_scope.is_eligible`; this function
    confines the commit to the paths it is handed and is not the place the
    policy lives.

    ``branch`` is verified rather than assumed. The caller is about to push
    that branch, and a commit made on a different one would be pushed by the
    next operation under a name it was never scoped for, so a mismatch is a
    :class:`GitOperationError` on step ``branch`` instead of a wrong-branch
    commit.

    Raises :class:`GitOperationError` when staging or committing fails, and
    when the checkout is not on ``branch``.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            raise GitOperationError(PREFLIGHT_STEP, exc.detail) from exc
        current = workspace_branch(workspace)
        if current != branch:
            raise GitOperationError(
                "branch",
                f"expected branch '{branch}' but the checkout is on "
                f"{current or 'a detached HEAD'}",
            )
        return await _commit_scoped(workspace, relpaths=relpaths, message=message)


async def _sync_branch(workspace: Path, *, branch: str) -> dict:
    """Body of :func:`sync_branch`; assumes the mutation lock is held."""
    try:
        await _commit_pending(workspace, branch=branch)
    except GitOperationError as exc:
        return {"ok": False, "step": exc.step, "error": exc.detail}
    rc_fetch, out_fetch, err_fetch = await _git(
        workspace, "fetch", "origin", timeout=GIT_NETWORK_TIMEOUT
    )
    if rc_fetch != 0:
        # Never pull against a fetch that did not land: the pull below would
        # merge a stale origin ref and the push would then be rejected as
        # non-fast-forward, reporting a divergence that does not exist.
        return {"ok": False, "step": "fetch", "error": err_fetch or out_fetch}
    # Pull only when the branch already exists on origin; a fresh branch has
    # nothing to merge and a bare pull would fail on missing upstream.
    rc_ref, _, _ = await _git(workspace, "rev-parse", "--verify", f"origin/{branch}")
    if rc_ref == 0:
        rc_pull, _, _ = await _git(
            workspace, "pull", "--no-rebase", "origin", branch, timeout=GIT_NETWORK_TIMEOUT
        )
        if rc_pull != 0:
            return {"ok": True, "merged": False, "conflict": True, "branch": branch}
    ok, detail = await _push_branch(workspace, branch=branch)
    if not ok:
        return {"ok": False, "step": "push", "error": detail}
    return {
        "ok": True,
        "merged": True,
        "deploy_needed": False,
        "pushed": True,
        "detail": detail,
    }


async def sync_branch(workspace: Path, *, branch: str) -> dict:
    """Commit pending work, pull from origin, and push the current branch.

    Never creates or switches branches. The whole sequence is one serialized
    mutation: nothing another Ciaobot operation (or a foreign git operation)
    can do to this checkout in between the commit and the push. Returns one of:
      {"ok": True, "merged": True, "deploy_needed": False, "pushed": True, "detail": str}
      {"ok": True, "merged": False, "conflict": True, "branch": branch}
      {"ok": False, "step": str, "error": str}

    A conflicting pull is left in place (conflict markers in the tree) so the
    conflict chat dispatched by the route layer can resolve it.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            return _busy_envelope(exc)
        return await _sync_branch(workspace, branch=branch)


async def _resync_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Body of :func:`resync_branch`; assumes the mutation lock is held."""
    rc, _, err = await _git(workspace, "fetch", "origin", timeout=GIT_NETWORK_TIMEOUT)
    if rc != 0:
        return False, f"fetch failed: {err}"
    try:
        await _commit_pending(workspace, branch=branch)
    except GitOperationError as exc:
        # Merging origin on top of an uncommitted tree is not what the caller
        # asked for, and the merge below would report a conflict the commit
        # failure caused.
        return False, f"{exc.step} failed: {exc.detail}"
    rc_ref, _, _ = await _git(workspace, "rev-parse", "--verify", f"origin/{branch}")
    if rc_ref != 0:
        return True, "no remote branch to sync from"
    rc, out, err = await _git(workspace, "merge", "--no-edit", f"origin/{branch}")
    if rc != 0:
        await _git(workspace, "merge", "--abort")
        return False, f"resync hit conflict on {branch}: {err or out}"
    return True, "resynced"


async def resync_branch(workspace: Path, *, branch: str) -> tuple[bool, str]:
    """Bring the current branch up to its origin counterpart without losing work.

    Used after the conflict-resolution chat has pushed the branch, and by the
    Settings sync flow. Commits pending work first (the live PWA workspace is
    almost always dirty), then *merges* ``origin/<branch>`` rather than
    resetting, so local commits are never discarded. One serialized mutation:
    the fetch, the commit, and the merge cannot be interleaved with another
    Ciaobot operation on this checkout.
    """
    async with repository_mutation(workspace):
        try:
            ensure_mutable(workspace)
        except RepositoryBusyError as exc:
            return False, exc.detail
        return await _resync_branch(workspace, branch=branch)


# ── preflight ────────────────────────────────────────────────────────────────


def _expand_status_paths(workspace: Path, porcelain: str) -> list[Path]:
    """Every pending file in ``porcelain`` output, as absolute paths.

    Git reports an untracked directory as a single entry, so the walk is what
    turns it into the files that would actually be staged. A deleted entry is
    not pending work — there is nothing left to read or to back up — and a
    nested checkout is skipped entirely: it is governed by its own Git
    metadata, and scanning its virtualenvs as workspace files is how a
    preflight invents hundreds of blockers.
    """
    raw_files: set[str] = set()
    for line in porcelain.splitlines():
        if not line:
            continue
        status_prefix = line[:2]
        file_part = line[3:].strip()
        if " -> " in file_part:
            parts = file_part.split(" -> ")
            file_part = parts[-1].strip()
        if file_part.startswith('"') and file_part.endswith('"'):
            file_part = file_part[1:-1]
        if "D" in status_prefix:
            continue
        raw_files.add(file_part)

    changed: list[Path] = []
    for f in raw_files:
        p = Path(workspace) / f
        if p.is_dir():
            if _is_nested_git_checkout(p, Path(workspace)):
                continue
            for dirpath, dirnames, filenames in os.walk(p):
                current = Path(dirpath)
                dirnames[:] = [
                    dirname
                    for dirname in dirnames
                    if not _is_nested_git_checkout(current / dirname, Path(workspace))
                ]
                changed.extend(current / fname for fname in filenames)
        elif p.is_file():
            changed.append(p)
    return changed


def scan_file_for_secrets(p: Path) -> tuple[list[str], list[str]]:
    """Blockers and warnings for one file's contents. Never raises.

    Shared by both preflights because a credential is a credential: the manual
    one and :func:`preflight_scoped` differ only in *which* files they are
    offered, and a second scanner would be a second set of rules to keep in
    step.
    """
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

    # OpenAI key check. Require a token boundary before `sk-` and
    # alphanumerics only after it (real keys have no interior dashes
    # except the `sk-proj-` / `sk-svcacct-` prefix). The old pattern
    # `sk-[A-Za-z0-9-]{40,}` false-positived on slugs like
    # `zendesk-121654-...-transcript` inside the generated vault INDEX.md.
    openai_keys = re.findall(r"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-)?[A-Za-z0-9]{40,}", content)
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


async def preflight_scoped(config, workspace: Path) -> dict:
    """What a scoped backup commit would contain here, and what it refuses.

    Three answers an unattended run needs before it stages anything: the paths
    inside the backup scope that are pending, the paths outside it that are
    (so a coverage gap is visible rather than silent), and the secret scan over
    the first group only. On top of those it reports every tracked path the
    scope refuses — a credential that was already committed to the repository
    is not protected by being skipped, and the manual sync path still stages
    the whole tree, so setup has to be able to raise that as a blocker.

    ``ok`` is false whenever there is anything to raise: a failing
    ``git status``, a credential in an eligible file, or a tracked file outside
    the scope.

    ``eligible`` is what a :func:`commit_scoped` call can really stage, so it
    has already been narrowed by git's own ignore rules
    (:func:`_ignored_paths`); the paths that narrowing removed are reported in
    ``excluded`` rather than dropped, and ``excluded`` is therefore every
    pending path this run will not commit.
    """
    root = backup_scope.data_root(config)
    rc, out, err = await _git(Path(workspace), "status", "--porcelain")
    blockers: list[str] = []
    if rc != 0:
        blockers.append(f"git status failed: {err or out}")
        changed: list[Path] = []
    else:
        changed = _expand_status_paths(Path(workspace), out)

    eligible, excluded = backup_scope.classify(changed, config)
    # The scope says where a path may go; git says whether it can be staged at
    # all, and a path it ignores can never be committed however eligible it is.
    # Reporting one as eligible that the commit would then refuse is how a
    # preflight hands its caller a failure, so it is reported as excluded
    # instead — a file the owner can see on disk and will not find in a backup
    # is exactly the coverage gap this report exists to surface.
    ignored = await _ignored_paths(Path(workspace), eligible)
    if ignored:
        excluded.extend(rel for rel in eligible if rel in ignored)
        eligible = [rel for rel in eligible if rel not in ignored]
    warnings: list[str] = []
    for rel in eligible:
        # The test-fixture exemption is keyed on a repo-relative path, so it
        # reads the same here as in the manual preflight.
        if _is_test_fixture(rel):
            continue
        file_blockers, file_warnings = scan_file_for_secrets(root / rel)
        blockers.extend(file_blockers)
        warnings.extend(file_warnings)

    tracked = await asyncio.to_thread(backup_scope.tracked_excluded, config)
    blockers.extend(f"Tracked but outside the backup scope: {rel}" for rel in tracked)
    return {
        "ok": not blockers,
        "data_root": str(root),
        "eligible": eligible,
        "excluded": excluded,
        "tracked_excluded": tracked,
        "blockers": blockers,
        "warnings": warnings,
    }


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
        """Commit the session and sync the current branch with origin.

        Branch detection runs *inside* the mutation lock: another local
        operation switching or creating a branch between detection and the
        commit would sync the wrong branch under the right branch's name.
        """
        async with repository_mutation(self.workspace):
            branch = workspace_branch(self.workspace)
            if branch is None:
                return {
                    "ok": False,
                    "step": "branch",
                    "error": "workspace is not a git repository (or is on a detached HEAD)",
                }
            try:
                ensure_mutable(self.workspace)
            except RepositoryBusyError as exc:
                return _busy_envelope(exc)
            return await _sync_branch(self.workspace, branch=branch)

    async def resync(self) -> dict:
        async with repository_mutation(self.workspace):
            branch = workspace_branch(self.workspace)
            if branch is None:
                return {
                    "ok": False,
                    "detail": "workspace is not a git repository (or is on a detached HEAD)",
                }
            try:
                ensure_mutable(self.workspace)
            except RepositoryBusyError as exc:
                return {"ok": False, "detail": exc.detail}
            ok, detail = await _resync_branch(self.workspace, branch=branch)
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

        changed_paths = _expand_status_paths(self.workspace, out)

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
                file_blockers, file_warnings = scan_file_for_secrets(p)
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
