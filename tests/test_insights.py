"""Tests for post-archive session insights extraction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao import insights, native_sidecar


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


def _config():
    from ciao.config import CiaoConfig
    return CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})


def test_extract_appends_section_when_archive_exists(tmp_path: Path) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n\nbody\n", encoding="utf-8")

    async def fake_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return "## Errors\n- something failed [idx=3]\n"

    with patch.object(insights, "_call_model", side_effect=fake_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
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

    async def fake_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return "fresh content"

    called = {"count": 0}

    async def counting_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        called["count"] += 1
        return await fake_call(filtered_jsonl, model)

    with patch.object(insights, "_call_model", side_effect=counting_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
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
        model="deepseek-v4-flash:0731-cloud",
    ))
    assert not missing.exists()


def test_extract_retries_once_then_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    calls = {"count": 0}

    async def flaky_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
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
            model="deepseek-v4-flash:0731-cloud",
        ))

    assert calls["count"] == 2
    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" not in text
    run = json.loads((tmp_path / "job_runs.jsonl").read_text().splitlines()[0])
    assert run["status"] == "error"
    assert run["error"] == "boom"


def test_extract_succeeds_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    calls = {"count": 0}

    async def flaky_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
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
            model="deepseek-v4-flash:0731-cloud",
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

    async def empty_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return ""

    with patch.object(insights, "_call_model", side_effect=empty_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
        ))

    assert "## Session insights" not in archive.read_text(encoding="utf-8")


def test_call_model_uses_oneshot_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_call_model routes through the unified run_oneshot helper."""
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, env=None, timeout_s=120.0):
        captured["model"] = model
        captured["timeout_s"] = timeout_s
        return "## Decisions\n- via oneshot [idx=1]\n"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    asyncio.run(insights.extract_and_append(
        archive_path=archive,
        filtered_jsonl="dummy-jsonl",
        config=_config(),
        model="deepseek-v4-flash:0731-cloud",
    ))

    text = archive.read_text(encoding="utf-8")
    assert "## Session insights" in text
    assert "via oneshot" in text
    assert captured["model"] == "deepseek-v4-flash:0731-cloud"
    # Was a flat 120.0, which the slow operator-chosen models this path allows
    # could not meet (measured 214-253s). Now configurable, default 600s.
    assert captured["timeout_s"] == insights._DEFAULT_TIMEOUT_S


def test_call_model_uses_apple_intelligence_for_the_local_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    monkeypatch.setattr("ciao.native_sidecar.apple_model_available", lambda: True)

    async def fake_respond(prompt: str, *, instructions: str, timeout: float) -> str:
        captured["prompt"] = prompt
        captured["instructions"] = instructions
        return "## Decisions\n- kept the local path"

    monkeypatch.setattr("ciao.native_sidecar.respond", fake_respond)
    result = asyncio.run(insights._call_model('{"idx": 1}', "apple"))

    assert '{"idx": 1}' in captured["prompt"]
    assert result.startswith("## Decisions")
    assert captured["instructions"] == insights._INSIGHTS_SYSTEM_PROMPT


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


def test_resolve_insights_model_uses_workspace_default_when_automatic() -> None:
    config = _config()
    config.insights_model_override = ""
    assert insights.resolve_insights_model(config, "personal") == config.claude_default_model
    assert insights.resolve_insights_model(config, "work") == config.claude_default_model


def test_resolve_insights_model_falls_back_without_workspace() -> None:
    config = _config()
    config.insights_model_override = ""
    assert insights.resolve_insights_model(config) == config.insights_model


def test_resolve_insights_call_routes_qualified_runtime_provider() -> None:
    config = _config()

    assert insights._resolve_insights_call(
        config, "opencode:anthropic/claude-sonnet-4.5"
    ) == ("anthropic/claude-sonnet-4.5", "opencode", None)


# ── project doc update wiring ────────────────────────────────────────────


_PROJECT_DOC = """---
tags: [project]
---
# Demo Project

## Open loops
- Decide on storage.
"""


