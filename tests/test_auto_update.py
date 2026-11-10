"""The unattended updater's lifecycle: policy, platform guard, claim, retry.

Every test here drives `AutoUpdateCoordinator` through its injected seams —
store, wall clock, stage, apply handoff, release lookup, operation record, the
engine's claim and the wait — so a whole update lifecycle (and every way it can
be refused) is exercised without a network, a receipt, a supervisor or a real
engine.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Callable

import pytest

from ciao import engine_update, package_version
from ciao.auto_update import (
    BUSY_RETRY_SECONDS,
    CHECK_INTERVAL_SECONDS,
    CLAIM_EXPIRY_SECONDS,
    _MAX_BACKOFF_SECONDS,
    _MIN_BACKOFF_SECONDS,
    OUTCOME_UNKNOWN,
    SNOOZE_SECONDS,
    SUPPORTED_PLATFORMS,
    AutoUpdateCoordinator,
    AutoUpdateSupport,
    auto_update_support,
    wire_auto_update,
)
from ciao.app_settings import AppSettingsStore
from ciao.backup_service import BackupService
from ciao.config import CiaoConfig
from ciao.install_receipt import InstallReceipt
from ciao.legacy_node_state import LegacyNodeState
from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager

# A wall-clock second count with plenty of room either side, so a test can jump
# months forward or an hour back without leaving the representable range.
START = 1_800_000_000.0


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _seconds(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _operation(
    *,
    operation_id: str = "20260101T000000Z-2.0.17",
    phase: str = "staged",
    to_version: str = "2.0.17",
    error: str = "",
) -> engine_update.Operation:
    return engine_update.Operation(
        id=operation_id,
        phase=phase,
        from_version="2.0.16",
        to_version=to_version,
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
        error=error,
    )


def _receipt(
    *, backend: str = "launchd", python: str | None = None
) -> InstallReceipt:
    return InstallReceipt(
        version="2.0.16",
        executable="/opt/ciao/bin/ciao",
        python=python or sys.executable,
        service_backend=backend,
        service_label="com.ciao.server",
        installed_at="2026-11-01T00:00:00+00:00",
    )


class _Clock:
    """A wall clock the tests move by hand."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Harness:
    """One coordinator over a real settings file, with every seam recorded."""

    def __init__(
        self,
        tmp_path,
        *,
        clock: _Clock | None = None,
        store: AppSettingsStore | None = None,
        current_version: str = "2.0.16",
        latest: str = "2.0.17",
        supported: bool = True,
        claim_result: bool = True,
        wait: Callable | None = None,
    ) -> None:
        self.path = tmp_path / "app_settings.json"
        self.store = store or AppSettingsStore(self.path)
        self.clock = clock or _Clock()
        self.current_version = current_version
        self.latest = latest
        self.support_result = (
            AutoUpdateSupport(True)
            if supported
            else AutoUpdateSupport(False, "this install cannot update itself")
        )
        self.claim_result = claim_result
        self.stages: list[str] = []
        self.spawns: list[str] = []
        self.claims: list[str] = []
        self.releases: list[str] = []
        self.fetches = 0
        self.operation: engine_update.Operation | None = None
        # Injected failures. Each one is what the real dependency would raise.
        self.stage_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.spawn_error: Exception | None = None
        self.support_error: Exception | None = None
        # Runs inside the stage call, which is where "the operator opted out
        # while the download was in flight" happens in real life.
        self.stage_hook: Callable[[], None] | None = None
        self.coordinator = AutoUpdateCoordinator(
            store=self.store,
            clock=self.clock,
            stage=self._stage,
            spawn_apply=self._spawn,
            fetch_latest=self._fetch,
            read_operation=lambda: self.operation,
            claim=self._claim,
            release_claim=self._release,
            support=self._support,
            current_version=lambda: self.current_version,
            wait=wait or self._never_waits,
        )

    # ── the injected seams ──────────────────────────────────────────────

    def _stage(self, version: str) -> engine_update.Operation:
        if self.stage_error is not None:
            raise self.stage_error
        if self.stage_hook is not None:
            self.stage_hook()
        self.stages.append(version)
        self.operation = _operation(
            operation_id=f"20260101T000000Z-{version}", to_version=version
        )
        return self.operation

    def _fetch(self) -> str:
        self.fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.latest

    def _claim(self, owner: str) -> bool:
        self.claims.append(owner)
        return self.claim_result

    def _release(self, owner: str) -> None:
        self.releases.append(owner)

    def _spawn(self, attempt_id: str) -> None:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawns.append(attempt_id)

    def _support(self) -> AutoUpdateSupport:
        if self.support_error is not None:
            raise self.support_error
        return self.support_result

    async def _never_waits(self, seconds: float, stop: asyncio.Event) -> None:
        raise AssertionError("a tick must not wait")

    # ── conveniences ────────────────────────────────────────────────────

    @property
    def settings(self):
        return self.store.settings

    def tick(self) -> None:
        asyncio.run(self.coordinator.tick())

    def due(self) -> float:
        """Move the clock to the persisted next check and return that instant."""
        self.clock.now = _seconds(self.settings.auto_update_next_check_at)
        return self.clock.now


