"""The web hosts reach providers only through the BaseProvider contract (#3132)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ciao.web
from ciao.providers.base import BaseProvider

_WEB_ROOT = Path(ciao.web.__file__).parent
_FORBIDDEN = re.compile(r"OpencodeAcpProvider|ClaudeProvider|\._connected")

# Every operation the hosts under ciao/web call on a provider object.
_HOST_SEAMS = (
    "is_connected",
    "current_session_id",
    "read_thread",
    "abort_session",
    "model_catalog",
    "invalidate_model_catalog",
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


_ACP_FORBIDDEN = re.compile(r"\bAcpProvider\b|ciao\.providers\.acp\b")


def test_web_sources_name_no_acp_provider_class() -> None:
    """ACP agents reach the hosts through ``provider_registry`` and ``BaseProvider`` only (#3137)."""
    offenders = [
        path.relative_to(_WEB_ROOT).as_posix()
        for path in sorted(_WEB_ROOT.rglob("*.py"))
        if _ACP_FORBIDDEN.search(path.read_text(encoding="utf-8"))
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


# A host branches on a provider id only where the difference is the agent's own
# on-disk or command-surface layout. Each entry is (file under ciao/web, stripped line)
# with its count, so an edit that moves a line does not break the guard, while a new
# branch does. Claude-only behaviour lives on ProviderDescriptor flags instead.
_PROVIDER_ID_BRANCH = re.compile(
    r"(?:==|!=)\s*[\"'](?:claude|opencode)[\"']|\bin\s*\{[^}]*[\"'](?:claude|opencode)[\"']"
)
_ALLOWED_PROVIDER_ID_BRANCHES: dict[tuple[str, str], int] = {
    # Command directories differ per agent (.opencode/commands vs .claude/commands).
    ("commands.py", 'if target == "opencode":'): 1,
    ("commands.py", 'if target == "claude":'): 1,
    # Connection-action cache invalidation: each agent keeps its own discovery cache.
    ("routes_api.py", 'if provider == "claude":'): 1,
    ("routes_api.py", 'if provider == "opencode":'): 1,
}


def test_web_sources_branch_on_no_acp_provider_id_outside_allowlist() -> None:
    from collections import Counter

    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(_WEB_ROOT.rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if _PROVIDER_ID_BRANCH.search(line):
                found[(path.name, line.strip())] += 1
    assert found == Counter(_ALLOWED_PROVIDER_ID_BRANCHES)
