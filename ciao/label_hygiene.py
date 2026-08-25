"""Label-hygiene audit for open GitHub issues.

Checks open issues on ``raffaelefarinaro/ciaobot`` against the title-prefix
to classification-label convention in the ``ciao-support`` skill and adds
missing classification labels. Never removes or replaces a label: removal
would risk overriding intentional human labeling.

Shape (modeled on :mod:`ciao.os_audit`): a pure decision function over
issue data, unit-testable with plain inputs and no subprocess, is kept
separate from the side-effecting layer that shells out to ``gh``. The
audit is cheap: one ``gh issue list --json`` call, then pure local
decisions, no per-issue API call during planning.

The retired ``[Report]`` prefix and ``report`` label belong to the
anonymous bug-report form that was removed on 2026-07-30. They are not in
the mapping and are surfaced for human decision rather than auto-applied.

Trigger: ``ciao label-hygiene`` (dry-run by default; ``--apply`` writes).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_REPO = "raffaelefarinaro/ciaobot"
DEFAULT_LIMIT = 100

# Title-prefix -> classification label, from ciao/system_prompt.md "Issue
# labeling". Keys are normalized (lowercased, stripped) bracket tokens.
PREFIX_LABELS: dict[str, str] = {
    "bug": "bug",
    "feature": "enhancement",
    "docs": "documentation",
    "chore": "chore",
    "goal": "enhancement",
}

# Prefixes that must never be auto-labeled. ``[Agent]`` classification
# follows the issue content, so a human must decide. ``[Report]`` is
# retired and must not be re-applied. Any other unrecognized bracket token
# is treated as unknown and surfaced the same way.
HUMAN_PREFIXES: frozenset[str] = frozenset({"agent"})
RETIRED_PREFIXES: frozenset[str] = frozenset({"report"})

# Matches a leading "[token]" prefix. Tolerant of surrounding whitespace
# and lowercase/odd spacing: "[Bug]", "[ bug ]", "[BUG]foo" all parse to
# "bug". Only the first bracket group is considered.
_PREFIX_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]")


def parse_title_prefix(title: str) -> str | None:
    """Return the normalized (lowercased, stripped) prefix token, or None.

    "[Bug] crash" -> "bug"; "[Feature] x" -> "feature"; "No prefix" -> None.
    The brackets themselves are not part of the returned token.
    """
    if not title:
        return None
    m = _PREFIX_RE.match(title)
    if not m:
        return None
    token = m.group(1).strip().lower()
    return token or None


@dataclass(frozen=True)
class Issue:
    """A single open issue as fetched from ``gh issue list``."""

    number: int
    title: str
    labels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LabelAddition:
    """Planned ``gh issue edit --add-label`` for a missing classification label."""

    number: int
    title: str
    prefix: str
    label: str


@dataclass(frozen=True)
class HumanReview:
    """An issue that needs a human label decision (not auto-applied)."""

    number: int
    title: str
    prefix: str
    reason: str  # "agent" | "retired-prefix" | "unknown-prefix"


@dataclass(frozen=True)
class TriageCandidate:
    """An unprefixed issue flagged for human triage."""

    number: int
    title: str
    labels: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class LabelHygieneReport:
    """Result of the pure decision pass over a set of open issues."""

    additions: tuple[LabelAddition, ...] = field(default_factory=tuple)
    needs_human: tuple[HumanReview, ...] = field(default_factory=tuple)
    no_prefix: tuple[TriageCandidate, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "additions": [a.__dict__ for a in self.additions],
            "needs_human": [h.__dict__ for h in self.needs_human],
            "no_prefix": [c.__dict__ for c in self.no_prefix],
        }


def plan_label_actions(issues: Iterable[Issue]) -> LabelHygieneReport:
    """Pure decision: map each issue to planned actions, no side effects.

    Rules (from the ciao-support skill's GitHub issue-label table and
    issue #235):
    - Known prefix -> add the expected classification label if missing.
    - ``[Agent]`` -> needs human; never auto-apply.
    - ``[Report]`` or other unknown bracket -> needs human; never auto-apply.
    - No bracket prefix -> flag for triage; never invent a label.

    Never produces a label removal. Idempotent: feeding the post-action
    state (expected label now present) yields zero additions.
    """
    additions: list[LabelAddition] = []
    needs_human: list[HumanReview] = []
    no_prefix: list[TriageCandidate] = []

    for issue in issues:
        token = parse_title_prefix(issue.title)
        labels = set(issue.labels)

        if token is None:
            no_prefix.append(
                TriageCandidate(
                    number=issue.number,
                    title=issue.title,
                    labels=issue.labels,
                )
            )
            continue

        if token in PREFIX_LABELS:
            expected = PREFIX_LABELS[token]
            if expected not in labels:
                additions.append(
                    LabelAddition(
                        number=issue.number,
                        title=issue.title,
                        prefix=token,
                        label=expected,
                    )
                )
            # Already correct: no action. Idempotent on re-run.
            continue

        if token in HUMAN_PREFIXES:
            reason = "agent"
        elif token in RETIRED_PREFIXES:
            reason = "retired-prefix"
        else:
            reason = "unknown-prefix"
        needs_human.append(
            HumanReview(
                number=issue.number,
                title=issue.title,
                prefix=token,
                reason=reason,
            )
        )

    return LabelHygieneReport(
        additions=tuple(additions),
        needs_human=tuple(needs_human),
        no_prefix=tuple(no_prefix),
    )


# --- Side-effecting layer -------------------------------------------------

# A runner type matching subprocess.run. Injectable for tests.
Runner = Callable[..., subprocess.CompletedProcess]


def fetch_open_issues(
    repo: str = DEFAULT_REPO,
    limit: int = DEFAULT_LIMIT,
    runner: Runner | None = None,
) -> list[Issue]:
    """Fetch open issues via one ``gh issue list`` call. No per-issue calls."""
    runner = runner or subprocess.run
    result = runner(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,labels",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    issues: list[Issue] = []
    for entry in data:
        raw_labels = entry.get("labels") or []
        label_names_list: list[str] = []
        for label in raw_labels:
            name = label.get("name") if isinstance(label, dict) else label
            if name is not None:
                label_names_list.append(str(name))
        label_names = tuple(label_names_list)
        issues.append(
            Issue(
                number=entry["number"],
                title=entry.get("title", ""),
                labels=label_names,
            )
        )
    return issues


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of one ``gh issue edit --add-label`` call."""

    number: int
    label: str
    ok: bool
    error: str = ""


def apply_additions(
    additions: Sequence[LabelAddition],
    repo: str = DEFAULT_REPO,
    runner: Runner | None = None,
) -> list[ApplyResult]:
    """Execute the planned label additions. Only ever adds labels."""
    runner = runner or subprocess.run
    results: list[ApplyResult] = []
    for add in additions:
        try:
            runner(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(add.number),
                    "--repo",
                    repo,
                    "--add-label",
                    add.label,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            results.append(ApplyResult(number=add.number, label=add.label, ok=True))
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            results.append(
                ApplyResult(number=add.number, label=add.label, ok=False, error=err)
            )
    return results


def run_audit(
    repo: str = DEFAULT_REPO,
    limit: int = DEFAULT_LIMIT,
    apply: bool = False,
    fetch_runner: Runner | None = None,
    apply_runner: Runner | None = None,
) -> dict[str, Any]:
    """Fetch open issues, plan actions, and optionally apply additions.

    Returns a dict with the report and (when ``apply`` is True) apply
    results. Dry-run by default: plans only, no ``gh issue edit`` calls.
    """
    issues = fetch_open_issues(repo=repo, limit=limit, runner=fetch_runner)
    report = plan_label_actions(issues)
    payload: dict[str, Any] = {
        "repo": repo,
        "open_count": len(issues),
        "report": report.to_dict(),
    }
    if apply and report.additions:
        payload["apply"] = [
            r.__dict__ for r in apply_additions(report.additions, repo=repo, runner=apply_runner)
        ]
    return payload


# --- CLI ------------------------------------------------------------------


def format_report_text(payload: dict[str, Any], apply: bool) -> str:
    """Render a human-readable summary of the audit payload."""
    report = payload["report"]
    lines: list[str] = []
    lines.append(f"Label hygiene audit: {payload['repo']}")
    lines.append(f"Open issues scanned: {payload['open_count']}")
    lines.append(f"Mode: {'apply' if apply else 'dry-run'}")

    additions = report["additions"]
    if additions:
        lines.append(f"Labels to add ({len(additions)}):")
        for a in additions:
            lines.append(f"  #{a['number']} [{a['prefix']}] +{a['label']}  {a['title']}")
    else:
        lines.append("Labels to add: 0")

    needs_human = report["needs_human"]
    if needs_human:
        lines.append(f"Needs human decision ({len(needs_human)}):")
        for h in needs_human:
            lines.append(
                f"  #{h['number']} [{h['prefix']}] ({h['reason']})  {h['title']}"
            )

    no_prefix = report["no_prefix"]
    if no_prefix:
        lines.append(f"No prefix, flagged for triage ({len(no_prefix)}):")
        for c in no_prefix:
            label_state = ",".join(c["labels"]) if c["labels"] else "(no labels)"
            lines.append(f"  #{c['number']} {label_state}  {c['title']}")

    if apply and "apply" in payload:
        applied = payload["apply"]
        ok = sum(1 for r in applied if r["ok"])
        failed = [r for r in applied if not r["ok"]]
        lines.append(f"Applied: {ok} ok, {len(failed)} failed")
        for r in failed:
            lines.append(f"  #{r['number']} +{r['label']} FAILED: {r['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit open-issue labels against the title-prefix convention "
            "and add missing classification labels. Dry-run by default."
        ),
    )
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Target GitHub repo (owner/name).")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum open issues to scan.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually add missing labels via gh issue edit. Default is dry-run.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured report as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    payload = run_audit(repo=args.repo, limit=args.limit, apply=args.apply)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_report_text(payload, apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
