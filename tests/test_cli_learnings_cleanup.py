"""``ciao learnings-cleanup`` — the attended legacy-cleanup workflow.

Modelled on ``tests/test_cli_learnings_migrate.py`` and for the same reason: the
preview is the apply, dry-run is the default, and the receipt is the only way
back. What is different here is the approval, because this command removes lines
nobody has proposed and therefore lines a queue cannot speak for.

Every test here drives a throwaway vault under ``tmp_path``. None of them
contacts GitHub, reads a real vault, or leaves a lock file in the shared temp
root.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import cli
from ciao import learnings_cleanup as lc
from ciao import skill_proposals as sp
from ciao.learning_records import (
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    render_learning,
)
from ciao.memory_receipts import write_queue_atomically

WORKSPACE = "personal"
RETIRED = "Blocked pages need a fallback."
UNPROPOSED = "Rate limits are per-endpoint, not global."


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


# ── Fixtures ────────────────────────────────────────────────────────────────


def _record(text: str, key: str) -> LearningRecord:
    return LearningRecord(
        learning_id=allocate_learning_id(WORKSPACE, f"- {text}"), key=key, text=text
    )


RETIRED_RECORD = _record(RETIRED, "blocked-pages")
UNPROPOSED_RECORD = _record(UNPROPOSED, "rate-limits")


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: tuple[LearningRecord, ...],
    settled: bool = True,
) -> Path:
    """A workspace with a learnings document, and optionally a settled finding.

    The proposal is written straight to the queue rather than filed through
    ``ciao skill-proposal-add``, because a filed origin is always ``pending`` and
    these tests are about what happens *after* somebody has answered it.
    """
    workspace = tmp_path / "workspace"
    (workspace / "memory-vault" / WORKSPACE / "Workspace").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    body = "".join(f"{render_learning(record)}\n" for record in records)
    (workspace / "memory-vault" / WORKSPACE / "Workspace" / "Learnings.md").write_text(
        f"---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
        f"# Learnings\n\n## Active\n\n{body}",
        encoding="utf-8",
    )
    if settled:
        _settle(workspace, RETIRED_RECORD)
    return workspace / "memory-vault" / WORKSPACE


def _settle(workspace: Path, record: LearningRecord, skill: str = "web-research") -> None:
    proposal = sp.SkillProposal(
        id=sp.proposal_id(WORKSPACE, skill),
        workspace=WORKSPACE,
        skill=skill,
        canonical_path=f"/agent/skills/{skill}/SKILL.md",
        reviewed_revision="a" * 64,
        title=f"Skill reflection: {skill}",
        problem="Repeated fetch failures.",
        change="Add a defuddle fallback.",
        rationale="It handles blocked pages.",
        sources=(
            sp.SkillEvidence(
                chat_id="s", archive="2026-08-09T10:00:00Z", turn="", excerpt="e"
            ),
        ),
        lifecycle=sp.PENDING,
        chat_id="",
        updated_at="2026-08-09T10:00:00Z",
        origins=(
            sp.SkillOrigin(
                workspace=WORKSPACE,
                learning_id=record.learning_id,
                source_revision=entry_revision(record),
                finding="add the defuddle fallback",
                state=sp.ORIGIN_APPLIED,
                verification="mrcpt_0123456789abcdef",
            ),
        ),
    )
    path = (
        workspace / "memory-vault" / WORKSPACE / "Workspace" / "Skill-Proposals" / f"{skill}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    write_queue_atomically(path, sp.render_proposal(proposal))


def _approval(
    learning_id: str, revision: str, *, reapprove: bool = False, **extra: str
) -> list[dict[str, object]]:
    row: dict[str, object] = {
        "learning_id": learning_id,
        "entry_revision": revision,
        "reason": extra.get("reason", "the fallback step is in the skill"),
        "evidence": extra.get("evidence", "mrcpt_0123456789abcdef"),
    }
    if reapprove:
        row["reapprove"] = True
    return [row]


def _write_approval(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _learnings(vault: Path) -> str:
    """The document's current text, from the workspace vault root."""
    return (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")


def _run(*args: str) -> list[str]:
    # `--vault-root` is this *workspace's* vault root, exactly as it is for
    # `learnings-migrate`, and the workspace name defaults to that directory's own
    # name — which is the identity its learning ids were minted under.
    return ["learnings-cleanup", *args, "--vault-root", f"memory-vault/{WORKSPACE}"]


# ── The dry run ─────────────────────────────────────────────────────────────


def test_the_dry_run_lists_every_active_entry_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every row, not just the removable ones.

    The rows this command refuses to act on are the ones an operator most needs to
    see. A table of only the candidates would answer "an entry nothing has ever
    asked about" by omission, and *it isn't in the list* is not an answer a person
    can act on.
    """
    vault = _install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    before = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")

    assert cli.main(_run("--runtime-root", str(tmp_path / ".runtime"))) == 0

    out = capsys.readouterr().out
    assert "2 Active entr(y/ies)" in out
    assert "blocked-pages" in out
    assert "rate-limits" in out
    assert "REMOVE" in out
    assert "no proposal has ever linked" in out
    assert (
        "1 removable, 1 kept, 0 unreadable, 0 routed upstream "
        "(waiting on another maintainer), 1 not linked to anything yet."
    ) in out
    assert "Nothing was written" in out
    assert (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8") == before
    assert not (vault / "Workspace" / "learnings-cleanup.json").exists()
    assert not (tmp_path / ".runtime" / "migration").exists()


def test_the_table_shows_the_evidence_a_removal_rests_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row that removes somebody's notes on the strength of "it was handled"
    without saying how is the failure this command exists to make impossible."""
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))

    assert cli.main(_run()) == 0

    out = capsys.readouterr().out
    assert "web-research: applied (mrcpt_0123456789abcdef)" in out
    # And the line itself, uncut, on its own row: the key is a label, the statement
    # is what a reviewer reads.
    assert render_learning(RETIRED_RECORD)[:60] in out
    # The two values an approval names, in full. An approval is bound to the exact
    # bytes it was reviewed at, so an abbreviated id or revision would be an
    # approval of nothing.
    assert f"id {RETIRED_RECORD.learning_id}" in out
    assert f"rev {entry_revision(RETIRED_RECORD)}" in out


