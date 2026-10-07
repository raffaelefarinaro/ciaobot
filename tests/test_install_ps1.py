"""Tests for the Windows 11 engine installer, scripts/install.ps1 (#838, C8; #853).

The script is a text file a Windows user pipes into `iex`, so nothing here runs
PowerShell: what is asserted is the shape of the file (ASCII, no `exit`, the
constants, the verifier copy) and the order of its steps, which is the one
property a failed run cannot report. What the script asks the engine to do is
asserted against the engine itself - the parser defines the flags, the receipt
module owns the schema - so a renamed flag fails here rather than on a user's
machine. The advisory Windows CI job runs the same file under both PowerShell
hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import install_receipt, windows_service
from ciao.cli import build_parser
from ciao.os_support import shell_hints
from ciao.release_manifest import RELEASE_PUBLIC_KEY
from tests.test_engine_installer import VERSION, WHEEL_BYTES, WHEEL_NAME, _manifest_bytes
from tests.test_release_manifest import _keypair, _sign

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install.ps1"
SH_SCRIPT = REPO_ROOT / "scripts" / "install-engine.sh"
SCRIPT_TEXT = SCRIPT.read_text(encoding="ascii")
SH_TEXT = SH_SCRIPT.read_text(encoding="utf-8")


def _ps1_verifier_source() -> str:
    """The Python program the PowerShell here-string holds, as it ships."""
    lines = SCRIPT_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if "$VerifierSource = @'" in line) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line == "'@")
    return "\n".join(lines[start:end]) + "\n"


def _sh_verifier_source() -> str:
    """The Python program the sh heredoc holds, as it ships."""
    lines = SH_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'PY'" in line) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "PY")
    return "\n".join(lines[start:end]) + "\n"


def _run_verifier(
    tmp_path: Path, manifest_bytes: bytes, sig_text: str, version: str, key_text: str
) -> subprocess.CompletedProcess[str]:
    manifest = tmp_path / "ciaobot-engine-manifest.json"
    signature = tmp_path / "ciaobot-engine-manifest.json.sig"
    manifest.write_bytes(manifest_bytes)
    signature.write_text(sig_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-", str(manifest), str(signature), version, key_text],
        input=_ps1_verifier_source(),
        capture_output=True,
        text=True,
        check=False,
    )


def _top_level_statements(text: str) -> list[str]:
    """Statements outside any block, comments and blank lines dropped.

    The here-string body is cut out first: it is Python, full of braces that are
    not PowerShell braces at all.
    """
    statements: list[str] = []
    depth = 0
    for line in text.replace(_ps1_verifier_source(), "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if depth == 0:
            statements.append(stripped)
        depth += line.count("{") - line.count("}")
    assert depth == 0, "unbalanced braces in install.ps1"
    return statements


def test_install_ps1_is_ascii_and_has_no_exit() -> None:
    # Windows PowerShell 5.1 reads a BOM-less file as the ANSI code page, so a
    # non-ASCII byte anywhere here is a mangled script, not a cosmetic problem.
    SCRIPT.read_bytes().decode("ascii")

    # `irm ... | iex` runs this text in the caller's interactive session, where
    # `exit` closes their terminal; every failure below is a `throw`.
    assert not re.search(r"^\s*exit\b", SCRIPT_TEXT, re.MULTILINE), (
        "install.ps1 would close the terminal it was piped into"
    )

    assert "$ErrorActionPreference = 'Stop'" in SCRIPT_TEXT
    # TLS 1.0/1.1 is the 5.1 default on some builds, and GitHub refuses both.
    assert "Tls12" in SCRIPT_TEXT


def test_release_constants_match_install_engine_sh() -> None:
    # The two installers have to pin the same uv, the same Python and the same
    # key, or "the same verified engine" is two different things on two
    # platforms.
    names = ("UV_VERSION", "PYTHON_VERSION", "CRYPTOGRAPHY_PIN", "RELEASE_PUBLIC_KEY", "repo")
    sh = {
        match.group(1): match.group(3)
        for match in re.finditer(
            r"^(UV_VERSION|PYTHON_VERSION|CRYPTOGRAPHY_PIN|RELEASE_PUBLIC_KEY|repo)=(\"?)(.*?)\2$",
            SH_TEXT,
            re.MULTILINE,
        )
    }
    ps1 = {}
    for name, pattern in (
        ("UV_VERSION", r"\$UvVersion = '(.*?)'"),
        ("PYTHON_VERSION", r"\$PythonVersion = '(.*?)'"),
        ("CRYPTOGRAPHY_PIN", r"\$CryptographyPin = '(.*?)'"),
        ("RELEASE_PUBLIC_KEY", r"\$ReleasePublicKey = '(.*?)'"),
        ("repo", r"\$Repo = '(.*?)'"),
    ):
        match = re.search(pattern, SCRIPT_TEXT)
        assert match, f"install.ps1 no longer defines ${name}"
        ps1[name] = match.group(1)

    assert set(sh) == set(names)
    for name in names:
        assert ps1[name] == sh[name], (
            f"install.ps1 {name}={ps1[name]!r}, install-engine.sh {name}={sh[name]!r}"
        )

    # Q-05 named 3.12, but the macOS installer ships 3.13 and the release wheel
    # is built for it; a second Python here is an engine nobody tested.
    assert sh["PYTHON_VERSION"] == "3.13"

    # The key is the one ciao/release_manifest.py signs and verifies with.
    assert sh["RELEASE_PUBLIC_KEY"] == RELEASE_PUBLIC_KEY

    # The same URL shapes, so a release that installs on macOS installs here.
    for fragment in (
        "https://github.com/$Repo/releases/download",
        "https://github.com/$Repo/releases/latest",
        '"$ReleaseBase/v$Version"',
        "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-installer.ps1",
        "'ciaobot-engine-manifest.json'",
        "'ciaobot-engine-manifest.json.sig'",
    ):
        assert fragment in SCRIPT_TEXT, f"install.ps1 lost a release URL shape: {fragment!r}"


def test_embedded_verifier_matches_install_engine_sh() -> None:
    # Two copies of one verifier, on purpose: install.ps1 cannot download a
    # verifier of its own without a new release asset, and nothing new is
    # downloaded, so old release tags keep installing. A drifted copy would
    # verify under different rules on the two platforms, so it fails here.
    assert _ps1_verifier_source() == _sh_verifier_source(), (
        "the verifier embedded in install.ps1 is no longer the one install-engine.sh runs"
    )


def test_verifier_body_cannot_end_the_here_string() -> None:
    # A here-string ends at the first line whose first characters are '@, so
    # nothing in the Python body may start with it: that would leave the
    # PowerShell copy silently truncated.
    for line in _sh_verifier_source().splitlines():
        assert not line.startswith("'@"), f"the verifier body would end the here-string: {line!r}"

    closers = [line for line in SCRIPT_TEXT.splitlines() if line == "'@"]
    assert closers == ["'@"], "install.ps1 must close $VerifierSource exactly once, at column 0"


def test_embedded_verifier_still_accepts_and_rejects(tmp_path: Path) -> None:
    # Comparing the two copies proves they are equal; running the PowerShell one
    # proves it is the verifier, not a copy of it that reads the manifest.
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()
    signature = _sign(raw, private_key, key_id)

    accepted = _run_verifier(tmp_path, raw, signature, VERSION, public_key)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.split() == [
        WHEEL_NAME,
        hashlib.sha256(WHEEL_BYTES).hexdigest(),
        str(len(WHEEL_BYTES)),
    ]

    refused = _run_verifier(
        tmp_path, raw.replace(b'"1.2.3"', b'"9.9.9"'), signature, VERSION, public_key
    )
    assert refused.returncode == 1
    assert "does not match" in refused.stderr


def test_verification_precedes_install() -> None:
    # Handing a wheel to a tool environment before the signature and the digest
    # have been checked would be no check at all.
    verify = SCRIPT_TEXT.index("python $verifier")
    digest = SCRIPT_TEXT.index("Get-FileHash -LiteralPath $wheel")
    install = SCRIPT_TEXT.index("tool install")

    assert verify < digest < install

    # `iex` runs this text in the caller's scope, so the param block, the one
    # function and its call are the only things allowed at the top level: a
    # top-level assignment would leave a variable and a preference behind in an
    # interactive session.
    assert _top_level_statements(SCRIPT_TEXT) == [
        "[CmdletBinding()]",
        "param(",
        "[string]$Version = '',",
        "[string]$ReleaseDir = '',",
        "[switch]$DryRun,",
        "[string]$Workspace = '',",
        "[switch]$NoStart,",
        "[switch]$Uninstall",
        ")",
        "function Install-Ciaobot {",
        "Install-Ciaobot -Version $Version -ReleaseDir $ReleaseDir -DryRun $DryRun.IsPresent"
        " -Workspace $Workspace -NoStart $NoStart.IsPresent -Uninstall $Uninstall.IsPresent",
    ]


def test_install_ps1_has_no_powershell_crypto() -> None:
    # Signature verification runs in the same Python verifier install-engine.sh
    # uses, under uv. A second verification path in PowerShell would be a second
    # trusted code path, and 5.1 (.NET Framework) has no Ed25519 to write one
    # with, so none is allowed here.
    outside = SCRIPT_TEXT.replace(_ps1_verifier_source(), "")
    for banned in ("Ed25519", "VerifyHash", "ECDsa", "System.Security.Cryptography"):
        assert banned not in outside, (
            f"install.ps1 verifies crypto in PowerShell ({banned}) instead of the shared verifier"
        )


# --- part 2: setup, logon task, start, uninstall (#853) ---------------------


def _ps1_function(name: str) -> str:
    """The body of one nested `function Name { ... }` in the script."""
    lines = SCRIPT_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"function {name}("))
    depth = 0
    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if depth == 0 and index > start:
            return "\n".join(lines[start : index + 1])
    raise AssertionError(f"install.ps1 no longer defines a {name} function")


def _undo_source() -> str:
    return _ps1_function("Undo-Install")


def _ps1_flag(block: str, flag: str) -> bool:
    """Is `flag` passed in the PowerShell argument list `block`?"""
    return bool(re.search(rf"'{re.escape(flag)}'", block))


_PS1_TOKEN = re.compile(r"'([^']*)'|\$([A-Za-z_][A-Za-z0-9_]*)")


def _ps1_list(literal: str) -> list[str]:
    """The tokens of one PowerShell array literal, in order.

    A quoted string is the token; a `$name` is a value the script computed, and
    is kept as a marker so the argument walk below can tell a flag from the value
    that follows it. That distinction is the whole reason this is not a plain
    `findall` of quoted strings: `--workspace $workspace --load-launchd` and
    `--workspace --load-launchd` are different calls.
    """
    return [quoted if quoted else f"${name}" for quoted, name in _PS1_TOKEN.findall(literal)]


def _ciao_argument_lists() -> list[list[str]]:
    """Every argv the script builds for the engine, in order.

    The list literals are what is read: they are the whole call the engine sees,
    and a test cannot run PowerShell to find out. `$setupArgs` is the one list
    built in two statements (`@('setup', ...)` and the conditional append), so it
    is reassembled here from both - the widest argv the script can pass, which
    is what the parser has to accept.
    """
    calls: list[list[str]] = []
    for match in re.finditer(r"^.*Invoke-Native \$ciao (.*)$", SCRIPT_TEXT, re.MULTILINE):
        line = match.group(1)
        if line.startswith("$setupArgs"):
            base = re.search(r"\$setupArgs = @\((.*)\)", SCRIPT_TEXT)
            assert base, "install.ps1 builds $setupArgs with something other than a list literal"
            extra = re.findall(r"\$setupArgs \+= (@\(.*?\)|'[^']*')", SCRIPT_TEXT)
            calls.append(
                _ps1_list(base.group(1)) + [t for group in extra for t in _ps1_list(group)]
            )
            continue
        # The list literal is what the engine is given; the trailing `$true`/
        # `$false` is Invoke-Native's own $Quiet argument, not part of the call.
        array = re.search(r"@\(.*\)", line)
        assert array, (
            f"the engine is called with a computed list {line!r}; this test reads the "
            "literal arguments, so a call has to keep one"
        )
        calls.append(_ps1_list(array.group(0)))
    return calls


def _probe_argv(parser: argparse.ArgumentParser, argv: list[str]) -> list[str]:
    """`argv` with every computed value replaced by a placeholder.

    Walks the sub-parser tree the way argparse does, so a flag is only paired
    with a following value when the option actually takes one, and a sub-command
    is descended into rather than mistaken for a value. What comes out is what
    the engine is asked to accept.
    """
    current = parser
    probe: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        subparsers = next(
            (a for a in current._actions if isinstance(a, argparse._SubParsersAction)), None
        )
        if subparsers is not None and token in subparsers.choices:
            current = subparsers.choices[token]
            probe.append(token)
            index += 1
            continue
        probe.append("value" if token.startswith("$") else token)
        if token.startswith("--"):
            action = next(
                (a for a in current._actions if token in (a.option_strings or [])), None
            )
            assert action is not None, f"the parser does not define {token}"
            if action.nargs != 0 and index + 1 < len(argv):
                index += 1
                probe.append("value")
        index += 1
    return probe


def test_flags_and_defaults_match_install_engine_sh() -> None:
    # The two installers are "the same verified engine, running" or they are two
    # products, so every user-facing switch and every constant that decides what
    # a finished install looks like has to be the same on both.
    for name in ("$Workspace", "$NoStart", "$Uninstall"):
        assert name in SCRIPT_TEXT.split("param(")[1], f"install.ps1 lost the {name} parameter"
    assert "--no-start" in SH_TEXT
    assert "--workspace" in SH_TEXT

    # The default workspace: Ciaobot under the user's home, as on macOS.
    assert "$DefaultWorkspaceName = 'Ciaobot'" in SCRIPT_TEXT
    assert "Join-Path $env:USERPROFILE $DefaultWorkspaceName" in SCRIPT_TEXT
    assert '[ -n "$workspace" ] || workspace="$HOME/Ciaobot"' in SH_TEXT

    # 60 x 1s of health polling, the same budget and the same fallback port.
    assert "$HealthAttempts = 60" in SCRIPT_TEXT
    assert "health_attempts=60" in SH_TEXT
    assert "$DefaultPort = 8443" in SCRIPT_TEXT
    assert '[ -n "$port" ] || port=8443' in SH_TEXT
    assert "/api/startup-status" in SCRIPT_TEXT
    assert "/api/startup-status" in SH_TEXT

    # The two lines that reach the user as "you are signed in now". Byte-equal,
    # because this is the one moment in the install the user has to trust it.
    for line in ("Open Ciaobot: ", "This link signs you in once. Do not share it."):
        assert line in SCRIPT_TEXT, f"install.ps1 lost the sign-in line {line!r}"
        assert line in SH_TEXT, f"install-engine.sh lost the sign-in line {line!r}"

    # `ciao setup` exiting 3 means the memory regions could not be set up, which
    # is a warning and not a failed install: the same one code, and only that
    # one, is tolerated on both platforms.
    assert "3 { Write-Warning 'ciao setup could not set up memory regions" in SCRIPT_TEXT
    assert "default { Fail 'ciao setup failed' }" in SCRIPT_TEXT
    assert '3) echo "warning: ciao setup could not set up memory regions' in SH_TEXT
    assert '*) abort_install "ciao setup failed" ;;' in SH_TEXT


def test_ps1_setup_and_service_flags_exist_in_cli() -> None:
    # A flag the script passes and the parser does not define is a failed
    # install on a fresh machine, and the advisory Windows job is the only place
    # it could ever be caught. argparse itself decides, not a copy of the parser.
    parser = build_parser()
    calls = _ciao_argument_lists()
    assert calls, "install.ps1 no longer calls the engine with a literal argument list"

    for argv in calls:
        probe = _probe_argv(parser, argv)
        try:
            parser.parse_args(probe)
        except SystemExit:
            raise AssertionError(
                f"install.ps1 calls `ciao {' '.join(probe)}`, which the parser refuses"
            ) from None

    # The three calls the install is made of, and their flags.
    assert ["setup", "--workspace", "$workspace", "--load-launchd"] in calls
    assert ["service", "start", "--workspace", "$workspace", "--json"] in calls
    assert ["setup-url", "--workspace", "$workspace"] in calls

    # `--yes` turns off every guard in `ciao setup`, and a fresh install has no
    # repoint to confirm: passing it would skip the very check that stops setup
    # from hijacking an existing workspace. `--python` is the macOS LaunchAgent
    # interpreter; on win32 the task takes the running ciao's own pythonw.exe.
    setup_calls = [argv for argv in calls if argv[0] == "setup"]
    assert setup_calls, "install.ps1 no longer calls `ciao setup`"
    for argv in setup_calls:
        assert "--yes" not in argv
        assert "--python" not in argv

    # --load-launchd is what registers the task, and -NoStart is what promises
    # no task, so that one conditional is the whole of the interaction.
    assert "if (-not $NoStart) { $setupArgs += '--load-launchd' }" in SCRIPT_TEXT


def test_receipt_call_matches_install_receipt_cli(tmp_path: Path) -> None:
    # The receipt is written by the module that owns its schema, validation,
    # atomic write and owner-only DACL; the script only chooses flags. Running
    # those flags through the real `main` is what proves they are the flags the
    # module has, rather than the ones it had.
    match = re.search(
        r"Invoke-Native \$toolPython @\('-m', 'ciao\.install_receipt', 'write',(.*?)\)\s*\$true",
        SCRIPT_TEXT,
        re.DOTALL,
    )
    assert match, "install.ps1 no longer writes the receipt through ciao.install_receipt"
    flags = re.findall(r"'(--[a-z-]+)', (\$[A-Za-z]+)", match.group(1))
    assert flags, "the receipt call passes no flags"
    names = [name for name, _ in flags]
    for required in ("--version", "--executable", "--python", "--service-backend", "--service-label"):
        assert required in names, f"the receipt call is missing {required}"

    argv: list[str] = ["write"]
    values = {
        "$Version": VERSION,
        "$ciao": r"C:\Users\me\.local\bin\ciao.exe",
        "$toolPython": r"C:\Users\me\AppData\Roaming\uv\tools\ciaobot\Scripts\python.exe",
        "$ServiceBackend": "windows-task",
        "$TaskName": windows_service.TASK_NAME,
    }
    for name, variable in flags:
        assert variable in values, f"the receipt call uses an unexpected value for {name}"
        argv += [name, values[variable]]

    target = tmp_path / "install-receipt.json"
    assert install_receipt.main([*argv, "--path", str(target)]) == 0
    written = install_receipt.read_receipt(target)
    assert written is not None
    assert written.service_backend == "windows-task"
    assert written.service_label == windows_service.TASK_NAME
    assert written.executable == values["$ciao"]
    assert "windows-task" in install_receipt.SERVICE_BACKENDS

    # The task name and the XML file name are duplicated in the script because
    # `ciao` is the thing being uninstalled and defines no unregister action, so
    # schtasks has to be called directly. Both copies are pinned to the module.
    assert "$TaskName = '\\Ciaobot\\Engine'" in SCRIPT_TEXT
    assert windows_service.TASK_NAME == "\\Ciaobot\\Engine"
    assert "$TaskFileName = 'Ciaobot-Engine.xml'" in SCRIPT_TEXT
    assert windows_service.TASK_FILE_NAME == "Ciaobot-Engine.xml"
    assert "$TaskDir = Join-Path $StateDir 'service'" in SCRIPT_TEXT
    assert "$ReceiptPath = Join-Path $env:USERPROFILE '.local\\state\\ciaobot\\install-receipt.json'" in SCRIPT_TEXT


def test_path_write_matches_shell_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    # One mental model and one tested spelling: the line `ciao setup` already
    # hands a Windows user is the line the installer performs for them, so a
    # user who has read one has read the other.
    monkeypatch.setattr(sys, "platform", "win32")
    hint = shell_hints.path_hint(r"C:\Users\me\.local\bin", persist=True)
    assert "OpenSubKey('Environment', $true)" in hint
    assert "'DoNotExpandEnvironmentNames'" in hint
    assert "RegistryValueKind]::ExpandString" in hint

    for fragment in (
        "OpenSubKey('Environment', $true)",
        "GetValue('Path', '', 'DoNotExpandEnvironmentNames')",
        "SetValue('Path', $Value, $kind)",
        "RegistryValueKind]::ExpandString",
    ):
        assert fragment in SCRIPT_TEXT, f"install.ps1 writes the user PATH without {fragment!r}"

    # The prepend shape the hint uses, so the entry lands in the same place.
    assert 'return "$Directory;$PathValue"' in _ps1_function("Add-UserPathEntry")

    # Q2: not [Environment]::SetEnvironmentVariable for the PATH. It reads the
    # old value expanded and writes it as REG_SZ, which freezes every %VAR% in
    # a REG_EXPAND_SZ user PATH and changes its type under every other tool that
    # reads it. A throwaway user variable is still set and cleared, because a
    # user-scope SetEnvironmentVariable is the broadcast #854 wanted.
    assert "SetEnvironmentVariable('Path'" not in SCRIPT_TEXT
    assert "SetEnvironmentVariable('Ciaobot_Install_Notify', '1', 'User')" in SCRIPT_TEXT
    assert "SetEnvironmentVariable('Ciaobot_Install_Notify', $null, 'User')" in SCRIPT_TEXT

    # HKCU only: a machine-scope write needs an administrator this installer
    # never asks for, and would be the wrong answer for a per-user engine.
    assert "'Machine'" not in SCRIPT_TEXT
    # `uv tool update-shell` has no inverse, and -Uninstall has to remove
    # exactly the entry this run added.
    assert "update-shell" not in SCRIPT_TEXT


def _ps1_param_block(lines: list[str], after: int) -> tuple[int, int]:
    """The `param(` block that starts at or after line `after`, as a line range."""
    start = next(i for i in range(after, len(lines)) if lines[i].strip() == "param(")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == ")")
    return start, end


def test_no_local_overwrites_a_parameter() -> None:
    # PowerShell variable names are case-insensitive, so a "local" called
    # $workspace is not a new variable: it is $Workspace, the parameter the
    # caller passed. `Install-Ciaobot` reset every one of its string parameters
    # to '' near the top of the body, and that silently threw away what the user
    # asked for - the Windows CI run passed -Workspace and the log printed the
    # default directory under %USERPROFILE% instead, which is why it then
    # reported "workspace kept: no" for a workspace it had never created. There
    # is nothing to see on a user's machine: the install succeeds against the
    # wrong directory. So the parameter names are read out of the block itself
    # and no line outside a `param` block may assign '' to one of them.
    lines = SCRIPT_TEXT.splitlines()
    function = next(
        i for i, line in enumerate(lines) if line.startswith("function Install-Ciaobot {")
    )
    script_params = _ps1_param_block(lines, 0)
    function_params = _ps1_param_block(lines, function)
    names = [
        match.group(1)
        for line in lines[function_params[0] + 1 : function_params[1]]
        if (match := re.search(r"\$([A-Za-z_][A-Za-z0-9_]*)", line))
    ]
    assert names == [
        "Version",
        "ReleaseDir",
        "DryRun",
        "Workspace",
        "NoStart",
        "Uninstall",
    ], "Install-Ciaobot's parameters changed; check the declarations this test reads"

    # The two `param` blocks are declarations, and both default a string to '';
    # a nested function's `[string]$Workspace, [string]$PathEntry) {` header is
    # a declaration too, and does not start with the variable.
    declared = {
        index
        for start, end in (script_params, function_params)
        for index in range(start, end + 1)
    }
    reset = re.compile(
        rf"(?im)^[ \t]*\$(?:{'|'.join(names)})[ \t]*=[ \t]*''", re.ASCII
    )
    offenders = [
        f"{index + 1}: {line.strip()}"
        for index, line in enumerate(lines)
        if index not in declared and reset.match(line)
    ]
    assert not offenders, (
        "install.ps1 assigns '' to a parameter it was given (PowerShell names are "
        f"case-insensitive, so this is the parameter): {offenders}"
    )

    # The one assignment to a parameter that is not `''` is the workspace being
    # resolved to a full path after it is created, which keeps its meaning.
    assert "$workspace = (Get-Item -LiteralPath $Workspace).FullName" in SCRIPT_TEXT


def test_uninstall_removes_exactly_what_install_adds() -> None:
    # "Uninstall removes exactly what install adds" has to be one list, not two
    # that drift. The markers are the list: every mutation in the install body
    # and every step in the undo carry one, and the two sets have to be equal.
    adds = re.findall(r"^\s*# ADDS: (\w+)\s*$", SCRIPT_TEXT, re.MULTILINE)
    undoes = re.findall(r"^\s*# UNDOES: (\w+)\s*$", _undo_source(), re.MULTILINE)
    assert adds, "the install body no longer marks what it adds"
    assert set(adds) == set(undoes), (
        f"install adds {sorted(set(adds))} but Undo-Install undoes {sorted(set(undoes))}"
    )
    assert adds == ["tool", "receipt", "path", "state", "task", "taskfile"]

    # The workspace is the user's notes and memory. No removal of any kind may
    # name it, in the undo or anywhere else in that function.
    for line in _undo_source().splitlines():
        if "Remove-Item" in line or "Remove-" in line:
            assert "workspace" not in line.lower() and "Workspace" not in line, (
                f"Undo-Install removes the workspace: {line.strip()!r}"
            )
    assert "Your workspace was kept:" in SCRIPT_TEXT

    # The PATH entry is removed only when the state file says this installer
    # added it: the uv bin directory belongs to the user's other uv tools too.
    assert "if (($Steps -contains 'path') -and $PathEntry)" in _undo_source()
    assert "if ($state -and $state.path_entry) { $pathEntry = [string]$state.path_entry }" in SCRIPT_TEXT
    assert "if ($pathEntry) { $done += 'path' }" in SCRIPT_TEXT


def test_rerun_rollback_undoes_only_this_runs_own_steps() -> None:
    # Q1: a re-run repairs an install instead of refusing, so "undo exactly what
    # this run did" has to be enforced per step, not once for the install as a
    # whole. Each of these four was a way the earlier version undid something it
    # had not created.
    undo = _undo_source()

    # 1. `receipt` and `state` were added to $done unconditionally, so a failed
    #    repair deleted the earlier install's receipt and state file while
    #    leaving its tool in place. What a rollback may remove is what was
    #    absent before this run wrote it.
    receipt_write = SCRIPT_TEXT.index("'ciao.install_receipt', 'write',")
    assert SCRIPT_TEXT.index("$receiptWasThere = Test-Path -LiteralPath $ReceiptPath") < receipt_write
    assert "if (-not $receiptWasThere) { $done += 'receipt' }" in SCRIPT_TEXT
    assert "\n        $done += 'receipt'\n" not in SCRIPT_TEXT
    state_write = SCRIPT_TEXT.index("WriteAllText($StateFile")
    assert SCRIPT_TEXT.index("$stateWasThere = Test-Path -LiteralPath $StateFile") < state_write
    assert "if (-not $stateWasThere) { $done += 'state' }" in SCRIPT_TEXT

    # 2. A re-run finds the PATH entry already present, so $pathEntry is '' for
    #    this run. Writing that '' over install-state.json lost the record of
    #    who added the entry, and the next -Uninstall then left it behind. The
    #    previous state is read before the write and its path_entry carried over.
    assert SCRIPT_TEXT.index("$previousState = Read-InstallState") < state_write
    keep = re.search(
        r"if \(\(-not \$pathEntryAdded\) -and \$previousState -and \$previousState\.path_entry\) \{\s*"
        r"\$pathEntry = \[string\]\$previousState\.path_entry",
        SCRIPT_TEXT,
    )
    assert keep, "a repair re-run overwrites install-state.json with path_entry=''"
    # The entry this run really added is still the one it undoes.
    assert re.search(
        r"\$pathEntryAdded = \$true\s*\n\s*\$done \+= 'path'", SCRIPT_TEXT
    ), "the PATH step is only recorded as done when this run added the entry"

    # 3. A failed `ciao setup` threw before the two $done += lines below it, so
    #    a task setup had already registered was never stopped or deleted, and
    #    `uv tool uninstall` then failed on the running pythonw.exe. Both checks
    #    are above the exit-code switch.
    setup_call = SCRIPT_TEXT.index("$setup = Invoke-Native $ciao $setupArgs")
    switch = SCRIPT_TEXT.index("switch ($setup.ExitCode)")
    assert setup_call < SCRIPT_TEXT.index("if ((-not $taskWasRegistered) -and (Test-TaskRegistered)) { $done += 'task' }") < switch
    assert SCRIPT_TEXT.index("if ((-not $taskFileWasThere) -and (Test-Path -LiteralPath $taskFile)) { $done += 'taskfile' }") < switch
    assert "$taskWasRegistered = Test-TaskRegistered" in SCRIPT_TEXT
    assert "$taskFileWasThere = Test-Path -LiteralPath $taskFile" in SCRIPT_TEXT

    # 4. Undo-Install deletes the task and its file only when this run is
    #    recorded as having added them; the -Uninstall path, which did nothing
    #    this run, builds its own list from what is on the machine.
    assert "$done += 'task'" not in undo
    assert "$Steps -contains 'task'" in undo


def test_wait_engine_stopped_looks_for_a_process_not_a_file() -> None:
    # Every venv on disk ships Scripts\pythonw.exe, so searching the tool
    # directory for that file reported "still running" forever: each undo waited
    # out its full 15 s and then named the engine as something the user had to
    # finish by hand. What is running is a process, matched on its image path.
    wait = _ps1_function("Wait-EngineStopped")
    assert "Get-Process -Name 'pythonw'" in wait
    assert "$_.Path.StartsWith($ToolDirectory" in wait
    assert "Get-ChildItem" not in wait
    assert "pythonw.exe" not in wait


def test_undo_reports_a_file_it_could_not_delete() -> None:
    # Remove-Item -ErrorAction SilentlyContinue inside try/catch never throws, so
    # a locked receipt, state file or task XML was silently skipped and the run
    # printed "uninstalled" with the file still there. -ErrorAction Stop makes the
    # catch reachable; Test-Path first keeps an already-absent file out of the
    # failure list.
    undo = _undo_source()
    removals = [
        line.strip()
        for line in undo.splitlines()
        if line.strip().startswith("Remove-Item")
    ]
    assert len(removals) == 3
    assert all("-ErrorAction Stop" in line for line in removals), (
        "a Remove-Item that cannot fail is a deletion that is never reported"
    )
    for path in ("$taskFile", "$StateFile", "$ReceiptPath"):
        assert f"if (Test-Path -LiteralPath {path}) {{" in undo, (
            f"Undo-Install removes {path} without checking it is there first"
        )
    # Nothing else in the undo swallows an error either: every other step in it
    # is a command whose exit code is tested, so a SilentlyContinue anywhere else
    # is the same silent skip.
    code = "\n".join(
        line for line in undo.splitlines() if not line.strip().startswith("#")
    )
    assert "SilentlyContinue" not in code


def test_rollback_ordering() -> None:
    undo = _undo_source()
    # Windows will not delete a running executable, so the engine has to be
    # stopped and proven gone before the tool is removed; the task goes before
    # the tool, because its RestartOnFailure (every minute, 999 times) would
    # start a dying engine again.
    assert undo.index("/End") < undo.index("/Delete")
    assert undo.index("/Delete") < undo.index("tool', 'uninstall', 'ciaobot")
    assert undo.index("tool', 'uninstall', 'ciaobot") < undo.index("$ReceiptPath")

    # Decision 4's table: the state file goes, then the PATH entry, then the
    # receipt, and the tool last of all.
    assert undo.index("$StateFile") < undo.index("Remove-UserPathEntry")
    assert undo.index("Remove-UserPathEntry") < undo.index("$ReceiptPath")

    # And the install body's own order: tool, receipt, PATH, state, setup,
    # start, health wait, URL.
    markers = re.findall(r"^\s*# ADDS: (\w+)\s*$", SCRIPT_TEXT, re.MULTILINE)
    assert markers.index("tool") < markers.index("receipt") < markers.index("path")
    assert markers.index("path") < markers.index("state") < markers.index("task")
    assert SCRIPT_TEXT.index("$setupArgs = @(") < SCRIPT_TEXT.index("service', 'start'")
    assert SCRIPT_TEXT.index("service', 'start'") < SCRIPT_TEXT.index("$HealthAttempts; $attempt++")
    assert SCRIPT_TEXT.index("$HealthAttempts; $attempt++") < SCRIPT_TEXT.index("setup-url")

    # The existing-install preflight reads the machine before anything is
    # downloaded; the reinstall it guards comes after.
    assert SCRIPT_TEXT.index("Test-ToolInstalled $uv") < SCRIPT_TEXT.index("tool install")

    # #857's spike: uv hard-links every .pyd/.dll from its cache on Windows, and
    # a cached file another venv shares can be locked by a process that has
    # nothing to do with Ciaobot. A locked file cannot be deleted, so uninstall
    # and rollback would fail for as long as that unrelated process runs - which
    # is why the environment is installed with its own copies.
    assert "@('tool', 'install', '--force', '--link-mode', 'copy', '--python'" in SCRIPT_TEXT

    # The rollback runs only once something has been changed, and the flag that
    # says so is set before the first mutation rather than after it.
    assert "$mutated = $true" in SCRIPT_TEXT
    assert SCRIPT_TEXT.index("$mutated = $true") < SCRIPT_TEXT.index("tool install")
    # The `catch` that carries the rollback is the one closing the install
    # `try`, not one of the many per-step handlers above it: it is the last
    # `} catch {` in the file, and the only one that names `$mutated`.
    catch = SCRIPT_TEXT.split("} catch {")[-1]
    assert "if ($mutated) {" in catch
    assert "Undo-Install -Steps $done" in catch
    assert "throw" in catch, "the rollback must rethrow, or the failure is a silent success"

    # Undo-Install never aborts midway: a rollback that stops at the first
    # refusal leaves more behind than it has to.
    body = "\n".join(
        line for line in undo.splitlines()[1:] if not line.strip().startswith("#")
    )
    assert "throw" not in body
    assert body.count("try {") == body.count("} catch {")


def _ps1_code_only(text: str) -> str:
    """install.ps1 with comments and string literals removed.

    PowerShell has no cheap way to lex this from outside, so the two things that
    hold a `?` legitimately - a comment and a quoted string - are cut with a
    regex that understands PowerShell's own quoting rather than guessed at. What
    is left is the only text a 5.1 parser would read as code.
    """
    stripped = re.sub(r"(?m)#.*$", "", text)
    stripped = re.sub(r"'[^'\n]*'", "''", stripped)
    return re.sub(r'"[^"\n]*"', '""', stripped)


def test_install_ps1_is_ps51_safe() -> None:
    code = _ps1_code_only(SCRIPT_TEXT.replace(_ps1_verifier_source(), ""))
    for banned, why in (
        ("&&", "the && operator is 7.0+"),
        ("||", "the || operator is 7.0+"),
        ("?", "the ternary, ?. and ?? operators are all 7.0+"),
    ):
        assert banned not in code, f"install.ps1 uses {banned}: {why}"

    # `ciao setup` prints warnings on stderr, and with
    # $ErrorActionPreference='Stop' a native command whose stderr is redirected
    # becomes a terminating error: the call would abort the install over a
    # warning install-engine.sh deliberately lets through. It goes through
    # Invoke-Native instead, which scopes the preference and tests the exit code.
    setup_line = next(
        line for line in SCRIPT_TEXT.splitlines() if "Invoke-Native $ciao $setupArgs" in line
    )
    assert "2>&1" not in setup_line
    assert "$setup = Invoke-Native $ciao $setupArgs" in SCRIPT_TEXT
    assert "default { Fail 'ciao setup failed' }" in SCRIPT_TEXT

    SCRIPT.read_bytes().decode("ascii")
    assert not re.search(r"^\s*exit\b", SCRIPT_TEXT, re.MULTILINE)

def test_uninstall_keeps_the_bin_dir_on_path_while_anything_is_still_in_it() -> None:
    # #904: the PATH entry is uv's tool bin dir. When this installer installed
    # uv there, uv.exe stays after -Uninstall, and so do the user's other uv
    # tools; the entry is removed only once the directory is empty, which means
    # after the tool (and its launchers) is gone.
    undo = _undo_source()
    assert undo.index("tool', 'uninstall', 'ciaobot") < undo.index("Remove-UserPathEntry")
    assert "Get-ChildItem -LiteralPath $PathEntry -Force -ErrorAction Stop" in undo
    assert "if ($left.Count -gt 0) {" in undo
    assert "Kept $PathEntry on your PATH: it still holds $names." in undo


def test_install_ps1_does_not_wait_for_an_engine_that_cannot_start_yet() -> None:
    # #903: the logon task is InteractiveToken, so with no Windows session for
    # this account (SSH, runas, never signed in) nothing can start until the next
    # sign-in. The health wait is skipped and the user is told when it starts.
    assert "$signedIn = Test-UserSignedIn" in SCRIPT_TEXT
    assert "$signedIn -and $attempt -le $HealthAttempts" in SCRIPT_TEXT
    assert "so the engine starts the next time this account signs in." in SCRIPT_TEXT
    # Asked of the session list, not of localized `query user` output.
    assert "WTSEnumerateSessionsW" in SCRIPT_TEXT
    assert "query user" not in _ps1_code_only(SCRIPT_TEXT) and "quser" not in _ps1_code_only(SCRIPT_TEXT)
    assert SCRIPT_TEXT.index("$signedIn = Test-UserSignedIn") < SCRIPT_TEXT.index("$HealthAttempts; $attempt++")


@pytest.mark.skipif(sys.platform != "win32", reason="needs wtsapi32 and Windows PowerShell 5.1")
def test_session_check_compiles_under_ps51_and_answers_false_for_an_absent_account() -> None:
    start = SCRIPT_TEXT.index("    function Test-UserSignedIn {")
    end = SCRIPT_TEXT.index("    function Test-EngineAnswering")
    probe = (
        SCRIPT_TEXT[start:end]
        + "\nTest-UserSignedIn | Out-Null\n"
        + "[CiaobotSessions]::SignedIn('NO-SUCH-DOMAIN', 'no-such-ciaobot-user')\n"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", probe],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "False"


def test_printed_commands_go_through_format_command() -> None:
    # "Run this later" lines are pasted back by the user; a bare `$ciao ...`
    # under a "C:\Users\First Last" profile ran `C:\Users\First`.
    printed = [
        line for line in SCRIPT_TEXT.splitlines()
        if line.strip().startswith(("Write-Host", "Fail ")) and "$ciao " in line
    ]
    assert printed
    assert all("$(Format-Command $ciao @(" in line for line in printed), printed


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows PowerShell 5.1")
def test_format_command_parses_back_to_the_same_arguments() -> None:
    program = r"C:\Users\First Last\.local\bin\ciao.exe"
    arguments = ["setup", "--workspace", r"C:\Users\Jane O'Neil\Ciaobot", "--load-launchd"]
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in arguments)
    probe = (
        _ps1_function("Format-Command")
        + f"\n$line = Format-Command '{program}' @({quoted})\n"
        + "$ast = [System.Management.Automation.Language.Parser]::ParseInput($line, [ref]$null, [ref]$null)\n"
        + "$command = $ast.Find({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)\n"
        + "$command.CommandElements | ForEach-Object { $_.Value }\n"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", probe],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [program, *arguments]
