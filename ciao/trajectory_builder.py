"""Session trajectory capture.

A trajectory is a structured JSON record of one Claude Code session: which
skills were loaded, which tools were used, how many turns, errors and user
corrections, and the eventual outcome. It is the raw dataset that powers
``ciao.skill_evolution``: by mining trajectories where a skill was active
but the session went sideways (errors, corrections, low success), we can
propose edits to the skill prompt.

Storage layout::

    ~/.ciao/trajectories/YYYY-MM/<session-id>.json

The data sources are the same JSONL the insights pipeline already
consumes. We reuse ``ciao.insights.filter_session_jsonl``'s line-oriented
output rather than re-reading the raw blob, because the raw blob is
deleted at archive time but the filtered string sticks around inside the
``ArchiveOutcome`` long enough for the post-archive task to consume it.
That keeps the two outputs (insights + trajectory) consistent and avoids
a second filesystem read.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


DEFAULT_RETENTION_MONTHS = 6
_TRAJECTORIES_DIR = Path.home() / ".ciao" / "trajectories"


@dataclass(slots=True)
class SessionData:
    """Aggregated metrics derived from a filtered session JSONL."""

    session_id: str = ""
    turns: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    skills_loaded: list[str] = field(default_factory=list)
    error_count: int = 0
    error_samples: list[dict[str, Any]] = field(default_factory=list)


# ── JSONL parsing ────────────────────────────────────────────────────────


def parse_filtered_jsonl(filtered_jsonl: str, *, session_id: str = "") -> SessionData:
    """Parse the line-oriented JSON from ``filter_session_jsonl`` into metrics.

    Counts user-text turns (tool_result-only user records don't count as a
    conversation turn). Tallies every ``tool_use.name`` and extracts the
    ``skill`` field from ``Skill`` tool inputs. Samples up to five error
    tool_results with a short snippet for downstream inspection.

    Skill discovery is multi-source because the SDK loads skills via three
    different paths and only one shows up as a ``Skill`` tool call:

    * Explicit ``Skill`` tool invocation -> tool_use block (we tally those).
    * Slash command (``/web-research foo``) -> the SDK injects a
      ``<command-name>web-research</command-name>`` system-reminder block
      into the assistant's text context. No tool_use.
    * Description-matched auto-activation -> same ``<command-name>`` tag
      pattern is emitted when the skill is loaded into the prompt.

    Scanning text/thinking blocks for the ``<command-name>`` tag catches
    paths 2 and 3 that the ``Skill`` tool_use scan misses. The CLAUDE.md
    in this repo documents the tag convention.
    """
    tool_counter: Counter[str] = Counter()
    skills: list[str] = []
    error_samples: list[dict[str, Any]] = []
    turns = 0

    def _add_skill(name: str) -> None:
        n = name.strip()
        if len(n) < 2 or n in skills:
            return
        skills.append(n)

    for raw in filtered_jsonl.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        content = rec.get("content")
        if rec.get("type") == "user" and _has_text_block(content):
            turns += 1
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name", "")).strip()
                if not name:
                    continue
                tool_counter[name] += 1
                if name == "Skill":
                    _add_skill(_extract_skill_id(block.get("input")))
            elif btype == "tool_result" and block.get("is_error"):
                if len(error_samples) < 5:
                    error_samples.append({
                        "tool_use_id": str(block.get("tool_use_id", "")),
                        "snippet": _short(block.get("content")),
                    })
            elif btype in ("text", "thinking"):
                text_field = block.get("text") or block.get("thinking") or ""
                if isinstance(text_field, str) and text_field:
                    for match in _COMMAND_NAME_RE.finditer(text_field):
                        _add_skill(match.group(1))

    return SessionData(
        session_id=session_id,
        turns=turns,
        tool_counts=dict(tool_counter),
        skills_loaded=skills,
        error_count=len(error_samples),
        error_samples=error_samples,
    )


# Matches the SDK's slash-command / auto-activation markers. The tag may
# be plain ``<command-name>name</command-name>`` or namespaced
# ``plugin:name``; we keep the full inner string so namespaced skills
# stay distinct.
_COMMAND_NAME_RE = re.compile(r"<command-name>([^<\s]+)</command-name>")


def _has_text_block(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                return True
    return False


def _extract_skill_id(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("skill", "skill_name", "name"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _short(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


# ── Insights parsing ─────────────────────────────────────────────────────


_IDX_SUFFIX = re.compile(r"\s*\[idx=\d+\]\s*$")
_REASON_SPLIT = re.compile(r"\s+because\s+", re.IGNORECASE)


def _section_body(text: str, header: str) -> str:
    """Return the body of ``## Header`` in an insights block."""
    if not text or header not in text:
        return ""
    start = text.index(header) + len(header)
    rest = text[start:]
    m = re.search(r"\n##\s", rest)
    return rest[: m.start()] if m else rest


