from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ImageAttachment,
    PermissionRequestEvent,
    ResultEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.providers.codex import (
    CodexProvider,
    CodexSettings,
    _PROTOCOL_CACHE,
    _REQUIRED_PROTOCOL_TOKENS,
    codex_collab_agents,
    codex_collab_tree_counts,
    codex_protocol_status,
    codex_running_subagents,
)


def test_codex_managed_process_receives_scoped_mcp_configuration(
    tmp_path: Path, monkeypatch,
) -> None:
    binary = tmp_path / "codex"
    binary.touch()
    monkeypatch.setattr("ciao.providers.codex.resolve_codex_binary", lambda: str(binary))
    provider = CodexProvider(tmp_path)
    request = AgentRequest(
        prompt="test",
        model="gpt-test",
        mode="auto",
        provider="codex",
        control_surface="mcp",
        mcp_url="http://127.0.0.1:8443/mcp/",
        mcp_token="secret-session-token",
        mcp_required=True,
    )

    command = provider._resolved_command(request)

    rendered = " ".join(command)
    assert command[-2:] == ["app-server", "--stdio"]
    assert "mcp_servers.ciaobot.url" in rendered
    assert "bearer_token_env_var" in rendered
    assert "mcp_servers.ciaobot.required=true" in rendered
    assert "shell_environment_policy.exclude" in rendered
    assert "secret-session-token" not in rendered


def test_codex_entity_context_uses_the_registry_selected_legacy_owner(
    tmp_path: Path,
) -> None:
    (tmp_path / "INDEX.md").write_text(
        "- `People/Alba` (aliases: Alba)\n",
        encoding="utf-8",
    )
    config = SimpleNamespace(
        vault_root=tmp_path,
        legacy_entity_workspace=lambda: "wrong-fallback",
    )
    provider = CodexProvider(tmp_path, config=config)

    hidden = provider._runtime_context(
        AgentRequest(
            prompt="Ask Alba",
            model="gpt-test",
            mode="auto",
            provider="codex",
            extra_env={
                "CIAO_ACTIVE_WORKSPACE": "personal",
                "CIAO_LEGACY_ENTITY_WORKSPACE": "research",
            },
        )
    )
    visible = provider._runtime_context(
        AgentRequest(
            prompt="Ask Alba",
            model="gpt-test",
            mode="auto",
            provider="codex",
            extra_env={
                "CIAO_ACTIVE_WORKSPACE": "research",
                "CIAO_LEGACY_ENTITY_WORKSPACE": "research",
            },
        )
    )

    assert "[[People/Alba]]" not in hidden
    assert "[[People/Alba]]" in visible


def test_codex_injects_memory_when_workspace_guides_diverge(tmp_path: Path) -> None:
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Claude guide\n", encoding="utf-8")
    ensure_regions(guide)
    write_region(guide, "memory", ["remember this workspace fact"])
    (tmp_path / "AGENTS.md").write_text("# Custom Codex guide\n", encoding="utf-8")

    provider = CodexProvider(tmp_path)
    instructions = provider._memory_instructions(
        AgentRequest(prompt="test", model="gpt-test", mode="normal", provider="codex")
    )

    assert "remember this workspace fact" in instructions


def test_codex_does_not_duplicate_memory_for_linked_workspace_guides(
    tmp_path: Path,
) -> None:
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Claude guide\n", encoding="utf-8")
    ensure_regions(guide)
    write_region(guide, "memory", ["native guide owns this fact"])
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")

    provider = CodexProvider(tmp_path)
    instructions = provider._memory_instructions(
        AgentRequest(prompt="test", model="gpt-test", mode="normal", provider="codex")
    )

    assert "native guide owns this fact" not in instructions