def test_extract_updates_project_doc_when_insights_carry_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(_PROJECT_DOC, encoding="utf-8")

    async def fake_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return "## Decisions\n- Chose sqlite over postgres because local-first. [idx=2]\n"

    updated_doc = _PROJECT_DOC.replace(
        "- Decide on storage.", "- ~~Decide on storage~~ Resolved: sqlite (local-first)."
    ).strip()

    async def fake_oneshot(prompt, *, system_prompt, model, env=None, timeout_s=120.0, **kwargs):
        return updated_doc

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    with patch.object(insights, "_call_model", side_effect=fake_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
            workspace_root=tmp_path,
            vault_root=tmp_path / "vault",
            project_doc_path="doc.md",
        ))

    assert "## Session insights" in archive.read_text(encoding="utf-8")
    assert "sqlite (local-first)" in doc.read_text(encoding="utf-8")


def test_extract_skips_project_doc_when_path_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n", encoding="utf-8")
    doc = tmp_path / "doc.md"
    doc.write_text(_PROJECT_DOC, encoding="utf-8")

    async def fake_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return "## Decisions\n- Chose sqlite over postgres because local-first. [idx=2]\n"

    async def fail_oneshot(prompt, **kwargs):
        raise AssertionError("doc updater must not be called without a path")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fail_oneshot)

    with patch.object(insights, "_call_model", side_effect=fake_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="dummy",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
            workspace_root=tmp_path,
            vault_root=tmp_path / "vault",
        ))

    assert doc.read_text(encoding="utf-8") == _PROJECT_DOC


# ── backfill_insights_task ───────────────────────────────────────────────


def test_backfill_insights_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Set up directory layout
    vault_root = tmp_path / "vault"
    workspace_root = tmp_path / "ws"
    
    # Matches production: main.py builds transcript_root as
    # config.vault_root / "Logs" / "Chats". vault_root is already the
    # memory-vault container, so there is no second "memory-vault" level.
    chats_dir = vault_root / "Logs" / "Chats" / "chat-123" / "claude"
    chats_dir.mkdir(parents=True)
    
    # 1. Archive already done
    done_archive = chats_dir / "already-done-00000000-0000-0000-0000-000000000001.md"
    done_archive.write_text("# Archived chat\n\n## Session insights\n- old insights", encoding="utf-8")
    
    # 2. Archive to be backfilled (full mode)
    full_archive = chats_dir / "full-mode-00000000-0000-0000-0000-000000000002.md"
    full_archive.write_text("# Archived chat full\n", encoding="utf-8")
    
    # 3. Archive to be backfilled (text fallback mode)
    text_archive = chats_dir / "text-mode-00000000-0000-0000-0000-000000000003.md"
    text_archive.write_text("# Archived chat text\n", encoding="utf-8")
    
    # Create the jsonl for full mode
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    
    # normal project directory path
    slug = str(workspace_root).replace("/", "-").lstrip("-")
    project_dir = fake_home / ".claude" / "projects" / f"-{slug}"
    project_dir.mkdir(parents=True)
    
    session2_id = "00000000-0000-0000-0000-000000000002"
    jsonl = project_dir / f"{session2_id}.jsonl"
    _write_jsonl(jsonl, [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi back"}]}},
    ])
    
    config = _config()
    config.vault_root = vault_root
    config.workspace_root = workspace_root
    config.insights_model = "deepseek-v4-flash:0731-cloud"
    
    calls = []
    
    async def fake_oneshot(user_prompt: str, **kwargs) -> str:
        calls.append((user_prompt, kwargs))
        if "JSONL" in user_prompt or "line-oriented JSON" in user_prompt:
            return "## Decisions\n- Full mode decisions\n"
        else:
            return "## Decisions\n- Text mode decisions\n"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    result = asyncio.run(insights.backfill_insights_task(
        config,
        mode="both",
        concurrency=1,
    ))

    assert result == {
        "total_discovered": 3,
        "already_done": 1,
        "eligible": 2,
        "to_process": 2,
        "processed": 2,
        "success": 2,
        "skipped": 0,
        "errors": 0,
    }
    assert insights.format_backfill_summary(result) == "Processed 2/2: 2 succeeded, 0 skipped."
    
    # Check that already_done was skipped (no content change)
    assert done_archive.read_text(encoding="utf-8") == "# Archived chat\n\n## Session insights\n- old insights"
    
    # Check full mode was processed with JSONL
    full_text = full_archive.read_text(encoding="utf-8")
    assert "## Session insights" in full_text
    assert "Full mode decisions" in full_text
    
    # Check text mode fallback was processed
    text_text = text_archive.read_text(encoding="utf-8")
    assert "## Session insights" in text_text
    assert "Text mode decisions" in text_text


