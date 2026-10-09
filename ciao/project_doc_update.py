"""Canonical project doc and people-note folds on the memory accept path.

A ``[project]`` bullet accepted from the proposals queue is folded into the
project's canonical doc, and an accepted ``[people: <Name>]`` bullet is merged
into that person's existing note, right away, instead of waiting for the
nightly ``system-memory-curation`` schedule (which only fires while the server
happens to be running).

The model decides *where* a fact belongs and how to phrase it, and nothing
else: it answers one JSON object — ``covered``, ``add`` or ``update`` — over
the note's current bullets, handed to it as numbered entries. The write is
then one list item through :mod:`ciao.note_receipts` (an
:func:`~ciao.note_receipts.apply_entry_edit` splice or an
:func:`~ciao.note_receipts.append_list_item` append), revision-checked,
journaled and undoable byte for byte. A fact that used to be merged into a
sentence becomes a new bullet instead: person notes that keep their facts in
prose gain a bullet under ``## Notes`` and their prose is never rewritten.

Writes to the same doc are serialized with a per-path asyncio lock so two
accepts into one project cannot interleave, and the entry write re-checks the
revision under the note's own lock, so a hand edit that lands during the model
call is a refusal rather than an overwrite. The nightly curation schedule
stays on as the cross-chat consolidator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Insight sections that justify touching the canonical doc. Mirrors the
# nightly curation prompt ("Decisions, Open loops, or material status
# changes"); errors, snippets, and entities stay out of project docs.
_TRIGGER_SECTIONS = ("Decisions", "Open loops")

_FOLD_SECTIONS = ("Decisions", "Open loops", "Notes")
"""Sections a fold may file a new bullet under."""


_DOC_UPDATE_SYSTEM_PROMPT = """\
You maintain the canonical documentation file for a project.
You receive the file's current bullets as numbered entries and a short set of
material facts from a conversation that just ended in this project. Decide
where each fact belongs: a new bullet, a rewording of one entry, or nothing.

Rules:
- Do not invent facts. Do not summarise the chat. Do not append a changelog.
- A bullet tagged `[project: <name>]` belongs to a different project, not
  this one: never fold it into this doc.
- Strip `[idx=N]` citations and bracketed destination tags (`[memory]`,
  `[project]`, `[people: <Name>]`, `[learnings]`, `[review]`) from anything
  you carry over.
- Reply with one JSON object and nothing else — no code fences, no commentary:
  - {"action":"covered"} when the entries already say it all.
  - {"action":"add","section":"Decisions","text":"- one bullet"} to file a new
    bullet. section is Decisions, Open loops, or Notes.
  - {"action":"update","index":1,"text":"- one bullet"} to reword entry 1.
    index is one of the numbered entries below.
- text is one list item with no trailing newline.
- Never add or change a `[verified: YYYY-MM-DD]` stamp: a new or reworded
  bullet carries no stamp, and re-dating a bullet that already says the
  fact is not an update.
