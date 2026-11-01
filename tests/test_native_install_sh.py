"""Shell tests for `install.sh --native-install` (#2405).

The public entry point drives the Mac client. Its shell half validates the
flags, refuses a Mac that is not fresh, bootstraps the digest-pinned uv,
verifies the signed release and then hands the events descriptor to the
coordinator. Every test here runs the real script against the fake tools from
`tests/test_engine_installer.py`, a fake `$HOME`, and a fake coordinator that
records its argv and writes its own events. Nothing touches the real launchd,
the operator's home, the network, uv or `~/.local`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from ciao import native_installer as native
from ciao.release_manifest import SERVER_HOST_FILENAME
from tests.test_engine_installer import (
    SCRIPT_TEXT,
    UV_SHA256,
    VERSION,
    WHEEL_NAME,
    _harness,
    _run_installer,
    _uv_archive,
    _write_exec,
    needs_local_tools,
    runs_the_sh_installer,
)
from tests.test_native_installer import RUN_ID, _drain, _public_event_pipe

pytestmark = [runs_the_sh_installer, needs_local_tools]

WHEEL_BYTES = b"the signed wheel"
HOST_BYTES = b"the signed universal host archive"
UV_NAME = "uv-aarch64-apple-darwin.tar.gz"
UV_DIR = "uv-aarch64-apple-darwin"
OTHER_RUN_ID = "7a1c2b3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"

# Fake tools. Every one reports its descriptor state (`s=open|closed`) so a test
# can prove which child inherited the public events descriptor.
_PROBE = (
    'if ( true >&"$(cat "$HOME/native-event-fd" 2>/dev/null)" ) 2>/dev/null; '
    "then s=open; else s=closed; fi"
)

_FAKE_CURL = """#!/bin/sh
__PROBE__
printf 'curl %s %s\\n' "$s" "$*" >> "$HOME/fd.log"
printf 'curl %s\\n' "$*" >> "$HOME/trace.log"
case "$*" in
    *ciaobot-engine-manifest.json*)
        if [ -f "$HOME/term-on-manifest" ]; then
            # The shell is the parent of this curl: a signal to it is a cancel.
            kill -TERM "$PPID"
            exit 1
        fi
        ;;
esac
out=
previous=
for arg in "$@"; do
    if [ "$previous" = -o ]; then out=$arg; fi
    previous=$arg
