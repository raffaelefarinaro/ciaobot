"""Shadow verdicts for region proposals: store, join, agreement and handover gate."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

import pytest
from tests.cap_checkpoint_helpers import build_checkpoint, validate_checkpoint, checkpoint_config

from ciao import memory_proposals, proposal_predictions as pp


def test_cap_checkpoint_reduces_exact_legacy_occurrences_for_all_lease_starts(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    stamps = [3, 1, 3, 2, 7, 4, 3, 6]
    rows = [
        {"text": f"fact {i}", "kind": "memory", "via": "agent",
         "promoted_at": f"2026-01-01T00:00:0{stamp}+00:00"}
        for i, stamp in enumerate(stamps)
    ]
    sidecars = {".dismissed.jsonl": b"".join((json.dumps(row) + "\n").encode() for row in rows),
                ".dismissed.log": None}
    assert memory_proposals.read_decisions(queue) == []
    checkpoint = build_checkpoint(queue, sidecars, [])
    checkpoint = json.loads(json.dumps(checkpoint))
    assert len(checkpoint["events"]) == 5
    assert len({event["identity"] for event in checkpoint["events"]}) == 5
    for second in range(9):
        since = datetime(2026, 1, 1, 0, 0, second, tzinfo=timezone.utc)
        original = sum(datetime.fromisoformat(row["promoted_at"]) >= since for row in rows)
        reduced = sum(datetime.fromisoformat(event["timestamp"]) >= since for event in checkpoint["events"])
        assert (original >= 5) == (reduced >= 5)
    assert validate_checkpoint(checkpoint, sidecars)
    assert not validate_checkpoint(checkpoint, {**sidecars, ".dismissed.jsonl": None})
    assert not validate_checkpoint(checkpoint, {**sidecars, ".dismissed.jsonl": sidecars[".dismissed.jsonl"][:-1]})
    assert not validate_checkpoint(checkpoint, {**sidecars, ".dismissed.jsonl": b"x" + sidecars[".dismissed.jsonl"][1:]})
    assert validate_checkpoint(checkpoint, {**sidecars, ".dismissed.jsonl": sidecars[".dismissed.jsonl"] + b'{"text":"later"}\n'})
    assert not validate_checkpoint(checkpoint, {**sidecars, ".dismissed.jsonl": sidecars[".dismissed.jsonl"] + b'{"text":'})
    altered = json.loads(json.dumps(checkpoint))
    altered["events"][0]["timestamp"] = "2030-01-01T00:00:00Z"
    assert not validate_checkpoint(altered, sidecars)


def test_cap_checkpoint_preserves_duplicate_legacy_rows_and_rejects_unproved_receipt(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    row = {"text": "same", "kind": "memory", "via": "agent", "promoted_at": "2026-01-01T00:00:00Z"}
    line = (json.dumps(row) + "\n").encode()
    sidecars = {".dismissed.jsonl": line * 2, ".dismissed.log": None}
    events = build_checkpoint(queue, sidecars, [])["events"]
    assert len(events) == 2 and events[0]["identity"] != events[1]["identity"]
    bad = {**row, "queue_receipt_id": "missing"}
    with pytest.raises(ValueError, match="receipt"):
        build_checkpoint(queue, {**sidecars, ".dismissed.jsonl": (json.dumps(bad) + "\n").encode()}, [])
    with pytest.raises(ValueError, match="duplicate sidecar key"):
        build_checkpoint(queue, {**sidecars, ".dismissed.jsonl": b'{"text":"a","text":"b"}\n'}, [])


def test_cap_checkpoint_keeps_independent_occurrences_in_both_sidecars(tmp_path: Path) -> None:
    row = {"text": "same", "kind": "memory", "via": "agent", "promoted_at": "2026-01-01T00:00:00Z"}
    line = (json.dumps(row) + "\n").encode()
    sidecars = {".dismissed.jsonl": line, ".dismissed.log": line}
    checkpoint = build_checkpoint(_queue(tmp_path), sidecars, [])
    assert len(checkpoint["events"]) == 2
    assert len({e["identity"] for e in checkpoint["events"]}) == 2
    assert not validate_checkpoint(checkpoint, {**sidecars, ".dismissed.log": None})


def test_two_generation_checkpoint_retains_equal_time_occurrences(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    old_rows = [{"text": f"old {i}", "via": "agent", "kind": "memory",
                 "promoted_at": "2026-01-01T00:00:00Z"} for i in range(7)]
    new_rows = [{"text": f"new {i}", "via": "agent", "kind": "memory",
                 "promoted_at": "2026-01-01T00:00:00Z"} for i in range(3)]
    encode = lambda rows: b"".join((json.dumps(row) + "\n").encode() for row in rows)
    old = {".dismissed.jsonl": encode(old_rows), ".dismissed.log": None}
    first = build_checkpoint(queue, old, [])
    current = {".dismissed.jsonl": old[".dismissed.jsonl"] + encode(new_rows), ".dismissed.log": None}
    with pytest.raises(ValueError, match="installed checkpoint"):
        build_checkpoint(queue, current, [], previous_checkpoint=first)
    second = build_checkpoint(queue, current, [first], previous_checkpoint=first)
    assert len(second["events"]) == 5 and len({e["identity"] for e in second["events"]}) == 5
    assert validate_checkpoint(second, current)
    assert not validate_checkpoint(second, old)


def _trusted_receipt_checkpoint(queue: Path, rows: list[dict[str, str]]) -> tuple[dict, bytes]:
    import hashlib
    from ciao import memory_receipts as mr
    lines = [(json.dumps(row) + "\n").encode() for row in rows]
    raw = b"".join(lines)
    events = [{"identity": f"receipt:{row['queue_receipt_id']}", "timestamp": row["promoted_at"],
               "sidecar": "", "ordinal": -1, "row_sha256": hashlib.sha256(line[:-1]).hexdigest(),
               "receipt_id": row["queue_receipt_id"], "proposal_id": "", "kind": row["kind"],
               "source": "", "identity_text": row["text"], "destination_receipt_id": "", "outcome": ""}
              for row, line in zip(rows, lines)]
    checkpoint = {"v": mr.RECEIPT_VERSION, "id": mr.CAP_CHECKPOINT_ID, "kind": "cap_event_checkpoint",
                  "status": mr.APPLIED, "schema": 1, "queue": str(queue),
                  "sidecars": {".dismissed.jsonl": {"length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                               ".dismissed.log": None}, "events": events[-5:]}
    queue.with_suffix(".dismissed.jsonl").write_bytes(raw)
    return checkpoint, raw


def test_checkpoint_top_five_cannot_identify_older_replayed_receipt(tmp_path: Path) -> None:
    rows = [
        {"text": f"fact {i}", "kind": "memory", "via": "agent",
         "queue_receipt_id": f"receipt-{i}", "promoted_at": f"2026-01-01T00:00:0{i}Z"}
        for i in range(7)
    ]
    encode = lambda row: (json.dumps(row) + "\n").encode()
    queue = _queue(tmp_path)
    checkpoint, prefix = _trusted_receipt_checkpoint(queue, rows)
    assert "receipt-0" not in {event["receipt_id"] for event in checkpoint["events"]}
    queue.with_suffix(".dismissed.jsonl").write_bytes(prefix + encode(rows[0]))
    assert pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))[0] == checkpoint["events"]
    queue.with_suffix(".dismissed.jsonl").write_bytes(prefix + encode({**rows[0], "text": "changed"}))
    with pytest.raises(ValueError, match="inconsistent duplicate"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))


def test_checkpoint_binds_non_cap_rows_independently_of_events(tmp_path: Path) -> None:
    import hashlib
    queue = _queue(tmp_path)
    rows = [{"text": "fact", "kind": "memory", "via": "agent",
             "queue_receipt_id": "receipt", "promoted_at": "2026-01-01T00:00:00Z"}]
    checkpoint, prefix = _trusted_receipt_checkpoint(queue, rows)
    non_cap = b'{"text":"other fact","kind":"memory","via":"pwa","dismissed_at":"2026-01-01T00:00:00Z"}\n'
    prefix += non_cap
    sidecar = queue.with_suffix(".dismissed.jsonl")
    sidecar.write_bytes(prefix)
    checkpoint["sidecars"][".dismissed.jsonl"] = {
        "length": len(prefix), "sha256": hashlib.sha256(prefix).hexdigest(),
    }
    assert pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))[0] == checkpoint["events"]
    sidecar.write_bytes(prefix.replace(b"other fact", b"alter fact"))
    with pytest.raises(ValueError):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))


def test_checkpoint_receipt_rescans_use_bounded_batches(tmp_path: Path, monkeypatch) -> None:
    queue = _queue(tmp_path)
    rows = [{"text": f"fact {i}", "kind": "memory", "via": "agent",
             "queue_receipt_id": f"receipt-{i}", "promoted_at": f"2026-01-01T00:00:{i:02}Z"}
            for i in range(32)]
    checkpoint, _raw = _trusted_receipt_checkpoint(queue, rows)
    real = memory_proposals.DecisionSidecarStream.__iter__
    calls = []
    def counted(stream):
        calls.append(stream.path)
        return real(stream)
    monkeypatch.setattr(memory_proposals.DecisionSidecarStream, "__iter__", counted)
    events, _identities = pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert events == checkpoint["events"]
    assert len(calls) <= 4


def test_checkpoint_receipt_index_scans_scale_near_linearly(tmp_path: Path, monkeypatch) -> None:
    from datetime import timedelta
    real = memory_proposals.DecisionSidecarStream.__iter__
    scanned = [0]
    def counted(stream):
        for entry in real(stream):
            scanned[0] += 1
            yield entry
    monkeypatch.setattr(memory_proposals.DecisionSidecarStream, "__iter__", counted)
    totals = []
    for count in (64, 128):
        queue = _queue(tmp_path / str(count))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{"text": f"fact {i}", "kind": "memory", "via": "agent",
                 "queue_receipt_id": f"receipt-{i}",
                 "promoted_at": (base + timedelta(seconds=i)).isoformat()}
                for i in range(count)]
        checkpoint, _ = _trusted_receipt_checkpoint(queue, rows)
        scanned[0] = 0
        assert pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))[0] == checkpoint["events"]
        totals.append(scanned[0])
        assert not list(queue.parent.glob(".cap-index-*.sqlite"))
    assert totals[1] <= 2.2 * totals[0], totals


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS ru_maxrss units")
def test_checkpoint_many_unique_receipts_native_rss_is_bounded(tmp_path: Path) -> None:
    import subprocess
    import sys
    from datetime import timedelta
    peaks = []
    for count in (1000, 16000):
        queue = _queue(tmp_path / str(count))
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [{"text": f"fact {i} " + "x" * 1024, "kind": "memory", "via": "agent",
                 "queue_receipt_id": f"receipt-{i}",
                 "promoted_at": (base + timedelta(seconds=i)).isoformat()}
                for i in range(count)]
        checkpoint, _ = _trusted_receipt_checkpoint(queue, rows)
        checkpoint_path = queue.parent / "test-checkpoint.json"
        checkpoint_path.write_text(json.dumps(checkpoint))
        script = (
            "import json,resource,sys; from pathlib import Path; "
            "from ciao.proposal_predictions import stream_checkpoint_events; "
            "from tests.cap_checkpoint_helpers import checkpoint_config; "
            "q=Path(sys.argv[1]); c=json.loads(Path(sys.argv[2]).read_text()); "
            "events,_=stream_checkpoint_events(q,c,[],config=checkpoint_config(q)); assert events==c['events']; "
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(queue), str(checkpoint_path)],
            check=True, capture_output=True, text=True,
        )
        peaks.append(int(result.stdout.strip()))
    assert peaks[1] - peaks[0] < 16 * 1024 * 1024, peaks


def test_checkpoint_index_is_private_and_removed_after_error(tmp_path: Path, monkeypatch) -> None:
    import os
    import stat
    queue = _queue(tmp_path)
    rows = [{"text": "private", "kind": "memory", "via": "agent", "queue_receipt_id": "receipt",
             "promoted_at": "2026-01-01T00:00:00Z"}]
    checkpoint, _ = _trusted_receipt_checkpoint(queue, rows)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    real = memory_proposals.DecisionSidecarStream.__iter__
    observed = []
    def fail_during_scan(stream):
        files = list(runtime.glob("op-*/index.sqlite"))
        assert len(files) == 1
        if os.name != "nt":
            assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        observed.append(files[0])
        yield from real(stream)
        raise ValueError("injected scan failure")
    monkeypatch.setattr(memory_proposals.DecisionSidecarStream, "__iter__", fail_during_scan)
    with pytest.raises(ValueError, match="injected"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert observed and not observed[0].exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
def test_checkpoint_index_crash_leaves_no_plaintext(tmp_path: Path, monkeypatch) -> None:
    import os
    from datetime import timedelta
    queue = _queue(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"text": f"private fact {i} " + "x" * 4096, "kind": "memory", "via": "agent",
             "queue_receipt_id": f"private-receipt-{i}",
             "promoted_at": (base + timedelta(seconds=i)).isoformat()}
            for i in range(700)]
    checkpoint, _ = _trusted_receipt_checkpoint(queue, rows)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        real = memory_proposals.DecisionSidecarStream.__iter__
        scans = [0]
        def crash_before_second_scan(stream):
            scans[0] += 1
            if scans[0] == 2:
                os.write(ready_w, b"1")
                os.read(hold_r, 1)
            return real(stream)
        memory_proposals.DecisionSidecarStream.__iter__ = crash_before_second_scan
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
        os._exit(2)
    os.close(ready_w)
    os.close(hold_r)
    assert os.read(ready_r, 1) == b"1"
    os.kill(pid, __import__("signal").SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(ready_r)
    os.close(hold_w)
    assert os.waitstatus_to_exitcode(status) == -__import__("signal").SIGKILL
    indexes = list(runtime.glob("op-*/index.sqlite"))
    assert len(indexes) == 1
    raw = indexes[0].read_bytes()
    assert b"private fact" not in raw and b"private-receipt" not in raw
    assert not list(runtime.glob("op-*/index.sqlite-*"))
    assert pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))[0] == checkpoint["events"]
    assert not indexes[0].exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
def test_checkpoint_recovers_marker_after_index_unlinked(tmp_path: Path, monkeypatch) -> None:
    import os
    import signal
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        unlink = Path.unlink
        def pause_after_unlink(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path.name == "index.sqlite":
                os.write(ready_w, b"1")
                os.read(hold_r, 1)
            return result
        Path.unlink = pause_after_unlink
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
        os._exit(2)
    os.close(ready_w)
    os.close(hold_r)
    try:
        assert os.read(ready_r, 1) == b"1"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    finally:
        os.close(ready_r)
        os.close(hold_w)
    orphan = next(runtime.glob("op-*"))
    assert (orphan / "owner.json").exists() and not (orphan / "index.sqlite").exists()
    pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert not orphan.exists()


def test_checkpoint_cleanup_preserves_incomplete_mismatched_and_unrelated(tmp_path: Path) -> None:
    import os
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    incomplete = runtime / ("op-" + "a" * 32)
    incomplete.mkdir(mode=0o700)
    (incomplete / "index.sqlite").write_bytes(b"early crash")
    mismatch = runtime / ("op-" + "b" * 32)
    mismatch.mkdir(mode=0o700)
    index = mismatch / "index.sqlite"
    index.write_bytes(b"unrelated")
    index.chmod(0o600)
    marker = mismatch / "owner.json"
    marker.write_text(json.dumps({"version": 1, "dev": os.stat(index).st_dev, "ino": os.stat(index).st_ino + 1}))
    marker.chmod(0o600)
    extra = runtime / ("op-" + "c" * 32)
    extra.mkdir(mode=0o700)
    (extra / "other").write_text("keep")
    link = runtime / ("op-" + "d" * 32)
    link.symlink_to(extra, target_is_directory=True)
    pp._cap_cleanup(runtime)
    assert (incomplete / "index.sqlite").read_bytes() == b"early crash"
    assert index.read_bytes() == b"unrelated"
    assert (extra / "other").read_text() == "keep"
    assert link.is_symlink()


def test_checkpoint_marker_only_wrong_directory_identity_is_preserved(tmp_path: Path) -> None:
    operation = tmp_path / ("op-" + "a" * 32)
    operation.mkdir(mode=0o700)
    info = operation.stat()
    marker = operation / "owner.json"
    marker.write_text(json.dumps({"version": 2, "dev": 1, "ino": 1,
                                  "dir_dev": info.st_dev, "dir_ino": info.st_ino + 1}))
    marker.chmod(0o600)
    pp._cap_cleanup_operation(operation)
    assert marker.exists()


def test_checkpoint_reserved_slot_does_not_remove_unrelated_contents(tmp_path: Path, monkeypatch) -> None:
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    slot = runtime / pp._CAP_OPERATION_SLOT
    slot.mkdir(mode=0o700)
    (slot / "unrelated").write_text("preserve")
    with pytest.raises(ValueError, match="slot needs manual inspection"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert (slot / "unrelated").read_text() == "preserve"


def test_operator_slot_recovery_holds_guard_and_public_retry_succeeds(tmp_path: Path, monkeypatch) -> None:
    import os
    from ciao.os_support.locks import lock_exclusive
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    slot = runtime / pp._CAP_OPERATION_SLOT
    slot.mkdir(mode=0o700)
    (slot / "evidence").write_text("preserve")
    with pytest.raises(pp.CapSlotNeedsInspection):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    evidence = tmp_path / "operator-preserved"
    with pp._cap_runtime_lock(runtime):
        competing = os.open(runtime / "guard.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                lock_exclusive(competing, blocking=False)
            assert (slot / "evidence").read_text() == "preserve"
            slot.rename(evidence)
        finally:
            os.close(competing)
    assert pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))[0] == checkpoint["events"]
    assert (evidence / "evidence").read_text() == "preserve"


def test_checkpoint_cleanup_retries_after_sharing_violation(tmp_path: Path, monkeypatch) -> None:
    import os
    operation = tmp_path / ("op-" + "a" * 32)
    operation.mkdir(mode=0o700)
    index = operation / "index.sqlite"
    index.write_bytes(b"index")
    index.chmod(0o600)
    info = index.stat()
    marker = operation / "owner.json"
    marker.write_text(json.dumps({"version": 1, "dev": info.st_dev, "ino": info.st_ino}))
    marker.chmod(0o600)
    real = Path.unlink
    def busy(path, *args, **kwargs):
        if path == index:
            raise PermissionError("simulated Windows sharing violation")
        return real(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", busy)
        pp._cap_cleanup_operation(operation)
    assert index.exists() and marker.exists()
    pp._cap_cleanup_operation(operation)
    assert not operation.exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process test needs fork")
def test_checkpoint_live_operation_holds_global_lock(tmp_path: Path, monkeypatch) -> None:
    import os
    from ciao.os_support.locks import lock_exclusive
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ready_r)
            os.close(release_w)
            real = pp._stream_checkpoint_events
            def hold(*args, **kwargs):
                os.write(ready_w, b"1")
                os.read(release_r, 1)
                return real(*args, **kwargs)
            pp._stream_checkpoint_events = hold
            pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
            os._exit(0)
        except BaseException:
            os._exit(2)
    os.close(ready_w)
    os.close(release_r)
    try:
        assert os.read(ready_r, 1) == b"1"
        indexes = list(runtime.glob("op-*/index.sqlite"))
        assert len(indexes) == 1
        fd = os.open(runtime / "guard.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                lock_exclusive(fd, blocking=False)
            assert indexes[0].exists()
        finally:
            os.close(fd)
    finally:
        os.write(release_w, b"1")
        os.close(release_w)
        os.close(ready_r)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert not indexes[0].exists()


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process test needs fork")
def test_two_public_checkpoint_streams_serialize_active_index(tmp_path: Path, monkeypatch) -> None:
    import os
    import select
    import signal
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    first_ready_r, first_ready_w = os.pipe()
    first_release_r, first_release_w = os.pipe()
    second_ready_r, second_ready_w = os.pipe()
    second_done_r, second_done_w = os.pipe()
    first = os.fork()
    if first == 0:
        try:
            real = pp._stream_checkpoint_events
            def hold(*args, **kwargs):
                os.write(first_ready_w, b"1")
                os.read(first_release_r, 1)
                return real(*args, **kwargs)
            pp._stream_checkpoint_events = hold
            pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
            os._exit(0)
        except BaseException:
            os._exit(2)
    second = None
    try:
        assert os.read(first_ready_r, 1) == b"1"
        active = next(runtime.glob("op-*/index.sqlite"))
        second = os.fork()
        if second == 0:
            try:
                os.write(second_ready_w, b"1")
                pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
                os.write(second_done_w, b"1")
                os._exit(0)
            except BaseException:
                os._exit(2)
        assert os.read(second_ready_r, 1) == b"1"
        assert not select.select([second_done_r], [], [], 0.2)[0]
        assert active.exists()
        os.write(first_release_w, b"1")
        assert select.select([second_done_r], [], [], 10)[0]
        assert os.read(second_done_r, 1) == b"1"
        assert os.waitstatus_to_exitcode(os.waitpid(first, 0)[1]) == 0
        first = 0
        assert os.waitstatus_to_exitcode(os.waitpid(second, 0)[1]) == 0
        second = None
        assert not list(runtime.glob("op-*/index.sqlite"))
    finally:
        for pid in (first, second):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except ProcessLookupError:
                    pass
        for fd in (first_ready_r, first_ready_w, first_release_r, first_release_w,
                   second_ready_r, second_ready_w, second_done_r, second_done_w):
            os.close(fd)


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process test needs fork")
@pytest.mark.parametrize("seam", ["after-mkdir", "before-marker"])
def test_checkpoint_early_crash_preserves_incomplete_operation(tmp_path: Path, monkeypatch, seam: str) -> None:
    import os
    import signal
    from ciao.os_support import private
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        mkdir = Path.mkdir
        def pause_after_mkdir(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            if seam == "after-mkdir" and path.name == pp._CAP_OPERATION_SLOT:
                os.write(ready_w, b"1")
                os.read(hold_r, 1)
            return result
        Path.mkdir = pause_after_mkdir
        real = private.open_private
        def crash(path, *args, **kwargs):
            if seam == "before-marker" and Path(path).name == "owner.json":
                os.write(ready_w, b"1")
                os.read(hold_r, 1)
            return real(path, *args, **kwargs)
        private.open_private = crash
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
        os._exit(2)
    os.close(ready_w)
    os.close(hold_r)
    try:
        assert os.read(ready_r, 1) == b"1"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    finally:
        os.close(ready_r)
        os.close(hold_w)
    incomplete = next(runtime.glob("op-*"))
    assert (incomplete / "index.sqlite").exists() is (seam == "before-marker")
    assert not (incomplete / "owner.json").exists()
    for _ in range(3):
        with pytest.raises(ValueError, match="slot needs manual inspection"):
            pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert (incomplete / "index.sqlite").exists() is (seam == "before-marker")
    assert list(runtime.glob("op-*")) == [incomplete]


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="process-crash test needs fork")
def test_checkpoint_crash_after_marker_deletion_is_bounded(tmp_path: Path, monkeypatch) -> None:
    import os
    import signal
    queue = _queue(tmp_path)
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(pp, "_cap_runtime", lambda queue, config: runtime)
    ready_r, ready_w = os.pipe()
    hold_r, hold_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_r)
        os.close(hold_w)
        unlink = Path.unlink
        def pause_after_marker(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path.name == "owner.json":
                os.write(ready_w, b"1")
                os.read(hold_r, 1)
            return result
        Path.unlink = pause_after_marker
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
        os._exit(2)
    os.close(ready_w)
    os.close(hold_r)
    try:
        assert os.read(ready_r, 1) == b"1"
        os.kill(pid, signal.SIGKILL)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
    finally:
        os.close(ready_r)
        os.close(hold_w)
    orphan = runtime / pp._CAP_OPERATION_SLOT
    assert orphan.is_dir() and list(orphan.iterdir()) == []
    for _ in range(3):
        with pytest.raises(ValueError, match="slot needs manual inspection"):
            pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(queue))
    assert list(runtime.glob("op-*")) == [orphan]


def test_checkpoint_runtime_is_outside_backup_scope(tmp_path: Path, monkeypatch) -> None:
    import tempfile
    from ciao import backup_scope
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    queue = _queue(tmp_path / "vault")
    runtime = pp._cap_runtime(queue, checkpoint_config(queue))
    monkeypatch.setattr(backup_scope, "data_root", lambda config: queue.parent.parent)
    assert not backup_scope.is_eligible(runtime / "op-example" / "index.sqlite", object())
    assert not runtime.is_relative_to(queue.parent.parent)


@pytest.mark.parametrize("case", ["enclosing", "other-workspace", "safe"])
def test_checkpoint_configured_backup_roots_are_checked(tmp_path: Path, monkeypatch, case: str) -> None:
    import tempfile
    from ciao.config import CiaoConfig, WorkspaceConfig
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    primary = tmp_path / "primary"
    other = system_temp if case == "other-workspace" else tmp_path / "other"
    vault = tmp_path if case == "enclosing" else primary
    config = CiaoConfig(
        pwa_auth_token="test", workspace_root=tmp_path / "workspace",
        state_path=tmp_path / "state.json", media_root=tmp_path / "media", vault_root=vault,
        workspaces={
            "primary": WorkspaceConfig(name="primary", vault_root=str(primary)),
            "other": WorkspaceConfig(name="other", vault_root=str(other)),
        },
    )
    if case == "safe":
        pp._cap_check_backup_scope(config)
    else:
        with pytest.raises(ValueError, match="configured backup root"):
            pp._cap_check_backup_scope(config)


@pytest.mark.parametrize("case", ["enclosing", "other-workspace"])
def test_public_checkpoint_refuses_backup_overlap_before_runtime_creation(tmp_path: Path, monkeypatch, case: str) -> None:
    import tempfile
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.os_support.users import user_key
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    queue = _queue(tmp_path / "primary")
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    vault = tmp_path if case == "enclosing" else queue.parent.parent
    other = system_temp if case == "other-workspace" else tmp_path / "other"
    config = CiaoConfig(
        pwa_auth_token="test", workspace_root=tmp_path / "workspace",
        state_path=tmp_path / "state.json", media_root=tmp_path / "media", vault_root=vault,
        workspaces={"primary": WorkspaceConfig(name="primary", vault_root=str(queue.parent.parent)),
                    "other": WorkspaceConfig(name="other", vault_root=str(other))},
    )
    with pytest.raises(ValueError, match="configured backup root"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=config)
    assert not (system_temp / f"ciao-cap-index-{user_key()}").exists()
    with pytest.raises(ValueError, match="configuration is unavailable"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=None)


def test_public_checkpoint_rejects_unregistered_queue(tmp_path: Path) -> None:
    queue = _queue(tmp_path / "primary")
    other = _queue(tmp_path / "unrelated")
    checkpoint, _ = _trusted_receipt_checkpoint(queue, [
        {"text": "fact", "kind": "memory", "via": "agent", "queue_receipt_id": "r",
         "promoted_at": "2026-01-01T00:00:00Z"},
    ])
    with pytest.raises(ValueError, match="not registered"):
        pp.stream_checkpoint_events(queue, checkpoint, [], config=checkpoint_config(other))


def test_checkpoint_runtime_rejects_existing_symlink(tmp_path: Path, monkeypatch) -> None:
    import tempfile
    from ciao.os_support.users import user_key
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    queue = _queue(tmp_path / "vault")
    target = tmp_path / "other"
    target.mkdir(mode=0o700)
    (system_temp / f"ciao-cap-index-{user_key()}").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe checkpoint runtime"):
        pp._cap_runtime(queue, checkpoint_config(queue))
    assert target.exists()


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS extended ACL")
def test_checkpoint_runtime_rejects_macos_acl_without_repair(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    import tempfile
    system_temp = tmp_path / "system-temp"
    system_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_temp))
    queue = _queue(tmp_path / "vault")
    runtime = pp._cap_runtime(queue, checkpoint_config(queue))
    subprocess.run(["chmod", "+a", "everyone allow read", str(runtime)], check=True)
    with pytest.raises(ValueError, match="unsafe checkpoint runtime"):
        pp._cap_runtime(queue, checkpoint_config(queue))
    assert "everyone allow" in subprocess.run(
        ["ls", "-lde", str(runtime)], capture_output=True, text=True, check=True,
    ).stdout


def _queue(tmp_path: Path) -> Path:
    path = tmp_path / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Memory Proposals\n", encoding="utf-8")
    return path


def _write_predictions(queue: Path, rows: list[dict[str, str]]) -> None:
    sidecar = pp.predictions_path(queue)
    sidecar.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _prediction(ts: str, text: str, verdict: str, kind: str = "memory") -> dict[str, str]:
    return {
        "predicted_at": ts,
        "kind": kind,
        "text": text,
        "verdict": verdict,
        "reason": "why",
    }


def _decision(
    ts: str,
    text: str,
    action: str,
    *,
    kind: str = "memory",
    via: str = "pwa",
    outcome: str = "",
) -> dict[str, str]:
    return {
        "ts": ts,
        "action": action,
        "kind": kind,
        "text": text,
        "via": via,
        "outcome": outcome,
    }


def test_prediction_key_flattens_whitespace_and_lowercases_kind() -> None:
    assert pp.prediction_key("Memory", "a  b") == pp.prediction_key("memory", "a b")


def test_record_prediction_appends_json_line_beside_the_queue(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    assert pp.record_prediction(
        queue, kind="memory", text="Prefers tea", verdict="accept", reason="stable"
    )
    sidecar = pp.predictions_path(queue)
    assert sidecar.name == "Memory-Proposals.predictions.jsonl"
    assert sidecar.parent == queue.parent
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "memory"
    assert rows[0]["text"] == "Prefers tea"
    assert rows[0]["verdict"] == "accept"
    assert rows[0]["reason"] == "stable"
    assert rows[0]["predicted_at"]


def test_record_prediction_is_a_noop_for_an_identical_latest_row(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    kwargs = dict(kind="memory", text="x", verdict="dismiss", reason="noise")
    assert pp.record_prediction(queue, **kwargs) is True
    assert pp.record_prediction(queue, **kwargs) is False
    lines = pp.predictions_path(queue).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_a_changed_verdict_appends_and_latest_wins(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    pp.record_prediction(queue, kind="memory", text="x", verdict="dismiss", reason="a")
    pp.record_prediction(queue, kind="memory", text="x", verdict="accept", reason="a")
    latest = pp.latest_predictions(queue)
    assert latest[pp.prediction_key("memory", "x")]["verdict"] == "accept"
    assert [row["verdict"] for row in pp.read_predictions(queue)] == ["dismiss", "accept"]


def test_record_prediction_rejects_bad_kind_verdict_and_empty_reason(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="people", text="x", verdict="accept", reason="r")
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="memory", text="x", verdict="maybe", reason="r")
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="memory", text="x", verdict="accept", reason="  ")
    pp.record_prediction(
        queue, kind="memory", text="x", verdict="accept", reason="a\nb" + "z" * 400
    )
    row = pp.read_predictions(queue)[0]
    assert "\n" not in row["reason"]
    assert len(row["reason"]) == pp.REASON_MAX_CHARS


def test_read_predictions_skips_garbage_lines(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    sidecar = pp.predictions_path(queue)
    sidecar.write_text(
        "not json\n[1, 2]\n"
        + json.dumps(_prediction("2026-01-01T00:00:00+00:00", "x", "accept"))
        + "\n",
        encoding="utf-8",
    )
    assert len(pp.read_predictions(queue)) == 1


def test_attach_predictions_joins_latest_prediction_not_after_the_decision(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "dismiss"),
            _prediction("2026-01-01T00:00:02+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:05+00:00", "x", "dismiss"),
        ],
    )
    rows = [_decision("2026-01-01T00:00:03+00:00", "x", "accepted")]
    pp.attach_predictions(queue, rows)
    assert rows[0]["prediction"] == {
        "verdict": "accept",
        "reason": "why",
        "ts": "2026-01-01T00:00:02+00:00",
        "match": True,
    }


def test_attach_predictions_skips_undated_and_non_region_rows(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:01+00:00", "p", "accept", kind="memory"),
        ],
    )
    rows = [
        _decision("", "x", "accepted"),
        _decision("2026-01-01T00:00:03+00:00", "p", "accepted", kind="people"),
        _decision("2026-01-01T00:00:03+00:00", "none", "accepted"),
    ]
    pp.attach_predictions(queue, rows)
    assert all("prediction" not in row for row in rows)


def test_attach_predictions_match_is_false_on_disagreement(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "a", "accept"),
            _prediction("2026-01-01T00:00:01+00:00", "b", "dismiss"),
        ],
    )
    rows = [
        _decision("2026-01-01T00:00:03+00:00", "a", "dismissed"),
        _decision("2026-01-01T00:00:03+00:00", "b", "dismissed"),
    ]
    pp.attach_predictions(queue, rows)
    assert rows[0]["prediction"]["match"] is False
    assert rows[1]["prediction"]["match"] is True


def _joined(
    n: int,
    *,
    matching: int | None = None,
    start: int = 0,
    **overrides: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    matching = n if matching is None else matching
    for i in range(n):
        row: dict[str, object] = {
            "ts": f"2026-01-01T00:{(start + i) // 60:02d}:{(start + i) % 60:02d}+00:00",
            "action": "dismissed",
            "kind": "memory",
            "text": f"t{start + i}",
            "via": "pwa",
            "outcome": "",
            "prediction": {
                "verdict": "dismiss" if i < matching else "accept",
                "reason": "r",
                "ts": "x",
                "match": i < matching,
            },
        }
        row.update(overrides)
        rows.append(row)
    return rows


def test_agreement_counts_only_operator_region_decisions() -> None:
    rows = _joined(2)
    rows += _joined(1, via="agent", start=10)
    rows += _joined(1, via="auto", start=11)
    rows += _joined(1, outcome="swept", start=12)
    rows += _joined(1, kind="people", start=13)
    rows += [
        {
            "ts": "2026-01-01T01:00:00+00:00",
            "action": "dismissed",
            "kind": "memory",
            "text": "no prediction",
            "via": "pwa",
            "outcome": "",
        }
    ]
    result = pp.agreement(rows, handover="off")
    assert result["decided"] == 2
    assert result["matched"] == 2


def test_agreement_uses_the_newest_fifty() -> None:
    old = _joined(10, matching=0, start=0)
    new = _joined(50, start=100)
    result = pp.agreement(old + new, handover="off")
    assert result["decided"] == 50
    assert result["rate"] == 1.0
    assert result["window"] == 50


def test_agreement_rate_is_none_with_no_decisions() -> None:
    result = pp.agreement([], handover="off")
    assert result["decided"] == 0
    assert result["rate"] is None
    assert result["suggest_handover"] is False


def test_suggest_handover_needs_twenty_decisions_ninety_percent_and_off() -> None:
    assert pp.agreement(_joined(19), handover="off")["suggest_handover"] is False
    assert pp.agreement(_joined(20, matching=17), handover="off")["suggest_handover"] is False
    assert pp.agreement(_joined(20, matching=18), handover="off")["suggest_handover"] is True
    assert pp.agreement(_joined(20), handover="dismiss")["suggest_handover"] is False


def test_agreement_reports_per_verdict_breakdown() -> None:
    rows = _joined(4, matching=3)  # 3 predicted dismiss+matched, 1 predicted accept+missed
    result = pp.agreement(rows, handover="off")
    assert result["predicted_dismiss"] == 3
    assert result["dismiss_matched"] == 3
    assert result["predicted_accept"] == 1
    assert result["accept_matched"] == 0


@pytest.mark.parametrize(
    ("handover", "verdict", "expected"),
    [
        ("off", "accept", False),
        ("off", "dismiss", False),
        ("dismiss", "accept", False),
        ("dismiss", "dismiss", True),
        ("all", "accept", True),
        ("all", "dismiss", True),
    ],
)
def test_handover_allows_matrix(handover: str, verdict: str, expected: bool) -> None:
    assert pp.handover_allows(handover, verdict) is expected


def test_agreement_for_queue_reads_the_decision_sidecar(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for i in range(20):
        text = f"fact {i}"
        pp.record_prediction(queue, kind="memory", text=text, verdict="accept", reason="r")
        if i < 18:
            memory_proposals.record_promotion(
                queue, text=text, kind="memory", via="pwa", outcome="written"
            )
        else:
            memory_proposals.record_dismissal(
                queue, text=text, kind="memory", via="pwa"
            )
    result = pp.agreement_for_queue(queue, handover="off")
    assert result["decided"] == 20
    assert result["matched"] == 18
    assert result["rate"] == 0.9
    assert result["suggest_handover"] is True


def _write_decisions(queue: Path, rows: list[tuple[str, str, str]]) -> None:
    """Hand-written decision sidecar rows: ``(ts, action, text)``."""
    from ciao.memory_proposals import dismissed_log_path

    log = dismissed_log_path(queue)
    key = {"accepted": "promoted_at", "dismissed": "dismissed_at"}
    log.write_text(
        "".join(
            json.dumps({key[action]: ts, "kind": "memory", "text": text, "via": "pwa"}) + "\n"
            for ts, action, text in rows
        ),
        encoding="utf-8",
    )


def test_a_prediction_older_than_a_decision_on_the_same_fact_is_not_live(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:01+00:00", "x", "accept")])
    _write_decisions(queue, [("2026-01-01T00:00:05+00:00", "dismissed", "x")])

    assert pp.latest_predictions(queue) == {}
    # The raw log keeps it: nothing is ever deleted.
    assert len(pp.read_predictions(queue)) == 1


def test_a_prediction_made_after_the_decision_is_live(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:09+00:00", "x", "accept")])
    _write_decisions(queue, [("2026-01-01T00:00:05+00:00", "dismissed", "x")])

    assert pp.prediction_key("memory", "x") in pp.latest_predictions(queue)


def test_history_joins_only_a_prediction_made_after_the_previous_decision(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:20+00:00", "x", "dismiss"),
        ],
    )
    _write_decisions(
        queue,
        [
            ("2026-01-01T00:00:10+00:00", "dismissed", "x"),
            ("2026-01-01T00:00:30+00:00", "dismissed", "x"),
        ],
    )
    rows = [
        _decision("2026-01-01T00:00:10+00:00", "x", "dismissed"),
        _decision("2026-01-01T00:00:30+00:00", "x", "dismissed"),
    ]

    pp.attach_predictions(queue, rows)

    assert rows[0]["prediction"]["verdict"] == "accept"
    assert rows[1]["prediction"]["verdict"] == "dismiss"
    assert rows[1]["prediction"]["ts"] == "2026-01-01T00:00:20+00:00"


def test_a_requeued_fact_with_only_a_stale_prediction_gets_no_join(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:01+00:00", "x", "accept")])
    _write_decisions(
        queue,
        [
            ("2026-01-01T00:00:10+00:00", "dismissed", "x"),
            ("2026-01-01T00:00:30+00:00", "dismissed", "x"),
        ],
    )
    rows = [_decision("2026-01-01T00:00:30+00:00", "x", "dismissed")]

    pp.attach_predictions(queue, rows)

    assert "prediction" not in rows[0]


def test_join_with_only_later_predictions_attaches_nothing(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:09+00:00", "x", "accept")])
    rows = [_decision("2026-01-01T00:00:05+00:00", "x", "accepted")]

    pp.attach_predictions(queue, rows)

    assert "prediction" not in rows[0]


def test_a_prediction_at_exactly_the_decision_time_is_joined(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:05+00:00", "x", "accept")])
    rows = [_decision("2026-01-01T00:00:05+00:00", "x", "accepted")]

    pp.attach_predictions(queue, rows)

    assert rows[0]["prediction"]["match"] is True


def test_latest_wins_through_dismiss_accept_dismiss(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for verdict, reason in (("dismiss", "a"), ("accept", "a"), ("dismiss", "a")):
        assert pp.record_prediction(queue, kind="memory", text="x", verdict=verdict, reason=reason)
    assert pp.latest_predictions(queue)[pp.prediction_key("memory", "x")]["verdict"] == "dismiss"
    assert len(pp.read_predictions(queue)) == 3


def test_agreement_ignores_row_order_and_windows_fifty_one_rows() -> None:
    rows = _joined(51, matching=51)
    rows[0]["prediction"] = {"verdict": "accept", "reason": "r", "ts": "x", "match": False}
    shuffled = list(reversed(rows))

    result = pp.agreement(shuffled, handover="off")

    # The oldest row (the only mismatch) falls outside the newest fifty.
    assert result["decided"] == 50
    assert result["matched"] == 50


def test_suggest_handover_is_false_under_all() -> None:
    assert pp.agreement(_joined(20), handover="all")["suggest_handover"] is False
