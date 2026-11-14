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


def test_bounded_runner_explicit_environment_and_cwd(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    monkeypatch.setenv("RUNNER_INHERITED_SENTINEL", "must-not-leak")
    env = {"RUNNER_SUPPLIED_SENTINEL": "present"}
    code = "import os; print(os.getcwd()); print(os.getenv('RUNNER_INHERITED_SENTINEL', 'absent')); print(os.getenv('RUNNER_SUPPLIED_SENTINEL'))"
    output = migration._run_bounded([sys.executable, "-c", code], 3, 4096, 4096, cwd=tmp_path, env=env)
    assert output.decode().splitlines() == [str(tmp_path), "absent", "present"]


def test_bounded_runner_directory_fd_survives_path_replacement(tmp_path):
    import os
    from ciao import provider_migration as migration
    if os.name != "posix":
        with pytest.raises(ValueError):
            migration._run_bounded([sys.executable, "-c", "pass"], 3, 4096, 4096, cwd_fd=0)
        return
    directory = tmp_path / "work"
    directory.mkdir()
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory.rename(tmp_path / "original")
        directory.mkdir()
        output = migration._run_bounded([sys.executable, "-c", "import os; print(os.stat('.').st_ino)"], 3, 4096, 4096, cwd_fd=fd)
        assert int(output.strip()) == os.fstat(fd).st_ino
        assert os.stat(directory).st_ino != os.fstat(fd).st_ino
    finally:
        os.close(fd)


def test_bounded_runner_combined_limit_terminates_live_child(monkeypatch):
    import time
    from ciao import provider_migration as migration
    children = []
    popen = migration.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    code = "import os,time; os.write(1,b'x'*9000); os.write(2,b'y'*9000); time.sleep(30)"
    started = time.monotonic()
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", code], 8, 16384, 16384, combined_cap=16384)
    assert time.monotonic() - started < 5
    assert len(children) == 1 and children[0].poll() is not None


def test_bounded_runner_combined_limit_terminates_descendant(tmp_path, monkeypatch):
    import subprocess
    from ciao import provider_migration as migration
    children = []
    popen = migration.subprocess.Popen
    def spawn(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(migration.subprocess, "Popen", spawn)
    marker = tmp_path / "descendant.pid"
    code = ("import os,pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
            "os.write(1,b'x'*9000); os.write(2,b'y'*9000); time.sleep(30)")
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", code, str(marker)], 8, 16384, 16384, combined_cap=16384)
    assert children[0].poll() is not None
    pid = int(marker.read_text())
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


def test_bounded_runner_gated_environment_cwd_and_failed_assignment(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao.os_support import processes
    marker = tmp_path / "payload"
    monkeypatch.setattr(migration, "_use_windows_bootstrap", lambda: True)
    env = {"RUNNER_SENTINEL": "owned"}
    code = "import os,pathlib; pathlib.Path('payload').write_text(os.environ['RUNNER_SENTINEL'])"
    original_tree = processes.ProcessTree
    monkeypatch.setattr(processes, "ProcessTree", lambda pid: (_ for _ in ()).throw(OSError("refused")))
    with pytest.raises(ValueError):
        migration._run_bounded([sys.executable, "-c", code], 3, 4096, 4096, cwd=tmp_path, env=env)
    assert not marker.exists()
    monkeypatch.setattr(processes, "ProcessTree", original_tree)
    migration._run_bounded([sys.executable, "-c", code], 3, 4096, 4096, cwd=tmp_path, env=env)
    assert marker.read_text() == "owned"


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


@pytest.mark.parametrize("field,value", [
    ("id", "../20261112T101500Z-1.2.3"),
    ("id", "20261112T101500Z-1.2.4"),
    ("stage_dir", "relative/1.2.3"),
])
def test_candidate_invalid_operation_refused_before_observation(preflight, tmp_path, monkeypatch, field, value):
    op, _, migration, _, _ = preflight
    setattr(op, field, value)
    monkeypatch.setattr(migration, "_run_bounded", lambda *a, **kw: pytest.fail("unexpected process"))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        migration.stage_opencode_candidate(op, "a" * 64, frozenset({"opencode"}), state_dir=tmp_path)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


@pytest.fixture
def published_candidate(preflight, tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from ciao.os_support import private
    op, _, migration, _, _ = preflight
    private.make_private_dir(tmp_path)
    stage = Path(op.stage_dir)
    private.make_private_dir(stage)
    candidate = stage / "provider-candidate"
    candidate.mkdir(mode=0o700)
    private.make_private_dir(candidate)
    binary = candidate / ("opencode.exe" if sys.platform == "win32" else "opencode")
    binary.write_bytes(b"owned candidate fixture")
    if sys.platform == "win32":
        private.make_private(binary)
    else:
        binary.chmod(0o700)
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    metadata = candidate / "candidate.json"
    metadata.write_text(json.dumps({"schema": 1, "operation_id": op.id, "package": "@opencode/cli-darwin-arm64", "version": "2.2.5", "sha256": sha, "relative_binary": binary.name}))
    private.make_private(metadata)
    original = migration._run_bounded
    observations = []
    def probe(argv, *args, **kwargs):
        observations.append(argv)
        if argv[0] == str(binary):
            return b"2.2.5\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", probe)
    return op, migration, binary, metadata, sha, observations


def test_stage_candidate_fresh_and_retry(preflight, tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.os_support import private
    op, _, migration, shared, _ = preflight
    private.make_private_dir(tmp_path)
    private.make_private_dir(Path(op.stage_dir))
    approved = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    original = migration._run_bounded
    calls = []
    def runner(argv, *args, **kwargs):
        if "install" in argv:
            calls.append((argv, args, kwargs))
            prefix = Path(argv[argv.index("--prefix") + 1])
            package = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64"
            (package / "bin").mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": "@opencode/cli-darwin-arm64", "version": "2.2.5", "os": ["darwin"], "cpu": ["arm64"]}))
            binary = package / "bin" / ("opencode.exe" if sys.platform == "win32" else "opencode")
            binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"native fixture" * 30)
            if sys.platform != "win32":
                binary.chmod(0o700)
            return b""
        if Path(argv[0]).name in ("opencode", "opencode.exe") and argv[0] != str(shared):
            return b"2.2.5\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    result = migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)
    assert not (Path(op.stage_dir) / "provider-candidate-work").exists()
    assert result["version"] == "2.2.5"
    assert migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path) == result
    assert len(calls) == 1
    assert shared.read_bytes() == b"executable"


def test_stage_stale_approval_does_not_write_or_install(preflight, tmp_path):
    op, state, migration, _, _ = preflight
    approved = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    state["candidate"] = "2.2.6"
    with pytest.raises(ValueError, match="stale"):
        migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_stage_refuses_linked_stage_before_writes(preflight, tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.os_support import private
    op, _, migration, _, _ = preflight
    private.make_private_dir(tmp_path)
    private.make_private_dir(Path(op.stage_dir))
    approved = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    stage = Path(op.stage_dir)
    displaced = stage.with_name("displaced-stage")
    stage.rename(displaced)
    stage.symlink_to(displaced, target_is_directory=True)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def test_candidate_file_rejects_linked_parent(tmp_path):
    import os
    from ciao import provider_migration as migration
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        migration._open_candidate_file(tmp_path / "link" / "secret", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert not list(outside.iterdir())


def test_candidate_cleanup_refuses_replaced_root(tmp_path):
    from ciao import provider_migration as migration
    work = tmp_path / "work"
    work.mkdir()
    identity = work.stat()
    work.rename(tmp_path / "original")
    work.mkdir()
    (work / "foreign").write_bytes(b"preserve")
    with pytest.raises(ValueError, match="manual recovery"):
        migration._remove_candidate_work(work, identity)
    assert (work / "foreign").read_bytes() == b"preserve"


def test_candidate_cleanup_refuses_link_without_deleting_entries(tmp_path):
    from ciao import provider_migration as migration
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"preserve")
    work = tmp_path / "work"
    work.mkdir()
    (work / "link").symlink_to(outside, target_is_directory=True)
    (work / "first").write_bytes(b"also preserve")
    with pytest.raises(ValueError, match="manual recovery"):
        migration._remove_candidate_work(work, work.stat())
    assert (work / "first").read_bytes() == b"also preserve"
    assert (work / "link").is_symlink()
    assert (outside / "sentinel").read_bytes() == b"preserve"


@pytest.mark.parametrize("fault", ["owner", "acl", "type"])
def test_candidate_cleanup_prevalidates_all_entries(tmp_path, monkeypatch, fault):
    import os
    from ciao import provider_migration as migration
    from ciao.os_support import private
    work = tmp_path / "work"
    work.mkdir()
    private.make_private_dir(work)
    first, untrusted = work / "first", work / "untrusted"
    first.write_bytes(b"preserve first")
    untrusted.write_bytes(b"preserve untrusted")
    private.make_private(first)
    private.make_private(untrusted)
    if sys.platform != "win32":
        untrusted.chmod(0o640)
    info = untrusted.stat()
    inspected = []
    if fault == "owner":
        original = private.is_owned_descriptor
        def owner(fd):
            if os.fstat(fd).st_ino == info.st_ino:
                inspected.append("owner")
                return False
            return original(fd)
        monkeypatch.setattr(private, "is_owned_descriptor", owner)
    elif fault == "acl":
        name = "is_private_descriptor_strict" if sys.platform == "win32" else "descriptor_acl_grants_another_account"
        original = getattr(private, name)
        def acl(fd):
            if os.fstat(fd).st_ino == info.st_ino:
                inspected.append("acl")
                return sys.platform != "win32"
            return original(fd)
        monkeypatch.setattr(private, name, acl)
    else:
        if sys.platform == "win32":
            original_lstat = type(untrusted).lstat
            def special_lstat(path, *args, **kwargs):
                result = original_lstat(path, *args, **kwargs)
                if path == untrusted:
                    inspected.append("type")
                    values = list(result)
                    values[0] = migration.stat.S_IFIFO | 0o600
                    return os.stat_result(values)
                return result
            monkeypatch.setattr(type(untrusted), "lstat", special_lstat)
        else:
            original = migration.stat.S_ISREG
            monkeypatch.setattr(migration.stat, "S_ISREG", lambda mode: False if mode == info.st_mode else original(mode))
    with pytest.raises(ValueError, match="manual recovery"):
        migration._remove_candidate_work(work, work.stat())
    if fault in ("owner", "acl"):
        assert inspected == [fault]
    elif sys.platform == "win32":
        assert "type" in inspected
    assert first.read_bytes() == b"preserve first"
    assert untrusted.read_bytes() == b"preserve untrusted"


def test_windows_cleanup_checks_each_file_dacl_before_deletion(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from ciao import provider_migration as migration
    from ciao.os_support import private
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    private.make_private_dir(work)
    first, unsafe = work / "first", work / "unsafe"
    for path in (first, unsafe):
        path.write_bytes(b"preserve")
        private.make_private(path)
    inode = unsafe.stat().st_ino
    original = private.is_private_descriptor_strict
    checked = []
    def dacl(fd):
        if os.fstat(fd).st_ino == inode:
            checked.append("unsafe-file")
            return False
        return original(fd)
    monkeypatch.setattr(private, "is_private_descriptor_strict", dacl)
    # Only the module's branch selection changes, not the host OS or pathlib.
    monkeypatch.setattr(migration, "sys", SimpleNamespace(platform="win32"))
    with pytest.raises(ValueError, match="manual recovery"):
        migration._remove_candidate_work(work, work.stat())
    assert checked == ["unsafe-file"]
    assert first.read_bytes() == unsafe.read_bytes() == b"preserve"


def test_windows_secures_inherited_work_after_whole_tree_validation(tmp_path, monkeypatch):
    from ciao import provider_migration as migration
    from ciao.os_support import private
    work = tmp_path / "work"
    work.mkdir()
    private.make_private_dir(work)
    child = work / "child"
    child.mkdir()
    file = child / "file"
    file.write_bytes(b"sentinel")
    secured = []
    monkeypatch.setattr(private, "is_owned_path", lambda path: True)
    monkeypatch.setattr(private, "is_private", lambda path: True)
    monkeypatch.setattr(private, "is_owned_descriptor", lambda fd: True)
    monkeypatch.setattr(private, "is_private_descriptor", lambda fd: True)
    monkeypatch.setattr(private, "make_private_dir", lambda path: secured.append(path))
    monkeypatch.setattr(private, "make_private", lambda path: secured.append(path))
    migration._secure_windows_work(work, work.stat())
    assert set(secured) == {child, file}
    assert file.read_bytes() == b"sentinel"


@pytest.mark.parametrize("fault", ["owner", "acl", "link", "special"])
def test_windows_work_refuses_whole_tree_before_protecting(tmp_path, monkeypatch, fault):
    import os
    from ciao import provider_migration as migration
    from ciao.os_support import private
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    private.make_private_dir(work)
    good = work / "good"
    good.write_bytes(b"keep")
    bad = work / "bad"
    if fault == "link":
        bad.symlink_to(good)
    elif fault == "special":
        if sys.platform != "win32":
            os.mkfifo(bad)
        else:
            # Exercise the rejected-type seam without requiring a Windows FIFO API.
            bad.write_bytes(b"preserve")
            original_lstat = type(bad).lstat
            def special_lstat(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if path == bad:
                    values = list(info)
                    values[0] = migration.stat.S_IFIFO | 0o600
                    return os.stat_result(values)
                return info
            monkeypatch.setattr(type(bad), "lstat", special_lstat)
    else:
        bad.write_bytes(b"keep bad")
    bad_inode = bad.lstat().st_ino
    monkeypatch.setattr(private, "is_owned_path", lambda path: True)
    monkeypatch.setattr(private, "is_private", lambda path: True)
    monkeypatch.setattr(private, "is_owned_descriptor", lambda fd: fault != "owner" or os.fstat(fd).st_ino != bad_inode)
    monkeypatch.setattr(private, "is_private_descriptor", lambda fd: fault != "acl" or os.fstat(fd).st_ino != bad_inode)
    changed = []
    monkeypatch.setattr(private, "make_private_dir", lambda path: changed.append(path))
    monkeypatch.setattr(private, "make_private", lambda path: changed.append(path))
    with pytest.raises(ValueError, match="manual recovery"):
        migration._secure_windows_work(work, work.stat())
    assert not changed
    assert good.read_bytes() == b"keep"
    if fault in ("owner", "acl"):
        assert bad.read_bytes() == b"keep bad"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows inherited DACL")
def test_windows_native_inherited_work_becomes_strictly_private(tmp_path):
    from ciao import provider_migration as migration
    from ciao.os_support import private
    work = tmp_path / "work"
    work.mkdir()
    private.make_private_dir(work)
    directory = work / "npm-output"
    directory.mkdir()
    file = directory / "package.json"
    file.write_bytes(b"sentinel")
    assert private.is_private(directory) and private.is_private(file)
    assert not private.is_private_strict(directory)
    assert not private.is_private_strict(file)
    migration._secure_windows_work(work, work.stat())
    assert private.is_private_strict(directory)
    assert private.is_private_strict(file)
    migration._remove_candidate_work(work, work.stat())
    assert not work.exists()


def test_windows_work_refuses_external_hardlink_before_permission_changes(tmp_path, monkeypatch):
    import os
    import subprocess
    from ciao import provider_migration as migration
    from ciao.os_support import private
    outside = tmp_path / "outside"
    outside.write_bytes(b"external private data")
    private.make_private(outside)
    work = tmp_path / "work"
    work.mkdir()
    private.make_private_dir(work)
    good = work / "good"
    good.write_bytes(b"keep")
    private.make_private(good)
    alias = work / "alias"
    os.link(outside, alias)
    assert alias.stat().st_ino == outside.stat().st_ino
    before = outside.stat()
    acl = subprocess.check_output(["icacls", str(outside)]) if sys.platform == "win32" else None
    changed = []
    monkeypatch.setattr(private, "make_private", lambda path: changed.append(path))
    monkeypatch.setattr(private, "make_private_dir", lambda path: changed.append(path))
    with pytest.raises(ValueError, match="manual recovery"):
        migration._secure_windows_work(work, work.stat())
    assert not changed
    assert good.read_bytes() == b"keep"
    assert outside.read_bytes() == alias.read_bytes() == b"external private data"
    assert outside.stat().st_mode == before.st_mode
    assert outside.stat().st_nlink == before.st_nlink
    if acl is not None:
        assert subprocess.check_output(["icacls", str(outside)]) == acl


def test_candidate_publication_does_not_replace_destination(tmp_path):
    from ciao import provider_migration as migration
    source, destination = tmp_path / "temp", tmp_path / "candidate"
    source.mkdir()
    destination.mkdir()
    (destination / "foreign").write_bytes(b"preserve")
    with pytest.raises(OSError):
        migration._publish_candidate(source, destination)
    assert (destination / "foreign").read_bytes() == b"preserve"
    assert source.is_dir()


@pytest.fixture
def stage_installer(preflight, tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.os_support import private
    op, state, migration, shared, _ = preflight
    private.make_private_dir(tmp_path)
    private.make_private_dir(Path(op.stage_dir))
    approval = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    original = migration._run_bounded
    setup = {"metadata": {"name": "@opencode/cli-darwin-arm64", "version": "2.2.5", "os": ["darwin"], "cpu": ["arm64"]}, "binary": b"\xcf\xfa\xed\xfe" + b"fixture" * 50, "calls": []}
    def runner(argv, *args, **kwargs):
        if "install" in argv:
            setup["calls"].append((argv, args, kwargs))
            prefix = Path(argv[argv.index("--prefix") + 1])
            package = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64"
            (package / "bin").mkdir(parents=True)
            data = setup["metadata"]
            (package / "package.json").write_text(json.dumps(data) if isinstance(data, dict) else data)
            binary = package / "bin" / ("opencode.exe" if sys.platform == "win32" else "opencode")
            binary.write_bytes(setup["binary"])
            if sys.platform != "win32":
                binary.chmod(0o700)
            if setup.get("symlink"):
                binary.unlink()
                binary.symlink_to(package / "package.json")
            if setup.get("after_install"):
                setup["after_install"](prefix)
            return b""
        if Path(argv[0]).name in ("opencode", "opencode.exe") and argv[0] != str(shared):
            return b"2.2.5\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    def stage():
        return migration.stage_opencode_candidate(op, approval, frozenset({"opencode"}), state_dir=tmp_path)
    return op, state, migration, setup, stage


@pytest.mark.parametrize("fault", ["duplicate", "oversize", "name", "version", "os", "cpu", "scripts", "type", "stub", "wrong_format", "symlink"])
def test_stage_rejects_bad_installed_package(stage_installer, tmp_path, monkeypatch, fault):
    op, _, migration, setup, stage = stage_installer
    original_open = migration._open_candidate_file
    inspected = []
    def observe(path, *args, **kwargs):
        inspected.append(path.name)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(migration, "_open_candidate_file", observe)
    data = setup["metadata"]
    if fault == "duplicate":
        setup["metadata"] = '{"name":"evil","name":"@opencode/cli-darwin-arm64","version":"2.2.5","os":["darwin"],"cpu":["arm64"]}'
    elif fault == "oversize":
        setup["metadata"] = " " * 65537
    elif fault in ("name", "version", "os", "cpu", "scripts"):
        data[fault] = {"name": "bad", "version": "2.2.6", "os": ["linux"], "cpu": ["x64"], "scripts": {"postinstall": "bad"}}[fault]
    elif fault == "type":
        data["scripts"] = []
    elif fault == "stub":
        setup["binary"] = b"#!/usr/bin/env node\n" + b"x" * 300
    elif fault == "wrong_format":
        setup["binary"] = b"\x7fELF" + b"x" * 350
    else:
        setup["symlink"] = True
    if fault == "symlink":
        with pytest.raises(ValueError, match="manual recovery"):
            stage()
    else:
        reason = ("duplicate package metadata key" if fault == "duplicate" else
                  "invalid native executable" if fault in ("stub", "wrong_format") else
                  "invalid package metadata")
        with pytest.raises(ValueError, match="^" + reason + "$"):
            stage()
    assert len(setup["calls"]) == 1
    if fault != "symlink":
        assert "package.json" in inspected
    if fault in ("stub", "wrong_format"):
        assert ("opencode.exe" if sys.platform == "win32" else "opencode") in inspected
    assert not (tmp_path / "1.2.3" / "provider-candidate").exists()


def test_stage_refuses_unfinished_without_modifying_it(stage_installer, tmp_path):
    _, _, _, _, stage = stage_installer
    work = tmp_path / "1.2.3" / "provider-candidate-work"
    work.mkdir()
    marker = work / "foreign"
    marker.write_bytes(b"preserve")
    before = work.stat().st_mode
    with pytest.raises(ValueError, match="manual recovery"):
        stage()
    assert marker.read_bytes() == b"preserve" and work.stat().st_mode == before


@pytest.mark.parametrize("fault", ["owner", "acl"])
def test_stage_checks_package_directory_permissions(stage_installer, monkeypatch, fault):
    import os
    from ciao.os_support import private
    op, _, migration, setup, stage = stage_installer
    seen = []
    before_cleanup = []
    remove = migration._remove_candidate_work
    def observe_cleanup(*args):
        before_cleanup.append(list(seen))
        return remove(*args)
    monkeypatch.setattr(migration, "_remove_candidate_work", observe_cleanup)
    def after_install(prefix):
        directory = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64" / "bin"
        inode = directory.stat().st_ino
        if sys.platform == "win32":
            original = private.is_private_strict
            def strict(path):
                if migration.Path(path) == directory:
                    seen.append(fault)
                    return False
                return original(path)
            monkeypatch.setattr(private, "is_private_strict", strict)
        else:
            name = "is_owned_descriptor" if fault == "owner" else "descriptor_acl_grants_another_account"
            original = getattr(private, name)
            def permission(fd):
                if os.fstat(fd).st_ino == inode:
                    seen.append(fault)
                    return fault == "acl"
                return original(fd)
            monkeypatch.setattr(private, name, permission)
    setup["after_install"] = after_install
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    assert seen and all(value == fault for value in seen)
    assert before_cleanup and before_cleanup[-1] == [fault]
    assert (migration.Path(op.stage_dir) / "provider-candidate-work").is_dir()
    assert not (migration.Path(op.stage_dir) / "provider-candidate").exists()


@pytest.mark.parametrize("entry", ["metadata", "binary"])
def test_windows_stage_checks_installed_file_dacl(stage_installer, monkeypatch, entry):
    import os
    from types import SimpleNamespace
    from ciao.os_support import private
    op, _, migration, setup, stage = stage_installer
    inspected = []
    before_cleanup = []
    remove = migration._remove_candidate_work
    def observe_cleanup(*args):
        before_cleanup.append(list(inspected))
        return remove(*args)
    monkeypatch.setattr(migration, "_remove_candidate_work", observe_cleanup)
    selected_inode = []
    original = private.is_private_descriptor_strict
    if sys.platform != "win32":
        # Model Windows owner-only ACEs independently of the strict DACL fault.
        monkeypatch.setattr(private, "is_private_descriptor", lambda fd: True)
    def dacl(fd):
        if selected_inode and os.fstat(fd).st_ino == selected_inode[0]:
            inspected.append(entry)
            return False
        return original(fd)
    monkeypatch.setattr(private, "is_private_descriptor_strict", dacl)
    def after_install(prefix):
        for path in (prefix, *prefix.rglob("*")):
            if path.is_dir():
                private.make_private_dir(path)
        package = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64"
        binary = package / "bin" / ("opencode.exe" if sys.platform == "win32" else "opencode")
        if binary.name != "opencode.exe":
            binary = binary.rename(binary.with_name("opencode.exe"))
        metadata = package / "package.json"
        private.make_private(metadata)
        selected_inode.append((metadata if entry == "metadata" else binary).stat().st_ino)
        monkeypatch.setattr(migration, "sys", SimpleNamespace(platform="win32"))
    setup["after_install"] = after_install
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    assert inspected and all(value == entry for value in inspected)
    assert before_cleanup and before_cleanup[0] == [entry]
    assert (migration.Path(op.stage_dir) / "provider-candidate-work").is_dir()
    assert len(setup["calls"]) == 1
    assert not (migration.Path(op.stage_dir) / "provider-candidate").exists()


def test_windows_stage_secures_inherited_output_and_cleans_work(stage_installer, monkeypatch):
    from types import SimpleNamespace
    from ciao.os_support import private
    op, _, migration, setup, stage = stage_installer
    protected = set()
    monkeypatch.setattr(private, "is_owned_path", lambda path: True)
    monkeypatch.setattr(private, "is_private", lambda path: True)
    monkeypatch.setattr(private, "is_private_descriptor", lambda fd: True)
    monkeypatch.setattr(private, "is_private_strict", lambda path: True)
    monkeypatch.setattr(private, "is_private_descriptor_strict", lambda fd: True)
    original_file, original_dir = private.make_private, private.make_private_dir
    def secure_file(path):
        protected.add(path)
        original_file(path)
    def secure_dir(path):
        protected.add(path)
        original_dir(path)
    monkeypatch.setattr(private, "make_private", secure_file)
    monkeypatch.setattr(private, "make_private_dir", secure_dir)
    def after_install(prefix):
        binary = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64" / "bin" / "opencode"
        if binary.exists():
            binary.rename(binary.with_name("opencode.exe"))
        monkeypatch.setattr(migration, "sys", SimpleNamespace(platform="win32"))
    setup["after_install"] = after_install
    result = stage()
    work = migration.Path(op.stage_dir) / "provider-candidate-work"
    assert result["version"] == "2.2.5"
    assert work / "prefix" in protected
    assert work / "prefix" / "node_modules" / "@opencode" / "cli-darwin-arm64" / "bin" / "opencode.exe" in protected
    assert not work.exists()


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "writable-root", "writable-dir", "writable-file"])
def test_installer_rejects_unsafe_unrelated_work_before_payload(stage_installer, preflight, tmp_path, monkeypatch, fault):
    import os
    from ciao.os_support import private
    op, _, migration, setup, stage = stage_installer
    shared = preflight[3]
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    private.make_private(outside)
    saved = []
    original = migration._run_bounded
    make_directory = migration._make_candidate_directory
    temporary = []
    payload = []
    def snapshot(work):
        paths = [work, *work.rglob("*")]
        return sorted((str(p.relative_to(work)), p.lstat().st_mode, p.lstat().st_dev, p.lstat().st_ino,
                       p.lstat().st_nlink, os.readlink(p) if p.is_symlink()
                       else p.read_bytes() if p.is_file() else None) for p in paths)
    def after_install(prefix):
        work = prefix.parent
        cache = work / "cache"
        cache.mkdir()
        item = cache / "entry"
        if fault == "symlink":
            item.symlink_to(outside)
        elif fault == "hardlink":
            os.link(outside, item)
        else:
            if fault == "writable-dir":
                item.mkdir()
                target = item
            elif fault == "writable-file":
                item.write_bytes(b"unsafe")
                target = item
            else:
                target = work
            if sys.platform == "win32":
                if fault == "writable-file":
                    inode = target.stat().st_ino
                    previous_fd = private.is_private_descriptor
                    monkeypatch.setattr(private, "is_private_descriptor", lambda fd: False if os.fstat(fd).st_ino == inode else previous_fd(fd))
                else:
                    name = "is_private_strict" if target == work else "is_private"
                    previous = getattr(private, name)
                    monkeypatch.setattr(private, name, lambda p: False if migration.Path(p) == target else previous(p))
            else:
                target.chmod(0o777)
        saved.append(snapshot(work))
    setup["after_install"] = after_install
    def runner(argv, *args, **kwargs):
        if "provider-candidate-tmp-" in argv[0]:
            payload.append(argv)
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    def observe_directory(path):
        if path.name.startswith("provider-candidate-tmp-"):
            temporary.append(path)
        return make_directory(path)
    monkeypatch.setattr(migration, "_make_candidate_directory", observe_directory)
    with pytest.raises(ValueError, match="manual recovery"):
        stage()
    work = migration.Path(op.stage_dir) / "provider-candidate-work"
    assert snapshot(work) == saved[0]
    assert not payload
    assert not temporary
    assert not (migration.Path(op.stage_dir) / "provider-candidate").exists()
    assert shared.read_bytes() == b"executable"


def test_installer_accepts_ordinary_readable_work_entries(stage_installer):
    from ciao.os_support import private
    _, _, _, setup, stage = stage_installer
    def after_install(prefix):
        directory = prefix.parent / "cache"
        directory.mkdir()
        file = directory / "entry"
        file.write_bytes(b"npm cache")
        if sys.platform == "win32":
            private.make_private_dir(directory)
            private.make_private(file)
        else:
            directory.chmod(0o755)
            file.chmod(0o644)
    setup["after_install"] = after_install
    assert stage()["version"] == "2.2.5"


def test_failed_installer_cleans_owned_work_and_allows_retry(stage_installer, preflight, tmp_path, monkeypatch):
    op, _, migration, setup, stage = stage_installer
    shared = preflight[3]
    original = migration._run_bounded
    failed = []
    def runner(argv, *args, **kwargs):
        if "install" in argv and not failed:
            failed.append(True)
            work = migration.Path(op.stage_dir) / "provider-candidate-work"
            (work / "partial").write_bytes(b"safe output")
            raise ValueError("installer failed")
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    with pytest.raises(ValueError, match="^installer failed$"):
        stage()
    assert not (migration.Path(op.stage_dir) / "provider-candidate-work").exists()
    assert not list(migration.Path(op.stage_dir).glob("provider-candidate-tmp-*"))
    assert not (migration.Path(op.stage_dir) / "provider-candidate").exists()
    assert stage()["version"] == "2.2.5"
    assert shared.read_bytes() == b"executable"


def test_windows_failed_installer_secures_inherited_output_before_cleanup(stage_installer, monkeypatch):
    from types import SimpleNamespace
    from ciao.os_support import private
    op, _, migration, _, stage = stage_installer
    original_runner, original_sys = migration._run_bounded, migration.sys
    original_private = private.make_private
    secured = []
    failed = []
    if sys.platform != "win32":
        monkeypatch.setattr(private, "is_private_descriptor", lambda fd: True)
    def protect(path):
        secured.append(path)
        original_private(path)
    def runner(argv, *args, **kwargs):
        if "install" in argv and not failed:
            failed.append(True)
            work = migration.Path(op.stage_dir) / "provider-candidate-work"
            (work / "partial").write_bytes(b"safe inherited output")
            monkeypatch.setattr(migration, "sys", SimpleNamespace(platform="win32"))
            raise ValueError("installer failed")
        return original_runner(argv, *args, **kwargs)
    monkeypatch.setattr(private, "make_private", protect)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    with pytest.raises(ValueError, match="^installer failed$"):
        stage()
    work = migration.Path(op.stage_dir) / "provider-candidate-work"
    assert work / "partial" in secured
    assert not work.exists()
    monkeypatch.setattr(migration, "sys", original_sys)
    assert stage()["version"] == "2.2.5"


def test_failed_candidate_verification_cleans_owned_work_and_temp_and_allows_retry(stage_installer, preflight, monkeypatch):
    op, _, migration, _, stage = stage_installer
    shared = preflight[3]
    original = migration._run_bounded
    failed = []
    def runner(argv, *args, **kwargs):
        if "provider-candidate-tmp-" in argv[0] and not failed:
            failed.append(True)
            return b"wrong version\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    with pytest.raises(ValueError, match="^candidate verification failed$"):
        stage()
    directory = migration.Path(op.stage_dir)
    assert not (directory / "provider-candidate-work").exists()
    assert not list(directory.glob("provider-candidate-tmp-*"))
    assert not (directory / "provider-candidate").exists()
    assert stage()["version"] == "2.2.5"
    assert shared.read_bytes() == b"executable"


def test_failed_verification_preserves_both_trees_if_temp_has_external_hardlink(stage_installer, tmp_path, monkeypatch):
    import os
    from ciao.os_support import private
    op, _, migration, _, stage = stage_installer
    outside = tmp_path / "outside"
    outside.write_bytes(b"external")
    private.make_private(outside)
    original = migration._run_bounded
    def runner(argv, *args, **kwargs):
        if "provider-candidate-tmp-" in argv[0]:
            temp = migration.Path(argv[0]).parent
            os.link(outside, temp / "external-link")
            return b"wrong version\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    directory = migration.Path(op.stage_dir)
    work = directory / "provider-candidate-work"
    temps = list(directory.glob("provider-candidate-tmp-*"))
    assert work.is_dir() and (work / "user.npmrc").exists()
    assert len(temps) == 1 and (temps[0] / "external-link").read_bytes() == b"external"
    assert outside.read_bytes() == b"external"
    assert not (directory / "provider-candidate").exists()


@pytest.mark.parametrize("unsafe", ["work", "temp"])
def test_windows_failed_verification_prevalidates_both_trees_before_mutation(stage_installer, tmp_path, monkeypatch, unsafe):
    import os
    import subprocess
    from types import SimpleNamespace
    from ciao.os_support import private
    op, _, migration, _, stage = stage_installer
    outside = tmp_path / "external"
    outside.write_bytes(b"external")
    private.make_private(outside)
    original_runner = migration._run_bounded
    original_remove = migration._remove_candidate_work
    mutations = []
    before = []
    def snapshot(work, temp):
        result = []
        for root in (work, temp):
            for path in (root, *root.rglob("*")):
                info = path.lstat()
                acl = subprocess.check_output(["icacls", str(path)]) if sys.platform == "win32" else None
                result.append((str(path), info.st_mode, info.st_dev, info.st_ino, info.st_nlink,
                               path.read_bytes() if path.is_file() else None, acl))
        return sorted(result)
    def runner(argv, *args, **kwargs):
        if "provider-candidate-tmp-" in argv[0]:
            temp = migration.Path(argv[0]).parent
            work = migration.Path(op.stage_dir) / "provider-candidate-work"
            os.link(outside, (work if unsafe == "work" else temp) / "unsafe")
            before.append(snapshot(work, temp))
            for name in ("make_private", "make_private_dir"):
                original = getattr(private, name)
                def protect(path, original=original, name=name):
                    mutations.append(name)
                    return original(path)
                monkeypatch.setattr(private, name, protect)
            def remove(path, identity, validate_only=False):
                if not validate_only:
                    mutations.append("delete")
                return original_remove(path, identity, validate_only)
            monkeypatch.setattr(migration, "_remove_candidate_work", remove)
            if sys.platform != "win32":
                monkeypatch.setattr(private, "is_private", lambda path: True)
                monkeypatch.setattr(private, "is_owned_path", lambda path: True)
                monkeypatch.setattr(private, "is_private_descriptor", lambda fd: True)
                monkeypatch.setattr(private, "is_private_descriptor_strict", lambda fd: True)
            monkeypatch.setattr(migration, "sys", SimpleNamespace(platform="win32"))
            return b"wrong version\n"
        return original_runner(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    with pytest.raises(ValueError, match="manual recovery"):
        stage()
    directory = migration.Path(op.stage_dir)
    work = directory / "provider-candidate-work"
    temps = list(directory.glob("provider-candidate-tmp-*"))
    assert len(temps) == 1 and snapshot(work, temps[0]) == before[0]
    assert mutations == []
    assert outside.read_bytes() == b"external"
    assert not (directory / "provider-candidate").exists()


def test_failed_verification_reports_cleanup_failure_and_preserves_trees(stage_installer, monkeypatch):
    op, _, migration, _, stage = stage_installer
    runner = migration._run_bounded
    remove = migration._remove_candidate_work
    def wrong_version(argv, *args, **kwargs):
        if "provider-candidate-tmp-" in argv[0]:
            return b"wrong version\n"
        return runner(argv, *args, **kwargs)
    def fail_deletion(path, identity, validate_only=False):
        if not validate_only:
            raise OSError("injected deletion failure")
        return remove(path, identity, validate_only)
    monkeypatch.setattr(migration, "_run_bounded", wrong_version)
    monkeypatch.setattr(migration, "_remove_candidate_work", fail_deletion)
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    directory = migration.Path(op.stage_dir)
    assert (directory / "provider-candidate-work" / "user.npmrc").exists()
    temps = list(directory.glob("provider-candidate-tmp-*"))
    assert len(temps) == 1 and (temps[0] / "candidate.json").exists()
    assert not (directory / "provider-candidate").exists()


def test_stage_installer_environment_and_arguments(stage_installer, tmp_path, monkeypatch):
    _, _, _, setup, stage = stage_installer
    for name in ("NODE_OPTIONS", "NODE_PATH", "HTTPS_PROXY", "NPM_CONFIG_REGISTRY"):
        monkeypatch.setenv(name, "hostile")
    assert stage()["version"] == "2.2.5"
    argv, limits, options = setup["calls"][0]
    work = tmp_path / "1.2.3" / "provider-candidate-work"
    assert argv[1:] == ["install", "--prefix", str(work / "prefix"), "--cache", str(work / "cache"), "--ignore-scripts", "--no-save", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/", "@opencode/cli-darwin-arm64@2.2.5"]
    assert limits == (600, 16384, 16384)
    assert options["cwd"] == work and options["combined_cap"] == 16384
    assert not ({"NODE_OPTIONS", "NODE_PATH", "HTTPS_PROXY", "NPM_CONFIG_REGISTRY"} & options["env"].keys())
    assert options["env"]["NPM_CONFIG_USERCONFIG"] == str(work / "user.npmrc")


@pytest.mark.parametrize("npm_os", ["win32", "windows"])
def test_stage_windows_installer_command_and_environment(preflight, tmp_path, monkeypatch, npm_os):
    import os
    from pathlib import Path
    from ciao.os_support import private
    op, _, migration, shared, _ = preflight
    private.make_private_dir(tmp_path)
    private.make_private_dir(Path(op.stage_dir))
    node, script = tmp_path / "node.exe", tmp_path / "npm-cli.js"
    node.write_bytes(b"node")
    script.write_bytes(b"npm")
    capability = {"state": "ready", "package": "@opencode/cli-windows-arm64", "npm_argv": [str(node), str(script)], "reason": ""}
    monkeypatch.setattr(migration, "installer_capability", lambda **_: capability)
    for key in ("NODE_OPTIONS", "NODE_PATH", "HTTPS_PROXY", "NPM_CONFIG_REGISTRY"):
        monkeypatch.setenv(key, "hostile")
    approved = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    original = migration._run_bounded
    calls = []
    def runner(argv, *args, **kwargs):
        if "install" in argv:
            calls.append((argv, args, kwargs))
            prefix = Path(argv[argv.index("--prefix") + 1])
            package = prefix / "node_modules" / "@opencode" / "cli-windows-arm64"
            (package / "bin").mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"name": capability["package"], "version": "2.2.5", "os": [npm_os], "cpu": ["arm64"]}))
            binary = package / "bin" / ("opencode.exe" if sys.platform == "win32" else "opencode")
            binary.write_bytes(b"MZ" + b"x" * 350)
            if sys.platform != "win32":
                binary.chmod(0o700)
            return b""
        if Path(argv[0]).name in ("opencode", "opencode.exe") and argv[0] != str(shared):
            return b"2.2.5\n"
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", runner)
    if npm_os == "win32":
        assert migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)["version"] == "2.2.5"
    else:
        with pytest.raises(ValueError, match="^invalid package metadata$"):
            migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)
    work = Path(op.stage_dir) / "provider-candidate-work"
    argv, limits, options = calls[0]
    assert argv == [str(node), str(script), "install", "--prefix", str(work / "prefix"), "--cache", str(work / "cache"), "--ignore-scripts", "--no-save", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org/", "@opencode/cli-windows-arm64@2.2.5"]
    assert limits == (600, 16384, 16384) and options["cwd"] == work and options["combined_cap"] == 16384
    expected = {"PATH": str(node.parent) + os.pathsep + os.defpath, "HOME": str(work), "USERPROFILE": str(work), "TEMP": str(work), "TMP": str(work), "NPM_CONFIG_USERCONFIG": str(work / "user.npmrc"), "NPM_CONFIG_GLOBALCONFIG": str(work / "global.npmrc"), "NPM_CONFIG_UPDATE_NOTIFIER": "false"}
    expected.update({key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ})
    assert options["env"] == expected


def test_stage_replaced_work_is_not_cleaned(stage_installer, tmp_path):
    _, _, _, setup, stage = stage_installer
    work = tmp_path / "1.2.3" / "provider-candidate-work"
    def replace(_):
        work.rename(work.with_name("displaced-work"))
        work.mkdir()
        (work / "foreign").write_bytes(b"preserve")
    setup["after_install"] = replace
    with pytest.raises((OSError, ValueError)):
        stage()
    assert (work / "foreign").read_bytes() == b"preserve"


def test_stage_rejects_package_component_replacement(stage_installer, monkeypatch):
    _, _, migration, _, stage = stage_installer
    original = migration._open_candidate_file
    def replace(path, *args, **kwargs):
        fd = original(path, *args, **kwargs)
        if path.name in ("opencode", "opencode.exe") and path.parent.name == "bin":
            path.parent.rename(path.parent.with_name("old-bin"))
            path.parent.mkdir()
            (path.parent / "opencode").write_bytes(b"replacement")
        return fd
    monkeypatch.setattr(migration, "_open_candidate_file", replace)
    with pytest.raises(ValueError):
        stage()


def test_stage_does_not_recursively_delete_by_path(stage_installer, tmp_path, monkeypatch):
    import shutil
    _, _, _, setup, stage = stage_installer
    work = tmp_path / "1.2.3" / "provider-candidate-work"
    sentinel = tmp_path / "unrelated"
    sentinel.write_bytes(b"preserve")
    original = shutil.rmtree
    def replace_at_delete(path, *args, **kwargs):
        if sys.platform != "win32":
            pytest.fail("POSIX cleanup must use held directory descriptors")
        assert path == work
        return original(path, *args, **kwargs)
    monkeypatch.setattr(shutil, "rmtree", replace_at_delete)
    assert stage()["version"] == "2.2.5"
    assert not work.exists()
    assert sentinel.read_bytes() == b"preserve"


def test_stage_accepts_ordinary_extra_npm_metadata(stage_installer):
    op, _, migration, setup, stage = stage_installer
    setup["metadata"].update(description="native package", license="MIT", extra={"nested": True})
    assert stage()["version"] == "2.2.5"
    assert json.loads((migration.Path(op.stage_dir) / "provider-candidate" / "candidate.json").read_text())["version"] == "2.2.5"


def test_retained_work_blocks_success_and_retry_until_manual_recovery(stage_installer, tmp_path, monkeypatch):
    op, _, migration, setup, stage = stage_installer
    cleanup = migration._remove_candidate_work
    def fail_cleanup(path, identity, validate_only=False):
        if not validate_only:
            raise OSError("injected cleanup failure")
        return cleanup(path, identity, validate_only)
    monkeypatch.setattr(migration, "_remove_candidate_work", fail_cleanup)
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    work = tmp_path / "1.2.3" / "provider-candidate-work"
    candidate = tmp_path / "1.2.3" / "provider-candidate"
    descriptor = json.loads((candidate / "candidate.json").read_text())
    assert migration.verify_candidate(op, descriptor["sha256"], state_dir=tmp_path) == candidate / descriptor["relative_binary"]
    marker = work / "sentinel"
    marker.write_bytes(b"preserve")
    before = [(str(p.relative_to(tmp_path)), p.lstat().st_mtime_ns) for p in tmp_path.rglob("*")]
    with pytest.raises(ValueError, match="^candidate work requires manual recovery$"):
        stage()
    assert marker.read_bytes() == b"preserve"
    assert before == [(str(p.relative_to(tmp_path)), p.lstat().st_mtime_ns) for p in tmp_path.rglob("*")]
    assert len(setup["calls"]) == 1
    assert not (tmp_path / "providers").exists()
    shutil.rmtree(work)  # This fixture created this exact tree.
    monkeypatch.setattr(migration, "_remove_candidate_work", cleanup)
    result = stage()
    assert result == {"version": descriptor["version"], "sha256": descriptor["sha256"], "relative_binary": descriptor["relative_binary"]}
    assert len(setup["calls"]) == 1


def test_candidate_descriptor_rejects_extra_key(published_candidate, tmp_path):
    op, migration, _, metadata, sha, _ = published_candidate
    data = json.loads(metadata.read_text())
    data["extra"] = True
    metadata.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="candidate verification failed"):
        migration.verify_candidate(op, sha, state_dir=tmp_path)


@pytest.mark.parametrize("fault", ["extra", "package", "version", "operation", "hash", "binary", "owner", "acl", "target", "checker", "capability"])
def test_published_retry_rejects_tampering_without_install(published_candidate, tmp_path, monkeypatch, fault):
    import os
    from ciao.os_support import private
    from ciao.providers import opencode
    op, migration, binary, metadata, sha, observations = published_candidate
    approved = migration.snapshot_id(migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=tmp_path))
    data = json.loads(metadata.read_text())
    inspected = []
    if fault in ("extra", "package", "version", "operation", "hash"):
        key, value = {"extra": ("extra", True), "package": ("package", "bad"), "version": ("version", "2.2.6"), "operation": ("operation_id", "other"), "hash": ("sha256", "a" * 64)}[fault]
        data[key] = value
        metadata.write_text(json.dumps(data))
    elif fault == "binary":
        binary.write_bytes(b"tampered")
    elif fault == "owner":
        original_owner = private.is_owned_descriptor
        inode = binary.stat().st_ino
        def owner(fd):
            if os.fstat(fd).st_ino == inode:
                inspected.append("binary-owner")
                return False
            return original_owner(fd)
        monkeypatch.setattr(private, "is_owned_descriptor", owner)
    elif fault == "acl":
        original_acl = private.descriptor_acl_grants_another_account
        inode = binary.stat().st_ino
        def acl(fd):
            if os.fstat(fd).st_ino == inode:
                inspected.append("binary-acl")
                return True
            return original_acl(fd)
        monkeypatch.setattr(private, "descriptor_acl_grants_another_account", acl)
    elif fault == "target":
        original = migration._target_probe
        def target(*args):
            result = original(*args)
            if args[-1] == "2.2.5":
                result["candidate_error"] = "rejected"
            return result
        monkeypatch.setattr(migration, "_target_probe", target)
    elif fault == "checker":
        original_check = opencode._server_version_error
        def check(value):
            if value.get("version") == "2.2.5":
                inspected.append("running-checker")
                return "rejected"
            return original_check(value)
        monkeypatch.setattr(opencode, "_server_version_error", check)
    else:
        original = migration._target_probe
        def capability(*args):
            result = original(*args)
            if args[-1] == "2.2.5":
                result["managed_resolver_version"] = 0
            return result
        monkeypatch.setattr(migration, "_target_probe", capability)
    with pytest.raises(ValueError, match="^candidate verification failed$"):
        if fault == "checker":
            # Exercise the verifier, not preflight's stale-approval rejection.
            migration.verify_candidate(op, sha, state_dir=tmp_path)
        else:
            migration.stage_opencode_candidate(op, approved, frozenset({"opencode"}), state_dir=tmp_path)
    if fault in ("owner", "acl", "checker"):
        assert inspected == [{"owner": "binary-owner", "acl": "binary-acl", "checker": "running-checker"}[fault]]
    assert not any("install" in argv for argv in observations)


