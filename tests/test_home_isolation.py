"""No test can reach the developer's real home (#696, #821)."""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import REAL_HOME


def test_the_home_is_this_tests_own(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert Path.home() == home
    assert Path(os.path.expanduser("~")) == home
    assert Path.home() != REAL_HOME


def test_the_home_dir_fixture_is_that_home(home_dir: Path) -> None:
    assert home_dir == Path.home()
    assert home_dir.is_dir()


def test_the_installed_opencode_is_never_found(monkeypatch) -> None:
    from ciao.providers.opencode import resolve_opencode_binary

    monkeypatch.delenv("CIAO_OPENCODE_BIN", raising=False)
    assert resolve_opencode_binary() is None
