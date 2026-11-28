"""Unit tests for the runtime issue report (ciao.debug_report)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao import job_runs
from ciao.debug_report import (
    SCOPED_ENGINE_LOG_OMISSION_NOTE,
    SCOPED_LOG_OMISSION_REASON,
    SCOPED_REPORT_MAX_CHARS,
    SCOPED_REPORT_TRUNCATION_MARKER,
    build_issue_report,
    build_workspace_issue_report,
    format_issue_report,
    format_workspace_issue_report,
    recent_job_failures,
)
from ciao.job_runs import JobRun
from ciao.startup_triage import TRIAGE_SCHEDULE_ID


def _ago(**delta: float) -> str:
    """An ISO timestamp relative to now.

    Fixed calendar dates drift past the age cutoff in
    :func:`recent_job_failures` as real time passes, which would turn these
    tests green for the wrong reason.
    """
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


def _write_error_log(workspace: Path, text: str) -> None:
    runtime = workspace / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "server_errors.log").write_text(text, encoding="utf-8")


def test_empty_report_has_no_issues(tmp_path: Path) -> None:
    report = build_issue_report(tmp_path)
    assert report["has_issues"] is False
    assert report["failed_jobs"] == []
    assert report["error_log_records"] == 0
    assert report["report_text"] == "(no runtime issues logged)"


def test_report_includes_error_log_and_failed_jobs(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "2026-07-06 ERROR ciao.web: boom\n")
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
        status="error", error="stream exploded",
    ))
    job_runs.record_run(JobRun(
        job="title", label="Title generation",
        started_at=_ago(hours=1),
        ended_at=_ago(hours=1),
        status="ok",
    ))

    report = build_issue_report(tmp_path)

    assert report["has_issues"] is True
    assert report["error_log_records"] == 1
    assert [f["job"] for f in report["failed_jobs"]] == ["schedule_dispatch"]
    assert "stream exploded" in report["report_text"]
    assert "boom" in report["report_text"]
    assert Path(report["error_log_path"]).as_posix().endswith(".runtime/server_errors.log")


def test_failures_sorted_newest_first_and_capped(tmp_path: Path) -> None:
    for hours in (3, 1, 2):
        job_runs.record_run(JobRun(
            job="vault_index", label="Vault index refresh",
            started_at=_ago(hours=hours),
            ended_at=_ago(hours=hours),
            status="error", error=f"fail {hours}h ago",
        ))
    failures = recent_job_failures(limit=2)
    assert [f["error"] for f in failures] == ["fail 1h ago", "fail 2h ago"]


def test_stale_failures_are_dropped(tmp_path: Path) -> None:
    """A one-off failure must stop being reported once it is old enough.

    ``job_runs_latest.json`` keeps a job's last run forever, so before the age
    cutoff a single failure from weeks ago re-opened a triage chat on every
    boot past the 12h cooldown.
    """
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        started_at=_ago(days=19), ended_at=_ago(days=19),
        status="error", error="OneShotError: Model not found",
    ))
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        started_at=_ago(hours=4), ended_at=_ago(hours=4),
        status="error", error="timed out",
    ))

    errors = [f["error"] for f in recent_job_failures()]

    assert errors == ["timed out"]


def test_undatable_failure_is_dropped(tmp_path: Path) -> None:
    """A run with no usable timestamp can never age out, so it would be
    re-reported on every boot forever."""
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        status="error", error="undated boom",
    ))

    assert recent_job_failures() == []


def test_failures_before_since_floor_are_dropped(tmp_path: Path) -> None:
    """The startup triage acknowledges what it already dispatched by passing
    its last dispatch time; failures it already reported must not come back."""
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=30), ended_at=_ago(hours=30),
        status="error", error="already triaged",
    ))
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        started_at=_ago(hours=2), ended_at=_ago(hours=2),
        status="error", error="fresh failure",
    ))

    since = datetime.now(UTC) - timedelta(hours=24)
    errors = [f["error"] for f in recent_job_failures(since=since)]

    assert errors == ["fresh failure"]


def test_report_forwards_the_since_floor(tmp_path: Path) -> None:
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=30), ended_at=_ago(hours=30),
        status="error", error="already triaged",
    ))

    report = build_issue_report(
        tmp_path, failures_since=datetime.now(UTC) - timedelta(hours=24)
    )

    assert report["failed_jobs"] == []
    assert report["has_issues"] is False


def _record_triage_and_real_failure() -> None:
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=3),
        ended_at=_ago(hours=3),
        status="error", error="## Triage Summary — nothing to file",
        extra={"schedule_id": TRIAGE_SCHEDULE_ID, "chat_id": "chat-abc"},
    ))
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
        status="error", error="real dispatch failure",
        extra={"schedule_id": "sched-real", "chat_id": "chat-xyz"},
    ))


def test_schedule_failures_included_by_default(tmp_path: Path) -> None:
    """The shared aggregator surfaces a broken triage run to the human debug
    report — exclusion is opt-in, not the default."""
    _record_triage_and_real_failure()
    errors = [f["error"] for f in recent_job_failures()]
    assert "real dispatch failure" in errors
    assert any("Triage Summary" in e for e in errors)


def test_triage_own_run_excluded_when_requested(tmp_path: Path) -> None:
    """Triage-dispatch callers pass their own schedule id so the triage never
    re-triages its own recorded summary, while other failures still show."""
    _record_triage_and_real_failure()
    failures = recent_job_failures(exclude_schedule_ids={TRIAGE_SCHEDULE_ID})
    errors = [f["error"] for f in failures]
    assert "real dispatch failure" in errors
    assert all("Triage Summary" not in e for e in errors)


def test_a_retired_job_is_not_re_triaged_from_its_old_runs(
    tmp_path: Path,
) -> None:
    """Retiring a job is what stops its history from resurfacing.

    `job_runs.jsonl` is append-only, so an install that ran the weekly
    skill-evolution pass still holds its failure rows. Once the job is retired
    they are not collected, and the report never opens a triage chat about a
    producer that no longer exists — which is also why the pre-fix
    `no-proposal` recognizer could be deleted along with it.
    """
    job_runs.record_run(JobRun(
        job="skill_evolution", label="skillevo:small-skill:has_proposal",
        started_at=_ago(hours=3),
        ended_at=_ago(hours=3),
        status="error", error="no-proposal",
        extra={
            "dag": "skillevo:small-skill",
            "node_id": "has_proposal",
            "kind": "gate",
        },
    ))
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
        status="error", error="could not write",
    ))

    failures = recent_job_failures()

    assert [failure["error"] for failure in failures] == ["could not write"]
    # The rows are still on disk; only the reporting changed.
    assert "skill_evolution" in job_runs.load_runs(keep_retired=True)


def test_format_report_only_errors_no_jobs(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "ERROR a\nERROR b\n")
    report = build_issue_report(tmp_path)
    text = format_issue_report(report)
    assert "Server error log tail" in text
    assert "Failed background jobs" not in text


def test_debug_log_included_when_present(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "server_debug.log").write_text(
        "2026-08-22 DEBUG ciao.providers.claude: stderr noise\n",
        encoding="utf-8",
    )

    report = build_issue_report(tmp_path)

    # Ambient verbose output is never an issue by itself...
    assert report["has_issues"] is False
    assert report["report_text"] == "(no runtime issues logged)"
    # ...but the raw tail is always in the payload for inspection.
    assert report["debug_log_lines"] == 1
    assert Path(report["debug_log_path"]).as_posix().endswith(".runtime/server_debug.log")
    assert "stderr noise" in report["debug_log"]


def test_debug_log_rendered_alongside_real_issues(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "ERROR boom\n")
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=1),
        ended_at=_ago(hours=1),
        status="error", error="stream exploded",
    ))
    runtime = tmp_path / ".runtime"
    (runtime / "server_debug.log").write_text(
        "DEBUG provider handshake failed\n", encoding="utf-8"
    )

    report = build_issue_report(tmp_path)

    assert report["has_issues"] is True
    text = report["report_text"]
    assert "stream exploded" in text
    assert "Debug log tail" in text
    assert "provider handshake failed" in text


def test_debug_log_absent_is_empty(tmp_path: Path) -> None:
    report = build_issue_report(tmp_path)
    assert report["debug_log"] == ""
    assert report["debug_log_lines"] == 0
    assert "Debug log tail" not in report["report_text"]


def test_recent_job_failures_require_exact_recorded_workspace() -> None:
    """A run's workspace is its captured execution owner, never inferred."""
    cases = [
        ("own", {"workspace": "alpha"}),
        ("foreign", {"workspace": "beta"}),
        ("missing", {}),
        ("empty", {"workspace": ""}),
        ("invalid", {"workspace": 7}),
        ("similar", {"workspace": "alpha2"}),
    ]
    for index, (error, extra) in enumerate(cases):
        job_runs.record_run(JobRun(
            job=f"job-{index}", label="Synthetic",
            started_at=_ago(hours=1), ended_at=_ago(hours=1),
            status="error", error=error, extra=extra,
        ))

    assert [f["error"] for f in recent_job_failures(workspace="alpha")] == ["own"]
    # Unknown caller scope matches nothing rather than falling back to all.
    assert recent_job_failures(workspace="") == []
    # The engine-wide reader keeps seeing every stamped and unscoped failure.
    assert {f["error"] for f in recent_job_failures()} == {
        error for error, _ in cases
    }


