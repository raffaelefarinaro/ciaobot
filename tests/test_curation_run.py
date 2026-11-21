"""The nightly curation worklist, run budget and lease.

Issue #461: the Workspace care schedule prompt asked a model to answer questions files already
answer (is the queue empty, is the region at 85%, is the weekly marker seven
days old), and nothing stopped two runs — or a run and an archiving chat —
from rewriting the same region from two stale reads.

These tests pin the three claims that replaced that: an idle workspace is
computable as idle without a model turn, a budget-limited run resumes rather
than restarts, and a held lease is honoured by the second run and released as
soon as the holder has no work left.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import skill_proposals, task_decisions
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    render_learning,
)

if TYPE_CHECKING:
    from ciao.entity_types import EntityTypeRegistry


MEMORY_START = "<!-- ciao:memory:start -->"
MEMORY_END = "<!-- ciao:memory:end -->"
PROFILE_START = "<!-- ciao:profile:start -->"
PROFILE_END = "<!-- ciao:profile:end -->"


@pytest.fixture(autouse=True)
def _isolated_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every lock file out of the shared temp root the install uses."""
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


def _guide(tmp_path: Path, *, memory: str = "", profile: str = "") -> Path:
    from ciao.memory_tool import ensure_regions

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Workspace\n", encoding="utf-8", newline="")
    ensure_regions(guide)
    if memory or profile:
        from ciao.memory_tool import write_region

        if memory:
            write_region(guide, "memory", [memory])
        if profile:
            write_region(guide, "profile", [profile])
    return guide


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _fresh_log(vault: Path, *, last_full_pass: str) -> None:
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: {last_full_pass}\n---\n\n# Curation log\n",
        encoding="utf-8", newline=""
    )


def _categories(vault: Path) -> "EntityTypeRegistry":
    """The category list ``build_worklist`` measures the notes against.

    The registry belongs to the agent vault root, which is a different directory
    from a workspace's notes on an install that has not re-rooted — so it is
    passed in rather than read from ``vault_root``. In these tests the two are the
    same directory, which keeps every other worklist assertion about one tree;
    :func:`test_a_cluster_the_registry_already_knows_is_not_offered` is the one
    that separates them.
    """
    from ciao.entity_types import load_entity_types

    return load_entity_types(vault)


def _planning_result(payload: dict[str, object] | None = None):
    from ciao.cli import _CurationPlanningResult

    worklist = cr.Worklist((), False, "", "")
    return _CurationPlanningResult(payload or {}, worklist, worklist)


# ── Worklist ──────────────────────────────────────────────────────────────


def test_remaining_items_filters_only_done_and_empty_keys_without_mutating() -> None:
    discovered = (
        cr.WorklistItem("first", "First", "reason", ("done", "", "pending", "pending"), True),
        cr.WorklistItem("second", "Second", "other", ("also-done", "later")),
    )

    remaining = cr.remaining_items(discovered, {"done", "also-done"})

    assert tuple((item.pass_id, item.keys) for item in remaining) == (
        ("first", ("pending", "pending")),
        ("second", ("later",)),
    )
    assert (remaining[0].label, remaining[0].reason, remaining[0].weekly) == (
        "First", "reason", True
    )
    assert discovered[0].keys == ("done", "", "pending", "pending")
    assert discovered[1].keys == ("also-done", "later")


@pytest.mark.parametrize("done_keys", [None, set(), frozenset()])
def test_remaining_items_always_removes_empty_keys(done_keys) -> None:
    item = cr.WorklistItem("pass", "Pass", "reason", ("", "key"))

    assert cr.remaining_items((item,), done_keys)[0].keys == ("key",)


@pytest.mark.parametrize("done_keys", [None, set(), frozenset()])
def test_remaining_items_drops_zero_key_item_without_done_keys(done_keys) -> None:
    item = cr.WorklistItem("empty", "Empty", "no keys", ())

    assert cr.remaining_items((item,), done_keys) == ()


def test_remaining_items_drops_entirely_completed_item() -> None:
    item = cr.WorklistItem("completed", "Completed", "all done", ("done",))

    assert cr.remaining_items((item,), {"done"}) == ()


def test_remaining_items_does_not_sample_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    item = cr.WorklistItem("pass", "Pass", "reason", ("done", "pending"), weekly=True)

    def unexpected_clock() -> str:
        pytest.fail("pure filtering must not sample the clock")

    monkeypatch.setattr(cr, "_now_iso", unexpected_clock)

    assert cr.remaining_items((item,), {"done"}) == (
        replace(item, keys=("pending",)),
    )


def test_worklist_views_keep_discovery_and_sample_one_timestamp_after_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    item = cr.WorklistItem("synthetic", "Synthetic", "reason", ("done", "pending"))
    events: list[str] = []
    original_filter = cr.remaining_items

    def filter_items(discovered, done_keys=None):
        events.append("filter")
        return original_filter(discovered, done_keys)

    monkeypatch.setattr(cr, "remaining_items", filter_items)
    monkeypatch.setattr(cr, "_now_iso", lambda: events.append("clock") or "now")
    views = cr.build_worklist_views(
        vault_root=vault,
        guide_path=guide,
        category_registry=_categories(vault),
        today=date(2026, 9, 19),
        done_keys={"done"},
        category_items=(item,),
        workspace=vault.name,
    )

    assert events == ["filter", "clock"]
    discovered = next(entry for entry in views.discovered.items if entry.pass_id == "synthetic")
    remaining = next(entry for entry in views.remaining.items if entry.pass_id == "synthetic")
    assert discovered == item
    assert remaining.keys == ("pending",)
    assert views.discovered.generated_at == views.remaining.generated_at == "now"


def test_legacy_builder_filters_before_its_single_timestamp_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    item = cr.WorklistItem("synthetic", "Synthetic", "reason", ("done", "pending"))
    events: list[str] = []
    original_filter = cr.remaining_items

    def filter_items(discovered, done_keys=None):
        events.append("filter")
        return original_filter(discovered, done_keys)

    monkeypatch.setattr(cr, "remaining_items", filter_items)
    monkeypatch.setattr(cr, "_now_iso", lambda: events.append("clock") or "now")
    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        category_registry=_categories(vault),
        today=date(2026, 9, 19),
        done_keys={"done"},
        category_items=(item,),
        workspace=vault.name,
    )

    assert events == ["filter", "clock"]
    synthetic = next(entry for entry in worklist.items if entry.pass_id == "synthetic")
    assert synthetic.keys == ("pending",)
    assert worklist.generated_at == "now"


def test_a_fresh_idle_workspace_has_nothing_to_do(tmp_path: Path) -> None:
    """Acceptance: the empty case is decided in code, not by a model turn."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty
    assert worklist.weekly_due is False
    assert worklist.items == ()


def test_every_mechanical_signal_lands_in_the_worklist(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())

    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n- [people Ada] Ada runs the release train.\n",
        encoding="utf-8", newline=""
    )
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        "# Learnings\n\n## Active\n"
        "- [pin-the-node-version] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
        encoding="utf-8", newline=""
    )
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "deploy.md").write_text("x", encoding="utf-8", newline="")
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: 2026-09-01\n---\n\n{'x' * (cr.LOG_ROTATION_BYTES + 1)}",
        encoding="utf-8", newline=""
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    by_pass = {item.pass_id: item for item in worklist.items}
    assert set(by_pass) == {
        cr.PASS_PROPOSALS,
        cr.PASS_LEARNINGS,
        cr.PASS_HYGIENE,
        cr.PASS_GUIDE,
        cr.PASS_LOGS,
        cr.PASS_SKILL_PROPOSALS,
    }
    assert worklist.weekly_due is True
    assert by_pass[cr.PASS_HYGIENE].keys == (cr.HYGIENE_INDEX_KEY, cr.HYGIENE_AUDIT_KEY)
    # Pass order is the skill's order, so a short run always drops the tail.
    assert [item.pass_id for item in worklist.items] == [
        cr.PASS_PROPOSALS,
        cr.PASS_LEARNINGS,
        cr.PASS_HYGIENE,
        cr.PASS_GUIDE,
        cr.PASS_LOGS,
        cr.PASS_SKILL_PROPOSALS,
    ]


def _cluster_vault(vault: Path, type_: str, count: int) -> None:
    notes = vault / "Journals"
    notes.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (notes / f"Note{index}.md").write_text(
            f"---\ntype: {type_}\n---\n# Note{index}\n", encoding="utf-8", newline=""
        )


def test_a_three_note_cluster_becomes_a_category_item(tmp_path: Path) -> None:
    """The pass files the bullet itself and reports the work.

    It is the one pass here that writes, and deliberately so: the trigger is a
    count over the notes on disk, so nothing about it needs a model and the
    decision belongs in the queue, not in a chat.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 3)

    lease = cr.begin_run(vault, holder="nightly")
    category_items = tuple(
        cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault)
    )
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        category_items=category_items,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    by_pass = {item.pass_id: item for item in worklist.items}
    assert cr.PASS_CATEGORIES in by_pass
    assert by_pass[cr.PASS_CATEGORIES].keys == (
        cr.item_key(cr.PASS_CATEGORIES, "recipe-book"),
    )
    queue = (vault / cr.PROPOSALS_RELATIVE).read_text(encoding="utf-8")
    assert "- [category recipe-book]" in queue
    # It is a real queue row, so the review pass sees it as work to route too.
    assert cr.PASS_PROPOSALS in by_pass


def test_a_cluster_the_registry_already_knows_is_not_offered(tmp_path: Path) -> None:
    """A category the owner accepted is canonical, even when its registry file
    lives in a different directory from the notes.

    This is the pre-re-rooting layout: notes under ``memory-vault/personal`` and
    ``entity-types.yaml`` in ``memory-vault``, which is where the accept writes
    and where ``GET``/``PATCH /api/memory/entity-types`` reads. A pass that loaded
    the registry from the notes root instead read a file that does not exist, so
    the accepted id stayed unlisted — queued again the moment the cluster grew,
    and then refused on accept as a duplicate nobody could resolve.
    """
    from ciao import entity_types

    agent_vault = tmp_path / "memory-vault"
    notes_vault = agent_vault / "personal"
    (notes_vault / "Workspace").mkdir(parents=True)
    guide = _guide(tmp_path)
    _fresh_log(notes_vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(notes_vault, "recipe-book", 3)
    entity_types.write_vault_file(
        agent_vault,
        [
            *entity_types.stock_entity_type_registry().entries(),
            entity_types.EntityType(id="recipe-book", label="Recipe book"),
        ],
    )
    assert not (notes_vault / entity_types.VAULT_FILENAME).exists()

    worklist = cr.build_worklist(
        vault_root=notes_vault,
        workspace=notes_vault.name,
        guide_path=guide,
        category_registry=entity_types.load_entity_types(agent_vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_CATEGORIES not in {item.pass_id for item in worklist.items}
    assert not (notes_vault / cr.PROPOSALS_RELATIVE).exists()


def test_a_declined_category_is_not_work_twice(tmp_path: Path) -> None:
    """The refusal is keyed by id, so the cluster it described never comes back
    and the pass has nothing to report."""
    from ciao.vocabulary_proposals import decline_category, read_category_sidecar

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 3)

    lease = cr.begin_run(vault, holder="nightly")
    first_categories = tuple(
        cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault)
    )
    first = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        category_items=first_categories,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert cr.PASS_CATEGORIES in {item.pass_id for item in first.items}
    decline_category(vault, "recipe-book")
    assert read_category_sidecar(vault, "recipe-book")["declined"] is True
    # The bullet is gone, as it would be after the owner rejected it.
    (vault / cr.PROPOSALS_RELATIVE).unlink()

    second_categories = tuple(
        cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault)
    )
    second = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        category_items=second_categories,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_CATEGORIES not in {item.pass_id for item in second.items}
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()


def _expire_lease(vault: Path) -> None:
    state = cr.load_state(vault)
    state.lease["expires_at"] = "2000-01-01T00:00:00Z"
    cr._store(cr.state_path(vault), state)


def _no_stray_temp_files(vault: Path) -> bool:
    return not list(vault.rglob("*.write.tmp"))


@pytest.mark.parametrize("during_scan", ["expired", "replaced"])
def test_category_publication_refuses_lost_begin_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_scan: str
) -> None:
    """The vocabulary scan runs outside every lock. A holder that lost its lease
    while it ran must publish neither the sidecar nor the queue bullet, and must
    leave no staged ``*.write.tmp`` behind.

    The lease guard is the only thing that stops the sidecar being written here.
    With the guard disabled, both cases fail on the sidecar assertion, because the
    sidecar was written after the scan with no holder check at all.
    """
    from ciao import vault_index
    from ciao.vocabulary_proposals import category_sidecar_path

    vault = _vault(tmp_path)
    _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    lease = cr.begin_run(vault, holder="nightly", ttl_s=600)
    real_scan = vault_index.scan_vault

    def scan_then_lose(*args: object, **kwargs: object):
        result = real_scan(*args, **kwargs)
        if during_scan == "expired":
            _expire_lease(vault)
        else:
            cr.end_run(vault, holder=lease.holder)
            cr.begin_run(vault, holder="replacement")
        return result

    monkeypatch.setattr(vault_index, "scan_vault", scan_then_lose)

    refused: Exception | None = None
    try:
        cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault)
    except cr.CurationBusy as exc:
        refused = exc

    assert not category_sidecar_path(vault, "recipe-book").exists()
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()
    assert _no_stray_temp_files(vault)
    assert refused is not None
    if during_scan == "replaced":
        assert cr.load_state(vault).lease["holder"].startswith("replacement:")


def _begin_args_for_recipe_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    return vault, guide


def test_category_begin_lease_expiring_during_scan_exits_75_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real ``curation-begin``: the lease really expires while the category scan
    runs, and nothing is published or printed as success."""
    from ciao import vault_index
    from ciao import vocabulary_proposals as vp
    from ciao.cli import CURATION_BUSY_EXIT, _curation_begin_command

    vault, guide = _begin_args_for_recipe_vault(tmp_path)
    real_scan = vault_index.scan_vault
    expired = []

    def scan_then_expire(*args: object, **kwargs: object):
        result = real_scan(*args, **kwargs)
        if not expired:
            expired.append(True)
            _expire_lease(vault)
        return result

    monkeypatch.setattr(vault_index, "scan_vault", scan_then_expire)

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    captured = capsys.readouterr()
    assert expired == [True]
    assert code == CURATION_BUSY_EXIT
    assert captured.out == ""
    assert "curation lease lost while building the worklist" in captured.err
    assert not vp.category_sidecar_path(vault, "recipe-book").exists()
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()
    assert _no_stray_temp_files(vault)


def test_category_begin_plans_without_categories_when_the_queue_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A queue lock that cannot be taken in time is not a reason to fail the
    nightly plan: the category pass is advisory, the plan is produced, and an
    empty plan releases the lease rather than holding it until the TTL."""
    import functools
    import threading

    from ciao import memory_receipts as mr
    from ciao.cli import _curation_begin_command

    vault, guide = _begin_args_for_recipe_vault(tmp_path)
    queue = vault / cr.PROPOSALS_RELATIVE
    held = threading.Event()
    release = threading.Event()

    def hold_queue_lock() -> None:
        with mr.queue_lock(queue):
            held.set()
            release.wait(10)

    holder_thread = threading.Thread(target=hold_queue_lock)
    holder_thread.start()
    assert held.wait(10)
    # The real timeout is 30 s; the same contended acquire with a short bound.
    real_queue_lock = mr.queue_lock
    monkeypatch.setattr(mr, "queue_lock", functools.partial(real_queue_lock, timeout_s=0.05))
    try:
        code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))
    finally:
        release.set()
        holder_thread.join(10)

    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["empty"] is True
    assert cr.active_lease(vault) is None
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()


def test_category_begin_plans_when_publication_fails_with_an_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao import vocabulary_proposals as vp
    from ciao.cli import _curation_begin_command

    vault, guide = _begin_args_for_recipe_vault(tmp_path)

    def read_only(*_args: object, **_kwargs: object):
        raise PermissionError("read-only vault")

    monkeypatch.setattr(vp, "write_category_sidecar", read_only)

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["empty"] is True
    assert cr.active_lease(vault) is None


def test_category_begin_plans_when_the_queue_is_not_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable queue is a publication failure: the plan is still produced."""
    from ciao.cli import _curation_begin_command

    vault, guide = _begin_args_for_recipe_vault(tmp_path)
    queue = vault / cr.PROPOSALS_RELATIVE
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_bytes(b"\xff\xfe not utf-8\n")

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["empty"] is True
    assert cr.active_lease(vault) is None


def test_a_begin_that_exits_non_zero_after_taking_the_lease_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao.cli import _curation_begin_command

    vault, guide = _begin_args_for_recipe_vault(tmp_path)

    def broken_plan(*_args: object, **_kwargs: object):
        raise cr.CurationStateError("state file is untrustworthy")

    monkeypatch.setattr("ciao.cli._curation_plan", broken_plan)

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    assert code == 1
    assert cr.active_lease(vault) is None
    assert cr.load_state(vault).last_run.get("status") == "error"


def test_category_publish_expiry_between_sidecar_and_queue_is_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar-then-queue order is the recovery protocol, not a transaction.

    A lease lost after the sidecar lands leaves the sidecar with no bullet and no
    success. A fresh live run then finishes the job with exactly one bullet.
    """
    from ciao import vocabulary_proposals as vp

    vault = _vault(tmp_path)
    _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    lease = cr.begin_run(vault, holder="nightly", ttl_s=600)
    real_write = vp.write_category_sidecar

    def write_then_expire(root: Path, payload: dict, **kwargs: object) -> Path:
        path = real_write(root, payload, **kwargs)
        _expire_lease(vault)
        return path

    monkeypatch.setattr(vp, "write_category_sidecar", write_then_expire)
    with pytest.raises(cr.CurationBusy):
        cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault)
    assert vp.category_sidecar_path(vault, "recipe-book").exists()
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()

    monkeypatch.setattr(vp, "write_category_sidecar", real_write)
    retry = cr.begin_run(vault, holder="retry", ttl_s=600)
    items = cr._category_cluster_items(vault, holder=retry.holder, registry_root=vault)

    assert [item.keys for item in items] == [
        (cr.item_key(cr.PASS_CATEGORIES, "recipe-book"),)
    ]
    queue = (vault / cr.PROPOSALS_RELATIVE).read_text(encoding="utf-8")
    assert queue.count("- [category recipe-book]") == 1


def test_category_publish_does_not_requeue_a_category_declined_after_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import vault_index
    from ciao import vocabulary_proposals as vp

    vault = _vault(tmp_path)
    _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    lease = cr.begin_run(vault, holder="nightly", ttl_s=600)
    real_scan = vault_index.scan_vault

    def scan_then_decline(*args: object, **kwargs: object):
        result = real_scan(*args, **kwargs)
        vp.write_category_sidecar(
            vault,
            {
                "id": "recipe-book",
                "label": "Recipe book",
                "folder": "Recipe books",
                "description": "Recipes",
                "source_type": "recipe-book",
                "paths": [],
                "declined": True,
            },
        )
        return result

    monkeypatch.setattr(vault_index, "scan_vault", scan_then_decline)

    assert cr._category_cluster_items(vault, holder=lease.holder, registry_root=vault) == []
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()
    assert vp.read_category_sidecar(vault, "recipe-book")["declined"] is True


