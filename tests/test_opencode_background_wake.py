"""OpenCode 2 runs follow-up executions of its own after background work (#1391).

`background_shell_wake.jsonl` and `background_subagent_wake.jsonl` are sanitized
2.0.27 captures: a background shell and a background subagent that finish after
the foreground turn ended. They pin which execution a foreground turn owns and
what `drain_events` streams afterwards. `foreground_shell_turn.jsonl` is a
sanitized 2.2.0 capture of a foreground shell call (#2272).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ciao.models import AgentRequest, PermissionRequestEvent, ResultEvent, ToolUseEvent
from ciao.providers import opencode as opencode_module
from ciao.providers.opencode import OpencodeProvider, _PendingRequest, _read_running_shells
from tests.test_opencode_provider import _FakeEventStream, _FakeServerClient

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
SHELL_SESSION = "ses_bgshell_parent"
SUB_SESSION = "ses_bgsub_parent"


def _fixture_lines(name: str) -> list[str]:
    return [
        line
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _split(lines: list[str], session: str) -> tuple[list[str], list[str]]:
    """Split after the first `session.execution.succeeded` of ``session`` (inclusive)."""
    for index, line in enumerate(lines):
        event = json.loads(line)
        if (
            event["type"] == "session.execution.succeeded"
            and event["data"].get("sessionID") == session
        ):
            return lines[: index + 1], lines[index + 1 :]
    raise AssertionError("fixture has no terminal event")


def _sse(lines: list[str]) -> list[str]:
    return [f"data: {line}\n\n" for line in lines]


class _HeldOpenStream(_FakeEventStream):
    """Yields its frames, then stays open like a live SSE connection."""

    def __init__(self, lines: list[str]) -> None:
        super().__init__(_sse(lines))
        self._never = asyncio.Event()

    async def aiter_bytes(self):
        async for chunk in super().aiter_bytes():
            yield chunk
        await self._never.wait()


class _Response:
    status_code = 200
    text = ""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _TurnClient(_FakeServerClient):
    """A turn client whose `/api/session/active` reports ``running``."""

    def __init__(
        self,
        lines: list[str],
        running: set[str] | None = None,
        shells: list[dict] | None = None,
        shell_error: Exception | None = None,
        shell_hangs: bool = False,
    ) -> None:
        super().__init__(_sse(lines))
        self._running = running or set()
        self._shells = shells or []
        self._shell_error = shell_error
        self._shell_hangs = shell_hangs

    async def get(self, path: str, *, params=None):
        if path == "/api/shell":
            if self._shell_hangs:
                await asyncio.Event().wait()
            if self._shell_error is not None:
                raise self._shell_error
            return _Response({"data": self._shells})
        if path == "/api/session/active":
            return _Response(
                {"data": {sid: {"type": "running"} for sid in self._running}}
            )
        return await super().get(path, params=params)


class _DrainClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def stream(self, _method: str, _path: str) -> _HeldOpenStream:
        return _HeldOpenStream(self._lines)


def _event(kind: str, **data: object) -> str:
    return json.dumps({"id": f"evt_{kind}", "type": kind, "data": data})


def _text(session: str, message: str, text: str) -> str:
    return _event(
        "session.text.ended",
        sessionID=session, assistantMessageID=message, ordinal=0, text=text,
    )


async def _run_turn(
    provider: OpencodeProvider,
    monkeypatch: pytest.MonkeyPatch,
    client: _TurnClient,
    session: str,
    token: str,
) -> list:
    monkeypatch.setattr(opencode_module.secrets, "token_hex", lambda _n: token)

    async def fake_server(_request):
        return client

    async def fake_session(_request):
        return session

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)
    request = AgentRequest(prompt="hi", model="", mode="default", provider="opencode")
    return [
        event
        async for event in provider.run_streaming(request, lambda _handle: None)
    ]


def _attach_live(provider: OpencodeProvider, client: object, session: str) -> None:
    """Make the provider look attached to a live server (the turn fakes skip that)."""
    provider._client = client  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    provider._session_id = session


def _texts(events: list) -> str:
    return "".join(e.text for e in events if e.type == "text")


def _drain_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str, rest: list[str]
) -> OpencodeProvider:
    provider = OpencodeProvider(tmp_path)
    provider._client = _DrainClient(rest)  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    provider._session_id = session
    monkeypatch.setattr(opencode_module, "_OPENCODE_DRAIN_POLL_S", 0.01)
    return provider


def _script(monkeypatch, provider, *, shells, active=((),)):
    """Scripted reads: each list is consumed in order, the last value repeats."""
    def stepper(values):
        remaining = list(values)

        async def _next(*_args):
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

        return _next

    shell_read = stepper(shells)
    active_read = stepper([set(ids) for ids in active])
    monkeypatch.setattr(opencode_module, "_read_running_shells", shell_read)
    monkeypatch.setattr(opencode_module, "_read_active_sessions", active_read)


async def _drain(provider: OpencodeProvider) -> list:
    async def collect() -> list:
        return [event async for event in provider.drain_events()]

    return await asyncio.wait_for(collect(), 2)


# ── foreground turns ─────────────────────────────────────────────────────


def test_foreground_shell_is_not_background_work(tmp_path):
    provider = OpencodeProvider(tmp_path)
    session = "ses_fgshell_parent"
    seen_progress = False
    for line in _fixture_lines("foreground_shell_turn.jsonl"):
        event = json.loads(line)
        provider._track_background_event(event, session)
        if event["type"] == "session.tool.progress":
            seen_progress = True
            assert provider.background_work_outstanding is False
            assert provider._background_shells == set()
    assert seen_progress
    assert provider._background_shells == set()
    assert provider._foreground_shells == set()


def test_foreground_shell_moved_to_background_counts(tmp_path):
    provider = OpencodeProvider(tmp_path)
    session = "ses_fgshell_parent"

    def track(line: str) -> None:
        provider._track_background_event(json.loads(line), session)

    track(_event(
        "shell.created",
        info={"id": "sh_slow", "command": "sleep 99", "metadata": {"sessionID": session}},
    ))
    track(_event(
        "session.tool.progress", sessionID=session, id="call_slow",
        metadata={"shellID": "sh_slow"},
    ))
    assert provider.background_work_outstanding is False
    track(_event(
        "session.tool.success", sessionID=session, id="call_slow",
        metadata={"status": "running", "shellID": "sh_slow"},
    ))
    assert provider.background_work_outstanding is True
    assert provider._background_shells == {"sh_slow"}
    track(_event("shell.exited", id="sh_slow", exit=0, status="exited"))
    assert provider.background_work_outstanding is False


def _track_all(provider: OpencodeProvider, *lines: str) -> None:
    for line in lines:
        provider._track_background_event(json.loads(line), "ses_fgshell_parent")


def _slow_created() -> str:
    return _event(
        "shell.created",
        info={
            "id": "sh_slow", "command": "sleep 99",
            "metadata": {"sessionID": "ses_fgshell_parent"},
        },
    )


def _slow_progress() -> str:
    return _event(
        "session.tool.progress", sessionID="ses_fgshell_parent", id="call_slow",
        metadata={"shellID": "sh_slow"},
    )


def _slow_backgrounded() -> str:
    return _event(
        "session.tool.success", sessionID="ses_fgshell_parent", id="call_slow",
        metadata={"status": "running", "shellID": "sh_slow"},
    )


def test_moved_to_background_survives_mid_call_reconcile(tmp_path):
    provider = OpencodeProvider(tmp_path)
    _track_all(provider, _slow_created(), _slow_progress())
    provider._replace_background_shells(set())
    _track_all(provider, _slow_backgrounded())
    assert provider._background_shells == {"sh_slow"}
    assert provider._foreground_shells == set()
    assert provider.background_work_outstanding is True


def test_moved_to_background_without_shell_created(tmp_path):
    provider = OpencodeProvider(tmp_path)
    _track_all(provider, _slow_progress(), _slow_backgrounded())
    assert provider._background_shells == {"sh_slow"}


def test_exited_shell_not_promoted_by_late_success(tmp_path):
    provider = OpencodeProvider(tmp_path)
    _track_all(
        provider,
        _slow_progress(),
        _event("shell.exited", id="sh_slow", exit=0, status="exited"),
        _slow_backgrounded(),
    )
    assert provider._background_shells == set()


@pytest.mark.asyncio
async def test_refresh_excludes_running_foreground_shell(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    _attach_live(provider, object(), SHELL_SESSION)
    provider._foreground_shells = {"sh_fg"}
    _script(monkeypatch, provider, shells=[{"sh_fg", "sh_bg"}])

    assert await provider.refresh_background_shells() == 1
    assert provider._background_shells == {"sh_bg"}


@pytest.mark.asyncio
async def test_background_shell_turn_ends_at_its_own_execution(tmp_path, monkeypatch):
    foreground, _ = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(
        provider, monkeypatch, _TurnClient(foreground), SHELL_SESSION, "bgshell_prompt"
    )

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert not result.is_error
    assert result.result == "STARTED"
    assert provider._background_shells == {"sh_bgshell"}
    assert provider.background_work_outstanding


@pytest.mark.asyncio
async def test_background_subagent_turn_tracks_the_child(tmp_path, monkeypatch):
    foreground, _ = _split(_fixture_lines("background_subagent_wake.jsonl"), SUB_SESSION)
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(
        provider, monkeypatch, _TurnClient(foreground), SUB_SESSION, "bgsub_prompt"
    )

    assert events[-1].result == "LAUNCHED"
    assert provider._background_children == {"ses_bgsub_child"}
    assert provider._child_sessions == {"ses_bgsub_child": SUB_SESSION}


@pytest.mark.asyncio
async def test_wake_running_when_the_user_prompts_is_skipped(tmp_path, monkeypatch):
    lines = [
        _text(SHELL_SESSION, "msg_wake", "BGDONE"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
        _event("session.execution.started", sessionID=SHELL_SESSION),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_ours"),
        _text(SHELL_SESSION, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
    ]
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch,
        _TurnClient(lines, running={SHELL_SESSION}), SHELL_SESSION, "ours",
    )

    assert events[-1].result == "ANSWER"
    assert "BGDONE" not in _texts(events)


@pytest.mark.asyncio
async def test_wake_delivered_before_our_prompt_is_not_attributed(tmp_path, monkeypatch):
    lines = [
        _event("session.inbox.enqueued", sessionID=SHELL_SESSION, inboxID="msg_ours",
               item={"type": "user", "payload": {"text": "hi"}, "delivery": "queue"}),
        _event("session.inbox.enqueued", sessionID=SHELL_SESSION, inboxID="msg_wake",
               item={"type": "synthetic", "payload": {"text": "<shell/>"}, "delivery": "steer"}),
        _event("session.execution.started", sessionID=SHELL_SESSION),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_wake"),
        _text(SHELL_SESSION, "msg_wake_a", "BGDONE"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
        _event("session.execution.started", sessionID=SHELL_SESSION),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_ours"),
        _text(SHELL_SESSION, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
    ]
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch, _TurnClient(lines), SHELL_SESSION, "ours"
    )

    assert events[-1].result == "ANSWER"
    assert "BGDONE" not in _texts(events)


@pytest.mark.asyncio
async def test_synthetic_steered_into_our_execution_stays_ours(tmp_path, monkeypatch):
    lines = [
        _event("session.execution.started", sessionID=SHELL_SESSION),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_ours"),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_wake"),
        _text(SHELL_SESSION, "msg_both", "BOTH"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
    ]
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch, _TurnClient(lines), SHELL_SESSION, "ours"
    )

    assert events[-1].result == "BOTH"


@pytest.mark.asyncio
async def test_permission_from_a_foreign_execution_still_surfaces(tmp_path, monkeypatch):
    lines = [
        _event(
            "permission.asked",
            id="per_wake", sessionID=SHELL_SESSION, action="shell",
            resources=["ls"], metadata={"command": "ls"},
            source={"type": "tool", "messageID": "msg_w", "id": "call_w"},
        ),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_ours"),
        _text(SHELL_SESSION, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
    ]
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch,
        _TurnClient(lines, running={SHELL_SESSION}), SHELL_SESSION, "ours",
    )

    assert any(isinstance(event, PermissionRequestEvent) for event in events)
    assert events[-1].result == "ANSWER"


@pytest.mark.asyncio
async def test_prompt_delivered_into_a_running_wake_execution(tmp_path, monkeypatch):
    """The real-turn case: our prompt joins the wake's execution, one terminal."""
    lines = [
        _text(SHELL_SESSION, "msg_wake", "BGDONE"),
        _event("session.inbox.delivered", sessionID=SHELL_SESSION, inboxID="msg_ours"),
        _text(SHELL_SESSION, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=SHELL_SESSION),
    ]
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch,
        _TurnClient(lines, running={SHELL_SESSION}), SHELL_SESSION, "ours",
    )

    assert events[-1].result == "ANSWER"
    assert "BGDONE" not in _texts(events)


