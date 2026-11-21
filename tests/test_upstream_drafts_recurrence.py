"""The recurrence gate on new-skill drafts, and the pass-chat check (#3238-B).

A new skill is an owned file every session of the workspace loads, so a draft
derived from one task must show the procedure recurred across independent tasks
before it is queued. Every fixture is a throwaway vault under ``tmp_path``; no
test reaches the developer's vault, GitHub or real skills.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from ciao import cli, recurrence_evidence, upstream_drafts
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LearningObservation,
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    learning_key,
    parse_learnings,
    render_learning,
)
from ciao.task_board import TaskBoardStore
from ciao.task_resolution import Completion, append_completion
from ciao.upstream_drafts import (
    NEW_SKILL,
    UPSTREAM_ISSUE,
    DraftRefused,
    RecurrenceRefused,
    file_draft,
    read_queue,
    read_records,
    reject_draft,
)
from ciao.workspace_reroot import mark_born_per_root

PROCEDURE = "Retry a timed-out fetch once before reporting its result as absent."


def _config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={"work": WorkspaceConfig(name="work", vault_root="work")},
    )
    mark_born_per_root(tmp_path, runtime, ["work"])
    return config


def _vault(config: CiaoConfig) -> Path:
    return Path(config.workspace_vault_root("work"))


def _cid(n: int) -> str:
    return f"{n:032x}"


def _task(config: CiaoConfig, title: str, completion_ids: list[str]) -> str:
    body = ""
    for cid in completion_ids:
        body = append_completion(
            body,
            Completion(
                id=cid,
                completed_at=datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
                resolution="Done.",
            ),
        )
    store = TaskBoardStore(
        workspace="work",
        vault_root=_vault(config),
        runtime_dir=config.state_path.parent,
        clock=lambda: datetime.now(UTC),
    )
    return store.create(title=title, body=body).record.id


def _procedure(
    config: CiaoConfig, cited: list[str], text: str = PROCEDURE
) -> tuple[str, str]:
    """Write one learning whose sightings cite ``cited``; return (id, revision)."""
    learning_id = allocate_learning_id("work", text)
    record = LearningRecord(
        learning_id=learning_id,
        key=learning_key(text),
        text=text,
        first_seen=date(2026, 11, 1),
        last_seen=date(2026, 11, 2),
        count=len(cited),
        observations=tuple(LearningObservation(request=cid) for cid in cited),
    )
    path = _vault(config) / LEARNINGS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "## Active\n\n" + render_learning(record) + "\n", encoding="utf-8", newline=""
    )
    document = parse_learnings(path.read_text(encoding="utf-8"), workspace="work")
    stored = next(
        entry.record
        for entry in document.entries
        if entry.record is not None and entry.record.learning_id == learning_id
    )
    return learning_id, entry_revision(stored)


def _recurrence(procedure: str, revision: str, outcomes: list[str]) -> dict[str, Any]:
    return {
        "procedure_learning_id": procedure,
        "entry_revision": revision,
        "outcome_ids": outcomes,
    }


def _new_skill(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target": NEW_SKILL,
        "skill": "fetch-timeouts",
        "title": "Retry a timed-out fetch before calling it absent",
        "change": "Retry a timed-out fetch once before reporting its result as absent.",
    }
    payload.update(overrides)
    return payload


def _three_tasks(config: CiaoConfig) -> list[str]:
    """Three independent tasks, each with one completion, all cited by the procedure."""
    cids = [_cid(1), _cid(2), _cid(3)]
    for n, cid in enumerate(cids):
        _task(config, f"Task {n}", [cid])
    return cids


# ── The gate ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("supported", [1, 2])
def test_one_or_two_independent_outcomes_refuse_and_write_nothing(
    tmp_path: Path, supported: int
) -> None:
    config = _config(tmp_path)
    cids = [_cid(n) for n in range(1, supported + 1)]
    for n, cid in enumerate(cids):
        _task(config, f"Task {n}", [cid])
    procedure, revision = _procedure(config, cids)

    with pytest.raises(RecurrenceRefused) as exc:
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(procedure, revision, cids),
        )

    assert exc.value.status == "below"
    assert read_records(config, "work") == []
    assert read_queue(config, "work") == []
    assert not upstream_drafts.queue_path(config, "work").exists()


def test_three_distinct_tasks_file_one_pending_draft_with_its_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, revision = _procedure(config, cids)

    stored = file_draft(
        config,
        "work",
        **_new_skill(),
        recurrence=_recurrence(procedure, revision, cids),
    )

    assert stored.lifecycle == "pending"
    assert len(read_queue(config, "work")) == 1
    assert sorted(origin["completion_id"] for origin in stored.origins) == sorted(cids)
    assert {origin["learning_id"] for origin in stored.origins} == {procedure}


def test_repeated_ids_and_a_replay_of_one_task_do_not_progress(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # Two completions of one task, plus one other task: two outcomes, not three.
    _task(config, "Replayed", [_cid(1), _cid(2)])
    _task(config, "Other", [_cid(3)])
    cids = [_cid(1), _cid(2), _cid(3)]
    procedure, revision = _procedure(config, cids)

    with pytest.raises(RecurrenceRefused) as exc:
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(procedure, revision, [_cid(1), _cid(1), _cid(2), _cid(3)]),
        )

    assert exc.value.status == "below"
    assert read_records(config, "work") == []


def test_an_invented_id_never_counts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, revision = _procedure(config, cids)

    with pytest.raises(RecurrenceRefused) as exc:
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(procedure, revision, [cids[0], cids[1], _cid(99)]),
        )

    assert exc.value.status == "below"
    assert read_records(config, "work") == []


def test_a_malformed_task_file_is_unavailable_not_below(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, revision = _procedure(config, cids)
    (_vault(config) / "Workspace" / "Tasks" / f"{_cid(9)}.md").write_text(
        "garbage\n", encoding="utf-8"
    )

    with pytest.raises(RecurrenceRefused) as exc:
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(procedure, revision, cids),
        )

    assert exc.value.status == "unavailable"
    assert read_records(config, "work") == []


def test_a_stale_revision_refuses_before_any_task_is_read(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, _revision = _procedure(config, cids)

    with pytest.raises(DraftRefused, match="revision"):
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(procedure, "0" * 64, cids),
        )
    assert read_records(config, "work") == []


def test_a_procedure_not_in_the_learnings_refuses(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    _procedure(config, cids)

    with pytest.raises(DraftRefused, match="Learnings"):
        file_draft(
            config,
            "work",
            **_new_skill(),
            recurrence=_recurrence(allocate_learning_id("work", "Something else"), "0" * 64, cids),
        )


def test_the_claims_reach_the_classifier_filtered_then_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert at the seam: what the verdict is computed from.

    Nineteen ids are claimed: sixteen invented, then the three real ones. Filtering
    to the procedure's own sightings before the cap keeps the three; a cap applied
    first would keep only the invented sixteen and refuse as unavailable.
    """
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, revision = _procedure(config, cids)
    invented = [_cid(n) for n in range(100, 116)]
    claimed = invented + cids
    seen: dict[str, Any] = {}
    real_classify = upstream_drafts.classify_recurrence

    def spy(claims, resolved, **kwargs):
        seen["claims"] = list(claims)
        seen["resolved"] = dict(resolved)
        return real_classify(claims, resolved, **kwargs)

    monkeypatch.setattr(upstream_drafts, "classify_recurrence", spy)

    stored = file_draft(
        config,
        "work",
        **_new_skill(),
        recurrence=_recurrence(procedure, revision, claimed),
    )

    assert seen["claims"] == claimed
    assert set(seen["resolved"]) == set(cids)
    assert all(seen["resolved"][cid] is not None for cid in cids)
    assert len(stored.origins) == 3


