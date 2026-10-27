"""`ciao note create`: the module and the control-plane method.

The module tests build a vault under ``tmp_path`` against the stock category
registry. The control-plane tests bind the real methods onto a stub install, the
way ``test_verify_note_op.py`` and ``test_note_related.py`` do.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import async_reads
from ciao import control_plane as cp
from ciao import entity_types, vault_index, vault_lint
from ciao.frontmatter import split_frontmatter
from ciao.mcp_server import OPERATIONS_BY_NAME
from ciao.note_create import (
    MAX_NOTE_FILE_BYTES,
    NoteCreateError,
    create_note,
)

TODAY = date(2026, 10, 27)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Any:
    entity_types.clear_entity_types_cache()
    yield
    entity_types.clear_entity_types_cache()


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _create(vault: Path, **kwargs: Any) -> Any:
    args: dict[str, Any] = {
        "note_type": "person",
        "title": "Sofia Rossi",
        "description": "",
        "tags": None,
        "body": "",
        "path": "",
        "today": TODAY,
    }
    args.update(kwargs)
    return create_note(vault, vault, **args)


def _code(vault: Path, **kwargs: Any) -> str:
    with pytest.raises(NoteCreateError) as info:
        _create(vault, **kwargs)
    return info.value.code


def _write_types(vault: Path, text: str) -> None:
    (vault / entity_types.VAULT_FILENAME).write_text(text, encoding="utf-8", newline="")
    entity_types.clear_entity_types_cache()


def test_creates_a_person_note_in_the_categorys_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    created = _create(vault)

    assert created.relative_path == "People/Sofia Rossi.md"
    assert created.as_dict()["type"] == "person"
    assert (vault / "People" / "Sofia Rossi.md").read_bytes() == (
        b"---\ntype: person\ntitle: Sofia Rossi\ntags: [person]\n"
        b"created: 2026-10-27\nupdated: 2026-10-27\n---\n\n# Sofia Rossi\n"
    )


def test_the_note_passes_the_vault_lint_and_indexes_with_its_type(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _create(vault, description="Product lead at Acme")

    assert vault_lint.run_validation(vault)["frontmatter_errors"] == []
    entries = {e.path.name: e for e in vault_index.scan_vault(vault, workspace="personal")}
    assert entries["Sofia Rossi.md"].type == "person"
    assert entries["Sofia Rossi.md"].updated == "2026-10-27"


def test_description_tags_and_body_are_stamped(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    _create(
        vault,
        description="Product lead",
        tags=["client/acme", "roadmap", "roadmap"],
        body="\nMet at the offsite.\n\n",
    )

    text = (vault / "People" / "Sofia Rossi.md").read_text(encoding="utf-8")
    block, body = split_frontmatter(text) or ("", "")
    keys = [line.split(":")[0] for line in block.splitlines()]
    assert keys == ["type", "title", "description", "tags", "created", "updated"]
    assert "tags: [client/acme, roadmap]" in block
    assert body.endswith("# Sofia Rossi\n\nMet at the offsite.\n")


def test_a_description_with_newlines_is_collapsed_to_one_line(tmp_path: Path) -> None:
    from ciao.note_create import normalize_description

    assert normalize_description("one\n  two\n\nthree ") == "one two three"
    with pytest.raises(NoteCreateError) as info:
        normalize_description("x" * 501)
    assert info.value.code == "note_file_invalid"


def test_an_alias_resolves_to_the_canonical_type(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    created = _create(vault, note_type="log", title="Offsite")

    assert created.type == "journal"
    assert created.relative_path == "journal/Offsite.md"
    assert "type: journal\n" in (vault / "journal" / "Offsite.md").read_text(encoding="utf-8")


def test_an_unknown_type_lists_the_valid_ones(tmp_path: Path) -> None:
    with pytest.raises(NoteCreateError) as info:
        _create(_vault(tmp_path), note_type="nonsense")

    assert info.value.code == "note_type_unknown"
    message = str(info.value)
    assert "person" in message and "journal" in message and "note" in message
    assert "workspace" not in message and "skill-proposal" not in message


def test_a_disabled_category_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_types(vault, "- id: place\n  enabled: false\n")

    assert _code(vault, note_type="place", title="Rome") == "note_type_unknown"


def test_a_hidden_type_is_refused(tmp_path: Path) -> None:
    assert _code(_vault(tmp_path), note_type="workspace") == "note_type_unknown"


def test_an_owner_added_category_routes_to_its_folder(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_types(
        vault, "- id: customer\n  label: Customer\n  kind: entity\n  folder: Customers\n"
    )

    created = _create(vault, note_type="customer", title="Acme")

    assert created.relative_path == "Customers/Acme.md"


def test_a_category_without_a_folder_needs_path(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert _code(vault, note_type="note", title="Draft") == "note_folder_required"
    created = _create(vault, note_type="note", title="Draft", path="Documents/Drafts")
    assert created.relative_path == "Documents/Drafts/Draft.md"


def test_path_overrides_the_category_folder(tmp_path: Path) -> None:
    created = _create(_vault(tmp_path), path="Elsewhere/Deep")

    assert created.relative_path == "Elsewhere/Deep/Sofia Rossi.md"


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../../x", "a\\b", "x.md", ".hidden"])
def test_path_cannot_escape_the_vault(tmp_path: Path, bad: str) -> None:
    vault = _vault(tmp_path)

    assert _code(vault, path=bad) == "note_path_invalid"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["memory-vault"]
    assert [p for p in vault.rglob("*.md")] == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlinked_folder_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "People").symlink_to(outside, target_is_directory=True)

    assert _code(vault) == "note_path_invalid"
    assert list(outside.iterdir()) == []


def test_it_never_overwrites(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    first = _create(vault, description="first")
    before = Path(first.path).read_bytes()

    assert _code(vault, description="second") == "note_exists"
    assert Path(first.path).read_bytes() == before


def test_title_rules(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    for bad in ["", "\n", "x" * 201, "???", ".env"]:
        assert _code(vault, title=bad) == "note_title_invalid"


def test_tag_rules(tmp_path: Path) -> None:
    from ciao.note_create import parse_tags

    vault = _vault(tmp_path)
    for bad in ["bad tag", "#x"]:
        assert _code(vault, tags=[bad]) == "note_tags_invalid"
    assert parse_tags(["", "  "], "person") == ["person"]
    assert parse_tags(None, "person") == ["person"]


def test_no_folder_is_created_when_the_note_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    assert _code(vault, path="New/Dir", tags=["bad tag"]) == "note_tags_invalid"
    assert _code(vault, path="New/Dir", title="???") == "note_title_invalid"
    assert _code(vault, path="New/Dir", note_type="nonsense") == "note_type_unknown"
    assert not (vault / "New").exists()


def test_an_existing_note_leaves_the_folders_alone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _create(vault)
    before = sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*"))

    assert _code(vault) == "note_exists"
    assert sorted(p.relative_to(vault).as_posix() for p in vault.rglob("*")) == before


# ---- control plane ---------------------------------------------------------


class _Install:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.vault = root / "memory-vault"
        (self.vault / "Workspace").mkdir(parents=True, exist_ok=True)

    def agent_root(self, workspace: str) -> Path:
        return self.root

    def workspace_vault_root(self, workspace: str) -> Path:
        return self.vault

    def agent_vault_root(self, workspace: str) -> Path:
        return self.vault

    def workspace(self, name: str) -> Any:
        return SimpleNamespace(name=name)

    def state_path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


def _plane(root: Path) -> Any:
    plane = SimpleNamespace(config=_Install(root))
    plane._workspace = lambda principal: "personal"
    plane._safe_relative = cp.CiaoControlPlane._safe_relative
    plane._workspace_text_file = cp.CiaoControlPlane._workspace_text_file.__get__(plane)
    plane.note_create = cp.CiaoControlPlane.note_create.__get__(plane)
    return plane


def _call(plane: Any, **kwargs: Any) -> Any:
    args: dict[str, Any] = {"note_type": "person", "title": "Sofia Rossi"}
    args.update(kwargs)
    return asyncio.run(plane.note_create(SimpleNamespace(), **args))


@pytest.fixture(autouse=True)
def _reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run_read(key: str, fn: Any, **kwargs: Any) -> Any:
        return fn()

    monkeypatch.setattr(cp, "run_read", run_read)
    assert async_reads is not None


def test_note_create_returns_the_envelope(tmp_path: Path) -> None:
    reply = _call(_plane(tmp_path))

    assert reply["ok"] is True
    data = reply["data"]
    assert data["relative_path"] == "People/Sofia Rossi.md"
    assert data["type"] == "person" and data["title"] == "Sofia Rossi"
    assert data["created"] == date.today().isoformat()
    assert Path(data["path"]).is_file()


def test_prose_comes_from_files_inside_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "desc.txt").write_text("Product\nlead", encoding="utf-8")
    (tmp_path / "body.md").write_text("Met at the offsite.\n", encoding="utf-8")

    reply = _call(
        _plane(tmp_path),
        description_file="desc.txt",
        body_file="body.md",
        tags=["a", "b"],
    )

    text = (tmp_path / "memory-vault" / reply["data"]["relative_path"]).read_text(encoding="utf-8")
    assert "description: Product lead\n" in text
    assert "tags: [a, b]" in text
    assert text.endswith("# Sofia Rossi\n\nMet at the offsite.\n")


def test_a_prose_file_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    with pytest.raises(cp.ControlPlaneError) as info:
        _call(_plane(root), description_file="../secret.txt")

    assert info.value.code == "path_forbidden"
    assert list((root / "memory-vault").rglob("*.md")) == []


def test_bad_prose_files_are_refused(tmp_path: Path) -> None:
    plane = _plane(tmp_path)
    (tmp_path / "big.txt").write_bytes(b"x" * (MAX_NOTE_FILE_BYTES + 1))
    (tmp_path / "fm.md").write_text("---\ntype: x\n---\nbody", encoding="utf-8")

    with pytest.raises(cp.ControlPlaneError) as missing:
        _call(plane, description_file="nope.txt")
    with pytest.raises(cp.ControlPlaneError) as big:
        _call(plane, body_file="big.txt")
    with pytest.raises(cp.ControlPlaneError) as frontmatter:
        _call(plane, body_file="fm.md")

    assert missing.value.code == "file_not_found"
    assert big.value.code == "note_file_invalid"
    assert frontmatter.value.code == "note_body_invalid"


def test_the_notes_land_in_the_callers_workspace_vault(tmp_path: Path) -> None:
    reply = _call(_plane(tmp_path))

    assert Path(reply["data"]["path"]) == tmp_path / "memory-vault" / "People" / "Sofia Rossi.md"


def test_an_error_surfaces_as_a_control_plane_error_with_the_code(tmp_path: Path) -> None:
    plane = _plane(tmp_path)

    with pytest.raises(cp.ControlPlaneError) as unknown:
        _call(plane, note_type="nonsense")
    _call(plane)
    with pytest.raises(cp.ControlPlaneError) as exists:
        _call(plane)
    with pytest.raises(cp.ControlPlaneError) as empty:
        _call(plane, title="  ")

    assert unknown.value.code == "note_type_unknown"
    assert exists.value.code == "note_exists"
    assert empty.value.code == "note_title_invalid"


def test_the_operation_takes_files_not_prose() -> None:
    params = set(inspect.signature(OPERATIONS_BY_NAME["note_create"].fn).parameters)

    assert params == {
        "service", "note_type", "title", "description_file", "tags", "body_file", "path",
    }
