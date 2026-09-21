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
import json
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


@pytest.mark.asyncio
async def test_shell_output_quoting_a_notification_stays_hidden(monkeypatch) -> None:
    """The summariser is keyed on the *opening* tag, not on a body search.

    The shared parser is unanchored, so a `<bash-stdout>` record from a
    command that printed a session JSONL carries a notification in its body.
    Summarising it would fabricate a "Subagent failed" status line out of
    shell output and throw the rest of the record away.
    """
    rows = await _render(monkeypatch, [f"<bash-stdout>\n{TWO}\n</bash-stdout>"])
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

# ------------------------------------- a record that only quotes the grammar
SHELL_OUTPUT = (
    "<bash-stdout>\n"
    "$ grep task-notification ~/.claude/projects/x/session.jsonl\n"
    f"{ONE}\n"
    "</bash-stdout>"
)


def _session(tmp_path, records: list[dict]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


_DISPATCH = {
    "type": "assistant",
    "message": {
        "content": [{
            "type": "tool_use",
            "id": "tu1",
            "name": "Agent",
            "input": {
                "description": "check the logs",
                "subagent_type": "Explore",
                "run_in_background": True,
            },
        }]
    },
}
_DISPATCH_RESULT = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1"}]},
    "toolUseResult": {"agentId": "aaa", "isAsync": True},
}


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def test_only_a_record_that_opens_with_the_tag_is_a_notification() -> None:
    """The anchored predicate both readers use to identify a completion.

    A completion is a record the CLI *wrote* as an envelope. Shell output
    that printed a session JSONL, and a human quoting a notification, carry
    the same grammar and are not completions.
    """
    assert cli_envelopes.envelope_notification_fields(ONE) is not None
    assert cli_envelopes.envelope_notification_fields(ONE + "\ntrailing") is not None
    assert cli_envelopes.envelope_notification_fields(SHELL_OUTPUT) is None
    assert cli_envelopes.envelope_notification_fields(f"why did this fail?\n{ONE}") is None


def test_shell_output_quoting_a_notification_never_completes_an_agent(
    tmp_path,
) -> None:
    """Regression: a `grep` of a session JSONL flipped a running agent.

    The tracker identified a completion by searching the record's body, so
    the agent's own transcript being printed into the chat reported it
    "failed" — and opened a synthesis-nudge window for a completion that
    never happened, which is the window steering into kills the run.
    """
    state = subagent_tracking.parse_session_subagents(
        _session(tmp_path, [_DISPATCH, _DISPATCH_RESULT, _user(SHELL_OUTPUT)])
    )
    assert state.subagents["aaa"].status == "running"
    assert state.notification_pending is False


def test_a_real_notification_still_completes_its_agent(tmp_path) -> None:
    """The guard must not cost the tracker the completions it exists for."""
    state = subagent_tracking.parse_session_subagents(
        _session(tmp_path, [
            _DISPATCH,
            _DISPATCH_RESULT,
            _user(
                "<task-notification><task-id>aaa</task-id>"
                "<status>completed</status><summary>done</summary>"
                "</task-notification>"
            ),
        ])
    )
    assert state.subagents["aaa"].status == "completed"


def test_prose_quoting_a_notification_advances_both_turn_counters(
    tmp_path,
) -> None:
    """Regression: the two readers disagreed and `turn_index` drifted.

    The renderer shows a quoting human message as their bubble (pinned
    above), but the tracker treated it as a notification and skipped it, so
    every later dispatch was stamped one turn too low and its subagent panel
    anchored to the wrong bubble.
    """
    records = [
        _user("first"),
        _user(f"why did this fail?\n{ONE}"),
        _user("third"),
        _DISPATCH,
        _DISPATCH_RESULT,
    ]
    state = subagent_tracking.parse_session_subagents(_session(tmp_path, records))
    assert state.subagents["aaa"].turn_index == 2

    # The renderer's side of the same claim: three user bubbles.
    assert subagent_tracking._is_countable_user_turn(f"why did this fail?\n{ONE}")
