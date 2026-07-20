from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.mcp_server import CiaoMcpService, McpSessionRegistry


class _FakeControlPlane:
    def __init__(self, *, mode: str = "auto") -> None:
        self.mode = mode
        self.add_calls = 0
        self.schedule_values = None

    def chat_mode(self, _principal) -> str:
        return self.mode

    def memory_read(self, principal, target: str) -> dict:
        return {
            "ok": True,
            "data": {
                "chat_id": principal.chat_id,
                "workspace": principal.workspace,
                "target": target,
            },
        }

    def memory_add(self, _principal, target: str, text: str) -> dict:
        self.add_calls += 1
        return {"ok": True, "data": {"target": target, "text": text}}

    def schedule_preview(self, _principal, **values) -> dict:
        self.schedule_values = values
        return {"ok": True, "data": values}


def _service(tmp_path: Path, *, mode: str = "auto") -> tuple[CiaoMcpService, _FakeControlPlane]:
    config = SimpleNamespace(
        state_path=tmp_path / ".runtime" / "state.json",
        pwa_port=18443,
        mcp_enabled=True,
    )
    service = CiaoMcpService(config)
    control_plane = _FakeControlPlane(mode=mode)
    service.bind(control_plane)  # type: ignore[arg-type]
    return service, control_plane


def _client(service: CiaoMcpService) -> TestClient:
    @asynccontextmanager
    async def lifespan(_app):
        async with service.lifespan():
            yield

    app = Starlette(
        routes=[Mount("/mcp", app=service.http_app)],
        lifespan=lifespan,
    )
    # FastMCP intentionally rejects arbitrary Host headers. This is the same
    # loopback host used by the managed Claude/Codex process configuration.
    return TestClient(app, base_url="http://127.0.0.1:18443")


def _headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _rpc(client: TestClient, token: str, method: str, params: dict, request_id: int = 1):
    return client.post(
        "/mcp/",
        headers=_headers(token),
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def test_registry_issues_scoped_reusable_and_revocable_tokens() -> None:
    registry = McpSessionRegistry(ttl_seconds=60)
    token, principal = registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )
    repeated, repeated_principal = registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )

    assert repeated == token
    assert repeated_principal == principal
    assert principal.workspace == "personal"
    assert registry.status()["active_sessions"] == 1
    assert registry.revoke_chat("chat-1") == 1
    assert registry.status()["active_sessions"] == 0


def test_streamable_http_auth_and_structured_tool_result(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    token, _ = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )
    initialize = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "ciaobot-test", "version": "1"},
    }

    with _client(service) as client:
        unauthorized = _rpc(client, "", "initialize", initialize)
        assert unauthorized.status_code == 401

        initialized = _rpc(client, token, "initialize", initialize)
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "ciaobot"

        called = _rpc(
            client,
            token,
            "tools/call",
            {"name": "memory_read", "arguments": {"target": "user"}},
            request_id=2,
        )

    assert called.status_code == 200
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "ok": True,
        "data": {"chat_id": "chat-1", "workspace": "personal", "target": "user"},
    }
    telemetry = service._telemetry_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(telemetry[-1])
    assert record["tool"] == "memory_read"
    assert record["chat_id"] == "chat-1"
    assert record["provider"] == "claude"
    assert record["status"] == "ok"


