"""Tests for ``ciao.label_hygiene``."""

from __future__ import annotations

import json
import subprocess

import pytest

from ciao import label_hygiene as lh
from ciao.label_hygiene import (
    Issue,
    apply_additions,
    fetch_open_issues,
    parse_title_prefix,
    plan_label_actions,
    run_audit,
)


# --- parse_title_prefix ----------------------------------------------------


@pytest.mark.parametrize(
    "title, expected",
    [
        ("[Bug] crash on startup", "bug"),
        ("[bug] lowercase already", "bug"),
        ("[ BUG ] odd spacing", "bug"),
        ("[Bug]no space after bracket", "bug"),
        ("  [Feature] leading whitespace", "feature"),
        ("[Agent] triage surfaced this", "agent"),
        ("[Report] retired prefix", "report"),
        ("[Mystery] unknown prefix", "mystery"),
        ("No prefix at all", None),
        ("", None),
        ("[Bug][Feature] only first prefix counts", "bug"),
    ],
)
def test_parse_title_prefix(title: str, expected: str | None) -> None:
    assert parse_title_prefix(title) == expected


# --- plan_label_actions: table-driven decision logic -----------------------


def test_each_known_prefix_maps_to_expected_label() -> None:
    issues = [
        Issue(1, "[Bug] x", ()),
        Issue(2, "[Feature] x", ()),
        Issue(3, "[Docs] x", ()),
        Issue(4, "[Chore] x", ()),
        Issue(5, "[Goal] x", ()),
    ]
    report = plan_label_actions(issues)
    got = {a.number: a.label for a in report.additions}
    assert got == {
        1: "bug",
        2: "enhancement",
        3: "documentation",
        4: "chore",
        5: "enhancement",
    }
    assert report.needs_human == ()
    assert report.no_prefix == ()


def test_missing_label_produces_addition() -> None:
    report = plan_label_actions([Issue(1, "[Bug] crash", ())])
    assert len(report.additions) == 1
    add = report.additions[0]
    assert add.number == 1
    assert add.prefix == "bug"
    assert add.label == "bug"


def test_already_correct_label_produces_no_action() -> None:
    report = plan_label_actions([Issue(1, "[Bug] crash", ("bug",))])
    assert report.additions == ()
    assert report.needs_human == ()
    assert report.no_prefix == ()


def test_agent_prefix_needs_human_and_no_addition() -> None:
    report = plan_label_actions([Issue(1, "[Agent] surfaced from triage loop", ())])
    assert report.additions == ()
    assert len(report.needs_human) == 1
    review = report.needs_human[0]
    assert review.number == 1
    assert review.prefix == "agent"
    assert review.reason == "agent"


def test_no_prefix_flagged_for_triage_and_no_addition() -> None:
    report = plan_label_actions([Issue(1, "Something broke", ("bug",))])
    assert report.additions == ()
    assert report.needs_human == ()
    assert len(report.no_prefix) == 1
    assert report.no_prefix[0].number == 1
    assert report.no_prefix[0].labels == ("bug",)


def test_multiple_existing_labels_only_adds_missing_classification_label() -> None:
    report = plan_label_actions(
        [Issue(1, "[Bug] crash", ("priority-high", "needs-repro"))]
    )
    assert len(report.additions) == 1
    assert report.additions[0].label == "bug"


def test_unknown_prefix_needs_human() -> None:
    report = plan_label_actions([Issue(1, "[Mystery] what is this", ())])
    assert report.additions == ()
    assert len(report.needs_human) == 1
    assert report.needs_human[0].reason == "unknown-prefix"
    assert report.needs_human[0].prefix == "mystery"


def test_retired_report_prefix_needs_human_and_is_not_auto_labeled() -> None:
    report = plan_label_actions([Issue(1, "[Report] old bug form", ())])
    assert report.additions == ()
    assert len(report.needs_human) == 1
    assert report.needs_human[0].reason == "retired-prefix"
    # Never invents the retired "report" label.
    assert all(a.label != "report" for a in report.additions)


def test_lowercase_and_odd_spacing_normalizes_before_matching() -> None:
    report = plan_label_actions(
        [
            Issue(1, "[bug] already lowercase", ()),
            Issue(2, "[ Feature ] padded", ()),
            Issue(3, "[FEATURE] shouting", ()),
        ]
    )
    got = {a.number: a.label for a in report.additions}
    assert got == {1: "bug", 2: "enhancement", 3: "enhancement"}


def test_mixed_batch_sorts_into_all_three_categories() -> None:
    issues = [
        Issue(1, "[Bug] missing label", ()),
        Issue(2, "[Bug] already labeled", ("bug",)),
        Issue(3, "[Agent] needs human", ()),
        Issue(4, "no prefix here", ()),
    ]
    report = plan_label_actions(issues)
    assert [a.number for a in report.additions] == [1]
    assert [h.number for h in report.needs_human] == [3]
    assert [c.number for c in report.no_prefix] == [4]


# --- idempotency and never-remove guarantees -------------------------------


