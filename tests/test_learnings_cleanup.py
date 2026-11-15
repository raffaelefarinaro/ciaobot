"""The settlement-linked cleanup of ``Workspace/Learnings.md`` (#728-E).

``ciao/skill_proposals.learning_cleanup_eligibility`` already answers "may this
learning be retired?"; this file is the reconciliation that acts on the answer,
and every test here is a way it could remove a line it should not have — or
leave a line it should have.

The three properties the module is built on are each pinned by a test of their
own failure:

* **lossless** — only the eligible Active spans leave; frontmatter except
  ``updated:``, format notes, the Promoted section, the BOM and CRLF all survive;
* **revision-checked** — a file that moved between the plan and the apply is a
  conflict, not a write, and a candidate whose own line moved is dropped from that
  apply rather than removed on a stale answer;
* **suppressed** — a second run removes nothing, an undo is not immediately
  reversed, and a re-reviewed revision makes the entry eligible again.

Every vault here is a throwaway one under ``tmp_path``. No test contacts GitHub,
reads a real vault, or leaves a lock file in the shared temp root.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterator

import pytest

from ciao import learnings_cleanup as lc
from ciao import skill_proposals as sp
from ciao import upstream_drafts
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    parse_learnings,
    render_learning,
)
from ciao.memory_receipts import QueueLockError, content_revision, write_queue_atomically
from ciao import memory_proposals, proposal_actions
from ciao.proposal_tracking import stable_proposal_id

WORKSPACE = "personal"
TODAY = date(2026, 9, 30)
POSIX_UPDATE_RECEIPTS = pytest.mark.skipif(
    os.name == "nt",
    reason="dormant automatic-review receipt transaction requires POSIX directory durability",
)
RECEIPT = "mrcpt_0123456789abcdef"

RETIRED = "Blocked pages need a fallback."
PENDING = "Long transcripts need chunking before they are read."
NEVER_MENTIONED = "Rate limits are per-endpoint, not global."


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every lock file out of the shared temp root the install uses."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


# ── Fixtures ────────────────────────────────────────────────────────────────


def _config(tmp_path: Path, *names: str) -> CiaoConfig:
    workspaces = names or (WORKSPACE,)
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in names
        }
        or {
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )


def _record(text: str, key: str, legacy: str) -> LearningRecord:
    """One canonical record, identified by the legacy line it was minted from."""
    return LearningRecord(
        learning_id=allocate_learning_id(WORKSPACE, legacy), key=key, text=text
    )


RETIRED_RECORD = _record(RETIRED, "blocked-pages", f"- {RETIRED}")
PENDING_RECORD = _record(PENDING, "long-transcripts", f"- {PENDING}")
UNLINKED_RECORD = _record(NEVER_MENTIONED, "rate-limits", f"- {NEVER_MENTIONED}")
#: Two more Active entries, so a run can remove three of five and produce every
#: gap shape: three separate gaps, one gap shared by a removed pair, and one gap
#: shared by a whole run of them. A single removal cannot tell those apart.
FOURTH_RECORD = _record(
    "Session logs grow faster than the notes they describe.",
    "log-growth",
    "- Session logs grow faster than the notes they describe.",
)
FIFTH_RECORD = _record(
    "Promotions need a dated review, not a mention.",
    "dated-review",
    "- Promotions need a dated review, not a mention.",
)
PROMOTED_RECORD = LearningRecord(
    learning_id=allocate_learning_id(WORKSPACE, "- [shipped] already done"),
    key="shipped",
    text="An entry that is already done.",
    first_seen=date(2024, 1, 1),
    last_seen=date(2024, 1, 2),
    count=3,
)

FRONTMATTER = "---\ntags: [ciao, learnings]\nupdated: 2020-01-01\n---\n"
#: The same frontmatter with an ``updated:`` the restamp has to *lengthen*, so the
#: write moves every offset below it.
_SHORT_STAMP = "---\ntags: [ciao, learnings]\nupdated: 2020-1-1\n---\n"


def test_system_update_review_undo_restores_original_frontmatter(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    original = _document(UNLINKED_RECORD, frontmatter=_SHORT_STAMP)
    path = _write(config, original)
    plan = lc.plan_cleanup(root, workspace=WORKSPACE, config=config)
    row = next(row for row in plan.rows if row.learning_id == UNLINKED_RECORD.learning_id)
    landed, spans = lc._splice(original, [row])
    lc._with_anchors(spans, landed)
    receipt = lc._build_receipt(
        plan, spans, vault_root=root, before=content_revision(original),
        after=content_revision(landed), actor="system/update-maintenance",
        approvals={}, reviewed=False,
    )
    receipt["automatic_review"] = {
        "schema": 1, "actor": "system/update-maintenance",
        "task_id": "learnings-cleanup", "task_revision": 3, "workspace": WORKSPACE,
    }
    path.write_text(landed, encoding="utf-8", newline="")

    result = lc.unmigrate_cleanup(root, receipt, apply=True, today=TODAY)

    assert not result["failed"]
    assert path.read_bytes() == original.encode("utf-8")


def test_update_review_input_validation_complete_and_protected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    plan = lc.plan_cleanup(root, workspace=WORKSPACE, config=config)
    verdict = {
        "learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD),
        "verdict": "RETIRE",
        "reason": "Obsolete",
        "evidence": ["Superseded by current practice"],
    }
    validate = lambda rows, suppressed=set(): lc._validate_update_review_inputs(
        text, workspace=WORKSPACE, source_revision=content_revision(text),
        verdicts=rows, plan=plan, suppressed=suppressed,
    )
    assert validate([verdict]) == [plan.rows[0]]
    for invalid in ([], [verdict, verdict], [{**verdict, "extra": 1}],
                    [{**verdict, "entry_revision": "stale"}],
                    [{**verdict, "evidence": ["bad\nline"]}],
                    [{**verdict, "proposal_ref": {"kind": "memory", "id": "0" * 16}}]):
        with pytest.raises(ValueError):
            validate(invalid)
    with pytest.raises(ValueError):
        validate([verdict], {(verdict["learning_id"], verdict["entry_revision"])})
    with pytest.raises(ValueError):
        validate([verdict], {(str(n), "rev") for n in range(lc.MAX_SUPPRESSED)})


def test_update_review_finding_snapshot_detects_bad_and_changed_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = lc._finding_dependency_snapshot(config, WORKSPACE)
    assert first == lc._finding_dependency_snapshot(config, WORKSPACE)
    queue = sp.queue_dir(config, WORKSPACE)
    queue.mkdir(parents=True)
    (queue / "bad.md").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        lc._finding_dependency_snapshot(config, WORKSPACE)
    (queue / "bad.md").unlink()
    assert first != lc._finding_dependency_snapshot(config, WORKSPACE)


@POSIX_UPDATE_RECEIPTS
def test_update_review_recorded_rechecks_findings_after_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130001.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    assert not result.failed
    original = lc._update_fence_receipt
    def fence(path, expected):
        ok = original(path, expected)
        queue = sp.queue_dir(config, WORKSPACE)
        queue.mkdir(parents=True, exist_ok=True)
        (queue / "bad.md").write_text("", encoding="utf-8")
        return ok
    monkeypatch.setattr(lc, "_update_fence_receipt", fence)
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


def test_update_review_validation_bounds_and_defer_schema(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    plan = lc.plan_cleanup(root, workspace=WORKSPACE, config=config)
    item = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
            "verdict": "DEFER", "reason": "Question", "evidence": ["é"],
            "proposal_ref": {"kind": "memory", "id": "a" * 16}}
    kwargs = dict(workspace=WORKSPACE, source_revision=content_revision(text), plan=plan, suppressed=set())
    assert lc._validate_update_review_inputs(text, verdicts=[item], **kwargs) == []
    size = len(json.dumps([item], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert lc._validate_update_review_inputs(text, verdicts=[item], max_payload_bytes=size, **kwargs) == []
    with pytest.raises(ValueError):
        lc._validate_update_review_inputs(text, verdicts=[item], max_payload_bytes=size - 1, **kwargs)
    for value in ({"kind": "skill", "id": "a" * 16}, {"kind": "memory", "id": "A" * 16},
                  {"kind": "memory", "id": "a" * 16, "extra": 1}):
        with pytest.raises(ValueError):
            lc._validate_update_review_inputs(text, verdicts=[{**item, "proposal_ref": value}], **kwargs)
    for value in ("", "x" * 2049, "x\tz"):
        with pytest.raises(ValueError):
            lc._validate_update_review_inputs(text, verdicts=[{**item, "reason": value}], **kwargs)
    with pytest.raises(ValueError):
        lc._validate_update_review_inputs(text, verdicts=[item] * 513, **kwargs)


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
@pytest.mark.parametrize("field", ["reason", "evidence"])
def test_automatic_review_rejects_unicode_line_separator(tmp_path: Path, separator: str, field: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130001.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    bad = {**verdict, field: ("a" + separator + "b") if field == "reason" else ["a" + separator + "b"]}
    with pytest.raises(ValueError):
        lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                               source_revision=content_revision(text), receipt_path=receipt, verdicts=[bad])
    assert note.read_bytes() == text.encode() and not receipt.exists()
    lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                           source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    data = json.loads(receipt.read_text(encoding="utf-8"))
    data["automatic_review"]["verdicts"][0][field] = bad[field]
    receipt.write_text(json.dumps(data), encoding="utf-8")
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_automatic_receipt_fifo_does_not_block_validator_or_recovery(tmp_path: Path) -> None:
    import multiprocessing

    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    _write(config, _document(UNLINKED_RECORD))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130002.json"
    receipt.parent.mkdir(parents=True, mode=0o700)
    os.mkfifo(receipt, 0o600)
    def check() -> None:
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is None
        assert lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name).failed
    process = multiprocessing.get_context("fork").Process(target=check)
    process.start()
    process.join(3)
    if process.is_alive():
        process.terminate()
        process.join()
        pytest.fail("FIFO receipt blocked without a writer")
    assert process.exitcode == 0
    assert stat.S_ISFIFO(receipt.lstat().st_mode)


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("bad", [None, [], {}, {"entry_revision": "rev"},
                                {"learning_id": "id"}, {"learning_id": 7, "entry_revision": "rev"},
                                {"learning_id": "id", "entry_revision": 7},
                                {"learning_id": "", "entry_revision": "rev"}])
def test_automatic_review_rejects_malformed_suppression_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    directory = config.state_path.parent / "migration"
    committed = directory / "learnings-cleanup-20261114-130003.json"
    pending = directory / "learnings-cleanup-20261114-130004.json"
    new = directory / "learnings-cleanup-20261114-130005.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    def apply(path: Path):
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=path, verdicts=[verdict])
    assert not apply(committed).failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=committed.name) is not None
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("leave pending")
        patch.setattr(lc, "_update_commit_receipt", fail)
        assert apply(pending).failed
    store = lc.suppression_path(root)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"schema_version": lc.SUPPRESSION_SCHEMA, "removed": [
        {"learning_id": "valid", "entry_revision": "rev"}, bad]}), encoding="utf-8")
    before = (note.read_bytes(), store.read_bytes(), committed.read_bytes(), pending.read_bytes())
    assert apply(new).failed and not new.exists()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=committed.name) is None
    assert lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                    task_revision=3, receipt_name=pending.name).failed
    assert (note.read_bytes(), store.read_bytes(), committed.read_bytes(), pending.read_bytes()) == before


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("store", ["skill", "skill_blank", "upstream"])
@pytest.mark.parametrize("operation", ["apply", "recover"])
def test_automatic_rejects_unattributable_origins(tmp_path: Path, monkeypatch, store: str, operation: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    if store.startswith("skill"):
        _file_settled(config, _origin(FOURTH_RECORD))
        finding = sp.proposal_path(config, WORKSPACE, "web-research")
    else:
        _draft(config, FOURTH_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)
        finding = next(upstream_drafts.sidecar_dir(config, WORKSPACE).glob("*.json"))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130010.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    def apply():
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation == "recover":
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("leave pending")
            patch.setattr(lc, "_update_commit_receipt", fail)
            assert apply().failed
    if store.startswith("skill"):
        original = finding.read_text(encoding="utf-8")
        if store == "skill_blank":
            malformed = original.replace(f'"learning_id":"{FOURTH_RECORD.learning_id}"', '"learning_id":"   "')
        else:
            malformed = original.replace('"schema":1', '"schema":99')
        finding.write_text(malformed, encoding="utf-8")
        assert finding.read_text(encoding="utf-8") != original
        parsed = sp.parse_proposal(finding, WORKSPACE)
        assert parsed is not None
        if store == "skill_blank":
            assert parsed.origins[0].learning_id == "   "
    else:
        data = json.loads(finding.read_text(encoding="utf-8"))
        data["origins"] = ["unattributable"]
        finding.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        lc._finding_dependency_capture(config, WORKSPACE)
    if operation == "recover":
        with monkeypatch.context() as patch:
            patch.setattr(lc, "_validate_automatic_origins", lambda *args: None)
            fingerprint = lc._finding_dependency_snapshot(config, WORKSPACE)
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["automatic_review"]["finding_revision"] = fingerprint
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    before = (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None)
    result = (apply() if operation == "apply" else
              lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                       task_revision=3, receipt_name=receipt.name))
    assert result.failed
    assert (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None) == before
    assert not lc.suppression_path(root).exists()
    if operation == "recover":
        with monkeypatch.context() as patch:
            patch.setattr(lc, "_validate_automatic_origins", lambda *args: None)
            assert not lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                                task_revision=3, receipt_name=receipt.name).failed


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("operation", ["apply", "recover"])
def test_automatic_retire_rejects_whitespace_finding(tmp_path: Path, monkeypatch, operation: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    _file_settled(config, _origin(FOURTH_RECORD))
    finding = sp.proposal_path(config, WORKSPACE, "web-research")
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-140030.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    def apply():
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation == "recover":
        with monkeypatch.context() as patch:
            original_commit = lc._update_commit_receipt
            calls = 0
            def fail(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("leave pending")
                return original_commit(*args, **kwargs)
            patch.setattr(lc, "_update_commit_receipt", fail)
            result = apply()
            assert result.applied and result.failed
    original = finding.read_text(encoding="utf-8")
    line = next(line for line in original.splitlines() if line.startswith('{"'))
    data = json.loads(line)
    data["finding"] = "   "
    finding.write_text(original.replace(line, json.dumps(data)), encoding="utf-8")
    parsed = sp.parse_proposal(finding, WORKSPACE)
    assert parsed is not None and parsed.origins[0].finding == ""
    if operation == "recover":
        with monkeypatch.context() as patch:
            patch.setattr(lc, "_validate_automatic_origins", lambda *args: None)
            fingerprint = lc._finding_dependency_snapshot(config, WORKSPACE)
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["automatic_review"]["finding_revision"] = fingerprint
        receipt.write_text(json.dumps(data), encoding="utf-8")
    before = (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None,
              lc.suppression_path(root).read_bytes() if lc.suppression_path(root).exists() else None)
    with pytest.raises(ValueError):
        lc._finding_dependency_capture(config, WORKSPACE)
    result = (apply() if operation == "apply" else
              lc.recover_update_review(root, config=config, workspace=WORKSPACE, task_revision=3, receipt_name=receipt.name))
    assert result.failed
    assert (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None,
            lc.suppression_path(root).read_bytes() if lc.suppression_path(root).exists() else None) == before
    if operation == "recover":
        with monkeypatch.context() as patch:
            patch.setattr(lc, "_validate_automatic_origins", lambda *args: None)
            assert not lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                                task_revision=3, receipt_name=receipt.name).failed


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("target", ["queue", "sidecar"])
def test_deferred_evidence_refuses_fifo_without_blocking(tmp_path: Path, target: str) -> None:
    import threading
    from ciao.memory_proposals import _sidecar_paths

    root = tmp_path / "vault"
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    path = queue if target == "queue" else _sidecar_paths(queue)[0]
    os.mkfifo(path)
    verdict = {"verdict": "DEFER", "learning_id": "x", "entry_revision": "rev",
               "proposal_ref": {"id": "id"}}
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(lc._deferred_references_valid(root, WORKSPACE, [verdict])), daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert result == [False]


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("target", ["queue", "sidecar"])
def test_public_defer_fifo_fails_apply_recover_and_verifier(tmp_path: Path, monkeypatch, target: str) -> None:
    import threading
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    queue = root / "Workspace" / "Memory-Proposals.md"
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    queue.write_text(f"- [memory] Fact _(from: {source})_\n", encoding="utf-8")
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", "memory", "Fact", source, 0)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Review", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    directory = config.state_path.parent / "migration"
    committed = directory / "learnings-cleanup-20261114-140001.json"
    pending = directory / "learnings-cleanup-20261114-140002.json"
    new = directory / "learnings-cleanup-20261114-140003.json"
    def apply(path):
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=path, verdicts=[verdict])
    assert not apply(committed).failed
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("leave pending")
        patch.setattr(lc, "_update_commit_receipt", fail)
        assert apply(pending).failed
    path = queue if target == "queue" else memory_proposals.dismissed_log_path(queue)
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    before = (note.read_bytes(), committed.read_bytes(), pending.read_bytes())
    def run(call):
        values = []
        def invoke():
            try:
                values.append(call())
            except ValueError as exc:
                values.append(exc)
        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        thread.join(2)
        assert not thread.is_alive()
        return values[0]
    assert isinstance(run(lambda: apply(new)), ValueError)
    assert run(lambda: lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                                task_revision=3, receipt_name=pending.name)).failed
    assert run(lambda: lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                                  task_revision=3, receipt_name=committed.name)) is None
    assert (note.read_bytes(), committed.read_bytes(), pending.read_bytes()) == before
    assert not new.exists()


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("store", ["skill", "upstream"])
def test_automatic_classification_uses_bounded_owner_records(tmp_path: Path, monkeypatch, store: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    if store == "skill":
        _file_settled(config, _origin(FOURTH_RECORD))
        finding = sp.proposal_path(config, WORKSPACE, "web-research")
    else:
        _draft(config, FOURTH_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)
        finding = next(upstream_drafts.sidecar_dir(config, WORKSPACE).glob("*.json"))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130006.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    def forbidden(*args, **kwargs):
        raise AssertionError("automatic classification reopened owner records")
    monkeypatch.setattr(sp, "read_records", forbidden)
    monkeypatch.setattr(upstream_drafts, "read_records", forbidden)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    assert not result.failed
    finding.write_bytes(b"x" * (1024 * 1024 + 1))
    other = receipt.with_name("learnings-cleanup-20261114-130007.json")
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=other, verdicts=[verdict])
    assert result.failed and not other.exists() and note.read_bytes() == text.encode()


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("operation", ["apply", "recover"])
@pytest.mark.parametrize("when", ["open", "classify"])
def test_automatic_finding_growth_fails_without_effects(tmp_path: Path, monkeypatch, operation: str, when: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    _file_settled(config, _origin(FOURTH_RECORD))
    finding = sp.proposal_path(config, WORKSPACE, "web-research")
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130008.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    def apply():
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation == "recover":
        with monkeypatch.context() as patch:
            def fail_commit(*args, **kwargs):
                raise OSError("leave pending")
            patch.setattr(lc, "_update_commit_receipt", fail_commit)
            assert apply().failed
    receipt_before = receipt.read_bytes() if receipt.exists() else None
    original_open, original_plan = lc.os.open, lc.plan_cleanup
    grown = False
    def grow() -> None:
        nonlocal grown
        if not grown:
            grown = True
            with finding.open("ab") as handle:
                handle.write(b"x" * (1024 * 1024 + 1))
    if when == "open":
        def opening(name, flags, *args, **kwargs):
            if isinstance(name, (str, os.PathLike)) and Path(name) == finding:
                grow()
            return original_open(name, flags, *args, **kwargs)
        monkeypatch.setattr(lc.os, "open", opening)
    else:
        def classify(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            grow()
            return result
        monkeypatch.setattr(lc, "plan_cleanup", classify)
    result = (apply() if operation == "apply" else
              lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                       task_revision=3, receipt_name=receipt.name))
    assert grown and result.failed
    assert note.read_bytes() == text.encode()
    assert (receipt.read_bytes() if receipt.exists() else None) == receipt_before
    assert not lc.suppression_path(root).exists()


@POSIX_UPDATE_RECEIPTS
def test_automatic_recovery_classifies_captured_records(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    _file_settled(config, _origin(FOURTH_RECORD))
    _draft(config, FOURTH_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130009.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("leave pending")
        patch.setattr(lc, "_update_commit_receipt", fail)
        assert lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt,
                                      verdicts=[verdict]).failed
    def forbidden(*args, **kwargs):
        raise AssertionError("recovery reopened owner records")
    monkeypatch.setattr(sp, "read_records", forbidden)
    monkeypatch.setattr(upstream_drafts, "read_records", forbidden)
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert not recovered.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None


@POSIX_UPDATE_RECEIPTS
def test_automatic_finding_enumeration_stops_at_first_excess(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    directory = sp.queue_dir(config, WORKSPACE)
    directory.mkdir(parents=True)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130014.json"
    real = Path.iterdir
    seen = 0
    def iter_directory(path):
        if path != directory:
            return real(path)
        def names():
            nonlocal seen
            for i in range(4097):
                seen += 1
                yield directory / f"{i:05}.md"
            raise AssertionError("enumerated beyond first excess")
        return names()
    monkeypatch.setattr(Path, "iterdir", iter_directory)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=receipt,
                                    verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
                                               "entry_revision": entry_revision(UNLINKED_RECORD),
                                               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}])
    assert result.failed and seen == 4097
    assert note.read_bytes() == text.encode() and not receipt.exists()


def test_finding_verification_enumeration_stops_at_first_new_file(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    directory = sp.queue_dir(config, WORKSPACE)
    directory.mkdir(parents=True)
    real = Path.iterdir
    scans = 0
    def iter_directory(path):
        nonlocal scans
        if path != directory:
            return real(path)
        scans += 1
        if scans == 1:
            return iter(())
        def names():
            yield directory / "new.md"
            raise AssertionError("enumerated past first excess")
        return names()
    monkeypatch.setattr(Path, "iterdir", iter_directory)
    with pytest.raises(ValueError):
        lc._finding_dependency_snapshot(config, WORKSPACE)
    assert scans == 2


def test_update_review_snapshot_parses_every_file_and_enforces_limits(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _file_settled(config, _origin(RETIRED_RECORD))
    queue = sp.queue_dir(config, WORKSPACE)
    path = next(queue.glob("*.md"))
    first = lc._finding_dependency_snapshot(config, WORKSPACE)
    path.write_text(path.read_text(encoding="utf-8") + "\n## Extra\n\nExtra\n", encoding="utf-8")
    assert lc._finding_dependency_snapshot(config, WORKSPACE) != first
    for limits in ({"max_files": 0}, {"max_file_bytes": 1}, {"max_total_bytes": 1}):
        with pytest.raises(ValueError):
            lc._finding_dependency_snapshot(config, WORKSPACE, **limits)
    upstream = upstream_drafts.sidecar_dir(config, WORKSPACE)
    upstream.mkdir(parents=True)
    (upstream / "malformed.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        lc._finding_dependency_snapshot(config, WORKSPACE)


def test_update_review_snapshot_refuses_symlink(tmp_path: Path) -> None:
    config = _config(tmp_path)
    queue = sp.queue_dir(config, WORKSPACE)
    queue.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("hello", encoding="utf-8")
    try:
        (queue / "linked.md").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        lc._finding_dependency_snapshot(config, WORKSPACE)


def test_update_review_retirement_capacity_500_and_protected_classification() -> None:
    records = [_record(f"Lesson {n}.", f"lesson-{n}", f"- Lesson {n}.") for n in range(501)]
    text = _document(*records)
    rows = tuple(lc.CleanupRow(
        learning_id=r.learning_id, key=r.key, line="", entry_revision=entry_revision(r),
        action=lc.KEEP, reason="never_proposed",
    ) for r in records)
    plan = lc.CleanupPlan(workspace=WORKSPACE, path=lc.LEARNINGS_RELATIVE,
                          revision=content_revision(text), kept=rows, active=501)
    verdicts = [{"learning_id": r.learning_id, "entry_revision": entry_revision(r),
                 "verdict": "RETIRE" if n < 500 else "KEEP",
                 "reason": "Obsolete", "evidence": ["Reviewed"]}
                for n, r in enumerate(records)]
    kwargs = dict(workspace=WORKSPACE, source_revision=content_revision(text), plan=plan, suppressed=set())
    assert len(lc._validate_update_review_inputs(text, verdicts=verdicts, **kwargs)) == 500
    with pytest.raises(ValueError):
        lc._validate_update_review_inputs(text, verdicts=verdicts[:-1] + [{**verdicts[-1], "verdict": "RETIRE"}], **kwargs)
    protected = replace(plan, kept=(replace(rows[0], reason="pending"),) + rows[1:])
    with pytest.raises(ValueError):
        lc._validate_update_review_inputs(text, verdicts=verdicts, **{**kwargs, "plan": protected})


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("count,existing", [(500, False), (501, False), (500, True)])
def test_update_review_public_retirement_capacity(tmp_path: Path, count: int, existing: bool) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    records = [_record(f"Lesson {n}.", f"lesson-{n}", f"- Lesson {n}.") for n in range(count)]
    text = _document(*records)
    path = _write(config, text)
    if existing:
        lc.write_suppressions(root, [("prior", "revision")])
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120028.json"
    verdicts = [{"learning_id": item.learning_id, "entry_revision": entry_revision(item),
                 "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]} for item in records]
    kwargs = dict(config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
                  verdicts=verdicts, receipt_path=receipt)
    if count == 500 and not existing:
        result = lc.apply_update_review(root, **kwargs)
        assert result.applied and not result.failed and len(lc.read_suppressions(root)) == 500
    else:
        with pytest.raises(ValueError):
            lc.apply_update_review(root, **kwargs)
        assert path.read_bytes() == text.encode() and not receipt.exists()
        assert set(lc.read_suppressions(root)) == ({("prior", "revision")} if existing else set())


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("state", ["pending", "reopened", "held_back", "upstream", "unreadable", "malformed"])
def test_update_review_public_mixed_protection_is_atomic(tmp_path: Path, state: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD, RETIRED_RECORD)
    if state == "unreadable":
        text = text.replace(f"{render_learning(RETIRED_RECORD)}\n",
                            f"{render_learning(RETIRED_RECORD)}\n- [broken] [2024-13-45 → nope] (x0) Bad dates.\n")
    path = _write(config, text)
    if state == "pending":
        _file_settled(config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification=""))
    elif state == "reopened":
        _file_settled(config, _origin(RETIRED_RECORD))
        assert any(row.learning_id == RETIRED_RECORD.learning_id for row in
                   lc.plan_cleanup(root, config=config, workspace=WORKSPACE).removals)
        _file_settled(config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification=""))
    elif state == "held_back":
        _file_settled(config, _origin(RETIRED_RECORD), sp.SkillOrigin(finding="unattributed"))
    elif state == "upstream":
        _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)
    elif state == "malformed":
        queue = sp.queue_dir(config, WORKSPACE)
        queue.mkdir(parents=True)
        (queue / "unlinked.md").write_text("", encoding="utf-8")
    if state in {"pending", "reopened", "held_back", "upstream"}:
        plan = lc.plan_cleanup(root, config=config, workspace=WORKSPACE)
        protected = next(row for row in plan.rows if row.learning_id == RETIRED_RECORD.learning_id)
        assert protected.reason == {"pending": "pending", "reopened": "pending",
                                    "held_back": "held_back", "upstream": "stock_waiting_upstream"}[state]
    finding_before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*")
                      if p.is_file() and p.suffix in {".md", ".json"}}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120029.json"
    verdicts = [{"learning_id": row.learning_id, "entry_revision": entry_revision(row), "verdict": "RETIRE",
                 "reason": "Obsolete", "evidence": ["Reviewed"]} for row in (UNLINKED_RECORD, RETIRED_RECORD)]
    try:
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), verdicts=verdicts, receipt_path=receipt)
    except ValueError:
        pass
    else:
        assert result.failed or result.conflicts
    assert path.read_bytes() == text.encode() and not receipt.exists() and not lc.suppression_path(root).exists()
    assert {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*")
            if p.is_file() and p.suffix in {".md", ".json"}} == finding_before


def test_update_review_snapshot_rechecks_earlier_store_after_later_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    _file_settled(config, _origin(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)
    parse = upstream_drafts.read_sidecar
    skill = next(sp.queue_dir(config, WORKSPACE).glob("*.md"))

    def changed(path: Path, workspace: str, *, text: str | None = None) -> upstream_drafts.UpstreamDraft | None:
        result = parse(path, workspace, text=text)
        skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        return result

    monkeypatch.setattr(upstream_drafts, "read_sidecar", changed)
    with pytest.raises(ValueError):
        lc._finding_dependency_snapshot(config, WORKSPACE)


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("verdict", ["KEEP", "REFRESH", "RETIRE"])
@pytest.mark.parametrize("bom,newline", [(False, "\n"), (True, "\r\n")])
def test_apply_update_review_records_real_effects_and_undo(tmp_path: Path, verdict: str, bom: bool, newline: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = ("\ufeff" if bom else "") + _document(UNLINKED_RECORD, frontmatter=_SHORT_STAMP).replace("\n", newline)
    path = _write(config, text)
    receipt_path = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120000.json"
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt_path,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
                   "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": verdict,
                   "reason": "Reviewed", "evidence": ["Current practice"]}],
    )
    assert not result.failed and not result.conflicts
    assert result.applied is (verdict == "RETIRE")
    assert receipt_path.exists()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt_path.name) is not None
    if verdict == "RETIRE":
        assert lc.unmigrate_cleanup(root, result.receipt, apply=True)["failed"] == []
    assert path.read_bytes() == text.encode("utf-8")


@POSIX_UPDATE_RECEIPTS
def test_undo_holds_learning_lock_until_suppression_persists(tmp_path: Path, monkeypatch) -> None:
    import threading
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    first = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130012.json"
    second = first.with_name("learnings-cleanup-20261114-130013.json")
    real = lc.write_suppressions
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("interrupted")
        patch.setattr(lc, "write_suppressions", fail)
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), verdicts=[verdict], receipt_path=first)
    assert result.applied and result.failed and not lc.suppression_path(root).exists()
    entered, release, review_done = threading.Event(), threading.Event(), threading.Event()
    results: dict[str, object] = {}
    def wait_write(*args, **kwargs):
        if threading.current_thread().name == "undo":
            entered.set()
            assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(lc, "write_suppressions", wait_write)
    def undo():
        results["undo"] = lc.unmigrate_cleanup(root, result.receipt, apply=True)
    def review():
        try:
            results["review"] = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                                        source_revision=content_revision(text), verdicts=[verdict],
                                                        receipt_path=second)
        except ValueError as exc:
            results["review"] = exc
        finally:
            review_done.set()
    a = threading.Thread(target=undo, name="undo")
    b = threading.Thread(target=review, name="review")
    a.start()
    try:
        assert entered.wait(5)
        b.start()
        assert not review_done.wait(0.3)
    finally:
        release.set()
        a.join(5)
        if b.ident is not None:
            b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert not results["undo"]["failed"]
    assert note.read_bytes() == text.encode()
    assert (UNLINKED_RECORD.learning_id, entry_revision(UNLINKED_RECORD)) in lc.read_suppressions(root)
    assert not second.exists()


@POSIX_UPDATE_RECEIPTS
def test_undo_reports_restoration_when_suppression_write_lock_fails(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-140020.json"
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("interrupted")
        patch.setattr(lc, "write_suppressions", fail)
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), verdicts=[verdict], receipt_path=receipt)
    assert result.applied and result.failed
    def lock_failure(*args, **kwargs):
        raise lc.QueueLockError("suppression busy")
    monkeypatch.setattr(lc, "write_suppressions", lock_failure)
    undo = lc.unmigrate_cleanup(root, result.receipt, apply=True)
    assert note.read_bytes() == text.encode()
    assert undo["applied"] and undo["entries_reverted"] == 1
    assert undo["reverted"] == [lc.LEARNINGS_RELATIVE]
    assert undo["failed"] and not undo["suppressions_kept"]


@POSIX_UPDATE_RECEIPTS
def test_undo_detects_suppression_write_that_omits_required_pair(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-140022.json"
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("interrupted")
        patch.setattr(lc, "write_suppressions", fail)
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), verdicts=[verdict], receipt_path=receipt)
    assert result.applied and result.failed
    monkeypatch.setattr(lc, "write_suppressions", lambda *args, **kwargs: {})
    undo = lc.unmigrate_cleanup(root, result.receipt, apply=True)
    assert note.read_bytes() == text.encode()
    assert undo["applied"] and undo["entries_reverted"] == 1 and undo["failed"]
    assert undo["suppressions_kept"] == []


@POSIX_UPDATE_RECEIPTS
def test_undo_refuses_full_suppression_store_before_restoration(tmp_path: Path, monkeypatch) -> None:
    import threading
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD, FOURTH_RECORD)
    note = _write(config, text)
    verdicts = [{"learning_id": row.learning_id, "entry_revision": entry_revision(row),
                 "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
                for row in (UNLINKED_RECORD, FOURTH_RECORD)]
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-140021.json"
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("interrupted")
        patch.setattr(lc, "write_suppressions", fail)
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), verdicts=verdicts, receipt_path=receipt)
    assert result.applied and result.failed
    first = (UNLINKED_RECORD.learning_id, entry_revision(UNLINKED_RECORD))
    lc.write_suppressions(root, [first] + [(f"old-{n}", "revision") for n in range(lc.MAX_SUPPRESSED - 1)])
    before_note, before_store = note.read_bytes(), lc.suppression_path(root).read_bytes()
    entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
    read = lc.read_suppressions
    def wait_read(*args, **kwargs):
        value = read(*args, **kwargs)
        if threading.current_thread().name == "undo":
            entered.set()
            assert release.wait(5)
        return value
    monkeypatch.setattr(lc, "read_suppressions", wait_read)
    results = {}
    undo_thread = threading.Thread(name="undo", target=lambda: results.update(
        undo=lc.unmigrate_cleanup(root, result.receipt, apply=True)))
    def compete():
        with lc.queue_lock(lc.suppression_path(root)):
            acquired.set()
    competitor = threading.Thread(target=compete)
    undo_thread.start()
    try:
        assert entered.wait(5)
        competitor.start()
        assert not acquired.wait(0.3)
    finally:
        release.set()
        undo_thread.join(5)
        if competitor.ident is not None:
            competitor.join(5)
    assert not undo_thread.is_alive() and not competitor.is_alive() and acquired.is_set()
    undo = results["undo"]
    assert undo["failed"] and not undo["applied"] and undo["entries_reverted"] == 0
    assert not undo["suppressions_kept"]
    assert note.read_bytes() == before_note and lc.suppression_path(root).read_bytes() == before_store


@POSIX_UPDATE_RECEIPTS
def test_apply_update_review_postcheck_change_restores_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120001.json"
    snapshot = lc._finding_dependency_snapshot
    calls = 0

    def changed(config: CiaoConfig, workspace: str) -> str:
        nonlocal calls
        calls += 1
        return "changed" if calls == 3 else snapshot(config, workspace)

    monkeypatch.setattr(lc, "_finding_dependency_snapshot", changed)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied
    assert path.read_bytes() == text.encode()
    assert json.loads(receipt.read_text())["automatic_review"]["committed"] is False
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_apply_update_review_suppression_failure_leaves_pending_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120002.json"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected suppression failure")

    monkeypatch.setattr(lc, "write_suppressions", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.applied and result.failed
    assert path.read_bytes() != text.encode()
    assert json.loads(receipt.read_text())["automatic_review"]["committed"] is False
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_apply_update_review_refuses_foreign_receipt_and_defer(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120003.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("foreign", encoding="utf-8")
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Reviewed", "evidence": ["Reviewed"]}
    kwargs = dict(config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
                  receipt_path=receipt)
    result = lc.apply_update_review(root, verdicts=[verdict], **kwargs)
    assert result.failed and receipt.read_text() == "foreign"
    with pytest.raises(ValueError):
        lc.apply_update_review(root, verdicts=[{**verdict, "verdict": "DEFER",
                               "proposal_ref": {"kind": "memory", "id": "a" * 16}}], **kwargs)
    assert path.read_bytes() == text.encode()


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("tamper", ["committed", "coverage", "span", "suppression", "revision",
                                    "actor", "workspace", "task_revision", "verdict", "removal"])
def test_update_review_recorded_rejects_tampered_effects(tmp_path: Path, tamper: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120004.json"
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert not result.failed
    data = json.loads(receipt.read_text())
    if tamper == "committed":
        data["automatic_review"]["committed"] = False
    elif tamper == "coverage":
        data["automatic_review"]["input_entries"] = []
    elif tamper == "span":
        data["removals"][0]["from"] += "x"
    elif tamper == "suppression":
        lc.suppression_path(root).unlink()
    elif tamper == "actor":
        data["automatic_review"]["actor"] = "operator"
    elif tamper == "workspace":
        data["automatic_review"]["workspace"] = "foreign"
    elif tamper == "task_revision":
        data["automatic_review"]["task_revision"] = 4
    elif tamper == "verdict":
        data["automatic_review"]["verdicts"] = []
    elif tamper == "removal":
        data["automatic_review"]["verdicts"][0]["verdict"] = "KEEP"
    else:
        path.write_text(path.read_text() + "edited", encoding="utf-8")
    if tamper not in {"suppression", "revision"}:
        receipt.write_text(json.dumps(data), encoding="utf-8")
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


def test_update_review_receipt_capability_failure_precedes_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120005.json"

    monkeypatch.setattr(lc, "_update_directory_durability_supported", lambda: False)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied and not receipt.exists()
    assert path.read_bytes() == text.encode()


def test_update_review_unsupported_precedes_queue_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120015.json"
    monkeypatch.setattr(lc, "_update_directory_durability_supported", lambda: False)
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("queue lock was entered")
    monkeypatch.setattr(lc, "queue_lock", forbidden)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision="unused", verdicts=[], receipt_path=receipt)
    recovery = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name)
    assert result.failed and recovery.failed and not receipt.parent.exists()


@POSIX_UPDATE_RECEIPTS
def test_update_review_suppression_directory_fails_before_receipt_or_note(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    suppression = lc.suppression_path(root)
    suppression.mkdir()
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120031.json"
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied
    assert path.read_bytes() == text.encode() and suppression.is_dir() and not receipt.exists()


@POSIX_UPDATE_RECEIPTS
def test_suppression_descriptor_read_failure_refuses_apply(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    lc.write_suppressions(root, [("existing", "revision")])
    store = lc.suppression_path(root)
    before = store.read_bytes()
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120038.json"
    real_open = lc.os.open

    def fail(name, flags, *args, **kwargs):
        if Path(name) == store and flags & os.O_ACCMODE == os.O_RDONLY:
            raise PermissionError("injected descriptor failure")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(lc.os, "open", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied
    assert path.read_bytes() == text.encode() and store.read_bytes() == before and not receipt.exists()


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("invalid", ["directory", "dangling", "symlink", "fifo", "readfault"])
def test_invalid_suppression_refuses_apply_validator_and_recovery(tmp_path: Path, monkeypatch, invalid: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120039.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    real_write = lc.write_suppressions
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("stop after landing")
        patch.setattr(lc, "write_suppressions", fail)
        pending = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                         source_revision=content_revision(text), receipt_path=receipt,
                                         verdicts=[verdict])
    assert pending.applied and pending.failed
    store = lc.suppression_path(root)
    foreign = tmp_path / "foreign-suppression"
    foreign.write_text("foreign data", encoding="utf-8")
    if invalid == "directory":
        store.mkdir()
    elif invalid in {"dangling", "symlink"}:
        try:
            store.symlink_to(foreign if invalid == "symlink" else tmp_path / "missing-target")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable")
    elif invalid == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation unavailable")
        os.mkfifo(store)
    else:
        real_write(root, [("existing", "revision")])
        real_open = lc.os.open
        def fail_open(name, flags, *args, **kwargs):
            if isinstance(name, (str, os.PathLike)) and Path(name) == store:
                raise PermissionError("injected read failure")
            return real_open(name, flags, *args, **kwargs)
        monkeypatch.setattr(lc.os, "open", fail_open)
    note_before, receipt_before = note.read_bytes(), receipt.read_bytes()
    other_receipt = receipt.with_name("learnings-cleanup-20261114-120040.json")
    applied = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                     source_revision=content_revision(note_before.decode()),
                                     receipt_path=other_receipt, verdicts=[])
    assert applied.failed and not applied.applied and not other_receipt.exists()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert recovered.failed
    assert note.read_bytes() == note_before and receipt.read_bytes() == receipt_before
    assert foreign.read_text(encoding="utf-8") == "foreign data"
    if invalid in {"dangling", "symlink"}:
        assert store.is_symlink()
    elif invalid == "fifo":
        assert stat.S_ISFIFO(store.lstat().st_mode)
    elif invalid == "directory":
        assert store.is_dir()


def test_update_review_lock_timeout_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120016.json"
    monkeypatch.setattr(lc, "_update_directory_durability_supported", lambda: True)
    @contextmanager
    def timeout(*args: object, **kwargs: object) -> Iterator[None]:
        raise QueueLockError("timeout")
        yield
    monkeypatch.setattr(lc, "queue_lock", timeout)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision="unused", verdicts=[], receipt_path=receipt)
    recovery = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name)
    assert result.failed and recovery.failed and not receipt.parent.exists()


@POSIX_UPDATE_RECEIPTS
def test_update_review_refuses_untrusted_receipt_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120017.json"
    receipt.parent.mkdir(parents=True, mode=0o700)
    receipt.parent.chmod(0o777)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}])
    assert result.failed and not result.applied and not receipt.exists()
    assert path.read_bytes() == text.encode()


@pytest.mark.skipif(os.name != "nt", reason="native Windows durability refusal contract")
def test_update_review_native_windows_refuses_without_mutating_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    lc.write_suppressions(root, [("existing", "revision")])
    suppression = lc.suppression_path(root)
    before_suppression = suppression.read_bytes()
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120014.json"
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert not result.applied and result.failed == ("unsupported receipt directory durability",)
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert not recovered.applied and recovered.failed == ("unsupported receipt directory durability",)
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    assert path.read_bytes() == text.encode("utf-8")
    assert suppression.read_bytes() == before_suppression
    assert not receipt.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows durability refusal contract")
def test_update_review_native_windows_fresh_tree_stays_unchanged(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120024.json"
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() if p.is_file() else None
              for p in tmp_path.rglob("*")}
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision="unused", verdicts=[], receipt_path=receipt)
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert result.failed and recovered.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    assert {p.relative_to(tmp_path).as_posix(): p.read_bytes() if p.is_file() else None
            for p in tmp_path.rglob("*")} == before


@POSIX_UPDATE_RECEIPTS
def test_update_review_defer_real_queue_reference(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.write_text(f"- [memory] Uncertain fact _(from: {source})_\n", encoding="utf-8")
    proposal_id = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md",
                                     "memory", "Uncertain fact", source, 0)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120006.json"
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "DEFER", "reason": "Unclear", "evidence": ["Needs review"],
                   "proposal_ref": {"kind": "memory", "id": proposal_id}}],
    )
    assert not result.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name)["deferred"] == 1
    queue.write_text("", encoding="utf-8")
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("settled", [False, True])
def test_public_defer_with_large_unrelated_history(tmp_path: Path, settled: bool, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.write_text("" if settled else f"- [memory] Uncertain fact _(from: {source})_\n", encoding="utf-8")
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md",
                                  "memory", "Uncertain fact", source, 0)
    log = memory_proposals.dismissed_log_path(queue)
    unrelated = json.dumps({"text": "x" * 1100, "dismissed_at": "2026-11-14T12:00:00+00:00"}) + "\n"
    with log.open("w", encoding="utf-8") as handle:
        for _ in range(8000):
            handle.write(unrelated)
        if settled:
            handle.write(json.dumps({"text": "Uncertain fact", "kind": "memory", "source": source,
                                     "proposal_id": identity, "promoted_at": "2026-11-14T12:00:00+00:00"}) + "\n")
    assert log.stat().st_size > 8 * 1024 * 1024
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Unclear", "evidence": ["Needs review"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120006.json"
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    assert not result.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None
    pending = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120007.json"
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("leave pending")
        patch.setattr(lc, "_update_commit_receipt", fail)
        assert lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=pending,
                                      verdicts=[verdict]).failed
    assert not lc.recover_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        receipt_name=pending.name).failed
    if settled:
        with log.open("a", encoding="utf-8") as handle:
            handle.write('{"broken":\n')
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_recover_update_review_repairs_pending_suppression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120007.json"
    write = lc.write_suppressions
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected crash")
    monkeypatch.setattr(lc, "write_suppressions", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.applied and result.failed
    monkeypatch.setattr(lc, "write_suppressions", write)
    write(root, [(f"old-{n}", "revision") for n in range(lc.MAX_SUPPRESSED)])
    full = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                    task_revision=3, receipt_name=receipt.name)
    assert full.failed and len(lc.read_suppressions(root)) == lc.MAX_SUPPRESSED
    lc.suppression_path(root).unlink()
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert not recovered.failed and not recovered.conflicts
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None


@POSIX_UPDATE_RECEIPTS
def test_recover_update_review_preserves_prior_and_partial_suppressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD, FOURTH_RECORD)
    _write(config, text)
    lc.write_suppressions(root, [("earlier", "revision")])
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120025.json"
    real_write = lc.write_suppressions
    def partial(root: Path, pairs: list[tuple[str, str]], *, actor: str = "system") -> object:
        real_write(root, pairs[:1], actor=actor)
        raise OSError("injected partial persistence")
    monkeypatch.setattr(lc, "write_suppressions", partial)
    verdicts = [{"learning_id": row.learning_id, "entry_revision": entry_revision(row),
                 "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
                for row in (UNLINKED_RECORD, FOURTH_RECORD)]
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), verdicts=verdicts, receipt_path=receipt)
    prior = set(lc.read_suppressions(root))
    assert result.applied and result.failed and len(prior) == 2 and ("earlier", "revision") in prior
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    monkeypatch.setattr(lc, "write_suppressions", real_write)
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert not recovered.failed
    final = set(lc.read_suppressions(root))
    assert prior < final and len(final) == 3
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None


def test_update_review_defer_decision_requires_canonical_duplicate_and_timestamp(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    queue.write_text("", encoding="utf-8")
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", "memory", "Fact", source, 0)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Question", "evidence": ["Review"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    log = memory_proposals.dismissed_log_path(queue)
    row = {"kind": "memory", "text": "Fact", "identity_text": "Fact",
           "source": source, "proposal_id": identity + ":2"}
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])
    row["dismissed_at"] = "2026-11-14T12:00:00+00:00"
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert lc._deferred_references_valid(root, WORKSPACE, [verdict])
    row["promoted_at"] = "2026-11-14T12:00:00+00:00"
    row["dismissed_at"] = ""
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert lc._deferred_references_valid(root, WORKSPACE, [verdict])
    del row["promoted_at"]
    row["dismissed_at"] = "2026-11-14T12:00:00+00:00"
    legacy = {key: value for key, value in row.items() if key != "identity_text"}
    log.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    assert lc._deferred_references_valid(root, WORKSPACE, [verdict])
    row["text"] = "Foreign fact"
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])
    row["text"] = "Fact"
    row["history_only"] = True
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("operation", ["apply", "recover", "verify"])
@pytest.mark.parametrize("promoted_at", ["", None])
def test_defer_conflicting_timestamp_uses_canonical_precedence(tmp_path: Path, monkeypatch, operation: str, promoted_at: str | None) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.write_text("", encoding="utf-8")
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", "memory", "Fact", source, 0)
    row = {"kind": "memory", "text": "Fact", "source": source, "proposal_id": identity,
           "dismissed_at": "2026-11-14T12:00:00+00:00"}
    log = memory_proposals.dismissed_log_path(queue)
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Review", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-160001.json"
    def apply():
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation != "apply":
        with monkeypatch.context() as patch:
            if operation == "recover":
                def fail(*args, **kwargs):
                    raise OSError("leave pending")
                patch.setattr(lc, "_update_commit_receipt", fail)
            result = apply()
            assert bool(result.failed) is (operation == "recover")
    row["promoted_at"] = promoted_at
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    canonical = memory_proposals.read_decisions(queue, include_history_only=False)
    assert len(canonical) == 1 and canonical[0]["action"] == "accepted" and canonical[0]["ts"] == str(promoted_at)
    before = (note.read_bytes(), log.read_bytes(), receipt.read_bytes() if receipt.exists() else None)
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])
    if operation == "apply":
        with pytest.raises(ValueError, match="DEFER"):
            apply()
    elif operation == "recover":
        assert lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name).failed
    else:
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is None
    assert (note.read_bytes(), log.read_bytes(), receipt.read_bytes() if receipt.exists() else None) == before
    with monkeypatch.context() as patch:
        patch.setattr(lc, "_deferred_references_valid", lambda *args: True)
        if operation == "apply":
            assert not apply().failed
        elif operation == "recover":
            assert not lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                                task_revision=3, receipt_name=receipt.name).failed
        else:
            assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                             task_revision=3, receipt_name=receipt.name) is not None


def test_update_review_defer_rejects_nonmemory_queue_and_history(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", "profile", "Fact", source, 0)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Question", "evidence": ["Review"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    queue.write_text(f"- [profile] Fact _(from: {source})_\n", encoding="utf-8")
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])
    queue.write_text("", encoding="utf-8")
    memory_proposals.record_dismissal(queue, text="Fact", kind="profile", source=source, proposal_id=identity)
    assert not lc._deferred_references_valid(root, WORKSPACE, [verdict])


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("text", ["Fact", "A  spaced fact"])
@pytest.mark.parametrize("tamper", ["none", "id", "source", "timestamp", "kind"])
def test_public_defer_accepts_only_provable_legacy_raw_text(tmp_path: Path, text: str, tamper: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    source_text = _document(UNLINKED_RECORD)
    note = _write(config, source_text)
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text("", encoding="utf-8")
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", "memory", text, source, 0)
    row = {"kind": "memory", "text": text, "source": source, "proposal_id": identity,
           "dismissed_at": "2026-11-14T12:00:00+00:00"}
    if tamper == "id": row["proposal_id"] = "0" * 16
    if tamper == "source": row["source"] = "foreign"
    if tamper == "timestamp": row["dismissed_at"] = ""
    if tamper == "kind": row["kind"] = "profile"
    memory_proposals.dismissed_log_path(queue).write_text(json.dumps(row) + "\n", encoding="utf-8")
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Review", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130010.json"
    if tamper == "none":
        assert not lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                          source_revision=content_revision(source_text),
                                          receipt_path=receipt, verdicts=[verdict]).failed
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is not None
    else:
        with pytest.raises(ValueError):
            lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                   source_revision=content_revision(source_text), receipt_path=receipt, verdicts=[verdict])
        assert not receipt.exists() and note.read_bytes() == source_text.encode()


@POSIX_UPDATE_RECEIPTS
def test_update_receipt_rejects_unprivate_descriptor_before_write_and_read(tmp_path: Path, monkeypatch) -> None:
    import os
    from ciao.os_support import private
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    fd = lc._update_receipt_dir(directory, create=False)
    real = private.is_private_descriptor_strict
    seen = []
    def reject_file(descriptor):
        info = os.fstat(descriptor)
        if not __import__("stat").S_ISDIR(info.st_mode):
            seen.append(info.st_size)
            return False
        return real(descriptor)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(private, "is_private_descriptor_strict", reject_file)
            with pytest.raises(OSError, match="unsafe receipt"):
                lc._update_write_new(fd, "receipt.json", {"secret": "sensitive"})
        assert seen == [0] and not (directory / "receipt.json").exists()
        lc._update_write_new(fd, "receipt.json", {"secret": "sensitive"})
    finally:
        os.close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(private, "is_private_descriptor_strict", reject_file)
        assert lc._update_receipt_read(directory / "receipt.json") is None
        assert not lc._update_fence_receipt(directory / "receipt.json", {"secret": "sensitive"})


@POSIX_UPDATE_RECEIPTS
def test_update_receipt_rejects_unprivate_directory(tmp_path: Path, monkeypatch) -> None:
    from ciao.os_support import private
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(private, "is_private_descriptor_strict", lambda fd: False)
    with pytest.raises(OSError, match="unsafe receipt directory"):
        lc._update_receipt_dir(directory, create=False)


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS extended ACL")
@POSIX_UPDATE_RECEIPTS
def test_update_receipt_rejects_real_macos_acl(tmp_path: Path) -> None:
    import os
    import subprocess
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    fd = lc._update_receipt_dir(directory, create=False)
    try:
        lc._update_write_new(fd, "receipt.json", {"secret": "sensitive"})
    finally:
        os.close(fd)
    path = directory / "receipt.json"
    subprocess.run(["chmod", "+a", "everyone allow read", str(path)], check=True)
    assert lc._update_receipt_read(path) is None
    assert not lc._update_fence_receipt(path, {"secret": "sensitive"})


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS extended ACL")
@POSIX_UPDATE_RECEIPTS
def test_update_receipt_rejects_inherited_macos_acl_before_writing(tmp_path: Path) -> None:
    import os
    import subprocess
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    fd = lc._update_receipt_dir(directory, create=False)
    try:
        subprocess.run(["chmod", "+a", "everyone allow read,file_inherit", str(directory)], check=True)
        with pytest.raises(OSError, match="unsafe receipt directory"):
            lc._update_receipt_dir(directory, create=False)
        with pytest.raises(OSError, match="unsafe receipt file"):
            lc._update_write_new(fd, "receipt.json", {"secret": "sensitive"})
        assert not (directory / "receipt.json").exists()
    finally:
        os.close(fd)


@POSIX_UPDATE_RECEIPTS
def test_real_cli_header_sweep_recovery_satisfies_public_defer(tmp_path: Path, monkeypatch, capsys) -> None:
    from ciao import cli, memory_receipts as mr
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    queue = root / "Workspace" / "Memory-Proposals.md"
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    queue.write_text(f"# Queue\n\n## 2026-11-14T12:00:00Z\n\n- [memory] A  fact _(from: {source})_\n\n",
                     encoding="utf-8")
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md",
                                  "memory", "A  fact", source, 0)
    reason = tmp_path / "reason.txt"
    reason.write_text("already reviewed", encoding="utf-8")
    monkeypatch.setattr(cli, "_resolve_workspace_and_vaults", lambda args: (tmp_path, root, tmp_path, WORKSPACE))
    with monkeypatch.context() as patch:
        def interrupted(*args, **kwargs):
            raise OSError("interrupted before decision")
        patch.setattr(proposal_actions, "record_decision", interrupted)
        assert cli.main(["memory-proposal-dismiss", "--workspace", str(tmp_path), "--vault-root", str(root),
                         "--reason-file", str(reason), "A  fact"]) == 1
        warning = capsys.readouterr().err.lower()
        assert "removed" in warning and "decision" in warning and "inspect" in warning
    journal = mr.journal_path(root, None)
    mr.recover_pending(journal=journal, config=config)
    mr.recover_pending(journal=journal, config=config)
    decisions = memory_proposals.read_decisions(queue)
    assert len(decisions) == 1 and decisions[0]["proposal_id"] == identity
    assert decisions[0]["via"] == "agent" and decisions[0]["reason"] == "already reviewed"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Review", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-130011.json"
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    assert not result.failed and lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                                            task_revision=3, receipt_name=receipt.name) is not None


def test_update_review_defer_keeps_exact_history_identity_text(tmp_path: Path) -> None:
    root = Path(_config(tmp_path).workspace_vault_root(WORKSPACE))
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    identity = stable_proposal_id(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md",
                                  "memory", "A  spaced fact", source, 0)
    proposal_actions.record_decision(queue, action="dismiss", text="A  spaced fact", kind="memory",
                                     source=source, via="pwa", proposal_id=identity)
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Unclear", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": identity}}
    assert lc._deferred_references_valid(root, WORKSPACE, [verdict])


@POSIX_UPDATE_RECEIPTS
def test_wrong_source_recovery_cannot_satisfy_public_defer(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    from ciao.proposal_tracking import walk_proposal_queue

    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    learning = _document(UNLINKED_RECORD)
    _write(config, learning)
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    first = f"- [memory] Uncertain fact _(from: {source})_\n"
    second = "- [memory] Uncertain fact _(from: another source)_\n"
    queue.write_text(first + second, encoding="utf-8")
    relative = f"{WORKSPACE}/Workspace/Memory-Proposals.md"
    entry = next(walk_proposal_queue(WORKSPACE, relative, first + second))
    selected = {"line": entry.line, "id": entry.proposal_id, "kind": entry.bullet.kind,
                "text": entry.bullet.text, "source": entry.bullet.source}
    def interrupted(*args, **kwargs):
        raise OSError("interrupted")
    with monkeypatch.context() as patch:
        patch.setattr(mr, "_write_queue_receipt", interrupted)
        with pytest.raises(OSError):
            with mr.queue_resolution(queue, removed_text=entry.bullet.text, kind=entry.bullet.kind,
                                     promoted=False, actor="operator", source="pwa",
                                     workspace=WORKSPACE, vault_root=root, selected=selected):
                queue.write_text(first, encoding="utf-8")
                raise OSError("interrupted")
    mr.recover_pending(journal=mr.journal_path(root, None), config=config)
    queue.write_text("", encoding="utf-8")
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Uncertain", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": entry.proposal_id}}
    with pytest.raises(ValueError):
        lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                               source_revision=content_revision(learning), verdicts=[verdict],
                               receipt_path=config.state_path.parent / "migration" / "learnings-cleanup-20261114-120032.json")


@POSIX_UPDATE_RECEIPTS
def test_recovered_spaced_identity_satisfies_public_defer_once(tmp_path: Path, monkeypatch) -> None:
    from ciao import memory_receipts as mr
    from ciao.proposal_tracking import walk_proposal_queue
    from ciao.web import proposal_service

    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    learning = _document(UNLINKED_RECORD)
    _write(config, learning)
    queue = root / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    before = f"- [memory] A  spaced fact _(from: {source})_\n"
    queue.write_text(before, encoding="utf-8")
    entry = next(walk_proposal_queue(WORKSPACE, f"{WORKSPACE}/Workspace/Memory-Proposals.md", before))
    assert proposal_service._rewrite_queue_single(
        queue, entry.line, entry.raw, entry.bullet.text, entry.bullet.kind,
        False, WORKSPACE, root, entry.proposal_id,
    )
    journal = mr.journal_path(root, None)
    mr.recover_pending(journal=journal, config=config)
    mr.recover_pending(journal=journal, config=config)
    decisions = memory_proposals.read_decisions(queue, include_history_only=False)
    assert len(decisions) == 1 and decisions[0]["identity_text"] == "A  spaced fact"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Uncertain", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": entry.proposal_id}}
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(learning), verdicts=[verdict],
                                    receipt_path=config.state_path.parent / "migration" / "learnings-cleanup-20261114-120033.json")
    assert not result.failed


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("tamper", ["source", "proposal_id", "dismissed_at", "history_only", "kind"])
def test_update_review_real_duplicate_decision_and_tampering(tmp_path: Path, tamper: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    proposal = memory_proposals.MemoryProposal(target="memory", text="Uncertain fact", source_section=source)
    queue = memory_proposals.append_proposals([proposal], root)
    assert queue is not None
    # append_proposals deduplicates, so reproduce a pre-existing duplicate with the
    # queue owner's atomic writer before recording the non-first decision.
    write_queue_atomically(queue, queue.read_text(encoding="utf-8") + proposal.as_bullet() + "\n")
    from ciao.proposal_tracking import walk_proposal_queue
    relative = f"{WORKSPACE}/Workspace/Memory-Proposals.md"
    entries = list(walk_proposal_queue(WORKSPACE, relative, queue.read_text(encoding="utf-8")))
    assert len(entries) == 2 and entries[1].proposal_id == entries[0].proposal_id + ":1"
    proposal_actions.record_decision(queue, action="dismiss", text=proposal.text, kind="memory",
                                     via="pwa", source=source, proposal_id=entries[1].proposal_id)
    recorded = memory_proposals.read_decisions(queue, include_history_only=False)
    assert len(recorded) == 1 and recorded[0]["proposal_id"] == entries[1].proposal_id
    assert recorded[0]["source"] == source and recorded[0]["ts"]
    write_queue_atomically(queue, "")
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120026.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "DEFER", "reason": "Uncertain", "evidence": ["Reviewed"],
               "proposal_ref": {"kind": "memory", "id": entries[0].proposal_id}}
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                    source_revision=content_revision(text), verdicts=[verdict], receipt_path=receipt)
    assert not result.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None
    log = memory_proposals.dismissed_log_path(queue)
    row = json.loads(log.read_text(encoding="utf-8"))
    row[tamper] = {"source": "foreign", "proposal_id": "a" * 16, "dismissed_at": "",
                   "history_only": True, "kind": "profile"}[tamper]
    log.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    with pytest.raises(ValueError):
        lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                               source_revision=content_revision(text), verdicts=[verdict],
                               receipt_path=receipt.parent / "learnings-cleanup-20261114-120027.json")


@POSIX_UPDATE_RECEIPTS
def test_update_review_defer_accepts_recovered_real_duplicate_resolution(tmp_path: Path) -> None:
    from ciao import memory_receipts
    from ciao.proposal_tracking import walk_proposal_queue
    from ciao.web import proposal_service
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    source = f"learning-review:{UNLINKED_RECORD.learning_id}@{entry_revision(UNLINKED_RECORD)}"
    proposal = memory_proposals.MemoryProposal(target="memory", text="Uncertain fact", source_section=source)
    queue = memory_proposals.append_proposals([proposal], root)
    assert queue is not None
    write_queue_atomically(queue, queue.read_text(encoding="utf-8") + proposal.as_bullet() + "\n")
    relative = f"{WORKSPACE}/Workspace/Memory-Proposals.md"
    entries = list(walk_proposal_queue(WORKSPACE, relative, queue.read_text(encoding="utf-8")))
    second = entries[1]
    assert proposal_service._rewrite_queue_single(queue, second.line, second.raw, second.bullet.text,
        second.bullet.kind, False, WORKSPACE, root, second.proposal_id)
    memory_receipts.recover_pending(journal=memory_receipts.journal_path(root, None), config=config)
    decisions = memory_proposals.read_decisions(queue, include_history_only=False)
    assert len(decisions) == 1 and decisions[0]["proposal_id"] == second.proposal_id
    assert decisions[0]["source"] == source and decisions[0]["ts"]
    first = next(walk_proposal_queue(WORKSPACE, relative, queue.read_text(encoding="utf-8")))
    assert proposal_service._rewrite_queue_single(queue, first.line, first.raw, first.bullet.text,
        first.bullet.kind, False, WORKSPACE, root, first.proposal_id)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120030.json"
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "DEFER", "reason": "Uncertain", "evidence": ["Reviewed"],
                   "proposal_ref": {"kind": "memory", "id": entries[0].proposal_id}}])
    assert not result.failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None


@POSIX_UPDATE_RECEIPTS
def test_update_review_late_classification_refusal_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120018.json"
    plan = lc.plan_cleanup
    calls = 0
    def changed(*args: object, **kwargs: object) -> lc.CleanupPlan:
        nonlocal calls
        calls += 1
        result = plan(*args, **kwargs)
        if calls == 2:
            return replace(result, kept=(replace(result.kept[0], reason="pending"),))
        return result
    monkeypatch.setattr(lc, "plan_cleanup", changed)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}])
    assert result.failed and not result.applied and path.read_bytes() == text.encode()


@POSIX_UPDATE_RECEIPTS
def test_recover_update_review_noop_revalidates_finding_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120008.json"
    commit = lc._update_commit_receipt
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected commit failure")
    monkeypatch.setattr(lc, "_update_commit_receipt", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied
    monkeypatch.setattr(lc, "_update_commit_receipt", commit)
    queue = sp.queue_dir(config, WORKSPACE)
    queue.mkdir(parents=True)
    failed = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                      task_revision=3, receipt_name=receipt.name)
    assert failed.failed
    queue.rmdir()
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert not recovered.failed


@POSIX_UPDATE_RECEIPTS
def test_update_review_substituted_receipt_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120009.json"
    commit = lc._update_commit_receipt
    def substitute(dir_fd: int, name: str, identity: tuple[int, int], data: dict[str, object]) -> None:
        receipt.unlink()
        receipt.write_text("foreign", encoding="utf-8")
        commit(dir_fd, name, identity, data)
    monkeypatch.setattr(lc, "_update_commit_receipt", substitute)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}],
    )
    assert result.failed and receipt.read_text() == "foreign"


@POSIX_UPDATE_RECEIPTS
def test_update_review_rollback_conflict_preserves_external_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120010.json"
    snapshot = lc._finding_dependency_snapshot
    calls = 0
    def changed(config: CiaoConfig, workspace: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 3:
            path.write_text(path.read_text(encoding="utf-8") + "external edit\n", encoding="utf-8")
            return "changed"
        return snapshot(config, workspace)
    monkeypatch.setattr(lc, "_finding_dependency_snapshot", changed)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "RETIRE",
        "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.applied and result.failed and path.read_text().endswith("external edit\n")
    assert json.loads(receipt.read_text())["automatic_review"]["committed"] is False
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_update_review_prepared_receipt_cannot_recover_foreign_afterimage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120019.json"
    write = lc._write_locked
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted before CAS")
    monkeypatch.setattr(lc, "_write_locked", fail)
    result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}])
    assert result.failed and not result.applied
    monkeypatch.setattr(lc, "_write_locked", write)
    row = lc.plan_cleanup(root, config=config, workspace=WORKSPACE).rows[0]
    foreign_after, _ = lc._splice(text, [row])
    path.write_text(foreign_after, encoding="utf-8")
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert recovered.failed and not lc.suppression_path(root).exists()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_update_review_postreplace_sync_failure_requires_successful_verifier_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120020.json"
    sync, replace_file = lc.os.fsync, lc.os.replace
    committed_replaced = False
    replace_count = 0
    def replace_observer(*args: object, **kwargs: object) -> None:
        nonlocal committed_replaced, replace_count
        replace_file(*args, **kwargs)
        replace_count += 1
        if replace_count == 2:
            committed_replaced = True
    def fail_directory(fd: int) -> None:
        if committed_replaced and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected postreplace directory fsync failure")
        sync(fd)
    monkeypatch.setattr(lc.os, "replace", replace_observer)
    monkeypatch.setattr(lc.os, "fsync", fail_directory)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "KEEP",
        "reason": "Current", "evidence": ["Reviewed"]}],
    )
    assert result.failed and committed_replaced
    before_receipt, before_note = receipt.read_bytes(), path.read_bytes()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None
    assert receipt.read_bytes() == before_receipt and path.read_bytes() == before_note
    monkeypatch.setattr(lc.os, "fsync", sync)
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None
@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("failure", ["file", "directory"])
def test_update_review_prewrite_sync_failure_cleans_owned_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120021.json"
    receipt.parent.mkdir(parents=True, mode=0o700)
    sync = lc.os.fsync
    def fail(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        if (failure == "file" and stat.S_ISREG(mode)) or (failure == "directory" and stat.S_ISDIR(mode)):
            raise OSError("injected fsync failure")
        sync(fd)
    monkeypatch.setattr(lc.os, "fsync", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "RETIRE",
        "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied and not receipt.exists()
    assert path.read_bytes() == text.encode("utf-8")


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("which", ["final", "temp"])
def test_receipt_failure_cleanup_preserves_substituted_inode(tmp_path: Path, monkeypatch, which: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120042.json"
    receipt.parent.mkdir(parents=True, mode=0o700)
    temp = receipt.parent / f".{receipt.name}.fixed.tmp"
    target = receipt if which == "final" else temp
    monkeypatch.setattr(lc.secrets, "token_hex", lambda _: "fixed")
    original_fsync = lc.os.fsync
    injected = False

    def fail(fd):
        nonlocal injected
        info = os.fstat(fd)
        if not injected and stat.S_ISREG(info.st_mode) and target.exists():
            named = target.stat()
            if (named.st_dev, named.st_ino) == (info.st_dev, info.st_ino):
                injected = True
                target.unlink()
                target.write_text("foreign inode", encoding="utf-8")
                raise OSError("injected fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(lc.os, "fsync", fail)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}],
    )
    assert injected and result.failed and not result.applied
    assert target.read_text(encoding="utf-8") == "foreign inode"
    assert note.read_bytes() == text.encode()


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("operation", ["apply", "validator", "recover"])
@pytest.mark.parametrize("guard", ["owner", "mode", "parent"])
def test_receipt_trust_guards_each_entrypoint(tmp_path: Path, monkeypatch, operation: str, guard: str) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120043.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    kwargs = dict(config=config, workspace=WORKSPACE, task_revision=3,
                  source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation == "validator":
        assert not lc.apply_update_review(root, **kwargs).failed
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is not None
    elif operation == "recover":
        with monkeypatch.context() as patch:
            def fail_commit(*args, **kwargs):
                raise OSError("leave pending")
            patch.setattr(lc, "_update_commit_receipt", fail_commit)
            assert lc.apply_update_review(root, **kwargs).failed
    else:
        receipt.parent.mkdir(parents=True, mode=0o700)
    note_before = note.read_bytes()
    receipt_before = receipt.read_bytes() if receipt.exists() else None
    if guard == "mode":
        receipt.parent.chmod(0o755)
    elif guard == "parent":
        receipt.parent.parent.chmod(0o777)
    else:
        original_fstat = lc.os.fstat
        target = receipt.parent.stat()
        class ForeignOwner:
            def __init__(self, info):
                self.info = info
                self.st_uid = os.getuid() + 1
            def __getattr__(self, name):
                return getattr(self.info, name)
        def foreign(fd):
            info = original_fstat(fd)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                return ForeignOwner(info)
            return info
        monkeypatch.setattr(lc.os, "fstat", foreign)
    if operation == "apply":
        assert lc.apply_update_review(root, **kwargs).failed
    elif operation == "validator":
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is None
    else:
        assert lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name).failed
    assert note.read_bytes() == note_before
    assert (receipt.read_bytes() if receipt.exists() else None) == receipt_before


@POSIX_UPDATE_RECEIPTS
def test_update_review_receipt_is_synced_before_source_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120022.json"
    receipt.parent.parent.mkdir(parents=True, mode=0o700)
    assert not receipt.parent.exists()
    sync, write = lc.os.fsync, lc._write_locked
    synced: list[tuple[int, int]] = []
    def observe_sync(fd: int) -> None:
        info = os.fstat(fd)
        sync(fd)
        synced.append((info.st_dev, info.st_ino))
    def observe_write(path: Path, proposed: str, *, expect: str) -> None:
        data = json.loads(receipt.read_bytes())
        assert data["automatic_review"]["phase"] == "prepared"
        assert data["automatic_review"]["committed"] is False
        assert data["removals"] and data["revision_before"] == content_revision(text)
        file_info, dir_info = receipt.stat(), receipt.parent.stat()
        ancestor_info = receipt.parent.parent.stat()
        assert (file_info.st_dev, file_info.st_ino) in synced
        assert (dir_info.st_dev, dir_info.st_ino) in synced
        assert (ancestor_info.st_dev, ancestor_info.st_ino) in synced
        assert synced.index((ancestor_info.st_dev, ancestor_info.st_ino)) < synced.index((file_info.st_dev, file_info.st_ino))
        assert synced.index((file_info.st_dev, file_info.st_ino)) < synced.index((dir_info.st_dev, dir_info.st_ino))
        write(path, proposed, expect=expect)
    monkeypatch.setattr(lc.os, "fsync", observe_sync)
    monkeypatch.setattr(lc, "_write_locked", observe_write)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "RETIRE",
        "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.applied and not result.failed


@POSIX_UPDATE_RECEIPTS
def test_update_review_real_finding_appearing_at_cas_restores_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120023.json"
    write = lc._write_locked
    calls = 0
    def transition(path: Path, proposed: str, *, expect: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            _file_settled(config, _origin(UNLINKED_RECORD, state=sp.ORIGIN_PENDING, verification=""))
        write(path, proposed, expect=expect)
    monkeypatch.setattr(lc, "_write_locked", transition)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "RETIRE",
        "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied and calls == 2
    assert path.read_bytes() == text.encode()
    assert not lc.suppression_path(root).exists()
    assert json.loads(receipt.read_text())["automatic_review"]["phase"] == "prepared"
    parsed = lc.parse_learnings(text, workspace=WORKSPACE)
    entry = next(e for e in parsed.entries if e.record and e.record.learning_id == UNLINKED_RECORD.learning_id)
    row = lc.CleanupRow(learning_id=UNLINKED_RECORD.learning_id, key=UNLINKED_RECORD.key,
                        line=entry.source_text, entry_revision=entry_revision(UNLINKED_RECORD),
                        action=lc.REMOVE, reason="never_proposed", start=entry.start,
                        end=entry.end, source_text=entry.source_text)
    foreign_after, _ = lc._splice(text, [row])
    path.write_bytes(foreign_after.encode())
    recovered = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name)
    assert recovered.failed and path.read_bytes() == foreign_after.encode()
    assert not lc.suppression_path(root).exists()
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is None


@POSIX_UPDATE_RECEIPTS
def test_rollback_phase_guard_with_restored_finding_fingerprint(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    note = _write(config, text)
    _file_settled(config, _origin(FOURTH_RECORD))
    finding = sp.proposal_path(config, WORKSPACE, "web-research")
    finding_before, inode = finding.read_bytes(), finding.stat().st_ino
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120041.json"
    original_write = lc._write_locked
    calls = 0

    def transition(path, proposed, *, expect):
        nonlocal calls
        calls += 1
        original_write(path, proposed, expect=expect)
        if calls == 1:
            with finding.open("ab") as handle:
                handle.write(b"\nexternal change\n")

    monkeypatch.setattr(lc, "_write_locked", transition)
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3,
        source_revision=content_revision(text), receipt_path=receipt,
        verdicts=[{"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
                   "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}],
    )
    assert result.failed and not result.applied and calls == 2
    assert note.read_bytes() == text.encode()
    with finding.open("wb") as handle:
        handle.write(finding_before)
    assert finding.stat().st_ino == inode
    data = json.loads(receipt.read_text(encoding="utf-8"))
    assert data["automatic_review"]["phase"] == "prepared"
    assert lc._finding_dependency_snapshot(config, WORKSPACE) == data["automatic_review"]["finding_revision"]
    row = lc.plan_cleanup(root, config=config, workspace=WORKSPACE).rows[0]
    after, _ = lc._splice(text, [row])
    note.write_bytes(after.encode())
    result = lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                      task_revision=3, receipt_name=receipt.name)
    assert result.failed and note.read_bytes() == after.encode()
    assert not lc.suppression_path(root).exists()


@POSIX_UPDATE_RECEIPTS
def test_update_review_does_not_overwrite_preexisting_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120011.json"
    receipt.parent.mkdir(parents=True, mode=0o700)
    temp = receipt.parent / f".{receipt.name}.fixed.tmp"
    temp.write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(lc.secrets, "token_hex", lambda _: "fixed")
    result = lc.apply_update_review(
        root, config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
        receipt_path=receipt, verdicts=[{"learning_id": UNLINKED_RECORD.learning_id,
        "entry_revision": entry_revision(UNLINKED_RECORD), "verdict": "KEEP",
        "reason": "Current", "evidence": ["Reviewed"]}],
    )
    assert result.failed and temp.read_text() == "foreign"
    assert json.loads(receipt.read_text())["automatic_review"]["committed"] is False


@POSIX_UPDATE_RECEIPTS
def test_update_review_competing_same_name_preserves_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-120012.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "KEEP", "reason": "Current", "evidence": ["Reviewed"]}
    kwargs = dict(config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
                  receipt_path=receipt, verdicts=[verdict])
    write = lc._update_write_new
    winner: list[lc.CleanupResult] = []
    def competing(dir_fd: int, name: str, data: dict[str, object]) -> tuple[int, int]:
        if not winner and name == receipt.name:
            monkeypatch.setattr(lc, "_update_write_new", write)
            winner.append(lc.apply_update_review(root, **kwargs))
        return write(dir_fd, name, data)
    monkeypatch.setattr(lc, "_update_write_new", competing)
    loser = lc.apply_update_review(root, **kwargs)
    assert len(winner) == 1 and not winner[0].failed
    assert loser.failed and not loser.applied
    assert path.read_bytes() == text.encode("utf-8")
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None
    assert json.loads(receipt.read_text())["automatic_review"]["committed"] is True


@POSIX_UPDATE_RECEIPTS
def test_update_review_refuses_receipt_directory_link_and_traversal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    text = _document(UNLINKED_RECORD)
    path = _write(config, text)
    runtime = config.state_path.parent
    runtime.mkdir(parents=True)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    link = runtime / "migration"
    try:
        link.symlink_to(foreign, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Native link creation may require privileges; traversal is still checked.
        pass
    name = "learnings-cleanup-20261114-120013.json"
    verdict = {"learning_id": UNLINKED_RECORD.learning_id, "entry_revision": entry_revision(UNLINKED_RECORD),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    kwargs = dict(config=config, workspace=WORKSPACE, task_revision=3, source_revision=content_revision(text),
                  verdicts=[verdict])
    with pytest.raises(ValueError):
        lc.apply_update_review(root, receipt_path=runtime / "other" / ".." / "migration" / name, **kwargs)
    if link.is_symlink():
        result = lc.apply_update_review(root, receipt_path=link / name, **kwargs)
        assert result.failed and not result.applied
        assert not (foreign / name).exists()
    assert path.read_bytes() == text.encode("utf-8")
FORMAT_NOTES = (
    "## Format\n\n"
    "An example, which is not an entry and must survive byte for byte:\n\n"
    "```\n"
    "- [not-an-entry] this is inside a fence\n"
    "```\n"
)


def _document(*records: LearningRecord, frontmatter: str = FRONTMATTER) -> str:
    """A canonical document: the given Active entries, notes, and a Promoted one."""
    body = "".join(f"{render_learning(record)}\n" for record in records)
    return (
        f"{frontmatter}# Learnings\n\n"
        "## Active\n\n"
        f"{body}\n"
        f"{FORMAT_NOTES}\n"
        "## Promoted / Resolved\n\n"
        f"{render_learning(PROMOTED_RECORD)}\n"
    )


def _write(config: CiaoConfig, text: str) -> Path:
    path = Path(config.workspace_vault_root(WORKSPACE)).joinpath("Workspace/Learnings.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _origin(
    learning: LearningRecord,
    *,
    finding: str = "add the defuddle fallback",
    state: str = sp.ORIGIN_APPLIED,
    verification: str = RECEIPT,
    revision: str | None = None,
) -> sp.SkillOrigin:
    return sp.SkillOrigin(
        workspace=WORKSPACE,
        learning_id=learning.learning_id,
        source_revision=entry_revision(learning) if revision is None else revision,
        finding=finding,
        summary="Add the defuddle fallback step.",
        state=state,
        verification=verification,
    )


def _file_settled(config: CiaoConfig, *origins: sp.SkillOrigin, skill: str = "web-research") -> None:
    """One settled proposal linking the given findings, written to the queue.

    Written rather than merged, because ``upsert_proposal`` files every origin it
    is handed as ``pending`` — a finding is a question and only a settlement
    writes a state — so this puts the record on disk in the shape a settled one
    reaches it in and leaves every assertion here about the fold.
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
                chat_id="sess-a1",
                archive="2026-08-09T10:00:00Z",
                turn="",
                excerpt="outcome=needs_review corrections=1 errors=0 turns=3",
            ),
        ),
        lifecycle=sp.PENDING,
        chat_id="",
        updated_at="2026-08-09T10:00:00Z",
        origins=origins,
    )
    write_queue_atomically(
        sp.proposal_path(config, WORKSPACE, skill), sp.render_proposal(proposal)
    )


