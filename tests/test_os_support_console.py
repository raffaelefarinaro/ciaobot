"""ciao.os_support.console: the CLI's standard streams carry UTF-8 (#696)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ciao
from ciao.os_support.console import use_utf8_stdio

_REPO = Path(ciao.__file__).resolve().parents[1]


def _stream(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


def test_a_piped_cli_reply_is_utf8(tmp_path: Path) -> None:
    """What an agent sees: the CLI's reply read through a pipe, as bytes."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = subprocess.run(
        [
            sys.executable, "-m", "ciao", "vault-search", "über",
            "--vault-root", str(vault), "--runtime-root", str(tmp_path / "runtime"),
        ],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(_REPO)},
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "über".encode("utf-8") in result.stdout, result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_leaves_the_streams_as_they_were(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _stream("latin-1")
    monkeypatch.setattr(sys, "stdout", out)
    use_utf8_stdio()
    assert sys.stdout is out
    assert out.encoding == "latin-1"


@pytest.mark.skipif(sys.platform != "win32", reason="code-page streams are the Windows branch")
def test_windows_switches_a_code_page_stream_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _stream("cp1252"), _stream("cp1252", errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    use_utf8_stdio()
    out.write("ü")
    out.flush()
    assert out.buffer.getvalue() == "ü".encode("utf-8")  # type: ignore[attr-defined]
    assert err.encoding == "utf-8"
    assert err.errors == "backslashreplace"


@pytest.mark.skipif(sys.platform != "win32", reason="code-page streams are the Windows branch")
def test_windows_skips_a_missing_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", None)  # pythonw starts with no streams
    use_utf8_stdio()
