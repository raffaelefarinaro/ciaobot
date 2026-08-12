from __future__ import annotations

import json
from typing import Any

import pytest

from ciao.eval_runner import (
    ChatRunSpec,
    IsolatedChatServer,
    PreparedChat,
    run_chat_turn,
    token_count,
)


class _FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


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
    monkeypatch.setattr(
        server, "start", lambda: setattr(server, "process", _FakeProcess())
    )
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
    monkeypatch.setattr(
        server, "start", lambda: setattr(server, "process", _FakeProcess())
    )
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

    def append_turn_telemetry(_chat_id, _timeout):
        # Append during wait_for_turn to mimic the real telemetry writers;
        # the offset captured before send() must include these new rows,
        # while pre-existing rows from earlier turns must not.
        with (runtime / "agent_tool_calls.jsonl").open("a", encoding="utf-8") as h:
            h.write(
                json.dumps({"chat_id": "chat-1", "tool": "Read"}) + "\n"
                + json.dumps({"chat_id": "other", "tool": "Ignored"}) + "\n"
            )
        with (runtime / "mcp_tool_calls.jsonl").open("a", encoding="utf-8") as h:
            h.write(
                json.dumps({"chat_id": "chat-1", "tool": "memory_add", "status": "ok"})
                + "\n"
                + json.dumps(
                    {"chat_id": "chat-1", "tool": "vault_read", "status": "error"}
                )
                + "\n"
            )
        return [
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
        ]

    monkeypatch.setattr(server, "wait_for_turn", append_turn_telemetry)

    observation = run_chat_turn(server, _spec())

    assert observation.selected_model == "claude-sonnet-4-5"
    assert observation.effective_model == "claude-sonnet-4-5-20250929"
    assert observation.provider_duration_ms == 34
    assert observation.usage["cache_read_input_tokens"] == "30"
    assert observation.tokens == 65
    assert observation.provider_tools == ("Read",)
    assert observation.mcp_tools == ("memory_add", "vault_read")
    assert observation.mcp_errors == 1


