"""Shadow verdicts for region proposals and the handover gate.

The nightly Workspace care run records what it would do with a queued
``[memory]``/``[profile]`` row without doing it. The verdicts live in an
append-only sidecar beside the queue, keyed by ``(kind, one-line text)`` rather
than ``stable_proposal_id``: the id mixes in the queue path, the source and the
duplicate ordinal, so removing an earlier duplicate renumbers a later row, and
the CLI decision writers record no id at all. The text is what every decision
writer already records.

This module joins a verdict onto the History row the operator's decision made,
measures how often the two agree, and says whether a workspace's handover
setting lets the run act on a verdict. Nothing here changes the setting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.config import MEMORY_HANDOVERS

REGION_KINDS: frozenset[str] = frozenset({"memory", "profile"})
VERDICTS: tuple[str, ...] = ("accept", "dismiss")
PREDICTIONS_SUFFIX = ".predictions.jsonl"
REASON_MAX_CHARS = 200
AGREEMENT_WINDOW = 50
SUGGEST_MIN_DECISIONS = 20
SUGGEST_MIN_RATE = 0.9
#: Most facts the agent may accept into the always-loaded regions in one curation run.
HANDOVER_ACCEPT_CAP = 5


def _one_line(value: str) -> str:
    return " ".join(value.split())


def predictions_path(proposals_path: Path) -> Path:
    """Sidecar holding the shadow verdicts, next to the queue it describes."""
    return proposals_path.with_suffix(PREDICTIONS_SUFFIX)


def prediction_key(kind: str, text: str) -> str:
    return f"{kind.lower()}\x00{_one_line(text)}"


def read_predictions(proposals_path: Path) -> list[dict[str, str]]:
    """Every stored prediction, oldest first. Unreadable lines are skipped."""
    path = predictions_path(proposals_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        rows.append({str(k): str(v) for k, v in entry.items()})
    return rows


def _row_key(row: dict[str, str]) -> str:
    return prediction_key(row.get("kind", ""), row.get("text", ""))


def _decision_times(proposals_path: Path) -> dict[str, list[str]]:
    """Every dated region decision's timestamp, per ``(kind, text)`` key."""
    from ciao import memory_proposals  # noqa: PLC0415

    times: dict[str, list[str]] = {}
    for decision in memory_proposals.read_decisions(proposals_path):
        kind = str(decision.get("kind", "")).lower()
        ts = str(decision.get("ts", ""))
        if kind in REGION_KINDS and ts:
            times.setdefault(prediction_key(kind, str(decision["text"])), []).append(ts)
    return times


def latest_predictions(proposals_path: Path) -> dict[str, dict[str, str]]:
    """The newest *live* prediction per ``(kind, text)`` key.

    A prediction is void once the operator (or anyone) has decided the same fact
    after it was made: a fact dismissed and later re-queued is a new question,
    and the verdict recorded for the old one must not authorise acting on it.
    The sidecar keeps the row; it just stops counting.
    """
    latest: dict[str, dict[str, str]] = {}
    for row in read_predictions(proposals_path):
        latest[_row_key(row)] = row
    if not latest:
        return latest
    decided = _decision_times(proposals_path)
    return {
        key: row
        for key, row in latest.items()
        if not any(ts > row.get("predicted_at", "") for ts in decided.get(key, ()))
    }


def count_agent_accepts_since(proposals_path: Path, since: datetime) -> int:
    """Agent-made accepts recorded in the decision sidecar at or after ``since``."""
    from ciao import memory_proposals  # noqa: PLC0415

    count = 0
    for decision in memory_proposals.read_decisions(proposals_path):
        if decision.get("via") != "agent" or decision.get("action") != "accepted":
            continue
        try:
            decided_at = datetime.fromisoformat(str(decision.get("ts", "")))
        except ValueError:
            continue
        if decided_at.tzinfo is not None and decided_at >= since:
            count += 1
    return count


