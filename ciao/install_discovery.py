"""Bare-shell install discovery: which installed engine is this shell talking to.

A bare-shell invocation (`ciao health get` from any directory) has no
``CIAO_WORKSPACE``, so ``ciao.config.installed_workspace_env`` has to find the
install the operator actually has rather than manufacture a bootstrap workspace
beside it. Before this module that question was answered by the macOS
LaunchAgent alone, which on Windows meant the engine's own logon task was
ignored and the bare shell fell back to the cwd. Linux reads the root-owned
systemd unit the installer wrote.

This is the one answer to that question, and both branches are read-only: they
read the service definition the engine wrote and mint nothing. A definition
naming a directory that is no longer there is stale and is not trusted — the
``is_dir()`` guard lives here so that rule has one home — because pinning it
would recreate what the operator deleted.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ciao.os_support.files import open_fd
from ciao.os_support.locks import lock_exclusive, unlock

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def _platform() -> str:
    """``sys.platform``, read fresh through a seam tests can force a branch on."""
    return sys.platform


def discover_workspace(base: Mapping[str, str]) -> Path | None:
    """The installed workspace this shell should use, or ``None`` if there is none.

    ``base`` is the caller's environment; the macOS branch needs it because the
    LaunchAgent's plist falls back to ``CIAO_WORKSPACE`` from it. The Windows
    branch reads the task definition the engine wrote and never consults the
    shell environment: the installed workspace is recorded there, and a live
    ``schtasks`` query would be a second source of truth this read-only seam does
    not need.

    The dispatch is platform-aware: on Windows the engine's own logon task is
    the install record, so it is read first; on Linux it is the systemd unit,
    trusted only when root-owned and not group- or world-writable, and never the
    shell environment. Everywhere else — and on Windows
    when no task was found — the macOS LaunchAgent read runs, which is the
    behaviour-before-this-seam call every platform made and the call the
    existing tests patch. On a real Windows machine there is no plist, so that
    read yields no workspace and the `is_dir()` guard leaves the mapping
    unchanged: no fallback install is invented.
    """
    if _platform() == "win32":
        discovered = _discover_windows()
        if discovered is not None:
            return discovered
        # Fall through rather than return None: the macOS read is the one the
        # seam must preserve on every platform, and on Windows it simply finds
        # no plist. Returning early here would bypass the patch target the
        # existing discovery tests install and break them on the Windows job.
    elif _platform().startswith("linux"):
        discovered = _discover_linux()
        if discovered is not None:
            return discovered
        # Same fall-through as Windows: a bare shell as another user, or an
        # install with no unit, keeps the behaviour it had before this branch.
    return _discover_macos(base)


def _discover_macos(base: Mapping[str, str]) -> Path | None:
    # Imported here, not at module scope: `ciao.macos_service` reads the plist,
    # and the tests patch `ciao.macos_service.discover_runtime`.
    from ciao.macos_service import discover_runtime

    try:
        discovered = discover_runtime(environ=dict(base))
    except Exception:  # noqa: BLE001 - plist missing/unreadable
        return None
    if not discovered or not discovered.workspace:
        return None
    workspace = Path(discovered.workspace)
    return workspace if workspace.is_dir() else None


def _discover_windows() -> Path | None:
    # Imported here for symmetry with the macOS branch and to keep this module
    # importable without pulling in the Task Scheduler surface.
    from ciao import windows_service

    definition = windows_service.live_task_dir() / windows_service.TASK_FILE_NAME
    workspace = windows_service.task_workspace(definition)
    if workspace is None:
        return None
    return workspace if workspace.is_dir() else None


def _discover_linux() -> Path | None:
    from ciao import linux_service

    workspace = linux_service.unit_workspace(linux_service.UNIT_PATH)
    if workspace is None or not workspace.is_dir():
        return None
    # A workspace this user cannot read (0700 under the service account) is not
    # this shell's install: fall through rather than point at it.
    return workspace if os.access(workspace, os.R_OK | os.X_OK) else None


def trusted_registry(name: str | None, vault: Path) -> CiaoConfig | None:
    """The installed engine's registry, if it registers *name* at exactly *vault*.

    The handover gate decides what an unattended agent may write into the
    always-loaded regions, so it must not read a registry the agent can name.
    ``--workspace``, ``CIAO_WORKSPACE`` and ``CIAO_RUNTIME_ROOT`` are caller
    inputs and are dropped here: the install comes only from the service
    definition (:func:`discover_workspace`), and nothing
    is guessed when there is none. ``None`` when no install is found, the name
    is not registered there, or the registered vault is not the one in hand.
    """
    if name is None:
        return None
    from ciao.config import CiaoConfig, installed_workspace_env

    base = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CIAO_WORKSPACE", "CIAO_RUNTIME_ROOT")
    }
    installed = discover_workspace(base)
    if installed is None:
        return None
    # A placeholder keeps a read-only resolution from minting a session secret.
    env_source = installed_workspace_env({**base, "CIAO_WORKSPACE": str(installed)})
    if not env_source.get("PWA_AUTH_TOKEN", "").strip():
        env_source["PWA_AUTH_TOKEN"] = "memory-proposals"
    config = CiaoConfig.from_env(env_source)
    if config.workspace(name) is None:
        return None
    try:
        registered_vault = Path(config.workspace_vault_root(name)).resolve()
    except (OSError, ValueError):
        return None
    return config if registered_vault == vault.resolve() else None


class InstallerEntryRefused(RuntimeError):
    """Installer bootstrap cannot prove that this is a fresh installation."""


def _strict_exists(path: Path) -> bool:
    """Whether ``path`` exists, refusing when that cannot be established.

    ``os.path.lexists`` reports ``False`` for an unreadable parent directory, so
    an install whose definition merely cannot be seen would look fresh. Only a
    definite "not there" is absence. A dangling symlink still counts as present.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not inspect {path}: {exc}") from exc
    return True


