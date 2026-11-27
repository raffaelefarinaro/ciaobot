"""Workspace care report: the parser and `GET /api/care-report`.

The report is the checklist the nightly care run writes into
`<vault>/Workspace/Care-Report.md`. The reader is read-only, bounded and never
raises: every state the Home card and the report page show comes from here.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ciao import care_report
from ciao.care_report import MAX_REPORT_BYTES, STEP_FILES, parse_care_report, read_care_report
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from tests.session import signed_in

COMPLETE = """# Care report 2026-11-26
Weekly done: 2026-11-26
- [x] unsure: apply ticked Unsure lines (2 ticked) \u2014 applied 2, left 0 ticked; verify: 0 `- [x]` lines left
- [x] regions: consolidate regions (memory 91%) \u2014 memory 91% -> 72%; verify: memory.pct 72, profile.pct 40
"""

BLOCKED = """# Care report 2026-11-26
Weekly done: 2026-11-19
Interrupted last run: guide-review, stale
- [x] unsure: apply ticked Unsure lines \u2014 applied 2; verify: 0 left
- [!] hygiene: run the vault index \u2014 blocked: cannot write INDEX.md
- [!] guide-review: review the AGENTS.md body (weekly due) \u2014 blocked: AGENTS.md unreadable

## What needs you
1. **Hygiene is blocked**: the vault index command exited 2.
2. Cross-project fact waiting for approval: "Invoices go out on the 1st." Remember it
   for every project? It is on the Unsure list.