# ── the policy and the install guard ────────────────────────────────────


@pytest.mark.parametrize(
    ("platform", "mode", "receipt", "backend", "supported", "reason"),
    [
        # Linux has no supervisor the transaction can hand a swap to. This is
        # the case `default_update_host()` would wrongly encourage: it returns
        # the macOS host off Windows so the transaction's tests can run on CI.
        ("linux", "installer", _receipt(), "", False, "macOS and Windows"),
        ("linux", "editable", None, "", False, "macOS and Windows"),
        # No receipt, so no proof of the environment and nothing to roll back.
        ("darwin", "installer", None, "", False, "no install receipt"),
        ("darwin", "editable", _receipt(), "", False, "engine installer"),
        ("darwin", "bundled_app", _receipt(backend="none"), "", False, "engine installer"),
        ("win32", "unknown", None, "", False, "engine installer"),
        # A receipt for a different environment is a stale install, not ours.
        (
            "darwin",
            "installer",
            _receipt(python="/nonexistent/env/bin/python"),
            "",
            False,
            "different environment",
        ),
        # The installer registers launchd on macOS and a task on Windows; a
        # receipt naming another supervisor describes something this engine
        # cannot stop, swap and start.
        ("darwin", "installer", _receipt(backend="systemd-user"), "", False, "launchd"),
        ("darwin", "installer", _receipt(), "windows-task", False, "launchd"),
        ("win32", "installer", _receipt(backend="launchd"), "", False, "windows-task"),
        # The two installs the transaction can actually perform unattended.
        ("darwin", "installer", _receipt(), "", True, ""),
        ("win32", "installer", _receipt(backend="windows-task"), "", True, ""),
    ],
)
def test_auto_update_support_classifies_every_install(
    platform: str,
    mode: str,
    receipt: InstallReceipt | None,
    backend: str,
    supported: bool,
    reason: str,
) -> None:
    result = auto_update_support(platform, mode, receipt, backend)

    assert result.supported is supported
    assert SUPPORTED_PLATFORMS == ("darwin", "win32")
    if supported:
        assert result.reason == ""
    else:
        # A sentence Settings can show, naming the fact that is missing.
        assert reason in result.reason


def test_auto_update_support_reads_the_running_receipt(monkeypatch) -> None:
    """With no receipt named, the classifier asks *this* process's receipt."""
    monkeypatch.setattr(
        package_version, "detect_install_mode", lambda: "installer"
    )
    monkeypatch.setattr("ciao.auto_update.running_receipt", lambda: None)

    missing = auto_update_support(platform="darwin")
    assert not missing.supported
    assert "no install receipt" in missing.reason

    monkeypatch.setattr("ciao.auto_update.running_receipt", lambda: _receipt())
    assert auto_update_support(platform="darwin").supported


def test_unsupported_install_never_stages(tmp_path) -> None:
    harness = _Harness(tmp_path, supported=False)

    harness.tick()

    # Not even the release lookup: there is nothing this install could do with
    # an answer, so asking GitHub would be a request per tick for nothing.
    assert harness.fetches == 0
    assert harness.stages == []
    assert harness.claims == []
    assert harness.spawns == []
    # The refusal is scheduled at the normal cadence, so a loop cannot spin.
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + CHECK_INTERVAL_SECONDS, abs=1
    )
    status = harness.coordinator.status()
    assert status["supported"] is False
    assert status["unsupported_reason"] == "this install cannot update itself"


