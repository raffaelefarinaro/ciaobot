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
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    PermissionRequestEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.providers.opencode import (
    OpencodeProvider,
    OpencodeSettings,
    _catalog_from_providers,
    _context_window_for,
    _log_catalog_change,
    catalog_providers,
    compose_system,
    config_placeholder_problems,
    error_text,
    model_accepts_images,
    missing_required_paths,
    mode_settings,
    opencode_collab_tree_counts,
    opencode_default_model,
    resolve_opencode_binary,
    _session_handover_text,
    split_model,
    unresolved_placeholders,
    usage_payload,
    workspace_config_placeholder_problems,
)
from ciao.execution_modes import MCP_SERVER_NAME

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


def _provider(tmp_path: Path) -> OpencodeProvider:
    return OpencodeProvider(tmp_path)


# ── capabilities ────────────────────────────────────────────────────────


def test_quota_is_unsupported():
    """Bring-your-own-provider: there is no unified quota snapshot to report."""
    assert OpencodeProvider.capabilities.quota is False


def test_background_subagents_are_supported():
    """Child sessions carry parentID, so background agents are inspectable."""
    assert OpencodeProvider.capabilities.background_subagents is True
    assert OpencodeProvider.capabilities.subagent_messages is True


@pytest.mark.asyncio
async def test_steer_never_sends_a_second_prompt(tmp_path):
    """Returning False keeps the message in the next-turn queue.

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


def _actions(mode: BridgeMode) -> dict[str, str]:
    """Flatten a ruleset to {permission: action} for readable assertions."""
    return {rule["permission"]: rule["action"] for rule in mode_settings(mode)[1]}


def test_compose_system_puts_instructions_before_runtime_facts():
    assert compose_system("Reply with only a title.", "today=2026-08-14") == (
        "Reply with only a title.\n\ntoday=2026-08-14"
    )


def test_compose_system_keeps_either_half_alone():
    # A chat supplies no instructions; a one-shot in a bare env has no runtime.
    assert compose_system("", "today=2026-08-14") == "today=2026-08-14"
    assert compose_system("Only the title.", "") == "Only the title."


def test_compose_system_is_empty_when_both_halves_are():
    # An empty result means "send no `system` field at all".
    assert compose_system("", "") == ""
    assert compose_system("   ", "\n") == ""


def test_normal_opencode_chat_uses_core_without_memory_duplication(tmp_path):
    provider = OpencodeProvider(tmp_path)
    request = AgentRequest(
        prompt="hello",
        model="",
        mode="auto",
        provider="opencode",
        control_surface="mcp",
    )
    instructions = provider._chat_system_instructions(request)
    assert "Ciaobot core instructions" in instructions
    assert "native workspace guide" in instructions
    assert "MEMORY (your personal notes)" not in instructions


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


def test_auto_allows_everything_but_keeps_shell_and_destructive_mcp_gated():
    """Auto's permissive default allows every tool outright; only bash and the
    destructive control-plane tools stay behind an ask so their events reach
    the classifier/operator."""
    actions = _actions("auto")
    assert actions["*"] == "allow"
    assert actions["bash"] == "ask"
    assert "edit" not in actions
    assert actions[f"{MCP_SERVER_NAME}_chat_delete"] == "ask"
    assert actions[f"{MCP_SERVER_NAME}_background_run_start"] == "ask"


def test_plan_mode_is_read_only():
    actions = _actions("plan")
    assert actions["read"] == "allow"
    assert actions["*"] == "ask"
    assert "edit" not in actions


def test_tools_can_be_disabled_for_one_shot_sessions():
    agent, rules = mode_settings("plan", tools_enabled=False)
    assert agent == "plan"
    assert rules == [{"permission": "*", "pattern": "*", "action": "deny"}]


class _SessionResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _SessionClient:
    def __init__(self, payload: object, messages: object | None = None) -> None:
        self.payload = payload
        self.messages = messages if messages is not None else []
        self.get_calls: list[str] = []
        self.post_calls: list[tuple[str, object]] = []

    async def get(self, path: str):
        self.get_calls.append(path)
        payload = self.messages if path.endswith("/message") else self.payload
        return _SessionResponse(payload)

    async def post(self, path: str, json=None):
        self.post_calls.append((path, json))
        return _SessionResponse({"id": "session-new"})


@pytest.mark.asyncio
async def test_resume_rotates_when_session_permission_is_stale(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient({
        "id": "session-old",
        "permission": mode_settings("bypass")[1],
    }, messages=[
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Earlier request"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Earlier answer"}]},
    ])
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="continue",
        model="",
        mode="normal",
        provider="opencode",
        resume_session="session-old",
        stable_context_prefix="[stable context]\n",
    )
    expected = mode_settings("normal")[1]

    assert await provider._ensure_session(request) == "session-new"
    assert client.get_calls == ["/session/session-old", "/session/session-old/message"]
    assert "User: Earlier request" in provider._session_handover_context
    assert "Assistant: Earlier answer" in provider._session_handover_context
    assert request.prompt.startswith("[stable context]\n")
    assert client.post_calls == [
        ("/session", {"agent": "build", "permission": expected})
    ]


@pytest.mark.asyncio
async def test_resume_keeps_session_when_permission_matches(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient({
        "id": "session-old",
        "permission": mode_settings("normal")[1],
    })
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="continue",
        model="",
        mode="normal",
        provider="opencode",
        resume_session="session-old",
    )

    assert await provider._ensure_session(request) == "session-old"
    assert client.get_calls == ["/session/session-old"]
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_session_passes_through_unqualified_model(tmp_path):
    provider = _provider(tmp_path)

    class _CatalogClient(_SessionClient):
        async def get(self, path: str):
            if path == "/provider":
                self.get_calls.append(path)
                return _SessionResponse({
                    "connected": ["anthropic"],
                    "all": [{
                        "id": "anthropic",
                        "models": {
                            "claude-sonnet-4-6": {"id": "claude-sonnet-4-6"},
                        },
                    }],
                })
            return await super().get(path)

    client = _CatalogClient(None)
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="hello",
        model="sonnet",
        mode="normal",
        provider="opencode",
    )

    assert await provider._ensure_session(request) == "session-new"
    # An unqualified model id is sent as-is under an empty provider, letting
    # opencode apply its own default.
    assert client.get_calls == []
    assert client.post_calls == [(
        "/session",
        {
            "agent": "build",
            "permission": mode_settings("normal")[1],
            "model": {"id": "sonnet", "providerID": ""},
        },
    )]


def test_unknown_mode_falls_back_to_normal():
    assert mode_settings("nonsense") == mode_settings("normal")  # type: ignore[arg-type]


def test_session_handover_omits_synthetic_parts():
    history = [
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Earlier request"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Earlier answer"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "synthetic", "synthetic": True}]},
    ]

    rendered = _session_handover_text(history)

    assert "User: Earlier request" in rendered
    assert "Assistant: Earlier answer" in rendered
    assert "synthetic" not in rendered


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
        {"input": 120, "output": 40, "reasoning": 8, "cache": {"read": 900, "write": 30}, "total": 1098}
    )
    assert usage == {
        "inputTokens": "120",
        "outputTokens": "40",
        "reasoningTokens": "8",
        "cacheReadTokens": "900",
        "cacheWriteTokens": "30",
        "totalTokens": "1098",
    }


def test_usage_payload_omits_zero_counts():
    assert usage_payload({"input": 0, "output": 5, "cache": {}}) == {"outputTokens": "5"}


def test_usage_payload_tolerates_junk():
    assert usage_payload(None) == {}
    assert usage_payload({"input": "not-a-number"}) == {}


def test_context_window_for_reads_the_model_limit():
    payload = {
        "all": [{
            "id": "anthropic",
            "models": {
                "claude-sonnet-4-6": {"id": "claude-sonnet-4-6", "limit": {"context": 200000}},
            },
        }],
    }
    assert _context_window_for(payload, "anthropic", "claude-sonnet-4-6") == 200000


def test_context_window_for_returns_none_when_unstated_or_unknown():
    payload = {
        "all": [{
            "id": "anthropic",
            "models": {
                "m": {"id": "m"},
                "no-limit": {"id": "no-limit", "limit": {"context": 0}},
            },
        }],
    }
    assert _context_window_for(payload, "anthropic", "m") is None
    assert _context_window_for(payload, "anthropic", "no-limit") is None
    assert _context_window_for(payload, "unknown", "m") is None
    assert _context_window_for(None, "anthropic", "m") is None


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


# ── image capability ────────────────────────────────────────────────────
# opencode is bring-your-own-backend, so it is the one provider where a user can
# pin a model that cannot take an image. Its catalog states this per model, which
# is what lets the image pre-flight stop guessing from model-name families.


def test_model_accepts_images_reads_the_input_modality():
    """`capabilities.input.image` is the authoritative per-model answer.

    Shape captured live from opencode 1.18's `GET /provider`.
    """
    capable = {
        "id": "m",
        "capabilities": {
            "attachment": True,
            "input": {"text": True, "image": True, "pdf": False},
        },
    }
    text_only = {
        "id": "m",
        "capabilities": {
            "attachment": False,
            "input": {"text": True, "image": False, "pdf": False},
        },
    }
    assert model_accepts_images(capable) is True
    assert model_accepts_images(text_only) is False


def test_model_accepts_images_falls_back_to_the_attachment_flag():
    """`attachment` covers any non-text input, so it can only rule vision out.

    attachment=false is a reliable no; attachment=true says "some attachment"
    and could mean pdf or audio, so it stays unknown rather than a false yes.
    """
    assert model_accepts_images({"capabilities": {"attachment": False}}) is False
    assert model_accepts_images({"capabilities": {"attachment": True}}) is None


def test_model_accepts_images_is_unknown_when_unstated():
    """Older builds omit the block; unknown must not read as a refusal."""
    assert model_accepts_images({"id": "m"}) is None
    assert model_accepts_images({"id": "m", "capabilities": "junk"}) is None
    assert model_accepts_images({"capabilities": {"input": {"image": "yes"}}}) is None


def test_catalog_states_image_support_only_when_opencode_does():
    """An absent `images` key means unknown -- distinct from a stated False."""
    payload = {
        "connected": ["opencode"],
        "all": [{
            "id": "opencode",
            "models": {
                "seer": {
                    "id": "seer",
                    "capabilities": {"input": {"text": True, "image": True}},
                },
                "reader": {
                    "id": "reader",
                    "capabilities": {"input": {"text": True, "image": False}},
                },
                "quiet": {"id": "quiet"},
            },
        }],
    }
    by_model = {row["model"]: row for row in _catalog_from_providers(payload)}
    assert by_model["opencode/seer"]["images"] is True
    assert by_model["opencode/reader"]["images"] is False
    assert "images" not in by_model["opencode/quiet"]


def test_catalog_is_empty_when_nothing_is_connected():
    payload = {"connected": [], "all": [{"id": "anthropic", "models": {"a": {"id": "a"}}}]}
    assert _catalog_from_providers(payload) == []


def test_catalog_tolerates_junk():
    assert _catalog_from_providers(None) == []
    assert _catalog_from_providers({"all": "nope"}) == []


# ── default model ───────────────────────────────────────────────────────


def test_default_model_override():
    class Config:
        opencode = OpencodeSettings(default_model="anthropic/claude-sonnet-4-6")

    assert opencode_default_model(Config()) == "anthropic/claude-sonnet-4-6"


def test_default_model_on_a_config_without_opencode():
    assert opencode_default_model(object()) == ""


# ── collaboration tree counts ────────────────────────────────────────────
# opencode session objects carry no status field; the running count comes from
# each child's own messages (see `_opencode_child_status` in routes_api).


def test_collab_tree_counts_derive_running_from_child_messages():
    tree = [
        # A turn still in flight: time.created without time.completed.
        {"info": {"id": "a"}, "messages": [
            {"info": {"role": "user"}, "parts": []},
            {"info": {"role": "assistant", "time": {"created": 1}}, "parts": []},
        ]},
        # A finished child.
        {"info": {"id": "b"}, "messages": [
            {"info": {"role": "assistant", "time": {"created": 1, "completed": 2}}, "parts": []},
        ]},
        # A failed child: its last assistant message carries an error.
        {"info": {"id": "c"}, "messages": [
            {"info": {"role": "assistant", "error": {"name": "E"}}, "parts": []},
        ]},
        # A child with no assistant messages counts as completed, not running.
        {"info": {"id": "d"}, "messages": [{"info": {"role": "user"}, "parts": []}]},
    ]
    running, had_subagents = opencode_collab_tree_counts(tree)
    assert running == 1
    assert had_subagents is True


def test_collab_tree_counts_are_empty_for_an_empty_tree():
    assert opencode_collab_tree_counts([]) == (0, False)


def test_collab_tree_counts_tolerate_junk():
    assert opencode_collab_tree_counts([None, {"info": {}}, {"messages": "nope"}]) == (0, True)


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
    provider = _provider(tmp_path)
    provider._current_mode = "normal"  # the mode the event was captured under
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_name == "bash"
    assert events[0].tool_input == "echo approved-ok"
    assert "bash" in events[0].message


def test_permission_card_links_back_to_the_tool_call(tmp_path):
    """So the UI can retract the tool card when the request is refused."""
    provider = _provider(tmp_path)
    provider._current_mode = "normal"
    _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert provider.tool_use_id_for_request("per_live1") == "call_abc"


def test_permission_card_falls_back_to_patterns_without_metadata(tmp_path):
    provider = _provider(tmp_path)
    provider._current_mode = "normal"
    payload = {**LIVE_PERMISSION, "metadata": {}}
    events = _convert(provider, "permission.asked", payload)
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
    assert payload["questions"][0]["isOther"] is True
    assert payload["questions"][0]["id"] == "0"
    assert "q_1" in provider._question_requests


def test_question_without_questions_is_ignored(tmp_path):
    provider = _provider(tmp_path)
    assert _convert(provider, "question.v2.asked", {"id": "q", "questions": []}) == []
    assert provider._question_requests == {}


def test_question_custom_false_is_preserved_for_the_pwa(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider,
        "question.v2.asked",
        {
            "id": "q_no_custom",
            "questions": [{
                "question": "Use the default?",
                "custom": False,
                "options": [{"label": "Yes"}],
            }],
        },
    )

    payload = json.loads(events[0].tool_input)
    assert payload["questions"][0]["isOther"] is False


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


# ── mode-aware auto-approval ────────────────────────────────────────────
# The session ruleset is fixed at creation and PATCH does not apply (see
# `_ensure_session`); mode changes rotate the session before the prompt runs.
# `permission.asked` is still answered against the *current* mode: bypass
# approves everything, auto approves verifiably read-only work, and every other
# mode surfaces the card as before.


class _RecordingPermissionClient:
    def __init__(self, *, status_code=200):
        self.calls: list[tuple[str, dict]] = []
        self.status_code = status_code

    async def post(self, path, json=None):
        self.calls.append((path, json))

        class _Response:
            status_code = self.status_code

        return _Response()


async def _drain_tasks():
    """Let the fire-and-forget reply task run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


