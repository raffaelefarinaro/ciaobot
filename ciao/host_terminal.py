"""Open a native terminal window on the computer running the engine.

The caller names an *action id* or a chat/project; it never supplies a command
or a path. Every command string is a server-side constant or comes from the
functions that already produce the text Ciaobot shows (the ``*-update`` actions
take theirs from the packaged provider record and the resolved binary, see
``ciao.provider_versions``), and every folder is
derived from server state (`resolve_folder`). The routes in
``ciao/web/routes_host_terminal.py`` add the auth and host-browser checks.
"""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.tool_path import terminal_path

PLATFORM_MACOS, PLATFORM_WINDOWS, PLATFORM_LINUX, PLATFORM_OTHER = (
    "macos",
    "windows",
    "linux",
    "other",
)

GWS_INSTALL_COMMAND = "npm install -g @googleworkspace/cli"
SKILLS_UPDATE_COMMAND = "npx skills update -g -y"


class NothingToUpdate(Exception):
    """An ``*-update`` action was requested but the provider is not older than tested."""


ACTION_IDS: tuple[str, ...] = (
    "claude-install",
    "claude-login",
    "opencode-install",
    "opencode-login",
    "gws-install",
    "skills-update",
    "opencode-update",
    "claude-update",
)

REASON_MESSAGES = {
    "not_on_host": "Opening a terminal is only available in a browser on the computer running Ciaobot.",
    "unavailable_headless": "Ciaobot runs without a desktop session here, so it cannot open a terminal window.",
    "no_terminal_found": "No supported terminal app was found on the computer running Ciaobot.",
    "unsupported_platform": "Opening a terminal is not supported on this operating system.",
}

# Read through getattr so mypy accepts them on every platform.
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

_LINUX_TERMINALS = (
    "x-terminal-emulator",
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "kitty",
    "alacritty",
)
_STRIPPED_ENV_KEYS = frozenset({"PWA_AUTH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"})


class TerminalUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(REASON_MESSAGES[reason])


@dataclass(frozen=True)
class Capabilities:
    available: bool
    platform: str
    reason: str = ""
    terminal: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "platform": self.platform,
            "reason": self.reason,
            "terminal": self.terminal,
            "actions": list(ACTION_IDS) if self.available else [],
        }


def host_platform() -> str:
    """Which terminal backend this platform has. Read fresh on every call."""
    if sys.platform == "darwin":
        return PLATFORM_MACOS
    if sys.platform == "win32":
        return PLATFORM_WINDOWS
    if sys.platform.startswith("linux"):
        return PLATFORM_LINUX
    return PLATFORM_OTHER


def _which(name: str) -> str | None:
    return shutil.which(name, path=terminal_path() or None)


def _spawn(
    argv: list[str], *, cwd: Path, env: dict[str, str], creationflags: int = 0
) -> None:
    subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
        start_new_session=(creationflags == 0),
    )


# Do not let a spawned terminal inherit provider credentials, API keys, or
# tokens belonging to the engine. The suffix rules also cover user-configured
# MCP and service credentials whose names are not known to Ciaobot ahead of time.
_SENSITIVE_ENV_SUFFIXES = (
    "_API_KEY",
    "_API_TOKEN",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SECRET",
    "_SECRET_KEY",
    "_PASSWORD",
    "_TOKEN",
)


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return (
        upper in {"GH_TOKEN", "GITHUB_TOKEN", "PRIVATE_KEY"}
        or upper.endswith(_SENSITIVE_ENV_SUFFIXES)
        or "_PRIVATE_KEY" in upper
    )


