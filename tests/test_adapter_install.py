"""The on-demand ACP adapter install (#3425). Fakes only: no npm, no network, no real CLI.

The one real thing touched is the filesystem under ``tmp_path``: the lock file and the tree the
fake npm writes. Executables are empty files that are never run.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from ciao import adapter_install as ai
from ciao import setup_status
from ciao.providers.acp import agents as agents_module
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.agents import CLAUDE, ClaudeAcpProvider, CodexAcpProvider
from ciao.main import StartupTracker

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX lock and permission semantics")

# Captured before any fixture patches subprocess.Popen, for the one test that starts a real child.
_REAL_POPEN = subprocess.Popen


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "adapters"
    monkeypatch.setattr(ai, "adapter_prefix", lambda: root)
    monkeypatch.setattr(ai, "_ATTEMPTED", set())
    monkeypatch.setattr(ai, "_FAILURES", {})
    monkeypatch.setattr(ai, "_NODE_MAJOR", {})
    monkeypatch.setattr(ai, "_probe_node_major", lambda node: 22)
    monkeypatch.setattr(connection_module, "terminal_path", lambda: "/term/bin")
    monkeypatch.setattr(ai, "terminal_path", lambda: "/term/bin")
    bin_dir = tmp_path / "bin"
    node, npm = _executable(bin_dir / "node"), _executable(bin_dir / "npm")
    claude, codex = _executable(bin_dir / "claude"), _executable(bin_dir / "codex")
    fakes = {"node": node, "npm": npm}
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: fakes.get(cmd))
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: claude)
    monkeypatch.setattr(agents_module, "codex_binary", lambda: codex)
    return root


def _pkg_dir(prefix: Path, name: str) -> Path:
    return prefix / name / "node_modules" / Path(*ai.ADAPTERS[name].package.split("/"))


def _private_dir(path: Path) -> None:
    """A folder the way the engine makes it: created if needed, then closed to everyone but us."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_adapter(prefix: Path, name: str, *, version: str | None = None, entry: bool = True) -> Path:
    pin = ai.ADAPTERS[name]
    _private_dir(prefix)
    _private_dir(prefix / name)
    pkg = _pkg_dir(prefix, name)
    pkg.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"name": pin.package, "version": pin.version if version is None else version}
    data["bin"] = {name: "dist/index.js"}
    (pkg / "package.json").write_text(json.dumps(data), encoding="utf-8")
    if entry:
        (pkg / "dist").mkdir(exist_ok=True)
        (pkg / "dist" / "index.js").write_text("// adapter\n", encoding="utf-8")
    return pkg


def _write_marker(prefix: Path, name: str) -> None:
    marker = {"version": ai.ADAPTERS[name].version, "lock_sha256": ai._lock_sha256(name)}
    (prefix / f".ciao-{name}.ok").write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")


def _ready(prefix: Path, name: str) -> None:
    _write_adapter(prefix, name)
    _write_marker(prefix, name)


class _Call:
    def __init__(self, argv: list[str], kwargs: dict[str, Any]) -> None:
        self.argv = argv
        self.kwargs = kwargs


class FakeNpm:
    """Stands in for ``subprocess.Popen`` for npm. ``on_run`` decides the outcome of each call."""

    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.on_run: Callable[[_Call], tuple[int, bytes, bytes]] = lambda call: (0, b"", b"")

    def popen(self, argv: list[str], **kwargs: Any) -> _FakeProc:
        call = _Call(list(argv), kwargs)
        self.calls.append(call)
        return _FakeProc(self, call)


class _FakeProc:
    pid = 4242

    def __init__(self, owner: FakeNpm, call: _Call) -> None:
        self._owner = owner
        self._call = call
        self.returncode: int | None = None

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self._call.kwargs["_timeout"] = timeout
        rc, out, err = self._owner.on_run(self._call)
        self.returncode = rc
        return out, err

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover - only the Windows branch calls it
        self.returncode = -9


@pytest.fixture(autouse=True)
def killed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Every os.killpg call is recorded, never sent: a fake pid must not signal a real group."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    return calls


@pytest.fixture
def npm(monkeypatch: pytest.MonkeyPatch) -> FakeNpm:
    fake = FakeNpm()
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    return fake


def _writes_claude_tree(prefix: Path) -> Callable[[_Call], tuple[int, bytes, bytes]]:
    def run(call: _Call) -> tuple[int, bytes, bytes]:
        _write_adapter(prefix, ai.CLAUDE_ADAPTER)
        return 0, b"", b""

    return run


# -- the install line and the pins ------------------------------------------------------------


def test_install_line_runs_this_module_with_the_engine_python_and_never_global(prefix: Path) -> None:
    for name in (ai.CLAUDE_ADAPTER, ai.CODEX_ADAPTER):
        argv = shlex.split(ai.install_command(name))
        assert argv == [sys.executable, *ai.isolated_probe_flags(), "-m", "ciao.adapter_install", name]
        assert "-g" not in argv and "--prefix" not in argv


def test_install_line_is_isolated_so_a_planted_module_in_the_workspace_never_runs(prefix: Path) -> None:
    argv = shlex.split(ai.install_command(ai.CLAUDE_ADAPTER))
    assert argv[1] == "-I"
    assert "-I" in ai.isolated_probe_flags()


def test_codex_cli_install_is_the_pinned_cli_only() -> None:
    assert ai.CODEX_CLI_INSTALL == "npm install -g @openai/codex@0.166.0"


