"""The dormant "is there due care work" verdict and `ciao curation-due`.

The nightly Workspace care run spends a model turn to find out that a quiet
vault has nothing to do. `care_due_verdict` answers that question from files
alone, without a lease, a model or a write. These tests pin three things: the
rules, in order; that an EMPTY verdict agrees with what `curation-begin` would
report on the same vault; and that the command's output and exit codes are the
contract the schedule relies on. Nothing here wires the command into the
packaged schedule.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ciao import cli
from ciao import curation_run as cr
from ciao.care_due import (
    REASON_BUSY,
    REASON_CATEGORY_SCAN,
    REASON_EMPTY,
    REASON_PASS_SKIPPED,
    REASON_STATE_UNREADABLE,
    Busy,
    Due,
    Empty,
    Unavailable,
    care_due_verdict,
    verdict_payload,
)
from ciao.cli import CURATION_BUSY_EXIT


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks under tmp_path, and no ambient workspace reaching the CLI."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)


# ── Pure rules ────────────────────────────────────────────────────────────


def _item(pass_id: str = cr.PASS_HYGIENE, keys: tuple[str, ...] = ("k",)) -> cr.WorklistItem:
    return cr.WorklistItem(pass_id=pass_id, label="label", reason="reason", keys=keys)


def _worklist(
    *items: cr.WorklistItem,
    notes: tuple[str, ...] = (),
    coverage: cr.NoteCoverage | None = None,
) -> cr.Worklist:
    return cr.Worklist(
        items=tuple(items),
        weekly_due=False,
        last_full_pass="2026-09-18",
        generated_at="2026-09-19T00:00:00Z",
        notes=notes,
        note_coverage=coverage,
    )


def _plan(
    planned: tuple[cr.WorklistItem, ...] = (),
    deferred: tuple[cr.WorklistItem, ...] = (),
    blocked: tuple[cr.WorklistItem, ...] = (),
) -> cr.RunPlan:
    return cr.RunPlan(
        planned=planned, deferred=deferred, budget=cr.RunBudget(), blocked=blocked
    )


def _coverage(status: str = "complete", *, undated: int = 0) -> cr.NoteCoverage:
    return cr.NoteCoverage(
        status=status,
        scanned=0,
        exempt=0,
        undated=undated,
        flagged=0,
        never_queued=0,
        unreadable=0,
        settled=0,
        planned=0,
        eligible_deferred=0,
        unexamined=0,
        oldest_deferred_age_days=None,
        cap=20,
    )


def _candidate(type_id: str = "recipe-book") -> dict[str, Any]:
    return {"type_id_suggestion": type_id}


def test_a_state_read_error_is_unavailable_whatever_else_is_known() -> None:
    verdict = care_due_verdict(
        _worklist(_item()),
        plan=_plan(planned=(_item(),)),
        lease=None,
        category_candidates=[],
        state_error="curation state is corrupt",
    )
    assert verdict == Unavailable(REASON_STATE_UNREADABLE)


def test_a_live_lease_is_busy_even_when_the_worklist_is_empty() -> None:
    verdict = care_due_verdict(
        _worklist(),
        plan=_plan(),
        lease={"holder": "other", "expires_at": "2026-09-19T01:00:00Z"},
        category_candidates=[],
    )
    assert verdict == Busy(REASON_BUSY)


def test_a_busy_lease_beats_a_failed_category_scan() -> None:
    verdict = care_due_verdict(
        _worklist(),
        plan=_plan(),
        lease={"holder": "other"},
        category_candidates=None,
    )
    assert isinstance(verdict, Busy)


def test_a_failed_category_scan_is_unavailable_not_empty() -> None:
    verdict = care_due_verdict(
        _worklist(), plan=_plan(), lease=None, category_candidates=None
    )
    assert verdict == Unavailable(REASON_CATEGORY_SCAN)


def test_a_category_candidate_makes_an_empty_worklist_due() -> None:
    verdict = care_due_verdict(
        _worklist(), plan=_plan(), lease=None, category_candidates=[_candidate()]
    )
    assert verdict == Due(planned_count=1)


@pytest.mark.parametrize("note", cr.SKIPPED_PASS_NOTES)
def test_a_skipped_pass_note_is_unavailable(note: str) -> None:
    verdict = care_due_verdict(
        _worklist(notes=(note,), coverage=_coverage()),
        plan=_plan(),
        lease=None,
        category_candidates=[],
    )
    assert verdict == Unavailable(REASON_PASS_SKIPPED)


def test_a_blocked_learnings_cleanup_note_is_unavailable() -> None:
    note = f"{cr.NOTE_LEARNINGS_CLEANUP_DID_NOT_RUN}the workspace has no registry"
    verdict = care_due_verdict(
        _worklist(notes=(note,), coverage=_coverage()),
        plan=_plan(),
        lease=None,
        category_candidates=[],
    )
    assert verdict == Unavailable(REASON_PASS_SKIPPED)


def test_unavailable_note_coverage_is_unavailable_even_without_a_note() -> None:
    verdict = care_due_verdict(
        _worklist(coverage=_coverage("unavailable")),
        plan=_plan(),
        lease=None,
        category_candidates=[],
    )
    assert verdict == Unavailable(REASON_PASS_SKIPPED)


def test_informational_notes_and_partial_coverage_do_not_make_an_empty_night_unavailable() -> None:
    notes = (
        "no usable last_full_pass marker; the weekly pass counts as due",
        "more than 20 notes are due; this run plans the 20 oldest of 25 found",
        "2 of 4 flagged note(s) already have a check that settles them",
    )
    verdict = care_due_verdict(
        _worklist(notes=notes, coverage=_coverage("partial", undated=2)),
        plan=_plan(),
        lease=None,
        category_candidates=[],
    )
    assert verdict == Empty(REASON_EMPTY)


def test_a_blocked_oversize_log_is_due_because_an_operator_must_act() -> None:
    blocked = _item(cr.PASS_LOGS)
    verdict = care_due_verdict(
        _worklist(blocked),
        plan=_plan(blocked=(blocked,)),
        lease=None,
        category_candidates=[],
    )
    assert isinstance(verdict, Due)


def test_budget_deferred_work_is_due() -> None:
    deferred = _item(cr.PASS_PROPOSALS, keys=("a", "b"))
    verdict = care_due_verdict(
        _worklist(deferred),
        plan=_plan(deferred=(deferred,)),
        lease=None,
        category_candidates=[],
    )
    assert isinstance(verdict, Due)


def test_a_worklist_with_nothing_in_it_and_no_skipped_pass_is_empty() -> None:
    verdict = care_due_verdict(
        _worklist(coverage=_coverage()),
        plan=_plan(),
        lease=None,
        category_candidates=[],
    )
    assert verdict == Empty(REASON_EMPTY)


def test_due_counts_the_planned_keys_and_the_category_candidates() -> None:
    planned = _item(cr.PASS_PROPOSALS, keys=("a", "b"))
    verdict = care_due_verdict(
        _worklist(planned),
        plan=_plan(planned=(planned,)),
        lease=None,
        category_candidates=[_candidate("one"), _candidate("two")],
    )
    assert verdict == Due(planned_count=4)


def test_the_verdict_payload_is_the_json_the_command_prints() -> None:
    assert verdict_payload(Empty(REASON_EMPTY)) == {
        "verdict": "empty",
        "reason": REASON_EMPTY,
    }
    assert verdict_payload(Due(planned_count=3)) == {
        "verdict": "due",
        "reason": Due(planned_count=3).reason,
        "planned_count": 3,
    }


# ── Real vault fixtures ───────────────────────────────────────────────────


def _today() -> date:
    return datetime.now(UTC).date()


def _registered_install(base: Path, *, log_days_ago: int = 1) -> tuple[Path, Path]:
    """An install whose registry names workspace `work` over `memory-vault/client-a`."""
    root = base / "workspace"
    vault = root / "memory-vault" / "client-a"
    (vault / "Workspace").mkdir(parents=True)
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "workspaces.json").write_text(
        json.dumps({"work": {"name": "work", "vault_root": str(vault)}}),
        encoding="utf-8",
    )
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        "---\nlast_full_pass: "
        f"{(_today() - timedelta(days=log_days_ago)).isoformat()}\n---\n\n# Curation log\n",
        encoding="utf-8",
    )
    # An absent learnings file is reported as a blocked learnings pass, so the
    # quiet-vault fixtures carry an empty one. See the absent-file test below.
    (vault / cr.LEARNINGS_RELATIVE).write_text("# Learnings\n", encoding="utf-8")
    return root, vault


def _cluster(vault: Path, type_: str, count: int) -> None:
    notes = vault / "Journals"
    notes.mkdir(exist_ok=True)
    for index in range(count):
        (notes / f"Note{index}.md").write_text(
            f"---\ntype: {type_}\n---\n# Note{index}\n", encoding="utf-8", newline=""
        )


def _stale_person(vault: Path) -> None:
    people = vault / "People"
    people.mkdir(exist_ok=True)
    (people / "Ada.md").write_text(
        "---\ntype: person\nupdated: 2024-01-05\n---\n\n# Ada\n\n"
        "- Ada runs the release train [verified: 2024-01-05]\n",
        encoding="utf-8",
    )


def _oversize_log(vault: Path) -> None:
    log = vault / cr.CURATION_LOG_RELATIVE
    log.write_text(log.read_text(encoding="utf-8") + ("x" * (cr.LOG_ROTATION_BYTES + 1)), encoding="utf-8")


def _undated_note(vault: Path) -> None:
    (vault / "Journals").mkdir(exist_ok=True)
    (vault / "Journals" / "Loose.md").write_text("# Loose\n\nNo frontmatter.\n", encoding="utf-8")


FIXTURES = {
    "idle": (lambda vault: None, "empty"),
    "weekly_due": (lambda vault: None, "due"),
    "category_only": (lambda vault: _cluster(vault, "recipe-book", 3), "due"),
    "stale_fact": (_stale_person, "due"),
    "blocked_log": (_oversize_log, "due"),
    "undated_note_only": (_undated_note, "empty"),
}


def _build(base: Path, name: str) -> tuple[Path, Path]:
    build, _expected = FIXTURES[name]
    days_ago = 30 if name == "weekly_due" else 1
    root, vault = _registered_install(base, log_days_ago=days_ago)
    build(vault)
    return root, vault


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_due_verdict_agrees_with_curation_begin_on_the_same_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    """Separate copies, due first: the predicate must not disturb the vault it reads."""
    _build_expected = FIXTURES[name][1]
    due_root, due_vault = _build(tmp_path / "due", name)
    begin_root, begin_vault = _build(tmp_path / "begin", name)

    code, out, err = _run(
        capsys,
        ["curation-due", "--workspace", str(due_root), "--vault-root", str(due_vault)],
    )
    begin_code, begin_out, _ = _run(
        capsys,
        [
            "curation-begin",
            "--workspace", str(begin_root),
            "--vault-root", str(begin_vault),
            "--holder", "parity",
            "--json",
        ],
    )

    assert begin_code == 0
    begin_empty = bool(json.loads(begin_out)["empty"])
    if _build_expected == "empty":
        assert (code, out, err) == (0, "", "")
        assert begin_empty is True
    else:
        assert code == 0
        assert out.startswith("care due: ")
        assert begin_empty is False


def test_a_category_only_vault_is_due_through_the_candidate_scan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression for trap 1: the plan alone reports this vault as empty."""
    root, vault = _build(tmp_path, "category_only")
    planned_only = cli._curation_plan(_namespace(root, vault)).run_plan
    assert planned_only.planned == ()

    code, out, _ = _run(
        capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault)]
    )

    assert code == 0
    assert out == "care due: 1 items planned\n"


