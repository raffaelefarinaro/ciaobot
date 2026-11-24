"""App-mode engine support: the workspace pointer, ready record and parent watchdog.

Ciaobot for Mac owns this engine: the app spawns ``ciao run --app-engine`` as
its child and the engine installs no LaunchAgent of its own. Everything the
service definition used to carry — which workspace, which runtime root — moves
into small JSON records the engine writes under the app-owned state directory
(:func:`state_dir`), and the app reads to prove the engine is up:

- ``workspace.json`` — the workspace pointer: ``{"schema": 1, "workspace": "<abs>",
  "runtime_root": "<abs>"}``. Durable; written by ``setup_workspace`` under the
  app and rewritten by the takeover to repoint the same file, so
  :func:`write_pointer` accepts any workspace and :func:`clear_pointer` removes
  one only while it still names the expected workspace.
- ``ready.json`` — ``{"schema": 1, "pid": ..., "port": ..., "startup_id": ...,
  "workspace": "<abs>", "bootstrap": bool}``, rewritten on every start,
  including after the exec-style restart, which keeps the same pid. Once
  both listeners are bound, it also records the private loopback HTTPS
  ``tls_port`` so later launches can preserve the PWA origin. The TLS pin
  is delivered to the owning app only through its inherited readiness pipe.

The kqueue parent watchdog SIGTERMs the engine when the app dies, so an
app-owned engine cannot outlive its owner. The module imports on every
platform (macOS-only behaviour is reached only through a macOS-only flag), but
every behaviour here is macOS only: ``--app-engine`` is refused outside the
app bundle, so nothing else ever reaches these paths.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import os
import select
import socket
import ssl
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import uvicorn
from starlette.applications import Starlette
from starlette.types import Message, Receive, Scope, Send
SCHEMA = 1

WORKSPACE_POINTER_NAME = "workspace.json"
READY_RECORD_NAME = "ready.json"
TLS_PORT_IN_USE_EXIT_CODE = 73


def state_dir() -> Path:
    """The engine's app-owned state directory, held private (0700)."""
    return Path.home() / "Library" / "Application Support" / "Ciaobot" / "engine"


@dataclass(frozen=True, slots=True)
class WorkspacePointer:
    """The workspace (and its runtime root) the app has named for this engine."""

    workspace: Path
    runtime_root: Path


#: A record is a few hundred bytes of JSON. Anything past this is not the
#: record some engine or takeover wrote; reading it is not worth the wait.
_MAX_RECORD_BYTES = 64 * 1024


def read_private_json(target: Path, *, label: str) -> dict[str, Any] | None:
    """One strict, bounded read of an engine record, or ``None``.

    The record is read the way the repo reads its other owner-only state
    (:func:`ciao.os_support.definition.read_bounded_file`): opened without
    following a trailing symlink, bounded in size, and checked with the strict
    descriptor verifier — the inode must be a regular file owned by this user,
    ``0o600``, and on macOS must grant no other principal through its ACL.
    Anything else — missing, a symlink, group- or world-readable, oversized,
    unparsable — reads as "no record", same as an absent file: this module's
    consumers treat a damaged or foreign record as bootstrap, never as data.
    """
    from ciao.os_support.definition import DefinitionReadError, read_bounded_file

    try:
        read = read_bounded_file(
            target,
            max_bytes=_MAX_RECORD_BYTES,
            label=label,
            require_private_owner=True,
        )
    except (DefinitionReadError, OSError):
        return None
    try:
        parsed: Any = json.loads(read.data.decode("utf-8"))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def read_pointer_record(path: Path | None = None) -> WorkspacePointer | None:
    """The pointer's recorded paths, whether or not they are directories now.

    The same strict file checks and schema test as :func:`read_pointer` (an
    owner-only file, schema exactly ``1``, absolute paths), without the
    directory requirement: after a workspace rename the pointer legitimately
    names a folder that is gone, and the pre-boot move needs to see that.
    """
    target = state_dir() / WORKSPACE_POINTER_NAME if path is None else Path(path)
    raw = read_private_json(target, label="the app engine workspace pointer")
    if raw is None:
        return None
    schema = raw.get("schema")
    # ``bool`` is an ``int`` subclass: JSON `true` must not pass as schema 1,
    # and a float `1.0` compares equal to 1 but is not the schema.
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != SCHEMA:
        return None
    workspace = raw.get("workspace")
    runtime_root = raw.get("runtime_root")
    if not isinstance(workspace, str) or not isinstance(runtime_root, str):
        return None
    workspace_path = Path(workspace)
    runtime_path = Path(runtime_root)
    if not workspace_path.is_absolute() or not runtime_path.is_absolute():
        return None
    return WorkspacePointer(workspace=workspace_path, runtime_root=runtime_path)


