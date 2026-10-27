from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.execution_modes import harness_skill_overrides
from ciao.fs_sandbox import FsSandboxUnavailable
from ciao.models import AgentRequest, ImageAttachment, ResultEvent
from ciao.providers import claude
from ciao.providers.base import (
    build_claude_message_content,
    build_prompt,
)
from claude_agent_sdk import ClaudeSDKError

from ciao.providers.claude import (
    ClaudeInitializeTimeout,
    ClaudeProvider,
    _is_initialize_timeout,
    _sdk_permission_mode,
)
from ciao.providers.opencode import OpencodeProvider
from ciao.web.project_chats import ProjectChatManager


@pytest.mark.asyncio
async def test_claude_cannot_move_to_background(tmp_path: Path) -> None:
    assert await ClaudeProvider(tmp_path).move_to_background() == "unsupported"


@pytest.fixture
def claude_provider(tmp_path: Path) -> ClaudeProvider:
    return ClaudeProvider(tmp_path)


async def _sent_uuid(payload) -> str:
    """Drain a prompt payload and return the uuid the turn tagged it with."""
    items = [item async for item in payload]
    return items[-1]["uuid"]


def _prompt_echo(prompt_uuid: str):
    from claude_agent_sdk import UserMessage

    return UserMessage(content="go", uuid=prompt_uuid)


def test_build_prompt_summarizes_images(tmp_path: Path) -> None:
    request = AgentRequest(
        prompt="Inspect this.",
        model="sonnet",
        mode="normal",
        images=[
            ImageAttachment(
                path=tmp_path / "image.png",
                mime_type="image/png",
                original_filename="image.png",
                caption="look here",
            )
        ],
    )
    prompt = build_prompt(request)
    assert "[INCOMING IMAGES]" in prompt
    assert "image.png" in prompt
    assert "look here" in prompt


@pytest.mark.asyncio
async def test_claude_managed_process_receives_scoped_mcp_configuration(
    tmp_path: Path, monkeypatch,
) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    config = SimpleNamespace(
        memory_char_limit=2200,
        user_char_limit=1375,
        vault_root=tmp_path / "memory-vault",
    )
    provider = ClaudeProvider(tmp_path, config=config)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        provider="claude",
    )

    await provider._ensure_connected(request)

    options = captured["options"]
    # No Ciaobot MCP server is attached since S6; the control surface is the
    # `ciao` CLI in the shell. claude.ai connector MCPs stay loaded (they are
    # fetched from the login, not declared in mcp_servers).
    assert options.mcp_servers is None or options.mcp_servers == {}
    # The CLI surface carries the compact shared core (the single CLI-first
    # prompt), not a second memory block or a provider-specific recipe.
    assert "Prefer the managed Ciaobot MCP tools" not in options.system_prompt["append"]
    assert "ciao vault search" in options.system_prompt["append"]
    # No `ciao …` argv prefix is pre-approved (an allow prefix is a shell-suffix
    # bypass risk); auto mode keeps a card on every shell command, and users who
    # want no cards switch to bypass.
    assert not [e for e in (options.allowed_tools or []) if e.startswith("Bash(ciao ")]
    assert "mcp__ciaobot__" not in " ".join(options.allowed_tools or [])
    # The bundled schedule/loop skills are removed from the model's context
    # (not merely denied): a denied-but-listed skill still gets picked, which
    # is how "create a loop" ended up as a cloud-routine Skill call.
    settings = json.loads(options.settings)
    # Derived from HARNESS_DISABLED_SKILLS, not hardcoded: the list grows as
    # bundled skills are evaluated, and this assertion is about the wiring.
    assert settings["skillOverrides"] == harness_skill_overrides()
    assert settings["skillOverrides"]["schedule"] == "off"
    assert settings["skillOverrides"]["loop"] == "off"
    # Native background Bash and Monitor run unchanged; the one Bash hook only
    # denies detached launches (nohup, &, setsid, disown).
    bash_hooks = options.hooks["PreToolUse"]
    matchers = {hook.matcher for hook in bash_hooks}
    assert matchers == {"Bash"}


@pytest.mark.asyncio
async def test_claude_live_mcp_status_is_normalized_and_excludes_secrets(
    claude_provider: ClaudeProvider,
) -> None:
    class FakeClient:
        async def get_mcp_status(self):
            return {
                "mcpServers": [
                    {"name": "notion", "status": "connected", "tools": [{"name": "search"}]},
                    {"name": "private", "status": "needs-auth", "error": "credential=secret"},
                ]
            }

    claude_provider._client = FakeClient()
    claude_provider._connected = True

    assert await claude_provider.read_mcp_status() == {
        "notion": "connected", "private": "needs_auth"
    }


@pytest.mark.asyncio
async def test_claude_mcp_status_timeout_becomes_unknown_in_runtime_status():
    class Claude:
        _connected = True

        async def read_mcp_status(self):
            raise TimeoutError("sensitive timeout detail")

    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._chats = {"chat": SimpleNamespace(provider="claude")}
    manager._providers = {"chat": SimpleNamespace(_provider=Claude())}
    manager._workspace_for_chat = lambda _chat_id: "work"

    rows = await manager.provider_mcp_runtime_connections("work", {"claude": ["notion"]})

    assert rows == {
        "claude": {
            "notion": [{
                "state": "unknown", "provider": "claude", "chat_id": "chat",
                "error": "Could not read provider MCP status.",
            }]
        }
    }


@pytest.mark.asyncio
async def test_claude_does_not_duplicate_native_guide_memory(
    tmp_path: Path, monkeypatch,
) -> None:
    """Claude's native guide loader is the sole memory source for chat turns."""
    from ciao.memory_tool import ensure_regions, write_region

    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    guide = tmp_path / "CLAUDE.md"
    ensure_regions(guide)
    write_region(guide, "memory", ["Prefer pytest for regressions"])
    write_region(guide, "profile", ["User prefers concise replies"])

    config = SimpleNamespace(
        memory_char_limit=2200,
        user_char_limit=1375,
        vault_root=tmp_path / "memory-vault",
    )
    provider = ClaudeProvider(tmp_path, config=config)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        provider="claude",
    )

    await provider._ensure_connected(request)

    append = captured["options"].system_prompt["append"]
    assert "Prefer pytest for regressions" not in append
    assert "User prefers concise replies" not in append
    assert "native workspace guide" in append
    assert captured["options"].system_prompt.get("exclude_dynamic_sections") is True


@pytest.mark.asyncio
async def test_plan_mode_gets_no_control_plane_allowlist(tmp_path: Path, monkeypatch) -> None:
    """Plan mode's contract is "propose, don't act"; an allow rule would
    punch a hole in it, so the CLI allowlist is withheld there."""
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    config = SimpleNamespace(
        memory_char_limit=2200,
        user_char_limit=1375,
        vault_root=tmp_path / "memory-vault",
    )
    provider = ClaudeProvider(tmp_path, config=config)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="plan",
        provider="claude",
    )

    await provider._ensure_connected(request)

    assert captured["options"].allowed_tools == []


def test_route_cli_stderr_demotes_known_benign_lines(caplog) -> None:
    """The CLI fires a final hook callback at end-of-turn that races with
    transport teardown; the resulting `Error in hook callback ... Stream
    closed` lines pollute logs but are harmless. Real failures must still
    surface at WARNING."""
    import logging
    from ciao.providers.claude import _route_cli_stderr

    benign_lines = [
        "Error in hook callback hook_0: 9212 | something",
        "error: Stream closed",
        "      at sendRequest (/$bunfs/root/src/entrypoints/cli.js:9217:133)",
        "      at <anonymous> (/$bunfs/root/src/entrypoints/cli.js:9217:2290)",
    ]
    real_failure = "panic: connect ECONNREFUSED 127.0.0.1:443"

    with caplog.at_level(logging.DEBUG, logger="ciao.providers.claude"):
        for line in benign_lines:
            _route_cli_stderr(line)
        _route_cli_stderr(real_failure)
        # Empty / whitespace lines must be ignored entirely so log volume
        # doesn't track stderr buffering quirks.
        _route_cli_stderr("")
        _route_cli_stderr("   \n")

    benign_records = [r for r in caplog.records if "benign" in r.message]
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(benign_records) == len(benign_lines), (
        f"Expected {len(benign_lines)} benign lines, got {len(benign_records)}"
    )
    assert len(warning_records) == 1
    assert real_failure in warning_records[0].message
    assert all(r.levelname == "DEBUG" for r in benign_records)


def test_stderr_handler_flags_fork_resume_on_background_agent_conflict(
    claude_provider: ClaudeProvider,
) -> None:
    """When the CLI refuses to resume a session held by a background agent,
    the stderr line must flip ``_fork_resume_next`` so the retry path forks
    instead of abandoning the conversation."""
    assert claude_provider._fork_resume_next is False

    claude_provider._stderr_handler("some unrelated noise")
    assert claude_provider._fork_resume_next is False

    claude_provider._stderr_handler(
        "Error: Session 82a747bd-6df5-4dc8-80be-4168228026db is currently "
        "running as a background agent (bg). Use `claude agents` to find "
        "and attach to it, or add --fork-session to branch off a copy."
    )
    assert claude_provider._fork_resume_next is True


def test_summarize_task_notification_extracts_completion_line() -> None:
    """The CLI's <task-notification> envelope must collapse to a one-line
    system bubble so subagent completions stay visible without leaking the
    raw XML into chat history.
    """
    from ciao.web.transcript_service import _summarize_task_notification

    envelope = (
        "<task-notification>\n"
        "<task-id>a7b9ecacf3a281869</task-id>\n"
        "<tool-use-id>toolu_016N18MKh4oq2TMMmGmQJ5RD</tool-use-id>\n"
        "<output-file>/tmp/claude-501/sess/tasks/a7b9ecacf3a281869.output</output-file>\n"
        "<status>completed</status>\n"
        '<summary>Agent "Enrich SparkScan report" completed\n'
        "Done. Summary:\n"
        "- Customers extracted: 32 unique names</summary>\n"
        "</task-notification>"
    )
    summary = _summarize_task_notification(envelope)
    assert summary is not None
    assert summary.startswith("\U0001F916 ")  # 🤖
    assert 'Agent "Enrich SparkScan report" completed' in summary
    # Long body must NOT bleed into the one-liner.
    assert "Customers extracted" not in summary

    # Non-completion summary (failure / stop): fall back to status + first line.
    failed = (
        "<task-notification>\n"
        "<task-id>x</task-id>\n"
        "<status>failed</status>\n"
        "<summary>Hit timeout after 600s</summary>\n"
        "</task-notification>"
    )
    fail_summary = _summarize_task_notification(failed)
    assert fail_summary is not None
    assert "failed" in fail_summary
    assert "Hit timeout" in fail_summary

    # Plain user text must be left untouched.
    assert _summarize_task_notification("hello world") is None


def test_summarize_task_notification_passes_through_finished_phrasing() -> None:
    """The subagent's own sign-off text varies ("completed", "finished", ...)
    since it's the model's own final message, not a fixed CLI string. Any of
    them must pass through as-is instead of doubling up with the generic
    "Subagent {status}: ..." wrapper (e.g. "Subagent completed: Agent "X"
    finished").
    """
    from ciao.web.transcript_service import _summarize_task_notification

    envelope = (
        "<task-notification>\n"
        "<task-id>a7b9ecacf3a281869</task-id>\n"
        "<status>completed</status>\n"
        '<summary>Agent "Curate recent chats and proposals" finished</summary>\n'
        "</task-notification>"
    )
    summary = _summarize_task_notification(envelope)
    assert summary == '\U0001F916 Agent "Curate recent chats and proposals" finished'
    assert "Subagent completed:" not in summary