# ── the happy path and the durable policy ───────────────────────────────


def test_fresh_install_without_key_updates_once_when_idle(tmp_path) -> None:
    harness = _Harness(tmp_path)
    # No settings file at all yet: the documented default is on, and the
    # coordinator acts on it without needing a key to have been written.
    assert not harness.path.exists()
    assert harness.settings.auto_update_enabled is True

    harness.tick()

    operation_id = "20260101T000000Z-2.0.17"
    assert harness.stages == ["2.0.17"]
    assert harness.claims == [operation_id]
    assert harness.spawns == [operation_id]
    assert harness.settings.auto_update_attempt_id == operation_id
    assert harness.settings.auto_update_attempt_at == _stamp(harness.clock.now)
    # A handoff is not a healthy update: the outcome is unknown until the
    # operation record says what happened.
    assert harness.settings.auto_update_outcome == ""

    # Repeated ticks do not stage again, not even after the cadence: the
    # attempt has an operation record and that record is still running.
    harness.tick()
    harness.clock.advance(CHECK_INTERVAL_SECONDS + 1)
    harness.tick()

    assert harness.stages == ["2.0.17"]
    assert harness.spawns == [operation_id]
    assert harness.fetches == 1


def test_explicit_off_never_attempts(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.coordinator.set_enabled(False)
    before = harness.path.read_text()

    harness.tick()

    assert harness.fetches == 0
    assert harness.stages == []
    assert harness.spawns == []
    # Nothing is written for a turned-off updater, so its file says exactly
    # what the owner decided.
    assert harness.path.read_text() == before

    # Turning it off *during* the download is honoured too: the stage is kept,
    # the claim is never taken.
    harness.coordinator.set_enabled(True)
    harness.stage_hook = lambda: harness.coordinator.set_enabled(False)
    harness.tick()

    assert harness.stages == ["2.0.17"]
    assert harness.claims == []
    assert harness.spawns == []


def test_already_current_never_stages(tmp_path) -> None:
    harness = _Harness(tmp_path, latest="2.0.16")

    harness.tick()

    assert harness.fetches == 1
    assert harness.stages == []
    assert harness.claims == []
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + CHECK_INTERVAL_SECONDS, abs=1
    )


def test_snooze_survives_restart_and_blocks_until_expiry(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.coordinator.snooze()

    until = _stamp(harness.clock.now + SNOOZE_SECONDS)
    assert harness.settings.auto_update_snooze_until == until
    # The hold is the wait: nothing polls the release list while it lasts.
    assert harness.coordinator.next_wait_seconds() == pytest.approx(SNOOZE_SECONDS, abs=1)

    # A restart reads the same hold out of the same file.
    restarted = _Harness(tmp_path, clock=harness.clock)
    assert restarted.settings.auto_update_snooze_until == until
    harness.clock.advance(3600)
    restarted.tick()
    assert restarted.stages == []

    # Enabling and disabling are not a way to clear a hold.
    restarted.coordinator.set_enabled(False)
    restarted.coordinator.set_enabled(True)
    assert restarted.settings.auto_update_snooze_until == until

    harness.clock.advance(SNOOZE_SECONDS - 3600 + 1)
    restarted.tick()
    assert restarted.stages == ["2.0.17"]


def test_staged_older_release_is_not_a_blocker(tmp_path) -> None:
    """A waiting stage for another version is not an apply in flight."""
    harness = _Harness(tmp_path)
    harness.operation = _operation(
        operation_id="20260101T000000Z-2.0.16", phase="staged", to_version="2.0.16"
    )

    harness.tick()

    assert harness.stages == ["2.0.17"]
    assert harness.spawns == ["20260101T000000Z-2.0.17"]


def test_claim_refused_keeps_stage_and_retries(tmp_path) -> None:
    harness = _Harness(tmp_path, claim_result=False)

    harness.tick()

    # The release is staged once. Staging is not a claim: work arriving during
    # the download wins, and the verified stage is kept for later.
    assert harness.stages == ["2.0.17"]
    assert harness.claims == ["20260101T000000Z-2.0.17"]
    assert harness.spawns == []
    # Nothing is recorded as an attempt: no attempt happened.
    assert harness.settings.auto_update_attempt_id == ""
    assert harness.settings.auto_update_attempt_at == ""
    assert harness.settings.auto_update_outcome == ""
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + BUSY_RETRY_SECONDS, abs=1
    )

    # Before the retry window, nothing — not even a network check.
    fetches = harness.fetches
    harness.tick()
    assert harness.fetches == fetches


