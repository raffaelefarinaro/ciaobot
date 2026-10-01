"""ciao.os_support.tool_path: one answer per OS, and the parts shared by both.

The helpers are defined once, above the platform split, so the interesting logic
— expanding a registry PATH, searching one with ``PATHEXT``, reading an npm
shim's own target — is exercised on whichever OS runs the tests, including the
POSIX CI that gates every change. What is left per platform is the call into the
operating system (the login-shell spawn, the registry read, the curated tool
directories), and those tests are pinned to the OS that owns them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ciao.os_support import tool_path as os_tool_path
from ciao.os_support.tool_path import ToolResolutionError

# The two wrappers `npm install -g @opencode/cli` leaves on a Windows 11
# machine, verbatim: the .cmd a PATHEXT search finds (PATHEXT has no .PS1) and
# the .ps1 beside it. Both name the same executable; only the spelling of "the
# shim's own directory" differs, and the reader has to handle both.
OPENCODE_CMD_SHIM = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
"%dp0%\\node_modules\\@opencode\\cli\\bin\\opencode.exe"   %*
"""

OPENCODE_PS1_SHIM = """#!/usr/bin/env pwsh
$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent

$exe=""
if ($PSVersionTable.PSVersion -lt "6.0" -or $IsWindows) {
  $exe=".exe"
}
if ($MyInvocation.ExpectingInput) {
  $input | & "$basedir/node_modules/@opencode/cli/bin/opencode.exe"   $args
} else {
  & "$basedir/node_modules/@opencode/cli/bin/opencode.exe"   $args
}
exit $LASTEXITCODE
"""

# What `npm install -g` writes for a package whose entry point is a script
# (npm's own `cmd-shim` template): the interpreter in a variable, then the
# script beside it. There is no single executable behind it, and `node.exe` is
# emphatically not the tool.
SCRIPT_PACKAGE_SHIM = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
SET "_prog=%dp0%\\node.exe"
IF EXIST "%_prog%" (
  "%_prog%" "%dp0%\\node_modules\\pkg\\bin\\run.js" %*
) ELSE (
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node "%dp0%\\node_modules\\pkg\\bin\\run.js" %*
)
"""

# The same package with an extensionless entry point, and its .ps1 twin.
EXTENSIONLESS_SHIM = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0
SET "_prog=%dp0%\\node.exe"
IF EXIST "%_prog%" (
  "%_prog%" "%dp0%\\node_modules\\pkg\\bin\\tool" %*
) ELSE (
  node "%dp0%\\node_modules\\pkg\\bin\\tool" %*
)
"""

EXTENSIONLESS_PS1_SHIM = """#!/usr/bin/env pwsh
$basedir=Split-Path $MyInvocation.MyCommand.Definition -Parent

$exe=""
if ($PSVersionTable.PSVersion -lt "6.0" -or $IsWindows) {
  $exe=".exe"
}
$ret=0
if (Test-Path "$basedir/node$exe") {
  if ($MyInvocation.ExpectingInput) {
    $input | & "$basedir/node$exe"  "$basedir/node_modules/pkg/bin/tool" $args
  } else {
    & "$basedir/node$exe"  "$basedir/node_modules/pkg/bin/tool" $args
  }
  $ret = $LASTEXITCODE
} else {
  if ($MyInvocation.ExpectingInput) {
    $input | & "node$exe"  "$basedir/node_modules/pkg/bin/tool" $args
  } else {
    & "node$exe"  "$basedir/node_modules/pkg/bin/tool" $args
  }
  $ret = $LASTEXITCODE
}
exit $ret
"""

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the POSIX branch is not defined on Windows"
)
windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the Windows branch is not defined elsewhere"
)


