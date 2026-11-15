"""Tests for the proposal-queue API routes (P5 server half).

The routes live in ``ciao/web/routes_api.py`` and are exercised here through a
bare Starlette app that registers them directly, mirroring the pattern in
``test_schedule_api_delivery_modes.py``. The PWA route wiring in ``app.py`` is
the UI delegate's follow-up, so it is not asserted here.
"""

from __future__ import annotations

import pathlib

import json
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import skill_proposals
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import proposal_service
from ciao.web.project_chats import ProjectChatManager
from ciao.web import routes_api
from ciao.web.proposal_service import _scan_proposal_rows
from ciao.web.routes_api import (
    dismiss_older_than,
    list_proposals,
    proposal_action,
    proposal_implement,
    proposal_preview,
    proposals_batch,
    proposals_handover,
    proposals_history,
)


def _config(tmp_path: Path) -> CiaoConfig:
    vault = tmp_path / "memory-vault"
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )


def _write_queue(config: CiaoConfig, workspace: str, content: str) -> None:
    _queue_text(config, workspace).write_text(content, encoding="utf-8")


def _queue_text(config: CiaoConfig, workspace: str) -> Path:
    """The workspace's memory proposal queue, the file a bullet lives in."""
    path = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_people_note(config: CiaoConfig, path: str, tags: list[str]) -> None:
    """Write a person note with frontmatter tags so rehome signal is real."""
    target = config.vault_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    tag_yaml = "\n".join(f"  - {t}" for t in tags)
    target.write_text(
        f"---\ntype: person\ntags:\n{tag_yaml}\ndescription: A person.\n---\n# {Path(path).stem}\n",
        encoding="utf-8",
    )


def _client(config: CiaoConfig, pcm: Any = None) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/history", proposals_history, methods=["GET"]),
            Route("/api/proposals/handover", proposals_handover, methods=["GET"]),
            Route("/api/proposals/{id}/preview", proposal_preview, methods=["POST"]),
            # Before `{id}/{action}`, exactly as app.py orders them: Starlette
            # matches in order and would otherwise read "implement" as an action.
            Route("/api/proposals/{id}/implement", proposal_implement, methods=["POST"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route("/api/proposals/batch", proposals_batch, methods=["POST"]),
            Route("/api/proposals/dismiss-older-than", dismiss_older_than, methods=["POST"]),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    return TestClient(app)


def _test_proposal_registry(tmp_path: Path, durable: bool):
    from ciao.web.proposal_reservations import DurableReservationRegistry, ReservationRegistry
    return DurableReservationRegistry(tmp_path / "reservation-db") if durable else ReservationRegistry()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_reserved_single_action_survives_request_cancellation(tmp_path: Path, monkeypatch, durable: bool) -> None:
    import asyncio
    import httpx
    from ciao.web.proposal_reservations import ReservationRegistry, seal_proposal_operation, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    raw_scope = {"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]}
    scope = validate_scope(raw_scope, set(config.workspace_names()))
    token = registry.reserve(scope)
    entered, release = threading.Event(), threading.Event()
    original = proposal_service._commit_region_row
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(proposal_service, "_commit_region_row", held)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as ac:
        task = asyncio.create_task(ac.post(f"/api/proposals/{row['id']}/accept",
                                           headers={"X-Ciao-Proposal-Reservation": token}, json={}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            seal = {"id": token, "scope": raw_scope, "inspected": True}
            assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 409
            assert (await ac.post(f"/api/proposals/{row['id']}/accept",
                                  headers={"X-Ciao-Proposal-Reservation": token}, json={})).status_code == 409
        finally:
            release.set()
        await asyncio.gather(*tuple(registry._tasks))
        await asyncio.sleep(0)
        assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 200
    assert row["text"] in _region_entries(config, row["workspace"], "memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_reserved_batch_survives_cancellation_and_seals_after_work(tmp_path: Path, monkeypatch, durable: bool) -> None:
    import asyncio
    import httpx
    from ciao.web.proposal_reservations import ReservationRegistry, seal_proposal_operation, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    raw_scope = {"kind": "batch_accept", "workspace": "", "targets": [row["id"]], "workspaces": [row["workspace"]]}
    scope = validate_scope(raw_scope, set(config.workspace_names()))
    token = registry.reserve(scope)
    entered, release = threading.Event(), threading.Event()
    original = proposal_service._commit_region_row
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(proposal_service, "_commit_region_row", held)
    body = {"action": "accept", "ids": [row["id"]]}
    headers = {"X-Ciao-Proposal-Reservation": token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as ac:
        task = asyncio.create_task(ac.post("/api/proposals/batch", headers=headers, json=body))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            seal = {"id": token, "scope": raw_scope, "inspected": True}
            assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 409
            assert (await ac.post("/api/proposals/batch", headers=headers, json=body)).status_code == 409
        finally:
            release.set()
        await asyncio.gather(*tuple(registry._tasks))
        await asyncio.sleep(0)
        assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 200
    assert row["text"] in _region_entries(config, row["workspace"], "memory")


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_reserved_sweep_survives_cancel_until_worker_finishes(tmp_path: Path, monkeypatch, durable: bool) -> None:
    import asyncio
    import httpx
    from dataclasses import replace
    from ciao.web.proposal_reservations import ReservationRegistry, capture_sweep_snapshot, seal_proposal_operation, validate_scope
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    client = _client(config)
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    raw_scope = {"kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
                 "workspaces": sorted(config.workspace_names())}
    scope = replace(validate_scope(raw_scope, set(config.workspace_names())), sweep_snapshot=capture_sweep_snapshot(config))
    token = registry.reserve(scope)
    entered, release = threading.Event(), threading.Event()
    original = proposal_service._sweep_queue_file
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(proposal_service, "_sweep_queue_file", held)
    headers = {"X-Ciao-Proposal-Reservation": token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as ac:
        task = asyncio.create_task(ac.post("/api/proposals/dismiss-older-than?date=2026-08-01", headers=headers))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            seal = {"id": token, "scope": raw_scope, "inspected": True}
            assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 409
            assert (await ac.post("/api/proposals/dismiss-older-than?date=2026-08-01", headers=headers)).status_code == 409
        finally:
            release.set()
        await asyncio.gather(*tuple(registry._tasks))
        await asyncio.sleep(0)
        assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 200
    assert "Old fact." not in _queue_text(config, "personal").read_text()


@pytest.mark.parametrize("case", ["cutoff", "workspaces", "sealed"])
def test_reserved_sweep_refuses_mismatch_or_sealed(tmp_path: Path, case: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
                            "workspaces": ["personal"] if case == "workspaces" else sorted(config.workspace_names())},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    if case == "sealed":
        assert registry.seal(token, scope)
    before = _queue_text(config, "personal").read_bytes()
    date = "2026-09-01" if case == "cutoff" else "2026-08-01"
    response = client.post(f"/api/proposals/dismiss-older-than?date={date}",
                           headers={"X-Ciao-Proposal-Reservation": token})
    assert response.status_code == 409
    assert _queue_text(config, "personal").read_bytes() == before


def test_sealed_sweep_rejects_before_reading_workspace_snapshot(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    names = sorted(config.workspace_names())
    scope = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-08-01"], "workspaces": names}, set(names))
    token = registry.reserve(scope)
    assert registry.seal(token, scope)
    def forbidden(_config):
        raise AssertionError("sealed request read workspace snapshot")
    monkeypatch.setattr(type(config), "workspace_names", forbidden)
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01",
                           headers={"X-Ciao-Proposal-Reservation": token})
    assert response.status_code == 409


@pytest.mark.parametrize("case", ["mismatch", "sealed", "duplicate"])
def test_reserved_batch_rejects_mismatch_sealed_and_duplicate(tmp_path: Path, case: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "batch_accept", "workspace": "", "targets": [row["id"]],
                            "workspaces": ["work" if case == "mismatch" else row["workspace"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    if case == "sealed":
        assert registry.seal(token, scope)
    ids = [row["id"], row["id"]] if case == "duplicate" else [row["id"]]
    response = client.post("/api/proposals/batch", headers={"X-Ciao-Proposal-Reservation": token},
                           json={"action": "accept", "ids": ids})
    assert response.status_code == (400 if case == "duplicate" else 409)
    assert "no_effect" not in response.json()
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")


@pytest.mark.parametrize("case", ["mismatch", "sealed"])
def test_reserved_single_action_rejects_mismatch_or_sealed(tmp_path: Path, case: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": "work" if case == "mismatch" else row["workspace"],
                            "targets": [row["id"]]}, set(config.workspace_names()))
    token = registry.reserve(scope)
    if case == "sealed":
        assert registry.seal(token, scope)
    response = client.post(f"/api/proposals/{row['id']}/accept", headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 409
    assert "no_effect" not in response.json()
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert row["id"] in {item["id"] for item in client.get("/api/proposals").json()["rows"]}


@pytest.mark.parametrize("case", ["missing", "workspace", "destination", "destination_error"])
def test_admitted_single_reresolution_refusal_is_proven_no_effect(tmp_path: Path, monkeypatch, case: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    import ciao.web.routes_api as routes
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    before = _queue_text(config, row["workspace"]).read_bytes()
    scan = proposal_service._scan_proposal_rows
    calls = 0
    def changed_scan(config):
        nonlocal calls
        rows, by_id = scan(config)
        calls += 1
        if calls == 2 and case == "missing":
            by_id.pop(row["id"])
        elif calls == 2 and case == "workspace":
            by_id[row["id"]] = {**by_id[row["id"]], "workspace": "work"}
        return rows, by_id
    monkeypatch.setattr(proposal_service, "_scan_proposal_rows", changed_scan)
    original_scope = routes._single_reservation_scope
    scope_calls = 0
    def changed_scope(*args):
        nonlocal scope_calls
        scope_calls += 1
        if scope_calls == 2 and case == "destination_error":
            raise ValueError("destination changed")
        if scope_calls == 2 and case == "destination":
            return validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]],
                                   "destination": "work"}, set(config.workspace_names()))
        return original_scope(*args)
    monkeypatch.setattr(routes, "_single_reservation_scope", changed_scope)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == (404 if case == "missing" else 409)
    assert response.json()["id"] == row["id"] and response.json()["no_effect"] is True
    assert calls == 2
    assert _queue_text(config, row["workspace"]).read_bytes() == before
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.parametrize("case", ["missing", "workspace", "invalid"])
def test_admitted_batch_reresolution_refusal_is_proven_no_effect(tmp_path: Path, monkeypatch, case: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "batch_accept", "workspace": "", "targets": [row["id"]],
                            "workspaces": [row["workspace"]]}, set(config.workspace_names()))
    token = registry.reserve(scope)
    before = _queue_text(config, row["workspace"]).read_bytes()
    resolve = proposal_service._resolve_batch
    calls = 0
    def changed_resolve(config, ids):
        nonlocal calls
        calls += 1
        resolved, error = resolve(config, ids)
        if calls == 2 and case == "missing":
            return None, "proposal missing"
        if calls == 2 and resolved is not None:
            resolved[0] = {**resolved[0], "workspace": "work"}
            if case == "workspace":
                resolved[0]["row"] = {**resolved[0]["row"], "workspace": "work"}
        return resolved, error
    monkeypatch.setattr(proposal_service, "_resolve_batch", changed_resolve)
    response = client.post("/api/proposals/batch", headers={"X-Ciao-Proposal-Reservation": token},
                           json={"action": "accept", "ids": [row["id"]]})
    assert response.status_code == (404 if case == "missing" else 409)
    assert response.json()["ids"] == [row["id"]] and response.json()["no_effect"] is True
    assert calls == 2
    assert _queue_text(config, row["workspace"]).read_bytes() == before
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.parametrize("batch", [False, True])
def test_admitted_row_removed_before_reresolution(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    value = {"kind": "batch_accept" if batch else "row_accept", "workspace": "" if batch else row["workspace"],
             "targets": [row["id"]]}
    if batch:
        value["workspaces"] = [row["workspace"]]
    scope = validate_scope(value, set(config.workspace_names()))
    token = registry.reserve(scope)
    queue = _queue_text(config, row["workspace"])
    run_admitted = registry._run_admitted
    async def remove_after_admission(token, operation):
        async def interleaved():
            queue.write_text("", encoding="utf-8")
            return await operation()
        return await run_admitted(token, interleaved)
    monkeypatch.setattr(registry, "_run_admitted", remove_after_admission)
    headers = {"X-Ciao-Proposal-Reservation": token}
    response = (client.post("/api/proposals/batch", headers=headers, json={"action": "accept", "ids": [row["id"]]})
                if batch else client.post(f"/api/proposals/{row['id']}/accept", headers=headers, json={}))
    assert response.status_code == 404
    assert response.json()["no_effect"] is True
    assert response.json()["ids" if batch else "id"] == ([row["id"]] if batch else row["id"])
    assert queue.read_text(encoding="utf-8") == ""
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.parametrize("durable", [False, True])
def test_admitted_row_removed_after_reresolution_before_queue_check(tmp_path: Path, monkeypatch, durable: bool) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    queue = _queue_text(config, row["workspace"])
    original = proposal_service.bullets_present
    def remove_before_check(path, bullets):
        queue.write_text("", encoding="utf-8")
        return original(path, bullets)
    monkeypatch.setattr(proposal_service, "bullets_present", remove_before_check)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 409
    assert response.json()["id"] == row["id"] and response.json()["no_effect"] is True
    assert queue.read_text(encoding="utf-8") == ""
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)
    if durable:
        from ciao.web.proposal_reservations import DurableReservationRegistry
        assert DurableReservationRegistry(tmp_path / "reservation-db").seal(token, scope, inspected=True)


@pytest.mark.parametrize("batch", [False, True])
def test_active_reservation_conflict_never_claims_no_effect(tmp_path: Path, batch: bool) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    value = {"kind": "batch_accept" if batch else "row_accept", "workspace": "" if batch else row["workspace"],
             "targets": [row["id"]]}
    if batch:
        value["workspaces"] = [row["workspace"]]
    scope = validate_scope(value, set(config.workspace_names()))
    token = registry.reserve(scope)
    assert registry.admit(token, scope)
    headers = {"X-Ciao-Proposal-Reservation": token}
    response = (client.post("/api/proposals/batch", headers=headers, json={"action": "accept", "ids": [row["id"]]})
                if batch else client.post(f"/api/proposals/{row['id']}/accept", headers=headers, json={}))
    assert response.status_code == 409 and "no_effect" not in response.json()
    assert not registry.seal(token, scope, inspected=True)
    assert registry.finish(token)
    assert registry.seal(token, scope, inspected=True)


def test_expired_reserved_single_action_cannot_dispatch(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    now = [0.0]
    registry = ReservationRegistry(clock=lambda: now[0])
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    before = _queue_text(config, row["workspace"]).read_bytes()
    now[0] = 301.0
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 409
    assert not registry.seal(token, scope, inspected=True)
    assert _queue_text(config, row["workspace"]).read_bytes() == before
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")


@pytest.mark.parametrize("batch", [False, True])
def test_durable_completed_route_seal_retries_after_registry_reopen(tmp_path: Path, batch: bool) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry, reserve_proposal_operation, seal_proposal_operation
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    directory = tmp_path / "reservation-db"
    client.app.state.proposal_reservations = DurableReservationRegistry(directory)
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    raw_scope = ({"kind": "batch_accept", "workspace": "", "targets": [row["id"]], "workspaces": [row["workspace"]]}
                 if batch else {"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]})
    reply = client.post("/api/proposals/reservations", json=raw_scope)
    assert reply.status_code == 200
    scope = reply.json().get("scope", raw_scope)
    token = reply.json()["id"]
    headers = {"X-Ciao-Proposal-Reservation": token}
    result = (client.post("/api/proposals/batch", headers=headers, json={"action": "accept", "ids": [row["id"]]})
              if batch else client.post(f"/api/proposals/{row['id']}/accept", headers=headers, json={}))
    assert result.status_code == 200
    assert row["text"] in _region_entries(config, row["workspace"], "memory")
    body = {"id": token, "scope": scope, "inspected": True}
    assert client.post("/api/proposals/reservations/seal", json=body).status_code == 200
    client.app.state.proposal_reservations = DurableReservationRegistry(directory)
    assert client.post("/api/proposals/reservations/seal", json=body).status_code == 200


@pytest.mark.parametrize("batch", [False, True])
def test_second_revision_check_refuses_before_effect_with_explicit_proof(tmp_path: Path, monkeypatch, batch: bool) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    before = _queue_text(config, row["workspace"]).read_bytes()
    checks = []
    def revisions(*args, **kwargs):
        checks.append(True)
        return "old" if len(checks) == 1 else "new"
    monkeypatch.setattr(proposal_service, "destination_revision", revisions)
    if batch:
        response = client.post("/api/proposals/batch", json={
            "action": "accept", "ids": [row["id"]], "revisions": {row["id"]: "old"},
        })
        result = response.json()["results"][0]
        assert response.status_code == 200
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept", json={"expected_revision": "old"})
        result = response.json()
        assert response.status_code == 409
    assert len(checks) == 2 and result["id"] == row["id"]
    assert result["conflict"] is True and result["no_effect"] is True
    assert _queue_text(config, row["workspace"]).read_bytes() == before
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")


def test_reserved_single_action_preserves_body_and_finishes(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token},
                           json={"text": "Remember the edited lesson.", "reason": "yes, everywhere"})
    assert response.status_code == 200
    assert _history_for(client, row["text"])[0]["reason"] == "yes, everywhere"
    assert "Remember the edited lesson." in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)


def test_reserved_single_action_preflight_refusal_finishes_without_effect(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _learnings_vault(tmp_path)
    (config.workspace_vault_root("personal") / "Workspace" / "Memory-Receipts.jsonl").mkdir()
    client = _client(config)
    row = _accept_kind_row(client, "learnings")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 503 and response.json()["no_effect"] is True
    assert _learnings_count(config) == 0
    assert registry.seal(token, scope, inspected=True)


def test_reserved_single_action_rechecks_workspace_before_effect(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": row["workspace"], "targets": [row["id"]]},
                           set(config.workspace_names()))
    token = registry.reserve(scope)
    scan = proposal_service._scan_proposal_rows
    count = 0
    def changed(config):
        nonlocal count
        rows, by_id = scan(config)
        count += 1
        if count == 2:
            by_id[row["id"]] = {**by_id[row["id"]], "workspace": "work"}
        return rows, by_id
    monkeypatch.setattr(proposal_service, "_scan_proposal_rows", changed)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 409
    assert count == 2 and row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert registry.seal(token, scope, inspected=True)


_SIMPLE_QUEUE = """# Memory Proposals

## 2026-08-19 curation pass (this pass)

- [memory] Remember the lesson about check-first.  _(from: Decisions)_
- [profile] Raffa prefers direct implementation.  _(from: Decisions)_
- [user] A legacy user bullet normalizes to profile.  _(from: Decisions)_
"""


def _default_vault(tmp_path: Path) -> CiaoConfig:
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(config, "personal", _SIMPLE_QUEUE)
    return config


# BOTH regions, because a missing region is CREATED rather than refused — only a
# duplicated one is unwritable. Corrupting `memory` alone let every profile row
# through and the batch half-succeeded.
_DUPLICATED_REGION_GUIDE = (
    "# Guide\n"
    "<!-- ciao:memory:start cap=2200 -->\n- a fact\n<!-- ciao:memory:end -->\n"
    "<!-- ciao:memory:start cap=2200 -->\n- another\n<!-- ciao:memory:end -->\n"
    "<!-- ciao:profile:start cap=1375 -->\n- a trait\n<!-- ciao:profile:end -->\n"
    "<!-- ciao:profile:start cap=1375 -->\n- another\n<!-- ciao:profile:end -->\n"
)


def _corrupt_guide(config: CiaoConfig, workspace: str) -> None:
    """Make one workspace's guide unwritable by duplicating a region's markers.

    A realistic cause — a bad hand edit — and the failure the write-then-dismiss
    order exists for. It replaced an over-cap region as the injection here: the
    cap is advisory now, so an over-cap write succeeds and no longer fails a batch.
    """
    guide = config.agent_root(workspace) / "AGENTS.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text(_DUPLICATED_REGION_GUIDE, encoding="utf-8")

def test_list_returns_rows_with_workspace_path_and_kind(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    resp = _client(config).get("/api/proposals")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    kinds = {r["kind"] for r in rows}
    assert kinds == {"memory", "profile", "user"}
    for row in rows:
        assert row["workspace"] == "personal"
        assert row["path"] == "personal/Workspace/Memory-Proposals.md"
        assert isinstance(row["line"], int)
        assert row["id"]


def test_ids_are_stable_across_calls(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    first = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    second = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    assert first == second


def test_dismiss_preserves_the_text_for_append_dedupe(tmp_path: Path) -> None:
    """A PWA dismissal must stop the nightly curator from re-filing the fact.

    The curator re-reads the same recent transcripts every night; dedupe
    against live bullets alone forgets the decision the moment the row is
    dropped, so the sidecar has to carry the rejected text.
    """
    from ciao.memory_proposals import append_proposals, dismissed_log_path
    from ciao.memory_proposals import MemoryProposal

    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    row = next(r for r in rows if r["text"].startswith("Remember the lesson"))

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")
    assert resp.status_code == 200

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert row["text"] in dismissed_log_path(queue).read_text(encoding="utf-8")

    # The nightly pass finds the same fact and tries again: refused as a dupe.
    assert (
        append_proposals(
            [MemoryProposal(target="memory", text=row["text"], source_section="curation")],
            config.workspace_vault_root("personal"),
        )
        is None
    )


def test_ids_survive_another_row_being_removed(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    memory = next(r for r in rows if r["kind"] == "memory")
    profile = next(r for r in rows if r["kind"] == "profile")
    client.post(f"/api/proposals/{memory['id']}/dismiss")
    rows_after = client.get("/api/proposals").json()["rows"]
    # The survivor's id is unchanged: dismissing a row never renumbers or
    # renames another, because the id derives from content, not position.
    assert next(r["id"] for r in rows_after if r["kind"] == "profile") == profile["id"]
    assert {r["kind"] for r in rows_after} == {"profile", "user"}


def _accept_kind_row(client: TestClient, kind: str) -> dict:
    rows = client.get("/api/proposals").json()["rows"]
    return next(r for r in rows if r["kind"] == kind)


def test_accept_dispatches_the_kinds_own_action(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    for kind, expected_region in (("memory", "memory"), ("profile", "profile"), ("user", "profile")):
        row = _accept_kind_row(client, kind)
        resp = client.post(f"/api/proposals/{row['id']}/accept")
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["action"] == "edit_region"
        assert result["region"] == expected_region


def _rerooted_vault(config: CiaoConfig, tmp_path: Path) -> None:
    """Give the fixture the per-root layout a real move needs."""
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def _per_root_config(tmp_path: Path) -> CiaoConfig:
    """A migrated install: each workspace owns a folder holding its own vault."""
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"{name}/memory-vault")
            for name in ("personal", "work")
        },
    )


def _rehome_fixture(tmp_path: Path, tags: list[str]) -> tuple[CiaoConfig, TestClient, dict]:
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    note.write_text(
        "---\ntype: person\ntags:\n" + "".join(f"  - {t}\n" for t in tags) + "---\n# Mo\n",
        encoding="utf-8",
    )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    return config, client, row


def test_reserved_rehome_binds_actual_destination(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation, validate_scope
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    config.workspaces["other"] = WorkspaceConfig(name="other", vault_root="other/memory-vault")
    config.workspace_vault_root("other").mkdir(parents=True, exist_ok=True)
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    requested = {"kind": "row_accept", "workspace": "personal", "targets": [row["id"]], "destination": "work"}
    reservation = client.post("/api/proposals/reservations", json=requested)
    assert reservation.status_code == 200
    assert reservation.json()["scope"] == requested
    token = reservation.json()["id"]
    headers = {"X-Ciao-Proposal-Reservation": token}
    wrong = client.post(f"/api/proposals/{row['id']}/accept?workspace=other", headers=headers, json={})
    assert wrong.status_code == 409
    assert (config.workspace_vault_root("personal") / "People/Mo.md").exists()
    right = client.post(f"/api/proposals/{row['id']}/accept?workspace=work", headers=headers, json={})
    assert right.status_code == 200
    assert (config.workspace_vault_root("work") / "People/Mo.md").exists()
    assert registry.seal(token, validate_scope(requested, set(config.workspace_names())), inspected=True)


def test_rehome_reservation_reports_server_derived_destination(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    _config_here, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    response = client.post("/api/proposals/reservations", json={
        "kind": "row_accept", "workspace": "personal", "targets": [row["id"]], "destination": "",
    })
    assert response.status_code == 200
    assert response.json()["scope"]["destination"] == "work"


@pytest.mark.parametrize("requested", ["", "work"])
def test_batch_rehome_reservation_binds_actual_destination(tmp_path: Path, requested: str) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    scope = {"kind": "batch_accept", "workspace": requested, "targets": [row["id"]],
             "workspaces": ["personal"]}
    reply = client.post("/api/proposals/reservations", json=scope)
    assert reply.status_code == 200
    assert reply.json()["scope"] == {**scope, "destinations": {row["id"]: "work"}}
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]], "workspace": requested},
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 200
    assert (config.workspace_vault_root("work") / "People/Mo.md").exists()


def test_batch_rehome_changed_signal_after_reservation_refuses_before_effect(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    queue = _queue_text(config, "personal")
    with queue.open("a", encoding="utf-8") as stream:
        stream.write("- [memory] Keep this fact unchanged. _(from: Decisions)_\n")
    other = next(item for item in client.get("/api/proposals").json()["rows"] if item["kind"] == "memory")
    ids = [other["id"], row["id"]]
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    scope = {"kind": "batch_accept", "workspace": "", "targets": ids, "workspaces": ["personal"]}
    reply = client.post("/api/proposals/reservations", json=scope)
    assert reply.status_code == 200
    source = config.workspace_vault_root("personal") / "People/Mo.md"
    source.write_text("---\ntype: person\ntags:\n  - person\n---\n# Mo\n", encoding="utf-8")
    before = queue.read_bytes()
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": ids},
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 409
    assert source.exists() and not (config.workspace_vault_root("work") / "People/Mo.md").exists()
    assert queue.read_bytes() == before
    assert other["text"] not in _region_entries(config, "personal", "memory")


def test_batch_rehome_signal_changes_after_admission(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "batch_accept", "workspace": "", "targets": [row["id"]], "workspaces": ["personal"],
    })
    assert reply.status_code == 200
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    queue = _queue_text(config, "personal")
    before = queue.read_bytes()
    run_admitted = registry._run_admitted
    async def change_after_admission(token, operation):
        async def interleaved():
            note.write_text("---\ntype: person\ntags:\n  - person\n---\n# Mo\n", encoding="utf-8")
            return await operation()
        return await run_admitted(token, interleaved)
    monkeypatch.setattr(registry, "_run_admitted", change_after_admission)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]},
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 409
    assert response.json()["ids"] == [row["id"]] and response.json()["no_effect"] is True
    assert note.exists() and not (config.workspace_vault_root("work") / "People/Mo.md").exists()
    assert queue.read_bytes() == before


def test_reserved_mixed_batch_binds_two_implicit_destinations(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for ws, name, tag in (("personal", "Mo", "colleague"), ("work", "Ida", "friend")):
        (config.workspace_vault_root(ws) / f"People/{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n  - {tag}\n---\n# {name}\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n"
                 "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? _(from: vault-rehome)_\n"
                 "- [memory] Prefers careful tests. _(from: Decisions)_\n")
    _write_queue(config, "work", "## pass\n\n"
                 "- [rehome] Re-home `work/People/Ida.md` to `personal/People/Ida.md`? _(from: vault-rehome)_\n")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    assert len(rows) == 3
    ids = [row["id"] for row in rows]
    destinations = {row["id"]: ("work" if row["workspace"] == "personal" else "personal")
                    for row in rows if row["kind"] == "rehome"}
    assert len(destinations) == 2
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "batch_accept", "workspace": "", "targets": ids, "workspaces": ["personal", "work"],
    })
    assert reply.status_code == 200
    assert reply.json()["scope"]["destinations"] == destinations
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": ids},
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 200
    assert all(result["dismissed"] for result in response.json()["results"])
    assert (config.workspace_vault_root("work") / "People/Mo.md").exists()
    assert (config.workspace_vault_root("personal") / "People/Ida.md").exists()


def test_admitted_rehome_signal_changes_before_reresolution(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "row_accept", "workspace": "personal", "targets": [row["id"]],
                            "destination": "work"}, set(config.workspace_names()))
    token = registry.reserve(scope)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    queue = _queue_text(config, "personal")
    before = queue.read_bytes()
    run_admitted = registry._run_admitted
    async def change_after_admission(token, operation):
        async def interleaved():
            note.write_text("---\ntype: person\ntags:\n  - person\n---\n# Mo\n", encoding="utf-8")
            return await operation()
        return await run_admitted(token, interleaved)
    monkeypatch.setattr(registry, "_run_admitted", change_after_admission)
    response = client.post(f"/api/proposals/{row['id']}/accept",
                           headers={"X-Ciao-Proposal-Reservation": token}, json={})
    assert response.status_code == 409
    assert response.json()["id"] == row["id"] and response.json()["no_effect"] is True
    assert note.exists() and not (config.workspace_vault_root("work") / "People/Mo.md").exists()
    assert queue.read_bytes() == before
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.parametrize("batch", [False, True])
def test_rehome_destination_lock_timeout_is_retryable(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import vault_rehome
    from ciao.memory_receipts import QueueLockError
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    def fail(*args, **kwargs):
        raise QueueLockError("destination busy")
    monkeypatch.setattr(vault_rehome, "move_note_between_roots", fail)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert response.status_code == 200 and not response.json()["results"][0]["dismissed"]
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
        assert response.status_code == 409
    assert "busy" in str(response.json()) and queue.read_bytes() == before


def test_rehome_stale_selection_does_not_move(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import proposal_service
    from ciao.memory_proposals import read_decisions

    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    queue = proposal_service._proposals_file(config, "personal")
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    before = note.read_bytes()
    real = proposal_service._rewrite_queue_single

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_single", race)
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert note.read_bytes() == before
    assert not read_decisions(queue)


def test_batch_rehome_stale_selection_does_not_move(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import proposal_service
    from ciao.memory_proposals import read_decisions

    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    queue = proposal_service._proposals_file(config, "personal")
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    before = note.read_bytes()
    real = proposal_service._rewrite_queue_batch

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_batch", race)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
    assert not response.json()["results"][0]["dismissed"]
    assert note.read_bytes() == before
    assert not read_decisions(queue)


@pytest.mark.parametrize("duplicate", [False, True])
def test_batch_rehome_resolves_moved_id_or_refuses_duplicate(tmp_path: Path, monkeypatch, duplicate: bool) -> None:
    from ciao.web import proposal_service

    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    queue = proposal_service._proposals_file(config, "personal")
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    original = queue.read_text(encoding="utf-8")
    if duplicate:
        bullet = next(line for line in original.splitlines() if "[rehome]" in line)
        queue.write_text(original + bullet + "\n", encoding="utf-8")
        row = _accept_kind_row(client, "rehome")
    real = proposal_service._rewrite_queue_batch

    def race(*args, **kwargs):
        current = queue.read_text(encoding="utf-8")
        if duplicate:
            lines = current.splitlines(keepends=True)
            first = next(i for i, line in enumerate(lines) if "[rehome]" in line)
            del lines[first]
            queue.write_text("".join(lines), encoding="utf-8")
        else:
            queue.write_text("Extra header\n" + current, encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_batch", race)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
    assert response.json()["results"][0]["dismissed"] is not duplicate
    assert note.exists() is duplicate


def test_an_ambiguous_rehome_accept_asks_instead_of_guessing(tmp_path: Path) -> None:
    """Tags naming two workspaces is a question only the operator can answer, so
    accepting without a choice must not move somebody's note on a guess."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 400
    assert resp.json()["no_effect"] is True and resp.json()["id"] == row["id"]
    assert "pick one explicitly" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()


def test_an_explicit_choice_moves_the_note(tmp_path: Path) -> None:
    """The whole point: the queue can now carry out the answer it asked for."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert not (config.workspace_vault_root("personal") / "People" / "Mo.md").exists()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()
    # And the row is gone from the queue.
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_choice_the_tags_do_not_name_is_still_honoured(tmp_path: Path) -> None:
    """The tags are a hint; the operator asking is the authority.

    Restricting the choice to tag-named candidates left every queued row on the
    reference install unmovable, because most have no tag naming anywhere.
    """
    config, client, row = _rehome_fixture(tmp_path, ["person"])
    assert row["rehome"]["candidates"] == [], row["rehome"]

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()


def test_an_unregistered_choice_is_still_refused(tmp_path: Path) -> None:
    """Free choice among REGISTERED workspaces, not a free-text path."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "friend", "colleague"])

    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=nowhere")

    assert resp.status_code == 409
    assert "not a registered workspace" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()


def test_a_justified_row_moves_without_a_choice(tmp_path: Path) -> None:
    """A single clean tag signal already names the destination."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    assert row["rehome"]["justified"] is True, row["rehome"]

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    assert (config.workspace_vault_root("work") / "People" / "Mo.md").is_file()


def test_a_failed_move_keeps_the_bullet(tmp_path: Path) -> None:
    """Move-then-dismiss, the same order as a region write: a note silently left
    where it was, with nothing recording that it should not be, is the outcome to
    avoid."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    # Something is already there, so the move refuses rather than merging.
    (config.workspace_vault_root("work") / "People" / "Mo.md").write_text(
        "# A different Mo\n", encoding="utf-8"
    )

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert "already exists" in resp.json()["error"]
    assert (config.workspace_vault_root("personal") / "People" / "Mo.md").is_file()
    assert [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_dismiss_does_not_mutate_the_workspace_guide(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    guide = config.workspace_vault_root("personal") / "AGENTS.md"
    guide.write_text("<!-- ciao:memory:start -->\n## Agent memory\n<!-- ciao:memory:end -->\n", encoding="utf-8")
    before = guide.read_text(encoding="utf-8")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    client.post(f"/api/proposals/{row['id']}/accept")
    assert guide.read_text(encoding="utf-8") == before


def test_batch_dismiss_is_atomic(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    before = queue.read_text(encoding="utf-8")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    profile = next(r for r in rows if r["kind"] == "profile")
    resp = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [profile["id"], "does-not-exist"]})
    assert resp.status_code == 404
    assert queue.read_text(encoding="utf-8") == before


def test_unknown_id_returns_404(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    resp = _client(config).post("/api/proposals/nope/dismiss")
    assert resp.status_code == 404


def test_no_signal_row_exposes_no_justified_destination(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_people_note(config, "personal/People/Christian.md", [])
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [rehome] Re-home `personal/People/Christian.md` to `work/People/Christian.md`? Uncertain: no tag names a workspace. Move it and its links with `ciao vault-rehome --apply` only after tagging it.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    assert row["rehome"]["justified"] is False
    # No tag names a workspace, so there is no evidence-backed candidate; the
    # destination field holds the computed guess and must not be pre-accepted.
    assert row["rehome"]["candidates"] == []


def test_dual_tag_row_exposes_more_than_one_candidate(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_people_note(config, "personal/People/Mo.md", ["ex-colleague", "friend", "person"])
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? Uncertain: tags name both personal and work (ex-colleague, friend). Move it and its links with `ciao vault-rehome --apply` only after tagging it.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    row = _accept_kind_row(client, "rehome")
    assert len(row["rehome"]["candidates"]) > 1
    assert row["rehome"]["candidates"] == ["personal", "work"]


def test_personal_people_user_never_appears(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    # Wave 1 fixed the rehome detector so it never proposes the operator's own
    # identity note. Guard that at the API too: even with a User.md note on
    # disk and a queue that could hold one, the row never surfaces.
    _write_people_note(config, "personal/People/User.md", [])
    resp = _client(config).get("/api/proposals")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert not any("User.md" in r.get("text", "") for r in rows)
    from ciao import vault_rehome
    candidates = vault_rehome.detect_misfiled_people(config.vault_root, workspaces=config.workspace_names())
    assert not any(c.path == "personal/People/User.md" for c in candidates)


def test_region_accept_from_non_primary_workspace_carries_leak_warning(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "work", "## 2026-08-19 curation pass (this pass)\n\n- [memory] A work-origin fact.  _(from: Decisions)_\n")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    work_memory = next(r for r in rows if r["workspace"] == "work" and r["kind"] == "memory")
    assert work_memory["leak_warning"] is True


def test_dismiss_older_than_removes_old_sections(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", (
        "## 2026-07-01 curation pass\n\n"
        "- [memory] An old fact about a forgotten chat.  _(from: Decisions)_\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A recent fact worth keeping.  _(from: Decisions)_\n"
    ))
    client = _client(config)
    resp = client.post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    rows = client.get("/api/proposals").json()["rows"]
    assert len(rows) == 1
    assert rows[0]["text"] == "A recent fact worth keeping."
    # The sweep is a decision per row: the dedupe history must carry the
    # swept text or the nightly curator re-files it from the same transcript.
    from ciao.memory_proposals import dismissed_log_path

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    log_text = dismissed_log_path(queue).read_text(encoding="utf-8")
    assert "An old fact about a forgotten chat." in log_text
    assert "A recent fact worth keeping." not in log_text


def test_sweep_uses_captured_receipt_vault_when_config_changes_at_worker(tmp_path: Path, monkeypatch) -> None:
    from ciao.memory_receipts import journal_path
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    original_vault = config.workspace_vault_root("personal")
    new_vault = tmp_path / "elsewhere"
    new_vault.mkdir()
    original = proposal_service._sweep_queue_file
    seen = []
    def change_config(queue, cutoff, captured_vault, workspace):
        if workspace == "personal":
            config.workspaces["personal"].vault_root = str(new_vault)
            seen.append(captured_vault)
        return original(queue, cutoff, captured_vault, workspace)
    monkeypatch.setattr(proposal_service, "_sweep_queue_file", change_config)
    response = _client(config).post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert response.status_code == 200
    assert seen == [original_vault]
    assert journal_path(original_vault, None).exists()
    assert not journal_path(new_vault, None).exists()


@pytest.mark.parametrize("durable", [False, True])
def test_reserved_sweep_refuses_same_name_changed_vault_before_effect(tmp_path: Path, durable: bool) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    old_queue = _queue_text(config, "personal")
    before = old_queue.read_bytes()
    new_root = tmp_path / "new-vault"
    (new_root / "Workspace").mkdir(parents=True)
    new_queue = new_root / "Workspace/Memory-Proposals.md"
    new_queue.write_text("## 2026-07-01 pass\n\n- [memory] Different. _(from: Decisions)_\n")
    new_before = new_queue.read_bytes()
    client = _client(config)
    client.app.state.proposal_reservations = _test_proposal_registry(tmp_path, durable)
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
    })
    assert reply.status_code == 200
    config.workspaces["personal"].vault_root = str(new_root)
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01",
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 409
    assert old_queue.read_bytes() == before and new_queue.read_bytes() == new_before


@pytest.mark.parametrize("durable", [False, True])
def test_reserved_sweep_stops_when_later_root_changes_mid_sweep(tmp_path: Path, monkeypatch, durable: bool) -> None:
    from ciao.memory_receipts import journal_path
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _default_vault(tmp_path)
    for ws in ("personal", "work"):
        _write_queue(config, ws, f"## 2026-07-01 pass\n\n- [memory] Old {ws}. _(from: Decisions)_\n")
    personal_vault = config.workspace_vault_root("personal")
    work_queue = _queue_text(config, "work")
    before = work_queue.read_bytes()
    new_root = tmp_path / "new-work"
    (new_root / "Workspace").mkdir(parents=True)
    new_queue = new_root / "Workspace/Memory-Proposals.md"
    new_queue.write_text("## 2026-07-01 pass\n\n- [memory] Other. _(from: Decisions)_\n")
    new_before = new_queue.read_bytes()
    client = _client(config)
    client.app.state.proposal_reservations = _test_proposal_registry(tmp_path, durable)
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
    })
    assert reply.status_code == 200
    original = proposal_service._sweep_queue_file
    def change_after_first(queue, cutoff, vault, workspace):
        result = original(queue, cutoff, vault, workspace)
        if workspace == "personal":
            config.workspaces["work"].vault_root = str(new_root)
        return result
    monkeypatch.setattr(proposal_service, "_sweep_queue_file", change_after_first)
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01",
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 503
    assert response.json()["removed"] == 1 and response.json()["queue_settled"] is False
    assert journal_path(personal_vault, None).exists()
    assert not journal_path(new_root, None).exists()
    assert work_queue.read_bytes() == before and new_queue.read_bytes() == new_before


@pytest.mark.parametrize("durable", [False, True])
def test_reserved_sweep_with_unchanged_snapshot_succeeds_and_seals(tmp_path: Path, durable: bool) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation, validate_scope
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    client = _client(config)
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
    })
    assert reply.status_code == 200
    assert str(tmp_path) not in reply.text
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01",
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 200 and response.json()["removed"] == 1
    assert "Old fact." not in _queue_text(config, "personal").read_text()
    assert registry.seal(reply.json()["id"], validate_scope(reply.json()["scope"], set(config.workspace_names())),
                         inspected=True)


def test_reserved_sweep_refuses_replaced_vault_at_same_path(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Original. _(from: Decisions)_\n")
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
    })
    assert reply.status_code == 200
    vault.rename(vault.with_name("original-personal"))
    (vault / "Workspace").mkdir(parents=True)
    queue = vault / "Workspace/Memory-Proposals.md"
    queue.write_text("## 2026-07-01 pass\n\n- [memory] Replacement. _(from: Decisions)_\n")
    before = queue.read_bytes()
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01",
                           headers={"X-Ciao-Proposal-Reservation": reply.json()["id"]})
    assert response.status_code == 409
    assert queue.read_bytes() == before


def test_sweep_reservation_refuses_queue_symlink_outside_vault(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    queue.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text("untouched")
    try:
        queue.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    client = _client(config)
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    reply = client.post("/api/proposals/reservations", json={
        "kind": "sweep", "workspace": "", "targets": ["2026-08-01"],
    })
    assert reply.status_code == 400
    assert outside.read_text() == "untouched"


def test_sweep_decision_lock_failure_reports_landed_rewrite_off_event_loop(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from ciao import proposal_actions
    from ciao.memory_receipts import QueueLockError
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    called = []
    def fail(*args, **kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        called.append(True)
        raise QueueLockError("decision lock busy")
    monkeypatch.setattr(proposal_actions, "record_decision", fail)
    response = _client(config).post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert response.status_code == 503 and called
    payload = response.json()
    assert payload["removed"] == 1 and payload["workspace"] == "personal"
    assert payload["queue_settled"] is False and payload["queue_state"] == "replaced"
    assert payload["decision_confirmed"] is False and "inspect" in payload["error"]
    assert "Old fact." not in queue.read_text()


@pytest.mark.parametrize("reserved", [False, True])
def test_sweep_later_queue_failure_retains_prior_removed_count(tmp_path: Path, monkeypatch, reserved: bool) -> None:
    from ciao.memory_receipts import QueueLockError
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Personal old fact. _(from: Decisions)_\n")
    _write_queue(config, "work", "## 2026-07-01 pass\n\n- [memory] Work old fact. _(from: Decisions)_\n")
    work = config.workspace_vault_root("work") / "Workspace" / "Memory-Proposals.md"
    original = proposal_service._sweep_queue_file
    def fail_work(queue, *args, **kwargs):
        if queue == work:
            raise QueueLockError("queue lock busy")
        return original(queue, *args, **kwargs)
    monkeypatch.setattr(proposal_service, "_sweep_queue_file", fail_work)
    client = _client(config)
    headers = {}
    if reserved:
        from dataclasses import replace
        from ciao.web.proposal_reservations import ReservationRegistry, capture_sweep_snapshot, validate_scope
        registry = ReservationRegistry()
        client.app.state.proposal_reservations = registry
        names = sorted(config.workspace_names())
        scope = replace(validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-08-01"], "workspaces": names},
                                       set(names)), sweep_snapshot=capture_sweep_snapshot(config))
        token = registry.reserve(scope)
        headers = {"X-Ciao-Proposal-Reservation": token}
    response = client.post("/api/proposals/dismiss-older-than?date=2026-08-01", headers=headers)
    assert response.status_code == 503
    payload = response.json()
    assert payload["removed"] == 1 and payload["workspace"] == "work"
    assert payload["queue_settled"] is False and payload["queue_state"] == "unknown"
    assert "Personal old fact." not in (config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md").read_text()
    assert "Work old fact." in work.read_text()
    if reserved:
        assert registry.seal(token, scope, inspected=True)


def test_sweep_receipt_failure_after_rewrite_reports_current_effect(tmp_path: Path, monkeypatch) -> None:
    from contextlib import contextmanager
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    @contextmanager
    def fail_after(*args, **kwargs):
        yield
        raise memory_receipts.QueueReceiptUnavailable("receipt failed")
    monkeypatch.setattr(memory_receipts, "queue_resolution_multi", fail_after)
    response = _client(config).post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert response.status_code == 503
    assert response.json()["removed"] == 1
    assert response.json()["queue_state"] == "replaced"
    assert response.json()["queue_receipt_confirmed"] is False
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert "Old fact." not in queue.read_text()


def test_sweep_real_applied_append_failure_reports_unconfirmed_receipt(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    append = memory_receipts._append
    def fail_applied(journal, receipt):
        if receipt.get("batch") and receipt.get("status") == memory_receipts.APPLIED:
            raise OSError("applied receipt unavailable")
        return append(journal, receipt)
    monkeypatch.setattr(memory_receipts, "_append", fail_applied)
    response = _client(config).post("/api/proposals/dismiss-older-than?date=2026-08-01")
    assert response.status_code == 503
    assert response.json()["removed"] == 1
    assert response.json()["queue_state"] == "replaced"
    assert response.json()["queue_receipt_confirmed"] is False
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert "Old fact." not in queue.read_text()


@pytest.mark.asyncio
async def test_sweep_decision_wait_does_not_block_heartbeat(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import threading
    from starlette.requests import Request
    from ciao import proposal_actions
    from ciao.memory_receipts import QueueLockError
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-07-01 pass\n\n- [memory] Old fact. _(from: Decisions)_\n")
    entered, release, timed_out = threading.Event(), threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(2):
            timed_out.set()
        raise QueueLockError("decision lock busy")
    monkeypatch.setattr(proposal_actions, "record_decision", blocked)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "query_string": b"date=2026-08-01", "app": _client(config).app})
    task = asyncio.create_task(dismiss_older_than(request))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # This coroutine must run before the blocked worker is released.
        assert not timed_out.is_set()
    finally:
        release.set()
    response = await task
    assert response.status_code == 503
    import json
    payload = json.loads(response.body)
    assert payload["removed"] == 1 and payload["queue_state"] == "replaced"
    assert payload["decision_confirmed"] is False


def test_batch_dismiss_records_every_row_in_the_dedupe_history(tmp_path: Path) -> None:
    """Multi-select dismiss persists each rejected row's text, like the single route."""
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    targets = [r for r in rows if r["kind"] in {"memory", "profile"}]
    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [r["id"] for r in targets]},
    )
    assert resp.status_code == 200

    from ciao.memory_proposals import append_proposals, dismissed_log_path
    from ciao.memory_proposals import MemoryProposal

    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    log_text = dismissed_log_path(queue).read_text(encoding="utf-8")
    for row in targets:
        assert row["text"] in log_text
        assert (
            append_proposals(
                [MemoryProposal(target=row["kind"], text=row["text"], source_section="curation")],
                config.workspace_vault_root("personal"),
            )
            is None
        )


def test_the_real_app_serves_every_documented_proposal_route() -> None:
    """The routes must be reachable on the app the server actually builds.

    The tests above mount the handlers on a hand-built Starlette app, which
    proves the handlers work but not that anything serves them. Registration
    lives in ciao/web/app.py, so a handler can be complete, documented, and
    still unreachable in production with the whole suite green. This asserts
    the real route table, and pins it to the paths PWA_API.md advertises.
    """
    import re

    from ciao.web import app as app_module

    registered = set(re.findall(r'Route\("(/api/proposals[^"]*)"', pathlib.Path(app_module.__file__).read_text(encoding="utf-8")))

    expected = {
        "/api/proposals",
        "/api/proposals/history",
        "/api/proposals/handover",
        "/api/proposals/reservations",
        "/api/proposals/reservations/seal",
        "/api/proposals/batch",
        "/api/proposals/dismiss-older-than",
        "/api/proposals/{id}/preview",
        "/api/proposals/{id}/implement",
        "/api/proposals/{id}/{action}",
    }
    assert registered == expected, f"app.py route table drifted: {registered}"
    # Every registered route must also appear in PWA_API.md, or the drift above
    # is only drift in one direction. Substring, not set equality: the prose
    # around each route names its path in curl examples too.
    api_doc = pathlib.Path("PWA_API.md").read_text(encoding="utf-8")
    missing = {path for path in expected if path not in api_doc}
    assert not missing, f"PWA_API.md does not document: {missing}"

    # Every concrete path the docs show must be served by one of the registered
    # patterns. `$ID/accept` in a curl recipe is the {id}/{action} route.
    for path in ("/api/proposals/batch", "/api/proposals/dismiss-older-than"):
        assert path in api_doc, f"{path} is registered but undocumented"


# -- Accept has to actually write the fact -----------------------------------
#
# It used to remove the bullet and return a descriptor saying what SHOULD happen,
# matching the MCP flow where the agent edits and then dismisses. In a UI where a
# person clicks Accept, that meant the fact left the queue and landed nowhere:
# one click from losing any of the 109 queued on the reference install.


def _region_entries(config, workspace: str, region: str) -> list[str]:
    """A region's entries with their learned-at stamps removed.

    Accepting a fact appends a learned-at `[YYYY-MM-DD]` stamp — the stamp the
    aging audit reads to surface facts that have not been re-verified.
    Stripping it here is the same normalisation the production dedupe does, and
    keeps these assertions from depending on today's date.
    """
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region

    guide = Path(config.agent_root(workspace)) / "AGENTS.md"
    entries, _diags = read_region(guide, region)
    return [strip_learned_stamp(entry) for entry in entries]


def test_accept_writes_the_fact_into_the_region(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert row["text"] in _region_entries(config, row["workspace"], "memory")
    # And the bullet is gone, in that order.
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


def test_accepting_a_people_row_writes_a_typed_note(tmp_path: Path) -> None:
    """A `[people]` accept files the note the category registry names, typed.

    The writer used to emit `tags: [person]` and no `type:` at all, so the note
    it created was one `ciao vault-lint` reports as untyped. Destination and
    content both come from the registry now, and this pins the whole accept:
    the row leaves the queue and a note with `type: person` is in `People/`.
    """
    config = _default_vault(tmp_path)
    _write_queue(
        config,
        "personal",
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [people Mo] Leads the pilot.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "people")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["action"] == "write_people_note"
    assert result["destination"] == "People/Mo.md"

    note = config.workspace_vault_root(row["workspace"]) / "People" / "Mo.md"
    text = note.read_text(encoding="utf-8")
    assert "type: person" in text
    assert "tags: [person]" in text
    assert re.search(r"^updated: \d{4}-\d{2}-\d{2}$", text, re.MULTILINE), text
    assert row["text"] in text
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


@pytest.mark.parametrize("batch", [False, True])
def test_fold_preparation_refusal_explicitly_reports_no_effect(tmp_path: Path, monkeypatch, batch: bool) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## 2026-08-19 pass\n\n- [people Mo] Leads the pilot. _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, "people")
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    before = queue.read_bytes()
    async def refuse(*args, **kwargs):
        return None, proposal_service.AcceptOutcome(ok=False, error="fold refused")
    monkeypatch.setattr(proposal_service, "_prepare_proposal_fold", refuse)
    response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
                if batch else client.post(f"/api/proposals/{row['id']}/accept"))
    result = response.json()["results"][0] if batch else response.json()
    assert response.status_code == (200 if batch else 409)
    assert result["id"] == row["id"] and result["no_effect"] is True
    assert queue.read_bytes() == before
    assert not (config.workspace_vault_root("personal") / "People" / "Mo.md").exists()


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
def test_fold_stale_selection_during_model_does_not_write(
    tmp_path: Path, monkeypatch, kind: str, batch: bool
) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Leads the pilot.  _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, kind)
    queue = proposal_service._proposals_file(config, "personal")
    before = note.read_bytes()
    queue_before = queue.read_bytes()
    replacement = queue.read_text(encoding="utf-8").replace("Decisions", "Other source")

    async def race(*args, **kwargs):
        assert note.read_bytes() == before
        assert queue.read_bytes() == queue_before
        queue.write_text(replacement, encoding="utf-8")
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert note.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == replacement
    assert not read_decisions(queue)


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_fold_commit_checks_destination_revision(
    tmp_path: Path, monkeypatch, kind: str, batch: bool, changed: bool
) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Leads the pilot.  _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, kind)
    queue = proposal_service._proposals_file(config, "personal")
    queue_before = queue.read_bytes()
    concurrent = "# Changed by another writer\n"

    async def model(*args, **kwargs):
        if changed:
            note.write_text(concurrent, encoding="utf-8")
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        result = response.json()["results"][0]
        assert result["dismissed"] is not changed
        if changed:
            assert result["conflict"]
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
        assert response.status_code == (409 if changed else 200)
        if changed:
            assert response.json()["conflict"]
    if changed:
        assert note.read_text(encoding="utf-8") == concurrent
        assert queue.read_bytes() == queue_before
        assert not read_decisions(queue)
    else:
        assert "Leads the pilot." in note.read_text(encoding="utf-8")
        assert row["id"] not in {item["id"] for item in client.get("/api/proposals").json()["rows"]}
        assert len(read_decisions(queue)) == 1


@pytest.mark.parametrize("kind,existing", [("people", True), ("people", False), ("project", True)])
def test_batch_folds_same_destination_sequentially(tmp_path: Path, monkeypatch, kind: str, existing: bool) -> None:
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Alpha detail. _(from: A)_\n"
                                       f"- [{tag}] Beta detail. _(from: B)_\n")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == kind]
    calls: list[str] = []
    async def model(*args, **kwargs):
        fact = "Alpha detail." if len(calls) == 0 else "Beta detail."
        calls.append(fact)
        return json.dumps({"action": "add", "section": "Notes", "text": f"- {fact}"})
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    assert response.status_code == 200
    assert all(result["dismissed"] for result in response.json()["results"])
    assert "Alpha detail." in note.read_text() and "Beta detail." in note.read_text()
    queue = proposal_service._proposals_file(config, "personal")
    assert {r["proposal_id"] for r in read_decisions(queue)} == {r["id"] for r in rows}


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("replay", [False, True])
def test_fold_decision_links_destination_receipt(tmp_path: Path, monkeypatch, kind: str, batch: bool, replay: bool) -> None:
    from ciao import memory_receipts, memory_proposals, proposal_actions
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Leads the pilot. _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, kind)
    queue = proposal_service._proposals_file(config, "personal")
    journal = memory_receipts.journal_path(vault, None)
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    original = proposal_actions.record_decision
    seen = []
    def check(path, **kwargs):
        receipts = memory_receipts.read_receipts(journal)
        notes = [r for r in receipts if r.get("kind") == "note_apply" and r.get("status") == "applied"]
        assert len(notes) == 1
        assert kwargs["receipt_id"] == notes[0]["id"] and kwargs["receipt_id"]
        if replay:
            memory_receipts.recover_pending(journal=journal, config=config)
            recovered = memory_proposals.read_decisions(queue)
            assert len(recovered) == 1 and recovered[0]["receipt_id"] == notes[0]["id"]
        seen.append(True)
        return original(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", check)
    response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}) if batch
                else client.post(f"/api/proposals/{row['id']}/accept"))
    assert response.status_code == 200 and seen
    decisions = memory_proposals.read_decisions(queue)
    assert len(decisions) == 1 and decisions[0]["receipt_id"]


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("operation", ["add", "update"])
def test_route_fold_preserves_crlf_undo_and_strips_verification(tmp_path: Path, monkeypatch, kind: str, batch: bool, operation: str) -> None:
    from ciao import memory_proposals, memory_receipts
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    before = b"## Notes\r\n- Lives in Porto [verified: 2024-03-01]\r\n- Likes tea.\r\n"
    note.write_bytes(before)
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Lives in Lisbon. _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, kind)
    async def model(*args, **kwargs):
        if operation == "update":
            return '{"action":"update","index":1,"text":"- Lives in Lisbon [verified: 2024-03-01]"}'
        return '{"action":"add","section":"Notes","text":"- Lives in Lisbon [verified: 2024-03-01]"}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}) if batch
                else client.post(f"/api/proposals/{row['id']}/accept"))
    assert response.status_code == 200
    after = note.read_bytes()
    assert b"- Lives in Lisbon\r\n" in after
    assert b"Lives in Lisbon [verified:" not in after
    assert b"- Likes tea.\r\n" in after
    decisions = memory_proposals.read_decisions(proposal_service._proposals_file(config, "personal"))
    assert len(decisions) == 1 and decisions[0]["receipt_id"]
    undone = memory_receipts.undo_receipt(decisions[0]["receipt_id"], vault_root=vault)
    assert undone["status"] == memory_receipts.UNDONE
    assert note.read_bytes() == before


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("mutant", [False, True])
def test_route_unchanged_fold_cannot_redate_verified_entry(tmp_path: Path, monkeypatch, kind: str, batch: bool, mutant: bool) -> None:
    from ciao import project_doc_update
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    before = b"## Notes\r\n- Lives in Porto [verified: 2024-03-01]\r\n- Likes tea.\r\n"
    note.write_bytes(before)
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] Lives in Porto. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    queue_before = queue.read_bytes()
    client = _client(config)
    row = _accept_kind_row(client, kind)
    async def model(*args, **kwargs):
        return '{"action":"update","index":1,"text":"- Lives in Porto [verified: 2026-11-14]"}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    if mutant:
        monkeypatch.setattr(project_doc_update, "_sanitize_fold_stamp",
                            lambda text, entry: project_doc_update._strip_opening_claim(text))
    def verify():
        response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}) if batch
                    else client.post(f"/api/proposals/{row['id']}/accept"))
        assert note.read_bytes() == before
        assert queue.read_bytes() == queue_before
        if batch:
            assert response.status_code == 200 and not response.json()["results"][0]["dismissed"]
        else:
            assert response.status_code == 409
    if mutant:
        with pytest.raises(AssertionError):
            verify()
    else:
        verify()


