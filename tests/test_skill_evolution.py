"""Tests for ``ciao.skill_evolution``."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ciao import skill_evolution as se
from ciao import skill_proposals
from ciao import trajectory_builder as tb
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.debug_report import recent_job_failures


def _config(tmp_path: Path, name: str = "client") -> CiaoConfig:
    """A throwaway registry whose one workspace vault lives under tmp_path.

    The pass is handed a config rather than a directory, because the queue's
    location is derived from the registry by ``ciao.skill_proposals`` — one
    derivation, so a pass and the review surface cannot name different folders.
    """
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")},
    )


def test_the_pass_queue_follows_the_active_workspace_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The queue is the registry's, named by workspace. Resolved per call, not at
    import: an import-time constant that depended on vault layout relocated the
    queue whenever the layout changed, orphaning proposals already written to
    the old location."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps([
            {
                "name": "client",
                "vault_root": "memory-vault/client",
            }
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "client")
    monkeypatch.setenv("CIAO_OLLAMA_LOCAL_DISCOVERY", "0")

    config = CiaoConfig.from_env()

    assert se._resolve_queue_workspace(config, None) == "client"
    assert skill_proposals.queue_dir(config, "client") == (
        tmp_path / "memory-vault" / "client" / "Workspace" / "Skill-Proposals"
    )


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


def test_propose_skill_edit_returns_none_when_model_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(side_effect=OSError("no upstream")),
    )

    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("web-research", corrections=1)],
            model="ministral-3:3b",
        )
    )
    assert result is None


def test_propose_skill_edit_returns_none_on_no_improvement_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(return_value="No clear improvement found."),
    )
    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("x", corrections=1)],
            model="ministral-3:3b",
        )
    )
    assert result is None


def test_propose_skill_edit_returns_text_when_model_proposes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\nold guidance\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(return_value="Replace 'old guidance' with 'new guidance'.\nconfidence: 0.7"),
    )
    result = asyncio.run(
        se.propose_skill_edit(
            skill_path, [_t("x", corrections=1)],
            model="ministral-3:3b",
        )
    )
    assert result is not None
    assert "new guidance" in result
    assert "confidence" in result


# ── write_proposal ──────────────────────────────────────────────────────


_PROPOSAL_TEXT = (
    "## What I noticed\nRepeated fetch failures.\n\n"
    "## Suggested improvement\nAdd a defuddle fallback.\n\n"
    "## Why this should help\nIt handles blocked pages.\n\n"
    "## Proposed edit\n```diff\n+fallback\n```\nconfidence: 0.8"
)


def test_write_proposal_records_one_readable_file_per_skill(tmp_path: Path) -> None:
    """The pass's output is a record, not a rendered file it owns: the finding
    lands in named sections and the sessions behind it are addressable."""
    config = _config(tmp_path)
    skill_path = tmp_path / "skills" / "web-research" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Skill\n")
    ts = datetime(2026, 5, 23, tzinfo=UTC)
    path = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[_t("web-research", corrections=1)],
        proposal_text=_PROPOSAL_TEXT,
        config=config,
        workspace="client",
        now=ts,
    )
    assert path == skill_proposals.queue_dir(config, "client") / "web-research.md"
    text = path.read_text(encoding="utf-8")
    assert "type: skill-proposal" in text
    assert "skill: web-research" in text
    assert "lifecycle: pending" in text

    record = skill_proposals.parse_proposal(path, "client")
    assert record is not None
    assert record.title == "Skill reflection: web-research"
    assert record.problem == "Repeated fetch failures."
    # The prompt asks for the improvement and the diff separately; both are the
    # change, so both arrive in the record's one change field.
    assert record.change.startswith("Add a defuddle fallback.")
    assert "+fallback" in record.change
    assert record.rationale == "It handles blocked pages."
    assert [item.chat_id for item in record.sources] == ["sess-web-research"]
    assert record.sources[0].excerpt == (
        "outcome=success corrections=1 errors=0 turns=3"
    )
    assert record.reviewed_revision == hashlib.sha256(
        skill_path.read_bytes()
    ).hexdigest()


def test_rerunning_the_writer_merges_rather_than_overwrites(tmp_path: Path) -> None:
    """The weekly pass re-reads the same skill every run, and each run saw a
    different set of sessions. The old writer rendered Markdown and wrote it
    over the last run's, so the queue could not say which sessions had now been
    seen and one run's findings simply vanished."""
    config = _config(tmp_path)
    skill_path = tmp_path / "skills" / "web-research" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Skill\n")
    queue = skill_proposals.queue_dir(config, "client")
    queue.mkdir(parents=True)
    legacy = queue / "2026-05-20-web-research.md"
    legacy.write_text("legacy", encoding="utf-8")

    first = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[_t("web-research", corrections=1)],
        proposal_text=_PROPOSAL_TEXT,
        config=config,
        workspace="client",
        now=datetime(2026, 5, 23, tzinfo=UTC),
    )

    updated = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[
            _t("web-research", corrections=2) | {"session_id": "sess-second"},
        ],
        proposal_text="## What I noticed\nUpdated evidence.",
        config=config,
        workspace="client",
        now=datetime(2026, 5, 24, tzinfo=UTC),
    )
    assert updated == first
    assert [item.name for item in queue.glob("*.md")] == ["web-research.md"]

    merged = skill_proposals.parse_proposal(updated, "client")
    before = skill_proposals.parse_proposal(first, "client")
    assert before is not None and merged is not None
    # One record, so one identity: the run did not fork a second row.
    assert merged.id == before.id
    assert merged.problem == "Updated evidence."
    assert [item.chat_id for item in merged.sources] == [
        "sess-web-research",
        "sess-second",
    ]


