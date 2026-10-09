from __future__ import annotations

import re
import tomllib
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
    render_curated_section,
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
        "{\n"
        '  "name": "ciaobot-pwa",\n'
        '  "version": "0.1.0",\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "ciaobot-pwa",\n'
        '      "version": "0.1.0"\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(UV_LOCK_FIXTURE, encoding="utf-8")


# A minimal but realistic lock: a top-level comment, one dependency block that
# must not move, and the editable root whose ``version`` the release aligns.
UV_LOCK_FIXTURE = (
    "version = 1\n"
    "revision = 3\n"
    "\n"
    "# a dependency block; only the root metadata above may change\n"
    "[[package]]\n"
    'name = "ciao"\n'
    'version = "0.2.0"\n'
    'source = { editable = "." }\n'
    "dependencies = [\n"
    '    { name = "anyio" },\n'
    "]\n"
    "\n"
    "[[package]]\n"
    'name = "anyio"\n'
    'version = "4.4.0"\n'
    'source = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/packages/anyio-4.4.0.tar.gz", '
    'hash = "sha256:deadbeef", size = 123 }\n'
)


def _lock_versions(root: Path) -> dict[str, str]:
    with (root / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    return {pkg["name"]: pkg["version"] for pkg in lock["package"]}


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


_CURATED = """**Heads up:** the default changed.

### New features
- **Rename a workspace** from Settings.

### Bug fixes
- Links failed to pin ([#1167](https://github.com/raffaelefarinaro/ciaobot/issues/1167))
"""


def test_render_curated_section_heads_the_notes_with_the_version() -> None:
    section = render_curated_section("1.3.0", date(2026, 10, 9), _CURATED)

    assert section.startswith("## v1.3.0 - 2026-10-09\n\n**Heads up:**")
    assert "### New features\n- **Rename a workspace**" in section


def test_curated_section_survives_the_release_workflow_extraction() -> None:
    # release-on-main.yml cuts the GitHub release body from CHANGELOG.md with
    # this pattern: from the version heading to the next "## v" heading.
    section = render_curated_section("1.3.0", date(2026, 10, 9), _CURATED)
    changelog = f"# Changelog\n\n{section}\n\n## v1.2.1 - 2026-10-08\n\n- older\n"
    pattern = r"^## v1\.3\.0 -.*?(?=^## v|\Z)"
    match = re.search(pattern, changelog, flags=re.MULTILINE | re.DOTALL)

    assert match is not None
    assert match.group(0).strip() == section


@pytest.mark.parametrize("notes", ["", "   \n", "## Ciaobot 1.3.0\n- x", "intro\n##\n"])
def test_render_curated_section_refuses_empty_or_second_level_headings(notes: str) -> None:
    with pytest.raises(ReleaseError):
        render_curated_section("1.3.0", date(2026, 10, 9), notes)


def test_apply_release_files_updates_versions_and_changelog(tmp_path: Path) -> None:
    _write_release_tree(tmp_path)
    section = "## v0.3.0 - 2026-07-05\n\n### Added\n- feat: add release automation"

    touched = apply_release_files(tmp_path, version="0.3.0", changelog_section=section)

    versions = read_versions(tmp_path)
    assert versions.pyproject == "0.3.0"
    assert versions.package == "0.3.0"
    assert versions.pwa == "0.3.0"
    assert versions.package_lock == "0.3.0"
    assert _lock_versions(tmp_path)["ciao"] == "0.3.0"
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "# Changelog\n\n"
        "## v0.3.0 - 2026-07-05\n\n"
        "### Added\n"
        "- feat: add release automation\n"
    )
    assert tmp_path / "uv.lock" in touched
    assert tmp_path / "web" / "package-lock.json" in touched


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


def test_apply_release_files_aligns_editable_lock_without_dependency_changes(
    tmp_path: Path,
) -> None:
    """The lock's root metadata follows the bump; no dependency byte moves.

    The release rewrites ``pyproject.toml``'s version but used to leave
    ``uv.lock``'s editable root at the old one, so every ``uv --frozen`` step
    failed after the tag. Alignment must touch exactly one version assignment.
    """
    _write_release_tree(tmp_path)
    before = (tmp_path / "uv.lock").read_text(encoding="utf-8")

    touched = apply_release_files(
        tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
    )

    after = (tmp_path / "uv.lock").read_text(encoding="utf-8")
    expected = before.replace('version = "0.2.0"', 'version = "0.3.0"', 1)
    assert after == expected
    assert after.count('version = "0.3.0"') == 1

    versions = _lock_versions(tmp_path)
    assert versions["ciao"] == "0.3.0"
    # The dependency block is untouched, version, source and hash intact.
    assert versions["anyio"] == "4.4.0"
    assert 'source = { registry = "https://pypi.org/simple" }' in after
    assert "sha256:deadbeef" in after
    assert "# a dependency block; only the root metadata above may change" in after
    assert tmp_path / "uv.lock" in touched


