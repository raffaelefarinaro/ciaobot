from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.install_receipt import InstallReceipt, write_receipt
from ciao.package_version import detect_install_mode, update_package
from ciao.web.routes_node import package_update_endpoint


_BUNDLE_PYTHON = (
    "/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/python/arm64/bin/python3.12"
)


def _installer_receipt(**overrides) -> InstallReceipt:
    """A receipt describing an engine this process could be."""
    fields = {
        "version": "0.9.2",
        "executable": "/u/.local/bin/ciao",
        "python": sys.executable,
        "service_backend": "launchd",
        "service_label": "com.ciao.server",
        "installed_at": "2026-09-25T16:00:00+00:00",
    }
    fields.update(overrides)
    return InstallReceipt(**fields)


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


def test_detect_install_mode_prefers_checkout_over_bundled_interpreter(
    monkeypatch, tmp_path
) -> None:
    # `ciao dev` from an app shell: the bundle's bin is first on PATH, so the
    # backend reuses the bundled interpreter while importing ciao from the
    # checkout. The imported package decides: this is a source checkout.
    repo = tmp_path / "repo"
    (repo / "ciao").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    monkeypatch.setattr(sys, "executable", _BUNDLE_PYTHON)
    _fake_ciao_module(monkeypatch, repo / "ciao" / "__init__.py")
    assert detect_install_mode() == "editable"


def test_detect_install_mode_bundled_package_on_bundled_interpreter(
    monkeypatch,
) -> None:
    # The genuine app: interpreter and package both inside the bundle runtime.
    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    monkeypatch.setattr(sys, "executable", _BUNDLE_PYTHON)
    _fake_ciao_module(
        monkeypatch,
        "/Applications/Ciaobot.app/Contents/Resources/ciao-runtime/"
        "site-packages/arm64/ciao/__init__.py",
    )
    assert detect_install_mode() == "bundled_app"


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


def _no_bundle_site_packages(monkeypatch, tmp_path) -> None:
    """Stage a plain site-packages import: neither a bundle nor a checkout."""
    monkeypatch.delenv("CIAO_BUNDLED_APP", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "ciao",
        types.SimpleNamespace(
            __file__=str(tmp_path / "site-packages" / "ciao" / "__init__.py")
        ),
    )


def test_detect_install_mode_installer_from_matching_receipt(monkeypatch, tmp_path) -> None:
    _no_bundle_site_packages(monkeypatch, tmp_path)
    write_receipt(_installer_receipt())

    assert detect_install_mode() == "installer"


def test_detect_install_mode_ignores_receipt_for_other_interpreter(
    monkeypatch, tmp_path
) -> None:
    # A receipt for a different engine on the same machine (an uninstalled
    # engine, or a colleague's install) must not relabel this process.
    _no_bundle_site_packages(monkeypatch, tmp_path)
    write_receipt(_installer_receipt(python=str(tmp_path / "other" / "python3")))

    assert detect_install_mode() == "unknown"


def test_detect_install_mode_checkout_beats_receipt(monkeypatch, tmp_path) -> None:
    # `ciao dev` from a shell on an installed machine: the receipt names this
    # interpreter, but the import comes from a checkout, which redeploys.
    repo = tmp_path / "repo"
    (repo / "ciao").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    venv_python = str(repo / ".venv" / "bin" / "python3")
    monkeypatch.setattr(sys, "executable", venv_python)
    _fake_ciao_module(monkeypatch, repo / "ciao" / "__init__.py")
    write_receipt(_installer_receipt(python=venv_python))

    assert detect_install_mode() == "editable"


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


def test_update_package_installer_mode_points_at_installer(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "installer")

    result = update_package()

    assert result["ok"] is False
    assert result["mode"] == "installer"
    assert "install.sh" in result["command"]
    assert "installer" in result["error"]


# Receipt ownership decides the answer, not the platform. A Linux
# `systemd-user` engine placed by the shell installer is a managed install, so
# it must be told about `ciao update stage` rather than the generic Linux
# administrator workflow, which the platform branch would otherwise return
# first. This is the #567 review finding on the branch order.
#
# The command follows the platform too: install.sh exits on Linux with "this
# installer supports macOS only", so a Linux installer engine must not be sent
# to it. (Round 1 review finding.)
def test_update_package_installer_checked_before_linux(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ciao.package_version.detect_install_mode", lambda: "installer")

    result = update_package()

    assert result["ok"] is False
    assert result["mode"] == "installer"
    assert "ciao update stage" in result["error"]
    assert result["command"] == "ciao update stage"


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


def test_package_update_endpoint_installer_is_guidance() -> None:
    # An installer install has nothing to report as a server failure either:
    # 400 carrying the re-run-installer guidance, not the 500 the UI would
    # render as a broken update check.
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
            "mode": "installer",
            "error": "This engine was installed by the Ciaobot installer.",
            "command": "curl -fsSL .../install.sh | sh",
        },
    ):
        response = TestClient(app).post("/api/package/update")

    assert response.status_code == 400
    assert response.json()["mode"] == "installer"