def test_an_empty_night_prints_nothing_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, vault = _build(tmp_path, "idle")

    assert _run(
        capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault)]
    ) == (0, "", "")


def test_an_unregistered_vault_is_unavailable_on_stderr_with_exit_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No registered name means a skipped pass, so the verdict cannot say empty."""
    root, _registered = _registered_install(tmp_path)
    plain = tmp_path / "plain"
    (plain / "Workspace").mkdir(parents=True)

    code, out, err = _run(
        capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(plain)]
    )

    assert (code, out) == (1, "")
    assert err == f"{REASON_PASS_SKIPPED}\n"


def test_a_vault_without_a_learnings_file_is_unavailable_not_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pins a known gap between the brief and the code, reported, not fixed here.

    ``learnings_cleanup.plan_cleanup`` reports an absent ``Workspace/Learnings.md``
    as a blocked pass ("does not exist"), which is a skipped-pass note. The verdict
    therefore cannot call such a vault empty, even though nothing is wrong. Making
    it empty needs a structured "no file, no work" result from the planner, which
    is a change to learnings cleanup and is left to the maintainer.
    """
    root, vault = _build(tmp_path, "idle")
    (vault / cr.LEARNINGS_RELATIVE).unlink()

    code, out, err = _run(
        capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault)]
    )

    assert (code, out) == (1, "")
    assert err == f"{REASON_PASS_SKIPPED}\n"


