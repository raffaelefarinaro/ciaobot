from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from ciao import mcp_server
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.execution_modes import AUTO_APPROVED_MCP_TOOLS, auto_approved_mcp_tool_names
from ciao.mcp_server import CiaoMcpService, McpSessionRegistry


class _FakeControlPlane:
    def __init__(self, *, mode: str = "auto") -> None:
        self.mode = mode
        self.create_calls = 0
        self.schedule_values = None
        self.schedule_create_values: dict | None = None
        self.schedule_updates: list[tuple[str, dict]] = []
        self.loop_updates: list[tuple[str, dict]] = []
        self.loop_lifecycle: list[tuple[str, str]] = []

    def chat_mode(self, _principal) -> str:
        return self.mode

    def context_get(self, principal) -> dict:
        return {
            "ok": True,
            "data": {
                "chat_id": principal.chat_id,
                "workspace": principal.workspace,
            },
        }

    def system_status_get(self, _principal) -> dict:
        return {"ok": True, "data": {"server": "ok"}}

    def schedule_create(self, _principal, **values) -> dict:
        self.create_calls += 1
        self.schedule_create_values = values
        return {"ok": True, "data": values}

    def schedule_preview(self, _principal, **values) -> dict:
        self.schedule_values = values
        return {"ok": True, "data": values}

    def schedule_update(self, _principal, schedule_id, **changes) -> dict:
        self.schedule_updates.append((schedule_id, changes))
        return {"ok": True, "data": {"schedule_id": schedule_id, **changes}}

    def loop_update(self, _principal, loop_id, **changes) -> dict:
        self.loop_updates.append((loop_id, changes))
        return {"ok": True, "data": {"loop_id": loop_id, **changes}}

    def loop_start(self, _principal, loop_id) -> dict:
        self.loop_lifecycle.append(("start", loop_id))
        return {"ok": True, "data": {"loop_id": loop_id}}

    def loop_stop(self, _principal, loop_id) -> dict:
        self.loop_lifecycle.append(("stop", loop_id))
        return {"ok": True, "data": {"loop_id": loop_id, "running": False}}


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
    # loopback host used by the managed Claude/opencode process configuration.
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


def test_registry_reissues_when_workspace_or_project_changes() -> None:
    registry = McpSessionRegistry(ttl_seconds=60)
    token, principal = registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )

    moved_token, moved = registry.issue(
        chat_id="chat-1",
        project_id="project-work",
        workspace="work",
        provider="claude",
    )

    assert moved_token != token
    assert moved.workspace == "work"
    assert moved.project_id == "project-work"
    assert principal.workspace == "personal"
    assert registry.status()["active_sessions"] == 1


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
            {"name": "context_get", "arguments": {}},
            request_id=2,
        )

    assert called.status_code == 200
    result = called.json()["result"]
    assert result["isError"] is False
    # system_status_get is folded into context_get under the "system" key.
    assert result["structuredContent"] == {
        "ok": True,
        "data": {
            "chat_id": "chat-1",
            "workspace": "personal",
            "system": {"server": "ok"},
        },
    }
    telemetry = service._telemetry_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(telemetry[-1])
    assert record["tool"] == "context_get"
    assert record["chat_id"] == "chat-1"
    assert record["provider"] == "claude"
    assert record["status"] == "ok"