def test_reprocessing_the_same_evidence_writes_nothing(tmp_path: Path) -> None:
    """A pass that saw exactly what the last one saw must leave the file
    untouched — including its timestamp, or every weekly run would rewrite the
    queue and a diff could never tell a real change from a re-run."""
    config = _config(tmp_path)
    skill_path = tmp_path / "skills" / "web-research" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Skill\n")
    ts = datetime(2026, 5, 23, tzinfo=UTC)
    written = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[_t("web-research", corrections=1)],
        proposal_text=_PROPOSAL_TEXT,
        config=config,
        workspace="client",
        now=ts,
    )
    before = written.read_bytes()
    stamp = written.stat().st_mtime_ns

    again = se.write_proposal(
        skill_name="web-research",
        skill_path=skill_path,
        trajectories=[_t("web-research", corrections=1)],
        proposal_text=_PROPOSAL_TEXT,
        config=config,
        workspace="client",
        now=ts + timedelta(days=7),
    )

    assert again == written
    assert written.read_bytes() == before
    assert written.stat().st_mtime_ns == stamp


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

    config = _config(tmp_path)

    # First call = proposal, second call = semantic check verdict
    pi_mock = _mock_pi_returning(
        "Suggested edit: explain defuddle.\nconfidence: 0.6",
        "VERDICT: PRESERVED\nREASON: only adds clarification on defuddle",
    )
    monkeypatch.setattr("ciao.skill_evolution.run_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            config=config,
            model="kimi-k2.7-code:cloud",
            min_sessions=1,
            enable_test_gate=False,
            now=now,
            retention_months=None,  # don't prune our test fixtures
        )
    )
    assert len(paths) == 1
    # The path is the queue's own, derived from the registry.
    assert paths[0] == skill_proposals.queue_dir(config, "client") / "web-research.md"
    record = skill_proposals.parse_proposal(paths[0], "client")
    assert record is not None
    # This answer ignored the prompt's headings, so it has no field of its own
    # and is carried verbatim rather than dropped or guessed at.
    assert "Suggested edit" in record.rationale
    assert record.problem == ""
    assert [item.chat_id for item in record.sources] == ["sess-web-research"]
    # The drift gate is a DAG node, so its verdict is a job_runs row; the record
    # is the finding, not the gate log.
    assert "semantic_check" not in paths[0].read_text(encoding="utf-8")


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
            config=_config(tmp_path),
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
    monkeypatch.setattr("ciao.skill_evolution.run_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            config=_config(tmp_path),
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


def test_run_evolution_pass_does_not_write_stub_for_undercap_no_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An under-cap skill whose model pass returns 'no improvement' must
    not clutter the review queue. Only over-cap no-proposal gets a stub;
    under-cap weak signals (loaded-but-not-causal) are audit-logged via
    job_runs (has_proposal=no-proposal) and return no file."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    tb.write_trajectory(_t("small-skill", corrections=1) | {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    })
    skills_root = tmp_path / "skills"
    (skills_root / "small-skill").mkdir(parents=True)
    # Under the size cap, so this is NOT a trim-mode pass.
    (skills_root / "small-skill" / "SKILL.md").write_text("# Small skill\n\nshort body\n")

    pi_mock = AsyncMock(return_value="No clear improvement found.")
    monkeypatch.setattr("ciao.skill_evolution.run_oneshot", pi_mock)

    config = _config(tmp_path)
    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            config=config,
            retention_months=None,
        )
    )
    # The model was called in normal (non-trim) mode and returned no proposal.
    pi_mock.assert_called_once()
    system_prompt = pi_mock.call_args.kwargs["system_prompt"]
    assert "trim" not in system_prompt.lower()
    # No stub file should be written for under-cap no-improvement.
    assert len(paths) == 0
    queue = skill_proposals.queue_dir(config, "client")
    assert not queue.is_dir() or list(queue.glob("*.md")) == []
    # The has_proposal=no-proposal branch must not create a runtime failure.
    runs = recent_job_failures()
    assert runs == []