def test_a_refiling_adds_only_unseen_evidence_and_never_reopens_a_rejection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    cids = [_cid(n) for n in range(1, 5)]
    for n, cid in enumerate(cids):
        _task(config, f"Task {n}", [cid])
    procedure, revision = _procedure(config, cids)
    first = file_draft(
        config,
        "work",
        **_new_skill(),
        recurrence=_recurrence(procedure, revision, cids[:3]),
    )
    reject_draft(config, first.id, reason="not this one")
    queued = upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8")

    again = file_draft(
        config,
        "work",
        **_new_skill(),
        recurrence=_recurrence(procedure, revision, cids),
    )
    once_more = file_draft(
        config,
        "work",
        **_new_skill(),
        recurrence=_recurrence(procedure, revision, cids),
    )

    assert again.id == first.id
    assert again.lifecycle == "rejected"
    assert sorted(origin["completion_id"] for origin in again.origins) == sorted(cids)
    assert len(once_more.origins) == 4
    assert read_queue(config, "work") == []
    assert upstream_drafts.queue_path(config, "work").read_text(encoding="utf-8") == queued


def test_the_manual_route_without_recurrence_is_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)

    stored = file_draft(config, "work", **_new_skill())

    assert stored.lifecycle == "pending"
    assert len(read_queue(config, "work")) == 1


