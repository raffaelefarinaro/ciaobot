"""Release preparation automation for Ciaobot."""

from __future__ import annotations

import argparse
import json
from ciao.jsonio import read_json_dict
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path


VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ReleaseError(RuntimeError):
    """Raised when release preparation cannot continue."""


@dataclass(frozen=True, slots=True)
class RepoVersions:
    pyproject: str
    package: str
    pwa: str
    package_lock: str


@dataclass(frozen=True, slots=True)
class CommitSummary:
    subject: str
    short_hash: str


@dataclass(frozen=True, slots=True)
class ReleaseFiles:
    pyproject: Path
    package_init: Path
    web_package: Path
    web_lock: Path
    changelog: Path

    @classmethod
    def for_root(cls, root: Path) -> "ReleaseFiles":
        return cls(
            pyproject=root / "pyproject.toml",
            package_init=root / "ciao" / "__init__.py",
            web_package=root / "web" / "package.json",
            web_lock=root / "web" / "package-lock.json",
            changelog=root / "CHANGELOG.md",
        )

    def tracked(self) -> list[Path]:
        return [
            self.pyproject,
            self.package_init,
            self.web_package,
            self.web_lock,
            self.changelog,
        ]


def _require_version(value: str, *, label: str) -> str:
    if not VERSION_RE.match(value):
        raise ReleaseError(f"{label} must be a numeric semver version, got {value!r}")
    return value


def bump_version(current: str, bump: str) -> str:
    """Return the next semver version for ``major``, ``minor``, or ``patch``."""

    _require_version(current, label="current version")
    major, minor, patch = [int(part) for part in current.split(".")]
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"unsupported bump kind: {bump}")