def test_busy_then_idle_retries_and_applies(tmp_path) -> None:
    harness = _Harness(tmp_path, claim_result=False)
    harness.tick()

    # The engine becomes idle; the same verified stage is applied, never
    # downloaded a second time.
    harness.claim_result = True
    harness.clock.advance(BUSY_RETRY_SECONDS + 1)
    harness.tick()

    assert harness.stages == ["2.0.17"]
    assert harness.claims == ["20260101T000000Z-2.0.17"] * 2
    assert harness.spawns == ["20260101T000000Z-2.0.17"]
    assert harness.settings.auto_update_attempt_id == "20260101T000000Z-2.0.17"


# ── failure, backoff and the wall clock ─────────────────────────────────


def test_transient_failure_backoff_handles_sleep_and_clock_reversal(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.fetch_error = engine_update.UpdateError("network is unreachable")

    harness.tick()

    assert harness.settings.auto_update_outcome == "check_failed"
    assert "network is unreachable" in harness.settings.auto_update_error
    assert harness.stages == []
    # The first retry is the floor, not an instant.
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + _MIN_BACKOFF_SECONDS, abs=1
    )

    # Doubling, from the floor to the ceiling, and never past it.
    delays: list[float] = []
    for _ in range(8):
        harness.due()
        harness.tick()
        delays.append(
            _seconds(harness.settings.auto_update_next_check_at) - harness.clock.now
        )
    assert delays == sorted(delays)
    assert delays[1] == 2 * delays[0]
    assert min(delays) >= _MIN_BACKOFF_SECONDS
    assert max(delays) <= _MAX_BACKOFF_SECONDS
    assert max(delays) == _MAX_BACKOFF_SECONDS

    # A Mac that slept through the whole backoff makes exactly one check when it
    # wakes — not one per interval it missed.
    harness.fetch_error = None
    harness.latest = "2.0.16"  # up to date, so the check ends here
    fetches = harness.fetches
    harness.clock.advance(10 * 3600)
    harness.tick()
    assert harness.fetches == fetches + 1
    # A recovered check clears the stale failure: the updater is working.
    assert harness.settings.auto_update_error == ""
    assert harness.settings.auto_update_outcome == ""

    # A backwards clock jump is a longer wait, never a spin.
    fetches = harness.fetches
    harness.clock.advance(-3600)
    harness.tick()
    assert harness.fetches == fetches
    assert harness.coordinator.next_wait_seconds() > 0


def test_run_waits_from_wall_clock_and_stops(tmp_path) -> None:
    waits: list[float] = []
    stop = asyncio.Event()

    async def wait(seconds: float, event: asyncio.Event) -> None:
        waits.append(seconds)
        event.set()

    harness = _Harness(tmp_path, wait=wait)

    async def scenario() -> None:
        await harness.coordinator.run(stop)

    asyncio.run(scenario())

    assert harness.stages == ["2.0.17"]
    # One tick, then the whole six-hour cadence as the wait — never a tight
    # zero-delay loop, and never a shorter wait than the persisted schedule.
    assert len(waits) == 1
    assert CHECK_INTERVAL_SECONDS - 1 <= waits[0] <= CHECK_INTERVAL_SECONDS


