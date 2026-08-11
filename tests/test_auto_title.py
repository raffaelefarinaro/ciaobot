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
@pytest.mark.parametrize("model", ["apple", "apfel"])
async def test_generate_chat_title_on_device_success(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """The on-device model titles the chat when it is the selected engine.

    Both ids are accepted: "apfel" is the legacy value still stored in settings
    saved before titles moved off the apfel CLI to FoundationModels.
    """
    from ciao import native_sidecar

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)

    captured: dict[str, str] = {}

    async def fake_respond(prompt, *, instructions="", timeout=0):
        captured["prompt"] = prompt
        captured["instructions"] = instructions
        return "   Test Title Generated   \n"

    monkeypatch.setattr(native_sidecar, "respond", fake_respond)

    title = await _generate_chat_title("hello world", assistant_text="", model=model)
    assert title == "Test Title Generated"
    assert "hello world" in captured["prompt"]
    # The titling system prompt has to reach the model, or it answers the
    # excerpt instead of naming it.
    assert "titling function" in captured["instructions"]


@pytest.mark.asyncio
async def test_generate_chat_title_skips_on_device_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apple Intelligence off / older macOS: fall through, never call the sidecar."""
    from ciao import native_sidecar

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: False)

    async def explode(*args, **kwargs):
        raise AssertionError("must not run the sidecar when it is unavailable")

    monkeypatch.setattr(native_sidecar, "respond", explode)

    async def fake_oneshot(*args, **kwargs):
        return "Cloud Title"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title("hello world", assistant_text="", model="apple")
    assert title == "Cloud Title"


@pytest.mark.asyncio
async def test_generate_chat_title_on_device_failure_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sidecar that errors mid-generation must not lose the title."""
    from ciao import native_sidecar

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)

    async def failing_respond(*args, **kwargs):
        raise native_sidecar.SidecarError("the on-device model failed")

    monkeypatch.setattr(native_sidecar, "respond", failing_respond)

    # Fail the provider one-shot too so it goes to deterministic fallback.
    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title(
        "This is a very long user message that should be truncated to some words",
        assistant_text="",
        model="apple",
    )
    # Deterministic fallback takes first ~6 words
    assert title == "This is a very long user"


@pytest.mark.asyncio
async def test_generate_chat_title_on_device_crash_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an unexpected exception (not SidecarError) must not escape."""
    from ciao import native_sidecar

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)

    async def exploding_respond(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(native_sidecar, "respond", exploding_respond)

    async def fake_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await _generate_chat_title(
        "How do I write Python unit tests?", assistant_text="", model="apple"
    )
    assert title == "How do I write Python unit"


@pytest.mark.asyncio
async def test_generate_chat_title_sentinel_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"apple"/"apfel" are routing sentinels from the Settings picker, not real
    Claude/Ollama model ids. When the on-device model is unavailable,
    run_oneshot must never receive the sentinel literally — that always fails
    with "There's an issue with the selected model" and drops straight to the
    raw-text truncated fallback title.
    """
    from ciao import native_sidecar

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: False)

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


def test_is_question_shaped_prompt() -> None:
    """Meta-inquiry openers defer to the post-reply title path (#176)."""
    from ciao.web.project_chats import _is_question_shaped_prompt

    for prompt in (
        "why no recent sessions?",
        "Why is X broken?",
        "what does Y mean?",
        "how do I fix the Automation page?",
        "where are the job logs?",
        "who owns this schedule?",
        "is the title job running?",
        "can the titler see the reply?",
        # No trailing "?" but a question opener still counts.
        "why no recent sessions",
        "How do I write Python unit tests",
    ):
        assert _is_question_shaped_prompt(prompt) is True, prompt
    for prompt in (
        "Create google tasks for my wedding checklist please",
        "Write a PRD about barcode scanning",
        "Summarize this article",
        "continue",
        "",
        "   ",
        "Add traction and pilot state to the delivery intelligence slides",
    ):
        assert _is_question_shaped_prompt(prompt) is False, prompt


