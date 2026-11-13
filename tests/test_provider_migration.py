import copy
import ast
import hashlib
import json
import shutil
import sys

import pytest

from ciao.provider_migration import snapshot_id, valid_operation_id, validate_snapshot


def sample_snapshot(tmp_path):
    return {
        "schema": 1,
        "operation": {"id": "20261112T101500Z-1.2.3", "phase": "staged", "to_version": "1.2.3", "wheel_sha256": "a" * 64, "env_python": str(tmp_path / "python")},
        "default_providers": [],
        "installer": {"state": "unavailable", "package": "", "npm_argv": [], "reason": "not needed"},
        "capabilities": {"managed_resolver": 0, "operation_schema": 0, "installed_target": "unknown", "candidate_target": "unknown", "candidate_running": "unknown"},
        "providers": [{"provider": p, "selected": False, "state": "not_default", "action": "none", "installed": None, "candidate": None, "tested": None, "identity": None, "reason": ""} for p in ("opencode", "claude")],
        "can_apply": True, "reason": "",
    }


def test_validate_snapshot_rejects_nested_extra_missing_wrong_types_and_inconsistent_claims(tmp_path):
    snapshot = sample_snapshot(tmp_path)
    assert validate_snapshot(snapshot) == snapshot
    mutations = [
        ((), "extra", 1), (("operation",), "extra", 1),
        (("installer",), "extra", 1), (("capabilities",), "extra", 1),
        (("providers", 0), "extra", 1),
        ((), "schema", True), ((), "can_apply", False),
        (("operation",), "wheel_sha256", "A" * 64),
        (("capabilities",), "managed_resolver", True),
        (("providers", 0), "selected", True),
        (("providers", 0), "action", "manual"),
        (("providers", 0), "installed", "1.2"),
        ((), "default_providers", ["claude", "claude"]),
    ]
    for path, key, value in mutations:
        invalid = copy.deepcopy(snapshot)
        obj = invalid
        for part in path:
            obj = obj[part]
        obj[key] = value
        with pytest.raises(ValueError):
            validate_snapshot(invalid)
    for path in [(), ("operation",), ("installer",), ("capabilities",), ("providers", 0)]:
        invalid = copy.deepcopy(snapshot)
        obj = invalid
        for part in path:
            obj = obj[part]
        del obj[next(iter(obj))]
        with pytest.raises(ValueError):
            validate_snapshot(invalid)
    for providers in ([snapshot["providers"][0], snapshot["providers"][0]], list(reversed(snapshot["providers"]))):
        invalid = copy.deepcopy(snapshot)
        invalid["providers"] = providers
        with pytest.raises(ValueError):
            validate_snapshot(invalid)


def test_snapshot_hash_includes_operation_and_file_identity(tmp_path):
    snapshot = sample_snapshot(tmp_path)
    original = snapshot_id(snapshot)
    for changes in ({"wheel_sha256": "b" * 64}, {"id": "20261112T101501Z-1.2.3"}, {"id": "20261112T101500Z-1.2.4", "to_version": "1.2.4"}, {"env_python": str(tmp_path / "other")}):
        changed = copy.deepcopy(snapshot)
        changed["operation"].update(changes)
        assert snapshot_id(changed) != original
    changed = copy.deepcopy(snapshot)
    changed["operation"]["phase"] = "applying"
    with pytest.raises(ValueError):
        snapshot_id(changed)
    changed = copy.deepcopy(snapshot)
    changed["default_providers"] = ["claude"]
    changed["providers"][1].update(selected=True, state="absent", action="none")
    assert snapshot_id(changed) != original


def test_staged_venv_python_symlink_and_outside_entrypoint(tmp_path, monkeypatch):
    from ciao.engine_update import Operation
    from ciao.provider_migration import _validate_operation
    from ciao import install_receipt

    stage = tmp_path / "1.2.3"
    python = stage / "tool" / "ciaobot" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    external = tmp_path / "python"
    external.write_bytes(b"python")
    python.symlink_to(external)
    wheel = stage / "package.whl"
    wheel.write_bytes(b"wheel")
    op = Operation("20261112T101500Z-1.2.3", "staged", "1.0.0", "1.2.3", "", "", stage_dir=str(stage), wheel=str(wheel), wheel_sha256=hashlib.sha256(b"wheel").hexdigest(), env_python=str(python))
    monkeypatch.setattr(install_receipt, "read_receipt", lambda *args, **kwargs: pytest.fail("must not read the active receipt"))
    _validate_operation(op, tmp_path)
    op.env_python = str(external)
    with pytest.raises(ValueError, match="invalid staged operation"):
        _validate_operation(op, tmp_path)


def test_shared_npm_symlink_snapshot_and_retarget(tmp_path):
    from ciao.provider_migration import _file_identity

    first, second, link = (tmp_path / name for name in ("one", "two", "opencode"))
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    link.symlink_to(first.name)
    before = _file_identity(link)
    assert before["logical_path"] == str(link)
    assert before["links"][0]["link_text"] == first.name
    link.unlink()
    link.symlink_to(second.name)
    assert _file_identity(link) != before


def test_probe_never_captures_unbounded_output():
    from ciao.provider_migration import _run_bounded

    for size, cap in ((4097, 4096), (16385, 16384)):
        with pytest.raises(ValueError):
            _run_bounded([sys.executable, "-c", f"print('x'*{size})"], 3, cap, 4096)
    with pytest.raises(ValueError):
        _run_bounded([sys.executable, "-c", "import sys; sys.stderr.write('x'*4097)"], 3, 4096, 4096)
    with pytest.raises(ValueError):
        _run_bounded([sys.executable, "-c", "import time; time.sleep(2)"], 0.1, 4096, 4096)


@pytest.mark.parametrize("stream,size,stdout_cap", [(1, 4097, 4096), (2, 4097, 4096), (1, 16385, 16384)])
def test_overflow_kills_running_owned_child(monkeypatch, stream, size, stdout_cap):
    import time
    from ciao import provider_migration as migration
    popen = migration.subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    script = f"import os,time; os.write({stream}, b'x' * {size}); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", script], 8, stdout_cap, 4096)
    assert time.monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None


