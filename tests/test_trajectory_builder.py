"""Tests for ``ciao.trajectory_builder``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ciao import trajectory_builder as tb


# ── parse_filtered_jsonl ─────────────────────────────────────────────────


def _filtered_lines(records: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in records)


def test_parse_counts_user_turns_only_when_text() -> None:
    text = _filtered_lines(
        [
            {"idx": 1, "type": "user", "content": [{"type": "text", "text": "hi"}]},
            {"idx": 2, "type": "assistant", "content": [{"type": "text", "text": "yo"}]},
            # tool_result-only user record: doesn't count as a conversation turn
            {
                "idx": 3,
                "type": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "is_error": False,
                        "content": "ok",
                    }
                ],
            },
            {"idx": 4, "type": "user", "content": [{"type": "text", "text": "again"}]},
        ]
    )
    data = tb.parse_filtered_jsonl(text, session_id="abc")
    assert data.session_id == "abc"
    assert data.turns == 2


def test_parse_tallies_tool_uses_and_skills() -> None:
    text = _filtered_lines(
        [
            {
                "idx": 1,
                "type": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "tu_1", "input": {"file_path": "/x"}},
                    {"type": "tool_use", "name": "Read", "id": "tu_2", "input": {"file_path": "/y"}},
                    {"type": "tool_use", "name": "Bash", "id": "tu_3", "input": {"command": "ls"}},
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "tu_4",
                        "input": {"skill": "web-research"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "tu_5",
                        "input": {"skill": "humanizer"},
                    },
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "tu_6",
                        "input": {"skill": "web-research"},  # dup, should be dedup'd
                    },
                ],
            }
        ]
    )
    data = tb.parse_filtered_jsonl(text)
    assert data.tool_counts == {"Read": 2, "Bash": 1, "Skill": 3}
    assert data.skills_loaded == ["web-research", "humanizer"]


def test_parse_collects_error_samples() -> None:
    text = _filtered_lines(
        [
            {
                "idx": 1,
                "type": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_e1",
                        "is_error": True,
                        "content": "Boom: file not found",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_e2",
                        "is_error": True,
                        "content": "x" * 600,
                    },
                ],
            }
        ]
    )
    data = tb.parse_filtered_jsonl(text)
    assert data.error_count == 2
    assert data.error_samples[0]["tool_use_id"] == "tu_e1"
    assert data.error_samples[1]["snippet"].endswith("…")
    assert len(data.error_samples[1]["snippet"]) < 600


def test_parse_skips_malformed_lines() -> None:
    text = "this is not json\n" + json.dumps(
        {"idx": 1, "type": "user", "content": [{"type": "text", "text": "ok"}]}
    )
    data = tb.parse_filtered_jsonl(text)
    assert data.turns == 1


def test_parse_picks_up_command_name_tags_in_text() -> None:
    """Skills loaded via slash command or auto-activation come through as
    ``<command-name>X</command-name>`` markers in user text blocks, not as
    Skill tool_use calls. The parser must surface those too, otherwise we
    massively under-count which skills were actually in play."""
    text = _filtered_lines(
        [
            {
                "idx": 1,
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Some prompt.\n\n"
                            "<command-name>web-research</command-name>\n"
                            "<command-message>Searching the web…</command-message>"
                        ),
                    }
                ],
            },
            {
                "idx": 2,
                "type": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "text": "Now invoking <command-name>humanizer</command-name>",
                    },
                    {
                        "type": "tool_use",
                        "name": "Skill",
                        "id": "tu_1",
                        "input": {"skill": "web-research"},  # dup with text tag
                    },
                ],
            },
        ]
    )
    data = tb.parse_filtered_jsonl(text)
    assert data.skills_loaded == ["web-research", "humanizer"]


def test_parse_command_name_tag_handles_namespaced_skills() -> None:
    text = _filtered_lines(
        [
            {
                "idx": 1,
                "type": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<command-name>frontend-design:frontend-design</command-name>",
                    }
                ],
            }
        ]
    )
    data = tb.parse_filtered_jsonl(text)
    assert data.skills_loaded == ["frontend-design:frontend-design"]


# ── insights parsing helpers ─────────────────────────────────────────────


_INSIGHTS = """\
## Errors
- Web fetch returned 403 -> unresolved [idx=4]
- Pi binary missing -> installed via apt [idx=5]

## Decisions
- Chose Pi over Claude SDK because lower cost [idx=2]
- Use sqlite-vec for vectors [idx=7]

