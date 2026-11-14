from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.update_task_catalog import UpdateTask, load_catalog


def config_for(tmp_path: Path) -> CiaoConfig:
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime/state.json",
        media_root=tmp_path / ".runtime/media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    for name in ("personal", "work"):
        Path(config.workspace_vault_root(name)).mkdir(parents=True)
    return config


def task(name: str) -> UpdateTask:
    return load_catalog().by_id[name]


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
        for p in root.rglob("*")
    }


def test_unknown_task_has_no_planner_and_invalid_workspace_is_rejected_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview

    config = config_for(tmp_path)
    unknown = replace(task("learnings-cleanup"), id="future")

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("planner called")

    monkeypatch.setattr(preview.learnings_cleanup, "plan_cleanup", fail)
    before = snapshot(tmp_path)
    for name in ("", "missing"):
        with pytest.raises(ValueError):
            preview.prepare(unknown, config=config, workspace=name)
    result = preview.prepare(unknown, config=config, workspace="personal")
    assert result == {
        "task_id": "future",
        "revision": unknown.revision,
        "workspace": "personal",
        "state": "unknown",
        "summary": "This task has no automatic preview; no changes were made.",
        "plan": None,
    }
    assert snapshot(tmp_path) == before


def test_real_learnings_preview_is_read_only_and_preserves_review(tmp_path: Path) -> None:
    from ciao import update_task_preview as preview
    from ciao.learning_records import LearningRecord, allocate_learning_id, render_learning

    config = config_for(tmp_path)
    record = LearningRecord(
        learning_id=allocate_learning_id("personal", "- [example] Keep the source."),
        key="example",
        text="Keep the source.",
    )
    note = Path(config.workspace_vault_root("personal")) / "Workspace/Learnings.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: document\nupdated: 2026-01-01\ntags: [learnings]\n---\n# Learnings\n\n## Active\n\n"
        + render_learning(record)
        + "\n\n## Promoted / Resolved\n",
        encoding="utf-8",
    )
    before = snapshot(tmp_path)
    result = preview.prepare(task("learnings-cleanup"), config=config, workspace="personal")
    assert result["state"] == "review"
    assert {k: result["plan"]["counts"][k] for k in ("active", "remove", "keep", "conflict")} == {
        "active": 1,
        "remove": 0,
        "keep": 1,
        "conflict": 0,
    }
    assert snapshot(tmp_path) == before


