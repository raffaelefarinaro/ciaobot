from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_public_ci_matches_release_contract() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "runs-on: macos-latest" in workflow
    assert "python-version: '3.12'" in workflow
    assert "npm ci" in workflow
    assert "npm run build" in workflow
    assert "pytest" in workflow
    assert "tests/" in workflow
    assert "ciao package-smoke --skip-frontend" in workflow
    assert "workflow_dispatch:" in workflow
    assert "branches: [ develop, main ]" in workflow


def test_release_on_main_workflow_publishes_from_main_merge() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-on-main.yml"
    ).read_text(encoding="utf-8")

    assert "branches: [ main ]" in workflow
    assert "gh release create" in workflow
    assert "sync-develop" in workflow
    assert "gh pr merge" in workflow
    assert "CHANGELOG.md" in workflow


def test_ci_builds_and_cold_starts_the_engine_not_the_app() -> None:
    # #655: the release is the engine, so CI builds no app and pins no
    # embedded runtime for one. A step that reappears here fails the job
    # rather than quietly rebuilding what a release no longer ships.
    # Comments are exempt, the way they are in the publish smoke test below:
    # naming what is gone is not building it.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    for gone in (
        "desktop/",
        "tauri",
        "check-desktop",
        "build-bundled-runtime",
        "pinned-python-runtime",
        "CIAO_PYTHON_ARM64_SHA256",
        "ciaobot-desktop",
        "desktop-service",
        "desktop-tray.log",
        "dtolnay/rust-toolchain",
    ):
        assert gone not in commands, f"ci.yml still builds the app: {gone!r}"

    # The macOS job keeps what is still real on that platform: the wheel this
    # branch would publish, installed into a throwaway venv, and the engine
    # answering for itself. Dropping this would leave the launchd, installer
    # and update machinery with no macOS coverage at all.
    assert "uv build --wheel --out-dir dist" in workflow
    assert 'uv pip install --python "$engine_venv/bin/python" dist/ciaobot-*.whl' in workflow
    assert '"$engine" setup --workspace' in workflow
    assert '"$engine" service start' in workflow
    assert "/api/startup-status" in workflow


def test_publish_workflow_ships_signed_engine_manifest() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    # The engine wheel is a release asset in its own right (#562), and the
    # manifest beside it is signed with the same key the installer trusts, so
    # the future installer (#568) and updater (#569) can check it is authentic
    # rather than merely intact.
    for fragment in (
        "uv build --wheel --out-dir dist",
        "-m ciao.release_manifest check-static",
        "-m ciao.release_manifest build",
        "npx --yes @tauri-apps/cli@2.11.4 signer sign ciaobot-engine-manifest.json",
        "-m ciao.release_manifest verify",
        "dist/ciaobot-*.whl",
        "ciaobot-engine-manifest.json.sig",
    ):
        assert fragment in workflow, (
            f"publish.yml no longer publishes the engine manifest: {fragment!r}"
        )

    # The wheel has to carry the PWA, so it is built after the frontend build.
    assert workflow.index("Build PWA assets") < workflow.index("Build engine wheel")

    # #653: the release is engine-only. Everything the app needed is gone, so
    # #579b can delete desktop/ as a pure removal instead of forking a release
    # rewrite into the deletion. A fragment that reappears here fails the job.
    # Comments are exempt, the way they are in the smoke test below: the steps
    # say in prose which app assets are gone, and naming them is not building
    # them.
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    for gone in (
        "build-desktop",
        "tauri build",
        "latest.json",
        "Ciaobot_",
        "ciaobot-installer-verify",
        "build-bundled-runtime",
        "pinned-python-runtime",
    ):
        assert gone not in commands, (
            f"publish.yml still builds or attaches the app: {gone!r}"
        )

    # The signer has to be the one the installer already trusts - the same
    # minisign key - and it must not be reached through the app's tree, which is
    # the whole reason this line changed. `signer sign <path>` needs no project
    # of its own, so the CLI runs standalone at a pinned version.
    sign = next(
        line
        for line in workflow.splitlines()
        if "signer sign ciaobot-engine-manifest.json" in line
    )
    assert "desktop" not in sign, f"the manifest signer still runs under desktop/: {sign}"
    assert "cd desktop" not in workflow


def test_publish_uploads_engine_installer() -> None:
    # The engine-only installer is served from /releases/latest/download, which
    # is a release asset, so it has to be attached with the rest (#568).
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/install-engine.sh" in workflow


def test_retired_app_installer_is_not_in_the_source_tree() -> None:
    # The macOS app installer is retired. scripts/install.sh may exist only as the
    # Linux-only headless installer (#3131 L2), which refuses macOS and root.
    root = Path(__file__).parents[1]
    installer = root / "scripts" / "install.sh"
    if installer.exists():
        text = installer.read_text(encoding="utf-8")
        assert "Linux" in text and "Darwin" in text and "root" in text


def test_release_workflows_do_not_publish_removed_install_channels() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (Path(__file__).parents[1] / ".github" / "workflows").glob("*.yml")
    )

    assert "update-homebrew-tap" not in workflows
    assert "pypi:" not in workflows
    assert ".dmg" not in workflows.lower()
    assert "brew install" not in workflows
    assert "mapfile" not in workflows


def test_release_smoke_only_runs_with_a_published_version() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-smoke.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_call:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert 'LaunchAgents/com.ciao.server.plist")' not in workflow


def test_publish_attaches_only_the_six_engine_assets() -> None:
    # #653: the release carries the engine and nothing else. #1022 adds one
    # prebuilt universal server host archive the signed manifest authenticates.
    # Exactly six assets - the macOS installer under both names, the wheel, the
    # manifest with its signature, and the host - each pinned here so a
    # re-added app asset is a failing test rather than a surprise in a
    # published release.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    attached = """
          gh release upload "$TAG" \\
            install.sh \\
            scripts/install-engine.sh \\
            dist/ciaobot-*.whl \\
            ciaobot-engine-manifest.json \\
            ciaobot-engine-manifest.json.sig \\
            "$host_archive" \\
            --clobber