def test_a_two_note_cluster_is_not_category_work(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _cluster_vault(vault, "recipe-book", 2)

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()


def test_guide_review_is_weekly_but_not_a_required_hygiene_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guide-body pass rides the weekly marker, not the vault-hygiene gate.

    The guide review is a model judgment that `curation-begin` cannot score
    mechanically, so it shows up whenever the weekly pass is due. But it must
    not be one of the two keys that gate `last_full_pass`: an over-budget run
    that never reached the guide must not be told it completed the review it
    skipped. Recording it should also keep next week's guide pass due.
    """
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")

    by_pass = {
        item.pass_id: item
        for item in cr.build_worklist(
            vault_root=vault,
            workspace=vault.name,
            guide_path=guide,
            category_registry=_categories(vault),
            workspace_dir=tmp_path,
            today=date(2026, 9, 19),
        ).items
    }
    assert by_pass[cr.PASS_GUIDE].weekly is True
    assert by_pass[cr.PASS_GUIDE].keys == (cr.item_key(cr.PASS_GUIDE, "guide-body"),)
    guide_keys = {key for item in by_pass.values() if item.pass_id == cr.PASS_GUIDE for key in item.keys}
    assert guide_keys.isdisjoint(cr.REQUIRED_HYGIENE_KEYS)
    assert cr.PASS_GUIDE in cr.PASS_ORDER

    lease = cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, [cr.item_key(cr.PASS_GUIDE, "guide-body")])
    _capture(monkeypatch)
    _curation_end_command(_args(tmp_path, vault, guide, holder=lease.holder, status="ok"))

    # Recording only the guide review never advances the weekly marker.
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"


def test_unpredicted_region_facts_are_predict_work(tmp_path: Path) -> None:
    """`[memory]`/`[profile]` rows with no verdict on file are work: to predict one.

    They are still never acted on at handover off; the run only records what it
    would do, so the operator's agreement number has something to measure.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [memory] The user prefers uv over pip.\n- [profile] Writes in British English.\n",
        encoding="utf-8", newline=""
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert not worklist.empty
    proposals = [item for item in worklist.items if item.pass_id == cr.PASS_PROPOSALS]
    assert len(proposals) == 1
    assert len(proposals[0].keys) == 2


def test_proposals_sharing_one_sentence_are_separate_work(tmp_path: Path) -> None:
    """Acceptance: finishing one row must not retire the rows that read alike.

    The work-item key hashed the bullet text alone, so two actionable rows with
    identical text — a different kind, a different destination, or a plain
    duplicate — collided. Completing the first before a failure or a budget
    boundary persisted that one key, and the next `build_worklist` filtered out
    every remaining row sharing it: an empty queue reported with unprocessed
    proposals still in the file.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [people Ada] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n",
        encoding="utf-8", newline=""
    )

    def plan() -> cr.Worklist:
        return cr.build_worklist(
            vault_root=vault,
            workspace=vault.name,
            guide_path=guide,
            category_registry=_categories(vault),
            workspace_dir=tmp_path,
            today=date(2026, 9, 19),
            done_keys=frozenset(cr.load_state(vault).done_keys),
        )

    first = plan()
    keys = first.items[0].keys
    assert first.items[0].pass_id == cr.PASS_PROPOSALS
    # Three rows, three identities: kind, destination and the duplicate ordinal
    # all separate them even though the sentence is one and the same.
    assert len(set(keys)) == 3

    cr.record_done(vault, [keys[0]])
    remaining = plan()

    assert not remaining.empty
    assert remaining.items[0].keys == keys[1:]


def test_a_missing_or_malformed_marker_leaves_the_weekly_pass_due(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == ""
    _fresh_log(vault, last_full_pass="not-a-date")
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == ""

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert worklist.weekly_due is True


def test_a_region_over_the_consolidation_threshold_is_work(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    guide = _guide(tmp_path, memory="The user ships on Fridays.")

    clear = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert clear.empty

    full = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
        memory_char_limit=30,
    )
    assert [item.pass_id for item in full.items] == [cr.PASS_REGIONS]
    assert "ciao:memory" in full.items[0].reason


def test_an_expired_entry_is_work_even_well_under_cap(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    guide = _guide(tmp_path, memory="Standup is at 9. [expires: 2026-01-01]")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert [item.pass_id for item in worklist.items] == [cr.PASS_REGIONS]
    assert "expired" in worklist.items[0].reason


def test_resolved_learnings_are_not_replanned(tmp_path: Path) -> None:
    """A promoted learning must not be promoted again the next night."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        "# Learnings\n\n## Active\n\n"
        "## Promoted / Resolved\n"
        "- [pin-the-node-version] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
        encoding="utf-8", newline=""
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.empty


# ── The stale-note pass ────────────────────────────────────────────────────


def _note(vault: Path, relative: str, *, updated: str = "2024-01-05", type_: str = "person") -> Path:
    """One content note in the vault, with the frontmatter the detector reads."""
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {type_}\nupdated: {updated}\n---\n\n# {path.stem}\n\nSomething durable.\n",
        encoding="utf-8", newline=""
    )
    return path


def _stale_items(vault: Path, guide: Path, today: date = date(2026, 9, 19), **kwargs) -> list[cr.WorklistItem]:
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
        **kwargs,
    )
    return [item for item in worklist.items if item.pass_id == cr.PASS_STALE_NOTE]


def test_a_stale_note_is_work_keyed_by_its_vault_relative_path(tmp_path: Path) -> None:
    """Acceptance: the selection is deterministic and keyed by the path the
    check state, the receipts and the note-edit sidecar all key on.

    Not the rendered `memory-vault/…` path the scan produces: a key that carried
    the render prefix would not match the `relative_path` every reader of a note
    uses, so finishing the item would not suppress it.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    note = _note(vault, "People/Sofia.md", updated="2024-01-05")

    items = _stale_items(vault, guide)

    assert len(items) == 1
    assert items[0].keys == (cr.item_key(cr.PASS_STALE_NOTE, "People/Sofia.md"),)
    assert items[0].weekly is False
    assert items[0].label == "Sofia"
    # The reason names the age and the horizon it was measured against, so a
    # reader can disagree with the verdict without losing the evidence — and the
    # note's own `content_revision`, which is the `expected_revision` the
    # managed operation insists on. Without it here the only way to obtain one is
    # to reimplement the hash, and a caller that guesses it gets `conflict` for
    # every note forever.
    age = (date(2026, 9, 19) - date(2024, 1, 5)).days
    assert items[0].reason == (
        f"unverified for {age}d against a 30d horizon; "
        f"revision {mr.content_revision(note.read_text(encoding='utf-8'))}"
    )


def test_the_stale_pass_uses_the_shared_audit_predicate(tmp_path: Path) -> None:
    """Exempt types, notes with no usable date, and the queue's own exclusions
    are NOT work — and they are not work because `find_stale_notes` and
    `vault_review.never_queued` said so, not because this pass re-decided.

    An exempt `journal` is the case worth pinning: a log from two years ago is
    exactly as true as the day it was written, and a pass that flagged it would
    send the nightly run to re-verify a record that cannot go stale. `Workspace/`
    is the other: a note the review queue would never show a person is not a
    question to put in tonight's plan, and a list that counts it while the
    surface it sends you to can never show it is two lists disagreeing.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "journal/2024-01-05-standup.md", type_="journal")
    _note(vault, "Workspace/Notes.md", type_="workspace")
    _note(vault, "projects/completed/old.md", type_="project")
    _note(vault, "People/NoDate.md", updated="not-a-date")
    _note(vault, "People/Fresh.md", updated="2026-09-18")

    assert _stale_items(vault, guide) == []

    _note(vault, "People/Sofia.md", updated="2024-01-05")
    planned = {item.keys[0] for item in _stale_items(vault, guide)}
    assert planned == {cr.item_key(cr.PASS_STALE_NOTE, "People/Sofia.md")}


def test_a_retired_stale_after_days_key_has_no_effect_on_the_plan(tmp_path: Path) -> None:
    """An owner file that still says `stale_after_days` loads, and the key does nothing.

    The horizon is one constant for every note, so a 1000-day override on
    `person` cannot hide a note that is 110 days unverified: it is planned exactly
    as it would be with no file at all.
    """
    from ciao import entity_types

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Sofia.md", updated="2026-06-01")
    assert len(_stale_items(vault, guide)) == 1

    (vault / entity_types.VAULT_FILENAME).write_text(
        "- id: person\n"
        "  label: Person\n"
        "  kind: entity\n"
        "  folder: People\n"
        "  stale_after_days: 1000\n",
        encoding="utf-8", newline=""
    )
    entity_types.clear_entity_types_cache()
    try:
        assert len(_stale_items(vault, guide)) == 1
    finally:
        entity_types.clear_entity_types_cache()


def test_stale_notes_are_planned_oldest_first(tmp_path: Path) -> None:
    """Acceptance: a short budget must drop the youngest stale note, not an
    arbitrary one.

    Deterministic because the order is inherited from `find_stale_notes` (age
    descending, then path) rather than re-sorted: the note that has gone longest
    without a check is the one whose facts are most likely to have changed.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Youngest.md", updated="2026-06-01")
    _note(vault, "People/Middle.md", updated="2025-06-01")
    _note(vault, "People/Oldest.md", updated="2024-01-05")

    items = _stale_items(vault, guide)

    assert [item.label for item in items] == ["Oldest", "Middle", "Youngest"]
    assert [item.keys[0] for item in items] == [
        cr.item_key(cr.PASS_STALE_NOTE, f"People/{name}.md")
        for name in ("Oldest", "Middle", "Youngest")
    ]
    # And a budget of two takes the two oldest, deferring the rest whole.
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=date(2026, 9, 19),
    )
    plan = cr.plan_run(worklist, cr.RunBudget(max_items=2))
    planned = [key for item in plan.planned for key in item.keys]
    assert planned == [
        cr.item_key(cr.PASS_STALE_NOTE, "People/Oldest.md"),
        cr.item_key(cr.PASS_STALE_NOTE, "People/Middle.md"),
    ]


def test_a_stale_note_item_names_its_exact_target(tmp_path: Path) -> None:
    """The planner carries the note's vault-relative path and the revision the
    evidence check compares against, and the serialized item does not change."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    note = _note(vault, "People/Sofia.md", updated="2024-01-05")

    items = _stale_items(vault, guide)

    assert items[0].target == cr.StaleNoteTarget(
        relative_path="People/Sofia.md",
        revision=mr.content_revision(note.read_text(encoding="utf-8")),
    )
    assert set(items[0].as_dict()) == {"pass", "label", "reason", "count", "keys", "weekly"}


def test_the_target_survives_planning_and_leaves_order_and_budget_alone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Youngest.md", updated="2026-06-01")
    _note(vault, "People/Middle.md", updated="2025-06-01")
    _note(vault, "People/Oldest.md", updated="2024-01-05")
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=date(2026, 9, 19),
    )

    plan = cr.plan_run(worklist, cr.RunBudget(max_items=2))

    planned = [item for item in plan.planned if item.pass_id == cr.PASS_STALE_NOTE]
    deferred = [item for item in plan.deferred if item.pass_id == cr.PASS_STALE_NOTE]
    assert [item.target.relative_path for item in planned] == [
        "People/Oldest.md",
        "People/Middle.md",
    ]
    assert [item.target.relative_path for item in deferred] == ["People/Youngest.md"]
    original = {item.keys[0]: item for item in worklist.items if item.pass_id == cr.PASS_STALE_NOTE}
    for item in planned + deferred:
        assert item.as_dict() == original[item.keys[0]].as_dict()


def test_a_settled_stale_note_is_not_planned_again(tmp_path: Path) -> None:
    """The run cursor applies to this pass like every other.

    A `still_valid` re-stamp moves the note's `updated:` to today, so the next
    night's scan would not list it anyway — but an `unverified` verdict leaves
    the note exactly as it was, and without the cursor every following night
    would re-ask the same question the cooldown had already answered.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Sofia.md", updated="2024-01-05")
    key = cr.item_key(cr.PASS_STALE_NOTE, "People/Sofia.md")

    assert _stale_items(vault, guide)
    assert _stale_items(vault, guide, done_keys=frozenset({key})) == []
    # A key from another pass does not suppress this one.
    assert len(_stale_items(vault, guide, done_keys=frozenset({"audit:x"}))) == 1


def test_the_stale_pass_is_not_a_weekly_hygiene_key(tmp_path: Path) -> None:
    """A note goes stale on its own clock, not the marker's.

    Putting `PASS_STALE_NOTE` in `REQUIRED_HYGIENE_KEYS` would make a nightly
    run that skipped it block `last_full_pass` for a week, and a run that
    completed it advance a marker that gates the index refresh and the audit
    instead. It sits after `PASS_AUDIT` — the same "verify, don't guess" family —
    and is due whenever a note is.
    """
    assert cr.PASS_STALE_NOTE in cr.PASS_ORDER
    assert cr.PASS_ORDER.index(cr.PASS_STALE_NOTE) == cr.PASS_ORDER.index(cr.PASS_AUDIT) + 1
    assert cr.PASS_STALE_NOTE not in cr.REQUIRED_HYGIENE_KEYS
    assert all(
        not key.startswith(f"{cr.PASS_STALE_NOTE}:") for key in cr.REQUIRED_HYGIENE_KEYS
    )


def test_the_stale_pass_reads_no_note_body_and_makes_no_verdict(tmp_path: Path) -> None:
    """The cheap, model-free half.

    The pass is a selection, so it must be computable without a model turn and
    must not decide anything: selecting a note says its facts have gone
    unverified, nothing about whether they still hold. The verdict belongs to
    the managed `verify_note` operation, which writes a receipt, a check and —
    when the rule refuses — a proposal.

    The one thing it does read is the flagged notes' own bytes, to state the
    `content_revision` the operation needs. It reads the *notes it already
    selected* rather than the vault, so the selection itself stays as cheap as
    the scan that produced it.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Sofia.md", updated="2024-01-05")

    items = _stale_items(vault, guide)

    assert len(items) == 1
    # Nothing was written: no check state, no queue row, no sidecar.
    assert not (vault / "Workspace" / "Note-Checks.json").exists()
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()
    assert not (vault / "Workspace" / "Memory-Note-Edit-Proposals").exists()


def test_a_note_a_check_already_settles_is_not_planned_again(tmp_path: Path) -> None:
    """The failure this fixes: a checked-but-still-stale note, asked every night.

    The audit measures `updated:` against the horizon and has never heard of the
    check state, so it lists a note whose verdict came back `unverified` (nothing
    written, `updated:` exactly where it was) again tomorrow. With no cooldown
    consulted the same cooled-down notes take the first slots every night, each
    one comes back `already_checked`, and the rest of the backlog never gets
    asked at all.

    Two shapes of "already answered", both suppressing:

    * a plain check in its 30-day cooldown — the note has not changed since, so
      re-asking would only produce the same `unverified`;
    * a check pinned to a pending `note_edit` proposal — a person is already
      looking at that question, whatever the cooldown says.

    And a note that *did* change is planned again regardless: the revision no
    longer matches, so it carries new claims nobody verified.
    """
    from ciao import note_verification as nv

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    cooled = _note(vault, "People/Cooled.md", updated="2024-01-05")
    pinned = _note(vault, "People/Pinned.md", updated="2024-01-05")
    _note(vault, "People/Fresh.md", updated="2024-01-05")

    def _check(note: Path, **overrides) -> nv.NoteCheck:
        fields = {
            "relative_path": note.relative_to(vault).as_posix(),
            "content_revision": mr.content_revision(note.read_text(encoding="utf-8")),
            "outcome": nv.UNVERIFIED,
            "checked_at": today,
            "retry_after": today + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
            "reason": "no source this pass reached",
        }
        fields.update(overrides)
        return nv.NoteCheck(**fields)

    # Nothing recorded: both notes are planned, so the filter is what changes.
    assert len(_stale_items(vault, guide, today=today)) == 3

    nv.record_note_check(vault, _check(cooled))
    planned = {item.label for item in _stale_items(vault, guide, today=today)}
    assert planned == {"Pinned", "Fresh"}

    nv.record_note_check(
        vault,
        _check(pinned, outcome=nv.RETIRE, proposal_id="prop-1", retry_after=today),
    )
    assert [item.label for item in _stale_items(vault, guide, today=today)] == ["Fresh"]

    # A note that changed since its check carries new claims: planned again,
    # because that is the only thing that makes the cooldown expire.
    pinned.write_text(
        pinned.read_text(encoding="utf-8").replace("Something durable", "Something newer"),
        encoding="utf-8", newline=""
    )
    assert {item.label for item in _stale_items(vault, guide, today=today)} == {
        "Fresh",
        "Pinned",
    }

    # And a note the cooldown holds is reported as held, not as a queue of one:
    # a nightly run that planned it anyway would look like the pass working.
    nv.record_note_check(vault, _check(cooled))
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
    )
    assert any("cooldown" in note for note in worklist.notes)


def test_a_stale_backlog_cannot_starve_the_required_hygiene_keys(tmp_path: Path) -> None:
    """A vault that has never been verified must still get its weekly care.

    The pass sits ahead of the hygiene keys and the budget is a whole-run
    allowance, so an uncapped backlog spends it: 25 stale notes planned, the two
    required checks never reached, `last_full_pass` unable to advance, and the
    backlog unchanged the next night. A workspace one large migration away from
    being verified would never be verified at all.

    So the pass is capped, oldest first, and the cap is applied *after* the
    cooldown filter — capping first would be the same starvation with a smaller
    constant, since the cooled-down note that is still the oldest would consume a
    slot every night.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    # No marker, so the weekly pass is due and the hygiene keys are in play.
    for index in range(cr.STALE_NOTE_MAX_ITEMS * 8):
        _note(vault, f"People/Note{index:02d}.md", updated="2024-01-05")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
    )
    plan = cr.plan_run(worklist)

    planned = {key for item in plan.planned for key in item.keys}
    assert cr.REQUIRED_HYGIENE_KEYS <= planned
    assert sum(item.count for item in plan.planned if item.pass_id == cr.PASS_STALE_NOTE) == (
        cr.STALE_NOTE_MAX_ITEMS
    )
    # Oldest first, so the note that has gone longest unanswered goes first; with
    # identical `updated:` stamps that is the order the path breaks the tie.
    stale = [item for item in plan.planned if item.pass_id == cr.PASS_STALE_NOTE]
    assert [item.label for item in stale] == sorted(item.label for item in stale)
    # And the run says what it left out rather than reporting a queue of five.
    assert any("wait for the next run" in note for note in worklist.notes)


def test_the_stale_cap_fills_with_notes_that_are_actually_due(tmp_path: Path) -> None:
    """The cap counts work, not candidates.

    Cooled-down notes are filtered before the cap, so a run against a vault where
    the oldest notes were all checked last week still plans a full night's work
    from the notes behind them. Capping first would leave the pass planning one
    note a night and returning `already_checked` for it.
    """
    from ciao import note_verification as nv

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(cr.STALE_NOTE_MAX_ITEMS):
        note = _note(vault, f"People/Old{index:02d}.md", updated="2024-01-05")
        nv.record_note_check(
            vault,
            nv.NoteCheck(
                relative_path=note.relative_to(vault).as_posix(),
                content_revision=mr.content_revision(note.read_text(encoding="utf-8")),
                outcome=nv.UNVERIFIED,
                checked_at=today,
                retry_after=today + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
            ),
        )
    for index in range(cr.STALE_NOTE_MAX_ITEMS):
        _note(vault, f"People/New{index:02d}.md", updated="2024-01-05")

    items = _stale_items(vault, guide, today=today)

    assert [item.label for item in items] == [f"New{index:02d}" for index in range(cr.STALE_NOTE_MAX_ITEMS)]


def test_the_stale_note_cap_is_twenty_and_the_rest_are_reported(tmp_path: Path) -> None:
    """Twenty and not five: 25 due notes plan 20, and coverage counts the rest."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(25):
        _note(vault, f"People/Note{index:02d}.md", updated="2024-01-05")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
    )
    plan = cr.plan_run(worklist)

    assert cr.STALE_NOTE_MAX_ITEMS == 20
    assert sum(i.count for i in plan.planned if i.pass_id == cr.PASS_STALE_NOTE) == 20
    coverage = worklist.note_coverage
    assert coverage is not None
    assert coverage.cap == 20
    assert coverage.flagged == 25
    assert coverage.planned == 20
    assert coverage.eligible_deferred == 1
    assert coverage.unexamined == 4
    assert coverage.status == "partial"
    assert any("wait for the next run" in note for note in worklist.notes)


