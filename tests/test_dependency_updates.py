"""Focused tests for the release-only dependency update helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ciao.dependency_updates import (
    AUTO_UPDATE_KEYS,
    _pinned_version,
    apply_auto_updates,
    check_available_updates,
    install_updated_packages,
)
import ciao.dependency_updates as depupdates


def _write_update_tree(root: Path) -> None:
    (root / "web").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "ciao"\n'
        'version = "0.4.5"\n'
        "dependencies = [\n"
        '  "claude-agent-sdk==0.2.111",\n'
        '  "openai==2.44.0",\n'
        "]\n",
        encoding="utf-8",
    )
    (root / "web" / "package.json").write_text(
        json.dumps(
            {"name": "pwa", "version": "0.1.0", "dependencies": {"vue": "^3.5.0"}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_pinned_version_extracts_python_and_npm_specs() -> None:
    assert _pinned_version("==0.2.111") == "0.2.111"
    assert _pinned_version("^5.0.0") == "5.0.0"
    assert _pinned_version("~1.2.3") == "1.2.3"
    assert _pinned_version("==0.4.0; platform_system == 'Darwin'") == "0.4.0"
    assert _pinned_version("*") is None


def test_check_available_updates_marks_safe_and_major_updates(tmp_path: Path, monkeypatch) -> None:
    _write_update_tree(tmp_path)
    pypi = {"claude-agent-sdk": "0.2.200", "openai": "2.44.0"}
    npm = {"vue": "4.0.0"}
    monkeypatch.setattr(depupdates, "get_latest_pypi_version", lambda name: pypi.get(name))
    monkeypatch.setattr(depupdates, "get_latest_npm_version", lambda name: npm.get(name))

    updates = {update.key: update for update in check_available_updates(tmp_path)}

    assert "openai" not in updates
    sdk = updates["claude-agent-sdk"]
    assert (sdk.current, sdk.latest) == ("0.2.111", "0.2.200")
    assert sdk.auto is True and sdk.is_safe is True
    assert updates["vue"].auto is False
    assert updates["vue"].is_safe is False


def test_apply_auto_updates_only_touches_release_allowlist(tmp_path: Path, monkeypatch) -> None:
    _write_update_tree(tmp_path)
    monkeypatch.setattr(
        depupdates,
        "get_latest_pypi_version",
        lambda name: {"claude-agent-sdk": "0.2.200", "openai": "3.0.0"}.get(name),
    )
    monkeypatch.setattr(depupdates, "get_latest_npm_version", lambda _name: "4.0.0")
    monkeypatch.setattr(depupdates, "regenerate_python_lock", lambda _root: None)

    updates = check_available_updates(tmp_path)
    applied = apply_auto_updates(tmp_path, updates, reinstall=False)

    assert "claude-agent-sdk" in AUTO_UPDATE_KEYS
    assert applied == ["claude-agent-sdk (Python: 0.2.111 -> 0.2.200)"]
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'claude-agent-sdk==0.2.200' in pyproject
    assert 'openai==2.44.0' in pyproject
    assert '"vue": "^3.5.0"' in (tmp_path / "web" / "package.json").read_text(
        encoding="utf-8"
    )


def test_apply_auto_updates_skips_major_auto_candidates(tmp_path: Path, monkeypatch) -> None:
    _write_update_tree(tmp_path)
    updates = [
        depupdates.AvailableUpdate(
            key="claude-agent-sdk",
            ecosystem="python",
            current="0.2.111",
            latest="1.0.0",
            is_safe=False,
            auto=True,
        )
    ]
    regenerate_calls: list[Path] = []
    monkeypatch.setattr(
        depupdates, "regenerate_python_lock", lambda root: regenerate_calls.append(root)
    )

    assert apply_auto_updates(tmp_path, updates, reinstall=False) == []
    assert regenerate_calls == []
    assert 'claude-agent-sdk==0.2.111' in (tmp_path / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_apply_auto_updates_restores_uv_lock_after_install_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _write_update_tree(tmp_path)
    uv_lock = tmp_path / "uv.lock"
    uv_lock.write_text("original lock\n", encoding="utf-8")
    update = depupdates.AvailableUpdate(
        key="claude-agent-sdk",
        ecosystem="python",
        current="0.2.111",
        latest="0.2.200",
        is_safe=True,
        auto=True,
    )

    def regenerate(root: Path) -> None:
        (root / "uv.lock").write_text("regenerated lock\n", encoding="utf-8")

    monkeypatch.setattr(depupdates, "regenerate_python_lock", regenerate)
    monkeypatch.setattr(
        depupdates,
        "install_updated_packages",
        lambda *_args: (False, "pip failed"),
    )

    with pytest.raises(RuntimeError, match="pip failed"):
        apply_auto_updates(tmp_path, [update])

    assert uv_lock.read_text(encoding="utf-8") == "original lock\n"


def test_install_updated_packages_uses_workspace_python(tmp_path: Path, monkeypatch) -> None:
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    calls = []
    monkeypatch.setattr(
        depupdates.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    ok, message = install_updated_packages(True, False, tmp_path)

    assert ok is True
    assert message == "dependencies installed"
    assert calls == [
        (
            [str(venv_python), "-m", "pip", "install", "-e", ".[test]"],
            {"cwd": str(tmp_path), "check": True},
        )
    ]


def test_install_updated_packages_falls_back_to_current_python(tmp_path: Path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        depupdates.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    ok, _message = install_updated_packages(True, False, tmp_path)

    assert ok is True
    assert calls[0][0][:3] == [sys.executable, "-m", "pip"]


def test_auto_update_paths_names_the_lock_the_commit_step_must_stage(
    tmp_path: Path,
) -> None:
    """`uv.lock` is the file a release forgot, so it must be reported.

    The other three are independently version-bearing, so a release stages them
    for its own reasons and the dependency edit rides along inside the same
    file. `uv.lock` carries no version string — nothing staged it, and the
    tagged commit carried the new pin beside the old lock, which fails every
    `uv --frozen` step including the post-tag bundled-runtime build.
    """
    _write_update_tree(tmp_path)
    (tmp_path / "uv.lock").write_text("# lock\n", encoding="utf-8")

    names = {p.name for p in depupdates.auto_update_paths(tmp_path)}

    assert "uv.lock" in names
    assert {"pyproject.toml", "package.json"} <= names


def test_auto_update_paths_skips_files_that_do_not_exist(tmp_path: Path) -> None:
    """A repo without a lock must not hand the commit step a missing path."""
    _write_update_tree(tmp_path)
    assert not (tmp_path / "uv.lock").exists()

    assert all(p.exists() for p in depupdates.auto_update_paths(tmp_path))
