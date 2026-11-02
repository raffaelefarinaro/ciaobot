"""Shell tests for `install.sh --native-install` (#2405).

The public entry point drives the Mac client. Its shell half validates the
flags, refuses a Mac that is not fresh, bootstraps the digest-pinned uv,
verifies the signed release, and then hands the events descriptor to the
coordinator. Every test runs the real script against fake tools, a fake
`$HOME`, and a fake coordinator that records its argv and writes its own
events. Nothing touches the real launchd, the operator's home, the network,
uv or `~/.local`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from ciao import native_installer as native
from ciao.release_manifest import SERVER_HOST_FILENAME, build_manifest, server_host_artifact_entry
from tests.test_engine_installer import (
    SCRIPT_TEXT,
    _function_source,
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
from tests.test_native_installer import RUN_ID, _public_event_pipe
from tests.test_release_manifest import _keypair, _sign

pytestmark = [runs_the_sh_installer, needs_local_tools]

WHEEL_BYTES = b"the signed wheel"
HOST_BYTES = b"the signed universal host archive"
UV_NAME = "uv-aarch64-apple-darwin.tar.gz"
UV_DIR = "uv-aarch64-apple-darwin"
OTHER_RUN_ID = "7a1c2b3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"
PINNED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
UNAME_LINE = f"NATIVE_PATH={PINNED_PATH}"

# Every fake reports its descriptor state (`s=open|closed`), so a test can show
# which program inherited the public events descriptor.
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

# Wrappers around the harness tools that native mode now calls. They only record
# the descriptor state and delegate, so the harness answers stay the same.
_FAKE_TOOL_WRAPPER = """#!/bin/sh
__PROBE__
printf '__NAME__ %s %s\\n' "$s" "$*" >> "$HOME/fd.log"
exec __TARGET__ "$@"
"""

_NATIVE_UV = """#!/bin/sh
__PROBE__
printf 'native-uv %s %s\\n' "$s" "$*" >> "$HOME/fd.log"
{
    printf 'ARGV %s\\n' "$*"
    printf 'PWD %s\\n' "$(pwd)"
    /usr/bin/env
    printf 'END\\n'
} >> "$HOME/uv-record.log"
case "${1:-}" in
    tool)
        printf '%s\\n' "$*" >> "$HOME/uv-tool-calls.log"
        exit 1
        ;;
    venv)
        dir=
        for arg in "$@"; do dir=$arg; done
        mkdir -p "$dir/bin"
        if [ ! -f "$HOME/venv-no-python" ]; then
            cp "__VENV_PY__" "$dir/bin/python"
            chmod 755 "$dir/bin/python"
        fi
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
# Each one logs its own label, so a test can tell them apart.
_VENV_PYTHON = """#!/bin/sh
__PROBE__
flag=
if [ "${1:-}" = -I ]; then flag=-I; shift; fi
if [ "${1:-}" = -m ] && [ "${2:-}" = ciao.native_installer ]; then
    printf 'coordinator %s %s\\n' "$s" "$flag" >> "$HOME/fd.log"
    shift 2
    exec "__SYS_PY__" "__FAKE_COORD__" "$@"
fi
if [ "${1:-}" = -c ]; then label=probe
elif [ "${1:-}" = -m ]; then label=select
else label=python; fi
printf '%s %s %s\\n' "$label" "$s" "$*" >> "$HOME/fd.log"
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
    return [json.loads(line) for line in payload.decode().splitlines() if line.startswith("{")]


def _event_lines(payload: bytes) -> list[bytes]:
    return [line for line in payload.splitlines() if line.startswith(b"{")]


def _read(home: Path, name: str) -> str:
    path = home / name
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _manifest_entries(h: dict[str, Any], wheel: dict[str, Any], host: dict[str, Any]) -> list[dict[str, Any]]:
    entries = [
        {
            "filename": WHEEL_NAME,
            "kind": "wheel",
            "platform": "any",
            "arch": "any",
            "sha256": hashlib.sha256(WHEEL_BYTES).hexdigest(),
            "size": len(WHEEL_BYTES),
            **wheel,
        }
    ]
    if h["host_archive"] is not None:
        entries.append({**server_host_artifact_entry(h["host_archive"]), **host})
    return entries


def _publish(
    h: dict[str, Any],
    *,
    wheel: dict[str, Any] | None = None,
    host: dict[str, Any] | None = None,
) -> None:
    """Sign a manifest over the original bytes, with optional tampered fields.

    Each override is applied to the entry the release really describes, so a
    same-length tamper of a file is caught by its digest, and a changed size is
    caught by the size check, not masked by the other.
    """
    raw = json.dumps(
        build_manifest(
            VERSION,
            _manifest_entries(h, wheel or {}, host or {}),
            created="2026-09-25T10:00:00+00:00",
        )
    ).encode()
    (h["release"] / "ciaobot-engine-manifest.json").write_bytes(raw)
    (h["release"] / "ciaobot-engine-manifest.json.sig").write_text(
        _sign(raw, h["private_key"], h["key_id"]), encoding="utf-8"
    )


def _rewrite_script(h: dict[str, Any], old: str, new: str) -> None:
    text = h["script"].read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    h["script"].write_text(text.replace(old, new), encoding="utf-8")


def _native_harness(tmp_path: Path, *, host: bool = True) -> dict[str, Any]:
    """The legacy fake release, with the native uv, tool wrappers and coordinator."""
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

    # Native mode: this key signs the release, and the script trusts only it.
    private_key, public_key, key_id = _keypair()
    harness.update(private_key=private_key, key_id=key_id, host_archive=host_archive)
    _rewrite_script(
        harness,
        f'RELEASE_PUBLIC_KEY="{_current_public_key(harness["script"])}"',
        f'RELEASE_PUBLIC_KEY="{public_key}"',
    )
    # Script variables the harness points at fakes: the pinned PATH now lists the
    # fake bin first, and the release scheme is the file:// one the fixtures use.
    _rewrite_script(
        harness,
        UNAME_LINE,
        f"NATIVE_PATH={fakebin}:{PINNED_PATH}",
    )
    _rewrite_script(harness, 'NATIVE_RELEASE_SCHEME=https', 'NATIVE_RELEASE_SCHEME=file')
    _publish(harness)

    # The verified uv is a script that execs the native fake uv. The archive uses
    # the exact member layout the shell allowlists.
    uv_wrapper = f'#!/bin/sh\nexec "{fakebin}/native-uv" "$@"\n'.encode()
    uv_tar = tmp_path / UV_NAME
    _uv_archive(uv_tar, [(f"{UV_DIR}/", "dir", b""), (f"{UV_DIR}/uv", "file", uv_wrapper)])
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
    for name, target in (
        ("uname", str(fakebin / "uname-fake")),
        ("sw_vers", str(fakebin / "sw_vers-fake")),
        ("mktemp", "/usr/bin/mktemp"),
        ("rm", "/bin/rm"),
    ):
        if name in ("uname", "sw_vers"):
            (fakebin / name).rename(fakebin / f"{name}-fake")
        _write_exec(
            fakebin / name,
            _FAKE_TOOL_WRAPPER.replace("__PROBE__", _PROBE)
            .replace("__NAME__", name)
            .replace("__TARGET__", target),
        )
    # shasum records the environment it was started with, then runs the real one.
    (fakebin / "shasum").rename(fakebin / "shasum-real")
    _write_exec(
        fakebin / "shasum",
        f'#!/bin/sh\n{{ printf "%s\\n" ---; /usr/bin/env; }} >> "{home}/shasum-env.log"\nexec "{fakebin}/shasum-real" "$@"\n',
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


def _current_public_key(script: Path) -> str:
    match = re.search(r'^RELEASE_PUBLIC_KEY="([^"]*)"$', script.read_text(encoding="utf-8"), re.M)
    assert match is not None
    return match.group(1)


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


def _argv_with_fd(value: str) -> list[str]:
    return ["--version", VERSION, "--native-install", "--events-fd", value, "--run-id", RUN_ID]


def _argv_with_run_id(fd: int, run_id: str) -> list[str]:
    return ["--version", VERSION, "--native-install", "--events-fd", str(fd), "--run-id", run_id]


@contextlib.contextmanager
def _pipe_on(number: int):
    """A pipe whose write end is descriptor `number`, restored afterwards."""
    read_fd, write_fd = os.pipe()
    try:
        saved: int | None = os.dup(number)
    except OSError:
        saved = None
    os.dup2(write_fd, number)
    os.close(write_fd)
    try:
        yield read_fd
    finally:
        with contextlib.suppress(OSError):
            os.close(number)
        if saved is not None:
            os.dup2(saved, number)
            os.close(saved)
        os.close(read_fd)


def _drain_until(read_fd: int, seconds: float = 20.0) -> bytes:
    """Every byte up to end-of-file, or a failure once the deadline passes."""
    chunks: list[bytes] = []
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        assert remaining > 0, "the events pipe never reached end-of-file"
        ready, _, _ = select.select([read_fd], [], [], remaining)
        if not ready:
            continue
        chunk = os.read(read_fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _run(
    h: dict[str, Any],
    argv_for: Callable[[int], list[str]] = _default_argv,
    *,
    mode: str = "normal",
    extra_args: tuple[str, ...] = (),
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    """Run the script with a fresh public pipe, and return (process, events bytes).

    Modes: `normal` passes the write end on the target; `readonly` passes a
    read-only descriptor; `stdout` and `stderr` alias the events pipe onto that
    stream; `closed` passes nothing and names a free number; `open10` passes a
    genuinely open pipe on descriptor 10, which is outside the public range.
    """
    if mode == "open10":
        channel = _pipe_on(10)
        target = 10
    else:
        channel = None
    with contextlib.ExitStack() as stack:
        if channel is not None:
            read_fd = stack.enter_context(channel)
            fd = target
        else:
            read_fd, fd = stack.enter_context(_public_channel())
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
            stderr=fd if mode == "stderr" else subprocess.PIPE,
            timeout=60,
            check=False,
        )
        # Close the parent's write end, or the drain below never sees end-of-file.
        with contextlib.suppress(OSError):
            os.close(fd)
        payload = _drain_until(read_fd)
    return completed, payload


@contextlib.contextmanager
def _public_channel():
    with _public_event_pipe() as (read_fd, fd):
        yield read_fd, fd


def _diagnostics(completed: subprocess.CompletedProcess[bytes], payload: bytes) -> str:
    # With the stderr alias the diagnostics arrive on the events pipe itself.
    return ((completed.stderr or b"") + payload).decode(errors="replace")


def _argv_record(h: dict[str, Any]) -> list[str] | None:
    path = h["home"] / "coordinator-argv.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _trace(h: dict[str, Any]) -> str:
    return _read(h["home"], "trace.log")


def _snapshot(home: Path) -> dict[str, object]:
    """Every file, link and directory under `home`, minus the fake tools' logs."""
    logs = {"trace.log", "launchctl.log", "fd.log", "responders.log", "native-event-fd", "uv-record.log"}
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


