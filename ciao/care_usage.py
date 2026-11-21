"""Attributable model-call usage for workspace care, as a pure typed projection.

``collect`` takes a trusted expected-call inventory and typed observations keyed
by run, worker and stable call id, and returns per-partition and run-wide
coverage in the shape of ``task_eval_contract``'s metrics. It never folds an
absent counter into zero: an observed zero is complete zero, anything missing,
unattributed, conflicting or unbound is ``unknown`` or ``partial``.

This is not ``task_eval.collect_usage``, which sums per-turn aggregates. Provider
turn or session totals do not describe individual attempted calls, so this module
accepts only per-attempt observations and writes no provider adapter. Nothing
here reads files, calls a provider or claims a saving; equivalence is decided by
``task_eval_contract.comparison_eligibility``, not by this projection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from ciao.task_eval_contract import (
    MAX_JSON_NUMBER,
    Metric,
    MetricReason,
    MetricSource,
    MetricStatus,
)

Partition = Literal["archive_capture", "care_worker", "skill_marginal", "narrator_retry"]
PARTITIONS: tuple[Partition, ...] = ("archive_capture", "care_worker", "skill_marginal", "narrator_retry")
TOKEN_METRICS = ("input", "output", "cache_read", "cache_write")
METRICS = (*TOKEN_METRICS, "model_calls", "cost_usd", "latency_ms")

MAX_EXPECTED_CALLS = 10_000
MAX_OBSERVATIONS = 100_000
MAX_ATTEMPT = 1_000
MAX_LATENCY_MS = 86_400_000
MAX_COST_USD = 1_000_000.0

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}\Z")
_OUTCOMES = ("completed", "failed")
# When several reasons apply to one metric, the most severe one is reported.
_REASON_RANK: dict[str, int] = {"collector_error": 4, "incomplete_coverage": 3, "missing_usage": 2, "not_collected": 1}
_SOURCE: dict[str, MetricSource] = {"latency_ms": "monotonic_clock"}


def _id(value: object, code: str = "invalid_id") -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ValueError(code)
    return value


def _count(value: object, *, limit: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > limit:
        raise ValueError("invalid_usage")
    return value


def _cost(value: object) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
        raise ValueError("invalid_usage")
    number = float(cast(float, value))
    if number < 0 or number > MAX_COST_USD:
        raise ValueError("invalid_usage")
    return number


@dataclass(frozen=True)
class ExpectedCall:
    """One call the trusted inventory says the run should make."""

    run_id: str
    worker_id: str
    call_id: str
    partition: Partition
    provider: str
    model: str

    def __post_init__(self) -> None:
        _id(self.run_id)
        _id(self.worker_id)
        _id(self.call_id)
        if self.partition not in PARTITIONS:
            raise ValueError("invalid_partition")
        _id(self.provider, "invalid_provider")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise ValueError("invalid_model")


@dataclass(frozen=True)
class Usage:
    """Counters one attempt reported. ``None`` means not reported; ``0`` means observed zero."""

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        for name in TOKEN_METRICS:
            _count(getattr(self, name), limit=MAX_JSON_NUMBER)
        _count(self.latency_ms, limit=MAX_LATENCY_MS)
        object.__setattr__(self, "cost_usd", _cost(self.cost_usd))


@dataclass(frozen=True)
class Observation:
    """One attempt at one expected call, as telemetry reported it."""

    run_id: str
    worker_id: str
    call_id: str
    partition: Partition
    provider: str
    model: str
    attempt: int
    outcome: Literal["completed", "failed"]
    usage: Usage | None

    def __post_init__(self) -> None:
        _id(self.run_id)
        _id(self.worker_id)
        _id(self.call_id)
        if self.partition not in PARTITIONS:
            raise ValueError("invalid_partition")
        _id(self.provider, "invalid_provider")
        if type(self.model) is not str or _MODEL.fullmatch(self.model) is None:
            raise ValueError("invalid_model")
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_ATTEMPT:
            raise ValueError("invalid_attempt")
        if self.outcome not in _OUTCOMES:
            raise ValueError("invalid_outcome")
        if self.usage is not None and type(self.usage) is not Usage:
            raise ValueError("invalid_usage")


@dataclass(frozen=True)
class Inventory:
    """Expected calls. ``proven`` asserts the list is the run's complete set of calls."""

    calls: tuple[ExpectedCall, ...]
    proven: bool

    def __post_init__(self) -> None:
        if type(self.calls) is not tuple or any(type(c) is not ExpectedCall for c in self.calls):
            raise ValueError("invalid_inventory")
        if len(self.calls) > MAX_EXPECTED_CALLS:
            raise ValueError("too_many_expected_calls")
        if type(self.proven) is not bool:
            raise ValueError("invalid_inventory")
        keys = [(c.run_id, c.worker_id, c.call_id) for c in self.calls]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate_expected_call")