def _fake_npm_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point %APPDATA% at ``tmp_path`` and hand back the npm bin dir inside it."""
    appdata = tmp_path / "AppData" / "Roaming"
    (appdata / "npm").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    return appdata / "npm"


def _opencode_exe(npm_dir: Path) -> Path:
    return npm_dir / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"


# ── %VAR% expansion and the registry join ─────────────────────────────────


def test_expand_vars_expands_only_percent_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """RegExpandString expands %NAME% and nothing else."""
    monkeypatch.setenv("CiaoTestVar", "C:\\Program Files\\Thing")
    assert (
        os_tool_path._expand_vars("%CiaoTestVar%\\bin")
        == "C:\\Program Files\\Thing\\bin"
    )
    # A Windows PATH entry may hold a literal $, and expanding it would corrupt
    # a real directory.
    assert os_tool_path._expand_vars("$CiaoTestVar") == "$CiaoTestVar"
    # A name that is not set stays broken and stays visible, as the OS leaves it.
    assert os_tool_path._expand_vars("%CiaoTestUnset%") == "%CiaoTestUnset%"
    assert os_tool_path._expand_vars("C:\\plain") == "C:\\plain"


def test_join_path_sources_keeps_machine_entries_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """The order Windows combines the two halves in, with one expansion pass."""
    monkeypatch.setenv("CiaoTestVar", "C:\\Windows")
    machine = "%CiaoTestVar%\\system32;%CiaoTestVar%;C:\\Program Files\\Git\\cmd;"
    user = "C:\\Users\\me\\.local\\bin;%CiaoTestVar%;C:\\Users\\me\\AppData\\Roaming\\npm;"
    joined = os_tool_path._join_path_sources([machine, user]).split(";")
    assert joined == [
        "C:\\Windows\\system32",
        "C:\\Windows",
        "C:\\Program Files\\Git\\cmd",
        "C:\\Users\\me\\.local\\bin",
        "C:\\Users\\me\\AppData\\Roaming\\npm",
    ]


def test_join_path_sources_drops_whitespace_and_empty_entries() -> None:
    """A trailing `;` and stray spaces are what a hand-edited registry value has."""
    joined = os_tool_path._join_path_sources(["  C:\\a ; ;C:\\b  ", "", "   "])
    assert joined == "C:\\a;C:\\b"


def test_join_path_sources_strips_the_quotes_a_registry_value_can_carry() -> None:
    """A quoted entry is one directory, not a name with a quote on it."""
    joined = os_tool_path._join_path_sources(['"C:\\Program Files\\Git\\cmd"'])
    assert joined == "C:\\Program Files\\Git\\cmd"


@windows_only
def test_join_path_sources_deduplicates_the_way_windows_compares() -> None:
    """`C:\\Tools` and `c:\\tools` are one directory; `normcase` is the identity on
    POSIX, where they would really be two."""
    joined = os_tool_path._join_path_sources(["C:\\Tools;C:\\bin", "c:\\tools"]).split(";")
    assert joined == ["C:\\Tools", "C:\\bin"]


# ── reading the target out of an npm shim ─────────────────────────────────


@pytest.mark.parametrize(
    ("name", "body"),
    [
        pytest.param("opencode.cmd", OPENCODE_CMD_SHIM, id="cmd"),
        pytest.param("opencode.ps1", OPENCODE_PS1_SHIM, id="ps1"),
    ],
)
def test_shim_executable_reads_the_target_out_of_the_file(
    tmp_path: Path, name: str, body: str
) -> None:
    """The shim says which file it launches; that answer is the only one used."""
    shim = tmp_path / name
    shim.write_text(body, encoding="utf-8", newline="")
    target = os_tool_path._shim_executable(shim)
    assert target
    assert Path(target) == tmp_path / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"


@pytest.mark.parametrize(
    ("name", "body"),
    [
        pytest.param("run.cmd", SCRIPT_PACKAGE_SHIM, id="script-cmd"),
        pytest.param("tool.cmd", EXTENSIONLESS_SHIM, id="extensionless-cmd"),
        pytest.param("tool.ps1", EXTENSIONLESS_PS1_SHIM, id="extensionless-ps1"),
    ],
)
def test_shim_executable_never_resolves_to_the_interpreter(
    tmp_path: Path, name: str, body: str
) -> None:
    """A package whose entry point is a script launches an interpreter.

    npm writes that interpreter into a variable (`SET "_prog=…\\node.exe"`) and
    quotes it on the launch line, so scanning the whole file for a quoted `.exe`
    found `node.exe` and returned it. Spawning that would run node with no
    arguments, so the reader takes the launch line's program token and refuses.
    """
    shim = tmp_path / name
    shim.write_text(body, encoding="utf-8", newline="")
    assert os_tool_path._shim_executable(shim) == ""
    with pytest.raises(ToolResolutionError) as raised:
        os_tool_path._unwrap_shim("tool", str(shim))
    assert "node.exe" not in str(raised.value).split("launches", 1)[-1]


def test_shim_launch_program_is_the_first_quoted_token_on_the_launch_line(
    tmp_path: Path,
) -> None:
    """`SET "_prog=…\\node.exe"` is setup, not the program, even though it quotes
    an `.exe`. The program is the first quoted token of the last `%*` line."""
    shim = tmp_path / "run.cmd"
    shim.write_text(SCRIPT_PACKAGE_SHIM, encoding="utf-8", newline="")
    program = os_tool_path._shim_launch_program(shim)
    # The last `%*` line is the ELSE branch, which runs bare `node` and quotes
    # only the script beside it.
    assert program == "%dp0%\\node_modules\\pkg\\bin\\run.js"
    assert "_prog=" not in program
    assert "node.exe" not in program


def test_shim_executable_is_empty_for_a_file_that_names_none(tmp_path: Path) -> None:
    shim = tmp_path / "handwritten.cmd"
    shim.write_text("@echo off\r\ngit status\r\n", encoding="utf-8", newline="")
    assert os_tool_path._shim_executable(shim) == ""


def test_shim_executable_is_empty_for_an_unreadable_shim(tmp_path: Path) -> None:
    assert os_tool_path._shim_executable(tmp_path / "absent.cmd") == ""


def test_expand_shim_dir_leaves_an_absolute_target_alone() -> None:
    assert os_tool_path._expand_shim_dir("C:\\other\\x.exe", "C:\\npm") == "C:\\other\\x.exe"


@pytest.mark.parametrize(
    ("written", "expected_tail"),
    [
        pytest.param("%dp0%\\node_modules\\pkg\\bin.exe", "node_modules/pkg/bin.exe", id="modern-cmd"),
        pytest.param("%~dp0\\node.exe", "node.exe", id="cmd-shim-with-separator"),
        pytest.param("%~dp0node.exe", "node.exe", id="cmd-shim-without-separator"),
        pytest.param("$basedir/node_modules/@opencode/cli/bin/opencode.exe",
                     "node_modules/@opencode/cli/bin/opencode.exe", id="ps1"),
    ],
)
def test_expand_shim_dir_handles_every_spelling_npm_writes(
    written: str, expected_tail: str
) -> None:
    """npm has three spellings for "this shim's own directory" in templates that
    are all still in use, and the reader has to resolve each to one path."""
    expanded = os_tool_path._expand_shim_dir(written, "C:/npm")
    assert Path(expanded) == Path("C:/npm") / Path(expected_tail)


# ── PATHEXT ───────────────────────────────────────────────────────────────


def test_pathext_suffixes_come_from_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.CMD;")
    assert os_tool_path._pathext_suffixes() == [".COM", ".EXE", ".CMD"]


def test_pathext_suffixes_fall_back_when_the_process_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service does not inherit PATHEXT; the bare name would then match nothing."""
    monkeypatch.delenv("PATHEXT", raising=False)
    assert os_tool_path._pathext_suffixes() == list(os_tool_path._DEFAULT_PATHEXT)