def test_recurrence_on_an_upstream_issue_is_refused_by_name(tmp_path: Path) -> None:
    config = _config(tmp_path)
    cids = _three_tasks(config)
    procedure, revision = _procedure(config, cids)

    with pytest.raises(DraftRefused, match="new_skill"):
        file_draft(
            config,
            "work",
            target=UPSTREAM_ISSUE,
            skill="web-research",
            title="Document the offline fallback",
            change="State the retry rule.",
            recurrence=_recurrence(procedure, revision, cids),
        )


# ── The pass-chat check ─────────────────────────────────────────────────────


def _helper(**overrides: Any) -> dict[str, Any]:
    helper: dict[str, Any] = {
        "kind": "memory_pass",
        "source_kind": "task_completion",
        "completion_id": _cid(1),
        "task_path": f"Workspace/Tasks/{'a' * 32}.md",
        "resolution": "Done.",
        "resolution_sha256": "0" * 64,
        "state": "queued",
        "archive_policy": "when_clean",
    }
    helper.update(overrides)
    return helper


@pytest.mark.parametrize(
    ("target", "has_recurrence", "helper", "refused"),
    [
        (NEW_SKILL, False, _helper(), True),
        (NEW_SKILL, False, _helper(source_kind="", focus="approved_task"), True),
        (NEW_SKILL, True, _helper(), False),
        (NEW_SKILL, False, None, False),
        (NEW_SKILL, False, {"kind": "proposal"}, False),
        (NEW_SKILL, False, _helper(source_kind="project_closure"), False),
        (UPSTREAM_ISSUE, False, _helper(), False),
    ],
)
def test_the_pass_chat_decision_is_a_pure_function(
    target: str, has_recurrence: bool, helper: dict[str, Any] | None, refused: bool
) -> None:
    message = upstream_drafts.new_skill_pass_refusal(
        target, has_recurrence=has_recurrence, helper=helper
    )

    assert bool(message) is refused
    if refused:
        assert "recurrence" in message


def _chat_store(state_dir: Path, chat_id: str, helper: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "web_projects.json").write_text(
        json.dumps({"projects": {}, "chats": {chat_id: {"helper": helper}}}),
        encoding="utf-8",
    )


def test_the_chat_helper_is_read_from_the_chat_store(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _chat_store(config.state_path.parent, "chat-1", _helper())

    assert upstream_drafts.chat_helper(config, "chat-1") == _helper()
    assert upstream_drafts.chat_helper(config, "missing") is None
    assert upstream_drafts.chat_helper(config, "") is None


def test_an_unreadable_chat_store_reads_as_no_helper(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_path.parent.mkdir(parents=True, exist_ok=True)
    (config.state_path.parent / "web_projects.json").write_text("{not json", encoding="utf-8")

    assert upstream_drafts.chat_helper(config, "chat-1") is None


def test_the_claims_cap_is_a_named_constant() -> None:
    assert recurrence_evidence.MAX_IDS == 16
    assert upstream_drafts.MAX_OUTCOME_IDS >= recurrence_evidence.MAX_IDS


# ── The CLI ─────────────────────────────────────────────────────────────────


def _cli_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "memory-vault" / "personal" / "Workspace").mkdir(parents=True)
    return root


def _cli_config(root: Path) -> CiaoConfig:
    return CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(root),
        "CIAO_VAULT_ROOT": str(root / "memory-vault"),
    })


def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(root))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    monkeypatch.delenv("CIAO_CHAT_ID", raising=False)
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)