def _plan(config: CiaoConfig, vault: Path, **kwargs: object) -> lc.CleanupPlan:
    return lc.plan_cleanup(vault, workspace=WORKSPACE, config=config, **kwargs)


def _settle_every(config: CiaoConfig, *records: LearningRecord) -> None:
    """One settled proposal linking a finding to each of *records*.

    One proposal rather than one each, so the three removals below are decided by
    a single fold in a single queue — which is the shape a real workspace has, and
    the one that makes a single apply do all three splices.
    """
    _file_settled(
        config,
        *(_origin(record, finding=f"finding for {record.key}") for record in records),
    )


# ── The happy path, and the losslessness ───────────────────────────────────


def test_an_eligible_entry_is_removed_and_every_other_byte_survives(
    tmp_path: Path,
) -> None:
    """The whole contract in one assertion: the eligible line goes, and the file
    around it is the same file."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)
    assert [row.key for row in plan.removals] == ["blocked-pages"]

    result = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    after = path.read_text(encoding="utf-8")
    assert result.applied is True
    assert result.removed_count == 1
    assert result.revision_before == result.receipt["revision_before"]
    assert result.revision_after == result.receipt["revision_after"]
    assert result.receipt["entries_removed"] == 1
    # Only the entry's own line and the blank line pairing with it are gone.
    assert render_learning(RETIRED_RECORD) not in after
    assert render_learning(PENDING_RECORD) in after
    assert render_learning(UNLINKED_RECORD) in after
    assert render_learning(PROMOTED_RECORD) in after
    # The format notes, the fenced example inside them, the headings, and the
    # frontmatter — except `updated:`, which the write restated.
    assert FORMAT_NOTES in after
    assert "## Promoted / Resolved\n" in after
    assert after.startswith("---\ntags: [ciao, learnings]\nupdated: 2026-09-30\n---\n")
    # And nothing else moved: the file is exactly the old one with that one line
    # and that one date changed.
    assert after == text.replace(f"{render_learning(RETIRED_RECORD)}\n", "").replace(
        "updated: 2020-01-01", "updated: 2026-09-30"
    )


def test_a_document_with_no_frontmatter_is_left_without_one(
    tmp_path: Path,
) -> None:
    """Frontmatter is optional in this model, so a document that has none must
    not acquire some. Inventing bookkeeping is the same class of move as
    repairing an unreadable line."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, frontmatter=""))
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    expected = (
        _document(frontmatter="")
        .replace(f"{render_learning(RETIRED_RECORD)}\n", "")
        .replace("updated: 2020-01-01\n", "")
    )
    assert result.applied is True
    assert path.read_text(encoding="utf-8") == expected
    assert not expected.startswith("---")


