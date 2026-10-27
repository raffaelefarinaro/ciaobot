from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import asyncio

import pytest

from ciao.fs_sandbox import (
    gh_config_read_root,
    clear_gh_token_cache,
    gh_token_for_sandbox,
    gws_tool_read_roots,
    SANDBOX_EXEC_PATH,
    FsSandboxUnavailable,
    _SYSTEM_READ_SUBPATHS,
    _quote_seatbelt_subpath,
    _seatbelt_profile,
    claude_file_tool_guard,
    claude_sandbox_settings,
    engine_read_roots,
    node_tool_read_roots,
    path_outside_roots,
    opencode_sandbox_prefix,
)


def _writable_rules(profile: str) -> int:
    """Write grants outside the fixed device rule (/dev/null, the terminal)."""
    return sum(
        1 for line in profile.splitlines() if "file-write*" in line and "/dev/null" not in line
    )


def test_claude_workspace_settings_allow_only_the_roots(tmp_path, monkeypatch):
    roots = [tmp_path / "root-a", tmp_path / "root-b"]
    # A fixed engine folder: the real install's symlink hops depend on the host.
    engine = tmp_path / "engine"
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine])

    auto = claude_sandbox_settings(scope="workspace", mode="auto", roots=roots)
    assert auto == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        # Network is open: the sandbox confines files only (#1234).
        "network": {"allowedDomains": ["*"], "allowLocalBinding": True},
        "filesystem": {
            "denyRead": [str(Path.home())],
            # The engine's own install is readable so the agent can run
            # `ciao` (#1185); it is never writable.
            "allowRead": [str(roots[0]), str(roots[1]), str(engine)],
            "allowWrite": [str(roots[0]), str(roots[1])],
            "denyWrite": [],
        },
    }

    normal = claude_sandbox_settings(scope="workspace", mode="normal", roots=roots)
    assert normal is not None
    assert normal["autoAllowBashIfSandboxed"] is False
    assert normal["filesystem"] == auto["filesystem"]

    assert claude_sandbox_settings(scope="machine", mode="auto", roots=roots) is None

    with pytest.raises(ValueError):
        claude_sandbox_settings(scope="workspace", mode="auto", roots=[])


def test_opencode_prefix_is_sandbox_exec_on_darwin(tmp_path, monkeypatch):
    fake_binary = tmp_path / "sandbox-exec"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.SANDBOX_EXEC_PATH", str(fake_binary))

    root = tmp_path / "root"
    argv = opencode_sandbox_prefix(roots=[root])

    assert argv[0] == str(fake_binary)
    assert argv[1] == "-p"
    profile = argv[2]
    assert f'(subpath "{_quote_seatbelt_subpath(str(root))}")' in profile
    # Write access exists only on the given root: the system paths are
    # read-only and there is no global file-write allow.
    assert _writable_rules(profile) == 1
    # Traversal of `/` itself is allowed (without it every child aborts under
    # `deny default`), and process rules carry no `*` suffix (a parse error).
    # The /var and /tmp symlinks are readable too, so a path spelled through
    # them (as $TMPDIR is) still resolves (#1174).
    assert '(allow file-read* (literal "/") (literal "/var") (literal "/tmp"))' in profile
    assert "(allow process-exec)" in profile
    assert "(allow process-fork)" in profile


def test_opencode_prefix_is_bwrap_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)

    root = tmp_path / "root"
    argv = opencode_sandbox_prefix(roots=[root])

    assert argv[0] == "/usr/bin/bwrap"
    assert "--bind" in argv
    idx = argv.index("--bind")
    assert argv[idx + 1] == str(root)
    assert argv[idx + 2] == str(root)
    if Path("/usr").exists():
        assert "--ro-bind" in argv
        ro_idx = argv.index("--ro-bind")
        assert argv[ro_idx + 1] == "/usr"
        assert argv[ro_idx + 2] == "/usr"
    assert argv[-1] == "--"


def test_missing_tool_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(FsSandboxUnavailable, match="whole machine"):
        opencode_sandbox_prefix(roots=[tmp_path])

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "ciao.fs_sandbox.SANDBOX_EXEC_PATH", str(tmp_path / "no-such-binary")
    )
    with pytest.raises(FsSandboxUnavailable, match="whole machine"):
        opencode_sandbox_prefix(roots=[tmp_path])


def test_darwin_sandbox_exec_refuses_a_file_outside_the_root(tmp_path):
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file():
        # Platform branch, not a skip marker: on Linux and Windows this probe
        # cannot run, and the prefix behavior is covered by the tests above.
        try:
            prefix = opencode_sandbox_prefix(roots=[tmp_path])
        except FsSandboxUnavailable:
            pass
        else:
            assert "bwrap" in prefix[0]
        return

    # Resolve: pytest tmp dirs live under /var (a symlink to /private/var),
    # and seatbelt matches the resolved vnode path, so both the profile root
    # and every access below must use the canonical path.
    root = tmp_path / "root"
    root.mkdir()
    resolved = root.resolve()
    inside = resolved / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    # The outside file must live somewhere the profile does not allow for
    # reading: /private (and the rest of the system list) is readable by
    # design, so a second tmp file would be the wrong probe — and the suite's
    # conftest fakes $HOME into tmp, so $HOME is out too. /etc is outside
    # every allowed subpath and /etc/hosts always exists on macOS.
    outside = Path("/etc/hosts")
    assert outside.is_file()
    prefix = opencode_sandbox_prefix(roots=[resolved])
    denied = subprocess.run(
        [*prefix, "/bin/cat", str(outside)], capture_output=True, timeout=30
    )
    assert denied.returncode != 0
    allowed = subprocess.run(
        [*prefix, "/bin/cat", str(inside)], capture_output=True, timeout=30
    )
    assert allowed.returncode == 0
    assert allowed.stdout == b"inside"


def test_seatbelt_backslash_root_is_escaped():
    # POSIX crafted name (literal backslash + "n") and Windows-style roots
    # must both be escaped for the Seatbelt string, never decoded.
    assert _quote_seatbelt_subpath("root\\nbar") == "root\\\\nbar"
    assert _quote_seatbelt_subpath("C:\\Users\\root") == "C:\\\\Users\\\\root"
    with pytest.raises(ValueError):
        _quote_seatbelt_subpath('ro"ot')
    with pytest.raises(ValueError):
        _quote_seatbelt_subpath("ro)ot")

    # Keep the crafted POSIX name literal on Windows without creating a file:
    # Windows treats a backslash as a path separator, not a filename character.
    tricky = PurePosixPath("/root\\nbar")
    profile = _seatbelt_profile([tricky])
    # The grant names the literal-backslash root (doubled backslash) and
    # never the escape-decoded sibling (real newline).
    assert "root\\\\nbar" in profile
    assert "root\nbar" not in profile