FAKE_APP_SERVER = r'''#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ.get("FAKE_CODEX_LOG", "")
turn_id = "turn-1"

def send(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)

def record(kind, payload):
    if not log_path:
        return
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"kind": kind, "payload": payload}) + "\n")

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        pass
    elif method in {"thread/start", "thread/resume", "thread/fork"}:
        record(method, params)
        send({"id": request_id, "result": {
            "thread": {"id": "thread-forked" if method == "thread/fork" else (params.get("threadId") or "thread-1"), "turns": []},
            "model": params.get("model") or "gpt-test",
            "approvalPolicy": params.get("approvalPolicy") or "on-request",
            "approvalsReviewer": params.get("approvalsReviewer") or "user",
            "cwd": params.get("cwd") or os.getcwd(),
            "modelProvider": "openai",
            "sandbox": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
        }})
    elif method == "account/rateLimits/read":
        send({"id": request_id, "result": {"rateLimits": {
            "primary": {"usedPercent": 12.5, "resetsAt": 1234, "windowDurationMins": 300},
            "limitId": "codex", "planType": "plus",
        }}})
    elif method == "model/list":
        send({"id": request_id, "result": {"data": [{
            "id": "gpt-test", "model": "gpt-test", "displayName": "GPT Test",
            "description": "fake", "hidden": False, "isDefault": True,
            "defaultReasoningEffort": "medium",
            "supportedReasoningEfforts": [
                {"reasoningEffort": "low", "description": "Low"},
                {"reasoningEffort": "high", "description": "High"},
            ],
            "inputModalities": ["text", "image"],
        }], "nextCursor": None}})
    elif method == "thread/read":
        send({"id": request_id, "result": {"thread": {
            "id": params.get("threadId"),
            "turns": [{"id": "turn-history", "status": "completed", "items": [
                {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "hello"}]},
                {"type": "agentMessage", "id": "a1", "text": "world"},
            ]}],
        }}})
    elif method in {"thread/archive", "thread/delete"}:
        record(method, params)
        send({"id": request_id, "result": {}})
    elif method == "turn/start":
        record("turn/start", params)
        send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        if os.environ.get("FAKE_CODEX_COMMENTARY"):
            send({"method": "item/started", "params": {"item": {
                "type": "agentMessage", "id": "note-1", "text": "", "phase": "commentary",
            }}})
            send({"method": "item/agentMessage/delta", "params": {
                "itemId": "note-1", "delta": "I'll check that now.",
            }})
            send({"method": "item/completed", "params": {"item": {
                "type": "agentMessage", "id": "note-1", "text": "I'll check that now.", "phase": "commentary",
            }}})
        if os.environ.get("FAKE_CODEX_COMMENTARY_ONLY"):
            send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
                "id": turn_id, "status": "completed", "items": [],
            }}})
            continue
        send({"method": "item/reasoning/summaryTextDelta", "params": {"delta": "checking"}})
        send({"method": "item/started", "params": {"item": {
            "type": "commandExecution", "id": "cmd-1", "command": "pwd",
        }}})
        send({"id": "question-rpc", "method": "item/tool/requestUserInput", "params": {
            "threadId": "thread-1", "turnId": turn_id, "itemId": "ask-1",
            "questions": [{
                "id": "choice", "header": "Choice", "question": "Pick one",
                "isOther": True, "isSecret": False,
                "options": [{"label": "A", "description": "first"}],
            }],
        }})
    elif request_id == "question-rpc" and "result" in message:
        record("question-response", message.get("result"))
        send({"id": "permission-rpc", "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "thread-1", "turnId": turn_id, "itemId": "cmd-2",
            "command": "touch safe.txt", "reason": "write a test file",
        }})
    elif request_id == "permission-rpc" and "result" in message:
        record("permission-response", message.get("result"))
        send({"method": "item/started", "params": {"item": {
            "type": "agentMessage", "id": "answer-1", "text": "", "phase": "final_answer",
        }}})
        send({"method": "item/agentMessage/delta", "params": {
            "itemId": "answer-1", "delta": "done",
        }})
        send({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "answer-1", "text": "done", "phase": "final_answer",
        }}})
        send({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {
            "last": {"inputTokens": 10, "outputTokens": 4, "cachedInputTokens": 2,
                     "reasoningOutputTokens": 1, "totalTokens": 14},
            "total": {"totalTokens": 14}, "modelContextWindow": 1000,
        }}})
        send({"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "primary": {"usedPercent": 20, "resetsAt": 2222},
            "limitId": "codex", "planType": "plus",
        }}})
        send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": turn_id, "status": "completed", "items": [],
        }}})
    elif method == "turn/steer":
        record("turn/steer", params)
        send({"id": request_id, "result": {"turnId": turn_id}})
    elif method == "turn/interrupt":
        record("turn/interrupt", params)
        send({"id": request_id, "result": {}})
        send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": turn_id, "status": "interrupted", "items": [],
        }}})
'''