def test_a_full_stale_backlog_in_both_passes_leaves_the_required_keys_planned(
    tmp_path: Path,
) -> None:
    """The two stale passes together can fill their caps (20 notes, 5 facts) and
    the weekly hygiene keys still fit in the same plan.

    Before the note cap rose, the two passes together could take at most 10 of the
    25-item budget. At 20 + 5 they take all of it, so the budget has to grow by
    the same 15 or the required checks are never reached.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    for index in range(25):
        _note(vault, f"People/Stale{index:02d}.md", updated="2024-01-05")
    _entry_note(
        vault,
        "People/Sofia.md",
        updated="2024-01-05",
        facts=tuple(
            (f"Fact number {index} about Sofia", "2024-01-05")
            for index in range(cr.STALE_ENTRY_MAX_ITEMS * 2)
        ),
    )

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=TODAY_ENTRIES,
    )
    plan = cr.plan_run(worklist)

    planned = {key for item in plan.planned for key in item.keys}
    assert cr.REQUIRED_HYGIENE_KEYS <= planned
    assert sum(i.count for i in plan.planned if i.pass_id == cr.PASS_STALE_NOTE) == 20
    assert sum(i.count for i in plan.planned if i.pass_id == cr.PASS_STALE_ENTRY) == 5


def test_a_missing_vault_is_not_a_failed_plan(tmp_path: Path) -> None:
    """The pass is advisory: a vault that is not there plans nothing rather
    than raising out of `curation-begin` and costing the run its other passes."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=tmp_path / "no-such-vault",
        workspace="no-such-vault",
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_STALE_NOTE not in {item.pass_id for item in worklist.items}


# ── Budget and cursor ─────────────────────────────────────────────────────


def test_mixed_workload_planning_baseline(tmp_path: Path) -> None:
    """A frozen mixed workload through the canonical worklist and budget split.

    The reproducible reference for planning a night's care: one synthetic vault,
    one frozen date, the whole plan. It is a deterministic baseline, not a
    measurement of token use or semantic coverage — three queued proposals at a
    two-item budget boundary, with the weekly checks deferred behind them.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [people Ada] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n"
        "- [project docs/Release.md] Ships on Fridays.\n",
        encoding="utf-8", newline=""
    )

    def build() -> cr.Worklist:
        return cr.build_worklist(
            vault_root=vault,
            workspace=vault.name,
            guide_path=guide,
            category_registry=_categories(vault),
            workspace_dir=tmp_path,
            today=date(2026, 9, 19),
        )

    # Literal identities, not the plan's own output read back as an expectation:
    # a `proposals:` key hashes the kind, the destination, the source and the
    # duplicate ordinal along with the text, so the two byte-identical rows
    # differ only in that ordinal. A change to any of them is a deliberate
    # re-baseline, not a test that agrees with whatever the planner now does.
    ada = "proposals:2618165c48d8"
    release = "proposals:af3d962c93b1"
    release_duplicate = "proposals:5e7e5eccfcd7"
    guide_review = "guide:a22c020177e7"
    expected_items = [
        (cr.PASS_PROPOSALS, (ada, release, release_duplicate)),
        (cr.PASS_HYGIENE, (cr.HYGIENE_INDEX_KEY, cr.HYGIENE_AUDIT_KEY)),
        (cr.PASS_GUIDE, (guide_review,)),
    ]
    expected_planned = [(cr.PASS_PROPOSALS, (ada, release))]
    expected_deferred = [
        (cr.PASS_PROPOSALS, (release_duplicate,)),
        (cr.PASS_HYGIENE, (cr.HYGIENE_INDEX_KEY, cr.HYGIENE_AUDIT_KEY)),
        (cr.PASS_GUIDE, (guide_review,)),
    ]

    def planned_and_deferred(
        worklist: cr.Worklist,
    ) -> tuple[list[tuple[str, tuple[str, ...]]], list[tuple[str, tuple[str, ...]]]]:
        plan = cr.plan_run(worklist, cr.RunBudget(max_items=2))
        return (
            [(item.pass_id, item.keys) for item in plan.planned],
            [(item.pass_id, item.keys) for item in plan.deferred],
        )

    worklist = build()

    assert worklist.weekly_due is True
    # The whole pass sequence, in `PASS_ORDER`, with every key: three proposals
    # first, then both weekly checks, then the weekly guide review.
    assert [(item.pass_id, item.keys) for item in worklist.items] == expected_items

    planned, deferred = planned_and_deferred(worklist)
    assert planned == expected_planned
    # The third proposal waits whole, then both required checks, then the guide:
    # the budget boundary never reorders the tail behind it.
    assert deferred == expected_deferred

    # An unmodified rebuild plans the same night: same passes, same keys, same
    # partition. `generated_at` is deliberately not compared — it is a clock.
    again = build()
    assert again.weekly_due is True
    assert [(item.pass_id, item.keys) for item in again.items] == expected_items
    assert planned_and_deferred(again) == (expected_planned, expected_deferred)


def _worklist_of(*counts: int) -> cr.Worklist:
    items = tuple(
        cr.WorklistItem(
            pass_id=pass_id,
            label=pass_id,
            reason="test",
            keys=tuple(f"{pass_id}:{index}" for index in range(count)),
        )
        for pass_id, count in zip(cr.PASS_ORDER, counts)
        if count
    )
    return cr.Worklist(items=items, weekly_due=False, last_full_pass="", generated_at="")


def test_the_budget_splits_a_pass_instead_of_deferring_it_whole() -> None:
    """A 200-item queue must make progress every night, not stall forever."""
    plan = cr.plan_run(_worklist_of(10), cr.RunBudget(max_items=4))

    assert plan.planned_count == 4
    assert plan.deferred_count == 6
    assert plan.planned[0].keys == ("proposals:0", "proposals:1", "proposals:2", "proposals:3")
    assert plan.deferred[0].keys[0] == "proposals:4"
    assert "resumes next run" in plan.deferred[0].reason


def test_the_budget_spends_passes_in_order() -> None:
    plan = cr.plan_run(_worklist_of(2, 2, 2), cr.RunBudget(max_items=3))

    assert [item.pass_id for item in plan.planned] == [cr.PASS_PROPOSALS, cr.PASS_REGIONS]
    assert [item.pass_id for item in plan.deferred] == [cr.PASS_REGIONS, cr.PASS_AUDIT]


def _settled_proposal(vault: Path, name: str) -> None:
    (vault / cr.SKILL_PROPOSALS_RELATIVE / f"{name}.md").write_text(
        f"---\nschema: 1\ntype: skill-proposal\nlifecycle: dismissed\n---\n\n# {name}\n",
        encoding="utf-8", newline=""
    )


def test_a_settled_skill_proposal_is_no_longer_waiting_on_a_decision(tmp_path: Path) -> None:
    """A dismissal stays on disk, so the pass must ask about the queue, not glob it.

    Review round 1 (#683): counting every ``*.md`` kept re-reporting an answered
    decision every night, which is the same "evidence immediately reappears"
    outcome settle-not-unlink was meant to end.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    _settled_proposal(vault, "web-research")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_SKILL_PROPOSALS not in {item.pass_id for item in worklist.items}


def test_a_pending_skill_proposal_beside_a_settled_one_still_counts(
    tmp_path: Path,
) -> None:
    """One open record is enough, and the keys name only that record."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    _settled_proposal(vault, "web-research")
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "deploy.md").write_text("x", encoding="utf-8", newline="")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    item = next(i for i in worklist.items if i.pass_id == cr.PASS_SKILL_PROPOSALS)
    assert item.keys == (cr.item_key(cr.PASS_SKILL_PROPOSALS, "deploy.md"),)
    assert "Report skill proposal deploy.md" in item.label
    assert "waiting on a decision" in item.reason


def test_a_budget_limited_run_resumes_at_the_remainder(tmp_path: Path) -> None:
    """Acceptance: no duplicate work and no lost items across two runs."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    for name in ("a", "b", "c"):
        (vault / cr.SKILL_PROPOSALS_RELATIVE / f"{name}.md").write_text("x", encoding="utf-8", newline="")

    first = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    plan = cr.plan_run(first, cr.RunBudget(max_items=2))
    assert plan.planned_count == 2 and plan.deferred_count == 1
    done = [key for item in plan.planned for key in item.keys]
    cr.record_done(vault, done)

    second = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 20),
        done_keys=frozenset(cr.load_state(vault).done_keys),
    )
    remaining = [key for item in second.items for key in item.keys]

    # Nothing repeated, nothing lost: the two runs together cover all three.
    assert set(remaining).isdisjoint(done)
    assert sorted(remaining + done) == sorted(
        key for item in first.items for key in item.keys
    )


def test_a_worklist_whose_every_key_is_done_reports_empty(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "a.md").write_text("x", encoding="utf-8", newline="")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    cr.record_done(vault, [key for item in worklist.items for key in item.keys])

    again = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
        done_keys=frozenset(cr.load_state(vault).done_keys),
    )
    assert again.empty


def test_end_run_prunes_keys_whose_work_has_left_the_vault(tmp_path: Path) -> None:
    """An unbounded cursor would also suppress a later item with the same subject."""
    vault = _vault(tmp_path)
    cr.record_done(vault, ["proposals:gone", "proposals:live"])

    cr.end_run(vault, live_keys=frozenset({"proposals:live"}))

    assert set(cr.load_state(vault).done_keys) == {"proposals:live"}


def test_end_run_records_counts_so_a_no_op_is_not_a_failure(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = cr.end_run(
        vault, status="failed", planned=5, completed=2, deferred=3, reasons=["os-audit exit 2"]
    )

    assert summary["status"] == "failed"
    assert (summary["planned"], summary["completed"], summary["deferred"]) == (5, 2, 3)
    assert cr.load_state(vault).last_run["reasons"] == ["os-audit exit 2"]


# ── Lease ─────────────────────────────────────────────────────────────────


def test_a_second_curation_run_is_refused_while_the_first_holds_the_lease(
    tmp_path: Path,
) -> None:
    """Acceptance: the contended path, not just the happy one."""
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run-a")

    with pytest.raises(cr.CurationBusy) as excinfo:
        cr.begin_run(vault, holder="run-b")

    assert "run-a" in str(excinfo.value)


def test_the_lease_is_free_again_once_the_first_run_ends(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="run-a")
    cr.end_run(vault, holder=lease.holder)

    second = cr.begin_run(vault, holder="run-b")

    assert second.holder.startswith("run-b:")


def test_an_expired_lease_does_not_wedge_the_vault(tmp_path: Path) -> None:
    """A crashed run releases its lease by expiring; nothing else can."""
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="crashed", ttl_s=60, now=started)

    assert cr.active_lease(vault, now=started + timedelta(seconds=30)) is not None
    assert cr.active_lease(vault, now=started + timedelta(seconds=120)) is None
    cr.begin_run(vault, holder="next", now=started + timedelta(seconds=120))


def test_an_unreadable_expiry_fails_closed_without_overwriting_state(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run-a")
    path = cr.state_path(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lease"]["expires_at"] = "whenever"
    path.write_text(json.dumps(payload), encoding="utf-8", newline="")

    original = path.read_bytes()
    with pytest.raises(cr.CurationStateError):
        cr.active_lease(vault)
    with pytest.raises(cr.CurationStateError):
        cr.begin_run(vault, holder="run-b")
    assert path.read_bytes() == original


def test_a_run_that_lost_its_lease_cannot_keep_stamping_work_done(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    expired = cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)
    cr.begin_run(vault, holder="run-b", now=started + timedelta(seconds=120))

    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, ["proposals:x"], holder=expired.holder)
    assert cr.load_state(vault).done_keys == {}


def test_renew_extends_only_the_holders_own_lease(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    lease = cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)

    assert cr.renew_run(vault, holder="run-b", ttl_s=60, now=started) is False
    assert cr.renew_run(vault, holder=lease.holder, ttl_s=600, now=started) is True
    assert cr.active_lease(vault, now=started + timedelta(seconds=300)) is not None


# ── Did the dispatched run do its job? ─────────────────────────────────────


_DISPATCHED = datetime(2026, 9, 19, 0, 10, tzinfo=UTC)
_LATER = datetime(2026, 9, 19, 0, 20, tzinfo=UTC)


def test_a_vault_with_no_state_file_reports_an_unstarted_run(tmp_path: Path) -> None:
    """`ciao` off the agent's PATH leaves no state file at all (issue #866)."""
    vault = _vault(tmp_path)

    assert cr.unstarted_run_reason(vault, _DISPATCHED, now=_LATER) != ""
    assert "curation lease was never taken" in cr.unstarted_run_reason(
        vault, _DISPATCHED, now=_LATER
    )


def test_a_run_that_took_the_lease_counts_as_started(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="t", now=_LATER)

    assert cr.unstarted_run_reason(vault, _DISPATCHED, now=_LATER) == ""


def test_a_run_that_finished_counts_as_started(tmp_path: Path) -> None:
    """A crashed run leaves a lease; a completed one leaves `last_run`."""
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="t", now=_LATER)
    cr.end_run(vault, holder=lease.holder, now=_LATER)

    assert cr.unstarted_run_reason(vault, _DISPATCHED, now=_LATER) == ""


def test_a_stale_last_run_from_an_earlier_night_is_not_this_run(
    tmp_path: Path,
) -> None:
    """Yesterday's finished run must not vouch for tonight's dispatch."""
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="t")
    cr.end_run(vault, holder=lease.holder)
    path = cr.state_path(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lease"] = {}
    payload["last_run"] = {
        **payload["last_run"],
        "finished_at": "2026-09-18T23:00:00Z",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cr.unstarted_run_reason(vault, _DISPATCHED, now=_LATER) != ""


def test_standing_down_for_another_runs_live_lease_is_not_a_failure(
    tmp_path: Path,
) -> None:
    """Exit 75 means another run is doing the work, not that none was."""
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="other", ttl_s=1800, now=_DISPATCHED)

    assert cr.unstarted_run_reason(vault, _DISPATCHED, now=_LATER) == ""


# ── Weekly marker ─────────────────────────────────────────────────────────


def test_the_weekly_marker_advances_only_after_both_required_checks(
    tmp_path: Path,
) -> None:
    """Acceptance: an unreliable scan leaves the full pass due."""
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")

    cr.record_done(vault, [cr.HYGIENE_INDEX_KEY])
    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is False
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"

    cr.record_done(vault, [cr.HYGIENE_AUDIT_KEY])
    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is True
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-19"


def test_advancing_the_marker_keeps_the_existing_log_body(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        "---\nlast_full_pass: 2026-09-01\ntags: [ciao]\n---\n\n# Curation log\n\n## 2026-09-01\nDid things.\n",
        encoding="utf-8", newline=""
    )
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))

    cr.advance_full_pass(vault, today=date(2026, 9, 19))

    text = (vault / cr.CURATION_LOG_RELATIVE).read_text(encoding="utf-8")
    assert "tags: [ciao]" in text
    assert "Did things." in text
    assert text.count("last_full_pass") == 1


# ── CLI ───────────────────────────────────────────────────────────────────


def _args(tmp_path: Path, vault: Path, guide: Path, **overrides: object) -> object:
    import argparse

    base = dict(
        workspace=tmp_path,
        vault_root=vault,
        guide=guide,
        max_items=None,
        max_seconds=None,
        json=True,
        holder="",
        key=[],
        status="ok",
        planned=0,
        completed=0,
        reason=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    printed: list[str] = []
    monkeypatch.setattr("sys.stdout.write", lambda text: printed.append(text) or len(text))
    return printed


def test_curation_begin_exits_temp_fail_when_another_run_holds_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the scheduled run must be able to tell "busy" from "broken"."""
    from ciao.cli import CURATION_BUSY_EXIT, _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    cr.begin_run(vault, holder="run-a")

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="run-b"))

    assert code == CURATION_BUSY_EXIT


@pytest.mark.parametrize("command", ["plan", "begin", "progress", "end"])
def test_corrupt_state_care_commands_fail_without_success_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    from ciao.cli import (
        _curation_begin_command,
        _curation_end_command,
        _curation_plan_command,
        _curation_progress_command,
    )

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date.today().isoformat())
    path = cr.state_path(vault)
    path.write_text('{"version":', encoding="utf-8")
    before = path.read_bytes()
    args = _args(tmp_path, vault, guide, holder="run", key=["pass:item"])
    handlers = {
        "plan": _curation_plan_command,
        "begin": _curation_begin_command,
        "progress": _curation_progress_command,
        "end": _curation_end_command,
    }

    assert handlers[command](args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curation state error:" in captured.err
    assert "JSONDecodeError" not in captured.err
    assert path.read_bytes() == before


def test_parser_recursion_is_controlled_by_curation_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao.cli import _curation_plan_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    path = cr.state_path(vault)
    path.write_text(
        '{"version":1,"done_keys":{},"lease":{},"last_run":{},"x":'
        + "[" * 2000
        + "0"
        + "]" * 2000
        + "}",
        encoding="utf-8",
    )

    assert _curation_plan_command(_args(tmp_path, vault, guide)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curation state error:" in captured.err
    assert "RecursionError" not in captured.err


@pytest.mark.parametrize("failure_point", ["planning", "empty-cleanup", "final-check"])
def test_curation_begin_state_uncertainty_at_each_post_acquisition_seam_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_point: str,
) -> None:
    from types import SimpleNamespace

    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date.today().isoformat())
    path = cr.state_path(vault)
    monkeypatch.setattr(cr, "_category_cluster_items", lambda *_args, **_kwargs: [])

    def corrupt() -> None:
        path.write_text('{"version":', encoding="utf-8")

    def plan(*_args: object, **_kwargs: object):
        if failure_point in {"planning", "final-check"}:
            corrupt()
        if failure_point == "planning":
            raise cr.CurationStateError("controlled test uncertainty")
        return _planning_result({"empty": failure_point == "empty-cleanup"})

    monkeypatch.setattr("ciao.cli._curation_plan", plan)
    if failure_point == "empty-cleanup":
        def fail_cleanup(*_args: object, **_kwargs: object) -> object:
            raise cr.CurationStateError("controlled test uncertainty")

        monkeypatch.setattr(cr, "end_run", fail_cleanup)

    assert _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curation state error:" in captured.err
    if failure_point != "empty-cleanup":
        assert path.read_text(encoding="utf-8") == '{"version":'


def test_curation_begin_releases_the_lease_on_a_quiet_night(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with nothing to do must not sit on the lease."""
    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    # Relative to today, not a fixed date: the begin command reads
    # date.today(), so a hard-coded marker turned the weekly pass due (and the
    # run non-empty) once the calendar passed it by WEEKLY_PASS_DAYS.
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)

    code = _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly"))

    assert code == 0
    assert json.loads("".join(printed))["empty"] is True
    assert cr.active_lease(vault) is None


def test_empty_begin_reports_busy_if_its_lease_is_lost_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import CURATION_BUSY_EXIT, _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)
    def lose_lease(*args: object, **kwargs: object) -> object:
        raise cr.CurationBusy("lease expired during planning")

    monkeypatch.setattr(cr, "end_run", lose_lease)

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder="nightly")
    ) == CURATION_BUSY_EXIT
    assert not printed
    assert cr.active_lease(vault) is not None


