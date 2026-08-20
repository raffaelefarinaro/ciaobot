"""Read-only survey of a vault root.

The census exists to turn the real vault into a test spec for a later
per-workspace migration, so the tests defend two things: every shape the
migration must handle is reported, and the census itself never modifies the
fixture it surveys. The read-only proof is the most important test in this
file: a census that writes is a migration that already happened.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ciao.workspace_census import format_census, survey_vault


def _note(vault: Path, relative: str, body: str = "# Title\n") -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _snapshot(root: Path) -> dict[str, str]:
    """Hash every file under root, keyed by its relative path."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def _make_fixture(vault: Path) -> None:
    _note(vault, "personal/People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(vault, "personal/Projects/Alpha.md", "---\ntype: project\n---\n# Alpha\n")
    _note(vault, "work/People/Mo.md", "---\ntype: person\n---\n# Mo\n")
    _note(vault, "work/Projects/Beta.md", "---\ntype: project\n---\n# Beta\n")
    _note(vault, "Logs/2026-08-19.md", "# Log\n")
    _note(vault, "Templates/note.md", "# Template\n")
    _note(vault, "INDEX.md", "# Index\n")
    _note(vault, "personal/Resources/attachment.bin", "binary")
    _note(vault, "work/Projects/notes.txt", "text")
    (vault / "personal/Projects").mkdir(parents=True, exist_ok=True)
    (vault / "personal/Projects/Alpha.md").write_text(
        "---\ntype: project\n---\n# Alpha\n", encoding="utf-8"
    )


def test_note_counts_include_excluded_dirs(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert census.note_counts["personal"] == 2
    assert census.note_counts["work"] == 2
    assert census.note_counts["Logs"] == 1
    assert census.note_counts["Templates"] == 1
    assert census.root_notes == 1


def test_non_md_files_counted(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert census.non_md_counts["personal"] == 1
    assert census.non_md_counts["work"] == 1


def test_symlink_reported_and_not_traversed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    _note(vault, "outside/Secret.md", "# Secret\n")
    (vault / "personal").mkdir(parents=True, exist_ok=True)
    (vault / "personal" / "link").symlink_to(vault / "outside", target_is_directory=True)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    targets = [link["target"] for link in census.symlinks]
    assert any("outside" in target for target in targets)
    # `outside/Secret.md` is a real top-level directory of its own, so it
    # counts once under `outside`. It must NOT also land in `personal` via the
    # symlinked `personal/link` directory: `personal` stays at Mo.md + Alpha.md.
    assert census.note_counts["personal"] == 2
    assert census.note_counts["outside"] == 1


def test_max_depth(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    _note(vault, "work/a/b/c/d/Deep.md", "# Deep\n")
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert census.max_depth == 5


def test_duplicate_stems_reported_with_all_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert set(census.duplicate_stems["Mo"]) == {
        "personal/People/Mo.md",
        "work/People/Mo.md",
    }


def test_frontmatter_less_note_reported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert "Logs/2026-08-19.md" in census.no_frontmatter
    assert "Templates/note.md" in census.no_frontmatter
    assert "personal/People/Mo.md" not in census.no_frontmatter


def test_unregistered_directory_reported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    _note(vault, "stray/Note.md", "# Stray\n")
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    assert census.unregistered_dirs == ["Logs", "Templates", "stray"]


def test_registered_workspaces_reported(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["work", "personal"])
    assert census.registered_workspaces == ["personal", "work"]


def test_census_does_not_modify_fixture(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    before = _snapshot(vault)
    survey_vault(vault, registered_workspaces=["personal", "work"])
    after = _snapshot(vault)
    assert after == before


def test_format_census_renders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_fixture(vault)
    census = survey_vault(vault, registered_workspaces=["personal", "work"])
    text = format_census(census)
    assert "Notes per top-level directory" in text
    assert "personal: 2" in text
    assert "Registered workspaces: personal, work" in text


def test_a_loose_non_markdown_file_at_the_vault_root_is_named(tmp_path: Path) -> None:
    """It used to be counted nowhere.

    The top-level loop branches on directory and on root `.md`; a loose `.zip`
    matched neither, and `non_md_counts` is keyed by directory so it had no bucket
    for the root. This is also the exact shape the re-rooting plan reports as
    `unclassified` and refuses on, so a census that stays silent about it hides the
    one thing most likely to block a migration.
    """
    vault = tmp_path / "memory-vault"
    (vault / "personal" / "People").mkdir(parents=True)
    (vault / "personal" / "People" / "Sam.md").write_text(
        "---\ntype: person\n---\n# Sam\n", encoding="utf-8"
    )
    (vault / "INDEX.md").write_text("<!-- generated -->\n", encoding="utf-8")
    (vault / "export.zip").write_bytes(b"PK\x03\x04")
    (vault / "screenshot.png").write_bytes(b"\x89PNG")

    census = survey_vault(vault, registered_workspaces=["personal"])

    assert census.root_notes == 1
    assert census.root_non_md == ["export.zip", "screenshot.png"]
    assert census.as_dict()["root_non_md"] == ["export.zip", "screenshot.png"]
    assert "(root): 2" in format_census(census)
    assert "export.zip" in format_census(census)


def test_a_clean_vault_root_reports_no_loose_files(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    (vault / "personal" / "People").mkdir(parents=True)
    (vault / "INDEX.md").write_text("x\n", encoding="utf-8")

    census = survey_vault(vault, registered_workspaces=["personal"])

    assert census.root_non_md == []
    assert "(root): 0" in format_census(census)
