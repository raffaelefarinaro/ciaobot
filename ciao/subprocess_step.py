"""A subprocess step runner that reports failures as steps, not exceptions.

Deploy helpers (the web deploy handler, its tests) need a command's outcome as
data — a step card with an ``ok`` flag and captured output — rather than as a
traceback. A missing binary and a timeout are the two cases that make that
necessary: both are ordinary operational outcomes for a step, and letting them
raise turns a structured report into a 500.

It used to live beside the macOS app rebuild, which is gone (#656); only the
deploy path still calls it.
"""

from __future__ import annotations

import subprocess


def run_step(args: list[str], *, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run ``args`` in ``cwd``, turning a missing binary or timeout into a
    failed ``CompletedProcess``.

    A missing binary becomes 127 and a timeout 124, the conventional shell
    codes for the same two conditions, so a caller can report a structured step
    instead of an exception.
    """
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="",
            stderr=f"{args[0]} not found on PATH: {exc}",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="",
            stderr=f"{args[0]} timed out after {timeout}s",
        )
