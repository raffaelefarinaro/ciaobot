"""The workspace guide's filename, and the migration off ``CLAUDE.md``.

Claude Code read only ``CLAUDE.md`` until 2.1.277. opencode has always read
``AGENTS.md``. Ciaobot bridged that by writing ``CLAUDE.md`` and symlinking
``AGENTS.md`` at it, so one file served both.

2.1.277 removed the reason for the bridge. Its default instruction-file mode
is ``claude-md-or-agents-md``: *a project with no CLAUDE.md of its own gets
its AGENTS.md files instead, loaded exactly where and how CLAUDE.md would
be.* The qualifier is the whole point — the fallback only engages when
``CLAUDE.md`` is **absent**, so keeping it around as a file or a symlink
keeps the old path alive. Removing it is what makes ``AGENTS.md`` canonical.

``AGENTS.md`` is therefore the guide, and nothing writes ``CLAUDE.md`` any
more.

What makes this delicate is not the rename. The guide carries the
``ciao:memory`` and ``ciao:profile`` regions — the durable facts the agent has
remembered — so a migration that writes a fresh file instead of moving the
existing one silently drops everything the user has taught it. That failure
already has a scar in this codebase (``workspace_reroot``'s guide split), so
the migration here only ever *renames*, never creates, and it keeps a backup
of anything it had to merge.

Compatibility: a Claude Code older than 2.1.277 stops seeing the guide, which
is the deliberate trade — there is no opt-out, and nothing creates or requires
``CLAUDE.md`` any more. opencode is unaffected: ``AGENTS.md`` is what it wanted
all along.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: The workspace guide. Both providers discover this name natively.
GUIDE_NAME = "AGENTS.md"

#: What the guide used to be called. Only the migration should reference it.
LEGACY_GUIDE_NAME = "CLAUDE.md"


def guide_path(root: Path | str) -> Path:
    """The workspace guide for `root`.

    ``AGENTS.md`` when it is a real file, the legacy ``CLAUDE.md`` when that is
    what this install still has, and otherwise the ``AGENTS.md`` path so a
    caller creating the guide creates the right one.

    The legacy fallback matters for the window between an upgrade and the
    migration running, and for any root the migration could not touch. Reading
    the guide must never depend on the migration having succeeded — the memory
    regions live in it.
    """
    base = Path(root)
    agents = base / GUIDE_NAME
    if agents.is_file():
        return agents
    legacy = base / LEGACY_GUIDE_NAME
    if legacy.is_file():
        return legacy
    return agents


def legacy_guide_path(root: Path | str) -> Path:
    """The pre-migration guide path, whether or not it exists."""
    return Path(root) / LEGACY_GUIDE_NAME


def _aliases(link: Path, target: Path) -> bool:
    """Whether `link` is a symlink that resolves to `target`."""
    try:
        return link.is_symlink() and link.resolve() == target.resolve()
    except OSError:
        return False


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _tracked(root: Path, name: str) -> bool:
    """Whether git tracks `name` in `root`. False when this is not a repo."""
    try:
        proc = _git(root, "ls-files", "--error-unmatch", name)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _commit_guide_change(root: Path, message: str, *paths: str) -> None:
    """Commit exactly `paths`, so the tree the migration touched ends clean.

    ``--no-verify`` and ``--no-gpg-sign`` because this is Ciaobot's own
    bookkeeping commit, not the operator's: a repo-level ``pre-commit`` hook
    or a global ``commit.gpgsign = true`` would otherwise fail the commit and
    leave the rename *staged* — a dirty tracked tree, which is the exact
    condition this function exists to avoid, plus a staged change the
    operator's next commit would silently carry.
    """
    committed = _git(
        root,
        "-c", "user.name=Ciaobot",
        "-c", "user.email=ciaobot@localhost",
        "commit", "-q", "--no-verify", "--no-gpg-sign",
        "-m", message,
        "--", *paths,
    )
    if committed.returncode == 0:
        return
    # A non-zero exit is not automatically a problem: when the rename produced
    # no net change against HEAD (the two guides held identical bytes), git
    # exits 1 with "nothing to commit" and the tree is already clean, which is
    # the outcome this function exists to produce. Judge by the tree, not the
    # exit code — warning there sends an operator looking for a failure that
    # did not happen.
    left = _git(root, "status", "--porcelain", "--untracked-files=no", "--", *paths)
    if left.returncode != 0 or left.stdout.strip():
        logger.warning(
            "could not commit the guide change in %s: %s",
            root,
            committed.stderr.strip() or committed.stdout.strip(),
        )


def _unlink(root: Path, path: Path, *, commit: bool = False) -> None:
    """Remove `path`, telling git about it when the file is tracked.

    `commit` is for the callers that finish here rather than going on to
    `_rename`: without it the deletion is left staged, which is the same
    dirty tracked tree a plain rename used to leave.
    """
    tracked = _tracked(root, path.name)
    if tracked:
        removed = _git(root, "rm", "-q", "--cached", "--force", path.name)
        if removed.returncode != 0:
            logger.warning("git rm --cached failed for %s", path)
    path.unlink()
    if tracked and commit:
        _commit_guide_change(root, f"chore(workspace): drop {path.name}", path.name)


def _rename(
    root: Path, source: Path, destination: Path, *, also_tracked: bool = False
) -> None:
    """Move `source` onto `destination`, keeping the git tree clean.

    A plain ``Path.rename`` on a tracked guide leaves the workspace dirty —
    ``T AGENTS.md`` / ``D CLAUDE.md`` — and `workspace_reroot.apply` refuses
    to run against uncommitted tracked changes. Nothing else commits it
    (`auto_sync_on_start` is off by default), so the re-root would be blocked
    on every later boot by a rename Ciaobot performed itself, with the
    housekeeping strip telling the operator to commit a file they never
    touched.

    So: ``git mv`` and commit the rename when the guide is tracked, and a
    plain rename when it is not (no repo, or an untracked guide). The commit
    is deliberately narrow — only the two guide paths are staged, never
    whatever else the operator has in flight.
    """
    if not _tracked(root, source.name):
        source.rename(destination)
        if also_tracked:
            # The destination was tracked even though the source was not — a
            # tracked AGENTS.md replaced by an untracked CLAUDE.md. Its
            # removal is already staged, so without this commit the tree keeps
            # `D AGENTS.md` plus an untracked AGENTS.md and the re-root's
            # clean-tree gate refuses on every boot.
            _git(root, "add", "--", destination.name)
            _commit_guide_change(
                root,
                f"chore(workspace): rename {source.name} to {destination.name}",
                destination.name,
            )
        return
    moved = _git(root, "mv", "-f", source.name, destination.name)
    if moved.returncode != 0:
        # Fall back rather than refuse: a readable guide under the new name
        # beats a migration that cannot proceed. The tree is left dirty and
        # the re-root's gate will say so, which is the honest outcome.
        logger.warning("git mv failed for %s: %s", source, moved.stderr.strip())
        source.rename(destination)
        return
    _commit_guide_change(
        root,
        f"chore(workspace): rename {source.name} to {destination.name}",
        source.name,
        destination.name,
    )


def _write_backup(path: Path, text: str) -> bool:
    """Write `text` to `path` without ever following a symlink.

    The backup name sits in the workspace, so whatever is already there is
    not necessarily a regular file. A symlink at ``AGENTS.md.bak`` pointing
    somewhere else — a dotfile, a shell rc — would otherwise receive the
    incoming guide's bytes through the link, because ``write_text`` follows
    it. The migration runs unattended at startup, before the server binds, so
    nobody is watching when it happens.

    ``O_NOFOLLOW`` refuses the open outright when the final component is a
    link, and ``O_TRUNC`` is deliberate for the regular-file case: the backup
    is rewritten, not appended. Returns whether the backup was written; a
    refusal is reported by the caller rather than silently skipped, because
    the backup is the only copy of what the merge does not fold in.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError:
        logger.warning(
            "refusing to write %s: it exists and is not a regular file", path
        )
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        logger.exception("could not write %s", path)
        return False
    return True


