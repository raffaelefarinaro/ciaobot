"""Verified macOS server-host identity, command and ownership contract (#1023).

The child plan for #1008 (B1). It defines the one fail-closed validator a later
service consumer (B2) renders an invocation from and an installer writes a
record with. There is no consumer wiring, no service activation and no receipt
writer here; the three layers below are the whole module.

Three questions, deliberately kept apart, in the order they must be asked:

* **Syntax** — :func:`parse_service_command` reads an invocation and answers
  only what the argv *says*: a typed :class:`ServiceCommand`, either the direct
  engine shape (``python -m ciao.cli run|supervise`` or ``ciao run|supervise``)
  or one of the two native-host shapes (``Ciaobot Server serve --python <abs>``
  and ``Ciaobot Server run-move --python <abs> --operation <id> --state-dir
  <abs>``). It is
  pure, runs on every platform, and proves nothing about the files it names. A
  hosted command whose executable basename is ``Ciaobot Server`` is accepted
  even when no such host exists, because "the argv names the host" is not "the
  host is ours". :func:`host_service_argv` renders the hosted command; a caller
  must :func:`verify_owned_host` *before* it renders that command.
* **Identity** — :func:`inspect_host_bundle` reads an installed ``Ciaobot
  Server.app`` on macOS and returns a frozen :class:`HostOwnership` snapshot:
  the bundle identity, the plist's fixed fields, the executable digest, the two
  signed slice CDHashes and the digest of every regular sealed file. It proves
  the bytes are a well-formed, strictly and ad-hoc signed universal host. It is
  **not** ownership: a stranger's correctly built bundle inspects the same way.
* **Ownership** — :func:`verify_owned_host` reads an existing, owner-only
  ownership record written outside the bundle and requires the inspected bundle
  to match it exactly: identity, revision, protocol, executable digest, both
  CDHashes and the whole file set. Only that match proves this machine installed
  this host. A missing record is a refusal, never a reason to trust a bundle.
  :func:`verify_superseded_host` is the one narrower proof, against the
  revision this engine supersedes, so the installer can replace a host it can
  prove is ours and refuse every other bundle.

The record never lives inside the sealed app: a record covered by the signature
it vouches for would be circular, and one written into the bundle would change
the very digest the next read compares. It holds no engine version, workspace or
credential — only the host's own immutable identity and bytes. The reader never
runs a native command, coerces a value or writes a file. The inspector runs only
the public, read-only ``/usr/bin/codesign --verify`` and ``/usr/bin/codesign -dv``
probes: no signing, launch, ``open``, ``launchctl`` or AX call, and every
subprocess is bounded and shell-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import posixpath
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

__all__ = [
    "APP_NAME",
    "ARCHITECTURES",
    "BUNDLE_ID",
    "EXECUTABLE_NAME",
    "EXIT_TIMEOUT_SECONDS",
    "HOST_PROTOCOL",
    "HOST_PROTOCOL_KEY",
    "HOST_REVISION",
    "ICON_NAME",
    "LEGACY_HOST_PROTOCOL",
    "LEGACY_HOST_REVISION",
    "MOVE_HOST_PROTOCOL",
    "MINIMUM_SYSTEM_VERSION",
    "NATIVE_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "STOP_GRACE_SECONDS",
    "HostOwnership",
    "ServerHostError",
    "ServiceCommand",
    "default_bundle_path",
    "default_ownership_path",
    "host_service_argv",
    "host_move_argv",
    "host_interpreter",
    "inspect_host_bundle",
    "parse_service_command",
    "read_host_ownership",
    "read_superseded_ownership",
    "verify_owned_host",
    "verify_superseded_host",
]

#: The fixed bundle identity. These must agree with ``scripts/build-server-host.py``
#: and ``native/server-host/ServerHost.swift``; ``tests/test_server_host_contract.py``
#: asserts that they do.
APP_NAME = "Ciaobot Server.app"
BUNDLE_ID = "local.ciaobot.server"
EXECUTABLE_NAME = "Ciaobot Server"
ICON_NAME = "CiaobotServer.icns"
HOST_REVISION = 4
HOST_PROTOCOL_KEY = "CiaobotServerHostProtocol"
HOST_PROTOCOL = 2
MINIMUM_SYSTEM_VERSION = "13.0"

#: The one superseded host revision this engine can still prove it owns, so
#: the installer can replace it instead of refusing the upgrade and leaving
#: the Mac without a host (#1659). A record of any other revision is refused
#: by both readers.
LEGACY_HOST_REVISION = 3
#: The protocol the superseded revision spoke.
LEGACY_HOST_PROTOCOL = 2

#: The first host protocol that carries the ``run-move`` operation, so the
#: workspace move can run as a descendant of the host (#1658). A host below it
#: has no such operation: the move job stays a bare interpreter and the move
#: keeps refusing a protected folder.
MOVE_HOST_PROTOCOL = 2

#: The host's own stop grace and the launchd ``ExitTimeOut`` a service plist must
#: exceed so launchd does not sweep the job group out from under the host's stop.
#: ``EXIT_TIMEOUT_SECONDS`` is deliberately above ``STOP_GRACE_SECONDS``, which is
#: itself deliberately above the Python supervisor's own grace.
STOP_GRACE_SECONDS = 35
EXIT_TIMEOUT_SECONDS = 45

#: The on-disk ownership record schema this module reads.
SCHEMA_VERSION = 1

#: Every native probe is bounded and shell-free. ``codesign`` is local and fast;
#: ten seconds is a hang, not a slow machine.
NATIVE_TIMEOUT_SECONDS = 10

#: The one native tool, by absolute path. ``codesign`` is a real system binary,
#: so neither the service's ``PATH`` nor ``DEVELOPER_DIR`` can substitute it, and
#: it needs no Command Line Tools. (``/usr/bin/lipo`` is an xcode-select shim
#: that does both, so the slices are read from ``codesign``'s ``Format=`` line.)
CODESIGN = "/usr/bin/codesign"


def default_bundle_path() -> Path:
    """Where the host bundle lives by default: ``~/Applications/Ciaobot Server.app``.

    Home-anchored and resolved at call time, like
    :func:`ciao.install_receipt.default_receipt_path`, so the per-test home
    isolation applies. There is no Ciaobot-specific environment override.
    """
    return Path.home() / "Applications" / APP_NAME


def default_ownership_path() -> Path:
    """Where the ownership record lives by default, outside the sealed app."""
    return Path.home() / ".local" / "state" / "ciaobot" / "server-host.json"


ARCHITECTURES = ("arm64", "x86_64")

# Stable refusal codes. They are part of the contract a later consumer and its
# tests match on, so they are module constants rather than free strings.
INVALID_COMMAND = "invalid_command"
INVALID_OWNERSHIP = "invalid_ownership"
UNSUPPORTED_SCHEMA = "unsupported_schema"
MISSING_RECORD = "missing_record"
UNSUPPORTED_PLATFORM = "unsupported_platform"
INSPECTION_FAILED = "inspection_failed"
NOT_OWNED = "not_owned"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CDHASH_RE = re.compile(r"^[0-9a-f]{40}$")
_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")

_PLIST_REL = "Contents/Info.plist"
_EXECUTABLE_REL = f"Contents/MacOS/{EXECUTABLE_NAME}"
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"

#: Exactly the files ``scripts/build-server-host.py`` seals into a host bundle.
#: An inspected bundle and a record's mapping must both name this set and
#: nothing else: any other file is a thin staging product, a stray config,
#: bundled Python or tampering. A new resource is a new host revision.
REQUIRED_BUNDLE_FILES = frozenset(
    {_PLIST_REL, _EXECUTABLE_REL, _ICON_REL, _CODESIGN_REL}
)

#: The directories a host bundle contains: the bundle root, ``Contents`` and the
#: parents of the sealed files. Any other directory is refused.
_ALLOWED_DIRECTORIES = frozenset(
    {
        ".",
        "Contents",
        "Contents/MacOS",
        "Contents/Resources",
        "Contents/_CodeSignature",
    }
)

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "bundle_path",
        "bundle_id",
        "host_revision",
        "host_protocol",
        "executable_sha256",
        "per_arch_cdhashes",
        "bundle_files",
    }
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class ServerHostError(ValueError):
    """A refusal from the server-host contract, carrying a stable ``code``."""

    def __init__(self, message: str, *, code: str = INVALID_OWNERSHIP) -> None:
        super().__init__(message)
        self.code = code


# ── Syntax: the invocation a service describes ─────────────────────────────


@dataclass(frozen=True)
class ServiceCommand:
    """What an invocation says, and nothing about whether it is trustworthy.

    ``program`` is the executable at argv[0] and ``python`` is the interpreter it
    would run (empty for the direct console ``ciao``). A hosted command naming a
    real-looking host executable is still only a syntax match: ownership is a
    separate question answered by :func:`verify_owned_host`.
    """

    mode: Literal["direct", "hosted"]
    program: str
    python: str
    argv: tuple[str, ...]


def _getuid() -> int:
    """This user's uid.

    ``os.getuid`` does not exist on Windows, where this module is imported (the
    contract is shared) but no macOS path runs. Read through ``getattr`` so
    mypy's win32 pass type-checks it too, and typed locally because the
    ``getattr`` is ``Any`` (``no-any-return``). The same seam
    :func:`ciao.macos_service._getuid` uses, and the seam a test patches.
    """
    getuid: Callable[[], int] = getattr(os, "getuid")
    return getuid()


def _absolute_posix(value: object, *, what: str, code: str) -> str:
    """Require an absolute POSIX path string, portable across platforms.

    Parsing is done with :class:`PurePosixPath` so the macOS fixtures parse the
    same way on Windows. A backslash is refused as well as a relative path: on
    Windows it is a separator and on macOS it is an ordinary (and suspicious)
    filename byte, so a value containing one does not name the same file on both
    and must not be accepted as a portable service argument.
    """
    if not isinstance(value, str) or value == "" or "\x00" in value:
        raise ServerHostError(
            f"{what} must be a non-empty absolute path string", code=code
        )
    if "\\" in value:
        raise ServerHostError(
            f"{what} must not contain a backslash: {value!r}", code=code
        )
    if value.startswith("//"):
        # POSIX treats a leading ``//`` as implementation-defined and
        # ``posixpath.normpath`` keeps it, so it must be refused explicitly.
        raise ServerHostError(
            f"{what} must not start with a doubled slash: {value!r}", code=code
        )
    if not PurePosixPath(value).is_absolute():
        raise ServerHostError(f"{what} must be absolute: {value!r}", code=code)
    if posixpath.normpath(value) != value:
        raise ServerHostError(
            f"{what} must be a normalized path (no '.', '..', '//' or trailing '/'): "
            f"{value!r}",
            code=code,
        )
    return value


def _python_interpreter(value: object) -> str:
    """An absolute interpreter path whose basename is python, python3 or python3.N."""
    interpreter = _absolute_posix(value, what="the interpreter", code=INVALID_COMMAND)
    if not _PYTHON_RE.fullmatch(PurePosixPath(interpreter).name):
        raise ServerHostError(
            f"the interpreter must be a python executable: {interpreter!r}",
            code=INVALID_COMMAND,
        )
    return interpreter


def host_interpreter(python: PurePath) -> str:
    """The interpreter a host would run, or a refusal.

    The host's own parser demands an absolute, normalized path whose basename
    is ``python``, ``python3`` or ``python3.N``. This exposes that rule to a
    caller that must answer "can this host run a child with this interpreter?"
    *before* it renders a command, so the answer and the rendering can never
    disagree.
    """
    return _python_interpreter(os.fspath(python))


def _parse_direct(program: str, argv: tuple[str, ...]) -> ServiceCommand:
    base = PurePosixPath(program).name
    if _PYTHON_RE.fullmatch(base):
        rest = list(argv[1:])
        if rest and rest[0] == "-I":
            rest = rest[1:]
        if (
            len(rest) != 3
            or rest[0] != "-m"
            or rest[1] != "ciao.cli"
            or rest[2]
            not in (
                "run",
                "supervise",
            )
        ):
            raise ServerHostError(
                "a direct Python service must be [python, [-I], -m, ciao.cli, run|supervise]",
                code=INVALID_COMMAND,
            )
        return ServiceCommand("direct", program, program, argv)
    if base == "ciao":
        if len(argv) != 2 or argv[1] not in ("run", "supervise"):
            raise ServerHostError(
                "a direct console service must be [ciao, run|supervise]",
                code=INVALID_COMMAND,
            )
        return ServiceCommand("direct", program, "", argv)
    raise ServerHostError(
        f"a direct service must be a python interpreter or the ciao console, not {base!r}",
        code=INVALID_COMMAND,
    )


def parse_service_command(arguments: object) -> ServiceCommand:
    """Parse a service invocation strictly into a :class:`ServiceCommand`.

    Accepts exactly the shapes the host actually runs:

    * hosted — ``[<abs>/Ciaobot Server, serve, --python, <abs python>]``;
    * hosted move — ``[<abs>/Ciaobot Server, run-move, --python, <abs python>,
      --operation, <id>, --state-dir, <abs dir>]``;
    * direct — ``[<abs python>, [-I], -m, ciao.cli, run|supervise]`` or
      ``[<abs ciao>, run|supervise]``.

    Repeated, unknown, extra or relative arguments, a non-string element, a
    non-sequence and a wrong host basename are all refusals. The hosted shapes
    are syntax only and never prove ownership.
    """
    if not isinstance(arguments, (list, tuple)) or isinstance(arguments, (str, bytes)):
        raise ServerHostError(
            "a service command is a list of string arguments", code=INVALID_COMMAND
        )
    argv: tuple[str, ...] = tuple(arguments)
    if not argv:
        raise ServerHostError("a service command is not empty", code=INVALID_COMMAND)
    if not all(isinstance(argument, str) for argument in argv):
        raise ServerHostError(
            "every service argument must be a string", code=INVALID_COMMAND
        )
    if any("\x00" in argument for argument in argv):
        raise ServerHostError(
            "a service argument must not contain a NUL", code=INVALID_COMMAND
        )

    if len(argv) == 4 and argv[1] == "serve" and argv[2] == "--python":
        host = _absolute_posix(
            argv[0], what="the host executable", code=INVALID_COMMAND
        )
        if PurePosixPath(host).name != EXECUTABLE_NAME:
            raise ServerHostError(
                f"the hosted executable must be {EXECUTABLE_NAME!r}, not "
                f"{PurePosixPath(host).name!r}",
                code=INVALID_COMMAND,
            )
        python = _python_interpreter(argv[3])
        return ServiceCommand("hosted", host, python, argv)

    if (
        len(argv) == 8
        and argv[1] == "run-move"
        and argv[2] == "--python"
        and argv[4] == "--operation"
        and argv[6] == "--state-dir"
    ):
        # The workspace move runs under the host (#1658), so its command is a
        # shape the parser knows. The same rules as the serve shape apply to
        # the interpreter; the state directory is an absolute, normalized path.
        host = _absolute_posix(
            argv[0], what="the host executable", code=INVALID_COMMAND
        )
        if PurePosixPath(host).name != EXECUTABLE_NAME:
            raise ServerHostError(
                f"the hosted executable must be {EXECUTABLE_NAME!r}, not "
                f"{PurePosixPath(host).name!r}",
                code=INVALID_COMMAND,
            )
        python = _python_interpreter(argv[3])
        _absolute_posix(argv[7], what="the state directory", code=INVALID_COMMAND)
        return ServiceCommand("hosted", host, python, argv)

    program = _absolute_posix(argv[0], what="the program", code=INVALID_COMMAND)
    return _parse_direct(program, argv)


def host_service_argv(bundle_path: PurePath, python: PurePath) -> tuple[str, ...]:
    """Render the exact hosted command for an installed host bundle.

    Strictly syntax and rendering: both arguments must be absolute POSIX paths
    and the bundle must be named ``Ciaobot Server.app``. This runs no
    subprocess. A caller must confirm the bundle with :func:`verify_owned_host`
    *before* rendering or running this command, because this function can only
    see the names, not the bytes behind them.
    """
    bundle = _absolute_posix(
        os.fspath(bundle_path), what="the bundle path", code=INVALID_COMMAND
    )
    if PurePosixPath(bundle).name != APP_NAME:
        raise ServerHostError(
            f"the bundle must be named {APP_NAME!r}: {bundle!r}", code=INVALID_COMMAND
        )
    interpreter = _python_interpreter(os.fspath(python))
    host = PurePosixPath(bundle) / "Contents" / "MacOS" / EXECUTABLE_NAME
    return (os.fspath(host), "serve", "--python", interpreter)


#: The operation id the engine mints for a move: a timestamp plus six hex
#: characters. The host accepts any plain identifier, so this is the shape
#: :func:`host_move_argv` is given.
_MOVE_OPERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def host_move_argv(
    bundle_path: PurePath,
    python: PurePath,
    operation: str,
    state_dir: PurePath,
) -> tuple[str, ...]:
    """Render the exact hosted command for the workspace move.

    The ``run-move`` shape :func:`parse_service_command` accepts:
    ``[<host>, "run-move", "--python", <abs python>, "--operation", <id>,
    "--state-dir", <abs dir>]``. The host spawns a fixed child
    (``[python, -I, -m, ciao.workspace_move, run, --operation, <id>,
    --state-dir, <dir>]``), so a macOS folder grant the user gave the host
    covers the move too (#1658).

    Strictly syntax and rendering, like :func:`host_service_argv`: this runs no
    subprocess and proves nothing about the bytes behind the names. A caller
    must confirm the bundle with :func:`verify_owned_host` *before* it renders
    or runs this command, and the operation id must be a plain identifier so
    the host's own parser cannot read it as an option.
    """
    bundle = _absolute_posix(
        os.fspath(bundle_path), what="the bundle path", code=INVALID_COMMAND
    )
    if PurePosixPath(bundle).name != APP_NAME:
        raise ServerHostError(
            f"the bundle must be named {APP_NAME!r}: {bundle!r}", code=INVALID_COMMAND
        )
    interpreter = _python_interpreter(os.fspath(python))
    if not isinstance(operation, str) or not _MOVE_OPERATION_RE.fullmatch(operation):
        raise ServerHostError(
            f"the move operation must be a plain identifier: {operation!r}",
            code=INVALID_COMMAND,
        )
    state = _absolute_posix(
        os.fspath(state_dir), what="the state directory", code=INVALID_COMMAND
    )
    host = PurePosixPath(bundle) / "Contents" / "MacOS" / EXECUTABLE_NAME
    return (
        os.fspath(host),
        "run-move",
        "--python",
        interpreter,
        "--operation",
        operation,
        "--state-dir",
        state,
    )


# ── The ownership snapshot ──────────────────────────────────────────────────


@dataclass(frozen=True)
class HostOwnership:
    """The immutable identity and bytes of one host bundle.

    Returned by :func:`inspect_host_bundle` (an unproven snapshot) and by
    :func:`verify_owned_host` (proven against an existing private record). The
    same shape serves both so a consumer can render from a verified value
    without a second type; only the second call answers "is it ours". The two
    mappings are read-only copies, so a returned snapshot cannot be edited
    after it was proven.
    """

    schema: int
    bundle_path: str
    bundle_id: str
    host_revision: int
    host_protocol: int
    executable_sha256: str
    per_arch_cdhashes: Mapping[str, str]
    bundle_files: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in ("per_arch_cdhashes", "bundle_files"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def to_record(self) -> dict[str, Any]:
        """The record document this snapshot serializes to.

        Pure: it returns a dict and writes nothing. Persisting it owner-only,
        outside the bundle and only after a verified installation, is the
        installer's later job (#1008 child E), not this module's.
        """
        return {
            "schema": self.schema,
            "bundle_path": self.bundle_path,
            "bundle_id": self.bundle_id,
            "host_revision": self.host_revision,
            "host_protocol": self.host_protocol,
            "executable_sha256": self.executable_sha256,
            "per_arch_cdhashes": dict(self.per_arch_cdhashes),
            "bundle_files": dict(self.bundle_files),
        }


def _require_sha256(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ServerHostError(
            f"{what} must be a lowercase SHA-256 hex digest", code=INVALID_OWNERSHIP
        )
    return value


def _require_cdhash(value: object, *, what: str, code: str = INVALID_OWNERSHIP) -> str:
    if not isinstance(value, str) or not _CDHASH_RE.fullmatch(value):
        raise ServerHostError(f"{what} must be a lowercase 40-hex CDHash", code=code)
    return value


def _strict_int(value: object) -> bool:
    """An integer that is not a bool: a JSON ``true`` must not pass as version 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that refuses a repeated key anywhere in the JSON."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ServerHostError(
                f"the ownership record repeats the key {key!r}", code=INVALID_OWNERSHIP
            )
        result[key] = value
    return result