def test_plan_mode_rejects_mutation_before_control_plane_call(tmp_path: Path) -> None:
    service, control_plane = _service(tmp_path, mode="plan")
    token, _ = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="codex",
    )

    with _client(service) as client:
        called = _rpc(
            client,
            token,
            "tools/call",
            {"name": "memory_add", "arguments": {"target": "memory", "text": "fact"}},
        )

    assert called.status_code == 200
    payload = called.json()["result"]["structuredContent"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "plan_mode_read_only"
    assert control_plane.add_calls == 0


def test_catalog_contains_core_pwa_domains(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    names = set(service.status()["tools"])

    assert {
        "memory_read",
        "memory_add",
        "vault_search",
        "project_create",
        "chat_create",
        "schedule_create",
        "loop_create",
        "workspace_file_read",
        "workspace_health_get",
    } <= names


def test_usage_aggregates_telemetry_by_tool(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    records = [
        {"tool": "memory_read", "status": "ok", "duration_ms": 8, "provider": "claude", "timestamp": "2026-07-19T10:00:00Z"},
        {"tool": "memory_read", "status": "ok", "duration_ms": 12, "provider": "codex", "timestamp": "2026-07-19T11:00:00Z"},
        {"tool": "vault_search", "status": "error", "error_code": "invalid_request", "duration_ms": 40, "provider": "claude", "timestamp": "2026-07-19T09:00:00Z"},
    ]
    service._telemetry_path.parent.mkdir(parents=True, exist_ok=True)
    with service._telemetry_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.write("\n")  # blank line is skipped
        handle.write("{not valid json\n")  # malformed line is skipped

    usage = service.usage()

    assert usage["total_calls"] == 3
    assert usage["total_errors"] == 1
    assert usage["tool_count"] == len(service.status()["tools"])
    by_tool = {row["tool"]: row for row in usage["tools"]}
    assert by_tool["memory_read"]["calls"] == 2
    assert by_tool["memory_read"]["errors"] == 0
    assert by_tool["memory_read"]["avg_ms"] == 10
    assert by_tool["memory_read"]["providers"] == ["claude", "codex"]
    assert by_tool["memory_read"]["last_used"] == "2026-07-19T11:00:00Z"
    assert by_tool["vault_search"]["errors"] == 1
    # Registered-but-never-called tools appear with zero counts.
    assert by_tool["chat_create"]["calls"] == 0
    # Sorted by call count descending, so the busiest tool is first.
    assert usage["tools"][0]["tool"] == "memory_read"


def test_usage_endpoint_returns_empty_when_no_telemetry(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    usage = service.usage()
    assert usage["total_calls"] == 0
    assert usage["total_errors"] == 0
    assert all(row["calls"] == 0 for row in usage["tools"])


def test_schedule_handler_does_not_forward_closed_over_service(tmp_path: Path) -> None:
    service, control_plane = _service(tmp_path)
    token, _ = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="codex",
    )

    with _client(service) as client:
        called = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "schedule_preview",
                "arguments": {
                    "prompt": "test",
                    "frequency": "manual",
                    "timezone": "UTC",
                    "project_id": "project-1",
                },
            },
        )

    assert called.json()["result"]["structuredContent"]["ok"] is True
    assert control_plane.schedule_values is not None
    assert "self" not in control_plane.schedule_values


class _LifecyclePcm:
    def __init__(self) -> None:
        self.project = SimpleNamespace(project_id="project-1", workspace="personal")
        self.chat = SimpleNamespace(chat_id="chat-1", project_id="project-1")
        self.completed: list[str] = []
        self.deleted: list[str] = []

    def get_project(self, project_id: str):
        return self.project if project_id == "project-1" else None

    def get_chat(self, chat_id: str):
        return self.chat if chat_id == "chat-1" else None

    def active_chat_ids(self) -> list[str]:
        return []

    def complete_project(self, project_id: str) -> dict:
        self.completed.append(project_id)
        return {"project_id": project_id}

    def delete_project(self, project_id: str) -> bool:
        self.deleted.append(project_id)
        return True


async def _assert_current_project_action_is_deferred(action: str) -> None:
    pcm = _LifecyclePcm()
    config = SimpleNamespace(workspace=lambda name: object() if name == "personal" else None)
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="token-1",
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="codex",
    )

    result = getattr(control_plane, action)(principal, "project-1")

    assert result["data"]["deferred"] is True
    assert not pcm.completed and not pcm.deleted
    await asyncio.sleep(0)
    assert pcm.completed == (["project-1"] if action == "project_complete" else [])
    assert pcm.deleted == (["project-1"] if action == "project_delete" else [])


def test_current_project_complete_and_delete_are_deferred() -> None:
    asyncio.run(_assert_current_project_action_is_deferred("project_complete"))
    asyncio.run(_assert_current_project_action_is_deferred("project_delete"))