def test_the_json_output_carries_the_whole_plan_and_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    before = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")

    assert cli.main(_run("--json")) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["plan"]["counts"] == {
        "active": 2,
        "remove": 1,
        "keep": 1,
        "conflict": 0,
        "routes": 0,
        "unmatched": 1,
    }
    assert payload["plan"]["removals"][0]["entry_revision"] == entry_revision(
        RETIRED_RECORD
    )
    # A dry run has no result: nothing happened, and a preview that reported one
    # would be claiming an outcome it did not produce.
    assert payload["result"] is None
    assert (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8") == before


def test_a_missing_vault_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")

    assert cli.main(_run()) == 1
    assert "missing or not a directory" in capsys.readouterr().err


def test_an_unreadable_line_exits_nonzero_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 for anything a person has to look at, not only for failure: an
    unreadable line did not fail, and a status of 0 would tell a script it needs no
    attention — which is the condition that leaves it unreadable forever."""
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    path = vault / "Workspace" / "Learnings.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "- [broken] [2024-13-45 → nope] (x0) Bad dates.\n",
        encoding="utf-8",
    )

    assert cli.main(_run()) == 1
    captured = capsys.readouterr()
    assert "1 unreadable" in captured.out
    # The whole diagnostic, on stderr, because the reason column is too narrow to
    # say why and this is the line a person has to go and look at.
    assert "neither a real date" in captured.err
    assert "x0 is not a positive count" in captured.err


# ── Approval ────────────────────────────────────────────────────────────────


def test_apply_without_an_approval_file_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole attended contract, in one refusal.

    A cleanup that runs because a flag was passed has had its judgement from a
    flag, and the rows that matter — a legacy entry nothing has proposed, an entry
    a person decided was obsolete — are exactly the rows a flag cannot judge.
    """
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    before = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")

    assert cli.main(_run("--apply")) == 1
    assert "--apply needs --approval-file" in capsys.readouterr().err
    assert (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8") == before


def test_an_approved_row_is_removed_and_the_rest_of_the_file_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    path = vault / "Workspace" / "Learnings.md"
    before = path.read_text(encoding="utf-8")
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )

    assert cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    ) == 0

    out = capsys.readouterr().out
    after = path.read_text(encoding="utf-8")
    assert "Removed 1 entr(y/ies)." in out
    assert render_learning(RETIRED_RECORD) not in after
    assert render_learning(UNPROPOSED_RECORD) in after
    today = lc._now()[:10]
    assert after == (
        before.replace(f"{render_learning(RETIRED_RECORD)}\n", "")
        .replace("updated: 2020-01-01", f"updated: {today}")
    )
    # The receipt is the way back, and the command says so.
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    assert f"--revert {receipt} --apply" in out
    stored = json.loads(receipt.read_text(encoding="utf-8"))
    assert stored["entries_removed"] == 1
    assert stored["approvals"][RETIRED_RECORD.learning_id]["reason"]


