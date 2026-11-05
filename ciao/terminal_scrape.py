"""Scrape a helper terminal's output: plain text, https URLs and the last error.

Pure and stdlib only. The engine reads the bytes of an engine-built helper
program (``claude mcp login``, ``gh auth login``) and needs two things out
of them: the sign-in URL and the line that says why it failed. Nothing here
logs or stores the bytes.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: The most bytes one scrape reads: the tail of the output, never more.
SCRAPE_MAX_BYTES = 65536

_OSC8 = re.compile(rb"\x1b\]8;[^;\x07\x1b]*;([^\x07\x1b]{1,4096})(?:\x07|\x1b\\)")
_OSC_ANY = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_OTHER = re.compile(rb"\x1b[@-Z\\-_]")
_PLAIN_URL = re.compile(r"https://[^\s\"'<>\x00-\x1f\x7f]{1,4096}")
_ERROR_MARKERS = (
    "error",
    "couldn't",
    "could not",
    "failed",
    "denied",
    "invalid",
    "not found",
)
_ERROR_LINE_MAX_CHARS = 300


def _is_https_url(url: str) -> bool:
    if not url or len(url) > 4096:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
    )


def strip_terminal_controls(raw: bytes) -> str:
    """The readable text of the output's last :data:`SCRAPE_MAX_BYTES`."""
    data = raw[-SCRAPE_MAX_BYTES:]
    data = _OSC_ANY.sub(b"", data)
    data = _CSI.sub(b"", data)
    data = _ESC_OTHER.sub(b"", data)
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch
        for ch in text
        if ch in "\n\t" or (ord(ch) >= 32 and not 0x7F <= ord(ch) <= 0x9F)
    )


def osc8_urls(raw: bytes) -> list[str]:
    """The https targets of OSC 8 hyperlinks, in order, without repeats."""
    found: list[str] = []
    for match in _OSC8.finditer(raw[-SCRAPE_MAX_BYTES:]):
        try:
            url = match.group(1).decode("ascii", errors="strict")
        except UnicodeDecodeError:
            continue
        if _is_https_url(url) and url not in found:
            found.append(url)
    return found


def first_https_url(raw: bytes) -> str:
    """The first https URL in the output, hyperlink first; ``""`` when none."""
    linked = osc8_urls(raw)
    if linked:
        return linked[0]
    for match in _PLAIN_URL.finditer(strip_terminal_controls(raw)):
        url = match.group(0).rstrip(".,)")
        if _is_https_url(url):
            return url
    return ""


def last_error_line(text: str) -> str:
    """The last line that reads as an error, collapsed and bounded; ``""``."""
    for line in reversed(text.split("\n")):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(marker in lowered for marker in _ERROR_MARKERS):
            collapsed = " ".join(stripped.split())
            return collapsed[:_ERROR_LINE_MAX_CHARS]
    return ""
