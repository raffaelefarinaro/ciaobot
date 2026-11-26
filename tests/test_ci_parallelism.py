"""Keep expensive gates parallel without losing stable required check names."""

from pathlib import Path
import re
import tomllib

import yaml


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
    # Bounded like mac-app below: a hung e2e run must not hold a PR open
    # indefinitely.
    assert "\n    timeout-minutes: " in jobs["linux-e2e"]


def test_linux_e2e_storybook_steps_are_gated_and_ordered() -> None:
    # #3269 added the workshop gates to linux-e2e beside the e2e suite: the
    # Storybook build (which also runs the story-index check) and the browser
    # runner. They are independent signals, so the runner still runs after a
    # failed build (`!cancelled()`) and one failure cannot mask the other;
    # the job still fails. They also stay out of linux-web, which keeps
    # backend-only PRs fast. Parsed per step, like mac-app below: a dropped or
    # mis-typed web_changed guard would make a backend-only develop PR run -
    # and fail - web commands no backend PR pays for, and only this check
    # sees it.
    jobs = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    )["jobs"]
    steps = jobs["linux-e2e"]["steps"]
    gate = "needs.web-scope.outputs.web_changed == 'true'"
    assert steps[0]["name"] == "Require successful web scope detection"
    assert "if" not in steps[0]
    *web_steps, traces = steps[1:]
    assert traces["if"] == "failure()"
    assert [step.get("uses") for step in web_steps[:2]] == [
        "actions/checkout@v7",
        "actions/setup-node@v7",
    ]
    # Build first, then the runner, both after the Chromium install and
    # before the e2e suite.
    assert [step["run"] for step in web_steps[2:]] == [
        "npm ci",
        "npx playwright install --with-deps chromium",
        "npm run build-storybook",
        "npm run test-storybook",
        "npm run test:e2e",
    ]
    assert all(step.get("working-directory") == "web" for step in web_steps[2:])
    # The runner is the one web step allowed to run after a failed sibling:
    # it keeps the plain web_changed guard but drops the implicit success()
    # via `!cancelled()`, so a failed build still gives the runner a report.
    runner = next(
        step for step in web_steps if step.get("run") == "npm run test-storybook"
    )
    assert runner["if"] == "${{ " + gate + " && !cancelled() }}"
    siblings = [step for step in web_steps if step is not runner]
    assert [step.get("if") for step in siblings] == [gate] * len(siblings)
    # The issue keeps the workshop gates out of linux-web entirely: not one
    # step name or command there may mention Storybook.
    assert not [
        step
        for step in jobs["linux-web"]["steps"]
        if "storybook" in (step.get("name", "") + step.get("run", "")).lower()
    ]


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
    # on macOS and `mac-app` stays green on Linux. Parsed, not substring-matched:
    # a gate that moved to a comment or another key must fail.
    gate = "needs.mac-scope.outputs.mac_changed == 'true'"
    app_steps = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    )["jobs"]["mac-app"]["steps"]
    assert app_steps[0]["name"] == "Require successful mac scope detection"
    assert "if" not in app_steps[0]
    assert [step.get("if") for step in app_steps[1:]] == [gate] * 11
    assert "timeout-minutes: " in app
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
        "run: swift test --filter AppAssetsTests --filter BundledEngineTests",
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


def test_macos_browser_tests_reuse_the_pwa_build() -> None:
    # The macOS job builds the PWA for the fixture, so it must build only the
    # terminal harness and run Playwright directly. Going through
    # `npm run test:e2e` would run the prehook's second PWA build. Linux-e2e
    # checks out a clean tree and keeps that prehook.
    jobs = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    )["jobs"]
    steps = jobs["test"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Build PWA") < names.index("Build the terminal harness")
    assert names.index("Build the terminal harness") < names.index(
        "Run PWA browser tests"
    )
    by_name = {step["name"]: step for step in steps if "name" in step}
    assert by_name["Build the terminal harness"]["run"] == "npm run build:harness"
    assert by_name["Build the terminal harness"]["working-directory"] == "web"
    assert by_name["Run PWA browser tests"]["run"] == "npx playwright test"
    assert by_name["Run PWA browser tests"]["working-directory"] == "web"
    assert not any("test:e2e" in (step.get("run") or "") for step in steps)
    assert any(
        step.get("run") == "npm run test:e2e" for step in jobs["linux-e2e"]["steps"]
    )


# Slowest successful run of each job, in minutes, over 14 recent PR runs and 10
# manual full runs (docs/DEVELOPMENT.md, "CI time bounds"). A bound must leave at
# least twice that, so a slow runner does not fail a healthy run, and stay under
# a ceiling, so a hung run cannot hold a runner for hours.
SLOWEST_SUCCESS_MINUTES = {
    "web-scope": 0.3,
    "mac-scope": 0.3,
    "linux-server": 6.9,
    "linux-server-latest-python": 5.5,
    "linux-web": 5.8,
    "linux-install-smoke": 2.1,
    "linux-e2e": 9.9,
    "mac-app": 8.5,
    "test": 32.5,
}
CEILING_MINUTES = 90


def _workflow_jobs() -> dict:
    return yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    )["jobs"]


def test_every_job_has_a_sane_time_bound() -> None:
    jobs = _workflow_jobs()
    assert set(SLOWEST_SUCCESS_MINUTES) <= set(jobs)
    for name, slowest in SLOWEST_SUCCESS_MINUTES.items():
        bound = jobs[name]["timeout-minutes"]
        assert slowest * 2 <= bound <= CEILING_MINUTES, name


def test_pytest_steps_are_bounded_and_clean_up_orphans() -> None:
    # Run 45669361072 hung at 99% after every test passed, with orphan
    # pytest processes left over. The step bound ends the wait; the cleanup
    # step runs even after a timeout or cancel and dumps stacks before killing.
    jobs = _workflow_jobs()
    for name in ("linux-server", "linux-server-latest-python", "test"):
        steps = jobs[name]["steps"]
        index = next(i for i, s in enumerate(steps) if "pytest -n auto" in (s.get("run") or ""))
        bound = steps[index].get("timeout-minutes")
        assert bound is not None and bound < jobs[name]["timeout-minutes"], name
        cleanup = steps[index + 1]
        assert cleanup["if"] == "always()", name
        assert "pkill -ABRT" in cleanup["run"] and "pkill -KILL" in cleanup["run"], name


def test_pytest_faulthandler_bounds_a_single_hung_test() -> None:
    # faulthandler_timeout is per test: it names a hung test but cannot fire
    # after the suite finished, which the step timeouts above cover.
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    ini = pyproject["tool"]["pytest"]["ini_options"]
    assert 60 <= ini["faulthandler_timeout"] <= 900
    assert ini["faulthandler_exit_on_timeout"] is True
