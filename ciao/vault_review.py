"""Deterministic, reversible review workflow for vault notes.

This module deliberately keeps review state outside the search index.  The
ledger is append-only JSONL and the readable queue is a projection of it.  A
candidate is identified by workspace, vault-relative path, and content hash,
so editing a note cannot accidentally inherit an old destructive decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import tempfile
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ciao.vault_index import canonical_type, scan_vault
from ciao.vault_lint import is_template_stem, run_validation

RETENTION_DAYS = 30
MAX_CANDIDATES = 5
REVIEW_STATUSES = frozenset({"candidate", "reviewed", "archived", "trashed", "deleted"})
# No ``archive``: nothing here moves or marks an archived note, so accepting it
# wrote a ledger row, left the note exactly where it was, and then suppressed
# the candidate for good — a decision the caller was told had been carried out.
# Archiving a note is an ordinary vault edit; the review workflow owns only the
# reversible trash and the attended permanent deletion.
# No ``defer`` either. It snoozed a row for N days, which is what leaving the
# row alone already does — an untouched candidate stays in the queue and asks
# again every time you open it. What the snooze bought was a temporarily
# shorter list, and it charged a number spinner on every row for it.
DISPOSITIONS = frozenset({"keep", "improve_link", "trash", "restore", "delete"})
DECISION_DISPOSITIONS = frozenset({"keep", "improve_link"})
_SUPERSEDED_RE = re.compile(r"\b(?:superseded|deprecated|obsolete|replaced by|moved to)\b", re.I)
# Where a note is allowed to say it was superseded: its frontmatter and its
# opening prose, before the first section heading.
#
# The phrase used to be searched across the whole file, which reads any mention
# of supersession as a claim about the note itself. That put a wedding plan in
# the queue for a log line saying the guest table "moved to" a CSV, a live
# tech-stack page for a "replaced by" column, and — the clearest case — the
# decision archive whose whole job is to record superseded decisions. All three
# are hubs, so they arrived with a negative priority: ranked most disposable.
_SECTION_RE = re.compile(r"(?m)^#{2,6}[ \t]")
_HEAD_CHARS = 600
# A note that declares itself live is not claiming to be superseded, whatever a
# sentence further down mentions.
_ACTIVE_STATUS_RE = re.compile(
    r"(?mi)^status:[ \t]*[\"']?(active|planning|in[ -]progress|open|current|ongoing|todo|doing)\b"
)
# Types whose notes exist to be looked *up*, not linked *from*. Nothing in a
# vault links to a person or a bookmark as a matter of course, so `unlinked`
# describes the whole directory and says nothing about any note in it: 41 of
# one vault's 50 candidates were `People/*.md` flagged by that signal alone, in
# a queue whose terminal action is deletion. The signal is still recorded when
# something else independently flags the note — unlinked *and* superseded is a
# real finding — it just cannot put a note here by itself.
_LOOKUP_TYPES = frozenset({"person", "place", "resource", "reference"})
# A dated record of what happened on one day is never superseded: nothing
# replaces 2026-07-24. Its prose is full of the vocabulary anyway — "all open
# items moved to Monday" is about the items, not the entry.
_RECORD_TYPES = frozenset({"journal"})
# Enough of the note to recognise it without opening it; the panel shows the
# first lines inline and keeps the disclosure for the rest.
EXCERPT_CHARS = 280
_FRONTMATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_HEADING_RE = re.compile(r"\A#{1,6}[ \t]+[^\n]*\n")
# The excerpt renders as plain text in a queue row, not as markdown, so the
# syntax itself is noise there: a preview that reads "Copy this folder into
# `memory-vault/...`. ## Methodology" spends its budget on punctuation. Links
# keep their label and drop the URL, which is the half a human reads.
_MD_LINK_RE = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_MD_NOISE_RE = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|>[ \t]?)|[*_`]{1,2}")
_CANDIDATE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_QUEUE_LOCKS: dict[tuple[Path, str], threading.Lock] = {}
_QUEUE_LOCKS_GUARD = threading.Lock()


def _workspace_dir(root: Path) -> Path:
    path = Path(root) / "Workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ledger_path(root: Path) -> Path:
    return Path(root) / "Workspace" / "Vault-Review.jsonl"


def queue_path(root: Path) -> Path:
    return Path(root) / "Workspace" / "Vault-Review.md"


def trash_dir(root: Path) -> Path:
    return Path(root) / "Workspace" / ".vault-trash"


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def candidate_id(workspace: str, path: str, digest: str) -> str:
    raw = f"{workspace}\0{Path(path).as_posix()}\0{digest}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    candidate_id: str
    workspace: str
    path: str
    content_hash: str
    signals: tuple[str, ...]
    priority: int
    evidence: dict[str, Any]
    status: str = "candidate"
    disposition: str = ""
    deferred_until: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _append(root: Path, payload: dict[str, Any]) -> None:
    _workspace_dir(root)
    path = ledger_path(root)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": _now(), **payload}, sort_keys=True) + "\n")


def read_ledger(root: Path) -> list[dict[str, Any]]:
    path = ledger_path(root)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _latest_decisions(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in read_ledger(root):
        if row.get("candidate_id"):
            latest[str(row["candidate_id"])] = row
    return latest


def _suppressed(decision: dict[str, Any]) -> bool:
    if decision.get("content_hash") == "":
        return False
    disposition = decision.get("disposition")
    # `improve_link` suppresses like `keep`. It used to fall through to False,
    # so the row the user had just acted on stayed exactly where it was: the
    # button wrote a ledger line and looked broken. The content-hash guard above
    # still re-raises the note the moment it is edited, which is the same
    # protection `keep` has.
    return disposition in {"keep", "improve_link", "trash", "delete"}


def _says_it_was_superseded(text: str) -> bool:
    """Whether the note says *it* was superseded, rather than mentioning the idea.

    A note announces its own retirement at the top — in frontmatter, or in the
    lead paragraph under the title. Further down it is writing about something
    else: a log entry, a status column, an archive of other decisions.
    """
    frontmatter = _FRONTMATTER_RE.match(text)
    head = frontmatter.group(0) if frontmatter else ""
    if _ACTIVE_STATUS_RE.search(head):
        return False
    body = text[len(head):].lstrip()
    body = _HEADING_RE.sub("", body, count=1).lstrip()
    section = _SECTION_RE.search(body)
    lead = body[: section.start()] if section else body
    return bool(_SUPERSEDED_RE.search(head) or _SUPERSEDED_RE.search(lead[:_HEAD_CHARS]))


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """The opening prose of a note, for a row that must be judged at a glance.

    Frontmatter and the leading heading come off because both are already on the
    row — the title above it, the tags and dates in the meta line. What is left
    is stripped of markdown syntax and collapsed to single spaces: the row shows
    this as plain text, so a note whose first paragraph is a bulleted list would
    otherwise spend its budget on newlines, hashes and URLs.
    """
    body = _FRONTMATTER_RE.sub("", text, count=1).lstrip()
    body = _HEADING_RE.sub("", body, count=1).lstrip()
    body = _MD_LINK_RE.sub(r"\1", body)
    body = _MD_NOISE_RE.sub("", body)
    flat = " ".join(body.split())
    if len(flat) <= limit:
        return flat
    # Cut on a word boundary when one is near the end, so the preview does not
    # stop mid-word for the sake of eight characters.
    head = flat[:limit]
    space = head.rfind(" ")
    if space > limit - 40:
        head = head[:space]
    return f"{head.rstrip()} …"


def _write_queue(root: Path, candidates: list[ReviewCandidate], decisions: dict[str, dict[str, Any]]) -> None:
    _workspace_dir(root)
    lines = ["# Vault Review", "", "Pending note-review candidates. This file is generated from the append-only ledger.", ""]
    for item in candidates:
        decision = decisions.get(item.candidate_id, {})
        if decision.get("content_hash") == item.content_hash and _suppressed(decision):
            continue
        reason = ", ".join(item.signals) or "weak provenance"
        lines.append(f"- `{item.path}` [{item.priority}] {reason} (candidate `{item.candidate_id}`)")
    destination = queue_path(root)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False
    ) as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, destination)


def _queue_lock(root: Path, workspace: str) -> threading.Lock:
    key = (root, workspace)
    with _QUEUE_LOCKS_GUARD:
        return _QUEUE_LOCKS.setdefault(key, threading.Lock())


def _generate_candidates(
    root: Path,
    *,
    workspace: str,
    max_candidates: int = MAX_CANDIDATES,
    now: datetime | None = None,
    write_queue: bool = True,
) -> list[ReviewCandidate]:
    """Generate explainable candidates without changing vault notes.

    ``write_queue`` refreshes the readable ``Workspace/Vault-Review.md``
    projection. Callers that promise to be read-only — the ``list``/``inspect``
    actions and ``GET /api/vault/review`` — pass ``False``: a listing that
    writes to the vault is a listing that cannot be trusted to be one.
    """
    root = Path(root).resolve()
    entries = scan_vault(root, workspace=workspace)
    validation = run_validation(root)
    # The linter reports paths relative to the vault root, while Entry IDs are
    # rendered in the shared ``memory-vault/`` namespace.
    def rendered(value: str) -> str:
        return value if value.startswith("memory-vault/") else f"memory-vault/{value}"

    orphans = {rendered(path) for path in validation.get("orphans", [])}
    duplicate_groups = [[rendered(path) for path in group] for group in validation.get("duplicates", [])]
    duplicate_by_path = {path: group for group in duplicate_groups for path in group}
    incoming: dict[str, list[str]] = {str(entry.path): [] for entry in entries}
    outbound: dict[str, list[str]] = {str(entry.path): [] for entry in entries}
    # ``entry.related`` already carries both the frontmatter refs and the body's
    # markdown links — `scan_vault` extends it with `_extract_body_links` — so
    # one pass over it is the whole graph. An earlier revision re-read every
    # note to walk `_links_in` as well; that loop compared extension-less refs
    # against `.md`-suffixed keys, so it never matched, and only cost a second
    # full read of the vault.
    for entry in entries:
        source = str(entry.path)
        for target in entry.related:
            target_path = str(target)
            if target_path in incoming:
                outbound[source].append(target_path)
                incoming[target_path].append(source)
    today = (now or datetime.now(UTC)).date()
    candidates: list[ReviewCandidate] = []
    for entry in entries:
        path = str(entry.path)
        if any(part.casefold() == "workspace" for part in Path(path).parts):
            continue
        # A template is not a stale note: it has no facts to verify and nothing
        # links to it by design, so every rule here fires on one. The linter
        # already exempts them from duplicate detection for the same reason.
        if is_template_stem(Path(path).stem):
            continue
        try:
            disk_path = root / Path(path).relative_to("memory-vault")
            raw = disk_path.read_bytes()
        except (OSError, ValueError):
            continue
        text = raw.decode("utf-8", errors="replace")
        note_type = entry.type or "note"
        # `entry.type` is the raw frontmatter string. The two type filters below
        # decide whether a note may be queued at all — in a queue whose terminal
        # action is deletion — so they must not be defeated by a spelling:
        # `type: Person` is not in `_LOOKUP_TYPES`, and `type: hackathon-log`
        # aliases to `journal`. Display keeps the raw value.
        canon_type = canonical_type(note_type) or note_type
        signals: list[str] = []
        if path in orphans and not entry.related:
            signals.append("unlinked")
        group = duplicate_by_path.get(path)
        if group:
            signals.append("possible_duplicate")
        if canon_type not in _RECORD_TYPES and _says_it_was_superseded(text):
            signals.append("superseded_language")
        if not (entry.updated or entry.tags or entry.aliases):
            signals.append("weak_provenance")
        if not signals:
            continue
        if canon_type in _LOOKUP_TYPES and signals == ["unlinked"]:
            continue
        digest = content_hash(raw)
        evidence = {
            "backlinks": sorted(incoming.get(path, [])),
            "outbound_links": sorted(outbound.get(path, [])),
            "bridge": len(incoming.get(path, [])) + len(outbound.get(path, [])) >= 4,
            "duplicate_group": group or [],
            "last_update": entry.updated or "",
            "type": note_type,
            "age_days": None,
            # Carried in the queue payload rather than fetched per row: the
            # panel used to lazy-load the whole file through
            # `/api/workspace-file` behind a disclosure, which is why nothing
            # was visible until you opened fifty of them one at a time.
            "excerpt": _excerpt(text),
        }
        if entry.updated:
            try:
                evidence["age_days"] = max(0, (today - datetime.fromisoformat(entry.updated).date()).days)
            except ValueError:
                pass
        backlinks = cast(list[str], evidence["backlinks"])
        # Connectedness makes a note LESS disposable, so both connectedness
        # terms subtract. An earlier revision added +2 for `bridge` while
        # subtracting for backlinks: the two rules contradicted, and a hub with
        # four outbound links and no backlinks outranked genuine orphans for
        # the five candidate slots of a workflow whose terminal action is
        # deletion.
        priority = len(signals) - min(len(backlinks), 2) - (1 if evidence["bridge"] else 0)
        item = ReviewCandidate(
            candidate_id=candidate_id(workspace, path, digest), workspace=workspace,
            path=path, content_hash=digest, signals=tuple(sorted(signals)),
            priority=priority, evidence=evidence,
        )
        candidates.append(item)
    candidates.sort(key=lambda item: (-item.priority, item.path))
    decisions = _latest_decisions(root)
    active = [item for item in candidates if not _suppressed(decisions.get(item.candidate_id, {})) or decisions.get(item.candidate_id, {}).get("content_hash") != item.content_hash]
    result = active[: max(1, min(int(max_candidates), 50))]
    if write_queue:
        _write_queue(root, result, decisions)
    return result


def generate_candidates(
    root: Path,
    *,
    workspace: str,
    max_candidates: int = MAX_CANDIDATES,
    now: datetime | None = None,
    write_queue: bool = True,
) -> list[ReviewCandidate]:
    """Generate candidates with one consistent snapshot per workspace."""
    root = Path(root).resolve()
    with _queue_lock(root, workspace):
        return _generate_candidates(
            root,
            workspace=workspace,
            max_candidates=max_candidates,
            now=now,
            write_queue=write_queue,
        )


_FRONTMATTER_DELIM = "---"
_UPDATED_KEY_RE = re.compile(r"^updated:\s*(.*)$")


def _stamp_updated(text: str, today: str) -> str | None:
    """Return *text* with frontmatter ``updated:`` set to *today*.

    Surgical: every other byte of the document survives, frontmatter comments
    and key order included. Round-tripping through a YAML dumper would reorder
    and reflow notes people write by hand.

    ``None`` means "nothing to write": the note already carries today's date,
    has no frontmatter to stamp, or opens a block it never closes. A note with
    no frontmatter does not get one invented here — that is a lint finding of
    its own, and pressing *Still true* should not restructure a file.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None
    close = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM),
        None,
    )
    if close is None:
        return None
    # Splitting on "\n" leaves a CRLF file's "\r" on every line, so a bare
    # "updated: …" would be the one LF-terminated line in the block. Harmless
    # to YAML, but it makes the note a whole-file diff the next time a
    # CRLF-normalising editor saves it — and the contract above is that every
    # other byte survives.
    eol = "\r" if lines[0].endswith("\r") else ""
    stamped = f"updated: {today}{eol}"
    for index in range(1, close):
        match = _UPDATED_KEY_RE.match(lines[index])
        if not match:
            continue
        if match.group(1).strip().strip("\"'") == today:
            return None
        # Always a plain scalar date, so one line is the whole value.
        return "\n".join(lines[:index] + [stamped] + lines[index + 1:])
    # Appended rather than prepended: `type:`/`title:` conventionally lead the
    # block, and a new key at the bottom reads as the addition it is.
    return "\n".join(lines[:close] + [stamped] + lines[close:])


