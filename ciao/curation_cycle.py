"""The Workspace care cycle, schema v2: a pure, write-free codec (#3061 phase A).

A cycle is the unit a weekly marker publication closes. This module defines its
document and the rules around it, and nothing else: strict encode and decode,
the obligations a cycle still owes, a dry-run migration from the v1 state, and
the decision that recovers an interrupted marker publication. It reads no
files, takes no lock and writes nothing. ``curation_run`` and ``cli`` do not
use it yet; activating it on a live vault is a separate, deliberate step.

Marker identity is byte-level on purpose. ``read_last_full_pass`` maps absent,
malformed and ambiguous markers to the same advisory "", so recovery compares
digests of the exact bytes instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from ciao import curation_logs
from ciao.curation_run import (
    PASS_GUIDE,
    PASS_OBSERVATIONS,
    PASS_RULES,
    REQUIRED_HYGIENE_KEYS,
    CurationState,
    WorklistItem,
    item_key,
)

CYCLE_SCHEMA_VERSION = 2

# The subject ``curation_run`` hashes for the observation merge work, and the
# subject for the guide review. Restated here so the classification is by key.
GUIDE_KEY = item_key(PASS_GUIDE, "guide-body")
MERGE_KEY = item_key(PASS_OBSERVATIONS, "merge")

_HEX64 = re.compile(r"[0-9a-f]{64}")
_CYCLE_ID = re.compile(r"c2-[0-9a-f]{24}")

_STATE_FIELDS = frozenset(
    {
        "version",
        "cycle_id",
        "prior_marker",
        "required",
        "optional",
        "carried",
        "legacy_historical",
        "daily",
        "lease",
        "last_run",
        "pending",
    }
)
_MARKER_FIELDS = frozenset({"kind", "sha256"})
_PENDING_FIELDS = frozenset(
    {"cycle_id", "prior", "target_stamp", "target_sha256", "proven_required", "phase"}
)


class CycleError(ValueError):
    """A cycle document, marker identity or transition cannot be trusted."""


# ── Marker identity ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarkerIdentity:
    """What the curation log's marker looked like at one moment, by bytes.

    ``absent`` means no log file at all and has no digest. ``present`` means
    the log's exact bytes hash to ``sha256``, whatever those bytes say.
    """

    kind: Literal["absent", "present"]
    sha256: str | None

    def __post_init__(self) -> None:
        if self.kind == "absent":
            if self.sha256 is not None:
                raise CycleError("an absent marker has no digest")
        elif self.kind == "present":
            if not isinstance(self.sha256, str) or not _HEX64.fullmatch(self.sha256):
                raise CycleError("a present marker needs a lowercase sha256 digest")
        else:
            raise CycleError("a marker is either absent or present")


def marker_identity_of(data: bytes | None) -> MarkerIdentity:
    """The identity of the log bytes as they are, or ``absent`` for no file."""
    if data is None:
        return MarkerIdentity("absent", None)
    return MarkerIdentity("present", hashlib.sha256(data).hexdigest())


def expected_target(existing: bytes | None, stamp: str) -> MarkerIdentity:
    """The identity the log will have once ``stamp`` is written into it.

    Computed with the real byte-preserving writer, so the digest is what
    publication will produce and not a re-derivation of it.
    """
    _check_date(stamp, "target stamp")
    try:
        written = curation_logs.with_full_pass_marker(existing, stamp)
    except curation_logs.CurationLogError as exc:
        raise CycleError(str(exc)) from exc
    return marker_identity_of(written)


# ── Transitions ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PreparedTransition:
    """A marker publication the state has committed to, before it has happened.

    ``prepared`` means the state records the intent and the marker may or may
    not have been published. ``committed`` means the state has recorded that
    the publication is done.
    """

    cycle_id: str
    prior: MarkerIdentity
    target_stamp: str
    target_sha256: str
    proven_required: tuple[str, ...]
    phase: Literal["prepared", "committed"]

    def __post_init__(self) -> None:
        _check_cycle_id(self.cycle_id)
        _check_date(self.target_stamp, "target stamp")
        if not isinstance(self.target_sha256, str) or not _HEX64.fullmatch(self.target_sha256):
            raise CycleError("the target digest must be a lowercase sha256")
        if not isinstance(self.proven_required, tuple):
            raise CycleError("proven required keys must be a tuple")
        if self.proven_required != tuple(sorted(set(self.proven_required))):
            raise CycleError("proven required keys must be sorted and unique")
        if not set(self.proven_required) <= REQUIRED_HYGIENE_KEYS:
            raise CycleError("only required hygiene keys can be proven")
        if self.phase not in ("prepared", "committed"):
            raise CycleError("a transition is prepared or committed")


RecoveryDecision = Literal["resume", "commit", "block"]


def decide_recovery(
    pending: PreparedTransition, observed: MarkerIdentity | None
) -> RecoveryDecision:
    """What to do with a prepared transition, given the marker bytes found now.

    ``commit``: the target bytes are published, so record the commit. ``resume``:
    the prior bytes are still there, so publish once more; the caller must not
    loop on it. ``block``: anything else, including bytes that could not be read
    (pass ``None``), a drifted or truncated file, or a regression from a
    committed transition. Pure: it decides and never publishes.
    """
    if observed is None:
        return "block"
    target = MarkerIdentity("present", pending.target_sha256)
    if observed == target:
        return "commit"
    if pending.phase == "prepared" and observed == pending.prior:
        return "resume"
    return "block"


# ── Cycle state ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CycleState:
    """One cycle's evidence, plus the carried and historical records around it.

    ``required``, ``optional`` and ``daily`` map a work key to the ISO date its
    done evidence was recorded, and are the only maps that certify this cycle.
    ``legacy_historical`` keeps v1 weekly keys for the record and never proves
    anything. ``carried`` lists optional work still owed after the marker
    advanced.
    """

    cycle_id: str
    prior_marker: MarkerIdentity
    required: dict[str, str]
    optional: dict[str, str]
    carried: tuple[str, ...]
    legacy_historical: dict[str, str]
    daily: dict[str, str]
    lease: dict[str, Any]
    last_run: dict[str, Any]
    pending: PreparedTransition | None

    def __post_init__(self) -> None:
        _check_cycle_id(self.cycle_id)
        if not isinstance(self.prior_marker, MarkerIdentity):
            raise CycleError("the prior marker must be a MarkerIdentity")
        for label, mapping in (
            ("required", self.required),
            ("optional", self.optional),
            ("legacy_historical", self.legacy_historical),
            ("daily", self.daily),
        ):
            _check_date_map(mapping, label)
        if not isinstance(self.carried, tuple) or self.carried != tuple(sorted(set(self.carried))):
            raise CycleError("carried work must be a sorted, unique tuple")
        for key in self.carried:
            _check_key(key, "carried work")
        _check_lease(self.lease)
        _check_last_run(self.last_run)
        if self.pending is not None:
            if not isinstance(self.pending, PreparedTransition):
                raise CycleError("pending must be a PreparedTransition or null")
            if self.pending.cycle_id != self.cycle_id:
                raise CycleError("a pending transition belongs to another cycle")


def cycle_identity(prior_marker: MarkerIdentity, active_cycle_id: str) -> str:
    """The id of the cycle in progress, which a week rollover must not change.

    An active id is kept as it is. A new cycle is named from the prior marker
    bytes, which carries no ISO week and no clock. The bytes are read when the
    cycle starts; later appends to the log do not rename a running cycle,
    because the active id is kept.
    """
    if active_cycle_id:
        _check_cycle_id(active_cycle_id)
        return active_cycle_id
    basis = json.dumps(
        {"kind": prior_marker.kind, "sha256": prior_marker.sha256},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "c2-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def encode_state(state: CycleState) -> str:
    """The canonical JSON text of a cycle. Sorted keys, two-space indent, final newline."""
    document = {
        "version": CYCLE_SCHEMA_VERSION,
        "cycle_id": state.cycle_id,
        "prior_marker": _marker_document(state.prior_marker),
        "required": dict(state.required),
        "optional": dict(state.optional),
        "carried": list(state.carried),
        "legacy_historical": dict(state.legacy_historical),
        "daily": dict(state.daily),
        "lease": dict(state.lease),
        "last_run": dict(state.last_run),
        "pending": None if state.pending is None else _pending_document(state.pending),
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def decode_state(text: str) -> CycleState:
    """Parse a cycle document strictly. Every refusal is a :class:`CycleError`."""
    if not isinstance(text, str):
        raise CycleError("a cycle document must be text")
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise CycleError("invalid JSON cycle document") from None
    document = _mapping(raw, "cycle document")
    version = document.get("version")
    if type(version) is not int:
        raise CycleError("the cycle version must be an integer")
    if version == 1:
        raise CycleError("a v1 curation state cannot be read as a cycle; migrate it first")
    if version != CYCLE_SCHEMA_VERSION:
        raise CycleError("unsupported cycle schema version")
    _exact_fields(document, _STATE_FIELDS, "cycle document")
    pending_raw = document["pending"]
    return CycleState(
        cycle_id=_text(document["cycle_id"], "cycle_id"),
        prior_marker=_decode_marker(document["prior_marker"]),
        required=_date_map(document["required"], "required"),
        optional=_date_map(document["optional"], "optional"),
        carried=tuple(_string_list(document["carried"], "carried")),
        legacy_historical=_date_map(document["legacy_historical"], "legacy_historical"),
        daily=_date_map(document["daily"], "daily"),
        lease=dict(_mapping(document["lease"], "lease")),
        last_run=dict(_mapping(document["last_run"], "last_run")),
        pending=None if pending_raw is None else _decode_pending(pending_raw),
    )


# ── Obligations ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Projection:
    """The work a cycle still owes. Empty tuples mean nothing is outstanding.

    ``optional_outstanding`` includes carried work from an earlier cycle.
    """

    required_outstanding: tuple[str, ...]
    optional_outstanding: tuple[str, ...]
    daily_outstanding: tuple[str, ...]

    @property
    def required_complete(self) -> bool:
        return not self.required_outstanding


def project_obligations(items: Iterable[WorklistItem], state: CycleState) -> Projection:
    """What this cycle still owes, looked up in the cycle's own evidence only.

    Required keys are always the two hygiene checks. Optional keys are the guide
    review, the rules distillation and the observation merge, classified by key
    and not by ``item.weekly``. Everything else is daily. Done evidence comes
    from ``state.required``, ``state.optional`` and ``state.daily``, never from
    ``legacy_historical``.
    """
    required_outstanding = sorted(key for key in REQUIRED_HYGIENE_KEYS if key not in state.required)
    optional_pending = set(state.carried)
    daily_pending: set[str] = set()
    for item in items:
        for key in item.keys:
            if key in REQUIRED_HYGIENE_KEYS:
                continue
            if key in (GUIDE_KEY, MERGE_KEY) or item.pass_id == PASS_RULES:
                optional_pending.add(key)
            else:
                daily_pending.add(key)
    return Projection(
        required_outstanding=tuple(required_outstanding),
        optional_outstanding=tuple(sorted(k for k in optional_pending if k not in state.optional)),
        daily_outstanding=tuple(sorted(k for k in daily_pending if k not in state.daily)),
    )


# ── Migration (dry run) ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What a v1 state would become. Nothing is written by producing it.

    ``ambiguous`` lists v1 weekly keys dated after the last marker, which may
    belong to an unfinished cycle; they are history and need fresh proof.
    ``dropped`` lists daily keys dated after ``today``, which cannot be
    attributed to any real run.
    """

    state: CycleState
    ambiguous: tuple[str, ...]
    dropped: tuple[str, ...]


