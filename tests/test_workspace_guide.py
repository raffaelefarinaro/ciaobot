"""The guide is AGENTS.md, and the migration never loses the memory regions.

Claude Code 2.1.277 reads AGENTS.md only when no CLAUDE.md is present, so the
rename is what makes the change take effect. The guide carries the
``ciao:memory`` and ``ciao:profile`` regions, which is why every path here
asserts the *content* survived, not just that a file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import workspace_guide as wg

REGIONS = (
    "# Workspace Guide\n\n"
    "<!-- ciao:memory:start cap=3000 -->\n"
    "## Agent memory\n"
    "- the user ships on Fridays\n"
    "<!-- ciao:memory:end -->\n\n"
    "<!-- ciao:profile:start cap=1375 -->\n"
    "## User profile\n"
    "- prefers terse answers\n"
    "<!-- ciao:profile:end -->\n"
)


def _remembered(text: str) -> bool:
    return "ships on Fridays" in text and "prefers terse answers" in text


# ----------------------------------------------------------- guide_path
def test_agents_md_is_the_guide(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")
    assert wg.guide_path(tmp_path).name == "AGENTS.md"


def test_a_legacy_claude_md_is_still_read(tmp_path: Path) -> None:
    """The window between upgrading and the migration running must not blind
    every reader of the memory regions."""
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    assert wg.guide_path(tmp_path).name == "CLAUDE.md"
    assert _remembered(wg.guide_path(tmp_path).read_text(encoding="utf-8"))


def test_agents_md_wins_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("stale\n", encoding="utf-8")
    assert wg.guide_path(tmp_path).name == "AGENTS.md"


def test_an_empty_root_points_at_the_new_name(tmp_path: Path) -> None:
    """So a caller creating the guide creates AGENTS.md, not CLAUDE.md."""
    assert wg.guide_path(tmp_path).name == "AGENTS.md"
    assert not wg.guide_path(tmp_path).exists()


# ------------------------------------------------------------ migration
def test_the_shape_ciaobot_used_to_create(tmp_path: Path) -> None:
    """CLAUDE.md real, AGENTS.md a symlink at it — every existing install."""
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")

    assert wg.migrate_root(tmp_path) == "relinked"

    agents = tmp_path / "AGENTS.md"
    assert agents.is_file() and not agents.is_symlink()
    assert not (tmp_path / "CLAUDE.md").exists()
    assert _remembered(agents.read_text(encoding="utf-8"))


def test_a_bare_legacy_guide_is_renamed(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "renamed"

    assert not (tmp_path / "CLAUDE.md").exists()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_this_repos_own_shape_just_drops_the_link(tmp_path: Path) -> None:
    """AGENTS.md real with CLAUDE.md symlinked at it — already the right way
    round, so only the extra name has to go."""
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")

    assert wg.migrate_root(tmp_path) == "relinked"

    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "CLAUDE.md").is_symlink()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_two_real_guides_are_merged_and_backed_up(tmp_path: Path) -> None:
    """A hand-authored AGENTS.md beside the real CLAUDE.md. Neither body may
    be dropped, and the regions live in the CLAUDE.md side."""
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Mine\n- my own rule\n", encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "merged"

    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert _remembered(merged), "memory regions must survive the merge"
    assert "my own rule" in merged, "the user's own guide must survive too"
    assert not (tmp_path / "CLAUDE.md").exists()
    assert "my own rule" in (tmp_path / "AGENTS.md.bak").read_text(encoding="utf-8")


def test_identical_copies_need_no_backup(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "renamed"

    assert not (tmp_path / "AGENTS.md.bak").exists()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    wg.migrate_root(tmp_path)
    before = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "noop"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == before


def test_an_empty_root_is_left_for_the_scaffolder(tmp_path: Path) -> None:
    """The migration renames; it must never create a guide, or it would write
    an empty one over a root whose guide simply has not been seeded yet."""
    assert wg.migrate_root(tmp_path) == "noop"
    assert not (tmp_path / "AGENTS.md").exists()


def test_a_dangling_agents_symlink_is_cleared(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")  # target never existed

    assert wg.migrate_root(tmp_path) == "noop"
    assert not (tmp_path / "AGENTS.md").is_symlink()


# --------------------------------------------------------- whole install
class _Config:
    def __init__(self, roots: list[Path]) -> None:
        self._roots = roots

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        return [(r, r.name) for r in self._roots]


def test_every_root_is_migrated(tmp_path: Path) -> None:
    roots = []
    for name in ("personal", "work"):
        root = tmp_path / name
        root.mkdir()
        (root / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
        (root / "AGENTS.md").symlink_to("CLAUDE.md")
        roots.append(root)

    results = wg.migrate_config(_Config(roots))

    assert set(results.values()) == {"relinked"}
    for root in roots:
        assert _remembered((root / "AGENTS.md").read_text(encoding="utf-8"))
        assert not (root / "CLAUDE.md").exists()


def test_the_startup_entry_point_migrates_unconditionally(tmp_path: Path) -> None:
    """No opt-out: CLAUDE.md is neither created nor required anywhere."""
    root = tmp_path / "personal"
    root.mkdir()
    (root / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_if_needed(_Config([root])) == {str(root): "renamed"}
    assert not (root / "CLAUDE.md").exists()
    assert _remembered((root / "AGENTS.md").read_text(encoding="utf-8"))


def test_an_unreadable_registry_does_not_raise(tmp_path: Path) -> None:
    class Broken:
        def agent_root_targets(self):
            raise RuntimeError("registry gone")

    assert wg.migrate_config(Broken()) == {}


@pytest.mark.parametrize("action", ["relinked", "renamed", "merged"])
def test_reported_actions_are_the_documented_ones(action: str) -> None:
    assert action in wg.migrate_root.__doc__