def _decode_record(
    raw: str,
    *,
    path: Path,
    revision: int = HOST_REVISION,
    protocol: int = HOST_PROTOCOL,
) -> HostOwnership:
    try:
        document: Any = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except ServerHostError:
        raise
    except (ValueError, RecursionError):
        raise ServerHostError(
            f"the ownership record at {path.name} is not valid JSON",
            code=INVALID_OWNERSHIP,
        ) from None
    if not isinstance(document, dict):
        raise ServerHostError(
            f"the ownership record at {path.name} is not a JSON object",
            code=INVALID_OWNERSHIP,
        )
    if set(document) != _RECORD_FIELDS:
        missing = sorted(_RECORD_FIELDS - set(document))
        extra = sorted(set(document) - _RECORD_FIELDS)
        raise ServerHostError(
            f"the ownership record at {path.name} has the wrong fields "
            f"(missing={missing}, extra={extra})",
            code=INVALID_OWNERSHIP,
        )

    # A JSON `true` must not pass as schema 1: `_strict_int` excludes `bool`.
    if not _strict_int(document["schema"]):
        raise ServerHostError(
            f"the ownership record at {path.name} has no integer schema version",
            code=UNSUPPORTED_SCHEMA,
        )
    if document["schema"] != SCHEMA_VERSION:
        raise ServerHostError(
            f"the ownership record at {path.name} is schema {document['schema']}; "
            f"this engine reads schema {SCHEMA_VERSION}",
            code=UNSUPPORTED_SCHEMA,
        )
    if document["bundle_id"] != BUNDLE_ID:
        raise ServerHostError(
            f"the ownership record at {path.name} names bundle "
            f"{document['bundle_id']!r}, not {BUNDLE_ID!r}",
            code=INVALID_OWNERSHIP,
        )
    if (
        not _strict_int(document["host_revision"])
        or document["host_revision"] != revision
    ):
        raise ServerHostError(
            f"the ownership record at {path.name} is not host revision {revision}",
            code=INVALID_OWNERSHIP,
        )
    if not _strict_int(document["host_protocol"]) or document["host_protocol"] != protocol:
        raise ServerHostError(
            f"the ownership record at {path.name} is not host protocol {protocol}",
            code=INVALID_OWNERSHIP,
        )

    bundle_path = _absolute_posix(
        document["bundle_path"], what="the recorded bundle path", code=INVALID_OWNERSHIP
    )
    if PurePosixPath(bundle_path).name != APP_NAME:
        raise ServerHostError(
            f"the ownership record at {path.name} names a bundle not called "
            f"{APP_NAME!r}: {bundle_path!r}",
            code=INVALID_OWNERSHIP,
        )
    executable_sha256 = _require_sha256(
        document["executable_sha256"], what="executable_sha256"
    )

    cdhashes = document["per_arch_cdhashes"]
    if not isinstance(cdhashes, dict) or set(cdhashes) != set(ARCHITECTURES):
        raise ServerHostError(
            f"the ownership record at {path.name} must carry exactly the "
            f"{sorted(ARCHITECTURES)} CDHashes",
            code=INVALID_OWNERSHIP,
        )
    per_arch = {
        arch: _require_cdhash(cdhashes[arch], what=f"the {arch} CDHash")
        for arch in ARCHITECTURES
    }

    # Exactly the sealed set: an absolute, traversing, backslashed or otherwise
    # unexpected name is simply not one of these four.
    files = document["bundle_files"]
    if not isinstance(files, dict) or set(files) != REQUIRED_BUNDLE_FILES:
        raise ServerHostError(
            f"the ownership record at {path.name} must map exactly "
            f"{sorted(REQUIRED_BUNDLE_FILES)}",
            code=INVALID_OWNERSHIP,
        )
    bundle_files = {
        name: _require_sha256(files[name], what=f"the digest of {name}")
        for name in sorted(REQUIRED_BUNDLE_FILES)
    }
    if bundle_files[_EXECUTABLE_REL] != executable_sha256:
        raise ServerHostError(
            f"the ownership record at {path.name} gives two executable digests",
            code=INVALID_OWNERSHIP,
        )

    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=bundle_path,
        bundle_id=BUNDLE_ID,
        host_revision=revision,
        host_protocol=protocol,
        executable_sha256=executable_sha256,
        per_arch_cdhashes=per_arch,
        bundle_files=bundle_files,
    )