@pytest.mark.parametrize("during_scan", ["expired", "replaced"])
def test_nonempty_begin_checks_ownership_after_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_scan: str
) -> None:
    from types import SimpleNamespace

    from ciao.cli import CURATION_BUSY_EXIT, _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)
    category_output = vault / "Workspace" / "category-created-during-begin.txt"

    def generate_category(*args: object, **kwargs: object) -> list[object]:
        category_output.parent.mkdir(parents=True, exist_ok=True)
        category_output.write_text("created before post-plan validation", encoding="utf-8")
        return []

    def replace_while_planning(*args: object, **kwargs: object):
        state = cr.load_state(vault)
        old = state.lease["holder"]
        if during_scan == "expired":
            state.lease["expires_at"] = (
                datetime.now(UTC) - timedelta(seconds=1)
            ).isoformat()
            cr._store(cr.state_path(vault), state)
        else:
            cr.end_run(vault, holder=old)
            cr.begin_run(vault, holder="replacement")
        return _planning_result({"empty": False})

    monkeypatch.setattr(cr, "_category_cluster_items", generate_category)
    monkeypatch.setattr("ciao.cli._curation_plan", replace_while_planning)

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder="nightly")
    ) == CURATION_BUSY_EXIT
    assert not printed
    assert category_output.read_text(encoding="utf-8") == "created before post-plan validation"
    active = cr.active_lease(vault)
    if during_scan == "expired":
        assert active is None
    else:
        assert (active or {})["holder"].startswith("replacement:")


def test_successful_nonempty_begin_check_does_not_extend_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)
    monkeypatch.setattr(cr, "_category_cluster_items", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "ciao.cli._curation_plan",
        lambda *_args, **_kwargs: _planning_result({"empty": False}),
    )

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder="nightly")
    ) == 0
    payload = json.loads("".join(printed))
    persisted = cr.load_state(vault).lease
    assert payload["lease"]["expires_at"] == persisted["expires_at"]


def test_progress_uses_atomic_record_done_ttl_without_separate_renew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    lease = cr.begin_run(vault, holder="nightly")
    _capture(monkeypatch)

    def record_done(*args: object, **kwargs: object) -> int:
        assert kwargs["holder"] == lease.holder
        assert kwargs["ttl_s"] == cr.RunBudget().max_seconds
        return 1

    def unexpected_renew(*args: object, **kwargs: object) -> bool:
        pytest.fail("progress must renew in the record_done transaction")

    monkeypatch.setattr(cr, "record_done", record_done)
    monkeypatch.setattr(cr, "renew_run", unexpected_renew)

    assert _curation_progress_command(
        _args(tmp_path, vault, guide, holder=lease.holder, key=["progress"])
    ) == 0


def test_rejected_progress_returns_busy_without_success_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import CURATION_BUSY_EXIT, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    lease = cr.begin_run(vault, holder="nightly")
    printed = _capture(monkeypatch)

    def reject(*args: object, **kwargs: object) -> int:
        raise cr.CurationBusy("lease expired")

    monkeypatch.setattr(cr, "record_done", reject)

    assert _curation_progress_command(
        _args(tmp_path, vault, guide, holder=lease.holder, key=["progress"])
    ) == CURATION_BUSY_EXIT
    assert printed == []


def test_cli_end_expired_unreplaced_holder_returns_busy_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    lease = cr.begin_run(vault, holder="nightly", ttl_s=3600)
    state = cr.load_state(vault)
    state.lease["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    cr._store(cr.state_path(vault), state)
    before_state = cr.state_path(vault).read_bytes()
    before_log = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()
    printed = _capture(monkeypatch)
    monkeypatch.setattr(
        "ciao.cli._curation_plan",
        lambda _args: _planning_result(
            {"deferred_count": 0, "remaining_observed_count": 0}
        ),
    )

    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=lease.holder)
    ) == CURATION_BUSY_EXIT
    assert printed == []
    assert cr.state_path(vault).read_bytes() == before_state
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == before_log


def test_cli_end_rejects_corruption_after_planning_without_advancing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    lease = cr.begin_run(vault, holder="nightly")
    state_path = cr.state_path(vault)
    log_path = vault / cr.CURATION_LOG_RELATIVE
    log_before = log_path.read_bytes()
    real_end = cr.end_run

    def corrupt_before_commit(*args: object, **kwargs: object) -> dict[str, object]:
        state_path.write_text('{"version":', encoding="utf-8")
        return real_end(*args, **kwargs)

    monkeypatch.setattr(cr, "end_run", corrupt_before_commit)
    monkeypatch.setattr(
        "ciao.cli._curation_plan",
        lambda _args: _planning_result(
            {"deferred_count": 0, "remaining_observed_count": 0}
        ),
    )

    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=lease.holder)
    ) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "curation state error:" in captured.err
    assert state_path.read_text(encoding="utf-8") == '{"version":'
    assert log_path.read_bytes() == log_before


def test_cli_end_replacement_during_planning_preserves_replacement_state_and_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    first = cr.begin_run(vault, holder="run-a")
    replacement = []
    state_and_marker_after_replacement = []
    printed = _capture(monkeypatch)

    def replace_during_plan(_args):
        cr.end_run(vault, holder=first.holder)
        replacement.append(cr.begin_run(vault, holder="run-b"))
        state_and_marker_after_replacement.append(
            (
                cr.state_path(vault).read_bytes(),
                (vault / cr.CURATION_LOG_RELATIVE).read_bytes(),
            )
        )
        return _planning_result({"deferred_count": 0, "remaining_observed_count": 0})

    monkeypatch.setattr("ciao.cli._curation_plan", replace_during_plan)
    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=first.holder, status="ok")
    ) == CURATION_BUSY_EXIT
    assert replacement
    assert len(state_and_marker_after_replacement) == 1
    state_before_end, marker_before_end = state_and_marker_after_replacement[0]
    assert cr.state_path(vault).read_bytes() == state_before_end
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == marker_before_end
    assert cr.load_state(vault).lease["holder"] == replacement[0].holder
    assert printed == []


def test_end_plans_without_holding_state_lock_and_rechecks_after_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    lease = cr.begin_run(vault, holder="nightly")
    _capture(monkeypatch)
    lock_held = False
    original_lock = cr._state_lock

    @contextmanager
    def tracking_lock(root):
        nonlocal lock_held
        with original_lock(root):
            lock_held = True
            try:
                yield
            finally:
                lock_held = False

    def expire_during_scan(args):
        assert not lock_held
        state = cr.load_state(vault)
        state.lease["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        cr._store(cr.state_path(vault), state)
        return _planning_result({"deferred_count": 0, "remaining_observed_count": 0})

    from types import SimpleNamespace

    monkeypatch.setattr(cr, "_state_lock", tracking_lock)
    monkeypatch.setattr("ciao.cli._curation_plan", expire_during_scan)

    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=lease.holder)
    ) == CURATION_BUSY_EXIT
    assert cr.load_state(vault).last_run == {}


def test_empty_text_begin_does_not_disclose_released_lease_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)
    label = "quiet-night-label"

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder=label, json=False)
    ) == 0

    text = "".join(printed)
    assert "No observed work remains" in text
    assert label not in text
    assert "lease.holder" not in text
    assert "Lease holder" not in text
    assert cr.active_lease(vault) is None


def test_cli_accepts_legacy_whitespace_holder_for_progress_and_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_end_command, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    holder = " legacy holder "
    cr._store(
        cr.state_path(vault),
        cr.CurationState(
            lease={
                "holder": holder,
                "started_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        ),
    )
    printed = _capture(monkeypatch)

    assert _curation_progress_command(
        _args(tmp_path, vault, guide, holder=holder, key=["legacy-progress"])
    ) == 0
    assert cr.load_state(vault).done_keys["legacy-progress"]
    assert cr.load_state(vault).lease["holder"] == holder

    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=holder, status="failed")
    ) == 0
    assert cr.load_state(vault).lease == {}
    assert "curation lease lost" not in "".join(printed)


def test_cli_reacquired_holder_works_and_original_label_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import (
        CURATION_BUSY_EXIT,
        _curation_end_command,
        _curation_progress_command,
    )

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    first = cr.begin_run(vault, holder="reused-label")
    cr.end_run(vault, holder=first.holder)
    replacement = cr.begin_run(vault, holder="reused-label")
    printed = _capture(monkeypatch)
    before = cr.state_path(vault).read_bytes()

    assert _curation_progress_command(
        _args(tmp_path, vault, guide, holder="reused-label", key=["wrong-holder"])
    ) == CURATION_BUSY_EXIT
    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder="reused-label", status="failed")
    ) == CURATION_BUSY_EXIT
    assert cr.state_path(vault).read_bytes() == before

    printed.clear()
    assert _curation_progress_command(
        _args(
            tmp_path,
            vault,
            guide,
            holder=replacement.holder,
            key=["right-holder"],
        )
    ) == 0
    assert cr.load_state(vault).done_keys["right-holder"]
    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=replacement.holder, status="failed")
    ) == 0
    assert cr.active_lease(vault) is None


def test_curation_end_leaves_the_weekly_pass_due_after_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    lease = cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder=lease.holder, status="failed"))

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"
    assert cr.active_lease(vault) is None


def test_curation_end_advances_the_marker_and_forgets_the_weekly_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping the hygiene keys would make next week's pass look already done."""
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    lease = cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder=lease.holder, status="ok"))

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) != "2026-09-01"
    assert set(cr.load_state(vault).done_keys) == set()


def _stale_holder_replaced(vault: Path) -> tuple[str, str]:
    """Leave `run-a`'s lease expired and `run-b` holding the vault."""
    expired = datetime.now(UTC) - timedelta(hours=2)
    old = cr.begin_run(vault, holder="run-a", ttl_s=60, now=expired)
    new = cr.begin_run(vault, holder="run-b")
    return old.holder, new.holder


def test_follow_up_commands_refuse_to_run_without_the_lease_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: an ownership check that is optional in practice is no lease.

    `record_done` and `end_run` skip their check when the holder is empty, and
    the stock workflow used to invoke both without one. An over-budget run
    therefore kept stamping work done after its lease expired, and its
    `curation-end` cleared the lease the *newer* run was holding — exactly the
    overlap the lease exists to prevent.
    """
    from ciao.cli import _curation_end_command, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _old_holder, new_holder = _stale_holder_replaced(vault)
    _capture(monkeypatch)

    progress = _curation_progress_command(
        _args(tmp_path, vault, guide, holder="", key=["proposals:deadbeef"])
    )

    # The stale run stamps nothing done: the next run must not be told that
    # items nobody verified were handled.
    assert cr.load_state(vault).done_keys == {}
    assert progress == 2

    ended = _curation_end_command(_args(tmp_path, vault, guide, holder=""))

    # And it cannot release the lease the newer run is holding.
    assert (cr.active_lease(vault) or {})["holder"] == new_holder
    assert ended == 2


def test_the_parser_will_not_let_a_follow_up_command_omit_the_holder() -> None:
    """The flag is required at parse time, not merely honoured when present."""
    from ciao.cli import build_parser

    parser = build_parser()
    for command in ("curation-progress", "curation-end"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])
        assert parser.parse_args([command, "--holder", "run-a"]).holder == "run-a"
        assert parser.parse_args([command, "--holder", "  run-a  "]).holder == "  run-a  "


def test_a_rejected_run_cannot_advance_the_weekly_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: the marker moves only for the run that still holds the lease.

    The marker used to be stamped before `end_run` checked ownership, so a run
    whose lease had expired — one `curation-end` then rejected with 75 —
    suppressed the weekly hygiene passes for the next seven days on its way
    out.
    """
    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    old_holder, new_holder = _stale_holder_replaced(vault)
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    code = _curation_end_command(
        _args(tmp_path, vault, guide, holder=old_holder, status="ok")
    )

    assert code == CURATION_BUSY_EXIT
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"
    # And the run it was rejected in favour of still holds the lease.
    assert (cr.active_lease(vault) or {})["holder"] == new_holder


def test_replaced_expired_holder_does_not_plan_category_proposals_before_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A category cluster must not make finalization mutate the vault before lease rejection."""
    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    _cluster_vault(vault, "recipe-book", 3)
    old_holder, new_holder = _stale_holder_replaced(vault)
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    _capture(monkeypatch)

    code = _curation_end_command(
        _args(tmp_path, vault, guide, holder=old_holder, status="ok")
    )

    assert code == CURATION_BUSY_EXIT
    assert cr.load_state(vault).lease["holder"] == new_holder
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before
    assert not (vault / "Workspace" / "Memory-Proposals.md").exists()


def test_curation_plan_skips_category_generation_and_reports_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_plan_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    before = {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    def unexpected_generation(*_args: object, **_kwargs: object) -> list[object]:
        pytest.fail("curation-plan must not generate category proposals")

    monkeypatch.setattr(cr, "_category_cluster_items", unexpected_generation)
    printed = _capture(monkeypatch)

    assert _curation_plan_command(_args(tmp_path, vault, guide)) == 0
    payload = json.loads("".join(printed))
    assert payload["counts_complete"] is False
    assert payload["remaining_observed_count"] == payload["planned_count"] + payload["deferred_count"]
    assert any("category generation was omitted" in note for note in payload["coverage_notes"])
    assert {p.relative_to(vault): p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before


def test_curation_plan_reports_positive_observed_work_with_no_budget_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_plan_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n- [review] Synthetic pending proposal.\n",
        encoding="utf-8",
    )
    printed = _capture(monkeypatch)

    _curation_plan_command(_args(tmp_path, vault, guide))
    payload = json.loads("".join(printed))
    assert payload["remaining_observed_count"] > 0
    assert payload["deferred_count"] == 0
    assert payload["counts_complete"] is False


def test_empty_text_plan_still_discloses_incomplete_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_plan_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    printed = _capture(monkeypatch)
    args = _args(tmp_path, vault, guide, json=False)

    _curation_plan_command(args)

    text = "".join(printed)
    assert "Coverage is incomplete" in text
    assert "Nothing to curate: every mechanical check is clear." not in text
    assert "category generation was omitted" in text


@pytest.mark.parametrize("with_cluster", [False, True])
def test_curation_begin_records_that_category_generation_was_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_cluster: bool
) -> None:
    from ciao.cli import _curation_begin_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    if with_cluster:
        _cluster_vault(vault, "recipe-book", 3)
    printed = _capture(monkeypatch)

    assert _curation_begin_command(_args(tmp_path, vault, guide, holder="nightly")) == 0
    payload = json.loads("".join(printed))

    if payload["empty"]:
        assert payload["lease"] == {}
    else:
        assert payload["lease"]["holder"] == cr.active_lease(vault)["holder"]
        assert payload["lease"]["holder"].startswith("nightly:")
    assert not any("category generation was omitted" in note for note in payload["coverage_notes"])
    if with_cluster:
        assert "- [category recipe-book]" in (vault / cr.PROPOSALS_RELATIVE).read_text(encoding="utf-8")
        assert cr.PASS_CATEGORIES in {item["pass"] for item in payload["items"]}
        assert cr.PASS_PROPOSALS in {item["pass"] for item in payload["items"]}
    else:
        assert not (vault / cr.PROPOSALS_RELATIVE).exists()


def test_text_begin_returns_exact_holder_usable_by_follow_ups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_begin_command, _curation_end_command, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n- [review] Synthetic pending proposal.\n",
        encoding="utf-8",
    )
    printed = _capture(monkeypatch)
    label = "  nightly: whitespace  "

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder=label, json=False)
    ) == 0
    output = "".join(printed)
    holder = cr.active_lease(vault)["holder"]
    assert holder.startswith(label + ":")
    assert f"Lease holder (use exactly for follow-ups): {holder}" in output
    printed.clear()

    assert _curation_progress_command(
        _args(tmp_path, vault, guide, holder=holder, key=["proposals:synthetic"])
    ) == 0
    # The key is intentionally synthetic; progress must still preserve identity
    # and never let the human label itself stand in for the returned credential.
    assert cr.load_state(vault).lease["holder"] == holder
    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=holder, status="failed")
    ) == 0


def test_whitespace_only_follow_up_holder_is_rejected(tmp_path: Path, capsys) -> None:
    from ciao.cli import _curation_holder

    assert _curation_holder(_args(tmp_path, tmp_path, tmp_path, holder=" \t ")) == ""
    assert "pass --holder" in capsys.readouterr().err


def test_end_prunes_progress_for_begin_category_absent_from_incomplete_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterize cursor pruning; an incomplete replan does not resolve queued work."""
    from ciao.cli import _curation_begin_command, _curation_end_command, _curation_progress_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=(date.today() - timedelta(days=1)).isoformat())
    _cluster_vault(vault, "recipe-book", 3)
    printed = _capture(monkeypatch)

    assert _curation_begin_command(
        _args(tmp_path, vault, guide, holder="nightly")
    ) == 0
    begin = json.loads("".join(printed))
    category_item = next(item for item in begin["items"] if item["pass"] == cr.PASS_CATEGORIES)
    category_key = category_item["keys"][0]
    printed.clear()

    assert _curation_progress_command(
        _args(
            tmp_path, vault, guide, holder=begin["lease"]["holder"],
            key=[category_key],
        )
    ) == 0
    assert category_key in cr.load_state(vault).done_keys
    printed.clear()

    assert _curation_end_command(
        _args(
            tmp_path, vault, guide, holder=begin["lease"]["holder"],
            status="failed",
        )
    ) == 0

    # End's replan omits category discovery, so end_run's existing live-key
    # pruning drops this cursor entry. The source cluster and proposal remain:
    # absence from the incomplete replan is not evidence of resolution.
    assert category_key not in cr.load_state(vault).done_keys
    assert "- [category recipe-book]" in (vault / cr.PROPOSALS_RELATIVE).read_text(encoding="utf-8")
    assert (vault / "Workspace" / "Memory-Category-Proposals" / "recipe-book.json").exists()


def test_curation_end_does_not_call_category_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import CURATION_BUSY_EXIT, _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    _cluster_vault(vault, "recipe-book", 3)
    old_holder, _new_holder = _stale_holder_replaced(vault)

    def unexpected_generation(*_args: object, **_kwargs: object) -> list[object]:
        pytest.fail("curation-end must not generate category proposals")

    monkeypatch.setattr(cr, "_category_cluster_items", unexpected_generation)
    _capture(monkeypatch)

    assert _curation_end_command(
        _args(tmp_path, vault, guide, holder=old_holder, status="ok")
    ) == CURATION_BUSY_EXIT


@pytest.mark.parametrize("as_json", [False, True])
def test_curation_end_reports_incomplete_observed_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n- [review] Synthetic pending proposal.\n",
        encoding="utf-8",
    )
    lease = cr.begin_run(vault, holder="nightly")
    printed = _capture(monkeypatch)
    args = _args(
        tmp_path, vault, guide, holder=lease.holder, status="failed", json=as_json
    )

    assert _curation_end_command(args) == 0
    output = "".join(printed)
    if as_json:
        summary = json.loads(output)
        assert summary["counts_complete"] is False
        assert summary["remaining_observed_count"] > 0
        assert any("discovery is not exhaustive" in note for note in summary["coverage_notes"])
        assert any("category generation was omitted" in note for note in summary["coverage_notes"])
    else:
        assert "coverage incomplete" in output.lower()
        assert "Observed work" in output
        assert "category generation was omitted" in output


# ── Shipped instructions ──────────────────────────────────────────────────


def _stock(relative: str) -> str:
    from importlib import resources

    return resources.files("ciao.stock").joinpath(relative).read_text(encoding="utf-8")