@pytest.mark.parametrize("batch", [False, True])
def test_fold_destination_lock_timeout_is_retryable(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import project_doc_update
    from ciao.memory_receipts import QueueLockError
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n- [people Mo] Fact. _(from: A)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    client = _client(config)
    row = _accept_kind_row(client, "people")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Fact."}'
    def fail(*args, **kwargs):
        raise QueueLockError("destination busy")
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    monkeypatch.setattr(project_doc_update, "commit_proposal_fold", fail)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert response.status_code == 200 and not response.json()["results"][0]["dismissed"]
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
        assert response.status_code == 409
    assert "busy" in str(response.json()) and queue.read_bytes() == before


def test_batch_fold_lock_timeout_preserves_prior_success(tmp_path: Path, monkeypatch) -> None:
    from ciao import project_doc_update
    from ciao.memory_receipts import QueueLockError
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n- [people Mo] First. _(from: A)_\n"
                                       "- [people Mo] Second. _(from: B)_\n")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == "people"]
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Accepted fact."}'
    real = project_doc_update.commit_proposal_fold
    count = 0
    def commit(plan):
        nonlocal count
        count += 1
        if count == 2:
            raise QueueLockError("destination busy")
        return real(plan)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    monkeypatch.setattr(project_doc_update, "commit_proposal_fold", commit)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    results = response.json()["results"]
    assert response.status_code == 200 and sum(bool(row["dismissed"]) for row in results) == 1
    assert any(not row["dismissed"] and row.get("conflict") and "busy" in row.get("error", "") for row in results)
    assert "Accepted fact." in note.read_text()