def test_stage_failure_is_actionable_and_preserves_install(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.stage_error = engine_update.UpdateError(
        "signature verification failed: wheel digest does not match the manifest"
    )

    harness.tick()

    assert harness.stages == []
    assert harness.claims == []
    assert harness.spawns == []
    assert harness.settings.auto_update_outcome == "stage_failed"
    assert "digest does not match" in harness.settings.auto_update_error
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + _MIN_BACKOFF_SECONDS, abs=1
    )
    # Nothing was applied, so the running install is untouched.
    assert harness.current_version == "2.0.16"

    # The reason survives a restart, which is when the owner reads it.
    restarted = _Harness(tmp_path, clock=harness.clock)
    status = restarted.coordinator.status()
    assert status["outcome"] == "stage_failed"
    assert "digest does not match" in status["error"]


def test_spawn_failure_releases_claim(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.spawn_error = RuntimeError("can't start new thread")

    harness.tick()

    operation_id = "20260101T000000Z-2.0.17"
    assert harness.spawns == []
    # The claim it just took is given back, through the owner path only.
    assert harness.releases == [operation_id]
    assert harness.settings.auto_update_attempt_id == ""
    assert harness.settings.auto_update_outcome == "failed"
    assert "could not be started" in harness.settings.auto_update_error
    assert _seconds(harness.settings.auto_update_next_check_at) == pytest.approx(
        harness.clock.now + _MIN_BACKOFF_SECONDS, abs=1
    )


def test_support_failure_is_recorded(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.support_error = OSError("install-receipt.json: permission denied")

    harness.tick()

    assert harness.stages == []
    assert harness.settings.auto_update_outcome == "check_failed"
    assert "permission denied" in harness.settings.auto_update_error


def test_unreadable_settings_never_stage(tmp_path) -> None:
    path = tmp_path / "app_settings.json"
    path.write_text("{not json")
    harness = _Harness(tmp_path, store=AppSettingsStore(path))

    harness.tick()

    # Fail closed: the file's "enabled" defaults are not consent, and writing
    # here would erase a decision nobody could read.
    assert harness.fetches == 0
    assert harness.stages == []
    assert harness.spawns == []
    assert path.read_text() == "{not json"
    status = harness.coordinator.status()
    assert status["enabled"] is False
    assert "could not be read" in status["error"]
    assert harness.coordinator.next_wait_seconds() >= BUSY_RETRY_SECONDS


# ── reconciling a previous process's attempt ────────────────────────────


@pytest.mark.parametrize(
    ("phase", "error"),
    [
        ("applied", ""),
        ("rolled_back", "the new engine never reported ready"),
        ("rollback_failed", "restoring the previous env failed"),
        ("failed", "drain timed out after 600s"),
    ],
)
def test_prior_attempt_reconciled_after_restart(tmp_path, phase, error) -> None:
    operation_id = "20260101T000000Z-2.0.17"
    harness = _Harness(tmp_path)
    harness.store.update(
        {
            "auto_update_attempt_id": operation_id,
            "auto_update_attempt_at": _stamp(START - 60),
            "auto_update_outcome": "",
            "auto_update_error": "",
        }
    )
    harness.operation = _operation(operation_id=operation_id, phase=phase, error=error)

    harness.tick()

    # The terminal phase is the outcome, including the transaction's own error.
    assert harness.settings.auto_update_outcome == phase
    assert harness.settings.auto_update_error == error
    assert harness.settings.auto_update_attempt_id == ""
    assert harness.settings.auto_update_attempt_at == ""
    # Admission is reopened by the owner that closed it, and only that one.
    assert harness.releases == [operation_id]
    # Reconciliation owns this tick: no second update starts on the strength of
    # the first one's record.
    assert harness.fetches == 0
    assert harness.stages == []


def test_prior_attempt_without_its_record_is_unknown(tmp_path) -> None:
    operation_id = "20260101T000000Z-2.0.17"
    harness = _Harness(tmp_path)
    harness.store.update(
        {
            "auto_update_attempt_id": operation_id,
            "auto_update_attempt_at": _stamp(START - CLAIM_EXPIRY_SECONDS - 60),
        }
    )
    # The record is gone, and so is any proof of what the apply did.
    harness.operation = None

    harness.tick()

    assert harness.settings.auto_update_outcome == "unknown"
    assert "unknown" in harness.settings.auto_update_error
    assert harness.settings.auto_update_attempt_id == ""
    assert harness.releases == [operation_id]

    # Young enough that the child may still be writing its record: wait, do
    # not call an in-flight apply unknown.
    harness2 = _Harness(tmp_path / "young")
    harness2.store.update(
        {
            "auto_update_attempt_id": operation_id,
            "auto_update_attempt_at": _stamp(START - 30),
        }
    )
    harness2.operation = None

    harness2.tick()

    assert harness2.settings.auto_update_attempt_id == operation_id
    assert harness2.settings.auto_update_outcome == ""
    assert harness2.releases == []
    assert harness2.stages == []


def test_a_matching_operation_record_is_not_aged_out_before_the_expiry(tmp_path) -> None:
    """A live swap's record is proof of work until the claim expires."""
    operation_id = "20260101T000000Z-2.0.17"
    harness = _Harness(tmp_path)
    harness.store.update(
        {
            "auto_update_attempt_id": operation_id,
            "auto_update_attempt_at": _stamp(START - 60),
        }
    )
    harness.operation = _operation(operation_id=operation_id, phase="swapping")

    harness.tick()

    # Releasing here would reopen admission while the updater swaps the install.
    assert harness.releases == []
    assert harness.settings.auto_update_attempt_id == operation_id
    assert harness.settings.auto_update_outcome == ""
    assert harness.stages == []


def test_a_stalled_applier_is_released_after_the_claim_expiry(tmp_path) -> None:
    """A swap that never took this engine down must not close admission forever.

    A real swap stops the engine first, so a non-terminal record this old,
    seen from a running engine, is an applier that stopped reporting.
    """
    operation_id = "20260101T000000Z-2.0.17"
    harness = _Harness(tmp_path)
    harness.store.update(
        {
            "auto_update_attempt_id": operation_id,
            "auto_update_attempt_at": _stamp(START - CLAIM_EXPIRY_SECONDS - 600),
        }
    )
    harness.operation = _operation(operation_id=operation_id, phase="swapping")

    harness.tick()

    assert harness.releases == [operation_id]
    assert harness.settings.auto_update_attempt_id == ""
    assert harness.settings.auto_update_outcome == OUTCOME_UNKNOWN
    assert "kept running" in harness.settings.auto_update_error
    assert harness.stages == []


# ── the engine wiring ───────────────────────────────────────────────────
#
# `wire_auto_update` is what `ciao.main` calls, so these tests drive the real
# chat manager, scheduler and backup service rather than stand-ins for them:
# the question is whether the probes are registered on the object whose claim
# reads them, not whether a fake can return a list.


class _MemoryPasses:
    """A memory-pass queue that is exactly as busy as the test says."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    def update_idle_reasons(self) -> list[str]:
        return list(self.reasons)


def _chat_manager(tmp_path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _backup_service(tmp_path) -> BackupService:
    """A real backup service; nothing is read until a run is admitted."""
    workspace = tmp_path / "backup"
    runtime = workspace / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=workspace,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return BackupService(
        config,
        AppSettingsStore(runtime / "app_settings.json"),
        node_state=lambda: LegacyNodeState(kind="host"),
    )


def _wired(tmp_path, *, app_state=None, **overrides):
    chats = _chat_manager(tmp_path)
    schedules = ScheduleManager(store=ScheduleStore(tmp_path))
    backup = _backup_service(tmp_path)
    memory_passes = _MemoryPasses()
    coordinator = wire_auto_update(
        store=AppSettingsStore(tmp_path / "app_settings.json"),
        chats=chats,
        schedules=schedules,
        backup=backup,
        memory_passes=memory_passes,
        app_state=app_state,
        platform="linux",
        **overrides,
    )
    return coordinator, chats, schedules, backup, memory_passes


def test_wire_auto_update_registers_every_probe(tmp_path) -> None:
    _coordinator, chats, schedules, backup, memory_passes = _wired(tmp_path)

    # The chat manager alone is idle: no streams, no parked work, nothing.
    assert chats.update_idle_reasons() == []

    # A run this process started, tracked before it has a stream to see.
    schedules._inflight.add("sched-1")
    assert any("sched-1" in reason for reason in chats.update_idle_reasons())
    assert chats.claim_for_update("auto-1") is False
    assert chats.restart_draining is False  # a refused claim closes nothing

    # A chat claimed by a dispatch that has not reached `start_stream` yet.
    schedules._inflight.clear()
    schedules._claimed_chats.add("chat-1")
    assert any("chat-1" in reason for reason in chats.update_idle_reasons())
    assert chats.claim_for_update("auto-1") is False

    schedules._claimed_chats.clear()
    backup._running = True
    assert chats.update_idle_reasons() == ["a backup is running"]
    assert chats.claim_for_update("auto-1") is False

    backup._running = False
    memory_passes.reasons.append("memory pass queued in chat m-1")
    assert chats.update_idle_reasons() == ["memory pass queued in chat m-1"]
    assert chats.claim_for_update("auto-1") is False

    # Every subsystem idle again, so the very same manager may claim.
    memory_passes.reasons.clear()
    assert chats.update_idle_reasons() == []
    assert chats.claim_for_update("auto-1") is True
    assert chats.cancel_restart_drain("auto-1") is True


def test_wire_auto_update_claim_sets_the_route_marker(tmp_path) -> None:
    app_state = SimpleNamespace(update_drain_active=False)
    coordinator, chats, *_ = _wired(tmp_path, app_state=app_state)

    assert coordinator._claim("auto-1") is True  # noqa: SLF001
    assert chats.restart_draining is True
    assert chats.drain_owner == "auto-1"
    # The marker is set *after* the synchronous close, never before it: the
    # cancel route reads it to decide whether an update owns the drain.
    assert app_state.update_drain_active is True

    # A second attempt cannot take the claim, and must not touch the marker.
    assert coordinator._claim("auto-2") is False  # noqa: SLF001
    assert chats.drain_owner == "auto-1"
    assert app_state.update_drain_active is True


def test_wire_auto_update_release_clears_it_only_after_reopen(tmp_path) -> None:
    app_state = SimpleNamespace(update_drain_active=False)
    coordinator, chats, *_ = _wired(tmp_path, app_state=app_state)

    assert coordinator._claim("auto-1") is True  # noqa: SLF001

    # Another operation's release is refused by the manager, so admission stays
    # closed and the route marker stays set with it.
    coordinator._release_claim("auto-2")  # noqa: SLF001
    assert chats.restart_draining is True
    assert app_state.update_drain_active is True

    # The owner's release reopens admission, and only then is the marker cleared.
    coordinator._release_claim("auto-1")  # noqa: SLF001
    assert chats.restart_draining is False
    assert app_state.update_drain_active is False

    # Releasing an already-open claim is harmless, not a marker toggled on.
    coordinator._release_claim("auto-1")  # noqa: SLF001
    assert app_state.update_drain_active is False


def test_wire_auto_update_returns_an_unstarted_coordinator(tmp_path) -> None:
    fetches: list[str] = []

    def fetch() -> str:
        fetches.append("fetch")
        return "2.0.16"

    async def scenario() -> AutoUpdateCoordinator:
        coordinator, chats, *_ = _wired(tmp_path, fetch_latest=fetch)
        # Wiring without an `app_state` is allowed (a CLI-shaped caller), and
        # claiming still works; it just has no route marker to keep in step.
        assert isinstance(coordinator, AutoUpdateCoordinator)
        # Only this coroutine: wiring must not create the loop's task, because
        # whether the engine runs it is `ciao.main`'s decision (never bootstrap).
        assert len(asyncio.all_tasks()) == 1
        assert coordinator._claim("auto-1") is True  # noqa: SLF001
        coordinator._release_claim("auto-1")  # noqa: SLF001
        return coordinator

    coordinator = asyncio.run(scenario())

    assert isinstance(coordinator, AutoUpdateCoordinator)
    # No tick ran: nothing was fetched, and a tick on this (Linux, therefore
    # unsupported) install would have scheduled the next look six hours out.
    assert fetches == []
    assert coordinator.next_wait_seconds() == 0
    # The support seam is the wired one: the platform override reaches the real
    # classifier, which refuses a Linux engine for its platform.
    status = coordinator.status()
    assert status["supported"] is False
    assert "macOS and Windows" in status["unsupported_reason"]