def test_editable_lock_alignment_preserves_crlf_and_assignment_spacing(
    tmp_path: Path,
) -> None:
    """Only the quoted root version value changes; every other byte survives.

    ``read_text`` normalizes CRLF to LF, so a Windows checkout was rewritten by
    a release that only meant to bump metadata. The root assignment is also free
    to carry its own spacing and a trailing comment, which a prefix-wide
    replacement would collapse.
    """
    _write_release_tree(tmp_path)
    lock = (
        "version = 1\r\n"
        "revision = 3\r\n"
        "\r\n"
        "[[package]]\r\n"
        'name = "ciao"\r\n'
        '  version   =    "0.2.0"  # keep me\r\n'
        'source = { editable = "." }\r\n'
        "\r\n"
        "[[package]]\r\n"
        'name = "anyio"\r\n'
        'version = "4.4.0"\r\n'
        'source = { registry = "https://pypi.org/simple" }\r\n'
    )
    lock_path = tmp_path / "uv.lock"
    lock_path.write_bytes(lock.encode("utf-8"))
    before = lock_path.read_bytes()

    apply_release_files(
        tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
    )

    after = lock_path.read_bytes()
    expected = before.replace(
        b'  version   =    "0.2.0"', b'  version   =    "0.3.0"'
    )
    assert after == expected
    # Every terminator stays CRLF: no lone LF was introduced.
    assert after.count(b"\n") == after.count(b"\r\n")
    assert b'  version   =    "0.3.0"  # keep me' in after
    assert b'version = "4.4.0"' in after
    assert b"# keep me" in after