def test_only_the_approved_rows_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Approving one row of two settles one row. An approval is a judgement about
    one entry, and applying it to its neighbour would be the tool deciding.

    The other row is still eligible and is printed as ``LATER`` rather than
    ``REMOVE``: it is real work for another run, and a table whose apply left a
    line in place must not label it as removed.
    """
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD))
    _settle(
        tmp_path / "workspace",
        UNPROPOSED_RECORD,
        skill="rate-limits",
    )
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 0
    assert "LATER" in capsys.readouterr().out

    vault = tmp_path / "workspace" / "memory-vault" / WORKSPACE
    body = _learnings(vault)
    assert render_learning(RETIRED_RECORD) not in body
    assert render_learning(UNPROPOSED_RECORD) in body


def test_a_stale_approval_is_reported_and_nothing_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's judgement was about specific text, and text that has since
    changed is not that text.

    So a stale approval is refreshed and re-confirmed rather than honoured against
    the closest thing: the entry moved, the settlement changed, or the review was
    of a different document.
    """
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, "0" * 64)
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 1

    err = capsys.readouterr().err
    assert "approval not used" in err
    assert "this entry is now" in err
    assert render_learning(RETIRED_RECORD) in (
        vault / "Workspace" / "Learnings.md"
    ).read_text(encoding="utf-8")


def test_an_approval_naming_a_learning_that_is_gone_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    approval = _write_approval(
        tmp_path, _approval("11111111-2222-3333-4444-555555555555", entry_revision(RETIRED_RECORD))
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 1
    assert "no Active entry in this document has that learning id" in capsys.readouterr().err
    assert render_learning(RETIRED_RECORD) in (
        vault / "Workspace" / "Learnings.md"
    ).read_text(encoding="utf-8")


def test_retiring_a_kept_row_needs_the_reapproval_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Obsolete classification is a judgement, and it is the one judgement this
    command must be able to record.

    An entry nothing has ever proposed cannot be retired by the planner, because
    the planner has no evidence for it. A person can — with a stated reason and
    the evidence for it, both of which the receipt keeps — and has to say
    ``reapprove`` so that "I looked at this and it should go anyway" is on the
    record rather than inferred from an approval row.
    """
    vault = _install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    path = vault / "Workspace" / "Learnings.md"
    approval = _write_approval(
        tmp_path,
        _approval(
            UNPROPOSED_RECORD.learning_id,
            entry_revision(UNPROPOSED_RECORD),
            reason="the limit is per-endpoint now; the lesson is obsolete",
            evidence="owner/repo#412 closed the old behaviour",
        ),
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 1
    assert "an approval to retire a kept entry has to say reapprove" in capsys.readouterr().err
    assert render_learning(UNPROPOSED_RECORD) in path.read_text(encoding="utf-8")

    approval = _write_approval(
        tmp_path,
        _approval(
            UNPROPOSED_RECORD.learning_id,
            entry_revision(UNPROPOSED_RECORD),
            reapprove=True,
            reason="the limit is per-endpoint now; the lesson is obsolete",
            evidence="owner/repo#412 closed the old behaviour",
        ),
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 0
    assert render_learning(UNPROPOSED_RECORD) not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "because"),
    [
        ({"nonsense": True}, "must hold a list"),
        (["not an object"], "approval 0 must be an object"),
        ([{"learning_id": "a"}], 'needs a non-empty "entry_revision"'),
        ([{"learning_id": "a", "entry_revision": "b"}], 'needs a non-empty "reason"'),
        (
            [{"learning_id": "a", "entry_revision": "b", "reason": "c"}],
            'needs a non-empty "evidence"',
        ),
        ([{"learning_id": "a", "entry_revision": "b", "reason": "c", "evidence": "d"}] * 2,
         "names a twice"),
    ],
)
def test_a_malformed_approval_file_is_refused_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: object,
    because: str,
) -> None:
    """A partially-read approval file is an approval of a subset nobody chose, so
    it is refused rather than trimmed."""
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cli.main(_run("--apply", "--approval-file", str(path))) == 1
    assert because in capsys.readouterr().err
    assert render_learning(RETIRED_RECORD) in (
        vault / "Workspace" / "Learnings.md"
    ).read_text(encoding="utf-8")


def test_an_unreadable_approval_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    path = tmp_path / "approved.json"
    path.write_text("{ not json", encoding="utf-8")

    assert cli.main(_run("--apply", "--approval-file", str(path))) == 1
    assert "cannot read the approval file" in capsys.readouterr().err


def test_a_settled_but_unapproved_row_is_reported_as_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removable is not approved. The table says both and the apply honours only
    one, so a reviewer who approves three of four rows gets exactly three — and
    the fourth is reported as still to do rather than as a problem with the run."""
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD))
    _settle(tmp_path / "workspace", UNPROPOSED_RECORD, skill="rate-limits")
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 0
    out = capsys.readouterr().out
    body = _learnings(tmp_path / "workspace" / "memory-vault" / WORKSPACE)
    assert render_learning(UNPROPOSED_RECORD) in body
    assert "LATER" in out
    assert "eligible; not approved in this run" in out