def read_pointer(path: Path | None = None) -> WorkspacePointer | None:
    """The pointer at ``path``, or ``None`` when nothing usable is stored there.

    Fail-safe by design: a missing file, a symlink, a file that is not this
    user's owner-only record, unparsable JSON, a foreign schema, a relative
    path or a workspace/runtime root that is not a directory all read as
    "there is no pointer" — the caller treats that as bootstrap instead of
    adopting a half-remembered workspace from a damaged file.
    """
    pointer = read_pointer_record(path)
    if pointer is None:
        return None
    if not pointer.workspace.is_dir() or not pointer.runtime_root.is_dir():
        return None
    return pointer


def write_pointer(
    workspace: Path, runtime_root: Path, *, path: Path | None = None
) -> Path:
    """Point the engine at ``workspace`` (and ``runtime_root``); return the path.

    Resolves and replaces whatever the file named, so the takeover (and a later
    in-app workspace move) repoints the same record rather than minting a
    second one. Atomic: the content is written to a private temp file in the
    target directory and renamed in one step.
    """
    target = state_dir() / WORKSPACE_POINTER_NAME if path is None else Path(path)
    payload = {
        "schema": SCHEMA,
        "workspace": str(Path(workspace).resolve()),
        "runtime_root": str(Path(runtime_root).resolve()),
    }
    write_private_json(target, payload)
    return target


def clear_pointer(*, expected_workspace: Path, path: Path | None = None) -> bool:
    """Remove the pointer, only while it still names ``expected_workspace``.

    Both sides are resolved before the comparison: the record stores the
    resolved directory (``write_pointer`` wrote it that way) and the caller may
    name the same directory through an alias, so a symlinked path or the
    macOS ``/var``/``/private/var`` pair must still match. A pointer naming a
    different workspace is the only thing standing between this engine and
    that workspace's install; it is never cleared on a guess.
    """
    target = state_dir() / WORKSPACE_POINTER_NAME if path is None else Path(path)
    pointer = read_pointer(target)
    if pointer is None:
        return False
    if pointer.workspace.resolve() != Path(expected_workspace).resolve():
        return False
    # Gone between the read and this unlink (another rollback got there first)
    # still leaves it cleared: the call is idempotent.
    target.unlink(missing_ok=True)
    return True


@dataclass(frozen=True, slots=True)
class LocalTLSEndpoint:
    port: int
    cert_sha256: str


@contextmanager
def _pipe_pem(pem: bytes) -> Iterator[str]:
    """Expose a small PEM to OpenSSL without creating a named key file."""
    reader, writer = os.pipe()
    try:
        with os.fdopen(writer, "wb", buffering=0) as handle:
            os.set_blocking(handle.fileno(), False)
            if handle.write(pem) != len(pem):
                raise OSError("Could not load the app engine TLS identity")
        yield f"/dev/fd/{reader}"
    finally:
        os.close(reader)


