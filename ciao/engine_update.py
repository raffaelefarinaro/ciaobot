"""Stage and apply an engine update for installer-managed installs (#562).

Everything here is one transaction with two halves, split by who can survive
what. :func:`stage_update` (part 1, #569) runs while the engine is up: lock,
durable operation record, signed-manifest verification, wheel download +
digest check, and a fully installed staged env. :func:`apply_update` and
:func:`run_apply` (this module's second half) own the downtime: a bounded
drain, then a *detached* updater job running from the staged env, outside the
engine's own launchd job and outside the env it replaces, so the swap, the
readiness check and the rollback all survive the engine being booted out.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Callable, Sequence

from ciao import install_receipt, macos_service, package_version, release_manifest

logger = logging.getLogger(__name__)

# `verifying` is the manifest/digest check of the staging half, so the
# post-start readiness check gets its own name: an operator reading the record
# must be able to tell "we have not confirmed the download" from "the new
# engine is up and answering".
PHASES = (
    "resolving",
    "downloading",
    "verifying",
    "staging",
    "staged",
    "draining",
    "applying",
    "stopping",
    "swapping",
    "starting",
    "verifying_start",
    "applied",
    "rolling_back",
    "rolled_back",
    "rollback_failed",
    "failed",
)
RELEASE_BASE = "https://github.com/raffaelefarinaro/ciaobot/releases/download"
MANIFEST_NAME = release_manifest.MANIFEST_NAME
SIGNATURE_NAME = release_manifest.SIGNATURE_NAME
PREVIOUS_RECEIPT_NAME = "previous-receipt.json"
OPERATION_NAME = "operation.json"
LOCK_NAME = "update.lock"
# The one-shot job that owns the swap. A sibling of `com.ciao.server`, not a
# child: `launchctl bootout` on the engine must not take the updater with it.
UPDATER_LABEL = "com.ciao.updater"
UPDATER_PLIST_NAME = "com.ciao.updater.plist"
# The durable recovery job: the net under the swap itself. A LaunchAgent of its
# own, written into the update state dir and run by launchd from the staged
# interpreter, because the window it covers is the one where
# `com.ciao.server`'s program no longer exists — launchd cannot start the
# engine, so nothing *inside* the engine can notice the swap is stranded. It is
# a sibling of both other jobs, and the only one that is re-run on a timer
# rather than once.
RECOVER_LABEL = "com.ciao.recover"
RECOVER_PLIST_NAME = "com.ciao.recover.plist"
# How often the recovery agent re-reads the record. Short enough that a reboot
# does not cost the operator an engine for long, long enough that a tick landing
# beside a live swap costs one `launchctl print`, one lock attempt and an exit.
_RECOVER_INTERVAL = 30
# Imported, not re-spelled: the label the updater boots out has to be the one
# the installer registered, and a second literal here would drift silently.
SERVER_LABEL = macos_service.SERVER_LABEL
# The live env renamed aside, kept until the next update succeeds, so exactly
# one rollback generation is retained.
PREVIOUS_ENV_NAME = "previous-env"
_CHUNK = 1 << 20
# Generous, because every one of these calls is to a local service answering
# from a warm page cache, and a hung local socket is a bug rather than a
# condition worth waiting on.
_HTTP_TIMEOUT = 5.0
_POLL_INTERVAL = 1.0
# Three consecutive empty readings, not one: a chat that settles between two
# polls would otherwise look drained while its work is still visible.
_IDLE_POLLS_REQUIRED = 3
_UV_TIMEOUT = 600
_STOP_TIMEOUT = 30.0
_READY_TIMEOUT = 120.0
_LOCK_TIMEOUT = 30.0
# How long a drain waits for active chats, and the floor an operator may set.
# Below a second there is no time to read the first poll, and a non-finite
# value would turn the deadline into no deadline at all.
_DEFAULT_DRAIN_TIMEOUT = 600.0
_MIN_DRAIN_TIMEOUT = 1.0
# Every loopback probe in this module is to the engine on *this* machine, so
# none of them may be routed through the system HTTP proxy: urllib honours the
# macOS proxy settings, and a Mac configured with one (a PAC URL, a corporate
# VPN) can turn a healthy engine into a probe that never answers — which, from
# a rollback, means `rollback_failed`. An empty `ProxyHandler` disables the
# lookup for this opener alone and changes nothing else about the process.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# A `fetch` is called with the URL and destination, and may take extra keyword
# bounds (`max_bytes`). Typed loosely on purpose: a test double is a plain
# two-argument function, and the contract that matters is that it writes
# `dest`.
Fetch = Callable[..., None]
Runner = Callable[..., subprocess.CompletedProcess[str]]
# Launchctl invocation: a list of arguments in, a completed process out. The
# default is `macos_service._launchctl`, so the only place launchd is ever
# named is the one place the installer already owns.
Launchctl = Callable[[list[str]], subprocess.CompletedProcess[str]]
PostJson = Callable[[str], dict[str, Any]]
GetJson = Callable[[str], "dict[str, Any] | None"]
Sleep = Callable[[float], None]
Clock = Callable[[], float]
# `ServiceResult` in production, a stub with the same `.ok` in tests: the only
# field this module reads is `ok`.
ServiceStarter = Callable[[], Any]
Opener = Callable[..., Any]



class UpdateError(RuntimeError):
    """An update could not be staged. The reason is safe to show an operator."""


class UpdateInProgress(UpdateError):
    """Another staging run holds the lock, so this one must not start."""


def default_state_dir() -> Path:
    """Where update state lives: beside the receipt, outside every env.

    The receipt's directory is already the one place on disk that survives a
    runtime swap and is not owned by any workspace, so the update state joins
    it rather than inventing a second root.
    """
    from ciao.install_receipt import default_receipt_path

    return default_receipt_path().parent / "updates"


@dataclass
class Operation:
    """The durable record of one staging run, rewritten at every phase.

    ``env_freeze`` is what the staged environment was resolved to, recorded
    while it is still only a directory nobody runs: the apply moves that very
    env rather than resolving anything, so this is the one place the dependency
    set of an update is ever known. Audit-only, and nothing in the apply reads
    it — a record written before the field existed reads as ``""``.
    """

    id: str
    phase: str
    from_version: str
    to_version: str
    started_at: str
    updated_at: str
    error: str = ""
    stage_dir: str = ""
    wheel: str = ""
    wheel_sha256: str = ""
    env_python: str = ""
    env_freeze: str = ""
    previous_receipt: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _stamp() -> str:
    """The operation id prefix: compact, sortable, and filesystem-safe."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _operation_path(state_dir: Path) -> Path:
    return state_dir / OPERATION_NAME


def read_operation(state_dir: Path | None = None) -> Operation | None:
    """Return the current operation record, or None when there is none.

    Fail-safe like the receipt: a missing, unreadable or non-object file means
    "no record" rather than an exception, because the UI reads this on its own
    schedule and must not break on a half-written document.
    """
    path = _operation_path(state_dir or default_state_dir())
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    data: dict[str, Any] = parsed
    fields = {name: str(data.get(name, "")) for name in Operation.__dataclass_fields__}
    if not fields["id"] or fields["phase"] not in PHASES:
        return None
    return Operation(**fields)


def write_operation(op: Operation, state_dir: Path | None = None) -> None:
    """Persist ``op`` atomically at 0600, so a reader never sees half a record.

    A unique temp name (``mkstemp``) because the UI may read the record while a
    run rewrites it, and a fixed name would let two writers collide on it.
    """
    target = _operation_path(state_dir or default_state_dir())
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(op), indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def acquire_lock(state_dir: Path | None = None) -> IO[str]:
    """Take the exclusive update lock, or raise ``UpdateInProgress``.

    ``LOCK_EX | LOCK_NB``: a second run must fail fast rather than queue behind
    the first one, because the second one would then stage over a staging area
    the first still owns. The handle is returned open and unlocked-on-close by
    the kernel; callers pass it to :func:`release_lock`.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    handle = open(root / LOCK_NAME, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise UpdateInProgress(
            "another engine update is already in progress"
        ) from exc
    return handle


def release_lock(handle: IO[str]) -> None:
    """Drop the update lock and close its handle. Safe to call once."""
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def default_fetch(
    url: str, dest: Path, *, attempts: int = 3, timeout: float = 30.0, max_bytes: int = 0
) -> None:
    """Stream ``url`` to ``dest`` with plain urllib, retrying transient errors.

    A stalled release CDN is the common failure, not a 404, so transient
    ``OSError``/``URLError`` gets up to ``attempts`` tries with a short sleep.
    Bytes land in a ``.part`` sibling and are renamed in one step, so an
    interrupted run never leaves a truncated file that a later digest check
    would have to distinguish from a corrupt one.

    ``max_bytes`` aborts a download that has already grown past what the
    caller expects. Without it a wrong or hostile ``Content-Length`` fills the
    disk long before the digest check that would have rejected the bytes; the
    ceiling is generous by the caller, because being wrong about it must fail
    a legitimate release rather than a truncated one.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "ciaobot-updater"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                written = 0
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            # Not an OSError, so this is not retried: the
                            # server is answering fine, it is just serving
                            # something this release does not describe. The
                            # partial bytes are dropped first: they are not a
                            # release anybody can install, and leaving them
                            # makes a retry look like it is resuming.
                            part.unlink(missing_ok=True)
                            raise UpdateError(
                                f"{url} is larger than the {max_bytes} bytes expected"
                            )
                        handle.write(chunk)
            os.replace(part, dest)
            return
        except (OSError, urllib.error.URLError) as exc:
            last = exc
            part.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(2)
    assert last is not None
    raise UpdateError(f"could not download {url}: {last}")


def resolve_latest_version() -> str:
    """The newest published version, or ``UpdateError`` when it is unknown.

    Reuses the same release-redirect lookup the recurring update check uses, so
    the app and the updater cannot disagree about what "latest" means.
    """
    status = package_version.package_status()
    latest = str(status.get("latest_version") or "")
    if not latest:
        detail = str(status.get("error") or "no release found")
        raise UpdateError(f"could not resolve the latest release: {detail}")
    return latest


