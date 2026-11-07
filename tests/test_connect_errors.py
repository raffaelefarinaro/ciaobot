"""Unit tests for the shared connection-error annotation helpers (#178)."""

from ciao.providers.connect_errors import (
    annotate_connection_host,
    annotate_reauth,
    classify_connection_error,
)


def test_classify_distinguishes_dns_from_refused_and_auth() -> None:
    """The category tells the operator whether to chase DNS, a refused
    port, a timeout, or a missing key (#178)."""
    assert classify_connection_error("getaddrinfo ENOTFOUND api.anthropic.com") == "dns"
    assert classify_connection_error("Unable to connect to API (ENOTFOUND)") == "dns"
    assert classify_connection_error("Temporary failure in name resolution") == "dns"
    assert classify_connection_error("connect ECONNREFUSED 127.0.0.1:11434") == "refused"
    assert classify_connection_error("connection refused") == "refused"
    assert classify_connection_error("ETIMEDOUT") == "timeout"
    assert classify_connection_error("connection timed out") == "timeout"
    assert classify_connection_error("Invalid API key provided") == "auth"
    assert classify_connection_error("401 Unauthorized") == "auth"
    assert classify_connection_error("403 Forbidden") == "auth"
    assert classify_connection_error("Not logged in. Please run /login") == "auth"
    # Bare status digits inside unrelated text don't trigger a false auth label.
    assert classify_connection_error("Generated 401 tokens") is None
    assert classify_connection_error("failed to fetch") == "connect"
    assert classify_connection_error("Model refused the request.") is None
    assert classify_connection_error("") is None


def test_auth_takes_precedence_over_dns_markers() -> None:
    """An auth error that happens to mention a host isn't mislabelled DNS."""
    assert classify_connection_error(
        "getaddrinfo ENOTFOUND api.anthropic.com: 401 Unauthorized"
    ) == "auth"


def test_annotate_adds_host_and_category() -> None:
    out = annotate_connection_host(
        "API Error: Unable to connect to API (ENOTFOUND)", "api.anthropic.com"
    )
    assert "host: api.anthropic.com" in out
    assert "category: dns" in out


def test_annotate_classifies_refused_and_timeout() -> None:
    assert "category: refused" in annotate_connection_host(
        "connect ECONNREFUSED 127.0.0.1:11434", "localhost"
    )
    assert "category: timeout" in annotate_connection_host(
        "connection timed out", "api.openai.com"
    )


def test_annotate_labels_auth_separately() -> None:
    """An auth failure is labelled auth, not dns, so the operator fixes the
    key instead of the network."""
    out = annotate_connection_host("Invalid API key", "api.anthropic.com")
    assert "category: auth" in out
    assert "category: dns" not in out


def test_annotate_is_idempotent_and_skips_non_connection_errors() -> None:
    once = annotate_connection_host(
        "API Error: Unable to connect to API (ENOTFOUND)", "api.anthropic.com"
    )
    assert annotate_connection_host(once, "api.anthropic.com") == once
    # Non-connection error: untouched.
    assert annotate_connection_host("Model refused the request.", "api.anthropic.com") == (
        "Model refused the request."
    )
    # Empty inputs: untouched.
    assert annotate_connection_host("", "api.anthropic.com") == ""
    assert annotate_connection_host("ENOTFOUND", "") == "ENOTFOUND"


def test_annotate_does_not_double_add_host_when_already_present() -> None:
    """If the CLI already named the host, we don't duplicate it; the category
    is still added (#178)."""
    out = annotate_connection_host(
        "getaddrinfo ENOTFOUND api.anthropic.com", "api.anthropic.com"
    )
    assert out == "getaddrinfo ENOTFOUND api.anthropic.com (category: dns)"
    assert out.count("api.anthropic.com") == 1


def test_invalidated_oauth_grant_is_reauth_not_auth() -> None:
    """An invalidated or revoked grant is a sign-in-again failure, not a
    missing key, so it gets its own category ahead of ``auth`` (#2648)."""
    assert classify_connection_error(
        "Encountered invalidated oauth token for user, failing request"
    ) == "reauth"
    assert classify_connection_error("invalid_grant: refresh token is invalid") == "reauth"
    assert classify_connection_error("Token has been expired or revoked.") == "reauth"
    assert classify_connection_error("Invalid API key provided") == "auth"
    assert classify_connection_error("Not logged in. Please run /login") == "auth"


def test_annotate_reauth_names_provider_upstream_and_action() -> None:
    """The operator sees which provider account to reconnect (#2648)."""
    raw = "Encountered invalidated oauth token for user, failing request"
    assert annotate_reauth(raw, "opencode", "openai/gpt-6.1-terra") == (
        f"{raw} (provider: opencode openai, action: sign in again)"
    )
    assert annotate_reauth(raw, "claude", "claude-sonnet-5-5") == (
        f"{raw} (provider: claude, action: sign in again)"
    )


def test_annotate_reauth_leaves_other_errors_and_is_idempotent() -> None:
    """Only reauth text changes, and a second pass adds nothing (#2648)."""
    raw = "Encountered invalidated oauth token for user, failing request"
    assert annotate_reauth("Invalid API key provided", "claude") == "Invalid API key provided"
    assert annotate_reauth("", "claude") == ""
    once = annotate_reauth(raw, "claude")
    assert annotate_reauth(once, "claude") == once
