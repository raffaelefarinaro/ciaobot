from __future__ import annotations

import json
from pathlib import Path

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
