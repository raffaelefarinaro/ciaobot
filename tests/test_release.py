from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import ciao.release as release_mod
from ciao.release import (
    CommitSummary,
    ReleaseError,
    _resolve_source_ref,
    apply_release_files,
    bump_version,
    read_versions,
    render_changelog_section,
)


def test_resolve_source_prefers_remote_over_stale_local(tmp_path: Path, monkeypatch) -> None:
    # Cutting a release must use the freshly-fetched origin/<source>, not a
    # same-named local branch that may lag behind (which would silently ship a
    # version missing already-merged PRs).
    calls: list[list[str]] = []

    def fake_git(root, args, check=False):
        calls.append(args)
        if args == ["rev-parse", "--verify", "origin/develop"]:
            return "abc123"  # remote exists
        return "def456"  # local also exists

    monkeypatch.setattr(release_mod, "_git", fake_git)
    assert _resolve_source_ref(tmp_path, "develop") == "origin/develop"
    # The remote was checked first.
    assert calls[0] == ["rev-parse", "--verify", "origin/develop"]


def test_resolve_source_falls_back_to_local_when_no_remote(tmp_path: Path, monkeypatch) -> None:
    def fake_git(root, args, check=False):
        if args == ["rev-parse", "--verify", "origin/develop"]:
            return ""  # no remote (e.g. a tag or local-only branch)
        return "def456"

    monkeypatch.setattr(release_mod, "_git", fake_git)
    assert _resolve_source_ref(tmp_path, "develop") == "develop"


def _write_release_tree(root: Path) -> None:
    (root / "ciao").mkdir()
    (root / "web").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ciao"\nversion = "0.2.0"\n',
        encoding="utf-8",
    )
    (root / "ciao" / "__init__.py").write_text(
        '"""Ciaobot personal assistant server."""\n\n__version__ = "0.2.0"\n',
        encoding="utf-8",
    )
    (root / "web" / "package.json").write_text(
        '{\n  "name": "ciaobot-pwa",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (root / "web" / "package-lock.json").write_text(
        '{\n'
        '  "name": "ciaobot-pwa",\n'
        '  "version": "0.1.0",\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "ciaobot-pwa",\n'
        '      "version": "0.1.0"\n'
        '    }\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )


def test_bump_version_supports_semver_steps() -> None:
    assert bump_version("0.2.3", "patch") == "0.2.4"
    assert bump_version("0.2.3", "minor") == "0.3.0"
    assert bump_version("0.2.3", "major") == "1.0.0"


def test_bump_version_rejects_non_numeric_versions() -> None:
    with pytest.raises(ReleaseError):
        bump_version("0.2", "patch")


def test_render_changelog_section_groups_commit_subjects() -> None:
    section = render_changelog_section(
        "0.3.0",
        date(2026, 7, 5),
        [
            CommitSummary("feat: add release automation", "abc1234"),
            CommitSummary("fix: repair package smoke", "def5678"),
            CommitSummary("docs: explain release flow", "987abcd"),
        ],
    )

    assert "## v0.3.0 - 2026-07-05" in section
    assert "### Added\n- feat: add release automation (`abc1234`)" in section
    assert "### Fixed\n- fix: repair package smoke (`def5678`)" in section
    assert "### Maintenance\n- docs: explain release flow (`987abcd`)" in section


def test_apply_release_files_updates_versions_and_changelog(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)
    section = "## v0.3.0 - 2026-07-05\n\n### Added\n- feat: add release automation"

    touched = apply_release_files(tmp_path, version="0.3.0", changelog_section=section)

    versions = read_versions(tmp_path)
    assert versions.pyproject == "0.3.0"
    assert versions.package == "0.3.0"
    assert versions.pwa == "0.3.0"
    assert versions.package_lock == "0.3.0"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n"
        "## v0.3.0 - 2026-07-05\n\n"
        "### Added\n"
        "- feat: add release automation\n"
    )
    assert tmp_path / "web" / "package-lock.json" in touched


def test_apply_release_files_prepends_existing_changelog(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v0.2.0 - 2026-07-01\n\n- Existing\n",
        encoding="utf-8",
    )

    apply_release_files(
        tmp_path,
        version="0.3.0",
        changelog_section="## v0.3.0 - 2026-07-05\n\n- New",
    )

    changelog = (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith("# Changelog\n\n## v0.3.0 - 2026-07-05\n\n- New\n\n")
    assert "## v0.2.0 - 2026-07-01" in changelog