def test_search_pathext_finds_an_executable_by_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.CMD")
    binary = tmp_path / "git.EXE"
    binary.write_bytes(b"MZ")
    assert os_tool_path._search_pathext("git", str(tmp_path)) == str(binary)


def test_search_pathext_follows_pathext_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    (tmp_path / "tool.CMD").write_bytes(b"")
    exe = tmp_path / "tool.EXE"
    exe.write_bytes(b"")
    # .EXE comes first in PATHEXT, so it wins even though .CMD is written first.
    assert os_tool_path._search_pathext("tool", str(tmp_path)) == str(exe)


def test_search_pathext_runs_a_command_that_already_carries_a_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    shim = tmp_path / "opencode.CMD"
    shim.write_text("", encoding="utf-8")
    assert os_tool_path._search_pathext("opencode.CMD", str(tmp_path)) == str(shim)


def test_search_pathext_finds_nothing_for_an_unknown_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    assert os_tool_path._search_pathext("definitely-not-real", str(tmp_path)) is None


def test_search_pathext_never_searches_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file dropped in a workspace must not become spawnable as a tool."""
    monkeypatch.setenv("PATHEXT", ".EXE")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "claude.exe").write_bytes(b"")
    monkeypatch.chdir(workspace)
    assert os_tool_path._search_pathext("claude", str(elsewhere)) is None


@pytest.mark.parametrize("path", ["", ";", ";" + os.pathsep])
def test_search_pathext_on_an_empty_path_finds_nothing(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert os_tool_path._search_pathext("git", path) is None


# ── wrappers on PATH ──────────────────────────────────────────────────────


def test_npm_bin_dir_is_appdata_npm_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one place `%APPDATA%\\npm` is spelled, read by the Windows tool-dir
    list; None means the variable is unset, so there is no such directory."""
    monkeypatch.delenv("APPDATA", raising=False)
    assert os_tool_path._npm_bin_dir() is None
    monkeypatch.setenv("APPDATA", "C:/Users/me/AppData/Roaming")
    assert os_tool_path._npm_bin_dir() == Path("C:/Users/me/AppData/Roaming/npm")


