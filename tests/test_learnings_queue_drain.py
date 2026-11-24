from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import learnings_queue_drain as drain
from ciao.memory_proposals import append_learning, was_promoted


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return vault, runtime


def test_apply_preserves_unmatched_crlf_rows_and_revert_restores_bytes(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    eol = bytes((13, 10)).decode()
    original = eol.join([
        "# Queue",
        "- [memory] keep this",
        "- [learnings] A reusable lesson _(from: chat-1)_ _(request: req-2)_ _(decide: nightly; carry the tail)_",
        "- [learnings] second lesson _(request: req-3)_",
        "- [project docs/x.md] learnings mentioned as prose",
        "",
    ])
    queue.write_bytes(original.encode())
    preview = drain.drain(vault, workspace="main", runtime_root=runtime)
    assert len(preview["rows"]) == 2
    assert queue.read_bytes().decode() == original

    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert result["failed"] == []
    assert was_promoted(vault, "A reusable lesson")
    assert len(result["receipts"]) == 1
    assert "- [memory] keep this\r\n" in queue.read_bytes().decode()
    assert "- [project docs/x.md] learnings mentioned as prose\r\n" in queue.read_bytes().decode()
    learning = (vault / "Workspace" / "Learnings.md").read_text()
    assert '"request":"req-2"' in learning and '"request":"req-3"' in learning
    assert "chat-1" in learning
    history = (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text()
    events = [json.loads(line) for line in history.splitlines()]
    assert [event["via"] for event in events] == ["migration", "migration"]
    assert events[0]["decider"] == "nightly"
    assert events[0]["decide_reason"] == "carry the tail"
    assert drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)["receipts"] == []

    receipt = Path(result["receipts"][0])
    reverted = drain.revert(vault, receipt, workspace="main", runtime_root=runtime, apply=True)
    assert reverted["reverted"]
    assert not was_promoted(vault, "A reusable lesson")
    assert queue.read_bytes().decode() == original
    assert not (vault / "Workspace" / "Learnings.md").exists()
    reverted_history = [json.loads(line) for line in (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text().splitlines()]
    assert [event["outcome"] for event in reverted_history] == ["accepted", "accepted", "reverted", "reverted"]


def test_fresh_only_does_not_reapply_reverted_rows_after_an_unrelated_queue_edit(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    original = "# Queue\n- [learnings] keep this undone\n- [memory] keep this too\n"
    queue.write_text(original, encoding="utf-8")

    applied = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )
    assert applied["applied"]
    reverted = drain.revert(
        vault, Path(applied["receipts"][0]), workspace="main", runtime_root=runtime, apply=True
    )
    assert reverted["reverted"]

    queue.write_text(original + "- [learnings] new independent lesson _(from: chat-2)_\n")
    next_run = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert [row["text"] for row in next_run["rows"]] == ["new independent lesson"]
    assert queue.read_text(encoding="utf-8") == original
    learning_text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    assert "keep this undone" not in learning_text
    assert "new independent lesson" in learning_text

    explicit_retry = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert [row["text"] for row in explicit_retry["rows"]] == ["keep this undone"]


def test_registered_startup_pass_covers_every_workspace_without_care_and_is_restart_safe(
    tmp_path: Path,
) -> None:
    personal, runtime = _vault(tmp_path / "personal")
    work, _ = _vault(tmp_path / "work")
    (personal / "Workspace" / "Memory-Proposals.md").write_text(
        "- [learnings] move this _(from: personal-chat)_\n- [memory] preserve this\n"
    )
    (work / "Workspace" / "Memory-Proposals.md").write_text(
        "- [learnings] also move this _(from: work-chat)_\n"
    )
    config = SimpleNamespace(
        state_path=runtime / "state.json",
        workspace_names=lambda: ("personal", "work"),
        workspace_vault_root=lambda workspace: personal if workspace == "personal" else work,
    )

    first = drain.drain_registered_workspaces(config)
    second = drain.drain_registered_workspaces(config)

    assert first["personal"]["applied"] and first["work"]["applied"]
    assert [row["text"] for row in first["personal"]["rows"]] == ["move this"]
    assert [row["text"] for row in first["work"]["rows"]] == ["also move this"]
    assert second["personal"]["not_applicable"] and second["work"]["not_applicable"]
    assert "preserve this" in (personal / "Workspace" / "Memory-Proposals.md").read_text()


def test_registered_startup_lock_failure_does_not_starve_later_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    personal, runtime = _vault(tmp_path / "personal")
    work, _ = _vault(tmp_path / "work")
    (personal / "Workspace" / "Memory-Proposals.md").write_text(
        "- [learnings] personal lesson _(from: personal-chat)_\n"
    )
    (work / "Workspace" / "Memory-Proposals.md").write_text(
        "- [learnings] work lesson _(from: work-chat)_\n"
    )
    config = SimpleNamespace(
        state_path=runtime / "state.json",
        workspace_names=lambda: ("personal", "work"),
        workspace_vault_root=lambda workspace: personal if workspace == "personal" else work,
    )
    original = drain.drain

    def one_locked_workspace(vault, *, workspace, **kwargs):
        if workspace == "personal":
            raise RuntimeError("queue lock is held")
        return original(vault, workspace=workspace, **kwargs)

    monkeypatch.setattr(drain, "drain", one_locked_workspace)
    results = drain.drain_registered_workspaces(config)

    assert results["personal"]["failed"] == ["queue lock is held"]
    assert results["work"]["applied"]
    assert "work lesson" in (work / "Workspace" / "Learnings.md").read_text()


def test_foreign_workspace_reverted_receipt_does_not_suppress_same_legacy_row(tmp_path: Path) -> None:
    personal, runtime = _vault(tmp_path / "personal")
    foreign, _foreign_runtime = _vault(tmp_path / "foreign")
    raw = "- [learnings] same words _(from: same-chat)_\n"
    (foreign / "Workspace" / "Memory-Proposals.md").write_text(raw)
    applied = drain.drain(foreign, workspace="work", runtime_root=runtime, apply=True)
    assert applied["applied"]
    drain.revert(
        foreign,
        Path(applied["receipts"][0]),
        workspace="work",
        runtime_root=runtime,
        apply=True,
    )

    personal_queue = personal / "Workspace" / "Memory-Proposals.md"
    personal_queue.write_text(raw)
    result = drain.drain(
        personal, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert result["applied"]
    assert "same words" in (personal / "Workspace" / "Learnings.md").read_text()


def test_legacy_row_dismissal_requires_exact_revision_and_preserves_neighbor_bytes(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    original = (
        "# Queue\r\n"
        "- [memory] leave this row exactly as it is\r\n"
        "- [learnings] remove only this obsolete row _(from: old-chat)_\r\n"
        "- [review] leave this other row too\r\n"
    )
    queue.write_bytes(original.encode("utf-8"))
    listed = drain.pending_rows(vault, workspace="main", runtime_root=runtime)
    target = listed["rows"][0]

    queue.write_bytes((original + "- [learnings] concurrent addition\r\n").encode())
    stale = drain.dismiss_pending_row(
        vault,
        workspace="main",
        runtime_root=runtime,
        row_id=target["id"],
        expected_revision=target["revision"],
    )
    assert stale["conflict"]
    assert "remove only this obsolete row" in queue.read_text(encoding="utf-8")

    current = drain.pending_rows(vault, workspace="main", runtime_root=runtime)["rows"]
    selected = next(row for row in current if row["text"] == "remove only this obsolete row")
    dismissed = drain.dismiss_pending_row(
        vault,
        workspace="main",
        runtime_root=runtime,
        row_id=selected["id"],
        expected_revision=selected["revision"],
    )
    assert dismissed["dismissed"]
    assert queue.read_bytes().decode("utf-8") == (
        original.replace(
            "- [learnings] remove only this obsolete row _(from: old-chat)_\r\n", ""
        )
        + "- [learnings] concurrent addition\r\n"
    )
    events = [
        json.loads(line)
        for line in (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text().splitlines()
    ]
    assert events[-1]["outcome"] == "dismissed"


def test_legacy_row_removal_resumes_after_interrupted_queue_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] discard this _(from: old-chat)_\n- [memory] preserve this\n")
    row = drain.pending_rows(vault, workspace="main", runtime_root=runtime)["rows"][0]
    original = drain._atomic_file

    def fail_publication(*args, **kwargs):
        raise OSError("simulated queue replace failure")

    monkeypatch.setattr(drain, "_atomic_file", fail_publication)
    first = drain.dismiss_pending_row(
        vault,
        workspace="main",
        runtime_root=runtime,
        row_id=row["id"],
        expected_revision=row["revision"],
    )
    assert first["failed"]

    monkeypatch.setattr(drain, "_atomic_file", original)
    pending = next(
        item for item in drain.pending_rows(vault, workspace="main", runtime_root=runtime)["rows"]
        if item.get("removal_pending")
    )
    second = drain.dismiss_pending_row(
        vault,
        workspace="main",
        runtime_root=runtime,
        row_id=pending["id"],
        expected_revision=pending["revision"],
    )
    assert second["dismissed"]
    assert queue.read_text() == "- [memory] preserve this\n"


def test_apply_and_revert_preserve_existing_file_modes(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    workspace = vault / "Workspace"
    queue = workspace / "Memory-Proposals.md"
    learning = workspace / "Learnings.md"
    queue.write_text("- [learnings] mode test\n")
    learning.write_text("Existing learning.\n")
    queue.chmod(0o640)
    learning.chmod(0o644)

    applied = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert not applied["failed"]
    assert stat.S_IMODE(queue.stat().st_mode) == 0o640
    assert stat.S_IMODE(learning.stat().st_mode) == 0o644

    reverted = drain.revert(
        vault, Path(applied["receipts"][0]), workspace="main", runtime_root=runtime, apply=True
    )
    assert reverted["reverted"]
    assert stat.S_IMODE(queue.stat().st_mode) == 0o640
    assert stat.S_IMODE(learning.stat().st_mode) == 0o644


def test_apply_with_absent_workspace_is_read_only_not_applicable(tmp_path: Path) -> None:
    vault = tmp_path / "fresh-vault"
    vault.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)

    assert result["not_applicable"]
    assert not result["failed"]
    assert not (vault / "Workspace").exists()


def test_symlinked_workspace_is_rejected_without_touching_its_target(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = tmp_path / "target-workspace"
    target.mkdir()
    (target / "Memory-Proposals.md").write_text("- [learnings] untouched\n")
    (vault / "Workspace").symlink_to(target, target_is_directory=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)

    assert result["failed"]
    assert (target / "Memory-Proposals.md").read_text() == "- [learnings] untouched\n"


def test_prepared_receipt_recovers_before_empty_queue_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    before = "# Queue\n- [learnings] one\n"
    queue.write_text(before)
    resume = drain._resume_forward

    def crash_before_mutation(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash after write-ahead receipt")

    monkeypatch.setattr(drain, "_resume_forward", crash_before_mutation)
    failed = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert failed["failed"]
    receipt = Path(failed["receipts"][0])
    before_preview = receipt.read_bytes()
    queue.write_text("# Queue\n")
    preview = drain.drain(vault, workspace="main", runtime_root=runtime)
    assert preview["unresolved"]
    assert preview["rows"] == []
    assert queue.read_text() == "# Queue\n"
    assert receipt.read_bytes() == before_preview
    blocked = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert blocked["failed"]
    assert not (vault / "Workspace" / "Learnings.md").exists()

    monkeypatch.setattr(drain, "_resume_forward", resume)
    queue.write_text(before)
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert result["rows"] == []
    assert queue.read_text() == "# Queue\n"
    assert (vault / "Workspace" / "Learnings.md").is_file()
    assert json.loads(receipt.read_text())["status"] == "applied"


def test_revert_refuses_changed_learning_and_does_not_touch_queue(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt = Path(result["receipts"][0])
    learning = vault / "Workspace" / "Learnings.md"
    learning.write_text(learning.read_text() + "\nLater edit.\n")
    before_queue = queue.read_bytes()
    reverted = drain.revert(vault, receipt, workspace="main", runtime_root=runtime, apply=True)
    assert not reverted["reverted"]
    assert queue.read_bytes() == before_queue


def test_receipt_is_bound_to_workspace_and_canonical_vault(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt = Path(result["receipts"][0])
    assert drain.revert(vault, receipt, workspace="other", runtime_root=runtime, apply=True)["reverted"] is False


def test_retry_finishes_after_history_write_without_duplicating_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    original = drain._write_receipt

    def fail_terminal(path: Path, receipt: dict[str, object], **kwargs: object) -> None:
        if receipt.get("status") == "applied":
            raise OSError("simulated crash after file writes")
        original(path, receipt, **kwargs)

    monkeypatch.setattr(drain, "_write_receipt", fail_terminal)
    failed = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert failed["failed"]
    assert len(failed["receipts"]) == 1
    monkeypatch.setattr(drain, "_write_receipt", original)
    recovered = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert recovered["failed"] == []
    history = (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text()
    assert len(history.splitlines()) == 1


def test_interrupted_revert_recovers_all_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    applied = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt = Path(applied["receipts"][0])
    atomic = drain._atomic_file

    def fail_queue(path: Path, text: str) -> None:
        if path == queue:
            raise OSError("simulated crash mid-revert")
        atomic(path, text)

    monkeypatch.setattr(drain, "_atomic_file", fail_queue)
    assert drain.revert(vault, receipt, workspace="main", runtime_root=runtime, apply=True)["failed"]
    monkeypatch.setattr(drain, "_atomic_file", atomic)
    assert drain.recover(vault, workspace="main", runtime_root=runtime) == []
    assert queue.read_text() == "- [learnings] one\n"
    assert not (vault / "Workspace" / "Learnings.md").exists()


def test_no_rows_is_positive_not_applicable_and_creates_no_receipt(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [memory] learning word only\n")
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert result["not_applicable"]
    assert result["receipts"] == []
    assert not (runtime / "migration").exists()


def test_recovery_repairs_a_partial_history_append_without_losing_other_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n- [learnings] two\n")
    original = drain.record_promotion
    failed_once = False

    def partial_history(path: Path, **kwargs: object) -> bool:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            row = {
                "text": kwargs["text"], "source": kwargs.get("source", ""),
                "request": kwargs.get("request", ""), "decider": kwargs.get("decider", ""),
                "decide_reason": kwargs.get("decide_reason", ""),
            }
            event = drain._expected_history(
                row, operation_id=kwargs["operation_id"], at=kwargs["decision_at"], outcome="accepted"
            )
            line = (json.dumps(event, ensure_ascii=False) + "\n").encode()
            history = vault / "Workspace" / "Memory-Proposals.dismissed.jsonl"
            history.write_bytes(line[: len(line) // 2])
            raise OSError("simulated partial history append")
        return original(path, **kwargs)

    monkeypatch.setattr(drain, "record_promotion", partial_history)
    failed = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert failed["failed"]
    assert queue.read_text() == ""
    monkeypatch.setattr(drain, "record_promotion", original)
    recovered = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert recovered["failed"] == []
    assert len(recovered["receipts"]) == 1
    assert len((vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text().splitlines()) == 2


def test_concurrent_apply_records_one_receipt_and_one_history_row(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: drain.drain(vault, workspace="main", runtime_root=runtime, apply=True),
            range(2),
        ))
    assert len({receipt for result in results for receipt in result["receipts"]}) == 1
    history = (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text()
    assert len(history.splitlines()) == 1


def test_unrelated_history_append_survives_drain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _vault(tmp_path)
    (vault / "Workspace" / "Memory-Proposals.md").write_text("- [learnings] one\n")
    original = drain.record_promotion
    added = False

    def with_other_writer(path: Path, **kwargs: object) -> bool:
        nonlocal added
        if not added:
            added = True
            original(path, text="concurrent history item", kind="memory", via="operator", outcome="accepted", operation_id="other-writer")
        return original(path, **kwargs)

    monkeypatch.setattr(drain, "record_promotion", with_other_writer)
    drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    rows = [json.loads(line) for line in (vault / "Workspace" / "Memory-Proposals.dismissed.jsonl").read_text().splitlines()]
    assert {row["operation_id"] for row in rows} == {"other-writer", rows[1]["operation_id"]}


def test_tampered_receipt_blocks_preview_and_apply_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    resume = drain._resume_forward
    monkeypatch.setattr(
        drain,
        "_resume_forward",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash")),
    )
    prepared = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    monkeypatch.setattr(drain, "_resume_forward", resume)
    receipt = Path(prepared["receipts"][0])
    changed = json.loads(receipt.read_text())
    changed["queue_after"] = "destructive replacement\n"
    receipt.write_text(json.dumps(changed))
    queue_before = queue.read_bytes()
    preview = drain.inspect(vault, workspace="main", runtime_root=runtime)
    assert preview["unresolved"]
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert result["failed"]
    assert queue.read_bytes() == queue_before


def test_fresh_only_refuses_interrupted_revert_without_any_transaction_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] preserve this lesson\n", encoding="utf-8")
    applied = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt_path = Path(applied["receipts"][0])

    original_revert = drain._resume_revert
    monkeypatch.setattr(
        drain,
        "_resume_revert",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("interrupted revert")),
    )
    interrupted = drain.revert(
        vault, receipt_path, workspace="main", runtime_root=runtime, apply=True
    )
    assert interrupted["failed"]
    monkeypatch.setattr(drain, "_resume_revert", original_revert)

    receipt_before = receipt_path.read_bytes()
    queue_before = queue.read_bytes()
    learning_path = vault / "Workspace" / "Learnings.md"
    learning_before = learning_path.read_bytes()
    history_path = vault / "Workspace" / "Memory-Proposals.dismissed.jsonl"
    history_before = history_path.read_bytes()
    assert json.loads(receipt_before)["status"] == "reverting"

    result = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert result["failed"] == result["unresolved"]
    assert "transaction is reverting" in result["failed"][0]
    assert receipt_path.read_bytes() == receipt_before
    assert queue.read_bytes() == queue_before
    assert learning_path.read_bytes() == learning_before
    assert history_path.read_bytes() == history_before


def test_fresh_only_refuses_prepared_receipt_instead_of_resuming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] keep prepared\n", encoding="utf-8")
    monkeypatch.setattr(
        drain,
        "_resume_forward",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("interrupted apply")),
    )
    prepared = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt_path = Path(prepared["receipts"][0])
    receipt_before = receipt_path.read_bytes()
    queue_before = queue.read_bytes()
    learning_path = vault / "Workspace" / "Learnings.md"
    assert not learning_path.exists()
    history_path = vault / "Workspace" / "Memory-Proposals.dismissed.jsonl"
    assert not history_path.exists()

    result = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert result["failed"] == result["unresolved"]
    assert "transaction is prepared" in result["failed"][0]
    assert receipt_path.read_bytes() == receipt_before
    assert queue.read_bytes() == queue_before
    assert not learning_path.exists()
    assert not history_path.exists()


@pytest.mark.parametrize("broken", [7, {"row": "bad"}, None])
def test_malformed_receipt_rows_are_reported_without_crashing(tmp_path: Path, broken: object) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    receipt = drain._prepare_batch(
        vault, workspace="main", queue_text=queue.read_text(), learning_exists=False, learning_text=""
    )
    assert receipt is not None
    folder = drain._receipt_dir(runtime)
    drain._private_dir(folder, create=True)
    path = folder / f"{drain.RECEIPT_PREFIX}{receipt['run_id']}.json"
    receipt["rows"] = broken
    drain._write_receipt(path, receipt, create=True)

    preview = drain.inspect(vault, workspace="main", runtime_root=runtime)
    assert preview["unresolved"]
    applied = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert applied["failed"]
    assert queue.read_text() == "- [learnings] one\n"


def test_corrupt_receipt_scope_uses_requested_workspace_not_prior_receipt(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    folder = drain._receipt_dir(runtime)
    drain._private_dir(folder, create=True)
    for filename, workspace in (("a-work", "work"), ("z-personal", "personal")):
        path = folder / f"{drain.RECEIPT_PREFIX}{filename}.json"
        path.write_text(json.dumps({"workspace": workspace, "vault_identity": str(vault.resolve())}))
        path.chmod(0o600)

    summary = drain.inspect(vault, workspace="personal", runtime_root=runtime)

    assert len(summary["unresolved"]) == 1
    assert "z-personal" in summary["unresolved"][0]


def test_unidentified_dismissal_receipt_blocks_automatic_migration(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] preserve this lesson\n")
    folder = drain._receipt_dir(runtime)
    drain._private_dir(folder, create=True)
    path = folder / f"{drain.DISMISS_PREFIX}unknown.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)

    result = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert result["failed"]
    assert queue.read_text() == "- [learnings] preserve this lesson\n"
    assert not (vault / "Learnings.md").exists()


def test_foreign_dismissal_receipt_does_not_block_requested_workspace(
    tmp_path: Path,
) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] migrate this lesson\n")
    folder = drain._receipt_dir(runtime)
    drain._private_dir(folder, create=True)
    path = folder / f"{drain.DISMISS_PREFIX}foreign.json"
    path.write_text(
        json.dumps(
            {
                "workspace": "other",
                "vault_identity": str(vault.resolve()),
                "malformed_payload": True,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    result = drain.drain(
        vault, workspace="main", runtime_root=runtime, apply=True, fresh_only=True
    )

    assert not result["failed"]
    assert queue.read_text() == ""
    assert (vault / "Workspace" / "Learnings.md").exists()


def test_apply_aborts_if_workspace_is_replaced_before_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _vault(tmp_path)
    workspace = vault / "Workspace"
    queue = workspace / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    original = drain._write_image
    swapped = False

    def swap_then_write(path: Path, before: tuple[bool, str], after: tuple[bool, str]) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            workspace.rename(vault / "Workspace.saved")
            workspace.mkdir()
            (workspace / "Memory-Proposals.md").write_text("replacement queue\n")
        original(path, before, after)

    monkeypatch.setattr(drain, "_write_image", swap_then_write)
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    assert result["failed"]
    assert (workspace / "Memory-Proposals.md").read_text() == "replacement queue\n"


def test_fifo_queue_is_rejected_without_blocking(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    os.mkfifo(queue)
    preview = drain.inspect(vault, workspace="main", runtime_root=runtime)
    assert preview["failed"]


def test_detector_surfaces_prepared_transaction_with_an_empty_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import update_tasks

    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    monkeypatch.setattr(
        drain,
        "_resume_forward",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("crash")),
    )
    drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    queue.write_text("")
    config = SimpleNamespace(
        workspace_vault_root=lambda _workspace: str(vault),
        state_path=runtime / "state.json",
    )
    probe = update_tasks.DETECTOR_FUNCTIONS["learnings-queue-drain-needed"]
    result = probe(config=config, workspace="main")
    assert result.applicable
    assert result.evidence["rows"] == 0
    assert result.evidence["unresolved"]


def test_receipt_directory_and_file_are_private_and_old_temp_symlink_is_ignored(tmp_path: Path) -> None:
    vault, runtime = _vault(tmp_path)
    queue = vault / "Workspace" / "Memory-Proposals.md"
    queue.write_text("- [learnings] one\n")
    outside = tmp_path / "outside"
    outside.write_text("sentinel")
    planted = queue.with_name(f".{queue.name}.drain.tmp")
    planted.symlink_to(outside)
    result = drain.drain(vault, workspace="main", runtime_root=runtime, apply=True)
    receipt = Path(result["receipts"][0])
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert receipt.parent.stat().st_mode & 0o777 == 0o700
    assert outside.read_text() == "sentinel"
    assert planted.is_symlink()
