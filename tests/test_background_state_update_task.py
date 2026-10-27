"""The install-scoped ``remove-background-run-state`` update task (#2225).

The removed ``ciao run`` runner kept its registry and logs in
``<runtime>/background/``. Nothing reads that folder any more, so an "After this
update" task offers to delete it once. Applicability is the folder's existence and
completion is its absence, which is what makes the task idempotent: a missing
folder is "done", never an error. Every fixture is a temp directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import __version__, update_tasks
from ciao.config import CiaoConfig
from ciao.update_task_catalog import TaskCatalog, load_catalog, read_prompt
from ciao.update_tasks import APPLICABLE, NOT_APPLICABLE

TASK_ID = "remove-background-run-state"


@pytest.fixture(autouse=True)
def _clean_cache():
    update_tasks.clear_applicability_cache()
    yield
    update_tasks.clear_applicability_cache()


def _config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "state.json").write_text("{}\n", encoding="utf-8")
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        vault_root=tmp_path / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )


def _legacy_dir(tmp_path: Path) -> Path:
    directory = tmp_path / ".runtime" / "background"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text("{}\n", encoding="utf-8")
    (directory / "bg-1.log").write_text("old output\n", encoding="utf-8")
    return directory


def _task():
    catalog = load_catalog()
    assert not catalog.diagnostics
    return catalog.by_id[TASK_ID]


def test_the_task_is_shipped_install_scoped_with_a_prompt() -> None:
    task = _task()

    assert task.scope == "install"
    assert "background/" in read_prompt(task)
    assert "rm -r" in read_prompt(task)


def test_no_legacy_folder_means_not_applicable_and_complete(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = update_tasks.apply_detector(_task(), config=config)

    assert result.status == NOT_APPLICABLE
    check = update_tasks.COMPLETION_FUNCTIONS[_task().completion_check]
    assert check(config=config).applicable is True


def test_the_legacy_folder_makes_it_applicable_and_incomplete(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _legacy_dir(tmp_path)

    result = update_tasks.apply_detector(_task(), config=config)

    assert result.status == APPLICABLE
    check = update_tasks.COMPLETION_FUNCTIONS[_task().completion_check]
    assert check(config=config).applicable is False


def test_deleting_the_folder_settles_the_task_and_repeating_is_harmless(
    tmp_path: Path,
) -> None:
    import shutil

    config = _config(tmp_path)
    directory = _legacy_dir(tmp_path)
    task = _task()
    assert update_tasks.record_completion(task, config=config) is None

    shutil.rmtree(directory)

    done = update_tasks.record_completion(task, config=config)
    assert done is not None and done.lifecycle == "completed"
    again = update_tasks.record_completion(task, config=config)
    assert again is not None and again.lifecycle == "completed"
    assert update_tasks.apply_detector(task, config=config).status == NOT_APPLICABLE


def test_other_runtime_files_do_not_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / ".runtime" / "backgrounds.json").write_text("{}\n", encoding="utf-8")

    assert update_tasks.apply_detector(_task(), config=config).status == NOT_APPLICABLE


async def test_evaluate_offers_it_only_while_the_folder_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)

    async def statuses() -> dict[str, str]:
        update_tasks.clear_applicability_cache()
        rows = await update_tasks.evaluate(
            config, installed_version=__version__, catalog=TaskCatalog(tasks=(_task(),))
        )
        return {row.task.id: row.applicability.status for row in rows}

    assert (await statuses())[TASK_ID] == NOT_APPLICABLE
    _legacy_dir(tmp_path)
    assert (await statuses())[TASK_ID] == APPLICABLE
