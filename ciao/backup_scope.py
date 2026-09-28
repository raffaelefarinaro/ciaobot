"""The explicit path scope an unattended backup is allowed to commit.

The manual sync path stages the whole tree (``git add -A``), which is right for
a person pressing "Sync with Remote" and wrong for a scheduled run: the sync
root is whatever repository holds the install's durable data, and on a
developer checkout that repository also holds the application source, nested
project worktrees and operator credentials. An unattended commit has to be a
*scope*, not a blanket, so the set of committable paths is defined here once
and every caller classifies against it.

Three properties are deliberate:

- **An allowlist, not a denylist.** A path is eligible because of where it
  is, not because nothing objected to it. Under a workspace agent root that
  means one of :data:`DURABLE_ROOTS` or the guide
  (:func:`ciao.workspace_guide.GUIDE_NAME`); a directory the app starts
  writing tomorrow is excluded until someone adds it here on purpose.
- **Resolved from configuration, never guessed.** :func:`data_root` is the same
  ``local_session.sync_root`` the existing push path uses, so a vault that
  lives in its own repository scopes against that repository. The frontend URL
  and the agent's working directory are never inputs.
- **Layout-aware.** There are three kinds of scope base, and which one a
  directory is decides what may live under it:

  - an *agent root* — the install root before the per-workspace re-rooting,
    one directory per workspace after — where the allowlist applies, because
    the install root also holds credentials, runtime state and the app itself;
  - the *configured vault*, whose internal layout is the app's own canonical
    structure and whose directory name is the operator's choice
    (``CIAO_VAULT_ROOT``), so provenance rather than a fixed set of names is
    what makes its contents durable;
  - the *archived workspaces* container, which holds one archived agent root
    per child, so the durable trees sit one level deeper there.

The denial rules refine that; they are not its complement. They name the
shapes that must not leave the machine at any depth: operator credentials,
runtime state, the provider mirrors ``sync-skills`` regenerates, dependency
trees and build output, and the derived transcript archive. ``Logs`` is in that
set at any depth because the app writes it under the vault before the
re-rooting and under the install root after, so there is no single location to
pin it to — and a multi-gigabyte derived archive is the worst possible thing to
hand an unattended ``git add``.

Leaf module by design: it imports nothing from ``ciao`` except the read-only
git helper and the guide's filename, and reaches ``local_session.sync_root``
through a deferred import so ``local_session`` can import *this* module for its
scoped commit and preflight without a cycle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from ciao.git_proc import run_git_sync
from ciao.workspace_guide import GUIDE_NAME

logger = logging.getLogger(__name__)

#: Durable, user-owned trees inside one agent root. The allowlist: a path under
#: an agent root is eligible only when its first segment is one of these. Each
#: is a canonical source the app reads at runtime, so losing one loses user
#: data rather than derived state:
#:
#: - ``memory-vault``: the notes, the proposal queues, the receipts.
#: - ``skills``/``subagents``/``commands``: the user's agent catalog. Stock
#:   copies are reinstalled from ``ciao/stock``, but an edited one is the
#:   user's work and is never overwritten.
DURABLE_ROOTS: tuple[str, ...] = ("memory-vault", "skills", "subagents", "commands")

#: Archived workspaces: a whole agent root, moved here byte for byte by
#: ``workspace_archive.move_to_archive``. Durable data that has left the live
#: tree, so it is a scope base in its own right — and a restore treats the
#: ``archive.json`` beside it as untrusted input, so the secret scan in
#: ``local_session.preflight_scoped`` still has to see what is inside.
ARCHIVED_WORKSPACES_DIR = ".archived-workspaces"

#: Directory names refused at any depth under a scope base. Grouped by what
#: they are, because the reason the scope refuses a row is the reason a reader
#: must not quietly delete it:
#:
#: - operator credentials and runtime state, which must not leave the machine
#: - the provider mirrors ``sync-skills`` regenerates from the canonical
#:   catalog, so a backup copy is only what git can already rebuild
#: - dependency trees, build output and caches: large, derived, useless
#:   off this machine
#: - ``Logs``, the derived transcript archive (see the module docstring)
#: - ``.git`` itself, which is not workspace data by any reading
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".runtime",
        "secrets",
        ".claude",
        ".agents",
        ".opencode",
        ".codex",
        ".worktrees",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cache",
        "dist",
        "build",
        "target",
        "Logs",
        ".git",
    }
)

#: File names refused at any depth under a scope base. ``opencode.json`` is the
#: opencode mirror of the provider session state; the ``.env`` family is the
#: operator's credentials, which ``local_session``'s secret scanner blocks too
#: — here it is refused before the scanner ever opens the file.
EXCLUDED_FILES: frozenset[str] = frozenset({".env", ".envrc", "opencode.json"})

#: Suffix marking a credential-shaped file whatever it is called
#: (``prod.env``). The secret scanner blocks the same shapes; refusing them
#: here means an excluded file is never even read.
EXCLUDED_SUFFIX = ".env"

# What a scope base holds, and therefore what may live under it.
_AGENT = "agent"
_VAULT = "vault"
_ARCHIVE = "archive"


class _ScopeBase(NamedTuple):
    """One directory whose contents the scope reasons about."""

    #: Data-root-relative, POSIX. Empty means the data root itself.
    rel: str
    kind: str


def data_root(config) -> Path:
    """The repository whose durable data this install backs up.

    Exactly the root the existing sync and branch-backup push operate on
    (``local_session.sync_root``): the repository containing the configured
    vault, or the workspace root when the vault is missing or is not in a
    repository. Deliberately *not* the frontend's base URL and *not* an agent's
    working directory — either would scope a backup onto whatever a request or
    a chat happened to point at.

    Imported deferred: ``local_session`` imports this module for the scoped
    commit, so a top-level import here would be a cycle.
    """
    from ciao.local_session import sync_root  # noqa: PLC0415

    return Path(sync_root(config))


def eligible_relpaths(config) -> tuple[str, ...]:
    """Every data-root-relative path the scope may commit, as path prefixes.

    The durable scope written out, once per scope base. Trailing slashes mark
    directory prefixes, a ``*`` marks the one level an archived workspace adds,
    and a vault base is rendered as the vault's own directory — its internal
    structure is the app's layout, not a fixed list of names.

    These are the *allowlist* only. A path under one of them can still be
    refused by the denial rules (``Logs``, a credential, a provider mirror),
    which :func:`ineligible` names; :func:`is_eligible` is the single answer
    to "may this be committed?".
    """
    prefixes: list[str] = []
    for base in _scope_bases(config, data_root(config)):
        head = f"{base.rel}/" if base.rel else ""
        if base.kind == _VAULT:
            prefixes.append(head or "./")
            continue
        offset = "*/" if base.kind == _ARCHIVE else ""
        for name in DURABLE_ROOTS:
            prefixes.append(f"{head}{offset}{name}/")
        prefixes.append(f"{head}{offset}{GUIDE_NAME}")
    return tuple(prefixes)


def ineligible(config) -> tuple[str, ...]:
    """What the scope refuses, as the same kind of names, for reporting.

    Every denied directory and file name, plus the transcript archive resolved
    for this install. Setup and the backup status surface this so an owner can
    see *why* a file is not backed up rather than discovering a gap after the
    fact. The names hold at any depth, so this is a vocabulary rather than a
    set of pathspecs.
    """
    names = {f"{name}/" for name in EXCLUDED_DIRS} | set(EXCLUDED_FILES)
    archive = _relative_to(Path(config.logs_root), data_root(config))
    if archive:
        names.add(f"{archive}/")
    return tuple(sorted(names))


def is_eligible(relpath: str | os.PathLike[str], config) -> bool:
    """Whether one path may be included in an unattended backup commit.

    Accepts a data-root-relative path or an absolute one; both resolve against
    :func:`data_root`, so a path outside the data root is ineligible whatever
    it is called. A symlink pointing out of the data root is ineligible for the
    same reason: resolution is what decides, not the spelling.
    """
    root = data_root(config)
    rel = _as_repo_relpath(relpath, root)
    if not rel:
        return False
    return _eligible_within(rel, _scope_bases(config, root))


def classify(
    paths: Iterable[str | os.PathLike[str]], config
) -> tuple[list[str], list[str]]:
    """Split paths into what the scope may commit and what it refuses.

    Returns ``(eligible, excluded)``, preserving input order and deduplicating.
    An eligible path comes back normalized and data-root relative, ready to
    hand to ``local_session.commit_scoped``. An excluded one comes back as the
    caller spelled it, because the interesting exclusions are the paths that
    are *not* inside the data root at all, and normalizing those has nothing
    to normalize against.
    """
    root = data_root(config)
    bases = _scope_bases(config, root)
    eligible: list[str] = []
    excluded: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        rel = _as_repo_relpath(raw, root)
        if rel:
            label, keep = rel, _eligible_within(rel, bases)
        else:
            label, keep = os.fspath(raw), False
        if label in seen:
            continue
        seen.add(label)
        (eligible if keep else excluded).append(label)
    return eligible, excluded


def tracked_excluded(config) -> list[str]:
    """Tracked paths the scope refuses, sorted.

    ``.gitignore`` only governs what git *starts* tracking: a credential that
    was already committed stays committed, and a later blanket stage — the
    manual sync path still performs one — would carry it off the machine. So
    the scope reports what git already tracks that it would not back up, which
    is what lets setup raise a blocking issue instead of skipping quietly.

    A repository that is also a developer checkout therefore reports its whole
    application source here, and that is the answer rather than noise: it means
    this repository is not a data-only one, and the fix is to give the data its
    own repository, not to widen this list.
    """
    root = data_root(config)
    rc, out, _err = run_git_sync(root, "ls-files", "-z")
    if rc != 0:
        logger.info("Could not list tracked files in %s", root)
        return []
    bases = _scope_bases(config, root)
    return sorted(
        {rel for rel in out.split("\0") if rel and not _eligible_within(rel, bases)}
    )


# ── the scope itself ─────────────────────────────────────────────────────────


def _scope_bases(config, root: Path) -> tuple[_ScopeBase, ...]:
    """The directories whose contents the scope reasons about, deepest first.

    ``root`` is the already-resolved data root: answering this costs a git
    subprocess, so a caller walking thousands of paths resolves it once (see
    :func:`classify`) and passes it in.

    A base is kept only when it resolves *inside* the data root: an install
    whose agent roots live in a different repository than its vault has part of
    its data outside what a commit here can reach, and that gap is reported by
    ``preflight_scoped`` rather than papered over. A vault base already covered
    by an agent base is dropped — with the default vault name it *is* one of
    the durable trees, not a root holding one — so the result is the exact
    scope rather than a superset of it.
    """
    candidates: dict[tuple[str, str], None] = {}
    for agent_root in _agent_roots(config):
        rel = _relative_to(agent_root, root)
        if rel is not None:
            candidates[(rel, _AGENT)] = None
    vault_rel = _relative_to(Path(config.vault_root), root)
    if vault_rel is not None:
        candidates.setdefault((vault_rel, _VAULT), None)
    candidates.setdefault((ARCHIVED_WORKSPACES_DIR, _ARCHIVE), None)

    bases = [
        _ScopeBase(rel, kind)
        for (rel, kind) in candidates
        if not any(
            other != (rel, kind) and _encloses_durable_tree(other[0], rel)
            for other in candidates
        )
    ]
    # Deepest first, so a nested base is matched before the tree holding it.
    bases.sort(key=lambda base: (-len(PurePosixPath(base.rel).parts), base.rel))
    return tuple(bases)


def _encloses_durable_tree(ancestor: str, rel: str) -> bool:
    """Whether ``ancestor`` already makes ``rel`` durable by virtue of its name."""
    remainder = _remainder(rel, ancestor)
    if not remainder:
        return False
    return remainder.split("/", 1)[0] in DURABLE_ROOTS


def _remainder(rel: str, base: str) -> str | None:
    """``rel`` expressed against ``base``, or None when it is not under it.

    The empty ``base`` is the data root itself, so it holds everything.
    """
    if not base:
        return rel
    return rel[len(base) + 1 :] if rel.startswith(f"{base}/") else None


def _agent_roots(config) -> tuple[Path, ...]:
    """The install's agent roots, through the one seam that answers for it.

    ``agent_root_targets`` is already the documented way to ask "where do this
    install's agent assets live" — the install root itself before the
    per-workspace re-rooting, one directory per workspace after — so the scope
    follows a layout change without a second rule to keep in step with it.
    """
    return tuple(root for root, _name in config.agent_root_targets())


def _relative_to(path: Path, root: Path) -> str | None:
    """``path`` as a POSIX path relative to ``root``, or None when outside it.

    Both sides are resolved, so a symlink pointing out of the data root reads
    as outside it rather than as the path it is spelled with. The data root
    itself resolves to the empty base, which is the "no prefix" case.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    try:
        rel = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    # `relative_to` renders "the same directory" as "."; the scope spells that
    # as the empty base, which is what a path under it is matched against.
    return "" if rel == "." else rel


