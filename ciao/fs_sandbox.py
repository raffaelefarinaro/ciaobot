"""Workspace filesystem sandbox profiles (issue #1148).

Builds the sandbox shapes the providers enforce. It returns settings dicts
and argv prefixes; the only process it runs is the one-time check that
``bwrap`` can create a sandbox on this host (``_bwrap_usable``).
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import site
import stat
import subprocess
import sys
import sysconfig
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ciao.os_support.tool_path import engine_bin_dir

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

# Config names OpenCode resolves in every ancestor of its cwd (#1197). A folder
# is a literal with read (it opens and lists, nothing inside it is read). A
# file that OpenCode loads as config or instructions needs its contents, so it
# is a literal with read too. CLAUDE.md is only realpathed here, so it gets
# metadata alone (#1208).
_ANCESTOR_CONFIG_FOLDERS = (".claude", ".agents", ".opencode")
_ANCESTOR_CONFIG_READ = ("AGENTS.md", "opencode.json", "opencode.jsonc")
_ANCESTOR_CONFIG_METADATA = ("CLAUDE.md",)

# /etc entries that commonly point outside /etc and that name resolution needs.
_BWRAP_ETC_LINKS = ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")


_BWRAP_BLOCKED = (
    "bwrap is installed but cannot create a sandbox on this machine "
    "(Ubuntu 23.10 and later block it with AppArmor until bwrap has a profile; "
    "see docs/LINUX.md); set the workspace to whole machine"
)


def _under(text: str, folders: Sequence[str]) -> bool:
    """Whether *text* is one of *folders* or lies beneath one of them."""
    return any(text == d or text.startswith(d.rstrip("/") + "/") for d in folders)


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
        # A relative readlink target joins onto its link's folder, so it can
        # carry ``..``; seatbelt never matches such a path, so normalize first.
        path = Path(os.path.normpath(pending.pop()))
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


def _user_scheme_install_paths() -> list[Path]:
    """The per-user install's packages and scripts, for a ``pip install --user`` engine.

    The default sysconfig scheme is the prefix's, so a user install's packages and
    console scripts sit outside it. They are added only when the running ``ciao``
    package lives in the user site, which is the case this covers.
    """
    if not site.ENABLE_USER_SITE:
        return []
    package = Path(__file__).resolve().parent
    if not package.is_relative_to(Path(site.getusersitepackages()).resolve()):
        return []
    scheme = sysconfig.get_preferred_scheme("user")
    return [
        Path(sysconfig.get_path("purelib", scheme=scheme)),
        Path(sysconfig.get_path("scripts", scheme=scheme)),
    ]


def node_tool_read_roots(path: str) -> list[Path]:
    """Read-only Node and npm code reached by the provider's actual PATH.

    nvm and similar installers put npm outside the workspace. Grant the
    executable, its symlink hops, and npm's own package, never the user's
    home or global npm cache. Sandboxed npx gets a separate writable cache.
    """
    roots: list[Path] = []
    for tool in ("node", "npm", "npx"):
        candidates = _tool_read_roots(tool, path)
        if candidates and tool in {"npm", "npx"}:
            # npm's own entry points are <npm>/bin/{npm,npx}-cli.js.
            resolved = candidates[0].resolve()
            package = resolved.parent.parent
            if resolved.parent.name == "bin" and (package / "package.json").is_file():
                candidates.append(package)
        for candidate in candidates:
            if candidate not in roots:
                roots.append(candidate)
    return roots


def _tool_read_roots(tool: str, path: str) -> list[Path]:
    """The executable ``tool`` resolves to on ``path``, its resolved target and links."""
    executable = shutil.which(tool, path=path)
    if executable is None:
        return []
    original = Path(executable).absolute()
    resolved = original.resolve()
    roots: list[Path] = []
    for candidate in (original, resolved, *_symlink_hops(original)):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def gws_tool_read_roots(path: str) -> list[Path]:
    """Read-only code the ``gws`` CLI runs, for a workspace-scoped chat (#1959).

    Grants the executable, its symlink hops, its npm package folder and node
    (the package's ``run.js`` starts with ``#!/usr/bin/env node``), never the
    home folder, ``~/.local/bin`` as a whole or an npm cache.
    """
    roots = _tool_read_roots("gws", path)
    if not roots:
        return []
    package = roots[0].resolve().parent
    if (package / "package.json").is_file() and "node_modules" in package.parts:
        roots.append(package)
        roots.extend(_tool_read_roots("node", path))
    return list(dict.fromkeys(roots))


def gh_config_read_root(env: Mapping[str, str]) -> Path | None:
    """The ``gh`` CLI's config directory, read-only for a workspace-scoped chat (#1961).

    Resolved the way gh does: ``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``,
    else ``~/.config/gh``. ``hosts.yml`` can hold an OAuth token; the agent can
    read it, as it can with whole-machine access.
    """
    if env.get("GH_CONFIG_DIR"):
        path = Path(env["GH_CONFIG_DIR"])
    elif env.get("XDG_CONFIG_HOME"):
        path = Path(env["XDG_CONFIG_HOME"]) / "gh"
    else:
        path = Path.home() / ".config" / "gh"
    return path.resolve() if path.is_dir() else None


_GH_TOKEN_TTL = 60.0
_gh_token_cache: dict[tuple[str, str | None, str | None, str | None], tuple[str, float]] = {}


def clear_gh_token_cache() -> None:
    """Forget every cached gh token (tests)."""
    _gh_token_cache.clear()


def gh_token_for_sandbox(
    gh: str | None, env: Mapping[str, str], *, cached_only: bool = False
) -> str | None:
    """The ``gh`` login token, to pass into a workspace-scoped chat (#1961).

    The macOS keychain is outside a workspace sandbox, so the engine reads gh's
    token unsandboxed and passes it in the chat's environment. The agent can
    read it, as it could read ``hosts.yml``. None when gh is missing, the
    environment already carries ``GH_TOKEN`` / ``GITHUB_TOKEN`` (the chat
    inherits it), or gh has no token. The token is never logged.

    A fetch is reused for ``_GH_TOKEN_TTL`` seconds; failures are never cached.
    ``cached_only`` returns the last good token whatever its age and never
    starts a process, so a caller on the event loop cannot block, and a retry or
    steer after a long turn keeps the token the turn started with.
    """
    if gh is None or env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return None
    key = (gh, env.get("GH_CONFIG_DIR"), env.get("XDG_CONFIG_HOME"), env.get("HOME"))
    hit = _gh_token_cache.get(key)
    if cached_only:
        return hit[0] if hit is not None else None
    if hit is not None and time.monotonic() - hit[1] < _GH_TOKEN_TTL:
        return hit[0]
    try:
        result = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
            env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        # gh answered that it has no token (a logout): stop handing out the old one.
        _gh_token_cache.pop(key, None)
        return None
    _gh_token_cache[key] = (token, time.monotonic())
    return token


def _inside_roots(roots: Sequence[Path], paths: Sequence[Path]) -> list[Path]:
    """The paths inside a writable root: readable there, but never writable.

    Engine folders and the caller's ``read_only`` paths both stay write-denied
    by a rule that follows the root's grant.
    """
    return [path for path in paths if any(path.is_relative_to(root) for root in roots)]


def _denied_inside_roots(
    roots: Sequence[Path], denied: Sequence[Path], regrant: Sequence[Path]
) -> tuple[list[Path], list[Path]]:
    """The hidden folders and re-allowed folders that apply to these roots (#1968).

    A denied folder counts only when it is a real directory (never a symlink)
    strictly inside a root; anything else is dropped, so nothing is masked or
    created for a missing or redirected path. A regrant counts only when it is
    a directory inside a kept denied folder.
    """
    kept = [
        d
        for d in denied
        if d.is_dir() and not d.is_symlink() and any(d != root and d.is_relative_to(root) for root in roots)
    ]
    again = [r for r in regrant if r.is_dir() and any(r.is_relative_to(d) for d in kept)]
    return kept, again


@functools.cache
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
    A ``pip install --user`` keeps its scripts and packages in the user site,
    outside ``sys.prefix``, so the sysconfig scheme paths are granted too.

    Cached for the process: every input is fixed once the engine starts. The
    list is shared, so callers copy it rather than change it.
    """
    seen: list[Path] = []
    # The ``ciao`` package itself: inside the venv for a wheel install, but in
    # the source checkout for an editable one (docs/LINUX.md installs ``-e``).
    package = Path(__file__).resolve().parent
    candidates = [
        Path(sys.prefix),
        Path(sys.base_prefix).resolve(),
        package,
        Path(engine_bin_dir()),
        Path(sysconfig.get_path("purelib")),
        Path(sysconfig.get_path("platlib")),
    ]
    candidates += _user_scheme_install_paths()
    candidates += _symlink_hops(Path(sys.executable))
    for path in candidates:
        # A folder already granted with its contents covers anything inside it
        # (a venv's site-packages sits under the venv), so it is not listed twice.
        if not any(path.is_relative_to(granted) for granted in seen):
            seen.append(path)
    return seen


# Claude treats these as pattern characters in a sandbox path, so a name holding
# one would deny a different set of paths than it names.
_DENY_GLOB_CHARS = frozenset("*?[]")


def claude_sandbox_settings(
    *,
    scope: str,
    mode: str,
    roots: list[Path],
    read_only: Sequence[Path] = (),
    denied: Sequence[Path] = (),
    regrant: Sequence[Path] = (),
) -> dict[str, Any] | None:
    """Sandbox settings dict for the Claude provider, or None for machine.

    ``read_only`` files are readable but never added to ``allowWrite`` (#1945).
    A ``read_only`` path inside a root stays write-denied by name (``denyWrite``),
    so a root that grants a folder can still pin single files in it (#1959).
    ``denied`` folders inside a root are hidden, except the ``regrant`` folders
    inside them, which stay read-write (#1968).
    """
    if scope == "machine":
        return None
    if scope != "workspace":
        raise ValueError("scope must be one of: workspace, machine")
    if not roots:
        raise ValueError("roots must not be empty")
    engine = engine_read_roots()
    if sys.platform != "darwin":
        # Claude's Linux sandbox is bubblewrap, which cannot bind a symlink, so
        # only the real folders are granted; the links resolve to them inside.
        # Known gap (#1208): unlike OpenCode's bwrap path, no ``--symlink`` is
        # recreated for a link outside the bound folders, so a uv alias under
        # the denied home may not resolve in Claude's shell. Unverified: it needs
        # a signed-in Linux Claude, so the behaviour is left as is.
        # The filter applies to read-only grants too: a symlink such as
        # ``~/.local/bin/gws`` is dropped there, and its resolved target stays granted.
        engine = [path for path in engine if not path.is_symlink()]
        read_only = [path for path in read_only if not path.is_symlink()]
    hidden, regranted = _denied_inside_roots(roots, denied, regrant)
    deny_read = [str(Path.home())]
    deny_write = [str(p) for p in _inside_roots(roots, [*engine, *read_only])]
    # Read from the bundled Claude CLI 2.1.306 (minified), #1968.
    for folder in hidden:
        if sys.platform == "darwin":
            # macOS: a glob denyRead gets a carve-out for nested allows, a plain
            # entry would hide the regranted folder too. denyWrite has no
            # carve-out, so every child except the regranted ones is listed.
            # Verified live with Claude Code 2.1.309 on 2026-10-23: a nested read
            # in another account is refused, the regrant can be read and written,
            # the pins are write-denied, and moving secrets or the account dir is
            # refused. Limits: denyWrite lists the children present at spawn, so a
            # child created later stays read-denied but is writable until the
            # client restarts, and `ls secrets` still lists names.
            if _DENY_GLOB_CHARS & set(str(folder)):
                raise FsSandboxUnavailable(
                    f"cannot hide {folder}: its path contains a glob character"
                )
            try:
                children = sorted(folder.iterdir())
            except OSError as exc:
                raise FsSandboxUnavailable(f"cannot hide {folder}: it cannot be listed ({exc})") from exc
            for child in children:
                if _DENY_GLOB_CHARS & set(child.name):
                    raise FsSandboxUnavailable(
                        f"cannot hide {folder}: {child.name!r} contains a glob character"
                    )
            deny_read.append(f"{folder}/*")
            deny_write.extend(str(child) for child in children if child not in regranted)
        else:
            # Linux: denyRead is a tmpfs, then the allow re-binds the regrant.
            # Not in denyWrite: its ro-bind is emitted last and would re-expose
            # the real directory.
            deny_read.append(str(folder))
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": mode == "auto",
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        # Only the filesystem is confined (parity with OpenCode's profile, which
        # allows network both ways). Without this section the SDK sandbox
        # blocks the engine's loopback port and every outbound host.
        "network": {
            "allowedDomains": ["*"],
            "allowLocalBinding": True,
        },
        "filesystem": {
            # Claude Code reads everywhere unless a path is denied, so
            # ``allowRead`` alone confines nothing: deny the home directory
            # and re-allow the roots inside it (#1174).
            "denyRead": deny_read,
            "allowRead": [str(p) for p in [*roots, *read_only, *engine, *regranted]],
            "allowWrite": [str(p) for p in [*roots, *regranted]],
            # The roots grant write access, so an engine folder or a read-only path
            # inside one is denied it by name, as OpenCode's profile shadows it (#1208).
            "denyWrite": deny_write,
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
    characters cut the path at the last literal directory. A path that cannot
    be resolved (a NUL byte, an unknown ``~user``) counts as outside, so the guard
    denies it rather than raising: a hook that raises is not a deny.
    """
    text = _static_prefix(value.strip())
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        resolved = candidate.resolve()
        resolved_roots = [root.resolve() for root in roots]
    except (ValueError, OSError, RuntimeError):
        return True
    return not any(resolved.is_relative_to(root) for root in resolved_roots)


_TASK_SESSION_ID = re.compile(r"[0-9A-Za-z-]{8,64}")


def claude_task_output_path_allowed(
    value: str, *, session_id: str, cwd: Path, tmp_base: str | None, uid: int
) -> bool:
    """Whether *value* is this session's own native background-task output file.

    Exactly ``<tmp base>/claude-<uid>/<one segment>/<session_id>/tasks/<name>.output``
    after resolving symlinks and ``..``; anything else, or a missing or
    malformed *session_id*, is refused. The target must be a regular file with
    one link, reached without a final symlink, and *value* is not trimmed.
    """
    if not _TASK_SESSION_ID.fullmatch(session_id):
        return False
    if value != value.strip() or value.endswith("/"):
        return False
    if any(char in value for char in "*?[{\x00\n\r"):
        return False
    base = Path(tmp_base or "/tmp").expanduser().resolve() / f"claude-{uid}"
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if os.path.islink(candidate):
            return False
        resolved = candidate.resolve()
    except (ValueError, OSError, RuntimeError):
        return False
    depth = len(base.parts)
    if not (
        resolved.is_relative_to(base)
        and len(resolved.parts) == depth + 4
        and resolved.parts[depth + 1] == session_id
        and resolved.parts[depth + 2] == "tasks"
        and resolved.name.endswith(".output")
    ):
        return False
    try:
        info = resolved.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _resolved_tool_path(value: str, *, cwd: Path) -> Path | None:
    """The path a tool argument names, resolved as ``path_outside_roots`` resolves it.

    None when it cannot be resolved (a NUL byte, an unknown ``~user``).
    """
    text = _static_prefix(value.strip())
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return candidate.resolve()
    except (ValueError, OSError, RuntimeError):
        return None


def _case_key(path: Path) -> tuple[str, ...]:
    """The path's components, case-folded where the filesystem ignores case."""
    if sys.platform in ("darwin", "win32"):
        return tuple(part.casefold() for part in path.parts)
    return path.parts


def _same_file(a: Path, b: Path) -> bool:
    """Inode identity: a case variant or a hardlink of the same file matches."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _is_read_only_target(target: Path, entries: Sequence[Path]) -> bool:
    """Whether *target* is, or sits inside, one of the read-only *entries* (#2459).

    An existing target is compared by identity, with each of its ancestors, so a
    case variant of a pinned file or folder, or a hardlink to one, still matches.
    A target that does not exist has no identity yet, so it is compared by its
    components, case-folded on macOS and Windows.
    """
    if target.exists():
        chain = [target, *target.parents]
        return any(_same_file(node, entry) for entry in entries for node in chain)
    key = _case_key(target)
    return any(key[: len(entry_key)] == entry_key for entry_key in (_case_key(e) for e in entries))


def _is_read_only_file(target: Path, entries: Sequence[Path]) -> bool:
    """Whether *target* is exactly one of the read-only *entries* that is a file."""
    return any(entry.is_file() and _same_file(target, entry) for entry in entries)


def _resolved_read_only(paths: Sequence[Path]) -> list[Path]:
    """The read-only entries, resolved. An entry that cannot resolve grants nothing."""
    resolved: list[Path] = []
    for path in paths:
        try:
            resolved.append(path.resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    return resolved


# Claude's file tools, split by what they do to the path. A read-only path is
# readable by the read tools and writable by none of them.
_CLAUDE_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
_CLAUDE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def claude_file_tool_guard(
    *,
    cwd: Path,
    roots: list[Path],
    cli_tmp_base: str | None = None,
    read_only: Sequence[Path] = (),
) -> Any:
    """PreToolUse hook denying Claude's file tools any path outside *roots*.

    Claude's plan directory is allowed as well: plan mode writes its plan file
    there, outside every workspace root. Read (only) may also open the output
    files of this chat's own native background tasks under the CLI temp
    directory *cli_tmp_base*, matched by the session id the CLI puts in the
    hook input; the CLI's tool result tells the model to Read them.

    *read_only* paths (#2459) are never writable, inside a root or not, by any
    write tool. Outside the roots, a read tool may open an exact *read-only file*
    (a folder grant does not extend to its files), which is what lets a memory
    pass read its source archive.
    """
    if not roots:
        raise ValueError("roots must not be empty")
    allowed = ", ".join(str(root) for root in roots)
    reachable = [*roots, Path.home() / ".claude" / "plans"]
    uid = getattr(os, "getuid", lambda: 0)()
    read_only_paths = _resolved_read_only(read_only)

    async def on_pre_tool_use(
        input_data: dict[str, Any], tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        del tool_use_id, context  # unused
        tool_name = str(input_data.get("tool_name") or "")
        session_id = str(input_data.get("session_id") or "")
        keys = CLAUDE_FILE_TOOL_PATH_KEYS.get(tool_name, ())
        tool_input = input_data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        for key in keys:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                resolved = _resolved_tool_path(value, cwd=cwd)
                if (
                    tool_name in _CLAUDE_WRITE_TOOLS
                    and resolved is not None
                    and _is_read_only_target(resolved, read_only_paths)
                ):
                    return _claude_guard_deny(f"{value} is read-only.")
                if path_outside_roots(value, cwd=cwd, roots=reachable):
                    if (
                        tool_name in _CLAUDE_READ_TOOLS
                        and resolved is not None
                        and _is_read_only_file(resolved, read_only_paths)
                    ):
                        continue
                    if (
                        tool_name == "Read"
                        and key == "file_path"
                        and claude_task_output_path_allowed(
                            value, session_id=session_id, cwd=cwd, tmp_base=cli_tmp_base, uid=uid
                        )
                    ):
                        continue
                    return _claude_guard_deny(
                        f"This workspace is confined to {allowed}; {value} is outside it."
                    )
        return {}

    return on_pre_tool_use


def _claude_guard_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _quote_seatbelt_subpath(root: str) -> str:
    if '"' in root or ")" in root:
        raise ValueError(f"unsafe sandbox root: {root!r}")
    # Seatbelt decodes backslash escapes inside quoted strings (e.g. `\n`
    # becomes a newline), so a literal backslash in a root would otherwise
    # redirect the grant to a different sibling path. Double every backslash
    # to keep the grant on the supplied root.
    return root.replace("\\", "\\\\")


def _seatbelt_profile(
    roots: list[Path],
    read_only: Sequence[Path] = (),
    shadowed: Sequence[Path] = (),
    denied: Sequence[Path] = (),
    regrant: Sequence[Path] = (),
) -> str:
    """The seatbelt profile; ``shadowed`` paths sit inside a root and stay read-only.

    ``denied`` folders inside a root are hidden, except the ``regrant`` folders
    in them (#1968).
    """
    # NOTE (issue #1148, live-tested on macOS): two details differ from the
    # first draft. `process-exec`/`process-fork` take no `*` suffix
    # (`process-fork*` is an "unbound variable" profile parse error, exit 65),
    # and `(deny default)` needs `(allow file-read* (literal "/"))` or every
    # child aborts (SIGABRT) resolving any path. The literal matches only `/`
    # itself, so traversal works and nothing extra becomes readable.
    # `/var`, `/tmp` and `/etc` are symlinks into `/private`. A path spelled
    # through one (`$TMPDIR` is `/var/folders/...`, and `ciao gws`'s PATH probe
    # reads `/etc/paths.d`, #2448) needs the link itself readable, or every
    # access under it is refused even where the target is allowed.
    lines = [
        "(version 1)",
        "(deny default)",
        '(allow file-read* (literal "/") (literal "/var") (literal "/tmp") (literal "/etc"))',
    ]
    # Tools resolve every directory above where they run (a realpath walk, a
    # search for project files), so each ancestor of a granted path is
    # readable as an entry: its name list, never the files inside it.
    ancestors = sorted({str(p) for g in (*roots, *read_only) for p in Path(g).parents} - {"/"})
    if ancestors:
        listed = " ".join(f'(literal "{_quote_seatbelt_subpath(a)}")' for a in ancestors)
        lines.append(f"(allow file-read* {listed})")
        # OpenCode realpaths config names in every ancestor of its cwd, and a
        # refused realpath fails the whole prompt with a 500 (#1197). Only the
        # names in ``_ANCESTOR_CONFIG_METADATA`` get metadata alone (#1208).
        folders = " ".join(
            f'(literal "{_quote_seatbelt_subpath(a + "/" + name)}")'
            for a in ancestors
            for name in _ANCESTOR_CONFIG_FOLDERS
        )
        read = " ".join(
            f'(literal "{_quote_seatbelt_subpath(a + "/" + name)}")'
            for a in ancestors
            for name in _ANCESTOR_CONFIG_READ
        )
        metadata = " ".join(
            f'(literal "{_quote_seatbelt_subpath(a + "/" + name)}")'
            for a in ancestors
            for name in _ANCESTOR_CONFIG_METADATA
        )
        lines.append(f"(allow file-read* {folders} {read})")
        lines.append(f"(allow file-read-metadata {metadata})")
    for root in roots:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* file-write* (subpath "{quoted}"))')
    # Seatbelt applies the last matching rule, so these denies follow every root
    # allow and win over it for the folder they name.
    for path in shadowed:
        quoted = _quote_seatbelt_subpath(str(path))
        lines.append(f'(deny file-write* (subpath "{quoted}"))')
    for root in read_only:
        quoted = _quote_seatbelt_subpath(str(root))
        lines.append(f'(allow file-read* (subpath "{quoted}"))')
    system = " ".join(f'(subpath "{p}")' for p in _SYSTEM_READ_SUBPATHS)
    lines.append(f"(allow file-read* {system})")
    # The hidden folders follow the system read allow, which covers /private,
    # so a workspace under it is hidden too. The regrant follows its deny, or
    # the deny would hide it as well, and the write pins repeat after the
    # regrant so they stay write-denied inside it (#1968).
    if denied:
        for path in denied:
            quoted = _quote_seatbelt_subpath(str(path))
            lines.append(f'(deny file-read* file-write* (subpath "{quoted}"))')
        for path in regrant:
            quoted = _quote_seatbelt_subpath(str(path))
            lines.append(f'(allow file-read* file-write* (subpath "{quoted}"))')
        for path in shadowed:
            quoted = _quote_seatbelt_subpath(str(path))
            lines.append(f'(deny file-write* (subpath "{quoted}"))')
        # A rename needs unlink on its source, and the subpath rules follow the
        # path, not the inode: moving the secrets folder, the account folder or
        # any folder above them inside a root would carry the data out from under
        # the rules. Last match wins, so this follows every allow (#1968).
        pinned = {
            str(q)
            for p in (*denied, *regrant, *shadowed)
            for q in (p, *p.parents)
            if q == p or any(q == r or q.is_relative_to(r) for r in roots)
        }
        listed = " ".join(f'(literal "{_quote_seatbelt_subpath(q)}")' for q in sorted(pinned))
        lines.append(f"(deny file-write-unlink {listed})")
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
    *,
    roots: list[Path],
    read_only: Sequence[Path] = (),
    denied: Sequence[Path] = (),
    regrant: Sequence[Path] = (),
) -> list[str]:
    """Argv prefix confining the OpenCode server to the workspace roots.

    ``roots`` are read-write; ``read_only`` are readable and never writable
    (the server's own install and config, which an unconfined server later
    loads as code). The engine's own install is always added read-only, so the
    agent can run ``ciao`` (#1185). A ``read_only`` path inside a root stays
    readable but write-denied (#1959). A ``denied`` folder strictly inside a root
    is hidden (seatbelt deny, bwrap tmpfs) except the ``regrant`` folders in it,
    which stay read-write; a missing or symlinked one is ignored (#1968).
    """
    if not roots:
        raise ValueError("roots must not be empty")
    # An engine folder inside a writable root is already readable there; it
    # stays read-only by a rule that follows the root's grant, not by a grant.
    engine = engine_read_roots()
    shadowed = _inside_roots(roots, [*engine, *read_only])
    hidden, regranted = _denied_inside_roots(roots, denied, regrant)
    read_only = [
        *(path for path in read_only if path not in shadowed),
        *(path for path in engine if path not in shadowed),
    ]
    platform = sys.platform
    if platform == "darwin":
        if Path(SANDBOX_EXEC_PATH).is_file():
            return [SANDBOX_EXEC_PATH, "-p", _seatbelt_profile(roots, read_only, shadowed, hidden, regranted)]
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
            # A symlink is never bound (bwrap cannot bind onto one), and a
            # folder under an earlier bind is already there. Shadowed folders
            # are bound after the roots, so they are left out of this pass.
            # A missing path would abort bwrap (a pre-re-root install has no
            # receipt file). A bound file keeps its inode, so a registry
            # rewritten by rename, or a receipt created later, needs a new
            # server: the OpenCode provider restarts it when either changes
            # (`_read_only_snapshot`, #1960).
            directories = [
                str(path)
                for path in read_only
                if not path.is_symlink() and path not in shadowed and path.exists()
            ]
            bound: list[str] = list(_BWRAP_RO_DIRS)
            for text in directories:
                if not _under(text, bound):
                    bound.append(text)
                    argv.extend(["--ro-bind", text, text])
            for root in roots:
                text = str(root)
                argv.extend(["--bind", text, text])
            # The secrets mask follows the root binds, then the linked account is
            # bound back over it (#1968). Both folders were checked to exist.
            for folder in hidden:
                argv.extend(["--tmpfs", str(folder)])
            for folder in regranted:
                argv.extend(["--bind", str(folder), str(folder)])
            # Mounted after the writable binds so the read-only mount sits on top.
            # A path inside a masked folder but outside its regrant would put
            # host content back over the tmpfs, so it is skipped.
            for path in shadowed:
                if any(path.is_relative_to(d) for d in hidden) and not any(
                    path.is_relative_to(r) for r in regranted
                ):
                    continue
                if not path.is_symlink() and path.exists():
                    text = str(path)
                    argv.extend(["--ro-bind", text, text])
            # A link outside every bind gets recreated inside the sandbox, so it
            # resolves to its target. A link under a bind resolves on its own.
            # No bind contains a link made here, so the order does not matter.
            covered = [*bound, *(str(root) for root in roots), *(str(p) for p in shadowed)]
            for path in read_only:
                text = str(path)
                if path.is_symlink() and not _under(text, covered):
                    argv.extend(["--symlink", os.readlink(text), text])
            argv.append("--")
            return argv
        raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
    raise FsSandboxUnavailable(_FS_SANDBOX_UNAVAILABLE)
