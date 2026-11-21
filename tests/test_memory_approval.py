"""Per-kind Workspace care approval: the stored value, the one resolver, the worklist.

Every case runs on ``tmp_path``. The seam (the accept itself) is covered in
``tests/test_curation_apply.py``; the PATCH merge in ``tests/test_workspace_settings_routes.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ciao import curation_run
from ciao.config import (
    CiaoConfig,
    WorkspaceConfig,
    coerce_memory_approval,
    merge_memory_approval,
)
from ciao.proposal_predictions import (
    HandoverPolicy,
    accept_allowed,
    accept_kinds,
    record_prediction,
)

KINDS = ("memory", "profile")


def _expected_accept(legacy: str, mode: str | None) -> bool:
    """The rule in words: an explicit choice wins; unset follows the legacy setting."""
    if mode == "approve":
        return False
    if mode == "auto":
        return True
    return legacy == "all"


# ── stored value ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ({}, {}),
        ({"memory": "auto"}, {"memory": "auto"}),
        ({"profile": "approve"}, {"profile": "approve"}),
        ({"memory": "approve", "profile": "auto"}, {"memory": "approve", "profile": "auto"}),
    ],
)
def test_coerce_accepts_the_sparse_map(raw: object, expected: dict[str, str]) -> None:
    assert coerce_memory_approval(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        {"project": "auto"},  # kind not supported yet
        {"note": "approve"},
        {"memory": "everything"},  # unknown mode
        {"memory": "all"},  # the legacy handover word is not a mode
        {"memory": "Auto"},  # exact match, like memory_handover
        {"Memory": "auto"},
        {"memory": None},  # null is a PATCH reset, never a stored value
        "auto",  # not an object
        ["memory"],
    ],
)
def test_coerce_rejects_unknown_kinds_modes_and_shapes(raw: object) -> None:
    with pytest.raises(ValueError, match="memory_approval"):
        coerce_memory_approval(raw)


# ── PATCH merge ──────────────────────────────────────────────────────────────


def test_merge_sets_one_kind_and_keeps_the_other() -> None:
    assert merge_memory_approval({"profile": "auto"}, {"memory": "approve"}) == {
        "profile": "auto",
        "memory": "approve",
    }


def test_merge_null_resets_only_that_kind_to_inherited() -> None:
    assert merge_memory_approval({"memory": "approve", "profile": "auto"}, {"profile": None}) == {
        "memory": "approve"
    }


def test_merge_does_not_mutate_the_current_map() -> None:
    current = {"memory": "approve"}
    merge_memory_approval(current, {"profile": "auto"})
    assert current == {"memory": "approve"}


@pytest.mark.parametrize(
    "patch",
    [
        None,  # whole-field null is rejected; reset per key instead
        "approve",
        {"project": "auto"},
        {"memory": "everything"},
        {"memory": "auto", "profile": "bogus"},  # one bad entry rejects the whole patch
    ],
)
def test_merge_rejects_the_whole_patch(patch: object) -> None:
    with pytest.raises(ValueError, match="memory_approval"):
        merge_memory_approval({"profile": "auto"}, patch)


# ── resolver: one rule, every kind x legacy x explicit ──────────────────────


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("legacy", ["off", "dismiss", "all"])
@pytest.mark.parametrize("mode", [None, "auto", "approve"])
def test_accept_matrix(kind: str, legacy: str, mode: str | None) -> None:
    policy = HandoverPolicy(memory_handover=legacy, memory_approval={} if mode is None else {kind: mode})

    assert accept_allowed(policy, kind) is _expected_accept(legacy, mode)


@pytest.mark.parametrize("legacy", ["off", "dismiss", "all"])
def test_an_explicit_choice_for_one_kind_does_not_move_the_other(legacy: str) -> None:
    policy = HandoverPolicy(memory_handover=legacy, memory_approval={"memory": "auto"})

    assert accept_allowed(policy, "memory") is True
    assert accept_allowed(policy, "profile") is (legacy == "all")


def test_accept_kinds_lists_the_kinds_the_run_may_accept_in_order() -> None:
    assert accept_kinds(HandoverPolicy(memory_handover="off", memory_approval={"profile": "auto"})) == [
        "profile"
    ]
    assert accept_kinds(HandoverPolicy(memory_handover="all", memory_approval={"memory": "approve"})) == [
        "profile"
    ]
    assert accept_kinds(HandoverPolicy(memory_handover="all", memory_approval={})) == ["memory", "profile"]


def test_policy_reads_the_registered_workspace() -> None:
    registered = WorkspaceConfig(
        name="work",
        vault_root="work",
        memory_handover="off",
        memory_approval={"memory": "auto"},
    )

    assert HandoverPolicy.of(registered) == HandoverPolicy(
        memory_handover="off", memory_approval={"memory": "auto"}
    )


# ── registry round trip ─────────────────────────────────────────────────────


def _registry(tmp_path: Path, entries: list[dict]) -> tuple[CiaoConfig, Path]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(exist_ok=True)
    registry = runtime / "workspaces.json"
    registry.write_text(json.dumps(entries), encoding="utf-8")
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
    }
    return CiaoConfig.from_env(env), registry


def test_a_registry_without_the_key_loads_empty_and_is_rewritten_without_it(tmp_path: Path) -> None:
    config, registry = _registry(tmp_path, [{"name": "work", "vault_root": "work", "memory_handover": "dismiss"}])

    assert config.workspace("work").memory_approval == {}

    config.persist_workspace_registry()
    stored = json.loads(registry.read_text(encoding="utf-8"))
    assert "memory_approval" not in stored[0]
    assert stored[0]["memory_handover"] == "dismiss"


def test_an_explicit_choice_survives_a_save_and_a_reset_removes_its_key(tmp_path: Path) -> None:
    config, registry = _registry(
        tmp_path,
        [{"name": "work", "vault_root": "work", "memory_approval": {"memory": "approve", "profile": "auto"}}],
    )
    assert config.workspace("work").memory_approval == {"memory": "approve", "profile": "auto"}

    config.persist_workspace_registry()
    assert json.loads(registry.read_text(encoding="utf-8"))[0]["memory_approval"] == {
        "memory": "approve",
        "profile": "auto",
    }

    config.workspace("work").memory_approval = {}
    config.persist_workspace_registry()
    assert "memory_approval" not in json.loads(registry.read_text(encoding="utf-8"))[0]


def test_a_stored_value_that_fails_validation_loads_as_inherited_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        config, _registry_path = _registry(
            tmp_path, [{"name": "work", "vault_root": "work", "memory_approval": {"project": "auto"}}]
        )

    assert config.workspace("work").memory_approval == {}
    assert any("memory_approval" in record.getMessage() for record in caplog.records)


# ── worklist: a kind the operator asked to approve plans no accept ──────────

QUEUE = "Workspace/Memory-Proposals.md"


def _queue_vault(tmp_path: Path, *lines: str) -> Path:
    vault = tmp_path / "vault"
    queue = vault / QUEUE
    queue.parent.mkdir(parents=True)
    queue.write_text("# Memory Proposals\n\n## 2026-01-01T00:00:00Z\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return vault


def test_an_approve_kind_plans_no_accept_work_and_an_auto_kind_does(tmp_path: Path) -> None:
    vault = _queue_vault(tmp_path, "- [memory] Prefers uv over pip.", "- [profile] Writes in short sentences.")
    queue = vault / QUEUE
    record_prediction(queue, kind="memory", text="Prefers uv over pip.", verdict="accept", reason="r")
    record_prediction(queue, kind="profile", text="Writes in short sentences.", verdict="accept", reason="r")

    # Legacy all, memory explicitly approved: only the profile row is planned.
    approve_memory = HandoverPolicy(memory_handover="all", memory_approval={"memory": "approve"})
    items = curation_run._proposal_items(vault, approve_memory)
    assert len(items) == 1 and len(items[0].keys) == 1

    both_approved = HandoverPolicy(
        memory_handover="all", memory_approval={"memory": "approve", "profile": "approve"}
    )
    assert curation_run._proposal_items(vault, both_approved) == []

    auto_under_off = HandoverPolicy(memory_handover="off", memory_approval={"profile": "auto"})
    items = curation_run._proposal_items(vault, auto_under_off)
    assert len(items) == 1 and len(items[0].keys) == 1


# ── fail closed for a kind the policy does not know ─────────────────────────


@pytest.mark.parametrize("kind", ["project", ""])
@pytest.mark.parametrize("policy_mode", [None, "auto"])
def test_an_unknown_kind_is_never_accepted(kind: str, policy_mode: str | None) -> None:
    approval = {} if policy_mode is None else {kind: policy_mode}
    policy = HandoverPolicy(memory_handover="all", memory_approval=approval)

    assert accept_allowed(policy, kind) is False