"""
    assert attached in workflow, (
        "publish.yml no longer attaches exactly the six engine assets"
    )

    # Every name is attached and proven present on the release where the tag is
    # still known - a missing asset would otherwise only surface as a failed
    # install on a user's machine.
    assert "cp scripts/install-engine.sh install.sh" in workflow
    assert "s/__VERIFIER_SHA256__/" not in workflow
    assert "verifier_name" not in workflow
    assert "grep -qx install.sh" in workflow
    assert "grep -qx install-engine.sh" in workflow
    assert "install.ps1" not in workflow
    assert "grep -qx ciaobot-server-host-macos-universal-v1.tar.gz" in workflow


def test_publish_host_asset_is_built_and_signed_in_manifest() -> None:
    # #1022: the signed manifest authenticates one prebuilt universal macOS host
    # archive. The builder runs before the manifest is written, the manifest
    # names the exact archive, and the release attaches that same archive as the
    # seventh asset - proven present where the tag is still known.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    # The builder is the clean-environment Python the wheel was verified with,
    # and it writes to a scratch directory outside the repo so the signed app is
    # never modified and no build output lands in the checkout.
    assert "scripts/build-server-host.py" in workflow
    assert '--output "$RUNNER_TEMP/server-host-build"' in workflow
    assert workflow.index("Build universal server host") < workflow.index(
        "Write and sign engine manifest"
    )
    assert workflow.index("Write and sign engine manifest") < workflow.index(
        "Attach release assets"
    )

    # The manifest step passes the exact fixed archive through --server-host,
    # and the archive it names is the one the release uploads.
    assert (
        "ciaobot-server-host-macos-universal-v1.tar.gz"
        in workflow
    )
    assert "--server-host \"$host_archive\"" in workflow
    assert '"$host_archive" \\' in workflow

    # The builder is invoked with the same Python that verifies the wheel, not a
    # system interpreter that might predate tarfile's data filter.
    assert '"$RUNNER_TEMP/engine-check/bin/python" scripts/build-server-host.py' in workflow

    # The manifest step and the upload step each define the archive path; both
    # must be the builder's --output directory plus the fixed archive name, so
    # the bytes uploaded are the bytes the signed manifest digested.
    archive_paths = re.findall(r'^\s*host_archive="([^"]+)"$', workflow, re.MULTILINE)
    assert archive_paths == [
        "$RUNNER_TEMP/server-host-build/ciaobot-server-host-macos-universal-v1.tar.gz"
    ] * 2


def test_release_smoke_installs_the_engine_instead_of_the_app() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-smoke.yml"
    ).read_text(encoding="utf-8")

    # install.sh is the engine installer now, so the smoke test runs the public
    # one-liner unchanged and asserts the engine came up: the entry point, the
    # receipt it writes, the LaunchAgent, and the startup API.
    for fragment in (
        "/download/v${VERSION}/install.sh",
        'test -x "$HOME/.local/bin/ciao"',
        "$HOME/.local/state/ciaobot/install-receipt.json",
        'launchctl print "gui/$(id -u)/com.ciao.server"',
        "/api/startup-status",
    ):
        assert fragment in workflow, (
            f"release-smoke.yml no longer checks the engine: {fragment!r}"
        )

    # It must not look for Ciaobot.app anywhere in what it runs: that install
    # does not exist any more, and asserting on it would fail every release for
    # a reason that has nothing to do with the build. Comments are exempt - the
    # step says in prose why the app is not there.
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    for gone in (
        "Ciaobot.app",
        "ciaobot-desktop",
        "ciao-runtime",
        "Ciaobot.plist",
        # #653: the release publishes no app, so the smoke test must not
        # download or assert on any of it.
        "latest.json",
        "app.tar.gz",
        "ciaobot-installer-verify",
    ):
        assert gone not in commands, (
            f"release-smoke.yml still expects the app release: {gone!r}"
        )

    # The two installer assets have to be the same bytes: the app's Move action
    # fetches install-engine.sh (#604) and would be stranded by a release where
    # the public install.sh and that alias drift apart.
    assert "grep -q -- '--migrate' install.sh" in workflow
    assert "shasum -a 256 install.sh" in workflow
    assert "shasum -a 256 install-engine.sh" in workflow
    assert "--pattern install.sh --pattern install-engine.sh" in workflow


def test_release_smoke_finishes_the_setup_wizard_and_is_bounded() -> None:
    # A fresh macOS install blocks on the browser setup wizard (#2021), so a
    # smoke run that only pipes install.sh into sh waits for a click nobody
    # makes (#2164: the v1.7.0 run hung for 90 minutes). The step must run the
    # public one-liner in the background and finish the wizard over HTTP.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-smoke.yml"
    ).read_text(encoding="utf-8")

    assert "timeout-minutes:" in workflow, "release-smoke.yml has no job timeout"
    assert "/api/setup-status" in workflow
    assert "/api/setup/finish" in workflow
    assert "RUNNER_TEMP" in workflow
    # Still the documented one-liner, not a hand-rolled install.
    assert "| sh -s -- --version \"$VERSION\"" in workflow
    assert "/download/v${VERSION}/install.sh" in workflow
    # The sign-in URL is a credential and stays out of the job log.
    assert "Open Ciaobot: ).*#\\1[redacted]" in workflow
    # The later steps follow the workspace the wizard chose.
    assert '"$SMOKE_WORKSPACE/.env"' in workflow


def test_first_party_install_command_stays_install_sh() -> None:
    # The public one-liner did not change name in the transition release (#651):
    # install.sh installs the engine now, so every existing link and every
    # documented command keeps working and none of them has to move to a name
    # the docs would then be the only place using.
    root = Path(__file__).parents[1]
    one_liner = (
        "curl -fsSL https://github.com/raffaelefarinaro/ciaobot"
        "/releases/latest/download/install.sh | sh"
    )
    for relative in (
        "README.md",
        "site/index.html",
        "site/guide.html",
        "docs/DEVELOPMENT.md",
        "skills/ciao-release/SKILL.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert one_liner in text, f"{relative} no longer documents the install command"
        assert "/releases/latest/download/install-engine.sh" not in text, (
            f"{relative} points a first-time user at install-engine.sh; the public"
            " command is still install.sh"
        )


def test_ci_has_no_path_filter_job_and_still_runs_the_macos_job() -> None:
    # The `changes` job existed to decide whether a desktop change needed the
    # macOS runner. Nothing builds the app any more, so the decision it made
    # has no meaning, and the macOS job runs on release PRs into main
    # regardless of which files changed - which is the coverage that matters.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "  changes:" not in workflow
    assert "needs: changes" not in workflow
    assert "needs.changes" not in workflow
    assert "runs-on: macos-latest" in workflow
    # #655: dropping the `changes` job also dropped the `if:` it fed, and
    # without a gate the macOS job would run on every develop PR. The gate is
    # release PRs into main only.
    assert (
        "if: github.event_name == 'workflow_dispatch' || github.base_ref == 'main'"
        in workflow
    )


def _web_changes_script() -> str:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    step = workflow.split("    - name: Detect web changes", 1)[1]
    run = step.split("      run: |\n", 1)[1]
    lines: list[str] = []
    for line in run.splitlines():
        if line.startswith("        "):
            lines.append(line[8:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_git(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CI workflow test")
    _git(repo, "config", "user.email", "ci-workflow@example.invalid")


def _commit_file(repo: Path, name: str, content: str, message: str) -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _run_web_detector(
    repo: Path, output: Path, base_sha: str | None, head_sha: str | None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(output),
    }
    if base_sha is not None:
        env["PR_BASE_SHA"] = base_sha
    if head_sha is not None:
        env["PR_HEAD_SHA"] = head_sha
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", _web_changes_script()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("add", "true"),
        ("modify", "true"),
        ("delete", "true"),
        ("rename_out", "true"),
        ("rename_in", "true"),
        ("backend", "false"),
        ("docs", "false"),
        ("prefix", "false"),
    ],
)
def test_web_change_detector_uses_pr_three_dot_diff(
    tmp_path: Path, change: str, expected: str
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    seed = {"web/a.ts": "a", "outside/a.ts": "o", "web-other/a.ts": "p"}
    for name, content in seed.items():
        _commit_file(repo, name, content, f"seed {name}")
    base = _git(repo, "rev-parse", "HEAD")
    if change == "add":
        head = _commit_file(repo, "web/new.ts", "new", "add web file")
    elif change == "modify":
        head = _commit_file(repo, "web/a.ts", "changed", "modify web file")
    elif change == "delete":
        _git(repo, "rm", "web/a.ts")
        _git(repo, "commit", "-qm", "delete web file")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "rename_out":
        _git(repo, "mv", "web/a.ts", "outside/renamed.ts")
        _git(repo, "commit", "-qm", "move file out of web")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "rename_in":
        _git(repo, "mv", "outside/a.ts", "web/renamed.ts")
        _git(repo, "commit", "-qm", "move file into web")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "backend":
        head = _commit_file(repo, "ciao/change.py", "x", "backend change")
    elif change == "docs":
        head = _commit_file(repo, "docs/change.md", "x", "docs change")
    else:
        head = _commit_file(repo, "web-other/change.ts", "x", "prefix change")

    output = tmp_path / "github-output"
    result = _run_web_detector(repo, output, base, head)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"web_changed={expected}\n"


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
def test_web_change_detector_ignores_base_only_web_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    ancestor = _commit_file(repo, "README.md", "seed", "common ancestor")
    _git(repo, "checkout", "-qb", "base")
    base = _commit_file(repo, "web/base-only.ts", "base", "base web change")
    _git(repo, "checkout", "-qb", "head", ancestor)
    head = _commit_file(repo, "docs/head.md", "head", "head docs change")
    assert _git(repo, "diff", "--name-only", f"{base}..{head}", "--", "web/")
    output = tmp_path / "github-output"
    result = _run_web_detector(repo, output, base, head)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "web_changed=false\n"


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
def test_web_change_detector_handles_fork_head_without_branch_name(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    _init_git(upstream)
    base = _commit_file(upstream, "README.md", "base", "base")
    fork = tmp_path / "fork"
    subprocess.run(["git", "clone", "-q", str(upstream), str(fork)], check=True)
    _git(fork, "config", "user.name", "Fork test")
    _git(fork, "config", "user.email", "fork@example.invalid")
    head = _commit_file(fork, "web/fork.ts", "fork", "fork web change")

    merge_checkout = tmp_path / "merge-checkout"
    _init_git(merge_checkout)
    _git(merge_checkout, "fetch", str(upstream), base)
    _git(merge_checkout, "checkout", "-qb", "pr-merge", "FETCH_HEAD")
    _git(merge_checkout, "fetch", str(fork), head)
    _git(merge_checkout, "merge", "--no-ff", "-m", "synthetic PR merge", "FETCH_HEAD")
    _git(merge_checkout, "checkout", "--detach", "HEAD")
    _git(merge_checkout, "branch", "-D", "pr-merge")
    output = tmp_path / "github-output"
    result = _run_web_detector(merge_checkout, output, base, head)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "web_changed=true\n"


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
@pytest.mark.parametrize("side", ["PR_BASE_SHA", "PR_HEAD_SHA"])
@pytest.mark.parametrize(
    "value",
    [None, "", "a1b2c3d", "a" * 39, "a" * 41, "g" * 40, "--help", "$(touch sentinel)"],
)
def test_web_change_detector_rejects_invalid_sha_inputs(
    tmp_path: Path, side: str, value: str | None
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    commit = _commit_file(repo, "README.md", "seed", "seed")
    base, head = commit, commit
    if side == "PR_BASE_SHA":
        base = value
    else:
        head = value
    output = tmp_path / "github-output"
    result = _run_web_detector(repo, output, base, head)
    assert result.returncode != 0
    assert not output.exists() or "web_changed=" not in output.read_text(encoding="utf-8")
    assert not (repo / "sentinel").exists()


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
@pytest.mark.parametrize("side", ["PR_BASE_SHA", "PR_HEAD_SHA"])
@pytest.mark.parametrize("invalid", ["f" * 40, "blob"])
def test_web_change_detector_fails_for_unknown_commits(
    tmp_path: Path, side: str, invalid: str
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    commit = _commit_file(repo, "README.md", "seed", "seed")
    if invalid == "blob":
        blob = _git(repo, "hash-object", "-w", "README.md")
        invalid_sha = blob
    else:
        invalid_sha = invalid
    output = tmp_path / "github-output"
    base, head = commit, commit
    if side == "PR_BASE_SHA":
        base = invalid_sha
    else:
        head = invalid_sha
    result = _run_web_detector(repo, output, base, head)
    assert result.returncode != 0
    assert not output.exists() or "web_changed=" not in output.read_text(encoding="utf-8")


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
def test_web_change_detector_fails_for_disconnected_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    base = _commit_file(repo, "README.md", "base", "base")
    _git(repo, "checkout", "--orphan", "other")
    _git(repo, "rm", "-rf", ".")
    head = _commit_file(repo, "README.md", "head", "orphan head")
    output = tmp_path / "github-output"
    result = _run_web_detector(repo, output, base, head)
    assert result.returncode != 0
    assert not output.exists() or "web_changed=" not in output.read_text(encoding="utf-8")


def _mac_changes_script() -> str:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    step = workflow.split("    - name: Detect mac changes", 1)[1]
    run = step.split("      run: |\n", 1)[1]
    lines: list[str] = []
    for line in run.splitlines():
        if line.startswith("        "):
            lines.append(line[8:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines)


def _run_mac_detector(
    repo: Path, output: Path, base_sha: str | None, head_sha: str | None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(output),
    }
    if base_sha is not None:
        env["PR_BASE_SHA"] = base_sha
    if head_sha is not None:
        env["PR_HEAD_SHA"] = head_sha
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", _mac_changes_script()],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("add", "true"),
        ("modify", "true"),
        ("delete", "true"),
        ("rename_out", "true"),
        ("rename_in", "true"),
        ("engine_colors", "true"),
        ("pyproject", "true"),
        ("uv_lock", "true"),
        ("ciao", "false"),
        ("nested_pyproject", "false"),
        ("web", "false"),
        ("native", "false"),
        ("prefix", "false"),
    ],
)
def test_mac_change_detector_uses_pr_three_dot_diff(
    tmp_path: Path, change: str, expected: str
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    seed = {
        "mac/a.swift": "a",
        "outside/a.swift": "o",
        "mac-other/a.swift": "p",
        "native/a.swift": "n",
        "web/src/lib/other.ts": "w",
        "web/src/lib/workspaceColors.ts": "c",
        "pyproject.toml": "p",
        "uv.lock": "l",
        "ciao/a.py": "c",
        "web/pyproject.toml": "n",
    }
    for name, content in seed.items():
        _commit_file(repo, name, content, f"seed {name}")
    base = _git(repo, "rev-parse", "HEAD")
    if change == "add":
        head = _commit_file(repo, "mac/new.swift", "new", "add mac file")
    elif change == "modify":
        head = _commit_file(repo, "mac/a.swift", "changed", "modify mac file")
    elif change == "delete":
        _git(repo, "rm", "mac/a.swift")
        _git(repo, "commit", "-qm", "delete mac file")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "rename_out":
        _git(repo, "mv", "mac/a.swift", "outside/renamed.swift")
        _git(repo, "commit", "-qm", "move file out of mac")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "rename_in":
        _git(repo, "mv", "outside/a.swift", "mac/renamed.swift")
        _git(repo, "commit", "-qm", "move file into mac")
        head = _git(repo, "rev-parse", "HEAD")
    elif change == "engine_colors":
        head = _commit_file(
            repo, "web/src/lib/workspaceColors.ts", "changed", "engine colours"
        )
    elif change == "pyproject":
        head = _commit_file(repo, "pyproject.toml", "changed", "pyproject change")
    elif change == "uv_lock":
        head = _commit_file(repo, "uv.lock", "changed", "lock change")
    elif change == "ciao":
        head = _commit_file(repo, "ciao/a.py", "changed", "engine change")
    elif change == "nested_pyproject":
        head = _commit_file(repo, "web/pyproject.toml", "changed", "nested pyproject")
    elif change == "web":
        head = _commit_file(repo, "web/src/lib/other.ts", "changed", "other web file")
    elif change == "native":
        head = _commit_file(repo, "native/a.swift", "changed", "native change")
    else:
        head = _commit_file(repo, "mac-other/change.swift", "x", "prefix change")

    output = tmp_path / "github-output"
    result = _run_mac_detector(repo, output, base, head)
    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"mac_changed={expected}\n"


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the detector runs on GitHub's Linux bash runner",
)
@pytest.mark.parametrize("side", ["PR_BASE_SHA", "PR_HEAD_SHA"])
@pytest.mark.parametrize(
    "value",
    [None, "a1b2c3d", "g" * 40, "$(touch sentinel)"],
)
def test_mac_change_detector_rejects_invalid_sha_inputs(
    tmp_path: Path, side: str, value: str | None
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    commit = _commit_file(repo, "README.md", "seed", "seed")
    base, head = commit, commit
    if side == "PR_BASE_SHA":
        base = value
    else:
        head = value
    output = tmp_path / "github-output"
    result = _run_mac_detector(repo, output, base, head)
    assert result.returncode != 0
    assert not output.exists() or "mac_changed=" not in output.read_text(encoding="utf-8")
    assert not (repo / "sentinel").exists()


def test_develop_web_prs_run_linux_e2e() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    assert "  paths:" not in workflow
    assert "web_changed: ${{ steps.web-changes.outputs.web_changed }}" in workflow
    assert "fetch-depth: 0" in workflow
    assert "PR_BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
    assert "PR_HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "on:\n  pull_request:\n    branches: [ develop, main ]\n  workflow_dispatch:" in workflow
    assert "git diff --no-renames --name-only \"${PR_BASE_SHA}...${PR_HEAD_SHA}\" -- web/" in _web_changes_script()
    assert "github.event_name == 'pull_request' && github.base_ref == 'develop'" in workflow
    jobs = re.split(r"(?m)^  (?=[a-z0-9-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}
    scope = by_name["web-scope"]
    assert "fetch-depth: 0" in scope
    assert "outputs:\n      web_changed: ${{ steps.web-changes.outputs.web_changed }}" in scope
    e2e = by_name["linux-e2e"]
    assert "needs: web-scope" in e2e
    assert "needs.web-scope.outputs.web_changed == 'true'" in e2e
    assert "runs-on: ubuntu-24.04" in e2e
    commands = ["npm ci", "npx playwright install --with-deps chromium", "npm run test:e2e"]
    assert [e2e.index(command) for command in commands] == sorted(e2e.index(command) for command in commands)
    assert "working-directory: web" in e2e
    assert e2e.index("npm ci") < e2e.index("playwright install")
    assert "permissions:" not in e2e
    assert "actions/upload-artifact@v7" in e2e
    assert "name: linux-playwright-traces" in e2e
    assert "path: web/test-results/" in e2e
    assert "retention-days: 7" in e2e
    assert "if: failure()" in e2e
    assert "permissions:\n  contents: read" in workflow


def test_branch_protection_declares_conditional_linux_e2e_for_develop() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "configure-github-branches.sh"
    ).read_text(encoding="utf-8")
    blocks = re.findall(r"(?ms)^\w+_json\(\) \{\n  cat <<'JSON'\n(.*?)\nJSON\n\}", script)
    assert len(blocks) == 2
    develop, main = (json.loads(block) for block in blocks)
    assert develop["required_status_checks"]["contexts"] == [
        "linux-server", "linux-web", "linux-install-smoke", "linux-e2e", "mac-app"
    ]
    assert main["required_status_checks"]["contexts"] == [
        "linux-server", "linux-web", "linux-install-smoke",
        "linux-server-latest-python", "test", "mac-app",
    ]
    assert develop["enforce_admins"] is False
    assert main["enforce_admins"] is True


def test_branch_protection_matches_ci_jobs() -> None:
    script = (
        Path(__file__).parents[1] / "scripts" / "configure-github-branches.sh"
    ).read_text(encoding="utf-8")
    blocks = re.findall(r"(?ms)^\w+_json\(\) \{\n  cat <<'JSON'\n(.*?)\nJSON\n\}", script)
    assert len(blocks) == 2
    develop, main = (json.loads(block) for block in blocks)
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    jobs = re.split(r"(?m)^  (?=[a-z0-9-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}
    for context in (
        develop["required_status_checks"]["contexts"]
        + main["required_status_checks"]["contexts"]
    ):
        assert context in by_name
    release_if = "    if: github.event_name == 'workflow_dispatch' || github.base_ref == 'main'"
    main_contexts = main["required_status_checks"]["contexts"]
    for name, job in by_name.items():
        header = job.split("\n    steps:", 1)[0]
        if any(line == release_if for line in header.splitlines()):
            assert name in main_contexts
    assert not [name for name in by_name if name.startswith("windows")]


def test_develop_prs_run_linux_split_gates() -> None:
    # A develop PR waits on the two parallel Linux jobs and conditional Linux
    # browser coverage for web changes.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    jobs = re.split(r"(?m)^  (?=[a-z-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}

    assert "pytest -n auto tests/" in by_name["linux-server"]
    assert "npm test" not in by_name["linux-server"]
    assert "npm test" in by_name["linux-web"]
    assert "npm run build" in by_name["linux-web"]
    # The smoke serves the packaged PWA, so it must follow the build.
    web = by_name["linux-web"]
    assert web.index("npm run build") < web.index("ciao package-smoke --skip-frontend")
    assert not [name for name in by_name if name.startswith("windows")]


def test_ci_runs_the_linux_install_smoke_in_a_clean_container() -> None:
    # #3131 L3: `scripts/install.sh` is the Linux release install path, so
    # every PR installs this checkout's wheel with it inside a plain
    # ubuntu:24.04 container, and the planned service unit is re-checked with
    # the runner's systemd. A guard that disappears here must fail the test,
    # not surface later as the first failing release install.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    jobs = re.split(r"(?m)^  (?=[a-z0-9-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}

    smoke = by_name["linux-install-smoke"]
    assert "runs-on: ubuntu-24.04" in smoke
    # The PWA rides inside the wheel (npm run build writes ciao/web/static),
    # so it is built before the wheel, and the container runs only once the
    # wheel exists.
    assert smoke.index("npm ci") < smoke.index("npm run build")
    assert smoke.index("npm run build") < smoke.index(
        "uv build --wheel --out-dir dist"
    )
    assert smoke.index("uv build --wheel --out-dir dist") < smoke.index("docker run")
    assert "ubuntu:24.04 sh -es" in smoke

    install = next(line for line in smoke.splitlines() if "install.sh --wheel" in line)
    assert "/src/dist/ciaobot-*.whl" in install
    assert "--no-service" in install
    assert "su smoke -c" in install

    # The install counts only when the tool answers with this checkout's own
    # version, read from pyproject, and the service path is exercised as a
    # bounded dry run.
    assert '"$HOME/.local/bin/ciao" --version' in smoke
    assert r's/^version = "\([^"]*\)"/\1/p' in smoke
    assert '"ciao $version"' in smoke
    assert "service install --dry-run" in smoke

    # The host strips the dry run's notes and re-checks the unit with
    # systemd-analyze, uncommented.
    verify = next(line for line in smoke.splitlines() if "systemd-analyze verify" in line)
    assert not verify.lstrip().startswith("#"), "the verify call is commented out"
    assert smoke.index("service install --dry-run") < smoke.index("systemd-analyze verify")

    # The job sits with the other Linux gates, immediately after linux-web.
    assert (
        workflow.index("  linux-web:")
        < workflow.index("  linux-install-smoke:")
        < workflow.index("  linux-e2e:")
    )


def _rehearsal_workflow() -> str:
    return (
        Path(__file__).parents[1]
        / ".github"
        / "workflows"
        / "server-host-signing-rehearsal.yml"
    ).read_text(encoding="utf-8")


def test_signing_rehearsal_is_manual_protected_and_no_release_upload() -> None:
    # #2331: a maintainer-requested rehearsal of the signed host. It is a
    # separate manual workflow on purpose: dispatching publish.yml cannot run
    # build-engine (it is release-only), and a green skipped job proves nothing.
    workflow = _rehearsal_workflow()

    assert "workflow_dispatch:" in workflow
    for untriggered in ("pull_request", "pull_request_target", "release:", "push:", "schedule:"):
        assert untriggered not in workflow, (
            f"the signing rehearsal must stay manually dispatched: {untriggered!r}"
        )

    # An unchecked confirmation is a refusal, never a successful skip.
    assert "confirm_no_release_upload:" in workflow
    assert "type: boolean" in workflow
    assert "required: true" in workflow
    assert "default: false" in workflow

    # Read-only, with no release or OIDC permission to upload anything.
    assert "permissions:\n  contents: read\n" in workflow
    for forbidden in ("contents: write", "id-token", "releases:", "packages:"):
        assert forbidden not in workflow, (
            f"the signing rehearsal grants a write permission: {forbidden!r}"
        )

    # The environment carries the required reviewers and allowed branches; the
    # workflow states the maintenance prerequisite rather than pretending YAML
    # proves protection.
    assert "environment: server-host-signing-rehearsal" in workflow
    assert "provisioned by a maintainer" in workflow

    # Bounded and serialized: one engine host signing run at a time.
    assert "timeout-minutes: 90" in workflow
    assert "concurrency:" in workflow
    assert "group: server-host-signing-rehearsal" in workflow

    # Untrusted refs and an unconfirmed run are refused before any credential
    # is bound to a step.
    guard = workflow.split("    - uses: actions/checkout@v7")[0]
    assert "refs/heads/develop|refs/heads/main" in guard
    assert "confirm_no_release_upload" in guard
    assert "exit 1" in guard
    assert "secrets." not in guard, "the refusal step must not receive a secret"
    assert "SERVER_HOST_CERTIFICATE_P12_BASE64" not in guard

    # Nothing is uploaded or distributed. The one permitted network write is the
    # Apple notary submission inside the release build.
    commands = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in (
        "actions/upload-artifact",
        "actions/cache",
        "gh release upload",
        "gh release",
        "--clobber",
        "git push",
        "npm publish",
        "uv publish",
        "aws s3",
    ):
        assert forbidden not in commands, (
            f"the signing rehearsal would distribute or implicitly fetch: {forbidden!r}"
        )
    # No dependency cache and no registry audit/funding traffic: a cache action
    # or setup-node caching uploads and downloads behind the workflow's back.
    assert "cache:" not in commands, "the rehearsal caches dependencies"
    assert "cache-dependency-path" not in commands
    assert "npm ci --no-audit --no-fund" in commands
    assert "npm audit" not in commands
    assert 'npm_config_audit: "false"' in commands
    assert 'npm_config_fund: "false"' in commands
    assert "continue-on-error" not in commands

    # The scratch directory is unique per run, and cleanup only removes a
    # directory this run created (the helper writes the ownership marker).
    assert (
        "server-host-signing-rehearsal-${{ github.run_id }}-${{ github.run_attempt }}"
        in commands
    )
    assert "always() && steps.claim-scratch.outcome == 'success'" in commands
    # Deleting is gated on the receipt the helper wrote after its own exclusive
    # mkdir, not on a marker string a pre-existing directory could carry.
    assert 'receipt="${REHEARSAL_DIR}.owned"' in commands
    assert '[ "$(cat "$receipt")" = "$REHEARSAL_DIR" ]' in commands
    assert 'rm -rf "$REHEARSAL_DIR"' in commands
    assert 'rm -f "$receipt"' in commands
    assert '[ -f "$REHEARSAL_DIR/.rehearsal-owned" ]' not in commands

    # Current repo-pinned action versions, not floating ones.
    assert "actions/checkout@v7" in workflow
    assert "actions/setup-node@v7" in workflow
    assert "astral-sh/setup-uv@v10.3.3" in workflow

    # The named CI inputs only, and they are bound to the signing step alone.
    signing_step = workflow.split("      - name: Rehearse the signed server host")[1]
    signing_step = signing_step.split("      - name: Remove runner-local")[0]
    for name in (
        "SERVER_HOST_CERTIFICATE_P12_BASE64",
        "SERVER_HOST_CERTIFICATE_PASSWORD",
        "SERVER_HOST_NOTARY_KEY_P8_BASE64",
        "SERVER_HOST_NOTARY_KEY_ID",
        "SERVER_HOST_NOTARY_ISSUER_ID",
        "SERVER_HOST_SIGNING_IDENTITY",
        "SERVER_HOST_TEAM_ID",
    ):
        assert name in signing_step, f"the signing step never receives {name}"
    assert "SERVER_HOST_SIGNING_IDENTITY: ${{ vars." in signing_step
    assert "SERVER_HOST_TEAM_ID: ${{ vars." in signing_step
    assert "SERVER_HOST_NOTARY_KEY_ID: ${{ vars." in signing_step
    assert "SERVER_HOST_NOTARY_ISSUER_ID: ${{ vars." in signing_step
    assert "SERVER_HOST_NOTARY_KEY_P8_BASE64: ${{ secrets." in signing_step
    assert "set -x" not in commands


def test_signing_rehearsal_binds_source_sha_and_clean_wheel_verifier() -> None:
    workflow = _rehearsal_workflow()

    # The event's exact commit is what runs and what the report names. Checkout
    # keeps no push credential, and the SHA is re-read from the checkout.
    assert "persist-credentials: false" in workflow
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in workflow
    assert '--source-sha "$GITHUB_SHA"' in workflow

    # The wheel a real release would publish is built, then verified by an
    # interpreter that only holds the installed wheel - never the checkout.
    assert workflow.index("Build PWA assets") < workflow.index("Build engine wheel")
    assert workflow.index("Build engine wheel") < workflow.index(
        "Verify engine wheel in a clean environment"
    )
    assert 'uv build --wheel --out-dir dist' in workflow
    assert 'uv venv --python 3.13 "$RUNNER_TEMP/engine-check"' in workflow
    assert (
        'uv pip install --python "$RUNNER_TEMP/engine-check/bin/python" '
        "dist/ciaobot-*.whl" in workflow
    )
    assert 'cd "$RUNNER_TEMP"' in workflow
    assert (
        '"$RUNNER_TEMP/engine-check/bin/python" -m ciao.release_manifest check-static'
        in workflow
    )

    # The helper runs from the checkout with that clean interpreter, a fresh
    # scratch output and the single candidate wheel; the report stays local and
    # only the sanitized summary reaches the step summary.
    helper = workflow.split("      - name: Rehearse the signed server host")[1]
    helper = helper.split("      - name: Remove runner-local")[0]
    assert "scripts/rehearse-server-host-signing.py" in helper
    # The receipt is claimed in an earlier step, before the guard can refuse.
    assert 'rm -f "${REHEARSAL_DIR}.owned"' not in helper
    assert '--output "$REHEARSAL_DIR"' in helper
    assert '--python "$RUNNER_TEMP/engine-check/bin/python"' in helper
    assert "--wheel dist/ciaobot-*.whl" in helper
    assert 'cat "$REHEARSAL_DIR/summary.txt" >> "$GITHUB_STEP_SUMMARY"' in helper
    assert "verification.json" not in helper, (
        "the raw runner-local report must not be echoed into the summary"
    )


def test_signing_rehearsal_still_reports_its_sanitized_summary_when_the_helper_fails() -> None:
    # #41221404339: the helper writes a sanitized summary.txt on a failed stage
    # too, but the step ran under `set -e`, which aborted at the failed command
    # before the append, so the run showed no reason at all. The step must
    # append the summary and still fail with the helper's status.
    workflow = _rehearsal_workflow()
    helper = workflow.split("      - name: Rehearse the signed server host")[1]
    helper = helper.split("      - name: Remove runner-local")[0]

    capture = '|| status=$?'
    report = 'cat "$REHEARSAL_DIR/summary.txt" >> "$GITHUB_STEP_SUMMARY"'
    propagate = 'exit "$status"'
    assert capture in helper
    assert helper.index(capture) < helper.index(report) < helper.index(propagate)
    # A failure before the scratch directory exists has no summary; the guard
    # keeps the append from becoming a second, masking error.
    assert '[ -f "$REHEARSAL_DIR/summary.txt" ]' in helper
    # The only file the step reads is that summary: nothing else is cat'd, so
    # the raw runner-local report can never reach the job summary.
    reads = [
        line.strip() for line in helper.splitlines() if line.split()[:1] == ["cat"]
    ]
    assert reads == [report], reads
    assert "verification.json" not in helper


def test_production_publish_remains_usable_without_apple_credentials() -> None:
    # #2331 prepares CI; it does not activate signed publishing. Today's release
    # must keep working with no Apple secret, no signed-mode switch and no
    # dependency on the rehearsal.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    for forbidden in (
        "SERVER_HOST_CERTIFICATE_P12_BASE64",
        "SERVER_HOST_CERTIFICATE_PASSWORD",
        "SERVER_HOST_NOTARY_KEY_P8_BASE64",
        "SERVER_HOST_NOTARY_KEY_ID",
        "SERVER_HOST_NOTARY_ISSUER_ID",
        "SERVER_HOST_SIGNING_IDENTITY",
        "SERVER_HOST_TEAM_ID",
    ):
        assert forbidden not in workflow, (
            f"publish.yml grew a mandatory Apple-secret dependency: {forbidden!r}"
        )
    for flag in ("--release", "--signing-identity", "--team-id", "--notary-profile"):
        assert flag not in workflow, (
            f"publish.yml switched the production host build to signed mode: {flag!r}"
        )
    assert "--server-host-team-id" not in workflow
    assert "server-host-signing-rehearsal" not in workflow
    assert "rehearsal" not in workflow.lower()

    # The ordinary ad-hoc build, its scratch output and the seven assets are
    # untouched, and the production job has no edge to the rehearsal.
    assert '"$RUNNER_TEMP/engine-check/bin/python" scripts/build-server-host.py' in workflow
    assert '--output "$RUNNER_TEMP/server-host-build"' in workflow
    assert "gh release upload \"$TAG\"" in workflow
    assert workflow.count("needs: build-engine") == 1


def _dispatch_guard_script() -> str:
    """The guard step's shell, extracted so a test can actually execute it."""
    workflow = _rehearsal_workflow()
    step = workflow.split(
        "      - name: Refuse an unconfirmed or untrusted run", 1
    )[1]
    run = step.split("        run: |\n", 1)[1]
    lines: list[str] = []
    for line in run.splitlines():
        if line.startswith("          "):
            lines.append(line[10:])
        elif not line.strip():
            lines.append("")
        else:
            break
    return "\n".join(lines)


@pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="the guard runs on the macOS runner under bash",
)
@pytest.mark.parametrize(
    ("event", "confirmed", "ref", "allowed"),
    [
        ("workflow_dispatch", "true", "refs/heads/develop", True),
        ("workflow_dispatch", "true", "refs/heads/main", True),
        ("workflow_dispatch", "false", "refs/heads/develop", False),
        ("workflow_dispatch", "", "refs/heads/develop", False),
        ("workflow_dispatch", "TRUE", "refs/heads/develop", False),
        ("workflow_dispatch", "true", "refs/heads/feature/x", False),
        ("workflow_dispatch", "true", "refs/pull/1/merge", False),
        ("push", "true", "refs/heads/develop", False),
    ],
)
def test_dispatch_guard_refuses_untrusted_refs_and_unconfirmed_runs(
    event: str, confirmed: str, ref: str, allowed: bool
) -> None:
    script = _dispatch_guard_script()
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", script],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "EVENT_NAME": event,
            "CONFIRMED": confirmed,
            "DISPATCHED_REF": ref,
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if allowed else 1), (
        result.stdout,
        result.stderr,
    )


def test_signing_rehearsal_claims_the_receipt_before_the_guard() -> None:
    # Cleanup runs always(), so the stale receipt must be cleared before the
    # confirmation/ref guard: otherwise a refused run could leave a planted
    # matching receipt that cleanup then trusts.
    workflow = _rehearsal_workflow()
    claim = workflow.index("      - name: Claim the run-scoped scratch receipt")
    guard = workflow.index("      - name: Refuse an unconfirmed or untrusted run")
    assert claim < guard
    claim_step = workflow[claim:guard]
    assert 'rm -f "${REHEARSAL_DIR}.owned"' in claim_step
    assert "secrets." not in claim_step, "the claim step must not receive a secret"

    helper = workflow.split("      - name: Rehearse the signed server host")[1]
    helper = helper.split("      - name: Remove runner-local")[0]
    assert 'rm -f "${REHEARSAL_DIR}.owned"' not in helper


