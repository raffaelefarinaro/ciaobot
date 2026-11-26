"""Moving a project folder inside the vault, with every reference to it repointed.

A project is closed by moving its folder under ``projects/completed/`` (and reopened by
moving it back). The move rewrites every note that links into the folder, in both link
dialects, and is all-or-nothing: if any write fails, everything already swapped is put
back and the folder stays where it was.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

from ciao.vault_index import Entry, build_filename_index, scan_vault, temp_prefix
from ciao.vault_reference_rewrite import rewrite_references

def _discard(path: Path) -> None:
    """Delete a file that no longer records anything real.

    Used for the leftover temps of a rolled-back rewrite and for the completion
    recovery sidecar once the ledger row is durable. Neither is recoverable data
    once its durable record exists, and a failed unlink is not worth failing a
    completed action over — the worst case is one inert file where no scan, lint
    pass or index will read it.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_exact(path: Path) -> str:
    """A note's text with its line endings exactly as written.

    ``Path.read_text`` opens in universal-newline mode, so a CRLF note comes back
    with every ``\\r`` already eaten — and :func:`_write_texts` then writes it
    back with LF endings. Completing one project would have silently reflowed
    every CRLF note that linked to it into a whole-file diff, and the undo images
    would have been "originals" that no longer matched the bytes on disk, so a
    restore could never tell an edit from a line-ending change.

    ``newline=""`` disables the translation in both directions, so what is read
    is what a byte-level diff of the file would show.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_exact(path: Path, text: str) -> None:
    """Write *text* to *path* without translating its line endings.

    The mirror of :func:`_read_exact`, and the reason an undo image can be
    compared against a file at all: ``Path.write_text`` would turn the CRLF in a
    recorded original into LF on its way back to disk.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _write_texts(pairs: list[tuple[Path, str, str]]) -> None:
    """Write each ``(path, current, text)`` as one transaction, rolling back on failure.

    Every new text is staged to a temp file beside its target first — same
    directory, so ``os.replace`` stays atomic and on the same filesystem — and
    only then are the targets swapped in. A failure at any point restores every
    already-swapped file from its recorded original in reverse order and removes
    the leftover temps, then re-raises: a raised ``OSError`` means the vault is
    exactly as it was.

    *current* is the text the rewrite was computed against, and it is re-read and
    compared immediately before each swap. The reads that produced these
    rewrites happened earlier in the same request — for a completion, a full
    vault sweep — and a note edited in between would otherwise have this
    rewrite laid over the edit, or the image of an "original" that no longer
    existed. The mismatch travels as an ``OSError`` because that is what every
    caller of this helper already turns into a refusal; a raised error means
    nothing was left half-written either way.

    The pattern ``_commit_staged_edits`` in ``vault_index`` uses, for the same reason. Writing each note the
    moment its rewrite was computed can leave earlier notes pointing at a
    destination that never arrived, and a half-applied link rewrite is the one
    state a completion must not be able to leave behind.
    """
    staged: list[tuple[Path, Path]] = []
    swapped: list[tuple[Path, str]] = []
    try:
        for path, _current, text in pairs:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                # `newline=""` for the reason `_read_exact` exists: without it a
                # CRLF note comes out of the temp file with its `\r` eaten.
                newline="",
                delete=False,
                dir=path.parent,
                prefix=temp_prefix(path.name),
                suffix=".tmp",
            ) as handle:
                handle.write(text)
                temp = Path(handle.name)
            # A fresh temp file lands at 0600 and os.replace would silently
            # tighten the rewritten note's permissions; carry the old mode over,
            # exactly as `_reverify` does for a stamped note.
            try:
                os.chmod(temp, path.stat().st_mode & 0o7777)
            except OSError:
                pass
            staged.append((path, temp))
        for (path, temp), (target, current, _text) in zip(staged, pairs):
            if _read_exact(target) != current:
                raise OSError(f"{target} changed while its references were being rewritten")
            os.replace(temp, path)
            swapped.append((target, current))
    # The decode failure is caught with the rest on purpose. A caller that has
    # already moved something unwinds on the files this swapped in, and it only
    # does that if this raises the error it is listening for: a
    # `UnicodeDecodeError` here skipped the unwind and left the vault with some
    # of its notes repointed and the rest not.
    except (OSError, UnicodeDecodeError):
        _unwrite_texts(swapped)
        for _path, temp in staged:
            _discard(temp)
        raise


