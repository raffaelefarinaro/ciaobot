"""``python -m ciao`` runs the CLI from any interpreter (#791).

The package's only entry points used to be the ``ciao``/``ciaobot`` console
scripts, so running the engine from a known interpreter without that shim on
PATH failed with "No module named ciao.__main__". ``ciao/__main__.py`` closes
that gap: these tests check the usage output and that a failing invocation
propagates its exit code.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    """``python -m ciao <args>`` against this checkout, as the plan spells it."""
    return subprocess.run(
        [sys.executable, "-m", "ciao", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(_ROOT)},
        timeout=60,
    )


def test_python_dash_m_ciao_runs_the_cli() -> None:
    result = _run_module("--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_python_dash_m_ciao_propagates_the_exit_code() -> None:
    result = _run_module("definitely-not-a-command")
    assert result.returncode != 0