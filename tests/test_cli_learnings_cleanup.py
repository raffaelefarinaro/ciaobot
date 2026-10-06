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
from types import SimpleNamespace

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
SECOND_RECORD = _record("Long transcripts need chunking.", "long-transcripts")
THIRD_RECORD = _record("Session logs outgrow the notes.", "log-growth")
FIFTH_RECORD = _record("Promotions need a dated review.", "dated-review")

#: Five Active entries, so three of them can be removed in one run in every gap
#: shape: three separate gaps, one gap shared by a removed pair, and one gap
#: shared by a whole run of them. A run of one cannot tell those apart, which is
#: why a single-removal revert test proves nothing about a batch.
FIVE_RECORDS = (
    RETIRED_RECORD,
    SECOND_RECORD,
    UNPROPOSED_RECORD,
    THIRD_RECORD,
    FIFTH_RECORD,
)

_REMOVAL_SHAPES: tuple[tuple[LearningRecord, ...], ...] = (
    (RETIRED_RECORD, UNPROPOSED_RECORD, FIFTH_RECORD),
    (RETIRED_RECORD, SECOND_RECORD, THIRD_RECORD),
    (RETIRED_RECORD, SECOND_RECORD, UNPROPOSED_RECORD),
)


def _install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: tuple[LearningRecord, ...],
    settled: bool = True,
    bom: str = "",
    newline: str = "\n",
) -> Path:
    """A workspace with a learnings document, and optionally a settled finding.

    The proposal is written straight to the queue rather than filed through
    ``ciao skill-proposal-add``, because a filed origin is always ``pending`` and
    these tests are about what happens *after* somebody has answered it.

    ``bom`` and ``newline`` write bytes rather than text, because a CRLF document
    read through ``read_text`` comes back LF — the byte-level claims would then
    hold of the preview and be false of the file.
    """
    workspace = tmp_path / "workspace"
    (workspace / "memory-vault" / WORKSPACE / "Workspace").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    body = "".join(f"{render_learning(record)}\n" for record in records)
    document = (
        f"---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
        f"# Learnings\n\n## Active\n\n{body}"
    )
    (
        workspace / "memory-vault" / WORKSPACE / "Workspace" / "Learnings.md"
    ).write_bytes((bom + document).replace("\n", newline).encode("utf-8"))
    if settled:
        _settle(workspace, RETIRED_RECORD)
    return workspace / "memory-vault" / WORKSPACE


def _settle(workspace: Path, *records: LearningRecord, skill: str = "web-research") -> None:
    """One proposal in the queue, carrying a settled finding per given record.

    Variadic because the interesting removals are batches: three entries decided
    in one fold, spliced by one apply, and reversed from one receipt.
    """
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
        origins=tuple(
            sp.SkillOrigin(
                workspace=WORKSPACE,
                learning_id=record.learning_id,
                source_revision=entry_revision(record),
                finding=f"the finding for {record.key}",
                state=sp.ORIGIN_APPLIED,
                verification="mrcpt_0123456789abcdef",
            )
            for record in records
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


def _approvals(*records: LearningRecord) -> list[dict[str, object]]:
    """One approved row per record, each bound to the revision it was reviewed at."""
    return [
        {
            "learning_id": record.learning_id,
            "entry_revision": entry_revision(record),
            "reason": f"the finding for {record.key} is settled",
            "evidence": "mrcpt_0123456789abcdef",
        }
        for record in records
    ]


def _learnings(vault: Path) -> str:
    """The document's current text, from the workspace vault root."""
    return (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")


def _run(*args: str) -> list[str]:
    # `--vault-root` is this *workspace's* vault root, exactly as it is for
    # `learnings-migrate`. The workspace name is resolved through the registry:
    # the default registry in this harness names `personal` → `memory-vault/personal`.
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


@pytest.mark.parametrize(
    "removed",
    _REMOVAL_SHAPES,
    ids=["separate-gaps", "shared-pair", "one-run"],
)
def test_three_removals_revert_to_the_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    removed: tuple[LearningRecord, ...],
) -> None:
    """A batch is reversible, byte for byte, through the command as well.

    The failure this pins is the one the whole review turned on: a receipt's
    offsets address the text the run *left*, and for the second and later removals
    in a run that is not the text it read. Recording a span's own start anyway put
    the bytes back in the wrong places, silently, because the anchors were sampled
    from those same wrong offsets — the undo's own check agreed with itself. Only
    a run of two or more shows it, and only a run of three with a survivor between
    two of them shows it *and* keeps the other entries in place to check.

    CRLF and a BOM throughout, because the offsets are byte offsets and a reader
    that normalises newlines is a reader addressing a different document.
    """
    vault = _install(
        tmp_path, monkeypatch, records=FIVE_RECORDS, bom="﻿", newline="\r\n"
    )
    path = vault / "Workspace" / "Learnings.md"
    before = path.read_bytes()
    _settle(tmp_path / "workspace", *removed)
    approval = _write_approval(tmp_path, _approvals(*removed))
    runtime = str(tmp_path / ".runtime")

    cli.main(_run("--apply", "--approval-file", str(approval), "--runtime-root", runtime))
    capsys.readouterr()
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))

    assert cli.main(_run("--revert", str(receipt), "--apply", "--runtime-root", runtime)) == 0

    out = capsys.readouterr().out
    assert "Restored 3 learning entr(y/ies)." in out
    assert path.read_bytes() == before.replace(
        b"updated: 2020-01-01", f"updated: {lc._now()[:10]}".encode()
    )


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


