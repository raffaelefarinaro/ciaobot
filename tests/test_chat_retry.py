from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.models import ChatContext, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import chat_service
from ciao.web.project_chats import ProjectChatManager

from tests.conftest import attach_stub_mcp


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    ))


@pytest.mark.parametrize("operation", ["delete", "archive", "reset", "handover"])
def test_lifecycle_route_reports_persisted_claim_conflict(tmp_path: Path, operation: str) -> None:
    from ciao.web.routes_api import chat_archive, chat_detail, chat_handover, chat_new_session

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    chat.pending_queue = [{"id": "keep", "text": "do not lose", "images": []}]
    owner._save()
    assert owner.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    manager = _make_manager(tmp_path)
    claimed, resume = Event(), Event()

    def start(*args, **kwargs):
        claimed.set()
        assert resume.wait(5)
        return object()

    owner.start_stream = start
    app = Starlette(routes=[
        Route("/chats/{chat_id}", chat_detail, methods=["DELETE"]),
        Route("/chats/{chat_id}/archive", chat_archive, methods=["POST"]),
        Route("/chats/{chat_id}/reset", chat_new_session, methods=["POST"]),
        Route("/chats/{chat_id}/handover", chat_handover, methods=["POST"]),
    ])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner.try_chat_retry_now, chat.chat_id)
        assert claimed.wait(5)
        try:
            before = owner._path.read_bytes()
            url = f"/chats/{chat.chat_id}"
            response = client.delete(url) if operation == "delete" else client.post(
                f"{url}/{operation}", json={"provider": "claude", "model": "test"}
            )
            assert response.status_code == 409
            assert response.json() == {"error": "recovery is in flight"}
            assert owner._path.read_bytes() == before
            assert manager.get_chat(chat.chat_id).pending_queue[0]["text"] == "do not lose"
        finally:
            resume.set()
        assert future.result() is not None


@pytest.mark.parametrize("operation", ["delete", "archive", "reset", "handover"])
@pytest.mark.parametrize("error", [ValueError("other validation"), OSError("storage failure")])
def test_lifecycle_routes_do_not_mask_other_errors(tmp_path: Path, monkeypatch, operation: str, error: Exception) -> None:
    from ciao.web.routes_api import chat_archive, chat_detail, chat_handover, chat_new_session

    manager = _make_manager(tmp_path)
    project = manager.create_project("test", workspace="personal")
    chat = manager.create_chat(project.project_id, title="test")

    def fail(*args, **kwargs):
        raise error

    async def fail_async(*args, **kwargs):
        raise error

    name = {"delete": "delete_chat", "archive": "archive_chat", "reset": "new_session",
            "handover": "handover_chat"}[operation]
    monkeypatch.setattr(manager, name, fail_async if operation == "archive" else fail)
    app = Starlette(routes=[
        Route("/chats/{chat_id}", chat_detail, methods=["DELETE"]),
        Route("/chats/{chat_id}/archive", chat_archive, methods=["POST"]),
        Route("/chats/{chat_id}/reset", chat_new_session, methods=["POST"]),
        Route("/chats/{chat_id}/handover", chat_handover, methods=["POST"]),
    ])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/chats/{chat.chat_id}"
    response = client.delete(url) if operation == "delete" else client.post(
        f"{url}/{operation}", json={"provider": "claude", "model": "test"}
    )
    assert response.status_code == (400 if isinstance(error, ValueError) and operation in ("reset", "handover") else 500)


async def _consume(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
    return events


def test_recovery_request_status_tombstones_delayed_submission(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failed", "error", "failure"
    owner._save()
    other = _make_manager(tmp_path)
    request_id = other.recovery_request_record(chat.chat_id, None)[0]["request_id"]

    def client(manager):
        app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
        app.state.project_chat_manager = manager
        return TestClient(app, raise_server_exceptions=False)

    url = f"/chats/{chat.chat_id}/retry"
    status = client(other).post(url, json={"action": "recovery_request_status", "request_id": request_id})
    assert status.status_code == 200
    assert status.json() == {"request_id": request_id, "status": "cancelled"}
    delayed = client(owner).post(url, json={"action": "continue_recovery", "failure_id": "failure", "request_id": request_id})
    assert delayed.status_code == 409
    assert owner._read_latest_payload()["chats"][chat.chat_id]["retry_status"] != "pending"


def test_recovery_status_cancels_request_delayed_before_admission(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failed", "error", "failure"
    owner._save()
    other = _make_manager(tmp_path)
    request_id = other.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    waiting, resume = Event(), Event()
    original = owner.recovery_request_record

    def delayed(*args, **kwargs):
        if kwargs.get("intent") and kwargs.get("result") is None:
            waiting.set()
            assert resume.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "recovery_request_record", delayed)

    def client(manager):
        app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
        app.state.project_chat_manager = manager
        return TestClient(app, raise_server_exceptions=False)

    url = f"/chats/{chat.chat_id}/retry"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client(owner).post, url, json={
            "action": "continue_recovery", "failure_id": "failure", "request_id": request_id,
        })
        assert waiting.wait(5)
        try:
            status = client(other).post(url, json={"action": "recovery_request_status", "request_id": request_id})
            assert status.json()["status"] == "cancelled"
        finally:
            resume.set()
        assert future.result().status_code == 409
    assert owner.recovery_status(chat.chat_id)["status"] != "pending"


def test_recovery_request_survives_client_disconnect_and_blocks_duplicate(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failed", "error", "failure"
    owner._save()
    other = _make_manager(tmp_path)
    request_id = other.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    entered, resume = Event(), Event()
    original = owner.schedule_continue_recovery

    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "schedule_continue_recovery", delayed)

    def client(manager):
        app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
        app.state.project_chat_manager = manager
        return TestClient(app, raise_server_exceptions=False)

    url = f"/chats/{chat.chat_id}/retry"
    body = {"action": "continue_recovery", "failure_id": "failure", "request_id": request_id}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client(owner).post, url, json=body)
        assert entered.wait(5)
        try:
            status = client(other).post(url, json={"action": "recovery_request_status", "request_id": request_id})
            assert status.json() == {"request_id": request_id, "status": "in_progress"}
            assert client(other).post(url, json=body).status_code == 202
            conflict = client(other).post(url, json={**body, "failure_id": "other"})
            assert conflict.status_code == 409
        finally:
            resume.set()
        assert future.result().json()["status"] == "completed"
    restarted = _make_manager(tmp_path)
    status = client(restarted).post(url, json={"action": "recovery_request_status", "request_id": request_id})
    assert status.json()["status"] == "completed"
    assert status.json()["result"]["http_status"] == 200
    assert restarted.recovery_status(chat.chat_id)["mode"] == "continue_recovery"


@pytest.mark.parametrize("stage", ["admission", "completion"])
def test_recovery_request_ambiguous_journal_write_is_never_absent(tmp_path: Path, monkeypatch, stage: str) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failed", "error", "failure"
    owner._save()
    other = _make_manager(tmp_path)
    request_id = other.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    original = owner.recovery_request_record

    def write_then_fail(*args, **kwargs):
        record = original(*args, **kwargs)
        if (kwargs.get("result") is not None) == (stage == "completion"):
            raise OSError("ambiguous journal write")
        return record

    monkeypatch.setattr(owner, "recovery_request_record", write_then_fail)

    def client(manager):
        app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
        app.state.project_chat_manager = manager
        return TestClient(app, raise_server_exceptions=False)

    url = f"/chats/{chat.chat_id}/retry"
    body = {"action": "continue_recovery", "failure_id": "failure", "request_id": request_id}
    response = client(owner).post(url, json=body)
    assert response.status_code == 503
    assert response.json()["status"] == "unknown"
    status = client(other).post(url, json={"action": "recovery_request_status", "request_id": request_id})
    assert status.json()["status"] == ("unknown" if stage == "admission" else "completed")
    duplicate = client(other).post(url, json=body)
    assert duplicate.json()["status"] == ("unknown" if stage == "admission" else "completed")


