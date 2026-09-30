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
from typing import Any, NamedTuple, cast
from urllib.parse import quote

from ciao.memory_audit import NoteVerification, note_verification
from ciao.vault_index import build_filename_index, canonical_type, scan_vault, temp_prefix
from ciao.vault_lint import is_template_stem, run_validation

# No retention window. A `RETENTION_DAYS = 30` constant sat here unread while
# three strings in the panel promised a note would be restorable "for 30 days"
# and one implied it could not be deleted before then — a policy with no
# purge behind it and no gate in `delete_permanently`. Enforcing it would have
# been the wrong repair: an automatic purge is the one thing this workflow
# refuses to do everywhere else, since the trash exists so that nothing leaves
# the vault unattended. Trashed notes stay until someone deletes them.
MAX_CANDIDATES = 5
# The most a single listing may return, and what the readable
# `Workspace/Vault-Review.md` projection always holds. It was 50 while the queue
# only knew link and wording signals; once age became a signal a real vault had
# ~70 candidates and the tail was silently cut off — including notes the Memory
# Map was telling the user to go and review there.
MAX_CANDIDATES_CEILING = 200
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
# No ``improve_link`` either. It cleared a row on the promise that you had
# re-linked the note from somewhere else — a repair nobody makes by hand, so
# the button was a third way to say "not now" wearing a claim about the vault.
# Historical ledger rows still carry it and still suppress; see ``_suppressed``.
# ``reopen`` undoes a ``keep``. It is the only way back into the queue: a kept
# note is suppressed until its bytes change, and the archetypal `weak_provenance`
# candidate — a note with no frontmatter — cannot be stamped at all, so `keep`
# clears its row while `memory-audit` and the Memory Map go on flagging it. That
# left the one irreversible act in a workflow whose whole claim is reversibility.
#
# ``vanished`` is written by the system, not a person: a note that left the
# vault by an ordinary file deletion simply stops being scanned, so the ledger —
# which presents itself as the durable record of what left — recorded nothing.
#
# ``complete`` is the one disposition that moves a note instead of taking it out
# of the vault: a project lands under `projects/completed/`, which `never_queued`
# already refuses to list, so a completed project cannot reappear as a candidate.
# It is terminal for the same reason `trash` is — the note is no longer where the
# row says it is — and unlike `trash` the note is still in the vault, so the row
# carries both paths and the inbound references that were rewritten to follow it.
DISPOSITIONS = frozenset({"keep", "reopen", "trash", "restore", "delete", "complete", "vanished"})
DECISION_DISPOSITIONS = frozenset({"keep"})
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
# The one type with an END, and so the only one `complete_project_note` acts on.
# A person, a place and a reference are not finished by anything; a project is,
# and the completed layout under `projects/completed/` is what the vault already
# means by that. `Retire` stays available for every type, including this one: a
# project note that is wrong or abandoned is retired, not completed.
PROJECT_TYPE = "project"
# The one frontmatter line a completion rewrites, with the anchor
# `ProjectChatManager.complete_project` uses for the PWA's own completion, so a
# project closed from the review panel and one closed from the Projects tab leave
# the same frontmatter. The same anchors as there — a `status:` line of its own,
# nothing looser, or the two paths could no longer be told apart.
#
# There is no reverse pattern. A completion records the note's whole original
# text as an undo image, and a restore writes that back, which puts the original
# `status: active` line back with it; a second substitution to undo a first one
# would be a way to get two answers for the same question.
#
# Applied by `_set_status` to the FRONTMATTER BLOCK ONLY, which is a deliberate
# narrowing of the PWA's whole-text substitution. A body line reading
# `status: active` — inside a fenced example, a pasted transcript, a sentence
# about a decision log — is prose, and rewriting it makes a note that was never
# closed claim that it was.
_STATUS_ACTIVE_RE = re.compile(r"(?m)^(status:\s*)active\s*$")
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
    # The vault this candidate was read from, so `as_dict` can ask the same
    # "is the destination free" question `complete_project_note` asks without
    # every one of its callers having to hand it a root. Never serialized: it
    # is an absolute path on the operator's disk and means nothing to a client.
    vault_root: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Dropped here rather than left for a caller to filter, so no route can
        # leak an operator's absolute path by forgetting to.
        payload.pop("vault_root", None)
        # Whether this row can be COMPLETED rather than only retired, decided
        # here so the panel never has to. Re-deriving project-ness in the UI
        # from `evidence.type` leaves two definitions of the same thing free to
        # disagree, and the failure is a Complete button the engine then
        # refuses — the worst kind, because the row still looks actionable.
        payload["completable"] = _is_completable(self)
        # Whether that completion takes a whole project folder with it. A bool
        # beside `completable` and never a path, for the reason `vault_root` is
        # dropped above: a row that dragged its folder says so, and a client
        # that re-derived the layout from the path would put a second
        # definition of "is this a folder project" next to the one that decides
        # what the move does.
        payload["completion_moves_folder"] = _completion_moves_folder(self)
        return payload


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
    # `improve_link` is no longer offered, but rows written while it was still
    # suppress: a note the user already cleared must not reappear because the
    # button behind it was retired. The content-hash guard above re-raises it
    # the moment the note is edited, exactly as it does for `keep`.
    #
    # `reopen` and `vanished` are absent by design. `reopen` exists to undo a
    # `keep`, so it must not suppress; `vanished` records that a note left the
    # vault, and if the file comes back it deserves to be judged again.
    #
    # `complete` belongs here for the same reason `trash` does: the note left the
    # path this row names. A completed project is already exempt from the queue
    # on its own (it sits under `projects/completed/`, which `never_queued`
    # refuses), so this is the second line of the same defence — a project that
    # was moved back by hand, or whose completed note was edited, must not be
    # judged against the decision that closed it. `restore_completed` supersedes
    # the row when it does happen, and the note comes back to the queue.
    return disposition in {"keep", "improve_link", "trash", "delete", "complete"}


_SUPERSEDED_LINE_CHARS = 300


def _is_workspace_path(path: str) -> bool:
    return any(part.casefold() == "workspace" for part in Path(path).parts)


def _is_completed_project(path: str) -> bool:
    parts = [part.casefold() for part in Path(path).parts]
    return any(a == "projects" and b == "completed" for a, b in zip(parts, parts[1:]))


def _is_project_candidate(candidate: ReviewCandidate) -> bool:
    """Whether this candidate is a project, and so has an end to be completed at.

    Two questions, both answered from what the candidate already carries — the
    evidence the queue built and the path it already read. Completion re-scans
    the vault for the *references* to the note, which is a different job, but
    deciding what a note IS is not something to pay a second scan for.

    A declared ``type:`` is the whole answer, and it is consulted first and
    alone: it is the user's own statement, and a ``type: person`` note filed
    under ``projects/`` is a person note they filed there, not a project. The
    path is the fallback for a note that declared nothing, which is the common
    case for the flat ``projects/<name>.md`` a project note is often written as
    — ``scan_vault`` infers ``project`` for a note under ``projects/active/``
    from the directory, but it has nothing to infer from ``projects/`` itself.

    The type is alias-resolved through the same ``canonical_type`` the queue
    already used, so a capitalised ``type: Project`` and a registered alias of it
    both count.
    """
    declared = str(candidate.evidence.get("type") or "").strip()
    if declared:
        return (canonical_type(declared) or declared).casefold() == PROJECT_TYPE
    return any(part.casefold() == "projects" for part in Path(candidate.path).parts)