def test_running_overflow_test_kills_capture_then_wait_mutant(monkeypatch):
    import subprocess
    from ciao import provider_migration as migration
    def capture_then_wait(argv, timeout, stdout_cap, stderr_cap):
        try:
            subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
        except subprocess.TimeoutExpired:
            raise ValueError("probe failed") from None
    monkeypatch.setattr(migration, "_run_bounded", capture_then_wait)
    with pytest.raises(AssertionError):
        test_overflow_kills_running_owned_child(monkeypatch, 1, 4097, 4096)


def test_probe_deadline_kills_running_owned_child(monkeypatch):
    import time
    from ciao import provider_migration as migration
    popen = migration.subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    start = time.monotonic()
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], 0.2, 4096, 4096)
    assert time.monotonic() - start < 5
    assert len(children) == 1 and children[0].poll() is not None


def test_probe_does_not_forward_credentials(monkeypatch):
    from ciao.provider_migration import _run_bounded
    monkeypatch.setenv("FAKE_PROVIDER_SECRET", "test-only-secret")
    output = _run_bounded([sys.executable, "-c", "import os; print(os.getenv('FAKE_PROVIDER_SECRET', 'absent'))"], 3, 4096, 4096)
    assert output.strip() == b"absent"


def test_installer_capability_is_shared_read_only(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao import tool_path

    npm = tmp_path / "npm"
    npm.write_bytes(b"npm")
    npm.chmod(0o755)
    monkeypatch.setattr(migration.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(migration.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(npm)])
    assert migration.installer_capability(state_dir=tmp_path / "state") == {"state": "ready", "package": "@opencode/cli-darwin-arm64", "npm_argv": [str(npm)], "reason": ""}
    assert not (tmp_path / "state").exists()
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [])
    assert migration.installer_capability()["state"] == "unavailable"
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: ["relative"])
    assert migration.installer_capability()["state"] == "unknown"


@pytest.mark.parametrize("system,machine,libc,package", [
    ("Darwin", "x86_64", "", "@opencode/cli-darwin-x64-baseline"),
    ("Darwin", "aarch64", "", "@opencode/cli-darwin-arm64"),
    ("Linux", "aarch64", "musl", "@opencode/cli-linux-arm64-musl"),
    ("Linux", "amd64", "musl", "@opencode/cli-linux-x64-baseline-musl"),
    ("Linux", "amd64", "glibc", "@opencode/cli-linux-x64-baseline"),
    ("Linux", "arm64", "glibc", "@opencode/cli-linux-arm64"),
])
def test_installer_platform_packages(tmp_path, monkeypatch, system, machine, libc, package):
    from ciao import provider_migration as migration, tool_path
    npm = tmp_path / "npm"
    npm.write_bytes(b"npm")
    npm.chmod(0o755)
    monkeypatch.setattr(migration.platform, "system", lambda: system)
    monkeypatch.setattr(migration.platform, "machine", lambda: machine)
    monkeypatch.setattr(migration.platform, "libc_ver", lambda: (libc, ""))
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(npm)])
    assert migration.installer_capability()["package"] == package
    npm.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(npm)])
    assert migration.installer_capability()["state"] == "unknown"


@pytest.mark.parametrize("machine,package", [("arm64", "@opencode/cli-windows-arm64"), ("amd64", "@opencode/cli-windows-x64-baseline")])
def test_installer_windows_unwrapped_npm(tmp_path, monkeypatch, machine, package):
    from ciao import provider_migration as migration, tool_path
    node, script = tmp_path / "node.exe", tmp_path / "npm-cli.js"
    node.write_bytes(b"node")
    script.write_bytes(b"npm")
    monkeypatch.setattr(migration.platform, "system", lambda: "Windows")
    monkeypatch.setattr(migration.platform, "machine", lambda: "amd64" if machine == "arm64" else "arm64")
    monkeypatch.setattr(migration.sysconfig, "get_platform", lambda: "win-arm64" if machine == "arm64" else "win-amd64")
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(node), str(script)])
    assert migration.installer_capability()["package"] == package
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(node)])
    assert migration.installer_capability()["state"] == "unknown"


