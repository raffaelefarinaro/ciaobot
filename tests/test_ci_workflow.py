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


def test_cold_start_uses_the_embedded_engine_for_launchagent_setup() -> None:
    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert 'engine="$app/Contents/Resources/ciao-runtime/bin/ciao"' in workflow
    assert '"$engine" setup --workspace' in workflow