def test_is_cli_internal_envelope_matches_known_wrappers() -> None:
    """Bash output, command echoes, and other CLI-synthesized user-role
    messages should be recognized so chat_messages can drop them on replay.
    """
    from ciao.web.transcript_service import _is_cli_internal_envelope

    cli_payloads = [
        "<task-notification><status>completed</status></task-notification>",
        "<bash-stdout>hello\n</bash-stdout>",
        "<bash-stderr>err</bash-stderr>",
        "<command-name>/model</command-name><command-args>opus</command-args>",
        "<local-command-stdout>x</local-command-stdout>",
        "  \n<teammate-message>hi</teammate-message>",  # leading whitespace ok
    ]
    for payload in cli_payloads:
        assert _is_cli_internal_envelope(payload), payload

    # Real user prose with an angle bracket must not be misclassified.
    assert not _is_cli_internal_envelope("Is X < Y true?")
    assert not _is_cli_internal_envelope("hello")
    assert not _is_cli_internal_envelope("<p>html-like text but not a CLI tag</p>")


def test_strip_injected_context_removes_image_manifest(tmp_path: Path) -> None:
    """The UI replay path must undo the manifest `build_prompt` appends."""
    from ciao.web.transcript_service import _strip_injected_context

    request = AgentRequest(
        prompt="First line.\nSecond line.",
        model="sonnet",
        mode="normal",
        images=[
            ImageAttachment(
                path=tmp_path / "a.png",
                mime_type="image/png",
                original_filename="a.png",
                caption="look here",
            ),
            ImageAttachment(
                path=tmp_path / "b.png",
                mime_type="image/png",
                original_filename="b.png",
                caption=None,
            ),
        ],
    )
    prompt = build_prompt(request)
    assert "[INCOMING IMAGES]" in prompt

    # Plain trailer (no CIAO context prefix)
    assert _strip_injected_context(prompt) == "First line.\nSecond line."

    # With injected CIAO context block
    with_prefix = (
        "[CIAO_CONTEXT_BEGIN]\n"
        '[Project: "Ciaobot Improvements"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        + prompt
    )
    assert _strip_injected_context(with_prefix) == "First line.\nSecond line."


def test_build_claude_message_content_embeds_native_images(tmp_path: Path) -> None:
    image_path = tmp_path / "image.png"
    image_path.write_bytes(b"png-bytes")
    request = AgentRequest(
        prompt="Check this.",
        model="sonnet",
        mode="normal",
        images=[
            ImageAttachment(
                path=image_path,
                mime_type="image/png",
                original_filename="image.png",
            )
        ],
    )

    content = build_claude_message_content(request)

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"]


def test_claude_convert_stream_event_text_delta(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import StreamEvent as SDKStreamEvent

    sdk_event = SDKStreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "hello"},
        },
    )
    events = claude_provider._convert_stream_event(sdk_event)
    assert len(events) == 1
    assert events[0].text == "hello"


def test_claude_convert_stream_event_tool_use_start(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import StreamEvent as SDKStreamEvent

    sdk_event = SDKStreamEvent(
        uuid="u2",
        session_id="s1",
        event={
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "name": "Read",
                "id": "toolu_abc",
            },
        },
    )
    events = claude_provider._convert_stream_event(sdk_event)
    assert len(events) == 1
    assert events[0].tool_name == "Read"
    assert events[0].tool_use_id == "toolu_abc"
    # No parent_tool_use_id on the SDK event → no attribution.
    assert events[0].parent_tool_use_id is None


def test_claude_convert_stream_event_threads_parent_tool_use_id(
    claude_provider: ClaudeProvider,
) -> None:
    """When the SDK marks a stream event as coming from a Task subagent,
    the converter must thread parent_tool_use_id through to text deltas,
    tool_use starts, and thinking events. Without this the trace can't tell
    parent prose from subagent prose, which was the leak caught in
    the Explore-agent demo run."""
    from claude_agent_sdk import StreamEvent as SDKStreamEvent

    parent_id = "toolu_parent_42"

    text_event = SDKStreamEvent(
        uuid="u3",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "from sub"},
        },
        parent_tool_use_id=parent_id,
    )
    [text] = claude_provider._convert_stream_event(text_event)
    assert text.text == "from sub"
    assert text.parent_tool_use_id == parent_id

    tool_event = SDKStreamEvent(
        uuid="u4",
        session_id="s1",
        event={
            "type": "content_block_start",
            "content_block": {
                "type": "tool_use",
                "name": "Bash",
                "id": "toolu_child_99",
            },
        },
        parent_tool_use_id=parent_id,
    )
    [tool] = claude_provider._convert_stream_event(tool_event)
    assert tool.tool_name == "Bash"
    assert tool.tool_use_id == "toolu_child_99"
    assert tool.parent_tool_use_id == parent_id

    thinking_event = SDKStreamEvent(
        uuid="u5",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hmm"},
        },
        parent_tool_use_id=parent_id,
    )
    [thinking] = claude_provider._convert_stream_event(thinking_event)
    assert thinking.text == "hmm"
    assert thinking.parent_tool_use_id == parent_id


def test_claude_stream_usage_uses_latest_input_not_cumulative(
    claude_provider: ClaudeProvider,
) -> None:
    """Each message_start reports the total input tokens for that API call,
    not a delta. Using the latest value keeps the live PWA counter near the
    real context size; summing across a tool loop inflates it misleadingly."""
    from claude_agent_sdk import StreamEvent as SDKStreamEvent

    start1 = SDKStreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "message_start",
            "message": {"usage": {"input_tokens": 500_000, "output_tokens": 0}},
        },
    )
    [usage1] = claude_provider._convert_stream_event(start1)
    assert usage1.input_tokens == 500_000
    assert usage1.output_tokens == 0

    delta1 = SDKStreamEvent(
        uuid="u2",
        session_id="s1",
        event={"type": "message_delta", "usage": {"output_tokens": 1_000}},
    )
    [usage2] = claude_provider._convert_stream_event(delta1)
    assert usage2.input_tokens == 500_000
    assert usage2.output_tokens == 1_000

    # A second assistant message in the same tool loop has a slightly larger
    # context. The input counter must reflect the latest context size, not the
    # cumulative 1,010,000 tokens.
    start2 = SDKStreamEvent(
        uuid="u3",
        session_id="s1",
        event={
            "type": "message_start",
            "message": {"usage": {"input_tokens": 510_000, "output_tokens": 0}},
        },
    )
    [usage3] = claude_provider._convert_stream_event(start2)
    assert usage3.input_tokens == 510_000
    assert usage3.output_tokens == 1_000  # previously committed output


def test_claude_rate_limit_event_suppresses_status_and_caches_quota(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo, ResultMessage

    # An "allowed_warning" tick is usage telemetry, not conversation: it emits
    # no chat-facing status event, but its quota is still cached for the
    # Settings rate-limit card and the ResultMessage payload.
    events = claude_provider._convert_message(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed_warning",
                rate_limit_type="five_hour",
                resets_at=123,
                utilization=0.91,
            ),
            uuid="uuid-1",
            session_id="sess-1",
        )
    )

    assert events == []

    result_events = claude_provider._convert_message(
        ResultMessage(
            subtype="result",
            duration_ms=1000,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="sess-1",
            result="All done.",
        )
    )

    assert result_events[0].quota["status"] == "allowed_warning"
    assert result_events[0].quota["rateLimitType"] == "five_hour"

    # If the status is just "allowed", it should not emit a SystemStatusEvent.
    no_events = claude_provider._convert_message(
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="allowed",
                rate_limit_type="five_hour",
                resets_at=123,
                utilization=0.1,
            ),
            uuid="uuid-2",
            session_id="sess-1",
        )
    )
    assert len(no_events) == 0


def test_claude_convert_result_message(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import ResultMessage

    msg = ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=1,
        session_id="sess-42",
        result="All done.",
    )
    events = claude_provider._convert_message(msg)
    assert len(events) == 1
    result = events[0]
    assert result.result == "All done."
    assert result.session_id == "sess-42"
    assert result.is_error is False


def test_claude_result_carries_the_follow_up_origin(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import ResultMessage

    def convert(origin: dict[str, str] | None) -> str:
        msg = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            origin=origin,
        )
        return claude_provider._convert_message(msg)[0].origin

    assert convert({"kind": "task-notification", "producer": "session-task"}) == (
        "task-notification"
    )
    assert convert(None) == ""
    assert convert({"kind": "human"}) == ""


def test_claude_error_result_gets_host_annotation(
    claude_provider: ClaudeProvider,
) -> None:
    """A hostless connection error gains the endpoint host and a DNS
    category, so a failed schedule names what failed and how (#162, #178)."""
    from claude_agent_sdk import ResultMessage

    claude_provider._api_host = "api.anthropic.com"
    msg = ResultMessage(
        subtype="result",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=True,
        num_turns=1,
        session_id="sess-enotfound",
        result="API Error: Unable to connect to API (ENOTFOUND)",
    )
    result = claude_provider._convert_message(msg)[0]
    assert result.is_error is True
    assert "host: api.anthropic.com" in result.result
    assert "category: dns" in result.result


def test_claude_error_result_annotation_is_selective(
    claude_provider: ClaudeProvider,
) -> None:
    """Non-connection errors and success results pass through untouched (#162)."""
    from claude_agent_sdk import ResultMessage

    from ciao.providers.claude import _annotate_connection_host

    claude_provider._api_host = "api.anthropic.com"
    # A non-connection error is not annotated.
    other = claude_provider._convert_message(
        ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="s",
            result="Model refused the request.",
        )
    )[0]
    assert "host:" not in other.result
    assert "category:" not in other.result

    # Already naming the host: the host is not duplicated, but the category
    # is still added so the operator can tell DNS from refused/auth (#178).
    assert (
        _annotate_connection_host(
            "getaddrinfo ENOTFOUND api.anthropic.com", "api.anthropic.com"
        )
        == "getaddrinfo ENOTFOUND api.anthropic.com (category: dns)"
    )
    # Idempotent: a second pass over an already-annotated string is a no-op.
    annotated = _annotate_connection_host(
        "API Error: Unable to connect to API (ENOTFOUND)", "api.anthropic.com"
    )
    assert _annotate_connection_host(annotated, "api.anthropic.com") == annotated


def test_claude_empty_result_preserves_sdk_errors(
    claude_provider: ClaudeProvider,
) -> None:
    """An error ResultMessage with no ``result`` keeps its SDK diagnostics.

    The CLI reports some failures (subtype ``error_during_execution``) with an
    empty ``result`` and the actual messages in ``errors``. Dropping those left
    the turn looking like a blank error with nothing for the user to act on.
    """
    from claude_agent_sdk import ResultMessage

    msg = ResultMessage(
        subtype="error_during_execution",
        duration_ms=10,
        duration_api_ms=10,
        is_error=True,
        num_turns=1,
        session_id="sess-errors",
        result="",
        errors=["boom one", "", "boom two"],
    )
    result = claude_provider._convert_message(msg)[0]
    assert result.is_error is True
    # Nonempty strings are joined, blank entries dropped.
    assert result.result == "boom one\nboom two"


def test_claude_interrupt_empty_error_remains_empty(
    claude_provider: ClaudeProvider,
) -> None:
    """A bare interrupt stays empty in the adapter: cancellation is inferred
    at the streaming boundary, never here."""
    from claude_agent_sdk import ResultMessage

    for errors in ([], None):
        msg = ResultMessage(
            subtype="error_during_execution",
            duration_ms=10,
            duration_api_ms=10,
            is_error=True,
            num_turns=1,
            session_id="sess-interrupt",
            result="",
            errors=errors,
            terminal_reason="aborted_streaming",
        )
        result = claude_provider._convert_message(msg)[0]
        assert result.is_error is True
        assert result.result == ""


