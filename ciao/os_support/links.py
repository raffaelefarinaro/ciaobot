"""Links that mirror a directory or a file, with POSIX symlink behaviour.

``sync_skills`` projects each skill directory, command and subagent into the
provider catalogs (``.claude/skills``, ``.claude/commands``, ...) as a link,
and recognises its own links later to repair or prune them. Every identity
check goes through here: ``is_link``, ``link_target`` and ``remove_link``.

POSIX is exactly what ``sync_skills`` did before this module existed: a
symlink, relative to ``relative_to`` when given, read back with
``os.readlink`` and removed with ``unlink``.

Windows cannot create a symlink without Developer Mode or admin, so a
directory becomes an NTFS junction (D-06, #696), which needs neither. A
junction's target is absolute, so moving the workspace leaves it pointing at
the old place until the next sync, which compares resolved targets and
re-creates it. ``Path.is_symlink()`` is ``False`` for a junction; ``is_link``
answers for both. ``link_target`` gives the target with ``/`` separators and
without the ``\\\\?\\`` prefix, so a caller matching ``"/skills/"`` in it works
on both. A junction is removed with ``rmdir``, which removes the link and
never its target.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import _winapi

    _PREFIXES = ("\\\\?\\", "\\??\\")

    def link_dir(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a junction to ``source``; ``relative_to`` has no effect."""
        _winapi.CreateJunction(str(Path(source).resolve()), str(link))

    def link_file(
        source: str | os.PathLike[str],
        link: str | os.PathLike[str],
        *,
        relative_to: str | os.PathLike[str] | None = None,
    ) -> None:
        """Make ``link`` a hard link to ``source``; ``relative_to`` has no effect."""
        os.link(Path(source).resolve(), link)

    def is_link(path: str | os.PathLike[str]) -> bool:
        """Whether ``path`` is a symlink or a junction, dangling or not."""
        candidate = Path(path)
        return candidate.is_symlink() or candidate.is_junction()

    def link_target(path: str | os.PathLike[str]) -> str:
        """The link's target, ``/``-separated; raises ``OSError`` if not a link."""
        raw = os.readlink(path)
        for prefix in _PREFIXES:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return raw.replace("\\", "/")

    def remove_link(path: str | os.PathLike[str]) -> None:
        """Remove the link itself, never what it points at."""
        candidate = Path(path)
        if candidate.is_junction():
            os.rmdir(candidate)
        else:
            candidate.unlink(missing_ok=True)

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

    def link_target(path: str | os.PathLike[str]) -> str:
        """The symlink's target as written; raises ``OSError`` if not a link."""
        return os.readlink(path)

    def remove_link(path: str | os.PathLike[str]) -> None:
        """Remove the link itself, never what it points at."""
        Path(path).unlink(missing_ok=True)
