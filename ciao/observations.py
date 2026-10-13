"""Counted observations: weak patterns the memory pass sees across chats.

``Workspace/Observations.md`` is a second learnings-shaped file. Each chat adds
at most one sighting of an observation, the engine counts sightings from
distinct chats, and at :data:`OBSERVATION_PROPOSE_CHATS` the nightly Workspace
care run files an ordinary ``[profile]`` or ``[people <Name>]`` proposal for the
user to accept or dismiss. Nothing here writes a bounded region or a person
note: the only output is a queued proposal.

The record machinery is :mod:`ciao.learning_records` unchanged. The subject of
an observation is a fixed prefix of its statement (``About the user: …`` or
``About <Person Name>: …``), so :func:`ciao.learning_records.normalized_statement`
keeps the same wording about two subjects as two observations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from ciao import memory_proposals
from ciao.learning_records import (
    SECTION_ACTIVE,
    SECTION_HEADINGS,
    SECTION_PROMOTED,
    LearningEntry,
    LearningRecord,
    parse_learnings,
    render_learning,
)
from ciao.memory_proposals import MemoryProposal
from ciao.memory_receipts import queue_lock, write_queue_atomically

OBSERVATIONS_RELATIVE = "Workspace/Observations.md"
OBSERVATIONS_ARCHIVE_RELATIVE = "Workspace/Observations-Archive.md"

OBSERVATIONS_STUB = (
    "---\n"
    "search: false\n"
    "tags: [ciao, observations]\n"
    "---\n"
    "# Observations\n\n"
    "Low-confidence patterns the memory pass noticed, counted per distinct "
    "chat. At x3 the nightly Workspace care run proposes one for review. "
    "Nothing here is loaded into prompts or recall.\n\n"
    "## Active\n"
)
"""The body a first write starts from.

