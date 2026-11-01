"""The native installer handoff: schema 2, wired only through the native context.

Ciaobot has two installer generations, and they must not share a result
schema. The legacy ``ciao.installer_handoff`` remains the production bootstrap
and activation path: it writes schema 1 and verifies exactly what it did
before. This module is the native handoff. The legacy CLI, routes and installer
paths never import it; the native bootstrap child imports it through
:class:`ciao.native_installer.NativeBootstrapContext`, the existing loopback-only,
bootstrap-mode-only ``POST /api/setup/finish`` (no session cookie; see
docs/REMOTE_BOUNDARY.md) handler's native branch, and
:func:`ciao.native_installer._run_native_install`'s activation dep. The public
``run_native_install`` still refuses until the shell/readiness integration
completes, so nothing here runs without that explicit native context.

What it shares with the legacy path is the storage location: one owner-only
``~/.ciao/installer-bootstrap/handoff.json``. What it does not share is the
schema. A native result is schema 2 only, and the two readers refuse each
other's documents, so a result written by one generation can never be
activated by the other.

The schema-2 result binds the setup child's report to the exact bytes of the
service definition it wrote, to the program the definition actually executes,
to the interpreter that program runs, to both files' identities, and to the
environment block the definition carries. A direct ``ciao`` console is a
script: the program identity describes the script and the interpreter identity
describes the absolute Python named on its shebang line. An unsupported console
(``/usr/bin/env``, a relative path, an argument-bearing shebang, or no shebang)
is refused rather than recorded as if the launcher were the interpreter. The
definition is always the authority.

Recording validates the job semantics, the workspace and the port against the
same bytes it hashes before it writes a result: a definition whose job launchd
would run differently from the template setup writes, or whose workspace or
port differs from setup's arguments, is a refusal and no result is written.
Activation re-establishes every one of those facts in the parent, then parses
the same bytes that produced the digest and requires the job launchd would run
to be the one setup wrote. Missing, stale, malformed, changed or uncertain is a
refusal; nothing here deletes, reorganises or replaces a service.

Reading is one strict, bounded descriptor everywhere: the result and the
definition go through ``ciao.os_support.definition`` (no-follow, non-blocking,
exact byte count, inode/size/mtime rechecks), and the result's privacy is
checked on the opened descriptor rather than the pathname. That check is the
strict one: the inode must be owned by the effective user and, on macOS, must
not carry an extended ACL allow entry for another account. The scratch directory
chain (home, ``~/.ciao``, ``installer-bootstrap``) is validated separately: no
component may be a symlink, every component must be owned by the current user,
no ancestor may be group/other-writable, and the scratch directory itself must
be ``0o700``. An existing directory is refused rather than chmod'ed. Activation's
default completed-workspace check is native-only for the same reason: the legacy
``is_set_up_workspace`` keeps ``read_text`` for its existing callers, but a FIFO
where a workspace ``.env`` belongs must refuse here, not hang the installer.

That chain has to be private before the bootstrap child starts, and the child
cannot be the one to create it privately: ``ciao run --installer-bootstrap``
creates its ``launcher`` folder and
``CiaoConfig.from_env(installer_bootstrap=True)`` creates ``root``,
``launcher`` and ``runtime`` with ``mkdir(parents=True, exist_ok=True)``, so on
a missing chain the scratch directory is created at the process umask
(normally ``0o755``) and recording then refuses it.
:func:`prepare_bootstrap_scratch` is the native parent's narrow preparation
step: it creates only the missing chain components ``0o700`` and fails closed on
a pre-existing symlinked, foreign-owned, group/other-writable or non-``0o700``
scratch directory, without chmod'ing it. The legacy config is unchanged. #2340
must call it after the parent's read-only fresh-install preflight and before
spawning that child — not before the preflight, and not to repair an existing
installation. Nothing calls it in production in this issue.

The native path implements macOS only. Windows remains on the legacy
installer: its task XML carries no port, and its only registration call uses
``schtasks /Create /F``, which can replace an intervening task. This module
cannot bind the port from the task bytes or promise fresh-only registration
without changing that legacy path, so a native Windows record or activation is
refused explicitly instead of claiming support it cannot back.

Five limits are real and are not hidden:

* Recording happens after setup has written the definition and mutated the
  workspace. Its checks validate and refuse a result; they cannot undo setup's
  writes and are not a pre-mutation preflight. #2340 must still run the console
  and definition checks before setup mutates a workspace.
* The digest and environment are read from the definition *after* setup wrote
  it, and each identity is a stat. A same-UID writer could change either
  between setup's write and the recording; the result binds the state at
  recording time.
* The stat identity is not a security guarantee. A same-UID writer can rewrite a
  file in place to the same number of bytes and restore its ``st_mtime_ns``
  with ``os.utime``, so device/inode/size/mtime all still match. It is evidence
  that the same inode was named, not proof that its bytes are unchanged; the
  digest is the stronger check, and the definition digest is what binds the
  definition's contents.
* The digest check and launchd opening the definition, and the interpreter
  identity check and the later ``exec``, are separate steps. A same-UID writer
  can replace the bytes or interpreter between them. The checks narrow those
  windows and do not close them. launchd has no compare-and-bootstrap call.
* Creating the private scratch chain in the parent narrows the umask problem but
  is not current-run ownership: a same-UID writer can create an owner-only
  directory first. #2340 must still bind the chain to this run's evidence and
  re-check it immediately before the write it guards.

The exact launchctl absence and ``print-disabled`` output shapes were recorded
from read-only probes on one current macOS. They are pinned and fail closed,
but have not been validated against every macOS release; #2340 must do that
before native activation is wired in.

macOS activation bootstraps only a job proven absent in this user's domain and
refuses an explicit disabled override without changing it.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable
from xml.parsers.expat import ExpatError

from ciao import installer_handoff
from ciao.os_support.definition import (
    MAX_DEFINITION_BYTES,
    DefinitionRead,
    DefinitionReadError,
    read_bounded_file,
    read_definition,
)
from ciao.os_support.private import (
    descriptor_acl_grants_another_account,
    is_owned_descriptor,
    is_owned_path,
    is_private_descriptor_strict,
    is_private_strict,
    make_private_dir,
    open_private,
)

#: The native schema. Schema 1 is the legacy, digest-unbound document and is
#: never read here.
SCHEMA_VERSION = 2
#: The native result is larger than the legacy one because it carries the
#: digest, both file identities and the environment. It stays bounded.
MAX_RESULT_BYTES = 32 * 1024
#: The native handoff currently exists for macOS only. Windows task XML has no
#: port and its registration path forces an overwrite, so it cannot bind the
#: same facts or guarantee fresh-only activation; the legacy installer remains
#: the Windows path until a native loader exists (#2340).
NATIVE_BACKENDS = ("macos",)
_DEFINITION_DIGEST = re.compile(r"[0-9a-f]{64}")
#: The host's own parser accepts only these interpreter basenames. A direct
#: ``ciao`` console whose shebang names anything else is a wrapper or a binary
#: whose real interpreter cannot be established from the file, and is refused
#: rather than recorded as if it were Python.
_PYTHON_INTERPRETER = re.compile(r"^python(?:3(?:\.\d+)?)?$")


class NativeHandoffError(RuntimeError):
    """The native handoff result is absent, stale, malformed, or does not verify."""


@dataclass(frozen=True)
class InterpreterIdentity:
    """The file identity of an executed file, as setup found it.

    The path alone is not the program: a different file at the same path would
    pass every name comparison. Device and inode catch a replacement, size
    catches a rewrite that changes length, and the nanosecond mtime catches an
    edit that keeps the size.

    This is a stat, not a hash, and it is **not** a security guarantee. A
    same-UID writer can open the file, rewrite the same number of bytes in
    place and restore ``st_mtime_ns`` with ``os.utime``; the recorded identity
    would then still match while the bytes are different. It is evidence that
    the path named the same inode at recording and activation time, not proof
    that the contents are unchanged. The definition digest, which hashes the
    bytes, is the stronger check; both still have the check-then-open/exec
    window described in the module docstring.
    """

    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ExecutedJob:
    """The program a definition executes and the interpreter it actually runs.

    They are the same file for the direct Python shape. A direct ``ciao``
    console is a script: ``program`` is that script and
    ``interpreter`` is the absolute interpreter on its shebang line. A hosted
    definition executes the host and runs the served Python, so the two differ.
    """

    program: str
    interpreter: str


@dataclass(frozen=True)
class NativeHandoffResult:
    run_id: str
    startup_id: str
    workspace: Path
    service_backend: str
    definition_path: Path
    definition_sha256: str
    engine_program: str
    engine_program_identity: InterpreterIdentity
    engine_python: str
    engine_python_identity: InterpreterIdentity
    environment: dict[str, str]
    port: int


def scratch_root() -> Path:
    """Where an installer bootstrap keeps its private state.

    Deliberately the legacy module's answer: the two generations write the same
    one file, and adding a second path would make one of them invisible. A home
    directory that cannot be resolved is a native refusal, not a bare
    ``RuntimeError`` from ``Path.home``.
    """
    try:
        return installer_handoff.scratch_root()
    except (OSError, RuntimeError) as exc:
        raise NativeHandoffError(
            f"Could not determine the installer scratch directory: {exc}"
        ) from exc


def result_path() -> Path:
    try:
        return installer_handoff.result_path()
    except (OSError, RuntimeError) as exc:
        raise NativeHandoffError(
            f"Could not determine the installer handoff result path: {exc}"
        ) from exc


def _definition_name() -> str:
    from ciao import macos_service

    return f"{macos_service.SERVER_LABEL}.plist"


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _strict_regular_file(path: Path, *, label: str) -> None:
    """Require an existing regular file; a symlink is not one."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        raise NativeHandoffError(f"{label} is missing: {path}") from None
    except OSError as exc:
        raise NativeHandoffError(f"Could not inspect {label} {path}: {exc}") from exc
    if not stat.S_ISREG(status.st_mode):
        raise NativeHandoffError(f"{label} is not a regular file: {path}")