def test_recovery_request_unreadable_journal_returns_unknown(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    path = manager._path.with_name(f"{manager._path.stem}.recovery_requests.json")
    path.write_text('{"req-4": null}', encoding="utf-8")
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/chats/{chat.chat_id}/retry", json={"action": "recovery_request_status", "request_id": "req-4"})
    assert response.status_code == 503
    assert response.json() == {"request_id": "req-4", "status": "unknown"}


def test_recovery_request_capacity_fails_closed_without_expiring_tombstones(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats
    from ciao.web.routes_api import chat_retry

    monkeypatch.setattr(project_chats, "_RECOVERY_REQUEST_LIMIT", 2)
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/chats/{chat.chat_id}/retry"
    tokens = []
    for _ in range(2):
        token = client.post(url, json={"action": "issue_recovery_request"}).json()["request_id"]
        tokens.append(token)
        assert client.post(url, json={"action": "recovery_request_status", "request_id": token}).json()["status"] == "cancelled"
    full = client.post(url, json={"action": "issue_recovery_request"})
    assert full.status_code == 503
    delayed = client.post(url, json={"action": "continue_recovery", "failure_id": "failure", "request_id": tokens[0]})
    assert delayed.status_code == 409 and delayed.json()["status"] == "cancelled"


def test_recovery_request_full_journal_still_finishes_admitted_request(tmp_path: Path, monkeypatch, request) -> None:
    from contextlib import ExitStack
    from ciao.web import project_chats

    monkeypatch.setattr(project_chats, "_RECOVERY_REQUEST_LIMIT", 32)
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    intent = {"action": "continue_recovery", "failure_id": "failure"}
    active = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    lifetime = ExitStack()
    request.addfinalizer(lifetime.close)
    _, admitted = manager.recovery_request_record(chat.chat_id, active, intent=intent, lifetime=lifetime)
    assert admitted
    tokens = []
    for _ in range(31):
        token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
        tokens.append(token)
        record, _ = manager.recovery_request_record(chat.chat_id, token)
        assert record["status"] == "cancelled"
    with pytest.raises(OSError):
        manager.recovery_request_record(chat.chat_id, None)
    finished, _ = manager.recovery_request_record(chat.chat_id, active, intent=intent, result={"http_status": 200})
    assert finished["status"] == "completed"
    lifetime.close()
    old, admitted = manager.recovery_request_record(chat.chat_id, tokens[0], intent=intent)
    assert old["status"] == "cancelled" and not admitted


def test_recovery_request_rejects_missing_chat_and_oversized_failure_without_journal(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    missing = client.post("/chats/missing/retry", json={"action": "recovery_request_status", "request_id": "missing"})
    assert missing.status_code == 404 and missing.json()["status"] == "unknown"
    oversized = client.post(f"/chats/{chat.chat_id}/retry", json={
        "action": "continue_recovery", "failure_id": "x" * 129, "request_id": "too-long",
    })
    assert oversized.status_code == 400
    assert not manager._path.with_name(f"{manager._path.stem}.recovery_requests.json").exists()


def test_recovery_request_missing_chat_does_not_change_existing_journal(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    request_id = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    manager.recovery_request_record(chat.chat_id, request_id)
    journal = manager._path.with_name(f"{manager._path.stem}.recovery_requests.json")
    before = journal.read_bytes()
    payload = manager._read_latest_payload()
    payload["chats"].pop(chat.chat_id)
    manager._path.write_text(json.dumps(payload))
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/chats/{chat.chat_id}/retry", json={"action": "recovery_request_status", "request_id": request_id},
    )
    assert response.status_code == 404 and response.json()["status"] == "unknown"
    assert journal.read_bytes() == before


def test_recovery_request_oversized_journal_returns_unknown(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats
    from ciao.web.routes_api import chat_retry

    monkeypatch.setattr(project_chats, "_RECOVERY_REQUEST_BYTES", 64)
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    path = manager._path.with_name(f"{manager._path.stem}.recovery_requests.json")
    path.write_bytes(b"x" * 65)
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/chats/{chat.chat_id}/retry", json={"action": "recovery_request_status", "request_id": "id"},
    )
    assert response.status_code == 503 and response.json()["status"] == "unknown"
    assert path.stat().st_size == 65


@pytest.mark.parametrize("replacement", ["literal", "recovery"])
def test_guarded_set_does_not_replace_schedule_created_during_confirmation(tmp_path: Path, monkeypatch, replacement: str) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    chat.session_id, chat.last_error, chat.last_response_status = "session", "failed", "error"
    owner._save()
    stale = _make_manager(tmp_path)
    original = stale._save_checked
    created = False

    def create_before_commit(*args, **kwargs):
        nonlocal created
        if kwargs.get("_empty_retry") and not created:
            created = True
            if replacement == "literal":
                assert owner.set_chat_retry(chat.chat_id, "newer prompt")
            else:
                assert owner.schedule_continue_recovery(chat.chat_id, "session", "failed", 0)
        return original(*args, **kwargs)

    monkeypatch.setattr(stale, "_save_checked", create_before_commit)
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = stale
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/chats/{chat.chat_id}/retry", json={
        "action": "set", "prompt": "older confirmation", "require_empty": True,
    })
    assert response.status_code == 409
    assert created
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["retry_status"] == "pending"
    assert row["retry_prompt"] == ("newer prompt" if replacement == "literal" else "continue")


def test_guarded_set_populates_empty_disk_slot_from_stale_manager(tmp_path: Path) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    owner.set_chat_retry(chat.chat_id, "old prompt")
    stale = _make_manager(tmp_path)
    owner.stop_chat_retry(chat.chat_id)
    assert stale.set_chat_retry(chat.chat_id, "new prompt", require_empty=True)
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["retry_status"] == "pending"
    assert row["retry_prompt"] == "new prompt"
    assert row["retry_schedule_id"]


def test_guarded_set_route_and_legacy_replacement(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project("retry", workspace="personal")
    chat = manager.create_chat(project.project_id, title="retry")
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    url = f"/chats/{chat.chat_id}/retry"
    assert client.post(url, json={"action": "set", "prompt": "first", "require_empty": True}).status_code == 200
    assert client.post(url, json={"action": "set", "prompt": "refused", "require_empty": True}).status_code == 409
    assert client.post(url, json={"action": "set", "prompt": "legacy"}).status_code == 200
    assert manager._read_latest_payload()["chats"][chat.chat_id]["retry_prompt"] == "legacy"
    assert client.post(url, json={"action": "set", "prompt": "x", "require_empty": False}).status_code == 400


@pytest.mark.parametrize("action", ["try_now", "stop"])
def test_guarded_literal_action_cannot_touch_concurrent_replacement(tmp_path: Path, monkeypatch, action: str) -> None:
    from ciao.web.routes_api import chat_retry

    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    owner.set_chat_retry(chat.chat_id, "first prompt")
    stale = _make_manager(tmp_path)
    observed = stale.get_chat(chat.chat_id).retry_schedule_id
    started = []
    monkeypatch.setattr(stale, "start_stream", lambda *args, **kwargs: started.append(args[1]))
    original = stale._save_checked
    replaced = False

    def replace_before_cas(*args, **kwargs):
        nonlocal replaced
        if kwargs.get("_literal_expected") and not replaced:
            replaced = True
            owner.set_chat_retry(chat.chat_id, "replacement prompt")
        return original(*args, **kwargs)

    monkeypatch.setattr(stale, "_save_checked", replace_before_cas)
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = stale
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/chats/{chat.chat_id}/retry", json={"action": action, "schedule_id": observed})
    assert response.status_code == 409
    assert replaced and not started
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["retry_status"] == "pending"
    assert row["retry_prompt"] == "replacement prompt"
    assert row["retry_schedule_id"] != observed


@pytest.mark.parametrize("action", ["try_now", "stop"])
def test_guarded_literal_action_accepts_observed_schedule(tmp_path: Path, monkeypatch, action: str) -> None:
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project("retry", workspace="personal")
    chat = manager.create_chat(project.project_id, title="retry")
    manager.set_chat_retry(chat.chat_id, "literal prompt")
    started = []
    monkeypatch.setattr(manager, "start_stream", lambda *args, **kwargs: started.append(args[1]) or object())
    app = Starlette(routes=[Route("/chats/{chat_id}/retry", chat_retry, methods=["POST"])])
    app.state.project_chat_manager = manager
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/chats/{chat.chat_id}/retry", json={
        "action": action, "schedule_id": chat.to_dict(local=True)["retry"]["schedule_id"],
    })
    assert response.status_code == 200
    assert started == (["literal prompt"] if action == "try_now" else [])
    if action == "stop":
        assert manager._read_latest_payload()["chats"][chat.chat_id]["retry_status"] == "stopped"


def test_legacy_literal_identity_is_stable_and_replacement_gets_new_id(tmp_path: Path) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    owner.set_chat_retry(chat.chat_id, "same prompt")
    payload = owner._read_latest_payload()
    payload["chats"][chat.chat_id].pop("retry_schedule_id")
    owner._path.write_text(json.dumps(payload))
    first, second = _make_manager(tmp_path), _make_manager(tmp_path)
    observed = first.get_chat(chat.chat_id).retry_schedule_id
    assert observed.startswith("legacy-")
    assert second.get_chat(chat.chat_id).retry_schedule_id == observed
    second.set_chat_retry(chat.chat_id, "same prompt")
    with pytest.raises(ValueError):
        first.stop_chat_retry(chat.chat_id, schedule_id=observed)
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["retry_status"] == "pending"
    assert row["retry_schedule_id"] != observed


def test_guarded_run_dispatches_observed_prompt_after_later_replacement(tmp_path: Path, monkeypatch) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    owner.set_chat_retry(chat.chat_id, "first prompt")
    runner = _make_manager(tmp_path)
    observed = runner.get_chat(chat.chat_id).retry_schedule_id
    original = runner._save_checked
    sent = []

    def replace_after_cas(*args, **kwargs):
        saved = original(*args, **kwargs)
        if kwargs.get("_literal_expected"):
            owner.set_chat_retry(chat.chat_id, "replacement prompt")
        return saved

    monkeypatch.setattr(runner, "_save_checked", replace_after_cas)
    monkeypatch.setattr(runner, "start_stream", lambda *args, **kwargs: sent.append(args[1]) or object())
    assert runner.try_chat_retry_now(chat.chat_id, schedule_id=observed) is not None
    assert sent == ["first prompt"]
    assert owner._read_latest_payload()["chats"][chat.chat_id]["retry_prompt"] == "replacement prompt"


