"""Cache-key completeness for one model judgment of a stale note (#3057), dormant.

A judgment is reusable only if the key captures everything the judgment depends
on. This module states that dependency set and checks it. It is pure: no I/O, no
clock, no environment, no provider, no scheduler. Nothing calls it yet.

The key is computed from the FRESH :class:`~ciao.curation_evidence.PreparedNote`,
so preparation always re-runs and a deferred, unselected or unreadable outcome
never produces a key. Every input the model sees, and every constant the preparer
and the validator consult, is read from its owning module at call time, so a
change to any of them changes the key by itself.

Same key means same verdict is a policy choice, not a fact about the model. The
parity checker tests the key against recorded full-path outcomes; it does not
decide whether reuse is acceptable.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from ciao import curation_evidence as ce
from ciao import curation_judgments as cj

KEY_PREFIX = "care-judgment-v1:"
#: Stands in for the per-attempt ids, so they cannot reach the prompt digest.
_NEUTRAL_ID = "-"
#: A resolved provider or model is never an alias.
_ALIASES = frozenset({"default"})

#: Names a reader might expect in the key that must not be in it, each with its reason.
EXCLUSION_REASONS: Mapping[str, str] = MappingProxyType(
    {
        "run_id": "per attempt; the reply echoes it but a validated Judgment never carries it",
        "batch_id": "per attempt; the reply echoes it but a validated Judgment never carries it",
        "timeout_s": "a transport limit, not part of what the model is asked",
        "today": "due-ness is decided by the planner on every run, never by a cache",
        "STALE_NOTE_MAX_ITEMS": "the planner's per-run cap decides what is due, not a cache",
        "NOTE_REVIEW_DAYS": "the planner's review horizon decides what is due, not a cache",
        "CHECK_COOLDOWN_DAYS": "the planner's cooldown decides what is due, not a cache",
        "generated_at": "a wall-clock stamp on an output, not an input to the judgment",
        "vault_root": "machine-local absolute path; the vault is identified by its workspace",
    }
)
EXCLUDED_FROM_KEY: frozenset[str] = frozenset(EXCLUSION_REASONS)


class IncompleteKey(ValueError):
    """A required key input is missing, empty, the wrong type or an alias. A bug, never a partial key."""


@dataclass(frozen=True, slots=True)
class Uncacheable:
    """The outcome produced no note to judge, so it has no key and is never stored."""

    reason: str


@dataclass(frozen=True, slots=True)
class JudgmentKeyInputs:
    """Every dependency of one judgment. Each field is required; none is optional."""

    schema_version: int
    role: str
    workspace: str
    prepared_digest: str
    prompt_digest: str
    limits_digest: str
    provider: str
    model: str


def canonical_digest(value: object) -> str:
    """SHA-256 of the canonical JSON of a dataclass, a tuple, a list or a plain value."""
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def canonical_payload(inputs: JudgmentKeyInputs) -> str:
    """The exact text the key is hashed from; the excluded names never appear in it."""
    return _canonical(inputs)


def key_inputs(
    prepared: ce.PreparedOutcome,
    *,
    workspace: str,
    provider: str,
    model: str,
) -> JudgmentKeyInputs | Uncacheable:
    """The dependency set of a judgment of ``prepared``, or why it has none.

    Only a :class:`~ciao.curation_evidence.PreparedNote` can be keyed. The
    workspace, provider and model are checked here, after the uncacheable
    outcomes return, so a bad caller value is caught only when a key would be built.
    """
    if isinstance(prepared, ce.NoSelectedNote):
        return Uncacheable(reason="preparation selected no note")
    if isinstance(prepared, ce.DeferredNote):
        return Uncacheable(reason=f"preparation deferred the note: {prepared.reason}")
    if not isinstance(prepared, ce.PreparedNote):
        raise TypeError("judgment keys are built only from a preparation outcome")
    _check_prepared(prepared)
    limits = _limits()
    for name, value in limits.items():
        _count(name, value)
    inputs = JudgmentKeyInputs(
        schema_version=cj.SCHEMA_VERSION,
        role=cj.ROLE,
        workspace=workspace,
        prepared_digest=canonical_digest(prepared),
        prompt_digest=_prompt_digest(prepared),
        limits_digest=canonical_digest(limits),
        provider=provider,
        model=model,
    )
    _check_inputs(inputs)
    return inputs


def judgment_key(
    prepared: ce.PreparedOutcome,
    *,
    workspace: str,
    provider: str,
    model: str,
) -> str | Uncacheable:
    """A versioned key for one judgment, or :class:`Uncacheable` when there is no note."""
    inputs = key_inputs(prepared, workspace=workspace, provider=provider, model=model)
    if isinstance(inputs, Uncacheable):
        return inputs
    return _key_of(inputs)


# ── Parity ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Record:
    """One full-path result: its key inputs, a digest of what it returned, and whether it validated."""

    label: str
    inputs: JudgmentKeyInputs | Uncacheable
    outcome_digest: str
    validated: bool


@dataclass(frozen=True, slots=True)
class Step:
    """A fixture step that declares it changed exactly one dependency, ``changed``.

    Its baseline is the record immediately before it in the replayed sequence.
    """

    label: str
    changed: str


@dataclass(frozen=True, slots=True)
class IncompleteKeyFinding:
    """Two records share a key but their full outcomes differ: the key misses a dependency."""

    first: str
    second: str


@dataclass(frozen=True, slots=True)
class StaleHitFinding:
    """A cache hit whose stored validated outcome differs from the record's own outcome."""

    label: str
    stored_label: str