def test_a_missing_updated_key_is_added_rather_than_refused(tmp_path: Path) -> None:
    """The file did change, so the document has to stop claiming it did not."""
    config = _config(tmp_path)
    path = _write(
        config,
        _document(RETIRED_RECORD, frontmatter="---\ntags: [ciao, learnings]\n---\n"),
    )
    _file_settled(config, _origin(RETIRED_RECORD))

    lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    after = path.read_text(encoding="utf-8")
    assert after.startswith("---\ntags: [ciao, learnings]\nupdated: 2026-09-30\n---\n")


@pytest.mark.parametrize("bom", ["", "﻿"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_bom_and_crlf_survive_the_whole_round_trip(
    tmp_path: Path, bom: str, newline: str
) -> None:
    """Both properties are file properties, and neither is content.

    The read is from bytes for this reason: a CRLF document read through
    ``read_text`` comes back LF and is written out that way, so the claim would
    hold of the preview and be false of the file. The terminator each removal
    takes with it is the document's own, for the same reason.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, frontmatter=FRONTMATTER)
    path = _write(config, "")
    path.write_bytes((bom + text).replace("\n", newline).encode("utf-8"))
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        path.parent.parent,
        _plan(config, path.parent.parent),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    raw = path.read_bytes()
    expected = (bom + text).replace("\n", newline)
    expected = expected.replace(
        f"{render_learning(RETIRED_RECORD)}\r\n" if newline == "\r\n" else f"{render_learning(RETIRED_RECORD)}\n",
        "",
    ).replace("updated: 2020-01-01", "updated: 2026-09-30")
    assert result.applied is True
    assert raw == expected.encode("utf-8")
    assert b"\r\n" in raw if newline == "\r\n" else b"\r\n" not in raw


# ── More than one removal, which is a different claim ────────────────────────


#: The three shapes a multi-removal run can leave behind, over five Active
#: entries. What they differ in is the *gaps*: how many there are, and how many
#: entries share one.
#:
#: * ``separate`` — none of the three touches its neighbour, so three gaps, and
#:   two of the three recorded offsets are shifted left by the bytes the entries
#:   below them took out. This is the case a single-removal test cannot see.
#: * ``shared-pair`` — the first two were side by side, so they left one gap
#:   between them and one of their own.
#: * ``one-run`` — all three were side by side, so the three removals left a
#:   single gap, and an undo that filled it three times would put the second
#:   entry inside the first one's bytes.
_REMOVAL_SHAPES: tuple[tuple[tuple[LearningRecord, ...], int], ...] = (
    ((RETIRED_RECORD, UNLINKED_RECORD, FIFTH_RECORD), 3),
    ((RETIRED_RECORD, PENDING_RECORD, FOURTH_RECORD), 2),
    ((RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD), 1),
)

_FIVE_RECORDS = (
    RETIRED_RECORD,
    PENDING_RECORD,
    UNLINKED_RECORD,
    FOURTH_RECORD,
    FIFTH_RECORD,
)


@pytest.mark.parametrize("bom", ["", "﻿"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("removed", "gaps"),
    _REMOVAL_SHAPES,
    ids=["separate-gaps", "shared-pair", "one-run"],
)
def test_three_removals_revert_to_the_exact_bytes(
    tmp_path: Path,
    bom: str,
    newline: str,
    removed: tuple[LearningRecord, ...],
    gaps: int,
) -> None:
    """Reversible means reversible, and a run of three is a different claim than a
    run of one.

    A receipt's offset is the position of the gap **in the text the run left**, and
    for the second and later removals in a run that is not the text it read: an
    entry further up the document also removed takes its bytes with it, so the gap
    ends up further left than the span's own start. Recording the start anyway put
    the bytes back in the wrong places — silently, because the anchors were
    sampled from the same wrong offsets and so the undo's own check agreed with
    itself. Every existing revert test removed exactly one entry, which is why
    this was not visible.

    The assertion is on the bytes, not on the arithmetic, and the anchors are
    checked against the file on disk *before* the undo runs so that a receipt
    which could not possibly match is caught as such instead of being repaired by
    an undo that happens to be lenient.
    """
    config = _config(tmp_path)
    text = _document(*_FIVE_RECORDS)
    path = _write(config, "")
    path.write_bytes((bom + text).replace("\n", newline).encode("utf-8"))
    _settle_every(config, *removed)
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert [row.key for row in result.removed] == [row.key for row in removed]
    receipt = result.receipt
    assert receipt is not None
    assert receipt["entries_removed"] == 3
    # The gaps are what the undo has, so they are checked against the file rather
    # than against the receipt's own bookkeeping.
    after = path.read_bytes().decode("utf-8")
    for span in receipt["removals"]:
        offset = int(span["offset"])
        assert after[offset : offset + len(span["after"])] == span["after"]
        assert after[max(0, offset - len(span["before"])) : offset] == span["before"]
    offsets = [int(span["offset"]) for span in receipt["removals"]]
    assert offsets == sorted(offsets)
    # Two entries removed side by side really do leave one gap between them, so
    # the receipt says so rather than inventing a second one.
    assert len(set(offsets)) == gaps

    undo = lc.unmigrate_cleanup(vault, receipt, apply=True, today=TODAY)

    assert undo["failed"] == []
    assert undo["entries_reverted"] == 3
    expected = (bom + text).replace("\n", newline)
    expected = expected.replace("updated: 2020-01-01", "updated: 2026-09-30")
    assert path.read_bytes() == expected.encode("utf-8")
    # And a second undo is refused rather than doubling the lines back.
    assert lc.unmigrate_cleanup(vault, receipt, apply=True)["failed"]


def test_the_anchors_are_sampled_from_the_document_that_lands(
    tmp_path: Path,
) -> None:
    """The anchors are sampled from what is written, not from the spliced text.

    The ``updated:`` restamp runs between the splice and the write, and it can
    change the document's length: ``updated: 2020-1-1`` becomes a full ISO date.
    An entry near the frontmatter — within :data:`~ciao.learnings_cleanup.ANCHOR_CHARS`
    of it — then has its whole gap moved by those extra bytes, and an anchor
    sampled from the pre-restamp text describes a document that never existed.
    The undo would refuse a perfectly good receipt, which is the safe direction
    and still a lie about what is on disk.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD, frontmatter=_SHORT_STAMP)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    receipt = result.receipt
    assert receipt is not None
    assert len(text) != len(text.replace("updated: 2020-1-1", "updated: 2026-09-30"))
    after = path.read_text(encoding="utf-8")
    span = receipt["removals"][0]
    offset = int(span["offset"])
    assert after[offset : offset + len(span["after"])] == span["after"]
    assert after[max(0, offset - len(span["before"])) : offset] == span["before"]
    assert lc.unmigrate_cleanup(vault, receipt, apply=True, today=TODAY)["failed"] == []


# ── Idempotence, suppression and the undo ────────────────────────────────────


def test_a_second_run_removes_nothing(tmp_path: Path) -> None:
    """The ordinary nightly case: the entry is gone, so there is nothing to do."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    first = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert first.applied is True

    second_plan = _plan(config, vault)
    second = lc.apply_cleanup(
        vault, second_plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert second_plan.removals == ()
    assert second.applied is False
    assert second.receipt is None
    # The file the first run left is exactly the file the second run finds, and
    # neither wrote a second byte of it.
    assert second.revision_before == first.revision_after
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_a_settlement_that_lands_after_a_failed_run_is_picked_up_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the settlement and the removal leaves retryable work.

    The two are separate records on purpose. The settlement is the queue's, written
    by whoever verified the lesson into the skill; the removal is the vault's, and
    a run that dies after the first and before the second has left the learning
    exactly where it was with its finding now answered. Nothing is "in progress",
    no cursor has to be resumed and nothing has to be reconciled from an image —
    the next plan simply reads a fold that now says yes, which is what makes the
    window between them recoverable rather than a hole.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    # The finding is open, so there is nothing to do.
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification="")
    )
    assert _plan(config, vault).removals == ()

    # The settlement lands, and then the run that would act on it dies before it
    # writes anything.
    _file_settled(config, _origin(RETIRED_RECORD))

    def _refuse(path: Path, text: str, *, expect: str = "") -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    failed = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert failed.applied is False
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")
    # Nothing was recorded as removed, so the retry is not suppressed.
    assert lc.read_suppressions(vault) == {}

    # The retry: the same plan, now against a working write.
    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_the_undo_restores_the_exact_bytes_and_the_next_pass_keeps_them(
    tmp_path: Path,
) -> None:
    """The two halves of "reversible but not fragile".

    The bytes come back from the receipt rather than being re-derived, and the
    suppression is deliberately *not* lifted by the undo — otherwise the very next
    nightly pass would remove the line the operator just put back, and ``--revert``
    would be a button that does nothing.
    """
    config = _config(tmp_path)
    original = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, original)
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert undo["failed"] == []
    # Exact, modulo the one field a write is allowed to move.
    assert path.read_text(encoding="utf-8") == original.replace(
        "updated: 2020-01-01", "updated: 2026-09-30"
    )
    after_undo = _plan(config, vault)
    assert after_undo.removals == ()
    assert [(row.key, row.reason) for row in after_undo.kept if row.key == "blocked-pages"] == [
        ("blocked-pages", "suppressed")
    ]
    # And a second undo is refused rather than doubling the line back.
    assert lc.unmigrate_cleanup(vault, result.receipt, apply=True)["failed"]


def test_the_undo_re_records_a_store_that_never_learned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undo protects the restored line even when the store is behind the file.

    The apply writes the document first and the store second, because a pair
    recorded for an entry still on disk suppresses an entry that is still there.
    So the store can be left knowing nothing about a removal the document has
    already made — and then the undo puts the line back, and the next nightly pass
    re-derives the same removals from the same fold and takes it out again. The
    receipt names every pair, so the undo can put them back itself: re-recording
    is a merge, not an overwrite, so the pairs a healthy apply already stored are
    left exactly as they are.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    def _refuse(vault_root: Path, added: list[tuple[str, str]], **_: object) -> None:
        raise OSError("the store could not be written")

    monkeypatch.setattr(lc, "write_suppressions", _refuse)
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    assert result.suppressed == ()
    assert lc.read_suppressions(vault) == {}

    monkeypatch.undo()
    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert undo["failed"] == []
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")
    # The store now holds the pair the apply could not write, and the plan that
    # would otherwise offer the line straight back keeps it.
    assert set(lc.read_suppressions(vault)) == {
        (RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))
    }
    assert _plan(config, vault).removals == ()
    assert undo["suppressions_kept"] == [
        {
            "learning_id": RETIRED_RECORD.learning_id,
            "entry_revision": entry_revision(RETIRED_RECORD),
        }
    ]


def test_an_undo_that_cannot_hold_the_restored_line_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored file and a store that does not know about it is a half-finished
    undo, and the next pass would quietly undo it again — so it is reported as the
    failure it is rather than as a clean revert."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    def _refuse(vault_root: Path, added: list[tuple[str, str]], **_: object) -> None:
        raise lc._SuppressionUnreadable("the store is nonsense")

    monkeypatch.setattr(lc, "write_suppressions", _refuse)
    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)

    assert undo["entries_reverted"] == 1
    assert [item["path"] for item in undo["failed"]] == [lc.SUPPRESSION_RELATIVE]
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")


def test_a_re_reviewed_entry_is_eligible_again(tmp_path: Path) -> None:
    """A changed line is a new fact, not the one that was retired.

    The suppression is keyed on ``(learning_id, entry_revision)``, so the whole
    point of hashing the entry rather than the file is here: editing this entry
    makes it eligible while everything else in the document is irrelevant.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True

    # The owner re-words the lesson and the finding is re-filed against the new
    # line — which is exactly what a person does after reading a retired lesson
    # they disagree with.
    reworded = replace(RETIRED_RECORD, text=f"{RETIRED} Sometimes.")
    _write(config, _document(reworded, PENDING_RECORD))
    _file_settled(config, _origin(reworded))

    plan = _plan(config, vault)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    # The neighbour is still listed as unproposed: the cap on what a pass decides
    # is per entry, and one entry becoming eligible says nothing about the other.
    assert [row.key for row in plan.kept] == ["long-transcripts"]


def test_reapproval_lifts_the_suppression_without_any_other_change(
    tmp_path: Path,
) -> None:
    """The attended path: somebody says "remove that one again"."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    lc.unmigrate_cleanup(vault, result.receipt, apply=True, today=TODAY)
    assert _plan(config, vault).removals == ()

    assert lc.clear_suppression(vault, RETIRED_RECORD.learning_id, entry_revision(RETIRED_RECORD))

    assert [row.key for row in _plan(config, vault).removals] == ["blocked-pages"]


def test_an_unreadable_suppression_store_stops_the_whole_plan(tmp_path: Path) -> None:
    """Unreadable is not empty.

    Every pair in that store is an entry this module already removed once, so
    treating a store it cannot read as "nothing was removed" is how the same line
    is retired twice — and the second retirement has no receipt, because the first
    one recorded the bytes.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    store = lc.suppression_path(vault)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{ not json", encoding="utf-8", newline="")

    plan = _plan(config, vault)

    assert plan.blocked
    assert "cannot be read" in plan.blocked
    assert plan.removals == ()
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is False
    assert result.skipped == plan.blocked
    assert render_learning(RETIRED_RECORD) in path.read_text(encoding="utf-8")


# ── Every way the answer could wrongly be "remove" ──────────────────────────


def test_a_pending_finding_keeps_the_entry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_PENDING, verification="")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert [(row.key, row.reason) for row in plan.kept] == [
        ("blocked-pages", "pending"),
        ("long-transcripts", "never_proposed"),
    ]


def test_a_chat_working_on_the_finding_keeps_the_entry(tmp_path: Path) -> None:
    """``implementing`` is a chat having started, not the lesson being in the
    skill, so it is still an open finding."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD, state=sp.ORIGIN_IMPLEMENTING, verification="")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "pending"
    assert "implementing" in plan.kept[0].detail


def test_an_unconfirmed_already_covered_keeps_the_entry(tmp_path: Path) -> None:
    """"The skill already says this" is a claim only somebody reading the target
    can confirm, and it is the state most likely to be recorded by a well-meaning
    pass."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config,
        _origin(RETIRED_RECORD, state=sp.ORIGIN_ALREADY_COVERED, verification=""),
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "unanswered"


def test_an_entry_nothing_has_ever_proposed_is_kept_and_counted_as_unmatched(
    tmp_path: Path,
) -> None:
    """The legacy case, and the one an unattended pass must never act on.

    It is also the row a reviewer most needs to see, which is why the plan lists
    it rather than omitting it: "it isn't in the list" is not an answer a person
    can act on.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, UNLINKED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [row.reason for row in plan.kept] == ["never_proposed"]
    assert plan.counts["unmatched"] == 1


def test_an_unattributable_finding_on_a_linked_proposal_keeps_the_entry(
    tmp_path: Path,
) -> None:
    """An origin naming no learning could be this entry's other half, so it is
    counted and the entry is kept rather than the one readable origin being
    believed on its own."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(
        config, _origin(RETIRED_RECORD), sp.SkillOrigin(finding="something unreadable")
    )

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "held_back"


