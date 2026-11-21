from __future__ import annotations

import pytest

from ciao.recurrence import (
    RECURRENCE_THRESHOLD,
    Outcome,
    classify_recurrence,
)


def _resolved(*pairs: tuple[str, str | None]) -> dict[str, Outcome | None]:
    """Build the caller-side lookup: completion id -> Outcome (or None)."""
    out: dict[str, Outcome | None] = {}
    for completion_id, task_id in pairs:
        out[completion_id] = (
            None if task_id is None else Outcome(completion_id, task_id)
        )
    return out


def test_threshold_is_three() -> None:
    assert RECURRENCE_THRESHOLD == 3


def test_one_and_two_distinct_outcomes_are_below() -> None:
    one = classify_recurrence(["c1"], _resolved(("c1", "t1")))
    assert one.status == "below"
    assert one.supported == (Outcome("c1", "t1"),)

    two = classify_recurrence(
        ["c1", "c2"], _resolved(("c1", "t1"), ("c2", "t2"))
    )
    assert two.status == "below"
    assert [o.task_id for o in two.supported] == ["t1", "t2"]


def test_three_distinct_tasks_are_met() -> None:
    verdict = classify_recurrence(
        ["c1", "c2", "c3"],
        _resolved(("c1", "t1"), ("c2", "t2"), ("c3", "t3")),
    )

    assert verdict.status == "met"
    assert verdict.supported == (
        Outcome("c1", "t1"),
        Outcome("c2", "t2"),
        Outcome("c3", "t3"),
    )
    assert verdict.excluded == ()


def test_repeated_completion_id_does_not_progress() -> None:
    verdict = classify_recurrence(
        ["c1", "c1", "c1", "c2"],
        _resolved(("c1", "t1"), ("c2", "t2")),
    )

    assert verdict.status == "below"
    assert verdict.supported == (Outcome("c1", "t1"), Outcome("c2", "t2"))
    assert verdict.excluded == ()


def test_two_completions_of_one_task_count_once() -> None:
    # Four claims against a threshold of three: a reversed "limit then
    # filter" would read only c1..c3 and wrongly land on the wrong count.
    verdict = classify_recurrence(
        ["c1", "c2", "c3", "c4"],
        _resolved(("c1", "t1"), ("c2", "t1"), ("c3", "t2"), ("c4", "t3")),
    )

    assert verdict.status == "met"
    assert verdict.supported == (
        Outcome("c1", "t1"),
        Outcome("c3", "t2"),
        Outcome("c4", "t3"),
    )
    assert verdict.excluded == (("c2", "same_task"),)


def test_invented_id_is_excluded_as_unsupported() -> None:
    # None entry: the caller looked it up and it is not persisted.
    with_none = classify_recurrence(
        ["c1", "c2", "c3"],
        _resolved(("c1", "t1"), ("c2", "t2"), ("c3", None)),
    )
    assert with_none.status == "below"
    assert with_none.supported == (Outcome("c1", "t1"), Outcome("c2", "t2"))
    assert with_none.excluded == (("c3", "unsupported"),)

    # Missing key is the same as None.
    missing = classify_recurrence(
        ["c1", "c2", "ghost"], _resolved(("c1", "t1"), ("c2", "t2"))
    )
    assert missing.status == "below"
    assert missing.excluded == (("ghost", "unsupported"),)


def test_empty_claim_is_unavailable() -> None:
    verdict = classify_recurrence([], {})

    assert verdict.status == "unavailable"
    assert verdict.supported == ()
    assert verdict.excluded == ()


def test_all_unresolved_is_unavailable_not_below() -> None:
    verdict = classify_recurrence(
        ["c1", "c2", "c3"],
        _resolved(("c1", None), ("c2", None)),
    )

    assert verdict.status == "unavailable"
    assert verdict.supported == ()
    assert verdict.excluded == (
        ("c1", "unsupported"),
        ("c2", "unsupported"),
        ("c3", "unsupported"),
    )


def test_blank_and_non_str_ids_are_excluded_without_raising() -> None:
    odd: list[object] = ["", "   ", 7, None, ["c9"], "c1", "c2", "c3"]
    verdict = classify_recurrence(
        odd,  # type: ignore[arg-type]
        _resolved(("c1", "t1"), ("c2", "t2"), ("c3", "t3")),
    )

    assert verdict.status == "met"
    assert verdict.supported == (
        Outcome("c1", "t1"),
        Outcome("c2", "t2"),
        Outcome("c3", "t3"),
    )
    assert verdict.excluded == (
        ("", "blank"),
        ("   ", "blank"),
        ("<int>", "invalid"),
        ("<NoneType>", "invalid"),
        ("<list>", "invalid"),
    )


def test_ordering_is_first_appearance_and_stable() -> None:
    claims = ["c3", "c1", "c3", "c2", "c4"]
    resolved = _resolved(
        ("c1", "t1"), ("c2", "t1"), ("c3", "t3"), ("c4", "t4")
    )

    first = classify_recurrence(claims, resolved)
    second = classify_recurrence(claims, resolved)

    assert first == second
    assert first.supported == (
        Outcome("c3", "t3"),
        Outcome("c1", "t1"),
        Outcome("c4", "t4"),
    )
    assert first.excluded == (("c2", "same_task"),)


def test_threshold_override_is_honoured() -> None:
    two_tasks = _resolved(("c1", "t1"), ("c2", "t2"))

    assert classify_recurrence(["c1", "c2"], two_tasks, threshold=2).status == (
        "met"
    )
    assert classify_recurrence(["c1"], two_tasks, threshold=2).status == "below"
    assert classify_recurrence(["c1"], two_tasks, threshold=1).status == "met"


@pytest.mark.parametrize("threshold", [0, -1])
def test_threshold_below_one_raises(threshold: int) -> None:
    with pytest.raises(ValueError):
        classify_recurrence(["c1"], _resolved(("c1", "t1")), threshold=threshold)
