from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.package_version import detect_install_mode, update_package
from ciao.web.routes_api import package_update_endpoint


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _release_opener(payload: dict):
    def opener(request, timeout: float):
        return _Response(payload)

    return opener


def test_detect_install_mode_homebrew(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)

    cellar = tmp_path / "opt" / "homebrew" / "Cellar" / "ciaobot" / "0.4.5" / "libexec"
    cellar.mkdir(parents=True)
    python = cellar / "bin" / "python3.12"
    python.parent.mkdir(parents=True)
    python.touch()

    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "prefix", str(cellar))
    monkeypatch.setattr(sys, "base_prefix", "/opt/homebrew/opt/python@3.12/Frameworks/Python.framework/Versions/3.12")
    assert detect_install_mode() == "homebrew"


def test_detect_install_mode(tmp_path, monkeypatch) -> None:
    # Disable editable check by mocking Path methods
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)

    # Test venv check when not editable
    monkeypatch.setattr(sys, "prefix", "/foo/venv")
    monkeypatch.setattr(sys, "base_prefix", "/foo/python")
    assert detect_install_mode() == "pip_venv"

    # Test unknown when not in venv
    monkeypatch.setattr(sys, "prefix", "/foo/python")
    monkeypatch.setattr(sys, "base_prefix", "/foo/python")
    assert detect_install_mode() == "unknown"


def test_update_package_homebrew_upgrade(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "homebrew")
    monkeypatch.setattr("ciao.package_version._resolve_brew", lambda: "/opt/homebrew/bin/brew")

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        result = MagicMock()
        result.stdout = "ciaobot 0.4.6"
        result.stderr = ""
        result.returncode = 0
        return result

    monkeypatch.setattr("subprocess.run", fake_run)
    res = update_package()
    assert res["ok"] is True
    assert captured["cmd"] == ["/opt/homebrew/bin/brew", "upgrade", "ciaobot"]


def test_update_package_homebrew_without_brew(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "homebrew")
    monkeypatch.setattr("ciao.package_version._resolve_brew", lambda: None)

    res = update_package()
    assert res["ok"] is False
    assert res["mode"] == "homebrew"
    assert "brew upgrade ciaobot" in res["command"]


def test_update_package_editable(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "editable")
    res = update_package()
    assert res["ok"] is False
    assert "git pull" in res["command"]
    assert "Editable checkouts must be updated manually" in res["error"]


def test_update_package_pip_venv_installs_release_wheel(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "pip_venv")

    wheel_url = (
        "https://github.com/raffaelefarinaro/ciaobot/releases/download/"
        "v0.3.0/ciao-0.3.0-py3-none-any.whl"
    )
    opener = _release_opener({
        "tag_name": "v0.3.0",
        "assets": [
            {"name": "ciao-0.3.0.tar.gz", "browser_download_url": wheel_url[:-4] + ".tar.gz"},
            {"name": "ciao-0.3.0-py3-none-any.whl", "browser_download_url": wheel_url},
        ],
    })

    mock_run = MagicMock()
    mock_run.stdout = "Successfully installed ciao-0.3.0"
    mock_run.stderr = ""
    mock_run.returncode = 0

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return mock_run

    monkeypatch.setattr("subprocess.run", fake_run)
    res = update_package(opener=opener)
    assert res["ok"] is True
    assert captured["cmd"] == [sys.executable, "-m", "pip", "install", "-U", wheel_url]
    assert wheel_url in res["command"]
    assert "pip install -U ciao" not in res["command"].replace(wheel_url, "")
    assert "Successfully installed ciao-0.3.0" in res["output"]


def test_update_package_pip_venv_without_wheel_asset(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "pip_venv")

    def fail_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("pip must not run without a wheel asset")

    monkeypatch.setattr("subprocess.run", fail_run)
    opener = _release_opener({"tag_name": "v0.3.0", "assets": []})

    res = update_package(opener=opener)
    assert res["ok"] is False
    assert "no .whl asset" in res["error"]
    assert "releases/latest" in res["command"]


def test_update_package_pip_venv_release_fetch_failure(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "pip_venv")

    from urllib.error import URLError

    def opener(request, timeout: float):
        raise URLError("offline")

    res = update_package(opener=opener)
    assert res["ok"] is False
    assert "Could not fetch the latest release" in res["error"]
    assert "releases/latest" in res["command"]


def test_package_update_endpoint_success() -> None:
    app = Starlette(
        routes=[Route("/api/package/update", package_update_endpoint, methods=["POST"])]
    )
    app.state.config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_PUSH_CONTACT": "mailto:owner@example.com",
    })
    
    restarts = []
    app.state.request_restart = restarts.append

    with patch("ciao.web.routes_api.update_package") as mock_update:
        mock_update.return_value = {
            "ok": True,
            "mode": "pip_venv",
            "output": "Successfully upgraded",
            "command": "pip install -U ciao",
        }
        
        resp = TestClient(app).post("/api/package/update")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert restarts == [] # Async task is scheduled to restart after 2 seconds
