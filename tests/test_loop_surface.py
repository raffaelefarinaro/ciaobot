"""The model-facing `ciao loop` surface (self-loop slice L3).

A loop is an interval schedule owned by one chat. The chat comes from the
bearer token's principal, never from an argument, so these tests drive the
control plane with a principal and check what it stores.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import agent_cli
from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError
from ciao.schedules import (
    LOOP_MAX_GLOBAL,
    LOOP_MAX_PER_CHAT,
    ScheduleManager,
    ScheduleStore,
    loop_until_minutes,
    parse_loop_span,
)

WORK = "work"
CHAT = "chat-work"
NOON_UTC = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _chat(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = dict(
        chat_id=CHAT,
        mode="auto",
        user_turn_count=1,
        user_turn_origin={"0": ""},
        user_turn_unattended={},
        helper={},
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Pcm:
    def __init__(self, chats: dict[str, SimpleNamespace] | None = None) -> None:
        self.chats = {CHAT: _chat()} if chats is None else chats
        self.projects = {
            "project-work": SimpleNamespace(project_id="project-work", name="AI", workspace=WORK),
        }

    def get_chat(self, chat_id: str) -> SimpleNamespace | None:
        return self.chats.get(chat_id)

    def get_project(self, project_id: str) -> SimpleNamespace | None:
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None) -> list[SimpleNamespace]:
        return [p for p in self.projects.values() if workspace is None or p.workspace == workspace]


def _plane(tmp_path: Path, chats: dict[str, SimpleNamespace] | None = None):
    config = SimpleNamespace(workspace=lambda name: object() if name == WORK else None)
    store = ScheduleStore(tmp_path)
    plane = CiaoControlPlane(
        config,
        project_chat_manager=_Pcm(chats),
        schedule_manager=ScheduleManager(store=store),
    )
    return plane, store


def _principal(chat_id: str = CHAT) -> AgentPrincipal:
    return AgentPrincipal(
        token_id="token-1", chat_id=chat_id, project_id="project-work", workspace=WORK, provider="claude"
    )


def _start(plane: CiaoControlPlane, principal: AgentPrincipal | None = None, **overrides: object) -> dict:
    values: dict[str, object] = dict(every="5m", until="90m", prompt="check the build", max_runs=None)
    values.update(overrides)
    return plane.loop_start(principal or _principal(), **values)


# ---- the chat comes from the token, and both bounds are stored ------------


def test_loop_start_binds_to_the_token_chat_and_stores_both_bounds(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path)
    result = _start(plane, every="5m", until="90m")

    entry = store.get(result["data"]["id"])
    assert entry is not None
    assert entry.owner_chat_id == CHAT
    assert entry.web_chat_id == CHAT
    assert entry.web_project_id in (None, "")
    assert entry.frequency == "interval"
    assert entry.interval_minutes == 5
    assert entry.command == ""
    assert entry.archive_policy == "manual"
    assert entry.workspace == WORK
    assert entry.provider == "" and entry.model == ""
    assert entry.max_runs == 90 // 5 + 1
    created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00"))
    assert expires - created == timedelta(minutes=90)


def test_max_runs_only_stores_an_expiry_from_the_run_count(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path)
    result = _start(plane, every="5m", until=None, max_runs=3)

    entry = store.get(result["data"]["id"])
    assert entry is not None
    assert entry.max_runs == 3
    created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
    expires = datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00"))
    assert expires - created == timedelta(minutes=5 * 3 + 5)


def test_loop_start_takes_no_chat_argument(tmp_path: Path) -> None:
    params = inspect.signature(CiaoControlPlane.loop_start).parameters
    assert not {"chat_id", "owner_chat_id", "chat"} & set(params)
    with pytest.raises(SystemExit):
        agent_cli.build_parser().parse_args(
            ["loop", "start", "--every", "5m", "--until", "90m", "--prompt", "x", "--chat", "chat-other"]
        )


def test_neither_bound_is_refused(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane, until=None, max_runs=None)
    assert excinfo.value.code == "loop_invalid"
    assert "--until or --max-runs" in str(excinfo.value)


def test_both_bounds_are_refused(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane, until="90m", max_runs=3)
    assert excinfo.value.code == "loop_invalid"


# ---- the duration grammar --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("5m", 5), ("1h", 60), ("1h30m", 90), ("90m", 90), ("2h0m", 120), ("1H5M", 65)],
)
def test_span_grammar_accepts_minutes_hours_and_hours_minutes(text: str, minutes: int) -> None:
    assert parse_loop_span(text) == minutes


@pytest.mark.parametrize("text", ["", "5", "5min", "1.5h", "h", "m", "1h-5m", "14:30", "-5m", None])
def test_span_grammar_refuses_anything_else(text: object) -> None:
    assert parse_loop_span(text) is None


def test_until_wall_clock_is_the_next_occurrence_in_the_default_zone() -> None:
    # 12:00 UTC is 13:00 in Europe/Zurich (CET), so 14:30 is 90 minutes away.
    assert loop_until_minutes("14:30", NOON_UTC) == 90
    # 16:00 Zurich is past 14:30, so the next 14:30 is 22 hours 30 minutes away.
    assert loop_until_minutes("14:30", datetime(2026, 11, 21, 15, 0, tzinfo=UTC)) == 22 * 60 + 30


def test_until_refuses_garbage_with_an_example(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane, until="soon")
    assert excinfo.value.code == "invalid_until"
    assert "90m" in str(excinfo.value) and "14:30" in str(excinfo.value)


def test_every_refuses_garbage_with_an_example(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane, every="often")
    assert excinfo.value.code == "invalid_every"
    assert "5m" in str(excinfo.value)


# ---- caps -------------------------------------------------------------------


def test_a_chat_is_capped_and_the_refusal_names_the_loops_to_stop(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    ids = [_start(plane)["data"]["id"] for _ in range(LOOP_MAX_PER_CHAT)]
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane)
    assert excinfo.value.code == "loop_limit_chat"
    assert "ciao loop stop" in str(excinfo.value)
    assert all(loop_id in str(excinfo.value) for loop_id in ids)


def test_the_engine_is_capped_across_chats(tmp_path: Path) -> None:
    chats = {f"chat-{n}": _chat(chat_id=f"chat-{n}") for n in range(LOOP_MAX_GLOBAL)}
    chats[CHAT] = _chat()
    plane, store = _plane(tmp_path, chats)
    for n in range(LOOP_MAX_GLOBAL):
        store.create_owned_loop(owner_chat_id=f"chat-{n}", prompt="p", interval_minutes=5, duration_minutes=60)
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane)
    assert excinfo.value.code == "loop_limit_global"


# ---- authority: plan, person-typed turns ------------------------------------


def test_a_plan_chat_cannot_start_a_loop(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(mode="plan")})
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane)
    assert excinfo.value.code == "schedule_requires_operator"
    assert store.list_entries() == []


@pytest.mark.parametrize("origin", ["", "retry"])
def test_a_typed_turn_may_start_a_loop(tmp_path: Path, origin: str) -> None:
    plane, _store = _plane(tmp_path, {CHAT: _chat(user_turn_origin={"0": origin})})
    assert _start(plane)["ok"] is True


@pytest.mark.parametrize("origin", ["schedule", "loop", "webhook", "background_run"])
def test_a_non_person_origin_cannot_start_a_loop(tmp_path: Path, origin: str) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(user_turn_origin={"0": origin})})
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane)
    assert excinfo.value.code == "unattended_forbidden"
    assert store.list_entries() == []


def test_an_unattended_turn_cannot_start_a_loop(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(user_turn_unattended={"0": True})})
    with pytest.raises(ControlPlaneError) as excinfo:
        _start(plane)
    assert excinfo.value.code == "unattended_forbidden"
    assert store.list_entries() == []


# ---- list and stop see only this chat's loops -------------------------------


def test_loop_list_shows_only_this_chats_loops(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(), "chat-other": _chat(chat_id="chat-other")})
    mine = _start(plane)["data"]["id"]
    store.create_owned_loop(owner_chat_id="chat-other", prompt="theirs", interval_minutes=5, duration_minutes=60)

    rows = plane.loop_list(_principal())["data"]
    assert [row["id"] for row in rows] == [mine]
    row = rows[0]
    for key in ("every_minutes", "run_count", "max_runs", "ends_at", "next_run", "last_status", "prompt_head"):
        assert key in row, key


def test_loop_stop_ends_this_chats_loop(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path)
    loop_id = _start(plane)["data"]["id"]
    assert plane.loop_stop(_principal(), loop_id)["ok"] is True
    assert store.get(loop_id) is None


def test_loop_stop_without_an_id_needs_exactly_one_loop(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.loop_stop(_principal())
    assert excinfo.value.code == "loop_not_found"

    _start(plane)
    _start(plane)
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.loop_stop(_principal())
    assert excinfo.value.code == "loop_id_required"


def test_loop_stop_refuses_another_chats_loop(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(), "chat-other": _chat(chat_id="chat-other")})
    theirs = store.create_owned_loop(
        owner_chat_id="chat-other", prompt="theirs", interval_minutes=5, duration_minutes=60
    )
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.loop_stop(_principal(), theirs.schedule_id)
    assert excinfo.value.code == "loop_not_found"
    assert store.get(theirs.schedule_id) is not None


def test_schedule_verbs_cannot_reach_another_chats_loop(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(), "chat-other": _chat(chat_id="chat-other")})
    theirs = store.create_owned_loop(
        owner_chat_id="chat-other", prompt="theirs", interval_minutes=5, duration_minutes=60
    )
    with pytest.raises(ControlPlaneError) as update:
        plane.schedule_update(_principal(), theirs.schedule_id, title="mine now")
    with pytest.raises(ControlPlaneError) as delete:
        plane.schedule_delete(_principal(), theirs.schedule_id)
    with pytest.raises(ControlPlaneError) as run:
        asyncio.run(plane.schedule_run(_principal(), theirs.schedule_id))
    assert update.value.code == delete.value.code == run.value.code == "schedule_not_found"
    stored = store.get(theirs.schedule_id)
    assert stored is not None and stored.title != "mine now"


# ---- schedule_create: no unattended interval, and no loop fields via update --


def test_an_unattended_turn_cannot_create_an_interval_schedule(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path, {CHAT: _chat(user_turn_unattended={"0": True})})
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.schedule_create(
            _principal(), prompt="poll", frequency="interval", interval_minutes=5, project_id="project-work"
        )
    assert excinfo.value.code == "unattended_forbidden"
    assert store.list_entries() == []


def test_a_typed_turn_still_creates_an_interval_schedule(tmp_path: Path) -> None:
    plane, store = _plane(tmp_path)
    result = plane.schedule_create(
        _principal(), prompt="poll", frequency="interval", interval_minutes=15, project_id="project-work"
    )
    assert result["data"]["frequency"] == "interval"
    assert [e.owner_chat_id for e in store.list_entries()] == [""]


def test_an_unattended_turn_can_still_create_a_weekly_schedule(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path, {CHAT: _chat(user_turn_unattended={"0": True})})
    result = plane.schedule_create(
        _principal(), prompt="weekly", frequency="weekly", daily_time="09:00", project_id="project-work"
    )
    assert result["ok"] is True


def test_schedule_update_refuses_the_loop_lifetime_fields(tmp_path: Path) -> None:
    plane, _store = _plane(tmp_path)
    loop_id = _start(plane)["data"]["id"]
    for field, value in (("max_runs", 999), ("expires_at", "2099-01-01T00:00:00Z"), ("owner_chat_id", "chat-x")):
        with pytest.raises(ControlPlaneError) as excinfo:
            plane.schedule_update(_principal(), loop_id, **{field: value})
        assert excinfo.value.code == "invalid_fields", field


# ---- the CLI verbs ----------------------------------------------------------


def _resolve(*argv: str):
    return agent_cli.resolve(agent_cli.build_parser().parse_args(list(argv)))


def test_loop_is_an_agent_noun() -> None:
    assert agent_cli.is_agent_invocation(["loop", "list"])


def test_loop_start_resolves_to_the_loop_start_operation() -> None:
    assert _resolve("loop", "start", "--every", "5m", "--until", "90m", "--prompt", "hi") == (
        "loop_start",
        {"every": "5m", "prompt": "hi", "until": "90m"},
    )
    assert _resolve("loop", "start", "--every", "1h", "--max-runs", "4", "--prompt", "hi") == (
        "loop_start",
        {"every": "1h", "prompt": "hi", "max_runs": 4},
    )


def test_loop_start_reads_a_prompt_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Check the build and say what changed.\n", encoding="utf-8")
    op, arguments = _resolve("loop", "start", "--every", "5m", "--until", "14:30", "--prompt-file", str(prompt))
    assert op == "loop_start"
    assert arguments["prompt"] == "Check the build and say what changed.\n"
    assert arguments["until"] == "14:30"


def test_loop_start_refuses_both_prompt_sources_and_both_bounds(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("x", encoding="utf-8")
    parser = agent_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["loop", "start", "--every", "5m", "--until", "9m", "--max-runs", "2", "--prompt", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["loop", "start", "--every", "5m", "--until", "9m", "--prompt", "x", "--prompt-file", str(prompt)])


def test_loop_list_and_stop_resolve() -> None:
    assert _resolve("loop", "list") == ("loop_list", {})
    assert _resolve("loop", "stop") == ("loop_stop", {"loop_id": ""})
    assert _resolve("loop", "stop", "sched-1234") == ("loop_stop", {"loop_id": "sched-1234"})


def test_loop_operations_are_in_the_operation_table() -> None:
    from ciao import mcp_server

    for name in ("loop_start", "loop_list", "loop_stop"):
        assert name in mcp_server.OPERATIONS_BY_NAME, name


def test_the_schedule_tool_docstring_no_longer_claims_user_routines_run_in_bypass() -> None:
    from ciao import mcp_server

    doc = mcp_server._op_schedule.__doc__ or ""
    assert "execute in bypass" not in doc
    assert "ciao loop start" in doc


def test_the_core_prompt_routes_in_chat_loops_to_ciao_loop() -> None:
    from ciao.core_prompt import system_prompt_payload

    append = system_prompt_payload("")["append"]
    assert "ciao loop start" in append
    assert "loop start|list|stop" in append
    for native in ("CronCreate", "ScheduleWakeup", "/loop"):
        assert native in append, native
