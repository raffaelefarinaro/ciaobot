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
    (root / "desktop" / "src-tauri").mkdir(parents=True)
    (root / "web" / "public").mkdir()
    (root / "ciao" / "web" / "static").mkdir(parents=True)
    for sw in (
        root / "web" / "public" / "sw.js",
        root / "ciao" / "web" / "static" / "sw.js",
    ):
        sw.write_text(
            "const CACHE_NAME = 'ciaobot-v0.2.0'\n"
            "const UNREAD_CACHE = 'ciaobot-unread'\n",
            encoding="utf-8",
        )
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
    (root / "desktop" / "package.json").write_text(
        '{\n  "name": "ciaobot-desktop",\n  "version": "0.1.0"\n}\n',
        encoding="utf-8",
    )
    (root / "desktop" / "package-lock.json").write_text(
        '{\n'
        '  "name": "ciaobot-desktop",\n'
        '  "version": "0.1.0",\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "ciaobot-desktop",\n'
        '      "version": "0.1.0"\n'
        '    }\n'
        '  }\n'
        '}\n',
        encoding="utf-8",
    )
    (root / "desktop" / "src-tauri" / "Cargo.toml").write_text(
        '[package]\nname = "ciaobot-desktop"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "desktop" / "src-tauri" / "Cargo.lock").write_text(
        'version = 4\n\n'
        '[[package]]\n'
        'name = "ciaobot-desktop"\n'
        'version = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "desktop" / "src-tauri" / "tauri.conf.json").write_text(
        '{\n  "productName": "Ciaobot",\n  "version": "0.1.0"\n}\n',
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
    assert versions.desktop == "0.3.0"
    assert versions.desktop_lock == "0.3.0"
    assert versions.desktop_cargo == "0.3.0"
    assert versions.desktop_cargo_lock == "0.3.0"
    assert versions.desktop_tauri == "0.3.0"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n"
        "## v0.3.0 - 2026-07-05\n\n"
        "### Added\n"
        "- feat: add release automation\n"
    )
    assert tmp_path / "web" / "package-lock.json" in touched
    assert tmp_path / "desktop" / "src-tauri" / "Cargo.toml" in touched
    assert tmp_path / "desktop" / "src-tauri" / "Cargo.lock" in touched


def test_apply_release_files_bumps_service_worker_caches(tmp_path: Path) -> None:
    """A stale cache name means clients keep serving the previous build.

    Both copies have to move: web/public/sw.js is the source, and the tracked
    ciao/web/static/sw.js is what the packaged wheel actually serves.
    """
    _write_release_tree(tmp_path)

    touched = apply_release_files(
        tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
    )

    for sw in (
        tmp_path / "web" / "public" / "sw.js",
        tmp_path / "ciao" / "web" / "static" / "sw.js",
    ):
        text = sw.read_text(encoding="utf-8")
        assert "'ciaobot-v0.3.0'" in text
        # The unread cache is user state and must remain stable across releases.
        assert "'ciaobot-unread'" in text
        assert "ciaobot-unread-v0.3.0" not in text
        assert sw in touched


def test_apply_release_files_tolerates_a_missing_service_worker(tmp_path: Path) -> None:
    """A checkout without built PWA output must not break the version bump."""
    _write_release_tree(tmp_path)
    (tmp_path / "ciao" / "web" / "static" / "sw.js").unlink()

    touched = apply_release_files(
        tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
    )

    assert read_versions(tmp_path).pyproject == "0.3.0"
    assert tmp_path / "ciao" / "web" / "static" / "sw.js" not in touched


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


def test_release_gate_blocks_on_types_like_ci_does(monkeypatch, tmp_path: Path) -> None:
    """CI's `test` job blocks on `mypy ciao` and this suite did not, so a type
    error passed every local gate and first surfaced as a red release PR - after
    the branch was cut and pushed."""
    ran: list[list[str]] = []
    monkeypatch.setattr(release_mod, "_run", lambda cmd, cwd=None: ran.append(list(cmd)))

    labels = release_mod._run_checks(tmp_path, skip_frontend=True)

    assert ["mypy", "ciao"] == ran[0][-2:], "type check must run, and run first"
    assert any("pytest" in c for c in ran)
    assert "mypy ciao" in labels
    # CI runs pip-audit, eslint and npm audit with `|| true`, so gating on them
    # here would make a release stricter than the thing it predicts.
    flat = " ".join(" ".join(c) for c in ran)
    assert "pip-audit" not in flat
    assert "audit" not in flat