def test_installer_libc_fallbacks(tmp_path, monkeypatch):
    from ciao import provider_migration as migration, tool_path
    npm, ldd = tmp_path / "npm", tmp_path / "ldd"
    npm.write_bytes(b"npm")
    ldd.write_bytes(b"ldd")
    npm.chmod(0o755)
    monkeypatch.setattr(migration.platform, "system", lambda: "Linux")
    monkeypatch.setattr(migration.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(migration.platform, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(tool_path, "resolve_command", lambda _: [str(npm)])
    monkeypatch.setattr(tool_path, "resolve_tool", lambda _: str(ldd))
    monkeypatch.setattr(migration.os, "confstr", lambda _: "glibc 2.39", raising=False)
    assert migration.installer_capability()["package"].endswith("x64-baseline")
    monkeypatch.setattr(migration.os, "confstr", lambda _: "", raising=False)
    monkeypatch.setattr(migration, "_run_bounded", lambda *args, **kwargs: b"musl libc")
    assert migration.installer_capability()["package"].endswith("x64-baseline-musl")
    monkeypatch.setattr(migration, "_run_bounded", lambda *args, **kwargs: b"unknown")
    assert migration.installer_capability()["state"] == "unknown"
    def broken_libc():
        raise OSError("private details")
    monkeypatch.setattr(migration.platform, "libc_ver", broken_libc)
    assert migration.installer_capability()["state"] == "unknown"


def test_probe_checks_installed_and_candidate(monkeypatch):
    from ciao import provider_migration as migration

    payload = {"engine_version": "1.2.3", "record": {"schema": 1, "ciaobot": "1.2.3", "recorded": "2026-11-12", "providers": {"opencode": {"status": "tested", "version": "2.2.5", "package": "@opencode/cli"}, "claude": {"status": "tested", "version": "2.1.318", "source": "bundled"}}}, "installed_error": "old", "candidate_error": None, "bundled_claude": None, "managed_resolver_version": 0, "operation_schema_version": 0, "operation_schema_fields": []}
    calls = []
    def fake(argv, timeout, stdout, stderr):
        calls.append((argv, timeout, stdout, stderr))
        return json.dumps(payload).encode()
    monkeypatch.setattr(migration, "_run_bounded", fake)
    result = migration._target_probe("/python", "1.2.3", "2.0.15")
    assert result["installed_error"] == "old"
    assert result["candidate_error"] is None
    assert calls[0][0][-1] == "2.0.15"
    assert calls[0][1:] == (15, 16384, 4096)
    payload["engine_version"] = "1.2.4"
    with pytest.raises(ValueError):
        migration._target_probe("/python", "1.2.3", "2.0.15")
    payload["engine_version"] = "1.2.3"
    payload["record"]["providers"]["opencode"]["package"] = "wrong"
    with pytest.raises(ValueError):
        migration._target_probe("/python", "1.2.3", "2.0.15")
    payload["record"]["providers"]["opencode"]["package"] = "@opencode/cli"
    payload["record"]["ciaobot"] = "1.2.4"
    with pytest.raises(ValueError):
        migration._target_probe("/python", "1.2.3", "2.0.15")
    payload["record"]["ciaobot"] = "1.2.3"
    del payload["record"]
    with pytest.raises(ValueError):
        migration._target_probe("/python", "1.2.3", "2.0.15")


def test_real_target_script_checks_distinct_versions(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from pathlib import Path

    source = Path(migration.__file__).parent
    package = tmp_path / "ciao"
    (package / "providers").mkdir(parents=True)
    (package / "stock").mkdir()
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (package / "providers" / "__init__.py").write_text("")
    (package / "stock" / "__init__.py").write_text("")
    shutil.copyfile(source / "provider_versions.py", package / "provider_versions.py")
    original = (source / "providers" / "opencode.py").read_text()
    tree = ast.parse(original)
    nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in {"_version_number", "_server_version_error"}) or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OPENCODE_V2_REQUIRED" for t in node.targets))]
    assert len(nodes) == 3
    checker = "import re\nfrom collections.abc import Mapping\nMANAGED_OPENCODE_RESOLVER_VERSION = 1\n" + "\n".join(ast.get_source_segment(original, node) for node in nodes)
    (package / "providers" / "opencode.py").write_text(checker)
    (package / "providers" / "claude.py").write_text("def get_bundled_claude_path(): return None\n")
    fields = list(migration._OPERATION_FIELDS)
    (package / "engine_update.py").write_text("from dataclasses import dataclass\nPROVIDER_OPERATION_SCHEMA_VERSION = 1\n@dataclass\nclass Operation:\n" + "".join(f"    {name}: str = ''\n" for name in fields))
    record = {"schema": 1, "ciaobot": "1.2.3", "recorded": "2026-11-12", "providers": {"opencode": {"status": "tested", "version": "2.2.5", "package": "@opencode/cli"}, "claude": {"status": "tested", "version": "2.1.318", "source": "bundled"}}}
    (package / "stock" / "provider-versions.json").write_text(json.dumps(record))
    run = migration._run_bounded
    observed = []
    def isolated(argv, timeout, stdout, stderr):
        observed.append(argv[:])
        bootstrap = "import sys; sys.path.insert(0, " + repr(str(tmp_path)) + ")\n"
        return run([sys.executable, "-I", "-c", bootstrap + argv[3], argv[4]], timeout, stdout, stderr)
    monkeypatch.setattr(migration, "_run_bounded", isolated)
    result = migration._target_probe(sys.executable, "1.2.3", "2.0.15")
    assert observed[0][1:3] == ["-I", "-c"] and observed[0][-1] == "2.0.15"
    assert isinstance(result["installed_error"], str)
    assert result["candidate_error"] is None
    assert result["managed_resolver_version"] == result["operation_schema_version"] == 1
    assert result["operation_schema_fields"] == fields
    mutant = migration._TARGET_SCRIPT.replace('"candidate_error": opencode._server_version_error({"version": candidate})', '"candidate_error": opencode._server_version_error({"version": sys.argv[1]})')
    assert mutant != migration._TARGET_SCRIPT
    monkeypatch.setattr(migration, "_TARGET_SCRIPT", mutant)
    mutated = migration._target_probe(sys.executable, "1.2.3", "2.0.15")
    with pytest.raises(AssertionError):
        assert mutated["candidate_error"] is None


def test_unknown_selection_differs_from_known_empty(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ciao import provider_migration as migration

    op = SimpleNamespace(env_python=str(tmp_path / "python"), to_version="1.2.3")
    operation = sample_snapshot(tmp_path)["operation"]
    monkeypatch.setattr(migration, "_validate_operation", lambda *_: operation)
    monkeypatch.setattr(migration, "_target_probe", lambda *_: (_ for _ in ()).throw(ValueError()))
    unknown = migration.provider_snapshot(op, None, state_dir=tmp_path)
    empty = migration.provider_snapshot(op, frozenset(), state_dir=tmp_path)
    assert not unknown["can_apply"]
    assert empty["can_apply"]
    assert unknown["providers"][0]["selected"] is None
    assert empty["providers"][0]["selected"] is False


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    from ciao import provider_migration as migration, tool_path
    from ciao.engine_update import Operation
    from ciao.providers.opencode import _server_version_error

    stage = tmp_path / "1.2.3"
    python = stage / "tool" / "ciaobot" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    wheel = stage / "release.whl"
    wheel.write_bytes(b"wheel")
    binary = tmp_path / "opencode"
    binary.write_bytes(b"executable")
    claude = tmp_path / "claude"
    claude.write_bytes(b"executable")
    npm = tmp_path / "npm"
    npm.write_bytes(b"npm fixture")
    op = Operation("20261112T101500Z-1.2.3", "staged", "1.0.0", "1.2.3", "", "", stage_dir=str(stage), wheel=str(wheel), wheel_sha256=hashlib.sha256(b"wheel").hexdigest(), env_python=str(python))
    state = {"opencode": "2.0.15", "claude": "2.1.318", "bundle": None, "bundle_version": None, "resolver": 1, "schema": 1, "fields": list(migration._OPERATION_FIELDS), "candidate": "2.2.5", "candidate_error": None, "force_installed_reject": False}
    def resolve(name):
        return str(binary if name == "opencode" else claude) if state[name] is not None else None
    def run(argv, timeout, stdout, stderr):
        if argv[0] == str(python):
            installed = argv[-1]
            record = {"schema": 1, "ciaobot": "1.2.3", "recorded": "2026-11-12", "providers": {"opencode": {"status": "tested", "version": state["candidate"], "package": "@opencode/cli"}, "claude": {"status": "tested", "version": "2.1.318", "source": "bundled"}}}
            return json.dumps({"engine_version": "1.2.3", "record": record, "installed_error": None if state.get("force_installed_accept") else "incompatible" if state["force_installed_reject"] else _server_version_error({"version": installed}), "candidate_error": state["candidate_error"], "bundled_claude": state["bundle"], "managed_resolver_version": state["resolver"], "operation_schema_version": state["schema"], "operation_schema_fields": state["fields"]}).encode()
        version = state["opencode"] if argv[0] == str(binary) else state["bundle_version"] if argv[0] == state["bundle"] else state["claude"]
        if version == "broken":
            raise ValueError("broken")
        return str(version).encode()
    monkeypatch.delenv("CIAO_OPENCODE_BIN", raising=False)
    monkeypatch.setattr(tool_path, "resolve_tool", resolve)
    monkeypatch.setattr(migration, "_run_bounded", run)
    monkeypatch.setattr(migration, "installer_capability", lambda **_: {"state": "ready", "package": "@opencode/cli-darwin-arm64", "npm_argv": [str(npm)], "reason": ""})
    return op, state, migration, binary, claude


@pytest.mark.parametrize("package,names", [
    ("attacker-package", ["npm"]),
    ("@opencode/cli-linux-x64", ["npm"]),
    ("@opencode/cli-darwin-arm64", ["sh"]),
    ("@opencode/cli-darwin-arm64", ["npm", "payload.js"]),
    ("@opencode/cli-windows-arm64", ["npm.cmd"]),
    ("@opencode/cli-windows-arm64", ["node.exe", "payload.js"]),
    ("@opencode/cli-windows-arm64", ["cmd.exe", "npm-cli.js"]),
    ("@opencode/cli-darwin-arm64", ["node.exe", "npm-cli.js"]),
    ("@opencode/cli-windows-arm64", ["npm"]),
])
def test_validator_rejects_unsupported_ready_installer(preflight, tmp_path, package, names):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    snapshot["installer"].update(package=package, npm_argv=[str(tmp_path / name) for name in names])
    with pytest.raises(ValueError):
        migration.validate_snapshot(snapshot)


@pytest.mark.parametrize("package,names", [
    ("@opencode/cli-darwin-arm64", ["npm"]),
    ("@opencode/cli-darwin-x64-baseline", ["npm"]),
    ("@opencode/cli-linux-arm64", ["npm"]),
    ("@opencode/cli-linux-arm64-musl", ["npm"]),
    ("@opencode/cli-linux-x64-baseline", ["npm"]),
    ("@opencode/cli-linux-x64-baseline-musl", ["npm"]),
    ("@opencode/cli-windows-arm64", ["node.exe", "npm-cli.js"]),
    ("@opencode/cli-windows-x64-baseline", ["NODE.EXE", "NPM-CLI.JS"]),
])
def test_validator_accepts_supported_ready_installer(preflight, tmp_path, package, names):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    snapshot["installer"].update(package=package, npm_argv=[str(tmp_path / name) for name in names])
    assert migration.validate_snapshot(snapshot)["can_apply"] is True


@pytest.mark.parametrize("candidate", ["2.2.5", "4.0.0"])
def test_future_target_acceptance_does_not_approve_opencode_three(preflight, tmp_path, monkeypatch, candidate):
    op, state, migration, _, _ = preflight
    from ciao.providers import opencode
    state.update(opencode="3.0.0", candidate=candidate, force_installed_accept=True)
    monkeypatch.setattr(opencode, "_server_version_error", lambda _: None)
    monkeypatch.setattr(migration, "installer_capability", lambda **_: pytest.fail("manual-only major must not select an installer"))
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    assert (snapshot["providers"][0]["state"], snapshot["providers"][0]["action"]) == ("blocked", "manual")
    assert snapshot["can_apply"] is False


@pytest.mark.parametrize("claim", ["compatible", "advisory"])
def test_validator_rejects_future_accepted_major(preflight, tmp_path, claim):
    op, state, migration, _, _ = preflight
    state["opencode"] = "2.2.5"
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    assert snapshot["can_apply"] is True
    for installed in ("3.0.0", "4.2.0"):
        forged = copy.deepcopy(snapshot)
        forged["providers"][0].update(state=claim, action="none", installed=installed)
        with pytest.raises(ValueError):
            migration.validate_snapshot(forged)


def test_selected_opencode_matrix(preflight, tmp_path, monkeypatch):
    op, state, migration, binary, _ = preflight
    for version, expected in [("1.19.5", "required"), ("2.0.15", "required"), ("2.0.16", "advisory"), ("2.2.5", "compatible"), ("3.0.0", "blocked"), ("broken", "unknown"), (None, "absent")]:
        state["opencode"] = version
        snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
        assert snapshot["providers"][0]["state"] == expected
    state["opencode"] = "1.19.5"
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(tmp_path / "missing"))
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    monkeypatch.delenv("CIAO_OPENCODE_BIN")
    state["candidate_error"] = "unsupported"
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    state["candidate_error"] = None
    state["candidate"] = "3.0.0"
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    state["candidate"] = "2.2.5"
    from ciao.providers import opencode
    monkeypatch.setattr(opencode, "_server_version_error", lambda _: "unsupported")
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"


@pytest.mark.parametrize("installed", ["2.5.0", "2.9.0", "2.2.5"])
def test_no_automatic_downgrade_or_equal_reinstall(preflight, tmp_path, monkeypatch, installed):
    op, state, migration, _, _ = preflight
    state["opencode"] = installed
    state["force_installed_reject"] = True
    monkeypatch.setattr(migration, "installer_capability", lambda **_: pytest.fail("installer not needed"))
    row = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]
    assert (row["state"], row["action"]) == ("blocked", "manual")


@pytest.mark.parametrize("state", ["compatible", "advisory"])
def test_validator_rejects_installed_checker_contradiction(preflight, tmp_path, state):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    snapshot["providers"][0].update(state=state, action="none")
    with pytest.raises(ValueError):
        migration.validate_snapshot(snapshot)


@pytest.mark.parametrize("installed", ["2.9.0", "2.2.5"])
def test_validator_rejects_required_downgrade_and_equal(preflight, tmp_path, monkeypatch, installed):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    snapshot["providers"][0]["installed"] = installed
    with pytest.raises(ValueError):
        migration.validate_snapshot(snapshot)
    # Mutate only the shared version comparison: the rejection assertion must fail.
    monkeypatch.setattr(migration, "_version_is_newer", lambda *_: True)
    def rejects():
        try:
            migration.validate_snapshot(snapshot)
        except ValueError:
            return True
        return False
    with pytest.raises(AssertionError):
        assert rejects()


@pytest.mark.parametrize("installed,candidate", [("3.0.0", "4.0.0"), ("4.2.0", "5.0.0")])
def test_validator_rejects_required_future_major(preflight, tmp_path, monkeypatch, installed, candidate):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    snapshot["providers"][0].update(installed=installed, candidate=candidate, tested=candidate)
    def rejects():
        try:
            migration.validate_snapshot(snapshot)
        except ValueError:
            return True
        return False
    assert rejects()
    monkeypatch.setattr(migration, "_managed_major_eligible", lambda _: True)
    with pytest.raises(AssertionError):
        assert rejects()


@pytest.mark.parametrize("state", ["compatible", "advisory"])
def test_validator_enforces_claude_minimum(preflight, tmp_path, monkeypatch, state):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"claude"}), state_dir=tmp_path)
    row = snapshot["providers"][1]
    row.update(state=state, action="none", installed="2.1.277")
    assert migration.validate_snapshot(snapshot)["can_apply"] is True
    row["installed"] = "2.1.276"
    def rejects():
        try:
            migration.validate_snapshot(snapshot)
        except ValueError:
            return True
        return False
    assert rejects()
    monkeypatch.setattr(migration, "_claude_version_supported", lambda _: True)
    with pytest.raises(AssertionError):
        assert rejects()


