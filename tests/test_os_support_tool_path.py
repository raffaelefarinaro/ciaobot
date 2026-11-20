"""ciao.os_support.tool_path: the login-shell PATH probe, the curated tool dirs and the ``shutil.which`` lookups.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from ciao.os_support import tool_path as os_tool_path


def test_dedupe_path_keeps_first_occurrences_in_order() -> None:
    """Each directory once, where it first appears; empty entries dropped."""
    path = os.pathsep.join(["/a", "", "/b", "/a", "/c", "/b", ""])
    assert os_tool_path.dedupe_path(path) == os.pathsep.join(["/a", "/b", "/c"])


def test_dedupe_path_is_stable_when_composed_twice() -> None:
    """Composing an already-composed PATH yields the same PATH."""
    once = os_tool_path.dedupe_path(os.pathsep.join(["/nvm/bin", "/usr/bin", "/nvm/bin"]))
    assert os_tool_path.dedupe_path(once) == once
    assert once.split(os.pathsep) == ["/nvm/bin", "/usr/bin"]


def test_dedupe_path_collapses_a_directory_repeated_many_times() -> None:
    """The reported shape: one node bin dir repeated ahead of everything else."""
    node_bin = "/Users/u/.nvm/versions/node/v22.23.2/bin"
    inherited = os.pathsep.join([node_bin] * 30 + ["/usr/bin", "/bin"])
    assert os_tool_path.dedupe_path(inherited) == os.pathsep.join([node_bin, "/usr/bin", "/bin"])


# ── the login-shell probe ──


def _probe_counter(
    monkeypatch: pytest.MonkeyPatch, value: str = "/usr/bin:/opt/homebrew/bin"
) -> list[int]:
    """Replace the login-shell spawn with a counter."""
    calls: list[int] = []

    def fake_probe() -> str:
        calls.append(1)
        return value

    monkeypatch.setattr(os_tool_path, "_probe_terminal_path", fake_probe)
    os_tool_path.clear_terminal_path_cache()
    return calls


def test_terminal_path_reads_the_shell_output_through_the_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noisy rc file echoes banners to stdout; the markers keep them out."""

    class _Result:
        returncode = 0
        stdout = f"banner\n{os_tool_path._START}/usr/local/bin{os_tool_path._END}\ntrailing"
        stderr = ""

    monkeypatch.setattr(os_tool_path.subprocess, "run", lambda *a, **k: _Result())
    os_tool_path.clear_terminal_path_cache()
    assert os_tool_path.terminal_path() == "/usr/local/bin"
    os_tool_path.clear_terminal_path_cache()


def test_terminal_path_does_not_inherit_our_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine prepends common_tool_dirs() to its own PATH at startup
    (ciao/main.py). Handing that to the probe would make ~/.local/bin come back
    as if the user's terminal had it, and setup would then tell them to type a
    bare command their shell cannot resolve."""
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = f"{os_tool_path._START}/usr/bin{os_tool_path._END}"
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(os_tool_path.subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert os_tool_path.terminal_path() == "/usr/bin"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "PATH" not in env
    # Everything else the rc files may need is still there.
    assert env["HOME"] == os.environ["HOME"]
    os_tool_path.clear_terminal_path_cache()


def test_terminal_path_does_not_respawn_a_shell_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard polls every 2s; each poll used to cost a ~0.74s login shell."""
    calls = _probe_counter(monkeypatch)
    assert os_tool_path.terminal_path() == "/usr/bin:/opt/homebrew/bin"
    for _ in range(20):
        os_tool_path.terminal_path()
    assert len(calls) == 1


def test_terminal_path_reprobes_when_an_rc_file_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who adds ~/.local/bin to their rc file must see the wizard's PATH
    hint clear on the next poll, without an engine restart."""
    home = tmp_path / "home"
    (home / ".config" / "fish").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    calls = _probe_counter(monkeypatch)

    os_tool_path.terminal_path()
    os_tool_path.terminal_path()
    assert len(calls) == 1

    # Creating a previously absent rc file counts, not just touching one.
    (home / ".zshrc").write_text(
        'export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8"
    )
    os_tool_path.terminal_path()
    assert len(calls) == 2

    # And a later edit to it.
    os.utime(home / ".zshrc", (0, 0))
    os_tool_path.terminal_path()
    assert len(calls) == 3


def test_terminal_path_reprobes_when_the_shell_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """$SHELL decides which rc files are read at all."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    calls = _probe_counter(monkeypatch)
    os_tool_path.terminal_path()
    os_tool_path.terminal_path()
    assert len(calls) == 1

    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    os_tool_path.terminal_path()
    assert len(calls) == 2


def test_rc_fingerprint_survives_an_unreadable_paths_d(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandboxed `ciao gws` cannot read /etc/paths.d, and Python 3.12+
    Path.is_dir() re-raises that EPERM. The fingerprint must mark the path and
    carry on, or every login_shell_path() caller crashes (#2448)."""
    real_is_dir = Path.is_dir

    def is_dir(self: Path) -> bool:
        if str(self) == "/etc/paths.d":
            raise PermissionError(1, "Operation not permitted", "/etc/paths.d")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert ("/etc/paths.d", None) in os_tool_path._rc_fingerprint()

    _probe_counter(monkeypatch)
    assert os_tool_path.terminal_path() == "/usr/bin:/opt/homebrew/bin"
    os_tool_path.clear_terminal_path_cache()


def test_concurrent_probes_share_one_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-flight: an rc file slower than the poll interval used to make every
    overlapping poll spawn its own login shell."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_probe() -> str:
        calls.append(1)
        started.set()
        release.wait(5)
        return "/usr/bin"

    monkeypatch.setattr(os_tool_path, "_probe_terminal_path", slow_probe)
    os_tool_path.clear_terminal_path_cache()

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(os_tool_path.terminal_path()))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert results == ["/usr/bin"] * 5


def test_common_tool_dirs_are_the_documented_fallback_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        os_tool_path.glob, "glob", lambda pattern: [str(home / "nvm" / "bin")]
    )
    dirs = os_tool_path.common_tool_dirs()
    assert "/opt/homebrew/bin" in dirs
    assert "/usr/local/bin" in dirs
    assert str(home / ".local" / "bin") in dirs
    # OpenCode's curl installer drops the binary here, beside its credentials.
    assert str(home / ".opencode" / "bin") in dirs
    assert str(home / "nvm" / "bin") in dirs


def test_resolve_executable_is_which_on_posix(tmp_path: Path) -> None:
    binary = tmp_path / "ciao-test-tool"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    assert (
        os_tool_path.resolve_executable("ciao-test-tool", path=str(tmp_path)) == str(binary)
    )
    assert os_tool_path.resolve_executable("definitely-not-real", path=str(tmp_path)) is None


def test_resolve_command_is_which_on_posix(tmp_path: Path) -> None:
    binary = tmp_path / "ciao-test-tool"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    assert os_tool_path.resolve_command("ciao-test-tool", path=str(tmp_path)) == [str(binary)]
    assert os_tool_path.resolve_command("definitely-not-real", path=str(tmp_path)) == []
