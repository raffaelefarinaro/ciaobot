from pathlib import Path
from contextlib import ExitStack

import pytest

from ciao.web import project_chats
from tests.test_chat_retry import _make_manager


@pytest.fixture
def lifetime():
    with ExitStack() as stack:
        yield stack


def test_receipts_reclaim_and_clock_rollback(tmp_path: Path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    for _ in range(520):
        record, _ = manager.recovery_request_record(chat.chat_id, None)
        token = record['request_id']
        with ExitStack() as lifetime:
            _, admitted = manager.recovery_request_record(chat.chat_id, token, intent=intent, lifetime=lifetime)
            assert admitted
            manager.recovery_request_record(chat.chat_id, token, intent=intent, result={'http_status': 200})
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    manager.recovery_request_record(chat.chat_id, None)
    clock[0] = 1000.0
    restarted = _make_manager(tmp_path)
    with pytest.raises(project_chats.RecoveryOutcomeUnknown):
        restarted.recovery_request_record(chat.chat_id, token, intent=intent)


def test_live_receipts_survive_expiry_and_capacity_contention(tmp_path: Path, monkeypatch, lifetime):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    monkeypatch.setattr(project_chats, '_RECOVERY_REQUEST_LIMIT', 2)
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    active = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
    manager.recovery_request_record(chat.chat_id, active, intent=intent, lifetime=lifetime)
    expired = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
    other = _make_manager(tmp_path)
    with pytest.raises(OSError):
        other.recovery_request_record(chat.chat_id, None)
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    new = other.recovery_request_record(chat.chat_id, None)[0]['request_id']
    with pytest.raises(project_chats.RecoveryOutcomeUnknown):
        manager.recovery_request_record(chat.chat_id, expired, intent=intent)
    assert other.recovery_request_record(chat.chat_id, active, intent=intent)[0]['status'] == 'in_progress'
    assert manager.recovery_request_record(chat.chat_id, active, intent=intent, result={'http_status': 200})[0]['status'] == 'completed'
    assert new != expired


def test_expired_submission_advances_durable_clock_before_rejecting(tmp_path: Path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    token = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    with pytest.raises(project_chats.RecoveryOutcomeUnknown):
        manager.recovery_request_record(chat.chat_id, token, intent=intent)
    clock[0] = 1000.0
    with pytest.raises(project_chats.RecoveryOutcomeUnknown):
        _make_manager(tmp_path).recovery_request_record(chat.chat_id, token, intent=intent)


def test_unissued_token_is_rejected_by_api(tmp_path: Path):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ciao.web.routes_api import chat_retry

    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    app = Starlette(routes=[Route('/chats/{chat_id}/retry', chat_retry, methods=['POST'])])
    app.state.project_chat_manager = manager
    client = TestClient(app)
    url = f'/chats/{chat.chat_id}/retry'
    body = {'action': 'continue_recovery', 'failure_id': 'f', 'request_id': 'forged'}
    assert client.post(url, json=body).status_code == 503
    assert client.post(url, json=body).json()['status'] == 'unknown'
    issued = client.post(url, json={'action': 'issue_recovery_request'})
    assert issued.status_code == 200 and issued.json()['request_id'] != 'forged'


def test_abandoned_after_execution_stays_unknown_after_reclamation(tmp_path: Path, monkeypatch, lifetime):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    token = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
    manager.recovery_request_record(chat.chat_id, token, intent=intent, lifetime=lifetime)
    # The operation ended without a durable terminal receipt.
    lifetime.close()
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    manager.recovery_request_record(chat.chat_id, None)
    with pytest.raises(project_chats.RecoveryOutcomeUnknown):
        _make_manager(tmp_path).recovery_request_record(chat.chat_id, token, intent=intent)


def test_expired_receipt_cleanup_bounds_owner_checks(tmp_path: Path, monkeypatch, lifetime):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    for _ in range(30):
        token = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
        manager.recovery_request_record(chat.chat_id, token, intent=intent, lifetime=lifetime)
    calls = []
    original = manager._recovery_request_lock
    def probe(slot):
        calls.append(slot)
        return original(slot)
    monkeypatch.setattr(manager, '_recovery_request_lock', probe)
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    manager.recovery_request_record(chat.chat_id, None)
    assert len(calls) == project_chats._RECOVERY_OWNER_CHECK_LIMIT


def test_owner_checks_are_capped_and_rotate_between_distinct_owners(tmp_path: Path, monkeypatch, lifetime):
    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    intent = {'action': 'continue_recovery', 'failure_id': 'f'}
    for _ in range(20):
        token = manager.recovery_request_record(chat.chat_id, None)[0]['request_id']
        manager.recovery_request_record(chat.chat_id, token, intent=intent, lifetime=lifetime)
    checks = []
    original = manager._recovery_request_lock
    def probe(slot):
        checks.append(slot)
        return original(slot)
    monkeypatch.setattr(manager, '_recovery_request_lock', probe)
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    for _ in range(3):
        previous = len(checks)
        manager.recovery_request_record(chat.chat_id, None)
        assert len(checks) - previous <= project_chats._RECOVERY_OWNER_CHECK_LIMIT
    assert len(set(checks)) == 20


def test_finished_unknown_releases_capacity_after_expiry(tmp_path: Path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ciao.web import routes_api

    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    monkeypatch.setattr(project_chats, '_RECOVERY_REQUEST_LIMIT', 2)
    async def fail(*args):
        return JSONResponse({'error': 'uncertain'}, status_code=503)
    monkeypatch.setattr(routes_api, '_chat_retry_action', fail)
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    app = Starlette(routes=[Route('/chats/{chat_id}/retry', routes_api.chat_retry, methods=['POST'])])
    app.state.project_chat_manager = manager
    client = TestClient(app)
    url = f'/chats/{chat.chat_id}/retry'
    bodies = []
    for _ in range(2):
        token = client.post(url, json={'action': 'issue_recovery_request'}).json()['request_id']
        body = {'action': 'continue_recovery', 'failure_id': 'f', 'request_id': token}
        bodies.append(body)
        assert client.post(url, json=body).json()['status'] == 'unknown'
        assert client.post(url, json={'action': 'recovery_request_status', 'request_id': token}).json()['status'] == 'unknown'
    assert client.post(url, json={'action': 'issue_recovery_request'}).status_code == 503
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    assert client.post(url, json={'action': 'issue_recovery_request'}).status_code == 200
    response = client.post(url, json=bodies[0])
    assert response.status_code == 503 and response.json()['status'] == 'unknown'


@pytest.mark.parametrize('failure', ['handler', 'result_write', 'transient_disk'])
def test_finished_exception_reconciles_and_releases_capacity(tmp_path: Path, monkeypatch, failure):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ciao.web import routes_api

    clock = [1000.0]
    monkeypatch.setattr(project_chats.time, 'time', lambda: clock[0])
    monkeypatch.setattr(project_chats, '_RECOVERY_REQUEST_LIMIT', 1)
    calls = []
    async def action(*args):
        calls.append('effect')
        if failure == 'handler':
            raise RuntimeError('handler failed after effect')
        return JSONResponse({'ok': True})
    monkeypatch.setattr(routes_api, '_chat_retry_action', action)
    manager = _make_manager(tmp_path)
    project = manager.create_project('recovery', workspace='personal')
    chat = manager.create_chat(project.project_id, title='recovery')
    original = manager.recovery_request_record
    failing = [True]
    def unstable(*args, **kwargs):
        if failure == 'result_write' and kwargs.get('result') is not None:
            raise OSError('result write failed')
        if failure == 'transient_disk' and failing[0] and (kwargs.get('result') is not None or kwargs.get('outcome_unknown')):
            raise OSError('disk unavailable')
        return original(*args, **kwargs)
    monkeypatch.setattr(manager, 'recovery_request_record', unstable)
    app = Starlette(routes=[Route('/chats/{chat_id}/retry', routes_api.chat_retry, methods=['POST'])])
    app.state.project_chat_manager = manager
    client = TestClient(app)
    url = f'/chats/{chat.chat_id}/retry'
    token = client.post(url, json={'action': 'issue_recovery_request'}).json()['request_id']
    body = {'action': 'continue_recovery', 'failure_id': 'f', 'request_id': token}
    response = client.post(url, json=body)
    assert response.status_code == 503 and response.json()['status'] == 'unknown'
    failing[0] = False
    status = client.post(url, json={'action': 'recovery_request_status', 'request_id': token})
    assert status.status_code == 503 and status.json()['status'] == 'unknown'
    assert client.post(url, json=body).json()['status'] == 'unknown'
    assert calls == ['effect']
    clock[0] += project_chats._RECOVERY_REQUEST_TTL_SECONDS + 1
    assert client.post(url, json={'action': 'issue_recovery_request'}).status_code == 200
    delayed = client.post(url, json=body)
    assert delayed.status_code == 503 and delayed.json()['status'] == 'unknown'
    assert calls == ['effect']