def _read_json(path: Path) -> dict:
    try:
        data = read_json_dict(path)
        return data
    except FileNotFoundError as exc:
        raise ReleaseError(f"missing required file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"invalid JSON in {path}: {exc}") from exc


def _extract_one(pattern: str, text: str, *, path: Path, label: str) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        raise ReleaseError(f"could not find {label} in {path}")
    return match.group(1)


def read_versions(root: Path | str) -> RepoVersions:
    repo = Path(root).resolve()
    files = ReleaseFiles.for_root(repo)
    pyproject_text = files.pyproject.read_text(encoding="utf-8")
    init_text = files.package_init.read_text(encoding="utf-8")
    web_package = _read_json(files.web_package)
    web_lock = _read_json(files.web_lock)

    return RepoVersions(
        pyproject=_extract_one(
            r'^version\s*=\s*"([^"]+)"',
            pyproject_text,
            path=files.pyproject,
            label="project version",
        ),
        package=_extract_one(
            r'^__version__\s*=\s*"([^"]+)"',
            init_text,
            path=files.package_init,
            label="package __version__",
        ),
        pwa=str(web_package.get("version", "")),
        package_lock=str(web_lock.get("version", "")),
    )


def _replace_once(pattern: str, text: str, replacement: str, *, path: Path) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise ReleaseError(f"expected one replacement in {path}, replaced {count}")
    return updated


def _dump_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _update_changelog(existing: str, section: str) -> str:
    section = section.strip()
    if not existing.strip():
        return f"# Changelog\n\n{section}\n"
    if existing.startswith("# Changelog\n"):
        rest = existing[len("# Changelog\n") :].lstrip("\n")
        return f"# Changelog\n\n{section}\n\n{rest.rstrip()}\n"
    return f"# Changelog\n\n{section}\n\n{existing.rstrip()}\n"


def _categorized_subject(subject: str) -> str:
    lower = subject.lower()
    if lower.startswith(("feat", "add")):
        return "Added"
    if lower.startswith(("fix", "bug", "repair")):
        return "Fixed"
    if lower.startswith(("docs", "test", "chore", "ci", "build")):
        return "Maintenance"
    return "Changed"


def render_changelog_section(
    version: str,
    release_date: date,
    commits: list[CommitSummary],
) -> str:
    _require_version(version, label="release version")
    groups: dict[str, list[str]] = {
        "Added": [],
        "Changed": [],
        "Fixed": [],
        "Maintenance": [],
    }
    for commit in commits:
        subject = commit.subject.strip()
        if not subject:
            continue
        suffix = f" (`{commit.short_hash}`)" if commit.short_hash else ""
        groups[_categorized_subject(subject)].append(f"- {subject}{suffix}")

    lines = [f"## v{version} - {release_date.isoformat()}"]
    if not any(groups.values()):
        lines.extend(["", "- No commit summaries found for this release range."])
        return "\n".join(lines)

    for heading in ("Added", "Changed", "Fixed", "Maintenance"):
        entries = groups[heading]
        if not entries:
            continue
        lines.extend(["", f"### {heading}", *entries])
    return "\n".join(lines)


def apply_release_files(
    root: Path | str,
    *,
    version: str,
    changelog_section: str,
) -> list[Path]:
    """Update version-bearing files and return the files touched."""

    _require_version(version, label="release version")
    repo = Path(root).resolve()
    files = ReleaseFiles.for_root(repo)

    pyproject_text = files.pyproject.read_text(encoding="utf-8")
    files.pyproject.write_text(
        _replace_once(
            r'^version\s*=\s*"[^"]+"',
            pyproject_text,
            f'version = "{version}"',
            path=files.pyproject,
        ),
        encoding="utf-8",
    )

    init_text = files.package_init.read_text(encoding="utf-8")
    files.package_init.write_text(
        _replace_once(
            r'^__version__\s*=\s*"[^"]+"',
            init_text,
            f'__version__ = "{version}"',
            path=files.package_init,
        ),
        encoding="utf-8",
    )

    web_package = _read_json(files.web_package)
    web_package["version"] = version
    _dump_json(files.web_package, web_package)

    web_lock = _read_json(files.web_lock)
    web_lock["version"] = version
    packages = web_lock.get("packages")
    if isinstance(packages, dict) and isinstance(packages.get(""), dict):
        packages[""]["version"] = version
    _dump_json(files.web_lock, web_lock)

    existing_changelog = (
        files.changelog.read_text(encoding="utf-8") if files.changelog.exists() else ""
    )
    files.changelog.write_text(
        _update_changelog(existing_changelog, changelog_section),
        encoding="utf-8",
    )
    return files.tracked()


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() if capture and result.stderr else ""
        raise ReleaseError(
            f"command failed ({result.returncode}): {' '.join(cmd)}"
            + (f"\n{detail}" if detail else "")
        )
    return result.stdout.strip() if capture and result.stdout else ""


def _git(root: Path, args: list[str], *, check: bool = True) -> str:
    return _run(["git", *args], cwd=root, capture=True, check=check)


def _latest_release_tag(root: Path) -> str | None:
    tag = _git(
        root,
        ["describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
        check=False,
    )
    return tag or None


def _commit_summaries(
    root: Path,
    *,
    from_ref: str | None,
    to_ref: str,
) -> list[CommitSummary]:
    revision = f"{from_ref}..{to_ref}" if from_ref else to_ref
    output = _git(
        root,
        ["log", "--reverse", "--pretty=format:%s%x1f%h", revision],
        check=False,
    )
    commits: list[CommitSummary] = []
    for line in output.splitlines():
        if "\x1f" not in line:
            continue
        subject, short_hash = line.split("\x1f", 1)
        commits.append(CommitSummary(subject=subject, short_hash=short_hash))
    return commits


def _ensure_clean(root: Path, *, allow_dirty: bool) -> None:
    if allow_dirty:
        return
    status = _git(root, ["status", "--porcelain"])
    if status:
        raise ReleaseError(
            "working tree is dirty; commit/stash changes or pass --allow-dirty"
        )


def _resolve_source_ref(root: Path, source: str) -> str:
    # Prefer the freshly-fetched remote branch over a same-named local branch.
    # _checkout_release_branch fetches origin/<source> right before this; a
    # local `develop` that lagged origin would otherwise cut the release from a
    # stale tree, silently shipping a version that omits already-merged PRs.
    # Fall back to a local ref only when there is no remote (tags, SHAs,
    # detached work).
    remote = _git(root, ["rev-parse", "--verify", f"origin/{source}"], check=False)
    if remote:
        return f"origin/{source}"
    local = _git(root, ["rev-parse", "--verify", source], check=False)
    if local:
        return source
    raise ReleaseError(
        f"could not resolve release source branch {source!r}; "
        "fetch origin or check out the branch locally"
    )


def _checkout_release_branch(root: Path, *, branch: str, source: str) -> None:
    _run(["git", "fetch", "origin", source], cwd=root, check=False)
    source_ref = _resolve_source_ref(root, source)
    _run(["git", "switch", "-c", branch, source_ref], cwd=root)


def _current_branch(root: Path) -> str:
    branch = _git(root, ["branch", "--show-current"])
    if not branch:
        raise ReleaseError("could not determine current git branch")
    return branch


def _run_checks(root: Path, *, skip_frontend: bool) -> list[str]:
    commands: list[tuple[list[str], Path, str]] = [
        ([sys.executable, "-m", "pytest", "tests/"], root, "pytest tests/"),
    ]
    if not skip_frontend:
        commands.extend(
            [
                (["npm", "run", "test"], root / "web", "cd web && npm run test"),
                (["npm", "run", "build"], root / "web", "cd web && npm run build"),
            ]
        )
    commands.append(
        (
            [
                sys.executable,
                "-m",
                "ciao.package_smoke",
                "--skip-frontend",
            ],
            root,
            "ciao package-smoke --skip-frontend",
        )
    )

    labels: list[str] = []
    for cmd, cwd, label in commands:
        _run(cmd, cwd=cwd)
        labels.append(label)
    return labels


def _pr_body(version: str, changelog_section: str, checks: list[str]) -> str:
    testing = "\n".join(f"- {label}" for label in checks) or "- Not run"
    return f"""## Summary
- Release Ciaobot v{version} to `main`
- Update package, PWA, and package-lock versions
- Add changelog notes for the release range

## Release notes
{changelog_section}

## Testing
{testing}

## After approval
- Merge this PR into `main`
- GitHub Actions will create tag `v{version}`, publish the GitHub release, and sync `develop`
"""


def _check_dependency_updates(root: Path) -> list:
    """Return available dependency updates, or [] if the check cannot run."""
    try:
        from ciao.dependency_review import check_available_updates

        return check_available_updates(root)
    except Exception as exc:  # noqa: BLE001 - never block a release on this
        print(f"Dependency update check skipped: {exc}")
        return []


def _print_dependency_updates(updates: list) -> None:
    print()
    if not updates:
        print("Dependency updates: none available.")
        return
    print("Dependency updates available (review whether to adopt):")
    for u in updates:
        flags = []
        if u.auto:
            flags.append("auto")
        flags.append("safe" if u.is_safe else "major")
        print(
            f"  - {u.key} [{u.ecosystem}] {u.current} -> {u.latest} ({', '.join(flags)})"
        )
    auto = [u.key for u in updates if u.auto]
    if auto:
        print(
            f"  (auto: {', '.join(auto)} will be bumped on --apply; "
            "review the rest and update manually if it makes sense)"
        )


def _apply_auto_dependency_updates(root: Path, updates: list, *, reinstall: bool) -> None:
    if not any(getattr(u, "auto", False) for u in updates):
        return
    try:
        from ciao.dependency_review import apply_auto_updates

        applied = apply_auto_updates(root, updates, reinstall=reinstall)
    except Exception as exc:  # noqa: BLE001 - never block a release on this
        print(f"Auto dependency update skipped: {exc}")
        return
    if applied:
        print(f"Auto-updated dependencies: {', '.join(applied)}")


def _stock_skills_dir(root: Path) -> Path:
    return root / "ciao" / "stock" / "skills"


def _gws_skills_versions(root: Path) -> tuple[str | None, str | None]:
    """Return ``(installed_gws_cli_version, pinned_stock_version)``."""
    try:
        from ciao.gws_skills import installed_gws_version, pinned_gws_version

        return installed_gws_version(), pinned_gws_version(_stock_skills_dir(root))
    except Exception as exc:  # noqa: BLE001 - never block a release on this
        print(f"gws skills version check skipped: {exc}")
        return None, None


def _print_gws_skills_status(installed: str | None, pinned: str | None) -> None:
    print()
    if installed is None:
        print("Stock gws skills: `gws` CLI not found; packaged skills left as-is.")
        return
    if pinned and installed != pinned:
        print(
            f"Stock gws skills: gws CLI {installed} != pinned {pinned}; "
            "will regenerate from the CLI on --apply."
        )
    else:
        print(
            f"Stock gws skills: pinned at {pinned or 'unknown'} (gws CLI {installed}); "
            "--apply still refreshes any upstream doc drift."
        )


def _refresh_stock_gws_skills(root: Path) -> list[Path]:
    """Regenerate the packaged ``gws-*`` skills from the CLI.

    Returns the list of ``SKILL.md`` paths that changed so the caller can add
    them to the release commit. Never raises: a missing/failed ``gws`` CLI just
    leaves the packaged skills untouched.
    """
    try:
        from ciao.gws_skills import regenerate_stock_gws_skills

        result = regenerate_stock_gws_skills(_stock_skills_dir(root), write=True)
    except Exception as exc:  # noqa: BLE001 - never block a release on this
        print(f"Stock gws skills refresh skipped: {exc}")
        return []
    if result.missing:
        print(f"WARN: gws CLI no longer generates: {', '.join(result.missing)}")
    if result.updated:
        print(f"Regenerated stock gws skills: {', '.join(result.updated)}")
    else:
        print("Stock gws skills already current.")
    return [_stock_skills_dir(root) / name / "SKILL.md" for name in result.updated]


def _print_plan(
    *,
    root: Path,
    current: RepoVersions,
    version: str,
    from_ref: str | None,
    changelog_to_ref: str,
    source: str,
    branch: str,
    changelog_section: str,
    apply: bool,
    commit: bool,
    push: bool,
    create_pr: bool,
) -> None:
    print(f"Repository: {root}")
    print(f"Current package version: {current.pyproject}")
    if current.pwa != current.pyproject:
        print(f"Current PWA version: {current.pwa} (will be aligned)")
    print(f"Next version: {version}")
    print(f"Changelog range: {from_ref or '<entire history>'}..{changelog_to_ref}")
    print(f"Release source: {source}")
    print(f"Release branch: {branch}")
    print(f"Apply changes: {'yes' if apply else 'no'}")
    print(f"Commit: {'yes' if commit else 'no'}")
    print(f"Push: {'yes' if push else 'no'}")
    print(f"Create PR: {'yes' if create_pr else 'no'}")
    print()
    print(changelog_section)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a Ciaobot release branch, changelog, and draft PR."
    )
    parser.add_argument("repo_root", nargs="?", default=".")
    version_group = parser.add_mutually_exclusive_group()
    version_group.add_argument("--version", help="Explicit release version, e.g. 0.3.0.")
    version_group.add_argument(
        "--bump",
        choices=["patch", "minor", "major"],
        default="patch",
        help="Version bump to apply when --version is omitted.",
    )
    parser.add_argument("--from-ref", help="Git ref/tag to start changelog from.")
    parser.add_argument("--to-ref", default="HEAD", help="Git ref to end changelog at.")
    parser.add_argument("--base", default="main", help="Pull request base branch.")
    parser.add_argument(
        "--source",
        default="develop",
        help="Branch to cut the release branch from (default: develop).",
    )
    parser.add_argument("--branch", help="Release branch name. Defaults to release/vX.Y.Z.")
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Release date for CHANGELOG.md.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write files. Without this flag, only print the release plan.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow applying on a dirty working tree.",
    )
    parser.add_argument(
        "--no-branch",
        action="store_true",
        help="Apply changes on the current branch instead of creating a release branch.",
    )
    parser.add_argument("--commit", action="store_true", help="Commit release files.")
    parser.add_argument("--push", action="store_true", help="Push the release branch.")
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="Create a GitHub draft PR with gh after pushing.",
    )
    parser.add_argument(
        "--ready",
        action="store_true",
        help="Create a ready-for-review PR instead of a draft PR.",
    )
    parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="Do not run pytest, frontend tests/build, or package smoke.",
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip frontend test/build checks; package smoke still runs.",
    )
    parser.add_argument(
        "--skip-dep-check",
        action="store_true",
        help=(
            "Do not check PyPI/npm for newer dependency versions or "
            "auto-bump the Claude SDK."
        ),
    )
    parser.add_argument(
        "--skip-gws-skills",
        action="store_true",
        help=(
            "Do not regenerate the packaged gws-* stock skills from the "
            "installed gws CLI."
        ),
    )

    args = parser.parse_args(argv)
    root = Path(args.repo_root).expanduser().resolve()
    if not root.is_dir():
        raise ReleaseError(f"repo root does not exist: {root}")

    current = read_versions(root)
    if current.pyproject != current.package:
        raise ReleaseError(
            "pyproject.toml and ciao.__version__ are out of sync: "
            f"{current.pyproject} != {current.package}"
        )
    version = (
        _require_version(args.version, label="release version")
        if args.version
        else bump_version(current.pyproject, args.bump)
    )
    release_date = date.fromisoformat(args.date)
    branch = args.branch or f"release/v{version}"
    changelog_to_ref = args.to_ref
    if changelog_to_ref == "HEAD" and not args.no_branch:
        _run(["git", "fetch", "origin", args.source], cwd=root, check=False)
        try:
            changelog_to_ref = _resolve_source_ref(root, args.source)
        except ReleaseError:
            changelog_to_ref = "HEAD"
    from_ref = args.from_ref or _latest_release_tag(root)
    commits = _commit_summaries(root, from_ref=from_ref, to_ref=changelog_to_ref)
    changelog_section = render_changelog_section(version, release_date, commits)

    if args.create_pr:
        args.push = True
    if args.push:
        args.commit = True

    _print_plan(
        root=root,
        current=current,
        version=version,
        from_ref=from_ref,
        changelog_to_ref=changelog_to_ref,
        source=args.source,
        branch=branch,
        changelog_section=changelog_section,
        apply=args.apply,
        commit=args.commit,
        push=args.push,
        create_pr=args.create_pr,
    )

    dep_updates: list = []
    if not args.skip_dep_check:
        dep_updates = _check_dependency_updates(root)
        _print_dependency_updates(dep_updates)

    if not args.skip_gws_skills:
        gws_installed, gws_pinned = _gws_skills_versions(root)
        _print_gws_skills_status(gws_installed, gws_pinned)

    if not args.apply:
        return 0

    _ensure_clean(root, allow_dirty=args.allow_dirty)
    if not args.no_branch:
        _checkout_release_branch(root, branch=branch, source=args.source)
    else:
        branch = _current_branch(root)

    touched = apply_release_files(
        root,
        version=version,
        changelog_section=changelog_section,
    )

    if not args.skip_dep_check:
        _apply_auto_dependency_updates(
            root, dep_updates, reinstall=not args.skip_checks
        )

    if not args.skip_gws_skills:
        touched = [*touched, *_refresh_stock_gws_skills(root)]

    checks = [] if args.skip_checks else _run_checks(root, skip_frontend=args.skip_frontend)

    if args.commit:
        _run(["git", "add", *[str(path.relative_to(root)) for path in touched]], cwd=root)
        _run(["git", "commit", "-m", f"release: prepare v{version}"], cwd=root)

    if args.push:
        _run(["git", "push", "-u", "origin", branch], cwd=root)

    if args.create_pr:
        cmd = [
            "gh",
            "pr",
            "create",
            "--base",
            args.base,
            "--head",
            branch,
            "--title",
            f"Release v{version}",
            "--body",
            _pr_body(version, changelog_section, checks),
        ]
        if not args.ready:
            cmd.append("--draft")
        _run(cmd, cwd=root)

    print(f"Prepared release v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
