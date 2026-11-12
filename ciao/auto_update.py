"""Unattended engine updates: the policy, the platform guard and the loop.

The transaction (:mod:`ciao.engine_update`) already does one update correctly
when a person asks for it: stage, verify, drain, stop, swap, health-check,
roll back. What it never had was a reason to run on its own, and the two
reasons it could not be pointed at a timer are the whole of this module.

**Which installs may be updated unattended.** Not "does an update host exist"
— ``default_update_host()`` deliberately answers with the macOS host on any
non-Windows platform so the transaction's tests can run on Linux CI, and
``detect_install_mode()`` proves the interpreter matches a receipt without
noticing which service runs it. So :func:`auto_update_support` asks all four
questions in one place — platform, install mode, a receipt for *this*
interpreter, and the service backend that receipt names — and answers
"unsupported" with a sentence a Settings card can show. Everything else
(Linux, an editable checkout, a bundled app, a missing or stale receipt, a
wrong supervisor) is refused, never quietly treated as an installer.

**When it is safe to start one.** Staging is not a claim. A release can be
downloaded while people are still working; work that arrives during the
download wins, and the downloaded stage waits. The claim itself is the chat
manager's (``ProjectChatManager.claim_for_update``): one synchronous call that
proves the engine idle and closes admission with an owner token tied to the
operation id, with no ``await`` between the last policy read and the close.
Everything this module decides is arranged around that seam — see
:meth:`AutoUpdateCoordinator.tick`.

**What survives a restart.** The policy, the 24-hour hold, and the last
attempt's operation id, outcome and error live in :class:`ciao.app_settings.
AppSettingsStore`, so an engine that restarted mid-update can read the
operation record back, report the phase it ended in, and reopen admission only
as the owner that closed it. A tick that lands on an unreadable settings file
does nothing at all: defaults are not consent, and writing over the file would
erase a decision nobody could read.

The coordinator is constructed and tested here; :func:`wire_auto_update` is the
call ``ciao.main`` makes to hand it the real engine (the chat manager's claim,
the three subsystems whose work a restart would interrupt, and the route's
drain marker), and ``ciao.main`` owns the one task and its stop event. It never
ticks from a GET: the status route is read-only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ciao import __version__, engine_update, package_version
from ciao.app_settings import AppSettings, AppSettingsStore
from ciao.install_receipt import (
    InstallReceipt,
    receipt_matches_running,
    running_receipt,
)
from ciao.update_host import current_update_host

logger = logging.getLogger(__name__)

#: How often a healthy install looks for a new stable release. Six hours is the
#: cadence the PWA's own check already used; the loop exists because that check
#: only ran while a browser was connected.
CHECK_INTERVAL_SECONDS = 6 * 3600

#: How soon to look again after a verified stage could not be claimed, because
#: the engine had work in it. Short enough that an idle moment is used, long
#: enough that a busy engine is not polled.
BUSY_RETRY_SECONDS = 300

#: How long a claimed attempt may go without any operation record before its
#: outcome is called unknown and the claim is released. A live apply always
#: leaves a record behind (`ciao update status` shows it), so a missing one is
#: a process that died rather than one that is still working.
CLAIM_EXPIRY_SECONDS = 900

#: The "Not now" hold. Persisted, so it survives the restart it exists to avoid.
SNOOZE_SECONDS = 24 * 3600

#: Bounded exponential backoff for a failure that may be transient (an
#: unreachable release list, a failed download). Five minutes is the floor
#: because a check that fails instantly must not become a request loop, and six
#: hours is the ceiling because past it the backoff is worse than the cadence.
_MIN_BACKOFF_SECONDS = 300
_MAX_BACKOFF_SECONDS = 6 * 3600

#: The platforms with a supervisor the transaction can hand a swap to.
SUPPORTED_PLATFORMS = ("darwin", "win32")

#: The supervisor each platform must actually be using. A receipt naming
#: another one describes an install this engine cannot restart safely.
_PLATFORM_BACKENDS = {"darwin": "launchd", "win32": "windows-task"}

#: Phases after which the record describes a finished run. Anything else is a
#: run that is still going, or a record nobody can call finished.
_TERMINAL_PHASES = frozenset({"applied", "rolled_back", "rollback_failed", "failed"})

# The coordinator's own outcome words. Operation phases are recorded as-is, so
# "rolled_back" and "failed" mean what the transaction says they mean; these
# four cover the failures that happen before (or instead of) an operation.
OUTCOME_CHECK_FAILED = "check_failed"
OUTCOME_STAGE_FAILED = "stage_failed"
OUTCOME_FAILED = "failed"
OUTCOME_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AutoUpdateSupport:
    """Whether this install may be updated unattended, and why not.

    ``reason`` is a sentence, not a code: it is rendered in Settings as the
    explanation for a disabled control, so "unknown" would be a worse answer
    than saying which fact is missing.
    """

    supported: bool
    reason: str = ""


class _NoReceipt:
    """Marker type for "the caller named no receipt; read this process's own".

    Kept apart from ``None`` on purpose. ``None`` is the answer "there is no
    matching receipt", which is a refusal, while the default has to go and ask.
    """

    __slots__ = ()


_UNSET_RECEIPT = _NoReceipt()


def auto_update_support(
    platform: str | None = None,
    install_mode: str = "",
    receipt: object = _UNSET_RECEIPT,
    service_backend: str = "",
) -> AutoUpdateSupport:
    """Classify this install for unattended updates. Never raises.

    Pure with respect to the facts it is given: ``platform``, ``install_mode``,
    ``receipt`` and ``service_backend`` default to what this process can see,
    and a caller (or a test on another operating system) may pass any of them.
    """
    resolved_platform = sys.platform if platform is None else platform
    if resolved_platform not in SUPPORTED_PLATFORMS:
        return AutoUpdateSupport(
            False,
            f"automatic updates run on macOS and Windows only "
            f"(this engine is on {resolved_platform})",
        )

    mode = install_mode or package_version.detect_install_mode()
    if mode != "installer":
        return AutoUpdateSupport(
            False,
            "automatic updates need an engine installed by the Ciaobot engine "
            f"installer (this one is {mode or 'unknown'})",
        )

    known_receipt: InstallReceipt | None
    if isinstance(receipt, _NoReceipt):
        known_receipt = running_receipt()
    else:
        known_receipt = receipt if isinstance(receipt, InstallReceipt) else None
    if known_receipt is None:
        return AutoUpdateSupport(
            False,
            "there is no install receipt for this engine, so an update could "
            "not be verified or rolled back",
        )
    if not receipt_matches_running(known_receipt):
        return AutoUpdateSupport(
            False,
            "the install receipt on disk describes a different environment, so "
            "this is not a managed install of this engine",
        )

    expected = _PLATFORM_BACKENDS[resolved_platform]
    backend = service_backend or known_receipt.service_backend
    if backend != expected:
        return AutoUpdateSupport(
            False,
            f"automatic updates need the {expected} service on this platform "
            f"(this engine is supervised by {backend or 'nothing'})",
        )
    return AutoUpdateSupport(True)


def fetch_latest_stable() -> str:
    """The latest stable release version, or an exception explaining why not.

    Deliberately not :func:`ciao.package_version.make_cached_package_status`:
    that cache serves the last good answer when a refresh fails, which is right
    for a card and wrong here — a stale answer would look like a successful
    fresh check and license an automatic download from an unknown release.
    """
    status = package_version.package_status()
    error = str(status.get("error") or "")
    if error:
        raise engine_update.UpdateError(f"could not check for a new release: {error}")
    latest = str(status.get("latest_version") or "")
    if not latest:
        raise engine_update.UpdateError("could not determine the latest release")
    return latest


def spawn_detached_apply(attempt_id: str) -> None:
    """Hand a claimed, staged update to the existing apply transaction.

    The transaction is not reimplemented here: this starts
    :func:`ciao.engine_update.apply_update` on its own thread with
    ``drain_owner`` set to the attempt's operation id, and that call does the
    rest — it re-asserts the drain (the claim already closed admission), waits
    for three idle readings, and spawns the one-shot updater *job*. The job is
    what is detached: a sibling of the engine's own service, so booting the
    engine out to swap its files cannot take the updater with it. The thread is
    the foreground half only, and it ends as soon as the job is loaded.

    Starting a thread can fail (a thread limit, no host on this platform); it
    raises here so the coordinator can release the claim it just took. What
    happens after the thread starts is the operation record's business, and the
    next tick reconciles it.
    """

    def _run() -> None:
        try:
            engine_update.apply_update(
                drain_owner=attempt_id,
                host=current_update_host(),
            )
        except Exception as exc:  # noqa: BLE001 — the record is the outcome
            logger.warning("automatic update %s could not apply: %s", attempt_id, exc)

    thread = threading.Thread(
        target=_run, name="ciao-auto-update-apply", daemon=True
    )
    thread.start()


class _UpdateAdmission(Protocol):
    """The engine's own claim, as ``ProjectChatManager`` implements it."""

    @property
    def restart_draining(self) -> bool:
        """Whether admission is closed, whoever closed it."""
        ...

    def claim_for_update(self, owner: str) -> bool:
        """Prove the engine idle and close admission, owned by ``owner``."""
        ...

    def cancel_restart_drain(self, owner: str | None = None) -> bool:
        """Reopen admission when ``owner`` is the one that closed it."""
        ...

    def register_update_blocker_probe(self, probe: Callable[[], list[str]]) -> None:
        """Add a subsystem's idle proof to the claim's snapshot."""
        ...


