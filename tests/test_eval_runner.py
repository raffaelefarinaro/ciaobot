from __future__ import annotations

import json
from typing import Any

import pytest

from ciao.eval_runner import (
    ChatRunSpec,
    IsolatedChatServer,
    run_chat_turn,
    token_count,
)


def _spec(*, provider: str = "claude") -> ChatRunSpec:
    return ChatRunSpec(
        scenario="memory_add",
        prompt="Remember this",
        provider=provider,  # type: ignore[arg-type]
        model="claude-sonnet-4-5" if provider == "claude" else "gpt-5.4",
        surface="mcp",
        turn_timeout_s=3,
    )


def _server(tmp_path: Any, *, provider: str = "claude") -> IsolatedChatServer:
    return IsolatedChatServer(
        root=tmp_path,
        surface="mcp",
        provider=provider,  # type: ignore[arg-type]
        workspace_name="work",
        startup_timeout=3,
    )


def test_chat_run_sends_explicit_provider_model_and_surface(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    requests: list[tuple[str, dict[str, Any] | None]] = []

    def request(
        _base_url: str,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        requests.append((path, payload))
        if path == "/api/projects":
            return {"project_id": "project-1", "name": "Eval memory_add"}
        if path == "/api/projects/project-1/chats":
            return {"chat_id": "chat-1", "title": "Eval memory_add"}
        if path == "/api/chats/chat-1/prompt":
            return {}
        raise AssertionError(path)

    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr(server, "start", lambda: setattr(server, "process", object()))
    monkeypatch.setattr(server, "stop", lambda: setattr(server, "process", None))
    monkeypatch.setattr(
        server,
        "wait_for_turn",
        lambda _chat_id, _timeout: [
            {"role": "assistant", "content": "done", "duration_ms": 12}
        ],
    )

    run_chat_turn(server, _spec())

    chat_payload = next(
        payload
        for path, payload in requests
        if path == "/api/projects/project-1/chats"
    )
    assert chat_payload is not None
    assert chat_payload["provider"] == "claude"
    assert chat_payload["model"] == "claude-sonnet-4-5"
    assert chat_payload["control_surface"] == "mcp"


def test_observation_collects_effective_model_usage_tokens_and_tools(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "agent_tool_calls.jsonl").write_text(
        json.dumps({"chat_id": "chat-1", "tool": "Read"}) + "\n"
        + json.dumps({"chat_id": "other", "tool": "Ignored"}) + "\n",
        encoding="utf-8",
    )
    (runtime / "mcp_tool_calls.jsonl").write_text(
        json.dumps({"chat_id": "chat-1", "tool": "memory_add", "status": "ok"})
        + "\n"
        + json.dumps({"chat_id": "chat-1", "tool": "vault_read", "status": "error"})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "start", lambda: setattr(server, "process", object()))
    monkeypatch.setattr(server, "stop", lambda: setattr(server, "process", None))
    monkeypatch.setattr(
        server,
        "prepare_chat",
        lambda _spec: type(
            "Prepared",
            (),
            {
                "project_id": "project-1",
                "project_name": "Eval memory_add",
                "chat_id": "chat-1",
                "chat_title": "Eval memory_add",
            },
        )(),
    )
    monkeypatch.setattr(server, "send", lambda _chat_id, _prompt: None)
    monkeypatch.setattr(
        server,
        "wait_for_turn",
        lambda _chat_id, _timeout: [
            {
                "role": "assistant",
                "content": "done",
                "duration_ms": 34,
                "effective_model": "claude-sonnet-4-5-20250929",
                "usage": {
                    "input_tokens": "10",
                    "cache_creation_input_tokens": "20",
                    "cache_read_input_tokens": "30",
                    "output_tokens": "5",
                },
            }
        ],
    )

    observation = run_chat_turn(server, _spec())

    assert observation.selected_model == "claude-sonnet-4-5"
    assert observation.effective_model == "claude-sonnet-4-5-20250929"
    assert observation.provider_duration_ms == 34
    assert observation.usage["cache_read_input_tokens"] == "30"
    assert observation.tokens == 65
    assert observation.provider_tools == ("Read",)
    assert observation.mcp_tools == ("memory_add", "vault_read")
    assert observation.mcp_errors == 1


def test_claude_and_codex_token_totals_keep_existing_cache_semantics() -> None:
    assert token_count(
        "claude",
        {
            "input_tokens": "10",
            "cache_creation_input_tokens": "20",
            "cache_read_input_tokens": "30",
            "output_tokens": "5",
        },
    ) == 65
    assert token_count(
        "codex",
        {"input_tokens": "100", "cached_input_tokens": "80", "output_tokens": "5"},
    ) == 105


def test_isolated_server_is_closed_after_timeout(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    server.process = object()  # type: ignore[assignment]
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True
        server.process = None

    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(
        server,
        "prepare_chat",
        lambda _spec: type("Prepared", (), {"chat_id": "chat-1"})(),
    )
    monkeypatch.setattr(server, "send", lambda _chat_id, _prompt: None)
    monkeypatch.setattr(
        server,
        "wait_for_turn",
        lambda _chat_id, _timeout: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    observation = run_chat_turn(server, _spec())

    assert "timed out" in observation.error
    assert stopped is True


def test_isolated_server_is_closed_after_provider_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    server.process = object()  # type: ignore[assignment]
    stopped = False

    def stop() -> None:
        nonlocal stopped
        stopped = True
        server.process = None

    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(
        server,
        "prepare_chat",
        lambda _spec: type("Prepared", (), {"chat_id": "chat-1"})(),
    )
    monkeypatch.setattr(
        server,
        "send",
        lambda _chat_id, _prompt: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )

    observation = run_chat_turn(server, _spec())

    assert observation.error == "provider failed"
    assert stopped is True