def test_workspace_report_omits_engine_logs_and_states_it(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "SYNTH-ENGINE ERROR\n")
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=1), ended_at=_ago(hours=1),
        status="error", error="own stream exploded",
        extra={"workspace": "alpha", "schedule_id": "sched-a"},
    ))
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        started_at=_ago(hours=2), ended_at=_ago(hours=2),
        status="error", error="beta stream exploded",
        extra={"workspace": "beta"},
    ))

    report = build_workspace_issue_report("alpha")

    assert report["scope"] == {"workspace": "alpha", "engine_logs": "omitted"}
    assert report["omitted"] == {
        "error_log": SCOPED_LOG_OMISSION_REASON,
        "debug_log": SCOPED_LOG_OMISSION_REASON,
        "service_log": SCOPED_LOG_OMISSION_REASON,
    }
    assert [f["error"] for f in report["failed_jobs"]] == ["own stream exploded"]
    # No engine log payload or path is part of a scoped report at all.
    assert "error_log" not in report
    assert "error_log_path" not in report
    assert "debug_log" not in report
    assert "debug_log_path" not in report
    text = report["report_text"]
    assert "own stream exploded" in text
    assert "beta stream exploded" not in text
    assert "SYNTH-ENGINE" not in text
    assert "omitted" in text
    assert text == format_workspace_issue_report(report)


