"""``ciao.host_terminal``: the allowlist, the per-platform argv and folder resolution.

Nothing is ever launched: ``_spawn`` is replaced by a recorder, and
``sys.platform``, ``_which`` and ``terminal_path`` are patched per test.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import host_terminal, setup_status
from ciao.providers import opencode

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Spawns:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str], int]] = []

    def __call__(
        self, argv: list[str], *, cwd: Path, env: dict[str, str], creationflags: int = 0
    ) -> None:
        self.calls.append((argv, cwd, env, creationflags))


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    *,
    found: set[str] | None = None,
    display: bool = True,
) -> _Spawns:
    spawns = _Spawns()
    available = found or set()
    monkeypatch.setattr(host_terminal.sys, "platform", platform)
    monkeypatch.setattr(host_terminal, "_which", lambda name: f"/bin/{name}" if name in available else None)
    monkeypatch.setattr(host_terminal, "terminal_path", lambda: "/term/path")
    monkeypatch.setattr(host_terminal, "_spawn", spawns)
    for key in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", ":0")
    return spawns


def test_actions_map_to_the_commands_ciaobot_already_shows() -> None:
    assert host_terminal.action_command("claude-install") == setup_status.claude_install_command()
    assert host_terminal.action_command("claude-login") == setup_status.claude_auth_command(
        setup_status.claude_cli_path()
    )
    assert host_terminal.action_command("opencode-install") == opencode.opencode_install_command()
    assert host_terminal.action_command("opencode-login") == opencode.OPENCODE_LOGIN_COMMAND
    assert host_terminal.action_command("gws-install") == host_terminal.GWS_INSTALL_COMMAND
    assert host_terminal.action_command("skills-update") == host_terminal.SKILLS_UPDATE_COMMAND
    assert set(host_terminal.ACTION_IDS) == {
        "claude-install", "claude-login", "opencode-install",
        "opencode-login", "gws-install", "skills-update",
    }
    with pytest.raises(KeyError):
        host_terminal.action_command("rm")


def test_frontend_copy_names_the_gws_install_command() -> None:
    for rel in ("web/src/components/settings/GwsSetupChecklist.vue", "web/src/components/SettingsView.vue"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert host_terminal.GWS_INSTALL_COMMAND in text, rel


def test_macos_opens_terminal_at_a_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spawns = _setup(monkeypatch, "darwin")
    folder = tmp_path / "proj"
    folder.mkdir()
    assert host_terminal.launch(folder, action=None, runtime_dir=tmp_path / "rt") == "Terminal"
    assert spawns.calls[0][0] == ["/usr/bin/open", "-a", "Terminal", str(folder)]


def test_macos_runs_a_command_through_a_self_deleting_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawns = _setup(monkeypatch, "darwin")
    folder = tmp_path / "proj"
    folder.mkdir()
    runtime = tmp_path / "rt"
    host_terminal.launch(folder, action="gws-install", runtime_dir=runtime)
    argv = spawns.calls[0][0]
    assert argv[:3] == ["/usr/bin/open", "-a", "Terminal"]
    script = Path(argv[3])
    assert script.parent == runtime and script.suffix == ".command"
    text = script.read_text(encoding="utf-8")
    assert 'rm -f -- "$0"' in text
    assert f"cd -- {shlex.quote(str(folder))}" in text
    assert host_terminal.GWS_INSTALL_COMMAND + "\n" in text
    if os.name == "posix":
        assert script.stat().st_mode & 0o777 == 0o700


def test_macos_quotes_a_hostile_folder_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spawns = _setup(monkeypatch, "darwin")
    folder = tmp_path / "a'b $(x) ;c"
    folder.mkdir()
    host_terminal.launch(folder, action="skills-update", runtime_dir=tmp_path / "rt")
    text = Path(spawns.calls[0][0][3]).read_text(encoding="utf-8")
    assert f"cd -- {shlex.quote(str(folder))} || exit 1" in text
    assert not any(line.startswith("cd -- ") and "'" not in line for line in text.splitlines())
    assert "\n$(x)" not in text


def test_windows_prefers_windows_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spawns = _setup(monkeypatch, "win32", found={"wt", "powershell.exe"})
    assert host_terminal.launch(tmp_path, action=None, runtime_dir=tmp_path) == "Windows Terminal"
    argv, cwd, _env, flags = spawns.calls[0]
    assert argv == ["/bin/wt", "-d", str(tmp_path)]
    assert flags == 0x8 | 0x200
    assert cwd == tmp_path


def test_windows_terminal_runs_the_command_in_powershell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawns = _setup(monkeypatch, "win32", found={"wt", "powershell.exe"})
    monkeypatch.setattr(host_terminal, "SKILLS_UPDATE_COMMAND", "a; b")
    host_terminal.launch(tmp_path, action="skills-update", runtime_dir=tmp_path)
    argv = spawns.calls[0][0]
    assert argv[-5:] == ["-NoExit", "-ExecutionPolicy", "RemoteSigned", "-Command", "a\; b"]


def test_windows_falls_back_to_a_powershell_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spawns = _setup(monkeypatch, "win32", found={"powershell.exe"})
    assert host_terminal.launch(tmp_path, action="gws-install", runtime_dir=tmp_path) == "PowerShell"
    argv, cwd, _env, flags = spawns.calls[0]
    assert argv[:2] == ["/bin/powershell.exe", "-NoExit"]
    assert argv[-1] == host_terminal.GWS_INSTALL_COMMAND
    assert flags == 0x10 | 0x200
    assert cwd == tmp_path


def test_windows_claude_login_uses_the_call_operator_for_a_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup(monkeypatch, "win32")
    path = "C:\\Users\\O'Neil\\claude.exe"
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: path)
    monkeypatch.setattr(setup_status, "claude_auth_command", lambda cli="": "whatever auth login")
    assert host_terminal.action_command("claude-login") == "& 'C:\\Users\\O''Neil\\claude.exe' auth login"
    monkeypatch.setattr(setup_status, "claude_auth_command", lambda cli="": "claude auth login")
    assert host_terminal.action_command("claude-login") == "claude auth login"


def test_linux_without_a_display_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spawns = _setup(monkeypatch, "linux", found={"gnome-terminal"}, display=False)
    caps = host_terminal.capabilities()
    assert not caps.available and caps.reason == "unavailable_headless"
    assert caps.as_dict()["actions"] == []
    with pytest.raises(host_terminal.TerminalUnavailable) as exc:
        host_terminal.launch(tmp_path, action=None, runtime_dir=tmp_path)
    assert exc.value.reason == "unavailable_headless"
    assert spawns.calls == []


def test_linux_without_a_known_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, "linux")
    assert host_terminal.capabilities().reason == "no_terminal_found"


@pytest.mark.parametrize("with_command", [False, True])
@pytest.mark.parametrize("name", list(host_terminal._LINUX_TERMINALS))
def test_linux_terminal_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, with_command: bool
) -> None:
    spawns = _setup(monkeypatch, "linux", found={name})
    assert host_terminal.launch(
        tmp_path, action="gws-install" if with_command else None, runtime_dir=tmp_path
    ) == name
    b, f, q = f"/bin/{name}", str(tmp_path), shlex.quote(str(tmp_path))
    shell = '"${SHELL:-/bin/bash}" -l'
    if with_command:
        s = f"cd -- {q} && {host_terminal.GWS_INSTALL_COMMAND}; exec {shell}"
    else:
        s = f"cd -- {q} && exec {shell}"
    expected: dict[str, list[str]] = {
        "x-terminal-emulator": [b, "-e", "bash", "-lc", s],
        "gnome-terminal": [b, f"--working-directory={f}"] + (["--", "bash", "-lc", s] if with_command else []),
        "konsole": [b, "--workdir", f] + (["-e", "bash", "-lc", s] if with_command else []),
        "xfce4-terminal": [b, f"--working-directory={f}"] + (["-x", "bash", "-lc", s] if with_command else []),
        "kitty": [b, "--directory", f] + (["bash", "-lc", s] if with_command else []),
        "alacritty": [b, "--working-directory", f] + (["-e", "bash", "-lc", s] if with_command else []),
    }
    assert spawns.calls[0][0] == expected[name]
    assert spawns.calls[0][1] == tmp_path


def test_terminal_env_drops_engine_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, "linux")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "x")
    monkeypatch.setenv("CIAO_AGENT_TOKEN", "x")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "x")
    monkeypatch.setenv("HOME", "/home/me")
    env = host_terminal._terminal_env()
    for key in ("ANTHROPIC_API_KEY", "PWA_AUTH_TOKEN", "CIAO_AGENT_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"):
        assert key not in env
    assert env["HOME"] == "/home/me"
    assert env["PATH"] == "/term/path"


def test_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup(monkeypatch, "sunos5")
    assert host_terminal.capabilities().reason == "unsupported_platform"


# --- resolve_folder ---------------------------------------------------------


class _Pcm:
    def __init__(self, chats: dict[str, Any], projects: dict[str, Any]) -> None:
        self._chats, self._projects = chats, projects

    def get_chat(self, chat_id: str) -> Any:
        return self._chats.get(chat_id)

    def get_project(self, project_id: str) -> Any:
        return self._projects.get(project_id)


def _config(root: Path) -> Any:
    return SimpleNamespace(
        workspace_root=root,
        workspace=lambda name: None,
        agent_root=lambda name: root,
    )


def _resolve_fixture(tmp_path: Path, *, code_mode: bool, exists: bool = True) -> tuple[Any, Any, Path, Path, Path]:
    root = tmp_path / "root"
    repo = tmp_path / "repo"
    tree = tmp_path / "tree"
    for p in (root, repo, tree):
        p.mkdir()
    project = SimpleNamespace(id="p", repo_path=str(repo), workspace="")
    chat = SimpleNamespace(
        id="c", project_id="p", code_mode=code_mode,
        repo_location={"path": str(tree), "exists": exists},
    )
    return _config(root), _Pcm({"c": chat}, {"p": project}), root, repo, tree


def test_resolve_folder_prefers_a_code_mode_worktree(tmp_path: Path) -> None:
    config, pcm, _root, _repo, tree = _resolve_fixture(tmp_path, code_mode=True)
    assert host_terminal.resolve_folder(config, pcm, chat_id="c") == tree.resolve()


def test_resolve_folder_ignores_the_worktree_outside_code_mode(tmp_path: Path) -> None:
    config, pcm, _root, repo, _tree = _resolve_fixture(tmp_path, code_mode=False)
    assert host_terminal.resolve_folder(config, pcm, chat_id="c") == repo.resolve()


def test_resolve_folder_skips_a_missing_worktree(tmp_path: Path) -> None:
    config, pcm, _root, repo, tree = _resolve_fixture(tmp_path, code_mode=True, exists=False)
    assert host_terminal.resolve_folder(config, pcm, chat_id="c") == repo.resolve()
    tree.rmdir()
    assert host_terminal.resolve_folder(config, pcm, chat_id="c") == repo.resolve()


def test_resolve_folder_falls_back_to_the_agent_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    project = SimpleNamespace(id="p", repo_path="", workspace="")
    pcm = _Pcm({}, {"p": project})
    assert host_terminal.resolve_folder(_config(root), pcm, project_id="p") == root.resolve()
    with pytest.raises(FileNotFoundError):
        host_terminal.resolve_folder(_config(tmp_path / "gone"), pcm, project_id="p")


def test_resolve_folder_unknown_chat_is_lookup_error(tmp_path: Path) -> None:
    pcm = _Pcm({}, {})
    with pytest.raises(LookupError):
        host_terminal.resolve_folder(_config(tmp_path), pcm, chat_id="nope")
    with pytest.raises(LookupError):
        host_terminal.resolve_folder(_config(tmp_path), pcm, project_id="nope")


def test_resolve_folder_needs_an_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        host_terminal.resolve_folder(_config(tmp_path), _Pcm({}, {}))
