"""ciao.os_support.paths: one spelling for a resolved path, whatever the race."""

from __future__ import annotations

from pathlib import Path

from ciao.os_support.paths import resolve_path


def test_resolve_path_is_path_resolve_for_an_existing_directory(tmp_path: Path) -> None:
    assert resolve_path(tmp_path) == tmp_path.resolve()