@dataclass(frozen=True)
class Coverage:
    """One metric over one scope, in the contract's status/source/evidence/reason shape."""

    value: int | float | None
    status: MetricStatus
    source: MetricSource
    evidence_sha256: str | None
    reason: MetricReason | None

    def __post_init__(self) -> None:
        if self.status == "complete":
            valid = (self.value is not None and self.evidence_sha256 is not None
                     and self.reason is None and self.source != "none")
        elif self.status == "partial":
            valid = (self.value is not None and self.evidence_sha256 is not None
                     and self.reason is not None)
        else:
            valid = (self.value is None and self.evidence_sha256 is None
                     and self.reason is not None and self.source == "none")
        if not valid:
            raise ValueError("invalid_coverage")


@dataclass(frozen=True)
class CareUsageReport:
    """Coverage per partition and for the whole run, with bound counts and no identifiers."""

    inventory_sha256: str
    proven: bool
    partitions: dict[str, dict[str, Coverage]]
    run: dict[str, Coverage]
    failed_attempts: int
    unbound: int
    mismatched: int
    conflicts: int
    collapsed: int


def _digest(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _inventory_digest(inventory: Inventory) -> str:
    rows = sorted([c.run_id, c.worker_id, c.call_id, c.partition, c.provider, c.model] for c in inventory.calls)
    return _digest(["ciao-care-usage-inventory/1", inventory.proven, rows])


def _mismatch(item: Observation, call: ExpectedCall) -> bool:
    return item.partition != call.partition or item.provider != call.provider or item.model != call.model


def _call_value(metric: str, attempts: dict[int, Observation]) -> tuple[int | float | None, MetricReason | None]:
    if metric == "model_calls":
        return len(attempts), None
    values: list[int | float] = []
    for item in attempts.values():
        usage = item.usage
        reported = None if usage is None else getattr(usage, metric)
        if reported is None:
            return None, "missing_usage"
        values.append(reported)
    total = sum(values)
    if total > MAX_JSON_NUMBER:
        return None, "collector_error"
    return total, None


def _worst(reasons: list[MetricReason]) -> MetricReason | None:
    if not reasons:
        return None
    return max(reasons, key=lambda reason: _REASON_RANK.get(reason, 0))


def _aggregate(metric: str, members: list[ExpectedCall], results: dict[tuple[str, str, str], dict[str, tuple[int | float | None, MetricReason | None]]],
               *, proven: bool, unbound: bool, inventory_sha: str, label: str) -> Coverage:
    source: MetricSource = _SOURCE.get(metric, "provider_usage")
    contributions: list[list[object]] = []
    reasons: list[MetricReason] = []
    total: int | float = 0.0 if metric == "cost_usd" else 0
    for member in members:
        key = (member.run_id, member.worker_id, member.call_id)
        value, reason = results[key][metric]
        if value is None:
            reasons.append(reason or "not_collected")
        else:
            contributions.append([member.run_id, member.worker_id, member.call_id, value])
            total += value
    incomplete = unbound or not proven or "incomplete_coverage" in reasons
    if not members and proven and not unbound:
        zero: int | float = 0.0 if metric == "cost_usd" else 0
        return Coverage(zero, "complete", source, _digest([metric, label, inventory_sha, []]), None)
    if total > MAX_JSON_NUMBER:
        return Coverage(None, "unknown", "none", None, "collector_error")
    pending: list[MetricReason] = list(reasons)
    if incomplete:
        pending.append("incomplete_coverage")
    reason = _worst(pending)
    if not contributions:
        return Coverage(None, "unknown", "none", None, reason or "not_collected")
    evidence = _digest([metric, label, inventory_sha, sorted(contributions)])
    if reason is None:
        return Coverage(total, "complete", source, evidence, None)
    return Coverage(total, "partial", source, evidence, reason)


def collect(inventory: Inventory, observations: Sequence[Observation]) -> CareUsageReport:
    if type(inventory) is not Inventory:
        raise ValueError("invalid_inventory")
    rows = tuple(observations)
    if len(rows) > MAX_OBSERVATIONS:
        raise ValueError("too_many_observations")
    if any(type(item) is not Observation for item in rows):
        raise ValueError("invalid_observation")

    expected = {(c.run_id, c.worker_id, c.call_id): c for c in inventory.calls}
    groups: dict[tuple[str, str, str, int], list[Observation]] = {}
    for item in rows:
        groups.setdefault((item.run_id, item.worker_id, item.call_id, item.attempt), []).append(item)

    collapsed = unbound = mismatched = conflicts = 0
    accepted: dict[tuple[str, str, str], dict[int, Observation]] = {}
    tainted: set[tuple[str, str, str]] = set()
    for (run_id, worker_id, call_id, attempt), group in groups.items():
        distinct = list(dict.fromkeys(group))
        collapsed += len(group) - len(distinct)
        key = (run_id, worker_id, call_id)
        call = expected.get(key)
        if call is None:
            unbound += 1
            continue
        if any(_mismatch(item, call) for item in distinct):
            mismatched += 1
            tainted.add(key)
            continue
        if len(distinct) > 1:
            conflicts += 1
            tainted.add(key)
            continue
        accepted.setdefault(key, {})[attempt] = distinct[0]

    results: dict[tuple[str, str, str], dict[str, tuple[int | float | None, MetricReason | None]]] = {}
    for key in expected:
        attempts = accepted.get(key, {})
        blocked: MetricReason | None = None
        if key in tainted or (attempts and set(attempts) != set(range(1, max(attempts) + 1))):
            blocked = "incomplete_coverage"
        elif not attempts:
            blocked = "not_collected"
        results[key] = {
            metric: (None, blocked) if blocked else _call_value(metric, attempts)
            for metric in METRICS
        }

    inventory_sha = _inventory_digest(inventory)
    proven = inventory.proven
    unbound_seen = unbound > 0
    partitions: dict[str, dict[str, Coverage]] = {
        partition: {
            metric: _aggregate(metric, [c for c in inventory.calls if c.partition == partition], results,
                               proven=proven, unbound=unbound_seen, inventory_sha=inventory_sha,
                               label=f"partition:{partition}")
            for metric in METRICS
        }
        for partition in PARTITIONS
    }
    run = {
        metric: _aggregate(metric, list(inventory.calls), results, proven=proven, unbound=unbound_seen,
                           inventory_sha=inventory_sha, label="run")
        for metric in METRICS
    }
    failed = sum(1 for attempts in accepted.values() for item in attempts.values() if item.outcome == "failed")
    return CareUsageReport(
        inventory_sha256=inventory_sha, proven=proven, partitions=partitions, run=run,
        failed_attempts=failed, unbound=unbound, mismatched=mismatched, conflicts=conflicts,
        collapsed=collapsed,
    )


def contract_metrics(report: CareUsageReport) -> dict[str, Metric]:
    """The run-wide coverage as ``task_eval_contract`` metrics.

    Latency is not a contract metric and is omitted; the contract's wall time
    needs a run-scoped monotonic clock this projection never receives, so it is
    always unknown here.
    """
    metrics: dict[str, Metric] = {}
    for name in ("input", "output", "cache_read", "cache_write", "model_calls", "cost_usd"):
        cov = report.run[name]
        if cov.value is None:
            metrics[name] = cast(Metric, {"value": None, "status": "unknown", "scope": "all_model_calls",
                                          "source": "none", "evidence_sha256": None, "reason": cov.reason})
        else:
            metrics[name] = cast(Metric, {"value": cov.value, "status": cov.status, "scope": "all_model_calls",
                                          "source": cov.source, "evidence_sha256": cov.evidence_sha256,
                                          "reason": cov.reason})
    metrics["wall_s"] = cast(Metric, {"value": None, "status": "unknown", "scope": "run", "source": "none",
                                      "evidence_sha256": None, "reason": "not_collected"})
    return metrics
