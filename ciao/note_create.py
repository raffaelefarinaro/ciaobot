"""Create one vault note with the canonical frontmatter, in the right folder.

Agents used to write a note's YAML header by hand, so the canonical core
(``type``, ``title``, ``description``, ``tags``, ``created``, ``updated``) was
applied inconsistently. ``ciao note create`` routes through this module: it
resolves the type against the vault's category registry, picks the folder,
stamps the frontmatter and creates the file exclusively, so an existing note is
never overwritten and a refusal writes nothing.

Pure and synchronous. The control plane scopes the call to a workspace, reads
the prose files confined to that workspace and runs this off the event loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ciao import note_receipts
from ciao.entity_types import EntityType, EntityTypeRegistry, load_entity_types
from ciao.frontmatter import yaml_scalar
from ciao.memory_proposals import safe_note_stem
from ciao.vault_index import is_reserved_bookkeeping

MAX_NOTE_FILE_BYTES = 256 * 1024
MAX_DESCRIPTION_CHARS = 500
MAX_TITLE_CHARS = 200

_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/_-]*")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
MAX_FOLDER_PART_CHARS = 80
MAX_FOLDER_DEPTH = 8
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


class NoteCreateError(ValueError):
    """A refusal with a stable ``code`` the control plane maps one to one."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CreatedNote:
    """The note that was written."""

    relative_path: str
    path: str
    type: str
    title: str
    created: str

    def as_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "path": self.path,
            "type": self.type,
            "title": self.title,
            "created": self.created,
        }


def resolve_type(registry: EntityTypeRegistry, requested: str) -> EntityType:
    """The enabled, visible category *requested* names (an id or an alias)."""
    key = str(requested or "").strip().casefold()
    type_id = registry.aliases().get(key, key)
    entry = registry.get(type_id)
    if entry is None or not entry.enabled or entry.hidden:
        valid = ", ".join(sorted(e.id for e in registry.entries() if e.enabled and not e.hidden))
        raise NoteCreateError(
            "note_type_unknown",
            f"Unknown note type {requested!r}. Valid types: {valid}.",
        )
    return entry


def parse_tags(tags: list[str] | None, default: str) -> list[str]:
    """Validated, de-duplicated tags, or ``[default]`` when none were given."""
    cleaned: list[str] = []
    for raw in tags or []:
        tag = str(raw).strip()
        if not tag:
            continue
        if not _TAG_RE.fullmatch(tag):
            raise NoteCreateError(
                "note_tags_invalid",
                f"Invalid tag {tag!r}: use letters, digits, '/', '_' and '-', starting with a letter or digit.",
            )
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned or [default]


def normalize_description(text: str) -> str:
    """The description collapsed to one line, within the cap."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) > MAX_DESCRIPTION_CHARS:
        raise NoteCreateError(
            "note_file_invalid",
            f"The description is {len(collapsed)} characters, over the {MAX_DESCRIPTION_CHARS} limit.",
        )
    return collapsed


def render_note(
    type_id: str,
    title: str,
    description: str,
    tags: list[str],
    body: str,
    *,
    today: str,
) -> str:
    """The note's bytes: canonical frontmatter, an H1, then the body."""
    lines = ["---", f"type: {yaml_scalar(type_id)}", f"title: {yaml_scalar(title)}"]
    if description:
        lines.append(f"description: {yaml_scalar(description)}")
    lines.append(f"tags: [{', '.join(yaml_scalar(tag) for tag in tags)}]")
    lines += [f"created: {today}", f"updated: {today}", "---", "", f"# {title}"]
    text = "\n".join(lines) + "\n"
    stripped = body.strip("\n")
    if stripped:
        text += "\n" + stripped + "\n"
    return text


def _check_name_part(part: str, *, code: str, what: str) -> None:
    """Refuse a name Windows cannot hold, on every platform.

    A vault is synced between machines, and ``NUL.md`` or ``a:b`` would be
    silently discarded or rejected where the note lands next.
    """
    if (
        any(ch in '<>:"|?*' for ch in part)
        or part.endswith((".", " "))
        or part.split(".")[0].strip().upper() in _WINDOWS_RESERVED
    ):
        raise NoteCreateError(code, f"{what} {part!r} is not a valid file name on every platform.")


