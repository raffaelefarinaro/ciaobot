"""Verified macOS server-host identity, command and ownership contract (#1023).

The child plan for #1008 (B1). It defines the one fail-closed validator a later
service consumer (B2) renders an invocation from and an installer writes a
record with. There is no consumer wiring, no service activation and no receipt
writer here; the three layers below are the whole module.

Three questions, deliberately kept apart, in the order they must be asked:

* **Syntax** — :func:`parse_service_command` reads an invocation and answers
  only what the argv *says*: a typed :class:`ServiceCommand`, either the direct
  engine shape (``python -m ciao.cli run|supervise`` or ``ciao run|supervise``)
  or the native-host shape (``CiaobotServerHost serve --python <abs>``). It is
  pure, runs on every platform, and proves nothing about the files it names. A
  hosted command whose executable basename is ``CiaobotServerHost`` is accepted
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
the three public, read-only probes ``codesign --verify``, ``codesign -dv`` and
``lipo -archs``: no signing, launch, ``open``, ``launchctl`` or AX call, and
every subprocess is bounded and shell-free.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

__all__ = [
    "APP_NAME",
    "ARCHITECTURES",
    "BUNDLE_ID",
    "DEFAULT_BUNDLE_PATH",
    "DEFAULT_OWNERSHIP_PATH",
    "EXECUTABLE_NAME",
    "EXIT_TIMEOUT_SECONDS",
    "HOST_PROTOCOL",
    "HOST_PROTOCOL_KEY",
    "HOST_REVISION",
    "ICON_NAME",
    "MINIMUM_SYSTEM_VERSION",
    "NATIVE_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "STOP_GRACE_SECONDS",
    "HostOwnership",
    "ServerHostError",
    "ServiceCommand",
    "host_service_argv",
    "inspect_host_bundle",
    "parse_service_command",
    "read_host_ownership",
    "verify_owned_host",
]

#: The fixed bundle identity. These must agree with ``scripts/build-server-host.py``
#: and ``native/server-host/ServerHost.swift``; ``tests/test_server_host_contract.py``
#: asserts that they do.
APP_NAME = "Ciaobot Server.app"
BUNDLE_ID = "local.ciaobot.server"
EXECUTABLE_NAME = "CiaobotServerHost"
ICON_NAME = "CiaobotServer.icns"
HOST_REVISION = 1
HOST_PROTOCOL_KEY = "CiaobotServerHostProtocol"
HOST_PROTOCOL = 1
MINIMUM_SYSTEM_VERSION = "13.0"

#: The host's own stop grace and the launchd ``ExitTimeOut`` a service plist must
#: exceed so launchd does not sweep the job group out from under the host's stop.
#: ``EXIT_TIMEOUT_SECONDS`` is deliberately above ``STOP_GRACE_SECONDS``, which is
#: itself deliberately above the Python supervisor's own grace.
STOP_GRACE_SECONDS = 35
EXIT_TIMEOUT_SECONDS = 45

#: The on-disk ownership record schema this module reads.
SCHEMA_VERSION = 1

#: Every native probe is bounded and shell-free. ``codesign``/``lipo`` are local
#: and fast; ten seconds is a hang, not a slow machine.
NATIVE_TIMEOUT_SECONDS = 10

#: Where the host bundle and its ownership record live by default. Hardcoded
#: constants, not environment variables: a path that could be redirected by the
#: environment is a path an unprivileged writer could redirect.
DEFAULT_BUNDLE_PATH = Path.home() / "Applications" / APP_NAME
DEFAULT_OWNERSHIP_PATH = (
    Path.home() / ".local" / "state" / "ciaobot" / "server-host.json"
)

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
_PYTHON_RE = re.compile(r"^python3(?:\.\d+)?$")

_PLIST_REL = "Contents/Info.plist"
_EXECUTABLE_REL = f"Contents/MacOS/{EXECUTABLE_NAME}"
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"

#: The four files every host bundle must seal. The record's mapping must name
#: all of them; every other file it names must be a real sealed file too.
REQUIRED_BUNDLE_FILES = frozenset(
    {_PLIST_REL, _EXECUTABLE_REL, _ICON_REL, _CODESIGN_REL}
)

#: Directories a host bundle may contain. Anything else is a thin staging file,
#: a stray config, bundled Python or tampering, and is refused rather than
#: recorded.
_ALLOWED_DIRECTORIES = frozenset(
    {
        ".",
        "Contents",
        "Contents/MacOS",
        "Contents/Resources",
        "Contents/_CodeSignature",
    }
)
_ALLOWED_EXTRA_PREFIX = "Contents/_CodeSignature/"

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
    if not PurePosixPath(value).is_absolute():
        raise ServerHostError(f"{what} must be absolute: {value!r}", code=code)
    return value


def _is_python_basename(name: str) -> bool:
    return name == "python3" or bool(_PYTHON_RE.fullmatch(name))


def _parse_direct(program: str, argv: tuple[str, ...]) -> ServiceCommand:
    base = PurePosixPath(program).name
    if base == "python" or _is_python_basename(base):
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

    * hosted — ``[<abs>/CiaobotServerHost, serve, --python, <abs python>]``;
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
        python = _absolute_posix(argv[3], what="the interpreter", code=INVALID_COMMAND)
        return ServiceCommand("hosted", host, python, argv)

    program = _absolute_posix(argv[0], what="the program", code=INVALID_COMMAND)
    return _parse_direct(program, argv)


def host_service_argv(bundle_path: Path, python: Path) -> tuple[str, ...]:
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
    interpreter = _absolute_posix(
        os.fspath(python), what="the interpreter", code=INVALID_COMMAND
    )
    host = PurePosixPath(bundle) / "Contents" / "MacOS" / EXECUTABLE_NAME
    return (os.fspath(host), "serve", "--python", interpreter)


# ── The ownership snapshot ──────────────────────────────────────────────────


@dataclass(frozen=True)
class HostOwnership:
    """The immutable identity and bytes of one host bundle.

    Returned by :func:`inspect_host_bundle` (an unproven snapshot) and by
    :func:`verify_owned_host` (proven against an existing private record). The
    same shape serves both so a consumer can render from a verified value
    without a second type; only the second call answers "is it ours".
    """

    schema: int
    bundle_path: str
    bundle_id: str
    host_revision: int
    host_protocol: int
    executable_sha256: str
    per_arch_cdhashes: Mapping[str, str]
    bundle_files: Mapping[str, str]

    def to_record(self) -> dict[str, Any]:
        """The record document this snapshot serializes to.

        Pure: it returns a dict and writes nothing. Persisting it owner-only,
        outside the bundle and only after a verified installation, is the
        installer's later job (B2), not this module's.
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


