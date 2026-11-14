"""Real registry/lock/drive admission contracts; only storage and provider seams fail."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import errno
import json
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import project_chats
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import ProjectChatManager, SEND_ID_HISTORY_TURNS
from tests.conftest import attach_stub_mcp


@pytest.fixture
def admission_case(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    registry = runtime / "web_projects.json"
    config = CiaoConfig(pwa_auth_token="test-token", workspace_root=tmp_path,
                        state_path=runtime / "state.json", media_root=runtime / "media")
    manager = attach_stub_mcp(ProjectChatManager(
        config, state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"), path=registry,
    ))
    project = manager.create_project("disk-full-repro", workspace="personal")
    chat = manager.create_chat(project.project_id, title="Disk full repro", provider="claude")
    return manager, chat, registry


@pytest.fixture
def storage_fault(monkeypatch):
    """Factory also records successful real writes/replaces, scoped to one registry."""
    faults = []

    def install(manager, registry, kind="write", error=errno.ENOSPC):
        fault = SimpleNamespace(armed=True, writes=[], replaces=[], temps=[])
        real_write, real_replace, real_unlink = Path.write_text, Path.replace, Path.unlink
        real_audit, real_lock = manager._append_registry_audit, project_chats._state_file_lock

        def is_temp(path):
            return path.parent == registry.parent and path.name.startswith(f".{registry.name}.") and path.suffix == ".tmp"

        def write(path, data, *args, **kwargs):
            if is_temp(path):
                fault.writes.append(data)
                fault.temps.append(path)
                if fault.armed and kind in {"write", "write_unlock", "write_unlink"}:
                    raise OSError(error, "synthetic registry write failure")
            return real_write(path, data, *args, **kwargs)

        def replace(path, target):
            if is_temp(path) and Path(target) == registry:
                if fault.armed and kind == "replace":
                    raise OSError(errno.EACCES, "synthetic replace failure")
                result = real_replace(path, target)
                fault.replaces.append(registry.read_bytes())
                return result
            return real_replace(path, target)

        def unlink(path, *args, **kwargs):
            result = real_unlink(path, *args, **kwargs)
            if is_temp(path) and fault.armed and kind in {"unlink", "write_unlink"}:
                raise OSError(errno.EACCES, "synthetic cleanup failure")
            return result

        def audit(**kwargs):
            if fault.armed and kind == "audit":
                raise RuntimeError("synthetic escaping audit failure")
            return real_audit(**kwargs)

        @contextmanager
        def lock(path):
            try:
                with real_lock(path):
                    yield
            finally:
                if path == registry and fault.armed and fault.writes and kind in {"unlock", "write_unlock"}:
                    raise OSError(errno.EACCES, "synthetic unlock failure")

        monkeypatch.setattr(Path, "write_text", write)
        monkeypatch.setattr(Path, "replace", replace)
        monkeypatch.setattr(Path, "unlink", unlink)
        monkeypatch.setattr(manager, "_append_registry_audit", audit)
        monkeypatch.setattr(project_chats, "_state_file_lock", lock)
        faults.append(fault)
        return fault

    yield install
    for fault in faults:
        fault.armed = False


@pytest.fixture
async def driver_probe(monkeypatch):
    probes = []

    def install(manager, chat):
        probe = SimpleNamespace(calls=[], entered=asyncio.Event(), release=asyncio.Event())

        async def stream_chat(chat_id, prompt, images=None, **kwargs):
            probe.calls.append((chat_id, prompt))
            probe.entered.set()
            await probe.release.wait()
            yield ResultEvent(type="result", result="ok", session_id="", is_error=False,
                              effective_model=chat.model, usage={}, quota={})

        monkeypatch.setattr(manager, "stream_chat", stream_chat)
        probes.append((manager, probe))
        return probe

    yield install
    for manager, probe in probes:
        probe.release.set()
        tasks = [task for _, task in list(manager._streaming._drive_tasks.values())]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), 5)
        # Memory lifecycle settlement is a real detached effect of drive teardown.
        for _ in range(10):
            tasks = [task for task in manager._detached_tasks if not task.done()]
            if not tasks:
                break
            await asyncio.wait_for(asyncio.gather(*tasks), 5)


async def consume(stream):
    async def collect():
        return [event async for event in stream.subscribe()]
    return await asyncio.wait_for(collect(), 5)


def seed_state(manager, chat):
    chat.pending_question = json.dumps({"request_id": "dead-form"})
    chat.pending_permission = json.dumps({"request_id": "dead-permission"})
    chat.last_error = "previous failure"
    chat.pending_queue = [{"id": "parked", "text": "later", "images": []}]
    chat.retry_status, chat.retry_prompt = "pending", "old prompt"
    chat.retry_image_refs = ["old-image"]
    chat.retry_next_at, chat.retry_last_error = "2099-01-01T00:00:00+00:00", "old retry"
    chat.retry_attempts, chat.retry_kind = 3, "network"
    chat.retry_interval_seconds, chat.retry_unattended = 123, True
    chat.user_turn_count = 2
    chat.user_turn_images = {"0": ["old"]}
    chat.user_turn_timings = {"0": {"sent_at": "old"}}
    chat.user_turn_unattended = {"0": True}
    chat.user_turn_origin = {"0": "retry"}
    chat.user_turn_send_ids = {"0": ["old-a", "old-b"], "1": "old-c"}
    chat.helper = {}
    chat.postprocess = {"updated_at": "old", "steps": {"other": {"nested": [1, 2]}}}
    manager._save()


def state_checkpoint(chat):
    # Independent expected state: not derived from the implementation's field list.
    values = {field.name: getattr(chat, field.name) for field in dataclasses.fields(chat)}
    refs = {name: value for name, value in values.items() if isinstance(value, (dict, list))}
    return copy.deepcopy(values), refs


def assert_restored(chat, checkpoint):
    expected, refs = checkpoint
    assert {field.name: getattr(chat, field.name) for field in dataclasses.fields(chat)} == expected
    for name, ref in refs.items():
        assert getattr(chat, name) is ref, name


@pytest.fixture
async def failed_start(admission_case, storage_fault, monkeypatch):
    manager, chat, registry = admission_case
    before, turns = registry.read_bytes(), chat.user_turn_count
    fault = storage_fault(manager, registry)
    drive = Mock(wraps=manager._streaming.start_drive)
    monkeypatch.setattr(manager._streaming, "start_drive", drive)
    with pytest.raises(OSError) as exc:
        manager.start_stream(chat.chat_id, "Synthetic prompt", send_id="synthetic-send")
    assert exc.value.errno == errno.ENOSPC
    assert len(fault.writes) == 1
    assert registry.read_bytes() == before
    assert not list(registry.parent.glob(f".{registry.name}.*.tmp"))
    drive.assert_not_called()
    assert chat.chat_id not in manager._providers
    assert manager._streaming.drive_task(chat.chat_id) is None
    fault.armed = False
    return manager, chat, turns


async def test_failed_start_must_release_admission(failed_start):
    manager, chat, turns = failed_start
    assert manager.get_active_stream(chat.chat_id) is None
    assert chat.chat_id not in manager.active_chat_ids()
    assert chat.user_turn_count == turns
    assert manager.find_turn_by_send_id(chat.chat_id, "synthetic-send") is None


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EACCES])
async def test_precommit_enospc_restores_admission_and_starts_no_driver(
    admission_case, storage_fault, driver_probe, monkeypatch, error,
):
    manager, chat, registry = admission_case
    seed_state(manager, chat)
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    baseline = copy.deepcopy(manager._last_local_payload)
    probe = driver_probe(manager, chat)
    drive = Mock(wraps=manager._streaming.start_drive)
    monkeypatch.setattr(manager._streaming, "start_drive", drive)
    publish = Mock(wraps=manager._events.publish)
    monkeypatch.setattr(manager._events, "publish", publish)
    register = Mock(wraps=manager._broker.register)
    monkeypatch.setattr(manager._broker, "register", register)
    subscription = manager._events.attach()
    local_streams = []
    def new_stream(*args, **kwargs):
        stream = ChatStream(*args, **kwargs)
        local_streams.append(stream)
        return stream
    monkeypatch.setattr(project_chats, "ChatStream", new_stream)
    storage_fault(manager, registry, error=error)
    with pytest.raises(OSError) as exc:
        manager.start_stream(chat.chat_id, "rejected", send_id="rejected", unattended=True, origin="retry")
    assert exc.value.errno == error
    assert_restored(chat, checkpoint)
    assert registry.read_bytes() == before
    assert manager._last_local_payload == baseline
    assert not manager._turn_perf_started and not probe.calls
    drive.assert_not_called()
    register.assert_not_called()
    assert chat.chat_id not in manager._providers
    publish.assert_not_called()
    await asyncio.sleep(0)
    observed = []
    while not subscription._queue.empty():
        observed.append(subscription._queue.get_nowait())
    subscription.close()
    assert observed == []
    assert manager._broker.get(chat.chat_id) is None
    assert manager.get_active_stream(chat.chat_id) is None  # idle reconnect cannot attach
    assert local_streams and all(stream.done for stream in local_streams)
    assert await consume(local_streams[0]) == []


async def test_replace_failure_restores_admission_and_original_registry(admission_case, storage_fault):
    manager, chat, registry = admission_case
    seed_state(manager, chat)
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    baseline = copy.deepcopy(manager._last_local_payload)
    fault = storage_fault(manager, registry, "replace")
    with pytest.raises(OSError, match="replace failure"):
        manager.start_stream(chat.chat_id, "rejected")
    assert fault.writes and not fault.replaces
    assert not any(path.exists() for path in fault.temps)
    assert registry.read_bytes() == before
    assert manager._last_local_payload == baseline
    assert_restored(chat, checkpoint)


async def test_failed_admission_restores_pruned_send_id_history(admission_case, storage_fault, driver_probe):
    manager, chat, registry = admission_case
    chat.user_turn_count = SEND_ID_HISTORY_TURNS
    chat.user_turn_send_ids = {str(i): [f"a-{i}", f"b-{i}"] if i % 2 == 0 else f"s-{i}"
                               for i in range(SEND_ID_HISTORY_TURNS)}
    manager._save()
    original, expected = chat.user_turn_send_ids, copy.deepcopy(chat.user_turn_send_ids)
    fault = storage_fault(manager, registry)
    with pytest.raises(OSError):
        manager.start_stream(chat.chat_id, "retry", send_id="new")
    assert chat.user_turn_send_ids is original
    assert original == expected
    fault.armed = False
    probe = driver_probe(manager, chat)
    stream = manager.start_stream(chat.chat_id, "retry", send_id="new")
    assert chat.user_turn_send_ids is original
    assert "0" not in original and original[str(SEND_ID_HISTORY_TURNS)] == "new"
    assert all(original[key] == value for key, value in expected.items() if key != "0")
    probe.release.set()
    await consume(stream)


async def test_failed_admission_preserves_retry_fields_task_and_events(admission_case, storage_fault, driver_probe, monkeypatch):
    manager, chat, registry = admission_case
    seed_state(manager, chat)
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    generation = manager._retry_generations.get(chat.chat_id, 0)
    retry_fields = {name: value for name, value in checkpoint[0].items() if name.startswith("retry_")}
    async def retry():
        entered.set()
        await release.wait()
    task = asyncio.create_task(retry())
    manager._retry_tasks[chat.chat_id] = task
    await asyncio.wait_for(entered.wait(), 5)
    publish = Mock(wraps=manager._publish_retry)
    monkeypatch.setattr(manager, "_publish_retry", publish)
    fault = storage_fault(manager, registry)
    try:
        with pytest.raises(OSError):
            manager.start_stream(chat.chat_id, "new")
        assert_restored(chat, checkpoint)
        assert registry.read_bytes() == before
        assert manager._retry_tasks[chat.chat_id] is task
        assert not task.done() and task.cancelling() == 0
        assert manager._retry_generations.get(chat.chat_id, 0) == generation
        publish.assert_not_called()
        fault.armed = False
        probe = driver_probe(manager, chat)
        stream = manager.start_stream(chat.chat_id, "new")
        assert manager._retry_tasks[chat.chat_id] is task
        assert not task.done() and task.cancelling() == 0
        assert stream.retry_generation == -1
        assert manager._retry_generations.get(chat.chat_id, 0) == generation
        assert {name: getattr(chat, name) for name in retry_fields} == retry_fields
        assert chat.retry_image_refs is checkpoint[1]["retry_image_refs"]
        publish.assert_not_called()
        probe.release.set()
        await consume(stream)
        assert manager._retry_tasks[chat.chat_id] is task
        assert not task.done() and task.cancelling() == 0
        assert {name: getattr(chat, name) for name in retry_fields} == retry_fields
        publish.assert_not_called()
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await task


async def test_retry_clear_does_not_cancel_current_task(admission_case, driver_probe):
    manager, chat, _ = admission_case
    chat.retry_status = "pending"
    manager._save()
    probe = driver_probe(manager, chat)
    async def retry():
        manager._retry_tasks[chat.chat_id] = asyncio.current_task()
        generation = manager._retry_generations.get(chat.chat_id, 0)
        manager._clear_chat_retry(chat)
        stream = manager.start_stream(chat.chat_id, "manual retry")
        assert asyncio.current_task().cancelling() == 0
        assert manager._retry_generations[chat.chat_id] == generation + 1
        assert chat.retry_status == ""
        probe.release.set()
        await consume(stream)
    await asyncio.wait_for(asyncio.create_task(retry()), 5)


async def test_failed_admission_preserves_existing_background_drain(admission_case, storage_fault, driver_probe):
    manager, chat, registry = admission_case
    old = ChatStream(prompt_text="background")
    old.background = True
    manager._broker.register(chat.chat_id, old)
    entered = asyncio.Event()
    async def drain():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            old.finish()
            manager._broker.clear(chat.chat_id, old)
    task = asyncio.create_task(drain())
    manager._between_turn_drains[chat.chat_id] = task
    await asyncio.wait_for(entered.wait(), 5)
    fault = storage_fault(manager, registry)
    try:
        with pytest.raises(OSError):
            manager.start_stream(chat.chat_id, "new")
        assert manager._broker.get(chat.chat_id) is old and not old.done
        assert manager._between_turn_drains[chat.chat_id] is task
        assert not task.done() and task.cancelling() == 0
        fault.armed = False
        probe = driver_probe(manager, chat)
        replacement = manager.start_stream(chat.chat_id, "new")
        with suppress(asyncio.CancelledError):
            await task
        assert old.done and manager._broker.get(chat.chat_id) is replacement
        probe.release.set()
        await consume(replacement)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_compaction_failure_preserves_retry_and_user_turn_metadata(admission_case, storage_fault):
    manager, chat, registry = admission_case
    seed_state(manager, chat)
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    storage_fault(manager, registry)
    with pytest.raises(OSError):
        manager.start_stream(chat.chat_id, "/compact", compact=True)
    assert_restored(chat, checkpoint)
    assert registry.read_bytes() == before
    assert not manager._turn_perf_started
    assert manager._broker.get(chat.chat_id) is None


async def test_normal_audit_write_failure_does_not_reject_admission(admission_case, driver_probe, monkeypatch):
    manager, chat, registry = admission_case
    probe = driver_probe(manager, chat)
    real_fsync = project_chats.os.fsync
    def fail_fsync(fd):
        raise OSError(errno.ENOSPC, "synthetic audit fsync")
    monkeypatch.setattr(project_chats.os, "fsync", fail_fsync)
    stream = manager.start_stream(chat.chat_id, "accepted")
    monkeypatch.setattr(project_chats.os, "fsync", real_fsync)
    assert json.loads(registry.read_bytes())["chats"][chat.chat_id]["user_turn_count"] == 1
    await asyncio.wait_for(probe.entered.wait(), 5)
    assert len(probe.calls) == 1
    probe.release.set()
    await consume(stream)


async def test_postreplace_audit_exception_keeps_commit_and_starts_one_driver(admission_case, storage_fault, driver_probe):
    manager, chat, registry = admission_case
    probe = driver_probe(manager, chat)
    fault = storage_fault(manager, registry, "audit")
    stream = manager.start_stream(chat.chat_id, "accepted", send_id="committed")
    assert not probe.calls  # driver has ownership but has not run yet
    assert manager._streaming.drive_task(chat.chat_id, stream) is not None
    assert json.loads(registry.read_bytes())["chats"][chat.chat_id]["user_turn_count"] == 1
    assert manager._last_local_payload == manager._state_payload()
    fault.armed = False
    await asyncio.wait_for(probe.entered.wait(), 5)
    probe.release.set()
    await consume(stream)
    assert manager.find_turn_by_send_id(chat.chat_id, "committed") is not None
    assert len(probe.calls) == 1


@pytest.mark.parametrize("kind", ["unlink", "unlock"])
async def test_postreplace_cleanup_exception_keeps_commit_and_local_baseline(admission_case, storage_fault, driver_probe, kind):
    manager, chat, registry = admission_case
    probe = driver_probe(manager, chat)
    fault = storage_fault(manager, registry, kind)
    stream = manager.start_stream(chat.chat_id, "accepted", send_id="owned")
    local = copy.deepcopy(manager._state_payload())
    assert manager._last_local_payload == local
    assert len(fault.replaces) == 1
    assert manager._streaming.drive_task(chat.chat_id, stream) is not None
    revision = json.loads(registry.read_bytes())["revision"]
    fault.armed = False
    manager._save()
    assert json.loads(registry.read_bytes())["revision"] == revision
    chat.title = "one later mutation"
    manager._save()
    assert json.loads(registry.read_bytes())["revision"] == revision + 1
    assert chat.user_turn_count == 1 and manager.find_turn_by_send_id(chat.chat_id, "owned")
    other = ProjectChatManager(manager._config, state_store=manager._state,
                               transcript_store=manager._transcripts, path=registry)
    with project_chats._state_file_lock(other._path):
        assert other.get_chat(chat.chat_id).title == "one later mutation"
    probe.release.set()
    await consume(stream)
    assert len(probe.calls) == 1


async def test_precommit_lock_release_exception_does_not_hide_failed_commit(admission_case, storage_fault):
    manager, chat, registry = admission_case
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    fault = storage_fault(manager, registry, "write_unlock")
    with pytest.raises(OSError) as caught:
        manager.start_stream(chat.chat_id, "rejected")
    error, chain = caught.value, []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    assert any(isinstance(error, OSError) and error.errno == errno.ENOSPC for error in chain)
    assert not any(isinstance(error, project_chats._RegistryCommittedError) for error in chain)
    assert_restored(chat, checkpoint)
    assert registry.read_bytes() == before
    assert manager._broker.get(chat.chat_id) is None
    assert manager._streaming.drive_task(chat.chat_id) is None
    assert not manager._turn_perf_started
    fault.armed = False
    with project_chats._state_file_lock(registry):
        pass


async def test_temp_cleanup_does_not_replace_precommit_write_error(admission_case, storage_fault):
    manager, chat, registry = admission_case
    before = registry.read_bytes()
    storage_fault(manager, registry, "write_unlink")
    with pytest.raises(OSError) as caught:
        manager.start_stream(chat.chat_id, "rejected")
    assert caught.value.errno == errno.ENOSPC
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.__cause__.errno == errno.EACCES
    assert registry.read_bytes() == before
    assert manager._broker.get(chat.chat_id) is None


async def test_retry_same_send_id_after_precommit_failure_runs_once(admission_case, storage_fault, driver_probe):
    manager, chat, registry = admission_case
    probe = driver_probe(manager, chat)
    fault = storage_fault(manager, registry)
    with pytest.raises(OSError):
        manager.start_stream(chat.chat_id, "once", send_id="same")
    assert manager.find_turn_by_send_id(chat.chat_id, "same") is None
    fault.armed = False
    stream = manager.start_stream(chat.chat_id, "once", send_id="same")
    await asyncio.wait_for(probe.entered.wait(), 5)
    assert chat.user_turn_count == 1 and len(probe.calls) == 1
    probe.release.set()
    events = await consume(stream)
    assert any(event["type"] == "result" for event in events)
    assert manager.find_turn_by_send_id(chat.chat_id, "same")["turn_index"] == 0


async def test_resend_after_postcommit_failure_is_deduplicated(admission_case, storage_fault, driver_probe):
    manager, chat, registry = admission_case
    probe = driver_probe(manager, chat)
    fault = storage_fault(manager, registry, "audit")
    stream = manager.start_stream(chat.chat_id, "once", send_id="same")
    fault.armed = False
    assert manager.find_turn_by_send_id(chat.chat_id, "same")["turn_index"] == 0
    assert manager.start_stream(chat.chat_id, "once", send_id="same") is stream
    probe.release.set()
    await consume(stream)
    assert chat.user_turn_count == 1 and len(probe.calls) == 1


async def test_retry_field_staging_has_no_persistence_or_runtime_effects(admission_case, monkeypatch):
    manager, chat, registry = admission_case
    seed_state(manager, chat)
    before = registry.read_bytes()
    task = asyncio.create_task(asyncio.Event().wait())
    manager._retry_tasks[chat.chat_id] = task
    save, publish = Mock(wraps=manager._save), Mock(wraps=manager._publish_retry)
    monkeypatch.setattr(manager, "_save", save)
    monkeypatch.setattr(manager, "_publish_retry", publish)
    try:
        manager._reset_chat_retry_fields(chat)
        assert (chat.retry_status, chat.retry_prompt, chat.retry_image_refs, chat.retry_next_at,
                chat.retry_last_error, chat.retry_attempts, chat.retry_kind,
                chat.retry_interval_seconds, chat.retry_unattended) == ("", "", [], "", "", 0, "", 3600, False)
        assert manager._retry_tasks[chat.chat_id] is task and not task.cancelling()
        assert registry.read_bytes() == before
        save.assert_not_called()
        publish.assert_not_called()
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_admission_without_running_loop_rejects_before_mutation(admission_case):
    manager, chat, registry = admission_case
    checkpoint, before = state_checkpoint(chat), registry.read_bytes()
    with pytest.raises(RuntimeError, match="no running event loop"):
        manager.start_stream(chat.chat_id, "no loop", send_id="rejected")
    assert_restored(chat, checkpoint)
    assert registry.read_bytes() == before
    assert manager._broker.get(chat.chat_id) is None
    assert not manager._turn_perf_started


async def test_recovery_cas_refusal_rolls_back_staged_memory_and_failure_fields(admission_case, monkeypatch):
    manager, chat, registry = admission_case
    source = manager.create_chat(chat.project_id, title="archived source")
    source.archived = True
    source.postprocess = {"steps": {"other": {"state": "done"}}}
    chat.helper = {"kind": "memory_pass", "state": "attention", "source_chat_id": source.chat_id,
                   "archive_policy": "when_clean"}
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failure", "error", "failure-id"
    manager._save()
    assert manager.schedule_continue_recovery(chat.chat_id, "session", "failure", 0, "failure-id")
    manager._cancel_retry_task(chat.chat_id)
    before_chat, before_source = state_checkpoint(chat), state_checkpoint(source)
    prepare = manager._memory_pass.prepare_turn_start
    competitors = []

    def stage_then_claim(target, **kwargs):
        changed = prepare(target, **kwargs)
        assert target.helper["state"] == "running"
        assert source.postprocess["steps"]["memory_pass"]["status"] == "running"
        other = ProjectChatManager(manager._config, state_store=manager._state,
                                   transcript_store=manager._transcripts, path=registry)
        competitors.append(other)
        monkeypatch.setattr(other, "start_stream", lambda *args, **kwargs: object())
        assert other.try_chat_retry_now(chat.chat_id) is not None
        return changed

    monkeypatch.setattr(manager._memory_pass, "prepare_turn_start", stage_then_claim)
    drive = Mock(wraps=manager._streaming.start_drive)
    monkeypatch.setattr(manager._streaming, "start_drive", drive)
    with pytest.raises(ValueError, match="recovery is in flight"):
        manager.start_stream(chat.chat_id, "not admitted", send_id="rejected")
    assert_restored(chat, before_chat)
    assert_restored(source, before_source)
    drive.assert_not_called()
    assert manager._broker.get(chat.chat_id) is None
    assert not manager._turn_perf_started
    persisted = json.loads(registry.read_bytes())["chats"]
    assert persisted[chat.chat_id]["retry_recovery"]["claim"]
    assert persisted[source.chat_id]["postprocess"] == before_source[0]["postprocess"]
    for other in competitors:
        other._cancel_retry_task(chat.chat_id)
        other._release_recovery_claim(chat.chat_id)


@pytest.mark.parametrize("kind", ["unlink", "unlock", "audit"])
async def test_postcommit_cleanup_still_stops_recovery_before_admitting_one_turn(
    admission_case, storage_fault, driver_probe, kind,
):
    manager, chat, registry = admission_case
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failure", "error", "failure-id"
    manager._save()
    assert manager.schedule_continue_recovery(chat.chat_id, "session", "failure", 0, "failure-id", manual=True)
    probe = driver_probe(manager, chat)
    fault = storage_fault(manager, registry, kind)
    stream = manager.start_stream(chat.chat_id, "new turn", send_id="one")
    fault.armed = False
    saved = json.loads(registry.read_bytes())["chats"][chat.chat_id]
    for row in (saved, manager._state_payload()["chats"][chat.chat_id], manager._last_local_payload["chats"][chat.chat_id]):
        assert row["retry_status"] == "stopped"
        assert row["retry_recovery"] == row["retry_schedule_id"] == row["failure_id"] == ""
        assert row["user_turn_count"] == 1
    assert stream.retry_generation == manager._retry_generations[chat.chat_id]
    assert manager._streaming.drive_task(chat.chat_id, stream) is not None
    revision = json.loads(registry.read_bytes())["revision"]
    manager._save()
    assert json.loads(registry.read_bytes())["revision"] == revision
    probe.release.set()
    await consume(stream)
    assert len(probe.calls) == 1


@pytest.mark.parametrize("kind", ["unlink", "unlock"])
def test_active_recovery_is_not_rolled_back_after_postcommit_cleanup(admission_case, storage_fault, kind):
    manager, chat, registry = admission_case
    chat.session_id, chat.last_error, chat.last_response_status, chat.failure_id = "session", "failure", "error", "failure-id"
    manager._save()
    fault = storage_fault(manager, registry, kind)
    try:
        assert manager.schedule_continue_recovery(chat.chat_id, "session", "failure", 0, "failure-id", manual=True)
    finally:
        fault.armed = False
    assert chat.retry_recovery["state"] == "active"
    assert manager._last_local_payload == manager._state_payload()
    assert json.loads(registry.read_bytes())["chats"][chat.chat_id]["retry_recovery"] == chat.retry_recovery


def test_strict_recovery_audit_failure_is_prepared_not_admitted(admission_case, storage_fault):
    manager, chat, registry = admission_case
    chat.session_id, chat.last_error, chat.last_response_status = "session", "failure", "error"
    manager._save()
    baseline = copy.deepcopy(manager._last_local_payload)
    fault = storage_fault(manager, registry, "audit")
    with pytest.raises(RuntimeError, match="audit failure") as rejected:
        manager.schedule_continue_recovery(chat.chat_id, "session", "failure", 0)
    fault.armed = False
    assert not isinstance(rejected.value, project_chats._RegistryCommittedError)
    assert manager._last_local_payload == baseline
    assert chat.retry_status == "" and chat.retry_recovery == ""
    assert chat.chat_id not in manager._retry_tasks
    assert json.loads(registry.read_bytes())["chats"][chat.chat_id]["retry_recovery"]["state"] == "prepared"
