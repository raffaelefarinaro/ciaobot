"""The CLI-envelope vocabulary is defined once and both readers agree.

``ciao/subagent_tracking.py`` (turn counter) and
``ciao/web/transcript_service.py`` (transcript renderer) have to skip exactly
the same synthetic user records, or the ``turn_index`` each stamps drifts
apart. They used to hold near-copies of six constants kept in sync by a
comment, and two of them had already diverged (issue #500). These tests pin
the unified semantics and the one call site whose behaviour that changed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli_envelopes, subagent_tracking
from ciao.web import transcript_service

REPO_ROOT = Path(__file__).resolve().parents[1]

ONE = (
    "<task-notification>\n"
    "<task-id>aaa</task-id>\n"
    "<status>completed</status>\n"
    "<summary>First agent done</summary>\n"
    "</task-notification>"
)
TWO = (
    "<task-notification>"
    "<task-id>aaa</task-id><status>completed</status>"
    "<summary>First agent done</summary>"
    "</task-notification>\n"
    "<task-notification>"
    "<task-id>bbb</task-id><status>failed</status>"
    "<summary>Second agent died</summary>"
    "</task-notification>"
)


# --------------------------------------------------------------- one home
def _module_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_no_module_defines_its_own_copy_of_the_shared_constants() -> None:
    """The six constants exist once, in ``ciao/cli_envelopes.py``.

    A re-introduced copy is exactly how these drifted the first time: the
    suite stays green while the two readers slowly disagree about which
    records are the CLI talking to itself.
    """
    shared = {
        "CLI_ENVELOPE_TAGS",
        "CLI_ENVELOPE_RE",
        "TASK_NOTIFICATION_RE",
        "INNER_TAG_RE",
        "CONTROL_SLASH_PREFIXES",
        "NO_RESPONSE_SENTINEL",
        "INTERRUPTED_REQUEST_RE",
    }
    assert shared <= _module_level_names(REPO_ROOT / "ciao" / "cli_envelopes.py")

    for module in ("ciao/subagent_tracking.py", "ciao/web/transcript_service.py"):
        defined = _module_level_names(REPO_ROOT / module)
        collisions = {n for n in defined if n.lstrip("_") in shared}
        assert collisions == set(), f"{module} redefines {collisions}"


def test_both_readers_share_one_envelope_predicate() -> None:
    """Identity, not equality: the renderer's alias *is* the shared helper."""
    assert transcript_service._is_cli_internal_envelope is cli_envelopes.is_cli_envelope
    assert (
        transcript_service._is_control_slash_command
        is cli_envelopes.is_control_slash_command
    )
    assert (
        transcript_service._is_no_response_sentinel
        is cli_envelopes.is_no_response_sentinel
    )
    assert (
        transcript_service._is_interrupted_request_sentinel
        is cli_envelopes.is_interrupted_request_sentinel
    )


# ------------------------------------------------- anchoring and greediness
def test_a_notification_alone_in_a_record_is_recognised() -> None:
    assert cli_envelopes.task_notification_fields(ONE) == {
        "task-id": "aaa",
        "status": "completed",
        "summary": "First agent done",
    }


def test_a_notification_embedded_in_prose_is_recognised() -> None:
    """Unanchored: the parser finds a notification wherever the CLI put it.

    The CLI has been seen to append text after the closing tag; anchoring the
    match to the whole record made the notification invisible to the reader
    that used ``match``.
    """
    embedded = f"Here is what happened:\n{ONE}\nand that is all."
    assert cli_envelopes.task_notification_fields(embedded) == {
        "task-id": "aaa",
        "status": "completed",
        "summary": "First agent done",
    }


def test_two_notifications_in_one_record_yield_the_first() -> None:
    """Non-greedy: the body stops at the first closing tag.

    A greedy body ran from the first opening tag to the *last* closing tag,
    so ``INNER_TAG_RE`` saw both notifications' fields and the last one won —
    reporting the second agent's status under the first one's identity.
    """
    assert cli_envelopes.task_notification_fields(TWO) == {
        "task-id": "aaa",
        "status": "completed",
        "summary": "First agent done",
    }


def test_plain_prose_carries_no_notification() -> None:
    assert cli_envelopes.task_notification_fields("hello world") is None


