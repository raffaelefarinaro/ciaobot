"""Tests for the anonymous bug-report Google Form client."""
from __future__ import annotations

import contextlib

import pytest

from ciao import bug_report


_ENV_VARS = (
    "CIAO_BUG_REPORT_FORM_URL",
    "CIAO_BUG_REPORT_ENTRY_TITLE",
    "CIAO_BUG_REPORT_ENTRY_DETAILS",
    "CIAO_BUG_REPORT_ENTRY_SYSTEM",
)


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_BUG_REPORT_FORM_URL", "https://forms.example/formResponse")
    monkeypatch.setenv("CIAO_BUG_REPORT_ENTRY_TITLE", "entry.111")
    monkeypatch.setenv("CIAO_BUG_REPORT_ENTRY_DETAILS", "entry.222")
    monkeypatch.setenv("CIAO_BUG_REPORT_ENTRY_SYSTEM", "entry.333")


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Neutralize any baked-in defaults so "unconfigured" is deterministic.
    monkeypatch.setattr(bug_report, "_DEFAULT_FORM_URL", "")
    monkeypatch.setattr(bug_report, "_DEFAULT_ENTRY_TITLE", "")
    monkeypatch.setattr(bug_report, "_DEFAULT_ENTRY_DETAILS", "")
    monkeypatch.setattr(bug_report, "_DEFAULT_ENTRY_SYSTEM", "")


def test_not_configured_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    assert bug_report.is_configured() is False
    # Must not attempt any network call when unconfigured.
    monkeypatch.setattr(
        bug_report.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("should not POST when unconfigured"),
    )
    assert bug_report.submit_bug_report("t", "d", "sys") is False


def test_submit_posts_all_fields_and_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

    @contextlib.contextmanager
    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        yield FakeResponse()

    monkeypatch.setattr(bug_report.urllib.request, "urlopen", fake_urlopen)

    assert bug_report.submit_bug_report("boom", "traceback here", "MyOS 1.0") is True
    assert captured["url"] == "https://forms.example/formResponse"
    assert captured["method"] == "POST"
    body = captured["body"]
    # All three entry ids and their url-encoded values are present.
    assert "entry.111=boom" in body
    assert "entry.222=traceback+here" in body
    assert "entry.333=MyOS+1.0" in body
    assert captured["timeout"] == bug_report._TIMEOUT_SECONDS


def test_submit_autofills_system_info_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    captured: dict[str, str] = {}

    class FakeResponse:
        status = 200

    @contextlib.contextmanager
    def fake_urlopen(request, timeout=None):
        captured["body"] = request.data.decode("utf-8")
        yield FakeResponse()

    monkeypatch.setattr(bug_report.urllib.request, "urlopen", fake_urlopen)

    assert bug_report.submit_bug_report("t", "d") is True
    # gather_system_info() names Ciaobot and Python; both should ride along.
    assert "entry.333=Ciaobot" in captured["body"]


def test_submit_returns_false_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(bug_report.urllib.request, "urlopen", boom)
    assert bug_report.submit_bug_report("t", "d", "sys") is False


def test_gather_system_info_has_version_and_python() -> None:
    info = bug_report.gather_system_info()
    assert "Ciaobot" in info
    assert "Python" in info


def test_submit_rejects_non_string_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked test double (e.g. a MagicMock) must never be POSTed to the form."""
    from unittest.mock import MagicMock

    _configure(monkeypatch)
    monkeypatch.setattr(
        bug_report.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("should not POST for non-string fields"),
    )

    mock = MagicMock()
    # A MagicMock auto-attribute is not a str -> rejected before any POST.
    assert bug_report.submit_bug_report(mock.title, mock.details, "sys") is False
    # Empty/whitespace strings are rejected too.
    assert bug_report.submit_bug_report("   ", "details", "sys") is False
    assert bug_report.submit_bug_report("title", "", "sys") is False


def test_submit_rejects_leaked_mock_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-stringified mock repr must not be filed as a real report."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        bug_report.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("should not POST a leaked object repr"),
    )

    leaked = "<MagicMock id='139314758788368'>"
    assert bug_report.submit_bug_report(leaked, "real details", "sys") is False
    assert bug_report.submit_bug_report("real title", leaked, "sys") is False
