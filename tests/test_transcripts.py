from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import SDKSessionInfo, SessionMessage

from ciao.models import AgentRequest, ChatContext
from ciao.transcripts import TranscriptStore, extract_cli_transcripts

CTX = ChatContext(chat_id=1)


def test_transcript_store_archives_markdown_with_usage_totals(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "memory-vault" / "Logs" / "Telegram")
    request = AgentRequest(
        prompt="Remember that Acme kickoff is next week",
        model="sonnet",
        mode="bypass",
        resume_session=None,
        images=[],
    )

    store.record_turn(
        request,
        ctx=CTX,
        response_text="Noted. I will keep that in mind.",
        effective_model="sonnet",
        session_id="sess-1",
        usage={"input_tokens": "10", "output_tokens": "5"},
        quota={},
        input_kind="text",
    )
    store.record_turn(
        request,
        ctx=CTX,
        response_text="Anything else?",
        effective_model="sonnet",
        session_id="sess-1",
        usage={"input_tokens": "4", "output_tokens": "3"},
        quota={"status": "ok"},
        input_kind="text",
    )

    archived = store.archive_session(
        ctx=CTX,
        active_model="sonnet",
        last_effective_model="sonnet",
        session_id="sess-1",
    )

    assert archived is not None
    content = archived.read_text(encoding="utf-8")
    assert "type: telegram-transcript" in content
    assert "turn_count: 2" in content
    assert "input_tokens: 14" in content
    assert "output_tokens: 8" in content
    assert "## Turn 1" in content
    assert "Remember that Acme kickoff is next week" in content
    # Active transcript should be deleted after archiving
    assert not store.current_path(CTX).exists()


def test_transcript_store_handles_missing_archive_root(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "memory-vault" / "Logs" / "Chats")
    request = AgentRequest(
        prompt="hello",
        model="sonnet",
        mode="bypass",
        resume_session=None,
        images=[],
    )

    store.record_turn(
        request,
        ctx=CTX,
        response_text="world",
        effective_model="sonnet",
        session_id="sess-2",
        usage={},
        quota={},
        input_kind="text",
    )

    assert store.current_path(CTX).exists()


def _write_first_line(path: Path, record: dict) -> None:
    """Write a single-line JSONL used only for the entrypoint peek."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_extract_cli_transcripts_converts_jsonl_to_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uses the SDK session APIs to build the transcript.

    The test mocks ``list_sessions`` and ``get_session_messages`` because the
    real SDK reads from ``~/.claude/projects/`` which we cannot redirect to a
    tmp path. For the entrypoint peek we still need real JSONL first-lines on
    disk.
    """
    projects_dir = tmp_path / ".claude" / "projects" / "-tmp-workspace"
    archive_root = tmp_path / "Logs" / "CLI"
    tracking = tmp_path / ".runtime" / "cli_extracted.json"
    workspace_root = tmp_path / "workspace"

    # First-line peeks: one CLI, one bridge session (to be skipped).
    _write_first_line(
        projects_dir / "abc-123.jsonl",
        {
            "type": "user",
            "entrypoint": "cli",
            "message": {"role": "user", "content": "What is 2+2?"},
        },
    )
    _write_first_line(
        projects_dir / "bridge-456.jsonl",
        {
            "type": "user",
            "entrypoint": "sdk-py",
            "message": {"role": "user", "content": "Hello from bridge"},
        },
    )

    # Fake SDK responses.
    sessions = [
        SDKSessionInfo(
            session_id="abc-123",
            summary="What is 2+2?",
            last_modified=1_711_968_001_000,  # 2024-04-01T10:00:01Z-ish
            created_at=1_711_968_000_000,
            cwd=str(workspace_root),
            git_branch="main",
        ),
        SDKSessionInfo(
            session_id="bridge-456",
            summary="Hello from bridge",
            last_modified=1_711_968_002_000,
            created_at=1_711_968_002_000,
            cwd=str(workspace_root),
            git_branch="main",
        ),
    ]
    messages_by_id: dict[str, list[SessionMessage]] = {
        "abc-123": [
            SessionMessage(
                type="user",
                uuid="u1",
                session_id="abc-123",
                message={"role": "user", "content": "What is 2+2?"},
            ),
            SessionMessage(
                type="assistant",
                uuid="a1",
                session_id="abc-123",
                message={
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "4."}],
                    "usage": {"input_tokens": 10, "output_tokens": 3},
                },
            ),
        ],
        "bridge-456": [],  # won't be called anyway, filtered by entrypoint peek
    }

    monkeypatch.setattr("ciao.transcripts.list_sessions", lambda **_kw: sessions)
    monkeypatch.setattr(
        "ciao.transcripts.get_session_messages",
        lambda session_id, **_kw: messages_by_id.get(session_id, []),
    )
    monkeypatch.setattr("ciao.transcripts.list_subagents", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "ciao.transcripts.get_subagent_messages", lambda *_a, **_kw: []
    )
    monkeypatch.setattr("ciao.transcripts._claude_projects_dir", lambda _ws: projects_dir)

    created = extract_cli_transcripts(
        workspace_root=workspace_root,
        archive_root=archive_root,
        tracking_path=tracking,
    )

    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "type: cli-transcript" in content
    assert "What is 2+2?" in content
    assert "4." in content
    assert "turn_count: 1" in content
    assert "subagent_count: 0" in content
    assert "model: claude-sonnet-4-6" in content
    assert "git_branch: main" in content

    tracked = json.loads(tracking.read_text(encoding="utf-8"))
    assert "abc-123" in tracked
    assert "bridge-456" in tracked  # marked as seen even though skipped

    # Re-running should extract nothing new
    re_run = extract_cli_transcripts(
        workspace_root=workspace_root,
        archive_root=archive_root,
        tracking_path=tracking,
    )
    assert len(re_run) == 0


