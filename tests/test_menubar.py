from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

from ciao import menubar


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_fetch_server_status_ready(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResponse({"phases": [], "overall_ready": True})

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    status = menubar.fetch_server_status(9443)

    assert calls["url"] == "http://localhost:9443/api/startup-status"
    assert status == menubar.ServerStatus(reachable=True, ready=True)


def test_fetch_server_status_starting(monkeypatch) -> None:
    monkeypatch.setattr(
        menubar.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse({"phases": [], "overall_ready": False}),
    )

    assert menubar.fetch_server_status(8443) == menubar.ServerStatus(
        reachable=True, ready=False
    )


def test_fetch_server_status_unreachable(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.fetch_server_status(8443) == menubar.ServerStatus(
        reachable=False, ready=False
    )


def test_status_labels() -> None:
    assert menubar.status_label(menubar.ServerStatus(True, True)) == "Server: running"
    assert menubar.status_label(menubar.ServerStatus(True, False)) == "Server: starting…"
    assert (
        menubar.status_label(menubar.ServerStatus(False, False))
        == "Server: not running"
    )


def test_open_app_command_uses_setup_token(tmp_path: Path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok123\n", encoding="utf-8")

    assert menubar.open_app_command(tmp_path, 9443) == [
        "open",
        "http://localhost:9443/?setup=tok123",
    ]


def test_open_app_command_without_token_falls_back_to_plain_url(tmp_path: Path) -> None:
    assert menubar.open_app_command(tmp_path, 8443) == [
        "open",
        "http://localhost:8443/",
    ]


def test_restart_server_command_targets_launchd_label() -> None:
    assert menubar.restart_server_command(uid=501) == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.ciao.server",
    ]


def test_view_logs_command_opens_stderr_log(tmp_path: Path) -> None:
    assert menubar.view_logs_command(tmp_path) == [
        "open",
        str(tmp_path / ".runtime" / "ciao.stderr.log"),
    ]


def test_icon_paths_resolve_to_packaged_faces() -> None:
    assert Path(menubar.icon_path("face.png")).is_file()
    assert Path(menubar.icon_path("face_scared.png")).is_file()


def test_run_menubar_without_rumps_explains_the_extra(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setitem(sys.modules, "rumps", None)

    assert menubar.run_menubar(tmp_path, 8443) == 1
    assert "ciao[menubar]" in capsys.readouterr().err
