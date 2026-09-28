from __future__ import annotations

from pathlib import Path

import pytest

from ciao.models import AgentRequest, ChatContext
from ciao.transcripts import TranscriptStore, read_archive_skills

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
    assert "type:" not in content
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


def test_current_messages_hide_the_injected_context_envelope(tmp_path: Path) -> None:
    """The stored prompt keeps the envelope (chat recovery parses it), but the
    rendered chat rows must show only what the user typed."""
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "archives")
    envelope = "[CIAO_CONTEXT_BEGIN]\n[Project: \"General\"]\n[CIAO_CONTEXT_END]\n\n"
    request = AgentRequest(
        prompt=f"{envelope}hello",
        model="opencode/big-pickle",
        mode="auto",
        provider="opencode",
        display_prompt=f"{envelope}hello",
    )

    store.record_turn(
        request,
        ctx=CTX,
        response_text="world",
        effective_model="opencode/big-pickle",
        session_id="ses_1",
        usage={},
        quota={},
        input_kind="text",
        provider="opencode",
    )
    rows = store.current_messages(CTX, "opencode")

    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "hello"
    assert rows[1] == {
        "role": "assistant",
        "content": "world",
        "sent_at": rows[1]["sent_at"],
        "effective_model": "opencode/big-pickle",
    }
    # The envelope is still on disk for recovery.
    assert "[CIAO_CONTEXT_BEGIN]" in store.current_path(CTX, "opencode").read_text(
        encoding="utf-8"
    )


def test_filtered_jsonl_numbers_turns_from_one(tmp_path: Path) -> None:
    """Citation indices must mean the same thing on every provider.

    The extraction prompt tells the model "Indices start at 1; never cite
    `[idx=0]`", and `ciao.insights.filter_session_jsonl` obeys it. This builder
    started at 0, so a correct `[idx=1]` citation resolved to the assistant
    turn — which the source-evidence gate reads as an unsupported fact and
    queues, for every opencode chat.
    """
    import json

    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "archives")
    request = AgentRequest(
        prompt="always deploy on Thursdays",
        model="opencode/big-pickle",
        mode="auto",
        provider="opencode",
    )
    store.record_turn(
        request,
        ctx=CTX,
        response_text="Noted.",
        effective_model="opencode/big-pickle",
        session_id="ses_1",
        usage={},
        quota={},
        input_kind="text",
        provider="opencode",
    )

    records = [
        json.loads(line)
        for line in store.current_filtered_jsonl(CTX, "opencode").splitlines()
    ]

    assert [(r["idx"], r["type"]) for r in records] == [(1, "user"), (2, "assistant")]


# ── Skill evidence in the archive ─────────────────────────────────────────
#
# The memory pass reads the archive and files a skill-improvement proposal
# against it, citing the turn the skill was used in. A turn's rendered blocks
# are the user's and the agent's prose, so the skill's name is the one thing
# that was not in the archive at all.


def _archive(tmp_path: Path, turns: list[dict]) -> Path:
    """Archive ``turns`` — each a ``tool_events``/``response`` pair — for real."""
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "memory-vault")
    for index, turn in enumerate(turns, start=1):
        store.record_turn(
            AgentRequest(
                prompt=f"question {index}",
                model="sonnet",
                mode="bypass",
                resume_session=None,
                images=[],
            ),
            ctx=CTX,
            response_text=turn.get("response", ""),
            effective_model="sonnet",
            session_id=f"ses_{index}",
            usage={},
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


def test_archive_retains_the_skill_and_the_turn_that_used_it(tmp_path: Path) -> None:
    """Both provider shapes, and the turn anchor a proposal quotes.

    Claude loads a skill with a ``Skill`` tool call whose summary is the name;
    opencode loads one natively, so it arrives as a read of the skill's own
    source. Neither is prose, so neither reached the archive before.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "tool_events": [
                    {"id": "1", "name": "Skill", "input": {"summary": "notes"}},
                ]
            },
            {
                "tool_events": [
                    {
                        "id": "2",
                        "name": "read",
                        "input": {"summary": "/w/skills/defuddle/SKILL.md"},
                    },
                ]
            },
            {
                "tool_events": [
                    {
                        "id": "3",
                        "name": "skill",
                        "input": {"summary": '{"name": "web-research"}'},
                    },
                ]
            },
        ],
    )

    body = archived.read_text(encoding="utf-8")
    assert "- Skills: notes" in body
    assert "- Skills: defuddle" in body
    assert "- Skills: web-research" in body
    # The anchor is the archive's own turn numbering, which is what a proposal
    # cites as evidence.
    assert read_archive_skills(archived) == {
        "notes": (1,),
        "defuddle": (2,),
        "web-research": (3,),
    }


def test_archive_retains_a_skill_loaded_without_a_tool_call(tmp_path: Path) -> None:
    """A slash command or a description match is a marker, not a tool call."""
    archived = _archive(
        tmp_path,
        [{"response": "<command-name>web-research</command-name> done"}],
    )

    assert read_archive_skills(archived) == {"web-research": (1,)}


def test_a_turn_that_used_no_skill_renders_unchanged(tmp_path: Path) -> None:
    """No evidence line, no invented name, and the metadata block as it was."""
    archived = _archive(
        tmp_path,
        [{"tool_events": [{"id": "1", "name": "Bash", "input": {"summary": "ls"}}]}],
    )

    body = archived.read_text(encoding="utf-8")
    assert "Skills:" not in body
    assert read_archive_skills(archived) == {}


def test_reading_a_missing_archive_yields_no_evidence(tmp_path: Path) -> None:
    """Evidence is a source for a proposal, not a reason to fail one."""
    assert read_archive_skills(tmp_path / "gone.md") == {}