def _strict_set_up_workspace(root: Path) -> bool:
    """``is_set_up_workspace`` for the guard: I/O errors refuse, never read as absent.

    This is the legacy bootstrap guard's reader and keeps its ``read_text``
    semantics on purpose. The native classifier must not reuse it: a FIFO or an
    unbounded file where ``.env`` belongs would block or exhaust that read, so
    :func:`_classify_workspace` uses :func:`_read_native_text` instead.
    """
    from ciao.setup_marker import RUNTIME_DIR_NAME, WORKSPACE_REGISTRY_FILENAME

    if _strict_exists(root / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME):
        return True
    env_path = root / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallerEntryRefused(f"Could not read {env_path}: {exc}") from exc
    from ciao.macos_service import parse_dotenv

    return bool(parse_dotenv(text).get("CIAO_WORKSPACES", "").strip())


def require_fresh_installer_entry(*, environ: Mapping[str, str], cwd: Path) -> None:
    """Raise unless installer bootstrap is demonstrably safe. Read-only."""
    platform = _platform()
    if platform not in ("darwin", "win32"):
        raise InstallerEntryRefused(f"Installer bootstrap is unsupported on {platform}")
    if any(environ.get(key, "").strip() for key in (
        "CIAO_WORKSPACE", "CIAO_WORKSPACES", "CIAO_RUNTIME_ROOT", "CIAO_MEMORY_DIR",
        "CIAO_VAULT_DIR", "CIAO_VAULT_ROOT",
    )):
        raise InstallerEntryRefused("Inherited workspace or storage redirect is present")
    if _strict_set_up_workspace(cwd):
        raise InstallerEntryRefused("Current directory is an existing Ciaobot workspace")
    home_root = Path.home() / "Ciaobot"
    if _strict_set_up_workspace(home_root):
        raise InstallerEntryRefused("Default Ciaobot workspace already exists")
    try:
        if platform == "darwin":
            from ciao import macos_service
            definition = macos_service.live_launch_agents_dir() / f"{macos_service.SERVER_LABEL}.plist"
            if _strict_exists(definition):
                raise InstallerEntryRefused("Live LaunchAgent definition already exists")
            registered = macos_service.server_job_registered()
        else:
            from ciao import windows_service
            definition = windows_service.live_task_dir() / windows_service.TASK_FILE_NAME
            if _strict_exists(definition):
                raise InstallerEntryRefused("Live scheduled-task definition already exists")
            registered = windows_service.engine_task_registered()
        if registered:
            raise InstallerEntryRefused("Engine service is already registered")
    except InstallerEntryRefused:
        raise
    except Exception as exc:
        raise InstallerEntryRefused(f"Could not verify fresh installation state: {exc}") from exc


