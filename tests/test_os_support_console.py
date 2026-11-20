"""The CLI's standard streams carry UTF-8 through a pipe (#696)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import ciao

_REPO = Path(ciao.__file__).resolve().parents[1]


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