def test_a_wrapper_is_unwrapped_in_any_prefix_not_just_the_npm_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nvm-windows, Volta and a custom `npm prefix` all put the wrapper
    somewhere other than `%APPDATA%\\npm`, and returning it means spawning
    `cmd.exe` — the bug this exists to fix."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-such-appdata"))
    for prefix in ("nvm/node-v22.11.0", "Volta/tools/image/npm/6.9.0/bin", "opt/npm"):
        directory = tmp_path / prefix
        target = directory / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"MZ")
        shim = directory / "opencode.cmd"
        shim.write_text(OPENCODE_CMD_SHIM, encoding="utf-8", newline="")
        assert os_tool_path._unwrap_shim("opencode", str(shim)) == str(target)


def test_a_wrapper_outside_every_prefix_is_never_returned_as_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.cmd` Ciaobot cannot read must not be handed back as the executable."""
    monkeypatch.delenv("APPDATA", raising=False)
    shim = tmp_path / "handwritten.cmd"
    shim.write_text("@echo off\r\ngit status\r\n", encoding="utf-8", newline="")
    with pytest.raises(ToolResolutionError):
        os_tool_path._unwrap_shim("git", str(shim))


def test_unwrap_shim_returns_the_executable_behind_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_dir = _fake_npm_dir(tmp_path, monkeypatch)
    target = _opencode_exe(npm_dir)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    shim = npm_dir / "opencode.cmd"
    shim.write_text(OPENCODE_CMD_SHIM, encoding="utf-8", newline="")

    assert os_tool_path._unwrap_shim("opencode", str(shim)) == str(target)


def test_unwrap_shim_fails_clearly_when_the_target_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken install is not a tool that is not installed: say which and what."""
    npm_dir = _fake_npm_dir(tmp_path, monkeypatch)
    shim = npm_dir / "opencode.cmd"
    shim.write_text(OPENCODE_CMD_SHIM, encoding="utf-8", newline="")

    with pytest.raises(ToolResolutionError) as raised:
        os_tool_path._unwrap_shim("opencode", str(shim))
    message = str(raised.value)
    assert str(shim) in message
    assert str(_opencode_exe(npm_dir)) in message


def test_unwrap_shim_leaves_a_real_executable_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_dir = _fake_npm_dir(tmp_path, monkeypatch)
    binary = npm_dir / "claude.exe"
    binary.write_bytes(b"MZ")
    assert os_tool_path._unwrap_shim("claude", str(binary)) == str(binary)


def test_tool_resolution_error_is_an_os_error() -> None:
    """`providers.opencode`'s model list already degrades on OSError."""
    assert issubclass(ToolResolutionError, OSError)


# ── the POSIX branch: the login-shell probe ───────────────────────────────


def _probe_counter(
    monkeypatch: pytest.MonkeyPatch, value: str = "/usr/bin:/opt/homebrew/bin"
) -> list[int]:
    """Replace the login-shell spawn with a counter."""
    calls: list[int] = []

    def fake_probe() -> str:
        calls.append(1)
        return value

    monkeypatch.setattr(os_tool_path, "_probe_terminal_path", fake_probe)
    os_tool_path.clear_terminal_path_cache()
    return calls


@posix_only
def test_terminal_path_reads_the_shell_output_through_the_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noisy rc file echoes banners to stdout; the markers keep them out."""

    class _Result:
        returncode = 0
        stdout = f"banner\n{os_tool_path._START}/usr/local/bin{os_tool_path._END}\ntrailing"
        stderr = ""

    monkeypatch.setattr(os_tool_path.subprocess, "run", lambda *a, **k: _Result())
    os_tool_path.clear_terminal_path_cache()
    assert os_tool_path.terminal_path() == "/usr/local/bin"
    os_tool_path.clear_terminal_path_cache()


@posix_only
def test_terminal_path_does_not_inherit_our_own_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine prepends common_tool_dirs() to its own PATH at startup
    (ciao/main.py). Handing that to the probe would make ~/.local/bin come back
    as if the user's terminal had it, and setup would then tell them to type a
    bare command their shell cannot resolve."""
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = f"{os_tool_path._START}/usr/bin{os_tool_path._END}"
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(os_tool_path.subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert os_tool_path.terminal_path() == "/usr/bin"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "PATH" not in env
    # Everything else the rc files may need is still there.
    assert env["HOME"] == os.environ["HOME"]
    os_tool_path.clear_terminal_path_cache()


@posix_only
def test_terminal_path_does_not_respawn_a_shell_when_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard polls every 2s; each poll used to cost a ~0.74s login shell."""
    calls = _probe_counter(monkeypatch)
    assert os_tool_path.terminal_path() == "/usr/bin:/opt/homebrew/bin"
    for _ in range(20):
        os_tool_path.terminal_path()
    assert len(calls) == 1


@posix_only
def test_terminal_path_reprobes_when_an_rc_file_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user who adds ~/.local/bin to their rc file must see the wizard's PATH
    hint clear on the next poll, without an engine restart."""
    home = tmp_path / "home"
    (home / ".config" / "fish").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    calls = _probe_counter(monkeypatch)

    os_tool_path.terminal_path()
    os_tool_path.terminal_path()
    assert len(calls) == 1

    # Creating a previously absent rc file counts, not just touching one.
    (home / ".zshrc").write_text(
        'export PATH="$HOME/.local/bin:$PATH"\n', encoding="utf-8"
    )
    os_tool_path.terminal_path()
    assert len(calls) == 2

    # And a later edit to it.
    os.utime(home / ".zshrc", (0, 0))
    os_tool_path.terminal_path()
    assert len(calls) == 3


@posix_only
def test_terminal_path_reprobes_when_the_shell_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """$SHELL decides which rc files are read at all."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    calls = _probe_counter(monkeypatch)
    os_tool_path.terminal_path()
    os_tool_path.terminal_path()
    assert len(calls) == 1

    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    os_tool_path.terminal_path()
    assert len(calls) == 2


@posix_only
def test_concurrent_probes_share_one_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Single-flight: an rc file slower than the poll interval used to make every
    overlapping poll spawn its own login shell."""
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_probe() -> str:
        calls.append(1)
        started.set()
        release.wait(5)
        return "/usr/bin"

    monkeypatch.setattr(os_tool_path, "_probe_terminal_path", slow_probe)
    os_tool_path.clear_terminal_path_cache()

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(os_tool_path.terminal_path()))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)

    assert len(calls) == 1
    assert results == ["/usr/bin"] * 5


@posix_only
def test_common_tool_dirs_are_the_documented_fallback_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        os_tool_path.glob, "glob", lambda pattern: [str(home / "nvm" / "bin")]
    )
    dirs = os_tool_path.common_tool_dirs()
    assert "/opt/homebrew/bin" in dirs
    assert "/usr/local/bin" in dirs
    assert str(home / ".local" / "bin") in dirs
    assert str(home / "nvm" / "bin") in dirs


@posix_only
def test_resolve_executable_is_which_on_posix(tmp_path: Path) -> None:
    binary = tmp_path / "ciao-test-tool"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | 0o111)
    assert (
        os_tool_path.resolve_executable("ciao-test-tool", path=str(tmp_path)) == str(binary)
    )
    assert os_tool_path.resolve_executable("definitely-not-real", path=str(tmp_path)) is None


# ── the Windows branch: the registry, the curated dirs, the whole path ─────


@windows_only
def test_terminal_path_is_the_registry_path_machine_half_first() -> None:
    """The real machine, so a typo in either key name fails here and nowhere else."""
    import winreg

    path = os_tool_path.terminal_path()
    assert path
    entries = path.split(";")
    # %SystemRoot% came out of the REG_EXPAND_SZ value as written.
    assert "%" not in path
    root = str(Path(os.environ["SystemRoot"]))
    assert any(entry.lower().startswith(root.lower()) for entry in entries)

    machine = os_tool_path._registry_path(
        winreg.HKEY_LOCAL_MACHINE, os_tool_path._MACHINE_ENVIRONMENT
    )
    user = os_tool_path._registry_path(
        winreg.HKEY_CURRENT_USER, os_tool_path._USER_ENVIRONMENT
    )
    assert machine and user
    assert os_tool_path._join_path_sources([machine, user]) == path


@windows_only
def test_common_tool_dirs_name_where_windows_installs_clis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    dirs = os_tool_path.common_tool_dirs()
    assert str(home / ".local" / "bin") in dirs
    assert str(tmp_path / "AppData" / "Roaming" / "npm") in dirs
    assert str(tmp_path / "AppData" / "Local" / "Microsoft" / "WindowsApps") in dirs


@windows_only
def test_resolve_executable_honours_pathext_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole Windows path: PATHEXT, then the npm shim unwrapped."""
    npm_dir = _fake_npm_dir(tmp_path, monkeypatch)
    target = _opencode_exe(npm_dir)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    (npm_dir / "opencode.cmd").write_text(OPENCODE_CMD_SHIM, encoding="utf-8", newline="")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")

    assert os_tool_path.resolve_executable("opencode", path=str(npm_dir)) == str(target)
    assert os_tool_path.resolve_executable("absent", path=str(npm_dir)) is None


@windows_only
def test_resolve_executable_raises_on_a_broken_npm_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_dir = _fake_npm_dir(tmp_path, monkeypatch)
    (npm_dir / "opencode.cmd").write_text(OPENCODE_CMD_SHIM, encoding="utf-8", newline="")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    with pytest.raises(ToolResolutionError):
        os_tool_path.resolve_executable("opencode", path=str(npm_dir))


@windows_only
def test_resolve_executable_never_hands_back_a_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: whatever a PATHEXT search finds, the answer is not a `.cmd`."""
    directory = tmp_path / "volta" / "bin"
    directory.mkdir(parents=True)
    (directory / "opencode.cmd").write_text(EXTENSIONLESS_SHIM, encoding="utf-8", newline="")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    with pytest.raises(ToolResolutionError):
        os_tool_path.resolve_executable("opencode", path=str(directory))


@windows_only
def test_the_six_tools_resolve_to_files_on_this_machine() -> None:
    """The CLIs Ciaobot looks up, against this machine's own registry PATH.

    A machine without one of them installed is not a failure; a resolved path
    that is not a real file would be.
    """
    from ciao.tool_path import login_shell_path

    login_shell_path.cache_clear()
    path = login_shell_path()
    for cmd in ("claude", "opencode", "git", "gh", "uv", "gws"):
        found = os_tool_path.resolve_executable(cmd, path=path)
        if found is not None:
            assert Path(found).is_file(), f"{cmd} -> {found}"
    assert os_tool_path.resolve_executable("git", path=path) is not None


@windows_only
def test_no_subprocess_is_spawned_to_find_a_tool() -> None:
    """`$SHELL -lic` does not exist here; the registry read replaces it."""
    from ciao import tool_path

    def boom(*args, **kwargs):
        raise AssertionError(f"a subprocess was spawned: {args!r}")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "run", boom)
        patch.setattr(subprocess, "Popen", boom)
        tool_path.login_shell_path.cache_clear()
        assert tool_path.terminal_path()
        assert tool_path.resolve_tool("git")