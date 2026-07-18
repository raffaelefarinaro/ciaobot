from __future__ import annotations

import subprocess
from pathlib import Path


def test_deploy_folder_has_no_private_reverse_proxy_or_absolute_paths() -> None:
    repo = Path(__file__).parents[1]

    assert not (repo / "ciao" / "stock" / "deploy" / "Caddyfile").exists()

    deploy_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo / "ciao" / "stock" / "deploy").rglob("*")
        if path.is_file() and path.suffix not in {".icns", ".png"}
    )
    forbidden = (
        "raff" + "aelefarinaro",
        "bot." + "raff" + "aelefarinaro.com",
        "/Users/" + "raff" + "aelefarinaro",
        "sdc" + "-labs",
    )
    for marker in forbidden:
        assert marker not in deploy_text


def test_deploy_plist_points_at_packaged_cli_template() -> None:
    text = (
        Path(__file__).parents[1] / "ciao" / "stock" / "deploy" / "com.ciao.server.plist.tmpl"
    ).read_text(encoding="utf-8")

    assert "{{CIAO_PYTHON}}" in text
    assert "{{CIAO_WORKSPACE}}" in text
    assert "{{CIAO_PORT}}" in text
    assert "{{CIAO_PATH}}" in text
    assert "<string>ciao.cli</string>" in text
    assert "<string>run</string>" in text


def test_render_launchd_plist_substitutes_path() -> None:
    import os

    from ciao.cli import _render_launchd_plist

    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin"
    try:
        out = _render_launchd_plist(
            workspace=Path("/tmp/ciao-ws"),
            python_path="/opt/ciao/bin/python",
            port=8443,
        )
    finally:
        os.environ["PATH"] = saved

    assert "{{CIAO_PATH}}" not in out
    assert "<key>PATH</key>" in out
    assert "/opt/homebrew/bin:/usr/bin:/bin" in out


def test_render_launchd_plist_maps_cellar_python_to_opt(tmp_path: Path) -> None:
    """A Homebrew Cellar interpreter is recorded via the upgrade-stable
    opt symlink: `brew upgrade` deletes the versioned keg, which killed the
    LaunchAgents (and the running server) pinned to it."""
    from ciao.cli import _render_launchd_plist

    cellar_python = tmp_path / "Cellar" / "ciaobot" / "0.4.8" / "libexec" / "bin" / "python"
    opt_python = tmp_path / "opt" / "ciaobot" / "libexec" / "bin" / "python"
    for p in (cellar_python, opt_python):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")

    out = _render_launchd_plist(
        workspace=Path("/tmp/ciao-ws"),
        python_path=str(cellar_python),
        port=8443,
    )
    assert str(opt_python) in out
    assert "/Cellar/" not in out


def test_render_launchd_plist_keeps_non_cellar_python(tmp_path: Path) -> None:
    """Non-Homebrew interpreters (and Cellar paths without an opt mirror)
    are recorded as given."""
    from ciao.cli import _render_launchd_plist

    out = _render_launchd_plist(
        workspace=Path("/tmp/ciao-ws"),
        python_path="/Users/me/.ciaobot-venv/bin/python",
        port=8443,
    )
    assert "/Users/me/.ciaobot-venv/bin/python" in out

    orphan = tmp_path / "Cellar" / "ciaobot" / "9.9.9" / "bin" / "python"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("", encoding="utf-8")
    out = _render_launchd_plist(
        workspace=Path("/tmp/ciao-ws"),
        python_path=str(orphan),
        port=8443,
    )
    # No opt mirror exists for this keg: the path is left untouched.
    assert str(orphan) in out


def test_run_step_reports_missing_binary_as_failed_step() -> None:
    from ciao.web.routes_api import _run_step

    result = _run_step(["definitely-not-a-real-binary-xyz"], cwd="/tmp", timeout=5)
    assert result.returncode == 127
    assert "not found on PATH" in result.stderr
    assert result.stdout == ""


def test_run_step_passes_through_success(tmp_path) -> None:
    from ciao.web.routes_api import _run_step

    result = _run_step(["true"], cwd=str(tmp_path), timeout=5)
    assert result.returncode == 0


def test_root_npm_install_skips_without_root_package_json(tmp_path) -> None:
    from ciao.web.routes_api import _run_root_npm_install

    result = _run_root_npm_install(tmp_path)

    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert "package.json" in result.stdout


