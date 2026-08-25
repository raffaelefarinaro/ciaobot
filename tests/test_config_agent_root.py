"""Tests for CiaoConfig.agent_root, the per-workspace agent directory seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {"PWA_AUTH_TOKEN": "test-token", "CIAO_WORKSPACE": str(tmp_path)}
    )


def test_agent_root_returns_workspace_root_for_every_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.agent_root("personal") == config.workspace_root
    assert config.agent_root("work") == config.workspace_root


def test_agent_root_accepts_a_single_segment_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.agent_root("research") == config.workspace_root


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "a/b", "/abs", "a\\b"],
)
def test_agent_root_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    config = _config(tmp_path)
    with pytest.raises(ValueError):
        config.agent_root(name)