class _UpdateProbe(Protocol):
    """A subsystem that knows whether a restart would interrupt it.

    Schedules, backups and the memory-pass queue each answer with one reason
    per unit of work. The reason is a sentence because it ends up in the claim
    refusal (and in a log), where "busy" would say nothing an operator could act
    on.
    """

    def update_idle_reasons(self) -> list[str]:
        """Why this subsystem is not idle, empty when it is."""
        ...


class _RouteState(Protocol):
    """The part of ``app.state`` the drain routes read.

    The coordinator itself does not care about the marker — the routes gate the
    manual cancel on it — so the wiring keeps the two in step instead of having
    the claim wrapper reach into Starlette.
    """

    update_drain_active: bool


def _unwired_claim(owner: str) -> bool:
    """The claim of an engine that never handed the coordinator its manager.

    Raising rather than answering "no" is the point: the caller records an
    actionable failure instead of retrying a stage it can never claim, and a
    wiring mistake is visible in Settings rather than silent.
    """
    raise RuntimeError(
        "automatic updates are not wired to the engine's admission; the engine "
        "must give the coordinator its chat manager before it starts the loop"
    )


async def _wait_for_stop(seconds: float, stop: asyncio.Event) -> None:
    """Sleep up to ``seconds``, waking the moment ``stop`` is set."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def _stamp(seconds: float) -> str:
    """A wall-clock second count as the ISO-8601 UTC string the store holds."""
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _parse_stamp(value: str) -> float | None:
    """The epoch seconds an ISO-8601 stamp names, or None when it names none."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _is_newer(latest: str, current: str) -> bool:
    """Whether ``latest`` is a later release than ``current``.

    The comparison is the one the update check already uses, so "newer" cannot
    mean two different things in the card and in the coordinator.
    """
    return package_version._version_key(latest) > package_version._version_key(current)