"""


# One lock per doc path; two accepts into the same project must not
# interleave their read-modify-write cycles.
_doc_locks: dict[str, asyncio.Lock] = {}


def _lock_for(doc_path: Path) -> asyncio.Lock:
    key = str(doc_path.resolve())
    lock = _doc_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _doc_locks[key] = lock
    return lock


def insights_warrant_doc_update(insights_md: str) -> bool:
    """True when the insights contain at least one Decisions/Open loops bullet."""
    if not insights_md.strip():
        return False
    from ciao.memory_proposals import _split_sections

    sections = _split_sections(insights_md)
    return any(sections.get(heading) for heading in _TRIGGER_SECTIONS)


@dataclass(frozen=True, slots=True)
class _FoldDecision:
    """One parsed fold reply: what to write, or nothing."""

    action: str
    section: str = ""
    text: str = ""
    index: int = 0


class _FoldRefused(Exception):
    """The fold reply was not a single entry edit, so nothing may be written."""


def _numbered_entries(
    current: str, *, note_path: str, workspace: str
) -> tuple[str, list[Any]]:
    """The note's supported bullets as ``1. section: <S>`` lines plus their text.

    Only supported entries are numbered, because only those can be addressed
    back: the model's ``index`` names a position in this list, 1-based. Returns
    the prompt block and the entries behind it, in the same order.
    """
    from ciao import note_entries as ne

    document = ne.parse_note_entries(
        current, note_path=note_path, workspace=workspace
    )
    supported = [entry for entry in document.entries if entry.supported]
    lines = [
        f"{number}. section: {entry.section or '(none)'}\n{entry.text}"
        for number, entry in enumerate(supported, 1)
    ]
    return "\n\n".join(lines), supported


def _check_single_item(text: Any, *, note_path: str, workspace: str) -> str:
    """``text`` as one supported list item and nothing else, or a refusal.

    The same one-item check :func:`ciao.note_receipts.compose_entry_edit`
    applies to a replacement — exactly one entry, starting at offset zero,
    covering the whole string — plus the supported-entry rule
    :func:`ciao.note_receipts.append_list_item` applies to a new bullet.
    """
    from ciao import note_entries as ne

    if not isinstance(text, str):
        raise _FoldRefused("the fold reply was not a single entry edit")
    document = ne.parse_note_entries(text, note_path=note_path, workspace=workspace)
    if len(document.entries) != 1 or document.entries[0].start != 0:
        raise _FoldRefused("the fold reply was not a single entry edit")
    only = document.entries[0]
    if only.end != len(text) or document.uncovered or not only.supported:
        raise _FoldRefused("the fold reply was not a single entry edit")
    return text


def _read_exact_text(note: Path) -> str:
    """The note's exact decoded text, with no newline normalization.

    ``Path.read_text`` universal-newlines the file on read, so a CRLF note
    comes back with LF endings and its revision no longer matches the one a
    byte-exact writer checks — every fold then fails as a conflict without
    any concurrent edit. Reading bytes keeps CRLF pairs, a BOM and a missing
    final newline exactly as they are, so the revision below and the undo
    the receipt journals are byte-exact too.
    """
    from ciao import memory_receipts as mr

    try:
        return note.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise mr.MemoryReceiptError(f"{note} is not valid UTF-8: {exc}") from exc


def _strip_opening_claim(text: str) -> str:
    """``text`` with the proposed bullet's trailing verification claim cut.

    Only the trailing claim on the opening line — the span the entry parser
    reads as this entry's verification — goes. A date-shaped token further
    left on the line is the fact's own prose about a date, and cutting it
    would rewrite the fact.
    """
    from ciao import note_entries as ne

    opening, separator, rest = text.partition("\n")
    stripped = ne.strip_trailing_stamp(opening)
    if stripped == opening:
        return text
    return stripped + separator + rest


def _sanitize_fold_stamp(text: str, entry: Any | None) -> str:
    """``text`` with only the verification stamp an ordinary fold may keep.

    A fold is not a verification: nobody went and checked anything, so a
    changed or new fact cannot gain or keep a ``[verified:]`` stamp — the
    trailing claim is cut off the proposed bullet. An unchanged fact cannot
    be re-dated either: when the proposed words fingerprint identically to
    the entry they replace, the entry's own text is the replacement, stamp
    and all, which makes the write the no-op it honestly is. Untouched
    neighbors never enter this function — the write splices one span or
    appends one bullet — so their bytes cannot move.
    """
    from ciao import note_entries as ne

    if entry is None:
        return _strip_opening_claim(text)
    if ne.refresh_fingerprint(text) == entry.fingerprint:
        return str(entry.text)
    return _strip_opening_claim(text)


_WHOLE_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n```", re.S)


def _unwrap_fold_fence(output: str) -> str:
    """``output`` with one surrounding code fence removed, or as it was.

    The reply is asked to be bare JSON, but a model may still wrap it in a
    single ```json (or bare ```) fence. Only a fence that spans the whole
    reply is removed; text outside it stays, and the JSON parse refuses it.
    """
    stripped = output.strip()
    match = _WHOLE_FENCE.fullmatch(stripped)
    return match.group(1) if match else stripped


def _parse_fold_reply(
    output: str, entries: list[Any], *, note_path: str, workspace: str
) -> _FoldDecision:
    """The model's JSON reply as a decision, or a refusal.

    A fence that is not the whole reply, a non-object, an unknown action, an
    index that is not one of the numbered entries, a section outside
    :data:`_FOLD_SECTIONS`, or a ``text`` that is not one list item are all the
    same outcome: do not write.
    """
    try:
        reply = json.loads(_unwrap_fold_fence(output))
    except (json.JSONDecodeError, ValueError):
        raise _FoldRefused("the fold reply was not a single entry edit") from None
    if not isinstance(reply, dict):
        raise _FoldRefused("the fold reply was not a single entry edit")
    action = reply.get("action")
    if action == "covered":
        return _FoldDecision(action="covered")
    if action == "add":
        section = reply.get("section")
        if section not in _FOLD_SECTIONS:
            raise _FoldRefused("the fold reply was not a single entry edit")
        text = _check_single_item(
            reply.get("text"), note_path=note_path, workspace=workspace
        )
        return _FoldDecision(action="add", section=str(section), text=text)
    if action == "update":
        index = reply.get("index")
        if type(index) is not int or not 1 <= index <= len(entries):
            raise _FoldRefused("the fold reply was not a single entry edit")
        text = _check_single_item(
            reply.get("text"), note_path=note_path, workspace=workspace
        )
        return _FoldDecision(action="update", text=text, index=index)
    raise _FoldRefused("the fold reply was not a single entry edit")


def _vault_for_note(note: Path) -> Path:
    """The vault ``note`` lives in: the nearest ancestor owning ``Workspace/``.

    The entry writes journal through the vault's own journal, so the vault is
    read off the note's location — the directory whose ``Workspace/`` folder
    holds the queue and the receipts — rather than guessed from configuration
    the fold was never given.
    """
    for ancestor in (note.parent, *note.parent.parents):
        if (ancestor / "Workspace").is_dir():
            return ancestor
    raise _FoldRefused(
        f"{note.name} is not inside a vault (no Workspace/ folder above it)"
    )


async def update_project_doc(
    *,
    doc_path: Path,
    insights_md: str,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = 300.0,
    error_out: list[str] | None = None,
    workspace: str = "",
) -> bool:
    """Fold the Decisions/Open loops of *insights_md* into the canonical doc.

    Returns True on write. No-ops (returning False) when the doc does not
    exist, the input carries no Decisions/Open loops, or the model reports
    ``covered`` or answers anything but a single entry edit. Never raises —
    callers treat this as fire-and-forget.

    ``error_out``, when given, records a non-empty reason for an internal
    failure (the provider call raised, the doc was unreadable, the write
    failed). A plain ``False`` return cannot distinguish that from a legitimate
    no-op, so the resumable pipeline passes this list to settle the stage as
    failed rather than succeeded.

    ``workspace`` is the row's workspace: it coordinates the entry identities
    the reply's ``index`` addresses, and the receipt the write journals.
    """
    try:
        if not doc_path.is_file():
            return False
        if not insights_warrant_doc_update(insights_md):
            return False

        async with _lock_for(doc_path):
            current = _read_exact_text(doc_path)
            vault_root = _vault_for_note(doc_path)
            try:
                relative_path = doc_path.relative_to(vault_root).as_posix()
            except ValueError:
                raise _FoldRefused(
                    f"{doc_path.name} is not inside its vault"
                ) from None
            numbered, entries = _numbered_entries(
                current, note_path=relative_path, workspace=workspace
            )

            from ciao.providers.oneshot import run_oneshot

            prompt = (
                "Current canonical doc as numbered entries (reply with JSON, "
                "never with the file):\n\n"
                f"{numbered}\n\n"
                "---\n\n"
                "Decisions to fold in (the accepted facts addressed to this "
                "doc):\n\n"
                f"{insights_md}"
            )
            output = await run_oneshot(
                prompt,
                system_prompt=_DOC_UPDATE_SYSTEM_PROMPT,
                model=model,
                timeout_s=timeout_s,
                provider=provider,
                cwd=cwd,
            )
            try:
                decision = _parse_fold_reply(
                    output, entries, note_path=relative_path, workspace=workspace
                )
            except _FoldRefused:
                return False
            if decision.action == "covered":
                return False

            from ciao import memory_receipts as mr
            from ciao import note_receipts as nr

            expected_revision = mr.content_revision(current)
            if decision.action == "add":
                receipt = nr.append_list_item(
                    vault_root=vault_root,
                    relative_path=relative_path,
                    expected_revision=expected_revision,
                    section=decision.section,
                    item=_sanitize_fold_stamp(decision.text, None),
                    actor="proposal-accept",
                    source="fold",
                    workspace=workspace,
                )
            else:
                entry = entries[decision.index - 1]
                receipt = nr.apply_entry_edit(
                    vault_root=vault_root,
                    relative_path=relative_path,
                    expected_revision=expected_revision,
                    identity=entry.identity,
                    fingerprint=entry.fingerprint,
                    replacement=_sanitize_fold_stamp(decision.text, entry),
                    actor="proposal-accept",
                    source="fold",
                    workspace=workspace,
                )
            if not receipt.get("changed", True):
                # The replacement spliced to the bytes already there (an
                # update restating the entry, or a re-date put back): the
                # receipt journals the no-change and nothing was replaced.
                return False
            logger.info("project doc updated from insights: %s", doc_path)
            return True
    except _FoldRefused:
        # The doc is not inside a vault, so no entry write can journal it.
        # Silent, like a missing doc: there is nothing a retry could do.
        return False
    except Exception as exc:  # noqa: BLE001 — fire-and-forget, never crash the pipeline
        logger.exception("project doc update failed for %s", doc_path)
        if error_out is not None:
            error_out.append(f"{type(exc).__name__}: {exc}"[:400] or "project doc update failed")
        return False


_PERSON_FOLD_SYSTEM_PROMPT = """\
You maintain a note about one person in a personal knowledge vault.
You receive the note's current bullets as numbered entries and one new fact
about that person that the operator has approved. Decide where the fact
belongs: a new bullet, a rewording of one entry, or nothing.