def _reverify(
    root: Path, candidate: ReviewCandidate, today: str
) -> tuple[str, bool]:
    """Stamp the note as verified today; return ``(hash, stamped)``.

    *Still true* used to write a ledger row and nothing else, so it cleared the
    queue while leaving the note exactly as unverified as it was — the Memory
    Map went on showing "needs review" and `memory-audit` went on listing it
    under stale notes. The button said it re-verified a note; now it does.

    The note's own hash is re-read first, for the same reason `trash_note`
    checks it: a note edited since the queue was generated is a different note,
    and stamping it would claim a verification of text nobody looked at.

    ``stamped`` is False when there was nothing to write — no frontmatter to
    stamp, an unterminated block, undecodable bytes, or a date already set to
    today. The caller still records the decision, because the user's intent
    ("this note is fine, stop asking") is honoured either way; it must not
    claim the stamp landed. A note with **no frontmatter** is the archetypal
    `weak_provenance` candidate, so reporting that case honestly is the
    difference between a button that half-worked and one that looks broken:
    the row clears while `memory-audit` and the Memory Map badge go on
    flagging the note, with no way to get it back into the queue.
    """
    note = (root / Path(candidate.path).relative_to("memory-vault")).resolve()
    if not note.is_relative_to(Path(root).resolve()):
        raise ValueError("note is outside the vault")
    try:
        raw = note.read_bytes()
    except OSError:
        return candidate.content_hash, False
    if content_hash(raw) != candidate.content_hash:
        raise ValueError("candidate changed or no longer exists; regenerate the review")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Decoding with errors="replace" and writing the result back would
        # rewrite every undecodable byte as U+FFFD — a destructive edit to a
        # note imported from a latin-1 source. Reading signals may be lossy;
        # a rewrite may not.
        return candidate.content_hash, False
    stamped = _stamp_updated(text, today)
    if stamped is None:
        return candidate.content_hash, False
    payload = stamped.encode("utf-8")
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=note.parent,
        prefix=f".{note.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    # A fresh temp file lands at 0600 and os.replace would silently tighten the
    # rewritten note's permissions; carry the old mode over, exactly as
    # `vault_index.apply_edits` does. A vault on a group-readable mount, or one
    # synced by another user or daemon, otherwise becomes unreadable to it
    # after a single click of "Still true".
    try:
        os.chmod(temporary, note.stat().st_mode & 0o7777)
    except OSError:
        pass
    os.replace(temporary, note)
    return content_hash(payload), True


