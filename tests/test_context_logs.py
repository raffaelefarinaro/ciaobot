"""`CiaoControlPlane.context_logs`: version and log tails for workspace-scoped chats."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import ciao
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, CiaoControlPlane
from ciao.error_log import tail_service_log
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _setup(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    plane = CiaoControlPlane(
        config, project_chat_manager=pcm, schedule_manager=SimpleNamespace()
    )
    project = pcm.create_project("2026-q2-context-logs", workspace="work")
    return pcm, plane, project


def _principal(chat_id: str, project_id: str) -> AgentPrincipal:
    return AgentPrincipal(
        token_id="t",
        chat_id=chat_id,
        project_id=project_id,
        workspace="work",
        provider="claude",
    )


async def test_context_logs_returns_version_and_log_tails(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="diag")
    (tmp_path / ".runtime" / "server_errors.log").write_text("boom\n")
    (tmp_path / ".runtime" / "ciao.stderr.log").write_text("stderr line\n")

    result = await plane.context_logs(_principal(chat.chat_id, project.project_id), lines=50)

    assert result["ok"] is True
    data = result["data"]
    assert data["version"] == ciao.__version__
    assert "boom" in data["error_log"]
    assert "stderr line" in data["service_log"]
    assert data["failed_jobs"] == []
    assert data["lines"] == 50


async def test_context_logs_bounds_lines(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="diag")
    principal = _principal(chat.chat_id, project.project_id)
    (tmp_path / ".runtime" / "server_errors.log").write_text("a\nb\nc\nd\ne\n")

    two = await plane.context_logs(principal, lines=2)
    assert two["data"]["error_log"] == "d\ne\n"
    assert (await plane.context_logs(principal, lines=0))["data"]["lines"] == 1
    assert (await plane.context_logs(principal, lines=10**6))["data"]["lines"] == 1000


def test_tail_service_log_is_empty_without_a_file(tmp_path: Path) -> None:
    assert tail_service_log(tmp_path) == ""
