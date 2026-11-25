"""`CiaoControlPlane.chat_reload`: restart a chat's provider so changed tools load."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.schedules import ScheduleEntry


class _FakeProvider:
    background_work_outstanding = False
    background_activity_age = 0.0

    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


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
    project = pcm.create_project("2026-q2-reload-cp", workspace="work")
    return pcm, plane, project


def _principal(chat_id: str, project_id: str) -> AgentPrincipal:
    return AgentPrincipal(
        token_id="t",
        chat_id=chat_id,
        project_id=project_id,
        workspace="work",
        provider="claude",
    )


async def test_managed_care_acquisition_requires_the_active_scheduler_dispatch(tmp_path: Path) -> None:
    from ciao import curation_run
    from ciao.agent_surface import AgentDispatcher
    from ciao.mcp_server import CiaoMcpService

    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="care")
    principal = _principal(chat.chat_id, project.project_id)
    vault = Path(plane.config.workspace_vault_root("work"))
    (vault / "Workspace").mkdir(parents=True)
    with pytest.raises(ControlPlaneError):
        plane.managed_care_begin(principal, 600.0)
    result: list[dict] = []
    service = CiaoMcpService(plane.config)
    service.bind(plane)
    token, _ = service.registry.issue(chat_id=chat.chat_id, project_id=project.project_id,
                                      workspace="work", provider="claude")

    async def fake_stream_chat(chat_id, prompt, images=None, **kwargs):
        status, envelope = await AgentDispatcher(service).dispatch(token, "care_begin", {"ttl_s": 600.0})
        assert status == 200
        result.append(envelope)
        holder = envelope["data"]["holder"]
        curation_run.record_plan(vault, holder=holder, plan={
            "planned": [["proposals", "synthetic-key"]], "blocked": [],
            "blocked_overflow": 0, "deferred_count": 0,
        })
        curation_run.record_done(vault, ["synthetic-key"], holder=holder)
        status, settled = await AgentDispatcher(service).dispatch(
            token, "care_end", {"holder": holder, "status": "ok", "reasons": []}
        )
        assert status == 200
        result.append(settled)
        yield {"type": "result", "text": "done", "is_error": False}

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    entry = ScheduleEntry(
        schedule_id="system-memory-curation@work", scope="system", workspace="work",
        last_dispatch_id="synthetic-dispatch", daily_time_utc="08:00", prompt="care",
        chat_id=0, created_at="2026-01-01T00:00:00Z", frequency="daily",
        web_chat_id=chat.chat_id, archive_policy="manual",
    )
    await pcm._schedule_dispatcher_for().dispatch_schedule(entry, "care", "opus", "auto", "claude")
    assert len(result) == 2
    holder = result[0]["data"]["holder"]
    assert "proof" not in str(result[0])
    assert curation_run.active_lease(vault) is None
    assert curation_run.load_state(vault).last_run["run_id"] == holder
    sidecars = list((plane.config.state_path.parent / "secrets" / "share-evidence").rglob("care-leases/*.json"))
    assert not sidecars
    receipts = list((plane.config.state_path.parent / "secrets" / "share-evidence").rglob("care-settlements/*.json"))
    assert len(receipts) == 1 and receipts[0].stat().st_mode & 0o077 == 0
    stored = json.loads(receipts[0].read_text())
    assert stored["payload"]["dispatch_id"] == "synthetic-dispatch"
    assert stored["payload"]["chat_id"] == chat.chat_id
    assert stored["payload"]["holder"] == holder


async def test_own_chat_with_a_foreground_stream_arms_the_reload(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="own")
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        entered.set()
        await gate.wait()
        return
        yield

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "initial")
    # A reload asked by the live turn itself arrives after the turn has
    # entered the provider, so the drive's pre-entry slot is already released.
    await asyncio.wait_for(entered.wait(), timeout=5)

    result = plane.chat_reload(_principal(chat.chat_id, project.project_id), "")

    assert result["data"]["reload"] == "after_turn"
    assert stream.reload_continue is True
    gate.set()


async def test_busy_chat_is_refused(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    chat = pcm.create_chat(project.project_id, title="busy")
    provider = _FakeProvider()
    provider.background_work_outstanding = True
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]

    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal(chat.chat_id, project.project_id), chat.chat_id)
    assert exc.value.code == "chat_busy"


async def test_another_idle_chat_has_its_provider_popped(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    caller = pcm.create_chat(project.project_id, title="caller")
    other = pcm.create_chat(project.project_id, title="other")
    provider = _FakeProvider()
    pcm._providers[other.chat_id] = provider  # type: ignore[assignment]

    result = plane.chat_reload(_principal(caller.chat_id, project.project_id), other.chat_id)
    await asyncio.sleep(0.05)

    assert result["data"]["reload"] == "next_turn"
    assert other.chat_id not in pcm._providers
    assert provider.disconnected is True


async def test_another_active_chat_is_refused(tmp_path: Path) -> None:
    pcm, plane, project = _setup(tmp_path)
    caller = pcm.create_chat(project.project_id, title="caller")
    other = pcm.create_chat(project.project_id, title="other")
    pcm.active_chat_ids = lambda: {other.chat_id}  # type: ignore[method-assign]

    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal(caller.chat_id, project.project_id), other.chat_id)
    assert exc.value.code == "chat_busy"


async def test_unknown_chat_is_not_found(tmp_path: Path) -> None:
    _, plane, project = _setup(tmp_path)
    with pytest.raises(ControlPlaneError) as exc:
        plane.chat_reload(_principal("chat-x", project.project_id), "missing")
    assert exc.value.code == "chat_not_found"
