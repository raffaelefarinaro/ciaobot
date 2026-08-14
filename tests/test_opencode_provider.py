"""opencode provider unit tests.

Event fixtures under ``tests/fixtures/opencode/`` were captured from a real
``opencode serve`` process (1.18.18) and sanitized: absolute paths replaced,
bundler stack traces trimmed to their first line. No credentials, prompts, or
account identifiers are recorded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao.models import (
    AssistantTextDelta,
    PermissionRequestEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.providers.opencode import (
    OpencodeProvider,
    OpencodeSettings,
    _catalog_from_providers,
    config_placeholder_problems,
    error_text,
    missing_required_paths,
    mode_settings,
    opencode_tier_overrides,
    split_model,
    unresolved_placeholders,
    usage_payload,
    workspace_config_placeholder_problems,
)

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


def _provider(tmp_path: Path) -> OpencodeProvider:
    return OpencodeProvider(tmp_path)


# ── capabilities ────────────────────────────────────────────────────────


def test_steer_is_unsupported():
    """opencode has no API to inject into a running turn (see the plan doc)."""
    assert OpencodeProvider.capabilities.steer is False


def test_quota_is_unsupported():
    """Bring-your-own-provider: there is no unified quota snapshot to report."""
    assert OpencodeProvider.capabilities.quota is False


def test_background_subagents_are_supported():
    """Child sessions carry parentID, so background agents are inspectable."""
    assert OpencodeProvider.capabilities.background_subagents is True
    assert OpencodeProvider.capabilities.subagent_messages is True


@pytest.mark.asyncio
async def test_steer_never_sends_a_second_prompt(tmp_path):
    """Returning False keeps the message in ProviderService's next-turn queue.

    Sending a second prompt instead would either queue it out of order or
    abort the active turn; neither is steering.
    """
    provider = _provider(tmp_path)
    assert await provider.steer(object()) is False  # type: ignore[arg-type]


# ── contract verification ───────────────────────────────────────────────


def test_missing_required_paths_flags_an_incompatible_build():
    spec = {"paths": {"/global/health": {}, "/session": {}}}
    missing = missing_required_paths(spec)
    assert "/session/{sessionID}/abort" in missing
    assert "/question/{requestID}/reply" in missing


def test_missing_required_paths_accepts_the_real_document():
    """The captured OpenAPI paths from opencode 1.18 satisfy every requirement."""
    spec = json.loads((FIXTURES / "openapi_paths.json").read_text(encoding="utf-8"))
    assert missing_required_paths(spec) == ()


# ── model ids ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("anthropic/claude-sonnet-4-6", ("anthropic", "claude-sonnet-4-6")),
        ("openai/gpt-5.6-terra", ("openai", "gpt-5.6-terra")),
        # A bare id names no provider; opencode falls back to its default.
        ("sonnet", ("", "sonnet")),
        ("", ("", "")),
        # A trailing slash is not a provider split.
        ("anthropic/", ("", "anthropic/")),
    ],
)
def test_split_model(value, expected):
    assert split_model(value) == expected


# ── modes ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "agent"),
    [("plan", "plan"), ("normal", "build"), ("auto", "build"), ("bypass", "build")],
)
def test_mode_agents(mode, agent):
    assert mode_settings(mode)[0] == agent


def _actions(mode: str) -> dict[str, str]:
    """Flatten a ruleset to {permission: action} for readable assertions."""
    return {rule["permission"]: rule["action"] for rule in mode_settings(mode)[1]}


def test_permission_rules_use_the_api_shape_not_the_config_map():
    """`POST /session` wants a list of rules; the `{"*": "ask"}` map 400s.

    The config-file form and the API form differ, and the server rejects the
    wrong one with a bare `{"_tag":"BadRequest"}`, so pin the shape here.
    """
    ruleset = mode_settings("normal")[1]
    assert isinstance(ruleset, list)
    assert ruleset[0] == {"permission": "*", "pattern": "*", "action": "ask"}


def test_every_rule_uses_a_valid_action():
    for mode in ("plan", "normal", "auto", "bypass"):
        for rule in mode_settings(mode)[1]:
            assert rule["action"] in {"allow", "deny", "ask"}
            assert set(rule) == {"permission", "pattern", "action"}


def test_the_wildcard_rule_comes_first():
    """Resolution is last-match-wins, so specific grants must follow it."""
    for mode in ("plan", "auto"):
        assert mode_settings(mode)[1][0]["permission"] == "*"


def test_bypass_allows_everything_and_normal_asks():
    # The wildcard is what defines the mode; the control-plane allow rules ride
    # alongside it in every non-plan mode.
    assert _actions("bypass")["*"] == "allow"
    assert _actions("normal")["*"] == "ask"
    assert "edit" not in _actions("normal")


def test_auto_allows_edits_but_still_gates_shell():
    """Auto mode applies edits automatically; bash stays behind the wildcard."""
    actions = _actions("auto")
    assert actions["edit"] == "allow"
    assert actions["*"] == "ask"
    assert "bash" not in actions


def test_plan_mode_is_read_only():
    actions = _actions("plan")
    assert actions["read"] == "allow"
    assert actions["*"] == "ask"
    assert "edit" not in actions


def test_unknown_mode_falls_back_to_normal():
    assert mode_settings("nonsense") == mode_settings("normal")  # type: ignore[arg-type]


# ── error sanitization ──────────────────────────────────────────────────


def test_error_text_drops_the_stack_trace():
    """opencode error payloads embed a bundler stack; only line one is shown."""
    error = {
        "name": "UnknownError",
        "data": {"message": "Model not found: x\n    at <anonymous> (/$bunfs/root/a.js:1:2)"},
    }
    assert error_text(error) == "Model not found: x"


def test_error_text_falls_back_to_the_error_name():
    assert error_text({"name": "ProviderAuthError", "data": {}}) == "ProviderAuthError"


def test_error_text_handles_a_missing_payload():
    assert error_text(None) == "opencode reported an error"


# ── usage ───────────────────────────────────────────────────────────────


def test_usage_payload_flattens_cache_counts():
    usage = usage_payload(
        {"input": 120, "output": 40, "reasoning": 8, "cache": {"read": 900, "write": 30}}
    )
    assert usage == {
        "inputTokens": "120",
        "outputTokens": "40",
        "reasoningTokens": "8",
        "cacheReadTokens": "900",
        "cacheWriteTokens": "30",
    }


def test_usage_payload_omits_zero_counts():
    assert usage_payload({"input": 0, "output": 5, "cache": {}}) == {"outputTokens": "5"}


def test_usage_payload_tolerates_junk():
    assert usage_payload(None) == {}
    assert usage_payload({"input": "not-a-number"}) == {}


# ── model catalog ───────────────────────────────────────────────────────


def test_catalog_lists_only_connected_providers():
    """`all` enumerates every backend opencode knows of — hundreds. Only the
    authenticated ones are models the user can actually run."""
    payload = {
        "connected": ["anthropic"],
        "all": [
            {"id": "anthropic", "models": {"claude-sonnet-4-6": {"id": "claude-sonnet-4-6", "name": "Sonnet"}}},
            {"id": "openai", "models": {"gpt-5.6-terra": {"id": "gpt-5.6-terra", "name": "Terra"}}},
        ],
    }
    assert _catalog_from_providers(payload) == [
        {
            "model": "anthropic/claude-sonnet-4-6",
            "label": "Sonnet (anthropic)",
            "variants": [],
        }
    ]


def test_catalog_handles_list_shaped_models():
    payload = {"connected": ["x"], "all": [{"id": "x", "models": [{"id": "m", "name": "M"}]}]}
    assert _catalog_from_providers(payload) == [
        {"model": "x/m", "label": "M (x)", "variants": []}
    ]


def test_catalog_reports_per_model_reasoning_variants():
    """opencode calls reasoning effort a model `variant`, and it is per model.

    Captured live: `deepseek-v4-flash-free` offers low/high/max while
    `big-pickle` offers none, so the level list has to be narrowed per model
    rather than assumed from a fixed ladder.
    """
    payload = {
        "connected": ["opencode"],
        "all": [{
            "id": "opencode",
            "models": {
                "deepseek-v4-flash-free": {
                    "id": "deepseek-v4-flash-free",
                    "variants": {"low": {}, "high": {}, "max": {}},
                },
                "big-pickle": {"id": "big-pickle"},
            },
        }],
    }
    by_model = {row["model"]: row["variants"] for row in _catalog_from_providers(payload)}
    assert by_model["opencode/deepseek-v4-flash-free"] == ["high", "low", "max"]
    assert by_model["opencode/big-pickle"] == []


def test_catalog_is_empty_when_nothing_is_connected():
    payload = {"connected": [], "all": [{"id": "anthropic", "models": {"a": {"id": "a"}}}]}
    assert _catalog_from_providers(payload) == []


def test_catalog_tolerates_junk():
    assert _catalog_from_providers(None) == []
    assert _catalog_from_providers({"all": "nope"}) == []


# ── tier pins ───────────────────────────────────────────────────────────


def test_tier_overrides_skips_unpinned_tiers():
    class Config:
        opencode = OpencodeSettings(sonnet_model="anthropic/claude-sonnet-4-6")

    assert opencode_tier_overrides(Config()) == {"sonnet": "anthropic/claude-sonnet-4-6"}


def test_tier_overrides_on_a_config_without_opencode():
    assert opencode_tier_overrides(object()) == {}


# ── event normalization ─────────────────────────────────────────────────


def _convert(provider: OpencodeProvider, kind: str, properties: dict):
    return provider._event_to_stream({"type": kind, "properties": properties})


def test_text_delta_becomes_assistant_text(tmp_path):
    events = _convert(
        _provider(tmp_path), "session.next.text.delta", {"delta": "hello"}
    )
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "hello"


def test_empty_text_delta_is_dropped(tmp_path):
    assert _convert(_provider(tmp_path), "session.next.text.delta", {"delta": ""}) == []


def test_reasoning_delta_becomes_thinking(tmp_path):
    events = _convert(
        _provider(tmp_path), "session.next.reasoning.delta", {"delta": "hmm"}
    )
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "hmm"


def test_tool_called_becomes_tool_use_with_a_stable_id(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.next.tool.called",
        {"callID": "call_1", "tool": "read", "input": {"filePath": "/workspace/a.py"}},
    )
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "read"
    assert events[0].tool_use_id == "call_1"
    assert events[0].tool_input == "/workspace/a.py"


def test_write_tool_reports_a_file_touch(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.next.tool.called",
        {"callID": "c", "tool": "write", "input": {"filePath": "/workspace/new.py"}},
    )
    assert events[0].file_touches == [{"file_path": "/workspace/new.py", "action": "write"}]


def test_read_tool_reports_no_file_touch(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.next.tool.called",
        {"callID": "c", "tool": "read", "input": {"filePath": "/workspace/a.py"}},
    )
    assert events[0].file_touches is None


def test_tool_result_recovers_the_tool_name_from_the_call(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "session.next.tool.called", {"callID": "c1", "tool": "bash", "input": {}})
    events = _convert(provider, "session.next.tool.success", {"callID": "c1"})
    assert events[0].type == "tool_result"
    assert events[0].tool_name == "bash"
    # The call is forgotten once resolved, so a duplicate cannot re-fire it.
    assert provider._tool_calls == {}


def test_failed_tool_carries_a_sanitized_reason(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "session.next.tool.called", {"callID": "c1", "tool": "bash", "input": {}})
    events = _convert(
        provider,
        "session.next.tool.failed",
        {"callID": "c1", "error": {"name": "E", "data": {"message": "boom\n  at x"}}},
    )
    assert events[0].tool_input == "boom"


def test_step_ended_reports_token_usage(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.next.step.ended",
        {"tokens": {"input": 10, "output": 3}},
    )
    assert isinstance(events[0], TokenUsageEvent)
    assert (events[0].input_tokens, events[0].output_tokens) == (10, 3)


# Captured verbatim from a live `permission.asked` event (opencode 1.18.18)
# when a bash command was gated in `normal` mode.
LIVE_PERMISSION = {
    "id": "per_live1",
    "sessionID": "ses_1",
    "permission": "bash",
    "patterns": ["echo approved-ok"],
    "metadata": {"command": "echo approved-ok"},
    "always": ["echo *"],
    "tool": {"messageID": "msg_1", "callID": "call_abc"},
}


def test_permission_card_names_the_tool_and_the_command(tmp_path):
    """Regression: an approval card must say *what* is being approved.

    The live event is `permission.asked` with `permission`/`patterns`/
    `metadata`; reading the schema's v2 `action`/`resources` instead produced
    a card that said only "run a tool" with no detail — the user could not
    tell what they were approving.
    """
    events = _convert(_provider(tmp_path), "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_name == "bash"
    assert events[0].tool_input == "echo approved-ok"
    assert "bash" in events[0].message


def test_permission_card_links_back_to_the_tool_call(tmp_path):
    """So the UI can retract the tool card when the request is refused."""
    provider = _provider(tmp_path)
    _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert provider.tool_use_id_for_request("per_live1") == "call_abc"


def test_permission_card_falls_back_to_patterns_without_metadata(tmp_path):
    payload = {**LIVE_PERMISSION, "metadata": {}}
    events = _convert(_provider(tmp_path), "permission.asked", payload)
    assert events[0].tool_input == "echo approved-ok"


def test_permission_ask_registers_a_pending_request(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider,
        "permission.v2.asked",
        {"id": "perm_1", "sessionID": "ses_1", "action": "edit", "resources": ["/workspace/a.py"]},
    )
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].request_id == "perm_1"
    # The newer v2 shape still resolves to a usable card.
    assert events[0].tool_name == "edit"
    assert events[0].tool_input == "/workspace/a.py"
    assert "perm_1" in provider._permission_requests


def test_permission_without_an_id_is_ignored(tmp_path):
    provider = _provider(tmp_path)
    assert _convert(provider, "permission.v2.asked", {"action": "edit"}) == []
    assert provider._permission_requests == {}


def test_question_becomes_an_ask_user_question_card(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider,
        "question.v2.asked",
        {
            "id": "q_1",
            "sessionID": "ses_1",
            "questions": [{
                "question": "Which database?",
                "header": "Database",
                "multiple": False,
                "custom": True,
                "options": [{"label": "Postgres", "description": "Relational"}],
            }],
        },
    )
    assert events[0].tool_name == "AskUserQuestion"
    payload = json.loads(events[0].tool_input)
    assert payload["questions"][0]["question"] == "Which database?"
    assert payload["questions"][0]["options"] == [
        {"label": "Postgres", "description": "Relational"}
    ]
    assert payload["questions"][0]["allowCustom"] is True
    assert "q_1" in provider._question_requests


def test_question_without_questions_is_ignored(tmp_path):
    provider = _provider(tmp_path)
    assert _convert(provider, "question.v2.asked", {"id": "q", "questions": []}) == []
    assert provider._question_requests == {}


def test_idle_status_is_not_surfaced_as_activity(tmp_path):
    assert _convert(_provider(tmp_path), "session.status", {"status": {"type": "idle"}}) == []


def test_busy_status_is_not_rendered_as_a_message(tmp_path):
    """opencode repeats `busy` through a turn and the PWA renders a system
    event as a visible row, which printed a column of "busy" lines above the
    reply. The streaming events already show the turn is running."""
    assert _convert(_provider(tmp_path), "session.status", {"status": {"type": "busy"}}) == []


def test_unknown_events_are_ignored(tmp_path):
    assert _convert(_provider(tmp_path), "pty.created", {"ptyID": "x"}) == []


# ── replies ─────────────────────────────────────────────────────────────


def test_permission_reply_for_an_unknown_request_is_refused(tmp_path):
    assert _provider(tmp_path).send_permission_response("nope", True) is False


def test_question_reply_for_an_unknown_request_is_refused(tmp_path):
    assert _provider(tmp_path).send_question_response("nope", {}) is False


def test_tool_use_id_for_unknown_request_is_empty(tmp_path):
    assert _provider(tmp_path).tool_use_id_for_request("nope") == ""


# ── real captured stream ────────────────────────────────────────────────


def _live_events() -> list[dict]:
    lines = (FIXTURES / "live_events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_live_fixture_parses_without_raising(tmp_path):
    """Replay a real captured SSE stream through the translator."""
    provider = _provider(tmp_path)
    events = _live_events()
    assert events[0]["type"] == "server.connected"
    # Nothing in a real stream should raise, whatever the variant.
    for event in events:
        provider._event_to_stream(event)


def test_a_failure_is_reported_before_idle_ends_the_turn():
    """The turn loop stops at `session.idle`, so the error must precede it.

    opencode emits the failure once before idle and repeats it afterwards with
    a bundler stack appended. `run_streaming` breaks at idle and never sees the
    repeat, so this ordering is what makes a failed turn report as failed.
    """
    kinds = [event["type"] for event in _live_events()]
    assert "session.error" in kinds and "session.idle" in kinds
    assert kinds.index("session.error") < kinds.index("session.idle")


def test_live_fixture_error_is_reported_without_a_stack(tmp_path):
    lines = (FIXTURES / "live_events.jsonl").read_text(encoding="utf-8").splitlines()
    errors = [
        json.loads(line)["properties"]["error"]
        for line in lines
        if line.strip() and json.loads(line)["type"] == "session.error"
    ]
    assert errors, "fixture should contain a session.error"
    for error in errors:
        text = error_text(error)
        assert "\n" not in text
        assert "$bunfs" not in text


# ── the real streaming path (`message.part.*`) ──────────────────────────
#
# `turn_with_tool.jsonl` is a full turn captured from a live opencode server
# against a free model: reasoning, a bash tool call, assistant text, and the
# usage totals. This is the event family opencode actually emits; the
# `session.next.*` cases above are a forward-compatible path that this build
# does not use for ordinary turns.


def _replay(provider: OpencodeProvider, name: str = "turn_with_tool.jsonl"):
    events = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.extend(provider._event_to_stream(json.loads(line)))
    return events


def _joined(events, kind: str) -> str:
    return "".join(e.text for e in events if e.type == kind)


def test_real_turn_yields_the_assistant_answer(tmp_path):
    events = _replay(_provider(tmp_path))
    assert "DONE" in _joined(events, "text")


def test_real_turn_keeps_reasoning_out_of_the_answer(tmp_path):
    """Regression: a ReasoningPart also stores its content in a `text` field.

    Keying off the delta's `field` alone merged the model's private reasoning
    into the visible reply. The part's tracked type is what separates them.
    """
    events = _replay(_provider(tmp_path))
    thinking = _joined(events, "thinking")
    text = _joined(events, "text")
    assert thinking, "the captured turn contains reasoning"
    assert thinking not in text
    assert not text.startswith(thinking[:20])


def test_real_turn_does_not_duplicate_text(tmp_path):
    """opencode sends each token twice: as a delta and in the settled part.

    Emitting both would double every character.
    """
    events = _replay(_provider(tmp_path))
    text = _joined(events, "text")
    assert text.count("DONE") == 1


def test_real_turn_does_not_replay_the_user_prompt(tmp_path):
    """The submitted user part is echoed back; the bubble already exists."""
    events = _replay(_provider(tmp_path))
    assert "Then reply DONE" not in _joined(events, "text")


def test_real_turn_reports_the_tool_call_and_its_result(tmp_path):
    events = _replay(_provider(tmp_path))
    tools = [(e.type, e.tool_name) for e in events if e.type in {"tool_use", "tool_result"}]
    assert ("tool_use", "bash") in tools
    assert ("tool_result", "bash") in tools


def test_a_tool_call_is_announced_exactly_once(tmp_path):
    """Running updates repeat; only the first should surface as a new call."""
    events = _replay(_provider(tmp_path))
    starts = [e for e in events if e.type == "tool_use" and e.tool_name == "bash"]
    assert len(starts) == 1


def test_real_turn_records_usage_and_cost(tmp_path):
    provider = _provider(tmp_path)
    _replay(provider)
    assert provider._usage.get("inputTokens")
    assert provider._cost is not None


def test_real_turn_records_the_model_opencode_actually_ran(tmp_path):
    """A chat may let opencode choose, so the request carries no model.

    Echoing `request.model` back as the effective model then recorded an empty
    string forever and the chat header had nothing to show; the assistant
    message is the only place the resolved `providerID/modelID` appears.
    """
    provider = _provider(tmp_path)
    _replay(provider)
    assert provider._effective_model == "opencode/big-pickle"


def test_replaying_a_turn_twice_is_clean_after_reset(tmp_path):
    """Per-turn state must not leak between turns on a reused provider."""
    provider = _provider(tmp_path)
    first = _joined(_replay(provider), "text")
    provider._reset_turn_state()
    second = _joined(_replay(provider), "text")
    assert first == second


def test_turn_fixture_carries_no_private_paths(tmp_path):
    raw = (FIXTURES / "turn_with_tool.jsonl").read_text(encoding="utf-8")
    for needle in ("raffaelefarinaro", "claude-501", "OPENCODE_SERVER_PASSWORD", "Bearer "):
        assert needle not in raw


# ── server lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_server_that_fails_validation_is_reaped(tmp_path, monkeypatch):
    """A server we could not validate must not outlive the attempt."""
    provider = _provider(tmp_path)
    terminated: list[str] = []

    class FakeProcess:
        returncode = None

        def terminate(self):
            terminated.append("terminate")
            FakeProcess.returncode = 0

        def kill(self):  # pragma: no cover - only on a hung process
            terminated.append("kill")

        async def wait(self):
            return 0

    async def fake_exec(*_args, **_kwargs):
        return FakeProcess()

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    async def boom(self):
        raise RuntimeError("incompatible build")

    monkeypatch.setattr(OpencodeProvider, "_await_health", boom)

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    with pytest.raises(RuntimeError, match="incompatible build"):
        await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert terminated == ["terminate"]
    assert provider._process is None
    assert provider._client is None
    FakeProcess.returncode = None


@pytest.mark.asyncio
async def test_missing_binary_names_the_override_env_var(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: None
    )

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    with pytest.raises(FileNotFoundError, match="CIAO_OPENCODE_BIN"):
        await provider._ensure_server(Request())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_servers_stderr_is_drained_and_kept_for_errors(tmp_path, monkeypatch):
    """An unread pipe blocks the child once the OS buffer fills.

    `opencode serve` logs for the whole life of a chat, so a piped stream nobody
    reads eventually wedges the server mid-turn. stdout is discarded outright
    and stderr is drained into a bounded tail, which also gives a failed startup
    a readable cause instead of a bare exit code.
    """
    provider = _provider(tmp_path)
    spawn_kwargs: dict = {}

    class FakeStderr:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    class FakeProcess:
        returncode = 3
        stderr = FakeStderr([b"listening\n", b"port already in use\n"])

        def terminate(self):  # pragma: no cover - process already exited
            pass

        async def wait(self):
            return 3

    async def fake_exec(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    with pytest.raises(RuntimeError, match="port already in use"):
        await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert spawn_kwargs["stdout"] is asyncio.subprocess.DEVNULL, "nothing reads stdout"
    assert spawn_kwargs["stderr"] is asyncio.subprocess.PIPE


@pytest.mark.asyncio
async def test_disconnect_is_safe_before_any_server_started(tmp_path):
    await _provider(tmp_path).disconnect()


def test_live_fixture_carries_no_credentials(tmp_path):
    """Guard the fixture itself: it must never gain secrets on a re-capture."""
    raw = (FIXTURES / "live_events.jsonl").read_text(encoding="utf-8")
    for needle in ("OPENCODE_SERVER_PASSWORD", "Authorization", "sk-", "Bearer "):
        assert needle not in raw


# ── /api/models contract the PWA reads ──────────────────────────────────


def test_models_route_reports_opencode_capabilities(tmp_path, monkeypatch):
    """The PWA gates its UI on `providers[].capabilities`, not on provider ids.

    In particular the composer's "this queues rather than steers" note keys off
    `steer`, so the flag has to survive the round trip to the API.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from ciao.config import CiaoConfig
    from ciao.providers.codex import CodexProvider
    from ciao.web.routes_api import list_models

    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    monkeypatch.setattr(CodexProvider, "model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[
        {"model": "opencode/big-pickle", "label": "Big Pickle (opencode)"},
    ]))
    request = Request({
        "type": "http", "method": "GET", "path": "/api/models",
        "headers": [], "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        "path_params": {},
    })

    payload = json.loads(asyncio.run(list_models(request)).body)

    by_id = {item["id"]: item for item in payload["providers"]}
    assert by_id["opencode"]["capabilities"]["steer"] is False
    assert by_id["claude"]["capabilities"]["steer"] is True
    assert by_id["codex"]["capabilities"]["steer"] is True
    assert by_id["opencode"]["capabilities"]["background_subagents"] is True
    assert by_id["opencode"]["short_label"] == "opencode"
    assert payload["opencode_models"] == ["opencode/big-pickle"]
    assert payload["backends"]["opencode"] is True


