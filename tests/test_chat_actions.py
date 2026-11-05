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


def test_mcp_auth_params_are_validated_and_stripped() -> None:
    assert validate_params("mcp_auth", {"server": " linear ", "provider": "opencode"}) == {
        "server": "linear",
        "provider": "opencode",
    }


def test_mcp_auth_requires_server_and_provider() -> None:
    assert _refusal("mcp_auth", {"provider": "opencode"}) == "missing_param"
    assert _refusal("mcp_auth", {"server": "linear"}) == "missing_param"


def test_mcp_auth_refuses_an_unknown_param() -> None:
    raw = {"server": "linear", "provider": "opencode", "note": "x"}
    assert _refusal("mcp_auth", raw) == "unknown_param"


def test_mcp_auth_refuses_an_unknown_provider() -> None:
    assert _refusal("mcp_auth", {"server": "linear", "provider": "codex"}) == "invalid_param"


def test_mcp_auth_server_bound() -> None:
    accepted = validate_params("mcp_auth", {"server": "a" * 128, "provider": "opencode"})
    assert accepted["server"] == "a" * 128
    assert _refusal("mcp_auth", {"server": "a" * 129, "provider": "opencode"}) == "too_long"


@pytest.mark.parametrize("bad", ["\x1b", "‮", "​", "\n"])
def test_mcp_auth_server_refuses_invisible_characters(bad: str) -> None:
    raw = {"server": f"lin{bad}ear", "provider": "opencode"}
    assert _refusal("mcp_auth", raw) == "invalid_characters"


def test_mcp_auth_url_must_be_http_or_https() -> None:
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://"):
        raw = {"server": "linear", "provider": "opencode", "url": url}
        assert _refusal("mcp_auth", raw) == "invalid_url", url
    ok = {"server": "linear", "provider": "opencode", "url": "https://mcp.linear.app/authorize?x=1"}
    assert validate_params("mcp_auth", ok)["url"] == "https://mcp.linear.app/authorize?x=1"


def test_mcp_auth_url_bound() -> None:
    at_limit = "https://e.com/" + "a" * (4096 - len("https://e.com/"))
    assert len(at_limit) == 4096
    ok = {"server": "linear", "provider": "opencode", "url": at_limit}
    assert validate_params("mcp_auth", ok)["url"] == at_limit
    over = {"server": "linear", "provider": "opencode", "url": at_limit + "a"}
    assert _refusal("mcp_auth", over) == "too_long"


def test_a_value_that_is_not_text_is_refused() -> None:
    assert _refusal("mcp_auth", {"server": 3, "provider": "opencode"}) == "invalid_type"


def test_an_unknown_kind_is_refused() -> None:
    assert _refusal("nope", {}) == "invalid_kind"


def test_a_kind_without_a_validator_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each child of the needs-you run registers its own kind; take one out to see the refusal.
    from ciao import chat_actions

    monkeypatch.delitem(chat_actions._VALIDATORS, "mcp_auth")
    assert _refusal("mcp_auth", {"server": "linear", "provider": "opencode"}) == "unsupported_kind"


def test_action_key() -> None:
    assert action_key("mcp_auth", {"server": "linear", "provider": "opencode"}) == "mcp_auth:linear"
    assert action_key("restart", {}) == "restart"


def test_action_payload_is_a_fresh_copy_with_the_contract_keys() -> None:
    action = ChatAction(
        id="a1",
        kind="mcp_auth",
        origin="engine",
        state="open",
        params=MappingProxyType({"server": "linear", "provider": "opencode"}),
        error="",
        created_at=1.0,
    )
    payload = action_payload(action)
    assert set(payload) == {"id", "kind", "origin", "state", "params", "error", "created_at"}
    payload["params"]["server"] = "changed"  # type: ignore[index]
    assert action.params["server"] == "linear"


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
        validate_params("mcp_auth", {"server": "SECRET\x1b", "provider": "opencode"})
    assert "SECRET" not in str(info.value)


def test_check_agent_params_refuses_engine_only_keys() -> None:
    raw = {"server": "x", "provider": "opencode", "url": "https://a.example/"}
    with pytest.raises(ChatActionError) as info:
        check_agent_params("mcp_auth", raw)
    assert info.value.reason == "unknown_param"
    assert check_agent_params("mcp_auth", {"server": "x", "provider": "opencode"}) is None
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
        "unsupported_provider",
        "not_local",
        "already_used",
        "no_pending_sign_in",
        "bad_callback_url",
        "state_mismatch",
    }