# --- the success path ---------------------------------------------------------


def test_native_mode_streams_shell_phases_then_hands_over(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload = _run(h)
    assert completed.returncode == 0, completed.stderr

    lines = payload.splitlines(keepends=True)
    assert lines[:3] == [
        _line(1, {"type": "phase", "phase": "preflight"}),
        _line(2, {"type": "phase", "phase": "bootstrap"}),
        _line(3, {"type": "phase", "phase": "download"}),
    ]
    coordinator = _events(payload)[3:]
    assert [e["seq"] for e in coordinator] == list(range(4, 11))
    assert coordinator[-1]["type"] == "service_ready"

    argv = _argv_record(h)
    assert argv is not None
    assert argv[0] == "run"
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert pairs["--first-seq"] == "4"
    assert pairs["--prior-events"] == "3"
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


def test_native_mode_passes_the_workspace_suggestion(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    workspace = h["home"] / "Elsewhere"
    completed, _ = _run(h, extra_args=("--workspace", str(workspace)))
    assert completed.returncode == 0, completed.stderr
    argv = _argv_record(h)
    assert argv is not None
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert pairs["--workspace"] == str(workspace)


# --- argument and descriptor validation --------------------------------------

# Refused before any event: the descriptor or run id is unusable. Each names the
# diagnostic it must print, so a refusal for the wrong reason fails.
_ZERO_EVENT_CASES: dict[str, tuple[str, Callable[[int], list[str]], str]] = {
    "fd-2": ("normal", lambda fd: _argv_with_fd("2"), "from 3 to 9"),
    "fd-10": ("normal", lambda fd: _argv_with_fd("10"), "from 3 to 9"),
    "fd-10-open": ("open10", lambda fd: _argv_with_fd("10"), "from 3 to 9"),
    "fd-x": ("normal", lambda fd: _argv_with_fd("x"), "from 3 to 9"),
    "fd-closed": ("closed", lambda fd: _argv_with_fd("8"), "is not open"),
    "fd-readonly": ("readonly", lambda fd: _argv_with_fd(str(fd)), "not writable"),
    "fd-stdout": ("stdout", lambda fd: _argv_with_fd(str(fd)), "standard output or standard error"),
    "fd-stderr": ("stderr", lambda fd: _argv_with_fd(str(fd)), "standard output or standard error"),
    "run-id-upper": ("normal", lambda fd: _argv_with_run_id(fd, RUN_ID.upper()), "canonical lowercase UUID"),
    "run-id-short": ("normal", lambda fd: _argv_with_run_id(fd, RUN_ID[:-1]), "canonical lowercase UUID"),
    "run-id-no-hyphen": (
        "normal",
        lambda fd: _argv_with_run_id(fd, RUN_ID.replace("-", "")),
        "canonical lowercase UUID",
    ),
}


@pytest.mark.parametrize("case", sorted(_ZERO_EVENT_CASES))
def test_native_mode_validates_flags_and_descriptor(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    mode, argv_for, fragment = _ZERO_EVENT_CASES[case]
    completed, payload = _run(h, argv_for, mode=mode)
    assert completed.returncode != 0
    assert _event_lines(payload) == []
    assert fragment in _diagnostics(completed, payload)
    assert "curl" not in _trace(h)
    assert not (h["home"] / "fd.log").exists()


# Refused after the channel opens: exactly one failed(invalid_arguments).
_EVENT_CASES: dict[str, tuple[Callable[[int], list[str]], str]] = {
    "no-version": (
        lambda fd: ["--native-install", "--events-fd", str(fd), "--run-id", RUN_ID],
        "requires --version",
    ),
    "migrate": (lambda fd: _default_argv(fd) + ["--migrate"], "--migrate cannot"),
    "as-host": (lambda fd: _default_argv(fd) + ["--as-host"], "--as-host cannot"),
    "as-client": (
        lambda fd: _default_argv(fd) + ["--as-client", "https://example.test"],
        "--as-client cannot",
    ),
    "no-start": (lambda fd: _default_argv(fd) + ["--no-start"], "--no-start cannot"),
    "ws-relative": (lambda fd: _default_argv(fd) + ["--workspace", "relative/dir"], "absolute path"),
    "ws-dotdot": (lambda fd: _default_argv(fd) + ["--workspace", "/tmp/a/../b"], "absolute path"),
    "ws-newline": (lambda fd: _default_argv(fd) + ["--workspace", "/tmp/a\nb"], "absolute path"),
    "ws-control": (lambda fd: _default_argv(fd) + ["--workspace", "/tmp/a\x01b"], "absolute path"),
}


@pytest.mark.parametrize("case", sorted(_EVENT_CASES))
def test_native_mode_refuses_bad_arguments_with_one_event(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    argv_for, fragment = _EVENT_CASES[case]
    completed, payload = _run(h, argv_for)
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["failed"]
    assert events[0] == {"schema": 1, "run_id": RUN_ID, "seq": 1, "type": "failed", "code": "invalid_arguments"}
    assert fragment in _diagnostics(completed, payload)
    assert "curl" not in _trace(h)
    assert not (h["home"] / "fd.log").exists()


@pytest.mark.parametrize("flag", ["events-fd", "run-id"])
def test_orphan_native_flags_take_the_legacy_failure(tmp_path: Path, flag: str) -> None:
    h = _native_harness(tmp_path)
    flags = {"events-fd": ["--events-fd", "5"], "run-id": ["--run-id", RUN_ID]}[flag]
    completed = _run_installer(h, "--version", VERSION, *flags)
    assert completed.returncode != 0
    assert "--native-install" in completed.stderr
    assert "curl" not in _trace(h)


def test_native_mode_refuses_a_non_https_release_base_before_any_network(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload = _run(h, env={"CIAO_RELEASE_BASE_URL": "http://example.test/releases"})
    _expect_one_terminal(payload, "invalid_arguments")
    assert completed.returncode != 0
    assert "curl" not in _trace(h)


def test_no_modify_path_is_accepted_and_no_profile_is_touched(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload = _run(h, extra_args=("--no-modify-path",))
    assert completed.returncode == 0, completed.stderr
    assert _events(payload)[-1]["type"] == "service_ready"
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
def test_native_mode_refuses_existing_setups_before_any_download(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    extra = _setup_refusal(case)(h)
    before = _snapshot(h["home"])
    completed, payload = _run(h, extra_args=tuple(extra))
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == _REFUSALS[case]
    assert "curl" not in _trace(h)
    # The platform probes run before the refusal; no fetch or uv call does.
    fd_log = _read(h["home"], "fd.log")
    assert "curl" not in fd_log and "native-uv" not in fd_log and "select" not in fd_log
    assert _snapshot(h["home"]) == before


@pytest.mark.parametrize("case", ["dead-pid", "live-pid", "garbage"])
def test_a_held_lock_reports_owner_liveness_and_is_kept(tmp_path: Path, case: str) -> None:
    h = _native_harness(tmp_path)
    lock = h["home"] / ".local/state/ciaobot/native-install.lock"
    lock.mkdir(parents=True)
    record = lock / "run.json"
    if case == "dead-pid":
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        body = json.dumps({"schema": 1, "run_id": OTHER_RUN_ID, "pid": child.pid}, separators=(",", ":"))
        expected = f"(pid {child.pid}, not running)".encode()
    elif case == "live-pid":
        body = json.dumps({"schema": 1, "run_id": OTHER_RUN_ID, "pid": os.getpid()}, separators=(",", ":"))
        expected = f"(pid {os.getpid()}, running)".encode()
    else:
        body = "not json at all"
        expected = b"cannot be read"
    record.write_text(body, encoding="utf-8")
    before = record.read_bytes()
    completed, payload = _run(h)
    events = _events(payload)
    assert events[-1]["code"] == "install_in_progress"
    assert events[-1]["seq"] == 2
    assert expected in completed.stderr
    if case == "live-pid":
        # "running" is a substring of "not running": a live owner must never read as dead.
        assert b"not running" not in completed.stderr
    assert record.read_bytes() == before
    assert "curl" not in _trace(h)


# The legacy `--help` text as committed at HEAD (#2405 round 1). Every byte is
# pinned: the usage can gain no native-only line that changes the legacy output.
LEGACY_USAGE = b"Usage: install-engine.sh [--version VERSION] [--workspace DIRECTORY] [--no-start] [--no-modify-path]\n                         [--migrate [--as-host | --as-client URL]]\n                         [--native-install --events-fd N --run-id UUID]\n\nInstalls the Ciaobot engine for the current user with uv; the PWA is its UI.\n\n  --migrate            Move an engine Ciaobot.app currently manages to this\n                       installer. The workspace, its .env and the runtime root\n                       are left untouched: only the LaunchAgents, the shim, the\n                       uv tool environment, ~/.local/bin/uv when this Mac had no\n                       uv, and ~/.local/state/ciaobot/ change.\n                       Before-images of the LaunchAgents, the shim, the install\n                       receipt and any existing uv tool environment are kept in\n                       ~/.local/state/ciaobot/migration/before/; a uv this run\n                       had to publish is not one of them and stays after a\n                       rollback. Any failure after the install puts the old\n                       engine back. If Ciaobot.app\n                       is still running 20s after it is asked to quit, the run\n                       stops with that engine and its service unchanged. A\n                       hand-over keeps the workspace that engine runs in:\n                       --workspace may only name that same existing workspace,\n                       and no workspace is ever created.\n  --no-modify-path     Do not add ciao to a shell startup file.\n  --as-host            With --migrate: treat this Mac as the host, even when its\n                       node state cannot be read or trusted.\n  --as-client URL      With --migrate: treat this Mac as a client of URL, which\n                       has to be an http:// or https:// address with a host name.\n                       Its local engine is disabled and never replaced, so the\n                       Mac does not become a second writer.\n  --native-install     The Mac client's fresh-only mode. Requires --version,\n                       --events-fd N (an open, writable descriptor from 3 to 9,\n                       not stdout or stderr) and --run-id UUID (lowercase).\n                       Progress is NDJSON on that descriptor only.\n"


def _help(h: dict[str, Any], *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["sh", str(h["script"]), *args],
        env=_native_env(h, env),
        cwd=str(h["home"] / "cwd"),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_help_output_is_byte_identical_to_the_legacy_text(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed = _help(h, "--help")
    assert completed.returncode == 0
    assert completed.stderr == LEGACY_USAGE


def test_help_runs_no_program_before_the_descriptor_is_checked(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    planted = tmp_path / "planted-help"
    planted.mkdir()
    marker = h["home"] / "cat-ran"
    _write_exec(planted / "cat", f"#!/bin/sh\necho ran > {marker}\nexit 9\n")
    env = {"PATH": f"{planted}:{h['env']['PATH']}"}
    for args in (("--help",), ("--native-install", "--help")):
        completed = _help(h, *args, env=env)
        assert completed.returncode == 0
        assert completed.stderr == LEGACY_USAGE
    assert not marker.exists()


_REDIRECTS = [
    "CIAO_WORKSPACE",
    "CIAO_WORKSPACES",
    "CIAO_RUNTIME_ROOT",
    "CIAO_MEMORY_DIR",
    "CIAO_VAULT_DIR",
    "CIAO_VAULT_ROOT",
    "CIAO_LAUNCH_AGENTS_DIR",
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
]


@pytest.mark.parametrize("name", _REDIRECTS)
def test_an_inherited_redirect_is_unknown_state(tmp_path: Path, name: str) -> None:
    h = _native_harness(tmp_path)
    completed, payload = _run(h, env={name: str(h["home"] / "redirected")})
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "unknown_state"
    assert "curl" not in _trace(h)


def _link_state(h: dict[str, Any]) -> None:
    target = h["home"] / "elsewhere-state"
    target.mkdir()
    (h["home"] / ".local/state").mkdir(parents=True, exist_ok=True)
    (h["home"] / ".local/state/ciaobot").symlink_to(target)


def _fifo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)


_UNKNOWN_CASES: dict[str, Callable[[dict[str, Any]], None]] = {
    "launchctl-indeterminate": lambda h: _write(h["home"] / "launchctl-print-indeterminate"),
    "state-symlink": lambda h: _link_state(h),
    "state-file": lambda h: _write(h["home"] / ".local/state/ciaobot"),
    "env-fifo": lambda h: _fifo(h["home"] / "Ciaobot/.env"),
}


@pytest.mark.parametrize("case", sorted(_UNKNOWN_CASES) + ["ancestor-unreadable"])
def test_native_mode_unknown_state_is_not_fresh(tmp_path: Path, case: str) -> None:
    if case == "ancestor-unreadable" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads every directory")
    h = _native_harness(tmp_path)
    library = h["home"] / "Library"
    if case == "ancestor-unreadable":
        library.mkdir(parents=True, exist_ok=True)
        library.chmod(0)
    else:
        _UNKNOWN_CASES[case](h)
    try:
        completed, payload = _run(h)
    finally:
        if library.exists():
            library.chmod(0o755)
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "unknown_state"
    assert "curl" not in _trace(h)


# --- download and verification ------------------------------------------------


def _uv_other_body(h: dict[str, Any]) -> None:
    """A valid tar with the same member layout and a different uv body."""
    fakebin = h["fakebin"]
    body = f'#!/bin/sh\nexec "{fakebin}/native-uv" "$@" # tampered\n'.encode()
    _uv_archive(h["uv_tar"], [(f"{UV_DIR}/", "dir", b""), (f"{UV_DIR}/uv", "file", body)])


def _remove_manifest(h: dict[str, Any]) -> None:
    (h["release"] / "ciaobot-engine-manifest.json").unlink()


def _bad_signature(h: dict[str, Any]) -> None:
    (h["release"] / "ciaobot-engine-manifest.json.sig").write_text("garbage\n", encoding="utf-8")


def _same_length_wheel(h: dict[str, Any]) -> None:
    h["release_wheel"].write_bytes(b"THE SIGNED WHEEL")
    assert len(b"THE SIGNED WHEEL") == len(WHEEL_BYTES)


def _same_length_host(h: dict[str, Any]) -> None:
    (h["release"] / SERVER_HOST_FILENAME).write_bytes(b"THE SIGNED UNIVERSAL HOST ARCHIVE")
    assert len(b"THE SIGNED UNIVERSAL HOST ARCHIVE") == len(HOST_BYTES)


def _wheel_size_mismatch(h: dict[str, Any]) -> None:
    _publish(h, wheel={"size": len(WHEEL_BYTES) + 1})


def _host_size_mismatch(h: dict[str, Any]) -> None:
    _publish(h, host={"size": len(HOST_BYTES) + 1})


def _traversal_name(h: dict[str, Any]) -> None:
    _publish(h, wheel={"filename": "../" + WHEEL_NAME})


def _glob_wheel_name(h: dict[str, Any]) -> None:
    """A signed wheel name that is a glob. The embedded verifier refuses it, so it
    never reaches the shell's field split (that split is also set -f)."""
    cwd = h["home"] / "cwd"
    (cwd / "a").write_text("a", encoding="utf-8")
    (cwd / "b").write_text("b", encoding="utf-8")
    _publish(h, wheel={"filename": "*"})


def _old_release(h: dict[str, Any]) -> None:
    _write(h["home"] / "old-release", "")


def _pip_fails(h: dict[str, Any]) -> None:
    _write(h["home"] / "fail-pip", "")


def _venv_without_python(h: dict[str, Any]) -> None:
    _write(h["home"] / "venv-no-python", "")


_FAILURE_CASES: dict[str, tuple[Callable[[dict[str, Any]], None] | None, bool, str, str]] = {
    # case: (mutation, host, expected code, expected diagnostic)
    "uv-digest": (_uv_other_body, True, "download_failed", "does not match its reviewed sha256"),
    "no-manifest": (_remove_manifest, True, "download_failed", "could not download the release manifest"),
    "bad-signature": (_bad_signature, True, "verification_failed", "release manifest verification failed"),
    "wheel-digest": (_same_length_wheel, True, "verification_failed", "does not match the signed manifest"),
    "wheel-size": (_wheel_size_mismatch, True, "verification_failed", "does not match the signed manifest"),
    "wheel-name": (_traversal_name, True, "verification_failed", "release manifest verification failed"),
    "wheel-glob": (_glob_wheel_name, True, "verification_failed", "release manifest verification failed"),
    "no-host": (None, False, "verification_failed", "has no server host"),
    "host-digest": (_same_length_host, True, "verification_failed", "server host does not match"),
    "host-size": (_host_size_mismatch, True, "verification_failed", "server host does not match"),
    "old-release": (_old_release, True, "invalid_arguments", "does not support --native-install"),
    "pip-fails": (_pip_fails, True, "install_failed", "could not prepare the installer environment"),
    "venv-no-python": (_venv_without_python, True, "install_failed", "has no python"),
}


@pytest.mark.parametrize("case", sorted(_FAILURE_CASES))
def test_native_mode_maps_download_and_verification_failures(tmp_path: Path, case: str) -> None:
    mutate, host, code, fragment = _FAILURE_CASES[case]
    h = _native_harness(tmp_path, host=host)
    if mutate is not None:
        mutate(h)
    completed, payload = _run(h)
    assert completed.returncode != 0
    events = _events(payload)
    terminal = [e for e in events if e["type"] in native.TERMINAL_TYPES]
    assert len(terminal) == 1
    assert terminal[0]["code"] == code
    assert events[-1] is terminal[0]
    assert fragment in _diagnostics(completed, payload)
    assert _argv_record(h) is None
    # The failure path cleans up through rm, and no child of it may see the channel.
    fd_log = _read(h["home"], "fd.log")
    assert "rm closed" in fd_log
    assert " open " not in fd_log


def test_the_uv_environment_is_cleared_and_pinned(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    planted = tmp_path / "planted-bin"
    planted.mkdir()
    _write_exec(planted / "python3.13", "#!/bin/sh\nexit 3\n")
    poison = {
        "UV_INDEX_URL": "https://evil.test/simple",
        "UV_PYTHON": "/evil/python",
        "PYTHONPATH": "/evil/lib",
        # A DYLD_* variable that cannot abort a process if it leaked: an
        # inserted dylib that does not exist makes dyld kill any non-system
        # binary, which on a runner is the test harness rather than the scrub.
        "DYLD_FALLBACK_LIBRARY_PATH": "/evil/lib",
        "UV_CONFIG_FILE": "/evil/uv.toml",
        "PATH": f"{planted}:{h['env']['PATH']}",
    }
    completed, _ = _run(h, env=poison)
    assert completed.returncode == 0, completed.stderr
    blocks = _uv_blocks(h)
    # The manifest verifier, the venv and the wheel install; the host selector and
    # the protocol probe run through the venv's python, not through uv.
    assert [b["argv"].split()[0] for b in blocks] == ["run", "venv", "pip"]
    for block in blocks:
        assert set(block["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG", "PWD", "SHLVL", "_"}, block["env"]
        for forbidden in ("UV_INDEX_URL", "UV_PYTHON", "PYTHONPATH", "DYLD_FALLBACK_LIBRARY_PATH", "UV_CONFIG_FILE"):
            assert forbidden not in block["env"]
        assert block["env"]["PATH"] == f"{h['fakebin']}:{PINNED_PATH}"
        assert str(planted) not in block["env"]["PATH"]
        assert block["env"]["LANG"] == "C"
        assert Path(block["env"]["TMPDIR"]).resolve() == Path(block["pwd"]).resolve()
        assert Path(block["pwd"]).resolve().parent == tmp_path.resolve()
        assert "--no-config" in block["argv"].split()
        assert "--managed-python" in block["argv"].split()
    by_command = {b["argv"].split()[0]: b["argv"] for b in blocks}
    assert "--default-index https://pypi.org/simple" in by_command["run"]
    assert "--no-build" in by_command["run"]
    assert "--default-index https://pypi.org/simple" in by_command["pip"]
    # The pip install is binary-only except http-ece, which only ships an sdist
    # (pinned by pywebpush). The full v1.8.0 set resolves this way with uv 0.12.17.
    pip_argv = by_command["pip"].split()
    assert "--only-binary" in pip_argv and ":all:" in pip_argv
    assert "--no-binary" in pip_argv and "http-ece" in pip_argv
    assert "--no-config" in pip_argv and "--managed-python" in pip_argv
    assert "--default-index" in pip_argv and "https://pypi.org/simple" in pip_argv
    assert "--only-binary" not in by_command["venv"]
    assert "--no-binary" not in by_command["venv"]


def _uv_blocks(h: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in _read(h["home"], "uv-record.log").splitlines():
        if line.startswith("ARGV "):
            current = {"argv": line[5:], "env": {}, "pwd": ""}
            blocks.append(current)
        elif line.startswith("PWD ") and current is not None:
            current["pwd"] = line[4:]
        elif line == "END":
            current = None
        elif current is not None and "=" in line:
            key, _, value = line.partition("=")
            current["env"][key] = value
    return blocks


def test_the_caller_path_cannot_supply_a_tool(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    planted = tmp_path / "planted-tools"
    planted.mkdir()
    # A caller-controlled uname that would refuse this Mac. Native mode resolves
    # tools through the pinned PATH only, so the planted one is never run.
    _write_exec(planted / "uname", "#!/bin/sh\necho Linux\n")
    completed, payload = _run(h, env={"PATH": f"{planted}:{h['env']['PATH']}"})
    assert completed.returncode == 0, completed.stderr
    assert _events(payload)[-1]["type"] == "service_ready"


def test_a_relative_tmpdir_falls_back_to_tmp(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, payload = _run(h, env={"TMPDIR": "relative-tmp"})
    assert completed.returncode == 0, completed.stderr
    assert "mktemp closed -d /tmp/ciaobot-engine." in _read(h["home"], "fd.log")


def test_native_children_get_only_the_pinned_environment(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, _ = _run(
        h,
        env={"PERL5OPT": "-MEvilHook", "PERL5LIB": "/evil/lib", "R2_SENTINEL": "leaked"},
    )
    assert completed.returncode == 0, completed.stderr
    # Every shasum the run starts is checked, including the bootstrap's own, which
    # runs inside a function rather than as a direct isolated command.
    blocks = [b.splitlines() for b in _read(h["home"], "shasum-env.log").split("---\n") if b.strip()]
    assert len(blocks) >= 3, "the isolated shasum ran fewer times than the install does"
    for recorded in blocks:
        names = {line.partition("=")[0] for line in recorded if "=" in line}
        assert {"PATH", "HOME", "LANG"} <= names
        assert names <= {"PATH", "HOME", "LANG", "PWD", "SHLVL", "_"}, names
        assert "PERL5OPT" not in names and "PERL5LIB" not in names and "R2_SENTINEL" not in names
        assert f"PATH={h['fakebin']}:{PINNED_PATH}" in recorded


def test_native_mode_survives_caller_exported_script_variables(tmp_path: Path) -> None:
    # The caller exports names the script uses for itself. The scrub must keep the
    # script's own values for them, and the children must still see only the three.
    h = _native_harness(tmp_path)
    completed, payload = _run(
        h,
        env={"tmp": "/nonexistent", "version": "9", "SHASUM": "/nonexistent", "base": "x"},
    )
    assert completed.returncode == 0, completed.stderr
    assert _events(payload)[-1]["type"] == "service_ready"
    blocks = [b.splitlines() for b in _read(h["home"], "shasum-env.log").split("---\n") if b.strip()]
    assert blocks, "the isolated shasum never ran"
    for recorded in blocks:
        names = {line.partition("=")[0] for line in recorded if "=" in line}
        assert {"PATH", "HOME", "LANG"} <= names
        assert names <= {"PATH", "HOME", "LANG", "PWD", "SHLVL", "_"}, names


SCRUB_SHELLS = [name for name in ("sh", "bash", "dash") if shutil.which(name)]


@pytest.mark.parametrize("shell", SCRUB_SHELLS)
def test_scrub_keeps_the_script_value_and_clears_the_export(shell: str) -> None:
    # Sourced directly, under each shell present: the script's own `tmp` is exported
    # because the caller exported it, and must come back unexported with its value.
    # `export -p` prints `declare -x NAME=...` under bash in non-POSIX mode.
    script = "\n".join([
        "NATIVE_PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        _function_source("native_scrub_env"),
        "export PERL5OPT=-MEvil R3_SENTINEL=leak",
        "export tmp=/caller-tmp",
        "tmp=/script-tmp",
        "native_scrub_env",
        "printf 'TMP=%s\\n' \"$tmp\"",
        "/usr/bin/env",
    ])
    completed = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"})
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert "TMP=/script-tmp" in lines
    exported = {line.partition("=")[0] for line in lines if "=" in line and not line.startswith("TMP=")}
    assert "PERL5OPT" not in exported and "R3_SENTINEL" not in exported
    assert "tmp" not in exported
    assert "PATH=/usr/bin:/bin:/usr/sbin:/sbin" in lines


def test_children_never_see_the_public_descriptor(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, _ = _run(h)
    assert completed.returncode == 0, completed.stderr
    lines = _read(h["home"], "fd.log").splitlines()
    for prefix in ("curl closed", "native-uv closed", "probe closed", "select closed", "uname closed", "sw_vers closed", "mktemp closed"):
        assert any(line.startswith(prefix) for line in lines), prefix
    # Only the coordinator is handed the descriptor, and it is handed it with -I.
    assert [line for line in lines if line.startswith("coordinator")] == ["coordinator open -I"]
    assert not [line for line in lines if " open " in line and not line.startswith("coordinator")]


def test_the_native_download_is_https_bounded_and_quiet(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    completed, _ = _run(h)
    assert completed.returncode == 0, completed.stderr
    calls = [line for line in _trace(h).splitlines() if "ciaobot-engine-manifest.json" in line]
    assert calls
    for call in calls:
        assert "-q" in call.split()
        assert "--proto =file" in call and "--proto-redir =file" in call
        assert "--max-time" in call and "--max-filesize" in call


# --- after the handover ----------------------------------------------------------


def _expect_one_terminal(payload: bytes, code: str) -> None:
    events = _events(payload)
    terminal = [e for e in events if e["type"] in native.TERMINAL_TYPES]
    assert len(terminal) == 1
    assert terminal[0]["code"] == code


@pytest.mark.parametrize("outcome", ["failed", "silent"])
def test_after_handover_the_shell_writes_nothing(tmp_path: Path, outcome: str) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "coordinator-outcome", outcome)
    completed, payload = _run(h)
    assert completed.returncode == 1
    events = _events(payload)
    assert [e["seq"] for e in events[:3]] == [1, 2, 3]
    assert events[0]["phase"] == "preflight"
    if outcome == "silent":
        assert len(events) == 3
    else:
        assert events[-1] == {"schema": 1, "run_id": RUN_ID, "seq": 10, "type": "failed", "code": "service_not_ready"}
        assert len(events) == 10
        # The EXIT handler sees the coordinator's status 1 and must not add a
        # second terminal event after the coordinator's own.
        assert len([e for e in events if e["type"] == "failed"]) == 1
    assert "launchctl bootstrap" not in _trace(h)
    assert "launchctl kickstart" not in _trace(h)
    assert not (h["home"] / "uv-tool-calls.log").exists()
    assert completed.stdout == b""
    assert b"http" not in completed.stdout


def test_a_signal_before_handover_reports_cancelled_once(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "term-on-manifest", "")
    completed, payload = _run(h)
    assert completed.returncode != 0
    events = _events(payload)
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"
    _expect_one_terminal(payload, "cancelled")
    assert _argv_record(h) is None


def test_a_signal_after_handover_is_forwarded_to_the_coordinator(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    _write(h["home"] / "coordinator-wait-term", "")
    with _public_channel() as (read_fd, fd):
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
            payload = _drain_until(read_fd)
    events = _events(payload)
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"
    assert events[-1]["seq"] == 4
    assert len(events) == 4


def test_a_blocked_event_reader_cannot_hang_the_shell(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    _rewrite_script(h, "NATIVE_WRITE_TIMEOUT=5", "NATIVE_WRITE_TIMEOUT=1")
    with _public_channel() as (read_fd, fd):
        os.set_blocking(fd, False)
        try:
            while True:
                os.write(fd, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(fd, True)
        (h["home"] / "native-event-fd").write_text(f"{fd}\n", encoding="utf-8")
        started = time.monotonic()
        completed = subprocess.run(
            ["sh", str(h["script"]), *_default_argv(fd)],
            env=_native_env(h, None),
            cwd=str(h["home"] / "cwd"),
            pass_fds=(fd,),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        elapsed = time.monotonic() - started
        os.close(fd)
        _drain_until(read_fd)
    assert completed.returncode != 0
    assert b"not writable" in completed.stderr
    assert elapsed < 15
    assert "curl" not in _trace(h)


def test_an_unexpected_abort_before_handover_reports_failed(tmp_path: Path) -> None:
    h = _native_harness(tmp_path)
    # A command the shell never expects to fail: under `set -e` it aborts the run
    # between the argument checks and the dispatch, with no fixed-code `fail`.
    _rewrite_script(
        h,
        'if [ "$native_install" -eq 1 ]; then\n    native_main\nfi\n',
        'false\nif [ "$native_install" -eq 1 ]; then\n    native_main\nfi\n',
    )
    completed, payload = _run(h)
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[-1]["code"] == "install_failed"
    assert "curl" not in _trace(h)


# --- the shell and the coordinator agree -------------------------------------


def test_shell_and_coordinator_share_their_contract_constants() -> None:
    lines = SCRIPT_TEXT.splitlines()
    assert "NATIVE_COORDINATOR_PROTOCOL=1" in lines
    assert native.NATIVE_COORDINATOR_PROTOCOL == 1
    assert f"PYTHON_VERSION={native.ENGINE_PYTHON_VERSION}" in lines
    assert "NATIVE_WRITE_TIMEOUT=5" in lines
    assert "NATIVE_TIMER=/bin/sleep" in lines
    assert UNAME_LINE in lines
    assert "NATIVE_RELEASE_SCHEME=https" in lines
    codes = set(re.findall(r"native_code=([a-z_]+)", SCRIPT_TEXT))
    assert codes
    assert codes <= set(native.FAILURE_CODES)
    shell_phases = ("preflight", "bootstrap", "download")
    assert native.PHASES[:3] == shell_phases
    assert native.SHELL_LAST_PHASE == "download"