def _unwrite_texts(swapped: list[tuple[Path, str]]) -> None:
    """Put already-written files back from their recorded originals, best effort.

    The inverse of :func:`_write_texts` for the failures that happen AFTER the
    writes are on disk. Best effort by design: a rollback that itself cannot
    write means the vault is already in a state no error message improves, and
    raising from inside the recovery path would replace the error that explains
    what went wrong with one that does not.
    """
    for path, original in reversed(swapped):
        try:
            _write_exact(path, original)
        except OSError:
            pass


def _vault_ref(path: str) -> str:
    """A note's path as a vault-relative ref: no ``memory-vault/`` prefix, no extension."""
    return Path(path).relative_to("memory-vault").with_suffix("").as_posix()


def _vault_path(root: Path, path: Path) -> str:
    """An absolute vault path as the ``memory-vault/…`` id every candidate uses.

    The ledger, the candidate evidence and the sweep all name notes in this
    namespace, so anything read off disk has to be rendered into it before it can
    be compared with a recorded key.
    """
    return (Path("memory-vault") / path.relative_to(root)).as_posix()


def _missing_ancestors(path: Path, stop: Path) -> list[Path]:
    """The directories between *stop* and *path* that do not exist yet."""
    missing: list[Path] = []
    probe = path
    while probe != stop and not probe.exists():
        missing.append(probe)
        probe = probe.parent
    return missing


def _prune_created(dirs: list[Path]) -> None:
    """Remove directories this action created, innermost first, when they are empty.

    A rolled-back completion has to leave the vault as it found it, and a
    ``mkdir(parents=True)`` that nothing is then moved into leaves
    ``projects/completed/<slug>/`` behind — which the PWA reads as a completed
    project, because that tree is how it discovers them. ``rmdir`` only removes
    an empty directory, so a directory something else has since put a note in is
    never touched, and the outer ``projects/completed/`` survives if the vault
    had it before.
    """
    for path in dirs:
        try:
            path.rmdir()
        except OSError:
            pass


class _Rewrite(NamedTuple):
    """One note's rewritten text, and the two paths it has across the move.

    The two paths are the whole reason this is not a plain triple. A note inside
    a completed project folder is written through ``path_before`` — the move has
    not happened yet when the rewrites land — and its undo image is recorded
    under ``path_after``, because that is where the file will be by the time
    anything reads it back. ``restore_completed`` looks its images up by
    ``path_after`` and writes them back through it, and the rollback after a
    failed move goes through ``path_before`` again.
    """

    path_before: Path
    path_after: Path
    before: str
    after: str


def _ref_needles(moves: list[tuple[str, str]]) -> set[str]:
    """Substrings that only a note naming one of these moved notes can contain.

    A reference in any dialect is a path without its extension, so it contains
    the note's stem — and in the percent-encoded spelling, which is the one that
    is not a substring of the bare stem: a destination with a space in it is
    written ``My%20Project.md``, so a prefilter comparing the bare stem against
    the text skipped the one file that needed rewriting and the link dangled
    after the move.

    Compared case-folded for the same reason, one step weaker: it only means a
    differently-cased spelling is *read and handed to the rewriter* rather than
    skipped unread. Whether it then resolves is `vault_index`'s case sensitivity,
    not this prefilter's — the rewriter leaving such a link alone is the correct
    outcome, and a link it cannot prove points at the project is one it must not
    touch.
    """
    return {
        needle.casefold()
        for old, _new in moves
        for needle in (Path(old).stem, quote(Path(old).stem))
    }


def _folder_moves(
    entries: list[Entry], folder: str, destination_folder: str, *, skip: str = ""
) -> list[tuple[str, str]]:
    """Every note under *folder* except *skip*, paired with its place under *destination_folder*.

    A folder project moves as a folder, so each note inside it moves with it. The
    suffix carries the note's place inside the folder, which the move does not
    change: only the leading segment above it does.
    """
    moves: list[tuple[str, str]] = []
    for entry in entries:
        rel = entry.path_key
        if rel == skip or not rel.startswith(f"{folder}/"):
            continue
        moves.append((rel, destination_folder + rel[len(folder):]))
    return moves