def test_darwin_seatbelt_backslash_root_stays_confined(tmp_path):
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file():
        # Same platform branch as the live probe above: escaping is covered
        # by the unit test on Linux/Windows, where sandbox-exec is absent.
        assert _quote_seatbelt_subpath("a\\b") == "a\\\\b"
        return

    # Resolve: pytest tmp dirs live under /var (a symlink to /private/var),
    # and seatbelt matches the resolved vnode path.
    tricky = tmp_path / "root\\nbar"
    tricky.mkdir()
    resolved = tricky.resolve()
    sibling = tmp_path / "root\nbar"
    sibling.mkdir()
    resolved_sibling = sibling.resolve()
    profile = _seatbelt_profile([resolved])
    granted = subprocess.run(
        [SANDBOX_EXEC_PATH, "-p", profile, "/usr/bin/touch", str(resolved / "ok.txt")],
        capture_output=True,
        timeout=30,
    )
    assert granted.returncode == 0
    assert (resolved / "ok.txt").is_file()
    denied = subprocess.run(
        [
            SANDBOX_EXEC_PATH,
            "-p",
            profile,
            "/usr/bin/touch",
            str(resolved_sibling / "evil.txt"),
        ],
        capture_output=True,
        timeout=30,
    )
    assert denied.returncode != 0
    assert not (resolved_sibling / "evil.txt").is_file()


def test_read_only_dirs_are_readable_and_never_writable(tmp_path, monkeypatch):
    root, config = tmp_path / "root", tmp_path / "config"
    # bwrap skips a read-only path that does not exist (#1945); a real config dir does.
    config.mkdir()
    profile = _seatbelt_profile([root], read_only=[config])
    assert f'(allow file-read* (subpath "{_quote_seatbelt_subpath(str(config))}"))' in profile
    assert _writable_rules(profile) == 1

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    argv = opencode_sandbox_prefix(roots=[root], read_only=[config])
    at = argv.index(str(config))
    assert argv[at - 1] == "--ro-bind"
    assert ["--bind", str(config), str(config)] not in [argv[i : i + 3] for i in range(len(argv))]


def test_darwin_seatbelt_reaches_a_root_spelled_through_var(tmp_path):
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file():
        assert '(literal "/var")' in _seatbelt_profile([tmp_path])
        return
    # $TMPDIR is spelled /var/folders/...; /var is a symlink to /private/var.
    # Without read on the link itself every such path is refused (#1174).
    unresolved = Path("/var") / tmp_path.resolve().relative_to("/private/var")
    target = unresolved / "made.txt"
    granted = subprocess.run(
        [SANDBOX_EXEC_PATH, "-p", _seatbelt_profile([tmp_path.resolve()]),
         "/usr/bin/touch", str(target)],
        capture_output=True,
        timeout=30,
    )
    assert granted.returncode == 0, granted.stderr
    assert target.is_file()


def test_tool_paths_outside_the_roots_are_detected(tmp_path):
    root = (tmp_path / "root").resolve()
    (root / "sub").mkdir(parents=True)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()

    def out(value: str) -> bool:
        return path_outside_roots(value, cwd=root, roots=[root])

    assert not out("notes.md")
    assert not out("**/*.py")
    assert not out(str(root / "sub" / "*.md"))
    assert out(str(outside / "secret.txt"))
    assert out("../outside/secret.txt")
    assert out("/**")
    assert out("~/anything")
    if sys.platform != "win32":
        # Windows needs a privilege to create symlinks; the guard only runs on
        # POSIX, where Claude workspace scope is available.
        (root / "escape").symlink_to(outside)
        assert out("escape/secret.txt")


def _guard_decision(guard, tool_name: str, tool_input: dict, session_id: str | None = None) -> str:
    hook_input: dict = {"tool_name": tool_name, "tool_input": tool_input}
    if session_id is not None:
        hook_input["session_id"] = session_id
    result = asyncio.run(guard(hook_input, None, None))
    return result.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


def test_claude_file_tool_guard_denies_paths_outside_the_roots(tmp_path):
    root = (tmp_path / "root").resolve()
    vault = (tmp_path / "vault").resolve()
    root.mkdir()
    vault.mkdir()
    guard = claude_file_tool_guard(cwd=root, roots=[root, vault])

    assert _guard_decision(guard, "Read", {"file_path": str(root / "a.md")}) == "allow"
    assert _guard_decision(guard, "Edit", {"file_path": str(vault / "n.md")}) == "allow"
    assert _guard_decision(guard, "Glob", {"pattern": "**/*.md"}) == "allow"
    plan = Path.home() / ".claude" / "plans" / "p.md"
    assert _guard_decision(guard, "Write", {"file_path": str(plan)}) == "allow"
    assert _guard_decision(guard, "Bash", {"command": "cat /etc/hosts"}) == "allow"

    assert _guard_decision(guard, "Read", {"file_path": "/etc/hosts"}) == "deny"
    assert _guard_decision(guard, "Write", {"file_path": str(tmp_path / "x")}) == "deny"
    assert _guard_decision(guard, "Grep", {"pattern": "x", "path": str(tmp_path)}) == "deny"
    assert _guard_decision(guard, "Glob", {"pattern": f"{tmp_path}/**"}) == "deny"
    assert _guard_decision(guard, "NotebookEdit", {"notebook_path": "/tmp/n.ipynb"}) == "deny"

    with pytest.raises(ValueError):
        claude_file_tool_guard(cwd=root, roots=[])


_TASK_SESSION = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


def _task_guard_setup(tmp_path):
    root = (tmp_path / "root").resolve()
    root.mkdir()
    tmp_base = (tmp_path / "clitmp").resolve()
    tasks = tmp_base / f"claude-{os.getuid()}" / "-munged-cwd" / _TASK_SESSION / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "abc.output").write_text("HELLO", encoding="utf-8")
    guard = claude_file_tool_guard(cwd=root, roots=[root], cli_tmp_base=str(tmp_base))
    return root, tmp_base, tasks, guard


