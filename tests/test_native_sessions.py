"""Tests for native Claude Code CLI session detection (W3).

The scanner reads ~/.claude/projects/**/*.jsonl (mtime-cached) and
~/.claude/sessions/*.json (sessionId→pid liveness map). A session is live
only when its pid answers os.kill(pid, 0). The /api/native/sessions endpoint
filters live sessions to a workspace root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.native_sessions import (
    NativeSessionScanner,
    live_sessions_for_workspace,
    _workspace_matches,
)
from ciao.web.routes_api import native_sessions


def _make_claude_dir(tmp_path: Path, sessions: dict[str, int]) -> Path:
    """Fake ~/.claude with one project dir, one session JSONL, and the
    liveness map."""
    claude_dir = tmp_path / ".claude"
    projects = claude_dir / "projects" / "-Users-me-repos-demo"
    projects.mkdir(parents=True)
    session_id = "11111111-2222-3333-4444-555555555555"
    entries = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": "/Users/me/repos/demo",
            "timestamp": "2026-08-22T10:00:00Z",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": "/Users/me/repos/demo",
            "timestamp": "2026-08-22T10:00:05Z",
            "message": {"role": "assistant", "content": "hello"},
        },
    ]
    (projects / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    sessions_dir = claude_dir / "sessions"
    sessions_dir.mkdir()
    for sid, pid in sessions.items():
        (sessions_dir / f"{sid}.json").write_text(
            json.dumps({"sessionId": sid, "pid": pid}), encoding="utf-8"
        )
    return claude_dir


def test_live_session_with_running_pid_is_detected(tmp_path: Path) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    # This test process is a guaranteed-live pid.
    claude_dir = _make_claude_dir(tmp_path, {session_id: os.getpid()})

    scanner = NativeSessionScanner(claude_dir)
    results = scanner.scan()

    assert len(results) == 1
    row = results[0]
    assert row["session_id"] == session_id
    assert row["pid"] == os.getpid()
    assert row["cwd"] == "/Users/me/repos/demo"
    assert row["updated_at"] == "2026-08-22T10:00:05Z"


def test_dead_or_missing_pid_is_not_live(tmp_path: Path) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    dead_pid = 2_000_000_000  # far above any real pid ceiling
    claude_dir = _make_claude_dir(tmp_path, {session_id: dead_pid})

    scanner = NativeSessionScanner(claude_dir)
    assert scanner.scan() == []

    # No sessions dir at all → nothing live either.
    empty = NativeSessionScanner(tmp_path / "no-claude")
    assert empty.scan() == []


def test_scan_reuses_mtime_cache_for_unchanged_files(tmp_path: Path) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    claude_dir = _make_claude_dir(tmp_path, {session_id: os.getpid()})
    jsonl = next((claude_dir / "projects").glob("*/*.jsonl"))
    scanner = NativeSessionScanner(claude_dir)

    first = scanner.scan()
    assert len(first) == 1

    # Corrupt the file WITHOUT changing its mtime: a rescan must serve the
    # cached summary rather than re-parsing.
    original_stat = jsonl.stat()
    jsonl.write_text("garbage\n", encoding="utf-8")
    import os as _os

    _os.utime(jsonl, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert len(scanner.scan()) == 1


def test_workspace_match_is_prefix_based() -> None:
    assert _workspace_matches("/Users/me/repos/demo/src", "/Users/me/repos/demo")
    assert _workspace_matches("/Users/me/repos/demo", "/Users/me/repos/demo")
    assert not _workspace_matches("/Users/me/repos/demo-other", "/Users/me/repos/demo")
    assert not _workspace_matches("", "/Users/me/repos/demo")


def test_live_sessions_for_workspace_filters_by_root(tmp_path: Path) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    claude_dir = _make_claude_dir(tmp_path, {session_id: os.getpid()})

    inside = live_sessions_for_workspace("/Users/me/repos/demo", claude_dir)
    outside = live_sessions_for_workspace("/somewhere/else", claude_dir)

    assert len(inside) == 1
    assert outside == []


def _request(config, workspace: str | None) -> Request:
    query = f"workspace={workspace}" if workspace else ""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/native/sessions",
        "headers": [],
        "query_string": query.encode(),
        "app": SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(workspace_root="/Users/me/repos/demo")
            ),
        ),
    })


@pytest.mark.asyncio
async def test_native_sessions_endpoint_returns_live_rows(tmp_path: Path) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    claude_dir = _make_claude_dir(tmp_path, {session_id: os.getpid()})

    response = await native_sessions(_request(None, str(claude_dir.parent)))
    payload = json.loads(response.body.decode())

    assert payload["workspace"] == str(claude_dir.parent)
    assert payload["checked_at"]
    assert payload["sessions"] == []  # no ~/.claude under that root


@pytest.mark.asyncio
async def test_native_sessions_endpoint_defaults_to_configured_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "11111111-2222-3333-4444-555555555555"
    claude_dir = _make_claude_dir(tmp_path, {session_id: os.getpid()})
    monkeypatch.setenv("HOME", str(tmp_path))
    # Point Path.home() at the fake home for the scan.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    response = await native_sessions(_request(None, None))
    payload = json.loads(response.body.decode())

    assert payload["workspace"] == "/Users/me/repos/demo"
    assert [s["session_id"] for s in payload["sessions"]] == [session_id]