done
case "$*" in
    *astral-sh/uv/releases/download/*)
        cp "__UVTAR__" "$out"
        exit $?
        ;;
esac
exec "__FAKEBIN__/curl-release" "$@"
"""

_NATIVE_UV = """#!/bin/sh
__PROBE__
printf 'native-uv %s %s\\n' "$s" "$*" >> "$HOME/fd.log"
case "${1:-}" in
    tool)
        printf '%s\\n' "$*" >> "$HOME/uv-tool-calls.log"
        exit 1
        ;;
    venv)
        dir=
        for arg in "$@"; do dir=$arg; done
        mkdir -p "$dir/bin"
        cp "__VENV_PY__" "$dir/bin/python"
        chmod 755 "$dir/bin/python"
        exit 0
        ;;
    pip)
        if [ -f "$HOME/fail-pip" ]; then exit 1; fi
        exit 0
        ;;
    run)
        exec "__FAKEBIN__/uv" "$@"
        ;;
esac
exit 1
"""

# The coordinator's python. `-I -m ciao.native_installer` is the fake
# coordinator; the protocol probe and the host selector run the real repo code.
_VENV_PYTHON = """#!/bin/sh
__PROBE__
if [ "${1:-}" = -I ]; then shift; fi
if [ "${1:-}" = -m ] && [ "${2:-}" = ciao.native_installer ]; then
    printf 'coordinator %s\\n' "$s" >> "$HOME/fd.log"
    shift 2
    exec "__SYS_PY__" "__FAKE_COORD__" "$@"
fi
printf 'python %s %s\\n' "$s" "$*" >> "$HOME/fd.log"
if [ "${1:-}" = -c ] && [ -f "$HOME/old-release" ]; then exit 1; fi
PYTHONPATH="__REPO__" exec "__SYS_PY__" "$@"
"""

_FAKE_COORDINATOR = '''\
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
stop = []
signal.signal(signal.SIGTERM, lambda signum, frame: stop.append(signum))

parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--run-id", required=True)
parser.add_argument("--events-fd", type=int, required=True)
parser.add_argument("--first-seq", type=int, required=True)
parser.add_argument("--prior-events", type=int, required=True)
parser.add_argument("--version", required=True)
parser.add_argument("--uv", required=True)
parser.add_argument("--uv-sha256", required=True)
parser.add_argument("--wheel", required=True)
parser.add_argument("--wheel-sha256", required=True)
parser.add_argument("--manifest", required=True)
parser.add_argument("--signature", required=True)
parser.add_argument("--host-archive", required=True)
parser.add_argument("--workspace", default=None)
args = parser.parse_args(sys.argv[1:])
fd = args.events_fd
seq = args.first_seq


def emit(body):
    global seq
    line = {"schema": 1, "run_id": args.run_id, "seq": seq}
    line.update(body)
    os.write(fd, (json.dumps(line, separators=(",", ":")) + "\\n").encode())
    seq += 1


def argv_record():
    (home / "coordinator-argv.json").write_text(json.dumps(sys.argv[1:]))


if (home / "coordinator-wait-term").exists():
    (home / "coordinator-pid").write_text(str(os.getpid()))
    argv_record()
    while not stop:
        time.sleep(0.05)
    emit({"type": "failed", "code": "cancelled"})
    raise SystemExit(1)

outcome = (home / "coordinator-outcome").read_text().strip() if (
    home / "coordinator-outcome"
).exists() else "success"
argv_record()
if outcome == "silent":
    raise SystemExit(1)
emit({"type": "phase", "phase": "install"})
emit({"type": "phase", "phase": "wizard"})
emit({"type": "wizard_ready", "origin": "http://127.0.0.1:54321"})
emit({"type": "setup_saved"})
emit({"type": "phase", "phase": "activation"})
emit({"type": "phase", "phase": "readiness"})
if outcome == "failed":
    emit({"type": "failed", "code": "service_not_ready"})
    raise SystemExit(1)
emit({"type": "service_ready", "origin": "http://127.0.0.1:8443"})
raise SystemExit(0)
'''


def _line(seq: int, body: dict[str, Any]) -> bytes:
    payload = {"schema": 1, "run_id": RUN_ID, "seq": seq, **body}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _events(payload: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in payload.decode().splitlines()]


def _read(home: Path, name: str) -> str:
    path = home / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _native_harness(tmp_path: Path, *, host: bool = True) -> dict[str, Any]:
    """The legacy fake release, with the native uv, curl and coordinator."""
    host_archive = None
    if host:
        build = tmp_path / "host-build"
        build.mkdir()
        host_archive = build / SERVER_HOST_FILENAME
        host_archive.write_bytes(HOST_BYTES)
    harness = _harness(tmp_path, host_archive=host_archive)
    home: Path = harness["home"]
    fakebin: Path = harness["fakebin"]
    (home / "cwd").mkdir()

    # The verified uv is a script that execs the native fake uv. The archive is
    # built with the exact member layout the shell allowlists.
    uv_wrapper = f'#!/bin/sh\nexec "{fakebin}/native-uv" "$@"\n'.encode()
    uv_tar = tmp_path / UV_NAME
    _uv_archive(
        uv_tar,
        [(f"{UV_DIR}/", "dir", b""), (f"{UV_DIR}/uv", "file", uv_wrapper)],
    )
    digests = {UV_NAME: hashlib.sha256(uv_tar.read_bytes()).hexdigest()}
    text, count = re.subn(
        r"(uv-[a-z0-9_-]+\.tar\.gz)\) printf '%s\\n' [0-9a-f]{64} ;;",
        lambda m: (
            f"{m.group(1)}) printf '%s\\n' "
            f"{digests.get(m.group(1), UV_SHA256[m.group(1)])} ;;"
        ),
        harness["script"].read_text(encoding="utf-8"),
    )
    assert count == 6
    harness["script"].write_text(text, encoding="utf-8")

    venv_python = tmp_path / "venv-python.sh"
    venv_python.write_text(
        _VENV_PYTHON.replace("__PROBE__", _PROBE)
        .replace("__SYS_PY__", sys.executable)
        .replace("__FAKE_COORD__", str(tmp_path / "fake_coordinator.py"))
        .replace("__REPO__", str(Path(__file__).parents[1])),
        encoding="utf-8",
    )
    (tmp_path / "fake_coordinator.py").write_text(_FAKE_COORDINATOR, encoding="utf-8")

    (fakebin / "curl").rename(fakebin / "curl-release")
    _write_exec(
        fakebin / "curl",
        _FAKE_CURL.replace("__PROBE__", _PROBE)
        .replace("__UVTAR__", str(uv_tar))
        .replace("__FAKEBIN__", str(fakebin)),
    )
    _write_exec(
        fakebin / "native-uv",
        _NATIVE_UV.replace("__PROBE__", _PROBE)
        .replace("__VENV_PY__", str(venv_python))
        .replace("__FAKEBIN__", str(fakebin)),
    )
    harness["uv_tar"] = uv_tar
    harness["uv_exec_sha"] = hashlib.sha256(uv_wrapper).hexdigest()
    harness["wheel_sha"] = hashlib.sha256(WHEEL_BYTES).hexdigest()
    harness["release_wheel"] = harness["release"] / WHEEL_NAME
    return harness


def _native_env(h: dict[str, Any], extra: dict[str, str] | None) -> dict[str, str]:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not re.match(r"^(CIAO|PWA|UV_TOOL)_", key)
        and key not in {"XDG_BIN_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"}
    }
    return {**inherited, **h["env"], **(extra or {})}


def _default_argv(fd: int, **_: object) -> list[str]:
    return ["--version", VERSION, "--native-install", "--events-fd", str(fd), "--run-id", RUN_ID]


def _run(
    h: dict[str, Any],
    argv_for: Callable[[int], list[str]] = _default_argv,
    *,
    mode: str = "normal",
    extra_args: tuple[str, ...] = (),
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], bytes, int]:
    """Run the script with a fresh public pipe.

    Modes: `normal` passes the write end; `readonly` passes a read-only
    descriptor at the target number; `stdout` aliases stdout to the pipe;
    `closed` passes nothing and names a descriptor the child never inherits.
    """
    with _public_event_pipe() as (read_fd, fd):
        if mode == "readonly":
            os.dup2(read_fd, fd)
        (h["home"] / "native-event-fd").write_text(f"{fd}\n", encoding="utf-8")
        pass_fds: tuple[int, ...] = () if mode == "closed" else (fd,)
        completed = subprocess.run(
            ["sh", str(h["script"]), *argv_for(fd), *extra_args],
            env=_native_env(h, env),
            cwd=str(cwd or h["home"] / "cwd"),
            pass_fds=pass_fds,
            stdin=subprocess.DEVNULL,
            stdout=fd if mode == "stdout" else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        os.close(fd)
        payload = _drain(read_fd)
    return completed, payload, fd


def _argv_record(h: dict[str, Any]) -> list[str] | None:
    path = h["home"] / "coordinator-argv.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _trace(h: dict[str, Any]) -> str:
    return _read(h["home"], "trace.log")


def _snapshot(home: Path) -> dict[str, object]:
    """Every file, link and directory under `home`, minus the fake tools' logs."""
    logs = {"trace.log", "launchctl.log", "fd.log", "responders.log", "native-event-fd"}
    state: dict[str, object] = {}
    for path in sorted(home.rglob("*")):
        rel = str(path.relative_to(home))
        if path.name in logs:
            continue
        if path.is_symlink():
            state[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            state[rel] = "dir"
        elif path.is_file():
            state[rel] = path.read_bytes()
        else:
            state[rel] = "other"
    return state


def _expect_fixed_failure(
    completed: subprocess.CompletedProcess[bytes], payload: bytes, code: str
) -> None:
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["failed"]
    assert events[0]["code"] == code
    assert events[0]["seq"] == 1


# --- the success path ---------------------------------------------------------


def test_native_mode_streams_shell_phases_then_hands_over(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload, fd = _run(h)
    assert completed.returncode == 0, completed.stderr

    lines = payload.splitlines(keepends=True)
    assert lines[:3] == [
        _line(1, {"type": "phase", "phase": "preflight"}),
        _line(2, {"type": "phase", "phase": "bootstrap"}),
        _line(3, {"type": "phase", "phase": "download"}),
    ]
    coordinator = _events(payload)[3:]
    assert [e["seq"] for e in coordinator] == list(range(4, 11))
    assert [e["type"] for e in coordinator][-1] == "service_ready"

    argv = _argv_record(h)
    assert argv is not None
    assert argv[0] == "run"
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert pairs["--first-seq"] == "4"
    assert pairs["--prior-events"] == "3"
    assert pairs["--events-fd"] == str(fd)
    assert pairs["--run-id"] == RUN_ID
    assert pairs["--version"] == VERSION
    assert pairs["--uv"].endswith("/uv-bootstrap/uv")
    assert pairs["--uv"] != str(h["fakebin"] / "uv")
    assert pairs["--uv-sha256"] == h["uv_exec_sha"]
    assert pairs["--uv-sha256"] != hashlib.sha256(h["uv_tar"].read_bytes()).hexdigest()
    assert pairs["--wheel-sha256"] == h["wheel_sha"]
    run_dir = pairs["--wheel"].rsplit("/", 1)[0]
    assert run_dir.startswith(str(tmp_path / "ciaobot-engine."))
    for key in ("--wheel", "--manifest", "--signature", "--host-archive"):
        assert pairs[key].startswith(run_dir + "/")
    assert "--workspace" not in argv
    assert not (h["home"] / "uv-tool-calls.log").exists()
    assert not list(tmp_path.glob("ciaobot-engine.*"))
    _assert_the_real_coordinator_accepts(argv)


def _assert_the_real_coordinator_accepts(argv: list[str]) -> None:
    """The shell's handover argv parses with the coordinator's real CLI.

    The fake coordinator records what the shell sent; this feeds the same argv
    to ``native_installer.main`` with the install itself stubbed, so a flag the
    shell sends and the coordinator does not know (or the reverse) fails here
    rather than on a real Mac.
    """
    seen: dict[str, object] = {}

    def fake_run(request: object, artifacts: object, **_: object) -> object:
        seen["request"] = request
        seen["artifacts"] = artifacts
        return SimpleNamespace(exit_code=0)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(native, "run_native_install", fake_run)
        assert native.main(argv) == 0
    pairs = dict(zip(argv[1::2], argv[2::2]))
    artifacts = seen["artifacts"]
    assert getattr(artifacts, "uv_sha256") == pairs["--uv-sha256"]
    assert getattr(artifacts, "wheel_sha256") == pairs["--wheel-sha256"]
    assert str(getattr(artifacts, "host_archive")) == pairs["--host-archive"]
    assert getattr(seen["request"], "first_seq") == int(pairs["--first-seq"])


def test_native_mode_passes_the_workspace_suggestion(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    workspace = h["home"] / "Elsewhere"
    completed, _, _ = _run(
        h, lambda fd: _default_argv(fd), extra_args=("--workspace", str(workspace))
    )
    assert completed.returncode == 0, completed.stderr
    argv = _argv_record(h)
    assert argv is not None
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert pairs["--workspace"] == str(workspace)


# --- argument and descriptor validation --------------------------------------

# Refused before any event: the descriptor or run id is unusable.
_ZERO_BYTE_CASES = {
    "fd-2": ("normal", lambda fd: _argv_with_fd("2")),
    "fd-10": ("normal", lambda fd: _argv_with_fd("10")),
    "fd-x": ("normal", lambda fd: _argv_with_fd("x")),
    "fd-closed": ("closed", lambda fd: _argv_with_fd("8")),
    "fd-readonly": ("readonly", lambda fd: _argv_with_fd(str(fd))),
    "fd-stdout": ("stdout", lambda fd: _argv_with_fd(str(fd))),
    "run-id-upper": ("normal", lambda fd: _argv_with_run_id(fd, RUN_ID.upper())),
    "run-id-short": ("normal", lambda fd: _argv_with_run_id(fd, RUN_ID[:-1])),
}

# Refused after the channel opens: exactly one failed(invalid_arguments).
_EVENT_CASES = {
    "no-version": lambda fd: ["--native-install", "--events-fd", str(fd), "--run-id", RUN_ID],
    "migrate": lambda fd: _default_argv(fd) + ["--migrate"],
    "as-host": lambda fd: _default_argv(fd) + ["--as-host"],
    "as-client": lambda fd: _default_argv(fd) + ["--as-client", "https://example.test"],
    "no-start": lambda fd: _default_argv(fd) + ["--no-start"],
    "ws-relative": lambda fd: _default_argv(fd) + ["--workspace", "relative/dir"],
    "ws-dotdot": lambda fd: _default_argv(fd) + ["--workspace", "/tmp/a/../b"],
}


def _argv_with_fd(value: str) -> list[str]:
    return ["--version", VERSION, "--native-install", "--events-fd", value, "--run-id", RUN_ID]


def _argv_with_run_id(fd: int, run_id: str) -> list[str]:
    return ["--version", VERSION, "--native-install", "--events-fd", str(fd), "--run-id", run_id]


@pytest.mark.parametrize("case", sorted(_ZERO_BYTE_CASES))
def test_native_mode_validates_flags_and_descriptor(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    mode, argv_for = _ZERO_BYTE_CASES[case]
    completed, payload, _ = _run(h, argv_for, mode=mode)
    assert completed.returncode != 0
    assert payload == b""
    assert "curl" not in _trace(h)
    assert not (h["home"] / "fd.log").exists()


@pytest.mark.parametrize("case", sorted(_EVENT_CASES))
def test_native_mode_refuses_bad_arguments_with_one_event(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    completed, payload, _ = _run(h, _EVENT_CASES[case])
    _expect_fixed_failure(completed, payload, "invalid_arguments")
    assert "curl" not in _trace(h)
    assert not (h["home"] / "fd.log").exists()


def test_orphan_native_flags_take_the_legacy_failure(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    flags = {"events-fd": ["--events-fd", "5"], "run-id": ["--run-id", RUN_ID]}
    for name in ("events-fd", "run-id"):
        completed = _run_installer(h, "--version", VERSION, *flags[name])
        assert completed.returncode != 0
        assert "--native-install" in completed.stderr
    assert "curl" not in _trace(h)


def test_no_modify_path_is_accepted_and_no_profile_is_touched(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload, _ = _run(h, extra_args=("--no-modify-path",))
    assert completed.returncode == 0, completed.stderr
    assert [e["type"] for e in _events(payload)][-1] == "service_ready"
    for profile in (".zshrc", ".bash_profile", ".profile"):
        assert not (h["home"] / profile).exists()


# --- refusals before any download --------------------------------------------


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _setup_refusal(name: str) -> Callable[[dict[str, Any]], list[str]]:
    """Plant one piece of evidence; return any extra arguments the case needs."""

    def existing_plist(h: dict[str, Any]) -> list[str]:
        _write(h["home"] / "Library/LaunchAgents/com.ciao.server.plist")
        return []

    def dangling_plist(h: dict[str, Any]) -> list[str]:
        path = h["home"] / "Library/LaunchAgents/com.ciao.server.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(h["home"] / "nowhere.plist")
        return []

    def loaded_job(h: dict[str, Any]) -> list[str]:
        _write(h["home"] / "loaded-com.ciao.server", "")
        return []

    def default_workspace(h: dict[str, Any]) -> list[str]:
        _write(h["home"] / "Ciaobot/.runtime/workspaces.json", "[]\n")
        return []

    def legacy_env(h: dict[str, Any]) -> list[str]:
        _write(h["home"] / "Ciaobot/.env", "CIAO_WORKSPACES=x\n")
        return []

    def suggested_workspace(h: dict[str, Any]) -> list[str]:
        suggestion = h["home"] / "Elsewhere"
        _write(suggestion / ".runtime/workspaces.json", "[]\n")
        return ["--workspace", str(suggestion)]

    def cwd_workspace(h: dict[str, Any]) -> list[str]:
        _write(h["home"] / "cwd/.runtime/workspaces.json", "[]\n")
        return []

    def partial(relative: str, *, directory: bool = False) -> Callable[[dict[str, Any]], list[str]]:
        def plant(h: dict[str, Any]) -> list[str]:
            path = h["home"] / relative
            if directory:
                path.mkdir(parents=True, exist_ok=True)
            else:
                _write(path)
            return []

        return plant

    cases: dict[str, Callable[[dict[str, Any]], list[str]]] = {
        "plist": existing_plist,
        "plist-dangling": dangling_plist,
        "job": loaded_job,
        "default-ws": default_workspace,
        "legacy-env": legacy_env,
        "suggested-ws": suggested_workspace,
        "cwd-ws": cwd_workspace,
        "menubar": partial("Library/LaunchAgents/com.ciao.menubar.plist"),
        "receipt": partial(".local/state/ciaobot/install-receipt.json"),
        "migration": partial(".local/state/ciaobot/migration", directory=True),
        "host-record": partial(".local/state/ciaobot/server-host.json"),
        "host-journal": partial(".local/state/ciaobot/server-host-upgrade.json"),
        "handoff": partial(".ciao/installer-bootstrap/handoff.json"),
        "shim": partial(".local/bin/ciao"),
        "tool-env": partial(".local/share/uv/tools/ciaobot", directory=True),
        "host-app": partial("Applications/Ciaobot Server.app", directory=True),
        "desktop-app": partial("Applications/Ciaobot.app", directory=True),
    }
    return cases[name]


_REFUSALS = {
    "plist": "existing_install",
    "plist-dangling": "existing_install",
    "job": "existing_install",
    "default-ws": "existing_install",
    "legacy-env": "existing_install",
    "suggested-ws": "existing_install",
    "cwd-ws": "existing_install",
    "menubar": "partial_install",
    "receipt": "partial_install",
    "migration": "partial_install",
    "host-record": "partial_install",
    "host-journal": "partial_install",
    "handoff": "partial_install",
    "shim": "partial_install",
    "tool-env": "partial_install",
    "host-app": "partial_install",
    "desktop-app": "partial_install",
}


@pytest.mark.parametrize("case", sorted(_REFUSALS))
def test_native_mode_refuses_existing_setups_before_any_download(
    tmp_path: Path, case: str
) -> None:
    h = _native_harness(tmp_path)
    extra = _setup_refusal(case)(h)
    before = _snapshot(h["home"])
    completed, payload, _ = _run(
        h,
        lambda fd: _default_argv(fd),
        extra_args=tuple(extra),
        cwd=h["home"] / "cwd",
    )
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == _REFUSALS[case]
    assert "curl" not in _trace(h)
    assert not (h["home"] / "fd.log").exists()
    assert _snapshot(h["home"]) == before


def test_native_mode_refuses_a_held_lock_and_keeps_it(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    lock = h["home"] / ".local/state/ciaobot/native-install.lock"
    lock.mkdir(parents=True)
    record = lock / "run.json"
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    record.write_text(
        json.dumps({"schema": 1, "run_id": OTHER_RUN_ID, "pid": child.pid}, separators=(",", ":")),
        encoding="utf-8",
    )
    before = record.read_bytes()
    completed, payload, _ = _run(h)
    events = _events(payload)
    assert events[-1] == {"schema": 1, "run_id": RUN_ID, "seq": 2, "type": "failed", "code": "install_in_progress"}
    assert b"not running" in completed.stderr
    assert record.read_bytes() == before
    assert "curl" not in _trace(h)


# Cases that plant a file or link on disk. The env and permission cases are
# handled inline in the test below.
_UNKNOWN_CASES: dict[str, Callable[[dict[str, Any]], None]] = {
    "launchctl-indeterminate": lambda h: _write(h["home"] / "launchctl-print-indeterminate"),
    "state-symlink": lambda h: _link_state(h),
    "state-file": lambda h: _write(h["home"] / ".local/state/ciaobot"),
    "env-fifo": lambda h: _fifo(h["home"] / "Ciaobot/.env"),
}
_INLINE_UNKNOWN = ("env-redirect", "uv-tool-dir", "ancestor-unreadable")


def _link_state(h: dict[str, Any]) -> None:
    target = h["home"] / "elsewhere-state"
    target.mkdir()
    (h["home"] / ".local/state").mkdir(parents=True, exist_ok=True)
    (h["home"] / ".local/state/ciaobot").symlink_to(target)


def _fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


@pytest.mark.parametrize("case", sorted([*_UNKNOWN_CASES, *_INLINE_UNKNOWN]))
def test_native_mode_unknown_state_is_not_fresh(tmp_path: Path, case: str) -> None:
    if case == "ancestor-unreadable" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads every directory")
    h = _native_harness(tmp_path)
    env: dict[str, str] | None = None
    library = h["home"] / "Library"
    if case == "env-redirect":
        env = {"CIAO_RUNTIME_ROOT": str(h["home"] / "redirected")}
    elif case == "uv-tool-dir":
        env = {"UV_TOOL_DIR": str(h["home"] / "tools")}
    elif case == "ancestor-unreadable":
        library.mkdir(parents=True, exist_ok=True)
        library.chmod(0)
    else:
        _UNKNOWN_CASES[case](h)
    try:
        completed, payload, _ = _run(h, env=env)
    finally:
        if library.exists():
            library.chmod(0o755)
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "unknown_state"
    assert "curl" not in _trace(h)


# --- download and verification ------------------------------------------------


def _mutate_uv(h: dict[str, Any]) -> None:
    h["uv_tar"].write_bytes(b"not the reviewed archive")


def _remove_manifest(h: dict[str, Any]) -> None:
    (h["release"] / "ciaobot-engine-manifest.json").unlink()


def _bad_signature(h: dict[str, Any]) -> None:
    (h["release"] / "ciaobot-engine-manifest.json.sig").write_text("garbage\n", encoding="utf-8")


def _bad_wheel(h: dict[str, Any]) -> None:
    h["release_wheel"].write_bytes(b"a different wheel")


def _bad_host(h: dict[str, Any]) -> None:
    (h["release"] / SERVER_HOST_FILENAME).write_bytes(b"a different host")


def _old_release(h: dict[str, Any]) -> None:
    _write(h["home"] / "old-release", "")


def _pip_fails(h: dict[str, Any]) -> None:
    _write(h["home"] / "fail-pip", "")


_FAILURE_CASES = {
    "uv-digest": (_mutate_uv, True, "download_failed"),
    "no-manifest": (_remove_manifest, True, "download_failed"),
    "bad-signature": (_bad_signature, True, "verification_failed"),
    "wheel-digest": (_bad_wheel, True, "verification_failed"),
    "no-host": (None, False, "verification_failed"),
    "host-digest": (_bad_host, True, "verification_failed"),
    "old-release": (_old_release, True, "invalid_arguments"),
    "pip-fails": (_pip_fails, True, "install_failed"),
}


@pytest.mark.parametrize("case", sorted(_FAILURE_CASES))
def test_native_mode_maps_download_and_verification_failures(tmp_path: Path, case: str) -> None:
    mutate, host, code = _FAILURE_CASES[case]
    h = _native_harness(tmp_path, host=host)
    if mutate is not None:
        mutate(h)
    completed, payload, _ = _run(h)
    assert completed.returncode != 0
    events = _events(payload)
    terminal = [e for e in events if e["type"] in native.TERMINAL_TYPES]
    assert len(terminal) == 1
    assert terminal[0]["code"] == code
    assert events[-1] is terminal[0]
    assert _argv_record(h) is None


def test_children_never_see_the_public_descriptor(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, _, _ = _run(h)
    assert completed.returncode == 0, completed.stderr
    lines = _read(h["home"], "fd.log").splitlines()
    assert any(line.startswith("curl closed") for line in lines)
    assert any(line.startswith("native-uv closed") for line in lines)
    assert any(line.startswith("python closed") for line in lines)
    # Only the coordinator is handed the descriptor; every other child saw it closed.
    assert [line for line in lines if line.startswith("coordinator")] == ["coordinator open"]
    assert not [line for line in lines if " open " in line and not line.startswith("coordinator")]


# --- after the handover ----------------------------------------------------------


@pytest.mark.parametrize("outcome", ["failed", "silent"])
def test_after_handover_the_shell_writes_nothing(tmp_path: Path, outcome: str) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "coordinator-outcome", outcome)
    completed, payload, _ = _run(h)
    assert completed.returncode == 1
    events = _events(payload)
    assert [e["seq"] for e in events[:3]] == [1, 2, 3]
    assert events[:3][0]["phase"] == "preflight"
    if outcome == "silent":
        assert len(events) == 3
    else:
        assert events[-1] == {"schema": 1, "run_id": RUN_ID, "seq": 10, "type": "failed", "code": "service_not_ready"}
        assert len(events) == 10
    assert "launchctl bootstrap" not in _trace(h)
    assert "launchctl kickstart" not in _trace(h)
    assert not (h["home"] / "uv-tool-calls.log").exists()
    assert completed.stdout == b""
    assert b"http" not in completed.stdout


def test_a_signal_before_handover_reports_cancelled(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "term-on-manifest", "")
    completed, payload, _ = _run(h)
    assert completed.returncode != 0
    events = _events(payload)
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"
    assert [e["type"] for e in events if e["type"] in native.TERMINAL_TYPES] == ["failed"]
    assert _argv_record(h) is None


def test_a_signal_after_handover_is_forwarded_to_the_coordinator(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "coordinator-wait-term", "")
    with _public_event_pipe() as (read_fd, fd):
        (h["home"] / "native-event-fd").write_text(f"{fd}\n", encoding="utf-8")
        proc = subprocess.Popen(
            ["sh", str(h["script"]), *_default_argv(fd)],
            env=_native_env(h, None),
            cwd=str(h["home"] / "cwd"),
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # The coordinator writes its argv only once its TERM handler is in
            # place, so this sentinel is the explicit handover point.
            deadline = time.monotonic() + 30
            while _argv_record(h) is None:
                assert time.monotonic() < deadline, "the coordinator never started"
                assert proc.poll() is None
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) != 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            coordinator_pid = _read(h["home"], "coordinator-pid").strip()
            if coordinator_pid:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(coordinator_pid), signal.SIGKILL)
            os.close(fd)
            payload = _drain(read_fd)
    events = _events(payload)
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"
    assert events[-1]["seq"] == 4
    assert len(events) == 4


def test_a_blocked_event_reader_cannot_hang_the_shell(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    script = h["script"]
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            "NATIVE_WRITE_TIMEOUT=5", "NATIVE_WRITE_TIMEOUT=1"
        ),
        encoding="utf-8",
    )
    with _public_event_pipe() as (read_fd, fd):
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(fd, True)
        (h["home"] / "native-event-fd").write_text(f"{fd}\n", encoding="utf-8")
        completed = subprocess.run(
            ["sh", str(script), *_default_argv(fd)],
            env=_native_env(h, None),
            cwd=str(h["home"] / "cwd"),
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        os.close(fd)
        _drain(read_fd)
    assert completed.returncode != 0
    assert "curl" not in _trace(h)


# --- the shell and the coordinator agree -------------------------------------


def test_shell_and_coordinator_share_their_contract_constants() -> None:
    lines = SCRIPT_TEXT.splitlines()
    assert "NATIVE_COORDINATOR_PROTOCOL=1" in lines
    assert native.NATIVE_COORDINATOR_PROTOCOL == 1
    assert f"PYTHON_VERSION={native.ENGINE_PYTHON_VERSION}" in lines
    assert "NATIVE_WRITE_TIMEOUT=5" in lines
    assert "NATIVE_TIMER=/bin/sleep" in lines
    codes = set(re.findall(r"native_code=([a-z_]+)", SCRIPT_TEXT))
    assert codes
    assert codes <= set(native.FAILURE_CODES)
    shell_phases = ("preflight", "bootstrap", "download")
    assert native.PHASES[:3] == shell_phases
    assert native.SHELL_LAST_PHASE == "download"
