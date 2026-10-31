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
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ciao.server_host import SUPERSEDED_HOST_PROTOCOLS
from ciao.release_manifest import (
    RELEASE_PUBLIC_KEY,
    SERVER_HOST_FILENAME,
    build_manifest,
)
from tests.test_release_manifest import _keypair, _sign

REPO_ROOT = Path(__file__).parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-engine.sh"
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")

VERSION = "1.2.3"
WHEEL_NAME = f"ciaobot-{VERSION}-py3-none-any.whl"
WHEEL_BYTES = b"the signed wheel"

# The end-to-end runs resolve their downloads with the real curl, so a machine
# without it (or without shasum) has nothing to run them against.
# install-engine.sh is the macOS/Linux `curl | sh` installer; Windows installs with
# scripts/install.ps1 (#853), so a test that runs the script checks nothing there.
# The tests that only read the script, or run its verifier through Python, stay on
# every OS.
runs_the_sh_installer = pytest.mark.skipif(
    sys.platform == "win32",
    reason="install-engine.sh is the macOS/Linux installer; Windows uses install.ps1 (#853)",
)

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


def _host_manifest_bytes(
    host_archive: Path, *, signing_team: str | None = None
) -> bytes:
    """A schema-1 manifest naming the wheel and `host_archive`."""
    from ciao.release_manifest import server_host_artifact_entry

    entry = server_host_artifact_entry(host_archive)
    if signing_team is not None:
        from ciao.server_host import DeveloperIDSigning

        entry = server_host_artifact_entry(
            host_archive, signing=DeveloperIDSigning(signing_team)
        )
    return json.dumps(
        build_manifest(
            VERSION,
            [
                {
                    "filename": WHEEL_NAME,
                    "kind": "wheel",
                    "platform": "any",
                    "arch": "any",
                    "sha256": hashlib.sha256(WHEEL_BYTES).hexdigest(),
                    "size": len(WHEEL_BYTES),
                },
                entry,
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


@runs_the_sh_installer
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
# The entry point it leaves behind is a *symlink* into the tool environment, the
# way a real `uv tool install` does it, so a rollback is exercised against the
# state a migration actually finds rather than a plain file uv never leaves.
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
        # Remember the wheel this run is authenticated against: a rollback must run
        # while that file still exists, and the EXIT handler deletes it only after
        # its attempt.
        previous=
        for argument in "$@"; do
            if [ "$previous" = "--with" ]; then
                printf '%s\n' "$argument" > "$HOME/native-host-wheel"
            fi
            previous=$argument
        done
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
        # `python -I -m ciao.server_host_install install` runs the real B1
        # inspector's native codesign probes, which only a Mac has and which the
        # module itself refuses off it. The shell contract tests exercise the
        # installer's acquisition wiring on every platform, so the install call
        # is recorded and answered here; the module's own behaviour is proved in
        # tests/test_server_host_install.py. `select` is pure and still runs for
        # real, so the entry the script acts on is the canonical one.
        case "$*" in
            "-I -m ciao.server_host_install install "*)
                printf '%s\\n' "$*" >> "$HOME/server-host-install.log"
                # The outer installer's own pid, which binds a deferred upgrade
                # to this run; the fake journal below reports it back so the
                # shell's ownership check is exercised for real.
                previous=
                for argument in "$@"; do
                    if [ "$previous" = "--owner-pid" ]; then
                        printf '%s\\n' "$argument" > "$HOME/native-host-owner-pid"
                    fi
                    previous=$argument
                done
                if [ -f "$HOME/fail-server-host-install" ]; then
                    printf 'server-host-install-failed\\n' >> "$HOME/trace.log"
                    exit 1
                fi
                if [ -f "$HOME/existing-server-host" ]; then
                    printf 'warning: there is already a host at the target\\n' >&2
                    exit 3
                fi
                # The two warn-and-continue kinds the module maps to exit 3: a
                # native inspection that failed (a codesign probe that timed out
                # or parsed differently) and a local install failure (disk full,
                # chmod EPERM). Both leave the engine install to carry on.
                if [ -f "$HOME/warn-inspection-server-host" ]; then
                    printf 'Error: the host bundle fails strict signature verification\\n' >&2
                    exit 3
                fi
                if [ -f "$HOME/warn-install-server-host" ]; then
                    printf 'Error: the ownership record could not be written: No space left on device\\n' >&2
                    exit 3
                fi
                # A legacy-to-signed replacement a --no-start run refused: the
                # module's own status for "the migration completes on a later
                # normal run", with nothing replaced.
                if [ -f "$HOME/defer-required-server-host" ]; then
                    printf 'warning: this host can only be upgraded as a recoverable transaction\\n' >&2
                    exit 4
                fi
                # A deferred upgrade: the old host and its exact record are
                # retained and the journal is installed. The fake journal is what
                # `transaction-status` reports back, so the shell's commit and
                # rollback wiring is exercised end to end.
                if [ -f "$HOME/defer-server-host-install" ]; then
                    if [ ! -f "$HOME/native-host-owner-pid" ]; then
                        printf 'deferred install needs --owner-pid\\n' >&2
                        exit 1
                    fi
                    # The old app and its exact record are retained, then replaced:
                    # the same pair a real deferred upgrade keeps in its journal.
                    mkdir -p "$HOME/server-host-before"
                    if [ -d "$HOME/Applications/Ciaobot Server.app" ]; then
                        rm -rf "$HOME/server-host-before/app"
                        cp -pR "$HOME/Applications/Ciaobot Server.app" \
                            "$HOME/server-host-before/app"
                    fi
                    if [ -f "$HOME/.local/state/ciaobot/server-host.json" ]; then
                        cp -p "$HOME/.local/state/ciaobot/server-host.json" \
                            "$HOME/server-host-before/record"
                    fi
                    mkdir -p "$HOME/Applications/Ciaobot Server.app/Contents"
                    printf 'candidate\\n' \
                        > "$HOME/Applications/Ciaobot Server.app/Contents/build"
                    mkdir -p "$HOME/.local/state/ciaobot"
                    printf '{"schema": 2, "candidate": true}\\n' \
                        > "$HOME/.local/state/ciaobot/server-host.json"
                    printf '0123456789abcdef0123456789abcdef\\n' > "$HOME/native-host-pending"
                    if [ -f "$HOME/unreadable-status-after-install" ]; then
                        : > "$HOME/native-host-status-unreadable"
                    fi
                    if [ -f "$HOME/interrupt-after-deferred-install" ]; then
                        rm -f "$HOME/interrupt-after-deferred-install"
                        # The shell is signalled after the durable install landed and
                        # before it could record the operation itself.
                        kill -TERM "$PPID"
                        exit 143
                    fi
                    exit 0
                fi
                exit 0
                ;;
            "-I -m ciao.server_host_install transaction-status")
                if [ -f "$HOME/native-host-status-unreadable" ]; then
                    printf 'Error: the host upgrade journal is unreadable\\n' >&2
                    exit 1
                fi
                if [ -f "$HOME/native-host-pending" ]; then
                    printf '{"operation": "%s", "owner_alive": %s, "owner_pid": %s, "pending": true, "phase": "installed"}\\n' \\
                        "$(cat "$HOME/native-host-pending")" \\
                        "$(cat "$HOME/native-host-owner-alive" 2>/dev/null || echo true)" \\
                        "$(cat "$HOME/native-host-owner-pid")"
                else
                    printf '{"pending": false}\\n'
                fi
                exit 0
                ;;
            "-I -m ciao.server_host_install commit-upgrade "*)
                printf '%s\\n' "$*" >> "$HOME/native-host-transaction.log"
                printf 'native-host-commit\\n' >> "$HOME/trace.log"
                if [ -f "$HOME/fail-native-host-commit" ]; then exit 1; fi
                rm -f "$HOME/native-host-pending"
                if [ -f "$HOME/leftover-native-host-commit" ]; then
                    printf 'Error: the before-images could not be removed\\n' >&2
                    exit 5
                fi
                exit 0
                ;;
            "-I -m ciao.server_host_install rollback-upgrade "*)
                printf '%s\\n' "$*" >> "$HOME/native-host-transaction.log"
                printf 'native-host-rollback\\n' >> "$HOME/trace.log"
                # The wheel this command runs out of still has to be on disk.
                if [ -f "$HOME/native-host-wheel" ] &&
                    [ -f "$(cat "$HOME/native-host-wheel")" ]; then
                    printf 'native-host-wheel-present\\n' >> "$HOME/trace.log"
                fi
                if [ -f "$HOME/fail-native-host-rollback" ]; then exit 1; fi
                if [ -d "$HOME/server-host-before/app" ]; then
                    rm -rf "$HOME/Applications/Ciaobot Server.app"
                    cp -pR "$HOME/server-host-before/app" \
                        "$HOME/Applications/Ciaobot Server.app"
                fi
                if [ -f "$HOME/server-host-before/record" ]; then
                    mkdir -p "$HOME/.local/state/ciaobot"
                    cp -p "$HOME/server-host-before/record" \
                        "$HOME/.local/state/ciaobot/server-host.json"
                fi
                printf 'native-host-restored\\n' >> "$HOME/trace.log"
                rm -f "$HOME/native-host-pending"
                # The pair is back but the retained material could not be removed:
                # the module's distinct "settled with a leftover" status.
                if [ -f "$HOME/leftover-native-host-rollback" ]; then
                    printf 'Error: the before-images could not be removed\\n' >&2
                    exit 5
                fi
                exit 0
                ;;
        esac
        # An engine that predates the host work has no `ciao.server_host_install`
        # module at all. The shell probes for it before reading the host entry, so
        # this knob is what an older wheel answers.
        if [ -f "$HOME/no-server-host-module" ]; then
            case "$*" in
                *"ciao.server_host_install"*)
                    printf 'No module named ciao.server_host_install\\n' >&2
                    exit 1
                    ;;
            esac
        fi
        if [ "${1:-}" = "-I" ]; then shift; fi
        # The port probe cannot run at all: the wheel would not import, or
        # the interpreter would not start. That says nothing about the port,
        # and the installer must not read it as "taken".
        if [ -f "$HOME/port-probe-cannot-run" ]; then
            case "$*" in
                *port_is_available*) exit 2 ;;
            esac
        fi
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
# The half-installed-tool-env knob leaves behind the environment uv tool install
# reported as installed with no importable package in it, the state #668 was
# reported from: the interpreter is there and executable, so the entry point and
# interpreter checks both pass, and every run through it dies with the
# module-not-found the operator saw, spelled the way the real interpreter spells
# it. The import probe and the receipt write both go through this interpreter,
# so this is what whichever of them runs first finds.
if [ -f "\\$HOME/half-installed-tool-env" ]; then
    printf 'No module named ciao.install_receipt\\n' >&2
    exit 1
fi
# The install receipt is written by this interpreter, so the knob that breaks it
# has to be honoured here: a full disk or a read-only state directory fails the
# same way, after the tool is already installed. Only the writer fails: a
# different invocation is a different fault.
case "\\$*" in
    "-m ciao.install_receipt write"*) [ -f "\\$HOME/fail-install-receipt" ] && exit 1 ;;
esac
PYTHONPATH="__REPO_ROOT__" exec "__PYTHON__" "\\$@"
EOF
                chmod 755 "$tools/ciaobot/bin/python"
                cat > "$tools/ciaobot/bin/ciao" <<'EOF'
#!/bin/sh
printf 'ciao %s\n' "$*" >> "$HOME/trace.log"
printf '%s\n' "$*" >> "$HOME/ciao-calls.log"
# One engine start: a fresh run id, the launched `Ciaobot Server` process launchd
# holds, the Python child that holds the socket, and the runtime lock that names
# both - the identity the installer's health gate binds to.
engine_start() {
    if [ -f "$HOME/unrelated-responder" ]; then
        # Something else answers the port; nothing of ours started. The unrelated
        # responder's own run id changes, which is what makes a "changed unrelated
        # responder" observable.
        printf 'ffffffffffff4ccc8dddeeeeeeeeee02\n' > "$HOME/unrelated-startup-id"
        return 0
    fi
    count=$(cat "$HOME/engine-start-count" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$HOME/engine-start-count"
    printf '4343\n' > "$HOME/host-pid"
    printf '5151\n' > "$HOME/engine-pid"
    printf '4747\n' > "$HOME/engine-parent-pid"
    : > "$HOME/engine-started"
    runtime=$1/.runtime
    mkdir -p "$runtime"
    "__PYTHON__" - "$runtime/server.lock" "$count" "$2" "$1" <<'LOCK'
import json
import pathlib
import sys

path, count, port, workspace = sys.argv[1:5]
runtime = pathlib.Path(path).parent
pathlib.Path(path).write_text(
    json.dumps(
        {
            "pid": 5151,
            "port": int(port),
            "workspace": workspace,
            "runtime": str(runtime),
            "status": "running",
            "startup_id": "aaaaaaaabbbb4ccc8dddeeeeeeeeee%02d" % int(count),
        }
    ),
    encoding="utf-8",
)
LOCK
    chmod 600 "$runtime/server.lock" 2>/dev/null || true
    if [ -f "$HOME/lock-mode-0644" ]; then
        chmod 644 "$runtime/server.lock" 2>/dev/null || true
    fi
}
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
        if [ -f "$HOME/setup-generates-password" ] || [ -f "$HOME/fail-ciao-setup-after-password" ]; then
            # `ciao setup` prints every path it wrote, one per line. A generated
            # password is what writes the welcome-pending marker, so that path
            # is in the list; an update keeps any older marker and prints
            # nothing for it.
            setup_workspace=$HOME/Ciaobot
            previous=
            for argument in "$@"; do
                if [ "$previous" = "--workspace" ]; then setup_workspace=$argument; fi
                previous=$argument
            done
            mkdir -p "$setup_workspace/.runtime"
            : > "$setup_workspace/.runtime/welcome-pending"
            printf '%s\n' "$setup_workspace/.runtime/welcome-pending"
        fi
        if [ -f "$HOME/fail-ciao-setup-after-password" ]; then
            # Setup wrote the password, then failed for a reason the installer
            # treats as hard. Whatever it printed is already captured and may
            # hold a sign-in credential; it must not reach the terminal.
            printf '%s\n' "__SETUP_STDOUT_SENTINEL__"
            exit 1
        fi
        # `ciao setup` reporting a workspace whose memory regions could not be
        # set up: its own exit code, with the warning on stderr, and every other
        # step of the workspace in place.
        if [ -f "$HOME/fail-ciao-setup-memory" ]; then
            printf 'Warning: memory regions not set up for %s: RuntimeError: guide unwritable\\n' "$HOME/Ciaobot" >&2
            exit 3
        fi
        # `ciao setup` repoints the engine LaunchAgent at the engine this run
        # installed, and several checks read that plist back as the truth about
        # what is installed on this Mac, so the stub writes a real one instead of
        # leaving a plist behind that still points into Ciaobot.app.
        program=
        previous=
        for argument in "$@"; do
            if [ "$previous" = "--python" ]; then program=$argument; fi
            previous=$argument
        done
        # `--load-launchd` reloads and starts the job: a new engine process, so a
        # new startup id.
        case " $* " in
            *" --load-launchd "*)
                setup_workspace=$HOME/Ciaobot
                previous=
                for argument in "$@"; do
                    if [ "$previous" = "--workspace" ]; then setup_workspace=$argument; fi
                    previous=$argument
                done
                engine_start "$setup_workspace" \
                    "$(cat "$HOME/engine-port" 2>/dev/null || echo 8443)"
                ;;
        esac
        if [ -n "$program" ]; then
            "__PYTHON__" - "$program" "$HOME/Library/LaunchAgents/com.ciao.server.plist" <<'PLIST'
import pathlib
import plistlib
import sys

program, target = sys.argv[1:3]
path = pathlib.Path(target)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("wb") as handle:
    plistlib.dump(
        {"Label": "com.ciao.server", "ProgramArguments": [program, "run"]}, handle
    )
PLIST
        fi
        # The interruption that lands *after* setup has repointed the engine
        # plist and *before* the service is started: the window in which the
        # classifier reads this Mac as an ordinary installer-managed engine, so
        # the retry has to recognise the hand-over from its receipt instead.
        if [ -f "$HOME/interrupt-after-setup" ]; then
            rm -f "$HOME/interrupt-after-setup"
            kill -TERM "$PPID"
            exit 143
        fi
        ;;
    run)
        # The installer's foreground bootstrap. Simulate a wizard that
        # provisions the suggested folder (or ~/Ciaobot) and writes the live
        # definition, and record whether the installer created anything first.
        proposed=$HOME/Ciaobot
        previous=
        for argument in "$@"; do
            case "$argument" in --installer-suggest-workspace=*) proposed=${argument#*=} ;; esac
            if [ "$previous" = "--installer-suggest-workspace" ]; then proposed=$argument; fi
            previous=$argument
        done
        if [ -e "$proposed" ]; then existed=1; else existed=0; fi
        printf 'bootstrap %s existed=%s\n' "$proposed" "$existed" >> "$HOME/bootstrap.log"
        if [ -f "$HOME/cancel-installer-bootstrap" ]; then exit 130; fi
        if [ -f "$HOME/fail-installer-bootstrap" ]; then exit 1; fi
        if [ -f "$HOME/refuse-installer-bootstrap" ]; then
            printf 'Ciaobot installer bootstrap refused: this machine is not fresh\n' >&2
            exit 64
        fi
        mkdir -p "$proposed/.runtime"
        printf '[]\n' > "$proposed/.runtime/workspaces.json"
        printf 'PWA_PORT=8443\n' > "$proposed/.env"
        printf '%s\n' "$proposed" > "$HOME/bootstrap-workspace"
        "__PYTHON__" - "$HOME/Library/LaunchAgents/com.ciao.server.plist" <<'PLIST'
import pathlib
import plistlib
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("wb") as handle:
    plistlib.dump(
        {"Label": "com.ciao.server", "ProgramArguments": [sys.executable, "run"]},
        handle,
    )
PLIST
        ;;
    installer)
        if [ "${2:-}" = activate ]; then
            engine_start "$(cat "$HOME/bootstrap-workspace" 2>/dev/null || echo "$HOME/Ciaobot")" \
                "$(cat "$HOME/engine-port" 2>/dev/null || echo 8443)"
        fi
        if [ "${2:-}" = activate ] && [ -f "$HOME/fail-installer-activate" ]; then
            printf '{"ok":false,"error":"activation refused"}\n'
            exit 1
        fi
        if [ -f "$HOME/installer-activation-output" ]; then
            cat "$HOME/installer-activation-output"
        else
            "__PYTHON__" -c 'import json, sys; print(json.dumps({"ok": True, "workspace": sys.argv[1], "service_backend": "macos", "definition": sys.argv[2], "port": 8443}))' "$(cat "$HOME/bootstrap-workspace")" "$HOME/Library/LaunchAgents/com.ciao.server.plist"
        fi
        ;;
    service)
        if [ -f "$HOME/fail-ciao-service-start" ]; then exit 1; fi
        service_workspace=$HOME/Ciaobot
        previous=
        for argument in "$@"; do
            if [ "$previous" = "--workspace" ]; then service_workspace=$argument; fi
            previous=$argument
        done
        engine_start "$service_workspace" \
            "$(cat "$HOME/engine-port" 2>/dev/null || echo 8443)"
        if [ -f "$HOME/deny-workspace-access" ]; then : > "$HOME/denial-armed"; fi
        ;;
    setup-url)
        echo "http://localhost:8443/?setup=tok"
        ;;
esac
exit 0
EOF
                chmod 755 "$tools/ciaobot/bin/ciao"
                # Real uv links the entry point into the tool environment with a
                # path relative to the bin directory it writes it in, and
                # replaces whatever was there, so that is what is left here: a
                # rollback that restores the app shim has to remove this link
                # rather than write the shim through it.
                rm -f "$HOME/.local/bin/ciao"
                ln -s ../share/uv/tools/ciaobot/bin/ciao "$HOME/.local/bin/ciao"
                ;;
        esac
        ;;
esac
"""

# A fake `ciao setup` prints this after the welcome marker it wrote, standing in
# for the sign-in credential setup's stdout can carry. A failed install must
# never echo the captured output (#2162).
SETUP_STDOUT_SENTINEL = "SYNTHETIC-SETUP-CREDENTIAL-2162-do-not-echo"


def _fake_uv() -> str:
    return (
        _UV_STUB.replace("__REPO_ROOT__", str(REPO_ROOT))
        .replace("__PYTHON__", sys.executable)
        .replace("__VERSION__", VERSION)
        .replace("__SETUP_STDOUT_SENTINEL__", SETUP_STDOUT_SENTINEL)
    )


# The engine's own answers come from a fake `curl`: the health poll has to be
# able to report a version that matches, or one that does not, without a real
# engine being started on the Mac running the tests. Every other URL is passed
# straight through to the real curl, so the file:// release downloads are still
# the real thing.
#
# The engine answers on the port its workspace names (`engine-port`), and on
# no other: a port nobody serves is not answered for, so the installer's
# preflight sees a foreign listener rather than Ciaobot's own engine.
#
# `startup-port-in-use` makes the engine answer that it could not bind its
# port (#1654), which is what the installer has to read instead of waiting
# the whole health budget out.
_CURL_STUB = """#!/bin/sh
printf 'curl %s\\n' "$*" >> "$HOME/trace.log"
engine_port=$(cat "$HOME/engine-port" 2>/dev/null || echo 8443)
# Every engine start bumps this counter, and the startup id is derived from it:
# the same id on two answers means the same running engine, which is how the
# installer can tell the candidate it started from an unrelated server that
# reports the same version.
engine_start=$(cat "$HOME/engine-start-count" 2>/dev/null || echo 0)
startup_id=$(printf 'aaaaaaaabbbb4ccc8dddeeeeeeeeee%02d' "$engine_start")
if [ -f "$HOME/unrelated-responder" ]; then
    startup_id=$(cat "$HOME/unrelated-startup-id" 2>/dev/null \\
        || echo ffffffffffff4ccc8dddeeeeeeeeee01)