def _read_task(guard, path, session_id=_TASK_SESSION) -> str:
    return _guard_decision(guard, "Read", {"file_path": str(path)}, session_id)


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_path_allowed_own_session(tmp_path):
    _, _, tasks, guard = _task_guard_setup(tmp_path)
    assert _read_task(guard, tasks / "abc.output") == "allow"


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_denied_other_session_or_workspace(tmp_path):
    _, tmp_base, tasks, guard = _task_guard_setup(tmp_path)
    other = tmp_base / f"claude-{os.getuid()}" / "-munged-cwd" / "ffffffff-0000-1111" / "tasks"
    other.mkdir(parents=True)
    (other / "x.output").write_text("secret", encoding="utf-8")
    assert _read_task(guard, other / "x.output") == "deny"
    # Same session id under another workspace's directory is still this
    # session's shape, but a different session id never matches.
    assert _read_task(guard, tasks / "abc.output", "ffffffff-0000-1111") == "deny"
    other_uid = tmp_base / f"claude-{os.getuid() + 1}" / "-munged-cwd" / _TASK_SESSION / "tasks"
    other_uid.mkdir(parents=True)
    (other_uid / "x.output").write_text("secret", encoding="utf-8")
    assert _read_task(guard, other_uid / "x.output") == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_denied_for_non_read_tools(tmp_path):
    _, _, tasks, guard = _task_guard_setup(tmp_path)
    path = str(tasks / "abc.output")
    for tool, tool_input in (
        ("Write", {"file_path": path, "content": "x"}),
        ("Edit", {"file_path": path}),
        ("Glob", {"pattern": "*.output", "path": str(tasks)}),
        ("Grep", {"pattern": "x", "path": str(tasks)}),
    ):
        assert _guard_decision(guard, tool, tool_input, _TASK_SESSION) == "deny", tool


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_denied_without_session_id(tmp_path):
    _, _, tasks, guard = _task_guard_setup(tmp_path)
    path = tasks / "abc.output"
    assert _guard_decision(guard, "Read", {"file_path": str(path)}) == "deny"
    assert _read_task(guard, path, "") == "deny"
    assert _read_task(guard, path, "short") == "deny"
    assert _read_task(guard, path, "bad/../id/12345678") == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_denied_for_symlink_escape(tmp_path):
    _, tmp_base, tasks, guard = _task_guard_setup(tmp_path)
    secret = tmp_path / "secret.output"
    secret.write_text("secret", encoding="utf-8")
    (tasks / "link.output").symlink_to(secret)
    assert _read_task(guard, tasks / "link.output") == "deny"
    # A symlinked tasks directory pointing at another session is refused too.
    other = tmp_base / f"claude-{os.getuid()}" / "-munged-cwd" / "ffffffff-0000-1111" / "tasks"
    other.mkdir(parents=True)
    (other / "x.output").write_text("secret", encoding="utf-8")
    session_dir = tasks.parent.parent / "0a1b2c3d-9999-9999"
    session_dir.symlink_to(other.parent)
    assert _read_task(guard, session_dir / "tasks" / "x.output", "0a1b2c3d-9999-9999") == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_denied_for_wrong_shape(tmp_path):
    _, _, tasks, guard = _task_guard_setup(tmp_path)
    (tasks / "abc.txt").write_text("x", encoding="utf-8")
    (tasks / "sub").mkdir()
    (tasks / "sub" / "n.output").write_text("x", encoding="utf-8")
    (tasks.parent / "extra.output").write_text("x", encoding="utf-8")
    assert _read_task(guard, tasks / "abc.txt") == "deny"
    assert _read_task(guard, tasks / "sub" / "n.output") == "deny"
    assert _read_task(guard, tasks.parent / "extra.output") == "deny"
    assert _read_task(guard, f"{tasks}/../tasks/abc.output") == "allow"  # collapses to the file
    assert _read_task(guard, f"{tasks}/../../abc.output") == "deny"
    assert _read_task(guard, f"{tasks}/*.output") == "deny"
    assert _read_task(guard, f"{tasks}/a?c.output") == "deny"
    assert _read_task(guard, f"{tasks}/[a]bc.output") == "deny"
    assert _read_task(guard, f"{tasks}/{{abc}}.output") == "deny"


@pytest.mark.skipif(sys.platform == "win32", reason="the guard only runs on POSIX")
def test_claude_task_output_respects_cli_tmp_base(tmp_path):
    root, tmp_base, tasks, _ = _task_guard_setup(tmp_path)
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    guard = claude_file_tool_guard(cwd=root, roots=[root], cli_tmp_base=str(elsewhere))
    assert _read_task(guard, tasks / "abc.output") == "deny"
    no_base = claude_file_tool_guard(cwd=root, roots=[root])
    assert _read_task(no_base, tasks / "abc.output") == "deny"


def test_seatbelt_lists_the_ancestors_of_granted_paths_only(tmp_path):
    root = tmp_path / "a" / "b" / "root"
    config = tmp_path / "cfg" / "opencode"
    profile = _seatbelt_profile([root], read_only=[config])
    for ancestor in (root.parent, root.parent.parent, config.parent):
        assert f'(literal "{_quote_seatbelt_subpath(str(ancestor))}")' in profile
    # An ancestor is an entry, never a subtree: nothing below it but the grant.
    assert f'(subpath "{_quote_seatbelt_subpath(str(root.parent))}")' not in profile


def test_darwin_seatbelt_allows_dev_null(tmp_path):
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file():
        assert '(literal "/dev/null")' in _seatbelt_profile([tmp_path])
        return
    base = tmp_path.resolve()
    root = base / "root"
    root.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    profile = _seatbelt_profile([root])
    shell = subprocess.run(
        [SANDBOX_EXEC_PATH, "-p", profile, "/bin/sh", "-c",
         f"cat {root / 'inside.txt'} </dev/null >/dev/null && echo ok"],
        capture_output=True, text=True, timeout=30,
    )
    assert shell.stdout.strip() == "ok", shell.stderr


def test_bwrap_mounts_the_fresh_tmp_before_the_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    root = Path("/tmp/ciao-workspace")

    argv = opencode_sandbox_prefix(roots=[root])

    # A root under /tmp must survive the fresh /tmp, so the tmpfs comes first.
    assert argv.index("--tmpfs") < argv.index(str(root))


def test_a_bwrap_that_cannot_sandbox_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: False)

    with pytest.raises(FsSandboxUnavailable, match="AppArmor"):
        opencode_sandbox_prefix(roots=[tmp_path])


def test_bwrap_usable_reads_the_probe_exit_code(monkeypatch):
    from ciao import fs_sandbox

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, b"", b"bwrap: setting up uid map: Permission denied")

    monkeypatch.setattr(fs_sandbox.subprocess, "run", fake_run)
    fs_sandbox._bwrap_usable.cache_clear()
    try:
        assert fs_sandbox._bwrap_usable("/usr/bin/bwrap") is False
        assert calls[0][0] == "/usr/bin/bwrap"
    finally:
        fs_sandbox._bwrap_usable.cache_clear()


def test_engine_read_roots_cover_the_venv_and_every_interpreter_link(tmp_path, monkeypatch):
    if sys.platform == "win32":
        # Platform branch, not a skip marker: Windows has no sandbox, so these
        # grants are never used there, and its symlinks resolve differently.
        return
    from ciao import fs_sandbox

    real = tmp_path / "python" / "cpython-3.13.13"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.13").write_text("")
    alias = tmp_path / "python" / "cpython-3.13"
    alias.symlink_to(real)
    venv = tmp_path / "tools" / "ciaobot"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(alias / "bin" / "python3.13")
    monkeypatch.setattr(fs_sandbox.sys, "prefix", str(venv))
    monkeypatch.setattr(fs_sandbox.sys, "base_prefix", str(real))
    monkeypatch.setattr(fs_sandbox.sys, "executable", str(venv / "bin" / "python"))
    # sysconfig caches its scheme under the real prefix; a patched prefix must
    # not reach it, so its paths are stubbed to the venv instead.
    venv_paths = {"scripts": str(venv / "bin"), "purelib": str(venv / "lib"), "platlib": str(venv / "lib")}
    monkeypatch.setattr("sysconfig.get_path", lambda name, *a, **k: venv_paths[name])
    # The result is cached per process; these inputs are patched, so start cold.
    engine_read_roots.cache_clear()
    try:
        granted = engine_read_roots()
    finally:
        engine_read_roots.cache_clear()

    # The venv, the interpreter, and the version alias uv links through: seatbelt
    # refuses the exec unless each link on the way is readable (#1185).
    assert venv in granted
    assert real.resolve() in granted
    assert alias in granted
    # The package folder: an editable install keeps it outside the venv.
    assert Path(fs_sandbox.__file__).resolve().parent in granted


def test_claude_denies_writes_to_an_engine_inside_a_root(tmp_path, monkeypatch):
    # #1208: the roots grant Claude write access, so an engine folder inside one
    # is denied writes by name, as OpenCode's profile shadows it.
    root = tmp_path / "root"
    engine = root / "engine"
    outside = tmp_path / "elsewhere"
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine, outside])
    monkeypatch.setattr(sys, "platform", "darwin")

    settings = claude_sandbox_settings(scope="workspace", mode="auto", roots=[root])

    assert settings["filesystem"]["denyWrite"] == [str(engine)]
    assert str(engine) in settings["filesystem"]["allowRead"]
    assert str(engine) not in settings["filesystem"]["allowWrite"]


