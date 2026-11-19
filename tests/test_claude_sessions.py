"""The SDK-free Claude session reader, pinned to claude-agent-sdk 0.2.178 output (#3138 C2a).

The ``*.golden.json`` lists were generated once with the SDK installed and are committed.
They do not change when the SDK is removed, so drift in the port shows up here.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ciao import claude_sessions

FIXTURES = Path(__file__).parent / "fixtures" / "claude_sessions"
CWD = "/workspace/example"
# claude-agent-sdk's slug for CWD (non-alphanumerics become hyphens).
SLUG = "-workspace-example"
SESSIONS = {
    "tool_use": "11111111-2222-4333-8444-555555555551",
    "compact_summary": "11111111-2222-4333-8444-555555555552",
    "subagent_sidechain": "11111111-2222-4333-8444-555555555553",
}


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    (home / "projects" / SLUG).mkdir(parents=True)
    return home


def _install(home: Path, name: str) -> Path:
    target = home / "projects" / SLUG / f"{SESSIONS[name]}.jsonl"
    shutil.copy(FIXTURES / f"{name}.jsonl", target)
    return target


def _rows(messages: list[claude_sessions.SessionMessage]) -> list[dict]:
    return [
        {
            "type": m.type,
            "uuid": m.uuid,
            "session_id": m.session_id,
            "message": m.message,
            "parent_tool_use_id": m.parent_tool_use_id,
            "parent_agent_id": m.parent_agent_id,
        }
        for m in messages
    ]


@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_messages_match_the_sdk_golden(claude_home: Path, name: str) -> None:
    _install(claude_home, name)
    got = _rows(claude_sessions.get_session_messages(SESSIONS[name], directory=CWD))
    golden = json.loads((FIXTURES / f"{name}.golden.json").read_text(encoding="utf-8"))
    assert got == golden


def test_parallel_tool_result_is_recovered_onto_the_chain(claude_home: Path) -> None:
    # r1 answers the first of two parallel calls; a plain parentUuid walk drops it.
    _install(claude_home, "tool_use")
    uuids = [m.uuid for m in claude_sessions.get_session_messages(SESSIONS["tool_use"], directory=CWD)]
    assert "r1" in uuids


def test_sidechain_messages_are_not_visible(claude_home: Path) -> None:
    _install(claude_home, "subagent_sidechain")
    uuids = {m.uuid for m in claude_sessions.get_session_messages(SESSIONS["subagent_sidechain"], directory=CWD)}
    assert not uuids & {"s3", "s4"}


def test_invalid_or_missing_session_gives_no_messages(claude_home: Path) -> None:
    assert claude_sessions.get_session_messages("not-a-uuid", directory=CWD) == []
    assert claude_sessions.get_session_messages(SESSIONS["tool_use"], directory=CWD) == []


def test_read_session_file_returns_the_jsonl_text(claude_home: Path) -> None:
    _install(claude_home, "tool_use")
    content = claude_sessions.read_session_file(SESSIONS["tool_use"], CWD)
    assert content is not None and content.splitlines()[0].startswith("{")
    assert claude_sessions.read_session_file(SESSIONS["compact_summary"], CWD) is None


def test_corrupt_lines_are_skipped() -> None:
    content = '{"type":"user","uuid":"ok"}\nnot json\n{"type":"progress"}\n[1]\n'
    entries = claude_sessions.parse_transcript_entries(content)
    assert [e["uuid"] for e in entries] == ["ok"]


def test_delete_session_file_removes_jsonl_and_subagent_dir(claude_home: Path) -> None:
    jsonl = _install(claude_home, "subagent_sidechain")
    subagents = jsonl.parent / SESSIONS["subagent_sidechain"]
    subagents.mkdir()
    (subagents / "agent.jsonl").write_text("{}\n", encoding="utf-8")

    assert claude_sessions.delete_session_file(SESSIONS["subagent_sidechain"], CWD) is True
    assert not jsonl.exists()
    assert not subagents.exists()
    assert claude_sessions.delete_session_file(SESSIONS["subagent_sidechain"], CWD) is False
    assert claude_sessions.delete_session_file("not-a-uuid", CWD) is False


def test_a_traversing_session_id_cannot_reach_a_file_outside_the_project(claude_home: Path) -> None:
    # "../victim" resolves to projects/victim.jsonl, one level above the project directory.
    victim = claude_home / "projects" / "victim.jsonl"
    victim.write_text(
        '{"type":"user","uuid":"v1","message":{"role":"user","content":"secret"}}\n', encoding="utf-8"
    )
    assert claude_sessions.get_session_messages("../victim", directory=CWD) == []
    assert claude_sessions.delete_session_file("../victim", CWD) is False
    assert victim.exists()


def test_transcript_history_refuses_a_traversing_session_id(claude_home: Path) -> None:
    import ciao.transcripts as transcripts

    victim = claude_home / "projects" / "victim.jsonl"
    victim.write_text(
        '{"type":"user","uuid":"v1","message":{"role":"user","content":"secret"}}\n', encoding="utf-8"
    )
    assert transcripts.get_session_messages_full("../victim", directory=CWD) == []
