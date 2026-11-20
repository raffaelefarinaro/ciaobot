from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from ciao.release_manifest import build_manifest
from tests.test_release_manifest import _keypair, _sign


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


def _publish_workflow() -> dict:
    import yaml

    return yaml.safe_load(
        (Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
    )


def _publish_steps() -> list[dict]:
    return _publish_workflow()["jobs"]["build-engine"]["steps"]


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def _index_containing(steps: list[dict], fragment: str) -> int:
    return next(i for i, step in enumerate(steps) if fragment in step.get("run", ""))


def _upload_names(script: str) -> list[str]:
    # Every `gh release upload` line, with continuations joined, reduced to the
    # file names it attaches. Parsed from the YAML so a re-added line fails here.
    import shlex

    names: list[str] = []
    for line in script.replace("\\\n", " ").splitlines():
        if not line.lstrip().startswith("gh release upload"):
            continue
        tokens = shlex.split(line.split("#", 1)[0], posix=True)
        tag = tokens.index("$TAG")
        names += [Path(token).name for token in tokens[tag + 1 :] if not token.startswith("--")]
    return names


def _uploaded_assets(steps: list[dict]) -> list[str]:
    return [name for step in steps for name in _upload_names(step.get("run", ""))]


def test_publish_attaches_two_assets_always_and_four_on_the_legacy_release() -> None:
    # #3131 L5: every release attaches the stamped installer and its wheel. The
    # 1.x manifest and its signature ride along on the legacy release (2.0.0)
    # only, behind the decision step. The published-assets check pins the exact
    # set at runtime; this pins which upload step carries which file.
    steps = _publish_steps()
    always = _step(steps, "Attach release assets")
    legacy = _step(steps, "Attach the 1.x engine manifest")

    assert _upload_names(always["run"]) == ["install.sh", "ciaobot-*.whl"]
    assert _upload_names(legacy["run"]) == [
        "ciaobot-engine-manifest.json",
        "ciaobot-engine-manifest.json.sig",
    ]
    assert legacy["if"] == "steps.legacy.outputs.publish == 'true'"
    assert "if" not in always
    assert _uploaded_assets(steps) == [
        "install.sh",
        "ciaobot-*.whl",
        "ciaobot-engine-manifest.json",
        "ciaobot-engine-manifest.json.sig",
    ]


def test_publish_uploads_the_stamped_linux_installer() -> None:
    steps = _publish_steps()
    stamp = _step(steps, "Stamp the Linux installer")["run"]

    # The installer is the template stamped against this release's own wheel,
    # from the release's own ciao, and written where the upload picks it up.
    assert "-m ciao.release_manifest stamp-installer" in stamp
    assert '--template "$ws/scripts/install.sh"' in stamp
    assert '--wheel "$ws/dist/ciaobot-$version-py3-none-any.whl"' in stamp
    assert '--out "$ws/install.sh"' in stamp
    assert (
        _index_containing(steps, "stamp-installer")
        < _index_containing(steps, "gh release upload")
    )

    # The published copy is read back and must pin this version and the
    # attached wheel's digest, so a stale or swapped installer fails the job.
    check = _step(steps, "Check the published assets")["run"]
    assert 'grep -Fx "CIAOBOT_VERSION=$version" "$downloaded/install.sh" || {' in check
    assert 'grep -Fx "WHEEL_SHA256=$digest" "$downloaded/install.sh" || {' in check


def test_publish_runs_the_legacy_update_gate_before_upload() -> None:
    # A 1.x engine must be able to stage the release before anything is attached.
    steps = _publish_steps()

    assert _index_containing(steps, "legacy-update-gate.py") < _index_containing(
        steps, "gh release upload"
    )


def test_publish_never_uploads_retired_installers_or_host() -> None:
    names = _uploaded_assets(_publish_steps())

    assert "install-engine.sh" not in names
    assert "install.ps1" not in names
    assert not [name for name in names if name.startswith("ciaobot-server-host")]
    text = (Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )
    assert "--server-host" not in text
    assert "--installer" not in text


def test_publish_ships_the_legacy_manifest_for_2_0_0_only() -> None:
    # Maintainer decision (#3131 Q3, 2026-11-18): the wheel-only manifest that 1.x
    # engines read is published for 2.0.0 only. The version is named once, in the
    # workflow env, and every step that makes or attaches the manifest is gated on
    # the decision step, whose output the behavioural test below pins.
    workflow = _publish_workflow()
    steps = workflow["jobs"]["build-engine"]["steps"]

    assert workflow["env"]["LEGACY_MANIFEST_VERSION"] == "2.0.0"
    decide = _step(steps, "Decide whether the 1.x engine manifest ships")["run"]
    assert '"${VERSION#v}" = "$LEGACY_MANIFEST_VERSION"' in decide
    assert '= "2.0.0"' not in decide
    for name in (
        "Write and sign engine manifest",
        "1.x updater accepts this release",
        "Attach the 1.x engine manifest",
    ):
        assert _step(steps, name)["if"] == "steps.legacy.outputs.publish == 'true'", name
    assert "ciaobot-engine-manifest" not in _step(steps, "Attach release assets")["run"]


def _run_bash_step(
    run: str, env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", run],
        env={"PATH": os.environ["PATH"], **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("tag", "published"),
    [("v2.0.0", "true"), ("v1.10.1", "false"), ("v2.0.1", "false"), ("v2.1.0", "false")],
)
def test_legacy_decision_step_outputs(tmp_path: Path, tag: str, published: str) -> None:
    # Runs the real step: a swapped true/false echo, or a dropped notice, fails here.
    workflow = _publish_workflow()
    step = _step(workflow["jobs"]["build-engine"]["steps"], "Decide whether the 1.x engine manifest ships")
    output = tmp_path / "github-output"
    output.touch()

    result = _run_bash_step(
        step["run"],
        {
            "VERSION": tag,
            "GITHUB_OUTPUT": str(output),
            "LEGACY_MANIFEST_VERSION": workflow["env"]["LEGACY_MANIFEST_VERSION"],
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"publish={published}\n"
    if published == "false":
        assert f"::notice::1.x engine manifest not published for {tag}" in result.stdout
    else:
        assert "::notice::" not in result.stdout


def test_publish_workflow_skips_the_updates_feed_release() -> None:
    # The Mac update feed is a pre-release named `updates` in this repository.
    # Publishing it fires the same `release: published` event as a version
    # release, so the build job must refuse that tag or it would build and
    # publish an engine manifest for the feed. Pinned on the parsed job `if:`.
    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
    )
    condition = workflow["jobs"]["build-engine"]["if"]
    assert "github.event_name == 'release'" in condition, condition
    assert "github.event.release.tag_name != 'updates'" in condition, condition


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


def _release_smoke() -> dict:
    import yaml

    return yaml.safe_load(_smoke_text())


def _smoke_text() -> str:
    return (Path(__file__).parents[1] / ".github" / "workflows" / "release-smoke.yml").read_text(
        encoding="utf-8"
    )


def _smoke_runs(job: dict) -> str:
    return "\n".join(step["run"] for step in job["steps"] if "run" in step)


def _smoke_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _shell_function(run: str, name: str) -> str:
    # The text of one bash function inside a run: block, from its opening line
    # to the closing brace, so a test can execute the real redaction.
    lines = run.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}() {{")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "\n".join(lines[start : end + 1])


# Every `|| true` line in a run: block of release-smoke.yml. The port lookup
# tolerates a missing .env under pipefail; the diagnostics commands keep
# collecting after one of them fails. Any other `|| true` fails the test.
_BENIGN_OR_TRUE = {
    "port=$(awk -F= '/^PWA_PORT=/{print $2}' \"$SMOKE_WORKSPACE/.env\" 2>/dev/null | tr -d '\"' | tail -1 || true)",
    'redact < "$RUNNER_TEMP/linux-install.log" 2>/dev/null | tail -200 || true',
    'redact < "$RUNNER_TEMP/linux-update.log" 2>/dev/null | tail -200 || true',
    "systemctl status ciaobot --no-pager 2>&1 | head -60 | redact || true",
    "sudo -n journalctl -u ciaobot -n 200 --no-pager 2>&1 | redact || true",
    'tail -80 "$debug_log" | redact || true',
}


def test_release_smoke_installs_linux_with_the_service_and_reruns() -> None:
    # #3131 L6: the smoke test runs the public Linux one-liner on an Ubuntu VM,
    # checks the systemd service and the startup API, then runs the same
    # one-liner again as a same-version reinstall and checks the service comes back.
    smoke = _release_smoke()
    linux = smoke["jobs"]["linux-install"]
    assert linux["runs-on"] == "ubuntu-24.04"
    runs = _smoke_runs(linux)

    for fragment in (
        "/releases/download/v${VERSION}/install.sh",
        "| sh -s -- --workspace \"$workspace\"",
        'test -x "$HOME/.local/bin/ciao"',
        '"ciao $VERSION"',
        "systemctl is-active --quiet ciaobot",
        "/api/startup-status",
    ):
        assert fragment in runs, f"release-smoke.yml linux-install lost {fragment!r}"

    # The re-run is the same one-liner with no flags, after the first install.
    names = [step.get("name", "") for step in linux["steps"]]
    rerun = names.index("Re-run the one-liner (same-version reinstall)")
    update = linux["steps"][rerun]["run"]
    assert "install.sh" in update and "| sh;" in update
    assert "sh -s" not in update
    assert names.index("Install with the one-line installer") < rerun
    assert "systemctl is-active --quiet ciaobot" in update

    # It must not look for the retired macOS app anywhere in what it runs.
    for gone in (
        "Ciaobot.app",
        "ciaobot-desktop",
        "ciao-runtime",
        "Ciaobot.plist",
        "com.ciao.server",
        "latest.json",
        "app.tar.gz",
        "ciaobot-installer-verify",
        "install-engine.sh",
    ):
        assert gone not in runs, f"release-smoke.yml still expects the app release: {gone!r}"


def test_release_smoke_same_version_rerun_must_restart_the_service() -> None:
    # The re-run is a reinstall, and `ciao service install` restarts the unit, so
    # the main PID has to change across it. Without this check a re-run that
    # left the old process running would pass.
    update = _smoke_step(
        _release_smoke()["jobs"]["linux-install"],
        "Re-run the one-liner (same-version reinstall)",
    )["run"]
    before = update.index('pid_before=$(systemctl show -p MainPID --value ciaobot.service)')
    download = update.index("install.sh")
    after = update.index('pid_after=$(systemctl show -p MainPID --value ciaobot.service)')
    assert before < download < after
    assert '[ "$pid_before" -gt 0 ]' in update
    assert '[ "$pid_after" -gt 0 ]' in update
    assert '[ "$pid_after" != "$pid_before" ]' in update


def test_release_smoke_waits_are_bounded() -> None:
    # A smoke run must fail instead of hang: every job and every step that
    # waits or downloads has a timeout, and every wait is a counted loop with
    # curl bounded by --max-time.
    smoke = _release_smoke()
    for name, job in smoke["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {name} has no timeout"
        for step in job["steps"]:
            run = step.get("run", "")
            if "sleep" in run or "install.sh" in run:
                assert isinstance(step.get("timeout-minutes"), int), (
                    f"job {name} step {step.get('name')!r} waits without a timeout"
                )
            assert "while " not in run and "until " not in run, (
                f"job {name} step {step.get('name')!r} has an unbounded loop"
            )
    text = _smoke_text()
    assert "seq 1 90" in text
    assert text.count("curl -fsS --max-time 5") >= 4
    assert text.count("curl -fsSL --max-time 600") == 2  # the install and the update download


def test_release_smoke_run_blocks_fail_closed() -> None:
    # Every run: block except Diagnostics starts with set -euo pipefail, and
    # `|| true` appears only on the explicit benign allowlist above. Mutation:
    # appending `|| true` to any assertion line adds a line outside the list.
    smoke = _release_smoke()
    found: set[str] = set()
    for name, job in smoke["jobs"].items():
        for step in job["steps"]:
            run = step.get("run")
            if run is None:
                continue
            label = f"job {name} step {step.get('name')!r}"
            if step.get("name") != "Diagnostics":
                assert run.lstrip().startswith("set -euo pipefail"), f"{label} is not fail-closed"
            for line in run.splitlines():
                if "|| true" in line:
                    found.add(line.strip())
    assert found == _BENIGN_OR_TRUE


def test_release_smoke_redacts_the_login_url_in_every_log_it_prints() -> None:
    # ciao setup prints the one-time login URL, and a bare login token line is
    # a credential too. Each log the job prints goes through the same rules.
    text = _smoke_text()
    assert text.count("Open Ciaobot: ).*#\\1[redacted]") == 3  # install, update failure, diagnostics
    assert text.count("^[A-Za-z0-9_-]{32,}$#[redacted]") == 3
    assert "journalctl -u ciaobot -n 200" in text

    # The journal and the service status both go through redact.
    diagnostics = _smoke_runs(_release_smoke()["jobs"]["linux-install"])
    status = next(line for line in diagnostics.splitlines() if "systemctl status ciaobot" in line)
    journal = next(line for line in diagnostics.splitlines() if "journalctl -u ciaobot" in line)
    assert "| redact" in status
    assert "| redact" in journal


_REDACTION_SAMPLE = "\n".join(
    [
        "Open Ciaobot: http://127.0.0.1:8443/?setup=abc",
        "login https://example.test/x?token=SECRET123 done",
        "aB3_dE5-fG7hI9jK1lM3nO5pQ7rS9tU1v",
        "ciao 2.0.0",
    ]
) + "\n"
_REDACTION_EXPECTED = "Open Ciaobot: [redacted]\nlogin [redacted] done\n[redacted]\nciao 2.0.0\n"


def test_release_smoke_redaction_acts_on_the_install_log(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("runs the workflow's bash")
    install = _smoke_step(_release_smoke()["jobs"]["linux-install"], "Install with the one-line installer")
    log = tmp_path / "install.log"
    log.write_text(_REDACTION_SAMPLE, encoding="utf-8")

    result = _run_bash_step(
        f'install_log="{log}"\n{_shell_function(install["run"], "show_log")}\nshow_log', {}
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == _REDACTION_EXPECTED


def test_release_smoke_diagnostics_never_reads_the_setup_token(tmp_path: Path) -> None:
    # Runs the real Diagnostics step against a workspace that holds the login
    # token file. The token must not reach the output, the journal and status
    # lines are redacted, and the allowlisted server_debug.log is still printed.
    if sys.platform == "win32":
        pytest.skip("runs the workflow's bash")
    diagnostics = _smoke_step(_release_smoke()["jobs"]["linux-install"], "Diagnostics")["run"]
    assert "setup-token" not in diagnostics
    assert "find " not in diagnostics

    workspace = tmp_path / "ws"
    runtime = workspace / ".runtime"
    runtime.mkdir(parents=True)
    token = "BARE-LOGIN-TOKEN-do-not-print-0123456789"
    (runtime / "setup-token").write_text(token + "\n", encoding="utf-8")
    (runtime / "server_debug.log").write_text("debug line\n" + "A" * 40 + "\n", encoding="utf-8")

    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "systemctl").write_text(
        "#!/bin/sh\necho 'STATUS Open Ciaobot: https://h/?token=JOURNALSECRET'\n", encoding="utf-8"
    )
    (stubs / "sudo").write_text("#!/bin/sh\necho 'JOURNAL https://h/?setup=JOURNALSECRET2'\n", encoding="utf-8")
    for stub in stubs.iterdir():
        stub.chmod(0o755)

    result = _run_bash_step(
        diagnostics,
        {
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "SMOKE_WORKSPACE": str(workspace),
        },
    )

    assert result.returncode == 0, result.stderr
    assert token not in result.stdout
    assert "JOURNALSECRET" not in result.stdout
    assert "A" * 40 not in result.stdout
    assert "== server_debug.log" in result.stdout
    assert "debug line" in result.stdout
    assert "STATUS Open Ciaobot: [redacted]" in result.stdout
    assert "JOURNAL [redacted]" in result.stdout


def test_release_smoke_runs_the_legacy_update_gate_against_published_assets() -> None:
    # The 1.x updater check only exists for the release that ships the 1.x
    # manifest. The job is conditioned on that version, read from the same
    # LEGACY_MANIFEST_VERSION value publish.yml uses, and runs the gate from the
    # tagged checkout against the published assets.
    smoke = _release_smoke()
    assert smoke["env"]["LEGACY_MANIFEST_VERSION"] == "2.0.0"

    legacy = smoke["jobs"]["legacy-update"]
    assert legacy["needs"] == "resolve"
    assert legacy["if"] == "needs.resolve.outputs.legacy == 'true'"
    assert legacy["runs-on"] == "macos-latest"

    resolve = _smoke_runs(smoke["jobs"]["resolve"])
    assert 'if [ "$version" = "$LEGACY_MANIFEST_VERSION" ]; then' in resolve

    checkout = next(step for step in legacy["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "v${{ needs.resolve.outputs.version }}"

    gate = next(step for step in legacy["steps"] if "legacy-update-gate.py" in step.get("run", ""))
    assert "python3 scripts/legacy-update-gate.py" in gate["run"]
    assert "--release-base https://github.com/raffaelefarinaro/ciaobot/releases/download" in gate["run"]
    assert '--target "$VERSION"' in gate["run"]
    assert "--uv \"$(command -v uv)\"" in gate["run"]


def test_release_smoke_gate_step_pins_its_token_and_timeout() -> None:
    gate = next(
        step
        for step in _release_smoke()["jobs"]["legacy-update"]["steps"]
        if "legacy-update-gate.py" in step.get("run", "")
    )
    assert gate["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert isinstance(gate.get("timeout-minutes"), int)


def test_release_smoke_and_publish_name_the_same_legacy_manifest_version() -> None:
    assert (
        _release_smoke()["env"]["LEGACY_MANIFEST_VERSION"]
        == _publish_workflow()["env"]["LEGACY_MANIFEST_VERSION"]
    )


@pytest.mark.parametrize(
    ("requested", "accepted"),
    [
        ("2.0.0", True),
        ("v2.0.0", True),
        ("1.10.0", True),
        ("10.0.12", True),
        ("2.0", False),
        ("2.0.0.0", False),
        ("02.0.0", False),
        ("2.00.0", False),
        ("2.0.0-rc1", False),
        ("2.0.0+build", False),
        ("2..0", False),
        ("2.0.x", False),
        ("v", False),
        ("", False),
        ("2.0.0\n", False),
    ],
)
def test_release_smoke_resolve_accepts_only_strict_semver(
    tmp_path: Path, requested: str, accepted: bool
) -> None:
    # Runs the real resolve step: a looser pattern, or a dropped check, fails here.
    if sys.platform == "win32":
        pytest.skip("runs the workflow's bash")
    smoke = _release_smoke()
    resolve = _smoke_step(smoke["jobs"]["resolve"], "Normalize version")["run"]
    output = tmp_path / "github-output"
    output.touch()

    result = _run_bash_step(
        resolve,
        {
            "REQUESTED": requested,
            "GITHUB_OUTPUT": str(output),
            "LEGACY_MANIFEST_VERSION": smoke["env"]["LEGACY_MANIFEST_VERSION"],
        },
    )

    assert (result.returncode == 0) is accepted, result.stderr
    if accepted:
        assert output.read_text(encoding="utf-8").startswith("version=")


def test_release_smoke_runs_only_when_called_by_publish_or_started_by_hand() -> None:
    smoke = _release_smoke()
    triggers = smoke.get("on", smoke.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert set(triggers) == {"workflow_call", "workflow_dispatch"}

    call = _publish_workflow()["jobs"]["release-smoke"]
    assert call["uses"] == "./.github/workflows/release-smoke.yml"
    assert call["if"] == "github.event_name == 'release'"


def test_release_smoke_keeps_expressions_out_of_run_blocks_and_secrets_out() -> None:
    # Security-sensitive workflow: no expression is interpolated into a shell
    # script (values come in through env:), no repository secret is read, and
    # every action is pinned to a version, not a branch.
    smoke = _release_smoke()
    for name, job in smoke["jobs"].items():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), (
                f"job {name} step {step.get('name')!r} interpolates an expression into run:"
            )
            if "uses" in step:
                assert "@v" in step["uses"] and not step["uses"].endswith("@main")
    assert not re.search(r"\bsecrets\b", json.dumps(smoke))
    assert smoke["permissions"] == {"contents": "read"}


def test_ciao_release_skill_describes_the_linux_smoke_and_the_2_0_0_legacy_job() -> None:
    skill = (Path(__file__).parents[1] / "skills" / "ciao-release" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "`release-smoke` (Linux" in skill
    assert "`legacy-update`" in skill
    assert "`release-smoke` (macos)" not in skill
    for gone in ("LaunchAgent", "--load-launchd", "install receipt"):
        assert gone not in skill, gone


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
        "docs/LINUX.md",
        "docs/DEVELOPMENT.md",
        "skills/ciao-release/SKILL.md",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert one_liner in text, f"{relative} no longer documents the install command"
    # The release skill names install-engine.sh only to warn against it, so it
    # is exempt from the negative check; every other first-party page stays clean.
    for relative in ("README.md", "site/index.html", "site/guide.html", "docs/LINUX.md"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "install-engine.sh" not in text, (
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
        "linux-server", "linux-web", "linux-install-smoke", "linux-e2e", "mac-app"
    ]
    assert main["required_status_checks"]["contexts"] == [
        "linux-server", "linux-web", "linux-install-smoke",
        "linux-server-latest-python", "test", "mac-app",
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


def test_ci_runs_the_linux_install_smoke_in_a_clean_container() -> None:
    # #3131 L3: `scripts/install.sh` is the Linux release install path, so
    # every PR installs this checkout's wheel with it inside a plain
    # ubuntu:24.04 container, and the planned service unit is re-checked with
    # the runner's systemd. A guard that disappears here must fail the test,
    # not surface later as the first failing release install.
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    jobs = re.split(r"(?m)^  (?=[a-z0-9-]+:\n)", workflow.split("\njobs:\n", 1)[1])
    by_name = {job.split(":", 1)[0]: job for job in jobs if job.strip()}

    smoke = by_name["linux-install-smoke"]
    assert "runs-on: ubuntu-24.04" in smoke
    # The PWA rides inside the wheel (npm run build writes ciao/web/static),
    # so it is built before the wheel, and the container runs only once the
    # wheel exists.
    assert smoke.index("npm ci") < smoke.index("npm run build")
    assert smoke.index("npm run build") < smoke.index(
        "uv build --wheel --out-dir dist"
    )
    assert smoke.index("uv build --wheel --out-dir dist") < smoke.index("docker run")
    assert "ubuntu:24.04 sh -es" in smoke

    install = next(line for line in smoke.splitlines() if "install.sh --wheel" in line)
    assert "/src/dist/ciaobot-*.whl" in install
    assert "--no-service" in install
    assert "su smoke -c" in install

    # The install counts only when the tool answers with this checkout's own
    # version, read from pyproject, and the service path is exercised as a
    # bounded dry run.
    assert '"$HOME/.local/bin/ciao" --version' in smoke
    assert r's/^version = "\([^"]*\)"/\1/p' in smoke
    assert '"ciao $version"' in smoke
    assert "service install --dry-run" in smoke

    # The host strips the dry run's notes and re-checks the unit with
    # systemd-analyze, uncommented.
    verify = next(line for line in smoke.splitlines() if "systemd-analyze verify" in line)
    assert not verify.lstrip().startswith("#"), "the verify call is commented out"
    assert smoke.index("service install --dry-run") < smoke.index("systemd-analyze verify")

    # The job sits with the other Linux gates, immediately after linux-web.
    assert (
        workflow.index("  linux-web:")
        < workflow.index("  linux-install-smoke:")
        < workflow.index("  linux-e2e:")
    )


































def test_docs_drop_the_pinned_install_sh_digest() -> None:
    # #2483: the docs no longer tell a client to pin one release's digest, and
    # the stale "after 1.7.0" wording is gone from both files. The native
    # installer's manifest-authentication prose went with the deleted installer,
    # so the docs only name the manifest verifier that still exists.
    root = Path(__file__).parents[1]
    development = (root / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    architecture = (root / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")

    assert "next engine release after 1.7.0" not in development
    assert "next engine release after 1.7.0" not in architecture
    assert "must pin the sha256" not in development
    assert "pin the `install.sh` digest" not in development
    assert "python -m ciao.release_manifest verify" in development


def test_publish_verifies_the_attached_files_after_the_manifest_upload() -> None:
    # The 1.x manifest is verified against the bytes the release now serves, not
    # the local copies: upload, re-download, feed each wheel in as --asset, verify.
    steps = _publish_steps()
    names = [step.get("name") for step in steps]
    assert names.index("Attach release assets") < names.index("Attach the 1.x engine manifest")
    assert names.index("Attach the 1.x engine manifest") < names.index("Check the published assets")

    step = _step(steps, "Attach the 1.x engine manifest")["run"]
    upload = step.index("gh release upload")
    download = step.index("gh release download")
    loop = step.index('for wheel in "$uploaded"/ciaobot-*.whl; do')
    feed = step.index('asset_args+=(--asset "$wheel")')
    verify = step.index("-m ciao.release_manifest verify")
    assert upload < download < loop < feed < verify
    assert '"${asset_args[@]}"' in step[verify:]


def test_publish_check_step_pins_the_installer_and_compares_the_bytes_last() -> None:
    # Retired installers and the server host archive are refused by the exact
    # asset-set diff, so no separate name list is kept here.
    check = _step(_publish_steps(), "Check the published assets")["run"]

    assert "if ! diff \"$expected\" \"$listed\"; then" in check
    assert 'grep -qx "CIAOBOT' not in check
    assert "|| true" not in check
    digest_pin = check.index('grep -Fx "WHEEL_SHA256=$digest"')
    installer_cmp = check.index('cmp "$downloaded/install.sh" "$GITHUB_WORKSPACE/install.sh"')
    wheel_cmp = check.index('cmp "${wheels[0]}"')
    assert digest_pin < installer_cmp < wheel_cmp
    assert check.count("cmp ") == 2


def test_publish_checkout_keeps_no_git_credentials_for_later_steps() -> None:
    # A checkout leaves a token in .git/config unless told not to. Nothing in the
    # publish job pushes with git, so the token is never persisted.
    steps = _publish_steps()
    checkouts = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]

    assert checkouts, "publish.yml has no checkout step to pin"
    for step in checkouts:
        assert step.get("with", {}).get("persist-credentials") is False, step
    for step in steps:
        assert not re.search(r"\bgit (push|fetch|pull|commit|tag|remote)\b", step.get("run", "")), step.get(
            "name"
        )


# A stand-in gh and shasum for the published-assets step. gh answers `release view`
# from a listing file and `release download` by copying a release directory;
# shasum is a hashlib wrapper so the step behaves the same on any runner.
_FAKE_GH = """#!__PYTHON__
import os, shutil, sys
args = sys.argv[1:]
if args[:2] == ["release", "view"]:
    with open(os.environ["FAKE_LISTING"], encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
    sys.exit(0)
if args[:2] == ["release", "download"]:
    target = args[args.index("--dir") + 1]
    for name in os.listdir(os.environ["FAKE_RELEASE"]):
        shutil.copy(os.path.join(os.environ["FAKE_RELEASE"], name), target)
    sys.exit(0)
if args[:2] == ["release", "upload"]:
    # The release now serves the uploaded bytes, as the real one does.
    for name in args[3:]:
        if not name.startswith("--"):
            shutil.copy(name, os.environ["FAKE_RELEASE"])
    # A test can make the release serve other bytes than were uploaded.
    override = os.environ.get("FAKE_SERVE_OVERRIDE")
    if override:
        for name in os.listdir(override):
            shutil.copy(os.path.join(override, name), os.environ["FAKE_RELEASE"])
    sys.exit(0)
sys.stderr.write(f"unexpected gh call: {args}\\n")
sys.exit(1)
"""

_FAKE_SHASUM = """#!__PYTHON__
import hashlib, sys
for name in [a for a in sys.argv[1:] if a not in ("-a", "256")]:
    with open(name, "rb") as handle:
        print(f"{hashlib.sha256(handle.read()).hexdigest()}  {name}")
"""

_CHECK_VERSION = "9.8.7"
_CHECK_WHEEL = f"ciaobot-{_CHECK_VERSION}-py3-none-any.whl"
_CHECK_MANIFEST = "ciaobot-engine-manifest.json"
_CHECK_SIGNATURE = _CHECK_MANIFEST + ".sig"


def _check_release() -> dict[str, bytes]:
    import hashlib

    wheel = b"the wheel bytes"
    installer = (
        f"CIAOBOT_VERSION={_CHECK_VERSION}\n"
        f"WHEEL_SHA256={hashlib.sha256(wheel).hexdigest()}\n"
    )
    return {
        "install.sh": installer.encode(),
        _CHECK_WHEEL: wheel,
        _CHECK_MANIFEST: b"manifest",
        _CHECK_SIGNATURE: b"signature",
    }


def _fake_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", _FAKE_GH), ("shasum", _FAKE_SHASUM)):
        exe = bin_dir / name
        exe.write_text(body.replace("__PYTHON__", sys.executable), encoding="utf-8")
        exe.chmod(0o755)
    return bin_dir


def _run_check(
    tmp_path: Path,
    *,
    legacy: str,
    listed: list[str],
    released: dict[str, bytes],
    workspace_wheel: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = _fake_tools(tmp_path)
    listing = tmp_path / "listing.txt"
    listing.write_text("".join(f"{name}\n" for name in listed), encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    for name, body in released.items():
        (release / name).write_bytes(body)
    workspace = tmp_path / "workspace"
    (workspace / "dist").mkdir(parents=True)
    # The wheel this job built. A different one here is the L3 case below.
    built = released[_CHECK_WHEEL] if workspace_wheel is None else workspace_wheel
    (workspace / "dist" / _CHECK_WHEEL).write_bytes(built)
    # The installer this job stamped. Tests alter only the released copy.
    (workspace / "install.sh").write_bytes(_check_release()["install.sh"])
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()

    step = _step(_publish_steps(), "Check the published assets")
    return _run_bash_step(
        step["run"],
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_WORKSPACE": str(workspace),
            "TAG": f"v{_CHECK_VERSION}",
            "VERSION": f"v{_CHECK_VERSION}",
            "LEGACY_PUBLISH": legacy,
            "GH_TOKEN": "test-token",
            "FAKE_LISTING": str(listing),
            "FAKE_RELEASE": str(release),
        },
    )


def test_published_assets_check_accepts_the_two_asset_release(tmp_path: Path) -> None:
    result = _run_check(
        tmp_path,
        legacy="false",
        listed=["install.sh", _CHECK_WHEEL],
        released=_check_release(),
    )

    assert result.returncode == 0, result.stderr


def test_published_assets_check_accepts_the_four_asset_legacy_release(tmp_path: Path) -> None:
    result = _run_check(
        tmp_path,
        legacy="true",
        listed=["install.sh", _CHECK_WHEEL, _CHECK_MANIFEST, _CHECK_SIGNATURE],
        released=_check_release(),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("legacy", "listed", "reason"),
    [
        ("false", ["install.sh", _CHECK_WHEEL, _CHECK_MANIFEST], "manifest on a non-legacy release"),
        ("true", ["install.sh", _CHECK_WHEEL, _CHECK_MANIFEST], "legacy release missing the signature"),
        ("false", ["install.sh", _CHECK_WHEEL, "install-engine.sh"], "retired installer attached"),
        (
            "false",
            ["install.sh", _CHECK_WHEEL, "ciaobot-server-host-macos-universal-v1.tar.gz"],
            "server host archive attached",
        ),
        ("false", ["install.sh"], "wheel missing"),
        ("", ["install.sh", _CHECK_WHEEL], "unknown legacy decision"),
    ],
)
def test_published_assets_check_refuses_any_other_asset_set(
    tmp_path: Path, legacy: str, listed: list[str], reason: str
) -> None:
    result = _run_check(tmp_path, legacy=legacy, listed=listed, released=_check_release())

    assert result.returncode != 0, reason


def test_published_assets_check_refuses_a_listing_the_download_does_not_match(tmp_path: Path) -> None:
    # The listing is the release's own answer; a wheel that appears only in the
    # download is still a second wheel, and the single-wheel guard names it.
    released = _check_release()
    released["ciaobot-0.0.1-py3-none-any.whl"] = b"another wheel"

    result = _run_check(
        tmp_path, legacy="false", listed=["install.sh", _CHECK_WHEEL], released=released
    )

    assert result.returncode != 0
    assert "expected exactly one wheel attached, found 2" in result.stderr


def test_published_assets_check_refuses_an_installer_pinning_another_digest(tmp_path: Path) -> None:
    released = _check_release()
    released["install.sh"] = f"CIAOBOT_VERSION={_CHECK_VERSION}\nWHEEL_SHA256={'0' * 64}\n".encode()

    result = _run_check(
        tmp_path, legacy="false", listed=["install.sh", _CHECK_WHEEL], released=released
    )

    assert result.returncode != 0
    assert "install.sh does not pin the attached wheel's sha256" in result.stderr


def test_published_assets_check_refuses_an_installer_pinning_another_version(tmp_path: Path) -> None:
    released = _check_release()
    released["install.sh"] = released["install.sh"].replace(
        _CHECK_VERSION.encode(), b"9.8.6"
    )

    result = _run_check(
        tmp_path, legacy="false", listed=["install.sh", _CHECK_WHEEL], released=released
    )

    assert result.returncode != 0
    assert f"install.sh does not pin version {_CHECK_VERSION}" in result.stderr


def test_published_assets_check_pins_the_version_as_literal_text(tmp_path: Path) -> None:
    # A regex would read the dots in 9.8.7 as any character and accept 9x8x7.
    released = _check_release()
    released["install.sh"] = released["install.sh"].replace(
        _CHECK_VERSION.encode(), b"9x8x7"
    )

    result = _run_check(
        tmp_path, legacy="false", listed=["install.sh", _CHECK_WHEEL], released=released
    )

    assert result.returncode != 0
    assert "does not pin version" in result.stderr


def test_published_assets_check_refuses_an_installer_that_is_not_the_stamped_one(
    tmp_path: Path,
) -> None:
    # Both pins still match, so only the byte comparison with the stamped
    # installer can refuse a published copy that carries extra content.
    released = _check_release()
    released["install.sh"] += b"# swapped after stamping\n"

    result = _run_check(
        tmp_path, legacy="false", listed=["install.sh", _CHECK_WHEEL], released=released
    )

    assert result.returncode != 0
    assert "is not the stamped installer built for" in result.stderr


def test_published_assets_check_refuses_a_wheel_the_job_did_not_build(tmp_path: Path) -> None:
    # The published wheel matches the installer's pin, so the only thing that
    # can refuse it is the comparison with the wheel this job built.
    result = _run_check(
        tmp_path,
        legacy="false",
        listed=["install.sh", _CHECK_WHEEL],
        released=_check_release(),
        workspace_wheel=b"a different wheel",
    )

    assert result.returncode != 0
    assert "is not the wheel built for" in result.stderr


def test_legacy_gate_is_named_only_in_publish_yml_in_the_scripts_readme() -> None:
    readme = (Path(__file__).parents[1] / "scripts" / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if "legacy-update-gate.py" in line)

    assert "run by `publish.yml` before upload" in row
    assert "release-smoke.yml" not in row


def test_development_doc_warns_that_patch_releases_before_2_0_0_strand_1x() -> None:
    text = (Path(__file__).parents[1] / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")

    assert "Cutting a patch release before 2.0.0 strands 1.x engines" in text
    assert "`release-smoke.yml` installs the published Linux `install.sh`" in text
    assert "until L6" not in text


# The 1.x manifest step signs nothing itself, but its verify runs with the
# release key, which no test holds. The stand-in engine-check python verifies
# against a throwaway key handed in through the environment instead.
_KEYED_VERIFY = """import os, sys
import ciao.release_manifest as release_manifest
release_manifest.RELEASE_PUBLIC_KEY = os.environ["TEST_RELEASE_PUBLIC_KEY"]
args = sys.argv[1:]
if args[:2] == ["-m", "ciao.release_manifest"]:
    args = args[2:]
raise SystemExit(release_manifest.main(args))
"""


def _release_manifest_for(wheel: bytes) -> bytes:
    import hashlib

    artifact = {
        "filename": _CHECK_WHEEL,
        "kind": "wheel",
        "platform": "any",
        "arch": "any",
        "sha256": hashlib.sha256(wheel).hexdigest(),
        "size": len(wheel),
    }
    manifest = build_manifest(_CHECK_VERSION, [artifact], created="2026-11-19T00:00:00+00:00")
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def _run_attach_legacy(
    tmp_path: Path,
    *,
    built: bytes,
    signed: bytes,
    served: bytes,
    served_manifest_for: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real "Attach the 1.x engine manifest" step against a fake release.

    ``built`` is the wheel this job built; the manifest is signed over ``signed``
    and written to the workspace; ``served`` is the wheel the release holds
    under the wheel's name before the step runs. ``served_manifest_for``, when
    given, is a validly signed manifest for another wheel that the release
    serves in place of the uploaded one.
    """
    priv, public_key, key_id = _keypair()
    manifest = _release_manifest_for(signed)
    signature = _sign(manifest, priv, key_id)
    override = tmp_path / "override"
    override.mkdir()
    if served_manifest_for is not None:
        served_manifest = _release_manifest_for(served_manifest_for)
        (override / _CHECK_MANIFEST).write_bytes(served_manifest)
        (override / _CHECK_SIGNATURE).write_text(_sign(served_manifest, priv, key_id), encoding="utf-8")

    bin_dir = _fake_tools(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "dist").mkdir(parents=True)
    (workspace / "dist" / _CHECK_WHEEL).write_bytes(built)
    (workspace / _CHECK_MANIFEST).write_bytes(manifest)
    (workspace / _CHECK_SIGNATURE).write_text(signature, encoding="utf-8")

    release = tmp_path / "release"
    release.mkdir()
    (release / _CHECK_WHEEL).write_bytes(served)

    runner_temp = tmp_path / "runner"
    engine_bin = runner_temp / "engine-check" / "bin"
    engine_bin.mkdir(parents=True)
    keyed = tmp_path / "keyed_verify.py"
    keyed.write_text(_KEYED_VERIFY, encoding="utf-8")
    python = engine_bin / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{keyed}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)

    step = _step(_publish_steps(), "Attach the 1.x engine manifest")
    return _run_bash_step(
        step["run"],
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_WORKSPACE": str(workspace),
            "TAG": f"v{_CHECK_VERSION}",
            "GH_TOKEN": "test-token",
            "FAKE_RELEASE": str(release),
            "FAKE_SERVE_OVERRIDE": str(override),
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "TEST_RELEASE_PUBLIC_KEY": public_key,
        },
        cwd=workspace,
    )


def test_attached_1x_manifest_verifies_the_wheel_the_release_serves(tmp_path: Path) -> None:
    wheel = b"the wheel bytes"

    result = _run_attach_legacy(tmp_path, built=wheel, signed=wheel, served=wheel)

    assert result.returncode == 0, result.stderr


def test_attached_1x_manifest_refuses_a_tampered_attached_wheel(tmp_path: Path) -> None:
    # The release serves different bytes under the wheel's name. The workspace
    # copy is still good, so only a verify against the downloaded wheel fails.
    result = _run_attach_legacy(
        tmp_path, built=b"the wheel bytes", signed=b"the wheel bytes", served=b"tampered bytes"
    )

    assert result.returncode != 0
    assert "does not match the manifest" in result.stderr


def test_attached_1x_manifest_is_verified_as_the_release_serves_it(tmp_path: Path) -> None:
    # The workspace manifest is good; the release serves a validly signed manifest
    # for another wheel after the upload. Only the re-downloaded copy can refuse it.
    result = _run_attach_legacy(
        tmp_path,
        built=b"the wheel bytes",
        signed=b"the wheel bytes",
        served=b"the wheel bytes",
        served_manifest_for=b"another wheel",
    )

    assert result.returncode != 0
    assert "does not match the manifest" in result.stderr


def test_attached_1x_manifest_refuses_a_manifest_for_another_wheel(tmp_path: Path) -> None:
    result = _run_attach_legacy(
        tmp_path, built=b"the wheel bytes", signed=b"another wheel", served=b"the wheel bytes"
    )

    assert result.returncode != 0
    assert "does not match the manifest" in result.stderr


# #3131 L7: release-rehearsal.yml builds the release artifact set and proves it
# without a release. It is security-sensitive: it holds the manifest signing
# secret, so its tests pin where the secret may appear and what the run may do.


def _rehearsal_text() -> str:
    return (
        Path(__file__).parents[1] / ".github" / "workflows" / "release-rehearsal.yml"
    ).read_text(encoding="utf-8")


def _rehearsal() -> dict:
    import yaml

    return yaml.safe_load(_rehearsal_text())


def _rehearsal_steps() -> list[tuple[str, dict]]:
    return [
        (job_name, step)
        for job_name, job in _rehearsal()["jobs"].items()
        for step in job["steps"]
    ]


def _rehearsal_run_text() -> str:
    return "\n".join(step.get("run", "") for _, step in _rehearsal_steps())


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` key as True


_REF_GUARD = "Refuse refs other than develop and main"


def test_release_rehearsal_never_uploads_to_a_release() -> None:
    # The rehearsal's outputs are workflow artifacts. A release is never created,
    # edited, or uploaded to, and no job asks for write access to the repository.
    rehearsal = _rehearsal()
    text = _rehearsal_text()
    assert rehearsal["permissions"] == {"contents": "read"}
    for name, job in rehearsal["jobs"].items():
        assert "permissions" not in job, f"job {name} overrides the read-only token"
    for banned in ("gh release", "gh api", "contents: write", "action-gh-release"):
        assert banned not in text, f"release-rehearsal.yml uses {banned!r}"
    # The run blocks never write back. Comments may name these commands (the
    # dispatch line does), so only the text of run: blocks is checked.
    runs = _rehearsal_run_text()
    for banned in ("git push", "git tag", "gh workflow", "-X POST"):
        assert banned not in runs, f"a run block uses {banned!r}"


def test_release_rehearsal_is_dispatch_only_without_inputs() -> None:
    # Only a hand dispatch starts the run: no push, pull_request or schedule
    # trigger, and no dispatch inputs that could widen what gets rehearsed.
    # Mutation: adding a trigger or an input fails here.
    triggers = _triggers(_rehearsal())
    assert set(triggers) == {"workflow_dispatch"}
    assert not (triggers["workflow_dispatch"] or {}).get("inputs")


def test_release_rehearsal_binds_signing_secret_to_one_step() -> None:
    # Only the manifest signing step may read the secret, and it reads both of the
    # two values the signer takes. \bsecrets\b matches every form a reference can
    # take (secrets.X, toJSON(secrets), secrets: inherit), so a reference in a job
    # or workflow env, or in another step, fails here.
    rehearsal = _rehearsal()
    holders = [
        (job, step)
        for job, step in _rehearsal_steps()
        if re.search(r"\bsecrets\b", json.dumps(step))
    ]
    assert [(job, step.get("name")) for job, step in holders] == [
        ("engine", "Sign engine manifest")
    ]
    assert holders[0][1]["env"] == {
        "TAURI_SIGNING_PRIVATE_KEY": "${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": "${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}",
    }
    outside_jobs = {key: value for key, value in rehearsal.items() if key != "jobs"}
    assert not re.search(r"\bsecrets\b", json.dumps(outside_jobs))
    for job in rehearsal["jobs"].values():
        job_level = {key: value for key, value in job.items() if key != "steps"}
        assert not re.search(r"\bsecrets\b", json.dumps(job_level))
    assert len(re.findall(r"\bsecrets\b", json.dumps(rehearsal))) == 2


def test_release_rehearsal_signing_job_names_the_release_signing_environment() -> None:
    # The job that signs names the Environment that is meant to hold the signing
    # secrets, and the file says what the maintainer must do to make it hold them.
    # Mutation: dropping the environment key, or naming another one, fails here.
    rehearsal = _rehearsal()
    signing_jobs = {
        job for job, step in _rehearsal_steps() if step.get("name") == "Sign engine manifest"
    }
    assert signing_jobs == {"engine"}
    assert rehearsal["jobs"]["engine"]["environment"] == "release-signing"
    text = _rehearsal_text()
    assert "move both secrets into this Environment" in text
    assert "deployment branches to develop and main" in text


def test_release_rehearsal_produces_wheel_installer_and_adhoc_app_archive() -> None:
    # The engine job makes the wheel, the stamped installer, the manifest and its
    # signature, and uploads them as one artifact. The mac-adhoc job packages the
    # ad-hoc app and uploads its archive. Mutation: dropping the stamped installer,
    # the signed manifest, or the archive from its upload breaks the set.
    rehearsal = _rehearsal()
    engine = rehearsal["jobs"]["engine"]
    engine_runs = "\n".join(step.get("run", "") for step in engine["steps"])
    assert "-m ciao.release_manifest stamp-installer" in engine_runs
    assert "--template \"$ws/scripts/install.sh\"" in engine_runs
    assert "--out \"$ws/rehearsal/install.sh\"" in engine_runs
    assert "-m ciao.release_manifest build" in engine_runs
    assert "npx --yes @tauri-apps/cli@2.11.4 signer sign ciaobot-engine-manifest.json" in engine_runs
    assert "-m ciao.release_manifest verify" in engine_runs
    assert "--asset \"$ws/ciaobot-$VERSION-py3-none-any.whl\"" in engine_runs
    assert "uv build --wheel --out-dir dist" in engine_runs

    # The artifact name carries the run attempt, so a re-run never mixes its files
    # with an earlier attempt's, and the download asks for the same name.
    artifact = "rehearsal-${{ github.sha }}-${{ github.run_attempt }}"
    upload = _smoke_step(engine, "Upload the rehearsal artifact set")
    assert upload["uses"].startswith("actions/upload-artifact@v")
    assert upload["with"]["name"] == artifact
    assert upload["with"]["path"] == "rehearsal/"
    assert upload["with"]["if-no-files-found"] == "error"

    assert rehearsal["jobs"]["linux"]["needs"] == "engine"
    download = _smoke_step(rehearsal["jobs"]["linux"], "Download the rehearsal artifact set")
    assert download["uses"].startswith("actions/download-artifact@v")
    assert download["with"]["name"] == artifact

    assert "dmg" not in rehearsal["jobs"]
    mac = rehearsal["jobs"]["mac-adhoc"]
    assert mac["runs-on"] == "macos-15"
    mac_runs = "\n".join(step.get("run", "") for step in mac["steps"])
    assert "scripts/package-app.sh --output" in mac_runs
    # make-dmg.sh refuses ad-hoc builds, so it is deliberately not run here.
    assert "make-dmg.sh" not in mac_runs
    archive = _smoke_step(mac, "Upload the ad-hoc app archive")
    assert archive["uses"].startswith("actions/upload-artifact@v")
    assert archive["with"]["name"] == "rehearsal-adhoc-${{ github.sha }}-${{ github.run_attempt }}"
    for pattern in ("rehearsal-package/Ciaobot-*-adhoc.zip", "rehearsal-package/MANIFEST.txt", "rehearsal-package/SHA256SUMS"):
        assert pattern in archive["with"]["path"]
    # The archive name comes from the app bundle's version, which build-app.sh
    # still hardcodes. The workflow says so instead of asserting the name.
    assert "CFBundleShortVersionString" in _rehearsal_text()
    assert "mac-app-signing-rehearsal.yml" in _rehearsal_text()


def test_release_rehearsal_refuses_refs_other_than_develop_and_main() -> None:
    # The guard is the first step of the engine and mac-adhoc jobs and runs for
    # real. Mutation: widening the case pattern, dropping the branch check, or
    # moving the guard below another step fails here.
    if sys.platform == "win32":
        pytest.skip("runs the workflow's bash")
    rehearsal = _rehearsal()
    for job_name in ("engine", "mac-adhoc"):
        first = rehearsal["jobs"][job_name]["steps"][0]
        assert first.get("name") == _REF_GUARD, f"job {job_name} does not refuse refs first"
    guards = [step for _, step in _rehearsal_steps() if step.get("name") == _REF_GUARD]
    assert len(guards) == 2
    for guard in guards:
        assert guard["env"] == {
            "REF_NAME": "${{ github.ref_name }}",
            "REF_TYPE": "${{ github.ref_type }}",
        }
        for name, ref_type, expected in (
            ("develop", "branch", 0),
            ("main", "branch", 0),
            ("raffaelefarinaro/feature", "branch", 1),
            ("release/v2.0.0", "branch", 1),
            ("develop-evil", "branch", 1),
            ("main2", "branch", 1),
            ("refs/heads/develop", "branch", 1),
            ("Develop", "branch", 1),
            ("release", "branch", 1),
            ("develop", "tag", 1),
            ("main", "tag", 1),
            ("", "branch", 1),
        ):
            result = _run_bash_step(
                guard["run"], {"REF_NAME": name, "REF_TYPE": ref_type}
            )
            assert result.returncode == expected, (name, ref_type, result.stderr)


def test_release_rehearsal_keeps_expressions_out_of_run_blocks_and_pins_actions() -> None:
    # No expression is interpolated into a shell script (values come in through
    # env:), every run block fails closed except Diagnostics, and every action
    # is pinned to a version, not a branch.
    rehearsal = _rehearsal()
    for job_name, step in _rehearsal_steps():
        label = f"job {job_name} step {step.get('name')!r}"
        run = step.get("run")
        if run is not None:
            assert "${{" not in run, f"{label} interpolates an expression into run:"
            if step.get("name") != "Diagnostics":
                assert run.lstrip().startswith("set -euo pipefail"), f"{label} is not fail-closed"
        if "uses" in step:
            assert "@v" in step["uses"] and not step["uses"].endswith("@main"), label
    for job_name, job in rehearsal["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {job_name} has no timeout"


def test_release_rehearsal_linux_asserts_are_the_release_smoke_asserts() -> None:
    # The Linux job copies the L6 service asserts and diagnostics verbatim. Only
    # the install and the re-run download differ: they install the rehearsal
    # wheel from the artifact instead of the public release.
    smoke = _release_smoke()["jobs"]["linux-install"]
    linux = _rehearsal()["jobs"]["linux"]
    for name in ("Check the service answers", "Diagnostics"):
        assert _smoke_step(linux, name)["run"] == _smoke_step(smoke, name)["run"], name
    assert _smoke_step(linux, "Diagnostics")["if"] == "failure()"

    smoke_rerun = _smoke_step(smoke, "Re-run the one-liner (same-version reinstall)")["run"]
    rehearsal_rerun = _smoke_step(linux, "Re-run the installer (same-version reinstall)")["run"]
    tail = smoke_rerun[smoke_rerun.index("port=$(awk") :]
    assert rehearsal_rerun.endswith(tail)

    linux_text = "\n".join(step.get("run", "") for step in linux["steps"])
    assert "releases/download" not in linux_text
    assert "curl -fsSL" not in linux_text
    assert "setup-token" not in linux_text
    assert "find " not in linux_text
    assert linux["runs-on"] == "ubuntu-24.04"


def test_release_rehearsal_waits_are_bounded() -> None:
    # Every job and every step that waits, installs or runs the gate is bounded,
    # and no loop is unbounded. Mutation: removing a timeout or an iteration cap
    # fails here.
    rehearsal = _rehearsal()
    for job_name, job in rehearsal["jobs"].items():
        for step in job["steps"]:
            run = step.get("run", "")
            if "sleep" in run or "install.sh" in run or "legacy-update-gate.py" in run:
                if job_name == "linux" or "legacy-update-gate.py" in run:
                    assert isinstance(step.get("timeout-minutes"), int), (
                        f"job {job_name} step {step.get('name')!r} waits without a timeout"
                    )
            assert "while " not in run and "until " not in run, (
                f"job {job_name} step {step.get('name')!r} has an unbounded loop"
            )
    text = _rehearsal_text()
    # Every wait loop has the same iteration cap. Mutation: one loop at 91 fails here.
    assert set(re.findall(r"seq 1 (\d+)", text)) == {"90"}
    assert text.count("seq 1 90") == 4
    assert text.count("curl -fsS --max-time 5") >= 4


def test_release_rehearsal_legacy_literal_matches_publish_and_smoke() -> None:
    # One literal decides the 1.x gate in all three release workflows. Mutation:
    # changing it in one file alone fails here.
    literal = _rehearsal()["env"]["LEGACY_MANIFEST_VERSION"]
    assert literal == "2.0.0"
    assert literal == _publish_workflow()["env"]["LEGACY_MANIFEST_VERSION"]
    assert literal == _release_smoke()["env"]["LEGACY_MANIFEST_VERSION"]


@pytest.mark.skipif(sys.platform == "win32", reason="runs the workflow's bash")
@pytest.mark.parametrize(
    ("version", "legacy"),
    [("1.10.0", "false"), ("2.0.0", "true"), ("2.0.1", "false")],
)
def test_release_rehearsal_decision_step_outputs(
    tmp_path: Path, version: str, legacy: str
) -> None:
    # Runs the real decision step. A swapped true/false echo, a dropped notice, or
    # a loose comparison fails here.
    rehearsal = _rehearsal()
    step = _smoke_step(rehearsal["jobs"]["engine"], "Decide whether the 1.x update gate runs")
    output = tmp_path / "github-output"
    output.touch()

    result = _run_bash_step(
        step["run"],
        {
            "VERSION": version,
            "GITHUB_OUTPUT": str(output),
            "LEGACY_MANIFEST_VERSION": rehearsal["env"]["LEGACY_MANIFEST_VERSION"],
        },
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"legacy={legacy}\n"
    if legacy == "false":
        assert f"::notice::1.x update gate skipped for {version}" in result.stdout
    else:
        assert "::notice::" not in result.stdout


def test_release_rehearsal_legacy_gate_runs_only_for_2_0_0_and_reads_staged_files() -> None:
    # The 1.x gate runs only when the decision step says so, and reads the
    # rehearsal's files through --release-root (a file:// URL), never from a
    # published release. Signing and verification run for every version.
    # Mutation: dropping the if, or swapping the outputs, fails here.
    engine = _rehearsal()["jobs"]["engine"]
    names = [step.get("name") for step in engine["steps"]]
    decision = _smoke_step(engine, "Decide whether the 1.x update gate runs")
    gate = _smoke_step(engine, "Run the 1.x update gate against the rehearsal files")
    assert decision["id"] == "legacy"
    assert names.index("Read the release version from pyproject.toml") < names.index(
        "Decide whether the 1.x update gate runs"
    ) < names.index("Run the 1.x update gate against the rehearsal files")
    assert gate["if"] == "steps.legacy.outputs.legacy == 'true'"
    for name in ("Sign engine manifest", "Verify the signed manifest and its wheel"):
        assert "if" not in _smoke_step(engine, name), name
    assert "python3 scripts/legacy-update-gate.py" in gate["run"]
    assert "--release-root \"$RUNNER_TEMP/legacy\"" in gate["run"]
    assert "--release-base" not in gate["run"]
    assert gate["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_release_rehearsal_is_documented_for_dispatch() -> None:
    development = (Path(__file__).parents[1] / "docs" / "DEVELOPMENT.md").read_text(
        encoding="utf-8"
    )
    assert "gh workflow run release-rehearsal.yml --ref develop" in development
    # GitHub offers workflow_dispatch only for files on the default branch, so the
    # doc names that branch and the acceptance-run ordering.
    assert "default branch, which is `main`" in development
    assert "release-signing" in development
    assert "rehearsal-<sha>-<run_attempt>" in development
    upkeep = (Path(__file__).parents[1] / "docs" / "UPKEEP.md").read_text(encoding="utf-8")
    assert "release-signing" in upkeep