def test_engine_install_is_read_only_in_every_profile(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    # bwrap skips a read-only path that does not exist (#1945); a real install does.
    engine.mkdir()
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine])
    root = tmp_path / "root"

    settings = claude_sandbox_settings(scope="workspace", mode="auto", roots=[root])
    assert str(engine) in settings["filesystem"]["allowRead"]
    assert str(engine) not in settings["filesystem"]["allowWrite"]

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    argv = opencode_sandbox_prefix(roots=[root])
    at = argv.index(str(engine))
    assert argv[at - 1] == "--ro-bind"


def test_bwrap_binds_where_resolv_conf_points(tmp_path, monkeypatch):
    run = tmp_path / "run" / "systemd" / "resolve"
    run.mkdir(parents=True)
    (run / "stub-resolv.conf").write_text("nameserver 127.0.0.53\n")
    link = tmp_path / "resolv.conf"
    link.symlink_to(run / "stub-resolv.conf")
    monkeypatch.setattr("ciao.fs_sandbox._BWRAP_ETC_LINKS", (str(link),))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)

    argv = opencode_sandbox_prefix(roots=[tmp_path / "root"])

    # systemd-resolved links /etc/resolv.conf into /run, which is not bound;
    # without its target every lookup in the sandbox fails (#1186).
    target = str(run.resolve())
    at = argv.index(target)
    assert argv[at - 1] == "--ro-bind"


def test_seatbelt_lets_opencode_resolve_config_names_in_ancestors(tmp_path):
    root = tmp_path / "workspace" / "agent"
    profile = _seatbelt_profile([root])

    # OpenCode realpaths `.claude` (and its siblings) in every ancestor of its
    # cwd; a refused realpath fails the whole prompt with a 500 (#1197).
    ancestor = tmp_path / "workspace"
    for name in (".claude", "AGENTS.md"):
        literal = f'(literal "{_quote_seatbelt_subpath(f"{ancestor}/{name}")}")'
        assert literal in profile
    # A literal, never a subpath: the folder opens, nothing inside it is read.
    assert f'(subpath "{_quote_seatbelt_subpath(f"{ancestor}/.claude")}")' not in profile
    assert _writable_rules(profile) == 1


def test_seatbelt_gives_claude_md_metadata_only_in_ancestors(tmp_path):
    # #1208: CLAUDE.md is realpathed in every ancestor but its contents stay
    # unreadable, so it is a metadata grant and never a read grant.
    root = tmp_path / "workspace" / "agent"
    profile = _seatbelt_profile([root])
    claude_md = _quote_seatbelt_subpath(f"{tmp_path / 'workspace'}/CLAUDE.md")
    literal = f'(literal "{claude_md}")'
    metadata = [line for line in profile.splitlines() if "file-read-metadata" in line]
    assert any(literal in line for line in metadata)
    assert literal not in "\n".join(
        line for line in profile.splitlines() if "file-read*" in line
    )
    assert _writable_rules(profile) == 1


def _bwrap_triples(argv: list[str]) -> list[list[str]]:
    return [argv[i : i + 3] for i in range(len(argv) - 2)]


def test_bwrap_recreates_an_alias_outside_the_bound_folders(tmp_path, monkeypatch):
    if sys.platform == "win32":
        # Platform branch, not a skip marker: creating a symlink needs a privilege
        # Windows does not grant by default, and bwrap does not run there.
        return
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])

    real = tmp_path / "python" / "cpython-3.13.13"
    real.mkdir(parents=True)
    # uv's version alias, outside every bound folder: the sandbox must get it.
    alias = tmp_path / "python" / "cpython-3.13"
    alias.symlink_to(real)
    root = tmp_path / "root"
    root.mkdir()
    # A link under the writable root is already bound, so it is left alone.
    inside = root / "alias"
    inside.symlink_to(real)

    argv = opencode_sandbox_prefix(roots=[root], read_only=[real, alias, inside])

    triples = _bwrap_triples(argv)
    assert ["--symlink", os.readlink(alias), str(alias)] in triples
    assert not [t for t in triples if t[0] == "--symlink" and t[2] == str(inside)]
    assert ["--ro-bind", str(real), str(real)] in triples
    assert not [t for t in triples if t[2] == str(inside)]


def test_claude_allow_read_has_symlink_hops_only_on_darwin(tmp_path, monkeypatch):
    if sys.platform == "win32":
        # Platform branch, not a skip marker: symlinks need a privilege on Windows.
        return
    real = tmp_path / "python" / "cpython"
    real.mkdir(parents=True)
    alias = tmp_path / "python" / "alias"
    alias.symlink_to(real)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [real, alias])
    root = tmp_path / "root"

    monkeypatch.setattr(sys, "platform", "linux")
    linux = claude_sandbox_settings(scope="workspace", mode="auto", roots=[root])
    # bubblewrap cannot bind a symlink: the folder is granted, the hop is not.
    assert linux["filesystem"]["allowRead"] == [str(root), str(real)]

    monkeypatch.setattr(sys, "platform", "darwin")
    darwin = claude_sandbox_settings(scope="workspace", mode="auto", roots=[root])
    assert darwin["filesystem"]["allowRead"] == [str(root), str(real), str(alias)]


def test_symlink_hops_normalize_relative_chains_with_dotdot(tmp_path):
    if sys.platform == "win32":
        # Platform branch, not a skip marker: symlinks need a privilege on Windows.
        return
    from ciao.fs_sandbox import _symlink_hops

    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "real").write_text("")
    (tmp_path / "b").mkdir()
    link2 = tmp_path / "b" / "link2"
    link2.symlink_to("../c/real")
    (tmp_path / "a").mkdir()
    link1 = tmp_path / "a" / "link1"
    link1.symlink_to("../b/link2")

    # Each relative target joins onto its link's folder as ``a/../b/link2``;
    # the hops must come out as the normalized paths seatbelt would match.
    hops = _symlink_hops(link1)
    assert hops == [link1, link2]
    assert all(".." not in hop.parts for hop in hops)


def test_engine_read_roots_include_the_sysconfig_install_paths(tmp_path, monkeypatch):
    import sysconfig

    from ciao import fs_sandbox

    real_get_path = sysconfig.get_path
    scripts = tmp_path / "home" / ".local" / "bin"
    purelib = tmp_path / "home" / ".local" / "lib" / "python3.13" / "site-packages"
    platlib = tmp_path / "home" / ".local" / "lib64" / "python3.13" / "site-packages"
    paths = {"scripts": str(scripts), "purelib": str(purelib), "platlib": str(platlib)}
    # A pip --user install: the packages and console scripts sit outside sys.prefix.
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name, *a, **k: paths[name] if name in paths else real_get_path(name, *a, **k),
    )
    monkeypatch.setattr(fs_sandbox.sys, "prefix", str(tmp_path / "system"))
    monkeypatch.setattr(fs_sandbox.sys, "base_prefix", str(tmp_path / "system"))
    monkeypatch.setattr(fs_sandbox.sys, "executable", str(tmp_path / "system" / "bin" / "python"))
    fs_sandbox.engine_read_roots.cache_clear()
    try:
        granted = fs_sandbox.engine_read_roots()
    finally:
        fs_sandbox.engine_read_roots.cache_clear()

    assert scripts in granted
    assert purelib in granted
    assert platlib in granted
    # De-duplicated: no folder is listed twice.
    assert len(granted) == len(set(granted))


