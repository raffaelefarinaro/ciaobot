from pathlib import Path

from ciao.menubar_prefs import (
    notifications_enabled,
    read_prefs,
    set_notifications_enabled,
)


def test_notifications_enabled_defaults_true(tmp_path: Path) -> None:
    assert notifications_enabled(tmp_path) is True
    assert read_prefs(tmp_path)["notifications_enabled"] is True


def test_set_notifications_enabled_persists(tmp_path: Path) -> None:
    set_notifications_enabled(tmp_path, False)
    assert notifications_enabled(tmp_path) is False
    set_notifications_enabled(tmp_path, True)
    assert notifications_enabled(tmp_path) is True