def test_workspace_report_states_omission_with_no_scoped_failures(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "SYNTH-ENGINE ERROR\n")
    report = build_workspace_issue_report("alpha")
    assert report["has_issues"] is False
    assert report["failed_jobs"] == []
    text = report["report_text"]
    assert "No failed background jobs" in text
    assert "omitted" in text
    assert "SYNTH-ENGINE" not in text


def test_workspace_report_never_reads_engine_logs(
    tmp_path: Path, monkeypatch
) -> None:
    def _refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("engine-wide log reader called")

    for target in (
        "ciao.debug_report.build_issue_report",
        "ciao.debug_report.tail_error_records",
        "ciao.debug_report.tail_debug_log",
    ):
        monkeypatch.setattr(target, _refuse)

    report = build_workspace_issue_report("alpha")

    assert report["failed_jobs"] == []
    assert report["scope"]["engine_logs"] == "omitted"


def test_workspace_report_redacts_failed_job_errors() -> None:
    job_runs.record_run(JobRun(
        job="backup", label="Backup",
        started_at=_ago(hours=1), ended_at=_ago(hours=1),
        status="error",
        error="push https://u:ghp_abc@github.com/x failed after 3 retries",
        extra={"workspace": "alpha"},
    ))

    report = build_workspace_issue_report("alpha")

    error = report["failed_jobs"][0]["error"]
    assert "ghp_abc" not in error
    assert "https://[redacted]@github.com/x" in error
    assert "failed after 3 retries" in error
    assert "ghp_abc" not in report["report_text"]
    assert "https://[redacted]@github.com/x" in report["report_text"]
    # Redaction changes the report, never the recorded run.
    raw = job_runs.load_runs()["backup"]["recent"][0]["error"]
    assert "ghp_abc" in raw


