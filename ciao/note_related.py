"""The vault notes connected to one note: a model-free, read-only lookup.

The nightly care run checks a stale note against outside sources, so two notes
that disagree about the same subject are never noticed. ``ciao note related``
hands the agent the notes around the one it is judging: those the target links
to, those linking to it, and those whose body names its title or an alias. Each
row carries the ``revision`` and ``due`` flag a ``note verify`` payload for that
note needs, so a neighbour the agent actually checked can be credited without
being planned again as its own stale item.

Nothing here writes. The scan is :func:`ciao.vault_index.scan_vault`, not the
FTS index, so no retrieval telemetry is recorded and the decay-by-disuse signal
is not distorted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ciao import memory_audit
from ciao import memory_receipts as mr
from ciao import note_receipts as nr
from ciao import note_verification as nv
from ciao import vault_index
from ciao.entity_types import load_entity_types
from ciao.vault_links import FRONTMATTER_RE
from ciao.vault_review import never_queued

logger = logging.getLogger(__name__)

RELATED_NOTES_LIMIT = 15
#: Titles and aliases shorter than this are not matched as mentions: two letters
#: of a name match half the vault.
MIN_MENTION_CHARS = 3
_MAX_LIMIT = 50

LINKS_TO = "links_to"  # the target's own related:/body links point at it
LINKED_FROM = "linked_from"  # it links to the target
MENTIONS = "mentions"  # its body names the target's title or an alias


@dataclass(frozen=True, slots=True)
class RelatedNote:
    relative_path: str  # vault-relative POSIX path
    title: str
    type: str
    relations: tuple[str, ...]  # subset of (LINKS_TO, LINKED_FROM, MENTIONS), in that order
    revision: str  # memory_receipts.content_revision(full text)
    due: bool  # the nightly worklist would plan it: stale for its type and no check settles it

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "title": self.title,
            "type": self.type,
            "relations": list(self.relations),
            "revision": self.revision,
            "due": self.due,
        }


@dataclass(frozen=True, slots=True)
class RelatedNotes:
    relative_path: str
    title: str
    revision: str
    related: tuple[RelatedNote, ...]
    truncated: int  # how many matches the limit left out

    def as_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "title": self.title,
            "revision": self.revision,
            "related": [note.as_dict() for note in self.related],
            "truncated": self.truncated,
        }


def _read(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        logger.debug("note related: %s is not readable UTF-8", path)
        return None


def _mention_pattern(names: list[str]) -> re.Pattern[str] | None:
    usable = sorted({name.strip() for name in names if len(name.strip()) >= MIN_MENTION_CHARS})
    if not usable:
        return None
    alternatives = "|".join(re.escape(name) for name in usable)
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


def _body(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return text[match.end() :] if match else text


def _due(
    root: Path, relative: str, entry: Any, revision: str, *, today: date, registry: Any
) -> bool:
    """Whether the nightly worklist would plan this note for verification.

    Both halves of the worklist's rule: the note's facts are stale for its type
    (a journal or log never is), and no recorded check settles this revision.
    A ``due`` that only asked the check state would invite a verdict on every
    journal day that names the subject.
    """
    try:
        mtime = (root / relative).stat().st_mtime
    except OSError:
        mtime = 0.0
    verification = memory_audit.note_verification(
        entry.type or "", entry.updated, mtime, today=today, registry=registry
    )
    if verification is None or not verification.stale:
        return False
    return nv.should_check(root, relative, revision, today=today)


def find_related_notes(
    vault_root: Path | str,
    relative_path: str,
    *,
    workspace: str,
    today: date,
    limit: int = RELATED_NOTES_LIMIT,
) -> RelatedNotes:
    """The notes connected to ``relative_path``, link relations first.

    Raises :class:`ciao.memory_receipts.MemoryReceiptError` for a path outside
    the vault, a symlink escape or something that is not a note.
    """
    root = nr.canonical_vault(vault_root)
    target = nr.resolve_note_path(root, relative_path)
    target_key = target.relative_to(root).as_posix()
    target_text = _read(target)
    if target_text is None:
        raise mr.MemoryReceiptError(f"the note is not readable UTF-8 text: {target_key}")
    bounded = max(1, min(_MAX_LIMIT, int(limit)))

    registry = load_entity_types(root)
    prefix = vault_index.VAULT_RENDER_PREFIX
    entries = vault_index.scan_vault(root, workspace=workspace, path_prefix=prefix)
    by_relative = {vault_index.vault_relative(entry.path_key, prefix): entry for entry in entries}
    entry = by_relative.get(target_key)
    title = (entry.title if entry is not None else "") or target.stem
    names = [title, *(entry.aliases if entry is not None else [])]
    pattern = _mention_pattern(names)
    target_rendered = entry.path_key if entry is not None else ""

    links_to = set(entry.related) if entry is not None else set()
    matches: list[tuple[int, str, RelatedNote]] = []
    for relative, other in by_relative.items():
        if relative == target_key or never_queued(relative):
            continue
        if vault_index.is_reserved_bookkeeping(Path(relative)):
            continue
        relations: list[str] = []
        if other.path_key in links_to:
            relations.append(LINKS_TO)
        if target_rendered and target_rendered in other.related:
            relations.append(LINKED_FROM)
        text = _read(root / relative)
        if text is None:
            continue
        if pattern is not None and pattern.search(_body(text)):
            relations.append(MENTIONS)
        if not relations:
            continue
        revision = mr.content_revision(text)
        note = RelatedNote(
            relative_path=relative,
            title=other.title or relative,
            type=other.type or "",
            relations=tuple(relations),
            revision=revision,
            due=_due(root, relative, other, revision, today=today, registry=registry),
        )
        linked = LINKS_TO in relations or LINKED_FROM in relations
        matches.append((0 if linked else 1, relative, note))

    matches.sort(key=lambda row: (row[0], row[1]))
    kept = tuple(note for _, _, note in matches[:bounded])
    return RelatedNotes(
        relative_path=target_key,
        title=title,
        revision=mr.content_revision(target_text),
        related=kept,
        truncated=len(matches) - len(kept),
    )