@pytest.mark.asyncio
async def test_prompt_payload_is_async_iterable_without_images(
    claude_provider: ClaudeProvider,
) -> None:
    """The SDK's can_use_tool gate requires AsyncIterable prompts at connect
    time (claude_agent_sdk>=0.1.63). A bare string triggers:
    'can_use_tool callback requires streaming mode'. Text-only requests must
    still produce a streaming payload.
    """
    from collections.abc import AsyncIterable

    request = AgentRequest(prompt="hello", model="sonnet", mode="normal")
    payload = claude_provider._prompt_payload(request, "prompt-1")
    assert isinstance(payload, AsyncIterable)

    messages = [msg async for msg in payload]
    assert len(messages) == 1
    assert messages[0]["uuid"] == "prompt-1"
    assert messages[0]["message"]["content"] == "hello"


def test_sdk_permission_mode_mapping() -> None:
    """Bridge modes map to the real SDK permission modes.

    ``"auto"`` must map to the SDK's own ``"auto"`` (classifier mode), NOT to
    ``"bypassPermissions"`` — that's the whole point of the Auto label in the
    UI. Regression guard for a prior alias bug.
    """
    assert _sdk_permission_mode("normal") == "default"
    assert _sdk_permission_mode("plan") == "plan"
    assert _sdk_permission_mode("auto") == "auto"
    assert _sdk_permission_mode("bypass") == "bypassPermissions"


@pytest.mark.asyncio
async def test_claude_run_streaming_raises_on_resume_failure(
    claude_provider: ClaudeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume failures re-raise instead of silently starting a fresh session."""
    from claude_agent_sdk import CLIConnectionError

    async def fake_run_once(request: AgentRequest, _register_handle):
        raise CLIConnectionError("stale session")
        yield  # make it an async generator  # noqa: RUF027

    monkeypatch.setattr(claude_provider, "_run_streaming_once", fake_run_once)

    request = AgentRequest(
        prompt="hello",
        model="sonnet",
        mode="normal",
        resume_session="sess-old",
    )
    with pytest.raises(CLIConnectionError):
        _ = [event async for event in claude_provider.run_streaming(request, lambda _handle: None)]


@pytest.mark.asyncio
async def test_claude_run_streaming_surfaces_error_when_no_result(
    claude_provider: ClaudeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn whose ``receive_response()`` ends without a ResultMessage (the
    CLI stream closing mid-turn, e.g. a server restart) must surface an
    explicit error ResultEvent instead of stopping silently, and preserve the
    session id so the next turn can resume."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from ciao.models import ResultEvent, ToolUseEvent
    from ciao.providers.claude import _TURN_INTERRUPTED_MESSAGE

    class FakeClient:
        async def connect(self, _payload):
            return None

        async def query(self, payload):
            self.prompt_uuid = await _sent_uuid(payload)

        async def receive_response(self):
            yield _prompt_echo(self.prompt_uuid)
            # One tool call, then the stream closes with NO ResultMessage —
            # exactly what a mid-turn subprocess kill looks like.
            yield AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
                model="opus",
                session_id="sess-mid",
            )

        async def get_context_usage(self):
            return {}

    fake = FakeClient()

    async def fake_ensure(_request):
        claude_provider._connected = True
        claude_provider._client = fake
        claude_provider._session_id = "sess-mid"
        return fake

    monkeypatch.setattr(claude_provider, "_ensure_connected", fake_ensure)

    request = AgentRequest(prompt="continue", model="opus", mode="normal")
    events = [
        event
        async for event in claude_provider.run_streaming(request, lambda _handle: None)
    ]

    # The tool call still streams through before the interruption.
    assert any(isinstance(e, ToolUseEvent) for e in events)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].result == _TURN_INTERRUPTED_MESSAGE
    assert results[0].session_id == "sess-mid"


@pytest.mark.asyncio
async def test_claude_run_streaming_recovers_from_oversized_message(
    claude_provider: ClaudeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single JSON message exceeding the SDK decode buffer raises a fatal
    reader error that would kill the chat stream (issue #137). The provider
    must catch it, stream the earlier events, and surface a recoverable error
    ResultEvent (session id preserved) instead of propagating the crash."""
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from ciao.models import ResultEvent, ToolUseEvent
    from ciao.providers.claude import _OVERSIZED_MESSAGE

    class FakeClient:
        def __init__(self) -> None:
            self.disconnected = False

        async def connect(self, _payload):
            return None

        async def query(self, payload):
            self.prompt_uuid = await _sent_uuid(payload)

        async def receive_response(self):
            yield _prompt_echo(self.prompt_uuid)
            # One tool call streams fine, then the reader raises the SDK's
            # fatal decode error — exactly the shape observed in #137.
            yield AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})],
                model="opus",
                session_id="sess-big",
            )
            raise Exception(
                "Failed to decode JSON: JSON message exceeded maximum buffer "
                "size of 1048576 bytes..."
            )

        async def disconnect(self):
            self.disconnected = True

        async def get_context_usage(self):
            return {}

    fake = FakeClient()

    async def fake_ensure(_request):
        claude_provider._connected = True
        claude_provider._client = fake
        claude_provider._session_id = "sess-big"
        return fake

    monkeypatch.setattr(claude_provider, "_ensure_connected", fake_ensure)

    request = AgentRequest(prompt="do a big thing", model="opus", mode="normal")
    # Must not raise — the fatal reader error becomes a recoverable result.
    events = [
        event
        async for event in claude_provider.run_streaming(request, lambda _handle: None)
    ]

    # Earlier tool activity still streamed through before the failure.
    assert any(isinstance(e, ToolUseEvent) for e in events)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].result == _OVERSIZED_MESSAGE
    assert results[0].session_id == "sess-big"
    assert fake.disconnected is True


def test_claude_options_raise_sdk_buffer_above_default() -> None:
    """max_buffer_size must be passed to the SDK well above its 1 MiB default
    so legitimately large messages don't abort the turn (issue #137)."""
    from ciao.providers.claude import _SDK_MAX_BUFFER_BYTES

    assert _SDK_MAX_BUFFER_BYTES > 1024 * 1024


def test_claude_convert_system_message_suppresses_allowed_rate_limit(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import SystemMessage

    events1 = claude_provider._convert_message(
        SystemMessage(
            subtype="status",
            data={"status": "Rate limit: allowed (five_hour)"},
        )
    )
    assert len(events1) == 0

    events2 = claude_provider._convert_message(
        SystemMessage(
            subtype="status",
            data={"status": "Rate limit: allowed_warning (five_hour) 90% used"},
        )
    )
    assert len(events2) == 0

    # A non-allowed rate-limit state still surfaces so the user is told.
    events3 = claude_provider._convert_message(
        SystemMessage(
            subtype="status",
            data={"status": "Rate limit exceeded (five_hour)"},
        )
    )
    assert len(events3) == 1
    assert events3[0].status == "Rate limit exceeded (five_hour)"


def test_claude_compact_boundary_reports_the_token_change(
    claude_provider: ClaudeProvider,
) -> None:
    from claude_agent_sdk import SystemMessage

    events = claude_provider._convert_message(
        SystemMessage(
            subtype="compact_boundary",
            data={
                "compact_metadata": {
                    "trigger": "manual",
                    "pre_tokens": 16000,
                    "post_tokens": 2000,
                }
            },
        )
    )
    assert [event.status for event in events] == [
        "Compacted context: 16,000 → 2,000 tokens"
    ]


@pytest.mark.asyncio
async def test_claude_compact_sends_only_the_slash_command(
    claude_provider: ClaudeProvider,
) -> None:
    from ciao.models import AgentRequest

    # A capsule ahead of "/compact" makes the SDK answer in text instead of
    # compacting, so the compact prompt must go out bare.
    request = AgentRequest(
        prompt="[CIAO_CONTEXT_BEGIN]\ncapsule\n[CIAO_CONTEXT_END]\n\n/compact",
        model="opus",
        mode="normal",
        compact=True,
        stable_context_prefix="capsule",
    )
    messages = [
        message async for message in claude_provider._prompt_payload(request, "prompt-1")
    ]
    assert [message["message"]["content"] for message in messages] == ["/compact"]
    assert [message["uuid"] for message in messages] == ["prompt-1"]


@pytest.mark.parametrize("mode", ["normal", "plan"])
@pytest.mark.asyncio
async def test_manual_and_plan_modes_pre_approve_nothing(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    """"Manual — ask for every action" has to mean it.

    The allowlist was applied for every mode except `plan`, so Manual silently
    auto-approved `memory_update`, `chat_send` and `schedule`. `schedule` is an
    escalation rather than just a write: an automation run is dispatched
    `unattended`, which forces `bypass`, so a one-minute interval created
    without an approval card buys unprompted arbitrary tool use every minute —
    the exact thing an operator picks this mode to prevent.
    """
    captured: dict = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    config = SimpleNamespace(
        memory_char_limit=2200,
        user_char_limit=1375,
        vault_root=tmp_path / "memory-vault",
    )
    provider = ClaudeProvider(tmp_path, config=config)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode=mode,
        provider="claude",
    )

    await provider._ensure_connected(request)

    allowed = captured["options"].allowed_tools or []
    assert "Bash(ciao schedule create:*)" not in allowed
    assert "Bash(ciao memory update:*)" not in allowed
    assert "Bash(ciao chat send:*)" not in allowed


@pytest.mark.parametrize("mode", ["normal", "plan"])
def test_opencode_manual_and_plan_modes_add_no_allow_rules(mode: str) -> None:
    """The same carve-out on the other provider."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings(mode)
    resources = " ".join(str(rule.get("resource") or "") for rule in rules)
    assert "ciao schedule create" not in resources
    assert "ciao memory update" not in resources


def test_opencode_auto_mode_keeps_every_ciao_command_gated() -> None:
    """Auto mode pre-approves no `ciao …` argv prefix (a shell-suffix bypass
    risk); every shell command, including `ciao …`, keeps a card."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings("auto")
    shell = [r for r in rules if r.get("action") == "shell"]
    assert not any(str(r.get("resource") or "").startswith("ciao ") for r in shell)
    assert shell[0] == {"action": "shell", "resource": "*", "effect": "ask"}


@pytest.mark.asyncio
async def test_drain_marks_cli_disconnected_on_transport_error(
    claude_provider: ClaudeProvider,
) -> None:
    """A dead CLI transport must flip cli_connected, not just end the drain."""
    from claude_agent_sdk import CLIConnectionError

    class DeadClient:
        async def receive_messages(self):
            raise CLIConnectionError("process died")
            yield  # pragma: no cover

    claude_provider._client = DeadClient()
    claude_provider._connected = True
    assert claude_provider.cli_connected is True

    async for _ in claude_provider.drain_events():
        pass

    assert claude_provider.cli_connected is False
    assert claude_provider._client is None


@pytest.mark.asyncio
async def test_drain_keeps_connection_on_unhandled_result(
    claude_provider: ClaudeProvider,
) -> None:
    """An odd SDK result shape ends the drain but the client is still usable."""
    from ciao.models import StreamEvent

    class OddResultClient:
        async def receive_messages(self):
            raise Exception("unhandled result shape")
            yield  # pragma: no cover

    provider = claude_provider
    provider._client = OddResultClient()
    provider._connected = True

    events = []
    async for event in provider.drain_events():
        events.append(event)

    assert events == []
    assert provider.cli_connected is True
    assert provider._client is not None
    _ = StreamEvent  # imported shape parity with real drain payloads


@pytest.mark.asyncio
async def test_drain_keeps_connection_on_message_parse_error(
    claude_provider: ClaudeProvider,
) -> None:
    """A bad payload from a still-alive CLI must not kill the connection."""
    from claude_agent_sdk._errors import MessageParseError

    class ParseErrorClient:
        async def receive_messages(self):
            raise MessageParseError("bad payload", data=None)
            yield  # pragma: no cover

    provider = claude_provider
    provider._client = ParseErrorClient()
    provider._connected = True

    events = []
    async for event in provider.drain_events():
        events.append(event)

    assert events == []
    assert provider.cli_connected is True
    assert provider._client is not None


def test_extract_effective_model_prefers_the_model_that_did_the_work() -> None:
    # The CLI's helper calls on its small model can be listed first.
    msg = SimpleNamespace(
        model_usage={
            "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 40},
            "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 533},
        }
    )
    assert ClaudeProvider._extract_effective_model(msg) == "claude-opus-5-5"


def test_extract_effective_model_keeps_first_entry_without_token_counts() -> None:
    msg = SimpleNamespace(model_usage={"claude-sonnet-5": {}, "claude-haiku-4-5": {}})
    assert ClaudeProvider._extract_effective_model(msg) == "claude-sonnet-5"
    assert ClaudeProvider._extract_effective_model(SimpleNamespace(model_usage=None)) == ""


def test_turn_model_is_the_main_call_not_a_helper_with_more_output() -> None:
    # Issue #1224: a one-line sonnet reply lost to a haiku helper call that
    # produced more output tokens, so the footer named haiku.
    result = SimpleNamespace(
        model_usage={
            "claude-sonnet-5-5": {"outputTokens": 6},
            "claude-haiku-5-5": {"outputTokens": 40},
        }
    )
    main = SimpleNamespace(model="claude-sonnet-5-5")
    assert ClaudeProvider._extract_effective_model(result) == "claude-haiku-5-5"
    assert ClaudeProvider._turn_effective_model(result, main) == "claude-sonnet-5-5"


def test_turn_model_falls_back_to_model_usage_without_a_main_call() -> None:
    result = SimpleNamespace(
        model_usage={"claude-opus-5-5": {"outputTokens": 533}, "claude-haiku-5-5": {"outputTokens": 40}}
    )
    assert ClaudeProvider._turn_effective_model(result, None) == "claude-opus-5-5"
    assert ClaudeProvider._turn_effective_model(None, None) == ""


@pytest.mark.asyncio
async def test_claude_workspace_scope_sets_sandbox_and_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    # The sandbox is POSIX-only; Windows refuses workspace scope (its own test).
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    agent_root = tmp_path / "agent"
    vault_root = tmp_path / "vault"
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(agent_root), str(vault_root)),
    )

    await provider._ensure_connected(request)

    options = captured["options"]
    sandbox = options.sandbox
    assert sandbox["enabled"] is True
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["excludedCommands"] == []
    assert sandbox["filesystem"]["allowRead"][:2] == [str(agent_root), str(vault_root)]
    assert sandbox["filesystem"]["allowWrite"] == [str(agent_root), str(vault_root)]
    assert sandbox["filesystem"]["denyRead"] == [str(Path.home())]
    # The vault sits outside the agent root, so the CLI also gets it as an add dir.
    assert [str(path) for path in options.add_dirs] == [str(vault_root)]
    # The CLI's own file tools are outside the Bash sandbox; a hook confines them.
    matchers = options.hooks["PreToolUse"]
    guard = [m for m in matchers if "Read" in (m.matcher or "").split("|")]
    assert len(guard) == 1
    assert set(guard[0].matcher.split("|")) >= {"Read", "Write", "Edit", "Glob", "Grep"}
    assert options.settings == json.dumps({"skillOverrides": harness_skill_overrides()})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_env", "process_env", "expected"),
    [
        ({"CLAUDE_CODE_TMPDIR": "/from/extra"}, None, "/from/extra"),
        ({}, "/from/process", "/from/process"),
        ({"CLAUDE_CODE_TMPDIR": "/from/extra"}, "/from/process", "/from/extra"),
        ({}, None, None),
    ],
)
async def test_claude_workspace_scope_passes_the_cli_tmp_base_to_the_guard(
    tmp_path: Path, monkeypatch, extra_env, process_env, expected,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    if process_env is None:
        monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR", process_env)
    captured: dict = {}

    def fake_guard(**kwargs):
        captured.update(kwargs)

        async def hook(*args):
            return {}

        return hook

    class FakeClient:
        def __init__(self, options):
            pass

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("ciao.providers.claude.claude_file_tool_guard", fake_guard)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(tmp_path / "agent"),),
        extra_env=extra_env,
    )

    await provider._ensure_connected(request)

    assert captured["cli_tmp_base"] == expected