@pytest.mark.parametrize("kind", ["wheel", "cli"])
def test_fifo_refusal_is_bounded_without_probing(tmp_path, kind):
    import os
    import subprocess
    from pathlib import Path
    if os.name == "nt":
        assert not hasattr(os, "mkfifo")
        return
    root = Path(__file__).resolve().parents[1]
    script = r'''
import hashlib, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ciao import provider_migration as migration, tool_path
from ciao.engine_update import Operation
root, kind, mutant = Path(sys.argv[2]), sys.argv[3], sys.argv[4] == 'mutant'
stage = root / '1.2.3'
stage.mkdir(parents=True)
python = stage / 'tool' / 'ciaobot' / 'bin' / 'python'
python.parent.mkdir(parents=True)
python.write_bytes(b'python')
wheel = stage / 'release.whl'
fifo = stage / ('release.whl' if kind == 'wheel' else 'opencode')
if kind == 'cli': wheel.write_bytes(b'wheel')
os.mkfifo(fifo)
if mutant: migration.os.O_NONBLOCK = 0
op = Operation('20261112T101500Z-1.2.3', 'staged', '1.0.0', '1.2.3', '', '', stage_dir=str(stage), wheel=str(wheel), wheel_sha256=hashlib.sha256(b'wheel').hexdigest(), env_python=str(python))
marker = root / 'probe-ran'
def forbidden(*args, **kwargs):
    marker.write_text('unexpected subprocess probe')
    raise AssertionError('unexpected subprocess probe')
migration._run_bounded = forbidden
if kind == 'wheel':
    try: migration.provider_snapshot(op, frozenset(), state_dir=root)
    except ValueError: pass
    else: raise AssertionError('FIFO wheel accepted')
else:
    tool_path.resolve_tool = lambda name: str(fifo)
    migration._target_probe = lambda *args: (_ for _ in ()).throw(ValueError('no target'))
    snapshot = migration.provider_snapshot(op, frozenset({'opencode'}), state_dir=root)
    assert snapshot['providers'][0]['state'] == 'unknown'
assert not marker.exists()
print('refused')
'''
    for mutant in (False, True):
        state = tmp_path / (kind + ("-mutant" if mutant else "-normal"))
        args = [sys.executable, "-I", "-c", script, str(root), str(state), kind, "mutant" if mutant else "normal"]
        if mutant:
            with pytest.raises(subprocess.TimeoutExpired):
                subprocess.run(args, capture_output=True, timeout=4, check=True)
        else:
            result = subprocess.run(args, capture_output=True, timeout=4, check=True)
            assert result.stdout.strip() == b"refused"


