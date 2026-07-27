"""Node state management for Ciaobot multi-device handover.

Controls active vs standby role state for multi-device deployments.
When a node is in 'standby' mode, background schedules, cron routines, auto loops,
and automated git backup pushes are paused.
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


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_default_node_id() -> str:
    return os.environ.get("CIAO_NODE_ID", "").strip() or socket.gethostname() or "ciaobot-node"


def get_default_role() -> str:
    role = os.environ.get("CIAO_DEFAULT_NODE_ROLE", "active").strip().lower()
    return role if role in {"active", "standby"} else "active"


class NodeStateManager:
    """Manages local node active/standby state and peer registrations in .runtime/node_state.json."""

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
                "active_since": now if default_role == "active" else None,
                "last_handover": now,
                "peers": [],
            }
            self._write_raw(data)
        else:
            # Sync node_id if environment overrode it
            if self.node_id and data.get("node_id") != self.node_id:
                data["node_id"] = self.node_id
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
        data = self._read_raw()
        return str(data.get("role", "active")) == "active"

    def get_role(self) -> str:
        data = self._read_raw()
        return str(data.get("role", get_default_role()))

    def get_active_peer_url(self) -> str | None:
        data = self._read_raw()
        peers = data.get("peers", [])
        if not peers:
            return None
        # Return peer marked as active if available, otherwise first peer
        active_peer = next((p for p in peers if p.get("is_active")), peers[0])
        return str(active_peer.get("url", "")).strip().rstrip("/") or None

    def get_status(self) -> dict[str, Any]:
        data = self._ensure_loaded()
        active_peer = self.get_active_peer_url()
        return {
            "node_id": data.get("node_id", self.node_id),
            "role": data.get("role", "active"),
            "active_since": data.get("active_since"),
            "last_handover": data.get("last_handover"),
            "active_peer_url": active_peer,
            "peers": data.get("peers", []),
        }

    def set_role(self, role: str) -> dict[str, Any]:
        cleaned = role.strip().lower()
        if cleaned not in {"active", "standby"}:
            raise ValueError(f"Invalid node role '{role}', must be 'active' or 'standby'")

        data = self._ensure_loaded()
        old_role = data.get("role")
        now = _now_iso()

        data["role"] = cleaned
        data["last_handover"] = now
        if cleaned == "active":
            data["active_since"] = now
        else:
            data["active_since"] = None

        self._write_raw(data)
        logger.info("Node %s role transitioned from %s to %s", self.node_id, old_role, cleaned)
        return self.get_status()

    def demote(self) -> dict[str, Any]:
        """Set role to standby."""
        return self.set_role("standby")

    def promote(self) -> dict[str, Any]:
        """Set role to active."""
        return self.set_role("active")

    def add_peer(self, url: str, peer_id: str = "") -> dict[str, Any]:
        url_cleaned = url.strip().rstrip("/")
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
            peers.append({
                "node_id": peer_id or url_cleaned,
                "url": url_cleaned,
                "last_seen": now,
                "is_active": False,
            })
        data["peers"] = peers
        self._write_raw(data)
        return self.get_status()

    def remove_peer(self, url: str) -> dict[str, Any]:
        url_cleaned = url.strip().rstrip("/")
        data = self._ensure_loaded()
        peers = [p for p in data.get("peers", []) if p.get("url") != url_cleaned]
        data["peers"] = peers
        self._write_raw(data)
        return self.get_status()