def _armed_provider(tmp_path, mode) -> tuple[OpencodeProvider, _RecordingPermissionClient]:
    provider = _provider(tmp_path)
    client = _RecordingPermissionClient()
    provider._client = client  # type: ignore[assignment]
    provider._current_mode = mode
    return provider, client


@pytest.mark.asyncio
async def test_bypass_mode_approves_without_a_card(tmp_path):
    """Bypass means bypass: even an unsafe command is approved, card-free."""
    provider, client = _armed_provider(tmp_path, "bypass")
    payload = {**LIVE_PERMISSION, "metadata": {"command": "rm -rf /tmp/x"}}
    assert _convert(provider, "permission.asked", payload) == []
    await _drain_tasks()
    assert client.calls == [("/permission/per_live1/reply", {"reply": "once"})]
    # Answered immediately, so nothing is left pending for the operator.
    assert provider._permission_requests == {}


@pytest.mark.asyncio
async def test_auto_mode_approves_any_non_destructive_bash_command(tmp_path, caplog):
    """The permissive auto default allows any non-destructive shell command,
    not just read-only ones."""
    provider, client = _armed_provider(tmp_path, "auto")
    payload = {**LIVE_PERMISSION, "metadata": {"command": "git status && git push"}}
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        events = _convert(provider, "permission.asked", payload)
    assert events == []
    await _drain_tasks()
    assert client.calls == [("/permission/per_live1/reply", {"reply": "once"})]
    logged = [record.getMessage() for record in caplog.records]
    assert any("auto-approved bash" in line and "git push" in line for line in logged)


