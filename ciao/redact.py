"""Redaction of credential-shaped text before it is quoted into a prompt."""

from __future__ import annotations

import re

# Credential-shaped assignments whose VALUE is dropped. Defence in depth: the
# callers' own bounds (a search window's section, a log tail) come first.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token"
    r"|auth[_-]?token|refresh[_-]?token|password|passwd|passphrase"
    r"|private[_-]?key|bearer)\s*[:=]\s*)(\S.*)"
)

# A bare, long, space-free base64 run: a PEM body or a raw token carries no
# keyword of its own, so the assignment rule above would not see it.
_OPAQUE_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")

# `scheme://user:pass@host` and `scheme://token@host`: a credential-bearing URL.
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def redact_secret_line(line: str) -> str:
    """Drop the value of a credential-shaped line, keeping its label."""
    if _OPAQUE_BLOB_RE.match(line.strip()):
        return "[redacted]"
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: m.group(1) + "[redacted]", line)


def redact_text(text: str) -> str:
    """Redact every line of ``text`` and any URL userinfo, keeping line endings."""
    out: list[str] = []
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        ending = raw[len(body):]
        body = _URL_USERINFO_RE.sub(r"\1[redacted]@", body)
        out.append(redact_secret_line(body) + ending)
    return "".join(out)