def never_queued(path: str) -> bool:
    """Whether the review queue refuses to ever list this note, whatever it says.

    ``Workspace/`` queue files, templates and completed projects. Public so the
    Memory Map can leave its ``stale`` flag off the same notes: a map that
    counts notes as "unchecked" while the queue it sends you to can never show
    them is two lists disagreeing.
    """
    return _is_workspace_path(path) or is_template_stem(Path(path).stem) or _is_completed_project(path)


def _line_at(lines: list[str], index: int) -> str:
    return lines[index].strip().lstrip("\ufeff").strip()[:_SUPERSEDED_LINE_CHARS]


def _neighbour(lines: list[str], index: int, step: int) -> dict[str, Any] | None:
    """The nearest non-blank, non-delimiter line before/after *index*."""
    probe = index + step
    while 0 <= probe < len(lines):
        text = _line_at(lines, probe)
        if text and text != "---":
            return {"line": probe + 1, "text": text}
        probe += step
    return None


def _superseded_match(text: str) -> dict[str, Any] | None:
    """Where the note says *it* was superseded, rather than mentioning the idea.

    A note announces its own retirement at the top — in frontmatter, or in the
    lead paragraph under the title. Further down it is writing about something
    else: a log entry, a status column, an archive of other decisions.

    Returns the evidence the queue shows — the 1-based line, that line, the
    phrase as written, which region matched, and the nearest non-blank line on
    either side — or None. Offsets are tracked into the original text, so the
    line number is the one an editor shows (BOM and CRLF included).
    """
    frontmatter = _FRONTMATTER_RE.match(text)
    head = frontmatter.group(0) if frontmatter else ""
    if _ACTIVE_STATUS_RE.search(head):
        return None
    where = "frontmatter"
    match = _SUPERSEDED_RE.search(head)
    offset = match.start() if match else 0
    if match is None:
        rest = text[len(head):]
        start = len(head) + len(rest) - len(rest.lstrip())
        # `_HEADING_RE` is anchored with `\A`, which `match(text, pos)` would
        # never honour at `pos`, so it runs on the slice.
        heading = _HEADING_RE.match(text[start:])
        if heading:
            start += heading.end()
            after_heading = text[start:]
            start += len(after_heading) - len(after_heading.lstrip())
        body = text[start:]
        section = _SECTION_RE.search(body)
        lead = body[: section.start()] if section else body
        match = _SUPERSEDED_RE.search(lead[:_HEAD_CHARS])
        if match is None:
            return None
        where = "lead"
        offset = start + match.start()
    lines = text.split("\n")
    index = text.count("\n", 0, offset)
    return {
        "line": index + 1,
        "text": _line_at(lines, index),
        "match": match.group(0),
        "where": where,
        "before": _neighbour(lines, index, -1),
        "after": _neighbour(lines, index, 1),
    }


def _says_it_was_superseded(text: str) -> bool:
    """Whether the note says *it* was superseded; see `_superseded_match`."""
    return _superseded_match(text) is not None


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
    """Refresh the readable projection of the pending queue.

    The header used to say the file was "generated from the append-only
    ledger", which names the wrong source: the rows come from a live scan of
    the vault, and the ledger only decides which of them are suppressed. The
    distinction matters to anyone reading this file to find out what the queue
    holds — a note deleted outside the workflow leaves the scan silently and no
    ledger row says so.

    The stamp is the other half. Nothing rewrites this file between runs, so a
    reader has no way to tell a queue refreshed a minute ago from one left by
    the nightly pass before a day of edits.
    """
    _workspace_dir(root)
    lines = [
        "# Vault Review",
        "",
        f"Pending note-review candidates, from a scan of the vault at {_now()}.",
        "Suppressed candidates are filtered out using `Vault-Review.jsonl`; this",
        "file is a projection and is rewritten whole on every run.",
        "",
    ]
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
    # What the vault actually holds right now, by path and by content. A note
    # renamed in an editor leaves its old path but never left the vault, and
    # `_record_vanished` must not say otherwise.
    present_paths: set[str] = set()
    present_digests: set[str] = set()
    for entry in entries:
        path = str(entry.path)
        if _is_workspace_path(path):
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
        try:
            mtime = disk_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        present_paths.add(path)
        digest = content_hash(raw)
        present_digests.add(digest)
        # A completed project is a closed record, not a live note: nothing
        # links to it by design, and `Still true` would stamp `updated: today`
        # onto a `Closed …` file. Same class of exemption as templates. Skipped
        # only after it is counted as present, so a project moved from
        # active/ to completed/ reads as a move, not a vanished note.
        if _is_completed_project(path):
            continue
        text = raw.decode("utf-8", errors="replace")
        note_type = entry.type or "note"
        # `entry.type` is the raw frontmatter string. The two type filters below
        # decide whether a note may be queued at all — in a queue whose terminal
        # action is deletion — so they must not be defeated by a spelling:
        # `type: Person` is not in `_LOOKUP_TYPES`, and `type: hackathon-log`
        # aliases to `journal`. Display keeps the raw value.
        canon_type = canonical_type(note_type) or note_type
        # Case-folded only, deliberately NOT alias-resolved: `analysis` aliases
        # to `reference`, and an orphaned analysis document is a real finding —
        # the exemption's rationale ("nothing links to a person or a bookmark
        # as a matter of course") does not hold for one. Aliases are still
        # resolved for `_RECORD_TYPES`, where every alias of `journal` really
        # is a dated record.
        lookup_type = note_type.strip().lower()
        signals: list[str] = []
        if path in orphans and not entry.related:
            signals.append("unlinked")
        group = duplicate_by_path.get(path)
        if group:
            signals.append("possible_duplicate")
        superseded = None if canon_type in _RECORD_TYPES else _superseded_match(text)
        if superseded is not None:
            signals.append("superseded_language")
        if not (entry.updated or entry.tags or entry.aliases):
            signals.append("weak_provenance")
        # Age is a signal too: the same predicate the Memory Map's "unchecked"
        # flag and `memory-audit`'s stale notes use, so a note the map tells
        # you to review is a note this queue can show. Logs, journals and
        # queue files are exempt inside the predicate.
        verification: NoteVerification | None = note_verification(
            entry.type or "", entry.updated or "", mtime, today=today
        )
        unverified = verification if verification is not None and verification.stale else None
        if unverified is not None:
            signals.append("unverified")
        if not signals:
            continue
        # `unlinked` on a lookup type describes the directory, not the note, so
        # it cannot queue one alone. Anything else — `unverified` included —
        # can: a linked person note unchecked for months is exactly the note
        # this queue exists to bring back. Hence the original, narrow rule.
        if lookup_type in _LOOKUP_TYPES and signals == ["unlinked"]:
            continue
        # `digest` was computed once, right after the read above, and reused
        # for `present_digests` — hashing every queued candidate twice showed
        # up on the queue-generation path.
        evidence = {
            "backlinks": sorted(incoming.get(path, [])),
            "outbound_links": sorted(outbound.get(path, [])),
            "bridge": len(incoming.get(path, [])) + len(outbound.get(path, [])) >= 4,
            "duplicate_group": group or [],
            "last_update": entry.updated or "",
            "type": note_type,
            "age_days": verification.age_days if verification is not None else None,
            # Why `unverified` fired, with the horizon beside the age so a
            # reader can disagree with the verdict without losing the evidence.
            "unverified": unverified.as_evidence() if unverified is not None else None,
            # Where the note says it was superseded, so the row can quote the
            # line instead of asking the user to go and find it.
            "superseded": superseded,
            # Carried in the queue payload rather than fetched per row: the
            # panel used to lazy-load the whole file through
            # `/api/workspace-file` behind a disclosure, which is why nothing
            # was visible until you opened fifty of them one at a time.
            "excerpt": _excerpt(text),
        }
        backlinks = cast(list[str], evidence["backlinks"])
        # Connectedness makes a note LESS disposable, so both connectedness
        # terms subtract. An earlier revision added +2 for `bridge` while
        # subtracting for backlinks: the two rules contradicted, and a hub with
        # four outbound links and no backlinks outranked genuine orphans for
        # the five candidate slots of a workflow whose terminal action is
        # deletion.
        #
        # `unverified` adds nothing: it says "check this", not "this may be
        # disposable", and priority orders a queue whose terminal action is
        # deletion. An orphan with a real disposability signal keeps ranking
        # above a well-linked note that merely needs re-reading; among equal
        # priorities, the note furthest past its horizon comes first.
        disposability = [signal for signal in signals if signal != "unverified"]
        priority = len(disposability) - min(len(backlinks), 2) - (1 if evidence["bridge"] else 0)
        item = ReviewCandidate(
            candidate_id=candidate_id(workspace, path, digest), workspace=workspace,
            path=path, content_hash=digest, signals=tuple(sorted(signals)),
            priority=priority, evidence=evidence, vault_root=root,
        )
        candidates.append(item)
    def overdue(item: ReviewCandidate) -> int:
        info = item.evidence.get("unverified")
        if not isinstance(info, dict):
            return 0
        return int(info["age_days"]) - int(info["threshold_days"])

    candidates.sort(key=lambda item: (-item.priority, -overdue(item), item.path))
    decisions = _latest_decisions(root)
    active = [item for item in candidates if not _suppressed(decisions.get(item.candidate_id, {})) or decisions.get(item.candidate_id, {}).get("content_hash") != item.content_hash]
    result = active[: max(1, min(int(max_candidates), MAX_CANDIDATES_CEILING))]
    if write_queue:
        _record_vanished(root, workspace, decisions, present_paths, present_digests)
        # The projection is the whole pending queue, not whatever slice this
        # caller asked for: the agent tools list the top 5, the PWA the lot,
        # and the file must not change shape depending on who wrote it last.
        _write_queue(root, active[:MAX_CANDIDATES_CEILING], decisions)
    return result