def test_auto_mode_surfaces_a_destructive_bash_command(tmp_path):
    provider, client = _armed_provider(tmp_path, "auto")
    payload = {**LIVE_PERMISSION, "metadata": {"command": "rm -rf /tmp/cache"}}
    events = _convert(provider, "permission.asked", payload)
    assert isinstance(events[0], PermissionRequestEvent)
    assert "per_live1" in provider._permission_requests
    assert client.calls == []


def test_auto_mode_surfaces_bash_without_a_command_to_classify(tmp_path):
    provider, client = _armed_provider(tmp_path, "auto")
    payload = {**LIVE_PERMISSION, "metadata": {}}
    events = _convert(provider, "permission.asked", payload)
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


@pytest.mark.asyncio
async def test_auto_mode_approves_a_read_only_tool_from_a_stale_ruleset(tmp_path):
    """A chat created in `normal` keeps `{"*": ask}` forever, so even `list`
    raises asks once the operator switches it to auto. Answer those here."""
    provider, client = _armed_provider(tmp_path, "auto")
    events = _convert(
        provider,
        "permission.asked",
        {"id": "perm_ls", "sessionID": "ses_1", "permission": "list", "patterns": ["/workspace"]},
    )
    assert events == []
    await _drain_tasks()
    assert client.calls == [("/permission/perm_ls/reply", {"reply": "once"})]


