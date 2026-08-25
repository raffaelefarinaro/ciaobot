"""Detect locally-running Claude Code CLI sessions started outside Ciaobot.

Scans ``~/.claude/projects/**/*.jsonl`` (mtime-cached) for session metadata
and ``~/.claude/sessions/*.json`` for the sessionId→pid liveness map the CLI
maintains while a session is running. A session counts as live only when its
pid answers ``os.kill(pid, 0)`` — the same cheap probe claude-code-remote
uses to block two writers on one session.

Read-only by design: this module never resumes, adopts, or writes sessions.
It exists so the node-handover flow can warn when an externally-started CLI
session is actively working in the target workspace.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process we cannot signal still exists.
        return True
    except OSError:
        return False


class NativeSessionScanner:
    """mtime-cached reader over Claude Code's on-disk session records."""

    def __init__(self, claude_dir: Path | None = None) -> None:
        self._claude_dir = claude_dir or (Path.home() / ".claude")
        self._cache: dict[str, tuple[float, dict]] = {}

    def _projects_dir(self) -> Path:
        return self._claude_dir / "projects"

    def _live_pids(self) -> dict[str, int]:
        """sessionId → pid for sessions the CLI reports as running."""
        live: dict[str, int] = {}
        sessions_dir = self._claude_dir / "sessions"
        if not sessions_dir.is_dir():
            return live
        for entry in sessions_dir.glob("*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            sid = data.get("sessionId")
            pid = data.get("pid")
            if isinstance(sid, str) and isinstance(pid, int) and _pid_alive(pid):
                live[sid] = pid
        return live

    def _parse_summary(self, jsonl_path: Path) -> dict | None:
        """Extract {session_id, cwd, updated_at} from one session JSONL."""
        session_id = ""
        cwd = ""
        last_ts = ""
        try:
            with jsonl_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if not session_id:
                        candidate = event.get("sessionId")
                        if isinstance(candidate, str):
                            session_id = candidate
                    candidate_cwd = event.get("cwd")
                    if not cwd and isinstance(candidate_cwd, str):
                        cwd = candidate_cwd
                    candidate_ts = event.get("timestamp")
                    if isinstance(candidate_ts, str):
                        last_ts = candidate_ts
        except OSError:
            return None
        if not session_id:
            return None
        return {
            "session_id": session_id,
            "cwd": cwd,
            "updated_at": last_ts,
            "source_file": str(jsonl_path),
        }

    def scan(self) -> list[dict]:
        """Return live native sessions: [{session_id, pid, cwd, updated_at}].

        Only sessions with a live pid are returned — the scanner exists to
        answer "is a CLI session actively running right now", not to list
        history.
        """
        live = self._live_pids()
        if not live:
            return []
        projects_dir = self._projects_dir()
        if not projects_dir.is_dir():
            return []
        seen: set[str] = set()
        results: list[dict] = []
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    mtime = jsonl_file.stat().st_mtime
                except OSError:
                    continue
                cached = self._cache.get(jsonl_file.name)
                if cached is None or cached[0] != mtime:
                    summary = self._parse_summary(jsonl_file)
                    if summary is None:
                        continue
                    self._cache[jsonl_file.name] = (mtime, summary)
                else:
                    summary = cached[1]
                session_id = summary["session_id"]
                pid = live.get(session_id)
                if pid is None or session_id in seen:
                    continue
                seen.add(session_id)
                results.append({
                    "session_id": session_id,
                    "pid": pid,
                    "cwd": summary["cwd"],
                    "updated_at": summary["updated_at"],
                })
        return results


def _workspace_matches(session_cwd: str, workspace_root: str) -> bool:
    """True when a session's cwd sits inside (or equals) the workspace root."""
    if not session_cwd or not workspace_root:
        return False
    session_path = Path(session_cwd)
    root_path = Path(workspace_root)
    try:
        session_path.relative_to(root_path)
        return True
    except ValueError:
        return False


def live_sessions_for_workspace(
    workspace_root: str, claude_dir: Path | None = None
) -> list[dict]:
    """Live native CLI sessions whose cwd is inside ``workspace_root``."""
    scanner = NativeSessionScanner(claude_dir)
    return [
        session
        for session in scanner.scan()
        if _workspace_matches(str(session.get("cwd") or ""), workspace_root)
    ]