def test_workspace_report_bounds_total_body_before_fencing() -> None:
    huge = "A" * (SCOPED_REPORT_MAX_CHARS * 3)
    report = {
        "failed_jobs": [
            {"label": "huge", "ended_at": "2026-01-01", "error": huge},
        ],
    }

    first = format_workspace_issue_report(report)
    second = format_workspace_issue_report(report)

    assert first == second
    assert len(first) <= SCOPED_REPORT_MAX_CHARS
    assert SCOPED_REPORT_TRUNCATION_MARKER.strip() in first
    assert SCOPED_ENGINE_LOG_OMISSION_NOTE in first


def test_recent_job_failures_filters_before_limit() -> None:
    """A newer foreign backlog must not crowd out an older owned failure."""
    for index in range(25):
        job_runs.record_run(JobRun(
            job=f"foreign-{index}", label="Foreign",
            started_at=_ago(minutes=index + 1), ended_at=_ago(minutes=index + 1),
            status="error", error=f"foreign {index}",
            extra={"workspace": "beta" if index % 2 else ""},
        ))
    job_runs.record_run(JobRun(
        job="owned", label="Owned",
        started_at=_ago(hours=5), ended_at=_ago(hours=5),
        status="error", error="owned failure",
        extra={"workspace": "alpha"},
    ))

    failures = recent_job_failures(limit=5, workspace="alpha")

    assert [failure["error"] for failure in failures] == ["owned failure"]
    assert len(failures) <= 5


def test_error_count_is_the_main_error_log_and_nothing_else(tmp_path: Path) -> None:
    # Startup triage and the {{ISSUE_REPORT}} placeholder both read the main
    # server_errors.log tail. Rotated and other logs must not
    # leak into the count when the main file is empty.
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "server_errors.log").write_text("", encoding="utf-8")
    (runtime / "server_errors.log.1").write_text(
        "2026-10-03 ERROR root: old\n" * 5, encoding="utf-8"
    )
    (runtime / "server_debug.log").write_text("DEBUG x\n" * 3, encoding="utf-8")
    (runtime / "ciao.stderr.log").write_text("ERROR stderr\n" * 4, encoding="utf-8")

    report = build_issue_report(tmp_path)

    assert report["error_log_records"] == 0
    assert report["has_issues"] is False
    assert report["report_text"] == "(no runtime issues logged)"


def _traceback_record(stamp: str, message: str, frames: int) -> str:
    """One ERROR record as logging writes it: header, then a traceback of ``frames`` frames."""
    lines = [f"{stamp} ERROR ciao.schedules: {message}", "Traceback (most recent call last):"]
    for index in range(frames):
        lines.append(f'  File "/app/ciao/module_{index}.py", line {index + 1}, in run')
        lines.append("    result = await self._dispatch(payload)")
    lines.append("RuntimeError: " + message)
    return "\n".join(lines) + "\n"


def test_error_count_matches_the_text_fix_in_chat_is_given(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "server_errors.log").write_text(
        _traceback_record("2026-11-20 08:40:13,123", "current", frames=3),
        encoding="utf-8",
    )
    (runtime / "server_errors.log.1").write_text(
        "2026-10-03 08:00:00,000 ERROR root: rotated\n" * 5, encoding="utf-8"
    )

    report = build_issue_report(tmp_path)

    # One record: the ERROR line and its traceback lines count once together.
    assert report["error_log_records"] == 1
    assert "current" in report["report_text"]
    assert "rotated" not in report["report_text"]


def test_nine_error_records_with_tracebacks_count_as_nine_not_sixty_three(tmp_path: Path) -> None:
    # The Settings summary used to count physical lines: nine tracebacks of this
    # shape made 63 "error-log lines". The count is records.
    text = "".join(
        _traceback_record(f"2026-11-20 08:4{index}:00,000", f"failure {index}", frames=2)
        for index in range(9)
    )
    assert sum(1 for line in text.splitlines() if line.strip()) == 63
    _write_error_log(tmp_path, text)

    report = build_issue_report(tmp_path)

    assert report["error_log_records"] == 9
    assert report["error_log"] == text