def _completed_counterpart(path: str) -> str:
    """Where ``projects/active/<x>/...`` lands once the project is completed."""
    parts = list(Path(path).parts)
    for index in range(len(parts) - 1):
        if parts[index].casefold() == "projects" and parts[index + 1].casefold() == "active":
            parts[index + 1] = "completed"
            return str(Path(*parts))
    return ""


def _completed_path_for(path: str) -> str:
    """Where *path* lands when its project is completed, or ``""`` if it cannot be.

    Two layouts are in real vaults and both are accepted here. The folder one,
    ``projects/active/<x>/...``, is what ``_completed_counterpart`` has always
    understood and what the PWA's own completion moves. The flat one,
    ``projects/<name>.md``, is the reported case: a single-file project with no
    folder of its own, whose counterpart function answers ``""`` for, so before
    this it could only be retired and never completed.

    Directory segments are compared case-folded, because ``Projects/`` and
    ``projects/`` are the same directory on a case-insensitive filesystem and
    the queue must not depend on which one a vault happens to use. Filenames
    keep their original spelling: that is the user's, and rewriting it would
    break every ref into the note for no reason.

    ``""`` means refuse, in both directions. A note already under
    ``projects/completed/`` is closed — completing it again would move it onto
    itself — and a note outside ``projects/`` is not a project at all.
    """
    if _is_completed_project(path):
        return ""
    counterpart = _completed_counterpart(path)
    if counterpart:
        return counterpart
    parts = list(Path(path).parts)
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "projects":
            # A flat project: the note sits directly in `projects/`, so the
            # destination is one segment deeper rather than a sibling rename.
            return str(Path(*parts[: index + 1], "completed", *parts[index + 1:]))
    return ""


def _project_folder(path: str) -> str:
    """The ``projects/active/<x>`` folder holding *path*, or ``""``.

    A folder project is completed as a folder. Its main markdown is only the
    entry point — the plan, the meeting notes and the attachments sit beside it
    — and moving that one file would leave the rest of the project under
    ``projects/active/`` describing a project the vault no longer has there.

    ``""`` means the note is the whole project: it sits directly in
    ``projects/active/`` (the single-file form) or in a flat ``projects/``. That
    is decided by how many segments follow ``active`` — a folder project has at
    least the folder and the note inside it, a single-file one has only the
    note.
    """
    parts = list(Path(path).parts)
    for index in range(len(parts) - 3):
        if parts[index].casefold() != "projects" or parts[index + 1].casefold() != "active":
            continue
        return str(Path(*parts[: index + 3]))
    return ""


def _inside_vault(root: Path, vault_relative: str) -> Path | None:
    """Resolve a ``memory-vault/...`` id to a real path, or ``None`` if it escapes.

    The inverse of `_vault_path`, and the one place the completion's path
    arithmetic happens, because two questions must share an answer: "is this
    note completable" and "may this note be completed". They are asked with the
    same ``relative_to`` / ``is_relative_to`` pair on purpose — the second one
    is what decides whether the vault gets written to, and a path that leaves
    the vault is refused by both rather than rounded to something
    harmless-looking.
    """
    root = Path(root).resolve()
    try:
        relative = Path(vault_relative).relative_to("memory-vault")
    except ValueError:
        return None
    resolved = (root / relative).resolve()
    return resolved if resolved.is_relative_to(root) else None


def _completion_move_to(root: Path, path: str, completed_path: str) -> Path | None:
    """Where completing *path* would put the thing it moves, or ``None`` if that escapes.

    Not the note's own ``completed_path``: a folder project moves as a FOLDER
    (``_project_folder``), so it is the folder's counterpart that has to be
    free. A candidate whose own destination is clear while its folder's is
    occupied is refused all the same, and a flag answering only the first
    question would offer a button that comes back 409.
    """
    folder = _project_folder(path)
    return _inside_vault(root, _completed_counterpart(folder) if folder else completed_path)


