"""The payload contract of `proposal_service.AcceptOutcome`.

Every accept helper in ``ciao/web/proposal_service.py`` used to answer with a
hand-built ``dict[str, Any]``; they now answer with :class:`AcceptOutcome` and
``as_dict()`` rebuilds that mapping. The routes read the fields directly, but
``proposal_actions.build_accept_result`` still consumes the mapping, and it
tells an ABSENT key from a false one (an outcome with no ``ok`` at all means
"nothing was written here", which is a success, not a failure). So a key this
type stops emitting — or starts emitting as ``None`` — is a behaviour change
wearing a refactor's clothes.

``EXPECTED_KEYS`` was captured by running these same branches against
``develop`` at 0d3938c7, before the type existed, and reading the keys off the
dictionaries the helpers returned then. The values were compared the same way.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from ciao import memory_proposals, memory_receipts as mr, memory_tool, project_doc_update
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web import proposal_service
from ciao.web.proposal_service import AcceptOutcome


# Branch -> the exact key set the pre-refactor dictionary carried. Sorted, so a
# key added or dropped is the diff pytest prints.
EXPECTED_KEYS: dict[str, list[str]] = {
    "region_prepare_failed": ["error", "ok", "region"],
    "region_write_raised": ["error", "ok", "region"],
    "region_written": ["ok", "receipt_id", "region", "usage", "written"],
    "region_written_no_usage": ["ok", "receipt_id", "region", "usage", "written"],
    "region_duplicate": ["duplicate", "ok", "region", "usage"],
    "region_unshaped": ["error", "ok", "region"],
    "region_conflict": ["conflict", "error", "ok", "region"],
    "region_deferred_with_row": [
        "competing", "deferred", "error", "ok", "reason", "region",
    ],
    "region_deferred_default": [
        "competing", "deferred", "error", "ok", "reason", "region",
    ],
    "region_reconcile_deferred": [
        "competing", "deferred", "error", "ok", "reason", "region",
    ],
    "region_failed": ["error", "ok", "region"],
    "people_no_name": ["error", "ok"],
    "people_written": ["destination", "ok"],
    "people_folded": ["destination", "ok"],
    "people_no_changes": ["error", "ok"],
    "people_fold_raised": ["error", "ok"],
    "learnings_written": ["destination", "ok"],
    "learnings_locked": ["error", "ok"],
    "project_no_target": ["error", "ok"],
    "project_missing_doc": ["error", "ok"],
    "project_folded": ["destination", "ok"],
    "project_no_changes": ["error", "ok"],
    "project_fold_raised": ["error", "ok"],
    "note_edit_no_sidecar": ["error", "ok"],
    "note_edit_vault_refused": ["error", "ok"],
    "note_edit_settled": ["destination", "error", "ok"],
    "note_edit_conflict": ["conflict", "destination", "error", "ok"],
    "note_edit_retire_conflict": ["conflict", "destination", "error", "ok"],
    "note_edit_retire_unreadable": ["conflict", "error", "ok"],
    "note_edit_no_frontmatter": ["destination", "error", "ok"],
    # A note that is not there any more is a conflict on BOTH operations, so the
    # client reopens the preview for it exactly as it does for a note that moved.
    "note_edit_unreadable": ["conflict", "destination", "error", "ok"],
    "note_edit_replaced": ["destination", "ok", "receipt_id"],
    "note_edit_written_raised": ["destination", "error", "ok", "receipt_id"],
    "note_edit_trashed": ["destination", "ok"],
    "note_edit_trash_unsettleable": ["destination", "error", "ok"],
    "note_edit_trash_raised": ["error", "ok"],
}

ROW = {"id": "p1", "workspace": "personal", "kind": "memory", "text": "a fact"}


def _config(tmp_path: Path) -> CiaoConfig:
    vault = tmp_path / "memory-vault"
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    (config.workspace_vault_root("personal") / "Workspace").mkdir(parents=True, exist_ok=True)
    return config


def _fake_accept(outcome: str, *, deferral: dict[str, Any] | None = None,
                 receipt: dict[str, Any] | None = None):
    """Stand in for `accept_region_fact`, driving one PromotionOutcome branch."""

    def _call(**kwargs: Any) -> tuple[str, str]:
        if deferral is not None and kwargs.get("deferral_out") is not None:
            kwargs["deferral_out"].append(deferral)
        if receipt is not None and kwargs.get("receipt_out") is not None:
            kwargs["receipt_out"].update(receipt)
        return outcome, "what landed"

    return _call


@pytest.fixture()
def region(monkeypatch, tmp_path):
    """A region promotion whose write and usage read are both stubbed."""
    config = _config(tmp_path)
    monkeypatch.setattr(memory_tool, "ensure_regions", lambda guide: None)
    monkeypatch.setattr(
        memory_tool,
        "memory_status",
        lambda guide, **kw: {"memory": {"chars": 10, "limit": 3000}},
    )

    def promote(**kwargs: Any) -> AcceptOutcome:
        return asyncio.run(proposal_service._promote_region_row(config, dict(ROW), **kwargs))

    return promote


def _assert_keys(branch: str, outcome: AcceptOutcome) -> dict[str, Any]:
    payload = outcome.as_dict()
    assert sorted(payload) == EXPECTED_KEYS[branch], branch
    # Absent, never null: a key that survives as `None` would read as a
    # reported empty value to the client and to `build_accept_result`.
    assert all(value is not None for value in payload.values()), branch
    return payload


def test_prepare_failure_keys(region, monkeypatch):
    monkeypatch.setattr(
        memory_tool, "ensure_regions", lambda guide: (_ for _ in ()).throw(OSError("boom"))
    )
    payload = _assert_keys("region_prepare_failed", region())
    assert payload["ok"] is False
    assert payload["region"] == "memory"


def test_write_raising_keys(region, monkeypatch):
    def raising(**kwargs: Any) -> tuple[str, str]:
        raise ValueError("nope")

    monkeypatch.setattr(memory_proposals, "accept_region_fact", raising)
    payload = _assert_keys("region_write_raised", region())
    assert payload["error"] == "nope"


def test_written_keys_carry_the_receipt(region, monkeypatch):
    """`receipt_id` (the memory-change receipt) rides on a written outcome."""
    monkeypatch.setattr(
        memory_proposals, "accept_region_fact", _fake_accept("written", receipt={"id": "r-1"})
    )
    payload = _assert_keys("region_written", region())
    assert payload == {
        "ok": True,
        "region": "memory",
        "written": "what landed",
        "usage": {"chars": 10, "limit": 3000},
        "receipt_id": "r-1",
    }


def test_written_keys_when_usage_cannot_be_read(region, monkeypatch):
    """Usage is advisory: an unreadable status reports `{}`, not a missing key."""
    monkeypatch.setattr(
        memory_proposals, "accept_region_fact", _fake_accept("written", receipt={"id": "r-2"})
    )
    monkeypatch.setattr(
        memory_tool, "memory_status", lambda guide, **kw: (_ for _ in ()).throw(RuntimeError("x"))
    )
    payload = _assert_keys("region_written_no_usage", region())
    assert payload["usage"] == {}


def test_duplicate_keys(region, monkeypatch):
    monkeypatch.setattr(memory_proposals, "accept_region_fact", _fake_accept("duplicate"))
    payload = _assert_keys("region_duplicate", region())
    assert payload["ok"] is True
    assert payload["duplicate"] is True
    assert "receipt_id" not in payload and "written" not in payload


def test_unshaped_keys(region, monkeypatch):
    monkeypatch.setattr(memory_proposals, "accept_region_fact", _fake_accept("unshaped"))
    payload = _assert_keys("region_unshaped", region())
    assert payload["ok"] is False
    assert "conflict" not in payload


def test_conflict_keys(region, monkeypatch):
    """`conflict` marks a refusal the client retries, so it must stay distinct."""
    monkeypatch.setattr(memory_proposals, "accept_region_fact", _fake_accept("conflict"))
    payload = _assert_keys("region_conflict", region())
    assert payload["conflict"] is True


def test_deferred_keys_carry_the_defer_row(region, monkeypatch):
    monkeypatch.setattr(
        memory_proposals,
        "accept_region_fact",
        _fake_accept(
            "deferred", deferral={"action": "defer", "reason": "unclear", "competing": ["old entry"]}
        ),
    )
    payload = _assert_keys("region_deferred_with_row", region())
    assert payload["deferred"] is True
    assert payload["reason"] == "unclear"
    assert payload["competing"] == ["old entry"]
    assert "It competes with: old entry." in payload["error"]


def test_deferred_keys_without_a_defer_row(region, monkeypatch):
    """No defer row still reports `competing`, as an empty list."""
    monkeypatch.setattr(memory_proposals, "accept_region_fact", _fake_accept("deferred"))
    payload = _assert_keys("region_deferred_default", region())
    assert payload["competing"] == []


def test_reconcile_retry_deferral_keys(region, monkeypatch):
    """The retry that cannot decide refuses BEFORE the write, same shape."""

    async def deferring(config, row, region_name, guide):
        return None, {"action": "defer", "reason": "still unclear", "competing": ["e1"]}

    calls: list[str] = []

    def unreachable(**kwargs: Any) -> tuple[str, str]:
        calls.append("wrote")
        return "written", "x"

    monkeypatch.setattr(proposal_service, "_plan_accept_reconcile", deferring)
    monkeypatch.setattr(memory_proposals, "accept_region_fact", unreachable)
    payload = _assert_keys("region_reconcile_deferred", region(reconcile=True))
    assert payload["reason"] == "still unclear"
    assert calls == [], "a deferred retry must not reach the write"


def test_failed_keys(region, monkeypatch):
    monkeypatch.setattr(memory_proposals, "accept_region_fact", _fake_accept("failed"))
    payload = _assert_keys("region_failed", region())
    assert payload["error"] == "could not write ciao:memory"


def test_people_keys(tmp_path, monkeypatch):
    config = _config(tmp_path)
    run = asyncio.run
    _assert_keys("people_no_name", run(proposal_service._accept_people_row(config, dict(ROW))))
    row = {**ROW, "kind": "people", "target": "Mo"}

    async def unreachable(**kwargs: Any) -> bool:
        raise AssertionError("a new note is created, not folded")

    monkeypatch.setattr(project_doc_update, "fold_fact_into_person_note", unreachable)
    payload = _assert_keys("people_written", run(proposal_service._accept_people_row(config, row)))
    assert payload["destination"] == "People/Mo.md"

    # The note now exists, so a second accept folds into it instead of refusing.
    seen: dict[str, Any] = {}

    async def wrote(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    async def unchanged(**kwargs: Any) -> bool:
        return False

    async def failed(**kwargs: Any) -> bool:
        kwargs["error_out"].append("TimeoutError: slow")
        return False

    monkeypatch.setattr(project_doc_update, "fold_fact_into_person_note", wrote)
    payload = _assert_keys("people_folded", run(proposal_service._accept_people_row(config, row)))
    assert payload["destination"] == "People/Mo.md"
    assert seen["note_path"].name == "Mo.md"
    assert seen["fact"] == row["text"]
    monkeypatch.setattr(project_doc_update, "fold_fact_into_person_note", unchanged)
    payload = _assert_keys("people_no_changes", run(proposal_service._accept_people_row(config, row)))
    assert "no changes" in payload["error"]
    monkeypatch.setattr(project_doc_update, "fold_fact_into_person_note", failed)
    payload = _assert_keys("people_fold_raised", run(proposal_service._accept_people_row(config, row)))
    assert payload["error"] == "fold failed: TimeoutError: slow"


def test_learnings_keys(tmp_path):
    config = _config(tmp_path)
    payload = _assert_keys(
        "learnings_written",
        proposal_service._accept_learnings_row(config, {**ROW, "kind": "learnings"}),
    )
    assert payload["destination"] == "Workspace/Learnings.md"


def test_a_held_learnings_lock_is_a_failed_row_not_an_exception(tmp_path, monkeypatch):
    """The care run and the migration take this file's lock; an accept waits, then gives up.

    `append_learning` reads and writes `Learnings.md` under that lock and raises
    `QueueLockError` — a `RuntimeError` — when it cannot take it. Catching only
    `OSError` let that refusal out of the handler as an exception: a 500 on the
    single accept, and every row after this one abandoned mid-loop on a batch,
    with nothing in the response to say why.
    """
    from ciao.memory_receipts import QueueLockError

    config = _config(tmp_path)

    def locked(*args: Any, **kwargs: Any) -> bool:
        raise QueueLockError("could not lock Workspace/Learnings.md")

    monkeypatch.setattr(memory_proposals, "append_learning", locked)
    payload = _assert_keys(
        "learnings_locked",
        proposal_service._accept_learnings_row(config, {**ROW, "kind": "learnings"}),
    )

    assert "could not append the learning" in payload["error"]
    assert "could not lock" in payload["error"]


def test_add_category_is_a_destination_not_a_rehome(tmp_path):
    """`add_category` is a destination action, so the builder reports where the
    category landed.

    An action missing from `proposal_actions._DESTINATION_ACTIONS` falls through
    to the re-home branch and reports `promoted=False` for a write that
    succeeded — the row leaves the queue and the client is told the category was
    not added.
    """
    from ciao import proposal_actions, proposal_kinds

    accept = proposal_kinds.accept_for("category")
    assert accept.action in proposal_actions._DESTINATION_ACTIONS
    result = proposal_actions.build_accept_result(
        "p1",
        accept,
        {"id": "p1", "kind": "category", "text": "Recipe Book"},
        {"ok": True, "destination": "Recipe Books"},
        include_usage=True,
    )
    payload = result.as_dict()
    assert payload["promoted"] is True
    assert payload["destination"] == "Recipe Books"
    assert "justified" not in payload, "the re-home shape must not be reached"


def test_add_category_carries_skipped_notes():
    """Notes a category accept skipped reach the client; an accept that skipped
    none carries no `skipped` key at all."""
    from ciao import proposal_actions, proposal_kinds

    accept = proposal_kinds.accept_for("category")
    row = {"id": "p1", "kind": "category", "text": "Recipe Book"}
    with_skips = proposal_actions.build_accept_result(
        "p1",
        accept,
        row,
        {"ok": True, "destination": "Recipes", "skipped": ["Two.md"]},
        include_usage=True,
    ).as_dict()
    assert with_skips["skipped"] == ["Two.md"]

    without = proposal_actions.build_accept_result(
        "p1",
        accept,
        row,
        {"ok": True, "destination": "Recipes"},
        include_usage=True,
    ).as_dict()
    assert "skipped" not in without


def test_project_keys(tmp_path, monkeypatch):
    config = _config(tmp_path)
    run = asyncio.run
    _assert_keys(
        "project_no_target",
        run(proposal_service._accept_project_row(config, {**ROW, "kind": "project"})),
    )
    _assert_keys(
        "project_missing_doc",
        run(proposal_service._accept_project_row(
            config, {**ROW, "kind": "project", "target": "nope.md"}
        )),
    )
    (tmp_path / "doc.md").write_text("# Doc\n", encoding="utf-8")
    row = {**ROW, "kind": "project", "target": "doc.md"}

    async def wrote(**kwargs: Any) -> bool:
        return True

    async def unchanged(**kwargs: Any) -> bool:
        return False

    async def boom(**kwargs: Any) -> bool:
        raise RuntimeError("fold blew up")

    monkeypatch.setattr(project_doc_update, "update_project_doc", wrote)
    payload = _assert_keys(
        "project_folded", run(proposal_service._accept_project_row(config, row))
    )
    assert payload["destination"] == "doc.md"
    monkeypatch.setattr(project_doc_update, "update_project_doc", unchanged)
    _assert_keys("project_no_changes", run(proposal_service._accept_project_row(config, row)))
    monkeypatch.setattr(project_doc_update, "update_project_doc", boom)
    _assert_keys("project_fold_raised", run(proposal_service._accept_project_row(config, row)))


# ---- note_edit: a whole-note write, and an attended retirement --------------


NOTE = "notes/office.md"

PLAIN = (
    "---\ntype: note\nupdated: 2024-01-05\n---\n\n"
    "# Office\n\nThe office is on Via Verdi 12, third floor.\n"
)
FOURTH = PLAIN.replace("third", "fourth")
NO_FRONTMATTER = "# Office\n\nThe office is on Via Verdi 12, third floor.\n"


def _note_edit_fixture(
    config: CiaoConfig, tmp_path: Path, **over: Any
) -> tuple[Path, Any, dict[str, Any]]:
    """A filed `note_edit` row against a note on disk, plus the note itself.

    Filed through the proposer rather than hand-written, so the row's
    ``note_edit.id`` is the id the accept resolves and the whole test is the real
    path from a `needs_review` verdict to a click.
    """
    from ciao import note_edit_proposals as nep
    from ciao import note_verification as nv

    vault = Path(config.workspace_vault_root("personal"))
    note = vault / NOTE
    note.parent.mkdir(parents=True, exist_ok=True)
    before_text = str(over.get("before", PLAIN))
    note.write_bytes(before_text.encode("utf-8"))
    fields: dict[str, Any] = {
        "workspace": "personal",
        "relative_path": NOTE,
        "expected_revision": mr.content_revision(before_text),
        "operation": nep.REPLACE,
        "before": PLAIN,
        "after": FOURTH,
        "outcome": nv.UPDATE,
        "coverage": nv.COVERAGE_COMPLETE,
        "evidence": (),
        "reason": "the third floor no longer exists",
    }
    fields.update(over)
    proposal = nep.file_note_edit(config, **fields)
    row = {**ROW, "kind": "note_edit", "note_edit": {"id": proposal.id}}
    return vault, proposal, row


def test_note_edit_keys(tmp_path: Path) -> None:
    """Every accept outcome the `note_edit` handler can produce.

    The refusals that carry a `destination` are the ones worth naming: they say
    WHICH note could not be written, which is the whole of what a person needs
    to go and look at the note themselves.
    """
    from ciao import note_edit_proposals as nep

    config = _config(tmp_path)
    # No sidecar at all: the row is a bullet whose record is gone.
    _assert_keys(
        "note_edit_no_sidecar",
        proposal_service._accept_note_edit_row(
            config, {**ROW, "kind": "note_edit", "note_edit": {"id": "0" * 16}}
        ),
    )
    _assert_keys(
        "note_edit_no_sidecar",
        proposal_service._accept_note_edit_row(
            config, {**ROW, "kind": "note_edit", "note_edit": {"id": ""}}
        ),
    )

    vault, proposal, row = _note_edit_fixture(config, tmp_path / "settled")
    # A record the owner already decided. The accept writes or trashes first and
    # settles second, so a row whose bullet outlived its settlement is the one
    # way to arrive here twice, and the second arrival must write nothing.
    nep.settle_note_edit(config, "personal", proposal.id, accepted=True)
    payload = _assert_keys(
        "note_edit_settled", proposal_service._accept_note_edit_row(config, row)
    )
    assert payload["destination"] == NOTE
    assert (vault / NOTE).read_bytes() == PLAIN.encode("utf-8"), "no second write"

    vault, _proposal, row = _note_edit_fixture(config, tmp_path)
    # A note the record does not describe any more: a conflict, never an
    # overwrite. The note is byte-identical afterwards.
    (vault / NOTE).write_bytes(FOURTH.encode("utf-8"))
    payload = _assert_keys(
        "note_edit_conflict",
        proposal_service._accept_note_edit_row(config, row),
    )
    assert payload["destination"] == NOTE
    assert (vault / NOTE).read_bytes() == FOURTH.encode("utf-8")

    # A re-stamp of a note with no frontmatter to stamp: the same refusal, and
    # for the same reason — adding the key would restructure a file that was
    # only asked to be verified.
    vault, _proposal, row = _note_edit_fixture(
        config,
        tmp_path / "no-frontmatter",
        operation=nep.RESTAMP,
        outcome="still_valid",
        before=NO_FRONTMATTER,
        after="",
    )
    payload = _assert_keys(
        "note_edit_no_frontmatter",
        proposal_service._accept_note_edit_row(config, row),
    )
    assert payload["destination"] == NOTE

    # A note that is not there at all: a conflict, like a retirement's, so the
    # client reopens its preview rather than reporting a permanent failure.
    vault, _proposal, row = _note_edit_fixture(
        config, tmp_path / "unreadable"
    )
    (vault / NOTE).unlink()
    payload = _assert_keys(
        "note_edit_unreadable", proposal_service._accept_note_edit_row(config, row)
    )
    assert payload["conflict"] is True

    # A config that cannot say where the workspace's notes live. Refused rather
    # than written somewhere a caller chose.
    _vault, _proposal, row = _note_edit_fixture(
        config, tmp_path / "no-vault"
    )

    class _NoVault:
        def workspace_vault_root(self, _name: str) -> Path:
            raise ValueError("this workspace has no vault")

    payload = _assert_keys(
        "note_edit_vault_refused",
        proposal_service._accept_note_edit_row(_NoVault(), row),
    )
    assert "workspace 'personal'" in payload["error"]

    vault, proposal, row = _note_edit_fixture(config, tmp_path / "written")
    payload = _assert_keys(
        "note_edit_replaced", proposal_service._accept_note_edit_row(config, row)
    )
    assert payload["destination"] == NOTE
    assert payload["receipt_id"], "an applied edit must name its receipt"
    assert (vault / NOTE).read_bytes() == FOURTH.encode("utf-8")
    assert mr.read_receipts(mr.journal_path(vault, None)), "journaled and undoable"
    settled = nep.read_sidecar(config, "personal", proposal.id)
    assert settled is not None and settled.settled != ""
    assert settled.accepted is True
    assert settled.receipt_id == payload["receipt_id"]

    # A write that lands and then cannot be recorded as settled says what landed
    # — the note, the path and the receipt to undo it — and is NOT a successful
    # accept. The bullet is the only thing left to act on, so an ok here would
    # remove it, leave the record unsettled and the check still pinned to this
    # revision: `_check_settles` would then suppress the note for good.
    vault, _proposal, row = _note_edit_fixture(config, tmp_path / "unsettleable")
    monkey = proposal_service._settle_note_edit
    try:
        proposal_service._settle_note_edit = (  # type: ignore[assignment]
            lambda *a, **k: "the sidecar is read-only"
        )
        payload = _assert_keys(
            "note_edit_written_raised",
            proposal_service._accept_note_edit_row(config, row),
        )
    finally:
        proposal_service._settle_note_edit = monkey  # type: ignore[assignment]
    assert payload["ok"] is False, "the row must survive to be dismissed"
    assert payload["receipt_id"] != ""
    assert "could not be settled" in payload["error"]
    assert (vault / NOTE).read_bytes() == FOURTH.encode("utf-8"), "the write stands"
    still = nep.read_sidecar(config, "personal", _proposal.id)
    assert still is not None and still.settled == "", "nothing was recorded"


def test_note_edit_trash_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retirement is attended-only and reversible: it moves the note into the
    review trash through ``vault_review.trash_note`` and nothing else.

    The expected revision guards it first, which is the only conflict check a
    move has: `trash_note` hashes the bytes the accept just read, so on its own
    it compares the note to itself.
    """
    from ciao import note_edit_proposals as nep
    from ciao import vault_review as review

    config = _config(tmp_path)
    vault, _proposal, row = _note_edit_fixture(
        config, tmp_path, operation=nep.RETIRE, outcome="retire", after=""
    )

    payload = _assert_keys(
        "note_edit_trashed", proposal_service._accept_note_edit_row(config, row)
    )

    assert (vault / NOTE).exists() is False, "the note left the vault"
    trashed = vault / payload["destination"]
    assert trashed.is_file(), payload["destination"]
    assert trashed.read_text(encoding="utf-8") == PLAIN
    assert not Path(payload["destination"]).is_absolute(), (
        "a destination inside the vault is named like every other one"
    )
    # Restore is the review panel's own primitive, and it puts the note back:
    # a retirement is a move, not a deletion.
    review.restore_note(vault, trashed.stem)
    assert (vault / NOTE).read_text(encoding="utf-8") == PLAIN

    # A note rewritten after the proposal was filed is a conflict, and the note
    # is left exactly as it stands: the hand-written line was never judged.
    vault, _proposal, row = _note_edit_fixture(
        config, tmp_path / "moved", operation=nep.RETIRE, outcome="retire", after=""
    )
    edited = (FOURTH + "A line somebody added by hand.\n").encode("utf-8")
    (vault / NOTE).write_bytes(edited)
    payload = _assert_keys(
        "note_edit_retire_conflict", proposal_service._accept_note_edit_row(config, row)
    )
    assert payload["destination"] == NOTE
    assert (vault / NOTE).read_bytes() == edited

    # A note that is not there at all cannot be retired, and says so as a
    # conflict rather than an ordinary failure.
    vault, _proposal, row = _note_edit_fixture(
        config, tmp_path / "gone", operation=nep.RETIRE, outcome="retire", after=""
    )
    (vault / NOTE).unlink()
    _assert_keys(
        "note_edit_retire_unreadable",
        proposal_service._accept_note_edit_row(config, row),
    )

    # A retirement that lands and then cannot be recorded is the same shape as an
    # edit's: the note is in the trash, the row stays, and the row is the only
    # thing left to dismiss — and a dismissal settles the record.
    vault, _proposal, row = _note_edit_fixture(
        config,
        tmp_path / "unsettleable",
        operation=nep.RETIRE,
        outcome="retire",
        after="",
    )
    monkeypatch.setattr(
        proposal_service, "_settle_note_edit", lambda *a, **k: "the sidecar is read-only"
    )
    payload = _assert_keys(
        "note_edit_trash_unsettleable",
        proposal_service._accept_note_edit_row(config, row),
    )
    assert payload["ok"] is False, "the row must survive to be dismissed"
    assert "could not be settled" in payload["error"]
    assert (vault / payload["destination"]).is_file()

    # A trash that cannot happen is a refusal, and the note is still there. Last,
    # because it patches out the very primitive the two cases above performed.
    vault, _proposal, row = _note_edit_fixture(
        config, tmp_path / "refused", operation=nep.RETIRE, outcome="retire", after=""
    )

    def _boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("candidate changed or no longer exists")

    monkeypatch.setattr(review, "trash_note", _boom)
    payload = _assert_keys(
        "note_edit_trash_raised",
        proposal_service._accept_note_edit_row(config, row),
    )
    assert "could not retire" in payload["error"]
    assert (vault / NOTE).is_file()