def test_a_reworded_entry_is_kept(tmp_path: Path) -> None:
    """The entry moved after the finding was filed, so the finding was written
    against bytes that are no longer there."""
    config = _config(tmp_path)
    reworded = replace(RETIRED_RECORD, text=f"{RETIRED} Sometimes.")
    _write(config, _document(reworded, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, _document(reworded, PENDING_RECORD) and Path(
        _write(config, _document(reworded, PENDING_RECORD)).parent.parent
    ))

    assert plan.removals == ()
    assert plan.kept[0].reason == "changed_since"


def test_an_origin_with_no_recorded_revision_is_kept(tmp_path: Path) -> None:
    """"Cannot show it is unchanged" is the same answer as "changed" for a
    question about deleting somebody's notes."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD, revision=""))

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "unrecorded"


def test_a_whole_file_revision_filed_before_the_move_is_kept(tmp_path: Path) -> None:
    """The migration story, and the reason no backfill exists.

    Every install that filed links before #728-E holds whole-file hashes. They
    match no entry, so every such learning reads as changed-since and is kept.
    Rewriting them to match the present file would assert that the file was
    unchanged since filing, and nothing established that.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    whole = lc.content_revision(path.read_text(encoding="utf-8"))
    _file_settled(config, _origin(RETIRED_RECORD, revision=whole))

    plan = _plan(config, path.parent.parent)

    assert whole != entry_revision(RETIRED_RECORD)
    assert plan.removals == ()
    assert plan.kept[0].reason == "changed_since"


def test_a_promoted_entry_is_never_a_candidate(tmp_path: Path) -> None:
    """A decision somebody already made, re-decided every night, is how a promoted
    learning gets retired on work nobody re-checked."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    # Even with a settled finding filed against it, the Promoted entry is not a
    # row at all: the section is not reconciled, so it cannot be a candidate.
    _file_settled(config, _origin(PROMOTED_RECORD), skill="shipped")

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert all(row.key != "shipped" for row in plan.rows)
    assert plan.active == 1


def test_an_unreadable_line_is_reported_and_never_repaired(tmp_path: Path) -> None:
    """A line this code cannot read is still the owner's line.

    It is reported by name so an owner who cannot see why their entry is being
    ignored does not assume the tool lost it — and it is never a candidate, never
    re-rendered, never dropped.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD).replace(
        f"{render_learning(RETIRED_RECORD)}\n",
        f"{render_learning(RETIRED_RECORD)}\n- [broken] [2024-13-45 → nope] (x0) Bad dates.\n",
    )
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [(row.reason, row.action) for row in plan.conflicts] == [
        ("unreadable", lc.CONFLICT)
    ]
    assert "neither a real date nor 'unknown'" in plan.conflicts[0].detail
    assert "not a positive count" in plan.conflicts[0].detail
    result = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert result.applied is True
    assert "- [broken] [2024-13-45 → nope] (x0) Bad dates.\n" in path.read_text(
        encoding="utf-8"
    )