def _local_tls_context() -> tuple[ssl.SSLContext, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Ciaobot")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    private_key_pem = private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with _pipe_pem(certificate_pem) as cert, _pipe_pem(private_key_pem) as key:
        context.load_cert_chain(cert, key)
    return context, certificate.fingerprint(hashes.SHA256()).hex()


def protect_ready_fd(fd: int) -> None:
    """Use only an inherited pipe writer and hide it from other child processes."""
    import fcntl

    if fd < 3 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise ValueError("Expected an inherited readiness pipe writer")
    if (fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE) != os.O_WRONLY:
        raise ValueError("Expected an inherited readiness pipe writer")
    os.set_inheritable(fd, False)
    os.set_blocking(fd, False)


def publish_tls_endpoint(fd: int, startup_id: str, endpoint: LocalTLSEndpoint) -> None:
    """Deliver one atomic, bounded frame to the parent app per engine generation."""
    message = {
        "pid": os.getpid(),
        "startup_id": startup_id,
        "tls_port": endpoint.port,
        "tls_cert_sha256": endpoint.cert_sha256,
    }
    frame = (json.dumps(message, separators=(",", ":")) + "\n").encode("ascii")
    if len(frame) > 512 or os.write(fd, frame) != len(frame):
        raise OSError("Could not publish the app engine TLS identity")


def exec_with_ready_fd(
    fd: int,
    executable: str,
    argv: list[str],
    execv: Callable[[str, list[str]], object] = os.execv,
) -> None:
    """Inherit readiness only for the engine's controlled self-exec."""
    os.set_inheritable(fd, True)
    try:
        execv(executable, argv)
    finally:
        os.set_inheritable(fd, False)


def remembered_tls_port() -> int | None:
    record = read_private_json(state_dir() / READY_RECORD_NAME, label="the app engine readiness record")
    port = record.get("tls_port") if record is not None else None
    return port if isinstance(port, int) and 1024 <= port <= 65535 else None


class LocalTLSPortInUse(OSError):
    """The saved private HTTPS port is already bound."""


def _loopback_http_origin(listeners: list[asyncio.Server]) -> str | None:
    """Find a browser-reachable loopback origin on the HTTP listeners themselves."""
    names = {
        "0.0.0.0": "127.0.0.1",
        "127.0.0.1": "127.0.0.1",
        "::": "[::1]",
        "::1": "[::1]",
    }
    for listener in listeners:
        for bound in listener.sockets:
            address, port, *_ = bound.getsockname()
            if address in names:
                return f"http://{names[address]}:{port}"
    return None


class AppEngineServer(uvicorn.Server):
    """The configured HTTP service and the Mac app's private HTTPS origin."""

    def __init__(
        self,
        config: uvicorn.Config,
        app: Starlette,
        publish: Callable[[LocalTLSEndpoint], None],
        *,
        preferred_port: int | None = None,
    ) -> None:
        super().__init__(config)
        self._app = app
        self._publish = publish
        self._preferred_port = preferred_port

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        context, digest = _local_tls_context()
        policy = b""

        async def local_app(scope: Scope, receive: Receive, send: Send) -> None:
            async def add_policy(message: Message) -> None:
                if message["type"] == "http.response.start":
                    message["headers"] = [*message.get("headers", ()), (b"content-security-policy", policy)]
                await send(message)

            await self._app(scope, receive, add_policy)

        tls = uvicorn.Config(
            local_app,
            ssl_context_factory=lambda _config, _default: context,
            log_config=None,
            proxy_headers=False,
        )
        tls.load()

        def protocol() -> asyncio.Protocol:
            return tls.http_protocol_class(  # type: ignore[call-arg]
                config=tls, server_state=self.server_state, app_state=self.lifespan.state
            )

        await super().startup(sockets=sockets)
        try:
            self._app.state.app_browser_http_origin = _loopback_http_origin(self.servers)
            listener = await asyncio.get_running_loop().create_server(
                protocol, host="127.0.0.1", port=self._preferred_port or 0, ssl=tls.ssl, start_serving=False
            )
            # Uvicorn drains every listener and shared connection before
            # stopping the one ASGI lifespan.
            self.servers.append(listener)
            port = listener.sockets[0].getsockname()[1]
            self._app.state.app_local_tls_port = port
            policy = (
                f"connect-src https://127.0.0.1:{port} wss://127.0.0.1:{port} "
                "https://fonts.googleapis.com https://fonts.gstatic.com"
            ).encode("ascii")
            await listener.start_serving()
            self._publish(LocalTLSEndpoint(port, digest))
        except BaseException as exc:
            await self.shutdown(sockets=sockets)
            if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE and self._preferred_port is not None:
                message = f"Saved Ciaobot HTTPS port {self._preferred_port} is in use."
                if self._preferred_port == self.config.port:
                    message += " Choose a different HTTP service port and try again."
                else:
                    message += " Free that port and try again."
                raise LocalTLSPortInUse(message) from exc
            raise


def write_ready_record(
    *,
    pid: int,
    port: int,
    startup_id: str,
    workspace: Path,
    bootstrap: bool,
    tls_port: int | None = None,
    path: Path | None = None,
) -> Path:
    """Publish the ready record for this process; return the path.

    Written under the workspace lock, first to identify the configured HTTP
    port for diagnostics, then to remember the bound HTTPS port. The app
    receives the bound port and TLS pin on its private readiness pipe.
    """
    target = state_dir() / READY_RECORD_NAME if path is None else Path(path)
    payload = {
        "schema": SCHEMA,
        "pid": int(pid),
        "port": int(port),
        "startup_id": str(startup_id),
        "workspace": str(Path(workspace).resolve()),
        "bootstrap": bool(bootstrap),
    }
    if tls_port is not None:
        payload['tls_port'] = tls_port
    write_private_json(target, payload)
    return target


def watch_parent(
    on_parent_exit: Callable[[], None],
    *,
    on_registered: Callable[[], None] | None = None,
    ppid: int | None = None,
) -> threading.Thread:
    """Start a daemon thread that calls ``on_parent_exit`` when ``ppid`` exits.

    ``ppid`` defaults to this process's parent (the app, when ``--app-engine``
    was launched by it). A kqueue ``EVFILT_PROC``/``NOTE_EXIT`` filter on the
    watched pid fires when it exits, reaped or not. When the parent is already
    gone (pid 1, or ``ESRCH`` at registration time) the callback runs at once:
    the engine's owner cannot be waited for, so it must shut down immediately.

    ``on_registered``, when given, runs from the watchdog thread as soon as
    the kqueue filter is actually registered — the moment the watch can
    observe the watched process's death. A test (or a launcher) that must not
    remove the watched process until missing its death is impossible waits
    for this; on the already-gone paths there is no watch waiting to be set
    up, so it never runs.
    """
    watched = int(ppid) if ppid is not None else os.getppid()
    thread = threading.Thread(
        target=_watch_parent,
        args=(on_parent_exit, watched, on_registered),
        daemon=True,
        name="ciao-app-engine-parent-watch",
    )
    thread.start()
    return thread


def _watch_parent(
    on_parent_exit: Callable[[], None],
    watched: int,
    on_registered: Callable[[], None] | None = None,
) -> None:
    if watched == 1:
        # Orphaned at startup: the owner's slot was already inherited by init.
        on_parent_exit()
        return
    kqueue_cls, kevent_cls, filter_proc, note_exit, ev_add, ev_enable = _kqueue_parts()
    kq = kqueue_cls()
    try:
        registration = kevent_cls(
            watched, filter_proc, ev_add | ev_enable, fflags=note_exit
        )
        try:
            kq.control([registration], 0)
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                on_parent_exit()
                return
            raise
        # The watch is in place: from here the watched pid's death cannot be
        # missed, so the caller may remove it.
        if on_registered is not None:
            on_registered()
        # ``kqueue.control`` takes positional arguments only (raising
        # ``TypeError`` on ``timeout=None``), and a ``None`` timeout blocks
        # until the registration's event arrives: the watched pid's exit.
        events: list[Any] = kq.control([], 1, None)
        if events and events[0].filter == filter_proc:
            on_parent_exit()
    finally:
        kq.close()


def _kqueue_parts() -> tuple[Any, Any, Any, Any, Any, Any]:
    """The kqueue identifiers this filter needs, resolved at runtime.

    They are macOS-only names, looked up through ``getattr`` so the module
    keeps importing (and typing) on the platforms that must still sweep it;
    :func:`_watch_parent` raises a loud error when any of them is missing,
    because an app-owned engine without its parent watchdog is a second
    lifecycle owner with nothing holding it to its app.
    """
    kqueue_cls = getattr(select, "kqueue", None)
    kevent_cls = getattr(select, "kevent", None)
    filter_proc = getattr(select, "KQ_FILTER_PROC", None)
    note_exit = getattr(select, "KQ_NOTE_EXIT", None)
    ev_add = getattr(select, "KQ_EV_ADD", None)
    ev_enable = getattr(select, "KQ_EV_ENABLE", None)
    if None in (kqueue_cls, kevent_cls, filter_proc, note_exit, ev_add, ev_enable):
        raise RuntimeError("watching the parent process needs select.kqueue (macOS)")
    return (kqueue_cls, kevent_cls, filter_proc, note_exit, ev_add, ev_enable)


def _ensure_state_dir(target: Path) -> None:
    """Create or verify the directory that holds an engine record (0700).

    ``target`` is the record's parent. A symlinked directory is refused where
    it stands (before anything is created or tightened): the record must be
    written where :func:`state_dir` or the caller's ``path=`` named it, never
    through a swapped link. The app-owned chain under :func:`state_dir` is
    created outright and kept private (0700) as a whole. A caller-supplied
    directory is only completed where it is missing, and only the directories
    this module itself creates are tightened — a custom ``path=`` never chmods
    a directory it did not create.
    """
    from ciao.os_support.private import make_private_dir

    if os.path.islink(target):
        raise OSError(
            errno.EPERM,
            f"the engine record directory must not be a symlink: {target}",
        )
    if target != state_dir():
        # The caller owns this directory: create what is missing and tighten
        # only what we created.
        chain: list[Path] = []
        directory = target
        while not directory.exists() and directory != directory.parent:
            chain.append(directory)
            directory = directory.parent
        target.mkdir(parents=True, exist_ok=True)
        for fresh in chain:
            make_private_dir(fresh)
        return
    target.parent.parent.mkdir(parents=True, exist_ok=True)  # .../Application Support
    for directory in (target.parent, target):  # .../Ciaobot, .../Ciaobot/engine
        directory.mkdir(exist_ok=True)
        make_private_dir(directory)


def write_private_json(target: Path, payload: dict[str, Any]) -> None:
    """Create-or-replace ``target`` atomically (0600 in the record directory).

    A private temp file is written, flushed and renamed in one step, so a
    reader never observes a partial record and a failed write leaves no temp
    file behind. The bytes only go onto a descriptor that passes the strict
    private check (:func:`ciao.os_support.private.is_private_descriptor_strict`)
    — this user's own ``0o600`` inode, carrying no macOS ACL that would let
    another account in — because these records name the workspace the engine
    will adopt.
    """
    from ciao.os_support.private import (
        is_private_descriptor_strict,
        make_private,
        mkstemp_private,
    )

    _ensure_state_dir(target.parent)
    fd, tmp = mkstemp_private(dir=target.parent, prefix=f".{target.name}.")
    try:
        # fdopen owns the descriptor: on every path out of this block —
        # including the strict refusal — it is closed exactly once.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            if not is_private_descriptor_strict(handle.fileno()):
                raise OSError(
                    errno.EPERM,
                    f"the engine record was not created with private "
                    f"ownership: {tmp}",
                )
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    make_private(target)
