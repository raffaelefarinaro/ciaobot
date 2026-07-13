from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ciao.upgrade import (
    UpgradeResult,
    update_skills,
    upgrade_all,
    upgrade_claude_code,
    upgrade_codex,
    upgrade_gws,
    upgrade_apfel,
    upgrade_root_npm,
    upgrade_scrapling,
    upgrade_web_npm,
)


def test_update_skills_uses_packaged_sync(monkeypatch, caplog, tmp_path) -> None:
    called = []

    def _sync(workspace):
        called.append(workspace)
        return SimpleNamespace(custom_installed=2)

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", _sync)

    with caplog.at_level(logging.INFO):
        result = update_skills(str(tmp_path))

    assert result is None
    assert called == [str(tmp_path)]
    assert "Installed 2 custom skill(s)." in caplog.text


def test_update_skills_handles_subprocess_exception_cleanly(monkeypatch, caplog, tmp_path) -> None:
    def _raise(*args, **kwargs):
        raise OSError("sync unavailable")

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", _raise)

    with caplog.at_level(logging.ERROR):
        result = update_skills(str(tmp_path))

    assert result is None
    assert "Custom skills install failed" in caplog.text


def test_upgrade_gws_skips_when_npm_missing(monkeypatch) -> None:
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: None)
    result = asyncio.run(upgrade_gws())
    assert result.success is False
    assert result.changed is False
    assert "npm not found" in result.stderr


def test_upgrade_claude_code_skips_when_claude_missing(monkeypatch) -> None:
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: None)
    result = asyncio.run(upgrade_claude_code())
    assert result.success is False
    assert result.changed is False
    assert "bundled claude not found" in result.stderr



def test_upgrade_claude_code_uses_bundled_cli(monkeypatch) -> None:
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/mock/bundled/claude")

    async def mock_read_version(cmd):
        assert cmd == ["/mock/bundled/claude", "--version"]
        return "2.1.0"

    monkeypatch.setattr("ciao.upgrade.read_version", mock_read_version)

    result = asyncio.run(upgrade_claude_code())
    assert result.success is True
    assert result.changed is False
    assert "Using bundled Claude Code: 2.1.0" in result.stdout
    assert result.before_version == "2.1.0"
    assert result.after_version == "2.1.0"


def test_upgrade_codex_reports_desktop_managed_binary(monkeypatch) -> None:
    binary = "/Applications/ChatGPT.app/Contents/Resources/codex"
    monkeypatch.setattr(
        "ciao.providers.codex.resolve_codex_binary", lambda: binary
    )
    monkeypatch.setattr(
        "ciao.upgrade.read_version", AsyncMock(return_value="codex-cli 1.2.3")
    )

    result = asyncio.run(upgrade_codex())

    assert result.success is True
    assert result.changed is False
    assert "desktop app" in result.stdout
    assert result.after_version == "codex-cli 1.2.3"


def test_upgrade_codex_uses_native_updater(monkeypatch) -> None:
    binary = "/usr/local/bin/codex"
    expected = UpgradeResult(
        command=[binary, "update"], changed=True, success=True,
        stdout="", stderr="", before_version="1", after_version="2",
    )
    monkeypatch.setattr(
        "ciao.providers.codex.resolve_codex_binary", lambda: binary
    )
    runner = AsyncMock(return_value=expected)
    monkeypatch.setattr("ciao.upgrade.run_upgrade", runner)

    result = asyncio.run(upgrade_codex())

    assert result is expected
    runner.assert_awaited_once_with(
        install_command=[binary, "update"],
        version_command=[binary, "--version"],
    )



_UNCHANGED = UpgradeResult(
    command=[], changed=False, success=True,
    stdout="", stderr="",
    before_version="1.0.0", after_version="1.0.0",
)


@pytest.mark.asyncio
async def test_upgrade_all_returns_none_when_nothing_changed(monkeypatch, tmp_path, caplog) -> None:
    async def _no_pip_changes(root):
        return {}

    monkeypatch.setattr("ciao.upgrade.upgrade_project_deps", _no_pip_changes)
    monkeypatch.setattr("ciao.upgrade.upgrade_gws", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_defuddle", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_claude_code", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_codex", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_root_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_web_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_apfel", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_libreoffice", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_scrapling", AsyncMock(return_value=_UNCHANGED))

    with caplog.at_level(logging.INFO):
        result = await upgrade_all(str(tmp_path))

    assert result is None
    assert "everything already up to date" in caplog.text