@pytest.mark.asyncio
async def test_reconnect_after_a_foreign_start_does_not_skip_our_turn(tmp_path, monkeypatch):
    class _TwoAttempts(_TurnClient):
        def __init__(self) -> None:
            super().__init__([], running={SHELL_SESSION})
            self.streams = [
                [],
                [
                    _text(SHELL_SESSION, "msg_ours_a", "OURS"),
                    _event("session.execution.succeeded", sessionID=SHELL_SESSION),
                ],
            ]

        def stream(self, _method: str, _path: str) -> _FakeEventStream:
            return _FakeEventStream(_sse(self.streams.pop(0)))

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(opencode_module.asyncio, "sleep", no_sleep)
    events = await _run_turn(
        OpencodeProvider(tmp_path), monkeypatch, _TwoAttempts(), SHELL_SESSION, "ours"
    )

    assert not events[-1].is_error
    assert events[-1].result == "OURS"


# ── background state at turn end ─────────────────────────────────────────

_PLAIN_TURN = [
    _text(SHELL_SESSION, "msg_a", "OK"),
    _event("session.execution.succeeded", sessionID=SHELL_SESSION),
]


@pytest.mark.asyncio
async def test_turn_end_drops_a_stale_background_shell(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(_PLAIN_TURN, shells=[])
    _attach_live(provider, client, SHELL_SESSION)
    provider._background_shells = {"sh_stale"}

    await _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours")

    assert provider._background_shells == set()


@pytest.mark.asyncio
async def test_turn_end_adopts_a_running_background_shell(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(
        _PLAIN_TURN,
        shells=[{"id": "sh_bgshell", "status": "running",
                 "metadata": {"sessionID": SHELL_SESSION}}],
    )
    _attach_live(provider, client, SHELL_SESSION)

    await _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours")

    assert provider._background_shells == {"sh_bgshell"}


@pytest.mark.asyncio
async def test_turn_end_survives_a_failing_shell_read(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(_PLAIN_TURN, shell_error=httpx.ConnectError("down"))
    _attach_live(provider, client, SHELL_SESSION)
    provider._background_shells = {"sh_keep"}

    events = await _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours")

    assert events[-1].result == "OK"
    assert not events[-1].is_error
    assert provider._background_shells == {"sh_keep"}


@pytest.mark.asyncio
async def test_turn_end_reconcile_cannot_hold_back_the_result(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(_PLAIN_TURN, shell_hangs=True)
    _attach_live(provider, client, SHELL_SESSION)
    monkeypatch.setattr(opencode_module, "_OPENCODE_RECONCILE_TIMEOUT_S", 0.05)

    events = await asyncio.wait_for(
        _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours"), 2
    )

    assert events[-1].result == "OK"
    assert not events[-1].is_error


@pytest.mark.asyncio
async def test_turn_end_drops_a_finished_background_child(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(_PLAIN_TURN, shells=[])
    _attach_live(provider, client, SHELL_SESSION)
    provider._background_children = {"ses_gone"}

    await _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours")

    assert provider._background_children == set()


@pytest.mark.asyncio
async def test_turn_end_tracks_a_child_resumed_by_id(tmp_path, monkeypatch):
    """A `subagent` call with an existing sessionID emits no `session.created`."""
    provider = OpencodeProvider(tmp_path)
    client = _TurnClient(_PLAIN_TURN, running={"ses_resumed"}, shells=[])
    _attach_live(provider, client, SHELL_SESSION)

    await _run_turn(provider, monkeypatch, client, SHELL_SESSION, "ours")

    assert provider._background_children == {"ses_resumed"}
    assert provider.background_work_outstanding


@pytest.mark.asyncio
async def test_turn_end_keeps_children_when_the_active_read_fails(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    _attach_live(provider, SimpleNamespace(), SHELL_SESSION)
    provider._background_children = {"ses_running"}

    async def no_shells(_client, _session):
        return set()

    async def broken_active(_client):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(opencode_module, "_read_running_shells", no_shells)
    monkeypatch.setattr(opencode_module, "_read_active_sessions", broken_active)
    await provider._reconcile_background()

    assert provider._background_children == {"ses_running"}


# ── server restart warning ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("outstanding", [True, False])
async def test_restart_warns_about_lost_background_work(
    tmp_path, monkeypatch, caplog, outstanding
):
    provider = OpencodeProvider(tmp_path)
    _attach_live(provider, SimpleNamespace(aclose=_noop), "ses_x")
    provider._mcp_token = "old-token"
    if outstanding:
        provider._background_shells = {"sh"}
        provider._background_children = {"ses_child"}
    monkeypatch.setattr(opencode_module, "resolve_opencode_binary", lambda *_a, **_k: "")
    request = AgentRequest(prompt="hi", model="", mode="default", provider="opencode")

    with caplog.at_level(logging.WARNING, logger=opencode_module.logger.name):
        with pytest.raises(FileNotFoundError):
            await provider._ensure_server(request)

    warned = [r.getMessage() for r in caplog.records if "Restarting the OpenCode server" in r.getMessage()]
    if outstanding:
        assert len(warned) == 1
        assert "1 background shell(s) and 1 background subagent(s)" in warned[0]
    else:
        assert warned == []


# ── can_drain ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_can_drain_needs_a_live_server_and_background_work(tmp_path):
    provider = OpencodeProvider(tmp_path)
    provider._client = SimpleNamespace(aclose=_noop)  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)  # type: ignore[assignment]
    provider._session_id = "ses_x"
    assert provider.can_drain is False

    provider._background_shells = {"sh"}
    assert provider.can_drain is True

    await provider.disconnect()
    assert provider.can_drain is False


async def _noop() -> None:
    return None


# ── drain_events ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_streams_a_background_shell_wake(tmp_path, monkeypatch):
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])

    events = await _drain(provider)

    assert _texts(events) == "BGDONE"
    results = [event for event in events if isinstance(event, ResultEvent)]
    assert len(results) == 1
    assert results[0].result == "BGDONE"
    assert results[0].session_id == SHELL_SESSION
    assert results[0].is_error is False
    assert provider.background_work_outstanding is False


@pytest.mark.asyncio
async def test_wake_result_carries_the_shell_notice(tmp_path, monkeypatch):
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])

    results = [e for e in await _drain(provider) if isinstance(e, ResultEvent)]

    assert len(results) == 1
    assert results[0].origin == "task-notification"
    assert results[0].wake_notice.startswith("[Ciaobot] 1 background run finished.")
    assert "BGDONE" in results[0].wake_notice

    plain = _drain_provider(
        tmp_path, monkeypatch, SHELL_SESSION,
        [
            _event("session.execution.started", sessionID=SHELL_SESSION),
            _text(SHELL_SESSION, "msg_x", "NOSHELL"),
            _event("session.execution.succeeded", sessionID=SHELL_SESSION),
        ],
    )
    plain._background_shells = {"sh_other"}
    _script(monkeypatch, plain, shells=[{"sh_other"}, set()])

    results = [e for e in await _drain(plain) if isinstance(e, ResultEvent)]

    assert [r.result for r in results] == ["NOSHELL"]
    assert results[0].wake_notice == ""


@pytest.mark.asyncio
async def test_replayed_shell_enqueue_does_not_double_the_run(tmp_path, monkeypatch):
    _, rest = _split(_fixture_lines("background_shell_wake.jsonl"), SHELL_SESSION)
    enqueued = next(line for line in rest if "session.inbox.enqueued" in line)
    provider = _drain_provider(
        tmp_path, monkeypatch, SHELL_SESSION, [enqueued, *rest]
    )
    provider._background_shells = {"sh_bgshell"}
    _script(monkeypatch, provider, shells=[{"sh_bgshell"}, set()])

    results = [e for e in await _drain(provider) if isinstance(e, ResultEvent)]

    assert results[0].wake_notice.startswith("[Ciaobot] 1 background run finished.")


@pytest.mark.asyncio
async def test_drain_streams_a_background_subagent_wake(tmp_path, monkeypatch):
    _, rest = _split(_fixture_lines("background_subagent_wake.jsonl"), SUB_SESSION)
    provider = _drain_provider(tmp_path, monkeypatch, SUB_SESSION, rest)
    provider._background_children = {"ses_bgsub_child"}
    _script(
        monkeypatch, provider,
        shells=[set()],
        active=[{"ses_bgsub_child"}, ()],
    )

    events = await _drain(provider)

    assert _texts(events) == "SUBDONE"
    results = [event for event in events if isinstance(event, ResultEvent)]
    assert [r.result for r in results] == ["SUBDONE"]
    assert provider._background_children == set()


@pytest.mark.asyncio
async def test_drain_counts_children_from_the_active_map(tmp_path, monkeypatch):
    """A failed child listing reads as an empty tree; it must not end the drain.

    The idle reaper reads the set once the drain ends, so emptying it while the
    server still runs the child let the reaper stop that server mid-review.
    """
    provider = _drain_provider(
        tmp_path, monkeypatch, SUB_SESSION, [_event("server.connected")]
    )
    monkeypatch.setattr(opencode_module, "_OPENCODE_DRAIN_MAX_S", 0.2)
    _script(monkeypatch, provider, shells=[set()], active=[{"ses_bgsub_child"}])

    async def empty_tree():
        return []

    monkeypatch.setattr(provider, "read_live_collab_tree", empty_tree)
    started = asyncio.get_running_loop().time()

    assert await _drain(provider) == []
    assert asyncio.get_running_loop().time() - started >= 0.15
    assert provider._background_children == {"ses_bgsub_child"}
    assert provider.background_work_outstanding


@pytest.mark.asyncio
async def test_drain_returns_when_nothing_is_outstanding(tmp_path, monkeypatch):
    provider = _drain_provider(
        tmp_path, monkeypatch, SHELL_SESSION, [_event("server.connected")]
    )
    _script(monkeypatch, provider, shells=[set()])

    assert await _drain(provider) == []


@pytest.mark.asyncio
async def test_drain_stops_at_the_horizon(tmp_path, monkeypatch):
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, [])
    monkeypatch.setattr(opencode_module, "_OPENCODE_DRAIN_MAX_S", 0.05)
    _script(monkeypatch, provider, shells=[{"sh"}])

    assert await _drain(provider) == []


