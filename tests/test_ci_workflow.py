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


def test_retired_app_installer_is_not_in_the_source_tree() -> None:
    # The macOS app installer is retired. scripts/install.sh may exist only as the
    # Linux-only headless installer (#3131 L2), which refuses macOS and root.
    root = Path(__file__).parents[1]
    installer = root / "scripts" / "install.sh"
    if installer.exists():
        text = installer.read_text(encoding="utf-8")
        assert "Linux" in text and "Darwin" in text and "root" in text


def _release_smoke() -> dict:
    import yaml

    return yaml.safe_load(_smoke_text())


def _smoke_text() -> str:
    return (Path(__file__).parents[1] / ".github" / "workflows" / "release-smoke.yml").read_text(
        encoding="utf-8"
    )


def _smoke_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _shell_function(run: str, name: str) -> str:
    # The text of one bash function inside a run: block, from its opening line
    # to the closing brace, so a test can execute the real redaction.
    lines = run.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}() {{")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "\n".join(lines[start : end + 1])


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


def _promote_script() -> Path:
    return Path(__file__).parents[1] / "mac" / "scripts" / "promote-release.sh"


def _run_promote(
    tmp_path: Path, *args: str, state: str, assets: list[str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (tmp_path / "state").write_text(state, encoding="utf-8")
    (tmp_path / "assets").write_text("\n".join(assets) + "\n", encoding="utf-8")
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$FAKE_LOG"\n'
        'case "$*" in\n'
        '  *"--json isDraft,isPrerelease"*) cat "$FAKE_DIR/state" ;;\n'
        '  *"--json assets"*) cat "$FAKE_DIR/assets" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_LOG": str(log),
        "FAKE_DIR": str(tmp_path),
    }
    result = subprocess.run(
        ["/bin/sh", str(_promote_script()), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


_FULL_ASSETS = ["install.sh", "ciaobot-2.0.1-py3-none-any.whl", "Ciaobot-2.0.1.dmg"]


_LEGACY_ASSETS = [
    "install.sh",
    "ciaobot-2.0.0-py3-none-any.whl",
    "Ciaobot-2.0.0.dmg",
    "ciaobot-engine-manifest.json",
    "ciaobot-engine-manifest.json.sig",
]


def test_promote_release_requires_the_1x_manifest_on_the_legacy_release(
    tmp_path: Path,
) -> None:
    # publish.yml attaches the manifest and signature for 2.0.0 only, and the 1.x
    # updater reads them from releases/latest.
    result, calls = _run_promote(tmp_path, "--tag", "v2.0.0", state="false true", assets=_LEGACY_ASSETS)
    assert result.returncode == 0, result.stderr
    assert [c for c in calls if c.startswith("release edit")] == [
        "release edit v2.0.0 --prerelease=false --latest"
    ]


@pytest.mark.parametrize(
    "missing", ["ciaobot-engine-manifest.json", "ciaobot-engine-manifest.json.sig"]
)
def test_promote_release_refuses_the_legacy_release_without_manifest_or_signature(
    tmp_path: Path, missing: str
) -> None:
    assets = [name for name in _LEGACY_ASSETS if name != missing]
    result, calls = _run_promote(tmp_path, "--tag", "v2.0.0", state="false true", assets=assets)
    assert result.returncode == 1
    assert missing in result.stderr
    assert not [c for c in calls if c.startswith("release edit")]


def test_promote_release_does_not_demand_the_manifest_on_later_releases(
    tmp_path: Path,
) -> None:
    result, calls = _run_promote(tmp_path, "--tag", "v2.0.1", state="false true", assets=_FULL_ASSETS)
    assert result.returncode == 0, result.stderr


def test_promote_release_marks_a_complete_prerelease_latest(tmp_path: Path) -> None:
    result, calls = _run_promote(
        tmp_path, "--tag", "v2.0.1", state="false true", assets=_FULL_ASSETS
    )
    assert result.returncode == 0, result.stderr
    edits = [call for call in calls if call.startswith("release edit")]
    assert edits == ["release edit v2.0.1 --prerelease=false --latest"]


def test_promote_release_targets_the_given_repo(tmp_path: Path) -> None:
    result, calls = _run_promote(
        tmp_path, "--tag", "v2.0.1", "--repo", "o/r", state="false true", assets=_FULL_ASSETS
    )
    assert result.returncode == 0, result.stderr
    assert all("--repo o/r" in call for call in calls)


def test_promote_release_refuses_a_release_without_the_dmg(tmp_path: Path) -> None:
    result, calls = _run_promote(
        tmp_path, "--tag", "v2.0.1", state="false true", assets=_FULL_ASSETS[:2]
    )
    assert result.returncode == 1
    assert "Ciaobot-2.0.1.dmg" in result.stderr
    assert not [call for call in calls if call.startswith("release edit")]


@pytest.mark.parametrize("missing", ["install.sh", "ciaobot-2.0.1-py3-none-any.whl"])
def test_promote_release_refuses_a_release_publish_has_not_finished(
    tmp_path: Path, missing: str
) -> None:
    assets = [name for name in _FULL_ASSETS if name != missing]
    result, calls = _run_promote(tmp_path, "--tag", "v2.0.1", state="false true", assets=assets)
    assert result.returncode == 1
    assert missing in result.stderr
    assert not [call for call in calls if call.startswith("release edit")]


def test_promote_release_refuses_drafts_and_already_published(tmp_path: Path) -> None:
    result, calls = _run_promote(
        tmp_path, "--tag", "v2.0.1", state="true true", assets=_FULL_ASSETS
    )
    assert result.returncode == 1 and "draft" in result.stderr
    assert not [call for call in calls if call.startswith("release edit")]

    other = tmp_path / "again"
    other.mkdir()
    result, calls = _run_promote(other, "--tag", "v2.0.1", state="false false", assets=_FULL_ASSETS)
    assert result.returncode == 1 and "not a pre-release" in result.stderr
    assert not [call for call in calls if call.startswith("release edit")]


@pytest.mark.parametrize("tag", ["2.0.0", "v2.0", "v2.0.0-rc1", "updates", "v2.0.0;rm"])
def test_promote_release_accepts_only_version_tags(tmp_path: Path, tag: str) -> None:
    # The `updates` feed release must never be promoted: it stays a pre-release.
    result, calls = _run_promote(tmp_path, "--tag", tag, state="false true", assets=_FULL_ASSETS)
    assert result.returncode != 0
    assert calls == []


def test_promote_release_script_is_executable_and_posix() -> None:
    script = _promote_script()
    assert os.access(script, os.X_OK)
    assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\n")

