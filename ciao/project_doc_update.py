"""Archive-time canonical project doc updates from session insights.

When a chat that belongs to a vault-backed project is archived, the insights
pipeline (``ciao/insights.py``) already extracts a ``## Session insights``
section with full chat context in hand. This module folds the material parts
of that section — Decisions and Open loops — into the project's canonical doc
right away, instead of waiting for the nightly ``system-memory-curation``
schedule (which only fires while the server happens to be running).

Safety posture:

* The model is asked for the complete updated doc, or the literal
  ``NO_CHANGES`` sentinel. Anything else is discarded.
* Output that drops existing frontmatter or shrinks the doc by more than
  half is rejected — a truncated or hallucinated rewrite must never replace
  a good doc.
* Writes to the same doc are serialized with a per-path asyncio lock so two
  chats archiving into one project cannot interleave.
* The nightly curation schedule stays on as the cross-chat consolidator.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# Insight sections that justify touching the canonical doc. Mirrors the
# nightly curation prompt ("Decisions, Open loops, or material status
# changes"); errors, snippets, and entities stay out of project docs.
_TRIGGER_SECTIONS = ("Decisions", "Open loops")

_NO_CHANGES = "NO_CHANGES"

_MIN_SIZE_RATIO = 0.5
"""Reject rewrites smaller than this fraction of the current doc."""


_DOC_UPDATE_SYSTEM_PROMPT = """\
You maintain the canonical documentation file for a project.
You receive the current doc and the session-insights section of a chat that
was just archived for this project. Fold in only material changes: decisions
made, open loops added or resolved, status changes.

Rules:
- Preserve the doc's existing frontmatter, structure, headings, and voice.
- Do not invent facts. Do not summarise the chat. Do not append a changelog.
- A bullet tagged `[project: <name>]` belongs to a different project, not
  this one: never fold it into this doc.
- Strip `[idx=N]` citations and bracketed destination tags (`[memory]`,
  `[project]`, `[people: <Name>]`, `[learnings]`, `[review]`) from anything
  you carry over.
- If nothing in the insights materially changes the doc, reply with exactly
  NO_CHANGES and nothing else.
- Otherwise reply with the complete updated doc content and nothing else —
  no code fences, no commentary.