def _home_directory() -> Path:
    """``Path.home()`` as a native refusal when the home directory is unknown.

    ``Path.home`` raises ``RuntimeError`` when ``~`` cannot be expanded. That is
    a fact about this machine, like any other missing prerequisite, so it is
    reported as :class:`NativeHandoffError` rather than escaping as an unrelated
    exception type.
    """
    try:
        return Path.home()
    except (OSError, RuntimeError) as exc:
        raise NativeHandoffError(
            f"Could not determine the home directory: {exc}"
        ) from exc


def _scratch_components() -> tuple[tuple[Path, str, bool], ...]:
    """The directories that decide whether the scratch path is safe.

    The standard location validates the home directory and ``~/.ciao`` as well
    as the scratch directory itself. A caller-provided root (tests, or a future
    override) is validated from its parent down, so an arbitrary world-writable
    parent is refused rather than skipped.

    The home directory is never created here. ``~/.ciao`` and the scratch
    directory itself may be created when recording, and every component is
    checked after creation as well, so a pre-existing directory that is a
    symlink, foreign-owned, writable by group or others, or (for the scratch
    directory itself) not exactly ``0o700`` is refused rather than repaired.
    """
    home = _home_directory()
    scratch = scratch_root()
    if scratch == home / ".ciao" / "installer-bootstrap":
        return (
            (home, "the home directory", False),
            (home / ".ciao", "the installer scratch parent", True),
            (scratch, "installer scratch directory", True),
        )
    return (
        (scratch.parent, "the installer scratch parent", True),
        (scratch, "installer scratch directory", True),
    )


def _open_directory_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise NativeHandoffError(f"Could not open {path}: {exc}") from exc


def _require_private_component(path: Path, *, label: str, leaf: bool, create: bool) -> None:
    """Require one existing scratch component, or create it privately.

    An existing directory is judged, never chmod'ed: changing a user's
    pre-existing ``~/.ciao`` just to make the check pass would hide state this
    code is supposed to refuse. A missing directory is created ``0o700`` and
    then checked through an opened, no-follow descriptor; the inode compared is
    the one that was inspected, which narrows (but cannot close) a same-UID
    replacement race. On Windows, where the native path is refused before
    recording anyway, a freshly created directory is given its protected DACL
    and the leaf is judged from its owner and DACL instead of POSIX mode bits.
    """
    created = False
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise NativeHandoffError(f"{label} is missing: {path}") from None
        try:
            os.mkdir(path, 0o700)
            created = True
        except FileExistsError:
            pass  # a concurrent setup created it; it is judged below
        except OSError as exc:
            raise NativeHandoffError(
                f"Could not create {label} {path}: {exc}"
            ) from exc
        if created and os.name == "nt":
            try:
                make_private_dir(path)
            except OSError as exc:
                raise NativeHandoffError(
                    f"Could not make {label} private {path}: {exc}"
                ) from exc
        try:
            status = os.lstat(path)
        except OSError as exc:
            raise NativeHandoffError(
                f"Could not inspect {label} {path}: {exc}"
            ) from exc
    except OSError as exc:
        raise NativeHandoffError(f"Could not inspect {label} {path}: {exc}") from exc
    if stat.S_ISLNK(status.st_mode):
        raise NativeHandoffError(f"{label} is a symlink: {path}")
    if not stat.S_ISDIR(status.st_mode):
        raise NativeHandoffError(f"{label} is not a directory: {path}")
    if os.name == "nt":
        try:
            owned = is_owned_path(path)
            private = is_private_strict(path) if leaf else True
        except OSError as exc:
            raise NativeHandoffError(
                f"Could not inspect {label} {path}: {exc}"
            ) from exc
        if not owned:
            raise NativeHandoffError(
                f"{label} is not owned by the current user: {path}"
            )
        if not private:
            raise NativeHandoffError(
                f"{label} is not owner-only: {path}"
            )
        return
    descriptor = _open_directory_no_follow(path)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise NativeHandoffError(
                f"{label} changed while it was being checked: {path}"
            )
        try:
            owned = is_owned_descriptor(descriptor)
            acl_grant = descriptor_acl_grants_another_account(descriptor)
        except OSError as exc:
            raise NativeHandoffError(
                f"Could not inspect {label} {path}: {exc}"
            ) from exc
        if not owned:
            raise NativeHandoffError(
                f"{label} is not owned by the current user: {path}"
            )
        if acl_grant:
            raise NativeHandoffError(
                f"{label} grants access to another account through its ACL: {path}"
            )
        mode = stat.S_IMODE(opened.st_mode)
        if leaf:
            if mode != 0o700:
                raise NativeHandoffError(
                    f"{label} is not owner-only ({mode:04o}): {path}"
                )
        elif mode & 0o022:
            raise NativeHandoffError(
                f"{label} is writable by group or others ({mode:04o}): {path}"
            )
    finally:
        os.close(descriptor)