def _read_same_file(path: Path, checked: os.stat_result) -> bytes:
    """Read ``path`` without following a symlink, refusing a swapped inode.

    The ``lstat`` checks and the read are two lookups of one name; opening with
    ``O_NOFOLLOW`` and comparing the open descriptor's device and inode to the
    checked ones keeps a rename between them from substituting another file.
    ``O_NONBLOCK`` keeps a FIFO swapped in after the check from blocking the
    ``open`` forever before the inode comparison can refuse it; it does not
    change how a regular file reads.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise OSError(f"{path} changed while it was being read")
        return handle.read()


def read_host_ownership(path: Path) -> HostOwnership:
    """Read the owner-only ownership record a verified installation wrote.

    Strict throughout: missing file, symlink, non-regular file, malformed JSON,
    a duplicate key, an unknown schema, a wrong identity or revision, a bad hash,
    a file mapping other than exactly the sealed set and a record living inside
    the bundle it describes (both paths resolved) are all refusals. Values are
    never coerced and no file is written. On macOS the record's owner must be this uid
    and its mode must grant nothing to group or other.

    Only the current revision is accepted. A record of the superseded revision
    is a different question, answered by
    :func:`read_superseded_ownership`.
    """
    return _read_ownership_record(path, revision=HOST_REVISION, protocol=HOST_PROTOCOL)


def read_superseded_ownership(path: Path) -> HostOwnership:
    """Read the ownership record of the one superseded host revision.

    The same strict reader, but it accepts a record whose revision is
    :data:`LEGACY_HOST_REVISION` and whose protocol is the one that revision
    spoke. It answers "was this host ours?", not "is this the host this engine
    speaks to?": the installer uses it to prove a host it may replace.
    """
    return _read_ownership_record(
        path, revision=LEGACY_HOST_REVISION, protocol=LEGACY_HOST_PROTOCOL
    )


def _read_ownership_record(
    path: Path, *, revision: int, protocol: int
) -> HostOwnership:
    """Read and strictly decode an ownership record for one revision."""
    record = Path(path)
    try:
        info = os.lstat(record)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise ServerHostError(
            f"the host ownership record is missing at {record}",
            code=MISSING_RECORD,
        ) from exc
    except OSError as exc:
        raise ServerHostError(
            f"the host ownership record at {record} cannot be examined: {exc}",
            code=INVALID_OWNERSHIP,
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ServerHostError(
            f"the host ownership record must not be a symlink: {record}",
            code=INVALID_OWNERSHIP,
        )
    if not stat.S_ISREG(info.st_mode):
        raise ServerHostError(
            f"the host ownership record must be a regular file: {record}",
            code=INVALID_OWNERSHIP,
        )
    if sys.platform == "darwin":
        if info.st_uid != _getuid():
            raise ServerHostError(
                f"the host ownership record is not owned by this user: {record}",
                code=INVALID_OWNERSHIP,
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ServerHostError(
                f"the host ownership record is group/other accessible: {record}",
                code=INVALID_OWNERSHIP,
            )
    try:
        raw = _read_same_file(record, info).decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ServerHostError(
            f"the host ownership record at {record} is unreadable",
            code=INVALID_OWNERSHIP,
        ) from exc
    ownership = _decode_record(raw, path=record, revision=revision, protocol=protocol)
    # The record must not live inside the bundle it vouches for. Both sides are
    # resolved, so a symlinked ancestor on either path cannot hide the overlap.
    if Path(os.path.realpath(record)).is_relative_to(
        os.path.realpath(ownership.bundle_path)
    ):
        raise ServerHostError(
            "the ownership record must live outside the bundle it describes",
            code=INVALID_OWNERSHIP,
        )
    return ownership


# ── Identity: inspecting the installed bundle ───────────────────────────────


def _run_native(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded, shell-free native probe with a fixed argv.

    A probe that cannot start or does not finish is a refusal, not a crash.
    """
    try:
        completed: subprocess.CompletedProcess[str] = runner(
            list(argv),
            capture_output=True,
            text=True,
            timeout=NATIVE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise ServerHostError(
            f"the host bundle could not be inspected ({' '.join(argv[:2])}): {exc}",
            code=INSPECTION_FAILED,
        ) from exc
    return completed


def _relative_name(root: Path, entry: Path) -> str:
    return entry.relative_to(root).as_posix()


def _refuse_unreadable(error: OSError) -> None:
    """``os.walk`` skips a directory it cannot list; a skipped one is unchecked."""
    raise ServerHostError(
        f"the host bundle cannot be read: {error}", code=INSPECTION_FAILED
    ) from error


def _collect_sealed_files(bundle: Path) -> tuple[dict[str, str], bytes]:
    """Digest every regular file, refusing a symlink, special file or stray entry.

    Returns the digests and the ``Info.plist`` bytes that were digested, so the
    plist the identity checks parse is the one the snapshot records.
    """
    files: dict[str, str] = {}
    plist = b""
    for current, dirs, names in os.walk(
        bundle, onerror=_refuse_unreadable, followlinks=False
    ):
        current_path = Path(current)
        relative_dir = current_path.relative_to(bundle).as_posix()
        for name in list(dirs):
            entry = current_path / name
            if entry.is_symlink():
                raise ServerHostError(
                    f"the host bundle contains a symlinked directory: "
                    f"{_relative_name(bundle, entry)}",
                    code=INSPECTION_FAILED,
                )
        dirs.sort()
        if relative_dir not in _ALLOWED_DIRECTORIES:
            raise ServerHostError(
                f"the host bundle contains an unexpected directory: {relative_dir}",
                code=INSPECTION_FAILED,
            )
        for name in sorted(names):
            entry = current_path / name
            relative = _relative_name(bundle, entry)
            if relative not in REQUIRED_BUNDLE_FILES:
                raise ServerHostError(
                    f"the host bundle contains an unexpected file: {relative}",
                    code=INSPECTION_FAILED,
                )
            try:
                checked = os.lstat(entry)
                mode = checked.st_mode
                if stat.S_ISREG(mode):
                    data = _read_same_file(entry, checked)
                    files[relative] = hashlib.sha256(data).hexdigest()
                    if relative == _PLIST_REL:
                        plist = data
            except OSError as exc:
                raise ServerHostError(
                    f"the host bundle file {relative} cannot be read: {exc}",
                    code=INSPECTION_FAILED,
                ) from exc
            if stat.S_ISLNK(mode):
                raise ServerHostError(
                    f"the host bundle contains a symlink: {relative}",
                    code=INSPECTION_FAILED,
                )
            if not stat.S_ISREG(mode):
                raise ServerHostError(
                    f"the host bundle contains a special file: {relative}",
                    code=INSPECTION_FAILED,
                )
    absent = sorted(REQUIRED_BUNDLE_FILES - set(files))
    if absent:
        raise ServerHostError(
            f"the host bundle does not seal {absent}", code=INSPECTION_FAILED
        )
    return files, plist


def _check_bundle_plist(
    raw: bytes,
    *,
    revision: int = HOST_REVISION,
    protocol: int = HOST_PROTOCOL,
) -> None:
    try:
        info: Any = plistlib.loads(raw)
    except Exception as exc:
        # `plistlib.loads` parses untrusted bytes (a foreign or tampered bundle
        # reaches this before `codesign --verify`), and beyond `ValueError` it
        # raises `AttributeError` (`<date>zz</date>`), `LookupError` (a bogus
        # encoding declaration) and `IndexError` on malformed XML/binary plists.
        # Any of them is an `inspection_failed` refusal, not a crash that would
        # escape a B2 consumer catching only `ServerHostError`. `Exception` is
        # the parser boundary alone; `KeyboardInterrupt`/`SystemExit` pass.
        raise ServerHostError(
            "the host bundle's Info.plist is unreadable", code=INSPECTION_FAILED
        ) from exc
    if not isinstance(info, dict):
        raise ServerHostError(
            "the host bundle's Info.plist is not a dictionary", code=INSPECTION_FAILED
        )
    if info.get("CFBundleIdentifier") != BUNDLE_ID:
        raise ServerHostError(
            f"the host bundle is not {BUNDLE_ID!r}", code=INSPECTION_FAILED
        )
    if info.get("CFBundleExecutable") != EXECUTABLE_NAME:
        raise ServerHostError(
            f"the host bundle's executable is not {EXECUTABLE_NAME!r}",
            code=INSPECTION_FAILED,
        )
    protocol_value = info.get(HOST_PROTOCOL_KEY)
    if not _strict_int(protocol_value) or protocol_value != protocol:
        raise ServerHostError(
            f"the host bundle is not protocol {protocol}", code=INSPECTION_FAILED
        )
    bundle_revision = str(revision)
    if (
        info.get("CFBundleVersion") != bundle_revision
        or info.get("CFBundleShortVersionString") != bundle_revision
    ):
        raise ServerHostError(
            f"the host bundle is not revision {revision}", code=INSPECTION_FAILED
        )
    if info.get("LSMinimumSystemVersion") != MINIMUM_SYSTEM_VERSION:
        raise ServerHostError(
            f"the host bundle requires macOS {info.get('LSMinimumSystemVersion')!r}, "
            f"not {MINIMUM_SYSTEM_VERSION!r}",
            code=INSPECTION_FAILED,
        )


def _display_value(text: str, key: str) -> str | None:
    """The value of the first ``key=`` line ``codesign -dv`` printed, if any."""
    prefix = f"{key}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _display(target: Path, runner: Runner, *extra: str) -> str | None:
    """``codesign -dv --verbose=4`` output (it prints on stderr), or None on failure."""
    completed = _run_native(
        runner, [CODESIGN, "-dv", "--verbose=4", *extra, os.fspath(target)]
    )
    if completed.returncode != 0:
        return None
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"


def _require_signed_universal(bundle: Path, runner: Runner) -> None:
    """An ad-hoc signature for this bundle id over exactly the two slices."""
    shown = _display(bundle, runner)
    if shown is None:
        raise ServerHostError(
            f"the host bundle's signature cannot be read: {bundle}",
            code=INSPECTION_FAILED,
        )
    if _display_value(shown, "Signature") != "adhoc":
        raise ServerHostError(
            "the host bundle is not ad-hoc signed", code=INSPECTION_FAILED
        )
    if _display_value(shown, "Identifier") != BUNDLE_ID:
        raise ServerHostError(
            f"the host bundle is not signed as {BUNDLE_ID!r}", code=INSPECTION_FAILED
        )
    shape = _display_value(shown, "Format") or ""
    match = re.fullmatch(r"app bundle with Mach-O universal \(([^()]*)\)", shape)
    archs = match.group(1).split() if match else []
    if sorted(archs) != sorted(ARCHITECTURES):
        raise ServerHostError(
            f"the host executable is not exactly {sorted(ARCHITECTURES)}: {shape!r}",
            code=INSPECTION_FAILED,
        )


def _per_arch_cdhashes(executable: Path, runner: Runner) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for arch in ARCHITECTURES:
        shown = _display(executable, runner, "--arch", arch)
        cdhash = None if shown is None else _display_value(shown, "CDHash")
        if cdhash is None:
            raise ServerHostError(
                f"the {arch} slice reports no CDHash", code=INSPECTION_FAILED
            )
        hashes[arch] = _require_cdhash(
            cdhash, what=f"the {arch} slice CDHash", code=INSPECTION_FAILED
        )
    return hashes


def inspect_host_bundle(
    bundle_path: Path,
    *,
    runner: Runner = subprocess.run,
    revision: int = HOST_REVISION,
    protocol: int = HOST_PROTOCOL,
) -> HostOwnership:
    """Snapshot an installed macOS host bundle's identity and bytes.

    macOS only: a non-darwin platform is refused before any native probe runs, so
    the module imports and its pure tests run everywhere. The injected ``runner``
    exists so offline tests can prove the exact probe order, the ten-second bound
    and the shell-free, fixed argv without a real host.

    It refuses a bundle not named ``Ciaobot Server.app``, a symlinked bundle, a
    symlinked, special or unreadable file, a missing sealed file, a stray
    directory or file (a thin build product, a config, bundled Python), a wrong
    identity, a non-ad-hoc or invalid signature, a slice set other than exactly
    ``arm64``/``x86_64``, a malformed CDHash and a probe that fails or hangs. What
    it returns is a well-formed host — **not** proof that this machine installed
    it. Only :func:`verify_owned_host` answers that, against an existing private
    record.

    ``revision`` and ``protocol`` name the identity the bundle is expected to
    carry. They default to the current one; :func:`verify_superseded_host`
    passes the superseded pair, so a bundle of an older revision can be
    inspected without loosening any other check.
    """
    if sys.platform != "darwin":
        raise ServerHostError(
            "the Ciaobot Server host is a macOS bundle; this platform cannot inspect it",
            code=UNSUPPORTED_PLATFORM,
        )
    supplied = Path(bundle_path)
    if not supplied.is_absolute():
        raise ServerHostError(
            f"the host bundle path must be absolute: {supplied}", code=INSPECTION_FAILED
        )
    if supplied.name != APP_NAME:
        raise ServerHostError(
            f"the host bundle must be named {APP_NAME!r}: {supplied}",
            code=INSPECTION_FAILED,
        )
    if supplied.is_symlink():
        raise ServerHostError(
            f"the host bundle must not be a symlink: {supplied}", code=INSPECTION_FAILED
        )
    if not supplied.is_dir():
        raise ServerHostError(
            f"there is no host bundle at {supplied}", code=INSPECTION_FAILED
        )
    bundle = Path(os.path.realpath(supplied))

    # The walk refuses a symlinked `Contents` and a missing sealed file.
    files, plist = _collect_sealed_files(bundle)
    _check_bundle_plist(plist, revision=revision, protocol=protocol)

    # Verification covers every architecture of a universal binary by default.
    verify = _run_native(runner, [CODESIGN, "--verify", "--strict", os.fspath(bundle)])
    if verify.returncode != 0:
        raise ServerHostError(
            f"the host bundle fails strict signature verification: {bundle}",
            code=INSPECTION_FAILED,
        )
    _require_signed_universal(bundle, runner)
    cdhashes = _per_arch_cdhashes(bundle / _EXECUTABLE_REL, runner)

    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=os.fspath(bundle),
        bundle_id=BUNDLE_ID,
        host_revision=revision,
        host_protocol=protocol,
        executable_sha256=files[_EXECUTABLE_REL],
        per_arch_cdhashes=cdhashes,
        bundle_files=files,
    )


