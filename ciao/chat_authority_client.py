"""Engine-side client for the macOS chat authority helper (#3623, slice 3).

Read-only and not wired into ingress: nothing in the engine calls this module,
and `chat_evidence_status` still answers `remote_client` or `unprovisioned`.
The design is `docs/CHAT_AUTHORITY_HELPER.md`. Operator key enrollment and
replacement are not engine operations at all (replacement is disallowed; the
only change is an admin-authorized reset).

The engine runs as the same OS user as the agent, so this client is not
trusted with authority. It has no command, signing or submit path: grants and
turns are signed by the native Mac app and checked by the helper, never by the
engine. The client only asks the helper for its health and for eligible events,
and it fails closed: any silence, malformed or inconsistent reply is
`degraded` or an error, never an empty-but-fine answer. It cannot verify
signatures (the engine holds neither key); trust in a reply comes from the
transport reaching the root-registered helper service, so the checks here are
consistency checks, not authentication.

A `Transport` carries one request and one reply. The real one will be an XPC
connection to a Mach service in the system domain with a timeout (a peer the
helper rejects gets silence, not an error).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ciao.chat_authority import MAX_LOOKUP, Lookup, TurnEvent

PROTOCOL_VERSION = 1
CALL_TIMEOUT_SECONDS = 2.0
MAX_REPLY_BYTES = 8 * 1024 * 1024


class TransportUnprovisioned(Exception):
    """No helper service is registered on this machine."""


class TransportTimeout(Exception):
    """The helper did not answer in time (also what a rejected peer sees)."""


class TransportError(Exception):
    """Any other transport failure."""


class Transport(Protocol):
    def call(self, request: bytes, *, timeout: float) -> bytes: ...


class ChatAuthorityUnavailable(Exception):
    """The helper could not give a trustworthy answer; `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChatAuthorityClient:
    def __init__(
        self,
        transport: Transport,
        *,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._transport = transport
        self._nonce = nonce_factory or (lambda: secrets.token_hex(16))
        self._max_state_counter = -1
        self._authority_key_id: str | None = None
        self._regressed = False

    # ---- wire --------------------------------------------------------

    def _call(self, op: str, **fields: Any) -> dict[str, Any]:
        nonce = self._nonce()
        request = json.dumps({"v": PROTOCOL_VERSION, "op": op, "nonce": nonce, **fields}).encode()
        try:
            raw = self._transport.call(request, timeout=CALL_TIMEOUT_SECONDS)
        except TransportUnprovisioned:
            raise
        except TransportTimeout as exc:
            raise ChatAuthorityUnavailable("timeout") from exc
        except (TransportError, OSError) as exc:
            raise ChatAuthorityUnavailable("transport") from exc
        if len(raw) > MAX_REPLY_BYTES:
            raise ChatAuthorityUnavailable("reply_too_large")
        try:
            reply = json.loads(raw)
        except ValueError as exc:
            raise ChatAuthorityUnavailable("bad_reply") from exc
        if (
            not isinstance(reply, dict)
            or reply.get("v") != PROTOCOL_VERSION
            or reply.get("nonce") != nonce
        ):
            raise ChatAuthorityUnavailable("bad_reply")
        if reply.get("ok") is not True:
            code = reply.get("code")
            raise ChatAuthorityUnavailable(code if isinstance(code, str) and code else "refused")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise ChatAuthorityUnavailable("bad_reply")
        return result

    # ---- health ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Verified health, or `ChatAuthorityUnavailable` (`TransportUnprovisioned` if absent).

        The helper's root-owned monotonic state counter must never decrease
        and its authority key id must not change while this client lives; a
        regression is remembered (a later higher value does not clear it).
        """
        result = self._call("health")
        counter = result.get("state_counter")
        key_id = result.get("authority_key_id")
        epoch = result.get("epoch")
        if (
            result.get("ready") is not True
            or type(counter) is not int
            or type(epoch) is not int
            or not isinstance(key_id, str)
            or not key_id
        ):
            raise ChatAuthorityUnavailable("not_ready")
        if counter < self._max_state_counter:
            self._regressed = True
        self._max_state_counter = max(self._max_state_counter, counter)
        if self._authority_key_id is not None and key_id != self._authority_key_id:
            self._regressed = True
        self._authority_key_id = self._authority_key_id or key_id
        if self._regressed:
            raise ChatAuthorityUnavailable("rollback_suspected")
        return result

    # ---- lookup ------------------------------------------------------

    def eligible_events(
        self,
        *,
        grant_id: str,
        producer: str,
        limit: int,
        destination: str,
        audience: str,
        chat: str | None = None,
        expected_epoch: int | None = None,
    ) -> Lookup:
        """Newest-first eligible events; every filter is applied by the helper.

        The destination and audience are sent to the helper, which returns
        nothing unless the grant carries exactly those; the reply must echo them
        with the grant id. The caller still re-checks the epoch at the point of
        use; this client checks that the reply is consistent with what was asked.
        """
        if not 1 <= limit <= MAX_LOOKUP:
            raise ValueError("bad_limit")
        try:
            self.health()
            result = self._call(
                "eligible_events",
                grant_id=grant_id,
                producer=producer,
                limit=limit,
                destination=destination,
                audience=audience,
                chat=chat,
                expected_epoch=expected_epoch,
            )
        except TransportUnprovisioned as exc:
            raise ChatAuthorityUnavailable("unprovisioned") from exc
        epoch = result.get("epoch")
        raw_events = result.get("events")
        if (
            type(epoch) is not int
            or not isinstance(raw_events, list)
            or result.get("grant_id") != grant_id
            or result.get("destination") != destination
            or result.get("audience") != audience
        ):
            raise ChatAuthorityUnavailable("bad_reply")
        if (expected_epoch is not None and epoch != expected_epoch) or len(raw_events) > limit:
            raise ChatAuthorityUnavailable("bad_reply")
        events = tuple(_event(raw) for raw in raw_events)
        seen: set[str] = set()
        for event in events:
            if (
                event.event_id in seen
                or grant_id not in event.grant_ids
                or (chat is not None and event.chat != chat)
                or event.epoch > epoch
            ):
                raise ChatAuthorityUnavailable("bad_reply")
            seen.add(event.event_id)
        return Lookup(epoch, grant_id, destination, audience, events)


def _event(raw: object) -> TurnEvent:
    if not isinstance(raw, dict):
        raise ChatAuthorityUnavailable("bad_reply")
    try:
        text = base64.b64decode(raw["text_b64"], validate=True)
        signature = base64.b64decode(raw["signature_b64"], validate=True)
        grant_ids = raw["grant_ids"]
        ints = [raw["revision"], raw["seq"], raw["epoch"]]
        strs = [
            raw[k]
            for k in (
                "event_id", "turn_id", "send_id", "workspace", "project",
                "chat", "principal", "text_sha256",
            )
        ]
        if (
            not isinstance(grant_ids, list)
            or not all(isinstance(g, str) for g in grant_ids)
            or any(type(i) is not int for i in ints)
            or not all(isinstance(s, str) for s in strs)
            or type(raw["opted_out"]) is not bool
            or hashlib.sha256(text).hexdigest() != raw["text_sha256"]
        ):
            raise ChatAuthorityUnavailable("bad_reply")
    except (KeyError, binascii.Error, TypeError, ValueError) as exc:
        raise ChatAuthorityUnavailable("bad_reply") from exc
    return TurnEvent(
        event_id=raw["event_id"],
        turn_id=raw["turn_id"],
        revision=raw["revision"],
        seq=raw["seq"],
        send_id=raw["send_id"],
        workspace=raw["workspace"],
        project=raw["project"],
        chat=raw["chat"],
        principal=raw["principal"],
        text=text,
        text_sha256=raw["text_sha256"],
        opted_out=raw["opted_out"],
        grant_ids=tuple(grant_ids),
        epoch=raw["epoch"],
        signature=signature,
    )


def helper_status(client: ChatAuthorityClient, *, provisioned: Callable[[], bool]) -> str:
    """Map the helper's state onto the existing chat-evidence status names.

    `unprovisioned` when no service is registered, `degraded` when one is
    registered but cannot be verified, and `available` only after a verified
    health reply **and** a passing static provisioning check. `provisioned` has
    no default so that health alone can never read `available`; pass
    `static_provisioning_ok`. Use one long-lived client: its rollback memory
    lives in the object. The engine's own platform and channel statuses
    (`unsupported_platform`, `remote_client`) are decided before this is asked.
    """
    try:
        client.health()
    except TransportUnprovisioned:
        return "unprovisioned"
    except ChatAuthorityUnavailable:
        return "degraded"
    return "available" if provisioned() else "degraded"


HELPER_FILES = (
    "Library/PrivilegedHelperTools/com.ciaobot.chat-authority",
    "Library/PrivilegedHelperTools/com.ciaobot.chat-authority-anchor",
    "Library/LaunchDaemons/com.ciaobot.chat-authority.plist",
    "Library/LaunchDaemons/com.ciaobot.chat-authority-anchor.plist",
)
HELPER_ACCOUNT = "_ciaoauth"


def _account_ok() -> bool:
    import grp
    import pwd

    try:
        pwd.getpwnam(HELPER_ACCOUNT)
        admin = grp.getgrnam("admin")
    except KeyError:
        return False
    return HELPER_ACCOUNT not in admin.gr_mem


def static_provisioning_ok(
    root: Path = Path("/"),
    *,
    owner_uid: int = 0,
    account_ok: Callable[[], bool] = _account_ok,
) -> bool:
    """True when the helper's files and account look provisioned.

    Every helper file and each directory above it (up to `root`) must exist, be
    owned by `owner_uid` (root), not be a symlink and not be group or world
    writable; the dedicated account must exist outside the `admin` group. This
    is the engine's own stat of what the installer should have left, not the
    helper's word for it. It does not prove the binaries are ours.
    """
    seen: set[Path] = set()
    for rel in HELPER_FILES:
        path = root / rel
        for item in (path, *path.parents):
            if item in seen:
                break
            seen.add(item)
            try:
                info = item.lstat()
            except OSError:
                return False
            if stat.S_ISLNK(info.st_mode) or info.st_uid != owner_uid or info.st_mode & 0o022:
                return False
            if item == root:
                break
    return account_ok()