def test_registry_and_serializer_failure_are_unknown_not_invalid_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview

    config = config_for(tmp_path)
    item = task("learnings-cleanup")
    expected = {
        "task_id": item.id,
        "revision": item.revision,
        "workspace": "personal",
        "state": "unknown",
        "summary": "Could not prepare this task; no changes were made.",
        "plan": None,
    }
    before = snapshot(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sensitive sentinel")

    with monkeypatch.context() as m:
        m.setattr(type(config), "workspace_names", fail)
        assert preview.prepare(item, config=config, workspace="personal") == expected
    monkeypatch.setattr(preview.learnings_cleanup.CleanupPlan, "as_dict", fail)
    assert preview.prepare(item, config=config, workspace="personal") == expected
    with pytest.raises(ValueError):
        preview.prepare(item, config=config, workspace="missing")
    assert snapshot(tmp_path) == before


PERSON = "---\ntype: person\nupdated: 2026-01-01\ntags: [colleague]\n---\n# Mo\nA contact.\n"


def rehome_fixture(config: CiaoConfig) -> Path:
    root = Path(config.vault_root)
    person = root / "personal/People/Mo.md"
    person.parent.mkdir(parents=True)
    person.write_text(PERSON, encoding="utf-8")
    example = root / "personal/Projects/Example.md"
    example.parent.mkdir(parents=True)
    example.write_text(
        "---\ntype: document\nupdated: 2026-01-01\ntags: []\n---\n# Example\n[Mo](../People/Mo.md)\n",
        encoding="utf-8",
    )
    return root


def test_real_rehome_preview_reports_unreadable_note_as_blocked(tmp_path: Path) -> None:
    from ciao import update_task_preview as preview, vault_rehome

    config = config_for(tmp_path)
    root = rehome_fixture(config)
    bad = root / "personal/People/Unreadable.md"
    bad.write_bytes(b"\xff\xfeinvalid")
    before = snapshot(tmp_path)
    assert vault_rehome.plan_rehome(root, workspaces=["personal", "work"])["read_errors"] == [
        "personal/People/Unreadable.md"
    ]
    result = preview.prepare(task("unrehomed-people"), config=config, workspace="personal")
    assert result["state"] == "blocked"
    assert result["plan"]["read_errors"] == ["personal/People/Unreadable.md"]
    assert "Blocked" in result["summary"]
    assert "0 conflicts" in result["summary"]
    assert "1 unreadable" in result["summary"]
    assert snapshot(tmp_path) == before


def test_real_rehome_preview_uses_registered_workspaces_without_writing(tmp_path: Path) -> None:
    from ciao import update_task_preview as preview

    config = config_for(tmp_path)
    root = rehome_fixture(config)
    before = snapshot(tmp_path)
    result = preview.prepare(task("unrehomed-people"), config=config, workspace="personal")
    assert result["state"] == "review"
    assert result["plan"]["moves"] == [{"from": "personal/People/Mo.md", "to": "work/People/Mo.md"}]
    assert result["plan"]["needs_judgement"] == result["plan"]["conflicts"] == []
    assert not (root / "work/People/Mo.md").exists()
    assert snapshot(tmp_path) == before


def test_rehome_serialization_excludes_full_document_rewrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview

    config = config_for(tmp_path)
    before = snapshot(tmp_path)

    def planner(root: Path, *, workspaces: list[str]) -> dict[str, Any]:
        assert root == Path(config.vault_root)
        assert workspaces == ["personal", "work"]
        return dict(
            workspaces=workspaces,
            mechanical=[],
            needs_judgement=[],
            moves=[],
            conflicts=[],
            notes_scanned=1,
            read_errors=[],
            files={"a": "secret sentinel"},
            vault_root="secret sentinel",
        )

    monkeypatch.setattr(preview.vault_rehome, "plan_rehome", planner)
    result = preview.prepare(task("unrehomed-people"), config=config, workspace="personal")
    assert "files" not in result["plan"] and "secret sentinel" not in str(result)
    assert snapshot(tmp_path) == before


def relocation_config(tmp_path: Path, nested: bool = False) -> tuple[CiaoConfig, Path]:
    workspaces = {"personal": WorkspaceConfig(name="personal", vault_root="elsewhere/personal")}
    if nested:
        workspaces["work"] = WorkspaceConfig(name="work", vault_root="elsewhere/personal/nested")
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime/state.json",
        media_root=tmp_path / ".runtime/media",
        vault_root=tmp_path / "memory-vault",
        workspaces=workspaces,
    )
    note = Path(config.workspace_vault_root("personal")) / "People/Mo.md"
    note.parent.mkdir(parents=True)
    note.write_text(PERSON, encoding="utf-8")
    if nested:
        Path(config.workspace_vault_root("work")).mkdir(parents=True)
    return config, note


def test_real_relocation_preview_reports_move_without_repointing(tmp_path: Path) -> None:
    from ciao import update_task_preview as preview

    config, note = relocation_config(tmp_path)
    before = snapshot(tmp_path)
    result = preview.prepare(task("vault-relocate"), config=config, workspace="personal")
    assert result["state"] == "review"
    assert result["plan"]["whole_directory"]
    assert Path(result["plan"]["source"]) == note.parent.parent.resolve()
    assert (
        Path(result["plan"]["destination"])
        == Path(config.canonical_workspace_vault_root("personal")).resolve()
    )
    assert "approval" in result["summary"] and "restart" in result["summary"]
    assert not Path(result["plan"]["destination"]).exists()
    assert snapshot(tmp_path) == before


