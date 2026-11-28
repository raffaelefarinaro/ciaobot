"""The open skill-edit proposals of one workspace, for Memory · Skills.

A skill edit is a proposal file, `<vault>/Workspace/Proposals/<slug>.md`, that an
agent writes and `ciao proposal-check` validates (:mod:`ciao.proposal_files`).
This module lists the ones still waiting on a person: `type: skill-edit` with
`status: draft` or `ready`. It only reads. Approving one is an attended chat
and rejecting one sets its `status:` line through the file viewer's save, as
the care page's proposal review already does.

Reading follows the care report's rules (:mod:`ciao.care_report`): `Workspace`
and `Proposals` are opened without following a link, each file is opened
relative to its folder with `O_NOFOLLOW`, must be a regular file with one link,
and is read up to the proposal size cap. Never raises. A file holding
credential-shaped text (by `redact_text`'s rules, on each line, each decoded
field and each word) is listed with its path only (`withheld`), so the value
never reaches the page.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any

from ciao import proposal_files
from ciao.care_report import _fd_relative_supported, _is_link, _read_bounded
from ciao.redact import redact_text

PROPOSALS_PARTS = ("Workspace", proposal_files.PROPOSALS_DIR)
OPEN_STATUSES = ("draft", "ready")
SKILL_EDIT = "skill-edit"
# A real folder holds a handful. At most MAX_ITEMS open skill edits are listed,
# and at most MAX_READS proposal files are read to find them (applied,
# rejected and other kinds count toward the reads only).
MAX_ITEMS = 200
MAX_READS = 1000
_WORD_EDGES = "\"'`()[]{}<>,;."
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.md")
_TEXT_FIELDS = ("target", "title", "summary", "rationale", "proposed_change")


def _names_fd_relative(vault_root: Path) -> list[str]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    dir_fd = os.open(vault_root, os.O_RDONLY | directory)
    try:
        for folder in PROPOSALS_PARTS:
            next_fd = os.open(folder, os.O_RDONLY | directory | nofollow, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        return os.listdir(dir_fd)
    finally:
        os.close(dir_fd)


def _names_by_path(vault_root: Path) -> list[str]:
    folder = vault_root.resolve()
    for name in PROPOSALS_PARTS:
        folder = folder / name
        info = os.lstat(folder)
        if _is_link(info) or not stat_module.S_ISDIR(info.st_mode):
            raise NotADirectoryError(str(folder))
    return os.listdir(folder)


def _list_names(vault_root: Path) -> tuple[bool, list[str]]:
    """Whether the folder is there, and the proposal-shaped file names in it, sorted."""
    try:
        names = _names_fd_relative(vault_root) if _fd_relative_supported() else _names_by_path(vault_root)
    except FileNotFoundError:
        return False, []
    except (OSError, ValueError):
        # A link at `Workspace` or `Proposals`, not a folder, unreadable.
        return False, []
    return True, sorted(name for name in names if _SLUG.fullmatch(name))


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _date(value: object) -> str:
    if isinstance(value, _dt.datetime):
        return ""
    if isinstance(value, _dt.date):
        return value.isoformat()
    return value if isinstance(value, str) else ""


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [entry for item in value for entry in _strings(item)]
    if isinstance(value, dict):
        return [entry for item in value.values() for entry in _strings(item)]
    return []


def _shaped(text: str) -> bool:
    return redact_text(text) != text


def _credential_shaped(text: str, fields: dict[str, Any], body: str) -> bool:
    """Whether the file holds anything `redact_text` would redact.

    Checked on each raw line (what `ciao proposal-check` checks), and again on
    each decoded front matter string and each word of it and of the body, so a
    token behind a harmless label (`key is <blob>`) or one spelled with YAML
    escapes is caught too. A short token with no credential shape still passes:
    these are `redact_text`'s rules, not a secret scanner.
    """
    if any(_shaped(line) for line in text.splitlines()):
        return True
    for value in [*_strings(fields), body]:
        if _shaped(value):
            return True
        if any(_shaped(word.strip(_WORD_EDGES)) for word in value.split()):
            return True
    return False


def _item(name: str, text: str) -> dict[str, Any] | None:
    """One file as the page shows it, or None when it is not an open skill edit."""
    parsed = proposal_files.read_front_matter(text)
    if parsed is None:
        return None
    fields, body = parsed
    if fields.get("type") != SKILL_EDIT or fields.get("status") not in OPEN_STATUSES:
        return None
    path = "/".join((*PROPOSALS_PARTS, name))
    problems = proposal_files.check(text, filename=name, parent=proposal_files.PROPOSALS_DIR)
    if _credential_shaped(text, fields, body):
        return {
            "path": path, "withheld": True, "status": fields["status"], "created": _date(fields.get("created")),
            "type": SKILL_EDIT, "target": "", "title": "", "summary": "", "rationale": "",
            "proposed_change": "", "evidence": [], "body": "", "problems": problems,
        }
    evidence = fields.get("evidence")
    listed = evidence if isinstance(evidence, list) else [evidence] if isinstance(evidence, str) else []
    item: dict[str, Any] = {name_: _text(fields.get(name_)).strip() for name_ in _TEXT_FIELDS}
    item.update({
        "path": path,
        "withheld": False,
        "type": SKILL_EDIT,
        "status": fields["status"],
        "created": _date(fields.get("created")),
        "evidence": [entry.strip() for entry in listed if isinstance(entry, str) and entry.strip()][
            : proposal_files.MAX_EVIDENCE_ITEMS
        ],
        "body": body.strip(),
        "problems": problems,
    })
    return item


def read_skill_proposals(vault_root: Path) -> dict[str, Any]:
    """The open skill-edit proposals under `<vault_root>/Workspace/Proposals/`. Never raises.

    `found` is false when the folder is missing or is not a plain folder;
    `skipped` counts proposal-named files that could not be read safely
    (a link, too large, unreadable). Items are newest `created` first.
    `truncated` is true when the list stopped at `MAX_ITEMS` open skill edits
    or after `MAX_READS` files.
    """
    found, names = _list_names(vault_root)
    items: list[dict[str, Any]] = []
    skipped = 0
    reads = 0
    truncated = False
    for name in names:
        if len(items) >= MAX_ITEMS or reads >= MAX_READS:
            truncated = True
            break
        reads += 1
        try:
            status, raw, _mtime = _read_bounded(vault_root, (*PROPOSALS_PARTS, name))
        except (OSError, ValueError, RuntimeError):
            skipped += 1
            continue
        if status != "ok":
            skipped += 1
            continue
        item = _item(name, raw.decode("utf-8", errors="replace"))
        if item is not None:
            items.append(item)
    items.sort(key=lambda entry: entry["path"])
    items.sort(key=lambda entry: entry["created"], reverse=True)
    return {
        "found": found,
        "items": items,
        "counts": {"waiting": len(items)},
        "skipped": skipped,
        "truncated": truncated,
    }
