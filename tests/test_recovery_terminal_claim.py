import asyncio
import copy
import errno
import json
from pathlib import Path

import pytest

from ciao.models import ResultEvent
from ciao.web import project_chats
from tests.test_chat_retry import _make_manager


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("recover_via", ["status", "stop", "admission", "replacement"])
async def test_terminal_enospc_releases_only_its_finished_claim(tmp_path, monkeypatch, failed, recover_via):
    manager = _make_manager(tmp_path)
    project = manager.create_project("recovery", workspace="personal")
    chat = manager.create_chat(project.project_id, title="recovery")
    chat.session_id, chat.last_error, chat.last_response_status = "session", "failure", "error"
    manager._save()
    assert manager.schedule_continue_recovery(chat.chat_id, "session", "failure", 0, manual=failed)
    manager._cancel_retry_task(chat.chat_id)
    recovery_id = chat.retry_recovery["id"]
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(chat_id, prompt, images=None, **kwargs):
        calls.append(prompt)
        entered.set()
        await release.wait()
        yield ResultEvent(type="result", result="ordinary failure" if failed and len(calls) == 1 else "done",
                          session_id="session", is_error=failed and len(calls) == 1,
                          effective_model=chat.model, usage={}, quota={}, cost_usd=0.0)

    monkeypatch.setattr(manager, "stream_chat", provider)
    stream = manager.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    task = manager._streaming.drive_task(chat.chat_id, stream)
    assert task is not None
    await asyncio.wait_for(entered.wait(), 5)
    marker = copy.deepcopy(chat.retry_recovery)
    observer = _make_manager(tmp_path)
    # Neither observing nor stopping an actual running attempt may reclaim it.
    assert observer.recovery_status(chat.chat_id)["recovery_state"] == "in_flight"
    assert not observer.stop_recovery(chat.chat_id, recovery_id)
    assert marker["claim"] not in project_chats._ABORTED_RECOVERY_CLAIMS
    write = Path.write_text
    armed = [True]
    failures = []

    def disk_full(path, data, *args, **kwargs):
        if path.parent == manager._path.parent and path.name.startswith(f".{manager._path.name}."):
            row = json.loads(data)["chats"].get(chat.chat_id, {})
            if armed[0] and row.get("retry_recovery") == "":
                failures.append(row["retry_status"])
                raise OSError(errno.ENOSPC, "terminal registry full")
        return write(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", disk_full)
    release.set()
    try:
        with pytest.raises(OSError) as failure:
            await asyncio.wait_for(asyncio.shield(task), 5)
        assert failure.value.errno == errno.ENOSPC
        assert failures == ["stopped" if failed else ""]
        assert manager._broker.get(chat.chat_id) is None
        saved = json.loads(manager._path.read_bytes())["chats"][chat.chat_id]
        assert saved["retry_recovery"] == marker
        assert marker["claim"] in project_chats._ABORTED_RECOVERY_CLAIMS
        armed[0] = False

        if recover_via == "stop":
            assert observer.stop_recovery(chat.chat_id, recovery_id)
        elif recover_via == "admission":
            fresh = manager.start_stream(chat.chat_id, "new admission")
            await asyncio.wait_for(manager._streaming.drive_task(chat.chat_id, fresh), 5)
            assert calls == ["continue", "new admission"]
        else:
            assert observer.recovery_status(chat.chat_id)["status"] == "stopped"

        assert observer.recovery_status(chat.chat_id)["recovery_state"] != "in_flight"
        assert observer.try_chat_retry_now(chat.chat_id) is None

        if recover_via == "replacement":
            # A late terminal clear from the stale owner must CAS-refuse the new claim.
            observer = _make_manager(tmp_path)
            replacement = observer.get_chat(chat.chat_id)
            replacement.last_error, replacement.last_response_status = "new failure", "error"
            observer._save()
            assert observer.schedule_continue_recovery(
                chat.chat_id, "session", "new failure", replacement.user_turn_count,
                replacement.failure_id,
            )
            monkeypatch.setattr(observer, "start_stream", lambda *args, **kwargs: object())
            assert observer.try_chat_retry_now(chat.chat_id) is not None
            newer = copy.deepcopy(replacement.retry_recovery)
            manager._clear_chat_retry(chat, status="stopped")
            assert observer.recovery_status(chat.chat_id)["recovery_state"] == "in_flight"
            assert not observer.stop_recovery(chat.chat_id, newer["id"])
            assert json.loads(manager._path.read_bytes())["chats"][chat.chat_id]["retry_recovery"] == newer
            assert newer["claim"] not in project_chats._ABORTED_RECOVERY_CLAIMS
            observer._release_recovery_claim(chat.chat_id)
            observer._cancel_retry_task(chat.chat_id)
            assert observer.stop_recovery(chat.chat_id, newer["id"])
    finally:
        armed[0] = False
        release.set()
        manager._cancel_retry_task(chat.chat_id)
        observer._cancel_retry_task(chat.chat_id)
        project_chats._ABORTED_RECOVERY_CLAIMS.discard(marker["claim"])
