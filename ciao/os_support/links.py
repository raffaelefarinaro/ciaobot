"""Links that mirror a directory or a file, with POSIX symlink behaviour.

``sync_skills`` projects each skill directory, command and subagent into the
provider catalogs (``.claude/skills``, ``.claude/commands``, ...) as a link,
and recognises its own links later to repair or prune them. Every identity
check goes through here: ``is_link``, ``points_to``, ``link_source``,
``link_target`` and ``remove_link``.

POSIX is exactly what ``sync_skills`` did before this module existed: a
symlink, relative to ``relative_to`` when given, read back with
``os.readlink``, compared by ``resolve()`` and removed with ``unlink``.

Windows cannot create a symlink without Developer Mode or admin (D-06, #696):

- A directory becomes an NTFS junction, which needs neither. Its target is
  absolute, so a moved workspace leaves it dangling until the next sync, which
  compares resolved targets and re-creates it. ``Path.is_symlink()`` is
  ``False`` for a junction; ``is_link`` answers for both, ``link_target``
  gives the target ``/``-separated without the ``\\\\?\\`` prefix, and a
  junction is removed with ``rmdir``, never its target.
- A file becomes a hard link, plus a sidecar ``<name>.ciao-link`` beside it
  that records the source relative to the link's own directory, ``/``-separated
  (what a POSIX relative symlink records). A hard link has no target of its own
  and survives its source being deleted as an ordinary file, so the sidecar is
  what makes it recognisably ours: ``is_link`` is true when the sidecar exists
  and the file is the recorded source's inode, or the recorded source is gone.
  An editor that saves the source by replacing it breaks the link; the file
  then still has a sidecar but a different inode, ``is_link`` is false,
  ``points_to`` is false, and the next sync re-links it. If it was the
  *mirror* that was saved that way, the mirror holds the newer text, and
  ``preserve_divergent_mirror`` moves it beside the source as
  ``<source>.ciao-conflict-<timestamp>`` before the re-link, so an edit is
  never discarded. ``prune_orphan_sidecars`` tidies sidecars whose mirror was
  deleted by hand. A file without a sidecar is never ours.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import _winapi
    import time

    _PREFIXES = ("\\\\?\\", "\\??\\")
    SIDECAR_SUFFIX = ".ciao-link"

    def _sidecar(link: Path) -> Path:
        return link.with_name(link.name + SIDECAR_SUFFIX)

    def _recorded_source(link: Path) -> Path | None:
        try:
            recorded = _sidecar(link).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return (link.parent / recorded) if recorded else None

    def link_dir(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a junction to ``source``, which must exist; ``relative_to`` has no effect."""
        _winapi.CreateJunction(str(Path(source).resolve()), str(link))

    def link_file(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a hard link to ``source`` and record the source beside it."""
        source_path, link_path = Path(source).resolve(), Path(link)
        os.link(source_path, link_path)
        recorded = os.path.relpath(source_path, link_path.parent.resolve()).replace("\\", "/")
        _sidecar(link_path).write_text(recorded + "\n", encoding="utf-8", newline="")

    def is_link(path: str | os.PathLike[str]) -> bool:
        """Whether ``path`` is one of our links (or any symlink/junction); see the module docstring."""
        candidate = Path(path)
        if candidate.is_symlink() or candidate.is_junction():
            return True
        source = _recorded_source(candidate)
        if source is None or not candidate.is_file():
            return False
        if not source.exists():
            return True  # ours, and its source is gone: prunable
        try:
            return os.path.samefile(source, candidate)
        except OSError:
            return False

    def link_source(path: str | os.PathLike[str]) -> Path:
        """What ``path`` mirrors: the resolved target, or a hard link's recorded source."""
        candidate = Path(path)
        if not (candidate.is_symlink() or candidate.is_junction()):
            source = _recorded_source(candidate)
            if source is not None:
                return source.resolve()
        return candidate.resolve()

    def points_to(link: str | os.PathLike[str], source: str | os.PathLike[str]) -> bool:
        """Whether ``link`` is a live mirror of ``source`` right now."""
        candidate, wanted = Path(link), Path(source)
        try:
            if candidate.is_symlink() or candidate.is_junction():
                return candidate.resolve() == wanted.resolve()
            return (
                _recorded_source(candidate) is not None
                and candidate.is_file()
                and os.path.samefile(candidate, wanted)
            )
        except OSError:
            return False

    def link_target(path: str | os.PathLike[str]) -> str:
        """The link's target, ``/``-separated; raises ``OSError`` if not a link."""
        candidate = Path(path)
        if not (candidate.is_symlink() or candidate.is_junction()):
            recorded = _recorded_source(candidate)
            if recorded is None:
                raise OSError(f"not a link: {candidate}")
            return recorded.as_posix()
        raw = os.readlink(candidate)
        for prefix in _PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw.replace("\\", "/")

    def remove_link(path: str | os.PathLike[str]) -> None:
        """Remove the link itself (and a hard link's sidecar), never what it points at."""
        candidate = Path(path)
        if candidate.is_junction():
            os.rmdir(candidate)
            return
        candidate.unlink(missing_ok=True)
        _sidecar(candidate).unlink(missing_ok=True)

    def preserve_divergent_mirror(link: str | os.PathLike[str]) -> Path | None:
        """Keep an edit made to a mirror before a sync re-links it; see the module docstring.

        Only a sidecar'd file that no longer shares its source's inode can hold
        such an edit: an editor saved the *mirror* by replacing it. Identical
        bytes, or a source newer than the mirror (the source was the one
        replaced), mean there is nothing to keep. Otherwise the mirror is moved
        beside the source as ``<source>.ciao-conflict-<timestamp>`` and that
        path is returned, so the caller can say where the text went.
        """
        candidate = Path(link)
        source = _recorded_source(candidate)
        if source is None or not candidate.is_file() or not source.is_file():
            return None
        try:
            if os.path.samefile(candidate, source):
                return None
            if candidate.read_bytes() == source.read_bytes():
                return None
            if candidate.stat().st_mtime <= source.stat().st_mtime:
                return None
        except OSError:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(candidate.stat().st_mtime))
        source = source.resolve()
        kept = source.with_name(f"{source.name}.ciao-conflict-{stamp}")
        os.replace(candidate, kept)
        return kept

    def prune_orphan_sidecars(folder: str | os.PathLike[str]) -> int:
        """Remove sidecars whose mirror is gone (deleted by hand); returns how many."""
        pruned = 0
        for sidecar in Path(folder).glob(f"*{SIDECAR_SUFFIX}"):
            mirror = sidecar.with_name(sidecar.name[: -len(SIDECAR_SUFFIX)])
            if not os.path.lexists(mirror):
                sidecar.unlink(missing_ok=True)
                pruned += 1
        return pruned

