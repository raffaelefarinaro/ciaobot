"""`ciao.usage_stats`: workspace usage figures from the existing stores."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

import ciao.transcripts as transcripts_mod
from ciao import agent_cli, mcp_server, usage_stats
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError
from ciao.models import AgentRequest, ChatContext
from ciao.sessions import StateStore
from ciao.task_board import TaskBoardStore
from ciao.transcripts import TranscriptStore
from ciao.usage_stats import (
    ChatFacts,
    DayBucket,
    Event,
    StatsSources,
    bucket_events,
    build_response,
    collect_events,
    compute_usage_stats,
)
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import usage_stats_endpoint

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
WINDOW = (datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 10, tzinfo=UTC))
CONTRACT_KEYS = {
    "workspace", "range_days", "generated_at", "today", "totals", "previous", "daily_messages",
    "projects", "memory_pipeline", "hours", "models",
}
TOTAL_KEYS = {
    "chats_opened", "messages_sent", "chats_archived", "memories_saved",
    "tasks_completed", "projects_worked_on", "automation_runs", "tokens",
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    usage_stats.clear_cache()
    yield
    usage_stats.clear_cache()


def _sources(tmp_path: Path, chats: tuple[ChatFacts, ...] = (), *, workspace: str = "work",
             names: dict[str, str] | None = None) -> StatsSources:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return StatsSources(
        workspace=workspace,
        chats=chats,
        project_names=names or {},
        workspace_root=tmp_path,
        vault_root=vault,
        runtime_dir=tmp_path / ".runtime",
        job_runs_path=tmp_path / "job_runs.jsonl",
    )


def _chat(tmp_path: Path, chat_id: str = "c1", *, person: bool = True, project_id: str = "p1",
          created_at: str = "", sent: tuple[str, ...] = (), archived: bool = False,
          archive_path: str = "") -> ChatFacts:
    return ChatFacts(
        chat_id=chat_id,
        project_id=project_id,
        created_at=created_at,
        archived=archived,
        archive_path=archive_path,
        person=person,
        person_sent_at=sent,
        live_transcript_dir=tmp_path / ".runtime" / "transcripts" / chat_id,
        archive_dirs=(tmp_path / "Chats" / chat_id,),
    )


def _bucket(**fields: object) -> DayBucket:
    bucket = DayBucket()
    for name, value in fields.items():
        setattr(bucket, name, value)
    return bucket


def _build(buckets: dict[date, DayBucket], range_days: int = 7,
           names: dict[str, str] | None = None) -> dict:
    return build_response(
        buckets, workspace="work", today=TODAY, range_days=range_days, generated_at=NOW,
        project_names=names or {},
    )


# ── parse_range ──────────────────────────────────────────────────────────


def test_parse_range_defaults_and_rejects() -> None:
    assert usage_stats.parse_range(None) == 30
    assert usage_stats.parse_range("") == 30
    assert usage_stats.parse_range("7") == 7
    assert usage_stats.parse_range("365") == 365
    for bad in ("14", "abc", "-7", "0"):
        with pytest.raises(ValueError):
            usage_stats.parse_range(bad)


def test_cli_ranges_match_engine() -> None:
    assert agent_cli.STATS_RANGES == usage_stats.RANGES


# ── bucket_events ────────────────────────────────────────────────────────


def test_bucket_events_counts_messages_by_local_day_and_hour() -> None:
    at = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)  # 00:30 on 9 Nov in Zurich
    buckets = bucket_events([Event("message", at, project_id="p1")], ZoneInfo("Europe/Zurich"))
    assert list(buckets) == [date(2026, 11, 9)]
    bucket = buckets[date(2026, 11, 9)]
    assert bucket.messages_sent == 1
    assert bucket.hours[0] == 1 and sum(bucket.hours) == 1
    assert bucket.project_messages == {"p1": 1}


def test_bucket_events_tokens_models_and_funnel() -> None:
    at = datetime(2026, 11, 9, 10, tzinfo=UTC)
    buckets = bucket_events(
        [
            Event("turn", at, model="opus", tokens=120),
            Event("turn", at, model="", tokens=5),
            Event("proposal_decided", at, accepted=True),
            Event("proposal_decided", at, accepted=False),
            Event("proposal_pending", at),
            Event("note_updated", at),
        ],
        UTC,
    )
    bucket = buckets[date(2026, 11, 9)]
    assert bucket.tokens == 125
    assert bucket.models == {"opus": 1}
    assert (bucket.proposed, bucket.accepted, bucket.notes_updated) == (3, 1, 1)


# ── build_response ───────────────────────────────────────────────────────


def test_build_response_shape_and_windows() -> None:
    buckets = {
        TODAY: _bucket(messages_sent=2),
        date(2026, 11, 3): _bucket(messages_sent=1),
        date(2026, 11, 2): _bucket(messages_sent=5),
        date(2026, 10, 27): _bucket(messages_sent=1),
        date(2026, 10, 26): _bucket(messages_sent=9),
    }
    out = _build(buckets, range_days=7)
    assert set(out) == CONTRACT_KEYS
    assert set(out["totals"]) == TOTAL_KEYS and set(out["previous"]) == TOTAL_KEYS
    assert out["range_days"] == 7
    assert out["generated_at"] == "2026-11-09T12:00:00Z"
    assert out["totals"]["messages_sent"] == 3
    assert out["previous"]["messages_sent"] == 6
    assert len(out["daily_messages"]) == 182
    assert out["daily_messages"][0] == {"date": "2026-05-12", "count": 0}
    assert out["daily_messages"][-1] == {"date": "2026-11-09", "count": 2}
    assert len(out["hours"]) == 24


def test_build_response_today_averages_use_the_30_days_before_today() -> None:
    buckets = {TODAY - timedelta(days=offset): _bucket(chats_opened=1, messages_sent=2)
               for offset in range(1, 31)}
    buckets[TODAY - timedelta(days=1)] = _bucket(chats_opened=4, messages_sent=2)
    buckets[TODAY - timedelta(days=31)] = _bucket(chats_opened=100, messages_sent=100)
    buckets[TODAY] = _bucket(chats_opened=7)
    today = _build(buckets)["today"]
    assert today["chats_opened"] == 7
    assert today["avg_chats_opened_30d"] == 1.1
    assert today["avg_messages_sent_30d"] == 2.0


def test_build_response_projects_top_eight_and_drop_unknown() -> None:
    names = {f"p{i}": f"Project {i}" for i in range(9)}
    counts = {f"p{i}": i + 1 for i in range(9)}
    counts["gone"] = 50
    out = _build({TODAY: _bucket(project_messages=counts)}, names=names)
    assert [row["project_id"] for row in out["projects"]] == [f"p{i}" for i in range(8, 0, -1)]
    assert out["projects"][0] == {"project_id": "p8", "name": "Project 8", "messages": 9}
    assert out["totals"]["projects_worked_on"] == 10


def test_build_response_projects_worked_on_is_distinct_and_models_sorted() -> None:
    buckets = {
        TODAY: _bucket(project_messages={"p1": 1}, models={"haiku": 2, "opus": 2}),
        TODAY - timedelta(days=1): _bucket(project_messages={"p1": 3}, models={"sonnet": 5}),
    }
    out = _build(buckets, names={"p1": "One"})
    assert out["totals"]["projects_worked_on"] == 1
    assert out["models"] == [
        {"model": "sonnet", "turns": 5},
        {"model": "haiku", "turns": 2},
        {"model": "opus", "turns": 2},
    ]


# ── collect_events, one store at a time ──────────────────────────────────


def test_collect_chat_events_counts_person_chats_only(tmp_path: Path) -> None:
    person = _chat(tmp_path, "c1", created_at="2026-11-05T10:00:00Z",
                   sent=("2026-11-05T10:01:00Z", "2026-10-01T10:00:00Z"))
    engine = _chat(tmp_path, "c2", person=False, created_at="2026-11-05T10:00:00Z")
    events = collect_events(_sources(tmp_path, (person, engine)), *WINDOW)
    kinds = sorted(e.kind for e in events)
    assert kinds == ["chat_opened", "message"]


def test_collect_turn_and_archive_events_from_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcripts_mod, "_now_iso", lambda: "2026-11-05T10:00:00Z")
    store = TranscriptStore(tmp_path / ".runtime", tmp_path / "Chats")
    request = AgentRequest(prompt="hi", model="opus", mode="auto")
    usage = {"input_tokens": "100", "output_tokens": "20", "cache_read_input_tokens": "999"}
    archived_ctx = ChatContext.for_web("c1")
    store.record_turn(request, ctx=archived_ctx, response_text="yo", effective_model="claude-opus-4",
                      session_id="s1", usage=usage, quota={}, input_kind="text")
    archive = store.archive_session(ctx=archived_ctx, active_model="opus",
                                    last_effective_model="claude-opus-4", session_id="s1")
    assert archive is not None
    store.record_turn(request, ctx=ChatContext.for_web("c2"), response_text="yo",
                      effective_model="claude-sonnet-4", session_id="s2",
                      usage={"inputTokens": "7", "outputTokens": "3"}, quota={}, input_kind="text")
    chats = (
        _chat(tmp_path, "c1", archived=True, archive_path=archive.relative_to(tmp_path).as_posix()),
        _chat(tmp_path, "c2"),
    )
    events = collect_events(_sources(tmp_path, chats), *WINDOW)
    turns = sorted((e.model, e.tokens) for e in events if e.kind == "turn")
    assert turns == [("claude-opus-4", 120), ("claude-sonnet-4", 10)]
    archived = [e for e in events if e.kind == "chat_archived"]
    assert [e.at for e in archived] == [datetime(2026, 11, 5, 10, tzinfo=UTC)]


def test_collect_proposal_events_skips_skill_rows_and_counts_pending(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    workspace = sources.vault_root / "Workspace"
    workspace.mkdir(parents=True)
    rows = [
        {"promoted_at": "2026-11-05T09:00:00+00:00", "kind": "memory", "text": "a"},
        {"dismissed_at": "2026-11-05T09:00:00+00:00", "kind": "profile", "text": "b"},
        {"promoted_at": "2026-11-05T09:00:00+00:00", "kind": "skill", "text": "c"},
        {"promoted_at": "2026-10-01T09:00:00+00:00", "kind": "memory", "text": "d"},
    ]
    (workspace / "Memory-Proposals.dismissed.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    (workspace / "Memory-Proposals.md").write_text(
        "# Memory Proposals\n\n## 2026-11-06T08:00:00+00:00\n\n- [memory] fact one\n- [profile] likes tea\n"
        "\n## 2026-10-01T08:00:00+00:00\n\n- [memory] old fact\n",
        encoding="utf-8",
    )
    events = collect_events(sources, *WINDOW)
    decided = sorted(e.accepted for e in events if e.kind == "proposal_decided")
    assert decided == [False, True]
    assert len([e for e in events if e.kind == "proposal_pending"]) == 2


def test_collect_task_completion_events(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    clock = SimpleNamespace(now=datetime(2026, 11, 5, 12, tzinfo=UTC))
    store = TaskBoardStore(workspace="work", vault_root=sources.vault_root,
                           runtime_dir=sources.runtime_dir, clock=lambda: clock.now)
    created = store.create(title="ship it")
    store.update(created.record.id, expected_revision=created.revision,
                 changes={"status": "done"}, actor="user")
    store.create(title="still open")
    events = collect_events(sources, *WINDOW)
    assert [e.at for e in events if e.kind == "task_completed"] == [clock.now]


def test_collect_automation_events_filters_job_and_workspace(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    rows = [
        {"job": "schedule_dispatch", "started_at": "2026-11-05T09:00:00+00:00", "extra": {"workspace": "work"}},
        {"job": "schedule_dispatch", "started_at": "2026-11-05T09:00:00+00:00", "extra": {"workspace": "home"}},
        {"job": "vault_index", "started_at": "2026-11-05T09:00:00+00:00", "extra": {"workspace": "work"}},
        {"job": "schedule_dispatch", "started_at": "2026-10-01T09:00:00+00:00", "extra": {"workspace": "work"}},
    ]
    sources.job_runs_path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n",
                                     encoding="utf-8")
    events = collect_events(sources, *WINDOW)
    assert len([e for e in events if e.kind == "automation_run"]) == 1


def test_collect_note_events_counts_applied_changed_note_writes(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    journal = sources.vault_root / "Workspace" / "Memory-Receipts.jsonl"
    journal.parent.mkdir(parents=True)
    rows = [
        {"id": "r1", "ts": "2026-11-05T09:00:00+00:00", "kind": "note_apply", "status": "applied", "changed": True},
        {"id": "r2", "ts": "2026-11-05T09:00:00+00:00", "kind": "note_apply", "status": "applied", "changed": False},
        {"id": "r3", "ts": "2026-11-05T09:00:00+00:00", "kind": "note_apply", "status": "prepared"},
        {"id": "r4", "ts": "2026-11-05T09:00:00+00:00", "kind": "region_apply", "status": "applied"},
    ]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    events = collect_events(sources, *WINDOW)
    assert len([e for e in events if e.kind == "note_updated"]) == 1


# ── compute_usage_stats: cache ───────────────────────────────────────────


def test_compute_caches_past_days_and_recomputes_today(tmp_path: Path) -> None:
    first = _sources(tmp_path, (_chat(tmp_path, sent=("2026-11-08T10:00:00Z", "2026-11-09T09:00:00Z")),))
    out = compute_usage_stats(first, 7, now=NOW, tz=UTC)
    assert [d["count"] for d in out["daily_messages"][-2:]] == [1, 1]

    more = ("2026-11-08T10:00:00Z", "2026-11-08T11:00:00Z", "2026-11-09T09:00:00Z", "2026-11-09T10:00:00Z")
    second = _sources(tmp_path, (_chat(tmp_path, sent=more),))
    out = compute_usage_stats(second, 7, now=NOW, tz=UTC)
    assert [d["count"] for d in out["daily_messages"][-2:]] == [1, 2]

    other = compute_usage_stats(_sources(tmp_path, (_chat(tmp_path, sent=more),), workspace="home"),
                                7, now=NOW, tz=UTC)
    assert [d["count"] for d in other["daily_messages"][-2:]] == [2, 2]

    usage_stats.clear_cache()
    out = compute_usage_stats(second, 7, now=NOW, tz=UTC)
    assert [d["count"] for d in out["daily_messages"][-2:]] == [2, 2]


def test_compute_rejects_unknown_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        compute_usage_stats(_sources(tmp_path), 14, now=NOW, tz=UTC)


# ── engine wiring: registry snapshot, route, control plane, CLI ──────────


def _engine(tmp_path: Path, workspaces: tuple[str, ...] = ("work",)):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    return config, pcm


def _typed_chat(pcm: ProjectChatManager, workspace: str, *, unattended: bool = False):
    project = pcm.create_project(f"stats-{workspace}", workspace=workspace)
    chat = pcm.create_chat(project.project_id, title="hello")
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    chat.user_turn_timings["0"] = {"sent_at": now}
    chat.user_turn_timings["1"] = {"sent_at": now}
    chat.user_turn_unattended["1"] = True
    chat.user_turn_count = 2
    return project, chat


def test_snapshot_sources_keeps_typed_turns_of_the_workspace(tmp_path: Path) -> None:
    config, pcm = _engine(tmp_path, ("work", "home"))
    project, chat = _typed_chat(pcm, "work")
    _typed_chat(pcm, "home")
    sources = usage_stats.snapshot_sources(config, pcm, "work")
    assert [c.chat_id for c in sources.chats] == [chat.chat_id]
    assert sources.chats[0].person is True
    assert len(sources.chats[0].person_sent_at) == 1
    assert sources.project_names == {project.project_id: project.name}


def _client(config: CiaoConfig, pcm: ProjectChatManager):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/stats", usage_stats_endpoint, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.project_chat_manager = pcm
    client = TestClient(app, base_url="https://ciao.example")
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def test_stats_route_validates_workspace_and_range(tmp_path: Path) -> None:
    config, pcm = _engine(tmp_path)
    client, cookies = _client(config, pcm)
    assert client.get("/api/stats", cookies=cookies).status_code == 400
    assert client.get("/api/stats?workspace=nope", cookies=cookies).status_code == 400
    assert client.get("/api/stats?workspace=work&range=14", cookies=cookies).status_code == 400


def test_stats_route_returns_the_contract(tmp_path: Path) -> None:
    config, pcm = _engine(tmp_path)
    _typed_chat(pcm, "work")
    client, cookies = _client(config, pcm)
    response = client.get("/api/stats?workspace=work&range=7", cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == CONTRACT_KEYS
    assert data["workspace"] == "work"
    assert data["range_days"] == 7
    assert data["totals"]["messages_sent"] == 1
    assert data["totals"]["chats_opened"] == 1
    assert data["today"]["messages_sent"] == 1
    assert len(data["daily_messages"]) == 182 and len(data["hours"]) == 24
    assert client.get("/api/stats?workspace=work", cookies=cookies).json()["range_days"] == 30


def _principal(workspace: str, chat_id: str, project_id: str) -> AgentPrincipal:
    return AgentPrincipal(token_id="t", chat_id=chat_id, project_id=project_id,
                          workspace=workspace, provider="claude")


async def test_plane_usage_stats_is_scoped_to_the_callers_workspace(tmp_path: Path) -> None:
    config, pcm = _engine(tmp_path, ("work", "home"))
    plane = CiaoControlPlane(config, project_chat_manager=pcm, schedule_manager=SimpleNamespace())
    work_project, work_chat = _typed_chat(pcm, "work")
    home_project = pcm.create_project("stats-home", workspace="home")
    home_chat = pcm.create_chat(home_project.project_id, title="quiet")
    work = await plane.usage_stats(_principal("work", work_chat.chat_id, work_project.project_id), range_days=7)
    home = await plane.usage_stats(_principal("home", home_chat.chat_id, home_project.project_id), range_days=7)
    assert work["ok"] is True and work["data"]["totals"]["messages_sent"] == 1
    assert home["ok"] is True and home["data"]["totals"]["messages_sent"] == 0
    with pytest.raises(ControlPlaneError):
        await plane.usage_stats(_principal("work", work_chat.chat_id, work_project.project_id), range_days=14)


def test_usage_stats_operation_is_a_read() -> None:
    assert mcp_server.OPERATIONS_BY_NAME["usage_stats"].annotations == mcp_server._READ


def test_cli_resolves_stats_command() -> None:
    parser = agent_cli.build_parser()
    assert agent_cli.is_agent_invocation(["stats"])
    assert agent_cli.resolve(parser.parse_args(["stats"])) == ("usage_stats", {"range_days": 30})
    assert agent_cli.resolve(parser.parse_args(["stats", "--range", "90"])) == (
        "usage_stats", {"range_days": 90},
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["stats", "--range", "14"])