@pytest.mark.parametrize("race", ["destination", "queue"])
def test_sequential_batch_second_fold_refuses_external_change(tmp_path: Path, monkeypatch, race: str) -> None:
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n- [people Mo] First. _(from: A)_\n"
                                       "- [people Mo] Second. _(from: B)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == "people"]
    calls = 0
    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if race == "destination":
                note.write_text(note.read_text() + "\nExternal edit.\n", encoding="utf-8")
            else:
                queue.write_text(queue.read_text() + "\nExternal queue edit.\n", encoding="utf-8")
        return json.dumps({"action": "add", "section": "Notes", "text": f"- Landed {calls}."})
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    results = response.json()["results"]
    assert sum(bool(row["dismissed"]) for row in results) == 1
    assert "Landed 1." in note.read_text() and "Landed 2." not in note.read_text()
    assert ("External edit." in note.read_text()) == (race == "destination")
    assert ("External queue edit." in queue.read_text()) == (race == "queue")
    assert len(read_decisions(queue)) == 1


def test_sequential_batch_resolves_both_duplicate_ordinals(tmp_path: Path, monkeypatch) -> None:
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    bullet = "- [people Mo] Leads the pilot. _(from: Decisions)_\n"
    _write_queue(config, "personal", "## pass\n\n" + bullet * 2)
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == "people"]
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    assert all(row["dismissed"] for row in response.json()["results"])
    queue = proposal_service._proposals_file(config, "personal")
    assert "Leads the pilot." not in queue.read_text()
    assert {row["proposal_id"] for row in read_decisions(queue)} == {row["id"] for row in rows}


@pytest.mark.parametrize("kind,existing", [("people", True), ("people", False), ("project", True)])
def test_mixed_batch_sequential_folds_and_memory(tmp_path: Path, monkeypatch, kind: str, existing: bool) -> None:
    from ciao import memory_receipts
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    if existing:
        note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    _write_queue(config, "personal", f"## pass\n\n- [{tag}] First fact. _(from: A)_\n"
                                       "- [memory] Unique middle fact. _(from: B)_\n"
                                       f"- [{tag}] Second fact. _(from: C)_\n")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] in {kind, "memory"}]
    calls = 0
    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        fact = "Second fact." if existing and calls == 1 else "First fact."
        return json.dumps({"action": "add", "section": "Notes", "text": f"- {fact}"})
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    assert all(row["dismissed"] for row in response.json()["results"])
    assert "First fact." in note.read_text() and "Second fact." in note.read_text()
    queue = proposal_service._proposals_file(config, "personal")
    assert {row["proposal_id"] for row in read_decisions(queue)} == {row["id"] for row in rows}
    receipts = memory_receipts.read_receipts(memory_receipts.journal_path(config.workspace_vault_root("personal"), None))
    assert {proof["proposal_id"] for receipt in receipts if receipt.get("batch")
            for proof in receipt.get("selected_proposals", [])} == {row["id"] for row in rows}
    assert "Unique middle fact." in (config.agent_root("personal") / "AGENTS.md").read_text()


@pytest.mark.parametrize("race", ["destination", "queue"])
def test_mixed_batch_external_change_after_memory_keeps_prior_effects(tmp_path: Path, monkeypatch, race: str) -> None:
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n- [people Mo] First. _(from: A)_\n"
                                       "- [memory] Middle unique fact. _(from: B)_\n"
                                       "- [people Mo] Second. _(from: C)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] in {"people", "memory"}]
    calls = 0
    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if race == "destination":
                note.write_text(note.read_text() + "\nExternal change.\n", encoding="utf-8")
            else:
                queue.write_text(queue.read_text() + "\nExternal queue change.\n", encoding="utf-8")
        return json.dumps({"action": "add", "section": "Notes", "text": f"- Fold {calls}."})
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"] for row in rows]})
    assert sum(bool(row["dismissed"]) for row in response.json()["results"]) == 2
    assert "Fold 1." in note.read_text() and "Fold 2." not in note.read_text()
    assert "Middle unique fact." in (config.agent_root("personal") / "AGENTS.md").read_text()
    assert ("External change." in note.read_text()) == (race == "destination")
    assert ("External queue change." in queue.read_text()) == (race == "queue")
    assert len(read_decisions(queue)) == 2


@pytest.mark.parametrize("kind", ["people", "project"])
@pytest.mark.parametrize("batch", [False, True])
def test_fold_rejects_duplicate_ordinal_shift(tmp_path: Path, monkeypatch, kind: str, batch: bool) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / ("People/Mo.md" if kind == "people" else "Projects/Test.md")
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    tag = "people Mo" if kind == "people" else f"project {note}"
    bullet = f"- [{tag}] Leads the pilot.  _(from: Decisions)_\n"
    _write_queue(config, "personal", "## pass\n\n" + bullet * 2)
    client = _client(config)
    row = _accept_kind_row(client, kind)
    queue = proposal_service._proposals_file(config, "personal")
    before = note.read_bytes()
    replacement = "## pass\n\n" + bullet

    async def model(*args, **kwargs):
        queue.write_text(replacement, encoding="utf-8")
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert note.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == replacement
    assert not read_decisions(queue)


@pytest.mark.parametrize("ordinal", [0, 1])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action", ["accept", "dismiss"])
def test_unchanged_duplicate_selected_ordinal_resolves(tmp_path: Path, monkeypatch, ordinal: int,
                                                       batch: bool, action: str) -> None:
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Note\n\n## Notes\n- Existing.\n", encoding="utf-8")
    bullet = "- [people Mo] Leads the pilot.  _(from: Decisions)_\n"
    _write_queue(config, "personal", "## pass\n\n" + bullet * 2)
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == "people"]
    selected = rows[ordinal]
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": action, "ids": [selected["id"]]})
        assert response.status_code == 200 and response.json()["results"][0]["dismissed"]
    else:
        response = client.post(f"/api/proposals/{selected['id']}/{action}")
        assert response.status_code == 200
    queue = proposal_service._proposals_file(config, "personal")
    assert queue.read_text().count(bullet.strip()) == 1
    assert [row["proposal_id"] for row in read_decisions(queue)] == [selected["id"]]


@pytest.mark.parametrize("batch", [False, True])
def test_new_person_appearing_before_commit_is_not_overwritten(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [people Mo] Leads the pilot.  _(from: Decisions)_\n")
    client = _client(config)
    row = _accept_kind_row(client, "people")
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    note = config.workspace_vault_root("personal") / "People" / "Mo.md"
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("# Another writer\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
        assert response.json()["results"][0]["conflict"]
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
        assert response.status_code == 409 and response.json()["conflict"]
    assert note.read_text(encoding="utf-8") == "# Another writer\n"
    assert queue.read_bytes() == before
    assert not read_decisions(queue)


def test_accepting_a_people_row_honours_an_edited_category_folder(
    tmp_path: Path,
) -> None:
    """The accept files the note in the folder the owner's edit names.

    The registry is read at the agent vault root — the one holding
    `entity-types.yaml` beside `VOCABULARY.md`, the file
    `PATCH /api/memory/entity-types` writes — while the note is written under
    the workspace's notes root. Reading the category from the notes root instead
    is what made an owner's `person.folder: Humans` invisible here: the accept
    went on writing `People/Mo.md` beside the very file that renamed it, on the
    default pre-re-rooting install where those two roots differ. This is that
    install: `agent_vault_root` is the shared vault, `workspace_vault_root` the
    `personal` subtree under it.
    """
    config = _default_vault(tmp_path)
    agent_root = config.agent_vault_root("personal")
    assert agent_root != config.workspace_vault_root("personal")
    (agent_root / "entity-types.yaml").write_text(
        "- id: person\n  folder: Humans\n", encoding="utf-8"
    )
    _write_queue(
        config,
        "personal",
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [people Mo] Leads the pilot.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "people")

    # The review card resolves the same destination, or the card would show a
    # folder the accept does not write to.
    preview = proposal_service._people_preview(config, row, str(row.get("text") or ""))
    assert preview["destination"] == "Humans/Mo.md", preview
    assert preview["can_accept"] is True, preview

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["action"] == "write_people_note"
    assert result["destination"] == "Humans/Mo.md", result

    notes_root = config.workspace_vault_root(row["workspace"])
    assert (notes_root / "Humans" / "Mo.md").is_file()
    assert not (notes_root / "People").exists(), "the stock folder, not the owner's"


def test_accept_persists_the_text_against_refiling(tmp_path: Path) -> None:
    """An accept must land in the same decision history a dismissal does.

    Append-time dedupe consults the live queue and the decision sidecar —
    never the promoted destination — so an accepted fact that leaves no
    trace in the sidecar is re-read by the nightly curator from the same
    transcript and queued again as if it were new.
    """
    from ciao.memory_proposals import MemoryProposal, append_proposals, dismissed_log_path

    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200

    queue = config.workspace_vault_root(row["workspace"]) / "Workspace" / "Memory-Proposals.md"
    assert row["text"] in dismissed_log_path(queue).read_text(encoding="utf-8")

    # The nightly pass re-reads the same transcript: refused as a dupe.
    assert (
        append_proposals(
            [MemoryProposal(target="memory", text=row["text"], source_section="curation")],
            config.workspace_vault_root(row["workspace"]),
        )
        is None
    )


def test_a_failed_write_keeps_the_bullet(tmp_path: Path) -> None:
    """Write-then-dismiss, never the reverse: the reverse loses the fact.

    A guide whose region markers are duplicated is the realistic cause: the write
    cannot tell which copy to edit, so it refuses and the bullet has to survive.
    """
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert "id" in resp.json()
    # Still queued, so the operator can fix the cap and retry.
    assert row["id"] in {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    assert row["text"] not in _region_entries(config, row["workspace"], "memory")


def test_a_batch_accept_writes_every_fact_it_dismisses(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] in {"memory", "profile", "user"}
    ]
    assert len(rows) >= 2

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    assert resp.status_code == 200
    assert all(r["promoted"] for r in resp.json()["results"])
    for row in rows:
        region = "memory" if row["kind"] == "memory" else "profile"
        assert row["text"] in _region_entries(config, row["workspace"], region), row["text"]


def test_a_batch_keeps_the_bullets_it_could_not_write(tmp_path: Path) -> None:
    """A batch that removed the lines first would lose every unwritable fact at once."""
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] in {"memory", "profile", "user"}
    ]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    results = resp.json()["results"]
    assert all(r["promoted"] is False and r["dismissed"] is False for r in results)
    still = {r["id"] for r in client.get("/api/proposals").json()["rows"]}
    for row in rows:
        assert row["id"] in still, row["id"]


def test_dismiss_still_writes_nothing(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    client.post(f"/api/proposals/{row['id']}/dismiss")

    assert row["text"] not in _region_entries(config, row["workspace"], "memory")
    assert row["id"] not in {r["id"] for r in client.get("/api/proposals").json()["rows"]}


# -- a skill proposal is a row you can actually act on -------------------------


def _write_skill_proposal(
    config: CiaoConfig,
    workspace: str,
    name: str,
    *,
    problem: str = "Repeated fetch failures.",
    change: str = "Add a defuddle fallback.",
) -> Path:
    """A versioned record, as the evolution pass writes it."""
    stored = skill_proposals.upsert_proposal(
        config,
        skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id(workspace, name),
            workspace=workspace,
            skill=name,
            canonical_path=f"/agent/skills/{name}/SKILL.md",
            reviewed_revision="a" * 64,
            title=f"Skill reflection: {name}",
            problem=problem,
            change=change,
            rationale="It handles blocked pages.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
        ),
    )
    return skill_proposals.proposal_path(config, workspace, stored.skill)


def test_a_skill_row_carries_the_record_not_the_filename(tmp_path: Path) -> None:
    """The listing used to hand the UI a bare stem, so a review card could say
    nothing about the proposal it was asking about: not what was noticed, not
    what was proposed, not which sessions it came from."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    rows, by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["id"] in by_id
    assert skill[0]["skill"] == "2026-08-09-defuddle"
    assert skill[0]["title"] == "Skill reflection: 2026-08-09-defuddle"
    assert skill[0]["problem"] == "Repeated fetch failures."
    assert skill[0]["change"] == "Add a defuddle fallback."
    assert skill[0]["rationale"] == "It handles blocked pages."
    assert [item["chat_id"] for item in skill[0]["sources"]] == ["sess-a1"]
    assert skill[0]["lifecycle"] == skill_proposals.PENDING
    # Empty for a proposal filed before origins existed. An empty list is not
    # "this learning is dealt with": it is "this record links nothing", which is
    # why no learning behind it can be retired by settling the row.
    assert skill[0]["origins"] == []


def test_a_skill_row_carries_its_learning_links(tmp_path: Path) -> None:
    """Which learnings a finding came from, and what became of each one. The row
    is the only place a reviewer can see that accepting it will retire a lesson
    — and that it will not, until the sibling findings are answered too."""
    config = _config(tmp_path)
    skill_proposals.upsert_proposal(
        config,
        skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id("personal", "notes"),
            workspace="personal",
            skill="notes",
            canonical_path="/agent/skills/notes/SKILL.md",
            reviewed_revision="a" * 64,
            title="Skill reflection: notes",
            problem="The user corrected the note type twice.",
            change="Read the Categories block before a type.",
            rationale="It repeated.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="sess-a1",
                    archive="2026-08-09T10:00:00Z",
                    turn="",
                    excerpt="no, that's a person",
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
            origins=(
                skill_proposals.SkillOrigin(
                    workspace="personal",
                    learning_id="5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f",
                    source_revision="c" * 64,
                    finding="read the Categories block first",
                    summary="Add the Categories step.",
                ),
            ),
        ),
    )

    rows, _by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert skill[0]["origins"] == [
        {
            "workspace": "personal",
            "learning_id": "5d6b0a1e-6f4a-5b1c-9d2e-3a4b5c6d7e8f",
            "source_revision": "c" * 64,
            "finding": "read the Categories block first",
            "summary": "Add the Categories step.",
            "state": skill_proposals.ORIGIN_PENDING,
            "verification": "",
        }
    ]


def test_a_legacy_skill_proposal_file_is_still_listed(tmp_path: Path) -> None:
    """The pre-#683 loose files have no versioned frontmatter. They must keep
    listing — a proposal a user already has is not something an upgrade gets to
    hide from the queue it was filed in."""
    config = _config(tmp_path)
    path = (
        config.workspace_vault_root("personal")
        / "Workspace" / "Skill-Proposals" / "2026-05-20-defuddle.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Skill reflection: defuddle\n\nA loose proposal.\n", encoding="utf-8")

    rows, _by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["text"] == "2026-05-20-defuddle"
    assert skill[0]["title"] == "Skill reflection: defuddle"


def test_a_skill_row_is_resolvable_by_id(tmp_path: Path) -> None:
    """It was listed but never registered, so the dismiss button the UI renders
    for every skill row answered 404 from BOTH endpoints. 49 dead buttons on a
    real vault."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    rows, by_id = _scan_proposal_rows(config)

    skill = [r for r in rows if r["kind"] == "skill"]
    assert len(skill) == 1
    assert skill[0]["id"] in by_id


def test_dismissing_a_skill_row_records_a_decision_and_leaves_the_queue(
    tmp_path: Path,
) -> None:
    """A reviewed proposal is a resolved decision — implemented or disregarded.
    It used to be resolved by deleting the file, which destroyed the only record
    that anyone had looked at it, so the next pass that saw the same evidence
    filed the same suggestion again as a new file. Now the decision is recorded
    and the record stays."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200, response.json()
    assert [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"] == []
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert record.problem == "Repeated fetch failures."


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_skill_dismiss_waits_for_real_decision_lock_off_event_loop(tmp_path: Path, monkeypatch, batch: bool) -> None:
    import asyncio
    import httpx
    from ciao.memory_receipts import queue_lock
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    locked, entered, release, timed_out = (threading.Event() for _ in range(4))
    def hold():
        with queue_lock(queue):
            locked.set()
            if not release.wait(2):
                timed_out.set()
    holder = threading.Thread(target=hold)
    holder.start()
    assert await asyncio.to_thread(locked.wait, 3)
    record = skill_proposals.record_dismissal
    def announce(*args, **kwargs):
        entered.set()
        return record(*args, **kwargs)
    monkeypatch.setattr(skill_proposals, "record_dismissal", announce)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://testserver") as ac:
            request = (ac.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
                       if batch else ac.post(f"/api/proposals/{row['id']}/dismiss"))
            task = asyncio.create_task(request)
            assert await asyncio.to_thread(entered.wait, 3)
            assert not timed_out.is_set()
            release.set()
            response = await task
            assert response.status_code == 200
    finally:
        release.set()
        await asyncio.to_thread(holder.join)
    assert skill_proposals.parse_proposal(source, "personal").lifecycle == skill_proposals.DISMISSED


@pytest.mark.parametrize("batch", [False, True])
def test_skill_dismiss_failed_record_rewrite_is_not_reported_settled(tmp_path: Path, monkeypatch, batch: bool) -> None:
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")
    def fail(*args, **kwargs):
        raise OSError("skill record write failed")
    monkeypatch.setattr(skill_proposals, "write_queue_atomically", fail)
    response = (client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
                if batch else client.post(f"/api/proposals/{row['id']}/dismiss"))
    result = response.json()["results"][0] if batch else response.json()
    assert response.status_code == (200 if batch else 409)
    assert result.get("dismissed") is not True and "error" in result
    assert skill_proposals.parse_proposal(source, "personal").lifecycle == skill_proposals.PENDING


def test_a_dismissed_skill_proposal_is_a_decision_the_history_shows(
    tmp_path: Path,
) -> None:
    """The decision is keyed by a synthetic ``skill:<name>`` text so a skill row
    can never be read as a memory fact that happens to share its wording, and so
    the pass that must honour it can find it after the file is gone."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    client.post(f"/api/proposals/{row['id']}/dismiss")

    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]
    assert history[0]["kind"] == "skill"
    assert history[0]["action"] == "dismissed"
    assert history[0]["via"] == "pwa"


def test_the_same_evidence_does_not_reappear_after_a_dismissal(tmp_path: Path) -> None:
    """The whole point of recording the decision. The pass merges into the
    settled record and it stays settled, so the queue does not re-ask a question
    the operator already answered — and the record still holds the evidence that
    was gathered."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")
    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    # A later pass sees the same session again, from a file that is gone.
    source.unlink()
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert [
        r for r in _client(config).get("/api/proposals").json()["rows"]
        if r["kind"] == "skill"
    ] == []


def test_dismissing_a_skill_row_twice_is_not_an_error(tmp_path: Path) -> None:
    """Two tabs racing on one row, or a retry after a dropped response. The
    second has nothing left to decide, which is not a failure."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200
    # The row is gone from the listing, so a retry is an unknown id — the same
    # answer every other settled row gives, and the reason settlement is
    # recorded rather than the row being kept around to be dismissed again.
    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 404


def test_accepting_a_skill_row_is_refused_with_a_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/accept")

    assert response.status_code == 400
    assert "nothing to promote" in response.json()["error"]
    assert source.is_file()   # untouched


def test_a_batch_dismiss_covers_skill_rows_and_bullets_together(tmp_path: Path) -> None:
    """The batch groups by queue file and drops bullet lines; a skill row has no
    line in any queue, so it has to be handled before that grouping."""
    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    ids = [r["id"] for r in rows]
    assert len(ids) == 2

    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": ids})

    assert response.status_code == 200, response.json()
    assert all(r["dismissed"] for r in response.json()["results"]), response.json()
    # Both rows are settled: the bullet leaves the queue file, the skill record
    # flips its lifecycle. Neither is deleted.
    assert "Remember the thing" not in _queue_text(config, "personal").read_text(
        encoding="utf-8"
    )
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    assert client.get("/api/proposals").json()["rows"] == []


def test_two_runs_of_one_skill_are_one_row_and_one_decision(tmp_path: Path) -> None:
    """Two dated proposals for the same skill used to be two files and two
    dismissals — the same finding, answered twice, with the second run's file
    left to be discovered and re-decided. Keyed by the skill, it is one record,
    so the second run merges into the first and one dismiss settles both."""
    config = _config(tmp_path)
    client = _client(config)
    first = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    # A later run writes to the same record: the same skill, new evidence.
    second = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")

    assert first == second
    rows = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"]
    assert len(rows) == 1

    assert client.post(f"/api/proposals/{rows[0]['id']}/dismiss").status_code == 200
    assert [
        r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill"
    ] == []
    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]


# -- accepting a skill proposal into a chat ----------------------------------
#
# The association used to be browser-local, so a reload or a second device
# started a second implementation and nothing recorded that either had run.


class _FakeProject:
    def __init__(self, project_id: str, name: str, workspace: str) -> None:
        self.project_id = project_id
        self.name = name
        self.workspace = workspace


class _FakeChat:
    def __init__(self, chat_id: str, project_id: str, title: str) -> None:
        self.chat_id = chat_id
        self.project_id = project_id
        self.title = title
        self.archived = False


class _FakePcm:
    """Just enough of the chat manager for the accept route."""

    def __init__(self, *, general_in: tuple[str, ...] = ("personal", "work")) -> None:
        self._projects = [
            _FakeProject(f"proj-{ws}", "General", ws) for ws in general_in
        ]
        self._chats: dict[str, _FakeChat] = {}
        self.prompts: list[tuple[str, str]] = []
        self.helpers: list[dict | None] = []

    def list_projects(self, workspace: str | None = None) -> list[_FakeProject]:
        if workspace is None:
            return list(self._projects)
        return [p for p in self._projects if p.workspace == workspace]

    def create_project(self, name: str, workspace: str) -> _FakeProject:
        project = _FakeProject(f"proj-{workspace}-new", name, workspace)
        self._projects.append(project)
        return project

    def create_chat(
        self, project_id: str, title: str = "New Chat", helper: dict | None = None
    ) -> _FakeChat:
        chat = _FakeChat(f"chat-{len(self._chats) + 1}", project_id, title)
        self._chats[chat.chat_id] = chat
        self.helpers.append(helper)
        return chat

    def get_chat(self, chat_id: str) -> _FakeChat | None:
        return self._chats.get(chat_id)

    def start_stream(self, chat_id: str, prompt: str) -> None:
        self.prompts.append((chat_id, prompt))


def _skill_row(client: TestClient) -> dict[str, Any]:
    return next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")


def test_skill_reservation_requires_actual_skill_and_workspace(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config, _FakePcm())
    row = _skill_row(client)
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    body = {"kind": "implement", "workspace": "personal", "targets": [row["id"]]}
    assert client.post("/api/proposals/reservations", json={**body, "workspace": "work"}).status_code == 404
    assert client.post("/api/proposals/reservations", json=body).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_reserved_skill_implementation_survives_cancel_and_rejects_wrong_scope(tmp_path: Path, monkeypatch, durable: bool) -> None:
    import asyncio
    import httpx
    from ciao.web.proposal_reservations import ReservationRegistry, seal_proposal_operation, validate_scope
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    raw_scope = {"kind": "implement", "workspace": "personal", "targets": [row["id"]]}
    scope = validate_scope(raw_scope, set(config.workspace_names()))
    wrong = registry.reserve(validate_scope({**raw_scope, "workspace": "work"}, set(config.workspace_names())))
    token = registry.reserve(scope)
    entered, release = threading.Event(), threading.Event()
    original = pcm.create_chat
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(pcm, "create_chat", held)
    path = f"/api/proposals/{row['id']}/implement"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as ac:
        assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": wrong})).status_code == 409
        task = asyncio.create_task(ac.post(path, headers={"X-Ciao-Proposal-Reservation": token}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            seal = {"id": token, "scope": raw_scope, "inspected": True}
            assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 409
            assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": token})).status_code == 409
        finally:
            release.set()
        await asyncio.gather(*tuple(registry._tasks))
        await asyncio.sleep(0)
        assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 200
        assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": token})).status_code == 409
    assert len(pcm.prompts) == 1 and len(pcm._chats) == 1


def test_accepting_a_skill_row_opens_one_chat_in_its_own_workspace(
    tmp_path: Path,
) -> None:
    """The skill is work's, so the chat is work's. Hosting it anywhere else
    edits the wrong copy of the catalog while the record says the right one."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "work", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)

    response = client.post(f"/api/proposals/{row['id']}/implement")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["ok"] is True
    assert body["created"] is True
    assert pcm.get_chat(body["chat_id"]).project_id == "proj-work"
    assert pcm.get_chat(body["chat_id"]).title == "Improve 2026-08-09-defuddle"
    # A resolution helper, so the chat archives once the proposal is resolved.
    assert pcm.helpers == [
        {
            "kind": "proposal",
            "intent": "resolve",
            "proposal_ids": [row["id"]],
            "archive_policy": "when_resolved",
        }
    ]


def test_accepting_twice_returns_the_same_chat_and_creates_no_duplicate(
    tmp_path: Path,
) -> None:
    """A double tap, a retry after a dropped response, and a second device all
    land here. Each is the same row, so each gets the same chat back."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))

    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    assert second["chat_id"] == first["chat_id"]
    assert (first["created"], second["created"]) == (True, False)
    assert len(pcm.prompts) == 1, "the prompt was dispatched twice"
    # And the association is on the record, so a fresh client sees it too.
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id == first["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def test_a_second_device_reads_the_live_chat_off_the_row(tmp_path: Path) -> None:
    """The server row is the source of truth. The browser used to keep the same
    association in localStorage, which is exactly what another device cannot
    see, and a reload throws away."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    accepted = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    relisted = _skill_row(_client(config, pcm))

    assert relisted["chat_id"] == accepted["chat_id"]
    assert relisted["lifecycle"] == skill_proposals.IMPLEMENTING
    # The finding is still on the row, so the review card can show what the
    # chat was asked to do.
    assert relisted["problem"] == "Repeated fetch failures."
    assert relisted["reviewed_revision"] == "a" * 64
    # Still queued: the work is unfinished, so the question stays on screen.
    assert _skill_row(_client(config, pcm))["id"] == row["id"]


def test_the_prompt_names_the_existing_skill_and_the_reviewed_revision(
    tmp_path: Path,
) -> None:
    """The prompt is the acceptance, and it is the server's. The browser's copy
    had drifted into telling the chat to create a skill — a different task from
    the one the record describes, since the skill is already there."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)

    client.post(f"/api/proposals/{row['id']}/implement")

    prompt = pcm.prompts[0][1]
    assert prompt == skill_proposals.render_improvement_prompt(
        skill_proposals.find_proposal(config, row["id"])
    )
    assert "/agent/skills/2026-08-09-defuddle/SKILL.md" in prompt
    assert "already exists" in prompt
    assert "Repeated fetch failures." in prompt
    assert "Add a defuddle fallback." in prompt
    assert "aaaaaaaaaaaa" in prompt   # the reviewed revision, shortened
    assert "sess-a1" in prompt       # the evidence it came from
    assert "ciao skill-proposal-remove" in prompt


def test_an_archived_chat_is_not_a_live_implementation(tmp_path: Path) -> None:
    """A chat the operator can no longer open is not the answer to "is this
    still being worked on". The proposal is still queued, so re-accepting has
    to be able to start a fresh one rather than hand back a dead link."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    pcm.get_chat(first["chat_id"]).archived = True

    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()

    assert second["created"] is True
    assert second["chat_id"] != first["chat_id"]
    # On disk, not just in the reply: a row still naming the dead chat is a row
    # whose "Open chat" is a link to nothing, and the next accept would think
    # the work is already running somewhere it cannot be.
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id == second["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def _real_pcm(config: CiaoConfig, tmp_path: Path) -> ProjectChatManager:
    """A real chat manager, so the route runs the real ``start_stream``.

    ``_FakePcm``'s ``start_stream`` appends to a list and returns, which is why
    it cannot see the defect this half of the file guards: the real
    ``ProjectChatManager.start_stream`` reaches
    ``ChatStreaming.start_drive``, which calls ``asyncio.create_task``. That is
    correct on the event loop and a ``RuntimeError: no running event loop`` in a
    worker thread, so an accept that dispatched its turn off-loop failed every
    time while every fake-based test stayed green.
    """
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def test_accepting_dispatches_the_turn_on_the_event_loop(tmp_path: Path) -> None:
    """The route with the real manager, through a real running loop.

    The off-loop half is the vault scan, the project lookup and ``create_chat``;
    the provider turn has to go out on the loop thread, like every other caller
    that opens a chat. Asserted on the turn actually being driven, not on a
    200: a route that returned OK without dispatching would leave a chat nobody
    is working in.
    """
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "work", "2026-08-09-defuddle")
    pcm = _real_pcm(config, tmp_path)
    driven: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        driven.append(prompt)
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-x",
            is_error=False,
            usage={},
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    with _client(config, pcm) as client:
        row = _skill_row(client)
        response = client.post(f"/api/proposals/{row['id']}/implement")
        assert response.status_code == 200, response.json()
        body = response.json()
        # The drive task is created by ``start_stream`` itself, so a running
        # loop turns the turn into a call on the fake provider above.
        deadline = time.monotonic() + 5.0
        while not driven and time.monotonic() < deadline:
            time.sleep(0.01)

    assert driven, "the accept never dispatched a provider turn"
    assert "2026-08-09-defuddle" in driven[0]
    record = skill_proposals.parse_proposal(source, "work")
    assert record is not None
    assert record.chat_id == body["chat_id"]
    assert record.lifecycle == skill_proposals.IMPLEMENTING


def test_a_failed_dispatch_leaves_the_association_behind(tmp_path: Path) -> None:
    """A turn that cannot start must not orphan the chat it was opened for.

    Binding before dispatching is what makes a second accept return the same
    chat instead of minting another one: the record is the association, so it
    is written first, and the operator still has an "Open chat" to send into.
    """

    class _RefusingPcm(_FakePcm):
        def start_stream(self, chat_id: str, prompt: str) -> None:
            raise RuntimeError("no provider")

    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _RefusingPcm()
    row = _skill_row(_client(config, pcm))

    first = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement")

    assert first.status_code == 500
    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.chat_id
    second = _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").json()
    assert second["chat_id"] == record.chat_id
    assert second["created"] is False


def test_accepting_refuses_a_row_that_is_not_a_skill_proposal(tmp_path: Path) -> None:
    """A memory bullet is promoted by writing to a region. Routing one here
    would open a chat to do work the queue already does inline."""
    config = _default_vault(tmp_path)
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    response = client.post(f"/api/proposals/{row['id']}/implement")

    assert response.status_code == 409
    assert "skill proposal" in response.json()["error"]
    assert pcm.prompts == []


def test_accepting_an_unknown_or_settled_proposal_is_not_found(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    row = _skill_row(_client(config, pcm))
    assert _client(config, pcm).post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    assert _client(config, pcm).post(f"/api/proposals/{row['id']}/implement").status_code == 404
    assert _client(config, pcm).post("/api/proposals/nope/implement").status_code == 404
    assert pcm.prompts == []


def test_accepting_settles_nothing_on_its_own(tmp_path: Path) -> None:
    """Completion is never inferred from the chat ending. The record stays
    queued until the work records an outcome, one way or the other."""
    config = _config(tmp_path)
    _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)
    client.post(f"/api/proposals/{row['id']}/implement")

    history = client.get("/api/proposals/history").json()["rows"]
    assert history == []
    assert _skill_row(_client(config, pcm))["lifecycle"] == skill_proposals.IMPLEMENTING


def test_a_dismissal_during_an_implementation_still_records_its_decision(
    tmp_path: Path,
) -> None:
    """Settling is a decision a person makes, in whatever state the work is in."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    pcm = _FakePcm()
    client = _client(config, pcm)
    row = _skill_row(client)
    client.post(f"/api/proposals/{row['id']}/implement")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    record = skill_proposals.parse_proposal(source, "personal")
    assert record is not None
    assert record.lifecycle == skill_proposals.DISMISSED
    history = client.get("/api/proposals/history").json()["rows"]
    assert [item["text"] for item in history] == ["skill:2026-08-09-defuddle"]


# -- the leak warning is about a SHARED guide, not about a workspace ----------


def _rerooted(config: CiaoConfig, tmp_path: Path) -> None:
    """Flip the config's layout to per-root, as the migration does."""
    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()


def test_no_leak_warning_once_each_workspace_owns_its_guide(tmp_path: Path) -> None:
    """It said a work row would be "visible in every workspace" — of a guide only
    work reads. A warning that is false teaches the operator to click through."""
    config = _default_vault(tmp_path)
    _rerooted(config, tmp_path)

    assert proposal_service._leak_warning(config, "memory", "work") is False
    assert proposal_service._leak_warning(config, "profile", "work") is False


def test_a_shared_guide_still_warns(tmp_path: Path) -> None:
    """Before the re-rooting one CLAUDE.md really is loaded by every session."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()   # no receipt: shared layout

    assert proposal_service._leak_warning(config, "memory", "work") is True
    # The primary workspace's own row is where the guide belongs, so no warning.
    assert proposal_service._leak_warning(config, "memory", "personal") is False


def test_a_rehome_never_warns_in_either_layout(tmp_path: Path) -> None:
    """A move is not a region write."""
    config = _default_vault(tmp_path)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    assert proposal_service._leak_warning(config, "rehome", "work") is False
    _rerooted(config, tmp_path)
    assert proposal_service._leak_warning(config, "rehome", "work") is False


def test_an_already_moved_row_clears_instead_of_erroring(tmp_path: Path) -> None:
    """The stuck-row case, end to end: the note is at the destination and its
    bullet is still queued, which is what a cancelled handler leaves. Accepting
    must clear the row rather than answer "no note at ..." forever."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    # Simulate the interrupted attempt: note moved, bullet still there.
    source = config.workspace_vault_root("personal") / "People" / "Mo.md"
    destination = config.workspace_vault_root("work") / "People" / "Mo.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)

    # With the note gone from its source there is no live signal, so the row is
    # unjustified and the operator names the destination — which is what the
    # picker does. The point is that naming it CLEARS the row instead of
    # answering "no note at ..." forever.
    resp = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")

    assert resp.status_code == 200, resp.json()
    assert resp.json()["result"]["already_moved"] is True
    assert resp.json()["result"]["destination"] == "work/memory-vault/People/Mo.md"
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


@pytest.mark.parametrize("already_moved", [False, True])
def test_batch_rehome_reports_real_destination_and_success(tmp_path: Path, already_moved: bool) -> None:
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
    source = config.workspace_vault_root("personal") / "People" / "Mo.md"
    destination = config.workspace_vault_root("work") / "People" / "Mo.md"
    original = source.read_bytes()
    if already_moved:
        source.replace(destination)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]], "workspace": "work"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["dismissed"] is True and result["already_moved"] is already_moved
    assert result["destination"] == "work/memory-vault/People/Mo.md"
    assert len(body["summary"]) == 1
    assert body["summary"][0]["destination"] == result["destination"]
    assert body["summary"][0]["ok"] == 1 and body["summary"][0]["failed"] == 0
    assert destination.read_bytes() == original and not source.exists()
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_batch_moves_every_selected_row_to_one_workspace(tmp_path: Path) -> None:
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for name in ("Mo", "Ida"):
        (config.workspace_vault_root("personal") / "People" / f"{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n---\n# {name}\n", encoding="utf-8"
        )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`?  _(from: vault-rehome)_\n"
        "- [rehome] Re-home `personal/People/Ida.md` to `work/People/Ida.md`?  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    ids = [r["id"] for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]
    assert len(ids) == 2

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": ids, "workspace": "work"}
    )

    assert resp.status_code == 200, resp.json()
    assert all(r["dismissed"] for r in resp.json()["results"]), resp.json()
    for name in ("Mo", "Ida"):
        assert (config.workspace_vault_root("work") / "People" / f"{name}.md").is_file()
        assert not (config.workspace_vault_root("personal") / "People" / f"{name}.md").exists()
    assert not [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]


def test_a_batch_keeps_the_rows_whose_move_failed(tmp_path: Path) -> None:
    """One bad row must not drop the others' bullets, and must keep its own."""
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for name in ("Mo", "Ida"):
        (config.workspace_vault_root("personal") / "People" / f"{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n---\n# {name}\n", encoding="utf-8"
        )
    # Something already occupies Ida's destination, so her move refuses.
    (config.workspace_vault_root("work") / "People" / "Ida.md").write_text(
        "# A different Ida\n", encoding="utf-8"
    )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`?  _(from: vault-rehome)_\n"
        "- [rehome] Re-home `personal/People/Ida.md` to `work/People/Ida.md`?  _(from: vault-rehome)_\n"
    ))
    client = _client(config)
    ids = [r["id"] for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": ids, "workspace": "work"}
    )

    results = {r["id"]: r for r in resp.json()["results"]}
    assert sum(1 for r in results.values() if r["dismissed"]) == 1
    assert any("already exists" in str(r.get("error", "")) for r in results.values())
    assert sum(group["ok"] for group in resp.json()["summary"]) == 1
    assert sum(group["failed"] for group in resp.json()["summary"]) == 1
    left = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "rehome"]
    assert len(left) == 1
    assert "Ida" in left[0]["rehome"]["note"]
    assert (config.workspace_vault_root("personal") / "People" / "Ida.md").is_file()


