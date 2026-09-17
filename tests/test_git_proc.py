"""Tests for ciao/git_proc.py: timeout-safe git subprocess spawning.

The regression these pin down is issue #470: a git command that times out while
a grandchild (``git push`` forks ``ssh``) still holds the stdout/stderr pipe
write ends leaked two file descriptors per call, permanently. Against an
unreachable remote the 30s backup loop exhausted a 256-fd limit in about an
hour and every later subprocess spawn failed with EMFILE.

A fake ``git`` on PATH stands in for the real thing: it backgrounds a child
that survives a SIGKILL aimed at the script alone, which is exactly the shape
``git push`` over SSH has.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest

from ciao.git_proc import GIT_TIMEOUT_DETAIL, run_git


def _open_fd_count() -> int:
    """Number of file descriptors this process currently holds."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    pytest.skip("no fd directory available on this platform")


def _fake_git(tmp_path: Path, script: str) -> dict[str, str]:
    """A PATH whose `git` is ``script``; returns the env to run under."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    git = bin_dir / "git"
    git.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    git.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.mark.asyncio
async def test_timeout_does_not_leak_file_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated timeouts must not accumulate descriptors (issue #470).

    The fake git backgrounds a long sleep holding the pipes, then blocks
    itself — killing the script alone would leave the sleep holding the write
    ends and the parent's read ends open forever.
    """
    env = _fake_git(tmp_path, "sleep 300 &\nexec sleep 300")
    monkeypatch.setenv("PATH", env["PATH"])

    # One warm-up call so any lazily-created loop internals are already open.
    await run_git(tmp_path, "push", timeout=0.3)
    baseline = _open_fd_count()

    for _ in range(10):
        rc, out, err = await run_git(tmp_path, "push", timeout=0.3)
        assert rc == -1
        assert out == ""
        assert err == GIT_TIMEOUT_DETAIL

    # Allow the event loop a tick to finish any deferred transport teardown.
    await asyncio.sleep(0.1)
    leaked = _open_fd_count() - baseline
    assert leaked <= 2, f"leaked {leaked} descriptors across 10 timeouts"


@pytest.mark.asyncio
async def test_timeout_kills_the_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forked `ssh`-equivalent must die with git, not reparent to init."""
    pid_file = tmp_path / "grandchild.pid"
    env = _fake_git(
        tmp_path,
        f'sleep 300 &\necho $! > "{pid_file}"\nexec sleep 300',
    )
    monkeypatch.setenv("PATH", env["PATH"])

    rc, _, err = await run_git(tmp_path, "push", timeout=0.5)
    assert rc == -1
    assert err == GIT_TIMEOUT_DETAIL

    grandchild = int(pid_file.read_text().strip())
    # SIGKILL delivery is not instantaneous; give it a moment to be reaped.
    for _ in range(50):
        if not _alive(grandchild):
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(grandchild, signal.SIGKILL)  # don't leave it behind
        pytest.fail(f"grandchild {grandchild} survived the timeout")


@pytest.mark.asyncio
async def test_runs_in_its_own_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Group kill is only safe if git is not in *our* process group."""
    env = _fake_git(tmp_path, "ps -o pgid= -p $$")
    monkeypatch.setenv("PATH", env["PATH"])

    rc, out, _ = await run_git(tmp_path, "status")
    assert rc == 0
    assert int(out.strip()) != os.getpgid(0)


@pytest.mark.asyncio
async def test_success_returns_untrimmed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers apply their own whitespace convention, so don't pre-trim."""
    env = _fake_git(tmp_path, 'printf "  branch  \\n"')
    monkeypatch.setenv("PATH", env["PATH"])

    rc, out, err = await run_git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD")
    assert rc == 0
    assert out == "  branch  \n"
    assert err == ""


@pytest.mark.asyncio
async def test_nonzero_exit_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fake_git(tmp_path, 'echo "boom" >&2\nexit 3')
    monkeypatch.setenv("PATH", env["PATH"])

    rc, out, err = await run_git(tmp_path, "push")
    assert rc == 3
    assert out == ""
    assert err.strip() == "boom"


@pytest.mark.asyncio
async def test_no_timeout_waits_for_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a timeout the call still completes normally rather than hanging."""
    env = _fake_git(tmp_path, 'sleep 0.2\necho done')
    monkeypatch.setenv("PATH", env["PATH"])

    rc, out, _ = await run_git(tmp_path, "fetch")
    assert rc == 0
    assert out.strip() == "done"
