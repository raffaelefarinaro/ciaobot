"""The workspace-scoped log of operator task decisions (#1726)."""

from __future__ import annotations

import json
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ciao.task_decisions import (
    MAX_REASON,
    append_decision,
    clean_reason,
    decisions_path,
    read_decisions,
)


def _append(vault: Path, **overrides: str) -> dict[str, object]:
    fields = {
        "kind": "delete",
        "task_id": "t1",
        "task_title": "Title",
        "task_created_by": "agent",
        "project_id": "p1",
        "attempt_id": "a1",
        "attempt_state": "ready_for_review",
        "actor": "user",
        "via": "pwa",
        "reason": "not mine",
    }
    fields.update(overrides)
    return append_decision(vault, **fields)  # type: ignore[arg-type]


def test_append_then_read_round_trips_every_field(tmp_path: Path) -> None:
    row = _append(tmp_path)
    rows = read_decisions(tmp_path)
    assert rows == [row]
    assert set(row) == {
        "v", "id", "at", "kind", "task_id", "task_title", "task_created_by",
        "project_id", "attempt_id", "attempt_state", "actor", "via", "reason",
    }
    assert row["v"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", str(row["id"]))
    parsed = datetime.fromisoformat(str(row["at"]))
    assert parsed.utcoffset() == UTC.utcoffset(None)


def test_rows_append_in_order_and_survive_a_corrupt_line(tmp_path: Path) -> None:
    first = _append(tmp_path, task_id="one")
    path = decisions_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"not json\n\n")
    second = _append(tmp_path, task_id="two")
    assert read_decisions(tmp_path) == [first, second]


def test_read_decisions_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_decisions(tmp_path) == []


def test_the_file_is_under_workspace_and_is_not_markdown(tmp_path: Path) -> None:
    path = decisions_path(tmp_path)
    assert path.relative_to(tmp_path).as_posix() == "Workspace/Task-Decisions.jsonl"
    assert path.suffix != ".md"


def test_clean_reason_none_blank_strip_cap_and_non_string() -> None:
    assert clean_reason(None) == ""
    assert clean_reason("   ") == ""
    assert clean_reason("  x ") == "x"
    assert clean_reason("x" * MAX_REASON) == "x" * MAX_REASON
    with pytest.raises(ValueError):
        clean_reason("x" * (MAX_REASON + 1))
    with pytest.raises(ValueError):
        clean_reason(5)
    with pytest.raises(ValueError):
        clean_reason({"a": 1})


def test_a_reason_with_newlines_and_quotes_stays_one_row(tmp_path: Path) -> None:
    _append(tmp_path, reason='line one\n{"kind": "forged"}\n"')
    lines = decisions_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "delete"


def test_complete_is_a_valid_kind(tmp_path: Path) -> None:
    row = _append(tmp_path, kind="complete", actor="agent", via="cli", reason="shipped")
    assert row["kind"] == "complete"
    assert [r["kind"] for r in read_decisions(tmp_path)] == ["complete"]


def test_append_refuses_an_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _append(tmp_path, kind="archive")
    with pytest.raises(ValueError):
        _append(tmp_path, actor="root")
    with pytest.raises(ValueError):
        _append(tmp_path, via="mail")
    assert not decisions_path(tmp_path).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_new_file_is_private(tmp_path: Path) -> None:
    _append(tmp_path)
    assert stat.S_IMODE(decisions_path(tmp_path).stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\x0b", "\x0c"])
def test_a_reason_with_unicode_line_separators_round_trips(tmp_path: Path, separator: str) -> None:
    row = _append(tmp_path, reason=f"before{separator}after")
    assert read_decisions(tmp_path) == [row]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_append_refuses_a_symlinked_decisions_file(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep\n", encoding="utf-8")
    path = decisions_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with pytest.raises(OSError):
        _append(tmp_path)
    assert target.read_text(encoding="utf-8") == "keep\n"