# -- resolution paths settle only what they carried out -----------------------


def test_a_failed_accept_is_refused(tmp_path: Path) -> None:
    """A refused write leaves the bullet queued: an attempt, not a decision."""
    config = _default_vault(tmp_path)
    _corrupt_guide(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409


def test_an_unknown_batch_id_is_a_404(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [rows[0]["id"], "does-not-exist"]},
    )

    assert resp.status_code == 404


def test_a_batch_accept_keeps_unwritable_rows_queued(tmp_path: Path) -> None:
    """The batch keeps unwritable bullets queued; those are not promotions."""
    # Per-root layout: in the shared layout both workspaces resolve ONE guide,
    # so corrupting work's would refuse personal's rows too.
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    _write_queue(config, "personal", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A personal fact.  _(from: Decisions)_\n"
    ))
    # Corrupting only work's guide splits the batch: personal's rows write,
    # work's refuse, and only the writes may count as decisions.
    _write_queue(config, "work", (
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A work-origin fact.  _(from: Decisions)_\n"
    ))
    _corrupt_guide(config, "work")
    client = _client(config)
    rows = [
        r for r in client.get("/api/proposals").json()["rows"]
        if r["kind"] == "memory"
    ]
    assert {r["workspace"] for r in rows} == {"personal", "work"}

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "accept", "ids": [r["id"] for r in rows]},
    )

    results = resp.json()["results"]
    promoted_count = sum(1 for r in results if r.get("promoted"))
    assert promoted_count == 1
    assert any(not r.get("promoted") for r in results)


def test_the_bulk_sweep_removes_only_older_rows(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "work", (
        "## 2026-07-01 curation pass\n\n"
        "- [memory] An old work fact.  _(from: Decisions)_\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A recent work fact.  _(from: Decisions)_\n"
    ))
    client = _client(config)

    resp = client.post("/api/proposals/dismiss-older-than?date=2026-08-01")

    assert resp.json()["removed"] == 1


def test_dismissing_a_skill_row_keeps_its_record(tmp_path: Path) -> None:
    """A skill row is settled rather than deleted."""
    config = _config(tmp_path)
    source = _write_skill_proposal(config, "personal", "2026-08-09-defuddle")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "skill")

    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    assert source.is_file()


def test_a_batch_rehome_accept_dismisses_every_result(tmp_path: Path) -> None:
    """A successful batch rehome produces two results (the move above the
    grouping and the bullet drop inside it); both report the row resolved."""
    config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])

    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})

    assert response.status_code == 200, response.json()
    assert all(r.get("dismissed") for r in response.json()["results"]), response.json()


def test_a_batch_row_removed_by_another_request_is_not_rewritten(
    tmp_path: Path, monkeypatch
) -> None:
    """When a concurrent resolver drops the bullet between this batch's scan
    and its write, ``_remove_bullet_line`` matches nothing; the loser reports
    nonremoval and must not rewrite the queue."""
    config = _config(tmp_path)
    queue_path = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    monkeypatch.setattr(proposal_service, "_remove_bullet_line", lambda *a, **k: False)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})

    assert response.status_code == 200
    assert response.json()["results"][0]["dismissed"] is False
    # The loser did not rewrite the queue around the winner's deletion.
    assert "Remember the thing" in queue_path.read_text()


def test_an_accept_that_loses_the_bullet_race_leaves_the_queue(tmp_path: Path, monkeypatch) -> None:
    """A concurrent resolver (another request, or the CLI) removes the bullet
    between this request's scan and its write: ``_remove_bullet_line`` matches
    nothing. The loser must not rewrite the queue around the winner's deletion
    — a decision it did not carry out."""
    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")

    monkeypatch.setattr(proposal_service, "_remove_bullet_line", lambda *a, **k: False)
    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 409
    assert response.json()["conflict"] is True
    # The queue was left as the winner wrote it.
    queue_path = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert "- [memory] Remember the thing" in queue_path.read_text()


# ---- concurrent queue rewrites ---------------------------------------------


def test_dismiss_route_runs_the_locked_transaction_off_the_event_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """A contended queue lock must not freeze the server.

    `queue_lock` waits with a synchronous sleep; if the route held it inline
    the event loop would stall for the wait. The rewrite helper must be
    dispatched through `asyncio.to_thread`.
    """

    config = _config(tmp_path)
    _write_queue(config, "personal", "# Proposals\n\n- [memory] Remember the thing\n")
    client = _client(config)
    row = next(
        r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory"
    )

    real = proposal_service._rewrite_queue_single
    worker_threads: list[int] = []
    requested_ids: list[str] = []
    main_thread = __import__("threading").get_ident()

    def spy(*args, **kwargs):
        worker_threads.append(__import__("threading").get_ident())
        requested_ids.append(args[-1])
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_single", spy)
    response = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert response.status_code == 200
    assert worker_threads and worker_threads[0] != main_thread
    assert requested_ids == [row["id"]]


def test_batch_duplicate_requested_id_refused_before_queue_change(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original = "# Proposals\n\n- [memory] Duplicate\n- [memory] Duplicate\n"
    _write_queue(config, "personal", original)
    client = _client(config)
    row = next(r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory")
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"], row["id"]]})
    assert response.status_code == 400
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    assert queue.read_text(encoding="utf-8") == original


def test_a_shifted_line_index_does_not_delete_a_bystander():
    """The captured index is a hint, not an address.

    `_scan_proposal_rows` records a line index, and an accept then awaits an
    unbounded model call before rewriting the queue - with no lock anywhere. A
    second accept or dismiss landing in that window removes a line and shifts
    every later index, so deleting by index alone took out an UNRELATED
    proposal and left the accepted one sitting in the queue.
    """
    from ciao.web.proposal_service import _remove_bullet_line

    # The bullet was at index 1 when it was scanned; a concurrent dismiss has
    # since removed the line above it, so index 1 now holds someone else.
    lines = ["- [memory] mine", "- [memory] a bystander"]

    assert _remove_bullet_line(lines, 2, "- [memory] mine") is True
    assert lines == ["- [memory] a bystander"], "it deleted the wrong bullet"


def test_removing_a_bullet_that_is_already_gone_is_a_no_op():
    """Whoever removed it got there first; nothing else may be taken instead."""
    from ciao.web.proposal_service import _remove_bullet_line

    lines = ["- [memory] someone else's"]

    assert _remove_bullet_line(lines, 0, "- [memory] already dismissed") is False
    assert lines == ["- [memory] someone else's"]


def test_accept_reports_the_text_it_actually_wrote(tmp_path: Path) -> None:
    """What landed is not always the sentence on the row.

    The event-shape guard promotes only a bullet's trailing "Durable rule:"
    clause, so a caller echoing the row's own text would tell the user something
    different from what is now in the region. The endpoint reports what it wrote.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "personal",
        "# Memory Proposals\n\n"
        "- [memory] User asked me to stop using em dashes. "
        "Durable rule: Avoid em dashes; use commas.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept")
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert result["written"] == "Avoid em dashes; use commas."
    assert "Avoid em dashes; use commas." in _region_entries(
        config, row["workspace"], "memory"
    )


def test_accept_reports_a_duplicate_rather_than_claiming_a_write(tmp_path: Path) -> None:
    """A row already in the region resolves, but nothing was written.

    Without this the caller cannot tell "stored" from "was already there",
    which is the difference between a write and a no-op.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    fact = "Prefers direct implementation over discussion."
    _write_queue(
        config,
        "personal",
        f"# Memory Proposals\n\n- [memory] {fact}  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    first = client.post(f"/api/proposals/{row['id']}/accept")
    assert first.status_code == 200
    assert "duplicate" not in first.json()["result"]

    # Queue the identical fact again; the region already holds it.
    _write_queue(
        config,
        "personal",
        f"# Memory Proposals\n\n- [memory] {fact}  _(from: Decisions)_\n",
    )
    again = _accept_kind_row(client, "memory")
    resp = client.post(f"/api/proposals/{again['id']}/accept")
    assert resp.status_code == 200
    assert resp.json()["result"]["duplicate"] is True


# -- Decision history paging and identity -------------------------------------


def _seed_history(config: CiaoConfig, workspace: str, entries: list[dict]) -> None:
    """Write decision rows straight into a workspace's sidecar."""
    from ciao.memory_proposals import dismissed_log_path

    queue = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    log = dismissed_log_path(queue)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
        encoding="utf-8",
    )


def _predict(config: CiaoConfig, workspace: str, text: str, verdict: str, at: str) -> None:
    """Write a prediction row with a chosen timestamp, as the nightly run would."""
    from ciao.proposal_predictions import predictions_path

    queue = config.workspace_vault_root(workspace) / "Workspace" / "Memory-Proposals.md"
    sidecar = predictions_path(queue)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "predicted_at": at,
        "kind": "memory",
        "text": text,
        "verdict": verdict,
        "reason": "stable preference",
    }
    with sidecar.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def test_history_rows_carry_the_joined_prediction_and_match(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One.", "via": "pwa"}],
    )
    _predict(config, "personal", "One.", "dismiss", "2026-09-01T09:00:00+00:00")

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert rows[0]["prediction"] == {
        "verdict": "dismiss",
        "reason": "stable preference",
        "ts": "2026-09-01T09:00:00+00:00",
        "match": False,
    }


def test_history_rows_without_a_prediction_have_no_prediction_key(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."}],
    )

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "prediction" not in rows[0]


def test_history_does_not_join_a_prediction_made_after_the_decision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."}],
    )
    _predict(config, "personal", "One.", "accept", "2026-09-01T10:00:01+00:00")

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "prediction" not in rows[0]


def _seed_agreeing_decisions(config: CiaoConfig, workspace: str, count: int) -> None:
    entries = []
    for i in range(count):
        text = f"Fact number {i}."
        entries.append(
            {
                "promoted_at": f"2026-09-01T10:{i:02d}:00+00:00",
                "kind": "memory",
                "text": text,
                "via": "pwa",
            }
        )
        _predict(config, workspace, text, "accept", f"2026-09-01T09:{i:02d}:00+00:00")
    _seed_history(config, workspace, entries)


def test_proposals_handover_returns_setting_and_agreement(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_agreeing_decisions(config, "personal", 20)
    client = _client(config)

    body = client.get("/api/proposals/handover", params={"workspace": "personal"}).json()

    assert body["workspace"] == "personal"
    assert body["handover"] == "off"
    assert body["agreement"]["decided"] == 20
    assert body["agreement"]["rate"] == 1.0
    assert body["agreement"]["suggest_handover"] is True

    config.workspace("personal").memory_handover = "dismiss"
    body = client.get("/api/proposals/handover", params={"workspace": "personal"}).json()
    assert body["handover"] == "dismiss"
    assert body["agreement"]["suggest_handover"] is False


def test_proposals_handover_reports_whether_acting_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    client = _client(config)
    monkeypatch.setattr("ciao.web.routes_api.trusted_registry", lambda name, vault: None)
    body = client.get("/api/proposals/handover", params={"workspace": "personal"}).json()
    assert body["acting_available"] is False

    seen: list[tuple[str | None, Path]] = []

    def trusted(name: str | None, vault: Path) -> CiaoConfig:
        seen.append((name, vault))
        return config

    monkeypatch.setattr("ciao.web.routes_api.trusted_registry", trusted)
    body = client.get("/api/proposals/handover", params={"workspace": "personal"}).json()
    assert body["acting_available"] is True
    assert seen == [("personal", config.workspace_vault_root("personal"))]


def test_proposals_handover_validates_workspace(tmp_path: Path) -> None:
    client = _client(_config(tmp_path))

    assert client.get("/api/proposals/handover").status_code == 400
    assert client.get("/api/proposals/handover", params={"workspace": "nope"}).status_code == 404


def test_proposals_handover_requires_the_session(tmp_path: Path) -> None:
    from itsdangerous import URLSafeTimedSerializer
    from starlette.middleware import Middleware

    from ciao.web.auth import SESSION_COOKIE, AuthMiddleware

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/proposals/handover", proposals_handover, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = _config(tmp_path)
    client = TestClient(app, base_url="https://ciao.example")
    url = "/api/proposals/handover?workspace=personal"

    assert client.get(url).status_code == 401
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    assert client.get(url).status_code == 200


def test_history_row_ids_are_unique_across_workspaces(tmp_path: Path) -> None:
    """The same fact decided in two workspaces in the same second.

    Timestamps are second-precision and the id used to hash only
    ts|action|kind|text, so these two rows collided. The History list keys its
    <li> on the id, so a collision made Vue drop one row on patch.
    """
    config = _config(tmp_path)
    row = {"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "Same fact."}
    for ws in ("personal", "work"):
        _seed_history(config, ws, [row])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert len(rows) == 2
    assert len({r["id"] for r in rows}) == 2
    assert {r["workspace"] for r in rows} == {"personal", "work"}


def test_history_row_ids_are_unique_for_identical_undated_rows(tmp_path: Path) -> None:
    """Two legacy rows with no timestamp and the same text are still two rows."""
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [{"kind": "memory", "text": "Legacy fact."}, {"kind": "memory", "text": "Legacy fact."}],
    )

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert len(rows) == 2
    assert len({r["id"] for r in rows}) == 2


def test_history_row_ids_are_stable_across_reads(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."},
            {"dismissed_at": "2026-09-01T11:00:00+00:00", "kind": "memory", "text": "Two."},
        ],
    )
    client = _client(config)

    first = client.get("/api/proposals/history").json()["rows"]
    second = client.get("/api/proposals/history").json()["rows"]

    assert [r["id"] for r in first] == [r["id"] for r in second]


def test_history_does_not_leak_the_internal_sequence_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(config, "personal", [{"kind": "memory", "text": "One."}])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "seq" not in rows[0]


def test_history_reports_at_max_when_the_limit_is_clamped(tmp_path: Path) -> None:
    """Past the cap a wider limit returns the same page, and the client has to
    know that: a "show more" button wired to `truncated` alone stayed visible
    and did nothing forever.
    """
    from ciao.web import routes_api as api_module

    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )
    client = _client(config)

    monkeyed = api_module._HISTORY_MAX_LIMIT
    api_module._HISTORY_MAX_LIMIT = 4
    try:
        data = client.get("/api/proposals/history", params={"limit": 200}).json()
    finally:
        api_module._HISTORY_MAX_LIMIT = monkeyed

    assert len(data["rows"]) == 4
    assert data["limit"] == 4
    assert data["total"] == 6
    # There ARE more rows...
    assert data["truncated"] is True
    # ...but asking for more will not return them.
    assert data["at_max"] is True


def test_history_does_not_report_at_max_below_the_cap(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )

    data = _client(config).get("/api/proposals/history", params={"limit": 2}).json()

    assert data["truncated"] is True
    assert data["at_max"] is False
    assert data["limit"] == 2


def test_history_workspace_filter_pages_within_that_workspace(tmp_path: Path) -> None:
    """A quiet workspace's decisions must not fall outside a global page.

    The client used to fetch the newest N rows across every workspace and
    filter them client-side, so on a busy install a quiet workspace's History
    tab read "No decisions yet." with a full ledger.
    """
    config = _config(tmp_path)
    _seed_history(
        config,
        "work",
        [
            {"promoted_at": f"2026-09-02T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Busy {i}."}
            for i in range(30)
        ],
    )
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-01-01T09:00:00+00:00", "kind": "memory", "text": "Quiet fact."}],
    )

    data = _client(config).get(
        "/api/proposals/history", params={"workspace": "personal", "limit": 5}
    ).json()

    assert [r["text"] for r in data["rows"]] == ["Quiet fact."]
    assert data["total"] == 1
    assert data["truncated"] is False


def test_history_reports_at_max_for_a_request_of_exactly_the_cap(tmp_path: Path) -> None:
    """A request for the cap is already at it.

    `at_max` was `requested > limit`, so asking for exactly the cap came back
    False alongside `truncated: True` — the client saw "more available, keep
    asking" and burned one full-page refetch before the flag finally tripped.
    """
    from ciao.web import routes_api as api_module

    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {"promoted_at": f"2026-09-01T10:00:{i:02d}+00:00", "kind": "memory", "text": f"Fact {i}."}
            for i in range(6)
        ],
    )
    client = _client(config)

    monkeyed = api_module._HISTORY_MAX_LIMIT
    api_module._HISTORY_MAX_LIMIT = 4
    try:
        data = client.get("/api/proposals/history", params={"limit": 4}).json()
    finally:
        api_module._HISTORY_MAX_LIMIT = monkeyed

    assert data["limit"] == 4
    assert data["truncated"] is True
    assert data["at_max"] is True


def test_history_reads_a_shared_sidecar_once(tmp_path: Path) -> None:
    """Two registered names can resolve to one vault root, hence one sidecar.

    The loop trusted one-sidecar-per-name, so both names read the same file and
    every decision in it appeared twice with `total` double-counted.
    """
    vault = tmp_path / "memory-vault"
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={
            # Both point at the same root, as a `vault_root: "."` entry or the
            # legacy entity layout does.
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/shared"),
            "alias": WorkspaceConfig(name="alias", vault_root="memory-vault/shared"),
        },
    )
    _seed_history(
        config,
        "personal",
        [{"promoted_at": "2026-09-01T10:00:00+00:00", "kind": "memory", "text": "One."}],
    )

    data = _client(config).get("/api/proposals/history").json()

    assert data["total"] == 1
    assert [r["text"] for r in data["rows"]] == ["One."]


def test_history_does_not_leak_the_internal_sidecar_field(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(config, "personal", [{"kind": "memory", "text": "One."}])

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert "log" not in rows[0]
    assert "seq" not in rows[0]


def test_a_stale_rehome_row_says_so_to_the_client(tmp_path: Path) -> None:
    """A bullet that outlived its cause reaches the PWA marked `stale`.

    `_rehome_lookup` computes `stale` for exactly this case — a `[rehome]`
    bullet naming a note that no longer produces a live signal — so the UI can
    offer to clear the litter instead of asking the operator to decide it. The
    row builder copied the signal field by field and dropped `stale`, so the
    stale branch in the client was dead code and such a row rendered as a
    genuine "no destination, needs a decision" question.
    """
    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    # Deliberately no note on disk: nothing backs this bullet any more.
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Ghost.md` to `work/People/Ghost.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
    ))
    client = _client(config)

    row = _accept_kind_row(client, "rehome")

    assert row["rehome"]["stale"] is True, row["rehome"]
    assert row["rehome"]["candidates"] == []


def test_a_live_rehome_row_is_not_marked_stale(tmp_path: Path) -> None:
    """The companion guard: a real question must not read as litter."""
    _config, _client_, row = _rehome_fixture(tmp_path, ["person", "colleague"])

    assert row["rehome"]["stale"] is False, row["rehome"]


_LEARNINGS_TEXT = "Check the queue lock before writing the destination."


def _learnings_queue(text: str = _LEARNINGS_TEXT) -> str:
    return (
        "# Memory Proposals\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        f"- [learnings] {text}  _(from: Decisions)_\n"
    )


def _learnings_vault(tmp_path: Path) -> CiaoConfig:
    config = _config(tmp_path)
    (config.workspace_vault_root("personal") / "Workspace").mkdir(
        parents=True, exist_ok=True
    )
    _write_queue(config, "personal", _learnings_queue())
    return config


def _learnings_count(config: CiaoConfig) -> int:
    """The recurrence count on the seeded learning, or 0 if nothing was written."""
    path = config.workspace_vault_root("personal") / "Workspace" / "Learnings.md"
    if not path.exists():
        return 0
    match = re.search(r"\(x(\d+)\)", path.read_text(encoding="utf-8"))
    return int(match.group(1)) if match else 0


# ---- Category proposals (issue #647) ----------------------------------------
#
# The notes carry the RAW spelling the cluster was detected under and the sidecar
# carries the id derived from it, so a retype is a real change of bytes rather
# than a rewrite of a value to itself.

_RAW_TYPE = "Recipe Book"
_CATEGORY_ID = "recipe-book"


def _category_vault(
    tmp_path: Path,
    *,
    folder: str = "Recipes",
    ticked: tuple[str, ...] = ("One.md", "Two.md", "Three.md"),
    declared: str | None = _RAW_TYPE,
) -> CiaoConfig:
    """A workspace whose queue holds one `[category]` row over a note cluster.

    ``ticked`` is the note list the sidecar carries — the rows the owner ticked
    in the drawer — and ``declared`` is what each of those notes currently says.
    A test for drift passes a different type, and one for a frontmatter-less
    note passes ``None``.

    The sidecar's paths are rendered ``Entry.path`` values: a vault directory
    name followed by the note's path inside the vault that was scanned, which is
    what ``vault_migration`` strips the same way when it retypes a note.
    """
    from ciao.memory_proposals import MemoryProposal, append_proposals
    from ciao.vocabulary_proposals import write_category_sidecar

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    notes = vault / "Journals"
    notes.mkdir(parents=True, exist_ok=True)
    for name in ticked:
        frontmatter = f"type: {declared}\n" if declared else ""
        (notes / name).write_text(
            f"---\n{frontmatter}---\n# {Path(name).stem}\n", encoding="utf-8"
        )
    write_category_sidecar(
        vault,
        {
            "id": _CATEGORY_ID,
            "label": "Recipe book",
            "folder": folder,
            "description": f"Proposed from {len(ticked)} notes already typed {_RAW_TYPE}.",
            "source_type": _RAW_TYPE,
            "paths": [f"memory-vault/Journals/{name}" for name in ticked],
            "declined": False,
        },
    )
    append_proposals(
        [
            MemoryProposal(
                target="category",
                payload=_CATEGORY_ID,
                text=(
                    f"Recipe book → {folder}: Proposed from {len(ticked)} notes "
                    f"already typed {_RAW_TYPE}."
                ),
                source_section=f"{len(ticked)} notes typed {_RAW_TYPE}",
            )
        ],
        vault,
    )
    return config


def _registry_ids(config: CiaoConfig) -> list[str]:
    from ciao import entity_types

    registry = entity_types.load_entity_types(config.agent_vault_root("personal"))
    return [entry.id for entry in registry.entries()]


def test_accepting_a_category_adds_it_and_retypes_the_ticked_notes(
    tmp_path: Path,
) -> None:
    """Nothing is moved: every note stays in the same file and only its
    frontmatter ``type:`` changes."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["action"] == "add_category"
    assert result["promoted"] is True
    assert result["destination"] == "Recipes"
    assert _CATEGORY_ID in _registry_ids(config)
    assert sorted(p.name for p in journals.iterdir()) == ["One.md", "Three.md", "Two.md"]
    for note in journals.iterdir():
        # The body and the file are untouched; only the type line moved.
        assert note.read_text(encoding="utf-8") == (
            f"---\ntype: {_CATEGORY_ID}\n---\n# {note.stem}\n"
        )
    # The row is gone, and the registry file names the new category.
    assert client.get("/api/proposals").json()["rows"] == []
    yaml = (config.agent_vault_root("personal") / "entity-types.yaml").read_text(
        encoding="utf-8"
    )
    assert f"id: {_CATEGORY_ID}" in yaml
    assert "folder: Recipes" in yaml


def test_accepting_a_category_leaves_an_unticked_note_alone(tmp_path: Path) -> None:
    """The sidecar is the note list, so a note the owner left unticked keeps
    the type it had."""
    config = _category_vault(tmp_path, ticked=("One.md", "Two.md"))
    journals = config.workspace_vault_root("personal") / "Journals"
    (journals / "Three.md").write_text(
        f"---\ntype: {_RAW_TYPE}\n---\n# Three\n", encoding="utf-8"
    )
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# One\n"
    )
    assert (journals / "Three.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_RAW_TYPE}\n---\n# Three\n"
    )


def test_accepting_a_category_retypes_a_note_with_no_frontmatter(
    tmp_path: Path,
) -> None:
    """A note that never said what it was has no type anybody could have
    changed, so it is retyped rather than reported as drifted."""
    config = _category_vault(tmp_path, ticked=("One.md",), declared=None)
    journals = config.workspace_vault_root("personal") / "Journals"
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# One\n"
    )


def test_a_category_whose_folder_is_taken_keeps_the_bullet(tmp_path: Path) -> None:
    """`Recipes` is not a collision, `People` is. The registry's own validator
    refuses it, the row survives, and the refusal is a 400 the owner can act on
    rather than a retry of the same request."""
    config = _category_vault(tmp_path, folder="People")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 400, resp.json()
    assert "People" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert _CATEGORY_ID not in _registry_ids(config)


def test_a_note_retyped_since_the_proposal_keeps_the_bullet(tmp_path: Path) -> None:
    """The cluster the operator read is not the cluster on disk, so nothing is
    written — not the registry, and not the notes that were fine."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    (journals / "Two.md").write_text("---\ntype: document\n---\n# Two\n", encoding="utf-8")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "Two.md" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert _CATEGORY_ID not in _registry_ids(config)
    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_RAW_TYPE}\n---\n# One\n"
    )