def test_observation_excludes_telemetry_from_prior_turns_on_same_chat(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-turn telemetry must skip rows written before send() in the same chat."""

    server = _server(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    # Pre-existing rows from an earlier turn on the same chat must NOT leak in.
    (runtime / "agent_tool_calls.jsonl").write_text(
        json.dumps({"chat_id": "chat-1", "tool": "PriorRead"}) + "\n",
        encoding="utf-8",
    )
    (runtime / "mcp_tool_calls.jsonl").write_text(
        json.dumps({"chat_id": "chat-1", "tool": "prior_tool", "status": "ok"})
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        server, "start", lambda: setattr(server, "process", _FakeProcess())
    )
    monkeypatch.setattr(server, "stop", lambda: setattr(server, "process", None))
    monkeypatch.setattr(
        server,
        "prepare_chat",
        lambda _spec: type(
            "Prepared",
            (),
            {
                "project_id": "project-1",
                "project_name": "Eval",
                "chat_id": "chat-1",
                "chat_title": "Eval",
            },
        )(),
    )
    monkeypatch.setattr(server, "send", lambda _chat_id, _prompt: None)

    def append_turn_telemetry(_chat_id, _timeout):
        with (runtime / "agent_tool_calls.jsonl").open("a", encoding="utf-8") as h:
            h.write(json.dumps({"chat_id": "chat-1", "tool": "CurrentRead"}) + "\n")
        with (runtime / "mcp_tool_calls.jsonl").open("a", encoding="utf-8") as h:
            h.write(
                json.dumps({"chat_id": "chat-1", "tool": "current_tool", "status": "ok"})
                + "\n"
            )
        return [{"role": "assistant", "content": "done", "duration_ms": 1}]

    monkeypatch.setattr(server, "wait_for_turn", append_turn_telemetry)

    observation = run_chat_turn(server, _spec())

    assert observation.provider_tools == ("CurrentRead",)
    assert observation.mcp_tools == ("current_tool",)
    assert observation.mcp_errors == 0


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
    server.process = _FakeProcess()  # type: ignore[assignment]
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
    server.process = _FakeProcess()  # type: ignore[assignment]
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


def test_startup_failure_closes_process_and_log(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExitedProcess:
        returncode = 17

        def poll(self) -> int:
            return self.returncode

    server = _server(tmp_path)
    monkeypatch.setattr("ciao.eval_runner._free_port", lambda: 9876)
    monkeypatch.setattr("ciao.eval_runner._copy_packaged_assets", lambda _root: None)
    monkeypatch.setattr(
        "ciao.eval_runner.subprocess.Popen",
        lambda *_args, **_kwargs: ExitedProcess(),
    )

    with pytest.raises(RuntimeError, match="server exited with 17"):
        server.start()

    assert server.process is None
    assert server._log_handle is None


def test_startup_timeout_terminates_child_and_closes_log(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LiveProcess:
        pid = 42
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            self.returncode = 0
            return 0

    server = _server(tmp_path)
    server.startup_timeout = 0.5
    monotonic = iter([0.0, 1.0])
    monkeypatch.setattr("ciao.eval_runner._free_port", lambda: 9876)
    monkeypatch.setattr("ciao.eval_runner._copy_packaged_assets", lambda _root: None)
    monkeypatch.setattr(
        "ciao.eval_runner.subprocess.Popen",
        lambda *_args, **_kwargs: LiveProcess(),
    )
    monkeypatch.setattr("ciao.eval_runner.time.monotonic", lambda: next(monotonic))
    monkeypatch.setattr("ciao.eval_runner.os.killpg", lambda _pid, _signal: None)

    with pytest.raises(RuntimeError, match="Timed out starting"):
        server.start()

    assert server.process is None
    assert server._log_handle is None


def test_terminated_process_is_not_running_and_direct_run_restarts_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self, returncode: int | None) -> None:
            self.returncode = returncode

        def poll(self) -> int | None:
            return self.returncode

    server = _server(tmp_path)
    server.process = Process(9)  # type: ignore[assignment]
    starts = 0

    def start() -> None:
        nonlocal starts
        starts += 1
        server.process = Process(None)  # type: ignore[assignment]

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "stop", lambda: setattr(server, "process", None))
    monkeypatch.setattr(
        server,
        "prepare_chat",
        lambda _spec: PreparedChat("project", "Eval", "chat", "Eval", 0),
    )
    monkeypatch.setattr(server, "send", lambda _chat_id, _prompt: None)
    monkeypatch.setattr(
        server,
        "wait_for_turn",
        lambda _chat_id, _timeout: [
            {"role": "assistant", "content": "done", "duration_ms": 1}
        ],
    )

    assert server.is_running is False

    observation = run_chat_turn(server, _spec())

    assert starts == 1
    assert observation.final_text == "done"


def test_start_closes_stale_log_before_restarting_dead_process(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale_log = (tmp_path / "stale.log").open("ab")
    replacement = _FakeProcess()
    server = _server(tmp_path)
    server.process = _FakeProcess(9)  # type: ignore[assignment]
    server._log_handle = stale_log
    monkeypatch.setattr("ciao.eval_runner._free_port", lambda: 9876)
    monkeypatch.setattr("ciao.eval_runner._copy_packaged_assets", lambda _root: None)
    monkeypatch.setattr(
        "ciao.eval_runner.subprocess.Popen",
        lambda *_args, **_kwargs: replacement,
    )
    monkeypatch.setattr(
        "ciao.eval_runner._json_request",
        lambda *_args, **_kwargs: {"enabled": True, "bound": True},
    )

    server.start()

    assert stale_log.closed is True
    assert server.process is replacement
    replacement.returncode = 0
    server.stop()


def test_wait_for_turn_observes_fast_terminal_response_without_active_poll(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    server.process = _FakeProcess()  # type: ignore[assignment]
    calls: list[str] = []

    def request(
        _base_url: str,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Any:
        calls.append(path)
        if path == "/api/active-chats":
            return {"active_chat_ids": []}
        if path == "/api/chats/chat/messages":
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "done", "duration_ms": 4},
            ]
        raise AssertionError(path)

    monotonic = iter([0.0, 0.1])
    monkeypatch.setattr("ciao.eval_runner._json_request", request)
    monkeypatch.setattr("ciao.eval_runner.time.monotonic", lambda: next(monotonic))

    messages = server.wait_for_turn("chat", 3)

    assert messages[-1]["content"] == "done"
    assert calls == ["/api/active-chats", "/api/chats/chat/messages"]


def test_direct_run_elapsed_time_starts_at_prompt_dispatch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LiveProcess:
        def poll(self) -> None:
            return None

    server = _server(tmp_path)
    clock = 0.0
    prepared_chat = PreparedChat("project", "Eval", "chat", "Eval", 99.0)

    def start() -> None:
        nonlocal clock
        clock = 50.0
        server.process = LiveProcess()  # type: ignore[assignment]

    def prepare_chat(_spec: ChatRunSpec) -> PreparedChat:
        nonlocal clock
        clock = 100.0
        return prepared_chat

    def send(_chat_id: str, _prompt: str) -> None:
        nonlocal clock
        clock = 100.1

    def wait(_chat_id: str, _timeout: float) -> list[dict[str, Any]]:
        nonlocal clock
        clock = 100.4
        return [{"role": "assistant", "content": "done", "duration_ms": 2}]

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "stop", lambda: setattr(server, "process", None))
    monkeypatch.setattr(server, "prepare_chat", prepare_chat)
    monkeypatch.setattr(server, "send", send)
    monkeypatch.setattr(server, "wait_for_turn", wait)
    monkeypatch.setattr("ciao.eval_runner.time.perf_counter", lambda: clock)

    observation = run_chat_turn(
        server,
        _spec(),
    )

    assert observation.elapsed_ms == 400


def test_prepared_run_elapsed_time_includes_time_since_chat_preparation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    server.process = _FakeProcess()  # type: ignore[assignment]
    clock = 100.0
    prepared_chat = PreparedChat("project", "Eval", "chat", "Eval", 99.0)

    def send(_chat_id: str, _prompt: str) -> None:
        nonlocal clock
        clock = 100.1

    def wait(_chat_id: str, _timeout: float) -> list[dict[str, Any]]:
        nonlocal clock
        clock = 100.4
        return [{"role": "assistant", "content": "done", "duration_ms": 2}]

    monkeypatch.setattr(server, "send", send)
    monkeypatch.setattr(server, "wait_for_turn", wait)
    monkeypatch.setattr("ciao.eval_runner.time.perf_counter", lambda: clock)

    observation = run_chat_turn(server, _spec(), prepared_chat=prepared_chat)

    # Prepared benchmark timing intentionally includes fixture time after prepare_chat().
    assert observation.elapsed_ms == 1400