def _cleanup_step(workflow: str) -> str:
    return workflow[
        workflow.index("      - name: Remove runner-local candidate and credential files") :
    ]


def _guard_step(workflow: str) -> str:
    return workflow[
        workflow.index("      - name: Refuse an unconfirmed or untrusted run") :
        workflow.index("      - uses: actions/checkout@v7")
    ]


def _claim_step(workflow: str) -> str:
    return workflow[
        workflow.index("      - name: Claim the run-scoped scratch receipt") :
        workflow.index("      - name: Refuse an unconfirmed or untrusted run")
    ]


def test_signing_rehearsal_cleanup_requires_a_successful_claim() -> None:
    workflow = _rehearsal_workflow()
    claim = _claim_step(workflow)
    cleanup = _cleanup_step(workflow)

    # The claim step is identifiable, cannot mask its own failure, and is the
    # first step of the job.
    assert "        id: claim-scratch" in claim
    assert "continue-on-error" not in claim
    assert workflow.index("      - name: Claim the run-scoped scratch receipt") == (
        workflow.index("    steps:\n") + len("    steps:\n")
    )

    # Cleanup only deletes when the claim succeeded: a failed, skipped or
    # cancelled claim leaves the condition false, so a pre-existing receipt is
    # never trusted.
    assert (
        "if: ${{ always() && steps.claim-scratch.outcome == 'success' }}"
        in cleanup
    )
    assert "continue-on-error" not in cleanup
    assert 'rm -rf "$REHEARSAL_DIR"' in cleanup
    # The id is defined exactly once, and the cleanup step follows every step
    # that could fail or be cancelled before the claim completes.
    assert workflow.count("id: claim-scratch") == 1
    assert workflow.index("      - name: Claim the run-scoped scratch receipt") < workflow.index(
        "      - name: Refuse an unconfirmed or untrusted run"
    )
    assert workflow.index("      - name: Claim the run-scoped scratch receipt") < workflow.index(
        "      - uses: actions/checkout@v7"
    )