def test_extract_cli_transcripts_includes_subagent_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subagent transcripts should be rendered as a dedicated section.

    Exercises the v0.1.60 ``list_subagents`` / ``get_subagent_messages``
    integration: a parent session that spawned one subagent should produce a
    single markdown file with a ``## Subagents`` section containing the
    subagent's user/assistant turns.
    """
    projects_dir = tmp_path / ".claude" / "projects" / "-tmp-workspace"
    archive_root = tmp_path / "Logs" / "CLI"
    tracking = tmp_path / ".runtime" / "cli_extracted.json"
    workspace_root = tmp_path / "workspace"

    _write_first_line(
        projects_dir / "parent-1.jsonl",
        {
            "type": "user",
            "entrypoint": "cli",
            "message": {"role": "user", "content": "Research Claude SDK."},
        },
    )

    sessions = [
        SDKSessionInfo(
            session_id="parent-1",
            summary="Research Claude SDK.",
            last_modified=1_711_968_001_000,
            created_at=1_711_968_000_000,
            cwd=str(workspace_root),
            git_branch="main",
        ),
    ]
    parent_messages = [
        SessionMessage(
            type="user",
            uuid="u1",
            session_id="parent-1",
            message={"role": "user", "content": "Research Claude SDK."},
        ),
        SessionMessage(
            type="assistant",
            uuid="a1",
            session_id="parent-1",
            message={
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Delegating to researcher."}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        ),
    ]
    subagent_messages = [
        SessionMessage(
            type="user",
            uuid="su1",
            session_id="parent-1",
            message={
                "role": "user",
                "content": "Summarize v0.1.60 release notes.",
            },
        ),
        SessionMessage(
            type="assistant",
            uuid="sa1",
            session_id="parent-1",
            message={
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {
                        "type": "text",
                        "text": "Added list_subagents and get_subagent_messages.",
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 8},
            },
        ),
    ]

    monkeypatch.setattr("ciao.transcripts.list_sessions", lambda **_kw: sessions)
    monkeypatch.setattr(
        "ciao.transcripts.get_session_messages",
        lambda session_id, **_kw: parent_messages if session_id == "parent-1" else [],
    )
    monkeypatch.setattr(
        "ciao.transcripts.list_subagents",
        lambda session_id, **_kw: ["agent-xyz"] if session_id == "parent-1" else [],
    )
    monkeypatch.setattr(
        "ciao.transcripts.get_subagent_messages",
        lambda session_id, agent_id, **_kw: (
            subagent_messages if agent_id == "agent-xyz" else []
        ),
    )
    monkeypatch.setattr("ciao.transcripts._claude_projects_dir", lambda _ws: projects_dir)

    created = extract_cli_transcripts(
        workspace_root=workspace_root,
        archive_root=archive_root,
        tracking_path=tracking,
    )

    assert len(created) == 1
    content = created[0].read_text(encoding="utf-8")
    assert "subagent_count: 1" in content
    assert "## Subagents" in content
    assert "### Subagent `agent-xyz`" in content
    assert "Summarize v0.1.60 release notes." in content
    assert "Added list_subagents and get_subagent_messages." in content