Rules:
- Do not invent facts, and do not drop or reword anything already there
  except to correct what the new fact directly supersedes.
- Strip `[idx=N]` citations and bracketed destination tags (`[memory]`,
  `[project]`, `[people: <Name>]`, `[learnings]`, `[review]`) from the fact.
- Reply with one JSON object and nothing else — no code fences, no commentary:
  - {"action":"covered"} when the entries already say what the fact says.
  - {"action":"add","section":"Notes","text":"- one bullet"} to file the fact
    as a new bullet. section is Decisions, Open loops, or Notes.
  - {"action":"update","index":1,"text":"- one bullet"} to reword entry 1.
    index is one of the numbered entries below.
- text is one list item with no trailing newline.
- Never add or change a `[verified: YYYY-MM-DD]` stamp: a new or reworded
  bullet carries no stamp, and re-dating a bullet that already says the
  fact is not a merge.
"""


async def fold_fact_into_person_note(
    *,
    note_path: Path,
    fact: str,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = 300.0,
    error_out: list[str] | None = None,
    workspace: str = "",
) -> bool:
    """Merge one approved fact into an existing person note. True on write.

    The accept-time counterpart of :func:`update_project_doc` for `[people]`
    rows: same per-file lock, numbered-entries prompt and single-entry writes,
    with a prompt that takes a single approved fact instead of a session's
    insights. False means ``covered`` (``error_out`` carries the
    dismiss-instead sentence) or, with ``error_out`` filled, a reply that was
    not a single entry edit, a note edited during the model call, or a
    failure; the note is untouched in every case.

    A prose note keeps its prose: the fold never rewrites the file, so a note
    whose facts live outside bullets gains the fact as a new bullet under
    ``## Notes``.
    """
    try:
        if not note_path.is_file() or not fact.strip():
            return False
        async with _lock_for(note_path):
            current = _read_exact_text(note_path)
            vault_root = _vault_for_note(note_path)
            try:
                relative_path = note_path.relative_to(vault_root).as_posix()
            except ValueError:
                raise _FoldRefused(
                    f"{note_path.name} is not inside its vault"
                ) from None
            numbered, entries = _numbered_entries(
                current, note_path=relative_path, workspace=workspace
            )

            from ciao.providers.oneshot import run_oneshot

            prompt = (
                "Current person note as numbered entries (reply with JSON, "
                "never with the note):\n\n"
                f"{numbered}\n\n"
                "---\n\n"
                f"Approved fact to merge:\n\n{fact.strip()}"
            )
            output = await run_oneshot(
                prompt,
                system_prompt=_PERSON_FOLD_SYSTEM_PROMPT,
                model=model,
                timeout_s=timeout_s,
                provider=provider,
                cwd=cwd,
            )
            try:
                decision = _parse_fold_reply(
                    output, entries, note_path=relative_path, workspace=workspace
                )
            except _FoldRefused as exc:
                if error_out is not None:
                    error_out.append(str(exc))
                return False
            if decision.action == "covered":
                if error_out is not None:
                    error_out.append(
                        "the fold reported no changes; dismiss instead"
                    )
                return False
            # The per-path lock only serializes this process's folds. A hand
            # edit (or an agent's Edit) during the model call would otherwise be
            # overwritten by a merge computed from the older text. Compared by
            # exact bytes: a text read would normalize a CRLF note and report
            # a concurrent edit that never happened.
            if _read_exact_text(note_path) != current:
                if error_out is not None:
                    error_out.append(f"{note_path.name} changed during the fold; nothing was written")
                return False

            from ciao import memory_receipts as mr
            from ciao import note_receipts as nr

            expected_revision = mr.content_revision(current)
            if decision.action == "add":
                receipt = nr.append_list_item(
                    vault_root=vault_root,
                    relative_path=relative_path,
                    expected_revision=expected_revision,
                    section=decision.section,
                    item=_sanitize_fold_stamp(decision.text, None),
                    actor="proposal-accept",
                    source="fold",
                    workspace=workspace,
                )
            else:
                entry = entries[decision.index - 1]
                receipt = nr.apply_entry_edit(
                    vault_root=vault_root,
                    relative_path=relative_path,
                    expected_revision=expected_revision,
                    identity=entry.identity,
                    fingerprint=entry.fingerprint,
                    replacement=_sanitize_fold_stamp(decision.text, entry),
                    actor="proposal-accept",
                    source="fold",
                    workspace=workspace,
                )
            if not receipt.get("changed", True):
                # The replacement spliced to the bytes already there (an
                # update restating the entry, or a re-date put back): dismiss
                # instead, like a covered reply.
                if error_out is not None:
                    error_out.append(
                        "the fold reported no changes; dismiss instead"
                    )
                return False
            logger.info("person note updated from an accepted proposal: %s", note_path)
            return True
    except _FoldRefused as exc:
        # The note is not inside a vault, so no entry write can journal it.
        if error_out is not None:
            error_out.append(str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 — a failed fold keeps the row queued
        logger.exception("person note fold failed for %s", note_path)
        if error_out is not None:
            error_out.append(f"{type(exc).__name__}: {exc}"[:400] or "person note fold failed")
        return False