@pytest.mark.asyncio
async def test_drain_gives_up_on_a_lost_terminal_frame(tmp_path, monkeypatch):
    rest = [_event("session.execution.started", sessionID=SHELL_SESSION)]
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    _script(monkeypatch, provider, shells=[set()])

    assert await _drain(provider) == []


@pytest.mark.asyncio
async def test_drain_does_not_treat_a_failed_active_read_as_stale(tmp_path, monkeypatch):
    rest = [_event("session.execution.started", sessionID=SHELL_SESSION)]
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    _script(monkeypatch, provider, shells=[set()])

    async def broken_active(_client):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(opencode_module, "_read_active_sessions", broken_active)
    monkeypatch.setattr(opencode_module, "_OPENCODE_DRAIN_MAX_S", 0.3)
    started = asyncio.get_running_loop().time()

    assert await _drain(provider) == []
    # Ended by the horizon, not by stale ticks (those would end it in ~0.03s).
    assert asyncio.get_running_loop().time() - started >= 0.25


@pytest.mark.asyncio
async def test_drain_reports_a_failed_wake(tmp_path, monkeypatch):
    rest = [
        _event("session.execution.started", sessionID=SHELL_SESSION),
        _event(
            "session.execution.failed",
            sessionID=SHELL_SESSION, error={"type": "x", "message": "boom"},
        ),
    ]
    provider = _drain_provider(tmp_path, monkeypatch, SHELL_SESSION, rest)
    _script(monkeypatch, provider, shells=[set()])

    events = await _drain(provider)

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.is_error
    assert result.result == "boom"


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_running_shells_filters_by_session_and_status():
    rows = [
        {"id": "sh_other", "status": "running", "metadata": {"sessionID": "ses_other"}},
        {"id": "sh_done", "status": "exited", "metadata": {"sessionID": "ses_a"}},
        {"id": "sh_live", "status": "running", "metadata": {"sessionID": "ses_a"}},
    ]

    class _Client:
        async def get(self, path: str):
            assert path == "/api/shell"
            return _Response({"data": rows})

    assert await _read_running_shells(_Client(), "ses_a") == {"sh_live"}  # type: ignore[arg-type]