def test_auto_mode_surfaces_a_non_read_only_tool_ask(tmp_path):
    """A stale session can still raise an *ask* for an already-allowed tool; a
    non-read-only tool ask is not auto-approved, so it keeps the card."""
    provider, client = _armed_provider(tmp_path, "auto")
    events = _convert(
        provider,
        "permission.v2.asked",
        {"id": "perm_ed", "sessionID": "ses_1", "action": "edit", "resources": ["/workspace/a.py"]},
    )
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


def test_normal_mode_surfaces_even_a_safe_command(tmp_path):
    provider, client = _armed_provider(tmp_path, "normal")
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


def test_plan_mode_surfaces_even_a_safe_command(tmp_path):
    provider, client = _armed_provider(tmp_path, "plan")
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


def test_auto_approval_needs_a_client_to_post_the_reply(tmp_path):
    """Without a client the approval could not be delivered and the turn would
    wedge with neither a card nor an answer; fail safe to the card."""
    provider = _provider(tmp_path)
    provider._current_mode = "bypass"
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)


@pytest.mark.asyncio
async def test_failed_permission_reply_keeps_request_for_retry(tmp_path):
    provider, client = _armed_provider(tmp_path, "normal")
    _convert(provider, "permission.asked", LIVE_PERMISSION)
    client.status_code = 500

    assert provider.send_permission_response("per_live1", True) is True
    await _drain_tasks()

    assert "per_live1" in provider._permission_requests