def _terminal_env() -> dict[str, str]:
    """The engine's environment minus secrets."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STRIPPED_ENV_KEYS
        and not key.startswith("CIAO_")
        and not _is_sensitive_env_key(key)
    }
    path = terminal_path()
    if host_platform() != PLATFORM_MACOS and path:
        env["PATH"] = path
    return env


def _linux_terminal() -> tuple[str, str] | None:
    for name in _LINUX_TERMINALS:
        found = _which(name)
        if found:
            return name, found
    return None


def capabilities() -> Capabilities:
    platform = host_platform()
    if platform == PLATFORM_MACOS:
        return Capabilities(True, platform, terminal="Terminal")
    if platform == PLATFORM_WINDOWS:
        return Capabilities(
            True, platform, terminal="Windows Terminal" if _which("wt") else "PowerShell"
        )
    if platform == PLATFORM_LINUX:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return Capabilities(False, platform, reason="unavailable_headless")
        found = _linux_terminal()
        if found is None:
            return Capabilities(False, platform, reason="no_terminal_found")
        return Capabilities(True, platform, terminal=found[0])
    return Capabilities(False, platform, reason="unsupported_platform")


def _ps_quote(value: str) -> str:
    # PowerShell treats the typographic single quotes as quotes too.
    return "'" + re.sub("['\u2018\u2019\u201a\u201b]", lambda m: m.group(0) * 2, value) + "'"


def action_command(action: str) -> str:
    """The command line for an allowlisted action id; KeyError for any other.

    NothingToUpdate when an update action has nothing to update.
    """
    if action not in ACTION_IDS:
        raise KeyError(action)
    if action in ("opencode-update", "claude-update"):
        from ciao import provider_versions

        provider = action.removesuffix("-update")
        check = provider_versions.current_checks().get(provider, {})
        if check.get("state") != "below_tested" or not check.get("command"):
            raise NothingToUpdate(f"{action}: nothing to update")
        return check["command"]
    from ciao import setup_status
    from ciao.providers import opencode

    if action == "claude-install":
        return setup_status.claude_install_command()
    if action == "claude-login":
        cli = setup_status.claude_cli_path()
        command = setup_status.claude_auth_command(cli)
        if host_platform() == PLATFORM_WINDOWS and command != "claude auth login":
            # shlex.quote output is not valid PowerShell; use the call operator.
            return "& " + _ps_quote(cli) + " auth login"
        return command
    if action == "opencode-install":
        return opencode.opencode_install_command()
    if action == "opencode-login":
        return opencode.OPENCODE_LOGIN_COMMAND
    if action == "gws-install":
        return GWS_INSTALL_COMMAND
    return SKILLS_UPDATE_COMMAND


def _launch_macos(folder: Path, command: str | None, env: dict[str, str], runtime_dir: Path) -> str:
    if command is None:
        _spawn(["/usr/bin/open", "-a", "Terminal", str(folder)], cwd=folder, env=env)
        return "Terminal"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    script = runtime_dir / f"terminal-{secrets.token_hex(8)}.command"
    text = (
        "#!/bin/zsh\n"
        'rm -f -- "$0"\n'
        f"cd -- {shlex.quote(str(folder))} || exit 1\n"
        f"{command}\n"
        'exec "${SHELL:-/bin/zsh}" -l\n'
    )
    fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    try:
        _spawn(["/usr/bin/open", "-a", "Terminal", str(script)], cwd=folder, env=env)
    except OSError:
        script.unlink(missing_ok=True)
        raise
    return "Terminal"


def _launch_windows(folder: Path, command: str | None, env: dict[str, str]) -> str:
    ps = _which("powershell.exe") or "powershell.exe"
    wt = _which("wt")
    if wt:
        argv = [wt, "-d", str(folder).replace(";", "\\;")]
        if command is not None:
            argv += [ps, "-NoExit", "-ExecutionPolicy", "RemoteSigned", "-Command", command.replace(";", "\\;")]
        _spawn(argv, cwd=folder, env=env, creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP)
        return "Windows Terminal"
    argv = [ps, "-NoExit"]
    if command is not None:
        argv += ["-ExecutionPolicy", "RemoteSigned", "-Command", command]
    _spawn(argv, cwd=folder, env=env, creationflags=_CREATE_NEW_CONSOLE | _CREATE_NEW_PROCESS_GROUP)
    return "PowerShell"


def _launch_linux(folder: Path, command: str | None, env: dict[str, str]) -> str:
    found = _linux_terminal()
    if found is None:
        raise TerminalUnavailable("no_terminal_found")
    name, binary = found
    f = str(folder)
    q = shlex.quote(f)
    shell = '"${SHELL:-/bin/bash}" -l'
    script = f"cd -- {q} && {command}; exec {shell}" if command else f"cd -- {q} && exec {shell}"
    run = command is not None
    if name == "x-terminal-emulator":
        argv = [binary, "-e", "bash", "-lc", script]
    elif name == "gnome-terminal":
        argv = [binary, f"--working-directory={f}"] + (["--", "bash", "-lc", script] if run else [])
    elif name == "konsole":
        argv = [binary, "--workdir", f] + (["-e", "bash", "-lc", script] if run else [])
    elif name == "xfce4-terminal":
        argv = [binary, f"--working-directory={f}"] + (["-x", "bash", "-lc", script] if run else [])
    elif name == "kitty":
        argv = [binary, "--directory", f] + (["bash", "-lc", script] if run else [])
    else:
        argv = [binary, "--working-directory", f] + (["-e", "bash", "-lc", script] if run else [])
    _spawn(argv, cwd=folder, env=env)
    return name


def launch(folder: Path, *, action: str | None, runtime_dir: Path) -> str:
    """Open a terminal at ``folder``, optionally running an allowlisted action.

    Returns the terminal's display name.
    """
    caps = capabilities()
    if not caps.available:
        raise TerminalUnavailable(caps.reason)
    command = action_command(action) if action else None
    env = _terminal_env()
    if caps.platform == PLATFORM_MACOS:
        return _launch_macos(folder, command, env, runtime_dir)
    if caps.platform == PLATFORM_WINDOWS:
        return _launch_windows(folder, command, env)
    return _launch_linux(folder, command, env)


def resolve_folder(
    config: Any, pcm: Any, *, chat_id: str = "", project_id: str = ""
) -> Path:
    """The folder to open, from server state only."""
    from ciao import workspaces

    if not chat_id and not project_id:
        raise ValueError("chat_id or project_id is required")
    project: Any = None
    if chat_id:
        chat = pcm.get_chat(chat_id)
        if chat is None:
            raise LookupError("chat not found")
        project = pcm.get_project(chat.project_id)
        if chat.code_mode and project and project.repo_path:
            loc = chat.repo_location or {}
            path = loc.get("path")
            if path and loc.get("exists", True) and Path(path).is_dir():
                return Path(path).resolve()
    else:
        project = pcm.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
    if project and project.repo_path and Path(project.repo_path).is_dir():
        return Path(project.repo_path).resolve()
    root = workspaces.agent_root_for(config, project.workspace if project else "")
    if root.is_dir():
        return root.resolve()
    raise FileNotFoundError(str(root))