3. The memory region is at 91% of its limit.
"""

INTERRUPTED = """# Care report 2026-11-26
Weekly done: 2026-11-19
- [x] unsure: apply ticked lines \u2014 applied 1; verify: 0 left
- [x] regions: consolidate \u2014 done; verify: pct 70
- [ ] learnings: promote entries (3 at x3)
- [ ] stale: re-check stale notes (weekly due)
"""


def test_complete_report_reads_every_field() -> None:
    body = parse_care_report(COMPLETE)
    assert body["updated"] == "2026-11-26"
    assert body["weekly_done"] == "2026-11-26"
    assert body["interrupted_ids"] == []
    assert body["counts"] == {"total": 2, "done": 2, "blocked": 0, "open": 0}
    first = body["steps"][0]
    assert first == {
        "id": "unsure",
        "state": "done",
        "what": "apply ticked Unsure lines (2 ticked)",
        "evidence": "applied 2, left 0 ticked",
        "verify": "0 `- [x]` lines left",
    }
    assert body["needs_you"] == []
    assert body["malformed"] is False
    assert body["nothing_due"] is False


def test_blocked_report_reads_reasons_interrupted_ids_and_needs_you() -> None:
    body = parse_care_report(BLOCKED)
    assert body["interrupted_ids"] == ["guide-review", "stale"]
    assert body["counts"] == {"total": 3, "done": 1, "blocked": 2, "open": 0}
    hygiene = body["steps"][1]
    assert hygiene["state"] == "blocked"
    assert hygiene["evidence"] == "cannot write INDEX.md"
    assert hygiene["verify"] == ""
    needs = body["needs_you"]
    assert [n["title"] for n in needs] == [
        "Hygiene is blocked",
        "Cross-project fact waiting for approval",
        "The memory region is at 91% of its limit.",
    ]
    # A continuation line belongs to the item above it.
    assert needs[1]["detail"].endswith("It is on the Unsure list.")
    assert [n["action"] for n in needs] == ["report", "unsure", "guide"]


def test_open_lines_are_open_steps() -> None:
    body = parse_care_report(INTERRUPTED)
    assert body["counts"] == {"total": 4, "done": 2, "blocked": 0, "open": 2}
    assert body["steps"][2] == {
        "id": "learnings", "state": "open", "what": "promote entries (3 at x3)", "evidence": "", "verify": "",
    }


def test_nothing_due_is_its_own_flag() -> None:
    body = parse_care_report("# Care report 2026-11-26\nWeekly done: 2026-11-20\nNothing due\n")
    assert body["nothing_due"] is True
    assert body["counts"]["total"] == 0
    assert body["malformed"] is False


@pytest.mark.parametrize("text", ["", "hello world\nno checklist here", "\x00\x01 binary", "- [?] odd: line"])
def test_unrecognised_text_is_malformed_not_an_error(text: str) -> None:
    body = parse_care_report(text)
    assert body["malformed"] is True
    assert body["steps"] == []


def test_ascii_separators_and_stray_lines_are_tolerated() -> None:
    body = parse_care_report(
        "2026-11-26\nsome prose\n* [X] Learnings: promote -- promoted 2; verify: active 7\n- [!] rules: digest\n"
    )
    assert body["updated"] == "2026-11-26"
    assert body["steps"][0]["id"] == "learnings"
    assert body["steps"][0]["evidence"] == "promoted 2"
    # A blocked line without a reason still reads as blocked, with nothing invented.
    assert body["steps"][1] == {"id": "rules", "state": "blocked", "what": "digest", "evidence": "", "verify": ""}


def test_counts_and_fields_are_bounded() -> None:
    lines = "\n".join(f"- [x] s{i}: {'w' * 2000} \u2014 e; verify: v" for i in range(200))
    needs = "\n".join(f"{i}. item {i}" for i in range(1, 100))
    body = parse_care_report(f"# Care report 2026-11-26\n{lines}\n## What needs you\n{needs}\n")
    assert len(body["steps"]) == care_report.MAX_STEPS
    assert len(body["steps"][0]["what"]) == care_report.MAX_FIELD_CHARS
    assert len(body["needs_you"]) == care_report.MAX_NEEDS


def _write_report(vault: Path, text: str) -> Path:
    path = vault / "Workspace" / "Care-Report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_report_is_never_run(tmp_path: Path) -> None:
    body = read_care_report(tmp_path)
    assert body["status"] == "missing"
    assert body["steps"] == [] and body["modified_at"] == "" and body["malformed"] is False


def test_oversize_report_is_refused_without_parsing(tmp_path: Path) -> None:
    _write_report(tmp_path, "- [x] unsure: a \u2014 b\n" * (MAX_REPORT_BYTES // 10))
    body = read_care_report(tmp_path)
    assert body["status"] == "too_large"
    assert body["steps"] == []


def test_symlink_out_of_the_vault_is_not_followed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    secret = tmp_path / "elsewhere.md"
    secret.write_text(COMPLETE, encoding="utf-8")
    (vault / "Workspace" / "Care-Report.md").symlink_to(secret)
    body = read_care_report(vault)
    assert body["status"] == "unreadable"
    assert body["steps"] == []


def test_a_directory_at_the_report_path_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "Workspace" / "Care-Report.md").mkdir(parents=True)
    assert read_care_report(tmp_path)["status"] == "unreadable"


def test_invalid_utf8_is_replaced_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "Workspace" / "Care-Report.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Care report 2026-11-26\n- [x] unsure: a \xe2\x80 \xff \xe2\x80\x94 ok; verify: v\n")
    body = read_care_report(tmp_path)
    assert body["status"] == "ok"
    assert body["steps"][0]["id"] == "unsure"


def test_step_files_match_the_care_prompt() -> None:
    """The report page links each step to the file the prompt dispatches it with."""
    schedules = json.loads((Path(care_report.__file__).parent / "stock" / "schedules.json").read_text())
    prompt = next(s["prompt"] for s in schedules["schedules"] if s["schedule_id"] == "system-memory-curation")
    files_line = next(line for line in prompt.splitlines() if line.startswith("Files: "))
    pairs = dict(re.findall(r"([a-z-]+) `([a-z0-9-]+\.md)`", files_line))
    assert pairs == STEP_FILES


def test_weekly_done_advances_only_when_every_weekly_line_is_done() -> None:
    schedules = json.loads((Path(care_report.__file__).parent / "stock" / "schedules.json").read_text())
    prompt = next(s["prompt"] for s in schedules["schedules"] if s["schedule_id"] == "system-memory-curation")
    assert "every weekly line is `[x]` or `[!]`" not in prompt
    assert "every weekly line is `[x]`; a `[!]` weekly line leaves it unchanged" in prompt


# ── GET /api/care-report ───────────────────────────────────────────────────


class _Schedules:
    """The schedule manager's read the route uses, with one care entry."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries
        self.asked: list[str] = []

    def get_with_revision(self, schedule_id: str):
        self.asked.append(schedule_id)
        entry = self.entries.get(schedule_id)
        return (entry, "rev") if entry is not None else None