@pytest.mark.parametrize(
    ("claim_outcome", "deletes"),
    [
        ("success", True),
        ("failure", False),
        ("cancelled", False),
        ("skipped", False),
    ],
)
def test_cleanup_gate_opens_only_for_a_successful_claim(
    claim_outcome: str, deletes: bool
) -> None:
    # This models the workflow's step condition literally:
    #   always() && steps.claim-scratch.outcome == 'success'
    condition = (
        "always() && steps.claim-scratch.outcome == 'success'"
    )
    assert condition in _cleanup_step(_rehearsal_workflow())
    gate_open = "always()" in condition and (
        claim_outcome == "success"
        if "steps.claim-scratch.outcome == 'success'" in condition
        else False
    )
    assert gate_open is deletes


def test_dispatch_guard_binds_the_real_event_expressions() -> None:
    # The shell harness injects CONFIRMED/DISPATCHED_REF/EVENT_NAME, so the
    # bindings themselves must be asserted: a literal binding would keep the
    # harness green while the real workflow trusted the wrong value.
    step = _guard_step(_rehearsal_workflow())
    assert "CONFIRMED: ${{ github.event.inputs.confirm_no_release_upload }}" in step
    assert "DISPATCHED_REF: ${{ github.ref }}" in step
    assert "EVENT_NAME: ${{ github.event_name }}" in step
    assert step.count("CONFIRMED:") == 1
    assert step.count("DISPATCHED_REF:") == 1
    assert step.count("EVENT_NAME:") == 1
    for literal in (
        'CONFIRMED: "true"',
        "CONFIRMED: 'true'",
        "CONFIRMED: true",
        'DISPATCHED_REF: "refs/heads/develop"',
        'EVENT_NAME: "workflow_dispatch"',
    ):
        assert literal not in step

    # And the guard consumes exactly the bound names.
    script = _dispatch_guard_script()
    for name in ("CONFIRMED", "DISPATCHED_REF", "EVENT_NAME"):
        assert f'"${name}"' in script, name