def test_nondefault_broken_provider_never_blocks_or_installs(preflight, tmp_path, monkeypatch):
    op, state, migration, _, _ = preflight
    state["opencode"] = "broken"
    monkeypatch.setattr(migration, "installer_capability", lambda **_: pytest.fail("must not inspect installer"))
    snapshot = migration.provider_snapshot(op, frozenset({"claude"}), state_dir=tmp_path)
    assert snapshot["can_apply"]
    assert snapshot["providers"][0]["state"] == "not_default"
    assert "chats" in snapshot["providers"][0]["reason"]


def test_claude_bundle_precedence_and_fallback(preflight, tmp_path):
    op, state, migration, _, claude = preflight
    snapshot = lambda: migration.provider_snapshot(op, frozenset({"claude"}), state_dir=tmp_path)["providers"][1]
    assert snapshot()["state"] == "compatible"
    bundle = tmp_path / "bundled-claude"
    bundle.write_bytes(b"bundle")
    state["bundle"] = str(bundle)
    state["bundle_version"] = "broken"
    assert snapshot()["state"] == "unknown"
    state["bundle"] = None
    state["claude"] = None
    assert snapshot()["state"] == "absent"
    state["claude"] = "2.1.276"
    assert snapshot()["state"] == "blocked"
    state["claude"] = "broken"
    row = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][1]
    assert row["state"] == "not_default"


