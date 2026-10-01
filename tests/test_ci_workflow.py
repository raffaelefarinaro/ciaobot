from __future__ import annotations

import re
from pathlib import Path


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
    assert "branches: [ develop ]" in workflow
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
    root = Path(__file__).parents[1]
    assert not (root / "scripts" / "install.sh").exists()


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
    # #653: the release carries the engine and nothing else. Exactly six
    # assets - the macOS installer under both names, the Windows installer, the
    # wheel, and the manifest with its signature - each pinned here so a
    # re-added app asset is a failing test rather than a surprise in a published
    # release.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    attached = """
          gh release upload "$TAG" \\
            install.sh \\
            scripts/install-engine.sh \\
            scripts/install.ps1 \\
            dist/ciaobot-*.whl \\
            ciaobot-engine-manifest.json \\
            ciaobot-engine-manifest.json.sig \\
            --clobber
"""
    assert attached in workflow, (
        "publish.yml no longer attaches exactly the six engine assets"
    )

    # Every name is attached and proven present on the release where the tag is
    # still known - a missing asset would otherwise only surface as a failed
    # install on a user's machine. install.ps1 is uploaded as the file itself,
    # so /releases/latest/download/install.ps1 resolves without a rename.
    assert "cp scripts/install-engine.sh install.sh" in workflow
    assert "s/__VERIFIER_SHA256__/" not in workflow
    assert "verifier_name" not in workflow
    assert "grep -qx install.sh" in workflow
    assert "grep -qx install-engine.sh" in workflow
    assert "grep -qx install.ps1" in workflow


def test_windows_job_checks_the_install_ps1_advisorily() -> None:
    # #838: the Windows installer can only be exercised on a Windows runner,
    # and the whole `windows` job is a measurement until C9 - a step that could
    # fail it would take every other measurement down with it.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/install.ps1" in workflow
    assert "-ReleaseDir ./ps1-fixture/release -DryRun" in workflow
    assert "Invoke-ScriptAnalyzer" in workflow
    assert "windows-install-ps1.txt" in workflow
    # The offline fixture is built from the same helpers the test suite uses, so
    # it has to import them from a checkout root.
    assert 'sys.path.insert(0, ".")' in workflow
    assert "from tests.test_engine_installer import" in workflow

    # Every step that touches the installer redirects into its own log and ends
    # in `|| true`: the job may report a refusal, never fail on one. #853 added
    # a second pair of steps (the end-to-end fixture and the end-to-end
    # install/uninstall run), so the property is checked per step rather than by
    # pinning whichever one happens to be last.
    steps = workflow.split("    - name: Build install.ps1 offline fixture")[1]
    steps = steps.split("    - name: Summarize")[0]
    assert steps.count("|| true") >= 2
    # Each run step's own `run` block ends with its own log redirection and
    # `|| true`; a step that lost either would fail the job on a refusal.
    run_blocks = re.findall(r"(?ms)^    - name: ([^\n]+)\n      run: \|\n(.*?)(?=^    - name:|\Z)", steps)
    assert run_blocks, "no install.ps1 run steps found in the windows job"
    for name, body in run_blocks:
        # The comment block introducing the *next* step is captured with this
        # one's body; it is not part of the command.
        body = re.sub(r"(?m)^\s*#.*$", "", body)
        lines = [line.strip() for line in body.rstrip().splitlines() if line.strip()]
        if lines[0].startswith("python - <<"):
            # A heredoc step: the guard is on the command that opens it, the way
            # the offline fixture step has always done it.
            assert lines[0].endswith("|| true"), (
                f"the '{name}' step would fail the windows job: it does not end in '|| true'"
            )
            continue
        last = lines[-1]
        assert last.endswith("2>&1 || true"), (
            f"the '{name}' step would fail the windows job: it does not end in '|| true'"
        )
        assert last.startswith("} > windows-"), (
            f"the '{name}' step does not redirect its output to a windows-*.txt log"
        )
    names = [name for name, _ in run_blocks]
    assert "install.ps1 offline verification and analyzer (advisory)" in names
    assert "install.ps1 end-to-end install and uninstall (advisory)" in names
    assert "} > windows-install-ps1.txt 2>&1 || true" in steps
    assert "} > windows-install-e2e.txt 2>&1 || true" in steps
    # The end-to-end run reports whether the task existed after install and
    # after uninstall, and never fails on either answer: an InteractiveToken
    # task may not register or start on a hosted runner, and that is the datum.
    e2e = dict(run_blocks)["install.ps1 end-to-end install and uninstall (advisory)"]
    assert "task registered after install:" in e2e
    assert "task registered after uninstall:" in e2e
    assert "workspace kept:" in e2e
    assert "-NoStart" in e2e
    assert "::add-mask::" in e2e, "the one-time setup URL is a credential and belongs in no log"


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
    # has no meaning, and the macOS job runs on develop pushes and PRs to main
    # regardless - which is the coverage that matters.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "  changes:" not in workflow
    assert "needs: changes" not in workflow
    assert "needs.changes" not in workflow
    assert "runs-on: macos-latest" in workflow
    # #655: dropping the `changes` job also dropped the `if:` it fed, and
    # without a gate the macOS job would run on every develop PR. The gate is
    # exactly develop pushes and PRs into main.
    assert (
        "if: github.event_name != 'pull_request' || github.base_ref == 'main'"
        in workflow
    )
