"""Tests for drag-to-reorder project ordering (ProjectChatManager.reorder_projects)."""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_reorder_projects_rewrites_order(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    b = pcm.create_project("Beta", workspace="personal")
    c = pcm.create_project("Gamma", workspace="personal")

    pcm.reorder_projects("personal", [c.project_id, a.project_id, b.project_id])

    ordered = [p.name for p in pcm.list_projects("personal")]
    # General is auto-pinned first; the rest follow the requested sequence.
    assert ordered == ["General", "Gamma", "Alpha", "Beta"]


def test_reorder_keeps_general_pinned_first(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    a = pcm.create_project("Alpha", workspace="personal")
    general = next(p for p in pcm.list_projects("personal") if p.name == "General")

    # Even if the client puts General in the middle, it stays first.
    pcm.reorder_projects("personal", [a.project_id, general.project_id])

    assert general.order == 0
    assert pcm.list_projects("personal")[0].name == "General"


def test_reorder_ignores_other_workspace_ids(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    p_personal = pcm.create_project("Alpha", workspace="personal")
    p_work = pcm.create_project("WorkOne", workspace="work")

    # Passing a work id into a personal reorder must not move it.
    pcm.reorder_projects("personal", [p_work.project_id, p_personal.project_id])

    assert p_work.workspace == "work"
    assert "Alpha" in [p.name for p in pcm.list_projects("personal")]