def _folder_parts(raw: str) -> list[str]:
    folder = str(raw or "")
    if (
        not folder
        or folder.startswith("/")
        or Path(folder).drive
        or "\\" in folder
        or "\x00" in folder
    ):
        raise NoteCreateError("note_path_invalid", f"Not a vault-relative folder: {raw!r}.")
    if folder.lower().endswith(".md"):
        raise NoteCreateError(
            "note_path_invalid", f"--path is a folder, not a file: {raw!r}."
        )
    parts = [part for part in folder.split("/") if part not in ("", ".")]
    if not parts:
        raise NoteCreateError("note_path_invalid", f"Not a vault-relative folder: {raw!r}.")
    for part in parts:
        if part == ".." or part.startswith("."):
            raise NoteCreateError(
                "note_path_invalid", f"A folder part may not be {part!r}: {raw!r}."
            )
        if len(part) > MAX_FOLDER_PART_CHARS:
            raise NoteCreateError(
                "note_path_invalid",
                f"A folder part is over {MAX_FOLDER_PART_CHARS} characters: {raw!r}.",
            )
        _check_name_part(part, code="note_path_invalid", what="The folder")
    if len(parts) > MAX_FOLDER_DEPTH:
        raise NoteCreateError(
            "note_path_invalid", f"A folder is at most {MAX_FOLDER_DEPTH} levels deep: {raw!r}."
        )
    return parts


def _check_title(title: str) -> str:
    if (
        not title.strip()
        or _CONTROL_RE.search(title)
        or len(title) > MAX_TITLE_CHARS
    ):
        raise NoteCreateError(
            "note_title_invalid",
            f"A title is 1 to {MAX_TITLE_CHARS} characters with no control characters.",
        )
    stem = safe_note_stem(title)
    if not stem or stem.startswith("."):
        raise NoteCreateError(
            "note_title_invalid", f"The title {title!r} cannot be a file name."
        )
    _check_name_part(stem, code="note_title_invalid", what="The title")
    return stem


def plan_destination(notes_root: Path, folder: str, title: str) -> tuple[Path, str]:
    """Where the note goes and its vault-relative path; creates nothing."""
    parts = _folder_parts(folder)
    stem = _check_title(title)
    try:
        root = note_receipts.canonical_vault(notes_root)
        current = root
        for part in parts:
            # Private on purpose: the vault's real spelling of a name decides the
            # path, the same walk resolve_note_path does for an existing note.
            current = note_receipts._on_disk_name(current, part)
            if current.is_symlink():
                raise NoteCreateError("note_path_invalid", f"{part!r} is a symlink.")
            if current.exists() and not current.is_dir():
                raise NoteCreateError("note_path_invalid", f"{part!r} is not a folder.")
        target = current / f"{stem}.md"
        rel = target.relative_to(root)
        if is_reserved_bookkeeping(rel):
            raise NoteCreateError(
                "note_path_invalid", f"{rel.as_posix()} is the app's own bookkeeping."
            )
        if target.exists() or target.is_symlink():
            raise NoteCreateError("note_exists", f"{rel.as_posix()} already exists.")
    except OSError as exc:
        raise NoteCreateError("note_path_invalid", f"Cannot use that folder: {exc}") from exc
    return target, rel.as_posix()


def create_note(
    notes_root: Path,
    registry_root: Path,
    *,
    note_type: str,
    title: str,
    description: str,
    tags: list[str] | None,
    body: str,
    path: str,
    today: date,
) -> CreatedNote:
    """Write one new note and return where it landed."""
    entry = resolve_type(load_entity_types(registry_root), note_type)
    folder = path if (path or "").strip() else entry.folder
    if not folder:
        raise NoteCreateError(
            "note_folder_required",
            f"The {entry.id!r} category has no folder; pass --path with a vault-relative folder.",
        )
    clean_tags = parse_tags(tags, entry.id)
    target, rel = plan_destination(notes_root, folder, title)
    stamp = today.isoformat()
    text = render_note(entry.id, title.strip(), description, clean_tags, body, today=stamp)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise NoteCreateError("note_exists", f"{rel} already exists.") from exc
    except OSError as exc:
        raise NoteCreateError("note_path_invalid", f"Could not write {rel}: {exc}") from exc
    return CreatedNote(
        relative_path=rel,
        path=str(target),
        type=entry.id,
        title=title.strip(),
        created=stamp,
    )