def record_decision(root: Path, candidate: ReviewCandidate, disposition: str, *, actor: str = "user", now: datetime | None = None) -> dict[str, Any]:
    if disposition not in DECISION_DISPOSITIONS:
        raise ValueError(f"unsupported vault review disposition: {disposition}")
    digest = candidate.content_hash
    stamped = False
    if disposition == "keep":
        digest, stamped = _reverify(
            root, candidate, (now or datetime.now(UTC)).date().isoformat()
        )
    # The POST-stamp hash, so the row suppresses the note as it now stands. The
    # pre-stamp hash would name a version that no longer exists on disk, and the
    # candidate would come straight back with its own decision not matching it.
    #
    # ``deferred_until`` stays in the row, always empty: the field is part of the
    # candidate shape the API already publishes, and a historical ledger written
    # when snoozing existed still parses against it.
    row = {"candidate_id": candidate_id(candidate.workspace, candidate.path, digest), "workspace": candidate.workspace, "path": candidate.path, "content_hash": digest, "disposition": disposition, "actor": actor, "status": "reviewed", "deferred_until": ""}
    _append(root, row)
    # The ledger row is the durable record and keeps its existing shape; the
    # two extra fields are for the caller only. `stamped` lets the UI stop
    # promising a date it did not write, and `previous_candidate_id` returns
    # the id the caller actually asked about — `candidate_id` above is
    # recomputed from the POST-stamp hash, so an agent reusing it for a
    # follow-up `inspect`/`trash` would get `candidate_not_found`.
    result: dict[str, Any] = dict(row)
    result["stamped"] = stamped
    result["previous_candidate_id"] = candidate_id(
        candidate.workspace, candidate.path, candidate.content_hash
    )
    return result