def _fake_command(tmp_path: Path) -> tuple[list[str], Path]:
    script = tmp_path / "fake_codex_app_server.py"
    script.write_text(FAKE_APP_SERVER, encoding="utf-8")
    log = tmp_path / "fake_codex.jsonl"
    return [sys.executable, str(script)], log


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_codex_collab_state_tracks_child_lifecycle_not_spawn_status() -> None:
    thread = {"turns": [{"items": [
        {
            "type": "collabAgentToolCall",
            "id": "spawn-1",
            "tool": "spawnAgent",
            "status": "completed",
            "receiverThreadIds": ["child-1"],
            "agentsStates": {"child-1": {"status": "running"}},
            "prompt": "Research",
        },
        {
            "type": "collabAgentToolCall",
            "id": "wait-1",
            "tool": "wait",
            "status": "completed",
            "receiverThreadIds": ["child-1"],
            "agentsStates": {"child-1": {"status": "completed"}},
        },
    ]}]}

    agents = codex_collab_agents(thread)
    assert agents["child-1"]["status"] == "completed"
    assert agents["child-1"]["description"] == "Research"
    assert codex_running_subagents(thread) == (0, True)


def test_codex_protocol_status_requires_complete_schema(
    tmp_path: Path, monkeypatch,
) -> None:
    binary = tmp_path / "codex"
    binary.touch()
    _PROTOCOL_CACHE.clear()

    def compatible_run(command, **_kwargs):
        out = Path(command[command.index("--out") + 1])
        (out / "protocol.json").write_text(
            json.dumps(sorted(_REQUIRED_PROTOCOL_TOKENS)), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", compatible_run)
    assert codex_protocol_status(str(binary)) == (
        True,
        "app-server protocol compatible",
    )

    _PROTOCOL_CACHE.clear()

    def incompatible_run(command, **_kwargs):
        out = Path(command[command.index("--out") + 1])
        (out / "protocol.json").write_text('"thread/start"', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", incompatible_run)
    ok, detail = codex_protocol_status(str(binary))
    assert ok is False
    assert "thread/resume" in detail


@pytest.mark.asyncio
async def test_codex_provider_streams_native_protocol_and_answers_gates(
    tmp_path: Path,
) -> None:
    command, log = _fake_command(tmp_path)
    image = tmp_path / "photo.png"
    image.write_bytes(b"not-decoded-by-app-server")
    provider = CodexProvider(tmp_path, command=command)
    request = AgentRequest(
        prompt="Inspect this",
        model="gpt-test",
        mode="auto",
        provider="codex",
        images=[ImageAttachment(
            path=image,
            mime_type="image/png",
            original_filename="photo.png",
        )],
        extra_env={"FAKE_CODEX_LOG": str(log)},
        thinking_level="high",
    )
    handles = []
    events = []
    async for event in provider.run_streaming(request, handles.append):
        events.append(event)
        if isinstance(event, ToolUseEvent) and event.request_id:
            assert provider.send_question_response(
                event.request_id, {"choice": ["A"]}
            )
        if isinstance(event, PermissionRequestEvent):
            assert provider.send_permission_response(event.request_id, True)

    assert any(isinstance(event, ThinkingEvent) for event in events)
    assert any(
        isinstance(event, ToolUseEvent) and event.tool_name == "Bash"
        for event in events
    )
    assert any(
        isinstance(event, ToolUseEvent)
        and event.tool_name == "AskUserQuestion"
        for event in events
    )
    assert any(isinstance(event, TokenUsageEvent) for event in events)
    text_delta = next(event for event in events if isinstance(event, AssistantTextDelta))
    assert text_delta.phase == "final_answer"
    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.result == "done"
    assert result.session_id == "thread-1"
    assert result.effective_model == "gpt-test"
    assert result.usage["input_tokens"] == "10"
    assert result.usage["context_pct"] == "1.4%"
    assert result.quota["planType"] == "plus"
    assert handles[0] is not None and handles[-1] is None

    records = _read_log(log)
    thread_start = next(row["payload"] for row in records if row["kind"] == "thread/start")
    assert thread_start["sandbox"] == "workspace-write"
    assert thread_start["ephemeral"] is False
    assert thread_start["approvalsReviewer"] == "auto_review"
    turn_start = next(row["payload"] for row in records if row["kind"] == "turn/start")
    assert turn_start["effort"] == "high"
    assert [item["type"] for item in turn_start["input"]] == ["text", "localImage"]
    assert turn_start["input"][1]["path"] == str(image)
    question = next(row["payload"] for row in records if row["kind"] == "question-response")
    assert question == {"answers": {"choice": {"answers": ["A"]}}}
    permission = next(row["payload"] for row in records if row["kind"] == "permission-response")
    assert permission == {"decision": "accept"}
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_provider_excludes_commentary_from_final_result(
    tmp_path: Path,
) -> None:
    command, log = _fake_command(tmp_path)
    provider = CodexProvider(tmp_path, command=command)
    request = AgentRequest(
        prompt="Inspect this",
        model="gpt-test",
        mode="auto",
        provider="codex",
        extra_env={
            "FAKE_CODEX_LOG": str(log),
            "FAKE_CODEX_COMMENTARY": "1",
        },
    )
    events = []
    async for event in provider.run_streaming(request, lambda _handle: None):
        events.append(event)
        if isinstance(event, ToolUseEvent) and event.request_id:
            provider.send_question_response(event.request_id, {"choice": ["A"]})
        elif isinstance(event, PermissionRequestEvent):
            provider.send_permission_response(event.request_id, True)

    deltas = [
        event for event in events if isinstance(event, AssistantTextDelta)
    ]
    assert [(event.text, event.phase) for event in deltas] == [
        ("I'll check that now.", "commentary"),
        ("done", "final_answer"),
    ]
    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.result == "done"
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_provider_promotes_commentary_only_completion(
    tmp_path: Path,
) -> None:
    command, _log = _fake_command(tmp_path)
    provider = CodexProvider(tmp_path, command=command)
    request = AgentRequest(
        prompt="Inspect this",
        model="gpt-test",
        mode="auto",
        provider="codex",
        extra_env={
            "FAKE_CODEX_COMMENTARY": "1",
            "FAKE_CODEX_COMMENTARY_ONLY": "1",
        },
    )

    events = [
        event
        async for event in provider.run_streaming(request, lambda _handle: None)
    ]

    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.result == "I'll check that now."
    assert result.fallback_final is True
    await provider.disconnect()


FAKE_APP_SERVER_CONNECT_ERROR = r'''#!/usr/bin/env python3
import json
import sys

turn_id = "turn-1"

def send(payload):
    print(json.dumps(payload, separators=(",", ":")), flush=True)

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake-codex"}})
    elif method == "initialized":
        pass
    elif method in {"thread/start", "thread/resume", "thread/fork"}:
        send({"id": request_id, "result": {
            "thread": {"id": "thread-1", "turns": []},
            "model": "gpt-test", "modelProvider": "openai",
        }})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": turn_id, "status": "inProgress", "items": []}}})
        send({"method": "error", "params": {"willRetry": False, "error": {
            "message": "API Error: Unable to connect to API (ENOTFOUND)",
        }}})
        send({"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {
            "id": turn_id, "status": "failed", "items": [],
        }}})
'''


@pytest.mark.asyncio
async def test_codex_provider_error_result_names_host_and_category(tmp_path: Path) -> None:
    """A connection-error result from the Codex app-server is annotated with
    the failing endpoint (OPENAI_BASE_URL host) and a DNS category, so a failed
    codex schedule names what failed and how (#178)."""
    script = tmp_path / "fake_codex_connect_error.py"
    script.write_text(FAKE_APP_SERVER_CONNECT_ERROR, encoding="utf-8")
    provider = CodexProvider(tmp_path, command=[sys.executable, str(script)])
    request = AgentRequest(
        prompt="do the thing",
        model="gpt-test",
        mode="auto",
        provider="codex",
        extra_env={"OPENAI_BASE_URL": "https://api.openai.com/v1"},
    )
    events = []
    async for event in provider.run_streaming(request, lambda _handle: None):
        events.append(event)

    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.is_error is True
    assert "host: api.openai.com" in result.result
    assert "category: dns" in result.result
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_provider_non_connection_error_passes_through(tmp_path: Path) -> None:
    """A non-connection error from Codex is not annotated (#178)."""
    script = tmp_path / "fake_codex_auth_error.py"
    script.write_text(
        FAKE_APP_SERVER_CONNECT_ERROR.replace(
            "Unable to connect to API (ENOTFOUND)",
            "Model refused the request: policy violation",
        ),
        encoding="utf-8",
    )
    provider = CodexProvider(tmp_path, command=[sys.executable, str(script)])
    request = AgentRequest(
        prompt="do the thing",
        model="gpt-test",
        mode="auto",
        provider="codex",
        extra_env={"OPENAI_BASE_URL": "https://api.openai.com/v1"},
    )
    events = []
    async for event in provider.run_streaming(request, lambda _handle: None):
        events.append(event)

    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.is_error is True
    assert "host:" not in result.result
    assert "category:" not in result.result
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_provider_discovers_models_and_reads_thread(tmp_path: Path) -> None:
    command, _log = _fake_command(tmp_path)
    catalog = await CodexProvider.model_catalog(
        tmp_path, command=command, force=True
    )
    assert catalog[0]["model"] == "gpt-test"
    assert catalog[0]["supportedReasoningEfforts"][1]["reasoningEffort"] == "high"

    thread = await CodexProvider.read_thread(
        tmp_path, "thread-history", command=command
    )
    assert thread is not None
    assert thread["id"] == "thread-history"
    assert thread["turns"][0]["items"][1]["text"] == "world"


@pytest.mark.asyncio
async def test_codex_provider_archives_and_deletes_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, log = _fake_command(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    assert await CodexProvider.archive_thread(
        tmp_path, "thread-archive-me", command=command
    )
    assert await CodexProvider.delete_thread(
        tmp_path, "thread-delete-me", command=command
    )
    assert await CodexProvider.delete_thread(tmp_path, "", command=command) is False

    records = _read_log(log)
    assert {"kind": "thread/archive", "payload": {"threadId": "thread-archive-me"}} in records
    assert {"kind": "thread/delete", "payload": {"threadId": "thread-delete-me"}} in records


@pytest.mark.asyncio
async def test_codex_provider_forks_resumed_thread(tmp_path: Path) -> None:
    command, log = _fake_command(tmp_path)
    provider = CodexProvider(tmp_path, command=command)
    request = AgentRequest(
        prompt="Branch",
        model="gpt-test",
        mode="normal",
        provider="codex",
        resume_session="thread-parent",
        fork_session=True,
        extra_env={"FAKE_CODEX_LOG": str(log)},
    )

    thread_id = await provider._ensure_thread(request)

    assert thread_id == "thread-forked"
    records = _read_log(log)
    fork = next(row for row in records if row["kind"] == "thread/fork")
    assert fork["payload"]["threadId"] == "thread-parent"
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_resume_fallback_replays_stable_context(tmp_path: Path) -> None:
    from ciao.providers.stdio_rpc import RpcError

    provider = CodexProvider(tmp_path)
    calls: list[str] = []

    class _Peer:
        async def request(self, method: str, _params: object, **_kwargs: object):
            calls.append(method)
            if method == "thread/resume":
                raise RpcError("thread disappeared")
            if method == "thread/start":
                return {"thread": {"id": "thread-new"}, "model": "gpt-test"}
            if method == "account/rateLimits/read":
                return {"rateLimits": {}}
            raise AssertionError(method)

    async def _peer(_request: AgentRequest):
        return _Peer()

    provider._ensure_peer = _peer  # type: ignore[method-assign]
    request = AgentRequest(
        prompt="continue",
        model="gpt-test",
        mode="normal",
        provider="codex",
        resume_session="thread-old",
        stable_context_prefix="[stable context]\n",
    )

    assert await provider._ensure_thread(request) == "thread-new"
    assert calls[:2] == ["thread/resume", "thread/start"]
    assert request.prompt.startswith("[stable context]\n")


@pytest.mark.asyncio
async def test_codex_tier_resolution_honors_config_pins(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = [
        {"model": "gpt-5.6-terra", "isDefault": True},
        {"model": "gpt-5.6-sol"},
        {"model": "gpt-5.6-luna"},
    ]
    monkeypatch.setattr(
        CodexProvider, "model_catalog", AsyncMock(return_value=catalog)
    )
    command, log = _fake_command(tmp_path)
    provider = CodexProvider(
        tmp_path,
        command=command,
        config=SimpleNamespace(codex=CodexSettings(sonnet_model="gpt-5.6-sol")),
    )
    request = AgentRequest(
        prompt="Hi",
        model="sonnet",
        mode="normal",
        provider="codex",
        extra_env={"FAKE_CODEX_LOG": str(log)},
    )

    await provider._ensure_thread(request)

    records = _read_log(log)
    start = next(row for row in records if row["kind"] == "thread/start")
    assert start["payload"]["model"] == "gpt-5.6-sol"
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_tier_resolution_drops_stale_pin(
    tmp_path: Path, monkeypatch,
) -> None:
    catalog = [
        {"model": "gpt-5.6-terra", "isDefault": True},
        {"model": "gpt-5.6-luna"},
    ]
    monkeypatch.setattr(
        CodexProvider, "model_catalog", AsyncMock(return_value=catalog)
    )
    command, log = _fake_command(tmp_path)
    provider = CodexProvider(
        tmp_path,
        command=command,
        config=SimpleNamespace(codex=CodexSettings(sonnet_model="gpt-4-retired")),
    )
    request = AgentRequest(
        prompt="Hi",
        model="sonnet",
        mode="normal",
        provider="codex",
        extra_env={"FAKE_CODEX_LOG": str(log)},
    )

    await provider._ensure_thread(request)

    records = _read_log(log)
    start = next(row for row in records if row["kind"] == "thread/start")
    assert start["payload"]["model"] == "gpt-5.6-terra"
    await provider.disconnect()


@pytest.mark.asyncio
async def test_codex_collab_tree_uses_receiver_turn_status(
    tmp_path: Path, monkeypatch,
) -> None:
    parent = {"turns": [{"items": [{
        "type": "collabAgentToolCall",
        "id": "spawn-1",
        "tool": "spawnAgent",
        "status": "completed",
        "receiverThreadIds": ["child-1"],
        "agentsStates": {"child-1": {"status": "completed"}},
    }]}]}
    child = {
        "id": "child-1",
        "turns": [{"id": "turn-1", "status": "inProgress", "items": []}],
    }
    monkeypatch.setattr(
        CodexProvider, "read_thread", AsyncMock(return_value=child)
    )

    tree = await CodexProvider.read_collab_tree(tmp_path, parent)

    assert codex_collab_tree_counts(tree) == (1, True)


@pytest.mark.asyncio
async def test_codex_provider_steers_active_turn(tmp_path: Path) -> None:
    command, log = _fake_command(tmp_path)
    provider = CodexProvider(tmp_path, command=command)
    request = AgentRequest(
        prompt="Start",
        model="gpt-test",
        mode="normal",
        provider="codex",
        extra_env={"FAKE_CODEX_LOG": str(log)},
    )
    async for event in provider.run_streaming(request, lambda _handle: None):
        if isinstance(event, ToolUseEvent) and event.request_id:
            steered = await provider.steer(AgentRequest(
                prompt="Add this",
                model="gpt-test",
                mode="normal",
                provider="codex",
            ))
            assert steered
            provider.send_question_response(event.request_id, {"choice": ["A"]})
        elif isinstance(event, PermissionRequestEvent):
            provider.send_permission_response(event.request_id, False)

    records = _read_log(log)
    assert any(row["kind"] == "turn/steer" for row in records)
    await provider.disconnect()


def test_codex_mcps_and_plugins_filters_enabled_json(monkeypatch, tmp_path: Path) -> None:
    from ciao.providers import codex as codex_mod

    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    payload = [
        {"name": "node_repl", "enabled": True},
        {"name": "computer-use", "enabled": False},
        {"name": "figma", "enabled": True},
        {"name": "ciaobot", "enabled": True},
        {"name": "n8n_mcp", "enabled": True},
        {"name": "notion", "enabled": True},
    ]

    class FakeResult:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    def fake_run(cmd, **_kwargs):
        assert cmd[-3:] == ["mcp", "list", "--json"]
        return FakeResult()

    monkeypatch.setattr(codex_mod, "resolve_codex_binary", lambda env=None: str(binary))
    monkeypatch.setattr(codex_mod, "_codex_path_env", lambda _binary: {})
    monkeypatch.setattr("subprocess.run", fake_run)

    assert codex_mod.codex_mcps_and_plugins() == ["figma", "node_repl"]


def test_codex_mcps_and_plugins_falls_back_to_config(monkeypatch, tmp_path: Path) -> None:
    from ciao.providers import codex as codex_mod

    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[mcp_servers.custom_http]\nenabled = true\n'
        '[mcp_servers.legacy]\nenabled = false\n'
        '[mcp_servers.stdio_tool]\ncommand = "npx"\n'
        '[mcp_servers.n8n_mcp]\nenabled = true\n'
        '[mcp_servers.notion]\nenabled = true\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(codex_mod, "resolve_codex_binary", lambda env=None: None)
    monkeypatch.setattr(codex_mod.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)

    # Project MCPs (n8n_mcp, notion) stay excluded even if present in config.
    assert codex_mod.codex_mcps_and_plugins() == ["custom_http", "stdio_tool"]
