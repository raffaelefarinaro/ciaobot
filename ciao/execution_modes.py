"""Provider-specific mappings and approval policies."""

from __future__ import annotations

import re
from pathlib import Path

# Permission-mode strength, weakest first: ``plan`` is read-only, ``normal``
# asks before acting, ``auto`` acts with safer defaults, ``bypass`` skips
# approvals. Mirrors ``BridgeMode`` in ciao.models. Shared by the control plane
# (an agent's ceiling) and the schedule dispatcher (a saved routine mode).
MODE_RANK: dict[str, int] = {"plan": 0, "normal": 1, "auto": 2, "bypass": 3}


def cap_schedule_mode(saved: str, default: str) -> str:
    """The mode a schedule run uses: its saved mode, never above ``default``.

    A saved mode is an upper limit, not a pin: when the workspace/engine default
    is lowered later, a routine an agent saved at a higher mode drops with it.
    An empty (or unrecognised) saved mode inherits ``default``.
    """
    if saved not in MODE_RANK or default not in MODE_RANK:
        return default
    return saved if MODE_RANK[saved] <= MODE_RANK[default] else default


# ── Ciaobot control-plane approval policy ────────────────────────────────
# Since S6 every Ciaobot operation runs as `ciao <noun> <verb>` through the
# harness's own shell tool. The per-tool MCP annotations that used to feed a
# static allow-list are gone. No `ciao …` argv prefix is pre-approved: an
# allow rule is a prefix that a shell suffix (``ciao help >/dev/null; <cmd>``)
# could ride past, so auto mode keeps a card on every shell command and users
# who want no cards switch to `bypass`. The control plane keeps its own mode
# gates and ``unattended_forbidden``.


_CREDENTIAL_DIR_NAMES = frozenset({".runtime", "secrets"})
# `.env`, `.env.local`, `.env-local`, `.env_prod`, `.envrc`; not `.envelope`.
_ENV_FILE_NAME = re.compile(r"\.env(rc)?([._-].*)?")


def is_credential_adjacent(path: Path) -> bool:
    """True when *path* (already resolved) names a credential store: a `.env`
    file or variant, or anything at any depth under a `.runtime` or `secrets`
    directory. Comparison is case-insensitive, matching `Path.match` on Windows
    and on case-insensitive macOS volumes. `Path.match` cannot express this: it
    is right-anchored and treats `**` as one segment (#1746)."""
    if _ENV_FILE_NAME.fullmatch(path.name.casefold()):
        return True
    return any(part.casefold() in _CREDENTIAL_DIR_NAMES for part in path.parts)