@pytest.mark.asyncio
async def test_claude_workspace_scope_keeps_vault_inside_agent_root_out_of_add_dirs(
    tmp_path: Path, monkeypatch,
) -> None:
    # The sandbox is POSIX-only; Windows refuses workspace scope (its own test).
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    agent_root = tmp_path / "agent"
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="normal",
        agent_fs_scope="workspace",
        agent_roots=(str(agent_root), str(agent_root / "memory-vault")),
    )

    await provider._ensure_connected(request)

    options = captured["options"]
    assert options.sandbox["autoAllowBashIfSandboxed"] is False
    assert options.add_dirs == []


@pytest.mark.asyncio
async def test_a_changed_root_set_rebuilds_the_claude_client(
    tmp_path: Path, monkeypatch,
) -> None:
    """The sandbox is fixed when the CLI process starts, so a turn whose roots
    differ from the live client's (a setup chat granted the data root) must
    disconnect it and build a new one with the widened sandbox."""
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = {}
    stopped: list[bool] = []

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    agent_root = tmp_path / "agent"
    data_root = tmp_path
    provider._client = object()  # type: ignore[assignment]
    provider._connected = True
    provider._fs_scope_key = ("workspace", (str(agent_root),), (), ())

    async def fake_disconnect() -> None:
        stopped.append(True)
        provider._client = None
        provider._connected = False

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(agent_root), str(data_root)),
    )

    await provider._ensure_connected(request)

    assert stopped == [True]
    assert captured["options"].sandbox["filesystem"]["allowWrite"] == [
        str(agent_root),
        str(data_root),
    ]


@pytest.mark.asyncio
async def test_an_unchanged_root_set_reuses_the_claude_client(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    stopped: list[bool] = []
    provider = ClaudeProvider(tmp_path)
    live = object()
    provider._client = live  # type: ignore[assignment]
    provider._connected = True
    # Model and mode already match the turn, so only the roots can decide.
    provider._current_model = "sonnet"
    provider._current_mode = "auto"
    agent_root = str(tmp_path / "agent")
    provider._fs_scope_key = ("workspace", (agent_root,), (), ())

    async def fake_disconnect() -> None:
        stopped.append(True)

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(agent_root,),
    )
    provider._spawn_key = provider._spawn_key_for(request)

    assert await provider._ensure_connected(request) is live
    assert stopped == []


@pytest.mark.asyncio
async def test_a_changed_denied_except_restarts_the_claude_client(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    stopped: list[bool] = []
    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", lambda options: object())
    provider._client = object()  # type: ignore[assignment]
    provider._connected = True
    provider._current_model = "sonnet"
    provider._current_mode = "auto"
    agent_root = str(tmp_path / "agent")
    provider._fs_scope_key = ("workspace", (agent_root,), (), ())

    async def fake_disconnect() -> None:
        stopped.append(True)
        provider._client = None
        provider._connected = False

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(agent_root,),
        agent_denied_except=(str(tmp_path / "agent" / "secrets" / "gws-a"),),
    )
    provider._spawn_key = provider._spawn_key_for(request)

    await provider._ensure_connected(request)

    assert stopped == [True]


@pytest.mark.asyncio
async def test_claude_machine_scope_omits_sandbox(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(prompt="test", model="sonnet", mode="auto")

    await provider._ensure_connected(request)

    options = captured["options"]
    assert options.sandbox is None
    assert options.add_dirs == []
    assert [m.matcher for m in options.hooks["PreToolUse"]] == ["Bash"]
    assert options.settings == json.dumps({"skillOverrides": harness_skill_overrides()})


@pytest.mark.asyncio
async def test_claude_workspace_scope_refuses_on_windows(
    tmp_path: Path, monkeypatch,
) -> None:
    constructed = []

    class FakeClient:
        def __init__(self, options):
            constructed.append(options)

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(sys, "platform", "win32")
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(tmp_path / "agent"),),
    )

    with pytest.raises(FsSandboxUnavailable):
        await provider._ensure_connected(request)
    assert constructed == []


@pytest.mark.asyncio
async def test_claude_scope_change_reconnects_live_process(
    tmp_path: Path, monkeypatch,
) -> None:
    # The sandbox is POSIX-only; Windows refuses workspace scope (its own test).
    monkeypatch.setattr(sys, "platform", "darwin")
    from ciao.models import provider_reuse_key

    captured = {"disconnects": 0, "constructed": 0}

    class FakeClient:
        def __init__(self, options):
            captured["constructed"] += 1
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(tmp_path / "agent"),),
    )
    # A live machine-scope process, unsandboxed, with a matching control token.
    provider._client = object()
    provider._connected = True
    provider._mcp_token = provider_reuse_key(request)
    provider._fs_scope_key = ("machine", (), (), ())

    async def fake_disconnect() -> None:
        captured["disconnects"] += 1
        provider._client = None
        provider._connected = False

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)

    await provider._ensure_connected(request)

    assert captured["disconnects"] == 1
    assert captured["constructed"] == 1
    assert captured["options"].sandbox["enabled"] is True
# -- OpenCode server filesystem scope (issue #1150) ---------------------------


class _SpawnCaptured(Exception):
    """Stops ``_start_server_once`` right after the spawn argv is recorded."""


def _opencode_scope_fixture(tmp_path: Path, scope: str):
    roots = (str(tmp_path / "agent"), str(tmp_path / "vault")) if scope == "workspace" else ()
    request = AgentRequest(
        prompt="hi",
        model="model",
        mode="auto",
        agent_fs_scope=scope,
        agent_roots=roots,
    )
    return OpencodeProvider(tmp_path), request


def _capture_spawn(monkeypatch, envs: list[dict] | None = None) -> list[tuple[str, ...]]:
    spawned: list[tuple[str, ...]] = []

    async def fake_spawn(*argv, **kwargs):
        spawned.append(argv)
        if envs is not None:
            envs.append(kwargs.get("env") or {})
        raise _SpawnCaptured

    monkeypatch.setattr("ciao.providers.opencode._spawn_server", fake_spawn)
    return spawned


async def test_opencode_workspace_scope_prefixes_sandbox_exec(tmp_path, monkeypatch):
    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    request.extra_env = {"PATH": str(tmp_path / "node-bin")}
    node_code = tmp_path / "node-code"
    paths: list[str] = []

    def native_node_roots(path):
        paths.append(path)
        return [node_code]

    monkeypatch.setattr("ciao.providers.opencode.node_tool_read_roots", native_node_roots)
    envs: list[dict] = []
    spawned = _capture_spawn(monkeypatch, envs)
    calls: list[tuple[list[Path], list[Path]]] = []
    xdg = tmp_path / "xdg"
    for name in ("DATA", "STATE", "CONFIG"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(xdg / name.lower()))
    (xdg / "config" / "opencode").mkdir(parents=True)
    binary = tmp_path / "install" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")

    def fake_prefix(*, roots, read_only=(), denied=(), regrant=()):
        calls.append((list(roots), list(read_only)))
        return ["sandbox-exec", "-p", "PROFILE"]

    monkeypatch.setattr("ciao.providers.opencode.opencode_sandbox_prefix", fake_prefix)

    with pytest.raises(_SpawnCaptured):
        await provider._start_server_once(request, str(binary))

    roots, read_only = calls[0]
    assert paths == [str(tmp_path / "node-bin")]
    assert node_code in read_only
    assert roots[:2] == [tmp_path / "agent", tmp_path / "vault"]
    sandbox_cache = Path.home() / ".cache" / "ciaobot-opencode-sandbox"
    # The server writes its data and state, and a cache no unconfined server
    # loads. Its config and install are code an unconfined server runs later,
    # so they are readable only (#1174).
    assert (xdg / "data" / "opencode").resolve() in roots
    assert (xdg / "state" / "opencode").resolve() in roots
    assert (sandbox_cache / "opencode").resolve() in roots
    assert (xdg / "config" / "opencode").resolve() in read_only
    assert (tmp_path / "install").resolve() in read_only
    assert (sandbox_cache / "npm").resolve() in roots
    assert not set(read_only) & set(roots)
    assert envs[0]["XDG_CACHE_HOME"] == str(sandbox_cache)
    assert envs[0]["npm_config_cache"] == str(sandbox_cache / "npm")
    argv = spawned[0]
    assert argv[:3] == ("sandbox-exec", "-p", "PROFILE")
    assert argv[3:6] == (str(binary), "serve", "--port")
    assert argv[7:] == ("--hostname", "127.0.0.1")


