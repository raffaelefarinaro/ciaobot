"""The bundled Mac engine's launcher exports two variables that no child should inherit.

``mac/scripts/engine-launcher.sh`` sets ``SSL_CERT_FILE`` (the certifi bundle) and
``PYTHONDONTWRITEBYTECODE=1`` for the engine. Children the engine starts (ACP agents, the
terminal, scheduled commands, provider probes) get a copy without them; the engine's own
``os.environ`` keeps them, so its httpx TLS still trusts certifi. Outside the bundle nothing is
scrubbed.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from ciao import adapter_install, dag, gws_wrapper, host_terminal, package_version, schedule_command
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


def test_bash_workflow_node_env_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    _mode(monkeypatch, "bundled_app")

    env = dag._subprocess_env({"env": {"NODE_KEY": "1"}})
    assert env is not None
    assert not (set(SCRUBBED) & set(env))
    assert env["NODE_KEY"] == "1"

    bare = dag._subprocess_env({})
    assert bare is not None and not (set(SCRUBBED) & set(bare))



def test_gws_environment_is_scrubbed_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None, tmp_path: Path
) -> None:
    _mode(monkeypatch, "bundled_app")
    monkeypatch.setattr(gws_wrapper, "login_shell_path", lambda: "/usr/bin")
    monkeypatch.setattr(gws_wrapper, "profile_config_dir", lambda *_a: tmp_path, raising=False)

    env = gws_wrapper._gws_environment(tmp_path, "default")

    assert not (set(SCRUBBED) & set(env))
    assert env["CHILD_ENV_PROBE_KEEP"] == "keep-sentinel"


def test_npm_for_adapter_installs_keeps_certifi_in_the_bundle(
    monkeypatch: pytest.MonkeyPatch, launcher_env: None
) -> None:
    """npm verifies TLS against SSL_CERT_FILE, so its allowlisted env must keep it in the bundle."""
    _mode(monkeypatch, "bundled_app")
    monkeypatch.setattr(adapter_install, "terminal_path", lambda: "/term/bin")

    env = adapter_install._npm_env("/opt/node/bin/node")

    assert env["SSL_CERT_FILE"] == os.environ["SSL_CERT_FILE"]
    # npm gets an allowlist, so the bytecode switch is not among the keys it receives.
    assert "PYTHONDONTWRITEBYTECODE" not in env
