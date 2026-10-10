"""Verified macOS server-host identity, command and ownership contract (#1023).

The child plan for #1008 (B1). It defines the one fail-closed validator a later
service consumer (B2) renders an invocation from and an installer writes a
record with. There is no consumer wiring, no service activation and no receipt
writer here; the three layers below are the whole module.

Three questions, deliberately kept apart, in the order they must be asked:

* **Syntax** — :func:`parse_service_command` reads an invocation and answers
  only what the argv *says*: a typed :class:`ServiceCommand`, either the direct
  engine shape (``python -m ciao.cli run|supervise`` or ``ciao run|supervise``)
  or the native-host shape (``Ciaobot Server serve --python <abs>``). It is
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
    "LEGACY_EXECUTABLE_NAME",
    "LEGACY_HOST_REVISION",
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
    "inspect_host_bundle",
    "parse_service_command",
    "read_host_ownership",
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
HOST_REVISION = 2
HOST_PROTOCOL_KEY = "CiaobotServerHostProtocol"
HOST_PROTOCOL = 1
MINIMUM_SYSTEM_VERSION = "13.0"

# MIGRATION (#1249), delete when no install can still hold it. Host revision 1
# shipped the executable as ``CiaobotServerHost``; revision 2 renames it so the
# macOS Login Items row reads "Ciaobot Server". The only reader of these values
# is :func:`verify_superseded_host`, which the installer calls to prove an
# existing revision-1 bundle is ours before replacing it. Nothing else accepts
# the old name: a revision-1 bundle is refused everywhere else. Delete these two
# constants, :func:`verify_superseded_host` and its installer caller once no
# supported install can still hold a revision-1 bundle, i.e. after a release
# that ships revision 2 has been out for a full release cycle.
LEGACY_EXECUTABLE_NAME = "CiaobotServerHost"
LEGACY_HOST_REVISION = 1

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
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"


@dataclass(frozen=True)
class _Layout:
    """The bundle layout one host revision seals: its executable and its revision."""

    executable_name: str
    host_revision: int

    @property
    def executable_rel(self) -> str:
        return f"Contents/MacOS/{self.executable_name}"

    @property
    def required_files(self) -> frozenset[str]:
        """Exactly the files ``scripts/build-server-host.py`` seals for this revision."""
        return frozenset({_PLIST_REL, self.executable_rel, _ICON_REL, _CODESIGN_REL})


_CURRENT_LAYOUT = _Layout(EXECUTABLE_NAME, HOST_REVISION)
# MIGRATION (#1249): see LEGACY_EXECUTABLE_NAME above.
_LEGACY_LAYOUT = _Layout(LEGACY_EXECUTABLE_NAME, LEGACY_HOST_REVISION)

_EXECUTABLE_REL = _CURRENT_LAYOUT.executable_rel

#: Exactly the files ``scripts/build-server-host.py`` seals into a host bundle.
#: An inspected bundle and a record's mapping must both name this set and
#: nothing else: any other file is a thin staging product, a stray config,
#: bundled Python or tampering. A new resource is a new host revision.
REQUIRED_BUNDLE_FILES = _CURRENT_LAYOUT.required_files

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

    Accepts exactly the two shapes the service actually uses:

    * hosted — ``[<abs>/Ciaobot Server, serve, --python, <abs python>]``;
    * direct — ``[<abs python>, [-I], -m, ciao.cli, run|supervise]`` or
      ``[<abs ciao>, run|supervise]``.

    Repeated, unknown, extra or relative arguments, a non-string element, a
    non-sequence and a wrong host basename are all refusals. The hosted shape is
    syntax only and never proves ownership.
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


def _decode_record(raw: str, *, path: Path, layout: _Layout) -> HostOwnership:
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
        or document["host_revision"] != layout.host_revision
    ):
        raise ServerHostError(
            f"the ownership record at {path.name} is not host revision {layout.host_revision}",
            code=INVALID_OWNERSHIP,
        )
    if (
        not _strict_int(document["host_protocol"])
        or document["host_protocol"] != HOST_PROTOCOL
    ):
        raise ServerHostError(
            f"the ownership record at {path.name} is not host protocol {HOST_PROTOCOL}",
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
    if not isinstance(files, dict) or set(files) != layout.required_files:
        raise ServerHostError(
            f"the ownership record at {path.name} must map exactly "
            f"{sorted(layout.required_files)}",
            code=INVALID_OWNERSHIP,
        )
    bundle_files = {
        name: _require_sha256(files[name], what=f"the digest of {name}")
        for name in sorted(layout.required_files)
    }
    if bundle_files[layout.executable_rel] != executable_sha256:
        raise ServerHostError(
            f"the ownership record at {path.name} gives two executable digests",
            code=INVALID_OWNERSHIP,
        )

    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=bundle_path,
        bundle_id=BUNDLE_ID,
        host_revision=layout.host_revision,
        host_protocol=HOST_PROTOCOL,
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


def _read_ownership(path: Path, layout: _Layout) -> HostOwnership:
    """Read the owner-only ownership record a verified installation wrote.

    Strict throughout: missing file, symlink, non-regular file, malformed JSON,
    a duplicate key, an unknown schema, a wrong identity or revision, a bad hash,
    a file mapping other than exactly the sealed set and a record living inside
    the bundle it describes (both paths resolved) are all refusals. Values are
    never coerced and no file is written. On macOS the record's owner must be this uid
    and its mode must grant nothing to group or other.
    """
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
        if info.st_uid != os.getuid():
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
    ownership = _decode_record(raw, path=record, layout=layout)
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


def read_host_ownership(path: Path) -> HostOwnership:
    """Read the owner-only ownership record a verified installation wrote.

    Strict throughout: missing file, symlink, non-regular file, malformed JSON,
    a duplicate key, an unknown schema, a wrong identity or revision, a bad hash,
    a file mapping other than exactly the sealed set and a record living inside
    the bundle it describes (both paths resolved) are all refusals. Values are
    never coerced and no file is written. On macOS the record's owner must be this uid
    and its mode must grant nothing to group or other.
    """
    return _read_ownership(Path(path), _CURRENT_LAYOUT)


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


def _collect_sealed_files(
    bundle: Path, layout: _Layout
) -> tuple[dict[str, str], bytes]:
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
            if relative not in layout.required_files:
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
    absent = sorted(layout.required_files - set(files))
    if absent:
        raise ServerHostError(
            f"the host bundle does not seal {absent}", code=INSPECTION_FAILED
        )
    return files, plist


def _check_bundle_plist(raw: bytes, layout: _Layout) -> None:
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
    if info.get("CFBundleExecutable") != layout.executable_name:
        raise ServerHostError(
            f"the host bundle's executable is not {layout.executable_name!r}",
            code=INSPECTION_FAILED,
        )
    protocol = info.get(HOST_PROTOCOL_KEY)
    if not _strict_int(protocol) or protocol != HOST_PROTOCOL:
        raise ServerHostError(
            f"the host bundle is not protocol {HOST_PROTOCOL}", code=INSPECTION_FAILED
        )
    revision = str(layout.host_revision)
    if (
        info.get("CFBundleVersion") != revision
        or info.get("CFBundleShortVersionString") != revision
    ):
        raise ServerHostError(
            f"the host bundle is not revision {layout.host_revision}",
            code=INSPECTION_FAILED,
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


def _inspect_bundle(
    bundle_path: Path, runner: Runner, layout: _Layout
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
    files, plist = _collect_sealed_files(bundle, layout)
    _check_bundle_plist(plist, layout)

    # Verification covers every architecture of a universal binary by default.
    verify = _run_native(runner, [CODESIGN, "--verify", "--strict", os.fspath(bundle)])
    if verify.returncode != 0:
        raise ServerHostError(
            f"the host bundle fails strict signature verification: {bundle}",
            code=INSPECTION_FAILED,
        )
    _require_signed_universal(bundle, runner)
    cdhashes = _per_arch_cdhashes(bundle / layout.executable_rel, runner)

    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=os.fspath(bundle),
        bundle_id=BUNDLE_ID,
        host_revision=layout.host_revision,
        host_protocol=HOST_PROTOCOL,
        executable_sha256=files[layout.executable_rel],
        per_arch_cdhashes=cdhashes,
        bundle_files=files,
    )


# ── Ownership: the record is the only proof ─────────────────────────────────


def _verify_owned(
    bundle_path: Path,
    ownership_path: Path | None,
    runner: Runner,
    layout: _Layout,
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
    record = _read_ownership(record_path, layout)
    inspected = _inspect_bundle(bundle_path, runner, layout)

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


# ── Public entry points ─────────────────────────────────────────────────────


def inspect_host_bundle(
    bundle_path: Path, *, runner: Runner = subprocess.run
) -> HostOwnership:
    """Snapshot an installed host bundle's identity and bytes (see :func:`_inspect_bundle`).

    Only the current host revision is accepted. A snapshot is a well-formed host,
    **not** proof that this machine installed it: :func:`verify_owned_host` is
    the only answer to that.
    """
    return _inspect_bundle(Path(bundle_path), runner, _CURRENT_LAYOUT)


def verify_owned_host(
    bundle_path: Path,
    *,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> HostOwnership:
    """Prove a current-revision bundle is this machine's host against its record.

    Reads the owner-only record (default :func:`default_ownership_path`), inspects
    the bundle and requires them to match exactly. A missing record is a refusal;
    there is no direct-engine fallback. Nothing is signed, launched, chmodded,
    deleted or written. A revision-1 bundle is refused here: it is replaced by
    the installer through :func:`verify_superseded_host`, never accepted.
    """
    return _verify_owned(bundle_path, ownership_path, runner, _CURRENT_LAYOUT)


def verify_superseded_host(
    bundle_path: Path,
    *,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> HostOwnership:
    """MIGRATION (#1249): prove an existing revision-1 ``CiaobotServerHost`` bundle is ours.

    The same exact record-and-bundle match as :func:`verify_owned_host`, against
    the revision-1 layout (:data:`LEGACY_EXECUTABLE_NAME`). It exists for one
    caller only: the installer, which replaces such a bundle with the current
    revision and records it again. Callers must never use it to accept the old
    name for anything else.
    """
    return _verify_owned(bundle_path, ownership_path, runner, _LEGACY_LAYOUT)
