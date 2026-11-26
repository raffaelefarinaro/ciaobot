"""Edge cases of moving a project folder with every reference repointed."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import project_folder_move as pfm
from ciao.vault_lint import run_validation


def _project(root: Path, slug: str) -> Path:
    folder = root / "projects" / "active" / slug
    folder.mkdir(parents=True)
    (folder / f"{slug}.md").write_text(
        "---\ntype: project\nstatus: active\nupdated: 2026-05-19\n---\n# demo\n\nLive.\n",
        encoding="utf-8",
    )
    return folder


def _move(root: Path, source: Path, destination: Path) -> list[str]:
    return pfm.move_project_folder(root, source, destination, workspace="personal")


def test_a_crlf_note_keeps_its_line_endings_through_a_move(tmp_path: Path) -> None:
    """One link changed, not every line ending in the vault."""
    folder = _project(tmp_path, "demo")
    (tmp_path / "notes").mkdir()
    hub = tmp_path / "notes" / "Hub.md"
    hub.write_bytes(
        b"---\r\ntype: note\r\nupdated: 2026-05-19\r\n---\r\n"
        b"# Hub\r\n\r\nSee [[projects/active/demo/demo]].\r\n"
    )
    before = hub.read_bytes()

    _move(tmp_path, folder, tmp_path / "projects" / "completed" / "demo")

    after = hub.read_bytes()
    assert b"\n" not in after.replace(b"\r\n", b"")
    assert after == before.replace(b"projects/active/demo/demo", b"projects/completed/demo/demo")


def test_a_failed_move_restores_crlf_bytes_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback's promise is byte-exact, and a translated original breaks it."""
    folder = _project(tmp_path, "demo")
    (tmp_path / "notes").mkdir()
    hub = tmp_path / "notes" / "Hub.md"
    hub.write_bytes(
        b"---\r\ntype: note\r\nupdated: 2026-05-19\r\n---\r\n"
        b"# Hub\r\n\r\nSee [[projects/active/demo/demo]].\r\n"
    )
    before = hub.read_bytes()
    project_before = (folder / "demo.md").read_bytes()

    def fail_move(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pfm.shutil, "move", fail_move)
    with pytest.raises(ValueError, match="it was put back"):
        _move(tmp_path, folder, tmp_path / "projects" / "completed" / "demo")

    assert hub.read_bytes() == before
    assert (folder / "demo.md").read_bytes() == project_before
    assert not (tmp_path / "projects" / "completed").exists()


def test_a_percent_encoded_ref_to_a_spaced_project_is_repointed(tmp_path: Path) -> None:
    """A destination with a space in it is written `My%20Project.md`.

    A prefilter comparing the bare stem against that spelling skipped the one file
    that needed rewriting, and the link dangled after the move.
    """
    (tmp_path / "projects").mkdir(parents=True)
    (tmp_path / "projects" / "My Project").mkdir()
    (tmp_path / "projects" / "My Project" / "My Project.md").write_text(
        "---\ntype: project\nstatus: active\nupdated: 2026-05-19\n---\n# My Project\n\nLive.\n",
        encoding="utf-8",
    )
    (tmp_path / "projects" / "Hub.md").write_text(
        "---\ntype: note\nupdated: 2026-05-19\n---\n# Hub\n\n"
        "[the project](My%20Project/My%20Project.md).\n",
        encoding="utf-8",
    )

    _move(
        tmp_path,
        tmp_path / "projects" / "My Project",
        tmp_path / "projects" / "completed" / "My Project",
    )

    hub = (tmp_path / "projects" / "Hub.md").read_text(encoding="utf-8")
    assert "(My%20Project/My%20Project.md)" not in hub
    assert "completed/My Project/My Project.md" in hub
    assert run_validation(tmp_path)["broken_markdown_links"] == []


def test_a_note_edited_under_a_move_is_not_overwritten(tmp_path: Path) -> None:
    """The rewrite was computed against text that no longer exists; refuse it."""
    hub = tmp_path / "Hub.md"
    hub.write_text("---\ntype: note\n---\n# Hub\n\nSee [[demo]].\n", encoding="utf-8")
    stale = pfm._read_exact(hub)
    hub.write_text(
        "---\ntype: note\n---\n# Hub\n\nSee [[demo]], edited by hand.\n", encoding="utf-8"
    )

    with pytest.raises(OSError, match="changed while"):
        pfm._write_texts([(hub, stale, "rewritten")])

    assert "edited by hand" in hub.read_text(encoding="utf-8")
    assert list(tmp_path.glob(".Hub.md.*")) == []


def test_a_note_that_stopped_being_utf8_mid_move_is_rolled_back(tmp_path: Path) -> None:
    """A decode failure unwinds like any other write failure."""
    hub = tmp_path / "Hub.md"
    hub.write_text("---\ntype: note\n---\n# Hub\n\nSee [[demo]].\n", encoding="utf-8")
    stale = pfm._read_exact(hub)
    hub.write_bytes(b"---\ntype: note\n---\n# Hub\n\nSee [[caf\xe9]].\n")
    other = tmp_path / "Other.md"
    other.write_text("---\ntype: note\n---\n# Other\n", encoding="utf-8")

    with pytest.raises((OSError, UnicodeDecodeError)):
        pfm._write_texts(
            [(other, pfm._read_exact(other), "rewritten too"), (hub, stale, "rewritten")]
        )

    assert other.read_text(encoding="utf-8") == "---\ntype: note\n---\n# Other\n"
    assert hub.read_bytes() == b"---\ntype: note\n---\n# Hub\n\nSee [[caf\xe9]].\n"
    assert list(tmp_path.glob(".Hub.md.*")) == []
