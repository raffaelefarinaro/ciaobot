"""Web Push (VAPID) support for the PWA.

Stores VAPID keys at ``.runtime/vapid.json`` and subscriptions at
``.runtime/push_subscriptions.json``. Sends notifications via pywebpush.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

NOTIFICATION_LOG_MAX = 100


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class PushManager:
    """Persist VAPID keys + subscriptions, send Web Push notifications."""

    def __init__(self, runtime_root: Path, subject: str = "mailto:admin@ciao.local") -> None:
        self._runtime = runtime_root
        self._runtime.mkdir(parents=True, exist_ok=True)
        self._vapid_path = runtime_root / "vapid.json"
        self._subs_path = runtime_root / "push_subscriptions.json"
        self._log_path = runtime_root / "notifications.jsonl"
        self._subject = subject
        self._lock = Lock()
        self._subs: list[dict[str, Any]] = []
        self._private_pem: str = ""
        self._private_raw_b64: str = ""
        self._public_b64: str = ""
        self._load_or_create_keys()
        self._load_subs()

    # ── VAPID keys ──────────────────────────────────────────────────────

    def _load_or_create_keys(self) -> None:
        if self._vapid_path.exists():
            try:
                data = json.loads(self._vapid_path.read_text())
                self._private_pem = data["private_pem"]
                self._public_b64 = data["public_b64"]
                # Older files may not have the raw key; derive + persist it.
                self._private_raw_b64 = data.get("private_raw_b64", "") or self._derive_raw_from_pem(self._private_pem)
                if not data.get("private_raw_b64"):
                    self._save_keys()
                return
            except Exception:
                logger.exception("Failed to load VAPID keys, regenerating")

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        public_numbers = private_key.public_key().public_numbers()
        x = public_numbers.x.to_bytes(32, "big")
        y = public_numbers.y.to_bytes(32, "big")
        uncompressed = b"\x04" + x + y
        private_value = private_key.private_numbers().private_value
        self._private_pem = pem
        self._private_raw_b64 = _b64url(private_value.to_bytes(32, "big"))
        self._public_b64 = _b64url(uncompressed)
        self._save_keys()

    def _save_keys(self) -> None:
        self._vapid_path.write_text(json.dumps({
            "private_pem": self._private_pem,
            "private_raw_b64": self._private_raw_b64,
            "public_b64": self._public_b64,
        }))

    @staticmethod
    def _derive_raw_from_pem(pem: str) -> str:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        value = key.private_numbers().private_value  # type: ignore[attr-defined]
        return _b64url(value.to_bytes(32, "big"))

    @property
    def public_key(self) -> str:
        return self._public_b64

    # ── Subscriptions ───────────────────────────────────────────────────

    def _load_subs(self) -> None:
        if not self._subs_path.exists():
            return
        try:
            self._subs = json.loads(self._subs_path.read_text()).get("subscriptions", [])
        except Exception:
            logger.exception("Failed to load subscriptions")
            self._subs = []

    def _save_subs(self) -> None:
        self._subs_path.write_text(json.dumps({"subscriptions": self._subs}, indent=2))

    def add(self, subscription: dict[str, Any]) -> None:
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("subscription missing endpoint")
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            self._subs.append(subscription)
            self._save_subs()

    def remove(self, endpoint: str) -> None:
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            if len(self._subs) != before:
                self._save_subs()

    def count(self) -> int:
        return len(self._subs)

    def has(self, endpoint: str) -> bool:
        return any(s.get("endpoint") == endpoint for s in self._subs)

    # ── Notification log ────────────────────────────────────────────────

    def _log_notification(self, payload: dict[str, Any]) -> None:
        """Append to .runtime/notifications.jsonl so local companions (the
        macOS menu bar app) can show notifications even when no Web Push
        subscription exists."""
        try:
            with self._lock:
                entry = {"ts": time.time(), **payload}
                with self._log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry) + "\n")
                lines = self._log_path.read_text(encoding="utf-8").splitlines()
                if len(lines) > NOTIFICATION_LOG_MAX * 2:
                    self._log_path.write_text(
                        "\n".join(lines[-NOTIFICATION_LOG_MAX:]) + "\n",
                        encoding="utf-8",
                    )
        except Exception:
            logger.exception("Failed to log notification")

    # ── Send ────────────────────────────────────────────────────────────

    def send(self, payload: dict[str, Any]) -> None:
        """Fire-and-forget send to all known subscriptions. Prunes dead endpoints."""
        self._log_notification(payload)
        if not self._subs:
            return
        try:
            from pywebpush import WebPushException, webpush
        except Exception:
            logger.warning("pywebpush not installed, skipping push")
            return

        body = json.dumps(payload)
        claims = {"sub": self._subject}
        dead: list[str] = []
        for sub in list(self._subs):
            try:
                webpush(
                    subscription_info=sub,
                    data=body,
                    vapid_private_key=self._private_raw_b64,
                    vapid_claims=dict(claims),
                    ttl=60,
                    timeout=10,
                )
            except WebPushException as exc:  # type: ignore[misc]
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (404, 410):
                    dead.append(sub.get("endpoint", ""))
                else:
                    logger.warning("Push failed (%s): %s", status, exc)
            except Exception:
                logger.exception("Push send error")
        if dead:
            with self._lock:
                self._subs = [s for s in self._subs if s.get("endpoint") not in dead]
                self._save_subs()
