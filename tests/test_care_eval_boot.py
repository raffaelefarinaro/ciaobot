from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from ciao import care_eval_boot as boot
from ciao import care_eval_seed as seed
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