def _care_references() -> str:
    """Every curation reference file of the `ciao-memory` skill, concatenated (#1462)."""
    from importlib import resources

    refs = resources.files("ciao.stock").joinpath("skills", "ciao-memory", "references")
    return "\n\n".join(
        item.read_text(encoding="utf-8")
        for item in sorted(refs.iterdir(), key=lambda i: i.name)
        if item.name.endswith(".md")
    )


def test_the_schedule_starts_from_the_computed_worklist() -> None:
    """The agent must not re-derive by hand what pass 0 already decided."""
    skill = next(entry["prompt"] for entry in json.loads(_stock("schedules.json"))["schedules"] if entry["schedule_id"] == "system-memory-curation")
    skill = skill + "\n\n" + _care_references()

    assert "ciao curation-begin --json" in skill
    assert "ciao curation-progress" in skill
    assert "ciao curation-end" in skill
    # The three decisions the deterministic path takes away from the model.
    assert "Exit 75 means another run holds the lease" in skill
    assert '`"empty": true`' in skill
    assert "`deferred` is next run's work" in skill
    # The weekly marker is stamped by code, gated on both required checks.
    assert "--key hygiene:vault-index" in skill
    assert "--key hygiene:os-audit" in skill
    assert "You never write that marker by hand." in skill
    # Every follow-up command carries the holder pass 0 returned; without it
    # the ownership check is skipped and the lease serializes nothing.
    assert "ciao curation-progress --holder <lease.holder>" in skill
    assert "ciao curation-end --holder <lease.holder>" in skill
    assert skill.count("ciao curation-progress --key") == 0


def test_the_schedule_prompt_points_at_the_worklist_first() -> None:
    schedules = json.loads(_stock("schedules.json"))
    prompt = next(
        entry["prompt"]
        for entry in schedules["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )

    assert "ciao curation-begin --json" in prompt
    assert "one-line no-op" in prompt
    # The lease only serializes anything if the follow-up commands carry it.
    assert "`lease.holder`" in prompt and "--holder" in prompt
    assert "## 1. Process the proposals queue" in prompt


def test_the_schedule_verifies_stale_notes_through_the_managed_operation() -> None:
    """Acceptance: the agent no longer hand-edits stale notes.

    The prompt used to say "open each note and set frontmatter `updated:` to
    today" — a direct Markdown edit, with no receipt, no check state and no
    record of who decided, and no way to tell afterwards whether a note was
    verified or merely touched. The managed `verify_note` operation replaced it,
    and the prompt has to name the operation rather than the edit.
    """
    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )
    reverify = _stock("skills/ciao-memory/references/curation-pass-3-reverify.md")
    stale = reverify[reverify.index("## `stale_notes`") : reverify.index("## `stale_entry` items")]
    prompt = prompt + "\n\n" + reverify

    # The managed pass, and the operation that is the only way to settle a note.
    assert "`note verify`" in stale
    assert "stale_note" in stale
    assert "never by editing Markdown" in stale
    # The old instruction, in every wording it took, is gone.
    assert "set frontmatter `updated:` to today" not in prompt
    assert "open each note and re-verify its facts" not in stale
    # The verdict's outcomes are named, because the words decide what happens.
    for status in ("`applied`", "`needs_review`", "`unverified`", "`conflict`"):
        assert status in stale
    # A refused verdict is a proposal for a person, and the run says so rather
    # than routing around it.
    assert "note_edit` proposal" in stale
    assert "**What needs you**" in stale
    # Retirement is a human decision all the way down.
    assert "Never delete it unattended" in stale
    assert "a retirement is never applied by this pass at all" in stale
    # The pass's keys are recorded, so the next run resumes rather than re-asks.
    assert "ciao curation-progress --holder <lease.holder> --key" in stale
    # The queue's own signals still point at the managed pass rather than at a
    # hand edit.
    assert "`unverified` (facts unchecked past the 30-day review horizon" in prompt
    assert "re-verify rather than retire" in prompt


def test_the_memory_skill_routes_note_verification_through_the_operation() -> None:
    """The skill is what a curation run loads for durable-vault guidance, so
    leaving the direct-edit instruction there would undo the schedule's own."""
    skill = _stock("skills/ciao-memory/SKILL.md")

    assert "ciao note verify --payload-file" in skill
    assert "managed operation" in skill
    # The specific failure mode this replaces.
    assert "never a direct edit" in skill
    assert "leaves no receipt" in skill


def test_the_schedule_compares_a_stale_note_with_its_related_notes() -> None:
    reverify = _stock("skills/ciao-memory/references/curation-pass-3-reverify.md")
    stale = reverify[reverify.index("## `stale_notes`") : reverify.index("## `stale_entry` items")]

    assert "ciao note related" in stale
    assert "Reading a note is not checking it" in stale
    assert '"due": true' in stale
    assert "ciao memory-proposal-add --kind review --text-file" in stale
    assert "**What needs you**" in stale


def test_a_note_verified_outside_the_worklist_is_not_planned(tmp_path: Path) -> None:
    """Crediting a related note needs no engine change: a verdict for a note that
    was never a planned item records a check, and the next plan skips it."""
    from types import SimpleNamespace

    from ciao import note_verification as nv

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Aldo.md", updated="2024-01-05")
    other = _note(vault, "People/Bruno.md", updated="2024-01-05")
    assert {item.label for item in _stale_items(vault, guide, today=today)} == {"Aldo", "Bruno"}

    result = nv.verify_note(
        nv.VerificationRequest(
            workspace="personal",
            relative_path="People/Bruno.md",
            expected_revision=mr.content_revision(other.read_text(encoding="utf-8")),
            outcome=nv.UNVERIFIED,
            reason="no source this pass reached",
        ),
        vault_root=vault,
        config=SimpleNamespace(workspace_vault_root=lambda _name: vault),
        today=today,
    )

    assert result.status == nv.UNVERIFIED
    assert {item.label for item in _stale_items(vault, guide, today=today)} == {"Aldo"}


def test_the_memory_skill_points_at_note_related() -> None:
    assert "ciao note related" in _stock("skills/ciao-memory/SKILL.md")


def test_the_memory_skill_points_at_note_create() -> None:
    assert "ciao note create" in _stock("skills/ciao-memory/SKILL.md")


def test_the_memory_skill_did_not_grow() -> None:
    skill = _stock("skills/ciao-memory/SKILL.md")

    # 13400 since #2815: the correction, narrative and credential rules.
    assert len(skill.encode()) <= 13400


# ── The Learnings cleanup pass (#728-E) ──────────────────────────────────────


def _cleanup_config(tmp_path: Path, vault: Path) -> CiaoConfig:
    """A registry naming exactly the vault being planned for.

    The pass folds the skill-proposal queue, which is addressed through the
    workspace registry, so the worklist needs one and the CLI builds exactly this.
    """
    name = vault.name
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=vault,
        workspaces={name: WorkspaceConfig(name=name, vault_root=str(vault))},
    )


def _settled_learning(
    text: str, key: str, *, count: int | None = None
) -> LearningRecord:
    """One canonical learning, identified by the legacy line it was minted from.

    ``count`` is here so a record that the promote pass will also act on can be
    filed *in that shape* from the start: an origin records the entry's own line
    revision, so a line edited after filing stops matching — which is the
    behaviour under test everywhere else and would be noise here.
    """
    return LearningRecord(
        learning_id=allocate_learning_id("personal", f"- {text}"),
        key=key,
        text=text,
        count=count,
        first_seen=None if count is None else date(2026, 1, 1),
        last_seen=None if count is None else date(2026, 9, 1),
    )


def _settled_vault(tmp_path: Path, *records: LearningRecord) -> Path:
    """A vault whose learnings are all settled, each by its own proposal."""
    vault = _vault(tmp_path)
    body = "".join(f"{render_learning(record)}\n" for record in records)
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        f"---\ntags: [ciao, learnings]\n---\n# Learnings\n\n## Active\n\n{body}",
        encoding="utf-8", newline=""
    )
    config = _cleanup_config(tmp_path, vault)
    # The queue is addressed through the registry, and the registry is the vault
    # being planned for — so the workspace name here is the vault directory's own.
    workspace = vault.name
    for record in records:
        proposal = skill_proposals.SkillProposal(
            id=skill_proposals.proposal_id(workspace, record.key),
            workspace=workspace,
            skill=record.key,
            canonical_path=f"/agent/skills/{record.key}/SKILL.md",
            reviewed_revision="a" * 64,
            title=f"Skill reflection: {record.key}",
            problem="Repeated failures.",
            change="Add the step.",
            rationale="It holds.",
            sources=(
                skill_proposals.SkillEvidence(
                    chat_id="s", archive="2026-08-09T10:00:00Z", turn="", excerpt="e"
                ),
            ),
            lifecycle=skill_proposals.PENDING,
            chat_id="",
            updated_at="2026-08-09T10:00:00Z",
            origins=(
                skill_proposals.SkillOrigin(
                    workspace=workspace,
                    learning_id=record.learning_id,
                    source_revision=entry_revision(record),
                    finding="add the step",
                    state=skill_proposals.ORIGIN_APPLIED,
                    verification="mrcpt_0123456789abcdef",
                ),
            ),
        )
        mr.write_queue_atomically(
            skill_proposals.proposal_path(config, workspace, record.key),
            skill_proposals.render_proposal(proposal),
        )
    return vault


def test_a_settled_learning_becomes_a_cleanup_key(tmp_path: Path) -> None:
    """The pass reports what the reconciliation would retire, one key each.

    It plans rather than removes: deciding is a fold over the queue, and the
    decision is recorded before anything is spliced, so the worklist names the
    work and ``ciao learnings-cleanup`` performs it."""
    vault = _settled_vault(tmp_path, _settled_learning("First lesson.", "first"))
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    by_pass = {item.pass_id: item for item in worklist.items}
    assert cr.PASS_LEARNINGS_CLEANUP in by_pass
    assert by_pass[cr.PASS_LEARNINGS_CLEANUP].keys == (
        cr.item_key(cr.PASS_LEARNINGS_CLEANUP, "retire:" + _settled_learning("First lesson.", "first").learning_id),
    )
    assert "1 settled and removable" in by_pass[cr.PASS_LEARNINGS_CLEANUP].reason
    assert "1 active entr(y/ies)" in by_pass[cr.PASS_LEARNINGS_CLEANUP].reason


def test_the_cleanup_pass_runs_after_the_learnings_pass(tmp_path: Path) -> None:
    """The ordering is the whole design: cleanup only removes an entry whose
    findings are durably settled, so a run that reaches it has already passed the
    pass that proposes and decides. Earlier would let it judge a proposal the same
    run has not looked at yet."""
    assert cr.PASS_ORDER.index(cr.PASS_LEARNINGS_CLEANUP) == (
        cr.PASS_ORDER.index(cr.PASS_LEARNINGS) + 1
    )
    # And ahead of the required weekly keys, so a settled backlog cannot spend the
    # budget that lets `last_full_pass` advance.
    assert cr.PASS_ORDER.index(cr.PASS_LEARNINGS_CLEANUP) < cr.PASS_ORDER.index(
        cr.PASS_HYGIENE
    )
    # The record carries a count of 3, so the promote pass has work too and both
    # passes are in the plan at once.
    vault = _settled_vault(
        tmp_path, _settled_learning("Recurring lesson.", "recurring", count=3)
    )
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    order = [item.pass_id for item in worklist.items]
    assert order.index(cr.PASS_LEARNINGS) < order.index(cr.PASS_LEARNINGS_CLEANUP)


def test_an_unproposed_learning_consumes_no_key(tmp_path: Path) -> None:
    """The parent's rule, kept verbatim: an entry nothing has ever asked about is
    not local work, and the unattended pass must not act on it.

    It is also the row a person needs to see, which is why the worklist *note*
    reports it — so the count is visible and the budget is not spent on it."""
    vault = _vault(tmp_path)
    record = _settled_learning("Nobody has proposed this.", "orphan")
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        f"---\ntags: [ciao, learnings]\n---\n# Learnings\n\n## Active\n\n{render_learning(record)}\n",
        encoding="utf-8", newline=""
    )
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_LEARNINGS_CLEANUP not in {item.pass_id for item in worklist.items}
    assert worklist.as_dict()["items"] == [
        item for item in worklist.as_dict()["items"] if item["pass"] != cr.PASS_LEARNINGS_CLEANUP
    ]


def test_a_worklist_without_a_registry_says_the_pass_did_not_run(
    tmp_path: Path,
) -> None:
    """A pass that silently found nothing must never look like a pass that found
    nothing to do. Without a registry there is no queue to fold, and the note says
    so rather than reporting a clean workspace."""
    vault = _settled_vault(tmp_path, _settled_learning("First lesson.", "first"))
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_LEARNINGS_CLEANUP not in {item.pass_id for item in worklist.items}
    assert any("without a workspace registry" in note for note in worklist.notes)


def test_learnings_cleanup_is_skipped_without_a_workspace_name(tmp_path: Path) -> None:
    """A registry is not enough on its own: the fold answers in registered names.

    The settlement is looked up under the workspace the registry knows this
    vault by, so a worklist built with no registered name has no scope to fold
    in — even with a registry in hand. Skipped and said out loud, never reported
    as a workspace with nothing to retire.
    """
    vault = _settled_vault(tmp_path, _settled_learning("First lesson.", "first"))
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=None,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_LEARNINGS_CLEANUP not in {item.pass_id for item in worklist.items}
    assert any("learnings cleanup was not planned" in note for note in worklist.notes)


def test_the_cleanup_backlog_is_capped_and_reported(tmp_path: Path) -> None:
    """A backlog allowed to drain at full speed would take the whole night's
    budget on pass seven of nine, and the required weekly keys would never be
    reached — so the marker cannot advance and the backlog is still there
    tomorrow."""
    records = [
        _settled_learning(f"Lesson {index} is settled.", f"lesson-{index}")
        for index in range(cr.LEARNINGS_CLEANUP_MAX_ITEMS + 3)
    ]
    vault = _settled_vault(tmp_path, *records)
    guide = _guide(tmp_path)
    # The weekly pass is due here, so the required keys are in the plan: the whole
    # claim is that the cleanup backlog does not stop them being reached.
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    item = next(i for i in worklist.items if i.pass_id == cr.PASS_LEARNINGS_CLEANUP)
    assert item.count == cr.LEARNINGS_CLEANUP_MAX_ITEMS
    assert any("wait for the next" in note for note in worklist.notes)
    # And the other passes are still planned, which is the point of the cap.
    assert {i.pass_id for i in worklist.items} >= {cr.PASS_HYGIENE, cr.PASS_GUIDE}


def test_a_maximal_cleanup_backlog_leaves_the_required_hygiene_keys_planned(
    tmp_path: Path,
) -> None:
    """The two keys ``last_full_pass`` may not advance without, named rather than
    implied.

    A cleanup backlog three times the cap is the shape that starves a run: the
    cleanup pass sits seventh of nine, the budget is a whole-run allowance, and
    ``last_full_pass`` is what tells the next week the workspace was actually
    cared for. So the claim is not "the other passes appear" — it is that *these*
    two keys are still in a plan that has a cleanup backlog in it.
    """
    records = [
        _settled_learning(f"Lesson {index} is settled.", f"lesson-{index}")
        for index in range(cr.LEARNINGS_CLEANUP_MAX_ITEMS * 3)
    ]
    vault = _settled_vault(tmp_path, *records)
    guide = _guide(tmp_path)
    # The weekly pass is due, which is the only way these keys exist at all.
    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    keys = {key for item in worklist.items for key in item.keys}

    assert {i.pass_id for i in worklist.items if i.pass_id == cr.PASS_LEARNINGS_CLEANUP}
    assert cr.REQUIRED_HYGIENE_KEYS <= keys
    # And they are planned, not merely present: the cap means the cleanup pass's
    # own tail is what gets deferred, never the checks the marker depends on.
    plan = cr.plan_run(worklist, cr.RunBudget())
    assert cr.REQUIRED_HYGIENE_KEYS <= {key for item in plan.planned for key in item.keys}


def test_the_cleanup_item_names_the_mode_that_performs_it(tmp_path: Path) -> None:
    """A worklist that made the agent look up which flag retires a settled entry
    is a worklist whose eligible rows stay eligible forever.

    The pass plans and the command carries out the plan, so the command is in the
    row the agent is reading — and the row says which rows are *not* its business,
    because the attended rows still need a person and an approval file.
    """
    vault = _settled_vault(tmp_path, _settled_learning("First lesson.", "first"))
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    item = next(i for i in worklist.items if i.pass_id == cr.PASS_LEARNINGS_CLEANUP)
    assert "ciao learnings-cleanup --apply-settled" in item.reason
    assert "no approval file" in item.reason


def test_the_pass_finds_nothing_after_the_unattended_mode_has_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """"A second run is a no-op", stated at the level the pass reads from.

    ``test_a_settled_entry_already_removed_is_not_planned_again`` calls the apply
    directly. This one goes through the mode the worklist names, with the mode's
    own cap, so the pass's claim and the command it points at are pinned against
    each other rather than against a hand-assembled call — and the pass stops
    offering the work, which is the property the budget depends on.
    """
    from ciao import cli
    from ciao import learnings_cleanup

    records = [
        _settled_learning(f"Lesson {index} is settled.", f"lesson-{index}")
        for index in range(cr.LEARNINGS_CLEANUP_MAX_ITEMS)
    ]
    vault = _settled_vault(tmp_path, *records)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    config = _cleanup_config(tmp_path, vault)
    mode = [
        "learnings-cleanup",
        "--apply-settled",
        "--vault-root",
        str(vault),
        "--runtime-root",
        str(tmp_path / ".runtime"),
    ]
    assert cli.main(mode) == 0
    assert f"Removed {cr.LEARNINGS_CLEANUP_MAX_ITEMS} entr(y/ies)." in capsys.readouterr().out

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=_guide(tmp_path),
        category_registry=_categories(vault),
        config=config,
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    after = learnings_cleanup.plan_cleanup(vault, workspace=vault.name, config=config)

    assert after.removals == ()
    assert cr.PASS_LEARNINGS_CLEANUP not in {i.pass_id for i in worklist.items}
    assert all(
        record.key not in (vault / cr.LEARNINGS_RELATIVE).read_text(encoding="utf-8")
        for record in records
    )
    # And the mode itself, run again, retires nothing rather than re-reading the
    # file and finding a reason to.
    capsys.readouterr()
    assert cli.main(mode) == 0
    assert "Nothing to remove." in capsys.readouterr().out


def test_the_cleanup_pass_does_not_starve_a_short_budget(tmp_path: Path) -> None:
    """Splitting within a pass is what lets a queue of proposals drain every night
    instead of being deferred whole forever. The cleanup keys are ordinary keys
    here: they are planned in :data:`PASS_ORDER` and truncated like any others,
    and the rest are deferred rather than dropped."""
    records = [
        _settled_learning(f"Lesson {index} is settled.", f"lesson-{index}")
        for index in range(6)
    ]
    vault = _settled_vault(tmp_path, *records)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    plan = cr.plan_run(worklist, cr.RunBudget(max_items=1))

    assert plan.planned_count == 1
    assert plan.deferred_count == worklist.total_keys - 1
    assert any(item.pass_id == cr.PASS_LEARNINGS_CLEANUP for item in plan.deferred)