def test_engine_read_roots_add_the_user_scheme_for_a_user_install(tmp_path, monkeypatch):
    import site
    import sysconfig

    from ciao import fs_sandbox

    home = (tmp_path / "home").resolve()
    user_site = home / ".local" / "lib" / "python3.13" / "site-packages"
    user_paths = {"purelib": str(user_site), "scripts": str(home / ".local" / "bin")}
    system = tmp_path / "system"
    # Only the user scheme is stubbed, and the default scheme points at the prefix:
    # the sysconfig cache is never touched, so the real install keeps its paths.
    monkeypatch.setattr(
        sysconfig,
        "get_path",
        lambda name, *a, scheme=None, **k: user_paths[name] if scheme else str(system / name),
    )
    monkeypatch.setattr(sysconfig, "get_preferred_scheme", lambda key: "posix_user")
    monkeypatch.setattr(site, "ENABLE_USER_SITE", True)
    monkeypatch.setattr(site, "getusersitepackages", lambda: str(user_site))
    monkeypatch.setattr(fs_sandbox, "__file__", str(user_site / "ciao" / "fs_sandbox.py"))
    fs_sandbox.engine_read_roots.cache_clear()
    try:
        granted = fs_sandbox.engine_read_roots()
    finally:
        fs_sandbox.engine_read_roots.cache_clear()
    assert user_site in granted
    assert home / ".local" / "bin" in granted

    # The same stubs with the package outside the user site: nothing is added.
    monkeypatch.setattr(fs_sandbox, "__file__", str(tmp_path / "checkout" / "ciao" / "fs_sandbox.py"))
    fs_sandbox.engine_read_roots.cache_clear()
    try:
        assert user_site not in fs_sandbox.engine_read_roots()
    finally:
        fs_sandbox.engine_read_roots.cache_clear()


def test_engine_read_roots_are_computed_once_per_process(tmp_path, monkeypatch):
    from ciao import fs_sandbox

    calls: list[Path] = []
    real_hops = fs_sandbox._symlink_hops

    def counting_hops(start: Path) -> list[Path]:
        calls.append(start)
        return real_hops(start)

    monkeypatch.setattr(fs_sandbox, "_symlink_hops", counting_hops)
    fs_sandbox.engine_read_roots.cache_clear()
    try:
        first = fs_sandbox.engine_read_roots()
        second = fs_sandbox.engine_read_roots()
    finally:
        fs_sandbox.engine_read_roots.cache_clear()

    assert first == second
    assert len(calls) == 1


def test_engine_inside_a_writable_root_is_denied_writes_on_darwin(tmp_path, monkeypatch):
    fake_binary = tmp_path / "sandbox-exec"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.SANDBOX_EXEC_PATH", str(fake_binary))
    root = tmp_path / "root"
    engine = root / "engine"
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine])

    argv = opencode_sandbox_prefix(roots=[root])

    lines = argv[2].splitlines()
    allow = f'(allow file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(root))}"))'
    deny = f'(deny file-write* (subpath "{_quote_seatbelt_subpath(str(engine))}"))'
    # Last matching rule wins: the deny must follow the writable root's allow.
    assert allow in lines and deny in lines
    assert lines.index(deny) > lines.index(allow)
    # The engine is already readable under the root, so it gets no separate grant.
    assert f'(subpath "{_quote_seatbelt_subpath(str(engine))}")' not in argv[2].replace(deny, "")


def test_engine_inside_a_writable_root_is_read_only_after_the_bind_on_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    root = tmp_path / "root"
    engine = root / "engine"
    engine.mkdir(parents=True)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine])

    argv = opencode_sandbox_prefix(roots=[root])

    triples = _bwrap_triples(argv)
    bind_at = [i for i, t in enumerate(triples) if t == ["--bind", str(root), str(root)]]
    ro_at = [i for i, t in enumerate(triples) if t == ["--ro-bind", str(engine), str(engine)]]
    # Exactly one read-only mount of the engine, and it comes after the writable bind
    # so it sits on top of it.
    assert len(bind_at) == 1 and len(ro_at) == 1
    assert ro_at[0] > bind_at[0]