def test_npm_runs_ci_with_the_omit_and_ignore_scripts_flags() -> None:
    assert ai.NPM_CI_FLAGS == ("ci", "--ignore-scripts", "--omit=optional", "--no-audit", "--no-fund")


def test_stock_lockfiles_pin_the_adapter_versions_and_ship_integrity(prefix: Path) -> None:
    for name, pin in ai.ADAPTERS.items():
        lock = json.loads(ai._stock_bytes(name, "package-lock.json"))
        package = json.loads(ai._stock_bytes(name, "package.json"))

        assert lock["packages"][""]["version"] == pin.version
        assert package["version"] == pin.version
        assert lock["packages"][""]["dependencies"] == {pin.package: pin.version}
        assert lock["packages"][f"node_modules/{pin.package}"]["version"] == pin.version

        for key, entry in lock["packages"].items():
            if key and not entry.get("link"):
                assert "integrity" in entry, f"{name}: {key} has no integrity hash"


def test_stock_lockfiles_carry_no_platform_binary_that_is_not_optional() -> None:
    markers = ("-darwin-", "-linux-", "-win32-")
    for name in ai.ADAPTERS:
        lock = json.loads(ai._stock_bytes(name, "package-lock.json"))
        for key, entry in lock["packages"].items():
            if any(marker in key for marker in markers):
                assert entry.get("optional") is True, f"{name}: {key} is a platform binary, not optional"


# -- detection --------------------------------------------------------------------------------


def test_detect_reports_the_first_failing_check_and_ready_only_with_a_marker(prefix: Path) -> None:
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER, version="0.100.0")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_outdated"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    (_pkg_dir(prefix, ai.CLAUDE_ADAPTER) / "dist" / "index.js").unlink()
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"

    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing", "a complete tree without a marker is not ready"

    _write_marker(prefix, ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"


def test_a_marker_for_another_lock_or_pin_does_not_count(prefix: Path) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    marker = prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok"
    stale = {"version": ai.ADAPTERS[ai.CLAUDE_ADAPTER].version, "lock_sha256": "0" * 64}
    marker.write_text(json.dumps(stale), encoding="utf-8")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"

    marker.write_text("not json", encoding="utf-8")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_node_and_cli_checks_come_before_the_adapter(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"

    monkeypatch.setattr(ai, "_NODE_MAJOR", {})  # a cached major from the ready check above would hide the probe
    monkeypatch.setattr(ai, "_probe_node_major", lambda node: 20)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_old"
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: None)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_missing"
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "cli_missing"


@pytest.mark.parametrize("bad", ["./node", "node", "", "relative/node"])
def test_a_relative_node_is_node_missing(prefix: Path, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: bad if cmd == "node" else None)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_missing"


def test_a_relative_node_is_refused_even_when_it_is_an_executable_in_the_working_directory(
    prefix: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    cwd = tmp_path / "cwd"
    _executable(cwd / "node")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "node" if cmd == "node" else None)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "node_missing"


def test_a_relative_or_non_executable_cli_is_cli_missing(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "./claude")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "cli_missing"

    inert = prefix.parent / "inert-claude"
    inert.write_text("", encoding="utf-8")
    inert.chmod(0o644)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: str(inert))
    assert ai.detect(ai.CLAUDE_ADAPTER) == "cli_missing"