# ── The unattended mode (#728-E, review round 1) ────────────────────────────
#
# The policy has always said the nightly run may retire a settled learning while
# nothing in the code could: every apply needed `--approval-file`, so the worklist
# item named a work nobody could do without a person. `--apply-settled` is the
# other half — the same fold, the same splice, the same receipt, and no judgement
# of anything the fold did not already answer.


def test_the_unattended_mode_needs_no_approval_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gap this mode closes, in one assertion: a run with nobody in it removes
    a settled entry, and the next run with nobody in it removes nothing."""
    vault = _install(
        tmp_path,
        monkeypatch,
        records=(RETIRED_RECORD, UNPROPOSED_RECORD, SECOND_RECORD),
    )
    _settle(tmp_path / "workspace", RETIRED_RECORD, SECOND_RECORD)
    runtime = str(tmp_path / ".runtime")

    assert cli.main(_run("--apply-settled", "--runtime-root", runtime)) == 0

    out = capsys.readouterr().out
    after = _learnings(vault)
    assert "Removed 2 entr(y/ies)." in out
    assert render_learning(RETIRED_RECORD) not in after
    assert render_learning(SECOND_RECORD) not in after
    # The row the reconciliation kept is untouched: nothing has ever linked a
    # finding to it, and an unattended run has no opinion about that.
    assert render_learning(UNPROPOSED_RECORD) in after
    # It is a receipt, on the same terms as every other removal, so the reversal
    # is the command's own.
    receipt = next((tmp_path / ".runtime" / "migration").glob("learnings-cleanup-*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["removed_by"] == "system"
    capsys.readouterr()
    assert cli.main(_run("--revert", str(receipt), "--apply", "--runtime-root", runtime)) == 0
    capsys.readouterr()

    # A second unattended run is a no-op: the entries are gone, and the ones an
    # undo put back are suppressed rather than removed again.
    assert cli.main(_run("--apply-settled", "--runtime-root", runtime)) == 0
    out = capsys.readouterr().out
    assert "Nothing to remove." in out
    assert "0 removable" in out
    assert all(
        render_learning(record) in _learnings(vault)
        for record in (RETIRED_RECORD, SECOND_RECORD, UNPROPOSED_RECORD)
    )


def test_the_unattended_mode_takes_the_pass_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backlog allowed to drain at full speed takes the whole night's budget and
    the required weekly hygiene keys are never reached, so ``last_full_pass`` can
    never advance. The cap is what keeps this a pass rather than a takeover, and it
    is the *plan* that is capped too — a table printing ``REMOVE`` beside a row
    this run will not remove is a table that lies about what it did."""
    from ciao.curation_run import LEARNINGS_CLEANUP_MAX_ITEMS

    records = tuple(
        _record(f"Lesson {index} is settled.", f"lesson-{index}")
        for index in range(LEARNINGS_CLEANUP_MAX_ITEMS + 3)
    )
    vault = _install(tmp_path, monkeypatch, records=records, settled=False)
    _settle(tmp_path / "workspace", *records)
    runtime = str(tmp_path / ".runtime")

    assert cli.main(_run("--apply-settled", "--runtime-root", runtime)) == 0

    out = capsys.readouterr().out
    assert f"Removed {LEARNINGS_CLEANUP_MAX_ITEMS} entr(y/ies)." in out
    assert "More are settled than one run retires" in out
    # The rows behind the cap are marked as waiting rather than as removed, so a
    # reader of the table is not misled about either.
    assert out.count("REMOVE") == LEARNINGS_CLEANUP_MAX_ITEMS
    assert out.count("LATER") == 3
    assert out.count("this run is capped at") == 3

    # And the backlog drains over the next runs rather than stalling.
    for _ in range(3):
        cli.main(_run("--apply-settled", "--runtime-root", runtime))
        capsys.readouterr()
    assert all(render_learning(record) not in _learnings(vault) for record in records)