class NativeInstallState(str, Enum):
    """What a native install looks like before this run writes anything.

    A native installer needs to tell four situations apart, and the permissive
    runtime discovery above cannot: it answers ``None`` for "nothing found" and
    for "could not look", which are different facts to a program about to
    create a workspace and a service.

    - ``fresh``: no installation evidence at any known surface. The permanent,
      idle host-upgrade lock may still exist; it is a mutex, not evidence, so
      it neither makes this ``partial`` nor denies ``fresh``.
    - ``configured``: a service definition or job exists, an explicit disabled
      launchd override is set, or a workspace has a complete setup (a regular
      workspace-registry file, or the legacy configured env).
    - ``partial``: installation evidence without a complete setup — a receipt,
      the uv tool directory, the ``ciao`` shim (``ciao.exe`` on Windows), a
      setup marker, a server host,
      a leftover migration artifact, an interrupted host-upgrade journal, or an
      interrupted handoff result.
    - ``unknown``: the state cannot be established (a failed or malformed
      service query, an unreadable or corrupt marker, a FIFO, oversize,
      symlinked, hard-linked, replaced or undecodable ``.env`` or receipt, a
      directory, FIFO or symlink where the workspace registry belongs, a
      symlink where the installer's state directory belongs, a redirected path,
      or a host-upgrade lock that is held, unsafe or otherwise not classifiable).
      A held lock is a *concurrent* host mutation: like ``partial`` it refuses,
      and it is never retried as a silent reinstall. Unknown is never fresh.
      A home directory that cannot be looked up is likewise unknown: the
      default workspace was not found absent, only unseen.

    The durable host-upgrade lock is deliberately not in that list. It is a
    permanent, empty ``flock`` file every mutating host command creates and
    never removes, so its mere existence says only that some host command once
    ran; it is not proof of a prior install. Only its liveness and ownership are
    read, without creating, writing or truncating it: held or unsafe is
    unknown, idle contributes no evidence.

    This is a classification primitive only. In this issue nothing calls it in
    production, and a ``fresh`` answer is not an activation permit: #2340 owns
    the native entry point that must establish current-run state before any
    engine, tool or workspace mutation, and it must refuse ``configured``,
    ``partial`` and ``unknown`` alike. The setup child cannot ask this question
    for a fresh answer once the parent has created its artifacts; #2340 must
    prove that those artifacts belong to this run instead of trusting their
    paths or the parent's decision.
    """

    fresh = "fresh"
    configured = "configured"
    partial = "partial"
    unknown = "unknown"


# A native bootstrap refuses to run with an inherited storage or definition
# redirect: the redirect would hide the surface it is about to inspect, so the
# answer is unknown/refused rather than "the redirected path looked fine".
_REDIRECT_VARIABLES = (
    "CIAO_WORKSPACE",
    "CIAO_WORKSPACES",
    "CIAO_RUNTIME_ROOT",
    "CIAO_MEMORY_DIR",
    "CIAO_VAULT_DIR",
    "CIAO_VAULT_ROOT",
    "CIAO_LAUNCH_AGENTS_DIR",
)

_UV_TOOL_NAME = "ciaobot"
_SHIM_NAME = "ciao"
#: uv and pip write the console script as ``ciao.exe`` on Windows. The
#: extensionless ``ciao`` is the POSIX entry point, not a runnable Windows shim,
#: so it is not install evidence there.
_WINDOWS_SHIM_NAME = "ciao.exe"


def _require_sound_ancestry(path: Path) -> None:
    """Refuse an absence whose ancestor chain is ambiguous.

    ``lstat`` reports ``ENOENT`` for a path under a dangling symlink exactly as
    it does for a path that was never created. A dangling link is not "nothing
    here": it is a surface nobody can see, so the caller must not read it as
    absence. A non-directory ancestor and an unreadable one are the same kind
    of ambiguity. A pathname the OS refuses outright (``ValueError``: an
    embedded NUL, which a caller-supplied candidate can carry) is likewise
    unseen, not absent.
    """
    for parent in path.parents:
        try:
            status = os.lstat(parent)
        except FileNotFoundError:
            # This ancestor does not exist either; its own ancestors are still
            # checked by the loop.
            continue
        except (OSError, ValueError) as exc:
            raise InstallerEntryRefused(f"Could not inspect {parent}: {exc}") from exc
        if stat.S_ISLNK(status.st_mode):
            if not parent.exists():
                raise InstallerEntryRefused(f"{parent} is a dangling symlink")
            continue
        if not stat.S_ISDIR(status.st_mode):
            raise InstallerEntryRefused(f"{parent} is not a directory")