# ── Ownership: the record is the only proof ─────────────────────────────────


def verify_owned_host(
    bundle_path: Path,
    *,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> HostOwnership:
    """Prove a bundle is this machine's host by matching an existing record.

    Reads the owner-only record (default :func:`default_ownership_path`), then
    inspects the bundle, and requires the canonical bundle path, identity,
    revision, protocol, executable digest, both CDHashes and the whole file set
    to be equal. It never falls back to a direct engine when the record is
    missing, never recreates a record, and never signs, launches, chmods,
    deletes or writes anything. A foreign, tampered or incompatible signed bundle
    is refused here, before any service change.
    """
    record_path = (
        default_ownership_path() if ownership_path is None else Path(ownership_path)
    )
    record = read_host_ownership(record_path)
    inspected = inspect_host_bundle(bundle_path, runner=runner)

    if record.bundle_path != inspected.bundle_path:
        raise ServerHostError(
            f"the ownership record names {record.bundle_path}, but the bundle is "
            f"{inspected.bundle_path}",
            code=NOT_OWNED,
        )
    # Both snapshots carry the fixed identity constants, so equality is the
    # executable digest, both CDHashes and the whole file set.
    if record != inspected:
        raise ServerHostError(
            "the installed host bundle does not match its ownership record",
            code=NOT_OWNED,
        )
    return inspected


def verify_superseded_host(
    bundle_path: Path,
    *,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> HostOwnership:
    """Prove a bundle is the superseded host this machine installed.

    The upgrade path for a host revision bump (#1659). A revision-3 host is not
    the host this engine speaks to, so :func:`verify_owned_host` refuses it;
    but it is still *ours*, and the installer must be able to replace it rather
    than leave the Mac without a host. This answers that narrower question: it
    reads the owner-only record (default :func:`default_ownership_path`) with
    the superseded revision and protocol, inspects the bundle against the same
    pair, and requires the canonical bundle path, identity, executable digest,
    both CDHashes and the whole file set to be equal.

    Only :data:`LEGACY_HOST_REVISION` is accepted. A record of any other
    revision - the current one, an older one this engine never shipped, or a
    future one - is refused, so this can never prove a host the installer may
    not replace. It never writes, signs, launches or deletes anything.
    """
    record_path = (
        default_ownership_path() if ownership_path is None else Path(ownership_path)
    )
    record = read_superseded_ownership(record_path)
    inspected = inspect_host_bundle(
        bundle_path,
        runner=runner,
        revision=LEGACY_HOST_REVISION,
        protocol=LEGACY_HOST_PROTOCOL,
    )

    if record.bundle_path != inspected.bundle_path:
        raise ServerHostError(
            f"the ownership record names {record.bundle_path}, but the bundle is "
            f"{inspected.bundle_path}",
            code=NOT_OWNED,
        )
    if record != inspected:
        raise ServerHostError(
            "the installed host bundle does not match its ownership record",
            code=NOT_OWNED,
        )
    return inspected