def test_plan_is_idempotent_after_applying_additions() -> None:
    issues = [
        Issue(1, "[Bug] crash", ()),
        Issue(2, "[Feature] new thing", ()),
        Issue(3, "[Agent] surfaced", ()),
        Issue(4, "no prefix", ()),
    ]
    first_report = plan_label_actions(issues)
    assert len(first_report.additions) == 2

    # Simulate the additions having been applied.
    by_number = {i.number: i for i in issues}
    for addition in first_report.additions:
        issue = by_number[addition.number]
        by_number[addition.number] = Issue(
            issue.number, issue.title, issue.labels + (addition.label,)
        )

    second_report = plan_label_actions(by_number.values())
    assert second_report.additions == ()
    # needs_human / no_prefix categories are unaffected by re-running.
    assert len(second_report.needs_human) == 1
    assert len(second_report.no_prefix) == 1


@pytest.mark.parametrize(
    "issue",
    [
        Issue(1, "[Bug] x", ("enhancement", "priority-high")),
        Issue(2, "[Agent] x", ("bug",)),
        Issue(3, "no prefix", ("chore",)),
        Issue(4, "[Docs] x", ()),
    ],
)
def test_no_case_ever_removes_an_existing_label(issue: Issue) -> None:
    report = plan_label_actions([issue])
    surviving_labels = set(issue.labels)
    for addition in report.additions:
        surviving_labels.add(addition.label)
    # Every original label is still present; the plan can only add.
    assert set(issue.labels) <= surviving_labels


# --- side-effecting layer: fetch and apply ---------------------------------


class _FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_fetch_open_issues_calls_gh_once_and_parses_labels() -> None:
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        payload = [
            {"number": 1, "title": "[Bug] x", "labels": [{"name": "bug"}]},
            {"number": 2, "title": "[Feature] y", "labels": []},
        ]
        return _FakeResult(stdout=json.dumps(payload))

    issues = fetch_open_issues(repo="acme/widgets", limit=50, runner=fake_runner)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == ["gh", "issue", "list"]
    assert "--repo" in cmd and "acme/widgets" in cmd
    assert "--limit" in cmd and "50" in cmd
    assert issues == [
        Issue(1, "[Bug] x", ("bug",)),
        Issue(2, "[Feature] y", ()),
    ]


def test_apply_additions_only_ever_uses_add_label() -> None:
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        return _FakeResult()

    additions = [
        lh.LabelAddition(number=1, title="[Bug] x", prefix="bug", label="bug"),
        lh.LabelAddition(number=2, title="[Docs] y", prefix="docs", label="documentation"),
    ]
    results = apply_additions(additions, repo="acme/widgets", runner=fake_runner)

    assert len(calls) == 2
    for cmd in calls:
        assert "--add-label" in cmd
        assert "--remove-label" not in cmd
    assert all(r.ok for r in results)


def test_apply_additions_reports_failure_without_raising() -> None:
    def fake_runner(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="not found")

    additions = [lh.LabelAddition(number=1, title="[Bug] x", prefix="bug", label="bug")]
    results = apply_additions(additions, runner=fake_runner)

    assert len(results) == 1
    assert results[0].ok is False
    assert "not found" in results[0].error


def test_run_audit_dry_run_makes_no_apply_calls() -> None:
    apply_calls = []

    def fetch_runner(cmd, **kwargs):
        payload = [{"number": 1, "title": "[Bug] x", "labels": []}]
        return _FakeResult(stdout=json.dumps(payload))

    def apply_runner(cmd, **kwargs):
        apply_calls.append(cmd)
        return _FakeResult()

    payload = run_audit(apply=False, fetch_runner=fetch_runner, apply_runner=apply_runner)

    assert apply_calls == []
    assert "apply" not in payload
    assert len(payload["report"]["additions"]) == 1


def test_run_audit_apply_true_executes_planned_additions() -> None:
    apply_calls = []

    def fetch_runner(cmd, **kwargs):
        payload = [{"number": 1, "title": "[Bug] x", "labels": []}]
        return _FakeResult(stdout=json.dumps(payload))

    def apply_runner(cmd, **kwargs):
        apply_calls.append(cmd)
        return _FakeResult()

    payload = run_audit(apply=True, fetch_runner=fetch_runner, apply_runner=apply_runner)

    assert len(apply_calls) == 1
    assert payload["apply"][0]["ok"] is True


# --- CLI entry point --------------------------------------------------------


def test_main_dry_run_default_makes_no_gh_edit_calls(monkeypatch, capsys) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        payload = [{"number": 1, "title": "[Bug] x", "labels": []}]
        return _FakeResult(stdout=json.dumps(payload))

    monkeypatch.setattr(lh.subprocess, "run", fake_run)

    exit_code = lh.main([])

    assert exit_code == 0
    assert len(calls) == 1  # only the list call, no edit call
    out = capsys.readouterr().out
    assert "Labels to add (1)" in out


def test_main_json_mode_emits_structured_report(monkeypatch, capsys) -> None:
    def fake_run(cmd, **kwargs):
        payload = [{"number": 1, "title": "[Agent] triage", "labels": []}]
        return _FakeResult(stdout=json.dumps(payload))

    monkeypatch.setattr(lh.subprocess, "run", fake_run)

    exit_code = lh.main(["--json"])

    assert exit_code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["report"]["needs_human"][0]["reason"] == "agent"
