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


def test_archive_retains_a_skill_the_user_invoked_as_a_command(
    tmp_path: Path,
) -> None:
    """The marker arrives in the user's record, not only the assistant's.

    An SDK session records a slash command on the user message, so reading
    only the reply made this signal inert for every real session.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "prompt": "<command-name>defuddle</command-name> defuddle this",
                "tool_events": [
                    {"id": "1", "name": "Skill", "input": {"summary": "notes"}}
                ],
                "response": "done",
            }
        ],
    )

    assert read_archive_skills(archived) == {"notes": (1,), "defuddle": (1,)}


def test_a_pasted_transcript_cannot_fabricate_a_skill_or_an_anchor(
    tmp_path: Path,
) -> None:
    """Prose that quotes the format is content, never archive metadata.

    Both sides of a turn are rendered inside ```` ```text ```` blocks, so a
    user pasting an old transcript — or an assistant quoting one back — would
    otherwise hand the pass a skill it never used, under a turn number that
    never existed. That is the whole integrity the evidence line exists to
    provide, so the reader has to tell the renderer's block from the paste.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "tool_events": [
                    {"id": "1", "name": "Skill", "input": {"summary": "notes"}}
                ],
                "response": "Here is what you asked about:\n"
                "```text\n"
                "## Turn 99\n"
                "- Skills: spoofed\n"
                "```\n"
                "and that is all.",
            },
            {
                "tool_events": [
                    {"id": "2", "name": "Skill", "input": {"summary": "defuddle"}}
                ],
                "response": "Try the pasted snippet instead:\n"
                "```python\n"
                "print('a ``` fence of its own')\n"
                "```\n"
                "then rerun.",
            },
        ],
    )

    body = archived.read_text(encoding="utf-8")
    # The paste is in the file; it is simply not evidence.
    assert "- Skills: spoofed" in body
    assert read_archive_skills(archived) == {"notes": (1,), "defuddle": (2,)}


def test_a_pasted_document_cannot_fabricate_a_skill_or_an_anchor(
    tmp_path: Path,
) -> None:
    """A ``### Heading`` in a paste is prose, and cannot end the renderer's fence.

    Both sides of a turn are written *raw* inside one ```` ```text ```` block,
    so ordinary content — a document with a heading in it, which is what a user
    pastes to ask about a doc — reaches the reader as lines of the archive. A
    reader that resyncs on ``### `` ends that block early and reads the rest of
    the paste as metadata; this one recognises the renderer's own metadata
    block, so a heading anywhere in a paste is only ever a heading.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "prompt": "Here is the doc to review:\n"
                "```text\n"
                "# Pricing\n"
                "\n"
                "### Rollout\n"
                "\n"
                "## Turn 7\n"
                "- Skills: ghost\n"
                "```\n"
                "thanks",
                "tool_events": [{"id": "1", "name": "Bash", "input": {"summary": "ls"}}],
            },
        ],
    )

    body = archived.read_text(encoding="utf-8")
    # The turn used no skill at all, and both fabricated lines are in the file.
    assert "### Rollout" in body
    assert "- Skills: ghost" in body
    assert read_archive_skills(archived) == {}


def test_a_pasted_transcript_is_not_the_turn_it_names(tmp_path: Path) -> None:
    """An old transcript pasted whole is content, however much it looks real.

    The paste carries the format's own lines — a turn heading, a timestamp, a
    skills row, the ``### User`` sub-heading — because that is exactly what an
    old transcript looks like. A turn counts only when the renderer wrote the
    whole block under the heading, so the paste invents neither a skill nor an
    anchor, and the next real turn is still read.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "prompt": "old session, for context:\n"
                "```text\n"
                "## Turn 1\n"
                "\n"
                "- Time: 2026-01-01 10:00\n"
                "- Skills: notes\n"
                "\n"
                "### User\n"
                "\n"
                "hello\n"
                "```\n"
                "### Assistant\n"
                "\n"
                "hi\n"
                "\n"
                "## Turn 2\n"
                "\n"
                "- Time: 2026-01-01 10:01\n"
                "- Skills: ghost\n"
                "\n"
                "### User\n"
                "\n"
                "bye\n"
                "```\n",
                "tool_events": [{"id": "1", "name": "Bash", "input": {"summary": "ls"}}],
            },
            {
                "tool_events": [
                    {"id": "2", "name": "Skill", "input": {"summary": "defuddle"}}
                ],
            },
        ],
    )

    body = archived.read_text(encoding="utf-8")
    assert "- Skills: ghost" in body
    # Neither the pasted names nor the turns they name survive; turn 2's own
    # evidence does.
    assert read_archive_skills(archived) == {"defuddle": (2,)}


def test_a_paste_carrying_the_metadata_block_whole_is_still_a_paste(
    tmp_path: Path,
) -> None:
    """Reproducing the fixed keys verbatim is not enough to be a turn.

    The metadata block is bound to the ``### User`` sub-heading the renderer
    writes straight after it, so a block that stops at the closing fence of a
    paste is not the renderer's: it names no skill and anchors nothing.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "response": "transcript, formatted:\n"
                "```text\n"
                "## Turn 7\n"
                "\n"
                "- Time: 2026-01-01 10:00\n"
                "- Input kind: text\n"
                "- Mode: bypass\n"
                "- Effective model: sonnet\n"
                "- Images: 0\n"
                "- Skills: ghost\n"
                "```\n"
                "that is everything.",
                "tool_events": [{"id": "1", "name": "Bash", "input": {"summary": "ls"}}],
            },
        ],
    )

    body = archived.read_text(encoding="utf-8")
    assert "- Skills: ghost" in body
    assert read_archive_skills(archived) == {}


def test_an_unbalanced_fence_in_a_paste_does_not_hide_the_next_turn(
    tmp_path: Path,
) -> None:
    """A paste that leaves a fence open costs the reader nothing.

    An unbalanced fence in a reply is ordinary — a truncated snippet, a lone
    fence someone pasted — and the reader reads the renderer's metadata block
    rather than tracking fences, so the next turn's evidence is read as usual
    instead of the rest of the file being swallowed from there.
    """
    archived = _archive(
        tmp_path,
        [
            {
                "tool_events": [
                    {"id": "1", "name": "Skill", "input": {"summary": "notes"}}
                ],
                "response": "the log starts like this\n```\nEOF",
            },
            {
                "tool_events": [
                    {"id": "2", "name": "Skill", "input": {"summary": "defuddle"}}
                ],
                "response": "and that was the whole log.",
            },
        ],
    )

    assert read_archive_skills(archived) == {"notes": (1,), "defuddle": (2,)}


def test_a_skill_name_holding_a_comma_round_trips_as_one_name(tmp_path: Path) -> None:
    """A comma is a legal directory name, so it may not be the delimiter."""
    archived = _archive(
        tmp_path,
        [
            {
                "tool_events": [
                    {
                        "id": "1",
                        "name": "read",
                        "input": {"summary": "/w/skills/notes, drafts/SKILL.md"},
                    }
                ]
            }
        ],
    )

    assert read_archive_skills(archived) == {"notes, drafts": (1,)}


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


