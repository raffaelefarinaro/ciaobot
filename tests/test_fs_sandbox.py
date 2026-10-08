from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ciao.fs_sandbox import (
    SANDBOX_EXEC_PATH,
    FsSandboxUnavailable,
    claude_sandbox_settings,
    opencode_sandbox_prefix,
)


def test_claude_workspace_settings_allow_only_the_roots(tmp_path):
    roots = [tmp_path / "root-a", tmp_path / "root-b"]

    auto = claude_sandbox_settings(scope="workspace", mode="auto", roots=roots)
    assert auto == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "filesystem": {
            "allowRead": [str(roots[0]), str(roots[1])],
            "allowWrite": [str(roots[0]), str(roots[1])],
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
    assert f'(subpath "{root}")' in profile
    # Write access exists only on the given root: the system paths are
    # read-only and there is no global file-write allow.
    assert profile.count("file-write*") == 1
    # Traversal of `/` itself is allowed (without it every child aborts under
    # `deny default`), and process rules carry no `*` suffix (a parse error).
    assert '(allow file-read* (literal "/"))' in profile
    assert "(allow process-exec)" in profile
    assert "(allow process-fork)" in profile


def test_opencode_prefix_is_bwrap_on_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name, *args, **kwargs: "/usr/bin/bwrap")

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
