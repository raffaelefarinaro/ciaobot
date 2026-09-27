"""Classify a runtime root's legacy ``node_state.json`` so a startup can fail
closed (#562 Phase 4, #636).

The decision this module owns is the one node mode used to own inside
`NodeStateManager`: *may this boot run the writers* (schedules, backup push)?
`NodeStateManager` answers it by reading the state through a manager that
*creates* a host state when the file is absent — the same trap
`engine_migration.py` documents — and #577 deletes that manager outright. So
the answer has to survive the deletion, which is what this leaf module is: it
imports nothing from `ciao` (the same shape as `vault_links.py`), reads the
file raw, never writes, and never raises.

Four kinds, and the two that stop the writers are the whole point:

- `none` — no `node_state.json`: a fresh install, or one whose node mode is
  already gone. There is nothing to guard, so the writers arm.
- `host` — role `host`/`active`. This Mac owns its state, so it arms.
- `client` — role `client`/`standby` with a usable `http(s)` `host_url`. This
  Mac was a client of another host, and a second writer here would double-write
  against that host.
- `invalid` — a file that exists but cannot be read, is not an object, carries
  an unknown role, or is a client with no addressable host. An unaccountable
  state is never guessed at: guessing `host` puts a second writer on a runtime
  root that may already have one, and guessing `client` strands a user with no
  writer anywhere. Both guesses are expensive, so `invalid` fails closed too.

`invalid` is deliberately the same answer for "unreadable" and "understood but
we refuse it", because the two have the same consequence: a human has to
choose. It is not the same as `none`, which is a *known* state: no file means
nobody ever claimed this runtime root.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# The node roles Ciaobot has ever persisted, mapped onto the two kinds a
# startup can act on. Values written by releases that predate the host/client
# rename are normalized through this table on every read; anything outside it is
# unknown, and an unknown role is never guessed at.
_ROLE_ALIASES = {
    "active": "host",
    "host": "host",
    "standby": "client",
    "client": "client",
}

# The kinds that may not run the writers. Fail closed, both of them.
BLOCKED_KINDS = frozenset({"client", "invalid"})

STATE_FILENAME = "node_state.json"


@dataclass(frozen=True)
class LegacyNodeState:
    """What a runtime root's ``node_state.json`` says, read raw.

    `kind` is the only field a decision turns on. `role` carries the normalized
    role when the file had a usable one (`"host"`/`"client"`) and `"invalid"`
    when it did not, so a caller can name what it found without re-deriving it.
    `host_url` is the validated address of a client and is empty for every other
    kind.
    """

    kind: str  # "none" | "host" | "client" | "invalid"
    role: str = ""  # "host" | "client" | "" | "invalid"
    host_url: str = ""


def _client_host_url(value: Any) -> str:
    """`value` as a host URL a client was really connected to, else "".

    The same rule `engine_migration._client_host_url` applies, spelled out here
    because this module has to keep standing when nothing else does: the scheme
    has to be http(s), there has to be a hostname that is not whitespace or a
    path, and credentials in the URL are refused rather than printed back at the
    user. A prefix check would accept `https://` and `https://:8443`, neither
    of which is an address the user could open.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate:
        return ""
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
        parts.port  # raises ValueError on a port that is not a number
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host:
        return ""
    if any(char.isspace() for char in host) or any(
        char in host for char in "/?#@\\"
    ):
        return ""
    if parts.username or parts.password:
        return ""
    return candidate


def detect(runtime_root: Path) -> LegacyNodeState:
    """Classify `runtime_root`'s legacy `node_state.json`, read raw.

    Never writes and never raises: `NodeStateManager` is not consulted (it
    creates a host state when the file is absent, so "there was no state" would
    be answered by a write), and a file that cannot be understood comes back as
    `invalid` rather than as an exception into the caller's startup path.
    """
    state_file = Path(runtime_root) / STATE_FILENAME
    try:
        if not state_file.exists():
            return LegacyNodeState(kind="none")
        raw = state_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` is a `ValueError`, not an `OSError`, so an
        # `OSError`-only guard lets it out — and this function is called from
        # the startup path with nothing between it and the boot. A half-written
        # or foreign-encoded state is unreadable in exactly the sense the kinds
        # above mean, and a truncated file is the case a fail-closed gate most
        # has to survive: an aborted write, not a host that chose this.
        return LegacyNodeState(kind="invalid", role="invalid")

    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        return LegacyNodeState(kind="invalid", role="invalid")
    if not isinstance(state, dict):
        return LegacyNodeState(kind="invalid", role="invalid")

    role = str(state.get("role", "") or "").strip().lower()
    mapped = _ROLE_ALIASES.get(role, "invalid")
    if mapped == "invalid":
        return LegacyNodeState(kind="invalid", role="invalid")

    host_url = _client_host_url(state.get("host_url"))
    if mapped == "client" and not host_url:
        # A client whose host cannot be named is not a client anybody can be
        # handed over to, so it fails closed with everything else rather than
        # being reported as a client that has somewhere to go.
        return LegacyNodeState(kind="invalid", role="invalid")
    return LegacyNodeState(kind=mapped, role=mapped, host_url=host_url)


def writers_armed(legacy: LegacyNodeState) -> bool:
    """Whether a boot may run the writers for a classification of `legacy`.

    `host` and `none` arm; `client` and `invalid` do not. Lives here, next to
    the kinds it reads, so the startup gate and the detector cannot disagree
    about which kinds are safe to write.
    """
    return legacy.kind not in BLOCKED_KINDS