# ── The upstream side ───────────────────────────────────────────────────────


def _draft(
    config: CiaoConfig,
    learning: LearningRecord,
    *,
    lifecycle: str,
    skill: str = "stock-skill",
) -> None:
    """One upstream ``[review]`` draft linking a learning, in the given state."""
    draft = upstream_drafts.UpstreamDraft(
        id=upstream_drafts.draft_id(
            WORKSPACE, upstream_drafts.UPSTREAM_ISSUE, skill, "add the fallback"
        ),
        workspace=WORKSPACE,
        target=upstream_drafts.UPSTREAM_ISSUE,
        skill=skill,
        title="Add the fallback",
        body="What was done, what went wrong, and the instruction that fixes it.",
        lifecycle=lifecycle,
        repository="owner/repo",
        change="add the fallback",
        issue_url="https://example.invalid/issue/1" if lifecycle == "filed" else "",
        updated_at="2026-08-09T10:00:00Z",
        origins=(
            {
                "learning_id": learning.learning_id,
                "finding": "add the defuddle fallback",
                "source_revision": entry_revision(learning),
                "summary": "Add the defuddle fallback step.",
            },
        ),
    )
    path = upstream_drafts.sidecar_path(config, WORKSPACE, draft.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_queue_atomically(path, upstream_drafts.render_sidecar(draft))


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("lifecycle", [upstream_drafts.DRAFT_PENDING, upstream_drafts.DRAFT_FILED])
@pytest.mark.parametrize("operation", ["apply", "recover", "verify"])
def test_automatic_alias_upstream_protects_retirement(tmp_path: Path, monkeypatch, lifecycle: str, operation: str) -> None:
    from dataclasses import replace
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    record = replace(UNLINKED_RECORD, aliases=("old-learning-id",))
    text = _document(record)
    note = _write(config, text)
    _draft(config, record, lifecycle=lifecycle)
    sidecar = next(upstream_drafts.sidecar_dir(config, WORKSPACE).glob("*.json"))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["origins"][0]["learning_id"] = record.aliases[0]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-150001.json"
    verdict = {"learning_id": record.learning_id, "entry_revision": entry_revision(record),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    def apply():
        return lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                      source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    if operation != "apply":
        with monkeypatch.context() as patch:
            patch.setattr(lc, "_upstream_origins", lambda *args, **kwargs: {})
            if operation == "recover":
                real = lc._update_commit_receipt
                count = 0
                def fail_second(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == 2:
                        raise OSError("leave pending")
                    return real(*args, **kwargs)
                patch.setattr(lc, "_update_commit_receipt", fail_second)
            prior = apply()
            assert prior.applied and bool(prior.failed) is (operation == "recover")
    before = (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None,
              lc.suppression_path(root).read_bytes() if lc.suppression_path(root).exists() else None)
    if operation == "apply":
        with pytest.raises(ValueError, match="protected"):
            apply()
    elif operation == "recover":
        assert lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                        task_revision=3, receipt_name=receipt.name).failed
    else:
        assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                         task_revision=3, receipt_name=receipt.name) is None
    assert (note.read_bytes(), receipt.read_bytes() if receipt.exists() else None,
            lc.suppression_path(root).read_bytes() if lc.suppression_path(root).exists() else None) == before


def test_upstream_alias_rejection_and_staleness(tmp_path: Path) -> None:
    from dataclasses import replace
    config = _config(tmp_path)
    record = replace(UNLINKED_RECORD, aliases=("old-learning-id",))
    _write(config, _document(record))
    _draft(config, record, lifecycle=upstream_drafts.DRAFT_REJECTED)
    sidecar = next(upstream_drafts.sidecar_dir(config, WORKSPACE).glob("*.json"))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["origins"][0]["learning_id"] = record.aliases[0]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    plan = lc.plan_cleanup(Path(config.workspace_vault_root(WORKSPACE)), workspace=WORKSPACE, config=config)
    assert len(plan.removals) == 1 and plan.removals[0].reason == "settled"
    data["origins"][0]["source_revision"] = "stale"
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    plan = lc.plan_cleanup(Path(config.workspace_vault_root(WORKSPACE)), workspace=WORKSPACE, config=config)
    assert not plan.removals and plan.kept[0].reason == "changed_since"


@POSIX_UPDATE_RECEIPTS
@pytest.mark.parametrize("recover", [False, True])
def test_automatic_rejected_alias_can_retire_and_certify(tmp_path: Path, monkeypatch, recover: bool) -> None:
    from dataclasses import replace
    config = _config(tmp_path)
    root = Path(config.workspace_vault_root(WORKSPACE))
    record = replace(UNLINKED_RECORD, aliases=("old-learning-id",))
    text = _document(record)
    note = _write(config, text)
    _draft(config, record, lifecycle=upstream_drafts.DRAFT_REJECTED)
    sidecar = next(upstream_drafts.sidecar_dir(config, WORKSPACE).glob("*.json"))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["origins"][0]["learning_id"] = record.aliases[0]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    receipt = config.state_path.parent / "migration" / "learnings-cleanup-20261114-150002.json"
    verdict = {"learning_id": record.learning_id, "entry_revision": entry_revision(record),
               "verdict": "RETIRE", "reason": "Obsolete", "evidence": ["Reviewed"]}
    with monkeypatch.context() as patch:
        if recover:
            real = lc._update_commit_receipt
            calls = 0
            def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("leave pending")
                return real(*args, **kwargs)
            patch.setattr(lc, "_update_commit_receipt", fail_second)
        result = lc.apply_update_review(root, config=config, workspace=WORKSPACE, task_revision=3,
                                        source_revision=content_revision(text), receipt_path=receipt, verdicts=[verdict])
    assert result.applied and bool(result.failed) is recover
    if recover:
        assert not lc.recover_update_review(root, config=config, workspace=WORKSPACE,
                                            task_revision=3, receipt_name=receipt.name).failed
    assert lc.update_review_recorded(root, config=config, workspace=WORKSPACE,
                                     task_revision=3, receipt_name=receipt.name) is not None
    assert record.key not in note.read_text(encoding="utf-8")


def test_a_lesson_whose_only_destination_is_a_filed_issue_stays(
    tmp_path: Path,
) -> None:
    """An issue existing is somebody being told, not the lesson landing anywhere
    this workspace can see.

    This is the stock-filed case the whole routing half of #728-D exists for: a
    lesson that applies to a packaged skill is a real finding, and the only honest
    record of it is the draft. Retiring the lesson because the issue was filed
    would delete the local copy of a lesson whose destination is not this vault.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_FILED)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    kept = plan.kept[0]
    assert kept.reason == "stock_filed_upstream"
    assert kept.destination == "stock-skill"
    assert "https://example.invalid/issue/1" in kept.evidence
    assert plan.counts["unmatched"] == 1


def test_a_lesson_waiting_on_an_upstream_decision_stays(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_PENDING)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "stock_waiting_upstream"


def test_a_rejected_upstream_draft_does_settle_the_lesson(tmp_path: Path) -> None:
    """A person rejecting the finding is the upstream equivalent of a dismissal,
    and it is the only way a stock-routed lesson is ever retired.

    A *rejection* is a decision; a filing is a notification. Nothing else about a
    stock lesson can ever reach a state where removing it is right, which is why
    this is the single lifecycle in
    :data:`ciao.learnings_cleanup.CLEARED_DRAFTS`.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_REJECTED)

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]