def test_dismissing_a_category_records_the_refusal_by_id(tmp_path: Path) -> None:
    """The queue bullet is gone but the cluster it described is still in the
    vault. A text-keyed decision has nothing left to match once the row is
    removed, so the id is what stops the same category coming back."""
    from ciao.vocabulary_proposals import declined_category_ids, read_category_sidecar

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    assert client.get("/api/proposals").json()["rows"] == []
    assert declined_category_ids(vault) == {_CATEGORY_ID}
    # The rest of the sidecar survives: a refusal is not a deletion.
    assert read_category_sidecar(vault, _CATEGORY_ID)["paths"]


def test_a_batch_dismiss_records_the_refusal_too(tmp_path: Path) -> None:
    """The batch path records the id before the rewrite too, or a bulk "reject
    these" would bring the whole cluster back the next night."""
    from ciao.vocabulary_proposals import declined_category_ids

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(
        "/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    assert declined_category_ids(vault) == {_CATEGORY_ID}


def test_a_category_refusal_that_cannot_be_recorded_keeps_the_bullet(
    tmp_path: Path,
) -> None:
    """The sidecar is the only record of the note list, so a dismiss without one
    has nothing to refuse: the row survives rather than being resolved into a
    decision that was not made."""
    from ciao.vocabulary_proposals import category_sidecar_path

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    category_sidecar_path(vault, _CATEGORY_ID).unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 409, resp.json()
    assert _CATEGORY_ID in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_category_preview_names_the_notes_it_would_retype(tmp_path: Path) -> None:
    """The card must not fall through to the review row's "no destination yet"
    wording: a category has a destination and the whole list of notes it moves
    onto, and both are known before the click."""
    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/preview")

    assert resp.status_code == 200, resp.json()
    body = resp.json()["preview"]
    assert body["action"] == "add_category"
    assert body["can_accept"] is True
    assert body["exact"] is True
    assert body["category"]["id"] == _CATEGORY_ID
    assert body["category"]["folder"] == "Recipes"
    assert len(body["category"]["notes"]) == 3


def test_batch_accept_covers_a_category_row(tmp_path: Path) -> None:
    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert resp.json()["results"][0]["promoted"] is True
    assert _CATEGORY_ID in _registry_ids(config)


@pytest.mark.parametrize("batch", [False, True])
def test_category_dismiss_stale_selection_does_not_settle(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao.vocabulary_proposals import category_sidecar_path
    from ciao.web import proposal_service

    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")
    queue = proposal_service._proposals_file(config, "personal")
    sidecar = category_sidecar_path(config.workspace_vault_root("personal"), _CATEGORY_ID)
    before = sidecar.read_bytes()
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 409
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("batch", [False, True])
def test_category_stale_selection_does_not_retype(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _category_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "category")
    queue = proposal_service._proposals_file(config, "personal")
    note = config.workspace_vault_root("personal") / "Journals" / "One.md"
    before = note.read_bytes()
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)
    replacement = "# Memory Proposals\n"

    def race(*args, **kwargs):
        queue.write_text(replacement, encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert note.read_bytes() == before
    assert _CATEGORY_ID not in _registry_ids(config)
    assert queue.read_text(encoding="utf-8") == replacement
    assert not read_decisions(queue)


def test_accepting_a_category_skips_a_note_that_no_longer_exists(
    tmp_path: Path,
) -> None:
    """A note trashed or deleted after the proposal was filed is skipped and
    reported, and the rest of the cluster is accepted as usual."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    (journals / "Two.md").unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert result["skipped"] == ["Two.md"]
    assert _CATEGORY_ID in _registry_ids(config)
    assert (journals / "One.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# One\n"
    )
    assert (journals / "Three.md").read_text(encoding="utf-8") == (
        f"---\ntype: {_CATEGORY_ID}\n---\n# Three\n"
    )
    assert client.get("/api/proposals").json()["rows"] == []


def test_a_category_whose_notes_are_all_gone_keeps_the_bullet(tmp_path: Path) -> None:
    """A category with no notes behind it is not added, and the row stays queued
    so the notes can be restored and the accept retried."""
    config = _category_vault(tmp_path)
    journals = config.workspace_vault_root("personal") / "Journals"
    for name in ("One.md", "Two.md", "Three.md"):
        (journals / name).unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "none of the 3 notes" in resp.json()["error"]
    assert _CATEGORY_ID not in _registry_ids(config)
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_batch_accept_skips_a_missing_category_note(tmp_path: Path) -> None:
    config = _category_vault(tmp_path)
    (config.workspace_vault_root("personal") / "Journals" / "Two.md").unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert resp.json()["results"][0]["promoted"] is True
    assert resp.json()["results"][0]["skipped"] == ["Two.md"]
    assert _CATEGORY_ID in _registry_ids(config)


def test_a_category_preview_reports_notes_that_no_longer_exist(
    tmp_path: Path,
) -> None:
    config = _category_vault(tmp_path)
    (config.workspace_vault_root("personal") / "Journals" / "Two.md").unlink()
    client = _client(config)
    row = _accept_kind_row(client, "category")

    resp = client.post(f"/api/proposals/{row['id']}/preview")

    assert resp.status_code == 200, resp.json()
    body = resp.json()["preview"]
    assert body["can_accept"] is True
    assert len(body["category"]["notes"]) == 2
    assert body["category"]["missing"] == ["Two.md"]
    assert "retypes 2 note(s)" in body["reason"]
    assert "Two.md" in body["reason"]


def test_a_note_trashed_before_a_category_accept_restores_untouched(
    tmp_path: Path,
) -> None:
    """Trashing does not prune the sidecar, so the accept skips the note and
    the note can still be restored from the trash with its original bytes."""
    from ciao import vault_review

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    two = vault / "Journals" / "Two.md"
    original = two.read_text(encoding="utf-8")
    candidate = next(
        item
        for item in vault_review.generate_candidates(vault, workspace="personal")
        if item.path.endswith("/Two.md")
    )
    vault_review.trash_note(vault, candidate)
    client = _client(config)
    row = _accept_kind_row(client, "category")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    assert not two.exists()

    vault_review.restore_note(vault, candidate.candidate_id)
    assert two.read_text(encoding="utf-8") == original


def test_undoing_a_partial_category_accept_restores_only_the_retyped_notes(
    tmp_path: Path,
) -> None:
    from ciao.memory_receipts import journal_path, read_receipts

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    journals = vault / "Journals"
    (journals / "Two.md").unlink()
    before = {note.name: note.read_text(encoding="utf-8") for note in journals.iterdir()}
    client = _undo_client(config)
    row = _accept_kind_row(client, "category")

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "category_apply"
    ][-1]
    undo = client.post(
        f"/api/memory/receipts/{receipt['id']}/undo", params={"workspace": "personal"}
    )

    assert undo.status_code == 200, undo.json()
    assert _CATEGORY_ID not in _registry_ids(config)
    assert not (journals / "Two.md").exists()
    for name, text in before.items():
        assert (journals / name).read_text(encoding="utf-8") == text


# ---- note_edit proposals: a whole note, decided by a person ----------------
#
# The fixture FILES the proposal through the proposer rather than hand-writing
# a bullet, so these tests drive the real path from a `needs_review` verdict to
# a click: the row's id, the sidecar, and the check the filing recorded are all
# the ones a nightly pass would produce.

_EDIT_NOTE = "notes/office.md"
_EDIT_PLAIN = (
    "---\ntype: note\nupdated: 2024-01-05\n---\n\n"
    "# Office\n\nThe office is on Via Verdi 12, third floor.\n"
)
_EDIT_FOURTH = _EDIT_PLAIN.replace("third", "fourth")


def _note_edit_vault(
    tmp_path: Path,
    *,
    operation: str = "replace",
    outcome: str = "update",
    after: str = _EDIT_FOURTH,
    before: str = _EDIT_PLAIN,
    text_on_disk: str | None = None,
    today: date | None = None,
) -> tuple[CiaoConfig, Any]:
    """A workspace whose queue holds one `[note_edit]` row over a real note."""
    from ciao import memory_receipts as mr
    from ciao import note_edit_proposals as nep

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    note = vault / _EDIT_NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes((text_on_disk if text_on_disk is not None else before).encode("utf-8"))
    proposal = nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=_EDIT_NOTE,
        expected_revision=mr.content_revision(before),
        operation=operation,
        before=before,
        after=after,
        outcome=outcome,
        coverage="complete",
        evidence=(),
        reason="the third floor no longer exists",
        today=today,
    )
    return config, proposal


def _note_edit_row(client: TestClient) -> dict:
    return _accept_kind_row(client, "note_edit")


@pytest.mark.parametrize("batch", [False, True])
def test_note_dismiss_stale_selection_does_not_settle(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import note_edit_proposals
    from ciao.web import proposal_service

    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)
    queue = proposal_service._proposals_file(config, "personal")
    sidecar = note_edit_proposals.sidecar_path(config, "personal", proposal.id)
    before = sidecar.read_bytes()
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 409
    assert sidecar.read_bytes() == before


@pytest.mark.parametrize("batch", [False, True])
def test_note_edit_stale_selection_does_not_edit(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config, _ = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)
    queue = proposal_service._proposals_file(config, "personal")
    note = config.workspace_vault_root("personal") / _EDIT_NOTE
    before = note.read_bytes()
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert note.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == "# Memory Proposals\n"
    assert not read_decisions(queue)


def test_a_note_edit_row_names_the_note_it_is_about(tmp_path: Path) -> None:
    """The bullet's payload is a sidecar id; the row a person reads is a path.

    A digest on the row would tell a reviewer nothing about which note they are
    being asked to change, and `destination_revision` needs the note to have
    something to compare.
    """
    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)

    row = _note_edit_row(client)

    assert row["target"] == _EDIT_NOTE
    assert row["note_edit"]["id"] == proposal.id
    assert row["note_edit"]["operation"] == "replace"
    assert row["note_edit"]["can_accept"] is True
    assert row["note_edit"]["settled"] == ""


def test_a_note_edit_preview_is_byte_exact_to_the_accept(tmp_path: Path) -> None:
    """The card's "after" IS the bytes the write produces.

    A whole-note rewrite is the one accept whose result is fully known before the
    click, so `exact` is True and `revision` is the digest the accept re-checks
    — and the preview's after-image has to be the file's bytes afterwards, not
    something the card recomputed.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/preview")

    assert resp.status_code == 200, resp.json()
    body = resp.json()["preview"]
    assert body["action"] == "note_edit"
    assert body["operation"] == "note_edit"
    assert body["destination"] == _EDIT_NOTE
    assert body["destination_path"] == str(vault / _EDIT_NOTE)
    assert body["exact"] is True
    assert body["can_accept"] is True
    assert body["before"] == _EDIT_PLAIN
    assert body["after"] == _EDIT_FOURTH

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == body["after"]


def test_accepting_a_note_edit_applies_it_through_the_note_receipt(
    tmp_path: Path,
) -> None:
    """Journaled, revision-checked and undoable like every other note write."""
    from ciao.memory_receipts import journal_path, read_receipts

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["action"] == "note_edit"
    assert result["promoted"] is True
    assert result["dismissed"] is True
    assert result["destination"] == _EDIT_NOTE
    assert "region" not in result, "a note edit is not a region write"
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH

    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "note_apply"
    ][-1]
    assert receipt["provenance"]["operation"] == "replace"
    assert receipt["provenance"]["proposal_id"] == proposal.proposal_id

    # The row is gone and the record says the decision was made, with the
    # receipt that can undo it.
    assert client.get("/api/proposals").json()["rows"] == []
    from ciao import note_edit_proposals as nep

    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.accepted is True
    assert settled.receipt_id == receipt["id"]


def test_a_note_that_moved_since_the_proposal_is_a_conflict(tmp_path: Path) -> None:
    """The replacement was planned against text that is no longer there.

    A note nobody can overwrite is the whole point of the expected revision, and
    a conflict is told apart from a failure so the client reopens its preview
    rather than reporting the row as broken.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, text_on_disk=_EDIT_PLAIN + "\nA line somebody added by hand.\n"
    )
    vault = config.workspace_vault_root("personal")
    edited = (vault / _EDIT_NOTE).read_bytes()
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["can_accept"] is False
    assert "changed since" in preview["reason"]

    resp = client.post(
        f"/api/proposals/{row['id']}/accept", json={"expected_revision": preview["revision"]}
    )

    assert resp.status_code == 409, resp.json()
    assert resp.json()["conflict"] is True
    assert (vault / _EDIT_NOTE).read_bytes() == edited, "byte-identical, untouched"
    # The row survives, so the owner can re-read the note and decide again.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_stale_preview_is_refused_before_the_write(tmp_path: Path) -> None:
    """The card's own revision handshake, on the note this accept writes."""
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)
    revision = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"][
        "revision"
    ]
    (vault / _EDIT_NOTE).write_text(
        _EDIT_PLAIN + "\nA line somebody added by hand.\n", encoding="utf-8"
    )

    resp = client.post(
        f"/api/proposals/{row['id']}/accept", json={"expected_revision": revision}
    )

    assert resp.status_code == 409, resp.json()
    assert "changed since this preview" in resp.json()["error"]
    assert "preview" in resp.json()
    assert _EDIT_PLAIN in (vault / _EDIT_NOTE).read_text(encoding="utf-8")


def test_accepting_a_retire_trashes_the_note_and_links_the_outcome(
    tmp_path: Path,
) -> None:
    """Attended-only, reversible, and never a delete.

    The only primitive reached is `vault_review.trash_note`, so the note is in
    the review trash with its metadata and the panel's own restore puts it back.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 200, resp.json()
    result = resp.json()["result"]
    assert result["promoted"] is True
    assert not Path(result["destination"]).is_absolute(), (
        "one destination shape, whichever operation wrote it"
    )
    trashed = vault / result["destination"]
    assert not (vault / _EDIT_NOTE).exists()
    assert trashed.is_file()
    assert trashed.read_text(encoding="utf-8") == _EDIT_PLAIN

    from ciao import vault_review as review

    review.restore_note(vault, trashed.stem)
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN


def test_a_retire_of_a_note_that_moved_is_a_conflict(tmp_path: Path) -> None:
    """A retirement is checked against the revision, like every other operation.

    A retirement moves whatever is on disk, and `trash_note`'s hash guard compares
    the note to the bytes the accept just read — so without the expected revision
    in front of it, a note rewritten after the proposal was filed would go to the
    trash with the owner's hand-written line in it, under a 200. The note nobody
    judged is not a note anybody may retire.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    edited = _EDIT_PLAIN + "\nA line somebody added by hand.\n"
    (vault / _EDIT_NOTE).write_text(edited, encoding="utf-8")
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["operation"] == "retire_note"
    assert preview["can_accept"] is False
    assert "changed since" in preview["reason"]
    # The card's own revision handshake covers a retirement too, rather than
    # being skipped for the one write a person cannot walk back by editing.
    assert preview["revision"]

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert resp.json()["conflict"] is True
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == edited, (
        "byte-identical, untouched: nothing reached the trash"
    )
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_settled_note_edit_row_cannot_be_accepted_again(tmp_path: Path) -> None:
    """The accept writes first and settles second, so a row can outlive its decision.

    A bullet whose removal failed leaves exactly that: a settled record behind a
    queued row. The second click must find the decision already on record, or a
    refusal is applied as a rewrite and an accepted retirement is performed twice.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)
    nep.settle_note_edit(config, "personal", proposal.id, accepted=True)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is False
    assert "already decided" in preview["reason"]
    assert _note_edit_row(client)["note_edit"]["can_accept"] is False

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "already decided" in resp.json()["error"]
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN, (
        "the settled decision was applied once, not twice"
    )
    # The row is still there, because a decision cannot un-settle itself.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_restamp_applies_the_date_it_was_filed_on(tmp_path: Path) -> None:
    """A re-stamp's bytes are computed, so the date has to be on the record.

    `date.today()` at accept time made the preview and the write two different
    documents whenever the click landed on the other side of midnight — and the
    card is labelled `exact`. Filed on a fixed date, the note comes back carrying
    THAT date whatever day the accept runs on.
    """
    filed = date(2026, 3, 14)
    config, _proposal = _note_edit_vault(
        tmp_path, operation="restamp", outcome="still_valid", after="", today=filed
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is True
    assert preview["exact"] is True
    assert "updated: 2026-03-14" in preview["after"]

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    written = (vault / _EDIT_NOTE).read_text(encoding="utf-8")
    assert "updated: 2026-03-14" in written
    assert written == preview["after"], "the preview's bytes are the written bytes"


def test_dismissing_a_note_edit_settles_it_without_refusing_it_forever(
    tmp_path: Path,
) -> None:
    """A dismissal releases the check, and leaves an expiring cooldown behind.

    Not a "rejected forever" marker: the note is not asked about again this
    month, and the moment it is edited the check no longer describes it and a
    fresh pass may propose it again.
    """
    from ciao import note_edit_proposals as nep
    from ciao import note_verification as nv
    from ciao.memory_receipts import content_revision

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.settled != ""
    assert settled.accepted is False
    check = nv.read_note_checks(vault)[_EDIT_NOTE]
    assert check.proposal_id == ""
    assert nv.should_check(
        vault, _EDIT_NOTE, content_revision(_EDIT_PLAIN), today=date.today()
    ) is False, "the cooldown is still running"


def test_a_batch_with_a_note_edit_row_returns_an_explicit_result(
    tmp_path: Path,
) -> None:
    """Both ways, and neither may fall through to the `[review]` wording.

    "No destination yet" describes a row with nowhere to go, which is the
    opposite of a row whose destination is the note it names — and a client
    reading it would show a decision that has not been asked for.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    accepted = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert accepted.status_code == 200, accepted.json()
    results = accepted.json()["results"]
    assert len(results) == 1
    assert results[0]["action"] == "note_edit"
    assert results[0]["promoted"] is True
    assert "no destination yet" not in json.dumps(results)
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH

    # And a row it cannot apply says so, per row, and stays queued.
    config, _proposal = _note_edit_vault(
        tmp_path / "conflict",
        text_on_disk=_EDIT_PLAIN + "\nA line somebody added by hand.\n",
    )
    client = _client(config)
    row = _note_edit_row(client)

    refused = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert refused.status_code == 200, refused.json()
    result = refused.json()["results"][0]
    assert result["promoted"] is False
    assert result["conflict"] is True
    assert "no destination yet" not in result.get("error", "")
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_batch_dismiss_settles_a_note_edit_too(tmp_path: Path) -> None:
    """A bulk "reject these" must settle what it removed, or the check keeps
    holding a note with nothing in the queue to settle it."""
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(
        "/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]}
    )

    assert resp.status_code == 200, resp.json()
    assert client.get("/api/proposals").json()["rows"] == []
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.accepted is False


def test_a_note_edit_without_its_record_keeps_the_bullet(tmp_path: Path) -> None:
    """The record IS the operation. Without it there is nothing to apply, so the
    row survives for the owner to dismiss rather than resolving into a decision
    that was not made."""
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    nep.sidecar_path(config, "personal", proposal.id).unlink()
    client = _client(config)
    row = _note_edit_row(client)
    assert row["note_edit"]["can_accept"] is False

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "no longer on file" in resp.json()["error"]
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_PLAIN


def test_a_write_that_cannot_be_settled_keeps_the_row_and_says_why(
    tmp_path: Path, monkeypatch
) -> None:
    """The write landed; the decision did not. The row has to survive.

    An ``ok=True`` here removes the bullet, and the bullet is the only thing left
    to act on: the sidecar stays unsettled, the check keeps this revision pinned
    to a proposal nobody is being asked about, and
    ``note_verification._check_settles`` suppresses the note for good — for a
    re-stamp of an already-current note, whose revision never moves, that means
    it is never verified again. So the accept refuses with what landed and where,
    and the row stays for the dismissal that settles it.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    def _read_only(*_args: Any, **_kwargs: Any) -> Any:
        raise nep.NoteEditSidecarError("the sidecar is read-only")

    monkeypatch.setattr(nep, "settle_note_edit", _read_only)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    error = resp.json()["error"]
    assert f"{_EDIT_NOTE} was written and the receipt is" in error
    assert "the sidecar is read-only" in error
    assert "Dismiss this row" in error
    # The write stands and the row is still there to be acted on.
    assert (vault / _EDIT_NOTE).read_text(encoding="utf-8") == _EDIT_FOURTH
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    unsettled = nep.read_sidecar(config, "personal", proposal.id)
    assert unsettled is not None and unsettled.settled == ""

    # The transient failure is over, and the row is the recovery path: a
    # dismissal settles the record it could not.
    monkeypatch.undo()
    dismissed = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert dismissed.status_code == 200, dismissed.json()
    assert client.get("/api/proposals").json()["rows"] == []
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.settled != ""
    assert settled.accepted is False, "a dismissal is a refusal, not an accept"


def test_a_retire_that_cannot_be_settled_keeps_the_row_too(
    tmp_path: Path, monkeypatch
) -> None:
    """A retirement is the one write a person cannot walk back by editing.

    So the row that says the note is in the trash has to stay until the record of
    the decision is on disk: a check pinned to a proposal that is no longer
    queued suppresses a note nothing will ever ask about again.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _note_edit_row(client)

    def _read_only(*_args: Any, **_kwargs: Any) -> Any:
        raise nep.NoteEditSidecarError("the sidecar is read-only")

    monkeypatch.setattr(nep, "settle_note_edit", _read_only)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert "could not be settled" in resp.json()["error"]
    assert not (vault / _EDIT_NOTE).exists(), "the note really was trashed"
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]
    unsettled = nep.read_sidecar(config, "personal", proposal.id)
    assert unsettled is not None and unsettled.settled == ""


def test_a_dismiss_that_cannot_be_settled_keeps_the_bullet(tmp_path: Path) -> None:
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    nep.sidecar_path(config, "personal", proposal.id).unlink()
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss")

    assert resp.status_code == 409, resp.json()
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def _undo_client(config: CiaoConfig) -> TestClient:
    """A client that also serves the undo the History card calls.

    The accept and the undo are two routes on the same decision, so a test that
    drove one over HTTP and the other as a function would not prove the pair
    works: the receipt is the only thing between them.
    """
    from ciao.web.routes_api import memory_receipt_undo

    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
            Route(
                "/api/memory/receipts/{id}/undo", memory_receipt_undo, methods=["POST"]
            ),
        ]
    )
    app.state.config = config
    return TestClient(app)


def test_undo_reservation_resolves_actual_receipt_workspace(tmp_path: Path) -> None:
    from ciao import memory_receipts
    from ciao.web.proposal_reservations import ReservationRegistry, reserve_proposal_operation
    config = _default_vault(tmp_path)
    client = _undo_client(config)
    row = _accept_kind_row(client, "memory")
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    rid = next(item["id"] for item in memory_receipts.read_receipts(journal)
               if item.get("kind") == "region_apply" and item.get("status") == memory_receipts.APPLIED)
    client.app.state.proposal_reservations = ReservationRegistry()
    client.app.router.routes.insert(0, Route("/api/proposals/reservations", reserve_proposal_operation, methods=["POST"]))
    scope = {"kind": "undo", "workspace": "", "targets": [rid]}
    result = client.post("/api/proposals/reservations", json=scope)
    assert result.status_code == 200 and result.json()["scope"]["workspace"] == "personal"
    assert client.post("/api/proposals/reservations", json={**scope, "workspace": "work"}).status_code == 404


