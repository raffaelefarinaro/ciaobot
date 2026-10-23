from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.schedules import ScheduleManager, ScheduleStore, compute_last_expected_run, compute_next_run, system_base_id, system_schedule_id
from datetime import UTC, datetime
from ciao.web.routes_api import schedule_detail


def _store(tmp_path: Path, *workspaces: str) -> ScheduleStore:
    return ScheduleStore(tmp_path, include_system=True, workspace_names=lambda: list(workspaces))


class _Pcm:
    def schedule_effective_routing(self, entry):
        return entry.provider, entry.model, entry.workspace

    def get_project(self, _project_id):
        return None

    def get_chat(self, _chat_id):
        return None

    events = None


def _client(tmp_path: Path) -> tuple[TestClient, ScheduleManager]:
    store = _store(tmp_path / ".runtime", "personal")
    manager = ScheduleManager(store)
    app = Starlette(routes=[Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH"])])
    app.state.schedule_manager = manager
    app.state.project_chat_manager = _Pcm()
    return TestClient(app), manager


def test_explicit_time_is_not_staggered_again(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal", "work")
    entry = next(e for e in store.list_entries() if e.schedule_id == system_schedule_id("system-memory-curation", "work"))
    entry.daily_time_utc = "08:35"
    entry.timezone_name = "America/New_York"
    store.replace(entry)
    reread = next(e for e in store.list_entries() if e.schedule_id == entry.schedule_id)
    assert (reread.daily_time_utc, reread.timezone_name) == ("08:35", "America/New_York")


def test_system_time_and_timezone_round_trip_across_store_restart(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc, entry.timezone_name = "07:45", "Pacific/Auckland"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc", "timezone_name"}))
    reopened = _store(tmp_path, "personal")
    restored = next(e for e in reopened.list_entries() if e.schedule_id == entry.schedule_id)
    assert (restored.daily_time_utc, restored.timezone_name) == ("07:45", "Pacific/Auckland")


def test_repeated_explicit_timing_edits_replace_persisted_values(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc, entry.timezone_name = "07:45", "Pacific/Auckland"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc", "timezone_name"}))
    store = _store(tmp_path, "personal")
    entry = store.get(entry.schedule_id)
    entry.daily_time_utc, entry.timezone_name = "08:25", "America/Chicago"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc", "timezone_name"}))
    restored = _store(tmp_path, "personal").get(entry.schedule_id)
    assert (restored.daily_time_utc, restored.timezone_name) == ("08:25", "America/Chicago")


def test_direct_changed_timing_replace_updates_existing_overlay(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc = "07:45"
    store.replace(entry)
    entry = _store(tmp_path, "personal").get(entry.schedule_id)
    entry.daily_time_utc = "08:25"
    store.replace(entry)
    assert _store(tmp_path, "personal").get(entry.schedule_id).daily_time_utc == "08:25"


def test_untouched_rows_keep_packaged_stagger(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal", "work")
    rows = [e for e in store.list_entries() if e.schedule_id.startswith("system-memory-curation@")]
    assert len(rows) == 2
    assert rows[0].daily_time_utc != rows[1].daily_time_utc


def test_timing_edit_preserves_disabled_state_and_run_history(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.enabled = False
    entry.last_status = "succeeded"
    entry.last_triggered_on = "2026-10-22"
    entry.daily_time_utc = "08:55"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc"}))
    restored = _store(tmp_path, "personal").get(entry.schedule_id)
    assert restored.enabled is False
    assert restored.last_status == "succeeded"
    assert restored.last_triggered_on == "2026-10-22"
    assert restored.daily_time_utc == "08:55"


def test_legacy_disabled_overlay_survives_timing_edit(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    (tmp_path / "system_schedules_state.json").write_text(json.dumps({"schedules": {
        entry.schedule_id: {"enabled": False, "last_status": "failed"}
    }}))
    entry = store.get(entry.schedule_id)
    entry.daily_time_utc = "09:25"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc"}))
    restored = _store(tmp_path, "personal").get(entry.schedule_id)
    assert restored.enabled is False
    assert restored.last_status == "failed"
    assert restored.daily_time_utc == "09:25"


def test_unsuffixed_legacy_overlay_timing_is_inherited_and_direct_editable(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    legacy_id = system_base_id(entry.schedule_id)
    state_path = tmp_path / "system_schedules_state.json"
    state_path.write_text(json.dumps({"schedules": {
        legacy_id: {"enabled": False, "daily_time_utc": "06:40", "timezone_name": "Asia/Tokyo"}
    }}))
    inherited = store.get(entry.schedule_id)
    assert (inherited.daily_time_utc, inherited.timezone_name, inherited.enabled) == ("06:40", "Asia/Tokyo", False)
    inherited.daily_time_utc = "07:50"
    store.replace(inherited)
    assert store.get(entry.schedule_id).daily_time_utc == "07:50"


def test_mixed_invalid_http_update_leaves_state_bytes_unchanged(tmp_path: Path) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    state_path = tmp_path / ".runtime" / "system_schedules_state.json"
    manager.replace(entry)  # prime an existing state file with bookkeeping only
    before = state_path.read_bytes()
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json={"enabled": False, "time": "9:00"})
    assert response.status_code == 400
    assert state_path.read_bytes() == before


def test_mcp_system_timing_uses_existing_aliases_and_persists(tmp_path: Path) -> None:
    store = _store(tmp_path / ".runtime", "personal")
    manager = ScheduleManager(store)
    plane = CiaoControlPlane(
        type("Config", (), {"workspace": lambda _self, name: object() if name == "personal" else None,
                            "workspace_root": str(tmp_path)})(),
        project_chat_manager=_Pcm(), schedule_manager=manager, background_runner=None,
    )
    principal = McpPrincipal("token", "chat", "project", "personal", "claude")
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    result = plane.schedule_update(principal, entry.schedule_id, daily_time="08:45", timezone="Europe/London")
    assert result["data"]["daily_time_utc"] == "08:45"
    assert result["data"]["timezone_name"] == "Europe/London"
    assert store.get(entry.schedule_id).daily_time_utc == "08:45"


def test_mcp_timezone_only_edit_and_conflicting_aliases(tmp_path: Path) -> None:
    store = _store(tmp_path / ".runtime", "personal")
    manager = ScheduleManager(store)
    plane = CiaoControlPlane(
        type("Config", (), {"workspace": lambda _self, name: object() if name == "personal" else None,
                            "workspace_root": str(tmp_path)})(),
        project_chat_manager=_Pcm(), schedule_manager=manager, background_runner=None,
    )
    principal = McpPrincipal("token", "chat", "project", "personal", "claude")
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    plane.schedule_update(principal, entry.schedule_id, timezone="Europe/London")
    assert store.get(entry.schedule_id).timezone_name == "Europe/London"
    with pytest.raises(ControlPlaneError, match="conflicting aliases"):
        plane.schedule_update(principal, entry.schedule_id, daily_time="08:00", daily_time_utc="09:00")


def test_mcp_system_timing_keeps_workspace_authorization(tmp_path: Path) -> None:
    store = _store(tmp_path / ".runtime", "personal", "work")
    manager = ScheduleManager(store)
    plane = CiaoControlPlane(
        type("Config", (), {"workspace": lambda _self, name: object() if name in {"personal", "work"} else None,
                            "workspace_root": str(tmp_path)})(),
        project_chat_manager=_Pcm(), schedule_manager=manager, background_runner=None,
    )
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@work"))
    principal = McpPrincipal("token", "chat", "project", "personal", "claude")
    with pytest.raises(ControlPlaneError, match="another workspace"):
        plane.schedule_update(principal, entry.schedule_id, daily_time="08:45", timezone="UTC")
    assert store.get(entry.schedule_id).daily_time_utc != "08:45"


def test_timing_edit_creates_no_user_schedule(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.timezone_name = "Asia/Tokyo"
    store.replace(entry, timing_fields=frozenset({"timezone_name"}))
    assert store._runtime_items() == []
    assert store.get(entry.schedule_id).timezone_name == "Asia/Tokyo"


def test_next_run_uses_selected_local_zone_across_dst(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc, entry.timezone_name = "07:30", "America/New_York"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc", "timezone_name"}))
    entry = store.get(entry.schedule_id)
    following = compute_next_run(entry, now=datetime(2026, 3, 8, 6, 0, tzinfo=UTC))
    assert following == datetime(2026, 3, 8, 11, 30, tzinfo=UTC)


def test_catch_up_uses_latest_selected_local_slot_only(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    store.record_system_first_seen(datetime(2026, 3, 5, tzinfo=UTC))
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc, entry.timezone_name = "07:30", "America/New_York"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc", "timezone_name"}))
    entry = store.get(entry.schedule_id)
    due = compute_last_expected_run(entry, now=datetime(2026, 3, 8, 15, 0, tzinfo=UTC))
    assert due == datetime(2026, 3, 8, 11, 30, tzinfo=UTC)


def test_timing_survives_changed_packaged_definition(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc = "06:20"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc"}))
    definition = store._load_system_definitions()[0].copy()
    definition["daily_time_utc"] = "11:00"
    store._system_definitions = [definition]
    changed = next(e for e in store.list_entries() if e.schedule_id == entry.schedule_id)
    assert changed.daily_time_utc == "06:20"


def test_explicit_same_package_time_survives_changed_definition(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    original_time = entry.daily_time_utc
    store.replace(entry, timing_fields=frozenset({"daily_time_utc"}))
    definition = store._load_system_definitions()[0].copy()
    definition["daily_time_utc"] = "11:00" if original_time != "11:00" else "12:00"
    store._system_definitions = [definition]
    assert store.get(entry.schedule_id).daily_time_utc == original_time


def test_packaged_prompt_still_updates_after_timing_override(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc = "06:20"
    store.replace(entry, timing_fields=frozenset({"daily_time_utc"}))
    definition = store._load_system_definitions()[0].copy()
    definition["prompt"] += " revised"
    store._system_definitions = [definition]
    changed = next(e for e in store.list_entries() if e.schedule_id == entry.schedule_id)
    assert changed.daily_time_utc == "06:20"
    assert changed.prompt.endswith(" revised")


def test_http_system_timing_response_matches_subsequent_get(tmp_path: Path) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json={"time": "08:10", "timezone": "Europe/London"})
    assert response.status_code == 200
    reread = manager._store.get(entry.schedule_id)
    assert response.json()["daily_time_utc"] == reread.daily_time_utc == "08:10"
    assert response.json()["timezone_name"] == reread.timezone_name == "Europe/London"


@pytest.mark.parametrize("body", [{"prompt": "changed"}, {"title": "changed"}, {"frequency": "weekly"}, {"target": "x"}])
def test_http_system_update_rejects_package_owned_fields(tmp_path: Path, body: dict) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json=body)
    assert response.status_code == 400


def test_http_system_update_requires_boolean_enabled(tmp_path: Path) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json={"enabled": "false"})
    assert response.status_code == 400
    assert manager._store.get(entry.schedule_id).enabled is True


@pytest.mark.parametrize("body", [None, [], {"time": None}, {"time": ["08:10"]}, {"timezone": ""}])
def test_http_system_rejects_malformed_updates_without_mutation(tmp_path: Path, body: object) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    state_path = tmp_path / ".runtime" / "system_schedules_state.json"
    manager.replace(entry)
    before = state_path.read_bytes()
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json=body)
    assert response.status_code == 400
    assert state_path.read_bytes() == before


def test_http_system_accepts_empty_object_without_state_drift(tmp_path: Path) -> None:
    client, manager = _client(tmp_path)
    entry = next(e for e in manager.list_entries() if e.schedule_id.endswith("@personal"))
    response = client.patch(f"/api/schedules/{entry.schedule_id}", json={})
    assert response.status_code == 200
    assert manager._store.get(entry.schedule_id).daily_time_utc == response.json()["daily_time_utc"]


def test_dispatch_bookkeeping_does_not_freeze_packaged_time(tmp_path: Path) -> None:
    store = _store(tmp_path, "personal", "work")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    original = entry.daily_time_utc
    entry.last_status = "running"
    store.replace(entry)
    state = json.loads((tmp_path / "system_schedules_state.json").read_text())
    assert "daily_time_utc" not in state["schedules"][entry.schedule_id]
    assert next(e for e in store.list_entries() if e.schedule_id == entry.schedule_id).daily_time_utc == original


@pytest.mark.parametrize("time,zone", [("9:00", "UTC"), ("25:00", "UTC"), ("09:00", "Not/AZone"), (9, "UTC"), ("09:00", ""), (None, "UTC"), (["09:00"], "UTC")])
def test_system_timing_rejects_invalid_values_without_writing(tmp_path: Path, time: object, zone: object) -> None:
    store = _store(tmp_path, "personal")
    entry = next(e for e in store.list_entries() if e.schedule_id.endswith("@personal"))
    entry.daily_time_utc = time
    entry.timezone_name = zone
    state_path = tmp_path / "system_schedules_state.json"
    store.replace(next(e for e in store.list_entries() if e.schedule_id == entry.schedule_id))
    before = state_path.read_bytes()
    with pytest.raises(ValueError):
        store.replace(entry)
    assert state_path.read_bytes() == before