@pytest.mark.asyncio
async def test_title_prefers_assistant_framing_for_meta_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meta-question whose reply pivots to a different topic should yield a
    title for the topic, not the question (#176). The titler is fed both the
    first user message and the first assistant reply; the prompt biases it
    toward the reply's framing."""
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    captured: dict = {}

    async def fake_oneshot(user_prompt: str, **kwargs):
        captured["prompt"] = user_prompt
        # The model titles the reply's topic, not the opening question.
        return "Automation Page Job Log"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title, engine, detail = await _generate_chat_title_with_engine(
        "why no recent sessions?",
        assistant_text=(
            "The Automation page only shows runs from the last 7 days. "
            "Your job log rotated at 2 MB, so older title runs dropped out "
            "of the view, not because they stopped running."
        ),
        model="haiku",
    )
    assert title == "Automation Page Job Log"
    assert engine == "claude:haiku"
    assert detail is None
    # Both sides of the exchange are fed in, and the prompt steers toward the
    # reply's topic when the question and reply differ.
    assert "why no recent sessions?" in captured["prompt"]
    assert "Assistant reply:" in captured["prompt"]
    assert "reply is about" in captured["prompt"]


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


@pytest.mark.asyncio
async def test_generate_chat_title_on_device_failure_propagates_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the on-device path fails AND the provider fallback also fails, the
    captured exception from the on-device path must reach the caller (#257).
    Previously the Apple except-block only logged and dropped the exception;
    the caller's error_detail ended up empty.
    """
    from ciao import native_sidecar
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)

    async def failing_respond(*args, **kwargs):
        raise native_sidecar.SidecarError("Apple sidecar timed out")

    monkeypatch.setattr(native_sidecar, "respond", failing_respond)

    async def failing_oneshot(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)

    title, engine, detail = await _generate_chat_title_with_engine(
        "Plan a standup retro", assistant_text="", model="apple"
    )
    assert engine == "fallback"
    assert title == "Plan a standup retro"
    # The provider path's detail wins because it is the most recent cause;
    # an empty `detail` would have been the original regression.
    assert detail == "provider unavailable"


@pytest.mark.asyncio
async def test_generate_chat_title_apple_only_failure_carries_apple_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only the on-device path fails and the provider fallback succeeds,
    the caller gets a clean title. But when the Apple failure is the only
    failure recorded (provider returns empty, no exception), the on-device
    detail should still flow through so the record isn't blank (#257).
    """
    from ciao import native_sidecar
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(native_sidecar, "apple_model_available", lambda: True)

    async def failing_respond(*args, **kwargs):
        raise native_sidecar.SidecarError("Apple sidecar timed out")

    monkeypatch.setattr(native_sidecar, "respond", failing_respond)

    async def empty_oneshot(*args, **kwargs):
        return ""

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", empty_oneshot)

    title, engine, detail = await _generate_chat_title_with_engine(
        "Brainstorm launch ideas", assistant_text="", model="apple"
    )
    assert engine == "fallback"
    assert title == "Brainstorm launch ideas"
    # The empty-return path falls back to the on-device detail rather than
    # leaving the record blank.
    assert detail == "Apple sidecar timed out"


@pytest.mark.asyncio
async def test_generate_chat_title_oneshot_empty_return_reports_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When run_oneshot returns an empty string with no exception, the titler
    must still report a non-null detail so the job record distinguishes
    "model returned empty" from "the model never ran" (#257)."""
    from ciao.web.project_chats import _generate_chat_title_with_engine

    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    async def empty_oneshot(*args, **kwargs):
        return ""

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", empty_oneshot)

    title, engine, detail = await _generate_chat_title_with_engine(
        "Sketch a release checklist", model="haiku"
    )
    assert engine == "fallback"
    assert title == "Sketch a release checklist"
    assert detail == "upstream returned empty text"
