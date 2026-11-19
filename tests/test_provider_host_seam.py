"""The web hosts reach providers only through the BaseProvider contract (#3132)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ciao.web
from ciao.providers.base import BaseProvider

_WEB_ROOT = Path(ciao.web.__file__).parent
_FORBIDDEN = re.compile(r"OpencodeProvider|ClaudeProvider|\._connected")

# Every operation the hosts under ciao/web call on a provider object.
_HOST_SEAMS = (
    "is_connected",
    "current_session_id",
    "read_thread",
    "abort_session",
    "read_mcp_status",
    "model_catalog",
    "native_title_candidates",
    "delete_session",
)


def test_web_sources_name_no_concrete_provider_class_or_private_flag() -> None:
    offenders = [
        path.relative_to(_WEB_ROOT).as_posix()
        for path in sorted(_WEB_ROOT.rglob("*.py"))
        if _FORBIDDEN.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize("name", _HOST_SEAMS)
def test_base_provider_defines_every_host_seam(name: str) -> None:
    assert hasattr(BaseProvider, name)


@pytest.mark.parametrize(
    "name",
    (
        "reconnect_mcp_server",
        "mcp_sign_in_exempt",
        "start_mcp_oauth",
        "read_mcp_oauth_attempt",
        "cancel_mcp_oauth",
        "ensure_server",
    ),
)
def test_base_provider_has_no_mcp_management_seam(name: str) -> None:
    assert not hasattr(BaseProvider, name)