def find_uv(receipt_uv: str = "") -> str:
    """Locate a ``uv`` executable, preferring the one that built this engine.

    The receipt's value is first because it is the binary that produced the
    running environment, so staging with it cannot mismatch a toolchain. Then
    ``PATH``, then the well-known installer location. Not being able to stage
    without uv is a hard, clearly-explained failure rather than a fallback that
    would install a differently-built env.
    """
    candidates = [receipt_uv]
    which = shutil.which("uv")
    if which:
        candidates.append(which)
    candidates.append(str(Path.home() / ".local" / "bin" / "uv"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise UpdateError("could not find the uv executable to stage the update with")


def _sha256(path: Path) -> tuple[str, int]:
    """Streamed digest and size, so a large wheel is never held in memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _receipt_uv() -> str:
    """The ``uv`` recorded by the installer for this very process, if any."""
    from ciao import install_receipt

    receipt = install_receipt.running_receipt()
    return receipt.uv if receipt is not None else ""


def _pick_wheel(manifest: dict[str, Any]) -> dict[str, Any]:
    """The single ``kind == "wheel"`` artifact in a verified manifest."""
    artifacts: list[Any] = manifest["artifacts"]
    wheels = [a for a in artifacts if a.get("kind") == "wheel"]
    if len(wheels) != 1:
        raise UpdateError(
            f"expected exactly one wheel in the release manifest, found {len(wheels)}"
        )
    wheel: dict[str, Any] = wheels[0]
    return wheel


def _staged_env_python(tool_dir: Path) -> Path:
    """The interpreter of the one tool env ``uv`` built in ``tool_dir``.

    Read off the directory rather than spelled out: a tool env is named after
    the *distribution* (``ciaobot``), not the wheel file, and the rule uv
    normalises that name by is uv's business, not this module's. The tool dir
    belongs to one staging run and is emptied before it, so anything in it is
    what this run just installed — and an amount other than one is a refusal,
    because the record's ``env_python`` has to name the env the apply moves.
    """
    try:
        found = sorted({path.parent.parent for path in tool_dir.glob("*/bin/python")})
    except OSError as exc:
        raise UpdateError(f"could not read the staged tool directory: {exc}") from exc
    if len(found) != 1:
        raise UpdateError(
            f"expected one staged tool environment in {tool_dir}, found {len(found)}"
        )
    return found[0] / "bin" / "python"


def stage_update(
    target_version: str | None = None,
    *,
    current_version: str | None = None,
    state_dir: Path | None = None,
    release_base: str = RELEASE_BASE,
    fetch: Fetch | None = None,
    run: Runner = subprocess.run,
    uv: str | None = None,
    python_version: str | None = None,
) -> Operation:
    """Stage ``target_version`` (or the latest) and return the final record.

    Raises ``UpdateInProgress`` when another run holds the lock, and
    ``UpdateError`` for anything that goes wrong; either way a ``failed``
    record is left behind describing the phase that broke, because the point of
    the record is to let the UI explain what happened.

    ``fetch``, ``run``, ``uv`` and ``state_dir`` are injectable so the tests
    never touch the network, a real ``uv``, or this machine's real state.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    handle = acquire_lock(root)
    try:
        return _stage_locked(
            target_version,
            current_version=current_version,
            state_dir=root,
            release_base=release_base,
            fetch=fetch or default_fetch,
            run=run,
            uv=uv,
            python_version=python_version,
        )
    finally:
        release_lock(handle)


def _stage_locked(
    target_version: str | None,
    *,
    current_version: str | None,
    state_dir: Path,
    release_base: str,
    fetch: Fetch,
    run: Runner,
    uv: str | None,
    python_version: str | None,
) -> Operation:
    import ciao

    current: str = current_version or ciao.__version__
    target = target_version or resolve_latest_version()
    if not release_manifest._VERSION_RE.fullmatch(target):
        raise UpdateError(f"not a release version: {target!r}")
    if target == current:
        # Before any record is written: an up-to-date engine is not an update
        # attempt, and leaving a `failed` record for it would be a lie.
        raise UpdateError(f"already on {target}")

    stage_dir = state_dir / target
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True)
    os.chmod(stage_dir, 0o700)

    now = _now()
    op = Operation(
        id=f"{_stamp()}-{target}",
        phase="resolving",
        from_version=current,
        to_version=target,
        started_at=now,
        updated_at=now,
        stage_dir=str(stage_dir),
    )

    def advance(phase: str) -> None:
        op.phase = phase
        op.updated_at = _now()
        write_operation(op, state_dir)

    advance("resolving")
    try:
        release_url = f"{release_base}/v{target}"
        manifest_path = stage_dir / MANIFEST_NAME
        signature_path = stage_dir / SIGNATURE_NAME
        advance("downloading")
        fetch(f"{release_url}/{MANIFEST_NAME}", manifest_path)
        fetch(f"{release_url}/{SIGNATURE_NAME}", signature_path)

        advance("verifying")
        manifest = release_manifest.verify_manifest(
            manifest_path.read_bytes(),
            signature_path.read_text(encoding="utf-8"),
            release_manifest.RELEASE_PUBLIC_KEY,
        )
        if manifest["version"] != target:
            raise UpdateError(
                f"manifest is for {manifest['version']}, not {target}"
            )
        entry = _pick_wheel(manifest)
        filename = str(entry["filename"])
        wheel_path = stage_dir / filename
        # One mebibyte of slack over the signed size: a wheel that is merely a
        # little larger than the manifest claims is a release to investigate,
        # not a download to keep filling the disk with.
        fetch(
            f"{release_url}/{filename}",
            wheel_path,
            max_bytes=int(entry["size"]) + 1_048_576,
        )
        digest, size = _sha256(wheel_path)
        if digest != entry["sha256"] or size != entry["size"]:
            raise UpdateError("downloaded wheel does not match the signed manifest")
        op.wheel = str(wheel_path)
        op.wheel_sha256 = digest

        advance("staging")
        uv_bin = uv or find_uv(_receipt_uv())
        py = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        # A real tool env, in a tool dir of this update's own, because the apply
        # moves this directory into the live env's place: it has to be the shape
        # `uv tool install` produces — its `uv-receipt.toml`, its entry points —
        # and not a venv that merely looks like one. Every dependency is
        # resolved here, while the engine is up and the network is there, so the
        # swap is a rename plus the two absolute paths a move breaks (#611).
        tool_dir = stage_dir / "tool"
        run(
            [uv_bin, "tool", "install", "--python", py, str(wheel_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
            # Pinned to this update's own directories, for the same reason the
            # apply used to pin the install it replaced: an inherited
            # `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` would build the environment
            # somewhere the apply does not know to look, and the version check
            # below would then be checking a directory nothing will ever move.
            env={
                **os.environ,
                "UV_TOOL_DIR": str(tool_dir),
                "UV_TOOL_BIN_DIR": str(stage_dir / "bin"),
            },
        )
        env_python = _staged_env_python(tool_dir)
        out = run(
            [str(env_python), "-I", "-c", "import ciao; print(ciao.__version__)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out != target:
            raise UpdateError(f"staged env reports version {out!r}, not {target}")
        op.env_python = str(env_python)
        # What this update is about to bring in, taken from the env itself and
        # not from the manifest: the pins that matter are the ones the resolver
        # chose, transitive ones included. `check=True` because a staging run
        # that cannot say what it resolved should not record `staged`; the env
        # is discarded either way, and this costs no network.
        op.env_freeze = run(
            [uv_bin, "pip", "freeze", "--python", str(env_python)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
        ).stdout.strip()

        from ciao import install_receipt

        receipt_path = install_receipt.default_receipt_path()
        if receipt_path.is_file():
            # Kept so #570 can roll back to the install the receipt describes,
            # after the staging env replaces the running one.
            previous = stage_dir / PREVIOUS_RECEIPT_NAME
            shutil.copy2(receipt_path, previous)
            op.previous_receipt = str(previous)
    except Exception as exc:
        # One write, with the reason attached: a `failed` record whose error is
        # still empty would be the one case the UI cannot explain.
        #
        # `run(..., check=True)` reports only "returned non-zero exit status 1"
        # here; uv's own stderr is the likeliest real explanation, so it is
        # carried into the record instead of being captured and dropped.
        message = _reason(exc)
        op.phase = "failed"
        op.error = message
        op.updated_at = _now()
        write_operation(op, state_dir)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(message) from exc

    advance("staged")
    return op


def _reason(exc: BaseException) -> str:
    """The operator-facing explanation of ``exc``, subprocess output included.

    ``run(..., check=True)`` reports only "returned non-zero exit status 1";
    uv's own reason is in the captured output, and dropping it is what makes a
    failed update unexplainable from the record. ``BaseException`` because an
    interrupted drain (Ctrl-C, SIGHUP) is recorded through this too.
    """
    message = str(exc)
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            message = f"{message}: {detail[-2000:]}"
    return message


def _engine_port() -> int:
    """The port the engine answers on: its plist, its workspace .env, or the default.

    Resolved the same way every other launchd caller resolves it, so the
    updater probes the same port the tray and the service helpers do.
    """
    return macos_service.discover_runtime().port


def _decode_body(raw: bytes) -> dict[str, Any]:
    parsed: Any = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _post_json(url: str, *, opener: Opener = _LOCAL_OPENER.open) -> dict[str, Any]:
    """POST an empty body to ``url`` and return the JSON object answer.

    ``opener`` is a parameter so the failure-injection tests never open a
    socket. The timeout is short because both callers are talking to a server
    on this machine, where a slow answer is a wedged one.
    """
    request = urllib.request.Request(url, method="POST", data=b"")
    with opener(request, timeout=_HTTP_TIMEOUT) as response:
        return _decode_body(response.read())


def _get_json(
    url: str, *, opener: Opener = _LOCAL_OPENER.open
) -> dict[str, Any] | None:
    """GET ``url`` and decode a JSON object, or None for any failure at all.

    None rather than an exception, because "the engine is not answering" is a
    normal state in this module and not an error: it is how the updater learns
    the server is gone, and how a drain poll survives the engine dying while it
    waits.
    """
    try:
        with opener(url, timeout=_HTTP_TIMEOUT) as response:
            return _decode_body(response.read())
    except Exception:  # noqa: BLE001 — unreachability is an answer here
        return None


def _drain_timeout(drain_timeout: float) -> UpdateError:
    """The one failure that must leave the running engine exactly as it was."""
    return UpdateError(
        f"drain timed out after {int(drain_timeout)}s; the running engine was left untouched"
    )


def _drain_timeout_arg(value: str) -> float:
    """``--drain-timeout`` as a usable number of seconds, or a usage error.

    A value below the floor is a typo (`--drain-timeout 60` meaning
    milliseconds, or `0` meaning "do not wait" and stopping the engine
    mid-turn), and a non-finite one (`inf`) is worse than a typo: it removes
    the deadline entirely, so a drain that never completes keeps the engine
    refusing turns with no upper bound on when that ends. argparse reports
    both as what they are — a bad argument — rather than letting the apply
    start and fail later, with the engine already drained.
    """
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not a number of seconds: {value!r}"
        ) from None
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError(
            f"must be a finite number of seconds, got {value!r}"
        )
    if seconds < _MIN_DRAIN_TIMEOUT:
        raise argparse.ArgumentTypeError(
            f"must be at least {int(_MIN_DRAIN_TIMEOUT)}s, got {value!r}"
        )
    return seconds


def _write_plist(plist: dict[str, Any], target: Path) -> Path:
    """Write ``plist`` to ``target`` atomically, owner-only; return ``target``.

    Owner-only, and through a temp file, because ``launchctl bootstrap`` reads
    it immediately afterwards and a half-written plist would be a job that never
    loads with nothing in the record to explain it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(plist, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _job_plist(
    op: Operation,
    python: str,
    *,
    label: str,
    verb: str,
    log_name: str,
    args: Sequence[str] | None = None,
    start_interval: int | None = None,
) -> dict[str, Any]:
    """The plist for one of this module's detached LaunchAgents.

    All of them are the same shape on purpose: a program that has to survive the
    engine being booted out, in its own process group, reading and writing nothing
    outside the update state dir. Three things differ, and each difference is
    load-bearing:

    * the *label* and the *verb*, because they are different jobs with different
      work to do;
    * ``start_interval``, which only the recovery agent takes. The updater runs
      the swap once and exits; the agent has to keep re-checking until it finds a
      stranded swap, because it also has to outlive the reboots and logouts it
      cannot observe. ``RunAtLoad`` is what makes it run at all after a reboot,
      and a one-shot job could not use either;
    * the log file, which is per job so that two of them running beside each
      other cannot interleave their output into something unreadable.

    Everything else — ``RunAtLoad``, ``KeepAlive`` false so a failed job never
    becomes a relaunch loop, ``AbandonProcessGroup`` so the engine's bootout
    cannot take it down and its exit cannot take the engine down — is shared
    because it is true of all of them.
    """
    log = Path(op.stage_dir) / log_name
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [
            python,
            "-I",
            "-m",
            "ciao.engine_update",
            verb,
            *(args if args is not None else ["--operation", op.id]),
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        # So the engine's bootout cannot reach the job's children, and the
        # job's exit cannot drag the engine down with it.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    if start_interval is not None:
        plist["StartInterval"] = start_interval
    return plist


def _write_job_plist(
    op: Operation,
    python: str,
    state_dir: Path,
    *,
    label: str,
    plist_name: str,
    verb: str,
    log_name: str,
    args: Sequence[str] | None = None,
    start_interval: int | None = None,
) -> Path:
    """Write one of the one-shot jobs' plist into the update state dir.

    In the state dir and nowhere else, deliberately: a plist in the LaunchAgents
    directory is re-registered by launchd at every login, and a one-shot job
    that came back to life after a reboot would run the swap again over an
    install the operator had already stopped trusting. The recovery agent is the
    exception, and says so in :func:`_recovery_plists`.
    """
    return _write_plist(
        _job_plist(
            op,
            python,
            label=label,
            verb=verb,
            log_name=log_name,
            args=args,
            start_interval=start_interval,
        ),
        state_dir / plist_name,
    )


def _write_updater_plist(
    op: Operation,
    python: str,
    state_dir: Path,
    *,
    verb: str = "run-apply",
    args: Sequence[str] | None = None,
) -> Path:
    """Write the one-shot updater LaunchAgent and return the path written.

    `verb` and `args` are what the job runs: `run-apply` for a staged swap and
    `run-recover` for one an earlier reboot left half-finished.
    """
    return _write_job_plist(
        op,
        python,
        state_dir,
        label=UPDATER_LABEL,
        plist_name=UPDATER_PLIST_NAME,
        verb=verb,
        log_name="updater.log",
        args=args,
    )


def _recovery_plists(root: Path) -> tuple[Path, Path]:
    """The durable agent's two plists: the record of it, and the one launchd reads.

    The state-dir copy is the one this module owns beside the operation record,
    and the one a reader (or a test) can find. The LaunchAgents copy is what makes
    the agent *durable*, and it is not redundant: a job registered with
    ``launchctl bootstrap`` lives in launchd's database for that login session
    only, and the login-time scan — the one thing that re-registers agents after
    a reboot or a logout — reads that directory and nothing else. Without it,
    ``RunAtLoad`` and ``StartInterval`` describe a job that disappears along with
    the crash it was installed for, which is the entire window this agent exists
    to cover.
    """
    return (
        root / RECOVER_PLIST_NAME,
        macos_service.default_launch_agents_dir() / RECOVER_PLIST_NAME,
    )


def _write_recover_plist(op: Operation, python: str, state_dir: Path) -> Path:
    """Write the durable recovery agent's plists; return the state-dir copy.

    The same job as the updater, on a timer and with a label of its own, so the
    two can be told apart in launchd and on disk: a recovery that re-ran the
    swap, or an apply that retired somebody else's job, would be a much worse bug
    than either one failing to load.

    The login-time copy is best effort. The state-dir copy is the durable record
    of what was installed, and an unwritable LaunchAgents directory must not cost
    the operator the net for the crash happening right now — launchd still runs
    the job this returns a path for, so the difference is only what survives a
    reboot.
    """
    plist = _job_plist(
        op,
        python,
        label=RECOVER_LABEL,
        verb="run-recover",
        log_name="recover.log",
        start_interval=_RECOVER_INTERVAL,
    )
    written = _write_plist(plist, state_dir / RECOVER_PLIST_NAME)
    try:
        _write_plist(
            plist, macos_service.default_launch_agents_dir() / RECOVER_PLIST_NAME
        )
    except OSError as exc:
        logger.warning(
            "could not write %s into the LaunchAgents directory, so the %s agent "
            "will not come back by itself after a reboot: %s",
            RECOVER_PLIST_NAME,
            RECOVER_LABEL,
            exc,
        )
    return written


def _retire_job(launch: Launchctl, domain_uid: int, label: str, *plists: Path) -> None:
    """Boot a job out and delete its plist(s), however either of those goes.

    Both steps, because neither is enough on its own. A plist alone leaves the job
    loaded in launchd with its ``StartInterval`` still ticking, so a settled update
    would keep re-checking its own record for as long as the job is registered —
    and, for the agent, would be re-registered at the next login on top of that.
    The bootout alone leaves a plist for that same next login to load.

    The plists go first, and that order is load-bearing: the recovery agent runs
    this on *itself*, so the bootout that follows is a SIGTERM this process may
    not survive. Unlinking first means the tidying-up cannot be the thing the
    signal interrupts.

    Nothing here raises and nothing here reports: the caller has already decided
    the outcome, and the machine is not improved by a recovery failing to tidy up
    after itself. Every caller reaches this only after the record has been settled
    and the engine started again.
    """
    for plist in plists:
        with contextlib.suppress(OSError):
            plist.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        launch(["bootout", f"gui/{domain_uid}/{label}"])


def _install_recovery_agent(
    op: Operation, python: str, root: Path, launch: Launchctl, domain_uid: int
) -> Path:
    """Load the durable recovery agent for ``op``; return the plist written.

    This one is started by launchd rather than by anything inside the engine, and
    re-checked on an interval, so it survives the crash, the reboot, the logout
    and the death of the job that installed it. That is the whole design, and it
    is needed because the window a swap can strand the machine in is one where
    ``com.ciao.server``'s own program names a file that does not exist: launchd
    cannot start the engine, and a recovery reached from inside the engine is
    then a recovery that cannot run.

    Which is why ``python`` is an explicit argument and not read off ``op``: the
    net is only worth anything if its program is a file that exists, so every
    caller has to place it in an env the swap does not *consume*.
    :func:`apply_update` installs it while the live env is still intact, from
    the staged interpreter; :func:`run_apply` re-points it at
    ``<stage>/previous-env/bin/python`` the moment the live env is renamed
    aside, because the swap then moves the staged env into the live env's place
    and the agent's program would be a dangling path forever after. The
    ``previous-env`` is complete from that instant and is only ever moved again
    by the rollback — which is the recovery.

    bootout before bootstrap, as everywhere else here: a job left loaded from an
    earlier attempt would make bootstrap fail with "service already loaded" and
    leave the machine with no net at all. A job that is not there is the normal
    case, so the bootout's non-zero exit is ignored.
    """
    plist = _write_recover_plist(op, python, root)
    launch(["bootout", f"gui/{domain_uid}/{RECOVER_LABEL}"])
    bootstrap = launch(["bootstrap", f"gui/{domain_uid}", str(plist)])
    if bootstrap.returncode != 0:
        detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
        raise UpdateError(
            f"could not start the {RECOVER_LABEL} job: "
            f"{detail or 'launchctl bootstrap failed'}"
        )
    return plist

def _reopen_admission(post: PostJson, base: str) -> None:
    """Undo a drain, ignoring whether it worked.

    The engine is either unreachable — in which case it is refusing nothing —
    or wedged, in which case the cancel is exactly what a retry needs. Neither
    is worth masking the real failure for, so this never raises.
    """
    try:
        post(f"{base}/api/admin/drain/cancel")
    except Exception:  # noqa: BLE001 — best effort by definition
        pass


def _advance_ignoring_failure(advance: Callable[[str], None], phase: str) -> None:
    """Advance the record, swallowing a write that cannot land.

    A record write fails on a full disk, on a read-only state directory, on a
    vanished parent — and the most likely way to get there is the same full
    disk that broke the swap in the first place. A phase write is bookkeeping,
    so losing one must never cost the operator the thing the phase describes:
    a rollback that never runs, or an update that is rolled back after it has
    already passed readiness. The caller keeps its own outcome either way.
    """
    try:
        advance(phase)
    except Exception:  # noqa: BLE001 — bookkeeping must not escalate
        pass


def _start_best_effort(start: ServiceStarter) -> None:
    """Start the engine without caring whether it worked.

    Only used where nothing was ever swapped: the engine was never taken down
    for good, so a start that fails is its own state to report, and must not
    escalate into a rollback of files that never moved.
    """
    try:
        start()
    except Exception:  # noqa: BLE001 — the recorded reason is the real one
        pass


def _require_started(result: Any) -> None:
    """Raise unless the service starter reported success."""
    if not bool(result.ok):
        detail = str(getattr(result, "message", "") or "").strip()
        raise UpdateError(detail or "the engine service did not start")


def _wait_until_unreachable(
    get: GetJson, url: str, timeout: float, sleep: Sleep, clock: Clock
) -> bool:
    """Wait for the engine to stop answering. False when it is still up."""
    deadline = clock() + timeout
    while True:
        if get(url) is None:
            return True
        if clock() >= deadline:
            return False
        sleep(_POLL_INTERVAL)


def _wait_until_ready(
    get: GetJson,
    url: str,
    version: str,
    timeout: float,
    sleep: Sleep,
    clock: Clock,
    *,
    subject: str,
) -> None:
    """Wait for the engine to report ``version`` *and* be ready, or raise.

    Both conditions, because each alone lies: the right version answering
    ``/api/startup-status`` can still be mid-import, and a ready engine is the
    only evidence the swap produced a working install.
    """
    deadline = clock() + timeout
    while True:
        body = get(url) or {}
        if body.get("version") == version and body.get("overall_ready") is True:
            return
        if clock() >= deadline:
            raise UpdateError(
                f"{subject} did not report {version} and ready within {int(timeout)}s"
            )
        sleep(_POLL_INTERVAL)


def _move_env(source: Path, dest: Path) -> None:
    """Move a whole environment, across filesystems when it has to.

    ``os.replace`` is a rename, which is what keeps the swap instant, but the
    tool directory and the update state can sit on different volumes, where a
    rename is refused outright; ``shutil.move`` copies in that case, slower but
    correct.
    """
    shutil.rmtree(dest, ignore_errors=True)
    try:
        os.replace(source, dest)
    except OSError:
        shutil.move(str(source), str(dest))


def _console_scripts(wheel: Path) -> list[str]:
    """The console-script names the wheel's own metadata declares.

    Read from the verified wheel rather than from the staged environment, so
    the set of entry points the swap has to place is a fact about the release
    and not about how a particular uv happened to lay a tool env out. A wheel
    that cannot be read here is refused, because the alternative is an install
    whose ``ciao`` silently is not the release's own.
    """
    try:
        with zipfile.ZipFile(wheel) as archive:
            entries = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/entry_points.txt")
            ]
            # Deliberately not folded into the `except` below: a wheel with the
            # wrong number of entry-point files is a refusal of its own, and the
            # message should say which.
            if len(entries) != 1:
                raise UpdateError(
                    f"the wheel {wheel.name} describes {len(entries)} "
                    "entry-point files, not one"
                )
            text = archive.read(entries[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise UpdateError(
            f"could not read the entry points of {wheel.name}: {exc}"
        ) from exc

    names: list[str] = []
    in_console_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_console_scripts = stripped == "[console_scripts]"
        elif in_console_scripts and "=" in stripped:
            names.append(stripped.partition("=")[0].strip())
    if not names:
        raise UpdateError(f"the wheel {wheel.name} declares no console scripts")
    return names


def _first_line(path: Path) -> str:
    """A file's first line, or ``""`` for anything unreadable.

    The one question both the shebang rewrite and its check below ask: does the
    first line of a file that just moved still name the env it was staged in?
    Unreadable reads as empty rather than raising — a file that cannot be read
    is not one this module can repair, and the entry points that matter are
    checked by the caller either way.
    """
    try:
        return path.read_text(encoding="utf-8").partition("\n")[0]
    except (OSError, UnicodeDecodeError):
        return ""


def _relocate_shebang(path: Path, staged_prefix: str, live_prefix: str) -> None:
    """Repoint a script's ``#!`` line from the staged env to the live one.

    A shebang is a fixed prefix of the first line, so this is a bounded textual
    replacement and not a guess at what a script means: a file whose first line
    is not a shebang, or one naming neither environment, is left exactly as it
    is.
    """
    first = _first_line(path)
    if not first.startswith("#!") or staged_prefix not in first:
        return
    _, newline, rest = path.read_text(encoding="utf-8").partition("\n")
    path.write_text(
        first.replace(staged_prefix, live_prefix) + newline + rest, encoding="utf-8"
    )


def _install_staged_env(
    staged_env: Path,
    live_env: Path,
    *,
    bin_dir: Path,
    wheel: Path,
) -> None:
    """Put the staged env where the live one was, and re-point what names it.

    A rename wherever the filesystem allows one, and nothing else: the staged
    env *is* the environment `stage_update` resolved and verified, so installing
    it again — from the network or from a cache — could only ever produce a
    different one (#611).

    A move breaks exactly two things *that matter* that name the env by absolute
    path, and both are repaired rather than tolerated:

    * the shebang of the scripts in ``<live_env>/bin``, which uv wrote naming
      the *staged* interpreter and which would otherwise be a program that
      cannot start (`bad interpreter`);
    * the entry points in ``bin_dir``, which uv places as links into the env
      and which would otherwise dangle.

    A real tool env leaves two more absolute paths behind, both stale-but-harmless
    and both left alone: the seven ``bin/activate*`` files, which each embed
    ``VIRTUAL_ENV="<env>"``, and the ``install-path`` entry in
    ``uv-receipt.toml``, which names the env's ``bin`` where it was staged.
    Nothing in this repo sources a tool env's ``activate``, and ``uv tool list``
    reads the requirement rather than the path.

    ``uv-receipt.toml`` itself rides along, so ``uv tool list`` still reports this
    install and the installer's own "was this installed by Ciaobot" guard keeps
    working.

    Anything that cannot be placed raises, which is what makes the caller's
    rollback the answer: an update that cannot put in place the environment it
    verified is not an update.
    """
    _move_env(staged_env, live_env)
    # A shebang is POSIX and uv writes absolute POSIX paths into it, so the
    # prefixes are spelled with a literal separator rather than `os.sep`.
    staged_prefix = f"{staged_env}/bin/"
    live_prefix = f"{live_env}/bin/"
    try:
        scripts = sorted((live_env / "bin").iterdir())
    except OSError as exc:
        raise UpdateError(f"the moved environment has no bin directory: {exc}") from exc
    for script in scripts:
        if script.is_file() and not script.is_symlink():
            _relocate_shebang(script, staged_prefix, live_prefix)
    # Fail closed on a survivor: `_relocate_shebang` only rewrites a first line
    # that *is* a shebang, so a launcher naming the interpreter some other way —
    # a wrapper whose first line is `exec <staged>/bin/python` — keeps naming a
    # directory the swap has just renamed away, and the entry point it leaves is
    # one that answers `bad interpreter`. Tolerated, that only surfaces as a new
    # engine that never comes up, at the end of a swap that has already cost the
    # operator their engine; refusing here is a rollback instead.
    for script in scripts:
        if (
            script.is_file()
            and not script.is_symlink()
            and staged_prefix in _first_line(script)
        ):
            raise UpdateError(
                f"the staged path is still in the first line of {script.name}, "
                "so it would not start"
            )
    for name in _console_scripts(wheel):
        script = live_env / "bin" / name
        if not script.is_file():
            raise UpdateError(f"the staged environment has no {name} entry point")
        shim = bin_dir / name
        if shim.is_symlink() or not shim.exists():
            # uv's own shape: a link into the env. The move broke it, because
            # it names the staged path, and a `bin_dir` that has lost the entry
            # point entirely gets the link it should have had.
            shim.unlink(missing_ok=True)
            shim.symlink_to(script)
        else:
            # A plain file here (a copy rather than a link, or a shim a previous
            # release wrote) is repaired in place: replacing it would throw away
            # whatever else it carries.
            _relocate_shebang(shim, staged_prefix, live_prefix)
        if not shim.is_file():
            raise UpdateError(f"the {name} entry point {shim} does not resolve")


def _prune_previous_envs(root: Path, op: Operation) -> None:
    """Drop the ``previous-env`` of every *other* staged update.

    This update's own copy stays: it is the one generation a rollback can use,
    and it is not superseded until a later update succeeds. Removing it here
    would leave a successful update with nothing to fall back to.
    """
    keep = os.path.abspath(op.stage_dir)
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or os.path.abspath(child) == keep:
            continue
        shutil.rmtree(child / PREVIOUS_ENV_NAME, ignore_errors=True)


def _previous_receipt(op: Operation) -> install_receipt.InstallReceipt | None:
    """The install ``previous-receipt.json`` describes, if it still reads."""
    path = (
        Path(op.previous_receipt)
        if op.previous_receipt
        else Path(op.stage_dir) / PREVIOUS_RECEIPT_NAME
    )
    return install_receipt.read_receipt(path)


def apply_update(
    *,
    drain_timeout: float = _DEFAULT_DRAIN_TIMEOUT,
    state_dir: Path | None = None,
    port: int | None = None,
    http_post: PostJson | None = None,
    launchctl: Launchctl | None = None,
    uid: int | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
) -> Operation:
    """Drain the running engine, then hand the swap to a detached job.

    The foreground half of the apply, run by the operator (and by the #571
    settings UI, through this same function). It stops admitting new turns,
    waits for the ones already running — with a deadline, because a drain that
    never completes must not cost the user their engine — and then hands the
    rest to a one-shot ``com.ciao.updater`` launchd job that runs from the
    *staged* environment.

    That split is the whole point. The swap has to happen after the engine is
    booted out and while its own files are being replaced, so it cannot be run
    by the engine; and ``bootout`` may kill the process tree of the job that
    started it, so it cannot be a child of the engine's job either. Hence a
    sibling job, in an environment the swap has not touched.

    Everything is injectable so the tests exercise the handshake, the timeout
    and the handoff without launchd, a network, or a real engine. Note there is
    no ``http_get``: the drain is polled by POSTing it again, so this half
    never asks the engine a question the drain answer does not carry.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    post = http_post or _post_json
    launch = launchctl or (lambda args: macos_service._launchctl(args))
    domain_uid = os.getuid() if uid is None else uid
    base = f"http://localhost:{_engine_port() if port is None else port}"
    handle = acquire_lock(root)
    try:
        op = read_operation(root)
        # The record's own interpreter, not a path rebuilt from the stage dir: a
        # tool env's directory is named after the distribution, so `<stage>/env`
        # stopped being where the staged interpreter lives in #611. The venv
        # layout is only the fallback for a record staged before that.
        staged_python = (
            Path(op.env_python)
            if op is not None and op.env_python
            else (Path(op.stage_dir, "env", "bin", "python") if op else None)
        )
        if (
            op is None
            or op.phase != "staged"
            or staged_python is None
            or not staged_python.exists()
        ):
            if op is not None and op.phase == "failed" and op.error:
                # The record knows why, and "nothing staged" would be a lie:
                # the staged env is usually still there, and only the apply
                # has to be run again.
                raise UpdateError(f"last apply failed: {op.error}; run: ciao update stage")
            raise UpdateError("nothing staged; run: ciao update stage")

        def advance(phase: str) -> None:
            op.phase = phase
            op.updated_at = _now()
            write_operation(op, root)

        def fail(message: str) -> None:
            """Reopen admission and record why, with the engine left running."""
            _reopen_admission(post, base)
            op.phase = "failed"
            op.error = message
            op.updated_at = _now()
            write_operation(op, root)

        try:
            advance("draining")
            post(f"{base}/api/admin/drain")
            # Polled by re-POSTing the drain, not by reading
            # `/api/active-chats`: on a client-mode node that path is mirrored
            # to the host, so a GET would drain *this* machine and then wait on
            # the host's chats. The drain POST is local (see
            # `EXCLUDED_LOCAL_PATHS`), and `begin_restart_drain` is
            # idempotent, so asking again is free and answers the same
            # question.
            # Three consecutive empty readings rather than one: a chat can look
            # idle between two of its own phases, and stopping the engine then
            # would cut a turn short.
            deadline = clock() + drain_timeout
            idle = 0
            while True:
                body = post(f"{base}/api/admin/drain")
                active = body.get("active_chat_ids") or []
                idle = idle + 1 if not active else 0
                if idle >= _IDLE_POLLS_REQUIRED:
                    break
                if clock() >= deadline:
                    raise _drain_timeout(drain_timeout)
                sleep(_POLL_INTERVAL)
        except BaseException as exc:
            # `BaseException`, not `Exception`: a Ctrl-C, a SIGHUP or a SIGTERM
            # during a wait of up to ten minutes is the most likely way this
            # ends, and skipping `fail()` there would leave the engine refusing
            # every turn with nothing in the record to say why.
            message = _reason(exc)
            fail(message)
            if not isinstance(exc, Exception):
                raise
            raise UpdateError(message) from exc

        try:
            plist_path = _write_updater_plist(
                op, op.env_python or str(staged_python), root
            )
            # bootout before bootstrap: a job left loaded from an earlier
            # attempt would make bootstrap fail with "service already loaded"
            # and leave nothing running at all. A job that is not there is the
            # normal case, so its non-zero exit is ignored.
            launch(["bootout", f"gui/{domain_uid}/{UPDATER_LABEL}"])
            bootstrap = launch(["bootstrap", f"gui/{domain_uid}", str(plist_path)])
            if bootstrap.returncode != 0:
                detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
                raise UpdateError(
                    f"could not start the updater job: {detail or 'launchctl bootstrap failed'}"
                )
        except BaseException as exc:
            message = _reason(exc)
            fail(message)
            if not isinstance(exc, Exception):
                raise
            raise UpdateError(message) from exc

        # The durable recovery agent goes in here, behind the updater and before
        # the engine is ever stopped. From the next moment on, the swap can
        # strand the machine where `com.ciao.server`'s program does not exist:
        # the live env is renamed aside and launchd cannot start the engine, so
        # the startup hook that would otherwise finish the swap cannot run
        # either. This job is started by launchd, from the staged interpreter,
        # and re-checks on an interval — so it is still there after the reboot,
        # the logout, or the death of the updater it is standing behind. The
        # staged interpreter is the right one *here* and only here: the swap has
        # not moved anything yet, and `run_apply` re-points the job at the env it
        # keeps the moment it does.
        #
        # Best effort with a loud log rather than a refusal: the swap can still
        # succeed, `recover_interrupted_apply` still covers the cases where the
        # engine does come back, and refusing a working update over a missing
        # net would be the worse outcome for the operator. The lock is still
        # held here, so the agent's first tick stands down rather than racing
        # the apply that installed it.
        try:
            _install_recovery_agent(op, str(staged_python), root, launch, domain_uid)
        except Exception as exc:  # noqa: BLE001 — a missing net is not a failed update
            logger.warning(
                "could not install the %s agent, so a swap interrupted from here "
                "is only recoverable when the engine starts: %s",
                RECOVER_LABEL,
                exc,
            )

        # The lock is released by the `finally` on the way out, which is the
        # handoff: the updater job takes it the moment this returns, and it
        # waits for it rather than failing when it loses the race.
        advance("applying")
        return op
    finally:
        release_lock(handle)


def _acquire_lock_waiting(root: Path, sleep: Sleep, clock: Clock) -> IO[str]:
    """Take the lock the foreground half is still holding, or give up.

    The foreground step releases the lock just before it returns, so the updater
    routinely finds it held: that is the handoff, not contention, and it is why
    this waits. A genuinely concurrent second update still times out rather
    than queueing behind a swap that may roll back under it.
    """
    deadline = clock() + _LOCK_TIMEOUT
    while True:
        try:
            return acquire_lock(root)
        except UpdateInProgress:
            if clock() >= deadline:
                raise
            sleep(_POLL_INTERVAL)


def _rollback(
    op: Operation,
    *,
    receipt: install_receipt.InstallReceipt,
    receipt_path: Path | None,
    live_env: Path,
    previous_env: Path,
    env_moved: bool,
    domain_uid: int,
    status_url: str,
    launch: Launchctl,
    get: GetJson,
    start: ServiceStarter,
    sleep: Sleep,
    clock: Clock,
) -> list[str]:
    """Put the previous env, receipt and engine back; return what could not be.

    Every step runs even after an earlier one failed. A rollback that stops at
    the first problem leaves an engine that is neither the old version nor the
    new one, which is the outcome rollback exists to prevent, so the service is
    started last and unconditionally: the operator gets a running engine even
    when the environment behind it could not be restored.

    The env is only touched when it was actually moved. A move that failed
    half-way is the one case where the live env is still the install the
    operator is running, and "restoring" it would mean deleting the only
    working copy — so in that case the rollback restarts the engine and
    verifies it, and does nothing else.
    """
    errors: list[str] = []

    def step(name: str, action: Callable[[], Any]) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — recorded, never raised
            errors.append(f"{name}: {exc}")

    step(
        "stop the engine",
        lambda: launch(["bootout", f"gui/{domain_uid}/{SERVER_LABEL}"]),
    )
    # `launchctl bootout` returns before launchd has finished with the job, and
    # the forward path already waits for the engine to stop before touching a
    # file. Starting it again in that window is the race
    # `scripts/install.sh` works around: a service start that lands while
    # launchd is still unloading comes back with a stale environment. The
    # answer is ignored — an engine that is still up is started anyway below —
    # but waiting costs one probe.
    step(
        "wait for the engine to stop",
        lambda: _wait_until_unreachable(get, status_url, _STOP_TIMEOUT, sleep, clock),
    )
    if env_moved:
        step(
            "remove the half-installed env",
            lambda: shutil.rmtree(live_env, ignore_errors=True),
        )
        step("restore the previous env", lambda: _move_env(previous_env, live_env))
    step(
        "restore the receipt",
        lambda: install_receipt.write_receipt(
            _previous_receipt(op) or receipt, receipt_path
        ),
    )
    step("start the engine", lambda: _require_started(start()))
    step(
        "verify the restored engine",
        lambda: _wait_until_ready(
            get,
            status_url,
            op.from_version,
            _READY_TIMEOUT,
            sleep,
            clock,
            subject="the restored engine",
        ),
    )
    return errors


def _loaded_field(printed: str, key: str) -> str:
    """The text ``launchctl print`` rendered for ``key``, or ``""``.

    launchd has printed a job's values in three shapes across versions: a bare
    scalar on the key's own line, a list opened on that same line, and a list
    whose opening bracket is on the line *after* the key. A list of any shape is
    flattened to its own lines here, so the callers only decide what a token in
    it means, and a key launchd did not render at all answers ``""`` — which is
    evidence of nothing, and so never refuses an update and never stands a
    recovery down.
    """
    lines = printed.splitlines()
    for index, line in enumerate(lines):
        head, separator, rest = line.partition("=")
        if not separator or head.strip() != key:
            continue
        value = rest.strip()
        if value[:1] not in {"(", "{"}:
            return value
        parts = [value]
        # The list's own delimiters, so a block that opens here is collected up
        # to the line that closes it — one argument per line, in every shape.
        depth = value.count("(") + value.count("{") - value.count(")") - value.count("}")
        for nxt in lines[index + 1 :]:
            parts.append(nxt)
            depth += nxt.count("(") + nxt.count("{") - nxt.count(")") - nxt.count("}")
            if depth <= 0:
                break
        return "\n".join(parts)
    return ""


def _loaded_tokens(printed: str, key: str) -> list[str]:
    """The tokens in a value ``launchctl print`` rendered for ``key``."""
    return [
        token
        for line in _loaded_field(printed, key).splitlines()
        for token in re.findall(r"[^\s,(){}]+", line)
    ]


def _loaded_program_argument(printed: str) -> str | None:
    """The program ``launchctl print`` says a loaded job runs, or ``None``.

    The first token of the ``program`` value in every shape launchd prints it
    (see :func:`_loaded_field`); nothing recognisable answers ``None``, which is
    evidence of nothing and so never refuses an update.
    """
    tokens = _loaded_tokens(printed, "program")
    return tokens[0].strip("\"'") if tokens else None


def _loaded_server_program(launch: Launchctl, domain_uid: int) -> str | None:
    """``com.ciao.server``'s program *as launchd would run it*, or ``None``.

    This is the interpreter the acceptance criterion is about, and the on-disk
    plist cannot be trusted for it: launchd loads a job once, so a plist
    rewritten afterwards — by a reinstall, by an operator's editor, by a
    `Ciaobot.app` installed over a terminal install — describes the next login
    while the running service still executes the old one. ``launchctl print``
    reports the loaded job, which is the one that has to agree with the
    receipt. ``None`` means "not loaded, or launchd said nothing usable", and
    only then may the on-disk plist answer instead.
    """
    try:
        printed = launch(["print", f"gui/{domain_uid}/{SERVER_LABEL}"])
    except OSError:
        return None
    if printed.returncode != 0:
        return None
    return _loaded_program_argument(printed.stdout or "")


def _runs_the_receipt_install(
    program: Path, live_env: Path, executable: str | Path | None
) -> bool:
    """Whether ``program`` is part of the install the receipt names.

    Two shapes, and the README install has both. `uv tool install` puts the
    interpreter inside the tool env (`live_env`) and the `ciao` entry point
    *beside* it in `~/.local/bin`; `install-engine.sh` then runs
    `ciao setup --python "$ciao"`, so the LaunchAgent's `ProgramArguments[0]` is
    that bin entry point — the receipt's own `executable`, and outside the env.
    Both belong to the one install: `run_apply` re-points the entry-point links
    in that entry point's directory on every swap. Accepting only the env would
    refuse every healthy terminal install, which is the install the feature
    exists for.

    The executable is compared resolved, because that entry point is a symlink
    into the tool env on a real install and comparing the link would refuse the
    install it names.
    """
    if program.is_relative_to(live_env):
        return True
    if not executable:
        return False
    return program.resolve() == Path(executable).resolve()


def _server_plist_disagreement(
    live_env: Path,
    *,
    executable: str | Path | None = None,
    launch: Launchctl,
    domain_uid: int,
) -> str:
    """Why the loaded ``com.ciao.server`` does not run ``live_env``, or ``""``.

    The swap replaces the env the receipt names, so it only means anything if
    that is the env the service is actually running: a Ciaobot.app installed
    over a terminal install, or a hand-edited plist, leaves the receipt and the
    loaded job disagreeing, and the swap would then replace an env nothing is
    using. Checked before anything moves, so the refusal costs the operator a
    re-apply and not their install.

    The loaded job is the authority (see :func:`_loaded_server_program`), and a
    loaded job running an env other than the receipt's refuses even when the
    plist on disk agrees — that is exactly the case where the disk is stale. The
    on-disk plist answers only for a job that is genuinely not loaded, which is
    also when a hand-edited plist is all there is to go on.

    Agreement is the whole install, not just the env directory: the receipt's
    entry point is part of it (see :func:`_runs_the_receipt_install`).

    A missing, unreadable or argument-less plist is *not* a disagreement: a
    service that has not been loaded yet is restored by the rollback's own start
    step, and a plist this process cannot parse is evidence of nothing. Only an
    answer that names a program outside the receipt's install refuses.
    """
    program = _loaded_server_program(launch, domain_uid)
    if program is None:
        path = macos_service.default_launch_agents_dir() / f"{SERVER_LABEL}.plist"
        try:
            with path.open("rb") as handle:
                loaded: Any = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return ""
        if not isinstance(loaded, dict):
            return ""
        arguments = loaded.get("ProgramArguments")
        if not isinstance(arguments, list) or not arguments:
            return ""
        program = str(arguments[0])
    if _runs_the_receipt_install(Path(program), live_env, executable):
        return ""
    return (
        f"the loaded {SERVER_LABEL} runs {program}, not the receipt's "
        f"environment {live_env}; point the LaunchAgent at the install the "
        "receipt names, or reinstall, then apply again"
    )


def run_apply(
    operation_id: str,
    *,
    state_dir: Path | None = None,
    port: int | None = None,
    http_post: PostJson | None = None,
    http_get: GetJson | None = None,
    launchctl: Launchctl | None = None,
    run: Runner = subprocess.run,
    start_service: ServiceStarter | None = None,
    uid: int | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    receipt_path: Path | None = None,
) -> Operation:
    """Stop, swap, start, verify — or roll back. Runs in the updater job.

    The detached half of the apply, launched by :func:`apply_update` through
    launchd. It boots the engine *out* before touching a file, because the
    engine's own 60-second file watcher would otherwise restart a half-swapped
    environment, then renames the live env aside, re-points the durable recovery
    agent at the env it just set aside, moves the staged env into its place,
    rewrites the receipt, starts the service and waits for the target version to
    answer.

    Nothing here resolves a dependency: the environment the swap installs is the
    one :func:`stage_update` built and verified, moved rather than rebuilt
    (#611). That is what makes an apply work with no network at all, and what
    makes "apply what was verified" true rather than nearly true.

    Nothing here raises once the engine is down: every failure is answered by a
    rollback and a persisted phase, because the process that has to survive this
    one dying is the operator's terminal, not the job. Everything that refuses
    *before* the engine is stopped reopens admission on the way out, because
    the foreground half has already closed it and nothing else would.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    post = http_post or _post_json
    get = http_get or _get_json
    launch = launchctl or (lambda args: macos_service._launchctl(args))
    start = start_service or (lambda: macos_service.start_service())
    domain_uid = os.getuid() if uid is None else uid
    base = f"http://localhost:{_engine_port() if port is None else port}"
    status_url = f"{base}/api/startup-status"

    try:
        handle = _acquire_lock_waiting(root, sleep, clock)
    except UpdateInProgress:
        # The handoff lost to a second update. The engine is still running and
        # still drained, and this job is the only thing that knows it, so the
        # cancel goes out before the failure propagates.
        _reopen_admission(post, base)
        raise
    try:
        op = read_operation(root)
        if op is None or op.id != operation_id:
            # A record describing a different update is not this job's to
            # rewrite, so this is the one failure with no phase of its own.
            # The engine is still up and still drained: reopen admission
            # before it propagates.
            _reopen_admission(post, base)
            raise UpdateError(f"no staged update with id {operation_id!r}")
        if op.phase != "applying":
            _reopen_admission(post, base)
            raise UpdateError(f"update {operation_id} is {op.phase}, not applying")

        def advance(phase: str) -> None:
            op.phase = phase
            op.updated_at = _now()
            write_operation(op, root)

        def record(message: str) -> Operation:
            op.phase = "failed"
            op.error = message
            op.updated_at = _now()
            try:
                write_operation(op, root)
            except OSError:
                # A record write fails on a full disk, which is one of the
                # things that brings a run here in the first place. The
                # refusal is still real, so it is returned either way.
                pass
            # Every pre-flight refusal lands here with the engine still up and
            # still drained by the foreground half, and nothing else reopens
            # it. Past the stop there is nothing left to reopen: the cancel is a
            # no-op against a dead engine, which is started again either way.
            _reopen_admission(post, base)
            return op

        # Pre-flight, while the engine is still serving: every one of these
        # refuses before anything has moved, so a `failed` record is the whole
        # answer and there is nothing to roll back.
        receipt = install_receipt.read_receipt(receipt_path)
        if receipt is None:
            return record("no install receipt; there is no installed env to replace")
        if not op.wheel or not Path(op.wheel).is_file():
            return record("the staged wheel is gone; nothing to install")
        if not op.env_python or not Path(op.env_python).exists():
            return record("the staged environment is gone; nothing to install")
        if receipt.version != op.from_version:
            # The install is not the one this update was staged against — the
            # user reinstalled in between. Applying would silently downgrade
            # the new install, and rolling back would restore a receipt that
            # no longer describes what is on disk.
            return record(
                f"the installed engine is {receipt.version}, but this update was "
                f"staged from {op.from_version}; run: ciao update stage"
            )

        live_env = Path(receipt.python).parent.parent
        # Before anything moves: the swap is only meaningful against the env the
        # service actually runs, and a receipt that disagrees with the loaded
        # job means the receipt is the thing that is wrong. Refusing here is the
        # same "nothing is touched" class as the pre-flight checks above. The
        # receipt's entry point is passed in with its env: `install-engine.sh`
        # points the LaunchAgent at `~/.local/bin/ciao`, which is outside the
        # tool env and is re-pointed by this very swap.
        disagreement = _server_plist_disagreement(
            live_env,
            executable=receipt.executable,
            launch=launch,
            domain_uid=domain_uid,
        )
        if disagreement:
            return record(disagreement)
        previous_env = Path(op.stage_dir) / PREVIOUS_ENV_NAME
        env_moved = False

        def stand_down() -> None:
            """Retire the durable recovery agent: this transaction is over.

            A settled record is something the agent has nothing left to do
            about, and a job left loaded would re-read that record every 30
            seconds for as long as launchd keeps it.
            """
            _retire_job(launch, domain_uid, RECOVER_LABEL, *_recovery_plists(root))

        # The engine has to be out before a single file moves: its install
        # watcher restarts it the moment `ciao/__init__.py` disappears, and
        # relaunching into a half-installed env is worse than not updating. A
        # failure here has touched nothing, so it is a plain `failed` and the
        # (still running, or just stopped) engine is started again, which is
        # idempotent either way.
        stopped = False
        ok = False
        try:
            advance("stopping")
            launch(["bootout", f"gui/{domain_uid}/{SERVER_LABEL}"])
            stopped = _wait_until_unreachable(
                get, status_url, _STOP_TIMEOUT, sleep, clock
            )
        except Exception as exc:
            message = _reason(exc)
            _start_best_effort(start)
            return record(message)
        if not stopped:
            _start_best_effort(start)
            return record(
                f"the engine did not stop within {int(_STOP_TIMEOUT)}s; nothing was touched"
            )

        try:
            advance("swapping")
            # One generation of rollback is kept: the old env is renamed, not
            # deleted, and stays until the next update succeeds. From here on
            # a failure has something to undo, which is what `env_moved` says.
            _move_env(live_env, previous_env)
            env_moved = True
            # The net is re-pointed at the env the swap just set aside, and before
            # the staged env is moved into the live env's place: `apply_update`
            # installed `com.ciao.recover` from the staged interpreter, and that
            # directory is about to become the live one, so the agent's program
            # would be a dangling path from the rename onwards — the one window in
            # which nothing can still bring the engine back, and so the one window
            # where launchd has to be able to run `run-recover` on its own. The
            # `previous-env` exists from this instant, is a complete old
            # installation, and is only moved again by the rollback.
            #
            # Best effort with a loud log, as in `apply_update`: a net that could
            # not be re-pointed still leaves `recover_interrupted_apply` covering
            # the paths where the engine does come back, and failing a swap that
            # is otherwise fine over a missing net costs the operator the update.
            try:
                _install_recovery_agent(
                    op,
                    str(previous_env / "bin" / "python"),
                    root,
                    launch,
                    domain_uid,
                )
            except Exception as exc:  # noqa: BLE001 — a missing net is not a failed swap
                logger.warning(
                    "could not re-point the %s agent at %s, so a swap interrupted "
                    "from here is only recoverable when the engine starts: %s",
                    RECOVER_LABEL,
                    previous_env / "bin" / "python",
                    exc,
                )
            # What was verified is what gets installed (#611). The staged env is
            # a real uv tool env, so installing it is a rename plus the two
            # absolute paths a move breaks: no resolver runs here, so the apply
            # needs no network and a dependency released since staging cannot
            # change what is installed. `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` are
            # not pinned here any more because uv is not called here at all.
            _install_staged_env(
                Path(op.env_python).parent.parent,
                live_env,
                bin_dir=Path(receipt.executable).parent,
                wheel=Path(op.wheel),
            )
            reported = run(
                [
                    str(live_env / "bin" / "python"),
                    "-I",
                    "-c",
                    "import ciao; print(ciao.__version__)",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if reported != op.to_version:
                raise UpdateError(
                    f"installed env reports version {reported!r}, not {op.to_version}"
                )

            advance("starting")
            install_receipt.write_receipt(
                replace(
                    receipt,
                    version=op.to_version,
                    installed_at=_now(),
                    previous_version=op.from_version,
                    previous_executable=receipt.executable,
                ),
                receipt_path,
            )
            _require_started(start())

            advance("verifying_start")
            _wait_until_ready(
                get,
                status_url,
                op.to_version,
                _READY_TIMEOUT,
                sleep,
                clock,
                subject="the new engine",
            )
            # Past readiness the update is a fact about the machine, not a
            # claim to be withdrawn: the new engine is installed, the receipt
            # names it and it answered for itself. Recorded as such, past the
            # try below, so a record write that fails cannot roll back an
            # install that is already serving.
            ok = True
        except Exception as exc:
            original = _reason(exc)
            # Every phase write from here on is best-effort. A record write can
            # fail on a full disk — usually the same full disk that broke the
            # swap — and a phase write is bookkeeping: losing it must not stop
            # the rollback that restores the operator's engine, nor override
            # the outcome the rollback already decided.
            _advance_ignoring_failure(advance, "rolling_back")
            errors = _rollback(
                op,
                receipt=receipt,
                receipt_path=receipt_path,
                live_env=live_env,
                previous_env=previous_env,
                env_moved=env_moved,
                domain_uid=domain_uid,
                status_url=status_url,
                launch=launch,
                get=get,
                start=start,
                sleep=sleep,
                clock=clock,
            )
            if errors:
                op.error = f"{original}; rollback failed: {'; '.join(errors)}"
                _advance_ignoring_failure(advance, "rollback_failed")
            else:
                op.error = f"{original}; rolled back to {op.from_version}"
                _advance_ignoring_failure(advance, "rolled_back")
            # The env is whole again and the record says so, so the net has
            # nothing left to guard: retiring it here is what stops the next
            # 30-second tick from finding a terminal record to stand down over.
            stand_down()
            return op
        if ok:
            _advance_ignoring_failure(advance, "applied")
            _prune_previous_envs(root, op)
            # Past readiness the update is a fact about the machine, not a
            # transaction to be recovered, and a job that kept re-reading an
            # `applied` record is an operator wondering what it is for.
            stand_down()
        return op
    finally:
        release_lock(handle)


# The phases an interrupted swap can leave behind, and the only ones recovery
# acts on. `stopping` is deliberately absent: nothing has moved yet, and a
# rollback that deleted the only copy of a live env over it would be the very
# damage recovery exists to prevent. Every other phase is a decision the
# transaction already made and an operator should not have to re-make.
_POST_MOVE_PHASES = ("swapping", "starting", "verifying_start", "rolling_back")


# A `launchctl print` renders `pid = <n>` only while a process is running the
# job, and the job's `arguments` block says what that process is running. Both
# are needed to tell a live *swap* from a live *recovery*, because the two share
# a label: `recover_interrupted_apply` bootstraps `com.ciao.updater` in
# `run-recover` mode, so the recovery job sees its own pid in that print and
# would stand down against itself.
_LOADED_PID = re.compile(r"^[ \t]*pid = (?P<pid>\d+)\s*$", re.MULTILINE)


def _loaded_updater(launch: Launchctl, domain_uid: int) -> str:
    """What ``launchctl print`` says about ``com.ciao.updater``, or ``""``.

    Empty for every answer that names no loaded job: a non-zero exit, a
    launchctl that cannot be run at all, or output with no field in it. A
    one-shot LaunchAgent stays registered in launchd after its process exits, so
    this being non-empty says the job is *loaded*, and nothing more than that.
    """
    try:
        printed = launch(["print", f"gui/{domain_uid}/{UPDATER_LABEL}"])
    except OSError:
        return ""
    if printed.returncode != 0:
        return ""
    return printed.stdout or ""


def _updater_running(launch: Launchctl, domain_uid: int) -> bool:
    """Whether a process is running ``com.ciao.updater`` *right now*.

    Deliberately not "is the job loaded". A one-shot LaunchAgent stays loaded
    in launchd after its process exits, and nothing here ever boots it out, so
    the job left behind by the very crash recovery exists for would read as a
    live apply for ever — and a recovery that stands down from a dead swap is
    exactly the stranded machine this is here to prevent. ``launchctl print``
    names the pid only while something is running it, so that is what is asked.

    A launchctl that cannot be run at all answers False for the same reason a
    missing job does: there is no swap in flight, and the rollback that follows
    is the same total one `run_apply` would have run itself.
    """
    return _LOADED_PID.search(_loaded_updater(launch, domain_uid)) is not None


def _swap_in_flight(launch: Launchctl, domain_uid: int) -> bool:
    """Whether a live ``run-apply`` owns ``com.ciao.updater`` — a real swap.

    The pid alone is not enough, and getting this wrong strands the machine
    recovery exists to repair. ``run-recover`` runs under the *same* label
    (:func:`recover_interrupted_apply` bootstraps it there), so the process
    executing :func:`recover_apply` is `com.ciao.updater` and `launchctl print`
    reports its own live pid back to it. A job whose arguments name `run-recover`
    is this very recovery and must proceed; only a loaded *and* running job whose
    arguments name `run-apply` is a swap in flight.

    Anything launchd does not render — no pid, no arguments block — answers
    False, for the same reason a launchctl that cannot be run does: there is no
    swap in flight to race, the lock this job already holds is what actually
    keeps the two apart, and answering True here would leave every interrupted
    swap unrolled back.
    """
    printed = _loaded_updater(launch, domain_uid)
    if _LOADED_PID.search(printed) is None:
        return False
    return "run-apply" in _loaded_tokens(printed, "arguments")


def _recover_python(op: Operation) -> str:
    """The interpreter the recovery job runs from.

    Whichever of the two envs the swap has not consumed yet: the staged one
    while it is still only a directory, because that is what `run-apply` itself
    runs from; then the previous env, which exists from the moment the live one
    is renamed aside and is both the second-best thing to run the recovery *from*
    and the last one guaranteed to have `ciao` importable. Failing both, this
    process's own interpreter: it is the engine that is running the recovery, and
    the recovery job is a *sibling* of `com.ciao.server`, so the bootout that
    follows cannot take it down.
    """
    for candidate in (
        op.env_python,
        str(Path(op.stage_dir) / PREVIOUS_ENV_NAME / "bin" / "python"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def recover_interrupted_apply(
    *,
    state_dir: Path | None = None,
    launchctl: Launchctl | None = None,
    uid: int | None = None,
    updater_loaded: Callable[[], bool] | None = None,
) -> Operation | None:
    """Hand a swap an earlier boot interrupted to a detached recovery job.

    :func:`run_apply` moves the live env aside from a *sibling* launchd job, so
    the engine being up says nothing at all about whether the apply is still
    running: a machine that reboots mid-swap comes back with the record in one
    of the post-move phases, the env renamed to `previous-env`, and launchd's
    `KeepAlive` on `com.ciao.server` perfectly willing to start the
    half-installed engine. Called once from `ciao/main.py` at startup, on the
    machine that owns the state dir, this recognises that record and bootstraps
    `com.ciao.updater` in `run-recover` mode, which is what actually rolls back
    (:func:`recover_apply`).

    Nothing is moved, stopped or waited on in this process, and that is the
    whole design: the engine cannot roll back the environment it is running out
    of, the `bootout` that has to come first would take it down before it
    finished, and `/api/startup-status` has nothing to answer until the server
    binds. The recovery job is outside all of that by construction, which is the
    same reason `apply_update` hands its half to a job rather than doing it
    inline.

    Fail-safe at every step, because a recovery that guesses is worse than one
    that does nothing:

    * A record that is not in a post-move phase is left alone, terminal phases
      included: it is an outcome, not an interrupted transaction. `stopping` is
      in that set because nothing has moved yet, and a rollback that deleted the
      only copy of a live env over it would be the damage this exists to
      prevent.
    * A `com.ciao.updater` that is *running* means another process owns the
      swap, so the record comes back untouched. The engine being up is not
      evidence that the updater died, and two rollbacks over one env is the race
      this function exists to prevent. A job that is only still *loaded* — which
      is what a killed updater leaves behind — is not a running swap and does
      not defer, or nothing would ever recover the crash it left. This asks only
      whether *anything* is running that label, which is the safe direction
      here: a deferral costs one boot, while standing down in the job it
      bootstraps would cost the rollback entirely (see :func:`_swap_in_flight`).
    * A recovery job that will not load leaves the phase alone and writes the
      reason onto the record, so the next boot tries again and the operator can
      read why it did not.
    * Nothing raises. A startup path must not die on bookkeeping, so any
      unexpected failure is logged and reported as "nothing was recovered".
    """
    root = state_dir or default_state_dir()
    op: Operation | None = None
    try:
        op = read_operation(root)
        if op is None or op.phase not in _POST_MOVE_PHASES:
            return None
        # A local alias, not a closure over `op`: this is the record recovery
        # is about, and it has to be writable from inside `advance`.
        record: Operation = op
        interrupted_at = record.phase

        launch = launchctl or (lambda args: macos_service._launchctl(args))
        domain_uid = os.getuid() if uid is None else uid
        running = (
            updater_loaded()
            if updater_loaded is not None
            else _updater_running(launch, domain_uid)
        )
        if running:
            logger.warning(
                "engine update %s is %s and %s is still running; leaving it to the updater",
                record.id,
                interrupted_at,
                UPDATER_LABEL,
            )
            return record

        def advance(phase: str) -> None:
            record.phase = phase
            record.updated_at = _now()
            write_operation(record, root)

        plist_path = _write_updater_plist(
            record, _recover_python(record), root, verb="run-recover"
        )
        # bootout before bootstrap, as in `apply_update`: a job left loaded from
        # an earlier attempt would make bootstrap fail with "service already
        # loaded" and leave a half-swapped env with nothing to finish it. The
        # loaded check above is the real guard; this is the belt to it.
        launch(["bootout", f"gui/{domain_uid}/{UPDATER_LABEL}"])
        bootstrap = launch(["bootstrap", f"gui/{domain_uid}", str(plist_path)])
        if bootstrap.returncode != 0:
            detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
            logger.error(
                "could not start the recovery job for %s: %s",
                record.id,
                detail or "launchctl bootstrap failed",
            )
            # The phase stays where it was: this is still an interrupted swap, so
            # the next boot tries again. The reason goes on the record, because a
            # record that says nothing is what leaves an operator guessing.
            record.error = (
                f"interrupted during {interrupted_at}; recovery job did not "
                f"start: {detail or 'launchctl bootstrap failed'}"
            )
            _advance_ignoring_failure(advance, interrupted_at)
            return record
        logger.warning(
            "engine update %s was interrupted during %s; recovery job bootstrapped",
            record.id,
            interrupted_at,
        )
        return record
    except Exception:  # noqa: BLE001 — a startup path must not die on recovery
        logger.exception("Engine update recovery failed")
        return op


def recover_apply(
    operation_id: str,
    *,
    state_dir: Path | None = None,
    port: int | None = None,
    http_post: PostJson | None = None,
    http_get: GetJson | None = None,
    launchctl: Launchctl | None = None,
    start_service: ServiceStarter | None = None,
    uid: int | None = None,
    sleep: Sleep = time.sleep,
    clock: Clock = time.monotonic,
    receipt_path: Path | None = None,
) -> Operation | None:
    """Stop the engine and run the same ``_rollback`` for an interrupted swap.

    The program of the durable ``com.ciao.recover`` agent, and of the
    ``com.ciao.updater`` job :func:`recover_interrupted_apply` bootstraps when
    the engine does come back. Both are *siblings* of the engine for the same
    reason: it cannot restore the env it is running out of, and the ``bootout``
    that has to come first would kill a child of the job doing the restoring.
    So this is ``run_apply``'s post-preflight sequence with a rollback in place
    of a swap — lock, read the record, stop the engine, then the same total
    ``_rollback`` — and it settles the record the same way.

    The lock is taken without waiting, and it is the first thing taken, because
    for the first tick of its life this job runs *beside* the apply that
    installed it: the live env has not been renamed away yet, and rolling back
    over a swap in flight is the race the durable agent must not join. The
    agent's whole job is to answer "is there still a stranded swap?" every
    ``_RECOVER_INTERVAL`` seconds, so standing down for one tick costs nothing,
    and retiring the job there would leave the machine with no net in the very
    window it exists for. Nothing is posted and nothing is written when it
    stands down: the swap in flight owns the record, and it owns the drain.

    That stand-down is asked with :func:`_swap_in_flight` rather than "is
    ``com.ciao.updater`` running", because the one-shot job this function also
    runs under carries the *same* label: reading the bare pid made the recovery
    stand down against its own process and restore nothing, which is the
    acceptance criterion for the whole feature.

    Once the lock is held, this is the only process working on the record, so
    every other answer here is final for this operation: no record, a different
    operation, or a phase that has already been settled all mean there is
    nothing to undo, and the agent retires its own job and plist rather than
    re-reading that same answer in 30 seconds. None of those is a failure, and
    none of them touches the engine, the env or the record — a recovery that
    never owned the drain must not reopen admission that someone else closed.
    A *newer* update's net is the exception to the retiring: it is installed
    under this same label and in these same two plists, so a stale one-shot
    that pulls them down would take away the only recovery for the update that
    is actually in flight.

    Nothing here raises once the engine is down, and nothing before the stop
    touches a file: the record is the outcome, and the operator's engine is
    started again either way.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    # Accepted and deliberately never used: this job owns no drain, so it has
    # no admission to reopen — `_reopen_admission` belongs to the foreground
    # half, the process that actually closed admission. It stays in the
    # signature so a caller can hand in a recorder and assert that the recovery
    # path makes no HTTP request at all.
    del http_post
    get = http_get or _get_json
    launch = launchctl or (lambda args: macos_service._launchctl(args))
    start = start_service or (lambda: macos_service.start_service())
    domain_uid = os.getuid() if uid is None else uid
    base = f"http://localhost:{_engine_port() if port is None else port}"
    status_url = f"{base}/api/startup-status"

    try:
        handle = acquire_lock(root)
    except UpdateInProgress:
        # Someone else is working on the record right now, which is the normal
        # state of the world for every tick that lands while a swap is in
        # flight. Nothing is posted and nothing is written — the swap holding
        # the lock owns both — and the agent stays loaded for its next tick.
        logger.info(
            "another process owns the update state dir; standing down for this tick"
        )
        return None
    try:
        op = read_operation(root)
        if op is None:
            # No record at all: the transaction this plist was installed for is
            # gone, so the net has nothing left to guard and retires itself.
            _retire_job(launch, domain_uid, RECOVER_LABEL, *_recovery_plists(root))
            return None
        if op.id != operation_id:
            # A newer update has taken the record over, and there is nothing
            # here to undo: the operation in flight is not this job's to
            # rewrite, and the engine is untouched and still serving. This plist
            # *is* stale — but the net in front of the operator now is the new
            # update's, installed under this same label and in these same two
            # plists, so retiring from here would pull down the only recovery
            # for the swap that is actually in flight. It stays loaded and keeps
            # answering for its own (retired) id until the newer one settles and
            # retires the pair.
            logger.info(
                "engine update %s is the one in flight, not %s; leaving the "
                "recovery net standing",
                op.id,
                operation_id,
            )
            return None
        if op.phase not in _POST_MOVE_PHASES:
            # The transaction finished while this job was starting. It settled
            # the record itself, and a rollback over a settled phase would undo
            # an outcome the operator already has.
            _retire_job(launch, domain_uid, RECOVER_LABEL, *_recovery_plists(root))
            return None
        if _swap_in_flight(launch, domain_uid):
            # Belt to the lock above: an apply that holds the record and is
            # still running must not be second-guessed by a second rollback over
            # one env, whatever the lock is doing about it. A live job of this
            # same label running `run-recover` is *this* process — see
            # `_swap_in_flight` — and the check has to let it through.
            logger.warning(
                "engine update %s is %s and a %s run-apply is still running; "
                "standing down",
                op.id,
                op.phase,
                UPDATER_LABEL,
            )
            return None

        def advance(phase: str) -> None:
            op.phase = phase
            op.updated_at = _now()
            write_operation(op, root)

        def record_failure(message: str) -> Operation:
            """Settle the record as an unrecovered swap, touching no file."""
            op.error = message
            _advance_ignoring_failure(advance, "rollback_failed")
            return op

        receipt = install_receipt.read_receipt(receipt_path)
        if receipt is None:
            # The rollback is built from the receipt: without one there is no
            # live env to put back, and the engine is still up and serving. The
            # record is now terminal, so the agent's next tick stands down over
            # it and retires itself — this branch is the one place that reports
            # rather than repairs, and it launches nothing.
            return record_failure(
                "no install receipt, so the interrupted swap cannot be rolled back"
            )

        live_env = Path(receipt.python).parent.parent
        previous_env = Path(op.stage_dir) / PREVIOUS_ENV_NAME
        # The retained previous env *is* the evidence that the move happened, and
        # its presence is the only thing that says so: a swap interrupted before
        # its rename leaves nothing under this name, and one interrupted after
        # it leaves the operator's install here whether or not `uv` has since
        # recreated `live_env`. Asking for the live env to be *absent* as well
        # mistook a successfully recreated new env for a swap that never moved
        # anything — so a crash in `starting` or `verifying_start` restored the
        # old receipt over the new engine and reported `rollback_failed` over a
        # restore that had not happened. `env_moved` therefore means "there is
        # something to put back", and `_rollback` replaces the half-installed
        # env with it rather than leaving the new one in place beside an old
        # receipt.
        env_moved = previous_env.exists()

        logger.warning(
            "recovering engine update %s interrupted during %s", op.id, op.phase
        )
        _advance_ignoring_failure(advance, "rolling_back")
        errors = _rollback(
            op,
            receipt=receipt,
            receipt_path=receipt_path,
            live_env=live_env,
            previous_env=previous_env,
            env_moved=env_moved,
            domain_uid=domain_uid,
            status_url=status_url,
            launch=launch,
            get=get,
            start=start,
            sleep=sleep,
            clock=clock,
        )
        if errors:
            op.error = (
                f"interrupted during recovery; rollback failed: {'; '.join(errors)}"
            )
            _advance_ignoring_failure(advance, "rollback_failed")
        else:
            op.error = (
                f"interrupted during recovery; rolled back to {op.from_version}"
            )
            _advance_ignoring_failure(advance, "rolled_back")
        # The record is settled, so the net has nothing left to guard. Retiring
        # it here is also what stops the next tick from re-reading a terminal
        # phase to reach the same conclusion. It boots this very process out
        # when this *is* the agent, which is why the record is already written,
        # the env is already back and the engine has already been started.
        _retire_job(launch, domain_uid, RECOVER_LABEL, *_recovery_plists(root))
        return op
    finally:
        release_lock(handle)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``ciao update``."""
    parser = argparse.ArgumentParser(prog="ciao update", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="Stage an engine update (no downtime)")
    stage.add_argument("--version", default=None, help="release to stage, no leading v")
    stage.add_argument("--json", action="store_true", help="print the operation record")

    apply_cmd = sub.add_parser("apply", help="Drain, then apply a staged update")
    apply_cmd.add_argument(
        "--drain-timeout",
        type=_drain_timeout_arg,
        default=_DEFAULT_DRAIN_TIMEOUT,
        help=(
            "seconds to wait for active chats before giving up "
            f"(default: {int(_DEFAULT_DRAIN_TIMEOUT)}, minimum: {int(_MIN_DRAIN_TIMEOUT)})"
        ),
    )
    apply_cmd.add_argument("--json", action="store_true", help="print the operation record")

    status = sub.add_parser("status", help="Print the current update record")
    status.add_argument("--json", action="store_true", help="print the operation record")

    # Hidden: this is what the one-shot updater job runs, not something an
    # operator types. It exits 0 whatever happens, because the record is the
    # outcome and a non-zero exit would make launchd report a rolled-back
    # update as a failed job.
    detached = sub.add_parser("run-apply", help=argparse.SUPPRESS)
    detached.add_argument("--operation", required=True, help="staged operation id")

    # Hidden, and the same job in its recovery role: `recover_interrupted_apply`
    # bootstraps this when it finds a swap an earlier boot interrupted, and it
    # exits 0 for the same reason `run-apply` does.
    recovery = sub.add_parser("run-recover", help=argparse.SUPPRESS)
    recovery.add_argument("--operation", required=True, help="interrupted operation id")

    args = parser.parse_args(argv)

    if args.command == "status":
        op = read_operation()
        if op is None:
            print("no update staged")
            return 0
        print(_format(op, args.json))
        return 0

    if args.command == "run-apply":
        try:
            run_apply(args.operation)
        except Exception as exc:  # noqa: BLE001 — the record is the outcome
            print(f"Error: {exc}", file=sys.stderr)
        return 0

    if args.command == "run-recover":
        try:
            recover_apply(args.operation)
        except Exception as exc:  # noqa: BLE001 — the record is the outcome
            print(f"Error: {exc}", file=sys.stderr)
        return 0

    # Staging and applying are only safe for an engine the installer owns: an
    # editable checkout or a bundled app is updated by its own path, and
    # preparing a staged env for one would be a no-op with a confusing side
    # effect.
    if package_version.detect_install_mode() != "installer":
        print(
            "Error: in-app updates are only for engines installed with the "
            "Ciaobot engine installer",
            file=sys.stderr,
        )
        return 2
    if args.command == "apply":
        # A drain can wait ten minutes, and closing the terminal (SIGHUP) or a
        # supervisor stopping the process (SIGTERM) is as ordinary a way to end
        # that wait as Ctrl-C is. Both default to killing the process outright,
        # which would skip the cancel that reopens admission and leave the
        # engine refusing every turn with nothing in the record to explain it.
        # Turning them into the KeyboardInterrupt the handler below already
        # deals with, and restoring the previous handlers on the way out, is
        # the whole of the fix. `signal.signal` needs the main thread and a
        # real handler; neither holds everywhere this is callable, and neither
        # failure is worth refusing an update over.
        def _interrupt(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        restore: dict[signal.Signals, Any] = {}
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with contextlib.suppress(ValueError, OSError, AttributeError):
                restore[sig] = signal.getsignal(sig)
                signal.signal(sig, _interrupt)
        try:
            op = apply_update(drain_timeout=args.drain_timeout)
        except KeyboardInterrupt:
            # 130 is the conventional exit for SIGINT, and this is the same
            # event: the apply was interrupted, the record says why, and
            # admission has been reopened.
            print("Error: update cancelled", file=sys.stderr)
            return 130
        except UpdateError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        finally:
            for sig, handler in restore.items():
                if handler is not None:
                    with contextlib.suppress(ValueError, OSError, AttributeError):
                        signal.signal(sig, handler)
        if args.json:
            print(_format(op, True))
        else:
            print(
                f"applying {op.to_version}: the engine will restart; "
                "check: ciao update status"
            )
        return 0
    try:
        op = stage_update(args.version)
    except UpdateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(_format(op, args.json))
    return 0


def _format(op: Operation, as_json: bool) -> str:
    if as_json:
        return json.dumps(asdict(op), indent=2, sort_keys=True)
    # The record's own phase, not a fixed word: a `failed` or still-running
    # operation printed as "staged" tells an operator the update is ready when
    # it is not.
    line = f"{op.phase} {op.to_version} in {op.stage_dir}"
    if op.error:
        line = f"{line}: {op.error}"
    return line


if __name__ == "__main__":
    raise SystemExit(main())