# ── Input budget and non-retryable overflow (issue #248) ──────────────────


def test_fit_transcript_leaves_a_small_payload_untouched() -> None:
    payload = "\n".join(f'{{"idx":{i}}}' for i in range(5))
    fitted, dropped = insights._fit_transcript(payload)
    assert fitted == payload
    assert dropped == 0


def test_fit_apple_input_keeps_newest_lines_with_small_budget() -> None:
    payload = "\n".join(f"line-{i}" for i in range(20))
    fitted, dropped = native_sidecar.fit_apple_input(payload, max_chars=30)
    assert fitted.splitlines() == ["line-17", "line-18", "line-19"]
    assert dropped == 17
    assert len(fitted) <= 30


def test_fit_transcript_drops_oldest_lines_and_keeps_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ten 10-char lines; a 25-char budget only fits the last two.
    lines = [f"{i:09d}" for i in range(10)]
    monkeypatch.setenv("CIAO_INSIGHTS_MAX_INPUT_CHARS", "25")
    fitted, dropped = insights._fit_transcript("\n".join(lines))
    kept = fitted.splitlines()
    assert kept == lines[-len(kept):], "must keep a suffix, i.e. the newest turns"
    assert dropped == len(lines) - len(kept)
    assert len(fitted) <= 25


def test_max_input_chars_ignores_junk_and_nonpositive_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in ("abc", "0", "-5", ""):
        monkeypatch.setenv("CIAO_INSIGHTS_MAX_INPUT_CHARS", bad)
        assert insights._max_input_chars() == insights._DEFAULT_MAX_INPUT_CHARS
    monkeypatch.setenv("CIAO_INSIGHTS_MAX_INPUT_CHARS", "1234")
    assert insights._max_input_chars() == 1234


def test_insights_timeout_defaults_generously_and_is_tunable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CIAO_INSIGHTS_TIMEOUT_S", raising=False)
    # The old flat 120s was below the 214-253s this path really takes.
    assert insights._insights_timeout_s() == insights._DEFAULT_TIMEOUT_S
    assert insights._insights_timeout_s() > 200
    monkeypatch.setenv("CIAO_INSIGHTS_TIMEOUT_S", "45.5")
    assert insights._insights_timeout_s() == 45.5


def test_context_overflow_is_distinguished_from_a_transient_timeout() -> None:
    overflow = Exception(
        "API Error 400 Message too long: 262183 > 125952 maximum context length"
    )
    assert insights.is_context_overflow(overflow)
    assert insights.is_context_overflow(Exception("context_length_exceeded"))
    # Transient failures must stay retryable.
    assert not insights.is_context_overflow(asyncio.TimeoutError())
    assert not insights.is_context_overflow(Exception("429 rate limit"))


