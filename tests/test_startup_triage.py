from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import ciao.startup_triage as startup_triage
from ciao.startup_triage import (
    TRIAGE_COOLDOWN_S,
    TRIAGE_MARKER_NAME,
    build_triage_entry,
    cap_service_logs,
    run_startup_triage,
)


class FakePCM:
    def __init__(self) -> None:
        self._projects = {
            "proj-general": SimpleNamespace(
                project_id="proj-general", name="General", workspace="personal"
            ),
            "proj-other": SimpleNamespace(
                project_id="proj-other", name="Research", workspace="personal"
            ),
        }
        self.dispatched: list[tuple] = []

    def _workspace_names(self):
        return ["personal"]

    async def dispatch_schedule(self, entry, prompt, model, mode, provider, **kwargs):
        self.dispatched.append((entry, prompt, model, mode, provider))
        return {}


def _config(tmp_path: Path) -> SimpleNamespace:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        state_path=runtime / "state.json",
        workspace_root=tmp_path,
    )


def _resolve(entry):
    return ("claude", "sonnet", "auto", "claude")


def _issue_report(errors: int, failed: int) -> dict:
    return {
        "error_line_count": errors,
        "failed_jobs": [{"job": f"job{i}"} for i in range(failed)],
        "report_text": "report",
    }


def test_cap_service_logs_keeps_recent_tail(tmp_path: Path) -> None:
    big = tmp_path / "ciao.stdout.log"
    lines = "".join(f"line {i:04d}\n" for i in range(200))
    big.write_text(lines, encoding="utf-8")
    small = tmp_path / "ciao.stderr.log"
    small.write_text("tiny\n", encoding="utf-8")

    capped = cap_service_logs(tmp_path, max_bytes=500, keep_bytes=100)

    assert capped == ["ciao.stdout.log"]
    content = big.read_text(encoding="utf-8")
    assert content.startswith("[log truncated by ciaobot")
    # The most recent line survives and the kept tail starts on a boundary.
    assert content.endswith("line 0199\n")
    assert "\nline " in content
    assert big.stat().st_size < 500
    assert small.read_text(encoding="utf-8") == "tiny\n"


@pytest.mark.asyncio
async def test_no_errors_means_no_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.debug_report.build_issue_report", lambda root: _issue_report(0, 0)
    )
    pcm = FakePCM()

    assert await run_startup_triage(pcm, _config(tmp_path), _resolve) is False
    assert pcm.dispatched == []


@pytest.mark.asyncio
async def test_errors_dispatch_triage_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.debug_report.build_issue_report", lambda root: _issue_report(5, 2)
    )
    pcm = FakePCM()
    config = _config(tmp_path)

    assert await run_startup_triage(pcm, config, _resolve) is True

    (entry, prompt, model, mode, provider) = pcm.dispatched[0]
    assert entry.web_project_id == "proj-general"
    assert entry.workspace == "personal"
    assert entry.scope == "system"
    # The pipeline substitutes the report and clears the log after a clean run.
    assert "{{ISSUE_REPORT}}" in prompt
    assert "raffaelefarinaro/ciaobot" in prompt
    assert (model, mode, provider) == ("sonnet", "auto", "claude")

    marker = json.loads(
        (config.state_path.parent / TRIAGE_MARKER_NAME).read_text(encoding="utf-8")
    )
    assert marker["last_dispatched_at"]


@pytest.mark.asyncio
async def test_cooldown_blocks_repeat_triage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.debug_report.build_issue_report", lambda root: _issue_report(1, 0)
    )
    pcm = FakePCM()
    config = _config(tmp_path)

    assert await run_startup_triage(pcm, config, _resolve) is True
    assert await run_startup_triage(pcm, config, _resolve) is False
    assert len(pcm.dispatched) == 1

    # An expired cooldown allows the next sweep.
    stale = datetime.now(UTC) - timedelta(seconds=TRIAGE_COOLDOWN_S + 60)
    (config.state_path.parent / TRIAGE_MARKER_NAME).write_text(
        json.dumps({"last_dispatched_at": stale.isoformat()}), encoding="utf-8"
    )
    assert await run_startup_triage(pcm, config, _resolve) is True
    assert len(pcm.dispatched) == 2


def test_build_triage_entry_is_one_off_and_system_scoped() -> None:
    entry = build_triage_entry(workspace="personal", web_project_id="proj-general")
    assert entry.frequency == "manual"
    assert entry.scope == "system"
    assert entry.editable is False and entry.removable is False
    assert "{{ISSUE_REPORT}}" in entry.prompt
