from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import _INHERITED_ENV_PREFIXES

# Keys the autouse fixtures in conftest.py setenv themselves (the isolation
# directories). Every other CIAO_/PWA_/GWS_ key must have been scrubbed before
# any test ran, so a Ciaobot chat's exported settings never reach the suite (#2356).
_FIXTURE_SET_KEYS = {
    "CIAO_MEMORY_DIR",
    "CIAO_LAUNCH_AGENTS_DIR",
    "CIAO_BOOTSTRAP_WORKSPACE",
    "CIAO_QUEUE_LOCK_DIR",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_inherited_ciaobot_environment_is_removed_before_tests() -> None:
    leaked = sorted(
        key
        for key in os.environ
        if key.startswith(_INHERITED_ENV_PREFIXES) and key not in _FIXTURE_SET_KEYS
    )
    # Names only: the values of these variables include credentials.
    assert leaked == [], f"inherited Ciaobot variables reached the test: {leaked}"


def test_conftest_import_removes_an_inherited_variable() -> None:
    # The in-process check above passes vacuously on CI, which exports no
    # CIAO_/PWA_/GWS_ variables. Importing conftest in a fresh interpreter with
    # probe variables set proves the scrub itself runs.
    probe = (
        "import os, tests.conftest\n"
        "assert 'CIAO_PROBE_2356' not in os.environ, 'CIAO_PROBE_2356 survived the scrub'\n"
        "assert 'GWS_PROBE_2356' not in os.environ, 'GWS_PROBE_2356 survived the scrub'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "CIAO_PROBE_2356": "1",
            "GWS_PROBE_2356": "1",
            "PYTHONPATH": str(_REPO_ROOT),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr:\n{result.stderr}"
