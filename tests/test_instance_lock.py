from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.instance_lock import WorkspaceAlreadyRunningError, WorkspaceInstanceLock


def test_workspace_instance_lock_blocks_second_backend_and_releases(tmp_path: Path) -> None:
    first = WorkspaceInstanceLock(tmp_path / ".runtime", workspace_root=tmp_path, port=8443)
    second = WorkspaceInstanceLock(tmp_path / ".runtime", workspace_root=tmp_path, port=8543)

    first.acquire()
    try:
        metadata = json.loads(first.path.read_text(encoding="utf-8"))
        assert metadata["status"] == "running"
        assert metadata["port"] == 8443
        with pytest.raises(WorkspaceAlreadyRunningError, match="port 8443"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
    metadata = json.loads(second.path.read_text(encoding="utf-8"))
    assert metadata["status"] == "stopped"


@pytest.mark.asyncio
async def test_startup_persists_normalized_registry_only_after_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import main
    import ciao.instance_lock as instance_lock

    events: list[str] = []

    class RecordingLock:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            events.append("lock")
            return self

        def __exit__(self, *args) -> None:
            events.append("unlock")

    config = SimpleNamespace(
        state_path=tmp_path / ".runtime" / "state.json",
        workspace_root=tmp_path,
        pwa_port=8443,
        _workspace_registry_changed=True,
    )

    def persist() -> None:
        assert events == ["lock"]
        events.append("persist")

    config.persist_workspace_registry = persist

    async def run_server_locked(_config) -> int:
        events.append("run")
        return 0

    monkeypatch.delenv("CIAO_WORKSPACES", raising=False)
    monkeypatch.setattr(main.CiaoConfig, "from_env", lambda: config)
    monkeypatch.setattr(instance_lock, "WorkspaceInstanceLock", RecordingLock)
    monkeypatch.setattr(main, "_run_server_locked", run_server_locked)

    assert await main._async_main() == 0
    assert events == ["lock", "persist", "run", "unlock"]
