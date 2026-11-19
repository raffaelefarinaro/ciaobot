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


def test_publish_workflow_uploads_wheel_manifest_and_sig_only() -> None:
    # #3130 R1: the 2.x release carries the engine wheel and the signed manifest
    # that lists it. The installer scripts and the server host archive are no
    # longer built, listed or attached, so the upload step's asset list is
    # exactly those three names. Parsed from the YAML so a re-added line fails
    # here, not in a release.
    import shlex

    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["build-engine"]["steps"]
    attach = next(step for step in steps if step.get("name") == "Attach release assets")
    script = attach["run"].replace("\\\n", " ")
    upload = next(
        line for line in script.splitlines() if line.lstrip().startswith("gh release upload")
    )
    tokens = shlex.split(upload.split("#", 1)[0], posix=True)
    assets = tokens[tokens.index('"$TAG"') + 1 :] if '"$TAG"' in tokens else tokens[4:]
    assets = [token for token in assets if not token.startswith("--")]
    assert assets == [
        "dist/ciaobot-*.whl",
        "ciaobot-engine-manifest.json",
        "ciaobot-engine-manifest.json.sig",
    ], assets

    text = (Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )
    for gone in ("install.sh", "install-engine.sh", "server-host", "--server-host", "--installer"):
        assert gone not in text, f"publish.yml still ships or lists {gone!r}"


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
        "linux-server", "linux-web", "linux-e2e", "mac-app"
    ]
    assert main["required_status_checks"]["contexts"] == [
        "linux-server", "linux-web", "linux-server-latest-python", "test", "mac-app"
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
