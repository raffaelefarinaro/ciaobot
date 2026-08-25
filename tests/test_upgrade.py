from __future__ import annotations

import logging
from types import SimpleNamespace

from ciao.upgrade import update_skills


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