def test_shell_and_subagent_fixtures_carry_no_private_paths():
    for name in ("background_shell_wake.jsonl", "background_subagent_wake.jsonl"):
        raw = (FIXTURES / name).read_text(encoding="utf-8")
        for needle in ("raffaelefarinaro", "claude-501", "OPENCODE_SERVER_PASSWORD", "Bearer "):
            assert needle not in raw


# ── child-session requests (#1617) ───────────────────────────────────────

P = "ses_task_parent"
C = "ses_task_child"
GC = "ses_task_grandchild"


def _child_asked(rid: str, sid: str) -> str:
    return _event(
        "permission.asked", id=rid, sessionID=sid, action="shell",
        resources=["gh issue list"], metadata={"command": "gh issue list"},
        source={"type": "tool", "messageID": "msg_c", "id": "call_c"},
    )


def _child_form(sid: str) -> str:
    return _event(
        "form.created",
        form={
            "id": "frm_child", "sessionID": sid, "title": "Pick",
            "fields": [{"key": "value", "title": "Value", "type": "string"}],
        },
    )


def _permissions(events: list) -> list:
    return [e for e in events if isinstance(e, PermissionRequestEvent)]


@pytest.mark.asyncio
async def test_child_permission_surfaces_during_the_turn(tmp_path, monkeypatch):
    lines = [
        _event("session.inbox.delivered", sessionID=P, inboxID="msg_ours"),
        _event("session.created", sessionID=C, parentID=P),
        _event("session.execution.started", sessionID=C),
        _child_asked("per_child", C),
        _text(C, "msg_c2", "CHILDTEXT"),
        _event("session.execution.succeeded", sessionID=C),
        _text(P, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=P),
    ]
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(provider, monkeypatch, _TurnClient(lines), P, "ours")

    asked = _permissions(events)
    assert [(e.request_id, e.session_id) for e in asked] == [("per_child", C)]
    assert provider._permission_requests["per_child"].session_id == C
    assert "CHILDTEXT" not in _texts(events)
    assert events[-1].result == "ANSWER"