def _merge_bodies(agents_text: str, legacy_text: str) -> str:
    """Fold `agents_text`'s unique **body** lines under `legacy_text`.

    Only reached when both files are real and differ, which means the user
    authored one of them by hand. Keeping both bodies beats picking a winner:
    the alternative loses whichever side the heuristic guesses against.

    The incoming file's bounded regions are stripped before the line merge,
    and the legacy file's are left exactly as they are. A line-level merge
    over region contents is actively destructive: the fenced entries land
    under a plain heading *outside* the fences, where nothing expires,
    audits or caps them, and two files with different `cap=` values produce a
    second `:start` marker whose matching `:end` is deduplicated away as a
    duplicate line — leaving an unterminated region that later writes refuse.

    Nothing is lost by stripping: the incoming file is copied verbatim to
    ``AGENTS.md.bak`` before any of this, regions included, and the merged
    guide points at it.
    """
    from ciao.memory_tool import strip_region_blocks

    if not legacy_text.strip():
        return agents_text
    agents_body = strip_region_blocks(agents_text)
    existing = {line.strip() for line in legacy_text.splitlines() if line.strip()}
    unique = [
        line for line in agents_body.splitlines()
        if line.strip() and line.strip() not in existing
    ]
    backup_note = (
        f"\n\nThe previous `{GUIDE_NAME}` was folded in here. Its bounded "
        f"memory regions were **not** merged — they are kept verbatim in "
        f"`{GUIDE_NAME}.bak`.\n"
    )
    if not unique:
        # Identical bodies differing only inside the regions: there is nothing
        # to fold, but the incoming entries are still not in this file and the
        # operator has no other way to learn that. The notice is the only
        # pointer to them.
        return legacy_text.rstrip() + backup_note
    return (
        legacy_text.rstrip()
        + f"\n\n## Merged from {GUIDE_NAME}\n\n"
        + "\n".join(unique)
        + backup_note
    )


