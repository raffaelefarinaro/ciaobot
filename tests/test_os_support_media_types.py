"""ciao.os_support.media_types: one media-type answer per file name (#696 C9)."""

from __future__ import annotations

import mimetypes
import sys

import pytest

from ciao.os_support.media_types import guess_type


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.zip", "application/zip"), ("b.png", "image/png"), ("c.md", "text/markdown"), ("d.pdf", "application/pdf")],
)
def test_common_types_are_the_standard_ones(name: str, expected: str) -> None:
    assert guess_type(name) == expected


def test_an_unknown_extension_has_no_type() -> None:
    assert guess_type("e.definitely-not-a-type") is None


@pytest.mark.skipif(sys.platform != "win32", reason="the registry is the Windows branch")
def test_windows_ignores_what_the_registry_says(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever another program registered for .zip, the engine's answer is Python's own."""
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: ("application/x-zip-compressed", None))
    assert guess_type("a.zip") == "application/zip"


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_is_mimetypes_guess_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mimetypes, "guess_type", lambda name, *a, **k: ("x/from-system", None))
    assert guess_type("a.zip") == "x/from-system"