@pytest.mark.asyncio
async def test_child_form_surfaces_and_settles_during_the_turn(tmp_path, monkeypatch):
    lines = [
        _event("session.inbox.delivered", sessionID=P, inboxID="msg_ours"),
        _event("session.created", sessionID=C, parentID=P),
        _event("session.execution.started", sessionID=C),
        _child_form(C),
        _event("form.replied", sessionID=C, formID="frm_child"),
        _text(C, "msg_c2", "CHILDTEXT"),
        _event("session.execution.succeeded", sessionID=C),
        _text(P, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=P),
    ]
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(provider, monkeypatch, _TurnClient(lines), P, "ours")

    forms = [
        e for e in events
        if isinstance(e, ToolUseEvent) and e.tool_name == "AskUserQuestion"
    ]
    assert [(e.request_id, e.session_id) for e in forms] == [("frm_child", C)]
    assert "frm_child" not in provider._question_requests
    assert "CHILDTEXT" not in _texts(events)
    assert events[-1].result == "ANSWER"


@pytest.mark.asyncio
async def test_grandchild_permission_surfaces(tmp_path, monkeypatch):
    lines = [
        _event("session.inbox.delivered", sessionID=P, inboxID="msg_ours"),
        _event("session.created", sessionID=C, parentID=P),
        _event("session.created", sessionID=GC, parentID=C),
        _child_asked("per_gc", GC),
        _text(P, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=P),
    ]
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(provider, monkeypatch, _TurnClient(lines), P, "ours")

    assert [(e.request_id, e.session_id) for e in _permissions(events)] == [
        ("per_gc", GC)
    ]
    assert provider._child_sessions == {C: P, GC: P}