def test_a_locally_settled_lesson_with_a_draft_also_stays(tmp_path: Path) -> None:
    """Both destinations have to be answered. A learning routed to an owned skill
    *and* filed upstream is not done until the upstream side is too."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))
    _draft(config, RETIRED_RECORD, lifecycle=upstream_drafts.DRAFT_FILED)

    plan = _plan(config, path.parent.parent)

    assert plan.removals == ()
    assert plan.kept[0].reason == "stock_filed_upstream"


# ── The revision checks ─────────────────────────────────────────────────────


def test_a_document_that_moved_between_the_plan_and_the_apply_is_a_conflict(
    tmp_path: Path,
) -> None:
    """A whole-document check, because a cleanup splices into a whole document.

    A concurrent ``[learnings]`` accept appending a lesson below the eligible entry
    leaves that entry's own bytes identical, so the per-entry compare is silent —
    and splicing on the plan's offsets would drop the new lesson along with the
    old one. This is the check that stops that, and it writes nothing.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)
    _write(config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD))
    before = path.read_text(encoding="utf-8")

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is False
    assert result.conflicts == (lc.REVISION_MOVED,)
    assert result.receipt is None
    assert path.read_text(encoding="utf-8") == before
    assert render_learning(UNLINKED_RECORD) in before


def test_a_candidate_whose_settlement_moved_is_dropped_from_that_apply(
    tmp_path: Path,
) -> None:
    """Re-checked against the bytes under the lock, not trusted from the plan.

    The revision check above cannot see this one: the file is byte-identical, and
    what changed is the *queue* — somebody reopened the finding between the plan
    and the apply. The candidate is dropped and named, and the rest of the apply
    goes ahead, because one reopened finding is not a reason to leave the others.
    """
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    _file_settled(
        config, _origin(PENDING_RECORD), skill="long-transcripts"
    )
    plan = _plan(config, vault)
    assert [row.key for row in plan.removals] == ["blocked-pages", "long-transcripts"]

    # Only the first is reopened, after the plan was computed.
    reopened = sp.SkillOrigin(
        workspace=WORKSPACE,
        learning_id=PENDING_RECORD.learning_id,
        source_revision=entry_revision(PENDING_RECORD),
        finding="add the defuddle fallback",
        state=sp.ORIGIN_PENDING,
    )
    _file_settled(config, reopened, skill="long-transcripts")

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is True
    assert [row.key for row in result.removed] == ["blocked-pages"]
    assert any("long-transcripts" in note for note in result.conflicts)
    after = path.read_text(encoding="utf-8")
    assert render_learning(PENDING_RECORD) in after
    assert render_learning(RETIRED_RECORD) not in after