def _strict_presence(path: Path) -> bool:
    """Whether ``path`` is present; anything but a definite absence refuses.

    Unlike :func:`_strict_exists`, a non-directory ancestor is not "absent",
    and neither is a path that cannot be seen through a dangling symlink:
    ``Ciaobot`` being a file, or a link to nowhere, where the install path
    expects a directory is an install surface nobody can classify, not an
    empty one. A pathname ``lstat`` refuses with ``ValueError`` — an embedded
    NUL in a caller-supplied suggestion is the case that reaches here — is
    refused the same way instead of escaping as a crash.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        _require_sound_ancestry(path)
        return False
    except (OSError, ValueError) as exc:
        raise InstallerEntryRefused(f"Could not inspect {path}: {exc}") from exc
    return True


#: A workspace ``.env`` and the install receipt are small documents. A file
#: that claims to be one and is larger than this is not one the installer
#: wrote, and reading it into memory is not worth the risk.
_NATIVE_TEXT_LIMIT = 64 * 1024
#: A state document is normally one name. More than one link means another
#: name can change the bytes this read is judging, and the native classifier
#: has no way to account for that writer.
_NATIVE_TEXT_LINKS = 1


def _read_native_text(path: Path) -> str:
    """Read one small regular file for the native classifier, or refuse.

    Native-specific on purpose. The legacy :func:`_strict_set_up_workspace`
    keeps ``Path.read_text`` for :func:`require_fresh_installer_entry`; here
    the classifier is the first thing to look at a machine it has not written
    yet, so the read must neither block on nor trust the size of what it finds.
    A FIFO, a socket, a device, a symlink, a file with more than one name, an
    unreadable path, a file over :data:`_NATIVE_TEXT_LIMIT`, a replacement or
    rewrite during the read, and text that is not UTF-8 all raise
    :class:`InstallerEntryRefused`, which the classifier reports as ``unknown``
    — never as partial or fresh.

    The open is read-only, non-blocking and no-follow where the platform has
    them; the descriptor is re-checked for a regular file, for a single link,
    and for the inode the pathname named before it opened, must yield exactly
    the size it opened with, and is compared again with the pathname afterwards
    so a swap is a refusal rather than someone else's bytes.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise InstallerEntryRefused(f"Could not read {path}: it is missing") from None
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not inspect {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise InstallerEntryRefused(f"{path} is not a regular file")
    if before.st_nlink != _NATIVE_TEXT_LINKS:
        raise InstallerEntryRefused(f"{path} has more than one name")
    if before.st_size > _NATIVE_TEXT_LIMIT:
        raise InstallerEntryRefused(
            f"{path} is larger than {_NATIVE_TEXT_LIMIT} bytes"
        )
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = open_fd(path, flags, follow_symlinks=False)
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not open {path}: {exc}") from exc
    chunks: list[bytes] = []
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise InstallerEntryRefused(f"{path} is not a regular file")
        if opened.st_nlink != _NATIVE_TEXT_LINKS:
            raise InstallerEntryRefused(f"{path} has more than one name")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise InstallerEntryRefused(f"{path} was replaced while it was opened")
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _NATIVE_TEXT_LIMIT:
                raise InstallerEntryRefused(
                    f"{path} is larger than {_NATIVE_TEXT_LIMIT} bytes"
                )
            chunks.append(chunk)
        settled = os.fstat(descriptor)
    except InstallerEntryRefused:
        raise
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not read {path}: {exc}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    # The byte count against the opened size, the descriptor against itself,
    # then the pathname against the descriptor: a rewrite in place changes
    # size/mtime, an append or early EOF is a short read, a replacement changes
    # the inode, and a new second name changes the link count.
    if (
        settled.st_nlink != _NATIVE_TEXT_LINKS
        or total != opened.st_size
        or (settled.st_dev, settled.st_ino) != (opened.st_dev, opened.st_ino)
        or settled.st_size != opened.st_size
        or settled.st_mtime_ns != opened.st_mtime_ns
    ):
        raise InstallerEntryRefused(f"{path} changed while it was being read")
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not re-inspect {path}: {exc}") from exc
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != _NATIVE_TEXT_LINKS
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != settled.st_size
        or after.st_mtime_ns != settled.st_mtime_ns
    ):
        raise InstallerEntryRefused(f"{path} changed while it was being read")
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallerEntryRefused(f"Could not decode {path}: {exc}") from exc


