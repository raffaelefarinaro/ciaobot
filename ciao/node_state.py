"""Node state for Ciaobot host/client multi-device mode.

Host: full local instance (schedules, vault writes).
Client: thin tunnel — PWA/tray proxy to a remote host; local automations pause.

Legacy role names ``active``/``standby`` are migrated to ``host``/``client``.

Naming note: "handover" in this module (``last_handover``, ``/api/node/handover``,
the force-handover action) means the DEVICE ROLE switch between host and client.
It is unrelated to a chat's provider-session handover
(``handover_context_pending`` in ``ciao/web/project_chats.py``); only the word is
shared. See the comment on that field for the other side of the disambiguation.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Roles were renamed from active/standby to host/client ("active" also collided
# with peer entries' `is_active`). Values persisted by older releases are
# normalized through this map on every read and rewritten in their canonical
# form on the next save, but the aliases stay accepted forever: a state file may
# not have been rewritten yet, and an unknown role would silently flip a client
# back to host (normalize_role's fallback), resuming schedules on both machines.
_ROLE_ALIASES = {
    "active": "host",
    "standby": "client",
    "host": "host",
    "client": "client",
}
_VALID_ROLES = frozenset({"host", "client"})


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_default_node_id() -> str:
    return os.environ.get("CIAO_NODE_ID", "").strip() or socket.gethostname() or "ciaobot-node"


def normalize_role(role: str) -> str:
    cleaned = str(role or "").strip().lower()
    mapped = _ROLE_ALIASES.get(cleaned, "")
    return mapped if mapped in _VALID_ROLES else "host"


def get_default_role() -> str:
    return normalize_role(os.environ.get("CIAO_DEFAULT_NODE_ROLE", "host"))


def _normalize_peer_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        cleaned = f"http://{cleaned}"

    try:
        from urllib.parse import urlparse
        parsed = urlparse(cleaned)
        if not parsed.port and parsed.scheme == "http":
            cleaned = f"{cleaned}:8443"
    except Exception:
        pass
    return cleaned


class NodeStateManager:
    """Persists host/client role and host connection in ``.runtime/node_state.json``."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = Path(runtime_root)
        self.state_file = self.runtime_root / "node_state.json"
        self.node_id = get_default_node_id()
        self._ensure_loaded()

    def _ensure_loaded(self) -> dict[str, Any]:
        data = self._read_raw()
        if not data:
            now = _now_iso()
            default_role = get_default_role()
            data = {
                "node_id": self.node_id,
                "role": default_role,
                "active_since": now if default_role == "host" else None,
                "last_handover": now,
                "host_url": None,
                "host_session": None,
                "peers": [],
            }
            self._write_raw(data)
            return data

        changed = False
        if self.node_id and data.get("node_id") != self.node_id:
            data["node_id"] = self.node_id
            changed = True
        role = normalize_role(str(data.get("role", "host")))
        if data.get("role") != role:
            data["role"] = role
            changed = True
        if "host_url" not in data:
            data["host_url"] = None
            changed = True
        if "host_session" not in data:
            data["host_session"] = None
            changed = True
        # Prefer explicit host_url; else migrate from first/active peer.
        if not data.get("host_url"):
            peers = data.get("peers") or []
            if isinstance(peers, list) and peers:
                active = next(
                    (p for p in peers if isinstance(p, dict) and p.get("is_active")),
                    None,
                )
                pick = active if isinstance(active, dict) else peers[0]
                if isinstance(pick, dict) and pick.get("url"):
                    data["host_url"] = _normalize_peer_url(str(pick["url"]))
                    changed = True
        if changed:
            self._write_raw(data)
        return data

    def _read_raw(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {}
        try:
            val = json.loads(self.state_file.read_text(encoding="utf-8"))
            return val if isinstance(val, dict) else {}
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", self.state_file, exc)
            return {}

    def _write_raw(self, payload: dict[str, Any]) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.state_file)

    def is_active(self) -> bool:
        """True when this node is the host (runs schedules and vault writes)."""
        return self.get_role() == "host"

    def is_client(self) -> bool:
        return self.get_role() == "client"

    def get_role(self) -> str:
        data = self._read_raw()
        return normalize_role(str(data.get("role", get_default_role())))

    def get_host_url(self) -> str | None:
        data = self._read_raw()
        raw = str(data.get("host_url") or "").strip()
        if raw:
            return _normalize_peer_url(raw) or None
        peers = data.get("peers", [])
        if not isinstance(peers, list) or not peers:
            return None
        active_peer = next(
            (p for p in peers if isinstance(p, dict) and p.get("is_active")),
            None,
        )
        pick = active_peer if isinstance(active_peer, dict) else peers[0]
        if not isinstance(pick, dict):
            return None
        return _normalize_peer_url(str(pick.get("url") or "")) or None

    def get_active_peer_url(self) -> str | None:
        """Host URL when in client mode (used by the proxy middleware)."""
        if self.get_role() != "client":
            return None
        return self.get_host_url()

    def get_host_session(self) -> str | None:
        data = self._read_raw()
        session = str(data.get("host_session") or "").strip()
        return session or None

    def get_status(self) -> dict[str, Any]:
        data = self._ensure_loaded()
        role = normalize_role(str(data.get("role", "host")))
        host_url = self.get_host_url()
        return {
            "node_id": data.get("node_id", self.node_id),
            "role": role,
            "mode": role,
            "active_since": data.get("active_since"),
            "last_handover": data.get("last_handover"),
            "host_url": host_url,
            "active_peer_url": host_url if role == "client" else None,
            "has_host_session": bool(str(data.get("host_session") or "").strip()),
            "peers": data.get("peers", []),
        }

    def set_role(self, role: str) -> dict[str, Any]:
        cleaned = normalize_role(role)
        if cleaned not in _VALID_ROLES:
            raise ValueError(f"Invalid node role '{role}', must be 'host' or 'client'")

        data = self._ensure_loaded()
        old_role = data.get("role")
        now = _now_iso()

        data["role"] = cleaned
        data["last_handover"] = now
        if cleaned == "host":
            data["active_since"] = now
        else:
            data["active_since"] = None

        self._write_raw(data)
        logger.info("Node %s role transitioned from %s to %s", self.node_id, old_role, cleaned)
        return self.get_status()

    def demote(self) -> dict[str, Any]:
        """Leave host mode (client without a tunnel until connect)."""
        return self.set_role("client")

    def promote(self) -> dict[str, Any]:
        """Become host and clear the client tunnel session."""
        data = self._ensure_loaded()
        data["host_session"] = None
        data["role"] = "host"
        data["active_since"] = _now_iso()
        data["last_handover"] = data["active_since"]
        self._write_raw(data)
        logger.info("Node %s became host", self.node_id)
        return self.get_status()

    def connect_as_client(
        self, host_url: str, *, host_session: str | None = None
    ) -> dict[str, Any]:
        """Enter client mode tunneling to ``host_url``."""
        url = _normalize_peer_url(host_url)
        if not url:
            raise ValueError("host_url is required")

        data = self._ensure_loaded()
        now = _now_iso()
        data["role"] = "client"
        data["host_url"] = url
        data["host_session"] = (host_session or "").strip() or None
        data["active_since"] = None
        data["last_handover"] = now

        peers: list[dict[str, Any]] = [
            p
            for p in (data.get("peers") or [])
            if isinstance(p, dict)
            and _normalize_peer_url(str(p.get("url") or "")) != url
        ]
        peers.insert(
            0,
            {
                "node_id": url,
                "url": url,
                "last_seen": now,
                "is_active": True,
            },
        )
        data["peers"] = peers
        self._write_raw(data)
        logger.info("Node %s connected as client to %s", self.node_id, url)
        return self.get_status()

    def set_host_session(self, session: str | None) -> dict[str, Any]:
        data = self._ensure_loaded()
        data["host_session"] = (session or "").strip() or None
        self._write_raw(data)
        return self.get_status()

    def add_peer(self, url: str, peer_id: str = "") -> dict[str, Any]:
        url_cleaned = _normalize_peer_url(url)
        if not url_cleaned:
            return self.get_status()

        data = self._ensure_loaded()
        peers: list[dict[str, Any]] = data.get("peers", [])

        existing = next((p for p in peers if p.get("url") == url_cleaned), None)
        now = _now_iso()
        if existing:
            existing["last_seen"] = now
            if peer_id:
                existing["node_id"] = peer_id
        else:
            peers.append(
                {
                    "node_id": peer_id or url_cleaned,
                    "url": url_cleaned,
                    "last_seen": now,
                    "is_active": False,
                }
            )
        data["peers"] = peers
        if not data.get("host_url"):
            data["host_url"] = url_cleaned
        self._write_raw(data)
        return self.get_status()

    def remove_peer(self, url: str) -> dict[str, Any]:
        url_cleaned = _normalize_peer_url(url) or url.strip().rstrip("/")
        data = self._ensure_loaded()
        peers = [
            p
            for p in data.get("peers", [])
            if _normalize_peer_url(str(p.get("url") or "")) != url_cleaned
        ]
        data["peers"] = peers
        if _normalize_peer_url(str(data.get("host_url") or "")) == url_cleaned:
            data["host_url"] = None
            data["host_session"] = None
        self._write_raw(data)
        return self.get_status()