"""


# One lock per doc path; two chats archiving into the same project must not
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


def _strip_code_fence(text: str) -> str:
    """Unwrap a whole-output ```-fence if the model added one anyway."""
    stripped = text.strip()
    match = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", stripped, re.S)
    return match.group(1) if match else stripped


def _is_safe_rewrite(current: str, updated: str) -> bool:
    """Guard against truncated or structure-destroying model output."""
    if not updated or updated == _NO_CHANGES:
        return False
    if updated == current.strip():
        return False
    if current.lstrip().startswith("---") and not updated.startswith("---"):
        logger.info("project doc update rejected: frontmatter dropped")
        return False
    if len(updated) < _MIN_SIZE_RATIO * len(current.strip()):
        logger.info(
            "project doc update rejected: shrank below %d%% of original",
            int(_MIN_SIZE_RATIO * 100),
        )
        return False
    return True


async def update_project_doc(
    *,
    doc_path: Path,
    insights_md: str,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = 300.0,
    error_out: list[str] | None = None,
) -> bool:
    """Fold session insights into the canonical doc. Returns True on write.

    No-ops (returning False) when the doc does not exist, the insights carry
    no Decisions/Open loops, the model reports ``NO_CHANGES``, or the output
    fails the safety guards. Never raises — callers treat this as
    fire-and-forget.

    ``error_out``, when given, records a non-empty reason for an internal
    failure (the provider call raised, the doc was unreadable, the write
    failed). A plain ``False`` return cannot distinguish that from a legitimate
    no-op, so the resumable pipeline passes this list to settle the stage as
    failed rather than succeeded.
    """
    try:
        if not doc_path.is_file():
            return False
        if not insights_warrant_doc_update(insights_md):
            return False

        async with _lock_for(doc_path):
            current = doc_path.read_text(encoding="utf-8")

            from ciao.providers.oneshot import run_oneshot

            prompt = (
                "Current canonical doc:\n\n"
                f"{current}\n\n"
                "---\n\n"
                "Session insights from the just-archived chat:\n\n"
                f"{insights_md}"
            )
            kwargs: dict = {
                "system_prompt": _DOC_UPDATE_SYSTEM_PROMPT,
                "model": model,
                "timeout_s": timeout_s,
            }
            if provider != "claude":
                kwargs.update({"provider": provider, "cwd": cwd})
            output = await run_oneshot(prompt, **kwargs)

            updated = _strip_code_fence(output)
            if not _is_safe_rewrite(current, updated):
                return False
            doc_path.write_text(updated + "\n", encoding="utf-8")
            logger.info("project doc updated from insights: %s", doc_path)
            return True
    except Exception as exc:  # noqa: BLE001 — fire-and-forget, never crash the pipeline
        logger.exception("project doc update failed for %s", doc_path)
        if error_out is not None:
            error_out.append(f"{type(exc).__name__}: {exc}"[:400] or "project doc update failed")
        return False


_PERSON_FOLD_SYSTEM_PROMPT = """\
You maintain a note about one person in a personal knowledge vault.
You receive the current note and one new fact about that person that the
operator has approved. Merge the fact into the note where it belongs: the
section it fits, or a short new line under the most fitting heading.

Rules:
- Preserve the note's existing frontmatter, structure, headings, and voice.
- Do not invent facts, and do not drop or reword anything already there
  except to correct what the new fact directly supersedes.
- Strip `[idx=N]` citations and bracketed destination tags (`[memory]`,
  `[project]`, `[people: <Name>]`, `[learnings]`, `[review]`) from the fact.
- If the note already says what the fact says, reply with exactly
  NO_CHANGES and nothing else.
- Otherwise reply with the complete updated note and nothing else —
  no code fences, no commentary.
"""


async def fold_fact_into_person_note(
    *,
    note_path: Path,
    fact: str,
    model: str,
    timeout_s: float = 300.0,
    error_out: list[str] | None = None,
) -> bool:
    """Merge one approved fact into an existing person note. True on write.

    The accept-time counterpart of :func:`update_project_doc` for `[people]`
    rows: same per-file lock, fence stripping and rewrite guards, with a prompt
    that takes a single approved fact instead of a session's insights. False
    means ``NO_CHANGES`` (``error_out`` left empty) or, with ``error_out``
    filled, a guard rejection, a note edited during the model call, or a
    failure; the note is untouched in every case.
    """
    try:
        if not note_path.is_file() or not fact.strip():
            return False
        async with _lock_for(note_path):
            current = note_path.read_text(encoding="utf-8")

            from ciao.providers.oneshot import run_oneshot

            prompt = (
                "Current person note:\n\n"
                f"{current}\n\n"
                "---\n\n"
                f"Approved fact to merge:\n\n{fact.strip()}"
            )
            output = await run_oneshot(
                prompt,
                system_prompt=_PERSON_FOLD_SYSTEM_PROMPT,
                model=model,
                timeout_s=timeout_s,
            )
            updated = _strip_code_fence(output)
            if updated == _NO_CHANGES or updated == current.strip():
                return False
            if not _is_safe_rewrite(current, updated):
                # Not "already covered": the model produced a merge the guards
                # refused (dropped frontmatter, shrank the note). Say so, or the
                # operator is told to dismiss a fact that was never filed.
                if error_out is not None:
                    error_out.append(
                        "the model's rewrite was rejected (it dropped the "
                        "frontmatter or shrank the note)"
                    )
                return False
            # The per-path lock only serializes this process's folds. A hand
            # edit (or an agent's Edit) during the model call would otherwise be
            # overwritten by a merge computed from the older text.
            if note_path.read_text(encoding="utf-8") != current:
                if error_out is not None:
                    error_out.append(f"{note_path.name} changed during the fold; nothing was written")
                return False
            note_path.write_text(updated + "\n", encoding="utf-8")
            logger.info("person note updated from an accepted proposal: %s", note_path)
            return True
    except Exception as exc:  # noqa: BLE001 — a failed fold keeps the row queued
        logger.exception("person note fold failed for %s", note_path)
        if error_out is not None:
            error_out.append(f"{type(exc).__name__}: {exc}"[:400] or "person note fold failed")
        return False