class AutoUpdateCoordinator:
    """One engine's unattended update loop.

    Everything that touches the outside world is injected: the store, the wall
    clock, staging, the apply handoff, the release lookup, the operation record,
    the engine's admission claim, and the wait. That is what lets the tests
    drive whole lifecycles — restarts, sleeps, clockwise and anticlockwise
    jumps, refused claims — without a network, a receipt, a supervisor, or a
    real engine.
    """

    def __init__(
        self,
        *,
        store: AppSettingsStore,
        clock: Callable[[], float] = time.time,
        stage: Callable[[str], engine_update.Operation] | None = None,
        spawn_apply: Callable[[str], None] | None = None,
        fetch_latest: Callable[[], str] | None = None,
        read_operation: Callable[[], engine_update.Operation | None] | None = None,
        claim: Callable[[str], bool] | None = None,
        release_claim: Callable[[str], None] | None = None,
        support: Callable[[], AutoUpdateSupport] | None = None,
        current_version: Callable[[], str] | None = None,
        chats: _UpdateAdmission | None = None,
        wait: Callable[[float, asyncio.Event], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._stage_fn = stage or (lambda version: engine_update.stage_update(version))
        self._spawn_apply = spawn_apply or spawn_detached_apply
        self._fetch_latest = fetch_latest or fetch_latest_stable
        self._read_operation = read_operation or engine_update.read_operation
        self._claim = claim or (
            chats.claim_for_update if chats is not None else _unwired_claim
        )
        self._release_claim = release_claim or (
            (lambda owner: chats.cancel_restart_drain(owner))
            if chats is not None
            else (lambda owner: None)
        )
        self._support = support or (lambda: auto_update_support())
        self._current_version = current_version or (lambda: __version__)
        self._wait = wait or _wait_for_stop
        # Consecutive failures in this process. The *schedule* is durable
        # (`auto_update_next_check_at`), so a restart never retries sooner than
        # the backoff already recorded; the streak itself starts over, because a
        # restart is a fresh look at a machine that may have changed.
        self._failures = 0
        # An in-memory floor for the next tick, used where no settings write is
        # allowed at all (an unreadable file).
        self._blocked_until = 0.0

    # ── the policy surface ───────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """What Settings shows: the policy, the install, and the last attempt.

        Read-only and cheap enough for a polled route. ``enabled`` is false
        when the settings file could not be read, because "on" is a decision
        and an unreadable file is the absence of one; ``error`` carries why.
        """
        settings = self._store.settings
        try:
            support = self._support()
        except Exception as exc:  # noqa: BLE001 — a card must still render
            support = AutoUpdateSupport(
                False, f"could not check this install for automatic updates: {exc}"
            )
        return {
            "enabled": bool(settings.auto_update_enabled) and not self._store.load_error,
            "supported": support.supported,
            "unsupported_reason": support.reason,
            "snooze_until": settings.auto_update_snooze_until,
            "outcome": settings.auto_update_outcome,
            "error": self._store.load_error or settings.auto_update_error,
            "attempted_at": settings.auto_update_attempt_at,
        }

    def set_enabled(self, enabled: bool) -> None:
        """Persist the standing decision. Never touches an active snooze."""
        self._store.update({"auto_update_enabled": bool(enabled)})

    def snooze(self, seconds: int = SNOOZE_SECONDS) -> None:
        """Persist "not now" until ``now + seconds``. Never clears the policy."""
        self._store.update(
            {"auto_update_snooze_until": _stamp(self._clock() + seconds)}
        )

    # ── the loop ────────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until ``stop``, waiting between ticks on the wall clock.

        The wait is recomputed from the stored next-check time and the hold, not
        counted down from a monotonic timer: on macOS a monotonic timer stops
        while the machine sleeps, so a laptop would wake to an update check
        scheduled for the day it was closed. A backwards clock jump makes the
        wait longer (nothing is due yet), never a burst of ticks.
        """
        while not stop.is_set():
            await self.tick()
            if stop.is_set():
                return
            await self._wait(max(self.next_wait_seconds(), 1.0), stop)

    def next_wait_seconds(self) -> float:
        """Seconds until the next useful tick, read from the persisted clocks."""
        now = self._clock()
        settings = self._store.settings
        if self._store.load_error:
            # Nothing may be written, so the floor lives in memory.
            return max(self._blocked_until - now, BUSY_RETRY_SECONDS)
        if not settings.auto_update_enabled:
            # A turned-off updater still looks every five minutes, so turning it
            # back on takes effect then rather than at the next six-hour check.
            return BUSY_RETRY_SECONDS
        due = _parse_stamp(settings.auto_update_next_check_at) or now
        snooze = _parse_stamp(settings.auto_update_snooze_until)
        if snooze is not None and snooze > now:
            due = max(due, snooze)
        due = max(due, self._blocked_until)
        return max(due - now, 0.0)

    # ── one tick ────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """One policy evaluation. Never raises; every failure is recorded.

        The order is load-bearing and is the same one the plan fixes: read the
        durable policy, reconcile a previous attempt, refuse unsupported or
        turned-off installs, honour the hold and the backoff, check for a newer
        stable release, re-read the policy, stage, re-read it again, claim, and
        only then hand over. A stage surviving a refusal is deliberate: the
        download is not the claim.
        """
        try:
            await self._tick()
        except Exception as exc:  # noqa: BLE001 — a raising loop stops updating forever
            logger.exception("automatic update tick failed")
            self._record_failure(
                OUTCOME_CHECK_FAILED, f"the automatic update check failed: {exc}"
            )

    async def _tick(self) -> None:
        settings = self._store.settings
        if self._store.load_error:
            # Fail closed. Writing here would replace the unreadable file with
            # this process's defaults, which is exactly the "silently flipped
            # opt-out" the durable contract forbids.
            logger.warning("automatic update skipped: %s", self._store.load_error)
            self._blocked_until = self._clock() + BUSY_RETRY_SECONDS
            return

        if self._reconcile(settings):
            return

        if not settings.auto_update_enabled:
            return

        try:
            support = self._support()
        except Exception as exc:  # noqa: BLE001 — unknown is unsupported
            self._record_failure(
                OUTCOME_CHECK_FAILED,
                f"could not check whether this install can update automatically: {exc}",
            )
            return
        if not support.supported:
            # Not an error: there is nothing to update and nothing to fix.
            self._persist(
                {
                    "auto_update_next_check_at": _stamp(
                        self._clock() + CHECK_INTERVAL_SECONDS
                    )
                }
            )
            return

        now = self._clock()
        if self._snoozed(settings, now):
            return
        next_check = _parse_stamp(settings.auto_update_next_check_at)
        if next_check is not None and now < next_check:
            return

        # A fresh answer, never a cached one: serving the last good result of a
        # failed check would look like a successful check and license a download.
        try:
            latest = await asyncio.to_thread(self._fetch_latest)
        except Exception as exc:  # noqa: BLE001 — transient by nature
            self._record_failure(
                OUTCOME_CHECK_FAILED,
                f"the automatic update check could not find the latest release: {exc}",
            )
            return
        if not latest:
            self._record_failure(
                OUTCOME_CHECK_FAILED,
                "the automatic update check found no stable release to compare against",
            )
            return

        current = self._current_version()
        # Whatever happens next, the next automatic look is the normal cadence;
        # every later branch may shorten it (busy) or lengthen it (failure).
        self._persist(
            {"auto_update_next_check_at": _stamp(self._clock() + CHECK_INTERVAL_SECONDS)}
        )
        self._failures = 0
        if not _is_newer(latest, current):
            self._clear_check_failure(settings)
            return

        # Re-read the live policy: the check above took long enough for someone
        # to have turned automatic updates off or asked for "not now".
        now = self._clock()
        settings = self._store.settings
        if not settings.auto_update_enabled or self._snoozed(settings, now):
            return

        operation = await self._stage(latest)
        if operation is None:
            return

        # From here to the claim there is no `await`. The policy read and the
        # synchronous claim are one decision: a PATCH cannot land between them,
        # and the claim cannot be taken against a policy that has moved.
        now = self._clock()
        settings = self._store.settings
        if not settings.auto_update_enabled or self._snoozed(settings, now):
            return

        attempt_id = operation.id
        try:
            claimed = self._claim(attempt_id)
        except Exception as exc:  # noqa: BLE001 — an unwired engine fails closed
            self._record_failure(
                OUTCOME_FAILED,
                f"the automatic update could not close admission first: {exc}",
            )
            return
        if not claimed:
            # Work is in it. The verified stage stays: staging is not a claim,
            # and the next tick applies this same stage once the engine is idle.
            self._persist(
                {"auto_update_next_check_at": _stamp(now + BUSY_RETRY_SECONDS)}
            )
            return

        self._persist(
            {
                "auto_update_attempt_id": attempt_id,
                "auto_update_attempt_at": _stamp(now),
                "auto_update_outcome": "",
                "auto_update_error": "",
            }
        )
        try:
            self._spawn_apply(attempt_id)
        except Exception as exc:  # noqa: BLE001 — nothing owns the claim now
            self._release(attempt_id)
            self._record_failure(
                OUTCOME_FAILED,
                f"the automatic update to {latest} could not be started: {exc}",
            )
            return
        logger.info(
            "automatic update %s handed to the detached applier", attempt_id
        )

    # ── the attempt record ──────────────────────────────────────────────

    def _reconcile(self, settings: AppSettings) -> bool:
        """Settle a previous attempt. True when it owned this tick.

        A matching record is the only proof of what the apply is doing. Its
        terminal phase is the outcome; a phase that is not terminal is a swap
        still in flight and is never aged out here, because releasing a claim
        under a live updater would reopen admission while it swaps the install.
        A record that is *absent* (or belongs to another run) proves nothing, so
        it becomes "unknown" — but only once the claim is old enough that a
        record would have been written.
        """
        attempt_id = settings.auto_update_attempt_id
        if not attempt_id:
            return False
        try:
            operation = self._read_operation()
        except Exception:  # noqa: BLE001 — unknown, handled below
            operation = None
        if operation is not None and operation.id == attempt_id:
            if operation.phase in _TERMINAL_PHASES:
                self._finish_attempt(
                    attempt_id, outcome=operation.phase, error=operation.error
                )
                return True
            # A swap in flight stops this engine before it swaps, so a
            # non-terminal record for a claim that old, observed from a live
            # engine, means the applier stopped reporting without ever taking
            # the engine down. Holding admission closed for it would refuse
            # every turn forever with no one left to notice; the bounded release
            # is the lesser failure and the outcome says so.
            attempt_at = _parse_stamp(settings.auto_update_attempt_at)
            if attempt_at is not None and (
                self._clock() - attempt_at >= CLAIM_EXPIRY_SECONDS
            ):
                self._finish_attempt(
                    attempt_id,
                    outcome=OUTCOME_UNKNOWN,
                    error=(
                        "the automatic update stopped reporting while this engine "
                        "kept running, so its claim was released; check the "
                        "installed version"
                    ),
                )
            return True
        attempt_at = _parse_stamp(settings.auto_update_attempt_at)
        if attempt_at is None or self._clock() - attempt_at >= CLAIM_EXPIRY_SECONDS:
            self._finish_attempt(
                attempt_id,
                outcome=OUTCOME_UNKNOWN,
                error=(
                    "the automatic update's operation record is gone, so its "
                    "outcome is unknown; check the installed version"
                ),
            )
        return True

    def _finish_attempt(self, attempt_id: str, *, outcome: str, error: str) -> None:
        """Record the outcome and give the claim back.

        The release comes first, and deliberately: if reopening admission fails,
        the attempt id is still on disk, so the next tick reconciles the same
        finished record and retries the release instead of leaving the engine
        refusing every turn with nothing left to notice it.
        """
        self._release(attempt_id)
        self._persist(
            {
                "auto_update_attempt_id": "",
                "auto_update_attempt_at": "",
                "auto_update_outcome": outcome,
                "auto_update_error": error,
                "auto_update_next_check_at": _stamp(
                    self._clock() + CHECK_INTERVAL_SECONDS
                ),
            }
        )
        logger.info("automatic update %s finished: %s", attempt_id, outcome)

    def _release(self, attempt_id: str) -> None:
        """Reopen admission through the owner path; a failure is logged, not fatal."""
        try:
            self._release_claim(attempt_id)
        except Exception as exc:  # noqa: BLE001 — retried by the next tick
            logger.warning(
                "could not reopen admission after automatic update %s: %s",
                attempt_id,
                exc,
            )

    # ── staging ─────────────────────────────────────────────────────────

    async def _stage(self, latest: str) -> engine_update.Operation | None:
        """A verified stage of ``latest``, reusing one a refused claim left.

        Re-downloading would be worse than useless: a release is staged once,
        and its stage directory holds the interpreter the updater and the
        recovery job run from. A record in a phase that is still running means
        another update owns the lock and the staged env, so this tick defers
        rather than racing it.
        """
        try:
            record = self._read_operation()
        except Exception as exc:  # noqa: BLE001 — unknown is not "safe to stage"
            self._record_failure(
                OUTCOME_STAGE_FAILED,
                f"could not read the update record before staging: {exc}",
            )
            return None
        if record is not None:
            if record.phase == "staged" and record.to_version == latest:
                return record
            # A `staged` record for another release is a download waiting for
            # someone, not an apply in flight: the newer release is staged
            # normally (each version has its own stage directory). Every other
            # non-terminal phase is a run that owns the lock and the staged env.
            if record.phase not in _TERMINAL_PHASES and record.phase != "staged":
                self._persist(
                    {
                        "auto_update_next_check_at": _stamp(
                            self._clock() + BUSY_RETRY_SECONDS
                        )
                    }
                )
                return None
        try:
            operation = await asyncio.to_thread(self._stage_fn, latest)
        except Exception as exc:  # noqa: BLE001 — a bad manifest is not a crash
            self._record_failure(
                OUTCOME_STAGE_FAILED,
                f"could not stage the update to {latest}: {exc}",
            )
            return None
        if operation.phase != "staged":
            self._record_failure(
                OUTCOME_STAGE_FAILED,
                f"staging {latest} ended in {operation.phase}"
                + (f": {operation.error}" if operation.error else ""),
            )
            return None
        return operation

    # ── small policy helpers ────────────────────────────────────────────

    def _snoozed(self, settings: AppSettings, now: float) -> bool:
        until = _parse_stamp(settings.auto_update_snooze_until)
        return until is not None and now < until

    def _record_failure(self, outcome: str, message: str) -> None:
        """Persist an actionable failure and the bounded backoff that follows it."""
        self._failures += 1
        delay = min(
            _MAX_BACKOFF_SECONDS, _MIN_BACKOFF_SECONDS * 2 ** (self._failures - 1)
        )
        now = self._clock()
        self._persist(
            {
                "auto_update_attempt_id": "",
                "auto_update_attempt_at": _stamp(now),
                "auto_update_outcome": outcome,
                "auto_update_error": message,
                "auto_update_next_check_at": _stamp(now + delay),
            }
        )
        logger.info("automatic update: %s (next check in %ss)", message, delay)

    def _clear_check_failure(self, settings: AppSettings) -> None:
        """Drop an old check/stage failure once a check succeeds again.

        A network error that recovered is not news, and leaving it in Settings
        would describe a broken updater that is working. An operation's own
        outcome is kept: that is the last attempt's result, not a stale check.
        """
        if settings.auto_update_outcome not in {
            OUTCOME_CHECK_FAILED,
            OUTCOME_STAGE_FAILED,
        }:
            return
        self._persist({"auto_update_outcome": "", "auto_update_error": ""})

    def _persist(self, changes: dict[str, object]) -> None:
        """Write attempt metadata, never over an unreadable settings file."""
        if self._store.load_error:
            return
        try:
            self._store.update(changes)
        except (OSError, ValueError) as exc:
            # The record is bookkeeping; losing one write must not end the loop.
            logger.warning("could not record automatic update state: %s", exc)


def wire_auto_update(
    *,
    store: AppSettingsStore,
    chats: _UpdateAdmission,
    schedules: _UpdateProbe,
    backup: _UpdateProbe,
    memory_passes: _UpdateProbe,
    app_state: _RouteState | None = None,
    platform: str | None = None,
    **overrides: Any,
) -> AutoUpdateCoordinator:
    """Build the one coordinator ``ciao.main`` starts, wired to the real engine.

    A function rather than a block in ``main`` so the wiring itself is testable:
    what the coordinator is given is exactly what decides whether it can claim
    (the chat manager), what it must account for before claiming (the three
    subsystem probes) and how it reports a claim to the drain routes
    (``update_drain_active``). Nothing here starts anything — the caller owns the
    task and its stop event, because the engine must not run this loop in
    bootstrap mode or in a CLI-only process.

    The claim/release pair is the slice-2 seam: ``claim_for_update`` is
    synchronous and closes admission itself, so the route marker is set only
    *after* it returns True; a marker set earlier would tell the cancel route
    that an update owns a drain that never happened. Release asks the manager to
    reopen, and clears the marker only if admission really is open again — a
    refused owner (not this attempt's claim) leaves both the claim and the
    marker alone.

    ``**overrides`` are handed to :class:`AutoUpdateCoordinator` unchanged, which
    is how a test drives the whole lifecycle through this function without a
    network, a receipt or a supervisor. ``platform`` is likewise an override for
    the install classifier, and defaults to this process's own platform.
    """

    def _schedule_probe() -> list[str]:
        return list(schedules.update_idle_reasons())

    def _backup_probe() -> list[str]:
        return list(backup.update_idle_reasons())

    def _memory_pass_probe() -> list[str]:
        return list(memory_passes.update_idle_reasons())

    # Registered before the coordinator exists, so it cannot tick against an
    # engine whose busy subsystems it cannot see. Each probe raises rather than
    # guessing when its subsystem cannot answer, and the claim fails closed.
    chats.register_update_blocker_probe(_schedule_probe)
    chats.register_update_blocker_probe(_backup_probe)
    chats.register_update_blocker_probe(_memory_pass_probe)

    def _claim(owner: str) -> bool:
        claimed = chats.claim_for_update(owner)
        if claimed and app_state is not None:
            app_state.update_drain_active = True
        return claimed

    def _release_claim(owner: str) -> None:
        chats.cancel_restart_drain(owner)
        if app_state is not None and not chats.restart_draining:
            app_state.update_drain_active = False

    def _support() -> AutoUpdateSupport:
        # Read the receipt once: the classifier takes its backend from the same
        # record it proves against this interpreter, so asking twice could
        # classify a receipt that changed in between.
        receipt = running_receipt()
        return auto_update_support(
            platform=platform,
            install_mode=package_version.detect_install_mode(),
            receipt=receipt,
            service_backend=receipt.service_backend if receipt is not None else "",
        )

    seams: dict[str, Any] = {
        "store": store,
        "claim": _claim,
        "release_claim": _release_claim,
        "support": _support,
        "current_version": lambda: __version__,
        "stage": lambda version: engine_update.stage_update(version),
        "spawn_apply": spawn_detached_apply,
        "fetch_latest": fetch_latest_stable,
        "read_operation": engine_update.read_operation,
    }
    seams.update(overrides)
    return AutoUpdateCoordinator(**seams)