def test_verify_candidate_accepts_private_executable_without_writes(published_candidate, tmp_path):
    op, migration, binary, metadata, sha, observations = published_candidate
    before = [(p, p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")]
    assert migration.verify_candidate(op, sha, state_dir=tmp_path) == binary
    assert observations
    assert before == [(p, p.lstat().st_mtime_ns, p.read_bytes() if p.is_file() else None) for p in tmp_path.rglob("*")]


@pytest.mark.parametrize("fault", ["mode", "hash", "metadata", "symlink", "root", "id", "version", "acl"])
def test_verify_candidate_refuses_untrusted_state(published_candidate, tmp_path, monkeypatch, fault):
    import os
    from ciao.os_support import private
    op, migration, binary, metadata, sha, observations = published_candidate
    inspected = []
    if fault == "mode":
        if sys.platform == "win32":
            inode = binary.stat().st_ino
            original = private.is_private_descriptor_strict
            def binary_dacl(fd):
                if os.fstat(fd).st_ino == inode:
                    inspected.append("binary-dacl")
                    return False
                return original(fd)
            monkeypatch.setattr(private, "is_private_descriptor_strict", binary_dacl)
        else:
            binary.chmod(0o600)
    elif fault == "hash":
        binary.write_bytes(b"replacement")
    elif fault == "metadata":
        metadata.write_text('{"schema":1,"schema":1}')
    elif fault == "symlink":
        binary.unlink()
        binary.symlink_to(metadata)
    elif fault == "root":
        if sys.platform == "win32":
            inode = tmp_path.stat().st_ino
            original = private.is_private_strict
            def root_dacl(path):
                if path == tmp_path and path.stat().st_ino == inode:
                    inspected.append("root-dacl")
                    return False
                return original(path)
            monkeypatch.setattr(private, "is_private_strict", root_dacl)
        else:
            tmp_path.chmod(0o755)
    elif fault == "id":
        op.id = "../bad"
    elif fault == "version":
        data = json.loads(metadata.read_text())
        data["version"] = "3.0.0"
        metadata.write_text(json.dumps(data))
    else:
        inode = binary.stat().st_ino
        original = private.descriptor_acl_grants_another_account
        def binary_acl(fd):
            if os.fstat(fd).st_ino == inode:
                inspected.append("binary-acl")
                return True
            return original(fd)
        monkeypatch.setattr(private, "descriptor_acl_grants_another_account", binary_acl)
    with pytest.raises(ValueError, match="candidate verification failed"):
        migration.verify_candidate(op, sha, state_dir=tmp_path)
    if fault in ("id", "root"):
        assert not observations
    if fault == "acl":
        assert inspected == ["binary-acl"]
    if sys.platform == "win32" and fault in ("mode", "root"):
        assert inspected == ["binary-dacl" if fault == "mode" else "root-dacl"]


@pytest.mark.parametrize("change", ["operation", "metadata"])
def test_verify_candidate_rejects_changes_during_probe(published_candidate, tmp_path, monkeypatch, change):
    op, migration, binary, metadata, sha, _ = published_candidate
    original = migration._run_bounded
    def changed(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if argv[0] == str(binary):
            if change == "operation":
                op.id = "20261112T101501Z-1.2.3"
            else:
                metadata.write_text(metadata.read_text() + " ")
        return result
    monkeypatch.setattr(migration, "_run_bounded", changed)
    with pytest.raises(ValueError, match="candidate verification failed"):
        migration.verify_candidate(op, sha, state_dir=tmp_path)


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


@pytest.fixture
def managed_receipt(tmp_path):
    from ciao.os_support import private

    private.make_private_dir(tmp_path)
    root = tmp_path / "updates"
    provider = tmp_path / "providers" / "opencode"
    operation = "20261112T101500Z-1.2.3"
    name = "opencode.exe" if sys.platform == "win32" else "opencode"
    version_dir = provider / "versions" / operation
    for directory in (provider.parent, provider, provider / "versions", version_dir):
        directory.mkdir()
        private.make_private_dir(directory)
    binary = version_dir / name
    binary.write_bytes(b"private executable")
    if sys.platform == "win32":
        private.make_private(binary)
    else:
        binary.chmod(0o700)
    receipt = provider / "active.json"
    data = {"schema": 1, "operation_id": operation, "version": "2.0.16",
            "relative_binary": f"versions/{operation}/{name}",
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    receipt.write_text(json.dumps(data))
    private.make_private(receipt)
    return root, provider, binary, receipt, data


def test_managed_resolver_valid_and_explicit_state(managed_receipt, monkeypatch):
    from ciao import engine_update, provider_migration
    root, _, binary, _, _ = managed_receipt
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: pytest.fail("default state used"))
    assert provider_migration.managed_opencode_binary(state_dir=root) == str(binary)


def test_managed_resolver_absent_receipt(tmp_path):
    from ciao.os_support.private import make_private_dir
    from ciao.provider_migration import managed_opencode_binary
    make_private_dir(tmp_path)
    assert managed_opencode_binary(state_dir=tmp_path / "updates") is None


@pytest.mark.parametrize("corruption", ["missing_binary", "receipt_link", "dangling_receipt", "directory_link", "escape", "hash", "private", "operation", "json", "schema", "extra", "relative", "version"])
def test_managed_resolver_refuses_corruption(managed_receipt, corruption, tmp_path, monkeypatch):
    import traceback
    from ciao.provider_migration import managed_opencode_binary
    root, provider, binary, receipt, data = managed_receipt
    if corruption == "missing_binary":
        binary.unlink()
    elif corruption in ("receipt_link", "dangling_receipt"):
        receipt.unlink()
        target = tmp_path / "target"
        if corruption == "receipt_link":
            target.write_text("secret-sentinel")
        try:
            receipt.symlink_to(target)
        except OSError:
            from ciao.os_support import files
            receipt.write_text("unavailable link")
            original_open = files.open_fd
            def reject_link(path, *args, **kwargs):
                if path == receipt:
                    raise OSError("reparse point")
                return original_open(path, *args, **kwargs)
            monkeypatch.setattr(files, "open_fd", reject_link)
    elif corruption == "directory_link":
        original = provider / "versions"
        original.rename(provider / "moved")
        try:
            original.symlink_to(provider / "moved", target_is_directory=True)
        except OSError:
            from pathlib import Path
            original.mkdir()
            is_junction = Path.is_junction
            monkeypatch.setattr(Path, "is_junction", lambda path: path == original or is_junction(path))
    elif corruption == "private":
        if sys.platform == "win32":
            from ciao.os_support import private
            monkeypatch.setattr(private, "is_private_descriptor_strict", lambda fd: False)
        else:
            binary.chmod(0o755)
    elif corruption == "json":
        receipt.write_text("secret-sentinel{")
    else:
        if corruption == "escape": data["relative_binary"] = "../../secret-sentinel"
        if corruption == "hash": data["sha256"] = "a" * 64
        if corruption == "operation": data["operation_id"] = "../secret-sentinel"
        if corruption == "schema": data["schema"] = True
        if corruption == "extra": data["secret-sentinel"] = 1
        if corruption == "relative": data["relative_binary"] = "versions/other/opencode"
        if corruption == "version": data["version"] = "secret-sentinel"
        receipt.write_text(json.dumps(data))
    with pytest.raises(OSError) as caught:
        managed_opencode_binary(state_dir=root)
    assert type(caught.value) is OSError
    assert str(caught.value) == "OpenCode is unavailable; check provider setup."
    assert caught.value.__suppress_context__
    assert "secret-sentinel" not in "".join(traceback.format_exception(caught.value))
    assert str(tmp_path) not in "".join(traceback.format_exception(caught.value))


def test_managed_resolver_absent_receipt_in_private_tree(managed_receipt):
    from ciao.provider_migration import managed_opencode_binary
    root, _, _, receipt, _ = managed_receipt
    receipt.unlink()
    assert managed_opencode_binary(state_dir=root) is None


def test_managed_resolver_refuses_unowned_file(managed_receipt, monkeypatch):
    from ciao import provider_migration
    from ciao.os_support import private
    root, _, _, _, _ = managed_receipt
    monkeypatch.setattr(private, "is_owned_descriptor", lambda fd: False)
    with pytest.raises(OSError, match="^OpenCode is unavailable; check provider setup\\.$") as caught:
        provider_migration.managed_opencode_binary(state_dir=root)
    assert type(caught.value) is OSError


def test_managed_resolver_refuses_nonprivate_directory(managed_receipt, monkeypatch):
    from ciao import provider_migration
    from ciao.os_support import private
    root, provider, _, _, _ = managed_receipt
    original = private.is_private_strict
    monkeypatch.setattr(private, "is_private_strict", lambda path: False if path == provider else original(path))
    with pytest.raises(OSError, match="^OpenCode is unavailable; check provider setup\\.$"):
        provider_migration.managed_opencode_binary(state_dir=root)


@pytest.fixture
def activation_candidate(published_candidate, tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.os_support.private import make_private_dir
    from ciao import engine_update
    op, migration, old_binary, _, sha, _ = published_candidate
    old_stage = Path(op.stage_dir)
    root = tmp_path / "updates"
    root.mkdir(mode=0o700)
    make_private_dir(tmp_path)
    make_private_dir(root)
    stage = root / op.to_version
    shutil.copytree(old_stage, stage, symlinks=True)
    make_private_dir(stage)
    make_private_dir(stage / "provider-candidate")
    old_python = op.env_python
    op.env_python = str(stage / Path(old_python).relative_to(old_stage))
    op.wheel = str(stage / Path(op.wheel).relative_to(old_stage))
    op.stage_dir = str(stage)
    binary = stage / "provider-candidate" / old_binary.name
    runner = migration._run_bounded
    def run(argv, *args, **kwargs):
        argv = list(argv)
        if argv[0] == op.env_python:
            argv[0] = old_python
        elif argv[0] == str(binary):
            argv[0] = str(old_binary)
        return runner(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", run)
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: pytest.fail("default state used"))
    return op, migration, root, binary, sha


def test_activation_publishes_verified_candidate_and_reuses_it(activation_candidate, tmp_path):
    op, migration, root, binary, sha = activation_candidate
    shared = tmp_path / "shared-cli"
    shared.write_bytes(b"untouched")
    result = migration.activate_candidate(op, sha, state_dir=root)
    assert result == str(tmp_path / "providers" / "opencode" / "versions" / op.id / binary.name)
    assert migration.managed_opencode_binary(state_dir=root) == result
    receipt = json.loads((tmp_path / "providers" / "opencode" / "active.json").read_text())
    assert receipt == {"schema": 1, "operation_id": op.id, "version": "2.2.5", "relative_binary": f"versions/{op.id}/{binary.name}", "sha256": sha}
    assert migration.activate_candidate(op, sha, state_dir=root) == result
    assert shared.read_bytes() == b"untouched"


@pytest.mark.parametrize("field,value", [("id", "../20261112T101500Z-1.2.3"), ("id", "20261112T101500Z-1.2.4"), ("stage_dir", "relative/1.2.3")])
def test_activation_rejects_operation_before_verification(activation_candidate, monkeypatch, field, value):
    op, migration, root, _, sha = activation_candidate
    setattr(op, field, value)
    monkeypatch.setattr(migration, "verify_candidate", lambda *a, **kw: pytest.fail("invalid operation was probed"))
    with pytest.raises(ValueError):
        migration.activate_candidate(op, sha, state_dir=root)


def test_activation_refuses_corrupt_existing_receipt(activation_candidate, tmp_path):
    op, migration, root, _, sha = activation_candidate
    migration.activate_candidate(op, sha, state_dir=root)
    receipt = tmp_path / "providers" / "opencode" / "active.json"
    receipt.write_text("bad")
    with pytest.raises((ValueError, OSError)):
        migration.activate_candidate(op, sha, state_dir=root)
    assert receipt.read_text() == "bad"


def test_activation_refuses_final_collision(activation_candidate, tmp_path):
    from ciao.os_support.private import make_private, make_private_dir
    op, migration, root, binary, sha = activation_candidate
    versions = tmp_path / "providers" / "opencode" / "versions"
    for directory in (versions.parent.parent, versions.parent, versions, versions / op.id):
        directory.mkdir()
        make_private_dir(directory)
    foreign = versions / op.id / binary.name
    foreign.write_bytes(b"foreign")
    if sys.platform == "win32":
        make_private(foreign)
    else:
        foreign.chmod(0o700)
    fd = migration._candidate_open(foreign, 0o700)
    import os
    os.close(fd)
    with pytest.raises((ValueError, OSError)):
        migration.activate_candidate(op, sha, state_dir=root)
    assert foreign.read_bytes() == b"foreign"
    assert not (versions.parent / "active.json").exists()


@pytest.mark.parametrize("when", ["before", "after"])
def test_activation_receipt_crash_is_retryable(activation_candidate, tmp_path, monkeypatch, when):
    import os
    op, migration, root, binary, sha = activation_candidate
    old = migration.activate_candidate(op, sha, state_dir=root)
    receipt = tmp_path / "providers" / "opencode" / "active.json"
    old_bytes = receipt.read_bytes()
    op.id = "20261112T101501Z-1.2.3"
    metadata = binary.parent / "candidate.json"
    data = json.loads(metadata.read_text())
    data["operation_id"] = op.id
    metadata.write_text(json.dumps(data))
    original = os.replace
    def interrupted(src, dst, *args, **kwargs):
        if str(dst).endswith("active.json"):
            if when == "after":
                original(src, dst, *args, **kwargs)
            raise OSError("simulated crash")
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="simulated crash"):
        migration.activate_candidate(op, sha, state_dir=root)
    current = migration.managed_opencode_binary(state_dir=root)
    if when == "before":
        assert receipt.read_bytes() == old_bytes
        assert current == old
    else:
        assert current == str(tmp_path / "providers" / "opencode" / "versions" / op.id / binary.name)
    monkeypatch.setattr(os, "replace", original)
    expected = str(tmp_path / "providers" / "opencode" / "versions" / op.id / binary.name)
    assert migration.activate_candidate(op, sha, state_dir=root) == expected
    assert migration.managed_opencode_binary(state_dir=root) == expected


def test_activation_refuses_target_resolver_change(activation_candidate, preflight):
    op, migration, root, _, sha = activation_candidate
    _, state, _, _, _ = preflight
    state["resolver"] = 0
    with pytest.raises(ValueError):
        migration.activate_candidate(op, sha, state_dir=root)
    assert not (root.parent / "providers").exists()


def test_activation_publishes_final_before_receipt(activation_candidate, monkeypatch):
    import os
    op, migration, root, binary, sha = activation_candidate
    events = []
    publish = migration._publish_candidate
    replace = os.replace
    def observe_publish(source, destination):
        events.append("final")
        return publish(source, destination)
    def observe_replace(src, dst, *args, **kwargs):
        if str(dst).endswith("active.json"):
            final = root.parent / "providers" / "opencode" / "versions" / op.id / binary.name
            assert final.read_bytes() == binary.read_bytes()
            events.append("receipt")
        return replace(src, dst, *args, **kwargs)
    monkeypatch.setattr(migration, "_publish_candidate", observe_publish)
    monkeypatch.setattr(os, "replace", observe_replace)
    migration.activate_candidate(op, sha, state_dir=root)
    assert events == ["final", "receipt"]


def test_activation_refuses_wrong_candidate_hash(activation_candidate):
    op, migration, root, _, _ = activation_candidate
    with pytest.raises(ValueError):
        migration.activate_candidate(op, "a" * 64, state_dir=root)
    assert not (root.parent / "providers").exists()


def test_activation_refuses_running_checker_rejection(activation_candidate, monkeypatch):
    from ciao.providers import opencode
    op, migration, root, _, sha = activation_candidate
    monkeypatch.setattr(opencode, "_server_version_error", lambda _: "rejected")
    with pytest.raises(ValueError):
        migration.activate_candidate(op, sha, state_dir=root)
    assert not (root.parent / "providers").exists()


@pytest.mark.parametrize("disappears", ["provider", "ancestor"])
def test_managed_resolver_raced_ancestor_never_falls_back(managed_receipt, monkeypatch, disappears):
    from pathlib import Path
    from ciao import engine_update
    from ciao.providers import opencode
    root, provider, _, receipt, _ = managed_receipt
    original = Path.lstat
    victim = provider if disappears == "provider" else provider.parent
    trigger = receipt if disappears == "provider" else provider
    moved = root.parent / ("moved-" + disappears)
    triggered = False
    def raced(path, *args, **kwargs):
        nonlocal triggered
        if path == trigger and not triggered:
            triggered = True
            victim.rename(moved)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", raced)
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: root)
    monkeypatch.delenv("CIAO_OPENCODE_BIN", raising=False)
    monkeypatch.setattr(opencode, "resolve_tool", lambda _: pytest.fail("shared fallback"))
    with pytest.raises(OSError) as caught:
        opencode.resolve_opencode_binary()
    assert triggered
    assert type(caught.value) is OSError
    assert str(caught.value) == "OpenCode is unavailable; check provider setup."
    assert caught.value.__suppress_context__


def test_activation_syncs_each_new_parent_before_next_publication(activation_candidate, monkeypatch):
    import os
    import stat
    op, migration, root, _, sha = activation_candidate
    events = []
    descriptors = {}
    create = migration._make_candidate_directory
    open_dir = migration._candidate_directory_fd
    sync = os.fsync
    publish = migration._publish_candidate
    def track_create(path):
        result = create(path)
        events.append(("create", path))
        return result
    def track_open(path):
        fd = open_dir(path)
        descriptors[fd] = path
        return fd
    def track_sync(fd):
        if fd in descriptors and stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append(("sync", descriptors[fd]))
        return sync(fd)
    def track_publish(src, dst):
        events.append(("publish", dst))
        return publish(src, dst)
    monkeypatch.setattr(migration, "_make_candidate_directory", track_create)
    monkeypatch.setattr(migration, "_candidate_directory_fd", track_open)
    monkeypatch.setattr(os, "fsync", track_sync)
    monkeypatch.setattr(migration, "_publish_candidate", track_publish)
    migration.activate_candidate(op, sha, state_dir=root)
    if sys.platform != "win32":
        providers = root.parent / "providers"
        provider = providers / "opencode"
        versions = provider / "versions"
        assert events.index(("create", providers)) < events.index(("sync", root.parent)) < events.index(("create", provider))
        assert events.index(("create", provider)) < events.index(("sync", providers)) < events.index(("create", versions))
        assert events.index(("create", versions)) < events.index(("sync", provider)) < events.index(("publish", versions / op.id))


@pytest.mark.parametrize("failure", ["raise", "mismatch"])
def test_activation_does_not_succeed_if_own_readback_fails(activation_candidate, monkeypatch, failure):
    import os
    op, migration, root, _, sha = activation_candidate
    original_replace = os.replace
    original_resolve = migration.managed_opencode_binary
    replaced = False
    def replace(src, dst, *args, **kwargs):
        nonlocal replaced
        result = original_replace(src, dst, *args, **kwargs)
        if str(dst).endswith("active.json"):
            replaced = True
        return result
    def resolve(*, state_dir=None):
        if replaced:
            if failure == "raise":
                raise OSError("readback failed")
            return None
        return original_resolve(state_dir=state_dir)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(migration, "managed_opencode_binary", resolve)
    with pytest.raises((ValueError, OSError)):
        migration.activate_candidate(op, sha, state_dir=root)
    assert replaced
    assert original_resolve(state_dir=root) is not None


def test_staging_preserves_existing_active_provider(activation_candidate, preflight, monkeypatch):
    from pathlib import Path
    op, migration, root, binary, sha = activation_candidate
    old = migration.activate_candidate(op, sha, state_dir=root)
    receipt = root.parent / "providers" / "opencode" / "active.json"
    before = receipt.read_bytes()
    binary.parent.rename(binary.parent.with_name("saved-candidate"))
    runner = migration._run_bounded
    def run(argv, *args, **kwargs):
        if "install" in argv:
            prefix = Path(argv[argv.index("--prefix") + 1])
            package = prefix / "node_modules" / "@opencode" / "cli-darwin-arm64"
            (package / "bin").mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({
                "name": "@opencode/cli-darwin-arm64", "version": "2.2.5",
                "os": ["darwin"], "cpu": ["arm64"],
            }))
            executable = package / "bin" / binary.name
            executable.write_bytes(b"\xcf\xfa\xed\xfe" + b"native fixture" * 30)
            if sys.platform != "win32":
                executable.chmod(0o700)
            return b""
        if Path(argv[0]).name in ("opencode", "opencode.exe") and "provider-candidate" in argv[0]:
            return b"2.2.5\n"
        return runner(argv, *args, **kwargs)
    monkeypatch.setattr(migration, "_run_bounded", run)
    snapshot = migration.provider_snapshot(op, frozenset({"opencode"}), state_dir=root)
    assert snapshot["can_apply"]
    result = migration.stage_opencode_candidate(op, migration.snapshot_id(snapshot), frozenset({"opencode"}), state_dir=root)
    assert result["sha256"] != sha
    assert receipt.read_bytes() == before
    assert migration.managed_opencode_binary(state_dir=root) == old


@pytest.mark.parametrize("entry", ["providers", "opencode", "versions", "final"])
def test_activation_retry_syncs_existing_parent_before_receipt(activation_candidate, monkeypatch, entry):
    import os
    import stat
    op, migration, root, _, sha = activation_candidate
    providers = root.parent / "providers"
    provider = providers / "opencode"
    versions = provider / "versions"
    entries = {"providers": providers, "opencode": provider,
               "versions": versions, "final": versions / op.id}
    created = entries[entry]
    parent = created.parent
    real_sync = os.fsync
    real_replace = os.replace
    events = []
    failed = False

    def sync(fd):
        nonlocal failed
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and created.exists():
            parent_info = parent.stat()
            if (info.st_dev, info.st_ino) == (parent_info.st_dev, parent_info.st_ino):
                events.append("parent-sync")
                if not failed:
                    failed = True
                    raise OSError("injected parent sync failure")
        return real_sync(fd)

    def replace(src, dst, *args, **kwargs):
        if str(dst).endswith("active.json"):
            events.append("receipt-replace")
        return real_replace(src, dst, *args, **kwargs)

    if sys.platform == "win32":
        # Windows does not expose this POSIX directory-fsync path.
        assert migration.activate_candidate(op, sha, state_dir=root)
        return
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="injected parent sync failure"):
        migration.activate_candidate(op, sha, state_dir=root)
    assert failed and created.is_dir()
    assert "receipt-replace" not in events
    events.clear()
    result = migration.activate_candidate(op, sha, state_dir=root)
    assert result == migration.managed_opencode_binary(state_dir=root)
    assert events.index("parent-sync") < events.index("receipt-replace")
