"""The Swift chat authority daemon, driven through the Python client (#3623, slice 2).

Builds `mac/`'s `ciao-authority` product, provisions a temporary root, seeds it with
operator-signed commands (made here with the Python reference signer and the shared
test keys), starts the daemon on a temporary Unix socket and reads it back through
`ciao.chat_authority_client`. Skipped without a usable Swift toolchain. On the
maintainer's Mac that needs `DEVELOPER_DIR=/Applications/Xcode-beta.app/Contents/Developer`
(see mac/AGENTS.md). Nothing here is installed, registered or written outside temp dirs.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ciao.chat_authority import (
    Command,
    EcdsaP256Signer,
    Ed25519Verifier,
    TurnEvent,
    sign_command,
)
from ciao.chat_authority_client import (
    ChatAuthorityClient,
    ChatAuthorityUnavailable,
    TransportError,
    TransportTimeout,
    TransportUnprovisioned,
    helper_status,
)
from tests.chat_authority_vectors import SCOPE, _command_json, _grant_params, keys

MAC_DIR = Path(__file__).resolve().parent.parent / "mac"


def _swift_usable() -> str | None:
    """None when the toolchain can load the package manifest, else why not."""
    if sys.platform != "darwin":
        return "the Swift daemon is macOS only"
    swift = shutil.which("swift")
    if swift is None:
        return "swift toolchain not installed"
    probe = subprocess.run(
        [swift, "package", "--package-path", str(MAC_DIR), "describe", "--type", "json"],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if probe.returncode != 0:
        return "swift cannot load mac/Package.swift (set DEVELOPER_DIR to a full Xcode)"
    return None


@pytest.fixture(scope="module")
def daemon_bin() -> str:
    reason = _swift_usable()
    if reason:
        pytest.skip(reason)
    build = subprocess.run(
        ["swift", "build", "--package-path", str(MAC_DIR), "--product", "ciao-authority"],
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )
    assert build.returncode == 0, build.stdout[-3000:] + build.stderr[-3000:]
    bin_dir = subprocess.run(
        ["swift", "build", "--package-path", str(MAC_DIR), "--show-bin-path"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return str(Path(bin_dir) / "ciao-authority")


class UnixSocketTransport:
    """One request and one reply per connection, 4-byte big-endian length frames."""

    def __init__(self, path: str) -> None:
        self.path = path

    def call(self, request: bytes, *, timeout: float) -> bytes:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(self.path)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise TransportUnprovisioned from exc
            sock.sendall(struct.pack(">I", len(request)) + request)
            header = self._read(sock, 4)
            return self._read(sock, struct.unpack(">I", header)[0])
        except TimeoutError as exc:
            raise TransportTimeout from exc
        except OSError as exc:
            raise TransportError from exc
        finally:
            sock.close()

    @staticmethod
    def _read(sock: socket.socket, count: int) -> bytes:
        out = b""
        while len(out) < count:
            chunk = sock.recv(count - len(out))
            if not chunk:
                raise TransportError("closed")
            out += chunk
        return out


class Daemon:
    def __init__(self, binary: str, root: str) -> None:
        self.binary = binary
        self.root = root
        self.socket = tempfile.mkdtemp(prefix="cs-") + "/s"
        self.proc: subprocess.Popen[str] | None = None
        self.signer = EcdsaP256Signer.from_private_scalar(int(keys()["operator_private_scalar_hex"], 16))

    def run(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args, "--root", self.root],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def command(self, kind: str, counter: int, params: dict[str, Any], payload: bytes = b"") -> dict[str, Any]:
        signed = sign_command(self.signer, Command(kind, counter, "op", params, payload))
        out = self.run("apply", stdin=json.dumps(_command_json(signed)))
        assert out.returncode == 0, out.stderr
        result: dict[str, Any] = json.loads(out.stdout)
        return result

    def serve(self) -> None:
        self.proc = subprocess.Popen(
            [self.binary, "serve", "--root", self.root, "--socket", self.socket],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        assert line.startswith("serving"), line

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
        if self.proc:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()
        shutil.rmtree(os.path.dirname(self.socket), ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.root), ignore_errors=True)

    def client(self) -> ChatAuthorityClient:
        return ChatAuthorityClient(UnixSocketTransport(self.socket))


@pytest.fixture
def daemon(daemon_bin: str) -> Iterator[Daemon]:
    parent = tempfile.mkdtemp(prefix="ca-")
    d = Daemon(daemon_bin, os.path.join(parent, "authority"))
    assert d.run("provision").returncode == 0
    enrolled = d.run(
        "enroll",
        "--operator-public-key-hex",
        keys()["operator_public_x963_hex"],
        "--reader-uid",
        str(os.getuid()),
    )
    assert enrolled.returncode == 0, enrolled.stderr
    try:
        yield d
    finally:
        d.stop()


def _lookup(client: ChatAuthorityClient, grant_id: str, **extra: Any) -> Any:
    return client.eligible_events(
        grant_id=grant_id,
        producer="memory_extraction",
        limit=10,
        destination="brain",
        audience="team",
        **extra,
    )


def test_client_reads_events_the_daemon_signed(daemon: Daemon) -> None:
    grant = daemon.command("create_grant", 1, _grant_params())["grant_id"]
    sub = daemon.command(
        "submit_turn", 2, {"send_id": "s1", **SCOPE}, "caf\u00e9 \U0001f600".encode()
    )
    assert sub["grant_ids"] == [grant] and sub["seq"] == 1
    daemon.serve()
    client = daemon.client()

    health = client.health()
    assert health["ready"] is True and health["state_counter"] == 2 and health["epoch"] == 0
    assert health["recovered_crash_window"] is False

    look = _lookup(client, grant)
    assert look.epoch == 0 and (look.destination, look.audience) == ("brain", "team")
    (event,) = look.events
    assert event.text == "caf\u00e9 \U0001f600".encode() and event.send_id == "s1"
    assert event.event_id == sub["event_id"]
    assert event.signature.hex() == sub["event_signature_hex"]
    # The Swift signature verifies with the Python verifier over the Python signing bytes.
    verifier = Ed25519Verifier(bytes.fromhex(health["event_public_key"]))
    assert isinstance(event, TurnEvent) and verifier.verify(event.signing_bytes(), event.signature)
    assert len(bytes.fromhex(health["event_public_key"])) == 32


def test_filters_run_in_the_daemon(daemon: Daemon) -> None:
    grant = daemon.command("create_grant", 1, _grant_params())["grant_id"]
    daemon.command("submit_turn", 2, {"send_id": "s1", **SCOPE}, b"one")
    daemon.command("submit_turn", 3, {"send_id": "s2", **SCOPE, "opted_out": True}, b"two")
    daemon.command("submit_turn", 4, {"send_id": "s3", **SCOPE}, b"three")
    daemon.serve()
    client = daemon.client()
    assert [e.send_id for e in _lookup(client, grant).events] == ["s3", "s1"]
    assert _lookup(client, grant, chat="other").events == ()
    wrong = client.eligible_events(
        grant_id=grant, producer="memory_extraction", limit=10, destination="elsewhere", audience="team"
    )
    assert wrong.events == ()
    with pytest.raises(ChatAuthorityUnavailable) as err:
        _lookup(client, grant, expected_epoch=7)
    assert err.value.code == "epoch_changed"


def test_revoked_grant_yields_nothing_and_counter_survives_restart(daemon: Daemon) -> None:
    grant = daemon.command("create_grant", 1, _grant_params())["grant_id"]
    daemon.command("submit_turn", 2, {"send_id": "s1", **SCOPE}, b"one")
    assert daemon.command("revoke_grant", 3, {"grant_id": grant}) == {"epoch": 1}
    replay = daemon.command("revoke_grant", 3, {"grant_id": grant})
    assert replay == {"error": "replayed"}  # also after a restart of the process
    daemon.serve()
    client = daemon.client()
    assert client.health()["epoch"] == 1
    assert _lookup(client, grant).events == ()


def test_nonce_is_echoed_and_unknown_ops_are_refused(daemon: Daemon) -> None:
    daemon.serve()
    transport = UnixSocketTransport(daemon.socket)
    nonce = "0123456789abcdef" * 2
    raw = json.loads(transport.call(json.dumps({"v": 1, "op": "health", "nonce": nonce}).encode(), timeout=2))
    assert raw["nonce"] == nonce and raw["v"] == 1 and raw["ok"] is True
    for op in ("create_grant", "submit_turn", "enroll_operator_key"):
        reply = json.loads(transport.call(json.dumps({"v": 1, "op": op, "nonce": nonce}).encode(), timeout=2))
        assert reply == {"v": 1, "nonce": nonce, "ok": False, "code": "unknown_op"}
    with pytest.raises(TransportError):  # no nonce: no answer
        transport.call(json.dumps({"v": 1, "op": "health"}).encode(), timeout=2)


def test_helper_status_reflects_the_daemon(daemon: Daemon) -> None:
    ghost = daemon.client()  # nothing listening yet
    assert helper_status(ghost, provisioned=lambda: True) == "unprovisioned"
    daemon.serve()
    client = daemon.client()
    assert helper_status(client, provisioned=lambda: True) == "available"
    assert helper_status(client, provisioned=lambda: False) == "degraded"


def test_tampered_state_leaves_the_daemon_closed(daemon: Daemon) -> None:
    daemon.command("create_grant", 1, _grant_params())
    blob = Path(daemon.root, "state", "state.blob")
    data = bytearray(blob.read_bytes())
    data[len(data) // 2] ^= 1
    blob.write_bytes(bytes(data))
    daemon.serve()
    client = daemon.client()
    with pytest.raises(ChatAuthorityUnavailable) as err:
        client.health()
    assert err.value.code == "not_ready"
    assert helper_status(client, provisioned=lambda: True) == "degraded"
    with pytest.raises(ChatAuthorityUnavailable) as closed:
        _lookup(client, "g")
    assert closed.value.code in {"not_ready", "closed"}


def test_rolled_back_blob_leaves_the_daemon_closed(daemon: Daemon) -> None:
    daemon.command("create_grant", 1, _grant_params())
    blob = Path(daemon.root, "state", "state.blob")
    old = blob.read_bytes()
    daemon.command("create_grant", 2, _grant_params())
    daemon.command("create_grant", 3, _grant_params())
    blob.write_bytes(old)
    daemon.serve()
    with pytest.raises(ChatAuthorityUnavailable):
        daemon.client().health()
    # The offline harness refuses to open it too, and repairs nothing.
    assert daemon.run("apply", stdin="{}").returncode != 0
    assert blob.read_bytes() == old
