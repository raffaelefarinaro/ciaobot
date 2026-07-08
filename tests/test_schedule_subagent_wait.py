from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _dispatch_records(tool_use_id: str, agent_id: str) -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Agent",
                        "input": {
                            "description": f"work {agent_id}",
                            "run_in_background": True,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "content": []}
                ],
            },
            "toolUseResult": {"isAsync": True, "agentId": agent_id},
        },
    ]


def _completion_record(agent_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{agent_id}</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    }


def _seed_chat(pcm: ProjectChatManager, session_id: str):
    project = pcm.create_project("sched-wait", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="sched-wait-test")
    chat.session_id = session_id
    pcm._save()
    return chat


async def test_await_subagents_settles_when_background_agent_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    chat = _seed_chat(pcm, "sess-wait-a")
    session_path = tmp_path / "sess-wait-a.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "curate"}},
        *_dispatch_records("toolu_1", "agent-a"),
    ]

    def flush() -> None:
        session_path.write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    flush()

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root: session_path,
    )

    # Each poll sleep appends the completion notification so running 1 → 0.
    done = iter([_completion_record("agent-a")])

    async def fake_sleep(_seconds: float) -> None:
        rec = next(done, None)
        if rec is not None:
            records.append(rec)
            flush()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    settled, had_async = await pcm._await_schedule_subagents(chat.chat_id)
    assert settled is True
    assert had_async is True


async def test_await_subagents_no_session_returns_no_wait(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sched-wait", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="no-session")
    chat.session_id = ""
    settled, had_async = await pcm._await_schedule_subagents(chat.chat_id)
    assert settled is True
    assert had_async is False


async def test_await_subagents_times_out_while_running(
    tmp_path: Path, monkeypatch
) -> None:
    pcm = _make_manager(tmp_path)
    chat = _seed_chat(pcm, "sess-wait-b")
    session_path = tmp_path / "sess-wait-b.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "curate"}},
        *_dispatch_records("toolu_1", "agent-a"),
    ]
    session_path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )

    from ciao import subagent_tracking

    monkeypatch.setattr(
        subagent_tracking,
        "find_parent_session_file",
        lambda session_id, workspace_root: session_path,
    )

    async def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # The agent never completes; a short deadline forces the timeout branch.
    settled, had_async = await pcm._await_schedule_subagents(
        chat.chat_id, timeout_s=0.05
    )
    assert settled is False
    assert had_async is True


async def test_wait_for_drain_result_returns_recorded_synthesis(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    pcm._last_drain_result["chat-x"] = ("curated 9 chats, 0 changes", False)
    result = await pcm._wait_for_drain_result("chat-x", timeout_s=0.5)
    assert result == ("curated 9 chats, 0 changes", False)


async def test_wait_for_drain_result_times_out_to_none(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    result = await pcm._wait_for_drain_result("chat-absent", timeout_s=0.05)
    assert result is None
