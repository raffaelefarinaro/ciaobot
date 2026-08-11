"""How a failed Restart step is reported back to Settings.

The bug these pin: a failed `pip install` rendered as a wall of pip's own
progress chatter ("Preparing editable metadata...") with the actual error
missing, because the output was truncated from the head and stderr was
discarded whenever stdout had content.
"""

from __future__ import annotations

import logging
import subprocess

from ciao.web.routes_api import (
    _DEPLOY_STEP_OUTPUT_CHARS,
    _pip_install_hint,
    _record_step,
)


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["pip", "install", "-e", "."],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_record_step_keeps_the_tail_not_the_head() -> None:
    """The diagnosis is the last thing a build tool prints."""
    chatter = "\n".join(f"  Installing build dependencies: step {i}" for i in range(500))
    result = _completed(1, stdout=f"{chatter}\nERROR: the actual cause")

    step = _record_step("pip install", result)

    assert step["ok"] is False
    assert "ERROR: the actual cause" in step["output"]
    assert len(step["output"]) <= _DEPLOY_STEP_OUTPUT_CHARS + len("[earlier output trimmed]\n")
    assert step["output"].startswith("[earlier output trimmed]")


def test_record_step_keeps_stderr_even_when_stdout_is_noisy() -> None:
    """pip writes progress to stdout and the error to stderr.

    `stdout or stderr` therefore dropped the error every single time, since
    stdout is never empty for a pip run that got far enough to fail.
    """
    result = _completed(
        1,
        stdout="Obtaining file:///repo\n  Installing build dependencies: started",
        stderr="ERROR: Cannot uninstall ciaobot 0.7.0",
    )

    step = _record_step("pip install", result)

    assert "Cannot uninstall ciaobot 0.7.0" in step["output"]
    assert "Obtaining file:///repo" in step["output"]


def test_record_step_logs_the_full_untruncated_output(caplog) -> None:
    """The response used to be the only copy of a failure, and it was trimmed."""
    result = _completed(1, stdout="a" * (_DEPLOY_STEP_OUTPUT_CHARS + 100), stderr="ERROR: boom")

    with caplog.at_level(logging.ERROR, logger="ciao.web.routes_api"):
        _record_step("pip install", result)

    logged = caplog.text
    assert "ERROR: boom" in logged
    assert "pip install" in logged


def test_record_step_stays_quiet_on_success(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="ciao.web.routes_api"):
        step = _record_step("npm build", _completed(0, stdout="built"))

    assert step == {"step": "npm build", "ok": True, "output": "built"}
    assert caplog.text == ""


def test_pip_hint_explains_the_homebrew_record_case() -> None:
    """Homebrew's dist-info has no RECORD, so pip refuses to uninstall it.

    Nothing is wrong with the checkout, so the raw pip text sends the reader
    to debug the wrong thing.
    """
    output = (
        "ERROR: Cannot uninstall ciaobot 0.7.0, RECORD file not found. "
        "Hint: The package was installed by a different tool."
    )

    hint = _pip_install_hint(output)

    assert "Homebrew" in hint
    assert "RECORD" in hint


def test_pip_hint_is_empty_for_an_unrelated_failure() -> None:
    """No guess when the failure is something else — an empty hint means the
    caller falls back to the plain headline plus the step output."""
    assert _pip_install_hint("ERROR: could not resolve dependency foo==1.0") == ""