# -------------------------------------------------- the changed call site
def test_summariser_reports_the_first_of_two_notifications() -> None:
    """Behaviour change: this used to report the *second* agent's status.

    ``_summarize_task_notification`` matched greedily, so a record holding
    two completions rendered "Subagent failed: Second agent died" under the
    first agent's envelope.
    """
    summary = transcript_service._summarize_task_notification(TWO)
    assert summary == "\U0001F916 Subagent completed: First agent done"


def test_summariser_survives_text_after_the_closing_tag() -> None:
    """Behaviour change: this record used to be hidden entirely.

    It opens with a task-notification, so the envelope filter caught it, but
    the whole-record anchoring meant the summariser declined it first and the
    completion never reached the transcript.
    """
    summary = transcript_service._summarize_task_notification(ONE + "\ntrailing")
    assert summary == "\U0001F916 Subagent completed: First agent done"


# ------------------------------------------------------ renderer integration
def _chat() -> SimpleNamespace:
    return SimpleNamespace(
        chat_id="chat-1",
        session_id="sess-1",
        previous_session_ids=[],
        provider="claude",
        archived=False,
        archive_path="",
        handover_messages=[],
        user_turn_images={},
        user_turn_timings={},
        user_turn_unattended={},
    )


def _pcm() -> SimpleNamespace:
    return SimpleNamespace(
        _transcripts=SimpleNamespace(current_messages=lambda _ctx, _p: []),
        _agent_root_for_chat=lambda _cid: "/tmp",
    )


async def _render(monkeypatch, contents: list[str]) -> list[dict]:
    monkeypatch.setattr(
        transcript_service,
        "_read_session_segment",
        lambda _sid, _dirs: [
            SimpleNamespace(type="user", message={"content": c}) for c in contents
        ],
    )
    return await transcript_service._assemble_chat_messages(
        _pcm(), SimpleNamespace(workspace_root=Path("/tmp")), _chat()
    )


@pytest.mark.asyncio
async def test_a_notification_record_renders_as_one_system_line(monkeypatch) -> None:
    rows = await _render(monkeypatch, [ONE + "\ntrailing"])
    assert [(r["role"], r["content"]) for r in rows] == [
        ("system", "\U0001F916 Subagent completed: First agent done")
    ]


@pytest.mark.asyncio
async def test_prose_quoting_a_notification_stays_the_users_bubble(
    monkeypatch,
) -> None:
    """The renderer guards the summariser behind the envelope check.

    The shared parser is unanchored, so on its own it would have collapsed a
    human message that quotes an envelope into a status line and thrown the
    prose away. Only a record that *opens* with an envelope tag is the CLI
    talking to itself.
    """
    prose = f"why did this fail?\n{ONE}"
    rows = await _render(monkeypatch, [prose])
    assert [(r["role"], r["content"]) for r in rows] == [("user", prose)]


@pytest.mark.asyncio
async def test_a_non_notification_envelope_is_still_hidden(monkeypatch) -> None:
    rows = await _render(monkeypatch, ["<bash-stdout>hello\n</bash-stdout>"])
    assert rows == []


# -------------------------------------------- the two readers stay aligned
@pytest.mark.parametrize(
    "content",
    [
        "/model opus",
        "/mode plan",
        "No response requested.",
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
        ONE,
        "<bash-stdout>hello</bash-stdout>",
        "  \n<teammate-message>hi</teammate-message>",
    ],
)
def test_records_hidden_by_the_renderer_never_advance_the_turn_counter(
    content: str,
) -> None:
    """Both readers must agree, record by record, or `turn_index` drifts."""
    assert not subagent_tracking._is_countable_user_turn(content)
    hidden = (
        transcript_service._is_control_slash_command(content)
        or transcript_service._is_no_response_sentinel(content)
        or transcript_service._is_interrupted_request_sentinel(content)
        or transcript_service._is_cli_internal_envelope(content)
    )
    assert hidden


@pytest.mark.parametrize(
    "content",
    ["hello there", "why did this fail?", "Is X < Y true?", "<p>not a CLI tag</p>"],
)
def test_real_user_prose_counts_for_both_readers(content: str) -> None:
    assert subagent_tracking._is_countable_user_turn(content)
    assert not transcript_service._is_cli_internal_envelope(content)
    assert not transcript_service._is_control_slash_command(content)
