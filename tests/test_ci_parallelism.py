"""Keep expensive gates parallel without losing stable required check names."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


def _summary_bash() -> str:
    if sys.platform == "win32":
        git = shutil.which("git")
        assert git is not None, "Git for Windows is required to test the CI Bash script"
        # Windows' PATH may put the WSL launcher before Git Bash.
        bash = Path(git).parent.parent / "bin" / "bash.exe"
        assert bash.is_file(), f"Git Bash is missing beside the Git installation: {bash}"
        return str(bash)
    bash = shutil.which("bash")
    assert bash is not None, "Bash is required to test the CI summary script"
    return bash


def test_windows_summary_uses_git_bash_not_wsl_launcher(tmp_path, monkeypatch) -> None:
    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    monkeypatch.setitem(globals(), "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(shutil, "which", lambda name: str(git) if name == "git" else "WSL/bash.exe")
    assert _summary_bash() == str(bash)


def _jobs() -> dict[str, str]:
    workflow = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    return dict(re.findall(r"(?ms)^  ([a-z][a-z0-9-]*):\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)", workflow.split("\njobs:\n")[1]))


def test_windows_suites_run_independently_and_summary_fails_closed() -> None:
    jobs = _jobs()
    backend, frontend, summary = (jobs[name] for name in ("windows-backend", "windows-web", "windows"))
    assert "npm ci && npm test" not in backend
    assert "python -m pytest -n auto" in backend
    assert "npm ci && npm test" in frontend
    assert "needs:" not in backend
    assert "needs:" not in frontend
    assert "needs: [windows-backend, windows-web]" in summary
    assert "if: always()" in summary
    assert 'test "$BACKEND" = success' in summary
    assert 'test "$FRONTEND" = success' in summary
    assert 'test "$FRONTEND" = skipped' in summary
    assert "windows-full" in frontend and "windows-full" in summary
    assert "continue-on-error" not in summary


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


@pytest.mark.parametrize("full,backend,frontend,passes", [
    ("true", "success", "success", True),
    ("true", "success", "skipped", False),
    ("true", "skipped", "success", False),
    ("true", "failure", "success", False),
    ("true", "success", "failure", False),
    ("true", "success", "cancelled", False),
    ("false", "success", "skipped", True),
    ("false", "failure", "skipped", False),
    ("false", "cancelled", "skipped", False),
    ("false", "success", "failure", False),
])
def test_actual_windows_summary_script(full, backend, frontend, passes) -> None:
    script = _jobs()["windows"].split("      run: |\n", 1)[1]
    script = "\n".join(line[8:] for line in script.splitlines() if line.startswith("        "))
    result = subprocess.run(
        [_summary_bash(), "-e", "-c", script],
        env={**os.environ, "FULL": full, "BACKEND": backend, "FRONTEND": frontend},
        capture_output=True,
    )
    assert (result.returncode == 0) is passes, (result.args, result.stdout, result.stderr)
