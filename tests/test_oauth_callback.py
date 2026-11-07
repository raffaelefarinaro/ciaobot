"""Pure validation of a pasted loopback OAuth callback.

The authorize URL is the one the engine obtained from the provider; the pasted URL
is what the user copied from the failed page. Every refusal is a closed reason word.
"""

from __future__ import annotations

import pytest

from ciao.oauth_callback import (
    CallbackRefused,
    CallbackTarget,
    validate_pasted_callback,
)

# Claude's shape: the CLI listens on localhost:<port>.
AUTH = (
    "https://mcp.notion.com/authorize?response_type=code"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcallback&state=S1&scope=default"
)
# OpenCode's verified shape (a random 127.0.0.1 port) plus a state.
OC_AUTH = (
    "https://mcp.linear.app/authorize?response_type=code&client_id=x"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A56446%2Fcallback&state=S2"
)
# A configured redirect path on a 127.0.0.1 listener.
PATH_AUTH = (
    "https://mcp.example.com/authorize?response_type=code"
    "&redirect_uri=http%3A%2F%2F127.0.0.1%3A19876%2Fmcp%2Foauth%2Fcallback&state=S3"
)


def test_valid_claude_callback_replays_whole_query_to_127() -> None:
    target = validate_pasted_callback(
        AUTH,
        "http://localhost:62471/callback?code=abc&state=S1&iss=https%3A%2F%2Fmcp.notion.com",
    )
    assert target.replay_url() == (
        "http://127.0.0.1:62471/callback?code=abc&state=S1&iss=https%3A%2F%2Fmcp.notion.com"
    )


def test_valid_opencode_callback() -> None:
    target = validate_pasted_callback(OC_AUTH, "http://127.0.0.1:56446/callback?code=x&state=S2")
    assert target.port == 56446
    assert target.path == "/callback"


def test_surrounding_whitespace_is_ignored() -> None:
    target = validate_pasted_callback(AUTH, "  http://localhost:62471/callback?code=a&state=S1\n")
    assert target.port == 62471


def test_a_configured_redirect_path_is_read_from_the_authorize_url() -> None:
    target = validate_pasted_callback(
        PATH_AUTH, "http://127.0.0.1:19876/mcp/oauth/callback?code=x&state=S3"
    )
    assert target.path == "/mcp/oauth/callback"
    with pytest.raises(CallbackRefused) as refused:
        validate_pasted_callback(PATH_AUTH, "http://127.0.0.1:19876/callback?code=x&state=S3")
    assert refused.value.reason == "wrong_path"


_BASE = "http://localhost:62471/callback"


@pytest.mark.parametrize(
    ("pasted", "reason"),
    [
        ("https://localhost:62471/callback?code=a&state=S1", "wrong_scheme"),
        ("http://evil.example:62471/callback?code=a&state=S1", "wrong_host"),
        ("http://169.254.169.254:62471/callback?code=a&state=S1", "wrong_host"),
        ("http://u:p@localhost:62471/callback?code=a&state=S1", "wrong_host"),
        ("http://localhost:62472/callback?code=a&state=S1", "wrong_port"),
        ("http://localhost/callback?code=a&state=S1", "wrong_port"),
        ("http://localhost:62471/other?code=a&state=S1", "wrong_path"),
        ("http://localhost:62471/callback/../x?code=a&state=S1", "wrong_path"),
        (f"{_BASE}?code=a&state=S9", "state_mismatch"),
        (f"{_BASE}?code=a&state=S1&state=S1", "state_mismatch"),
        (f"{_BASE}?state=S1", "missing_code"),
        (f"{_BASE}?code=a&code=b&state=S1", "missing_code"),
        (f"{_BASE}?error=access_denied&state=S1", "provider_error"),
        (f"{_BASE}?code=a b&state=S1", "not_a_url"),
        (f"{_BASE}?code=<a>&state=S1", "bad_query"),
        (f"{_BASE}?code=a&state=S1#frag", "bad_query"),
        ("x" * 4097, "too_long"),
    ],
)
def test_refusals(pasted: str, reason: str) -> None:
    with pytest.raises(CallbackRefused) as refused:
        validate_pasted_callback(AUTH, pasted)
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    "authorize_url",
    [
        "http://",
        "https://mcp.example.com/authorize?response_type=code&state=S1",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcb",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcb&state=",
        "https://mcp.example.com/authorize?redirect_uri=https%3A%2F%2Flocalhost%3A62471%2Fcb&state=S1",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Fexample.com%3A62471%2Fcb&state=S1",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%2Fcb&state=S1",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A80%2Fcb&state=S1",
        "https://mcp.example.com/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcb&state=S1&state=S1",
    ],
)
def test_bad_authorize_urls(authorize_url: str) -> None:
    with pytest.raises(CallbackRefused) as refused:
        validate_pasted_callback(authorize_url, "http://localhost:1/cb?code=a&state=S1")
    assert refused.value.reason == "bad_authorize_url"


def test_refusal_message_never_contains_the_url() -> None:
    with pytest.raises(CallbackRefused) as refused:
        validate_pasted_callback(AUTH, f"{_BASE}?code=SECRETCODE&state=S9")
    assert str(refused.value) == refused.value.reason
    assert "SECRETCODE" not in repr(refused.value)
    assert "SECRETCODE" not in repr(CallbackTarget(port=1, path="/", query="code=SECRETCODE"))