def test_record_cap_keeps_whole_records_not_the_last_physical_lines(tmp_path: Path) -> None:
    first = _traceback_record("2026-11-20 08:00:00,000", "first-run", frames=4)
    middle = _traceback_record("2026-11-20 08:01:00,000", "middle-run", frames=4)
    last = _traceback_record("2026-11-20 08:02:00,000", "newest-run", frames=4)
    _write_error_log(tmp_path, first + middle + last)

    report = build_issue_report(tmp_path, log_records=2)

    # A physical-line cap would cut into the middle traceback; a record cap keeps
    # the two newest records whole and drops the oldest.
    assert report["error_log"] == middle + last
    assert report["error_log_records"] == 2
    assert "first-run" not in report["report_text"]


def test_record_cap_is_applied_before_the_count_so_they_agree(tmp_path: Path) -> None:
    text = "".join(
        _traceback_record(f"2026-11-20 {index // 60:02d}:{index % 60:02d}:00,000", f"run {index}", frames=2)
        for index in range(250)
    )
    _write_error_log(tmp_path, text)

    report = build_issue_report(tmp_path)

    assert report["error_log_records"] == 200
    assert report["error_log"].count("ERROR ciao.schedules:") == 200
    assert "run 49\n" not in report["error_log"]
    assert "run 50\n" in report["error_log"]



# --- a takeover's handover: the old engine's records are not this engine's ----------------------


def _local(when: datetime) -> str:
    """``when`` the way the error log stamps it: local wall-clock time."""
    return when.astimezone().strftime("%Y-%m-%d %H:%M:%S") + ",123"


def _handover_at(monkeypatch, when: datetime | None) -> None:
    from ciao import mac_takeover

    monkeypatch.setattr(mac_takeover, "handed_over_at", lambda: when)


def test_records_and_failures_from_before_a_handover_are_left_out(tmp_path: Path, monkeypatch) -> None:
    handover = datetime.now(UTC) - timedelta(hours=1)
    _handover_at(monkeypatch, handover)
    _write_error_log(tmp_path, (
        f"{_local(handover - timedelta(minutes=10))} ERROR ciao.old: old engine boom\n"
        "Traceback (most recent call last):\n  old frame\n"
        f"{_local(handover + timedelta(minutes=10))} ERROR ciao.new: new engine boom\n"
    ))
    job_runs.record_run(JobRun(
        job="old_job", label="Old job", started_at=_ago(hours=2), ended_at=_ago(hours=2),
        status="error", error="old engine failure",
    ))
    job_runs.record_run(JobRun(
        job="new_job", label="New job", started_at=_ago(minutes=30), ended_at=_ago(minutes=30),
        status="error", error="new engine failure",
    ))

    report = build_issue_report(tmp_path)

    assert report["error_log_records"] == 1
    assert "new engine boom" in report["error_log"]
    assert "old engine boom" not in report["error_log"]
    assert "old frame" not in report["error_log"]
    assert [f["error"] for f in report["failed_jobs"]] == ["new engine failure"]


def test_a_log_from_before_the_handover_alone_is_no_issue(tmp_path: Path, monkeypatch) -> None:
    handover = datetime.now(UTC) - timedelta(minutes=5)
    _handover_at(monkeypatch, handover)
    _write_error_log(tmp_path, "".join(
        f"{_local(handover - timedelta(hours=1, seconds=i))} ERROR ciao.old: boom {i}\n" for i in range(160)
    ))

    report = build_issue_report(tmp_path)

    assert report["has_issues"] is False
    assert report["error_log_records"] == 0


def test_without_a_handover_every_record_counts(tmp_path: Path, monkeypatch) -> None:
    _handover_at(monkeypatch, None)
    _write_error_log(tmp_path, "2020-01-01 10:00:00,000 ERROR ciao.web: long ago\n")

    assert build_issue_report(tmp_path)["error_log_records"] == 1


def test_the_handover_floor_wins_over_an_earlier_or_naive_failures_since(tmp_path: Path, monkeypatch) -> None:
    handover = datetime.now(UTC) - timedelta(hours=1)
    _handover_at(monkeypatch, handover)
    job_runs.record_run(JobRun(
        job="old_job", label="Old job", started_at=_ago(hours=2), ended_at=_ago(hours=2),
        status="error", error="old engine failure",
    ))
    naive = (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None)

    assert build_issue_report(tmp_path, failures_since=naive)["failed_jobs"] == []
    later = datetime.now(UTC) - timedelta(minutes=1)
    assert build_issue_report(tmp_path, failures_since=later)["failed_jobs"] == []