# ── credential reporting ────────────────────────────────────────────────


def test_credential_count_reads_the_reported_number(monkeypatch):
    """Captured verbatim from `opencode auth list` with an empty store.

    Regression: counting non-empty lines counted the ANSI reset and the
    box-drawing characters, so an empty store was reported as several
    authenticated providers.
    """
    from types import SimpleNamespace

    import ciao.providers.opencode as mod

    output = "\x1b[0m\n┌  Credentials \x1b[90m~/.local/share/opencode/auth.json\n│\n└  0 credentials\n"
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout=output, returncode=0)
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 0


def test_credential_count_parses_a_populated_store(monkeypatch):
    from types import SimpleNamespace

    import ciao.providers.opencode as mod

    output = "┌  Credentials\n│  anthropic\n│  openai\n└  2 credentials\n"
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout=output, returncode=0)
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 2


def test_credential_count_is_unknown_when_the_cli_fails(monkeypatch):
    import ciao.providers.opencode as mod

    def boom(*_a, **_k):
        raise OSError("nope")

    monkeypatch.setattr("subprocess.run", boom)
    assert mod._credential_count("/bin/opencode", timeout=1.0) is None


def test_status_does_not_claim_authentication_without_credentials(monkeypatch):
    """The free tier works with zero credentials, so `ok` stays true — but the
    detail must not imply the user connected something."""
    import ciao.providers.opencode as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr(mod, "_credential_count", lambda *_a, **_k: 0)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: __import__("types").SimpleNamespace(stdout="1.18.18\n"))

    status = mod.opencode_login_status()

    assert status["ok"] is True
    assert status["auth"] == "free"
    assert "authenticated" not in status["detail"]
    assert "free models only" in status["detail"]