def test_workflow_expressions_are_valid_for_github() -> None:
    """A `runner` context in a job-level `env` block fails to parse the workflow.

    GitHub then refuses the dispatch with 422 and the run never starts, which no
    text assertion over the file previously noticed.
    """
    workflow_path = (
        Path(__file__).parents[1] / ".github" / "workflows" / "server-host-signing-rehearsal.yml"
    )
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    in_jobs = False
    job_indent: int | None = None
    in_job_env = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if indent == 2:
            job_indent = indent
            in_job_env = False
            continue
        if job_indent is not None and indent == job_indent + 2 and stripped == "env:":
            in_job_env = True
            continue
        if job_indent is not None and indent <= job_indent + 2:
            if stripped != "env:":
                in_job_env = False
        if in_job_env:
            assert "runner." not in stripped, (
                "a job-level env value may not use the runner context: " + stripped
            )


def test_publish_lists_installers_in_the_signed_manifest() -> None:
    # #2483: a client authenticates install.sh from the signed manifest, so the
    # two installers are listed in the build, by the exact files that are
    # uploaded later, and the manifest is built after Prepare release assets has
    # copied install.sh.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    build = next(
        line for line in workflow.splitlines() if "-m ciao.release_manifest build" in line
    )
    for installer in (
        '--installer "$ws/install.sh"',
        '--installer "$ws/scripts/install-engine.sh"',
    ):
        assert installer in build, f"the manifest build does not list {installer}"
    assert "install.ps1" not in build
    assert workflow.index("Prepare release assets") < workflow.index(
        "Write and sign engine manifest"
    )
    # The upload block ships the same install.sh the build lists: the bare name
    # is the workspace copy that "$ws/install.sh" points at.
    upload = workflow[workflow.index('gh release upload "$TAG"'):]
    upload = upload[: upload.index("--clobber")]
    assert any(line.strip() == "install.sh \\" for line in upload.splitlines()), (
        "gh release upload does not ship the install.sh the manifest lists"
    )


