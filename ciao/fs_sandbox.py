"""Workspace filesystem sandbox profiles (issue #1148).

Builds the sandbox shapes the providers enforce. This module never spawns
a process: it only returns settings dicts / argv prefixes. Provider children
call these functions; this child does not wire them into either provider.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Sequence
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
            # Claude Code reads everywhere unless a path is denied, so
            # ``allowRead`` alone confines nothing: deny the home directory
            # and re-allow the roots inside it (#1174).
            "denyRead": [str(Path.home())],
            "allowRead": [str(p) for p in roots],
            "allowWrite": [str(p) for p in roots],
        },
    }


# Claude's file tools run in the CLI process, not under the Bash sandbox, so
# workspace scope guards them with a PreToolUse hook instead. Each entry names
# the input keys that carry a path; a tool not listed here takes no path.
CLAUDE_FILE_TOOL_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path",),
    "LS": ("path",),
}

_GLOB_CHARS = frozenset("*?[{")


def _static_prefix(value: str) -> str:
    """The leading path components of *value* before any glob character."""
    parts: list[str] = []
    for part in value.split("/"):
        if _GLOB_CHARS & set(part):
            break
        parts.append(part)
    return "/".join(parts) or ("/" if value.startswith("/") else ".")


def path_outside_roots(value: str, *, cwd: Path, roots: list[Path]) -> bool:
    """Whether a tool path argument escapes every workspace root.

    Relative paths resolve against *cwd*, ``~`` expands, and symlinks resolve,
    so a link inside a root that points outside it counts as outside. Glob
    characters cut the path at the last literal directory.
    """
    text = _static_prefix(value.strip())
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    resolved = candidate.resolve()
    return not any(resolved.is_relative_to(root.resolve()) for root in roots)


def claude_file_tool_guard(*, cwd: Path, roots: list[Path]) -> Any:
    """PreToolUse hook denying Claude's file tools any path outside *roots*.

    Claude's plan directory is allowed as well: plan mode writes its plan file
    there, outside every workspace root.
    """
    if not roots:
        raise ValueError("roots must not be empty")
    allowed = ", ".join(str(root) for root in roots)
    reachable = [*roots, Path.home() / ".claude" / "plans"]

    async def on_pre_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        keys = CLAUDE_FILE_TOOL_PATH_KEYS.get(str(input_data.get("tool_name") or ""), ())
        tool_input = input_data.get("tool_input") or {}
        for key in keys:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                if path_outside_roots(value, cwd=cwd, roots=reachable):
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"This workspace is confined to {allowed}; "
                                f"{value} is outside it."
                            ),
                        }
                    }
        return {}

    return on_pre_tool_use


def _quote_seatbelt_subpath(root: str) -> str:
    if '"' in root or ")" in root:
        raise ValueError(f"unsafe sandbox root: {root!r}")
    # Seatbelt decodes backslash escapes inside quoted strings (e.g. `\n`
    # becomes a newline), so a literal backslash in a root would otherwise
    # redirect the grant to a different sibling path. Double every backslash
    # to keep the grant on the supplied root.
    return root.replace("\\", "\\\\")


def _seatbelt_profile(roots: list[Path], read_only: Sequence[Path] = ()) -> str:
    # NOTE (issue #1148, live-tested on macOS): two details differ from the
    # first draft. `process-exec`/`process-fork` take no `*` suffix
    # (`process-fork*` is an "unbound variable" profile parse error, exit 65),
    # and `(deny default)` needs `(allow file-read* (literal "/"))` or every
    # child aborts (SIGABRT) resolving any path. The literal matches only `/`
    # itself, so traversal works and nothing extra becomes readable.
    # `/var` and `/tmp` are symlinks into `/private`. A path spelled through
    # one (`$TMPDIR` is `/var/folders/...`) needs the link itself readable, or
    # every access under it is refused even where the target is allowed.
    lines = [
        "(version 1)",
        "(deny default)",
        '(allow file-read* (literal "/") (literal "/var") (literal "/tmp"))',
    ]
    for root in roots:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* file-write* (subpath "{quoted}"))')
    for root in read_only:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* (subpath "{quoted}"))')
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


def opencode_sandbox_prefix(
    *, roots: list[Path], read_only: Sequence[Path] = ()
) -> list[str]:
    """Argv prefix confining the OpenCode server to the workspace roots.

    ``roots`` are read-write; ``read_only`` are readable and never writable
    (the server's own install and config, which an unconfined server later
    loads as code).
    """
    if not roots:
        raise ValueError("roots must not be empty")
    platform = sys.platform
    if platform == "darwin":
        if Path(SANDBOX_EXEC_PATH).is_file():
            return [SANDBOX_EXEC_PATH, "-p", _seatbelt_profile(roots, read_only)]
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    if platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap:
            argv = [bwrap, "--die-with-parent"]
            for candidate in _BWRAP_RO_DIRS:
                if Path(candidate).exists():
                    argv.extend(["--ro-bind", candidate, candidate])
            for root in read_only:
                text = str(root)
                argv.extend(["--ro-bind", text, text])
            for root in roots:
                text = str(root)
                argv.extend(["--bind", text, text])
            argv.extend(["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--"])
            return argv
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