fi

response_output=
previous=
for arg in "$@"; do
    if [ "$previous" = -o ]; then response_output=$arg; fi
    previous=$arg
done
for arg in "$@"; do
    case "$arg" in
        */api/startup-status)
            [ -z "$response_output" ] || exec > "$response_output"
            case "$arg" in
                *":$engine_port/"*) ;;
                *) exit 7 ;;
            esac
            if [ -f "$HOME/no-pre-responder" ] && [ ! -f "$HOME/engine-started" ]; then
                # Only the installer's own pre-start capture carries this marker;
                # the port availability check has to keep seeing an answer.
                probe=$(ls -d "${TMPDIR:-/tmp}"/ciaobot-engine.* 2>/dev/null | head -1)
                if [ -n "$probe" ] && [ -f "$probe/pre-responder-probe" ]; then
                    printf 'responder=absent\\n' >> "$HOME/responders.log"
                    exit 7
                fi
            fi
            printf 'responder=%s\\n' "$startup_id" >> "$HOME/responders.log"
            # macOS denied the workspace folder: the supervisor writes its one
            # denial line and exits, so the engine never answers this probe again.
            # Armed only once the service was started, like the real prompt, so the
            # earlier port check still sees a normal engine.
            if [ -f "$HOME/deny-workspace-access" ] && [ -f "$HOME/denial-armed" ]; then
                if [ ! -f "$HOME/denial-written" ]; then
                    : > "$HOME/denial-written"
                    # The installer compares this file's mtime with the marker it
                    # wrote before the wait, and a shell without sub-second `-nt`
                    # needs the two to be in different seconds.
                    [ -x /bin/sleep ] && /bin/sleep 1
                    mkdir -p "$HOME/.local/state/ciaobot"
                    printf 'Ciaobot Server was denied access to the workspace folder\n' \
                        > "$HOME/.local/state/ciaobot/workspace-access-denied.txt"
                fi
                exit 7
            fi
            version=$(cat "$HOME/startup-version" 2>/dev/null || echo "__VERSION__")
            count=$(cat "$HOME/startup-response-count" 2>/dev/null || echo 0)
            count=$((count + 1))
            printf '%s\\n' "$count" > "$HOME/startup-response-count"
            if [ -f "$HOME/startup-response" ]; then
                cat "$HOME/startup-response"
                exit "$(cat "$HOME/startup-response-exit" 2>/dev/null || echo 0)"
            fi
            if [ "$count" -eq 1 ] && [ -f "$HOME/startup-first-response" ]; then
                cat "$HOME/startup-first-response"
                exit 0
            fi
            if [ -f "$HOME/startup-port-in-use" ]; then
                port=$(cat "$HOME/startup-port-in-use")
                printf '{"phases": [{"name": "server_starting", "status": "failed", "message": "port %s is already in use by another program. Free port %s, or set PWA_PORT in the workspace .env to serve on a different port.", "started_at": null, "finished_at": "2026-10-25T12:00:00+00:00"}], "overall_ready": true, "port_in_use": true, "version": "%s", "desktop_api_version": 1, "startup_id": "'"$startup_id"'", "pid": 123, "latest_version": null, "update_available": false}\\n' "$port" "$port" "$version"
                exit 0
            fi
            # Shaped like the real response, which has keys before `version`.
            printf '{"phases": [{"name": "server_starting", "status": "done", "message": "", "started_at": "2026-10-25T12:00:00+00:00", "finished_at": "2026-10-25T12:00:01+00:00"}], "overall_ready": true, "port_in_use": false, "version": "%s", "desktop_api_version": 1, "startup_id": "'"$startup_id"'", "pid": 123, "latest_version": null, "update_available": false}\\n' "$version"
            exit 0
            ;;
    esac