def test_node_tool_roots_keep_npm_code_read_only_and_exclude_home_cache(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # Windows does not use these sandbox grants or POSIX npm symlinks.
    install = tmp_path / "node-install"
    binary = install / "bin"
    binary.mkdir(parents=True)
    node = binary / "node"
    node.write_text("node")
    package = install / "lib" / "node_modules" / "npm"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text("{}")
    for tool in ("npm", "npx"):
        entry = package / "bin" / f"{tool}-cli.js"
        entry.write_text("npm code")
        (binary / tool).symlink_to(entry)
    monkeypatch.setattr(shutil, "which", lambda tool, path: str(binary / tool))
    roots = node_tool_read_roots(str(binary))
    assert node in roots
    assert package in roots
    assert binary / "npx" in roots
    assert install not in roots
    assert tmp_path not in roots
    assert tmp_path / ".npm" not in roots


def test_gws_tool_roots_grant_npm_package_and_node_not_home(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # Windows does not use these sandbox grants or POSIX symlinks.
    home = tmp_path / "home"
    package = home / ".local" / "lib" / "node_modules" / "@googleworkspace" / "cli"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text("{}")
    run_js = package / "run.js"
    run_js.write_text("#!/usr/bin/env node")
    (package / "bin" / "gws").write_text("native")
    link_dir = home / ".local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "gws"
    link.symlink_to(run_js)
    node_dir = tmp_path / "node-install" / "bin"
    node_dir.mkdir(parents=True)
    node = node_dir / "node"
    node.write_text("node")
    found = {"gws": str(link), "node": str(node)}
    monkeypatch.setattr(shutil, "which", lambda tool, path: found.get(tool))

    roots = gws_tool_read_roots("ignored")

    assert package.resolve() in roots
    assert link in roots
    assert run_js.resolve() in roots
    assert node in roots
    for denied in (home, home / ".local", link_dir):
        assert denied not in roots
        assert denied.resolve() not in roots


def test_gws_tool_roots_empty_when_not_installed(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda tool, path: None)
    assert gws_tool_read_roots("ignored") == []


def test_gws_tool_roots_plain_binary_skips_node(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gws = bin_dir / "gws"
    gws.write_text("native")
    node = bin_dir / "node"
    node.write_text("node")
    found = {"gws": str(gws), "node": str(node)}
    monkeypatch.setattr(shutil, "which", lambda tool, path: found.get(tool))

    roots = gws_tool_read_roots("ignored")

    assert gws in roots
    assert node not in roots
    assert bin_dir not in roots


def test_gws_tool_roots_skip_package_json_outside_node_modules(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # Windows does not use these sandbox grants or POSIX symlinks.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gws = bin_dir / "gws"
    gws.write_text("#!/usr/bin/env node")
    (bin_dir / "package.json").write_text("{}")
    node = tmp_path / "node-install" / "node"
    node.parent.mkdir()
    node.write_text("node")
    found = {"gws": str(gws), "node": str(node)}
    monkeypatch.setattr(shutil, "which", lambda tool, path: found.get(tool))

    roots = gws_tool_read_roots("ignored")

    assert roots == [gws]
    assert bin_dir not in roots
    assert node not in roots


def test_gh_config_root_prefers_gh_config_dir(tmp_path):
    config = tmp_path / "custom-gh"
    config.mkdir()
    xdg = tmp_path / "xdg"
    (xdg / "gh").mkdir(parents=True)

    root = gh_config_read_root({"GH_CONFIG_DIR": str(config), "XDG_CONFIG_HOME": str(xdg)})

    assert root == config.resolve()


def test_gh_config_root_uses_xdg_then_home(tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    (xdg / "gh").mkdir(parents=True)
    assert gh_config_read_root({"XDG_CONFIG_HOME": str(xdg)}) == (xdg / "gh").resolve()

    home = tmp_path / "home"
    (home / ".config" / "gh").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert gh_config_read_root({}) == (home / ".config" / "gh").resolve()


def test_gh_config_root_missing_returns_none(tmp_path):
    assert gh_config_read_root({"GH_CONFIG_DIR": str(tmp_path / "absent")}) is None


def test_gh_token_for_sandbox_returns_token(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "tok\n", "")

    monkeypatch.setattr("ciao.fs_sandbox.subprocess.run", fake_run)

    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"


def test_gh_token_for_sandbox_none_cases(monkeypatch):
    calls = []

    def run_returning(result):
        def fake_run(args, **kwargs):
            calls.append(args)
            if isinstance(result, BaseException):
                raise result
            return result

        return fake_run

    monkeypatch.setattr("ciao.fs_sandbox.subprocess.run", run_returning(None))
    assert gh_token_for_sandbox(None, {}) is None
    assert gh_token_for_sandbox("/bin/gh", {"GH_TOKEN": "x"}) is None
    assert gh_token_for_sandbox("/bin/gh", {"GITHUB_TOKEN": "x"}) is None
    assert calls == []

    failed = subprocess.CompletedProcess(["gh"], 1, "tok\n", "")
    empty = subprocess.CompletedProcess(["gh"], 0, "\n", "")
    for outcome in (
        failed,
        empty,
        subprocess.TimeoutExpired("gh", 5),
        OSError("boom"),
    ):
        monkeypatch.setattr("ciao.fs_sandbox.subprocess.run", run_returning(outcome))
        assert gh_token_for_sandbox("/bin/gh", {}) is None


def _gh_run(monkeypatch, outcomes):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(args, *outcome)

    monkeypatch.setattr("ciao.fs_sandbox.subprocess.run", fake_run)
    return calls


def test_gh_token_cache_hit_skips_subprocess(monkeypatch):
    clear_gh_token_cache()
    calls = _gh_run(monkeypatch, [(0, "tok\n", "")])

    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"
    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"
    assert len(calls) == 1


def test_gh_token_cached_only_never_runs_subprocess(monkeypatch):
    clear_gh_token_cache()
    calls = _gh_run(monkeypatch, [(0, "tok\n", "")])

    assert gh_token_for_sandbox("/bin/gh", {}, cached_only=True) is None
    assert calls == []
    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"
    assert gh_token_for_sandbox("/bin/gh", {}, cached_only=True) == "tok"
    assert len(calls) == 1
    # Past the TTL a cache-only read still returns the last good token: a
    # retry or steer after a long turn must not lose GH_TOKEN.
    monkeypatch.setattr("ciao.fs_sandbox._GH_TOKEN_TTL", 0.0)
    assert gh_token_for_sandbox("/bin/gh", {}, cached_only=True) == "tok"
    assert len(calls) == 1


def test_gh_token_failure_not_cached(monkeypatch):
    clear_gh_token_cache()
    calls = _gh_run(monkeypatch, [(1, "", "no"), (0, "tok\n", "")])

    assert gh_token_for_sandbox("/bin/gh", {}) is None
    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"
    assert len(calls) == 2


def test_gh_token_logout_evicts_the_cached_token(monkeypatch):
    clear_gh_token_cache()
    _gh_run(monkeypatch, [(0, "tok\n", ""), (1, "", "not logged in")])

    assert gh_token_for_sandbox("/bin/gh", {}) == "tok"
    monkeypatch.setattr("ciao.fs_sandbox._GH_TOKEN_TTL", 0.0)
    assert gh_token_for_sandbox("/bin/gh", {}) is None
    assert gh_token_for_sandbox("/bin/gh", {}, cached_only=True) is None


def test_gh_token_runs_expected_argv(monkeypatch):
    clear_gh_token_cache()
    calls = _gh_run(monkeypatch, [(0, "tok\n", "")])

    gh_token_for_sandbox("/bin/gh", {})

    assert calls == [["/bin/gh", "auth", "token", "--hostname", "github.com"]]


def test_claude_settings_grant_read_only_files_without_write(tmp_path, monkeypatch):
    root = tmp_path / "root-a"
    granted = tmp_path / "runtime" / "workspaces.json"
    engine = tmp_path / "engine"
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [engine])

    settings = claude_sandbox_settings(
        scope="workspace", mode="auto", roots=[root], read_only=[granted]
    )

    assert settings is not None
    filesystem = settings["filesystem"]
    assert filesystem["allowRead"] == [str(root), str(granted), str(engine)]
    assert str(granted) not in filesystem["allowWrite"]


def test_claude_settings_drop_read_only_symlinks_on_linux(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # Creating a symlink needs a privilege Windows does not grant by default.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)

    settings = claude_sandbox_settings(
        scope="workspace", mode="auto", roots=[tmp_path / "root"], read_only=[link, target]
    )

    assert settings is not None
    allow_read = settings["filesystem"]["allowRead"]
    assert str(target) in allow_read
    assert str(link) not in allow_read


def test_bwrap_grants_gws_link_package_and_node(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # bwrap does not run on Windows and symlinks need a privilege there.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    home = tmp_path / "home"
    package = home / ".local" / "lib" / "node_modules" / "@googleworkspace" / "cli"
    package.mkdir(parents=True)
    (package / "package.json").write_text("{}")
    run_js = package / "run.js"
    run_js.write_text("#!/usr/bin/env node")
    link_dir = home / ".local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "gws"
    link.symlink_to(run_js)
    node_dir = tmp_path / "node-install" / "bin"
    node_dir.mkdir(parents=True)
    node = node_dir / "node"
    node.write_text("node")

    argv = opencode_sandbox_prefix(
        roots=[tmp_path / "root"], read_only=[link, run_js, package, node]
    )

    triples = _bwrap_triples(argv)
    assert ["--symlink", os.readlink(link), str(link)] in triples
    assert ["--ro-bind", str(package), str(package)] in triples
    assert ["--ro-bind", str(node), str(node)] in triples
    # run.js comes before the package in read_only and is not under an earlier
    # bind (``_under``), so it is bound itself; the package, not being under
    # run.js, is bound as well. The link is never bound, only recreated.
    assert ["--ro-bind", str(run_js), str(run_js)] in triples
    assert ["--ro-bind", str(link), str(link)] not in triples
    for denied in (home, home / ".local", link_dir):
        assert ["--ro-bind", str(denied), str(denied)] not in triples
        assert ["--bind", str(denied), str(denied)] not in triples


def test_bwrap_skips_missing_read_only_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    existing = tmp_path / "workspaces.json"
    existing.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.json"

    argv = opencode_sandbox_prefix(roots=[tmp_path / "root"], read_only=[existing, missing])

    triple = ["--ro-bind", str(existing), str(existing)]
    assert any(argv[i : i + 3] == triple for i in range(len(argv)))
    assert str(missing) not in argv


def test_darwin_seatbelt_reads_a_granted_runtime_file_only(tmp_path):
    if sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file():
        try:
            prefix = opencode_sandbox_prefix(roots=[tmp_path])
        except FsSandboxUnavailable:
            pass
        else:
            assert "bwrap" in prefix[0]
        return

    base = tmp_path.resolve()
    root = base / "root"
    root.mkdir()
    receipt = base / "runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("receipt", encoding="utf-8")
    prefix = opencode_sandbox_prefix(roots=[root], read_only=[receipt])

    allowed = subprocess.run([*prefix, "/bin/cat", str(receipt)], capture_output=True, timeout=30)
    assert allowed.returncode == 0
    assert allowed.stdout == b"receipt"
    # pytest's tmp lives under /private, which the profile reads anyway, so the
    # read above passes without the grant: assert the grant on the profile text.
    profile = prefix[2]
    assert f'(allow file-read* (subpath "{_quote_seatbelt_subpath(str(receipt))}"))' in profile
    assert not any(
        "file-write*" in line and str(receipt) in line for line in profile.splitlines()
    )
    ok = root / "ok.txt"
    control = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo ok > '{ok}'"], capture_output=True, timeout=30
    )
    assert control.returncode == 0
    assert ok.read_text(encoding="utf-8").strip() == "ok"
    written = subprocess.run(
        [*prefix, "/bin/sh", "-c", f"echo x > '{receipt}'"], capture_output=True, timeout=30
    )
    assert written.returncode != 0
    assert receipt.read_text(encoding="utf-8") == "receipt"


def test_claude_settings_deny_write_on_read_only_inside_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    inside = root / "creds" / "client_secret.json"
    outside = tmp_path / "runtime" / "workspaces.json"

    settings = claude_sandbox_settings(
        scope="workspace", mode="auto", roots=[root], read_only=[inside, outside]
    )

    assert settings is not None
    filesystem = settings["filesystem"]
    assert str(inside) in filesystem["denyWrite"]
    assert str(inside) in filesystem["allowRead"]
    assert str(outside) not in filesystem["denyWrite"]


def test_seatbelt_denies_write_on_read_only_inside_root(tmp_path, monkeypatch):
    fake_binary = tmp_path / "sandbox-exec"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.SANDBOX_EXEC_PATH", str(fake_binary))
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    inside = root / "creds" / "client_secret.json"

    argv = opencode_sandbox_prefix(roots=[root], read_only=[inside])

    profile = argv[2]
    allow = f'(allow file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(root))}"))'
    deny = f'(deny file-write* (subpath "{_quote_seatbelt_subpath(str(inside))}"))'
    assert deny in profile
    assert profile.index(deny) > profile.index(allow)


def test_bwrap_ro_binds_read_only_inside_root_after_root_bind(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # bwrap does not run on Windows.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    creds = root / "creds"
    creds.mkdir(parents=True)
    pinned = creds / "client_secret.json"
    pinned.write_text("{}", encoding="utf-8")
    missing = creds / "credentials.json"

    argv = opencode_sandbox_prefix(roots=[root], read_only=[pinned, missing])

    triples = _bwrap_triples(argv)
    root_bind = triples.index(["--bind", str(root), str(root)])
    assert triples.index(["--ro-bind", str(pinned), str(pinned)]) > root_bind
    assert str(missing) not in argv


def _secrets_tree(tmp_path: Path) -> tuple[Path, Path, Path, Path, list[Path]]:
    """root/secrets with a linked account (gws-a), another account and a loose file."""
    root = tmp_path / "root"
    secrets = root / "secrets"
    linked = secrets / "gws-a"
    other = secrets / "gws-b"
    linked.mkdir(parents=True)
    other.mkdir()
    (linked / "client_secret.json").write_text("{}", encoding="utf-8")
    (linked / "credentials.json").write_text("{}", encoding="utf-8")
    (other / "credentials.json").write_text("{}", encoding="utf-8")
    (secrets / "other.json").write_text("{}", encoding="utf-8")
    pins = [linked / "client_secret.json", linked / "credentials.json"]
    return root, secrets, linked, other, pins


def _fake_darwin(tmp_path, monkeypatch):
    fake_binary = tmp_path / "sandbox-exec"
    fake_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.SANDBOX_EXEC_PATH", str(fake_binary))
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])


def test_seatbelt_denies_secrets_then_regrants_linked_after_system_allow(tmp_path, monkeypatch):
    _fake_darwin(tmp_path, monkeypatch)
    root, secrets, linked, _other, pins = _secrets_tree(tmp_path)

    argv = opencode_sandbox_prefix(
        roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )

    profile = argv[2]
    system = "(allow file-read* " + " ".join(f'(subpath "{p}")' for p in _SYSTEM_READ_SUBPATHS) + ")"
    allow = f'(allow file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(root))}"))'
    deny = f'(deny file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(secrets))}"))'
    regrant = f'(allow file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(linked))}"))'
    pin = f'(deny file-write* (subpath "{_quote_seatbelt_subpath(str(pins[0]))}"))'
    assert profile.count(deny) == profile.count(regrant) == 1
    assert profile.index(allow) < profile.index(system) < profile.index(deny)
    assert profile.index(deny) < profile.index(regrant) < profile.rindex(pin)


def test_seatbelt_is_unchanged_without_denied_paths(tmp_path):
    root, _secrets, _linked, _other, pins = _secrets_tree(tmp_path)

    plain = _seatbelt_profile([root], pins, pins)

    assert _seatbelt_profile([root], pins, pins, denied=(), regrant=()) == plain
    assert "file-read* file-write* (subpath" in plain
    assert "deny file-read*" not in plain


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file(),
    reason="needs macOS sandbox-exec",
)
def test_darwin_seatbelt_hides_other_accounts_and_keeps_linked(tmp_path):
    # tmp_path lives under /private, which the system read allow covers, so this
    # also proves the deny follows that allow.
    tmp_path = tmp_path.resolve()
    root, secrets, linked, other, pins = _secrets_tree(tmp_path)
    prefix = opencode_sandbox_prefix(
        roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )

    def run(command: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([*prefix, "/bin/sh", "-c", command], capture_output=True, timeout=30)

    assert run(f"cat '{other}/credentials.json'").returncode != 0
    assert run(f"cat '{secrets}/other.json'").returncode != 0
    assert run(f"cat '{linked}/client_secret.json'").returncode == 0
    assert run(f"echo x > '{linked}/token.json'").returncode == 0
    assert (linked / "token.json").read_text(encoding="utf-8").strip() == "x"
    assert run(f"echo x > '{linked}/credentials.json'").returncode != 0
    assert (linked / "credentials.json").read_text(encoding="utf-8") == "{}"


def _fake_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")
    monkeypatch.setattr("ciao.fs_sandbox._bwrap_usable", lambda bwrap: True)
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])


def test_bwrap_masks_secrets_between_root_bind_and_linked_bind(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # bwrap does not run on Windows.
    _fake_linux(monkeypatch)
    root, secrets, linked, other, pins = _secrets_tree(tmp_path)

    argv = opencode_sandbox_prefix(
        roots=[root], read_only=[*pins, other / "credentials.json"], denied=[secrets], regrant=[linked]
    )

    triples = _bwrap_triples(argv)
    root_bind = triples.index(["--bind", str(root), str(root)])
    mask_at = next(i for i, t in enumerate(triples) if t[:2] == ["--tmpfs", str(secrets)])
    linked_bind = triples.index(["--bind", str(linked), str(linked)])
    pin = triples.index(["--ro-bind", str(pins[0]), str(pins[0])])
    assert root_bind < mask_at < linked_bind < pin
    # A pin inside secrets but outside the linked account would re-expose host content.
    assert str(other / "credentials.json") not in argv


def test_bwrap_skips_secrets_mask_when_missing_or_symlink_or_outside_roots(tmp_path, monkeypatch):
    if sys.platform == "win32":
        return  # bwrap does not run on Windows.
    _fake_linux(monkeypatch)
    root = tmp_path / "root"
    root.mkdir()
    missing = root / "secrets"
    target = tmp_path / "target"
    target.mkdir()
    link = root / "linked-secrets"
    link.symlink_to(target)
    outside = tmp_path / "elsewhere" / "secrets"
    outside.mkdir(parents=True)

    for denied in (missing, link, outside):
        argv = opencode_sandbox_prefix(roots=[root], denied=[denied], regrant=[])
        assert "--tmpfs" not in argv[argv.index("/tmp") + 1 :]
    assert not missing.exists()


def test_claude_settings_secrets_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root, secrets, linked, _other, pins = _secrets_tree(tmp_path)

    settings = claude_sandbox_settings(
        scope="workspace", mode="auto", roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )

    assert settings is not None
    filesystem = settings["filesystem"]
    assert str(Path.home()) in filesystem["denyRead"]
    assert str(secrets) in filesystem["denyRead"]
    assert str(linked) in filesystem["allowRead"]
    assert str(linked) in filesystem["allowWrite"]
    assert str(secrets) not in filesystem["denyWrite"]
    for pin in pins:
        assert str(pin) in filesystem["denyWrite"]


def test_claude_settings_secrets_darwin(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root, secrets, linked, other, pins = _secrets_tree(tmp_path)

    settings = claude_sandbox_settings(
        scope="workspace", mode="auto", roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )

    assert settings is not None
    filesystem = settings["filesystem"]
    assert f"{secrets}/*" in filesystem["denyRead"]
    assert str(other) in filesystem["denyWrite"]
    assert str(secrets / "other.json") in filesystem["denyWrite"]
    assert str(linked) not in filesystem["denyWrite"]
    assert str(linked) in filesystem["allowRead"]
    assert str(linked) in filesystem["allowWrite"]


def _nested_secrets_tree(tmp_path: Path) -> tuple[Path, Path, Path, list[Path]]:
    """root/ws/secrets/{gws-a, gws-b}: the workspace sits one level below the root."""
    root = tmp_path / "root"
    ws = root / "ws"
    secrets = ws / "secrets"
    linked = secrets / "gws-a"
    (secrets / "gws-b").mkdir(parents=True)
    linked.mkdir()
    (linked / "credentials.json").write_text("{}", encoding="utf-8")
    return root, secrets, linked, [linked / "credentials.json"]


def test_seatbelt_blocks_unlinking_the_root_and_the_account_dir(tmp_path, monkeypatch):
    _fake_darwin(tmp_path, monkeypatch)
    root, secrets, linked, pins = _nested_secrets_tree(tmp_path)

    profile = opencode_sandbox_prefix(
        roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )[2]

    unlink = [line for line in profile.splitlines() if line.startswith("(deny file-write-unlink")]
    assert len(unlink) == 1
    for path in (root, root / "ws", secrets, linked, pins[0]):
        assert f'(literal "{_quote_seatbelt_subpath(str(path))}")' in unlink[0]
    assert f'(literal "{_quote_seatbelt_subpath(str(root.parent))}")' not in unlink[0]
    # Last match wins, so it follows every allow for the same paths.
    assert profile.index(unlink[0]) > profile.rindex(
        f'(allow file-read* file-write* (subpath "{_quote_seatbelt_subpath(str(linked))}"))'
    )


def test_seatbelt_has_no_unlink_rule_without_denied_paths(tmp_path):
    root, _secrets, _linked, pins = _nested_secrets_tree(tmp_path)

    assert "file-write-unlink" not in _seatbelt_profile([root], [], pins)


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path(SANDBOX_EXEC_PATH).is_file(),
    reason="needs macOS sandbox-exec",
)
def test_darwin_seatbelt_blocks_moving_secrets_ancestors_and_the_account_dir(tmp_path):
    tmp_path = tmp_path.resolve()
    root, secrets, linked, pins = _nested_secrets_tree(tmp_path)
    prefix = opencode_sandbox_prefix(
        roots=[root], read_only=pins, denied=[secrets], regrant=[linked]
    )

    def run(command: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([*prefix, "/bin/sh", "-c", command], capture_output=True, timeout=30)

    assert run(f"mv '{root}/ws' '{root}/ws2'").returncode != 0
    assert run(f"mv '{linked}' '{root}/o'").returncode != 0
    assert run(f"mv '{secrets}' '{root}/s2'").returncode != 0
    assert secrets.is_dir() and linked.is_dir()
    assert run(f"echo x > '{linked}/t.tmp' && mv '{linked}/t.tmp' '{linked}/token.json'").returncode == 0
    assert (linked / "token.json").read_text(encoding="utf-8").strip() == "x"


@pytest.mark.parametrize(
    "name",
    [
        # `*` and `?` are not legal Windows filename characters, so the folder
        # the refusal is about cannot exist there; `[`/`]` do exist and still
        # exercise the glob guard.
        pytest.param(
            "se*crets",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="* is not a legal Windows filename character"
            ),
        ),
        pytest.param(
            "se?crets",
            marks=pytest.mark.skipif(
                sys.platform == "win32", reason="? is not a legal Windows filename character"
            ),
        ),
        "se[crets",
        "se]crets",
    ],
)
def test_claude_macos_settings_refuse_glob_characters_in_the_denied_folder(
    tmp_path, monkeypatch, name
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    secrets = root / name
    secrets.mkdir(parents=True)

    with pytest.raises(FsSandboxUnavailable, match="glob"):
        claude_sandbox_settings(
            scope="workspace", mode="auto", roots=[root], denied=[secrets], regrant=[]
        )


def test_claude_macos_settings_refuse_glob_characters_in_a_child_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    secrets = root / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "a[1].json").write_text("{}", encoding="utf-8")

    with pytest.raises(FsSandboxUnavailable, match="glob"):
        claude_sandbox_settings(
            scope="workspace", mode="auto", roots=[root], denied=[secrets], regrant=[]
        )


def test_claude_macos_settings_fail_closed_when_the_denied_folder_cannot_be_listed(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("ciao.fs_sandbox.engine_read_roots", lambda: [])
    root = tmp_path / "root"
    secrets = root / "secrets"
    secrets.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def broken(self):
        if self == secrets:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", broken)

    with pytest.raises(FsSandboxUnavailable, match="secrets"):
        claude_sandbox_settings(
            scope="workspace", mode="auto", roots=[root], denied=[secrets], regrant=[]
        )