def test_a_held_live_lease_exits_seventy_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, vault = _build(tmp_path, "idle")
    cr.begin_run(vault, holder="other-run")

    code, out, err = _run(
        capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault)]
    )

    assert code == CURATION_BUSY_EXIT
    assert out == "care busy: a run holds the lease\n"
    assert err == ""


def test_the_json_form_carries_the_verdict_and_its_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, vault = _build(tmp_path, "category_only")

    code, out, _ = _run(
        capsys,
        ["curation-due", "--workspace", str(root), "--vault-root", str(vault), "--json"],
    )

    assert code == 0
    assert json.loads(out) == {
        "verdict": "due",
        "reason": Due(planned_count=1).reason,
        "planned_count": 1,
    }


@pytest.mark.parametrize("name", ["idle", "category_only", "stale_fact", "weekly_due"])
def test_the_command_leaves_the_vault_and_its_state_byte_for_byte_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    root, vault = _build(tmp_path, name)
    # Write a real state file, then release the lease, so the bytes under test exist.
    lease = cr.begin_run(vault, holder="setup")
    cr.end_run(vault, holder=lease.holder, status="ok", planned=0, completed=0, deferred=0)
    before = _tree(root)
    state_before = (vault / "Workspace" / "Curation-State.json").read_bytes()

    _run(capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault)])
    _run(capsys, ["curation-due", "--workspace", str(root), "--vault-root", str(vault), "--json"])

    assert _tree(root) == before
    assert (vault / "Workspace" / "Curation-State.json").read_bytes() == state_before


