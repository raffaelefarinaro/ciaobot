"""The port the engine binds is taken by something else (#1654).

An engine that cannot bind its port has to say so once and stop. Before this,
the bind failure surfaced as an unhandled ``OSError`` inside uvicorn: the
process died, the service manager respawned it, and the operator was told the
engine was "still starting" while a dead sign-in link was printed at them.

Everything that has to name the port — the engine's own log and startup
status, ``ciao service status``, ``ciao service start`` and the one-line
installer — reads its wording from here, so the port, the reason and the two
ways out are the same sentence everywhere.

The reason is also *recorded*, outside the workspace, because an engine that
cannot bind cannot serve ``/api/startup-status`` either: the reader is the
one who has to be told. The record is written where
:mod:`ciao.workspace_access` writes its denial, and read the same way.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
from pathlib import Path

# The exit code the engine leaves with when it could not bind its port. It is
# not RESTART_EXIT_CODE (75), not the workspace-access code (78), not the
# setup-memory code (3) and not a signal death, so a service manager reads it
# as "this run failed" rather than "relaunch me" — the point of the whole
# path is to stop the respawn loop. EX_UNAVAILABLE, in the same sysexits
# family as the workspace-access code it sits next to.
PORT_IN_USE_EXIT_CODE = 69

# The startup phase the bind failure is recorded under.
BIND_PHASE = "server_starting"

# The record of the last bind failure, outside every workspace, the way
# ``ciao.workspace_access`` keeps its denial: the engine cannot write into
# the workspace it failed to serve, and a reader outside it has to be able
# to find the reason.
PORT_IN_USE_FILENAME = "port-in-use.txt"


def port_in_use_path() -> Path:
    """Where the bind-failure record is kept."""
    from ciao.install_receipt import default_receipt_path

    return default_receipt_path().parent / PORT_IN_USE_FILENAME


def is_address_in_use(exc: OSError) -> bool:
    """Whether this OSError is a bind failure caused by a taken address."""
    return exc.errno == errno.EADDRINUSE


def port_in_use_message(port: int, workspace: Path | None = None) -> str:
    """The operator-facing sentence for a taken port.

    Names the port, says what holds it, and gives the two ways out: free the
    port, or choose another one through ``PWA_PORT`` in the workspace ``.env``.
    """
    env = (Path(workspace) / ".env") if workspace is not None else Path(".env")
    return (
        f"port {port} is already in use by another program. "
        f"Free port {port}, or set PWA_PORT in {env} to serve on a different port."
    )


def check_server_port(host: str, port: int) -> None:
    """Bind the engine's port once, to learn whether it is free.

    uvicorn binds the port itself, inside ``Server.startup``, and swallows
    the ``OSError``: the engine only ever saw a ``SystemExit`` with uvicorn's
    own startup-failure code, so the reason never reached the startup
    status, the log, or the operator — and a service manager respawned the
    job forever. This attempts the same bind at a point the engine owns, so
    a taken port is a startup phase with a reason and an exit code of its
    own (#1654).

    The addresses and the socket options are the ones asyncio's own
    ``create_server`` would use — ``SO_REUSEADDR`` on POSIX, none on
    Windows — so this bind fails exactly when uvicorn's would, and the
    ``OSError`` it raises is the one the engine would have seen.
    """
    infos = socket.getaddrinfo(
        host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )
    reuse_address = os.name == "posix" and sys.platform != "cygwin"
    sockets: list[socket.socket] = []
    try:
        for family, socktype, proto, _canonname, address in infos:
            sock = socket.socket(family, socktype, proto)
            if reuse_address:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(address)
            except OSError:
                sock.close()
                raise
            sockets.append(sock)
    finally:
        # The socket is only a probe: uvicorn binds the port itself, so it
        # is released again and the engine's own bind is the real one.
        for sock in sockets:
            sock.close()


def port_is_available(port: int, host: str = "") -> bool:
    """Whether the engine could bind ``port`` right now.

    The probe is the engine's own bind — the same addresses and the same
    socket options — so its answer is the engine's answer, not an
    approximation of it. A probe that cannot run at all (an address this
    platform will not give us) answers "available": the engine's own bind is
    the authority, and a caller must not refuse an install on a guess.
    """
    try:
        check_server_port(host, port)
    except OSError:
        return False
    return True


def record_port_in_use(message: str, path: Path | None = None) -> None:
    """Record why the engine could not start. Best effort, never raises.

    Written like the install receipt: a private temp file renamed into
    place, so a symlink planted at the record path is replaced, never
    followed.
    """
    from ciao.os_support.files import replace_file
    from ciao.os_support.private import make_private_dir, mkstemp_private

    target = path or port_in_use_path()
    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(target.parent)
        fd, tmp_name = mkstemp_private(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(message + "\n")
        replace_file(tmp, target)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def clear_port_in_use(path: Path | None = None) -> None:
    """Forget a previous bind failure: this boot got past the bind."""
    try:
        (path or port_in_use_path()).unlink(missing_ok=True)
    except OSError:
        pass


def read_port_in_use(path: Path | None = None) -> str:
    """The recorded reason, or "" when there is none or it cannot be read."""
    try:
        return (path or port_in_use_path()).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