def test_publish_rechecks_uploaded_installers_against_the_manifest() -> None:
    # #2483: the bytes that matter are the ones on the release, so the publish
    # job re-downloads them after the upload and verifies each against the
    # signed manifest. The checks read only the slice from the upload to the
    # next job, so a commented-out or `|| true` verify cannot pass on a
    # fragment that happens to sit elsewhere in the file.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    attach = workflow.index("- name: Attach release assets")
    upload = workflow.index('gh release upload "$TAG"', attach)
    step = workflow[upload : workflow.index("\n  release-smoke:")]

    # The Attach step fails the job on the first error, before anything uploads.
    assert "set -euo pipefail" in workflow[attach:upload]

    download = step.index('gh release download "$TAG" --dir "$uploaded"')
    assert download < step.index("ciao.release_manifest verify")
    for asset in (
        '"$uploaded/ciaobot-engine-manifest.json" "$uploaded/ciaobot-engine-manifest.json.sig"',
        '--asset "$uploaded/install.sh"',
        '--asset "$uploaded/install-engine.sh"',
    ):
        assert asset in step, f"publish.yml does not verify {asset}"

    # The verify statement itself: a live call, the two installers in it, and
    # no `||` on any of its lines, so a failure cannot be swallowed.
    lines = step.splitlines()
    start_line = next(i for i, line in enumerate(lines) if "ciao.release_manifest verify" in line)
    assert not lines[start_line].lstrip().startswith("#"), "the verify call is commented out"
    end_line = next(i for i in range(start_line, len(lines)) if ")" in lines[i])
    statement = lines[start_line : end_line + 1]
    for asset in (
        '--asset "$uploaded/install.sh"',
        '--asset "$uploaded/install-engine.sh"',
    ):
        assert any(asset in line for line in statement), f"verify does not check {asset}"
    assert not any("||" in line for line in statement), (
        "the manifest verify can fail without failing the job"
    )


def test_docs_drop_the_pinned_install_sh_digest() -> None:
    # #2483: the client verifies install.sh against the signed manifest, so the
    # docs no longer tell it to pin one release's digest, and the stale
    # "after 1.7.0" wording is gone from both files.
    from ciao.release_manifest import RELEASE_PUBLIC_KEY

    root = Path(__file__).parents[1]
    development = (root / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    architecture = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "next engine release after 1.7.0" not in development
    assert "next engine release after 1.7.0" not in architecture
    assert "must pin the sha256" not in development
    assert "pin the `install.sh` digest" not in development
    assert RELEASE_PUBLIC_KEY in development
    assert "version >= 1.8.1" in development
    assert "Authenticating `install.sh`" in development