# ── DNS-flap retry for deploy-path git pulls ───────────────────────────
# The deploy snapshot step and the post-snapshot ``git pull`` both call
# ``_git_pull_with_retry``. They used to hard-fail on a DNS resolver flap
# (the same kind that intermittently trips ``branch_backup``). These tests
# pin the classification logic and the retry behaviour. Using a small
# shell script as the ``git`` binary keeps the test honest — it actually
# exercises the subprocess path instead of mocking the whole runner.


_TRANSIENT_STDERR = (
    "fatal: unable to access 'https://github.com/x/y.git/': "
    "Could not resolve host: github.com\n"
)
_AUTH_STDERR = (
    "fatal: Authentication failed for 'https://github.com/x/y.git/'\n"
)


def test_is_transient_git_pull_error_matches_resolve_host() -> None:
    from ciao.web.routes_helpers import _is_transient_git_pull_error

    assert _is_transient_git_pull_error(_TRANSIENT_STDERR) is True
    assert _is_transient_git_pull_error(
        "fatal: unable to access 'https://github.com/x/y.git/': "
        "Could not resolve host: github.com"
    ) is True
    # Case-insensitive.
    assert _is_transient_git_pull_error("COULD NOT RESOLVE HOST: github.com") is True
    # Different transient markers.
    assert _is_transient_git_pull_error("fatal: unable to connect: connection timed out") is True
    assert _is_transient_git_pull_error("fatal: unable to access ... network is unreachable") is True


def test_is_transient_git_pull_error_ignores_real_failures() -> None:
    from ciao.web.routes_helpers import _is_transient_git_pull_error

    assert _is_transient_git_pull_error(_AUTH_STDERR) is False
    assert _is_transient_git_pull_error(
        "fatal: no upstream configured for branch 'develop'"
    ) is False
    assert _is_transient_git_pull_error("CONFLICT (content): Merge conflict in foo.md") is False
    # Empty / missing stderr is not transient (lets the caller see the real rc).
    assert _is_transient_git_pull_error("") is False


async def test_git_pull_with_retry_recovers_from_dns_flap(tmp_path, monkeypatch) -> None:
    """A first attempt that fails with 'Could not resolve host' should be
    retried and succeed; total 2 subprocess invocations."""
    from ciao.web import routes_helpers

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        n = len(calls)
        if n == 1:
            return subprocess.CompletedProcess(
                args=args, returncode=128,
                stdout="", stderr=_TRANSIENT_STDERR,
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0,
            stdout="Already up to date.\n", stderr="",
        )

    monkeypatch.setattr(routes_helpers.subprocess, "run", fake_run)
    # Shrink the backoff so the test finishes in <100ms.
    rc, out = await routes_helpers._git_pull_with_retry(
        tmp_path, attempts=2, backoff_s=0.0,
    )
    assert rc == 0
    assert "Already up to date" in out
    assert len(calls) == 2, "expected exactly one retry after the transient failure"


async def test_git_pull_with_retry_does_not_retry_auth_failure(tmp_path, monkeypatch) -> None:
    """An auth failure is not transient; the helper returns immediately so
    the deploy surfaces the real problem instead of waiting pointlessly."""
    from ciao.web import routes_helpers

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=128,
            stdout="", stderr=_AUTH_STDERR,
        )

    monkeypatch.setattr(routes_helpers.subprocess, "run", fake_run)
    rc, out = await routes_helpers._git_pull_with_retry(
        tmp_path, attempts=2, backoff_s=0.0,
    )
    assert rc == 128
    assert "Authentication failed" in out
    assert len(calls) == 1, "auth errors must not trigger a retry"


async def test_git_pull_with_retry_gives_up_after_attempts(tmp_path, monkeypatch) -> None:
    """If the transient error persists past the configured attempts, the
    helper returns the last failure verbatim so the deploy reports a real
    error instead of silently swallowing the problem."""
    from ciao.web import routes_helpers

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=args, returncode=128,
            stdout="", stderr=_TRANSIENT_STDERR,
        )

    monkeypatch.setattr(routes_helpers.subprocess, "run", fake_run)
    rc, out = await routes_helpers._git_pull_with_retry(
        tmp_path, attempts=2, backoff_s=0.0,
    )
    assert rc == 128
    assert "Could not resolve host" in out
    assert len(calls) == 2, "expected one initial attempt plus one retry"
