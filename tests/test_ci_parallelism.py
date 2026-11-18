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


def test_mac_app_waits_only_for_fast_mac_scope_detection() -> None:
    jobs = _jobs()
    scope = jobs["mac-scope"]
    assert "Detect mac changes" in scope
    assert "fetch-depth: 0" in scope
    assert (
        "outputs:\n      mac_changed: ${{ steps.mac-changes.outputs.mac_changed "
        "|| steps.mac-full.outputs.mac_changed }}"
    ) in scope
    assert "swift" not in scope
    assert '-- mac/ web/src/lib/workspaceColors.ts pyproject.toml uv.lock)"' in scope
    app = jobs["mac-app"]
    assert "needs: mac-scope" in app
    assert "if: ${{ !cancelled() }}" in app
    assert 'test "$SCOPE_RESULT" = success' in app
    assert "macos-15" in app
    assert "working-directory: mac" in app
    assert "always()" not in app
    # Every step but the scope check is gated, so an unrelated PR runs nothing
    # on macOS and `mac-app` stays green on Linux.
    gate = "if: needs.mac-scope.outputs.mac_changed == 'true'"
    app_steps = app.split("\n    - ")[1:]
    assert app_steps[0].startswith("name: Require successful mac scope detection")
    assert all(gate in step for step in app_steps[1:])
    assert app.count(gate) == 11
    assert "timeout-minutes: 60" in app
    assert "python-version: '3.12'" in app
    assert "path: mac/.build/engine-cache" in app
    assert (
        "key: mac-engine-wheelhouse-${{ hashFiles('pyproject.toml', 'uv.lock', "
        "'mac/engine-runtime.json', 'mac/engine-build-inputs.json') }}"
    ) in app
    assert "restore-keys" not in app
    steps = [
        'run: test "$(uname -m)" = arm64',
        "actions/setup-python@v7",
        "actions/setup-node@v7",
        "actions/cache@v6",
        "run: swift test\n",
        "run: swift build\n",
        "run: scripts/build-app.sh",
        "run: swift test --filter AppAssetsTests",
        "run: plutil -lint .build/Ciaobot.app/Contents/Info.plist",
    ]
    found = [app.index(step) for step in steps]
    assert found == sorted(found)
    assert len(found) == len(set(found))


def test_mac_full_runs_always_build_the_app() -> None:
    job = _jobs()["mac-scope"]
    full = job.split("id: mac-full", 1)[1]
    assert (
        "if: ${{ !(github.event_name == 'pull_request' && github.base_ref == 'develop') }}"
        in full
    )
    assert "printf 'mac_changed=true\\n' >> \"$GITHUB_OUTPUT\"" in full
