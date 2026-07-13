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
    _PROTOCOL_CACHE,
    _REQUIRED_PROTOCOL_TOKENS,
    codex_collab_agents,
    codex_collab_tree_counts,
    codex_protocol_status,
    codex_running_subagents,
)


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
