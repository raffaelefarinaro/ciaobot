from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.package_version import detect_install_mode, update_package
from ciao.web.routes_api import package_update_endpoint


def test_detect_install_mode(tmp_path, monkeypatch) -> None:
    # Disable editable check by mocking Path methods
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr(Path, "is_dir", lambda self: False)

    # Test venv check when not editable or homebrew
    monkeypatch.setattr(sys, "prefix", "/foo/venv")
    monkeypatch.setattr(sys, "base_prefix", "/foo/python")
    assert detect_install_mode() == "pip_venv"

    # Test unknown when not in venv
    monkeypatch.setattr(sys, "prefix", "/foo/python")
    monkeypatch.setattr(sys, "base_prefix", "/foo/python")
    assert detect_install_mode() == "unknown"


def test_update_package_editable(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "editable")
    res = update_package()
    assert res["ok"] is False
    assert "git pull" in res["command"]
    assert "Editable checkouts must be updated manually" in res["error"]


def test_update_package_pip_venv(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "pip_venv")
    
    mock_run = MagicMock()
    mock_run.return_code = 0
    mock_run.stdout = "Successfully installed ciao-0.3.0"
    mock_run.stderr = ""
    mock_run.returncode = 0
    
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: mock_run)
    res = update_package()
    assert res["ok"] is True
    assert "pip install -U ciao" in res["command"]
    assert "Successfully installed ciao-0.3.0" in res["output"]


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