def test_oversized_input_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 overflow must fail fast: the retry would send the same payload."""
    calls = 0

    async def fake_call_model(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        raise Exception("400 Message too long: 200328 > 125952")

    monkeypatch.setattr(insights, "_call_model", fake_call_model)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(insights.asyncio, "sleep", fake_sleep)
    out, err = asyncio.run(
        insights._run_model_with_retry(
            filtered_jsonl='{"idx":0}', model="some-model"
        )
    )
    assert out == ""
    assert "too long" in err.lower()
    assert calls == 1, "overflow must not be retried"
    assert slept == [], "must not burn the 30s retry wait on a deterministic failure"


def test_usage_limit_rejection_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A weekly-quota 429 is terminal upstream; the second call buys nothing.

    ``run_oneshot`` already classifies it (``transient=False``) and raises
    without retrying internally. Insights must honour that flag instead of
    sleeping 30s and re-sending, which on a backfill repeats once per archive.
    """
    from ciao.providers.oneshot import OneShotError

    calls = 0

    async def rejected(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        detail = (
            "API Error: Request rejected (429) · you have reached your "
            "weekly usage limit, upgrade for higher limits"
        )
        raise OneShotError(detail, transient=False)

    monkeypatch.setattr(insights, "_call_model", rejected)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(insights.asyncio, "sleep", fake_sleep)
    out, err = asyncio.run(
        insights._run_model_with_retry(
            filtered_jsonl='{"idx":0}', model="some-model"
        )
    )
    assert out == ""
    assert "weekly usage limit" in err
    assert calls == 1, "a terminal upstream rejection must not be retried"
    assert slept == [], "must not burn the 30s retry wait on a terminal rejection"


def test_terminal_failure_flag_only_trips_on_explicit_false() -> None:
    from ciao.providers.oneshot import OneShotError

    assert insights.is_terminal_failure(OneShotError("bad key", transient=False))
    # Retryable, and anything without the flag stays retryable (safe default).
    assert not insights.is_terminal_failure(OneShotError("empty body", transient=True))
    assert not insights.is_terminal_failure(asyncio.TimeoutError())
    assert not insights.is_terminal_failure(Exception("subprocess died"))


def test_transient_failure_still_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def flaky(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError()
        return "## Errors\n- boom [idx=1]"

    monkeypatch.setattr(insights, "_call_model", flaky)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(insights.asyncio, "sleep", fake_sleep)
    out, err = asyncio.run(
        insights._run_model_with_retry(
            filtered_jsonl='{"idx":0}', model="some-model"
        )
    )
    assert err == ""
    assert "boom" in out
    assert calls == 2


def test_backfill_caps_an_unlimited_run_and_reports_it(monkeypatch, tmp_path):
    """limit=0 must not mean "one model call per archive in the vault".

    Both automatic callers (startup, the Settings button) pass no limit, and
    the archive path was broken until recently, so this had never run against
    a real workspace. The cap is recorded rather than silent.
    """
    from ciao import insights

    monkeypatch.setenv("CIAO_INSIGHTS_BACKFILL_MAX", "2")
    assert insights._backfill_ceiling() == 2


# ── locate_insights_section ──────────────────────────────────────────────


def test_locate_returns_none_without_marker() -> None:
    assert insights.locate_insights_section("# chat\n\n## Turn 1\n\nhi\n") is None


def test_locate_finds_legacy_appended_section() -> None:
    text = "# chat\n\n## Turn 1\n\nhi\n\n## Session insights\n\n## Errors\n- x\n"
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_rejects_marker_quoted_mid_transcript() -> None:
    """A turn heading after the marker proves the marker is quoted content.

    Curation chats quote insights sections verbatim; the old substring check
    treated those archives as already processed and skipped extraction.
    """
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n"
        "## Decisions\n- old bullet\n\n## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_prefers_last_marker_over_quoted_one() -> None:
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Turn 2\n\nbye\n\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert "real" in text[location[1]:]
    assert "old" not in text[location[1]:]


def test_locate_trusts_the_stamp() -> None:
    """A stamped section at end of file is authoritative."""
    text = (
        "# chat\n\n## Turn 1\n\nhi\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[0]:].startswith("<!-- ciao:session-insights -->")


def test_append_section_stamps_and_is_detected(tmp_path: Path) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n\n## Turn 1\n\nhi\n", encoding="utf-8")

    insights._append_section(archive, "## Errors\n- x")

    text = archive.read_text(encoding="utf-8")
    assert "<!-- ciao:session-insights -->\n## Session insights" in text
    assert insights._has_insights_section(archive)


def test_has_insights_section_ignores_quoted_marker(tmp_path: Path) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text(
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Turn 2\n\nbye\n",
        encoding="utf-8",
    )
    assert not insights._has_insights_section(archive)


def test_locate_rejects_stamp_quoted_inside_code_fence() -> None:
    """A stamped section pasted into a chat turn is fenced, hence quoted.

    Rendered archives fence quoted transcript text; without the fence check
    the stamp fast path would trust the quoted copy and skip extraction.
    """
    text = (
        "# chat\n\n## Turn 1\n\nlook at this archive:\n\n"
        "```text\n<!-- ciao:session-insights -->\n## Session insights\n\n"
        "## Decisions\n- old reviewed bullet\n```\n\n"
        "## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_ignores_stamp_mentioned_in_prose() -> None:
    """A stamp in prose, not adjacent to a header, never binds to one."""
    text = (
        "# chat\n\n## Turn 1\n\nthe stamp is <!-- ciao:session-insights --> ok\n\n"
        "## Turn 2\n\nbye\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert "## Errors" in text[location[1]:]
    # The real appended stamp wins, not the prose mention.
    assert text[location[0]:].startswith("<!-- ciao:session-insights -->\n## Session")


def test_locate_rejects_marker_quoted_in_last_turn() -> None:
    """A quote in the final turn is followed by trailers, not another turn."""
    text = (
        "# chat\n\n## Turn 1\n\nhi\n\n## Turn 2\n\nquoting:\n\n"
        "## Session insights\n\n## Decisions\n- old bullet\n\n"
        "### Usage\n- tokens\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_requires_stamped_marker_to_be_line_anchored() -> None:
    """A prose stamp cannot bind to a following legacy-looking heading."""
    text = (
        "# chat\n\n## Turn 1\n\nquoted stamp: "
        "<!-- ciao:session-insights -->\n## Session insights\n\n"
        "## Decisions\n- quoted\n\n## Turn 2\n\nmore chat\n"
    )
    assert insights.locate_insights_section(text) is None


def test_locate_accepts_stamped_crlf_archive() -> None:
    """Archives written on Windows retain the same marker semantics."""
    text = (
        "# chat\r\n\r\n## Turn 1\r\n\r\nhi\r\n\r\n"
        "<!-- ciao:session-insights -->\r\n## Session insights  \r\n\r\n"
        "## Errors\r\n- real\r\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_survives_unbalanced_fence_in_transcript() -> None:
    """A stray line-start ``` inside a quoted turn must not hide the section.

    Rendered archives embed turn text verbatim inside ```text fences, so a
    message containing an odd number of line-start fences (a truncated code
    block, a chat about Markdown) is routine. Prefix fence *parity* would
    flip there and make the real appended stamp read as quoted content —
    re-running extraction and appending a duplicate section on every pass.
    """
    text = (
        "# chat\n\n## Turn 1\n\n```text\nlook:\n```python\nprint('unclosed')\n```\n\n"
        "## Turn 2\n\nbye\n\n"
        "<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- real\n"
    )
    location = insights.locate_insights_section(text)
    assert location is not None
    assert text[location[1]:].lstrip().startswith("## Errors")


def test_locate_rejects_marker_quoted_before_subagents_block() -> None:
    text = (
        "# chat\n\n## Turn 1\n\nquoting:\n\n## Session insights\n\n- old\n\n"
        "## Subagents\n\n#### Turn 1\n\nsub\n"
    )
    assert insights.locate_insights_section(text) is None


# ── text-mode extraction ─────────────────────────────────────────────────


def test_extract_appends_in_text_mode_against_archive_body(tmp_path: Path) -> None:
    """``text_mode=True`` uses the rendered archive, not the JSONL, as input."""
    archive = tmp_path / "archive.md"
    archive.write_text("# Existing\n\nbody text\n", encoding="utf-8")

    seen: dict[str, object] = {}

    async def fake_text_call(body: str, model: str, **kwargs: object) -> str:
        seen["body"] = body
        return "## Decisions\n- chose A\n"

    with patch.object(insights, "_call_text_model", side_effect=fake_text_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="should-not-be-used",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
            text_mode=True,
        ))

    assert "should-not-be-used" not in str(seen.get("body", ""))
    assert "body text" in str(seen.get("body", ""))
    assert "## Session insights" in archive.read_text(encoding="utf-8")


def test_extract_text_mode_skips_when_archive_already_has_insights(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text(
        "# Existing\n\n## Session insights\n\nold\n", encoding="utf-8"
    )

    async def fake_text_call(body: str, model: str, **kwargs: object) -> str:
        return "fresh"

    with patch.object(insights, "_call_text_model", side_effect=fake_text_call):
        asyncio.run(insights.extract_and_append(
            archive_path=archive,
            filtered_jsonl="",
            config=_config(),
            model="deepseek-v4-flash:0731-cloud",
            text_mode=True,
        ))

    text = archive.read_text(encoding="utf-8")
    assert text.count("## Session insights") == 1
    assert "old" in text
    assert "fresh" not in text


def test_run_text_model_with_retry_retries_once_then_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n", encoding="utf-8")

    calls = {"count": 0}

    async def flaky_call(body: str, model: str, **kwargs: object) -> str:
        calls["count"] += 1
        raise RuntimeError("boom")

    async def no_sleep(_: float) -> None:
        return None

    with patch.object(insights, "_call_text_model", side_effect=flaky_call), \
         patch.object(insights.asyncio, "sleep", side_effect=no_sleep):
        output, error = asyncio.run(insights._run_text_model_with_retry(
            archive_path=archive,
            model="deepseek-v4-flash:0731-cloud",
        ))

    assert calls["count"] == 2
    assert output == ""
    assert "boom" in error


def test_retry_insights_for_chat_runs_text_mode_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retried chat is always text-mode and skips trajectory/proposals."""
    archive = tmp_path / "archive.md"
    archive.write_text("# chat\n\nbody\n", encoding="utf-8")

    config = _config()
    config.insights_model = "deepseek-v4-flash:0731-cloud"

    called: dict[str, object] = {}

    async def fake_extract_and_append(**kwargs: object) -> None:
        called.update(kwargs)
        archive.write_text(
            archive.read_text(encoding="utf-8")
            + "\n\n<!-- ciao:session-insights -->\n## Session insights\n\n## Errors\n- x\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(insights, "extract_and_append", fake_extract_and_append)

    ok = asyncio.run(insights.retry_insights_for_chat(
        config=config,
        archive_path=archive,
        model="",
        provider="claude",
        workspace="work",
    ))

    assert ok is True
    assert called["text_mode"] is True
    assert called["trajectories_enabled"] is False
    assert called["memory_proposals_enabled"] is False
    assert called["filtered_jsonl"] == ""


def test_an_appended_snippet_fence_does_not_hide_the_section(tmp_path):
    """A line-start fence in our own body must not make the section invisible.

    `_is_appended_tail` reads a line-start ``` after the stamp as proof the
    stamp is quoted transcript content, and its contract is that the appended
    body's fences are always indented. The "## Reusable snippets" template does
    indent them, but the model does not reliably preserve that — and one
    unindented fence hid the entire section: `locate_insights_section` returned
    None, memory proposals filed nothing, and each backfill run appended
    another copy of the same section.
    """
    archive = tmp_path / "archive.md"
    archive.write_text("# Chat\n\n## Turn 1\n\nhello\n", encoding="utf-8")

    insights._append_section(
        archive,
        "## Reusable snippets\n- rebuild the bundle:\n```sh\nnpm run build\n```\n",
    )

    text = archive.read_text(encoding="utf-8")
    assert insights.locate_insights_section(text) is not None
    assert insights._has_insights_section(archive)
    # Appended once, and a second pipeline pass must still see it rather than
    # appending a duplicate.
    assert text.count(insights._INSIGHTS_HEADER) == 1
    assert "npm run build" in text


# ---- Fact-augmented extraction (known-context block) ----------------------


def test_known_context_block_carries_regions_and_roster(tmp_path):
    from ciao import memory_tool as mt
    from ciao.insights import _known_context_block

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n", encoding="utf-8")
    mt.ensure_regions(guide)
    mt.write_region(guide, "memory", ["Prefers plain engineering notes."])
    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Ipek.md").write_text("# Ipek\n", encoding="utf-8")
    (vault / "projects" / "active" / "Wedding").mkdir(parents=True)

    block = _known_context_block(guide, vault)
    assert block.startswith("## Known context")
    assert "Prefers plain engineering notes." in block
    assert "Known people: Ipek" in block
    assert "Known projects: Wedding" in block
    # Absent inputs mean no context section at all, not an empty header.
    assert _known_context_block(None, None) == ""
    assert _known_context_block(tmp_path / "missing.md", tmp_path / "nope") == ""


def test_text_user_prompt_prepends_context():
    from ciao.insights import _text_user_prompt

    plain = _text_user_prompt("body")
    augmented = _text_user_prompt("body", "## Known context\nKnown people: A\n\n")
    assert plain.startswith("Below is a rendered")
    assert augmented.startswith("## Known context")
    assert augmented.endswith(plain)
