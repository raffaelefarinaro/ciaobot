"""Read a workspace's `Workspace/Care-Report.md` into a fixed shape.

The nightly Workspace care run (the `system-memory-curation` schedule) writes
one Markdown checklist per workspace and ticks it as each step finishes. This
module reads that file for the PWA and never writes it: the agent owns the
file, and the app only shows what the file says.

Reading is deterministic, bounded and never raises. A missing file is the
"never run" state; an oversized or unreadable file is reported as such; a file
whose lines do not match the checklist format yields what could be read. The
path is fixed under the workspace's vault and must resolve inside it, so a
symlink planted at `Workspace/Care-Report.md` cannot point the reader at a file
elsewhere on disk.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_RELATIVE_PATH = Path("Workspace") / "Care-Report.md"
UNSURE_RELATIVE_PATH = Path("Workspace") / "Unsure.md"

# A real report is a few kilobytes: a header, under ten step lines and a short
# list. Anything past this is not a checklist the run wrote.
MAX_REPORT_BYTES = 64 * 1024
MAX_STEPS = 40
MAX_NEEDS = 20
MAX_FIELD_CHARS = 600

# The step files the care prompt dispatches (ciao/stock/schedules.json, step 2),
# under the durable-memory procedures' `references/` folder. A test keeps this
# map equal to the prompt's own list.
STEP_FILES: dict[str, str] = {
    "unsure": "curation-pass-1-unsure.md",
    "drain": "drain-legacy-queues.md",
    "regions": "curation-pass-2-regions.md",
    "learnings": "curation-pass-4-learnings.md",
    "stale": "curation-pass-3-stale-notes.md",
    "guide-review": "curation-pass-7-guide-body.md",
    "rules": "curation-pass-7b-decision-rules.md",
    "hygiene": "curation-pass-6-hygiene.md",
}

_STEP_LINE = re.compile(r"^\s*[-*]\s+\[([ xX!])\]\s+([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*:\s*(.*)$")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DATE_LINE = re.compile(r"^\s*(?:date:\s*)?\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
_WEEKLY = re.compile(r"^\s*Weekly done:\s*(.*)$", re.IGNORECASE)
_INTERRUPTED = re.compile(r"^\s*Interrupted last run:\s*(.*)$", re.IGNORECASE)
_NEEDS_HEADING = re.compile(r"^\s*#{2,6}\s+What needs you\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_NUMBERED = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*)$")
_BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*[:\-\u2014]?\s*(.*)$")
_STATES = {" ": "open", "x": "done", "X": "done", "!": "blocked"}
# The em dash the prompt writes, and the ASCII stand-ins a model may use instead.
_EVIDENCE_SEPARATORS = (" \u2014 ", " -- ", " \u2013 ")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "\u2026"


def _split_step(state: str, rest: str) -> dict[str, str]:
    what, tail = rest, ""
    for separator in _EVIDENCE_SEPARATORS:
        if separator in rest:
            what, tail = rest.split(separator, 1)
            break
    evidence, verify = tail, ""
    if state == "blocked":
        evidence = re.sub(r"^blocked:\s*", "", tail, flags=re.IGNORECASE)
    else:
        marker = tail.lower().rfind("; verify:")
        if marker >= 0:
            evidence, verify = tail[:marker], tail[marker + len("; verify:"):]
        elif tail.lower().startswith("verify:"):
            evidence, verify = "", tail[len("verify:"):]
    return {"what": _clip(what), "evidence": _clip(evidence), "verify": _clip(verify)}


def _ids(raw: str) -> list[str]:
    return [part for part in (piece.strip() for piece in re.split(r"[,\s]+", raw)) if part][:MAX_STEPS]


def _needs_item(text: str) -> dict[str, str]:
    text = " ".join(text.split())
    title, detail = text, ""
    bold = _BOLD_LEAD.match(text)
    if bold:
        title, detail = bold.group(1), bold.group(2)
    else:
        colon = text.find(": ")
        if 0 < colon <= 80:
            title, detail = text[:colon], text[colon + 2:]
    lowered = text.lower()
    if "unsure" in lowered:
        action = "unsure"
    elif "agents.md" in lowered or "guide" in lowered or "region" in lowered or "standing memory" in lowered:
        action = "guide"
    else:
        action = "report"
    return {"title": _clip(title.strip()), "detail": _clip(detail.strip()), "action": action}


def parse_care_report(text: str) -> dict[str, Any]:
    """Parse the text of a care report. Pure: no I/O, never raises."""
    updated = ""
    weekly_done = ""
    interrupted: list[str] = []
    steps: list[dict[str, str]] = []
    needs: list[dict[str, str]] = []
    nothing_due = False
    recognised = False
    in_needs = False
    current: list[str] | None = None

    def flush() -> None:
        if current is not None and len(needs) < MAX_NEEDS:
            needs.append(_needs_item(" ".join(current)))

    for line in text.splitlines():
        if in_needs:
            if _HEADING.match(line):
                flush()
                current = None
                in_needs = False
            else:
                numbered = _NUMBERED.match(line)
                if numbered:
                    flush()
                    current = [numbered.group(2)]
                elif current is not None and line.strip():
                    current.append(line.strip())
                continue
        if _NEEDS_HEADING.match(line):
            in_needs = True
            recognised = True
            continue
        step = _STEP_LINE.match(line)
        if step:
            recognised = True
            if len(steps) < MAX_STEPS:
                state = _STATES[step.group(1)]
                steps.append({"id": step.group(2).lower(), "state": state, **_split_step(state, step.group(3))})
            continue
        weekly = _WEEKLY.match(line)
        if weekly:
            recognised = True
            found = _DATE.search(weekly.group(1))
            weekly_done = found.group(1) if found else ""
            continue
        interrupted_line = _INTERRUPTED.match(line)
        if interrupted_line:
            recognised = True
            interrupted = _ids(interrupted_line.group(1))
            continue
        if not updated and not steps and (_HEADING.match(line) or _DATE_LINE.match(line)):
            found = _DATE.search(line)
            if found:
                updated = found.group(1)
                recognised = True
            continue
        if line.strip().lower().rstrip(".") == "nothing due":
            nothing_due = True
            recognised = True
    if in_needs:
        flush()

    counts = {
        "total": len(steps),
        "done": sum(1 for s in steps if s["state"] == "done"),
        "blocked": sum(1 for s in steps if s["state"] == "blocked"),
        "open": sum(1 for s in steps if s["state"] == "open"),
    }
    return {
        "updated": updated,
        "weekly_done": weekly_done,
        "interrupted_ids": interrupted,
        "nothing_due": nothing_due and not steps,
        "steps": steps,
        "needs_you": needs,
        "counts": counts,
        "malformed": not recognised,
    }


def _empty(status: str) -> dict[str, Any]:
    body = parse_care_report("")
    body["malformed"] = False
    return {"status": status, "modified_at": "", **body}


def read_care_report(vault_root: Path) -> dict[str, Any]:
    """Read `<vault_root>/Workspace/Care-Report.md`. Never raises.

    `status` is `missing` (no report yet: care never ran here), `too_large`,
    `unreadable` (including a path that resolves outside the vault) or `ok`.
    """
    try:
        root = vault_root.resolve()
        path = (root / REPORT_RELATIVE_PATH).resolve()
        if not path.is_relative_to(root):
            return _empty("unreadable")
        if not path.exists():
            return _empty("missing")
        if not path.is_file():
            return _empty("unreadable")
        stat = path.stat()
        if stat.st_size > MAX_REPORT_BYTES:
            return _empty("too_large")
        with path.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            return _empty("too_large")
        text = raw.decode("utf-8", errors="replace")
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z")
    except (OSError, ValueError, RuntimeError):
        return _empty("unreadable")
    return {"status": "ok", "modified_at": modified, **parse_care_report(text)}
