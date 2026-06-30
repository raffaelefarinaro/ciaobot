from __future__ import annotations

from pathlib import Path

import pytest

from ciao import dev


def test_build_dev_environment_loads_dotenv_and_sets_dev_defaults(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "web").mkdir(parents=True)
    (workspace / ".env").write_text("PWA_AUTH_TOKEN=test-token\n", encoding="utf-8")

    result = dev.build_dev_environment(workspace, base_env={})

    assert result.workspace == workspace.resolve()
    assert result.web_dir == (workspace / "web").resolve()
    assert result.env["PWA_AUTH_TOKEN"] == "test-token"
    assert result.env["CIAO_AUTO_SYNC_ON_START"] == "false"
    assert result.env["PWA_PORT"] == "8543"
    assert result.env["VITE_BACKEND_URL"] == "http://127.0.0.1:8543"
    assert result.env["CIAO_WORKSPACE"] == str(workspace.resolve())
    assert result.backend_url == "http://127.0.0.1:8543"
    assert result.frontend_url == "http://localhost:5173"


def test_build_dev_environment_requires_web_checkout(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="app checkout"):
        dev.build_dev_environment(tmp_path, base_env={"PWA_AUTH_TOKEN": "token"})


def test_build_dev_environment_requires_auth_token(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()

    with pytest.raises(RuntimeError, match="PWA_AUTH_TOKEN"):
        dev.build_dev_environment(tmp_path, base_env={})
