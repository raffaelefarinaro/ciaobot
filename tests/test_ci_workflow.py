from __future__ import annotations

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


def test_publish_attaches_only_the_five_engine_assets() -> None:
    # #653: the release carries the engine and nothing else. Exactly five
    # assets - the installer under both names, the wheel, and the manifest with
    # its signature - each pinned here so a re-added app asset is a failing
    # test rather than a surprise in a published release.
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
            --clobber
"""
    assert attached in workflow, (
        "publish.yml no longer attaches exactly the five engine assets"
    )

    # Both names are attached, from the one script, and both are proven present
    # on the release where the tag is still known - a missing asset would
    # otherwise only surface as a failed install on a user's machine.
    assert "cp scripts/install-engine.sh install.sh" in workflow
    assert "s/__VERIFIER_SHA256__/" not in workflow
    assert "verifier_name" not in workflow
    assert "grep -qx install.sh" in workflow
    assert "grep -qx install-engine.sh" in workflow


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