class _ChatCreatePcm:
    def __init__(self) -> None:
        self.projects = {
            "project-1": SimpleNamespace(project_id="project-1", name="Ciaobot Improvements", workspace="personal"),
            "project-2": SimpleNamespace(project_id="project-2", name="Research", workspace="personal"),
        }
        self.created: list[dict] = []
        self.queued: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [p for p in self.projects.values() if workspace is None or p.workspace == workspace]

    def create_chat(self, project_id, **kwargs):
        self.created.append({"project_id": project_id, **kwargs})
        return SimpleNamespace(
            chat_id="chat-new",
            project_id=project_id,
            to_dict=lambda local=True: {"chat_id": "chat-new", "project_id": project_id},
        )

    def queue_message(self, chat_id: str, text: str) -> bool:
        self.queued.append((chat_id, text))
        return False

    def start_stream(self, chat_id: str, text: str) -> None:
        self.started.append((chat_id, text))


def _chat_create_control_plane(pcm: _ChatCreatePcm) -> CiaoControlPlane:
    config = SimpleNamespace(workspace=lambda name: object() if name == "personal" else None)
    return CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )


def _chat_create_principal(**overrides) -> McpPrincipal:
    defaults = dict(
        token_id="token-1",
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="codex",
    )
    defaults.update(overrides)
    return McpPrincipal(**defaults)


def test_chat_create_defaults_to_callers_current_project() -> None:
    pcm = _ChatCreatePcm()
    control_plane = _chat_create_control_plane(pcm)
    principal = _chat_create_principal()

    result = control_plane.chat_create(principal, None)

    assert result["data"]["project_id"] == "project-1"
    assert pcm.created[0]["project_id"] == "project-1"


def test_chat_create_resolves_project_by_case_insensitive_name() -> None:
    pcm = _ChatCreatePcm()
    control_plane = _chat_create_control_plane(pcm)
    principal = _chat_create_principal()

    result = control_plane.chat_create(principal, "research")

    assert result["data"]["project_id"] == "project-2"


def test_chat_create_rejects_unknown_project_name() -> None:
    pcm = _ChatCreatePcm()
    control_plane = _chat_create_control_plane(pcm)
    principal = _chat_create_principal()

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.chat_create(principal, "does-not-exist")
    assert excinfo.value.code == "project_not_found"


def test_chat_create_with_prompt_sends_first_turn_immediately() -> None:
    pcm = _ChatCreatePcm()
    control_plane = _chat_create_control_plane(pcm)
    principal = _chat_create_principal()

    result = control_plane.chat_create(principal, None, prompt="Let's research the new API changes.")

    assert result["data"]["send_status"] == "started"
    assert pcm.started == [("chat-new", "Let's research the new API changes.")]


def test_schedule_create_resolves_project_by_name(tmp_path: Path) -> None:
    from ciao.schedules import ScheduleManager, ScheduleStore

    pcm = _ChatCreatePcm()
    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    schedules = ScheduleManager(store=ScheduleStore(tmp_path), dispatch_to_web=dispatch)
    config = SimpleNamespace(workspace=lambda name: object() if name == "personal" else None)
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=schedules,
        loop_manager=SimpleNamespace(),
    )
    principal = _chat_create_principal()

    result = control_plane.schedule_create(
        principal,
        prompt="Check for new signals.",
        daily_time="09:00",
        timezone="UTC",
        frequency="weekly",
        project_id="research",
    )

    assert result["data"]["web_project_id"] == "project-2"


def test_adversarial_review_synthesizes_panel_results(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(),
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = _chat_create_principal(project_id=None, chat_id=None)

    async def fake_oneshot(prompt, *, system_prompt, model, env, timeout_s=120.0, provider="claude", cwd=None):
        return json.dumps({"verdict": "revise", "confidence": 4, "summary": "solid but needs work", "issues": []})

    monkeypatch.setattr("ciao.critique.run_oneshot", fake_oneshot)

    result = asyncio.run(control_plane.adversarial_review(principal, "Draft artifact text.", models="opus,fable"))

    assert result["ok"] is True
    assert result["data"]["model_count"] == 2
    assert result["data"]["ok_count"] == 2
    assert result["data"]["verdicts"] == {"revise": 2}
    assert "Adversarial review" in result["data"]["markdown"]


def test_adversarial_review_rejects_empty_artifact() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=SimpleNamespace(),
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = _chat_create_principal(project_id=None, chat_id=None)

    with pytest.raises(ControlPlaneError) as excinfo:
        asyncio.run(control_plane.adversarial_review(principal, "   "))
    assert excinfo.value.code == "empty_artifact"
