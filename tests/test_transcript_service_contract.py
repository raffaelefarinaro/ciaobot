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

from ciao.web import app as web_app
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
        user_turn_timings={
            "0": {
                "sent_at": "2026-08-31T11:11:39Z",
                "completed_at": "2026-08-31T11:13:15Z",
                "duration_ms": 96000,
            }
        },
        user_turn_unattended={"0": False},
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
    assert transcript_service._opencode_child_status(messages) == "failed"
    assert transcript_service._opencode_child_status(messages[:2]) == "completed"
    assert (
        transcript_service._opencode_child_status(
            [{"info": {"role": "assistant", "time": {"created": 1}}}]
        )
        == "running"
    )
    assert transcript_service._opencode_child_status([]) == "completed"


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
