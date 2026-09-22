from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import ciao.startup_triage as startup_triage
from ciao import job_runs
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


def _record_failure(ended: datetime, job: str = "background_run") -> None:
    job_runs.record_run(job_runs.JobRun(
        job=job, label="Background command run",
        started_at=ended.isoformat(), ended_at=ended.isoformat(),
        status="error", error="one-off boom",
    ))


def _seed_issues(
    config: SimpleNamespace, *, error_lines: int = 0, failures: int = 0
) -> None:
    """Put real runtime issues where ``build_issue_report`` looks for them.

    These tests used to hand ``run_startup_triage`` a hand-rolled report dict
    whose error count lived under ``error_line_count`` — a key
    ``build_issue_report`` has never emitted (it emits ``error_log_lines``).
    The fixture and the gate agreed with each other and both disagreed with
    the producer, so the error-log half of the gate was dead in production
    while the suite stayed green. Seeding the real error log and run records
    and letting the real producer build the report exercises the contract
    instead of restating it.
    """
    runtime = Path(config.state_path).parent
    runtime.mkdir(parents=True, exist_ok=True)
    if error_lines:
        (runtime / "server_errors.log").write_text(
            "".join(
                f"2026-09-20 12:00:{i:02d} ERROR ciao.web: boom {i}\n"
                for i in range(error_lines)
            ),
            encoding="utf-8",
        )
    now = datetime.now(UTC)
    for i in range(failures):
        _record_failure(now - timedelta(minutes=i + 1), job=f"job{i}")


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
async def test_no_errors_means_no_chat(tmp_path: Path) -> None:
    pcm = FakePCM()

    assert await run_startup_triage(pcm, _config(tmp_path), _resolve) is False
    assert pcm.dispatched == []


@pytest.mark.asyncio
async def test_error_log_alone_dispatches_triage_chat(tmp_path: Path, caplog) -> None:
    """Error-log lines must trigger a triage even with no failed job run.

    The gate read ``error_line_count``, which ``build_issue_report`` never
    emits, so this half of it always evaluated falsy: a boot whose
    ``server_errors.log`` held real tracebacks opened no triage chat unless a
    background job happened to fail in the same window, and the log is only
    cleared by a clean triage, so the errors accumulated forever.
    """
    pcm = FakePCM()
    config = _config(tmp_path)
    _seed_issues(config, error_lines=4)

    with caplog.at_level(logging.INFO, logger="ciao.startup_triage"):
        assert await run_startup_triage(pcm, config, _resolve) is True
    assert len(pcm.dispatched) == 1
    # The operator reads these counts; the gate no longer supplies them, so
    # they are asserted separately from the dispatch decision.
    assert "4 error line(s) and 0 failed job run(s)" in caplog.text


@pytest.mark.asyncio
async def test_debug_log_alone_does_not_dispatch(tmp_path: Path) -> None:
    """The verbose debug log is ambient output, not an issue."""
    pcm = FakePCM()
    config = _config(tmp_path)
    runtime = Path(config.state_path).parent
    (runtime / "server_debug.log").write_text(
        "2026-09-20 12:00:00 DEBUG ciao.web: chatty\n", encoding="utf-8"
    )

    assert await run_startup_triage(pcm, config, _resolve) is False
    assert pcm.dispatched == []


@pytest.mark.asyncio
async def test_errors_dispatch_triage_chat(tmp_path: Path) -> None:
    pcm = FakePCM()
    config = _config(tmp_path)
    _seed_issues(config, error_lines=5, failures=2)

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
async def test_cooldown_blocks_repeat_triage(tmp_path: Path) -> None:
    pcm = FakePCM()
    config = _config(tmp_path)
    _seed_issues(config, error_lines=1)

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


def _write_marker(config: SimpleNamespace, when: datetime) -> None:
    (config.state_path.parent / TRIAGE_MARKER_NAME).write_text(
        json.dumps({"last_dispatched_at": when.isoformat()}), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_failure_older_than_last_triage_is_not_retriaged(
    tmp_path: Path,
) -> None:
    """The run log is append-only, so before the acknowledgement floor a
    single old failure re-opened a triage chat on every boot past the
    cooldown, forever."""
    config = _config(tmp_path)
    now = datetime.now(UTC)
    _write_marker(config, now - timedelta(seconds=TRIAGE_COOLDOWN_S + 3600))
    _record_failure(now - timedelta(hours=20))
    pcm = FakePCM()

    assert await run_startup_triage(pcm, config, _resolve) is False
    assert pcm.dispatched == []


@pytest.mark.asyncio
async def test_failure_after_last_triage_still_dispatches(tmp_path: Path) -> None:
    """The floor must not swallow a genuinely new failure."""
    config = _config(tmp_path)
    now = datetime.now(UTC)
    _write_marker(config, now - timedelta(seconds=TRIAGE_COOLDOWN_S + 3600))
    _record_failure(now - timedelta(minutes=5))
    pcm = FakePCM()

    assert await run_startup_triage(pcm, config, _resolve) is True
    assert len(pcm.dispatched) == 1


def test_build_triage_entry_is_one_off_and_system_scoped() -> None:
    entry = build_triage_entry(workspace="personal", web_project_id="proj-general")
    assert entry.frequency == "manual"
    assert entry.scope == "system"
    assert entry.editable is False and entry.removable is False
    # Nothing needing attention archives itself instead of lingering in General.
    assert entry.archive_policy == "auto"
    assert "{{ISSUE_REPORT}}" in entry.prompt
