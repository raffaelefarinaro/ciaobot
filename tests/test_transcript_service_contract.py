"""The wire contract of the chat transcript read path.

``ciao/web/transcript_service.py`` was cut out of ``routes_api.py`` without a
behaviour change. These tests pin the three things that had to stay true, so
the next extraction in the same region has a gate to fail against:

* the HTTP surface is untouched — the transcript routes still resolve to the
  same handlers in ``routes_api``;
* the service holds no transport — no ``Request``, no Starlette;
* and the rows it produces are byte-for-byte the rows it produced before,
  captured here as golden payloads for the three renderers and for the full
  assemble-then-prune path the ``/api/chats/{id}/messages`` endpoint runs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli_envelopes
from ciao.models import AgentRequest, ChatContext
from ciao.transcripts import TranscriptStore
from ciao.web import app as web_app
from ciao.providers.opencode import opencode_child_status
from ciao.web import transcript_service

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------- route table
# (path, methods, handler) for every route that serves a rendered transcript.
# The handlers stay in routes_api: only the rendering moved.
TRANSCRIPT_ROUTES = [
    ("/api/chats/{chat_id}/messages", "GET", "chat_messages"),
    ("/api/chats/{chat_id}/messages/part", "GET", "chat_message_part"),
    ("/api/chats/{chat_id}/subagents", "GET", "chat_subagents"),
    ("/api/subagents/running", "GET", "running_subagents"),
]


def _declared_routes() -> list[tuple[str, str, str]]:
    """(path, methods, endpoint name) parsed out of ``create_app``'s table."""
    tree = ast.parse((REPO_ROOT / "ciao" / "web" / "app.py").read_text(encoding="utf-8"))
    rows: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "Route" or len(node.args) < 2:
            continue
        path, endpoint = node.args[0], node.args[1]
        if not isinstance(path, ast.Constant) or not isinstance(endpoint, ast.Name):
            continue
        methods = ""
        for kw in node.keywords:
            if kw.arg == "methods" and isinstance(kw.value, ast.List):
                methods = ",".join(
                    e.value for e in kw.value.elts if isinstance(e, ast.Constant)
                )
        rows.append((path.value, methods, endpoint.id))
    return rows


def test_transcript_routes_still_resolve_to_routes_api() -> None:
    declared = _declared_routes()
    for row in TRANSCRIPT_ROUTES:
        assert row in declared, f"{row[0]} {row[1]} no longer served by {row[2]}"
        handler = getattr(web_app, row[2])
        assert handler.__module__ == "ciao.web.routes_api"


# ------------------------------------------------------------ ownership rules
def test_service_carries_no_transport() -> None:
    """No Request, no Starlette: the service is callable without a request."""
    source = (REPO_ROOT / "ciao" / "web" / "transcript_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("starlette"), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("starlette"), node.module
            assert "Request" not in {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            assert node.id != "Request"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            named = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            assert "request" not in {a.arg for a in named}, node.name


def _module_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_nothing_was_left_behind_or_duplicated() -> None:
    """Every moved name lives in exactly one module.

    A copy left in ``routes_api`` would keep the tests green while the two
    renderers drifted apart — which is how the proposal payload diverged
    before #481.
    """
    service = _module_level_names(REPO_ROOT / "ciao" / "web" / "transcript_service.py")
    routes = _module_level_names(REPO_ROOT / "ciao" / "web" / "routes_api.py")
    service.discard("logger")
    assert service & routes == set()
    # Floor on what #498 actually moved out of routes_api. It dropped from 40
    # when #500 lifted the six CLI-envelope constants one level up into
    # ciao/cli_envelopes.py, which subagent_tracking shares.
    assert len(service) >= 34


# ------------------------------------------------------------------- fixtures
SDK_USER = SimpleNamespace(type="user", message={"content": "hello there"})
SDK_ASSISTANT = SimpleNamespace(
    type="assistant",
    message={
        "content": [
            {"type": "thinking", "thinking": "T" * 1400},
            {"type": "text", "text": "The answer"},
            {
                "type": "tool_use",
                "id": "tu1",
                "name": "Write",
                "input": {"file_path": "/tmp/a.md", "content": "x"},
            },
            {
                "type": "tool_use",
                "id": "tu2",
                "name": "Bash",
                "input": {"command": "echo hi"},
            },
            {
                "type": "tool_use",
                "id": "tu3",
                "name": "Read",
                "input": {"file_path": "/tmp/b.md"},
            },
        ]
    },
)
# tu1 was denied and tu2 returned a failed MCP envelope; tu3 succeeded.
SDK_RESULT = SimpleNamespace(
    type="user",
    message={
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu1",
                "is_error": True,
                "content": "denied",
            },
            {
                "type": "tool_result",
                "tool_use_id": "tu2",
                "content": '{"ok": false, "error": "x"}',
            },
            {"type": "tool_result", "tool_use_id": "tu3", "content": "fine"},
        ]
    },
)