def test_a_settled_entry_already_removed_is_not_planned_again(tmp_path: Path) -> None:
    """The suppression is what makes the pass idempotent: the entry is gone, and
    the one an undo put back is recorded, so tonight's plan does not remove it a
    second time. A new revision is what makes it eligible again."""
    from ciao import learnings_cleanup

    record = _settled_learning("First lesson.", "first")
    vault = _settled_vault(tmp_path, record)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    config = _cleanup_config(tmp_path, vault)
    plan = learnings_cleanup.plan_cleanup(vault, workspace=vault.name, config=config)
    assert len(plan.removals) == 1
    learnings_cleanup.apply_cleanup(
        vault, plan, workspace=vault.name, config=config, today=date(2026, 9, 19)
    )

    after = learnings_cleanup.plan_cleanup(vault, workspace=vault.name, config=config)

    assert after.removals == ()
    # And the document no longer has the entry at all, which is the other half.
    assert record.key not in (vault / cr.LEARNINGS_RELATIVE).read_text(encoding="utf-8")


def test_an_unreadable_line_is_reported_in_the_notes(tmp_path: Path) -> None:
    """Unresolved parsing issues are part of the output the plan owes the operator,
    and a line nobody can read is not something the pass should quietly skip."""
    record = _settled_learning("First lesson.", "first")
    vault = _settled_vault(tmp_path, record)
    body = (vault / cr.LEARNINGS_RELATIVE).read_text(encoding="utf-8")
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        body.replace(
            render_learning(record),
            render_learning(record) + "\n- [broken] [2024-13-45 → nope] (x0) Bad.\n",
        ),
        encoding="utf-8", newline=""
    )
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        config=_cleanup_config(tmp_path, vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert any("could not be read" in note for note in worklist.notes)
    assert any("parsing issue" in note for note in worklist.notes)
    # And the readable entry is still planned: one broken line is not a reason to
    # stop reconciling the rest.
    assert cr.PASS_LEARNINGS_CLEANUP in {item.pass_id for item in worklist.items}


# ── The stale-entry pass ───────────────────────────────────────────────────
#
# A note is not the unit a person keeps current: one bullet in it can be two
# years out of date while its neighbours were checked last week, and a
# whole-note verdict has nowhere to put that. These tests pin the pass that sees
# it, and specifically the two properties the note pass's own tests pin one
# level up — determinism with a cap that does not starve hygiene, and a
# suppression predicate that is the *same* one the operation short-circuits on.

TODAY_ENTRIES = date(2026, 9, 19)


def _entry_note(
    vault: Path,
    relative: str,
    *,
    updated: str = "2026-09-18",
    facts: tuple[tuple[str, str], ...] = (),
    type_: str = "person",
) -> Path:
    """A note whose facts are bullets, each with its own verification date.

    ``facts`` is ``(text, stamp)`` pairs and an empty stamp means no ``[verified:]``
    token at all, so a fixture can ask both questions at once: a fact whose own
    date is past the horizon and one that inherits the note's.
    """
    lines = "".join(
        f"- {text}{f' [verified: {stamp}]' if stamp else ''}\n" for text, stamp in facts
    )
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: {type_}\nupdated: {updated}\n---\n\n# {path.stem}\n\n{lines}",
        encoding="utf-8", newline=""
    )
    return path


def _entry_items(
    vault: Path, guide: Path, today: date = TODAY_ENTRIES, **kwargs
) -> list[cr.WorklistItem]:
    # `workspace` is required by `build_worklist` — the entry identity digests
    # it — and the vault directory's name is what these fixtures register as the
    # workspace, which is what `_entry_identity` parses under too.
    kwargs.setdefault("workspace", vault.name)
    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
        **kwargs,
    )
    return [item for item in worklist.items if item.pass_id == cr.PASS_STALE_ENTRY]


def _entry_identity(vault: Path, relative: str, text: str) -> str:
    from ciao import note_entries as ne

    note = (vault / relative).read_text(encoding="utf-8")
    for entry in ne.parse_note_entries(
        note, note_path=relative, workspace=vault.name, today=TODAY_ENTRIES
    ).entries:
        if entry.text.startswith(text):
            return entry.identity
    raise AssertionError(f"no entry starting {text!r} in {relative}")


def test_an_entry_is_work_keyed_by_its_identity_not_its_line(
    tmp_path: Path,
) -> None:
    """Acceptance: the key is the fact, and it survives the note moving.

    A key carrying a line number or an offset would go stale the moment anybody
    edited the file above it, and the pass holding it would re-ask a fact it had
    already answered — the failure :func:`ciao.note_entries.entry_identity`'s
    deliberate absence of both is buying.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "People/Sofia.md",
        facts=(("Speaks Italian and Greek", "2024-01-05"),),
    )
    identity = _entry_identity(vault, "People/Sofia.md", "- Speaks Italian")

    items = _entry_items(vault, guide)

    assert [item.keys for item in items] == [
        (cr.item_key(cr.PASS_STALE_ENTRY, identity),)
    ]
    assert items[0].weekly is False, "a fact goes stale on a clock of its own"
    assert "Sofia" in items[0].label
    # The reason carries the note's revision, the entry's identity and
    # fingerprint, and the span, because those are the values a caller cannot
    # rederive without reimplementing a hash and guessing wrong — the operation
    # would then come back `conflict` for every entry, for ever, and an identity
    # that is not a full 64-hex digest is a refusal before the note is opened.
    # All three WHOLE: a truncated fingerprint is not a prefix match but a
    # different string, and the expected revision is compared exactly.
    from ciao import memory_receipts as mr
    from ciao import note_entries as ne

    note_text = (vault / "People/Sofia.md").read_text(encoding="utf-8")
    entry = ne.parse_note_entries(
        note_text,
        note_path="People/Sofia.md",
        workspace=vault.name,
    ).entries[0]
    age = (TODAY_ENTRIES - date(2024, 1, 5)).days
    # The "why" is the detector's own sentence, not a template this pass composes:
    # an entry with a valid `[verified:]` stamp is aged from that day, and the age
    # and horizon have to travel with the values below so a reader can disagree
    # with the verdict without losing the evidence.
    assert items[0].reason == (
        f"unverified for {age}d against a 30d horizon; "
        f"People/Sofia.md at revision {mr.content_revision(note_text)}, "
        f"entry identity {entry.identity}, "
        f"entry fingerprint {entry.fingerprint} at characters "
        f"{entry.start}-{entry.end}"
    )

    # The note's own date says the note is current, so the note pass finds
    # nothing here: this is exactly the case it cannot see.
    assert [i for i in cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=TODAY_ENTRIES,
    ).items if i.pass_id == cr.PASS_STALE_NOTE] == []


def test_an_entry_survives_its_note_growing_another_fact(tmp_path: Path) -> None:
    """The acceptance criterion's other half: the key does not go stale.

    A fact inserted above the answer moves its line, its offsets and the note's
    revision — and the same entry is still the same entry, so the run cursor
    still suppresses it rather than planning a question that was answered.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    note = _entry_note(
        vault,
        "People/Sofia.md",
        facts=(("Speaks Italian and Greek", "2024-01-05"),),
    )
    identity = _entry_identity(vault, "People/Sofia.md", "- Speaks Italian")
    _entry_note(
        vault,
        "People/Sofia.md",
        facts=(
            ("Lives in Via Verdi 12", "2026-09-01"),
            ("Speaks Italian and Greek", "2024-01-05"),
        ),
    )
    assert note.read_text(encoding="utf-8") != ""
    moved = _entry_identity(vault, "People/Sofia.md", "- Speaks Italian")

    assert moved == identity
    # The fresh fact is not work, and the stale one still is.
    assert [item.keys[0] for item in _entry_items(vault, guide)] == [
        cr.item_key(cr.PASS_STALE_ENTRY, identity)
    ]


def test_an_entry_ages_on_its_own_date_not_only_its_notes(
    tmp_path: Path,
) -> None:
    """A fact with no stamp inherits the note's date; one with a stamp is its own.

    Both cases, because the difference is the point: a note re-stamped yesterday
    has silently re-certified everything in it, and an entry that was checked two
    years ago and has not been since is still the oldest work in the vault.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "People/Sofia.md",
        updated="2024-01-05",
        facts=(
            ("Lives in Via Verdi 12", ""),
            ("Works at Acme", "2024-01-05"),
            ("Speaks Greek", "2026-09-01"),
        ),
    )

    items = _entry_items(vault, guide)

    # The unstamped fact and the two-year-old one are both due; the freshly
    # stamped one is not. Same note, same horizon, three different answers.
    assert len(items) == 2
    assert {item.label for item in items} == {
        f"Sofia — entry {_entry_identity(vault, 'People/Sofia.md', '- Lives in')[:12]}",
        f"Sofia — entry {_entry_identity(vault, 'People/Sofia.md', '- Works at')[:12]}",
    }


def _entry_fingerprint(vault: Path, relative: str) -> str:
    from ciao import note_entries as ne

    return ne.parse_note_entries(
        (vault / relative).read_text(encoding="utf-8"),
        note_path=relative,
        workspace=vault.name,
    ).entries[0].fingerprint


def test_an_undated_note_is_still_scanned_for_its_entries(tmp_path: Path) -> None:
    """The note pass cannot plan a note with no usable date; the entry pass can.

    "Unverifiable is not stale" is right about a *file* and wrong one bullet in.
    An entry carrying its own `[verified:]` stamp is aged from that day, and the
    note's date is only the fallback — so a note with no `updated:` and an mtime
    the scan cannot read can still be holding a fact from 2019. This scan used to
    drop such a note entirely, which left `os-audit` and the Memory Map reporting
    an overdue fact the nightly plan said nothing about: the exact disagreement
    the entry level was built to remove, still present in the one place the two
    passes diverge.
    """
    from ciao import memory_audit as ma

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    path = vault / "People/Sofia.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # No `updated:`, and an mtime far enough back that the fallback cannot
    # rescue it either.
    path.write_text(
        "---\ntype: person\n---\n\n# Sofia\n\n- Speaks Greek [verified: 2020-01-01]\n",
        encoding="utf-8", newline=""
    )
    import os

    os.utime(path, (0, 0))

    items = _entry_items(vault, guide)
    assert len(items) == 1
    assert "Speaks Greek" not in items[0].reason  # the reason states the numbers
    assert "unverified for" in items[0].reason

    # And the note pass still cannot plan it, because that is what
    # "unverifiable is not stale" means at file width.
    worklist = cr.build_worklist(
        vault_root=vault,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace=vault.name,
        workspace_dir=guide.parent,
        today=TODAY_ENTRIES,
    )
    assert [i for i in worklist.items if i.pass_id == cr.PASS_STALE_NOTE] == []

    # The detector agrees: the same entry is the same finding here as it is to
    # the audit and the map, under the same identity.
    from ciao.vault_index import scan_vault

    entries = scan_vault(vault, registry=_categories(vault))
    detected = ma.find_stale_entries(
        entries,
        vault_root=vault,
        workspace=vault.name,
        today=TODAY_ENTRIES,
        registry=_categories(vault),
    )
    assert {i.keys[0] for i in items} == {
        cr.item_key(cr.PASS_STALE_ENTRY, row["identity"])
        for row in detected["stale_entries"]
    }


def test_a_settled_entry_is_not_planned_again(tmp_path: Path) -> None:
    """The entry pass's twin of the note pass's cooldown filter.

    An `unverified` verdict writes nothing — a re-stamp is the only outcome that
    touches the note — so without this the same bullet would be listed every
    night, come back `already_checked`, and take the first slot for ever. The
    predicate is the one `verify_entry` short-circuits on, so the plan cannot
    offer an entry the operation would refuse to judge.
    """
    from ciao import entry_verification as ev

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "People/Sofia.md",
        facts=(("Speaks Italian and Greek", "2024-01-05"),),
    )
    identity = _entry_identity(vault, "People/Sofia.md", "- Speaks Italian")
    assert _entry_items(vault, guide), "the entry is due before it is checked"
    ev.record_entry_check(
        vault,
        ev.EntryCheck(
            identity=identity,
            note_path="People/Sofia.md",
            workspace=vault.name,
            content_fingerprint=_entry_fingerprint(vault, "People/Sofia.md"),
            outcome=ev.UNVERIFIED,
            checked_at=TODAY_ENTRIES,
            retry_after=TODAY_ENTRIES + timedelta(days=ev.CHECK_COOLDOWN_DAYS),
        ),
    )

    assert _entry_items(vault, guide) == []

    # A re-worded fact is due again, because the check describes the words: the
    # fingerprint the predicate compares is the entry's, not the note's.
    ev.record_entry_check(
        vault,
        replace(
            ev.read_entry_checks(vault)[identity], content_fingerprint="b" * 64
        ),
    )
    assert len(_entry_items(vault, guide)) == 1


def test_the_entry_pass_plans_oldest_first_and_is_capped(tmp_path: Path) -> None:
    """Acceptance: deterministic, oldest-first, capped, and it says what it left.

    The cap comes after the filter and the order is a total one — age, then path,
    then the entry's own position — so two runs over the same vault produce the
    same list in the same order and a short budget drops the *youngest* fact
    rather than an arbitrary one.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    # Eight notes re-stamped yesterday, each holding one fact stamped a fortnight
    # apart since 2024: more than the cap, a total order with no ties to break by
    # accident, and — the point of the fixture — nothing the *note* pass can see.
    stamps = [date(2024, 1, 5) + timedelta(days=14 * index) for index in range(8)]
    for index, stamp in enumerate(stamps):
        _entry_note(
            vault,
            f"People/Note{index:02d}.md",
            updated="2026-09-18",
            facts=((f"Lives at number {index}", stamp.isoformat()),),
        )

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=TODAY_ENTRIES,
    )
    assert [i for i in worklist.items if i.pass_id == cr.PASS_STALE_NOTE] == [], (
        "every note was re-stamped yesterday; only the entries are work"
    )
    items = _entry_items(vault, guide)

    # Oldest first, so the fact that has gone longest unanswered goes first.
    assert [item.label.split(" — ")[0] for item in items] == [
        f"Note{index:02d}" for index in range(cr.STALE_ENTRY_MAX_ITEMS)
    ]
    # Capped, and the cap is not passed off as the whole queue.
    assert len(items) == cr.STALE_ENTRY_MAX_ITEMS
    assert any("wait for the next run" in note for note in worklist.notes)
    # And a budget of two takes the two oldest, deferring the rest whole.
    plan = cr.plan_run(worklist, cr.RunBudget(max_items=2))
    planned = [key for item in plan.planned for key in item.keys]
    assert planned == [
        cr.item_key(cr.PASS_STALE_ENTRY, _entry_identity(vault, f"People/Note{index:02d}.md", "- Lives"))
        for index in range(2)
    ]


def test_an_entry_backlog_cannot_starve_the_required_hygiene_keys(
    tmp_path: Path,
) -> None:
    """The acceptance criterion, at the new pass's width.

    The pass sits ahead of the weekly hygiene keys and the budget is a whole-run
    allowance, so an uncapped backlog of bullets would spend it: the two required
    checks never reached, `last_full_pass` unable to advance, and the backlog
    unchanged the next night. A vault with one enormous fact list is exactly the
    one that would never get its weekly care.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    facts = tuple(
        (f"Fact number {index} about Sofia", "2024-01-05")
        for index in range(cr.STALE_ENTRY_MAX_ITEMS * 8)
    )
    _entry_note(vault, "People/Sofia.md", updated="2024-01-05", facts=facts)

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=TODAY_ENTRIES,
    )
    plan = cr.plan_run(worklist)

    planned = {key for item in plan.planned for key in item.keys}
    assert cr.REQUIRED_HYGIENE_KEYS <= planned
    assert sum(
        item.count for item in plan.planned if item.pass_id == cr.PASS_STALE_ENTRY
    ) == cr.STALE_ENTRY_MAX_ITEMS
    assert cr.PASS_STALE_ENTRY in cr.PASS_ORDER
    assert cr.PASS_ORDER.index(cr.PASS_STALE_ENTRY) == (
        cr.PASS_ORDER.index(cr.PASS_STALE_NOTE) + 1
    )
    assert cr.PASS_ORDER.index(cr.PASS_STALE_ENTRY) < cr.PASS_ORDER.index(
        cr.PASS_HYGIENE
    )


def test_an_exempt_entry_type_is_not_work(tmp_path: Path) -> None:
    """A `journal` is as true the day it was written, bullet or not.

    And the reason it is not work is the audit's exempt set rather than a second
    copy of it: a pass that re-decided would drift from the Memory Map's flag and
    the review queue's signal, and the run would go and verify records that
    cannot go stale.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "Journal/2024.md",
        updated="2024-01-05",
        type_="journal",
        facts=(("Shipped the first release", ""),),
    )

    assert _entry_items(vault, guide) == []


def test_an_entry_the_pass_cannot_read_is_not_planned(tmp_path: Path) -> None:
    """Advisory: a note that is not there plans nothing rather than raising out of
    `curation-begin` and costing the run its other passes."""
    from ciao import curation_run

    assert curation_run._stale_entry_items(
        vault_root=tmp_path / "not-a-vault",
        workspace="personal",
        scanned=[],
        today=TODAY_ENTRIES,
    ) == ([], "")


def test_the_entry_items_reason_verifies_the_entry_it_planned(tmp_path: Path) -> None:
    """The plan's reason has to be the payload the operation needs, whole.

    This is the difference between a nightly pass that verifies facts and one
    that plans work nobody can act on. The skills tell the agent to copy the
    reason's revision, identity and fingerprint into the payload file, and
    :func:`ciao.entry_verification.verify_entry` compares all three *exactly* —
    a 64-hex fingerprint compared as a string is not a prefix match but a
    different value, an identity that is not a full 64-hex digest is a refusal
    before the note is even opened, and a missing `expected_revision` is a
    refusal. So the test parses the reason the pass printed and hands it to the
    real service, which is the only way to know the plan and the operation agree
    — and it takes the identity *out of the reason* rather than recomputing it,
    because recomputing proves nothing about what the agent is given: the label
    shows twelve characters and the worklist key is a digest of the identity, so
    an agent with neither could not have built this payload.
    """
    import re

    from types import SimpleNamespace

    from ciao import entry_verification as ev
    from ciao import note_verification as nv

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "People/Sofia.md",
        facts=(("Speaks Italian and Greek", "2024-01-05"),),
    )
    items = _entry_items(vault, guide)
    reason = items[0].reason

    parsed = re.search(
        r"(?P<note>\S+) at revision (?P<revision>[0-9a-f]{64}), entry identity "
        r"(?P<identity>[0-9a-f]{64}), entry fingerprint "
        r"(?P<fingerprint>[0-9a-f]{64}) at characters (?P<start>\d+)-(?P<end>\d+)",
        reason,
    )
    assert parsed is not None, f"the reason does not carry a usable payload: {reason!r}"

    result = ev.verify_entry(
        ev.EntryVerificationRequest(
            workspace=vault.name,
            relative_path=parsed["note"],
            identity=parsed["identity"],
            entry_fingerprint=parsed["fingerprint"],
            expected_revision=parsed["revision"],
            # An `unverified` verdict: this test is about the plan handing the
            # operation a request it can act on, not about a verdict's own rule.
            outcome=ev.UNVERIFIED,
            coverage=nv.COVERAGE_PARTIAL,
            reason="planned by the stale-entry pass",
        ),
        vault_root=vault,
        config=SimpleNamespace(workspace_vault_root=lambda _name: vault),
        today=TODAY_ENTRIES,
    )

    assert result.status == ev.UNVERIFIED, result.message
    # The identity the reason printed is the one the note holds at that span, so
    # the identity an agent reads off the plan is the one it must hand back.
    assert parsed["identity"] == _identity_of(
        vault, parsed["note"], int(parsed["start"])
    )
    # The span the reason printed is where the entry actually is, so a caller who
    # uses it to read the note reads the fact and not its neighbour.
    assert int(parsed["end"]) - int(parsed["start"]) == len(
        _entry_text_at(vault, parsed["note"], int(parsed["start"]))
    )
    # And nothing else in the item carries it reversibly: the label abbreviates
    # and the key is a digest, so the reason is the only place it is whole.
    assert parsed["identity"] not in items[0].label
    assert parsed["identity"] not in items[0].keys[0]


