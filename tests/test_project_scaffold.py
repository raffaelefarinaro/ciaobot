"""``create_project(scaffold=True)`` writes and binds the project's vault note."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


WS = "personal"


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
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


def _frontmatter(doc: Path) -> dict:
    text = doc.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---\n")[1])


def test_scaffold_writes_parseable_note_with_colon_in_name_and_context(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("SLC: No-code configurator", WS, "Q4: label tool", scaffold=True)
    slug = "slc-no-code-configurator"
    doc = pcm._vault_active_root(WS) / slug / f"{slug}.md"
    assert project.vault_folder == slug
    assert doc.is_file()
    meta = _frontmatter(doc)
    assert meta["title"] == "SLC: No-code configurator"
    assert meta["description"] == "Q4: label tool"
    assert meta["type"] == "project"
    assert meta["status"] == "active"
    assert pcm._read_project_metadata(doc, "x") == ("SLC: No-code configurator", "Q4: label tool")


def test_scaffold_sets_vault_doc_path_and_persists(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    assert project.vault_doc_path.endswith("q4-launch/q4-launch.md")
    again = _make_manager(tmp_path).get_project(project.project_id)
    assert again is not None
    assert again.vault_folder == "q4-launch"
    assert again.vault_doc_path == project.vault_doc_path


def test_default_create_does_not_touch_the_vault(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Q4 Launch", WS)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "q4-launch").exists()


def test_scaffold_adopts_existing_folder_without_overwriting(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    doc = pcm._vault_active_root(WS) / "q4-launch" / "q4-launch.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("---\ntitle: Mine\n---\n\nhand written\n", encoding="utf-8")
    before = doc.read_bytes()
    project = pcm.create_project("Q4 Launch", WS, "ctx", scaffold=True)
    assert doc.read_bytes() == before
    assert project.vault_folder == "q4-launch"


def test_scaffold_skips_a_slug_bound_to_another_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    first = pcm.create_project("Q4 Launch", WS, scaffold=True)
    doc = pcm._vault_active_root(WS) / "q4-launch" / "q4-launch.md"
    before = doc.read_bytes()
    second = pcm.create_project("q4 launch", WS, scaffold=True)
    assert first.vault_folder == "q4-launch"
    assert second.vault_folder == ""
    assert doc.read_bytes() == before


def test_scaffold_skips_when_completed_folder_exists(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    (pcm._vault_completed_root(WS) / "q4-launch").mkdir(parents=True)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "q4-launch").exists()


def test_scaffold_without_context_omits_description(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    pcm.create_project("Plain", WS, scaffold=True)
    doc = pcm._vault_active_root(WS) / "plain" / "plain.md"
    assert "description" not in _frontmatter(doc)


def test_scaffold_skips_unknown_workspace(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("X", "nowhere", scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._workspace_vault_root("nowhere") / "projects").exists()


def test_scaffold_skips_non_ascii_slug(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Café", WS, scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "café").exists()


def test_scaffold_write_failure_leaves_no_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pcm = _make_manager(tmp_path)

    real_write_text = Path.write_text

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "q4-launch.md":
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", boom)
    project = pcm.create_project("Q4 Launch", WS, scaffold=True)
    assert project.vault_folder == ""
    assert not (pcm._vault_active_root(WS) / "q4-launch").exists()
