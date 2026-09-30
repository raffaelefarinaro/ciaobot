from __future__ import annotations

import logging
from types import SimpleNamespace

from ciao.upgrade import install_custom_skills, update_skills


def test_update_skills_uses_packaged_sync(monkeypatch, caplog, tmp_path) -> None:
    called = []

    def _sync(workspace, **kwargs):
        called.append((workspace, kwargs))
        return SimpleNamespace(custom_installed=2)

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", _sync)

    with caplog.at_level(logging.INFO):
        result = update_skills(str(tmp_path), workspace_name="work")

    assert result is None
    assert called == [(str(tmp_path), {"workspace_name": "work"})]
    assert "Installed 2 custom skill(s)." in caplog.text


def test_update_skills_handles_subprocess_exception_cleanly(monkeypatch, caplog, tmp_path) -> None:
    def _raise(*args, **kwargs):
        raise OSError("sync unavailable")

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", _raise)

    with caplog.at_level(logging.ERROR):
        result = update_skills(str(tmp_path))

    assert result is None
    assert "Custom skills install failed" in caplog.text


def test_install_custom_skills_does_not_raise_when_memory_step_fails(
    monkeypatch, caplog, tmp_path
) -> None:
    """Server startup must keep going when the memory step fails (#790): the
    failure is reported by the terminal callers, not raised here."""
    def _boom(*args, **kwargs):
        raise RuntimeError("guide unwritable")

    monkeypatch.setattr("ciao.memory_tool.ensure_regions", _boom)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with caplog.at_level(logging.ERROR):
        result = install_custom_skills(str(workspace))

    assert result == 0
    # The sync itself logged the memory failure and carried on, so nothing
    # reached the catch here: an outer failure would have swallowed the whole
    # skill sync and left the workspace with no skills at all.
    assert "memory region ensure/migrate failed" in caplog.text
    assert "Custom skills install failed" not in caplog.text
    assert (workspace / ".claude" / "skills").is_dir()