def migrate_root(root: Path | str) -> str:
    """Make ``AGENTS.md`` the only guide in `root`. Returns what it did.

    One of:

    ``"noop"``
        Already migrated, or there is no guide to migrate.
    ``"relinked"``
        ``AGENTS.md`` was a symlink at ``CLAUDE.md`` — the shape Ciaobot used
        to create. The symlink is removed and ``CLAUDE.md`` **renamed** onto
        it, so the bytes (and the memory regions in them) are the same
        inode's, never a copy.
    ``"renamed"``
        Only ``CLAUDE.md`` existed; renamed.
    ``"merged"``
        Both were real files with different contents, so the user authored
        one. ``CLAUDE.md`` takes the unique lines of ``AGENTS.md`` under a
        heading, the original ``AGENTS.md`` is kept as ``AGENTS.md.bak``, and
        the result is renamed into place.
    ``"failed"``
        An OSError got in the way; the root is left exactly as it was and the
        legacy fallback in :func:`guide_path` keeps serving it.

    Never creates a guide. A root with neither file is left for the scaffolder.
    """
    base = Path(root)
    agents = base / GUIDE_NAME
    legacy = base / LEGACY_GUIDE_NAME

    try:
        agents_is_link = agents.is_symlink()
        agents_is_file = agents.is_file() and not agents_is_link
        legacy_is_link = legacy.is_symlink()
        # A symlink that resolves to a readable file still *has* content. Only
        # an alias of this root's own AGENTS.md is safe to discard; every other
        # one is treated as a real guide so its text is merged rather than
        # thrown away.
        legacy_is_file = legacy.is_file() and (
            not legacy_is_link or not _aliases(legacy, agents)
        )

        # Already done: AGENTS.md real, no CLAUDE.md of any kind.
        if agents_is_file and not legacy_is_link and not legacy_is_file:
            return "noop"

        # CLAUDE.md is itself a symlink AT this root's AGENTS.md (this repo's
        # own shape, and anything a user set up that way). Dropping it loses
        # nothing, because the file it names is the one being kept.
        #
        # Checked, not assumed: a CLAUDE.md symlinked to an external or shared
        # guide is NOT an alias of AGENTS.md — it is the instructions Claude
        # has been loading. Unlinking that on the "it's just our alias"
        # fast path drops them out of the canonical guide entirely, so it
        # falls through to the divergent-guide merge below instead.
        if legacy_is_link and agents_is_file and _aliases(legacy, agents):
            _unlink(base, legacy, commit=True)
            return "relinked"

        if not legacy_is_file:
            # No real legacy guide to move. A dangling AGENTS.md symlink is
            # cleared so the scaffolder can write a real file.
            if agents_is_link and not agents.exists():
                agents.unlink()
            return "noop"

        if agents_is_link:
            # The shape Ciaobot created: AGENTS.md -> CLAUDE.md.
            agents_tracked = _tracked(base, agents.name)
            _unlink(base, agents)
            _rename(base, legacy, agents, also_tracked=agents_tracked)
            return "relinked"

        if not agents_is_file:
            _rename(base, legacy, agents)
            return "renamed"

        # Both real. Compare before merging: identical copies need no backup.
        agents_text = agents.read_text(encoding="utf-8")
        legacy_text = legacy.read_text(encoding="utf-8")
        if agents_text.strip() == legacy_text.strip():
            agents_tracked = _tracked(base, agents.name)
            _unlink(base, agents)
            _rename(base, legacy, agents, also_tracked=agents_tracked)
            return "renamed"

        # The backup is the only copy of what the merge does not fold in, so
        # a refusal to write it stops the merge rather than proceeding without
        # it. The guide stays readable under its old name either way.
        if not _write_backup(base / f"{GUIDE_NAME}.bak", agents_text):
            return "failed"
        merged = _merge_bodies(agents_text, legacy_text)
        if legacy_is_link:
            # A symlink at the legacy name points at a guide this root does
            # not own (an alias of AGENTS.md returned above). Replacing the
            # link would repoint the root at a new file instead of updating
            # the shared guide, so this path keeps writing through it.
            legacy.write_text(merged, encoding="utf-8")
        else:
            # Atomic swap: a crash or a full disk mid-write must not leave a
            # truncated guide behind. ``os.replace`` is atomic on one
            # filesystem, and the temp file lives beside the target so it is.
            tmp = base / f"{LEGACY_GUIDE_NAME}.merge.tmp"
            try:
                tmp.write_text(merged, encoding="utf-8")
                os.replace(tmp, legacy)
            except OSError:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        agents_tracked = _tracked(base, agents.name)
        _unlink(base, agents)
        _rename(base, legacy, agents, also_tracked=agents_tracked)
        return "merged"
    except (OSError, subprocess.SubprocessError):
        # SubprocessError too: `_git` has a timeout, and TimeoutExpired is not
        # an OSError, so it used to escape the documented "failed" contract
        # and unwind whole callers (agent_assets' repair loop has no per-root
        # guard).
        logger.exception("workspace guide migration failed for %s", base)
        return "failed"


def migrate_config(config: object) -> dict[str, str]:
    """Migrate every agent root this install owns. Returns root -> action.

    Never raises: a guide that cannot be moved is still readable through
    :func:`guide_path`'s legacy fallback, so a failure here degrades to "not
    migrated yet" rather than to a lost guide. Callers log the result.
    """
    results: dict[str, str] = {}
    try:
        targets = config.agent_root_targets()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — an unreadable registry must not fail startup
        logger.exception("could not enumerate agent roots for guide migration")
        return results
    for root, _name in targets:
        try:
            results[str(root)] = migrate_root(root)
        except Exception:  # noqa: BLE001
            logger.exception("guide migration raised for %s", root)
            results[str(root)] = "failed"
    return results


def migrate_if_needed(config: object) -> dict[str, str]:
    """Startup entry point: rename every root's legacy guide, once."""
    results = migrate_config(config)
    moved = {r: a for r, a in results.items() if a not in ("noop", "failed")}
    if moved:
        logger.info("workspace guide migrated to %s: %s", GUIDE_NAME, moved)
    failed = [r for r, a in results.items() if a == "failed"]
    if failed:
        logger.warning("workspace guide migration failed for: %s", failed)
    return results
