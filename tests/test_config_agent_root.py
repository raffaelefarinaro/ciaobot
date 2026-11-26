"""Tests for CiaoConfig.agent_root, the per-workspace agent directory seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {"PWA_AUTH_TOKEN": "test-token", "CIAO_WORKSPACE": str(tmp_path)}
    )


def test_agent_root_is_the_workspace_folder_under_the_install(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.agent_root("personal") == config.workspace_root / "personal"
    assert config.agent_root("work") == config.workspace_root / "work"


def test_agent_root_targets_are_the_per_workspace_roots(tmp_path: Path) -> None:
    """The install root is never an agent root."""

    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="personal"),
            "work": WorkspaceConfig(name="work", vault_root="work"),
        },
    )

    assert config.agent_root_targets() == [
        (tmp_path / "personal", "personal"),
        (tmp_path / "work", "work"),
    ]


def test_agent_root_accepts_a_single_segment_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.agent_root("research") == config.workspace_root / "research"


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "/abs", "a\\b"],
)
def test_agent_root_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    config = _config(tmp_path)
    with pytest.raises(ValueError):
        config.agent_root(name)
