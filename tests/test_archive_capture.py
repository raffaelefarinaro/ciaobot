"""The workspace-scoped log of archive captures (#3239-c1)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ciao.archive_capture import (
    CAPTURES_READ_CEILING,
    CaptureRecord,
    append_capture,
    build_record,
    capture_id,
    captures_path,
    read_captures,
)
from ciao.task_attempts import TaskAttempt
from ciao.task_decisions import DecisionRead, DecisionRow

NOW = datetime(2026, 11, 21, 10, 0, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=2)
EXACT_KEYS = {
    "v",
    "id",
    "kind",
    "workspace",
    "chat_id",
    "captured_at",
    "turn_count",
    "attempt_ids",
    "attempts_status",
    "decision_ids",
    "decisions_status",
    "omitted_decisions",
    "completion_id",
    "complete",
}


def _hex(n: int) -> str:
    return f"{n:032x}"


def _attempt(n: int, chat_id: str = "chat-1") -> TaskAttempt:
    return TaskAttempt(
        attempt_id=_hex(n),
        task_id=_hex(1000 + n),
        task_revision="r1",
        chat_id=chat_id,
        state="ready_for_review",
        created_at="2026-11-21T09:00:00+00:00",
        updated_at="2026-11-21T09:00:00+00:00",
    )


def _decision(n: int, attempt_n: int) -> DecisionRow:
    return DecisionRow(
        id=_hex(n),
        at="2026-11-21T09:30:00+00:00",
        kind="complete",
        task_id=_hex(1000 + attempt_n),
        attempt_id=_hex(attempt_n),
        actor="agent",
        via="pwa",
        reason="free text the record must never hold",
    )


def _ok(*rows: DecisionRow) -> DecisionRead:
    return DecisionRead("ok", tuple(rows), 0)


def _record(
    *,
    chat_id: str = "chat-1",
    turn_count: int = 3,
    attempts: tuple[TaskAttempt, ...] | None = (),
    decisions: DecisionRead | None = None,
    now: datetime = NOW,
    kind: str = "archive",
    completion_id: str = "",
    workspace: str = "ws",
) -> CaptureRecord:
    return build_record(
        workspace=workspace,
        chat_id=chat_id,
        kind=kind,
        completion_id=completion_id,
        turn_count=turn_count,
        attempts=attempts,
        decisions=decisions or DecisionRead("absent", (), 0),
        now=now,
    )


def _raw(record: CaptureRecord) -> dict[str, Any]:
    return {
        "v": 1,
        "id": record.id,
        "kind": record.kind,
        "workspace": record.workspace,
        "chat_id": record.chat_id,
        "captured_at": record.captured_at.isoformat(timespec="seconds"),
        "turn_count": record.turn_count,
        "attempt_ids": list(record.attempt_ids),
        "attempts_status": record.attempts_status,
        "decision_ids": list(record.decision_ids),
        "decisions_status": record.decisions_status,
        "omitted_decisions": record.omitted_decisions,
        "completion_id": record.completion_id,
        "complete": record.complete,
    }


def _write_lines(vault: Path, lines: list[str]) -> None:
    path = captures_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_the_file_is_under_workspace_and_is_jsonl(tmp_path: Path) -> None:
    assert captures_path(tmp_path) == tmp_path / "Workspace" / "Archive-Captures.jsonl"


def test_append_then_read_round_trips_the_record(tmp_path: Path) -> None:
    record = _record(attempts=(_attempt(1),), decisions=_ok(_decision(7, 1)))
    append_capture(tmp_path, record)
    read = read_captures(tmp_path)
    assert read.status == "ok"
    assert read.records == (record,)
    assert read.malformed_lines == 0
    assert read.conflicts == ()


def test_a_written_line_has_exactly_the_contract_keys(tmp_path: Path) -> None:
    append_capture(tmp_path, _record(attempts=(_attempt(1),)))
    (line,) = captures_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert set(json.loads(line)) == EXACT_KEYS


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda row: row.update(extra="nope"), id="extra-key"),
        pytest.param(lambda row: row.pop("complete"), id="missing-key"),
        pytest.param(lambda row: row.update(turn_count="3"), id="string-count"),
        pytest.param(lambda row: row.update(turn_count=True), id="bool-count"),
        pytest.param(lambda row: row.update(captured_at="2026-11-21T10:00:00"), id="naive-time"),
        pytest.param(lambda row: row.update(id="ZZ" * 16), id="bad-id"),
        pytest.param(lambda row: row.update(attempt_ids=["not-hex"]), id="bad-attempt-id"),
        pytest.param(lambda row: row.update(workspace="x" * 129), id="long-workspace"),
    ],
)
def test_a_malformed_row_is_counted_and_the_valid_rows_are_kept(
    tmp_path: Path, mutate: Any
) -> None:
    first = _record(chat_id="chat-a")
    second = _record(chat_id="chat-b")
    bad = _raw(_record(chat_id="chat-c"))
    mutate(bad)
    _write_lines(
        tmp_path,
        [json.dumps(_raw(first)), json.dumps(bad), json.dumps(_raw(second))],
    )
    read = read_captures(tmp_path)
    assert read.status == "malformed"
    assert read.malformed_lines == 1
    assert read.records == (first, second)


def test_an_over_4_kib_line_is_malformed_and_the_valid_rows_are_kept(tmp_path: Path) -> None:
    first = _record(chat_id="chat-a")
    second = _record(chat_id="chat-b")
    padded = json.dumps(_raw(_record(chat_id="chat-c"))) + " " * 4200
    _write_lines(tmp_path, [json.dumps(_raw(first)), padded, json.dumps(_raw(second))])
    read = read_captures(tmp_path)
    assert read.status == "malformed"
    assert read.malformed_lines == 1
    assert read.records == (first, second)


def test_an_absent_file_reads_as_absent(tmp_path: Path) -> None:
    read = read_captures(tmp_path)
    assert (read.status, read.records, read.malformed_lines) == ("absent", (), 0)


def test_a_directory_at_the_path_is_unreadable(tmp_path: Path) -> None:
    captures_path(tmp_path).mkdir(parents=True)
    read = read_captures(tmp_path)
    assert (read.status, read.records) == ("unreadable", ())


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_is_unreadable_and_never_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps(_raw(_record())) + "\n", encoding="utf-8")
    captures_path(tmp_path).parent.mkdir(parents=True)
    captures_path(tmp_path).symlink_to(target)
    read = read_captures(tmp_path)
    assert (read.status, read.records) == ("unreadable", ())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_at_the_path_is_unreadable_and_does_not_block(tmp_path: Path) -> None:
    captures_path(tmp_path).parent.mkdir(parents=True)
    os.mkfifo(captures_path(tmp_path))
    read = read_captures(tmp_path)
    assert (read.status, read.records) == ("unreadable", ())


def test_non_utf8_content_is_unreadable(tmp_path: Path) -> None:
    captures_path(tmp_path).parent.mkdir(parents=True)
    captures_path(tmp_path).write_bytes(b"\xff\xfe\n")
    read = read_captures(tmp_path)
    assert (read.status, read.records) == ("unreadable", ())


def test_the_read_ceiling_is_eight_mebibytes() -> None:
    assert CAPTURES_READ_CEILING == 8 * 1024 * 1024


def test_a_file_over_the_ceiling_is_oversize_with_no_partial_read(tmp_path: Path) -> None:
    append_capture(tmp_path, _record(chat_id="chat-a"))
    path = captures_path(tmp_path)
    path.write_bytes(path.read_bytes() + b" " * CAPTURES_READ_CEILING)
    read = read_captures(tmp_path)
    assert (read.status, read.records, read.malformed_lines) == ("oversize", (), 0)


def test_an_exact_replay_folds_to_one_row(tmp_path: Path) -> None:
    record = _record(attempts=(_attempt(1),))
    append_capture(tmp_path, record)
    append_capture(tmp_path, record)
    read = read_captures(tmp_path)
    assert read.records == (record,)
    assert read.malformed_lines == 0
    assert read.conflicts == ()


def test_a_re_archive_at_a_later_instant_folds_to_the_first_row(tmp_path: Path) -> None:
    first = _record(chat_id="chat-a", now=NOW, attempts=(_attempt(1),))
    again = _record(chat_id="chat-a", now=LATER, attempts=(_attempt(1),))
    other = _record(chat_id="chat-b")
    assert first.id == again.id
    _write_lines(
        tmp_path,
        [json.dumps(_raw(first)), json.dumps(_raw(again)), json.dumps(_raw(other))],
    )
    read = read_captures(tmp_path)
    assert read.conflicts == ()
    assert read.records == (first, other)
    assert read.records[0].captured_at == NOW
    assert read.malformed_lines == 0


def test_the_same_identity_with_a_different_count_is_a_conflict(tmp_path: Path) -> None:
    first = _record(chat_id="chat-a", turn_count=3, now=NOW)
    clash = _record(chat_id="chat-a", turn_count=4, now=LATER)
    other = _record(chat_id="chat-b")
    assert first.id == clash.id
    _write_lines(
        tmp_path,
        [json.dumps(_raw(first)), json.dumps(_raw(clash)), json.dumps(_raw(other))],
    )
    read = read_captures(tmp_path)
    assert read.conflicts == (first.id,)
    assert read.records == (other,)
    assert read.malformed_lines == 0


def test_the_same_identity_with_different_attempt_ids_is_a_conflict(tmp_path: Path) -> None:
    first = _record(chat_id="chat-a", attempts=(_attempt(1),), now=NOW)
    clash = _record(chat_id="chat-a", attempts=(_attempt(2),), now=LATER)
    _write_lines(tmp_path, [json.dumps(_raw(first)), json.dumps(_raw(clash))])
    read = read_captures(tmp_path)
    assert read.conflicts == (first.id,)
    assert read.records == ()


def test_attempts_are_capped_at_the_newest_eight(tmp_path: Path) -> None:
    attempts = tuple(_attempt(n) for n in range(1, 10))  # nine, newest first
    record = _record(attempts=attempts)
    assert record.attempt_ids == tuple(_hex(n) for n in range(1, 9))
    assert record.attempts_status == "ok"


def test_decisions_are_capped_at_the_newest_twenty_after_filtering_by_attempt(
    tmp_path: Path,
) -> None:
    # Eight kept attempts (1..8) plus one dropped attempt (9). The file is
    # append-only, so file order is time order: oldest first.
    attempts = tuple(_attempt(n) for n in range(1, 10))
    matching = [_decision(100 + i, 1 + (i % 8)) for i in range(25)]
    unrelated = [_decision(200 + i, 9) for i in range(6)]
    interleaved: list[DecisionRow] = []
    for index, row in enumerate(matching):
        interleaved.append(row)
        if index < len(unrelated):
            interleaved.append(unrelated[index])
    record = _record(attempts=attempts, decisions=_ok(*interleaved))
    newest_first = [row.id for row in reversed(matching)]
    assert record.decision_ids == tuple(newest_first[:20])
    assert record.omitted_decisions == 5
    assert record.decisions_status == "ok"


def test_a_decision_for_a_dropped_attempt_is_not_counted_as_omitted() -> None:
    attempts = tuple(_attempt(n) for n in range(1, 10))
    record = _record(attempts=attempts, decisions=_ok(_decision(5, 9)))
    assert record.decision_ids == ()
    assert record.omitted_decisions == 0


def test_no_attempts_means_no_decision_ids_and_complete() -> None:
    record = _record(attempts=(), decisions=_ok(_decision(5, 1)))
    assert record.attempt_ids == ()
    assert record.decision_ids == ()
    assert record.complete is True


def test_unreadable_attempts_are_a_status_not_an_exception() -> None:
    record = _record(attempts=None, decisions=_ok(_decision(5, 1)))
    assert record.attempts_status == "unreadable"
    assert record.attempt_ids == ()
    assert record.complete is False


@pytest.mark.parametrize(
    "decisions",
    [
        pytest.param(DecisionRead("malformed", (), 1), id="malformed"),
        pytest.param(DecisionRead("unreadable", (), 0), id="unreadable"),
        pytest.param(DecisionRead("oversize", (), 0), id="oversize"),
    ],
)
def test_a_degraded_decision_read_is_incomplete(decisions: DecisionRead) -> None:
    record = _record(attempts=(_attempt(1),), decisions=decisions)
    assert record.decisions_status == decisions.status
    assert record.complete is False


def test_an_absent_decision_file_is_complete_when_nothing_is_omitted() -> None:
    record = _record(attempts=(_attempt(1),), decisions=DecisionRead("absent", (), 0))
    assert record.decisions_status == "absent"
    assert record.complete is True


def test_omitted_decisions_make_the_record_incomplete() -> None:
    rows = [_decision(100 + i, 1) for i in range(21)]
    record = _record(attempts=(_attempt(1),), decisions=_ok(*rows))
    assert record.omitted_decisions == 1
    assert record.complete is False


def test_dropped_attempts_make_the_record_incomplete() -> None:
    attempts = tuple(_attempt(n) for n in range(1, 10))
    record = _record(attempts=attempts, decisions=_ok())
    assert record.complete is False


def test_a_completion_record_carries_its_completion_id() -> None:
    completion = _hex(0xC0FFEE)
    record = _record(kind="completion", completion_id=completion, attempts=(_attempt(1),))
    assert record.kind == "completion"
    assert record.completion_id == completion


def test_a_completion_record_needs_a_valid_completion_id() -> None:
    with pytest.raises(ValueError):
        _record(kind="completion", completion_id="")
    with pytest.raises(ValueError):
        _record(kind="completion", completion_id="not-hex")


def test_an_archive_record_refuses_a_completion_id() -> None:
    with pytest.raises(ValueError):
        _record(kind="archive", completion_id=_hex(1))


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        _record(kind="sighting")


def test_a_naive_capture_time_is_refused() -> None:
    with pytest.raises(ValueError):
        _record(now=datetime(2026, 11, 21, 10, 0, 0))


def test_a_non_utc_capture_time_is_stored_as_utc() -> None:
    offset = timezone(timedelta(hours=2))
    record = _record(now=datetime(2026, 11, 21, 12, 0, 0, tzinfo=offset))
    assert record.captured_at == NOW
    assert record.captured_at.utcoffset() == timedelta(0)


def test_a_serialized_line_has_no_free_text_and_no_long_string(tmp_path: Path) -> None:
    attempts = tuple(_attempt(n, chat_id="c" * 128) for n in range(1, 10))
    rows = [_decision(100 + i, 1) for i in range(25)]
    record = _record(
        chat_id="c" * 128,
        workspace="w" * 128,
        attempts=attempts,
        decisions=_ok(*rows),
    )
    append_capture(tmp_path, record)
    (line,) = captures_path(tmp_path).read_text(encoding="utf-8").splitlines()
    parsed = json.loads(line)
    assert set(parsed) == EXACT_KEYS
    assert "free text" not in line
    for value in parsed.values():
        if isinstance(value, str):
            assert len(value) <= 128
        if isinstance(value, list):
            assert all(isinstance(item, str) and len(item) == 32 for item in value)
    assert len(line.encode("utf-8")) <= 4 * 1024


def test_the_capture_id_is_deterministic_and_hex() -> None:
    first = capture_id("ws", "chat-1", "archive", "")
    assert first == capture_id("ws", "chat-1", "archive", "")
    assert re.fullmatch(r"[0-9a-f]{32}", first)


def test_the_capture_id_differs_by_each_identity_part() -> None:
    base = capture_id("ws", "chat-1", "completion", _hex(9))
    variants = {
        capture_id("other", "chat-1", "completion", _hex(9)),
        capture_id("ws", "chat-2", "completion", _hex(9)),
        capture_id("ws", "chat-1", "archive", ""),
        capture_id("ws", "chat-1", "completion", _hex(8)),
    }
    assert base not in variants
    assert len(variants) == 4


def test_the_capture_id_is_the_documented_hash() -> None:
    expected = hashlib.sha256("ws\x00chat-1\x00archive\x00".encode("utf-8")).hexdigest()[:32]
    assert capture_id("ws", "chat-1", "archive", "") == expected


def test_append_refuses_a_symlinked_file(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("symlinks need privileges on Windows")
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep\n", encoding="utf-8")
    captures_path(tmp_path).parent.mkdir(parents=True)
    captures_path(tmp_path).symlink_to(target)
    with pytest.raises(OSError):
        append_capture(tmp_path, _record())
    assert target.read_text(encoding="utf-8") == "keep\n"