def test_a_failed_write_removes_nothing_and_records_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document is written last, after the receipt is prepared, so a failure
    at any point above leaves the file exactly as it was."""

    def _refuse(path: Path, text: str, *, expect: str = "") -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.failed
    assert result.receipt is None
    assert path.read_text(encoding="utf-8") == text
    # And nothing was remembered as removed, so the next run tries again.
    assert lc.read_suppressions(vault) == {}


def test_a_receipt_that_cannot_be_written_removes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse map is what makes a removal reversible, so it is written *first*.

    Prepared-then-persisted-then-written is three different things, and only the
    middle one is the contract: a receipt serialized in memory and handed back
    after the document is gone cannot stop anything, it can only be reported
    missing afterwards — which is exactly what happened, and the CLI's own way of
    describing it was "the removals landed but their receipt could not be
    written". So the write of the receipt is the last thing that can fail, and it
    fails closed: not one byte of the document moves.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent

    def _refuse(target: Path, receipt: dict[str, object]) -> Path:
        raise OSError("no space left on device")

    monkeypatch.setattr(lc, "write_receipt", _refuse)
    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-1.json",
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.receipt is None
    assert result.receipt_path == ""
    assert result.failed
    assert "no space left on device" in result.failed[0]
    assert path.read_bytes() == text.encode("utf-8")
    # And nothing was remembered as removed, so the next run tries again.
    assert lc.read_suppressions(vault) == {}


def test_a_receipt_that_landed_before_a_write_that_did_not_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one window the new order opens, and what a run that dies in it leaves.

    The receipt is durable and the document is untouched, which looks like a
    removal that half-happened and is in fact inert: the entries are still on
    disk, so every anchor in that receipt describes a document that was never
    written and ``--revert`` refuses it rather than filling gaps in a file that
    still holds the entries. The retry needs no reconciliation at all, because the
    document's revision is still the one the plan was computed from — which is the
    whole reason the plan is a plan and not a cursor.

    A process that *dies* is what leaves that receipt behind, so that is what this
    drives: a ``BaseException`` out of the write, which is the one failure the run
    cannot catch, tidy up after, or report. The recoverable failures below take
    their receipt back instead, which is why this test raises something no
    ``except`` in the module will see.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    receipt_file = tmp_path / "receipts" / "learnings-cleanup-1.json"

    def _die(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise KeyboardInterrupt("the process died between the two writes")

    monkeypatch.setattr(lc, "_write_locked", _die)
    with pytest.raises(KeyboardInterrupt):
        lc.apply_cleanup(
            vault,
            _plan(config, vault),
            workspace=WORKSPACE,
            config=config,
            today=TODAY,
            receipt_path=receipt_file,
        )

    assert path.read_bytes() == text.encode("utf-8")
    orphan = lc.read_receipt(receipt_file)
    assert orphan is not None
    # The tell: the receipt's *before* image is still the file. Nothing was
    # removed, and the next plan is computed against the same revision.
    assert orphan["revision_before"] == content_revision(text)
    assert orphan["revision_after"] != orphan["revision_before"]
    assert lc.read_suppressions(vault) == {}

    stale = lc.unmigrate_cleanup(vault, orphan, apply=True, today=TODAY)
    assert stale["entries_reverted"] == 0
    assert stale["failed"]
    assert path.read_bytes() == text.encode("utf-8")

    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-2.json",
    )

    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


def test_a_write_that_fails_takes_its_receipt_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run still alive when the document write fails does not leave its receipt.

    The run knows the removals did not happen, so the record of them is not true
    and is taken back rather than left for the next reader to interpret. What that
    protects is concrete: the update task's completion check asks whether a receipt
    certifies a review of the document as it stands, and this receipt would name
    exactly the revision the file still has while describing entries it never took
    out. The retry is an ordinary apply, so nothing is lost by removing the file.
    """
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(RETIRED_RECORD))
    vault = path.parent.parent
    receipt_file = tmp_path / "receipts" / "learnings-cleanup-1.json"

    def _refuse(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(lc, "_write_locked", _refuse)
    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=receipt_file,
    )

    assert result.applied is False
    assert result.failed
    assert result.receipt_path == ""
    assert path.read_bytes() == text.encode("utf-8")
    assert lc.read_receipt(receipt_file) is None
    assert lc.read_suppressions(vault) == {}

    # And the same on the other recoverable failure, the document moving under the
    # run between the revision check and the write.
    def _moved(path_arg: Path, body: str, *, expect: str = "") -> None:
        raise lc._RevisionMoved("the document moved under the run")

    monkeypatch.setattr(lc, "_write_locked", _moved)
    second = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-2.json",
    )

    assert second.applied is False
    assert second.conflicts
    assert lc.read_receipt(tmp_path / "receipts" / "learnings-cleanup-2.json") is None

    monkeypatch.undo()
    retried = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        receipt_path=tmp_path / "receipts" / "learnings-cleanup-3.json",
    )
    assert retried.applied is True
    assert [row.key for row in retried.removed] == ["blocked-pages"]