def extract_decisions(insights_text: str) -> list[dict[str, str]]:
    """Pull the ``## Decisions`` bullets out of an insights block."""
    out: list[dict[str, str]] = []
    for raw in _section_body(insights_text, "## Decisions").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body = _IDX_SUFFIX.sub("", line[2:].strip())
        if not body:
            continue
        parts = _REASON_SPLIT.split(body, maxsplit=1)
        if len(parts) == 2:
            out.append({
                "what": parts[0].rstrip(",. "),
                "why": parts[1].rstrip("."),
            })
        else:
            out.append({"what": body, "why": ""})
    return out


def extract_insight_errors(insights_text: str) -> list[dict[str, Any]]:
    """Pull the ``## Errors`` bullets and mark resolution status."""
    out: list[dict[str, Any]] = []
    for raw in _section_body(insights_text, "## Errors").splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        body = _IDX_SUFFIX.sub("", line[2:].strip())
        if not body:
            continue
        resolved = "unresolved" not in body.lower()
        out.append({"summary": body, "resolved": resolved})
    return out


def count_section_items(insights_text: str, header: str) -> int:
    return sum(
        1
        for line in _section_body(insights_text, header).splitlines()
        if line.strip().startswith("- ")
    )


def infer_outcome(*, errors: int, user_corrections: int) -> str:
    """Heuristic outcome label.

    ``success`` = clean run, no errors and no pushback.
    ``needs_review`` = at least one error or user correction. Subjective,
    refined later via LLM-as-judge; the gate today is just a flag for the
    weekly evolution pass to look at.
    """
    if errors > 0 or user_corrections > 0:
        return "needs_review"
    return "success"


# ── Trajectory assembly ──────────────────────────────────────────────────


