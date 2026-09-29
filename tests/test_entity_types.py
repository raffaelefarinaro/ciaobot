"""Tests for the entity-type registry (#622, track A1; consumed by #626, A2a-1).

With no vault file, every derived view must be identical to the hardcoded
constant it replaced: the consumers read this registry, so a change on either
side would quietly change the vocabulary, the folder map or the lint's orphan
set. Each view is asserted against the LIVE constant rather than a copy, so a
change on either side fails here instead of at the consumer — and, since #626,
the last test asserts the same thing about the four consumers themselves rather
than about a literal one of them used to hold.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import pytest

from ciao import config, entity_types, memory_audit, vault_index, vault_lint, vault_rehome


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Keep the module-level cache out of the other tests' results.

    The cache is process-wide by design, so without this a registry built in one
    test (against a tmp vault or a cleared cache) could be the one a later test
    reads.
    """
    entity_types.clear_entity_types_cache()
    yield
    entity_types.clear_entity_types_cache()


def test_stock_registry_reproduces_the_hardcoded_constants(tmp_path: Path) -> None:
    """No vault file: every view equals the constant it will eventually feed."""
    registry = entity_types.load_entity_types(tmp_path)

    # The closed `type:` vocabulary, the folder -> type inference and the
    # near-duplicate spellings a real vault used, all three from vault_index.
    assert registry.canonical_types() == vault_index.CANONICAL_TYPES
    assert registry.dir_type_map() == vault_index.DIR_TYPE_MAP
    assert len(registry.dir_type_map()) == 15
    assert registry.aliases() == vault_index.TYPE_ALIASES
    assert len(registry.aliases()) == 9

    # The two aging overrides over memory_audit's default (which stays there).
    assert registry.stale_thresholds() == memory_audit.STALE_NOTE_THRESHOLDS_DAYS

    # The folders the orphan linter watches: the entity categories' own folders
    # plus the lower-case names an entry cannot express. This used to be read
    # out of `vault_lint.run_validation`'s source with `ast`, because the linter
    # held a literal of its own and the point of the assertion was that the two
    # could not drift. Since #626 the linter asks the registry for this view, so
    # there is no second list left to read — the two are now structurally the
    # same expression. `_ORPHAN_EXTRA_DIRS` is the name of the part of the view
    # no entry can express, and `test_no_vault_file_is_identical_for_every_wired_
    # consumer` below pins the linter's own output against the constants.
    assert registry.orphan_dirs() == (
        frozenset(registry.entity_folders()) | entity_types._ORPHAN_EXTRA_DIRS
    )
    assert len(registry.entity_folders()) == 5


    # Every stock category carries a real one-liner: the description is the point
    # of the feature (it is what tells the agent when to use the category).
    assert all(entry.description.strip() for entry in registry.entries())

    # And it is package data, not just a file in the checkout: the registry read
    # it through `resources`, and a wheel without the `package-data` line would
    # have made every assertion above fail on an install.
    assert resources.files("ciao.stock").joinpath(entity_types.STOCK_FILENAME).is_file()