def _entry(status: str = "ok", dispatched: str = "2026-11-26T00:01:00+00:00", enabled: bool = True):
    return SimpleNamespace(
        schedule_id="system-memory-curation@personal",
        enabled=enabled,
        last_status=status,
        last_dispatched_at=dispatched,
    )


@pytest.fixture
def setup(tmp_path: Path):
    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    app = create_app(cfg)
    schedules = _Schedules({})
    app.state.schedule_manager = schedules
    client = signed_in(TestClient(app))
    return SimpleNamespace(client=client, vault=tmp_path / "memory-vault" / "personal", schedules=schedules, root=tmp_path)


def test_route_reads_the_active_workspace_report(setup) -> None:
    _write_report(setup.vault, BLOCKED)
    _write_report(setup.root / "memory-vault" / "work", COMPLETE)
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["status"] == "ok"
    assert body["workspace"] == "personal"
    assert body["counts"]["blocked"] == 2
    assert body["report_path"] == "memory-vault/personal/Workspace/Care-Report.md"
    assert body["unsure_path"] == "memory-vault/personal/Workspace/Unsure.md"
    assert body["modified_at"].endswith("Z")
    hygiene = body["steps"][1]
    assert hygiene["step_file"].endswith("/references/curation-pass-6-hygiene.md")
    assert Path(hygiene["step_file"]).is_file()
    assert body["schedule"] is None and body["running"] is None
    assert setup.schedules.asked == ["system-memory-curation@personal"]


def test_route_says_missing_for_a_workspace_care_never_ran_in(setup) -> None:
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["status"] == "missing"
    assert body["schedule"] == {
        "id": "system-memory-curation@personal", "enabled": True, "last_status": "", "last_run_at": "2026-11-26T00:01:00+00:00",
    }


@pytest.mark.parametrize("query", ["", "?workspace=", "?workspace=nope", "?workspace=../personal"])
def test_route_refuses_an_unknown_workspace(setup, query: str) -> None:
    response = setup.client.get(f"/api/care-report{query}")
    assert response.status_code == 400


def test_route_is_read_only(setup) -> None:
    path = _write_report(setup.vault, INTERRUPTED)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert setup.client.get("/api/care-report?workspace=personal").status_code == 200
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert setup.client.post("/api/care-report?workspace=personal").status_code == 405


def test_running_names_the_open_step_of_a_report_written_after_dispatch(setup) -> None:
    path = _write_report(setup.vault, INTERRUPTED)
    stamp = datetime(2026, 11, 26, 0, 5, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="running", dispatched="2026-11-26T00:01:00+00:00")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["running"] == {"since": "2026-11-26T00:01:00+00:00", "step": "learnings", "index": 3, "total": 4}


def test_running_ignores_the_previous_runs_report(setup) -> None:
    """Before the new run rewrites the checklist, the open lines on disk are the old run's."""
    path = _write_report(setup.vault, INTERRUPTED)
    stamp = datetime(2026, 11, 25, 0, 5, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="running", dispatched="2026-11-26T00:01:00+00:00")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["running"] == {"since": "2026-11-26T00:01:00+00:00", "step": "", "index": 0, "total": 0}


def test_route_needs_a_session(setup) -> None:
    anonymous = TestClient(setup.client.app)
    assert anonymous.get("/api/care-report?workspace=personal").status_code in (401, 403)