@pytest.mark.asyncio
async def test_request_from_an_unrelated_session_stays_filtered(tmp_path, monkeypatch):
    lines = [
        _event("session.inbox.delivered", sessionID=P, inboxID="msg_ours"),
        _child_asked("per_stranger", "ses_stranger"),
        _text(P, "msg_answer", "ANSWER"),
        _event("session.execution.succeeded", sessionID=P),
    ]
    provider = OpencodeProvider(tmp_path)

    events = await _run_turn(provider, monkeypatch, _TurnClient(lines), P, "ours")

    assert _permissions(events) == []
    assert "per_stranger" not in provider._permission_requests


@pytest.mark.asyncio
async def test_drain_surfaces_a_background_child_permission(tmp_path, monkeypatch):
    provider = _drain_provider(
        tmp_path, monkeypatch, SUB_SESSION, [_child_asked("per_bg", "ses_bgsub_child")]
    )
    provider._child_sessions = {"ses_bgsub_child": SUB_SESSION}
    _script(
        monkeypatch, provider,
        shells=[set()],
        active=[{"ses_bgsub_child"}, ()],
    )

    events = await _drain(provider)

    assert [e.session_id for e in _permissions(events)] == ["ses_bgsub_child"]


class _ReloadClient:
    def __init__(self, listings: dict[str, list]) -> None:
        self.listings = listings
        self.paths: list[str] = []

    async def get(self, path: str, *, params=None):
        self.paths.append(path)
        return _Response({"data": self.listings.get(path, [])})


