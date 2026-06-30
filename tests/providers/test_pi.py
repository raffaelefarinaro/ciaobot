"""Tests for PiProvider event mapping and core mechanics."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.models import StreamEvent
from ciao.providers.pi import PiProvider, _convert_pi_event


def test_convert_text_delta() -> None:
    event = {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": "hello",
        },
    }
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert isinstance(result, StreamEvent)
    assert result.type == "assistant_text_delta"
    assert result.text == "hello"


def test_convert_thinking_delta() -> None:
    event = {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "thinking_delta",
            "contentIndex": 0,
            "delta": "planning...",
        },
    }
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result.type == "thinking"
    assert result.text == "planning..."


def test_convert_tool_execution_start() -> None:
    event = {
        "type": "tool_execution_start",
        "toolCallId": "call_abc",
        "toolName": "bash",
        "args": {"command": "ls -la"},
    }
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result.type == "tool_use"
    assert result.tool_name == "bash"
    assert result.tool_input == "command: ls -la"
    assert result.tool_use_id == "call_abc"


def test_convert_agent_end() -> None:
    event = {"type": "agent_end", "messages": []}
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result.type == "result"
    assert result.is_error is False


def test_convert_unknown_event_returns_none() -> None:
    event = {"type": "queue_update", "steering": []}
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result is None


def test_convert_extension_ui_confirm() -> None:
    event = {
        "type": "extension_ui_request",
        "id": "uuid-1",
        "method": "confirm",
        "title": "Allow dangerous command?",
        "message": "This will delete files.",
    }
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result is not None
    assert result.type == "permission_request"
    assert result.message == "Allow dangerous command?: This will delete files."
    assert result.request_id == "uuid-1"


def test_convert_extension_ui_select() -> None:
    event = {
        "type": "extension_ui_request",
        "id": "uuid-2",
        "method": "select",
        "title": "Choose action",
        "options": ["Allow", "Block", "Review"],
    }
    result = _convert_pi_event(event, None)  # type: ignore[arg-type]
    assert result is not None
    assert result.type == "permission_request"
    assert result.message == "Choose action: Allow, Block, Review"


def test_convert_system_status_events() -> None:
    for etype in ("auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end"):
        result = _convert_pi_event({"type": etype}, None)  # type: ignore[arg-type]
        assert result is not None
        assert result.type == "system_status"
        assert result.status == etype


@pytest.fixture
def pi_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PiProvider:
    from ciao.providers.pi import PiSettings
    # Sandbox the session dir under tmp_path so tests don't leak into
    # ``~/.pi/agent/sessions`` (the runtime default).
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))
    return PiProvider(
        tmp_path,
        config=PiSettings(models=("qwen3-coder",), provider="ollama"),
    )


def test_pi_provider_init(pi_provider: PiProvider) -> None:
    assert pi_provider._settings.models == ("qwen3-coder",)
    assert pi_provider._settings.provider == "ollama"


def test_pi_provider_build_args_new_session(pi_provider: PiProvider) -> None:
    args = pi_provider._build_pi_args("qwen3-coder", chat_id="chat-123")
    assert args[0] == "pi"
    assert "--mode" in args
    assert "rpc" in args
    assert "--provider" in args
    assert "ollama" in args
    assert "--model" in args
    assert "qwen3-coder" in args
    # Fresh chat: no --session and no --no-session — let Pi create a new
    # session file under --session-dir so the transcript persists.
    assert "--session" not in args
    assert "--no-session" not in args
    assert "--session-dir" in args


def test_pi_provider_build_args_resume_session(pi_provider: PiProvider) -> None:
    session_dir = pi_provider._session_dir / "chat-123"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session.jsonl"
    session_file.write_text('{"id":"1","type":"user","content":"hi"}\n')
    args = pi_provider._build_pi_args("qwen3-coder", chat_id="chat-123")
    assert "--session" in args
    assert str(session_file) in args
    assert "--no-session" not in args


def test_pi_provider_build_args_routes_builtin_provider(pi_provider: PiProvider) -> None:
    """Models like ``openai/gpt-4o`` must use Pi's built-in provider,
    not the default ``ollama`` (which would 404 at ollama.com)."""
    args = pi_provider._build_pi_args("openai/gpt-4o", chat_id="chat-xyz")
    p_idx = args.index("--provider")
    m_idx = args.index("--model")
    assert args[p_idx + 1] == "openai"
    assert args[m_idx + 1] == "gpt-4o"


def test_pi_provider_build_args_bare_model_uses_default_provider(pi_provider: PiProvider) -> None:
    """Bare model ids (no slash) keep using the configured default provider."""
    args = pi_provider._build_pi_args("kimi-k2.7-code:cloud", chat_id="chat-xyz")
    p_idx = args.index("--provider")
    m_idx = args.index("--model")
    assert args[p_idx + 1] == "ollama"
    assert args[m_idx + 1] == "kimi-k2.7-code:cloud"


def test_pi_provider_build_args_openrouter_preserves_subpath(pi_provider: PiProvider) -> None:
    """Openrouter model ids contain a vendor slash (e.g. anthropic/claude-3.5-sonnet);
    only the leading ``openrouter/`` should be peeled off."""
    args = pi_provider._build_pi_args("openrouter/anthropic/claude-3.5-sonnet", chat_id="chat-xyz")
    p_idx = args.index("--provider")
    m_idx = args.index("--model")
    assert args[p_idx + 1] == "openrouter"
    assert args[m_idx + 1] == "anthropic/claude-3.5-sonnet"


def test_pi_provider_build_args_unknown_prefix_passes_through(pi_provider: PiProvider) -> None:
    """A prefix that isn't a known Pi built-in stays bundled with the model id and
    routes through the default provider, so custom models.json entries keep working."""
    args = pi_provider._build_pi_args("custom-vendor/some-model", chat_id="chat-xyz")
    p_idx = args.index("--provider")
    m_idx = args.index("--model")
    assert args[p_idx + 1] == "ollama"
    assert args[m_idx + 1] == "custom-vendor/some-model"


def test_build_prompt_command_text_only(pi_provider: PiProvider) -> None:
    from ciao.models import AgentRequest
    request = AgentRequest(prompt="hello", model="qwen3-coder", mode="normal")
    cmd = pi_provider._build_prompt_command(request)
    assert cmd == {"type": "prompt", "message": "hello"}


def test_build_prompt_command_with_images(pi_provider: PiProvider, tmp_path: Path) -> None:
    from ciao.models import AgentRequest, ImageAttachment
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"png-bytes")
    request = AgentRequest(
        prompt="Describe this.",
        model="qwen3-coder",
        mode="normal",
        images=[
            ImageAttachment(path=img_path, mime_type="image/png", original_filename="test.png")
        ],
    )
    cmd = pi_provider._build_prompt_command(request)
    assert cmd["type"] == "prompt"
    assert cmd["message"] == "Describe this."
    assert "images" in cmd
    assert len(cmd["images"]) == 1
    assert cmd["images"][0]["type"] == "image"
    assert cmd["images"][0]["mimeType"] == "image/png"
    assert cmd["images"][0]["data"] == "cG5nLWJ5dGVz"


# ── run_pi_oneshot ───────────────────────────────────────────────────────


class _FakePiProcess:
    """Async-shaped fake subprocess for run_pi_oneshot tests.

    ``script`` is a list of bytes lines (each one a JSON-encoded Pi event
    plus newline) the helper will read sequentially from stdout. Empty
    bytes signals EOF; the helper breaks out of its read loop.
    """

    def __init__(self, script: list[bytes]) -> None:
        self._script = list(script)
        self.returncode: int | None = None
        self.killed = False
        self.terminated_with_abort = False
        self.stdin = self._FakeStdin(self)
        self.stdout = self._FakeStdout(self)
        self.stderr = self._FakeStderr()

    class _FakeStdin:
        def __init__(self, parent: "_FakePiProcess") -> None:
            self._parent = parent
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.writes.append(data)
            if data.startswith(b'{"type":"abort"'):
                self._parent.terminated_with_abort = True
                self._parent.returncode = 0

        async def drain(self) -> None:
            return None

    class _FakeStdout:
        def __init__(self, parent: "_FakePiProcess") -> None:
            self._parent = parent

        async def readline(self) -> bytes:
            if not self._parent._script:
                return b""
            return self._parent._script.pop(0)

    class _FakeStderr:
        async def read(self) -> bytes:
            return b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def test_run_pi_oneshot_returns_assistant_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Helper accumulates text_delta events until agent_end and returns."""
    import asyncio
    import json as _json
    from ciao.providers import pi as pi_mod
    from ciao.providers.pi import PiSettings, run_pi_oneshot

    script = [
        (_json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Cosy "},
        }) + "\n").encode(),
        (_json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "title"},
        }) + "\n").encode(),
        (_json.dumps({"type": "agent_end", "messages": []}) + "\n").encode(),
    ]
    fake = _FakePiProcess(script)

    async def fake_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        return fake

    monkeypatch.setattr(pi_mod.asyncio, "create_subprocess_exec", fake_spawn)

    text = asyncio.run(run_pi_oneshot(
        "user prompt",
        system_prompt="be brief",
        model="ministral-3:3b",
        settings=PiSettings(provider="ollama"),
        timeout_s=5.0,
    ))
    assert text == "Cosy title"
    # Helper sent the composed prompt envelope.
    sent = fake.stdin.writes[0].decode()
    assert "Instructions:" in sent
    assert "be brief" in sent
    assert "user prompt" in sent


