"""Tests for post-archive session insights extraction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao import insights
from ciao.providers.ollama import OllamaSettings


# ── filter_session_jsonl ─────────────────────────────────────────────────


def _project_dir(workspace_root: Path) -> Path:
    """Mirror the directory layout that `_claude_projects_dir` resolves to."""
    slug = str(workspace_root).replace("/", "-").lstrip("-")
    return Path.home() / ".claude" / "projects" / f"-{slug}"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_filter_returns_none_when_jsonl_missing(tmp_path: Path) -> None:
    out = insights.filter_session_jsonl(tmp_path, "missing-session")
    assert out is None


def test_filter_returns_none_for_empty_session_id(tmp_path: Path) -> None:
    assert insights.filter_session_jsonl(tmp_path, "") is None


def test_filter_keeps_user_assistant_text_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-abc"
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi back"},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 2
    assert lines[0]["idx"] == 1
    assert lines[0]["type"] == "user"
    assert lines[1]["idx"] == 2
    assert lines[1]["type"] == "assistant"


def test_filter_truncates_read_tool_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-trunc"
    long_body = "x" * 5000
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "tu_1",
             "input": {"file_path": "/big.txt"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "is_error": False, "content": long_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["type"] == "tool_result"
    assert "[truncated, total=5000 chars]" in result_block["content"]
    assert len(result_block["content"]) < 1000


def test_filter_keeps_edit_and_bash_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-edit"
    edit_body = "long edit output: " + ("y" * 1000)
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "id": "tu_e",
             "input": {"file_path": "/foo.py", "old_string": "a", "new_string": "b"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_e",
             "is_error": False, "content": edit_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["content"] == edit_body
    # Tool input also kept in full for Edit
    use_block = lines[0]["content"][0]
    assert use_block["input"]["new_string"] == "b"


def test_filter_keeps_errors_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-err"
    err_body = "error: " + ("z" * 5000)
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "id": "tu_r",
             "input": {"file_path": "/missing.txt"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_r",
             "is_error": True, "content": err_body},
        ]}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    result_block = lines[1]["content"][0]
    assert result_block["is_error"] is True
    assert result_block["content"] == err_body


def test_filter_drops_sidechain_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    workspace = tmp_path / "ws"
    session_id = "sess-sc"
    jsonl = _project_dir(workspace) / f"{session_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "isSidechain": True, "message": {"content": "subagent"}},
        {"type": "user", "message": {"content": "main"}},
    ])

    out = insights.filter_session_jsonl(workspace, session_id)
    assert out is not None
    lines = [json.loads(line) for line in out.splitlines()]
    assert len(lines) == 1
    assert lines[0]["content"][0]["text"] == "main"


# ── extract_and_append ───────────────────────────────────────────────────


def _ollama() -> OllamaSettings:
    return OllamaSettings(
        models=(),
        base_url="http://localhost:11434",
        api_key="ollama",
    )


def _config():
    from ciao.config import CiaoConfig
    return CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "CIAO_OLLAMA_API_KEY": "sk-cloud"})


def test_extract_appends_section_when_archive_exists(tmp_path: Path) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n\nbody\n", encoding="utf-8")

    async def fake_call(filtered_jsonl: str, model: str, env: dict) -> str:
        return "## Errors\n- something failed [idx=3]\n"

    with patch.object(insights, "_call_model", side_effect=fake_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:cloud",
        ))

    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" in text
    assert "## Errors" in text
    assert "[idx=3]" in text


def test_extract_is_idempotent_when_section_already_present(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text(
        "# Existing\n\n## Session insights\n\nold body\n", encoding="utf-8"
    )

    async def fake_call(filtered_jsonl: str, model: str, env: dict) -> str:
        return "fresh content"

    called = {"count": 0}

    async def counting_call(filtered_jsonl: str, model: str, env: dict) -> str:
        called["count"] += 1
        return await fake_call(filtered_jsonl, model, env)

    with patch.object(insights, "_call_model", side_effect=counting_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:cloud",
        ))

    assert called["count"] == 0
    text = archive.read_text(encoding="utf-8")
    assert text.count("## Session insights") == 1
    assert "old body" in text
    assert "fresh content" not in text


def test_extract_skips_silently_when_archive_missing(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.md"

    asyncio.run(insights.extract_and_append(
        archive_path=missing,
        filtered_jsonl="dummy",
        config=_config(),
        model="deepseek-v4-flash:cloud",
    ))
    assert not missing.exists()


def test_extract_retries_once_then_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    calls = {"count": 0}

    async def flaky_call(filtered_jsonl: str, model: str, env: dict) -> str:
        calls["count"] += 1
        raise RuntimeError("boom")

    async def no_sleep(_: float) -> None:
        return None

    with patch.object(insights, "_call_model", side_effect=flaky_call), \
         patch.object(insights.asyncio, "sleep", side_effect=no_sleep):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:cloud",
        ))

    assert calls["count"] == 2
    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" not in text


def test_extract_succeeds_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    calls = {"count": 0}

    async def flaky_call(filtered_jsonl: str, model: str, env: dict) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient")
        return "## Decisions\n- chose retry [idx=1]\n"

    async def no_sleep(_: float) -> None:
        return None

    with patch.object(insights, "_call_model", side_effect=flaky_call), \
         patch.object(insights.asyncio, "sleep", side_effect=no_sleep):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:cloud",
        ))

    assert calls["count"] == 2
    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" in text
    assert "[idx=1]" in text


def test_extract_skips_silently_on_empty_model_output(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    async def empty_call(filtered_jsonl: str, model: str, env: dict) -> str:
        return ""

    with patch.object(insights, "_call_model", side_effect=empty_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:cloud",
        ))

    assert "## Session insights" not in archive.read_text(encoding="utf-8")


def test_call_model_uses_oneshot_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_call_model routes through the unified run_oneshot helper."""
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, env, timeout_s=120.0):
        captured["model"] = model
        captured["timeout_s"] = timeout_s
        return "## Decisions\n- via oneshot [idx=1]\n"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    asyncio.run(insights.extract_and_append(
        archive_path=archive,
        filtered_jsonl="dummy-jsonl",
        config=_config(),
        model="deepseek-v4-flash:cloud",
    ))

    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" in text
    assert "via oneshot" in text
    assert captured["model"] == "deepseek-v4-flash:cloud"
    assert captured["timeout_s"] == 120.0


