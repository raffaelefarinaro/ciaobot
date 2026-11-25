from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import os
import signal
import subprocess
import sys

import pytest

from ciao import care_eval_boot as boot
from ciao import care_eval_seed as seed
from ciao import curation_run as cr
from ciao import task_eval as te
from ciao.schedules import ScheduleStore, system_schedule_id
from ciao.task_eval import TASKS_ROOT, WORKSPACE, EvalTask, IsolatedEngine
from ciao.workspace_guide import GUIDE_NAME

DISPATCH_DATE = date(2026, 11, 24)
FIXTURE_ROOT = TASKS_ROOT / te.FIXTURE_DIRNAME


def fixture_relatives() -> set[str]:
    return {p.relative_to(FIXTURE_ROOT).as_posix() for p in FIXTURE_ROOT.rglob("*") if p.is_file()}


def vault_relatives(vault: Path) -> set[str]:
    return {p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_file()}


def seed_overlay(base: Path, scenario: str = "nightly") -> seed.SeedManifest:
    # The overlay root is laid out like the agent root: the guide at its top, the vault beneath.
    root = base / "overlay"
    return boot.seed_overlay(scenario, root, dispatch_date=DISPATCH_DATE)


def test_care_task_is_a_schedule_boot_with_no_prompt_and_the_seed_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay"
    task = boot.care_task("nightly", overlay)
    assert isinstance(task, EvalTask)
    assert task.prompt == ""
    assert task.entry == {"kind": "schedule"}
    assert task.overlay == overlay


def test_prepare_workspace_with_a_caller_vault_copies_only_the_seed(tmp_path: Path) -> None:
    manifest = seed_overlay(tmp_path)
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    task = boot.care_task("nightly", tmp_path / "overlay")

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", task, vault_fixture=empty)

    vault = ws / WORKSPACE / seed.VAULT_DIRNAME
    expected_vault = {f.path for f in manifest.files if f.path != GUIDE_NAME}
    assert vault_relatives(vault) == expected_vault
    assert not (fixture_relatives() & vault_relatives(vault)) - expected_vault


def test_the_fixture_vault_is_never_copied_under_a_caller_vault(tmp_path: Path) -> None:
    seed_overlay(tmp_path)
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    task = boot.care_task("nightly", tmp_path / "overlay")

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", task, vault_fixture=empty)

    vault = ws / WORKSPACE / seed.VAULT_DIRNAME
    for relative in fixture_relatives():
        assert not (vault / relative).exists(), relative


def test_the_guide_under_the_agent_root_is_the_seed_guide(tmp_path: Path) -> None:
    seed_overlay(tmp_path)
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    task = boot.care_task("nightly", tmp_path / "overlay")

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", task, vault_fixture=empty)

    assert (ws / WORKSPACE / GUIDE_NAME).read_bytes() == seed.GUIDE_TEXT


def test_the_system_curation_schedule_is_materialized_for_personal(tmp_path: Path) -> None:
    seed_overlay(tmp_path)
    empty = tmp_path / "empty-vault"
    empty.mkdir()
    task = boot.care_task("nightly", tmp_path / "overlay")

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", task, vault_fixture=empty)

    # Read the store the way the engine builds it (main.py), not the raw files.
    store = ScheduleStore(ws / ".runtime", include_system=True, workspace_names=lambda: [WORKSPACE])
    ids = {entry.schedule_id for entry in store.list_entries()}
    assert system_schedule_id("system-memory-curation", WORKSPACE) in ids


def test_default_prepare_still_copies_the_fixture_vault(tmp_path: Path) -> None:
    task = boot.care_task("nightly", tmp_path / "unused")
    task = EvalTask(**{**task.__dict__, "overlay": None})

    ws = te.prepare_workspace(tmp_path / "T", "claude", "haiku", task)

    assert fixture_relatives() <= vault_relatives(ws / WORKSPACE / "memory-vault")


def test_boot_care_passes_the_empty_vault_and_boots_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_boot(cls: type[IsolatedEngine], task: EvalTask, **kwargs: Any) -> str:
        captured["task"] = task
        captured["kwargs"] = kwargs
        return "engine-stub"

    monkeypatch.setattr(IsolatedEngine, "boot", classmethod(fake_boot))
    engine = boot.boot_care(
        "nightly",
        provider="claude",
        model="haiku",
        dispatch_date=DISPATCH_DATE,
        scratch=tmp_path / "scratch",
    )

    assert engine == "engine-stub"
    assert captured["kwargs"]["vault_fixture"] == tmp_path / "scratch" / boot.EMPTY_VAULT_DIRNAME
    assert captured["kwargs"]["provider"] == "claude"
    assert captured["task"].overlay == tmp_path / "scratch" / boot.OVERLAY_DIRNAME
    assert (tmp_path / "scratch" / boot.OVERLAY_DIRNAME / GUIDE_NAME).is_file()


def test_boot_care_refuses_a_scratch_that_already_holds_a_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IsolatedEngine, "boot", classmethod(lambda cls, task, **kw: None))
    boot.boot_care("nightly", provider="claude", model="haiku", dispatch_date=DISPATCH_DATE,
                   scratch=tmp_path / "scratch")
    with pytest.raises(FileExistsError):
        boot.boot_care("nightly", provider="claude", model="haiku", dispatch_date=DISPATCH_DATE,
                       scratch=tmp_path / "scratch")


# --- tight budget (#3244) ---------------------------------------------------------


def _worklist(*keys: str) -> cr.Worklist:
    item = cr.WorklistItem(pass_id="hygiene", label="Hygiene", reason="test", keys=tuple(keys))
    return cr.Worklist(items=(item,), weekly_due=False, last_full_pass="", generated_at="2026-11-24T00:00:00Z")


