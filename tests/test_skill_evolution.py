"""Tests for ``ciao.skill_evolution``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ciao import skill_evolution as se
from ciao import trajectory_builder as tb
from ciao.providers.pi import PiSettings


# ── trajectory mining ───────────────────────────────────────────────────


def _t(skill: str, *, outcome: str = "success", corrections: int = 0, errors: list | None = None) -> dict:
    return {
        "session_id": f"sess-{skill}",
        "timestamp": "2026-05-22T10:00:00Z",
        "outcome": outcome,
        "user_corrections": corrections,
        "errors": errors or [],
        "skills_loaded": [skill],
        "tools_used": [],
        "turns": 3,
    }


def test_find_underperforming_groups_by_skill() -> None:
    trajectories = [
        _t("web-research", outcome="success"),  # clean → skip
        _t("web-research", outcome="needs_review", errors=[{"summary": "x"}]),
        _t("humanizer", corrections=2),
        _t("humanizer", outcome="success"),  # clean → skip
    ]
    flagged = se.find_underperforming_skills(trajectories)
    assert set(flagged.keys()) == {"web-research", "humanizer"}
    assert len(flagged["web-research"]) == 1
    assert len(flagged["humanizer"]) == 1


def test_find_underperforming_min_sessions_filter() -> None:
    trajectories = [
        _t("web-research", corrections=1),
        _t("humanizer", corrections=1),
        _t("humanizer", errors=[{"x": 1}]),
    ]
    flagged = se.find_underperforming_skills(trajectories, min_sessions=2)
    assert "humanizer" in flagged
    assert "web-research" not in flagged


def test_find_underperforming_handles_missing_fields() -> None:
    flagged = se.find_underperforming_skills([{"skills_loaded": ["x"]}])
    assert flagged == {}


# ── skill file resolution ───────────────────────────────────────────────


def test_find_skill_file_prefers_skill_md(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "web-research").mkdir(parents=True)
    (root / "web-research" / "SKILL.md").write_text("content")
    assert se.find_skill_file("web-research", root) == root / "web-research" / "SKILL.md"


def test_find_skill_file_returns_none_when_missing(tmp_path: Path) -> None:
    assert se.find_skill_file("nope", tmp_path) is None
    assert se.find_skill_file("", tmp_path) is None


def test_passes_size_check() -> None:
    assert se.passes_size_check("hello")
    assert not se.passes_size_check("x" * (se.MAX_SKILL_BYTES + 1))


def test_find_skill_tests_handles_dashed_names(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_web_research.py").write_text("# t")
    found = se.find_skill_tests("web-research", tests_root=tests_root)
    assert len(found) == 1
    assert found[0].name == "test_web_research.py"


def test_run_skill_tests_no_files_returns_true() -> None:
    assert se.run_skill_tests([]) is True


# ── propose_skill_edit ──────────────────────────────────────────────────


def test_propose_skill_edit_returns_none_when_pi_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: None)

    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("web-research", corrections=1)],
            pi_settings=PiSettings(), model="ministral-3:3b",
        )
    )
    assert result is None


def test_propose_skill_edit_returns_none_on_no_improvement_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_pi_oneshot",
        AsyncMock(return_value="No clear improvement found."),
    )
    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("x", corrections=1)],
            pi_settings=PiSettings(), model="ministral-3:3b",
        )
    )
    assert result is None


def test_propose_skill_edit_returns_text_when_model_proposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\nold guidance\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_pi_oneshot",
        AsyncMock(return_value="Replace 'old guidance' with 'new guidance'.\nconfidence: 0.7"),
    )
    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("x", corrections=1)],
            pi_settings=PiSettings(), model="ministral-3:3b",
        )
    )
    assert result is not None
    assert "new guidance" in result
    assert "confidence" in result


# ── write_proposal ──────────────────────────────────────────────────────


def test_write_proposal_creates_dated_file(tmp_path: Path) -> None:
    skill_path = tmp_path / "skills" / "web-research" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Skill\n")
    output_dir = tmp_path / "Skill-Proposals"
    ts = datetime(2026, 5, 23, tzinfo=UTC)
    path = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[_t("web-research", corrections=1)],
        proposal_text="add a defuddle fallback",
        output_dir=output_dir,
        now=ts,
    )
    assert path.name == "2026-05-23-web-research.md"
    text = path.read_text()
    assert "type: skill-proposal" in text
    assert "skill: web-research" in text
    assert "add a defuddle fallback" in text
    assert "trajectories: 1" in text


# ── run_evolution_pass end-to-end ───────────────────────────────────────


def _mock_pi_returning(*responses: str) -> AsyncMock:
    """Helper: AsyncMock that returns each response in order, then sticks on last."""
    mock = AsyncMock()
    mock.side_effect = list(responses) + [responses[-1]] * 20 if responses else None
    return mock


def test_run_evolution_pass_writes_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stage a single bad trajectory under a fake ~/.ciao/trajectories root
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(UTC)
    tb.write_trajectory(_t("web-research", corrections=2) | {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
    })

    # Stage the matching skill file
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    skill_dir = skills_root / "web-research"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Skill\n\noriginal\n")

    output_dir = tmp_path / "Skill-Proposals"

    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    # First call = proposal, second call = semantic check verdict
    pi_mock = _mock_pi_returning(
        "Suggested edit: explain defuddle.\nconfidence: 0.6",
        "VERDICT: PRESERVED\nREASON: only adds clarification on defuddle",
    )
    monkeypatch.setattr("ciao.skill_evolution.run_pi_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            output_dir=output_dir,
            pi_settings=PiSettings(),
            model="kimi-k2.7-code:cloud",
            min_sessions=1,
            enable_test_gate=False,
            now=now,
            retention_months=None,  # don't prune our test fixtures
        )
    )
    assert len(paths) == 1
    text = paths[0].read_text()
    assert "Suggested edit" in text
    assert "semantic_check: PRESERVED" in text


def test_run_evolution_pass_returns_empty_when_no_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Only a clean trajectory
    tb.write_trajectory(_t("web-research") | {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    })
    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=tmp_path / "skills",
            output_dir=tmp_path / "out",
            retention_months=None,
        )
    )
    assert paths == []


def test_run_evolution_pass_writes_trim_proposal_for_oversized_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over-cap skills get a trim-mode proposal, not a silent skip."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tb.write_trajectory(_t("big-skill", corrections=1) | {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    })
    skills_root = tmp_path / "skills"
    (skills_root / "big-skill").mkdir(parents=True)
    big_size = se.MAX_SKILL_BYTES + 1
    (skills_root / "big-skill" / "SKILL.md").write_text("x" * big_size)

    pi_mock = AsyncMock(return_value="No clear improvement found.")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr("ciao.skill_evolution.run_pi_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            output_dir=tmp_path / "out",
            retention_months=None,
        )
    )
    # The skill is over the cap, so the model is called in trim mode.
    # Even when it returns "no safe trim", a stub proposal lands so the
    # signal doesn't disappear.
    pi_mock.assert_called_once()
    system_prompt = pi_mock.call_args.kwargs["system_prompt"]
    assert "trim" in system_prompt.lower()
    assert "OVER the 15KB size cap" in system_prompt
    assert str(big_size) in system_prompt
    assert len(paths) == 1
    body = paths[0].read_text(encoding="utf-8")
    assert "No clear improvement found" in body


# ── semantic-drift gate ─────────────────────────────────────────────────


def test_passes_semantic_check_fail_open_when_pi_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: None)
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            pi_settings=PiSettings(), model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is True
    assert "pi" in reason.lower()


def test_passes_semantic_check_returns_true_on_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_pi_oneshot",
        AsyncMock(return_value="VERDICT: PRESERVED\nREASON: clarification only"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            pi_settings=PiSettings(), model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is True
    assert reason == "clarification only"


def test_passes_semantic_check_returns_false_on_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_pi_oneshot",
        AsyncMock(return_value="VERDICT: DRIFTED\nREASON: changed the trigger keywords"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            pi_settings=PiSettings(), model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is False
    assert "trigger" in reason


def test_passes_semantic_check_fail_open_on_unparseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the judge returns garbage, surface the raw output but don't
    drop the proposal — human review is the actual gate."""
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_pi_oneshot",
        AsyncMock(return_value="I think it's fine?"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            pi_settings=PiSettings(), model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is True
    assert "unparseable" in reason or "no output" in reason or reason


def test_run_evolution_pass_drops_drifted_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(UTC)
    tb.write_trajectory(_t("web-research", corrections=2) | {
        "timestamp": now.isoformat().replace("+00:00", "Z"),
    })
    skills_root = tmp_path / "skills"
    (skills_root / "web-research").mkdir(parents=True)
    (skills_root / "web-research" / "SKILL.md").write_text("# Skill\n")

    pi_mock = _mock_pi_returning(
        "Replace 'web research' with 'cooking recipes'.\nconfidence: 0.9",
        "VERDICT: DRIFTED\nREASON: changes the skill's domain entirely",
    )
    monkeypatch.setattr("ciao.skill_evolution.shutil.which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr("ciao.skill_evolution.run_pi_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            output_dir=tmp_path / "out",
            now=now,
            retention_months=None,
        )
    )
    assert paths == []


# ── retention pruning at tail of pass ───────────────────────────────────


def test_run_evolution_pass_prunes_old_trajectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime(2026, 5, 23, tzinfo=UTC)
    # Trajectory from way before retention window
    old_ts = datetime(2025, 1, 1, tzinfo=UTC)
    tb.write_trajectory({
        "session_id": "ancient",
        "timestamp": old_ts.isoformat().replace("+00:00", "Z"),
    })
    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=tmp_path / "skills",
            output_dir=tmp_path / "out",
            now=now,
            retention_months=6,
        )
    )
    assert paths == []
    # The ancient trajectory should be gone
    remaining = list(tb.list_trajectories())
    assert remaining == []
