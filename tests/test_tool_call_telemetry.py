"""Tool-call telemetry: control-surface classification, the telemetry row, and the
archive's ``### Tools`` section."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import AgentRequest, ChatContext, ToolUseEvent, tool_call_facts
from ciao.transcripts import TranscriptStore, parse_archive_turns

CTX = ChatContext(chat_id=1)


@pytest.mark.parametrize(
    ("tool", "raw", "surface"),
    [
        ("Bash", {"command": "ciao chats list"}, "cli"),
        ("Bash", {"command": "   ciao chats list"}, "cli"),
        ("Bash", {"command": "cd /tmp/x && ciao memory search foo"}, "cli"),
        ("Bash", {"command": "cd /a &&  cd /b && ciao status"}, "cli"),
        ("Bash", {"command": "CIAO_DEBUG=1 ciao status"}, "cli"),
        ("bash", {"command": "A=1 B=2 ciao status"}, "cli"),
        ("shell", {"command": "ciao status"}, "cli"),
        ("Bash", {"command": "git status"}, ""),
        ("Bash", {"command": "echo ciao status"}, ""),
        ("Bash", {"command": "ciaobot status"}, ""),
        ("Bash", {"description": "ciao status"}, ""),
        ("Read", {"command": "ciao status"}, ""),
        ("mcp__ciaobot__chat_send", {"text": "hi"}, "mcp"),
        ("mcp__notion__search", {"query": "x"}, ""),
        ("Read", {"file_path": "/a"}, ""),
    ],
)
def test_control_surface_classification(tool: str, raw: dict, surface: str) -> None:
    _, got = tool_call_facts(tool, raw)
    assert got == surface


def test_input_chars_is_the_length_of_the_json_input() -> None:
    raw = {"command": "ciao status"}
    chars, _ = tool_call_facts("Bash", raw)
    assert chars == len(json.dumps(raw, ensure_ascii=False))
    assert tool_call_facts("Bash", {}) == (0, "")


def test_telemetry_row_carries_control_surface_only_when_classified(
    tmp_path: Path,
) -> None:
    from ciao.web.project_chats import ProjectChatManager

    fake = SimpleNamespace(_config=SimpleNamespace(state_path=tmp_path / "state.json"))
    chat = SimpleNamespace(chat_id=7, project_id="p", provider="claude")
    request = AgentRequest(
        prompt="x", model="sonnet", mode="bypass", resume_session=None, images=[]
    )

    def record(tool: str, surface: str) -> dict:
        ProjectChatManager._record_agent_tool_use(
            fake,
            chat,
            request,
            ToolUseEvent(
                type="tool_use", tool_name=tool, tool_use_id="t", control_surface=surface
            ),
        )
        rows = (tmp_path / "agent_tool_calls.jsonl").read_text().splitlines()
        return json.loads(rows[-1])

    assert record("Bash", "cli")["control_surface"] == "cli"
    assert record("mcp__ciaobot__chat_send", "mcp")["control_surface"] == "mcp"
    assert "control_surface" not in record("Read", "")


def _archive(tmp_path: Path, turns: list[dict]) -> Path:
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "memory-vault")
    for index, turn in enumerate(turns, start=1):
        store.record_turn(
            AgentRequest(
                prompt=turn.get("prompt", f"question {index}"),
                model="sonnet",
                mode="bypass",
                resume_session=None,
                images=[],
            ),
            ctx=CTX,
            response_text=turn.get("response", ""),
            effective_model="sonnet",
            session_id=f"ses_{index}",
            usage={"input": 3},
            quota={},
            input_kind="text",
            tool_events=turn.get("tool_events", []),
        )
    archived = store.archive_session(
        ctx=CTX,
        active_model="sonnet",
        last_effective_model="sonnet",
        session_id="ses_1",
    )
    assert archived is not None
    return archived


def test_archive_renders_a_tools_section_after_the_assistant(tmp_path: Path) -> None:
    """One row per call with its input size and error flag; never the input."""
    archived = _archive(
        tmp_path,
        [
            {
                "prompt": "list files",
                "response": "done",
                "tool_events": [
                    {
                        "id": "a",
                        "name": "Bash",
                        "input": {"summary": "rm -rf secret"},
                        "input_chars": 120,
                    },
                    {
                        "id": "b",
                        "name": "Read",
                        "input": {"summary": "notes.md"},
                        "input_chars": 40,
                        "error": True,
                    },
                ],
            },
            {"prompt": "hi", "response": "hello"},
        ],
    )

    body = archived.read_text(encoding="utf-8")
    assert "### Tools\n\n- Bash · in 120\n- Read · in 40 · error\n" in body
    assert "secret" not in body
    assert body.index("### Assistant") < body.index("### Tools") < body.index("## Turn 2")
    assert body.count("### Tools") == 1


def test_a_turn_without_tool_calls_has_no_tools_section(tmp_path: Path) -> None:
    archived = _archive(tmp_path, [{"prompt": "hi", "response": "hello"}])
    assert "### Tools" not in archived.read_text(encoding="utf-8")


def test_tools_section_round_trips_through_parse_archive_turns(tmp_path: Path) -> None:
    """The Tools section is not read as part of either message body."""
    prompt = "run it\n```\ncode\n```"
    response = "ran it\n## Turn 9\n### Tools\nnot a tools section"
    archived = _archive(
        tmp_path,
        [
            {
                "prompt": prompt,
                "response": response,
                "tool_events": [
                    {
                        "id": "a",
                        "name": "Bash",
                        "input": {"summary": "ls"},
                        "input_chars": 7,
                    }
                ],
            },
            {"prompt": "second", "response": "reply two"},
        ],
    )

    turns = parse_archive_turns(archived.read_text(encoding="utf-8"))
    assert [turn["user"] for turn in turns] == [prompt, "second"]
    assert [turn["assistant"] for turn in turns] == [response, "reply two"]
    assert "- Bash · in 7" in turns[0]["trailer"]
    assert "- Bash" not in turns[1]["trailer"]
