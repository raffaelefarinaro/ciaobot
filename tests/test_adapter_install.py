"""The on-demand ACP adapter install (#3425). Fakes only: no npm, no network, no real CLI."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ciao import adapter_install as ai
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.agents import CLAUDE, CodexAcpProvider


@pytest.fixture
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "adapters"
    monkeypatch.setattr(ai, "adapter_prefix", lambda: root)
    monkeypatch.setattr(ai, "_ATTEMPTED", set())
    monkeypatch.setattr(ai, "_FAILURES", {})
    monkeypatch.setattr(ai, "_node_major", lambda node: 22)
    monkeypatch.setattr(ai, "_node_path", lambda: "/fake/node")
    monkeypatch.setattr(ai, "_cli_path", lambda name: f"/fake/{ai.ADAPTERS[name].cli}")
    monkeypatch.setattr("ciao.providers.acp.connection.terminal_path", lambda: "/term/bin")
    return root


def _write_adapter(prefix: Path, name: str, *, version: str | None = None, entry: bool = True) -> None:
    pin = ai.ADAPTERS[name]
    pkg = prefix / "node_modules" / Path(*pin.package.split("/"))
    pkg.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"name": pin.package, "version": pin.version if version is None else version}
    data["bin"] = {name: "dist/index.js"}
    (pkg / "package.json").write_text(json.dumps(data), encoding="utf-8")
    if entry:
        (pkg / "dist").mkdir(exist_ok=True)
        (pkg / "dist" / "index.js").write_text("// adapter\n", encoding="utf-8")


def _completed(rc: int, stderr: str = "") -> subprocess.CompletedProcess[Any]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=b"", stderr=stderr.encode())


# -- the install line and the pins ------------------------------------------------------------


def test_install_line_pins_the_exact_version_and_skips_optional_packages_and_scripts(
    prefix: Path,
) -> None:
    for name, package in (
        (ai.CLAUDE_ADAPTER, "@agentclientprotocol/claude-agent-acp@0.108.1"),
        (ai.CODEX_ADAPTER, "@agentclientprotocol/codex-acp@2.7.0"),
    ):
        argv = shlex.split(ai.install_command(name))
        assert argv[0] == "npm"
        assert package in argv
        assert "--omit=optional" in argv
        assert "--ignore-scripts" in argv
        assert argv[argv.index("--prefix") + 1] == str(prefix)
        assert "-g" not in argv


def test_codex_cli_install_is_the_pinned_cli_only() -> None:
    assert ai.CODEX_CLI_INSTALL == "npm install -g @openai/codex@0.166.0"


# -- detection --------------------------------------------------------------------------------


def test_detect_reports_the_first_failing_check_and_ready_when_all_pass(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER, version="0.100.0")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_outdated"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER, entry=False)
    (prefix / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js").unlink(
        missing_ok=True
    )
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"

    monkeypatch.setattr(ai, "_node_major", lambda node: 20)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_old"
    monkeypatch.setattr(ai, "_node_path", lambda: None)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_missing"
    monkeypatch.setattr(ai, "_cli_path", lambda name: None)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "cli_missing"


def test_an_entry_outside_the_adapter_package_is_refused(prefix: Path) -> None:
    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    outside = prefix / "node_modules" / "evil.js"
    outside.write_text("//\n", encoding="utf-8")
    pkg_json = prefix / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "package.json"
    data = json.loads(pkg_json.read_text(encoding="utf-8"))
    data["bin"] = {ai.CLAUDE_ADAPTER: "../../evil.js"}
    pkg_json.write_text(json.dumps(data), encoding="utf-8")

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_launch_runs_the_adapter_under_the_resolved_node_only_when_ready(prefix: Path) -> None:
    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    entry = prefix / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js"

    assert ai.launch_argv(ai.CLAUDE_ADAPTER) == ["/fake/node", str(entry.resolve())]


def test_launch_refuses_to_start_when_the_cli_is_missing(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(ai, "_cli_path", lambda name: None)

    with pytest.raises(ai.AdapterNotReady, match="claude CLI is not installed"):
        ai.launch_argv(ai.CLAUDE_ADAPTER)


def test_managed_adapter_never_spawns_when_it_is_not_ready(
    prefix: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned: list[tuple[Any, ...]] = []

    async def no_spawn(*argv: Any, **_kwargs: Any) -> None:
        spawned.append(argv)
        raise AssertionError("the adapter must not start")

    monkeypatch.setattr(ai, "_cli_path", lambda name: None)
    monkeypatch.setattr(connection_module.asyncio, "create_subprocess_exec", no_spawn)

    with pytest.raises(connection_module.AgentStartError, match="claude CLI is not installed"):
        asyncio.run(
            connection_module.AcpConnection.open(CLAUDE, cwd=tmp_path, env={}, client=object())  # type: ignore[arg-type]
        )
    assert spawned == []


# -- the install itself -----------------------------------------------------------------------


def test_install_runs_npm_as_an_argv_in_the_prefix_with_a_timeout_and_no_ciao_env(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append((list(argv), kwargs))
        _write_adapter(prefix, ai.CLAUDE_ADAPTER)
        return _completed(0)

    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "/fake/npm" if cmd == "npm" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CIAO_SECRET_TEST_VALUE", "1")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "pwa-token-value")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    ai.install(ai.CLAUDE_ADAPTER)

    (argv, kwargs), = calls
    assert argv[0] == "/fake/npm"
    assert "@agentclientprotocol/claude-agent-acp@0.108.1" in argv
    assert "--omit=optional" in argv and "--ignore-scripts" in argv
    assert kwargs["cwd"] == prefix
    assert kwargs["timeout"] == ai.INSTALL_TIMEOUT_S == 300
    assert not kwargs.get("shell", False)
    env = kwargs["env"]
    assert not any(key.startswith("CIAO_") for key in env)
    assert "PWA_AUTH_TOKEN" not in env
    assert env["PATH"] == "/term/bin"
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"


def test_install_that_finishes_without_a_ready_adapter_says_so(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _completed(0))

    with pytest.raises(ai.AdapterInstallError, match="still adapter_missing"):
        ai.install(ai.CLAUDE_ADAPTER)


def test_failed_install_message_is_redacted_and_not_retried_at_startup(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    stderr = "npm ERR! fetch failed https://user:s3cret-pass@registry.example/x npm_ABCDEFGH1234"

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append(list(argv))
        return _completed(1, stderr)

    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", fake_run)

    first = ai.install_at_startup(ai.CLAUDE_ADAPTER)
    second = ai.install_at_startup(ai.CLAUDE_ADAPTER)

    assert "exit 1" in first
    assert "s3cret-pass" not in first and "npm_ABCDEFGH1234" not in first
    assert "registry.example" in first
    assert second == first
    assert len(calls) == 1
    assert ai.failure(ai.CLAUDE_ADAPTER) == first


def test_startup_install_runs_only_for_a_present_cli_with_a_missing_or_outdated_adapter(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        calls.append(list(argv))
        _write_adapter(prefix, ai.CODEX_ADAPTER)
        return _completed(0)

    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "/fake/npm")
    monkeypatch.setattr(subprocess, "run", fake_run)

    monkeypatch.setattr(ai, "_cli_path", lambda name: None)
    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ai.describe(
        ai.CODEX_ADAPTER, "cli_missing"
    )
    assert calls == []

    monkeypatch.setattr(ai, "_cli_path", lambda name: "/fake/codex")
    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ""
    assert len(calls) == 1

    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ""
    assert len(calls) == 1


def test_install_itself_refuses_without_the_cli_or_a_usable_node(
    prefix: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("npm must not run"))
    monkeypatch.setattr(ai, "_cli_path", lambda name: None)
    with pytest.raises(ai.AdapterInstallError, match="claude CLI is not installed"):
        ai.install(ai.CLAUDE_ADAPTER)

    monkeypatch.setattr(ai, "_cli_path", lambda name: "/fake/claude")
    monkeypatch.setattr(ai, "_node_major", lambda node: 18)
    with pytest.raises(ai.AdapterInstallError, match="Node 22"):
        ai.install(ai.CLAUDE_ADAPTER)


def test_a_ready_adapter_is_never_reinstalled_at_startup(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("npm must not run"))

    assert ai.install_at_startup(ai.CLAUDE_ADAPTER) == ""


# -- the Codex and shared environment seams ---------------------------------------------------


def test_codex_spawn_env_points_codex_acp_at_the_users_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.providers.acp import agents

    monkeypatch.setattr(agents, "codex_binary", lambda: "/opt/tools/codex")
    assert CodexAcpProvider.spawn_env() == {"CODEX_PATH": "/opt/tools/codex"}

    monkeypatch.setattr(agents, "codex_binary", lambda: None)
    assert CodexAcpProvider.spawn_env() == {}


def test_agent_env_puts_the_terminal_path_first_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "terminal_path", lambda: "/term/bin:/usr/bin")
    monkeypatch.setenv("PATH", "/stripped/launchd/path")

    env = connection_module.agent_base_env(())

    assert env["PATH"] == "/term/bin:/usr/bin"
    assert all(not key.startswith("CIAO_") for key in env)
    assert os.environ["PATH"] == "/stripped/launchd/path"
