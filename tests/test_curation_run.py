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
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ciao import curation_run as cr
from ciao import memory_receipts as mr
from ciao import skill_proposals
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import (
    LearningRecord,
    allocate_learning_id,
    entry_revision,
    render_learning,
)


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
    guide.write_text("# Workspace\n", encoding="utf-8")
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
        encoding="utf-8",
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
    from ciao.entity_types import EntityTypeRegistry, load_entity_types

    return load_entity_types(vault)


# ── Worklist ──────────────────────────────────────────────────────────────


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
        encoding="utf-8",
    )
    (vault / cr.LEARNINGS_RELATIVE).write_text(
        "# Learnings\n\n## Active\n"
        "- [pin-the-node-version] [2026-01-01 → 2026-09-01] (x4) Pin the Node version.\n",
        encoding="utf-8",
    )
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "deploy.md").write_text("x", encoding="utf-8")
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: 2026-09-01\n---\n\n{'x' * (cr.LOG_ROTATION_BYTES + 1)}",
        encoding="utf-8",
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
            f"---\ntype: {type_}\n---\n# Note{index}\n", encoding="utf-8"
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

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
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

    first = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )
    assert cr.PASS_CATEGORIES in {item.pass_id for item in first.items}
    decline_category(vault, "recipe-book")
    assert read_category_sidecar(vault, "recipe-book")["declined"] is True
    # The bullet is gone, as it would be after the owner rejected it.
    (vault / cr.PROPOSALS_RELATIVE).unlink()

    second = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=_categories(vault),
        workspace_dir=tmp_path,
        today=date(2026, 9, 19),
    )

    assert cr.PASS_CATEGORIES not in {item.pass_id for item in second.items}
    assert not (vault / cr.PROPOSALS_RELATIVE).exists()


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

    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, [cr.item_key(cr.PASS_GUIDE, "guide-body")])
    _capture(monkeypatch)
    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="ok"))

    # Recording only the guide review never advances the weekly marker.
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"


def test_queued_region_facts_are_not_work(tmp_path: Path) -> None:
    """`[memory]`/`[profile]` rows stay queued for the user by contract.

    Counting them as work would make a workspace with one pending
    cross-project fact look busy every night forever, and the run would spend
    a model turn re-reading a row it is forbidden to act on.
    """
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.PROPOSALS_RELATIVE).write_text(
        "- [memory] The user prefers uv over pip.\n- [profile] Writes in British English.\n",
        encoding="utf-8",
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
        encoding="utf-8",
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
        encoding="utf-8",
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
        encoding="utf-8",
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
        f"unverified for {age}d against a 90d horizon; "
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


def test_a_custom_category_threshold_is_honoured(tmp_path: Path) -> None:
    """The registry is threaded into the detector, not just the cluster pass.

    A category with its own `stale_after_days` must reach the nightly plan or a
    vault whose categories were retuned would keep re-verifying notes the owner
    said age slowly — and, worse, keep quiet about the ones they said age fast.

    Written through the vault's own `entity-types.yaml`, which is where a real
    owner's threshold lives, so the test exercises the same load path the plan
    does rather than a hand-built registry.
    """
    from ciao import entity_types

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    # `person` ages at 90 days by default, so 2026-06-01 is stale today. A vault
    # that says its people age at 1000 days says the opposite.
    _note(vault, "People/Sofia.md", updated="2026-06-01")
    assert len(_stale_items(vault, guide)) == 1

    (vault / entity_types.VAULT_FILENAME).write_text(
        "- id: person\n"
        "  label: Person\n"
        "  kind: entity\n"
        "  folder: People\n"
        "  stale_after_days: 1000\n",
        encoding="utf-8",
    )
    entity_types.clear_entity_types_cache()
    try:
        assert _stale_items(vault, guide) == []
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
        encoding="utf-8",
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
        encoding="utf-8",
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
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "deploy.md").write_text("x", encoding="utf-8")

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
    assert "1 proposal(s) waiting on a decision" in item.reason


def test_a_budget_limited_run_resumes_at_the_remainder(tmp_path: Path) -> None:
    """Acceptance: no duplicate work and no lost items across two runs."""
    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass=date(2026, 9, 18).isoformat())
    (vault / cr.SKILL_PROPOSALS_RELATIVE).mkdir()
    for name in ("a", "b", "c"):
        (vault / cr.SKILL_PROPOSALS_RELATIVE / f"{name}.md").write_text("x", encoding="utf-8")

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
    (vault / cr.SKILL_PROPOSALS_RELATIVE / "a.md").write_text("x", encoding="utf-8")

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

    assert second.holder == "run-b"


