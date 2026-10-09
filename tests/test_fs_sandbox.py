from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import asyncio

import pytest

from ciao.fs_sandbox import (
    SANDBOX_EXEC_PATH,
    FsSandboxUnavailable,
    _quote_seatbelt_subpath,
    _seatbelt_profile,
    claude_file_tool_guard,
    claude_sandbox_settings,
    engine_read_roots,
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


def _guard_decision(guard, tool_name: str, tool_input: dict) -> str:
    result = asyncio.run(guard({"tool_name": tool_name, "tool_input": tool_input}, None, None))
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