def trash_note(root: Path, candidate: ReviewCandidate, *, actor: str = "user") -> dict[str, Any]:
    """Move one exact note to the reversible workspace trash."""
    root = Path(root).resolve()
    source = (root / Path(candidate.path).relative_to("memory-vault")).resolve()
    if not source.is_relative_to(root):
        raise ValueError("note is outside the vault")
    if not source.is_file() or content_hash(source.read_bytes()) != candidate.content_hash:
        raise ValueError("candidate changed or no longer exists; regenerate the review")
    destination = trash_dir(root) / f"{candidate.candidate_id}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    metadata = {"candidate_id": candidate.candidate_id, "workspace": candidate.workspace, "original_path": candidate.path, "content_hash": candidate.content_hash, "edited_backlinks": [], "trashed_at": _now()}
    try:
        shutil.move(str(source), str(destination))
        destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _append(root, {**metadata, "path": candidate.path, "disposition": "trash", "status": "trashed", "actor": actor})
    except OSError as exc:
        if destination.is_file() and not source.exists():
            shutil.move(str(destination), str(source))
        destination.with_suffix(".json").unlink(missing_ok=True)
        raise ValueError(f"trash failed; note was restored: {exc}") from exc
    return metadata


def restore_note(root: Path, candidate_id_value: str, *, actor: str = "user") -> dict[str, Any]:
    _validate_candidate_id(candidate_id_value)
    metadata_path = trash_dir(root) / f"{candidate_id_value}.json"
    if not metadata_path.is_file():
        raise ValueError("trashed candidate not found")
    metadata = cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))
    destination = (Path(root).resolve() / Path(str(metadata["original_path"]).replace("memory-vault/", "", 1))).resolve()
    source = trash_dir(root) / f"{candidate_id_value}.md"
    if not destination.is_relative_to(Path(root).resolve()):
        raise ValueError("restore path is outside the vault")
    if not source.is_file() or destination.exists() or content_hash(source.read_bytes()) != metadata["content_hash"]:
        raise ValueError("restore would overwrite data or the trashed note changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))
    try:
        # Keep the trash copy and metadata available until the restore audit is
        # durable; otherwise a read-only ledger could strand an untracked move.
        _append(root, {**metadata, "disposition": "restore", "status": "reviewed", "actor": actor})
    except OSError as exc:
        shutil.move(str(destination), str(source))
        raise ValueError(f"restore audit failed; note remains trashed: {exc}") from exc
    try:
        metadata_path.unlink()
    except OSError as exc:
        if destination.is_file() and not source.exists():
            shutil.move(str(destination), str(source))
        raise ValueError(f"restore metadata cleanup failed; note remains trashed: {exc}") from exc
    return metadata


def list_trashed(root: Path, *, workspace: str) -> list[dict[str, Any]]:
    """Read-only inventory of the reversible trash for one workspace.

    The trash view renders from this rather than from candidate generation:
    a trashed note is no longer in the vault, so it can never be a
    candidate again, and its only durable record is the ``.json`` sidecar
    `trash_note` wrote next to it. Scoped to one workspace; anything that
    is not a well-formed sidecar for a still-restorable note is skipped.
    """
    directory = trash_dir(root)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for metadata_path in sorted(directory.glob("*.json")):
        if not _CANDIDATE_ID_RE.fullmatch(metadata_path.stem):
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("workspace") or "") != workspace:
            continue
        if str(metadata.get("candidate_id") or "") != metadata_path.stem:
            continue
        if not (directory / f"{metadata_path.stem}.md").is_file():
            continue
        items.append(
            {
                "candidate_id": metadata_path.stem,
                "workspace": workspace,
                "original_path": str(metadata.get("original_path") or ""),
                "content_hash": str(metadata.get("content_hash") or ""),
                "trashed_at": str(metadata.get("trashed_at") or ""),
            }
        )
    items.sort(key=lambda item: item["trashed_at"])
    return items


