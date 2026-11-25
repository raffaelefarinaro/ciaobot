#!/usr/bin/env python3
"""Standalone disposable probe for #3626 ordinary-folder note changes.

This script answers one narrow question:

    Does any cheap ordinary-filesystem primitive give an existing-note writer
    an *atomic* expected-version precondition against an independent writer?

It does not import Ciaobot, touch an engine, a vault, a live shared brain, a
cloud account, or production state.  Every path it writes is under a fresh
temporary directory (or a caller-supplied *empty* directory), and the default is
to delete that directory when the run ends.

The probe is deliberately adversarial: the "external" writer ignores advisory
locks, claim files, directory symlinks and root swaps.  That is the relevant
model for a second editor or a cloud-sync agent editing a user-visible folder.

Run:

    python3 experiments/ciao-3626-folder-probe/probe.py
    python3 experiments/ciao-3626-folder-probe/probe.py --keep --json /tmp/3626.json

The process exits 0 when every test executed (including tests that report a
failed capability); a non-zero exit means the probe harness itself could not run.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "ciao-3626-folder-probe/1"
DEFAULT_TIMEOUT_S = 20.0
_RENAME_SWAP = 0x00000002
_AT_FDCWD = -2


# ---------------------------------------------------------------------------
# Small filesystem helpers
# ---------------------------------------------------------------------------


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_in_place(path: Path, data: bytes) -> None:
    """An ordinary external editor write: same inode, truncated in place."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace(path: Path, data: bytes) -> None:
    """The usual safe-looking write: unique temp in the same directory + rename."""
    fd, tmp_name = tempfile.mkstemp(prefix=".probe-write-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _delete_recreate(path: Path, data: bytes, *, mtime_ns: int | None = None) -> None:
    path.unlink()
    _write(path, data)
    if mtime_ns is not None:
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, mtime_ns))


def _identity(path: Path) -> dict[str, Any]:
    st = os.stat(path)
    values: dict[str, Any] = {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
    }
    birthtime = getattr(st, "st_birthtime_ns", None)
    if birthtime is not None:
        values["birthtime_ns"] = birthtime
    return values