def test_plan_mode_rejects_mutation_before_control_plane_call(tmp_path: Path) -> None:
    service, control_plane = _service(tmp_path, mode="plan")
    token, _ = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="opencode",
    )

    with _client(service) as client:
        called = _rpc(
            client,
            token,
            "tools/call",
            {"name": "schedule", "arguments": {"action": "create", "prompt": "do a thing"}},
        )

    assert called.status_code == 200
    payload = called.json()["result"]["structuredContent"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "plan_mode_read_only"
    assert control_plane.create_calls == 0


def test_vault_search_telemetry_keeps_only_relative_result_paths(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    _token, principal = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="claude",
    )

    service._record_tool_call(
        name="vault_search",
        principal=principal,
        status="ok",
        error_code="",
        duration_ms=7,
        value={
            "ok": True,
            "data": [{"path": "memory-vault/personal/Workspace/Ada.md"}],
        },
    )

    record = json.loads(service._telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["result_count"] == 1
    assert record["result_paths"] == ["memory-vault/personal/Workspace/Ada.md"]
    assert "content" not in record


def test_catalog_contains_core_pwa_domains(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    names = set(service.status()["tools"])

    assert {
        "context_get",
        "memory_status",
        "memory_update",
        "vault_search",
        "project",
        "project_action",
        "workspace_create",
        "chat_create",
        "schedule",
        "schedule_action",
        "loop",
        "loop_action",
        "chat_handover",
    } <= names

    # Tools migrated to the ciao CLI, PWA, or the provider's native tools are gone.
    assert not (
        {
            "memory_read",
            "memory_add",
            "workspace_file_read",
            "workspace_health_get",
            "skills_sync",
            "capabilities_get",
            "chat_new_session",
            "chat_retry_update",
            # Folded into `schedule` / `loop` / `project` / `project_action`.
            "schedule_preview",
            "schedule_create",
            "schedule_update",
            "loop_create",
            "loop_update",
            "project_create",
            "project_update",
            "project_complete",
            "project_restore",
            "project_delete",
            # Moved to PWA Settings / skill / native Glob.
            "workspace_update",
            "workspace_delete",
            "project_files_list",
            "adversarial_review",
        }
        & names
    )


def test_usage_aggregates_telemetry_by_tool(tmp_path: Path) -> None:
    service, _control_plane = _service(tmp_path)
    records = [
        {"tool": "memory_read", "status": "ok", "duration_ms": 8, "provider": "claude", "timestamp": "2026-07-19T10:00:00Z"},
        {"tool": "memory_read", "status": "ok", "duration_ms": 12, "provider": "opencode", "timestamp": "2026-07-19T11:00:00Z"},
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
    assert by_tool["memory_read"]["providers"] == ["claude", "opencode"]
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
        provider="opencode",
    )

    with _client(service) as client:
        called = _rpc(
            client,
            token,
            "tools/call",
            {
                "name": "schedule",
                "arguments": {
                    "action": "preview",
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


def _schedule_token(service: CiaoMcpService) -> str:
    token, _ = service.registry.issue(
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="opencode",
    )
    return token


def _call(service: CiaoMcpService, name: str, arguments: dict) -> dict:
    """Call one tool over the real MCP transport and return its JSON-RPC result.

    Tool-level raises (bad `action`, missing id) come back as `isError` with no
    structuredContent, so tests read the whole result rather than just the
    payload.
    """
    token = _schedule_token(service)
    with _client(service) as client:
        called = _rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    result: dict = called.json()["result"]
    return result


def test_schedule_update_forwards_values_that_equal_the_create_defaults(
    tmp_path: Path,
) -> None:
    """"Move the daily report to 09:00" reported ok and changed nothing.

    The update branch dropped every field equal to a create default, so
    daily_time="09:00", frequency="weekly", archive_policy="manual" and the ""
    clears never reached schedule_update — which then ran with an empty payload
    and still returned ok.
    """
    service, control_plane = _service(tmp_path)

    result = _call(
        service,
        "schedule",
        {
            "action": "update",
            "schedule_id": "sched-1",
            "daily_time": "09:00",
            "frequency": "weekly",
            "archive_policy": "manual",
            "title": "",
        },
    )

    assert result["structuredContent"]["ok"] is True
    # Exactly the fields the caller passed — no omitted field is invented.
    assert control_plane.schedule_updates == [
        (
            "sched-1",
            {
                "daily_time": "09:00",
                "frequency": "weekly",
                "archive_policy": "manual",
                "title": "",
            },
        )
    ]


def test_schedule_update_refuses_a_payload_with_nothing_to_change(tmp_path: Path) -> None:
    """A no-op update must not report success — that is how the silent no-op
    above stayed invisible."""
    service, control_plane = _service(tmp_path)

    result = _call(service, "schedule", {"action": "update", "schedule_id": "sched-1"})

    assert result["isError"] is True
    assert "at least one field" in result["content"][0]["text"]
    assert control_plane.schedule_updates == []


def test_schedule_create_still_applies_the_documented_defaults(tmp_path: Path) -> None:
    """The signature defaults moved to None, so create has to materialize them."""
    service, control_plane = _service(tmp_path)

    result = _call(service, "schedule", {"action": "create", "prompt": "do a thing"})

    assert result["structuredContent"]["ok"] is True
    assert control_plane.schedule_create_values is not None
    assert control_plane.schedule_create_values["daily_time"] == "09:00"
    assert control_plane.schedule_create_values["timezone"] == "UTC"
    assert control_plane.schedule_create_values["frequency"] == "weekly"
    assert control_plane.schedule_create_values["archive_policy"] == "manual"
    assert control_plane.schedule_create_values["workspace"] == ""
    # Fields with no create default stay unset rather than becoming "".
    assert control_plane.schedule_create_values["days_of_week"] is None
    assert "self" not in control_plane.schedule_create_values


def test_update_refuses_to_clear_a_prompt(tmp_path: Path) -> None:
    """"" now reaches the control plane (that is how a title/provider/model is
    cleared), but neither schedule_update nor loop_update rejects a blank
    prompt — an automation with no prompt would keep firing on nothing."""
    # One service per call: the streamable-HTTP session manager refuses a
    # second lifespan.
    schedule_service, schedule_plane = _service(tmp_path / "schedule")
    loop_service, loop_plane = _service(tmp_path / "loop")

    schedule_result = _call(
        schedule_service,
        "schedule",
        {"action": "update", "schedule_id": "sched-1", "prompt": ""},
    )
    loop_result = _call(
        loop_service, "loop", {"action": "update", "loop_id": "loop-1", "prompt": ""}
    )

    assert schedule_result["isError"] is True
    assert loop_result["isError"] is True
    assert schedule_plane.schedule_updates == []
    assert loop_plane.loop_updates == []


def test_loop_update_forwards_the_create_default_interval(tmp_path: Path) -> None:
    """interval_minutes=10 equals the create default, so it was dropped and the
    loop kept its old cadence while the call reported ok."""
    service, control_plane = _service(tmp_path)

    result = _call(
        service,
        "loop",
        {"action": "update", "loop_id": "loop-1", "interval_minutes": 10},
    )

    assert result["structuredContent"]["ok"] is True
    assert control_plane.loop_updates == [("loop-1", {"interval_minutes": 10})]


def test_loop_update_applies_start_false_through_the_lifecycle(tmp_path: Path) -> None:
    """`start` is a runtime flag, not a stored field: forwarding it to
    loop_update failed with `invalid_fields: start`, so an update that also
    stopped the loop errored out entirely."""
    service, control_plane = _service(tmp_path)

    result = _call(
        service,
        "loop",
        {"action": "update", "loop_id": "loop-1", "title": "watch", "start": False},
    )

    assert result["structuredContent"]["ok"] is True
    assert control_plane.loop_updates == [("loop-1", {"title": "watch"})]
    assert control_plane.loop_lifecycle == [("stop", "loop-1")]
    assert result["structuredContent"]["data"]["running"] is False


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
        provider="opencode",
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
    def __init__(self, *, parent_mode: str = "auto") -> None:
        self.projects = {
            "project-1": SimpleNamespace(project_id="project-1", name="Ciaobot Improvements", workspace="personal"),
            "project-2": SimpleNamespace(project_id="project-2", name="Research", workspace="personal"),
        }
        self.parent_mode = parent_mode
        self.created: list[dict] = []
        self.queued: list[tuple[str, str]] = []
        self.started: list[tuple[str, str]] = []

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [p for p in self.projects.values() if workspace is None or p.workspace == workspace]

    def get_chat(self, chat_id: str):
        if chat_id == "chat-1":
            return SimpleNamespace(chat_id=chat_id, mode=self.parent_mode)
        return None

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


def _chat_create_control_plane(
    pcm: _ChatCreatePcm,
    *,
    schedule_manager: Any = None,
    workspaces: tuple[str, ...] = ("personal",),
) -> CiaoControlPlane:
    config = SimpleNamespace(workspace=lambda name: object() if name in workspaces else None)
    return CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace() if schedule_manager is None else schedule_manager,
        loop_manager=SimpleNamespace(),
    )


def _work_project_pcm() -> _ChatCreatePcm:
    """A pcm holding a project in a non-Personal workspace, to prove inheritance.

    Also carries that workspace's General project, since every workspace gets
    one at init and a cross-workspace reassignment re-points onto it.
    """
    pcm = _ChatCreatePcm()
    pcm.projects["project-work"] = SimpleNamespace(
        project_id="project-work",
        name="AI-NATIVE-SDK",
        workspace="work",
    )
    pcm.projects["project-work-general"] = SimpleNamespace(
        project_id="project-work-general",
        name="General",
        workspace="work",
    )
    return pcm


def _schedule_control_plane(tmp_path: Path, pcm: _ChatCreatePcm) -> tuple[CiaoControlPlane, Any]:
    """Control plane wired to a real ScheduleManager over a temp store."""
    from ciao.schedules import ScheduleManager, ScheduleStore

    schedules = ScheduleManager(store=ScheduleStore(tmp_path), dispatch_to_web=lambda *a, **k: None)
    plane = _chat_create_control_plane(
        pcm, schedule_manager=schedules, workspaces=("personal", "work")
    )
    return plane, schedules


def _chat_create_principal(**overrides) -> McpPrincipal:
    defaults = dict(
        token_id="token-1",
        chat_id="chat-1",
        project_id="project-1",
        workspace="personal",
        provider="opencode",
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


def test_chat_create_clamps_child_mode_to_calling_chat() -> None:
    pcm = _ChatCreatePcm(parent_mode="normal")
    control_plane = _chat_create_control_plane(pcm)

    result = control_plane.chat_create(_chat_create_principal(), mode="bypass")

    assert pcm.created[-1]["mode"] == "normal"
    assert result["data"]["mode_clamped"] is True
    assert result["data"]["requested_mode"] == "bypass"


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
    assert result["data"]["workspace"] == "personal"
    assert result["data"]["project_name"] == "Research"


def test_schedule_create_defaults_to_callers_project_and_workspace(tmp_path: Path) -> None:
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    principal = _chat_create_principal(
        project_id="project-work",
        workspace="work",
    )

    result = control_plane.schedule_create(
        principal,
        prompt="Seed the weekly skills CSV.",
        daily_time="08:00",
        timezone="Europe/Zurich",
        frequency="weekly",
        days_of_week=["mon"],
    )

    assert result["data"]["web_project_id"] == "project-work"
    assert result["data"]["workspace"] == "work"
    assert result["data"]["project_name"] == "AI-NATIVE-SDK"
    stored = schedules.list_entries()[0]
    assert stored.web_project_id == "project-work"
    assert stored.web_project_name == "AI-NATIVE-SDK"
    assert stored.workspace == "work"


def test_schedule_create_with_chat_id_skips_project_default(tmp_path: Path) -> None:
    pcm = _work_project_pcm()
    chat = SimpleNamespace(chat_id="chat-fixed", project_id="project-work")
    pcm.get_chat = lambda cid: chat if cid == "chat-fixed" else None  # type: ignore[method-assign]
    control_plane, _schedules = _schedule_control_plane(tmp_path, pcm)
    principal = _chat_create_principal(
        project_id="project-work",
        workspace="work",
    )

    result = control_plane.schedule_create(
        principal,
        prompt="Continue this thread weekly.",
        daily_time="08:00",
        timezone="UTC",
        frequency="weekly",
        chat_id="chat-fixed",
    )

    assert result["data"]["web_chat_id"] == "chat-fixed"
    assert result["data"]["web_project_id"] is None
    assert result["data"]["workspace"] == "work"


# ── workspace boundary for schedule targeting ────────────────────────────
#
# MCP tokens are scoped to one workspace, and a schedule is auto-approved
# model input whose unattended runs execute in bypass inside the target
# workspace. So no cross-workspace targeting exists here at all: naming
# another registered workspace is refused before any foreign project or
# chat is resolved, and moving an existing schedule out is refused the
# same way.


def test_schedule_create_rejects_a_foreign_registered_workspace(
    tmp_path: Path,
) -> None:
    """`work` exists and holds resolvable projects; none of it may be touched."""
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    principal = _chat_create_principal()  # personal

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_create(
            principal,
            prompt="Run the weekly self-improvement review.",
            daily_time="02:01",
            timezone="Europe/Zurich",
            frequency="weekly",
            days_of_week=["sun"],
            workspace="work",
        )
    assert excinfo.value.code == "workspace_forbidden"
    assert "'personal'" in str(excinfo.value)
    assert schedules.list_entries() == []


def test_schedule_create_cross_workspace_rejects_foreign_project_id(
    tmp_path: Path,
) -> None:
    """The workspace guard fires before any project resolution."""
    control_plane, _schedules = _schedule_control_plane(tmp_path, _work_project_pcm())

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_preview(
            _chat_create_principal(),  # personal
            prompt="p",
            daily_time="09:00",
            frequency="daily",
            project_id="project-1",  # personal project named for a work target
            workspace="work",
        )
    assert excinfo.value.code == "workspace_forbidden"


def test_schedule_create_cross_workspace_rejects_chat_binding(
    tmp_path: Path,
) -> None:
    control_plane, _schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    pcm = _ChatCreatePcm()

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_preview(
            _chat_create_principal(),
            prompt="p",
            daily_time="09:00",
            frequency="daily",
            chat_id="chat-1",
            workspace="work",
        )
    assert excinfo.value.code == "workspace_forbidden"
    assert pcm.created == []


def test_schedule_create_refuses_any_foreign_name_even_an_unregistered_one(
    tmp_path: Path,
) -> None:
    """Scoping is checked before the registry, so a typo reads as forbidden."""
    control_plane, _schedules = _schedule_control_plane(tmp_path, _ChatCreatePcm())

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_preview(
            _chat_create_principal(),
            prompt="p",
            daily_time="09:00",
            frequency="daily",
            workspace="does-not-exist",
        )
    assert excinfo.value.code == "workspace_forbidden"


def test_schedule_preview_validates_the_scoped_workspace_exists(
    tmp_path: Path,
) -> None:
    """Omitting workspace still validates the principal's own registration."""
    control_plane, _schedules = _schedule_control_plane(tmp_path, _ChatCreatePcm())

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_preview(
            _chat_create_principal(workspace="nowhere"),
            prompt="p",
            daily_time="09:00",
            frequency="daily",
        )
    assert excinfo.value.code == "workspace_not_found"


def test_schedule_create_with_explicit_own_workspace_still_works(
    tmp_path: Path,
) -> None:
    """Restating the caller's own workspace is a no-op, not a rejection."""
    control_plane, schedules = _schedule_control_plane(tmp_path, _ChatCreatePcm())

    result = control_plane.schedule_create(
        _chat_create_principal(),
        prompt="Check for new signals.",
        daily_time="09:00",
        timezone="UTC",
        frequency="weekly",
        workspace="personal",
    )

    assert result["data"]["workspace"] == "personal"
    # Workspace omitted or restated, the active project is inherited as usual.
    assert result["data"]["web_project_id"] == "project-1"
    stored = schedules.list_entries()[0]
    assert stored.workspace == "personal"
    assert stored.web_project_id == "project-1"


def test_schedule_update_rejects_moving_an_entry_to_another_workspace(
    tmp_path: Path,
) -> None:
    """Refused even when the move names a real project in the destination."""
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    entry = schedules.create(
        daily_time_utc="09:00", prompt="p", model="", mode="auto",
        chat_id=0, workspace="personal",
        web_project_id="project-1", web_project_name="Ciaobot Improvements",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_update(
            _chat_create_principal(),  # personal
            entry.schedule_id,
            workspace="work",
            project_id="ai-native-sdk",  # work project, resolved by name
        )
    assert excinfo.value.code == "workspace_forbidden"
    stored = schedules.list_entries()[0]
    assert stored.workspace == "personal"
    assert stored.web_project_id == "project-1"


def test_schedule_update_rejects_a_bare_move_and_keeps_the_bindings(
    tmp_path: Path,
) -> None:
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    entry = schedules.create(
        daily_time_utc="09:00", prompt="p", model="", mode="auto",
        chat_id=0, workspace="personal",
        web_chat_id="chat-1",
        web_project_id="project-1", web_project_name="Ciaobot Improvements",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_update(
            _chat_create_principal(), entry.schedule_id, workspace="work"
        )
    assert excinfo.value.code == "workspace_forbidden"
    stored = schedules.list_entries()[0]
    assert stored.workspace == "personal"
    assert stored.web_chat_id == "chat-1"
    assert stored.web_project_id == "project-1"


def test_schedule_update_restating_the_current_workspace_still_works(
    tmp_path: Path,
) -> None:
    """A restated workspace is not a move: run bindings survive untouched."""
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    entry = schedules.create(
        daily_time_utc="09:00", prompt="p", model="", mode="auto",
        chat_id=0, workspace="personal",
        web_project_id="project-1", web_project_name="Ciaobot Improvements",
    )

    updated = control_plane.schedule_update(
        _chat_create_principal(), entry.schedule_id,
        workspace="personal", title="Renamed",
    )

    assert updated["data"]["workspace"] == "personal"
    assert updated["data"]["web_project_id"] == "project-1"
    assert updated["data"]["title"] == "Renamed"
    stored = schedules.list_entries()[0]
    assert stored.workspace == "personal"
    assert stored.web_project_id == "project-1"


def test_schedule_update_rejects_unregistered_and_foreign_names_alike(
    tmp_path: Path,
) -> None:
    control_plane, schedules = _schedule_control_plane(tmp_path, _work_project_pcm())
    entry = schedules.create(
        daily_time_utc="09:00", prompt="p", model="", mode="auto",
        chat_id=0, workspace="personal",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        control_plane.schedule_update(_chat_create_principal(), entry.schedule_id, workspace="nowhere")
    assert excinfo.value.code == "workspace_forbidden"
    assert schedules.list_entries()[0].workspace == "personal"


def test_chat_update_resolves_omitted_chat_id() -> None:
    pcm = SimpleNamespace(
        update_chat=lambda cid, **kwargs: SimpleNamespace(
            chat_id=cid,
            control_surface="",
            to_dict=lambda local=True: {"chat_id": cid, **kwargs},
        ),
        is_session_local=lambda c: True,
        get_chat=lambda cid: SimpleNamespace(chat_id=cid, project_id="project-1"),
        get_project=lambda pid: SimpleNamespace(project_id=pid, workspace="personal"),
    )
    config = SimpleNamespace(workspace=lambda name: object() if name == "personal" else None)
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = _chat_create_principal()

    result = control_plane.chat_update(principal, "", title="Renamed")

    assert result["data"]["chat_id"] == "chat-1"
    assert result["data"]["title"] == "Renamed"


def test_loop_create_defaults_to_caller_and_stamps_workspace(tmp_path: Path) -> None:
    from ciao.loops import LoopManager, LoopStore

    pcm = _ChatCreatePcm()
    pcm.projects["project-work"] = SimpleNamespace(
        project_id="project-work",
        name="AI-NATIVE-SDK",
        workspace="work",
    )
    pcm.get_chat = lambda cid: (  # type: ignore[method-assign]
        SimpleNamespace(chat_id="chat-work", project_id="project-work")
        if cid == "chat-work"
        else None
    )
    manager = LoopManager(store=LoopStore(tmp_path))
    config = SimpleNamespace(
        workspace=lambda name: object() if name in {"personal", "work"} else None
    )
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=manager,
    )
    principal = _chat_create_principal(
        chat_id="chat-work",
        project_id="project-work",
        workspace="work",
    )

    result = control_plane.loop_create(principal, "", "Check PRs", interval_minutes=15)

    assert result["data"]["web_chat_id"] == "chat-work"
    assert result["data"]["web_project_id"] == "project-work"
    assert result["data"]["workspace"] == "work"
    assert result["data"]["interval_minutes"] == 15


@pytest.mark.asyncio
async def test_chat_archive_defaults_to_caller_chat() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    archived_calls = []

    async def _archive_chat(cid: str) -> SimpleNamespace:
        archived_calls.append(cid)
        return SimpleNamespace(
            outcome=SimpleNamespace(path=Path("/tmp/chat.md")),
            delegates=[],
            stopped_ids=lambda: [],
            failed_ids=lambda: [],
        )

    fake_pcm = SimpleNamespace(
        archive_chat=_archive_chat,
        run_archive_postprocess=lambda cid, outcome, chat, project: None,
        active_chat_ids=lambda: set(),
    )
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=fake_pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    control_plane._chat = lambda p, cid: SimpleNamespace(project_id="p1")
    control_plane._project = lambda p, pid: SimpleNamespace(name="Project")
    principal = McpPrincipal(
        token_id="t1",
        chat_id="chat-active-123",
        project_id="p1",
        workspace="personal",
        provider="claude",
    )

    # Calling chat_archive with empty or "this chat" targets principal.chat_id
    res1 = await control_plane.chat_archive(principal, "")
    assert res1["ok"] is True
    assert res1["data"]["deferred"] is True
    assert res1["data"]["chat_id"] == "chat-active-123"

    res2 = await control_plane.chat_archive(principal, "this chat")
    assert res2["ok"] is True
    assert res2["data"]["deferred"] is True
    assert res2["data"]["chat_id"] == "chat-active-123"

    # Calling with specific another chat archives that chat directly
    res3 = await control_plane.chat_archive(principal, "chat_other_999")
    assert res3["ok"] is True
    assert res3["data"]["chat_id"] == "chat_other_999"
    assert "chat_other_999" in archived_calls


def test_project_and_chat_resolution_defaults() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    fake_pcm = SimpleNamespace(
        get_chat=lambda cid: SimpleNamespace(chat_id=cid, project_id="proj-active-123", to_dict=lambda **k: {"chat_id": cid}),
        get_project=lambda pid: SimpleNamespace(project_id=pid, name="Active Project", workspace="personal", to_dict=lambda: {"project_id": pid}),
        list_projects=lambda ws: [SimpleNamespace(project_id="proj-active-123", name="Active Project", workspace="personal")],
        is_session_local=lambda c: True,
    )
    control_plane = CiaoControlPlane(
        config,
        project_chat_manager=fake_pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    principal = McpPrincipal(
        token_id="t1",
        chat_id="chat-active-123",
        project_id="proj-active-123",
        workspace="personal",
        provider="claude",
    )

    # project_get defaults to active project when empty or 'this project'
    p_res1 = control_plane.project_get(principal, "")
    assert p_res1["ok"] is True
    assert p_res1["data"]["project_id"] == "proj-active-123"

    p_res2 = control_plane.project_get(principal, "this project")
    assert p_res2["ok"] is True
    assert p_res2["data"]["project_id"] == "proj-active-123"

    # chat_get defaults to active chat when empty or 'this chat'
    c_res1 = control_plane.chat_get(principal, "")
    assert c_res1["ok"] is True
    assert c_res1["data"]["chat_id"] == "chat-active-123"

    c_res2 = control_plane.chat_get(principal, "self")
    assert c_res2["ok"] is True
    assert c_res2["data"]["chat_id"] == "chat-active-123"


# ── workspace registry tools ────────────────────────────────────────────


def _workspace_control_plane(tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> tuple[CiaoControlPlane, Any, list[str]]:
    """Control plane over a real CiaoConfig registry; returns (plane, config, refreshes)."""
    from ciao.config import CiaoConfig, WorkspaceConfig

    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )
    refreshes: list[str] = []
    pcm = SimpleNamespace(
        refresh_workspaces=lambda: refreshes.append("refresh"),
        active_chat_ids=lambda: [],
    )
    plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )
    return plane, config, refreshes


def test_workspaces_list_lists_all_configured_workspaces(tmp_path: Path) -> None:
    plane, _config, _refreshes = _workspace_control_plane(tmp_path)
    principal = _chat_create_principal()

    result = plane.workspaces_list(principal)

    assert result["ok"] is True
    names = [item["name"] for item in result["data"]["workspaces"]]
    assert names == ["personal", "work"]
    personal = result["data"]["workspaces"][0]
    assert personal["vault_root"].endswith("memory-vault/personal")
    assert personal["default_provider"] == "claude"


def test_workspace_create_registers_and_persists(tmp_path: Path) -> None:
    plane, config, refreshes = _workspace_control_plane(tmp_path)
    principal = _chat_create_principal()

    result = plane.workspace_create(
        principal,
        name="research",
        default_provider="opencode",
        default_model="opencode/big-pickle",
        gws_profile="work",
        disallowed_tools=["Bash"],
        color="cyan",
    )

    assert result["ok"] is True
    assert result["data"]["name"] == "research"
    assert result["data"]["default_provider"] == "opencode"
    assert result["data"]["disallowed_tools"] == ["Bash"]
    assert result["data"]["color"] == "cyan"
    assert refreshes == ["refresh"]
    assert config.workspace("research") is not None
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    assert {item["name"] for item in stored} == {"personal", "work", "research"}


def test_workspace_create_rejects_conflicts_and_bad_provider(tmp_path: Path) -> None:
    plane, config, _refreshes = _workspace_control_plane(tmp_path)
    principal = _chat_create_principal()

    with pytest.raises(ValueError, match="conflicts with existing workspace"):
        plane.workspace_create(principal, name="Personal")

    with pytest.raises(ValueError, match="default_provider must be one of"):
        plane.workspace_create(principal, name="research", default_provider="ollama")

    assert config.workspace("research") is None


def test_collect_env_refs_from_headers_and_env_block() -> None:
    from ciao.mcp_server import _collect_env_refs

    refs = _collect_env_refs(
        {
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${N8N_MCP_TOKEN}"},
            "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
        }
    )
    by_key = dict(refs)
    assert by_key["N8N_MCP_TOKEN"] == "headers"
    assert by_key["NOTION_TOKEN"] == "env"


def test_discover_project_servers_reports_env_and_observed_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = workspace / ".runtime"
    runtime.mkdir()
    (workspace / ".env").write_text("N8N_MCP_TOKEN=secret-token\n", encoding="utf-8")
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "n8n_mcp": {
                        "type": "http",
                        "url": "https://example.test/mcp-server/http",
                        "headers": {"Authorization": "Bearer ${N8N_MCP_TOKEN}"},
                    },
                    "notion": {
                        "command": "npx",
                        "args": ["-y", "@notionhq/notion-mcp-server"],
                        "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "agent_tool_calls.jsonl").write_text(
        json.dumps({"tool": "mcp__notion__API-retrieve-a-page"}) + "\n"
        + json.dumps({"tool": "Bash"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("N8N_MCP_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    config = SimpleNamespace(
        state_path=runtime / "state.json",
        pwa_port=18443,
        mcp_enabled=True,
        workspace_root=workspace,
    )
    service = CiaoMcpService(config)
    payload = service.status_for_api()
    by_name = {row["name"]: row for row in payload["project_servers"]}

    assert payload["env_path"] == str(workspace / ".env")
    assert "_meta" not in by_name["n8n_mcp"]
    assert by_name["n8n_mcp"]["ready"] is True
    assert by_name["n8n_mcp"]["env_keys"][0]["key"] == "N8N_MCP_TOKEN"
    assert by_name["n8n_mcp"]["env_keys"][0]["configured"] is True
    assert by_name["notion"]["ready"] is False
    assert by_name["notion"]["command"] == "npx"
    assert by_name["notion"]["args"] == ["-y", "@notionhq/notion-mcp-server"]
    assert by_name["notion"]["tools"] == ["mcp__notion__API-retrieve-a-page"]
    assert by_name["notion"]["tools_source"] == "observed"
    assert service.project_server_env_keys() == {"N8N_MCP_TOKEN", "NOTION_TOKEN"}


def test_probe_stdio_server_returns_observed_tools_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = workspace / ".runtime"
    runtime.mkdir()
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "notion": {
                        "command": "npx",
                        "args": ["-y", "@notionhq/notion-mcp-server"],
                        "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (runtime / "agent_tool_calls.jsonl").write_text(
        json.dumps({"tool": "mcp__notion__search"}) + "\n",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        state_path=runtime / "state.json",
        pwa_port=18443,
        mcp_enabled=True,
        workspace_root=workspace,
    )
    service = CiaoMcpService(config)
    result = service.probe_project_server_tools("notion")
    assert result["ok"] is True
    assert result["tools"] == ["mcp__notion__search"]
    assert "Stdio" in result["tools_note"]


def test_auto_approved_policy_matches_tool_annotations() -> None:
    """The allowed_tools policy must track the annotations on the tools.

    ``AUTO_APPROVED_MCP_TOOLS`` bypasses the PermissionGate, so a new tool
    silently inheriting either policy is the failure mode worth catching. The
    contract: every ``_READ``/``_WRITE`` tool is auto-approved, every
    ``_DESTRUCTIVE`` one still raises an approval card.
    """
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    declared = re.findall(r'@tool\(name="([a-z_]+)", annotations=(_[A-Z]+)', source)
    assert declared, "no annotated @tool declarations found in ciao/mcp_server.py"

    expected = [name for name, ann in declared if ann in {"_READ", "_WRITE"}]
    destructive = {name for name, ann in declared if ann == "_DESTRUCTIVE"}

    assert list(AUTO_APPROVED_MCP_TOOLS) == expected
    assert destructive.isdisjoint(AUTO_APPROVED_MCP_TOOLS)
    assert auto_approved_mcp_tool_names()[0] == f"mcp__ciaobot__{expected[0]}"


class _StreamPcm:
    """Fake pcm exposing only what `_file_surface_signal` reads."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream

    def get_active_stream(self, chat_id: str) -> Any:
        return self._stream


def _fake_ws(host: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host, port=1), headers={})


def _file_surface_plane(
    tmp_path: Path, *, stream: Any = None, connection_tracker: Any = None
) -> CiaoControlPlane:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = SimpleNamespace(workspace_root=workspace)
    return CiaoControlPlane(
        config,
        project_chat_manager=_StreamPcm(stream),
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
        connection_tracker=connection_tracker,
    )


def test_file_surface_signal_distinguishes_states(tmp_path: Path) -> None:
    """The old bug collapsed "no stream", "stream with a stuck/absent
    client", and "stream with a real client" into one int. The caller must
    now be able to tell them apart."""
    from ciao.web.connection_tracker import ConnectionTracker

    tracker = ConnectionTracker()
    conn_id = tracker.register(_fake_ws("10.0.0.5"), "chat", chat_id="chat-1")

    # No active stream, empty chat_id: the plain zero case.
    plane = _file_surface_plane(tmp_path, stream=None, connection_tracker=tracker)
    assert plane._file_surface_signal("") == (0, "none")

    # No active stream, but a real client socket for this chat.
    assert plane._file_surface_signal("chat-1") == (1, "none")

    # Active stream, but the tracker sees nobody for THIS chat_id: the
    # orphaned-subscriber case that used to read as "viewers: 0" no matter
    # which chat the real client was stuck watching.
    plane = _file_surface_plane(tmp_path, stream=object(), connection_tracker=tracker)
    assert plane._file_surface_signal("chat-2") == (0, "active")

    # Active stream AND a real client for this chat: the healthy case.
    assert plane._file_surface_signal("chat-1") == (1, "active")

    tracker.unregister(conn_id)
    assert plane._file_surface_signal("chat-1") == (0, "active")


def test_file_surface_signal_without_connection_tracker(tmp_path: Path) -> None:
    """A lifecycle path that never wired a tracker must degrade to 0
    viewers, not raise."""
    plane = _file_surface_plane(tmp_path, stream=object(), connection_tracker=None)
    assert plane._file_surface_signal("chat-1") == (0, "active")


def test_file_surface_returns_honest_signal_fields(tmp_path: Path) -> None:
    from ciao.web.connection_tracker import ConnectionTracker

    tracker = ConnectionTracker()
    tracker.register(_fake_ws("10.0.0.5"), "chat", chat_id="chat-1")
    plane = _file_surface_plane(tmp_path, stream=object(), connection_tracker=tracker)
    (plane.config.workspace_root / "note.md").write_text("hi", encoding="utf-8")

    principal = McpPrincipal(
        token_id="t",
        chat_id="chat-1",
        project_id="p",
        workspace="personal",
        provider="opencode",
    )
    result = plane.file_surface(principal, "note.md")
    assert result["ok"] is True
    assert result["data"] == {"path": "note.md", "viewers": 1, "stream_state": "active"}


def test_forged_role_claim_is_normalised_to_chat() -> None:
    """A token claim cannot smuggle in a privileged-looking role.

    Claims are the one input to `McpPrincipal` that does not come from our own
    call sites, so an unrecognised role must be dropped rather than carried
    around as something later code might branch on. `handoff` is the concrete
    regression: a gate once tested for exactly that value.
    """
    principal = McpPrincipal.from_claims(
        {
            "token_id": "t",
            "chat_id": "chat-1",
            "project_id": "p",
            "workspace": "personal",
            "provider": "claude",
            "role": "handoff",
        }
    )

    assert principal.role == "chat"
    assert principal.to_claims()["role"] == "chat"


def test_issued_principals_are_always_the_chat_role(tmp_path: Path) -> None:
    """`issue()` has no role knob, so every principal it mints is `chat`.

    Guards the invariant the type annotation now states: adding a restricted
    role has to change this issuing path, instead of only adding a check that
    silently never fires.
    """
    registry = McpSessionRegistry(ttl_seconds=300)

    _token, principal = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )

    assert principal.role == "chat"


def test_revoke_clears_the_reuse_key_so_the_next_issue_mints_a_fresh_token(
    tmp_path: Path,
) -> None:
    """Revoking must drop the `(chat_id, provider)` reuse entry, not leak it.

    The reuse key used to be a 3-tuple including the role. If a cleanup path
    still popped a differently-shaped key, the entry would survive revocation
    and hand a revoked token back to the next caller.
    """
    registry = McpSessionRegistry(ttl_seconds=300)
    token, _ = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )

    assert registry.revoke(token) is True

    reissued, _ = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )
    assert reissued != token

    # revoke_chat() pops the same key shape; it must clear it too.
    assert registry.revoke_chat("chat-1") == 1
    again, _ = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )
    assert again != reissued


def test_same_chat_and_provider_still_reuses_one_token(tmp_path: Path) -> None:
    """Dropping role from the key must not break token reuse."""
    registry = McpSessionRegistry(ttl_seconds=300)

    first, _ = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )
    second, _ = registry.issue(
        chat_id="chat-1", project_id="p", workspace="personal", provider="claude"
    )

    assert first == second