def _unsafe_native_state_dir(path: Path) -> bool:
    """Whether the installer's state directory is not a plain real directory.

    ``~/.local/state/ciaobot`` holds the receipt, the migration transaction,
    the host-upgrade journal and the upgrade lock. A symlink there — or a file
    where the directory belongs — makes all of those somebody else's surface,
    so the answer is ``unknown`` before any of them is read. A missing
    directory is "no state yet", not unsafe; a path that cannot be inspected is
    unsafe because its absence cannot be established.
    """
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        try:
            _require_sound_ancestry(path)
        except InstallerEntryRefused:
            return True
        return False
    except OSError:
        return True
    return not stat.S_ISDIR(status.st_mode)


def _require_regular_file(path: Path) -> None:
    """Refuse unless ``path`` is a regular file, not merely a name that exists.

    :func:`_strict_presence` answers only whether the name is there, so a
    directory, a FIFO, a device or a dangling symlink at the workspace-registry
    name would otherwise be read as a completed setup. None of those is the
    file ``ciao setup`` writes, and nobody can describe the machine behind
    them, so they refuse (``unknown``) rather than answering ``configured``.
    """
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not inspect {path}: {exc}") from exc
    if not stat.S_ISREG(status.st_mode):
        raise InstallerEntryRefused(f"{path} is not a regular file")


def _classify_workspace(root: Path) -> NativeInstallState | None:
    """One candidate workspace's evidence, or ``None`` when it has none.

    The completed-setup test is the native classifier's own, not the legacy
    guard's: the registry must be a regular file (presence alone is not proof),
    and a legacy ``.env`` is read once through :func:`_read_native_text` so one
    FIFO or oversize file can only answer ``unknown`` instead of blocking the
    scan.
    """
    from ciao.setup_marker import RUNTIME_DIR_NAME, WORKSPACE_REGISTRY_FILENAME

    if not _strict_presence(root):
        return None
    try:
        status = os.lstat(root)
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not inspect {root}: {exc}") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise InstallerEntryRefused(f"{root} is not a directory")
    runtime = root / RUNTIME_DIR_NAME
    registry = runtime / WORKSPACE_REGISTRY_FILENAME
    if _strict_presence(registry):
        # A name at the registry path is not a completed setup: only the
        # regular file `ciao setup` writes is. A directory, FIFO, device or
        # symlink (dangling or not) refuses as unknown.
        _require_regular_file(registry)
        return NativeInstallState.configured
    # Collect the partial signals and read the config even when .runtime is
    # already evidence: an unreadable .env must reach the caller as unknown,
    # not be hidden behind a partial directory.
    partial = False
    if _strict_presence(runtime):
        try:
            runtime_status = os.lstat(runtime)
        except OSError as exc:
            raise InstallerEntryRefused(f"Could not inspect {runtime}: {exc}") from exc
        if not stat.S_ISDIR(runtime_status.st_mode):
            raise InstallerEntryRefused(f"{runtime} is not a directory")
        # An interrupted `ciao setup` (or a marker with no registry) is
        # provisioning evidence, not a completed workspace.
        partial = True
    env_path = root / ".env"
    if _strict_presence(env_path):
        from ciao.macos_service import parse_dotenv

        values = parse_dotenv(_read_native_text(env_path))
        # The pre-1.0 workspace record. A registry above is the modern one;
        # this is still a complete setup, not a fragment.
        if values.get("CIAO_WORKSPACES", "").strip():
            return NativeInstallState.configured
        if any(
            key.startswith("CIAO_") or key == "PWA_AUTH_TOKEN" for key in values
        ):
            partial = True
    return NativeInstallState.partial if partial else None


def _migration_evidence(unknown: bool, partial: bool) -> tuple[bool, bool]:
    """The installer's migration transaction and before-images, strictly read.

    ``~/.local/state/ciaobot/migration`` is what ``install-engine.sh --migrate``
    keeps while a hand-over is in flight and after it settles; its ``before/``
    directory holds the before-images a rollback restores. Either is install
    evidence (partial). This classifier never deletes, rolls back or rewrites
    that material: recovery is the operator's explicit action through the
    migration's own rollback path. A migration path that is not a real
    directory cannot be classified (unknown): a file or a symlink where the
    transaction expects its own directory means this machine is mid-install in
    a way nobody can read.
    """
    from ciao import install_receipt

    migration = install_receipt.default_receipt_path().parent / "migration"
    if _strict_presence(migration):
        try:
            status = os.lstat(migration)
        except OSError:
            return (True, partial)
        if not stat.S_ISDIR(status.st_mode):
            return (True, partial)
        partial = True
        before = migration / "before"
        if _strict_presence(before):
            try:
                before_status = os.lstat(before)
            except OSError:
                return (True, partial)
            if not stat.S_ISDIR(before_status.st_mode):
                return (True, partial)
            partial = True
    return (unknown, partial)


