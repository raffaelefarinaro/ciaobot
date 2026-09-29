"""Migrate an installed ``Workspace/Learnings.md`` onto the canonical record model.

Why this exists
---------------
``ciao/learning_records.py`` defines what a learning is, and the canonical
writer writes lines that model can read back. Installs that predate it hold
``Learnings.md`` files full of shapes that model has to be taught to read: a
``- [2024-05-01] debugging: … — confidence: high`` line, a bare bullet with no
structure, and the current ``- [key] [first → last] (xN) …`` line that carries
no identity at all. Left alone, each of those is an entry the writer cannot
attribute: a sighting of it mints a *new* record rather than adding to the one
that is already there, so recurrence silently restarts and the count the care
schedule promotes on stops meaning anything.

So the conversion is offered as a command rather than run silently on first
write. A file is the user's, the shapes in it are theirs, and "this rewrote
lines you wrote" is a thing an operator should see before it happens.

The three properties that make it safe
--------------------------------------
**Dry-run first.** Nothing is written without ``--apply``, and the preview is
the same computation the apply performs, not a separate description of it.

**Lossless.** Only the spans of recognized ``## Active`` entries are replaced,
and only when :func:`ciao.learning_records.migrate_learnings` renders something
different for them. Frontmatter, format notes, the ``## Promoted / Resolved``
section, blank lines, a byte-order mark and CRLF line endings all pass through
byte for byte, and an entry the parser could not read keeps its exact bytes
rather than being repaired. No entry is ever dropped: the migration only ever
adds the machine comment a line was missing.

**Reversible.** Every span this run rewrote is recorded — offset, original
text, replacement — in a timestamped receipt under ``<runtime>/migration/``, so
``--revert`` restores the exact original bytes rather than re-deriving what the
line probably said. Each recorded span is re-checked against the file before it
is touched, and a file with one mismatch is left entirely alone: a half-reverted
file is worse than an unreverted one.

Idempotence is a property of the model rather than of this command. A migrated
line renders as itself, so a second run finds nothing to change and writes
nothing — which is also the gate: there is no state to invalidate, because the
content itself says whether it has been done.

Why not ``commit_note_change``
------------------------------
``Workspace/Learnings.md`` is bookkeeping, not an entity note. It is written by
the unattended care run and by every ``[learnings]`` accept, and its undo log is
the migration receipt below, not a per-write journal row. The write therefore
takes the same per-file lock and the same atomic helper the proposal queue takes,
so it is serialized against a concurrent accept and cannot truncate the file.
The read is from bytes rather than ``read_text``, which translates newlines and
would have rewritten a CRLF file on the way in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    SECTION_ACTIVE,
    LearningDocument,
    parse_learnings,
    render_learning,
)
from ciao.memory_proposals import learnings_path
from ciao.memory_receipts import content_revision, queue_lock, write_queue_atomically

RECEIPT_VERSION = 1

RECEIPT_PREFIX = "learnings-"

# `learnings-<YYYYMMDD-HHMMSS>.json` under `<runtime>/migration/`. One file per
# applied run, never overwritten: a run's receipt is the only record of what that
# run did, and a second migration of the same vault is a different reverse map.
RECEIPT_STAMP = "%Y%m%d-%H%M%S"


# ---- receipt ---------------------------------------------------------------


def _receipt_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "migration"


def new_receipt_path(runtime_root: Path) -> Path:
    """A free timestamped receipt path, one per run.

    The stamp has one-second resolution and two runs inside the same second would
    otherwise name the same file, so a name already taken moves to the next free
    second rather than overwriting a reverse map that is the only way back.
    """
    directory = _receipt_dir(runtime_root)
    moment = datetime.now(UTC)
    for _ in range(60):
        path = directory / f"{RECEIPT_PREFIX}{moment.strftime(RECEIPT_STAMP)}.json"
        if not path.exists():
            return path
        moment = datetime.fromtimestamp(moment.timestamp() + 1, UTC)
    return directory / f"{RECEIPT_PREFIX}receipt.json"


def read_receipt(path: Path) -> dict[str, Any] | None:
    """A migration receipt, or ``None`` when it cannot be read.

    A receipt that is missing, unparseable, not an object, or written by a
    different schema is reported as absent rather than half-trusted: reversing
    from a reverse map this code does not fully understand would restore spans
    against a file it has not checked.
    """
    receipt = Path(path)
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != RECEIPT_VERSION:
        return None
    return data


def write_receipt(path: Path, summary: dict[str, Any]) -> Path:
    """Persist the reverse map through a temp file and ``os.replace``.

    A half-written receipt is worse than none: `--revert` restores part of a file
    from it, and the operator has no way to tell which part. A run whose write
    failed records nothing at all, so the receipt only ever claims spans that are
    on disk.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECEIPT_VERSION,
        "migrated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "vault_root": summary.get("vault_root", ""),
        "path": summary.get("path", ""),
        "entries_scanned": summary.get("entries_scanned", 0),
        "entries_migrated": summary.get("entries_migrated", 0),
        "revision_before": summary.get("revision", ""),
        "rewrites": summary.get("rewrites", []),
        "diagnostics": summary.get("diagnostics", []),
    }
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return target