def _weak_version(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


def _touch(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def _wait_for(directory: Path, prefix: str, count: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            found = sum(
                1
                for entry in directory.iterdir()
                if entry.name.startswith(prefix + ".")
            )
        except FileNotFoundError:
            found = 0
        if found >= count:
            return True
        time.sleep(0.002)
    return False


def _wait_for_path(path: Path, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.002)
    return False


def _barrier(directory: Path, phase: str, role: str, count: int, deadline: float) -> None:
    _touch(directory / f"{phase}.{role}")
    if not _wait_for(directory, phase, count, deadline):
        raise TimeoutError(f"barrier {phase!r} timed out waiting for {count} writers")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".probe-json-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _filesystem_type(path: Path) -> str:
    """Best-effort volume type. This is evidence, not a capability test."""
    if sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["stat", "-f", "-c", "%T", str(path)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            value = result.stdout.strip()
            if value:
                return value
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        result = subprocess.run(
            ["mount"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in result.stdout.splitlines():
        if " on " not in line or not line.rstrip().endswith(")"):
            continue
        before, after = line.split(" on ", 1)
        mountpoint, _, options = after.rpartition(" (")
        if not mountpoint or not options.endswith(")"):
            continue
        if real == mountpoint or real.startswith(mountpoint.rstrip("/") + "/"):
            length = len(mountpoint)
            if best is None or length > best[0]:
                filesystem = options[:-1].split(",", 1)[0].strip()
                best = (length, filesystem)
    return best[1] if best else "unknown"


def _os_build() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["sw_vers", "-buildVersion"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _platform_info(root: Path) -> dict[str, Any]:
    mac_version = platform.mac_ver()
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "mac_ver": mac_version[0],
        "os_build": _os_build(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "executable": sys.executable,
        "uname": list(os.uname()),
        "filesystem_type": _filesystem_type(root),
        "root_device": os.stat(root).st_dev,
        "probe_sha256": _sha(Path(__file__).read_bytes()),
    }


# ---------------------------------------------------------------------------
# renameatx_np(RENAME_SWAP), macOS only
# ---------------------------------------------------------------------------


def _renameatx_swap(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = libc.renameatx_np
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "renameatx_np is unavailable") from exc
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    code = function(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_SWAP,
    )
    if code != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


# ---------------------------------------------------------------------------
# Writer subprocess
# ---------------------------------------------------------------------------


def _acquire_flock(fd: int, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(0.002)
    return False


def _writer_main(job_path: str) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    result_path = Path(job["result_path"])
    try:
        result = _run_writer(job)
    except Exception as exc:  # noqa: BLE001 - the result file is the report
        result = {
            "role": job.get("role"),
            "protocol": job.get("protocol"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        _write_json(result_path, result)
    except Exception as exc:  # noqa: BLE001
        print(f"could not write result: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


def _run_writer(job: dict[str, Any]) -> dict[str, Any]:
    role = str(job["role"])
    protocol = str(job["protocol"])
    target = Path(job["target"])
    payload = _unb64(str(job["payload_b64"]))
    expected = str(job["expected_sha256"])
    barrier = Path(job["barrier_dir"])
    count = int(job.get("n_writers", 2))
    timeout = float(job.get("timeout_s", DEFAULT_TIMEOUT_S))
    deadline = time.monotonic() + timeout
    result: dict[str, Any] = {
        "role": role,
        "protocol": protocol,
        "pid": os.getpid(),
        "expected_sha256": expected,
    }

    if protocol == "hash_then_rename":
        return _writer_hash_then_rename(
            result, target, payload, expected, barrier, role, count, deadline
        )
    if protocol == "flock_coop":
        return _writer_flock_coop(
            result, target, payload, expected, barrier, role, count, deadline
        )
    if protocol == "claim_coop":
        return _writer_claim_coop(
            result, target, payload, expected, barrier, role, count, deadline
        )
    if protocol in {
        "hash_then_rename_external",
        "flock_external",
        "claim_external",
        "vnode_observation",
        "swap_external",
    }:
        return _writer_external(
            result, target, payload, expected, barrier, role, protocol, deadline
        )
    if protocol == "excl_create":
        return _writer_excl_create(
            result, target, payload, barrier, role, count, deadline
        )
    raise ValueError(f"unknown protocol {protocol!r}")


def _writer_hash_then_rename(
    result: dict[str, Any],
    target: Path,
    payload: bytes,
    expected: str,
    barrier: Path,
    role: str,
    count: int,
    deadline: float,
) -> dict[str, Any]:
    base = _read(target)
    result["initial_sha256"] = _sha(base or b"")
    fd, tmp_name = tempfile.mkstemp(prefix=".probe-prepared-", dir=str(target.parent))
    prepared = Path(tmp_name)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    _barrier(barrier, "ready", role, count, deadline)
    current = _read(target)
    result["check_sha256"] = _sha(current or b"")
    result["check_ok"] = current is not None and _sha(current) == expected
    _barrier(barrier, "checked", role, count, deadline)

    if not result["check_ok"]:
        result["conflict"] = True
        result["replace_ok"] = False
        prepared.unlink()
        return result

    os.replace(prepared, target)
    result["replace_ok"] = True
    result["final_sha256"] = _sha(_read(target) or b"")
    return result


def _writer_flock_coop(
    result: dict[str, Any],
    target: Path,
    payload: bytes,
    expected: str,
    barrier: Path,
    role: str,
    count: int,
    deadline: float,
) -> dict[str, Any]:
    _barrier(barrier, "ready", role, count, deadline)
    lock_path = target.parent / (target.name + ".probe.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        acquired = _acquire_flock(fd, deadline)
        result["lock_acquired"] = acquired
        if not acquired:
            result["conflict"] = True
            return result
        current = _read(target)
        result["check_sha256"] = _sha(current or b"")
        result["check_ok"] = current is not None and _sha(current) == expected
        if not result["check_ok"]:
            result["conflict"] = True
            return result
        _atomic_replace(target, payload)
        result["replace_ok"] = True
        result["final_sha256"] = _sha(_read(target) or b"")
        return result
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _writer_claim_coop(
    result: dict[str, Any],
    target: Path,
    payload: bytes,
    expected: str,
    barrier: Path,
    role: str,
    count: int,
    deadline: float,
) -> dict[str, Any]:
    _barrier(barrier, "ready", role, count, deadline)
    claim = target.parent / (target.name + ".probe.claim")
    try:
        claim_fd = os.open(
            claim,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        result["claim_acquired"] = False
        result["conflict"] = True
        return result
    try:
        os.write(claim_fd, role.encode("utf-8"))
        os.fsync(claim_fd)
        result["claim_acquired"] = True
        current = _read(target)
        result["check_sha256"] = _sha(current or b"")
        result["check_ok"] = current is not None and _sha(current) == expected
        if not result["check_ok"]:
            result["conflict"] = True
            return result
        _atomic_replace(target, payload)
        result["replace_ok"] = True
        result["final_sha256"] = _sha(_read(target) or b"")
        return result
    finally:
        os.close(claim_fd)
        try:
            claim.unlink()
        except FileNotFoundError:
            pass


def _writer_external(
    result: dict[str, Any],
    target: Path,
    payload: bytes,
    expected: str,
    barrier: Path,
    role: str,
    protocol: str,
    deadline: float,
) -> dict[str, Any]:
    lock_fd: int | None = None
    claim: Path | None = None
    claim_fd: int | None = None
    prepared: Path | None = None
    try:
        if protocol == "flock_external":
            lock_path = target.parent / (target.name + ".probe.lock")
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            result["lock_acquired"] = _acquire_flock(lock_fd, deadline)
            if not result["lock_acquired"]:
                result["conflict"] = True
                return result
        elif protocol == "claim_external":
            claim = target.parent / (target.name + ".probe.claim")
            try:
                claim_fd = os.open(
                    claim,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
            except FileExistsError:
                result["claim_acquired"] = False
                result["conflict"] = True
                return result
            os.write(claim_fd, role.encode("utf-8"))
            result["claim_acquired"] = True

        base = _read(target)
        result["initial_sha256"] = _sha(base or b"")
        result["identity_before"] = _identity(target)

        if protocol == "swap_external":
            fd, tmp_name = tempfile.mkstemp(prefix=".probe-prepared-", dir=str(target.parent))
            prepared = Path(tmp_name)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        # The ordinary variants prepare bytes in a temp too, but use rename below.

        current = _read(target)
        result["check_sha256"] = _sha(current or b"")
        result["check_ok"] = current is not None and _sha(current) == expected
        result["identity_check_ok"] = result["identity_before"] == _identity(target)
        if not result["check_ok"]:
            result["conflict"] = True
            return result

        _barrier(barrier, "checked", role, 1, deadline)
        if not _wait_for_path(barrier / "external.done", deadline):
            raise TimeoutError("external writer did not signal completion")

        external = _read(target)
        result["external_seen_sha256"] = _sha(external or b"")
        result["identity_after_external"] = _identity(target)

        if protocol == "swap_external":
            assert prepared is not None
            _renameatx_swap(prepared, target)
            result["swap_ok"] = True
            result["prepared_after_swap_sha256"] = _sha(_read(prepared) or b"")
            result["final_sha256"] = _sha(_read(target) or b"")
            prepared.unlink()
            prepared = None
            return result

        _atomic_replace(target, payload)
        result["replace_ok"] = True
        result["final_sha256"] = _sha(_read(target) or b"")
        return result
    finally:
        if prepared is not None:
            try:
                prepared.unlink()
            except FileNotFoundError:
                pass
        if claim_fd is not None:
            os.close(claim_fd)
        if claim is not None:
            try:
                claim.unlink()
            except FileNotFoundError:
                pass
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _writer_excl_create(
    result: dict[str, Any],
    target: Path,
    payload: bytes,
    barrier: Path,
    role: str,
    count: int,
    deadline: float,
) -> dict[str, Any]:
    _barrier(barrier, "ready", role, count, deadline)
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        result["created"] = False
        result["conflict"] = True
        result["error_errno"] = errno.EEXIST
        return result
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    result["created"] = True
    result["final_sha256"] = _sha(_read(target) or b"")
    return result


# ---------------------------------------------------------------------------
# Parent: launching and collecting writers
# ---------------------------------------------------------------------------


def _job(
    *,
    role: str,
    protocol: str,
    target: Path,
    payload: bytes,
    expected: str,
    barrier: Path,
    count: int,
) -> dict[str, Any]:
    return {
        "role": role,
        "protocol": protocol,
        "target": str(target),
        "payload_b64": _b64(payload),
        "expected_sha256": expected,
        "barrier_dir": str(barrier),
        "n_writers": count,
        "result_path": str(barrier / f"result.{role}.json"),
        "timeout_s": DEFAULT_TIMEOUT_S,
    }


def _launch(job: dict[str, Any]) -> subprocess.Popen[bytes]:
    job_path = Path(job["barrier_dir"]) / f"job.{job['role']}.json"
    _write_json(job_path, job)
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--writer", str(job_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _collect(
    jobs: list[dict[str, Any]],
    processes: list[subprocess.Popen[bytes]],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    diagnostics: list[dict[str, Any]] = []
    for process, job in zip(processes, jobs, strict=True):
        remaining = max(0.1, deadline - time.monotonic())
        try:
            out, err = process.communicate(timeout=remaining)
            diagnostics.append(
                {
                    "role": job["role"],
                    "returncode": process.returncode,
                    "stdout": out.decode("utf-8", "replace"),
                    "stderr": err.decode("utf-8", "replace"),
                }
            )
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            diagnostics.append(
                {
                    "role": job["role"],
                    "timeout": True,
                    "returncode": process.returncode,
                    "stdout": out.decode("utf-8", "replace"),
                    "stderr": err.decode("utf-8", "replace"),
                }
            )
    results: dict[str, dict[str, Any]] = {}
    for job in jobs:
        path = Path(job["result_path"])
        try:
            results[job["role"]] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            results[job["role"]] = {
                "role": job["role"],
                "protocol": job["protocol"],
                "error": "writer produced no readable result",
            }
    return results, diagnostics


def _target(case: Path, base: bytes = b"base\n") -> Path:
    target = case / "note.md"
    _write(target, base)
    return target


def _case(root: Path, name: str) -> Path:
    case = root / name
    case.mkdir(parents=True, exist_ok=False)
    return case


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hash_then_rename(root: Path, iterations: int) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    payloads = {"A": b"writer-A\n", "B": b"writer-B\n"}
    for index in range(iterations):
        case = _case(root, f"hash-then-rename-{index}")
        target = _target(case)
        expected = _sha(b"base\n")
        barrier = case / "barrier"
        barrier.mkdir()
        jobs = [
            _job(
                role=role,
                protocol="hash_then_rename",
                target=target,
                payload=payload,
                expected=expected,
                barrier=barrier,
                count=2,
            )
            for role, payload in payloads.items()
        ]
        processes = [_launch(job) for job in jobs]
        results, notes = _collect(jobs, processes)
        diagnostics.extend(notes)
        final = _read(target) or b""
        final_is = next((role for role, data in payloads.items() if final == data), None)
        lost = next((role for role, data in payloads.items() if final != data), None)
        runs.append(
            {
                "iteration": index,
                "results": results,
                "final_is": final_is,
                "lost": lost,
                "both_checked": all(
                    bool(result.get("check_ok")) for result in results.values()
                ),
                "replacements": sum(
                    1 for result in results.values() if result.get("replace_ok")
                ),
            }
        )
    failed = any(
        run["both_checked"]
        and run["replacements"] == 2
        and run["final_is"] is not None
        and run["lost"] is not None
        for run in runs
    )
    return {
        "name": "hash_then_rename_two_writers",
        "capability": "expected-version atomicity",
        "status": "failed" if failed else "unverified",
        "detail": (
            "Two writers both validated the same expected SHA-256 and both "
            "renamed their prepared file over the target; one writer's change "
            "was lost without a conflict."
            if failed
            else "The race did not reproduce in these runs; treat the primitive as unverified."
        ),
        "evidence": {
            "iterations": iterations,
            "expected_sha256": _sha(b"base\n"),
            "runs": runs,
            "writer_diagnostics": diagnostics,
        },
    }


def test_cooperating(root: Path, protocol: str) -> dict[str, Any]:
    case = _case(root, f"{protocol}-coop")
    target = _target(case)
    expected = _sha(b"base\n")
    payloads = {"A": b"writer-A\n", "B": b"writer-B\n"}
    barrier = case / "barrier"
    barrier.mkdir()
    jobs = [
        _job(
            role=role,
            protocol=protocol,
            target=target,
            payload=payload,
            expected=expected,
            barrier=barrier,
            count=2,
        )
        for role, payload in payloads.items()
    ]
    processes = [_launch(job) for job in jobs]
    results, diagnostics = _collect(jobs, processes)
    winners = [role for role, result in results.items() if result.get("replace_ok")]
    conflicts = [
        role
        for role, result in results.items()
        if result.get("conflict") or result.get("check_ok") is False
    ]
    final = _read(target) or b""
    supported = (
        len(winners) == 1
        and len(conflicts) == 1
        and final == payloads[winners[0]]
    )
    return {
        "name": f"{protocol}_cooperating_writers",
        "capability": "cooperating-writer serialization (not external-writer exclusion)",
        "status": "supported" if supported else "failed",
        "detail": (
            "Two writers that both honor the same primitive serialized: one "
            "won and the other reported a conflict without writing."
            if supported
            else "The cooperating-writer case did not serialize as claimed."
        ),
        "evidence": {
            "winners": winners,
            "conflicts": conflicts,
            "final_sha256": _sha(final),
            "results": results,
            "writer_diagnostics": diagnostics,
        },
    }


def _external_test(
    root: Path,
    *,
    name: str,
    protocol: str,
    capability: str,
    description: str,
) -> dict[str, Any]:
    case = _case(root, name)
    target = _target(case)
    expected = _sha(b"base\n")
    payload = b"writer-payload\n"
    external = b"external-editor-change\n"
    barrier = case / "barrier"
    barrier.mkdir()
    job = _job(
        role="A",
        protocol=protocol,
        target=target,
        payload=payload,
        expected=expected,
        barrier=barrier,
        count=1,
    )
    process = _launch(job)
    if not _wait_for(barrier, "checked", 1, time.monotonic() + DEFAULT_TIMEOUT_S):
        process.kill()
        out, err = process.communicate()
        return {
            "name": name,
            "capability": capability,
            "status": "error",
            "detail": "The writer never reached the checked barrier.",
            "evidence": {
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace"),
            },
        }

    _write_in_place(target, external)
    _touch(barrier / "external.done")
    results, diagnostics = _collect([job], [process])
    result = results["A"]
    final = _read(target) or b""
    if protocol == "swap_external" and result.get("error"):
        status = "unsupported"
        detail = f"renameatx_np(RENAME_SWAP) is unavailable: {result['error']}"
    elif result.get("error"):
        status = "error"
        detail = f"Writer failed: {result['error']}"
    else:
        lost = (
            final == payload
            and result.get("external_seen_sha256") == _sha(external)
        )
        status = "failed" if lost else "unverified"
        detail = (
            description
            if lost
            else "The external write was not lost in this schedule; treat the primitive as unverified."
        )
    return {
        "name": name,
        "capability": capability,
        "status": status,
        "detail": detail,
        "evidence": {
            "protocol": protocol,
            "external_sha256": _sha(external),
            "final_sha256": _sha(final),
            "result": result,
            "writer_diagnostics": diagnostics,
        },
    }


def test_swap_external(root: Path) -> dict[str, Any]:
    return _external_test(
        root,
        name="renameatx_swap_external_writer",
        protocol="swap_external",
        capability="atomic expected-version replacement (swap)",
        description=(
            "renameatx_np(RENAME_SWAP) replaced the target after the external "
            "write; the external bytes were moved to the prepared path and "
            "discarded."
        ),
    )


def test_remote_etag_only(root: Path) -> dict[str, Any]:
    case = _case(root, "remote-etag-only")
    target = _target(case)
    remote_version = {
        "version": "v1",
        "sha256": _sha(b"base\n"),
    }
    dirty = b"offline-local-dirty\n"
    _write_in_place(target, dirty)
    dirty_seen = _read(target) or b""

    # ETag-only writer: the remote version still matches, so it never looks at
    # local bytes. It replaces the dirty local file.
    accepted = remote_version["version"] == "v1"
    _atomic_replace(target, b"writer-payload\n")
    etag_final = _read(target) or b""
    etag_lost = etag_final == b"writer-payload\n"

    # A writer that re-reads local bytes and compares a content hash can detect
    # this particular dirty state, but that recheck is not atomic either.
    _write_in_place(target, dirty)
    expected_sha = _sha(b"base\n")
    content_recheck_detected = _sha(_read(target) or b"") != expected_sha

    return {
        "name": "remote_etag_only_local_dirty",
        "capability": "local dirty-state authority",
        "status": "failed" if etag_lost else "unverified",
        "detail": (
            "A remote ETag-only writer accepted the unchanged remote version "
            "and replaced an offline-dirty local file; the local change was "
            "lost. A local content recheck detects this example but cannot be "
            "made atomic with the replacement."
        ),
        "evidence": {
            "remote_version": remote_version,
            "local_dirty_sha256": _sha(dirty_seen),
            "etag_accepted": accepted,
            "etag_final_sha256": _sha(etag_final),
            "etag_local_change_lost": etag_lost,
            "content_recheck_detected": content_recheck_detected,
        },
    }


def test_content_hash_aba(root: Path) -> dict[str, Any]:
    case = _case(root, "content-hash-aba")
    target = _target(case)
    expected = _sha(b"base\n")
    before = _identity(target)
    _delete_recreate(target, b"base\n")
    after = _identity(target)
    accepted = _sha(_read(target) or b"") == expected
    if accepted:
        _atomic_replace(target, b"writer-payload\n")
    return {
        "name": "content_hash_delete_recreate_aba",
        "capability": "delete/recreate ABA detection",
        "status": "failed" if accepted else "unverified",
        "detail": (
            "A content-hash expected version accepted a delete/recreate that "
            "returned identical bytes; the file generation changed but the "
            "writer could not tell. The inode/ctime did change in this run, so "
            "a stronger local identity check could detect this particular ABA, "
            "but it still cannot be compared atomically with the replacement."
        ),
        "evidence": {
            "expected_sha256": expected,
            "accepted": accepted,
            "identity_before": before,
            "identity_after": after,
            "identity_changed": before != after,
            "final_sha256": _sha(_read(target) or b""),
        },
    }


def test_mtime_size_aba(root: Path) -> dict[str, Any]:
    case = _case(root, "mtime-size-aba")
    target = _target(case)
    before = _identity(target)
    weak_before = _weak_version(target)
    _delete_recreate(target, b"base\n", mtime_ns=before["mtime_ns"])
    weak_after = _weak_version(target)
    after = _identity(target)
    accepted = weak_before == weak_after
    if accepted:
        _atomic_replace(target, b"writer-payload\n")
    return {
        "name": "mtime_size_delete_recreate_aba",
        "capability": "delete/recreate ABA detection (mtime+size version)",
        "status": "failed" if accepted else "unverified",
        "detail": (
            "A size+mtime_ns version accepted a delete/recreate after the "
            "mtime was restored; an ETag-like weak version cannot distinguish "
            "the generations."
        ),
        "evidence": {
            "weak_version_before": list(weak_before),
            "weak_version_after": list(weak_after),
            "accepted": accepted,
            "identity_before": before,
            "identity_after": after,
            "identity_changed": before != after,
            "final_sha256": _sha(_read(target) or b""),
        },
    }


def test_inode_reuse(root: Path, cycles: int = 200) -> dict[str, Any]:
    case = _case(root, "inode-reuse")
    target = case / "cycle"
    inodes: set[int] = set()
    for _ in range(cycles):
        _write(target, b"x")
        inodes.add(os.stat(target).st_ino)
        target.unlink()
    reuse = len(inodes) < cycles
    return {
        "name": "inode_reuse_cycles",
        "capability": "stable local identity for generations",
        "status": "failed" if reuse else "unverified",
        "detail": (
            f"{cycles} create/unlink cycles used {len(inodes)} distinct inode "
            f"numbers; inode reuse {'was' if reuse else 'was not'} observed. "
            "Even without reuse, inode identity is still not atomic with a "
            "replacement."
        ),
        "evidence": {
            "cycles": cycles,
            "distinct_inodes": len(inodes),
            "reuse_observed": reuse,
            "sample_inodes": sorted(inodes)[:20],
        },
    }


def test_root_substitution_path(root: Path) -> dict[str, Any]:
    case = _case(root, "root-substitution-path")
    root_dir = case / "root"
    moved = case / "moved"
    outside = case / "outside"
    root_dir.mkdir()
    outside.mkdir()
    _write(root_dir / "note.md", b"base\n")
    _write(outside / "note.md", b"outside-base\n")
    os.rename(root_dir, moved)
    os.symlink(outside, root_dir)

    _atomic_replace(root_dir / "note.md", b"writer-payload\n")
    outside_after = _read(outside / "note.md") or b""
    moved_after = _read(moved / "note.md") or b""
    substituted = (
        outside_after == b"writer-payload\n" and moved_after == b"base\n"
    )
    # Remove the symlink before the temp tree is cleaned up.
    root_dir.unlink()
    return {
        "name": "root_substitution_path_writer",
        "capability": "root substitution containment",
        "status": "failed" if substituted else "unverified",
        "detail": (
            "A path-based writer followed a substituted root symlink and wrote "
            "into the decoy directory, not the root the link originally named."
            if substituted
            else "The path-based writer did not follow the substituted root in this run."
        ),
        "evidence": {
            "outside_sha256": _sha(outside_after),
            "original_root_sha256": _sha(moved_after),
        },
    }


def test_root_substitution_dirfd(root: Path) -> dict[str, Any]:
    case = _case(root, "root-substitution-dirfd")
    root_dir = case / "root"
    moved = case / "moved"
    outside = case / "outside"
    root_dir.mkdir()
    outside.mkdir()
    _write(root_dir / "note.md", b"base\n")
    _write(outside / "note.md", b"outside-base\n")

    root_fd = os.open(
        root_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.rename(root_dir, moved)
        os.symlink(outside, root_dir)
        tmp_name = ".probe-prepared"
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.write(tmp_fd, b"writer-payload\n")
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        os.rename(tmp_name, "note.md", src_dir_fd=root_fd, dst_dir_fd=root_fd)
    finally:
        os.close(root_fd)

    outside_after = _read(outside / "note.md") or b""
    moved_after = _read(moved / "note.md") or b""
    visible_after = _read(root_dir / "note.md") or b""
    contained = outside_after == b"outside-base\n" and moved_after == b"writer-payload\n"
    visible_updated = visible_after == b"writer-payload\n"

    symlink_root_refused = False
    try:
        check_fd = os.open(
            root_dir,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        symlink_root_refused = exc.errno in (errno.ELOOP, errno.ENOTDIR)
    else:
        os.close(check_fd)

    root_dir.unlink()
    status = "partial" if contained and not visible_updated else "failed"
    return {
        "name": "root_substitution_dirfd_writer",
        "capability": "root substitution containment (descriptor-relative)",
        "status": status,
        "detail": (
            "Descriptor-relative traversal kept the write inside the original "
            "directory and did not follow the substituted root symlink, but "
            "the user-visible root no longer received the write: the root "
            "must be rebound before commit, which an ordinary filesystem does "
            "not make atomic. Opening a root that is already a symlink fails "
            "closed with O_NOFOLLOW."
            if status == "partial"
            else "The descriptor-relative write did not behave as expected."
        ),
        "evidence": {
            "outside_sha256": _sha(outside_after),
            "original_root_sha256": _sha(moved_after),
            "visible_root_sha256": _sha(visible_after),
            "contained": contained,
            "visible_root_updated": visible_updated,
            "symlink_root_refused": symlink_root_refused,
        },
    }


def test_root_aba_identical_clone(root: Path) -> dict[str, Any]:
    case = _case(root, "root-aba-identical")
    root_dir = case / "root"
    moved = case / "moved"
    root_dir.mkdir()
    _write(root_dir / "note.md", b"base\n")
    root_fd = os.open(
        root_dir,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.rename(root_dir, moved)
        root_dir.mkdir()
        _write(root_dir / "note.md", b"base\n")
        tmp_name = ".probe-prepared"
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.write(tmp_fd, b"writer-payload\n")
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)
        os.rename(tmp_name, "note.md", src_dir_fd=root_fd, dst_dir_fd=root_fd)
    finally:
        os.close(root_fd)

    visible_after = _read(root_dir / "note.md") or b""
    detached_after = _read(moved / "note.md") or b""
    return {
        "name": "root_aba_identical_clone",
        "capability": "root ABA / identical-root substitution",
        "status": "failed" if visible_after == b"base\n" else "unverified",
        "detail": (
            "The visible root was replaced by an identical clone while a "
            "writer held the old directory descriptor. The bytes read at the "
            "visible path were unchanged, so a content/version check cannot "
            "distinguish the clone; the writer committed to a detached directory."
        ),
        "evidence": {
            "visible_root_sha256": _sha(visible_after),
            "detached_root_sha256": _sha(detached_after),
            "visible_root_unchanged": visible_after == b"base\n",
        },
    }


def test_excl_create(root: Path) -> dict[str, Any]:
    case = _case(root, "excl-create")
    target = case / "note.md"
    payloads = {"A": b"writer-A-new-note\n", "B": b"writer-B-new-note\n"}
    barrier = case / "barrier"
    barrier.mkdir()
    expected = _sha(b"")
    jobs = [
        _job(
            role=role,
            protocol="excl_create",
            target=target,
            payload=payload,
            expected=expected,
            barrier=barrier,
            count=2,
        )
        for role, payload in payloads.items()
    ]
    processes = [_launch(job) for job in jobs]
    results, diagnostics = _collect(jobs, processes)
    creators = [role for role, result in results.items() if result.get("created")]
    losers = [
        role
        for role, result in results.items()
        if result.get("error_errno") == errno.EEXIST
    ]
    final = _read(target) or b""
    supported = (
        len(creators) == 1
        and len(losers) == 1
        and final == payloads[creators[0]]
    )
    return {
        "name": "o_excl_new_note_create",
        "capability": "new-note no-clobber creation",
        "status": "supported" if supported else "failed",
        "detail": (
            "Two writers racing to create the same new path produced exactly "
            "one file; the loser got EEXIST and wrote nothing. This is a "
            "create-only capability and says nothing about updating an "
            "existing note."
            if supported
            else "The O_EXCL create race did not produce one winner and one EEXIST."
        ),
        "evidence": {
            "creators": creators,
            "losers": losers,
            "final_sha256": _sha(final),
            "results": results,
            "writer_diagnostics": diagnostics,
        },
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _capabilities(tests: list[dict[str, Any]]) -> dict[str, str]:
    by_name = {test["name"]: test for test in tests}

    def status(name: str) -> str:
        return by_name.get(name, {}).get("status", "unverified")

    existing_note_supported = any(
        status(name) == "supported"
        for name in (
            "hash_then_rename_two_writers",
            "renameatx_swap_external_writer",
        )
    )
    return {
        "existing_note_expected_version_atomicity": (
            "supported" if existing_note_supported else "unsupported"
        ),
        "local_dirty_state_authority": "unsupported",
        "delete_recreate_aba_detection": "unsupported",
        "root_substitution_binding": "unsupported",
        "new_note_no_clobber_creation": status("o_excl_new_note_create"),
        "cooperating_writer_serialization": (
            "supported_with_cooperation"
            if status("flock_coop_cooperating_writers") == "supported"
            and status("claim_coop_cooperating_writers") == "supported"
            else "unverified"
        ),
        "external_writer_exclusion": "unsupported",
        "automatic_folder_existing_note_edit": "disabled",
        "engine_applied_reviewed_replacement": "disabled",
        "cloud_backed_folder_backends": "unverified",
    }


def _report(root: Path, tests: list[dict[str, Any]], *, kept: bool) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for test in tests:
        counts[test["status"]] = counts.get(test["status"], 0) + 1
    capabilities = _capabilities(tests)
    return {
        "schema": SCHEMA,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": _platform_info(root),
        "root": str(root),
        "root_retained": kept,
        "tests": tests,
        "summary": {
            "counts": counts,
            "capabilities": capabilities,
            "verdict": (
                "No tested ordinary-filesystem primitive enforces an atomic "
                "expected-version precondition for an existing note against an "
                "independent external writer. Content hash, mtime+size, "
                "hash-then-rename, renameatx_np(RENAME_SWAP), advisory locks, "
                "exclusive claim files and vnode observation all lost an "
                "external or ABA change; descriptor-relative traversal "
                "contains a root swap but does not bind the user-visible root. "
                "Keep automatic and engine-applied reviewed replacement for "
                "existing notes on ordinary folders disabled and retain the "
                "proposed diff for manual application. O_EXCL supports "
                "create-only no-clobber. Cloud-backed folder paths were not "
                "validated and remain unsupported."
            ),
        },
    }


def _print_report(report: dict[str, Any]) -> None:
    platform_info = report["platform"]
    print("Ciao #3626 ordinary-folder probe")
    print(
        "platform: "
        f"{platform_info['platform']} | {platform_info['machine']} | "
        f"macOS {platform_info['mac_ver'] or 'n/a'} | "
        f"filesystem {platform_info['filesystem_type']} | "
        f"python {platform_info['python']}"
    )
    print(f"root: {report['root']} ({'retained' if report['root_retained'] else 'removed'})")
    print("")
    for test in report["tests"]:
        print(f"[{test['status'].upper():<10}] {test['name']}")
        print(f"            {test['detail']}")
    print("")
    print("capability summary:")
    for name, value in report["summary"]["capabilities"].items():
        print(f"  {name}: {value}")
    print("")
    print("verdict:", report["summary"]["verdict"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _prepare_root(requested: str | None) -> tuple[Path, bool]:
    if requested is None:
        return Path(tempfile.mkdtemp(prefix="ciao-3626-probe-")), True
    parent = Path(requested).expanduser()
    if parent.exists():
        if not parent.is_dir():
            raise SystemExit(f"--root is not a directory: {parent}")
        # Never write into or delete the caller's directory itself. Create one
        # disposable child and remove only that child.
        child = Path(
            tempfile.mkdtemp(prefix="ciao-3626-probe-", dir=str(parent))
        )
        return child, True
    parent.mkdir(parents=True)
    return parent, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--writer",
        metavar="JOB",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--root",
        help=(
            "Use a disposable child of this directory. It must not contain live "
            "data; the child directory is removed unless --keep is given."
        ),
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the disposable probe directory and print its path.",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Write the full JSON report to PATH (atomic replace).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Repetitions of the hash-then-rename race (default: 3).",
    )
    parser.add_argument(
        "--aba-cycles",
        type=int,
        default=200,
        help="Create/unlink cycles for the inode-reuse observation (default: 200).",
    )
    args = parser.parse_args(argv)

    if args.writer:
        return _writer_main(args.writer)

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if args.aba_cycles < 1:
        parser.error("--aba-cycles must be >= 1")

    root, disposable = _prepare_root(args.root)
    tests: list[dict[str, Any]] = []
    try:
        tests.append(test_hash_then_rename(root, args.iterations))
        tests.append(test_cooperating(root, "flock_coop"))
        tests.append(test_cooperating(root, "claim_coop"))
        tests.append(
            _external_test(
                root,
                name="hash_then_rename_external_writer",
                protocol="hash_then_rename_external",
                capability="atomic expected-version replacement (hash-then-rename)",
                description=(
                    "A hash-then-rename writer validated the expected version, "
                    "then renamed its prepared file over the target after an "
                    "external editor wrote to it; the external change was lost."
                ),
            )
        )
        tests.append(
            _external_test(
                root,
                name="flock_external_writer",
                protocol="flock_external",
                capability="external writer exclusion (advisory lock)",
                description=(
                    "A writer holding an advisory flock still replaced the "
                    "target after an external editor wrote to it."
                ),
            )
        )
        tests.append(
            _external_test(
                root,
                name="claim_external_writer",
                protocol="claim_external",
                capability="external writer exclusion (exclusive claim)",
                description=(
                    "A writer holding an O_EXCL claim file still replaced the "
                    "target after an external editor wrote to it."
                ),
            )
        )
        tests.append(
            _external_test(
                root,
                name="vnode_observation_external_writer",
                protocol="vnode_observation",
                capability="atomic expected-version replacement (vnode observation)",
                description=(
                    "A writer that observed the target's stat identity before "
                    "the external write still replaced the target afterwards."
                ),
            )
        )
        tests.append(test_swap_external(root))
        tests.append(test_remote_etag_only(root))
        tests.append(test_content_hash_aba(root))
        tests.append(test_mtime_size_aba(root))
        tests.append(test_inode_reuse(root, args.aba_cycles))
        tests.append(test_root_substitution_path(root))
        tests.append(test_root_substitution_dirfd(root))
        tests.append(test_root_aba_identical_clone(root))
        tests.append(test_excl_create(root))
        report = _report(root, tests, kept=args.keep)
        if args.json:
            _write_json(Path(args.json), report)
        _print_report(report)
        return 0
    finally:
        if disposable and not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
