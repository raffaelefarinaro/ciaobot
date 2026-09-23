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

from ciao import memory_proposals, memory_tool, project_doc_update
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
    "project_no_target": ["error", "ok"],
    "project_missing_doc": ["error", "ok"],
    "project_folded": ["destination", "ok"],
    "project_no_changes": ["error", "ok"],
    "project_fold_raised": ["error", "ok"],
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
