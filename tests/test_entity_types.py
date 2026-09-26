"""Tests for the entity-type registry (#622, track A1).

The registry is additive: nothing reads it yet, so the load-bearing assertion
is the FIRST one. With no vault file, every derived view must be identical to
the hardcoded constant it is going to replace, because the swap (track A2) is
planned against whichever ``develop`` exists then and must not quietly change
the vocabulary, the folder map or the lint's orphan set. Each view is asserted
against the LIVE constant rather than a copy, so a change on either side fails
here instead of at the consumer.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from importlib import resources
from pathlib import Path

import pytest

from ciao import entity_types, memory_audit, vault_index, vault_lint
from ciao.context import entity_tagger


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


def _vault_lint_orphan_candidate_dirs() -> set[str]:
    """The folders ``vault_lint`` actually reports orphans under.

    It is a local inside ``run_validation`` rather than a module constant, so it
    is read out of the source with ``ast`` instead of copied here: the point of
    the assertion is that the linter and the registry cannot drift, and a copy in
    this file would be a third list that drifts silently.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(vault_lint.run_validation)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "orphan_candidate_dirs" for t in node.targets):
            continue
        return set(ast.literal_eval(node.value))
    raise AssertionError("vault_lint.run_validation no longer names orphan_candidate_dirs")


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
    # plus the lower-case names an entry cannot express.
    orphan_dirs = _vault_lint_orphan_candidate_dirs()
    assert registry.orphan_dirs() == frozenset(orphan_dirs)
    assert set(registry.entity_folders()) == orphan_dirs - {"projects", "references"}

    # The tagger's INDEX.md wire format, case variants included.
    assert registry.category_parts() == entity_tagger._CATEGORY_PARTS
    assert len(registry.category_parts()) == 14

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
    assert registry.category_parts() == stock.category_parts()
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
