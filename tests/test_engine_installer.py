"""Tests for the uv-based one-line engine installer (#568).

The installer is the one path that puts bytes on a user's machine straight from
a pipe, so most of what is asserted here is *ordering*: the signed manifest and
the wheel digest are checked before `uv tool install` runs, and the two things
it must never take over (a Ciaobot.app engine, someone else's `ciao`) are
refused before that same call.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao.release_manifest import RELEASE_PUBLIC_KEY, build_manifest
from tests.test_release_manifest import _keypair, _sign

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-engine.sh"
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")

VERSION = "1.2.3"
WHEEL_NAME = f"ciaobot-{VERSION}-py3-none-any.whl"
WHEEL_BYTES = b"the signed wheel"

# The end-to-end runs resolve their downloads with the real curl, so a machine
# without it (or without shasum) has nothing to run them against.
needs_local_tools = pytest.mark.skipif(
    shutil.which("curl") is None or shutil.which("shasum") is None,
    reason="needs curl (for file:// downloads) and shasum",
)


def _function_source(name: str) -> str:
    """Extract one top-level shell function, closing brace included."""
    lines = SCRIPT_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(i for i, line in enumerate(lines[start:], start) if line == "}")
    return "\n".join(lines[start : end + 1])


def _verifier_source() -> str:
    """The Python program the script pipes into `python -` inside read_manifest.

    The heredoc *is* the verifier, so the tests below exercise the shipped text
    instead of a copy of it that could drift away silently.
    """
    lines = SCRIPT_TEXT.splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'PY'" in line) + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "PY")
    assert "--- embedded verifier" in lines[start], "the verifier marker moved"
    return "\n".join(lines[start:end]) + "\n"


def _manifest_bytes(filename: str = WHEEL_NAME, payload: bytes = WHEEL_BYTES) -> bytes:
    """A schema-1 manifest naming one wheel, with the digest of `payload`."""
    return json.dumps(
        build_manifest(
            VERSION,
            [
                {
                    "filename": filename,
                    "kind": "wheel",
                    "platform": "any",
                    "arch": "any",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            ],
            created="2026-09-25T10:00:00+00:00",
        )
    ).encode()


def _run_verifier(
    tmp_path: Path, manifest_bytes: bytes, sig_text: str, version: str, key_text: str
) -> subprocess.CompletedProcess[str]:
    manifest = tmp_path / "ciaobot-engine-manifest.json"
    signature = tmp_path / "ciaobot-engine-manifest.json.sig"
    manifest.write_bytes(manifest_bytes)
    signature.write_text(sig_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-", str(manifest), str(signature), version, key_text],
        input=_verifier_source(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_engine_installer_is_posix_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_embedded_key_matches_release_manifest() -> None:
    literal = re.search(r'^RELEASE_PUBLIC_KEY="(.*?)"$', SCRIPT_TEXT, re.MULTILINE)
    assert literal, "the script no longer embeds RELEASE_PUBLIC_KEY"

    assert literal.group(1) == RELEASE_PUBLIC_KEY


def test_verification_happens_before_install() -> None:
    # Handing a wheel to a tool environment and only then discovering it is not
    # the one the release signed would be no check at all, so both the signature
    # verification and the digest comparison have to come first.
    install = SCRIPT_TEXT.index("tool install --force")

    assert SCRIPT_TEXT.index("$(read_manifest)") < install
    assert SCRIPT_TEXT.index("shasum -a 256") < install


def test_setup_reloads_launch_agent_before_start() -> None:
    # `ciao setup` rewrites the plist but does not reload it, so launchd keeps
    # serving the job definition it already holds - the previous engine's, for a
    # source checkout or a pip install. `service start` then kickstarts that
    # one, and the engine this script just installed never runs. Reloading the
    # agent is what makes the takeover actually take, so it has to be requested
    # from setup and only when the script is also going to start the service.
    line = next(l for l in SCRIPT_TEXT.splitlines() if "--load-launchd" in l)

    assert "no_start" in line
    assert SCRIPT_TEXT.index("--load-launchd") < SCRIPT_TEXT.index("service start --workspace")


def test_refuses_desktop_engine_and_foreign_ciao() -> None:
    refuse_desktop = _function_source("refuse_desktop_engine")

    assert ".app/" in refuse_desktop
    # The refusal has to name the way out, or the user is stuck on it: the
    # migration is what replaced "not supported yet (#576)".
    assert "re-run with --migrate" in refuse_desktop
    # A `ciao` this installer did not write stays: the desktop shim (its marker
    # line) and an existing uv tool entry point are the only ones replaced.
    assert "# Ciaobot shim (managed by the Ciaobot installer)" in SCRIPT_TEXT
    assert "was not installed by Ciaobot" in SCRIPT_TEXT


def test_embedded_verifier_accepts_signed_manifest_and_prints_wheel(
    tmp_path: Path,
) -> None:
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()

    result = _run_verifier(tmp_path, raw, _sign(raw, private_key, key_id), VERSION, public_key)

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        WHEEL_NAME,
        hashlib.sha256(WHEEL_BYTES).hexdigest(),
        str(len(WHEEL_BYTES)),
    ]


def test_embedded_verifier_rejects_tampered_manifest(tmp_path: Path) -> None:
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()
    signature = _sign(raw, private_key, key_id)

    result = _run_verifier(
        tmp_path, raw.replace(b'"1.2.3"', b'"9.9.9"'), signature, VERSION, public_key
    )

    assert result.returncode == 1
    assert "does not match" in result.stderr


def test_embedded_verifier_rejects_wrong_version(tmp_path: Path) -> None:
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()

    result = _run_verifier(tmp_path, raw, _sign(raw, private_key, key_id), "2.0.0", public_key)

    assert result.returncode == 1
    assert "2.0.0" in result.stderr


def test_embedded_verifier_rejects_path_in_filename(tmp_path: Path) -> None:
    # The filename is the only untrusted string that reaches a shell word, so a
    # manifest must not be able to point the download at a directory.
    raw = _manifest_bytes(filename="../evil.whl")
    private_key, public_key, key_id = _keypair()

    result = _run_verifier(tmp_path, raw, _sign(raw, private_key, key_id), VERSION, public_key)

    assert result.returncode == 1
    assert "malformed" in result.stderr


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


# A uv stub. `run` hands the embedded verifier and the embedded receipt
# writer/reader to the real interpreter, `tool dir` answers, `tool list` is empty
# (nothing installed yet), and `tool install` fabricates a tool environment whose
# interpreter and `ciao` are scripts that only record how they were called.
# Nothing here installs anything.
_UV_STUB = """#!/bin/sh
set -eu
# Which embedded call this is. The two receipt programs are told apart by a
# marker in their own source, the same way the embedded verifier is, so the
# stub needs no knowledge of the arguments beyond those.
case "$*" in
    *migration-receipt-write*)
        # `fail-receipt-write-at N` fails the Nth write of the migration
        # receipt, which is how a failure is injected at the one that records
        # the start and at the one that settles the migration without the stub
        # having to know which is which.
        if [ -f "$HOME/fail-receipt-write-at" ]; then
            count=$(cat "$HOME/receipt-write-count" 2>/dev/null || echo 0)
            count=$((count + 1))
            echo "$count" > "$HOME/receipt-write-count"
            if [ "$count" -ge "$(cat "$HOME/fail-receipt-write-at")" ]; then
                printf 'receipt-write-failed\\n' >> "$HOME/trace.log"
                exit 1
            fi
        fi
        ;;
    *migration-receipt-read*)
        if [ -f "$HOME/fail-receipt-read" ]; then
            printf 'receipt-read-failed\\n' >> "$HOME/trace.log"
            exit 1
        fi
        ;;
