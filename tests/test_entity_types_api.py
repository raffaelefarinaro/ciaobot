"""The entity-types edit surface: `GET`/`PATCH /api/memory/entity-types` (#624).

Track A2b is the write half of the category registry that #622 stored: the two
endpoints behind the future Categories UI, and the writes that keep the vault
file and the generated `VOCABULARY.md` in step.

What is pinned here, beyond the shapes, is the set of ways a save can be wrong
that a status code alone would hide: a file that is written with the stock
defaults frozen into it, a disabled category that comes back enabled, a custom
category deleted while its notes still carry its `type:`, a half-written file
the loader would fail closed to stock, and a `VOCABULARY.md` whose Categories
section disagrees with the census printed under it. A PATCH is the whole desired
list, so every one of those has a test that sends the GET's own rows back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from starlette.testclient import TestClient

from ciao import entity_types
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app


def _note(vault: Path, name: str, note_type: str) -> None:
    path = vault / "personal" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: {note_type}\n---\n# {name}\n", encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """A vault with two `type: person` notes and one of an unlisted `customer`."""
    root = tmp_path / "memory-vault"
    _note(root, "Mo", "person")
    _note(root, "Jo", "person")
    _note(root, "Prospect", "customer")
    return root


@pytest.fixture
def client(vault: Path) -> TestClient:
    tmp_path = vault.parent
    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        pwa_auth_required=False,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=vault,
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )
    return TestClient(create_app(cfg))


def _rows(client: TestClient) -> list[dict[str, Any]]:
    """The current effective list, as a PATCH would send it back."""
    body = client.get("/api/memory/entity-types?workspace=personal").json()
    rows: list[dict[str, Any]] = body["types"]
    return rows


def _by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in rows}


def _patch(client: TestClient, rows: list[dict[str, Any]]):
    return client.patch("/api/memory/entity-types?workspace=personal", json={"types": rows})


def _customer_row(**overrides: Any) -> dict[str, Any]:
    """A custom category row, as the Categories form would build it."""
    row: dict[str, Any] = {
        "id": "customer",
        "label": "Customer",
        "kind": "entity",
        "folder": "Customers",
        "description": "An account we sell to or support.",
        "aliases": ["client"],
        "stale_after_days": 60,
        "enabled": True,
    }
    row.update(overrides)
    return row


def _persisted(vault: Path) -> list[dict[str, Any]]:
    """The rows the vault file holds, as data."""
    rows: list[dict[str, Any]] = yaml.safe_load(
        (vault / "entity-types.yaml").read_text(encoding="utf-8")
    )
    return rows


def test_get_returns_stock_plus_counts(client: TestClient) -> None:
    """The list is the vault's configuration, and each row says how used it is."""
    response = client.get("/api/memory/entity-types?workspace=personal")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace"] == "personal"
    assert body["vault"].endswith("memory-vault")
    rows = body["types"]
    # Every stock id, no vault file yet, all of them shipped.
    assert {row["id"] for row in rows} == {
        "person",
        "project",
        "place",
        "idea",
        "resource",
        "product",
        "feature",
        "automation",
        "document",
        "workspace",
        "reference",
        "content",
        "journal",
        "note",
        "log",
        "skill-proposal",
    }
    assert all(row["builtin"] is True for row in rows)
    assert all(row["enabled"] is True for row in rows)

    counts = _by_id(rows)
    assert counts["person"]["note_count"] == 2
    # The `customer` note is drift: a `type:` no category claims. It has no row
    # to inflate and no count to be read from, which is the whole point of
    # showing the list a user can act on rather than a census of the notes.
    assert "customer" not in counts
    assert sum(row["note_count"] for row in rows) == 2


def test_patch_persists_an_override_and_a_custom_entry(client: TestClient, vault: Path) -> None:
    """An override and a new category round-trip; stock is neither frozen nor lost."""
    rows = _rows(client)
    rows_by_id = _by_id(rows)
    rows_by_id["person"]["label"] = "Human"
    rows.append(_customer_row())

    response = _patch(client, rows)

    assert response.status_code == 200
    after = _by_id(response.json()["types"])
    assert after["person"]["label"] == "Human"
    assert after["person"]["builtin"] is True, "origin decides builtin, not the file"
    assert after["customer"]["builtin"] is False
    assert after["customer"]["label"] == "Customer"

    # The same list the next GET serves, not just the submitted one echoed back.
    reread = _by_id(_rows(client))
    assert reread["person"]["label"] == "Human"
    assert reread["customer"]["folder"] == "Customers"
    assert reread["customer"]["aliases"] == ["client"]

    written = _persisted(vault)
    assert [row["id"] for row in written] == ["person", "customer"], (
        "only the override and the custom entry are persisted; the other fifteen "
        "stock rows are still the shipped defaults, so an upgrade to them lands"
    )
    assert written[0]["label"] == "Human"
    # An override keeps the fields it did not change, so the file is a complete
    # row rather than a patch to apply by hand.
    assert written[0]["folder"] == "People"
    assert "builtin" not in written[0], "builtin is decided by origin, not stored"
    # The data view of "the file" and the file itself cannot drift apart.
    assert entity_types.user_entries(entity_types.load_entity_types(vault)) == written


