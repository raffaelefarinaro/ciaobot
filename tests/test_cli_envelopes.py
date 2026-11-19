"""The CLI-envelope vocabulary is defined once and the renderer uses it.

The transcript renderer has to skip the synthetic user records the CLI
writes. Its constants used to be near-copies kept in sync by a comment, and
two of them had already diverged (issue #500). These tests pin the unified
semantics and the one call site whose behaviour that changed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli_envelopes
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

    for module in ("ciao/web/transcript_service.py",):
        defined = _module_level_names(REPO_ROOT / module)
        collisions = {n for n in defined if n.lstrip("_") in shared}
        assert collisions == set(), f"{module} redefines {collisions}"


def test_the_renderer_shares_the_envelope_predicates() -> None:
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


def test_compaction_prefix_without_metadata_is_ordinary_user_text() -> None:
    text = "This session is being continued from a previous conversation. I wrote this myself."
    record = {"type": "user", "message": {"role": "user", "content": text}}

    assert not cli_envelopes.is_compact_summary(record)


def test_compaction_summary_requires_authoritative_record_flag() -> None:
    record = {
        "type": "user",
        "message": {
            "role": "user",
            "content": "A recap whose wording does not match the old prefix.",
            "isCompactSummary": True,
        },
    }

    assert cli_envelopes.is_compact_summary(record)


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


def test_leading_notification_scan_stops_before_shell_output() -> None:
    quoted = (
        "<bash-stdout>cat session.jsonl\n"
        "<task-notification><task-id>bbb</task-id><status>failed</status>"
        "</task-notification></bash-stdout>"
    )
    content = ONE + "\n" + quoted
    assert cli_envelopes.envelope_notifications(content) == [
        (
            {
                "task-id": "aaa",
                "status": "completed",
                "summary": "First agent done",
            },
            ["aaa"],
        )
    ]


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
        turn_index_base=0,
        user_turn_timings={},
        user_turn_unattended={},
        user_turn_steered={},
        user_turn_origin={},
        user_turn_send_ids={},
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
async def test_context_prefixed_notification_is_hidden_and_not_counted(monkeypatch) -> None:
    context = "[CIAO_CONTEXT_BEGIN]\nworkspace=personal\n[CIAO_CONTEXT_END]\n\n"
    rows = await _render(monkeypatch, [context + ONE, "real turn"])
    assert [(r["role"], r["content"]) for r in rows] == [
        ("system", "\U0001F916 Subagent completed: First agent done"),
        ("user", "real turn"),
    ]
    assert rows[1]["turn_index"] == 0


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
def test_records_the_renderer_hides(
    content: str,
) -> None:
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
def test_real_user_prose_is_not_hidden_by_the_renderer(content: str) -> None:
    assert not transcript_service._is_cli_internal_envelope(content)
    assert not transcript_service._is_control_slash_command(content)

# ------------------------------------- a record that only quotes the grammar
SHELL_OUTPUT = (
    "<bash-stdout>\n"
    "$ grep task-notification ~/.claude/projects/x/session.jsonl\n"
    f"{ONE}\n"
    "</bash-stdout>"
)


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


def test_hidden_continuations_are_recognised_with_and_without_the_capsule():
    capsule = "[CIAO_CONTEXT_BEGIN]\nx\n[CIAO_CONTEXT_END]\n\n"
    for prompt in (
        cli_envelopes.LEGACY_CODE_MODE_CONTINUE_PROMPT,
        cli_envelopes.RELOAD_CONTINUE_PROMPT,
    ):
        assert cli_envelopes.is_hidden_continuation(prompt)
        assert cli_envelopes.is_hidden_continuation(capsule + prompt)
        assert not cli_envelopes.is_hidden_continuation("please " + prompt)
    assert not cli_envelopes.is_hidden_continuation("ordinary text")


def _native_fields(status: str, summary: str, task_id: str = "be0mbj0cr") -> dict[str, str]:
    return {"task-id": task_id, "status": status, "summary": summary}


def test_native_task_run_parses_bash_failed_completed_stopped_and_monitor() -> None:
    failed = cli_envelopes.native_task_run(
        _native_fields(
            "failed", 'Background command "Run probe A in background" failed with exit code 3'
        )
    )
    assert failed == cli_envelopes.NativeTaskRun(
        label="Run probe A in background",
        run_id="be0mbj0cr",
        status="failed",
        exit_code=3,
        error="",
    )

    completed = cli_envelopes.native_task_run(
        _native_fields(
            "completed", 'Background command "Run probe B in background" completed (exit code 0)'
        )
    )
    assert completed is not None
    assert (completed.status, completed.exit_code, completed.error) == ("ok", 0, "")

    quoted = cli_envelopes.native_task_run(
        _native_fields("completed", 'Background command "echo "hi" there" completed (exit code 0)')
    )
    assert quoted is not None
    assert quoted.label == 'echo "hi" there'

    stopped = cli_envelopes.native_task_run(
        _native_fields(
            "stopped",
            'Background command "sleep 999" was stopped after reaching its background time limit',
        )
    )
    assert stopped is not None
    assert (stopped.status, stopped.exit_code) == ("cancelled", None)
    assert stopped.error == "was stopped after reaching its background time limit"

    script_failed = cli_envelopes.native_task_run(
        _native_fields("failed", 'Monitor "watch logs" script failed (exit 2)')
    )
    assert script_failed is not None
    assert (script_failed.label, script_failed.status, script_failed.exit_code) == (
        "watch logs",
        "failed",
        2,
    )

    ended = cli_envelopes.native_task_run(
        _native_fields("completed", 'Monitor "watch logs" stream ended')
    )
    assert ended is not None
    assert (ended.status, ended.exit_code, ended.error) == ("ok", None, "")


def test_native_task_run_ignores_subagents_and_monitor_events() -> None:
    assert cli_envelopes.native_task_run(_native_fields("completed", 'Agent "X" finished')) is None
    assert (
        cli_envelopes.native_task_run(_native_fields("completed", 'Monitor event: "X"')) is None
    )


def test_subagent_task_run_parses_agent_completions() -> None:
    finished = cli_envelopes.subagent_task_run(
        {**_native_fields("completed", 'Agent "Fix "quoted" bug" finished', "a7b9e"),
         "result": "line one\n\n" + "\n".join(f"r{i}" for i in range(60))}
    )
    assert finished is not None
    assert (finished.label, finished.run_id, finished.status) == ('Fix "quoted" bug', "a7b9e", "ok")
    assert finished.exit_code is None
    assert len(finished.tail) == 50 and finished.tail[-1] == "r59"

    # Older CLIs carry the report after the summary's first line.
    legacy = cli_envelopes.subagent_task_run(
        _native_fields("failed", 'Agent "X" failed\nDone. Summary:\n- 32 names')
    )
    assert legacy is not None
    assert (legacy.status, legacy.tail) == ("failed", ("Done. Summary:", "- 32 names"))

    stopped = cli_envelopes.subagent_task_run(_native_fields("killed", 'Agent "X" was stopped'))
    assert stopped is not None and stopped.status == "cancelled"

    assert cli_envelopes.subagent_task_run(
        _native_fields("completed", 'Background command "x" completed (exit code 0)')
    ) is None
    assert cli_envelopes.subagent_task_run(_native_fields("failed", "Hit timeout")) is None


def test_native_task_run_keeps_an_unexplained_summary_on_one_line() -> None:
    run = cli_envelopes.native_task_run(
        _native_fields("killed", 'Background command "job" was stopped\n\u2014 fake (x, ok)')
    )
    assert run is not None
    assert "\n" not in run.error