def _namespace(root: Path, vault: Path) -> Any:
    import argparse

    return argparse.Namespace(
        workspace=root,
        vault_root=vault,
        guide=None,
        max_items=None,
        max_seconds=None,
        json=True,
    )


# ── Dispatch seam: the contract the preflight relies on ──────────────────


def _dispatch_host(tmp_path: Path) -> Any:
    from tests.test_schedule_dispatch_boundary import _ScheduleHost

    return _ScheduleHost(
        tmp_path, [{"type": "result", "text": "done", "is_error": False}]
    )


def _command_entry() -> Any:
    from tests.test_schedule_dispatch_boundary import _entry

    entry = _entry()
    entry.command = "probe"
    return entry


@pytest.mark.asyncio
async def test_a_silent_command_takes_the_quiet_branch_and_opens_no_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_schedule_dispatch_boundary import _capture_run_records
    from ciao.schedule_command import CommandRun
    from ciao.web.schedule_dispatch import ScheduleDispatcher, ScheduleDispatchHost

    host = _dispatch_host(tmp_path)
    rows = _capture_run_records(monkeypatch)

    async def _silent(command: str, cwd: Path, env: dict[str, str]) -> CommandRun:
        return CommandRun(command=command, exit_code=0, stdout="", stderr="", timed_out=False)

    monkeypatch.setattr("ciao.web.schedule_dispatch.run_schedule_command", _silent)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(_command_entry(), "Run the routine.", "opus", "auto", "claude")

    assert host.prepared == []
    assert host.started == []
    assert rows[0].status == "ok"
    assert rows[0].extra["chat_id"] == ""


@pytest.mark.asyncio
async def test_a_command_line_opens_the_chat_with_the_line_fenced_ahead_of_the_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_schedule_dispatch_boundary import _capture_run_records
    from ciao.schedule_command import CommandRun
    from ciao.web.schedule_dispatch import ScheduleDispatcher, ScheduleDispatchHost

    host = _dispatch_host(tmp_path)
    _capture_run_records(monkeypatch)

    async def _one_line(command: str, cwd: Path, env: dict[str, str]) -> CommandRun:
        return CommandRun(
            command=command,
            exit_code=0,
            stdout="care due: 1 items planned\n",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ciao.web.schedule_dispatch.run_schedule_command", _one_line)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(_command_entry(), "Run the routine.", "opus", "auto", "claude")

    assert len(host.prepared) == 1
    prompt = host.prepared[0][1]
    assert "care due: 1 items planned" in prompt
    assert "command-output" in prompt
    assert prompt.endswith("Run the routine.")