def plan_migration(
    v1: CurationState,
    *,
    last_full_pass: str,
    prior_marker: MarkerIdentity,
    today: date,
) -> MigrationPlan:
    """Plan the v1 to v2 move without touching the vault.

    ``last_full_pass`` is the advisory marker read used for classification; it
    must be "" or a canonical date. ``prior_marker`` is the byte identity of the
    log the plan is made against, which is what recovery later compares with.
    The result is deterministic and independent of the order of ``done_keys``.
    """
    if last_full_pass:
        _check_date(last_full_pass, "last_full_pass")
    daily: dict[str, str] = {}
    historical: dict[str, str] = {}
    ambiguous: list[str] = []
    dropped: list[str] = []
    today_text = today.isoformat()
    for key in sorted(v1.done_keys):
        stamp = v1.done_keys[key]
        _check_key(key, "done key")
        _check_date(stamp, f"done date for {key}")
        if _is_weekly_key(key):
            historical[key] = stamp
            if not last_full_pass or stamp > last_full_pass:
                ambiguous.append(key)
        elif stamp > today_text:
            dropped.append(key)
        else:
            daily[key] = stamp
    state = CycleState(
        cycle_id=cycle_identity(prior_marker, ""),
        prior_marker=prior_marker,
        required={},
        optional={},
        carried=(),
        legacy_historical=historical,
        daily=daily,
        lease=dict(v1.lease),
        last_run=dict(v1.last_run),
        pending=None,
    )
    return MigrationPlan(state=state, ambiguous=tuple(ambiguous), dropped=tuple(dropped))


