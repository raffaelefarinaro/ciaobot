"""Unit tests for the shared connection-error annotation helpers (#178)."""

from ciao.providers.connect_errors import (
    annotate_connection_host,
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