def test_the_unattended_mode_is_not_the_attended_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two decisions, so two flags, and a run may only make one of them.

    ``--apply-settled`` retires the rows the reconciliation proposed and writes no
    review, so accepting an approval file alongside it would mean a receipt holding
    somebody's reasons and evidence for rows a flag chose, with no way to tell
    afterwards which was which. Refused rather than reconciled.
    """
    _install(tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD))
    approval = _write_approval(
        tmp_path, _approval(RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    )
    runtime = str(tmp_path / ".runtime")

    for extra, expected in (
        (["--approval-file", str(approval)], "cannot be combined"),
        (["--apply"], "two different decisions"),
        (["--revert", str(approval)], "takes no --apply-settled"),
    ):
        capsys.readouterr()
        assert cli.main(_run(*extra, "--apply-settled", "--runtime-root", runtime)) == 1
        assert expected in capsys.readouterr().err
    assert render_learning(RETIRED_RECORD) in _learnings(
        tmp_path / "workspace" / "memory-vault" / WORKSPACE
    )


def test_the_unattended_mode_says_when_there_is_nothing_to_retire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run with nothing to do is still a run: it reports the whole Active list,
    and it writes no receipt claiming a review nobody made."""
    vault = _install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD), settled=False
    )
    before = (vault / "Workspace" / "Learnings.md").read_bytes()

    assert cli.main(_run("--apply-settled")) == 0

    out = capsys.readouterr().out
    assert "2 Active entr(y/ies)" in out
    assert "0 removable" in out
    assert (vault / "Workspace" / "Learnings.md").read_bytes() == before
    assert not (tmp_path / ".runtime" / "migration").exists()


# ── Which workspace a run is about (#912) ───────────────────────────────────
#
# On a per-root install every workspace's vault directory is called
# `memory-vault`, so the directory's own name is not an identity: a receipt
# written under it names a workspace the update-task check has never heard of,
# and a fold keyed by it looks for a workspace segment the vault does not have.
# The registered name is the only scope, and it comes from the registry.


def _per_root_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    records: tuple[LearningRecord, ...],
    settled: bool = True,
) -> Path:
    """The layout a re-rooted install has: one `memory-vault` per workspace.

    Built by hand for the same reason as the rest of this module's fixtures —
    the shape being claimed is the one a real per-root install has, and the
    registry file is what every consumer of a workspace name reads. The
    environment is pinned because `_resolve_vault_root` reads `CIAO_VAULT_ROOT`
    and resolves a relative default against `CIAO_WORKSPACE`, so an ambient
    value from the developer's shell would decide what this run is about.
    """
    install = tmp_path / "install"
    vault = install / WORKSPACE / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": WORKSPACE, "vault_root": str(vault)}]), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(install))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(runtime))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    body = "".join(f"{render_learning(record)}\n" for record in records)
    (vault / "Workspace" / "Learnings.md").write_text(
        f"---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
        f"# Learnings\n\n## Active\n\n{body}",
        encoding="utf-8",
    )
    if settled:
        _settle_in(vault, RETIRED_RECORD)
    return vault


def _settle_in(vault: Path, *records: LearningRecord, skill: str = "web-research") -> None:
    """One settled finding per record, in *this* vault's proposal queue.

    The same shape :func:`_settle` files, addressed from the vault rather than
    from the workspace directory, because a per-root vault is not under the
    shared `memory-vault/<workspace>` layout at all.
    """
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
        origins=tuple(
            sp.SkillOrigin(
                workspace=WORKSPACE,
                learning_id=record.learning_id,
                source_revision=entry_revision(record),
                finding=f"the finding for {record.key}",
                state=sp.ORIGIN_APPLIED,
                verification="mrcpt_0123456789abcdef",
            )
            for record in records
        ),
    )
    path = vault / "Workspace" / "Skill-Proposals" / f"{skill}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_queue_atomically(path, sp.render_proposal(proposal))


