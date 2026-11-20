"""ciao.os_support.shell_hints: the POSIX PATH hint lines.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.os_support.shell_hints import path_hint


def test_posix_session_hint_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/fish")

    # The short line is what `ciao setup` has always printed, and it must not
    # start looking up $SHELL now that the wizard's longer line does.
    assert path_hint("/opt/venv/bin", persist=False) == 'export PATH="/opt/venv/bin:$PATH"'


def test_posix_persist_zsh(monkeypatch: pytest.MonkeyPatch) -> None:
    target = str(Path.home() / ".local" / "bin")
    expected = """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""

    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert path_hint(target, persist=True) == expected

    # The engine usually runs under launchd, where SHELL is unset entirely.
    monkeypatch.delenv("SHELL", raising=False)
    assert path_hint(target, persist=True) == expected


def test_posix_persist_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")

    line = path_hint(str(Path.home() / ".local" / "bin"), persist=True)

    assert line == (
        """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile && source ~/.bash_profile"""
    )


def test_posix_persist_fish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    assert path_hint(str(Path.home() / ".local" / "bin"), persist=True) == (
        "fish_add_path $HOME/.local/bin"
    )


def test_posix_persist_outside_home_is_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # $HOME is only a placeholder the shell expands; outside it there is
    # nothing to collapse to.
    assert path_hint("/opt/tools/bin", persist=True) == (
        """echo 'export PATH="/opt/tools/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""
    )