def _is_completable(candidate: ReviewCandidate) -> bool:
    """Whether the engine will accept ``complete`` for this candidate.

    Three questions, and they are the ones ``complete_project_note`` asks
    before it writes anything — the flag exists so the panel's button is a
    promise the engine keeps:

    * is it a project at all (``_is_project_candidate``);
    * is there a ``projects/`` layout to complete into (``_completed_path_for``,
      which answers ``""`` for a note already under ``projects/completed/``);
    * is the destination free (``_completion_move_to``), because two projects
      landing on one name is a content decision and the move refuses it.

    The third cannot be answered from the candidate alone, so a candidate
    carrying no vault is not completable: Retire always works, whereas a flag
    that answered yes without looking would be a lie the panel repeats.
    """
    if not _is_project_candidate(candidate):
        return False
    completed_path = _completed_path_for(candidate.path)
    if not completed_path or candidate.vault_root is None:
        return False
    move_to = _completion_move_to(candidate.vault_root, candidate.path, completed_path)
    return move_to is not None and not move_to.exists()


def _completion_moves_folder(candidate: ReviewCandidate) -> bool:
    """Whether completing this candidate moves a project FOLDER, not just the note.

    The button on a completable row says "Complete" either way, but the two
    moves are not the same act: a folder project's plan, meeting notes and
    attachments travel with it, while a flat ``projects/<name>.md`` is the
    whole project and moves alone. The panel has to be able to say which,
    because it is the difference between closing one file and closing a
    directory.

    Gated on ``_is_completable`` for the same reason the flag beside it is: a
    row whose Complete would be refused must not also describe what the
    refused move would have done.

    ``_project_folder`` answers the question on its own and is the helper
    ``complete_project_note`` moves on — it returns a directory holding the
    note, never the note itself, and ``""`` for the flat form, which is exactly
    "this candidate drags nothing but itself".
    """
    if not _is_completable(candidate):
        return False
    folder = _project_folder(candidate.path)
    return bool(folder) and folder != candidate.path


def _record_vanished(
    root: Path,
    workspace: str,
    decisions: dict[str, dict[str, Any]],
    present_paths: set[str],
    present_digests: set[str],
) -> None:
    """Close the ledger's account of notes that left outside this workflow.

    A note deleted with an ordinary file delete just stops being scanned: it
    drops out of the queue with no row explaining why, while the ledger goes on
    presenting itself as the durable record of what left the vault. One
    `vanished` row per candidate says what actually happened.

    Written only when the caller is already allowed to write — a listing that
    appends to the ledger is not the read-only listing it claims to be.

    Three guards, because ``candidate_id`` carries the content hash and a note
    therefore has a *different* id per revision. Guarding on the candidate id
    alone wrote rows that were simply false:

    * **Path, not candidate.** A note kept under one hash, edited, then retired
      has a `trash` row under the new id and a stale `keep` under the old one —
      which then produced a `vanished` row for a note sitting in the trash,
      offering Restore, while the audit trail said it had vanished.
    * **Still on disk.** The same stale row fires for any path that later
      disappears, so the check must be against what this scan actually found.
    * **Still in the vault under another name.** A rename leaves the old path
      empty; the note never left. Matching the recorded content hash against
      what the scan read tells a move from a deletion.

    Known boundary, by design: the content match cannot tell identical twins
    apart. If two notes share byte-identical content and one is deleted
    externally, the survivor's hash suppresses the `vanished` row and the
    deletion goes unrecorded. Disambiguating would need per-path revision
    history, which the ledger deliberately does not keep.
    """
    terminal_paths = {
        str(row.get("path") or "")
        for row in decisions.values()
        if row.get("disposition") in {"trash", "delete", "complete", "vanished"}
    }
    for candidate_id_value, decision in decisions.items():
        if str(decision.get("workspace") or "") != workspace:
            continue
        path = str(decision.get("path") or "")
        # `trash`, `delete` and `complete` already say where the note went;
        # `vanished` would be a second, vaguer answer to a question the ledger
        # has answered. For `complete` this is the only guard there is in the
        # flat layout: `_completed_counterpart` below knows `projects/active/<x>/`
        # but not `projects/<name>.md`, and without it a flat project would be
        # recorded as having vanished from the vault it is still sitting in.
        if path in terminal_paths:
            continue
        if path in present_paths:
            continue
        if str(decision.get("content_hash") or "") in present_digests:
            continue
        if _completed_counterpart(path) in present_paths:
            # Completing a project moves it *and* rewrites its status line,
            # so neither the path nor the hash matches any more; the note is
            # under projects/completed, not gone. This covers a project closed
            # from the PWA's Projects tab, which writes no ledger row of its
            # own; a `complete` row never reaches here, because `terminal_paths`
            # above already answered for its path.
            continue
        try:
            note = (root / Path(path).relative_to("memory-vault")).resolve()
        except ValueError:
            continue
        if not note.is_relative_to(root) or note.exists():
            continue
        _append(
            root,
            {
                "candidate_id": candidate_id_value,
                "workspace": workspace,
                "path": path,
                "content_hash": str(decision.get("content_hash") or ""),
                "disposition": "vanished",
                # Not a decision anybody made here: the note was removed by some
                # other means, and the row is a record, not an instruction.
                "actor": "system",
                "status": "deleted",
                "deferred_until": "",
            },
        )


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


# A UTF-8 BOM is not whitespace, so `"\ufeff---".strip()` is not `"---"` and a
# BOM-prefixed note read as having no frontmatter at all. `_FRONTMATTER_RE`
# already tolerates one; these two paths must agree with it.
def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


def _stamp_updated(text: str, today: str) -> tuple[str | None, str]:
    """Return *text* with frontmatter ``updated:`` set to *today*, and a status.

    Surgical: every other byte of the document survives, frontmatter comments
    and key order included. Round-tripping through a YAML dumper would reorder
    and reflow notes people write by hand.

    A ``None`` text means "nothing to write", and the status says why:
    ``already_current`` for a note that already carries today's date — a
    success — and ``no_frontmatter`` for one with no frontmatter to stamp or a
    block it never closes. A note with no frontmatter does not get one invented
    here: that is a lint finding of its own, and pressing *Still true* should
    not restructure a file.

    The status comes from this pass rather than a second one. `_reverify` used
    to ask a separate `_already_current` helper first, which re-implemented the
    same scan (opening and closing delimiter, `_UPDATED_KEY_RE`, quote
    stripping) and left the "already today" branch below unreachable from the
    only caller — so either copy could have drifted on quote handling or date
    comparison with no test to notice, and `_reverify` would report the wrong
    status.
    """
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    lines = _strip_bom(text).split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return None, "no_frontmatter"
    close = next(
        (i for i in range(1, len(lines)) if lines[i].strip() == _FRONTMATTER_DELIM),
        None,
    )
    if close is None:
        return None, "no_frontmatter"
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
            return None, "already_current"
        # Always a plain scalar date, so one line is the whole value.
        return bom + "\n".join(lines[:index] + [stamped] + lines[index + 1:]), "stamped"
    # Appended rather than prepended: `type:`/`title:` conventionally lead the
    # block, and a new key at the bottom reads as the addition it is.
    return bom + "\n".join(lines[:close] + [stamped] + lines[close:]), "stamped"


