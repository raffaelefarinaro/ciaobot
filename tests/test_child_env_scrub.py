"""The bundled Mac engine's launcher exports two variables that no child should inherit.

``mac/scripts/engine-launcher.sh`` sets ``SSL_CERT_FILE`` (the certifi bundle) and
``PYTHONDONTWRITEBYTECODE=1`` for the engine. Children the engine starts (ACP agents, the
terminal, scheduled commands, provider probes) get a copy without them; the engine's own
``os.environ`` keeps them, so its httpx TLS still trusts certifi. Outside the bundle nothing is
scrubbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ciao import adapter_install, host_terminal, package_version, schedule_command
from ciao.os_support.child_env import BUNDLED_ENGINE_SCRUBBED_KEYS, child_env
from ciao.providers.acp import connection as connection_module

SCRUBBED = ("SSL_CERT_FILE", "PYTHONDONTWRITEBYTECODE")


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(package_version, "detect_install_mode", lambda: mode)


@pytest.fixture
def launcher_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The variables the launcher exports, as the engine's process sees them."""
    monkeypatch.setenv("SSL_CERT_FILE", "/Applications/Ciaobot.app/Contents/lib/certifi/cacert.pem")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("CHILD_ENV_PROBE_KEEP", "keep-sentinel")


def test_scrubbed_keys_are_exactly_the_launcher_exports() -> None:
    assert set(BUNDLED_ENGINE_SCRUBBED_KEYS) == set(SCRUBBED)


def test_bundled_engine_child_env_drops_both_and_keeps_the_engine_environ(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "bundled_app")
    before = dict(os.environ)

    env = child_env()

    assert not (set(SCRUBBED) & set(env))
    assert env["CHILD_ENV_PROBE_KEEP"] == "keep-sentinel"
    assert dict(os.environ) == before
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_child_env_takes_an_explicit_source_without_touching_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _mode(monkeypatch, "bundled_app")
    source = {"SSL_CERT_FILE": "/x.pem", "PYTHONDONTWRITEBYTECODE": "1", "KEEP": "1"}

    assert child_env(source) == {"KEEP": "1"}
    assert source == {"SSL_CERT_FILE": "/x.pem", "PYTHONDONTWRITEBYTECODE": "1", "KEEP": "1"}


@pytest.mark.parametrize("mode", ["editable", "installer", "uv_tool", "unknown"])
def test_without_the_bundled_marker_nothing_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, mode: str
) -> None:
    _mode(monkeypatch, mode)

    env = child_env()

    assert env["SSL_CERT_FILE"] == os.environ["SSL_CERT_FILE"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_marker_is_read_from_the_install_mode_at_spawn_time(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    modes = iter(["editable", "bundled_app"])
    monkeypatch.setattr(package_version, "detect_install_mode", lambda: next(modes))

    assert "SSL_CERT_FILE" in child_env()
    assert "SSL_CERT_FILE" not in child_env()


def test_acp_agent_base_env_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "bundled_app")
    monkeypatch.setattr(connection_module, "terminal_path", lambda: "/term/bin")

    env = connection_module.agent_base_env(())

    assert not (set(SCRUBBED) & set(env))
    assert env["PATH"] == "/term/bin"
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")


def test_acp_agent_base_env_keeps_both_outside_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "editable")
    monkeypatch.setattr(connection_module, "terminal_path", lambda: "/term/bin")

    env = connection_module.agent_base_env(())

    assert env["SSL_CERT_FILE"] == os.environ["SSL_CERT_FILE"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_native_and_pty_terminal_env_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "bundled_app")

    env = host_terminal._terminal_env()

    assert not (set(SCRUBBED) & set(env))
    assert env["CHILD_ENV_PROBE_KEEP"] == "keep-sentinel"


def test_scheduled_command_env_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, tmp_path: Path
) -> None:
    _mode(monkeypatch, "bundled_app")
    seen: dict[str, Any] = {}

    async def fake_shell(command: str, **kwargs: Any) -> None:
        seen.update(kwargs)
        raise RuntimeError("stop after capturing the env")

    monkeypatch.setattr(schedule_command.asyncio, "create_subprocess_shell", fake_shell)

    with pytest.raises(RuntimeError):
        asyncio.run(schedule_command.run_schedule_command("true", tmp_path, {"EXTRA": "1"}))

    env = seen["env"]
    assert not (set(SCRUBBED) & set(env))
    assert env["EXTRA"] == "1"
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")