def _cli_payload(tmp_path: Path, **overrides: Any) -> str:
    path = tmp_path / "draft.json"
    payload: dict[str, Any] = {
        "target": NEW_SKILL,
        "skill": "fetch-timeouts",
        "title": "Retry a timed-out fetch before calling it absent",
        "change": "Retry a timed-out fetch once before reporting its result as absent.",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _cli_vault_procedure(root: Path, cids: list[str], n_tasks: int) -> tuple[str, str]:
    """Tasks and a learning in the ``personal`` vault of the CLI workspace."""
    vault = root / "memory-vault" / "personal"
    store = TaskBoardStore(
        workspace="personal",
        vault_root=vault,
        runtime_dir=root / ".runtime",
        clock=lambda: datetime.now(UTC),
    )
    for n, cid in enumerate(cids[:n_tasks]):
        body = append_completion(
            "",
            Completion(
                id=cid,
                completed_at=datetime(2026, 11, 2, 10, 0, tzinfo=UTC),
                resolution="Done.",
            ),
        )
        store.create(title=f"Task {n}", body=body)
    learning_id = allocate_learning_id("personal", PROCEDURE)
    record = LearningRecord(
        learning_id=learning_id,
        key=learning_key(PROCEDURE),
        text=PROCEDURE,
        first_seen=date(2026, 11, 1),
        last_seen=date(2026, 11, 2),
        count=len(cids),
        observations=tuple(LearningObservation(request=cid) for cid in cids),
    )
    path = vault / LEARNINGS_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("## Active\n\n" + render_learning(record) + "\n", encoding="utf-8", newline="")
    stored = next(
        entry.record
        for entry in parse_learnings(path.read_text(encoding="utf-8"), workspace="personal").entries
        if entry.record is not None and entry.record.learning_id == learning_id
    )
    return learning_id, entry_revision(stored)


def _cli_recurrence(procedure: str, revision: str, outcomes: list[str]) -> dict[str, Any]:
    return _recurrence(procedure, revision, outcomes)


def test_cli_recurrence_met_files_and_reports_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    cids = [_cid(1), _cid(2), _cid(3)]
    procedure, revision = _cli_vault_procedure(root, cids, 3)
    draft = _cli_payload(
        tmp_path, recurrence=_cli_recurrence(procedure, revision, cids)
    )

    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["filed"] is True
    assert payload["recurrence"] == "met"


def test_cli_recurrence_below_refuses_with_exit_2_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    cids = [_cid(1), _cid(2), _cid(3)]
    procedure, revision = _cli_vault_procedure(root, cids, 2)
    draft = _cli_payload(
        tmp_path, recurrence=_cli_recurrence(procedure, revision, cids)
    )

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 2

    err = capsys.readouterr().err
    assert "backed by 2 independent task(s)" in err
    assert "needs 3" in err
    assert read_records(_cli_config(root), "personal") == []


@pytest.mark.parametrize(
    ("recurrence", "field"),
    [
        ({"entry_revision": "a", "outcome_ids": [_cid(1)]}, "procedure_learning_id"),
        ({"procedure_learning_id": "x", "entry_revision": "a", "outcome_ids": "nope"}, "outcome_ids"),
        ({"procedure_learning_id": "x", "entry_revision": "a", "outcome_ids": []}, "outcome_ids"),
        ({"procedure_learning_id": "x", "entry_revision": "a", "outcome_ids": [1]}, "outcome_ids"),
        ({"procedure_learning_id": "x", "outcome_ids": [_cid(1)]}, "entry_revision"),
        (["not", "an", "object"], "recurrence"),
    ],
)
def test_cli_recurrence_block_names_the_field_it_cannot_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recurrence: Any,
    field: str,
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    draft = _cli_payload(tmp_path, recurrence=recurrence)

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 2
    assert field in capsys.readouterr().err


def test_cli_pass_chat_new_skill_without_recurrence_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    _chat_store(_cli_config(root).state_path.parent, "pass-chat", _helper())
    monkeypatch.setenv("CIAO_CHAT_ID", "pass-chat")

    assert cli.main(["skill-draft-add", "--input-file", _cli_payload(tmp_path)]) == 2

    err = capsys.readouterr().err
    assert "recurrence" in err
    assert read_records(_cli_config(root), "personal") == []


def test_cli_pass_chat_new_skill_with_recurrence_is_filed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    _chat_store(_cli_config(root).state_path.parent, "pass-chat", _helper())
    monkeypatch.setenv("CIAO_CHAT_ID", "pass-chat")
    cids = [_cid(1), _cid(2), _cid(3)]
    procedure, revision = _cli_vault_procedure(root, cids, 3)
    draft = _cli_payload(tmp_path, recurrence=_cli_recurrence(procedure, revision, cids))

    assert cli.main(["skill-draft-add", "--input-file", draft, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["recurrence"] == "met"


def test_cli_pass_chat_upstream_issue_is_not_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    _chat_store(_cli_config(root).state_path.parent, "pass-chat", _helper())
    monkeypatch.setenv("CIAO_CHAT_ID", "pass-chat")
    draft = _cli_payload(
        tmp_path,
        target=UPSTREAM_ISSUE,
        skill="web-research",
        title="Document the offline fallback",
        change="State the retry rule.",
        body="A fetch that times out is a transport failure, not an empty answer.",
        repository="example/tools",
    )

    assert cli.main(["skill-draft-add", "--input-file", draft]) == 0


def test_cli_without_a_chat_store_degrades_to_the_recurrence_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No store to consult is not a crash, and not a refusal of the manual route."""
    root = _cli_workspace(tmp_path)
    _cli_env(monkeypatch, tmp_path, root)
    monkeypatch.setenv("CIAO_CHAT_ID", "unknown-chat")

    assert cli.main(["skill-draft-add", "--input-file", _cli_payload(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["filed"] is True