def _safe_relative_name(name: object, *, what: str) -> str:
    """Require a canonical, relative, POSIX file name inside the bundle."""
    if not isinstance(name, str) or name == "":
        raise ServerHostError(
            f"{what} must be a non-empty relative path", code=INVALID_OWNERSHIP
        )
    if name.startswith("/") or "\\" in name or "\x00" in name:
        raise ServerHostError(
            f"{what} must be a relative POSIX path without a backslash: {name!r}",
            code=INVALID_OWNERSHIP,
        )
    if str(PurePosixPath(name)) != name:
        raise ServerHostError(
            f"{what} is not canonical (a dot, doubled slash or trailing slash): {name!r}",
            code=INVALID_OWNERSHIP,
        )
    if any(part in ("", ".", "..") for part in PurePosixPath(name).parts):
        raise ServerHostError(
            f"{what} must not traverse: {name!r}", code=INVALID_OWNERSHIP
        )
    return name


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


def _current_uid() -> int:
    """This user's uid, read through ``getattr`` so the module imports on Windows."""
    getuid: Callable[[], int] = getattr(os, "getuid")
    return getuid()


def _decode_record(raw: str, *, path: Path) -> HostOwnership:
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
        or document["host_revision"] != HOST_REVISION
    ):
        raise ServerHostError(
            f"the ownership record at {path.name} is not host revision {HOST_REVISION}",
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

    files = document["bundle_files"]
    if not isinstance(files, dict) or not files:
        raise ServerHostError(
            f"the ownership record at {path.name} has no bundle file mapping",
            code=INVALID_OWNERSHIP,
        )
    bundle_files: dict[str, str] = {}
    for name, digest in files.items():
        safe = _safe_relative_name(name, what="a recorded bundle file name")
        bundle_files[safe] = _require_sha256(digest, what=f"the digest of {safe}")
    absent = sorted(REQUIRED_BUNDLE_FILES - set(bundle_files))
    if absent:
        raise ServerHostError(
            f"the ownership record at {path.name} does not seal {absent}",
            code=INVALID_OWNERSHIP,
        )

    # The record must not live inside the bundle it vouches for. Compare as
    # POSIX: both are macOS paths when this matters, and a Windows record path
    # simply cannot be a prefix of an absolute POSIX bundle path.
    record_posix = PurePosixPath(os.fspath(path))
    bundle_posix = PurePosixPath(bundle_path)
    if record_posix == bundle_posix or bundle_posix in record_posix.parents:
        raise ServerHostError(
            "the ownership record must live outside the bundle it describes",
            code=INVALID_OWNERSHIP,
        )

    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=bundle_path,
        bundle_id=BUNDLE_ID,
        host_revision=HOST_REVISION,
        host_protocol=HOST_PROTOCOL,
        executable_sha256=executable_sha256,
        per_arch_cdhashes=per_arch,
        bundle_files=bundle_files,
    )


