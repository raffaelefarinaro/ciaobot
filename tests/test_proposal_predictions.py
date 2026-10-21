"""Shadow verdicts for region proposals: store, join, agreement and handover gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import memory_proposals, proposal_predictions as pp


def _queue(tmp_path: Path) -> Path:
    path = tmp_path / "Workspace" / "Memory-Proposals.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Memory Proposals\n", encoding="utf-8")
    return path


def _write_predictions(queue: Path, rows: list[dict[str, str]]) -> None:
    sidecar = pp.predictions_path(queue)
    sidecar.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _prediction(ts: str, text: str, verdict: str, kind: str = "memory") -> dict[str, str]:
    return {
        "predicted_at": ts,
        "kind": kind,
        "text": text,
        "verdict": verdict,
        "reason": "why",
    }


def _decision(
    ts: str,
    text: str,
    action: str,
    *,
    kind: str = "memory",
    via: str = "pwa",
    outcome: str = "",
) -> dict[str, str]:
    return {
        "ts": ts,
        "action": action,
        "kind": kind,
        "text": text,
        "via": via,
        "outcome": outcome,
    }


def test_prediction_key_flattens_whitespace_and_lowercases_kind() -> None:
    assert pp.prediction_key("Memory", "a  b") == pp.prediction_key("memory", "a b")


def test_record_prediction_appends_json_line_beside_the_queue(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    assert pp.record_prediction(
        queue, kind="memory", text="Prefers tea", verdict="accept", reason="stable"
    )
    sidecar = pp.predictions_path(queue)
    assert sidecar.name == "Memory-Proposals.predictions.jsonl"
    assert sidecar.parent == queue.parent
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["kind"] == "memory"
    assert rows[0]["text"] == "Prefers tea"
    assert rows[0]["verdict"] == "accept"
    assert rows[0]["reason"] == "stable"
    assert rows[0]["predicted_at"]


def test_record_prediction_is_a_noop_for_an_identical_latest_row(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    kwargs = dict(kind="memory", text="x", verdict="dismiss", reason="noise")
    assert pp.record_prediction(queue, **kwargs) is True
    assert pp.record_prediction(queue, **kwargs) is False
    lines = pp.predictions_path(queue).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_a_changed_verdict_appends_and_latest_wins(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    pp.record_prediction(queue, kind="memory", text="x", verdict="dismiss", reason="a")
    pp.record_prediction(queue, kind="memory", text="x", verdict="accept", reason="a")
    latest = pp.latest_predictions(queue)
    assert latest[pp.prediction_key("memory", "x")]["verdict"] == "accept"
    assert [row["verdict"] for row in pp.read_predictions(queue)] == ["dismiss", "accept"]


def test_record_prediction_rejects_bad_kind_verdict_and_empty_reason(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="people", text="x", verdict="accept", reason="r")
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="memory", text="x", verdict="maybe", reason="r")
    with pytest.raises(ValueError):
        pp.record_prediction(queue, kind="memory", text="x", verdict="accept", reason="  ")
    pp.record_prediction(
        queue, kind="memory", text="x", verdict="accept", reason="a\nb" + "z" * 400
    )
    row = pp.read_predictions(queue)[0]
    assert "\n" not in row["reason"]
    assert len(row["reason"]) == pp.REASON_MAX_CHARS


def test_read_predictions_skips_garbage_lines(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    sidecar = pp.predictions_path(queue)
    sidecar.write_text(
        "not json\n[1, 2]\n"
        + json.dumps(_prediction("2026-01-01T00:00:00+00:00", "x", "accept"))
        + "\n",
        encoding="utf-8",
    )
    assert len(pp.read_predictions(queue)) == 1


def test_attach_predictions_joins_latest_prediction_not_after_the_decision(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "dismiss"),
            _prediction("2026-01-01T00:00:02+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:05+00:00", "x", "dismiss"),
        ],
    )
    rows = [_decision("2026-01-01T00:00:03+00:00", "x", "accepted")]
    pp.attach_predictions(queue, rows)
    assert rows[0]["prediction"] == {
        "verdict": "accept",
        "reason": "why",
        "ts": "2026-01-01T00:00:02+00:00",
        "match": True,
    }


def test_attach_predictions_skips_undated_and_non_region_rows(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:01+00:00", "p", "accept", kind="memory"),
        ],
    )
    rows = [
        _decision("", "x", "accepted"),
        _decision("2026-01-01T00:00:03+00:00", "p", "accepted", kind="people"),
        _decision("2026-01-01T00:00:03+00:00", "none", "accepted"),
    ]
    pp.attach_predictions(queue, rows)
    assert all("prediction" not in row for row in rows)


def test_attach_predictions_match_is_false_on_disagreement(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "a", "accept"),
            _prediction("2026-01-01T00:00:01+00:00", "b", "dismiss"),
        ],
    )
    rows = [
        _decision("2026-01-01T00:00:03+00:00", "a", "dismissed"),
        _decision("2026-01-01T00:00:03+00:00", "b", "dismissed"),
    ]
    pp.attach_predictions(queue, rows)
    assert rows[0]["prediction"]["match"] is False
    assert rows[1]["prediction"]["match"] is True


def _joined(
    n: int,
    *,
    matching: int | None = None,
    start: int = 0,
    **overrides: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    matching = n if matching is None else matching
    for i in range(n):
        row: dict[str, object] = {
            "ts": f"2026-01-01T00:{(start + i) // 60:02d}:{(start + i) % 60:02d}+00:00",
            "action": "dismissed",
            "kind": "memory",
            "text": f"t{start + i}",
            "via": "pwa",
            "outcome": "",
            "prediction": {
                "verdict": "dismiss" if i < matching else "accept",
                "reason": "r",
                "ts": "x",
                "match": i < matching,
            },
        }
        row.update(overrides)
        rows.append(row)
    return rows


def test_agreement_counts_only_operator_region_decisions() -> None:
    rows = _joined(2)
    rows += _joined(1, via="agent", start=10)
    rows += _joined(1, via="auto", start=11)
    rows += _joined(1, outcome="swept", start=12)
    rows += _joined(1, kind="people", start=13)
    rows += [
        {
            "ts": "2026-01-01T01:00:00+00:00",
            "action": "dismissed",
            "kind": "memory",
            "text": "no prediction",
            "via": "pwa",
            "outcome": "",
        }
    ]
    result = pp.agreement(rows, handover="off")
    assert result["decided"] == 2
    assert result["matched"] == 2


def test_agreement_uses_the_newest_fifty() -> None:
    old = _joined(10, matching=0, start=0)
    new = _joined(50, start=100)
    result = pp.agreement(old + new, handover="off")
    assert result["decided"] == 50
    assert result["rate"] == 1.0
    assert result["window"] == 50


def test_agreement_rate_is_none_with_no_decisions() -> None:
    result = pp.agreement([], handover="off")
    assert result["decided"] == 0
    assert result["rate"] is None
    assert result["suggest_handover"] is False


def test_suggest_handover_needs_twenty_decisions_ninety_percent_and_off() -> None:
    assert pp.agreement(_joined(19), handover="off")["suggest_handover"] is False
    assert pp.agreement(_joined(20, matching=17), handover="off")["suggest_handover"] is False
    assert pp.agreement(_joined(20, matching=18), handover="off")["suggest_handover"] is True
    assert pp.agreement(_joined(20), handover="dismiss")["suggest_handover"] is False


def test_agreement_reports_per_verdict_breakdown() -> None:
    rows = _joined(4, matching=3)  # 3 predicted dismiss+matched, 1 predicted accept+missed
    result = pp.agreement(rows, handover="off")
    assert result["predicted_dismiss"] == 3
    assert result["dismiss_matched"] == 3
    assert result["predicted_accept"] == 1
    assert result["accept_matched"] == 0


@pytest.mark.parametrize(
    ("handover", "verdict", "expected"),
    [
        ("off", "accept", False),
        ("off", "dismiss", False),
        ("dismiss", "accept", False),
        ("dismiss", "dismiss", True),
        ("all", "accept", True),
        ("all", "dismiss", True),
    ],
)
def test_handover_allows_matrix(handover: str, verdict: str, expected: bool) -> None:
    assert pp.handover_allows(handover, verdict) is expected


def test_agreement_for_queue_reads_the_decision_sidecar(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for i in range(20):
        text = f"fact {i}"
        pp.record_prediction(queue, kind="memory", text=text, verdict="accept", reason="r")
        if i < 18:
            memory_proposals.record_promotion(
                queue, text=text, kind="memory", via="pwa", outcome="written"
            )
        else:
            memory_proposals.record_dismissal(
                queue, text=text, kind="memory", via="pwa"
            )
    result = pp.agreement_for_queue(queue, handover="off")
    assert result["decided"] == 20
    assert result["matched"] == 18
    assert result["rate"] == 0.9
    assert result["suggest_handover"] is True


def _write_decisions(queue: Path, rows: list[tuple[str, str, str]]) -> None:
    """Hand-written decision sidecar rows: ``(ts, action, text)``."""
    from ciao.memory_proposals import dismissed_log_path

    log = dismissed_log_path(queue)
    key = {"accepted": "promoted_at", "dismissed": "dismissed_at"}
    log.write_text(
        "".join(
            json.dumps({key[action]: ts, "kind": "memory", "text": text, "via": "pwa"}) + "\n"
            for ts, action, text in rows
        ),
        encoding="utf-8",
    )


def test_a_prediction_older_than_a_decision_on_the_same_fact_is_not_live(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:01+00:00", "x", "accept")])
    _write_decisions(queue, [("2026-01-01T00:00:05+00:00", "dismissed", "x")])

    assert pp.latest_predictions(queue) == {}
    # The raw log keeps it: nothing is ever deleted.
    assert len(pp.read_predictions(queue)) == 1


def test_a_prediction_made_after_the_decision_is_live(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:09+00:00", "x", "accept")])
    _write_decisions(queue, [("2026-01-01T00:00:05+00:00", "dismissed", "x")])

    assert pp.prediction_key("memory", "x") in pp.latest_predictions(queue)


def test_history_joins_only_a_prediction_made_after_the_previous_decision(
    tmp_path: Path,
) -> None:
    queue = _queue(tmp_path)
    _write_predictions(
        queue,
        [
            _prediction("2026-01-01T00:00:01+00:00", "x", "accept"),
            _prediction("2026-01-01T00:00:20+00:00", "x", "dismiss"),
        ],
    )
    _write_decisions(
        queue,
        [
            ("2026-01-01T00:00:10+00:00", "dismissed", "x"),
            ("2026-01-01T00:00:30+00:00", "dismissed", "x"),
        ],
    )
    rows = [
        _decision("2026-01-01T00:00:10+00:00", "x", "dismissed"),
        _decision("2026-01-01T00:00:30+00:00", "x", "dismissed"),
    ]

    pp.attach_predictions(queue, rows)

    assert rows[0]["prediction"]["verdict"] == "accept"
    assert rows[1]["prediction"]["verdict"] == "dismiss"
    assert rows[1]["prediction"]["ts"] == "2026-01-01T00:00:20+00:00"


def test_a_requeued_fact_with_only_a_stale_prediction_gets_no_join(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:01+00:00", "x", "accept")])
    _write_decisions(
        queue,
        [
            ("2026-01-01T00:00:10+00:00", "dismissed", "x"),
            ("2026-01-01T00:00:30+00:00", "dismissed", "x"),
        ],
    )
    rows = [_decision("2026-01-01T00:00:30+00:00", "x", "dismissed")]

    pp.attach_predictions(queue, rows)

    assert "prediction" not in rows[0]


def test_join_with_only_later_predictions_attaches_nothing(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:09+00:00", "x", "accept")])
    rows = [_decision("2026-01-01T00:00:05+00:00", "x", "accepted")]

    pp.attach_predictions(queue, rows)

    assert "prediction" not in rows[0]


def test_a_prediction_at_exactly_the_decision_time_is_joined(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _write_predictions(queue, [_prediction("2026-01-01T00:00:05+00:00", "x", "accept")])
    rows = [_decision("2026-01-01T00:00:05+00:00", "x", "accepted")]

    pp.attach_predictions(queue, rows)

    assert rows[0]["prediction"]["match"] is True


def test_latest_wins_through_dismiss_accept_dismiss(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    for verdict, reason in (("dismiss", "a"), ("accept", "a"), ("dismiss", "a")):
        assert pp.record_prediction(queue, kind="memory", text="x", verdict=verdict, reason=reason)
    assert pp.latest_predictions(queue)[pp.prediction_key("memory", "x")]["verdict"] == "dismiss"
    assert len(pp.read_predictions(queue)) == 3


def test_agreement_ignores_row_order_and_windows_fifty_one_rows() -> None:
    rows = _joined(51, matching=51)
    rows[0]["prediction"] = {"verdict": "accept", "reason": "r", "ts": "x", "match": False}
    shuffled = list(reversed(rows))

    result = pp.agreement(shuffled, handover="off")

    # The oldest row (the only mismatch) falls outside the newest fifty.
    assert result["decided"] == 50
    assert result["matched"] == 50


def test_suggest_handover_is_false_under_all() -> None:
    assert pp.agreement(_joined(20), handover="all")["suggest_handover"] is False
