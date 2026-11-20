"""ciao.os_support.links and the sync_skills mirrors built on it: relative symlinks."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ciao import sync_skills
from ciao.os_support.links import (
    is_link,
    link_dir,
    link_file,
    link_source,
    link_target,
    points_to,
    remove_link,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_a_directory_link_is_recognised_and_removed_without_its_target(tmp_path: Path) -> None:
    source = tmp_path / "skills" / "demo"
    _write(source / "SKILL.md", "# Demo\n")
    link = tmp_path / ".claude" / "skills" / "demo"
    link.parent.mkdir(parents=True)
    link_dir(source, link, relative_to=link.parent)

    assert is_link(link)
    assert points_to(link, source)
    assert link_source(link) == source.resolve()
    assert "/skills/" in link_target(link)
    remove_link(link)
    assert not os.path.lexists(link)
    assert (source / "SKILL.md").read_bytes() == b"# Demo\n"


def test_a_file_link_mirrors_its_source(tmp_path: Path) -> None:
    source = tmp_path / "commands" / "remember.md"
    _write(source, "# Remember\n")
    link = tmp_path / ".claude" / "commands" / "remember.md"
    link.parent.mkdir(parents=True)
    link_file(source, link, relative_to=link.parent)

    assert is_link(link)
    assert points_to(link, source)
    assert link_source(link) == source.resolve()
    assert link.read_bytes() == b"# Remember\n"
    remove_link(link)
    assert not os.path.lexists(link)
    assert source.read_bytes() == b"# Remember\n"
    assert sorted(p.name for p in link.parent.iterdir()) == []


def test_a_users_own_file_is_never_a_link(tmp_path: Path) -> None:
    own = tmp_path / ".claude" / "commands" / "mine.md"
    _write(own, "# Mine\n")
    assert not is_link(own)
    with pytest.raises(OSError):
        link_target(own)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "demo" / "SKILL.md", "# Demo\n")
    _write(workspace / "commands" / "note-it.md", "# Note it\n")
    _write(workspace / "subagents" / "helper.md", "# Helper\n")
    return workspace


def test_a_sync_links_skills_commands_and_agents(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    for mirror, source in [
        (workspace / ".claude" / "skills" / "demo", workspace / "skills" / "demo"),
        (workspace / ".claude" / "commands" / "note-it.md", workspace / "commands" / "note-it.md"),
        (workspace / ".claude" / "agents" / "helper.md", workspace / "subagents" / "helper.md"),
    ]:
        assert is_link(mirror), mirror
        assert points_to(mirror, source), mirror


def test_a_moved_workspace_heals_on_the_next_sync(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    moved = tmp_path / "moved"
    shutil.move(str(workspace), str(moved))

    sync_skills.sync_workspace_skills(moved, refresh_upstream=False)

    assert points_to(moved / ".claude" / "skills" / "demo", moved / "skills" / "demo")
    assert (moved / ".claude" / "skills" / "demo" / "SKILL.md").read_bytes() == b"# Demo\n"
    assert points_to(moved / ".claude" / "commands" / "note-it.md", moved / "commands" / "note-it.md")


def test_a_source_saved_by_replacement_is_re_linked(tmp_path: Path) -> None:
    """An editor's atomic save gives the source a new inode; the mirror follows."""
    workspace = _workspace(tmp_path)
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    source = workspace / "commands" / "note-it.md"
    replacement = source.with_name(".note-it.md.tmp")
    _write(replacement, "# Note it, edited\n")
    os.replace(replacement, source)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    mirror = workspace / ".claude" / "commands" / "note-it.md"
    assert points_to(mirror, source)
    assert mirror.read_bytes() == b"# Note it, edited\n"


def test_a_mirror_whose_source_is_gone_is_pruned_and_a_users_file_is_kept(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    own = workspace / ".claude" / "commands" / "mine.md"
    _write(own, "# Mine\n")
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    (workspace / "commands" / "note-it.md").unlink()

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    commands = workspace / ".claude" / "commands"
    assert not os.path.lexists(commands / "note-it.md")
    assert own.read_bytes() == b"# Mine\n"


def test_mirrors_list_as_commands_and_agents_only(tmp_path: Path) -> None:
    """Claude Code and the settings views glob `*.md`; nothing else is visible."""
    workspace = _workspace(tmp_path)
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    for folder in (workspace / ".claude" / "commands", workspace / ".claude" / "agents"):
        visible = sorted(p.name for p in folder.glob("*.md"))
        assert all(name.endswith(".md") for name in visible)


def test_posix_makes_the_same_relative_symlinks_as_before(tmp_path: Path) -> None:
    source = tmp_path / "commands" / "x.md"
    _write(source, "x\n")
    link = tmp_path / ".claude" / "commands" / "x.md"
    link.parent.mkdir(parents=True)
    link_file(source, link, relative_to=link.parent)
    assert link.is_symlink()
    assert os.readlink(link) == os.path.relpath(source, link.parent)


def test_a_source_replaced_and_newer_is_simply_re_linked(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    source = workspace / "commands" / "note-it.md"
    replacement = source.with_name(".note-it.md.tmp")
    _write(replacement, "# Note it, v2\n")
    os.replace(replacement, source)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    mirror = workspace / ".claude" / "commands" / "note-it.md"
    assert points_to(mirror, source)
    assert mirror.read_bytes() == b"# Note it, v2\n"