def _require_private_scratch(*, create: bool) -> None:
    """Validate every component of the scratch path, or create fresh ones.

    A reader uses ``create=False`` and refuses a missing scratch directory. A
    writer may create ``~/.ciao`` and the scratch directory itself, but still
    judges each one after creation and never alters an existing directory.
    """
    for path, label, may_create in _scratch_components():
        _require_private_component(
            path,
            label=label,
            leaf=path == scratch_root(),
            create=create and may_create,
        )


def prepare_bootstrap_scratch() -> Path:
    """Prepare the private scratch chain before a bootstrap child starts.

    This is the native installer parent's narrow preparation step. It creates
    the missing ``~/.ciao`` and ``~/.ciao/installer-bootstrap`` directories
    ``0o700`` and returns the scratch directory. The home directory is never
    created or changed.

    It exists because the bootstrap child cannot create the chain privately
    itself: ``ciao run --installer-bootstrap`` creates its ``launcher`` folder and
    ``CiaoConfig.from_env`` creates ``root``, ``launcher`` and ``runtime``
    with ``mkdir(parents=True, exist_ok=True)``. On a missing chain that makes
    ``~/.ciao/installer-bootstrap`` at the process umask, normally ``0o755``,
    and :func:`record_setup_completion` then refuses to write its result into
    it. Preparing the chain first means the child's ``exist_ok`` calls find the
    scratch directory already present and leave its mode alone; the child and
    the legacy config are not changed.

    An existing component is judged and never chmod'ed. A symlinked,
    foreign-owned or group/other-writable ``~/.ciao``, a scratch directory that
    is not exactly ``0o700``, or a non-directory at either name raises
    :class:`NativeHandoffError` and leaves the path as it was found. A
    pre-existing shared ``~/.ciao`` that is merely ``0o755`` is accepted, as it
    is everywhere else in this module.

    This function mutates the filesystem, so it must only be called after the
    installer parent's read-only fresh-install preflight (no installation
    evidence, no concurrent host lock) and before spawning the bootstrap child
    — never before the preflight and never to repair an existing installation.
    A freshly created private directory is not proof that this run created it: a
    same-UID writer can create an owner-only name first, so #2340 still has to
    establish current-run ownership from its own run-bound evidence. Nothing
    calls this in production in this issue; #2340 wires it.
    """
    _require_private_scratch(create=True)
    return scratch_root()


def require_bootstrap_scratch() -> Path:
    """Validate the private scratch chain read-only and return its directory.

    The native bootstrap child uses this before it relies on
    ``~/.ciao/installer-bootstrap/handoff.json``: the chain has to be the one
    the parent prepared, so a missing, symlinked, foreign-owned,
    group/other-writable or non-``0o700`` scratch directory is a refusal. Unlike
    :func:`prepare_bootstrap_scratch` this never creates or chmods anything.
    """
    _require_private_scratch(create=False)
    return scratch_root()


def _require_private_result(path: Path) -> None:
    """The result must sit in the validated, private scratch directory.

    The result's own owner-only requirement is not checked here:
    :func:`read_result` reads through the shared bounded reader with
    ``require_private_owner=True``, which answers it from the opened descriptor.
    A pathname check would describe whichever inode happened to be at the name
    when it ran, and the read could then be of another one. The directory chain
    is checked separately because the result cannot be trusted to be in a
    private directory just because its own mode is right.
    """
    _require_private_scratch(create=False)
    directory = scratch_root()
    try:
        parent_is_scratch = (
            path.parent.resolve() == directory.expanduser().resolve()
        )
    except (OSError, ValueError) as exc:
        raise NativeHandoffError(
            f"Could not resolve the installer scratch directory: {exc}"
        ) from exc
    if not parent_is_scratch:
        raise NativeHandoffError(
            f"installer handoff result is not in the scratch directory: {path}"
        )