done
exec "__CURL__" "$@"
"""


def _harness(
    tmp_path: Path,
    *,
    host_archive: Path | None = None,
    signed_team: str | None = None,
) -> dict[str, Any]:
    """A signed release on `file://`, fake tools, a fake `$HOME`, and the script
    itself with the embedded key swapped for the test key.

    ``host_archive`` stages a signed ``server-host`` entry beside the wheel: the
    archive is copied into the release under its fixed name, and the manifest is
    rebuilt to authenticate it with the same test key. ``signed_team`` selects the
    explicitly signed revision-5 host entry instead of the production one; the
    team is always a fake one.
    """
    home = (tmp_path / "home").resolve()
    (home / ".local" / "bin").mkdir(parents=True)
    (home / "Ciaobot").mkdir(parents=True)
    # The port the fake engine answers on. The workspace `.env` says 8443, so
    # the fake curl answers there and nowhere else.
    (home / "engine-port").write_text("8443\n", encoding="utf-8")

    release = tmp_path / "rel" / f"v{VERSION}"
    release.mkdir(parents=True)
    private_key, public_key, key_id = _keypair()
    if host_archive is None:
        raw = _manifest_bytes()
    else:
        raw = _host_manifest_bytes(host_archive, signing_team=signed_team)
        shutil.copyfile(host_archive, release / SERVER_HOST_FILENAME)
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
        '# Which label the call names. `print` and `bootout` take it from the\n'
        '# target, `bootstrap` from the plist it loads, `kickstart` from the target.\n'
        'label=\n'
        'case "$1" in\n'
        '    print|bootout|enable|disable) label=${2##*/} ;;\n'
        '    bootstrap) name=${3##*/}; label=${name%.plist} ;;\n'
        '    kickstart) label=${3##*/} ;;\n'
        'esac\n'
        '# A knob per subcommand, optionally narrowed to one label, so a test\n'
        '# can make launchctl refuse exactly the call it is about.\n'
        'if [ -f "$HOME/fail-launchctl-$1-$label" ] || [ -f "$HOME/fail-launchctl-$1" ]; then\n'
        '    printf \'launchctl-failed %s\\n\' "$*" >> "$HOME/trace.log"\n'
        '    exit 1\n'
        'fi\n'
        '# launchd state, so the installer has to ask rather than assume: `print`\n'
        '# succeeds only for a label launchd holds, and bootstrap/bootout move it.\n'
        'case "$1" in\n'
        '    print)\n'
        '        if [ -f "$HOME/launchctl-print-indeterminate" ]; then\n'
        '            printf \'launchctl: Operation not permitted\\n\' >&2\n'
        '            exit 1\n'
        '        fi\n'
        '        if [ -f "$HOME/loaded-$label" ]; then\n'
        '            printf \'%s = {\\n\' "$label"\n'
        '            printf \'\\tactive count = 1\\n\'\n'
        '            if [ -f "$HOME/host-pid" ]; then\n'
        '                printf \'\\tpid = %s\\n\' "$(cat "$HOME/host-pid")"\n'
        '            fi\n'
        '            printf \'}\\n\'\n'
        '            exit 0\n'
        '        fi\n'
        '        printf \'Could not find service "%s" in domain for user gui: 501\\n\' \\\n'
        '            "$label" >&2\n'
        '        exit 113\n'
        '        ;;\n'
        '    bootstrap) : > "$HOME/loaded-$label" ;;\n'
        '    bootout) rm -f "$HOME/loaded-$label" ;;\n'
        'esac\n'
        '# An interruption in the middle of a retirement: launchd has done what\n'
        '# it was asked and the process goes away before the next call, which is\n'
        '# what a Ctrl-C there looks like. From here the label is unloaded.\n'
        'if [ -f "$HOME/interrupt-after-$1-$label" ]; then\n'
        '    rm -f "$HOME/interrupt-after-$1-$label"\n'
        '    kill -TERM "$PPID"\n'
        'fi\n'
        'exit 0\n',
    )
    # `lsof` answers the two questions the health gate asks: which single pid holds
    # the listening socket (plain `-t`, one pid per line, nonzero when nothing
    # matches), and which executable text a pid runs (`-d txt -Fn`, one `n` record).
    # The listener can be replaced while a request is in flight, and the launchd
    # job's executable can be something else entirely; both are knobs.
    _write_exec(
        fakebin / "lsof",
        "#!/bin/sh\n"
        'printf \'lsof %s\\n\' "$*" >> "$HOME/trace.log"\n'
        'if [ -f "$HOME/lsof-cannot-run" ]; then exit 1; fi\n'
        'executable=\n'
        'for argument in "$@"; do\n'
        '    case "$argument" in\n'
        '        -d) executable=1 ;;\n'
        '    esac\n'
        'done\n'
        'if [ -n "$executable" ]; then\n'
        '    if [ -f "$HOME/host-executable-indeterminate" ]; then exit 1; fi\n'
        '    if [ -f "$HOME/host-executable-wrong" ]; then\n'
        '        printf \'n/usr/bin/python3\\n\'\n'
        '        exit 0\n'
        '    fi\n'
        '    if [ -f "$HOME/host-executable-two" ]; then\n'
        '        printf \'n/usr/bin/python3\\n\'\n'
        '        printf \'n/usr/bin/python3\\n\'\n'
        '        exit 0\n'
        '    fi\n'
        '    if [ -f "$HOME/host-executable-mixed" ]; then\n'
        '        printf \'n/usr/bin/python3\\n\'\n'
        '        printf \'n%s\\n\' \\\n'
        '            "$HOME/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"\n'
        '        exit 0\n'
        '    fi\n'
        '    printf \'p%s\\n\' "$(cat "$HOME/host-pid" 2>/dev/null || echo 4343)"\n'
        '    printf \'n%s\\n\' \\\n'
        '        "$HOME/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"\n'
        '    exit 0\n'
        'fi\n'
        'if [ -f "$HOME/listener-replaced" ]; then\n'
        '    # The socket changes owner on every lookup: an unrelated listener and\n'
        '    # the candidate take turns, so no single request sees the same listener\n'
        '    # on both sides of it.\n'
        '    count=$(cat "$HOME/listener-replaced-count" 2>/dev/null || echo 0)\n'
        '    count=$((count + 1))\n'
        '    echo "$count" > "$HOME/listener-replaced-count"\n'
        '    if [ $((count % 2)) -eq 1 ]; then\n'
        '        printf \'9999\\n\'\n'
        '    else\n'
        '        cat "$HOME/engine-pid" 2>/dev/null || exit 1\n'
        '    fi\n'
        '    exit 0\n'
        'fi\n'
        'if [ -f "$HOME/engine-started" ]; then\n'
        '    if [ -f "$HOME/listener-pid" ]; then\n'
        '        cat "$HOME/listener-pid"\n'
        '        exit 0\n'
        '    fi\n'
        '    cat "$HOME/engine-pid" 2>/dev/null || exit 1\n'
        '    exit 0\n'
        'fi\n'
        'if [ -f "$HOME/pre-listener-pid" ]; then\n'
        '    cat "$HOME/pre-listener-pid"\n'
        '    exit 0\n'
        'fi\n'
        'exit 1\n',
    )
    # The process table the health check walks: the engine's Python child, an
    # intermediate parent, and the `Ciaobot Server` process launchd spawned. The
    # job pid is never the socket holder, exactly as on a real install.
    _write_exec(
        fakebin / "ps",
        "#!/bin/sh\n"
        'printf \'ps %s\\n\' "$*" >> "$HOME/trace.log"\n'
        'if [ -f "$HOME/ps-cannot-run" ]; then exit 1; fi\n'
        'field=\n'
        'target=\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    case "$1" in\n'
        '        -o) field=$2; shift 2 ;;\n'
        '        -p) target=$2; shift 2 ;;\n'
        '        *) shift ;;\n'
        '    esac\n'
        'done\n'
        'engine=$(cat "$HOME/engine-pid" 2>/dev/null || echo 5151)\n'
        'middle=$(cat "$HOME/engine-parent-pid" 2>/dev/null || echo 4747)\n'
        'host=$(cat "$HOME/host-pid" 2>/dev/null || echo 4343)\n'
        'case "$field" in\n'
        '    ppid=)\n'
        '        if [ -f "$HOME/deep-chain" ]; then\n'
        '            # Six hops to the launched host: deeper than the bounded walk.\n'
        '            case "$target" in\n'
        '                "$engine") echo 6100 ;;\n'
        '                6100) echo 6101 ;;\n'
        '                6101) echo 6102 ;;\n'
        '                6102) echo 6103 ;;\n'
        '                6103) echo 6104 ;;\n'
        '                6104) echo "$host" ;;\n'
        '                "$host") echo 1 ;;\n'
        '                *) echo 1 ;;\n'
        '            esac\n'
        '            exit 0\n'
        '        fi\n'
        '        case "$target" in\n'
        '            "$engine") echo "$middle" ;;\n'
        '            "$middle") echo "$host" ;;\n'
        '            "$host") echo 1 ;;\n'
        '            *) echo 1 ;;\n'
        '        esac\n'
        '        ;;\n'
        '    comm=)\n'
        '        if [ "$target" = "$host" ]; then\n'
        '            printf \'%s\\n\' \\\n'
        '                "$HOME/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"\n'
        '        else\n'
        '            printf \'python3.13\\n\'\n'
        '        fi\n'
        '        ;;\n'
        '    *) exit 1 ;;\n'
        'esac\n'
        'exit 0\n',
    )
    _write_exec(fakebin / "osascript", "#!/bin/sh\nexit 1\n")
    # `plutil` reads one key out of a plist. The installer asks it for the
    # installed host's bundle version, so the stub answers with the version
    # the test wrote into the plist, the way the real tool does. It is a
    # script, not a binary, so it works on the Linux CI runner too.
    _write_exec(
        fakebin / "plutil",
        "#!/bin/sh\n"
        "# `plutil -extract CFBundleVersion raw -o - <plist>`\n"
        'plist=""\n'
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '        -*) continue ;;\n'
        '        *) plist="$arg" ;;\n'
        '    esac\n'
        'done\n'
        'if [ -f "$plist" ]; then\n'
        f'    exec "{sys.executable}" -c '
        "'import plistlib,sys; "
        "print(plistlib.load(open(sys.argv[1],\"rb\")).get(\"CFBundleVersion\",\"\"))' "
        '"$plist"\n'
        'fi\n'
        'exit 1\n',
    )
    _write_exec(
        fakebin / "pgrep",
        '#!/bin/sh\n'
        'printf \'pgrep %s\\n\' "$*" >> "$HOME/trace.log"\n'
        '# A `pgrep` that could not answer is not "no match": the installer has to\n'
        '# treat it as an unanswered question and refuse to revert anything.\n'
        'if [ -f "$HOME/pgrep-cannot-run" ]; then exit 3; fi\n'
        'if [ -f "$HOME/desktop-running" ]; then exit 0; fi\n'
        'exit 1\n',
    )
    _write_exec(fakebin / "sleep", "#!/bin/sh\nexit 0\n")
    _write_exec(
        fakebin / "uuidgen",
        "#!/bin/sh\necho 11111111-2222-3333-4444-555555555555\n",
    )

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
        r"^(PLISTBUDDY|LAUNCHCTL|OSASCRIPT|PGREP|LSOF|PS|CURL|SLEEP)=.*$",
        lambda m: f"{m.group(1)}={fakebin / m.group(1).lower()}",
        rewritten,
        flags=re.MULTILINE,
    )
    # A fresh install probes its port with the engine's own bind; 8443 is
    # whatever this machine runs there, so the copy probes a port just freed.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("0.0.0.0", 0))
    fresh_port = probe.getsockname()[1]
    probe.close()
    rewritten = re.sub(
        r"^FRESH_PORT=.*$", f"FRESH_PORT={fresh_port}", rewritten, count=1, flags=re.MULTILINE
    )
    script = tmp_path / "install-engine.sh"
    script.write_text(rewritten, encoding="utf-8")
    script.chmod(0o755)

    return {
        "fresh_port": fresh_port,
        "home": home,
        "script": script,
        "release": release,
        "fakebin": fakebin,
        "public_key": public_key,
        "env": {
            "PATH": f"{fakebin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            # Keep shell-specific startup-file writes inside the disposable
            # home even when the host/CI environment sets a global XDG path.
            "XDG_CONFIG_HOME": str(home / ".config"),
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
    # Simulate this harness's Mac, not the developer's workspace or a setup
    # test that exported its destination. Explicit test inputs still win below.
    inherited_env = {
        key: value for key, value in os.environ.items()
        if key not in {
            "CIAO_WORKSPACE", "CIAO_WORKSPACES", "CIAO_RUNTIME_ROOT",
            "CIAO_VAULT_ROOT", "CIAO_MEMORY_DIR", "CIAO_BOOTSTRAP_WORKSPACE",
        }
    }
    return subprocess.run(
        ["sh", str(harness["script"]), *args],
        env={**inherited_env, **harness["env"], **(env or {})},
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )


def _existing_install(
    harness: dict[str, Any], root: Path | None = None, *, port: str = "8443"
) -> Path:
    """Make the machine look like a configured Ciaobot workspace.

    The installer takes the CLI setup path only for a Mac that already has one;
    a machine with no configured workspace is onboarded in the browser instead.
    """
    workspace = root or harness["home"] / "Ciaobot"
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
    (workspace / ".env").write_text(f"PWA_PORT={port}\n", encoding="utf-8")
    return workspace


def _log(harness: dict[str, Any], name: str) -> str:
    path: Path = harness["home"] / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


@runs_the_sh_installer
@pytest.mark.parametrize("explicit_source", [None, "harness", "call"])
def test_installer_subprocess_isolates_inherited_storage_redirects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_source: str | None
) -> None:
    redirects = (
        "CIAO_WORKSPACE", "CIAO_WORKSPACES", "CIAO_RUNTIME_ROOT",
        "CIAO_VAULT_ROOT", "CIAO_MEMORY_DIR", "CIAO_BOOTSTRAP_WORKSPACE",
    )
    for key in redirects:
        monkeypatch.setenv(key, "inherited-from-another-workspace")
    monkeypatch.setenv("INSTALLER_TEST_SENTINEL", "keep-unrelated-environment")
    script = tmp_path / "environment.sh"
    script.write_text(
        f"exec {shlex.quote(sys.executable)} -c "
        "'import json, os; print(json.dumps(dict(os.environ)))'\n",
        encoding="utf-8",
    )
    explicit = {key: "deliberately-supplied" for key in redirects}
    harness = {
        "script": script,
        "env": explicit if explicit_source == "harness" else {},
    }

    result = _run_installer(
        harness, env=explicit if explicit_source == "call" else None
    )

    assert result.returncode == 0, result.stderr
    child_env = json.loads(result.stdout)
    for key in redirects:
        if explicit_source is None:
            assert key not in child_env
        else:
            assert child_env[key] == "deliberately-supplied"
        assert os.environ[key] == "inherited-from-another-workspace"
    assert child_env["INSTALLER_TEST_SENTINEL"] == "keep-unrelated-environment"


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_opens_the_bootstrap_without_a_suggestion(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    calls = _log(harness, "ciao-calls.log").splitlines()
    bootstrap = next(line for line in calls if line.startswith("run --installer-bootstrap"))
    assert "--installer-suggest-workspace" not in bootstrap
    assert "Ciaobot is running from the folder you chose." in result.stdout
    assert f"  workspace: {harness['home'] / 'Ciaobot'}" in result.stdout
    assert "/api/startup-status" in _log(harness, "trace.log")


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_consumes_actual_cli_activation_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from argparse import Namespace

    from ciao import cli, installer_handoff, macos_service
    from ciao.service_backend import MacOSBackend

    harness = _harness(tmp_path)
    home = harness["home"]
    _capture_browser_opens(harness)
    result = installer_handoff.HandoffResult(
        run_id="11111111-2222-3333-4444-555555555555",
        startup_id="aaaaaaaabbbb4ccc8dddeeeeeeeeeeee",
        workspace=home / "Chosen notes",
        service_backend=MacOSBackend.name,
        definition_path=home / "Library/LaunchAgents" / f"{macos_service.SERVER_LABEL}.plist",
        port=18446,
    )

    # Only activation's OS/service side effects are replaced. The CLI emits
    # the actual HandoffResult schema consumed by the shell installer.
    def activate(*, run_id: str) -> installer_handoff.HandoffResult:
        assert run_id == result.run_id
        return result

    monkeypatch.setattr(installer_handoff, "activate", activate)
    assert cli._installer_command(Namespace(as_json=True, run_id=result.run_id)) == 0
    emitted = capsys.readouterr().out
    (home / "installer-activation-output").write_text(emitted, encoding="utf-8")
    (home / "engine-port").write_text(str(result.port), encoding="utf-8")

    installed = _run_installer(harness, "--version", VERSION)

    assert installed.returncode == 0, installed.stderr
    assert f"  workspace: {result.workspace}" in installed.stdout
    assert "Ciaobot is running from the folder you chose." in installed.stdout
    assert _log(harness, "open.log") == "http://localhost:18446\n"


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("port", [8443, 18446])
def test_fresh_install_uses_the_verified_activation_port(tmp_path: Path, port: int) -> None:
    harness = _harness(tmp_path)
    home = harness["home"]
    (home / "engine-port").write_text(str(port), encoding="utf-8")
    # Simulate an interactive terminal without launching the host's browser.
    script = harness["script"]
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            'if [ -t 1 ] && command -v open', 'if command -v open'
        ),
        encoding="utf-8",
    )
    opener = harness["fakebin"] / "open"
    opener.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/open.log"\n', encoding="utf-8"
    )
    opener.chmod(0o755)
    (home / "installer-activation-output").write_text(
        json.dumps({
            "ok": True,
            "workspace": str(home / "Chosen notes"),
            "service_backend": "macos",
            "definition": str(home / "Library/LaunchAgents/com.ciao.server.plist"),
            "port": port,
        }),
        encoding="utf-8",
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert f"  workspace: {home / 'Chosen notes'}" in result.stdout
    assert f"http://localhost:{port}/api/startup-status" in _log(harness, "trace.log")
    assert f"open http://localhost:{port} and log in" in result.stdout
    assert _log(harness, "open.log") == f"http://localhost:{port}\n"
    assert (
        "installer activate --run-id 11111111-2222-3333-4444-555555555555 --json"
    ) in _log(harness, "ciao-calls.log")
    if port != 8443:
        assert "http://localhost:8443" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("output", [
    "", "not json", "[]", "null",
    '{"ok":false,"port":18446}',
    '{"ok":1,"port":18446}',
    '{"ok":true}',
    '{"ok":true,"port":"18446"}',
    '{"ok":true,"port":true}',
    '{"ok":true,"port":18446.0}',
    '{"ok":true,"port":0}',
    '{"ok":true,"port":65536}',
    '{"ok":true,"port":8443,"port":18446}',
    '{"ok":true,"port":"18446; touch /untrusted"}',
])
def test_fresh_install_refuses_malformed_activation_output(
    tmp_path: Path, output: str
) -> None:
    harness = _harness(tmp_path)
    # Keep all unrelated required fields valid so each port/status case tests
    # its own validation, including the duplicate-key case (no JSON re-encode).
    if output.startswith('{"ok"'):
        output = output.replace(
            '{', '{"workspace":"/chosen-notes","service_backend":"macos",'
            '"definition":"/LaunchAgents/com.ciao.server.plist",', 1,
        )
    (harness["home"] / "installer-activation-output").write_text(output, encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "could not verify the background service activation result" in result.stderr
    ciao = harness["home"] / ".local/bin/ciao"
    assert f"Resume with: '{ciao}' installer activate --run-id " in result.stderr
    assert "Return to the browser tab" not in result.stdout
    assert "http://localhost:" not in result.stdout
    assert _install_receipt(harness)["version"] == VERSION


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(("field", "value"), [
    ("workspace", None), ("workspace", "relative/notes"),
    ("workspace", "/notes\n18446"), ("workspace", "/notes\r"),
    ("definition", None), ("definition", "relative.plist"),
    ("service_backend", None), ("service_backend", "systemd"),
    ("service_backend", "launchd"), ("service_backend", "windows"),
])
def test_fresh_install_refuses_invalid_activation_workspace_schema(
    tmp_path: Path, field: str, value: Any
) -> None:
    harness = _harness(tmp_path)
    payload = {
        "ok": True, "workspace": str(harness["home"] / "Ciaobot"),
        "definition": str(harness["home"] / "Library/LaunchAgents/com.ciao.server.plist"),
        "service_backend": "macos", "port": 8443,
    }
    payload[field] = value
    (harness["home"] / "installer-activation-output").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "could not verify the background service activation result" in result.stderr
    assert "/api/startup-status" not in _log(harness, "trace.log")
    assert "Return to the browser tab" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    ("knob", "code"),
    [("cancel-installer-bootstrap", 130), ("fail-installer-bootstrap", 1)],
)
def test_fresh_install_cancel_leaves_a_resumable_install(
    tmp_path: Path, knob: str, code: int
) -> None:
    harness = _harness(tmp_path)
    workspace = harness["home"] / "Fresh"
    (harness["home"] / knob).write_text("", encoding="utf-8")

    result = _run_installer(
        harness, "--version", VERSION, "--workspace", str(workspace)
    )

    assert result.returncode == code
    ciao = harness["home"] / ".local/bin/ciao"
    assert f"Resume setup with: '{ciao}' run --installer-bootstrap" in result.stderr
    calls = _log(harness, "ciao-calls.log")
    assert "installer activate" not in calls
    assert "setup " not in calls
    assert not workspace.exists()
    assert _install_receipt(harness)["version"] == VERSION


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_absolutizes_a_relative_suggestion(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    home: Path = harness["home"]

    result = _run_installer(
        harness, "--version", VERSION, "--workspace", "Fresh", cwd=home
    )

    assert result.returncode == 0, result.stderr
    calls = _log(harness, "ciao-calls.log").splitlines()
    bootstrap = next(line for line in calls if line.startswith("run --installer-bootstrap"))
    assert f"--installer-suggest-workspace={home}/Fresh" in bootstrap
    assert f"bootstrap {home}/Fresh existed=0" in _log(harness, "bootstrap.log")


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_guard_refusal_is_reported(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    (harness["home"] / "refuse-installer-bootstrap").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 64
    assert "not fresh" in result.stderr
    assert "installer activate" not in _log(harness, "ciao-calls.log")
    ciao = harness["home"] / ".local/bin/ciao"
    assert f"Resume setup with: '{ciao}' run --installer-bootstrap" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_no_start_provisions_nothing(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    workspace = harness["home"] / "Fresh"

    result = _run_installer(
        harness, "--version", VERSION, "--workspace", str(workspace), "--no-start"
    )

    assert result.returncode == 0, result.stderr
    ciao = harness["home"] / ".local/bin/ciao"
    assert f"Setup was not started. Resume with: '{ciao}' run --installer-bootstrap" in result.stdout
    calls = _log(harness, "ciao-calls.log")
    assert "run --installer-bootstrap" not in calls
    assert "installer activate" not in calls
    assert "setup " not in calls
    assert not workspace.exists()


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_activation_failure_is_reported(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    (harness["home"] / "fail-installer-activate").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "the background service did not start" in result.stderr
    assert "activation refused" in result.stderr
    ciao = harness["home"] / ".local/bin/ciao"
    assert (
        f"Resume with: '{ciao}' installer activate --run-id "
        "11111111-2222-3333-4444-555555555555"
    ) in result.stderr
    assert _install_receipt(harness)["version"] == VERSION


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_warns_on_a_held_default_port_and_accepts_advanced_port(tmp_path: Path) -> None:
    """A foreign default-port listener must not veto Advanced setup."""
    harness = _harness(tmp_path)
    home = harness["home"]
    (home / "engine-port").write_text("18446", encoding="utf-8")
    (home / "installer-activation-output").write_text(json.dumps({
        "ok": True, "workspace": str(home / "Ciaobot"),
        "service_backend": "macos",
        "definition": str(home / "Library/LaunchAgents/com.ciao.server.plist"),
        "port": 18446,
    }), encoding="utf-8")
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("0.0.0.0", harness["fresh_port"]))
    holder.listen(1)
    try:
        result = _run_installer(harness, "--version", VERSION)
    finally:
        holder.close()

    assert result.returncode == 0, result.stderr
    assert f"port {harness['fresh_port']} is already in use" in result.stderr
    assert "Choose another port in Advanced setup" in result.stderr
    calls = _log(harness, "ciao-calls.log")
    assert "run --installer-bootstrap" in calls
    assert "installer activate" in calls
    assert "Ciaobot is running from the folder you chose." in result.stdout
    assert "open http://localhost:18446 and log in" in result.stdout
    assert "http://localhost:18446/api/startup-status" in _log(harness, "trace.log")


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_reports_an_engine_that_could_not_bind(tmp_path: Path) -> None:
    """Activation succeeding is not the engine running: a bind failure after
    the wizard is reported instead of sending the user back to the browser."""
    harness = _harness(tmp_path)
    (harness["home"] / "startup-port-in-use").write_text("8443", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1, result.stderr
    assert "port 8443 is already in use" in result.stderr
    assert "Return to the browser tab" not in result.stdout
    assert "Setup is saved in the folder you chose, but Ciaobot is not answering yet." in result.stdout


def _startup_payload() -> dict[str, Any]:
    return {
        "phases": [{
            "name": "server_starting", "status": "done", "message": "",
            "started_at": "2026-10-25T12:00:00+00:00",
            "finished_at": "2026-10-25T12:00:01+00:00",
        }],
        "overall_ready": True, "port_in_use": False, "version": VERSION,
        "desktop_api_version": 1,
        "startup_id": "aaaaaaaabbbb4ccc8dddeeeeeeeeeeee", "pid": 123,
        "latest_version": None, "update_available": False,
    }


def _capture_browser_opens(harness: dict[str, Any]) -> None:
    script = harness["script"]
    script.write_text(script.read_text(encoding="utf-8").replace(
        'if [ -t 1 ] && command -v open', 'if command -v open'
    ), encoding="utf-8")
    _write_exec(
        harness["fakebin"] / "open",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/open.log"\n',
    )


@runs_the_sh_installer
@needs_local_tools
def test_fresh_install_accepts_actual_locked_engine_startup_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    import ciao
    from ciao.instance_lock import WorkspaceInstanceLock
    from ciao.main import StartupTracker
    from ciao.port_in_use import BIND_PHASE
    from ciao.web import routes_api

    harness = _harness(tmp_path)
    _capture_browser_opens(harness)
    # This harness installs a simulated release; let the actual endpoint emit
    # that version, without performing an external update check.
    monkeypatch.setattr(ciao, "__version__", VERSION)

    async def update_hint(_request: Any) -> tuple[str, bool]:
        return VERSION, False

    monkeypatch.setattr(routes_api, "_cached_update_hint", update_hint)
    with WorkspaceInstanceLock(
        tmp_path / "locked-runtime", workspace_root=harness["home"] / "Ciaobot", port=8443
    ) as lock:
        tracker = StartupTracker(startup_id=lock.startup_id, engine_pid=os.getpid())
        tracker.start(BIND_PHASE)
        tracker.done(BIND_PHASE)
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(startup_tracker=tracker)
        ))
        response = asyncio.run(routes_api.startup_status_endpoint(request))
        payload = json.loads(response.body)
        assert payload["startup_id"] == lock.startup_id
        assert re.fullmatch(r"[0-9a-f]{32}", payload["startup_id"])
        (harness["home"] / "startup-response").write_bytes(response.body)

        result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert _log(harness, "startup-response-count").strip() == "1"
    assert "Ciaobot is running from the folder you chose." in result.stdout
    assert _log(harness, "open.log") == "http://localhost:8443\n"


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("response", [
    "<html>unrelated server</html>", "{}", "[]", "null", "not json",
    json.dumps(_startup_payload())[:-1] + ',"version":"foreign"}',
    pytest.param(json.dumps(_startup_payload()) + "\x00", id="embedded-nul"),
    pytest.param(" " * 65537 + json.dumps(_startup_payload()), id="oversized"),
])
def test_fresh_install_rejects_foreign_http_200_before_delayed_bind_failure(
    tmp_path: Path, response: str
) -> None:
    harness = _harness(tmp_path)
    _capture_browser_opens(harness)
    (harness["home"] / "startup-response").write_text(response, encoding="utf-8")
    # The foreign server answers first; only after the first health poll does
    # the engine supervisor publish its bind failure. No real sleep or service.
    _write_exec(harness["fakebin"] / "sleep", f'''#!/bin/sh
if [ -f "$HOME/startup-response-count" ]; then
    {shlex.quote(sys.executable)} - <<'PY'
import os
import pathlib
import time
path = pathlib.Path.home() / ".local/state/ciaobot/port-in-use.txt"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("port 8443 is already in use")
os.utime(path, (time.time() + 2, time.time() + 2))
PY
fi
''')

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "port 8443 is already in use" in result.stderr
    assert "Ciaobot is running" not in result.stdout
    assert "Return to the browser tab" not in result.stdout
    assert _log(harness, "open.log") == ""


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(("field", "value"), [
    ("version", "0.0.1"), ("overall_ready", 1), ("overall_ready", False),
    ("port_in_use", 0), ("desktop_api_version", True),
    ("desktop_api_version", 2), ("startup_id", "not-a-uuid"),
    ("startup_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    ("startup_id", "AAAAAAAABBBB4CCC8DDDEEEEEEEEEEEE"),
    ("pid", True), ("pid", 0), ("phases", []),
    ("phases", [{**_startup_payload()["phases"][0], "status": "in_progress"}]),
    ("phases", [{**_startup_payload()["phases"][0], "message": 42}]),
])
def test_fresh_install_requires_real_bound_ready_startup_schema(
    tmp_path: Path, field: str, value: Any
) -> None:
    harness = _harness(tmp_path)
    _capture_browser_opens(harness)
    payload = _startup_payload()
    payload[field] = value
    (harness["home"] / "startup-response").write_text(json.dumps(payload), encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "Ciaobot is running" not in result.stdout
    assert "Return to the browser tab" not in result.stdout
    assert _log(harness, "open.log") == ""


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("pending_state", ["bind", "overall"])
def test_fresh_install_waits_for_bind_readiness_before_opening(
    tmp_path: Path, pending_state: str
) -> None:
    harness = _harness(tmp_path)
    _capture_browser_opens(harness)
    pending = _startup_payload()
    if pending_state == "bind":
        # overall_ready alone is not proof of a bound server.
        pending["phases"][0]["status"] = "in_progress"
        pending["phases"][0]["finished_at"] = None
    else:
        pending["overall_ready"] = False
    (harness["home"] / "startup-first-response").write_text(json.dumps(pending), encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert _log(harness, "startup-response-count").strip() == "2"
    assert "Ciaobot is running from the folder you chose." in result.stdout
    assert _log(harness, "open.log") == "http://localhost:8443\n"
    assert "--max-time 5 --max-filesize 65536" in _log(harness, "trace.log")


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("curl_exit", [28, 63])
def test_fresh_install_discards_partial_status_after_curl_limit_failure(
    tmp_path: Path, curl_exit: int
) -> None:
    harness = _harness(tmp_path)
    _capture_browser_opens(harness)
    (harness["home"] / "startup-response").write_text(
        json.dumps(_startup_payload()), encoding="utf-8"
    )
    (harness["home"] / "startup-response-exit").write_text(str(curl_exit), encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "Ciaobot is running" not in result.stdout
    assert _log(harness, "open.log") == ""
    assert "--max-time 5 --max-filesize 65536" in _log(harness, "trace.log")


WELCOME_LINE = (
    "Your Ciaobot password was created for you. Ciaobot shows it once on Home "
    "on this computer: save it in your password manager."
)


@runs_the_sh_installer
@needs_local_tools
def test_installer_update_does_not_announce_an_existing_password(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    workspace = _existing_install(harness)
    env_path = workspace / ".env"
    env_path.write_text("PWA_PORT=8443\nPWA_AUTH_TOKEN=synthetic-existing-password\n", encoding="utf-8")
    marker = workspace / ".runtime" / "welcome-pending"
    marker.write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")
    original_env = env_path.read_bytes()
    original_marker = marker.read_bytes()

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert env_path.read_bytes() == original_env
    assert marker.read_bytes() == original_marker
    assert WELCOME_LINE not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_installer_says_where_the_generated_password_is_shown(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _existing_install(harness)
    home: Path = harness["home"]
    (home / "setup-generates-password").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert WELCOME_LINE in result.stdout
    assert f"  workspace: {home / 'Ciaobot'}" in result.stdout


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("generated", id="new-password"),
        pytest.param("generated-memory-warning", id="new-password-with-tolerated-exit-3"),
        pytest.param("stale-marker", id="update-with-stale-marker"),
        pytest.param("no-marker", id="update-without-marker"),
        pytest.param("literal-path", id="literal-path-with-spaces-and-metacharacters"),
    ],
)
def test_installer_password_announcement_tracks_this_setup(
    tmp_path: Path, case: str
) -> None:
    # The announcement is about this run, not about a marker an older install
    # left behind: only setup's own written-files output can say whether a
    # password was generated now (#2162).
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    args = ["--version", VERSION, "--no-start"]
    password_generated = case in {
        "generated",
        "generated-memory-warning",
        "literal-path",
    }
    if case == "literal-path":
        # A literal full-line match, not a pattern: these are real characters
        # in a real folder name.
        workspace = _existing_install(harness, home / "Ciaobot [draft] * (copy)")
        args += ["--workspace", str(workspace)]
    else:
        workspace = _existing_install(harness)
    marker = workspace / ".runtime" / "welcome-pending"

    if password_generated:
        (home / "setup-generates-password").write_text("", encoding="utf-8")
    else:
        # An update keeps the password it already has, and an unacknowledged
        # marker from the run that generated it can still be on disk.
        (workspace / ".env").write_text(
            "PWA_PORT=8443\nPWA_AUTH_TOKEN=synthetic-existing-password\n",
            encoding="utf-8",
        )
        if case == "stale-marker":
            marker.write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")
    if case == "generated-memory-warning":
        # Setup generated the password, then reported memory regions it could
        # not scaffold; exit 3 is tolerated and the password still exists.
        (home / "fail-ciao-setup-memory").write_text("", encoding="utf-8")

    result = _run_installer(harness, *args)

    assert result.returncode == 0, result.stderr
    if password_generated:
        assert WELCOME_LINE in result.stdout
        assert marker.is_file()
    else:
        assert WELCOME_LINE not in result.stdout
        if case == "stale-marker":
            # The marker the earlier run left is untouched, and this run does
            # not mistake it for a password it created.
            assert marker.read_text(encoding="utf-8") == "2026-01-01T00:00:00+00:00\n"
        else:
            assert not marker.exists()


@runs_the_sh_installer
@needs_local_tools
def test_installer_ready_update_keeps_sign_in_link_without_password_announcement(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    workspace = _existing_install(harness)
    (workspace / ".env").write_text(
        "PWA_PORT=8443\nPWA_AUTH_TOKEN=synthetic-existing-password\n",
        encoding="utf-8",
    )
    marker = workspace / ".runtime" / "welcome-pending"
    marker.write_text("2026-01-01T00:00:00+00:00\n", encoding="utf-8")
    original_marker = marker.read_bytes()

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "Open Ciaobot: http://localhost:8443/?setup=tok" in result.stdout
    assert WELCOME_LINE not in result.stdout
    assert marker.read_bytes() == original_marker


@runs_the_sh_installer
@needs_local_tools
def test_installer_failed_setup_never_announces_a_password(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _existing_install(harness)
    home: Path = harness["home"]
    (home / "setup-generates-password").write_text("", encoding="utf-8")
    (home / "fail-ciao-setup").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode != 0
    assert "ciao setup failed" in result.stderr
    assert WELCOME_LINE not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_installer_failed_setup_after_writing_password_stays_quiet(
    tmp_path: Path,
) -> None:
    # Setup can write the password and its welcome marker, then fail before the
    # workspace is complete. The install fails, so nothing may claim a password
    # was created - and setup's captured stdout can carry a sign-in credential,
    # so a hard failure must never echo it either (#2162).
    harness = _harness(tmp_path)
    workspace = _existing_install(harness)
    home: Path = harness["home"]
    (home / "fail-ciao-setup-after-password").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "ciao setup failed" in result.stderr
    assert WELCOME_LINE not in result.stdout
    assert WELCOME_LINE not in result.stderr
    assert SETUP_STDOUT_SENTINEL not in result.stdout
    assert SETUP_STDOUT_SENTINEL not in result.stderr
    # The marker really was written before the failure, so the old
    # marker-existence check would have had something to misread.
    assert (workspace / ".runtime" / "welcome-pending").is_file()


@runs_the_sh_installer
@needs_local_tools
def test_default_zsh_path_update_is_marked_and_idempotent(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    startup = harness["home"] / ".zshrc"
    startup.write_text("# keep me\n", encoding="utf-8")
    env = {"SHELL": "/bin/zsh"}

    first = _run_installer(harness, "--version", VERSION, "--no-start", env=env)
    second = _run_installer(harness, "--version", VERSION, "--no-start", env=env)

    stanza = '# >>> Ciaobot installer PATH >>>'
    assert first.returncode == second.returncode == 0
    assert startup.read_text(encoding="utf-8").count(stanza) == 1
    assert f"export PATH='{harness['home']}/.local/bin':\"$PATH\"" in startup.read_text(encoding="utf-8")
    assert "PATH entry is already present" in second.stdout

    sourced = subprocess.run(
        ["sh", "-c", '. "$1"; . "$1"; . "$1"; printf %s "$PATH"', "sh", str(startup)],
        env={**os.environ, "HOME": str(harness["home"]), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert sourced.returncode == 0, sourced.stderr
    assert sourced.stdout.split(":").count(str(harness["home"] / ".local/bin")) == 1


@runs_the_sh_installer
@needs_local_tools
def test_dangling_zsh_profile_symlink_is_not_followed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    startup = harness["home"] / ".zshrc"
    target = harness["home"] / "missing-zsh-profile"
    startup.symlink_to(target)

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", env={"SHELL": "/bin/zsh"}
    )

    assert result.returncode == 0, result.stderr
    assert startup.is_symlink()
    assert not target.exists()
    assert "not a safe writable file" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_installer_does_not_edit_profile_when_bin_dir_is_already_on_path(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    startup = harness["home"] / ".zshrc"
    original = "# user config\n"
    startup.write_text(original, encoding="utf-8")

    result = _run_installer(
        harness, "--version", VERSION, "--no-start",
        env={
            "SHELL": "/bin/zsh",
            "PATH": f"{harness['fakebin']}:{harness['home']}/.local/bin:/usr/bin:/bin",
        },
    )

    assert result.returncode == 0, result.stderr
    assert startup.read_text(encoding="utf-8") == original
    assert "already on PATH" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_no_modify_path_leaves_all_shell_profiles_untouched(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    startup = harness["home"] / ".zshrc"
    original = "# user content\n"
    startup.write_text(original, encoding="utf-8")

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", "--no-modify-path",
        env={"SHELL": "/bin/zsh"},
    )

    assert result.returncode == 0, result.stderr
    assert startup.read_text(encoding="utf-8") == original
    assert f"export PATH='{harness['home']}/.local/bin':\"$PATH\"" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_unknown_shell_does_not_receive_a_posix_profile_edit(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", env={"SHELL": "/bin/unknown"}
    )

    assert result.returncode == 0, result.stderr
    assert not list(harness["home"].glob(".*rc"))
    assert "could not update PATH automatically" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_zdotdir_controls_the_zsh_startup_file(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    zdotdir = harness["home"] / "zsh config"
    zdotdir.mkdir()

    result = _run_installer(
        harness, "--version", VERSION, "--no-start",
        env={"SHELL": "/bin/zsh", "ZDOTDIR": str(zdotdir)},
    )

    assert result.returncode == 0, result.stderr
    assert (zdotdir / ".zshrc").is_file()
    assert not (harness["home"] / ".zshrc").exists()
    assert f"'{harness['home']}/.local/bin'" in (zdotdir / ".zshrc").read_text()


@runs_the_sh_installer
@needs_local_tools
def test_bash_login_path_update_uses_effective_login_profile(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    profile = harness["home"] / ".bash_login"
    profile.write_text("# login settings\n", encoding="utf-8")
    (harness["home"] / ".profile").write_text("# lower precedence\n", encoding="utf-8")

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", env={"SHELL": "/bin/bash"}
    )

    assert result.returncode == 0, result.stderr
    assert "# >>> Ciaobot installer PATH >>>" in profile.read_text(encoding="utf-8")
    assert "Ciaobot installer PATH >>>" not in (harness["home"] / ".profile").read_text()


@runs_the_sh_installer
@needs_local_tools
def test_fish_path_update_uses_fish_syntax(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", env={"SHELL": "/usr/local/bin/fish"}
    )

    fish_config = harness["home"] / ".config/fish/config.fish"
    assert result.returncode == 0, result.stderr
    assert "fish_add_path '" in fish_config.read_text(encoding="utf-8")
    assert "For this terminal run: fish_add_path '" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_custom_uv_bin_directory_is_used_in_path_guidance(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    custom_bin = harness["home"] / "custom tools"
    uv = harness["fakebin"] / "uv"
    uv_source = uv.read_text(encoding="utf-8").replace(
        "$HOME/.local/bin", "$HOME/custom tools"
    ).replace(
        'ln -s ../share/uv/tools/ciaobot/bin/ciao "$HOME/custom tools/ciao"',
        'ln -s "$HOME/.local/share/uv/tools/ciaobot/bin/ciao" "$HOME/custom tools/ciao"',
    )
    uv.write_text(
        uv_source,
        encoding="utf-8",
    )

    result = _run_installer(
        harness, "--version", VERSION, "--no-start", "--no-modify-path",
        env={"SHELL": "/bin/zsh"},
    )

    assert result.returncode == 0, result.stderr
    assert f"'{custom_bin}':\"$PATH\"" in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_start_failure_hint_quotes_ciao_path_with_spaces(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _existing_install(harness)
    custom_bin = harness["home"] / "custom tools"
    uv = harness["fakebin"] / "uv"
    uv_source = uv.read_text(encoding="utf-8").replace(
        "$HOME/.local/bin", "$HOME/custom tools"
    ).replace(
        'ln -s ../share/uv/tools/ciaobot/bin/ciao "$HOME/custom tools/ciao"',
        'ln -s "$HOME/.local/share/uv/tools/ciaobot/bin/ciao" "$HOME/custom tools/ciao"',
    )
    uv.write_text(uv_source, encoding="utf-8")
    (harness["home"] / "fail-ciao-service-start").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert f"try: '{custom_bin}/ciao' service status" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_installer_is_quiet_about_a_password_it_did_not_generate(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert WELCOME_LINE not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_engine_installer_end_to_end_with_fakes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _existing_install(harness)
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


def test_verification_precedes_host_acquisition() -> None:
    # The host archive's digest is checked against the signed entry, and the
    # install runs only after the wheel's own digest and size have been checked.
    wheel_verify = SCRIPT_TEXT.index("downloaded wheel does not match the signed manifest")
    host_verify = SCRIPT_TEXT.index(
        "downloaded server host does not match the signed manifest"
    )

    assert wheel_verify < host_verify


def test_host_acquisition_follows_the_read_only_preflight() -> None:
    # A preflight refusal (a bad --as-client, an untrusted receipt, a foreign
    # `ciao` on PATH) must still leave this Mac untouched, so the host is only
    # touched after every read-only check, and before the migration changes
    # anything. That includes recovering a previous run's abandoned upgrade: it
    # moves the host, so it happens in the same window.
    preflight = SCRIPT_TEXT.index("exists and was not installed by Ciaobot")
    recover = SCRIPT_TEXT.index("    native_host_recover\n")
    host_install = SCRIPT_TEXT.index("        native_host_run install \\")
    migration = SCRIPT_TEXT.index("# --- the migration ---")

    assert preflight < recover < host_install < migration


@runs_the_sh_installer
@needs_local_tools
def test_a_wheel_only_release_never_touches_the_host(tmp_path: Path) -> None:
    # The historical shape: no server-host entry, so nothing is downloaded and
    # the install command is never reached. The engine installs exactly as it
    # did before the host existed.
    harness = _harness(tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "tool install" in _log(harness, "uv-calls.log")
    assert _log(harness, "server-host-install.log") == ""
    assert "Ciaobot Server host installed and recorded." not in result.stdout
    assert SERVER_HOST_FILENAME not in _log(harness, "trace.log")


@runs_the_sh_installer
@needs_local_tools
def test_a_first_host_install_does_not_warn_about_permissions(tmp_path: Path) -> None:
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "Ciaobot Server host installed and recorded." in result.stdout
    assert "Accessibility" not in result.stderr


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize("revision", sorted(SUPERSEDED_HOST_PROTOCOLS))
def test_replacing_a_superseded_host_warns_about_permissions(
    tmp_path: Path, revision: int
) -> None:
    # A superseded host is replaced by the new one (#1659). The replacement
    # changes the signed bytes macOS granted Accessibility/Automation to, so the
    # user is told once that macOS will ask again.
    #
    # The revision is the constant, not a literal: a copy of the number here
    # would stop matching on the next revision bump, and the warning is exactly
    # what a user needs then.

    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    _write_host_plist(harness["home"], str(revision))

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "Ciaobot Server host installed and recorded." in result.stdout
    assert "Accessibility" in result.stderr
    assert "Automation" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_a_current_revision_host_does_not_warn_about_permissions(
    tmp_path: Path,
) -> None:
    # A host that is already the current revision is not replaced, so nothing
    # changed and nothing is warned about.
    from ciao.server_host import HOST_REVISION

    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    _write_host_plist(harness["home"], str(HOST_REVISION))

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "Ciaobot Server host installed and recorded." in result.stdout
    assert "Accessibility" not in result.stderr


def _write_host_plist(home: Path, revision: str) -> Path:
    """A host bundle's Info.plist, with the given host revision."""
    installed = home / "Applications" / "Ciaobot Server.app" / "Contents"
    installed.mkdir(parents=True, exist_ok=True)
    plist = installed / "Info.plist"
    with plist.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "local.ciaobot.server",
                "CFBundleExecutable": "Ciaobot Server",
                "CFBundleVersion": revision,
                "CFBundleShortVersionString": revision,
            },
            handle,
        )
    return plist