def test_target_schema_marker_and_all_fields_required(preflight, tmp_path):
    op, state, migration, _, _ = preflight
    state["schema"] = 0
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    state["schema"] = 1
    state["fields"] = state["fields"][:-1]
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "blocked"
    state["fields"] = list(migration._OPERATION_FIELDS)
    assert migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]["state"] == "required"

    # Execute the real fixed script against synthetic target modules, not a
    # fabricated output: dataclass field discovery must reflect the target.
    import subprocess
    harness = r'''
import dataclasses, json, sys, types
def module(name):
    m = types.ModuleType(name); sys.modules[name] = m; return m
ciao = module("ciao"); ciao.__version__ = "1.2.3"; ciao.__path__ = []
providers = module("ciao.providers"); providers.__path__ = []; ciao.providers = providers
versions = module("ciao.provider_versions"); ciao.provider_versions = versions
versions.load_record = lambda: {"providers": {"opencode": {"version": "2.2.5"}}}
oc = module("ciao.providers.opencode"); providers.opencode = oc
oc._server_version_error = lambda payload: None
oc.MANAGED_OPENCODE_RESOLVER_VERSION = 1
cl = module("ciao.providers.claude"); providers.claude = cl
cl.get_bundled_claude_path = lambda: None
up = module("ciao.engine_update"); ciao.engine_update = up
up.PROVIDER_OPERATION_SCHEMA_VERSION = 1
names = ["provider_error", "provider_snapshot_id", "provider_snapshot_json", "provider_state", "provider_version"]
if sys.argv[1] == "missing": names.pop()
up.Operation = dataclasses.make_dataclass("Operation", [(n, str, dataclasses.field(default="")) for n in names])
up.read_operation = lambda data: up.Operation(**{n: data.get(n, "") for n in names})
assert all(getattr(up.read_operation({n: "retained" for n in names}), n) == "retained" for n in names)
sys.argv = ["-c", "2.0.15"]
exec(sys.argv_script)
'''
    for case, expected in (("missing", 4), ("complete", 5)):
        script = "import sys\nsys.argv_script = " + repr(migration._TARGET_SCRIPT) + "\n" + harness
        output = subprocess.run([sys.executable, "-c", script, case], capture_output=True, check=True, timeout=3)
        observed = json.loads(output.stdout)
        assert len(observed["operation_schema_fields"]) == expected
        state["fields"] = observed["operation_schema_fields"]
        state["schema"] = observed["operation_schema_version"]
        state["resolver"] = observed["managed_resolver_version"]
        row = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)["providers"][0]
        assert row["state"] == ("blocked" if case == "missing" else "required")


def test_required_snapshot_validation_at_nested_seams(preflight, tmp_path):
    op, _, migration, _, _ = preflight
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    original = snapshot_id(snapshot)
    changes = [
        (("providers", 0, "identity"), "extra", 1),
        (("providers", 0, "identity", "target"), "inode", True),
        (("providers", 0, "identity", "target"), "size", 0),
        (("providers", 0, "identity", "target"), "sha256", "z" * 64),
        (("providers", 0, "identity", "target"), "path", "relative"),
        (("providers", 0), "action", "none"),
        (("providers", 0), "candidate", "2.2.6"),
        (("capabilities",), "operation_schema", 0),
        (("capabilities",), "candidate_running", "reject"),
        ((), "can_apply", False),
    ]
    for path, key, value in changes:
        invalid = copy.deepcopy(snapshot)
        current = invalid
        for part in path:
            current = current[part]
        current[key] = value
        with pytest.raises(ValueError):
            validate_snapshot(invalid)
    changed = copy.deepcopy(snapshot)
    changed["providers"][0]["identity"]["target"]["sha256"] = "f" * 64
    assert snapshot_id(changed) != original
    changed = copy.deepcopy(snapshot)
    changed["providers"][0].update(candidate="2.2.6", tested="2.2.6")
    assert snapshot_id(changed) != original
    changed = copy.deepcopy(snapshot)
    changed["providers"][0]["identity"]["links"] = [{"path": str(tmp_path / "link"), "device": 0, "inode": 0, "mtime_ns": 0, "link_text": "target"}]
    assert snapshot_id(changed) != original
    for key, bad in (("path", "relative"), ("device", True), ("inode", -1), ("mtime_ns", None), ("link_text", ""), ("extra", 1)):
        invalid = copy.deepcopy(changed)
        invalid["providers"][0]["identity"]["links"][0][key] = bad
        with pytest.raises(ValueError):
            validate_snapshot(invalid)
    for path in (("providers", 0, "identity"), ("providers", 0, "identity", "target"), ("providers", 0, "identity", "links", 0)):
        invalid = copy.deepcopy(changed)
        current = invalid
        for part in path:
            current = current[part]
        del current[next(iter(current))]
        with pytest.raises(ValueError):
            validate_snapshot(invalid)
    invalid = copy.deepcopy(changed)
    invalid["providers"][0]["identity"]["links"] *= 41
    with pytest.raises(ValueError):
        validate_snapshot(invalid)


def test_invalid_operation_never_yields_a_snapshot(preflight, tmp_path):
    op, _, migration, _, _ = preflight
    for field, bad in (("phase", "applying"), ("wheel_sha256", "a" * 64), ("stage_dir", str(tmp_path / "elsewhere")), ("id", "../../bad")):
        original = getattr(op, field)
        setattr(op, field, bad)
        with pytest.raises(ValueError, match="invalid staged operation"):
            migration.provider_snapshot(op, frozenset(), state_dir=tmp_path)
        setattr(op, field, original)
    original_wheel = op.wheel
    linked_wheel = tmp_path / "1.2.3" / "linked.whl"
    linked_wheel.symlink_to("release.whl")
    op.wheel = str(linked_wheel)
    with pytest.raises(ValueError, match="invalid staged operation"):
        migration.provider_snapshot(op, frozenset(), state_dir=tmp_path)
    op.wheel = original_wheel


def test_missing_target_record_does_not_claim_absent_provider_is_compatible(preflight, tmp_path, monkeypatch):
    op, state, migration, _, _ = preflight
    state["opencode"] = None
    monkeypatch.setattr(migration, "_target_probe", lambda *_: (_ for _ in ()).throw(ValueError("bad record")))
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    assert snapshot["providers"][0]["state"] == "unknown"
    assert not snapshot["can_apply"]


