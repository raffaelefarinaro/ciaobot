"""API tests for interval schedules and the retired /api/loops compat routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.schedules import INTERVAL_FREQUENCY, ScheduleManager, ScheduleStore
from ciao.web.routes_api import (
    create_loop,
    create_schedule,
    list_loops,
    list_schedules,
    loop_detail,
    run_loop_now,
    run_schedule_now,
    schedule_detail,
)


class _Chat:
    def __init__(
        self, title: str, *, archived: bool = False, project_id: str = "proj-1"
    ) -> None:
        self.title = title
        self.archived = archived
        self.project_id = project_id


class _NewChat:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id
        self.project_id = "proj-1"


class _Project:
    def __init__(self, project_id: str = "proj-1", name: str = "General") -> None:
        self.project_id = project_id
        self.name = name
        self.workspace = "personal"


class _ProjectChats:
    """Stub PCM: an idle chat, a busy chat, and a resolvable project."""

    def __init__(self) -> None:
        self.chats = {
            "chat-idle": _Chat("Idle chat"),
            "chat-busy": _Chat("Busy chat"),
            "chat-other-project": _Chat("Elsewhere", project_id="proj-2"),
        }
        self._created = 0
        self.events = None

    def get_chat(self, chat_id: str):
        return self.chats.get(chat_id)

    def get_project(self, project_id: str):
        if project_id in {"proj-1", "proj-2"}:
            return _Project(project_id, "General" if project_id == "proj-1" else "Other")
        return None


    def find_project(self, name: str, workspace: str):
        return _Project() if name == "General" else None

    def chat_stream_active(self, chat_id: str) -> bool:
        return chat_id == "chat-busy"

    def resolve_automation_project(self, entry: object):
        return _Project()

    def schedule_effective_routing(self, entry: object):
        return ("claude", getattr(entry, "model", "") or "chat-model", "personal")

    def create_chat(self, project_id: str, title: str = ""):
        self._created += 1
        new_id = f"chat-created-{self._created}"
        self.chats[new_id] = _Chat(title)
        return _NewChat(new_id)


class _StateStore:
    def get_mode(self, ctx):
        return "auto"


@pytest.fixture
def client(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    pcm = _ProjectChats()

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return {"status": "ok", "chat_id": target_chat_id}

    manager = ScheduleManager(
        store=ScheduleStore(runtime),
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=pcm.chat_stream_active,
        chat_dispatchable=lambda entry: pcm.get_chat(entry.web_chat_id) is not None,
    )
    app = Starlette(routes=[
        Route("/api/schedules", list_schedules, methods=["GET"]),
        Route("/api/schedules", create_schedule, methods=["POST"]),
        Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"]),
        Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH", "DELETE"]),
        Route("/api/loops", list_loops, methods=["GET"]),
        Route("/api/loops", create_loop, methods=["POST"]),
        Route("/api/loop-run/{loop_id}", run_loop_now, methods=["POST"]),
        Route("/api/loops/{loop_id}", loop_detail, methods=["PATCH", "DELETE"]),
    ])
    app.state.schedule_manager = manager
    app.state.project_chat_manager = pcm
    app.state.state_store = _StateStore()
    return TestClient(app)


def _create_interval(client: TestClient, **overrides) -> dict:
    body = {
        "prompt": "check PRs",
        "frequency": INTERVAL_FREQUENCY,
        "interval_minutes": 5,
        "web_chat_id": "chat-idle",
    }
    body.update(overrides)
    resp = client.post("/api/schedules", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── /api/schedules with frequency=interval ───────────────────────────────


def test_create_interval_needs_no_time_of_day(client: TestClient) -> None:
    entry = _create_interval(client)
    assert entry["frequency"] == INTERVAL_FREQUENCY
    assert entry["interval_minutes"] == 5
    assert entry["daily_time_utc"] == ""
    assert entry["context_label"] == "Idle chat"
    # Never fired but enabled: next_run is "now-ish", not null.
    assert entry["next_run"]
    # Relative cadence has no expected slot, so it can never read as missed.
    assert entry["missed"] is False
    assert entry["last_expected_run"] is None


def test_create_interval_validates_cadence_and_target(client: TestClient) -> None:
    for bad in (0, -1, "x"):
        resp = client.post("/api/schedules", json={
            "prompt": "p", "frequency": INTERVAL_FREQUENCY,
            "interval_minutes": bad, "web_chat_id": "chat-idle",
        })
        assert resp.status_code == 400, bad
    # No target at all: nothing to dispatch into.
    assert client.post("/api/schedules", json={
        "prompt": "p", "frequency": INTERVAL_FREQUENCY, "interval_minutes": 5,
    }).status_code == 400
    assert client.post("/api/schedules", json={
        "prompt": "p", "frequency": INTERVAL_FREQUENCY, "interval_minutes": 5,
        "web_chat_id": "chat-nope",
    }).status_code == 400


def test_unknown_frequency_is_rejected(client: TestClient) -> None:
    assert client.post(
        "/api/schedules", json={"prompt": "p", "frequency": "hourly", "time": "09:00"}
    ).status_code == 400


def test_interval_can_open_a_new_chat_per_run(client: TestClient) -> None:
    """The combination neither primitive offered before the merge."""
    entry = _create_interval(client, web_chat_id=None, web_project_id="proj-1")
    assert entry["frequency"] == INTERVAL_FREQUENCY
    assert entry["web_project_id"] == "proj-1"
    assert entry["context_label"] == "General (new chat per run)"


def test_patch_updates_cadence_and_pause_state(client: TestClient) -> None:
    schedule_id = _create_interval(client)["schedule_id"]

    body = client.patch(
        f"/api/schedules/{schedule_id}",
        json={"prompt": "new prompt", "interval_minutes": 3},
    ).json()
    assert body["prompt"] == "new prompt"
    assert body["interval_minutes"] == 3

    assert client.patch(
        f"/api/schedules/{schedule_id}", json={"enabled": False}
    ).json()["next_run"] is None

    assert client.patch(
        f"/api/schedules/{schedule_id}", json={"interval_minutes": 0}
    ).status_code == 400


def test_switching_to_interval_seeds_a_cadence(client: TestClient) -> None:
    """Frequency alone would leave interval_minutes at 0, which the floor turns
    into one minute — far faster than the caller asked for."""
    resp = client.post("/api/schedules", json={
        "prompt": "p", "frequency": "daily", "time": "09:00",
        "web_chat_id": "chat-idle",
    })
    schedule_id = resp.json()["schedule_id"]
    body = client.patch(
        f"/api/schedules/{schedule_id}", json={"frequency": INTERVAL_FREQUENCY}
    ).json()
    assert body["interval_minutes"] == 10


def test_run_now_conflicts_on_a_busy_chat(client: TestClient) -> None:
    idle = _create_interval(client)["schedule_id"]
    busy = _create_interval(client, web_chat_id="chat-busy")["schedule_id"]

    resp = client.post(f"/api/schedule-run/{idle}")
    assert resp.status_code == 201
    assert resp.json()["status"] == "started"

    assert client.post(f"/api/schedule-run/{busy}").status_code == 409


def test_context_stays_available_while_the_project_resolves(
    client: TestClient,
) -> None:
    """A lost chat does not strand an interval entry: the next run continues in
    a replacement chat, so the UI must not prompt the user to re-pick one."""
    schedule_id = _create_interval(client)["schedule_id"]
    body = client.patch(
        f"/api/schedules/{schedule_id}", json={"web_chat_id": "chat-gone"}
    ).json()
    assert body["context_available"] is True


# ── /api/loops compatibility ─────────────────────────────────────────────


def test_legacy_create_makes_an_interval_schedule(client: TestClient) -> None:
    resp = client.post("/api/loops", json={
        "prompt": "check PRs", "web_chat_id": "chat-idle",
        "interval_minutes": 5, "title": "PR watcher", "start": True,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["running"] is True
    assert body["autostart"] is True
    assert body["interval_minutes"] == 5
    assert body["context_label"] == "Idle chat"

    # The same entry is a first-class schedule.
    schedules = client.get("/api/schedules").json()
    assert [s["schedule_id"] for s in schedules] == [body["loop_id"]]
    assert schedules[0]["frequency"] == INTERVAL_FREQUENCY


def test_legacy_create_without_start_stays_stopped(client: TestClient) -> None:
    body = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()
    assert body["running"] is False
    assert body["next_run"] is None


def test_legacy_create_validates_its_inputs(client: TestClient) -> None:
    assert client.post("/api/loops", json={"web_chat_id": "chat-idle"}).status_code == 400
    assert client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-nope"}
    ).status_code == 400
    assert client.post(
        "/api/loops",
        json={"prompt": "p", "web_chat_id": "chat-idle", "interval_minutes": 0},
    ).status_code == 400


def test_legacy_patch_toggles_the_one_enabled_flag(client: TestClient) -> None:
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()["loop_id"]

    body = client.patch(f"/api/loops/{loop_id}", json={"running": True}).json()
    # autostart and running collapsed into one flag, so both report it.
    assert body["running"] is True and body["autostart"] is True

    body = client.patch(f"/api/loops/{loop_id}", json={"autostart": False}).json()
    assert body["running"] is False and body["autostart"] is False

    assert client.patch("/api/loops/loop-nope", json={"prompt": "x"}).status_code == 404


def test_legacy_list_shows_only_interval_entries(client: TestClient) -> None:
    interval = _create_interval(client)["schedule_id"]
    client.post("/api/schedules", json={
        "prompt": "daily", "frequency": "daily", "time": "09:00",
        "web_project_id": "proj-1",
    })
    assert [item["loop_id"] for item in client.get("/api/loops").json()] == [interval]


def test_legacy_run_now_and_busy_conflict(client: TestClient) -> None:
    idle = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()["loop_id"]
    busy = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-busy"}
    ).json()["loop_id"]

    assert client.post(f"/api/loop-run/{idle}").status_code == 201
    assert client.post(f"/api/loop-run/{busy}").status_code == 409
    assert client.post("/api/loop-run/loop-nope").status_code == 404


def test_legacy_delete(client: TestClient) -> None:
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()["loop_id"]
    assert client.delete(f"/api/loops/{loop_id}").json() == {"ok": True}
    assert client.get("/api/loops").json() == []
    assert client.delete(f"/api/loops/{loop_id}").json() == {"ok": False}


def test_interval_to_wall_clock_without_a_time_is_rejected(client: TestClient) -> None:
    """An interval entry carries no ``daily_time_utc``.

    Editing one to a wall-clock cadence therefore arrives with the time empty.
    Persisting that produced an automation that reads as enabled and can never
    fire: ``compute_next_run`` cannot parse the empty string, so ``tick()``
    never matches it.
    """
    schedule_id = _create_interval(client)["schedule_id"]

    resp = client.patch(
        f"/api/schedules/{schedule_id}", json={"frequency": "daily", "time": ""}
    )

    assert resp.status_code == 400
    assert "HH:MM" in resp.json()["error"]

    # The entry is untouched, still a working interval.
    after = client.get("/api/schedules").json()
    entry = next(s for s in after if s["schedule_id"] == schedule_id)
    assert entry["frequency"] == INTERVAL_FREQUENCY
    assert entry["next_run"] is not None


def test_interval_to_wall_clock_with_a_time_is_accepted(client: TestClient) -> None:
    schedule_id = _create_interval(client)["schedule_id"]

    body = client.patch(
        f"/api/schedules/{schedule_id}", json={"frequency": "daily", "time": "09:30"}
    ).json()

    assert body["frequency"] == "daily"
    assert body["daily_time_utc"] == "09:30"
    assert body["next_run"] is not None


@pytest.mark.parametrize("bad", ["", "9:30", "25:00", "09:60", "nope", "09:30:00"])
def test_wall_clock_times_that_compute_next_run_cannot_parse_are_rejected(
    client: TestClient, bad: str
) -> None:
    schedule_id = _create_interval(client)["schedule_id"]
    resp = client.patch(
        f"/api/schedules/{schedule_id}", json={"frequency": "daily", "time": bad}
    )
    assert resp.status_code == 400, f"{bad!r} should not be storable"


def test_manual_and_interval_still_need_no_time(client: TestClient) -> None:
    schedule_id = _create_interval(client)["schedule_id"]

    assert client.patch(
        f"/api/schedules/{schedule_id}", json={"frequency": "manual"}
    ).status_code == 200
    assert client.patch(
        f"/api/schedules/{schedule_id}",
        json={"frequency": INTERVAL_FREQUENCY, "interval_minutes": 5},
    ).status_code == 200


def test_a_legacy_created_loop_keeps_its_project_as_the_rehome_fallback(
    client: TestClient,
) -> None:
    """A cached pre-upgrade PWA can still create loops through this route.

    Migrated loops keep their original project as `fallback_project_id`; ones
    created here have to as well, or a loop made in a non-General project
    re-homes into General when its target chat is deleted and the unattended
    run changes context.
    """
    for start in (True, False):
        resp = client.post("/api/loops", json={
            "prompt": "p",
            "web_chat_id": "chat-idle",
            "start": start,
        })
        assert resp.status_code == 201, resp.text
        loop_id = resp.json()["loop_id"]

        # Read the stored row: fallback_project_id is internal and not part of
        # the API payload, so asserting on the response would prove nothing.
        stored = next(
            e for e in client.app.state.schedule_manager.list_entries()
            if e.schedule_id == loop_id
        )
        assert stored.fallback_project_id == "proj-1"
        # web_project_id stays unset: on an interval entry it would mean
        # "a new chat per run" and outrank the fixed chat.
        assert stored.web_project_id is None
        assert stored.enabled is start


def test_a_chat_bound_interval_records_its_chat_project_as_the_fallback(
    client: TestClient,
) -> None:
    """The path actually in use, not just migration and the legacy route.

    Without the fallback a chat-bound interval whose chat is deleted re-homes
    into the workspace's General and continues the unattended prompt in the
    wrong project.
    """
    schedule_id = _create_interval(client)["schedule_id"]

    stored = next(
        e for e in client.app.state.schedule_manager.list_entries()
        if e.schedule_id == schedule_id
    )
    assert stored.fallback_project_id == "proj-1"
    # Still a fixed-chat entry: web_project_id would mean "new chat per run".
    assert stored.web_project_id is None


def test_an_explicit_workspace_does_not_skip_the_fallback(
    client: TestClient,
) -> None:
    """The workspace derivation is skipped when the caller supplies one.

    The fallback is computed independently precisely so it is not lost in that
    case.
    """
    resp = client.post("/api/schedules", json={
        "prompt": "p",
        "frequency": INTERVAL_FREQUENCY,
        "interval_minutes": 5,
        "web_chat_id": "chat-idle",
        "workspace": "personal",
    })
    assert resp.status_code == 201, resp.text
    schedule_id = resp.json()["schedule_id"]

    stored = next(
        e for e in client.app.state.schedule_manager.list_entries()
        if e.schedule_id == schedule_id
    )
    assert stored.fallback_project_id == "proj-1"


def test_a_project_bound_interval_records_no_fallback(client: TestClient) -> None:
    """A project entry already names its project; a fallback would be noise."""
    resp = client.post("/api/schedules", json={
        "prompt": "p",
        "frequency": INTERVAL_FREQUENCY,
        "interval_minutes": 5,
        "web_project_id": "proj-1",
    })
    assert resp.status_code == 201, resp.text
    stored = next(
        e for e in client.app.state.schedule_manager.list_entries()
        if e.schedule_id == resp.json()["schedule_id"]
    )
    assert stored.fallback_project_id == ""
    assert stored.web_project_id == "proj-1"


def test_retargeting_an_interval_moves_its_rehome_fallback(client: TestClient) -> None:
    """Stamping at creation is not enough — the binding can change later.

    Editing a chat-bound entry from a chat in one project to a chat in another
    left the fallback on the old project, so deleting the new chat resumed the
    run in the previous project.
    """
    schedule_id = _create_interval(client)["schedule_id"]

    def stored():
        return next(
            e for e in client.app.state.schedule_manager.list_entries()
            if e.schedule_id == schedule_id
        )

    assert stored().fallback_project_id == "proj-1"

    assert client.patch(
        f"/api/schedules/{schedule_id}", json={"web_chat_id": "chat-other-project"}
    ).status_code == 200

    assert stored().fallback_project_id == "proj-2"


def test_converting_to_project_bound_clears_the_fallback(client: TestClient) -> None:
    """A project entry already names where its runs go."""
    schedule_id = _create_interval(client)["schedule_id"]

    client.patch(
        f"/api/schedules/{schedule_id}",
        json={"web_project_id": "proj-1", "web_chat_id": None},
    )

    stored = next(
        e for e in client.app.state.schedule_manager.list_entries()
        if e.schedule_id == schedule_id
    )
    assert stored.fallback_project_id == ""
