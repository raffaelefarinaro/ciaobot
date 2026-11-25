"""Private, typed decision evidence and the operator's permission to disclose it.

This module owns the one trusted record a shared-brain contribution may be built
from. A structured decision is recorded by the engine on an authenticated
operator save into an engine-owned stream, never by a model, a general file
write, a chat tool, an importer or a linked note. A standing grant authorizes
later decisions from that one stream, field by field, for named producers; no
individual approval is needed for a matching later entry. An individual snapshot
freezes the exact previewed fields of one externally editable project decision
and is unaffected by a later edit or deletion of that document.

Everything here is owner-only runtime state outside the vault and shared
folder. Evidence identifiers are local handles, never bearer credentials: a
grant is effective only while its workspace lifetime, brain link lifetime,
project, source and consent generation still agree. A producer relation is an
HMAC-sealed adapter result over a real archived memory-pass chat and its project,
or a live curation lease's planned project work item; a caller-supplied producer,
project, source or relation string is never authority.

One workspace state file is the authoritative transaction point. Thread and
``flock`` locks serialize it across processes, and a grant plus the preview
token it consumes are written in a single atomic replacement, so a crash cannot
leave a reusable token or a second grant. Evidence bytes are content-addressed
and keyed by workspace lifetime, project and source, so identical decisions in
different projects never collide.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, NoReturn, cast

from ciao.config import SharedBrainLink
from ciao.os_support.files import open_fd
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import (
    is_private_descriptor_strict,
    make_private_dir,
    mkstemp_private,
    open_private,
)
from ciao.redact import redact_text

SCHEMA = 1
FIELDS = ("decision", "reason", "effective_date")
PRODUCERS = ("memory_extraction", "nightly_curation")
GRANT_TYPES = ("standing", "snapshot")
MODE_REVIEW = "review"
MODE_AUTOMATIC = "automatic"

MAX_DECISION_CHARS = 2_000
MAX_REASON_CHARS = 2_000
MAX_PROJECT_ID_CHARS = 128
MAX_WORKSPACE_CHARS = 128
MAX_ENTRIES_PER_STREAM = 500
MAX_GRANTS_PER_WORKSPACE = 100
MAX_DOCUMENT_ENTRIES = 50
MAX_SOURCE_BYTES = 65_536
MAX_STATE_BYTES = 12 * 1_048_576
MAX_EVIDENCE_BYTES = 131_072
MAX_PREVIEWS = 20
PREVIEW_TTL = timedelta(minutes=15)
RELATION_TTL = timedelta(hours=1)
DOCUMENT_NAME = "Decisions.md"
DOCUMENT_SOURCE = f"document:{DOCUMENT_NAME}"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GRANT_ID_RE = re.compile(r"^grn_[0-9a-f]{24}$")
_ENTRY_ID_RE = re.compile(r"^dec_[0-9a-f]{24}$")
_LIFETIME_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class EvidenceError(ValueError):
    """A refusal with a stable code; no evidence bytes are included."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _refuse(code: str, message: str) -> NoReturn:
    raise EvidenceError(code, message)


_ENGINE_PROJECT_MANAGER: Any = None


def bind_project_manager(manager: Any) -> None:
    """Bind the one engine-owned project manager producer adapters may read.

    Called once from engine startup. Producer code never passes a manager, so a
    caller cannot substitute a fake object that invents project ownership.
    """
    from ciao.web.project_chats import ProjectChatManager  # noqa: PLC0415

    if not isinstance(manager, ProjectChatManager):
        raise TypeError("the bound project manager must be the engine's own")
    global _ENGINE_PROJECT_MANAGER
    if _ENGINE_PROJECT_MANAGER is not None and _ENGINE_PROJECT_MANAGER is not manager:
        raise RuntimeError("the engine project manager is already bound")
    _ENGINE_PROJECT_MANAGER = manager


def _reset_project_manager_for_tests() -> None:
    """Test-only seam; production binds exactly once at engine startup."""
    global _ENGINE_PROJECT_MANAGER
    _ENGINE_PROJECT_MANAGER = None


def _project_manager() -> Any:
    if _ENGINE_PROJECT_MANAGER is None:
        _refuse(
            "engine_manager_unavailable",
            "No engine project manager is bound; producer source lookup is unavailable.",
        )
    return _ENGINE_PROJECT_MANAGER


@dataclass(frozen=True, slots=True)
class SourceRelation:
    """An engine-owned proof that a producer's source belongs to one project.

    Only the adapter functions in this module construct one, after reading the
    real memory-pass archive registry or a live curation lease plan. The seal is
    an HMAC over every field, so dataclasses.replace() cannot retarget it to a
    foreign workspace, project, source or producer.
    """

    kind: str
    producer: str
    workspace: str
    workspace_lifetime_id: str
    project_id: str
    source_id: str
    relation_id: str
    evidence: str
    lease_vault: str
    lease_proof_hash: str
    nonce: str
    attested_at: str
    expires_at: str
    seal: bytes = field(repr=False, compare=False)


