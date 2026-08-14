"""Two-way sync between a project's `context` and its canonical doc.

`context` is the one-liner injected into every user turn as
`[Project context: ...]`; the canonical doc's `description:` frontmatter is
where that same sentence lives on disk. They used to be seeded once at
discovery and then never reconciled - a PWA edit stayed in the projects
registry, a doc edit stayed in the file, and neither side knew.

These cover both directions: `update_project` writing into the frontmatter,
and vault discovery reading it back out.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    ProjectChatManager,
    _set_frontmatter_description,
)


# ── fixtures ───────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _make_project_folder(
    root: Path, folder_name: str, *, description: str = "", body: str = ""
) -> Path:
    """Create memory-vault/personal/projects/active/<folder>/README.md."""
    folder = root / "memory-vault" / "personal" / "projects" / "active" / folder_name
    folder.mkdir(parents=True)
    readme = folder / "README.md"
    readme.write_text(
        f"---\nname: {folder_name}\ndescription: {description}\nstatus: active\n---\n"
        f"# {folder_name}\n{body}",
        encoding="utf-8",
    )
    return readme


def _yaml_head(text: str) -> dict:
    """Parse a document's frontmatter block."""
    assert text.startswith("---")
    return yaml.safe_load(text[3 : text.find("---", 3)])


def _frontmatter(readme: Path) -> dict:
    return _yaml_head(readme.read_text(encoding="utf-8"))


# ── _set_frontmatter_description ───────────────────────────────────────────


def test_replaces_existing_description_and_leaves_the_rest_alone() -> None:
    text = (
        "---\nname: widgets\ndescription: old blurb\nstatus: active\n---\n"
        "# Widgets\n\nBody paragraph.\n"
    )
    out = _set_frontmatter_description(text, "new blurb")
    assert out is not None
    assert "description: \"new blurb\"" in out
    assert "old blurb" not in out
    # Sibling keys and the body survive byte-for-byte.
    assert "name: widgets" in out
    assert "status: active" in out
    assert out.endswith("# Widgets\n\nBody paragraph.\n")


def test_inserts_description_when_the_key_is_absent() -> None:
    text = "---\nname: widgets\nstatus: active\n---\n# Widgets\n"
    out = _set_frontmatter_description(text, "a blurb")
    assert out is not None
    assert yaml.safe_load(out[3 : out.find("---", 3)]) == {
        "name": "widgets",
        "status": "active",
        "description": "a blurb",
    }
    assert out.endswith("# Widgets\n")


def test_creates_a_frontmatter_block_when_the_doc_has_none() -> None:
    out = _set_frontmatter_description("# Widgets\n\nJust prose.\n", "a blurb")
    assert out is not None
    assert out.startswith("---\ndescription: \"a blurb\"\n---\n")
    assert out.endswith("# Widgets\n\nJust prose.\n")


def test_creates_a_frontmatter_block_for_an_empty_doc() -> None:
    out = _set_frontmatter_description("", "a blurb")
    assert out == "---\ndescription: \"a blurb\"\n---\n"


def test_a_rule_in_the_body_is_not_mistaken_for_frontmatter() -> None:
    text = "# Widgets\n\n---\n\nA horizontal rule above.\n"
    out = _set_frontmatter_description(text, "a blurb")
    assert out is not None
    # The block is prepended; the rule stays exactly where it was.
    assert out.startswith("---\ndescription: \"a blurb\"\n---\n")
    assert out.endswith(text)


def test_unterminated_frontmatter_is_left_alone() -> None:
    assert _set_frontmatter_description("---\nname: widgets\n# Widgets\n", "x") is None


def test_replaces_a_block_scalar_value_including_its_continuation_lines() -> None:
    text = (
        "---\nname: widgets\ndescription: |\n  first line\n  second line\n"
        "status: active\n---\n# Widgets\n"
    )
    out = _set_frontmatter_description(text, "one line now")
    assert out is not None
    assert _yaml_head(out) == {
        "name": "widgets",
        "description": "one line now",
        "status": "active",
    }
    assert "second line" not in out


def test_replaces_a_wrapped_flow_scalar() -> None:
    text = (
        "---\nname: widgets\ndescription: a very long blurb\n  that wraps onto\n"
        "  a third line\nstatus: active\n---\n# Widgets\n"
    )
    out = _set_frontmatter_description(text, "short")
    assert out is not None
    assert _yaml_head(out) == {
        "name": "widgets",
        "description": "short",
        "status": "active",
    }
    assert "wraps onto" not in out


def test_an_indented_description_key_is_not_treated_as_top_level() -> None:
    text = "---\nname: widgets\nmeta:\n  description: nested\n---\n# Widgets\n"
    out = _set_frontmatter_description(text, "top level")
    assert out is not None
    head = _yaml_head(out)
    assert head["description"] == "top level"
    assert head["meta"] == {"description": "nested"}