def _identity_of(vault: Path, relative: str, start: int) -> str:
    """The identity of the entry at *start* in *relative*, as an operation is given it."""
    from ciao import note_entries as ne

    document = ne.parse_note_entries(
        (vault / relative).read_text(encoding="utf-8"),
        note_path=relative,
        workspace=vault.name,
    )
    return next(
        entry.identity for entry in document.entries if entry.start == start
    )


def _entry_text_at(vault: Path, relative: str, start: int) -> str:
    """The entry text at *start* in *relative*."""
    from ciao import note_entries as ne

    document = ne.parse_note_entries(
        (vault / relative).read_text(encoding="utf-8"),
        note_path=relative,
        workspace=vault.name,
    )
    return next(entry.text for entry in document.entries if entry.start == start)


def test_the_entry_pass_mints_identities_for_the_registered_workspace(
    tmp_path: Path,
) -> None:
    """A vault directory's name is not the workspace's name on every layout.

    :func:`ciao.note_entries.entry_identity` digests the workspace, and every
    operation that resolves an identity — the managed verifier, the proposal's
    accept — asks for it under the name the *registry* knows the workspace by.
    An install whose ``memory-vault/client-a`` holds workspace ``work`` would
    otherwise have the pass mint identities nothing can resolve: every entry
    returns CONFLICT, no verdict is ever filed, and the worklist key never
    settles. So the name is passed in, and the key the pass plans is the key the
    operation answers to.
    """
    from ciao import note_entries as ne

    vault = tmp_path / "memory-vault" / "client-a"
    (vault / "Workspace").mkdir(parents=True)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _entry_note(
        vault,
        "People/Sofia.md",
        facts=(("Speaks Italian and Greek", "2024-01-05"),),
    )
    relative = "People/Sofia.md"

    items = _entry_items(vault, guide, workspace="work")
    document = ne.parse_note_entries(
        (vault / relative).read_text(encoding="utf-8"),
        note_path=relative,
        workspace="work",
    )
    wrong = ne.parse_note_entries(
        (vault / relative).read_text(encoding="utf-8"),
        note_path=relative,
        workspace=vault.name,
    )
    entry = document.entries[0]

    assert vault.name == "client-a"
    assert [item.keys for item in items] == [
        (cr.item_key(cr.PASS_STALE_ENTRY, entry.identity),)
    ]
    assert "entry client-a" not in items[0].label, (
        "the pass keyed the work on the directory's name, which resolves to nothing"
    )
    assert wrong.entries[0].identity != entry.identity
    assert wrong.entries[0].identity not in {key for item in items for key in item.keys}


def test_the_entry_pass_reads_the_check_state_once(tmp_path: Path) -> None:
    """One read of the sidecar per plan, not one per due entry.

    :func:`ciao.entry_verification.read_entry_checks` re-reads and re-parses the
    whole document on every call, and the entry pass asks about *every* due entry
    in the vault on every ``curation-begin`` — so the per-entry spelling is
    O(entries × check-state size) for no reason, and it is the only caller that
    does it. The batch form is the same predicate over a map read once, so this
    pins the *count* rather than the outcome: a plan over many due entries must
    not read the file once per entry.
    """
    from ciao import entry_verification as ev

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(cr.STALE_ENTRY_MAX_ITEMS + 3):
        _entry_note(
            vault,
            f"People/Note{index:02d}.md",
            updated="2026-09-18",
            facts=((f"Lives at number {index}", "2024-01-05"),),
        )
    assert len(_entry_items(vault, guide)) == cr.STALE_ENTRY_MAX_ITEMS

    reads: list[Path] = []
    original = ev.read_entry_checks

    def counted(root: Path | str) -> dict[str, ev.EntryCheck]:
        reads.append(Path(root) / cr.CURATION_LOG_RELATIVE)
        return original(root)

    monkey = ev.read_entry_checks
    ev.read_entry_checks = counted  # type: ignore[assignment]
    try:
        _entry_items(vault, guide)
    finally:
        ev.read_entry_checks = monkey  # type: ignore[assignment]

    assert len(reads) == 1, f"the sidecar was read {len(reads)} times for one plan"


# ── Observations pass (#1338) ─────────────────────────────────────────────


def _observation_record(
    text: str, chats: tuple[str, ...], *, last: date, first: date | None = None
) -> LearningRecord:
    from ciao.learning_records import LearningObservation

    return LearningRecord(
        learning_id=allocate_learning_id("work", f"obs:{text}"),
        key=text.split()[-1],
        text=f"About the user: {text}",
        first_seen=first or last,
        last_seen=last,
        count=len(chats),
        observations=tuple(LearningObservation(source=chat) for chat in chats),
    )


def _observation_vault(tmp_path: Path, *records: LearningRecord, resolved: tuple[LearningRecord, ...] = ()) -> Path:
    from ciao import observations

    vault = _vault(tmp_path)
    body = "".join(f"{render_learning(record)}\n" for record in records)
    done = "".join(f"{render_learning(record)}\n" for record in resolved)
    (vault / observations.OBSERVATIONS_RELATIVE).write_text(
        f"{observations.OBSERVATIONS_STUB}{body}\n## Promoted / Resolved\n{done}",
        encoding="utf-8",
        newline="",
    )
    return vault


def _observation_worklist(vault: Path, tmp_path: Path, *, workspace: str | None = "work") -> cr.Worklist:
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    return cr.build_worklist(
        vault_root=vault,
        workspace=workspace,
        guide_path=_guide(tmp_path),
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )


def _observation_items(worklist: cr.Worklist) -> list[cr.WorklistItem]:
    return [item for item in worklist.items if item.pass_id == cr.PASS_OBSERVATIONS]


def test_observation_pass_plans_propose_and_prune(tmp_path: Path) -> None:
    due = _observation_record("prefers one line", ("a", "b", "c"), last=date(2026, 9, 10))
    stale = _observation_record("likes tea", ("a",), last=date(2026, 6, 1))
    vault = _observation_vault(tmp_path, due, stale)

    (item,) = _observation_items(_observation_worklist(vault, tmp_path))

    assert item.label == "Propose recurring observations"
    assert item.keys == (
        cr.item_key(cr.PASS_OBSERVATIONS, f"propose:{due.learning_id}"),
        cr.item_key(cr.PASS_OBSERVATIONS, "prune"),
    )
    assert "1 observation(s) seen in 3+ chats" in item.reason
    assert "1 x1 observation(s) older than 60 days" in item.reason


def test_observation_pass_caps_at_five_and_notes_overflow(tmp_path: Path) -> None:
    records = [
        _observation_record(f"habit number{index}", ("a", "b", "c"), last=date(2026, 9, 10),
                            first=date(2026, 8, index + 1))
        for index in range(7)
    ]
    vault = _observation_vault(tmp_path, *records)

    worklist = _observation_worklist(vault, tmp_path)

    (item,) = _observation_items(worklist)
    assert len(item.keys) == cr.OBSERVATIONS_MAX_ITEMS
    oldest = [cr.item_key(cr.PASS_OBSERVATIONS, f"propose:{r.learning_id}") for r in records[:5]]
    assert list(item.keys) == oldest
    assert any("7 observations are due" in note for note in worklist.notes)


def test_observation_merge_is_planned_weekly_for_a_subject_seen_twice(tmp_path: Path) -> None:
    one = _observation_record("prefers one line", ("a",), last=date(2026, 9, 10))
    two = _observation_record("wants a short summary first", ("b",), last=date(2026, 9, 12))
    vault = _observation_vault(tmp_path, one, two)

    assert _observation_items(_observation_worklist(vault, tmp_path)) == []

    _fresh_log(vault, last_full_pass=date(2026, 9, 1).isoformat())
    worklist = cr.build_worklist(
        vault_root=vault,
        workspace="work",
        guide_path=_guide(tmp_path),
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    (item,) = _observation_items(worklist)
    assert item.keys == (cr.item_key(cr.PASS_OBSERVATIONS, "merge"),)


def test_resolved_observations_are_not_planned(tmp_path: Path) -> None:
    done = _observation_record("prefers one line", ("a", "b", "c"), last=date(2026, 9, 10))
    stale = _observation_record("likes tea", ("a",), last=date(2026, 1, 1))
    vault = _observation_vault(tmp_path, resolved=(done, stale))

    assert _observation_items(_observation_worklist(vault, tmp_path)) == []


def test_observation_pass_runs_after_learnings_cleanup() -> None:
    order = cr.PASS_ORDER
    assert order.index(cr.PASS_OBSERVATIONS) == order.index(cr.PASS_LEARNINGS_CLEANUP) + 1
    assert order.index(cr.PASS_OBSERVATIONS) < order.index(cr.PASS_HYGIENE)


def test_observations_skipped_without_workspace_name(tmp_path: Path) -> None:
    due = _observation_record("prefers one line", ("a", "b", "c"), last=date(2026, 9, 10))
    vault = _observation_vault(tmp_path, due)

    worklist = _observation_worklist(vault, tmp_path, workspace=None)

    assert _observation_items(worklist) == []
    assert any("observations were not planned" in note for note in worklist.notes)


def test_the_care_prompt_proposes_observations_between_sections_4_and_5() -> None:
    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )

    assert "## 4b. Propose recurring observations" in prompt
    assert prompt.index("## 4. ") < prompt.index("## 4b. ") < prompt.index("## 5. ")
    observations = _stock("skills/ciao-memory/references/curation-pass-4b-observations.md")
    assert "references/curation-pass-4b-observations.md" in prompt
    for command in ("ciao observation-propose", "ciao observation-merge", "ciao observation-prune"):
        assert command in observations


def test_rules_pass_planned_only_when_weekly_due_and_stale(tmp_path: Path) -> None:
    """The decision-rules pass rides the weekly marker and needs fresh operator decisions."""
    from ciao import decision_rules

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)

    def plan(last: str) -> dict[str, cr.WorklistItem]:
        _fresh_log(vault, last_full_pass=last)
        return {
            item.pass_id: item
            for item in cr.build_worklist(
                vault_root=vault,
                workspace=vault.name,
                guide_path=guide,
                category_registry=_categories(vault),
                workspace_dir=tmp_path,
                today=date(2026, 9, 19),
            ).items
        }

    assert cr.PASS_RULES not in plan("2026-09-01")
    for index in range(5):
        task_decisions.append_decision(
            vault,
            kind="delete",
            task_id=f"t{index}",
            task_title="x",
            task_created_by="agent",
            project_id="",
            attempt_id="",
            attempt_state="",
            actor="user",
            via="pwa",
            reason="",
            now=datetime(2026, 9, 10, tzinfo=UTC),
        )
    item = plan("2026-09-01")[cr.PASS_RULES]
    assert item.weekly is True
    assert item.keys == (decision_rules.progress_key("task"),)
    assert set(item.keys).isdisjoint(cr.REQUIRED_HYGIENE_KEYS)
    assert cr.PASS_RULES not in plan("2026-09-18")
    assert cr.PASS_ORDER.index(cr.PASS_GUIDE) < cr.PASS_ORDER.index(cr.PASS_RULES) < cr.PASS_ORDER.index(cr.PASS_LOGS)


def test_the_care_prompt_names_the_rules_pass_and_pass1_read() -> None:
    prompt = next(
        entry["prompt"]
        for entry in json.loads(_stock("schedules.json"))["schedules"]
        if entry["schedule_id"] == "system-memory-curation"
    )
    assert "## 7b. Distill decision rules" in prompt
    assert prompt.index("## 7. ") < prompt.index("## 7b. ") < prompt.index("## 8. ")
    assert "references/curation-pass-7b-decision-rules.md" in prompt
    assert "Workspace/Memory-Review-Rules.md` if it exists" in prompt
    assert len(prompt.encode("utf-8")) <= 8192


# ── decider ────────────────────────────────────────────────────────────────


def _worklist_for_queue(
    tmp_path: Path,
    queue_text: str,
    *,
    handover: str = "off",
    predictions: tuple[tuple[str, str, str], ...] = (),
    dismissed_at: str | None = None,
) -> "cr.Worklist":
    import json

    from ciao import proposal_predictions
    from ciao.memory_proposals import dismissed_log_path

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "# Memory proposals\n\n" + queue_text, encoding="utf-8", newline=""
    )
    for kind, text, verdict in predictions:
        proposal_predictions.record_prediction(
            vault / cr.PROPOSALS_RELATIVE,
            kind=kind,
            text=text,
            verdict=verdict,
            reason="because",
        )
    if dismissed_at is not None:
        # Every queued fact was decided (and re-queued) after its prediction.
        decided = [text for _kind, text, _verdict in predictions]
        dismissed_log_path(vault / cr.PROPOSALS_RELATIVE).write_text(
            "".join(
                json.dumps({"dismissed_at": dismissed_at, "kind": "memory", "text": text}) + "\n"
                for text in decided
            ),
            encoding="utf-8",
        )
    return cr.build_worklist(
        handover=handover,
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )


def test_decide_you_rows_are_not_nightly_work(tmp_path: Path) -> None:
    """An escalated row waits for the operator; a nightly or undecided one is work."""
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    escalated = _worklist_for_queue(tmp_path / "a", "- [people Mo] x  _(decide: you; r)_\n")
    assert escalated.empty
    nightly = _worklist_for_queue(tmp_path / "b", "- [people Mo] x  _(decide: nightly; r)_\n")
    assert not nightly.empty
    region = _worklist_for_queue(tmp_path / "c", "- [memory] x  _(decide: nightly; r)_\n")
    assert not region.empty


def _proposal_keys(worklist: "cr.Worklist") -> tuple[str, ...]:
    for item in worklist.items:
        if item.pass_id == cr.PASS_PROPOSALS:
            return item.keys
    return ()


def test_a_predicted_region_fact_is_not_work_when_handover_is_off(tmp_path: Path) -> None:
    worklist = _worklist_for_queue(
        tmp_path,
        "- [memory] x\n",
        handover="off",
        predictions=(("memory", "x", "dismiss"),),
    )
    assert worklist.empty


def test_a_predicted_dismiss_is_act_work_for_dismiss_and_all(tmp_path: Path) -> None:
    for name, handover in (("a", "dismiss"), ("b", "all")):
        (tmp_path / name).mkdir()
        worklist = _worklist_for_queue(
            tmp_path / name,
            "- [memory] x\n",
            handover=handover,
            predictions=(("memory", "x", "dismiss"),),
        )
        assert len(_proposal_keys(worklist)) == 1


