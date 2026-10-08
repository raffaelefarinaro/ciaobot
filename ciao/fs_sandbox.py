"""Workspace filesystem sandbox profiles (issue #1148).

Builds the sandbox shapes the providers enforce. This module never spawns
a process: it only returns settings dicts / argv prefixes. Provider children
call these functions; this child does not wire them into either provider.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

AGENT_FS_SCOPES = ("workspace", "machine")

SANDBOX_EXEC_PATH = "/usr/bin/sandbox-exec"

_FS_SANDBOX_UNAVAILABLE = (
    "workspace filesystem sandbox is not available on this machine; "
    "set the workspace to whole machine"
)

_SYSTEM_READ_SUBPATHS = (
    "/usr",
    "/bin",
    "/opt",
    "/System",
    "/Library",
    "/private",
    "/Applications",
)

_BWRAP_RO_DIRS = ("/usr", "/bin", "/lib", "/lib64", "/etc")


class FsSandboxUnavailable(RuntimeError):
    """Raised when scope is workspace but no sandbox tool exists here."""


def claude_sandbox_settings(
    *, scope: str, mode: str, roots: list[Path]
) -> dict[str, Any] | None:
    """Sandbox settings dict for the Claude provider, or None for machine."""
    if scope == "machine":
        return None
    if scope != "workspace":
        raise ValueError("scope must be one of: workspace, machine")
    if not roots:
        raise ValueError("roots must not be empty")
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": mode == "auto",
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "filesystem": {
            "allowRead": [str(p) for p in roots],
            "allowWrite": [str(p) for p in roots],
        },
    }


def _quote_seatbelt_subpath(root: str) -> str:
    if '"' in root or ")" in root:
        raise ValueError(f"unsafe sandbox root: {root!r}")
    return root


def _seatbelt_profile(roots: list[Path]) -> str:
    # NOTE (issue #1148, live-tested on macOS): two details differ from the
    # first draft. `process-exec`/`process-fork` take no `*` suffix
    # (`process-fork*` is an "unbound variable" profile parse error, exit 65),
    # and `(deny default)` needs `(allow file-read* (literal "/"))` or every
    # child aborts (SIGABRT) resolving any path. The literal matches only `/`
    # itself, so traversal works and nothing extra becomes readable.
    lines = ["(version 1)", "(deny default)", '(allow file-read* (literal "/"))']
    for root in roots:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* file-write* (subpath "{quoted}"))')
    system = " ".join(f'(subpath "{p}")' for p in _SYSTEM_READ_SUBPATHS)
    lines.append(f"(allow file-read* {system})")
    lines.extend(
        [
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow signal (target same-sandbox))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow network-outbound)",
            "(allow network-inbound)",
        ]
    )
    return "\n".join(lines)


def opencode_sandbox_prefix(*, roots: list[Path]) -> list[str]:
    """Argv prefix confining the OpenCode server to the workspace roots."""
    if not roots:
        raise ValueError("roots must not be empty")
    platform = sys.platform
    if platform == "darwin":
        if Path(SANDBOX_EXEC_PATH).is_file():
            return [SANDBOX_EXEC_PATH, "-p", _seatbelt_profile(roots)]
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    if platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap:
            argv = [bwrap, "--die-with-parent"]
            for candidate in _BWRAP_RO_DIRS:
                if Path(candidate).exists():
                    argv.extend(["--ro-bind", candidate, candidate])
            for root in roots:
                text = str(root)
                argv.extend(["--bind", text, text])
            argv.extend(["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--"])
            return argv
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