def test_a_per_root_vault_resolves_its_registered_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--vault-root memory-vault` must still reconcile the workspace `personal`.

    Both halves of the old bug are pinned by one run. The receipt named the
    directory, so the update-task check — which compares receipts against
    registered names — never saw the review as done; and the registry the fold
    read was keyed `memory-vault`, so `workspace_vault_root` looked under
    `memory-vault/personal` inside a vault that has no segments, read no
    proposals at all, and reported every entry as never proposed.
    """
    vault = _per_root_install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    approval = _write_approval(tmp_path, _approvals(RETIRED_RECORD))
    runtime = tmp_path / "install" / ".runtime"

    assert cli.main(
        [
            "learnings-cleanup",
            "--apply",
            "--approval-file",
            str(approval),
            "--runtime-root",
            str(runtime),
            "--vault-root",
            str(vault),
        ]
    ) == 0

    receipt = next((runtime / "migration").glob("learnings-cleanup-*.json"))
    stored = json.loads(receipt.read_text(encoding="utf-8"))
    assert stored["workspace"] == WORKSPACE
    assert stored["vault_root"] == str(vault)
    # The fold really read THIS vault's queue: the settled finding was found and
    # the entry retired, while its unproposed neighbour is left alone.
    assert "Removed 1 entr(y/ies)." in capsys.readouterr().out
    assert render_learning(RETIRED_RECORD) not in _learnings(vault)
    assert render_learning(UNPROPOSED_RECORD) in _learnings(vault)


def test_workspace_alone_finds_the_registered_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Naming the workspace is enough — the vault comes from the registry.

    The per-root case is where an operator is most likely to have to say which
    workspace they mean, because the directory names say nothing: two of them
    are `memory-vault`. That the settlement is found is also the proof that the
    vault really was this one — the fold read the proposal queue under the
    workspace segment the registry resolves, and a per-root vault has no
    segments to fall back on.
    """
    vault = _per_root_install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )

    assert cli.main(
        [
            "learnings-cleanup",
            "--workspace",
            WORKSPACE,
            "--json",
            "--runtime-root",
            str(tmp_path / "install" / ".runtime"),
        ]
    ) == 0

    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["workspace"] == WORKSPACE
    assert plan["path"] == "Workspace/Learnings.md"
    assert (vault / "Workspace" / "Skill-Proposals" / "web-research.md").is_file()
    assert [row["learning_id"] for row in plan["removals"]] == [
        RETIRED_RECORD.learning_id
    ]
    # The settlement is the fold's own answer about this vault's queue, so a
    # removal here is a removal nothing else could have proposed.
    assert "applied or dismissed" in plan["removals"][0]["detail"]
    assert plan["counts"]["active"] == 2
    assert plan["counts"]["remove"] == 1


def test_an_unknown_workspace_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Refused, not repaired.

    Guessing a name for a workspace that is not registered would write a receipt
    naming a workspace nothing resolves, which is the failure this whole command
    path exists to remove. The message lists what *is* registered, so the
    operator does not have to go and look.
    """
    _per_root_install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))

    assert cli.main(["learnings-cleanup", "--workspace", "nope"]) == 1

    err = capsys.readouterr().err
    assert "Unknown workspace `nope`" in err
    assert WORKSPACE in err


def test_a_vault_root_that_is_not_the_workspaces_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both names are explicit and they disagree, so the run does not happen.

    Reconciling one vault under another workspace's name produces a receipt the
    update-task check reads as that other workspace's review, over a document
    the operator never looked at. Both paths are printed, because the fix is to
    drop one of the two arguments.
    """
    vault = _per_root_install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    other = tmp_path / "elsewhere"
    other.mkdir()

    assert cli.main(
        ["learnings-cleanup", "--workspace", WORKSPACE, "--vault-root", str(other)]
    ) == 1

    err = capsys.readouterr().err
    assert f"is not workspace `{WORKSPACE}`'s vault" in err
    assert str(other) in err
    assert str(vault) in err
    assert _learnings(vault) == (
        f"---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
        f"# Learnings\n\n## Active\n\n{render_learning(RETIRED_RECORD)}\n"
    )


def test_a_bare_shell_resolves_the_installed_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported case: no `CIAO_WORKSPACE`, just a terminal.

    A plain shell has no workspace in the environment, and handing `from_env` an
    explicit dict makes it skip the LaunchAgent lookup a bare invocation gets, so
    the run reads the bootstrap registry — which knows no vault, and mints a
    secret under `~/.ciao/bootstrap`. Then `--vault-root memory-vault` resolves
    to the directory's own name and the receipt is #912 all over again. The
    install the operator actually has is what the running server's LaunchAgent
    points at, so that is what this asks.
    """
    vault = _per_root_install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    approval = _write_approval(tmp_path, _approvals(RETIRED_RECORD))
    runtime = tmp_path / "install" / ".runtime"
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(tmp_path / "install"), runtime_root=str(runtime)
        ),
    )

    assert cli.main(
        [
            "learnings-cleanup",
            "--apply",
            "--approval-file",
            str(approval),
            "--runtime-root",
            str(runtime),
            "--vault-root",
            str(vault),
        ]
    ) == 0

    receipt = next((runtime / "migration").glob("learnings-cleanup-*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["workspace"] == WORKSPACE
    assert "Removed 1 entr(y/ies)." in capsys.readouterr().out
    assert render_learning(RETIRED_RECORD) not in _learnings(vault)
    # A read-only name resolution must not manufacture an install beside the real
    # one: the whole reason discovery is consulted is to avoid that bootstrap root.
    assert not (tmp_path / "home" / ".ciao").exists()


def test_a_bare_shell_writes_the_receipt_to_the_installed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No flags naming a runtime either: the receipt must reach the install.

    `--runtime-root` is what the test above passed, and passing it hid the half of
    #912 that round 1 left standing. `_resolve_runtime_root` reads only
    `os.environ`, so with neither `CIAO_RUNTIME_ROOT` nor the flag it answers
    `<cwd>/.runtime` — while `update_tasks` reads receipts from
    `<install>/.runtime/migration`. A receipt in the shell's home is a review the
    update task never sees, so the entry resurfaces forever: the run does its work
    and the task that would retire it never completes.
    """
    vault = _per_root_install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    approval = _write_approval(tmp_path, _approvals(RETIRED_RECORD))
    runtime = tmp_path / "install" / ".runtime"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(tmp_path / "install"), runtime_root=str(runtime)
        ),
    )

    assert cli.main(
        [
            "learnings-cleanup",
            "--apply",
            "--approval-file",
            str(approval),
            "--vault-root",
            str(vault),
        ]
    ) == 0

    receipt = next((runtime / "migration").glob("learnings-cleanup-*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["workspace"] == WORKSPACE
    assert "Removed 1 entr(y/ies)." in capsys.readouterr().out
    assert not (elsewhere / ".runtime").exists()


def test_a_bare_shell_ignores_a_runtime_root_in_the_installed_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bare-shell run reaches the installed runtime, `<install>/.runtime`.

    `CIAO_RUNTIME_ROOT` is no longer read from the workspace `.env` (only the
    process environment moves the runtime root), so a leftover line naming
    another directory must not divert the receipt there: the server, which
    ignores the line too, would never see it.
    """
    vault = _per_root_install(
        tmp_path, monkeypatch, records=(RETIRED_RECORD, UNPROPOSED_RECORD)
    )
    approval = _write_approval(tmp_path, _approvals(RETIRED_RECORD))
    install = tmp_path / "install"
    runtime = install / ".runtime"
    custom = install / "custom-runtime"
    custom.mkdir()
    (custom / "workspaces.json").write_text(
        (runtime / "workspaces.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (install / ".env").write_text("CIAO_RUNTIME_ROOT=custom-runtime\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "ciao.macos_service.discover_runtime",
        lambda **_: SimpleNamespace(
            workspace=str(install), runtime_root=str(runtime)
        ),
    )

    assert cli.main(
        [
            "learnings-cleanup",
            "--apply",
            "--approval-file",
            str(approval),
            "--vault-root",
            str(vault),
        ]
    ) == 0

    receipt = next((runtime / "migration").glob("learnings-cleanup-*.json"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["workspace"] == WORKSPACE
    assert "Removed 1 entr(y/ies)." in capsys.readouterr().out
    assert not (custom / "migration").exists(), "a .env runtime root is not read"
    assert not (elsewhere / ".runtime").exists()


def test_an_unknown_active_workspace_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A set-but-unregistered `CIAO_ACTIVE_WORKSPACE` is refused, not repaired.

    The failure it stands in for is silent and destructive: the run reconciles the
    *primary* workspace's `Learnings.md`, removes entries from it, and writes a
    receipt naming it — because the registry it read was built somewhere else, or
    the workspace was renamed. `_resolve_workspace_and_vaults` answers this the
    same way, so the nightly planner and this command do not disagree about which
    workspace a run is about.
    """
    vault = _per_root_install(tmp_path, monkeypatch, records=(RETIRED_RECORD,))
    before = (vault / "Workspace" / "Learnings.md").read_bytes()
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "nope")

    assert cli.main(["learnings-cleanup"]) == 1

    err = capsys.readouterr().err
    assert "CIAO_ACTIVE_WORKSPACE `nope` is not a registered workspace" in err
    assert WORKSPACE in err
    assert (vault / "Workspace" / "Learnings.md").read_bytes() == before
