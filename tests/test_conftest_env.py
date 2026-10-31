from __future__ import annotations

import os

# Keys the autouse fixtures in conftest.py setenv themselves (the isolation
# directories). Every other CIAO_/PWA_/GWS_ key must have been scrubbed before
# any test ran, so a Ciaobot chat's exported settings never reach the suite (#2356).
_FIXTURE_SET_KEYS = {
    "CIAO_MEMORY_DIR",
    "CIAO_LAUNCH_AGENTS_DIR",
    "CIAO_BOOTSTRAP_WORKSPACE",
    "CIAO_QUEUE_LOCK_DIR",
}


def test_inherited_ciaobot_environment_is_removed_before_tests() -> None:
    prefixes = ("CIAO_", "PWA_", "GWS_")
    leaked = sorted(
        key for key in os.environ if key.startswith(prefixes) and key not in _FIXTURE_SET_KEYS
    )
    # Names only: the values of these variables include credentials.
    assert leaked == [], f"inherited Ciaobot variables reached the test: {leaked}"