def test_operation_change_during_probe_is_refused(preflight, tmp_path, monkeypatch):
    op, _, migration, _, _ = preflight
    target_probe = migration._target_probe
    def changed(*args):
        result = target_probe(*args)
        op.phase = "applying"
        return result
    monkeypatch.setattr(migration, "_target_probe", changed)
    with pytest.raises(ValueError, match="invalid staged operation"):
        migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)


@pytest.mark.parametrize("field", ["id", "phase", "to_version", "wheel_sha256", "env_python"])
@pytest.mark.parametrize("probe", ["target", "claude"])
def test_every_operation_identity_change_during_probe_is_refused(preflight, tmp_path, monkeypatch, field, probe):
    from pathlib import Path
    op, _, migration, _, _ = preflight
    def mutate():
        if field == "id":
            op.id = "20261112T101501Z-1.2.3"
        elif field == "phase":
            op.phase = "applying"
        elif field == "wheel_sha256":
            Path(op.wheel).write_bytes(b"changed wheel")
            op.wheel_sha256 = hashlib.sha256(b"changed wheel").hexdigest()
        elif field == "env_python":
            python = Path(op.env_python)
            env = python.parent.parent
            new_env = env.with_name("another-env")
            env.rename(new_env)
            op.env_python = str(new_env / python.relative_to(env))
        else:
            old = Path(op.stage_dir)
            new = old.with_name("1.2.4")
            old.rename(new)
            op.stage_dir = str(new)
            op.wheel = str(new / Path(op.wheel).relative_to(old))
            op.env_python = str(new / Path(op.env_python).relative_to(old))
            op.to_version = "1.2.4"
            op.id = "20261112T101500Z-1.2.4"
        if field != "phase":
            migration._validate_operation(op, tmp_path)
    if probe == "target":
        original = migration._target_probe
        def changed(*args):
            result = original(*args)
            mutate()
            return result
        monkeypatch.setattr(migration, "_target_probe", changed)
    else:
        original_cli = migration._cli_version
        def changed_cli(*args):
            result = original_cli(*args)
            mutate()
            return result
        monkeypatch.setattr(migration, "_cli_version", changed_cli)
    with pytest.raises(ValueError, match="invalid staged operation"):
        migration.provider_snapshot(op, frozenset({"claude"}), state_dir=tmp_path)


def test_staged_python_parent_symlink_is_not_an_entrypoint(preflight, tmp_path):
    op, _, migration, _, _ = preflight
    python = migration.Path(op.env_python)
    actual_bin = tmp_path / "external-bin"
    python.parent.rename(actual_bin)
    python.parent.symlink_to(actual_bin, target_is_directory=True)
    with pytest.raises(ValueError, match="invalid staged operation"):
        migration.provider_snapshot(op, frozenset(), state_dir=tmp_path)


def test_symlink_retarget_changes_preflight_hash(preflight, tmp_path):
    op, _, migration, binary, _ = preflight
    first = tmp_path / "first-cli"
    second = tmp_path / "second-cli"
    binary.rename(first)
    second.write_bytes(b"different executable")
    binary.symlink_to(first.name)
    before = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    binary.unlink()
    binary.symlink_to(second.name)
    after = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    assert snapshot_id(before) != snapshot_id(after)


def test_explicit_symlink_preserves_logical_identity_and_retarget(preflight, tmp_path, monkeypatch):
    op, state, migration, binary, _ = preflight
    first = tmp_path / "explicit-first"
    second = tmp_path / "explicit-second"
    binary.rename(first)
    second.write_bytes(b"another executable")
    binary.symlink_to(first.name)
    state["opencode"] = "2.2.5"
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))
    before = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    identity = before["providers"][0]["identity"]
    assert identity["logical_path"] == str(binary)
    assert identity["links"]
    binary.unlink()
    binary.symlink_to(second.name)
    after = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path)
    assert snapshot_id(before) != snapshot_id(after)


def test_process_tree_constructor_failure_waits_only_with_timeout(monkeypatch):
    import io
    from ciao import provider_migration as migration
    from ciao.os_support import processes

    class FakeProcess:
        pid = 123
        def __init__(self):
            self.stdin = None
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.killed = False
            self.waits = []
        def kill(self):
            self.killed = True
        def poll(self):
            return None
        def wait(self, timeout=None):
            self.waits.append(timeout)
            assert timeout is not None, "unbounded wait"
            return 0

    process = FakeProcess()
    monkeypatch.setattr(migration.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(migration.os, "killpg", lambda *_: None, raising=False)
    def fail_tree(*args, **kwargs):
        raise OSError("tree setup failed")
    monkeypatch.setattr(processes, "ProcessTree", fail_tree)
    with pytest.raises(ValueError, match="probe failed"):
        migration._run_bounded(["fake"], 1, 4096, 4096)
    assert process.killed and process.waits and all(timeout is not None for timeout in process.waits)
    assert process.stdout.closed and process.stderr.closed


def test_process_tree_constructor_failure_kills_owned_child(monkeypatch):
    import time
    from ciao import provider_migration as migration
    from ciao.os_support import processes

    popen = migration.subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    def fail_tree(*args, **kwargs):
        raise OSError("tree setup failed")
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    monkeypatch.setattr(processes, "ProcessTree", fail_tree)
    started = time.monotonic()
    with pytest.raises(ValueError, match="probe failed"):
        migration._run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], 1, 4096, 4096)
    assert time.monotonic() - started < 3
    child = children[0]
    assert child.poll() is not None
    assert child.stdout.closed and child.stderr.closed


