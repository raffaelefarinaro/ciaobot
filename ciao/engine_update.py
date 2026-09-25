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
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Callable

from ciao import install_receipt, macos_service, package_version, release_manifest

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
    """The durable record of one staging run, rewritten at every phase."""

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
                            # something this release does not describe.
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
        env_dir = stage_dir / "env"
        uv_bin = uv or find_uv(_receipt_uv())
        py = python_version or f"{sys.version_info.major}.{sys.version_info.minor}"
        env_python = env_dir / "bin" / "python"
        run(
            [uv_bin, "venv", "--python", py, str(env_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
        )
        # Every dependency is installed here, before any downtime, so the swap
        # in #570 is a rename rather than a network-bound install.
        run(
            [
                uv_bin,
                "pip",
                "install",
                "--python",
                str(env_python),
                str(wheel_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=_UV_TIMEOUT,
        )
        out = run(
            [str(env_python), "-I", "-c", "import ciao; print(ciao.__version__)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out != target:
            raise UpdateError(f"staged env reports version {out!r}, not {target}")
        op.env_python = str(env_python)

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


def _reason(exc: Exception) -> str:
    """The operator-facing explanation of ``exc``, subprocess output included.

    ``run(..., check=True)`` reports only "returned non-zero exit status 1";
    uv's own reason is in the captured output, and dropping it is what makes a
    failed update unexplainable from the record.
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


def _post_json(url: str, *, opener: Opener = urllib.request.urlopen) -> dict[str, Any]:
    """POST an empty body to ``url`` and return the JSON object answer.

    ``opener`` is a parameter so the failure-injection tests never open a
    socket. The timeout is short because both callers are talking to a server
    on this machine, where a slow answer is a wedged one.
    """
    request = urllib.request.Request(url, method="POST", data=b"")
    with opener(request, timeout=_HTTP_TIMEOUT) as response:
        return _decode_body(response.read())


def _get_json(
    url: str, *, opener: Opener = urllib.request.urlopen
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


def _write_updater_plist(op: Operation, python: str, state_dir: Path) -> Path:
    """Write the one-shot updater LaunchAgent and return the path written.

    Owner-only, and through a temp file, because ``launchctl bootstrap`` reads
    it immediately afterwards and a half-written plist would be a job that
    never loads with nothing in the record to explain it.
    """
    log = Path(op.stage_dir) / "updater.log"
    plist: dict[str, Any] = {
        "Label": UPDATER_LABEL,
        "ProgramArguments": [
            python,
            "-I",
            "-m",
            "ciao.engine_update",
            "run-apply",
            "--operation",
            op.id,
        ],
        # RunAtLoad, once: the job performs the swap and exits. `KeepAlive`
        # false is the safety property here — a failed swap must not become a
        # relaunch loop that re-runs it every few seconds.
        "RunAtLoad": True,
        "KeepAlive": False,
        # So the engine's bootout cannot reach the updater's children, and the
        # updater's exit cannot drag the engine down with it.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    target = state_dir / UPDATER_PLIST_NAME
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


def _staged_python_version(run: Runner, python: str) -> str:
    """The ``X.Y`` of the staged env's interpreter, which is what uv installs with.

    Taken from the staged env and not from this process: staging that env is
    how the verified release got its interpreter, and installing with any other
    one would produce an environment nobody verified.
    """
    out = run(
        [
            python,
            "-I",
            "-c",
            "import sys;print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not out:
        raise UpdateError("the staged environment did not report a Python version")
    return out


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
    drain_timeout: float = 600.0,
    state_dir: Path | None = None,
    port: int | None = None,
    http_post: PostJson | None = None,
    http_get: GetJson | None = None,
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
    and the handoff without launchd, a network, or a real engine.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    post = http_post or _post_json
    get = http_get or _get_json
    launch = launchctl or (lambda args: macos_service._launchctl(args))
    domain_uid = os.getuid() if uid is None else uid
    base = f"http://localhost:{_engine_port() if port is None else port}"
    handle = acquire_lock(root)
    try:
        op = read_operation(root)
        staged_python = Path(op.stage_dir, "env", "bin", "python") if op else None
        if (
            op is None
            or op.phase != "staged"
            or staged_python is None
            or not staged_python.exists()
        ):
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
            # Three consecutive empty readings rather than one: a chat can look
            # idle between two of its own phases, and stopping the engine then
            # would cut a turn short.
            deadline = clock() + drain_timeout
            idle = 0
            while True:
                body = get(f"{base}/api/active-chats") or {}
                active = body.get("active_chat_ids") or []
                idle = idle + 1 if not active else 0
                if idle >= _IDLE_POLLS_REQUIRED:
                    break
                if clock() >= deadline:
                    raise _drain_timeout(drain_timeout)
                sleep(_POLL_INTERVAL)
        except Exception as exc:
            message = _reason(exc)
            fail(message)
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
        except Exception as exc:
            message = _reason(exc)
            fail(message)
            raise UpdateError(message) from exc

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


def run_apply(
    operation_id: str,
    *,
    state_dir: Path | None = None,
    port: int | None = None,
    http_get: GetJson | None = None,
    launchctl: Launchctl | None = None,
    uv: str | None = None,
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
    environment, then renames the live env aside, installs the verified wheel
    over it, rewrites the receipt, starts the service and waits for the target
    version to answer.

    Nothing here raises once the engine is down: every failure is answered by a
    rollback and a persisted phase, because the process that has to survive this
    one dying is the operator's terminal, not the job.
    """
    root = state_dir or default_state_dir()
    root.mkdir(parents=True, exist_ok=True)
    get = http_get or _get_json
    launch = launchctl or (lambda args: macos_service._launchctl(args))
    start = start_service or (lambda: macos_service.start_service())
    domain_uid = os.getuid() if uid is None else uid
    base = f"http://localhost:{_engine_port() if port is None else port}"
    status_url = f"{base}/api/startup-status"

    handle = _acquire_lock_waiting(root, sleep, clock)
    try:
        op = read_operation(root)
        if op is None or op.id != operation_id:
            # A record describing a different update is not this job's to
            # rewrite, so this is the one failure with no phase of its own.
            raise UpdateError(f"no staged update with id {operation_id!r}")
        if op.phase != "applying":
            raise UpdateError(f"update {operation_id} is {op.phase}, not applying")

        def advance(phase: str) -> None:
            op.phase = phase
            op.updated_at = _now()
            write_operation(op, root)

        def record(message: str) -> Operation:
            op.phase = "failed"
            op.error = message
            op.updated_at = _now()
            write_operation(op, root)
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
        try:
            uv_bin = uv or find_uv(receipt.uv)
        except UpdateError as exc:
            return record(_reason(exc))

        live_env = Path(receipt.python).parent.parent
        previous_env = Path(op.stage_dir) / PREVIOUS_ENV_NAME
        env_moved = False

        # The engine has to be out before a single file moves: its install
        # watcher restarts it the moment `ciao/__init__.py` disappears, and
        # relaunching into a half-installed env is worse than not updating. A
        # failure here has touched nothing, so it is a plain `failed` and the
        # (still running, or just stopped) engine is started again, which is
        # idempotent either way.
        stopped = False
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
            run(
                [
                    uv_bin,
                    "tool",
                    "install",
                    "--force",
                    "--python",
                    _staged_python_version(run, op.env_python),
                    op.wheel,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_UV_TIMEOUT,
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

            advance("applied")
            _prune_previous_envs(root, op)
            return op
        except Exception as exc:
            original = _reason(exc)
            advance("rolling_back")
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
                advance("rollback_failed")
            else:
                op.error = f"{original}; rolled back to {op.from_version}"
                advance("rolled_back")
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
        type=float,
        default=600.0,
        help="seconds to wait for active chats before giving up (default: 600)",
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
        try:
            op = apply_update(drain_timeout=args.drain_timeout)
        except UpdateError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
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
