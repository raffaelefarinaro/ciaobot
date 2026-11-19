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
import secrets
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ciao.tool_path import terminal_path

PLATFORM_MACOS, PLATFORM_LINUX, PLATFORM_OTHER = ("macos", "linux", "other")

# npm 12 blocks install scripts by default, and the package's postinstall is what
# downloads the `gws` binary, so a bare install "succeeds" with no binary. npm 11.x
# also accepts the flag (documented under `allow-scripts`); an npm that predates it
# only warns about an unknown config key and runs the scripts anyway.
GWS_INSTALL_COMMAND = "npm install -g --allow-scripts=@googleworkspace/cli @googleworkspace/cli"
# Both packages pinned: the adapter bundles @openai/codex ^0.166.0, and the CLI here must match
# the one `codex login` writes credentials for. Bump deliberately with the adapter pin.
CODEX_INSTALL_COMMAND = (
    "npm install -g @openai/codex@0.166.0 @agentclientprotocol/codex-acp@2.7.0"
)
SKILLS_UPDATE_COMMAND = "npx skills update -g -y"


class NothingToUpdate(Exception):
    """An ``*-update`` action was requested but the provider is not older than tested."""


class NotInstalled(Exception):
    """An action needs a CLI that is not on this computer's terminal PATH, so there is no command to run."""


ACTION_IDS: tuple[str, ...] = (
    "claude-install",
    "claude-login",
    "opencode-install",
    "opencode-login",
    "codex-install",
    "codex-login",
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
    if sys.platform.startswith("linux"):
        return PLATFORM_LINUX
    return PLATFORM_OTHER


def _which(name: str) -> str | None:
    return shutil.which(name, path=terminal_path() or None)


def _spawn(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
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
    if platform == PLATFORM_LINUX:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return Capabilities(False, platform, reason="unavailable_headless")
        found = _linux_terminal()
        if found is None:
            return Capabilities(False, platform, reason="no_terminal_found")
        return Capabilities(True, platform, terminal=found[0])
    return Capabilities(False, platform, reason="unsupported_platform")


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
        # The ACP adapter Ciaobot runs chats through is always needed. The CLI installer
        # runs only when no CLI resolves, so an existing install is not reinstalled.
        adapter = setup_status.claude_adapter_install_command()
        if setup_status.claude_cli_path():
            return adapter
        return f"{setup_status.claude_install_command()} && {adapter}"
    if action == "claude-login":
        return setup_status.claude_auth_command(setup_status.claude_cli_path())
    if action == "opencode-install":
        return opencode.opencode_install_command()
    if action == "opencode-login":
        return opencode.OPENCODE_LOGIN_COMMAND
    if action == "codex-install":
        return CODEX_INSTALL_COMMAND
    if action == "codex-login":
        from ciao.providers.acp import agents

        try:
            argv = agents.codex_auth_command()
        except FileNotFoundError as exc:
            raise NotInstalled(str(exc)) from exc
        return shlex.join(argv)
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
    return _launch_linux(folder, command, env)


FolderLocation = Literal["workspace", "repo"]


def folder_choices(
    config: Any, pcm: Any, *, chat_id: str = "", project_id: str = ""
) -> dict[FolderLocation, Path]:
    """The folders a terminal for this target may open in, from server state only.

    ``workspace`` is the workspace's agent root; ``repo`` is the linked
    repository, or the chat's code-mode worktree when it has one (a chat is in
    code mode when its project's linked repository exists). Only folders
    that exist are listed, repo first: the first entry is the default.
    """
    from ciao import workspaces

    if not chat_id and not project_id:
        raise ValueError("chat_id or project_id is required")
    project: Any = None
    repo: Path | None = None
    if chat_id:
        chat = pcm.get_chat(chat_id)
        if chat is None:
            raise LookupError("chat not found")
        project = pcm.get_project(chat.project_id)
        if pcm.linked_repo_for_chat(chat):
            loc = chat.repo_location or {}
            path = loc.get("path")
            if path and loc.get("exists", True) and Path(path).is_dir():
                repo = Path(path).resolve()
    else:
        project = pcm.get_project(project_id)
        if project is None:
            raise LookupError("project not found")
    if repo is None and project and project.repo_path and Path(project.repo_path).is_dir():
        repo = Path(project.repo_path).resolve()
    choices: dict[FolderLocation, Path] = {}
    if repo is not None:
        choices["repo"] = repo
    root = workspaces.agent_root_for(config, project.workspace if project else "")
    # A repository that is the agent root itself is one folder, not a choice.
    if root.is_dir() and root.resolve() != repo:
        choices["workspace"] = root.resolve()
    if not choices:
        raise FileNotFoundError(str(root))
    return choices


def resolve_folder(
    config: Any,
    pcm: Any,
    *,
    chat_id: str = "",
    project_id: str = "",
    location: FolderLocation | None = None,
) -> Path:
    """The folder to open, from server state only: ``location``, else the default."""
    choices = folder_choices(config, pcm, chat_id=chat_id, project_id=project_id)
    if location is None:
        return next(iter(choices.values()))
    folder = choices.get(location)
    if folder is None:
        raise FileNotFoundError(f"no {location} folder for this target")
    return folder
