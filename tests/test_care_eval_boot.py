from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ciao import care_eval_boot as boot
from ciao import care_eval_seed as seed
from ciao import curation_run as cr
from ciao import task_eval as te
from ciao.care_eval_scenario import CONTINUATION_BUDGET
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
# The continuation budget is a flag the harness adds through a ciao shim on the engine's
# PATH. No vault file or product setting carries it, so a note cannot change it.


def _worklist(*keys: str) -> cr.Worklist:
    item = cr.WorklistItem(pass_id="hygiene", label="Hygiene", reason="test", keys=tuple(keys))
    return cr.Worklist(items=(item,), weekly_due=False, last_full_pass="", generated_at="2026-11-24T00:00:00Z")


def _boot(tmp_path: Path, scenario: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_boot(cls: type[IsolatedEngine], task: EvalTask, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        # Read the overlay while it exists: the real boot copies it into the workspace.
        assert task.overlay is not None
        captured["overlay_files"] = vault_relatives(task.overlay / seed.VAULT_DIRNAME)
        return "engine-stub"

    monkeypatch.setattr(IsolatedEngine, "boot", classmethod(fake_boot))
    boot.boot_care(
        scenario, provider="claude", model="haiku", dispatch_date=DISPATCH_DATE,
        scratch=tmp_path / "scratch",
    )
    return captured


def test_continuation_boot_asks_for_a_one_item_curation_begin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _boot(tmp_path, "budget_continuation", monkeypatch)
    assert captured["kwargs"]["ciao_flags"] == {"curation-begin": ("--max-items", "1")}
    assert CONTINUATION_BUDGET.max_items == 1


@pytest.mark.parametrize("scenario", ["nightly", "weekly_due"])
def test_other_scenarios_boot_with_no_shim_and_the_default_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    captured = _boot(tmp_path, scenario, monkeypatch)
    assert captured["kwargs"]["ciao_flags"] is None


def test_the_budget_is_never_written_into_the_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _boot(tmp_path, "budget_continuation", monkeypatch)
    assert not any("budget" in name.lower() for name in captured["overlay_files"])


def test_a_stray_vault_budget_file_does_not_throttle_or_break_curation(tmp_path: Path) -> None:
    import argparse

    from ciao import cli

    vault = tmp_path / "vault"
    for body in ('{"max_items": 1}', "{", "[]", '{"max_items": 0}'):
        path = vault / "Workspace" / "Curation-Budget.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        args = argparse.Namespace(vault_root=vault, workspace=None, guide=None, max_items=None, max_seconds=None)
        assert cli._curation_context(args)[3] == cr.RunBudget()


def _fake_real_ciao(tmp_path: Path) -> Path:
    real = tmp_path / "real-ciao"
    real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    real.chmod(0o755)
    return real


def test_the_shim_adds_flags_to_the_named_subcommand_only(tmp_path: Path) -> None:
    shim_dir = te.write_ciao_shim(tmp_path, _fake_real_ciao(tmp_path), boot.continuation_flags())
    shim = shim_dir / "ciao"

    def run(*args: str) -> list[str]:
        return subprocess.run([str(shim), *args], capture_output=True, text=True, check=True).stdout.split()

    assert run("curation-begin", "--json") == ["curation-begin", "--json", "--max-items", "1"]
    assert run("curation-begin", "--max-items", "9") == ["curation-begin", "--max-items", "9", "--max-items", "1"]
    assert run("curation-progress", "--key", "k") == ["curation-progress", "--key", "k"]
    assert run() == []


def test_a_shim_needs_a_subcommand(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        te.write_ciao_shim(tmp_path, _fake_real_ciao(tmp_path), {})
    assert not (tmp_path / te.SHIM_DIRNAME).exists()


def test_the_engine_env_puts_the_shim_first_and_keeps_the_rest(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    plain = te.engine_env({"PATH": "/usr/bin"}, tmp_path, ws)
    shimmed = te.engine_env({"PATH": "/usr/bin"}, tmp_path, ws, path_prefix=tmp_path / "shim")
    assert plain["PATH"] == "/usr/bin"
    assert shimmed["PATH"] == f"{tmp_path / 'shim'}{os.pathsep}/usr/bin"


def _context_args(vault: Path, **flags: Any) -> Any:
    import argparse

    base: dict[str, Any] = {
        "vault_root": vault, "workspace": None, "guide": None, "max_items": None, "max_seconds": None,
        "holder": "h1", "json": True, "key": ["k1"],
    }
    return argparse.Namespace(**{**base, **flags})


def test_curation_flags_win_over_the_default_budget_at_the_cli(tmp_path: Path) -> None:
    from ciao import cli

    assert cli._curation_context(_context_args(tmp_path, max_items=1))[3] == cr.RunBudget(max_items=1)


def test_curation_begin_takes_its_lease_for_the_effective_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli

    seen: list[float] = []

    def busy(vault: Path, **kwargs: Any) -> Any:
        seen.append(kwargs["ttl_s"])
        raise cr.CurationBusy("held")

    monkeypatch.setattr(cr, "begin_run", busy)
    for name in ("CIAO_AGENT_TOKEN", "CIAO_AGENT_URL"):
        monkeypatch.delenv(name, raising=False)
    cli._curation_begin_command(_context_args(tmp_path, max_seconds=90.0))
    cli._curation_begin_command(_context_args(tmp_path))
    assert seen == [90.0, cr.DEFAULT_MAX_SECONDS]


def test_managed_care_begin_sends_the_effective_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import agent_cli, cli

    sent: list[tuple[str, dict[str, Any]]] = []

    def post(action: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        sent.append((action, payload))
        return {"ok": False, "error": {"code": "curation_busy", "message": "held"}}

    monkeypatch.setattr(agent_cli, "post", post)
    monkeypatch.setenv("CIAO_AGENT_TOKEN", "t")
    monkeypatch.setenv("CIAO_AGENT_URL", "http://127.0.0.1:1")
    assert cli._curation_begin_command(_context_args(tmp_path, max_seconds=45.0)) == cli.CURATION_BUSY_EXIT
    assert sent == [("care_begin", {"ttl_s": 45.0, "vault": str(tmp_path)})]


def test_curation_progress_renews_the_lease_for_the_effective_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli

    seen: list[float] = []

    def record(vault: Path, keys: list[str], **kwargs: Any) -> list[str]:
        seen.append(kwargs["ttl_s"])
        raise cr.CurationBusy("lost")

    monkeypatch.setattr(cr, "record_done", record)
    cli._curation_progress_command(_context_args(tmp_path, max_seconds=120.0))
    cli._curation_progress_command(_context_args(tmp_path))
    assert seen == [120.0, cr.DEFAULT_MAX_SECONDS]


def test_attempt_one_settles_incomplete_and_attempt_two_resumes(tmp_path: Path) -> None:
    root = tmp_path / "overlay"
    manifest = boot.seed_overlay("budget_continuation", root, dispatch_date=DISPATCH_DATE)
    vault = root / seed.VAULT_DIRNAME
    keys = ("k-first", "k-second")

    def attempt(done: frozenset[str]) -> tuple[cr.RunPlan, str]:
        worklist = _worklist(*[k for k in keys if k not in done])
        plan = cr.plan_run(worklist, CONTINUATION_BUDGET)
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