@runs_the_sh_installer
@needs_local_tools
def test_a_host_release_verifies_and_installs_the_host(tmp_path: Path) -> None:
    # A signed server-host entry: the archive is downloaded from the release
    # under the manifest's name, and the verified wheel's module is asked to
    # install it. The install command is reached only after the wheel and host
    # digests both matched.
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    install_log = _log(harness, "server-host-install.log")
    assert "-I -m ciao.server_host_install install" in install_log
    assert str(harness["release"] / SERVER_HOST_FILENAME) in _log(harness, "trace.log")
    assert "Ciaobot Server host installed and recorded." in result.stdout
    # The host is inert: no launchd, no plist and no service start for it.
    assert "launchctl" not in _log(harness, "launchctl.log")


@runs_the_sh_installer
@needs_local_tools
def test_a_tampered_host_archive_is_refused_before_install(tmp_path: Path) -> None:
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    (harness["release"] / SERVER_HOST_FILENAME).write_bytes(b"nobody signed this")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "downloaded server host does not match the signed manifest" in result.stderr
    assert _log(harness, "server-host-install.log") == ""


@runs_the_sh_installer
@needs_local_tools
def test_an_older_wheel_without_the_module_installs_the_engine(tmp_path: Path) -> None:
    # A wheel cut before the host work has no `ciao.server_host_install`: the
    # probe must fail open and the engine must install exactly as before, not
    # abort on "No module named".
    harness = _harness(tmp_path)
    (harness["home"] / "no-server-host-module").write_text("")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "tool install" in _log(harness, "uv-calls.log")
    assert _log(harness, "server-host-install.log") == ""
    assert "Ciaobot Server host installed and recorded." not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_a_failed_host_install_stops_the_engine_install(tmp_path: Path) -> None:
    # The host is acquired before the engine install, so a host failure stops
    # the run visibly rather than being silently skipped.
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    (harness["home"] / "fail-server-host-install").write_text("")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "the signed server host could not be installed" in result.stderr
    assert "Ciaobot Server host installed and recorded." not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_an_existing_unowned_host_warns_and_the_engine_still_installs(
    tmp_path: Path,
) -> None:
    # Exit 3 is the module leaving a target it cannot prove it owns untouched
    # (a bundle orphaned by a killed run, a copied one). That is not a release
    # that failed to verify, so it must not block the engine install - or every
    # later update - until the user deletes the bundle by hand.
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    (harness["home"] / "existing-server-host").write_text("")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "the Ciaobot Server host was not installed" in result.stderr
    assert "the engine install continues" in result.stderr
    assert "Ciaobot Server host installed and recorded." not in result.stdout
    assert "tool install" in _log(harness, "uv-calls.log")


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "knob",
    ["warn-inspection-server-host", "warn-install-server-host"],
)
def test_a_non_security_host_failure_warns_and_the_engine_installs(
    tmp_path: Path, knob: str
) -> None:
    # By the time `install` runs, the shell has already verified the manifest
    # signature and the archive's sha256/size, so an INSTALL_FAILED (disk full,
    # chmod EPERM) or an INSPECTION_FAILED (a codesign probe that timed out or
    # parsed differently on this macOS release) is local or a signed-build
    # defect, never attacker input. It must warn and let the engine install,
    # not abort the one-liner for an optional, inert host.
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)
    (harness["home"] / knob).write_text("")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "the Ciaobot Server host was not installed" in result.stderr
    assert "the engine install continues" in result.stderr
    assert "Ciaobot Server host installed and recorded." not in result.stdout
    assert "tool install" in _log(harness, "uv-calls.log")
    assert f"Ciaobot engine {VERSION} installed." in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_the_host_install_gets_the_verified_manifest_archive_and_embedded_key(
    tmp_path: Path,
) -> None:
    # The exact argv, not a substring: the module re-verifies the same signed
    # manifest with the same embedded key and installs the archive that was just
    # digest-checked, with no path or key override reaching it.
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    calls = _log(harness, "server-host-install.log").splitlines()
    assert len(calls) == 1
    argv = calls[0].split(" ")
    assert argv[:4] == ["-I", "-m", "ciao.server_host_install", "install"]
    options = dict(zip(argv[4::2], argv[5::2]))
    assert set(options) == {"--manifest", "--signature", "--archive", "--public-key"}
    assert options["--manifest"].endswith("/ciaobot-engine-manifest.json")
    assert options["--signature"].endswith("/ciaobot-engine-manifest.json.sig")
    assert Path(options["--archive"]).name == SERVER_HOST_FILENAME
    assert Path(options["--archive"]).parent == Path(options["--manifest"]).parent
    assert options["--public-key"] == harness["public_key"]


# ── the transactional signed-host upgrade (#2330) ───────────────────────────

_SIGNED_TEAM = "TESTTEAM01"


def _installed_host_record(home: Path) -> Path:
    """The ownership record of the legacy host the fake Mac already runs."""
    record = home / ".local" / "state" / "ciaobot" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text('{"schema": 1, "host_revision": 3}\n', encoding="utf-8")
    os.chmod(record, 0o600)
    build = home / "Applications" / "Ciaobot Server.app" / "Contents" / "build"
    build.parent.mkdir(parents=True, exist_ok=True)
    build.write_text("legacy\n", encoding="utf-8")
    return record


def _signed_host_harness(
    tmp_path: Path, *, installed_revision: int | None = 3
) -> dict[str, Any]:
    """A signed release whose host entry names a Developer ID team, over a legacy
    host that is already installed at ``installed_revision``.

    ``installed_revision=None`` installs no host at all. The fake module records a
    deferred upgrade (the journal the shell has to commit or roll back) instead of
    moving a real bundle, so what is asserted here is the shell's ordering:
    warning, install, health, commit or rollback.
    """
    host_archive = tmp_path / "host-build" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    harness = _harness(tmp_path, host_archive=host_archive, signed_team=_SIGNED_TEAM)
    if installed_revision is not None:
        _write_host_plist(harness["home"], str(installed_revision))
        # The app and record this Mac already has, plus the job launchd holds for
        # it: a real migration replaces those, and a rollback has to bring them
        # back before the old engine definition is reloaded.
        _installed_host_record(harness["home"])
        (harness["home"] / "loaded-com.ciao.server").write_text("")
    return harness


def _engine_definition(harness: dict[str, Any]) -> Path:
    """An ordinary installer-managed engine plist, so this Mac is not "fresh".

    The classifier reads it as an installer-managed engine (its program is the
    `ciao` console, not a `.app`), so a run with `--workspace` takes the CLI setup
    path rather than the browser bootstrap.
    """
    home: Path = harness["home"]
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / "com.ciao.server.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.ciao.server",
                "ProgramArguments": [str(home / ".local" / "bin" / "ciao"), "run"],
                "WorkingDirectory": str(home / "Ciaobot"),
            }
        )
    )
    return plist


def _defer(harness: dict[str, Any]) -> None:
    """Make the fake module answer a deferred upgrade with a pending journal."""
    (harness["home"] / "defer-server-host-install").write_text("")


def _trace_index(harness: dict[str, Any], needle: str) -> int:
    trace = _log(harness, "trace.log").splitlines()
    return trace.index(needle)


def _last_trace_index(harness: dict[str, Any], needle: str) -> int:
    trace = _log(harness, "trace.log").splitlines()
    return max(index for index, line in enumerate(trace) if line == needle)


def _last_health_index(harness: dict[str, Any]) -> int:
    trace = _log(harness, "trace.log").splitlines()
    return max(index for index, line in enumerate(trace) if "startup-status" in line)


@runs_the_sh_installer
@needs_local_tools
def test_signed_upgrade_warning_precedes_any_identity_change(tmp_path: Path) -> None:
    # A signed candidate over an older host is the replacement macOS evaluates a
    # new identity for. The user is told once, before anything is replaced, and
    # without a promise that existing grants carry over - a permission warning
    # printed after the swap is a permission warning printed too late.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert result.stderr.count("Accessibility") == 1
    assert "Automation" in result.stderr
    assert "Desktop, Documents and Downloads" in result.stderr
    assert "not transferred automatically" in result.stderr
    # In the shipped script the warning is emitted before the command that
    # replaces the host, not after it.
    assert SCRIPT_TEXT.index("Existing permissions are not transferred") < (
        SCRIPT_TEXT.index("        native_host_run install \\")
    )
    argv = _log(harness, "server-host-install.log").splitlines()[0].split(" ")
    assert "--defer-commit" in argv
    assert argv[argv.index("--owner-pid") + 1].isdigit()


@runs_the_sh_installer
@needs_local_tools
def test_a_fresh_or_current_host_never_warns_about_permissions(tmp_path: Path) -> None:
    # Nothing is being replaced, so nothing is warned about: no installed host at
    # all, and an installed host of the revision this release selects.
    from ciao.server_host import SIGNED_HOST_REVISION

    fresh = _signed_host_harness(tmp_path / "fresh", installed_revision=None)
    _existing_install(fresh)
    result = _run_installer(fresh, "--version", VERSION)
    assert result.returncode == 0, result.stderr
    assert "Accessibility" not in result.stderr

    current = _signed_host_harness(
        tmp_path / "current", installed_revision=SIGNED_HOST_REVISION
    )
    _existing_install(current)
    result = _run_installer(current, "--version", VERSION)
    assert result.returncode == 0, result.stderr
    assert "Accessibility" not in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_pending_native_upgrade_commits_only_after_exact_engine_health(
    tmp_path: Path,
) -> None:
    # An old engine answering on the configured port is not the engine this run
    # installed. Until the exact new version answers, the host transaction stays
    # uncommitted and a failure gives the previous host back.
    failing = _signed_host_harness(tmp_path / "failing")
    _defer(failing)
    _existing_install(failing)
    (failing["home"] / "startup-version").write_text("9.9.9\n")

    result = _run_installer(failing, "--version", VERSION)

    assert result.returncode != 0
    assert "rollback-upgrade" in _log(failing, "native-host-transaction.log")
    assert "commit-upgrade" not in _log(failing, "native-host-transaction.log")
    assert "with version" in result.stderr
    # The journal stayed pending for the whole health wait and was only released
    # by the rollback this failure caused.
    assert _last_health_index(failing) < _trace_index(failing, "native-host-rollback")
    assert (failing["home"] / "native-host-pending").exists() is False

    healthy = _signed_host_harness(tmp_path / "healthy")
    _defer(healthy)
    _existing_install(healthy)

    result = _run_installer(healthy, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "commit-upgrade" in _log(healthy, "native-host-transaction.log")
    assert "rollback-upgrade" not in _log(healthy, "native-host-transaction.log")
    # Committed once, and only after the exact version answered.
    assert _trace_index(healthy, "native-host-commit") > _last_health_index(healthy)
    assert _log(healthy, "trace.log").count("native-host-commit") == 1
    assert not (healthy["home"] / "native-host-pending").exists()
    # The responder really changed between the pre-start probe and the health
    # answer, and the one that passed is the engine run the runtime lock names.
    home = healthy["home"]
    responders = (home / "responders.log").read_text().splitlines()
    assert len(responders) >= 2
    assert responders[0] != responders[-1]
    lock = json.loads((home / "Ciaobot/.runtime/server.lock").read_text())
    assert responders[-1] == f"responder={lock['startup_id']}"
    # The launched host and the socket holder are distinct processes, as on a real
    # install: launchd runs `Ciaobot Server`, which posix_spawns Python.
    assert (home / "host-pid").read_text().strip() != (
        home / "engine-pid"
    ).read_text().strip()


@runs_the_sh_installer
@needs_local_tools
def test_native_upgrade_commits_after_desktop_retirement(tmp_path: Path) -> None:
    # A desktop hand-over can still roll the engine back while it retires the
    # app's agent, so the host is committed only after that succeeded - and the
    # commit is what releases the before-images.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    retirement = _trace_index(harness, "launchctl bootout gui/%d/Ciaobot" % os.getuid())
    assert _trace_index(harness, "native-host-commit") > retirement
    assert _migration_receipt(harness)["phase"] == "migrated"


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "knob,extra",
    [
        pytest.param("fail-ciao-setup", (), id="setup"),
        pytest.param("fail-ciao-service-start", (), id="start"),
        pytest.param("startup-port-in-use", ("8443",), id="port-conflict"),
        pytest.param("fail-launchctl-bootout-Ciaobot", (), id="retirement"),
        pytest.param("deny-workspace-access", (), id="folder-denial"),
    ],
)
def test_late_native_upgrade_failure_rolls_back_before_service_restore(
    tmp_path: Path, knob: str, extra: tuple[str, ...]
) -> None:
    # Every failure that lands after the host was replaced but before the new
    # engine is proven gives the previous host back - the observed app and record
    # bytes, not just a marker - and, in a hand-over, before the old engine
    # definition is reloaded.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _desktop_install(harness, tmp_path)
    (harness["home"] / knob).write_text("".join(extra))

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode != 0
    transcript = _log(harness, "native-host-transaction.log")
    assert "rollback-upgrade" in transcript
    assert transcript.count("rollback-upgrade") == 1
    assert "commit-upgrade" not in transcript
    # The previous app and its record are back, byte for byte, and the restore
    # event was observed.
    home = harness["home"]
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    assert (home / ".local/state/ciaobot/server-host.json").read_text() == (
        '{"schema": 1, "host_revision": 3}\n'
    )
    restored = _trace_index(harness, "native-host-restored")
    # The old engine definition is reloaded only after the host pair is back.
    reload_line = next(
        line
        for line in _log(harness, "trace.log").splitlines()
        if line.startswith("launchctl bootstrap")
        and line.endswith("com.ciao.server.plist")
    )
    trace = _log(harness, "trace.log").splitlines()
    assert restored < trace.index(reload_line)
    if knob == "deny-workspace-access":
        assert "denied access" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_health_timeout_with_a_pending_upgrade_rolls_back(tmp_path: Path) -> None:
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _desktop_install(harness, tmp_path)
    (harness["home"] / "startup-version").write_text("9.9.9\n")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode != 0
    assert "with version" in result.stderr
    assert "rollback-upgrade" in _log(harness, "native-host-transaction.log")
    assert _trace_index(harness, "native-host-rollback") < _last_trace_index(
        harness, "launchctl bootout gui/%d/com.ciao.server" % os.getuid()
    )