@pytest.mark.parametrize("failure", ["claim_write", "release"])
def test_aborted_claim_reconciles_while_owner_process_alive(tmp_path: Path, monkeypatch, failure: str) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    owner._save()
    assert owner.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    survivor = _make_manager(tmp_path)
    original = owner._save_checked

    def save_then_fail(*args, **kwargs):
        if kwargs.get("reason") == ("continue_recovery_fire" if failure == "claim_write" else "release_recovery_claim"):
            if failure == "claim_write":
                original(*args, **kwargs)
            raise OSError("injected write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "_save_checked", save_then_fail)
    if failure == "claim_write":
        with pytest.raises(OSError):
            owner.try_chat_retry_now(chat.chat_id)
    else:
        monkeypatch.setattr(owner, "start_stream", lambda *args, **kwargs: object())
        assert owner.try_chat_retry_now(chat.chat_id) is not None
        with pytest.raises(OSError):
            owner._release_recovery_claim(chat.chat_id)
    assert survivor.recovery_status(chat.chat_id)["status"] == "stopped"


def test_surviving_manager_reconciles_only_after_owner_dies(tmp_path: Path) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    owner._save()
    assert owner.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    survivor = _make_manager(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        payload = owner._read_latest_payload()
        marker = payload["chats"][chat.chat_id]["retry_recovery"]
        marker.update(claim="foreign-claim", owner=f"{process.pid}-foreign")
        owner._path.write_text(json.dumps(payload))
        assert survivor.recovery_status(chat.chat_id)["recovery_state"] == "in_flight"
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert survivor.recovery_status(chat.chat_id)["status"] == "stopped"


def test_reused_pid_does_not_keep_old_claim_alive(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats

    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    owner._save()
    assert owner.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    survivor = _make_manager(tmp_path)
    payload = owner._read_latest_payload()
    payload["chats"][chat.chat_id]["retry_recovery"].update(
        claim="foreign", owner="12345-foreign", owner_incarnation="old-process"
    )
    owner._path.write_text(json.dumps(payload))
    monkeypatch.setattr(project_chats, "_recovery_owner_alive", lambda owner: True)
    monkeypatch.setattr(project_chats, "_process_incarnation", lambda pid: "new-process")
    assert survivor.recovery_status(chat.chat_id)["status"] == "stopped"


@pytest.mark.parametrize("operation", ["reset", "handover"])
async def test_session_change_stops_recovery_saved_by_another_manager(tmp_path: Path, monkeypatch, operation: str) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    owner._save()
    stale = _make_manager(tmp_path)
    assert owner.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    if operation == "reset":
        stale.new_session(chat.chat_id)
    else:
        monkeypatch.setattr(stale, "_resolve_and_validate_chat_model", lambda *args: "model")
        stale.handover_chat(chat.chat_id, provider="claude", model="model")
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["session_id"] == ""
    assert row["retry_status"] != "pending"
    assert row["retry_recovery"] == ""


@pytest.mark.parametrize("operation", ["reset", "handover"])
async def test_session_change_preserves_other_managers_literal_retry(tmp_path: Path, monkeypatch, operation: str) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("retry", workspace="personal")
    chat = owner.create_chat(project.project_id, title="retry")
    chat.session_id = "session-1"
    owner._save()
    stale = _make_manager(tmp_path)
    owner.set_chat_retry(chat.chat_id, "literal prompt")
    if operation == "reset":
        stale.new_session(chat.chat_id)
    else:
        monkeypatch.setattr(stale, "_resolve_and_validate_chat_model", lambda *args: "model")
        stale.handover_chat(chat.chat_id, provider="claude", model="model")
    row = owner._read_latest_payload()["chats"][chat.chat_id]
    assert row["retry_status"] == "pending"
    assert row["retry_prompt"] == "literal prompt"
    assert row["retry_recovery"] == ""


@pytest.mark.parametrize("operation", ["reset", "handover"])
async def test_stale_session_change_does_not_cancel_new_sessions_recovery(tmp_path: Path, monkeypatch, operation: str) -> None:
    owner = _make_manager(tmp_path)
    project = owner.create_project("recovery", workspace="personal")
    chat = owner.create_chat(project.project_id, title="recovery")
    chat.session_id = "old-session"
    owner._save()
    stale = _make_manager(tmp_path)
    chat.session_id, chat.last_error, chat.last_response_status = "new-session", "failed", "error"
    owner._save()
    assert owner.schedule_continue_recovery(chat.chat_id, "new-session", "failed", 0)
    before = owner._read_latest_payload()["chats"][chat.chat_id]
    with pytest.raises(ValueError, match="recovery session changed"):
        if operation == "reset":
            stale.new_session(chat.chat_id)
        else:
            monkeypatch.setattr(stale, "_resolve_and_validate_chat_model", lambda *args: "model")
            stale.handover_chat(chat.chat_id, provider="claude", model="model")
    after = owner._read_latest_payload()["chats"][chat.chat_id]
    assert after["session_id"] == before["session_id"]
    assert after["retry_recovery"] == before["retry_recovery"]


def test_recovery_opt_in_survives_restart_and_sends_only_continue(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", chat.user_turn_count)
    restarted = _make_manager(tmp_path)
    sent = []
    restarted.start_stream = lambda *args, **kwargs: sent.append((args, kwargs)) or object()  # type: ignore[method-assign,assignment]
    assert restarted.try_chat_retry_now(chat.chat_id) is not None
    assert sent[0][0] == (chat.chat_id, "continue")
    assert sent[0][1]["images"] is None


def test_recovery_does_not_replace_concurrent_pending_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    other = _make_manager(tmp_path)
    other.set_chat_retry(chat.chat_id, "unrelated")
    assert not pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", chat.user_turn_count)
    assert _make_manager(tmp_path).get_chat(chat.chat_id).retry_prompt == "unrelated"


def test_recovery_stale_manager_cannot_fire_after_other_manager_stops(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    other.stop_chat_retry(chat.chat_id)
    sent = []
    pcm.start_stream = lambda *args, **kwargs: sent.append(args)  # type: ignore[method-assign,assignment]
    assert pcm.try_chat_retry_now(chat.chat_id) is None
    assert not sent
    assert _make_manager(tmp_path).get_chat(chat.chat_id).retry_status == "stopped"


def test_recovery_will_not_fire_if_session_changes(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    chat.session_id = "session-2"
    pcm._save()
    restarted = _make_manager(tmp_path)
    sent = []
    restarted.start_stream = lambda *args, **kwargs: sent.append(args)  # type: ignore[method-assign,assignment]
    assert restarted.try_chat_retry_now(chat.chat_id) is None
    assert not sent


def test_concurrent_recovery_opt_ins_have_one_winner(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    other = _make_manager(tmp_path)
    barrier = Barrier(2)
    def schedule(manager):
        barrier.wait()
        return manager.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(schedule, [pcm, other]))
    assert sorted(results) == [False, True]
    saved = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert saved.retry_prompt == "continue"
    assert saved.retry_recovery["session_id"] == "session-1"


def test_concurrent_managers_only_one_claims_recovery_attempt(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    barrier = Barrier(2)
    sent = []
    for manager in (pcm, other):
        manager.start_stream = lambda *args, **kwargs: sent.append(args) or object()  # type: ignore[method-assign,assignment]
    def fire(manager):
        barrier.wait()
        return manager.try_chat_retry_now(chat.chat_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fire, [pcm, other]))
    assert sum(result is not None for result in results) == 1
    assert len(sent) == 1


def test_interrupted_recovery_is_stopped_on_restart(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    chat.last_error = ""
    chat.turn_owner = "previous-process"
    pcm._save()
    restored = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert restored.retry_status == "stopped"


def test_audit_and_quarantine_failure_leaves_recovery_nonrunnable(tmp_path: Path, monkeypatch) -> None:
    import os
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    replace = os.replace
    calls = 0
    def fail_second_replace(*args):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("quarantine unavailable")
        return replace(*args)
    def fail_audit(**kwargs):
        raise OSError("audit unavailable")
    monkeypatch.setattr(os, "replace", fail_second_replace)
    monkeypatch.setattr(pcm, "_append_registry_audit", fail_audit)
    with pytest.raises(OSError):
        pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    monkeypatch.undo()
    restored = _make_manager(tmp_path)
    sent = []
    restored.start_stream = lambda *args, **kwargs: sent.append(args)  # type: ignore[method-assign,assignment]
    assert restored.try_chat_retry_now(chat.chat_id) is None
    assert not sent
    assert restored.recovery_status(chat.chat_id)["recovery_state"] == "prepared"
    restored.stop_chat_retry(chat.chat_id)
    assert restored.recovery_status(chat.chat_id)["status"] == "stopped"


def test_activation_error_with_unavailable_readback_reports_actual_disk_status(tmp_path: Path, monkeypatch) -> None:
    import os
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    replace = os.replace
    read = pcm._read_latest_payload
    calls = 0
    def fail_after_activation(*args):
        nonlocal calls
        calls += 1
        replace(*args)
        if calls == 2:
            raise OSError("activation outcome unknown")
    def fail_readback():
        if calls >= 2:
            raise OSError("readback unavailable")
        return read()
    monkeypatch.setattr(os, "replace", fail_after_activation)
    monkeypatch.setattr(pcm, "_read_latest_payload", fail_readback)
    with pytest.raises(OSError):
        pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    monkeypatch.undo()
    status = pcm.recovery_status(chat.chat_id)
    assert status["recovery_state"] == "active"
    assert pcm.stop_recovery(chat.chat_id, status["recovery_id"])
    assert pcm.recovery_status(chat.chat_id)["status"] == "stopped"


def test_claim_blocks_unrelated_turn_and_stop_before_stream_starts(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    claimed, resume = Event(), Event()
    def start(*args, **kwargs):
        claimed.set()
        assert resume.wait(5)
        return object()
    pcm.start_stream = start  # type: ignore[method-assign,assignment]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pcm.try_chat_retry_now, chat.chat_id)
        assert claimed.wait(5)
        try:
            status = other.recovery_status(chat.chat_id)
            assert status["recovery_state"] == "in_flight"
            assert not other.stop_recovery(chat.chat_id, status["recovery_id"])
            with pytest.raises(ValueError):
                other.stop_chat_retry(chat.chat_id)
            assert other.set_chat_retry(chat.chat_id, "unrelated retry") is None
            async def refuse_turn():
                with pytest.raises(ValueError, match="recovery is in flight"):
                    other.start_stream(chat.chat_id, "unrelated")
            asyncio.run(refuse_turn())
            assert other.get_chat(chat.chat_id).last_error == "failed"
        finally:
            resume.set()
        assert future.result() is not None


@pytest.mark.parametrize("operation", ["archive", "delete", "reset", "handover"])
async def test_lifecycle_refuses_claim_before_side_effects(tmp_path: Path, operation: str, monkeypatch) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    chat.pending_queue = [{"id": "keep", "text": "do not lose", "images": []}]
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    claimed, resume = Event(), Event()
    def start(*args, **kwargs):
        claimed.set()
        assert resume.wait(5)
        return object()
    pcm.start_stream = start  # type: ignore[method-assign,assignment]
    touched = []
    monkeypatch.setattr(other._transcripts, "archive_session", lambda **kwargs: touched.append("archive"))
    monkeypatch.setattr(other._state, "delete_context", lambda *args: touched.append("delete"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pcm.try_chat_retry_now, chat.chat_id)
        assert claimed.wait(5)
        try:
            with pytest.raises(ValueError, match="recovery"):
                if operation == "archive":
                    await other.archive_chat(chat.chat_id)
                elif operation == "delete":
                    other.delete_chat(chat.chat_id)
                elif operation == "handover":
                    other.handover_chat(chat.chat_id, provider="claude", model="test")
                else:
                    other.new_session(chat.chat_id)
            assert not touched
            saved = other.get_chat(chat.chat_id)
            assert saved is not None and saved.session_id == "session-1"
            assert saved.pending_queue[0]["text"] == "do not lose"
        finally:
            resume.set()
        assert future.result() is not None


@pytest.mark.parametrize("operation", ["archive", "delete", "reset"])
async def test_lifecycle_reservation_blocks_other_manager_firing(tmp_path: Path, operation: str, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    manager._save()
    assert manager.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    started = []
    monkeypatch.setattr(other, "start_stream", lambda *a, **kw: started.append(True))
    observed = []

    def during_lifecycle(*args, **kwargs):
        observed.append(other.try_chat_retry_now(chat.chat_id))
        return None

    if operation == "archive":
        monkeypatch.setattr(manager, "_read_archive_inputs", lambda *args: (during_lifecycle(), None))
        await manager.archive_chat(chat.chat_id)
    elif operation == "delete":
        monkeypatch.setattr(manager._state, "delete_context", during_lifecycle)
        manager.delete_chat(chat.chat_id)
    else:
        monkeypatch.setattr(manager._transcripts, "archive_session", during_lifecycle)
        manager.new_session(chat.chat_id)
    assert observed == [None]
    assert not started
    assert other.try_chat_retry_now(chat.chat_id) is None


def test_lifecycle_reservation_blocks_activation_and_abandoned_reservation_recovers(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats

    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    manager._save()
    other = _make_manager(tmp_path)
    with manager._chat_lifecycle_guard(chat.chat_id):
        assert not other.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    payload = manager._read_latest_payload()
    payload["chats"][chat.chat_id]["recovery_lifecycle"] = {"owner": "99999999-dead", "id": "old"}
    manager._path.write_text(json.dumps(payload))
    monkeypatch.setattr(project_chats, "_recovery_owner_alive", lambda owner: False)
    restarted = _make_manager(tmp_path)
    assert restarted.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)


def test_literal_retry_still_fires_during_lifecycle_reservation(tmp_path: Path, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("retry", workspace="personal")
    chat = manager.create_chat(project.project_id, title="retry")
    manager.set_chat_retry(chat.chat_id, "literal prompt")
    other = _make_manager(tmp_path)
    sent = []
    monkeypatch.setattr(other, "start_stream", lambda *args, **kwargs: sent.append(args[1]))
    with manager._chat_lifecycle_guard(chat.chat_id):
        other.try_chat_retry_now(chat.chat_id)
    assert sent == ["literal prompt"]


def test_abandoned_recovery_claim_stops_on_restart(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    chat.retry_recovery = {**chat.retry_recovery, "claim": "claim", "owner": "2147483647-previous"}
    pcm._save()
    monkeypatch.setattr(project_chats, "_recovery_owner_alive", lambda owner: False)
    restarted = _make_manager(tmp_path)
    assert restarted.get_chat(chat.chat_id).retry_status == "stopped"


def test_live_foreign_owner_claim_is_not_reconciled_as_abandoned(tmp_path: Path) -> None:
    import os
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    chat.retry_recovery = {**chat.retry_recovery, "claim": "claim", "owner": f"{os.getpid()}-other"}
    pcm._save()
    other = _make_manager(tmp_path)
    assert other.recovery_status(chat.chat_id)["recovery_state"] == "in_flight"
    assert other.get_chat(chat.chat_id).retry_status == "pending"


def test_process_liveness_probe_is_safe_for_current_process() -> None:
    import os
    from ciao.os_support.processes import pid_alive
    assert pid_alive(os.getpid())
    assert pid_alive(0)  # Unknown rather than signalling a process group.


def test_stale_recovery_id_cannot_stop_replacement(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    old_id = pcm.recovery_status(chat.chat_id)["recovery_id"]
    assert pcm.stop_recovery(chat.chat_id, old_id)
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    new_id = pcm.recovery_status(chat.chat_id)["recovery_id"]
    assert new_id != old_id
    assert not pcm.stop_recovery(chat.chat_id, old_id)
    assert pcm.recovery_status(chat.chat_id)["recovery_id"] == new_id


@pytest.mark.parametrize("failure", ["before_replace", "after_replace", "replace_then_raise", "audit_oserror"])
def test_recovery_save_exception_never_fires_after_reload(tmp_path: Path, monkeypatch, failure: str) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    if failure == "before_replace":
        def fail_replace(*args, **kwargs):
            raise OSError("replace failed")
        monkeypatch.setattr(Path, "replace", fail_replace)
    elif failure == "replace_then_raise":
        original_replace = Path.replace
        def replace_then_fail(path, target):
            original_replace(path, target)
            raise OSError("replace outcome ambiguous")
        monkeypatch.setattr(Path, "replace", replace_then_fail)
    elif failure == "after_replace":
        def fail_audit(**kwargs):
            raise RuntimeError("audit failed after replace")
        monkeypatch.setattr(pcm, "_append_registry_audit", fail_audit)
    else:
        original_open = Path.open
        def fail_audit_open(path, *args, **kwargs):
            if path.name.endswith(".audit.jsonl"):
                raise OSError("audit unavailable")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", fail_audit_open)
    with pytest.raises((OSError, RuntimeError)):
        pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", chat.user_turn_count)
    monkeypatch.undo()
    restarted = _make_manager(tmp_path)
    sent = []
    restarted.start_stream = lambda *args, **kwargs: sent.append(args)  # type: ignore[method-assign,assignment]
    assert restarted.try_chat_retry_now(chat.chat_id) is None
    assert not sent
    assert pcm.get_chat(chat.chat_id).retry_status != "pending"


def test_recovery_refuses_stale_failure_and_missing_session(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    assert not pcm.schedule_continue_recovery(chat.chat_id, "", "failed", 0)
    chat.session_id = "session-1"
    pcm._save()
    other = _make_manager(tmp_path)
    other.get_chat(chat.chat_id).last_error = "new failure"
    other._save()
    assert not pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    assert _make_manager(tmp_path).get_chat(chat.chat_id).retry_status != "pending"


async def test_recovery_api_is_explicit_and_conflicts_on_pending(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    chat.failure_id = "failure-token"
    pcm._save()
    body = {"action": "continue_recovery", "failure_id": "failure-token"}
    async def read_json():
        return body
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm)),
                              path_params={"chat_id": chat.chat_id}, json=read_json)
    assert (await chat_retry(request)).status_code == 200
    assert chat.to_dict()["retry"]["mode"] == "continue_recovery"
    body.clear()
    body["action"] = "recovery_status"
    status = await chat_retry(request)
    assert json.loads(status.body)["recovery_state"] == "active"
    body.update({"action": "continue_recovery", "failure_id": "failure-token"})
    assert (await chat_retry(request)).status_code == 409
    pcm.stop_chat_retry(chat.chat_id)


async def test_recovery_api_reports_unknown_outcome(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.project_chats import RecoveryOutcomeUnknown
    from ciao.web.routes_api import chat_retry
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    chat.failure_id = "failure-token"
    pcm._save()
    def fail(*args, **kwargs):
        raise RecoveryOutcomeUnknown("unknown")
    monkeypatch.setattr(pcm, "schedule_continue_recovery", fail)
    async def read_json():
        return {"action": "continue_recovery", "failure_id": "failure-token"}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm)),
                              path_params={"chat_id": chat.chat_id}, json=read_json)
    response = await chat_retry(request)
    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "recovery_outcome_unknown"


async def test_recovery_status_and_guarded_stop_reconcile_stale_local_state(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    other = _make_manager(tmp_path)
    assert other.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    assert chat.retry_status != "pending"
    body = {"action": "recovery_status"}
    async def read_json():
        return body
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm)),
                              path_params={"chat_id": chat.chat_id}, json=read_json)
    status = json.loads((await chat_retry(request)).body)
    assert status["recovery_state"] == "active"
    body.update(action="stop")
    assert (await chat_retry(request)).status_code == 409
    body.update(action="stop_recovery", recovery_id="wrong-id")
    assert (await chat_retry(request)).status_code == 409
    body.update(action="stop_recovery", recovery_id=status["recovery_id"])
    assert (await chat_retry(request)).status_code == 200
    assert other.recovery_status(chat.chat_id)["status"] == "stopped"
    other._cancel_retry_task(chat.chat_id)


async def test_guarded_continue_now_rejects_stale_and_literal_retry(tmp_path: Path) -> None:
    from ciao.web.routes_api import chat_retry
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    chat.failure_id = "new-token"
    pcm._save()
    body = {"action": "continue_recovery_now", "failure_id": "old-token"}
    async def read_json():
        return body
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm)),
                              path_params={"chat_id": chat.chat_id}, json=read_json)
    assert (await chat_retry(request)).status_code == 409
    chat.session_id = ""
    body["failure_id"] = "new-token"
    assert (await chat_retry(request)).status_code == 409
    chat.session_id = "session-1"
    pcm.set_chat_retry(chat.chat_id, "dangerous original")
    assert chat.to_dict()["retry"]["mode"] == "literal"
    assert not chat.to_dict()["recovery_eligible"]
    body["failure_id"] = "new-token"
    assert (await chat_retry(request)).status_code == 409
    assert chat.retry_prompt == "dangerous original"
    pcm.stop_chat_retry(chat.chat_id)
    body["prompt"] = "dangerous original"
    assert (await chat_retry(request)).status_code == 400
    del body["prompt"]
    sent = []
    pcm.start_stream = lambda *args, **kwargs: sent.append((args, kwargs)) or object()  # type: ignore[method-assign,assignment]
    assert (await chat_retry(request)).status_code == 200
    assert sent[0][0] == (chat.chat_id, "continue")
    assert sent[0][1]["images"] is None
    assert chat.retry_recovery["manual"] is True
    assert chat.chat_id not in pcm._retry_tasks
    pcm._release_recovery_claim(chat.chat_id)
    pcm.stop_chat_retry(chat.chat_id)


async def test_distinct_failures_have_opaque_identity_in_completion(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    events = []
    original_publish = pcm._events.publish
    def capture(event):
        events.append(event)
        original_publish(event)
    pcm._events.publish = capture  # type: ignore[method-assign]
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        yield ResultEvent(type="result", result="same error", session_id="session-1", is_error=True,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    pcm.stream_chat = fake_stream  # type: ignore[assignment]
    first = pcm.start_stream(chat.chat_id, "one")
    await asyncio.wait_for(_consume(first), timeout=10)
    first_id = chat.failure_id
    assert first_id and chat.to_dict()["failure_id"] == first_id
    assert any(event.get("type") == "chat_streaming_done" and event.get("failure_id") == first_id for event in events)
    second = pcm.start_stream(chat.chat_id, "two")
    await asyncio.wait_for(_consume(second), timeout=10)
    assert chat.failure_id and chat.failure_id != first_id


async def test_recovery_failed_attempt_stays_hourly_then_success_stops(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    prompts = []
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        prompts.append((prompt, images))
        yield ResultEvent(type="result", result="unexpected failure" if len(prompts) == 1 else "done",
                          session_id="session-1", is_error=len(prompts) == 1,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    pcm.stream_chat = fake_stream  # type: ignore[assignment]
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", chat.user_turn_count)
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10)
    assert chat.retry_status == "pending"
    assert chat.retry_interval_seconds == 3600
    assert "claim" not in chat.retry_recovery
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10)
    assert chat.retry_status != "pending"
    assert prompts == [("continue", None), ("continue", None)]


@pytest.mark.parametrize("is_error", [False, True])
async def test_new_turn_stops_only_its_old_recovery(tmp_path: Path, is_error: bool) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        yield ResultEvent(type="result", result="ordinary failure" if is_error else "done",
                          session_id="session-1", is_error=is_error,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    pcm.stream_chat = fake_stream  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "unrelated new request")
    await asyncio.wait_for(_consume(stream), timeout=10)
    assert chat.retry_status != "pending"
    restored = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert restored.retry_status != "pending"
    assert pcm.try_chat_retry_now(chat.chat_id) is None


async def test_new_turn_does_not_cancel_literal_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    pcm.set_chat_retry(chat.chat_id, "literal")
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        yield ResultEvent(type="result", result="done", session_id="session-1", is_error=False,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    pcm.stream_chat = fake_stream  # type: ignore[assignment]
    await asyncio.wait_for(_consume(pcm.start_stream(chat.chat_id, "unrelated")), timeout=10)
    assert chat.retry_status == "pending"
    assert chat.retry_prompt == "literal"
    pcm.stop_chat_retry(chat.chat_id)


@pytest.mark.parametrize("replace_with_literal", [False, True])
async def test_new_turn_checks_current_retry_under_registry_lock(tmp_path: Path, replace_with_literal: bool) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    if replace_with_literal:
        assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
        other = _make_manager(tmp_path)
        other.set_chat_retry(chat.chat_id, "literal")
    else:
        other = _make_manager(tmp_path)
        assert other.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        yield ResultEvent(type="result", result="done", session_id="session-1", is_error=False,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    pcm.stream_chat = fake_stream  # type: ignore[assignment]
    await asyncio.wait_for(_consume(pcm.start_stream(chat.chat_id, "new request")), timeout=10)
    saved = _make_manager(tmp_path).get_chat(chat.chat_id)
    if replace_with_literal:
        assert saved.retry_status == "pending" and saved.retry_prompt == "literal"
    else:
        assert saved.retry_status == "stopped"
    other.stop_chat_retry(chat.chat_id) if replace_with_literal else other._cancel_retry_task(chat.chat_id)


def test_recovery_firing_refuses_concurrently_archived_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session-1", "failed", "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    other = _make_manager(tmp_path)
    other.get_chat(chat.chat_id).archived = True
    other._save()
    sent = []
    pcm.start_stream = lambda *args, **kwargs: sent.append(args)  # type: ignore[method-assign,assignment]
    assert pcm.try_chat_retry_now(chat.chat_id) is None
    assert not sent


async def test_recovery_timer_fires_after_restart(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    chat.last_error = "failed"
    chat.last_response_status = "error"
    pcm._save()
    assert pcm.schedule_continue_recovery(chat.chat_id, "session-1", "failed", 0)
    pcm._cancel_retry_task(chat.chat_id)
    chat.retry_next_at = "2000-01-01T00:00:00Z"
    pcm._save()
    restarted = _make_manager(tmp_path)
    called = asyncio.Event()
    sent = []
    async def fake_stream(chat_id, prompt, images=None, **kwargs):
        sent.append((prompt, images))
        called.set()
        yield ResultEvent(type="result", result="done", session_id="session-1", is_error=False,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)
    restarted.stream_chat = fake_stream  # type: ignore[assignment]
    await asyncio.wait_for(called.wait(), timeout=5)
    assert sent == [("continue", None)]
    stream = restarted._broker.get(chat.chat_id)
    if stream is not None:
        await asyncio.wait_for(_consume(stream), timeout=10)


@pytest.mark.parametrize("marker", [
    {"mode": "continue", "session_id": "session-1", "cadence_seconds": 3600},
    None,
    {"mode": "unknown"},
])
def test_recovery_record_cannot_replay_original_after_restart(tmp_path: Path, marker: object) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("recovery", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="recovery")
    chat.session_id = "session-1"
    pcm.set_chat_retry(chat.chat_id, "original dangerous prompt", image_refs=["image.png"])
    path = tmp_path / ".runtime" / "web_projects.json"
    payload = json.loads(path.read_text())
    payload["chats"][chat.chat_id]["retry_recovery"] = marker
    path.write_text(json.dumps(payload))
    restarted = _make_manager(tmp_path)
    sent = []
    restarted.start_stream = lambda *args, **kwargs: sent.append((args, kwargs))  # type: ignore[method-assign,assignment]
    assert restarted.try_chat_retry_now(chat.chat_id) is None
    assert sent == []


async def test_quota_error_marks_turn_for_hourly_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="sess-retry",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_attempts == 0
    assert updated.retry_next_at
    assert updated.retry_interval_seconds == 3600
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    pcm.stop_chat_retry(chat.chat_id)


CLAUDE_FIVE_HOUR_BANNER = "You've hit your 5-hour limit · resets 7:20pm (Europe/Zurich)"
CLAUDE_FIVE_HOUR_QUOTA = {
    "status": "rejected",
    "rateLimitType": "five_hour",
    "resetsAt": "1794507600",
}


@pytest.mark.parametrize(
    "quota",
    [{}, CLAUDE_FIVE_HOUR_QUOTA],
    ids=["without-quota", "with-rejected-five-hour-quota"],
)
async def test_claude_five_hour_limit_arms_retry(
    tmp_path: Path, quota: dict[str, str]
) -> None:
    """The five-hour exhaustion banner is a usage limit, with or without quota
    telemetry: the text is the admission gate and the retry must be armed."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result=CLAUDE_FIVE_HOUR_BANNER,
            session_id="sess-five-hour",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota=quota,
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "quota"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_last_error == CLAUDE_FIVE_HOUR_BANNER
    assert updated.retry_interval_seconds == 3600
    assert any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


async def test_claude_five_hour_limit_after_progress_continues(tmp_path: Path) -> None:
    """A five-hour limit that lands after output streamed resumes the live
    session with "continue" instead of replaying the prompt."""
    from ciao.models import AssistantTextDelta
    from ciao.web.project_chats import _RESUME_CONTINUE_PROMPT

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.session_id = "sess-live"
    pcm._save()
    prompts: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            yield AssistantTextDelta(type="assistant", text="sending the email… ")
            yield ResultEvent(
                type="result",
                result=CLAUDE_FIVE_HOUR_BANNER,
                session_id="sess-live",
                is_error=True,
                effective_model=chat.model,
                usage={},
                quota=CLAUDE_FIVE_HOUR_QUOTA,
                cost_usd=0.0,
            )
            return
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-live",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "send the email then reply")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == _RESUME_CONTINUE_PROMPT
    assert updated.retry_image_refs == []
    assert updated.retry_interval_seconds == 3600

    retried = pcm.try_chat_retry_now(chat.chat_id)
    assert retried is not None
    await asyncio.wait_for(_consume(retried), timeout=10.0)

    assert prompts == ["send the email then reply", _RESUME_CONTINUE_PROMPT]
    finished = pcm.get_chat(chat.chat_id)
    assert finished is not None
    assert finished.retry_status == ""


async def test_unrelated_error_with_rejected_quota_does_not_retry(tmp_path: Path) -> None:
    """Rejected quota telemetry can accompany an unrelated failure; only the
    error text arms a retry."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="Error: the tool crashed while reading the log file",
            session_id="sess-crash",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota=CLAUDE_FIVE_HOUR_QUOTA,
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )


def test_manual_retry_can_be_set_and_stopped(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    updated = pcm.set_chat_retry(chat.chat_id, "retry this manually", reason="manual")
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "retry this manually"
    assert updated.retry_last_error == "manual"

    stopped = pcm.stop_chat_retry(chat.chat_id)
    assert stopped is not None
    assert stopped.retry_status == "stopped"
    assert stopped.retry_prompt == ""


def test_manual_retry_is_hourly_after_a_daily_automatic_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_interval_seconds = 24 * 60 * 60

    updated = pcm.set_chat_retry(chat.chat_id, "retry this hourly", reason="manual")

    assert updated is not None
    assert updated.retry_interval_seconds == 60 * 60


async def test_try_retry_now_starts_stream_when_chat_is_idle(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run me", reason="manual")

    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-ok",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert calls == ["run me"]
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == ""
    assert updated.retry_prompt == ""


async def test_try_retry_now_tags_the_resend_as_a_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run me", reason="manual", unattended=True)

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-ok",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    echo = next(e for e in events if e.get("type") == "user_echo")
    assert echo["origin"] == "retry"
    assert echo["unattended"] is True
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.user_turn_origin == {"0": "retry"}
    assert updated.user_turn_unattended == {"0": True}


async def test_try_retry_now_refuses_active_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run later", reason="manual")

    release = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        await release.wait()
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-active",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    active_stream = pcm.start_stream(chat.chat_id, "already running")
    consumer = asyncio.create_task(_consume(active_stream))
    await asyncio.sleep(0.01)

    assert pcm.try_chat_retry_now(chat.chat_id) is None

    release.set()
    await asyncio.wait_for(consumer, timeout=10.0)


async def test_retry_due_on_busy_chat_defers_minutes_not_interval(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-busy")

    release = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        await release.wait()
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-busy",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    active_stream = pcm.start_stream(chat.chat_id, "already running")
    consumer = asyncio.create_task(_consume(active_stream))
    await asyncio.sleep(0.01)
    # Armed after the turn started: starting a turn clears a pending retry.
    pcm.set_chat_retry(
        chat.chat_id,
        "run later",
        reason="quota",
        next_at=chat_service._iso_after(0),
        interval_seconds=8 * 60 * 60,
    )
    # set_chat_retry started the chat's retry loop; drive that one, not a second.
    loop_task = pcm._retry_tasks[chat.chat_id]
    await asyncio.sleep(0.05)

    due = chat_service._parse_iso(pcm.get_chat(chat.chat_id).retry_next_at)
    assert due is not None
    remaining = (due - chat_service._parse_iso(chat_service._iso_after(0))).total_seconds()
    assert 0 < remaining <= 5 * 60

    loop_task.cancel()
    release.set()
    await asyncio.wait_for(consumer, timeout=10.0)


# ── Auto tier-fallback on capability errors (ciao/web/project_chats.py) ──


async def test_capability_error_triggers_tier_fallback(tmp_path: Path) -> None:
    """A 4xx saying 'does not support image input' surfaces as an error result.

    The auto tier-fallback ladder was removed; a capability error is now
    reported to the PWA as the single result of the turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("tier-fallback", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-test")
    chat.model = "fable"
    pcm._save()
    # This test exercises the dispatch path, not the pre-flight, so let the
    # turn through as if the pre-flight approved the model.
    async def _always_capable(model, chat):
        return True

    pcm._model_capable = _always_capable  # type: ignore[assignment]

    seen_models: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        seen_models.append(request.model)
        outcome.response_text = "API Error: 400 this model does not support image input"
        outcome.had_error = True
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result=(
                "API Error: 400 this model does not support image "
                "input (ref: test)"
            ),
            session_id="sess-1",
            is_error=True,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    from ciao.models import ImageAttachment

    stream = pcm.start_stream(
        chat.chat_id,
        "what is in the image?",
        images=[
            ImageAttachment(
                path=tmp_path / "shot.png",
                mime_type="image/png",
                original_filename="shot.png",
            )
        ],
    )
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    # Only the original model is dispatched; no silent retry.
    assert seen_models == ["fable"]
    # Exactly one result — the error, surfaced to the PWA.
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1, f"expected exactly one result, got {result_events}"
    final = result_events[0]
    assert final.get("is_error") is True
    assert "does not support image" in final.get("text", "")
    # No model_changed event: the model was not swapped.
    model_changed = [e for e in events if e.get("type") == "model_changed"]
    assert model_changed == []
    # The chat keeps its original model.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "fable"


async def test_rate_limit_does_not_trigger_tier_fallback(tmp_path: Path) -> None:
    """Rate limit errors are NOT capability errors — no retry, error surfaces."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("tier-fallback-no-retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-no-retry-test")
    chat.model = "fable"
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        outcome.response_text = "API Error: 429 Rate Limit Exceeded"
        outcome.had_error = True
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result="API Error: 429 Rate Limit Exceeded",
            session_id="sess-rl",
            is_error=True,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    # _drive_stream is only called once — the retry is gated on
    # is_capability_error, which rejects rate limits.
    assert drive_calls == ["fable"]
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[-1].get("is_error") is True
    # No model_changed when fallback does not run.
    assert not any(e.get("type") == "model_changed" for e in events)
    # Chat model stays on the original.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "fable"


async def test_connection_error_marks_turn_for_fast_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Unable to connect to API (ENOTFOUND)",
            session_id="sess-retry",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_attempts == 0
    assert updated.retry_next_at
    assert updated.retry_interval_seconds == 30
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    pcm.stop_chat_retry(chat.chat_id)


async def test_connection_error_exception_marks_turn_for_fast_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        raise Exception("Unable to connect to API (ENOTFOUND)")
        yield ResultEvent(type="result", result="not reached")  # type: ignore[unreachable]

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_interval_seconds == 30
    assert any(
        event.get("type") == "chat_retry" and event.get("status") == "pending"
        for event in events
    )
    pcm.stop_chat_retry(chat.chat_id)


def test_connection_drop_banner_classified_as_connection_error() -> None:
    """The CLI mid-response drop banner must be a retryable connection error."""
    from ciao.web.chat_service import (
        _is_retryable_connection_error,
        _is_retryable_provider_startup_error,
        _is_retryable_quota_error,
    )

    banner = (
        "API Error: Connection closed mid-response. The response above may "
        "be incomplete."
    )
    assert _is_retryable_connection_error(banner) is True
    assert _is_retryable_provider_startup_error(
        "opencode serve exited with code 1: database is locked"
    ) is True
    assert _is_retryable_provider_startup_error(
        "the model said database is locked"
    ) is False
    # A server that stayed alive but never answered /api/info is the
    # same transient startup wedge (shared SQLite contention) and gets the
    # same bounded auto-retry.
    assert _is_retryable_provider_startup_error(
        "opencode serve did not become healthy: "
        "server stayed alive but never answered /api/info"
    ) is True
    # It is NOT a quota error — must not be routed to the hourly retry path.
    assert _is_retryable_quota_error(banner) is False

    # Out of credits / spend limits must be classified as retryable quota errors
    credits_err = "Title generation failed: Your workspace is out of credits. Ask your workspace owner to refill in order to continue."
    spend_limit_err = "You've hit your org's monthly spend limit"
    rate_limit_err = "API Error: Request rejected (429): rate limit exceeded"
    claude_code_session_limit = (
        "You've hit your session limit - resets 2:20pm (Europe/Zurich)"
    )
    assert _is_retryable_quota_error(credits_err) is True
    assert _is_retryable_quota_error(spend_limit_err) is True
    assert _is_retryable_quota_error(rate_limit_err) is True
    assert _is_retryable_quota_error(claude_code_session_limit) is True
    # OpenRouter rejects a turn once the balance cannot cover it, as a 402 with
    # no rate-limit noun at all. It is still a budget error: the turn recovers
    # on its own as soon as the account is topped up.
    assert _is_retryable_quota_error(
        "This request would exceed your available credits given your "
        "current balance. Add credits to continue."
    ) is True


async def test_opencode_startup_error_is_persisted_and_retried(tmp_path: Path) -> None:
    """Pre-session provider failures must not leave an empty scheduled chat."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(
        project.project_id,
        title="retry-test",
        provider="opencode",
    )

    async def fail_before_provider_session(*_args, **_kwargs):
        if False:  # pragma: no cover - keeps this a minimal async generator
            yield None
        raise RuntimeError("opencode serve exited with code 1: database is locked")

    pcm.stream_chat = fail_before_provider_session  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "run the scheduled review")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_interval_seconds == 30
    assert any(
        event.get("type") == "result"
        and event.get("is_error") is True
        and "database is locked" in event.get("text", "")
        for event in events
    )

    rows = pcm._transcripts.current_messages(
        ChatContext.for_web(chat.chat_id), "opencode"
    )
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[-1]["is_error"] is True
    assert "database is locked" in rows[-1]["content"]
    pcm.stop_chat_retry(chat.chat_id)



def test_provider_detects_connection_drop_text() -> None:
    from ciao.providers.claude import _is_connection_drop_text

    assert _is_connection_drop_text(
        "API Error: Connection closed mid-response. The response above may "
        "be incomplete."
    ) is True
    assert _is_connection_drop_text("here is a normal answer") is False


async def test_midresponse_drop_resumes_with_continue_not_replay(tmp_path: Path) -> None:
    """A drop AFTER streaming output resumes with "continue", not the prompt.

    Replaying the original prompt could re-run tool calls the partial turn
    already executed, so with provider progress the retry must ask the
    resumed session to continue instead. It must also fire on the fast (30s)
    interval and preserve any queued follow-ups.
    """
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # A mid-response drop implies a live session to resume. In production
    # _drive_stream persists this from the event; the fake here bypasses it.
    chat.session_id = "sess-drop"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        # Stream some output first so had_provider_progress flips True.
        yield AssistantTextDelta(type="assistant", text="Working on it… ")
        yield ResultEvent(
            type="result",
            result=(
                "API Error: Connection closed mid-response. The response "
                "above may be incomplete."
            ),
            session_id="sess-drop",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the big multi-step task")
    # Queue a follow-up while the turn is (about to be) in flight.
    stream.enqueue("and then summarise it", entry_id="q-follow")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    # Resume-continue, NOT a replay of the original prompt.
    assert updated.retry_prompt == "continue"
    assert updated.retry_interval_seconds == 30
    # The queued follow-up survived the errored turn (parked for the retry).
    assert [e.get("text") for e in updated.pending_queue] == ["and then summarise it"]
    pcm.stop_chat_retry(chat.chat_id)


async def test_quota_error_with_progress_resumes_continue(tmp_path: Path) -> None:
    """A usage-limit error that lands mid-turn (after output streamed) must
    resume the live session with "continue" rather than replaying the prompt,
    which would re-run any tool calls the partial turn already executed."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # A mid-turn limit implies a live session to resume (see the drop test).
    chat.session_id = "sess-live"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        # A tool ran / text streamed before the limit hit → had progress.
        yield AssistantTextDelta(type="assistant", text="sending the email… ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="sess-live",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "send the email then reply")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    # Resume-continue, NOT a replay that would re-send the email.
    assert updated.retry_prompt == "continue"
    # Still gated by the hourly quota interval, not the fast connection one.
    assert updated.retry_interval_seconds == 3600
    pcm.stop_chat_retry(chat.chat_id)


async def test_non_billing_rate_limit_with_progress_resumes_continue(tmp_path: Path) -> None:
    """A plain (non-billing) rate-limit 429, e.g. a provider's weekly usage
    limit, landing mid-turn with a live session must also resume with
    "continue" rather than being dropped without any retry armed."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.session_id = "sess-live"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="editing files… ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429) · rate limit exceeded, try again later",
            session_id="sess-live",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "fix the bug and run the tests")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "continue"
    assert updated.retry_interval_seconds == 3600
    pcm.stop_chat_retry(chat.chat_id)


async def test_midresponse_drop_without_session_does_not_replay(tmp_path: Path) -> None:
    """Partial work cannot safely be replayed without a resumable session."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # No session_id set — nothing to resume.

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="starting… ")
        yield ResultEvent(
            type="result",
            result="API Error: Connection closed mid-response.",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"


@pytest.mark.parametrize("result", ["success", "error", "connection"])
@pytest.mark.parametrize("replacement", ["literal", "network", "recreate"])
async def test_retry_stream_cannot_change_replacement_slot(
    tmp_path: Path, result: str, replacement: str
) -> None:
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        started.set()
        await release.wait()
        if result == "connection":
            yield AssistantTextDelta(type="assistant", text="partial")
        yield ResultEvent(
            type="result", result={"success": "done", "error": "unexpected failure",
                                   "connection": "API Error: Connection closed mid-response."}[result],
            session_id="", is_error=result != "success", effective_model=chat.model,
            usage={}, quota={}, cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    pcm.set_chat_retry(chat.chat_id, "original")
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(started.wait(), 10)
    if replacement == "recreate":
        pcm.stop_chat_retry(chat.chat_id)
    if replacement == "network":
        assert pcm.arm_network_retry(chat.chat_id, "replacement") == "armed"
    else:
        pcm.set_chat_retry(chat.chat_id, "replacement")
    before = (chat.retry_status, chat.retry_prompt, chat.retry_kind, chat.retry_attempts,
              chat.retry_next_at, chat.retry_last_error, chat.retry_interval_seconds)
    release.set()
    await asyncio.wait_for(_consume(stream), 10)
    assert (chat.retry_status, chat.retry_prompt, chat.retry_kind, chat.retry_attempts,
            chat.retry_next_at, chat.retry_last_error, chat.retry_interval_seconds) == before
    pcm.stop_chat_retry(chat.chat_id)


async def test_missing_session_stops_owned_retry_after_partial_work(tmp_path: Path) -> None:
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    calls = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append((prompt, images))
        yield AssistantTextDelta(type="assistant", text="partial")
        yield ResultEvent(type="result", result="API Error: Connection closed mid-response.",
                          session_id="", is_error=True, effective_model=chat.model,
                          usage={}, quota={}, cost_usd=0.0)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    pcm.set_chat_retry(chat.chat_id, "original")
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), 10)
    assert calls == [("original", None)]
    assert chat.retry_status == "stopped"
    assert chat.retry_prompt == ""


async def test_replacement_timer_survives_old_timer_cleanup(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "first")
    old = pcm._retry_tasks[chat.chat_id]
    await asyncio.sleep(0)
    pcm.set_chat_retry(chat.chat_id, "replacement")
    new = pcm._retry_tasks[chat.chat_id]
    assert new is not old
    await asyncio.gather(old, return_exceptions=True)
    assert pcm._retry_tasks[chat.chat_id] is new
    assert not new.cancelled()
    pcm.stop_chat_retry(chat.chat_id)


async def test_literal_replacement_does_not_inherit_network_budget(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    assert pcm.arm_network_retry(chat.chat_id, "network") == "armed"
    chat.retry_attempts = 3
    pcm.set_chat_retry(chat.chat_id, "literal")
    assert chat.retry_kind == ""
    assert chat.retry_attempts == 0
    assert chat.retry_interval_seconds == 3600
    pcm.stop_chat_retry(chat.chat_id)


async def test_unrelated_turn_does_not_replace_pending_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "scheduled")
    before = (chat.retry_prompt, chat.retry_next_at, chat.retry_attempts)

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(type="result", result="API Error: Connection closed mid-response.",
                          session_id="", is_error=True, effective_model=chat.model,
                          usage={}, quota={}, cost_usd=0.0)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "unrelated")
    await asyncio.wait_for(_consume(stream), 10)
    assert chat.retry_status == "pending"
    assert (chat.retry_prompt, chat.retry_next_at, chat.retry_attempts) == before
    pcm.stop_chat_retry(chat.chat_id)


async def test_midresponse_drop_stops_after_retry_cap(tmp_path: Path) -> None:
    """Once the connection-drop retry cap is hit, no further retry is armed."""
    from ciao.models import AssistantTextDelta
    from ciao.web.project_chats import _MAX_CONNECTION_DROP_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # Simulate a live session plus an already-exhausted retry budget.
    chat.session_id = "sess-drop"
    chat.retry_attempts = _MAX_CONNECTION_DROP_RETRIES
    chat.retry_kind = "connection"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="partial… ")
        yield ResultEvent(
            type="result",
            result="API Error: Connection closed mid-response.",
            session_id="sess-drop",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    # No retry armed past the cap.
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )


async def test_billing_spend_limit_error_with_progress_arms_retry(tmp_path: Path) -> None:
    """Billing and spend limit errors must trigger auto-retry even after provider progress."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.session_id = "sess-billing"

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="some progress... ")
        yield ResultEvent(
            type="result",
            result="You've hit your org's monthly spend limit · run /usage-credits to ask your admin for a higher limit",
            session_id="sess-billing",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "continue"
    assert any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


async def test_rate_limit_error_with_progress_arms_retry(tmp_path: Path) -> None:
    """A plain rate-limit 429 must auto-retry after progress too, same as a
    billing/spend-limit error — the earlier carve-out that skipped arming
    for non-billing quota errors once anything had streamed left users with
    a bare error and no retry affordance for the common "usage limit" case."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.session_id = "sess-rate"

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="some progress... ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="sess-rate",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "continue"
    assert any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


def test_a_plainly_stated_usage_limit_arms_the_hourly_retry() -> None:
    """opencode/OpenAI states the limit without a 429 or a vendor phrasing.

    "The usage limit has been reached" contains "usage limit", but that needle
    used to sit behind a `429`/`too many requests` gate, so the turn fell
    through to unknown: no hourly retry armed, and the user had to sit on the
    chat and press Retry by hand.
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("The usage limit has been reached") is True
    assert _is_retryable_quota_error("Usage limit reached") is True
    assert _is_retryable_quota_error("You have exceeded your quota") is True
    assert _is_retryable_quota_error("Token limit exhausted for this key") is True


def test_a_tool_quota_error_does_not_arm_the_provider_retry() -> None:
    """"quota" alone is a noun tools and the OS use too, so it is not a provider limit.

    "disk quota exceeded" ended a turn and armed the hourly provider retry,
    re-running the turn against a problem that is not a provider limit. A
    quota counts only when it is the caller's own (exceeded your quota) or a
    named provider's (OpenAI quota exceeded).
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("OSError: [Errno 122] Disk quota exceeded") is False
    assert (
        _is_retryable_quota_error(
            "Google API quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute'"
        )
        is False
    )
    assert _is_retryable_quota_error("storage quota reached") is False
    assert _is_retryable_quota_error("You have exceeded your daily request quota for this API") is False
    assert (
        _is_retryable_quota_error(
            "You exceeded your current quota, please check your plan and billing details"
        )
        is True
    )
    assert _is_retryable_quota_error("OpenAI quota exceeded") is True
    assert _is_retryable_quota_error("API Error: 429 Too Many Requests: quota") is True


def test_a_limit_noun_alone_still_needs_the_429_marker() -> None:
    """The gate exists so ordinary prose mentioning a quota is not a quota error.

    Only pairing the noun with an exhaustion verb bypasses it; the bare noun
    must still carry the 429 / too-many-requests marker.
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("I checked the quota settings for you") is False
    assert _is_retryable_quota_error("the session was restored") is False
    assert _is_retryable_quota_error("explain how rate limit headers work") is False


OLLAMA_SESSION_LIMIT = (
    "API Error: Request rejected (429) · you (x) have reached your session usage "
    "limit, upgrade for higher limits: https://ollama.com/upgrade"
)
OLLAMA_WEEKLY_LIMIT = (
    "API Error: Request rejected (429) · you (x) have reached your weekly usage "
    "limit, upgrade for higher limits: https://ollama.com/upgrade"
)


def test_weekly_and_monthly_limits_are_ordinary_retryable_quota_errors() -> None:
    """There is no daily tier: weekly, monthly and "upgrade" phrasings retry like any limit."""
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error(OLLAMA_WEEKLY_LIMIT) is True
    assert _is_retryable_quota_error(OLLAMA_SESSION_LIMIT) is True
    assert _is_retryable_quota_error("You've hit your weekly usage limit") is True
    assert _is_retryable_quota_error("You've hit your monthly limit") is True


def test_session_limit_and_capacity_stay_retryable() -> None:
    from ciao.web.chat_service import _is_retryable_quota_error

    session = "API Error: Request rejected (429) · you have reached your session usage limit"
    capacity = "Model is at capacity, try again shortly"
    for text in (session, capacity, "You've hit your session limit - resets 2:20pm"):
        assert _is_retryable_quota_error(text) is True


def test_claude_five_hour_limit_classification_is_narrow() -> None:
    """The standalone five-hour banner counts; a tool quoting it, a bare limit
    noun, and a negated sentence do not."""
    from ciao.web.chat_service import _is_retryable_quota_error

    for text in (
        "You've hit your 5-hour limit",
        "You’ve hit your 5-hour limit",
        "You've hit your 5-hour limit · resets 7:20pm (Europe/Zurich)",
        "You've hit your 5-hour limit · resets 6am (UTC)",
        "You’ve hit your 5-hour limit · resets 7:20pm (Europe/Zurich)",
        "Error: You've hit your 5-hour limit",
        "Error: You've hit your 5-hour limit · resets 7:20pm (Europe/Zurich)",
        "  You've hit your 5-hour limit  ",
        # The other quota matchers still apply when their own trigger is there.
        "You've hit your 5-hour limit, upgrade for higher limits",
    ):
        assert _is_retryable_quota_error(text) is True

    for text in (
        "Google API: You've hit your 5-hour limit",
        "You've hit your 5-hour limit for the Google API",
        "You've hit your 5-hour limit, ask your admin",
        "You've hit your 5-hour limit · resets 7:20pm\n...and the tool printed more output",
        "You haven't hit your 5-hour limit",
        "5-hour limit",
        "you can hit your 5-hour limit after a long session",
        "OSError: [Errno 122] Disk quota exceeded",
        "storage quota reached",
        "You have exceeded your daily request quota for this API",
    ):
        assert _is_retryable_quota_error(text) is False


@pytest.mark.parametrize("text", [
    "You've hit your 5-hour limit · resets",
    "You've hit your 5-hour limit · resets 7:20pm for the Google API",
])
def test_claude_five_hour_limit_rejects_invalid_reset_details(text: str) -> None:
    assert not chat_service._is_retryable_quota_error(text)


@pytest.mark.parametrize("limit_text", [OLLAMA_SESSION_LIMIT, OLLAMA_WEEKLY_LIMIT])
async def test_ollama_limit_error_arms_hourly_quota_retry(tmp_path: Path, limit_text: str) -> None:
    """Ollama session and weekly limits both arm the ordinary hourly quota retry."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result=limit_text,
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "quota"
    assert updated.retry_interval_seconds == 3600
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    assert any(
        e.get("type") == "result" and e.get("is_error") for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


def test_bare_monthly_limit_without_exhaustion_is_not_a_quota_error() -> None:
    from ciao.web.chat_service import _is_retryable_quota_error

    assert not _is_retryable_quota_error("Stripe: this plan has a monthly limit of 100 invoices")
    assert _is_retryable_quota_error("You have reached your monthly limit")


def test_quota_reset_at_ignores_rejected_weekly_bucket(tmp_path: Path) -> None:
    """A rejected weekly bucket no longer moves the retry: it stays hourly."""
    import json
    import time

    from ciao.rate_limits import default_store_path

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test", provider="claude")
    store = default_store_path(pcm._runtime_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    resets_at = int(time.time()) + 3 * 86400
    store.write_text(
        json.dumps({"buckets": {"seven_day": {"status": "rejected", "resets_at": resets_at}}}),
        encoding="utf-8",
    )
    assert pcm._quota_reset_at(chat, "quota") is None

    chat.provider = "opencode"
    store.write_text(
        json.dumps({"buckets": {"five_hour": {"status": "rejected", "resets_at": resets_at}}}),
        encoding="utf-8",
    )
    assert pcm._quota_reset_at(chat, "quota") is None
    chat.provider = "claude"

    store.write_text(
        json.dumps({"buckets": {"five_hour": {"status": "rejected", "resets_at": 1}}}),
        encoding="utf-8",
    )
    assert pcm._quota_reset_at(chat, "quota") is None

    store.write_text("{not json", encoding="utf-8")
    assert pcm._quota_reset_at(chat, "quota") is None


def test_quota_reset_at_uses_claude_five_hour_bucket_for_session_limits(tmp_path: Path) -> None:
    """An hourly-kind quota retry lands just after a rejected five-hour bucket resets."""
    import json
    import time
    from datetime import UTC, datetime

    from ciao.rate_limits import default_store_path

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test", provider="claude")
    store = default_store_path(pcm._runtime_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    resets_at = int(time.time()) + 2 * 3600
    store.write_text(
        json.dumps({
            "buckets": {
                "five_hour": {"status": "rejected", "resets_at": resets_at},
                "seven_day": {"status": "allowed", "resets_at": resets_at + 86400},
            },
        }),
        encoding="utf-8",
    )

    expected = datetime.fromtimestamp(resets_at + 60, UTC).replace(microsecond=0)
    assert pcm._quota_reset_at(chat, "quota") == expected.isoformat().replace("+00:00", "Z")
    # Other kinds read no bucket at all.
    assert pcm._quota_reset_at(chat, "connection") is None


async def test_five_hour_retry_uses_existing_reset_snapshot(tmp_path: Path) -> None:
    """A five-hour banner waits for the rejected bucket's reset when the rate
    limit store knows it, and keeps the hourly fallback when it does not. The
    terminal's quota telemetry is not that store."""
    import json
    import time
    from datetime import UTC, datetime

    from ciao.rate_limits import default_store_path

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    store = default_store_path(pcm._runtime_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    resets_at = int(time.time()) + 2 * 3600
    far_future = int(time.time()) + 30 * 86400
    quota = {**CLAUDE_FIVE_HOUR_QUOTA, "resetsAt": str(far_future)}

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result=CLAUDE_FIVE_HOUR_BANNER,
            session_id="sess-five-hour",
            is_error=True,
            effective_model="claude-sonnet-4-5",
            usage={},
            quota=quota,
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    chat = pcm.create_chat(project.project_id, title="retry-test", provider="claude")
    store.write_text(
        json.dumps({
            "buckets": {"five_hour": {"status": "rejected", "resets_at": resets_at}},
        }),
        encoding="utf-8",
    )
    await asyncio.wait_for(
        _consume(pcm.start_stream(chat.chat_id, "do the thing")), timeout=10.0
    )

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_interval_seconds == 3600
    expected = datetime.fromtimestamp(resets_at + 60, UTC).replace(microsecond=0)
    assert updated.retry_next_at == expected.isoformat().replace("+00:00", "Z")
    pcm.stop_chat_retry(chat.chat_id)

    store.unlink()
    other = pcm.create_chat(project.project_id, title="retry-test-2", provider="claude")
    await asyncio.wait_for(
        _consume(pcm.start_stream(other.chat_id, "do the thing")), timeout=10.0
    )

    updated = pcm.get_chat(other.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_interval_seconds == 3600
    due = chat_service._parse_iso(updated.retry_next_at)
    now = chat_service._parse_iso(chat_service._iso_after(0))
    assert due is not None and now is not None
    # The hourly fallback, not the terminal's fake 30-day quota resetsAt.
    assert 3500 <= (due - now).total_seconds() <= 3700
    pcm.stop_chat_retry(other.chat_id)


async def test_consecutive_quota_retries_stay_hourly(tmp_path: Path) -> None:
    """Each further session-limit failure waits the same hour: no backoff."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    intervals = [pcm.get_chat(chat.chat_id).retry_interval_seconds]  # type: ignore[union-attr]
    for _ in range(4):
        retried = pcm.try_chat_retry_now(chat.chat_id)
        assert retried is not None
        await asyncio.wait_for(_consume(retried), timeout=10.0)
        intervals.append(pcm.get_chat(chat.chat_id).retry_interval_seconds)  # type: ignore[union-attr]

    assert intervals == [3600] * 5
    pcm.stop_chat_retry(chat.chat_id)


async def test_unattended_quota_turn_is_replayed_unattended(tmp_path: Path) -> None:
    """A scheduled turn that hits a usage limit keeps its unattended flag on replay,
    including across a restart that reloads the retry state from disk."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    seen: list[bool] = []

    async def failing_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="usage limit reached",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = failing_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "scheduled care", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    failed = pcm.get_chat(chat.chat_id)
    assert failed is not None
    assert failed.retry_status == "pending"
    assert failed.retry_kind == "quota"
    assert failed.retry_unattended is True

    reloaded = _make_manager(tmp_path)
    restored = reloaded.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.retry_unattended is True

    async def ok_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="done",
            session_id="",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    reloaded.stream_chat = ok_stream_chat  # type: ignore[assignment]
    retried = reloaded.try_chat_retry_now(chat.chat_id)
    assert retried is not None
    await asyncio.wait_for(_consume(retried), timeout=10.0)

    assert seen == [True, True]
    after = reloaded.get_chat(chat.chat_id)
    assert after is not None
    replay_index = str(after.user_turn_count - 1)
    assert after.user_turn_unattended.get(replay_index) is True
    assert after.retry_status == ""
    assert after.retry_unattended is False


async def test_attended_quota_turn_is_replayed_attended(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    seen: list[bool] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="usage limit reached",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "typed by a person")
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    assert pcm.get_chat(chat.chat_id).retry_unattended is False  # type: ignore[union-attr]

    retried = pcm.try_chat_retry_now(chat.chat_id)
    assert retried is not None
    await asyncio.wait_for(_consume(retried), timeout=10.0)
    assert seen == [False, False]
    pcm.stop_chat_retry(chat.chat_id)


async def test_failed_turn_sets_last_error_and_next_turn_clears_it(tmp_path: Path) -> None:
    """A failed turn persists its error on the chat and in the done event; a clean turn clears it."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    pcm._events.publish = capture_publish  # type: ignore[method-assign]

    outcomes = iter([
        ResultEvent(
            type="result",
            result="API Error: Request rejected (400): bad request body",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        ),
        ResultEvent(
            type="result",
            result="all good",
            session_id="",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        ),
    ])

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield next(outcomes)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "first")
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    await asyncio.sleep(0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert "bad request body" in updated.last_error
    assert updated.to_dict()["last_error"] == updated.last_error
    done = [e for e in published if e.get("type") == "chat_streaming_done"]
    assert done and done[-1]["last_error"] == updated.last_error

    stream = pcm.start_stream(chat.chat_id, "second")
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    await asyncio.sleep(0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.last_error == ""
    done = [e for e in published if e.get("type") == "chat_streaming_done"]
    assert done[-1]["last_error"] == ""


async def test_quota_retries_stop_after_cap(tmp_path: Path) -> None:
    """After _MAX_QUOTA_RETRIES hourly retries, a further quota error arms nothing."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = _MAX_QUOTA_RETRIES
    chat.retry_kind = "quota"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )


async def test_invalidated_grant_is_labelled_and_not_retried(tmp_path: Path) -> None:
    """An invalidated OAuth grant arms no retry and names the provider and the
    remedy in the chat's last_error (#2648)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="Encountered invalidated oauth token for user, failing request",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"
    assert "action: sign in again" in updated.last_error
    assert f"provider: {updated.provider}" in updated.last_error


async def test_quota_retry_armed_below_cap(tmp_path: Path) -> None:
    """One short of the quota cap, a rate-limit error still arms the hourly retry."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = _MAX_QUOTA_RETRIES - 1
    chat.retry_kind = "quota"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    pcm.stop_chat_retry(chat.chat_id)


async def test_session_limit_gets_a_week_of_hourly_attempts(tmp_path: Path) -> None:
    """A limit that never clears is retried hourly for a week, then stops."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    # Each hourly fire fails again; the first failure arms the ladder.
    for _ in range(_MAX_QUOTA_RETRIES):
        updated = pcm.get_chat(chat.chat_id)
        assert updated is not None
        assert updated.retry_status == "pending"
        retry_stream = pcm.try_chat_retry_now(chat.chat_id)
        assert retry_stream is not None
        await asyncio.wait_for(_consume(retry_stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"


async def test_connection_retries_do_not_consume_quota_budget(tmp_path: Path) -> None:
    """Earlier connection retries leave the full quota budget for a later limit."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = 3
    chat.retry_kind = "connection"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "quota"
    assert updated.retry_attempts == 0
    pcm.stop_chat_retry(chat.chat_id)


@pytest.mark.parametrize("already_due", [False, True])
async def test_retry_loop_wakes_at_a_rearmed_retry_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_due: bool
) -> None:
    """A failed daily retry that re-arms a near time is retried then, not a day later."""
    from ciao.web import project_chats

    monkeypatch.setattr(project_chats, "_RETRY_STREAM_POLL_SECONDS", 0.01)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    calls: list[str] = []
    second_call = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            # The first daily retry fails and re-arms a near or already-due retry.
            owner = pcm._broker.get(chat_id)
            assert owner is not None
            pcm.set_chat_retry(
                chat_id,
                prompt,
                reason="connection",
                next_at="2000-01-01T00:00:00Z" if already_due else chat_service._iso_after(1),
                interval_seconds=24 * 60 * 60,
                _owner=owner,
            )
            return
        second_call.set()
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-ok",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    pcm.set_chat_retry(
        chat.chat_id,
        "run me",
        reason="quota",
        next_at="2000-01-01T00:00:00Z",
        interval_seconds=24 * 60 * 60,
    )
    await asyncio.wait_for(second_call.wait(), timeout=5.0)
    while (current := pcm.get_chat(chat.chat_id)) is not None and current.retry_status == "pending":
        await asyncio.sleep(0.01)

    assert calls == ["run me", "run me"]
    assert current is not None
    assert current.retry_status == ""


async def test_already_due_owner_rearm_is_not_deferred_as_a_busy_chat(tmp_path: Path, monkeypatch) -> None:
    from ciao.web import project_chats
    from ciao.web.chat_broker import ChatStream

    monkeypatch.setattr(project_chats, "_RETRY_STREAM_POLL_SECONDS", 0.01)
    manager = _make_manager(tmp_path)
    project = manager.create_project("retry", workspace="personal")
    chat = manager.create_chat(project.project_id, title="retry owner")
    owner = ChatStream(prompt_text="failed turn")
    owner.retry_generation = manager._retry_generations.get(chat.chat_id, 0)
    manager._broker.register(chat.chat_id, owner)
    fired = asyncio.Event()
    observed = []
    due = "2000-01-01T00:00:00Z"

    def fire(chat_id):
        observed.append(chat.retry_next_at)
        chat.retry_status = ""
        fired.set()
        return None

    monkeypatch.setattr(manager, "try_chat_retry_now", fire)
    manager.set_chat_retry(chat.chat_id, "retry", reason="connection", next_at=due, _owner=owner)
    task = manager._retry_tasks[chat.chat_id]
    try:
        await asyncio.sleep(0)  # run the timer until its first suspension
        assert chat.retry_next_at == due and not fired.is_set()
        manager._broker.clear(chat.chat_id, owner)
        owner.finish()
        await asyncio.wait_for(fired.wait(), 5)
        assert observed == [due]
    finally:
        owner.finish()
        manager._broker.clear(chat.chat_id, owner)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _ok_result(chat) -> ResultEvent:
    return ResultEvent(
        type="result", result="network looks down", session_id="sess-net",
        is_error=False, effective_model=chat.model, usage={}, quota={}, cost_usd=0.0,
    )


async def test_network_retry_arms_with_ten_minute_backoff_and_in_flight_prompt(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        assert pcm.arm_network_retry(chat.chat_id) == "armed"
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "do the scheduled thing", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "network"
    assert updated.retry_prompt == "do the scheduled thing"
    assert updated.retry_attempts == 0
    assert updated.retry_interval_seconds == 600
    pcm.stop_chat_retry(chat.chat_id)


async def test_network_retry_turn_that_rearms_survives_its_own_turn_end(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
    seen: list[object] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        seen.append(_kwargs.get("unattended"))
        assert pcm.arm_network_retry(chat.chat_id) == "armed"
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert seen == [True]
    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == "pending"
    assert updated.retry_attempts == 1
    assert updated.retry_interval_seconds == 1200
    pcm.stop_chat_retry(chat.chat_id)


async def test_network_retry_resend_is_tagged_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    events = await asyncio.wait_for(_consume(stream), timeout=10.0)

    echo = next(e for e in events if e.get("type") == "user_echo")
    assert echo["origin"] == "retry"
    assert echo["unattended"] is True


async def test_network_retry_turn_that_succeeds_clears_the_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert pcm.get_chat(chat.chat_id).retry_status == ""


async def test_network_retry_backoff_doubles_and_caps_at_three(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    for attempts, interval in ((0, 600), (1, 1200), (2, 2400)):
        chat.retry_attempts = attempts
        assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
        assert chat.retry_interval_seconds == interval
    chat.retry_attempts = 3
    assert pcm.arm_network_retry(chat.chat_id, "job") == "exhausted"
    assert chat.retry_status == "stopped"
    assert chat.retry_kind == ""


async def test_try_now_past_the_network_cap_exhausts_instead_of_firing(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
    chat.retry_attempts = 3
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    assert pcm.try_chat_retry_now(chat.chat_id) is None
    assert calls == []
    assert chat.retry_status == "stopped"


async def test_arm_network_retry_without_prompt_or_turn_refuses(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    assert pcm.arm_network_retry(chat.chat_id) == "no_prompt"
    assert chat.retry_status == ""


async def test_network_retry_holds_the_result_push(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    pcm._push_delay_seconds = 0
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    pushed: list[tuple] = []
    pcm.notify_result_cb = lambda cid, title, snippet: pushed.append((cid, title, snippet))

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        pcm.arm_network_retry(chat.chat_id)
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "job", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=10.0)
    await asyncio.sleep(0.05)

    assert pushed == []
    pcm.stop_chat_retry(chat.chat_id)


async def test_control_plane_network_kind_maps_outcomes(tmp_path: Path) -> None:
    from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    cp = CiaoControlPlane(pcm._config, project_chat_manager=pcm, schedule_manager=None)
    principal = McpPrincipal(
        token_id="t", chat_id=chat.chat_id, project_id="p",
        workspace="personal", provider="opencode",
    )

    out = cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="network")
    assert out["data"]["retry"]["interval_seconds"] == 600

    with pytest.raises(ControlPlaneError) as bad_kind:
        cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="bogus")
    assert bad_kind.value.code == "invalid_kind"
    with pytest.raises(ControlPlaneError) as bad_action:
        cp.chat_retry_update(principal, chat.chat_id, "stop", kind="network")
    assert bad_action.value.code == "invalid_action"

    chat.retry_attempts = 3
    with pytest.raises(ControlPlaneError) as capped:
        cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="network")
    assert capped.value.code == "retry_exhausted"
    pcm.stop_chat_retry(chat.chat_id)


async def test_control_plane_set_retry_from_unattended_turn_stays_unattended(tmp_path: Path) -> None:
    from ciao.control_plane import CiaoControlPlane, McpPrincipal

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="sched")
    other = pcm.create_chat(project.project_id, title="other")
    chat.user_turn_count = 1
    chat.user_turn_unattended["0"] = True
    cp = CiaoControlPlane(pcm._config, project_chat_manager=pcm, schedule_manager=None)
    principal = McpPrincipal(
        token_id="t", chat_id=chat.chat_id, project_id="p",
        workspace="personal", provider="claude",
    )

    cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job")
    assert pcm.get_chat(chat.chat_id).retry_unattended is True

    # Arming another chat's retry does not lend it this turn's unattended flag.
    cp.chat_retry_update(principal, other.chat_id, "set", prompt="job")
    assert pcm.get_chat(other.chat_id).retry_unattended is False

    chat.user_turn_unattended.clear()
    cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job")
    assert pcm.get_chat(chat.chat_id).retry_unattended is False
    pcm.stop_chat_retry(chat.chat_id)
    pcm.stop_chat_retry(other.chat_id)


async def test_wake_during_pending_quota_retry_joins_the_retry(tmp_path: Path) -> None:
    """A background-run wake into a chat waiting out a usage limit must not start a
    turn the provider will refuse, and must not overwrite the pending retry: the
    retried turn has to carry the original prompt and every wake notice."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="The usage limit has been reached",
            session_id="sess-retry",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    await asyncio.wait_for(_consume(pcm.start_stream(chat.chat_id, "do the thing")), timeout=10.0)
    armed = pcm.get_chat(chat.chat_id)
    assert armed is not None and armed.retry_status == "pending"
    next_at = armed.retry_next_at

    for notice in ("[Ciaobot] 1 background run finished.\n\n— smoke (FAILED)",
                   "[Ciaobot] 1 background run finished.\n\n— gates (FAILED)"):
        delivery = pcm._deliver_wake(armed, notice, count=1, origin="cli_task_wake")
        stream = pcm._broker.get(chat.chat_id)
        if stream is not None:
            await asyncio.wait_for(_consume(stream), timeout=10.0)
        assert delivery == "deferred"

    assert calls == ["do the thing"]
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_next_at == next_at
    assert updated.retry_prompt.startswith("do the thing")
    assert "smoke (FAILED)" in updated.retry_prompt
    assert "gates (FAILED)" in updated.retry_prompt
    pcm.stop_chat_retry(chat.chat_id)


async def test_wake_without_pending_retry_still_starts_a_turn(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="ok",
            session_id="sess-wake",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    delivery = pcm._deliver_wake(chat, "WAKE", count=1, origin="cli_task_wake")
    stream = pcm._broker.get(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert delivery == "started"
    assert calls == ["WAKE"]
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"
    pcm.stop_chat_retry(chat.chat_id)


async def test_retry_that_fails_unclassified_stops_instead_of_staying_pending(
    tmp_path: Path,
) -> None:
    """A retried turn that errors with no retry classifier match must not stay pending (#2647)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_kind = "quota"
    pcm.set_chat_retry(chat.chat_id, "do the thing", reason="usage limit", interval_seconds=7200)

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="Encountered invalidated oauth token for user, failing request",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert chat.retry_status == "stopped"
    assert chat.retry_next_at == ""


async def test_retry_that_fails_with_a_quota_error_still_rearms(tmp_path: Path) -> None:
    """A retried turn that errors with a retryable quota error still re-arms (#2647)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_kind = "quota"
    pcm.set_chat_retry(chat.chat_id, "do the thing", reason="usage limit", interval_seconds=7200)

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=10.0)

    assert chat.retry_status == "pending"
    pcm.stop_chat_retry(chat.chat_id)


async def test_try_now_past_the_quota_cap_stops_instead_of_firing(tmp_path: Path) -> None:
    """try_chat_retry_now refuses a quota retry at or past its cap and stops it (#2647)."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="quota")
    pcm.set_chat_retry(chat.chat_id, "job", reason="usage limit")
    chat.retry_kind = "quota"
    chat.retry_attempts = _MAX_QUOTA_RETRIES
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    assert pcm.try_chat_retry_now(chat.chat_id) is None
    assert calls == []
    assert chat.retry_status == "stopped"


def test_legacy_quota_terminal_retry_kind_loads_as_quota(tmp_path: Path) -> None:
    """A chat persisted with the retired "quota_terminal" kind resumes as "quota"."""
    import json

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="legacy")
    pcm.set_chat_retry(chat.chat_id, "job", reason="weekly limit")
    chat.retry_kind = "quota_terminal"
    chat.retry_interval_seconds = 24 * 60 * 60
    from ciao.web import chat_service
    chat.retry_next_at = chat_service._iso_after(20 * 60 * 60)
    pcm._save()
    path = tmp_path / ".runtime" / "web_projects.json"
    assert "quota_terminal" in path.read_text(encoding="utf-8")

    reloaded = _make_manager(tmp_path)
    restored = reloaded.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.retry_status == "pending"
    assert restored.retry_kind == "quota"
    # Resumes hourly: no "every hour" card that still waits a day.
    assert restored.retry_interval_seconds == 3600
    due = chat_service._parse_iso(restored.retry_next_at)
    assert due is not None and due <= chat_service._parse_iso(chat_service._iso_after(3600 + 5))
    json.loads(path.read_text(encoding="utf-8"))