def test_run_pi_oneshot_timeout_kills_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the subprocess never emits ``agent_end``, the helper raises and kills."""
    import asyncio
    from ciao.providers import pi as pi_mod
    from ciao.providers.pi import PiSettings, run_pi_oneshot

    class _HangingProcess(_FakePiProcess):
        def __init__(self) -> None:
            super().__init__(script=[])

        class _HangingStdout:
            async def readline(self) -> bytes:
                await asyncio.sleep(10)
                return b""

        def __post_init__(self) -> None:
            pass

    proc = _HangingProcess()
    proc.stdout = _HangingProcess._HangingStdout()  # type: ignore[assignment]

    async def fake_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        return proc

    monkeypatch.setattr(pi_mod.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(TimeoutError):
        asyncio.run(run_pi_oneshot(
            "anything",
            system_prompt="x",
            model="m",
            settings=PiSettings(provider="ollama"),
            timeout_s=0.05,
        ))
    assert proc.killed is True


# ── PiHandle.stop ────────────────────────────────────────────────────────────


import asyncio as _asyncio


def test_pi_handle_stop_writes_abort_to_stdin() -> None:
    from ciao.providers.pi import PiHandle

    class _StdinStub:
        def __init__(self) -> None:
            self.writes: list[bytes] = []
            self.drained = False

        def write(self, data: bytes) -> None:
            self.writes.append(data)

        async def drain(self) -> None:
            self.drained = True

    class _ProcStub:
        def __init__(self) -> None:
            self.stdin = _StdinStub()
            self.returncode = None

    proc = _ProcStub()
    handle = PiHandle(process=proc)  # type: ignore[arg-type]
    _asyncio.run(handle.stop())
    assert proc.stdin.writes == [b'{"type":"abort"}\n']
    assert proc.stdin.drained is True


def test_pi_handle_stop_noop_when_process_already_exited() -> None:
    from ciao.providers.pi import PiHandle

    class _ProcStub:
        def __init__(self) -> None:
            self.stdin = None
            self.returncode = 0  # already exited

    handle = PiHandle(process=_ProcStub())  # type: ignore[arg-type]
    _asyncio.run(handle.stop())  # must not raise


def test_pi_handle_stop_swallows_broken_pipe() -> None:
    from ciao.providers.pi import PiHandle

    class _Stdin:
        def write(self, data: bytes) -> None:
            raise BrokenPipeError("pipe gone")

        async def drain(self) -> None:
            pass

    class _ProcStub:
        def __init__(self) -> None:
            self.stdin = _Stdin()
            self.returncode = None

    handle = PiHandle(process=_ProcStub())  # type: ignore[arg-type]
    _asyncio.run(handle.stop())  # must not raise


# ── ensure_models_json ───────────────────────────────────────────────────────


import json as _json

from ciao.providers.pi import PiSettings as _PiSettings, ensure_models_json


def test_ensure_models_json_cloud_uses_anthropic_messages(tmp_path) -> None:
    pi = _PiSettings(models=("kimi-k2.7-code:cloud",), provider="ollama", base_url="https://ollama.com")
    path = tmp_path / "models.json"
    ensure_models_json(
        pi,
        ollama_base_url="https://ollama.com",
        ollama_api_key="real-key",
        extra_models=("deepseek-v4-flash:cloud", "kimi-k2.7-code:cloud"),
        path=path,
    )
    data = _json.loads(path.read_text())
    provider = data["providers"]["ollama"]
    # Cloud setups route through ollama.com's Anthropic-Messages endpoint
    # so vision works for kimi-class models (OpenAI-compat path silently
    # drops images for kimi at the ollama.com gateway).
    assert provider["api"] == "anthropic-messages"
    assert provider["authHeader"] is True
    # Anthropic SDK appends /v1/messages, so the stored base must be bare host.
    assert provider["baseUrl"] == "https://ollama.com"
    assert provider["apiKey"] == "real-key"
    # Every model is declared multimodal so pi-ai won't gate image blocks.
    for m in provider["models"]:
        assert "image" in m["input"]
    ids = [m["id"] for m in provider["models"]]
    assert ids == ["kimi-k2.7-code:cloud", "deepseek-v4-flash:cloud"]


def test_ensure_models_json_cloud_strips_v1_suffix(tmp_path) -> None:
    pi = _PiSettings(models=("m",), provider="ollama")
    path = tmp_path / "models.json"
    ensure_models_json(pi, ollama_base_url="https://ollama.com/v1", ollama_api_key="k", path=path)
    data = _json.loads(path.read_text())
    # Anthropic SDK appends /v1/messages itself — passing a /v1 base would
    # produce /v1/v1/messages and fail.
    assert data["providers"]["ollama"]["baseUrl"] == "https://ollama.com"


def test_ensure_models_json_local_daemon_uses_openai_completions(tmp_path) -> None:
    pi = _PiSettings(models=(), provider="ollama", base_url="http://localhost:11434")
    path = tmp_path / "models.json"
    ensure_models_json(pi, ollama_base_url="http://localhost:11434", ollama_api_key="", path=path)
    data = _json.loads(path.read_text())
    # Local daemon doesn't speak Anthropic — fall back to OpenAI-compat at /v1.
    assert data["providers"]["ollama"]["baseUrl"] == "http://localhost:11434/v1"
    assert data["providers"]["ollama"]["api"] == "openai-completions"
    # Empty key falls back to the literal "ollama" placeholder local daemons use.
    assert data["providers"]["ollama"]["apiKey"] == "ollama"
    compat = data["providers"]["ollama"]["compat"]
    assert compat["supportsDeveloperRole"] is False
    assert compat["supportsReasoningEffort"] is False


def test_ensure_models_json_uses_configured_provider_name(tmp_path) -> None:
    pi = _PiSettings(models=("m",), provider="custom-ollama")
    path = tmp_path / "models.json"
    ensure_models_json(pi, ollama_base_url="http://x", ollama_api_key="k", path=path)
    data = _json.loads(path.read_text())
    assert "custom-ollama" in data["providers"]
    assert "ollama" not in data["providers"]


def test_ensure_models_json_cloud_compat_eager_streaming_disabled(tmp_path) -> None:
    pi = _PiSettings(models=("m",), provider="ollama")
    path = tmp_path / "models.json"
    ensure_models_json(pi, ollama_base_url="https://ollama.com", ollama_api_key="k", path=path)
    data = _json.loads(path.read_text())
    # ollama.com's Anthropic endpoint rejects per-tool eager_input_streaming.
    assert data["providers"]["ollama"]["compat"]["supportsEagerToolInputStreaming"] is False
