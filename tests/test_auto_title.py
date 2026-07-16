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

    # apfel is opt-in: it only runs when explicitly selected as the title model.
    title = await _generate_chat_title("hello world", assistant_text="", model="apfel")
    assert title == "Test Title Generated"


@pytest.mark.asyncio
async def test_generate_chat_title_via_apfel_failure_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that _generate_chat_title falls back when apfel exits with non-zero."""
    monkeypatch.setattr(shutil, "which", lambda cmd: "/opt/homebrew/bin/apfel" if cmd == "apfel" else None)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess(1, b"", b"Model not ready")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    # Fail the provider one-shot too so it goes to deterministic fallback.
    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

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

    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title("How do I write Python unit tests?", assistant_text="")
    assert title == "How do I write Python unit"


@pytest.mark.asyncio
async def test_generate_chat_title_apfel_selected_but_not_installed_falls_back_to_haiku(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"apfel" is a routing sentinel from the Settings picker (provider=apple),
    not a real Claude/Ollama model id. When the binary isn't actually
    installed, run_oneshot must never receive "apfel" literally — that always
    fails with "There's an issue with the selected model (apfel)" and drops
    straight to the raw-text truncated fallback title.
    """
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    captured_model: list[str] = []

    async def fake_oneshot(*args, **kwargs):
        captured_model.append(kwargs.get("model", ""))
        return "Generated Title"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title("hello world", assistant_text="", model="apfel")
    assert captured_model == ["haiku"]
    assert title == "Generated Title"


@pytest.mark.asyncio
async def test_generate_chat_title_uses_codex_oneshot(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    captured: dict = {}

    async def fake_oneshot(*args, **kwargs):
        captured.update(kwargs)
        return "Codex Title"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title(
        "Investigate provider support",
        model="gpt-test",
        provider="codex",
        cwd=tmp_path,
    )

    assert title == "Codex Title"
    assert captured["provider"] == "codex"
    assert captured["model"] == "gpt-test"
    assert captured["cwd"] == tmp_path


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


def test_clean_title_rejects_reply_shaped_output() -> None:
    from ciao.web.project_chats import _clean_title

    user = "Create google tasks for my wedding checklist please"
    # The title model answered the message instead of titling it.
    assert (
        _clean_title(
            "I'd be happy to help you create Google Tasks, but I need more "
            "details about what tasks you want.",
            user,
        )
        == "Create google tasks for my wedding"
    )
    assert _clean_title("Sure, let me create those tasks for you", user) == (
        "Create google tasks for my wedding"
    )
    # Real titles pass through untouched, including ones with I/O-style words.
    assert _clean_title("Wedding Checklist Google Tasks", user) == (
        "Wedding Checklist Google Tasks"
    )
    assert _clean_title("I/O Performance Tuning", user) == "I/O Performance Tuning"


def test_clean_title_rejects_negated_and_apologetic_openers() -> None:
    """A model handed a contentless excerpt answers instead of titling
    ("I don't have any prior context…"). These openers must be caught so
    the reply never lands as the title (regression: "I don't"/"There's no"
    slipped past the original affirmative-only guard)."""
    from ciao.web.project_chats import _clean_title

    user = "Add traction and pilot state to the delivery intelligence slides"
    for reply in (
        "I don't have any prior context to continue from. Could you clarify?",
        "There's no prior conversation for me to continue.",
        "It looks like there isn't enough information to continue.",
        "I cannot continue without more details.",
        "Let me know what you'd like me to continue with.",
    ):
        assert _clean_title(reply, user) == (
            "Add traction and pilot state to"
        ), reply


def test_is_contentless_prompt() -> None:
    from ciao.web.project_chats import _is_contentless_prompt

    for prompt in ("continue", "Continue.", " GO ON ", "ok", "yes", "keep going"):
        assert _is_contentless_prompt(prompt) is True, prompt
    for prompt in ("continue the slide deck", "ok now add a chart", "resume the migration"):
        assert _is_contentless_prompt(prompt) is False, prompt


@pytest.mark.asyncio
async def test_generate_title_skips_model_for_contentless_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare "continue" must never reach the title model — otherwise the
    model answers it conversationally and that reply becomes the title."""
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    called = False

    async def spy_oneshot(*args, **kwargs):
        nonlocal called
        called = True
        return "I don't have any prior context to continue from."

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", spy_oneshot)
    title, engine, detail = await _generate_chat_title_with_engine("continue", model="haiku")
    assert called is False
    assert engine == "fallback"
    assert title == "continue"
    # A contentless prompt is skipped, not a failure — no upstream detail.
    assert detail is None


@pytest.mark.asyncio
async def test_generate_title_reports_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    async def good_oneshot(*args, **kwargs):
        return "Wedding Task Planning"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", good_oneshot)
    title, engine, detail = await _generate_chat_title_with_engine(
        "Create google tasks for my wedding", model="haiku"
    )
    assert (title, engine, detail) == ("Wedding Task Planning", "claude:haiku", None)

    async def reply_shaped_oneshot(*args, **kwargs):
        return "I'd be happy to help you create Google Tasks, but I need more info."

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", reply_shaped_oneshot)
    title, engine, detail = await _generate_chat_title_with_engine(
        "Create google tasks for my wedding", model="haiku"
    )
    assert engine == "fallback"
    assert title == "Create google tasks for my wedding"
    # Reply-shaped output is a soft fallback, not an upstream failure.
    assert detail is None

    async def failing_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)
    title, engine, detail = await _generate_chat_title_with_engine(
        "Create google tasks for my wedding", model="haiku"
    )
    assert engine == "fallback"
    assert title == "Create google tasks for my wedding"
    # A hard failure surfaces the upstream error text for job_runs.
    assert detail == "provider unavailable"