@runs_the_sh_installer
@needs_local_tools
def test_unexpected_exit_and_signal_roll_back_before_temp_cleanup(
    tmp_path: Path,
) -> None:
    # A signal ends the run without any `fail` call at all. The one EXIT handler
    # still gives the previous host back - exactly once - using the wheel that is
    # deleted only afterwards, and the original status survives.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    workspace = _desktop_install(harness, tmp_path)
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (harness["home"] / "interrupt-after-bootout-Ciaobot").write_text("")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 130
    transcript = _log(harness, "native-host-transaction.log").splitlines()
    assert len(transcript) == 1
    assert "rollback-upgrade" in transcript[0]
    # The rollback ran while the wheel still existed (a marker the fake module
    # writes only when that file is still on disk), and the temporary directory it
    # lives in is only deleted afterwards.
    trace = _log(harness, "trace.log").splitlines()
    assert "native-host-rollback" in trace
    assert trace.index("native-host-rollback") < trace.index("native-host-restored")
    assert "native-host-wheel-present" in trace
    assert not list(Path(harness["env"]["TMPDIR"]).glob("ciaobot-engine.*"))
    # The previous pair is back.
    home = harness["home"]
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_an_unexpected_set_e_exit_rolls_back_before_temp_cleanup(
    tmp_path: Path,
) -> None:
    # A command that fails under `set -e` - here a workspace that cannot be
    # created - ends the script without any `fail` call. The same EXIT handler
    # rolls back once, keeps the original status, and does it while the wheel
    # exists.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "notadir").write_text("not a directory\n")

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--workspace",
        str(harness["home"] / "notadir" / "Ciaobot"),
    )

    assert result.returncode == 1
    transcript = _log(harness, "native-host-transaction.log").splitlines()
    assert len(transcript) == 1
    assert "rollback-upgrade" in transcript[0]
    trace = _log(harness, "trace.log").splitlines()
    assert "native-host-wheel-present" in trace
    assert "native-host-restored" in trace
    assert not list(Path(harness["env"]["TMPDIR"]).glob("ciaobot-engine.*"))
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_a_signal_inside_the_install_call_still_binds_its_own_operation(
    tmp_path: Path,
) -> None:
    # The reviewed window: the child finished the durable install and the shell is
    # signalled before `native_host_begin` recorded anything. The EXIT handler
    # discovers the operation from the journal, proves it owns it (owner pid plus
    # nonce), and rolls it back instead of leaving it behind.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "interrupt-after-deferred-install").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 130
    transcript = _log(harness, "native-host-transaction.log").splitlines()
    assert len(transcript) == 1
    assert "rollback-upgrade" in transcript[0]
    assert not (harness["home"] / "native-host-pending").exists()
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_an_unreadable_status_retains_the_journal_and_reports_incomplete_recovery(
    tmp_path: Path,
) -> None:
    # Discovery itself fails: the status cannot be read, so the shell must not
    # guess. It keeps the journal, says the recovery was not made, and stays
    # nonzero - deleting the journal or rolling back blind would both be worse.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    (home / "unreadable-status-after-install").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "could not be read" in result.stderr
    assert "NOT rolled back" in result.stderr
    assert "server-host-upgrade.json" in result.stderr
    assert _log(harness, "native-host-transaction.log") == ""
    # The operation is still there for the next run.
    assert (home / "native-host-pending").exists()


@runs_the_sh_installer
@needs_local_tools
def test_an_indeterminate_launchctl_query_fails_closed(tmp_path: Path) -> None:
    # `launchctl print` failed for an operational reason (a permission error, a
    # broken tool), not because the job is gone. That is uncertainty, not proof, so
    # the identity is not reverted and the journal is kept.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    (home / "startup-version").write_text("9.9.9\n")
    (home / "launchctl-print-indeterminate").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was NOT replaced by the previous host" in result.stderr
    assert _log(harness, "native-host-transaction.log") == ""
    assert (home / "native-host-pending").exists()
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "candidate\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_a_confirmed_absent_job_still_lets_the_rollback_proceed(tmp_path: Path) -> None:
    # The contrast: launchd explicitly says it has no such job ("Could not find
    # service", status 113). That *is* proof, so the rollback runs and the previous
    # pair is restored.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    (home / "startup-version").write_text("9.9.9\n")
    (home / "loaded-com.ciao.server").unlink()

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    transcript = _log(harness, "native-host-transaction.log")
    assert transcript.count("rollback-upgrade") == 1
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    assert not (home / "native-host-pending").exists()


@runs_the_sh_installer
@needs_local_tools
def test_a_failing_status_query_in_the_exit_handler_keeps_the_status(
    tmp_path: Path,
) -> None:
    # The run is interrupted inside the install call, so the EXIT handler has to
    # discover its own operation - and the status query fails. Under `set -e` an
    # unguarded failure would abort the trap and lose the original status; the
    # journal must stay and the run must still report the interruption.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    (home / "interrupt-after-deferred-install").write_text("")
    (home / "unreadable-status-after-install").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 130
    assert "could not be read" in result.stderr
    assert "NOT rolled back" in result.stderr
    assert _log(harness, "native-host-transaction.log") == ""
    assert (home / "native-host-pending").exists()
    # The temporary directory was cleaned anyway, which is only reached when the
    # trap survived the failed discovery.
    assert not list(Path(harness["env"]["TMPDIR"]).glob("ciaobot-engine.*"))


@runs_the_sh_installer
@needs_local_tools
def test_a_failed_native_rollback_stops_the_service_restoration(
    tmp_path: Path,
) -> None:
    # The native rollback itself failed. Reloading the previous engine definition
    # now would put the old engine back on top of a candidate that may still be
    # running, over before-images that are the only way back. The run stops,
    # keeps the material and says the rollback was incomplete.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _desktop_install(harness, tmp_path)
    home = harness["home"]
    (home / "fail-native-host-rollback").write_text("")
    (home / "startup-version").write_text("9.9.9\n")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode != 0
    assert "could not be restored" in result.stderr
    assert "NOT reloaded" in result.stderr
    assert "incomplete rollback" in result.stderr
    # Exactly one attempt, and no engine definition was reloaded.
    assert _log(harness, "native-host-transaction.log").count("rollback-upgrade") == 1
    assert "bootstrap" not in _log(harness, "launchctl.log")
    # The candidate is still installed and the journal is still there to recover.
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "candidate\n"
    )
    assert (home / "native-host-pending").exists()


@runs_the_sh_installer
@needs_local_tools
def test_a_same_version_unrelated_server_does_not_commit(
    tmp_path: Path,
) -> None:
    # The port is held by a process that is not the job launchd started, and it
    # reports the exact new version. A version string says nothing about *which*
    # process answered; the pid binding does.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "unrelated-responder").write_text("")
    (harness["home"] / "listener-pid").write_text("9999\n")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was not proven" in result.stderr
    transcript = _log(harness, "native-host-transaction.log")
    assert "commit-upgrade" not in transcript
    assert transcript.count("rollback-upgrade") == 1
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    # The scenario is a genuinely different engine run: its own startup id answers
    # while this install's runtime lock names another, and the launched host and the
    # socket holder are distinct processes.
    home = harness["home"]
    # Nothing of this install's ever started, so no runtime lock exists, and the
    # port answered a foreign run both before the start and during the health wait.
    assert not (home / "Ciaobot/.runtime/server.lock").exists()
    responders = (home / "responders.log").read_text().splitlines()
    foreign = "responder=ffffffffffff4ccc8dddeeeeeeeeee01"
    assert foreign in responders
    assert responders[-1].startswith("responder=ffffffffff")
    assert "responder=absent" not in responders


@runs_the_sh_installer
@needs_local_tools
def test_an_unrelated_responder_without_an_earlier_answer_does_not_commit(
    tmp_path: Path,
) -> None:
    # Nothing answered the port before this run started one, and then an unrelated
    # same-version server appears. "No earlier answer" is not permission to accept
    # whatever answers now: the responder has to be the job launchd started.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "unrelated-responder").write_text("")
    (harness["home"] / "no-pre-responder").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was not proven" in result.stderr
    assert "commit-upgrade" not in _log(harness, "native-host-transaction.log")
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    # The pre-start response really was absent, and the post-start one is somebody
    # else's: a scenario with no earlier answer is not permission to accept it.
    responders = (harness["home"] / "responders.log").read_text().splitlines()
    # The pre-start capture itself was answered by nothing, and the health wait then
    # saw somebody else's run: the absent answer is what that scenario exercises.
    assert "responder=absent" in responders
    assert responders.index("responder=absent") < len(responders) - 1
    assert responders[-1].startswith("responder=ffffffffff")


@runs_the_sh_installer
@needs_local_tools
def test_a_changed_unrelated_responder_does_not_commit(tmp_path: Path) -> None:
    # An unrelated server answered before the start and a *different* unrelated
    # server answers after it. Something changed, but not into the job this run
    # started - so nothing is committed.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "unrelated-responder").write_text("")
    (harness["home"] / "pre-listener-pid").write_text("7777\n")
    (harness["home"] / "unrelated-startup-id").write_text(
        "ffffffffffff4ccc8dddeeeeeeeeee01\n"
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was not proven" in result.stderr
    assert "commit-upgrade" not in _log(harness, "native-host-transaction.log")
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    # Both responses were observed and they differ: something changed on the port,
    # but not into the engine run this install started.
    responders = (harness["home"] / "responders.log").read_text().splitlines()
    assert any(entry.endswith("01") for entry in responders)
    assert responders[-1].endswith("02")
    assert responders.index(next(e for e in responders if e.endswith("01"))) < (
        len(responders) - 1
    )


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "knob",
    [
        # launchd still holds the job after the bootout was asked for.
        pytest.param("fail-launchctl-bootout-com.ciao.server", id="bootout-refused"),
        # the process never leaves within the bounded wait.
        pytest.param("desktop-running", id="still-running"),
        # the process lookup itself could not answer.
        pytest.param("pgrep-cannot-run", id="lookup-failed"),
    ],
)
def test_quiescence_fails_closed_and_retains_the_recovery_material(
    tmp_path: Path, knob: str
) -> None:
    # A candidate that may still be running is never replaced and never reported
    # as rolled back: no rollback command is sent, no service is restarted, the
    # before-images and the journal stay, and the run is nonzero.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    legacy_plist = (
        home / "Applications/Ciaobot Server.app/Contents/Info.plist"
    ).read_bytes()
    (home / "startup-version").write_text("9.9.9\n")
    (home / knob).write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was NOT replaced by the previous host" in result.stderr
    assert "server-host-upgrade.json" in result.stderr
    assert _log(harness, "native-host-transaction.log") == ""
    assert "native-host-restored" not in _log(harness, "trace.log")
    assert "launchctl bootstrap" not in _log(harness, "launchctl.log")
    # The candidate is still what is installed, and the journal is still there.
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "candidate\n"
    )
    assert (home / "native-host-pending").exists()
    # The retained before-images are byte-exact: nothing was half-restored while
    # the candidate may still be running.
    before = home / "server-host-before"
    assert (before / "app/Contents/build").read_text() == "legacy\n"
    assert (before / "app/Contents/Info.plist").read_bytes() == legacy_plist
    assert (before / "record").read_text() == '{"schema": 1, "host_revision": 3}\n'
    assert stat.S_IMODE((before / "record").stat().st_mode) == 0o600


def _assert_no_commit_and_legacy_pair(
    harness: dict[str, Any], result: subprocess.CompletedProcess[str], *, step: str
) -> None:
    """The shared shape of every unproven-candidate failure."""
    home = harness["home"]
    assert result.returncode != 0
    assert "was not proven" in result.stderr
    assert step in result.stderr
    transcript = _log(harness, "native-host-transaction.log")
    assert "commit-upgrade" not in transcript
    assert transcript.count("rollback-upgrade") == 1
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )
    assert (home / ".local/state/ciaobot/server-host.json").read_text() == (
        '{"schema": 1, "host_revision": 3}\n'
    )


@runs_the_sh_installer
@needs_local_tools
def test_a_wrong_boundary_executable_does_not_commit(tmp_path: Path) -> None:
    # The ancestry reaches launchd's pid, but that process is not running the
    # installed host executable. On a real Mac `ps -o comm=` cannot answer this at
    # all, which is why the executable text is read from lsof; either way, a job
    # that is not the host is not the candidate.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "host-executable-wrong").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    _assert_no_commit_and_legacy_pair(harness, result, step="executable")


def test_the_engine_runtime_lock_is_created_private_under_a_typical_umask(
    tmp_path: Path,
) -> None:
    # The installer's health gate refuses a runtime lock that is not 0600, so the
    # engine has to *create* it that way: `open()` honours the umask, and the usual
    # 022 would leave a fresh lock group/world readable without the fchmod in
    # `WorkspaceInstanceLock.acquire`. The harness chmods its fake lock explicitly,
    # so this exercises the real engine code instead of that fixture.
    if sys.platform == "win32":
        pytest.skip("a POSIX mode is what this asserts")
    from ciao.instance_lock import WorkspaceInstanceLock

    runtime = tmp_path / "Ciaobot" / ".runtime"
    runtime.mkdir(parents=True)
    lock = WorkspaceInstanceLock(
        runtime, workspace_root=tmp_path / "Ciaobot", port=8443
    )
    previous_umask = os.umask(0o022)
    try:
        lock.acquire()
    finally:
        os.umask(previous_umask)
    try:
        info = os.lstat(lock.path)
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_uid == os.getuid()
        metadata = json.loads(lock.path.read_text(encoding="utf-8"))
        assert metadata["status"] == "running"
        assert metadata["pid"] == os.getpid()
        assert metadata["port"] == 8443
        assert metadata["startup_id"] == lock.startup_id
    finally:
        lock.release()


@runs_the_sh_installer
@needs_local_tools
def test_an_ambiguous_executable_lookup_does_not_commit(tmp_path: Path) -> None:
    # `lsof -d txt` reports two `n` records and one of them *is* the installed host.
    # A parser that accepted any matching record would pass this; the contract is
    # exactly one record, so an ambiguous answer proves nothing.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "host-executable-mixed").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    _assert_no_commit_and_legacy_pair(
        harness, result, step="exactly one executable text record"
    )


@runs_the_sh_installer
@needs_local_tools
def test_an_indeterminate_process_lookup_does_not_commit(tmp_path: Path) -> None:
    # `ps` could not answer at all. An unknown ancestry is not a proven one, so the
    # before-images are not released.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "ps-cannot-run").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    # The reason names the unreadable lookup itself, not a chain that merely
    # stopped early: "could not ask" and "asked and got nothing" are different
    # failures for an operator.
    _assert_no_commit_and_legacy_pair(
        harness, result, step="readable process ancestry"
    )


@runs_the_sh_installer
@needs_local_tools
def test_an_over_deep_ancestor_chain_does_not_commit(tmp_path: Path) -> None:
    # The listener really is a descendant of launchd's job, but six hops away. The
    # walk is bounded at four, so the chain does not reach the job and nothing is
    # proven.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "deep-chain").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    _assert_no_commit_and_legacy_pair(harness, result, step="ancestry")


@runs_the_sh_installer
@needs_local_tools
def test_a_listener_replaced_during_the_request_does_not_commit(
    tmp_path: Path,
) -> None:
    # An unrelated listener answers the health request and exits; the candidate
    # binds the socket before the second capture. The readable lock metadata would
    # have matched, which is exactly why the listener identity is captured on both
    # sides of the request.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "listener-replaced").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    _assert_no_commit_and_legacy_pair(harness, result, step="listener")


@runs_the_sh_installer
@needs_local_tools
def test_an_unsafe_runtime_lock_does_not_commit(tmp_path: Path) -> None:
    # The lock names the right engine but is group/world readable, so it is not
    # trusted as this install's private identity.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "lock-mode-0644").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    _assert_no_commit_and_legacy_pair(harness, result, step="runtime lock")


@runs_the_sh_installer
@needs_local_tools
def test_a_committed_host_with_unremovable_before_images_is_reported_and_fails(
    tmp_path: Path,
) -> None:
    # The module committed the host but could not remove its retained material. The
    # settled state is kept - nothing rolls a committed host back - but the
    # installer result is a failure, because the leftover blocks the next host
    # transaction until a later run removes it.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    (harness["home"] / "leftover-native-host-commit").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was committed, but its retained before-images could not be removed" in (
        result.stderr
    )
    transcript = _log(harness, "native-host-transaction.log")
    assert "commit-upgrade" in transcript
    # No second attempt at either terminal command, and the committed host stays.
    assert transcript.count("commit-upgrade") == 1
    assert "rollback-upgrade" not in transcript
    assert (harness["home"] / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "candidate\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_a_restored_host_with_unremovable_before_images_is_not_a_failed_rollback(
    tmp_path: Path,
) -> None:
    # The rollback restored the pair but could not remove the retained material.
    # The run still fails for its original reason, and the message must not claim
    # the host could not be restored.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    _engine_definition(harness)
    home = harness["home"]
    (home / "startup-version").write_text("9.9.9\n")
    (home / "leftover-native-host-rollback").write_text("")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "was restored, but its retained before-images could not be removed" in (
        result.stderr
    )
    assert "could NOT be fully restored" not in result.stderr
    assert (home / "Applications/Ciaobot Server.app/Contents/build").read_text() == (
        "legacy\n"
    )


@runs_the_sh_installer
@needs_local_tools
def test_failed_candidate_quiescence_retains_native_upgrade_recovery_material(
    tmp_path: Path,
) -> None:
    # The candidate's service may still be running. Reverting the bytes under a
    # live signed process is what this transaction exists to prevent, so a
    # quiescence that fails keeps the before-images and the journal, reports an
    # incomplete rollback and stays nonzero. It never claims a rollback it did not
    # do.
    harness = _signed_host_harness(tmp_path)
    _defer(harness)
    _existing_install(harness)
    (harness["home"] / "desktop-running").write_text("")
    (harness["home"] / "startup-version").write_text("9.9.9\n")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode != 0
    assert "rollback-upgrade" not in _log(harness, "native-host-transaction.log")
    assert "was NOT replaced by the previous host" in result.stderr
    assert "server-host-upgrade.json" in result.stderr
    # The recoverable material is still there for the next run.
    assert (harness["home"] / "native-host-pending").exists()


@runs_the_sh_installer
@needs_local_tools
def test_no_start_defers_legacy_to_signed_upgrade(tmp_path: Path) -> None:
    # --no-start never starts the engine, so it cannot prove the new host is the
    # one serving. It keeps the previous host and says the upgrade completes on a
    # later normal run: no fabricated health, no commit, no service call.
    harness = _signed_host_harness(tmp_path)
    _existing_install(harness)
    (harness["home"] / "defer-required-server-host").write_text("")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    assert "later installer run without --no-start" in result.stderr
    assert "Ciaobot Server host installed and recorded." not in result.stdout
    argv = _log(harness, "server-host-install.log").splitlines()[0].split(" ")
    assert "--defer-commit" not in argv
    assert _log(harness, "native-host-transaction.log") == ""
    assert not (harness["home"] / "native-host-pending").exists()
    assert "launchctl" not in _log(harness, "launchctl.log")


@runs_the_sh_installer
@needs_local_tools
def test_a_previous_unfinished_upgrade_is_recovered_before_the_new_one(
    tmp_path: Path,
) -> None:
    # A journal a killed run left. Its owner is gone, so this run restores the
    # previous host from that material before it installs anything new - never
    # committing or adopting the abandoned operation.
    harness = _signed_host_harness(tmp_path)
    _existing_install(harness)
    home = harness["home"]
    (home / "native-host-pending").write_text("0123456789abcdef0123456789abcdef")
    (home / "native-host-owner-pid").write_text("999999")
    (home / "native-host-owner-alive").write_text("false")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "did not finish" in result.stderr
    assert "rollback-upgrade" in _log(harness, "native-host-transaction.log")
    assert "commit-upgrade" not in _log(harness, "native-host-transaction.log")
    assert not (home / "native-host-pending").exists()
    assert "server-host-install.log" in [path.name for path in home.iterdir()]


@runs_the_sh_installer
@needs_local_tools
def test_a_live_installer_owns_its_unfinished_upgrade(tmp_path: Path) -> None:
    # The first installer is still running between its subprocess calls. A second
    # one refuses instead of rolling that live operation back, and touches no host
    # at all.
    harness = _signed_host_harness(tmp_path)
    _existing_install(harness)
    home = harness["home"]
    (home / "native-host-pending").write_text("0123456789abcdef0123456789abcdef")
    (home / "native-host-owner-pid").write_text("4321")
    (home / "native-host-owner-alive").write_text("true")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1
    assert "still upgrading" in result.stderr
    assert _log(harness, "server-host-install.log") == ""
    assert _log(harness, "native-host-transaction.log") == ""
    assert (home / "native-host-pending").exists()