``search: false`` keeps the file out of recall: an observation about a third
party is an unconfirmed inference and must never surface in a chat as a fact.
"""

OBSERVATIONS_ARCHIVE_STUB = (
    "---\n"
    "search: false\n"
    "tags: [ciao, observations]\n"
    "---\n"
    "# Observations archive\n\n"
    "Observations seen in one chat that never recurred.\n\n"
    "## Archived\n"
)

OBSERVATION_PROPOSE_CHATS = 3
"""Distinct chats an observation needs before it becomes a proposal."""

OBSERVATION_PRUNE_DAYS = 60
"""An x1 observation not seen again for longer than this is archived."""

USER_SUBJECT = "the user"

_STATEMENT_RE = re.compile(r"^About (?P<about>.+?): (?P<text>.+)$")

_PROMOTED_HEADING_RE = re.compile(
    r"(?mi)^#{1,3}[ \t]+(?:promoted|resolved)[^\r\n]*(?P<eol>\r\n|\n|\Z)"
)


@dataclass(frozen=True, slots=True)
class Observation:
    """One entry of ``Observations.md`` and the subject it is about."""

    record: LearningRecord
    section: str
    about: str
    statement: str
    chats: tuple[str, ...]

    @property
    def destination(self) -> tuple[str, str]:
        """The proposal destination as ``(kind, payload)``."""
        if self.about == USER_SUBJECT:
            return ("profile", "")
        return ("people", self.about)


def subject_statement(about: str, text: str) -> str:
    """The stored statement: ``About <about>: <text>`` on one line."""
    about = about.strip()
    flat = " ".join(text.split())
    if not about or not flat:
        raise ValueError("an observation needs both a subject and a statement")
    if ":" in about or "\n" in about or "\r" in about:
        raise ValueError("the subject of an observation cannot contain ':' or a newline")
    return f"About {about}: {flat}"


def parse_observations(text: str, *, workspace: str) -> list[Observation]:
    """Every readable entry whose statement carries the ``About X:`` prefix."""
    found: list[Observation] = []
    for entry in parse_learnings(text, workspace=workspace).entries:
        observation = _observation_of(entry)
        if observation is not None:
            found.append(observation)
    return found


def _observation_of(entry: LearningEntry) -> Observation | None:
    record = entry.record
    if record is None:
        return None
    match = _STATEMENT_RE.match(record.text)
    if match is None:
        return None
    chats = tuple(
        dict.fromkeys(seen.source for seen in record.observations if seen.source)
    )
    return Observation(
        record=record,
        section=entry.section,
        about=match.group("about"),
        statement=match.group("text"),
        chats=chats,
    )


def observations_path(vault_root: Path) -> Path:
    return vault_root / OBSERVATIONS_RELATIVE


def _archive_path(vault_root: Path) -> Path:
    return vault_root / OBSERVATIONS_ARCHIVE_RELATIVE


def _read(path: Path, default: str) -> str:
    # Bytes, not read_text: read_text translates newlines and would rewrite a
    # CRLF file in every line the writer did not mean to touch.
    return path.read_bytes().decode("utf-8") if path.exists() else default


def add_observation(
    vault_root: Path,
    *,
    workspace: str,
    about: str,
    text: str,
    source: str,
    today: date | None = None,
) -> str:
    """Record one sighting from chat *source*: ``"add"``, ``"update"`` or ``"none"``."""
    source = source.strip()
    if not source:
        raise ValueError("an observation needs the chat it was seen in (source)")
    statement = subject_statement(about, text)
    path = observations_path(vault_root)
    with queue_lock(path):
        existing = _read(path, OBSERVATIONS_STUB)
        updated, operation = memory_proposals.render_learning_append(
            existing,
            statement,
            workspace=workspace,
            source=source,
            today=today.isoformat() if today else "",
        )
        if operation != "none":
            write_queue_atomically(path, updated)
    return operation


def due_for_proposal(obs: Observation) -> bool:
    return (
        obs.section == SECTION_ACTIVE
        and len(obs.chats) >= OBSERVATION_PROPOSE_CHATS
    )


def due_for_prune(obs: Observation, *, today: date) -> bool:
    last_seen = obs.record.last_seen
    return (
        obs.section == SECTION_ACTIVE
        and obs.record.count == 1
        and last_seen is not None
        and (today - last_seen).days > OBSERVATION_PRUNE_DAYS
    )


def _newline(text: str) -> str:
    return memory_proposals._newline(text)


def _eol_after(text: str, end: int) -> int:
    """Length of the line terminator at *end*, which is 0 on a final line."""
    if text.startswith("\r\n", end):
        return 2
    return 1 if text.startswith("\n", end) else 0


def _splice(
    text: str, edits: list[tuple[LearningEntry, str | None]]
) -> str:
    """Replace (or, given ``None``, delete with its terminator) entry lines.

    Applied from the last line to the first so earlier offsets stay valid.
    """
    for entry, rendered in sorted(edits, key=lambda item: item[0].start, reverse=True):
        end = entry.end
        if rendered is None:
            end += _eol_after(text, end)
            rendered = ""
        text = text[: entry.start] + rendered + text[end:]
    return text


def _file_under_promoted(text: str, line: str) -> str:
    """*text* with *line* directly under ``## Promoted / Resolved``.

    The heading is created at the end of the file when it is absent. Both the
    heading and the entry use the file's own line ending.
    """
    newline = _newline(text)
    heading = _PROMOTED_HEADING_RE.search(text)
    if heading is None:
        if text and not text.endswith("\n"):
            text += newline
        title = SECTION_HEADINGS[SECTION_PROMOTED]
        return f"{text}{newline}## {title}{newline}{line}{newline}"
    at = heading.end()
    lead = "" if heading.group("eol") else newline
    return f"{text[:at]}{lead}{line}{newline}{text[at:]}"


def _active(
    text: str, *, workspace: str, learning_id: str
) -> tuple[Observation, LearningEntry]:
    for entry in parse_learnings(text, workspace=workspace).entries:
        observation = _observation_of(entry)
        if (
            observation is not None
            and observation.section == SECTION_ACTIVE
            and observation.record.learning_id == learning_id
        ):
            return observation, entry
    raise ValueError(f"no active observation with id {learning_id}")


def propose_observation(
    vault_root: Path,
    *,
    workspace: str,
    observation_id: str,
    today: date | None = None,
) -> dict[str, Any]:
    """Queue the proposal for one due observation and move it to Resolved."""
    path = observations_path(vault_root)
    with queue_lock(path):
        text = _read(path, "")
        obs, entry = _active(text, workspace=workspace, learning_id=observation_id)
        if not due_for_proposal(obs):
            raise ValueError(
                f"observation {observation_id} has {len(obs.chats)} distinct "
                f"chat(s); {OBSERVATION_PROPOSE_CHATS} are needed"
            )
        kind, payload = obs.destination
        proposal = MemoryProposal(
            target=kind,
            payload=payload,
            text=obs.statement,
            source_section="observations " + ", ".join(obs.chats),
        )
        queued_path = memory_proposals.append_proposals([proposal], vault_root)
        dismissed = queued_path is None and memory_proposals.was_dismissed(
            vault_root, obs.statement
        )
        promoted = (
            queued_path is None
            and not dismissed
            and memory_proposals.was_promoted(vault_root, obs.statement)
        )
        without = _splice(text, [(entry, None)])
        write_queue_atomically(
            path, _file_under_promoted(without, entry.source_text)
        )
    return {
        "id": observation_id,
        "about": obs.about,
        "destination": kind if not payload else f"{kind} {payload}",
        "queued": queued_path is not None,
        "dismissed_before": dismissed,
        "promoted_before": promoted,
    }


def merge_observations(
    vault_root: Path, *, workspace: str, keep_id: str, drop_id: str
) -> dict[str, Any]:
    """Fold the *drop_id* observation into *keep_id* and delete its line."""
    if keep_id == drop_id:
        raise ValueError("cannot merge an observation into itself")
    path = observations_path(vault_root)
    with queue_lock(path):
        text = _read(path, "")
        keep, keep_entry = _active(text, workspace=workspace, learning_id=keep_id)
        drop, drop_entry = _active(text, workspace=workspace, learning_id=drop_id)
        if keep.about != drop.about:
            raise ValueError(
                f"observations are about different subjects: {keep.about!r} "
                f"and {drop.about!r}"
            )
        seen_identities: set[tuple[str, ...] | None] = set()
        ordered = []
        for seen in keep.record.observations + drop.record.observations:
            if seen.identity in seen_identities:
                continue
            seen_identities.add(seen.identity)
            ordered.append(seen)
        firsts = [d for d in (keep.record.first_seen, drop.record.first_seen) if d]
        lasts = [d for d in (keep.record.last_seen, drop.record.last_seen) if d]
        aliases = tuple(
            dict.fromkeys(
                (*keep.record.aliases, drop_id, *drop.record.aliases)
            )
        )
        merged = replace(
            keep.record,
            observations=tuple(ordered),
            first_seen=min(firsts) if firsts else None,
            last_seen=max(lasts) if lasts else None,
            count=len(ordered),
            aliases=tuple(alias for alias in aliases if alias != keep_id),
        )
        updated = _splice(
            text, [(keep_entry, render_learning(merged)), (drop_entry, None)]
        )
        write_queue_atomically(path, updated)
    chats = len(dict.fromkeys(seen.source for seen in ordered if seen.source))
    return {"kept": keep_id, "dropped": drop_id, "chats": chats}


def prune_observations(
    vault_root: Path, *, workspace: str, today: date | None = None
) -> dict[str, Any]:
    """Archive every Active x1 observation older than the prune horizon."""
    today = today or date.today()
    path = observations_path(vault_root)
    archive = _archive_path(vault_root)
    with queue_lock(path), queue_lock(archive):
        if not path.exists():
            return {"archived": []}
        text = _read(path, "")
        doomed: list[tuple[Observation, LearningEntry]] = []
        for entry in parse_learnings(text, workspace=workspace).entries:
            obs = _observation_of(entry)
            if obs is not None and due_for_prune(obs, today=today):
                doomed.append((obs, entry))
        if not doomed:
            return {"archived": []}
        archived_text = _read(archive, OBSERVATIONS_ARCHIVE_STUB)
        newline = _newline(archived_text)
        if not archived_text.endswith("\n"):
            archived_text += newline
        lines = "".join(f"{entry.source_text}{newline}" for _, entry in doomed)
        write_queue_atomically(archive, archived_text + lines)
        write_queue_atomically(path, _splice(text, [(entry, None) for _, entry in doomed]))
    return {"archived": [obs.record.learning_id for obs, _ in doomed]}