esac
case "${1:-}" in
    run)
        # `run --quiet --no-project --python 3.13 --with <pin> python - args`
        shift
        while [ "$#" -gt 0 ]; do
            if [ "$1" = "python" ]; then
                shift
                break
            fi
            shift
        done
        # `--migrate` classifies through the wheel with `python -I -m ciao....
        # The wheel the installer hands over is a fake file, so the module is
        # resolved out of this checkout instead - and `-I` drops PYTHONPATH, so
        # it goes with it.
        printf '%s\\n' "$*" >> "$HOME/uv-run-calls.log"
        if [ "${1:-}" = "-c" ]; then
            case "${2:-}" in
                *migration-receipt-write*)
                    printf 'receipt-write %s\\n' "${3:-}" >> "$HOME/trace.log"
                    ;;
            esac
        fi
        if [ "${1:-}" = "-I" ]; then shift; fi
        PYTHONPATH="__REPO_ROOT__" exec "__PYTHON__" "$@"
        ;;
    tool)
        case "${2:-}" in
            install) printf '%s\\n' "$*" >> "$HOME/uv-calls.log" ;;
        esac
        shift
        case "${1:-}" in
            dir)
                shift
                if [ "${1:-}" = "--bin" ]; then
                    echo "$HOME/.local/bin"
                else
                    echo "$HOME/.local/share/uv/tools"
                fi
                ;;
            list)
                # A real `uv tool list` is indented output under a heading, and
                # the collision check reads that to tell our own entry point
                # from somebody else's program. A retry therefore sees the tool
                # its previous run installed, exactly as a second real run would.
                if [ -d "$HOME/.local/share/uv/tools/ciaobot" ]; then
                    echo "Installed tools:"
                    echo "- ciaobot v__VERSION__"
                    echo "    - ciaobot"
                fi
                ;;
            install)
                if [ -f "$HOME/fail-uv-tool-install" ]; then
                    printf 'uv-tool-install-failed\\n' >> "$HOME/trace.log"
                    exit 1
                fi
                tools="$HOME/.local/share/uv/tools"
                mkdir -p "$tools/ciaobot/bin" "$HOME/.local/bin"
                cat > "$tools/ciaobot/bin/python" <<EOF
#!/bin/sh
# The install receipt is written by this interpreter, so the knob that breaks it
# has to be honoured here: a full disk or a read-only state directory fails the
# same way, after the tool is already installed.
case "\\$*" in
    *install_receipt*) [ -f "\\$HOME/fail-install-receipt" ] && exit 1 ;;
esac
PYTHONPATH="__REPO_ROOT__" exec "__PYTHON__" "\\$@"
EOF
                chmod 755 "$tools/ciaobot/bin/python"
                cat > "$HOME/.local/bin/ciao" <<'EOF'
#!/bin/sh
printf 'ciao %s\n' "$*" >> "$HOME/trace.log"
printf '%s\n' "$*" >> "$HOME/ciao-calls.log"
case "${1:-}" in
    setup)
        # An interrupted install, the way a user hits Ctrl-C or the laptop lid
        # closes: the process goes away in the middle of the migration, with
        # the tool already installed and nothing settled.
        if [ -f "$HOME/interrupt-at-setup" ]; then
            rm -f "$HOME/interrupt-at-setup"
            kill -TERM "$PPID"
            exit 143
        fi
        if [ -f "$HOME/fail-ciao-setup" ]; then exit 1; fi
        ;;
    service)
        if [ -f "$HOME/fail-ciao-service-start" ]; then exit 1; fi
        ;;
    setup-url)
        echo "http://localhost:8443/?setup=tok"
        ;;
esac
exit 0
EOF
                chmod 755 "$HOME/.local/bin/ciao"
                ;;
        esac
        ;;
esac
"""

def _fake_uv() -> str:
    return (
        _UV_STUB.replace("__REPO_ROOT__", str(REPO_ROOT))
        .replace("__PYTHON__", sys.executable)
        .replace("__VERSION__", VERSION)
    )


# The engine's own answers come from a fake `curl`: the health poll has to be
# able to report a version that matches, or one that does not, without a real
# engine being started on the Mac running the tests. Every other URL is passed
# straight through to the real curl, so the file:// release downloads are still
# the real thing.
_CURL_STUB = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$HOME/trace.log"
for arg in "$@"; do
    case "$arg" in
        */api/startup-status)
            version=$(cat "$HOME/startup-version" 2>/dev/null || echo "__VERSION__")
            # Shaped like the real response, which has keys before `version`.
            printf '{"phases": [], "overall_ready": true, "version": "%s", "node_role": "host"}\\n' "$version"
            exit 0
            ;;
    esac
done
exec "__CURL__" "$@"
"""