@runs_the_sh_installer
@needs_local_tools
def test_client_and_wheel_only_paths_never_begin_native_upgrade(
    tmp_path: Path,
) -> None:
    # A wheel-only release has no host at all, and a client runs no engine to
    # host: neither may download a host, read a journal or start a transaction.
    wheel_only = _harness(tmp_path / "wheel-only")
    result = _run_installer(wheel_only, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert SERVER_HOST_FILENAME not in _log(wheel_only, "trace.log")
    assert _log(wheel_only, "server-host-install.log") == ""
    assert _log(wheel_only, "native-host-transaction.log") == ""
    assert "transaction-status" not in _log(wheel_only, "uv-run-calls.log")

    host_archive = tmp_path / "client-host" / SERVER_HOST_FILENAME
    host_archive.parent.mkdir(parents=True)
    host_archive.write_bytes(b"the signed universal host archive")
    client = _harness(tmp_path / "client", host_archive=host_archive, signed_team=_SIGNED_TEAM)
    _desktop_install(
        client, tmp_path / "client", {"role": "standby", "host_url": "https://host.example"}
    )

    result = _run_installer(
        client,
        "--version",
        VERSION,
        "--migrate",
        "--as-client",
        "https://host.example",
    )

    assert result.returncode == 0, result.stderr
    assert _log(client, "server-host-install.log") == ""
    assert _log(client, "native-host-transaction.log") == ""
    assert "transaction-status" not in _log(client, "uv-run-calls.log")


@runs_the_sh_installer
@needs_local_tools
def test_engine_installer_rejects_tampered_wheel(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    (harness["release"] / WHEEL_NAME).write_bytes(b"a wheel nobody signed")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "does not match the signed manifest" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@runs_the_sh_installer
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
    """The shim the retired app installer wrote, pointing at `target`.

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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
@needs_local_tools
def test_engine_installer_rejects_cdpath_surprise(tmp_path: Path) -> None:
    # `cd` consults CDPATH for a bare relative name and prints the path it
    # found, so an inherited CDPATH decides where the workspace really is (or
    # makes the install fail after the wheel is already installed). The decoy
    # `wsrel` under the CDPATH entry is the case that must not win.
    harness = _harness(tmp_path)
    _existing_install(harness, harness["home"] / "wsrel")
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
    _write_exec(plistbuddy, f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(str(program))}\n")
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


@runs_the_sh_installer
def test_engine_installer_refuses_app_managed_engine(tmp_path: Path) -> None:
    # The bundle is built under tmp_path, so the refusal means the same thing
    # on a Mac with Ciaobot.app installed and on one without.
    app_engine = tmp_path / "Ciaobot.app" / "Contents" / "MacOS" / "ciao"
    app_engine.parent.mkdir(parents=True)
    _write_exec(app_engine, "#!/bin/sh\nexit 0\n")

    result = _run_refuse_desktop_engine(tmp_path, app_engine)

    assert result.returncode == 1
    assert "#576" in result.stderr


@runs_the_sh_installer
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


@runs_the_sh_installer
def test_engine_installer_accepts_its_own_server_host(tmp_path: Path) -> None:
    # v1.2.0 points the LaunchAgent at Ciaobot Server.app. A later install is
    # an update of that host, not a hand-over from the retired Ciaobot.app
    # (#1141). The executable has to exist: a missing program is already
    # ignored, and that would not catch this refusal.
    host = (
        tmp_path
        / "Applications"
        / "Ciaobot Server.app"
        / "Contents"
        / "MacOS"
        / "Ciaobot Server"
    )
    host.parent.mkdir(parents=True)
    _write_exec(host, "#!/bin/sh\nexit 0\n")

    result = _run_refuse_desktop_engine(tmp_path, host)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


# --- the Ciaobot.app → terminal-engine migration (#576) -------------------
#
# The classifier these tests run against is the real one, out of this checkout,
# over a fake `$HOME`: a real `--migrate` on this Mac would migrate the machine
# running the tests.


def _app_bundle(tmp_path: Path) -> Path:
    """The bundle `_desktop_install` puts on the fake Mac, as the path the
    classifier reports it as. A real directory, because #672 removes it: an
    install whose bundle is not there is not the state a hand-over is run
    against."""
    return tmp_path / "Ciaobot.app"


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
        _app_bundle(tmp_path) / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    )
    app_engine.parent.mkdir(parents=True)
    _write_exec(app_engine, "#!/bin/sh\nexit 0\n")
    _write_desktop_shim(home, app_engine)

    workspace = home / "Ciaobot"
    (workspace / ".runtime").mkdir(parents=True, exist_ok=True)
    (workspace / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    (workspace / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")
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


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_repoints_and_retires_app_agent(tmp_path: Path) -> None:
    # --no-start because the health poll needs a real engine to answer; what is
    # under test here is everything up to the point where the app's own agent
    # would be retired, which is the ordering the whole migration turns on.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert result.returncode == 0, result.stderr
    before = home / ".local/state/ciaobot/migration/before"
    for name in ("com.ciao.server.plist", "Ciaobot.plist", "ciao"):
        assert (before / name).exists(), f"{name} was not backed up"
    # The before-image is a copy of what was there, not of what setup went on to
    # write: the app's plist is the only way back to the engine that is running
    # now, and an image taken after `ciao setup` would restore the new engine's
    # own plist instead.
    assert (before / "com.ciao.server.plist").read_bytes() == original_plist

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


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "knob,booted_out",
    [
        # A launchd that will not boot the app's own agent out: nothing has been
        # unloaded, so a rollback only has to re-enable the label.
        pytest.param("fail-launchctl-bootout-Ciaobot", False, id="bootout"),
        # A launchd that will not disable it. The bootout before it went
        # through, so the agent is unloaded and the rollback has to load and
        # start it again - an enabled label with no job behind it never runs.
        pytest.param("fail-launchctl-disable-Ciaobot", True, id="disable"),
    ],
)
def test_migrate_client_retirement_failure_rolls_back(
    tmp_path: Path, knob: str, booted_out: bool
) -> None:
    # A client that stopped the engine but left the app's own agent loaded comes
    # back on the next relaunch with a desktop engine running next to the host it
    # was just handed to - and a `migrated_client` receipt on top of that tells
    # the user the hand-over is done. So neither bootout nor disable of that
    # agent is shrugged off: the run aborts, the plist stays where it was, both
    # labels go back, and nothing claims the migration finished.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )
    _knob(harness, knob)
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert "the local engine could not be stopped" in result.stderr
    # The plist is not taken away from an agent that is still loaded.
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert _replaced_state(harness) == untouched, knob
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    log = _log(harness, "launchctl.log")
    for label in ("com.ciao.server", "Ciaobot"):
        assert f"enable gui/{os.getuid()}/{label}" in log, label
    desktop_plist = home / "Library/LaunchAgents/Ciaobot.plist"
    if booted_out:
        assert f"bootstrap gui/{os.getuid()} {desktop_plist}" in log
        assert f"kickstart -k gui/{os.getuid()}/Ciaobot" in log
    # Nothing this transaction created is left behind, and the user is never
    # told a hand-over happened: a client has no engine, and no host to be at.
    assert not _tool_env(harness).exists()
    assert "no longer runs its own engine" not in result.stdout
    assert "already" not in result.stdout


@runs_the_sh_installer
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
    # The whole hand-over sentence, as install-engine.sh:1453 prints it, not a
    # URL substring: a bare "https://mini.ts.net" would also match an unrelated
    # line that merely has the address in it, and would pass even if the client
    # explanation never reached the user.
    assert (
        "This Mac was a client of https://mini.ts.net. Open that address in your "
        "browser and sign in there; this Mac no longer runs its own engine."
    ) in result.stdout.splitlines()


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
@needs_local_tools
def test_without_migrate_live_desktop_still_refused_with_hint(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 1
    assert "--migrate" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")


@runs_the_sh_installer
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
        shutil.rmtree(home / "Ciaobot" / ".runtime")
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


@runs_the_sh_installer
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


@runs_the_sh_installer
@needs_local_tools
def test_migrate_client_noop_after_success(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _write_settled_migration(harness, phase="migrated_client", backend="none")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert "already migrated (migrated_client)" in result.stdout
    assert "tool install" not in _log(harness, "uv-calls.log")


@runs_the_sh_installer
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
    was really migrated looks like. The rest of that is built too, because the
    gate is only worth having if the Mac it approves of is the one a migration
    actually leaves behind: the `ciao` entry point and the tool environment it
    was installed into exist, and a host's engine LaunchAgent execs the entry
    point rather than the program inside Ciaobot.app.
    """
    home: Path = harness["home"]
    migration = home / ".local/state/ciaobot/migration"
    before = migration / "before"
    before.mkdir(parents=True, exist_ok=True)
    images: dict[str, str] = {}
    for name in ("server_plist", "desktop_plist", "shim", "install_receipt", "tool_env"):
        images[name] = ""
    # Under the names the installer itself writes them under, because a receipt
    # is only a receipt of a before-image the installer can put back.
    agents = home / "Library/LaunchAgents"
    for name, source, copied in (
        ("server_plist", agents / "com.ciao.server.plist", "com.ciao.server.plist"),
        ("desktop_plist", agents / "Ciaobot.plist", "Ciaobot.plist"),
        ("shim", home / ".local/bin/ciao", "ciao"),
    ):
        if source.exists():
            shutil.copyfile(source, before / copied)
            images[name] = str(before / copied)
    if phase in ("migrated", "migrated_client"):
        (home / "Library/LaunchAgents/Ciaobot.plist").unlink(missing_ok=True)
        images["desktop_plist"] = ""
    executable = home / ".local/bin/ciao"
    if phase in ("migrated", "migrated_client"):
        # The tool the receipt claims this migration installed: without it the
        # Mac is one a `uv tool uninstall`, or a wiped state directory, could
        # have been left as, and the receipt outlives both.
        _write_exec(executable, "#!/bin/sh\nexit 0\n")
        (_tool_env(harness) / "bin").mkdir(parents=True)
        _write_exec(_tool_env(harness) / "bin" / "python", "#!/bin/sh\nexit 0\n")
    if phase == "migrated":
        # A host's engine is the com.ciao.server agent, so its plist is the one
        # `ciao setup` wrote. A client has no engine: its plist is left pointing
        # inside Ciaobot.app on purpose, and the gate has to know the difference.
        (home / "Library/LaunchAgents").mkdir(parents=True, exist_ok=True)
        (home / "Library/LaunchAgents/com.ciao.server.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.ciao.server",
                    "ProgramArguments": [str(executable), "run"],
                }
            )
        )
    state = home / ".local/state/ciaobot"
    state.mkdir(parents=True, exist_ok=True)
    # Indented and sorted, because the installer's receipt reader picks its
    # fields out with awk line by line, exactly as it does for a real one.
    (state / "install-receipt.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "executable": str(executable),
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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
        # A key that is not there at all. Read as an absence it is the most
        # dangerous receipt there is: a rollback then deletes a file the user
        # had before the migration started, and says the engine was restored.
        pytest.param(
            "incomplete-image",
            "does not record every before-image",
            id="incomplete-image",
        ),
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
    elif case == "incomplete-image":
        _write_settled_migration(harness, phase="started")
        receipt = json.loads((migration / "receipt.json").read_text(encoding="utf-8"))
        del receipt["before"]["shim"]
        (migration / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
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


@runs_the_sh_installer
@needs_local_tools
def test_migrate_retry_accepts_a_tool_environment_before_image(tmp_path: Path) -> None:
    # A Mac that already had an engine installed through uv has a tool
    # environment before this migration, and its before-image is a *directory* -
    # `cp -pR` of the environment, not a file. Read as a file it looks like a
    # snapshot that has been deleted, so the retry would refuse a transaction it
    # could in fact undo: the worst possible answer on the one Mac where there is
    # something to roll back to.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")
    state = home / ".local/state/ciaobot"
    state.mkdir(parents=True, exist_ok=True)
    (state / "install-receipt.json").write_text(
        json.dumps(
            {
                "version": "0.9.0",
                "executable": str(home / ".local/bin/ciao"),
                "python": str(_tool_env(harness) / "bin" / "python"),
                "service_backend": "launchd",
                "service_label": "com.ciao.server",
                "installed_at": "2026-09-01T10:00:00+00:00",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (_tool_env(harness) / "bin").mkdir(parents=True)
    _write_exec(_tool_env(harness) / "bin" / "python", "#!/bin/sh\nexit 0\n")
    _knob(harness, "fail-ciao-setup")

    failed = _run_installer(harness, "--version", VERSION, "--migrate")

    assert failed.returncode == 1
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    before = migration_before(harness)
    assert (before / "tool-env" / "bin" / "python").exists()

    (home / "fail-ciao-setup").unlink()
    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 0, retry.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    # Resumed from the originals the first run kept, not from a fresh snapshot
    # taken after the install that failed.
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim


@runs_the_sh_installer
@needs_local_tools
def test_migrate_never_reads_an_unrecorded_before_image_as_an_absence(
    tmp_path: Path,
) -> None:
    # A `started` receipt with no `shim` key is not a receipt saying this Mac had
    # no shim; it is a receipt that does not say what it replaced. Treating the
    # omission as the recorded absence it looks like is how a rollback deletes
    # the shim Ciaobot.app is using and then reports the engine restored, so the
    # receipt is refused with the recovery spelled out - before the install is
    # even attempted, which is what the armed `uv tool install` failure here is
    # there to prove.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")
    migration = home / ".local/state/ciaobot/migration"
    _write_settled_migration(harness, phase="started")
    receipt = json.loads((migration / "receipt.json").read_text(encoding="utf-8"))
    del receipt["before"]["shim"]
    (migration / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    _arm(harness, "fail-uv-tool-install")
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert "does not record every before-image" in result.stderr
    assert "move the receipt aside" in result.stderr
    # Refused, not rolled back: the original shim and everything else it is
    # holding is exactly as it was, and nothing claims an engine came back.
    assert (home / ".local/bin/ciao").read_text(encoding="utf-8") == original_shim
    assert _replaced_state(harness) == untouched
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert "engine was restored" not in result.stderr
    assert _log(harness, "launchctl.log") == ""


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "value",
    [
        # Not a string at all: a `null` is what a half-written receipt looks
        # like, and reading it as an absence would delete a real file.
        pytest.param(None, id="null"),
        pytest.param(0, id="number"),
        pytest.param(["ciao"], id="list"),
    ],
)
def test_migrate_rejects_a_before_image_that_is_not_a_path(
    tmp_path: Path, value: object
) -> None:
    # The recorded absence of a file is the empty string and only the empty
    # string. Anything else - a null, a number, a list - is a receipt this
    # installer cannot read, not a fact about this Mac.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    migration = home / ".local/state/ciaobot/migration"
    _write_settled_migration(harness, phase="started")
    receipt = json.loads((migration / "receipt.json").read_text(encoding="utf-8"))
    receipt["before"]["shim"] = value
    (migration / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert "does not record every before-image" in result.stderr
    assert _replaced_state(harness) == untouched
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""


@runs_the_sh_installer
@needs_local_tools
def test_migrate_is_a_noop_after_a_real_migration(tmp_path: Path) -> None:
    # The same no-op claim, asked of a Mac this script really migrated rather than
    # of a fixture: the receipt it wrote, the install receipt, the tool
    # environment and the engine LaunchAgent `ciao setup` repointed all have to
    # agree, and only then is a second run a no-op that touches nothing.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)

    first = _run_installer(harness, "--version", VERSION, "--migrate")

    assert first.returncode == 0, first.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    ciao = home / ".local/bin/ciao"
    assert _tool_env(harness).exists()
    # The engine LaunchAgent is the one setup wrote, execing the installed ciao.
    program = plistlib.loads(
        (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()
    )["ProgramArguments"][0]
    assert program == str(ciao)

    settled = _replaced_state(harness)
    installed = _log(harness, "uv-calls.log")
    labels = _log(harness, "launchctl.log")
    second = _run_installer(harness, "--version", VERSION, "--migrate")

    assert second.returncode == 0, second.stderr
    assert "already migrated (migrated)" in second.stdout
    # A no-op is a no-op: nothing was installed, no label was touched, and the
    # install on this Mac is byte for byte what the first run left.
    assert _log(harness, "uv-calls.log") == installed
    assert _log(harness, "launchctl.log") == labels
    assert _replaced_state(harness) == settled


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "break_engine",
    [
        # The engine agent is gone: a receipt next to no server plist describes a
        # Mac with nothing running on its port.
        pytest.param("missing-server-plist", id="missing-server-plist"),
        # The plist is back and points into Ciaobot.app again, which is the
        # desktop's engine - the one this migration replaced.
        pytest.param("desktop-pointing-plist", id="desktop-pointing-plist"),
        # The tool the receipt names is gone: the Mac has a receipt saying it
        # migrated and no `ciao` to run.
        pytest.param("missing-tool", id="missing-tool"),
    ],
)
def test_migrate_settled_receipt_must_describe_this_mac(
    tmp_path: Path, break_engine: str
) -> None:
    # "Already migrated" is a claim about this Mac, and the engine LaunchAgent is
    # the part of it the receipt cannot see for itself. A settled receipt on a Mac
    # whose plist was deleted, put back, or whose tool environment was removed is
    # not believed: the migration runs again from the originals the receipt kept,
    # and the run says why it did not stop at "nothing to do".
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    _write_settled_migration(harness, phase="migrated")
    server_plist = home / "Library/LaunchAgents/com.ciao.server.plist"
    if break_engine == "missing-server-plist":
        server_plist.unlink()
    elif break_engine == "desktop-pointing-plist":
        # Back to the program inside the bundle - and the same workspace the
        # plist carried, because that is what the classifier reads the runtime
        # root from, not just the program.
        workspace = home / "Ciaobot"
        server_plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.ciao.server",
                    "ProgramArguments": [
                        str(
                            tmp_path
                            / "Ciaobot.app/Contents/Resources/ciao-runtime/bin/ciao"
                        ),
                        "run",
                    ],
                    "EnvironmentVariables": {"CIAO_WORKSPACE": str(workspace)},
                    "WorkingDirectory": str(workspace),
                }
            )
        )
    else:
        (home / ".local/bin/ciao").unlink()
        (home / ".local/bin/ciao").write_text("not there\n", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert "already migrated" not in result.stdout
    assert "is not believed" in result.stderr
    # And it is not left claiming more than the Mac has: the receipt settles
    # again from the originals it kept, and the app's own agent stays retired.
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    if server_plist.exists():
        program = plistlib.loads(server_plist.read_bytes())["ProgramArguments"][0]
        assert program == str(home / ".local/bin/ciao")


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "url",
    [
        # The prefix check accepts all of these, and none of them is an address:
        # a client handed one has no engine of its own and nowhere to sign in.
        pytest.param("https://", id="scheme-only"),
        pytest.param("https:///app", id="empty-host"),
        pytest.param("https://:8443", id="no-host"),
        # A URL the parser itself rejects is not an address either.
        pytest.param("https://[::1", id="unparseable-ipv6"),
        # Credentials in the URL would be printed back at the user and stored in
        # the receipt, so they are refused rather than passed on.
        pytest.param("https://user:secret@h.example", id="credentials"),
        pytest.param("https://h.example/a b", id="whitespace"),
    ],
)
def test_migrate_rejects_a_malformed_as_client_override(
    tmp_path: Path, url: str
) -> None:
    # A node state nobody can read, and an override that is a prefix rather than
    # an address: this Mac must not lose its engine before anyone has checked
    # where the user is being sent instead. The refusal is before any mutation at
    # all - no label, no file, not even the migration directory.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path, "{not json at all")
    untouched = _replaced_state(harness)

    result = _run_installer(
        harness, "--version", VERSION, "--migrate", "--as-client", url
    )

    assert result.returncode == 1, result.stdout
    assert "--as-client takes an address" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    assert _replaced_state(harness) == untouched
    assert not (home / ".local/state/ciaobot/migration").exists()
    # Nothing told the user to open an address that opens nothing.
    assert "no longer runs its own engine" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_does_not_believe_a_stale_success_receipt(
    tmp_path: Path,
) -> None:
    # A success receipt on a Mac whose install does not match it - restored from
    # a backup, or with the app's agent back - is a Mac with no engine and a
    # message saying it is migrated. The receipt is not believed, and it is not
    # thrown away either: the migration is done again from the originals it kept.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    _write_settled_migration(harness, phase="migrated")
    # Ciaobot.app is back: its own agent's plist and the engine plist it wrote
    # are both back, restored out of the very before-images the receipt kept -
    # which is what a Mac restored from a backup looks like, and the one case
    # where the receipt claims a migration this Mac plainly has not had.
    before = migration_before(harness)
    for name in ("com.ciao.server.plist", "Ciaobot.plist"):
        shutil.copyfile(before / name, home / "Library/LaunchAgents" / name)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert "already migrated" not in result.stdout
    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (harness["home"] / "Library/LaunchAgents/Ciaobot.plist").exists()


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
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


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_rollback_restores_the_shim_as_a_regular_file(
    tmp_path: Path,
) -> None:
    # The window #668 is about. `uv tool install --force` leaves the entry point
    # as a symlink into the tool environment, and the migration then fails after
    # the tool is installed - here at the install receipt, the step that failed on
    # the operator's Mac. A rollback that restores the app shim with a plain
    # `cp` writes the shim *through* that symlink, overwrites the tool
    # environment's own entry point with it, and then removes the environment:
    # the Mac is left with a dangling `ciao` and no engine at all, while the
    # receipt says the rollback happened. The shim has to come back as the
    # regular file the before-image holds.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    app_engine = (
        tmp_path / "Ciaobot.app" / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    )
    _knob(harness, "fail-install-receipt")
    shim = home / ".local" / "bin" / "ciao"

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert result.returncode == 1, result.stdout
    # The failure has to be the one after the tool was installed, or the state
    # under test is not the state #668 hit.
    assert "tool install --force" in _log(harness, "uv-calls.log")
    assert "could not write the install receipt" in result.stderr
    assert "Ciaobot.app's engine was restored" in result.stderr
    # A regular file, not the symlink uv left, and the app's shim byte for byte.
    assert not shim.is_symlink()
    assert shim.is_file()
    assert f'exec "{app_engine}" "$@"' in shim.read_text(encoding="utf-8")
    assert os.access(shim, os.X_OK)
    # The tool environment the restored shim would have pointed through is gone,
    # rather than being what the shim was written into.
    assert not _tool_env(harness).exists()
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    # The app's engine is the one the restored shim execs, so a rolled-back run
    # may not have taken the bundle it runs out of with it.
    assert _app_bundle(tmp_path).is_dir()


@runs_the_sh_installer
@needs_local_tools
def test_tool_env_is_probed_before_the_receipt_is_written(tmp_path: Path) -> None:
    # `uv tool install --force` can exit 0 and leave an environment the installed
    # interpreter cannot import from. #668 was reported from exactly that: the
    # failure surfaced at the install receipt as a missing module, one step too
    # late, with the app's engine already gone. Probed where it is found, the
    # same half-install is a clean rollback with a message that names it.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    _knob(harness, "half-installed-tool-env")
    shim = home / ".local" / "bin" / "ciao"

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1, result.stdout
    assert "the installed engine is not importable" in result.stderr, result.stderr
    # Not the same failure one step later: the receipt was never reached, so its
    # own message is not the one the run reported.
    assert "could not write the install receipt" not in result.stderr
    assert "Ciaobot.app's engine was restored" in result.stderr
    # Which is a rollback of a real half-install, not a no-op: the tool
    # environment the bad interpreter came from is gone and the app's shim is
    # back, and no install receipt is left naming an engine that is not there.
    assert not _tool_env(harness).exists()
    assert not shim.is_symlink()
    assert f'exec "{tmp_path / "Ciaobot.app"}' in shim.read_text(encoding="utf-8")
    assert not (home / ".local/state/ciaobot/install-receipt.json").exists()
    assert _migration_receipt(harness)["phase"] == "rolled_back"


@runs_the_sh_installer
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


@runs_the_sh_installer
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
    # The completed hand-over retires the bundle with the app's agent, and the
    # manual command is named only when that removal could not run.
    assert not _app_bundle(tmp_path).exists()
    assert "Removed the retired Ciaobot.app." in result.stdout
    assert "ciao desktop uninstall" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_install_keeps_going_when_setup_skips_memory_regions(tmp_path: Path) -> None:
    # #790. `ciao setup` reports a workspace whose memory regions could not be
    # set up with its own exit code, after everything else it scaffolds has
    # landed. Treating that like any other setup failure would roll back an
    # install that works, so that one code is tolerated and the install goes on.
    harness = _harness(tmp_path)
    _existing_install(harness)
    _knob(harness, "fail-ciao-setup-memory")

    result = _run_installer(harness, "--version", VERSION, "--no-start")

    assert result.returncode == 0, result.stderr
    # Setup's own warning is not swallowed by the redirect, and the install says
    # why it is continuing.
    assert "memory regions not set up for" in result.stderr
    assert "continuing install" in result.stderr
    assert f"Ciaobot engine {VERSION} installed." in result.stdout
    assert _install_receipt(harness)["version"] == VERSION


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_removes_the_retired_app_bundle(tmp_path: Path) -> None:
    # #672. Once the receipt says `migrated` and no launchd job points into
    # Ciaobot.app, the bundle is inert: nothing runs out of it, and a
    # double-click on it would bring the old PWA back against an engine this
    # Mac no longer has. So the run that completed the hand-over takes it away
    # and says so, rather than printing a second command the user has to find.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    bundle = _app_bundle(tmp_path)
    # The bundle is a real directory holding the engine the plist names, so the
    # removal below is observable rather than a no-op against a missing path.
    assert (bundle / "Contents/Resources/ciao-runtime/bin/ciao").is_file()

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not bundle.exists()
    assert "Removed the retired Ciaobot.app." in result.stdout
    # Nothing is left pointing into the deleted bundle: the app's own agent is
    # out of launchd and its plist went with it.
    assert not (harness["home"] / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert f"disable gui/{os.getuid()}/Ciaobot" in _log(harness, "launchctl.log")
    # The install itself is untouched: only the retired bundle went.
    assert _install_receipt(harness)["service_backend"] == "launchd"
    assert (harness["home"] / ".local/bin/ciao").exists()


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_no_start_keeps_the_app_bundle(tmp_path: Path) -> None:
    # `--no-start` promised no service and no retirement, and the app's own
    # agent is still loaded on this Mac afterwards: the bundle is still what the
    # next relaunch of Ciaobot.app runs out of. Deleting it there would take
    # away the very engine a second run without `--no-start` completes the
    # hand-over from.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "installed_no_start"
    assert _app_bundle(tmp_path).is_dir()
    assert "Removed the retired Ciaobot.app." not in result.stdout
    assert "ciao desktop uninstall" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_client_keeps_the_app_bundle(tmp_path: Path) -> None:
    # The client path retires the app's own agent, but this Mac is not being
    # given the engine it was running - it is being pointed at somebody else's.
    # The bundle is not this transaction's to delete, and the path that could do
    # it is not on this branch at all.
    harness = _harness(tmp_path)
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated_client"
    assert _app_bundle(tmp_path).is_dir()
    assert "Removed the retired Ciaobot.app." not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_reports_a_bundle_it_could_not_remove(tmp_path: Path) -> None:
    # A `/Applications` install the account cannot write. The hand-over has
    # already succeeded - the receipt is `migrated` and the app's agent is gone
    # - so a bundle that would not delete is reported, never fatal: failing here
    # would report a migration that worked as one that did not, and would point
    # the user at a rollback for a leftover directory.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    real_rm = shutil.which("rm") or "/bin/rm"
    # `rm` is a plain command in the script, so the harness reaches it through
    # PATH: a stub that refuses a `.app` and does everything else for real is
    # the one case a fake `$HOME` cannot produce by permissions alone.
    _write_exec(
        harness["fakebin"] / "rm",
        '#!/bin/sh\n'
        'for arg in "$@"; do\n'
        '    case "$arg" in\n'
        '        *.app) exit 1 ;;\n'
        '    esac\n'
        'done\n'
        f'exec {real_rm} "$@"\n',
    )

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    # The bundle is still there, and the manual way to remove it is named
    # exactly here and nowhere else in the run.
    assert _app_bundle(tmp_path).is_dir()
    assert "Ciaobot.app could not be removed automatically" in result.stdout
    assert f"'{harness['home']}/.local/bin/ciao' desktop uninstall" in result.stdout
    assert "Removed the retired Ciaobot.app." not in result.stdout


@runs_the_sh_installer
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
    # The bootout before the failed disable did go through, so the app's agent is
    # unloaded. A rollback that only re-enabled its label would leave a host
    # whose Ciaobot.app engine never starts again while reporting the engine
    # restored, so the agent is loaded and started again here.
    log = _log(harness, "launchctl.log")
    desktop_plist = home / "Library/LaunchAgents/Ciaobot.plist"
    assert f"bootstrap gui/{os.getuid()} {desktop_plist}" in log
    assert f"kickstart -k gui/{os.getuid()}/Ciaobot" in log
    assert "Ciaobot.app's engine was restored" in result.stderr
    # The durable phase a crash would resume from was written before the
    # retirement was attempted, and `migrated` was never reached.
    trace = _trace(harness)
    assert "receipt-write retiring" in trace
    assert "receipt-write migrated" not in trace
    # The bundle is what the rollback gave the app its engine back out of, so a
    # rolled-back hand-over may not have deleted it.
    assert _app_bundle(tmp_path).is_dir()


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_interruption_after_bootout_reloads_the_app_agent(
    tmp_path: Path,
) -> None:
    # An interruption in the middle of the retirement is the same fact reached a
    # different way: launchd has dropped the app's own agent, and the process
    # goes away before the disable. The receipt has to record that the agent was
    # being taken out, or the retry's rollback re-enables a label with no job
    # behind it - a host whose desktop engine never comes back, reported as one
    # whose engine was restored.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")
    _knob(harness, "interrupt-after-bootout-Ciaobot")

    interrupted = _run_installer(harness, "--version", VERSION, "--migrate")

    assert interrupted.returncode == 130, interrupted.stdout
    assert _migration_receipt(harness)["phase"] == "retiring"
    assert _migration_receipt(harness)["retiring_desktop"] is True
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()

    # The retry, this time with a launchd that refuses the bootout. The agent is
    # already out of launchd from the interrupted run, so the rollback has to
    # load and start it again - and it rolls back to the originals the first run
    # snapshotted, not to whatever it had put in place.
    (home / "interrupt-after-bootout-Ciaobot").unlink(missing_ok=True)
    _knob(harness, "fail-launchctl-bootout-Ciaobot")
    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 1, retry.stdout
    assert "could not be retired" in retry.stderr
    log = _log(harness, "launchctl.log")
    desktop_plist = home / "Library/LaunchAgents/Ciaobot.plist"
    assert f"bootstrap gui/{os.getuid()} {desktop_plist}" in log
    assert f"kickstart -k gui/{os.getuid()}/Ciaobot" in log
    assert "Ciaobot.app's engine was restored" in retry.stderr
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    assert (home / ".local/bin/ciao").read_text(encoding="utf-8") == original_shim
    kept_plist = migration_before(harness) / "Ciaobot.plist"
    assert desktop_plist.read_bytes() == kept_plist.read_bytes()


@runs_the_sh_installer
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
    # This is the run that finishes the retirement, so it is also the one that
    # retires the bundle with it.
    assert not _app_bundle(tmp_path).exists()
    assert "Removed the retired Ciaobot.app." in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_completes_an_unfinished_no_start_handover(tmp_path: Path) -> None:
    # A `--migrate --no-start` run installs the tool, repoints `com.ciao.server`
    # at it and stops, with the app's own agent still in place and a receipt that
    # says `installed_no_start`. That repointed plist is what makes the retry
    # hard: the classifier reads it as an ordinary installer-managed engine, so
    # the ordinary install runs, exits 0, and leaves Ciaobot.app's own agent
    # loaded next to the engine that was supposed to replace it - in a receipt
    # that never settles. The retry has to finish the transaction the receipt
    # describes, from the originals it kept and in the workspace it recorded.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")

    first = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert first.returncode == 0, first.stderr
    assert _migration_receipt(harness)["phase"] == "installed_no_start"
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    # The plist really does point at the newly installed tool now, which is what
    # the retry's classifier reads as "nothing to migrate".
    with (home / "Library/LaunchAgents/com.ciao.server.plist").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"][0] == str(
            home / ".local/bin/ciao"
        )

    second = _run_installer(harness, "--version", VERSION, "--migrate")

    assert second.returncode == 0, second.stderr
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "migrated"
    assert receipt["kind"] == "desktop_host"
    assert receipt["workspace"] == str(workspace)
    # The app's own agent is retired rather than left to come back at the next
    # relaunch, and the engine it was replaced by is left running.
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    log = _log(harness, "launchctl.log")
    assert f"bootout gui/{os.getuid()}/Ciaobot" in log
    assert f"disable gui/{os.getuid()}/Ciaobot" in log
    assert f"bootout gui/{os.getuid()}/com.ciao.server" not in log
    # The originals are still the originals: the retry re-takes no snapshot over
    # them, or a rollback could no longer give Ciaobot.app its engine back.
    before = migration_before(harness)
    assert (before / "com.ciao.server.plist").read_bytes() == original_plist
    assert (before / "ciao").read_text(encoding="utf-8") == original_shim
    assert _install_receipt(harness)["service_backend"] == "launchd"
    # And the engine was set up in the workspace the receipt recorded, not in a
    # fresh one and not in $HOME/Ciaobot.
    ciao = home / ".local/bin/ciao"
    assert f"setup --workspace {workspace} --python {ciao} --yes --load-launchd\n" in (
        _log(harness, "ciao-calls.log")
    )
    # The hand-over completed on this run, so the bundle goes with it. The
    # classifier cannot name it here - the plist it reads is the one `ciao setup`
    # repointed - so this also pins that the transaction recorded it and the
    # resumed run acted on that record rather than skipping the removal.
    assert _migration_receipt(harness)["app_bundle"] == str(_app_bundle(tmp_path))
    assert not _app_bundle(tmp_path).exists()
    assert "Removed the retired Ciaobot.app." in second.stdout
    assert "ciao desktop uninstall" not in second.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_resumes_a_receipt_that_never_recorded_the_bundle(
    tmp_path: Path,
) -> None:
    # A transaction started by an installer that did not record the bundle it was
    # retiring. The retry can still finish the hand-over - the receipt carries
    # the kind, the workspace and the originals - but it has no way to learn
    # which bundle that was, because the plist it can read now points at the tool
    # the first run installed. So it may not fail on the missing field, and it
    # may not pass over the bundle in silence either: the app is still on disk,
    # inert but double-clickable, and saying nothing is the state #672 exists to
    # end. The manual way to remove it is named instead.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)

    first = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert first.returncode == 0, first.stderr
    receipt_path = home / ".local/state/ciaobot/migration/receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["app_bundle"] == str(_app_bundle(tmp_path))
    del receipt["app_bundle"]
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 0, retry.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    # The hand-over finished - the app's own agent is retired and gone - and the
    # bundle is still there, which is only honest because the run says so.
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert _app_bundle(tmp_path).is_dir()
    assert "Ciaobot.app could not be removed automatically" in retry.stdout
    assert f"'{home}/.local/bin/ciao' desktop uninstall" in retry.stdout
    assert "Removed the retired Ciaobot.app." not in retry.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_completes_an_interruption_after_setup(tmp_path: Path) -> None:
    # The same window by way of a signal: `ciao setup` has repointed the engine
    # plist at the new engine and the process goes away before the service is
    # started or the app's agent is retired. The receipt says `interrupted`, and
    # the retry has to read the hand-over out of that receipt - the same way it
    # does for a `retiring` one - instead of installing over the top of it and
    # leaving a desktop engine loaded next to the new one.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()
    _knob(harness, "interrupt-after-setup")

    interrupted = _run_installer(harness, "--version", VERSION, "--migrate")

    assert interrupted.returncode == 130, interrupted.stdout
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "interrupted"
    assert receipt["kind"] == "desktop_host"
    assert receipt["workspace"] == str(workspace)
    # Setup did its work before the process went away, so the retry's classifier
    # sees an installer-managed engine.
    with (home / "Library/LaunchAgents/com.ciao.server.plist").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"][0] == str(
            home / ".local/bin/ciao"
        )

    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 0, retry.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert f"disable gui/{os.getuid()}/Ciaobot" in _log(harness, "launchctl.log")
    # The before-image of the engine plist is still the desktop's own, so a
    # rollback out of this transaction would hand Ciaobot.app its engine back.
    assert (migration_before(harness) / "com.ciao.server.plist").read_bytes() == (
        original_plist
    )
    ciao = home / ".local/bin/ciao"
    assert f"setup --workspace {workspace} --python {ciao} --yes --load-launchd\n" in (
        _log(harness, "ciao-calls.log")
    )


@runs_the_sh_installer
@needs_local_tools
def test_migrate_as_host_completes_an_unfinished_no_start_handover(
    tmp_path: Path,
) -> None:
    # The same window as the test above, on the branch that needs an override to
    # enter at all. The user answered "this Mac is the host" with `--as-host` and
    # the receipt is where that answer is recorded, so a retry that only carries
    # `--migrate` - which is exactly what the transition release sends, and
    # exactly what a user re-running the one-liner types - has to finish the
    # hand-over rather than classify the repointed plist as an ordinary install
    # and exit 0 with Ciaobot.app's own agent still loaded beside it.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path, "{not json at all")
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()

    first = _run_installer(
        harness, "--version", VERSION, "--migrate", "--as-host", "--no-start"
    )

    assert first.returncode == 0, first.stderr
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "installed_no_start"
    assert receipt["kind"] == "desktop_invalid"
    # A host hand-over records no URL, and that absence is the record of the
    # decision: it is what tells a retry this was not a client run.
    assert receipt["host_url"] == ""
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    # Setup has repointed the engine plist, so the retry's own classifier reads
    # this Mac as an ordinary installer-managed engine - the whole reason the
    # receipt, and not the classifier, has to be what the retry trusts.
    with (home / "Library/LaunchAgents/com.ciao.server.plist").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"][0] == str(
            home / ".local/bin/ciao"
        )

    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 0, retry.stderr
    settled = _migration_receipt(harness)
    assert settled["phase"] == "migrated"
    # Finished as the hand-over it was, not re-decided into something else.
    assert settled["kind"] == "desktop_invalid"
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    log = _log(harness, "launchctl.log")
    assert f"bootout gui/{os.getuid()}/Ciaobot" in log
    assert f"disable gui/{os.getuid()}/Ciaobot" in log
    # The originals the first run snapshotted are still what a rollback would put
    # back, and the engine was set up in the workspace that hand-over recorded.
    assert (migration_before(harness) / "com.ciao.server.plist").read_bytes() == (
        original_plist
    )
    ciao = home / ".local/bin/ciao"
    assert f"setup --workspace {workspace} --python {ciao} --yes --load-launchd\n" in (
        _log(harness, "ciao-calls.log")
    )
    # The `--as-host` decision survived the interruption in the receipt, and so
    # did the bundle it was retiring: the resumed hand-over takes the app away
    # rather than leaving the dead one behind.
    assert _migration_receipt(harness)["app_bundle"] == str(_app_bundle(tmp_path))
    assert not _app_bundle(tmp_path).exists()
    assert "Removed the retired Ciaobot.app." in retry.stdout
    assert "ciao desktop uninstall" not in retry.stdout


