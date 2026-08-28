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
    # Personal is over the memory cap; work is not. Seeded well above the
    # shipped default so a future default bump cannot silence this fixture.
    huge = ["x" * 3600]
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


# -- P10.8: the global / per-root audit split --------------------------------
#
# Measured on the reference install before writing any of this: with two
# workspaces registered, SIX of the seven audit sections came back
# byte-identical, because today one vault, one skill catalog and one CLAUDE.md
# are shared. The hygiene routine is `per_workspace: true`, so it reported all
# of that once per workspace. Two of those sections never become per-root — their
# subject is the global runtime directory — so they need a scope, not a
# migration.


def _seeded(tmp_path: Path) -> CiaoConfig:
    """A registry with two workspaces, each holding one note and one guide."""
    config = _config(tmp_path)
    for name in ("personal", "work"):
        notes = config.workspace_vault_root(name)
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f"{name}-note.md").write_text(
            f"---\ntype: note\ntitle: {name}\n---\n# {name}\n", encoding="utf-8"
        )
    (tmp_path / ".runtime").mkdir(parents=True, exist_ok=True)
    _write_guide(config.workspace_root, memory=["one lesson"], profile=["terse"])
    return config


def _audit(config: CiaoConfig, **kwargs) -> dict:
    return run_os_audit(
        workspace_dir=config.workspace_root,
        vault_root=config.vault_root,
        runtime_dir=config.state_path.parent,
        config=config,
        **kwargs,
    )


def test_workspace_scope_drops_the_sections_that_are_the_same_for_every_root(
    tmp_path: Path,
) -> None:
    report = _audit(_seeded(tmp_path), workspace_name="personal", scope="workspace")

    assert "job_runs_audit" not in report
    assert "upgrade_notices" not in report
    # Setup stays: it is a precondition on the roots this scope read, not a
    # finding, and a report that cannot say whether its roots were readable is
    # not a report.
    assert "setup_audit" in report
    assert "vault_hygiene" in report
    assert report["scope"] == "workspace"


def test_global_scope_drops_the_sections_that_describe_one_workspace(
    tmp_path: Path,
) -> None:
    report = _audit(_seeded(tmp_path), scope="global")

    for key in ("vault_hygiene", "skill_audit", "rule_audit", "memory_hygiene"):
        assert key not in report
    assert "job_runs_audit" in report
    assert "upgrade_notices" in report


def test_a_section_outside_the_scope_is_absent_rather_than_zeroed(
    tmp_path: Path,
) -> None:
    """An empty section reads as "checked and clean", which is a false claim."""
    report = _audit(_seeded(tmp_path), workspace_name="personal", scope="workspace")

    assert report.get("job_runs_audit") is None
    assert "job_runs_audit" not in report


def test_the_default_scope_is_unchanged(tmp_path: Path) -> None:
    """Every existing caller passes no scope, and must see the same report."""
    config = _seeded(tmp_path)

    report = _audit(config)

    for key in (
        "setup_audit",
        "vault_hygiene",
        "skill_audit",
        "rule_audit",
        "memory_hygiene",
        "job_runs_audit",
        "upgrade_notices",
    ):
        assert key in report, key
    assert report["scope"] == "all"


def test_a_named_workspace_audits_its_own_notes_and_not_the_others(
    tmp_path: Path,
) -> None:
    """The present leak: a personal hygiene chat reported the work notes' defects.

    Before this, ``_vault_audit`` ran over the whole shared vault regardless of
    which workspace the run was for, so both workspaces' reports came back
    byte-identical and each claimed the other's findings as its own.
    """
    config = _seeded(tmp_path)
    # A defect that exists only in the work notes.
    (config.workspace_vault_root("work") / "broken.md").write_text(
        "---\ntype: note\ntitle: broken\n---\n[gone](./missing-target.md)\n",
        encoding="utf-8",
    )

    personal = _audit(config, workspace_name="personal", scope="workspace")
    work = _audit(config, workspace_name="work", scope="workspace")

    assert personal["setup_audit"]["vault_root"].endswith("memory-vault/personal")
    assert work["setup_audit"]["vault_root"].endswith("memory-vault/work")
    assert work["vault_hygiene"]["broken_markdown_links"], "the defect must be found"
    assert personal["vault_hygiene"]["broken_markdown_links"] == [], (
        "a personal run must not report a defect that only exists in work"
    )


def test_a_named_workspace_reports_only_its_own_guide(tmp_path: Path) -> None:
    """One guide, not N. Reporting every guide inside a per-workspace run is the
    same N-times duplication as the global sections, one level down."""
    config = _seeded(tmp_path)

    report = _audit(config, workspace_name="work", scope="workspace")

    assert [g["workspace"] for g in report["memory_hygiene"]["guides"]] == ["work"]


def test_an_unregistered_scope_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scope must be one of"):
        _audit(_seeded(tmp_path), scope="everything")


def test_agent_vault_root_is_the_shared_vault_until_the_install_re_roots(
    tmp_path: Path,
) -> None:
    """The aggregate files, not the notes — and the distinction is the point.

    ``workspace_vault_root`` is where a workspace's NOTES live, a subtree of one
    shared vault today. ``agent_vault_root`` is where the INDEX.md ABOUT them
    lives, which is one shared pair today and one pair per root afterwards.
    """
    config = _seeded(tmp_path)

    assert config.agent_vault_root("personal") == config.vault_root
    assert config.agent_vault_root("work") == config.vault_root
    assert config.workspace_vault_root("personal") != config.agent_vault_root("personal")