def delete_permanently(root: Path, candidate_id_value: str, *, confirm: str, actor: str = "user") -> dict[str, Any]:
    _validate_candidate_id(candidate_id_value)
    if confirm != candidate_id_value:
        raise ValueError("explicit candidate confirmation is required")
    source = trash_dir(root) / f"{candidate_id_value}.md"
    metadata_path = trash_dir(root) / f"{candidate_id_value}.json"
    if not source.is_file() or not metadata_path.is_file():
        raise ValueError("only trashed notes can be permanently deleted")
    metadata = cast(dict[str, Any], json.loads(metadata_path.read_text(encoding="utf-8")))
    digest = content_hash(source.read_bytes())
    if digest != metadata.get("content_hash"):
        raise ValueError("trashed note changed; refusing permanent deletion")
    original = (Path(root).resolve() / Path(str(metadata["original_path"]).replace("memory-vault/", "", 1))).resolve()
    if not original.is_relative_to(Path(root).resolve()) or original.exists():
        raise ValueError("original note path is unavailable; refusing permanent deletion")
    original.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(original))
    except OSError as exc:
        raise ValueError(f"cannot delete: {exc}") from exc
    # Probe the target directory before any backlink is rewritten.
    # `strip_references` has to run while the note is still on disk — it
    # resolves refs against the vault's real files — so the rewrites are
    # committed before the unlink that ends the delete. Without the probe an
    # unwritable folder failed only at that unlink, by which point every live
    # link to the note had been dissolved: edits the rollback below cannot undo.
    # Same probe as the Memory Map's delete route.
    try:
        with tempfile.NamedTemporaryFile(dir=original.parent, prefix=".ciao-delete-", suffix=".probe"):
            pass
    except OSError as exc:
        shutil.move(str(original), str(source))
        raise ValueError(f"cannot delete: {exc}") from exc
    from ciao.vault_index import strip_references

    edited: list[str] = []
    undo: dict[str, str] = {}
    cleanup_error = ""
    try:
        edited = strip_references(
            Path(root), str(metadata["original_path"]), undo=undo
        )
    except OSError as exc:
        cleanup_error = str(exc)
    if cleanup_error:
        # The note is back where it was and nothing else was rewritten
        # (`strip_references` is all-or-nothing); leave it in the trash.
        shutil.move(str(original), str(source))
        raise ValueError(f"backlink cleanup failed: {cleanup_error}")
    try:
        # Keep the trash metadata until the completion audit is durable. If the
        # append fails, the note can still be restored from this recovery state.
        _append(root, {**metadata, "edited_backlinks": edited, "disposition": "delete", "status": "deleted", "actor": actor, "deleted_at": _now()})
    except OSError as exc:
        if original.is_file() and not source.exists():
            for path, text in undo.items():
                Path(path).write_text(text, encoding="utf-8")
            shutil.move(str(original), str(source))
        raise ValueError(f"delete audit failed; recovery metadata was retained: {exc}") from exc
    try:
        original.unlink()
    except OSError as exc:
        if original.is_file() and not source.exists():
            for path, text in undo.items():
                Path(path).write_text(text, encoding="utf-8")
            shutil.move(str(original), str(source))
        _append(root, {
            **metadata, "edited_backlinks": edited, "disposition": "delete",
            "status": "trashed", "actor": actor, "delete_failed_at": _now(),
            "delete_error": str(exc),
        })
        raise ValueError(f"cannot delete: {exc}") from exc
    metadata_path.unlink()
    return metadata


def _validate_candidate_id(value: str) -> None:
    if not _CANDIDATE_ID_RE.fullmatch(value):
        raise ValueError("invalid candidate id")