def record_prediction(
    proposals_path: Path,
    *,
    kind: str,
    text: str,
    verdict: str,
    reason: str,
) -> bool:
    """Append a prediction unless the latest one for the row says the same."""
    kind = kind.lower()
    if kind not in REGION_KINDS:
        raise ValueError("predictions are only recorded for [memory] and [profile] rows")
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of: {', '.join(VERDICTS)}")
    flat_text = _one_line(text)
    if not flat_text:
        raise ValueError("prediction text is empty")
    flat_reason = _one_line(reason)[:REASON_MAX_CHARS].strip()
    if not flat_reason:
        raise ValueError("prediction reason is empty")
    latest = latest_predictions(proposals_path).get(prediction_key(kind, flat_text))
    if latest is not None and latest.get("verdict") == verdict and latest.get("reason") == flat_reason:
        return False
    row = {
        "predicted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": flat_text,
        "verdict": verdict,
        "reason": flat_reason,
    }
    path = predictions_path(proposals_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def handover_allows(handover: str, verdict: str) -> bool:
    """Whether a workspace's handover setting lets the run act on ``verdict``."""
    if handover not in MEMORY_HANDOVERS:
        return False
    if handover == "dismiss":
        return verdict == "dismiss"
    if handover == "all":
        return verdict in VERDICTS
    return False


def attach_predictions(proposals_path: Path, rows: list[dict[str, Any]]) -> None:
    """Join the prediction that preceded each decision onto History-shaped rows.

    A row gets ``prediction`` only for a region kind with a dated decision and a
    prediction for the same ``(kind, text)`` made at or before that decision and
    after the previous decision on the same fact, so a re-queued fact never
    inherits the verdict recorded for its earlier life.
    """
    by_key: dict[str, list[dict[str, str]]] = {}
    for entry in read_predictions(proposals_path):
        by_key.setdefault(_row_key(entry), []).append(entry)
    if not by_key:
        return
    decided = _decision_times(proposals_path)
    for row in rows:
        row_ts = str(row.get("ts", ""))
        if row_ts:
            decided.setdefault(
                prediction_key(str(row.get("kind", "")), str(row.get("text", ""))), []
            ).append(row_ts)
    for row in rows:
        kind = str(row.get("kind", "")).lower()
        ts = str(row.get("ts", ""))
        if kind not in REGION_KINDS or not ts:
            continue
        key = prediction_key(kind, str(row.get("text", "")))
        # The previous decision on this fact, from the sidecar and the rows in
        # hand (a filtered History page may not carry it).
        earlier = [t for t in decided.get(key, ()) if t < ts]
        floor = max(earlier, default="")
        chosen: dict[str, str] | None = None
        for entry in by_key.get(key, []):
            predicted_at = entry.get("predicted_at", "")
            if predicted_at and floor < predicted_at <= ts:
                chosen = entry
        if chosen is None:
            continue
        verdict = chosen.get("verdict", "")
        row["prediction"] = {
            "verdict": verdict,
            "reason": chosen.get("reason", ""),
            "ts": chosen.get("predicted_at", ""),
            "match": (verdict == "accept") == (row.get("action") == "accepted"),
        }


def agreement(rows: list[dict[str, Any]], *, handover: str) -> dict[str, Any]:
    """Agreement between predictions and the operator over the newest decisions.

    ``rows`` must already have been through :func:`attach_predictions`. Agent
    and auto decisions are not operator judgement and a sweep is expiry, so
    none of them count.
    """
    eligible = [
        row
        for row in rows
        if str(row.get("kind", "")).lower() in REGION_KINDS
        and row.get("action") in ("accepted", "dismissed")
        and row.get("via") == "pwa"
        and row.get("outcome") != "swept"
        and isinstance(row.get("prediction"), dict)
    ]
    eligible.sort(key=lambda row: str(row.get("ts", "")))
    window = eligible[-AGREEMENT_WINDOW:]
    decided = len(window)
    matched = sum(1 for row in window if row["prediction"]["match"])
    accepts = [row for row in window if row["prediction"]["verdict"] == "accept"]
    dismisses = [row for row in window if row["prediction"]["verdict"] == "dismiss"]
    rate = matched / decided if decided else None
    return {
        "decided": decided,
        "matched": matched,
        "rate": rate,
        "window": AGREEMENT_WINDOW,
        "min_decisions": SUGGEST_MIN_DECISIONS,
        "threshold": SUGGEST_MIN_RATE,
        "predicted_accept": len(accepts),
        "accept_matched": sum(1 for row in accepts if row["prediction"]["match"]),
        "predicted_dismiss": len(dismisses),
        "dismiss_matched": sum(1 for row in dismisses if row["prediction"]["match"]),
        "suggest_handover": (
            handover == "off"
            and decided >= SUGGEST_MIN_DECISIONS
            and rate is not None
            and rate >= SUGGEST_MIN_RATE
        ),
    }


def agreement_for_queue(proposals_path: Path, *, handover: str) -> dict[str, Any]:
    from ciao import memory_proposals  # noqa: PLC0415

    rows = memory_proposals.read_decisions(proposals_path)
    attach_predictions(proposals_path, rows)
    return agreement(rows, handover=handover)
