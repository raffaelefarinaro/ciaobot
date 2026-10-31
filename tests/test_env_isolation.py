"""The suite must not see the environment of the shell that started it.

A Ciaobot chat exports its own ``CIAO_*`` settings (vault root, workspace,
agent token) and ``PWA_AUTH_TOKEN`` to every command it runs, so pytest
started from a chat inherits them. Read by the code under test, they made
dozens of CLI and config tests fail on an unchanged checkout.
"""

from __future__ import annotations

import os

from tests.conftest import INHERITED_ENV_PREFIXES

# Names a Ciaobot chat exports; none is set by a conftest fixture.
CHAT_EXPORTS = ("CIAO_VAULT_ROOT", "CIAO_AGENT_TOKEN", "CIAO_CHAT_ID", "CIAO_BUNDLED_APP", "PWA_AUTH_TOKEN")


def test_inherited_ciao_and_pwa_variables_are_gone() -> None:
    leaked = [name for name in CHAT_EXPORTS if name in os.environ]
    assert leaked == []


def test_the_stripped_prefixes_cover_every_chat_export() -> None:
    assert all(name.startswith(INHERITED_ENV_PREFIXES) for name in CHAT_EXPORTS)