# ---- reading ---------------------------------------------------------------


def learnings_file(vault_root: Path) -> Path:
    """Where this workspace's learnings live — the writer's own answer, not a second one."""
    return learnings_path(vault_root)


def _load(vault_root: Path) -> str:
    """The file's text, or the empty string when there is no file.

    Decoded from bytes rather than `Path.read_text`, because `read_text`
    translates newlines: a CRLF file would come back with every line ending
    converted to `\n` and be written out that way, so "the migration preserves
    the line endings" would be true of the preview and false of the file. UTF-8
    without a BOM strip, because a byte-order mark is a file property and the
    model treats it as one.

    A missing file is an empty document here and the command reports it — there
    is nothing to migrate in a file that was never written.
    """
    path = learnings_file(vault_root)
    if not path.is_file():
        return ""
    return path.read_bytes().decode("utf-8")


def _rewrite(text: str, document: LearningDocument) -> tuple[str, list[dict[str, Any]]]:
    """The migrated text, and every span this run would rewrite.

    Both halves come out of one pass over the parsed entries, so the bytes about
    to be written and the spans about to be recorded cannot describe different
    edits. That is the whole reason this exists beside
    :func:`ciao.learning_records.migrate_learnings`: the model is the reference
    for *what* a migrated file is (and the tests hold the two to the same
    answer), but a receipt is only an exact reverse map if its offsets were
    computed against the text that was actually produced.

    Each change carries its ``offset`` in the **migrated** text plus the exact
    ``from``/``to`` strings, which is the direction ``--revert`` walks. A
    receipt carrying original-text offsets would make every span after the first
    one land on the wrong bytes. The shift is the running total of how much each
    earlier replacement grew or shrank the file, so the entries are walked in
    document order.
    """
    parts: list[str] = []
    changes: list[dict[str, Any]] = []
    last = 0
    shift = 0
    for entry in document.entries:
        if entry.section != SECTION_ACTIVE or entry.record is None:
            continue
        rendered = render_learning(entry.record)
        if rendered == entry.source_text:
            continue
        parts.append(text[last : entry.start])
        parts.append(rendered)
        changes.append(
            {
                "offset": entry.start + shift,
                "from": entry.source_text,
                "to": rendered,
                "format": entry.format,
            }
        )
        shift += len(rendered) - len(entry.source_text)
        last = entry.end
    if not changes:
        return text, []
    parts.append(text[last:])
    return "".join(parts), changes


# ---- the migration ---------------------------------------------------------