# ── chat model validation ───────────────────────────────────────────────


def test_dynamic_catalog_providers_skip_the_configured_model_check():
    """Regression: selecting an opencode model returned a 400.

    `_validate_configured_model` checks a free-text id against the
    Claude/Ollama/OpenRouter lists. Those do not describe a provider that
    serves its own catalog, so `opencode/hy3-free` was rejected with
    "Unknown model ... (configured models: opus, sonnet, haiku, ...)". The
    exemption is keyed on the `dynamic_models` capability so it covers any
    such provider, not just Codex by name.
    """
    from ciao.provider_service import capabilities_for

    assert capabilities_for("opencode").dynamic_models is True
    assert capabilities_for("codex").dynamic_models is True
    # Claude's catalog is configured, so it must stay validated.
    assert capabilities_for("claude").dynamic_models is False
    # An unknown provider must not be waved through.
    assert capabilities_for("nope").dynamic_models is False


def _chat_manager(tmp_path: Path):
    from ciao.config import CiaoConfig
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    runtime = tmp_path / ".runtime"
    runtime.mkdir(exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "archives"),
        path=runtime / "web_projects.json",
    )


def test_an_opencode_model_id_is_accepted(tmp_path):
    """`opencode/hy3-free` must not be rejected against the Claude model list."""
    manager = _chat_manager(tmp_path)
    manager._validate_configured_model("opencode/hy3-free", "opencode")