def test_patch_regenerates_vocabulary_with_a_categories_section(
    client: TestClient, vault: Path
) -> None:
    """The file the memory agent reads names the categories, above the census."""
    rows = _rows(client)
    _by_id(rows)["person"]["label"] = "Human"
    _by_id(rows)["idea"]["enabled"] = False
    rows.append(_customer_row())

    assert _patch(client, rows).status_code == 200

    text = (vault / "VOCABULARY.md").read_text(encoding="utf-8")
    header, marker, after = text.partition("## Categories")
    assert marker, "the Categories section is missing"
    assert header.startswith("<!-- generated by ciao vault-index")
    section, _, census = after.partition("## Types (canonical")
    assert "- `customer` (Customer) → Customers — An account we sell to or support." in section
    assert "- `person` (Human) → People" in section
    # A disabled category is not a `type:` any note should take, so it is left
    # out of the section rather than struck through in it.
    assert "`idea`" not in section
    # Above the type census, so the configured list reads before the used one,
    # and the two are visibly two lists.
    assert census.strip(), "the type census is still rendered"
    canonical, _, drift = census.partition("## Types (drift")
    assert "`person`" in canonical
    assert "- `customer`" not in canonical, (
        "the census is the hardcoded set until the consumers read the registry, "
        "so a new category is still reported as drift below it"
    )
    assert "`customer`" in drift


@pytest.mark.parametrize(
    "label, body",
    [
        ("id is not a usable type value", {"types": [{"id": "Customer", "label": "C"}]}),
        (
            "the list names one id twice",
            {"types": [{"id": "customer", "label": "A"}, {"id": "customer", "label": "B"}]},
        ),
        (
            "a folder belongs to one category",
            {"types": [{"id": "customer", "label": "C", "folder": "People"}]},
        ),
        (
            "an alias is not somebody's id",
            {"types": [{"id": "customer", "label": "C", "aliases": ["person"]}]},
        ),
        ("a label is required", {"types": [{"id": "customer", "label": ""}]}),
        (
            "a kind is one of the two",
            {"types": [{"id": "customer", "label": "C", "kind": "bogus"}]},
        ),
        (
            "enabled is a yes or a no",
            {"types": [{"id": "customer", "label": "C", "enabled": "yes"}]},
        ),
        (
            "a negative staleness is a typo",
            {"types": [{"id": "customer", "label": "C", "stale_after_days": -5}]},
        ),
        ("types is not a list", {"types": {"id": "customer"}}),
        ("types is a string", {"types": "customer"}),
        ("there is no types key", {"workspace": "personal"}),
    ],
)
def test_patch_rejects_bad_input(
    client: TestClient, vault: Path, label: str, body: dict[str, Any]
) -> None:
    """Every refusal is a 400 that leaves the vault's file alone."""
    response = client.patch(
        "/api/memory/entity-types?workspace=personal", json=body
    )

    assert response.status_code == 400, label
    assert response.json()["error"], "a refusal has to say what was wrong"
    # A malformed submission must not leave a half-written file behind for the
    # loader to fail closed on, and must not write a valid-looking empty one
    # either: nothing at all is the honest state for a refused save.
    assert not (vault / "entity-types.yaml").exists(), label
    assert not (vault / "VOCABULARY.md").exists(), label


def test_patch_deleting_a_custom_type_with_notes_is_refused(
    client: TestClient, vault: Path
) -> None:
    """A category cannot be deleted out from under the notes that name it."""
    rows = _rows(client)
    rows.append(_customer_row())
    assert _patch(client, rows).status_code == 200
    assert _by_id(_rows(client))["customer"]["note_count"] == 1, (
        "the fixture's `type: customer` note is this category's only user"
    )

    without = [row for row in _rows(client) if row["id"] != "customer"]
    refused = _patch(client, without)

    assert refused.status_code == 400
    assert "customer" in refused.json()["error"]
    assert _by_id(_rows(client))["customer"]["label"] == "Customer", (
        "a refused delete must not have dropped it"
    )
    # Still the same file: the save that was refused did not rewrite it.
    assert [row["id"] for row in _persisted(vault)] == ["customer"]

    # With no note naming it, the same omission is a delete.
    (vault / "personal" / "Prospect.md").unlink()
    assert _patch(client, without).status_code == 200
    assert "customer" not in _by_id(_rows(client))
    assert _persisted(vault) == []