def _resolved(value: str, *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise NativeHandoffError(f"Could not resolve {label}: {exc}") from exc


def _no_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise NativeHandoffError(
                f"installer handoff result repeats the field {key!r}"
            )
        payload[key] = value
    return payload


def _require_native_backend(name: str) -> None:
    """Refuse a backend this native module cannot verify end to end.

    Windows is named separately because it is a supported *legacy* backend: the
    native path is not silently unavailable there, it is deliberately absent
    until a loader can bind the task bytes, the port and fresh-only registration.
    """
    if name == "windows":
        raise NativeHandoffError(
            "the native installer handoff does not implement Windows; Windows "
            "remains on the legacy installer handoff"
        )
    if name not in NATIVE_BACKENDS:
        raise NativeHandoffError(
            f"the native installer handoff does not support {name}"
        )


def _current_backend_name() -> str:
    from ciao import service_backend

    try:
        name = service_backend.current_backend().name
    except service_backend.UnsupportedPlatformError as exc:
        raise NativeHandoffError(str(exc)) from exc
    _require_native_backend(name)
    return name


def _definition_from_written(written: Iterable[Path | str]) -> Path:
    name = _definition_name()
    matches = [Path(path) for path in written if Path(path).name == name]
    if len(matches) != 1:
        raise NativeHandoffError(
            "setup did not report exactly one service definition "
            f"({name}); found {len(matches)}"
        )
    return matches[0]


def _read_definition(definition: Path) -> DefinitionRead:
    """Read exactly one bounded regular file, or refuse.

    The read is :func:`ciao.os_support.definition.read_definition`, shared with
    :func:`ciao.macos_service._definition_digest` so the two callers cannot
    drift on binary mode, the byte count, or the inode/mtime rechecks. Its
    failures are re-raised as the native handoff's own error.
    """
    try:
        return read_definition(definition, max_bytes=MAX_DEFINITION_BYTES)
    except DefinitionReadError as exc:
        raise NativeHandoffError(str(exc)) from None


def _absolute_program(value: object, *, label: str) -> str:
    """An absolute POSIX program path, compared exactly as written.

    The native path implements macOS only, so a Windows-spelled path is refused
    here rather than recorded and discovered at activation on a host where it
    names nothing.
    """
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise NativeHandoffError(f"{label} is not a usable program path")
    if not PurePosixPath(value).is_absolute():
        raise NativeHandoffError(f"{label} is not absolute: {value}")
    return value


def _interpreter_identity(
    program: str, *, label: str = "the engine interpreter"
) -> InterpreterIdentity:
    """The identity of the regular file at ``program``, or a refusal.

    Follows symlinks on purpose: a virtualenv interpreter is usually a link to
    the base build, and replacing either one changes what runs. A missing path,
    a directory, a FIFO or any unreadable metadata is a refusal. ``label`` names
    which file in the message: the executed program and the interpreter are
    separate files in the console shape and have separate identities.
    """
    try:
        status = os.stat(program)
    except FileNotFoundError:
        raise NativeHandoffError(f"{label} is missing: {program}") from None
    except OSError as exc:
        raise NativeHandoffError(
            f"Could not inspect {label} {program}: {exc}"
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        raise NativeHandoffError(f"{label} is not a regular file: {program}")
    return InterpreterIdentity(
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        mtime_ns=status.st_mtime_ns,
    )


def _identity_payload(identity: InterpreterIdentity) -> dict[str, int]:
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def _read_identity(value: object, *, label: str) -> InterpreterIdentity:
    """A strictly shaped four-integer identity block, or a refusal."""
    if not isinstance(value, dict) or set(value) != {
        "device",
        "inode",
        "size",
        "mtime_ns",
    }:
        raise NativeHandoffError(
            f"installer handoff result has no usable {label}"
        )
    checked: dict[str, int] = {}
    for key in ("device", "inode", "size", "mtime_ns"):
        field = value[key]
        if (
            not isinstance(field, int)
            or isinstance(field, bool)
            or field < 0
        ):
            raise NativeHandoffError(
                f"installer handoff result has no usable {label}"
            )
        checked[str(key)] = field
    return InterpreterIdentity(**checked)


def _environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise NativeHandoffError("installer handoff result has no usable environment")
    return {str(key): str(item) for key, item in value.items()}


def _parse_macos_definition(definition: Path, data: bytes) -> dict[str, object]:
    """The parsed plist of exactly the bytes that produced the digest."""
    try:
        parsed = plistlib.loads(data)
    except (ValueError, plistlib.InvalidFileException, ExpatError) as exc:
        raise NativeHandoffError(
            f"Could not read the service definition: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise NativeHandoffError("the service definition is not a plist dictionary")
    return parsed


def _plist_workspace(parsed: dict[str, object]) -> Path:
    """The destination a parsed service definition names."""
    environment = parsed.get("EnvironmentVariables")
    raw = environment.get("CIAO_WORKSPACE") if isinstance(environment, dict) else None
    if not raw:
        raise NativeHandoffError("the service definition does not name a workspace")
    named = Path(str(raw)).expanduser()
    if not named.is_absolute():
        raise NativeHandoffError("the service definition names a relative workspace")
    return _resolved(str(named), label="the definition's workspace")


def _definition_environment(parsed: dict[str, object]) -> dict[str, str]:
    """The ``EnvironmentVariables`` mapping of the parsed definition, or ``{}``.

    The dictionary passed in is the parse of exactly the bytes that produced the
    recorded digest; no path is read a second time.
    """
    environment = parsed.get("EnvironmentVariables")
    if environment is None:
        return {}
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise NativeHandoffError(
            "the service definition's EnvironmentVariables is not a string mapping"
        )
    return {str(key): str(value) for key, value in environment.items()}


def resolve_console_interpreter(program: str) -> str:
    """The absolute interpreter a direct ``ciao`` console script runs, or refuse.

    A POSIX ``#!`` line is what the kernel reads when it executes the console
    script. Only the single, absolute, Python-named form a normal console-script
    install writes is accepted: ``#!/path/to/python3`` (or ``python3.N``).
    ``#!/usr/bin/env python3`` is refused because the interpreter it picks
    depends on the activating process's ``PATH``, and the handoff could not bind
    a truthful identity for that file. A shebang with an argument, a relative
    path, no shebang, or a non-Python program is refused as an unsupported
    console.

    This function is pure apart from reading the script's first line. #2340
    must call it *before* the native path lets setup mutate a workspace, so an
    unsupported console is a refusal at the start rather than a result that
    activation cannot verify. :func:`record_setup_completion` calls it after
    setup has written; that refuses the result but cannot undo the mutation.
    """
    _absolute_program(program, label="the ciao console")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = os.open(program, flags)
    except OSError as exc:
        raise NativeHandoffError(
            f"Could not read the ciao console {program}: {exc}"
        ) from exc
    first = b""
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise NativeHandoffError(
                f"the ciao console is not a regular file: {program}"
            )
        while len(first) < 4096 and b"\n" not in first:
            chunk = os.read(descriptor, 4096 - len(first))
            if not chunk:
                break
            first += chunk
    except NativeHandoffError:
        raise
    except OSError as exc:
        raise NativeHandoffError(
            f"Could not read the ciao console {program}: {exc}"
        ) from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if b"\n" not in first and len(first) >= 4096:
        raise NativeHandoffError(
            f"the ciao console has no supported shebang: {program}"
        )
    line = first.partition(b"\n")[0].rstrip(b"\r")
    if not line.startswith(b"#!"):
        raise NativeHandoffError(
            f"the ciao console has no supported shebang: {program}"
        )
    parts = line[2:].split()
    if len(parts) != 1:
        raise NativeHandoffError(
            "the ciao console's shebang must name one absolute interpreter, "
            f"not {line[2:].decode('utf-8', 'replace').strip()!r}: {program}"
        )
    interpreter = os.fsdecode(parts[0])
    if not PurePosixPath(interpreter).is_absolute():
        raise NativeHandoffError(
            f"the ciao console's shebang interpreter is not absolute: {interpreter!r}"
        )
    if _PYTHON_INTERPRETER.fullmatch(PurePosixPath(interpreter).name) is None:
        raise NativeHandoffError(
            "the ciao console's shebang interpreter is not a Python executable: "
            f"{interpreter}"
        )
    return interpreter


def _recorded_job(parsed: dict[str, object], chosen: str) -> ExecutedJob:
    """The program and interpreter to record for the job the definition starts.

    The definition is the authority: setup's resolved engine is rendered into
    either a direct program or a hosted ``serve --python`` pair, and only the
    parsed bytes that produced the digest tell the truth. A direct ``ciao``
    console is a script, so its program is the script and its interpreter is
    the shebang target; recording the script as ``engine_python`` made the
    identity describe the launcher and not the Python that runs the engine. The
    chosen engine still has to agree with the definition, and an unsupported
    console is refused here rather than blessed because a later digest check
    binds the wrong file.
    """
    from ciao import macos_service, server_host

    try:
        command = server_host.parse_service_command(parsed.get("ProgramArguments"))
    except server_host.ServerHostError as exc:
        raise NativeHandoffError(
            f"the service definition's program is not an approved engine "
            f"command: {exc}"
        ) from exc
    if command.mode == "hosted":
        mapped = macos_service.host_interpreter_for(chosen)
        if command.python != mapped:
            raise NativeHandoffError(
                f"the hosted service definition serves {command.python}, not the "
                f"interpreter setup recorded ({mapped})"
            )
        return ExecutedJob(command.program, command.python)
    if command.program != chosen:
        raise NativeHandoffError(
            f"the service definition runs {command.program}, not the engine "
            f"setup chose ({chosen})"
        )
    if command.python:
        return ExecutedJob(command.program, command.python)
    return ExecutedJob(command.program, resolve_console_interpreter(command.program))


def record_setup_completion(
    *,
    run_id: str,
    startup_id: str,
    workspace: Path | str,
    written: Iterable[Path | str],
    port: int,
    engine_python: str,
) -> Path:
    """Write the native installer handoff result after a successful setup.

    The result is bound to the exact bytes of the service definition this setup
    wrote, to the program the definition actually executes, to the interpreter
    that program actually runs, to both files' identities, and to the
    environment block setup wrote, so activation can refuse anything that
    changed afterwards.

    Those bytes are also the preflight: the definition's workspace, its
    ``CIAO_PORT`` and the complete job semantics launchd would run are checked
    against setup's arguments and the template before a result exists. A
    definition pointing at another workspace, a different port, or an
    unapproved program, environment, key set or resource limit is a refusal and
    leaves no result behind.

    ``engine_python`` is setup's resolved engine: ``python_path``, then
    ``CIAO_ENGINE_PATH``, then ``sys.executable`` in
    :func:`ciao.cli.setup_workspace`; a direct ``ciao`` console is a script
    whose shebang names the interpreter. The definition is the authority for
    what really runs, and :func:`resolve_console_interpreter` refuses an
    unsupported console instead of recording the launcher as if it were the
    Python that runs the engine.

    This runs *after* setup wrote the definition and mutated the workspace. It
    can refuse the result, but it cannot undo a write; it is not #2340's
    pre-mutation preflight and no non-mutation is claimed here.

    Before writing, the scratch chain (the home directory, ``~/.ciao`` and
    ``~/.ciao/installer-bootstrap``) is validated: no component may be a
    symlink, every component must belong to the effective user, an ancestor may
    not be group/other-writable, and the scratch directory itself must be
    ``0o700``. Missing ``~/.ciao`` and scratch directories are created privately;
    a pre-existing directory is never chmod'ed, and an unsafe one is refused
    instead. The checks are bound to opened, no-follow descriptors and compare
    the inspected inode, which narrows but does not close a same-UID race
    between validation and use.

    Creating them here cannot rescue a bootstrap child that already made them
    ``0o755``: the child's ``launcher.mkdir(parents=True, exist_ok=True)`` and
    ``CiaoConfig.from_env(installer_bootstrap=True)`` run before setup records
    anything. A native run therefore needs
    :func:`prepare_bootstrap_scratch` in the parent, after the fresh-install
    preflight and before the child starts; see the module docstring.

    Recording reads the definition *after* setup wrote it and stats both files
    rather than proving them: a same-UID writer could change either between
    setup's write and this call, and a same-size rewrite with a restored mtime
    can even defeat the stat identity. What is bound is the state at recording
    time; the window before recording is the same-UID bootstrap race the module
    docstring acknowledges, and it is not closed here.
    """
    if not _is_uuid(run_id):
        raise NativeHandoffError("installer run id is not a valid uuid")
    if not isinstance(startup_id, str) or not startup_id.strip():
        raise NativeHandoffError("this server did not record a startup id")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise NativeHandoffError(f"invalid port for the handoff result: {port!r}")
    backend_name = _current_backend_name()
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise NativeHandoffError(f"the chosen workspace does not exist: {root}")
    definition = _definition_from_written(written)
    _strict_regular_file(definition, label="service definition")
    # One read supplies the digest and every fact judged below: the workspace,
    # the port, the environment and the job all describe these bytes, not
    # successive states of the path.
    definition_read = _read_definition(definition)
    parsed = _parse_macos_definition(definition, definition_read.data)
    chosen = _absolute_program(
        engine_python, label="the chosen engine interpreter"
    )
    job = _recorded_job(parsed, chosen)
    served = _plist_workspace(parsed)
    if served != root:
        raise NativeHandoffError(
            f"the service definition serves {served}, not the chosen "
            f"workspace {root}"
        )
    environment = _definition_environment(parsed)
    recorded_port = environment.get("CIAO_PORT")
    if recorded_port != str(port):
        raise NativeHandoffError(
            f"the service definition's CIAO_PORT is {recorded_port!r}, not the "
            f"port setup chose ({port})"
        )
    program_identity = _interpreter_identity(
        job.program, label="the executed program"
    )
    interpreter_identity = _interpreter_identity(
        job.interpreter, label="the engine interpreter"
    )
    candidate = NativeHandoffResult(
        run_id=run_id,
        startup_id=startup_id,
        workspace=root,
        service_backend=backend_name,
        definition_path=definition.expanduser().resolve(),
        definition_sha256=definition_read.sha256,
        engine_program=job.program,
        engine_program_identity=program_identity,
        engine_python=job.interpreter,
        engine_python_identity=interpreter_identity,
        environment=environment,
        port=port,
    )
    _require_approved_macos_job(parsed, candidate)

    payload = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "startup_id": startup_id,
        "workspace": str(root),
        "service_backend": backend_name,
        "definition_path": str(candidate.definition_path),
        "definition_sha256": definition_read.sha256,
        "engine_program": job.program,
        "engine_program_identity": _identity_payload(program_identity),
        "engine_python": job.interpreter,
        "engine_python_identity": _identity_payload(interpreter_identity),
        "environment": environment,
        "port": port,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise NativeHandoffError(
            "the installer handoff result would exceed its size limit"
        )

    _require_private_scratch(create=True)
    target = result_path()
    if target.is_symlink():
        raise NativeHandoffError(f"installer handoff result is a symlink: {target}")
    # One freshly created, owner-only name per write: a predictable temporary
    # path could already be a hardlink to a file outside the scratch directory.
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    created = False
    try:
        descriptor = open_private(temporary, os.O_WRONLY | os.O_EXCL)
        created = True
        try:
            if not is_private_descriptor_strict(descriptor):
                raise NativeHandoffError(
                    "the installer handoff result was not created with private "
                    f"ownership: {temporary}"
                )
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor = None  # fdopen owns it now
                handle.write(text)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        os.replace(temporary, target)
    except OSError as exc:
        # Only remove the name if this invocation created it. A collision on
        # the unpredictable name means some other writer's file is there and
        # must be left alone.
        if created:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise NativeHandoffError(
            f"Could not write the installer handoff result: {exc}"
        ) from exc
    except BaseException:
        if created:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise
    return target


def _field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise NativeHandoffError(f"installer handoff result has no usable {key}")
    return value


def read_result(
    *, expected_run_id: str, path: Path | None = None
) -> NativeHandoffResult:
    """Read and validate a schema-2 result; anything unexpected is a refusal.

    The document is read through
    :func:`ciao.os_support.definition.read_bounded_file`, the same bounded,
    no-follow, non-blocking descriptor read as a service definition: a FIFO or
    symlink where the result belongs cannot block the installer, a replacement
    between the check and the open is refused by the inode comparison, an
    oversize or partially read file is a refusal, and privacy is checked on the
    *opened* descriptor with the strict verifier (owner, exact mode, and on
    macOS no granting extended ACL) rather than the pathname. Only then are
    the bytes decoded as strict UTF-8 and parsed; malformed UTF-8 is a refusal,
    never a replacement-decoded document.
    """
    target = result_path() if path is None else path
    _require_private_result(target)
    try:
        read = read_bounded_file(
            target,
            max_bytes=MAX_RESULT_BYTES,
            label="the installer handoff result",
            require_private=True,
            require_private_owner=True,
        )
    except DefinitionReadError as exc:
        raise NativeHandoffError(str(exc)) from None
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NativeHandoffError(
            f"the installer handoff result is not valid UTF-8: {exc}"
        ) from None
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_fields)
    except ValueError as exc:
        raise NativeHandoffError(
            "installer handoff result is not valid json"
        ) from exc
    if not isinstance(payload, dict):
        raise NativeHandoffError("installer handoff result is not a json object")
    schema = payload.get("schema")
    if type(schema) is int and schema == 1:
        raise NativeHandoffError(
            "schema 1 installer handoff results are not bound to a definition "
            "and are refused by the native path; re-run the installer"
        )
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise NativeHandoffError(
            f"unsupported installer handoff schema: {schema!r}"
        )
    if set(payload) != {
        "schema",
        "run_id",
        "startup_id",
        "workspace",
        "service_backend",
        "definition_path",
        "definition_sha256",
        "engine_program",
        "engine_program_identity",
        "engine_python",
        "engine_python_identity",
        "environment",
        "port",
    }:
        raise NativeHandoffError("installer handoff result has unexpected fields")
    run_id = _field(payload, "run_id")
    if not _is_uuid(run_id) or run_id != expected_run_id:
        raise NativeHandoffError(
            "installer handoff result belongs to a different installer run"
        )
    startup_id = _field(payload, "startup_id")
    backend_name = _field(payload, "service_backend")
    _require_native_backend(backend_name)
    workspace = Path(_field(payload, "workspace"))
    definition = Path(_field(payload, "definition_path"))
    if not workspace.is_absolute() or not definition.is_absolute():
        raise NativeHandoffError(
            "installer handoff result paths must be absolute"
        )
    definition_sha256 = _field(payload, "definition_sha256")
    if _DEFINITION_DIGEST.fullmatch(definition_sha256) is None:
        raise NativeHandoffError(
            "installer handoff result has no usable definition_sha256"
        )
    engine_program = _absolute_program(
        _field(payload, "engine_program"),
        label="the handoff's executed program",
    )
    engine_program_identity = _read_identity(
        payload.get("engine_program_identity"), label="engine_program_identity"
    )
    engine_python = _absolute_program(
        _field(payload, "engine_python"),
        label="the handoff's engine interpreter",
    )
    engine_python_identity = _read_identity(
        payload.get("engine_python_identity"), label="engine_python_identity"
    )
    environment = _environment(payload.get("environment"))
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise NativeHandoffError(f"invalid port in the handoff result: {port!r}")
    return NativeHandoffResult(
        run_id=run_id,
        startup_id=startup_id,
        workspace=_resolved(str(workspace), label="the handoff workspace"),
        service_backend=backend_name,
        definition_path=definition.expanduser(),
        definition_sha256=definition_sha256,
        engine_program=engine_program,
        engine_program_identity=engine_program_identity,
        engine_python=engine_python,
        engine_python_identity=engine_python_identity,
        environment=environment,
        port=port,
    )


#: The complete set of keys setup's template writes into the server plist.
#: Anything else — ``Program``, ``BundleProgram``, ``WatchPaths``,
#: ``StartInterval`` — changes what launchd executes or when, and is refused
#: even when the digest matches. ``StandardOutPath``/``StandardErrorPath``,
#: ``SoftResourceLimits`` and ``HardResourceLimits`` are in the set because the
#: packaged template always writes them; their values are pinned separately.
_ALLOWED_DEFINITION_KEYS = frozenset(
    {
        "AssociatedBundleIdentifiers",
        "EnvironmentVariables",
        "ExitTimeOut",
        "HardResourceLimits",
        "KeepAlive",
        "Label",
        "ProgramArguments",
        "RunAtLoad",
        "SoftResourceLimits",
        "StandardErrorPath",
        "StandardOutPath",
        "WorkingDirectory",
    }
)

#: The environment keys setup's template writes. A ``PYTHONPATH`` (or any other
#: interpreter-affecting variable) is not one of them.
_ALLOWED_ENVIRONMENT_KEYS = frozenset(
    {"CIAO_WORKSPACE", "CIAO_RUNTIME_ROOT", "CIAO_PORT", "PATH"}
)


def _require_number_of_files(parsed: dict[str, object], key: str) -> None:
    """A resource-limit dictionary is exactly ``{"NumberOfFiles": target}``.

    The packaged template always writes both limits, so absence is not a
    different valid policy: it is a key setup did not write.
    """
    from ciao.os_support.limits import SERVER_NOFILE_TARGET

    limits = parsed.get(key)
    if limits is None:
        raise NativeHandoffError(
            f"the service definition does not carry the setup resource limit ({key})"
        )
    if (
        not isinstance(limits, dict)
        or set(limits) != {"NumberOfFiles"}
        or not isinstance(limits["NumberOfFiles"], int)
        or isinstance(limits["NumberOfFiles"], bool)
        or limits["NumberOfFiles"] != SERVER_NOFILE_TARGET
    ):
        raise NativeHandoffError(
            f"the service definition changes the job's resource limits ({key})"
        )


def _require_setup_logs(
    parsed: dict[str, object], result: NativeHandoffResult
) -> None:
    """``StandardOutPath``/``StandardErrorPath`` are exactly the workspace logs.

    Both keys are always rendered by the template, so a definition that omits
    one is not the file setup wrote even when the remaining keys look right.
    """
    for key, name in (
        ("StandardOutPath", "ciao.stdout.log"),
        ("StandardErrorPath", "ciao.stderr.log"),
    ):
        value = parsed.get(key)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise NativeHandoffError(
                f"the service definition's {key} is not an absolute path"
            )
        try:
            actual = Path(value).expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise NativeHandoffError(
                f"Could not resolve the service definition's {key}: {exc}"
            ) from exc
        expected = (result.workspace / ".runtime" / name).resolve()
        if actual != expected:
            raise NativeHandoffError(
                f"the service definition writes {key} to {actual}, not the "
                f"workspace log {expected}"
            )


def _require_approved_environment(
    parsed: dict[str, object], result: NativeHandoffResult
) -> None:
    """The definition's environment must be exactly the one setup recorded.

    The four keys the template always writes must all be present, values must
    equal the recorded block, and no other key is allowed. No fixed PATH
    allowlist is applied: the definition's bytes are already bound to the digest
    setup recorded, so the recorded environment is not an independent
    authority; a policy derived from paths at activation time would silently
    narrow the legacy wizard's persisted development PATH (and a directory
    allowlist that includes the approving process's own PATH can be widened by
    anything that process inherited). A bounded service PATH belongs to native
    install mode, where the writer can carry its own explicit signal.
    """
    environment = parsed.get("EnvironmentVariables")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise NativeHandoffError(
            "the service definition's EnvironmentVariables is not a string mapping"
        )
    typed = {str(key): str(value) for key, value in environment.items()}
    missing = sorted(_ALLOWED_ENVIRONMENT_KEYS - set(typed))
    if missing:
        raise NativeHandoffError(
            "the service definition does not carry the environment keys setup "
            "writes: " + ", ".join(missing)
        )
    unexpected = sorted(set(typed) - _ALLOWED_ENVIRONMENT_KEYS)
    if unexpected:
        raise NativeHandoffError(
            "the service definition sets environment keys this installer never "
            "writes: " + ", ".join(unexpected)
        )
    if typed != result.environment:
        raise NativeHandoffError(
            "the service definition's environment changed after setup recorded it"
        )
    named = typed.get("CIAO_WORKSPACE")
    if named is None:
        raise NativeHandoffError(
            "the service definition does not name a workspace"
        )
    try:
        workspace = Path(named).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise NativeHandoffError(
            f"Could not resolve the definition's CIAO_WORKSPACE: {exc}"
        ) from exc
    if workspace != result.workspace:
        raise NativeHandoffError(
            f"the service definition's CIAO_WORKSPACE is {workspace}, not "
            f"{result.workspace}"
        )
    runtime_root = typed.get("CIAO_RUNTIME_ROOT")
    if runtime_root is None:
        raise NativeHandoffError(
            "the service definition does not name a runtime root"
        )
    try:
        runtime = Path(runtime_root).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise NativeHandoffError(
            f"Could not resolve the definition's CIAO_RUNTIME_ROOT: {exc}"
        ) from exc
    if runtime != (result.workspace / ".runtime").resolve():
        raise NativeHandoffError(
            "the service definition's CIAO_RUNTIME_ROOT is not the "
            "workspace runtime"
        )
    port = typed.get("CIAO_PORT")
    if port is None:
        raise NativeHandoffError(
            "the service definition does not name the port setup recorded"
        )
    if port != str(result.port):
        raise NativeHandoffError(
            "the service definition's CIAO_PORT is not the port setup recorded"
        )


def _require_owned_host(program: str) -> None:
    """The definition's host must be the canonical, owned Ciaobot Server.app."""
    from ciao import server_host

    bundle = server_host.default_bundle_path()
    if bundle.is_symlink():
        raise NativeHandoffError(f"the installed host path is a symlink: {bundle}")
    try:
        ownership = server_host.verify_owned_host(bundle)
    except (server_host.ServerHostError, OSError) as exc:
        raise NativeHandoffError(
            "the service definition's host is not this install's owned host: "
            f"{exc}"
        ) from exc
    executable = os.fspath(
        Path(ownership.bundle_path)
        / "Contents"
        / "MacOS"
        / server_host.EXECUTABLE_NAME
    )
    if program != executable:
        raise NativeHandoffError(
            f"the service definition runs the host {program}, not the installed "
            f"host {executable}"
        )


def _require_approved_macos_job(
    parsed: dict[str, object], result: NativeHandoffResult
) -> None:
    """Require the job launchd would run to be the one this setup wrote.

    The digest binds the bytes; this binds their meaning. A definition with the
    right digest still refuses if it carries any key outside the fixed set
    setup writes, omits a key the template always writes, carries a foreign
    label or association, an environment other than the recorded four-key
    block, ``RunAtLoad``/``KeepAlive`` other than true, a ``WorkingDirectory``
    other than the workspace, changed soft or hard resource limits, a
    standard-output or standard-error path that is not the workspace log, a
    direct ``ExitTimeOut`` (only the hosted template writes one), an unapproved
    argv, a host command other than ``serve``, a program other than the one
    recorded, an interpreter other than the one recorded, or a host that is
    not the canonical owned bundle.

    A direct ``ciao`` console is checked by reading the shebang target and
    requiring it to be the recorded interpreter; an unsupported console is a
    refusal here as well as at recording.

    The same function runs at recording, against the bytes about to be bound,
    and at activation, against the re-read live bytes. It takes a result rather
    than a path so both callers judge exactly the bytes that produced the digest
    they are comparing, never a second read of the definition.
    """
    from ciao import macos_service, server_host

    unexpected = sorted(set(parsed) - _ALLOWED_DEFINITION_KEYS)
    if unexpected:
        raise NativeHandoffError(
            "the service definition carries keys this installer never writes: "
            + ", ".join(unexpected)
        )
    label = parsed.get("Label")
    if label != macos_service.SERVER_LABEL:
        raise NativeHandoffError(
            f"the service definition's label is {label!r}, not "
            f"{macos_service.SERVER_LABEL!r}"
        )
    try:
        command = server_host.parse_service_command(parsed.get("ProgramArguments"))
    except server_host.ServerHostError as exc:
        raise NativeHandoffError(
            "the service definition's program is not an approved engine "
            f"command: {exc}"
        ) from exc
    hosted = command.mode == "hosted"
    expected_associated = (
        [server_host.BUNDLE_ID]
        if hosted
        else [macos_service.DESKTOP_BUNDLE_ID]
    )
    if parsed.get("AssociatedBundleIdentifiers") != expected_associated:
        raise NativeHandoffError(
            "the service definition's AssociatedBundleIdentifiers is not "
            f"{expected_associated!r}"
        )
    for key in ("RunAtLoad", "KeepAlive"):
        if parsed.get(key) is not True:
            raise NativeHandoffError(
                f"the service definition does not set {key} to true"
            )
    _require_number_of_files(parsed, "SoftResourceLimits")
    _require_number_of_files(parsed, "HardResourceLimits")
    _require_setup_logs(parsed, result)
    exit_timeout = parsed.get("ExitTimeOut")
    if hosted:
        if exit_timeout != server_host.EXIT_TIMEOUT_SECONDS:
            raise NativeHandoffError(
                "the hosted service definition does not carry the setup "
                "ExitTimeOut"
            )
    elif exit_timeout is not None:
        raise NativeHandoffError(
            "a direct service definition carries an ExitTimeOut setup never writes"
        )
    working = parsed.get("WorkingDirectory")
    if not isinstance(working, str):
        raise NativeHandoffError(
            "the service definition does not name its WorkingDirectory"
        )
    try:
        working_path = Path(working).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise NativeHandoffError(
            f"Could not resolve the definition's WorkingDirectory: {exc}"
        ) from exc
    if working_path != result.workspace:
        raise NativeHandoffError(
            f"the service definition runs in {working_path}, not "
            f"{result.workspace}"
        )
    _require_approved_environment(parsed, result)
    if hosted:
        if command.argv[1] != "serve":
            raise NativeHandoffError(
                "the service definition is not an approved `serve` host command"
            )
        if command.program != result.engine_program:
            raise NativeHandoffError(
                f"the service definition runs {command.program}, not the host "
                f"setup recorded ({result.engine_program})"
            )
        if command.python != result.engine_python:
            raise NativeHandoffError(
                f"the host is told to serve {command.python}, not the "
                f"interpreter setup recorded ({result.engine_python})"
            )
        _require_owned_host(command.program)
        return
    if command.program != result.engine_program:
        raise NativeHandoffError(
            f"the service definition runs {command.program}, not the program "
            f"setup recorded ({result.engine_program})"
        )
    interpreter = (
        command.python
        if command.python
        else resolve_console_interpreter(command.program)
    )
    if interpreter != result.engine_python:
        raise NativeHandoffError(
            f"the service definition runs {interpreter}, not the interpreter "
            f"setup recorded ({result.engine_python})"
        )


#: A completed workspace's legacy ``.env`` record is a few lines. The native
#: activation reads it through the shared bounded reader, so a FIFO or an
#: unbounded file where it belongs is a refusal instead of a hung installer.
MAX_WORKSPACE_ENV_BYTES = 64 * 1024


def _native_set_up_workspace(workspace: Path) -> bool:
    """Whether ``workspace`` is a completed setup, read without blocking.

    The same facts as :func:`ciao.setup_marker.is_set_up_workspace` — the
    workspace registry, or a legacy ``.env`` whose ``CIAO_WORKSPACES`` is
    non-empty — but the ``.env`` comes through the shared bounded reader. The
    legacy function keeps ``read_text`` for every existing caller; on this path
    a FIFO where ``.env`` belongs would block the installer's activation
    forever, which is not a refusal.

    A missing file is a definite "no"; a link, a special file, an oversize file,
    a read that does not cover the opened size, a replacement during the read,
    undecodable bytes or an unreadable path is :class:`NativeHandoffError`
    (uncertainty is refused, not reported as an incomplete workspace). The
    registry is required to be a regular file for the same reason, so a
    symlinked registry cannot stand in for the one setup wrote.
    """
    from ciao.setup_marker import RUNTIME_DIR_NAME, WORKSPACE_REGISTRY_FILENAME

    registry = workspace / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME
    try:
        registry_status = os.lstat(registry)
    except FileNotFoundError:
        registry_status = None
    except OSError as exc:
        raise NativeHandoffError(
            f"Could not inspect the workspace registry {registry}: {exc}"
        ) from exc
    if registry_status is not None:
        if not stat.S_ISREG(registry_status.st_mode):
            raise NativeHandoffError(
                f"the workspace registry is not a regular file: {registry}"
            )
        return True
    env_path = workspace / ".env"
    try:
        os.lstat(env_path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NativeHandoffError(
            f"Could not inspect the workspace .env {env_path}: {exc}"
        ) from exc
    try:
        read = read_bounded_file(
            env_path,
            max_bytes=MAX_WORKSPACE_ENV_BYTES,
            label="the workspace .env",
        )
    except DefinitionReadError as exc:
        raise NativeHandoffError(str(exc)) from None
    try:
        text = read.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NativeHandoffError(
            f"the workspace .env is not valid UTF-8: {exc}"
        ) from None
    from ciao.macos_service import parse_dotenv

    return bool(parse_dotenv(text).get("CIAO_WORKSPACES", "").strip())


def workspace_is_set_up(workspace: Path) -> bool:
    """The native bounded "is this already a Ciaobot workspace?" answer.

    The same facts as :func:`ciao.setup_marker.is_set_up_workspace` — the
    workspace registry, or a legacy ``.env`` whose ``CIAO_WORKSPACES`` is
    non-empty — through the bounded, non-blocking reader. A FIFO, symlink,
    special file, oversize file or unreadable path is
    :class:`NativeHandoffError`, never a hang and never "not set up".
    """
    return _native_set_up_workspace(workspace)


def activate(
    *,
    run_id: str,
    is_set_up: Callable[[Path], bool] | None = None,
) -> NativeHandoffResult:
    """Verify the native handoff result and start the service, or raise.

    Every fact the result carries is re-established here, including the digest
    of the live definition's bytes, the executed program and the interpreter it
    runs — at the same device, inode, size and mtime setup recorded — and the
    environment setup wrote. A ``ciao`` console is checked by reading its shebang
    target, so the interpreter identity is the Python that runs, not the script
    that launches it. Activation checks the digest before parsing, then parses
    those same bytes and requires the approved job semantics, then goes through
    :func:`ciao.macos_service.load_fresh_agent`, which bootstraps only a job
    that is proven absent and refuses a disabled override; a service that
    appeared after setup is refused, not unloaded or replaced.

    The native path implements macOS only. A result for any other backend,
    Windows included, is refused before it is read: this module has no loader
    that can bind a Windows task's semantics or promise fresh-only registration.

    The stat identities narrow the window; they do not authenticate the bytes.
    A same-UID writer can rewrite a file in place to the same size and restore
    its mtime, so identity equality is evidence, not proof.

    The caller is the installer parent, and it must already have observed the
    child exit 0; this function cannot see that. Nothing in this issue calls it.

    A ``ciao`` console's shebang target and the result document are read with
    the same bounded descriptor reader, and the completed-workspace check is
    :func:`_native_set_up_workspace`: the default is still the registry or the
    legacy ``CIAO_WORKSPACES`` ``.env`` record, but a FIFO or unreadable ``.env``
    is a refusal instead of a read that never returns. An injected ``is_set_up``
    replaces that check, as before.
    """
    from ciao import service_backend

    if not _is_uuid(run_id):
        raise NativeHandoffError("installer run id is not a valid uuid")
    try:
        backend = service_backend.current_backend()
    except service_backend.UnsupportedPlatformError as exc:
        raise NativeHandoffError(str(exc)) from exc
    _require_native_backend(backend.name)
    result = read_result(expected_run_id=run_id)
    if result.service_backend != backend.name:
        raise NativeHandoffError(
            f"the handoff result was written for {result.service_backend}, "
            f"not {backend.name}"
        )
    live_program_identity = _interpreter_identity(
        result.engine_program, label="the executed program"
    )
    if live_program_identity != result.engine_program_identity:
        raise NativeHandoffError(
            "the executed program changed after setup recorded it: "
            f"{result.engine_program}"
        )
    live_identity = _interpreter_identity(
        result.engine_python, label="the engine interpreter"
    )
    if live_identity != result.engine_python_identity:
        raise NativeHandoffError(
            "the engine interpreter changed after setup recorded it: "
            f"{result.engine_python}"
        )
    try:
        live_directory = backend.live_agents_dir()
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeHandoffError(
            f"Could not locate the live service definitions: {exc}"
        ) from exc
    expected = live_directory / _definition_name()
    try:
        recorded_is_live = result.definition_path.resolve() == expected.resolve()
    except (OSError, ValueError):
        recorded_is_live = False
    if not recorded_is_live:
        raise NativeHandoffError(
            f"the recorded definition {result.definition_path} is not the live one {expected}"
        )
    _strict_regular_file(expected, label="live service definition")
    checker = _native_set_up_workspace if is_set_up is None else is_set_up
    if not checker(result.workspace):
        raise NativeHandoffError(
            f"{result.workspace} is not a completed Ciaobot workspace"
        )
    # One read produces both the digest and the bytes that are judged: the
    # workspace, the digest and the job semantics all describe the same file,
    # not successive states of the path. The digest is checked before parsing,
    # so a changed file is refused without its bytes being interpreted.
    definition = _read_definition(expected)
    if definition.sha256 != result.definition_sha256:
        raise NativeHandoffError(
            "the live service definition changed after setup recorded it: "
            f"{expected}"
        )
    parsed = _parse_macos_definition(expected, definition.data)
    served = _plist_workspace(parsed)
    if served != result.workspace:
        raise NativeHandoffError(
            f"the service definition serves {served}, not {result.workspace}"
        )
    _require_approved_macos_job(parsed, result)
    try:
        from ciao import macos_service

        status = macos_service.load_fresh_agent(
            expected, expected_sha256=result.definition_sha256
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise NativeHandoffError(f"Could not start the engine: {exc}") from exc
    if status != 0:
        raise NativeHandoffError(
            f"the service manager refused to start the engine (status {status})"
        )
    return result