def test_note_edit_reaches_its_own_branch_in_the_result_builder(
    tmp_path: Path,
) -> None:
    """`note_edit` is not a destination action and not a region edit.

    An action missing from `build_accept_result` falls through to the re-home
    branch and reports `promoted=False` for a write that succeeded, and a
    conflict would be reported as a destination write rather than as a
    conflict. This asserts both, because both are the failure this branch
    exists to prevent.
    """
    from ciao import proposal_actions, proposal_kinds

    accept = proposal_kinds.accept_for("note_edit")
    assert accept.action == "note_edit"
    assert accept.action not in proposal_actions._DESTINATION_ACTIONS

    result = proposal_actions.build_accept_result(
        "p1",
        accept,
        {"id": "p1", "kind": "note_edit", "text": "notes/office.md — replace"},
        {"ok": True, "destination": "notes/office.md", "receipt_id": "mrcpt_abc"},
        include_usage=True,
    ).as_dict()
    assert result["action"] == "note_edit"
    assert result["promoted"] is True
    assert result["dismissed"] is True
    assert result["destination"] == "notes/office.md"
    assert "justified" not in result, "the re-home shape must not be reached"
    assert "region" not in result, "a note edit is not a region write"

    # A conflict reads as a conflict: the row is still promotable, just not
    # against the text the operator read, so the client reopens its preview.
    conflicted = proposal_actions.build_accept_result(
        "p1",
        accept,
        {"id": "p1", "kind": "note_edit", "text": "notes/office.md — replace"},
        {
            "ok": False,
            "conflict": True,
            "destination": "notes/office.md",
            "error": "this note changed since the proposal was filed",
        },
        include_usage=True,
    ).as_dict()
    assert conflicted["conflict"] is True
    assert conflicted["dismissed"] is False
    assert conflicted["promoted"] is False
    assert "changed since" in conflicted["error"]


def test_every_captured_branch_is_asserted():
    """The table is the contract, so an entry nobody checks is a hole in it."""
    source = Path(__file__).read_text(encoding="utf-8")
    checked = set(re.findall(r'_assert_keys\(\s*"(\w+)"', source))
    assert set(EXPECTED_KEYS) - checked == set()


def test_empty_outcome_reports_nothing():
    """An outcome with no fields emits no keys at all.

    `build_accept_result` reads an absent ``ok`` as "nothing was written here"
    — how a re-home (moved above the queue-file grouping) and a row the batch
    never reached both report success — so an empty outcome must not acquire a
    false ``ok``.
    """
    assert AcceptOutcome().as_dict() == {}
    assert AcceptOutcome(ok=False).as_dict() == {"ok": False}
    assert AcceptOutcome(destination="").as_dict() == {"destination": ""}
