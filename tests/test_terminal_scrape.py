"""Scraping a helper terminal's output: URLs and the last error line."""

from __future__ import annotations

from ciao.terminal_scrape import (
    SCRAPE_MAX_BYTES,
    first_https_url,
    last_error_line,
    osc8_urls,
    strip_terminal_controls,
)

CLAUDE_AUTHORIZE = (
    "https://mcp.notion.com/authorize?response_type=code"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcallback&state=abc"
)


def test_osc8_bel_terminated_url_from_claude_mcp_login() -> None:
    raw = (
        b'Starting authentication for "notiontest"\xe2\x80\xa6\r\n'
        b"Visit this URL to authorize:\r\n"
        b"  \x1b]8;;" + CLAUDE_AUTHORIZE.encode() + b"\x07"
        b"\x1b[94m" + CLAUDE_AUTHORIZE.encode() + b"\x1b[39m"
        b"\x1b]8;;\x07\r\n"
    )
    assert first_https_url(raw) == CLAUDE_AUTHORIZE
    assert osc8_urls(raw) == [CLAUDE_AUTHORIZE]


def test_osc8_st_terminated_url() -> None:
    raw = (
        b"\x1b]8;;" + CLAUDE_AUTHORIZE.encode() + b"\x1b\\"
        + CLAUDE_AUTHORIZE.encode()
        + b"\x1b]8;;\x1b\\"
    )
    assert osc8_urls(raw) == [CLAUDE_AUTHORIZE]
    assert first_https_url(raw) == CLAUDE_AUTHORIZE


def test_plain_url_fallback_strips_trailing_punctuation() -> None:
    raw = b"open https://github.com/login/device."
    assert first_https_url(raw) == "https://github.com/login/device"


def test_non_https_and_credential_urls_are_ignored() -> None:
    assert first_https_url(b"http://x.example/a") == ""
    assert first_https_url(b"https://user:pw@x.example/") == ""


def test_strip_removes_csi_osc_and_controls() -> None:
    assert strip_terminal_controls(b"\x1b[31mred\x1b[0m\x07ok\r\n") == "redok\n"


def test_last_error_line_from_claude_failure() -> None:
    raw = (
        b'\x1b[31mCouldn\'t complete authentication for "notiontest": '
        b"Issuer mismatch\x1b[39m\r\n"
    )
    text = strip_terminal_controls(raw)
    assert (
        last_error_line(text)
        == 'Couldn\'t complete authentication for "notiontest": Issuer mismatch'
    )


def test_last_error_line_is_bounded() -> None:
    text = "error " + "x" * 1000
    assert len(last_error_line(text)) == 300


def test_scrape_reads_only_the_tail() -> None:
    raw = b"https://early.example/x " + b"a" * SCRAPE_MAX_BYTES
    assert "early.example" not in strip_terminal_controls(raw)
