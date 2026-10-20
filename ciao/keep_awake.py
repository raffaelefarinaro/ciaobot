"""Coordinate one host power assertion across active agent work."""

from __future__ import annotations

import logging

from ciao.os_support.power import PowerAssertion

logger = logging.getLogger(__name__)


class KeepAwakeController:
    """Hold a host power assertion while the opt-in setting and work are active.

    Work is tracked by idempotent tokens rather than a counter, so replacing a
    watcher or receiving a duplicate start/finish notification cannot leak or
    prematurely release the assertion.
    """

    def __init__(self, assertion: PowerAssertion | None, *, enabled: bool = False) -> None:
        self._assertion = assertion
        self._enabled = enabled
        self._active_work: set[str] = set()
        self._assertion_active = False
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def active_work(self) -> frozenset[str]:
        return frozenset(self._active_work)

    def set_enabled(self, enabled: bool) -> None:
        """Apply a Settings change immediately, including during active work."""
        if self._closed:
            return
        self._enabled = enabled
        self._sync()

    def work_started(self, work_id: str) -> None:
        """Register an active agent turn, subagent watcher, or background run."""
        if self._closed:
            return
        self._active_work.add(work_id)
        self._sync()

    def work_finished(self, work_id: str) -> None:
        """Release one work token without affecting concurrent work."""
        self._active_work.discard(work_id)
        self._sync()

    def close(self) -> None:
        """Release the OS assertion during graceful engine shutdown."""
        if self._closed:
            return
        self._closed = True
        self._enabled = False
        self._active_work.clear()
        self._sync()

    def _sync(self) -> None:
        assertion = self._assertion
        if assertion is None:
            return
        should_be_active = self._enabled and bool(self._active_work)
        if should_be_active == self._assertion_active:
            return
        if should_be_active:
            try:
                assertion.acquire()
            except Exception:  # noqa: BLE001 — power management must not fail agent work
                logger.exception("Could not prevent host idle sleep during agent work")
                return
            self._assertion_active = True
            return

        try:
            assertion.release()
        except Exception:  # noqa: BLE001 — a failed release must not block shutdown
            logger.exception("Could not release the host idle-sleep assertion")
        finally:
            self._assertion_active = False