def test_a_relative_codex_cli_is_cli_missing(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(prefix, ai.CODEX_ADAPTER)
    monkeypatch.setattr(agents_module, "codex_binary", lambda: "./codex")
    assert ai.detect(ai.CODEX_ADAPTER) == "cli_missing"


def test_an_entry_outside_the_adapter_package_is_refused(prefix: Path) -> None:
    pkg = _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    (prefix / ai.CLAUDE_ADAPTER / "evil.js").write_text("//\n", encoding="utf-8")
    pkg_json = pkg / "package.json"
    data = json.loads(pkg_json.read_text(encoding="utf-8"))
    data["bin"] = {ai.CLAUDE_ADAPTER: "../../../evil.js"}
    pkg_json.write_text(json.dumps(data), encoding="utf-8")
    _write_marker(prefix, ai.CLAUDE_ADAPTER)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_a_symlinked_package_directory_is_refused(prefix: Path, tmp_path: Path) -> None:
    other = tmp_path / "other-root"
    _ready(other, ai.CLAUDE_ADAPTER)  # a complete, marked tree, just not under the prefix
    node_modules = prefix / ai.CLAUDE_ADAPTER / "node_modules"
    prefix.joinpath(ai.CLAUDE_ADAPTER).mkdir(parents=True, exist_ok=True)
    node_modules.symlink_to(other / ai.CLAUDE_ADAPTER / "node_modules", target_is_directory=True)
    _write_marker(prefix, ai.CLAUDE_ADAPTER)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_a_symlinked_entry_is_refused(prefix: Path, tmp_path: Path) -> None:
    pkg = _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    outside = tmp_path / "outside.js"
    outside.write_text("//\n", encoding="utf-8")
    (pkg / "dist" / "index.js").unlink()
    (pkg / "dist" / "index.js").symlink_to(outside)
    _write_marker(prefix, ai.CLAUDE_ADAPTER)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_launch_runs_the_adapter_under_the_resolved_node_only_when_ready(prefix: Path) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    entry = _pkg_dir(prefix, ai.CLAUDE_ADAPTER) / "dist" / "index.js"

    assert ai.launch_argv(ai.CLAUDE_ADAPTER) == [ai._node_path(), os.path.realpath(entry)]


def test_launch_refuses_when_not_ready_and_says_why(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")

    with pytest.raises(ai.AdapterNotReady, match="claude CLI is not installed"):
        ai.launch_argv(ai.CLAUDE_ADAPTER)


def test_managed_adapter_never_spawns_when_it_is_not_ready(
    prefix: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawned: list[tuple[Any, ...]] = []

    async def no_spawn(*argv: Any, **_kwargs: Any) -> None:
        spawned.append(argv)
        raise AssertionError("the adapter must not start")

    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    monkeypatch.setattr(connection_module.asyncio, "create_subprocess_exec", no_spawn)

    with pytest.raises(connection_module.AgentStartError, match="claude CLI is not installed"):
        asyncio.run(
            connection_module.AcpConnection.open(CLAUDE, cwd=tmp_path, env={}, client=object())  # type: ignore[arg-type]
        )
    assert spawned == []


def test_node_major_is_re_probed_when_the_node_binary_changes(
    prefix: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    node = tmp_path / "node-under-test"
    _executable(node)
    majors = iter([20, 22])
    monkeypatch.setattr(ai, "_probe_node_major", lambda path: next(majors))

    assert ai._node_major(str(node)) == 20
    assert ai._node_major(str(node)) == 20, "cached on path and mtime, so no second probe"
    os.utime(node, ns=(os.stat(node).st_atime_ns, os.stat(node).st_mtime_ns + 5_000_000_000))
    assert ai._node_major(str(node)) == 22


def test_describe_says_installing_while_the_adapter_lock_is_held(prefix: Path) -> None:
    fd = _hold(prefix, ai.CLAUDE_ADAPTER)
    try:
        assert "is being installed" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")
    finally:
        os.close(fd)
    assert "is being installed" not in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")


def test_launch_does_not_read_a_tree_while_its_install_holds_the_lock(prefix: Path) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    fd = _hold(prefix, ai.CLAUDE_ADAPTER)
    try:
        with pytest.raises(ai.AdapterNotReady, match="being installed"):
            ai.launch_argv(ai.CLAUDE_ADAPTER)
    finally:
        os.close(fd)
    assert ai.launch_argv(ai.CLAUDE_ADAPTER)[1].endswith("dist/index.js")


def _hold(prefix: Path, name: str) -> int:
    import fcntl

    _private_dir(prefix)
    fd = os.open(prefix / f".ciao-{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


# -- the private prefix -----------------------------------------------------------------------


def test_prefix_is_created_private_and_the_adapter_folder_too(prefix: Path, npm: FakeNpm) -> None:
    npm.on_run = _writes_claude_tree(prefix)

    ai.install(ai.CLAUDE_ADAPTER)

    assert stat.S_IMODE(os.stat(prefix).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(prefix / ai.CLAUDE_ADAPTER).st_mode) == 0o700


def test_a_group_or_world_writable_prefix_is_refused_before_npm_runs(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    _write_marker(prefix, ai.CLAUDE_ADAPTER)
    prefix.chmod(0o777)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert "open to other users" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")
    with pytest.raises(ai.AdapterInstallError, match="open to other users"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_a_prefix_owned_by_another_user_is_refused(prefix: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(prefix).st_uid + 1)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert "not owned by this user" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")


# -- the install itself -----------------------------------------------------------------------


def test_install_runs_npm_ci_in_the_adapter_folder_from_the_shipped_lockfile(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm.on_run = _writes_claude_tree(prefix)
    monkeypatch.setenv("CIAO_SECRET_TEST_VALUE", "1")

    ai.install(ai.CLAUDE_ADAPTER)

    (call,) = npm.calls
    assert call.argv == [ai.absolute_executable(ai.resolve_tool("npm")), *ai.NPM_CI_FLAGS]
    assert call.kwargs["cwd"] == prefix / ai.CLAUDE_ADAPTER
    assert call.kwargs["_timeout"] == ai.INSTALL_TIMEOUT_S == 300
    assert call.kwargs["start_new_session"] is True
    assert "shell" not in call.kwargs or not call.kwargs["shell"]
    assert (prefix / ai.CLAUDE_ADAPTER / "package-lock.json").read_bytes() == ai._stock_bytes(
        ai.CLAUDE_ADAPTER, "package-lock.json"
    )
    assert (prefix / ai.CLAUDE_ADAPTER / "package.json").read_bytes() == ai._stock_bytes(
        ai.CLAUDE_ADAPTER, "package.json"
    )
    marker = json.loads((prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok").read_text(encoding="utf-8"))
    assert marker == {
        "version": ai.ADAPTERS[ai.CLAUDE_ADAPTER].version,
        "lock_sha256": hashlib.sha256(ai._stock_bytes(ai.CLAUDE_ADAPTER, "package-lock.json")).hexdigest(),
    }
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"


def test_npm_gets_an_allowlisted_environment_and_no_secrets(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm.on_run = _writes_claude_tree(prefix)
    secrets = {
        "CIAO_SECRET_TEST_VALUE": "ciao-sentinel",
        "PWA_AUTH_TOKEN": "pwa-sentinel",
        "OPENAI_API_KEY": "openai-sentinel",
        "ANTHROPIC_API_KEY": "anthropic-sentinel",
        "GITHUB_TOKEN": "github-sentinel",
        "NODE_OPTIONS": "--require /evil.js",
        "AWS_SECRET_ACCESS_KEY": "aws-sentinel",
    }
    kept = {
        "HOME": "/Users/tester",
        "https_proxy": "http://proxy.example:3128",
        "NO_PROXY": "localhost",
        "NODE_EXTRA_CA_CERTS": "/etc/ssl/corp.pem",
        "SSL_CERT_FILE": "/etc/ssl/cert.pem",
        "npm_config_registry": "https://registry.example/",
        "NPM_CONFIG_USERCONFIG": "/Users/tester/.npmrc",
    }
    for key, value in {**secrets, **kept}.items():
        monkeypatch.setenv(key, value)

    ai.install(ai.CLAUDE_ADAPTER)

    env = npm.calls[0].kwargs["env"]
    for key, value in secrets.items():
        assert key not in env, key
        assert value not in env.values()
    for key, value in kept.items():
        assert env[key] == value, key
    # npm's own node comes first, so its env-node is the one that passed the version check.
    assert env["PATH"] == f"{os.path.dirname(ai._node_path())}{os.pathsep}/term/bin"


def test_install_that_finishes_without_a_ready_adapter_says_so(prefix: Path, npm: FakeNpm) -> None:
    npm.on_run = lambda call: (0, b"", b"")

    with pytest.raises(ai.AdapterInstallError, match="still adapter_missing"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert not (prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok").exists()


def test_install_itself_refuses_without_the_cli_or_a_usable_node(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    with pytest.raises(ai.AdapterInstallError, match="claude CLI is not installed"):
        ai.install(ai.CLAUDE_ADAPTER)

    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: str(prefix.parent / "bin" / "claude"))
    monkeypatch.setattr(ai, "_probe_node_major", lambda node: 18)
    with pytest.raises(ai.AdapterInstallError, match="Node 22"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_a_relative_npm_is_not_run(prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch) -> None:
    node = ai.resolve_tool("node")
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: "./npm" if cmd == "npm" else node)

    with pytest.raises(ai.AdapterInstallError, match="npm was not found"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_marker_is_removed_before_npm_starts_and_a_complete_looking_tree_is_not_ready(
    prefix: Path, npm: FakeNpm
) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    seen: list[bool] = []

    def half_finished(call: _Call) -> tuple[int, bytes, bytes]:
        seen.append((prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok").exists())
        _write_adapter(prefix, ai.CLAUDE_ADAPTER)
        raise subprocess.TimeoutExpired(call.argv, ai.INSTALL_TIMEOUT_S)

    npm.on_run = half_finished
    # A ready adapter is never reinstalled, so force the install path with an outdated tree first.
    _write_adapter(prefix, ai.CLAUDE_ADAPTER, version="0.100.0")
    _write_marker(prefix, ai.CLAUDE_ADAPTER)

    with pytest.raises(ai.AdapterInstallError, match="timed out"):
        ai.install(ai.CLAUDE_ADAPTER)

    assert seen[0] is False, "the marker is gone before npm starts"
    assert not (prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok").exists()
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


def test_a_clean_exit_with_a_tree_that_is_not_ready_writes_no_marker(prefix: Path, npm: FakeNpm) -> None:
    def leaves_a_tree_without_its_entry(call: _Call) -> tuple[int, bytes, bytes]:
        _write_adapter(prefix, ai.CLAUDE_ADAPTER, entry=False)
        return 0, b"", b""

    npm.on_run = leaves_a_tree_without_its_entry
    with pytest.raises(ai.AdapterInstallError, match="still adapter_missing"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert not (prefix / f".ciao-{ai.CLAUDE_ADAPTER}.ok").exists()


def test_timeout_kills_the_process_group_and_is_reported_as_a_timeout(
    prefix: Path, npm: FakeNpm, killed: list[tuple[int, int]]
) -> None:
    def hangs(call: _Call) -> tuple[int, bytes, bytes]:
        raise subprocess.TimeoutExpired(call.argv, ai.INSTALL_TIMEOUT_S)

    npm.on_run = hangs
    with pytest.raises(ai.AdapterInstallError) as caught:
        ai.install(ai.CLAUDE_ADAPTER)

    assert str(caught.value) == "npm install timed out after 300 s."
    assert killed == [(4242, signal.SIGKILL)]


def test_failed_install_message_is_redacted_and_not_retried_at_startup(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    stderr = "\n".join(
        [
            "npm warn deprecated something@1.0.0: old",
            "npm error 401 Unauthorized - GET https://user:s3cret-pass@registry.example/x - Unauthorized",
            f"npm error cache at {home}/.npm/_cacache",
            "npm error _authToken=npm_ABCDEFGH1234567890 for registry.example",
            f"npm error A complete log of this run can be found in: {home}/.npm/_logs/2026-11-20-debug-0.log",
        ]
    ).encode()
    npm.on_run = lambda call: (1, b"", stderr)

    first = ai.install_at_startup(ai.CLAUDE_ADAPTER)
    second = ai.install_at_startup(ai.CLAUDE_ADAPTER)

    assert first == "npm install failed (exit 1): npm error 401 Unauthorized - GET https://[redacted]@registry.example/x - Unauthorized"
    assert "s3cret-pass" not in first
    assert "npm_ABCDEFGH1234567890" not in first
    assert second == first
    assert len(npm.calls) == 1
    assert ai.failure(ai.CLAUDE_ADAPTER) == first


def test_failure_line_prefers_the_error_code_and_skips_npms_log_pointer() -> None:
    stderr = "\n".join(
        [
            "npm warn deprecated left-pad@1.3.0",
            "npm error code E404",
            "npm error 404 Not Found - GET https://registry.npmjs.org/@agentclientprotocol%2fx",
            "npm error 404  '@agentclientprotocol/x@0.108.1' is not in this registry.",
            "npm error A complete log of this run can be found in: /Users/someone/.npm/_logs/x-debug-0.log",
        ]
    )
    assert ai._failure_line(stderr) == "npm error code E404"


def test_failure_line_without_a_code_takes_the_first_error_and_redacts_auth() -> None:
    stderr = "\n".join(
        [
            "npm ERR! something",
            "npm error 401 Unauthorized - GET https://registry.example/x",
            "npm error //registry.example/:_authToken=npm_ZZZZZZZZZZZZZZZZ",
            "npm error A complete log of this run can be found in: /x/_logs/y.log",
            "npm error Log: please see the file",
        ]
    )
    line = ai._failure_line(stderr)
    assert line == "npm error 401 Unauthorized - GET https://registry.example/x"


def test_failure_line_shows_the_home_folder_as_tilde(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    line = ai._failure_line(f"npm error EACCES: permission denied, mkdir '{tmp_path}/.npm/_cacache'")
    assert line == "npm error EACCES: permission denied, mkdir '~/.npm/_cacache'"


def test_failure_line_skips_the_log_pointer_even_when_npm_prints_it_first() -> None:
    stderr = "\n".join(
        [
            "npm error A complete log of this run can be found in: /x/_logs/y.log",
            "npm error 401 Unauthorized - GET https://registry.example/x",
        ]
    )
    assert ai._failure_line(stderr) == "npm error 401 Unauthorized - GET https://registry.example/x"


def test_failure_line_redacts_a_token_that_is_the_line() -> None:
    line = ai._failure_line("npm error //registry.example/:_authToken=npm_ZZZZZZZZZZZZZZZZ")
    assert "npm_ZZZZZZZZZZZZZZZZ" not in line
    assert "_authToken=[redacted]" in line


def test_startup_install_runs_only_for_a_present_cli_with_a_missing_or_outdated_adapter(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm.on_run = lambda call: (_write_adapter(prefix, ai.CODEX_ADAPTER), (0, b"", b""))[1]

    monkeypatch.setattr(agents_module, "codex_binary", lambda: None)
    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ai.describe(ai.CODEX_ADAPTER, "cli_missing")
    assert npm.calls == []

    monkeypatch.setattr(agents_module, "codex_binary", lambda: str(prefix.parent / "bin" / "codex"))
    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ""
    assert len(npm.calls) == 1

    assert ai.install_at_startup(ai.CODEX_ADAPTER) == ""
    assert len(npm.calls) == 1


def test_a_ready_adapter_is_never_reinstalled_at_startup(prefix: Path, npm: FakeNpm) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)

    assert ai.install_at_startup(ai.CLAUDE_ADAPTER) == ""
    assert npm.calls == []


def test_each_adapter_has_its_own_lock_so_installs_do_not_serialize(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "INSTALL_TIMEOUT_S", 0.2)
    npm.on_run = lambda call: (_write_adapter(prefix, ai.CODEX_ADAPTER), (0, b"", b""))[1]
    fd = _hold(prefix, ai.CLAUDE_ADAPTER)
    try:
        with pytest.raises(ai.AdapterInstallError, match="still running"):
            ai.install(ai.CLAUDE_ADAPTER)
        ai.install(ai.CODEX_ADAPTER)
    finally:
        os.close(fd)
    assert ai.detect(ai.CODEX_ADAPTER) == "ready"


# -- the Codex and shared environment seams ---------------------------------------------------


def test_codex_spawn_env_points_codex_acp_at_the_users_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.providers.acp import agents

    codex = _executable(tmp_path / "opt" / "codex")
    monkeypatch.setattr(agents, "codex_binary", lambda: codex)
    assert CodexAcpProvider.spawn_env() == {"CODEX_PATH": codex}

    monkeypatch.setattr(agents, "codex_binary", lambda: None)
    assert CodexAcpProvider.spawn_env() == {}


@pytest.mark.parametrize("bad", ["./codex", "codex", "/nonexistent/codex"])
def test_codex_path_is_never_relative_or_missing(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    from ciao.providers.acp import agents

    monkeypatch.setattr(agents, "codex_binary", lambda: bad)
    assert CodexAcpProvider.spawn_env() == {}


def test_claude_executable_is_never_relative_or_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    claude = _executable(tmp_path / "claude")
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: claude)
    assert ClaudeAcpProvider.spawn_env() == {"CLAUDE_CODE_EXECUTABLE": claude}

    for bad in ("./claude", "claude", "/nonexistent/claude", ""):
        monkeypatch.setattr(setup_status, "claude_cli_path", lambda bad=bad: bad)
        assert ClaudeAcpProvider.spawn_env() == {}


def test_agent_env_puts_the_terminal_path_first_on_every_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "terminal_path", lambda: "/term/bin:/usr/bin")
    monkeypatch.setenv("PATH", "/stripped/launchd/path")

    env = connection_module.agent_base_env(())

    assert env["PATH"] == "/term/bin:/usr/bin"
    assert all(not key.startswith("CIAO_") for key in env)
    assert os.environ["PATH"] == "/stripped/launchd/path"


def test_engine_entry_point_reports_usage_and_failures(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ai._main(["adapter_install.py", "not-an-adapter"]) == 2

    npm.on_run = lambda call: (1, b"", b"npm error code E500\n")
    assert ai._main(["adapter_install.py", ai.CLAUDE_ADAPTER]) == 1
    assert "npm error code E500" in capsys.readouterr().err

    npm.on_run = _writes_claude_tree(prefix)
    assert ai._main(["adapter_install.py", ai.CLAUDE_ADAPTER]) == 0
    assert "0.108.1 is installed" in capsys.readouterr().out


# -- round 2: lock sharing, npm inheriting the lock, background start, machine-wide adapters ---


def _join_installs(name: str) -> None:
    for thread in threading.enumerate():
        if thread.name == f"ciao-adapter-install-{name}":
            thread.join(10)


def test_npm_inherits_the_adapter_lock_so_it_outlives_the_engine(prefix: Path, npm: FakeNpm) -> None:
    lock_path = prefix / f".ciao-{ai.CLAUDE_ADAPTER}.lock"
    seen: dict[str, Any] = {}

    def run(call: _Call) -> tuple[int, bytes, bytes]:
        seen["pass_fds"] = call.kwargs["pass_fds"]
        other = os.open(lock_path, os.O_RDWR)
        try:
            seen["held_while_npm_runs"] = not ai._try_lock(other, shared=False)
        finally:
            os.close(other)
        _write_adapter(prefix, ai.CLAUDE_ADAPTER)
        return 0, b"", b""

    npm.on_run = run
    ai.install(ai.CLAUDE_ADAPTER)

    assert len(seen["pass_fds"]) == 1, "the lock fd is handed to npm"
    assert seen["held_while_npm_runs"] is True
    other = os.open(lock_path, os.O_RDWR)
    try:
        assert ai._try_lock(other, shared=False), "released once the install is done"
    finally:
        os.close(other)


def test_a_lock_held_by_another_process_stops_the_install_before_npm_starts(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover npm from a previous engine holds the lock; a second install must not start npm."""
    prefix.mkdir(parents=True, mode=0o700)
    lock_path = prefix / f".ciao-{ai.CLAUDE_ADAPTER}.lock"
    script = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = _REAL_POPEN([sys.executable, "-c", script, str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"
        monkeypatch.setattr(ai, "INSTALL_TIMEOUT_S", 0.3)
        with pytest.raises(ai.AdapterInstallError, match="still running"):
            ai.install(ai.CLAUDE_ADAPTER)
        assert npm.calls == []
    finally:
        child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()


def test_an_install_holding_the_lock_makes_a_launch_say_so_and_a_shared_holder_does_not(prefix: Path) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    shared = os.open(prefix / f".ciao-{ai.CLAUDE_ADAPTER}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert ai._try_lock(shared, shared=True)
        assert ai.launch_argv(ai.CLAUDE_ADAPTER)[1].endswith("dist/index.js"), "launches share"
        assert "is being installed" not in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")
    finally:
        ai._unlock(shared)
        os.close(shared)


def test_concurrent_launches_both_succeed_while_the_tree_is_healthy(prefix: Path) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    barrier = threading.Barrier(2)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def launch() -> None:
        barrier.wait()
        try:
            results.append(ai.launch_argv(ai.CLAUDE_ADAPTER))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert len(results) == 2 and results[0] == results[1]


def test_startup_install_in_background_returns_while_npm_still_runs(prefix: Path, npm: FakeNpm) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow(call: _Call) -> tuple[int, bytes, bytes]:
        started.set()
        release.wait(10)
        _write_adapter(prefix, ai.CLAUDE_ADAPTER)
        return 0, b"", b""

    npm.on_run = slow
    try:
        assert ai.start_install_in_background(ai.CLAUDE_ADAPTER) is None
        assert started.wait(10), "the install runs on its own thread"
        assert "is being installed" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")
    finally:
        release.set()
        _join_installs(ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"


def test_overall_ready_is_reached_while_an_adapter_install_runs(prefix: Path, npm: FakeNpm) -> None:
    release = threading.Event()
    npm.on_run = lambda call: (release.wait(10), _write_adapter(prefix, ai.CLAUDE_ADAPTER), (0, b"", b""))[2]
    tracker = StartupTracker()
    tracker.start("connect_claude_code")
    state = ai.start_install_in_background(ai.CLAUDE_ADAPTER)
    tracker.done(
        "connect_claude_code",
        "connected: 2.1.301"
        + ("; the ACP adapter is installing in the background" if state is None else ""),
    )
    try:
        assert tracker.overall_ready is True, "the startup overlay is not held by the install"
        assert tracker.phase("connect_claude_code").status == "done"
    finally:
        release.set()
        _join_installs(ai.CLAUDE_ADAPTER)


def test_windows_timeout_kills_the_whole_tree_with_taskkill(
    npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, killed: list[tuple[int, int]]
) -> None:
    monkeypatch.setattr(ai, "_windows", lambda: True)
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    proc = _FakeProc(npm, _Call([], {}))

    ai._kill_tree(proc)  # type: ignore[arg-type]

    assert commands == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert proc.returncode == -9, "proc.kill is the fallback after taskkill"
    assert killed == [], "no POSIX process-group signal on Windows"


def test_windows_kill_still_kills_the_process_when_taskkill_cannot_run(
    npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ai, "_windows", lambda: True)

    def missing(argv: list[str], **kwargs: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    proc = _FakeProc(npm, _Call([], {}))

    ai._kill_tree(proc)  # type: ignore[arg-type]

    assert proc.returncode == -9


def test_windows_npm_keeps_the_variables_npm_needs_to_start(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm.on_run = _writes_claude_tree(prefix)
    needed = {
        "TEMP": "C:\\Temp",
        "TMP": "C:\\Temp",
        "COMSPEC": "C:\\Windows\\system32\\cmd.exe",
        "ProgramFiles": "C:\\Program Files",
        "ProgramData": "C:\\ProgramData",
        "SystemDrive": "C:",
        "windir": "C:\\Windows",
    }
    for key, value in {**needed, "PWA_AUTH_TOKEN": "pwa-sentinel"}.items():
        monkeypatch.setenv(key, value)

    ai.install(ai.CLAUDE_ADAPTER)

    env = npm.calls[0].kwargs["env"]
    for key, value in needed.items():
        assert env[key] == value, key
    assert "PWA_AUTH_TOKEN" not in env


def test_a_prefix_that_cannot_be_created_is_reported_with_its_error_type(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: Path) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(ai, "_ensure_private_dir", denied)
    with pytest.raises(ai.AdapterInstallError, match=r"could not be prepared \(PermissionError\)"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_a_lock_that_cannot_be_opened_is_reported_with_its_error_type(prefix: Path, npm: FakeNpm) -> None:
    prefix.mkdir(parents=True, mode=0o700)
    (prefix / f".ciao-{ai.CLAUDE_ADAPTER}.lock").mkdir()

    with pytest.raises(ai.AdapterInstallError, match="IsADirectoryError"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_a_marker_that_cannot_be_written_is_its_own_failure_and_stays_recorded(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    npm.on_run = _writes_claude_tree(prefix)
    monkeypatch.setattr(ai, "_marker_path", lambda name: tmp_path / "missing" / "marker.ok")

    first = ai.install_at_startup(ai.CLAUDE_ADAPTER)

    assert first == "the install marker could not be written (FileNotFoundError)."
    assert "npm could not run" not in first
    assert ai.install_at_startup(ai.CLAUDE_ADAPTER) == first, "a later call never reads the failure as ready"
    assert len(npm.calls) == 1


def test_only_a_held_lock_counts_as_held_and_other_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fd = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:

        def busy(fd_: int, op: int) -> None:
            raise OSError(errno.EAGAIN, "busy")

        monkeypatch.setattr(ai.fcntl, "flock", busy)
        assert ai._try_lock(fd, shared=False) is False

        def broken(fd_: int, op: int) -> None:
            raise OSError(errno.EBADF, "bad fd")

        monkeypatch.setattr(ai.fcntl, "flock", broken)
        with pytest.raises(OSError, match="bad fd"):
            ai._try_lock(fd, shared=False)
    finally:
        os.close(fd)


def test_a_symlinked_prefix_is_refused(prefix: Path, npm: FakeNpm, tmp_path: Path) -> None:
    real = tmp_path / "real-prefix"
    _ready(real, ai.CLAUDE_ADAPTER)
    prefix.symlink_to(real, target_is_directory=True)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert "symbolic link" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")
    with pytest.raises(ai.AdapterInstallError, match="symbolic link"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []


def test_a_lock_file_that_is_a_symlink_is_not_followed(prefix: Path, npm: FakeNpm, tmp_path: Path) -> None:
    prefix.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("", encoding="utf-8")
    (prefix / f".ciao-{ai.CLAUDE_ADAPTER}.lock").symlink_to(target)

    with pytest.raises(ai.AdapterInstallError, match="could not be prepared"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert target.read_text(encoding="utf-8") == ""
    assert npm.calls == []


def test_a_prefix_open_to_the_group_is_refused_not_only_one_open_to_others(prefix: Path, npm: FakeNpm) -> None:
    _ready(prefix, ai.CLAUDE_ADAPTER)
    prefix.chmod(0o750)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert "open to other users" in ai.describe(ai.CLAUDE_ADAPTER, "adapter_missing")


def test_an_executable_in_a_group_or_world_writable_folder_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    node = _executable(folder / "node")

    folder.chmod(0o775)
    assert ai.absolute_executable(node) is None
    folder.chmod(0o755)
    assert ai.absolute_executable(node) == node


def test_timeout_drains_the_pipes_once_more_after_the_kill(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def hangs(call: _Call) -> tuple[int, bytes, bytes]:
        calls.append(1)
        raise subprocess.TimeoutExpired(call.argv, ai.INSTALL_TIMEOUT_S)

    npm.on_run = hangs
    with pytest.raises(ai.AdapterInstallError, match="timed out"):
        ai.install(ai.CLAUDE_ADAPTER)
    assert len(calls) == 2, "communicate once before the kill and once after it"


def test_bin_as_a_string_is_accepted_and_an_absolute_bin_outside_the_package_is_not(prefix: Path, tmp_path: Path) -> None:
    pkg = _write_adapter(prefix, ai.CLAUDE_ADAPTER)
    pkg_json = pkg / "package.json"
    data = json.loads(pkg_json.read_text(encoding="utf-8"))

    data["bin"] = "dist/index.js"
    pkg_json.write_text(json.dumps(data), encoding="utf-8")
    _write_marker(prefix, ai.CLAUDE_ADAPTER)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"

    outside = tmp_path / "outside.js"
    outside.write_text("//\n", encoding="utf-8")
    data["bin"] = {ai.CLAUDE_ADAPTER: str(outside)}
    pkg_json.write_text(json.dumps(data), encoding="utf-8")
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"


@pytest.mark.parametrize("name", list(ai.ADAPTERS))
def test_every_lock_entry_comes_from_the_npm_registry_with_no_git_file_or_link_source(name: str) -> None:
    lock = json.loads(ai._stock_bytes(name, "package-lock.json"))
    for key, entry in lock["packages"].items():
        if not key:
            continue
        assert not entry.get("link"), key
        assert entry.get("resolved", "").startswith("https://registry.npmjs.org/"), key
        assert not str(entry.get("version", "")).startswith(("git", "file:", "link:", "http", "github:")), key


# -- machine-wide adapter at the pin (M-6) ----------------------------------------------------


def _global_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str, writable: bool = False
) -> Path:
    """A machine-wide adapter: a package under a global npm root, its bin linked on the PATH."""
    name = ai.CLAUDE_ADAPTER
    root = tmp_path / "global-npm"
    pkg = root / "lib" / "node_modules" / Path(*ai.ADAPTERS[name].package.split("/"))
    pkg.mkdir(parents=True)
    data = {"name": ai.ADAPTERS[name].package, "version": version, "bin": {name: "dist/index.js"}}
    (pkg / "package.json").write_text(json.dumps(data), encoding="utf-8")
    entry = _executable(pkg / "dist" / "index.js")
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    bin_path = bin_dir / name
    bin_path.symlink_to(entry)
    if writable:
        pkg.chmod(0o777)
    base = ai.resolve_tool
    monkeypatch.setattr(ai, "resolve_tool", lambda cmd: str(bin_path) if cmd == name else base(cmd))
    # On Linux CI tmp_path sits under the world-writable /tmp, which the guard (rightly) refuses;
    # only judge the folders this test built.
    others_can_write = ai._others_can_write
    root_real = Path(os.path.realpath(root))
    monkeypatch.setattr(
        ai,
        "_others_can_write",
        lambda folder: others_can_write(folder) if folder == root_real or root_real in folder.parents else False,
    )
    return pkg


def test_a_machine_wide_adapter_at_the_pin_is_used_with_no_npm_call(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg = _global_install(tmp_path, monkeypatch, version=ai.ADAPTERS[ai.CLAUDE_ADAPTER].version)

    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"
    ai.install(ai.CLAUDE_ADAPTER)
    assert npm.calls == []
    assert ai.launch_argv(ai.CLAUDE_ADAPTER) == [ai._node_path(), os.path.realpath(pkg / "dist" / "index.js")]
    assert "using the adapter found at" in ai.describe(ai.CLAUDE_ADAPTER, "ready")


def test_a_machine_wide_adapter_at_another_version_is_left_alone_and_the_private_one_is_installed(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pkg = _global_install(tmp_path, monkeypatch, version="0.100.0")
    before = (pkg / "package.json").read_bytes()
    npm.on_run = _writes_claude_tree(prefix)

    ai.install(ai.CLAUDE_ADAPTER)

    assert len(npm.calls) == 1
    assert (pkg / "package.json").read_bytes() == before, "the machine-wide copy is never modified"
    assert ai.launch_argv(ai.CLAUDE_ADAPTER)[1].startswith(os.path.realpath(prefix))
    assert "Ciaobot-managed adapter" in ai.describe(ai.CLAUDE_ADAPTER, "ready")


def test_a_relative_or_writable_machine_wide_adapter_is_refused(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pin = ai.ADAPTERS[ai.CLAUDE_ADAPTER].version
    pkg = _global_install(tmp_path, monkeypatch, version=pin, writable=True)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert ai.in_use_path(ai.CLAUDE_ADAPTER) is None

    # The same real package, reached through a relative path from the working directory.
    pkg.chmod(0o755)
    assert ai.detect(ai.CLAUDE_ADAPTER) == "ready"
    monkeypatch.chdir(tmp_path / "global-npm")
    base = ai.resolve_tool
    monkeypatch.setattr(
        ai, "resolve_tool", lambda cmd: "bin/claude-agent-acp" if cmd == ai.CLAUDE_ADAPTER else base(cmd)
    )
    assert ai.detect(ai.CLAUDE_ADAPTER) == "adapter_missing"
    assert ai.in_use_path(ai.CLAUDE_ADAPTER) is None


def test_an_attempt_that_raises_unexpectedly_never_reads_as_ready_on_a_later_call(
    prefix: Path, npm: FakeNpm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(name: str) -> None:
        raise RuntimeError("a bug, not an install error")

    monkeypatch.setattr(ai, "install", unexpected)
    with pytest.raises(RuntimeError, match="a bug"):
        ai.install_at_startup(ai.CLAUDE_ADAPTER)

    later = ai.install_at_startup(ai.CLAUDE_ADAPTER)
    assert later != "", "nothing recorded must not read as ready"
    assert "is not installed for Ciaobot yet" in later