class _LockObservation(str, Enum):
    """What a read-only probe of the durable host-upgrade lock can say."""

    absent = "absent"
    idle = "idle"
    held = "held"
    unsafe = "unsafe"


def _owned_private_lock(status: os.stat_result) -> bool:
    """Whether this lock's ownership and mode are the ones the installer writes.

    The probe is only meaningful on the installer's own file: another owner or a
    group/other bit means the answer could be someone else's, so it is
    ambiguous rather than evidence. Windows has no ``st_uid``/mode contract in
    the same sense, so the ownership half is asked only where it exists.
    """
    if hasattr(os, "getuid") and status.st_uid != os.getuid():
        return False
    return stat.S_IMODE(status.st_mode) & 0o077 == 0


def _observe_upgrade_lock(path: Path) -> _LockObservation:
    """Probe the durable host-upgrade lock without changing it.

    ``server_host_install`` creates ``server-host-upgrade.lock`` with
    ``O_CREAT`` and takes a non-blocking ``flock`` on it for the short
    check-and-prepare step of every mutating host command; it never unlinks it.
    That makes the file *durable* but not *proof of an install*: an idle, empty,
    correctly owned lock says only that some host command once ran. Its
    liveness is still worth asking, because a held lock is a concurrent host
    mutation and a native install must refuse rather than race it.

    The probe never creates, writes, truncates, chmods or unlinks the file. It
    opens the existing regular file read-only and non-blocking, requires the
    same device/inode/owner/mode/empty-size it saw under ``lstat`` to still be
    true on the descriptor, takes a non-blocking exclusive lock, releases it,
    and re-checks the pathname still names that inode. A symlink, a special
    file, more than one hard link, an unexpected owner or group/other bit,
    unreadable metadata, a non-empty file, a failed lock call or a replacement
    while probing is ``unsafe``: an answer about someone else's or an unknown
    file is not an answer.

    Limits: the probe briefly holds the lock when it is idle, so a real
    installer opening it in that instant would see ``TRANSACTION_BUSY``. The
    window is one non-blocking call and a close, and this is why #2340 must run
    the classifier as the *single* pre-mutation scan rather than polling it.
    A lock taken after this probe is, by construction, invisible to it; the
    caller's own ownership checks and re-check of the known surfaces remain
    mandatory.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        try:
            # A missing file under a dangling symlink is not absence.
            _require_sound_ancestry(path)
        except InstallerEntryRefused:
            return _LockObservation.unsafe
        return _LockObservation.absent
    except OSError:
        return _LockObservation.unsafe
    if not stat.S_ISREG(before.st_mode) or before.st_size != 0:
        return _LockObservation.unsafe
    # The installer creates one name for one inode; an extra link means the
    # "lock" may be another file's inode, so its liveness is not this lock's.
    if before.st_nlink != 1:
        return _LockObservation.unsafe
    if not _owned_private_lock(before):
        return _LockObservation.unsafe
    try:
        descriptor = open_fd(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
            follow_symlinks=False,
        )
    except OSError:
        return _LockObservation.unsafe
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != 0
            or opened.st_nlink != 1
            or not _owned_private_lock(opened)
        ):
            return _LockObservation.unsafe
        try:
            lock_exclusive(descriptor, blocking=False)
        except BlockingIOError:
            return _LockObservation.held
        except OSError:
            return _LockObservation.unsafe
        try:
            unlock(descriptor)
        except OSError:
            return _LockObservation.unsafe
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        after = os.lstat(path)
    except OSError:
        return _LockObservation.unsafe
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        return _LockObservation.unsafe
    return _LockObservation.idle


def _host_upgrade_evidence(platform: str, unknown: bool, partial: bool) -> tuple[bool, bool]:
    """The macOS server-host upgrade journal and its sibling lock.

    ``server_host_install`` owns both paths (``~/.local/state/ciaobot/
    server-host-upgrade.json`` and ``.lock``) and is macOS-only, so neither is
    read on Windows. A journal means a host swap is in flight or was
    interrupted; it is install evidence (partial), and it is never deleted or
    rolled back here — that is the operator's explicit recovery through
    ``server-host-install transaction-status`` / ``rollback-upgrade``. A journal
    that is a symlink or not a regular file cannot be classified (unknown): the
    host transaction refuses that shape too, and nobody should call the machine
    fresh behind it.

    The lock is a different fact and is handled by :func:`_observe_upgrade_lock`:
    a held or unsafe lock is unknown, and an idle empty lock contributes no
    install evidence. Presence of the lock alone is never reported as partial.
    """
    if platform != "darwin":
        return (unknown, partial)

    from ciao import server_host_install

    journal = server_host_install.upgrade_journal_path()
    if _strict_presence(journal):
        try:
            status = os.lstat(journal)
        except OSError:
            unknown = True
        else:
            if stat.S_ISREG(status.st_mode):
                partial = True
            else:
                unknown = True

    lock = _observe_upgrade_lock(server_host_install.upgrade_lock_path())
    if lock in (_LockObservation.held, _LockObservation.unsafe):
        unknown = True
    return (unknown, partial)


def _native_marker_state(
    platform: str, tool_dir: Path, bin_dir: Path
) -> NativeInstallState | None:
    """Partial or unknown evidence that is not a workspace.

    The shim marker is the platform's own entry-point name: ``ciao.exe`` on
    Windows, where uv and pip write the console script with that extension,
    and ``ciao`` everywhere else.
    """
    from ciao import install_receipt, installer_handoff

    if _unsafe_native_state_dir(install_receipt.default_receipt_path().parent):
        return NativeInstallState.unknown

    markers = [
        tool_dir / _UV_TOOL_NAME,
        bin_dir / (_WINDOWS_SHIM_NAME if platform == "win32" else _SHIM_NAME),
        installer_handoff.result_path(),
    ]
    if platform == "darwin":
        from ciao import server_host

        markers += [
            Path.home() / "Applications" / "Ciaobot.app",
            server_host.default_bundle_path(),
            server_host.default_ownership_path(),
        ]
    partial = False
    unknown = False
    for marker in markers:
        if _strict_presence(marker):
            partial = True
    receipt_path = install_receipt.default_receipt_path()
    if _strict_presence(receipt_path):
        # The startup reader is deliberately permissive; here a receipt that
        # cannot be read is an install nobody can describe, so it is unknown
        # rather than partial-or-absent. The bytes come through the native
        # bounded reader: a FIFO or oversize file must not block the scan, and
        # the parse itself stays the one shared schema check.
        try:
            receipt = install_receipt.parse_receipt(_read_native_text(receipt_path))
        except InstallerEntryRefused:
            unknown = True
        else:
            if receipt is None:
                unknown = True
            else:
                partial = True
    unknown, partial = _migration_evidence(unknown, partial)
    unknown, partial = _host_upgrade_evidence(platform, unknown, partial)
    if unknown:
        return NativeInstallState.unknown
    return NativeInstallState.partial if partial else None


def classify_native_install(
    *,
    environ: Mapping[str, str],
    cwd: Path,
    suggestion: Path | None,
    tool_dir: Path,
    bin_dir: Path,
) -> NativeInstallState:
    """Classify this machine for a native install. Read-only, strict, native-only.

    ``configured`` beats ``unknown`` beats ``partial`` beats ``fresh``: a
    complete setup is positive proof whatever else is unreadable, and a
    surface that cannot be inspected is never reported as absence. Inherited
    storage or definition redirects, a failed or malformed service query, an
    unreadable or corrupt receipt, an undecodable ``.env``, a FIFO or a file
    over :data:`_NATIVE_TEXT_LIMIT` or with a second hard link where ``.env`` or
    the receipt belongs, a directory, FIFO or symlink where the workspace
    registry belongs, a symlink or a file where the installer's state
    directory or another expected directory belongs, a home directory whose
    lookup fails, and a held or unsafe host-upgrade lock all answer
    ``unknown``. Only the absence of installation
    evidence at every surface it classifies is ``fresh`` (an idle, empty
    host-upgrade lock is not evidence). ``partial``, ``unknown`` and a
    concurrent (held-lock) state all refuse a native install; none of them is a
    reason to delete what was found and reinstall silently.

    ``.env`` and the receipt are read through :func:`_read_native_text`, one
    bounded, no-follow, non-blocking descriptor of a single-link regular file
    whose inode, size and mtime are rechecked before its bytes are used. The
    legacy :func:`_strict_set_up_workspace` is not on this path and keeps its
    ``read_text`` semantics for :func:`require_fresh_installer_entry`.

    ``tool_dir`` and ``bin_dir`` are the caller's uv tool root and entry-point
    directory; nothing here resolves or installs anything.

    The workspace search is deliberately finite: exactly ``cwd``,
    ``~/Ciaobot`` and the caller's ``suggestion``. It does not walk the disk, so
    a complete Ciaobot workspace somewhere else is invisible here and does not
    turn this answer into ``configured``. That is a known limit, not a proof of
    absence; #2340 must combine this scan with the surfaces it knows about. A
    marker written by the current run is still evidence too — there is no
    run-id, timestamp or "this process wrote it" exemption here — because path
    presence is not ownership.

    No production caller runs this yet. #2340 owns the native entry point on
    both sides of the handoff. Neither the installer parent's decision nor a
    list of paths the installer is expected to create proves who created one:
    a same-UID writer can pre-create any of them. Each side must establish
    current-run ownership itself from owner-private, run-id-bound evidence and
    re-check every known surface immediately before its first write rather
    than trusting an earlier scan. The setup child in particular cannot call
    this again for a ``fresh`` answer: the parent has already created the uv
    tool directory, the shim, the workspace and often the receipt by then, so
    the same surfaces read ``partial`` or ``configured`` even when this run
    created every one of them. Until #2340 wires that, a plain installer run
    does not consult this answer, so ``fresh`` here is not by itself permission
    to write anything.
    """
    platform = _platform()
    if platform not in ("darwin", "win32"):
        return NativeInstallState.unknown

    configured = False
    partial = False
    # An inherited redirect is one unknown signal, not a verdict: a live
    # definition or a completed workspace is positive proof even when the
    # environment also points somewhere else. The signals are collected first
    # and precedence is applied once, at the end.
    unknown = any(environ.get(key, "").strip() for key in _REDIRECT_VARIABLES)

    candidates = [cwd]
    try:
        candidates.append(Path.home() / "Ciaobot")
    except (OSError, RuntimeError):
        # No home means the default workspace could not even be looked at.
        # That is an unreadable surface, not absence, so it is unknown; the
        # candidate itself is skipped rather than guessed.
        unknown = True
    if suggestion is not None:
        candidates.append(suggestion)
    for root in candidates:
        try:
            outcome = _classify_workspace(root)
        except InstallerEntryRefused:
            unknown = True
            continue
        if outcome is NativeInstallState.configured:
            configured = True
        elif outcome is NativeInstallState.partial:
            partial = True

    try:
        if platform == "darwin":
            from ciao import macos_service

            agents = macos_service.live_launch_agents_dir()
            if _strict_presence(agents / f"{macos_service.SERVER_LABEL}.plist"):
                configured = True
            if _strict_presence(agents / f"{macos_service.LEGACY_MENUBAR_LABEL}.plist"):
                partial = True
            # The qualified probe: presence is configured evidence, and
            # uncertainty (a failed, unqualified or other-domain answer) is
            # unknown, never fresh. The unqualified `launchctl list` cannot
            # prove absence and is not asked here.
            if not macos_service.server_job_absent():
                configured = True
            elif macos_service.launchd_override_disabled():
                # An explicit override is the operator's existing service
                # configuration, so a label that launchd will not start is not
                # a fresh install even though no job is loaded. Fresh activation
                # refuses it for the same reason; it never clears the override.
                configured = True
        else:
            from ciao import windows_service

            if _strict_presence(
                windows_service.live_task_dir() / windows_service.TASK_FILE_NAME
            ):
                configured = True
            if windows_service.engine_task_registered():
                configured = True
    except Exception:  # noqa: BLE001 - a failed or uncertain probe is unknown
        unknown = True

    try:
        marker_state = _native_marker_state(platform, tool_dir, bin_dir)
    except Exception:  # noqa: BLE001 - an unreadable marker is unknown
        marker_state = NativeInstallState.unknown
    if marker_state is NativeInstallState.unknown:
        unknown = True
    elif marker_state is NativeInstallState.partial:
        partial = True

    if configured:
        return NativeInstallState.configured
    if unknown:
        return NativeInstallState.unknown
    if partial:
        return NativeInstallState.partial
    return NativeInstallState.fresh
