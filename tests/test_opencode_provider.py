"""opencode provider unit tests.

Event fixtures under ``tests/fixtures/opencode/`` were captured from a real
``opencode serve`` process (2.0.16) and sanitized: absolute paths replaced and
identifiers shortened. No credentials, prompts, or account identifiers are
recorded.
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
from ciao.execution_modes import opencode_credential_deny_rules
from ciao.providers.opencode import (
    OPENCODE_V2_REQUIRED,
    OpencodeProvider,
    OpencodeSettings,
    _catalog_from_api,
    _context_window_for,
    _form_answer_value,
    _form_field_active,
    _validate_form_field,
    _projected_message,
    _log_catalog_change,
    _read_v2_messages,
    _server_version_error,
    catalog_providers,
    compose_system,
    config_placeholder_problems,
    error_text,
    model_accepts_images,
    missing_required_paths,
    mode_settings,
    opencode_collab_tree_counts,
    opencode_default_model,
    readonly_agent_rules,
    resolve_opencode_binary,
    _session_handover_text,
    split_model,
    unresolved_placeholders,
    usage_payload,
    workspace_config_placeholder_problems,
)

FIXTURES = Path(__file__).parent / "fixtures" / "opencode"


def _provider(tmp_path: Path) -> OpencodeProvider:
    return OpencodeProvider(tmp_path)


def test_resolve_opencode_binary_honors_exported_path(tmp_path, monkeypatch):
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))

    assert resolve_opencode_binary({"OPENCODE_CONFIG": "/x"}) == str(binary.resolve())
    assert resolve_opencode_binary() == str(binary.resolve())

    request_binary = tmp_path / "opencode-request"
    request_binary.write_text("#!/bin/sh\n")
    assert resolve_opencode_binary(
        {"CIAO_OPENCODE_BIN": str(request_binary)}
    ) == str(request_binary.resolve())


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
    spec = {"paths": {"/api/info": {}, "/api/session": {}}}
    missing = missing_required_paths(spec)
    assert "/api/session/{sessionID}/prompt" in missing
    assert "/api/session/{sessionID}/form/{formID}/reply" in missing


def test_missing_required_paths_accepts_the_v2_document():
    """The sanitized OpenCode 2.0.16 path subset satisfies every requirement."""
    spec = json.loads((FIXTURES / "openapi_paths.json").read_text(encoding="utf-8"))
    assert missing_required_paths(spec) == ()


def test_v1_and_unknown_server_versions_fail_with_the_upgrade_message():
    assert _server_version_error({"version": "1.18.18"}) == (
        f"{OPENCODE_V2_REQUIRED} Installed server version: 1.18.18."
    )
    assert _server_version_error({"version": "2.0.0"}) is not None
    assert _server_version_error({"version": "3.0.0"}) is not None
    assert _server_version_error({"version": "unknown"}) is not None
    assert _server_version_error({"version": "2.0.16"}) is None


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
    """Flatten wildcard V2 rules to {action: effect}."""
    return {
        rule["action"]: rule["effect"]
        for rule in mode_settings(mode)[1]
        if rule["resource"] == "*"
    }


def _shell_patterns(mode: BridgeMode) -> dict[str, set[str]]:
    """Flatten shell rules to {effect: set of resources}."""
    out: dict[str, set[str]] = {"allow": set(), "ask": set()}
    for rule in mode_settings(mode)[1]:
        if rule.get("action") != "shell":
            continue
        out.setdefault(str(rule.get("effect") or ""), set()).add(
            str(rule.get("resource") or "")
        )
    return out


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
    instructions = provider._chat_system_instructions()
    assert "Ciaobot core instructions" in instructions
    assert "native workspace guide" in instructions
    assert "MEMORY (your personal notes)" not in instructions


def test_permission_rules_use_the_v2_api_shape():
    ruleset = mode_settings("normal")[1]
    assert ruleset[0] == {"action": "*", "resource": "*", "effect": "ask"}


def test_every_rule_uses_a_valid_v2_effect():
    for mode in ("plan", "normal", "auto", "bypass"):
        for rule in mode_settings(mode)[1]:
            assert rule["effect"] in {"allow", "deny", "ask"}
            assert set(rule) == {"action", "resource", "effect"}


def test_the_wildcard_rule_comes_first():
    for mode in ("plan", "auto"):
        assert mode_settings(mode)[1][0]["action"] == "*"


def test_bypass_keeps_wildcard_allow_and_normal_asks():
    assert _actions("bypass")["*"] == "allow"
    assert _actions("normal")["*"] == "ask"
    assert "edit" not in _actions("normal")
    # Search actions are the deliberate security exception to bypass.


def test_auto_allows_everything_but_keeps_shell_gated():
    actions = _actions("auto")
    assert actions["*"] == "allow"
    assert actions["shell"] == "ask"
    assert actions["glob"] == "ask"
    assert actions["grep"] == "ask"
    assert "edit" not in actions
    shell = _shell_patterns("auto")
    assert shell["ask"] == {"*"}


@pytest.mark.parametrize("mode", ["plan", "normal", "auto", "bypass"])
def test_v2_search_actions_require_explicit_approval(mode: BridgeMode):
    rules = mode_settings(mode)[1]
    for action in {"glob", "grep"}:
        matching = [
            rule for rule in rules
            if rule["action"] == action and rule["resource"] == "*"
        ]
        assert matching[-1]["effect"] == "ask"


def test_protected_glob_patterns_remain_hard_denied_after_search_approval():
    rules = mode_settings("bypass")[1]
    glob_rules = [rule for rule in rules if rule["action"] == "glob"]
    assert next(rule["effect"] for rule in reversed(glob_rules) if rule["resource"] == ".env") == "deny"
    assert next(rule["effect"] for rule in reversed(glob_rules) if rule["resource"] == ".runtime/**") == "deny"


def test_plan_mode_is_read_only():
    actions = _actions("plan")
    assert actions["read"] == "allow"
    assert actions["*"] == "ask"
    assert "edit" not in actions


def test_tools_can_be_disabled_for_one_shot_sessions():
    agent, rules = mode_settings("plan", tools_enabled=False)
    assert agent == "plan"
    assert rules == [{"action": "*", "resource": "*", "effect": "deny"}]


# ── read-only memory agent ruleset ─────────────────────────────────────
# The insights agent reads the vault and answers; it must not be able to
# write, shell out, or search outside the root it was given.


def test_readonly_agent_rules_scope(tmp_path: Path):
    root = tmp_path / "vault"
    rules = readonly_agent_rules([root])
    base = str(root.resolve())

    # Deny-all first: OpenCode resolves last-match-wins, so every carve-out
    # has to follow the wildcard.
    assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}
    allowed = [
        (r["action"], r["resource"])
        for r in rules
        if r["effect"] == "allow"
    ]
    assert allowed == [
        ("read", base),
        ("read", f"{base}/**"),
        ("external_directory", base),
        ("external_directory", f"{base}/**"),
    ]
    # V2 sends the search pattern, not the search root, so a search cannot be
    # scoped at all: glob and grep stay denied.
    for action in ("glob", "grep", "edit", "shell"):
        assert not any(
            r["action"] == action and r["effect"] == "allow" for r in rules
        ), action
    # The credential denies come last so nothing above can outrank them.
    assert rules[len(rules) - len(opencode_credential_deny_rules()):] == (
        opencode_credential_deny_rules()
    )


@pytest.mark.parametrize("mode", ["plan", "bypass"])
def test_session_settings_prefers_custom_rules(tmp_path: Path, mode: BridgeMode):
    request = AgentRequest(prompt="p", model="m", mode=mode, provider="opencode")

    plain = OpencodeProvider(tmp_path)
    assert plain._session_settings(request) == mode_settings(mode)

    custom = [{"action": "*", "resource": "*", "effect": "deny"}]
    scoped = OpencodeProvider(tmp_path, permission_rules=custom)
    assert scoped._session_settings(request) == ("build", custom)
    # A copy, not the caller's list: a session that mutates its ruleset
    # cannot reach back into the provider's.
    agent, rules = scoped._session_settings(request)
    assert agent == "build"
    assert rules == custom
    assert rules is not custom


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

    async def get(self, path: str, *, params=None):
        self.get_calls.append(path)
        if path.endswith("/message"):
            return _SessionResponse({
                "data": self.messages,
                "cursor": {"previous": None, "next": None},
            })
        if path == "/api/model/default":
            return _SessionResponse({
                "data": {"modelID": "default-model", "providerID": "opencode"}
            })
        if path == "/api/model":
            return _SessionResponse({"data": [
                {"providerID": "anthropic", "modelID": "sonnet", "enabled": True}
            ]})
        return _SessionResponse({"data": self.payload})

    async def post(self, path: str, json=None):
        self.post_calls.append((path, json))
        return _SessionResponse({"data": {"id": "session-new"}})


@pytest.mark.asyncio
async def test_resume_rotates_when_session_permission_is_stale(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient({
        "id": "session-old",
        "agent": "build",
        "permissions": mode_settings("bypass")[1],
    }, messages=[
        {"id": "msg_u", "type": "user", "text": "Earlier request"},
        {"id": "msg_a", "type": "assistant", "content": [
            {"type": "text", "text": "Earlier answer"},
        ]},
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
    assert client.get_calls == [
        "/api/model/default",
        "/api/session/session-old",
        "/api/session/session-old/message",
    ]
    assert "User: Earlier request" in provider._session_handover_context
    assert "Assistant: Earlier answer" in provider._session_handover_context
    assert request.prompt.startswith("[stable context]\n")
    assert client.post_calls == [(
        "/api/session",
        {
            "agent": "build",
            "permissions": expected,
            "model": {"id": "default-model", "providerID": "opencode"},
        },
    )]


@pytest.mark.asyncio
async def test_resume_keeps_session_when_permission_matches(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient({
        "id": "session-old",
        "agent": "build",
        "permissions": mode_settings("normal")[1],
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
    assert client.get_calls == ["/api/model/default", "/api/session/session-old"]
    assert client.post_calls == [(
        "/api/session/session-old/model",
        {"model": {"id": "default-model", "providerID": "opencode"}},
    )]


@pytest.mark.asyncio
async def test_session_passes_through_unqualified_model(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient(None)
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="hello",
        model="sonnet",
        mode="normal",
        provider="opencode",
    )

    assert await provider._ensure_session(request) == "session-new"
    assert client.get_calls == ["/api/model"]
    assert client.post_calls == [(
        "/api/session",
        {
            "agent": "build",
            "permissions": mode_settings("normal")[1],
            "model": {"id": "sonnet", "providerID": "anthropic"},
        },
    )]


@pytest.mark.asyncio
async def test_new_default_session_applies_the_requested_thinking_variant(tmp_path):
    provider = _provider(tmp_path)
    client = _SessionClient(None)
    provider._client = client  # type: ignore[assignment]
    request = AgentRequest(
        prompt="hello",
        model="",
        thinking_level="high",
        mode="normal",
        provider="opencode",
    )

    assert await provider._ensure_session(request) == "session-new"
    assert client.post_calls == [(
        "/api/session",
        {
            "agent": "build",
            "permissions": mode_settings("normal")[1],
            "model": {
                "id": "default-model",
                "providerID": "opencode",
                "variant": "high",
            },
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


def test_session_handover_strips_v2_prompt_context():
    history = [{
        "info": {"role": "user"},
        "parts": [{
            "type": "text",
            "text": (
                "[CIAO_CONTEXT_BEGIN]\nprivate runtime context\n"
                "[CIAO_CONTEXT_END]\n\nVisible request"
            ),
        }],
    }]
    rendered = _session_handover_text(history)
    assert "Visible request" in rendered
    assert "private runtime context" not in rendered


def test_prompt_body_uses_v2_text_files_and_queue_delivery(tmp_path):
    provider = _provider(tmp_path)
    body = provider._prompt_body(
        AgentRequest(prompt="Hello", model="", mode="bypass", provider="opencode"),
        system="Core instructions",
    )
    assert body == {
        "text": "[CIAO_CONTEXT_BEGIN]\nCore instructions\n[CIAO_CONTEXT_END]\n\nHello",
        "delivery": "queue",
        "resume": True,
    }


@pytest.mark.asyncio
async def test_v2_message_pagination_omits_order_after_the_cursor():
    class _Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class _Client:
        def __init__(self):
            self.params = []

        async def get(self, _path, *, params=None):
            self.params.append(params)
            if len(self.params) == 1:
                return _Response({
                    "data": [{"id": "msg_1", "type": "user", "text": "one"}],
                    "cursor": {"next": "cursor-2"},
                })
            return _Response({
                "data": [{"id": "msg_2", "type": "user", "text": "two"}],
                "cursor": {"next": None},
            })

    client = _Client()
    messages = await _read_v2_messages(client, "ses_1")  # type: ignore[arg-type]
    assert [message["info"]["id"] for message in messages] == ["msg_1", "msg_2"]
    assert client.params[0]["order"] == "asc"
    assert "order" not in client.params[1]
    assert client.params[1]["cursor"] == "cursor-2"


def test_v2_flat_messages_normalize_at_the_provider_boundary():
    normalized = _projected_message({
        "id": "msg_1",
        "type": "assistant",
        "agent": "build",
        "model": {"id": "model", "providerID": "opencode"},
        "time": {"created": 1, "completed": 2},
        "content": [
            {"type": "reasoning", "text": "think"},
            {"type": "text", "text": "answer"},
            {
                "type": "tool", "id": "call_1", "name": "shell",
                "state": {"status": "completed", "input": {"command": "pwd"}},
            },
        ],
    })
    assert normalized is not None
    assert normalized["info"]["role"] == "assistant"
    assert normalized["info"]["modelID"] == "model"
    assert [part["type"] for part in normalized["parts"]] == [
        "reasoning", "text", "tool",
    ]
    assert normalized["parts"][2]["tool"] == "shell"


def test_v2_idle_messages_keep_the_recovery_discriminator():
    normalized = _projected_message({"id": "idle_1", "type": "idle", "outcome": "failed"})
    assert normalized is not None
    assert normalized["info"]["type"] == "idle"
    assert normalized["info"]["outcome"] == "failed"


# ── error sanitization ──────────────────────────────────────────────────


def test_error_text_drops_the_stack_trace():
    error = {
        "type": "provider.no-route",
        "message": "Model not found: x\n    at <anonymous> (/$bunfs/root/a.js:1:2)",
    }
    assert error_text(error) == "Model not found: x"


def test_error_text_falls_back_to_the_v2_error_type():
    assert error_text({"type": "provider.auth", "message": ""}) == "provider.auth"


def test_error_text_handles_a_missing_payload():
    assert error_text(None) == "OpenCode reported an error"


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
    assert usage_payload({"input": 0, "output": 5, "cache": {}}) == {
        "outputTokens": "5", "totalTokens": "5",
    }


def test_usage_payload_tolerates_junk():
    assert usage_payload(None) == {}
    assert usage_payload({"input": "not-a-number"}) == {}


def test_context_window_for_reads_the_v2_model_limit():
    payload = {"data": [{
        "providerID": "anthropic",
        "modelID": "claude-sonnet-4-6",
        "limit": {"context": 200000},
    }]}
    assert _context_window_for(payload, "anthropic", "claude-sonnet-4-6") == 200000


def test_context_window_for_returns_none_when_unstated_or_unknown():
    payload = {"data": [
        {"providerID": "anthropic", "modelID": "m"},
        {"providerID": "anthropic", "modelID": "no-limit", "limit": {"context": 0}},
    ]}
    assert _context_window_for(payload, "anthropic", "m") is None
    assert _context_window_for(payload, "anthropic", "no-limit") is None
    assert _context_window_for(payload, "unknown", "m") is None
    assert _context_window_for(None, "anthropic", "m") is None


# ── model catalog ───────────────────────────────────────────────────────


def test_catalog_lists_only_active_providers():
    providers = {"data": [
        {"id": "anthropic", "activation": "enabled"},
        {"id": "openai", "activation": "disabled"},
    ]}
    models = {"data": [
        {
            "providerID": "anthropic", "modelID": "claude-sonnet-4-6",
            "name": "Sonnet", "enabled": True, "variants": [],
        },
        {
            "providerID": "openai", "modelID": "gpt-5.6-terra",
            "name": "Terra", "enabled": True, "variants": [],
        },
    ]}
    assert _catalog_from_api(providers, models) == [
        {
            "model": "anthropic/claude-sonnet-4-6",
            "label": "Sonnet (anthropic)",
            "variants": [],
        }
    ]


def test_catalog_reports_per_model_reasoning_variants():
    providers = {"data": [{"id": "opencode", "activation": "auto"}]}
    models = {"data": [
        {
            "providerID": "opencode", "modelID": "deepseek-v4-flash-free",
            "enabled": True,
            "variants": [{"id": "low"}, {"id": "high"}, {"id": "max"}],
        },
        {
            "providerID": "opencode", "modelID": "big-pickle",
            "enabled": True, "variants": [],
        },
    ]}
    by_model = {
        row["model"]: row["variants"] for row in _catalog_from_api(providers, models)
    }
    assert by_model["opencode/deepseek-v4-flash-free"] == ["high", "low", "max"]
    assert by_model["opencode/big-pickle"] == []


# ── image capability ────────────────────────────────────────────────────


def test_model_accepts_images_reads_the_v2_input_modalities():
    assert model_accepts_images({
        "capabilities": {"input": ["text", "image"]},
    }) is True
    assert model_accepts_images({
        "capabilities": {"input": ["text"]},
    }) is False


def test_model_accepts_images_is_unknown_when_unstated():
    assert model_accepts_images({"id": "m"}) is None
    assert model_accepts_images({"id": "m", "capabilities": "junk"}) is None
    assert model_accepts_images({"capabilities": {"input": {}}}) is None


def test_catalog_states_image_support_only_when_opencode_does():
    providers = {"data": [{"id": "opencode", "activation": "auto"}]}
    models = {"data": [
        {
            "providerID": "opencode", "modelID": "seer", "enabled": True,
            "capabilities": {"input": ["text", "image"]}, "variants": [],
        },
        {
            "providerID": "opencode", "modelID": "reader", "enabled": True,
            "capabilities": {"input": ["text"]}, "variants": [],
        },
        {
            "providerID": "opencode", "modelID": "quiet", "enabled": True,
            "variants": [],
        },
    ]}
    by_model = {row["model"]: row for row in _catalog_from_api(providers, models)}
    assert by_model["opencode/seer"]["images"] is True
    assert by_model["opencode/reader"]["images"] is False
    assert "images" not in by_model["opencode/quiet"]


def test_catalog_keeps_free_models_before_provider_discovery_settles():
    models = {"data": [{
        "providerID": "opencode", "modelID": "space-bunny-free",
        "enabled": True, "variants": [],
    }]}
    assert _catalog_from_api({"data": []}, models) == [
        {"model": "opencode/space-bunny-free", "label": "space-bunny-free (opencode)", "variants": []}
    ]


def test_catalog_tolerates_junk():
    assert _catalog_from_api(None, None) == []
    assert _catalog_from_api({"data": "nope"}, {"data": "nope"}) == []


# ── default model ───────────────────────────────────────────────────────


def test_default_model_override():
    class Config:
        opencode = OpencodeSettings(default_model="anthropic/claude-sonnet-4-6")

    assert opencode_default_model(Config()) == "anthropic/claude-sonnet-4-6"


def test_default_model_on_a_config_without_opencode():
    assert opencode_default_model(object()) == ""


# ── collaboration tree counts ────────────────────────────────────────────


def test_collab_tree_counts_use_v2_active_session_metadata():
    tree = [
        {"info": {"id": "a", "outcome": "failed"}, "active": True, "messages": []},
        {"info": {"id": "b", "outcome": "succeeded"}, "active": False, "messages": []},
        {"info": {"id": "c", "outcome": "failed"}, "active": False, "messages": []},
        {"info": {"id": "d"}, "active": False, "messages": []},
    ]
    running, had_subagents = opencode_collab_tree_counts(tree)
    assert running == 1
    assert had_subagents is True


def test_collab_tree_counts_are_empty_for_an_empty_tree():
    assert opencode_collab_tree_counts([]) == (0, False)


def test_collab_tree_counts_tolerate_junk():
    assert opencode_collab_tree_counts([None, {"info": {}}, {"messages": "nope"}]) == (0, True)


@pytest.mark.asyncio
async def test_live_collab_tree_preserves_children_when_activity_lookup_fails(
    tmp_path, monkeypatch,
):
    provider = _provider(tmp_path)
    provider._client = object()  # type: ignore[assignment]
    provider._session_id = "ses_parent"

    async def children(_client, _parent_id):
        return [{"id": "ses_child", "parentID": "ses_parent"}]

    async def active(_client):
        raise RuntimeError("activity endpoint unavailable")

    async def messages(_client, _child_id):
        return []

    monkeypatch.setattr("ciao.providers.opencode._read_v2_children", children)
    monkeypatch.setattr("ciao.providers.opencode._read_active_sessions", active)
    monkeypatch.setattr("ciao.providers.opencode._read_v2_messages", messages)

    tree = await provider.read_live_collab_tree()

    assert tree[0]["info"]["id"] == "ses_child"
    assert tree[0]["active"] is None
    # Unknown activity remains conservative instead of looking settled.
    assert opencode_collab_tree_counts(tree) == (1, True)


# ── event normalization ─────────────────────────────────────────────────


def _convert(provider: OpencodeProvider, kind: str, data: dict):
    return provider._event_to_stream({"type": kind, "data": data})


def test_text_delta_becomes_assistant_text(tmp_path):
    events = _convert(
        _provider(tmp_path), "session.text.delta",
        {"assistantMessageID": "msg_1", "ordinal": 0, "delta": "hello"},
    )
    assert len(events) == 1
    assert isinstance(events[0], AssistantTextDelta)
    assert events[0].text == "hello"


def test_empty_text_delta_is_dropped(tmp_path):
    assert _convert(
        _provider(tmp_path), "session.text.delta",
        {"assistantMessageID": "msg_1", "ordinal": 0, "delta": ""},
    ) == []


def test_reasoning_delta_becomes_thinking(tmp_path):
    events = _convert(
        _provider(tmp_path), "session.reasoning.delta",
        {"assistantMessageID": "msg_1", "ordinal": 0, "delta": "hmm"},
    )
    assert isinstance(events[0], ThinkingEvent)
    assert events[0].text == "hmm"


def test_tool_called_becomes_tool_use_with_a_stable_id(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "session.tool.input.started", {"id": "call_1", "name": "read"})
    events = _convert(
        provider,
        "session.tool.called",
        {"id": "call_1", "input": {"filePath": "/workspace/a.py"}},
    )
    assert isinstance(events[0], ToolUseEvent)
    assert events[0].tool_name == "read"
    assert events[0].tool_use_id == "call_1"
    assert events[0].tool_input == "/workspace/a.py"


def test_write_tool_reports_a_file_touch(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.tool.called",
        {"id": "c", "name": "write", "input": {"filePath": "/workspace/new.py"}},
    )
    assert events[0].file_touches == [{"file_path": "/workspace/new.py", "action": "write"}]


def test_read_tool_reports_no_file_touch(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.tool.called",
        {"id": "c", "name": "read", "input": {"filePath": "/workspace/a.py"}},
    )
    assert events[0].file_touches is None


def test_tool_result_recovers_the_tool_name_from_the_call(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "session.tool.input.started", {"id": "c1", "name": "shell"})
    _convert(provider, "session.tool.called", {"id": "c1", "input": {}})
    events = _convert(provider, "session.tool.success", {"id": "c1"})
    assert events[0].type == "tool_result"
    assert events[0].tool_name == "shell"
    assert provider._tool_calls == {}


def test_failed_tool_carries_a_sanitized_reason(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "session.tool.called", {"id": "c1", "name": "shell", "input": {}})
    events = _convert(
        provider,
        "session.tool.failed",
        {"id": "c1", "error": {"type": "ToolError", "message": "boom\n  at x"}},
    )
    assert events[0].tool_input == "boom"


def test_step_ended_reports_token_usage(tmp_path):
    events = _convert(
        _provider(tmp_path),
        "session.step.ended",
        {"tokens": {"input": 10, "output": 3, "reasoning": 0, "cache": {"read": 0, "write": 0}}},
    )
    assert isinstance(events[0], TokenUsageEvent)
    assert (events[0].input_tokens, events[0].output_tokens) == (10, 3)


# Captured from a live OpenCode 2.0.16 `permission.asked` event.
LIVE_PERMISSION = {
    "id": "per_live1",
    "sessionID": "ses_1",
    "action": "shell",
    "resources": ["echo approved-ok"],
    "metadata": {"command": "echo approved-ok"},
    "source": {"type": "tool", "messageID": "msg_1", "id": "call_abc"},
}


def test_permission_card_names_the_tool_and_the_command(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_name == "shell"
    assert events[0].tool_input == "echo approved-ok"
    assert "shell" in events[0].message


def test_permission_card_links_back_to_the_tool_call(tmp_path):
    provider = _provider(tmp_path)
    _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert provider.tool_use_id_for_request("per_live1") == "call_abc"


def test_permission_card_falls_back_to_resources_without_metadata(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider, "permission.asked", {**LIVE_PERMISSION, "metadata": {}}
    )
    assert events[0].tool_input == "echo approved-ok"


def test_permission_without_session_is_ignored(tmp_path):
    provider = _provider(tmp_path)
    assert _convert(provider, "permission.asked", {"id": "perm_1", "action": "edit"}) == []
    assert provider._permission_requests == {}


def test_reused_permission_id_in_a_new_session_replaces_the_old_request(tmp_path):
    provider = _provider(tmp_path)
    first = {**LIVE_PERMISSION, "sessionID": "ses_old", "source": {"id": "call_old"}}
    second = {**LIVE_PERMISSION, "sessionID": "ses_new", "source": {"id": "call_new"}}

    _convert(provider, "permission.asked", first)
    events = _convert(provider, "permission.asked", second)

    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].session_id == "ses_new"
    assert provider._permission_requests["per_live1"].session_id == "ses_new"
    assert provider.tool_use_id_for_request("per_live1", "ses_old") == ""
    assert provider.tool_use_id_for_request("per_live1", "ses_new") == "call_new"


@pytest.mark.asyncio
async def test_permission_reply_rejects_a_different_session(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(provider, "permission.asked", LIVE_PERMISSION)

    result = await provider.send_permission_response(
        "per_live1", True, session_id="ses_other"
    )

    assert result.ok is False
    assert client.calls == []
    assert "per_live1" in provider._permission_requests


def test_form_becomes_an_ask_user_question_card(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_1",
            "sessionID": "ses_1",
            "title": "Database",
            "fields": [{
                "key": "database",
                "title": "Database",
                "description": "Which database should I use?",
                "type": "string",
                "custom": True,
                "options": [{
                    "value": "postgres", "label": "Postgres",
                    "description": "Relational",
                }],
            }],
        }},
    )
    assert events[0].tool_name == "AskUserQuestion"
    payload = json.loads(events[0].tool_input)
    question = payload["questions"][0]
    assert question["id"] == "database"
    assert question["question"] == "Which database should I use?"
    assert question["header"] == "Database"
    assert question["options"] == [
        {"label": "Postgres", "value": "postgres", "description": "Relational"}
    ]
    assert question["isOther"] is True
    assert "frm_1" in provider._question_requests


def test_form_without_fields_is_ignored(tmp_path):
    provider = _provider(tmp_path)
    assert _convert(
        provider, "form.created", {"form": {"id": "frm", "sessionID": "s", "fields": []}}
    ) == []
    assert provider._question_requests == {}


def test_form_custom_false_is_preserved_for_the_pwa(tmp_path):
    provider = _provider(tmp_path)
    events = _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_2", "sessionID": "ses_1", "title": "Default",
            "fields": [{
                "key": "use_default", "title": "Use the default?", "type": "string",
                "custom": False,
                "options": [{"value": "yes", "label": "Yes"}],
            }],
        }},
    )
    assert json.loads(events[0].tool_input)["questions"][0]["isOther"] is False


def test_reused_form_id_in_a_new_session_replaces_the_old_request(tmp_path):
    provider = _provider(tmp_path)
    form = {
        "id": "frm_reused",
        "title": "Choice",
        "fields": [{"key": "value", "title": "Value", "type": "string"}],
    }
    _convert(
        provider,
        "form.created",
        {"form": {**form, "sessionID": "ses_old"}},
    )
    events = _convert(
        provider,
        "form.created",
        {"form": {**form, "sessionID": "ses_new"}},
    )

    assert events[0].session_id == "ses_new"
    assert provider._question_requests["frm_reused"].session_id == "ses_new"


@pytest.mark.asyncio
async def test_form_reply_rejects_a_different_session(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_session",
            "sessionID": "ses_1",
            "fields": [{"key": "value", "title": "Value", "type": "string"}],
        }},
    )

    result = await provider.send_question_response(
        "frm_session", {"value": ["ok"]}, session_id="ses_other"
    )

    assert result.ok is False
    assert client.calls == []
    assert "frm_session" in provider._question_requests


def test_form_values_are_coerced_to_v2_field_types():
    assert _form_answer_value("string", ["Postgres"]) == "Postgres"
    assert _form_answer_value("integer", ["3"]) == 3
    assert _form_answer_value("number", ["2.5"]) == 2.5
    assert _form_answer_value("boolean", ["true"]) is True
    assert _form_answer_value("external", ["true"]) is True
    assert _form_answer_value("multiselect", ["a", "b"]) == ["a", "b"]


@pytest.mark.asyncio
async def test_form_reply_honors_conditions_and_external_fields(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_typed", "sessionID": "ses_1", "title": "Setup",
            "fields": [
                {
                    "key": "enabled", "title": "Enable?", "type": "boolean",
                    "required": True,
                },
                {
                    "key": "details", "title": "Details", "type": "string",
                    "when": [{"key": "enabled", "op": "eq", "value": True}],
                },
                {
                    "key": "external", "title": "Open portal", "type": "external",
                    "url": "https://example.com",
                },
            ],
        }},
    )
    result = await provider.send_question_response(
        "frm_typed",
        {"enabled": ["Yes"], "details": ["only when enabled"], "external": ["Done"]},
    )
    assert result.ok is True
    assert client.calls == [(
        "/api/session/ses_1/form/frm_typed/reply",
        {"answer": {"enabled": True, "details": "only when enabled", "external": True}},
    )]


@pytest.mark.asyncio
async def test_form_reply_prefers_an_exact_wire_value_over_a_label_collision(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_collision", "sessionID": "ses_1", "title": "Choice",
            "fields": [{
                "key": "choice", "title": "Choice", "type": "string", "custom": True,
                "options": [
                    {"value": "1", "label": "One"},
                    {"value": "2", "label": "1"},
                ],
            }],
        }},
    )

    result = await provider.send_question_response(
        "frm_collision", {"choice": ["1"]}
    )

    assert result.ok is True
    assert client.calls == [(
        "/api/session/ses_1/form/frm_collision/reply",
        {"answer": {"choice": "1"}},
    )]


@pytest.mark.asyncio
async def test_required_invalid_form_input_stays_pending(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_invalid", "sessionID": "ses_1", "title": "Count",
            "fields": [{
                "key": "count", "title": "How many?", "type": "integer",
                "required": True,
            }],
        }},
    )
    result = await provider.send_question_response("frm_invalid", {"count": ["many"]})
    assert result.ok is False
    assert client.calls == []
    assert "frm_invalid" in provider._question_requests


def test_execution_terminal_events_are_not_user_visible(tmp_path):
    assert _convert(
        _provider(tmp_path), "session.execution.succeeded", {"sessionID": "s"}
    ) == []


def test_unknown_events_are_ignored(tmp_path):
    assert _convert(_provider(tmp_path), "pty.created", {"ptyID": "x"}) == []


# ── replies ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_reply_for_an_unknown_request_is_refused(tmp_path):
    result = await _provider(tmp_path).send_permission_response("nope", True)
    assert result.ok is False


@pytest.mark.asyncio
async def test_question_reply_for_an_already_settled_request_is_idempotent(tmp_path):
    result = await _provider(tmp_path).send_question_response("nope", {})
    assert result.ok is True


def test_tool_use_id_for_unknown_request_is_empty(tmp_path):
    assert _provider(tmp_path).tool_use_id_for_request("nope") == ""


def test_v2_form_conditions_use_strict_unanswered_and_multiselect_semantics():
    eq = {"key": "mode", "op": "eq", "value": "advanced"}
    neq = {"key": "mode", "op": "neq", "value": "advanced"}
    assert not _form_field_active({"when": [eq]}, {})
    assert not _form_field_active({"when": [neq]}, {})
    assert _form_field_active({"when": [eq]}, {"mode": "advanced"})
    assert _form_field_active({"when": [neq]}, {"mode": "basic"})
    assert _form_field_active(
        {"when": [{"key": "tags", "op": "eq", "value": "safe"}]},
        {"tags": ["safe", "other"]},
    )
    assert not _form_field_active(
        {"when": [{"key": "tags", "op": "eq", "value": "safe"}]},
        {"tags": ["other"]},
    )


def test_v2_form_validation_covers_typed_constraints():
    assert _validate_form_field(
        {"type": "string", "format": "email", "required": True},
        ["person@example.com"],
    ) == "person@example.com"
    with pytest.raises(ValueError):
        _validate_form_field({"type": "string", "format": "email"}, ["not an email"])
    with pytest.raises(ValueError):
        _validate_form_field(
            {"type": "number", "minimum": 1, "maximum": 3}, ["4"]
        )
    with pytest.raises(ValueError):
        _validate_form_field(
            {"type": "multiselect", "minItems": 1, "custom": True}, []
        )
    with pytest.raises(ValueError, match="Choose one answer"):
        _validate_form_field({"type": "string", "custom": True}, ["option", "other"])



# ── permission.asked surfaces a card ────────────────────────────────────
# The auto ruleset keeps shell and destructive control-plane tools behind an
# operator approval card.


class _RecordingPermissionClient:
    def __init__(self, *, status_code=200):
        self.calls: list[tuple[str, dict]] = []
        self.status_code = status_code

    async def post(self, path, json=None):
        self.calls.append((path, json))

        class _Response:
            status_code = self.status_code

        return _Response()

    async def delete(self, path):
        self.calls.append((path, {}))

        class _Response:
            status_code = self.status_code

        return _Response()


async def _drain_tasks():
    """Let the fire-and-forget reply task run to completion."""
    for _ in range(3):
        await asyncio.sleep(0)


def _armed_provider(tmp_path) -> tuple[OpencodeProvider, _RecordingPermissionClient]:
    provider = _provider(tmp_path)
    client = _RecordingPermissionClient()
    provider._client = client  # type: ignore[assignment]
    return provider, client


def test_any_permission_ask_surfaces_a_card(tmp_path):
    """Every permission.asked becomes an approval card, naming what is asked."""
    provider, client = _armed_provider(tmp_path)
    payload = {**LIVE_PERMISSION, "metadata": {"command": "git status && git push"}}
    events = _convert(provider, "permission.asked", payload)
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_input == "git status && git push"
    assert "per_live1" in provider._permission_requests
    assert client.calls == []


def test_permission_ask_names_the_command_detail(tmp_path):
    provider, client = _armed_provider(tmp_path)
    payload = {**LIVE_PERMISSION, "metadata": {"command": "rm -rf /tmp/cache"}}
    events = _convert(provider, "permission.asked", payload)
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_input == "rm -rf /tmp/cache"
    assert client.calls == []


def test_permission_ask_without_metadata_uses_patterns(tmp_path):
    provider, client = _armed_provider(tmp_path)
    payload = {**LIVE_PERMISSION, "metadata": {}}
    events = _convert(provider, "permission.asked", payload)
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


def test_permission_ask_surfaces_for_any_tool_action(tmp_path):
    provider, client = _armed_provider(tmp_path)
    events = _convert(
        provider,
        "permission.asked",
        {"id": "perm_ed", "sessionID": "ses_1", "action": "edit", "resources": ["/workspace/a.py"]},
    )
    assert isinstance(events[0], PermissionRequestEvent)
    assert client.calls == []


def test_search_permission_card_names_the_search_action(tmp_path):
    provider, _client = _armed_provider(tmp_path)
    events = _convert(
        provider,
        "permission.asked",
        {
            "id": "perm_search",
            "sessionID": "ses_1",
            "action": "grep",
            "resources": ["PWA_AUTH_TOKEN"],
            "metadata": {"path": "."},
        },
    )
    assert isinstance(events[0], PermissionRequestEvent)
    assert events[0].tool_name == "content search"
    assert events[0].tool_input == "PWA_AUTH_TOKEN"


def test_permission_ask_works_without_a_client(tmp_path):
    """No client just means the card stands; there is nothing to post."""
    provider = _provider(tmp_path)
    events = _convert(provider, "permission.asked", LIVE_PERMISSION)
    assert isinstance(events[0], PermissionRequestEvent)


@pytest.mark.asyncio
async def test_permission_reply_sends_v2_feedback_message(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(provider, "permission.asked", LIVE_PERMISSION)

    result = await provider.send_permission_response(
        "per_live1", False, "Do not push yet"
    )

    assert result.ok is True
    assert client.calls == [(
        "/api/session/ses_1/permission/per_live1/reply",
        {"decision": "reject", "message": "Do not push yet"},
    )]


@pytest.mark.asyncio
async def test_failed_permission_reply_keeps_request_for_retry(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(provider, "permission.asked", LIVE_PERMISSION)
    client.status_code = 500

    result = await provider.send_permission_response("per_live1", True)
    assert result.ok is False
    assert result.retryable is True

    assert "per_live1" in provider._permission_requests


@pytest.mark.asyncio
async def test_failed_form_reply_keeps_request_for_retry(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_retry", "sessionID": "ses_1", "title": "Pick",
            "fields": [{"key": "q", "title": "Which?", "type": "string"}],
        }},
    )
    client.status_code = 500

    result = await provider.send_question_response("frm_retry", {"q": ["answer"]})
    assert result.ok is False
    assert result.retryable is True

    assert "frm_retry" in provider._question_requests


@pytest.mark.asyncio
async def test_form_reply_uses_field_keys_and_option_values(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_order", "sessionID": "ses_1", "title": "Order",
            "fields": [
                {
                    "key": "first", "title": "First?", "type": "string",
                    "options": [{"value": "a", "label": "A"}],
                },
                {
                    "key": "second", "title": "Second?", "type": "string",
                    "options": [{"value": "b", "label": "B"}],
                },
            ],
        }},
    )

    result = await provider.send_question_response(
        "frm_order", {"second": ["B"], "first": ["A"]}
    )
    assert result.ok is True

    assert client.calls == [
        (
            "/api/session/ses_1/form/frm_order/reply",
            {"answer": {"first": "a", "second": "b"}},
        )
    ]
    assert provider._question_requests == {}


@pytest.mark.asyncio
async def test_empty_form_reply_is_not_cancellation(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_empty", "sessionID": "ses_1", "title": "Optional",
            "fields": [{"key": "q", "title": "Which?", "type": "string"}],
        }},
    )

    result = await provider.send_question_response("frm_empty", {})
    assert result.ok is True
    assert client.calls == [(
        "/api/session/ses_1/form/frm_empty/reply", {"answer": {}}
    )]


@pytest.mark.asyncio
async def test_explicit_form_cancel_uses_delete(tmp_path):
    provider, client = _armed_provider(tmp_path)
    _convert(
        provider,
        "form.created",
        {"form": {
            "id": "frm_cancel", "sessionID": "ses_1", "title": "Cancel",
            "fields": [{"key": "q", "title": "Which?", "type": "string"}],
        }},
    )

    result = await provider.send_question_response("frm_cancel", {}, cancel=True)
    assert result.ok is True
    assert client.calls == [("/api/session/ses_1/form/frm_cancel", {})]


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


def test_v2_failure_is_a_terminal_execution_event():
    events = _live_events()
    assert events[0]["type"] == "server.connected"
    failures = [
        event["data"]["error"]
        for event in events
        if event["type"] == "session.execution.failed"
    ]
    assert failures
    assert failures[0]["message"].startswith("Model unavailable:")


def test_live_fixture_error_is_reported_without_a_stack(tmp_path):
    for event in _live_events():
        if event["type"] != "session.execution.failed":
            continue
        text = error_text(event["data"]["error"])
        assert "\n" not in text
        assert "$bunfs" not in text


# ── the real OpenCode 2 streaming path ───────────────────────────────────
#
# `turn_with_tool.jsonl` is a full V2 turn captured from a live server against
# a free model: reasoning, a shell call, assistant text, and usage totals.


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
    assert ("tool_use", "shell") in tools
    assert ("tool_result", "shell") in tools


def test_a_tool_call_is_announced_exactly_once(tmp_path):
    """Running updates repeat; only the first should surface as a new call."""
    events = _replay(_provider(tmp_path))
    starts = [e for e in events if e.type == "tool_use" and e.tool_name == "shell"]
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
            return {"data": [{
                "providerID": "opencode", "modelID": "big-pickle",
                "limit": {"context": 1000},
            }]}

    class _FakeClient:
        async def get(self, _path: str):
            return _FakeResponse()

    async def _run():
        await provider._augment_context_pct(_FakeClient(), ("opencode", "big-pickle"))

    asyncio.run(_run())
    assert provider._usage["context_window"] == "1000"
    assert provider._usage["context_pct"] == "15.0%"


def test_v2_context_uses_the_last_step_not_the_cumulative_session_snapshot(
    tmp_path,
):
    """V2 reports both per-step and cumulative session token snapshots.

    The result usage keeps OpenCode's cumulative session total, while the
    context percentage uses the latest model-call size rather than adding the
    cached prompt from every tool-loop step.
    """
    provider = _provider(tmp_path)
    _replay(provider)
    assert provider._usage["totalTokens"] == "240"
    assert provider._usage["cacheReadTokens"] == "100"
    assert provider._context_usage["totalTokens"] == "102"
    assert provider._context_usage["cacheReadTokens"] == "80"


def test_recovered_turn_context_uses_the_final_assistant_call(tmp_path):
    provider = _provider(tmp_path)
    provider._restore_turn_metadata([
        {"info": {"id": "user-1", "role": "user"}},
        {
            "info": {
                "id": "assistant-1",
                "role": "assistant",
                "modelID": "model",
                "providerID": "opencode",
                "tokens": {"total": 100, "input": 10, "output": 5},
            },
            "parts": [],
        },
        {
            "info": {
                "id": "assistant-2",
                "role": "assistant",
                "modelID": "model",
                "providerID": "opencode",
                "tokens": {"total": 200, "input": 20, "output": 7},
            },
            "parts": [],
        },
    ])

    assert provider._usage["totalTokens"] == "300"
    assert provider._context_usage["totalTokens"] == "200"


def test_augment_context_pct_is_silent_when_limit_is_missing(tmp_path):
    provider = _provider(tmp_path)
    provider._usage = {"inputTokens": "100", "outputTokens": "50", "totalTokens": "150"}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{
                "providerID": "opencode", "modelID": "big-pickle",
            }]}

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

    async def get(self, _path: str, *, params=None):
        class _Accepted:
            status_code = 404
            text = ""

            def json(self):
                return {}

        return _Accepted()

    async def post(self, _path: str, json=None):
        message_id = str((json or {}).get("id") or "")

        class _Accepted:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"data": {"id": message_id}}

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
            raise TimeoutError("opencode serve did not become healthy: server stayed alive but never answered /api/info")

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

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert attempts == [1, 2]
    assert delays == [0.25]
    await provider.disconnect()


@pytest.mark.asyncio
async def test_contract_validation_retries_a_temporarily_empty_openapi_document(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path)
    attempts = 0

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    class Client:
        async def get(self, _path, *, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return Response(ValueError("empty response"))
            return Response({"paths": {path: {} for path in REQUIRED_PATHS}})

    async def no_wait(_delay):
        return None

    from ciao.providers.opencode import REQUIRED_PATHS

    provider._client = Client()
    monkeypatch.setattr("ciao.providers.opencode.asyncio.sleep", no_wait)
    await provider._verify_contract()
    assert attempts == 2


@pytest.mark.asyncio
async def test_contract_validation_still_fails_after_repeated_invalid_documents(
    tmp_path, monkeypatch
):
    provider = _provider(tmp_path)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("empty response")

    class Client:
        async def get(self, _path, *, timeout):
            return Response()

    async def no_wait(_delay):
        return None

    provider._client = Client()
    monkeypatch.setattr("ciao.providers.opencode.asyncio.sleep", no_wait)
    with pytest.raises(RuntimeError, match="could not read the OpenCode API document"):
        await provider._verify_contract()


def test_health_failure_reason_says_what_the_poll_saw():
    """A wedged server must not trail a bare empty ``: `` in the error."""
    from ciao.providers.opencode import _health_failure_reason

    assert _health_failure_reason(503, None) == "health returned HTTP 503"
    assert _health_failure_reason(None, ConnectionRefusedError("refused")) == "refused"
    assert (
        _health_failure_reason(None, None)
        == "server stayed alive but never answered /api/info"
    )


@pytest.mark.asyncio
async def test_missing_binary_says_how_to_fix_it(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: None
    )

    class Request:
        extra_env: dict = {}
        mcp_token = ""

    with pytest.raises(FileNotFoundError, match="login shell PATH"):
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
async def test_opencode_process_does_not_inherit_the_agent_token(tmp_path, monkeypatch):
    """The Ciaobot agent token rides in the request's extra_env, never in a
    child-server environment where model-launched commands could see it."""
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

    class Request:
        extra_env: dict = {"CIAO_AGENT_TOKEN": "stale-from-request-env"}
        mcp_token = ""

    await provider._ensure_server(Request())  # type: ignore[arg-type]

    assert "CIAO_AGENT_TOKEN" in spawn_kwargs["env"]
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


def test_credential_count_reads_the_v2_json_listing(monkeypatch):
    """2.0.16 dropped the `N credentials` footer for a plain table; the JSON
    listing is what gets counted. Shape captured from `auth list --format json`
    with identifiers removed."""
    import json as _json
    from types import SimpleNamespace

    import ciao.providers.opencode as mod

    listing = [
        {"id": "openai", "name": "OpenAI", "connections": [
            {"type": "credential", "label": "OAuth", "method": "oauth"},
            {"type": "env", "name": "OPENAI_API_KEY"},
        ]},
        {"id": "openrouter", "name": "OpenRouter", "connections": [
            {"type": "credential", "label": "API key", "method": "key"},
        ]},
    ]
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return SimpleNamespace(stdout=_json.dumps(listing), returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 2
    assert calls == [["/bin/opencode", "auth", "list", "--format", "json"]]

    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout="[]", returncode=0)
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 0


def test_status_reports_skills_plugins_and_mcps_from_the_server(monkeypatch):
    """Settings lists what the opencode CLI brings; the V2 server's list routes
    are the source, minus built-in and failed plugins."""
    import json as _json
    from types import SimpleNamespace

    import ciao.providers.opencode as mod

    routes = {
        "/api/skill": [{"id": "opencode", "path": "/builtin/opencode.md"}, {"id": "pdf"}],
        "/api/plugin": [
            {"id": "opencode.config.worktree", "source": {"type": "builtin"}, "state": {"status": "active"}},
            {"id": "review", "source": {"type": "local"}, "state": {"status": "active"}},
            {"id": "dupe", "source": {"type": "local"}, "state": {"status": "failed"}},
        ],
        "/api/mcp": [{"name": "github"}],
    }

    def fake_run(cmd, *a, **k):
        if cmd[1:3] == ["api", "GET"]:
            return SimpleNamespace(stdout=_json.dumps({"data": routes[cmd[3]]}), returncode=0)
        if cmd[1] == "--version":
            return SimpleNamespace(stdout="opencode v2.0.16\n", returncode=0)
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr("subprocess.run", fake_run)

    status = mod.opencode_login_status()

    assert status["skills"] == ["opencode", "pdf", "review"]
    assert status["mcps"] == ["github"]


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
    monkeypatch.setattr("subprocess.run", lambda *a, **k: __import__("types").SimpleNamespace(stdout="opencode v2.0.16\n"))

    status = mod.opencode_login_status()

    assert status["ok"] is True
    assert status["auth"] == "free"
    assert "authenticated" not in status["detail"]
    assert "free models only" in status["detail"]


def test_status_rejects_opencode_v1_with_an_update_message(monkeypatch):
    import ciao.providers.opencode as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: __import__("types").SimpleNamespace(stdout="opencode v1.18.18\n"),
    )
    status = mod.opencode_login_status()
    assert status["ok"] is False
    assert status["auth"] == "unsupported_version"
    assert status["detail"] == OPENCODE_V2_REQUIRED


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
        async def get(self, path):
            calls["n"] += 1
            if path == "/api/provider":
                return SimpleNamespaceResponse(
                    {"data": [{"id": "opencode", "activation": "auto"}]}
                )
            return SimpleNamespaceResponse({"data": [{
                "providerID": "opencode", "modelID": "m", "name": "M",
                "enabled": True, "variants": [],
            }]})

    class SimpleNamespaceResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

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
async def test_model_catalog_waits_for_a_fresh_server_to_load_its_models(tmp_path, monkeypatch):
    """opencode 2.0.16 answers /api/info before its providers load, so the
    first /api/model is an empty list. Caching that emptied the model picker."""
    import ciao.providers.opencode as mod

    mod._MODEL_CACHE.clear()
    monkeypatch.setattr(mod, "_CATALOG_WARMUP_POLL", 0.0)
    calls = {"n": 0}

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        async def get(self, _path):
            calls["n"] += 1
            if calls["n"] < 3:
                return Response({"data": []})
            return Response({"data": [{
                "providerID": "openrouter", "modelID": "m", "name": "M",
                "enabled": True, "variants": [],
            }]})

    class FakeServer:
        def __init__(self, _root):
            pass

        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *_exc):
            return None

    monkeypatch.setattr(mod, "_EphemeralServer", FakeServer)

    catalog = await mod.OpencodeProvider.model_catalog(tmp_path)

    assert [item["model"] for item in catalog] == ["openrouter/m"]
    assert calls["n"] == 3
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


# ── control-plane auto-approval ─────────────────────────────────────────


def test_control_plane_tools_do_not_prompt_in_the_permissive_modes():
    for mode in ("auto", "bypass"):
        shell = _shell_patterns(mode)
        assert not shell["allow"], mode
    assert _actions("bypass")["*"] == "allow"


def test_manual_mode_still_prompts_for_every_action():
    assert _actions("normal")["*"] == "ask"
    assert _shell_patterns("normal")["ask"] == set()


def test_auto_gates_every_shell_command_without_argv_prefixes():
    actions = _actions("auto")
    assert actions["*"] == "allow"
    assert actions["shell"] == "ask"
    assert _shell_patterns("auto") == {"allow": set(), "ask": {"*"}}


def test_plan_mode_grants_no_control_plane_allowance():
    """Plan's contract is propose-don't-act; an allow rule would hole it."""
    assert _shell_patterns("plan")["allow"] == set()


# ── activity-row rendering ──────────────────────────────────────────────


def test_a_projected_running_tool_is_announced_with_its_input(tmp_path):
    provider = _provider(tmp_path)
    running = {"part": {
        "type": "tool", "id": "prt_1", "callID": "c1", "tool": "shell",
        "state": {"status": "running", "input": {"command": "echo hi"}},
    }}
    events = provider._part_updated(running)
    assert len(events) == 1
    assert events[0].tool_name == "shell"
    assert events[0].tool_input == "echo hi"


def test_a_tool_with_genuinely_empty_input_is_still_announced(tmp_path):
    """Skipping pending must not swallow a tool that takes no arguments."""
    provider = _provider(tmp_path)
    events = provider._part_updated({"part": {
        "type": "tool", "id": "prt_2", "callID": "c2", "tool": "read",
        "state": {"status": "running", "input": {}},
    }})
    assert len(events) == 1
    assert events[0].tool_name == "read"
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
    assert events[0].message == "Approve use of shell?"


def test_dollar_brace_is_not_opencode_interpolation_syntax():
    """OpenCode's config substitution is
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
# opencode addresses models as providerID/modelID. Qualified ids pass through;
# a bare id is resolved against the V2 catalog and an unknown id is rejected.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("anthropic/claude-sonnet-4-6", ("anthropic", "claude-sonnet-4-6")),
        ("sonnet", ("anthropic", "sonnet")),
        ("  sonnet  ", ("anthropic", "sonnet")),
        ("", ("", "")),
    ],
)
async def test_resolve_model_resolves_unqualified_ids(
    tmp_path, requested, expected
):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"providerID": "anthropic", "modelID": "sonnet", "enabled": True}
            ]}

    class _Client:
        @staticmethod
        async def get(path: str):
            assert path == "/api/model"
            return _Response()

    provider = _provider(tmp_path)
    assert await provider._resolve_model(_Client(), requested) == expected


@pytest.mark.asyncio
async def test_resolve_model_waits_for_a_fresh_server_to_load_its_models(
    tmp_path, monkeypatch
):
    """A new chat server lists no models until its providers load; a valid
    bare id must not be rejected in that window."""
    import ciao.providers.opencode as mod

    monkeypatch.setattr(mod, "_CATALOG_WARMUP_POLL", 0.0)
    calls = {"n": 0}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            if calls["n"] < 3:
                return {"data": []}
            return {"data": [
                {"providerID": "openrouter", "modelID": "glm", "enabled": True}
            ]}

    class _Client:
        @staticmethod
        async def get(_path: str):
            calls["n"] += 1
            return _Response()

    assert await _provider(tmp_path)._resolve_model(_Client(), "glm") == (
        "openrouter", "glm",
    )
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_resolve_model_rejects_an_unknown_bare_id(tmp_path, monkeypatch):
    import ciao.providers.opencode as mod

    monkeypatch.setattr(mod, "_CATALOG_WARMUP_TIMEOUT", 0.0)
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    class _Client:
        @staticmethod
        async def get(_path: str):
            return _Response()

    with pytest.raises(ValueError, match="was not found"):
        await _provider(tmp_path)._resolve_model(_Client(), "missing")


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