async def test_opencode_machine_scope_spawns_the_binary_directly(tmp_path, monkeypatch):
    provider, request = _opencode_scope_fixture(tmp_path, "machine")
    spawned = _capture_spawn(monkeypatch)

    def no_prefix(*, roots, read_only=()):
        raise AssertionError("machine scope must not build a sandbox prefix")

    monkeypatch.setattr("ciao.providers.opencode.opencode_sandbox_prefix", no_prefix)

    with pytest.raises(_SpawnCaptured):
        await provider._start_server_once(request, "/bin/opencode")

    argv = spawned[0]
    assert argv[0] == "/bin/opencode"
    assert argv[1:2] == ("serve",)
    assert argv[-2:] == ("--hostname", "127.0.0.1")


async def test_opencode_workspace_scope_does_not_spawn_when_the_sandbox_is_missing(
    tmp_path, monkeypatch
):
    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    spawned = _capture_spawn(monkeypatch)

    def missing_sandbox(*, roots, read_only=(), denied=(), regrant=()):
        raise FsSandboxUnavailable("no sandbox tool")

    monkeypatch.setattr("ciao.providers.opencode.opencode_sandbox_prefix", missing_sandbox)

    with pytest.raises(FsSandboxUnavailable, match="no sandbox tool"):
        await provider._start_server_once(request, "/bin/opencode")

    assert spawned == []


async def test_opencode_scope_change_restarts_the_live_server(tmp_path, monkeypatch):
    from ciao.models import provider_reuse_key

    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    live = object()
    provider._client = live
    provider._process = SimpleNamespace(returncode=None)
    provider._mcp_token = provider_reuse_key(request)
    provider._fs_scope_key = ("machine", (), (), ())
    events: list[str] = []

    async def fake_disconnect():
        events.append("disconnect")
        provider._client = None
        provider._process = None

    async def fake_start(req, binary):
        events.append("start")
        return "fresh"

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    monkeypatch.setattr(provider, "_start_server_once", fake_start)
    monkeypatch.setattr("ciao.providers.opencode.resolve_opencode_binary", lambda env: "/bin/opencode")

    assert await provider._ensure_server(request) == "fresh"
    assert events == ["disconnect", "start"]

    # Same scope and roots: the live server is reused.
    provider._client = live
    provider._process = SimpleNamespace(returncode=None)
    provider._fs_scope_key = (
        request.agent_fs_scope,
        request.agent_roots,
        request.agent_denied,
        request.agent_denied_except,
    )
    assert await provider._ensure_server(request) is live


def _live_scoped_opencode(tmp_path, monkeypatch, granted):
    """A workspace-scoped provider with a live server started with ``granted`` read-only."""
    from ciao.models import provider_reuse_key
    from ciao.providers.opencode import _read_only_snapshot

    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    request.agent_read_only = (str(granted),)
    live = object()
    provider._client = live
    provider._process = SimpleNamespace(returncode=None)
    provider._mcp_token = provider_reuse_key(request)
    provider._fs_scope_key = (
        request.agent_fs_scope,
        request.agent_roots,
        request.agent_denied,
        request.agent_denied_except,
    )
    provider._read_only_snapshot = _read_only_snapshot(request)
    events: list[str] = []

    async def fake_disconnect():
        events.append("disconnect")
        provider._client = None
        provider._process = None

    async def fake_start(req, binary):
        events.append("start")
        return "fresh"

    monkeypatch.setattr(provider, "disconnect", fake_disconnect)
    monkeypatch.setattr(provider, "_start_server_once", fake_start)
    monkeypatch.setattr("ciao.providers.opencode.resolve_opencode_binary", lambda env: "/bin/opencode")
    return provider, request, live, events


def _replace_by_rename(path: Path, text: str) -> None:
    staged = path.with_name(path.name + ".new")
    staged.write_text(text, encoding="utf-8")
    os.replace(staged, path)


async def test_opencode_linux_restarts_when_a_granted_file_is_replaced(tmp_path, monkeypatch):
    # bwrap binds the registry by inode: a rename-replaced file needs a new server (#1960).
    monkeypatch.setattr(sys, "platform", "linux")
    registry = tmp_path / "workspaces.json"
    registry.write_text("[]", encoding="utf-8")
    provider, request, live, events = _live_scoped_opencode(tmp_path, monkeypatch, registry)

    assert await provider._ensure_server(request) is live
    assert events == []

    _replace_by_rename(registry, '[{"name": "work"}]')
    assert await provider._ensure_server(request) == "fresh"
    assert events == ["disconnect", "start"]


async def test_opencode_linux_restarts_when_a_granted_file_appears(tmp_path, monkeypatch):
    # A receipt missing at launch is never bound; once written, the server restarts (#1960).
    monkeypatch.setattr(sys, "platform", "linux")
    receipt = tmp_path / "workspace-rooting.json"
    provider, request, live, events = _live_scoped_opencode(tmp_path, monkeypatch, receipt)

    assert await provider._ensure_server(request) is live
    receipt.write_text('{"status": "migrated"}', encoding="utf-8")
    assert await provider._ensure_server(request) == "fresh"
    assert events == ["disconnect", "start"]


async def test_opencode_linux_restarts_when_a_granted_file_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    registry = tmp_path / "workspaces.json"
    registry.write_text("[]", encoding="utf-8")
    provider, request, live, events = _live_scoped_opencode(tmp_path, monkeypatch, registry)

    registry.unlink()
    assert await provider._ensure_server(request) == "fresh"
    assert events == ["disconnect", "start"]


async def test_opencode_snapshot_predates_the_sandbox_binds(tmp_path, monkeypatch):
    # A receipt written while the bwrap argv is built was skipped by it, so the
    # recorded snapshot must still say "missing" and the next turn must restart.
    from ciao.providers.opencode import _read_only_snapshot

    monkeypatch.setattr(sys, "platform", "linux")
    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    receipt = tmp_path / "workspace-rooting.json"
    request.agent_read_only = (str(receipt),)
    _capture_spawn(monkeypatch)

    def racing_launch(req, binary, port):
        receipt.write_text('{"status": "migrated"}', encoding="utf-8")
        return ["opencode", "serve"], {}

    monkeypatch.setattr(provider, "_server_launch", racing_launch)
    with pytest.raises(_SpawnCaptured):
        await provider._start_server_once(request, "/bin/opencode")

    assert provider._read_only_snapshot == ((str(receipt), None),)
    assert provider._read_only_snapshot != _read_only_snapshot(request)


async def test_opencode_darwin_keeps_the_server_when_a_granted_file_is_replaced(tmp_path, monkeypatch):
    # Seatbelt grants by path, so a replaced file is already visible.
    monkeypatch.setattr(sys, "platform", "darwin")
    registry = tmp_path / "workspaces.json"
    registry.write_text("[]", encoding="utf-8")
    provider, request, live, events = _live_scoped_opencode(tmp_path, monkeypatch, registry)

    _replace_by_rename(registry, '[{"name": "work"}]')
    assert await provider._ensure_server(request) is live
    assert events == []


# -- Claude turn client decision log -----------------------------------------


def _claude_turn_log(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("claude turn:")]


@pytest.mark.asyncio
async def test_claude_turn_logs_new_first_turn_then_reused(tmp_path: Path, monkeypatch, caplog) -> None:
    constructed: list[object] = []

    class FakeClient:
        def __init__(self, options):
            constructed.append(options)

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        extra_env={"CIAO_CHAT_ID": "chat-1"},
    )

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(request)
        # The SDK client only becomes connected once the first stream starts.
        provider._connected = True
        await provider._ensure_connected(request)

    assert len(constructed) == 1
    assert _claude_turn_log(caplog) == [
        "claude turn: client=new reason=first_turn chat=chat-1",
        "claude turn: client=reused reason=none chat=chat-1",
    ]