@runs_the_sh_installer
@needs_local_tools
def test_migrate_as_host_completes_an_interruption_after_setup(tmp_path: Path) -> None:
    # The same branch, interrupted the other way: `ciao setup` has repointed the
    # engine plist and the process goes away before the service is started or the
    # app's agent is retired. The `interrupted` receipt names `desktop_invalid`
    # and carries no URL, so it is a host hand-over the user already decided -
    # and a plain `--migrate` retry has to finish it instead of answering `skip`
    # and installing over it.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path, "{not json at all")
    _knob(harness, "interrupt-after-setup")

    interrupted = _run_installer(
        harness, "--version", VERSION, "--migrate", "--as-host"
    )

    assert interrupted.returncode == 130, interrupted.stdout
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "interrupted"
    assert receipt["kind"] == "desktop_invalid"
    assert receipt["host_url"] == ""
    with (home / "Library/LaunchAgents/com.ciao.server.plist").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"][0] == str(
            home / ".local/bin/ciao"
        )

    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 0, retry.stderr
    assert _migration_receipt(harness)["phase"] == "migrated"
    assert not (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert f"disable gui/{os.getuid()}/Ciaobot" in _log(harness, "launchctl.log")
    ciao = home / ".local/bin/ciao"
    assert f"setup --workspace {workspace} --python {ciao} --yes --load-launchd\n" in (
        _log(harness, "ciao-calls.log")
    )


@runs_the_sh_installer
@needs_local_tools
def test_migrate_invalid_client_receipt_is_not_resumed_as_a_host_handover(
    tmp_path: Path,
) -> None:
    # The guard on the fix above: an empty host_url is what says "this was a
    # `--as-host` hand-over", so a receipt that records a URL has to stay on the
    # client side of the question. Reading it as a host hand-over would be the
    # worse mistake of the two - it runs `ciao setup` on a Mac whose state nobody
    # could read, and turns the very second writer the `--as-client` decision was
    # made to prevent. Here the interruption lands before the app's plist is
    # removed, so the classifier still says `desktop_invalid` and the retry has
    # to refuse with the same question rather than decide for the user.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path, "{not json at all")
    _knob(harness, "interrupt-after-bootout-Ciaobot")

    interrupted = _run_installer(
        harness, "--version", VERSION, "--migrate", "--as-client", "https://mini.ts.net"
    )

    assert interrupted.returncode == 130, interrupted.stdout
    receipt = _migration_receipt(harness)
    assert receipt["phase"] == "interrupted"
    assert receipt["kind"] == "desktop_invalid"
    assert receipt["host_url"] == "https://mini.ts.net"
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    # launchd was already asked for the three calls the client path makes before
    # the interruption; the retry must add none.
    after_interruption = _log(harness, "launchctl.log")

    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 1
    assert "--as-host" in retry.stderr
    # Not resumed as a host: nothing was installed, set up, or started, and the
    # app's own agent is exactly as the interrupted run left it.
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert "setup" not in _log(harness, "ciao-calls.log")
    assert _log(harness, "launchctl.log") == after_interruption
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    # The transaction is still open, and the receipt still says which way the
    # user answered it.
    assert _migration_receipt(harness)["phase"] == "interrupted"


@runs_the_sh_installer
@needs_local_tools
def test_migrate_retry_of_an_unfinished_handover_undoes_itself(tmp_path: Path) -> None:
    # Finishing an unfinished hand-over means the retry is a host transaction,
    # so a retry that fails has to undo itself against the originals the *first*
    # run snapshotted - the desktop shim, the desktop engine plist - and not
    # against the new engine already sitting in their places. Otherwise a failed
    # retry is a desktop Mac whose engine the first run replaced and the second
    # one removed, with nothing running to replace it.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    original_plist = (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes()
    original_shim = (home / ".local/bin/ciao").read_text(encoding="utf-8")

    first = _run_installer(harness, "--version", VERSION, "--migrate", "--no-start")

    assert first.returncode == 0, first.stderr
    _knob(harness, "fail-ciao-setup")
    retry = _run_installer(harness, "--version", VERSION, "--migrate")

    assert retry.returncode == 1
    assert "Ciaobot.app's engine was restored" in retry.stderr
    assert (home / "Library/LaunchAgents/com.ciao.server.plist").read_bytes() == (
        original_plist
    )
    assert (home / ".local/bin/ciao").read_text(encoding="utf-8") == original_shim
    assert (home / "Library/LaunchAgents/Ciaobot.plist").exists()
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    # This retry never got as far as booting the app's own agent out of launchd,
    # so the rollback only has to re-enable its label - the same distinction a
    # retirement makes.
    assert f"enable gui/{os.getuid()}/Ciaobot" in _log(harness, "launchctl.log")


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(lambda workspace: f"{workspace}/", id="trailing-slash"),
        pytest.param(
            lambda workspace: str(workspace.parent / "." / workspace.name), id="dot"
        ),
        pytest.param(lambda workspace: str(workspace.parent / "Link"), id="symlink"),
    ],
)
def test_migrate_host_accepts_its_own_workspace_spelled_differently(
    tmp_path: Path, spelling: Any
) -> None:
    # The rule is about the directory, not about the spelling of it: a trailing
    # slash, a `.` segment or a symlink all name the workspace the engine is
    # already running in, and refusing those would train users to drop the flag
    # instead of the path. They resolve to the same directory, so they are the
    # same workspace.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)
    (home / "Link").symlink_to(workspace)

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--workspace",
        spelling(workspace),
        "--no-start",
    )

    assert result.returncode == 0, result.stderr
    ciao = home / ".local/bin/ciao"
    # No `--yes` either: this workspace was typed, not detected, and the point of
    # the test is that a spelling of the right directory is the right directory.
    assert f"setup --workspace {workspace} --python {ciao}\n" in _log(
        harness, "ciao-calls.log"
    )
    assert _migration_receipt(harness)["workspace"] == str(workspace)
    assert _migration_receipt(harness)["phase"] == "installed_no_start"


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--no-start"], id="no-start"),
        pytest.param([], id="start"),
    ],
)
def test_migrate_host_refuses_a_different_workspace(
    tmp_path: Path, extra: list[str]
) -> None:
    # `--migrate` is a hand-over of the engine that is already running, and that
    # engine runs in one workspace: the one its own plist names, holding the
    # password, the provider keys, the runtime root and every chat in it. A
    # `--workspace` naming a different directory is not a move of that engine -
    # accepting it creates an empty workspace, repoints the service at it,
    # retires the app's agent and leaves the original and all of its data
    # behind, while the receipt still names the original. So it is refused while
    # nothing has been touched, and the refusal is the same with and without a
    # start: the mutation it prevents is the backup and the hand-over, not the
    # service.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = _desktop_install(harness, tmp_path)
    untouched = _replaced_state(harness)
    # An existing, perfectly good Ciaobot workspace - just not the one the
    # engine being handed over is running in. This is the case that cannot be
    # waved through as a typo: everything about it is valid, and taking it
    # anyway is the silent move this has to refuse.
    another = home / "Another"
    (another / ".runtime").mkdir(parents=True)
    (another / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    (another / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--workspace",
        str(another),
        *extra,
    )

    assert result.returncode == 1
    assert str(another) in result.stderr
    assert str(workspace) in result.stderr
    assert "Nothing on this Mac has been changed" in result.stderr
    # Nothing was decided, backed up, installed or launched.
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    assert not (home / ".local/state/ciaobot/migration").exists()
    assert _replaced_state(harness) == untouched
    # The workspace that was there, and everything in it, is where it was, and
    # the other one was not written to either.
    assert (workspace / ".env").read_text(encoding="utf-8") == "PWA_PORT=8443\n"
    assert sorted(p.name for p in workspace.iterdir()) == [".env", ".runtime"]
    assert sorted(p.name for p in another.iterdir()) == [".env", ".runtime"]


@runs_the_sh_installer
@needs_local_tools
@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(["--no-start"], id="no-start"),
        pytest.param([], id="start"),
    ],
)
def test_migrate_host_refuses_a_workspace_that_is_not_there(
    tmp_path: Path, extra: list[str]
) -> None:
    # A host hand-over never creates a workspace. `--migrate` on a Mac whose
    # engine is running keeps that engine's workspace, and a `--workspace` that
    # does not exist is either a typo or a request to make a new one - which
    # would be a fresh, empty workspace next to the real one, and the app's
    # engine retired in favour of it. Both are refused, before anything exists.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    untouched = _replaced_state(harness)
    missing = home / "NotThere"

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--workspace",
        str(missing),
        *extra,
    )

    assert result.returncode == 1
    assert "never creates a workspace" in result.stderr
    assert "Nothing on this Mac has been changed" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    assert not missing.exists()
    assert not (home / ".local/state/ciaobot/migration").exists()
    assert _replaced_state(harness) == untouched


