"""Tests for ciao/subagent_loader.py."""

from __future__ import annotations

from pathlib import Path
from ciao.subagent_loader import (
    parse_subagent_file,
    parse_subagent_folder,
    scaffold_subagent,
)


def test_parse_subagent_file(tmp_path: Path):
    sub_file = tmp_path / "researcher.md"
    sub_file.write_text(
        "---\n"
        "description: Deep research assistant\n"
        "---\n\n"
        "# Researcher\n"
        "Analyze complex technical docs.\n",
        encoding="utf-8",
    )

    manifest = parse_subagent_file(sub_file)
    assert manifest is not None
    assert manifest.name == "researcher"
    assert manifest.description == "Deep research assistant"
    assert "Analyze complex technical docs" in manifest.instructions
    assert not manifest.is_folder


def test_parse_subagent_folder(tmp_path: Path):
    folder = tmp_path / "analyst"
    folder.mkdir()
    (folder / "tools").mkdir()
    (folder / "resources").mkdir()

    (folder / "SKILL.md").write_text(
        "---\n"
        "name: analyst\n"
        "description: Data analysis subagent\n"
        "---\n\n"
        "Analyze metrics and charts.\n",
        encoding="utf-8",
    )
    (folder / "tools" / "query.py").write_text("print(1)", encoding="utf-8")
    (folder / "resources" / "schema.json").write_text("{}", encoding="utf-8")

    manifest = parse_subagent_folder(folder)
    assert manifest is not None
    assert manifest.name == "analyst"
    assert manifest.description == "Data analysis subagent"
    assert manifest.is_folder
    assert "query.py" in manifest.tools
    assert any(r.name == "schema.json" for r in manifest.resources)


def test_scaffold_subagent(tmp_path: Path):
    folder = scaffold_subagent(tmp_path, "tester")
    assert folder.exists()
    assert (folder / "SKILL.md").exists()
    assert (folder / "scripts").is_dir()
    assert (folder / "resources").is_dir()
    assert "tester" in (folder / "SKILL.md").read_text(encoding="utf-8")
