"""ciao.os_support.shell_hints: one PATH hint per OS, one implementation.

The Windows branch is exercised by patching ``sys.platform`` rather than by
running on Windows: the helper only does string work, so the platform string is
the whole difference.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ciao.os_support.shell_hints import path_hint, path_hint_note

_POWERSHELL_USER_PATH = (
    '[Environment]::SetEnvironmentVariable("Path", \'C:\\Tools\\bin;\' + '
    '[Environment]::GetEnvironmentVariable("Path","User"), "User")'
)


def test_posix_session_hint_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/usr/bin/fish")

    # The short line is what `ciao setup` has always printed, and it must not
    # start looking up $SHELL now that the wizard's longer line does.
    assert path_hint("/opt/venv/bin", persist=False) == 'export PATH="/opt/venv/bin:$PATH"'


def test_posix_persist_zsh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    target = str(Path.home() / ".local" / "bin")
    expected = """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""

    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert path_hint(target, persist=True) == expected

    # The engine usually runs under launchd, where SHELL is unset entirely.
    monkeypatch.delenv("SHELL", raising=False)
    assert path_hint(target, persist=True) == expected


def test_posix_persist_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/bin/bash")

    line = path_hint(str(Path.home() / ".local" / "bin"), persist=True)

    assert line == (
        """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile && source ~/.bash_profile"""
    )


def test_posix_persist_fish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    assert path_hint(str(Path.home() / ".local" / "bin"), persist=True) == (
        "fish_add_path $HOME/.local/bin"
    )


def test_posix_persist_outside_home_is_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # $HOME is only a placeholder the shell expands; outside it there is
    # nothing to collapse to.
    assert path_hint("/opt/tools/bin", persist=True) == (
        """echo 'export PATH="/opt/tools/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""
    )


def test_windows_line_is_a_persistent_user_path_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # `export PATH=...` was the bug: it is POSIX syntax and does nothing in
    # PowerShell or cmd.
    for persist in (False, True):
        line = path_hint(r"C:\Tools\bin", persist=persist)
        assert line == _POWERSHELL_USER_PATH
        assert "export" not in line
        assert "$PATH" not in line


def test_windows_single_quote_in_directory_is_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    line = path_hint(r"C:\Users\O'Neil\bin", persist=True)

    # An unescaped ' would end the literal and swallow the rest of the path.
    assert r"'C:\Users\O''Neil\bin;'" in line


def test_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert path_hint_note() == "Open a new terminal for the change to take effect."

    monkeypatch.setattr(sys, "platform", "darwin")
    assert path_hint_note() == ""