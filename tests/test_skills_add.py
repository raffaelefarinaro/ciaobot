"""Tests for scripts/skills_add.py (URL parsing + npx add wrapper)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import skills_add  # type: ignore  # noqa: E402


class _FakeResult:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, cwd=None) -> _FakeResult:
        self.calls.append(cmd)
        return _FakeResult(0)


def test_parse_owner_repo_bare():
    assert skills_add.parse_source("owner/repo") == ("owner/repo", None)


def test_parse_https_url_no_skill():
    assert skills_add.parse_source("https://github.com/owner/repo") == ("owner/repo", None)


def test_parse_url_with_tree_skills_segment():
    repo, skill = skills_add.parse_source(
        "https://github.com/owner/repo/tree/main/skills/foo"
    )
    assert repo == "owner/repo"
    assert skill == "foo"


def test_parse_owner_repo_with_tree_skills_segment():
    repo, skill = skills_add.parse_source("owner/repo/tree/main/skills/bar")
    assert repo == "owner/repo"
    assert skill == "bar"


def test_parse_deep_subpath_skills_segment():
    repo, skill = skills_add.parse_source(
        "https://github.com/owner/repo/tree/main/sub/dir/skills/baz"
    )
    assert repo == "owner/repo"
    assert skill == "baz"


def test_parse_tree_branch_only_no_skill():
    repo, skill = skills_add.parse_source("https://github.com/owner/repo/tree/main")
    assert repo == "owner/repo"
    assert skill is None


def test_parse_rejects_garbage():
    try:
        skills_add.parse_source("not a valid source!!")
    except ValueError:
        return
    raise AssertionError("expected ValueError for garbage input")


def test_add_skill_infers_name_and_runs_npx():
    runner = _FakeRunner()
    rc = skills_add.add_skill(
        "https://github.com/owner/repo/tree/main/skills/foo",
        skill=None,
        agent="claude-code",
        runner=runner,
    )
    assert rc == 0
    assert runner.calls == [
        ["npx", "-y", "skills", "add", "owner/repo", "--skill", "foo", "--agent", "claude-code", "-y"]
    ]


def test_add_skill_explicit_skill_overrides_inferred():
    runner = _FakeRunner()
    rc = skills_add.add_skill(
        "https://github.com/owner/repo/tree/main/skills/foo",
        skill="explicit-name",
        agent="claude-code",
        runner=runner,
    )
    assert rc == 0
    assert runner.calls[0][6] == "explicit-name"


def test_add_skill_errors_when_name_not_inferable():
    runner = _FakeRunner()
    rc = skills_add.add_skill("owner/repo", skill=None, agent="claude-code", runner=runner)
    assert rc == 2
    assert runner.calls == []  # npx must not run without a skill name