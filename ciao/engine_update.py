"""Stage an engine update for installer-managed installs (#562 update transaction, part 1).

Everything here happens before any downtime and outside the running
environment: lock, durable operation record, signed-manifest verification,
wheel download + digest check, and a fully installed staged env. The swap,
drain and rollback are #570.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Callable

from ciao import package_version, release_manifest

PHASES = ("resolving", "downloading", "verifying", "staging", "staged", "failed")
RELEASE_BASE = "https://github.com/raffaelefarinaro/ciaobot/releases/download"
MANIFEST_NAME = release_manifest.MANIFEST_NAME
SIGNATURE_NAME = release_manifest.SIGNATURE_NAME
PREVIOUS_RECEIPT_NAME = "previous-receipt.json"
OPERATION_NAME = "operation.json"
LOCK_NAME = "update.lock"
_CHUNK = 1 << 20

Fetch = Callable[[str, Path], None]
Runner = Callable[..., subprocess.CompletedProcess[str]]


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
    url: str, dest: Path, *, attempts: int = 3, timeout: float = 30.0
) -> None:
    """Stream ``url`` to ``dest`` with plain urllib, retrying transient errors.

    A stalled release CDN is the common failure, not a 404, so transient
    ``OSError``/``URLError`` gets up to ``attempts`` tries with a short sleep.
    Bytes land in a ``.part`` sibling and are renamed in one step, so an
    interrupted run never leaves a truncated file that a later digest check
    would have to distinguish from a corrupt one.
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
                with part.open("wb") as handle:
                    while True:
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
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
        fetch(f"{release_url}/{filename}", wheel_path)
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
        op.phase = "failed"
        op.error = str(exc)
        op.updated_at = _now()
        write_operation(op, state_dir)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(str(exc)) from exc

    advance("staged")
    return op


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``ciao update``."""
    parser = argparse.ArgumentParser(prog="ciao update", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    stage = sub.add_parser("stage", help="Stage an engine update (no downtime)")
    stage.add_argument("--version", default=None, help="release to stage, no leading v")
    stage.add_argument("--json", action="store_true", help="print the operation record")

    status = sub.add_parser("status", help="Print the current update record")
    status.add_argument("--json", action="store_true", help="print the operation record")

    args = parser.parse_args(argv)

    if args.command == "status":
        op = read_operation()
        if op is None:
            print("no update staged")
            return 0
        print(_format(op, args.json))
        return 0

    # Staging is only safe for an engine the installer owns: an editable
    # checkout or a bundled app is updated by its own path, and preparing a
    # staged env for one would be a no-op with a confusing side effect.
    if package_version.detect_install_mode() != "installer":
        print(
            "Error: in-app updates are only for engines installed with the "
            "Ciaobot engine installer",
            file=sys.stderr,
        )
        return 2
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
    return f"staged {op.to_version} in {op.stage_dir}"


if __name__ == "__main__":
    raise SystemExit(main())