def test_relocation_uses_validated_registry_snapshot_for_overlap_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview

    config, _ = relocation_config(tmp_path, nested=True)
    before = snapshot(tmp_path)
    calls = []

    def names(self: CiaoConfig) -> list[str]:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("registry unavailable")
        return ["personal", "work"]

    monkeypatch.setattr(type(config), "workspace_names", names)
    result = preview.prepare(task("vault-relocate"), config=config, workspace="personal")
    assert len(calls) == 1
    assert result["state"] == "blocked"
    assert not result["plan"]["whole_directory"]
    assert any("another registered workspace" in reason for reason in result["plan"]["refusals"])
    assert "Blocked" in result["summary"]
    assert result["plan"]["refusals"][0] in result["summary"]
    assert "proposes moving" not in result["summary"]
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("kind", "refusal"),
    [
        ("learnings-cleanup", "blocked"),
        ("vault-relocate", "refused"),
        ("vault-relocate", "unclassified"),
        ("unrehomed-people", "skipped"),
        ("unrehomed-people", "conflicts"),
    ],
)
def test_planner_refusals_are_blocked_not_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, refusal: str
) -> None:
    from ciao import update_task_preview as preview
    from types import SimpleNamespace

    config = config_for(tmp_path)
    before = snapshot(tmp_path)
    if kind == "learnings-cleanup":
        plan = {"blocked": "refused", "counts": dict(active=0, remove=0, keep=0, conflict=0)}
        monkeypatch.setattr(
            preview.learnings_cleanup,
            "plan_cleanup",
            lambda *a, **k: SimpleNamespace(as_dict=lambda: plan),
        )
    elif kind == "vault-relocate":
        plan = {
            "refused": True,
            "whole_directory": False,
            "entries": [],
            "refusals": ["relocation refusal"] if refusal == "refused" else [],
        }
        if refusal == "unclassified":
            plan["entries"] = [
                {"name": "Mystery", "action": "unclassified", "reason": "cannot classify"}
            ]
        monkeypatch.setattr(
            preview.vault_relocate, "plan", lambda *a, **k: SimpleNamespace(as_dict=lambda: plan)
        )
    else:
        plan = dict(
            workspaces=[],
            mechanical=[],
            needs_judgement=[],
            moves=[],
            conflicts=[],
            notes_scanned=0,
            read_errors=[],
        )
        plan[refusal] = ["conflict"] if refusal == "conflicts" else "skipped root"
        monkeypatch.setattr(preview.vault_rehome, "plan_rehome", lambda *a, **k: plan)
    result = preview.prepare(task(kind), config=config, workspace="personal")
    assert result["state"] == "blocked"
    assert "Blocked" in result["summary"]
    if kind == "learnings-cleanup":
        assert plan["blocked"] in result["summary"]
    elif kind == "vault-relocate":
        assert ("relocation refusal" if refusal == "refused" else "cannot classify") in result[
            "summary"
        ]
        assert "proposes moving" not in result["summary"]
    else:
        assert ("1 conflicts" if refusal == "conflicts" else "0 conflicts") in result["summary"]
        assert "0 unreadable files" in result["summary"]
    assert snapshot(tmp_path) == before


def test_planner_failure_is_unknown_without_exception_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview

    config = config_for(tmp_path)
    before = snapshot(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sensitive sentinel")

    monkeypatch.setattr(preview.vault_rehome, "plan_rehome", fail)
    item = task("unrehomed-people")
    result = preview.prepare(item, config=config, workspace="personal")
    assert result == {
        "task_id": item.id,
        "revision": item.revision,
        "workspace": "personal",
        "state": "unknown",
        "summary": "Could not prepare this task; no changes were made.",
        "plan": None,
    }
    assert snapshot(tmp_path) == before


def test_zero_change_preview_does_not_settle_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_task_preview as preview, update_tasks
    from ciao.web import update_task_launch

    config = config_for(tmp_path)
    before = snapshot(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not settle or launch")

    monkeypatch.setattr(update_tasks, "record_completion", fail)
    monkeypatch.setattr(update_task_launch, "launch_task", fail)
    result = preview.prepare(task("unrehomed-people"), config=config, workspace="personal")
    assert result["state"] == "review"
    assert result["plan"]["moves"] == []
    assert snapshot(tmp_path) == before
