import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.schedules import ScheduleEntry
from ciao.web.project_chats import (
    ProjectChatManager,
    ScheduleRunOutcome,
    _should_auto_archive_schedule_run,
)


def _entry(*, archive: str = "auto") -> ScheduleEntry:
    return ScheduleEntry(
        schedule_id="sched-test",
        daily_time_utc="01:00",
        prompt="curate",
        chat_id=0,
        created_at="2026-06-06T00:00:00Z",
        archive_policy=archive,
    )


def test_auto_policy_archives_when_classifier_says_no_user_needed() -> None:
    outcome = ScheduleRunOutcome(completed=True, is_error=False)
    assert _should_auto_archive_schedule_run(_entry(), outcome, needs_user=False) is True


def test_permission_request_stays_visible() -> None:
    outcome = ScheduleRunOutcome(
        completed=True,
        is_error=False,
        permission_requested=True,
    )
    assert _should_auto_archive_schedule_run(_entry(), outcome) is False


def test_structured_question_stays_visible() -> None:
    outcome = ScheduleRunOutcome(
        completed=False,
        is_error=False,
        question_requested=True,
    )
    assert _should_auto_archive_schedule_run(_entry(), outcome) is False


def test_retry_pending_stays_visible() -> None:
    outcome = ScheduleRunOutcome(
        completed=True,
        is_error=False,
        retry_pending=True,
    )
    assert _should_auto_archive_schedule_run(_entry(), outcome) is False


def test_quota_retry_is_recorded_as_skipped_not_error() -> None:
    from ciao.web.project_chats import _schedule_dispatch_status

    status, error = _schedule_dispatch_status(
        ScheduleRunOutcome(
            completed=True,
            is_error=True,
            retry_pending=True,
            final_text="API Error: Request rejected (429) weekly usage limit",
        )
    )

    assert status == "skipped"
    assert error is None


def test_unsettled_subagents_are_recorded_as_skipped_not_ok() -> None:
    """A parent turn that finished while its background subagents did not
    settle is an unfinished run: recording "ok" would clear a previous error
    stamp and brand the schedule healthy while its synthesis never ran."""
    from ciao.web.project_chats import _schedule_dispatch_status

    status, error = _schedule_dispatch_status(
        ScheduleRunOutcome(
            completed=True,
            is_error=False,
            subagents_pending=True,
        )
    )

    assert status == "skipped"
    assert error is None


def test_manual_policy_stays_visible_after_clean_success() -> None:
    outcome = ScheduleRunOutcome(completed=True, is_error=False)
    assert _should_auto_archive_schedule_run(_entry(archive="manual"), outcome) is False


def test_auto_policy_archives_when_classifier_says_no_user_needed() -> None:
    outcome = ScheduleRunOutcome(completed=True, is_error=False)
    assert (
        _should_auto_archive_schedule_run(
            _entry(archive="auto"),
            outcome,
            needs_user=False,
        )
        is True
    )


def test_auto_policy_stays_visible_when_classifier_says_user_needed() -> None:
    outcome = ScheduleRunOutcome(completed=True, is_error=False)
    assert (
        _should_auto_archive_schedule_run(
            _entry(archive="auto"),
            outcome,
            needs_user=True,
        )
        is False
    )


def test_failed_run_is_not_clean_so_error_log_survives() -> None:
    # A 429/stream failure mid-triage must not count as clean: the
    # error-log clear in _dispatch gates on _schedule_run_clean.
    from ciao.web.project_chats import _schedule_run_clean

    assert _schedule_run_clean(ScheduleRunOutcome(completed=True, is_error=False)) is True
    assert _schedule_run_clean(ScheduleRunOutcome(completed=True, stream_error=True)) is False
    assert _schedule_run_clean(ScheduleRunOutcome(completed=True, is_error=True)) is False
    assert _schedule_run_clean(ScheduleRunOutcome(completed=True, retry_pending=True)) is False
    assert _schedule_run_clean(ScheduleRunOutcome(completed=True, question_requested=True)) is False
    assert _schedule_run_clean(ScheduleRunOutcome(completed=False)) is False


def test_pending_background_subagents_keep_run_unclean() -> None:
    # A parent turn that finished cleanly but left background subagents
    # running is not "done": it must not count as clean (so it stays visible
    # and is not auto-archived on a half-complete result).
    from ciao.web.project_chats import _schedule_run_clean

    assert (
        _schedule_run_clean(
            ScheduleRunOutcome(completed=True, is_error=False, subagents_pending=True)
        )
        is False
    )


def test_auto_policy_does_not_archive_while_subagents_pending() -> None:
    outcome = ScheduleRunOutcome(
        completed=True, is_error=False, subagents_pending=True
    )
    # Even if the classifier would say no attention needed, an unsettled run
    # must stay visible.
    assert (
        _should_auto_archive_schedule_run(_entry(), outcome, needs_user=False)
        is False
    )


# ── Interim subagent text (the 2026-08-30 daily-log failure) ─────────────


def test_interim_subagent_text_detected() -> None:
    """A run that ended on "Waiting on X" never synthesized its agents."""
    f = ProjectChatManager._is_interim_subagent_text
    assert f("Waiting on the Drive subagent, the last one.") is True
    assert f("Phase A complete (all empty). Waiting on the Drive, Tasks, "
             "and chat-transcript subagents.") is True
    assert f("Nothing to do — all background agents still running.") is False
    assert f("") is False