@pytest.mark.asyncio
async def test_upgrade_all_reports_changes(monkeypatch, tmp_path, caplog) -> None:
    async def _pip_bumped(root):
        return {"notebooklm-py": ("0.4.0", "0.5.0")}

    gws_bumped = UpgradeResult(
        command=[], changed=True, success=True,
        stdout="", stderr="",
        before_version="0.22.1", after_version="0.23.0",
    )
    monkeypatch.setattr("ciao.upgrade.upgrade_project_deps", _pip_bumped)
    monkeypatch.setattr("ciao.upgrade.upgrade_gws", AsyncMock(return_value=gws_bumped))
    monkeypatch.setattr("ciao.upgrade.upgrade_defuddle", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_claude_code", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_codex", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_root_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_web_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_apfel", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_libreoffice", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_scrapling", AsyncMock(return_value=_UNCHANGED))

    with caplog.at_level(logging.INFO):
        result = await upgrade_all(str(tmp_path))

    assert result is not None
    assert "notebooklm-py: 0.4.0 -> 0.5.0" in result
    assert "gws: 0.22.1 -> 0.23.0" in result


@pytest.mark.asyncio
async def test_upgrade_all_surfaces_silent_failures(monkeypatch, tmp_path, caplog) -> None:
    async def _no_pip_changes(root):
        return {}

    claude_failed = UpgradeResult(
        command=["npm", "install", "-g", "@anthropic-ai/claude-code"],
        changed=False, success=False,
        stdout="",
        stderr="npm error code EACCES\nnpm error syscall mkdir",
        before_version="", after_version="",
    )

    monkeypatch.setattr("ciao.upgrade.upgrade_project_deps", _no_pip_changes)
    monkeypatch.setattr("ciao.upgrade.upgrade_gws", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_defuddle", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_claude_code", AsyncMock(return_value=claude_failed))
    monkeypatch.setattr("ciao.upgrade.upgrade_codex", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_root_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_web_npm", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_apfel", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_libreoffice", AsyncMock(return_value=_UNCHANGED))
    monkeypatch.setattr("ciao.upgrade.upgrade_scrapling", AsyncMock(return_value=_UNCHANGED))

    with caplog.at_level(logging.WARNING):
        result = await upgrade_all(str(tmp_path))

    assert result is not None
    assert "claude: install failed" in result
    assert "claude upgrade failed" in caplog.text


def test_upgrade_apfel_skips_when_brew_missing(monkeypatch) -> None:
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: None)
    result = asyncio.run(upgrade_apfel())
    assert result.success is False
    assert result.changed is False
    assert "brew not found" in result.stderr


def test_upgrade_apfel_runs_install_when_not_installed(monkeypatch) -> None:
    # brew is found but apfel is not on PATH
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: "/usr/local/bin/brew" if cmd == "brew" else None)
    
    async def mock_run_upgrade(install_command, version_command):
        assert install_command == ["/usr/local/bin/brew", "install", "apfel"]
        assert version_command == ["apfel", "--version"]
        return UpgradeResult(
            command=install_command, changed=True, success=True,
            stdout="", stderr="", before_version="", after_version="1.5.2"
        )
    
    monkeypatch.setattr("ciao.upgrade.run_upgrade", mock_run_upgrade)
    result = asyncio.run(upgrade_apfel())
    assert result.success is True
    assert result.changed is True
    assert result.after_version == "1.5.2"


