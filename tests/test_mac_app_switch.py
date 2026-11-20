"""Tests for the Mac app switch tile (#3205).

The tile lives in ``ciao/operator_actions.py`` and its eligibility helper in
``ciao/package_version.py``. The condition belongs to the engine: only a
1.x-installer-managed engine on an Apple-silicon Mac emits it, and the "Not
now" receipt snoozes it for every device for 30 days.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ciao import package_version
from ciao.operator_actions import (
    detect_actions,
    dismiss_action,
    run_action,
)

from tests.test_operator_actions import _FakeConfig, _context, _starred
from tests.test_web_housekeeping import _client as _housekeeping_client


@pytest.mark.parametrize(
    ("host_platform", "install_mode", "apple_silicon", "expected"),
    [
        ("darwin", "installer", True, True),
        ("linux", "installer", True, False),
        ("win32", "installer", True, False),
        ("darwin", "bundled_app", True, False),
        ("darwin", "editable", True, False),
        ("darwin", "unknown", True, False),
        ("darwin", "installer", False, False),
    ],
)
def test_needs_switch_truth_table(
    host_platform: str,
    install_mode: str,
    apple_silicon: bool,
    expected: bool,
) -> None:
    """darwin + installer + Apple silicon, and nothing else, qualifies."""
    assert (
        package_version.needs_mac_app_switch(
            host_platform=host_platform,
            install_mode=install_mode,
            apple_silicon=apple_silicon,
        )
        is expected
    )


def test_needs_switch_defaults_to_detected_install_mode() -> None:
    """Without an explicit mode, the detected install mode decides."""
    with patch.object(package_version, "detect_install_mode", lambda: "bundled_app"):
        assert (
            package_version.needs_mac_app_switch(host_platform="darwin", apple_silicon=True)
            is False
        )
    with patch.object(package_version, "detect_install_mode", lambda: "installer"):
        assert (
            package_version.needs_mac_app_switch(host_platform="darwin", apple_silicon=True)
            is True
        )


@pytest.fixture()
def _cold_host_probe():
    """``_host_is_apple_silicon`` is cached per process; keep the cache fresh."""
    package_version._host_is_apple_silicon.cache_clear()
    yield
    package_version._host_is_apple_silicon.cache_clear()


@pytest.mark.usefixtures("_cold_host_probe")
def test_apple_silicon_probe_under_rosetta(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Python reporting x86_64 can still run on Apple silicon (Rosetta)."""
    probe = SimpleNamespace(stdout="1\n")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: probe)
    assert package_version._host_is_apple_silicon() is True

    probe.stdout = "0\n"
    package_version._host_is_apple_silicon.cache_clear()
    assert package_version._host_is_apple_silicon() is False


def test_download_url_is_the_latest_release_page() -> None:
    """One constant: the newest stable release page. Releases attach
    Ciaobot-<version>.dmg, so a fixed asset URL would 404."""
    assert (
        package_version.MAC_APP_DOWNLOAD_URL
        == "https://github.com/raffaelefarinaro/ciaobot/releases/latest"
    )
    assert not package_version.MAC_APP_DOWNLOAD_URL.endswith(".dmg")


def test_tile_on_an_old_mac_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one tile, link-only ("Download for Mac"), Not now in the menu."""
    monkeypatch.setattr(package_version, "needs_mac_app_switch", lambda **_: True)
    _starred(tmp_path)
    tiles = [a for a in detect_actions(_context(tmp_path)) if a.id == "mac-app-switch"]
    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.kind == "mac-app-switch"
    assert tile.severity == 35
    assert tile.glyph == "▲"
    assert tile.title == "Switch to the new Ciaobot app"
    assert tile.detail == (
        "Your workspaces, chats and settings are kept. Install Ciaobot for Mac "
        "on the computer running Ciaobot and it takes over from this install."
    )
    assert tile.workspace == ""
    assert tile.run_label == ""
    assert tile.chat_prompt == ""
    assert tile.view_route == ""
    assert tile.link_label == "Download for Mac"
    assert tile.link_url == package_version.MAC_APP_DOWNLOAD_URL
    assert tile.dismiss_label == "Not now"
    assert tile.as_dict()["link_url"] == package_version.MAC_APP_DOWNLOAD_URL


def test_tile_silent_when_switch_not_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(package_version, "needs_mac_app_switch", lambda **_: False)
    _starred(tmp_path)
    assert "mac-app-switch" not in [a.id for a in detect_actions(_context(tmp_path))]


def test_tile_silent_while_bootstrapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-run wizard stays silent, like the star nudge."""
    monkeypatch.setattr(package_version, "needs_mac_app_switch", lambda **_: True)
    config = _FakeConfig(tmp_path)
    config.bootstrap_mode = True
    assert "mac-app-switch" not in [a.id for a in detect_actions(_context(tmp_path, config=config))]


def test_not_now_snoozes_for_thirty_days_on_every_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dismissal is a shared receipt on the engine, not per device."""
    monkeypatch.setattr(package_version, "needs_mac_app_switch", lambda **_: True)
    start = datetime.now(UTC)
    context = _context(tmp_path)
    context.now = start
    result, summary = dismiss_action("mac-app-switch", context)
    assert result == {"status": "later"}
    assert summary == "Hidden for 30 days."
    receipt = json.loads(
        (tmp_path / ".runtime" / "mac-app-switch-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "later"

    # Absent now and at 29 days, back once at 31.
    context.now = start
    assert "mac-app-switch" not in [a.id for a in detect_actions(context)]
    context.now = start + timedelta(days=29)
    assert "mac-app-switch" not in [a.id for a in detect_actions(context)]
    context.now = start + timedelta(days=31)
    assert "mac-app-switch" in [a.id for a in detect_actions(context)]


async def test_run_of_the_switch_tile_is_unknown(tmp_path: Path) -> None:
    """There is nothing to run: the tile is only a link."""
    with pytest.raises(ValueError):
        await run_action("mac-app-switch", _context(tmp_path))


def test_route_lists_and_dismisses_the_switch_tile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real route serves the tile, and Not now clears it through the engine."""
    monkeypatch.setattr(package_version, "needs_mac_app_switch", lambda **_: True)
    from tests.test_web_housekeeping import _config as _housekeeping_config

    client = _housekeeping_client(_housekeeping_config(tmp_path))
    data = client.get("/api/housekeeping").json()
    tiles = [a for a in data["actions"] if a["id"] == "mac-app-switch"]
    assert len(tiles) == 1
    assert tiles[0]["title"] == "Switch to the new Ciaobot app"
    assert tiles[0]["link_url"] == package_version.MAC_APP_DOWNLOAD_URL

    resp = client.post("/api/housekeeping/mac-app-switch/dismiss")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "mac-app-switch" not in [a["id"] for a in body["actions"]]
    assert (tmp_path / ".runtime" / "mac-app-switch-receipt.json").exists()
