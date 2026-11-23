"""The pure needs-you action contract: validation, keys, payloads and text bounds."""

from __future__ import annotations

import re
from typing import get_args

import pytest

from ciao.chat_actions import (
    ChatActionError,
    ChatActionErrorReason,
    action_key,
    action_payload,
    check_agent_params,
    new_action_id,
    normalize_error,
    normalize_note,
    validate_params,
)
from ciao.chat_actions import ChatAction
from types import MappingProxyType


def _refusal(kind: str, raw: dict[str, object]) -> str:
    with pytest.raises(ChatActionError) as info:
        validate_params(kind, raw)
    return info.value.reason


def test_mcp_auth_is_no_longer_a_kind() -> None:
    from ciao.chat_actions import ACTION_KINDS, AGENT_ACTION_KINDS

    assert _refusal("mcp_auth", {"server": "linear", "provider": "opencode"}) == "invalid_kind"
    assert "mcp_auth" not in AGENT_ACTION_KINDS
    assert "mcp_auth" not in ACTION_KINDS


def test_a_value_that_is_not_text_is_refused() -> None:
    assert _refusal("device_code", {"tool": 3}) == "invalid_type"


def test_an_unknown_kind_is_refused() -> None:
    assert _refusal("nope", {}) == "invalid_kind"


def test_a_kind_without_a_validator_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each child of the needs-you run registers its own kind; take one out to see the refusal.
    from ciao import chat_actions

    monkeypatch.delitem(chat_actions._VALIDATORS, "device_code")
    assert _refusal("device_code", {"tool": "gh"}) == "unsupported_kind"


def test_action_key() -> None:
    assert action_key("device_code", {"tool": "gh", "session_id": "s1"}) == "device_code:gh"
    assert action_key("restart", {}) == "restart"


def test_action_payload_is_a_fresh_copy_with_the_contract_keys() -> None:
    action = ChatAction(
        id="a1",
        kind="device_code",
        origin="agent",
        state="open",
        params=MappingProxyType({"tool": "gh"}),
        error="",
        created_at=1.0,
    )
    payload = action_payload(action)
    assert set(payload) == {"id", "kind", "origin", "state", "params", "error", "created_at"}
    payload["params"]["tool"] = "changed"  # type: ignore[index]
    assert action.params["tool"] == "gh"


def test_new_action_ids_are_random_hex() -> None:
    first, second = new_action_id(), new_action_id()
    assert first != second
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert re.fullmatch(r"[0-9a-f]{32}", second)


def test_normalize_note() -> None:
    assert normalize_note("  hi  ") == "hi"
    assert normalize_note("") == ""
    assert len(normalize_note("a" * 200)) == 200
    with pytest.raises(ChatActionError) as too_long:
        normalize_note("a" * 201)
    assert too_long.value.reason == "too_long"
    with pytest.raises(ChatActionError) as bad:
        normalize_note("a\nb")
    assert bad.value.reason == "invalid_characters"


def test_normalize_error_keeps_one_short_printable_line() -> None:
    assert normalize_error("\n  first line \nsecond") == "first line"
    long = normalize_error("x" * 400)
    assert len(long) == 300
    assert long.endswith("…")
    assert normalize_error("a\x1bb") == "ab"


def test_refusals_never_echo_the_value() -> None:
    with pytest.raises(ChatActionError) as info:
        validate_params("device_code", {"tool": "SECRET\x1b"})
    assert "SECRET" not in str(info.value)


def test_check_agent_params_refuses_engine_only_keys() -> None:
    raw = {"tool": "gh", "url": "https://a.example/"}
    with pytest.raises(ChatActionError) as info:
        check_agent_params("device_code", raw)
    assert info.value.reason == "unknown_param"
    assert check_agent_params("device_code", {"tool": "gh"}) is None
    assert check_agent_params("nope", raw) is None


def test_the_reason_set_is_the_whole_run() -> None:
    assert set(get_args(ChatActionErrorReason)) == {
        "invalid_kind",
        "unsupported_kind",
        "invalid_type",
        "invalid_encoding",
        "missing_param",
        "unknown_param",
        "invalid_param",
        "empty",
        "too_long",
        "invalid_characters",
        "invalid_url",
        "invalid_body",
        "invalid_state",
        "not_local",
    }