# ── Helpers ───────────────────────────────────────────────────────────────


def _is_weekly_key(key: str) -> bool:
    return key in REQUIRED_HYGIENE_KEYS or key in (GUIDE_KEY, MERGE_KEY) or key.startswith(f"{PASS_RULES}:")


def _check_cycle_id(value: object) -> None:
    if not isinstance(value, str) or not _CYCLE_ID.fullmatch(value):
        raise CycleError("a cycle id is c2- followed by 24 lowercase hex digits")


def _check_key(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CycleError(f"{label} must be a non-empty string")


def _check_date(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise CycleError(f"{label} must be a date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise CycleError(f"{label} is not a valid date") from None
    if parsed.isoformat() != value:
        raise CycleError(f"{label} is not a canonical date")


def _check_date_map(mapping: object, label: str) -> None:
    if not isinstance(mapping, dict):
        raise CycleError(f"{label} must be an object")
    for key, value in mapping.items():
        _check_key(key, f"{label} key")
        _check_date(value, f"{label} date for {key}")


def _check_lease(lease: dict[str, Any]) -> None:
    if not lease:
        return
    holder = lease.get("holder")
    if not isinstance(holder, str) or not holder.strip():
        raise CycleError("lease holder is invalid")
    for field_name in ("started_at", "expires_at"):
        value = lease.get(field_name)
        if not isinstance(value, str) or _parse_instant(value) is None:
            raise CycleError(f"lease {field_name} is invalid")


def _check_last_run(last_run: dict[str, Any]) -> None:
    if not last_run:
        return
    finished = last_run.get("finished_at")
    if (
        not isinstance(last_run.get("status"), str)
        or not isinstance(finished, str)
        or _parse_instant(finished) is None
        or any(type(last_run.get(name)) is not int for name in ("planned", "completed", "deferred"))
        or not isinstance(last_run.get("reasons"), list)
        or any(not isinstance(reason, str) for reason in last_run["reasons"])
    ):
        raise CycleError("last_run summary is invalid")


def _parse_instant(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CycleError("duplicate key in cycle document")
        result[key] = value
    return result


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CycleError(f"{label} must be an object")
    return value


def _exact_fields(value: dict[str, Any], fields: frozenset[str], label: str) -> None:
    if set(value) != fields:
        missing = sorted(fields - set(value))
        unknown = sorted(set(value) - fields)
        raise CycleError(f"{label} has missing fields {missing} or unknown fields {unknown}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CycleError(f"{label} must be a string")
    return value


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CycleError(f"{label} must be a list of strings")
    return value


def _date_map(value: object, label: str) -> dict[str, str]:
    mapping = _mapping(value, label)
    _check_date_map(mapping, label)
    return dict(mapping)


def _marker_document(marker: MarkerIdentity) -> dict[str, Any]:
    return {"kind": marker.kind, "sha256": marker.sha256}


def _decode_marker(value: object) -> MarkerIdentity:
    fields = _mapping(value, "prior_marker")
    _exact_fields(fields, _MARKER_FIELDS, "prior_marker")
    kind = fields["kind"]
    digest = fields["sha256"]
    if kind not in ("absent", "present"):
        raise CycleError("a marker is either absent or present")
    if digest is not None and not isinstance(digest, str):
        raise CycleError("a marker digest must be a string or null")
    return MarkerIdentity(kind, digest)


def _pending_document(pending: PreparedTransition) -> dict[str, Any]:
    return {
        "cycle_id": pending.cycle_id,
        "prior": _marker_document(pending.prior),
        "target_stamp": pending.target_stamp,
        "target_sha256": pending.target_sha256,
        "proven_required": list(pending.proven_required),
        "phase": pending.phase,
    }


def _decode_pending(value: object) -> PreparedTransition:
    fields = _mapping(value, "pending")
    _exact_fields(fields, _PENDING_FIELDS, "pending")
    phase = fields["phase"]
    if phase not in ("prepared", "committed"):
        raise CycleError("a transition is prepared or committed")
    return PreparedTransition(
        cycle_id=_text(fields["cycle_id"], "pending cycle_id"),
        prior=_decode_marker(fields["prior"]),
        target_stamp=_text(fields["target_stamp"], "target_stamp"),
        target_sha256=_text(fields["target_sha256"], "target_sha256"),
        proven_required=tuple(_string_list(fields["proven_required"], "proven_required")),
        phase=phase,
    )