def test_the_store_is_read_and_written_under_its_own_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is a read-modify-write, so it needs the lock every other queue
    writer here takes.

    Unlocked, two concurrent runs each read the store, each add their own pairs,
    and the second write discards the first run's — so the entries that run
    removed are not recorded as removed and the next pass removes them a second
    time. The check is that the lock is actually *taken*, since a lock that is
    documented and not held is the bug it was added to fix.
    """
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    held: list[Path] = []
    real = lc.queue_lock

    @contextmanager
    def _spy(path: Path) -> Iterator[None]:
        held.append(Path(path))
        with real(path):
            yield

    monkeypatch.setattr(lc, "queue_lock", _spy)
    lc.write_suppressions(vault, [("a" * 8, "b" * 64)], actor="operator")
    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is True

    assert held == [lc.suppression_path(vault)] * 2


def test_an_undo_of_a_changed_file_is_refused_entirely(tmp_path: Path) -> None:
    """The context either side of the gap is what an undo checks, because a
    removal leaves nothing at the offset to check. One hand edit near it and the
    whole undo is refused: a half-restored document is worse than an unrestored
    one, because nothing in it can be trusted afterwards."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    # The hand edit is right beside the gap, which is what the anchor covers: an
    # undo has to refuse when somebody has typed into the file this module wrote.
    body = path.read_text(encoding="utf-8")
    path.write_text(
        body.replace(render_learning(PENDING_RECORD), "# a hand edit"), encoding="utf-8", newline=""
    )

    undo = lc.unmigrate_cleanup(vault, result.receipt, apply=True)

    assert undo["entries_reverted"] == 0
    assert undo["failed"][0]["error"] == "the file changed since the cleanup"
    assert "# a hand edit" in path.read_text(encoding="utf-8")
    assert render_learning(RETIRED_RECORD) not in path.read_text(encoding="utf-8")


# ── The budget ──────────────────────────────────────────────────────────────


def test_the_cap_holds_the_backlog_back_and_says_so(tmp_path: Path) -> None:
    """A pass that may drain the whole document takes the whole night's budget on
    pass seven of nine, and the required weekly keys are never reached — so the
    marker cannot advance and the backlog is still there tomorrow. The entries
    behind the cap are reported rather than silently dropped, because a plan that
    quietly omitted them would read as a complete answer."""
    config = _config(tmp_path)
    records = [
        LearningRecord(
            learning_id=allocate_learning_id(WORKSPACE, f"- lesson {index}"),
            key=f"lesson-{index}",
            text=f"Lesson {index} is settled.",
        )
        for index in range(5)
    ]
    path = _write(config, _document(*records))
    for record in records:
        _file_settled(config, _origin(record, finding=f"finding for {record.key}"),
                      skill=record.key)

    plan = _plan(config, path.parent.parent, max_removals=2)

    assert [row.key for row in plan.removals] == ["lesson-0", "lesson-1"]
    assert plan.over_cap is True
    assert [row.reason for row in plan.kept] == ["pending"] * 3
    assert "capped at 2" in plan.kept[0].detail


def test_a_capped_row_is_taken_by_the_next_run(tmp_path: Path) -> None:
    """The cap defers, it does not decide. Nothing is recorded as removed for a row
    the cap skipped, so the next plan still offers it."""
    config = _config(tmp_path)
    records = [
        LearningRecord(
            learning_id=allocate_learning_id(WORKSPACE, f"- lesson {index}"),
            key=f"lesson-{index}",
            text=f"Lesson {index} is settled.",
        )
        for index in range(3)
    ]
    path = _write(config, _document(*records))
    vault = path.parent.parent
    for record in records:
        _file_settled(
            config, _origin(record, finding=f"finding for {record.key}"), skill=record.key
        )

    first = lc.apply_cleanup(
        vault,
        _plan(config, vault, max_removals=1),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    second = lc.apply_cleanup(
        vault,
        _plan(config, vault, max_removals=1),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )

    assert [row.key for row in first.removed] == ["lesson-0"]
    assert [row.key for row in second.removed] == ["lesson-1"]


# ── The reviewed no-op ──────────────────────────────────────────────────────


def test_a_review_that_removes_nothing_is_still_recorded(tmp_path: Path) -> None:
    """The difference between "a person looked and agreed" and "nobody looked".

    A table is not a review and an approval nobody gave is not a review, and both
    of those leave no receipt — so this is the only shape in which a cleanup run
    produces a receipt without a removal, and it exists because the update-task
    completion check needs it.
    """
    config = _config(tmp_path)
    path = _write(config, _document(PENDING_RECORD))
    vault = path.parent.parent
    approvals = {
        "abc": {"learning_id": "abc", "reason": "read the whole table", "evidence": "none"}
    }

    result = lc.apply_cleanup(
        vault,
        _plan(config, vault),
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
        approvals=approvals,
    )

    assert result.applied is False
    assert result.removed_count == 0
    assert result.receipt is not None
    assert result.receipt["entries_removed"] == 0
    assert result.receipt["approvals"] == approvals
    assert result.receipt["revision_before"] == result.revision_before


def test_a_dry_run_records_no_receipt_at_all(tmp_path: Path) -> None:
    """Without approvals there is nothing to attest to, and a receipt for a run
    that changed nothing would be a claim nobody made."""
    config = _config(tmp_path)
    path = _write(config, _document(PENDING_RECORD))
    vault = path.parent.parent

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )

    assert result.receipt is None
    assert lc.read_suppressions(vault) == {}


# ── The receipt and the store ───────────────────────────────────────────────


def test_the_receipt_is_serializable_and_names_both_revisions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    result = lc.apply_cleanup(
        vault, _plan(config, vault), workspace=WORKSPACE, config=config, today=TODAY
    )
    receipt = result.receipt

    assert json.loads(json.dumps(receipt)) == receipt
    assert receipt["schema_version"] == lc.RECEIPT_VERSION
    assert receipt["removed_by"] == "system"
    assert receipt["revision_before"] != receipt["revision_after"]
    span = receipt["removals"][0]
    assert span["from"] == f"{render_learning(RETIRED_RECORD)}\n"
    assert span["learning_id"] == RETIRED_RECORD.learning_id
    assert span["entry_revision"] == entry_revision(RETIRED_RECORD)
    # The offset addresses the gap in the text the run *left*, which is the only
    # text an undo will ever be holding.
    after = path.read_text(encoding="utf-8")
    assert after[span["offset"] : span["offset"] + len(span["after"])] == span["after"]


def test_the_suppression_store_is_bounded_and_keeps_the_newest(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    pairs = [
        (f"learning-{index}", f"{index:064d}") for index in range(lc.MAX_SUPPRESSED + 10)
    ]

    stored = lc.write_suppressions(vault, pairs, actor="operator")

    assert len(stored) == lc.MAX_SUPPRESSED
    # Trimmed from the front, so the removals an undo could plausibly hit survive.
    assert (f"learning-{len(pairs) - 1}", f"{len(pairs) - 1:064d}") in stored
    assert ("learning-0", f"{0:064d}") not in stored
    assert json.loads(lc.suppression_path(vault).read_text(encoding="utf-8"))["schema_version"] == lc.SUPPRESSION_SCHEMA


def test_the_store_round_trips_through_disk(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))

    lc.write_suppressions(vault, [("a" * 8, "b" * 64)], actor="operator")
    assert lc.read_suppressions(vault) == {("a" * 8, "b" * 64): lc.read_suppressions(vault)[("a" * 8, "b" * 64)]}
    assert lc.read_suppressions(vault)[("a" * 8, "b" * 64)]["removed_by"] == "operator"

    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is True
    assert lc.read_suppressions(vault) == {}
    assert lc.clear_suppression(vault, "a" * 8, "b" * 64) is False


def test_a_receipt_from_another_schema_is_not_honoured(tmp_path: Path) -> None:
    """Reversing from a reverse map this code does not understand would restore
    spans against a file it has not checked."""
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"schema_version": 99, "removals": []}), encoding="utf-8", newline="")
    assert lc.read_receipt(path) is None
    path.write_text("not json", encoding="utf-8", newline="")
    assert lc.read_receipt(path) is None
    assert lc.read_receipt(tmp_path / "missing.json") is None


def test_a_receipt_path_never_overwrites_an_existing_one(tmp_path: Path) -> None:
    """Two runs in the same second must not share a reverse map."""
    first = lc.new_receipt_path(tmp_path)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("{}", encoding="utf-8", newline="")

    second = lc.new_receipt_path(tmp_path)

    assert first != second
    assert second.name.startswith(lc.RECEIPT_PREFIX)


# ── The frontmatter helper, on its own ──────────────────────────────────────


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("---\nupdated: 2020-01-01\n---\n", "---\nupdated: 2026-09-30\n---\n"),
        ("---\n---\n", "---\nupdated: 2026-09-30\n---\n"),
        ("---\r\nupdated: 2020-01-01\r\n---\r\n", "---\r\nupdated: 2026-09-30\r\n---\r\n"),
        ("---\n...\n", "---\nupdated: 2026-09-30\n...\n"),
        ("# no frontmatter\n", "# no frontmatter\n"),
        ("---\nupdated: yesterday\n---\n", "---\nupdated: 2026-09-30\n---\n"),
    ],
)
def test_the_frontmatter_is_restamped_and_nothing_else_moves(
    before: str, after: str
) -> None:
    assert lc._restamp(before, today=TODAY) == after


def test_a_bom_does_not_hide_the_frontmatter(tmp_path: Path) -> None:
    """A document that starts with a byte-order mark does still have a
    frontmatter block, and treating the mark as part of the opening fence would
    put the whole file inside it."""
    assert lc._restamp("﻿---\nupdated: 2020-01-01\n---\n", today=TODAY) == (
        "﻿---\nupdated: 2026-09-30\n---\n"
    )


def test_an_unterminated_frontmatter_is_left_alone() -> None:
    """An unterminated block swallows the file, and there is no closing fence to
    add a key before. Inventing one would put metadata inside prose."""
    assert lc._restamp("---\nupdated: 2020-01-01\n", today=TODAY) == (
        "---\nupdated: 2020-01-01\n"
    )


# ── The plan never writes ───────────────────────────────────────────────────


def test_planning_writes_nothing_at_all(tmp_path: Path) -> None:
    """A pure plan: no file, no store, no receipt, on any code path."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD, PENDING_RECORD)
    path = _write(config, text)
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))

    _plan(config, vault)
    _plan(config, vault, max_removals=0)

    assert path.read_text(encoding="utf-8") == text
    assert not lc.suppression_path(vault).exists()
    assert not (vault.parent.parent / ".runtime" / "migration").exists()


def test_a_missing_document_is_a_no_op_not_a_crash(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vault = Path(config.workspace_vault_root(WORKSPACE))
    vault.mkdir(parents=True, exist_ok=True)

    plan = lc.plan_cleanup(vault, workspace=WORKSPACE, config=config)

    assert plan.blocked
    assert "does not exist" in plan.blocked
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is False
    assert result.skipped == plan.blocked


def test_two_entries_with_the_same_statement_stay_two_entries(tmp_path: Path) -> None:
    """The cleanup keys by ``learning_id``, and a document may legitimately hold
    two lines that say the same thing.

    Only the settled one is removed, and the other keeps its bytes — which is the
    case a ``record.key``-keyed reconciliation would get wrong by removing both
    or neither."""
    config = _config(tmp_path)
    first = _record(RETIRED, "blocked-pages", f"- {RETIRED} first")
    second = _record(RETIRED, "blocked-pages", f"- {RETIRED} second")
    text = _document(first, second, PENDING_RECORD)
    path = _write(config, text)
    _file_settled(config, _origin(first))

    plan = _plan(config, path.parent.parent)

    assert [row.learning_id for row in plan.removals] == [first.learning_id]
    after = lc.apply_cleanup(
        path.parent.parent,
        plan,
        workspace=WORKSPACE,
        config=config,
        today=TODAY,
    )
    assert after.applied is True
    body = path.read_text(encoding="utf-8")
    assert render_learning(first) not in body
    assert render_learning(second) in body


def test_a_conflicting_identifier_is_reported_and_never_removed(tmp_path: Path) -> None:
    """Two lines claiming one identity cannot both be right, and picking a winner
    would let one learning's evidence overwrite another's. The parser reports the
    second as a conflict; the cleanup carries that through as a conflict rather
    than resolving it."""
    config = _config(tmp_path)
    duplicate = replace(RETIRED_RECORD, text=f"{RETIRED} A different entry entirely.")
    path = _write(config, _document(RETIRED_RECORD, duplicate, PENDING_RECORD))
    _file_settled(config, _origin(RETIRED_RECORD))

    plan = _plan(config, path.parent.parent)

    assert [row.key for row in plan.removals] == ["blocked-pages"]
    assert [row.reason for row in plan.conflicts] == ["unreadable"]
    assert "already used by an earlier entry" in plan.conflicts[0].detail
    result = lc.apply_cleanup(
        path.parent.parent, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True
    assert render_learning(duplicate) in path.read_text(encoding="utf-8")


def test_the_kept_reasons_are_a_closed_vocabulary(tmp_path: Path) -> None:
    """A caller has to be able to group the table, and a reason that is not in the
    set cannot be grouped. The plan asserts its own answers rather than trusting
    the classifier."""
    config = _config(tmp_path)
    reworded = _record(f"{RETIRED} Sometimes.", "blocked-pages", f"- {RETIRED} v2")
    path = _write(
        config, _document(RETIRED_RECORD, PENDING_RECORD, UNLINKED_RECORD, reworded)
    )
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    _file_settled(
        config, _origin(PENDING_RECORD, state=sp.ORIGIN_INTERRUPTED, verification=""),
        skill="long-transcripts",
    )
    # Filed against a revision that is not this entry's own line: a finding a
    # pass wrote for the lesson before somebody reworded it.
    _file_settled(
        config,
        _origin(reworded, finding="stale", revision=entry_revision(RETIRED_RECORD)),
        skill="reworded",
    )

    plan = _plan(config, vault)

    assert plan.removals
    assert {row.reason for row in plan.kept} <= lc.KEPT_REASONS
    assert {row.reason for row in plan.kept} == {
        "pending",
        "never_proposed",
        "changed_since",
    }
    assert all(row.action in {lc.KEEP, lc.CONFLICT, lc.REMOVE} for row in plan.rows)
    assert plan.counts["active"] == len(plan.rows)


def test_an_entry_whose_record_cannot_be_rendered_is_a_conflict(
    tmp_path: Path,
) -> None:
    """A record with no renderable canonical line has no revision, so it cannot be
    the thing a finding was filed against. The parser cannot produce one, so this
    drives the branch directly rather than pretending a document can."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)
    broken = LearningRecord(learning_id="not-a-uuid", key="broken", text="Broken.")

    with pytest.raises(ValueError, match="not a UUID"):
        lc.entry_revision(broken)
    # Which is why `plan_cleanup` reports such a record as a conflict rather than
    # letting the exception out of a planning pass.
    assert broken.learning_id
    # And a plan carrying it is still applied for its real removals.
    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )
    assert result.applied is True


def test_the_as_dict_shapes_are_json_safe(tmp_path: Path) -> None:
    """Both summaries are the contract the CLI's ``--json`` and the completion
    check read, so neither may hold a Path, a date or a dataclass."""
    config = _config(tmp_path)
    path = _write(config, _document(RETIRED_RECORD, PENDING_RECORD))
    vault = path.parent.parent
    _file_settled(config, _origin(RETIRED_RECORD))
    plan = _plan(config, vault)

    result = lc.apply_cleanup(
        vault, plan, workspace=WORKSPACE, config=config, today=TODAY
    )

    assert json.loads(json.dumps(plan.as_dict()))["counts"]["remove"] == 1
    payload = json.loads(json.dumps(result.as_dict()))
    assert payload["removed_count"] == 1
    assert payload["receipt"]["entries_removed"] == 1
    assert payload["suppressed"][0]["learning_id"] == RETIRED_RECORD.learning_id


def test_the_document_is_read_through_the_shared_parser(tmp_path: Path) -> None:
    """A reconciliation that read the file its own way would be reasoning about a
    shape nothing produces. Pinned by the ids matching what the parser mints."""
    config = _config(tmp_path)
    text = _document(RETIRED_RECORD)
    _write(config, text)

    document = parse_learnings(text, workspace=WORKSPACE)

    assert [entry.record.learning_id for entry in document.entries if entry.record] == [
        RETIRED_RECORD.learning_id,
        PROMOTED_RECORD.learning_id,
    ]