# ── semantic-drift gate ─────────────────────────────────────────────────


def test_passes_semantic_check_fail_open_when_model_call_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(side_effect=OSError("no upstream")),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is True
    assert "semantic check unavailable" in reason.lower()


def test_passes_semantic_check_returns_true_on_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(return_value="VERDICT: PRESERVED\nREASON: clarification only"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            model="kimi-k2.7-code:cloud",
        )
    )
    assert passed is True
    assert reason == "clarification only"


def test_passes_semantic_check_returns_false_on_drifted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# Skill\n")
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(return_value="VERDICT: DRIFTED\nREASON: changed the trigger keywords"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            model="kimi-k2.7-code:cloud",
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
    monkeypatch.setattr(
        "ciao.skill_evolution.run_oneshot",
        AsyncMock(return_value="I think it's fine?"),
    )
    passed, reason = asyncio.run(
        se.passes_semantic_check(
            skill_path, "some edit",
            model="kimi-k2.7-code:cloud",
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
    monkeypatch.setattr("ciao.skill_evolution.run_oneshot", pi_mock)

    paths = asyncio.run(
        se.run_evolution_pass(
            since_days=7,
            skills_root=skills_root,
            config=_config(tmp_path),
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
            config=_config(tmp_path),
            now=now,
            retention_months=6,
        )
    )
    assert paths == []
    # The ancient trajectory should be gone
    remaining = list(tb.list_trajectories())
    assert remaining == []


# ── skills-root resolution (post-2026-06-30 split) ─────────────────────


def test_resolve_skills_roots_uses_ciao_workspace_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the canonical user-owned skill directory is eligible."""
    ciao_root = tmp_path / "ciao"
    (ciao_root / "skills").mkdir(parents=True)
    (ciao_root / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setenv("CIAO_WORKSPACE", str(ciao_root))
    roots = se._resolve_skills_roots()
    assert roots == (ciao_root / "skills",)


def test_resolve_skills_roots_has_no_packaged_fallback_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    roots = se._resolve_skills_roots()
    assert roots == ()


def test_resolve_skills_roots_ignores_blank_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "   ")
    roots = se._resolve_skills_roots()
    assert roots == ()


# ── non-skill classifier (commands / subagents / external) ─────────────


def test_classify_non_skill_finds_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    (tmp_path / "commands").mkdir(parents=True)
    (tmp_path / "commands" / "critique.md").write_text("# critique")
    label, path = se._classify_non_skill("critique")
    assert label == "command"
    assert path == tmp_path / "commands" / "critique.md"


def test_classify_non_skill_finds_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    (tmp_path / "subagents").mkdir(parents=True)
    (tmp_path / "subagents" / "memory.md").write_text("# memory agent")
    label, path = se._classify_non_skill("memory")
    assert label == "subagent"
    assert path == tmp_path / "subagents" / "memory.md"


def test_classify_non_skill_falls_back_to_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    label, path = se._classify_non_skill("impeccable-skill-from-plugin")
    assert label == "external"
    assert path is None


def test_classify_non_skill_external_when_workspace_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    label, path = se._classify_non_skill("critique")
    assert label == "external"
    assert path is None