TRANSCRIPT_ROWS = [
    {
        "role": "user",
        "content": "hello there",
        "turn_index": 0,
        "sent_at": "2026-08-31T11:11:39Z",
    },
    {
        "role": "assistant",
        "content": "The answer",
        "sent_at": "2026-08-31T11:13:15Z",
        "effective_model": "claude-opus-5",
        "usage": {
            "input_tokens": "14",
            "output_tokens": "5842",
            "context_pct": "95.7%",
        },
        "quota": {"used": "1"},
    },
]

ACTIVITY_LINE = "\U0001F4DD Write /tmp/a.md\n$ Bash echo hi\n\U0001F4D6 Read /tmp/b.md"


def _chat(**overrides) -> SimpleNamespace:
    base = dict(
        chat_id="chat-1",
        session_id="sess-1",
        previous_session_ids=[],
        provider="claude",
        archived=False,
        archive_path="",
        handover_messages=[],
        user_turn_images={},
        turn_index_base=0,
        user_turn_timings={
            "0": {
                "sent_at": "2026-08-31T11:11:39Z",
                "completed_at": "2026-08-31T11:13:15Z",
                "duration_ms": 96000,
            }
        },
        user_turn_unattended={"0": False},
        user_turn_steered={},
        user_turn_origin={},
        user_turn_send_ids={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _pcm(transcript_rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        _transcripts=SimpleNamespace(current_messages=lambda _ctx, _p: transcript_rows),
        _agent_root_for_chat=lambda _cid: "/tmp",
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(workspace_root=Path("/tmp"))


# ------------------------------------------------------------ golden payloads
@pytest.mark.asyncio
async def test_assembled_and_pruned_rows_match_the_wire_contract(monkeypatch) -> None:
    """The exact payload ``GET /api/chats/{id}/messages`` puts on the wire.

    Covers, in one pass: thinking blocks rendered as their own ``_thinking``
    row and truncated with a lazy marker; a denied ``Write`` and a failed MCP
    ``Bash`` losing their file cards while ``Read`` keeps its activity line;
    the durable transcript's usage/quota/model overlaid on the turn's last
    assistant row; and the per-turn timings.
    """
    monkeypatch.setattr(
        transcript_service,
        "_read_session_segment",
        lambda _sid, _dirs: [SDK_USER, SDK_ASSISTANT, SDK_RESULT],
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(TRANSCRIPT_ROWS), _config(), _chat()
    )
    wire = transcript_service._prune_rows_for_wire(rows)

    keep = transcript_service._THINKING_KEEP_CHARS
    thinking = "T" * 1400
    assert wire == [
        {
            "role": "user",
            "content": "hello there",
            "turn_index": 0,
            "sent_at": "2026-08-31T11:11:39Z",
            "i": 0,
        },
        {
            "role": "system",
            "tool_name": "_thinking",
            "content": (
                thinking[:keep]
                + f"\n… ({1400 - 2 * keep} chars hidden, expand to load)\n"
                + thinking[-keep:]
            ),
            "lazy": True,
            "full_length": 1400,
            "i": 1,
        },
        {
            "role": "assistant",
            "content": "The answer",
            "sent_at": "2026-08-31T11:13:15Z",
            "duration_ms": 96000,
            "effective_model": "claude-opus-5",
            "usage": {
                "input_tokens": "14",
                "output_tokens": "5842",
                "context_pct": "95.7%",
            },
            "quota": {"used": "1"},
            "i": 2,
        },
        {
            "role": "system",
            "content": ACTIVITY_LINE,
            "tool_name": "_activity",
            "i": 3,
        },
    ]


@pytest.mark.asyncio
async def test_handover_messages_lead_the_assembled_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        transcript_service,
        "_read_session_segment",
        lambda _sid, _dirs: [SDK_USER, SDK_ASSISTANT, SDK_RESULT],
    )
    handover = {"role": "system", "content": "handed over"}

    rows = await transcript_service._assemble_chat_messages(
        _pcm(TRANSCRIPT_ROWS), _config(), _chat(handover_messages=[handover])
    )

    assert rows[0] == handover


@pytest.mark.asyncio
async def test_a_sessionless_chat_serves_the_durable_transcript(monkeypatch) -> None:
    """A provider that died before creating its session still renders."""
    rows = await transcript_service._assemble_chat_messages(
        _pcm(TRANSCRIPT_ROWS), _config(), _chat(session_id="")
    )

    assert rows == TRANSCRIPT_ROWS


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_its_row_and_does_not_shift_the_next_turn(
    monkeypatch,
) -> None:
    """#1861: a send that died before the provider recorded it still renders.

    The provider session holds only the turn that reached the model, so
    numbering its user rows by session position stamped the surviving question
    with the failed send's index and ``sent_at``. The failed turn is in the
    durable transcript with ``is_error`` on its assistant row; it must own its
    user row, its error row, and the numbering.
    """
    durable = [
        {
            "role": "user",
            "content": "and you also didn't find one?",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "Why is stopped?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:52:58Z",
        },
        {
            "role": "assistant",
            "content": "Nothing is stuck.",
            "sent_at": "2026-10-22T11:52:58Z",
        },
    ]
    session = [
        SimpleNamespace(type="user", message={"content": "Why is stopped?"}),
        SimpleNamespace(
            type="assistant",
            message={"content": [{"type": "text", "text": "Nothing is stuck."}]},
        ),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T11:26:33Z"},
            "1": {"sent_at": "2026-10-22T11:52:58Z"},
        },
        user_turn_origin={"1": "retry"},
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert rows == [
        {
            "role": "user",
            "content": "and you also didn't find one?",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "Why is stopped?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:52:58Z",
            "origin": "retry",
        },
        {"role": "assistant", "content": "Nothing is stuck."},
    ]
    assert [
        row
        for row in rows
        if row.get("turn_index") == 0 and row.get("content") == "Why is stopped?"
    ] == []


@pytest.mark.asyncio
async def test_a_non_error_turn_missing_from_the_session_adds_no_row(
    monkeypatch,
) -> None:
    """Compaction / session reset: only an *error* durable turn may be rendered.

    A durable turn the session legitimately does not contain (a compaction
    summary, a rotation) must keep today's numbering and add no row, so no
    transcript is ever duplicated.
    """
    durable = [
        {
            "role": "user",
            "content": "older turn",
            "turn_index": 0,
            "sent_at": "2026-10-22T10:00:00Z",
        },
        {
            "role": "user",
            "content": "newer turn",
            "turn_index": 1,
            "sent_at": "2026-10-22T10:05:00Z",
        },
    ]
    session = [SimpleNamespace(type="user", message={"content": "newer turn"})]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(user_turn_timings={}, user_turn_unattended={})

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    users = [row for row in rows if row.get("role") == "user"]
    assert len(users) == 1
    assert users[0]["content"] == "newer turn"
    assert users[0]["turn_index"] == 1
    assert all(row.get("content") != "older turn" for row in rows)


@pytest.mark.asyncio
async def test_a_handover_turn_is_numbered_from_the_current_lineage(
    monkeypatch,
) -> None:
    """A provider handover leaves the previous provider's durable file on disk.

    ``turn_index_base`` counts every send so far, while the durable file's own
    indices restart at the provider stint, so numbering from ``base + file
    index`` overshoots. The rows are numbered from the end of the file and only
    the current lineage (``abs >= base``) participates.
    """
    durable = [
        {
            "role": "user",
            "content": "first",
            "turn_index": 0,
            "sent_at": "2026-10-22T09:00:00Z",
        },
        {"role": "assistant", "content": "a1", "sent_at": "2026-10-22T09:00:00Z"},
        {
            "role": "user",
            "content": "second",
            "turn_index": 1,
            "sent_at": "2026-10-22T10:00:00Z",
        },
        {"role": "assistant", "content": "a2", "sent_at": "2026-10-22T10:00:00Z"},
        {
            "role": "user",
            "content": "third",
            "turn_index": 2,
            "sent_at": "2026-10-22T12:00:00Z",
        },
        {"role": "assistant", "content": "a3", "sent_at": "2026-10-22T12:00:00Z"},
    ]
    session = [SimpleNamespace(type="user", message={"content": "third"})]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        turn_index_base=4,
        user_turn_count=5,
        user_turn_timings={"4": {"sent_at": "2026-10-22T12:00:00Z"}},
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert rows == [
        {
            "role": "user",
            "content": "third",
            "turn_index": 4,
            "sent_at": "2026-10-22T12:00:00Z",
        }
    ]
    assert all(row.get("content") not in {"first", "second"} for row in rows)


@pytest.mark.asyncio
async def test_a_retry_of_the_same_text_keeps_the_failed_turn(monkeypatch) -> None:
    """``retryFromError`` re-sends the same text after the failure.

    Durable turns are ``[failed "why?", retry "why?", answer]`` while the
    session holds only the retry. Matching the failed turn to the session row
    by text stamped the retry's row with the failure's index and hid the
    failure; a run of identical texts has to align from the right.
    """
    durable = [
        {
            "role": "user",
            "content": "why?",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "why?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:32:00Z",
        },
        {
            "role": "assistant",
            "content": "Nothing is stuck.",
            "sent_at": "2026-10-22T11:32:00Z",
        },
    ]
    session = [
        SimpleNamespace(type="user", message={"content": "why?"}),
        SimpleNamespace(
            type="assistant",
            message={"content": [{"type": "text", "text": "Nothing is stuck."}]},
        ),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T11:26:33Z"},
            "1": {"sent_at": "2026-10-22T11:32:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert rows == [
        {
            "role": "user",
            "content": "why?",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "why?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:32:00Z",
        },
        {"role": "assistant", "content": "Nothing is stuck."},
    ]


@pytest.mark.asyncio
async def test_a_restamped_row_does_not_keep_the_failed_sends_images(
    monkeypatch,
) -> None:
    """A wrong turn index also handed the row the failed send's image refs.

    The re-stamped row must re-derive its images from its own turn (or the
    session's inline blocks), and the failed row must own the attachments.
    """
    durable = [
        {
            "role": "user",
            "content": "look at this",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "why?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:32:00Z",
        },
        {
            "role": "assistant",
            "content": "Nothing is stuck.",
            "sent_at": "2026-10-22T11:32:00Z",
        },
    ]
    session = [SimpleNamespace(type="user", message={"content": "why?"})]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_images={"0": ["/tmp/a.png"]},
        user_turn_timings={"1": {"sent_at": "2026-10-22T11:32:00Z"}},
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert rows[0]["content"] == "look at this"
    assert rows[0]["images"] == ["/tmp/a.png"]
    restamped = next(row for row in rows if row.get("content") == "why?")
    assert "images" not in restamped


def _pcm_store(store: TranscriptStore) -> SimpleNamespace:
    """A manager double whose durable transcript is a real store."""
    return SimpleNamespace(
        _transcripts=store,
        _agent_root_for_chat=lambda _cid: "/tmp",
    )


def _record_turn(
    store: TranscriptStore,
    prompt: str,
    response: str,
    *,
    is_error: bool = False,
) -> None:
    store.record_turn(
        AgentRequest(
            prompt=prompt,
            model="sonnet",
            mode="bypass",
            resume_session=None,
            images=[],
        ),
        ctx=ChatContext.for_web("chat-1"),
        response_text=response,
        effective_model="sonnet",
        session_id="s",
        usage={},
        quota={},
        input_kind="text",
        is_error=is_error,
    )


def _sdk_user(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="user", message={"content": text})


def _sdk_assistant(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="assistant", message={"content": [{"type": "text", "text": text}]}
    )


@pytest.mark.asyncio
async def test_a_turn_in_flight_does_not_shift_the_settled_rows(
    monkeypatch,
) -> None:
    """`user_turn_count` is bumped at send time, `record_turn` writes later.

    While a turn runs, the durable file holds one turn fewer than the counter,
    so numbering from the counter shifted every settled row by one and made it
    collide with the in-flight row's own index.
    """
    durable = [
        {"role": "user", "content": "u0", "turn_index": 0, "sent_at": "t0"},
        {"role": "assistant", "content": "a0", "sent_at": "t0"},
        {"role": "user", "content": "u1", "turn_index": 1, "sent_at": "t1"},
        {"role": "assistant", "content": "a1", "sent_at": "t1"},
    ]
    session = [
        _sdk_user("u0"),
        _sdk_assistant("a0"),
        _sdk_user("u1"),
        _sdk_assistant("a1"),
        _sdk_user("u2"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(user_turn_count=3, user_turn_unattended={})

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert [row["turn_index"] for row in rows if row["role"] == "user"] == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_steered_turn_does_not_shift_the_settled_rows(
    monkeypatch,
) -> None:
    """A steer bumps `user_turn_count` but leaves no durable turn behind.

    The steer's own row is in the session, so the positional numbering already
    accounts for it; the settled rows must not be pulled forward by the extra
    counter value.
    """
    durable = [
        {"role": "user", "content": "t0", "turn_index": 0, "sent_at": "t0"},
        {"role": "assistant", "content": "a0", "sent_at": "t0"},
        {"role": "user", "content": "t1", "turn_index": 1, "sent_at": "t1"},
        {"role": "assistant", "content": "a1", "sent_at": "t1"},
    ]
    session = [
        _sdk_user("t0"),
        _sdk_assistant("a0"),
        _sdk_user("steer this"),
        _sdk_user("t1"),
        _sdk_assistant("a1"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=3, user_turn_steered={"1": True}, user_turn_unattended={}
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert [row["turn_index"] for row in rows if row["role"] == "user"] == [0, 1, 2]
    assert len(rows) == 5
    assert not any(row.get("is_error") for row in rows)


@pytest.mark.asyncio
async def test_a_failed_first_send_still_renders(monkeypatch) -> None:
    """The very first send can fail before the provider records anything."""
    durable = [
        {
            "role": "user",
            "content": "look at this",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "why?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:32:00Z",
        },
        {
            "role": "assistant",
            "content": "Nothing is stuck.",
            "sent_at": "2026-10-22T11:32:00Z",
        },
    ]
    session = [_sdk_user("why?"), _sdk_assistant("Nothing is stuck.")]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=2,
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T11:26:33Z"},
            "1": {"sent_at": "2026-10-22T11:32:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm(durable), _config(), chat
    )

    assert rows == [
        {
            "role": "user",
            "content": "look at this",
            "turn_index": 0,
            "sent_at": "2026-10-22T11:26:33Z",
        },
        {
            "role": "assistant",
            "content": "TimeoutError",
            "sent_at": "2026-10-22T11:26:33Z",
            "is_error": True,
        },
        {
            "role": "user",
            "content": "why?",
            "turn_index": 1,
            "sent_at": "2026-10-22T11:32:00Z",
        },
        {"role": "assistant", "content": "Nothing is stuck."},
    ]


@pytest.mark.asyncio
async def test_a_hidden_continuation_does_not_shift_the_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable file records our continuation prompts, the engine does not.

    `record_turn` writes the continuation prompt as a turn, but it never bumps
    `user_turn_count` and the renderer hides it, so it is not a chat turn: it
    must not consume an index for the rows after it.
    """
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts")
    for prompt in ("initial", cli_envelopes.CODE_MODE_CONTINUE_PROMPT, "second"):
        _record_turn(store, prompt, "answer")
    session = [
        _sdk_user("initial"),
        _sdk_assistant("a1"),
        _sdk_user(cli_envelopes.CODE_MODE_CONTINUE_PROMPT),
        _sdk_assistant("a2"),
        _sdk_user("second"),
        _sdk_assistant("a3"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=2,
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T10:00:00Z"},
            "1": {"sent_at": "2026-10-22T10:01:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm_store(store), _config(), chat
    )

    users = [
        (row["content"], row["turn_index"], row.get("sent_at"))
        for row in rows
        if row["role"] == "user"
    ]
    assert users == [
        ("initial", 0, "2026-10-22T10:00:00Z"),
        ("second", 1, "2026-10-22T10:01:00Z"),
    ]
    assert all(
        cli_envelopes.CODE_MODE_CONTINUE_PROMPT not in str(row.get("content") or "")
        for row in rows
    )


@pytest.mark.asyncio
async def test_an_attachment_prompt_does_not_shift_the_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable prompt keeps ``ciao-drop:`` tokens; the session expands them.

    The same send renders different text on the two sides, so a text mismatch
    there is not a turn the session is missing.
    """
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts")
    _record_turn(store, "Summarise these `ciao-drop:drop_abc`", "First answer")
    _record_turn(store, "next question", "Second answer")
    session = [
        _sdk_user("Summarise these /tmp/a.pdf"),
        _sdk_assistant("First answer"),
        _sdk_user("next question"),
        _sdk_assistant("Second answer"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=2,
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T10:00:00Z"},
            "1": {"sent_at": "2026-10-22T10:01:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm_store(store), _config(), chat
    )

    users = [
        (row["content"], row["turn_index"], row.get("sent_at"))
        for row in rows
        if row["role"] == "user"
    ]
    assert users == [
        ("Summarise these /tmp/a.pdf", 0, "2026-10-22T10:00:00Z"),
        ("next question", 1, "2026-10-22T10:01:00Z"),
    ]
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_a_rewritten_prompt_before_a_failed_send_stays_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine-rewritten prompt is still the send its session row holds.

    The durable prompt keeps its ``ciao-drop:`` refs while the session expands
    them, so the texts differ. With one send missing from the session (the
    failed one), that rewrite must consume the session row instead of being
    skipped: otherwise the failed turn anchors above it and both rows get the
    wrong index, time and attachments.
    """
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts")
    _record_turn(store, "Summarise these `ciao-drop:drop_abc`", "first answer")
    _record_turn(store, "lost question", "TimeoutError", is_error=True)
    session = [
        _sdk_user("Summarise these /tmp/a.pdf"),
        _sdk_assistant("first answer"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=2,
        user_turn_images={"0": ["/tmp/a.pdf"]},
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T10:00:00Z"},
            "1": {"sent_at": "2026-10-22T10:05:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm_store(store), _config(), chat
    )

    assert [
        (row["role"], row["content"], row.get("turn_index"), row.get("is_error"))
        for row in rows
    ] == [
        ("user", "Summarise these /tmp/a.pdf", 0, None),
        ("assistant", "first answer", None, None),
        ("user", "lost question", 1, None),
        ("assistant", "TimeoutError", None, True),
    ]
    assert rows[0]["sent_at"] == "2026-10-22T10:00:00Z"
    assert rows[0]["images"] == ["/tmp/a.pdf"]


@pytest.mark.asyncio
async def test_a_rewritten_prompt_then_failure_then_next_send_numbers_0_1_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rewritten prompt, the failed turn and the next send number 0, 1, 2."""
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts")
    _record_turn(store, "Summarise these `ciao-drop:drop_abc`", "first answer")
    _record_turn(store, "lost question", "TimeoutError", is_error=True)
    _record_turn(store, "next question", "next answer")
    session = [
        _sdk_user("Summarise these /tmp/a.pdf"),
        _sdk_assistant("first answer"),
        _sdk_user("next question"),
        _sdk_assistant("next answer"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=3,
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T10:00:00Z"},
            "1": {"sent_at": "2026-10-22T10:05:00Z"},
            "2": {"sent_at": "2026-10-22T10:10:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm_store(store), _config(), chat
    )

    assert [
        (row["content"], row["turn_index"])
        for row in rows
        if row["role"] == "user"
    ] == [
        ("Summarise these /tmp/a.pdf", 0),
        ("lost question", 1),
        ("next question", 2),
    ]


@pytest.mark.asyncio
async def test_a_recorded_turn_that_is_still_streaming_is_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three durable turns where the session never held the first one.

    The counter and the durable file both hold three turns, so the stint is
    complete: the walk must keep the failed ``comment`` row and its error row,
    and leave ``why?`` and ``third`` on their own indices.
    """
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "transcripts")
    _record_turn(store, "comment", "TimeoutError", is_error=True)
    _record_turn(store, "why?", "Nothing is stuck.")
    _record_turn(store, "third", "third answer")
    session = [
        _sdk_user("why?"),
        _sdk_assistant("Nothing is stuck."),
        _sdk_user("third"),
        _sdk_assistant("third answer"),
    ]
    monkeypatch.setattr(
        transcript_service, "_read_session_segment", lambda _sid, _dirs: session
    )
    chat = _chat(
        user_turn_count=3,
        user_turn_timings={
            "0": {"sent_at": "2026-10-22T11:26:33Z"},
            "1": {"sent_at": "2026-10-22T11:52:58Z"},
            "2": {"sent_at": "2026-10-22T12:10:00Z"},
        },
        user_turn_unattended={},
    )

    rows = await transcript_service._assemble_chat_messages(
        _pcm_store(store), _config(), chat
    )

    assert [
        (row["role"], row["content"], row.get("turn_index"), row.get("is_error"))
        for row in rows
    ] == [
        ("user", "comment", 0, None),
        ("assistant", "TimeoutError", None, True),
        ("user", "why?", 1, None),
        ("assistant", "Nothing is stuck.", None, None),
        ("user", "third", 2, None),
        ("assistant", "third answer", None, None),
    ]
    # The failed turn carries its own record time, not the next turn's send.
    assert rows[0]["sent_at"] == (
        store.current_messages(ChatContext.for_web("chat-1"))[0]["sent_at"]
    )
    assert rows[2]["sent_at"] == "2026-10-22T11:52:58Z"
    assert rows[4]["sent_at"] == "2026-10-22T12:10:00Z"


def test_subagent_rows_match_the_wire_contract() -> None:
    rendered = transcript_service._render_subagent_messages(
        [SDK_USER, SDK_ASSISTANT, SDK_RESULT]
    )

    assert rendered == [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "The answer"},
        {"role": "system", "content": ACTIVITY_LINE, "tool_name": "_activity"},
    ]


def test_subagent_rows_are_identical_for_sdk_objects_and_jsonl_dicts() -> None:
    """Both shapes reach this renderer; they must render the same."""
    objects = [SDK_USER, SDK_ASSISTANT, SDK_RESULT]
    dicts = [{"type": m.type, "message": m.message} for m in objects]

    assert transcript_service._render_subagent_messages(
        objects
    ) == transcript_service._render_subagent_messages(dicts)


OPENCODE_THREAD = {
    "info": {"id": "s1"},
    "messages": [
        {
            "info": {"role": "user", "time": {"created": 1000}},
            "parts": [{"type": "text", "text": "do it"}],
        },
        {
            "info": {"role": "assistant", "time": {"created": 1100, "completed": 1200}},
            "parts": [
                {"type": "text", "text": "working"},
                {
                    "type": "tool",
                    "tool": "read",
                    "state": {"status": "completed", "input": {"filePath": "/tmp/x"}},
                },
            ],
        },
        {
            "info": {"role": "assistant", "error": {"name": "boom"}, "time": {"created": 1300}},
            "parts": [],
        },
    ],
}


def test_opencode_thread_rows_match_the_wire_contract() -> None:
    rendered = transcript_service._render_opencode_thread(OPENCODE_THREAD, _chat())

    assert rendered == [
        {
            "role": "user",
            "content": "do it",
            "turn_index": 0,
            "sent_at": "2026-08-31T11:11:39Z",
        },
        {
            "role": "assistant",
            "content": "working",
            "sent_at": "2026-08-31T11:13:15Z",
            "duration_ms": 96000,
        },
        {
            "role": "system",
            "content": "⚙️ read /tmp/x",
            "tool_name": "_activity",
        },
    ]


def test_a_child_thread_keeps_its_own_turn_numbering() -> None:
    """A child session restarts at turn 0, so the parent's metadata is off."""
    rendered = transcript_service._render_opencode_thread(
        OPENCODE_THREAD, _chat(), metadata=False, start_user_idx=3
    )

    assert rendered[0]["turn_index"] == 3
    assert "sent_at" not in rendered[0]


def test_opencode_child_status_reads_the_last_assistant_message() -> None:
    messages = OPENCODE_THREAD["messages"]
    assert opencode_child_status(messages) == "failed"
    assert opencode_child_status(messages[:2]) == "completed"
    assert (
        opencode_child_status(
            [{"info": {"role": "assistant", "time": {"created": 1}}}]
        )
        == "running"
    )
    assert opencode_child_status([]) == "completed"
    # The active map describes the current execution, not Session.Info.outcome
    # from the previous completed turn.
    assert opencode_child_status(
        [], {"outcome": "failed"}, active=True
    ) == "running"
    assert opencode_child_status(
        [], {"outcome": "succeeded"}, active=False
    ) == "completed"


def test_prune_rows_for_wire_only_truncates_oversized_thinking() -> None:
    keep = transcript_service._THINKING_KEEP_CHARS
    long_thinking = "H" * 2100
    rows = transcript_service._prune_rows_for_wire(
        [
            {"role": "user", "content": "hi"},
            {"role": "system", "tool_name": "_thinking", "content": long_thinking},
            {"role": "system", "tool_name": "_thinking", "content": "short"},
            {"role": "system", "tool_name": "_activity", "content": "x" * 3000},
        ]
    )

    assert [row["i"] for row in rows] == [0, 1, 2, 3]
    assert rows[1]["lazy"] is True
    assert rows[1]["full_length"] == 2100
    assert rows[1]["content"] == (
        long_thinking[:keep]
        + f"\n… ({2100 - 2 * keep} chars hidden, expand to load)\n"
        + long_thinking[-keep:]
    )
    assert "lazy" not in rows[2]
    assert rows[3]["content"] == "x" * 3000
