"""Confining a note path to its vault.

A note path arrives as a vault-relative string from an agent. These helpers
resolve it to the one canonical file it names, or refuse: empty, absolute and
``..``-bearing spellings, symlinks anywhere on the way, anything that is not
a regular Markdown file, and the app's own bookkeeping under ``Workspace/``.
"""

from __future__ import annotations

import os
from pathlib import Path

from ciao.vault_index import is_reserved_bookkeeping


class NoteTargetRefused(ValueError):
    """The named note is not a readable Markdown file inside the vault."""


def canonical_vault(vault_root: Path | str) -> Path:
    """The vault root, resolved once.

    Only the root is canonicalized. Resolving the note path would follow a
    symlink planted inside the vault and hand the write to whatever it points
    at, so :func:`resolve_note_path` walks the components instead and refuses
    any link it finds.
    """
    root = Path(vault_root).resolve()
    if not root.is_dir():
        raise NoteTargetRefused(f"vault root is not a directory: {vault_root}")
    return root


def on_disk_name(parent: Path, part: str) -> Path:
    """The entry under *parent* that *part* names, spelled the way it is on disk.

    A case-insensitive filesystem resolves ``Notes/A.md`` and ``notes/a.md`` to
    one file while the two strings stay different, and everything keyed off the
    string then splits in two: the per-file lock (keyed by the resolved path),
    the ``relative_path`` a receipt records. A second writer reaching the same note through the other
    spelling takes a different lock and both revisions compare equal, so one
    write silently lands on top of the other. The directory listing is the only
    place the real spelling exists, so the walk reads it and takes that.

    An exact match wins, so this is a no-op on a case-sensitive filesystem
    (where the caller already spelled the name correctly). A component the
    *caller's own spelling does not resolve* is returned unchanged, so it
    reaches the walk's "no such note" refusal below: canonicalizing a name that
    is not there would answer a different question, and on a case-sensitive
    filesystem the two spellings are different paths — asking for
    ``notes/a.md`` with only ``Notes/A.md`` on disk would write a file nobody
    named. So the case-insensitive match runs only where the filesystem itself
    already resolved the caller's spelling, which is exactly the
    case-insensitive case. The single entry matching case-insensitively is then
    used, and a *tie* — two entries differing only in case under a filesystem
    that resolved one spelling to a file — is refused rather than guessed at: a
    name the caller cannot pick unambiguously is not a name this protocol
    should write through. A component that is not there at all, and an
    unreadable directory, are reported the same way they always were.
    """
    try:
        with os.scandir(parent) as entries:
            names = {entry.name for entry in entries}
    except OSError:
        return parent / part
    if part in names:
        return parent / part
    if not (parent / part).exists():
        return parent / part
    folded = part.casefold()
    matches = sorted(name for name in names if name.casefold() == folded)
    if not matches:
        return parent / part
    if len(matches) > 1:
        raise NoteTargetRefused(
            f"{parent} holds more than one entry named {part} up to case "
            f"({', '.join(matches)}); a note write must name one file"
        )
    return parent / matches[0]


def resolve_note_path(vault_root: Path | str, relative_path: str) -> Path:
    """The canonical file one vault-relative path names, or a refusal.

    The path is a ``/``-separated path *inside* the vault. Empty, absolute and
    ``..``-bearing spellings are refused, as is any component that is a symlink
    (the file itself or any directory on the way to it), anything that is not a
    regular file, and anything that is not ``.md``.

    The returned path is spelled the way the vault has it, so both spellings of
    a case-insensitive name resolve to one file, one lock and one recorded
    ``relative_path`` (see :func:`on_disk_name`).

    The app's own bookkeeping under ``Workspace/`` is refused as a target: the
    Unsure list, the curation logs and their neighbours are managed by the
    pipelines that own them, each with its own atomic write, and a note write
    bypassing those would be an ordinary-looking undo of a file the user never
    edited.
    """
    root = canonical_vault(vault_root)
    raw = str(relative_path or "").strip()
    if not raw:
        raise NoteTargetRefused("a note path is required")
    candidate = Path(raw)
    if candidate.is_absolute() or raw.startswith(("/", "\\")) or candidate.drive:
        raise NoteTargetRefused(f"a note path must be relative to the vault: {raw}")
    parts = [part for part in candidate.parts if part not in ("", ".")]
    if not parts:
        raise NoteTargetRefused("a note path is required")
    if any(part == ".." for part in parts):
        raise NoteTargetRefused(
            f"a note path may not traverse out of the vault: {raw}"
        )
    current = root
    for part in parts:
        current = on_disk_name(current, part)
        if current.is_symlink():
            raise NoteTargetRefused(
                f"{current} is a symlink; a note write follows no link"
            )
    target = current
    if not target.exists():
        raise NoteTargetRefused(f"no such note in this vault: {raw}")
    if not target.is_file():
        raise NoteTargetRefused(f"not a regular file: {raw}")
    if target.suffix.lower() != ".md":
        raise NoteTargetRefused(f"only Markdown notes are writable here: {raw}")
    # Belt and braces: the component walk above already refuses every link, so
    # a resolved path that still left the vault would mean that walk was wrong.
    # Checked before the first `relative_to`, which would raise a bare
    # `ValueError` for exactly that regression — a 500 for an ordinary user
    # action, where every other way of naming the wrong file is a refusal.
    if not target.is_relative_to(root):
        raise NoteTargetRefused(f"a note path may not leave the vault: {raw}")
    if is_reserved_bookkeeping(target.relative_to(root)):
        raise NoteTargetRefused(
            f"{target.name} is the app's own vault bookkeeping under "
            f"{target.parent.name}/, not a note: it is written by the pipeline "
            f"that owns it, not by a note edit"
        )
    return target