def _reverify(
    root: Path, candidate: ReviewCandidate, today: str
) -> tuple[str, str]:
    """Stamp the note as verified today; return ``(hash, status)``.

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
        return candidate.content_hash, "unreadable"
    if content_hash(raw) != candidate.content_hash:
        raise ValueError("candidate changed or no longer exists; regenerate the review")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Decoding with errors="replace" and writing the result back would
        # rewrite every undecodable byte as U+FFFD — a destructive edit to a
        # note imported from a latin-1 source. Reading signals may be lossy;
        # a rewrite may not.
        return candidate.content_hash, "not_utf8"
    stamped, status = _stamp_updated(text, today)
    if stamped is None:
        # Nothing to write, and which nothing it was matters. "Verified today
        # already" is the opposite outcome from "could not be stamped", and
        # collapsing the two would make the UI warn about a note that is
        # perfectly stamped. The unreadable and undecodable cases returned
        # above, so `status` is `already_current` or `no_frontmatter` here.
        return candidate.content_hash, status
    payload = stamped.encode("utf-8")
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=note.parent,
        # Truncated: the prefix exists for debuggability, but a full note name
        # near NAME_MAX plus the dot, 8 random chars and ".tmp" would raise
        # ENAMETOOLONG where the old fixed "tmp" prefix never could — and
        # `record_decision` does not catch OSError. `temp_prefix` is shared
        # with the other two note writers, which have the same bug.
        prefix=temp_prefix(note.name),
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
    return content_hash(payload), "stamped"


def record_decision(root: Path, candidate: ReviewCandidate, disposition: str, *, actor: str = "user", now: datetime | None = None) -> dict[str, Any]:
    if disposition not in DECISION_DISPOSITIONS:
        raise ValueError(f"unsupported vault review disposition: {disposition}")
    digest = candidate.content_hash
    stamp_status = "not_applicable"
    if disposition == "keep":
        digest, stamp_status = _reverify(
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
    # `stamped` is the plain yes/no the UI needs to stop promising a date it
    # did not write; `stamp_status` separates the two very different reasons
    # for a no — "already verified today" is a success, "nothing to stamp" is
    # the half-working case worth telling the user about.
    result["stamped"] = stamp_status == "stamped"
    result["stamp_status"] = stamp_status
    result["previous_candidate_id"] = candidate_id(
        candidate.workspace, candidate.path, candidate.content_hash
    )
    return result


def list_cleared(root: Path, *, workspace: str, limit: int = 20) -> list[dict[str, Any]]:
    """Notes cleared with `keep` that are still in the vault, newest first.

    The panel's way back in. A `keep` is suppressed by content hash, so short of
    editing the note there was no route from "I cleared that by mistake" to the
    row returning — and the note with no frontmatter, the one `keep` cannot
    stamp, is exactly the note a user is most likely to clear by mistake.

    Only `keep` rows appear. A trashed note has its own tab, and a note that has
    since been edited is already back in the queue on its own.
    """
    root = Path(root).resolve()
    wanted = max(1, min(int(limit), 100))
    # Ordered before anything is read, then read only far enough to fill the
    # page. Hashing every kept note first meant a whole-file read per cleared
    # note on every GET *and* inside every mutation's snapshot — hundreds of
    # reads to return twenty rows, in a module whose other passes go to some
    # length to avoid exactly that.
    rows = sorted(
        (
            row
            for row in _latest_decisions(root).values()
            if row.get("disposition") == "keep"
            and str(row.get("workspace") or "") == workspace
        ),
        key=lambda row: str(row.get("timestamp") or ""),
        reverse=True,
    )
    items: list[dict[str, Any]] = []
    for decision in rows:
        if len(items) >= wanted:
            break
        path = str(decision.get("path") or "")
        try:
            note = (root / Path(path).relative_to("memory-vault")).resolve()
        except ValueError:
            continue
        # Gone, or edited since: the first belongs to `vanished`, the second is
        # already un-suppressed and back in the queue under a new hash.
        if not note.is_file() or not note.is_relative_to(root):
            continue
        try:
            digest = content_hash(note.read_bytes())
        except OSError:
            # One unreadable note (permissions, a broken symlink, a delete
            # racing the is_file check) must drop its own row, not 500 the
            # whole review endpoint — this runs unprotected from the GET
            # handler and from every mutation's snapshot, so the panel would
            # go blank rather than lose a row.
            continue
        if digest != decision.get("content_hash"):
            continue
        items.append(
            {
                "candidate_id": str(decision.get("candidate_id") or ""),
                "workspace": workspace,
                "path": path,
                "content_hash": str(decision.get("content_hash") or ""),
                "decided_at": str(decision.get("timestamp") or ""),
            }
        )
    return items


def reopen_note(root: Path, candidate_id_value: str, *, workspace: str, actor: str = "user") -> dict[str, Any]:
    """Undo a `keep`, putting the note back in front of whoever cleared it.

    Appends rather than rewrites: the ledger is the audit trail, so the record
    is "kept, then reopened", not a `keep` that never happened.
    """
    _validate_candidate_id(candidate_id_value)
    root = Path(root).resolve()
    decision = _latest_decisions(root).get(candidate_id_value)
    if not decision or str(decision.get("workspace") or "") != workspace:
        raise ValueError("cleared candidate not found")
    if decision.get("disposition") != "keep":
        raise ValueError("only a kept note can be reopened")
    row = {
        "candidate_id": candidate_id_value,
        "workspace": workspace,
        "path": str(decision.get("path") or ""),
        "content_hash": str(decision.get("content_hash") or ""),
        "disposition": "reopen",
        "actor": actor,
        "status": "candidate",
        "deferred_until": "",
    }
    _append(root, row)
    return row


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

    The pattern ``_commit_staged_edits`` in ``vault_index`` and the per-root move
    in ``vault_rehome`` already use, for the same reason. Writing each note the
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
    return str(Path(path).relative_to("memory-vault").with_suffix(""))


def _vault_path(root: Path, path: Path) -> str:
    """An absolute vault path as the ``memory-vault/…`` id every candidate uses.

    The ledger, the candidate evidence and the sweep all name notes in this
    namespace, so anything read off disk has to be rendered into it before it can
    be compared with a recorded key.
    """
    return str(Path("memory-vault") / path.relative_to(root))


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


def _set_status(text: str, pattern: re.Pattern[str], value: str) -> str:
    """Set the note's own ``status:`` line, inside the frontmatter and nowhere else.

    A note whose frontmatter never closed is a normal shape and is left exactly
    as it is: the substitution reports nothing by returning *text* unchanged, and
    the caller says so in the ledger row rather than claiming a line it did not
    find.
    """
    frontmatter = _FRONTMATTER_RE.match(text)
    if frontmatter is None:
        return text
    block = frontmatter.group(0)
    rewritten = pattern.sub(rf"\g<1>{value}", block)
    if rewritten == block:
        return text
    return rewritten + text[len(block):]


def _rewrite_status(text: str) -> tuple[str, bool]:
    """Close a project out in its own frontmatter; return the text and whether it changed.

    The same substitution ``ProjectChatManager.complete_project`` applies, scoped
    to the frontmatter block rather than the whole document — see
    ``_STATUS_ACTIVE_RE``.

    A note with no ``status: active`` line is a normal shape — the key is
    optional frontmatter — so it still completes, and the row records
    ``status_rewritten: False`` rather than claiming a line was found. Saying so
    honestly is the difference between a closed project that says it is closed
    and a ledger row asserting a rewrite that never happened.
    """
    closed = _set_status(text, _STATUS_ACTIVE_RE, "completed")
    return closed, closed != text


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


def _moved_back_map(
    root: Path, folder: str, previous_path: str, completed_path: str, recorded: list[Path]
) -> dict[str, str]:
    """``{completed path: original path}`` for the undo images a restore can read.

    A completion records each image under the path its note has while the project
    is COMPLETED, and the move that precedes a restore takes those paths away
    again: every note inside the project folder, and — in the flat layout, where
    there is no folder at all — the project's own note, whose recorded path *is*
    ``completed_path``. So the keys have to be translated before they can be
    looked up.

    Only the recorded keys are translated, and only when the translation is the
    move's own mirror: a note that merely referred to the project never moved, and
    its image is already at the path it will be read from.
    """
    moved_back: dict[str, str] = {}
    if folder:
        completed_folder = f"{_completed_counterpart(folder)}/"
        for path in recorded:
            key = _vault_path(root, path)
            if key.startswith(completed_folder):
                moved_back[key] = f"{folder}/{key[len(completed_folder):]}"
    if previous_path != completed_path:
        moved_back[completed_path] = previous_path
    return moved_back


def _repoint_project_references(
    root: Path, previous_path: str, completed_path: str, *, workspace: str, folder: str = ""
) -> tuple[list[str], list[_Rewrite]]:
    """Every reference to a note that is moving, repointed at where it is going.

    Pure: returns the vault-relative paths that would change and one
    :class:`_Rewrite` per note, carrying the path it has now and the path it will
    have after the move. Nothing is written, so a vault that cannot be rewritten
    costs the caller nothing but the refusal.

    *folder* is the ``projects/active/<x>`` the whole move is for, if any. A
    folder project moves as a folder, so EVERY note under it is moving — and a
    map holding only the entry note left every sibling reference dangling:
    ``[[projects/active/demo/plan]]`` in another note resolved to a file that was
    no longer there after the move, which is the same broken link as a reference
    to the entry note and needs the same rewrite. The map is built per moved
    note, in both value spaces, for that reason.

    For a note that is itself moving, ``source_after`` is its new path: its own
    relative links are measured from a different directory afterwards and have
    to be re-spelled even when nothing in them names the project. That is also
    why each :class:`_Rewrite` carries two paths — the caller writes through
    ``path_before`` (the file is still there) and records the undo image under
    ``path_after`` (where the file will be, and where a restore will look).

    Both dialects go through one primitive, ``rewrite_references``, because they
    are the same edge written twice. Completion is the case it was extracted
    for: the note STAYS in the vault, so unlike a deletion the links must be
    repointed rather than stripped. The ref dialect does not change the way it
    does for a move between workspaces — a note that said
    ``[[projects/active/x]]`` reads the same way from ``projects/completed/`` —
    so each reference is re-spelled relative to where the project now is, which
    is what the helper does for a relative markdown destination.

    The project's own entry note is swept like every other note, and it used not
    to be. It sits inside the moving folder and may link its siblings, so
    ``[[projects/active/demo/plan]]`` in it became a dangling reference in
    ``completed/demo/demo.md`` — the same broken link as one written anywhere
    else, on the one note the sweep excluded. Excluding it bought nothing: the
    caller folds its ``status:`` line into whatever comes back, so one write and
    one undo image still cover both edits.

    Only notes that can name a moved note are read. A reference in any dialect is
    a path without its extension, so it always contains that note's stem, and a
    file without any of them cannot mention one: parsing every note in the vault
    to discover that is the cost this skips.
    """
    # Imported here, not at module scope: `vault_rehome` pulls in the migration
    # receipt and the git rail, and a read-only listing of this module's queue
    # must not pay for them. The same reason `delete_permanently` imports
    # `strip_references` at the point of use.
    from ciao.vault_rehome import rewrite_references

    root = Path(root).resolve()
    entries = scan_vault(root, workspace=workspace)
    filename_index = build_filename_index(entries)
    moves: list[tuple[str, str]] = []
    if folder:
        completed_folder = _completed_counterpart(folder)
        for entry in entries:
            rel = str(entry.path)
            if rel == previous_path or not rel.startswith(f"{folder}/"):
                continue
            # The suffix carries the note's place inside the folder, which is
            # unchanged by the move: only the `active` segment above it is.
            moves.append((rel, completed_folder + rel[len(folder):]))
    moves.append((previous_path, completed_path))
    moved_to = dict(moves)
    # `rewrite_references` works in two value spaces, so the one move is spelled
    # twice: a markdown destination is a path resolved against the note holding
    # it, so both sides keep the `memory-vault/` prefix and drop the extension,
    # while a frontmatter ref and a wikilink are vault-relative and drop both.
    moved_by_ref = {
        str(Path(old).with_suffix("")): str(Path(new).with_suffix("")) for old, new in moves
    }
    moved_by_resolved = {old: _vault_ref(new) for old, new in moves}
    known = {str(Path(str(entry.path)).with_suffix("")) for entry in entries}
    needles = _ref_needles(moves)
    edited: list[str] = []
    rewrites: list[_Rewrite] = []
    for entry in entries:
        rel = str(entry.path)
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
        if not any(needle in folded for needle in needles):
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


def complete_project_note(root: Path, candidate: ReviewCandidate, *, actor: str = "user") -> dict[str, Any]:
    """Close a project out: move it to ``projects/completed/`` and repoint what linked to it.

    ``trash_note`` was the only way to retire a project candidate, and it is the
    wrong instrument for one that simply finished. The project left the active
    tree with ``status: active`` still in it, nothing in the vault recorded an
    end, and every note that linked to it kept a reference to a file the queue
    had stopped listing — so it left the index and the graph silently, which is
    the complaint `trash_note` itself makes about search.

    Completion moves the note where the vault already keeps closed projects,
    rewrites the one frontmatter line that says otherwise, and repoints every
    inbound reference in both dialects at the new path. This module already knew
    that layout — ``_is_completed_project`` exempts it from candidates,
    ``_completed_counterpart`` names it — and could not reach the one code path
    that acted on it, which lived on the PWA's project chat manager and could
    only close a project it already knew about.

    Both layouts are real and both are handled: the folder project
    ``projects/active/<x>/...``, which moves as a whole folder, and the flat
    ``projects/<name>.md`` a hand-written project note usually is. A folder
    project moves as a folder because its main markdown is only the entry point
    — the plan, the notes and the attachments sit beside it, and moving the one
    file would strand them under ``active/`` describing a project that is no
    longer there. Any note under that folder is a candidate in its own right, so
    the candidate need not be the entry markdown: what moves is always the
    folder, and ``new_path`` names where the candidate itself ends up inside it.

    The whole action is one transaction, in the ordering ``delete_permanently``
    established and for the same reason: the links must be repointed BEFORE the
    note moves, because resolving them needs the note to still be on disk, and
    nothing may be left half-done. A failure at any point — a link rewrite, the
    move itself, the ledger append — puts every file already written back and
    leaves the project exactly where it was. A completion that landed for the
    note and failed for its backlinks is the one outcome this workflow must not
    produce: the ledger row would say the project was closed while notes across
    the vault still point at the old path.

    Refuses rather than repairs, in the three cases where the right answer is not
    knowable from here: a candidate that is not a project (retiring one is what
    the trash is for), a path with no ``projects/`` layout to complete into, and a
    destination that is already occupied — two projects landing on one name is a
    content decision, never a move. The last two are the ones `_is_completable`
    reads, so the flag the panel draws from cannot promise what this refuses.
    """
    root = Path(root).resolve()
    source = (root / Path(candidate.path).relative_to("memory-vault")).resolve()
    if not source.is_relative_to(root):
        raise ValueError("note is outside the vault")
    if not source.is_file() or content_hash(source.read_bytes()) != candidate.content_hash:
        raise ValueError("candidate changed or no longer exists; regenerate the review")
    if not _is_project_candidate(candidate):
        raise ValueError("only a project can be completed; retire this note instead")
    completed_path = _completed_path_for(candidate.path)
    if not completed_path:
        raise ValueError("this note has no projects/ layout to complete into")
    destination = _inside_vault(root, completed_path)
    if destination is None:
        raise ValueError("completion destination is outside the vault")
    folder = _project_folder(candidate.path)
    if folder:
        # The destination of the MOVE, which is the counterpart of the folder and
        # not the parent of the note. Those agree only while the candidate is the
        # project's entry markdown: a candidate nested below it
        # (`projects/active/x/meetings/2026-01.md`) has a parent of
        # `completed/x/meetings`, and moving the whole `x` folder there would
        # leave `new_path` naming a file that does not exist and the project
        # split across two trees.
        move_from = _inside_vault(root, folder)
        move_to = _completion_move_to(root, candidate.path, completed_path)
    else:
        move_from, move_to = source, destination
    if move_from is None or move_to is None:
        raise ValueError("completion path is outside the vault")
    if not move_from.is_relative_to(root) or not move_to.is_relative_to(root):
        raise ValueError("completion path is outside the vault")
    # Checked before anything is written, so a refusal costs the vault nothing.
    if move_to.exists():
        raise ValueError(f"a note already exists at {completed_path}; refusing to complete into it")
    try:
        original_text = _read_exact(source)
    except UnicodeDecodeError as exc:
        # Decoding with errors="replace" and writing the result back would turn
        # every undecodable byte into U+FFFD — a destructive edit to a note
        # imported from a latin-1 source. Reading signals may be lossy; a
        # rewrite may not. `_reverify` refuses the same note rather than
        # stamping it, and completion rewrites more of it.
        raise ValueError(f"project note is not readable as utf-8: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"project note could not be read: {exc}") from exc
    # References resolve against the vault's real files, so the sweep runs while
    # the note is still at its old path. That ordering constraint is why this is
    # before the move and not after it, and it is the same reason
    # `delete_permanently` refuses before it strips anything. It sweeps the
    # project's own note too, so the note's references follow it out of `active/`.
    edited, rewrites = _repoint_project_references(
        root, candidate.path, completed_path, workspace=candidate.workspace, folder=folder
    )
    # The `status:` line is folded into whatever the sweep made of that note, so
    # the note leaves `active/` saying it is closed AND with its references
    # following it, in one write and one undo image. A note the sweep found
    # nothing to change in still gets the status line, as its own rewrite.
    closed_text, status_rewritten = _rewrite_status(
        next((r.after for r in rewrites if r.path_before == source), original_text)
    )
    if closed_text != original_text:
        entry = next((i for i, r in enumerate(rewrites) if r.path_before == source), None)
        if entry is None:
            rewrites.insert(0, _Rewrite(source, destination, original_text, closed_text))
        elif closed_text != rewrites[entry].after:
            rewrites[entry] = rewrites[entry]._replace(after=closed_text)
    # Every rewritten note is written through the path it has NOW and recorded
    # under the path it will have AFTER the move, which is the path
    # `restore_completed` looks its image up by and the only one at which it can
    # be written. The project's own note is in here like any other: keeping it out
    # would have meant reversing its reference rewrite by hand, and a restore that
    # undoes the status line but not the links leaves a project pointing at a tree
    # that no longer has the notes it named.
    pending = [(r.path_before, r.before, r.after) for r in rewrites]
    rollback = [(r.path_before, r.before) for r in rewrites]
    undo = {str(r.path_after): {"before": r.before, "after": r.after} for r in rewrites}
    try:
        _write_texts(pending)
    except OSError as exc:
        raise ValueError(f"could not rewrite the references to the project: {exc}") from exc
    metadata = {
        "candidate_id": candidate.candidate_id,
        "workspace": candidate.workspace,
        # `path` is what every other row in this ledger names the note by, and
        # what `_suppressed` and `_record_vanished` read. `previous_path` and
        # `new_path` say where the completion actually put it.
        "path": candidate.path,
        "previous_path": candidate.path,
        "new_path": completed_path,
        "content_hash": candidate.content_hash,
        "status_rewritten": status_rewritten,
        "edited_backlinks": edited,
        "undo": undo,
        "completed_at": _now(),
    }
    # Durable recovery evidence BEFORE the terminal row, the way `trash_note`
    # writes its sidecar before appending: if this process dies between the two,
    # the sidecar is the only thing on disk that says where the note went and
    # which files were rewritten, and with what to put back.
    #
    # Unlike `delete_permanently`, which KEEPS its metadata when the append
    # fails, this one is discarded: there the note is still sitting in the trash
    # and the metadata is the only way back to it, while here the whole
    # transaction has just been undone. A file claiming a completed move for a
    # project that is still active would be worse than no file at all.
    recovery = destination.with_name(f"{destination.stem}.completion.json")
    created = _missing_ancestors(move_to.parent, root)
    try:
        move_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move_from), str(move_to))
    except OSError as exc:
        _discard(recovery)
        _unwrite_texts(rollback)
        _prune_created(created)
        raise ValueError(f"could not complete the project; it was put back: {exc}") from exc
    try:
        recovery.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _append(root, {**metadata, "disposition": "complete", "status": "reviewed", "actor": actor})
    except OSError as exc:
        if move_to.exists() and not move_from.exists():
            shutil.move(str(move_to), str(move_from))
        _unwrite_texts(rollback)
        # The sidecar first: it sits inside the tree `_prune_created` is about to
        # remove, and an empty directory is the only thing `rmdir` will take.
        _discard(recovery)
        _prune_created(created)
        raise ValueError(f"complete audit failed; the project was put back: {exc}") from exc
    _discard(recovery)
    return metadata


def restore_completed(
    root: Path, candidate_id_value: str, *, workspace: str, actor: str = "user"
) -> dict[str, Any]:
    """Undo one ``complete``: put the project back where it was.

    The counterpart to :func:`complete_project_note`, and what keeps the
    workflow's claim to be reversible true for completion the way `restore_note`
    keeps it true for the trash. Appends rather than rewrites: the trail reads
    "completed, then restored", which is the only order in which the
    intermediate state stays visible to anyone reading it afterwards.

    Scans the ledger for the last row that says where this project is, and
    restores it only if that row is a ``complete``. Looking the id up in the
    decision index would not do: the note being restored is by definition NOT a
    candidate any more — a completed project sits under ``projects/completed/``
    and never reaches the queue — and the answer has to be the LATEST row, so
    that a project already restored is not restored a second time.

    Four refusals, one judgement — a restore must never destroy work that
    happened after the completion:

    * there is no ``complete`` row to undo, or the last one has already been
      undone;
    * the completed note is gone, so there is nothing to move back;
    * the original path is occupied, so restoring would overwrite it — and for a
      folder project, so would a `projects/active/<x>/` that holds anything at
      all, because moving a directory onto an existing one nests it;
    * a note that was repointed at the completed path has been edited since, so
      writing the recorded image back would throw those edits away. The
      completion recorded the text each file was left in as well as the text it
      had, and that is the only thing this can compare against.

    Every note the completion rewrote is restored from its recorded image, the
    project's own entry note among them — its ``status:`` line and its
    references in one image, because a restore that undid the line but not the
    links would hand back a project still pointing at a tree that no longer holds
    the notes it named. The price of that consistency is that an edit made to any
    of those notes while the project sat in ``completed/`` refuses the restore
    rather than surviving it, which is the same answer the referring notes have
    always given.

    Each image is looked up where the completion left its note and written back
    through where the project has just landed: for a note that moved with the
    project those are two different paths, and ``_moved_back_map`` is the move
    mirrored.
    """
    _validate_candidate_id(candidate_id_value)
    root = Path(root).resolve()
    # The LAST row for this id that says where the note is, whether it moved
    # there or came back. Matching only on `complete` answered with the original
    # completion no matter how many restores had happened since, so a second
    # restore ran again against a project that was already back under
    # `active/` — the docstring's "only a completed project can be restored",
    # asserted rather than enforced. The trailing `restore` is the answer in
    # that case, and both refusals are the same one.
    decision: dict[str, Any] | None = None
    for row in read_ledger(root):
        if (
            str(row.get("candidate_id") or "") == candidate_id_value
            and str(row.get("workspace") or "") == workspace
            and row.get("disposition") in {"complete", "restore"}
        ):
            decision = row
    if not decision or decision.get("disposition") != "complete":
        raise ValueError("completed candidate not found")
    completed_path = str(decision.get("new_path") or "")
    previous_path = str(decision.get("previous_path") or "")
    if not completed_path or not previous_path:
        raise ValueError("ledger row does not record where the project went")
    source = (root / Path(completed_path).relative_to("memory-vault")).resolve()
    destination = (root / Path(previous_path).relative_to("memory-vault")).resolve()
    if not source.is_relative_to(root) or not destination.is_relative_to(root):
        raise ValueError("restore path is outside the vault")
    if not source.is_file():
        raise ValueError("the completed project is no longer in the vault")
    if destination.exists():
        raise ValueError("refusing to restore: the original path is occupied")
    folder = _project_folder(previous_path)
    if folder:
        # Mirror of the completion's move, and the same two corrections: a folder
        # project comes back as a folder, so the plan and the attachments beside
        # its main markdown travel with it instead of being left in `completed/`,
        # and the destination is the counterpart's counterpart rather than the
        # parent of the note — which for a candidate nested below the project's
        # entry markdown is a directory inside it, not the project.
        move_from = (root / Path(_completed_counterpart(folder)).relative_to("memory-vault")).resolve()
        move_to = (root / Path(folder).relative_to("memory-vault")).resolve()
        if not move_from.is_relative_to(root) or not move_from.is_dir():
            raise ValueError("the completed project folder is no longer in the vault")
        # `shutil.move(dir, existing_dir)` does not refuse: it nests the project
        # as `active/<x>/<x>/…`, and the vault then holds the project's notes
        # twice with only one of them reachable by any ref. The note-level check
        # above cannot see this — an `active/<x>/` holding something else entirely
        # leaves the note's own path free.
        if move_to.exists():
            raise ValueError("refusing to restore: the original folder is occupied")
    else:
        move_from, move_to = source, destination
    if not move_from.is_relative_to(root) or not move_to.is_relative_to(root):
        raise ValueError("restore path is outside the vault")
    undo = cast(dict[str, Any], decision.get("undo") or {})
    images: list[tuple[Path, str]] = []
    for path, image in undo.items():
        target = Path(str(path))
        # The recorded paths are the completion's own, but the ledger is a file
        # on disk that a person can edit, and a restore is the one place this
        # module writes to paths it did not choose.
        if not target.is_file() or not target.resolve().is_relative_to(root):
            raise ValueError(f"a note rewritten by the completion is gone: {path}")
        if _read_exact(target) != str(image.get("after") or ""):
            raise ValueError(f"a note rewritten by the completion has changed since: {path}")
        images.append((target, str(image.get("before") or "")))
    metadata = {
        "candidate_id": candidate_id_value,
        "workspace": workspace,
        "path": previous_path,
        "previous_path": previous_path,
        "new_path": completed_path,
        "content_hash": str(decision.get("content_hash") or ""),
        "edited_backlinks": list(decision.get("edited_backlinks") or []),
        "restored_at": _now(),
    }
    created = _missing_ancestors(move_to.parent, root)
    try:
        move_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(move_from), str(move_to))
    except OSError as exc:
        _prune_created(created)
        raise ValueError(f"could not restore the project: {exc}") from exc
    # `writes` is what this restore puts on disk, `applied` is what it found
    # there. Both are read after the move, because until the project lands at
    # its old path the note is not there to read; `applied` is what puts the
    # whole restore back if the ledger append below fails.
    #
    # The images are keyed where the COMPLETION left each note, which is a path
    # the move above has just taken away again for every note that moved with the
    # project — the project's own note included. `_moved_back_map` is that move
    # mirrored, so the keys are translated rather than looked up; an image for a
    # note that merely referred to the project is not in it, because that note
    # never moved.
    moved_back = _moved_back_map(
        root, folder, previous_path, completed_path, [Path(key) for key in undo]
    )
    writes: list[tuple[Path, str, str]] = []
    applied: list[tuple[Path, str]] = []
    try:
        for recorded, before in images:
            old_path = moved_back.get(_vault_path(root, recorded))
            target = (
                (root / Path(old_path).relative_to("memory-vault")).resolve()
                if old_path is not None
                else recorded
            )
            current = _read_exact(target)
            writes.append((target, current, before))
            applied.append((target, current))
        _write_texts(writes)
    except (OSError, UnicodeDecodeError) as exc:
        # `_write_texts` restores every file it swapped in, and a read that
        # failed wrote nothing at all, so the only thing left to undo here is
        # the move: the project goes back to where it came from, linked the way
        # it was linked. A decode failure is caught with the rest, because the
        # project has already been moved by this point and leaving it there
        # would strand it with no ledger row saying where it went.
        if move_to.exists() and not move_from.exists():
            shutil.move(str(move_to), str(move_from))
        _prune_created(created)
        raise ValueError(f"could not restore the project references: {exc}") from exc
    try:
        _append(root, {**metadata, "disposition": "restore", "status": "reviewed", "actor": actor})
    except OSError as exc:
        _unwrite_texts(applied)
        if move_to.exists() and not move_from.exists():
            shutil.move(str(move_to), str(move_from))
        _prune_created(created)
        raise ValueError(f"restore audit failed; the project stays completed: {exc}") from exc
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
