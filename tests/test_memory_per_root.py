"""Tests for per-root memory tool resolution (P9.1) and per-guide memory audits (P9.2).

``memory_status`` and ``memory_update`` resolve their guide from
``config.agent_root(workspace)``; today that equals ``workspace_root`` for every
workspace, so nothing observable changes until the re-rooting release. The audit
reports one entry per registered workspace guide and attributes over-cap per
workspace rather than as a single global figure.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane, McpPrincipal
from ciao.os_audit import run_os_audit


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


def _write_guide(root: Path, *, memory: list[str], profile: list[str]) -> None:
    from ciao.memory_tool import ensure_regions, write_region
    guide = root / "CLAUDE.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    if not guide.exists():
        guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    write_region(guide, "memory", memory)
    write_region(guide, "profile", profile)


def _principal(workspace: str) -> McpPrincipal:
    return McpPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace=workspace, provider="claude"
    )


def _plane(config: CiaoConfig) -> CiaoControlPlane:
    return CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(),
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )


def test_memory_status_resolves_the_guide_from_agent_root(tmp_path: Path) -> None:
    """memory_status reads the guide under ``agent_root(workspace)``.

    Today that equals ``workspace_root``, so a guide written there is reported;
    once the re-rooting release flips ``agent_root`` the same code reads the
    per-workspace guide without any change here.
    """
    config = _config(tmp_path)
    _write_guide(config.workspace_root, memory=["one lesson"], profile=["terse"])
    plane = _plane(config)

    status = plane.memory_status(_principal("personal"))["data"]

    assert status["regions"]["memory"]["entry_count"] == 1
    assert status["regions"]["profile"]["entry_count"] == 1
    assert status["guide"].endswith("CLAUDE.md")


def test_memory_update_writes_the_guide_under_agent_root(tmp_path: Path) -> None:
    """memory_update edits the guide under ``agent_root(workspace)``."""
    config = _config(tmp_path)
    _write_guide(config.workspace_root, memory=[], profile=[])
    plane = _plane(config)

    result = plane.memory_update(
        _principal("personal"), "memory", action="add", entry="a new lesson"
    )["data"]

    assert result["ok"] is True


def test_audit_reports_one_entry_per_registered_guide(tmp_path: Path) -> None:
    """``run_os_audit`` emits one guide entry per registered workspace."""
    config = _config(tmp_path)
    _write_guide(config.workspace_root, memory=["shared"], profile=[])
    for name in ("personal", "work"):
        (config.workspace_root / "memory-vault" / name).mkdir(parents=True, exist_ok=True)

    report = run_os_audit(
        workspace_dir=config.workspace_root,
        vault_root=config.vault_root,
        runtime_dir=config.workspace_root / ".runtime",
        config=config,
    )

    guides = report["memory_hygiene"]["guides"]
    assert {g["workspace"] for g in guides} == {"personal", "work"}
    assert len(guides) == 2


def test_audit_over_cap_is_attributed_per_workspace(tmp_path: Path) -> None:
    """An over-cap region names the workspace that owns it, not a global total."""
    config = _config(tmp_path)
    # Personal is over the memory cap; work is not.
    huge = ["x" * 2600]
    _write_guide(config.workspace_root, memory=huge, profile=[])
    (config.workspace_root / "memory-vault" / "personal").mkdir(parents=True, exist_ok=True)
    (config.workspace_root / "memory-vault" / "work").mkdir(parents=True, exist_ok=True)

    report = run_os_audit(
        workspace_dir=config.workspace_root,
        vault_root=config.vault_root,
        runtime_dir=config.workspace_root / ".runtime",
        config=config,
    )

    over_cap = report["memory_hygiene"]["over_cap"]
    # Every over-cap finding carries the workspace that owns it.
    assert over_cap, "expected at least one over-cap region"
    assert all("workspace" in finding for finding in over_cap)