def test_built_pwa_check_requires_the_shell(tmp_path: Path) -> None:
    static = tmp_path / "ciao" / "web" / "static"
    static.mkdir(parents=True)

    with pytest.raises(ReleaseError, match="is missing"):
        release_mod._check_built_pwa(tmp_path)


def test_built_pwa_check_rejects_a_shell_pointing_at_absent_bundles(
    tmp_path: Path,
) -> None:
    """What a stale build looks like: the shell survives a branch switch, the
    hashed bundle it names does not."""
    static = tmp_path / "ciao" / "web" / "static"
    static.mkdir(parents=True)
    (static / "index.html").write_text(
        '<script type="module" src="/assets/index-GONE.js"></script>',
        encoding="utf-8",
    )

    with pytest.raises(ReleaseError, match="not on disk"):
        release_mod._check_built_pwa(tmp_path)


def test_built_pwa_check_accepts_a_coherent_build(tmp_path: Path) -> None:
    static = tmp_path / "ciao" / "web" / "static"
    (static / "assets").mkdir(parents=True)
    (static / "assets" / "index-OK.js").write_text("//", encoding="utf-8")
    (static / "index.html").write_text(
        '<script type="module" src="/assets/index-OK.js"></script>',
        encoding="utf-8",
    )

    release_mod._check_built_pwa(tmp_path)


def test_dependency_step_reports_the_files_it_wrote(tmp_path: Path, monkeypatch) -> None:
    """The bump's PATHS reach the caller, not just its description.

    Only the description used to come back, so the commit step never learned
    that `uv.lock` had been rewritten and staged the new pin without it.
    """
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("# lock\n", encoding="utf-8")

    import ciao.dependency_updates as depmod

    monkeypatch.setattr(
        depmod, "apply_auto_updates", lambda *_a, **_k: ["pkg (Python: 1 -> 2)"]
    )
    update = type("U", (), {"auto": True})()

    written = release_mod._apply_auto_dependency_updates(
        tmp_path, [update], reinstall=False
    )

    assert (tmp_path / "uv.lock") in written


def test_dependency_step_reports_nothing_when_no_bump_applied(
    tmp_path: Path, monkeypatch
) -> None:
    """No adopted bump means no extra paths to stage."""
    import ciao.dependency_updates as depmod

    monkeypatch.setattr(depmod, "apply_auto_updates", lambda *_a, **_k: [])
    update = type("U", (), {"auto": True})()

    assert release_mod._apply_auto_dependency_updates(
        tmp_path, [update], reinstall=False
    ) == []
    # And a run with no auto candidates at all short-circuits the same way.
    assert release_mod._apply_auto_dependency_updates(
        tmp_path, [type("U", (), {"auto": False})()], reinstall=False
    ) == []


def test_release_commit_refuses_to_leave_a_tracked_file_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """An unstaged tracked file after the commit fails the release loudly.

    This is the backstop for the class of bug above: the tagged commit must not
    carry a dependency pin whose lock stayed in the working tree, because the
    step that breaks on it runs only after the tag exists.
    """
    monkeypatch.setattr(
        release_mod, "_git", lambda root, args, check=False: " M uv.lock"
    )

    with pytest.raises(release_mod.ReleaseError, match="left modified tracked files"):
        release_mod._ensure_nothing_left_behind(tmp_path)


def test_release_commit_ignores_untracked_files(tmp_path: Path, monkeypatch) -> None:
    """Untracked output is not the release's problem.

    The generated PWA bundle is gitignored, and a shared checkout may hold
    another session's untracked work; only a modified TRACKED file means the
    release wrote something it did not commit.
    """
    monkeypatch.setattr(release_mod, "_git", lambda root, args, check=False: "")
    release_mod._ensure_nothing_left_behind(tmp_path)