@pytest.mark.parametrize(
    "value",
    [
        "colons: everywhere: like this",
        'quotes "inside" the blurb',
        "a # hash that would start a comment",
        "trailing backslash \\",
        "multi\nline\nblurb",
        "accented crème brûlée",
        "*starts with an asterisk",
        "",
    ],
)
def test_hostile_values_round_trip_through_yaml(value: str) -> None:
    """The failure mode this replaces: an unquoted `description:` that
    `yaml.safe_load` rejects, which drops the whole project's metadata."""
    text = "---\nname: widgets\ndescription: old\n---\n# Widgets\n"
    out = _set_frontmatter_description(text, value)
    assert out is not None
    assert _yaml_head(out)["description"] == value


# ── writeback: PWA edit → doc ──────────────────────────────────────────────


def test_update_project_writes_the_context_into_the_doc(tmp_path: Path) -> None:
    readme = _make_project_folder(tmp_path, "widgets", description="seeded")
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")
    assert project.context == "seeded"

    manager.update_project(project.project_id, context="edited in the PWA")

    assert _frontmatter(readme)["description"] == "edited in the PWA"
    assert manager.get_project(project.project_id).context == "edited in the PWA"


def test_writeback_survives_rediscovery(tmp_path: Path) -> None:
    """The drift regression: an edit must still be there after the next
    discovery pass re-reads the doc, not be reverted by the seeded value."""
    _make_project_folder(tmp_path, "widgets", description="seeded")
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    manager.update_project(project.project_id, context="edited in the PWA")
    refreshed = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    assert refreshed.context == "edited in the PWA"


def test_writeback_preserves_the_doc_body(tmp_path: Path) -> None:
    readme = _make_project_folder(
        tmp_path, "widgets", description="seeded", body="\n## Decisions\n\n- Ship it.\n"
    )
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    manager.update_project(project.project_id, context="new")

    text = readme.read_text(encoding="utf-8")
    assert text.endswith("# widgets\n\n## Decisions\n\n- Ship it.\n")
    assert _frontmatter(readme)["status"] == "active"


def test_update_project_without_a_vault_doc_is_registry_only(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Standalone", "personal")

    updated = manager.update_project(project.project_id, context="no doc here")

    assert updated is not None
    assert updated.context == "no doc here"


def test_a_name_only_update_does_not_touch_the_doc(tmp_path: Path) -> None:
    readme = _make_project_folder(tmp_path, "widgets", description="seeded")
    before = readme.read_text(encoding="utf-8")
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    manager.update_project(project.project_id, name="Renamed")

    assert readme.read_text(encoding="utf-8") == before


# ── sync: doc edit → context ───────────────────────────────────────────────


def test_discovery_picks_up_a_doc_edit_for_an_already_bound_project(
    tmp_path: Path,
) -> None:
    """The half that was missing entirely: once a project was bound to its
    vault folder, discovery refreshed only the doc *path* and skipped the
    frontmatter, so agent- and hand-edits never reached the preamble."""
    readme = _make_project_folder(tmp_path, "widgets", description="seeded")
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")
    assert project.context == "seeded"

    readme.write_text(
        "---\nname: widgets\ndescription: rewritten by the insights fold\n"
        "status: active\n---\n# widgets\n",
        encoding="utf-8",
    )

    refreshed = next(p for p in manager.list_projects() if p.vault_folder == "widgets")
    assert refreshed.context == "rewritten by the insights fold"


def test_an_empty_doc_description_does_not_clear_the_context(tmp_path: Path) -> None:
    readme = _make_project_folder(tmp_path, "widgets", description="seeded")
    manager = _make_manager(tmp_path)
    project = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    readme.write_text(
        "---\nname: widgets\nstatus: active\n---\n# widgets\n", encoding="utf-8"
    )

    refreshed = next(p for p in manager.list_projects() if p.vault_folder == "widgets")
    assert refreshed.context == "seeded"
    assert refreshed.project_id == project.project_id


def test_first_bind_keeps_the_typed_context_and_pushes_it_into_the_doc(
    tmp_path: Path,
) -> None:
    """Adoption is the one moment the registry wins: a context typed in the PWA
    before the vault folder existed is deliberate, and the README it adopts was
    most likely scaffolded."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("widgets", "personal", context="typed first")

    readme = _make_project_folder(tmp_path, "widgets", description="scaffolded")
    bound = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    assert bound.project_id == project.project_id
    assert bound.context == "typed first"
    assert _frontmatter(readme)["description"] == "typed first"


def test_first_bind_adopts_the_doc_description_when_there_is_no_context(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    manager.create_project("widgets", "personal")

    _make_project_folder(tmp_path, "widgets", description="from the doc")
    bound = next(p for p in manager.list_projects() if p.vault_folder == "widgets")

    assert bound.context == "from the doc"


def test_discovery_republishes_only_when_something_changed(tmp_path: Path) -> None:
    """Discovery runs on every list_projects() call; an unconditional event
    would make each sidebar refresh look like a project edit to the PWA."""
    _make_project_folder(tmp_path, "widgets", description="seeded")
    manager = _make_manager(tmp_path)
    manager.list_projects()  # settle initial discovery + events

    seen: list[dict] = []
    manager._events.publish = lambda event: seen.append(event)  # type: ignore[method-assign]
    manager.list_projects()

    assert [e for e in seen if e.get("type") == "project_updated"] == []