def test_an_unconfigured_claude_model_is_still_rejected(tmp_path):
    """The exemption must not disable validation for configured-catalog providers."""
    from ciao.web.project_chats import UnknownModelError

    manager = _chat_manager(tmp_path)
    with pytest.raises(UnknownModelError):
        manager._validate_configured_model("totally-made-up-model", "claude")


# ── catalog caching ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_catalog_is_cached_between_calls(tmp_path, monkeypatch):
    """/api/models is hit on every picker open; a server spawn each time is ~2s."""
    import ciao.providers.opencode as mod

    mod._MODEL_CACHE.clear()
    calls = {"n": 0}

    class FakeClient:
        async def get(self, _path):
            calls["n"] += 1
            return SimpleNamespaceResponse()

    class SimpleNamespaceResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "connected": ["opencode"],
                "all": [{"id": "opencode", "models": {"m": {"id": "m", "name": "M"}}}],
            }

    class FakeServer:
        def __init__(self, _root):
            pass

        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(mod, "_EphemeralServer", FakeServer)

    first = await mod.OpencodeProvider.model_catalog(tmp_path)
    second = await mod.OpencodeProvider.model_catalog(tmp_path)

    assert first == second
    assert calls["n"] == 1, "second call must be served from cache"

    forced = await mod.OpencodeProvider.model_catalog(tmp_path, force=True)
    assert forced == first
    assert calls["n"] == 2, "force must bypass the cache"
    mod._MODEL_CACHE.clear()