@pytest.mark.asyncio
async def test_claude_turn_logs_reconnect_reason_on_model_change(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    constructed: list[object] = []

    class FakeClient:
        def __init__(self, options):
            constructed.append(options)

        async def set_model(self, model):
            raise ClaudeSDKError("set_model refused")

        async def disconnect(self):
            return None

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    first = AgentRequest(prompt="a", model="sonnet", mode="auto", extra_env={"CIAO_CHAT_ID": "c2"})
    await provider._ensure_connected(first)
    provider._connected = True

    second = AgentRequest(prompt="b", model="opus", mode="auto", extra_env={"CIAO_CHAT_ID": "c2"})
    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(second)

    assert len(constructed) == 2
    assert _claude_turn_log(caplog) == ["claude turn: client=new reason=model_change chat=c2"]


class _ReuseFakeClient:
    """Fake SDK client for ``_ensure_connected`` reuse decisions."""

    instances: list["_ReuseFakeClient"] = []

    def __init__(self, options=None):
        self.options = options
        self._transport = SimpleNamespace(_process=SimpleNamespace(returncode=None))
        self.mode_error: Exception | None = None
        _ReuseFakeClient.instances.append(self)

    async def set_model(self, model):
        return None

    async def set_permission_mode(self, mode):
        if self.mode_error is not None:
            raise self.mode_error

    async def disconnect(self):
        return None


def _reuse_provider(tmp_path: Path, monkeypatch) -> ClaudeProvider:
    _ReuseFakeClient.instances = []
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", _ReuseFakeClient)
    return ClaudeProvider(tmp_path)


def _reuse_request(**overrides) -> AgentRequest:
    extra_env = {"CIAO_CHAT_ID": "chat-1", **overrides.pop("extra_env", {})}
    fields = {"prompt": "t", "model": "sonnet", "mode": "normal", "extra_env": extra_env}
    fields.update(overrides)
    return AgentRequest(**fields)


@pytest.mark.asyncio
async def test_claude_exited_cli_reconnects_with_reason_dead_process(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)
    request = _reuse_request()

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(request)
        provider._connected = True
        _ReuseFakeClient.instances[0]._transport._process.returncode = 0
        await provider._ensure_connected(request)

    assert len(_ReuseFakeClient.instances) == 2
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=new reason=dead_process chat=chat-1"


@pytest.mark.asyncio
async def test_claude_live_cli_refusing_bypass_reconnects(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(_reuse_request(mode="normal"))
        provider._connected = True
        _ReuseFakeClient.instances[0].mode_error = Exception(
            "Cannot set permission mode to bypassPermissions because the session was "
            "not launched with --dangerously-skip-permissions"
        )
        await provider._ensure_connected(_reuse_request(mode="bypass"))

    assert len(_ReuseFakeClient.instances) == 2
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=new reason=mode_change chat=chat-1"


@pytest.mark.asyncio
async def test_claude_thinking_level_change_rebuilds_client(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(_reuse_request(thinking_level=""))
        provider._connected = True
        await provider._ensure_connected(_reuse_request(thinking_level="high"))

    assert len(_ReuseFakeClient.instances) == 2
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=new reason=spawn_options chat=chat-1"


@pytest.mark.asyncio
async def test_claude_extra_env_change_rebuilds_client(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(_reuse_request())
        provider._connected = True
        await provider._ensure_connected(
            _reuse_request(extra_env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
        )

    assert len(_ReuseFakeClient.instances) == 2
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=new reason=spawn_options chat=chat-1"


@pytest.mark.asyncio
async def test_claude_model_env_alone_does_not_rebuild_client(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(
            _reuse_request(model="sonnet", extra_env={"CIAO_MODEL": "sonnet"})
        )
        provider._connected = True
        await provider._ensure_connected(
            _reuse_request(model="opus", extra_env={"CIAO_MODEL": "opus"})
        )

    assert len(_ReuseFakeClient.instances) == 1
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=reused reason=none chat=chat-1"


@pytest.mark.asyncio
async def test_claude_guide_edit_rebuilds_client(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    provider = _reuse_provider(tmp_path, monkeypatch)
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("a")

    with caplog.at_level(logging.INFO, logger="ciao.providers.claude"):
        await provider._ensure_connected(_reuse_request())
        provider._connected = True
        guide.write_text("bb")
        await provider._ensure_connected(_reuse_request())

    assert len(_ReuseFakeClient.instances) == 2
    assert _claude_turn_log(caplog)[-1] == "claude turn: client=new reason=spawn_options chat=chat-1"


@pytest.mark.asyncio
async def test_drain_marks_cli_disconnected_when_stream_ends(
    claude_provider: ClaudeProvider,
) -> None:
    """EOF on the transport is the CLI exiting, so the flag must flip."""

    class EndedClient:
        async def receive_messages(self):
            return
            yield  # pragma: no cover

    claude_provider._client = EndedClient()
    claude_provider._connected = True
    claude_provider._spawn_key = ("stale",)

    async for _ in claude_provider.drain_events():
        pass

    assert claude_provider.cli_connected is False
    assert claude_provider._client is None
    assert claude_provider._spawn_key == ()


@pytest.mark.asyncio
async def test_old_drain_stream_ending_after_reconnect_keeps_the_new_client(
    claude_provider: ClaudeProvider,
) -> None:
    """A stale drain must not wipe the client a reconnect just installed."""
    release = asyncio.Event()

    class OldClient:
        async def receive_messages(self):
            await release.wait()
            return
            yield  # pragma: no cover

        async def disconnect(self):
            return None

    new_client = object()
    claude_provider._client = OldClient()
    claude_provider._connected = True

    drain = asyncio.ensure_future(_collect(claude_provider.drain_events()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Reconnect while the old consumer is still alive.
    claude_provider._client = new_client  # type: ignore[assignment]
    claude_provider._connected = True
    claude_provider._spawn_key = ("fresh",)
    release.set()
    await drain

    assert claude_provider._client is new_client
    assert claude_provider._connected is True
    assert claude_provider._spawn_key == ("fresh",)


async def _collect(gen) -> list:
    return [item async for item in gen]


def _steer_result(text: str):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-steer",
        result=text,
    )


def _steer_text(text: str):
    # Text reaches the host as partial stream events; a tool-use message is the
    # simplest ordered marker the converter turns into an event.
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    return AssistantMessage(
        content=[ToolUseBlock(id=f"t-{text}", name=text, input={})],
        model="haiku",
        session_id="sess-steer",
    )


class _SteerFakeClient:
    """Fake SDK client that records the steer's streamed user message."""

    def __init__(self, rounds) -> None:
        self.rounds = list(rounds)
        self.queries: list[dict] = []
        self.calls = 0
        self.control_requests: list[dict] = []
        self.plain_interrupts = 0
        self.echo_prompt = True
        client = self

        class _Query:
            async def _send_control_request(self, request, timeout=60.0):
                client.control_requests.append(request)
                return {"cancelled": True, "still_queued": []}

        self._query = _Query()

    async def connect(self, _payload):
        return None

    async def query(self, payload):
        if payload is None or not hasattr(payload, "__aiter__"):
            return None
        async for item in payload:
            self.queries.append(item)

    async def receive_response(self):
        self.calls += 1
        if self.echo_prompt and self.calls == 1:
            yield _prompt_echo(self.prompt_uuid)
        if not self.rounds:
            await asyncio.sleep(3600)
        round_ = self.rounds.pop(0)
        async for msg in round_(self):
            yield msg

    async def interrupt(self):
        self.plain_interrupts += 1

    async def disconnect(self):
        return None

    async def get_context_usage(self):
        return {}

    @property
    def steer_uuid(self) -> str:
        return self.queries[-1]["uuid"]

    @property
    def prompt_uuid(self) -> str:
        return self.queries[0]["uuid"]


def _wire_steer_provider(provider: ClaudeProvider, monkeypatch, fake) -> None:
    async def fake_ensure(_request):
        provider._connected = True
        provider._client = fake
        provider._session_id = "sess-steer"
        return fake

    monkeypatch.setattr(provider, "_ensure_connected", fake_ensure)


@pytest.mark.asyncio
async def test_claude_first_turn_connects_without_prompt_then_queries(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """No prompt at connect keeps the SDK from closing stdin at idle (#1402)."""
    connects: list[tuple] = []

    class FirstTurnClient(_SteerFakeClient):
        async def connect(self, *args):
            connects.append(args)

    async def round_one(_client):
        yield _steer_result("ok")

    fake = FirstTurnClient([round_one])

    async def fake_ensure(_request):
        claude_provider._client = fake
        claude_provider._session_id = "sess-steer"
        return fake

    monkeypatch.setattr(claude_provider, "_ensure_connected", fake_ensure)
    assert claude_provider._connected is False

    _ = [
        e
        async for e in claude_provider.run_streaming(
            AgentRequest(prompt="hi", model="haiku", mode="normal"), lambda _h: None
        )
    ]

    assert connects == [()]
    assert [q["message"]["content"] for q in fake.queries] == ["hi"]
    assert claude_provider._connected is True


@pytest.mark.asyncio
async def test_claude_steer_turn_refuses_without_open_turn(
    claude_provider: ClaudeProvider,
) -> None:
    fake = _SteerFakeClient([])
    claude_provider._client = fake
    claude_provider._connected = True

    request = AgentRequest(prompt="more", model="haiku", mode="normal")
    assert await claude_provider.steer_turn(request) is None
    assert fake.queries == []


@pytest.mark.asyncio
async def test_claude_steer_echo_mid_turn_emits_delivered_and_one_result(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from claude_agent_sdk import UserMessage

    from ciao.models import ResultEvent, SteerDeliveredEvent, ToolUseEvent

    steer_request = AgentRequest(prompt="x", model="haiku", mode="normal")
    delivery: dict[str, str | None] = {}

    async def round_one(client):
        yield _steer_text("working")
        delivery["id"] = await claude_provider.steer_turn(steer_request)
        yield UserMessage(content=[], uuid="not-ours")  # ignored: unknown uuid
        yield UserMessage(
            content=[{"type": "text", "text": "x"}], uuid=client.steer_uuid
        )
        yield _steer_text("done PINEAPPLE")
        yield _steer_result("done PINEAPPLE")

    fake = _SteerFakeClient([round_one])
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = [
        e
        async for e in claude_provider.run_streaming(
            AgentRequest(prompt="go", model="haiku", mode="normal"), lambda _h: None
        )
    ]

    delivered = [e for e in events if isinstance(e, SteerDeliveredEvent)]
    assert [e.delivery_id for e in delivered] == [delivery["id"]]
    assert delivered[0].type == "steer_delivered"
    assert len([e for e in events if isinstance(e, ResultEvent)]) == 1
    markers = [i for i, e in enumerate(events) if isinstance(e, ToolUseEvent)]
    assert [events[i].tool_name for i in markers] == ["working", "done PINEAPPLE"]
    assert markers[0] < events.index(delivered[0]) < markers[1]
    assert fake.calls == 1
    assert claude_provider._turn_open is False
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_steer_echo_after_result_keeps_turn_open(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from claude_agent_sdk import UserMessage

    from ciao.models import ResultEvent, SteerDeliveredEvent, ToolUseEvent

    steer_request = AgentRequest(prompt="x", model="haiku", mode="normal")

    async def round_one(_client):
        yield _steer_text("first")
        await claude_provider.steer_turn(steer_request)
        yield _steer_result("a")

    async def round_two(client):
        yield UserMessage(content="x", uuid=client.steer_uuid)
        yield _steer_text("second")
        yield _steer_result("b")

    fake = _SteerFakeClient([round_one, round_two])
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = [
        e
        async for e in claude_provider.run_streaming(
            AgentRequest(prompt="go", model="haiku", mode="normal"), lambda _h: None
        )
    ]

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    assert results[0].result == "b"
    assert len([e for e in events if isinstance(e, SteerDeliveredEvent)]) == 1
    assert [e.tool_name for e in events if isinstance(e, ToolUseEvent)] == ["first", "second"]
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_claude_steer_grace_timeout_ends_turn(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent, SteerDeliveredEvent

    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 0.05)
    steer_request = AgentRequest(prompt="x", model="haiku", mode="normal")

    async def round_one(_client):
        yield _steer_text("first")
        await claude_provider.steer_turn(steer_request)
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])  # the second receive_response never yields
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = [
        e
        async for e in claude_provider.run_streaming(
            AgentRequest(prompt="go", model="haiku", mode="normal"), lambda _h: None
        )
    ]

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    assert results[0].result == "a"
    assert not [e for e in events if isinstance(e, SteerDeliveredEvent)]
    assert claude_provider._steers_pending == set()
    assert claude_provider._turn_open is False


class _StopFakeClient:
    def __init__(self, cancelled=None, still_queued=None) -> None:
        self.control_requests: list[dict] = []
        self.plain_interrupts = 0
        self.reply = {"cancelled": cancelled or [], "still_queued": still_queued or []}
        client = self

        class _Query:
            async def _send_control_request(self, request, timeout=60.0):
                client.control_requests.append(request)
                return client.reply

        self._query = _Query()

    async def interrupt(self):
        self.plain_interrupts += 1


@pytest.mark.asyncio
async def test_claude_stop_cancels_queued_steers(
    claude_provider: ClaudeProvider, caplog,
) -> None:
    from ciao.providers.claude import SDKHandle

    claude_provider._steers_pending.add("s1")
    client = _StopFakeClient(still_queued=["s1"])
    with caplog.at_level(logging.WARNING):
        await SDKHandle(
            client=client, pending_steers=claude_provider._steers_pending
        ).stop()

    assert client.control_requests == [{"subtype": "interrupt", "cancel_queued": True}]
    assert client.plain_interrupts == 0
    assert claude_provider._steers_pending == set()
    assert "s1" in caplog.text


@pytest.mark.asyncio
async def test_claude_stop_without_steers_uses_plain_interrupt(
    claude_provider: ClaudeProvider,
) -> None:
    from ciao.providers.claude import SDKHandle

    client = _StopFakeClient()
    await SDKHandle(client=client, pending_steers=claude_provider._steers_pending).stop()

    assert client.plain_interrupts == 1
    assert client.control_requests == []


@pytest.mark.asyncio
async def test_claude_steer_with_images_builds_image_blocks(
    claude_provider: ClaudeProvider, monkeypatch, tmp_path: Path,
) -> None:
    image = tmp_path / "dot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    steer_request = AgentRequest(
        prompt="what colour is this?",
        model="haiku",
        mode="normal",
        images=[
            ImageAttachment(path=image, mime_type="image/png", original_filename="dot.png")
        ],
    )

    async def round_one(client):
        delivery_id = await claude_provider.steer_turn(steer_request)
        assert delivery_id == client.steer_uuid
        sent = client.queries[-1]
        assert sent["type"] == "user"
        content = sent["message"]["content"]
        assert content[0]["type"] == "text"
        assert "what colour is this?" in content[0]["text"]
        assert [block["type"] for block in content[1:]] == ["image"]
        assert content[1]["source"]["media_type"] == "image/png"
        claude_provider._steers_pending.clear()
        yield _steer_result("ok")

    fake = _SteerFakeClient([round_one])
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    async for _event in claude_provider.run_streaming(
        AgentRequest(prompt="go", model="haiku", mode="normal"), lambda _h: None
    ):
        pass
    assert len(fake.queries) == 2  # the prompt, then the steer


@pytest.mark.asyncio
async def test_claude_options_enable_user_message_replay(
    tmp_path: Path, monkeypatch,
) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)

    await provider._ensure_connected(
        AgentRequest(prompt="test", model="sonnet", mode="auto", provider="claude")
    )

    assert "replay-user-messages" in captured["options"].extra_args


async def _run_steer_turn(provider: ClaudeProvider, handles: list | None = None):
    return [
        e
        async for e in provider.run_streaming(
            AgentRequest(prompt="go", model="haiku", mode="normal"),
            (lambda h: handles.append(h)) if handles is not None else (lambda _h: None),
        )
    ]


@pytest.mark.asyncio
async def test_claude_steer_ended_stream_does_not_spin(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent

    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")

    async def empty(_client):
        return
        yield  # pragma: no cover

    fake = _SteerFakeClient([round_one] + [empty] * 200)
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    assert len([e for e in events if isinstance(e, ResultEvent)]) == 1
    assert fake.calls <= 3
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_turn_closes_steer_gate_before_post_result_awaits(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    seen: list[bool] = []

    async def spy(*_args, **_kwargs):
        seen.append(claude_provider._turn_open)

    async def round_one(_client):
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    monkeypatch.setattr(claude_provider, "_augment_with_context_pct", spy)

    await _run_steer_turn(claude_provider)

    assert seen == [False]


@pytest.mark.asyncio
async def test_claude_stop_during_grace_wait_ends_turn_and_closes_gate(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent

    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 30.0)
    in_grace = asyncio.Event()

    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")
        in_grace.set()

    fake = _SteerFakeClient([round_one])  # the follow-up round never yields
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(in_grace.wait(), 2)
    await asyncio.sleep(0.05)

    await handles[0].stop()
    events = await asyncio.wait_for(turn, 2)

    assert len([e for e in events if isinstance(e, ResultEvent)]) == 1
    assert await claude_provider.steer_turn(
        AgentRequest(prompt="late", model="haiku", mode="normal")
    ) is None
    assert fake.control_requests == [{"subtype": "interrupt", "cancel_queued": True}]


@pytest.mark.asyncio
async def test_claude_stop_cancel_failure_warns_and_falls_back_to_interrupt(
    claude_provider: ClaudeProvider, caplog,
) -> None:
    from ciao.providers.claude import SDKHandle

    client = _StopFakeClient()

    async def boom(request, timeout=60.0):
        raise RuntimeError("transport gone")

    client._query._send_control_request = boom
    claude_provider._steers_pending.add("s1")
    with caplog.at_level(logging.WARNING):
        await SDKHandle(
            client=client, pending_steers=claude_provider._steers_pending
        ).stop()

    assert client.plain_interrupts == 1
    assert "cancel" in caplog.text.lower()


@pytest.mark.asyncio
async def test_claude_unrelated_timeout_in_round_is_not_swallowed(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")

    async def round_two(_client):
        raise asyncio.TimeoutError("unrelated")
        yield  # pragma: no cover

    fake = _SteerFakeClient([round_one, round_two])
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    with pytest.raises(asyncio.TimeoutError):
        await _run_steer_turn(claude_provider)


@pytest.mark.asyncio
async def test_claude_steer_turn_discards_pending_when_send_raises(
    claude_provider: ClaudeProvider,
) -> None:
    class Client:
        async def query(self, payload):
            raise RuntimeError("send blew up")

    claude_provider._client = Client()
    claude_provider._connected = True
    claude_provider._turn_open = True

    with pytest.raises(RuntimeError):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_steer_grace_timeout_cancels_the_queued_steer(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 0.05)
    ids: list[str | None] = []

    async def round_one(_client):
        ids.append(await claude_provider.steer_turn(
            AgentRequest(prompt="x", model="haiku", mode="normal")
        ))
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    await _run_steer_turn(claude_provider)

    assert fake.control_requests == [
        {"subtype": "cancel_async_message", "message_uuid": ids[0]}
    ]
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_grace_timeout_closes_steer_gate_before_cancelling(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 0.05)
    late: list[str | None] = []

    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])

    async def cancel(request, timeout=60.0):
        late.append(await claude_provider.steer_turn(
            AgentRequest(prompt="late", model="haiku", mode="normal")
        ))
        return {"cancelled": True}

    fake._query._send_control_request = cancel
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    await _run_steer_turn(claude_provider)

    assert late == [None]
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_grace_timeout_cancel_is_bounded(
    claude_provider: ClaudeProvider, monkeypatch, caplog,
) -> None:
    from ciao.models import ResultEvent

    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 0.05)
    monkeypatch.setattr("ciao.providers.claude._STEER_CANCEL_TIMEOUT_S", 0.05)

    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])

    async def hang(request, timeout=60.0):
        await asyncio.sleep(3600)

    fake._query._send_control_request = hang
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        events = await asyncio.wait_for(_run_steer_turn(claude_provider), 3)

    assert len([e for e in events if isinstance(e, ResultEvent)]) == 1
    assert "cancel" in caplog.text.lower()
    assert claude_provider._steers_pending == set()


@pytest.mark.asyncio
async def test_claude_grace_timeout_warns_when_cancel_not_confirmed(
    claude_provider: ClaudeProvider, monkeypatch, caplog,
) -> None:
    monkeypatch.setattr("ciao.providers.claude._STEER_ECHO_GRACE_S", 0.05)

    async def round_one(_client):
        await claude_provider.steer_turn(AgentRequest(prompt="x", model="haiku", mode="normal"))
        yield _steer_result("a")

    fake = _SteerFakeClient([round_one])

    async def not_cancelled(request, timeout=60.0):
        return {"cancelled": False}

    fake._query._send_control_request = not_cancelled
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    with caplog.at_level(logging.WARNING):
        await _run_steer_turn(claude_provider)

    assert "not cancelled" in caplog.text.lower()


# --- #1406: attribute a turn's output by its own prompt's replay echo ---


def _follow_up_result(text: str, *, is_error: bool = False, origin: dict | None = None):
    from claude_agent_sdk import ResultMessage

    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sess-steer",
        result=text,
        origin=origin if origin is not None else {"kind": "task-notification"},
    )


def _marker_names(events) -> list[str]:
    from ciao.models import ToolUseEvent

    return [e.tool_name for e in events if isinstance(e, ToolUseEvent)]


@pytest.mark.asyncio
async def test_claude_turn_skips_a_follow_up_that_ran_before_its_prompt(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent

    async def follow_up(_client):
        yield _steer_text("follow-up")
        yield _follow_up_result("background done")

    async def ours(client):
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_text("mine")
        yield _steer_result("ANSWER")

    fake = _SteerFakeClient([follow_up, ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [r.result for r in results] == ["ANSWER"]
    assert results[0].origin == ""
    assert _marker_names(events) == ["mine"]
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_claude_turn_ends_on_the_result_after_its_echo_whatever_its_origin(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """The CLI can fold a queued prompt into a running follow-up turn."""
    from ciao.models import ResultEvent

    async def merged(client):
        yield _steer_text("follow-up")
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_text("mine")
        yield _follow_up_result("ANSWER")

    fake = _SteerFakeClient([merged])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [r.result for r in results] == ["ANSWER"]
    assert results[0].origin == ""
    assert _marker_names(events) == ["mine"]
    assert fake.calls == 1


@pytest.mark.parametrize(
    "stale_is_error", [False, True], ids=["orphan_steer_ran", "stopped_follow_up"]
)
@pytest.mark.asyncio
async def test_claude_turn_skips_a_stale_result_left_in_the_cli(
    claude_provider: ClaudeProvider, monkeypatch, stale_is_error: bool,
) -> None:
    """A steer whose cancel failed, or a turn a Stop left queued, runs unread
    while the CLI stays alive (#1402); its result must not end the next turn."""
    from claude_agent_sdk import UserMessage

    from ciao.models import ResultEvent

    async def stale(_client):
        yield UserMessage(content="old steer", uuid="orphan-steer")
        yield _steer_text("stale")
        yield _follow_up_result("STALE", is_error=stale_is_error, origin={"kind": "human"})

    async def ours(client):
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_result("THIRD")

    fake = _SteerFakeClient([stale, ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    assert [e.result for e in events if isinstance(e, ResultEvent)] == ["THIRD"]
    assert _marker_names(events) == []


@pytest.mark.asyncio
async def test_claude_stop_before_the_echo_cancels_the_queued_prompt(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent

    stopped = asyncio.Event()
    in_follow_up = asyncio.Event()

    async def follow_up(_client):
        yield _steer_text("follow-up")
        in_follow_up.set()
        await stopped.wait()
        yield _follow_up_result("", is_error=True)

    fake = _SteerFakeClient([follow_up])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(in_follow_up.wait(), 2)

    await handles[0].stop()
    stopped.set()
    events = await asyncio.wait_for(turn, 2)

    assert fake.control_requests == [{"subtype": "interrupt", "cancel_queued": True}]
    assert fake.plain_interrupts == 0
    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [(r.result, r.is_error) for r in results] == [("", True)]
    assert _marker_names(events) == []


@pytest.mark.asyncio
async def test_claude_stop_wakes_a_turn_waiting_for_its_echo(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import ResultEvent

    async def follow_up(_client):
        yield _follow_up_result("background done")

    fake = _SteerFakeClient([follow_up])  # the prompt never runs
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    while fake.calls < 2:
        await asyncio.sleep(0.01)

    await handles[0].stop()
    events = await asyncio.wait_for(turn, 2)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [(r.result, r.is_error) for r in results] == [("", True)]


@pytest.mark.asyncio
async def test_claude_stop_after_the_echo_keeps_the_plain_interrupt(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    echoed = asyncio.Event()
    stopped = asyncio.Event()

    async def ours(client):
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_text("mine")
        echoed.set()
        await stopped.wait()
        yield _follow_up_result("", is_error=True, origin={"kind": "human"})

    fake = _SteerFakeClient([ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(echoed.wait(), 2)

    await handles[0].stop()
    stopped.set()
    await asyncio.wait_for(turn, 2)

    assert fake.plain_interrupts == 1
    assert fake.control_requests == []


@pytest.mark.asyncio
async def test_claude_compaction_status_before_the_echo_is_shown(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """A /compact prompt is echoed only after the compaction it ran."""
    from claude_agent_sdk import SystemMessage

    from ciao.models import ResultEvent, SystemStatusEvent

    async def compact(client):
        yield SystemMessage(subtype="status", data={"status": "compacting"})
        yield SystemMessage(
            subtype="compact_boundary",
            data={"compact_metadata": {"pre_tokens": 50000, "post_tokens": 4000}},
        )
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_result("")

    fake = _SteerFakeClient([compact])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    assert [e.status for e in events if isinstance(e, SystemStatusEvent)] == [
        "compacting",
        "Compacted context: 50,000 → 4,000 tokens",
    ]
    assert len([e for e in events if isinstance(e, ResultEvent)]) == 1


@pytest.mark.asyncio
async def test_claude_pre_echo_init_still_records_bundled_skills(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from claude_agent_sdk import SystemMessage

    seen: list[dict] = []

    async def spy(_client, init):
        seen.append(init)

    monkeypatch.setattr(claude_provider, "_record_bundled_skills", spy)

    async def ours(client):
        yield SystemMessage(subtype="init", data={"skills": ["a"]})
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_result("ok")

    fake = _SteerFakeClient([ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    assert seen == [{"skills": ["a"]}]


@pytest.mark.asyncio
async def test_claude_follow_up_text_does_not_flag_our_result_as_a_drop(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """The connection-drop banner check reads only this turn's own text."""
    from claude_agent_sdk.types import StreamEvent as SDKStreamEvent

    from ciao.models import ResultEvent

    async def merged(client):
        yield SDKStreamEvent(
            uuid="e1",
            session_id="sess-steer",
            event={
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": "API Error: Connection closed mid-response.",
                },
            },
            parent_tool_use_id=None,
        )
        yield _prompt_echo(client.prompt_uuid)
        yield _steer_result("ok")

    fake = _SteerFakeClient([merged])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)

    events = await asyncio.wait_for(_run_steer_turn(claude_provider), 5)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [(r.result, r.is_error) for r in results] == [("ok", False)]


@pytest.mark.asyncio
async def test_claude_stop_before_the_echo_reports_the_cancelled_prompt(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """The host puts the cancelled prompt back in the waiting queue (#1406)."""
    from ciao.models import PromptCancelledEvent

    stopped = asyncio.Event()
    in_follow_up = asyncio.Event()

    async def follow_up(_client):
        yield _steer_text("follow-up")
        in_follow_up.set()
        await stopped.wait()
        yield _follow_up_result("", is_error=True)

    fake = _SteerFakeClient([follow_up])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(in_follow_up.wait(), 2)

    await handles[0].stop()
    stopped.set()
    events = await asyncio.wait_for(turn, 2)

    assert len([e for e in events if isinstance(e, PromptCancelledEvent)]) == 1


@pytest.mark.asyncio
async def test_claude_stop_after_the_echo_reports_no_cancelled_prompt(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    from ciao.models import PromptCancelledEvent

    echoed = asyncio.Event()
    stopped = asyncio.Event()

    async def ours(client):
        yield _prompt_echo(client.prompt_uuid)
        echoed.set()
        await stopped.wait()
        yield _follow_up_result("", is_error=True, origin={"kind": "human"})

    fake = _SteerFakeClient([ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(echoed.wait(), 2)

    await handles[0].stop()
    stopped.set()
    events = await asyncio.wait_for(turn, 2)

    assert not [e for e in events if isinstance(e, PromptCancelledEvent)]


@pytest.mark.asyncio
async def test_claude_cli_death_after_the_echo_on_stop_keeps_the_interrupted_error(
    claude_provider: ClaudeProvider, monkeypatch,
) -> None:
    """Our prompt was echoed, so an empty stopped result would hide a CLI death."""
    from ciao.models import PromptCancelledEvent, ResultEvent
    from ciao.providers.claude import _TURN_INTERRUPTED_MESSAGE

    echoed = asyncio.Event()
    stopped = asyncio.Event()

    async def ours(client):
        yield _prompt_echo(client.prompt_uuid)
        echoed.set()
        await stopped.wait()  # the stream then closes with no result

    fake = _SteerFakeClient([ours])
    fake.echo_prompt = False
    _wire_steer_provider(claude_provider, monkeypatch, fake)
    handles: list = []
    turn = asyncio.create_task(_run_steer_turn(claude_provider, handles))
    await asyncio.wait_for(echoed.wait(), 2)

    await handles[0].stop()
    stopped.set()
    events = await asyncio.wait_for(turn, 2)

    results = [e for e in events if isinstance(e, ResultEvent)]
    assert [(r.result, r.is_error) for r in results] == [(_TURN_INTERRUPTED_MESSAGE, True)]
    assert not [e for e in events if isinstance(e, PromptCancelledEvent)]


@pytest.mark.asyncio
async def test_claude_machine_scope_adds_the_linked_repo_dir(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = ClaudeProvider(workspace)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    repo = str(tmp_path / "repo")
    request = AgentRequest(prompt="test", model="sonnet", mode="auto", linked_repo=repo)

    await provider._ensure_connected(request)

    options = captured["options"]
    assert [str(p) for p in options.add_dirs] == [repo]
    assert options.sandbox is None


@pytest.mark.asyncio
async def test_claude_workspace_scope_lists_the_linked_repo_once(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    agent_root = tmp_path / "agent"
    repo = str(tmp_path / "repo")
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="normal",
        agent_fs_scope="workspace",
        agent_roots=(str(agent_root), str(agent_root / "memory-vault"), repo),
        linked_repo=repo,
    )

    await provider._ensure_connected(request)

    assert [str(p) for p in captured["options"].add_dirs] == [repo]


def test_a_changed_linked_repo_changes_the_claude_spawn_key(tmp_path: Path) -> None:
    provider = ClaudeProvider(tmp_path)
    req_a = AgentRequest(prompt="x", model="sonnet", mode="auto", linked_repo="/r/a")
    req_b = AgentRequest(prompt="x", model="sonnet", mode="auto", linked_repo="/r/b")

    assert provider._spawn_key_for(req_a) != provider._spawn_key_for(req_b)


# ── Initialize handshake: budget, diagnostics, retry ─────────────────────
# A cold CLI start measured 13-53 s on a loaded machine (2026-10-20), right at
# the SDK's 60 s initialize floor. These pin the three things that keep a
# first turn alive: a longer deadline, a startup failure that names itself, and
# one retry on a fresh CLI when the handshake still misses.


def test_initialize_timeout_matches_only_the_initialize_subtype() -> None:
    assert _is_initialize_timeout(Exception("Control request timeout: initialize"))
    assert not _is_initialize_timeout(Exception("Control request timeout: get_context_usage"))
    assert not _is_initialize_timeout(Exception("ProcessTransport is not ready for writing"))


def test_claude_connect_deadline_outlives_the_sdk_initialize_deadline() -> None:
    # Our outer wait must outlive the SDK's own initialize budget, or it fires
    # first with a bare TimeoutError and the SDK never names the cause.
    assert claude._CONNECT_TIMEOUT_S > claude._INITIALIZE_TIMEOUT_MS / 1000


@pytest.mark.asyncio
async def test_claude_connect_uses_the_connect_budget(monkeypatch) -> None:
    # Pins that the helper waits on _CONNECT_TIMEOUT_S, not a literal: the
    # other connect tests pass whichever deadline the helper uses, because
    # a raising connect() never reaches asyncio.wait_for's timer, and a
    # literal (e.g. 120) would still raise ClaudeInitializeTimeout here,
    # just two minutes later.
    monkeypatch.setattr(claude, "_CONNECT_TIMEOUT_S", 0.01)

    class HangingClient:
        async def connect(self) -> None:
            await asyncio.sleep(3600)

    started = asyncio.get_running_loop().time()
    with pytest.raises(ClaudeInitializeTimeout):
        await claude._connect_with_deadline(HangingClient())
    assert asyncio.get_running_loop().time() - started < 1.0


@pytest.mark.asyncio
async def test_claude_connect_timeout_is_an_initialize_miss() -> None:
    class FakeClient:
        async def connect(self) -> None:
            raise asyncio.TimeoutError()

    with pytest.raises(ClaudeInitializeTimeout):
        await claude._connect_with_deadline(FakeClient())


@pytest.mark.asyncio
async def test_claude_initialize_message_is_still_classified() -> None:
    class FakeClient:
        async def connect(self) -> None:
            raise Exception("Control request timeout: initialize")

    with pytest.raises(ClaudeInitializeTimeout):
        await claude._connect_with_deadline(FakeClient())


@pytest.mark.asyncio
async def test_claude_connect_other_errors_propagate_unchanged() -> None:
    boom = ValueError("boom")

    class FakeClient:
        async def connect(self) -> None:
            raise boom

    with pytest.raises(ValueError) as excinfo:
        await claude._connect_with_deadline(FakeClient())

    assert excinfo.value is boom


@pytest.mark.asyncio
async def test_claude_cli_gets_the_initialize_budget_and_startup_failure_results(
    tmp_path: Path, monkeypatch
) -> None:
    constructed: list[object] = []

    class FakeClient:
        def __init__(self, options):
            constructed.append(options)

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    monkeypatch.delenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", raising=False)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        extra_env={"CIAO_CHAT_ID": "chat-1"},
    )

    await provider._ensure_connected(request)

    options = constructed[0]
    assert options.env["CLAUDE_CODE_STARTUP_FAILURE_RESULTS"] == "1"
    # The caller's own environment still reaches the CLI.
    assert options.env["CIAO_CHAT_ID"] == "chat-1"
    # The SDK reads this from the process environment, not from options.
    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "180000"


@pytest.mark.asyncio
async def test_caller_startup_failure_results_value_wins(
    tmp_path: Path, monkeypatch
) -> None:
    constructed: list[object] = []

    class FakeClient:
        def __init__(self, options):
            constructed.append(options)

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        extra_env={"CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "0"},
    )

    await provider._ensure_connected(request)

    assert constructed[0].env["CLAUDE_CODE_STARTUP_FAILURE_RESULTS"] == "0"


@pytest.mark.asyncio
async def test_claude_initialize_timeout_retries_once_with_a_fresh_cli(
    tmp_path: Path, monkeypatch
) -> None:
    provider = ClaudeProvider(tmp_path)
    attempts: list[int] = []

    async def fake_once(request, register_handle):
        attempts.append(1)
        if len(attempts) == 1:
            raise ClaudeInitializeTimeout("Control request timeout: initialize")
        yield ResultEvent(type="result", result="ok")

    monkeypatch.setattr(provider, "_run_streaming_once", fake_once)
    request = AgentRequest(prompt="hi", model="sonnet", mode="auto")

    events = [event async for event in provider.run_streaming(request, lambda handle: None)]

    assert len(attempts) == 2
    assert [event.type for event in events] == ["result"]
    assert events[0].result == "ok"


@pytest.mark.asyncio
async def test_claude_second_initialize_timeout_is_not_retried_again(
    tmp_path: Path, monkeypatch
) -> None:
    provider = ClaudeProvider(tmp_path)
    attempts: list[int] = []

    async def fake_once(request, register_handle):
        attempts.append(1)
        raise ClaudeInitializeTimeout("Control request timeout: initialize")
        yield ResultEvent(type="result")  # pragma: no cover - unreachable

    monkeypatch.setattr(provider, "_run_streaming_once", fake_once)
    request = AgentRequest(prompt="hi", model="sonnet", mode="auto")

    with pytest.raises(ClaudeInitializeTimeout):
        async for _ in provider.run_streaming(request, lambda handle: None):
            pass

    # One retry, then the error is the user's: a machine failing every spawn
    # must not loop.
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_claude_workspace_scope_passes_read_only_files_to_the_sandbox(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(tmp_path)
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)
    registry = tmp_path / "rt" / "workspaces.json"
    request = AgentRequest(
        prompt="test",
        model="sonnet",
        mode="auto",
        agent_fs_scope="workspace",
        agent_roots=(str(tmp_path / "agent"), str(tmp_path / "vault")),
        agent_read_only=(str(registry),),
    )

    await provider._ensure_connected(request)

    filesystem = captured["options"].sandbox["filesystem"]
    assert str(registry) in filesystem["allowRead"]
    assert str(registry) not in filesystem["allowWrite"]


async def test_opencode_workspace_scope_passes_read_only_files_to_the_prefix(tmp_path, monkeypatch):
    provider, request = _opencode_scope_fixture(tmp_path, "workspace")
    request.extra_env = {"PATH": str(tmp_path / "node-bin")}
    request.agent_read_only = (str(tmp_path / "rt" / "workspaces.json"),)
    monkeypatch.setattr("ciao.providers.opencode.node_tool_read_roots", lambda path: [])
    _capture_spawn(monkeypatch, [])
    calls: list[list[Path]] = []
    xdg = tmp_path / "xdg"
    for name in ("DATA", "STATE", "CONFIG"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(xdg / name.lower()))
    (xdg / "config" / "opencode").mkdir(parents=True)
    binary = tmp_path / "install" / "bin" / "opencode"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")

    def fake_prefix(*, roots, read_only=(), denied=(), regrant=()):
        calls.append(list(read_only))
        return ["sandbox-exec", "-p", "PROFILE"]

    monkeypatch.setattr("ciao.providers.opencode.opencode_sandbox_prefix", fake_prefix)

    with pytest.raises(_SpawnCaptured):
        await provider._start_server_once(request, str(binary))

    assert Path(request.agent_read_only[0]) in calls[0]
