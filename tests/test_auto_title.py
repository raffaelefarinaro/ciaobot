from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
import shutil

from ciao.web.project_chats import _generate_chat_title, resolve_title_model


class FakeProcess:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_generate_chat_title_via_apfel_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _generate_chat_title succeeds when apfel is installed and works."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/opt/homebrew/bin/apfel" if cmd == "apfel" else None)

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args[0] == "apfel"
        assert "-q" in args
        assert "-s" in args
        return FakeProcess(0, b"   Test Title Generated   \n", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    title = await _generate_chat_title("hello world", assistant_text="")
    assert title == "Test Title Generated"


@pytest.mark.asyncio
async def test_generate_chat_title_via_apfel_failure_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _generate_chat_title falls back when apfel exits with non-zero."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/opt/homebrew/bin/apfel" if cmd == "apfel" else None)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess(1, b"", b"Model not ready")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    # We mock claude_agent_sdk import to raise exception so it goes to deterministic fallback
    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    title = await _generate_chat_title("This is a very long user message that should be truncated to some words", assistant_text="")
    # Deterministic fallback takes first ~6 words
    assert title == "This is a very long user"


@pytest.mark.asyncio
async def test_generate_chat_title_via_apfel_exception_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _generate_chat_title falls back when apfel raise Exception."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/opt/homebrew/bin/apfel" if cmd == "apfel" else None)

    async def fake_create_subprocess_exec(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    import sys
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)

    title = await _generate_chat_title("How do I write Python unit tests?", assistant_text="")
    assert title == "How do I write Python unit"


def test_resolve_title_model_uses_override() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "CIAO_OLLAMA_API_KEY": "sk-cloud"})
    config.title_model_override = "anthropic/claude-haiku-4.5"
    assert resolve_title_model(config, "personal") == "anthropic/claude-haiku-4.5"


def test_resolve_title_model_uses_workspace_haiku_when_automatic() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "CIAO_OLLAMA_API_KEY": "sk-cloud"})
    config.title_model_override = ""
    assert resolve_title_model(config, "personal") == config.ollama.haiku_model
    assert resolve_title_model(config, "work") == "haiku"


def test_resolve_title_model_falls_back_without_workspace() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    config.title_model_override = ""
    assert resolve_title_model(config) == config.title_model