@pytest.mark.asyncio
async def test_failed_question_reply_keeps_request_for_retry(tmp_path):
    provider, client = _armed_provider(tmp_path, "normal")
    _convert(
        provider,
        "question.v2.asked",
        {
            "id": "q_retry",
            "sessionID": "ses_1",
            "questions": [{
                "question": "Which?",
                "header": "Pick",
                "options": [],
            }],
        },
    )
    client.status_code = 500

    assert provider.send_question_response("q_retry", {"q": ["answer"]}) is True
    await _drain_tasks()

    assert "q_retry" in provider._question_requests


@pytest.mark.asyncio
async def test_question_reply_uses_provider_question_order(tmp_path):
    provider, client = _armed_provider(tmp_path, "normal")
    _convert(
        provider,
        "question.v2.asked",
        {
            "id": "q_order",
            "sessionID": "ses_1",
            "questions": [
                {"id": "first", "question": "First?", "options": []},
                {"id": "second", "question": "Second?", "options": []},
            ],
        },
    )

    assert provider.send_question_response(
        "q_order", {"second": ["B"], "first": ["A"]}
    ) is True
    await _drain_tasks()

    assert client.calls == [
        ("/question/q_order/reply", {"answers": [["A"], ["B"]]})
    ]
    assert provider._question_requests == {}


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


def test_augment_context_pct_attaches_window_occupancy(tmp_path):
    """The turn's total over the model's context window becomes context_pct."""
    provider = _provider(tmp_path)
    provider._usage = {"inputTokens": "100", "outputTokens": "50", "totalTokens": "150"}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"all": [{"id": "opencode", "models": {"big-pickle": {"id": "big-pickle", "limit": {"context": 1000}}}}]}

    class _FakeClient:
        async def get(self, _path: str):
            return _FakeResponse()

    async def _run():
        await provider._augment_context_pct(_FakeClient(), ("opencode", "big-pickle"))

    asyncio.run(_run())
    assert provider._usage["context_window"] == "1000"
    assert provider._usage["context_pct"] == "15.0%"


def test_augment_context_pct_is_silent_when_limit_is_missing(tmp_path):
    provider = _provider(tmp_path)
    provider._usage = {"inputTokens": "100", "outputTokens": "50", "totalTokens": "150"}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"all": [{"id": "opencode", "models": {"big-pickle": {"id": "big-pickle"}}}]}

    class _FakeClient:
        async def get(self, _path: str):
            return _FakeResponse()

    async def _run():
        await provider._augment_context_pct(_FakeClient(), ("opencode", "big-pickle"))

    asyncio.run(_run())
    assert "context_pct" not in provider._usage


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


