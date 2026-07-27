"""Shared connection-error annotation for provider result strings.

Schedule dispatches surface a provider error as the turn's result text, and
that text is what the operator sees in a failed ``schedule_dispatch`` job run.
A name-resolution or connect failure arrives as a generic string like
``API Error: Unable to connect to API (ENOTFOUND)`` with no host and no error
category, so the operator can't tell which endpoint failed to resolve (the
Anthropic API, an OpenRouter/Ollama gateway, a custom base URL, the Codex
binary) or whether it was DNS, a refused connection, a timeout, or auth.

The helpers here annotate the string with both the failing host and a short
category, so the recorded error identifies the endpoint and the failure
class. Shared across providers (Claude CLI routing covers Anthropic /
OpenRouter / Ollama; the Codex provider annotates its own results) so every
provider a schedule can dispatch through surfaces the same context. See
issue #178 (extending the Claude-only fix from #162 to all schedule
providers and adding error classification).
"""

# Markers that flag a result string as a hostless connection/DNS failure
# worth annotating. Kept in sync with the retryable-error classifier in
# ``ciao/web/project_chats.py::_is_retryable_connection_error``.
_HOSTLESS_CONNECT_MARKERS = (
    "unable to connect",
    "enotfound",
    "econnrefused",
    "etimedout",
    "getaddrinfo",
    "name resolution",
    "temporary failure in name resolution",
    "dns resolution failed",
    "failed to fetch",
    "network request failed",
    "connection refused",
    "connection reset",
    "connection closed",
    "connection timed out",
    "socket timeout",
    "connect timeout",
)

# Auth/credential failures are NOT connection errors. Label them distinctly so
# the operator doesn't chase a DNS or firewall fix for a missing API key. The
# numeric status codes are paired with their reason phrase to avoid matching
# the bare digits inside unrelated error text (e.g. "generated 401 tokens").
_AUTH_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "permission denied",
    "401 unauthorized",
    "403 forbidden",
)

_DNS_MARKERS = (
    "enotfound",
    "getaddrinfo",
    "name resolution",
    "temporary failure in name resolution",
    "dns resolution failed",
)

_REFUSED_MARKERS = (
    "econnrefused",
    "connection refused",
)

_TIMEOUT_MARKERS = (
    "etimedout",
    "timed out",
    "socket timeout",
    "connect timeout",
    "connection timed out",
)


def classify_connection_error(text: str) -> str | None:
    """Return a short category for a connection-error string, or ``None``.

    Categories: ``dns``, ``refused``, ``timeout``, ``auth``, ``connect``
    (a connection error we couldn't classify further), or ``None`` (not a
    recognised connection/auth error). DNS resolution failures, refused
    connections, and timeouts are network-side; auth failures are
    credential-side. Distinguishing them lets the operator pick the right
    remediation instead of guessing.
    """
    low = (text or "").lower()
    if not low:
        return None
    if any(marker in low for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in low for marker in _DNS_MARKERS):
        return "dns"
    if any(marker in low for marker in _REFUSED_MARKERS):
        return "refused"
    if any(marker in low for marker in _TIMEOUT_MARKERS):
        return "timeout"
    if any(marker in low for marker in _HOSTLESS_CONNECT_MARKERS):
        return "connect"
    return None


def _looks_like_connection_error(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _HOSTLESS_CONNECT_MARKERS)


def annotate_connection_host(text: str, host: str) -> str:
    """Append the failing host and error category to a connection/auth-error string.

    Annotates any string :func:`classify_connection_error` recognises, so a
    DNS failure, a refused connection, a timeout, or an auth/credential
    error all get labelled. ``host`` is added only when missing; the
    category is added when missing. Ordinary errors and already-annotated
    strings pass through untouched, so the helper is idempotent and safe to
    call on every error result.
    """
    if not host or not text:
        return text
    low = text.lower()
    category = classify_connection_error(text)
    if category is None and not _looks_like_connection_error(low):
        return text
    parts: list[str] = []
    if host.lower() not in low:
        parts.append(f"host: {host}")
    if category and f"category: {category}" not in low:
        parts.append(f"category: {category}")
    if not parts:
        return text
    return f"{text} ({', '.join(parts)})"