def _fake_boot(captured: dict[str, Any]) -> Any:
    def fake_boot(cls: type[IsolatedEngine], task: EvalTask, **kwargs: Any) -> str:
        # Read the overlay while it exists: the real boot copies it into the workspace.
        captured["budget"] = (task.overlay / seed.VAULT_DIRNAME / cr.BUDGET_RELATIVE) if task.overlay else None
        captured["budget_bytes"] = (
            captured["budget"].read_bytes() if captured["budget"] and captured["budget"].is_file() else None
        )
        return "engine-stub"

    return classmethod(fake_boot)


def _boot(tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(IsolatedEngine, "boot", _fake_boot(captured))
    boot.boot_care(
        scenario, provider="claude", model="haiku", dispatch_date=DISPATCH_DATE,
        scratch=tmp_path / "scratch", **kwargs,
    )
    return captured


def test_continuation_boot_writes_a_one_item_budget_without_caller_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _boot(tmp_path, "budget_continuation", monkeypatch)
    vault = tmp_path / "scratch" / boot.OVERLAY_DIRNAME / seed.VAULT_DIRNAME
    assert cr.load_budget(vault).max_items == boot.TIGHT_MAX_ITEMS == 1
    assert captured["budget_bytes"] is not None


@pytest.mark.parametrize("scenario", ["nightly", "weekly_due"])
def test_other_scenarios_keep_the_default_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    captured = _boot(tmp_path, scenario, monkeypatch)
    assert captured["budget_bytes"] is None
    vault = tmp_path / "scratch" / boot.OVERLAY_DIRNAME / seed.VAULT_DIRNAME
    assert cr.load_budget(vault) == cr.RunBudget()


def test_an_explicit_max_items_overrides_the_scenario_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _boot(tmp_path, "nightly", monkeypatch, max_items=3)
    vault = tmp_path / "scratch" / boot.OVERLAY_DIRNAME / seed.VAULT_DIRNAME
    assert cr.load_budget(vault).max_items == 3


def test_the_budget_file_is_not_part_of_the_seed_manifest(tmp_path: Path) -> None:
    manifest = boot.seed_overlay("budget_continuation", tmp_path / "a", dispatch_date=DISPATCH_DATE)
    assert cr.BUDGET_RELATIVE not in {f.path for f in manifest.files}


@pytest.mark.parametrize("body", ["{", "[]", '{"max_items": 0}', '{"max_items": true}', '{"max_items": "2"}'])
def test_an_unreadable_budget_file_is_refused_not_defaulted(tmp_path: Path, body: str) -> None:
    path = tmp_path / cr.BUDGET_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    with pytest.raises(cr.CurationStateError):
        cr.load_budget(tmp_path)


def test_the_curation_cli_budget_reads_the_vault_file_and_an_explicit_flag_wins(tmp_path: Path) -> None:
    import argparse

    from ciao import cli

    vault = tmp_path / "vault"
    boot.write_tight_budget(vault, 1)
    args = argparse.Namespace(vault_root=vault, workspace=None, guide=None, max_items=None, max_seconds=None)
    assert cli._curation_context(args)[3].max_items == 1
    args.max_items = 5
    assert cli._curation_context(args)[3].max_items == 5


def test_attempt_one_settles_incomplete_and_attempt_two_resumes(tmp_path: Path) -> None:
    root = tmp_path / "overlay"
    manifest = boot.seed_overlay("budget_continuation", root, dispatch_date=DISPATCH_DATE)
    vault = root / seed.VAULT_DIRNAME
    boot.write_tight_budget(vault, boot.TIGHT_MAX_ITEMS)
    keys = ("k-first", "k-second")

    def attempt(done: frozenset[str]) -> tuple[cr.RunPlan, str]:
        worklist = _worklist(*[k for k in keys if k not in done])
        plan = cr.plan_run(worklist, cr.load_budget(vault))
        lease = cr.begin_run(vault, holder=f"h-{len(done)}")
        planned = [k for item in plan.planned for k in item.keys]
        cr.record_done(vault, planned, holder=lease.holder)
        left = sum(item.count for item in plan.deferred)
        cr.end_run(
            vault, holder=lease.holder, planned=len(planned), completed=len(planned), deferred=left,
            live_keys=frozenset(keys),
            failed=0, counts_complete=left == 0,
        )
        return plan, lease.holder

    plan1, _ = attempt(frozenset())
    first = seed.observe_cursor(vault)
    last1 = seed.observe_last_run(vault)
    assert [k for i in plan1.planned for k in i.keys] == ["k-first"]
    assert last1 is not None and last1.deferred == 1
    assert first.done_keys == {"k-first"}

    plan2, _ = attempt(frozenset(first.done_keys))
    last2 = seed.observe_last_run(vault)
    assert [k for i in plan2.planned for k in i.keys] == ["k-second"]
    assert last2 is not None and last2.deferred == 0
    assert seed.observe_cursor(vault).done_keys == {"k-first", "k-second"}
    assert manifest.scenario == "budget_continuation"


def test_cleanup_stops_only_the_owned_engine(tmp_path: Path) -> None:
    def sleeper() -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)

    owned, bystander = sleeper(), sleeper()
    engine = IsolatedEngine(tmp_path / "owned", keep_workdir=False)
    engine._proc = owned
    try:
        engine.close()
        owned.wait(timeout=10)
        assert owned.returncode is not None
        assert bystander.poll() is None
        os.kill(bystander.pid, 0)
    finally:
        os.killpg(bystander.pid, signal.SIGKILL)
        bystander.wait(timeout=10)