def _desktop_install_without_a_workspace(
    harness: dict[str, Any], tmp_path: Path
) -> Path:
    """A live Ciaobot.app engine whose plists name no workspace at all, which is
    what the classifier reports as `desktop_invalid` with an empty workspace:
    there is nothing for it to recover, and `--as-host` is the only way on.

    The `~/Ciaobot` directory the fixture makes is emptied on the way out, so it
    reads as what it would be on such a Mac: an unrelated empty directory this
    run must leave alone rather than fill in.
    """
    home: Path = harness["home"]
    _desktop_install(harness, tmp_path)
    app_engine = (
        tmp_path
        / "Ciaobot.app"
        / "Contents"
        / "Resources"
        / "ciao-runtime"
        / "bin"
        / "ciao"
    )
    for label in ("com.ciao.server", "Ciaobot"):
        (home / f"Library/LaunchAgents/{label}.plist").write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": [str(app_engine), "run"],
                }
            )
        )
    (home / "Ciaobot" / ".env").unlink(missing_ok=True)
    shutil.rmtree(home / "Ciaobot" / ".runtime")
    return home


@runs_the_sh_installer
@needs_local_tools
def test_migrate_as_host_with_no_recoverable_workspace_asks_for_one(
    tmp_path: Path,
) -> None:
    # `--as-host` on a state this script cannot read is the user's decision
    # about a Mac the classifier cannot describe, and the one thing it still
    # cannot tell them is which workspace their data is in. A workspace it
    # cannot recover has to be named, and it has to be one that is already there:
    # inventing `~/Ciaobot` here is a second workspace created during a
    # hand-over, which is the outcome the refusals above exist to prevent.
    harness = _harness(tmp_path)
    home = _desktop_install_without_a_workspace(harness, tmp_path)
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate", "--as-host")

    assert result.returncode == 1
    assert "--workspace" in result.stderr
    assert "Nothing on this Mac has been changed" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    # A workspace was not invented, and nothing that was there was touched.
    assert list((home / "Ciaobot").iterdir()) == []
    assert not (home / ".local/state/ciaobot/migration").exists()
    assert _replaced_state(harness) == untouched

    # The same Mac, told which workspace to keep: an explicit one that exists is
    # accepted, because the user naming it is the decision the classifier could
    # not make.
    existing = home / "KeepMe"
    (existing / ".runtime").mkdir(parents=True)
    (existing / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    (existing / ".runtime" / "workspaces.json").write_text("[]\n", encoding="utf-8")

    accepted = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--as-host",
        "--workspace",
        str(existing),
        "--no-start",
    )

    assert accepted.returncode == 0, accepted.stderr
    ciao = home / ".local/bin/ciao"
    # No `--yes`: that flag is for a workspace this script detected rather than
    # one the user typed, and this one was typed.
    assert f"setup --workspace {existing} --python {ciao}\n" in _log(
        harness, "ciao-calls.log"
    )
    assert _migration_receipt(harness)["workspace"] == str(existing)
    assert _migration_receipt(harness)["phase"] == "installed_no_start"


@runs_the_sh_installer
@needs_local_tools
def test_migrate_as_host_accepts_a_pre_1_0_workspace_with_only_ciao_workspaces(
    tmp_path: Path,
) -> None:
    # Pre-1.0 path: an install configured only through CIAO_WORKSPACES in its
    # `.env` has no registry until it first starts on a release that imports
    # the variable, and is still the workspace to keep.
    harness = _harness(tmp_path)
    home = _desktop_install_without_a_workspace(harness, tmp_path)
    legacy = home / "Legacy"
    legacy.mkdir()
    (legacy / ".env").write_text(
        'PWA_PORT=8443\nCIAO_WORKSPACES=[{"name":"personal"}]\n', encoding="utf-8"
    )

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--as-host",
        "--workspace",
        str(legacy),
        "--no-start",
    )

    assert result.returncode == 0, result.stderr
    assert _migration_receipt(harness)["workspace"] == str(legacy)


@runs_the_sh_installer
@needs_local_tools
def test_migrate_as_host_refuses_a_workspace_that_was_never_set_up(tmp_path: Path) -> None:
    # An existing directory is not yet a Ciaobot workspace. Handing an engine
    # over to one with no `.runtime/workspaces.json` would start it with a fresh password and a
    # fresh runtime root next to the real ones, which is the same second
    # workspace as creating a new one - so it is refused here too, where it is
    # still a refusal and not an install.
    harness = _harness(tmp_path)
    home = _desktop_install_without_a_workspace(harness, tmp_path)
    untouched = _replaced_state(harness)
    empty = home / "NotAWorkspace"
    empty.mkdir()

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--as-host",
        "--workspace",
        str(empty),
    )

    assert result.returncode == 1
    assert "no .runtime/workspaces.json" in result.stderr
    assert "Nothing on this Mac has been changed" in result.stderr
    assert "tool install" not in _log(harness, "uv-calls.log")
    assert _log(harness, "launchctl.log") == ""
    assert not (home / ".local/state/ciaobot/migration").exists()
    assert _replaced_state(harness) == untouched


@runs_the_sh_installer
@needs_local_tools
def test_migrate_client_ignores_a_workspace_it_never_uses(tmp_path: Path) -> None:
    # The workspace rules above are about a host hand-over. A client installs no
    # workspace at all - it gets no setup, no service and no agent, so it never
    # becomes a second writer for a runtime root the host owns - and there is
    # nothing there for a `--workspace` to override.
    harness = _harness(tmp_path)
    _desktop_install(
        harness, tmp_path, {"role": "standby", "host_url": "https://mini.ts.net"}
    )

    result = _run_installer(
        harness,
        "--version",
        VERSION,
        "--migrate",
        "--workspace",
        str(harness["home"] / "Another"),
    )

    assert result.returncode == 0, result.stderr
    assert not (harness["home"] / "Another").exists()
    assert "setup" not in _log(harness, "ciao-calls.log")
    assert _migration_receipt(harness)["phase"] == "migrated_client"


@runs_the_sh_installer
@needs_local_tools
def test_ordinary_install_runs_the_browser_bootstrap(tmp_path: Path) -> None:
    # A Mac that never ran Ciaobot.app has no workspace to keep. The installer
    # installs the engine, opens the wizard, and lets the wizard provision the
    # folder the user confirms: nothing may be created before that.
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    workspace = home / "Fresh"

    result = _run_installer(
        harness, "--version", VERSION, "--workspace", str(workspace)
    )

    assert result.returncode == 0, result.stderr
    calls = _log(harness, "ciao-calls.log").splitlines()
    bootstrap = [line for line in calls if line.startswith("run --installer-bootstrap")]
    assert len(bootstrap) == 1
    run_id = bootstrap[0].split("--installer-run-id ")[1].split()[0]
    assert f"--installer-suggest-workspace={workspace}" in bootstrap[0]
    assert f"installer activate --run-id {run_id} --json" in calls
    assert not any(line.startswith("setup") for line in calls)
    # The folder did not exist when the wizard started: the installer had not
    # created it, and the suggestion is only a suggestion.
    assert f"bootstrap {workspace} existed=0" in _log(harness, "bootstrap.log")
    assert _install_receipt(harness)["version"] == VERSION
    assert "migrated" not in _log(harness, "ciao-calls.log")
    assert not (home / ".local/state/ciaobot/migration").exists()

def _denial_record(harness, mtime_offset: float) -> str:
    """Write the supervisor's record with an mtime relative to now.

    The installer only believes a record written after its health wait began, so
    a future mtime stands for "written during the wait" and a past one for stale.
    """
    line = "macOS denied Ciaobot Server access to /w: allow it in System Settings."
    record = harness["home"] / ".local/state/ciaobot/workspace-access-denied.txt"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(line + "\n", encoding="utf-8")
    stamp = time.time() + mtime_offset
    os.utime(record, (stamp, stamp))
    return line


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_reports_a_workspace_access_denial(tmp_path: Path) -> None:
    # A macOS Files & Folders denial keeps the engine from ever answering. The
    # supervisor records why outside the workspace; the abort reason names it.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    (harness["home"] / "startup-version").write_text("0.0.1\n", encoding="utf-8")
    line = _denial_record(harness, 3600)
    untouched = _replaced_state(harness)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert line in result.stderr
    assert _migration_receipt(harness)["phase"] == "rolled_back"
    assert _replaced_state(harness) == untouched


@runs_the_sh_installer
@needs_local_tools
def test_migrate_host_ignores_a_stale_workspace_access_denial(tmp_path: Path) -> None:
    # A record from an earlier run says nothing about this one.
    harness = _harness(tmp_path)
    _desktop_install(harness, tmp_path)
    (harness["home"] / "startup-version").write_text("0.0.1\n", encoding="utf-8")
    line = _denial_record(harness, -3600)

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 1
    assert line not in result.stderr
    assert f"with version {VERSION}" in result.stderr


# ── the port the engine binds is taken (#1654) ────────────────────────────


def _hold_port(harness: dict[str, Any]) -> int:
    """A port really held by a socket, and the number it was given.

    The installer's preflight is the engine's own bind, so a port that is
    only *reported* as busy would not fail it: the socket has to be there.
    A wildcard bind is what the engine does, so that is what is held. The
    fake engine is moved off the port, so the port is held by a foreign
    listener and not by Ciaobot's own engine.

    The wildcard address is load-bearing here, and not an accident: the
    engine binds ``0.0.0.0`` (``DEFAULT_PWA_HOST``), and a listener on
    ``127.0.0.1`` does **not** block a wildcard bind - the probe would
    succeed and the port would look free. Only a wildcard listener makes
    the engine's own bind fail, which is the case under test. The socket
    listens on an ephemeral port for the length of one installer run and
    accepts nothing.
    """
    import socket

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    # The socket is closed on the next line's process exit; the port stays
    # held for the length of the installer run through this helper's
    # caller, so the fd is kept in the module-level registry below.
    _HELD_PORTS.append(holder)
    (harness["home"] / "engine-port").write_text("1\n", encoding="utf-8")
    return port


# The sockets the port tests hold, so they are closed when the test ends.
_HELD_PORTS: list[Any] = []


@runs_the_sh_installer
@needs_local_tools
def test_installer_refuses_to_install_over_a_port_something_else_holds(
    tmp_path: Path,
) -> None:
    """A port held by another program is refused before the service starts.

    The engine would die on the bind and the service manager would respawn
    it forever, so the installer checks the port first and says which port
    it is and how to move it.
    """
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    # The port the workspace will use, and a program that holds it. The
    # probe is the engine's own bind, so the port has to be really held:
    # a socket on it is what makes the probe fail.
    port = _hold_port(harness)
    _existing_install(harness, port=str(port))
    _write_exec(
        harness["fakebin"] / "lsof",
        "#!/bin/sh\n"
        "printf 'lsof %s\\n' \"$*\" >> \"$HOME/trace.log\"\n"
        "printf 'COMMAND  PID  USER  FD  TYPE  DEVICE  SIZE/OFF  NODE  NAME\\n'\n"
        f"printf 'tailscaled  418  root  9u  IPv4  0x1  0t0  TCP *:{port} (LISTEN)\\n'\n"
        "exit 0\n",
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1, result.stdout
    assert str(port) in result.stderr
    assert "already in use" in result.stderr
    # The two ways out.
    assert "tailscaled" in result.stderr
    assert "PWA_PORT" in result.stderr
    # The service is never registered: nothing is started on a port the
    # engine cannot bind.
    assert "service start" not in _log(harness, "ciao-calls.log")


@runs_the_sh_installer
@needs_local_tools
def test_installer_refuses_to_install_over_a_port_something_else_holds_no_lsof(
    tmp_path: Path,
) -> None:
    """Without lsof the port is still named; only the owner is unknown."""
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    port = _hold_port(harness)
    _existing_install(harness, port=str(port))

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1, result.stdout
    assert str(port) in result.stderr
    assert "already in use" in result.stderr
    assert "PWA_PORT" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_installer_accepts_a_port_ciaobots_own_engine_holds(
    tmp_path: Path,
) -> None:
    """A re-install of a running engine is legitimate.

    The port is held — by Ciaobot's own engine, which answers on it — and
    the install goes ahead: refusing there would make every upgrade of a
    running engine fail.
    """
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    # The engine answers on the port the workspace names, and the port is
    # really held by it.
    port = _hold_port(harness)
    (home / "engine-port").write_text(f"{port}\n", encoding="utf-8")
    _existing_install(harness, port=str(port))

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "already in use" not in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_the_port_owner_name_cannot_smuggle_terminal_escapes(
    tmp_path: Path,
) -> None:
    """The name of the program holding the port is printed to a terminal.

    It is the name the holding process chose for itself, so it can carry a
    carriage return or an ANSI escape. Either would reach the operator's
    terminal verbatim and could rewrite the line the message appears on, so
    only printable bytes survive.
    """
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    port = _hold_port(harness)
    _existing_install(harness, port=str(port))
    _write_exec(
        harness["fakebin"] / "lsof",
        "#!/bin/sh\n"
        "printf 'COMMAND  PID  USER  FD  TYPE  DEVICE  SIZE/OFF  NODE  NAME\\n'\n"
        f"printf 'evil\\033[2K\\r  418  root  9u  IPv4  0x1  0t0  TCP *:{port} (LISTEN)\\n'\n"
        "exit 0\n",
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1, result.stdout
    assert str(port) in result.stderr
    # The escape and the carriage return are gone; the name is still there.
    assert "\033" not in result.stderr
    assert "\r" not in result.stderr
    assert "evil" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_a_process_that_merely_answers_on_the_port_is_not_the_engine(
    tmp_path: Path,
) -> None:
    """The exemption is for Ciaobot's own engine, not for anything that answers.

    A hostile process holding the port could answer the health check with
    anything. The engine's startup status carries a "version" field, so a
    response without one is not the engine and the install is refused.
    """
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    port = _hold_port(harness)
    _existing_install(harness, port=str(port))
    _write_exec(
        harness["fakebin"] / "curl",
        "#!/bin/sh\n"
        "printf 'curl %s\\n' \"$*\" >> \"$HOME/trace.log\"\n"
        "case \"$*\" in\n"
        "    */api/startup-status)\n"
        "        # Something is listening, and it answers - but it is not the\n"
        "        # engine: no version field.\n"
        "        printf 'hello\\n'\n"
        "        exit 0\n"
        "        ;;\n"
        "esac\n"
        f'exec "{shutil.which("curl") or "curl"}" "$@"\n',
    )

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 1, result.stdout
    assert str(port) in result.stderr
    assert "already in use" in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_a_port_probe_that_cannot_run_does_not_refuse_the_install(
    tmp_path: Path,
) -> None:
    """A probe that could not run says nothing about the port.

    Reading a probe that failed as "the port is taken" would refuse an
    install on a guess — a wheel that would not import, or an interpreter
    that would not start, is not evidence about the port.
    """
    harness = _harness(tmp_path)
    home: Path = harness["home"]
    (home / "port-probe-cannot-run").write_text("", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "already in use" not in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_installer_says_why_the_engine_never_came_up(tmp_path: Path) -> None:
    """The engine's own answer is read, and it names the port.

    A `tailscale serve` holding the port leaves the engine dead; the
    installer waited 60 s and then printed "the engine is still starting"
    and a sign-in link that could not work. It now says which port, and
    prints no link.
    """
    harness = _harness(tmp_path)
    _existing_install(harness)
    home: Path = harness["home"]
    (home / "Ciaobot" / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    (home / "startup-port-in-use").write_text("8443\n", encoding="utf-8")

    result = _run_installer(harness, "--version", VERSION)

    assert "8443" in result.stderr
    assert "already in use" in result.stderr
    assert "PWA_PORT" in result.stderr
    # The dead link is the point: it must not be printed.
    assert "Open Ciaobot:" not in result.stdout


@runs_the_sh_installer
@needs_local_tools
def test_installer_reads_the_record_the_engine_left_outside_the_workspace(
    tmp_path: Path,
) -> None:
    """An engine that cannot bind cannot answer the startup status.

    So the reason is read from the record the engine leaves outside the
    workspace, the way the workspace-access denial is read (#1654).
    """
    harness = _harness(tmp_path)
    _existing_install(harness)
    home: Path = harness["home"]
    (home / "Ciaobot" / ".env").write_text("PWA_PORT=8443\n", encoding="utf-8")
    record = home / ".local/state/ciaobot/port-in-use.txt"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        "port 8443 is already in use by another program. Free port 8443, or "
        "set PWA_PORT in the workspace .env to serve on a different port.\n",
        encoding="utf-8",
    )
    # Written before the installer's wait began, so it says nothing about
    # this run: the installer must not believe a stale record.
    stamp = time.time() - 3600
    os.utime(record, (stamp, stamp))

    result = _run_installer(harness, "--version", VERSION)

    # The stale record is not this run's failure, so the install carries on
    # and the engine's own answer is what counts.
    assert "Open Ciaobot:" in result.stdout
    assert "already in use" not in result.stderr


@runs_the_sh_installer
@needs_local_tools
def test_installer_still_prints_the_link_when_the_engine_came_up(
    tmp_path: Path,
) -> None:
    """The ordinary path is unchanged: a working engine gets its link."""
    harness = _harness(tmp_path)
    _existing_install(harness)

    result = _run_installer(harness, "--version", VERSION)

    assert result.returncode == 0, result.stderr
    assert "Open Ciaobot: http://localhost:8443/?setup=tok" in result.stdout