@dataclass(frozen=True, slots=True)
class CachedUncertainFinding:
    """A keyed record whose outcome did not validate; it would be stored or served."""

    label: str


@dataclass(frozen=True, slots=True)
class UnkeyedChangeFinding:
    """A declared change left the key unchanged."""

    label: str
    changed: str
    baseline: str


Finding = IncompleteKeyFinding | StaleHitFinding | CachedUncertainFinding | UnkeyedChangeFinding


@dataclass(frozen=True, slots=True)
class ParityReport:
    """``ok`` is false only for a finding. Redundant misses are informational."""

    ok: bool
    findings: tuple[Finding, ...]
    hits: int
    misses: int
    redundant_misses: int


def check_parity(records: Sequence[Record], steps: Sequence[Step] = ()) -> ParityReport:
    """Check recorded full-path outcomes against their keys, in the order given.

    Findings are reported, never raised. Caller errors (duplicate labels, a step
    that names no record, a step with no dependency) raise ``ValueError``.
    """
    labels = [record.label for record in records]
    if len(set(labels)) != len(labels):
        raise ValueError("record labels must be unique")
    position = {label: at for at, label in enumerate(labels)}
    for step in steps:
        if step.label not in position:
            raise ValueError("a step names no record")
        if type(step.changed) is not str or not step.changed.strip():
            raise ValueError("a step must name the dependency it changes")

    keys: list[str | None] = [
        _key_of(record.inputs) if isinstance(record.inputs, JudgmentKeyInputs) else None
        for record in records
    ]
    findings: list[Finding] = []
    for at, first in enumerate(records):
        for later in range(at + 1, len(records)):
            second = records[later]
            if (
                keys[at] is not None
                and keys[at] == keys[later]
                and first.outcome_digest != second.outcome_digest
            ):
                findings.append(IncompleteKeyFinding(first=first.label, second=second.label))

    # The cache stores only validated outcomes, keyed by the first one seen.
    stored: dict[str, Record] = {}
    stored_keys_by_digest: dict[str, set[str]] = {}
    hits = misses = redundant = 0
    for record, key in zip(records, keys):
        if key is None:
            continue
        held = stored.get(key)
        if held is not None:
            hits += 1
            if held.outcome_digest != record.outcome_digest:
                findings.append(StaleHitFinding(label=record.label, stored_label=held.label))
            continue
        misses += 1
        if record.validated:
            if any(other != key for other in stored_keys_by_digest.get(record.outcome_digest, ())):
                redundant += 1
            stored[key] = record
            stored_keys_by_digest.setdefault(record.outcome_digest, set()).add(key)

    for record, key in zip(records, keys):
        if key is not None and not record.validated:
            findings.append(CachedUncertainFinding(label=record.label))

    for step in steps:
        at = position[step.label]
        if at == 0:
            continue
        if keys[at] is not None and keys[at] == keys[at - 1]:
            findings.append(
                UnkeyedChangeFinding(
                    label=step.label, changed=step.changed, baseline=labels[at - 1]
                )
            )

    return ParityReport(
        ok=not findings,
        findings=tuple(findings),
        hits=hits,
        misses=misses,
        redundant_misses=redundant,
    )