@pytest.mark.parametrize("error_name,status,field", [
    ("RevisionConflict", 409, "conflict"), ("UndoUnsupported", 400, "undoable"),
])
def test_reserved_undo_known_refusal_seals_without_effect(tmp_path: Path, monkeypatch, error_name: str, status: int, field: str) -> None:
    from ciao import memory_receipts
    from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
    config = _default_vault(tmp_path)
    client = _undo_client(config)
    row = _accept_kind_row(client, "memory")
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    rid = next(item["id"] for item in memory_receipts.read_receipts(journal)
               if item.get("kind") == "region_apply" and item.get("status") == memory_receipts.APPLIED)
    registry = ReservationRegistry()
    client.app.state.proposal_reservations = registry
    scope = validate_scope({"kind": "undo", "workspace": "personal", "targets": [rid]}, set(config.workspace_names()))
    token = registry.reserve(scope)
    def refuse(*args, **kwargs):
        raise getattr(memory_receipts, error_name)("refused before undo")
    monkeypatch.setattr(memory_receipts, "undo_receipt", refuse)
    response = client.post(f"/api/memory/receipts/{rid}/undo?workspace=personal",
                           headers={"X-Ciao-Proposal-Reservation": token})
    assert response.status_code == status and response.json()["id"] == rid
    assert response.json()[field] is (field == "conflict")
    assert row["text"] in _region_entries(config, "personal", "memory")
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_reserved_receipt_undo_survives_cancel_and_rejects_wrong_scope(tmp_path: Path, monkeypatch, durable: bool) -> None:
    import asyncio
    import httpx
    from ciao import memory_receipts
    from ciao.web.proposal_reservations import ReservationRegistry, seal_proposal_operation, validate_scope
    config = _default_vault(tmp_path)
    client = _undo_client(config)
    row = _accept_kind_row(client, "memory")
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    receipt = next(item for item in memory_receipts.read_receipts(journal)
                   if item.get("kind") == "region_apply" and item.get("status") == memory_receipts.APPLIED)
    rid = receipt["id"]
    registry = _test_proposal_registry(tmp_path, durable)
    client.app.state.proposal_reservations = registry
    client.app.router.routes.insert(0, Route("/api/proposals/reservations/seal", seal_proposal_operation, methods=["POST"]))
    raw_scope = {"kind": "undo", "workspace": "personal", "targets": [rid]}
    scope = validate_scope(raw_scope, set(config.workspace_names()))
    wrong = registry.reserve(validate_scope({**raw_scope, "workspace": "work"}, set(config.workspace_names())))
    token = registry.reserve(scope)
    entered, release = threading.Event(), threading.Event()
    original = memory_receipts.undo_receipt
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(memory_receipts, "undo_receipt", held)
    path = f"/api/memory/receipts/{rid}/undo?workspace=personal"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as ac:
        assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": wrong})).status_code == 409
        task = asyncio.create_task(ac.post(path, headers={"X-Ciao-Proposal-Reservation": token}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            seal = {"id": token, "scope": raw_scope, "inspected": True}
            assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 409
            assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": token})).status_code == 409
        finally:
            release.set()
        await asyncio.gather(*tuple(registry._tasks))
        await asyncio.sleep(0)
        assert (await ac.post("/api/proposals/reservations/seal", json=seal)).status_code == 200
        assert (await ac.post(path, headers={"X-Ciao-Proposal-Reservation": token})).status_code == 409
    assert row["text"] not in _region_entries(config, "personal", "memory")


def test_undoing_an_accepted_category_restores_the_yaml_and_every_note(
    tmp_path: Path,
) -> None:
    """Acceptance: the receipt undoes the registry entry AND every retyped note.

    Driven through the two routes, over the real cluster: the notes carry
    ``type: Recipe Book`` and the id derived from it is ``recipe-book``, so each
    retype is a real change of bytes and the restore is observed rather than
    asserted against files that never moved. A receipt that recorded no image of
    what the retype left behind could not tell an unrelated later edit from its
    own work, and would refuse the undo of every category it had just applied.
    """
    from ciao.memory_receipts import journal_path, read_receipts

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    journals = vault / "Journals"
    registry = config.agent_vault_root("personal") / "entity-types.yaml"
    before = {note.name: note.read_text(encoding="utf-8") for note in journals.iterdir()}
    assert not registry.exists(), "the fixture must start with no category file"
    client = _undo_client(config)
    row = _accept_kind_row(client, "category")

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    assert registry.exists()
    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "category_apply"
    ][-1]
    # Every note really changed, and every note's after-image says so.
    images = {
        entry["path"]: entry["after"] for entry in receipt["category_notes"]
    }
    assert set(images) == {str(note) for note in journals.iterdir()}
    for note in journals.iterdir():
        assert note.read_text(encoding="utf-8") != before[note.name]
        assert images[str(note)] == note.read_text(encoding="utf-8")

    undo = client.post(
        f"/api/memory/receipts/{receipt['id']}/undo", params={"workspace": "personal"}
    )

    assert undo.status_code == 200, undo.json()
    # The vault had no category file before, so the undo leaves none behind.
    assert not registry.exists()
    for name, text in before.items():
        assert (journals / name).read_text(encoding="utf-8") == text


def test_an_accepted_category_is_not_offered_again(tmp_path: Path) -> None:
    """The accepted id is canonical, so the cluster it came from is not drift.

    The accept writes ``entity-types.yaml`` at the AGENT vault root — the same
    file ``GET``/``PATCH /api/memory/entity-types`` reads, and a different
    directory from the notes on an install that has not re-rooted. A pass that
    measured the notes against the registry under the notes root would keep
    seeing drift: the bullet comes back, and accepting it 400s on a duplicate id
    and a duplicate folder that no owner edit can resolve.
    """
    from ciao import entity_types
    from ciao import curation_run as cr
    from ciao.memory_tool import ensure_regions

    config = _category_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    # A fourth note, already carrying the accepted id: the cluster is intact and
    # every note in it is canonical.
    (vault / "Journals" / "Four.md").write_text(
        f"---\ntype: {_CATEGORY_ID}\n---\n# Four\n", encoding="utf-8"
    )
    guide = config.agent_root("personal") / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    ensure_regions(guide)

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=entity_types.load_entity_types(
            config.agent_vault_root("personal")
        ),
        workspace_dir=config.workspace_root,
    )

    assert cr.PASS_CATEGORIES not in {item.pass_id for item in worklist.items}
    queue = (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "[category" not in queue


def _append_category_row(
    config: CiaoConfig,
    *,
    category_id: str,
    raw_type: str,
    folder: str,
    names: tuple[str, str, str],
) -> None:
    """A second ``[category]`` row over its own cluster, in the same queue."""
    from ciao.memory_proposals import MemoryProposal, append_proposals
    from ciao.vocabulary_proposals import write_category_sidecar

    vault = config.workspace_vault_root("personal")
    journals = vault / "Journals"
    for name in names:
        (journals / name).write_text(
            f"---\ntype: {raw_type}\n---\n# {Path(name).stem}\n", encoding="utf-8"
        )
    write_category_sidecar(
        vault,
        {
            "id": category_id,
            "label": category_id.replace("-", " ").title(),
            "folder": folder,
            "description": f"Proposed from {len(names)} notes already typed {raw_type}.",
            "source_type": raw_type,
            "paths": [f"memory-vault/Journals/{name}" for name in names],
            "declined": False,
        },
    )
    append_proposals(
        [
            MemoryProposal(
                target="category",
                payload=category_id,
                text=(
                    f"{category_id.replace('-', ' ').title()} → {folder}: "
                    f"Proposed from {len(names)} notes already typed {raw_type}."
                ),
                source_section=f"{len(names)} notes typed {raw_type}",
            )
        ],
        vault,
    )


def test_two_category_accepts_both_persist(tmp_path: Path) -> None:
    """Two clusters accepted one after the other: both categories are on disk.

    Each accept appends to the same registry file, so this is the case where a
    second write built from a stale snapshot drops the first entry — silently,
    with the second accept still reporting success.
    """
    config = _category_vault(tmp_path)
    _append_category_row(
        config,
        category_id="cook-book",
        raw_type="Cook Book",
        folder="Cook Books",
        names=("Four.md", "Five.md", "Six.md"),
    )
    client = _client(config)
    first = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{first['id']}/accept").status_code == 200
    second = _accept_kind_row(client, "category")
    assert client.post(f"/api/proposals/{second['id']}/accept").status_code == 200

    ids = _registry_ids(config)
    assert _CATEGORY_ID in ids
    assert "cook-book" in ids
    assert client.get("/api/proposals").json()["rows"] == []


def test_the_registry_is_read_inside_the_lock_that_writes_it(
    tmp_path: Path, monkeypatch
) -> None:
    """The read, the append and the write are one transaction.

    Loaded before the lock was taken, two accepts could both read the list before
    either wrote, append to the same snapshot, and the second write would replace
    the first — one category lost with no error anywhere. The lock is the whole
    fix, so the test holds it from here and watches the accept refuse to read
    until it is released. The ``started`` event is what makes that a claim about
    the ordering rather than about how fast the thread happened to start.
    """
    from ciao import entity_types
    from ciao.memory_receipts import queue_lock

    config = _category_vault(tmp_path)
    registry = config.agent_vault_root("personal") / "entity-types.yaml"
    first, second = _client(config), _client(config)
    row = _accept_kind_row(first, "category")
    started, read_registry = threading.Event(), threading.Event()
    real_accept = proposal_service._accept_category_row
    real_load = entity_types.load_entity_types

    def announce(cfg, accepted_row):
        started.set()
        return real_accept(cfg, accepted_row)

    def watched_load(vault):
        read_registry.set()
        return real_load(vault)

    monkeypatch.setattr(proposal_service, "_accept_category_row", announce)
    monkeypatch.setattr(entity_types, "load_entity_types", watched_load)
    accepted: dict[str, Any] = {}

    def accept() -> None:
        accepted["resp"] = second.post(f"/api/proposals/{row['id']}/accept")

    thread = threading.Thread(target=accept)
    with queue_lock(registry):
        thread.start()
        assert started.wait(10), "the accept never reached its category handler"
        # The lock is held here, so the accept is parked before it reads the
        # list it is about to append to.
        assert not read_registry.wait(2), "the registry was read outside the lock"
    thread.join(20)

    assert read_registry.is_set()
    assert accepted["resp"].status_code == 200, accepted["resp"].json()
    assert _CATEGORY_ID in _registry_ids(config)


def test_batch_accept_does_not_promote_before_the_receipt_can_be_written(
    tmp_path: Path,
) -> None:
    """The batch's pre-flight, the same one the single-row accept runs.

    `_rewrite_queue_batch` is what raises `QueueReceiptUnavailable`, and it runs
    after every destination mutation — so an unwritable journal returned 503
    with the learning already appended (or a region written, a doc folded) and
    every bullet still queued, and the operator's retry applied the whole batch
    a second time.
    """
    config = _learnings_vault(tmp_path)
    # A directory where the journal file belongs: the receipt append cannot
    # succeed, the same way a read-only vault leaves it.
    journal = config.workspace_vault_root("personal") / "Workspace" / "Memory-Receipts.jsonl"
    journal.mkdir()
    client = _client(config)
    row = _accept_kind_row(client, "learnings")

    resp = client.post(
        "/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}
    )

    assert resp.status_code == 503, resp.json()
    assert resp.json()["no_effect"] is True and resp.json()["ids"] == [row["id"]]
    # The fact must NOT have been written: a 503 with the destination already
    # mutated is what turns the operator's retry into a double promotion.
    assert _learnings_count(config) == 0
    # And the row is still queued, so the retry has something to apply.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_single_accept_journal_preflight_explicit_no_effect(tmp_path: Path) -> None:
    config = _learnings_vault(tmp_path)
    journal = config.workspace_vault_root("personal") / "Workspace" / "Memory-Receipts.jsonl"
    journal.mkdir()
    client = _client(config)
    row = _accept_kind_row(client, "learnings")
    response = client.post(f"/api/proposals/{row['id']}/accept")
    assert response.status_code == 503
    assert response.json()["id"] == row["id"] and response.json()["no_effect"] is True
    assert _learnings_count(config) == 0


def test_two_concurrent_accepts_promote_the_row_once(
    tmp_path: Path, monkeypatch
) -> None:
    """Two tabs accepting one proposal must not write the fact twice.

    An accept promotes first and rewrites the queue last, so both requests
    passed the id lookup, both promoted — the recurrence count incremented
    twice — and only then contended on `_rewrite_queue_single`, where the loser
    removed nothing and still reported success.
    """
    config = _learnings_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    from ciao.memory_proposals import append_learning

    # Seed the learning at (x1) so each promotion is visible as an increment.
    append_learning(vault, _LEARNINGS_TEXT, workspace="personal")
    assert _learnings_count(config) == 1

    entered = threading.Event()
    release = threading.Event()
    real_accept = proposal_service._accept_learnings_row
    calls: list[str] = []

    def blocking_accept(cfg, row):
        """Hold the FIRST request inside its promotion: the real race window."""
        calls.append(row["id"])
        if len(calls) == 1:
            entered.set()
            release.wait(10)
        return real_accept(cfg, row)

    monkeypatch.setattr(proposal_service, "_accept_learnings_row", blocking_accept)

    # Two clients, so the second request runs on its own event loop instead of
    # queueing behind the first one's blocked handler.
    first_client, second_client = _client(config), _client(config)
    pid = _accept_kind_row(first_client, "learnings")["id"]
    first: dict[str, Any] = {}

    def run_first() -> None:
        first["resp"] = first_client.post(f"/api/proposals/{pid}/accept")

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(10), "the first accept never reached its promotion"
        second = second_client.post(f"/api/proposals/{pid}/accept")
    finally:
        release.set()
        thread.join(20)

    assert _learnings_count(config) == 2, "the fact was promoted twice"
    assert first["resp"].status_code == 200, first["resp"].json()
    # The loser is told the row is spoken for instead of reporting a success it
    # never performed.
    assert second.status_code == 409, second.json()
    # One promotion, one removal: the bullet is gone exactly once.
    assert first_client.get("/api/proposals").json()["rows"] == []


def test_an_accept_revalidates_the_row_another_resolver_took(
    tmp_path: Path, monkeypatch
) -> None:
    """The other half of the guard: a resolver outside this process.

    The claim is in-process (holding the file lock across a promotion would
    block every other queue writer for a model call), so the CLI, the undo
    path or a second server can still resolve a row between this request's scan
    and its promotion. Promoting anyway writes the fact a second time for a
    bullet this request then fails to remove.
    """
    config = _learnings_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    from ciao.memory_proposals import append_learning

    append_learning(vault, _LEARNINGS_TEXT, workspace="personal")
    queue = vault / "Workspace" / "Memory-Proposals.md"
    real_probe = routes_api._accept_journal_writable

    def steal_the_row(cfg, workspace, queue_path):
        """Another resolver lands in the window the pre-flight sits in."""
        text = queue.read_text(encoding="utf-8")
        queue.write_text(
            "\n".join(
                line for line in text.splitlines() if "[learnings]" not in line
            )
            + "\n",
            encoding="utf-8",
        )
        return real_probe(cfg, workspace, queue_path)

    monkeypatch.setattr(routes_api, "_accept_journal_writable", steal_the_row)
    client = _client(config)
    pid = _accept_kind_row(client, "learnings")["id"]

    resp = client.post(f"/api/proposals/{pid}/accept")

    assert resp.status_code == 409, resp.json()
    assert _learnings_count(config) == 1, "the fact was promoted a second time"


@pytest.mark.parametrize("batch", [False, True])
def test_learning_accept_does_not_write_after_row_is_replaced(
    tmp_path: Path, monkeypatch, batch: bool
) -> None:
    from ciao.web import proposal_service
    from ciao.memory_proposals import read_decisions

    config = _learnings_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "learnings")
    queue = proposal_service._proposals_file(config, "personal")
    learning = config.workspace_vault_root("personal") / "Workspace" / "Learnings.md"
    before = learning.read_bytes() if learning.exists() else None
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)
    replacement = queue.read_text(encoding="utf-8").replace("Decisions", "Other source")

    def race(*args, **kwargs):
        queue.write_text(replacement, encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
        assert response.status_code == 409
    assert (learning.read_bytes() if learning.exists() else None) == before
    assert queue.read_text(encoding="utf-8") == replacement
    assert not read_decisions(queue)


@pytest.mark.parametrize("action", ["accept", "dismiss"])
@pytest.mark.parametrize("batch", [False, True])
def test_learning_duplicate_shift_fails_before_effect(tmp_path: Path, monkeypatch, action: str, batch: bool) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config = _learnings_vault(tmp_path)
    queue = proposal_service._proposals_file(config, "personal")
    original = queue.read_text(encoding="utf-8")
    bullet = next(line for line in original.splitlines() if "[learnings]" in line)
    queue.write_text(original + bullet + "\n", encoding="utf-8")
    client = _client(config)
    row = _accept_kind_row(client, "learnings")
    learning = config.workspace_vault_root("personal") / "Workspace" / "Learnings.md"
    before = learning.read_bytes() if learning.exists() else None
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        lines = queue.read_text(encoding="utf-8").splitlines(keepends=True)
        del lines[next(i for i, line in enumerate(lines) if "[learnings]" in line)]
        queue.write_text("".join(lines), encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": action, "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/{action}").status_code == 409
    assert (learning.read_bytes() if learning.exists() else None) == before
    assert queue.read_text(encoding="utf-8").count(bullet) == 1
    assert not read_decisions(queue)


# ---- Retry reconciliation from the review path ----------------------------


_COMPETING_QUEUE = """# Memory Proposals

## 2026-09-19 curation pass (this pass)

- [memory] Office is in Berlin.  _(from: Decisions)_
"""


def _competing_vault(tmp_path: Path) -> CiaoConfig:
    """A queued fact that supersedes an entry already in the region.

    The archive-time reconcile deferred it, so it is sitting in the queue with
    its competitor still live. Accepting it plainly appends both.
    """
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(config, "personal", _COMPETING_QUEUE)
    from ciao import memory_tool as mt

    guide = config.agent_root("personal") / "AGENTS.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text("# Guide\n\n", encoding="utf-8")
    mt.ensure_regions(guide)
    mt.write_region(guide, "memory", ["Office is in Zurich. [2026-01-01]"])
    return config


def _memory_entries(config: CiaoConfig) -> list[str]:
    from ciao import memory_tool as mt

    entries, _diags = mt.read_region(config.agent_root("personal") / "AGENTS.md", "memory")
    return entries


def test_accept_without_reconcile_stays_model_free(tmp_path: Path, monkeypatch) -> None:
    """The default accept is still one synchronous write, no model call.

    A reconcile on every click costs the batch endpoint one timeout per row,
    which is why the retry is opt-in rather than the new default.
    """

    async def never(*args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("the plain accept must not call a model")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", never)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200


def test_region_replay_before_route_duplicate_decision_is_once(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts, memory_tool, proposal_actions

    config = _competing_vault(tmp_path)
    guide = config.agent_root("personal") / "AGENTS.md"
    memory_tool.write_region(guide, "memory", ["Office is in Berlin. [2026-01-01]"])
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    queue = proposal_service._proposals_file(config, "personal")
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    real = proposal_actions.record_decision
    called: list[str] = []

    def replay_first(path, **kwargs):
        assert kwargs["outcome"] == "duplicate"
        memory_receipts.recover_pending(journal=journal, config=config)
        recovered = memory_proposals.read_decisions(queue, include_history_only=False)
        assert len(recovered) == 1
        for key in ("via", "source", "destination", "outcome", "proposal_id", "receipt_id", "queue_receipt_id", "reason"):
            assert recovered[0][key] == kwargs.get(key, "")
        called.append(kwargs["queue_receipt_id"])
        return real(path, **kwargs)

    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    response = client.post(f"/api/proposals/{row['id']}/accept", json={"reason": "already known"})
    assert response.status_code == 200
    decisions = memory_proposals.read_decisions(queue, include_history_only=False)
    assert len(decisions) == 1
    assert decisions[0]["queue_receipt_id"] == called[0]


@pytest.mark.parametrize("batch,action", [(False, "dismiss"), (True, "dismiss"), (True, "accept")])
def test_pwa_replay_preserves_decision_metadata(tmp_path: Path, monkeypatch, batch: bool, action: str) -> None:
    from ciao import memory_proposals, memory_receipts, proposal_actions

    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    queue = proposal_service._proposals_file(config, "personal")
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    original = proposal_actions.record_decision
    seen = []
    def replay_first(path, **kwargs):
        memory_receipts.recover_pending(journal=journal, config=config)
        decisions = memory_proposals.read_decisions(queue, include_history_only=False)
        assert len(decisions) == 1
        for key in ("via", "source", "destination", "outcome", "proposal_id", "receipt_id", "queue_receipt_id", "reason"):
            assert decisions[0][key] == kwargs.get(key, "")
        seen.append(True)
        return original(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": action, "ids": [row["id"]]})
    else:
        response = client.post(f"/api/proposals/{row['id']}/{action}", json={"reason": "not needed"})
    assert response.status_code == 200 and seen
    assert len(memory_proposals.read_decisions(queue, include_history_only=False)) == 1


def test_batch_replay_preserves_each_selected_decision(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts, proposal_actions

    config = _default_vault(tmp_path)
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    selected = [row for row in rows if row["workspace"] == "personal" and row["kind"] in {"memory", "profile"}][:2]
    assert len(selected) == 2
    queue = proposal_service._proposals_file(config, "personal")
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    original = proposal_actions.record_decision
    seen = []
    def replay_first(path, **kwargs):
        memory_receipts.recover_pending(journal=journal, config=config)
        decisions = memory_proposals.read_decisions(queue, include_history_only=False)
        assert len(decisions) == 2
        recovered = next(item for item in decisions if item["proposal_id"] == kwargs["proposal_id"])
        for key in ("via", "source", "destination", "outcome", "proposal_id", "receipt_id", "queue_receipt_id", "reason"):
            assert recovered[key] == kwargs.get(key, "")
        seen.append(kwargs["proposal_id"])
        return original(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"] for row in selected]})
    assert response.status_code == 200 and len(seen) == 2
    assert len(memory_proposals.read_decisions(queue, include_history_only=False)) == 2


@pytest.mark.parametrize("tamper", ["none", "outcome", "receipt"])
def test_mixed_batch_replay_preserves_per_row_outcomes(tmp_path: Path, monkeypatch, tamper: str) -> None:
    from contextlib import contextmanager
    from ciao import memory_proposals, memory_receipts, memory_tool, proposal_actions

    config = _competing_vault(tmp_path)
    guide = config.agent_root("personal") / "AGENTS.md"
    memory_tool.write_region(guide, "memory", ["Office is in Berlin. [2026-01-01]"])
    _write_queue(config, "personal", "## pass\n\n"
                 "- [memory] Office is in Berlin. _(from: Old source)_\n"
                 "- [memory] Prefers thorough testing. _(from: New source)_\n")
    client = _client(config)
    rows = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "memory"]
    assert len(rows) == 2
    queue = proposal_service._proposals_file(config, "personal")
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    original_record = proposal_actions.record_decision
    original_multi = memory_receipts.queue_resolution_multi
    if tamper != "none":
        @contextmanager
        def corrupted(path, removals, **kwargs):
            for item in removals:
                if item.get("decision", {}).get("decision_outcome") == "written":
                    item["decision"]["decision_outcome" if tamper == "outcome" else "decision_receipt_id"] = "corrupt"
            with original_multi(path, removals, **kwargs) as result:
                yield result
        monkeypatch.setattr(memory_receipts, "queue_resolution_multi", corrupted)
    seen = {}
    def replay_first(path, **kwargs):
        memory_receipts.recover_pending(journal=journal, config=config)
        decisions = memory_proposals.read_decisions(queue, include_history_only=False)
        assert len(decisions) == 2
        actual = next(item for item in decisions if item["proposal_id"] == kwargs["proposal_id"])
        for key in ("via", "source", "destination", "outcome", "proposal_id", "receipt_id", "queue_receipt_id", "reason"):
            assert actual[key] == kwargs.get(key, "")
        seen[actual["proposal_id"]] = actual
        return original_record(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    def request():
        return client.post("/api/proposals/batch", json={"action": "accept", "ids": [r["id"] for r in rows]})
    if tamper != "none":
        with pytest.raises(AssertionError):
            request()
        return
    assert request().status_code == 200
    assert len(seen) == 2
    assert {item["outcome"] for item in seen.values()} == {"duplicate", "written"}
    for row in rows:
        decision = seen[row["id"]]
        assert decision["source"] == row["source"] and decision["destination"] == "ciao:memory"
        assert decision["queue_receipt_id"]
        if decision["outcome"] == "written":
            assert decision["receipt_id"]
    assert len(memory_proposals.read_decisions(queue, include_history_only=False)) == 2


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("over", [False, True])
def test_selected_queue_image_capacity_before_region_effect(tmp_path: Path, monkeypatch, batch: bool, over: bool) -> None:
    from ciao import memory_proposals, memory_receipts, proposal_actions

    config = _competing_vault(tmp_path)
    queue = proposal_service._proposals_file(config, "personal")
    initial = queue.read_text(encoding="utf-8")
    desired = memory_receipts.MAX_IMAGE_CHARS + int(over)
    queue.write_text(initial + "#" + "x" * (desired - len(initial) - 2) + "\n", encoding="utf-8")
    assert len(queue.read_text(encoding="utf-8")) == desired
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    guide = config.agent_root("personal") / "AGENTS.md"
    before_guide, before_queue = guide.read_bytes(), queue.read_bytes()
    replayed = []
    original = proposal_actions.record_decision
    def replay_first(path, **kwargs):
        memory_receipts.recover_pending(
            journal=memory_receipts.journal_path(config.workspace_vault_root("personal"), None), config=config
        )
        decisions = memory_proposals.read_decisions(queue, include_history_only=False)
        assert len(decisions) == 1 and decisions[0]["queue_receipt_id"] == kwargs["queue_receipt_id"]
        replayed.append(True)
        return original(path, **kwargs)
    monkeypatch.setattr(proposal_actions, "record_decision", replay_first)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
    else:
        response = client.post(f"/api/proposals/{row['id']}/accept")
    if over:
        assert guide.read_bytes() == before_guide
        assert queue.read_bytes() == before_queue
        assert "destination_effect" not in str(response.json())
    else:
        assert response.status_code == 200
        assert replayed
        assert guide.read_bytes() != before_guide
        journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
        receipts = memory_receipts.read_receipts(journal)
        selected = [r for r in receipts if r.get("kind") == "queue_resolve" and (r.get("proposal_id") or r.get("selected_proposals"))]
        assert selected and selected[-1]["before_text"] is not None and selected[-1]["after_text"] is not None


def test_region_accept_does_not_write_when_selection_disappears_during_reconcile(
    tmp_path: Path, monkeypatch
) -> None:
    from ciao.web import proposal_service

    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    guide = config.agent_root("personal") / "AGENTS.md"
    before = guide.read_bytes()
    queue = proposal_service._proposals_file(config, "personal")
    replacement = "# Memory Proposals\n"

    async def race(*args, **kwargs):
        queue.write_text(replacement, encoding="utf-8")
        return None, None

    monkeypatch.setattr(proposal_service, "_plan_accept_reconcile", race)
    response = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")
    assert response.status_code == 409
    assert guide.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == replacement
    from ciao.memory_proposals import read_decisions
    assert not read_decisions(queue)


def test_region_preparation_does_not_create_guide(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from ciao.web import proposal_service

    config = _competing_vault(tmp_path)
    guide = config.agent_root("personal") / "AGENTS.md"
    guide.unlink()
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    row = _accept_kind_row(_client(config), "memory")

    async def plan(*args, **kwargs):
        assert not guide.exists()
        assert queue.read_bytes() == before
        return None, None

    monkeypatch.setattr(proposal_service, "_plan_accept_reconcile", plan)
    decision, refusal = asyncio.run(proposal_service._prepare_region_row(
        config, row, reconcile=True
    ))
    assert decision is None and refusal is None
    assert not guide.exists()
    assert queue.read_bytes() == before


def test_batch_region_stale_before_commit_does_not_write(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import proposal_service

    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    guide = config.agent_root("personal") / "AGENTS.md"
    before = guide.read_bytes()
    queue = proposal_service._proposals_file(config, "personal")
    real = proposal_service._rewrite_queue_batch

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, "_rewrite_queue_batch", race)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
    assert guide.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == "# Memory Proposals\n"
    assert not response.json().get("results", [{}])[0].get("dismissed", False)
    from ciao.memory_proposals import read_decisions
    assert not read_decisions(queue)


def test_reconcile_on_accept_replaces_the_competing_entry(
    tmp_path: Path, monkeypatch
) -> None:
    """`?reconcile=1` resolves a deferred fact instead of appending beside it."""

    async def merges(prompt: str, **kwargs: object) -> str:
        assert "Office is in Zurich." in prompt
        return '[{"action": "update", "index": 1, "text": "Office is in Berlin."}]'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", merges)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 200, resp.json()
    entries = _memory_entries(config)
    assert len(entries) == 1, "the obsolete entry and its replacement are both live"
    assert entries[0].startswith("Office is in Berlin.")


def test_a_retry_that_cannot_decide_keeps_the_row_queued(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed retry is not a licence to append; the bullet survives."""

    async def timing_out(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("reconcile timed out")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 409, resp.json()
    # The reason and the competing entry are on the response, not only in a log.
    assert "Office is in Zurich." in resp.json()["error"]
    assert _memory_entries(config) == ["Office is in Zurich. [2026-01-01]"]
    assert [r["kind"] for r in client.get("/api/proposals").json()["rows"]] == ["memory"]


def test_a_deferral_names_what_it_was_weighed_against(
    tmp_path: Path, monkeypatch
) -> None:
    """The refusal is structured, not only prose.

    The review UI puts the reason and the competing entries next to the retry
    button. Taking them back out of the error sentence would make the wording
    of that sentence part of the contract, so both are carried as fields —
    ``deferred`` marks the one refusal another retry can resolve on its own,
    which is why it and an over-cap region must not look alike.
    """

    async def timing_out(prompt: str, **kwargs: object) -> str:
        raise TimeoutError("reconcile timed out")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", timing_out)
    config = _competing_vault(tmp_path)
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    body = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1").json()

    assert body["deferred"] is True
    assert body["no_effect"] is True
    assert body["reason"], "a deferral with no reason is an ordinary refusal"
    assert body["competing"] == ["Office is in Zurich. [2026-01-01]"]


def test_an_unshaped_refusal_is_not_marked_deferred(
    tmp_path: Path, monkeypatch
) -> None:
    """Only a deferral offers a retry, because only a deferral can be retried.

    An event-shaped fact is refused no matter how many times it is reconciled;
    marking it retryable would put a button on the row that cannot do what it
    says.
    """

    async def unused(*args: object, **kwargs: object) -> str:  # pragma: no cover
        raise AssertionError("an unshaped fact is rejected before any model call")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", unused)
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "personal",
        "# Memory Proposals\n\n## 2026-09-19 curation pass (this pass)\n\n"
        "- [memory] User said the office moved.  _(from: Decisions)_\n",
    )
    client = _client(config)
    row = _accept_kind_row(client, "memory")

    resp = client.post(f"/api/proposals/{row['id']}/accept?reconcile=1")

    assert resp.status_code == 409, resp.json()
    assert "deferred" not in resp.json()


# ---- History: a settled verification, in full and with its undo --------------
#
# The decision sidecar records the bullet's one line — that text is the dedupe
# key, and the queue needs nothing more. Everything a reader of History needs to
# judge the decision lives in the note-edit record, so the route resolves each
# `note_edit` decision back to it.


def _history_rows(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/proposals/history").json()["rows"]


def test_history_shows_an_accepted_note_edit_with_its_evidence_and_undo(
    tmp_path: Path,
) -> None:
    """Before, after, why, on what, and the button that reverses it.

    Without the record a settled verification reads as one opaque sentence with
    a destination: nothing a reader could check a decision against months later.
    The undo is real, because the accept went through `note_receipts`.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    decision = next(
        r for r in _history_rows(client) if r.get("kind") == "note_edit"
    )
    detail = decision["note_edit"]
    assert detail["relative_path"] == _EDIT_NOTE
    assert detail["operation"] == "replace"
    assert detail["outcome"] == "update"
    assert detail["coverage"] == "complete"
    assert detail["before"] == _EDIT_PLAIN
    assert detail["after"] == _EDIT_FOURTH
    assert detail["reason"] == "the third floor no longer exists"
    assert detail["pending"] is False
    assert detail["accepted"] is True
    assert detail["settled"] != ""
    # A retirement is NOT this: reversible through Vault Review, not an undo.
    assert "reversible_by" not in decision
    assert decision["change"]["kind"] == "note_apply"
    assert decision["change"]["undoable"] is True
    assert decision["change"]["changed"] is True


def test_history_cites_the_evidence_a_verdict_rested_on(tmp_path: Path) -> None:
    """The citations travel with the decision, not just with the queue row.

    A check can be auto-applied only when every evidence row is a citation
    somebody could re-open, so the record of *why* a note was rewritten is the
    record of what was checked — and it is gone from the queue the moment the
    row is resolved.
    """
    from ciao import memory_receipts as mr
    from ciao import note_edit_proposals as nep

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    note = vault / _EDIT_NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(_EDIT_PLAIN.encode("utf-8"))
    citation = nep.nv.Evidence(
        source_type="url",
        source_ref="https://example.test/office-move",
        quoted="The office is now on the fourth floor.",
        supports="notes/office.md",
    )
    nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=_EDIT_NOTE,
        expected_revision=mr.content_revision(_EDIT_PLAIN),
        operation="replace",
        before=_EDIT_PLAIN,
        after=_EDIT_FOURTH,
        outcome="update",
        coverage="complete",
        evidence=(citation,),
        reason="the office moved up a floor",
    )
    client = _client(config)
    row = _note_edit_row(client)

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    detail = next(
        r for r in _history_rows(client) if r.get("kind") == "note_edit"
    )["note_edit"]
    assert len(detail["evidence"]) == 1
    assert detail["evidence"][0]["source_ref"] == "https://example.test/office-move"
    assert detail["evidence"][0]["quoted"].startswith("The office is now")
    assert detail["evidence"][0]["supports"] == _EDIT_NOTE


def test_history_shows_a_retirement_as_reversible_through_the_trash(
    tmp_path: Path,
) -> None:
    """No receipt, so no Undo — and emphatically not "nothing happened".

    A retirement journals to the review ledger, not the memory journal, so
    `_attach_change_receipts` finds nothing and the row would render as "no
    change snapshot available" beside a note that really was moved. The trash is
    where it went and Restore is the way back.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, operation="retire", outcome="retire", after=""
    )
    client = _client(config)
    row = _note_edit_row(client)

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    decision = next(
        r for r in _history_rows(client) if r.get("kind") == "note_edit"
    )
    assert decision["reversible_by"] == "restore"
    # The change itself is still described: the record knows it was a retirement,
    # and it carries no after-image because a retirement writes no text.
    assert decision["note_edit"]["operation"] == "retire"
    assert decision["note_edit"]["after"] == ""
    assert decision["note_edit"]["accepted"] is True


def test_history_shows_a_dismissed_verification_as_a_dismissal(tmp_path: Path) -> None:
    """A refusal wrote nothing, and must not read as a change.

    A dismissal leaves the note exactly as it was and settles the record with
    `accepted: False`, so the note is the before-image and there is no receipt —
    which is the honest "nothing was written" state, not a missing snapshot.
    """
    config, _proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)

    assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 200

    decision = next(
        r for r in _history_rows(client) if r.get("kind") == "note_edit"
    )
    assert decision["action"] == "dismissed"
    assert decision["note_edit"]["accepted"] is False
    assert decision["note_edit"]["pending"] is False
    assert decision["note_edit"]["before"] == _EDIT_PLAIN
    assert decision["note_edit"]["after"] == _EDIT_FOURTH, (
        "the after-image is what WOULD have been written, and nothing wrote it"
    )
    # The one receipt a dismissal does leave describes the QUEUE — the bullet it
    # removed — and not the note, which is exactly the distinction a reader needs:
    # undoing this brings the row back, it does not apply the edit.
    assert decision["change"]["kind"] == "queue_resolve"
    assert decision["change"]["destination"] == ""


def test_a_conflict_is_never_rendered_as_an_applied_change(tmp_path: Path) -> None:
    """A refused accept leaves the row queued and records no decision at all.

    The 409 is a conflict, not a failure, and the note is byte-identical — so
    there is nothing in History to show and nothing to undo, and the row the
    owner can still decide is the row that is still there.
    """
    config, _proposal = _note_edit_vault(
        tmp_path, text_on_disk=_EDIT_PLAIN + "\nA line somebody added by hand.\n"
    )
    client = _client(config)
    row = _note_edit_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert resp.json()["conflict"] is True
    assert _history_rows(client) == []
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_a_note_edit_whose_record_is_gone_is_left_unannotated(tmp_path: Path) -> None:
    """Same contract as a missing receipt: absence, never an invented record.

    The decision is still on record and the page still renders — one corrupt
    sidecar must not blank History or claim a before/after nobody can read.
    """
    from ciao import note_edit_proposals as nep

    config, proposal = _note_edit_vault(tmp_path)
    client = _client(config)
    row = _note_edit_row(client)
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200
    nep.sidecar_path(config, "personal", proposal.id).unlink()

    decision = next(
        r for r in _history_rows(client) if r.get("kind") == "note_edit"
    )

    assert "note_edit" not in decision
    assert decision["change"]["undoable"] is True, (
        "the receipt is the durable record; losing the sidecar loses the prose, "
        "not the ability to reverse the write"
    )


# ---- note_edit proposals: one entry, decided by a person -------------------
#
# The whole-note accept above writes the `after` image it was handed, because that
# image *is* the write. An entry operation goes through the range helper instead,
# so what is pinned here is the difference that buys: the write is the bytes the
# card said, the entry is proved to be the one a person read, and a retirement of
# one bullet is a deletion of one bullet.

_ENTRY_NOTE = "notes/office.md"
_ENTRY_PLAIN = (
    "---\ntype: note\nupdated: 2026-01-05\n---\n\n"
    "# Office\n\n"
    "- The office is on Via Verdi 12, third floor [verified: 2024-01-05]\n"
    "- The landlord is Bianchi\n"
)


def _entry_edit_vault(
    tmp_path: Path,
    *,
    operation: str = "replace_entry",
    outcome: str = "update",
    replacement: str = "- The office is on Via Verdi 12, fourth floor",
    today: date | None = None,
) -> tuple[CiaoConfig, Any, Any]:
    """A workspace holding one `[note_edit]` row about one entry of one note."""
    from ciao import memory_receipts as mr
    from ciao import note_edit_proposals as nep
    from ciao import note_entries as ne
    from ciao import note_receipts as nr

    config = _config(tmp_path)
    vault = config.workspace_vault_root("personal")
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    note = vault / _ENTRY_NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_bytes(_ENTRY_PLAIN.encode("utf-8"))
    entry = ne.parse_note_entries(
        _ENTRY_PLAIN, note_path=_ENTRY_NOTE, workspace="personal"
    ).entries[0]
    after = nr.compose_entry_edit(
        _ENTRY_PLAIN,
        entry,
        replacement=None if operation == nep.RETIRE_ENTRY else replacement,
        delete=operation == nep.RETIRE_ENTRY,
    )[0]
    proposal = nep.file_note_edit(
        config,
        workspace="personal",
        relative_path=_ENTRY_NOTE,
        expected_revision=mr.content_revision(_ENTRY_PLAIN),
        operation=operation,
        before=_ENTRY_PLAIN,
        after=after,
        outcome=outcome,
        coverage="complete",
        evidence=(),
        reason="the third floor no longer exists",
        today=today,
        entry_identity=entry.identity,
        entry_fingerprint=entry.fingerprint,
    )
    return config, proposal, entry


def test_an_entry_edit_preview_is_byte_exact_to_the_accept(
    tmp_path: Path,
) -> None:
    """The card's "after" IS the bytes the entry write produces.

    The same promise the whole-note accept makes, at the smaller unit — which is
    only true because the preview and the accept both resolve the entry by
    identity and compose through `note_receipts.compose_entry_edit`, over the same
    revision, rather than one recomputing what the other re-derives.
    """
    config, _proposal, entry = _entry_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    body = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert body["action"] == "note_edit"
    assert body["operation"] == "note_edit", "the card's own vocabulary, not a new one"
    assert body["destination"] == _ENTRY_NOTE
    assert body["exact"] is True
    assert body["can_accept"] is True
    assert body["before"] == _ENTRY_PLAIN
    assert body["after"] == (
        _ENTRY_PLAIN[: entry.start]
        + "- The office is on Via Verdi 12, fourth floor"
        + _ENTRY_PLAIN[entry.end :]
    )
    assert "one entry" in body["reason"], "the card says what it will touch"

    accepted = client.post(f"/api/proposals/{row['id']}/accept")

    assert accepted.status_code == 200, accepted.json()
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == body["after"]
    # And the note's other fact is still there: this was an edit, not a rewrite.
    assert "The landlord is Bianchi" in (vault / _ENTRY_NOTE).read_text(encoding="utf-8")


def test_an_entry_row_carries_the_bullet_it_is_about(tmp_path: Path) -> None:
    """Two verdicts on two bullets of one note must not render as the same row."""
    config, _proposal, entry = _entry_edit_vault(tmp_path)
    row = _accept_kind_row(_client(config), "note_edit")

    assert row["note_edit"]["entry"] == _ENTRY_PLAIN[entry.start : entry.end].strip()
    assert "Via Verdi 12, third floor" in row["note_edit"]["entry"]


def test_accepting_an_entry_edit_uses_the_note_receipt_and_the_entry(
    tmp_path: Path,
) -> None:
    """Journaled, revision-checked and undoable, with the entry named in the row.

    The receipt's provenance is what makes the write auditable at the width it
    happened at: History says which entry of which note was changed and under
    which verdict, which a whole-note row could not have said.
    """
    from ciao.memory_receipts import journal_path, read_receipts

    config, proposal, entry = _entry_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    result = client.post(f"/api/proposals/{row['id']}/accept").json()["result"]

    assert result["action"] == "note_edit"
    assert result["promoted"] is True and result["dismissed"] is True
    assert result["destination"] == _ENTRY_NOTE
    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "note_apply"
    ][-1]
    assert receipt["provenance"]["operation"] == "replace_entry"
    assert receipt["provenance"]["entry_identity"] == entry.identity
    assert receipt["provenance"]["proposal_id"] == proposal.proposal_id
    assert "region" not in result, "an entry edit is not a region write"

    from ciao import memory_receipts as mr

    undone = mr.undo_receipt(receipt["id"], vault_root=vault)
    assert undone["status"] == "undone"
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == _ENTRY_PLAIN


def test_retiring_an_entry_removes_that_span_and_is_undoable(
    tmp_path: Path,
) -> None:
    """Attended-only, reversible, and never a whole-file trash.

    The note itself is not retired: the other fact survives, the file is not in
    the review trash, and one click of undo puts the bullet back exactly.
    """
    from ciao import memory_receipts as mr

    config, _proposal, entry = _entry_edit_vault(
        tmp_path, operation="retire_entry", outcome="retire"
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is True
    assert "one entry" in preview["reason"]
    assert "third floor" not in preview["after"]
    assert "The landlord is Bianchi" in preview["after"]

    result = client.post(f"/api/proposals/{row['id']}/accept").json()["result"]

    after = (vault / _ENTRY_NOTE).read_text(encoding="utf-8")
    assert "third floor" not in after
    assert "The landlord is Bianchi" in after
    assert result["destination"] == _ENTRY_NOTE, "not a path in the review trash"
    assert not (vault / "Workspace" / "vault-review").exists()

    from ciao.memory_receipts import journal_path, read_receipts

    receipt = [
        r
        for r in read_receipts(journal_path(vault, None))
        if r["kind"] == "note_apply"
    ][-1]
    assert receipt["provenance"]["operation"] == "retire_entry"
    mr.undo_receipt(receipt["id"], vault_root=vault)
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == _ENTRY_PLAIN


def test_an_entry_that_is_not_the_one_the_record_describes_is_a_conflict(
    tmp_path: Path,
) -> None:
    """The second binding, and the one the whole-note pass cannot make.

    `expected_revision` proves the note is the one the record was filed against.
    Only the entry's identity and fingerprint prove the *bullet* is the one a
    person read — so a record naming a different fingerprint of that bullet is a
    conflict, and the accept refuses rather than replacing whatever now sits at
    those offsets.
    """
    from ciao import note_edit_proposals as nep

    config, proposal, _entry = _entry_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    # A record whose fingerprint names a version of the bullet nobody filed. The
    # note's revision still matches, so this is exactly the case the whole-note
    # rule cannot see.
    sidecar = nep.sidecar_path(config, "personal", proposal.id)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["proposal"]["entry_fingerprint"] = "b" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is False
    assert "not the text this edit was planned against" in preview["reason"]

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409, resp.json()
    assert resp.json()["conflict"] is True
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == _ENTRY_PLAIN
    assert "third floor" in (vault / _ENTRY_NOTE).read_text(encoding="utf-8")
    # The row survives, so the owner can re-read the entry and decide again.
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_an_entry_identity_the_note_no_longer_holds_is_a_conflict(
    tmp_path: Path,
) -> None:
    """No entry, no edit — and never a guess at the nearest bullet.

    The note is at exactly the revision the record was filed against, so the
    only thing left to prove is that the entry is still there. A bullet deleted
    by hand is a conflict, and the row stays.
    """
    from ciao import note_edit_proposals as nep

    config, proposal, entry = _entry_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    sidecar = nep.sidecar_path(config, "personal", proposal.id)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["proposal"]["entry_identity"] = "c" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]

    assert preview["can_accept"] is False
    assert "holds no entry with identity" in preview["reason"]
    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == _ENTRY_PLAIN
    assert entry.identity != "c" * 64


def test_accepting_an_entry_edit_files_the_check_for_the_text_it_left(
    tmp_path: Path,
) -> None:
    """The accept records the same post-check the unattended path does.

    An entry identity digests the entry's own fingerprint, so a `replace_entry`
    mints a new one: the row pinned to this proposal describes text the note no
    longer holds, and the re-worded fact that a person just decided on has no
    verdict at all. So the check is filed under the identity the *written* note
    gives the entry, and the row for the entry as it was goes in the same locked
    write — one row per fact, not one per version of it.
    """
    from ciao import entry_verification as ev
    from ciao import note_entries as ne

    config, _proposal, entry = _entry_edit_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "note_edit")
    replacement = "- The office is on Via Verdi 12, fourth floor"

    result = client.post(f"/api/proposals/{row['id']}/accept").json()["result"]

    assert result["promoted"] is True
    written = ne.parse_note_entries(
        (vault / _ENTRY_NOTE).read_text(encoding="utf-8"),
        note_path=_ENTRY_NOTE,
        workspace="personal",
    ).entries[0]
    assert written.text == replacement
    assert written.identity != entry.identity
    checks = ev.read_entry_checks(vault)
    assert set(checks) == {written.identity}
    assert checks[written.identity].content_fingerprint == written.fingerprint
    assert checks[written.identity].proposal_id == "", "the decision is settled"
    assert checks[written.identity].receipt_id
    assert not ev.should_check_entry(
        vault, written.identity, written.fingerprint, today=date.today()
    )


def test_retiring_an_entry_leaves_no_check_for_a_fact_that_is_gone(
    tmp_path: Path,
) -> None:
    """A retired bullet has no identity left to key a check on, and must not keep one.

    The entry is out of the note, so the row pinned to the proposal describes a
    fact that is not in the vault: it suppresses nothing (no identity resolves to
    it) and it is litter that grows one row per retirement. Dropping it is the
    honest end state, and it is the same write the replace path uses.
    """
    from ciao import entry_verification as ev

    config, _proposal, entry = _entry_edit_vault(
        tmp_path, operation="retire_entry", outcome="retire"
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    row = _accept_kind_row(client, "note_edit")

    assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 200

    assert "third floor" not in (vault / _ENTRY_NOTE).read_text(encoding="utf-8")
    assert entry.identity not in ev.read_entry_checks(vault), (
        "a check for a bullet the vault no longer holds suppresses nothing"
    )


def test_a_settled_entry_row_cannot_be_accepted_again(tmp_path: Path) -> None:
    """The one state where a row outlived its own decision, at this width too.

    The accept writes or splices first and settles second, so a row whose bullet
    outlived the settlement is the only way to reach the handler twice — and the
    second click must find the decision already on record rather than splice the
    same bullet out of a note that no longer has it.
    """
    from ciao import note_edit_proposals as nep

    config, proposal, _entry = _entry_edit_vault(
        tmp_path, operation="retire_entry", outcome="retire"
    )
    vault = config.workspace_vault_root("personal")
    client = _client(config)
    nep.settle_note_edit(config, "personal", proposal.id, accepted=True)

    row = _accept_kind_row(client, "note_edit")

    assert row["note_edit"]["can_accept"] is False
    assert "already decided" in row["note_edit"]["reason"]
    assert (
        client.post(f"/api/proposals/{row['id']}/accept").json()["error"]
        == "already decided; dismiss this row"
    )
    assert (vault / _ENTRY_NOTE).read_text(encoding="utf-8") == _ENTRY_PLAIN, (
        "a settled record is not a licence to splice the bullet out afterwards"
    )


# ── [repo <path>] rows ─────────────────────────────────────────────────────


def _repo_vault(tmp_path: Path) -> tuple[CiaoConfig, Path]:
    from types import SimpleNamespace  # noqa: F401

    repo = tmp_path / "linked-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# App\n\nRules.\n", encoding="utf-8")
    config = _config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "work",
        "# Memory Proposals\n\n## 2026-08-19 curation pass (this pass)\n\n"
        f"- [repo {repo}] Run tests with make test  _(from: Decisions)_\n",
    )
    return config, repo


def test_repo_accept_appends_to_linked_agents_md(tmp_path: Path) -> None:
    from types import SimpleNamespace

    config, repo = _repo_vault(tmp_path)
    pcm = SimpleNamespace(
        list_projects=lambda ws=None: [SimpleNamespace(workspace="work", repo_path=str(repo))]
    )
    client = _client(config, pcm)
    row = _accept_kind_row(client, "repo")

    preview = client.post(f"/api/proposals/{row['id']}/preview").json()["preview"]
    assert preview["exact"] is True
    assert preview["operation"] == "add"
    assert preview["destination_path"] == str(repo.resolve() / "AGENTS.md")

    resp = client.post(
        f"/api/proposals/{row['id']}/accept", json={"expected_revision": preview["revision"]}
    )

    assert resp.status_code == 200, resp.json()
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Agent learnings" in text
    assert "- Run tests with make test" in text
    assert client.get("/api/proposals").json()["rows"] == []


def test_batch_repo_lock_failure_preserves_prior_real_fold(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    from ciao import memory_receipts, note_receipts, memory_proposals
    config, repo = _repo_vault(tmp_path)
    note = config.workspace_vault_root("work") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    queue = proposal_service._proposals_file(config, "work")
    queue.write_text(queue.read_text() + "- [people Mo] Leads the pilot. _(from: Decisions)_\n")
    pcm = SimpleNamespace(list_projects=lambda ws=None: [SimpleNamespace(workspace="work", repo_path=str(repo))])
    client = _client(config, pcm)
    rows = client.get("/api/proposals").json()["rows"]
    person = next(r for r in rows if r["kind"] == "people")
    repo_row = next(r for r in rows if r["kind"] == "repo")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    def busy(**kwargs):
        raise memory_receipts.QueueLockError("repository destination busy; retry")
    monkeypatch.setattr(note_receipts, "append_repo_doc_item", busy)
    response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [person["id"], repo_row["id"]]})
    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[person["id"]]["dismissed"] and results[person["id"]]["promoted"]
    assert not results[repo_row["id"]]["dismissed"] and "try again" in results[repo_row["id"]]["error"]
    assert "Leads the pilot." in note.read_text() and "Leads the pilot." not in queue.read_text()
    assert "Run tests with make test" in queue.read_text()
    assert "Run tests with make test" not in (repo / "AGENTS.md").read_text()
    assert len(memory_proposals.read_decisions(queue)) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_repo_accept_does_not_write_after_selection_changes(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from types import SimpleNamespace
    from ciao.memory_proposals import read_decisions
    from ciao.web import proposal_service

    config, repo = _repo_vault(tmp_path)
    pcm = SimpleNamespace(list_projects=lambda ws=None: [SimpleNamespace(workspace="work", repo_path=str(repo))])
    client = _client(config, pcm)
    row = _accept_kind_row(client, "repo")
    queue = proposal_service._proposals_file(config, "work")
    doc = repo / "AGENTS.md"
    before = doc.read_bytes()
    name = "_rewrite_queue_batch" if batch else "_rewrite_queue_single"
    real = getattr(proposal_service, name)

    def race(*args, **kwargs):
        queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return real(*args, **kwargs)

    monkeypatch.setattr(proposal_service, name, race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/accept").status_code == 409
    assert doc.read_bytes() == before
    assert queue.read_text(encoding="utf-8") == "# Memory Proposals\n"
    assert not read_decisions(queue)


def test_repo_accept_refuses_unlinked_repo_and_keeps_row(tmp_path: Path) -> None:
    from types import SimpleNamespace

    config, repo = _repo_vault(tmp_path)
    pcm = SimpleNamespace(list_projects=lambda ws=None: [])
    client = _client(config, pcm)
    row = _accept_kind_row(client, "repo")

    resp = client.post(f"/api/proposals/{row['id']}/accept")

    assert resp.status_code == 409
    assert "linked" in resp.json()["error"]
    assert "make test" not in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert [r["id"] for r in client.get("/api/proposals").json()["rows"]] == [row["id"]]


def test_decider_is_not_part_of_the_proposal_id() -> None:
    """Adding, changing or dropping the decide tail never moves an id or a dup ordinal."""
    from ciao.proposal_tracking import walk_proposal_queue

    rel = "ws/Workspace/Memory-Proposals.md"
    tails = ("", "  _(decide: nightly; r)_", "  _(decide: you; other)_")
    walks = [
        list(
            walk_proposal_queue(
                "ws", rel, f"- [people Mo] x  _(from: s)_{t}\n- [people Mo] x  _(from: s)_{t}\n"
            )
        )
        for t in tails
    ]
    ids = [[e.proposal_id for e in w] for w in walks]
    assert ids[0] == ids[1] == ids[2]
    assert len(set(ids[0])) == 2
    assert [[e.dup for e in w] for w in walks] == [[0, 1]] * 3


def test_listing_exposes_decider_and_reason(tmp_path: Path) -> None:
    """Rows carry decider and decide_reason only when the bullet has a tail."""
    config = _config(tmp_path)
    (config.workspace_vault_root("personal") / "Workspace").mkdir(parents=True, exist_ok=True)
    _write_queue(
        config,
        "personal",
        "- [memory] tagged  _(from: s)_  _(decide: you; judgement)_\n"
        "- [memory] plain  _(from: s)_\n",
    )
    rows = {r["text"]: r for r in _client(config).get("/api/proposals").json()["rows"]}
    assert rows["tagged"]["decider"] == "you"
    assert rows["tagged"]["decide_reason"] == "judgement"
    assert "decider" not in rows["plain"]
    assert "decide_reason" not in rows["plain"]


# -- Decision reasons (#1734) -------------------------------------------------


def _memory_row(client: TestClient) -> dict:
    rows = client.get("/api/proposals").json()["rows"]
    return next(r for r in rows if r["text"].startswith("Remember the lesson"))


def _history_for(client: TestClient, text: str) -> list[dict]:
    rows = client.get("/api/proposals/history").json()["rows"]
    return [r for r in rows if r["text"] == text]


def test_dismiss_with_a_reason_lands_in_history(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _memory_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss", json={"reason": "already known"})

    assert resp.status_code == 200
    (hist,) = _history_for(client, row["text"])
    assert hist["reason"] == "already known"
    assert hist["action"] == "dismissed"


def test_accept_with_a_reason_and_edited_text_lands_in_history(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _memory_row(client)

    resp = client.post(
        f"/api/proposals/{row['id']}/accept",
        json={"text": "Remember the edited lesson.", "reason": "yes, everywhere"},
    )

    assert resp.status_code == 200
    (hist,) = _history_for(client, row["text"])
    assert hist["reason"] == "yes, everywhere"
    assert hist["action"] == "accepted"
    # The history API folds the receipt id into ``change``; the recorded
    # original text and the receipt reference both survive the edit.
    assert hist["change"]["receipt_id"]


@pytest.mark.parametrize("bad", ["x" * 1001, 123, ["a"]])
def test_an_over_cap_or_non_string_reason_is_refused_before_any_write(
    tmp_path: Path, bad: object
) -> None:
    from ciao.memory_proposals import dismissed_log_path

    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    client = _client(config)
    row = _memory_row(client)

    def snapshot() -> dict[str, bytes]:
        # Every file under the test root: the queue, the accept destination (the
        # workspace guide), the sidecar and any receipt journal.
        return {
            p.relative_to(tmp_path).as_posix(): p.read_bytes()
            for p in sorted(tmp_path.rglob("*"))
            if p.is_file()
        }

    before = snapshot()
    bodies = [
        {"reason": bad},
        {"text": "edited", "reason": bad},
    ]
    for action in ("dismiss", "accept"):
        for body in bodies:
            resp = client.post(f"/api/proposals/{row['id']}/{action}", json=body)
            assert resp.status_code == 400
            assert "1000" in resp.json()["error"] or "string" in resp.json()["error"]

    assert snapshot() == before, "a refused reason must write nothing at all"
    assert not dismissed_log_path(queue).exists()
    assert any(r["id"] == row["id"] for r in client.get("/api/proposals").json()["rows"])


def test_a_reason_of_exactly_the_cap_after_trim_passes(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _memory_row(client)

    resp = client.post(
        f"/api/proposals/{row['id']}/dismiss", json={"reason": "  " + "x" * 1000 + "  "}
    )

    assert resp.status_code == 200
    (hist,) = _history_for(client, row["text"])
    assert hist["reason"] == "x" * 1000


@pytest.mark.parametrize("blank", ["   ", None])
def test_a_blank_reason_is_no_reason(tmp_path: Path, blank: object) -> None:
    config = _default_vault(tmp_path)
    client = _client(config)
    row = _memory_row(client)

    resp = client.post(f"/api/proposals/{row['id']}/dismiss", json={"reason": blank})

    assert resp.status_code == 200
    (hist,) = _history_for(client, row["text"])
    assert hist["reason"] == ""


def test_batch_and_sweep_never_store_a_reason(tmp_path: Path) -> None:
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", (
        "## 2026-07-01 curation pass\n\n"
        "- [memory] An old fact about a forgotten chat.  _(from: Decisions)_\n\n"
        "## 2026-08-19 curation pass (this pass)\n\n"
        "- [memory] A recent fact worth keeping.  _(from: Decisions)_\n"
    ))
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    recent = next(r for r in rows if r["text"].startswith("A recent"))

    swept = client.post(
        "/api/proposals/dismiss-older-than?date=2026-08-01", json={"reason": "nope"}
    )
    assert swept.status_code == 200
    batch = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [recent["id"]], "reason": "nope"},
    )
    assert batch.status_code == 200

    history = client.get("/api/proposals/history").json()["rows"]
    assert len(history) == 2
    assert all(r["reason"] == "" for r in history)


def test_history_rows_carry_a_seeded_reason(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _seed_history(
        config,
        "personal",
        [
            {
                "dismissed_at": "2026-09-01T10:00:00+00:00",
                "kind": "memory",
                "text": "Seeded.",
                "reason": "one-off",
            }
        ],
    )

    rows = _client(config).get("/api/proposals/history").json()["rows"]

    assert rows[0]["reason"] == "one-off"


def test_list_without_rehome_rows_skips_the_vault_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rehome-free list must not pay for the multi-vault rehome scan."""
    config = _default_vault(tmp_path)
    calls: list[int] = []

    def counting_scan(*args: Any, **kwargs: Any) -> list[Any]:
        calls.append(1)
        return []

    monkeypatch.setattr(
        "ciao.web.proposal_service.vault_rehome.detect_misfiled_people", counting_scan
    )

    resp = _client(config).get("/api/proposals")

    assert resp.status_code == 200
    assert len(resp.json()["rows"]) == 3
    assert calls == []


def test_list_with_rehome_rows_scans_the_vault_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rehome rows share one vault scan, not one scan per row."""
    from ciao import vault_rehome

    config = _per_root_config(tmp_path)
    for ws in ("personal", "work"):
        (config.workspace_vault_root(ws) / "People").mkdir(parents=True, exist_ok=True)
    _rerooted_vault(config, tmp_path)
    for name in ("Mo", "Christian"):
        (config.workspace_vault_root("personal") / "People" / f"{name}.md").write_text(
            f"---\ntype: person\ntags:\n  - person\n  - colleague\n---\n# {name}\n",
            encoding="utf-8",
        )
    _write_queue(config, "personal", (
        "## curation pass\n\n"
        "- [rehome] Re-home `personal/People/Mo.md` to `work/People/Mo.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
        "- [rehome] Re-home `personal/People/Christian.md` to `work/People/Christian.md`? "
        "Uncertain.  _(from: vault-rehome)_\n"
    ))
    real_scan = vault_rehome.detect_misfiled_people
    calls: list[int] = []

    def counting_scan(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(
        "ciao.web.proposal_service.vault_rehome.detect_misfiled_people", counting_scan
    )

    rows = _client(config).get("/api/proposals").json()["rows"]

    rehome_rows = [r for r in rows if r["kind"] == "rehome"]
    assert len(rehome_rows) == 2
    assert all(isinstance(r["rehome"], dict) for r in rehome_rows)
    assert len(calls) == 1


def _file_skill_draft(config: CiaoConfig, workspace: str) -> str:
    """File one synthetic skill draft into ``workspace``; return its id."""
    from ciao.upstream_drafts import UPSTREAM_ISSUE, file_draft

    draft = file_draft(
        config,
        workspace,
        target=UPSTREAM_ISSUE,
        skill="web-research",
        title="Document the offline fallback for a timed-out fetch",
        change="State that a fetch that times out must be retried once with a longer budget.",
        body="A search fetch that times out is reported as no results. Retry once first.",
        repository="example/tools",
        version="1.4.0",
        private_evidence="chat-42 turn 7: the fetch reported no results",
    )
    return draft.id


@pytest.mark.parametrize("batch", [False, True])
def test_skill_draft_outer_queue_busy_is_retryable_and_batch_preserves_prior(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from contextlib import contextmanager
    from ciao import memory_proposals, memory_receipts, upstream_drafts
    config = _default_vault(tmp_path)
    personal = _queue_text(config, "personal")
    personal.write_text("# Queue\n\n- [memory] Prior success. _(from: Decisions)_\n", encoding="utf-8")
    draft_id = _file_skill_draft(config, "work")
    queue = _queue_text(config, "work")
    sidecar = upstream_drafts.sidecar_path(config, "work", draft_id)
    before_queue, before_sidecar = queue.read_bytes(), sidecar.read_bytes()
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    draft = next(row for row in rows if row["kind"] == "review" and row["workspace"] == "work")
    ordinary = next(row for row in rows if row["kind"] == "memory" and row["workspace"] == "personal")
    real_lock = memory_receipts.queue_lock
    calls = []
    @contextmanager
    def busy(path, *args, **kwargs):
        if Path(path) == queue:
            calls.append(path)
            raise memory_receipts.QueueLockError("queue lock timed out")
        with real_lock(path, *args, **kwargs) as handle:
            yield handle
    monkeypatch.setattr(memory_receipts, "queue_lock", busy)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [ordinary["id"], draft["id"]]})
        assert response.status_code == 200
        results = {item["id"]: item for item in response.json()["results"]}
        assert results[ordinary["id"]]["dismissed"]
        assert not results[draft["id"]]["dismissed"] and "retry" in results[draft["id"]]["error"].lower()
        assert "Prior success." not in personal.read_text(encoding="utf-8")
        assert len(memory_proposals.read_decisions(personal)) == 1
    else:
        response = client.post(f"/api/proposals/{draft['id']}/dismiss")
        assert response.status_code == 409 and "retry" in response.json()["error"].lower()
        assert "Prior success." in personal.read_text(encoding="utf-8")
    assert calls and queue.read_bytes() == before_queue and sidecar.read_bytes() == before_sidecar
    assert not memory_proposals.read_decisions(queue)
    monkeypatch.setattr(memory_receipts, "queue_lock", real_lock)
    retry = client.post(f"/api/proposals/{draft['id']}/dismiss")
    assert retry.status_code == 200
    assert upstream_drafts.find_record(config, draft_id).lifecycle == upstream_drafts.DRAFT_REJECTED
    assert len(memory_proposals.read_decisions(queue)) == 1


def test_batch_skill_draft_busy_after_same_queue_success(tmp_path: Path, monkeypatch) -> None:
    from contextlib import contextmanager
    from ciao import memory_proposals, memory_receipts, upstream_drafts
    config = _default_vault(tmp_path)
    first = _file_skill_draft(config, "personal")
    second = upstream_drafts.file_draft(
        config, "personal", target=upstream_drafts.UPSTREAM_ISSUE, skill="another-skill",
        title="Clarify retries", change="Explain retries.", body="Explain the retry procedure.",
        repository="example/tools",
    ).id
    queue = _queue_text(config, "personal")
    client = _client(config)
    rows = [row for row in client.get("/api/proposals").json()["rows"] if row["kind"] == "review"]
    by_target = {row["target"]: row for row in rows}
    assert set(by_target) == {first, second}
    real_lock = memory_receipts.queue_lock
    real_settle = proposal_service._settle_skill_draft
    completed = []
    def settle(*args, **kwargs):
        result = real_settle(*args, **kwargs)
        if not result:
            completed.append(True)
        return result
    @contextmanager
    def busy_second(path, *args, **kwargs):
        if Path(path) == queue and completed:
            raise memory_receipts.QueueLockError("busy")
        with real_lock(path, *args, **kwargs) as handle:
            yield handle
    monkeypatch.setattr(memory_receipts, "queue_lock", busy_second)
    monkeypatch.setattr(proposal_service, "_settle_skill_draft", settle)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [by_target[first]["id"], by_target[second]["id"]]})
    assert response.status_code == 200
    results = {row["id"]: row for row in response.json()["results"]}
    assert results[by_target[first]["id"]]["dismissed"]
    assert not results[by_target[second]["id"]]["dismissed"]
    assert "retry" in results[by_target[second]["id"]]["error"]
    assert upstream_drafts.find_record(config, first).lifecycle == upstream_drafts.DRAFT_REJECTED
    assert upstream_drafts.find_record(config, second).lifecycle == upstream_drafts.DRAFT_PENDING
    assert first not in queue.read_text(encoding="utf-8") and second in queue.read_text(encoding="utf-8")
    assert len(memory_proposals.read_decisions(queue)) == 1


@pytest.mark.parametrize("kind", ["people", "rehome"])
@pytest.mark.parametrize("batch", [False, True])
def test_queue_receipt_failure_reports_landed_destination(tmp_path: Path, monkeypatch, kind: str, batch: bool) -> None:
    from ciao import memory_receipts, memory_proposals
    if kind == "rehome":
        config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
        source = config.workspace_vault_root("personal") / "People/Mo.md"
        destination = config.workspace_vault_root("work") / "People/Mo.md"
        before_destination = source.read_bytes()
    else:
        config = _default_vault(tmp_path)
        destination = config.workspace_vault_root("personal") / "People/Mo.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
        before_destination = destination.read_bytes()
        _write_queue(config, "personal", "## pass\n\n- [people Mo] Leads the pilot. _(from: Decisions)_\n")
        client = _client(config)
        row = _accept_kind_row(client, "people")
        async def model(*args, **kwargs):
            return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
        monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    queue = proposal_service._proposals_file(config, "personal")
    before_queue = queue.read_bytes()
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    original = memory_receipts._append
    faults = []
    def fail_queue_receipt(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED:
            faults.append(True)
            raise OSError("injected queue receipt failure")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail_queue_receipt)
        if batch:
            response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]], "workspace": "work"})
            assert response.status_code == 200
            result = response.json()["results"][0]
        else:
            suffix = "?workspace=work" if kind == "rehome" else ""
            response = client.post(f"/api/proposals/{row['id']}/accept{suffix}")
            assert response.status_code == 503
            result = response.json()
    assert faults and queue.read_bytes() == before_queue
    effect = result["destination_effect"]
    assert effect["status"] == "completed"
    assert effect["destination"] == ("People/Mo.md" if kind == "people" else "work/memory-vault/People/Mo.md")
    assert "do not retry" in result["error"].lower()
    assert result["queue_settled"] is False
    if kind == "people":
        assert destination.read_bytes() != before_destination and b"Leads the pilot." in destination.read_bytes()
        note_receipt = next(r for r in memory_receipts.read_receipts(journal) if r.get("kind") == "note_apply" and r.get("status") == memory_receipts.APPLIED)
        assert effect["receipt_id"] == note_receipt["id"]
    else:
        assert not source.exists() and destination.read_bytes() == before_destination
        assert effect["destination"] == "work/memory-vault/People/Mo.md"
    assert not memory_proposals.read_decisions(queue)
    after_effect = destination.read_bytes()
    if kind == "rehome":
        # An inspected move can be retried through its existing already-moved path.
        retry = client.post(f"/api/proposals/{row['id']}/accept?workspace=work")
        assert retry.status_code == 200 and retry.json()["result"]["already_moved"] is True
    else:
        # The operator can instead settle the inspected fold without folding again.
        dismissed = client.post(f"/api/proposals/{row['id']}/dismiss")
        assert dismissed.status_code == 200
    assert destination.read_bytes() == after_effect


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("fault,state", [("applied", "replaced"), ("write_before", "unchanged"), ("write_after", "replaced")])
def test_queue_landing_failure_reports_observed_state(tmp_path: Path, monkeypatch, batch: bool, fault: str, state: str) -> None:
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n- [people Mo] Leads the pilot. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    client = _client(config)
    row = _accept_kind_row(client, "people")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    append = memory_receipts._append
    write = memory_receipts.write_queue_atomically
    attempted = {}
    def append_fault(path, payload, *args, **kwargs):
        if (fault == "applied" and payload.get("kind") == "queue_resolve"
                and payload.get("status") == memory_receipts.APPLIED and payload.get("owner_landed")):
            raise OSError("applied receipt failed")
        return append(path, payload, *args, **kwargs)
    def write_fault(path, text, *args, **kwargs):
        if Path(path) == queue:
            attempted["after"] = text.encode("utf-8")
            if fault.startswith("write_"):
                if fault == "write_after":
                    write(path, text, *args, **kwargs)
                raise OSError("queue replacement error")
        return write(path, text, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", append_fault)
        patch.setattr(memory_receipts, "write_queue_atomically", write_fault)
        response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}) if batch
                    else client.post(f"/api/proposals/{row['id']}/accept"))
    assert response.status_code == (200 if batch else 503)
    result = response.json()["results"][0] if batch else response.json()
    assert result["queue_settled"] is False and result["queue_state"] == state
    assert result["queue_receipt_confirmed"] is False
    assert result["destination_effect"]["destination"] == "People/Mo.md"
    assert result["destination_effect"]["receipt_id"]
    assert "do not retry" in result["error"].lower()
    assert "Leads the pilot." in note.read_text()
    assert queue.read_bytes() == (before if state == "unchanged" else attempted["after"])
    journal = memory_receipts.journal_path(vault, None)
    receipts = memory_receipts.read_receipts(journal)
    assert any(item["id"] == result["destination_effect"]["receipt_id"] and item["kind"] == "note_apply" for item in receipts)
    queue_receipt = next(item for item in receipts if item.get("kind") == "queue_resolve" and item.get("selected_proposals" if batch else "proposal_id"))
    assert queue_receipt["status"] == (memory_receipts.ROLLED_BACK if batch and state == "unchanged" else memory_receipts.PREPARED)
    if state == "replaced":
        recovered = memory_receipts.recover_pending(journal=journal, config=config)
        assert any(item.get("kind") == "queue_resolve" and item.get("status") == memory_receipts.CONFLICT for item in recovered.conflicts)


@pytest.mark.parametrize("kind", ["people", "rehome"])
@pytest.mark.parametrize("reserved", [False, True])
def test_batch_receipt_failure_reports_prior_success_and_partial_effect(tmp_path: Path, monkeypatch, kind: str, reserved: bool) -> None:
    from ciao import memory_receipts, memory_proposals
    if kind == "rehome":
        config, client, row = _rehome_fixture(tmp_path, ["person", "colleague"])
        destination = config.workspace_vault_root("work") / "People/Mo.md"
    else:
        config = _default_vault(tmp_path)
        destination = config.workspace_vault_root("personal") / "People/Mo.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
        _write_queue(config, "personal", "## pass\n\n- [people Mo] Leads the pilot. _(from: Decisions)_\n")
        client = _client(config)
        row = _accept_kind_row(client, "people")
        async def model(*args, **kwargs):
            return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
        monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    _write_queue(config, "work", "## pass\n\n- [memory] Prefers thorough checks. _(from: Work)_\n")
    rows = client.get("/api/proposals").json()["rows"]
    prior = next(item for item in rows if item["workspace"] == "work" and item["kind"] == "memory")
    headers = {}
    if reserved:
        from ciao.web.proposal_reservations import ReservationRegistry, validate_scope
        registry = ReservationRegistry()
        client.app.state.proposal_reservations = registry
        scope = validate_scope({"kind": "batch_accept", "workspace": "work",
                                "targets": [prior["id"], row["id"]],
                                "workspaces": ["personal", "work"],
                                "destinations": {row["id"]: "work"} if kind == "rehome" else {}},
                               set(config.workspace_names()))
        token = registry.reserve(scope)
        headers = {"X-Ciao-Proposal-Reservation": token}
    queue = proposal_service._proposals_file(config, "personal")
    before = queue.read_bytes()
    original = memory_receipts._append
    def fail_personal(path, payload, *args, **kwargs):
        if (payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED
                and payload.get("queue") == str(queue)):
            raise OSError("injected queue receipt failure")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail_personal)
        response = client.post("/api/proposals/batch", headers=headers, json={"action": "accept", "ids": [prior["id"], row["id"]], "workspace": "work"})
    assert response.status_code == 200
    results = {item["id"]: item for item in response.json()["results"]}
    assert results[prior["id"]]["dismissed"] and results[prior["id"]]["promoted"]
    assert not results[row["id"]]["dismissed"] and results[row["id"]]["queue_settled"] is False
    assert results[row["id"]]["destination_effect"]["status"] == "completed"
    assert "do not retry" in results[row["id"]]["error"].lower()
    assert destination.is_file() and queue.read_bytes() == before
    assert len(memory_proposals.read_decisions(proposal_service._proposals_file(config, "work"))) == 1
    assert len(response.json()["results"]) == 2
    if reserved:
        assert registry.seal(token, scope, inspected=True)


def test_sequential_batch_receipt_failure_keeps_prior_queue_success(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts
    config = _default_vault(tmp_path)
    note = config.workspace_vault_root("personal") / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n"
                 "- [people Mo] Leads the pilot. _(from: Decisions)_\n"
                 "- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    person = next(row for row in rows if row["kind"] == "people")
    prior = next(row for row in rows if row["kind"] == "memory" and row["workspace"] == "personal")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    original = memory_receipts._append
    count = 0
    def fail_second(path, payload, *args, **kwargs):
        nonlocal count
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED:
            count += 1
            if count == 2:
                raise OSError("second queue receipt failed")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail_second)
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [person["id"], prior["id"]]})
    assert response.status_code == 200 and count == 2
    results = {item["id"]: item for item in response.json()["results"]}
    assert results[prior["id"]]["dismissed"] and results[prior["id"]]["promoted"]
    assert not results[person["id"]]["dismissed"] and results[person["id"]]["queue_settled"] is False
    receipt_id = results[person["id"]]["destination_effect"]["receipt_id"]
    journal = memory_receipts.journal_path(config.workspace_vault_root("personal"), None)
    assert any(row["id"] == receipt_id and row["kind"] == "note_apply" for row in memory_receipts.read_receipts(journal))
    queue = proposal_service._proposals_file(config, "personal")
    assert "Leads the pilot." in queue.read_text() and "Prefers thorough checks." not in queue.read_text()
    assert len(memory_proposals.read_decisions(queue)) == 1
    assert "Leads the pilot." in note.read_text()


def test_two_folds_same_note_second_prepare_fails(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_proposals, memory_receipts
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n"
                 "- [people Mo] Leads the pilot. _(from: Decisions)_\n"
                 "- [people Mo] Designs the tests. _(from: Decisions)_\n")
    client = _client(config)
    rows = [r for r in client.get("/api/proposals").json()["rows"] if r["kind"] == "people"]
    model_calls = []
    async def model(*args, **kwargs):
        model_calls.append(True)
        fact = "Designs the tests." if len(model_calls) == 1 else "Leads the pilot."
        return '{"action":"add","section":"Notes","text":"- ' + fact + '"}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    original = memory_receipts._append
    count = 0
    def fail_second(path, payload, *args, **kwargs):
        nonlocal count
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED:
            count += 1
            if count == 2:
                raise OSError("second prepared receipt failed")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail_second)
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [r["id"] for r in rows]})
    assert response.status_code == 200 and count == 2 and len(model_calls) == 2
    results = {r["id"]: r for r in response.json()["results"]}
    succeeded = next(r for r in rows if results[r["id"]]["dismissed"])
    partial = next(r for r in rows if results[r["id"]].get("queue_settled") is False)
    effect = results[partial["id"]]["destination_effect"]
    assert effect["destination"] == "People/Mo.md" and effect["status"] == "completed"
    assert "Leads the pilot." in note.read_text() and "Designs the tests." in note.read_text()
    queue = proposal_service._proposals_file(config, "personal")
    assert succeeded["text"] not in queue.read_text() and partial["text"] in queue.read_text()
    decisions = memory_proposals.read_decisions(queue)
    assert len(decisions) == 1 and decisions[0]["proposal_id"] == succeeded["id"]
    receipts = memory_receipts.read_receipts(memory_receipts.journal_path(vault, None))
    assert sum(r.get("kind") == "note_apply" and r.get("status") == memory_receipts.APPLIED for r in receipts) == 2
    assert any(r["id"] == effect["receipt_id"] and r.get("kind") == "note_apply" for r in receipts)


@pytest.mark.parametrize("fault,state", [("applied", "replaced"), ("write_before", "unchanged"), ("write_after", "replaced")])
def test_batch_landing_fault_preserves_prior_success(tmp_path: Path, monkeypatch, fault: str, state: str) -> None:
    from ciao import memory_proposals, memory_receipts
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n"
                 "- [people Mo] Leads the pilot. _(from: Decisions)_\n"
                 "- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    person = next(r for r in rows if r["kind"] == "people")
    prior = next(r for r in rows if r["kind"] == "memory" and r["workspace"] == "personal")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    append = memory_receipts._append
    write = memory_receipts.write_queue_atomically
    count = 0
    observed = {}
    def append_fault(path, payload, *args, **kwargs):
        nonlocal count
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.APPLIED and payload.get("owner_landed"):
            count += 1
            if fault == "applied" and count == 2:
                observed["after"] = queue.read_bytes()
                raise OSError("second applied receipt failed")
        return append(path, payload, *args, **kwargs)
    def write_fault(path, text, *args, **kwargs):
        if Path(path) == queue and fault.startswith("write_") and count == 1:
            observed["before"] = queue.read_bytes()
            observed["expected"] = text.encode("utf-8")
            if fault == "write_after":
                write(path, text, *args, **kwargs)
            raise OSError("second queue write failed")
        return write(path, text, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", append_fault)
        patch.setattr(memory_receipts, "write_queue_atomically", write_fault)
        response = client.post("/api/proposals/batch", json={"action": "accept", "ids": [person["id"], prior["id"]]})
    assert response.status_code == 200
    results = {r["id"]: r for r in response.json()["results"]}
    assert results[prior["id"]]["dismissed"] and results[prior["id"]]["promoted"]
    partial = results[person["id"]]
    assert partial["queue_settled"] is False and partial["queue_receipt_confirmed"] is False
    assert partial["queue_state"] == state and partial["destination_effect"]["destination"] == "People/Mo.md"
    assert partial["destination_effect"]["receipt_id"]
    assert "Leads the pilot." in note.read_text()
    if fault == "write_before":
        assert queue.read_bytes() == observed["before"]
    elif fault == "write_after":
        assert queue.read_bytes() == observed["expected"] != observed["before"]
    else:
        assert queue.read_bytes() == observed["after"]
    assert ("Leads the pilot." in queue.read_text()) is (state == "unchanged")
    assert "Prefers thorough checks." not in queue.read_text()
    assert len(memory_proposals.read_decisions(queue)) == 1


@pytest.mark.parametrize("fault", ["decision", "receipt"])
def test_single_dismiss_partial_outcome(tmp_path: Path, monkeypatch, fault: str) -> None:
    from ciao import memory_receipts, proposal_actions
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    if fault == "decision":
        def fail(*args, **kwargs):
            raise OSError("decision unavailable")
        monkeypatch.setattr(proposal_actions, "record_decision", fail)
    else:
        append = memory_receipts._append
        def fail_applied(path, payload, *args, **kwargs):
            if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.APPLIED:
                raise OSError("receipt unavailable")
            return append(path, payload, *args, **kwargs)
        monkeypatch.setattr(memory_receipts, "_append", fail_applied)
    response = client.post(f"/api/proposals/{row['id']}/dismiss")
    assert response.status_code == 503
    result = response.json()
    assert result["queue_settled"] is False and result["queue_state"] == "replaced"
    assert "Prefers thorough checks." not in queue.read_text()
    if fault == "decision":
        assert result["queue_receipt_confirmed"] is True and result["queue_receipt_id"]
        assert result["decision_confirmed"] is False
        assert "dismissal" in result["error"] and "accept blindly" not in result["error"]
    else:
        assert result["queue_receipt_confirmed"] is False


def test_batch_dismiss_applied_receipt_failure_marks_each_partial(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    append = memory_receipts._append
    def fail_applied(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.APPLIED:
            raise OSError("receipt unavailable")
        return append(path, payload, *args, **kwargs)
    monkeypatch.setattr(memory_receipts, "_append", fail_applied)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["id"] == row["id"]
    assert result["queue_settled"] is False and result["queue_state"] == "replaced"
    assert result["queue_receipt_confirmed"] is False
    assert "Prefers thorough checks." not in queue.read_text()


def test_batch_dismiss_prior_success_and_later_receipt_failure(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [memory] Personal fact. _(from: Decisions)_\n")
    _write_queue(config, "work", "## pass\n\n- [memory] Work fact. _(from: Decisions)_\n")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    personal = next(row for row in rows if row["text"] == "Personal fact.")
    work = next(row for row in rows if row["text"] == "Work fact.")
    work_journal = memory_receipts.journal_path(config.workspace_vault_root("work"), None)
    append = memory_receipts._append
    def fail_work(path, payload, *args, **kwargs):
        if (path == work_journal and payload.get("kind") == "queue_resolve"
                and payload.get("status") == memory_receipts.APPLIED):
            raise OSError("work receipt unavailable")
        return append(path, payload, *args, **kwargs)
    monkeypatch.setattr(memory_receipts, "_append", fail_work)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [personal["id"], work["id"]]})
    assert response.status_code == 200
    results = {result["id"]: result for result in response.json()["results"]}
    assert results[personal["id"]]["dismissed"] is True
    assert results[work["id"]]["queue_settled"] is False
    assert results[work["id"]]["queue_state"] == "replaced"
    assert results[work["id"]]["queue_receipt_confirmed"] is False
    assert "Work fact." not in proposal_service._proposals_file(config, "work").read_text()


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_batch_dismiss_decision_failure_reports_removed_but_unconfirmed(tmp_path: Path, monkeypatch, error_type) -> None:
    from ciao import proposal_actions
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    def fail(*args, **kwargs):
        raise error_type("decision unavailable")
    monkeypatch.setattr(proposal_actions, "record_decision", fail)
    response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["dismissed"] is True and result["queue_state"] == "replaced"
    assert result["queue_settled"] is False and result["decision_confirmed"] is False
    assert result["queue_receipt_confirmed"] is True and result["queue_receipt_id"]
    assert "Prefers thorough checks." not in queue.read_text()


@pytest.mark.parametrize("batch", [False, True])
def test_decision_write_failure_reports_real_landed_fold(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import memory_proposals, memory_receipts, proposal_actions
    config = _default_vault(tmp_path)
    vault = config.workspace_vault_root("personal")
    note = vault / "People/Mo.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Mo\n\n## Notes\n- Existing.\n", encoding="utf-8")
    _write_queue(config, "personal", "## pass\n\n"
                 "- [people Mo] Leads the pilot. _(from: Decisions)_\n"
                 "- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    person = next(r for r in rows if r["kind"] == "people")
    prior = next(r for r in rows if r["kind"] == "memory" and r["workspace"] == "personal")
    async def model(*args, **kwargs):
        return '{"action":"add","section":"Notes","text":"- Leads the pilot."}'
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", model)
    original = proposal_actions.record_decision
    def fail_person(queue_path, **kwargs):
        if kwargs.get("kind") == "people":
            raise OSError("decision sidecar unavailable")
        return original(queue_path, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(proposal_actions, "record_decision", fail_person)
        response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [person["id"], prior["id"]]})
                    if batch else client.post(f"/api/proposals/{person['id']}/accept"))
    assert response.status_code == (200 if batch else 503)
    if batch:
        results = {r["id"]: r for r in response.json()["results"]}
        assert results[prior["id"]]["dismissed"] and results[prior["id"]]["promoted"]
        result = results[person["id"]]
    else:
        result = response.json()
    assert result["queue_settled"] is False and result["queue_state"] == "replaced"
    assert result["queue_receipt_confirmed"] is True and result["decision_confirmed"] is False
    assert result["queue_receipt_id"]
    assert result["destination_effect"]["destination"] == "People/Mo.md"
    receipt_id = result["destination_effect"]["receipt_id"]
    assert "inspect" in result["error"].lower()
    assert "Leads the pilot." in note.read_text() and "Leads the pilot." not in queue.read_text()
    receipts = memory_receipts.read_receipts(memory_receipts.journal_path(vault, None))
    assert any(r.get("id") == receipt_id and r.get("kind") == "note_apply" and r.get("status") == memory_receipts.APPLIED for r in receipts)
    assert any(r.get("id") == result["queue_receipt_id"] and r.get("kind") == "queue_resolve" and r.get("status") == memory_receipts.APPLIED
               and (r.get("decision_receipt_id") == receipt_id or
                    any(p.get("decision_receipt_id") == receipt_id for p in r.get("selected_proposals", [])))
               for r in receipts)
    assert len(memory_proposals.read_decisions(queue)) == (1 if batch else 0)


@pytest.mark.parametrize("batch", [False, True])
def test_dismiss_write_error_after_replacement_is_not_reported_as_unchanged(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import memory_receipts
    config = _default_vault(tmp_path)
    _write_queue(config, "personal", "## pass\n\n- [memory] Prefers thorough checks. _(from: Decisions)_\n")
    queue = proposal_service._proposals_file(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    original = memory_receipts.write_queue_atomically
    def fail_after(path, text, *args, **kwargs):
        original(path, text, *args, **kwargs)
        raise OSError("after replacement")
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "write_queue_atomically", fail_after)
        response = (client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]}) if batch
                    else client.post(f"/api/proposals/{row['id']}/dismiss"))
    assert response.status_code == (200 if batch else 503)
    result = response.json()["results"][0] if batch else response.json()
    assert result["queue_state"] == "replaced" and result["queue_settled"] is False
    assert result["queue_receipt_confirmed"] is False
    assert "not removed" not in result["error"]
    assert "inspect" in result["error"].lower()
    assert "Prefers thorough checks." not in queue.read_text()


def test_batch_receipt_failure_keeps_prior_skill_draft_settlement(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts, upstream_drafts
    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    queue.write_text("# Queue\n\n- [memory] Remains queued. _(from: Decisions)_\n", encoding="utf-8")
    draft_id = _file_skill_draft(config, "personal")
    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    draft = next(row for row in rows if row["kind"] == "review")
    ordinary = next(row for row in rows if row["kind"] == "memory" and row["workspace"] == "personal")
    original = memory_receipts._append
    def fail(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED:
            raise OSError("queue receipt failed")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail)
        response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [draft["id"], ordinary["id"]]})
    assert response.status_code == 200
    results = {item["id"]: item for item in response.json()["results"]}
    assert results[draft["id"]]["dismissed"] and not results[ordinary["id"]]["dismissed"]
    assert upstream_drafts.find_record(config, draft_id).lifecycle == upstream_drafts.DRAFT_REJECTED
    assert draft_id not in queue.read_text() and "Remains queued." in queue.read_text()


@pytest.mark.parametrize("batch", [False, True])
def test_queue_receipt_failure_reports_duplicate_as_already_present(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import memory_receipts, memory_tool
    config = _competing_vault(tmp_path)
    guide = config.agent_root("personal") / "AGENTS.md"
    memory_tool.write_region(guide, "memory", ["Office is in Berlin. [2026-01-01]"])
    before = guide.read_bytes()
    client = _client(config)
    row = _accept_kind_row(client, "memory")
    original = memory_receipts._append
    def fail(path, payload, *args, **kwargs):
        if payload.get("kind") == "queue_resolve" and payload.get("status") == memory_receipts.PREPARED:
            raise OSError("queue receipt failed")
        return original(path, payload, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(memory_receipts, "_append", fail)
        response = (client.post("/api/proposals/batch", json={"action": "accept", "ids": [row["id"]]}) if batch
                    else client.post(f"/api/proposals/{row['id']}/accept"))
    result = response.json()["results"][0] if batch else response.json()
    assert result["destination_effect"]["status"] == "already_present"
    assert "receipt_id" not in result["destination_effect"]
    assert result["queue_settled"] is False and guide.read_bytes() == before


@pytest.mark.parametrize("batch", [False, True])
def test_skill_draft_stale_selection_does_not_settle(tmp_path: Path, monkeypatch, batch: bool) -> None:
    from ciao import upstream_drafts
    from ciao.memory_proposals import read_decisions

    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    draft_id = _file_skill_draft(config, "personal")
    client = _client(config)
    row = _accept_kind_row(client, "review")
    sidecar = upstream_drafts.sidecar_path(config, "personal", draft_id)
    before = sidecar.read_bytes()
    real = proposal_service._is_skill_draft_row

    def race(cfg, selected):
        found = real(cfg, selected)
        if found:
            queue.write_text("# Memory Proposals\n", encoding="utf-8")
        return found

    monkeypatch.setattr(proposal_service, "_is_skill_draft_row", race)
    if batch:
        response = client.post("/api/proposals/batch", json={"action": "dismiss", "ids": [row["id"]]})
        assert not response.json()["results"][0]["dismissed"]
    else:
        assert client.post(f"/api/proposals/{row['id']}/dismiss").status_code == 409
    assert sidecar.read_bytes() == before
    assert not read_decisions(queue)


@pytest.mark.parametrize("duplicate", [False, True])
def test_draft_dismiss_ignores_unrelated_mentions_but_refuses_duplicate(tmp_path: Path, duplicate: bool) -> None:
    from ciao import upstream_drafts
    from ciao.memory_proposals import read_decisions
    config = _default_vault(tmp_path)
    draft_id = _file_skill_draft(config, "personal")
    queue = _queue_text(config, "personal")
    lines = queue.read_text(encoding="utf-8").splitlines()
    draft_line = next(line for line in lines if f"[review {draft_id}]" in line)
    with queue.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- [memory] Mention skill-draft:{draft_id} _(from: unrelated)_\n")
        if duplicate:
            handle.write(draft_line + "\n")
    before = queue.read_bytes()
    sidecar = upstream_drafts.sidecar_path(config, "personal", draft_id)
    sidecar_before = sidecar.read_bytes()
    client = _client(config)
    row = _accept_kind_row(client, "review")
    response = client.post(f"/api/proposals/{row['id']}/dismiss")
    if duplicate:
        assert response.status_code == 409
        assert queue.read_bytes() == before and sidecar.read_bytes() == sidecar_before
        assert not read_decisions(queue)
    else:
        assert response.status_code == 200
        assert f"Mention skill-draft:{draft_id}" in queue.read_text()
        assert upstream_drafts.find_record(config, draft_id).lifecycle == upstream_drafts.DRAFT_REJECTED


def test_dismissing_a_skill_draft_row_rejects_the_draft(tmp_path: Path) -> None:
    from ciao.memory_proposals import read_decisions
    from ciao.upstream_drafts import DRAFT_REJECTED, read_records

    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    queue.write_text(
        "# Memory Proposals\n\n- [memory] An ordinary bullet before the draft.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    draft_id = _file_skill_draft(config, "personal")
    with queue.open("a", encoding="utf-8") as fh:
        fh.write("- [memory] An ordinary bullet after the draft.  _(from: Decisions)_\n")

    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    draft_row = next(r for r in rows if r["kind"] == "review")

    resp = client.post(f"/api/proposals/{draft_row['id']}/dismiss")

    assert resp.status_code == 200
    draft = next(d for d in read_records(config, "personal") if d.id == draft_id)
    assert draft.lifecycle == DRAFT_REJECTED
    assert any(d["text"] == f"skill-draft:{draft_id}" for d in read_decisions(queue))
    text = queue.read_text(encoding="utf-8")
    assert f"skill-draft:{draft_id}" not in text
    assert "An ordinary bullet before the draft." in text
    assert "An ordinary bullet after the draft." in text


def test_dismissing_an_orphan_skill_draft_row_removes_its_bullet(tmp_path: Path) -> None:
    from ciao import upstream_drafts

    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    queue.write_text(
        "# Memory Proposals\n\n- [memory] An ordinary bullet before the orphan.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    draft = upstream_drafts.file_draft(
        config,
        "personal",
        target=upstream_drafts.UPSTREAM_ISSUE,
        skill="web-research",
        title="Document the offline fallback for a timed-out fetch",
        change="State that a fetch that times out must be retried once with a longer budget.",
        body="A search fetch that times out is reported as no results. Retry once first.",
        repository="example/tools",
        version="1.4.0",
        private_evidence="chat-42 turn 7: the fetch reported no results",
    )
    upstream_drafts.sidecar_path(config, "personal", draft.id).unlink()

    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    orphan_row = next(r for r in rows if r["kind"] == "review")

    resp = client.post(f"/api/proposals/{orphan_row['id']}/dismiss")

    assert resp.status_code == 200
    text = queue.read_text(encoding="utf-8")
    assert f"skill-draft:{draft.id}" not in text
    assert "An ordinary bullet before the orphan." in text


def test_batch_dismiss_settles_a_skill_draft_and_keeps_other_rows(tmp_path: Path) -> None:
    from ciao.upstream_drafts import DRAFT_REJECTED, read_records

    config = _default_vault(tmp_path)
    queue = _queue_text(config, "personal")
    queue.write_text(
        "# Memory Proposals\n\n- [memory] An ordinary row in the batch.  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    draft_id = _file_skill_draft(config, "personal")
    with queue.open("a", encoding="utf-8") as fh:
        fh.write("- [memory] A bullet outside the batch that must survive.  _(from: Decisions)_\n")

    client = _client(config)
    rows = client.get("/api/proposals").json()["rows"]
    ordinary = next(r for r in rows if r["text"].startswith("An ordinary row in the batch"))
    draft_row = next(r for r in rows if r["kind"] == "review")

    resp = client.post(
        "/api/proposals/batch",
        json={"action": "dismiss", "ids": [ordinary["id"], draft_row["id"]]},
    )

    assert resp.status_code == 200
    assert all(r["dismissed"] for r in resp.json()["results"])
    text = queue.read_text(encoding="utf-8")
    assert "An ordinary row in the batch" not in text
    assert f"skill-draft:{draft_id}" not in text
    assert "A bullet outside the batch that must survive." in text
    draft = next(d for d in read_records(config, "personal") if d.id == draft_id)
    assert draft.lifecycle == DRAFT_REJECTED
