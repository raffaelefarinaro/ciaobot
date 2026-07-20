from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import AgentRequest, ImageAttachment
from ciao.providers.base import (
    build_claude_message_content,
    build_prompt,
)
from ciao.providers.claude import ClaudeProvider, _sdk_permission_mode


@pytest.fixture
def claude_provider(tmp_path: Path) -> ClaudeProvider:
    return ClaudeProvider(tmp_path)


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
        memory_enabled=False,
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
        control_surface="mcp",
        mcp_url="http://127.0.0.1:8443/mcp/",
        mcp_token="secret-session-token",
        mcp_required=True,
    )

    await provider._ensure_connected(request)

    options = captured["options"]
    # Strict mode must NOT be forced on the chat path: it restricts the CLI to
    # only the ciaobot server and suppresses the account's claude.ai connector
    # MCPs (mcp__claude_ai_*). Connectors stay loaded and are gated per-workspace
    # by the disallowed_tools denylist instead. The ciaobot server is still injected.
    assert options.strict_mcp_config is False
    assert options.mcp_servers == {
        "ciaobot": {
            "type": "http",
            "url": request.mcp_url,
            "headers": {"Authorization": "Bearer secret-session-token"},
        }
    }
    # The MCP path carries the slim prefer-typed-tools nudge, not the old block.
    assert "prefer them over curl, the ciao CLI" in options.system_prompt["append"]


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
    from ciao.web.routes_api import _summarize_task_notification

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
    from ciao.web.routes_api import _summarize_task_notification

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
    from ciao.web.routes_api import _is_cli_internal_envelope

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
    from ciao.web.routes_api import _strip_injected_context

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


def test_claude_error_result_gets_host_annotation(
    claude_provider: ClaudeProvider,
) -> None:
    """A hostless connection error gains the resolved endpoint host (#162)."""
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
    assert "(host: api.anthropic.com)" in result.result


def test_claude_error_result_annotation_is_selective(
    claude_provider: ClaudeProvider,
) -> None:
    """Non-connection errors and success results pass through untouched (#162)."""
    from claude_agent_sdk import ResultMessage

    from ciao.providers.claude import _annotate_connection_host, _resolve_api_host

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

    # Already naming the host: no double-annotation.
    assert (
        _annotate_connection_host(
            "getaddrinfo ENOTFOUND api.anthropic.com", "api.anthropic.com"
        )
        == "getaddrinfo ENOTFOUND api.anthropic.com"
    )
    # A per-turn ANTHROPIC_BASE_URL override (Ollama/OpenRouter) wins.
    assert (
        _resolve_api_host({"ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1"})
        == "openrouter.ai"
    )
    # Falls back to a real hostname (process env or Anthropic's default),
    # never an empty string.
    assert _resolve_api_host({})


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
    payload = claude_provider._prompt_payload(request)
    assert isinstance(payload, AsyncIterable)

    messages = [msg async for msg in payload]
    assert len(messages) == 1
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

        async def query(self, _payload):
            return None

        async def receive_response(self):
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
    monkeypatch.setattr(claude_provider, "_prompt_payload", lambda _req: None)

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

        async def query(self, _payload):
            return None

        async def receive_response(self):
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
    monkeypatch.setattr(claude_provider, "_prompt_payload", lambda _req: None)

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