def test_a_predicted_accept_is_act_work_only_for_all(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    for name, handover, expected in (("a", "off", 0), ("b", "dismiss", 0), ("c", "all", 1)):
        worklist = _worklist_for_queue(
            tmp_path / name,
            "- [memory] x\n",
            handover=handover,
            predictions=(("memory", "x", "accept"),),
        )
        assert len(_proposal_keys(worklist)) == expected


def test_decide_you_region_row_is_predicted_but_never_acted_on(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    row = "- [memory] x  _(decide: you; r)_\n"
    unpredicted = _worklist_for_queue(tmp_path / "a", row, handover="all")
    assert len(_proposal_keys(unpredicted)) == 1
    predicted = _worklist_for_queue(
        tmp_path / "b", row, handover="all", predictions=(("memory", "x", "accept"),)
    )
    assert predicted.empty


def test_region_row_key_changes_between_predict_and_act_stage(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    predict = _worklist_for_queue(tmp_path / "a", "- [memory] x\n", handover="all")
    act = _worklist_for_queue(
        tmp_path / "b",
        "- [memory] x\n",
        handover="all",
        predictions=(("memory", "x", "accept"),),
    )
    assert _proposal_keys(predict) != _proposal_keys(act)
    assert len(_proposal_keys(predict)) == len(_proposal_keys(act)) == 1


def test_a_requeued_region_fact_with_a_stale_prediction_is_predict_work_again(
    tmp_path: Path,
) -> None:
    """The operator dismissed it after the verdict was recorded; the old verdict is void."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    row = "- [memory] x\n"
    acting = _worklist_for_queue(
        tmp_path / "a", row, handover="all", predictions=(("memory", "x", "accept"),)
    )
    requeued = _worklist_for_queue(
        tmp_path / "b",
        row,
        handover="all",
        predictions=(("memory", "x", "accept"),),
        dismissed_at="2999-01-01T00:00:00+00:00",
    )

    assert len(_proposal_keys(acting)) == 1  # act:accept
    # Back at the predict stage, so a different key.
    assert len(_proposal_keys(requeued)) == 1
    assert _proposal_keys(requeued) != _proposal_keys(acting)


def test_dismiss_and_all_share_the_act_key_for_a_predicted_dismiss(tmp_path: Path) -> None:
    """Pins today's behaviour: the key carries the stage and verdict, not the setting."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    keys = [
        _proposal_keys(
            _worklist_for_queue(
                tmp_path / name,
                "- [memory] x\n",
                handover=handover,
                predictions=(("memory", "x", "dismiss"),),
            )
        )
        for name, handover in (("a", "dismiss"), ("b", "all"))
    ]
    assert keys[0] == keys[1]


def test_item_key_ignores_the_decider() -> None:
    """The worklist key must not hash the decider, or resume of a run breaks."""
    from ciao.proposal_kinds import parse_bullet

    plain = parse_bullet("- [people Mo] x  _(from: s)_")
    tagged = parse_bullet("- [people Mo] x  _(from: s)_  _(decide: you; r)_")
    assert plain is not None and tagged is not None
    assert cr._proposal_subject(plain, 0) == cr._proposal_subject(tagged, 0)


# ── Managed log append and the shared marker primitive (#3101, A1) ────────


@pytest.fixture
def _lock_depth(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record how many times the canonical state lock is entered, and how deep.

    Returned list holds the nesting depth observed at each marker write, so a
    test can assert the write happened under exactly one lock.
    """
    import contextlib

    real = cr._state_lock
    state = {"depth": 0}
    observed: list[int] = []

    @contextlib.contextmanager
    def counting(vault_root: Path):
        state["depth"] += 1
        try:
            with real(vault_root):
                yield
        finally:
            state["depth"] -= 1

    real_marker = cr._write_full_pass_marker

    def marker(vault_root: Path, **kwargs: object) -> None:
        observed.append(state["depth"])
        real_marker(vault_root, **kwargs)

    monkeypatch.setattr(cr, "_state_lock", counting)
    monkeypatch.setattr(cr, "_write_full_pass_marker", marker)
    return observed


def test_advance_full_pass_checks_and_writes_under_the_state_lock(
    tmp_path: Path, _lock_depth: list[int]
) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))

    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is True
    assert _lock_depth == [1]


def test_end_run_writes_the_marker_under_one_lock_without_reacquiring(
    tmp_path: Path, _lock_depth: list[int]
) -> None:
    vault = _vault(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    lease = cr.begin_run(vault, holder="run")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS), holder=lease.holder)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, today=date(2026, 9, 19)
    )
    assert summary["full_pass_advanced"] is True
    assert _lock_depth == [1]


def test_append_log_appends_under_the_live_lease_and_reports_bytes(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="run")
    log = vault / cr.CURATION_LOG_RELATIVE

    result = cr.append_log(vault, holder=lease.holder, log="curation", payload=b"technical report")

    assert result == {"status": "appended", "log": "curation", "bytes_appended": 2 + 16}
    assert log.read_bytes() == b"# Curation log\n" + b"\n\n" + b"technical report"


def test_append_log_refuses_a_holder_that_does_not_own_the_lease(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run")
    _fresh_log(vault, last_full_pass="2026-09-01")
    before = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()

    with pytest.raises(cr.CurationBusy):
        cr.append_log(vault, holder="someone-else", log="curation", payload=b"x")
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == before


def test_append_log_refuses_an_expired_lease_without_writing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="run", ttl_s=60, now=_DISPATCHED)

    with pytest.raises(cr.CurationBusy):
        cr.append_log(
            vault,
            holder=lease.holder,
            log="weekly-review",
            payload=b"late",
            now=_DISPATCHED + timedelta(seconds=61),
        )
    assert not (vault / cr.WEEKLY_REVIEW_LOG_RELATIVE).exists()


def test_append_log_never_renews_or_changes_state(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="run", ttl_s=60, now=_DISPATCHED)
    state_before = (vault / cr.STATE_RELATIVE).read_bytes()

    cr.append_log(
        vault, holder=lease.holder, log="curation", payload=b"x", now=_DISPATCHED,
        clock=lambda: _DISPATCHED,
    )

    assert (vault / cr.STATE_RELATIVE).read_bytes() == state_before


def test_oversized_logs_are_blocked_and_consume_no_runnable_budget(tmp_path: Path) -> None:
    """Through the real `_log_items` and skill-proposal items, not hand-built items."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 19).isoformat())
    head = b"---\nlast_full_pass: 2026-09-19\n---\n\n# Curation log\n"
    big = head + b"x" * (cr.LOG_ROTATION_BYTES + 1 - len(head))
    (vault / cr.CURATION_LOG_RELATIVE).write_bytes(big)
    (vault / cr.WEEKLY_REVIEW_LOG_RELATIVE).write_bytes(big)
    queue = vault / cr.SKILL_PROPOSALS_RELATIVE
    queue.mkdir()
    for name in ("alpha.md", "bravo.md"):
        (queue / name).write_text("x", encoding="utf-8", newline="")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert [item.pass_id for item in worklist.items] == [
        cr.PASS_LOGS,
        cr.PASS_SKILL_PROPOSALS,
        cr.PASS_SKILL_PROPOSALS,
    ]
    plan = cr.plan_run_with_blocked(worklist, cr.RunBudget(max_items=1))
    assert [item.label for item in plan.planned] == ["Report skill proposal alpha.md (do not settle)"]
    assert plan.planned_count == 1
    assert plan.blocked_count == 2
    assert plan.blocked_passes == [cr.PASS_LOGS]
    assert plan.deferred_count == 2 + 1
    assert [item.pass_id for item in plan.deferred] == [cr.PASS_LOGS, cr.PASS_SKILL_PROPOSALS]
    assert "operator archival required" in plan.deferred[0].reason
    assert "over 64KB" in plan.deferred[0].reason


def test_each_open_skill_proposal_is_its_own_named_item_so_budget_binds_one_file(
    tmp_path: Path,
) -> None:
    """A budgeted key means one named proposal, never the whole queue."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    queue = vault / cr.SKILL_PROPOSALS_RELATIVE
    queue.mkdir()
    for name in ("alpha.md", "bravo.md", "charlie.md"):
        (queue / name).write_text("x", encoding="utf-8", newline="")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    skill_items = [i for i in worklist.items if i.pass_id == cr.PASS_SKILL_PROPOSALS]
    assert [i.keys for i in skill_items] == [
        (cr.item_key(cr.PASS_SKILL_PROPOSALS, name),) for name in ("alpha.md", "bravo.md", "charlie.md")
    ]
    assert [i.label for i in skill_items] == [
        f"Report skill proposal {name} (do not settle)" for name in ("alpha.md", "bravo.md", "charlie.md")
    ]

    plan = cr.plan_run_with_blocked(worklist, cr.RunBudget(max_items=1))
    assert [i.label for i in plan.planned] == ["Report skill proposal alpha.md (do not settle)"]
    assert plan.planned_count == 1
    assert plan.deferred_count == 2


# ── Round 2: refused marker, authority recheck, read-only, shared closer ──


@pytest.mark.parametrize(
    "case",
    ["duplicate-marker", "unclosed-frontmatter", "oversized", "symlink-leaf", "non-utf8", "near-cap"],
)
def test_a_refused_marker_still_releases_the_lease_and_records_a_failed_run(
    tmp_path: Path, case: str
) -> None:
    from ciao import curation_logs as cl

    vault = _vault(tmp_path)
    log = vault / cr.CURATION_LOG_RELATIVE
    watched = log
    if case == "duplicate-marker":
        log.write_bytes(b"---\nlast_full_pass: 2026-09-01\nlast_full_pass: 2026-09-02\n---\n\n# Curation log\n")
    elif case == "unclosed-frontmatter":
        log.write_bytes(b"---\nlast_full_pass: 2026-09-01\n# Curation log\n")
    elif case == "oversized":
        log.write_bytes(b"x" * (cl.LOG_MAX_BYTES + 1))
    elif case == "symlink-leaf":
        target = tmp_path / "elsewhere.md"
        target.write_bytes(b"keep me")
        log.unlink(missing_ok=True)
        log.symlink_to(target)
        watched = target
    elif case == "non-utf8":
        log.write_bytes(b"# Curation log\n\xff\xfe broken\n")
    else:
        log.write_bytes(b"x" * (cl.LOG_MAX_BYTES - 10))
    before = watched.read_bytes()

    lease = cr.begin_run(vault, holder="run")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS), holder=lease.holder)
    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, today=date(2026, 9, 19)
    )

    assert summary["full_pass_advanced"] is False
    assert summary["status"] == "failed"
    assert summary["marker_error"]
    state = cr.load_state(vault)
    assert state.lease == {}
    assert state.last_run["status"] == "failed"
    assert any("weekly marker not advanced" in reason for reason in state.last_run["reasons"])
    assert watched.read_bytes() == before


def test_a_marker_update_inside_the_cap_succeeds_without_growing_the_log(tmp_path: Path) -> None:
    from ciao import curation_logs as cl

    vault = _vault(tmp_path)
    head = b"---\nlast_full_pass: 2026-09-01\n---\n\n# Curation log\n"
    body = head + b"y" * (cl.LOG_MAX_BYTES - 10 - len(head))
    (vault / cr.CURATION_LOG_RELATIVE).write_bytes(body)
    lease = cr.begin_run(vault, holder="run")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS), holder=lease.holder)

    summary = cr.end_run(
        vault, holder=lease.holder, status="ok", advance_marker=True, today=date(2026, 9, 19)
    )

    after = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()
    assert summary["full_pass_advanced"] is True
    assert len(after) == len(body) <= cl.LOG_MAX_BYTES
    assert after.startswith(b"---\nlast_full_pass: 2026-09-19\n---\n")


def test_the_append_refuses_when_the_lease_expires_during_a_slow_read(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    log = vault / cr.CURATION_LOG_RELATIVE
    _fresh_log(vault, last_full_pass="2026-09-01")
    before = log.read_bytes()
    lease = cr.begin_run(vault, holder="run", ttl_s=60, now=_DISPATCHED)
    instants = iter([_DISPATCHED, _DISPATCHED + timedelta(seconds=61)])

    with pytest.raises(cr.CurationBusy):
        cr.append_log(
            vault, holder=lease.holder, log="curation", payload=b"late", clock=lambda: next(instants)
        )
    assert log.read_bytes() == before


def test_the_marker_refuses_when_the_lease_expires_before_publication(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    log = vault / cr.CURATION_LOG_RELATIVE
    _fresh_log(vault, last_full_pass="2026-09-01")
    before = log.read_bytes()
    lease = cr.begin_run(vault, holder="run", ttl_s=60, now=_DISPATCHED)
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS), holder=lease.holder, now=_DISPATCHED)

    with pytest.raises(cr.CurationBusy):
        cr.end_run(
            vault,
            holder=lease.holder,
            status="ok",
            advance_marker=True,
            today=date(2026, 9, 19),
            now=_DISPATCHED,
            clock=lambda: _DISPATCHED + timedelta(seconds=61),
        )
    assert log.read_bytes() == before


def test_a_read_only_log_refuses_and_its_bytes_survive(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    log = vault / cr.CURATION_LOG_RELATIVE
    log.write_bytes(b"# Curation log\nkept\n")
    os.chmod(log, 0o444)
    lease = cr.begin_run(vault, holder="run")

    with pytest.raises(cr.CurationStateError):
        cr.append_log(vault, holder=lease.holder, log="curation", payload=b"report")
    assert log.read_bytes() == b"# Curation log\nkept\n"


def test_the_closer_with_a_trailing_space_is_read_and_written_the_same_way(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    log = vault / cr.CURATION_LOG_RELATIVE
    log.write_bytes(b"--- \nlast_full_pass: 2026-09-01\n--- \n\n# Curation log\n")
    assert cr.read_last_full_pass(log) == "2026-09-01"

    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    assert cr.advance_full_pass(vault, today=date(2026, 9, 19)) is True
    assert cr.read_last_full_pass(log) == "2026-09-19"
    assert log.read_bytes().count(b"last_full_pass") == 1


# ── The stale-note coverage status ────────────────────────────────────────


def _coverage_worklist(vault: Path, guide: Path, today: date = date(2026, 9, 19)) -> cr.Worklist:
    return cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=guide.parent,
        today=today,
    )


def _coverage(vault: Path, guide: Path, today: date = date(2026, 9, 19)) -> dict[str, object]:
    coverage = _coverage_worklist(vault, guide, today).note_coverage
    assert coverage is not None
    return coverage.as_dict()


def _assert_every_flagged_note_is_accounted_for(coverage: dict[str, object]) -> None:
    accounted = sum(
        int(coverage[key])
        for key in (
            "planned",
            "never_queued",
            "unreadable",
            "settled",
            "eligible_deferred",
            "unexamined",
        )
    )
    assert accounted == coverage["flagged"], coverage


def test_coverage_with_nothing_flagged_is_complete_and_empty(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Fresh.md", updated="2026-09-18")

    assert _coverage(vault, guide) == {
        "status": "complete",
        "scanned": 1,
        "exempt": 0,
        "undated": 0,
        "flagged": 0,
        "never_queued": 0,
        "unreadable": 0,
        "settled": 0,
        "planned": 0,
        "eligible_deferred": 0,
        "unexamined": 0,
        "oldest_deferred_age_days": None,
        "cap": cr.STALE_NOTE_MAX_ITEMS,
    }


def test_coverage_under_the_cap_plans_every_flagged_note(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for name in ("Ada", "Bea", "Cy"):
        _note(vault, f"People/{name}.md", updated="2024-01-05")

    coverage = _coverage(vault, guide)

    assert coverage["status"] == "complete"
    assert coverage["flagged"] == 3
    assert coverage["planned"] == 3
    assert coverage["eligible_deferred"] == 0
    assert coverage["unexamined"] == 0
    assert coverage["oldest_deferred_age_days"] is None
    _assert_every_flagged_note_is_accounted_for(coverage)


def test_coverage_over_the_cap_names_the_deferred_and_unexamined_notes(tmp_path: Path) -> None:
    """Three more stale notes than the cap: the one past the cap is read and found
    eligible, so it is deferred, and the two behind it are never opened, so they
    are unexamined. The age reported is the deferred note's."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(cr.STALE_NOTE_MAX_ITEMS + 3):
        _note(
            vault,
            f"People/Note{index:02d}.md",
            updated=(date(2024, 1, 1) + timedelta(days=index)).isoformat(),
        )

    coverage = _coverage(vault, guide, today)

    assert coverage == {
        "status": "partial",
        "scanned": cr.STALE_NOTE_MAX_ITEMS + 3,
        "exempt": 0,
        "undated": 0,
        "flagged": cr.STALE_NOTE_MAX_ITEMS + 3,
        "never_queued": 0,
        "unreadable": 0,
        "settled": 0,
        "planned": cr.STALE_NOTE_MAX_ITEMS,
        "eligible_deferred": 1,
        "unexamined": 2,
        "oldest_deferred_age_days": (
            today - (date(2024, 1, 1) + timedelta(days=cr.STALE_NOTE_MAX_ITEMS))
        ).days,
        "cap": cr.STALE_NOTE_MAX_ITEMS,
    }
    _assert_every_flagged_note_is_accounted_for(coverage)


def test_a_mixed_vault_accounts_for_every_flagged_note_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import note_verification as nv

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    today = date(2026, 9, 19)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    # Flagged but never queued: a Workspace/ file the review queue cannot show.
    _note(vault, "Workspace/Scratch.md", updated="2024-01-05")
    # Flagged, settled by a check inside its cooldown.
    settled = _note(vault, "People/Settled.md", updated="2024-01-05")
    nv.record_note_check(
        vault,
        nv.NoteCheck(
            relative_path="People/Settled.md",
            content_revision=mr.content_revision(settled.read_text(encoding="utf-8")),
            outcome=nv.UNVERIFIED,
            checked_at=today,
            retry_after=today + timedelta(days=nv.CHECK_COOLDOWN_DAYS),
        ),
    )
    # Flagged and planned.
    _note(vault, "People/Sofia.md", updated="2024-01-05")
    # Flagged, but its body cannot be read for a revision. The scan decodes every
    # note it lists, so a note it cannot decode never reaches this pass; the read
    # failure is simulated at the seam the pass actually uses.
    _note(vault, "People/Unreadable.md", updated="2024-01-05")
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self: Path) -> bytes:
        if self.name == "Unreadable.md":
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    # Not flagged: no usable date (its mtime is the epoch, so there is no date at
    # all), and an exempt event type.
    undated = _note(vault, "People/NoDate.md", updated="not-a-date")
    os.utime(undated, (0, 0))
    _note(vault, "journal/2024-01-05-standup.md", type_="journal")

    coverage = _coverage(vault, guide, today)

    assert coverage == {
        "status": "partial",
        "scanned": 6,
        "exempt": 1,
        "undated": 1,
        "flagged": 4,
        "never_queued": 1,
        "unreadable": 1,
        "settled": 1,
        "planned": 1,
        "eligible_deferred": 0,
        "unexamined": 0,
        "oldest_deferred_age_days": None,
        "cap": cr.STALE_NOTE_MAX_ITEMS,
    }
    _assert_every_flagged_note_is_accounted_for(coverage)


def test_no_note_past_the_cap_break_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read sentinel at the seam the claim is about: the note pass's own body reads.

    Only reads made while `_stale_note_items` runs are recorded, so the entry pass
    (which reads bodies later, outside this function) does not pollute the record.
    A read-ahead past the cap break would open the sixth note's neighbours and
    fail this test.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(cr.STALE_NOTE_MAX_ITEMS + 3):
        _note(
            vault,
            f"People/Note{index:02d}.md",
            updated=(date(2024, 1, 1) + timedelta(days=index)).isoformat(),
        )
    real_stale_note_items = cr._stale_note_items
    real_read_bytes = Path.read_bytes
    inside_note_pass = False
    opened: list[str] = []

    def recording_note_pass(**kwargs):
        nonlocal inside_note_pass
        inside_note_pass = True
        try:
            return real_stale_note_items(**kwargs)
        finally:
            inside_note_pass = False

    def sentinel_read_bytes(self: Path) -> bytes:
        if inside_note_pass and self.parent.name == "People":
            opened.append(self.name)
        return real_read_bytes(self)

    monkeypatch.setattr(cr, "_stale_note_items", recording_note_pass)
    monkeypatch.setattr(Path, "read_bytes", sentinel_read_bytes)

    worklist = _coverage_worklist(vault, guide)

    assert opened == [f"Note{index:02d}.md" for index in range(cr.STALE_NOTE_MAX_ITEMS + 1)]
    assert worklist.note_coverage is not None
    assert worklist.note_coverage.unexamined == 2


def test_a_vault_with_only_an_unreadable_note_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Unreadable.md", updated="2024-01-05")
    real_read_bytes = Path.read_bytes

    def flaky_read_bytes(self: Path) -> bytes:
        if self.name == "Unreadable.md":
            raise OSError("permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)

    coverage = _coverage(vault, guide)

    assert coverage["status"] == "partial"
    assert coverage["unreadable"] == 1
    assert coverage["undated"] == 0
    assert coverage["planned"] == 0
    _assert_every_flagged_note_is_accounted_for(coverage)


def test_a_vault_with_only_an_undated_note_is_partial(tmp_path: Path) -> None:
    """An exempt journal with no usable date is not counted as undated: the
    exempt set is its own category and it is reported under `exempt`."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    undated = _note(vault, "People/NoDate.md", updated="not-a-date")
    os.utime(undated, (0, 0))
    journal = _note(vault, "journal/undated-standup.md", updated="not-a-date", type_="journal")
    os.utime(journal, (0, 0))

    coverage = _coverage(vault, guide)

    assert coverage["status"] == "partial"
    assert coverage["flagged"] == 0
    assert coverage["undated"] == 1
    assert coverage["exempt"] == 1
    _assert_every_flagged_note_is_accounted_for(coverage)


def test_coverage_leaves_the_items_the_plan_and_their_order_alone(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(cr.STALE_NOTE_MAX_ITEMS + 3):
        _note(
            vault,
            f"People/Note{index:02d}.md",
            updated=(date(2024, 1, 1) + timedelta(days=index)).isoformat(),
        )

    worklist = _coverage_worklist(vault, guide)
    bare = dc_replace(worklist, note_coverage=None)

    stale = [item for item in worklist.items if item.pass_id == cr.PASS_STALE_NOTE]
    assert [item.keys for item in stale] == [
        (cr.item_key(cr.PASS_STALE_NOTE, f"People/Note{index:02d}.md"),)
        for index in range(cr.STALE_NOTE_MAX_ITEMS)
    ]
    assert worklist.as_dict() | {"note_coverage": None} == bare.as_dict() | {"note_coverage": None}
    assert cr.plan_run(worklist).as_dict() == cr.plan_run(bare).as_dict()


def test_coverage_is_the_same_on_a_repeated_build(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    for index in range(7):
        _note(vault, f"People/Note{index:02d}.md", updated="2024-01-05")

    first = _coverage_worklist(vault, guide).note_coverage
    second = _coverage_worklist(vault, guide).note_coverage

    assert first is not None
    assert first == second


def test_a_failed_vault_scan_is_reported_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import vault_index

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    _note(vault, "People/Sofia.md", updated="2024-01-05")

    def broken_scan(*_args: object, **_kwargs: object):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(vault_index, "scan_vault", broken_scan)

    coverage = _coverage(vault, guide)

    assert coverage["status"] == "unavailable"
    assert coverage["scanned"] == 0
    assert coverage["flagged"] == 0
    assert coverage["planned"] == 0


def test_a_missing_vault_is_reported_as_unavailable(tmp_path: Path) -> None:
    guide = _guide(tmp_path)

    worklist = cr.build_worklist(
        vault_root=tmp_path / "no-such-vault",
        workspace="no-such-vault",
        guide_path=guide,
        category_registry=_categories(tmp_path / "no-such-vault"),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert worklist.note_coverage is not None
    assert worklist.note_coverage.status == "unavailable"


def test_worklist_as_dict_carries_coverage_only_when_it_is_known() -> None:
    plain = cr.Worklist((), False, "", "now")
    assert "note_coverage" not in plain.as_dict()

    coverage = cr.NoteCoverage(
        status="complete",
        scanned=0,
        exempt=0,
        undated=0,
        flagged=0,
        never_queued=0,
        unreadable=0,
        settled=0,
        planned=0,
        eligible_deferred=0,
        unexamined=0,
        oldest_deferred_age_days=None,
        cap=cr.STALE_NOTE_MAX_ITEMS,
    )
    with_coverage = cr.Worklist((), False, "", "now", note_coverage=coverage)
    assert with_coverage.as_dict()["note_coverage"] == coverage.as_dict()