def test_editable_lock_alignment_refuses_decoy_version_in_multiline_string(
    tmp_path: Path,
) -> None:
    """A ``version =`` line inside a multiline string must not be the target.

    The narrow assignment regex matches the first such line in the block, which
    can be one buried in a multi-line TOML string. Rewriting that decoy left the
    parsed editable root at the old version while reporting success, so the
    helper now re-parses the edited block and requires the package entry to
    match the original with only its version changed.
    """
    _write_release_tree(tmp_path)
    lock = (
        "version = 1\n"
        "revision = 3\n"
        "\n"
        "[[package]]\n"
        'name = "ciao"\n'
        'description = """\n'
        'version = "decoy"\n'
        '"""\n'
        'version = "0.2.0"\n'
        'source = { editable = "." }\n'
        "\n"
        "[[package]]\n"
        'name = "anyio"\n'
        'version = "4.4.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(lock, encoding="utf-8")
    lock_before = lock_path.read_bytes()
    pyproject_before = (tmp_path / "pyproject.toml").read_bytes()

    with pytest.raises(ReleaseError, match="uv.lock"):
        apply_release_files(
            tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
        )

    assert lock_path.read_bytes() == lock_before
    assert (tmp_path / "pyproject.toml").read_bytes() == pyproject_before


def test_editable_lock_alignment_rejects_non_utf8(tmp_path: Path) -> None:
    """An undecodable lock fails as a path-bearing ReleaseError, before writes.

    A ``UnicodeDecodeError`` escaping the release helper would be an opaque
    crash rather than the requested path-bearing ``ReleaseError``, and running
    it before the version writes is what keeps a bad lock from leaving a
    half-updated tree.
    """
    _write_release_tree(tmp_path)
    (tmp_path / "uv.lock").write_bytes(b"\xff\xfe[[package]]\n")
    pyproject_before = (tmp_path / "pyproject.toml").read_bytes()

    with pytest.raises(ReleaseError, match="uv.lock"):
        apply_release_files(
            tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
        )

    assert (tmp_path / "pyproject.toml").read_bytes() == pyproject_before
    assert (tmp_path / "uv.lock").read_bytes() == b"\xff\xfe[[package]]\n"


def _lock_mutations() -> dict[str, str]:
    missing_version = UV_LOCK_FIXTURE.replace('version = "0.2.0"\n', "", 1)
    return {
        "malformed": '[[package]\nname = "ciao"\n',
        "no_editable_root": UV_LOCK_FIXTURE.replace(
            'source = { editable = "." }',
            'source = { registry = "https://pypi.org/simple" }',
        ),
        "duplicate_root": UV_LOCK_FIXTURE + "\n[[package]]\n"
        'name = "ciao"\n'
        'version = "0.1.0"\n'
        'source = { editable = "." }\n',
        "missing_version": missing_version,
    }


@pytest.mark.parametrize("case", [*sorted(_lock_mutations()), "missing_lock"])
def test_apply_release_files_rejects_invalid_editable_lock(
    tmp_path: Path, case: str
) -> None:
    """A lock the release cannot align exactly fails closed before any write.

    Writing new versions around a lock it could not fix would recreate the
    ``uv --frozen`` failure this issue exists to close, so the validation runs
    before the first release file is touched.
    """
    _write_release_tree(tmp_path)
    if case == "missing_lock":
        (tmp_path / "uv.lock").unlink()
    else:
        (tmp_path / "uv.lock").write_text(_lock_mutations()[case], encoding="utf-8")

    snapshot = {
        name: (tmp_path / name).read_text(encoding="utf-8")
        for name in ("pyproject.toml", "ciao/__init__.py", "web/package.json")
    }
    lock_before = (
        (tmp_path / "uv.lock").read_text(encoding="utf-8")
        if (tmp_path / "uv.lock").exists()
        else None
    )

    with pytest.raises(ReleaseError, match="uv.lock"):
        apply_release_files(
            tmp_path, version="0.3.0", changelog_section="## v0.3.0 - 2026-07-05\n"
        )

    for name, text in snapshot.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == text
    if lock_before is not None:
        assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == lock_before


def test_main_skip_dependency_check_aligns_and_stages_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--skip-dep-check`` still aligns and stages the editable lock root.

    The whole point of the flag is to leave dependency bytes alone, so the
    release cannot rely on the dependency updater to make the lock consistent.
    With it skipped, the version bump alone must align the root metadata and
    ``git add`` must include the lock.
    """
    _write_release_tree(tmp_path)
    # main() requires the PWA versions to already match pyproject.toml.
    (tmp_path / "web" / "package.json").write_text(
        '{\n  "name": "ciaobot-pwa",\n  "version": "0.2.0"\n}\n',
        encoding="utf-8",
    )
    (tmp_path / "web" / "package-lock.json").write_text(
        "{\n"
        '  "name": "ciaobot-pwa",\n'
        '  "version": "0.2.0",\n'
        '  "packages": {\n'
        '    "": {\n'
        '      "name": "ciaobot-pwa",\n'
        '      "version": "0.2.0"\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    commands: list[list[str]] = []
    monkeypatch.setattr(
        release_mod,
        "_run",
        lambda cmd, **kwargs: commands.append(list(cmd)) or "",
    )

    def fake_git(root, args, check=False):
        if args == ["branch", "--show-current"]:
            return "release/v0.3.0"
        return ""

    monkeypatch.setattr(release_mod, "_git", fake_git)

    dep_calls: list = []
    monkeypatch.setattr(
        release_mod,
        "_apply_auto_dependency_updates",
        lambda *a, **k: dep_calls.append(a) or [],
    )

    result = release_mod.main(
        [
            str(tmp_path),
            "--bump",
            "minor",
            "--apply",
            "--commit",
            "--no-branch",
            "--skip-dep-check",
            "--skip-gws-skills",
            "--skip-checks",
        ]
    )

    assert result == 0
    assert dep_calls == []
    assert _lock_versions(tmp_path)["ciao"] == "0.3.0"
    assert _lock_versions(tmp_path)["anyio"] == "4.4.0"
    add_commands = [cmd for cmd in commands if cmd[:2] == ["git", "add"]]
    assert len(add_commands) == 1
    staged = set(add_commands[0][2:])
    assert "uv.lock" in staged
    assert "pyproject.toml" in staged


def _is_audit_command(command: list[str]) -> bool:
    if not command:
        return False
    executable = Path(command[0]).stem.lower()
    return executable == "pip-audit" or (
        executable == "npm" and command[1:2] == ["audit"]
    )


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
    assert not any(_is_audit_command(command) for command in ran)


@pytest.mark.parametrize(
    "command",
    [
        ["/worktrees/release-audit/.venv/bin/pip-audit"],
        ["/worktrees/release-audit/node/bin/npm", "audit", "--json"],
    ],
)
def test_release_gate_audit_check_matches_exact_invocations(command: list[str]) -> None:
    assert _is_audit_command(command)


def test_release_gate_audit_check_ignores_audit_in_checkout_path() -> None:
    command = [
        "/worktrees/release-audit/.venv/bin/python",
        "-m",
        "mypy",
        "ciao",
    ]

    assert not _is_audit_command(command)


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


def test_release_commit_guard_is_skipped_under_allow_dirty(
    tmp_path: Path, monkeypatch
) -> None:
    """--allow-dirty starts from a modified tree, so a leftover proves nothing.

    The guard runs *after* `git commit`, so raising on the user's own
    pre-existing edits would abort the run with the release commit already on
    the branch — the worst moment to fail.
    """
    monkeypatch.setattr(
        release_mod, "_git", lambda root, args, check=False: " M ciao/config.py"
    )
    release_mod._ensure_nothing_left_behind(tmp_path, allow_dirty=True)

    # Without the flag the same tree still fails: the guard is skipped, not
    # weakened.
    with pytest.raises(release_mod.ReleaseError, match="left modified tracked files"):
        release_mod._ensure_nothing_left_behind(tmp_path, allow_dirty=False)
