"""Links that mirror a directory or a file: relative symlinks.

``sync_skills`` projects each skill directory, command and subagent into the
provider catalogs (``.claude/skills``, ``.claude/commands``, ...) as a link,
and recognises its own links later to repair or prune them. Every identity
check goes through here: ``is_link``, ``points_to``, ``link_source``,
``link_target`` and ``remove_link``.

POSIX is exactly what ``sync_skills`` did before this module existed: a
symlink, relative to ``relative_to`` when given, read back with
``os.readlink``, compared by ``resolve()`` and removed with ``unlink``.
"""

from __future__ import annotations

import os
from pathlib import Path

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