def _harness(tmp_path: Path) -> dict[str, Any]:
    """A signed release on `file://`, fake tools, a fake `$HOME`, and the script
    itself with the embedded key swapped for the test key."""
    home = (tmp_path / "home").resolve()
    (home / ".local" / "bin").mkdir(parents=True)
    (home / "Ciaobot").mkdir(parents=True)

    release = tmp_path / "rel" / f"v{VERSION}"
    release.mkdir(parents=True)
    raw = _manifest_bytes()
    private_key, public_key, key_id = _keypair()
    (release / "ciaobot-engine-manifest.json").write_bytes(raw)
    (release / "ciaobot-engine-manifest.json.sig").write_text(
        _sign(raw, private_key, key_id), encoding="utf-8"
    )
    (release / WHEEL_NAME).write_bytes(WHEEL_BYTES)

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _write_exec(fakebin / "uname", "#!/bin/sh\necho Darwin\n")
    _write_exec(fakebin / "sw_vers", '#!/bin/sh\n[ "$1" = "-productVersion" ] && echo 14.5\n')
    _write_exec(fakebin / "uv", _fake_uv())
    _write_exec(
        fakebin / "curl", _CURL_STUB.replace("__CURL__", shutil.which("curl") or "curl")
        .replace("__VERSION__", VERSION)
    )
    # launchd records what it was asked to do and always succeeds unless a test
    # asks it to refuse one call. The app and `pgrep` are not there, which is the
    # state of a Mac with no Ciaobot.app. `sleep` never sleeps, so a poll that
    # has to time out does so at once instead of holding the test open.
    _write_exec(
        fakebin / "launchctl",
        '#!/bin/sh\n'
        'printf \'launchctl %s\\n\' "$*" >> "$HOME/trace.log"\n'
        'printf \'%s\\n\' "$*" >> "$HOME/launchctl.log"\n'
        '# A knob per subcommand, optionally narrowed to one label, so a test\n'
        '# can make launchctl refuse exactly the call it is about.\n'
        'label=${2##*/}\n'
        'if [ -f "$HOME/fail-launchctl-$1-$label" ] || [ -f "$HOME/fail-launchctl-$1" ]; then\n'
        '    printf \'launchctl-failed %s\\n\' "$*" >> "$HOME/trace.log"\n'
        '    exit 1\n'
        'fi\n'
        'exit 0\n',
    )
    _write_exec(fakebin / "osascript", "#!/bin/sh\nexit 1\n")
    _write_exec(
        fakebin / "pgrep",
        '#!/bin/sh\n'
        'printf \'pgrep %s\\n\' "$*" >> "$HOME/trace.log"\n'
        'if [ -f "$HOME/desktop-running" ]; then exit 0; fi\n'
        'exit 1\n',
    )
    _write_exec(fakebin / "sleep", "#!/bin/sh\nexit 0\n")

    # The tools a migration reaches for are script variables, so the test
    # rewrites the lines that set them instead of setting environment
    # variables: a run here must never touch this Mac's real launchd, the
    # running Ciaobot.app, a real `pgrep`, a real `sleep`, or whatever happens to
    # be answering on localhost here.
    rewritten = re.sub(
        r'^RELEASE_PUBLIC_KEY=".*?"$',
        f'RELEASE_PUBLIC_KEY="{public_key}"',
        SCRIPT_TEXT,
        count=1,
        flags=re.MULTILINE,
    )
    rewritten = re.sub(
        r"^(PLISTBUDDY|LAUNCHCTL|OSASCRIPT|PGREP|CURL|SLEEP)=.*$",
        lambda m: f"{m.group(1)}={fakebin / m.group(1).lower()}",
        rewritten,
        flags=re.MULTILINE,
    )
    script = tmp_path / "install-engine.sh"
    script.write_text(rewritten, encoding="utf-8")
    script.chmod(0o755)

    return {
        "home": home,
        "script": script,
        "release": release,
        "fakebin": fakebin,
        "env": {
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "CIAO_RELEASE_BASE_URL": f"file://{tmp_path / 'rel'}",
            "TMPDIR": str(tmp_path),
        },
    }


def _run_installer(
    harness: dict[str, Any],
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(harness["script"]), *args],
        env={**os.environ, **harness["env"], **(env or {})},
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _log(harness: dict[str, Any], name: str) -> str:
    path: Path = harness["home"] / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