else:

    def link_dir(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a symlink to ``source``, relative to ``relative_to`` if given."""
        target: str | os.PathLike[str] = source
        if relative_to is not None:
            target = os.path.relpath(source, relative_to)
        Path(link).symlink_to(target)

    def link_file(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a symlink to ``source``, relative to ``relative_to`` if given."""
        link_dir(source, link, relative_to=relative_to)

    def is_link(path: str | os.PathLike[str]) -> bool:
        """Whether ``path`` is a symlink, dangling or not."""
        return Path(path).is_symlink()

    def link_source(path: str | os.PathLike[str]) -> Path:
        """What ``path`` mirrors: its resolved target."""
        return Path(path).resolve()

    def points_to(link: str | os.PathLike[str], source: str | os.PathLike[str]) -> bool:
        """Whether ``link`` resolves to ``source``."""
        try:
            return Path(link).resolve() == Path(source).resolve()
        except OSError:
            return False

    def link_target(path: str | os.PathLike[str]) -> str:
        """The symlink's target as written; raises ``OSError`` if not a link."""
        return os.readlink(path)

    def remove_link(path: str | os.PathLike[str]) -> None:
        """Remove the link itself, never what it points at."""
        Path(path).unlink(missing_ok=True)

    def preserve_divergent_mirror(link: str | os.PathLike[str]) -> Path | None:
        """Nothing to keep: a symlink cannot diverge from its source."""
        return None

    def prune_orphan_sidecars(folder: str | os.PathLike[str]) -> int:
        """No sidecars on POSIX."""
        return 0
