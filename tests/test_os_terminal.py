"""ciao.os_support.terminal: one terminal surface over the POSIX adapter.
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao.os_support import terminal
from ciao.os_support import terminal_posix
from ciao.os_support.terminal import TERMINAL_READ_BYTES, TerminalProcess


# ── helpers ──────────────────────────────────────────────────────────────────


class _Sentinel:
    """A terminal that exists only to be recognised.

    It carries the five members of the protocol, so a test that gets one back
    knows the facade really returned the adapter's own object.
    """

    def __init__(self) -> None:
        self.pid = 4242
        self.calls: list[dict[str, Any]] = []

    def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
        return None

    def write(self, data: bytes) -> int:
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        return None

    def poll(self) -> int | None:
        return None

    def close(self) -> None:
        return None


def _recording_spawn(sentinel: _Sentinel) -> Any:
    """A stand-in for an adapter's ``spawn_terminal`` that records the call."""

    def spawn(**kwargs: Any) -> _Sentinel:
        sentinel.calls.append(kwargs)
        return sentinel

    return spawn


# ── the real terminal of this platform ───────────────────────────────────────


def test_the_real_terminal_satisfies_the_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real terminal of the running platform, behind the one interface.

    ``/bin/sh`` is the shell the POSIX adapter runs: a line editor with its own
    idea of what a terminal is would redraw its prompt.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    term = terminal.spawn_terminal(cwd=tmp_path)
    try:
        assert isinstance(term, TerminalProcess)
        assert term.pid > 0
        # The protocol's read size is the one the adapter really uses, so the
        # two cannot drift apart silently.
        default = inspect.signature(type(term).read).parameters["size"].default
        assert default == TERMINAL_READ_BYTES
    finally:
        term.close()


# ── the seam: which adapter answers ──────────────────────────────────────────


def test_the_facade_calls_the_posix_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The POSIX adapter is the one that is called."""
    sentinel = _Sentinel()
    monkeypatch.setattr(terminal_posix, "spawn_terminal", _recording_spawn(sentinel))

    result = terminal.spawn_terminal(cwd=tmp_path, cols=100, rows=40)

    assert result is sentinel
    assert sentinel.calls == [{"cwd": tmp_path, "cols": 100, "rows": 40, "argv": None}]


# ── the protocol is structural ───────────────────────────────────────────────


class _FakeTerminal:
    """A terminal written by hand, with the five members of the protocol."""

    def __init__(self) -> None:
        self.pid = 7

    def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
        return None

    def write(self, data: bytes) -> int:
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        return None

    def poll(self) -> int | None:
        return None

    def close(self) -> None:
        return None


class _TerminalWithoutClose:
    """The same fake, with one member missing."""

    pid = 7

    def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
        return None

    def write(self, data: bytes) -> int:
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        return None

    def poll(self) -> int | None:
        return None


def test_the_protocol_is_structural() -> None:
    """A hand-written terminal satisfies the protocol; one short of it does not."""
    assert isinstance(_FakeTerminal(), TerminalProcess)
    assert not isinstance(_TerminalWithoutClose(), TerminalProcess)


# ── the facade adds no policy ────────────────────────────────────────────────


def test_an_adapter_refusal_reaches_the_caller_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is wrapped: the adapter's own error comes out as it is."""
    refusal = ValueError("cols must be between 20 and 500")

    def refusing_spawn(**kwargs: Any) -> _Sentinel:
        raise refusal

    monkeypatch.setattr(terminal_posix, "spawn_terminal", refusing_spawn)

    with pytest.raises(ValueError) as caught:
        terminal.spawn_terminal(cwd=tmp_path, cols=0)

    assert caught.value is refusal


def test_a_size_the_adapter_refuses_is_refused_by_the_facade(tmp_path: Path) -> None:
    """The real adapter refuses a bad size, and the facade passes it on."""
    with pytest.raises(ValueError, match="cols"):
        terminal.spawn_terminal(cwd=tmp_path, cols=0)


# ── only the chosen adapter is loaded ────────────────────────────────────────


def test_importing_the_facade_loads_no_adapter_at_all() -> None:
    """The facade names no adapter until one is asked for.

    The check runs in a fresh interpreter: this one has already imported
    both adapters, for the tests above.
    """
    script = (
        "import sys\n"
        "from ciao.os_support import terminal\n"
        "print('\\n'.join(sorted(m for m in sys.modules "
        "if m in ('ciao.os_support.terminal_posix',))))\n"
    )
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    # The checkout under test, not whatever copy is installed in the venv.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(repo) if not existing else f"{repo}{os.pathsep}{existing}"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=str(repo),
        env=env,
    )
    loaded = [line for line in result.stdout.split() if line]
    assert loaded == [], f"the facade loaded an adapter on import: {loaded!r}"
