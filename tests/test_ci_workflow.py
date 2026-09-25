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


def test_cold_start_uses_the_embedded_engine_for_launchagent_setup() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert 'engine="$app/Contents/Resources/ciao-runtime/bin/ciao"' in workflow
    assert '"$engine" setup --workspace' in workflow
