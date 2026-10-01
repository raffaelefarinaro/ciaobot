from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


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

    assert "{{CIAO_EXECUTABLE}}" in text
    assert "{{CIAO_WORKSPACE}}" in text
    assert "{{CIAO_RUNTIME_ROOT}}" in text
    assert "{{CIAO_PORT}}" in text
    assert "{{CIAO_PATH}}" in text
    assert "{{LAUNCHD_PROGRAM_ARGUMENTS}}" in text
    assert "<string>{{CIAO_EXECUTABLE}}</string>" in text
    assert "<string>ciao.cli</string>" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="renders a launchd plist from POSIX paths")
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
    assert "{{CIAO_RUNTIME_ROOT}}" not in out
    assert "/tmp/ciao-ws/.runtime" in out
    assert "<key>PATH</key>" in out
    assert "/opt/homebrew/bin:/usr/bin:/bin" in out
    assert "<string>-m</string>" in out
    assert "<string>ciao.cli</string>" in out


def test_render_launchd_plist_preserves_custom_runtime_root() -> None:
    from ciao.cli import _render_launchd_plist

    out = _render_launchd_plist(
        workspace=Path("/tmp/ciao-ws"),
        runtime_root=Path("/tmp/ciao-runtime"),
        port=8443,
    )

    assert f"<string>{Path('/tmp/ciao-runtime').resolve()}</string>" in out


def test_render_launchd_plist_uses_bundled_engine_path() -> None:
    from ciao.cli import _render_launchd_plist

    out = _render_launchd_plist(
        workspace=Path("/tmp/ciao-ws"),
        engine_path=(
            "/Users/me/Applications/Ciaobot.app/Contents/Resources/"
            "ciao-runtime/bin/ciao"
        ),
        port=8443,
    )

    assert "Contents/Resources/ciao-runtime/bin/ciao" in out
    assert "ciao.cli" not in out


def test_run_step_reports_missing_binary_as_failed_step() -> None:
    from ciao.subprocess_step import run_step as _run_step

    result = _run_step(["definitely-not-a-real-binary-xyz"], cwd="/tmp", timeout=5)
    assert result.returncode == 127
    assert "not found on PATH" in result.stderr
    assert result.stdout == ""


def test_run_step_passes_through_success(tmp_path) -> None:
    from ciao.subprocess_step import run_step as _run_step

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


# ── Snapshot commit identity (#851) ─────────────────────────────────────
# Settings -> This host -> Restart (from a source checkout) posts
# /api/admin/deploy, whose first step is the workspace snapshot. That step
# used to run a plain ``git commit``, which dies with "Author identity
# unknown" on a machine with no git identity anywhere — a fresh install,
# which is exactly where Restart is most likely to be pressed. The snapshot
# is automated bookkeeping, so it is authored as Ciaobot, like every other
# commit the engine makes, and no config is written to get there.
#
# The fixture pins HOME, the global config and the system config into
# tmp_path, and puts ``user.useConfigOnly = true`` in the fixture's own
# global config. That last part is what keeps the test honest: without it
# git quietly invents an identity from the hostname ("you@example.com") and
# commits fine even with no config at all, hiding the bug.

_IDENTITY_FREE_CONFIG = "[user]\n\tuseConfigOnly = true\n"

# Anything that would hand git an identity has to go, including a
# developer's own shell exports.
_IDENTITY_ENV_VARS = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "EMAIL",
)


def _git_without_identity(home: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run git with HOME, global config and system config confined to ``home``."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        },
        capture_output=True,
        text=True,
    )


async def test_commit_and_push_commits_without_a_git_identity(tmp_path, monkeypatch) -> None:
    """The snapshot commit must not depend on the operator's git config (#851)."""
    from ciao.web import routes_helpers

    home = tmp_path / "home"
    origin = tmp_path / "origin.git"
    repo = tmp_path / "workspace"
    home.mkdir()
    repo.mkdir()
    (home / ".gitconfig").write_text(_IDENTITY_FREE_CONFIG, encoding="utf-8")

    # A bare repo stands in for the remote, so the real ``git push`` inside
    # _commit_and_push has somewhere to go. Everything below runs under the
    # confined env; the seed commit is the one thing that needs an explicit
    # identity, and it gets one on the command line for that reason.
    _git_without_identity(home, "init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git_without_identity(home, "init", "-q", str(repo), cwd=tmp_path)
    _git_without_identity(home, "remote", "add", "origin", str(origin), cwd=repo)
    (repo / "notes.md").write_text("seed\n", encoding="utf-8")
    _git_without_identity(home, "add", "-A", cwd=repo)
    seed = _git_without_identity(
        home,
        "-c", "user.name=Seed", "-c", "user.email=seed@example.invalid",
        "commit", "-m", "seed", cwd=repo,
    )
    assert seed.returncode == 0, seed.stderr
    pushed = _git_without_identity(home, "push", "-q", "-u", "origin", "HEAD", cwd=repo)
    assert pushed.returncode == 0, pushed.stderr

    # Now dirty the tree with no identity reachable from anywhere, including
    # the ambient environment subprocess.run in the code under test inherits.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in _IDENTITY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    (repo / "notes.md").write_text("seed\nsnapshot\n", encoding="utf-8")

    async def fake_pull(workspace, **kwargs):
        return 0, "ok"

    monkeypatch.setattr(routes_helpers, "_git_pull_with_retry", fake_pull)

    ok, details = await routes_helpers._commit_and_push(repo, "pwa snapshot before deploy")
    assert ok is True, details

    author = _git_without_identity(home, "log", "-1", "--format=%an <%ae>", cwd=repo)
    assert author.stdout.strip() == "Ciaobot <ciaobot@localhost>"
    remote_log = _git_without_identity(home, "log", "-1", "--format=%s", cwd=origin)
    assert remote_log.stdout.strip() == "pwa snapshot before deploy"

    # The identity travelled on the commit command only: no config written,
    # in the repo or globally.
    for key in ("user.name", "user.email"):
        found = _git_without_identity(home, "config", "--get", key, cwd=repo)
        assert found.returncode != 0, f"{key} must not be configured"
    assert (home / ".gitconfig").read_text(encoding="utf-8") == _IDENTITY_FREE_CONFIG


async def test_commit_and_push_passes_identity_before_the_commit_subcommand(
    tmp_path, monkeypatch,
) -> None:
    """git only reads ``-c`` options placed ahead of the subcommand, and the
    identity must not leak onto the other calls."""
    from ciao.web import routes_helpers

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        # `diff --quiet --cached` exits non-zero when the tree is dirty, which
        # is what sends the helper on to committing.
        dirty = list(args[-2:]) == ["--quiet", "--cached"]
        return subprocess.CompletedProcess(
            args=args, returncode=1 if dirty else 0, stdout="", stderr="",
        )

    async def fake_pull(workspace, **kwargs):
        return 0, "ok"

    monkeypatch.setattr(routes_helpers.subprocess, "run", fake_run)
    monkeypatch.setattr(routes_helpers, "_git_pull_with_retry", fake_pull)

    ok, details = await routes_helpers._commit_and_push(tmp_path, "pwa snapshot before deploy")
    assert ok is True, details

    assert [
        "git",
        "-c", "user.name=Ciaobot",
        "-c", "user.email=ciaobot@localhost",
        "commit", "-m", "pwa snapshot before deploy",
    ] in calls
    # add/diff/push stay plain — only the commit needs an author.
    assert calls[0] == ["git", "add", "-A"]
    assert calls[-1] == ["git", "push"]