def migrate_learnings_file(
    vault_root: Path, *, workspace: str, apply: bool = False
) -> dict[str, Any]:
    """Rewrite the recognized Active entries of one ``Learnings.md``.

    With ``apply=False`` (the default) nothing is written and the summary is the
    diff preview. Idempotent: a migrated line renders as itself, so a second run
    over the same file finds no changes and leaves the file alone.

    A file this cannot read is reported rather than rewritten. A read error is
    not a licence to replace content with the stub — that is how a real file gets
    lost to a tool that believed the failure was the absence of a file.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "path": LEARNINGS_RELATIVE,
        "applied": bool(apply),
        "entries_scanned": 0,
        "entries_migrated": 0,
        "rewrites": [],
        "diagnostics": [],
        "failed": [],
    }
    path = learnings_file(root)
    if not path.is_file():
        summary["skipped"] = f"{LEARNINGS_RELATIVE} does not exist"
        return summary
    try:
        text = _load(root)
    except (OSError, UnicodeDecodeError) as exc:
        summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
        return summary
    summary["revision"] = content_revision(text)

    document = parse_learnings(text, workspace=workspace)
    migrated, changes = _rewrite(text, document)
    summary["entries_scanned"] = len(document.entries)
    summary["diagnostics"] = list(document.diagnostics)
    summary["rewrites"] = changes
    summary["entries_migrated"] = len(changes)
    if not changes:
        return summary

    if apply:
        # The bytes written are the ones the receipt's offsets address, from the
        # same pass that recorded them: a receipt describing one edit while the
        # file holds another is worse than no receipt, because `--revert` would
        # then restore bytes nobody ever wrote.
        try:
            _write_locked(path, migrated, expect=summary["revision"])
        except _RevisionMoved:
            summary["failed"].append(
                {
                    "path": LEARNINGS_RELATIVE,
                    "error": "the file changed while the migration was running",
                }
            )
            return summary
        except OSError as exc:
            summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
            return summary
        summary["revision"] = content_revision(migrated)
    return summary


class _RevisionMoved(RuntimeError):
    """The file changed between this command reading it and writing it."""


def _write_locked(path: Path, text: str, *, expect: str = "") -> None:
    """Replace ``Learnings.md`` atomically, under the per-file queue lock.

    The same lock and the same atomic helper the proposal queue uses, because
    this file is written by the unattended care run and by every ``[learnings]``
    accept — and one helper means one place where a partial write is impossible.
    The lock is what makes the read this command did still describe the text it
    is about to replace: without it, an accept landing in between would be
    discarded by the rename. ``expect`` is the revision the caller computed from
    that read, re-checked inside the lock, so a concurrent write is reported
    rather than overwritten.

    The read side of the round trip is bytes (:func:`_load`), which is where
    ``read_text``'s newline translation would have rewritten a CRLF file. What
    the writer does with a ``\\r\\n`` already in the string is the platform's
    ``os.linesep`` substitution, so the pair is exact on the POSIX platforms this
    app runs on and `test_bom_and_crlf_survive_the_whole_round_trip` says so.
    """
    with queue_lock(path):
        if expect:
            current = path.read_bytes().decode("utf-8")
            if content_revision(current) != expect:
                raise _RevisionMoved
        write_queue_atomically(path, text)


# ---- the reverse -----------------------------------------------------------


def unmigrate_learnings_file(
    vault_root: Path, receipt: dict[str, Any], *, apply: bool = False
) -> dict[str, Any]:
    """Restore the pre-migration text of every span in ``receipt``.

    Exact rather than re-derived: the spans are put back from the recorded bytes,
    so a line the owner wrote is not reconstructed by guessing. Back to front per
    file, so the recorded offsets stay valid as the text grows. A file whose
    current text disagrees with the receipt at any offset is reported and left
    **entirely** untouched.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "receipt_vault_root": str(receipt.get("vault_root", "")),
        "path": str(receipt.get("path", LEARNINGS_RELATIVE)),
        "applied": bool(apply),
        "entries_reverted": 0,
        "reverted": [],
        "failed": [],
    }
    rewrites = receipt.get("rewrites")
    if not isinstance(rewrites, list) or not rewrites:
        summary["skipped"] = "receipt records no rewrites to reverse"
        return summary

    path = learnings_file(root)
    if not path.is_file():
        summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": "file is missing"})
        return summary
    try:
        text = _load(root)
    except (OSError, UnicodeDecodeError) as exc:
        summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
        return summary

    restored = text
    mismatch = False
    for change in sorted(
        (item for item in rewrites if isinstance(item, dict)),
        key=lambda item: int(item.get("offset", 0)),
        reverse=True,
    ):
        offset = int(change.get("offset", 0))
        migrated, original = str(change.get("to", "")), str(change.get("from", ""))
        if restored[offset : offset + len(migrated)] != migrated:
            summary["failed"].append(
                {
                    "path": LEARNINGS_RELATIVE,
                    "offset": offset,
                    "error": "the file changed since the migration",
                }
            )
            mismatch = True
            break
        restored = restored[:offset] + original + restored[offset + len(migrated) :]
    if mismatch or restored == text:
        return summary

    if apply:
        try:
            _write_locked(path, restored, expect=content_revision(text))
        except _RevisionMoved:
            summary["failed"].append(
                {"path": LEARNINGS_RELATIVE, "error": "the file changed while reverting"}
            )
            return summary
        except OSError as exc:
            summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
            return summary
    summary["entries_reverted"] = len(rewrites)
    summary["reverted"].append(LEARNINGS_RELATIVE)
    return summary