# ── the terminal ResultEvent ─────────────────────────────────────────────
#
# `record_turn` persists `ResultEvent.result` as the durable transcript's
# response, which is what the PWA replays when the opencode session cannot be
# read. A success that carried "" made every replayed turn render blank (#295).


class _FakeEventStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for line in self._lines:
            yield line.encode("utf-8")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeServerClient:
    """Just enough of httpx.AsyncClient for a run_streaming turn."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def stream(self, _method: str, _path: str) -> _FakeEventStream:
        return _FakeEventStream(self._lines)

    async def get(self, _path: str):
        class _Accepted:
            status_code = 404
            text = ""

            def json(self):
                return {}

        return _Accepted()

    async def post(self, _path: str, json=None):
        class _Accepted:
            status_code = 200
            text = ""

        return _Accepted()


async def _run_fixture_turn(provider, monkeypatch, name: str, session_id: str):
    from ciao.models import AgentRequest

    lines = [
        f"data: {line}\n\n"
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    client = _FakeServerClient(lines)

    async def fake_server(_request):
        return client

    async def fake_session(_request):
        return session_id

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)
    request = AgentRequest(prompt="hi", model="", mode="bypass", provider="opencode")
    return [
        event
        async for event in provider.run_streaming(request, lambda _handle: None)
    ]


@pytest.mark.asyncio
async def test_success_result_carries_the_accumulated_answer(tmp_path, monkeypatch):
    events = await _run_fixture_turn(
        _provider(tmp_path), monkeypatch,
        "turn_with_tool.jsonl", "ses_003133027ffeJooFKUT3slZ0al",
    )
    result = events[-1]
    assert result.type == "result"
    assert not result.is_error
    assert result.result == _joined(events, "text").strip()
    assert "DONE" in result.result
    assert _joined(events, "thinking") not in result.result


@pytest.mark.asyncio
async def test_current_mode_is_set_before_session_setup(tmp_path, monkeypatch):
    """A resumed session must classify setup-time permission events by this turn."""
    provider = _provider(tmp_path)
    lines = [
        f"data: {line}\n\n"
        for line in (FIXTURES / "turn_with_tool.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]
    client = _FakeServerClient(lines)

    async def fake_server(_request):
        return client

    async def fake_session(_request):
        assert provider._current_mode == "bypass"
        return "ses_current_mode"

    monkeypatch.setattr(provider, "_ensure_server", fake_server)
    monkeypatch.setattr(provider, "_ensure_session", fake_session)
    request = AgentRequest(
        prompt="hi", model="", mode="bypass", provider="opencode"
    )

    events = [
        event
        async for event in provider.run_streaming(request, lambda _handle: None)
    ]

    assert events[-1].type == "result"


@pytest.mark.asyncio
async def test_failure_result_still_carries_the_error(tmp_path, monkeypatch):
    events = await _run_fixture_turn(
        _provider(tmp_path), monkeypatch,
        "live_events.jsonl", "ses_0034015a8ffetzCVc5kq6mI2oW",
    )
    result = events[-1]
    assert result.type == "result"
    assert result.is_error
    # The error text wins over any accumulated output, and stays sanitized.
    assert result.result
    assert "\n" not in result.result


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
async def test_database_lock_during_startup_retries_after_contention(tmp_path, monkeypatch):
    """A shared opencode SQLite lock gets short in-process startup retries."""
    provider = _provider(tmp_path)
    attempts: list[int] = []
    delays: list[float] = []

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.stderr = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        return FakeProcess()

    async def fake_health():
        if len(attempts) == 1:
            raise RuntimeError("opencode serve exited with code 1: database is locked")

    async def fake_sleep(delay: float):
        delays.append(delay)

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("ciao.providers.opencode._free_port", lambda: 43123)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("ciao.providers.opencode.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(provider, "_await_health", fake_health)

    async def noop(*_args):
        return None

    monkeypatch.setattr(provider, "_verify_contract", noop)
    monkeypatch.setattr(provider, "_register_control_plane", noop)

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert attempts == [1, 2]
    assert delays == [0.25]
    await provider.disconnect()


@pytest.mark.asyncio
async def test_never_healthy_server_gets_startup_retries(tmp_path, monkeypatch):
    """A live-but-wedged server must be treated like the SQLite lock it is.

    ``opencode serve`` staying up but never answering 200 means it is blocked
    on startup (shared database migration). That is the same recoverable
    contention as ``database is locked``, so ``_ensure_server`` must retry
    rather than failing the classifier run outright.
    """
    provider = _provider(tmp_path)
    attempts: list[int] = []
    delays: list[float] = []

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.stderr = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        return FakeProcess()

    async def fake_health():
        if len(attempts) == 1:
            raise TimeoutError("opencode serve did not become healthy: server stayed alive but never answered /global/health")

    async def fake_sleep(delay: float):
        delays.append(delay)

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("ciao.providers.opencode._free_port", lambda: 43123)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("ciao.providers.opencode.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(provider, "_await_health", fake_health)

    async def noop(*_args):
        return None

    monkeypatch.setattr(provider, "_verify_contract", noop)
    monkeypatch.setattr(provider, "_register_control_plane", noop)

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert attempts == [1, 2]
    assert delays == [0.25]
    await provider.disconnect()


def test_health_failure_reason_says_what_the_poll_saw():
    """A wedged server must not trail a bare empty ``: `` in the error."""
    from ciao.providers.opencode import _health_failure_reason

    assert _health_failure_reason(503, None) == "health returned HTTP 503"
    assert _health_failure_reason(None, ConnectionRefusedError("refused")) == "refused"
    assert (
        _health_failure_reason(None, None)
        == "server stayed alive but never answered /global/health"
    )


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
async def test_mcp_token_is_not_exported_to_the_opencode_process(tmp_path, monkeypatch):
    """The MCP token is a header, never a shell-visible child credential."""
    provider = _provider(tmp_path)
    spawn_kwargs: dict = {}

    class FakeProcess:
        returncode = None
        stderr = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*_args, **kwargs):
        spawn_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("ciao.providers.opencode._free_port", lambda: 43123)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    async def noop(*_args):
        return None

    monkeypatch.setattr(provider, "_await_health", noop)
    monkeypatch.setattr(provider, "_verify_contract", noop)
    monkeypatch.setattr(provider, "_register_control_plane", noop)

    class Request:
        extra_env: dict = {"CIAO_MCP_SESSION_TOKEN": "stale-from-request-env"}
        mcp_token = "chat-scoped-secret"

    await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert "CIAO_MCP_SESSION_TOKEN" not in spawn_kwargs["env"]
    await provider.disconnect()


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

    Capabilities survive the round trip to the /api/models payload.
    """
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from ciao.config import CiaoConfig
    from ciao.web.routes_api import list_models

    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[
        {"model": "opencode/big-pickle", "label": "Big Pickle (opencode)"},
    ]))
    def _request(query: bytes = b"") -> Request:
        return Request({
            "type": "http", "method": "GET", "path": "/api/models",
            "headers": [], "app": SimpleNamespace(state=SimpleNamespace(config=config)),
            "path_params": {}, "query_string": query,
        })

    payload = json.loads(asyncio.run(list_models(_request())).body)

    by_id = {item["id"]: item for item in payload["providers"]}
    assert by_id["opencode"]["capabilities"]["background_subagents"] is True
    assert by_id["opencode"]["short_label"] == "opencode"
    assert payload["opencode_models"] == ["opencode/big-pickle"]
    assert payload["backends"]["opencode"] is True

    # `?refresh=1` bypasses the provider catalog caches, so a provider connected
    # in another window shows up without waiting out the 5-minute TTL.
    assert OpencodeProvider.model_catalog.await_args.kwargs["force"] is False
    asyncio.run(list_models(_request(b"refresh=1")))
    assert OpencodeProvider.model_catalog.await_args.kwargs["force"] is True


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
    any such provider, not just one hard-coded provider name.
    """
    from ciao.provider_service import capabilities_for

    assert capabilities_for("opencode").dynamic_models is True
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

    The registration is pinned because opencode initially got no control plane,
    so those chats silently had no memory/vault tools. The token must also be
    literal: opencode's `{env:VAR}`
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
        assert f"{MCP_SERVER_NAME}_project" in allowed, mode
        assert f"{MCP_SERVER_NAME}_chat_send" in allowed, mode


def test_destructive_control_plane_tools_still_prompt():
    """In the permissive auto default the wildcard is allow, so the destructive
    control-plane tools must be pinned to `ask` explicitly (a later, more
    specific rule wins) to keep surfacing an approval card."""
    from ciao.execution_modes import MCP_SERVER_NAME

    actions = _actions("auto")
    assert actions["*"] == "allow"
    for destructive in (
        "chat_delete", "project_action", "chat_stop",
        "schedule_action", "loop_action",
        "background_run_start", "background_run_cancel",
    ):
        assert actions[f"{MCP_SERVER_NAME}_{destructive}"] == "ask", destructive


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
    """opencode permission events use the shared approval-card convention.

    A different type plus a restated "opencode wants to use bash" rendered as
    an extra transcript line beside the approval card.
    """
    provider = _provider(tmp_path)
    provider._current_mode = "normal"
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
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


# ── catalog change logging ──────────────────────────────────────────────
# The catalog is read-through: nothing persists it, so without a log line a
# user whose model list changed has no record of which provider came or went.


def _rows(*models: str) -> list[dict[str, object]]:
    return [{"model": m, "label": m, "variants": []} for m in models]


def test_catalog_providers_reads_the_provider_half_of_each_id():
    assert catalog_providers(_rows("anthropic/sonnet", "openai/gpt", "anthropic/haiku")) == {
        "anthropic",
        "openai",
    }
    # A bare id names no provider and must not produce a phantom one.
    assert catalog_providers(_rows("sonnet")) == set()


def test_first_catalog_is_logged_once(caplog):
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        _log_catalog_change("ws", None, _rows("anthropic/sonnet", "openai/gpt"))
    assert "opencode catalog: 2 model(s) from anthropic, openai" in caplog.text


def test_an_empty_first_catalog_is_not_logged(caplog):
    """A fresh install reports nothing connected; that is not an event."""
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        _log_catalog_change("ws", None, [])
    assert caplog.text == ""


def test_connecting_and_losing_providers_is_logged(caplog):
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        _log_catalog_change(
            "ws", _rows("anthropic/sonnet"), _rows("anthropic/sonnet", "ollama/llama")
        )
    assert "connected ollama" in caplog.text

    caplog.clear()
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        _log_catalog_change(
            "ws", _rows("anthropic/sonnet", "ollama/llama"), _rows("anthropic/sonnet")
        )
    assert "lost ollama" in caplog.text


def test_an_unchanged_provider_set_stays_quiet(caplog):
    """The catalog refreshes every 5 minutes; a healthy install must not spam."""
    with caplog.at_level("INFO", logger="ciao.providers.opencode"):
        # Same providers, different models: still not a provider-set change.
        _log_catalog_change(
            "ws", _rows("anthropic/sonnet"), _rows("anthropic/sonnet", "anthropic/haiku")
        )
    assert caplog.text == ""


# ── model resolution ────────────────────────────────────────────────────
# opencode addresses models as providerID/modelID. A qualified id passes
# through; an unqualified one is sent as-is under an empty provider, letting
# opencode apply its own default.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        # Already qualified: passes straight through.
        ("anthropic/claude-sonnet-4-6", ("anthropic", "claude-sonnet-4-6")),
        # An unqualified id is sent as-is under an empty provider.
        ("sonnet", ("", "sonnet")),
        ("  Sonnet  ", ("", "Sonnet")),
        ("fable", ("", "fable")),
        ("", ("", "")),
    ],
)
async def test_resolve_model_passes_through_unqualified_ids(
    tmp_path, requested, expected
):
    class _Client:
        @staticmethod
        async def get(path: str) -> None:
            raise AssertionError(f"catalog should not be consulted for {path}")

    provider = _provider(tmp_path)
    assert await provider._resolve_model(_Client(), requested) == expected


def test_extra_env_overlay_does_not_hide_an_exported_override(tmp_path, monkeypatch):
    """`extra_env` is an overlay, not a replacement environment.

    `_ensure_server` passes `AgentRequest.extra_env`, which never carries
    `CIAO_OPENCODE_BIN`; reading only that overlay made the documented
    override dead on every chat turn while the error still named it.
    """
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))

    # An unrelated per-request overlay must not mask the exported override.
    assert resolve_opencode_binary({"OPENCODE_CONFIG": "/x"}) == str(binary.resolve())
    # No overlay at all still reads the process environment.
    assert resolve_opencode_binary(None) == str(binary.resolve())

    # A per-request override still wins over the exported one.
    other = tmp_path / "opencode-req"
    other.write_text("#!/bin/sh\n")
    assert resolve_opencode_binary(
        {"CIAO_OPENCODE_BIN": str(other)}
    ) == str(other.resolve())
