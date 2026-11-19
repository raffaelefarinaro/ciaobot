"""ciao.main's restart watchdog: supervised exits, unsupervised re-execs.

``_restart_watchdog`` only runs after the restart drain, so it is called here
directly with the sleep/exec/exit seams passed in: no monkeypatching, no
process is ever replaced or killed, and no test sleeps.
"""

from __future__ import annotations

import sys

import pytest

from ciao import main
from ciao.main import _restart_watchdog


class _NeverExec:
    """Stands in for ``os.execv``, which must not run in these paths."""

    def __call__(self, exe: str, argv: list[str]) -> object:  # pragma: no cover
        raise AssertionError("execv must not be called here")


def test_supervised_watchdog_exits_with_the_restart_code_and_never_execs() -> None:
    """Under `ciao supervise` an exec would bypass the supervisor's relaunch
    loop and crash-loop backoff, so the child only reports the code."""
    exits: list[int] = []

    _restart_watchdog(
        75,
        supervised=True,
        sleep=lambda s: None,
        execv=_NeverExec(),
        exit_now=exits.append,
    )

    assert exits == [75]


def test_unsupervised_watchdog_re_execs_as_before() -> None:
    """A plain `ciao run` under launchd has no supervisor to relaunch it, so
    the watchdog re-execs a fresh interpreter under the same pid."""
    execs: list[tuple[str, list[str]]] = []
    exits: list[int] = []

    _restart_watchdog(
        75,
        supervised=False,
        sleep=lambda s: None,
        execv=lambda exe, argv: execs.append((exe, argv)),
        exit_now=exits.append,
    )

    assert len(execs) == 1
    exe, argv = execs[0]
    assert exe == sys.executable
    assert argv[:3] == [sys.executable, "-m", "ciao.cli"]
    assert exits == []


def test_unsupervised_watchdog_falls_back_to_exit_when_exec_fails() -> None:
    """An exec can fail outright (bad interpreter path); the restart still
    has to happen, so exit with the restart code instead."""

    def _fail_execv(exe: str, argv: list[str]) -> object:
        raise OSError("exec failed")

    exits: list[int] = []

    _restart_watchdog(
        75,
        supervised=False,
        sleep=lambda s: None,
        execv=_fail_execv,
        exit_now=exits.append,
    )

    assert exits == [75]


@pytest.mark.parametrize("supervised", [True, False])
def test_clean_exit_request_exits_zero_supervised_or_not(supervised: bool) -> None:
    """A clean-exit request (the setup wizard handing the server over to
    launchd) means dying is the point, so never relaunch."""
    exits: list[int] = []

    _restart_watchdog(
        0,
        supervised=supervised,
        sleep=lambda s: None,
        execv=_NeverExec(),
        exit_now=exits.append,
    )

    assert exits == [0]


def test_watchdog_waits_the_grace_period_first() -> None:
    """Cleanup gets the full grace before anything is forced."""
    slept: list[float] = []

    _restart_watchdog(
        75,
        supervised=True,
        sleep=slept.append,
        execv=_NeverExec(),
        exit_now=lambda code: None,
    )

    assert slept == [main.RESTART_WATCHDOG_GRACE_S]