def build_trajectory(
    *,
    session_id: str,
    session_data: SessionData,
    archive_path: Path,
    insights_text: str = "",
    context: str = "",
    project_id: str = "",
    chat_id: str = "",
    task_summary: str = "",
    workspace: str = "",
    timestamp: datetime | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Compose a trajectory record from its inputs.

    ``archive_path`` is stored as a path relative to ``workspace_root``
    when possible so trajectories survive workspace moves.
    """
    ts = (timestamp or datetime.now(UTC)).replace(microsecond=0)
    decisions = extract_decisions(insights_text)
    user_corrections = count_section_items(insights_text, "## User corrections")
    insight_errors = extract_insight_errors(insights_text)

    if workspace_root is not None:
        try:
            archive_str = str(archive_path.relative_to(workspace_root))
        except ValueError:
            archive_str = str(archive_path)
    else:
        archive_str = str(archive_path)

    tools_used = [
        {"name": name, "count": count}
        for name, count in sorted(
            session_data.tool_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
    ]

    # If insights extracted errors, prefer those (semantic). Otherwise
    # fall back to raw tool_result error samples.
    errors_field: list[dict[str, Any]] = (
        insight_errors
        if insight_errors
        else [
            {
                "tool_use_id": e["tool_use_id"],
                "snippet": e["snippet"],
                "resolved": False,
            }
            for e in session_data.error_samples
        ]
    )

    outcome = infer_outcome(
        errors=len(insight_errors) + session_data.error_count,
        user_corrections=user_corrections,
    )

    return {
        "session_id": session_id,
        "timestamp": ts.isoformat().replace("+00:00", "Z"),
        "context": context,
        "workspace": workspace,
        "project": project_id,
        "chat_id": chat_id,
        "task_summary": task_summary,
        "skills_loaded": session_data.skills_loaded,
        "tools_used": tools_used,
        "decisions": decisions,
        "user_corrections": user_corrections,
        "errors": errors_field,
        "outcome": outcome,
        "turns": session_data.turns,
        "archive_path": archive_str,
        "insights_path": archive_str,
    }


# ── Storage ──────────────────────────────────────────────────────────────


def trajectories_root() -> Path:
    """Resolve the root each call so tests can monkeypatch ``Path.home``."""
    return Path.home() / ".ciao" / "trajectories"


def trajectory_path_for(
    session_id: str, timestamp: datetime | None = None
) -> Path:
    ts = (timestamp or datetime.now(UTC)).astimezone(UTC)
    return trajectories_root() / ts.strftime("%Y-%m") / f"{session_id}.json"


def write_trajectory(trajectory: dict[str, Any]) -> Path:
    """Persist a trajectory record to disk. Returns the file path."""
    session_id = trajectory.get("session_id") or "session"
    ts_str = trajectory.get("timestamp") or ""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.now(UTC)
    path = trajectory_path_for(session_id, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(trajectory, f, indent=2, ensure_ascii=False)
    logger.info("Wrote trajectory %s", path)
    return path


def build_and_persist_trajectory(
    *,
    session_id: str,
    filtered_jsonl: str,
    archive_path: Path,
    insights_text: str = "",
    context: str = "",
    project_id: str = "",
    chat_id: str = "",
    task_summary: str = "",
    workspace: str = "",
    workspace_root: Path | None = None,
    timestamp: datetime | None = None,
) -> Path | None:
    """One-shot orchestrator used by the post-archive task.

    Parses the filtered JSONL into a ``SessionData``, assembles a
    trajectory record (folding in insights text when available), and
    writes it to ``~/.ciao/trajectories/YYYY-MM/<session-id>.json``.
    Returns the written path or ``None`` if the input is empty.
    """
    if not session_id or not filtered_jsonl:
        return None
    try:
        session_data = parse_filtered_jsonl(filtered_jsonl, session_id=session_id)
        trajectory = build_trajectory(
            session_id=session_id,
            session_data=session_data,
            archive_path=archive_path,
            insights_text=insights_text,
            context=context,
            project_id=project_id,
            chat_id=chat_id,
            task_summary=task_summary,
            workspace=workspace,
            timestamp=timestamp,
            workspace_root=workspace_root,
        )
        return write_trajectory(trajectory)
    except Exception:  # noqa: BLE001 — never crash the post-archive task
        logger.exception(
            "Failed to build/persist trajectory for session %s", session_id
        )
        return None


def list_trajectories(
    *,
    month: str | None = None,
    since: datetime | None = None,
    skill: str | None = None,
    root: Path | None = None,
) -> list[Path]:
    """List trajectory files, optionally filtered.

    ``month`` is ``YYYY-MM``. ``since`` filters by stored ``timestamp``.
    ``skill`` keeps only records whose ``skills_loaded`` includes that
    skill name. Filters compose.
    """
    base = root or trajectories_root()
    if not base.exists():
        return []
    if month:
        candidates = sorted((base / month).glob("*.json")) if (base / month).exists() else []
    else:
        candidates = sorted(base.glob("*/*.json"))

    if since is None and skill is None:
        return candidates

    kept: list[Path] = []
    for path in candidates:
        try:
            rec = load_trajectory(path)
        except (OSError, json.JSONDecodeError):
            continue
        if since is not None:
            ts_str = rec.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
        if skill is not None and skill not in (rec.get("skills_loaded") or []):
            continue
        kept.append(path)
    return kept


def load_trajectory(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def prune_old(
    *,
    retention_months: int = DEFAULT_RETENTION_MONTHS,
    now: datetime | None = None,
    root: Path | None = None,
) -> int:
    """Delete YYYY-MM subdirs older than the retention window.

    Returns the number of trajectory files deleted. Empty month dirs are
    rmdir'd. The cutoff is inclusive of the current month plus
    ``retention_months`` previous months.
    """
    base = root or trajectories_root()
    if not base.exists() or retention_months <= 0:
        return 0
    cutoff = (now or datetime.now(UTC))
    months_total = cutoff.year * 12 + (cutoff.month - 1) - retention_months
    cutoff_y, m_zero = divmod(months_total, 12)
    cutoff_m = m_zero + 1
    deleted = 0
    for sub in base.iterdir():
        if not sub.is_dir():
            continue
        try:
            y_str, m_str = sub.name.split("-")
            yi = int(y_str)
            mi = int(m_str)
        except ValueError:
            continue
        if (yi, mi) < (cutoff_y, cutoff_m):
            for f in sub.glob("*.json"):
                try:
                    f.unlink()
                    deleted += 1
                except OSError:
                    continue
            try:
                sub.rmdir()
            except OSError:
                pass
    return deleted


# ── CLI ──────────────────────────────────────────────────────────────────


def _format_summary(rec: dict[str, Any]) -> str:
    skills = ",".join(rec.get("skills_loaded") or []) or "-"
    return (
        f"{rec.get('timestamp', ''):20s} "
        f"{(rec.get('session_id') or '')[:8]:8s} "
        f"turns={rec.get('turns', 0):3d} "
        f"outcome={(rec.get('outcome') or '?'):12s} "
        f"skills={skills}"
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Ciao session trajectories",
    )
    parser.add_argument("--list", action="store_true", help="list trajectories")
    parser.add_argument("--month", help="filter by YYYY-MM month")
    parser.add_argument("--skill", help="filter to sessions that loaded this skill")
    parser.add_argument(
        "--since-days", type=int, help="only sessions from the last N days"
    )
    parser.add_argument("--show", help="dump a single trajectory by session_id")
    parser.add_argument(
        "--prune",
        type=int,
        metavar="MONTHS",
        help="prune trajectories older than N months",
    )
    args = parser.parse_args(argv)

    if args.prune is not None:
        n = prune_old(retention_months=args.prune)
        print(f"Pruned {n} files")
        return 0

    if args.show:
        for path in list_trajectories():
            try:
                rec = load_trajectory(path)
            except (OSError, json.JSONDecodeError):
                continue
            if rec.get("session_id") == args.show:
                print(json.dumps(rec, indent=2, ensure_ascii=False))
                return 0
        print(f"No trajectory for session_id={args.show}", file=sys.stderr)
        return 1

    since: datetime | None = None
    if args.since_days is not None:
        since = datetime.now(UTC) - timedelta(days=args.since_days)

    paths = list_trajectories(month=args.month, since=since, skill=args.skill)
    for path in paths:
        try:
            rec = load_trajectory(path)
        except (OSError, json.JSONDecodeError):
            continue
        print(_format_summary(rec))
    print(f"\n{len(paths)} trajectories", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
