import asyncio
import json
import multiprocessing
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web import project_chats, routes_api
from tests.test_chat_retry import _make_manager


def _probe(root, chat_id, token, now, connection):
    """A fresh interpreter must not depend on the owner's in-memory state."""
    try:
        project_chats.time.time = lambda: now
        project_chats._RECOVERY_REQUEST_LIMIT = 1
        manager = _make_manager(Path(root))
        try:
            record, _ = manager.recovery_request_record(chat_id, token)
            connection.send(record["status"])
        except project_chats.RecoveryOutcomeUnknown:
            connection.send("RecoveryOutcomeUnknown")
        manager.recovery_request_record(chat_id, None)
        connection.send("capacity available")
    except Exception as exc:
        connection.send(type(exc).__name__)
    finally:
        connection.close()


def _process_probe(tmp_path, chat_id, token, now):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_probe, args=(str(tmp_path), chat_id, token, now, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20)
        status = parent.recv()
        assert parent.poll(20)
        capacity = parent.recv()
        return status, capacity
    finally:
        process.join(20)
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()
        assert process.exitcode == 0


def _request(manager, chat_id, token):
    body = json.dumps({"action": "continue_recovery", "failure_id": "failure", "request_id": token}).encode()

    async def receive():
        return {"type": "http.request", "body": body}

    return Request({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "path_params": {"chat_id": chat_id},
        "app": SimpleNamespace(state=SimpleNamespace(project_chat_manager=manager)),
    }, receive)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_cancel_during_admission_retains_owner_until_thread_and_handler_finish(tmp_path, monkeypatch, stage):
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    entered, release = Event(), Event()
    handling, finish = asyncio.Event(), asyncio.Event()
    original = manager.recovery_request_record
    effects = []

    def admission(*args, **kwargs):
        if stage == "before" and kwargs.get("lifetime") is not None:
            entered.set()
            assert release.wait(60)
        result = original(*args, **kwargs)
        if stage == "after" and result[1]:
            entered.set()
            assert release.wait(60)
        return result

    async def action(*args):
        handling.set()
        await finish.wait()
        effects.append("effect")
        return JSONResponse({"ok": True})

    monkeypatch.setattr(manager, "recovery_request_record", admission)
    monkeypatch.setattr(routes_api, "_chat_retry_action", action)
    task = asyncio.create_task(routes_api.chat_retry(_request(manager, chat.chat_id, token)))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation abandoned the admission thread"
        now = project_chats.time.time() + project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
        if stage == "after":
            assert await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now) == ("in_progress", "OSError")
        release.set()
        await asyncio.wait_for(handling.wait(), 10)
        task.cancel()  # repeated cancellation must not drop the handler's lock either
        await asyncio.sleep(0)
        assert not task.done()
        assert await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now) == ("in_progress", "OSError")
    finally:
        release.set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert effects == ["effect"]


@pytest.mark.asyncio
async def test_failed_terminal_write_is_finished_to_another_process_while_owner_lives(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    original = manager.recovery_request_record

    def failing(*args, **kwargs):
        if kwargs.get("result") is not None or kwargs.get("outcome_unknown"):
            raise OSError("disk unavailable after effect")
        return original(*args, **kwargs)

    async def action(*args):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(manager, "recovery_request_record", failing)
    monkeypatch.setattr(routes_api, "_chat_retry_action", action)
    response = await routes_api.chat_retry(_request(manager, chat.chat_id, token))
    assert response.status_code == 503
    now = project_chats.time.time()
    assert (await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now))[0] == "unknown"
    now += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    status, capacity = await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now)
    assert status in {"unknown", "RecoveryOutcomeUnknown"}
    assert capacity == "capacity available"


@pytest.mark.asyncio
async def test_shutdown_cancels_worker_without_abandoning_admission_thread(tmp_path, monkeypatch):
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    entered, release = Event(), Event()
    original = manager.recovery_request_record

    def admission(*args, **kwargs):
        result = original(*args, **kwargs)
        if result[1]:
            entered.set()
            assert release.wait(60)
        return result

    monkeypatch.setattr(manager, "recovery_request_record", admission)
    request = _request(manager, chat.chat_id, token)
    body = await request.json()
    task = asyncio.create_task(routes_api._chat_recovery_request(
        request, body, {"action": "continue_recovery", "failure_id": "failure"},
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        now = project_chats.time.time()
        assert (await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now))[0] == "in_progress"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await asyncio.to_thread(_process_probe, tmp_path, chat.chat_id, token, now))[0] == "unknown"


def _hold(root, chat_id, token, connection):
    manager = _make_manager(Path(root))
    with ExitStack() as lifetime:
        _, admitted = manager.recovery_request_record(
            chat_id, token, intent={"action": "continue_recovery", "failure_id": "failure"}, lifetime=lifetime,
        )
        connection.send(admitted)
        connection.recv()


def test_process_death_reclaims_expired_receipt_but_live_process_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(project_chats, "_RECOVERY_REQUEST_LIMIT", 1)
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold, args=(str(tmp_path), chat.chat_id, token, child))
    process.start()
    child.close()
    try:
        assert parent.poll(20) and parent.recv()
        now = project_chats.time.time() + project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
        monkeypatch.setattr(project_chats.time, "time", lambda: now)
        assert manager.recovery_request_record(chat.chat_id, token)[0]["status"] == "in_progress"
        with pytest.raises(OSError, match="journal is full"):
            manager.recovery_request_record(chat.chat_id, None)
        process.kill()
        process.join(20)
        assert not process.is_alive()
        replacement = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
        assert replacement != token
        with pytest.raises(project_chats.RecoveryOutcomeUnknown):
            manager.recovery_request_record(chat.chat_id, token)
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        parent.close()


def test_lock_files_are_bounded_and_reused_without_unlink(tmp_path, monkeypatch):
    monkeypatch.setattr(project_chats, "_RECOVERY_REQUEST_LIMIT", 2)
    clock = [project_chats.time.time()]
    monkeypatch.setattr(project_chats.time, "time", lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    intent = {"action": "continue_recovery", "failure_id": "failure"}
    identities = {}
    for _ in range(12):
        with ExitStack() as lifetime:
            for _ in range(2):
                token = manager.recovery_request_record(chat.chat_id, None)[0]["request_id"]
                assert manager.recovery_request_record(chat.chat_id, token, intent=intent, lifetime=lifetime)[1]
        # Simulate terminal write failure: closed ownership, unfinished receipts.
        clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
        files = list(manager._path.parent.glob("*.recovery_request.*.lock"))
        assert len(files) == 2
        for file in files:
            identity = file.stat().st_ino
            assert identities.setdefault(file, identity) == identity