def test_load_is_cached_by_mtime_and_a_missing_file_is_stock_only(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    vault.mkdir()

    first = entity_types.load_entity_types(vault)
    assert first.canonical_types() == vault_index.CANONICAL_TYPES
    assert first is entity_types.load_entity_types(vault), "unchanged file must be served from cache"

    (vault / "entity-types.yaml").write_text(
        "- id: customer\n  label: Customer\n  kind: entity\n  folder: Customers\n",
        encoding="utf-8",
    )
    second = entity_types.load_entity_types(vault)
    assert second is not first, "a rewritten file must rebuild"
    assert "customer" in second.canonical_types()
    assert "customer" not in first.canonical_types(), "a built registry is never mutated in place"
    assert second is entity_types.load_entity_types(vault), "the rewrite is cached too"

    entity_types.clear_entity_types_cache()
    assert entity_types.load_entity_types(vault) is not second

    # Deleting the file falls back to stock rather than keeping the old merge.
    (vault / "entity-types.yaml").unlink()
    assert entity_types.load_entity_types(vault).canonical_types() == vault_index.CANONICAL_TYPES


def test_a_vault_entry_overrides_a_stock_entry_by_id(tmp_path: Path) -> None:
    stock = entity_types.load_entity_types(tmp_path)
    (tmp_path / "entity-types.yaml").write_text(
        "- id: person\n"
        "  label: Human\n"
        "  folder: Humans\n"
        "  stale_after_days: 45\n"
        # Stating a key with its default is a real instruction, not a silence:
        # a project the user does not want aged out at all.
        "- id: project\n"
        "  stale_after_days: 0\n",
        encoding="utf-8",
    )
    registry = entity_types.load_entity_types(tmp_path)

    assert registry.dir_type_map()["Humans"] == "person"
    assert "People" not in registry.dir_type_map()
    assert registry.stale_thresholds() == {"person": 45}
    assert registry.orphan_dirs() == frozenset(
        (stock.orphan_dirs() - {"People"}) | {"Humans"}
    )

    person = registry.get("person")
    stock_person = stock.get("person")
    assert person is not None and stock_person is not None
    assert person.label == "Human"
    assert person.builtin is True, "a stock id stays builtin even when overridden"

    # A partial override: the keys the file did not name are still stock's, and
    # every other category is untouched.
    assert person.description == stock_person.description
    assert person.kind == stock_person.kind
    assert person.folder != stock_person.folder
    assert registry.canonical_types() == stock.canonical_types()
    assert registry.aliases() == stock.aliases()
    assert [entry.id for entry in registry.entries()] == [
        entry.id for entry in stock.entries()
    ]


def test_a_new_vault_entry_is_appended_as_custom(tmp_path: Path) -> None:
    stock = entity_types.load_entity_types(tmp_path)
    (tmp_path / "entity-types.yaml").write_text(
        "- id: customer\n"
        "  label: Customer\n"
        "  kind: entity\n"
        "  folder: Customers\n"
        "  description: A company we sell to or support.\n"
        "  aliases: [client, account]\n"
        "  stale_after_days: 60\n"
        "  builtin: true\n",
        encoding="utf-8",
    )
    registry = entity_types.load_entity_types(tmp_path)

    assert registry.canonical_types() == stock.canonical_types() | {"customer"}
    assert registry.dir_type_map()["Customers"] == "customer"
    assert registry.aliases() == {**stock.aliases(), "client": "customer", "account": "customer"}
    assert registry.stale_thresholds() == {**stock.stale_thresholds(), "customer": 60}
    assert "Customers" in registry.entity_folders()
    assert "Customers" in registry.orphan_dirs(), "a new entity category joins the orphan watch"

    customer = registry.get("customer")
    assert customer is not None
    assert customer.builtin is False, "builtin comes from origin, never from the file"
    assert [entry.id for entry in registry.entries()] == [
        *(entry.id for entry in stock.entries()),
        "customer",
    ], "a new id is appended, not shuffled into the middle"


def test_a_malformed_vault_file_fails_closed_to_stock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file the user broke must not break indexing, and must say so."""
    path = tmp_path / "entity-types.yaml"
    stock = entity_types.load_entity_types(tmp_path)

    for label, text in (
        ("invalid YAML", "- id: person\n  label: Person\n   folder: [unclosed\n"),
        ("not a list", "person: {label: Person}\n"),
        ("entry is not a mapping", "- just a string\n"),
        ("empty label", '- id: person\n  label: ""\n'),
        ("unknown kind", "- id: person\n  label: Person\n  kind: widget\n"),
        ("negative threshold", "- id: person\n  label: Person\n  stale_after_days: -5\n"),
        ("aliases not names", "- id: person\n  label: Person\n  aliases: [1, 2]\n"),
    ):
        path.write_text(text, encoding="utf-8")
        entity_types.clear_entity_types_cache()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=entity_types.__name__):
            registry = entity_types.load_entity_types(tmp_path)

        assert registry.canonical_types() == stock.canonical_types(), label
        assert registry.dir_type_map() == stock.dir_type_map(), label
        assert registry.aliases() == stock.aliases(), label
        assert [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ], f"{label} fell back silently"


def test_disabling_a_category_removes_it_from_the_effective_views(tmp_path: Path) -> None:
    stock = entity_types.load_entity_types(tmp_path)
    (tmp_path / "entity-types.yaml").write_text(
        "- id: idea\n  enabled: false\n",
        encoding="utf-8",
    )
    registry = entity_types.load_entity_types(tmp_path)

    assert registry.canonical_types() == stock.canonical_types() - {"idea"}
    assert "Ideas" not in registry.dir_type_map()
    assert registry.entity_folders() == tuple(f for f in stock.entity_folders() if f != "Ideas")
    assert "Ideas" not in registry.orphan_dirs()
    idea = registry.get("idea")
    assert idea is not None, "a disabled category is still a row in the list"
    assert idea.enabled is False
    assert [entry.id for entry in registry.entries()] == [
        entry.id for entry in stock.entries()
    ], "disabling never removes a builtin; a user can switch it back on"


def test_no_vault_file_is_identical_for_the_bootstrap_and_rehome(tmp_path: Path) -> None:
    """No `<vault>/entity-types.yaml`: bootstrap and re-home both read the
    shipped list.

    These are the consumers #635 put the registry in front of (the entity
    tagger was the third, removed in #723), and each is checked against the
    constant it replaced. Re-home CAN be handed a registry and is also checked
    against the same answer that way, because a caller that passes one has to
    get what the consumer would have loaded for itself or the two forms of the
    same call drift; that plumbing is pinned in `tests/test_vault_rehome.py`.

    The bootstrap is the exception, and cannot be handed a registry at all: it runs
    inside `CiaoConfig.__post_init__`, before any workspace is known, so its
    evidence folders are stock-derived by construction and a vault file cannot
    widen them. That is asserted here as the derived view it is; the literal it
    replaced, and a vault naming a new entity folder that still does not claim
    one, are pinned in `tests/test_config_role.py`.

    A2a-1 (#626) wires the read path — the indexer, the linter, staleness — and
    asserts the same "identical with no vault file" for those four consumers in
    this same file. The two bodies are one test split across the two changes.
    """
    vault = tmp_path / "memory-vault"
    (vault / "personal" / "People").mkdir(parents=True)
    (vault / "work").mkdir(parents=True)
    (vault / "personal" / "People" / "Alba.md").write_text(
        "---\ntype: person\ntags: [colleague]\n---\n# Alba\n", encoding="utf-8"
    )
    (vault / "work" / "alpha.md").write_text(
        "---\ntype: project\n---\n# Alpha\n", encoding="utf-8"
    )

    registry = entity_types.load_entity_types(vault)

    # Workspace bootstrap: a containment test, so it takes the entity categories'
    # folders and adds the three names an entry's single folder cannot express.
    assert config._WORKSPACE_EVIDENCE_DIRS == frozenset(
        {"People", "Projects", "Places", "Ideas", "Resources", "Workspace", "journal", "projects"}
    )
    assert config._WORKSPACE_EVIDENCE_DIRS == (
        frozenset(registry.entity_folders()) | config._WORKSPACE_EVIDENCE_EXTRA_DIRS
    )
    assert list(config._bootstrap_registry(vault)) == ["personal"], (
        "one workspace per vault directory holding an evidence folder"
    )


    # Re-home: the person folder, and the folder map whose keys are not workspace
    # names. Both are the shipped ones, and the misfiled note is still found.
    assert vault_rehome.people_dirs(registry) == vault_rehome.people_dirs() == frozenset(
        {"People"}
    )
    assert vault_rehome.dir_type_map(registry) == vault_rehome.dir_type_map()
    assert vault_rehome.dir_type_map() == vault_index.DIR_TYPE_MAP
    candidates = {c.path: c for c in vault_rehome.detect_misfiled_people(vault)}
    assert [
        (path, candidate.bucket, candidate.destination)
        for path, candidate in candidates.items()
    ] == [("personal/People/Alba.md", "mechanical", "work/People/Alba.md")]


def test_no_vault_file_is_identical_for_the_index_lint_and_staleness(tmp_path: Path) -> None:
    """No `<vault>/entity-types.yaml`: the four consumers read the shipped list.

    `scan_vault`, `canonical_type`, `run_validation` and `note_threshold_days`
    are the modules #626 put the registry in front of, and each is checked
    twice: against the live constant it replaced, and against the same answer
    when the caller hands the loaded registry over explicitly. A caller that
    passes a registry has to get what the consumer would have loaded for itself,
    or the two forms of the same call drift.

    `Clients/` is the interesting half. A folder no category claims infers no
    type, is not watched for orphans and reports its `type:` as drift — which is
    precisely what the hardcoded map and the hardcoded orphan set did, and the
    reason a vault that has not configured anything is unchanged by this swap.
    """
    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "Clients").mkdir(parents=True)
    (vault / "People" / "Alba.md").write_text("# Alba\n", encoding="utf-8")
    (vault / "People" / "Ben.md").write_text("---\ntype: doc\n---\n# Ben\n", encoding="utf-8")
    (vault / "Clients" / "Acme.md").write_text(
        "---\ntype: customer\n---\n# Acme\n", encoding="utf-8"
    )

    registry = entity_types.load_entity_types(vault)
    assert registry.canonical_types() == vault_index.CANONICAL_TYPES
    assert registry.dir_type_map() == vault_index.DIR_TYPE_MAP
    assert registry.aliases() == vault_index.TYPE_ALIASES
    assert registry.stale_thresholds() == memory_audit.STALE_NOTE_THRESHOLDS_DAYS

    # Path -> type inference, read off the constant the shipped map is. The
    # workspace half is the same map read for a different question — is this
    # first segment a folder type or a workspace? — so `Clients/` reads as a
    # workspace name here, exactly as it did before the swap.
    entries = vault_index.scan_vault(vault)
    assert {e.path.name: e.type for e in entries} == {
        "Alba.md": "person",
        "Ben.md": "doc",
        "Acme.md": "customer",
    }
    assert {e.path.name: e.workspace for e in entries} == {
        "Alba.md": "personal",
        "Ben.md": "personal",
        "Acme.md": "Clients",
    }

    # The closed set, over the whole range of a real vault's spellings.
    for raw in ("project", "Person", "doc", "hackathon-log", "", "customer", "x"):
        assert vault_index.canonical_type(raw, registry=registry) == vault_index.canonical_type(raw)

    # The linter: an untyped note has no frontmatter, a stock alias and an
    # unclaimed type are both drift (the second naming its target), and an
    # unclaimed folder is not watched for orphans.
    issues = vault_lint.run_validation(vault)
    assert sorted(issues["orphans"]) == ["People/Alba.md", "People/Ben.md"]
    assert sorted(
        (e["kind"], e["source"]) for e in issues["frontmatter_errors"]
    ) == [
        ("missing_frontmatter", "People/Alba.md"),
        ("unknown_type", "Clients/Acme.md"),
        ("unknown_type", "People/Ben.md"),
    ]
    ben = next(
        e for e in issues["frontmatter_errors"] if e["source"] == "People/Ben.md"
    )
    assert "document" in ben["message"]

    # Every horizon, against the constant it replaced.
    for note_type in (*sorted(registry.canonical_types()), "client", "x"):
        expected = memory_audit.STALE_NOTE_THRESHOLDS_DAYS.get(
            note_type, memory_audit.STALE_NOTE_DEFAULT_DAYS
        )
        assert memory_audit.note_threshold_days(note_type) == expected
        assert (
            memory_audit.note_threshold_days(note_type, registry=registry) == expected
        )
