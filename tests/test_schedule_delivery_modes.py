from ciao.schedules import ScheduleEntry
from ciao.web.project_chats import (
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


def test_retry_pending_stays_visible() -> None:
    outcome = ScheduleRunOutcome(
        completed=True,
        is_error=False,
        retry_pending=True,
    )
    assert _should_auto_archive_schedule_run(_entry(), outcome) is False


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
