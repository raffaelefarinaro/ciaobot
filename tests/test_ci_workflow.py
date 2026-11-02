from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

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


def test_publish_attaches_only_the_seven_engine_assets() -> None:
    # #653: the release carries the engine and nothing else. #1022 adds one
    # prebuilt universal server host archive the signed manifest authenticates.
    # Exactly seven assets - the macOS installer under both names, the Windows
    # installer, the wheel, the manifest with its signature, and the host -
    # each pinned here so a re-added app asset is a failing test rather than a
    # surprise in a published release.
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
            "$host_archive" \\
            --clobber
"""
    assert attached in workflow, (
        "publish.yml no longer attaches exactly the seven engine assets"
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


def test_windows_job_checks_the_install_ps1_advisorily() -> None:
    # #838: the Windows installer can only be exercised on a Windows runner,
    # and the whole `windows` job is a measurement until C9 - a step that could
    # fail it would take every other measurement down with it.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/install.ps1" in workflow
    assert "    runs-on: windows-latest\n" in workflow
    windows_job = workflow.split("    runs-on: windows-latest\n", 1)[1].split("\n    steps:", 1)[0]
    assert "timeout-minutes: 45" in windows_job, "a hung step must not hold the runner for six hours"
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
    # `|| true`; a step that lost either would fail the job on a refusal. A step
    # may carry keys other than `run` between its name and its `run` block - the
    # end-to-end step sets `env:` there - so everything up to `run: |` is
    # skipped rather than assumed to be a single line.
    run_blocks = re.findall(
        r"(?ms)^    - name: ([^\n]+)\n(?:^(?!      run: \|\n)[^\n]*\n)*?      run: \|\n"
        r"(.*?)(?=^    - name:|\Z)",
        steps,
    )
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
    # The four markers the summary line and a human reader both grep for. They
    # are worded so that reading the log says what happened in one word each:
    # `installed` is the engine's own entry point, `uninstalled` is the absence
    # of the uv tool, and the two task lines are the schtasks probe either side
    # of -Uninstall. A step that failed before installing prints "installed: no"
    # and nothing pretends otherwise.
    for marker in (
        '"installed: yes"',
        '"task existed after install: yes"',
        '"uninstalled: yes',
        '"task gone after uninstall: yes',
        "workspace kept:",
        "task registered after -NoStart:",
        "-NoStart",
    ):
        assert marker in e2e, f"the end-to-end run no longer reports {marker!r}"

    # The summary line greps the same four markers, so a reader of the job
    # summary sees whether the installer ran at all without opening the artifact.
    summary = workflow.split("    - name: Summarize")[1]
    assert (
        "^(installed|task existed after install|uninstalled|task gone after uninstall): (yes|no)"
        in summary
    )
    assert "install.ps1 end-to-end:" in summary
    # The comments in this step quote `::add-mask::` while explaining why it is not
    # used; every check here is about the commands, not the prose about them.
    commands = "\n".join(
        line for line in e2e.splitlines() if not line.strip().startswith("#")
    )
    # Q3: the one-time setup URL is a credential and belongs in no log, least of
    # all in windows-install-e2e.txt, which is uploaded as an artifact. The token
    # is only known after the installer prints it, so it cannot be masked before
    # the fact: `::add-mask::` on its own masks the empty string and protects
    # nothing. The install output is redacted on the way into the file instead,
    # and the exit code is the installer's own, from `wait` on the background
    # job rather than from sed.
    assert "add-mask" not in commands, (
        "the setup URL token is redacted, not masked: it is unknown until the "
        "installer prints it, so no mask can cover it"
    )
    assert r"sed -E 's#(Open Ciaobot: ).*#\1[redacted]#'" in commands
    assert 'wait "$installer"\n            install_exit=$?' in commands, (
        "the install's exit code must come from the installer, not from sed's"
    )
    # A fresh install serves the browser setup wizard and waits for it. Nobody
    # is at a CI runner's browser, so the step finishes it over HTTP, and both
    # the wizard wait and the installer wait are bounded: an unbounded wait
    # held the Windows job for GitHub's six-hour limit on every develop push.
    assert "/api/setup/finish" in commands
    assert "deadline = time.time() + 600" in commands
    assert "taskkill /F /T /PID" in commands
    # The version the installer is given is cut from the wheel *file name*. The
    # path has to go through basename first: `cut -d- -f2` on
    # `ps1-e2e/release/ciaobot-1.2.3-py3-none-any.whl` yields
    # `e2e/release/ciaobot`, which the installer refuses as invalid, and the step
    # then reports "no" for everything having installed nothing.
    assert 'basename "$(ls ps1-e2e/release/*.whl' in commands
    assert "| head -1 | cut -d- -f2)" not in commands

    # The step's shell is Git Bash, whose MSYS path conversion rewrites the
    # `/Query`, `/TN` and `/v` arguments of the schtasks and reg probes below
    # into Windows paths: both programs were then asked about paths, printed
    # nothing, and reported "no" while the task and the PATH entry were there -
    # the install log said it had added the PATH entry and the very next line
    # contradicted it. MSYS_NO_PATHCONV=1 is what makes the probes measure the
    # machine instead of the shell, and it has to be on this step specifically:
    # `env:` scopes it to the run that needs it.
    step = workflow.split(
        "    - name: install.ps1 end-to-end install and uninstall (advisory)"
    )[1].split("\n    - name:")[0]
    assert "env:" in step, "the end-to-end step carries no env: mapping"
    assert re.search(r"(?m)^        MSYS_NO_PATHCONV: '1'$", step), (
        "the end-to-end step runs under Git Bash without MSYS_NO_PATHCONV, so its "
        "schtasks and reg probes read paths and report the shell, not the machine"
    )


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


def test_develop_prs_run_linux_split_and_only_fast_windows_gates() -> None:
    # A develop PR waits on the two parallel Linux jobs and on the Windows
    # import and win32 mypy gates. The Windows suites take over a quarter of
    # an hour on a hosted runner, so they share the macOS job's gate: pushes
    # to develop and release PRs into main.
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

    windows = by_name["windows"]
    assert (
        "WINDOWS_FULL: ${{ github.event_name != 'pull_request' || github.base_ref == 'main' || contains(github.event.pull_request.labels.*.name, 'windows-full') }}"
        in windows
    )
    steps = dict(re.findall(r"(?ms)^    - name: ([^\n]+)\n(.*?)(?=^    - |\Z)", windows))
    full = "if: ${{ !cancelled() && env.WINDOWS_FULL == 'true' }}"
    for name in ("Import every ciao module", "Python static type check (win32)"):
        assert "WINDOWS_FULL" not in steps[name], f"{name!r} must run on every PR"
    for name in (
        "Windows backend tests",
        "Windows frontend tests",
        "Build install.ps1 offline fixture",
        "Build install.ps1 end-to-end fixture",
        "install.ps1 offline verification and analyzer (advisory)",
        "install.ps1 end-to-end install and uninstall (advisory)",
    ):
        assert full in steps[name], f"{name!r} is not gated on WINDOWS_FULL"


def test_windows_full_label_opts_a_develop_pr_in() -> None:
    # #2360: a develop PR labelled windows-full runs the full Windows suites.
    # The label is read from the event payload, so the pull_request trigger
    # must keep no `types:` filter: a `labeled` type would rerun all of CI on
    # every label change.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    on_block = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    pull_request_block = on_block.split("  pull_request:", 1)[1]
    assert "types:" not in pull_request_block

    jobs = re.split(r"(?m)^  (?=[a-z-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}
    windows_full = [
        line
        for line in by_name["windows"].splitlines()
        if line.strip().startswith("WINDOWS_FULL:")
    ]
    assert len(windows_full) == 1
    assert (
        "contains(github.event.pull_request.labels.*.name, 'windows-full')"
        in windows_full[0]
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
    assert "astral-sh/setup-uv@v10.3.2" in workflow

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
    sys.platform == "win32" or shutil.which("bash") is None,
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
