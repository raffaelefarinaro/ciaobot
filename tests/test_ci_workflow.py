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


def test_runtime_resolution_uses_checked_in_pins() -> None:
    root = Path(__file__).parents[1] / ".github" / "workflows"
    for name in ("ci.yml", "publish.yml"):
        workflow = (root / name).read_text(encoding="utf-8")
        assert ". scripts/pinned-python-runtime.env" in workflow
        assert "CIAO_PYTHON_ARM64_SHA256" in workflow
        assert "CIAO_PYTHON_X86_64_SHA256" not in workflow
        assert "aarch64-apple-darwin" in workflow
        assert "x86_64-apple-darwin" not in workflow


def test_publish_workflow_ships_signed_engine_manifest() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    # The engine wheel is a release asset in its own right (#562), and the
    # manifest beside it is signed with the same key the installer verifier
    # trusts, so the future installer (#568) and updater (#569) can check it is
    # authentic rather than merely intact. The desktop assets stay in the list.
    for fragment in (
        "uv build --wheel --out-dir dist",
        "-m ciao.release_manifest check-static",
        "-m ciao.release_manifest build",
        "npx tauri signer sign ../ciaobot-engine-manifest.json",
        "-m ciao.release_manifest verify",
        "dist/ciaobot-*.whl",
        "ciaobot-engine-manifest.json.sig",
        "latest.json",
        "Ciaobot_*_aarch64.app.tar.gz",
    ):
        assert fragment in workflow, (
            f"publish.yml no longer publishes the engine manifest: {fragment!r}"
        )

    # The wheel has to carry the PWA, so it is built after the frontend build.
    assert workflow.index("Build PWA assets") < workflow.index("Build engine wheel")


def test_publish_uploads_engine_installer() -> None:
    # The engine-only installer is served from /releases/latest/download, which
    # is a release asset, so it has to be attached with the rest (#568).
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/install-engine.sh" in workflow


def test_transition_release_keeps_the_app_updater_and_repoints_install_sh() -> None:
    # The transition release (#651) keeps the updater feed and changes what the
    # fresh-install path installs, without renaming it: an already-installed
    # Ciaobot.app still has to hear about this release, or the hand-over offer
    # never reaches anyone, while a first-time user must get the engine and must
    # not be able to install the app a second time.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")

    # The updater half: latest.json and the signed app archive, signature
    # included, are still release assets.
    for fragment in (
        "latest.json",
        "Ciaobot_*_aarch64.app.tar.gz",
        "Ciaobot_*_aarch64.app.tar.gz.sig",
    ):
        assert fragment in workflow, (
            f"publish.yml no longer offers the app its update: {fragment!r}"
        )

    # The install half: install.sh is a copy of the engine installer, so every
    # name the docs and any stale external link already point at installs the
    # engine, and the app installer is never generated. Byte-identity of the two
    # release assets is the smoke test's job - there is no local release build
    # here to compare.
    assert "cp scripts/install-engine.sh install.sh" in workflow
    assert "s/__VERIFIER_SHA256__/" not in workflow
    assert "verifier_name" not in workflow

    # Both names are attached, from the one script, and the verifier - which only
    # the retired app installer used - is not: its build stays for #579, but
    # nothing downloads it.
    upload = workflow[workflow.index("gh release upload") :]
    for asset in ("install.sh", "scripts/install-engine.sh"):
        assert asset in upload, f"publish.yml no longer attaches {asset}"
    assert "ciaobot-installer-verify_aarch64" not in upload


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
    ):
        assert gone not in commands, (
            f"release-smoke.yml still expects install.sh to install the app: {gone!r}"
        )

    # The updater notice is the part that has to keep working, and the two
    # installer assets have to be the same bytes: the app's Move action fetches
    # install-engine.sh (#604) and would be stranded by a release where the
    # public install.sh and that alias drift apart.
    assert '.platforms["darwin-aarch64"].url' in workflow
    assert "Ciaobot_${VERSION}_aarch64.app.tar.gz" in workflow
    assert "grep -q -- '--migrate' install.sh" in workflow
    assert "shasum -a 256 install.sh" in workflow
    assert "shasum -a 256 install-engine.sh" in workflow


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


def test_cold_start_uses_the_embedded_engine_for_launchagent_setup() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert 'engine="$app/Contents/Resources/ciao-runtime/bin/ciao"' in workflow
    assert '"$engine" setup --workspace' in workflow