def test_npm_for_adapter_installs_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    """npm runs under the same child rule: Node ignores SSL_CERT_FILE, so nothing is kept for it."""
    _mode(monkeypatch, "bundled_app")
    monkeypatch.setattr(adapter_install, "terminal_path", lambda: "/term/bin")

    env = adapter_install._npm_env("/opt/node/bin/node")

    assert not (set(SCRUBBED) & set(env))
    assert os.environ["SSL_CERT_FILE"].endswith("cacert.pem")


def test_npm_for_adapter_installs_keeps_ssl_cert_file_outside_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "editable")
    monkeypatch.setattr(adapter_install, "terminal_path", lambda: "/term/bin")

    env = adapter_install._npm_env("/opt/node/bin/node")

    assert env["SSL_CERT_FILE"] == os.environ["SSL_CERT_FILE"]


# -- every other spawn site: the env it passes to subprocess.run ----------------------------


@pytest.fixture
def capture_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": list(argv), **kwargs})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _check_env(call: dict[str, Any], bundled: bool) -> None:
    assert "env" in call, "the spawn passes no env, so it inherits os.environ"
    env = call["env"]
    if bundled:
        assert not (set(SCRUBBED) & set(env))
    else:
        assert env["SSL_CERT_FILE"] == os.environ["SSL_CERT_FILE"]
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_auth_login_child_gets_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    mode: str, bundled: bool,
) -> None:
    from argparse import Namespace

    from ciao import cli

    _mode(monkeypatch, mode)
    monkeypatch.setattr(cli, "_auth_command_for_provider", lambda *_a, **_k: ["/bin/claude", "auth", "login"])
    before = dict(os.environ)

    assert cli._auth_command(Namespace(provider="claude", print_only=False, device_auth=False)) == 0

    _check_env(capture_runs[0], bundled)
    assert dict(os.environ) == before


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_claude_auth_status_child_gets_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    mode: str, bundled: bool,
) -> None:
    from ciao import setup_status

    _mode(monkeypatch, mode)
    setup_status.claude_auth_status("/bin/claude")

    _check_env(capture_runs[0], bundled)


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_git_helpers_child_get_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    tmp_path: Path, mode: str, bundled: bool,
) -> None:
    from ciao import git_proc

    _mode(monkeypatch, mode)
    git_proc.run_git_sync(tmp_path, "status")

    _check_env(capture_runs[0], bundled)


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_async_git_helper_child_gets_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, tmp_path: Path, mode: str, bundled: bool
) -> None:
    from ciao import git_proc

    _mode(monkeypatch, mode)
    seen: list[dict[str, Any]] = []

    async def fake_exec(*argv: Any, **kwargs: Any) -> None:
        seen.append(kwargs)
        raise RuntimeError("stop after capturing the env")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError):
        asyncio.run(git_proc.run_git(tmp_path, "status"))

    _check_env(seen[0], bundled)


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_pwa_commit_and_push_children_get_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    tmp_path: Path, mode: str, bundled: bool,
) -> None:
    from ciao.web import routes_helpers

    _mode(monkeypatch, mode)
    asyncio.run(routes_helpers._commit_and_push(tmp_path, "snapshot"))

    assert capture_runs, "no git child ran"
    for call in capture_runs:
        _check_env(call, bundled)


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_provider_version_probe_child_gets_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    mode: str, bundled: bool,
) -> None:
    from ciao import provider_versions

    _mode(monkeypatch, mode)
    provider_versions._probe_version("/bin/tool")

    _check_env(capture_runs[0], bundled)


@pytest.mark.parametrize(("mode", "bundled"), [("bundled_app", True), ("editable", False)])
def test_setup_cli_version_probe_child_gets_the_child_env(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, capture_runs: list[dict[str, Any]],
    mode: str, bundled: bool,
) -> None:
    from ciao import setup_status

    _mode(monkeypatch, mode)
    setup_status._cli_version.cache_clear()  # lru_cache'd: a cached answer spawns nothing
    setup_status._cli_version("/bin/tool")

    _check_env(capture_runs[0], bundled)