def test_upgrade_apfel_runs_upgrade_when_installed(monkeypatch) -> None:
    # brew and apfel are both found on PATH
    monkeypatch.setattr(
        "ciao.upgrade.shutil.which",
        lambda cmd: "/usr/local/bin/brew" if cmd == "brew" else ("/opt/homebrew/bin/apfel" if cmd == "apfel" else None)
    )
    
    async def mock_run_upgrade(install_command, version_command):
        assert install_command == ["/usr/local/bin/brew", "upgrade", "apfel"]
        assert version_command == ["apfel", "--version"]
        return UpgradeResult(
            command=install_command, changed=False, success=True,
            stdout="", stderr="", before_version="1.5.2", after_version="1.5.2"
        )
    
    monkeypatch.setattr("ciao.upgrade.run_upgrade", mock_run_upgrade)
    result = asyncio.run(upgrade_apfel())
    assert result.success is True
    assert result.changed is False
    assert result.before_version == "1.5.2"


def test_upgrade_scrapling_skips_when_not_installed(monkeypatch) -> None:
    # Scrapling is opt-in: a missing binary is not a failure and must not
    # trigger an install.
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: None)
    result = asyncio.run(upgrade_scrapling())
    assert result.success is True
    assert result.changed is False
    assert result.command == []
    assert "not installed (optional)" in result.stdout


def test_upgrade_scrapling_upgrades_when_installed(monkeypatch) -> None:
    import sys as _sys
    monkeypatch.setattr(
        "ciao.upgrade.shutil.which",
        lambda cmd: "/usr/local/bin/scrapling" if cmd == "scrapling" else None,
    )

    async def mock_run_upgrade(install_command, version_command):
        assert install_command == [
            _sys.executable, "-m", "pip", "install", "--upgrade", "scrapling[fetchers]",
        ]
        assert version_command == [_sys.executable, "-m", "pip", "show", "scrapling"]
        return UpgradeResult(
            command=install_command, changed=False, success=True,
            stdout="", stderr="", before_version="0.4.10", after_version="0.4.10",
        )

    monkeypatch.setattr("ciao.upgrade.run_upgrade", mock_run_upgrade)
    result = asyncio.run(upgrade_scrapling())
    assert result.success is True
    assert result.before_version == "0.4.10"


def test_upgrade_scrapling_refreshes_browsers_on_change(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.upgrade.shutil.which",
        lambda cmd: "/usr/local/bin/scrapling" if cmd == "scrapling" else None,
    )
    bumped = UpgradeResult(
        command=[], changed=True, success=True,
        stdout="", stderr="", before_version="0.4.9", after_version="0.4.10",
    )
    monkeypatch.setattr("ciao.upgrade.run_upgrade", AsyncMock(return_value=bumped))

    exec_calls = []

    async def mock_exec(*cmd, **kwargs):
        exec_calls.append(list(cmd))
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr("ciao.upgrade.asyncio.create_subprocess_exec", mock_exec)
    result = asyncio.run(upgrade_scrapling())
    assert result.changed is True
    assert exec_calls == [["scrapling", "install"]]


def test_upgrade_root_npm_uses_prefix(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: "/usr/bin/npm")
    
    async def mock_run_upgrade(install_command, version_command):
        assert "--prefix" in install_command
        prefix_idx = install_command.index("--prefix")
        assert install_command[prefix_idx + 1] == str(tmp_path)
        return UpgradeResult(
            command=install_command, changed=False, success=True,
            stdout="", stderr="", before_version="1.0.0", after_version="1.0.0"
        )
        
    monkeypatch.setattr("ciao.upgrade.run_upgrade", mock_run_upgrade)
    result = asyncio.run(upgrade_root_npm(str(tmp_path)))
    assert result.success is True


def test_upgrade_web_npm_uses_prefix(monkeypatch, tmp_path) -> None:
    import os
    monkeypatch.setattr("ciao.upgrade.shutil.which", lambda cmd: "/usr/bin/npm")
    expected_web_dir = os.path.join(str(tmp_path), "web")
    
    async def mock_run_upgrade(install_command, version_command):
        assert "--prefix" in install_command
        prefix_idx = install_command.index("--prefix")
        assert install_command[prefix_idx + 1] == expected_web_dir
        return UpgradeResult(
            command=install_command, changed=False, success=True,
            stdout="", stderr="", before_version="1.0.0", after_version="1.0.0"
        )
        
    monkeypatch.setattr("ciao.upgrade.run_upgrade", mock_run_upgrade)
    result = asyncio.run(upgrade_web_npm(str(tmp_path)))
    assert result.success is True