def test_gated_bootstrap_refuses_payload_if_tree_assignment_fails(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao.os_support import processes
    marker = tmp_path / "started"
    launches = []
    children = []
    popen = migration.subprocess.Popen
    def spawn(argv, *args, **kwargs):
        launches.append(argv)
        child = popen(argv, *args, **kwargs)
        children.append(child)
        return child
    def fail_tree(*args, **kwargs):
        assert not marker.exists()
        raise OSError("job assignment failed")
    monkeypatch.setattr(migration, "_use_windows_bootstrap", lambda: True, raising=False)
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    monkeypatch.setattr(processes, "ProcessTree", fail_tree)
    payload = "import pathlib,subprocess,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); time.sleep(30)"
    with pytest.raises(ValueError, match="probe failed"):
        migration._run_bounded([sys.executable, "-c", payload, str(marker)], 3, 4096, 4096)
    assert launches[0][0:3] == [sys.executable, "-I", "-S"]
    assert children[0].poll() is not None
    assert not marker.exists()


def test_gated_bootstrap_grants_only_after_tree_assignment(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao.os_support import processes
    marker = tmp_path / "started"
    real_tree = processes.ProcessTree
    assigned = []
    def assign(pid):
        assert not marker.exists()
        tree = real_tree(pid)
        assigned.append(True)
        return tree
    monkeypatch.setattr(migration, "_use_windows_bootstrap", lambda: True, raising=False)
    monkeypatch.setattr(processes, "ProcessTree", assign)
    payload = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started'); print('ok')"
    output = migration._run_bounded([sys.executable, "-c", payload, str(marker)], 5, 4096, 4096)
    assert assigned and marker.read_text() == "started" and output.strip() == b"ok"


@pytest.mark.parametrize("grant", [b"", b"X"])
def test_gated_bootstrap_rejects_eof_and_wrong_grant(tmp_path, grant):
    import subprocess
    from ciao import provider_migration as migration
    marker = tmp_path / "started"
    payload = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", migration._WINDOWS_PROBE_BOOTSTRAP, sys.executable, "-c", payload, str(marker)], input=grant, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert result.returncode != 0 and not marker.exists()


def test_gated_bootstrap_does_not_grant_after_deadline(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao.os_support import processes
    marker = tmp_path / "started"
    real_tree = processes.ProcessTree
    clock = [0.0]
    def assign(pid):
        tree = real_tree(pid)
        clock[0] = 10.0
        return tree
    monkeypatch.setattr(migration, "_use_windows_bootstrap", lambda: True)
    monkeypatch.setattr(migration.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(processes, "ProcessTree", assign)
    payload = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')"
    with pytest.raises(ValueError, match="probe failed"):
        migration._run_bounded([sys.executable, "-c", payload, str(marker)], 1, 4096, 4096)
    assert not marker.exists()


@pytest.mark.parametrize("overflow", [True, False])
def test_gated_bootstrap_cleans_payload_and_descendant(tmp_path, monkeypatch, overflow):
    import os
    import subprocess
    from ciao import provider_migration as migration
    monkeypatch.setattr(migration, "_use_windows_bootstrap", lambda: True, raising=False)
    popen = migration.subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    pid_file = tmp_path / "descendant.pid"
    payload = ("import os,pathlib,subprocess,sys,time; "
               "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)']); "
               "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(p.pid)); "
               + ("os.write(1,b'x'*4097); " if overflow else "") + "time.sleep(4)")
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", payload, str(pid_file)], 1.5, 4096, 4096)
    assert children[0].poll() is not None
    for pid in map(int, pid_file.read_text().split()):
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if handle:
                try:
                    assert kernel.WaitForSingleObject(ctypes.c_void_p(handle), 2000) == 0
                finally:
                    kernel.CloseHandle(ctypes.c_void_p(handle))
        else:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
            assert not status or status.startswith("Z")


def test_constructor_failure_kills_owned_descendant(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    from ciao import provider_migration as migration
    from ciao.os_support import processes

    popen = subprocess.Popen
    children = []
    descendant = []
    still_alive = False
    marker = tmp_path / "payload-started"
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    def fail_tree(*args, **kwargs):
        nonlocal still_alive
        if sys.platform == "win32":
            assert not marker.exists()
            raise OSError("tree setup failed")
        descendant.append(int(children[0].stdout.readline().decode()))
        still_alive = True
        raise OSError("tree setup failed")
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    monkeypatch.setattr(processes, "ProcessTree", fail_tree)
    script = "import pathlib,subprocess,sys,time; pathlib.Path(sys.argv[1]).write_text('started'); p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
    try:
        with pytest.raises(ValueError, match="probe failed"):
            migration._run_bounded([sys.executable, "-c", script, str(marker)], 3, 4096, 4096)
        assert children[0].poll() is not None
        if sys.platform == "win32":
            assert not marker.exists() and not descendant
        else:
            status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant[0])], capture_output=True, text=True, timeout=3).stdout.strip()
            still_alive = bool(status and not status.startswith("Z"))
            assert not status or status.startswith("Z")
    finally:
        if still_alive:
            try:
                os.killpg(children[0].pid, signal.SIGKILL)
            except OSError:
                pass


def test_constructor_failure_kills_group_after_leader_exits(monkeypatch):
    import os
    import signal
    import subprocess
    from ciao import provider_migration as migration
    from ciao.os_support import processes

    if sys.platform == "win32":
        assert migration._use_windows_bootstrap()
        return
    popen = subprocess.Popen
    children = []
    descendant = []
    live = False
    def spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        return process
    def fail_tree(*args, **kwargs):
        nonlocal live
        descendant.append(int(children[0].stdout.readline().decode()))
        live = True
        assert children[0].wait(timeout=3) == 0
        raise OSError("tree setup failed after leader exit")
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    monkeypatch.setattr(processes, "ProcessTree", fail_tree)
    script = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); print(p.pid,flush=True)"
    try:
        with pytest.raises(ValueError, match="probe failed"):
            migration._run_bounded([sys.executable, "-c", script], 3, 4096, 4096)
        status = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant[0])], capture_output=True, text=True, timeout=3).stdout.strip()
        live = bool(status and not status.startswith("Z"))
        assert not live
    finally:
        if live:
            try:
                os.killpg(children[0].pid, signal.SIGKILL)
            except OSError:
                pass


def test_operation_id_is_safe_timestamp_and_matching_version() -> None:
    assert valid_operation_id("20261112T101500Z-1.2.3", "1.2.3")
    for operation_id in (
        "20261112T101500Z-1.2.3/other",
        "20261112T101500Z-1..3",
        "20261112T101500Z-",
        "20261112T10150Z-1.2.3",
        "20261312T101500Z-1.2.3",
        "20261112T991500Z-1.2.3",
        "20261112T101500Z-١.2.3",
        "20261112T101500Z-01.2.3",
    ):
        assert not valid_operation_id(operation_id)
    assert not valid_operation_id("20261112T101500Z-1.2.3", "1.2.4")
