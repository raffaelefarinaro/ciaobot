"""`ciao.redact`: credential-shaped text is dropped before it reaches a prompt."""

from __future__ import annotations

from ciao.redact import redact_secret_line, redact_text


def test_assignment_value_redacted_label_kept() -> None:
    assert redact_secret_line("api_key=SECRET123") == "api_key=[redacted]"


def test_opaque_blob_line_redacted() -> None:
    assert redact_secret_line("A" * 45) == "[redacted]"


def test_url_userinfo_redacted() -> None:
    assert (
        redact_text("https://user:ghp_abc@github.com/x")
        == "https://[redacted]@github.com/x"
    )
    assert redact_text("git push https://tok@host/r") == "git push https://[redacted]@host/r"


def test_plain_text_unchanged_and_newline_preserved() -> None:
    assert redact_text("hello\nworld\n") == "hello\nworld\n"
    assert redact_text("a\r\npassword: x\n") == "a\r\npassword: [redacted]\n"
    assert redact_text("") == ""
