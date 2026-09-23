from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.package_version import detect_install_mode, update_package
from ciao.web.routes_node import package_update_endpoint


_BUNDLE_PYTHON = (
    "/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/python/arm64/bin/python3.12"
)


def _fake_ciao_module(monkeypatch, path) -> None:
    monkeypatch.setitem(
        sys.modules, "ciao", types.SimpleNamespace(__file__=str(path))
    )


def test_detect_install_mode_bundled_app(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    monkeypatch.setattr(sys, "executable", _BUNDLE_PYTHON)
    _fake_ciao_module(monkeypatch, tmp_path / "site-packages" / "ciao" / "__init__.py")
    assert detect_install_mode() == "bundled_app"


def test_detect_install_mode_bundled_app_from_package_location(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python3"))
    _fake_ciao_module(
        monkeypatch,
        "/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/"
        "site-packages/arm64/ciao/__init__.py",
    )
    assert detect_install_mode() == "bundled_app"


def test_detect_install_mode_ignores_inherited_marker_in_checkout(monkeypatch, tmp_path) -> None:
    # `ciao dev` started from a shell the app opened inherits the marker, but
    # the backend runs from a checkout and must keep redeploy.
    repo = tmp_path / "repo"
    (repo / "ciao").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    monkeypatch.setattr(sys, "executable", str(repo / ".venv" / "bin" / "python3"))
    _fake_ciao_module(monkeypatch, repo / "ciao" / "__init__.py")
    assert detect_install_mode() == "editable"


def test_detect_install_mode_ignores_a_non_one_bundled_marker(monkeypatch, tmp_path) -> None:
    # Matches ciao.main, which only treats CIAO_BUNDLED_APP == "1" as bundled.
    monkeypatch.setenv("CIAO_BUNDLED_APP", "0")
    monkeypatch.setitem(
        sys.modules,
        "ciao",
        types.SimpleNamespace(
            __file__=str(tmp_path / "site-packages" / "ciao" / "__init__.py")
        ),
    )
    assert detect_install_mode() == "unknown"


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
    # The bundled app is macOS-only; on Linux every install mode gets the
    # administrator workflow instead.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "bundled_app")

    result = update_package()

    assert result["ok"] is False
    assert result["already_current"] is True
    assert result["mode"] == "bundled_app"
    assert "install.sh" in result["command"]


def test_update_package_editable_requires_git_pull(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "editable")

    result = update_package()

    assert result["ok"] is False
    assert result["command"] == "git pull"
    assert "Editable checkouts" in result["error"]


def test_linux_source_export_never_recommends_the_mac_installer(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    for mode in ("editable", "unknown"):
        monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda m=mode: m)
        result = update_package()
        assert result["ok"] is False
        assert result["mode"] == mode
        assert "docs/LINUX.md" in result["error"]
        assert result["command"] == ""


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
