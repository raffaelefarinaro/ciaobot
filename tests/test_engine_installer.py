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


# A uv stub. `run` hands the embedded verifier to the real interpreter, `tool
# dir` answers, `tool list` is empty (nothing installed yet), and `tool install`
# fabricates a tool environment whose interpreter and `ciao` are scripts that
# only record how they were called. Nothing here installs anything.
_UV_STUB = """#!/bin/sh
set -eu
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
            list) ;;
            install)
                tools="$HOME/.local/share/uv/tools"
                mkdir -p "$tools/ciaobot/bin" "$HOME/.local/bin"
                cat > "$tools/ciaobot/bin/python" <<EOF
#!/bin/sh
PYTHONPATH="__REPO_ROOT__" exec "__PYTHON__" "\\$@"
EOF
                chmod 755 "$tools/ciaobot/bin/python"
                cat > "$HOME/.local/bin/ciao" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$HOME/ciao-calls.log"
if [ "${1:-}" = "setup-url" ]; then
    echo "http://localhost:8443/?setup=tok"
fi
EOF
                chmod 755 "$HOME/.local/bin/ciao"
                ;;
        esac
        ;;
esac
"""

def _fake_uv() -> str:
    return _UV_STUB.replace("__REPO_ROOT__", str(REPO_ROOT)).replace(
        "__PYTHON__", sys.executable
    )


def _harness(tmp_path: Path) -> dict[str, Any]:
    """A signed release on `file://`, a fake uv, a fake `$HOME`, and the script
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
    # launchd records what it was asked to do and always succeeds; the app and
    # `pgrep` are not there, which is the state of a Mac with no Ciaobot.app.
    _write_exec(
        fakebin / "launchctl", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/launchctl.log"\n'
    )
    _write_exec(fakebin / "osascript", "#!/bin/sh\nexit 1\n")
    _write_exec(fakebin / "pgrep", "#!/bin/sh\nexit 1\n")

    # The four tools a migration reaches for are script variables, so the test
    # rewrites the lines that set them instead of setting environment
    # variables: a run here must never touch this Mac's real launchd, the
    # running Ciaobot.app, or a real `pgrep`.
    rewritten = re.sub(
        r'^RELEASE_PUBLIC_KEY=".*?"$',
        f'RELEASE_PUBLIC_KEY="{public_key}"',
        SCRIPT_TEXT,
        count=1,
        flags=re.MULTILINE,
    )
    rewritten = re.sub(
        r"^(PLISTBUDDY|LAUNCHCTL|OSASCRIPT|PGREP)=.*$",
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
def test_migrate_is_a_noop_after_success(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    migration = harness["home"] / ".local/state/ciaobot/migration"
    migration.mkdir(parents=True)
    (migration / "receipt.json").write_text(
        json.dumps({"schema": 1, "kind": "desktop_host", "phase": "migrated"}),
        encoding="utf-8",
    )

    result = _run_installer(harness, "--version", VERSION, "--migrate")

    assert result.returncode == 0, result.stderr
    assert "already migrated" in result.stdout
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