@pytest.mark.asyncio
async def test_an_empty_catalog_is_cached_only_briefly(tmp_path, monkeypatch):
    """An empty result must be cached, but must not hide models for long.

    Not caching it at all meant every /api/models request — which is on the
    PWA's load path — paid another server spawn, and the full health-poll
    deadline when the binary exists but never answers. Caching it for the full
    TTL would hide the models for five minutes once opencode starts working, so
    the empty result gets its own much shorter TTL.
    """
    import ciao.providers.opencode as mod

    mod._MODEL_CACHE.clear()
    starts = {"n": 0}

    class FakeServer:
        def __init__(self, _root):
            starts["n"] += 1

        async def __aenter__(self):
            return None  # opencode not installed / server failed to start

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(mod, "_EphemeralServer", FakeServer)

    assert await mod.OpencodeProvider.model_catalog(tmp_path) == []
    assert await mod.OpencodeProvider.model_catalog(tmp_path) == []
    assert starts["n"] == 1, "a repeat call must not spawn another server"

    assert mod._EMPTY_MODEL_CACHE_TTL < mod._MODEL_CACHE_TTL
    # Age the entry past the empty TTL: the next call must look again.
    stamp, catalog = mod._MODEL_CACHE[str(tmp_path)]
    mod._MODEL_CACHE[str(tmp_path)] = (stamp - mod._EMPTY_MODEL_CACHE_TTL - 1, catalog)
    assert await mod.OpencodeProvider.model_catalog(tmp_path) == []
    assert starts["n"] == 2
    mod._MODEL_CACHE.clear()


