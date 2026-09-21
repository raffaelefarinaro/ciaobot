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


def _unlink(root: Path, path: Path) -> None:
    """Remove `path`, telling git about it when the file is tracked."""
    if _tracked(root, path.name):
        removed = _git(root, "rm", "-q", "--cached", "--force", path.name)
        if removed.returncode != 0:
            logger.warning("git rm --cached failed for %s", path)
    path.unlink()


def _rename(root: Path, source: Path, destination: Path) -> None:
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
        return
    moved = _git(root, "mv", "-f", source.name, destination.name)
    if moved.returncode != 0:
        # Fall back rather than refuse: a readable guide under the new name
        # beats a migration that cannot proceed. The tree is left dirty and
        # the re-root's gate will say so, which is the honest outcome.
        logger.warning("git mv failed for %s: %s", source, moved.stderr.strip())
        source.rename(destination)
        return
    committed = _git(
        root,
        "-c", "user.name=Ciaobot",
        "-c", "user.email=ciaobot@localhost",
        "commit", "-q",
        "-m", f"chore(workspace): rename {source.name} to {destination.name}",
        "--", source.name, destination.name,
    )
    if committed.returncode != 0:
        logger.warning(
            "could not commit the guide rename in %s: %s",
            root,
            committed.stderr.strip(),
        )


def _merge_bodies(agents_text: str, legacy_text: str) -> str:
    """Fold `agents_text`'s unique lines under `legacy_text`.

    Only reached when both files are real and differ, which means the user
    authored one of them by hand. Keeping both bodies beats picking a winner:
    the alternative loses whichever side the heuristic guesses against.
    """
    if not legacy_text.strip():
        return agents_text
    existing = {line.strip() for line in legacy_text.splitlines() if line.strip()}
    unique = [
        line for line in agents_text.splitlines()
        if line.strip() and line.strip() not in existing
    ]
    if not unique:
        return legacy_text
    return (
        legacy_text.rstrip()
        + "\n\n## Merged from AGENTS.md\n\n"
        + "\n".join(unique)
        + "\n"
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
        legacy_is_file = legacy.is_file() and not legacy_is_link

        # Already done: AGENTS.md real, no CLAUDE.md of any kind.
        if agents_is_file and not legacy_is_link and not legacy_is_file:
            return "noop"

        # CLAUDE.md is itself a symlink at AGENTS.md (this repo's own shape,
        # and anything a user set up that way). Just drop it.
        if legacy_is_link and agents_is_file:
            _unlink(base, legacy)
            return "relinked"

        if not legacy_is_file:
            # No real legacy guide to move. A dangling AGENTS.md symlink is
            # cleared so the scaffolder can write a real file.
            if agents_is_link and not agents.exists():
                agents.unlink()
            return "noop"

        if agents_is_link:
            # The shape Ciaobot created: AGENTS.md -> CLAUDE.md.
            _unlink(base, agents)
            _rename(base, legacy, agents)
            return "relinked"

        if not agents_is_file:
            _rename(base, legacy, agents)
            return "renamed"

        # Both real. Compare before merging: identical copies need no backup.
        agents_text = agents.read_text(encoding="utf-8")
        legacy_text = legacy.read_text(encoding="utf-8")
        if agents_text.strip() == legacy_text.strip():
            _unlink(base, agents)
            _rename(base, legacy, agents)
            return "renamed"

        (base / f"{GUIDE_NAME}.bak").write_text(agents_text, encoding="utf-8")
        legacy.write_text(_merge_bodies(agents_text, legacy_text), encoding="utf-8")
        _unlink(base, agents)
        _rename(base, legacy, agents)
        return "merged"
    except OSError:
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