# ── Internals ──────────────────────────────────────────────────────────────


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, dict):
        if not all(type(key) is str for key in value):
            raise TypeError("digest mappings must have string keys")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    raise TypeError(f"cannot digest a {type(value).__name__}")


def _canonical(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _limits() -> dict[str, int]:
    """Every constant the preparer or the validator consults, read from its module now."""
    return {
        "curation_judgments.MAX_JUDGMENT_BYTES": cj.MAX_JUDGMENT_BYTES,
        "curation_judgments.MAX_REASON_CHARS": cj.MAX_REASON_CHARS,
        "curation_judgments.MAX_SECTION_CHARS": cj.MAX_SECTION_CHARS,
        "curation_judgments.MAX_CHANGE_CHARS": cj.MAX_CHANGE_CHARS,
        "curation_judgments.MAX_PROPOSAL_REF_CHARS": cj.MAX_PROPOSAL_REF_CHARS,
        "curation_judgments.MAX_REFERENCES": cj.MAX_REFERENCES,
        "curation_evidence.MAX_NOTE_BYTES": ce.MAX_NOTE_BYTES,
        "curation_evidence.MAX_QUEUE_BYTES": ce.MAX_QUEUE_BYTES,
        "curation_evidence.MAX_SIDECARS": ce.MAX_SIDECARS,
        "curation_evidence.MAX_SIDECAR_BYTES": ce.MAX_SIDECAR_BYTES,
        "curation_evidence.MAX_STORE_BYTES": ce.MAX_STORE_BYTES,
        "curation_evidence.MAX_REFERENCES": ce.MAX_REFERENCES,
    }


def _prompt_digest(prepared: ce.PreparedNote) -> str:
    """The exact model-visible bytes: the system prompt and the user prompt with neutral ids."""
    user = cj._build_prompt(prepared, run_id=_NEUTRAL_ID, batch_id=_NEUTRAL_ID)
    return canonical_digest({"system": cj._SYSTEM_PROMPT, "user": user})


def _text(name: str, value: object, *, empty_ok: bool = False) -> None:
    if type(value) is not str:
        raise IncompleteKey(f"{name} is not a string")
    if not empty_ok and not value.strip():
        raise IncompleteKey(f"{name} is empty")


def _count(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise IncompleteKey(f"{name} is not a count")


def _check_prepared(prepared: ce.PreparedNote) -> None:
    _text("item_key", prepared.item_key)
    _text("relative_path", prepared.relative_path)
    _text("revision", prepared.revision)
    if type(prepared.references) not in (tuple, list):
        raise IncompleteKey("references is not a sequence")
    for ref in prepared.references:
        if type(ref) is not ce.ProposalReference:
            raise IncompleteKey("a reference is not a proposal reference")
        _text("sidecar_id", ref.sidecar_id)
        _text("status", ref.status)
        _text("reference revision", ref.revision)
        _text("operation", ref.operation)
        _text("proposal_id", ref.proposal_id, empty_ok=True)
        _text("expected_revision", ref.expected_revision, empty_ok=True)
        _text("settled_at", ref.settled_at, empty_ok=True)
        if type(ref.accepted) is not bool:
            raise IncompleteKey("accepted is not a boolean")


def _check_inputs(inputs: JudgmentKeyInputs) -> None:
    _count("schema_version", inputs.schema_version)
    _text("role", inputs.role)
    _text("workspace", inputs.workspace)
    _text("prepared_digest", inputs.prepared_digest)
    _text("prompt_digest", inputs.prompt_digest)
    _text("limits_digest", inputs.limits_digest)
    for name, value in (("provider", inputs.provider), ("model", inputs.model)):
        _text(name, value)
        if value.strip() in _ALIASES:
            raise IncompleteKey(f"{name} is an alias, not a resolved value")


def _key_of(inputs: JudgmentKeyInputs) -> str:
    _check_inputs(inputs)
    return KEY_PREFIX + hashlib.sha256(canonical_payload(inputs).encode("ascii")).hexdigest()