def test_run_oneshot_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.providers.oneshot import run_oneshot
    from claude_agent_sdk import ResultMessage
    from dataclasses import dataclass

    # 1. Test ResultMessage with is_error=True
    async def fake_query_result_error(prompt, options):
        yield ResultMessage(
            subtype="failure",
            duration_ms=100,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="123",
            stop_reason="error",
            total_cost_usd=0,
            usage={},
            result="API Error: Rate Limit Exceeded",
        )

    monkeypatch.setattr("ciao.providers.oneshot.query", fake_query_result_error)
    with pytest.raises(RuntimeError, match="API Error: Rate Limit Exceeded"):
        asyncio.run(run_oneshot("prompt", system_prompt="sys", model="m"))

    # 2. Test AssistantMessage with error attribute
    @dataclass
    class DummyTextBlock:
        text: str

    @dataclass
    class DummyAssistantMessage:
        content: list
        model: str = "<synthetic>"
        error: str = "authentication_failed"

    async def fake_query_asst_error(prompt, options):
        yield DummyAssistantMessage(content=[DummyTextBlock(text="Failed to authenticate")])

    monkeypatch.setattr("ciao.providers.oneshot.query", fake_query_asst_error)
    monkeypatch.setattr("ciao.providers.oneshot.AssistantMessage", DummyAssistantMessage)
    monkeypatch.setattr("ciao.providers.oneshot.TextBlock", DummyTextBlock)
    with pytest.raises(RuntimeError, match="Failed to authenticate"):
        asyncio.run(run_oneshot("prompt", system_prompt="sys", model="m"))


# ── resolve_insights_model ───────────────────────────────────────────────


def test_resolve_insights_model_uses_override() -> None:
    config = _config()
    config.insights_model_override = "anthropic/claude-haiku-4.5"
    assert insights.resolve_insights_model(config, "personal") == "anthropic/claude-haiku-4.5"


def test_resolve_insights_model_uses_workspace_sonnet_when_automatic() -> None:
    config = _config()
    config.insights_model_override = ""
    assert insights.resolve_insights_model(config, "personal") == config.ollama.sonnet_model
    assert insights.resolve_insights_model(config, "work") == "sonnet"


def test_resolve_insights_model_falls_back_without_workspace() -> None:
    config = _config()
    config.insights_model_override = ""
    assert insights.resolve_insights_model(config) == config.insights_model