def read_host_ownership(path: Path) -> HostOwnership:
    """Read the owner-only ownership record a verified installation wrote.

    Strict throughout: missing file, symlink, non-regular file, malformed JSON,
    a duplicate key, an unknown schema, a wrong identity or revision, a bad hash,
    a traversing or backslashed file name, a missing sealed file and a record
    living inside the bundle it describes are all refusals. Values are never
    coerced and no file is written. On macOS the record's owner must be this uid
    and its mode must grant nothing to group or other.
    """
    record = Path(path)
    try:
        info = os.lstat(record)
    except OSError as exc:
        raise ServerHostError(
            f"the host ownership record is missing at {record}",
            code=MISSING_RECORD,
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
        if info.st_uid != _current_uid():
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
        raw = record.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ServerHostError(
            f"the host ownership record at {record} is unreadable",
            code=INVALID_OWNERSHIP,
        ) from exc
    return _decode_record(raw, path=record)


# ── Identity: inspecting the installed bundle ───────────────────────────────


def _run_native(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded, shell-free native probe with a fixed argv."""
    completed: subprocess.CompletedProcess[str] = runner(
        list(argv),
        capture_output=True,
        text=True,
        timeout=NATIVE_TIMEOUT_SECONDS,
        check=False,
    )
    return completed


def _relative_name(root: Path, entry: Path) -> str:
    return entry.relative_to(root).as_posix()


def _collect_sealed_files(bundle: Path) -> dict[str, str]:
    """Digest every regular file, refusing a symlink, special file or stray dir."""
    files: dict[str, str] = {}
    for current, dirs, names in os.walk(bundle, followlinks=False):
        current_path = Path(current)
        relative_dir = current_path.relative_to(bundle).as_posix()
        if relative_dir == ".":
            relative_dir = "."
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
            info = os.lstat(entry)
            if stat.S_ISLNK(info.st_mode):
                raise ServerHostError(
                    f"the host bundle contains a symlink: "
                    f"{_relative_name(bundle, entry)}",
                    code=INSPECTION_FAILED,
                )
            if not stat.S_ISREG(info.st_mode):
                raise ServerHostError(
                    f"the host bundle contains a special file: "
                    f"{_relative_name(bundle, entry)}",
                    code=INSPECTION_FAILED,
                )
            relative = _relative_name(bundle, entry)
            if relative not in REQUIRED_BUNDLE_FILES and not relative.startswith(
                _ALLOWED_EXTRA_PREFIX
            ):
                raise ServerHostError(
                    f"the host bundle contains an unexpected file: {relative}",
                    code=INSPECTION_FAILED,
                )
            files[relative] = hashlib.sha256(entry.read_bytes()).hexdigest()
    absent = sorted(REQUIRED_BUNDLE_FILES - set(files))
    if absent:
        raise ServerHostError(
            f"the host bundle does not seal {absent}", code=INSPECTION_FAILED
        )
    return files


def _read_bundle_plist(bundle: Path) -> dict[str, Any]:
    plist_path = bundle / _PLIST_REL
    try:
        info: Any = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ServerHostError(
            f"the host bundle's Info.plist is unreadable: {plist_path}",
            code=INSPECTION_FAILED,
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
    if info.get(HOST_PROTOCOL_KEY) != HOST_PROTOCOL:
        raise ServerHostError(
            f"the host bundle is not protocol {HOST_PROTOCOL}", code=INSPECTION_FAILED
        )
    revision = str(HOST_REVISION)
    if (
        info.get("CFBundleVersion") != revision
        or info.get("CFBundleShortVersionString") != revision
    ):
        raise ServerHostError(
            f"the host bundle is not revision {HOST_REVISION}", code=INSPECTION_FAILED
        )
    if info.get("LSMinimumSystemVersion") != MINIMUM_SYSTEM_VERSION:
        raise ServerHostError(
            f"the host bundle requires macOS {info.get('LSMinimumSystemVersion')!r}, "
            f"not {MINIMUM_SYSTEM_VERSION!r}",
            code=INSPECTION_FAILED,
        )
    return info


def _cdhash_from_output(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1].strip()
    return None


def _require_ad_hoc_signature(bundle: Path, runner: Runner) -> None:
    completed = _run_native(
        runner, ["codesign", "-dv", "--verbose=4", os.fspath(bundle)]
    )
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if completed.returncode != 0:
        raise ServerHostError(
            f"the host bundle's signature cannot be read: {bundle}",
            code=INSPECTION_FAILED,
        )
    if "Signature=adhoc" not in combined.splitlines():
        raise ServerHostError(
            "the host bundle is not ad-hoc signed", code=INSPECTION_FAILED
        )


def _require_universal_slices(executable: Path, runner: Runner) -> None:
    completed = _run_native(runner, ["lipo", "-archs", os.fspath(executable)])
    if completed.returncode != 0:
        raise ServerHostError(
            f"the host executable's architectures cannot be read: {executable}",
            code=INSPECTION_FAILED,
        )
    archs = set((completed.stdout or "").split())
    if archs != set(ARCHITECTURES):
        raise ServerHostError(
            f"the host executable is not universal; it has {sorted(archs)}",
            code=INSPECTION_FAILED,
        )


def _per_arch_cdhashes(executable: Path, runner: Runner) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for arch in ARCHITECTURES:
        completed = _run_native(
            runner,
            ["codesign", "-dv", "--verbose=4", "--arch", arch, os.fspath(executable)],
        )
        combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        cdhash = _cdhash_from_output(combined) if completed.returncode == 0 else None
        if cdhash is None:
            raise ServerHostError(
                f"the {arch} slice reports no CDHash", code=INSPECTION_FAILED
            )
        hashes[arch] = _require_cdhash(
            cdhash, what=f"the {arch} slice CDHash", code=INSPECTION_FAILED
        )
    return hashes


def inspect_host_bundle(
    bundle_path: Path, *, runner: Runner = subprocess.run
) -> HostOwnership:
    """Snapshot an installed macOS host bundle's identity and bytes.

    macOS only: a non-darwin platform is refused before any native probe runs, so
    the module imports and its pure tests run everywhere. The injected ``runner``
    exists so offline tests can prove the exact probe order, the ten-second bound
    and the shell-free, fixed argv without a real host.

    It refuses a symlinked bundle, a symlinked or special file, a missing sealed
    file, a stray directory or file (a thin build product, a config, bundled
    Python), a wrong identity, a non-ad-hoc or invalid signature, a missing
    architecture and a malformed CDHash. What it returns is a well-formed host —
    **not** proof that this machine installed it. Only :func:`verify_owned_host`
    answers that, against an existing private record.
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
    if supplied.is_symlink():
        raise ServerHostError(
            f"the host bundle must not be a symlink: {supplied}", code=INSPECTION_FAILED
        )
    if not supplied.is_dir():
        raise ServerHostError(
            f"there is no host bundle at {supplied}", code=INSPECTION_FAILED
        )
    bundle = Path(os.path.realpath(supplied))

    contents = bundle / "Contents"
    if contents.is_symlink() or not contents.is_dir():
        raise ServerHostError(
            "the host bundle has no Contents directory", code=INSPECTION_FAILED
        )

    executable = bundle / _EXECUTABLE_REL
    files = _collect_sealed_files(bundle)
    _read_bundle_plist(bundle)

    verify = _run_native(
        runner, ["codesign", "--verify", "--strict", os.fspath(bundle)]
    )
    if verify.returncode != 0:
        raise ServerHostError(
            f"the host bundle fails strict signature verification: {bundle}",
            code=INSPECTION_FAILED,
        )
    _require_ad_hoc_signature(bundle, runner)
    _require_universal_slices(executable, runner)
    cdhashes = _per_arch_cdhashes(executable, runner)

    executable_sha256 = _require_sha256(
        files[_EXECUTABLE_REL], what="executable_sha256"
    )
    return HostOwnership(
        schema=SCHEMA_VERSION,
        bundle_path=os.fspath(bundle),
        bundle_id=BUNDLE_ID,
        host_revision=HOST_REVISION,
        host_protocol=HOST_PROTOCOL,
        executable_sha256=executable_sha256,
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

    Reads the owner-only record (default :data:`DEFAULT_OWNERSHIP_PATH`), then
    inspects the bundle, and requires the canonical bundle path, identity,
    revision, protocol, executable digest, both CDHashes and the whole file set
    to be equal. It never falls back to a direct engine when the record is
    missing, never recreates a record, and never signs, launches, chmods,
    deletes or writes anything. A foreign, tampered or incompatible signed bundle
    is refused here, before any service change.
    """
    record_path = (
        DEFAULT_OWNERSHIP_PATH if ownership_path is None else Path(ownership_path)
    )
    record = read_host_ownership(record_path)
    inspected = inspect_host_bundle(bundle_path, runner=runner)

    if record.bundle_path != inspected.bundle_path:
        raise ServerHostError(
            f"the ownership record names {record.bundle_path}, but the bundle is "
            f"{inspected.bundle_path}",
            code=NOT_OWNED,
        )
    if (
        record.executable_sha256 != inspected.executable_sha256
        or dict(record.per_arch_cdhashes) != dict(inspected.per_arch_cdhashes)
        or dict(record.bundle_files) != dict(inspected.bundle_files)
    ):
        raise ServerHostError(
            "the installed host bundle does not match its ownership record",
            code=NOT_OWNED,
        )
    return inspected