def _plan_reference_rewrites(
    root: Path, entries: list[Entry], moves: list[tuple[str, str]]
) -> tuple[list[str], list[_Rewrite]]:
    """The rewrites that *moves* requires across the vault, with nothing written.

    *moves* is ``(old, new)`` per note, both ``memory-vault/…`` paths, and *root*
    is the resolved vault root. Nothing here depends on the direction of the move,
    so any folder move can plan through it, not only a completion.

    Only notes that can name a moved note are read. A reference in any dialect is
    a path without its extension, so it always contains that note's stem, and a
    file without any of them cannot mention one: parsing every note in the vault
    to discover that is the cost this skips. A note that is itself moving is read
    regardless, because its own relative links are measured from its new place.
    """
    filename_index = build_filename_index(entries)
    moved_to = dict(moves)
    # `rewrite_references` works in two value spaces, so the one move is spelled
    # twice: a markdown destination is a path resolved against the note holding
    # it, so both sides keep the `memory-vault/` prefix and drop the extension,
    # while a frontmatter ref and a wikilink are vault-relative and drop both.
    moved_by_ref = {
        Path(old).with_suffix("").as_posix(): Path(new).with_suffix("").as_posix()
        for old, new in moves
    }
    moved_by_resolved = {old: _vault_ref(new) for old, new in moves}
    known = {entry.path.with_suffix("").as_posix() for entry in entries}
    needles = _ref_needles(moves)
    edited: list[str] = []
    rewrites: list[_Rewrite] = []
    for entry in entries:
        rel = entry.path_key
        try:
            note = (root / Path(rel).relative_to("memory-vault")).resolve()
        except ValueError:
            continue
        if not note.is_file() or not note.is_relative_to(root):
            continue
        try:
            text = _read_exact(note)
        except (OSError, UnicodeDecodeError):
            # Unreadable means unrewritable. A note the index already skipped is
            # no reason to refuse closing a project, and there is nothing to
            # record for `restore_completed` either.
            continue
        folded = text.casefold()
        if rel not in moved_to and not any(needle in folded for needle in needles):
            continue
        after_rel = moved_to.get(rel, rel)
        new_text, _changes = rewrite_references(
            text, rel, after_rel, moved_by_ref, filename_index,
            moved_by_resolved=moved_by_resolved, known_notes=known,
        )
        if new_text == text:
            continue
        after = (root / Path(after_rel).relative_to("memory-vault")).resolve()
        edited.append(_vault_path(root, after))
        rewrites.append(_Rewrite(note, after, text, new_text))
    return edited, rewrites


def move_project_folder(
    root: Path, source: Path, destination: Path, *, workspace: str
) -> list[str]:
    """Repoint every reference into *source*, then move the folder to *destination*.

    *root* is the workspace vault root, the same root ControlPlane passes to
    :func:`complete_project_note`; *source* and *destination* are absolute folders
    inside it. The references are written before the move because resolving them
    needs the notes still at their old paths, and a failure at either step puts
    the vault back as it was: written notes are restored and any directories the
    move created are pruned. Returns the edited notes as ``memory-vault/…`` ids,
    at their paths after the move.
    """
    root = Path(root).resolve()
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_relative_to(root) or not destination.is_relative_to(root):
        raise ValueError("project folder is outside the vault")
    if not source.is_dir():
        raise ValueError(f"project folder {source.name} does not exist")
    if destination.exists():
        raise ValueError(f"a folder already exists at {destination.name}; refusing to move into it")
    entries = scan_vault(root, workspace=workspace, include_unheaded=True)
    edited, rewrites = _plan_reference_rewrites(
        root,
        entries,
        _folder_moves(entries, _vault_path(root, source), _vault_path(root, destination)),
    )
    try:
        _write_texts([(r.path_before, r.before, r.after) for r in rewrites])
    except OSError as exc:
        raise ValueError(f"could not rewrite the references to the project: {exc}") from exc
    created = _missing_ancestors(destination.parent, root)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    except OSError as exc:
        _unwrite_texts([(r.path_before, r.before) for r in rewrites])
        _prune_created(created)
        raise ValueError(f"could not move the project; it was put back: {exc}") from exc
    return edited