def test_an_expired_lease_does_not_wedge_the_vault(tmp_path: Path) -> None:
    """A crashed run releases its lease by expiring; nothing else can."""
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="crashed", ttl_s=60, now=started)

    assert cr.active_lease(vault, now=started + timedelta(seconds=30)) is not None
    assert cr.active_lease(vault, now=started + timedelta(seconds=120)) is None
    cr.begin_run(vault, holder="next", now=started + timedelta(seconds=120))


def test_an_unreadable_expiry_reads_as_expired(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.begin_run(vault, holder="run-a")
    path = cr.state_path(vault)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lease"]["expires_at"] = "whenever"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cr.active_lease(vault) is None
    cr.begin_run(vault, holder="run-b")


def test_a_run_that_lost_its_lease_cannot_keep_stamping_work_done(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)
    cr.begin_run(vault, holder="run-b", now=started + timedelta(seconds=120))

    with pytest.raises(cr.CurationBusy):
        cr.record_done(vault, ["proposals:x"], holder="run-a")
    assert cr.load_state(vault).done_keys == {}


def test_renew_extends_only_the_holders_own_lease(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    started = datetime(2026, 9, 19, 0, 1, tzinfo=UTC)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=started)

    assert cr.renew_run(vault, holder="run-b", ttl_s=60, now=started) is False
    assert cr.renew_run(vault, holder="run-a", ttl_s=600, now=started) is True
    assert cr.active_lease(vault, now=started + timedelta(seconds=300)) is not None


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
        encoding="utf-8",
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


def test_curation_end_leaves_the_weekly_pass_due_after_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.cli import _curation_end_command

    vault = _vault(tmp_path)
    guide = _guide(tmp_path)
    _fresh_log(vault, last_full_pass="2026-09-01")
    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="failed"))

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
    cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    _curation_end_command(_args(tmp_path, vault, guide, holder="nightly", status="ok"))

    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) != "2026-09-01"
    assert set(cr.load_state(vault).done_keys) == set()


def _stale_holder_replaced(vault: Path) -> None:
    """Leave `run-a`'s lease expired and `run-b` holding the vault."""
    expired = datetime.now(UTC) - timedelta(hours=2)
    cr.begin_run(vault, holder="run-a", ttl_s=60, now=expired)
    cr.begin_run(vault, holder="run-b")


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
    _stale_holder_replaced(vault)
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
    assert (cr.active_lease(vault) or {})["holder"] == "run-b"
    assert ended == 2


def test_the_parser_will_not_let_a_follow_up_command_omit_the_holder() -> None:
    """The flag is required at parse time, not merely honoured when present."""
    from ciao.cli import build_parser

    parser = build_parser()
    for command in ("curation-progress", "curation-end"):
        with pytest.raises(SystemExit):
            parser.parse_args([command])
        assert parser.parse_args([command, "--holder", "run-a"]).holder == "run-a"


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
    _stale_holder_replaced(vault)
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))
    _capture(monkeypatch)

    code = _curation_end_command(
        _args(tmp_path, vault, guide, holder="run-a", status="ok")
    )

    assert code == CURATION_BUSY_EXIT
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-01"
    # And the run it was rejected in favour of still holds the lease.
    assert (cr.active_lease(vault) or {})["holder"] == "run-b"


# ── Shipped instructions ──────────────────────────────────────────────────


def _stock(relative: str) -> str:
    from importlib import resources

    return resources.files("ciao.stock").joinpath(relative).read_text(encoding="utf-8")


def test_the_schedule_starts_from_the_computed_worklist() -> None:
    """The agent must not re-derive by hand what pass 0 already decided."""
    skill = next(entry["prompt"] for entry in json.loads(_stock("schedules.json"))["schedules"] if entry["schedule_id"] == "system-memory-curation")

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
    stale = prompt[prompt.index("**`stale_notes`**") : prompt.index("## 4.")]

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
    assert "`unverified` (facts unchecked past the type's horizon" in prompt
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
        encoding="utf-8",
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
        encoding="utf-8",
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
        "--workspace",
        vault.name,
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
        encoding="utf-8",
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
        encoding="utf-8",
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
        f"unverified for {age}d against a 90d horizon; "
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
