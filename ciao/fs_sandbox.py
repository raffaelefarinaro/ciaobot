"""Workspace filesystem sandbox profiles (issue #1148).

Builds the sandbox shapes the providers enforce. It returns settings dicts
and argv prefixes; the only process it runs is the one-time check that
``bwrap`` can create a sandbox on this host (``_bwrap_usable``).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
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

# /etc entries that commonly point outside /etc and that name resolution needs.
_BWRAP_ETC_LINKS = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")


_BWRAP_BLOCKED = (
    "bwrap is installed but cannot create a sandbox on this machine "
    "(Ubuntu 23.10 and later block it with AppArmor until bwrap has a profile; "
    "see docs/LINUX.md); set the workspace to whole machine"
)


def _bwrap_link_targets() -> list[str]:
    """Folders outside the bound system dirs that /etc symlinks point into.

    On systemd-resolved hosts (Ubuntu by default) ``/etc/resolv.conf`` links
    to ``/run/systemd/resolve/stub-resolv.conf``. ``/run`` is not bound, so
    without its target every DNS lookup inside the sandbox fails (#1186).
    """
    targets: list[str] = []
    for link in _BWRAP_ETC_LINKS:
        if not os.path.islink(link):
            continue
        parent = os.path.dirname(os.path.realpath(link))
        if any(parent == d or parent.startswith(d + "/") for d in _BWRAP_RO_DIRS):
            continue
        if os.path.isdir(parent) and parent not in targets:
            targets.append(parent)
    return targets


class FsSandboxUnavailable(RuntimeError):
    """Raised when scope is workspace but no sandbox tool exists here."""


@functools.cache
def _bwrap_usable(bwrap: str) -> bool:
    """Whether ``bwrap`` can set up a user namespace on this host.

    Ubuntu 23.10+ restricts unprivileged user namespaces through AppArmor, so
    an installed ``bwrap`` still fails with ``setting up uid map: Permission
    denied``. Checked once per process; installing the profile takes effect
    on the next engine start.
    """
    try:
        probe = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--", "true"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _symlink_hops(start: Path) -> list[Path]:
    """Every symlink met while resolving *start*, in order.

    Seatbelt checks each link it traverses, not only the final target, so a
    grant on the resolved path alone still refuses the exec.
    """
    hops: list[Path] = []
    pending = [start.absolute()]
    while pending and len(hops) < 40:
        path = pending.pop()
        current = Path(path.anchor)
        for index, part in enumerate(path.parts[1:], start=1):
            current = current / part
            if current.is_symlink():
                hops.append(current)
                target = Path(os.readlink(current))
                if not target.is_absolute():
                    target = current.parent / target
                rest = path.parts[index + 1:]
                pending.append(target.joinpath(*rest) if rest else target)
                break
    return hops


def engine_read_roots() -> list[Path]:
    """The running engine's install, readable (never writable) in workspace scope.

    The agent PATH starts with the engine's bin dir (``prepend_engine_path``),
    and the installer puts that environment under the home folder (a uv tool
    venv in ``~/.local/share/uv/tools``, its interpreter in
    ``~/.local/share/uv/python``). Without these a workspace-scoped turn cannot
    run its own ``ciao`` CLI (#1185). ``sys.prefix`` is the venv and
    ``sys.base_prefix`` the interpreter it links to. uv links a venv's
    ``python`` through a version alias (``cpython-3.13-…`` → ``cpython-3.13.13-…``),
    so every link on the way to the interpreter is granted as well, and so is
    the ``ciao`` package folder, which an editable install keeps outside the venv.
    """
    seen: list[Path] = []
    # The ``ciao`` package itself: inside the venv for a wheel install, but in
    # the source checkout for an editable one (docs/LINUX.md installs ``-e``).
    package = Path(__file__).resolve().parent
    candidates = [Path(sys.prefix), Path(sys.base_prefix).resolve(), package]
    candidates += _symlink_hops(Path(sys.executable))
    for path in candidates:
        if path not in seen:
            seen.append(path)
    return seen


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
            "allowRead": [str(p) for p in [*roots, *engine_read_roots()]],
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
    # Tools resolve every directory above where they run (a realpath walk, a
    # search for project files), so each ancestor of a granted path is
    # readable as an entry: its name list, never the files inside it.
    ancestors = sorted({str(p) for g in (*roots, *read_only) for p in Path(g).parents} - {"/"})
    if ancestors:
        listed = " ".join(f'(literal "{_quote_seatbelt_subpath(a)}")' for a in ancestors)
        lines.append(f"(allow file-read* {listed})")
    for root in roots:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* file-write* (subpath "{quoted}"))')
    for root in read_only:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* (subpath "{quoted}"))')
    system = " ".join(f'(subpath "{p}")' for p in _SYSTEM_READ_SUBPATHS)
    lines.append(f"(allow file-read* {system})")
    # The devices every shell touches: redirects to /dev/null, the terminal a
    # tool's process runs on, and the random sources. Without /dev/null a
    # tool's `< /dev/null` fails and its spawn never returns (#1174).
    lines.extend(
        [
            "(allow file-read* file-write* file-ioctl"
            ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty")'
            ' (literal "/dev/ptmx") (regex #"^/dev/ttys[0-9]+$") (subpath "/dev/fd"))',
            '(allow file-read* (literal "/dev/random") (literal "/dev/urandom"))',
            "(allow pseudo-tty)",
        ]
    )
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
    loads as code). The engine's own install is always added read-only, so the
    agent can run ``ciao`` (#1185).
    """
    if not roots:
        raise ValueError("roots must not be empty")
    read_only = [*read_only, *engine_read_roots()]
    platform = sys.platform
    if platform == "darwin":
        if Path(SANDBOX_EXEC_PATH).is_file():
            return [SANDBOX_EXEC_PATH, "-p", _seatbelt_profile(roots, read_only)]
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    if platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap:
            if not _bwrap_usable(bwrap):
                raise FsSandboxUnavailable(_BWRAP_BLOCKED)
            # The fresh /tmp goes first: mounted after the binds it would hide
            # any root (or OpenCode's temp folder) that lives under /tmp.
            argv = [bwrap, "--die-with-parent", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
            for candidate in _BWRAP_RO_DIRS:
                if Path(candidate).exists():
                    argv.extend(["--ro-bind", candidate, candidate])
            for candidate in _bwrap_link_targets():
                argv.extend(["--ro-bind", candidate, candidate])
            bound: list[str] = list(_BWRAP_RO_DIRS)
            for root in read_only:
                text = str(root)
                # bwrap cannot bind onto a symlink, and needs no grant for one:
                # a link inside a bound folder resolves on its own. Seatbelt
                # does need the hops, which is why they are in ``read_only``.
                if Path(text).is_symlink() or any(
                    text == d or text.startswith(d.rstrip("/") + "/") for d in bound
                ):
                    continue
                bound.append(text)
                argv.extend(["--ro-bind", text, text])
            for root in roots:
                text = str(root)
                argv.extend(["--bind", text, text])
            argv.append("--")
            return argv
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