@pytest.mark.asyncio
async def test_reload_polls_live_child_sessions(tmp_path):
    provider = OpencodeProvider(tmp_path)
    provider._child_sessions = {C: P}
    provider._background_children = {C}
    client = _ReloadClient({
        f"/api/session/{C}/permission": [{
            "id": "per_reload", "sessionID": C, "action": "shell",
            "resources": ["ls"], "metadata": {"command": "ls"},
        }],
    })

    events = await provider._reload_pending_requests(client, P)  # type: ignore[arg-type]

    assert [e.session_id for e in _permissions(events)] == [C]
    assert f"/api/session/{C}/permission" in client.paths
    assert f"/api/session/{C}/form" in client.paths


@pytest.mark.asyncio
async def test_reload_prunes_a_vanished_child_request(tmp_path):
    provider = OpencodeProvider(tmp_path)
    provider._child_sessions = {C: P}
    provider._permission_requests["per_gone"] = _PendingRequest("per_gone", session_id=C)
    client = _ReloadClient({})

    await provider._reload_pending_requests(client, P)  # type: ignore[arg-type]

    assert "per_gone" not in provider._permission_requests


@pytest.mark.asyncio
async def test_reload_does_not_poll_finished_children(tmp_path):
    provider = OpencodeProvider(tmp_path)
    provider._child_sessions = {C: P}
    client = _ReloadClient({})

    await provider._reload_pending_requests(client, P)  # type: ignore[arg-type]

    assert not any(C in path for path in client.paths)


@pytest.mark.asyncio
async def test_disconnect_forgets_child_sessions(tmp_path):
    provider = OpencodeProvider(tmp_path)
    provider._child_sessions = {C: P}

    await provider.disconnect()

    assert provider._child_sessions == {}


@pytest.mark.asyncio
async def test_refresh_background_shells_drops_commands_of_vanished_shells(tmp_path, monkeypatch):
    provider = OpencodeProvider(tmp_path)
    _attach_live(provider, object(), "ses_a")
    provider._background_shells = {"sh_gone", "sh_live"}
    provider._shell_commands = {"sh_gone": "sleep 60", "sh_live": "npm run dev"}
    _script(monkeypatch, provider, shells=[{"sh_live"}])

    assert await provider.refresh_background_shells() == 1
    rows = provider.background_shell_rows()
    assert [(row["id"], row["command"]) for row in rows] == [("sh_live", "npm run dev")]
    assert isinstance(rows[0]["started_at"], float)
    assert provider._shell_commands == {"sh_live": "npm run dev"}
