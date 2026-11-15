"""Keep expensive gates parallel without losing stable required check names."""

from pathlib import Path
import re


def _jobs() -> dict[str, str]:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    return dict(re.findall(r"(?ms)^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", workflow.split("\njobs:\n")[1]))


def test_browser_waits_only_for_fast_scope_detection() -> None:
    jobs = _jobs()
    assert "needs: web-scope" in jobs["linux-e2e"]
    assert "needs.web-scope.outputs.web_changed" in jobs["linux-e2e"]
    assert "needs: linux-web" not in jobs["linux-e2e"]
    assert "Detect web changes" in jobs["web-scope"]
    assert "npm" not in jobs["web-scope"]
    assert "always()" in jobs["linux-e2e"]
    assert 'test "$SCOPE_RESULT" = success' in jobs["linux-e2e"]


def test_only_superseded_pr_runs_are_cancelled() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    assert "concurrency:" in workflow
    assert "github.event.pull_request.number || github.sha" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow


def test_lean_develop_and_full_release_scopes() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    assert "  push:" not in workflow.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in workflow
    jobs = _jobs()
    assert "github.base_ref == 'main'" in jobs["linux-server-latest-python"].split("steps:", 1)[0]
    assert not [name for name in jobs if name.startswith("windows")]
    assert "windows-full" not in workflow
    nightly = (Path(__file__).parents[1] / ".github/workflows/nightly-ci.yml").read_text()
    assert "schedule:" in nightly
    assert "gh workflow run ci.yml --ref develop" in nightly
