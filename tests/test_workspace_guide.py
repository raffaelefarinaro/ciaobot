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


def test_a_failed_merge_swap_leaves_the_legacy_guide_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merged body is swapped into place atomically: a crash or a full
    disk mid-write must not leave a truncated CLAUDE.md behind, and no temp
    file may be left in the workspace."""
    import os

    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Mine\n- my own rule\n", encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)

    assert wg.migrate_root(tmp_path) == "failed"

    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == REGIONS
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# Mine\n- my own rule\n"
    assert list(tmp_path.glob("*.merge.tmp")) == []


def test_identical_copies_need_no_backup(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "renamed"
    assert not (tmp_path / "AGENTS.md.bak").exists()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_merge_keeps_live_agents_regions_over_an_empty_legacy(tmp_path: Path) -> None:
    """A seeded, region-empty CLAUDE.md beside a region-carrying AGENTS.md.

    The legacy-wins direction would park the live memory in a backup nobody
    reads (observed live: 25 entries unloaded across two workspaces). The
    side with entries wins instead: the regions stay in the guide, the
    legacy body is folded in, and the backup holds the folded side.
    """
    (tmp_path / "CLAUDE.md").write_text("# Seeded guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "merged"

    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert _remembered(merged), "live regions must stay in the guide"
    assert "Seeded guide" in merged, "the legacy body is folded, not dropped"
    assert not (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "CLAUDE.md.bak").read_text(encoding="utf-8") == "# Seeded guide\n"
    assert not (tmp_path / "AGENTS.md.bak").exists()


def test_merge_of_two_live_guides_still_parks_the_agents_side(tmp_path: Path) -> None:
    """Both sides carry entries: a genuine conflict, and the documented
    legacy-wins direction applies — the agents regions land in the backup
    with a pointer, exactly as before."""
    legacy_regions = REGIONS.replace("ships on Fridays", "legacy fact")
    (tmp_path / "CLAUDE.md").write_text(legacy_regions, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "merged"

    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "legacy fact" in merged
    assert "ships on Fridays" not in merged
    assert "AGENTS.md.bak" in merged
    assert _remembered((tmp_path / "AGENTS.md.bak").read_text(encoding="utf-8"))


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


# ------------------------------------------------------- the git-tracked case
def _git(root: Path, *args: str) -> None:
    import subprocess
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True,
    )


def _status(root: Path) -> str:
    import subprocess
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _tracked_status(root: Path) -> str:
    """Only tracked changes — what the re-root's clean-tree gate judges on.

    An untracked `AGENTS.md.bak` is expected after a merge and is ignored
    there (`--untracked-files=no`), so it must not fail these assertions.
    """
    import subprocess
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_a_tracked_guide_is_renamed_without_dirtying_the_tree(tmp_path: Path) -> None:
    """Regression: a plain rename permanently blocked the workspace re-root.

    `ciao setup` commits the guide, so `Path.rename` left `T AGENTS.md` /
    `D CLAUDE.md` behind. `workspace_reroot.apply` refuses to run against
    uncommitted tracked changes and nothing else commits them
    (`auto_sync_on_start` is off by default), so the re-root was refused on
    every later boot over a rename Ciaobot performed itself.
    """
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    assert wg.migrate_root(tmp_path) == "relinked"

    assert _status(tmp_path) == "", "the workspace must be left committable"
    assert not (tmp_path / "CLAUDE.md").exists()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))
    assert not (tmp_path / "AGENTS.md").is_symlink()


def test_an_untracked_guide_still_migrates(tmp_path: Path) -> None:
    """A workspace with no git repo at all is the other common shape."""
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "renamed"
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_history_follows_the_renamed_guide(tmp_path: Path) -> None:
    """`git mv`, not add+rm: the guide's history has to survive the rename."""
    import subprocess

    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed the guide")

    wg.migrate_root(tmp_path)

    log = subprocess.run(
        ["git", "log", "--follow", "--oneline", "--", "AGENTS.md"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "seed the guide" in log


def test_dropping_a_legacy_symlink_leaves_a_clean_tree(tmp_path: Path) -> None:
    """The shape this repo itself has: AGENTS.md real, CLAUDE.md linked at it.

    Regression: this branch only unlinked, so `git rm --cached` left a staged
    deletion behind — the same dirty tracked tree the rename fix exists to
    avoid, and a change the operator's next bare `git commit` would carry.
    """
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    assert wg.migrate_root(tmp_path) == "relinked"

    assert _status(tmp_path) == ""
    assert not (tmp_path / "CLAUDE.md").exists()
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_a_signing_repo_still_ends_clean(tmp_path: Path) -> None:
    """Regression: `commit.gpgsign = true` failed the commit and left the
    rename staged. A repo-level pre-commit hook did the same. This is
    Ciaobot's own bookkeeping commit, so it skips both."""
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    _git(tmp_path, "config", "commit.gpgsign", "true")

    assert wg.migrate_root(tmp_path) == "renamed"

    assert _status(tmp_path) == ""
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_a_hanging_hook_reports_failed_instead_of_raising(tmp_path, monkeypatch) -> None:
    """`_git` has a timeout, and TimeoutExpired is not an OSError — it used to
    escape the documented "failed" contract and unwind whole callers."""
    import subprocess

    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")

    _git(tmp_path, "init", "-q", ".")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    real = wg._git

    def _hang(root, *args):
        # ls-files answers (so the guide is seen as tracked); the move hangs,
        # which is where a wedged pre-commit or index lock actually bites.
        if args[:1] == ("ls-files",):
            return real(root, *args)
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)

    monkeypatch.setattr(wg, "_git", _hang)

    assert wg.migrate_root(tmp_path) == "failed"
    # And the guide is still readable under its old name, so nothing is lost.
    assert _remembered((tmp_path / "CLAUDE.md").read_text(encoding="utf-8"))
    assert wg.guide_path(tmp_path).name == "CLAUDE.md"


def test_a_merge_never_breaks_the_bounded_regions(tmp_path: Path) -> None:
    """Regression: the line merge treated fenced entries as ordinary lines.

    Two real guides that each gained their own memory entries produced a
    second `:start` marker with a different cap, while the matching `:end`
    was deduplicated away as a duplicate line — an unterminated region that
    later region writes refuse. The incoming entries also landed outside the
    fences, where nothing expires, audits or caps them.
    """
    (tmp_path / "CLAUDE.md").write_text(
        "# Guide\n\n"
        "<!-- ciao:memory:start cap=3000 -->\n- fact A\n<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Guide\n- my own rule\n\n"
        "<!-- ciao:memory:start cap=5000 -->\n- fact B\n<!-- ciao:memory:end -->\n",
        encoding="utf-8",
    )

    assert wg.migrate_root(tmp_path) == "merged"

    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert merged.count("ciao:memory:start") == 1
    assert merged.count("ciao:memory:end") == 1
    assert "cap=5000" not in merged
    assert "- fact A" in merged, "the managed region's entries stay"
    assert "my own rule" in merged, "the user's own body survives"
    assert "fact B" not in merged, "a fenced entry must not land outside the fences"
    # Nothing is lost: the incoming file is kept verbatim, regions included.
    assert "fact B" in (tmp_path / "AGENTS.md.bak").read_text(encoding="utf-8")
    assert "AGENTS.md.bak" in merged, "the merged guide points at the backup"

    # The result is a guide the region tooling still accepts.
    from ciao.memory_tool import read_region

    entries, diags = read_region(tmp_path / "AGENTS.md", "memory")
    assert not diags
    assert any("fact A" in entry for entry in entries)


def test_a_tracked_destination_is_committed_even_when_the_source_is_not(
    tmp_path: Path,
) -> None:
    """Regression: the commit hung off the rename *source* being tracked.

    A tracked AGENTS.md replaced by an untracked CLAUDE.md staged the
    destination's removal and then renamed without committing, leaving
    `D AGENTS.md` plus an untracked AGENTS.md — a dirty tracked tree, so the
    re-root's clean-tree gate refused on every boot.
    """
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    _git(tmp_path, "add", "AGENTS.md")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")  # untracked

    assert wg.migrate_root(tmp_path) == "merged"

    assert _tracked_status(tmp_path) == ""
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_a_region_only_difference_still_points_at_the_backup(tmp_path: Path) -> None:
    """Regression: identical bodies left no unique lines, so the early return
    skipped the notice and the incoming facts vanished with no pointer."""
    body = (
        "# Guide\n\n<!-- ciao:memory:start cap=3000 -->\n"
        "- fact {fact}\n<!-- ciao:memory:end -->\n"
    )
    (tmp_path / "CLAUDE.md").write_text(body.format(fact="A"), encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(body.format(fact="B"), encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "merged"

    merged = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "- fact A" in merged
    assert f"{wg.GUIDE_NAME}.bak" in merged, "the only pointer to the unmerged facts"
    assert "- fact B" in (tmp_path / "AGENTS.md.bak").read_text(encoding="utf-8")


def test_a_legacy_symlink_to_an_external_guide_is_not_discarded(
    tmp_path: Path,
) -> None:
    """Regression: any CLAUDE.md symlink was treated as Ciaobot's own alias.

    One pointing at a shared or external guide is not an alias — it is the
    instructions Claude has been loading. Unlinking it on the fast path
    dropped them out of the canonical guide entirely.
    """
    (tmp_path / "shared.md").write_text("# Team instructions\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Local guide\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("shared.md")

    assert wg.migrate_root(tmp_path) == "merged"

    guide = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Team instructions" in guide, "the loaded instructions must survive"
    assert (tmp_path / "shared.md").is_file(), "the external file is not ours to delete"
    assert (tmp_path / "shared.md").read_text(encoding="utf-8") == "# Team instructions\n", (
        "the merge must materialize locally, never write through the link "
        "into a guide other roots share"
    )
    assert not (tmp_path / "CLAUDE.md").exists()


def test_a_legacy_symlink_that_really_is_our_alias_is_just_dropped(
    tmp_path: Path,
) -> None:
    """The fast path still applies when the link does alias this AGENTS.md."""
    (tmp_path / "AGENTS.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")

    assert wg.migrate_root(tmp_path) == "relinked"

    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "AGENTS.md.bak").exists(), "nothing to back up"
    assert _remembered((tmp_path / "AGENTS.md").read_text(encoding="utf-8"))


def test_a_symlinked_backup_path_is_refused_not_followed(tmp_path: Path) -> None:
    """Security regression: the backup write followed a symlink.

    `AGENTS.md.bak` sits in the workspace, so what is already at that name is
    not necessarily a regular file. A link pointing at a dotfile received the
    incoming guide's bytes through it — and the migration runs unattended at
    startup, before the server binds, so nobody is watching.
    """
    victim = tmp_path / "victim.rc"
    victim.write_text("# the user's real shell config\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# incoming\npayload\n", encoding="utf-8")
    (tmp_path / "AGENTS.md.bak").symlink_to("victim.rc")

    assert wg.migrate_root(tmp_path) == "failed"

    assert victim.read_text(encoding="utf-8") == "# the user's real shell config\n"
    # Refusing leaves BOTH guides exactly as they were rather than
    # half-migrating: the backup is the only copy of what the merge does not
    # fold in, so there is no safe way to continue without it.
    assert _remembered((tmp_path / "CLAUDE.md").read_text(encoding="utf-8"))
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# incoming\npayload\n"
    assert (tmp_path / "AGENTS.md.bak").is_symlink(), "the link itself is left alone"


def test_a_regular_backup_file_is_still_overwritten(tmp_path: Path) -> None:
    """A stale backup from an earlier run is replaced, not appended to."""
    (tmp_path / "CLAUDE.md").write_text(REGIONS, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# mine\n- my rule\n", encoding="utf-8")
    (tmp_path / "AGENTS.md.bak").write_text("stale from last time\n", encoding="utf-8")

    assert wg.migrate_root(tmp_path) == "merged"

    backup = (tmp_path / "AGENTS.md.bak").read_text(encoding="utf-8")
    assert "my rule" in backup
    assert "stale from last time" not in backup