# ── Ciaobot control-plane MCP ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_control_plane_mcp_is_attached_with_a_literal_token(tmp_path):
    """Ciaobot's own MCP must reach the chat, or opencode has no memory/vault.

    Two things are pinned. First that it is registered at all: Claude gets it
    via `options.mcp_servers` and Codex via `-c mcp_servers.ciaobot.*`, and
    opencode initially got neither, so those chats silently had no control
    plane. Second that the token is literal: opencode's `{env:VAR}`
    interpolation is a config-file feature and is NOT applied to configs
    registered through the API — the placeholder went out verbatim and the
    control plane would have rejected it.
    """
    from ciao.models import AgentRequest

    provider = _provider(tmp_path)
    posted: dict = {}

    class FakeClient:
        async def post(self, path, json=None):
            posted["path"] = path
            posted["body"] = json
            return SimpleResponse()

    class SimpleResponse:
        status_code = 200
        text = ""

    provider._client = FakeClient()  # type: ignore[assignment]
    request = AgentRequest(
        prompt="hi", model="opencode/big-pickle", mode="bypass", provider="opencode",
        mcp_url="http://127.0.0.1:1234/mcp", mcp_token="tok-abc", mcp_required=True,
    )

    await provider._register_control_plane(request)

    assert posted["path"] == "/mcp"
    assert posted["body"]["name"] == "ciaobot"
    config = posted["body"]["config"]
    assert config["url"] == "http://127.0.0.1:1234/mcp"
    assert config["headers"]["Authorization"] == "Bearer tok-abc"
    assert "{env:" not in config["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_a_refused_control_plane_is_fatal_only_when_required(tmp_path):
    from ciao.models import AgentRequest

    class Refusing:
        async def post(self, _path, json=None):
            class R:
                status_code = 500
                text = "nope"
            return R()

    def _request(required: bool):
        return AgentRequest(
            prompt="hi", model="m", mode="bypass", provider="opencode",
            mcp_url="http://127.0.0.1:1/mcp", mcp_token="t", mcp_required=required,
        )

    provider = _provider(tmp_path)
    provider._client = Refusing()  # type: ignore[assignment]
    # Optional: degrade rather than break the turn.
    await provider._register_control_plane(_request(False))
    with pytest.raises(RuntimeError, match="refused the Ciaobot MCP"):
        await provider._register_control_plane(_request(True))


@pytest.mark.asyncio
async def test_no_control_plane_registration_without_a_token(tmp_path):
    from ciao.models import AgentRequest

    calls = []

    class Recording:
        async def post(self, path, json=None):
            calls.append(path)

    provider = _provider(tmp_path)
    provider._client = Recording()  # type: ignore[assignment]
    await provider._register_control_plane(
        AgentRequest(prompt="hi", model="m", mode="bypass", provider="opencode")
    )
    assert calls == []


# ── control-plane auto-approval ─────────────────────────────────────────


def test_control_plane_tools_do_not_prompt_outside_plan_mode():
    """Regression: `ciaobot_project_files_list` raised an Approve/Deny card in
    auto mode. Ciaobot's own bookkeeping is not a third-party tool the operator
    should confirm call by call — Claude allows these via allowed_tools, and
    opencode needs them as session permission rules."""
    from ciao.execution_modes import MCP_SERVER_NAME

    for mode in ("normal", "auto", "bypass"):
        allowed = {
            rule["permission"]
            for rule in mode_settings(mode)[1]
            if rule["action"] == "allow"
        }
        assert f"{MCP_SERVER_NAME}_project_files_list" in allowed, mode
        assert f"{MCP_SERVER_NAME}_chat_send" in allowed, mode


def test_destructive_control_plane_tools_still_prompt():
    """The allow list is enumerated, not globbed. A `ciaobot_*` rule would also
    wave through the deletes and lifecycle actions deliberately kept out of
    AUTO_APPROVED_MCP_TOOLS."""
    from ciao.execution_modes import MCP_SERVER_NAME

    allowed = {
        rule["permission"]
        for rule in mode_settings("auto")[1]
        if rule["action"] == "allow"
    }
    for destructive in (
        "chat_delete", "project_delete", "chat_stop",
        "schedule_action", "loop_action", "project_complete",
        "background_run_start", "background_run_cancel",
    ):
        assert f"{MCP_SERVER_NAME}_{destructive}" not in allowed, destructive
    # And no wildcard snuck in that would cover them.
    assert not any(r["permission"].endswith("*") and r["action"] == "allow"
                   for r in mode_settings("auto")[1])


def test_plan_mode_grants_no_control_plane_allowance():
    """Plan's contract is propose-don't-act; an allow rule would hole it."""
    from ciao.execution_modes import MCP_SERVER_NAME

    assert not any(
        rule["permission"].startswith(f"{MCP_SERVER_NAME}_")
        for rule in mode_settings("plan")[1]
    )


def test_the_allow_list_tracks_the_shared_auto_approved_tuple():
    """So a tool added to AUTO_APPROVED_MCP_TOOLS reaches opencode too, instead
    of silently prompting only on this provider."""
    from ciao.execution_modes import AUTO_APPROVED_MCP_TOOLS, MCP_SERVER_NAME
    from ciao.providers.opencode import control_plane_permission_rules

    got = {rule["permission"] for rule in control_plane_permission_rules()}
    assert got == {f"{MCP_SERVER_NAME}_{tool}" for tool in AUTO_APPROVED_MCP_TOOLS}


# ── activity-row rendering ──────────────────────────────────────────────


def test_a_pending_tool_is_not_announced_before_its_arguments_arrive(tmp_path):
    """Captured live: a `pending` tool part carries `input={}` and the real
    arguments only land at `running`. Announcing at pending rendered the tool
    with no detail beside it."""
    provider = _provider(tmp_path)
    pending = {"part": {
        "type": "tool", "id": "prt_1", "callID": "c1", "tool": "bash",
        "state": {"status": "pending", "input": {}},
    }}
    assert provider._part_updated(pending) == []

    running = {"part": {
        "type": "tool", "id": "prt_1", "callID": "c1", "tool": "bash",
        "state": {"status": "running", "input": {"command": "echo hi"}},
    }}
    events = provider._part_updated(running)
    assert len(events) == 1
    assert events[0].tool_name == "bash"
    assert events[0].tool_input == "echo hi"


def test_a_tool_with_genuinely_empty_input_is_still_announced(tmp_path):
    """Skipping pending must not swallow a tool that takes no arguments."""
    provider = _provider(tmp_path)
    events = provider._part_updated({"part": {
        "type": "tool", "id": "prt_2", "callID": "c2", "tool": "list",
        "state": {"status": "running", "input": {}},
    }})
    assert len(events) == 1
    assert events[0].tool_name == "list"
    assert events[0].tool_input == ""


def test_an_empty_argument_map_summarizes_to_nothing():
    """It used to print a literal "{}" next to the tool name."""
    from ciao.providers.opencode import _summarize_tool_input

    assert _summarize_tool_input("bash", {}) == ""
    assert _summarize_tool_input("bash", {"command": "ls"}) == "ls"


def test_permission_events_match_the_house_convention(tmp_path):
    """Claude and Codex both emit `system` with "Approve use of X?". A different
    type plus a restated "opencode wants to use bash" rendered as an extra
    transcript line beside the approval card."""
    events = _convert(_provider(tmp_path), "permission.asked", LIVE_PERMISSION)
    assert events[0].type == "system"
    assert events[0].message == "Approve use of bash?"


def test_dollar_brace_is_not_opencode_interpolation_syntax():
    """Verified against opencode 1.18.x, whose config substitution is
    ``/\\{env:([^}]+)\\}/g``: ``${VAR}`` is passed through verbatim. That is how
    a literal ``${NOTION_TOKEN}`` reached the Notion MCP server as a bearer
    token and came back 401.
    """
    problems = config_placeholder_problems(
        {"mcp": {"notion": {"environment": {"NOTION_TOKEN": "${NOTION_TOKEN}"}}}},
        {"NOTION_TOKEN": "ntn_real"},
    )
    assert len(problems) == 1
    assert "{env:NOTION_TOKEN}" in problems[0]


def test_a_missing_variable_resolves_to_an_empty_credential():
    """opencode's substitution ends in ``|| ""``, so this is silent at spawn and
    surfaces only as a 401 on the first tool call."""
    problems = config_placeholder_problems(
        {"mcp": {"notion": {"environment": {"NOTION_TOKEN": "{env:NOTION_TOKEN}"}}}},
        {},
    )
    assert len(problems) == 1
    assert "empty string" in problems[0]


def test_correct_syntax_with_the_variable_set_is_silent():
    config = {"mcp": {"notion": {"environment": {"NOTION_TOKEN": "{env:NOTION_TOKEN}"}}}}
    assert config_placeholder_problems(config, {"NOTION_TOKEN": "ntn_real"}) == ()


def test_placeholders_are_found_through_lists_and_nesting():
    config = {
        "mcp": {"n8n": {"headers": {"Authorization": "Bearer {env:N8N}"}}},
        "command": ["npx", "-y", "${PKG}"],
    }
    assert set(unresolved_placeholders(config)) == {"{env:N8N}", "${PKG}"}


def test_api_registered_configs_are_reported_regardless_of_the_environment():
    """``{env:VAR}`` is a config-*file* feature; configs registered over ``/mcp``
    are not interpolated, so a set variable does not make one safe."""
    config = {"headers": {"Authorization": "Bearer {env:CIAO_MCP_SESSION_TOKEN}"}}
    assert unresolved_placeholders(config) == ("{env:CIAO_MCP_SESSION_TOKEN}",)


def test_the_workspace_config_is_the_file_opencode_will_load(tmp_path):
    (tmp_path / "opencode.json").write_text(
        json.dumps(
            {"mcp": {"notion": {"environment": {"NOTION_TOKEN": "${NOTION_TOKEN}"}}}}
        ),
        encoding="utf-8",
    )
    problems = workspace_config_placeholder_problems(tmp_path, {"NOTION_TOKEN": "x"})
    assert len(problems) == 1


def test_a_workspace_without_a_config_is_not_a_problem(tmp_path):
    assert workspace_config_placeholder_problems(tmp_path, {}) == ()


def test_unparseable_config_is_left_alone(tmp_path):
    """jsonc comments are legal for opencode and are not ours to diagnose."""
    (tmp_path / "opencode.json").write_text("{ // comment\n}", encoding="utf-8")
    assert workspace_config_placeholder_problems(tmp_path, {}) == ()