def _relation_payload(relation: SourceRelation) -> bytes:
    return json.dumps(
        {
            "kind": relation.kind,
            "producer": relation.producer,
            "workspace": relation.workspace,
            "workspace_lifetime_id": relation.workspace_lifetime_id,
            "project_id": relation.project_id,
            "source_id": relation.source_id,
            "relation_id": relation.relation_id,
            "evidence": relation.evidence,
            "lease_vault": relation.lease_vault,
            "lease_proof_hash": relation.lease_proof_hash,
            "nonce": relation.nonce,
            "attested_at": relation.attested_at,
            "expires_at": relation.expires_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _seal_relation(config: Any, relation: SourceRelation) -> SourceRelation:
    seal = hmac.new(
        _relation_secret(config), _relation_payload(relation), hashlib.sha256
    ).digest()
    return SourceRelation(
        kind=relation.kind,
        producer=relation.producer,
        workspace=relation.workspace,
        workspace_lifetime_id=relation.workspace_lifetime_id,
        project_id=relation.project_id,
        source_id=relation.source_id,
        relation_id=relation.relation_id,
        evidence=relation.evidence,
        lease_vault=relation.lease_vault,
        lease_proof_hash=relation.lease_proof_hash,
        nonce=relation.nonce,
        attested_at=relation.attested_at,
        expires_at=relation.expires_at,
        seal=seal,
    )


def _relation_is_sealed(config: Any, relation: object) -> bool:
    if not isinstance(relation, SourceRelation):
        return False
    expected = hmac.new(
        _relation_secret(config), _relation_payload(relation), hashlib.sha256
    ).digest()
    return hmac.compare_digest(expected, relation.seal)


def _session_fingerprint(session: str) -> str:
    return _digest(b"share-evidence-operator-session\0" + session.encode("utf-8"))[:32]


def _operator_event_payload(
    *,
    workspace: str,
    lifetime_id: str,
    project_id: str,
    entry_id: str,
    revision: int,
    decision: str,
    reason: str,
    effective_date: str,
    digest: str,
    session_fingerprint: str,
) -> bytes:
    return json.dumps(
        {
            "workspace": workspace,
            "workspace_lifetime_id": lifetime_id,
            "project_id": project_id,
            "entry_id": entry_id,
            "revision": revision,
            "decision": decision,
            "reason": reason,
            "effective_date": effective_date,
            "digest": digest,
            "session": session_fingerprint,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _operator_event_signature(config: Any, payload: bytes) -> str:
    return hmac.new(
        _relation_secret(config),
        b"share-evidence-operator-event\0" + payload,
        hashlib.sha256,
    ).hexdigest()


def _operator_event_valid(
    config: Any,
    entry: dict[str, Any],
    *,
    workspace: str,
    lifetime_id: str,
    project_id: str,
) -> bool:
    provenance = entry.get("provenance")
    event = provenance.get("operator_event") if isinstance(provenance, dict) else None
    if not isinstance(event, dict):
        return False
    signature = event.get("signature")
    session = event.get("session_fingerprint")
    if (
        not isinstance(signature, str)
        or not isinstance(session, str)
        or not isinstance(entry.get("entry_id"), str)
        or type(entry.get("revision")) is not int
    ):
        return False
    payload = _operator_event_payload(
        workspace=workspace,
        lifetime_id=lifetime_id,
        project_id=project_id,
        entry_id=entry["entry_id"],
        revision=entry["revision"],
        decision=str(entry.get("decision", "")),
        reason=str(entry.get("reason", "")),
        effective_date=str(entry.get("effective_date", "")),
        digest=str(entry.get("digest", "")),
        session_fingerprint=session,
    )
    return hmac.compare_digest(_operator_event_signature(config, payload), signature)


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(fields: dict[str, str]) -> bytes:
    return json.dumps(
        {name: fields[name] for name in FIELDS if name in fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _workspace_key(workspace: str, lifetime_id: str) -> str:
    return hashlib.sha256(
        b"share-evidence-workspace\0"
        + workspace.encode("utf-8")
        + b"\0"
        + lifetime_id.encode("utf-8")
    ).hexdigest()


def _store_root(config: Any) -> Path:
    return Path(config.state_path).parent / "secrets" / "share-evidence"


def _relation_secret(config: Any) -> bytes:
    """A durable owner-only key shared by engine adapter and lookup processes."""
    root = _store_root(config)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(root)
    make_private_dir(root.parent)
    path = root / "relation.key"
    try:
        descriptor = open_private(
            root / ".relation.lock", os.O_RDWR | os.O_CREAT, 0o600, follow_symlinks=False
        )
    except OSError:
        _refuse("private_state_unavailable", "The producer relation key is unavailable.")
    try:
        lock_exclusive(descriptor)
        try:
            raw = _read_private(path, limit=64)
            if len(raw) == 32:
                return raw
            if raw:
                _refuse("private_state_corrupt", "The producer relation key is unreadable.")
            key = secrets.token_bytes(32)
            _write_private(path, key, limit=64)
            return key
        finally:
            unlock(descriptor)
    finally:
        os.close(descriptor)


def _workspace_lifetime(config: Any, workspace: str) -> str:
    """The registered workspace instance's lifetime, made durable before use.

    A registry row that predates the field gets a generated lifetime only after
    the registry CAS accepts it. A config double without a registry cannot prove
    durability and is refused; it must present an already-persisted lifetime.
    """
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    if record is None:
        _refuse("unknown_workspace", "The workspace is not available.")
    value = getattr(record, "lifetime_id", "")
    valid = isinstance(value, str) and _LIFETIME_RE.fullmatch(value) is not None
    persisted = bool(getattr(record, "_lifetime_persisted", False))
    if valid and persisted:
        return value
    new = value if valid else "wsl_" + secrets.token_urlsafe(18)
    previous = value
    previous_persisted = persisted
    try:
        setattr(record, "lifetime_id", new)
        setattr(record, "_lifetime_persisted", False)
    except (AttributeError, TypeError):
        _refuse("private_state_unavailable", "The workspace lifetime cannot be recorded.")
    persist = getattr(config, "persist_workspace_registry", None)
    if not callable(persist):
        try:
            setattr(record, "lifetime_id", previous)
            setattr(record, "_lifetime_persisted", previous_persisted)
        except (AttributeError, TypeError):
            pass
        _refuse(
            "workspace_lifetime_unpersisted",
            "The workspace lifetime is not durable; evidence stays unavailable.",
        )
    from ciao.workspaces import WorkspaceRegistryConflict  # noqa: PLC0415

    try:
        persist()
    except (OSError, ValueError, WorkspaceRegistryConflict):
        try:
            setattr(record, "lifetime_id", previous)
            setattr(record, "_lifetime_persisted", previous_persisted)
        except (AttributeError, TypeError):
            pass
        _refuse(
            "private_state_unavailable",
            "The workspace lifetime could not be saved; evidence stays unavailable.",
        )
    setattr(record, "_lifetime_persisted", True)
    return new
def _workspace_dir(config: Any, workspace: str) -> Path:
    return _store_root(config) / _workspace_key(
        workspace, _workspace_lifetime(config, workspace)
    )


def _lock(root: Path) -> threading.RLock:
    key = str(root.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _require_workspace(config: Any, workspace: str) -> str:
    if (
        not isinstance(workspace, str)
        or not workspace
        or len(workspace) > MAX_WORKSPACE_CHARS
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in workspace)
    ):
        _refuse("unknown_workspace", "The workspace is not available.")
    known = config.workspace_names() if hasattr(config, "workspace_names") else []
    if workspace not in known:
        _refuse("unknown_workspace", "The workspace is not available.")
    return workspace


def _require_project(project_id: str) -> str:
    if not isinstance(project_id, str) or not _PROJECT_RE.fullmatch(project_id):
        _refuse("invalid_project", "Choose a project in this workspace.")
    return project_id


def _clean_text(value: object, name: str, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        _refuse("invalid_field", f"{name} must be text.")
    text = value.strip()
    if required and not text:
        _refuse("invalid_field", f"{name} is required.")
    if len(text) > limit:
        _refuse("too_large", f"{name} is longer than {limit} characters.")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text) or "\x7f" in text:
        _refuse("invalid_field", f"{name} contains a control character.")
    if "\x00" in text:
        _refuse("invalid_field", f"{name} contains a control character.")
    if redact_text(text) != text:
        _refuse("secret_detected", f"{name} contains credential-shaped text.")
    return text


def _clean_effective_date(value: object) -> str:
    if not isinstance(value, str):
        _refuse("invalid_field", "Effective date must be text.")
    text = value.strip()
    if not _DATE_RE.fullmatch(text):
        _refuse("invalid_field", "Effective date must be YYYY-MM-DD.")
    try:
        date.fromisoformat(text)
    except ValueError:
        _refuse("invalid_field", "Effective date must be a real calendar date.")
    return text


def _clean_field_mask(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        _refuse("invalid_field_mask", "Choose at least one field to disclose.")
    chosen = {item for item in value if isinstance(item, str)}
    unknown = chosen - set(FIELDS)
    if unknown or len(chosen) != len(value):
        _refuse("invalid_field_mask", "Choose only the listed decision fields.")
    return [name for name in FIELDS if name in chosen]


def _clean_producers(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        _refuse("invalid_producers", "Choose at least one producer.")
    chosen = {item for item in value if isinstance(item, str)}
    unknown = chosen - set(PRODUCERS)
    if unknown or len(chosen) != len(value):
        _refuse("invalid_producers", "Choose only the listed producers.")
    return [name for name in PRODUCERS if name in chosen]


# ---------------------------------------------------------------------------
# Private durable state and interprocess transaction


def _write_private(path: Path, data: bytes, *, limit: int) -> None:
    if len(data) > limit:
        _refuse("too_large", "The private evidence record exceeds its storage limit.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(path.parent)
    fd, tmp_name = mkstemp_private(dir=path.parent, prefix=".share-evidence-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _read_private(path: Path, *, limit: int) -> bytes:
    try:
        fd = open_fd(path, os.O_RDONLY, follow_symlinks=False)
        try:
            if not is_private_descriptor_strict(fd):
                _refuse("private_state_unsafe", "The private evidence state is not owner-only.")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(limit + 1)
                if len(raw) > limit:
                    _refuse("private_state_corrupt", "The private evidence state exceeds its limit.")
        finally:
            os.close(fd)
    except FileNotFoundError:
        return b""
    except EvidenceError:
        raise
    except OSError:
        _refuse("private_state_unavailable", "The private evidence state is unavailable.")
    return raw


def _read_json(path: Path, *, limit: int, code: str) -> dict[str, Any]:
    raw = _read_private(path, limit=limit)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, json.JSONDecodeError):
        _refuse(code, "The private evidence state is unreadable.")
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        _refuse(code, "The private evidence state cannot be read.")
    return data


def _empty_state(workspace: str, lifetime_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "workspace": workspace,
        "workspace_lifetime_id": lifetime_id,
        "streams": {},
        "grants": {},
        "previews": {},
        "relation_uses": {},
    }


def _state_path(config: Any, workspace: str) -> Path:
    return _workspace_dir(config, workspace) / "state.json"


def _ensure_store(config: Any, workspace: str) -> Path:
    """Create the store and workspace folder owner-only, including ancestors."""
    root = _store_root(config)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(root)
    make_private_dir(root.parent)
    directory = _workspace_dir(config, workspace)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    make_private_dir(directory)
    return directory


@contextmanager
def _workspace_guard(config: Any, workspace: str) -> Iterator[Path]:
    """Thread + ``flock`` guard around one authoritative state transaction."""
    directory = _ensure_store(config, workspace)
    with _lock(directory):
        try:
            descriptor = open_private(
                directory / ".state.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
                follow_symlinks=False,
            )
        except OSError:
            _refuse("private_state_unavailable", "The private evidence lock is unavailable.")
        try:
            lock_exclusive(descriptor)
            try:
                yield directory
            finally:
                unlock(descriptor)
        finally:
            os.close(descriptor)


def _load_state(config: Any, workspace: str) -> dict[str, Any]:
    lifetime = _workspace_lifetime(config, workspace)
    data = _read_json(
        _state_path(config, workspace), limit=MAX_STATE_BYTES, code="private_state_corrupt"
    )
    if not data:
        return _empty_state(workspace, lifetime)
    if (
        data.get("workspace") != workspace
        or data.get("workspace_lifetime_id") != lifetime
        or not isinstance(data.get("streams"), dict)
        or not isinstance(data.get("grants"), dict)
        or not isinstance(data.get("previews"), dict)
        or not isinstance(data.setdefault("relation_uses", {}), dict)
    ):
        _refuse("private_state_corrupt", "The private evidence state belongs to another workspace instance.")
    return data


def _save_state(config: Any, workspace: str, data: dict[str, Any]) -> None:
    lifetime = _workspace_lifetime(config, workspace)
    if (
        data.get("schema") != SCHEMA
        or data.get("workspace") != workspace
        or data.get("workspace_lifetime_id") != lifetime
        or not isinstance(data.get("streams"), dict)
        or not isinstance(data.get("grants"), dict)
        or not isinstance(data.get("previews"), dict)
        or not isinstance(data.get("relation_uses"), dict)
    ):
        _refuse("private_state_corrupt", "The private evidence state is not writable.")
    _write_private(
        _state_path(config, workspace),
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        limit=MAX_STATE_BYTES,
    )


# ---------------------------------------------------------------------------
# Engine-owned structured decisions


def _entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    """The local entry handle and exact fields; provenance stays private."""
    return {
        "entry_id": entry.get("entry_id", ""),
        "revision": entry.get("revision", 0),
        "decision": entry.get("decision", ""),
        "reason": entry.get("reason", ""),
        "effective_date": entry.get("effective_date", ""),
        "recorded_at": entry.get("recorded_at", ""),
        "digest": entry.get("digest", ""),
        "superseded": bool(entry.get("superseded", False)),
    }


def _stream_view(stream: dict[str, Any]) -> dict[str, Any]:
    raw_entries = stream.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    return {
        "stream_id": stream.get("stream_id", ""),
        "project_id": stream.get("project_id", ""),
        "created_at": stream.get("created_at", ""),
        "revision": stream.get("revision", 0),
        "entry_count": len(entries),
    }


def _stream_for(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    streams = data.get("streams")
    if not isinstance(streams, dict):
        return None
    stream = streams.get(project_id)
    return stream if isinstance(stream, dict) else None


def record_decision(
    config: Any,
    *,
    workspace: str,
    project_id: str,
    decision: object,
    reason: object,
    effective_date: object,
    operator_session: str = "",
    disclosure_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one engine-recorded structured decision to the project's stream.

    Settings Save is the sole text ingress. The authenticated operator route
    passes ``operator_session`` and the engine signs a per-event attestation.
    Producers never author stream text; they select these existing masked
    entries through a managed source relation. Unattributed text stays inert.
    """
    _require_workspace(config, workspace)
    project = _require_project(project_id)
    text = _clean_text(decision, "Decision", MAX_DECISION_CHARS, required=True)
    why = _clean_text(reason, "Reason", MAX_REASON_CHARS, required=False)
    day = _clean_effective_date(effective_date)
    moment = _now(now)
    with _workspace_guard(config, workspace):
        data = _load_state(config, workspace)
        if disclosure_token is not None:
            expected = _disclosure_token(config, workspace, project, operator_session, data)
            if not hmac.compare_digest(disclosure_token, expected):
                _refuse("disclosure_changed", "Sharing permissions changed. Review them before saving.")
        stream = _stream_for(data, project)
        if stream is None:
            stream_id = "strm_" + secrets.token_hex(16)
            stream = {
                "stream_id": stream_id,
                "project_id": project,
                "workspace": workspace,
                "created_at": _iso(moment),
                "revision": 0,
                "entries": [],
            }
            streams = data.get("streams")
            if not isinstance(streams, dict):
                _refuse("private_state_corrupt", "The private decision stream is unreadable.")
            streams[project] = stream
        entries = stream.get("entries")
        if not isinstance(entries, list):
            _refuse("private_state_corrupt", "The private decision stream is unreadable.")
        if len(entries) >= MAX_ENTRIES_PER_STREAM:
            _refuse("too_many_entries", "This decision stream is full. Start a new project stream.")
        revision = int(stream.get("revision", 0)) + 1
        entry_id = "dec_" + secrets.token_hex(12)
        body = {"decision": text, "reason": why, "effective_date": day}
        digest = _digest(_canonical(body))
        if operator_session:
            session_fp = _session_fingerprint(operator_session)
            payload = _operator_event_payload(
                workspace=workspace,
                lifetime_id=str(data.get("workspace_lifetime_id", "")),
                project_id=project,
                entry_id=entry_id,
                revision=revision,
                decision=text,
                reason=why,
                effective_date=day,
                digest=digest,
                session_fingerprint=session_fp,
            )
            origin = "authenticated_operator_save"
            author = "operator"
            provenance_event = {
                "operator_event": {
                    "session_fingerprint": session_fp,
                    "signature": _operator_event_signature(config, payload),
                }
            }
        else:
            origin = "unattributed"
            author = "engine"
            provenance_event = {}
        entry = {
            "entry_id": entry_id,
            "revision": revision,
            "source": "structured_decision",
            "origin": origin,
            "author": author,
            "decision": text,
            "reason": why,
            "effective_date": day,
            "recorded_at": _iso(moment),
            "digest": digest,
            "provenance": {
                "fact_id": entry_id,
                "stream_id": stream.get("stream_id", ""),
                "source_revision": revision,
                "workspace": workspace,
                "project_id": project,
                "author": author,
                "origin": origin,
                "recorded_at": _iso(moment),
                "digest": digest,
                **provenance_event,
            },
        }
        entries.append(entry)
        stream["revision"] = revision
        _save_state(config, workspace, data)
    return {"entry": _entry_view(entry), "stream": _stream_view(stream)}


def record_operator_decision(
    config: Any,
    *,
    workspace: str,
    project_id: str,
    decision: object,
    reason: object,
    effective_date: object,
    session: str,
    disclosure_token: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authenticated operator route entry, signed as a per-event operator event."""
    if not isinstance(session, str) or not session:
        _refuse("session_required", "An authenticated operator session is required.")
    return record_decision(
        config, workspace=workspace, project_id=project_id, decision=decision,
        reason=reason, effective_date=effective_date,
        operator_session=session, now=now,
        disclosure_token=disclosure_token,
    )


def supersede_entry(
    config: Any,
    *,
    workspace: str,
    project_id: str,
    entry_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Engine-recorded correction: an old entry stops resolving as eligible."""
    _require_workspace(config, workspace)
    project = _require_project(project_id)
    if not isinstance(entry_id, str) or not _ENTRY_ID_RE.fullmatch(entry_id):
        _refuse("invalid_entry", "The decision entry is not available.")
    moment = _now(now)
    with _workspace_guard(config, workspace):
        data = _load_state(config, workspace)
        stream = _stream_for(data, project)
        entries = stream.get("entries") if stream else None
        if not isinstance(entries, list):
            _refuse("invalid_entry", "The decision entry is not available.")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
                entry["superseded"] = True
                entry["superseded_at"] = _iso(moment)
                _save_state(config, workspace, data)
                return _entry_view(entry)
    _refuse("invalid_entry", "The decision entry is not available.")


def stream_for_project(config: Any, *, workspace: str, project_id: str) -> dict[str, Any] | None:
    _require_workspace(config, workspace)
    stream = _stream_for(_load_state(config, workspace), _require_project(project_id))
    return _stream_view(stream) if stream is not None else None


# ---------------------------------------------------------------------------
# Sources


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _parse_document(path: Path) -> list[dict[str, str]]:
    """Parse bounded ``## Decision`` blocks from an externally editable file.

    Only the three declared field lines are read; prose around them is not
    exported and is not trusted as part of the decision. A malformed block
    refuses the whole document rather than offering a partial reading.
    """
    try:
        fd = open_fd(path, os.O_RDONLY, follow_symlinks=False)
        try:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(MAX_SOURCE_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        _refuse("source_unavailable", "The decision document is not available.")
    if len(raw) > MAX_SOURCE_BYTES:
        _refuse("source_too_large", "The decision document exceeds 65,536 bytes.")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _refuse("invalid_source", "The decision document is not valid UTF-8.")
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def finish() -> None:
        nonlocal current
        if current is None:
            return
        if {"decision", "effective_date"} <= set(current):
            entries.append(current)
            if len(entries) > MAX_DOCUMENT_ENTRIES:
                _refuse("source_too_large", "The decision document has too many entries.")
        current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## Decision"):
            finish()
            current = {}
            continue
        if current is None or not line:
            continue
        if line.startswith("## "):
            finish()
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        if name == "Decision":
            if "decision" in current:
                _refuse("invalid_source", "The decision document repeats a field.")
            current["decision"] = value.strip()
        elif name == "Reason":
            if "reason" in current:
                _refuse("invalid_source", "The decision document repeats a field.")
            current["reason"] = value.strip()
        elif name == "Effective date":
            if "effective_date" in current:
                _refuse("invalid_source", "The decision document repeats a field.")
            current["effective_date"] = value.strip()
        elif name in ("Decision", "Reason", "Effective date"):
            _refuse("invalid_source", "The decision document has a malformed field.")
    finish()
    if not entries:
        _refuse("invalid_source", "The decision document has no complete decision.")
    for entry in entries:
        entry["decision"] = _clean_text(entry.get("decision", ""), "Decision", MAX_DECISION_CHARS, required=True)
        entry["reason"] = _clean_text(entry.get("reason", ""), "Reason", MAX_REASON_CHARS, required=False)
        entry["effective_date"] = _clean_effective_date(entry.get("effective_date", ""))
    return entries


def list_sources(
    config: Any,
    *,
    workspace: str,
    project_id: str,
    project_name: str,
    project_dir: Path,
) -> list[dict[str, Any]]:
    """Server-filtered actual sources for one project in one workspace.

    Only the engine-owned stream and the project's own ``Decisions.md`` can be
    offered. A document outside the workspace vault is not listed.
    """
    _require_workspace(config, workspace)
    project = _require_project(project_id)
    vault = Path(config.workspace_vault_root(workspace))
    if not _inside(project_dir, vault):
        _refuse("invalid_source", "The project folder is outside this workspace.")
    sources: list[dict[str, Any]] = []
    state = _load_state(config, workspace)
    stream = _stream_for(state, project)
    if stream is not None:
        sources.append({
            "source_id": f"stream:{project}",
            "kind": "stream",
            "label": f"Structured decisions · {project_name}",
            "project_id": project,
            "project_name": project_name,
            "entry_count": _stream_view(stream)["entry_count"],
            "stream_id": stream.get("stream_id", ""),
        })
    document = project_dir / DOCUMENT_NAME
    if document.is_file() and not document.is_symlink():
        try:
            entries = _parse_document(document)
        except (EvidenceError, OSError):
            entries = []
        if entries:
            sources.append({
                "source_id": DOCUMENT_SOURCE,
                "kind": "document",
                "label": f"Decision document · {DOCUMENT_NAME}",
                "project_id": project,
                "project_name": project_name,
                "entry_count": len(entries),
            })
    return sources


def _source_entries(
    config: Any,
    *,
    workspace: str,
    project_id: str,
    project_dir: Path,
    source_id: str,
) -> list[dict[str, Any]]:
    _require_project(project_id)
    stream_source = f"stream:{project_id}"
    if source_id == stream_source:
        stream = _stream_for(_load_state(config, workspace), project_id)
        entries = stream.get("entries") if stream else None
        if not isinstance(entries, list):
            _refuse("source_unavailable", "The project decision stream is not available.")
        return [entry for entry in entries if isinstance(entry, dict)]
    if source_id == DOCUMENT_SOURCE:
        if not _inside(project_dir, Path(config.workspace_vault_root(workspace))):
            _refuse("invalid_source", "The project folder is outside this workspace.")
        document = project_dir / DOCUMENT_NAME
        if not document.is_file() or document.is_symlink():
            _refuse("source_unavailable", "The decision document is not available.")
        return [dict(entry) for entry in _parse_document(document)]
    _refuse("invalid_source", "Choose one of the listed sources.")


def _link(config: Any, workspace: str, brain_name: str) -> SharedBrainLink:
    record = config.workspace(workspace) if hasattr(config, "workspace") else None
    links = list(getattr(record, "shared_brains", []) or [])
    for link in links:
        if link.name == brain_name:
            return cast(SharedBrainLink, link)
    _refuse("brain_unavailable", "The selected shared brain is not linked to this workspace.")


def _link_matches(link: SharedBrainLink, grant: dict[str, Any]) -> bool:
    brain = grant.get("brain")
    if not isinstance(brain, dict):
        return False
    return (
        brain.get("name") == link.name
        and brain.get("identity") == link.identity
        and brain.get("lifetime_id") == link.lifetime_id
    )


def _grant_is_live(config: Any, workspace: str, grant: dict[str, Any]) -> bool:
    return (
        bool(grant.get("active"))
        and grant.get("type") == "standing"
        and _grant_linked(config, workspace, grant)
    )


# ---------------------------------------------------------------------------
# Preview and confirm


def _evidence_dir(config: Any, workspace: str) -> Path:
    return _workspace_dir(config, workspace) / "evidence"


def _evidence_key(
    lifetime_id: str, project_id: str, source_id: str, canonical: bytes
) -> str:
    return _digest(
        b"share-evidence-evidence\0"
        + lifetime_id.encode("utf-8")
        + b"\0"
        + project_id.encode("utf-8")
        + b"\0"
        + source_id.encode("utf-8")
        + b"\0"
        + canonical
    )


def _token_hash(token: str) -> str:
    return _digest(b"share-evidence-preview\0" + token.encode("utf-8"))


def _session_hash(session: str) -> str:
    return _digest(b"share-evidence-session\0" + session.encode("utf-8"))


def _field_rows(fields: dict[str, str], mask: list[str]) -> list[dict[str, str]]:
    return [{"field": name, "value": fields.get(name, "")} for name in FIELDS if name in mask]


def _sample_stream(entries: list[dict[str, Any]], mask: list[str]) -> list[list[dict[str, str]]]:
    sample = [entry for entry in entries if not entry.get("superseded")][-2:]
    return [_field_rows(entry, mask) for entry in sample]


def _link_fingerprint(lifetime_id: object) -> str:
    """A non-secret local identifier for one brain link lifetime."""
    if not isinstance(lifetime_id, str) or not lifetime_id:
        return ""
    return _digest(b"share-evidence-link\0" + lifetime_id.encode("utf-8"))[:16]


def _grant_linked(config: Any, workspace: str, grant: dict[str, Any]) -> bool:
    raw_brain = grant.get("brain")
    if not isinstance(raw_brain, dict):
        return False
    try:
        link = _link(config, workspace, str(raw_brain.get("name", "")))
    except EvidenceError:
        return False
    return _link_matches(link, grant)


def _public_grant(grant: dict[str, Any], *, linked: bool) -> dict[str, Any]:
    """The operator-facing grant, with no evidence bytes and no bearer field."""
    raw_brain = grant.get("brain")
    brain: dict[str, Any] = raw_brain if isinstance(raw_brain, dict) else {}
    active = bool(grant.get("active", False))
    if not active:
        status = "withdrawn" if grant.get("withdrawn_at") else (
            "revoked" if grant.get("revoked_at") else "inactive"
        )
    else:
        status = "active" if linked else "unlinked"
    return {
        "grant_id": grant.get("grant_id", ""),
        "type": grant.get("type", ""),
        "project_id": grant.get("project_id", ""),
        "source_id": grant.get("source_id", ""),
        "source_kind": grant.get("source_kind", ""),
        "source_label": grant.get("source_label", ""),
        "brain": {
            "name": brain.get("name", ""),
            "identity": brain.get("identity", ""),
            "link_fingerprint": _link_fingerprint(brain.get("lifetime_id")),
        },
        "audience": grant.get("audience", ""),
        "field_mask": list(grant.get("field_mask") or []),
        "producers": list(grant.get("producers") or []),
        "issued_at": grant.get("issued_at", ""),
        "start_revision": grant.get("start_revision"),
        "consent_generation": grant.get("consent_generation", 0),
        "active": active,
        "linked": linked,
        "status": status,
        "revoked_at": grant.get("revoked_at"),
        "revoked_generation": grant.get("revoked_generation"),
        "withdrawn_at": grant.get("withdrawn_at"),
        "withdrawal_reason": grant.get("withdrawal_reason", ""),
        "history": list(grant.get("history") or []),
    }


def list_grants(config: Any, *, workspace: str) -> list[dict[str, Any]]:
    _require_workspace(config, workspace)
    grants = _load_state(config, workspace).get("grants")
    rows = grants.values() if isinstance(grants, dict) else []
    return [
        _public_grant(row, linked=_grant_linked(config, workspace, row))
        for row in rows if isinstance(row, dict)
    ]


def _disclosure_token(config: Any, workspace: str, project: str, session: str, data: dict[str, Any]) -> str:
    grants = data.get("grants")
    rows = grants.values() if isinstance(grants, dict) else []
    scope = sorted(
        (_public_grant(row, linked=_grant_linked(config, workspace, row))
         for row in rows if isinstance(row, dict) and row.get("project_id") == project
         and row.get("type") == "standing"),
        key=lambda row: str(row["grant_id"]),
    )
    payload = json.dumps(
        {"workspace": workspace, "lifetime": data.get("workspace_lifetime_id"),
         "project": project, "session": _session_hash(session), "grants": scope},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_relation_secret(config), payload, hashlib.sha256).hexdigest()


def disclosure_state(config: Any, *, workspace: str, projects: list[str], session: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Read displayed grants and their save tokens in the same transaction."""
    _require_workspace(config, workspace)
    with _workspace_guard(config, workspace):
        data = _load_state(config, workspace)
        grants = data.get("grants")
        rows = grants.values() if isinstance(grants, dict) else []
        public = [_public_grant(row, linked=_grant_linked(config, workspace, row))
                  for row in rows if isinstance(row, dict)]
        tokens = {project: _disclosure_token(config, workspace, project, session, data)
                  for project in projects}
        return public, tokens


def grant_detail(config: Any, *, workspace: str, grant_id: str) -> dict[str, Any]:
    _require_workspace(config, workspace)
    if not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id):
        _refuse("grant_unavailable", "The grant is not available.")
    state = _load_state(config, workspace)
    grants = state.get("grants")
    grant = grants.get(grant_id) if isinstance(grants, dict) else None
    if (
        not isinstance(grant, dict)
        or grant.get("workspace") != workspace
        or grant.get("workspace_lifetime_id") != state.get("workspace_lifetime_id")
    ):
        _refuse("grant_unavailable", "The grant is not available.")
    return _public_grant(grant, linked=_grant_linked(config, workspace, grant))


def workspace_mode(config: Any, *, workspace: str) -> str:
    """Automatic only for a live standing grant on a still-linked brain."""
    _require_workspace(config, workspace)
    state = _load_state(config, workspace)
    grants = state.get("grants")
    rows = grants.values() if isinstance(grants, dict) else []
    return MODE_AUTOMATIC if any(
        isinstance(row, dict)
        and row.get("workspace") == workspace
        and row.get("workspace_lifetime_id") == state.get("workspace_lifetime_id")
        and _grant_is_live(config, workspace, row)
        for row in rows
    ) else MODE_REVIEW


def preview(
    config: Any,
    *,
    workspace: str,
    brain_name: str,
    project_id: str,
    project_name: str,
    project_dir: Path,
    source_id: str,
    grant_type: str,
    field_mask: object,
    producers: object,
    entry_index: object = None,
    session: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render the exact scope a confirmation would authorize and mint a token.

    The token is bound to this operator session, workspace, workspace lifetime,
    brain lifetime, project, source and expected generation. Preview bytes for
    a snapshot are captured now and are the only bytes a later confirm can
    persist.
    """
    _require_workspace(config, workspace)
    project = _require_project(project_id)
    link = _link(config, workspace, brain_name)
    mask = _clean_field_mask(field_mask)
    chosen_producers = _clean_producers(producers)
    if grant_type not in GRANT_TYPES:
        _refuse("invalid_grant_type", "Choose future decisions or only this decision.")
    if not isinstance(session, str) or not session:
        _refuse("session_required", "An operator session is required.")
    if source_id not in (f"stream:{project}", DOCUMENT_SOURCE):
        _refuse("invalid_source", "Choose one of the listed sources.")
    if not _inside(project_dir, Path(config.workspace_vault_root(workspace))):
        _refuse("invalid_source", "The project folder is outside this workspace.")
    if source_id == DOCUMENT_SOURCE and grant_type != "snapshot":
        _refuse("invalid_grant_type", "A decision document supports only an individual snapshot.")
    if source_id.startswith("stream:") and grant_type != "standing":
        _refuse("invalid_grant_type", "A decision stream supports only a standing grant.")
    entries = _source_entries(
        config, workspace=workspace, project_id=project,
        project_dir=project_dir, source_id=source_id,
    )
    source_label = (
        f"Structured decisions · {project_name}"
        if source_id.startswith("stream:")
        else f"Decision document · {DOCUMENT_NAME}"
    )
    moment = _now(now)
    preview_rows: list[list[dict[str, str]]]
    if grant_type == "standing":
        stream = _stream_for(_load_state(config, workspace), project)
        if stream is None:
            _refuse("source_unavailable", "Add a decision before authorizing its stream.")
        revision = int(stream.get("revision", 0))
        if revision == 0:
            _refuse("source_unavailable", "Add a decision before authorizing its stream.")
        preview_rows = _sample_stream(entries, mask)
        snapshot_fields: dict[str, str] | None = None
        expected = str(revision)
        entry_label = f"Decisions from revision {revision + 1} onward"
    else:
        if type(entry_index) is not int or entry_index < 0 or entry_index >= len(entries):
            _refuse("invalid_source_entry", "Choose one decision from the document.")
        fields = {name: str(entries[entry_index].get(name, "")) for name in FIELDS}
        snapshot_fields = {name: fields[name] for name in mask}
        canonical = _canonical(snapshot_fields)
        preview_rows = [_field_rows(fields, mask)]
        expected = _digest(canonical)
        revision = None
        entry_label = f"Decision {entry_index + 1} in {DOCUMENT_NAME}"
    token = "sev_" + secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    record = {
        "schema": SCHEMA,
        "token_hash": token_hash,
        "session_hash": _session_hash(session),
        "workspace": workspace,
        "workspace_lifetime_id": _workspace_lifetime(config, workspace),
        "brain_name": link.name,
        "brain_identity": link.identity,
        "brain_lifetime_id": link.lifetime_id,
        "project_id": project,
        "project_name": project_name,
        "source_id": source_id,
        "source_kind": "stream" if source_id.startswith("stream:") else "document",
        "source_label": source_label,
        "grant_type": grant_type,
        "field_mask": mask,
        "producers": chosen_producers,
        "expected_generation": expected,
        "entry_index": entry_index if grant_type == "snapshot" else None,
        "snapshot_fields": snapshot_fields,
        "created_at": _iso(moment),
        "expires_at": _iso(moment + PREVIEW_TTL),
        "used_at": None,
    }
    with _workspace_guard(config, workspace):
        state = _load_state(config, workspace)
        previews = state.get("previews")
        if not isinstance(previews, dict):
            _refuse("private_state_corrupt", "The private preview state is unreadable.")
        live = sum(
            1 for item in previews.values()
            if isinstance(item, dict)
            and item.get("used_at") is None
            and isinstance(item.get("expires_at"), str)
            and item["expires_at"] > _iso(moment)
        )
        if live >= MAX_PREVIEWS:
            _refuse("too_many_previews", "Too many unreviewed previews. Finish or cancel one first.")
        previews[token_hash] = record
        _save_state(config, workspace, state)
    return {
        "preview_token": token,
        "preview": {
            "grant_type": grant_type,
            "scope": "future_decisions" if grant_type == "standing" else "one_decision",
            "summary": (
                "When automatic sharing becomes available, future structured decisions in this project may be shared without review."
                if grant_type == "standing"
                else "When sharing becomes available, only the decision shown below is permitted, exactly as previewed."
            ),
            "future_items_without_review": grant_type == "standing",
            "project_id": project,
            "source_id": source_id,
            "source_label": source_label,
            "entry_label": entry_label,
            "fields": preview_rows,
            "field_mask": mask,
            "excluded_fields": [name for name in FIELDS if name not in mask],
            "producers": chosen_producers,
            "recipient": {
                "brain": link.name,
                "identity": link.identity,
                "audience": link.identity,
            },
            "expected_generation": expected,
            "expires_at": record["expires_at"],
            "notice": (
                "Changing or deleting the document later does not withdraw this confirmation; "
                "revoke it here instead. A newer edit needs a fresh preview and grant."
                if grant_type == "snapshot"
                else "Only entries recorded after this grant can be extracted. Entries already "
                     "recorded are not included. Revoke in Settings at any time."
            ),
        },
    }


def _preview_for(
    state: dict[str, Any], workspace: str, token: str
) -> tuple[str, dict[str, Any]]:
    if not isinstance(token, str) or not token.startswith("sev_"):
        _refuse("invalid_preview", "The preview is not available. Preview the scope again.")
    token_hash = _token_hash(token)
    previews = state.get("previews")
    record = previews.get(token_hash) if isinstance(previews, dict) else None
    if (
        not isinstance(record, dict)
        or record.get("token_hash") != token_hash
        or record.get("workspace") != workspace
    ):
        _refuse("invalid_preview", "The preview is not available. Preview the scope again.")
    return token_hash, record


def _validate_preview_use(
    record: dict[str, Any], *, session: str, now: datetime
) -> None:
    if record.get("used_at") is not None:
        _refuse("preview_used", "That preview was already confirmed. Start a new one.")
    if not isinstance(session, str) or _session_hash(session) != record.get("session_hash"):
        _refuse("preview_session_mismatch", "This preview belongs to another operator session.")
    if not isinstance(record.get("expires_at"), str) or record["expires_at"] <= _iso(now):
        _refuse("preview_expired", "The preview expired. Review the exact scope again.")


def preview_target(
    config: Any,
    *,
    workspace: str,
    token: str,
    session: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Validate a token's session and expiry, and return its local target only."""
    _require_workspace(config, workspace)
    state = _load_state(config, workspace)
    _hash, record = _preview_for(state, workspace, token)
    _validate_preview_use(record, session=session, now=_now(now))
    return {
        "project_id": str(record.get("project_id", "")),
        "brain_name": str(record.get("brain_name", "")),
        "source_id": str(record.get("source_id", "")),
    }


def confirm(
    config: Any,
    *,
    workspace: str,
    token: str,
    session: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically consume the preview and persist exactly the previewed scope."""
    _require_workspace(config, workspace)
    moment = _now(now)
    lifetime = _workspace_lifetime(config, workspace)
    with _workspace_guard(config, workspace):
        state = _load_state(config, workspace)
        token_hash, record = _preview_for(state, workspace, token)
        grants = state.get("grants")
        if not isinstance(grants, dict):
            _refuse("private_state_corrupt", "The private grant store is unreadable.")
        # A grant already carries this preview: a crash or replay cannot mint a second one.
        if any(
            isinstance(row, dict) and row.get("preview_token_hash") == token_hash
            for row in grants.values()
        ):
            record["used_at"] = str(record.get("used_at") or _iso(moment))
            _save_state(config, workspace, state)
            _refuse("preview_used", "That preview was already confirmed. Start a new one.")
        _validate_preview_use(record, session=session, now=moment)
        link = _link(config, workspace, str(record.get("brain_name", "")))
        if not (
            link.identity == record.get("brain_identity")
            and link.lifetime_id == record.get("brain_lifetime_id")
        ):
            _refuse("brain_changed", "The selected shared brain changed while previewing.")
        project = _require_project(str(record.get("project_id", "")))
        grant_type = record.get("grant_type")
        mask = _clean_field_mask(record.get("field_mask"))
        chosen_producers = _clean_producers(record.get("producers"))
        if len(grants) >= MAX_GRANTS_PER_WORKSPACE:
            _refuse("too_many_grants", "This workspace has too many evidence grants.")
        grant_id = "grn_" + secrets.token_hex(12)
        evidence_key: str | None = None
        start_revision: int | None = None
        if grant_type == "standing":
            stream = _stream_for(state, project)
            if stream is None:
                _refuse("source_unavailable", "The project decision stream is not available.")
            if str(stream.get("revision", 0)) != record.get("expected_generation"):
                _refuse("stale_preview", "The decision stream changed. Review the new scope.")
            start_revision = int(stream.get("revision", 0))
        elif grant_type == "snapshot":
            snapshot_fields = record.get("snapshot_fields")
            if not isinstance(snapshot_fields, dict) or set(snapshot_fields) != set(mask):
                _refuse("private_state_corrupt", "The previewed snapshot is unreadable.")
            canonical = _canonical(snapshot_fields)
            if _digest(canonical) != record.get("expected_generation"):
                _refuse("preview_corrupt", "The previewed snapshot does not match its token.")
            source_id = str(record.get("source_id", ""))
            evidence_key = _evidence_key(lifetime, project, source_id, canonical)
            evidence = {
                "schema": SCHEMA,
                "workspace": workspace,
                "workspace_lifetime_id": lifetime,
                "project_id": project,
                "source_id": source_id,
                "source_label": record.get("source_label"),
                "field_mask": mask,
                "fields": snapshot_fields,
                "canonical_bytes": canonical.decode("utf-8"),
                "digest": _digest(canonical),
                "evidence_key": evidence_key,
                "captured_at": record.get("created_at"),
            }
            evidence_path = _evidence_dir(config, workspace) / (evidence_key + ".json")
            if evidence_path.exists():
                stored = _read_json(
                    evidence_path, limit=MAX_EVIDENCE_BYTES, code="evidence_corrupt"
                )
                if (
                    stored.get("evidence_key") != evidence_key
                    or stored.get("canonical_bytes") != evidence["canonical_bytes"]
                ):
                    _refuse("evidence_corrupt", "The stored snapshot does not match this preview.")
            else:
                _write_private(
                    evidence_path,
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                    limit=MAX_EVIDENCE_BYTES,
                )
        else:
            _refuse("invalid_grant_type", "The preview has no supported grant type.")
        issued = _iso(moment)
        grant: dict[str, Any] = {
            "schema": SCHEMA,
            "grant_id": grant_id,
            "type": grant_type,
            "workspace": workspace,
            "workspace_lifetime_id": lifetime,
            "project_id": project,
            "source_id": record.get("source_id"),
            "source_kind": record.get("source_kind"),
            "source_label": record.get("source_label"),
            "brain": {
                "name": link.name,
                "identity": link.identity,
                "lifetime_id": link.lifetime_id,
            },
            "audience": link.identity,
            "field_mask": mask,
            "producers": chosen_producers,
            "issuer": "operator",
            "issued_at": issued,
            "start_revision": start_revision,
            "evidence_key": evidence_key,
            "preview_token_hash": token_hash,
            "consent_generation": 1,
            "active": True,
            "revoked_at": None,
            "revoked_generation": None,
            "withdrawn_at": None,
            "withdrawal_reason": "",
            "history": [{
                "event": "issued",
                "at": issued,
                "generation": 1,
                "grant_type": grant_type,
                "field_mask": mask,
                "producers": chosen_producers,
            }],
        }
        grants[grant_id] = grant
        record["used_at"] = issued
        _save_state(config, workspace, state)
    return {"grant": _public_grant(grant, linked=True), "mode": workspace_mode(config, workspace=workspace)}


def revoke(
    config: Any,
    *,
    workspace: str,
    grant_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Withdraw this workspace's grant; the old generation cannot resolve."""
    _require_workspace(config, workspace)
    moment = _now(now)
    with _workspace_guard(config, workspace):
        data = _load_state(config, workspace)
        grants = data.get("grants")
        grant = grants.get(grant_id) if isinstance(grants, dict) else None
        if (
            not isinstance(grant, dict)
            or grant.get("workspace") != workspace
            or grant.get("workspace_lifetime_id") != data.get("workspace_lifetime_id")
        ):
            _refuse("grant_unavailable", "The grant is not available.")
        if grant.get("active"):
            grant["active"] = False
            grant["revoked_at"] = _iso(moment)
            grant["revoked_generation"] = grant.get("consent_generation", 1)
            grant.setdefault("history", []).append({
                "event": "revoked",
                "at": _iso(moment),
                "generation": grant.get("consent_generation", 1),
            })
            _save_state(config, workspace, data)
    return {
        "grant": _public_grant(grant, linked=_grant_linked(config, workspace, grant)),
        "fresh_grant_required": True,
    }


def withdraw(
    config: Any,
    *,
    workspace: str,
    grant_id: str,
    reason: object = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Engine-recorded withdrawal of an authorized snapshot or grant."""
    note = _clean_text(reason, "Reason", MAX_REASON_CHARS, required=False)
    moment = _now(now)
    with _workspace_guard(config, workspace):
        data = _load_state(config, workspace)
        grants = data.get("grants")
        grant = grants.get(grant_id) if isinstance(grants, dict) else None
        if (
            not isinstance(grant, dict)
            or grant.get("workspace") != workspace
            or grant.get("workspace_lifetime_id") != data.get("workspace_lifetime_id")
        ):
            _refuse("grant_unavailable", "The grant is not available.")
        if grant.get("active"):
            grant["active"] = False
            grant["withdrawn_at"] = _iso(moment)
            grant["withdrawal_reason"] = note
            grant.setdefault("history", []).append({
                "event": "withdrawn",
                "at": _iso(moment),
                "generation": grant.get("consent_generation", 1),
                "reason": note,
            })
            _save_state(config, workspace, data)
    return {"grant": _public_grant(grant, linked=_grant_linked(config, workspace, grant))}


# ---------------------------------------------------------------------------
# Producer source lookup


def _confirmed_snapshot_available(
    config: Any, *, workspace: str, project_id: str, source_id: str
) -> bool:
    """A live snapshot grant with its immutable evidence still present."""
    state = _load_state(config, workspace)
    grants = state.get("grants")
    rows = grants.values() if isinstance(grants, dict) else []
    for grant in rows:
        if (
            isinstance(grant, dict)
            and grant.get("active")
            and grant.get("type") == "snapshot"
            and grant.get("workspace") == workspace
            and grant.get("workspace_lifetime_id") == state.get("workspace_lifetime_id")
            and grant.get("project_id") == project_id
            and grant.get("source_id") == source_id
        ):
            evidence_key = grant.get("evidence_key")
            if isinstance(evidence_key, str) and (
                _evidence_dir(config, workspace) / (evidence_key + ".json")
            ).is_file():
                return True
    return False


def memory_pass_source_relation(
    config: Any,
    registry: Any,
    *,
    workspace: str,
    pass_chat_id: str,
    source_id: str = "",
    now: datetime | None = None,
) -> SourceRelation:
    """Prove one completed managed memory pass and its verified source chat.

    The live managed principal is the pass chat's own, which is what exists at
    engine completion; the source chat's token is already revoked by archival.
    The pass must be done in the workspace's app-owned Memory project and name
    an archived source chat whose project genuinely owns the requested stream or
    confirmed project decision document.
    """
    _require_workspace(config, workspace)
    if not isinstance(pass_chat_id, str) or not pass_chat_id:
        _refuse("invalid_source_chat", "The memory pass chat is not available.")
    from ciao.web import chat_service, memory_pass  # noqa: PLC0415

    pcm = _project_manager()
    principal = (
        registry.principal_for_chat(pass_chat_id)
        if hasattr(registry, "principal_for_chat") else None
    )
    if principal is None:
        _refuse(
            "managed_session_required",
            "Memory identity requires the live managed pass; unmanaged runs are review-only.",
        )
    pass_chat = pcm.get_chat(pass_chat_id)
    pass_project = pcm.get_project(pass_chat.project_id) if pass_chat is not None else None
    if (
        pass_chat is None
        or pass_project is None
        or pass_project.kind != memory_pass.MEMORY_PROJECT_KIND
        or pass_project.workspace != workspace
        or principal.chat_id != pass_chat_id
        or principal.workspace != workspace
        or principal.project_id != pass_project.project_id
    ):
        _refuse("invalid_source_chat", "The memory pass does not belong to this workspace.")
    helper = chat_service._normalize_chat_helper(pass_chat.helper)
    source_chat_id = str(helper.get("source_chat_id") or "")
    if helper.get("kind") != memory_pass.MEMORY_PASS_KIND or helper.get("state") != "done":
        _refuse("source_unavailable", "The memory pass is not complete.")
    if not source_chat_id:
        _refuse("source_unavailable", "The memory pass names no source chat.")
    chat = pcm.get_chat(source_chat_id)
    project = pcm.get_project(chat.project_id) if chat is not None else None
    if chat is None or project is None or project.workspace != workspace:
        _refuse("invalid_source_chat", "The source chat does not belong to this workspace.")
    if memory_pass.is_memory_pass_chat(chat, project):
        _refuse("invalid_source_chat", "A memory pass is not itself an evidence source.")
    if not getattr(chat, "archived", False) or not getattr(chat, "archive_path", ""):
        _refuse("source_unavailable", "The source chat has no archived transcript.")
    locate = getattr(pcm, "_locate_archive", None)
    if not callable(locate):
        _refuse("source_unavailable", "The archive registry is unavailable.")
    archive = locate(source_chat_id, str(chat.archive_path))
    if archive is None:
        _refuse("source_unavailable", "The archived transcript is not under this workspace.")
    stored = str(helper.get("archive_path") or "")
    pass_archive = locate(source_chat_id, stored) if stored else None
    if pass_archive is None or pass_archive != archive:
        _refuse("source_unavailable", "The memory pass names a different archive.")
    owned_roots = [
        Path(root) / "Logs" / "Chats"
        for root, name in config.agent_root_targets()
        if name == workspace
    ] if hasattr(config, "agent_root_targets") else []
    if owned_roots:
        try:
            resolved_archive = archive.resolve()
            if not any(
                resolved_archive.is_relative_to(root.resolve()) for root in owned_roots
            ):
                _refuse(
                    "source_unavailable",
                    "The archived transcript belongs to another workspace.",
                )
        except OSError:
            _refuse("source_unavailable", "The archived transcript is unavailable.")
    chosen_source = source_id or f"stream:{project.project_id}"
    if chosen_source == f"stream:{project.project_id}":
        pass
    elif chosen_source == DOCUMENT_SOURCE:
        project_dir = pcm.active_project_vault_dir(project.project_id)
        if project_dir is None:
            _refuse("source_unavailable", "The project decision document is unavailable.")
        document = Path(project_dir) / DOCUMENT_NAME
        if (
            not document.is_file()
            and not _confirmed_snapshot_available(
                config, workspace=workspace, project_id=project.project_id,
                source_id=DOCUMENT_SOURCE,
            )
        ):
            _refuse("source_unavailable", "The project decision document is unavailable.")
    else:
        _refuse("invalid_source", "The memory pass can select only this project's stream or document.")
    moment = _now(now)
    return _seal_relation(config, SourceRelation(
        kind="memory_pass_chat",
        producer="memory_extraction",
        workspace=workspace,
        workspace_lifetime_id=_workspace_lifetime(config, workspace),
        project_id=str(project.project_id),
        source_id=chosen_source,
        relation_id=pass_chat_id,
        evidence=str(archive),
        lease_vault="",
        lease_proof_hash="",
        nonce=secrets.token_hex(16),
        attested_at=_iso(moment),
        expires_at=_iso(moment + RELATION_TTL),
        seal=b"",
    ))
def curation_source_relation(
    config: Any,
    registry: Any,
    vault_root: Path,
    *,
    workspace: str,
    project_id: str,
    source_id: str,
    lease: Any,
    agent_token: str,
    now: datetime | None = None,
) -> SourceRelation:
    """Prove a project source through a live curation lease's planned work item.

    The project and its folder come from the bound engine project manager, and
    the caller must present the ``Lease`` object returned by ``curation_run``.
    That object carries the one-time proof whose hash is in the live state, so
    reading or copying the holder from the state is not enough.
    """
    _require_workspace(config, workspace)
    project = _require_project(project_id)
    pcm = _project_manager()
    principal = registry.principal_for_token(agent_token) if hasattr(registry, "principal_for_token") else None
    if principal is None:
        _refuse(
            "managed_session_required",
            "Curation identity requires a live managed agent session; unmanaged CLI is review-only.",
        )
    owner = pcm.get_project(project)
    if owner is None or getattr(owner, "workspace", "") != workspace or principal.workspace != workspace:
        _refuse("invalid_project", "The project does not belong to this managed agent's workspace.")
    resolved_dir = pcm.active_project_vault_dir(project)
    if resolved_dir is None:
        _refuse("invalid_project", "The project has no active folder in this workspace.")
    project_dir = Path(resolved_dir)
    vault = Path(config.workspace_vault_root(workspace))
    try:
        if Path(vault_root).resolve() != vault.resolve():
            _refuse("invalid_source", "The curation vault is not this workspace's vault.")
    except OSError:
        _refuse("invalid_source", "The curation vault is not this workspace's vault.")
    if not _inside(project_dir, vault):
        _refuse("invalid_source", "The project folder is outside this workspace.")
    if source_id not in (f"stream:{project}", DOCUMENT_SOURCE):
        _refuse("invalid_source", "The source is not a project decision source.")
    # The plan proves the project, not a mutable file. Compute every stable
    # project work-item key independently of which files currently exist or
    # which one preference would pick: a planned Decisions.md remains provable
    # after deletion, and an unrelated README cannot stand in for it.
    candidate_paths = [
        project_dir / "README.md",
        project_dir / f"{project_dir.name}.md",
        project_dir / DOCUMENT_NAME,
    ]
    try:
        relative_paths = [
            path.resolve().relative_to(vault.resolve()).as_posix()
            for path in candidate_paths
        ]
    except (OSError, ValueError):
        _refuse("invalid_source", "The project source is outside this workspace's vault.")
    from ciao import curation_run  # noqa: PLC0415

    moment = _now(now)
    if (
        not isinstance(lease, curation_run.Lease)
        or not lease.proof
        or not lease.proof_hash
    ):
        _refuse("invalid_lease", "The caller does not hold a curation lease proof.")
    try:
        if lease.vault_root.resolve() != vault.resolve():
            _refuse("invalid_lease", "The curation lease belongs to another vault.")
    except OSError:
        _refuse("invalid_lease", "The curation lease belongs to another vault.")
    try:
        live = curation_run.active_lease(vault, now=moment)
    except curation_run.CurationStateError:
        _refuse("source_unavailable", "The curation lease state is unreadable.")
    if (
        not live
        or str(live.get("holder") or "") != lease.holder
        or live.get("proof_hash") != lease.proof_hash
        or hashlib.sha256(lease.proof.encode("utf-8")).hexdigest() != lease.proof_hash
        or not isinstance(live.get("chat_id"), str)
        or not live["chat_id"]
        or live["chat_id"] != principal.chat_id
    ):
        _refuse("invalid_lease", "The curation lease proof is not current for the lease-owning agent.")
    plan = live.get("plan")
    planned = plan.get("planned") if isinstance(plan, dict) else None
    if not isinstance(planned, list):
        _refuse("source_unavailable", "The live curation lease has no recorded plan.")
    project_keys = {
        curation_run.item_key(curation_run.PASS_STALE_NOTE, relative)
        for relative in relative_paths
    }
    planned_keys = {
        str(key) for pass_id, key in planned
        if pass_id == curation_run.PASS_STALE_NOTE and isinstance(key, str)
    }
    matched = project_keys & planned_keys
    if not matched:
        _refuse(
            "source_unavailable",
            "The live curation plan does not include this project's work item.",
        )
    key = sorted(matched)[0]
    expires = live.get("expires_at")
    if not isinstance(expires, str):
        _refuse("source_unavailable", "The curation lease has no valid expiry.")
    return _seal_relation(config, SourceRelation(
        kind="curation_lease_item",
        producer="nightly_curation",
        workspace=workspace,
        workspace_lifetime_id=_workspace_lifetime(config, workspace),
        project_id=project,
        source_id=source_id,
        relation_id=lease.holder,
        evidence=key,
        lease_vault=str(vault.resolve()),
        lease_proof_hash=lease.proof_hash,
        nonce=secrets.token_hex(16),
        attested_at=_iso(moment),
        expires_at=expires,
        seal=b"",
    ))


def eligible_entries(
    config: Any,
    *,
    workspace: str,
    brain_name: str,
    relation: SourceRelation,
    limit: int = 20,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Entries one engine-attested source relation may publish right now.

    The relation must carry the module's HMAC seal over its workspace lifetime,
    project, source and producer, and it must still be unexpired. Grants are
    then filtered by that exact project/source/producer and the live brain
    lifetime; a forged, retargeted, foreign, expired, unlinked or revoked
    relation resolves nothing.
    """
    _require_workspace(config, workspace)
    if type(limit) is not int or limit < 1 or limit > 100:
        _refuse("invalid_limit", "The limit must be between 1 and 100.")
    if not _relation_is_sealed(config, relation):
        _refuse("invalid_relation", "The producer source relation is not engine-attested.")
    if (
        relation.workspace != workspace
        or relation.workspace_lifetime_id != _workspace_lifetime(config, workspace)
    ):
        return []
    moment = _now(now)
    if relation.expires_at <= _iso(moment):
        return []
    if relation.kind == "curation_lease_item":
        from ciao import curation_run  # noqa: PLC0415

        try:
            lease = curation_run.active_lease(Path(relation.lease_vault), now=moment)
        except (OSError, curation_run.CurationStateError):
            return []
        if not lease or str(lease.get("holder") or "") != relation.relation_id:
            return []
        plan = lease.get("plan")
        planned = plan.get("planned") if isinstance(plan, dict) else None
        if not isinstance(planned, list) or [
            curation_run.PASS_STALE_NOTE, relation.evidence
        ] not in planned:
            return []
    try:
        link = _link(config, workspace, brain_name)
    except EvidenceError:
        return []
    state = _load_state(config, workspace)
    grants = state.get("grants")
    streams = state.get("streams")
    rows = grants.values() if isinstance(grants, dict) else []
    out: list[dict[str, Any]] = []
    for grant in rows:
        if not isinstance(grant, dict) or not grant.get("active"):
            continue
        if (
            grant.get("workspace") != workspace
            or grant.get("workspace_lifetime_id") != state.get("workspace_lifetime_id")
            or grant.get("project_id") != relation.project_id
            or grant.get("source_id") != relation.source_id
            or not _link_matches(link, grant)
        ):
            continue
        if relation.producer not in (grant.get("producers") or []):
            continue
        mask = _clean_field_mask(grant.get("field_mask"))
        project = grant.get("project_id")
        if grant.get("type") == "standing" and grant.get("source_kind") == "stream":
            stream = streams.get(project) if isinstance(streams, dict) else None
            entries = stream.get("entries") if isinstance(stream, dict) else None
            if not isinstance(entries, list):
                continue
            boundary = grant.get("start_revision")
            if type(boundary) is not int:
                continue
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("superseded"):
                    continue
                if type(entry.get("revision")) is not int or entry["revision"] <= boundary:
                    continue
                if not _operator_event_valid(
                    config,
                    entry,
                    workspace=workspace,
                    lifetime_id=str(state.get("workspace_lifetime_id", "")),
                    project_id=str(project),
                ):
                    continue
                fields = {name: str(entry.get(name, "")) for name in mask}
                out.append({
                    "grant_id": grant.get("grant_id", ""),
                    "grant_type": "standing",
                    "entry_id": entry.get("entry_id", ""),
                    "project_id": project,
                    "source_id": grant.get("source_id"),
                    "producer": relation.producer,
                    "relation_kind": relation.kind,
                    "relation_id": relation.relation_id,
                    "relation_evidence": relation.evidence,
                    "relation_expires_at": relation.expires_at,
                    "field_mask": mask,
                    "canonical_bytes": _canonical(fields).decode("utf-8"),
                    "digest": _digest(_canonical(fields)),
                    "recorded_at": entry.get("recorded_at", ""),
                })
                if len(out) >= limit:
                    return out
        elif grant.get("type") == "snapshot":
            evidence_key = grant.get("evidence_key")
            if not isinstance(evidence_key, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_key):
                continue
            evidence_path = _evidence_dir(config, workspace) / (evidence_key + ".json")
            try:
                evidence = _read_json(evidence_path, limit=MAX_EVIDENCE_BYTES, code="evidence_corrupt")
            except EvidenceError:
                continue
            if (
                evidence.get("workspace") != workspace
                or evidence.get("workspace_lifetime_id") != state.get("workspace_lifetime_id")
                or evidence.get("project_id") != project
                or evidence.get("source_id") != relation.source_id
                or evidence.get("evidence_key") != evidence_key
            ):
                continue
            canonical = evidence.get("canonical_bytes")
            if not isinstance(canonical, str) or _digest(canonical.encode("utf-8")) != evidence.get("digest"):
                continue
            out.append({
                "grant_id": grant.get("grant_id", ""),
                "grant_type": "snapshot",
                "entry_id": "",
                "project_id": project,
                "source_id": grant.get("source_id"),
                "producer": relation.producer,
                "relation_kind": relation.kind,
                "relation_id": relation.relation_id,
                "relation_evidence": relation.evidence,
                "relation_expires_at": relation.expires_at,
                "field_mask": mask,
                "canonical_bytes": canonical,
                "digest": evidence.get("digest"),
                "recorded_at": grant.get("issued_at", ""),
            })
            if len(out) >= limit:
                return out
    return out