@needs_local_tools
def test_engine_installer_end_to_end_with_fakes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    home: Path = harness["home"]

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    install_line = next(
        line for line in _log(harness, "uv-calls.log").splitlines() if "tool install" in line
    )
    assert install_line.startswith("tool install --force --python 3.13 ")
    # The verified wheel, downloaded into the installer's own private temp dir.
    wheel_arg = install_line.rsplit(" ", 1)[-1]
    assert wheel_arg.endswith(f"/{WHEEL_NAME}")
    assert wheel_arg.startswith(f"{tmp_path}/ciaobot-engine.")
    assert str(harness["release"]) not in install_line

    ciao = home / ".local" / "bin" / "ciao"
    calls = _log(harness, "ciao-calls.log")
    # The trailing newline is the assertion: `--yes` would end the line, and
    # passing it unconditionally is what turns off setup's guards.
    assert f"setup --workspace {home / 'Ciaobot'} --python {ciao}\n" in calls
    assert "service start" not in calls

    receipt = json.loads(
        (home / ".local" / "state" / "ciaobot" / "install-receipt.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["version"] == VERSION
    assert receipt["executable"] == str(ciao)
    assert receipt["python"] == str(
        home / ".local" / "share" / "uv" / "tools" / "ciaobot" / "bin" / "python"
    )
    assert receipt["service_backend"] == "launchd"
    assert receipt["service_label"] == "com.ciao.server"

    assert f"Ciaobot engine {VERSION} installed." in result.stdout


@needs_local_tools
def test_engine_installer_rejects_tampered_wheel(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    (harness["release"] / WHEEL_NAME).write_bytes(b"a wheel nobody signed")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "does not match the signed manifest" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
def test_engine_installer_refuses_foreign_ciao(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    foreign: Path = harness["home"] / ".local" / "bin" / "ciao"
    _write_exec(foreign, "#!/bin/sh\necho other\n")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "was not installed by Ciaobot" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho other\n"


def _write_desktop_shim(home: Path, target: Path) -> Path:
    """The shim scripts/install.sh writes, pointing at `target`.

    The desktop installer writes it *before* onboarding creates the server
    plist, so the shim is the only evidence a Ciaobot.app owns the engine
    during that window.
    """
    shim = home / ".local" / "bin" / "ciao"
    shim.write_text(
        "#!/bin/sh\n"
        "# Ciaobot shim (managed by the Ciaobot installer)\n"
        f'exec "{target}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


@needs_local_tools
def test_engine_installer_refuses_live_desktop_shim(tmp_path: Path) -> None:
    # The plist is absent (onboarding has not run yet) but the app is live, so
    # refusing here is the difference between a refused install and an engine
    # taken over from a running Ciaobot.app.
    harness = _harness(tmp_path)
    app_engine = (
        tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    )
    app_engine.parent.mkdir(parents=True)
    _write_exec(app_engine, "#!/bin/sh\nexit 0\n")
    _write_desktop_shim(harness["home"], app_engine)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "#576" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
def test_engine_installer_replaces_stale_desktop_shim(tmp_path: Path) -> None:
    # The app is gone: nothing owns the engine any more, so the stale shim is
    # ours to replace and the install goes on.
    harness = _harness(tmp_path)
    _write_desktop_shim(
        harness["home"],
        tmp_path / "Applications" / "Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao",
    )

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "tool install" in _log(harness, "uv-calls.log")


@needs_local_tools
def test_engine_installer_rejects_cdpath_surprise(tmp_path: Path) -> None:
    # `cd` consults CDPATH for a bare relative name and prints the path it
    # found, so an inherited CDPATH decides where the workspace really is (or
    # makes the install fail after the wheel is already installed). The decoy
    # `wsrel` under the CDPATH entry is the case that must not win.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    (tmp_path / "wsrel").mkdir()

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--workspace",
        "wsrel",
        "--no-start",
        cwd=home,
        env={"CDPATH": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    ciao = home / ".local" / "bin" / "ciao"
    calls = _log(harness, "ciao-calls.log")
    assert f"setup --workspace {home / 'wsrel'} --python {ciao}\n" in calls


def _run_refuse_desktop_engine(
    tmp_path: Path, program: Path
) -> subprocess.CompletedProcess[str]:
    """Run refuse_desktop_engine against a plist whose ProgramArguments:0 is
    `program`.

    PLISTBUDDY is a script variable, not an environment variable, so the
    function is run with it pointed at a stub printing that path.
    """
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "com.ciao.server.plist").write_text(
        "<plist/>", encoding="utf-8"
    )
    plistbuddy = tmp_path / "PlistBuddy"
    _write_exec(plistbuddy, f"#!/bin/sh\necho {program}\n")
    body = "\n".join(
        [
            f"PLISTBUDDY={plistbuddy}",
            "SERVER_LABEL=com.ciao.server",
            "migrate=0",
            _function_source("fail"),
            _function_source("refuse_desktop_engine"),
            "refuse_desktop_engine",
        ]
    )
    return subprocess.run(
        ["sh", "-c", body],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_engine_installer_refuses_app_managed_engine(tmp_path: Path) -> None:
    # The bundle is built under tmp_path, so the refusal means the same thing
    # on a Mac with Ciaobot.app installed and on one without.
    app_engine = tmp_path / "Ciaobot.app" / "Contents" / "MacOS" / "ciao"
    app_engine.parent.mkdir(parents=True)
    _write_exec(app_engine, "#!/bin/sh\nexit 0\n")

    result = _run_refuse_desktop_engine(tmp_path, app_engine)

    assert result.returncode == 1
    assert "#576" in result.stderr


def test_engine_installer_ignores_plist_of_deleted_app(tmp_path: Path) -> None:
    # Ciaobot.app was moved to the Trash and left its plist behind. Refusing
    # here would be a dead end: the advice in the message
    # (`ciao desktop uninstall`) runs the shim, whose target is gone too, so
    # the user could neither install nor uninstall.
    result = _run_refuse_desktop_engine(
        tmp_path,
        tmp_path / "Applications" / "Ciaobot.app" / "Contents" / "MacOS" / "ciao",
    )

    assert result.returncode == 0
    assert result.stderr == ""


# --- the Ciaobot.app → terminal-engine migration (#576) -------------------
#
# The classifier these tests run against is the real one, out of this checkout,
# over a fake `$HOME`: a real `--migrate` on this Mac would migrate the machine
# running the tests.


def _desktop_install(
    harness: dict[str, Any], tmp_path: Path, node_state: object = None
) -> Path:
    """Put a live Ciaobot.app engine on the fake Mac and return its workspace.

    `node_state` is what the runtime root holds: None (nothing) is the host
    case, a dict is a node role, and a string is written verbatim so an
    unreadable state can be reproduced.
    """
    home: Path = harness["home"]
    app_engine = (
        tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    )
    app_engine.parent.mkdir(parents=True)
    _write_exec(app_engine, "#!/bin/sh\nexit 0\n")
    _write_desktop_shim(home, app_engine)

    workspace = home / "Ciaobot"
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    if node_state is not None:
        text = node_state if isinstance(node_state, str) else json.dumps(node_state)
        (workspace / ".runtime" / "node_state.json").write_text(text, encoding="utf-8")

    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in ("com.ciao.server", "Ciaobot"):
        (agents / f"{label}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": [str(app_engine), "run"],
                    "EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)},
                    "WorkingDirectory": str(workspace),
                }
            )
        )
    return workspace


def _migration_receipt(harness: dict[str, Any]) -> dict[str, Any]:
    path: Path = harness["home"] / ".local/state/ciaobot/migration/receipt.json"
    return json.loads(path.read_text(encoding="utf-8"))


def migration_before(harness: dict[str, Any]) -> Path:
    return harness["home"] / ".local/state/ciaobot/migration/before"


def _install_receipt(harness: dict[str, Any]) -> dict[str, Any]:
    path: Path = harness["home"] / ".local/state/ciaobot/install-receipt.json"
    return json.loads(path.read_text(encoding="utf-8"))


@needs_local_tools
def test_migrate_host_repoints_and_retires_app_agent(tmp_path: Path) -> None:
    # --no-start because the health poll needs a real engine to answer; what is
    # under test here is everything up to the point where the app's own agent
    # would be retired, which is the ordering the whole migration turns on.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert result.returncode == 0, result.stderr
    before = home / ".local/state/ciaobot/migration/before"
    for name in ("com.ciao.server.plist", "Ciaobot.plist", "ciao"):
        assert (before / name).exists(), f"{name} was not backed up"
    # The before-image is a copy of what was there, not a rewrite of it.
    assert (before / "com.ciao.server.plist").read_bytes() == (
        home / "Library/LaunchAgents/com.ciao.server.plist"
    ).read_bytes()

    ciao = home / ".local" / "bin" / "ciao"
    calls = _log(harness, "ciao-calls.log")
    # The workspace the app's engine was already running in, and --yes for the
    # same reason the auto-detection passes it. No --load-launchd: nothing is
    # started, so there is no job definition to reload.
    assert f"setup --workspace {workspace} --python {ciao} --yes\n" in calls
    assert "--load-launchd" not in calls

    assert _migration_receipt(harness)["phase"] == "installed_no_start"
    # Nothing was retired, so nothing may have been booted out either.
    assert "com.ciao.server" not in _log(harness, "launchctl.log")


@needs_local_tools
def test_migrate_client_disables_local_engine_and_never_sets_up(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    log = _log(harness, "launchctl.log")
    for label in ("com.ciao.server", "Ciaobot"):
        assert f"bootout gui/{os.getuid()}/{label}" in log, label
        assert f"disable gui/{os.getuid()}/{label}" in log, label
    # The Mac it is migrating to must not end up running an engine of its own.
    calls = _log(harness, "ciao-calls.log")
    assert "setup" not in calls
    assert "service start" not in calls
    assert _install_receipt(harness)["service_backend"] == "none"
    assert _migration_receipt(harness)["phase"] == "migrated_client"
    assert "https://mini.ts.net" in result.stdout


@needs_local_tools
def test_migrate_invalid_requires_explicit_choice(tmp_path: Path) -> None:
    # A node state nobody can read is not a licence to guess: guessing "host"
    # would put a second writer on a runtime root that may already have one.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path, "{not json at all")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "--as-host" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
def test_migrate_invalid_as_client_uses_given_url(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path, "{not json at all")

    result = _run_installer(
        harness, "--version", VERSION, "--migrate", "--as-client", "https://h.example"
    )

    assert result.returncode == 0, result.stderr
    assert "https://h.example" in result.stdout
    # The override is the decision, so it is what the receipt records - not a
    # host_url that was in a state file this script could not read.
    receipt = _migration_receipt(harness)
    assert receipt["kind"] == "desktop_invalid"
    assert receipt["host_url"] == "https://h.example"
    assert receipt["phase"] == "migrated_client"
    assert "setup" not in _log(harness, "ciao-calls.log")
    assert f"disable gui/{os.getuid()}/com.ciao.server" in _log(harness, "launchctl.log")


@needs_local_tools
def test_without_migrate_live_desktop_still_refused_with_hint(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "--migrate" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
@pytest.mark.parametrize(
    "break_state",
    [
        # A plist that is there and is not a plist: a service definition whose
        # program nobody can read, which is not "nothing to migrate".
        pytest.param("malformed-plist", id="malformed-plist"),
        # The runtime root the live engine is supposed to own is not there.
        pytest.param("missing-runtime-root", id="missing-runtime-root"),
        # A client whose host_url is a prefix and not an address. Taking the
        # client path here would disable this Mac's engine and then hand the
        # user "https://", which opens nothing.
        pytest.param("empty-host-url", id="empty-host-url"),
    ],
)
def test_migrate_unreadable_state_never_guesses(
    tmp_path: Path, break_state: str
) -> None:
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    if break_state == "malformed-plist":
        # No `.app` program and no marked shim, so nothing but the malformed
        # plist says there is a service here.
        _write_exec(home / ".local/bin/ciao", "#!/bin/sh\nexit 0\n")
        (home / "Library/LaunchAgents").mkdir(parents=True)
        (home / "Library/LaunchAgents/com.ciao.server.plist").write_bytes(
            b"this is not a plist at all\n"
        )
    elif break_state == "missing-runtime-root":
        _desktop_install(harness, tmp_path)
        (home / "Ciaobot" / ".runtime").rmdir()
    else:
        _desktop_install(harness, tmp_path, {"role": "standby", "host_url": "https://"})
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "--as-host" in result.stderr
    # Nothing was decided for the user, and nothing was touched.
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    assert _replaced_state(harness) == untouched


@needs_local_tools
def test_migrate_is_a_noop_after_success(tmp_path: Path) -> None:
    # "Already migrated" is a claim about this Mac, not about a file: it holds
    # only when the receipt is whole, names before-images that are still there,
    # and agrees with what is actually installed. This one is a real completed
    # migration - a settled receipt, the install receipt it recorded, and the
    # app's own plist gone - and it must not touch anything.
    harness = _harness(tmp_path)
    _write_settled_migration(harness, phase="migrated")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert "already migrated (migrated)" in result.stdout
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""


@needs_local_tools
def test_migrate_client_noop_after_success(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _write_settled_migration(harness, phase="migrated_client", backend="none")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert "already migrated (migrated_client)" in result.stdout
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
def test_migrate_verification_still_first(tmp_path: Path) -> None:
    # A migration is the one path that rewrites a working install, so the
    # signed-manifest and digest checks have to come before it - including
    # before the classifier, which runs code out of the wheel.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    (harness["release"] / WHEEL_NAME).write_bytes(b"a wheel nobody signed")

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert result.returncode == 1
    assert "does not match the signed manifest" in result.stderr
    assert "engine_migration" not in _log(harness, "uv-run-calls.log")
    assert not (harness["home"] / ".local/state/ciaobot/migration").exists()
    assert _log(harness, "launchctl.log") == ""


# --- failure injection: nothing a migration breaks may stay broken ----------


def _knob(harness: dict[str, Any], name: str) -> None:
    """Arm one of the fake tools' failure knobs, as a file in the fake `$HOME`."""
    (harness["home"] / name).write_text("1\n", encoding="utf-8")


def _arm(harness: dict[str, Any], knob: str) -> None:
    """Arm the failure a parameter names.

    `collision` is not a stub knob but the state that causes one: a `ciao` in
    the way that this installer did not put there. `receipt-write-N` is the Nth
    write of the migration receipt, which is how a failure lands on the one that
    records the start or on the one that settles the transaction without the
    test having to know which is which.
    """
    if knob == "collision":
        _write_exec(harness["home"] / ".local/bin/ciao", "#!/bin/sh\necho other\n")
    elif knob.startswith("receipt-write-"):
        (harness["home"] / "fail-receipt-write-at").write_text(
            f"{knob.rsplit('-', 1)[1]}\n", encoding="utf-8"
        )
    else:
        _knob(harness, knob)


def _replaced_state(harness: dict[str, Any]) -> dict[str, bytes | None]:
    """Everything a migration is allowed to replace, as it is right now.

    Byte for byte, so a rollback that restores a *different* receipt, a
    rewritten plist or a replacement shim is caught even when the exit status
    is right.
    """
    home: Path = harness["home"]
    names = (
        ".local/bin/ciao",
        "Library/LaunchAgents/com.ciao.server.plist",
        "Library/LaunchAgents/Ciaobot.plist",
        ".local/state/ciaobot/install-receipt.json",
    )
    return {
        name: (home / name).read_bytes() if (home / name).exists() else None
        for name in names
    }


def _tool_env(harness: dict[str, Any]) -> Path:
    return harness["home"] / ".local/share/uv/tools/ciaobot"


def _trace(harness: dict[str, Any]) -> list[str]:
    return _log(harness, "trace.log").splitlines()


def _write_settled_migration(
    harness: dict[str, Any], *, phase: str, backend: str = "launchd"
) -> Path:
    """A migration receipt with everything the no-op gate is meant to check.

    The before-images are copied from the fake Mac as it is, the install receipt
    records the version and the service role the migration settled on, and a
    settled phase means the app's own plist is gone - which is what a Mac that
    was really migrated looks like.
    """
    home: Path = harness["home"]
    migration = home / ".local/state/ciaobot/migration"
    before = migration / "before"
    before.mkdir(parents=True, exist_ok=True)
    images: dict[str, str] = {}
    for name in ("server_plist", "desktop_plist", "shim", "install_receipt", "tool_env"):
        images[name] = ""
    for name, source in (
        ("server_plist", home / "Library/LaunchAgents/com.ciao.server.plist"),
        ("desktop_plist", home / "Library/LaunchAgents/Ciaobot.plist"),
        ("shim", home / ".local/bin/ciao"),
    ):
        if source.exists():
            shutil.copyfile(source, before / f"before-{name}")
            images[name] = str(before / f"before-{name}")
    if phase in ("migrated", "migrated_client"):
        (home / "Library/LaunchAgents/Ciaobot.plist").unlink(missing_ok=True)
        images["desktop_plist"] = ""
    state = home / ".local/state/ciaobot"
    state.mkdir(parents=True, exist_ok=True)
    # Indented and sorted, because the installer's receipt reader picks its
    # fields out with awk line by line, exactly as it does for a real one.
    (state / "install-receipt.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "executable": str(home / ".local/bin/ciao"),
                "python": str(_tool_env(harness) / "bin" / "python"),
                "service_backend": backend,
                "service_label": "com.ciao.server" if backend == "launchd" else "",
                "installed_at": "2026-09-26T10:00:00+00:00",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (migration / "receipt.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "desktop_host",
                "phase": phase,
                "version": VERSION,
                "workspace": str(home / "Ciaobot"),
                "host_url": "",
                "started_at": "2026-09-26T10:00:00Z",
                "before": images,
            }
        ),
        encoding="utf-8",
    )
    return migration


@needs_local_tools
def test_migrate_refuses_running_app_after_timeout(tmp_path: Path) -> None:
    # A Ciaobot.app that is still there 20s after it was asked to quit is a live
    # writer. Carrying on would replace its engine underneath it, so this stops
    # before the tool is installed and before either label is touched.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    original_shim = (harness["home"] / ".local/bin/ciao").read_text(encoding="utf-8")
    _knob(harness, "desktop-running")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "still running" in result.stderr
    assert "nothing on this Mac has been changed" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    # Not one label was booted out or disabled, and the receipt says why rather
    # than claiming a migration that did not happen.
    assert _log(harness, "launchctl.log") == ""
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "started"
    assert "still running" in receipt["error"]
    # The before-images are still the originals, so the retry can use them.
    before = harness["home"] / ".local/state/ciaobot/migration/before"
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim
    # And the wait was really bounded rather than exited from at once: the
    # deadline is the only reason it is safe to go on at all.
    assert sum(1 for line in _trace(harness) if line.startswith("pgrep")) == 21


@needs_local_tools
def test_migrate_preflight_collision_preserves_client_agents(
    tmp_path: Path,
) -> None:
    # A `ciao` this script did not install is refused, and it has to be refused
    # while it can still be a refusal: a client that has already had both labels
    # disabled and its plist deleted is a client told to move a file away while
    # it has no engine and no host.
    harness = _harness(tmp_path)
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )
    foreign: Path = harness["home"] / ".local/bin/ciao"
    _write_exec(foreign, "#!/bin/sh\necho other\n")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "was not installed by Ciaobot" in result.stderr
    assert _log(harness, "launchctl.log") == ""
    assert not (harness["home"] / ".local/state/ciaobot/migration").exists()
    for label in ("com.ciao.server", "Ciaobot"):
        assert (harness["home"] / f"Library/LaunchAgents/{label}.plist").exists()
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\necho other\n"


@needs_local_tools
def test_migrate_retry_preserves_original_before_images(tmp_path: Path) -> None:
    # The before-images are the only way back to the engine that is running now,
    # so they belong to the migration and not to the attempt. A second
    # `--migrate --no-start` runs with the uv entry point already sitting where
    # the desktop shim was: re-taking the snapshot would overwrite the shim with
    # it, and after that no rollback could give Ciaobot.app its engine back.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()

    first = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")
    assert first.returncode == 0, first.stderr
    before = home / ".local/state/ciaobot/migration/before"
    started_at = _migration_receipt(harness)["started_at"]
    assert _migration_receipt(harness)["phase"] == "installed_no_start"

    second = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert second.returncode == 0, second.stderr
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim
    assert (before / "com.ciao.server.plist").read_bytes() == original_plist
    # One transaction, not two: the same start time and the same before-images.
    assert _migration_receipt(harness)["started_at"] == started_at
    assert _migration_receipt(harness)["before"]["shim"] == str(before / "ciao")
    assert _migration_receipt(harness)["phase"] == "installed_no_start"


@needs_local_tools
def test_migrate_interruption_retry_preserves_originals(tmp_path: Path) -> None:
    # An install that is interrupted part-way is a failure with less tidiness:
    # the tool is already installed and nothing is settled. The receipt has to
    # say the transaction never finished, and the retry has to pick it up from
    # the originals rather than snapshot the tool the interrupted run already
    # put in place.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")
    _knob(harness, "interrupt-at-setup")

    interrupted = _run_installer(harness, "--version", VERSION, "--migrate")

    assert interrupted.returncode == 130
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "interrupted"
    assert "interrupted" in receipt["error"]
    # The install it had already done is still there - this is a stopped
    # migration, not a rollback - and the originals are untouched.
    assert "tool install" in _log(harness, "uv-calls.log")
    before = home / ".local/state/ciaobot/migration/before"
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim

    retry = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert retry.returncode == 0, retry.stderr
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim
    assert _migration_receipt(harness)["phase"] == "installed_no_start"


@needs_local_tools
@pytest.mark.parametrize(
    "case,message",
    [
        # A receipt with no before-images at all: nothing to roll back to, and
        # the only thing a substring match ever needed was the phase.
        pytest.param("no-before-images", "Move it aside", id="missing-before-images"),
        # Truncated. The old gate grepped `"phase":` out of whatever was on
        # disk, so this read as `migrated` and exited 0 on a Mac that had never
        # been migrated at all.
        pytest.param("truncated", "Move it aside", id="truncated"),
        pytest.param("wrong-schema", "Move it aside", id="wrong-schema"),
        pytest.param("unknown-phase", "Move it aside", id="unknown-phase"),
        # A before-image that is no longer there: a transaction that can no
        # longer be undone, and a different recovery to offer.
        pytest.param("missing-image", "Finish the install by hand", id="missing-image"),
    ],
)
def test_migrate_rejects_invalid_or_stale_receipts(
    tmp_path: Path, case: str, message: str
) -> None:
    # A receipt this installer cannot trust is a state to refuse, not a state to
    # carry on from: the next step would be to replace an engine whose
    # before-images are unknown. Every answer names what to do with the file,
    # because deleting the only record of a rollback by hand is the user's
    # decision.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    migration = harness["home"] / ".local/state/ciaobot/migration"
    if case == "no-before-images":
        migration.mkdir(parents=True)
        (migration / "receipt.json").write_text(
            json.dumps({"schema": 1, "kind": "desktop_host", "phase": "migrated"}),
            encoding="utf-8",
        )
    elif case == "truncated":
        migration.mkdir(parents=True)
        (migration / "receipt.json").write_text(
            '{"schema":1,"kind":"desktop_host","phase":"migrated",', encoding="utf-8"
        )
    elif case == "wrong-schema":
        migration.mkdir(parents=True)
        (migration / "receipt.json").write_text(
            json.dumps({"schema": 2, "kind": "desktop_host", "phase": "migrated"}),
            encoding="utf-8",
        )
    elif case == "unknown-phase":
        migration.mkdir(parents=True)
        (migration / "receipt.json").write_text(
            json.dumps({"schema": 1, "kind": "desktop_host", "phase": "done"}),
            encoding="utf-8",
        )
    else:
        migration = _write_settled_migration(harness, phase="started")
        receipt = json.loads((migration / "receipt.json").read_text(encoding="utf-8"))
        receipt["before"]["shim"] = str(migration / "before/gone-ciao")
        (migration / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert "receipt.json" in result.stderr
    assert message in result.stderr, result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""


@needs_local_tools
def test_migrate_does_not_believe_a_stale_success_receipt(
    tmp_path: Path,
) -> None:
    # A success receipt on a Mac whose install does not match it - restored from
    # a backup, or with the app's agent back - is a Mac with no engine and a
    # message saying it is migrated. The receipt is not believed, and it is not
    # thrown away either: the migration is done again from the originals it kept.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    _write_settled_migration(harness, phase="migrated")
    (harness["home"] / "Library/LaunchAgents/Ciaobot.plist").write_bytes(b"<plist/>")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert "already migrated" not in result.stdout
    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (harness["home"] / "Library/LaunchAgents/Ciaobot.plist").exists()


@needs_local_tools
def test_migrate_ignores_success_receipt_with_no_install(tmp_path: Path) -> None:
    # The receipt claims a settled host migration, but there is no install
    # receipt at all, so there is no engine the migration could have installed.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    migration = harness["home"] / ".local/state/ciaobot/migration"
    (migration / "before").mkdir(parents=True)
    (migration / "receipt.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "desktop_host",
                "phase": "migrated",
                "version": VERSION,
                "workspace": str(harness["home"] / "Ciaobot"),
                "host_url": "",
                "started_at": "2026-09-26T10:00:00Z",
                "before": {
                    "server_plist": "",
                    "desktop_plist": "",
                    "shim": "",
                    "install_receipt": "",
                    "tool_env": "",
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert "already migrated" not in result.stdout
    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "installed_no_start"


@needs_local_tools
def test_migrate_rejects_receipt_the_reader_cannot_run(tmp_path: Path) -> None:
    # A receipt that cannot be validated is a receipt that cannot be resumed
    # from, including when the reader itself is the thing that failed: assuming
    # "no receipt" would re-snapshot a Mac whose originals are already gone.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    _write_settled_migration(harness, phase="migrated")
    _knob(harness, "fail-receipt-read")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "cannot be read" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@needs_local_tools
@pytest.mark.parametrize(
    "knob,message,phase",
    [
        # A collision is refused before anything is touched, so there is nothing
        # to undo and no receipt to write.
        pytest.param(
            "collision", "was not installed by Ciaobot", None, id="collision"
        ),
        pytest.param(
            "fail-uv-tool-install", "uv tool install failed", "rolled_back",
            id="tool-install",
        ),
        pytest.param(
            "fail-install-receipt",
            "could not write the install receipt",
            "rolled_back",
            id="receipt-write",
        ),
        # When the writer of the receipt is itself what is broken, the last thing
        # it managed to record is the phase the transaction started in - which is
        # still a resumable one, and the message says what happened.
        pytest.param(
            "receipt-write-2",
            "could not write the migration receipt",
            "started",
            id="final-receipt-write",
        ),
    ],
)
def test_migrate_client_failure_restores_original_state(
    tmp_path: Path, knob: str, message: str, phase: str | None
) -> None:
    # Every way a client migration can fail has to leave the Mac as it was: the
    # labels it disabled enabled and loaded again, the plist it deleted back, the
    # shim and the install receipt it replaced back byte for byte. A client left
    # disabled has no engine of its own *and* no host to sign in at, which is
    # the one state this path exists to avoid.
    harness = _harness(tmp_path)
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )
    _arm(harness, knob)
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert message in result.stderr, result.stderr
    if phase is None:
        assert _log(harness, "launchctl.log") == ""
    else:
        assert _migration_receipt(harness)["phase"] == phase
        log = _log(harness, "launchctl.log")
        for label in ("com.ciao.server", "Ciaobot"):
            assert f"enable gui/{os.getuid()}/{label}" in log, label
        assert f"bootstrap gui/{os.getuid()} " in log
        assert f"kickstart -k gui/{os.getuid()}/com.ciao.server" in log
    assert _replaced_state(harness) == untouched, knob
    # Nothing this transaction created is left behind either.
    assert not _tool_env(harness).exists()
    # And a client never claims an engine was restored: it had none.
    assert "engine was restored" not in result.stderr


@needs_local_tools
@pytest.mark.parametrize(
    "knob,message",
    [
        pytest.param("fail-uv-tool-install", "uv tool install failed", id="tool-install"),
        pytest.param(
            "fail-install-receipt",
            "could not write the install receipt",
            id="receipt-writer",
        ),
        pytest.param("fail-ciao-setup", "ciao setup failed", id="setup"),
        pytest.param(
            "fail-ciao-service-start",
            "could not start the engine",
            id="service-start",
        ),
    ],
)
def test_migrate_host_failure_restores_original_state(
    tmp_path: Path, knob: str, message: str
) -> None:
    # The host path hands a working engine over. Any failure after the tool is
    # installed has to give the Mac its engine back, and "back" means the app's
    # plist, the app's shim, the install receipt the previous install left and a
    # launchd that has the agent loaded and started again - not just an exit
    # status of 1.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    _arm(harness, knob)
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert message in result.stderr, result.stderr
    assert "Ciaobot.app's engine was restored" in result.stderr
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    assert _replaced_state(harness) == untouched, knob
    log = _log(harness, "launchctl.log")
    assert (
        f"bootstrap gui/{os.getuid()} "
        f"{harness['home']}/Library/LaunchAgents/com.ciao.server.plist"
    ) in log
    assert f"kickstart -k gui/{os.getuid()}/com.ciao.server" in log
    assert f"enable gui/{os.getuid()}/Ciaobot" in log


@needs_local_tools
def test_migrate_host_wrong_health_version_restores_original_state(
    tmp_path: Path,
) -> None:
    # An engine that answers is not enough: the app's agent is retired only once
    # the engine replacing it reports the version that was just installed.
    # Anything else means something else is still serving this Mac.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    (harness["home"] / "startup-version").write_text("0.0.1\n", encoding="utf-8")
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert f"with version {VERSION}" in result.stderr
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    assert _replaced_state(harness) == untouched
    # The app's agent was never retired on the strength of the wrong answer.
    assert f"bootout gui/{os.getuid()}/Ciaobot" not in _log(harness, "launchctl.log")


@needs_local_tools
def test_migrate_host_failure_restores_previous_install(tmp_path: Path) -> None:
    # A Mac that already has an engine installed through uv - an upgrade, not a
    # first hand-over - has an install receipt and a tool environment that this
    # migration replaces. Saying the app's engine was restored while the install
    # receipt still names the version this run installed would leave the next
    # swap working from a lie.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    _knob(harness, "fail-ciao-setup")
    state = home / ".local/state/ciaobot"
    state.mkdir(parents=True, exist_ok=True)
    previous = {
        "version": "0.9.0",
        "executable": str(home / ".local/bin/ciao"),
        "python": str(_tool_env(harness) / "bin" / "python"),
        "service_backend": "launchd",
        "service_label": "com.ciao.server",
        "installed_at": "2026-09-01T10:00:00+00:00",
    }
    (state / "install-receipt.json").write_text(
        json.dumps(previous, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (_tool_env(harness) / "bin").mkdir(parents=True)
    _write_exec(_tool_env(harness) / "bin" / "python", "#!/bin/sh\nexit 0\n")
    before_tool_env = (_tool_env(harness) / "bin" / "python").read_bytes()
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "Ciaobot.app's engine was restored" in result.stderr
    assert _replaced_state(harness) == untouched
    # The receipt the previous install left is the one in place afterwards, and
    # so is the environment it was installed into.
    assert _install_receipt(harness) == previous
    assert (_tool_env(harness) / "bin" / "python").read_bytes() == before_tool_env
    # And the originals it would need are kept, so a later retry can still work.
    before = migration_before(harness)
    assert (before / "install-receipt.json").exists()
    assert (before / "tool-env" / "bin" / "python").read_bytes() == before_tool_env


@needs_local_tools
def test_migrate_host_rollback_failure_is_reported(tmp_path: Path) -> None:
    # A rollback that cannot put the engine back has to say so. launchd keeps
    # whatever job definition it already holds, so a bootstrap that fails leaves
    # an agent pointing at an engine this script has just removed - and
    # reporting that as a completed rollback is how a host ends up with none.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    _knob(harness, "fail-ciao-setup")
    _knob(harness, "fail-launchctl-bootstrap")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "could NOT be fully restored" in result.stderr
    assert "engine was restored:" not in result.stderr
    assert "rollback incomplete" in _migration_receipt(harness)["error"]


@needs_local_tools
def test_migrate_host_success_orders_start_health_retirement(
    tmp_path: Path,
) -> None:
    # The migration is this order: reload the agent, start the service, see the
    # installed version come back, and only then retire the app's own agent.
    # Every one of those steps is in one trace, so the order is asserted rather
    # than inferred from the fact that the run happened to succeed.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    trace = _trace(harness)

    def at(prefix: str) -> int:
        return next(i for i, line in enumerate(trace) if line.startswith(prefix))

    order = [
        at("receipt-write started"),
        at("ciao setup "),
        at("ciao service start"),
        at("curl -fsS http://localhost:"),
        at(f"launchctl bootout gui/{os.getuid()}/Ciaobot"),
        at(f"launchctl disable gui/{os.getuid()}/Ciaobot"),
        at("receipt-write migrated"),
    ]
    assert order == sorted(order), trace
    # The app's agent is gone and the engine that replaced it is undisturbed.
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert f"bootout gui/{os.getuid()}/com.ciao.server" not in _log(
        harness, "launchctl.log"
    )
    assert _install_receipt(harness)["service_backend"] == "launchd"
    assert _migration_receipt(harness)["phase"] == "migrated"
    # The bundle is not deleted here, and the user is told how to remove it.
    assert "ciao desktop uninstall" in result.stdout


@needs_local_tools
def test_migrate_host_retirement_failure_rolls_back(tmp_path: Path) -> None:
    # A launchd that refuses to disable the app's agent means the app can come
    # back next to the engine that was supposed to replace it. That is not a
    # migration that worked, so the plist stays where it was, the app's engine
    # goes back, and the failure is reported.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    _knob(harness, "fail-launchctl-disable-Ciaobot")
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert "could not be retired" in result.stderr
    # The plist is not removed behind a label that is still loaded.
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert _replaced_state(harness) == untouched
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    # The durable phase a crash would resume from was written before the
    # retirement was attempted, and `migrated` was never reached.
    trace = _trace(harness)
    assert "receipt-write retiring" in trace
    assert "receipt-write migrated" not in trace


@needs_local_tools
def test_migrate_host_resumes_from_retiring(tmp_path: Path) -> None:
    # A crash between the phase and the retirement leaves a `retiring` receipt on
    # a Mac whose engine is already the new one. The next run resumes from the
    # originals it kept rather than treating the transaction as settled.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    _write_settled_migration(harness, phase="retiring")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (harness["home"] / "Library/LaunchAgents/Ciaobot.plist").exists()