def test_auto_policy_does_not_archive_interim_subagent_text() -> None:
    """The exact 2026-08-30 shape: clean 1-turn run whose final words were an
    interim waiting message. The agents' data was never synthesized and the
    run's follow-up work (writing the log, committing) never happened, so the
    run must not auto-archive."""
    outcome = ScheduleRunOutcome(
        completed=True,
        is_error=False,
        final_text="Waiting on the Drive subagent, the last one.",
        subagents_pending=True,
    )
    assert (
        _should_auto_archive_schedule_run(_entry(), outcome, needs_user=False)
        is False
    )
    # It is reported as skipped-with-reason, not ok: the dispatch row must
    # tell the story instead of reading "ok".
    from ciao.web.project_chats import _schedule_run_clean

    assert _schedule_run_clean(outcome) is False


def _job_rows(tmp_path: Path) -> list[dict]:
    path = tmp_path / "job_runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _manager_for_classifier() -> ProjectChatManager:
    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_INSIGHTS_MODEL": "haiku",
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
    })
    manager._projects = {}
    return manager


async def test_schedule_attention_classifier_tracks_model_and_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_oneshot(*args, **kwargs):
        assert kwargs["model"] == "haiku"
        return '{"needs_user": false, "reason": "routine"}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is False
    row = _job_rows(tmp_path)[0]
    assert row["job"] == "schedule_attention_classifier"
    assert row["status"] == "ok"
    assert row["model"] == "haiku"
    assert row["extra"]["schedule_id"] == "sched-test"
    assert row["extra"]["needs_user"] is False
    assert row["extra"]["reason"] == "routine"


async def test_schedule_attention_classifier_routes_qualified_insights_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager_for_classifier()
    manager._config.insights_model_override = "opencode:vendor/insights-model"
    captured: dict[str, object] = {}

    async def fake_oneshot(*args, **kwargs):
        captured.update(kwargs)
        return '{"needs_user": false, "reason": "routine"}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    assert await manager._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    ) is False
    assert captured["provider"] == "opencode"
    assert captured["model"] == "vendor/insights-model"


async def test_schedule_attention_classifier_tracks_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is True
    row = _job_rows(tmp_path)[0]
    assert row["job"] == "schedule_attention_classifier"
    assert row["status"] == "error"
    assert row["model"] == "haiku"
    assert row["error"] == "model unavailable"


async def test_schedule_attention_classifier_records_bare_timeout_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_oneshot(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is True
    row = _job_rows(tmp_path)[0]
    assert row["status"] == "error"
    assert row["error"] == "TimeoutError"


async def test_schedule_attention_classifier_uses_insights_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classifier shares the insights job's env-tunable budget.

    The slow Ollama Cloud model measures 214-253s on a successful call;
    a hard 60s window turns tail latency into a guaranteed TimeoutError.
    """
    captured: dict[str, object] = {}

    async def fake_oneshot(*args, **kwargs):
        captured["timeout_s"] = kwargs["timeout_s"]
        return '{"needs_user": false, "reason": "routine"}'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)
    monkeypatch.setenv("CIAO_INSIGHTS_TIMEOUT_S", "900")

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is False
    assert captured["timeout_s"] == 900.0


async def test_schedule_attention_classifier_records_context_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 400-style overflow is recorded on the run, not as a generic error.

    The classifier payload is already trimmed to final_text[-6000:], so an
    overflow here is rare, but the run should still surface the failure
    class so the Automation page can tell a context-window problem from a
    transient timeout.
    """
    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("API Error 400 Message too long: 9999 > 4096")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    # Conservative: keep the chat visible on classifier failure.
    assert needs_user is True
    row = _job_rows(tmp_path)[0]
    assert row["status"] == "error"
    assert row["extra"]["context_overflow"] is True


async def test_schedule_attention_classifier_parses_prose_wrapped_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that wraps its JSON in prose or fences must still be parsed.

    The classifier previously did a bare ``json.loads`` after stripping fences,
    so prose-wrapped output (or empty output) raised a JSONDecodeError and the
    run was recorded as an error even though the verdict was recoverable.
    """
    async def fake_oneshot(*args, **kwargs):
        return 'Here is my assessment:\n```json\n{"needs_user": false, "reason": "routine"}\n```\nNo action needed.'

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is False
    row = _job_rows(tmp_path)[0]
    assert row["status"] == "ok"
    assert row["extra"]["needs_user"] is False
    assert row["extra"]["reason"] == "routine"


async def test_schedule_attention_classifier_empty_output_keeps_chat_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty model output must degrade to the conservative keep-visible default.

    This is the exact failure seen in production: the model returned nothing,
    ``json.loads("")`` raised, and the run was logged as an error. It should
    still keep the chat visible, but without a spurious traceback.
    """
    async def fake_oneshot(*args, **kwargs):
        return ""

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    needs_user = await _manager_for_classifier()._schedule_run_needs_user(
        _entry(), ScheduleRunOutcome(completed=True, final_text="done")
    )

    assert needs_user is True
    row = _job_rows(tmp_path)[0]
    assert row["status"] == "error"
    assert row["error"] == "classifier returned no parseable JSON"