def _as_repo_relpath(raw: str | os.PathLike[str], root: Path) -> str | None:
    """Normalize a path to a clean data-root-relative POSIX path, or None.

    None means "not a path this scope can speak about": a path outside the data
    root, the data root itself, or a relative path that climbs out of it with
    ``..``. The ``..`` spelling is refused before resolution rather than
    resolved and re-checked, so an escape attempt is a refusal instead of a
    path that quietly normalizes to somewhere legitimate.

    Relative input is resolved against the data root rather than left as
    written, so a symlinked folder reads the same however it is spelled: git
    reports a linked worktree by its own name, and the scope must refuse the
    target, not the name.
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        if ".." in PurePosixPath(str(raw).replace(os.sep, "/")).parts:
            return None
        candidate = Path(root) / candidate
    return _relative_to(candidate, root)


def _eligible_within(rel: str, bases: tuple[_ScopeBase, ...]) -> bool:
    """Whether ``rel`` is durable data under one of ``bases``."""
    for base in bases:
        remainder = _remainder(rel, base.rel)
        if remainder is not None and _is_durable(remainder, base.kind):
            return True
    return False


def _is_durable(remainder: str, kind: str) -> bool:
    """Whether a base-relative path is committable durable data.

    The denial rules apply to every kind, at every depth: that is the floor
    under a credential, not an option on top of the allowlist. Above it, what
    counts as durable depends on the kind of base.
    """
    parts = PurePosixPath(remainder).parts
    if not parts or any(part in EXCLUDED_DIRS for part in parts):
        return False
    name = parts[-1]
    if name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIX):
        return False
    if kind == _VAULT:
        # The vault is the user's notes by construction — its own layout is
        # the app's canonical structure, and its folder name is theirs.
        return True
    if kind == _ARCHIVE:
        # One archived agent root per child, so the durable trees sit one
        # level below the container rather than directly inside it.
        return _holds_durable_tree(parts, offset=1) or _holds_guide(parts, offset=1)
    return _holds_durable_tree(parts, offset=0) or _holds_guide(parts, offset=0)


def _holds_guide(parts: tuple[str, ...], *, offset: int) -> bool:
    """Whether the workspace guide sits where this base expects it.

    Only the guide at the base's own depth: ``AGENTS.md`` carries the bounded
    memory regions, and a file with that name deeper in a tree is the user's
    own file, not the workspace's guide.
    """
    return parts[-1] == GUIDE_NAME and len(parts) == offset + 1


def _holds_durable_tree(parts: tuple[str, ...], *, offset: int) -> bool:
    """Whether one of the durable trees starts where this base expects it.

    The tree needs a child of its own: a bare directory name is not a path git
    can commit, so ``memory-vault`` is not a backup target and
    ``memory-vault/note.md`` is.
    """
    return len(parts) > offset + 1 and parts[offset] in DURABLE_ROOTS