# ── The reviewed no-op ──────────────────────────────────────────────────────


def test_a_review_that_removes_nothing_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The difference between "a person looked and agreed" and "nobody looked".

    A table is not a review and an approval nobody gave is not a review, and both
    of those leave no receipt. The update-task completion check needs the third
    thing to be distinguishable from both, so an empty approval list is a valid
    answer and it does write a receipt.
    """
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,), settled=False)
    approval = _write_approval(tmp_path, [])

    assert cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    ) == 0

    out = capsys.readouterr().out
    assert "the review itself is recorded" in out
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    stored = json.loads(receipt.read_text(encoding="utf-8"))
    assert stored["entries_removed"] == 0
    assert stored["approvals"] == {}


def test_a_second_apply_of_the_same_approval_removes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotence, end to end through the command: the second run finds the
    entry gone and the approval stale, and writes nothing."""
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )

    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 0
    vault = tmp_path / "workspace" / "memory-vault" / WORKSPACE
    body = _learnings(vault)

    # Nothing moved. The exit is 1 because the approval names a learning this
    # document no longer has, and an operator who re-runs a command should be told
    # that their second run did not do the thing rather than left to assume it.
    assert cli.main(_run("--apply", "--approval-file", str(approval))) == 1
    assert "no Active entry in this document has that learning id" in (
        capsys.readouterr().err
    )
    assert _learnings(vault) == body


# ── The undo ────────────────────────────────────────────────────────────────


def test_the_undo_restores_the_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD))
    path = vault / "Workspace" / "Learnings.md"
    before = path.read_text(encoding="utf-8")
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )
    cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    )
    capsys.readouterr()
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))

    assert cli.main(
        _run("--revert", str(receipt), "--apply", "--runtime-root", str(tmp_path / ".runtime"))
    ) == 0

    out = capsys.readouterr().out
    assert "Restored 1 learning entr(y/ies)." in out
    # Exact, modulo the one field a write is allowed to move.
    assert path.read_text(encoding="utf-8") == before.replace(
        "updated: 2020-01-01", f"updated: {lc._now()[:10]}"
    )
    assert "still recorded as removed" in out


def test_a_dry_run_undo_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    path = vault / "Workspace" / "Learnings.md"
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )
    cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    )
    capsys.readouterr()
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    after = path.read_text(encoding="utf-8")

    assert cli.main(_run("--revert", str(receipt))) == 0
    out = capsys.readouterr().out
    assert "Would restore 1 learning entr(y/ies)." in out
    assert "Re-run with --apply" in out
    assert path.read_text(encoding="utf-8") == after


def test_an_undo_does_not_let_the_next_pass_undo_the_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suppression survives the undo, which is the whole reason it exists.

    An undo puts the entry back for a person to read. If the nightly pass then
    removed it again on the next run, ``--revert`` would be a button that does
    nothing — so the restored entry is reported as ``suppressed`` until it is
    edited (a new revision) or somebody reapproves it.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig

    vault = _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )
    cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    )
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    cli.main(_run("--revert", str(receipt), "--apply"))
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path / "workspace",
        state_path=tmp_path / "workspace" / ".runtime" / "state.json",
        media_root=tmp_path / "workspace" / ".runtime" / "media",
        vault_root=tmp_path / "workspace" / "memory-vault",
        workspaces={
            WORKSPACE: WorkspaceConfig(
                name=WORKSPACE, vault_root="memory-vault/personal"
            )
        },
    )

    plan = lc.plan_cleanup(vault, workspace=WORKSPACE, config=config)

    assert plan.removals == ()
    assert [(row.key, row.reason) for row in plan.kept] == [
        ("blocked-pages", "suppressed")
    ]


def test_an_undo_from_another_vault_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The receipt's offsets are offsets into one specific file, so reversing from a
    different root reads the wrong bytes at those offsets — and the check that
    would notice is itself checking the wrong document."""
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )
    cli.main(
        _run("--apply", "--approval-file", str(approval), "--runtime-root", str(tmp_path / ".runtime"))
    )
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    other = tmp_path / "other" / "memory-vault" / "personal"
    (other / "Workspace").mkdir(parents=True)

    assert (
        cli.main(
            [
                "learnings-cleanup",
                "--revert",
                str(receipt),
                "--apply",
                "--vault-root",
                str(other),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "Re-run with `--vault-root" in err


def test_an_unreadable_receipt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    assert cli.main(_run("--revert", str(path), "--apply")) == 1
    assert "Not a readable learnings-cleanup receipt" in capsys.readouterr().err