def test_patch_deleting_a_custom_type_whose_alias_names_a_note_is_refused(
    client: TestClient, vault: Path
) -> None:
    """A category's own alias is the category, and its notes still block a delete.

    `canonical_type` knows the static tables only, so before the count resolved a
    `type:` through the registry as well, a note typed with a custom category's
    alias counted as drift under a spelling no row claims: the category read as
    empty, and omitting it passed the delete guard.
    """
    rows = _rows(client)
    rows.append(_customer_row())
    assert _patch(client, rows).status_code == 200
    _note(vault, "Acme", "client")

    assert _by_id(_rows(client))["customer"]["note_count"] == 2, (
        "the fixture's `type: customer` note and this one, whose `type:` is the "
        "alias only the registry knows"
    )

    without = [row for row in _rows(client) if row["id"] != "customer"]
    refused = _patch(client, without)

    assert refused.status_code == 400
    assert "customer" in refused.json()["error"]
    assert _by_id(_rows(client))["customer"]["note_count"] == 2, (
        "a refused delete must not have dropped it or re-counted it"
    )
    assert [row["id"] for row in _persisted(vault)] == ["customer"]

    # A disabled category claims no `type:`, so its alias is drift again — the
    # registry's alias view is enabled-only, which is what keeps the count from
    # crediting a category the user has turned off.
    disabled = _rows(client)
    _by_id(disabled)["customer"]["enabled"] = False
    assert _patch(client, disabled).status_code == 200
    assert _by_id(_rows(client))["customer"]["note_count"] == 1, (
        "only the note carrying the id itself; the alias resolves for nobody"
    )


def test_patch_cannot_delete_a_builtin(client: TestClient) -> None:
    """Omitting a stock id is not deleting it, and is not disabling it either."""
    without = [row for row in _rows(client) if row["id"] != "person"]

    assert _patch(client, without).status_code == 200

    rows = _by_id(_rows(client))
    assert rows["person"]["enabled"] is True, "an omitted builtin stays at its default"
    assert rows["person"]["label"] == "Person"
    assert rows["person"]["builtin"] is True

    # Disabling one is explicit, and reversible the same way. A fresh read, so
    # this PATCH is the current list rather than the pre-save one.
    disabled = [row for row in _rows(client) if row["id"] != "person"]
    for row in disabled:
        if row["id"] == "idea":
            row["enabled"] = False
    assert _patch(client, disabled).status_code == 200
    rows = _by_id(_rows(client))
    assert rows["idea"]["enabled"] is False, "a disabled entry is still a row to turn back on"
    assert rows["person"]["enabled"] is True


def test_get_is_unauthenticated_400_without_workspace(client: TestClient) -> None:
    """`?workspace=` is how the route finds the vault, so its absence is a 400."""
    assert client.get("/api/memory/entity-types").status_code == 400
    assert client.get("/api/memory/entity-types?workspace=").status_code == 400
    unknown = client.get("/api/memory/entity-types?workspace=nope")
    assert unknown.status_code == 400
    assert unknown.json()["error"] == "workspace is required"
    # And a PATCH cannot be the way around the guard: the same query parameter
    # resolves the vault for both methods.
    assert client.patch("/api/memory/entity-types", json={"types": []}).status_code == 400


def test_write_is_atomic_and_leaves_no_temp_file(client: TestClient, vault: Path) -> None:
    """The file is replaced, never truncated: the loader fails closed on a partial one."""
    rows = _rows(client)
    rows.append(_customer_row())
    assert _patch(client, rows).status_code == 200
    assert not list(vault.glob(".entity-types.*"))

    # A second save over the first: same directory, unique temp name, and the
    # file that was there is the one replaced.
    first = (vault / "entity-types.yaml").read_text(encoding="utf-8")
    again = _rows(client)
    _by_id(again)["customer"]["label"] = "Account"
    assert _patch(client, again).status_code == 200
    assert (vault / "entity-types.yaml").read_text(encoding="utf-8") != first
    assert not list(vault.glob(".entity-types.*"))
    assert [path.name for path in sorted(vault.iterdir())] == [
        "VOCABULARY.md",
        "entity-types.yaml",
        "personal",
    ], "the temp file is gone and nothing else was left behind"
