from __future__ import annotations

import sys
import types
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.package_version import detect_install_mode, update_package
from ciao.web.routes_node import package_update_endpoint


def test_detect_install_mode_bundled_app(monkeypatch) -> None:
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    assert detect_install_mode() == "bundled_app"


def test_detect_install_mode_unknown_without_package_manager(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CIAO_BUNDLED_APP", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "ciao",
        types.SimpleNamespace(
            __file__=str(tmp_path / "site-packages" / "ciao" / "__init__.py")
        ),
    )
    assert detect_install_mode() == "unknown"


def test_update_package_points_bundled_app_at_installer(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "bundled_app")

    result = update_package()

    assert result["ok"] is False
    assert result["already_current"] is True
    assert result["mode"] == "bundled_app"
    assert "install.sh" in result["command"]


def test_update_package_editable_requires_git_pull(monkeypatch) -> None:
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "editable")

    result = update_package()

    assert result["ok"] is False
    assert result["command"] == "git pull"
    assert "Editable checkouts" in result["error"]


def test_package_update_endpoint_explains_app_owned_updates() -> None:
    app = Starlette(
        routes=[Route("/api/package/update", package_update_endpoint, methods=["POST"])]
    )
    app.state.config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "test-token",
            "PWA_AUTH_REQUIRED": "false",
            "CIAO_WORKSPACE": "/tmp/ciaobot-test-workspace",
        }
    )

    with patch(
        "ciao.web.routes_node.update_package",
        return_value={
            "ok": False,
            "already_current": True,
            "mode": "bundled_app",
            "error": "The bundled app and engine update together through Ciaobot.app.",
            "command": "curl -fsSL .../install.sh | sh",
        },
    ):
        response = TestClient(app).post("/api/package/update")

    assert response.status_code == 400
    assert response.json()["mode"] == "bundled_app"