## User corrections
- User said: "no, use defuddle" -> assistant switched [idx=3]
"""


def test_extract_decisions_parses_what_why() -> None:
    decisions = tb.extract_decisions(_INSIGHTS)
    assert decisions[0] == {"what": "Chose Pi over Claude SDK", "why": "lower cost"}
    assert decisions[1] == {"what": "Use sqlite-vec for vectors", "why": ""}


def test_extract_errors_marks_resolution() -> None:
    errs = tb.extract_insight_errors(_INSIGHTS)
    assert errs[0]["resolved"] is False
    assert errs[1]["resolved"] is True


def test_count_section_items_counts_bullets() -> None:
    assert tb.count_section_items(_INSIGHTS, "## User corrections") == 1
    assert tb.count_section_items(_INSIGHTS, "## Decisions") == 2
    assert tb.count_section_items(_INSIGHTS, "## Nothing") == 0


def test_infer_outcome_clean_vs_dirty() -> None:
    assert tb.infer_outcome(errors=0, user_corrections=0) == "success"
    assert tb.infer_outcome(errors=1, user_corrections=0) == "needs_review"
    assert tb.infer_outcome(errors=0, user_corrections=2) == "needs_review"


# ── build_trajectory ─────────────────────────────────────────────────────


def test_build_trajectory_composes_full_record(tmp_path: Path) -> None:
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    archive_dir = workspace_root / "memory-vault" / "Logs" / "Chats"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "session.md"
    archive.write_text("# transcript")

    session_data = tb.SessionData(
        session_id="sess-1",
        turns=3,
        tool_counts={"Read": 5, "Bash": 1},
        skills_loaded=["web-research"],
        error_count=0,
    )
    ts = datetime(2026, 5, 23, 10, 0, tzinfo=UTC)
    record = tb.build_trajectory(
        session_id="sess-1",
        session_data=session_data,
        archive_path=archive,
        insights_text=_INSIGHTS,
        context="Some chat",
        project_id="proj-1",
        chat_id="chat-1",
        task_summary="Test task",
        workspace="personal",
        timestamp=ts,
        workspace_root=workspace_root,
    )
    assert record["session_id"] == "sess-1"
    assert record["timestamp"] == "2026-05-23T10:00:00Z"
    assert record["context"] == "Some chat"
    assert record["workspace"] == "personal"
    assert record["project"] == "proj-1"
    assert record["chat_id"] == "chat-1"
    assert record["task_summary"] == "Test task"
    assert record["skills_loaded"] == ["web-research"]
    assert record["tools_used"][0] == {"name": "Read", "count": 5}
    assert record["tools_used"][1] == {"name": "Bash", "count": 1}
    assert record["turns"] == 3
    assert record["user_corrections"] == 1
    assert record["outcome"] == "needs_review"  # one error + one correction
    assert record["archive_path"].startswith("memory-vault/Logs/Chats/")
    assert len(record["decisions"]) == 2
    assert len(record["errors"]) == 2


def test_build_trajectory_outcome_success_when_clean(tmp_path: Path) -> None:
    archive = tmp_path / "a.md"
    archive.write_text("x")
    record = tb.build_trajectory(
        session_id="ok",
        session_data=tb.SessionData(turns=1),
        archive_path=archive,
        insights_text="",  # no errors, no corrections
    )
    assert record["outcome"] == "success"
    assert record["errors"] == []
    assert record["decisions"] == []


# ── persistence ──────────────────────────────────────────────────────────


def test_write_and_load_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    trajectory = {
        "session_id": "sess-99",
        "timestamp": "2026-05-23T10:00:00Z",
        "skills_loaded": ["web-research"],
        "tools_used": [],
        "turns": 1,
        "outcome": "success",
    }
    path = tb.write_trajectory(trajectory)
    assert path.exists()
    assert path.parent.name == "2026-05"
    loaded = tb.load_trajectory(path)
    assert loaded == trajectory


def test_list_trajectories_filters_by_since_and_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(UTC)
    recent = {
        "session_id": "recent",
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "skills_loaded": ["web-research"],
    }
    old = {
        "session_id": "old",
        "timestamp": (now - timedelta(days=40)).isoformat().replace("+00:00", "Z"),
        "skills_loaded": ["web-research", "humanizer"],
    }
    tb.write_trajectory(recent)
    tb.write_trajectory(old)

    # since-filter: only recent survives
    since = now - timedelta(days=7)
    paths = tb.list_trajectories(since=since)
    assert len(paths) == 1
    assert tb.load_trajectory(paths[0])["session_id"] == "recent"

    # skill-filter: both load humanizer? only `old` does
    paths = tb.list_trajectories(skill="humanizer")
    assert len(paths) == 1
    assert tb.load_trajectory(paths[0])["session_id"] == "old"


def test_prune_old_drops_outdated_months(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Write three trajectories: two old (way past retention), one current
    now = datetime(2026, 5, 23, tzinfo=UTC)
    very_old = datetime(2025, 1, 1, tzinfo=UTC)
    tb.write_trajectory({
        "session_id": "current",
        "timestamp": now.isoformat().replace("+00:00", "Z"),
    })
    tb.write_trajectory({
        "session_id": "old1",
        "timestamp": very_old.isoformat().replace("+00:00", "Z"),
    })
    deleted = tb.prune_old(retention_months=6, now=now)
    assert deleted == 1
    remaining = list(tb.list_trajectories())
    assert len(remaining) == 1
    assert tb.load_trajectory(remaining[0])["session_id"] == "current"


def test_build_and_persist_trajectory_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    archive = workspace_root / "archive.md"
    archive.write_text("transcript")
    filtered = _filtered_lines(
        [
            {"idx": 1, "type": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "idx": 2,
                "type": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "id": "tu_1", "input": {}},
                    {"type": "tool_use", "name": "Skill", "id": "tu_2",
                     "input": {"skill": "web-research"}},
                ],
            },
        ]
    )
    ts = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    path = tb.build_and_persist_trajectory(
        session_id="sess-end2end",
        filtered_jsonl=filtered,
        archive_path=archive,
        insights_text=_INSIGHTS,
        context="C",
        project_id="P",
        chat_id="chat-X",
        task_summary="T",
        workspace="personal",
        workspace_root=workspace_root,
        timestamp=ts,
    )
    assert path is not None and path.exists()
    rec = tb.load_trajectory(path)
    assert rec["session_id"] == "sess-end2end"
    assert rec["skills_loaded"] == ["web-research"]
    assert any(t["name"] == "Read" for t in rec["tools_used"])
    assert rec["outcome"] == "needs_review"
    assert rec["user_corrections"] == 1


def test_build_and_persist_returns_none_for_empty_input(tmp_path: Path) -> None:
    assert tb.build_and_persist_trajectory(
        session_id="",
        filtered_jsonl="",
        archive_path=tmp_path / "x.md",
    ) is None
    assert tb.build_and_persist_trajectory(
        session_id="abc",
        filtered_jsonl="",
        archive_path=tmp_path / "x.md",
    ) is None
