"""Acquire and install the signed universal server host (#1050, child E1 of #1008).

This is the installer half of the server-host work. It consumes the two things
that are already authenticated before it runs: a release manifest that
:func:`ciao.release_manifest.verify_manifest` has accepted and the one
``server-host`` artifact :func:`ciao.release_manifest.select_server_host_artifact`
resolved from it, plus the downloaded archive. Nothing here signs, activates or
starts anything: there is no ``launchctl``, no plist, no ``open`` and no TCC
call, and the host is inert until the service consumer (B2) and the activation
child (E2) decide to use it.

The steps are deliberately ordered so a refusal leaves nothing installed:

1. the archive is a regular file whose SHA-256 and byte count equal the signed
   entry's ``sha256``/``size``;
2. it extracts under ``filter="data"`` into a staging directory beside the
   target, refusing a link or a special member before the extraction;
3. the extracted ``Ciaobot Server.app`` passes :func:`ciao.server_host.inspect_host_bundle`
   — the plist identity and the native ``/usr/bin/codesign`` probes, not the
   manifest constants alone — while it is still in the dotted staging directory
   beside the target, not at the target path;
4. the owner-only record is written from the staged snapshot, then the staging
   directory is renamed onto the target, which must not already exist as a
   symlink, and an existing verified host is a no-op rather than something to
   overwrite;
5. :func:`ciao.server_host.verify_owned_host` must accept the installed bundle
   and the record.

A **superseded** host — one of the revisions this engine replaces — is the one
bundle that may already be at the target: :func:`ciao.server_host.verify_superseded_host`
proves it is ours from the same private record, and the install then moves it
aside, writes the new record, and renames the new bundle in. Without that path
a host revision bump would silently de-install the host on every existing Mac:
a revision-2 or -3 record is refused, the installer says "a host already exists",
and the engine install continues with no host at all (#1659). The replaced host
and its old record are put back if anything fails, so a failed replacement
leaves the Mac exactly as it was.

The record is written **before** the rename on purpose: the window between the
rename and a record write contains a full native re-inspection (seconds), and a
process killed there would leave ``~/Applications/Ciaobot Server.app`` with no
record, which every later run then reports as ``host_exists`` until the user
deletes it by hand. Written first, a crash before the rename leaves a record
with no bundle (the next fresh install overwrites it) and a crash after the
rename leaves a consistent, verified host.

A foreign, tampered or mismatched archive is refused at step 1-3, before the
target is touched; a Python failure removes the bundle this run renamed into
place *and* the record it wrote, so a partial install survives neither as a
bundle without a record nor as a record without a bundle. There is no fallback
that trusts an unverified archive, and the record path is
:func:`ciao.server_host.default_ownership_path` with no Ciaobot-specific
environment override.

A refusal is only fatal to the engine install when it is about the release the
shell could not already rule out — a manifest that did not verify, an
``ARCHIVE_MISMATCH``, or a candidate whose expected identity the signed entry
and the local record disagree about. Everything else (``INSTALL_FAILED``,
``INSPECTION_FAILED``, an ``ARCHIVE_UNSAFE`` archive the release signed, an
existing ``HOST_EXISTS``/``TARGET_UNSAFE`` target) returns
:data:`HOST_WARNING_EXIT` from ``install`` so the shell warns and carries on:
the host is optional and inert, and it must not abort the one-liner.
:data:`WARN_AND_CONTINUE` is that set. :data:`SIGNATURE_REJECTED` is never in
it: a completed native probe that disproved the signed identity is a candidate
this installer must not place, and it aborts before anything is changed.

**The transactional upgrade (#2330).** A host this installer *replaces* may be
one of the revisions this engine supersedes, or the production revision-4 host
when the release selects the explicitly signed revision-5 one. The plain
``install`` still replaces a superseded host in one step (its before-image lives
in the staging directory and is gone once the call returns). A **legacy to
signed** replacement is different: the old app and the exact old record bytes
have to survive until the *engine* the shell installs has answered healthily,
which is outside this module's lifetime. So that path is a durable transaction:

1. ``install --defer-commit --owner-pid <installer pid>`` retains the old bundle
   (a same-filesystem rename into a nonce-derived dotted backup directory beside
   the target) and the **exact** old record bytes (never a ``to_record()``
   reserialization), writes one owner-only journal under
   ``~/.local/state/ciaobot/server-host-upgrade.json``, records the nonce and the
   outer installer's pid, installs the signed host, and returns with the journal
   at ``installed``.
2. ``commit-upgrade --operation <nonce> --owner-pid <pid>`` re-verifies the
   installed signed pair and only then marks the journal terminal and releases
   the before-images.
3. ``rollback-upgrade`` restores the exact old pair and marks the journal
   ``rolled_back``.

``transaction-status`` is how the shell learns whether *this* run has a pending
upgrade. An unfinished journal whose owner is a live, different process blocks
every other mutating command; a new owner may claim **rollback** of an abandoned
operation (proved absent under the shared lock) but never a commit. Nothing here
runs a service, launches an app, signs anything or touches TCC: quiescing the
candidate service is the shell installer's job, and it must happen before the
identity on disk is reverted.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive
from ciao.os_support.private import (
    descriptor_acl_grants_another_account,
    is_private_descriptor_strict,
    make_private,
    make_private_dir,
    mkstemp_private,
)
from ciao.release_manifest import (
    RELEASE_PUBLIC_KEY,
    SERVER_HOST_FILENAME,
    SERVER_HOST_KIND,
    select_server_host_artifact,
    verify_manifest,
)
from ciao.server_host import (
    APP_NAME,
    DEVELOPER_ID_SIGNING_KIND,
    HOST_PROTOCOL,
    HOST_REVISION,
    INSPECTION_FAILED,
    SIGNED_HOST_REVISION,
    SIGNED_SCHEMA_VERSION,
    SUPERSEDED_HOST_PROTOCOLS,
    UNSUPPORTED_PLATFORM,
    DeveloperIDSigning,
    HostOwnership,
    Runner,
    ServerHostError,
    default_bundle_path,
    default_ownership_path,
    inspect_host_bundle,
    read_host_ownership,
    read_superseded_ownership,
    verify_owned_host,
    verify_superseded_host,
)

__all__ = [
    "ARCHIVE_MISMATCH",
    "ARCHIVE_UNSAFE",
    "DEFERRED_UPGRADE_REQUIRED",
    "HOST_DEFERRED_EXIT",
    "HOST_EXISTS",
    "HOST_WARNING_EXIT",
    "INSTALL_FAILED",
    "POLICY_REFUSED",
    "STAGING_PREFIX",
    "TARGET_UNSAFE",
    "HOST_TRANSACTION_LEFTOVER_EXIT",
    "TRANSACTION_BUSY",
    "TRANSACTION_CLEANUP",
    "TRANSACTION_CONFLICT",
    "TRANSACTION_FAILED",
    "UPGRADE_BACKUP_PREFIX",
    "UPGRADE_JOURNAL_NAME",
    "UPGRADE_LOCK_NAME",
    "UPGRADE_PHASES",
    "WARN_AND_CONTINUE",
    "ServerHostInstallError",
    "commit_host_upgrade",
    "extract_host_archive",
    "install_server_host",
    "main",
    "rollback_host_upgrade",
    "transaction_status",
    "upgrade_journal_path",
    "upgrade_lock_path",
    "verify_host_archive",
]

#: Stable refusal codes. They extend :mod:`ciao.server_host`'s vocabulary so a
#: caller can match on one set of strings whichever layer refused.
ARCHIVE_MISMATCH = "archive_mismatch"
ARCHIVE_UNSAFE = "archive_unsafe"
HOST_EXISTS = "host_exists"
INSTALL_FAILED = "install_failed"
TARGET_UNSAFE = "target_unsafe"
#: The signed entry and the local record disagree about *which* host this Mac may
#: run: a signed-to-legacy replacement (a downgrade), a different Developer ID
#: team (a rotation this installer never performs on its own), or a local signed
#: host this run was not asked to migrate. It is deliberately fatal rather than a
#: warning: continuing would leave a host whose identity the release did not
#: choose.
POLICY_REFUSED = "policy_refused"
#: A legacy host can only become the signed host through the recoverable
#: transaction above. A run that did not ask for one refuses with this code
#: instead of taking the old short-lived rollback path; ``HOST_DEFERRED_EXIT`` is
#: how ``install`` reports it, and the shell turns that into a spoken skip on
#: ``--no-start`` (the old app and record are left exactly as they were).
DEFERRED_UPGRADE_REQUIRED = "deferred_upgrade_required"
TRANSACTION_BUSY = "transaction_busy"
TRANSACTION_CONFLICT = "transaction_conflict"
TRANSACTION_FAILED = "transaction_failed"
#: The operation itself is settled - the signed host was committed, or the
#: previous pair was restored - but the before-images or the journal could not be
#: removed. That leftover is *reported*, not discarded: it keeps the next host
#: transaction refused until a later run finishes the cleanup. ``main`` reports it
#: with :data:`HOST_TRANSACTION_LEFTOVER_EXIT` so the shell does not roll a
#: committed (or already restored) host back over it.
TRANSACTION_CLEANUP = "transaction_cleanup"

#: ``install``'s exit status for :data:`DEFERRED_UPGRADE_REQUIRED`. Distinct from
#: :data:`HOST_WARNING_EXIT` so the shell can say "this completes on a later run"
#: rather than "the host was not installed" for every other local refusal.
HOST_DEFERRED_EXIT = 4

#: ``main``'s exit status for :data:`TRANSACTION_CLEANUP`: settled, but with
#: retained material left behind. Distinct from 1 so the shell reports the
#: leftover truthfully instead of treating a committed host as a failed commit.
HOST_TRANSACTION_LEFTOVER_EXIT = 5

#: Refusal codes that must not abort the engine install. The shell has already
#: verified the signed manifest and the archive's digest and size, so by the time
#: ``install`` runs these are local facts (a disk full, a permission error, a
#: ``codesign`` probe that timed out) or a signed-build defect — never attacker
#: input — and the engine should install anyway: the host is optional and inert.
#: ``ARCHIVE_MISMATCH`` is deliberately absent: a downloaded archive that does not
#: match the signed entry means the release did not verify, and the run stops.
WARN_AND_CONTINUE = frozenset(
    {
        ARCHIVE_UNSAFE,
        HOST_EXISTS,
        INSTALL_FAILED,
        INSPECTION_FAILED,
        TARGET_UNSAFE,
        UNSUPPORTED_PLATFORM,
    }
)

#: The staging directory is created beside the target so the final rename is a
#: same-filesystem, atomic operation. The name is dotted so a listing of
#: ``~/Applications`` during a run shows it as scratch, and unique so two runs
#: cannot collide.
STAGING_PREFIX = ".ciaobot-host-stage."

#: ``install``'s exit status for a refusal in :data:`WARN_AND_CONTINUE`. The
#: target is left exactly as it is and the shell installer warns and carries on
#: with the engine: a host that is optional and inert must not turn every later
#: engine install into a failure. Every other refusal - a mismatched archive the
#: release did not authenticate, or a manifest that did not verify - is exit 1.
HOST_WARNING_EXIT = 3

#: The one durable upgrade journal (#2330): a fixed owner-only file beside the
#: host record, a sibling lock, and a nonce-derived backup directory under the
#: bundle's own parent so retaining the old app is a same-filesystem rename.
#: There is no ``CIAO_*`` override: the path is derived from ``Path.home()`` at
#: call time, exactly like :func:`ciao.server_host.default_ownership_path`, so
#: the per-test home isolation applies and no environment can redirect it.
UPGRADE_JOURNAL_NAME = "server-host-upgrade.json"
UPGRADE_LOCK_NAME = "server-host-upgrade.lock"
UPGRADE_BACKUP_PREFIX = ".ciaobot-host-upgrade."

#: The journal document's schema and its exact phase vocabulary. Terminal phases
#: are written durably *before* any best-effort removal of the before-images, so
#: a kill between the two is a later cleanup, never a second rollback.
UPGRADE_SCHEMA_VERSION = 1
UPGRADE_PHASES = ("prepared", "old_moved", "installed", "committed", "rolled_back")
UPGRADE_TERMINAL_PHASES = frozenset({"committed", "rolled_back"})

_CHUNK = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_JOURNAL_FIELDS = frozenset(
    {
        "schema",
        "operation",
        "phase",
        "owner_pid",
        "bundle_path",
        "record_path",
        "backup_dir",
        "backup_dev",
        "backup_inode",
        "archive_sha256",
        "old_record_sha256",
        "new_record_sha256",
        "old",
        "new",
        "policy",
    }
)
_POLICY_FIELDS = frozenset({"revision", "protocol", "schema", "team_id"})
#: The old app keeps its real name inside the before-image directory. That is not
#: cosmetic: the inspector refuses a bundle that is not named ``APP_NAME``, and the
#: retained copy has to pass exactly the same inspection as the installed one
#: before a rollback may adopt it.
_RETAINED_BUNDLE = APP_NAME
_RETAINED_RECORD = "record"
#: The directory the rollback moves a verified *candidate* into before deleting it.
#: The app keeps its real name inside it, so the moved object can be inspected
#: exactly like an installed one - what is removed is then the object that was
#: verified and moved, not whatever the canonical name holds later.
_RETIRED_BUNDLE = "candidate"


class ServerHostInstallError(ServerHostError):
    """A refusal from the host installer, carrying a stable ``code``.

    It subclasses :class:`ciao.server_host.ServerHostError` so a caller that
    catches that one type catches every refusal in the acquisition path,
    including the ones the B1 inspector raises while it inspects the bytes.
    """


def _require_macos() -> None:
    """The host is a macOS bundle; nothing is downloaded or extracted elsewhere."""
    if sys.platform != "darwin":
        raise ServerHostInstallError(
            "the Ciaobot Server host is a macOS bundle; this platform cannot install it",
            code=UNSUPPORTED_PLATFORM,
        )


def _hash_regular_file(path: Path, *, what: str, code: str) -> tuple[str, int]:
    """Digest ``path`` as a regular file, refusing a symlink or special file.

    The checks and the read are two lookups of one name, so the descriptor is
    opened with ``O_NOFOLLOW`` and its device/inode compared to the ``lstat``
    result before a byte is read: a file swapped in between the two is refused
    rather than hashed as if it were the one the entry named.
    """
    try:
        checked = os.lstat(path)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} cannot be examined: {exc}", code=code
        ) from exc
    if stat.S_ISLNK(checked.st_mode):
        raise ServerHostInstallError(f"{what} must not be a symlink", code=code)
    if not stat.S_ISREG(checked.st_mode):
        raise ServerHostInstallError(f"{what} must be a regular file", code=code)
    try:
        # O_NONBLOCK so a FIFO swapped in after the lstat cannot block the open
        # forever before the inode comparison refuses it; it does not change how
        # a regular file reads.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        descriptor = open_fd(path, flags, follow_symlinks=False)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
                raise ServerHostInstallError(
                    f"{what} changed while it was being read", code=code
                )
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ServerHostInstallError:
        raise
    except OSError as exc:
        raise ServerHostInstallError(f"{what} is unreadable: {exc}", code=code) from exc
    return digest.hexdigest(), size


def _entry_digest_and_size(entry: Mapping[str, Any]) -> tuple[str, str, int]:
    """The signed ``filename``, ``sha256`` and ``size`` of a selected host entry.

    The selector already pins the fixed identity, but ``verify_host_archive`` is
    a public function that can be called with any mapping, so it re-checks the
    three fields a download and a digest comparison actually use.
    """
    if entry.get("kind") != SERVER_HOST_KIND:
        raise ServerHostInstallError(
            "the manifest entry is not a server host artifact", code=ARCHIVE_MISMATCH
        )
    filename = entry.get("filename")
    if filename != SERVER_HOST_FILENAME:
        raise ServerHostInstallError(
            f"the host archive must be named {SERVER_HOST_FILENAME!r}",
            code=ARCHIVE_MISMATCH,
        )
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
        raise ServerHostInstallError(
            "the manifest entry has no lowercase SHA-256 digest", code=ARCHIVE_MISMATCH
        )
    # A boolean is an int subclass, so a signed `true` must not stand in for a
    # byte count. Spelled the way `server_host._strict_int` spells it.
    size = entry.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ServerHostInstallError(
            "the manifest entry has no positive integer size", code=ARCHIVE_MISMATCH
        )
    return filename, sha256, size


def verify_host_archive(archive_path: Path, entry: Mapping[str, Any]) -> None:
    """Prove the downloaded archive is the one the signed entry names.

    A filename mismatch, a missing regular file, a symlink, a SHA-256 that is not
    the signed one and a byte count that is not the signed size are all refusals
    carrying :data:`ARCHIVE_MISMATCH`. Nothing is extracted or installed.
    """
    filename, expected_sha, expected_size = _entry_digest_and_size(entry)
    archive = Path(archive_path)
    if archive.name != filename:
        raise ServerHostInstallError(
            f"the host archive must be named {filename!r}, not {archive.name!r}",
            code=ARCHIVE_MISMATCH,
        )
    digest, size = _hash_regular_file(
        archive, what="the server host archive", code=ARCHIVE_MISMATCH
    )
    if digest != expected_sha:
        raise ServerHostInstallError(
            "the downloaded server host does not match the signed manifest",
            code=ARCHIVE_MISMATCH,
        )
    if size != expected_size:
        raise ServerHostInstallError(
            "the downloaded server host does not match the signed manifest",
            code=ARCHIVE_MISMATCH,
        )


def extract_host_archive(archive_path: Path, destination: Path) -> Path:
    """Extract the host archive into a fresh ``destination`` and return the app.

    Every member must be an ordinary file or directory: a symlink, a hard link,
    a device, a FIFO or a name that is absolute or contains ``..`` is refused
    before ``tarfile.extractall(..., filter="data")`` runs. The extraction is
    expected to be the whole app: after unpacking, ``destination`` must hold the
    one ``Ciaobot Server.app`` directory and nothing else.
    """
    archive = Path(archive_path)
    target = Path(destination)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.issym() or member.islnk():
                    raise ServerHostInstallError(
                        f"the host archive member is a link: {member.name}",
                        code=ARCHIVE_UNSAFE,
                    )
                if not (member.isfile() or member.isdir()):
                    raise ServerHostInstallError(
                        f"the host archive member is a special file: {member.name}",
                        code=ARCHIVE_UNSAFE,
                    )
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ServerHostInstallError(
                        f"the host archive member is unsafe: {name}",
                        code=ARCHIVE_UNSAFE,
                    )
            try:
                tar.extractall(target, filter="data")
            except tarfile.FilterError as exc:
                raise ServerHostInstallError(
                    f"the host archive member is unsafe: {exc}", code=ARCHIVE_UNSAFE
                ) from exc
    except ServerHostInstallError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise ServerHostInstallError(
            f"the host archive could not be extracted: {exc}", code=ARCHIVE_UNSAFE
        ) from exc

    entries = sorted(item.name for item in target.iterdir())
    if entries != [APP_NAME]:
        raise ServerHostInstallError(
            f"the host archive must hold exactly {APP_NAME!r}: found {entries}",
            code=ARCHIVE_UNSAFE,
        )
    app = target / APP_NAME
    if not app.is_dir() or app.is_symlink():
        raise ServerHostInstallError(
            f"the host archive did not extract {APP_NAME!r} as a directory",
            code=ARCHIVE_UNSAFE,
        )
    return app


def _fsync_file(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the staged host file could not be opened for flushing: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the staged host file could not be flushed: {exc}", code=INSTALL_FAILED
        ) from exc
    finally:
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    """Best-effort directory flush; a platform without it is not a failure."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    """Flush every regular file under ``root``, then the directories bottom-up.

    A rename makes the names durable but not the bytes behind them, so the four
    sealed files are fsynced before the staged directory is moved into place.
    """
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            _fsync_file(Path(current) / name)
    for current, _dirs, _names in os.walk(root, topdown=False):
        _fsync_dir(Path(current))


def _record_bytes(snapshot: HostOwnership) -> bytes:
    """The exact bytes a record for ``snapshot`` is written as.

    One function for both the write and the digest the journal keeps, so "the
    record this transaction installed" is a hash of the bytes actually on disk,
    not of a second serialization that could drift from it.
    """
    return (json.dumps(snapshot.to_record(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_ownership_record(
    snapshot: HostOwnership,
    path: Path,
    *,
    return_identity: bool = False,
    preserve_parent_mode: bool = False,
) -> tuple[int, int] | None:
    """Write the record owner-only, through a temp file and an atomic replace.

    The parent is created ``0700`` and the file ``0600``, matching the reader's
    macOS owner/mode contract. The JSON is written before the rename and fsynced,
    so a reader never observes a half-written record and a crash cannot leave a
    truncated one in its place.

    With ``return_identity=True`` this also returns the device/inode of the file
    that was written. ``replace_file`` moves that same inode onto the target, so a
    native fresh install's cleanup can bind itself to exactly this record rather
    than to whatever the name holds then. Legacy callers omit the flag and get
    ``None``, preserving their existing path.

    ``preserve_parent_mode`` is the native fresh mode: when the record parent
    already exists, the ancestry preflight has proved it private, so this does not
    chmod it again. A missing parent is created and made private.
    """
    target = Path(path)
    parent_existed = False
    if preserve_parent_mode:
        try:
            info = os.lstat(target.parent)
        except FileNotFoundError:
            parent_existed = False
        except OSError as exc:
            raise ServerHostInstallError(
                f"the ownership record directory could not be examined: {exc}",
                code=INSTALL_FAILED,
            ) from exc
        else:
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ServerHostInstallError(
                    f"the ownership record parent is not a real directory: "
                    f"{target.parent}",
                    code=INSTALL_FAILED,
                )
            if hasattr(os, "getuid") and info.st_uid != _getuid():
                raise ServerHostInstallError(
                    f"the ownership record parent is not owned by this user: "
                    f"{target.parent}",
                    code=INSTALL_FAILED,
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ServerHostInstallError(
                    f"the ownership record parent is not private: {target.parent}",
                    code=INSTALL_FAILED,
                )
            parent_existed = True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (preserve_parent_mode and parent_existed):
            make_private_dir(target.parent)
        descriptor, tmp_name = mkstemp_private(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise ServerHostInstallError(
            f"the ownership record directory could not be prepared: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    tmp = Path(tmp_name)
    identity: tuple[int, int] | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(_record_bytes(snapshot).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            if return_identity:
                opened = os.fstat(handle.fileno())
                identity = (opened.st_dev, opened.st_ino)
        make_private(tmp)
        replace_file(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the ownership record could not be written: {exc}", code=INSTALL_FAILED
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return identity


@dataclasses.dataclass(frozen=True)
class _EntryPolicy:
    """The host identity the authenticated manifest entry selects.

    Read from the entry alone: the revision, protocol and — for the signed host —
    the Developer ID team the release authenticated. The team is never taken from
    the candidate bundle, the keychain or the environment.
    """

    revision: int
    protocol: int
    signing: DeveloperIDSigning | None

    @property
    def signed(self) -> bool:
        return self.signing is not None


def _entry_policy(entry: Mapping[str, Any]) -> _EntryPolicy:
    """The one policy an authenticated host entry selects, or a refusal.

    Two shapes and nothing in between: the production revision-4 entry with no
    signing object, and the signed revision-5 entry with exactly one. A revision
    without its policy is a signed release this installer must not guess at, so
    it is an ``ARCHIVE_MISMATCH`` (fatal) rather than a warning.
    """
    revision = entry.get("host_revision")
    protocol = entry.get("host_protocol")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ServerHostInstallError(
            "the manifest entry has no integer host revision", code=ARCHIVE_MISMATCH
        )
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise ServerHostInstallError(
            "the manifest entry has no integer host protocol", code=ARCHIVE_MISMATCH
        )
    signing_value = entry.get("signing")
    if signing_value is None:
        if revision != HOST_REVISION or protocol != HOST_PROTOCOL:
            raise ServerHostInstallError(
                f"the manifest selects host revision {revision} / protocol "
                f"{protocol} without a signing policy",
                code=ARCHIVE_MISMATCH,
            )
        return _EntryPolicy(revision=revision, protocol=protocol, signing=None)
    if (
        not isinstance(signing_value, Mapping)
        or set(signing_value) != {"kind", "team_id"}
        or signing_value.get("kind") != DEVELOPER_ID_SIGNING_KIND
    ):
        raise ServerHostInstallError(
            "the manifest entry has a malformed signing policy",
            code=ARCHIVE_MISMATCH,
        )
    team_id = signing_value.get("team_id")
    if not isinstance(team_id, str):
        raise ServerHostInstallError(
            "the manifest entry has a malformed signing team", code=ARCHIVE_MISMATCH
        )
    try:
        signing = DeveloperIDSigning(team_id)
    except ServerHostError as exc:
        raise ServerHostInstallError(
            "the manifest entry has a malformed signing team", code=ARCHIVE_MISMATCH
        ) from exc
    if revision != SIGNED_HOST_REVISION or protocol != HOST_PROTOCOL:
        raise ServerHostInstallError(
            f"the manifest's signing policy does not match host revision {revision} "
            f"/ protocol {protocol}",
            code=ARCHIVE_MISMATCH,
        )
    return _EntryPolicy(revision=revision, protocol=protocol, signing=signing)


#: What a verified target is, relative to the policy this run selected.
#: ``current`` is a host whose record proves the same policy (a no-op, even when
#: the release rebuilt it with different CDHashes); ``replace`` is a host this
#: installer owns and may replace (a superseded revision, or the legacy
#: production host when the release selects the signed one).
_CURRENT = "current"
_REPLACE = "replace"
_ABSENT = "absent"


def _classify_host(
    bundle: Path,
    record_path: Path,
    runner: Runner,
    policy: _EntryPolicy,
) -> tuple[str, HostOwnership | None]:
    """Classify what is at the target for the selected policy.

    A symlinked target is never followed. Nothing there is ``absent``. A target
    the private record proves is *this* policy is ``current`` and left alone —
    deliberately without comparing CDHashes, because a rebuild of the same
    revision and team is not a reason to replace a host the user granted
    permissions to. A target whose record proves a superseded revision, or the
    legacy revision-4 host while the release selects the signed one, is
    ``replace``. Everything else is refused and left untouched, and so is a
    signed host this run may not rotate: a signed-to-legacy downgrade and a
    different Developer ID team are :data:`POLICY_REFUSED`.
    """
    if os.path.islink(bundle):
        raise ServerHostInstallError(
            f"the host bundle must not be a symlink: {bundle}", code=TARGET_UNSAFE
        )
    if not os.path.lexists(bundle):
        return _ABSENT, None
    try:
        current = verify_owned_host(bundle, ownership_path=record_path, runner=runner)
    except ServerHostError as current_error:
        try:
            superseded = verify_superseded_host(
                bundle, ownership_path=record_path, runner=runner
            )
        except ServerHostError as exc:
            raise ServerHostInstallError(
                f"there is already a host at {bundle} that this installer cannot "
                f"prove it owns ({current_error}); it is left untouched",
                code=HOST_EXISTS,
            ) from exc
        return _REPLACE, superseded
    if not policy.signed:
        if current.signing is not None:
            raise ServerHostInstallError(
                f"the host at {bundle} is a Developer ID signed host and this "
                f"release selects the ad-hoc one; a signed host is never replaced "
                f"by a legacy one",
                code=POLICY_REFUSED,
            )
        return _CURRENT, current
    assert policy.signing is not None
    if current.signing is not None:
        if current.signing.team_id != policy.signing.team_id:
            raise ServerHostInstallError(
                f"the host at {bundle} is signed by a different Developer ID team "
                f"than this release; a team rotation is never automatic",
                code=POLICY_REFUSED,
            )
        return _CURRENT, current
    # The legacy production host this Mac already runs, and a release that
    # explicitly selected the signed one: the one replacement that has to be a
    # durable transaction rather than a one-step swap.
    return _REPLACE, current


def _getuid() -> int:
    """This user's uid; see :func:`ciao.server_host._getuid` for the same seam."""
    getuid: Any = getattr(os, "getuid")
    return int(getuid())


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that could still be the journal's owner.

    A permission error, an unsupported platform or any unexpected failure is
    conservatively *live*: the answer authorizes a mutation, so "could not tell"
    must never read as "it is gone".
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # No macOS install path runs here, and Windows' ``os.kill`` has no
        # signal-0 probe; never authorize a mutation from this platform.
        return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return getattr(exc, "errno", None) != errno.ESRCH
    return True


def _upgrade_state_dir() -> Path:
    """The fixed owner-only state directory: ``~/.local/state/ciaobot``."""
    return Path.home() / ".local" / "state" / "ciaobot"


def upgrade_journal_path() -> Path:
    """The one durable upgrade journal this installer ever writes."""
    return _upgrade_state_dir() / UPGRADE_JOURNAL_NAME


def upgrade_lock_path() -> Path:
    """The sibling lock every mutating host command takes."""
    return _upgrade_state_dir() / UPGRADE_LOCK_NAME


def _ensure_state_dir(
    directory: Path,
    *,
    preserve_existing: bool = False,
    trusted_root: Path | None = None,
) -> None:
    """Create and tighten the fixed state directory, refusing anything else.

    ``preserve_existing`` is the native fresh mode: missing controlled
    components are created one at a time, descriptor-relative and privately,
    while an already-present directory is validated and never chmod'ed.
    """
    if preserve_existing:
        root = Path.home() if trusted_root is None else Path(trusted_root)
        _prepare_fresh_directory_chain(directory, root, private_final=True)
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        make_private_dir(directory)
        info = os.lstat(directory)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade state directory could not be prepared: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ServerHostInstallError(
            f"the host upgrade state path is not a directory: {directory}",
            code=TRANSACTION_FAILED,
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ServerHostInstallError(
            f"the host upgrade state directory is group/other accessible: {directory}",
            code=TRANSACTION_FAILED,
        )


@contextlib.contextmanager
def _upgrade_lock(
    *, preserve_existing: bool = False, trusted_root: Path | None = None
) -> Iterator[None]:
    """Hold the short-lived upgrade lock, refusing rather than waiting.

    The lock serializes the check-and-prepare step of every mutating command. It
    is deliberately *not* the thing that keeps a second installer away from a
    first one's transaction between the shell's subprocess calls: that is the
    journal's owner pid, which outlives this descriptor.
    """
    journal = upgrade_journal_path()
    _ensure_state_dir(
        journal.parent,
        preserve_existing=preserve_existing,
        trusted_root=trusted_root,
    )
    lock = upgrade_lock_path()
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = (
            _open_fresh_upgrade_lock(lock)
            if preserve_existing
            else open_fd(lock, flags, 0o600, follow_symlinks=False)
        )
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade lock could not be opened: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    try:
        try:
            if not preserve_existing:
                make_private(lock)
            lock_exclusive(descriptor, blocking=False)
            if preserve_existing:
                _verify_fresh_lock_name(lock, descriptor)
        except BlockingIOError as exc:
            raise ServerHostInstallError(
                "another Ciaobot installer is changing the server host on this Mac",
                code=TRANSACTION_BUSY,
            ) from exc
        except OSError as exc:
            raise ServerHostInstallError(
                f"the host upgrade lock could not be taken: {exc}",
                code=TRANSACTION_FAILED,
            ) from exc
        yield
    finally:
        os.close(descriptor)


def _verify_fresh_lock_name(lock: Path, descriptor: int) -> None:
    """Prove the acquired lock descriptor is still the single file at ``lock``.

    The name can be replaced between the pre-open check and ``flock``. This
    compares the held descriptor with the public name after the lock is held, so
    a replaced or doubly linked name is a refusal rather than a lock on an orphan
    inode. A same-UID writer can still replace it after this check; that is the
    documented check-then-act limit.
    """
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(lock)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade lock could not be re-checked: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_nlink != 1
        or named.st_nlink != 1
        or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ServerHostInstallError(
            "the host upgrade lock name changed while it was being acquired",
            code=TRANSACTION_FAILED,
        )


def _open_fresh_upgrade_lock(lock: Path) -> int:
    """Open the existing lock read-write only after proving it is safe, or create it once.

    Fresh native mode never chmods an existing lock file. An existing name must
    already be a regular, single-link, owner-only private file with no extended
    ACL grant to another account. The descriptor is compared with the ``lstat``
    result, so a name swapped between those steps is refused. A missing name is
    created with ``O_EXCL`` and ``O_NOFOLLOW`` and made exactly ``0600`` with
    ``fchmod`` on that descriptor, so no path is followed to set its mode. Any
    uncertainty is a refusal; a file this call created and cannot verify is
    removed only when the public name still names the same single-link inode.
    """
    try:
        checked = os.lstat(lock)
    except FileNotFoundError:
        return _create_fresh_upgrade_lock(lock)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade lock cannot be examined: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    if stat.S_ISLNK(checked.st_mode) or not stat.S_ISREG(checked.st_mode):
        raise ServerHostInstallError(
            "the existing host upgrade lock is not a regular file",
            code=TRANSACTION_FAILED,
        )
    if checked.st_nlink != 1:
        raise ServerHostInstallError(
            "the existing host upgrade lock has another hard link",
            code=TRANSACTION_FAILED,
        )
    open_flags = (
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = open_fd(lock, open_flags, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the existing host upgrade lock could not be opened: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise ServerHostInstallError(
                "the host upgrade lock changed while it was being opened",
                code=TRANSACTION_FAILED,
            )
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ServerHostInstallError(
                "the existing host upgrade lock is not a single regular file",
                code=TRANSACTION_FAILED,
            )
        try:
            private = is_private_descriptor_strict(descriptor)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the host upgrade lock privacy could not be checked: {exc}",
                code=TRANSACTION_FAILED,
            ) from exc
        if not private:
            raise ServerHostInstallError(
                "the existing host upgrade lock is not owned, private and ACL-clean",
                code=TRANSACTION_FAILED,
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_fresh_upgrade_lock(lock: Path) -> int:
    """Create the lock exactly once, privately, on a descriptor-bound path."""
    create_flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = open_fd(
            lock, create_flags, 0o600, follow_symlinks=False
        )
    except FileExistsError as exc:
        raise ServerHostInstallError(
            "the host upgrade lock appeared while this run was creating it",
            code=TRANSACTION_FAILED,
        ) from exc
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade lock could not be created: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    try:
        fchmod: Any = getattr(os, "fchmod", None)
        if fchmod is None:
            raise ServerHostInstallError(
                "the host upgrade lock mode cannot be set on its descriptor",
                code=TRANSACTION_FAILED,
            )
        try:
            opened = os.fstat(descriptor)
            named = os.lstat(lock)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the new host upgrade lock could not be verified: {exc}",
                code=TRANSACTION_FAILED,
            ) from exc
        # Prove the descriptor still names the single file this O_EXCL created
        # before changing any mode. A hard link or replacement at this point is
        # ambiguous, so no chmod is attempted on it.
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
        ):
            raise ServerHostInstallError(
                "the new host upgrade lock is not the single file this run created",
                code=TRANSACTION_FAILED,
            )
        try:
            fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            named = os.lstat(lock)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the new host upgrade lock could not be verified: {exc}",
                code=TRANSACTION_FAILED,
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            or stat.S_ISLNK(named.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
        ):
            raise ServerHostInstallError(
                "the new host upgrade lock is not the single file this run created",
                code=TRANSACTION_FAILED,
            )
        try:
            private = is_private_descriptor_strict(descriptor)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the new host upgrade lock privacy could not be checked: {exc}",
                code=TRANSACTION_FAILED,
            ) from exc
        if not private:
            raise ServerHostInstallError(
                "the new host upgrade lock is not owned, private and ACL-clean",
                code=TRANSACTION_FAILED,
            )
        return descriptor
    except BaseException:
        _remove_created_lock_best_effort(lock, descriptor)
        os.close(descriptor)
        raise


def _remove_created_lock_best_effort(lock: Path, descriptor: int) -> None:
    """Remove a lock this call already proved it created, when the name still matches.

    This is cleanup of our own failed creation, not of pre-existing state. The
    device/inode, regular-file and single-link check narrows it to that object;
    a same-UID replacement between the check and ``unlink`` remains the documented
    check-then-act race and is not claimed to be closed.
    """
    try:
        opened = os.fstat(descriptor)
        named = os.lstat(lock)
    except OSError:
        return
    if (
        (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
    ):
        return
    try:
        os.unlink(lock)
    except OSError:
        pass


def _read_private_bytes(path: Path, *, what: str) -> bytes:
    """Read a private file's exact bytes, refusing a symlink, special file or swap.

    The same checks :mod:`ciao.server_host` applies to the ownership record (its
    owner, no group/other bit, no follow, one inode from ``lstat`` to ``fstat``)
    because the retained record has to be the bytes the verified snapshot was
    read from, not whatever is there later.
    """
    return _read_private_bytes_and_identity(path, what=what)[0]


def _read_private_bytes_and_identity(
    path: Path, *, what: str
) -> tuple[bytes, tuple[int, int]]:
    """Read a private file and return its bytes *and* the inode that supplied them.

    The identity is taken from the open descriptor (``fstat``) that was compared
    with the ``lstat`` result, so a later action can bind itself to exactly the file
    the bytes came from instead of to whatever the name holds then.
    """
    try:
        checked = os.lstat(path)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} cannot be examined: {exc}", code=TRANSACTION_FAILED
        ) from exc
    if stat.S_ISLNK(checked.st_mode):
        raise ServerHostInstallError(
            f"{what} must not be a symlink: {path}", code=TRANSACTION_FAILED
        )
    if not stat.S_ISREG(checked.st_mode):
        raise ServerHostInstallError(
            f"{what} must be a regular file: {path}", code=TRANSACTION_FAILED
        )
    if hasattr(os, "getuid"):
        if checked.st_uid != _getuid():
            raise ServerHostInstallError(
                f"{what} is not owned by this user: {path}", code=TRANSACTION_FAILED
            )
        if stat.S_IMODE(checked.st_mode) & 0o077:
            raise ServerHostInstallError(
                f"{what} is group/other accessible: {path}", code=TRANSACTION_FAILED
            )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = open_fd(path, flags, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} is unreadable: {exc}", code=TRANSACTION_FAILED
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
            raise ServerHostInstallError(
                f"{what} changed while it was being read", code=TRANSACTION_FAILED
            )
        identity = (opened.st_dev, opened.st_ino)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(), identity
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} is unreadable: {exc}", code=TRANSACTION_FAILED
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_private_bytes(path: Path, data: bytes, *, what: str) -> None:
    """Write ``data`` owner-only through a temp file and an atomic replace."""
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(target.parent)
        descriptor, tmp_name = mkstemp_private(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} could not be prepared: {exc}", code=TRANSACTION_FAILED
        ) from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        make_private(tmp)
        replace_file(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} could not be written: {exc}", code=TRANSACTION_FAILED
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


def _strict_document(raw: bytes, *, what: str) -> dict[str, Any]:
    """The strict JSON object of a document, refusing duplicate keys anywhere."""

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ServerHostInstallError(
                    f"{what} repeats the key {key!r}", code=TRANSACTION_FAILED
                )
            seen[key] = value
        return seen

    try:
        document: Any = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except ServerHostInstallError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ServerHostInstallError(
            f"{what} is not valid JSON: {exc}", code=TRANSACTION_FAILED
        ) from exc
    if not isinstance(document, dict):
        raise ServerHostInstallError(
            f"{what} is not a JSON object", code=TRANSACTION_FAILED
        )
    return document


def _strict_positive_pid(value: object, *, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServerHostInstallError(
            f"{what} must be a positive integer process id", code=TRANSACTION_FAILED
        )
    return value


def _strict_operation(value: object) -> str:
    if not isinstance(value, str) or not _OPERATION_RE.fullmatch(value):
        raise ServerHostInstallError(
            "the host upgrade operation is not a nonce this installer wrote",
            code=TRANSACTION_FAILED,
        )
    return value


def _snapshot_shape(value: object, *, what: str) -> dict[str, Any]:
    """Validate one ownership snapshot the journal carries.

    Only used to decide whether a stored snapshot is structurally safe to compare
    against. The authoritative comparison at every mutation seam is
    ``to_record()`` equality with a record this process decoded itself.
    """
    if not isinstance(value, dict):
        raise ServerHostInstallError(
            f"{what} is not an ownership snapshot", code=TRANSACTION_FAILED
        )
    schema = value.get("schema") if "schema" in value else None
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise ServerHostInstallError(
            f"{what} has no integer schema", code=TRANSACTION_FAILED
        )
    expected = {
        "schema",
        "bundle_path",
        "bundle_id",
        "host_revision",
        "host_protocol",
        "executable_sha256",
        "per_arch_cdhashes",
        "bundle_files",
    }
    if schema == SIGNED_SCHEMA_VERSION:
        expected = expected | {"signing"}
    if set(value) != expected:
        raise ServerHostInstallError(
            f"{what} has the wrong fields", code=TRANSACTION_FAILED
        )
    if schema not in (1, SIGNED_SCHEMA_VERSION):
        raise ServerHostInstallError(
            f"{what} is schema {schema}", code=TRANSACTION_FAILED
        )
    for name in ("bundle_path", "bundle_id", "executable_sha256"):
        if not isinstance(value[name], str):
            raise ServerHostInstallError(
                f"{what} has a malformed {name}", code=TRANSACTION_FAILED
            )
    for name in ("host_revision", "host_protocol"):
        if isinstance(value[name], bool) or not isinstance(value[name], int):
            raise ServerHostInstallError(
                f"{what} has a malformed {name}", code=TRANSACTION_FAILED
            )
    for name in ("per_arch_cdhashes", "bundle_files"):
        mapping = value[name]
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in mapping.items()
        ):
            raise ServerHostInstallError(
                f"{what} has a malformed {name}", code=TRANSACTION_FAILED
            )
    if schema == SIGNED_SCHEMA_VERSION:
        signing = value["signing"]
        if (
            not isinstance(signing, dict)
            or set(signing) != {"kind", "team_id"}
            or signing.get("kind") != DEVELOPER_ID_SIGNING_KIND
            or not isinstance(signing.get("team_id"), str)
        ):
            raise ServerHostInstallError(
                f"{what} has a malformed signing identity", code=TRANSACTION_FAILED
            )
    return dict(value)


@dataclasses.dataclass(frozen=True)
class _UpgradeJournal:
    """One durable legacy-to-signed host upgrade.

    Everything needed for a fresh process to finish it: the fixed canonical
    targets, the nonce-derived before-image directory, the exact record digests,
    the two ownership snapshots, the signed policy and the outer installer's pid.
    """

    operation: str
    phase: str
    owner_pid: int
    bundle_path: Path
    record_path: Path
    backup_dir: Path
    #: The device and inode of the before-image directory this transaction
    #: created. Every later cleanup compares them, so a directory substituted at
    #: that path is refused instead of adopted or deleted.
    backup_dev: int
    backup_inode: int
    archive_sha256: str
    old_record_sha256: str
    new_record_sha256: str
    old_record: Mapping[str, Any]
    new_record: Mapping[str, Any]
    signing: DeveloperIDSigning
    #: The device/inode of the journal file this value was decoded from, or of the
    #: file a write just produced. Only used to bind the unlink to that exact file;
    #: never serialized.
    observed: tuple[int, int] | None = dataclasses.field(
        default=None, compare=False, repr=False
    )

    def document(self) -> dict[str, Any]:
        return {
            "schema": UPGRADE_SCHEMA_VERSION,
            "operation": self.operation,
            "phase": self.phase,
            "owner_pid": self.owner_pid,
            "bundle_path": os.fspath(self.bundle_path),
            "record_path": os.fspath(self.record_path),
            "backup_dir": os.fspath(self.backup_dir),
            "backup_dev": self.backup_dev,
            "backup_inode": self.backup_inode,
            "archive_sha256": self.archive_sha256,
            "old_record_sha256": self.old_record_sha256,
            "new_record_sha256": self.new_record_sha256,
            "old": dict(self.old_record),
            "new": dict(self.new_record),
            "policy": {
                "revision": SIGNED_HOST_REVISION,
                "protocol": HOST_PROTOCOL,
                "schema": SIGNED_SCHEMA_VERSION,
                "team_id": self.signing.team_id,
            },
        }


def _decode_journal(document: Mapping[str, Any]) -> _UpgradeJournal:
    """Validate a journal document field by field, refusing anything unexpected.

    Nothing here is inferred: the schema, phase, nonce, canonical targets, backup
    path (which must be exactly the nonce-derived name under the target's own
    parent), digests, snapshots and signed policy are all checked before a single
    path is acted on, so a hand-edited or foreign journal can never name a file
    this installer would delete or adopt.
    """
    if set(document) != _JOURNAL_FIELDS:
        raise ServerHostInstallError(
            "the server host upgrade journal has unsupported fields",
            code=TRANSACTION_FAILED,
        )
    schema = document["schema"]
    if isinstance(schema, bool) or schema != UPGRADE_SCHEMA_VERSION:
        raise ServerHostInstallError(
            "the server host upgrade journal has an unsupported schema",
            code=TRANSACTION_FAILED,
        )
    operation = _strict_operation(document["operation"])
    phase = document["phase"]
    if not isinstance(phase, str) or phase not in UPGRADE_PHASES:
        raise ServerHostInstallError(
            "the server host upgrade journal names an unknown phase",
            code=TRANSACTION_FAILED,
        )
    owner_pid = _strict_positive_pid(document["owner_pid"], what="the journal owner")
    bundle_value = document["bundle_path"]
    record_value = document["record_path"]
    backup_value = document["backup_dir"]
    for name, value in (
        ("bundle", bundle_value),
        ("record", record_value),
        ("before-image", backup_value),
    ):
        if (
            not isinstance(value, str)
            or not os.path.isabs(value)
            or any(ord(character) < 32 for character in value)
        ):
            raise ServerHostInstallError(
                f"the server host upgrade journal names no absolute {name} path",
                code=TRANSACTION_FAILED,
            )
    bundle = Path(bundle_value)
    backup = Path(backup_value)
    if bundle.name != APP_NAME:
        raise ServerHostInstallError(
            "the server host upgrade journal names a bundle that is not the host",
            code=TRANSACTION_FAILED,
        )
    expected_backup = bundle.parent / f"{UPGRADE_BACKUP_PREFIX}{operation}"
    if backup != expected_backup:
        raise ServerHostInstallError(
            "the server host upgrade journal names a before-image path this "
            "installer would not have created",
            code=TRANSACTION_FAILED,
        )
    identity: dict[str, int] = {}
    for name in ("backup_dev", "backup_inode"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ServerHostInstallError(
                f"the server host upgrade journal has a malformed {name}",
                code=TRANSACTION_FAILED,
            )
        identity[name] = value
    digests: dict[str, str] = {}
    for name in ("archive_sha256", "old_record_sha256", "new_record_sha256"):
        value = document[name]
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ServerHostInstallError(
                f"the server host upgrade journal has a malformed {name}",
                code=TRANSACTION_FAILED,
            )
        digests[name] = value
    old_record = _snapshot_shape(document["old"], what="the retained host record")
    new_record = _snapshot_shape(document["new"], what="the installed host record")
    policy = document["policy"]
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise ServerHostInstallError(
            "the server host upgrade journal has a malformed policy",
            code=TRANSACTION_FAILED,
        )
    if (
        isinstance(policy["revision"], bool)
        or policy["revision"] != SIGNED_HOST_REVISION
        or isinstance(policy["protocol"], bool)
        or policy["protocol"] != HOST_PROTOCOL
        or isinstance(policy["schema"], bool)
        or policy["schema"] != SIGNED_SCHEMA_VERSION
    ):
        raise ServerHostInstallError(
            "the server host upgrade journal names a policy this installer does "
            "not upgrade to",
            code=TRANSACTION_FAILED,
        )
    team_id = policy["team_id"]
    if not isinstance(team_id, str):
        raise ServerHostInstallError(
            "the server host upgrade journal has a malformed signing team",
            code=TRANSACTION_FAILED,
        )
    try:
        signing = DeveloperIDSigning(team_id)
    except ServerHostError as exc:
        raise ServerHostInstallError(
            "the server host upgrade journal has a malformed signing team",
            code=TRANSACTION_FAILED,
        ) from exc
    return _UpgradeJournal(
        operation=operation,
        phase=phase,
        owner_pid=owner_pid,
        bundle_path=bundle,
        record_path=Path(record_value),
        backup_dir=backup,
        backup_dev=identity["backup_dev"],
        backup_inode=identity["backup_inode"],
        archive_sha256=digests["archive_sha256"],
        old_record_sha256=digests["old_record_sha256"],
        new_record_sha256=digests["new_record_sha256"],
        old_record=old_record,
        new_record=new_record,
        signing=signing,
    )


def _read_upgrade_journal() -> _UpgradeJournal | None:
    """The journal, or ``None`` when there is none. Corrupt state is a refusal."""
    path = upgrade_journal_path()
    if not os.path.lexists(path):
        return None
    raw, identity = _read_private_bytes_and_identity(
        path, what="the server host upgrade journal"
    )
    journal = _decode_journal(
        _strict_document(raw, what="the server host upgrade journal")
    )
    # The identity comes from the descriptor that supplied these bytes, not from a
    # separate look at the name.
    return dataclasses.replace(journal, observed=identity)


def _write_upgrade_journal(journal: _UpgradeJournal) -> tuple[int, int]:
    """Persist one journal phase durably and return the identity of what was written.

    The identity comes from the descriptor that wrote the bytes, not from a second
    look at the pathname: the temp file is the file ``replace`` moves into place, so
    ``fstat`` on the writing handle names exactly the inode that now holds this
    phase. The later unlink is bound to that inode and refuses a journal somebody
    substituted.
    """
    path = upgrade_journal_path()
    data = (json.dumps(journal.document(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(path.parent)
        descriptor, tmp_name = mkstemp_private(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
    except OSError as exc:
        raise ServerHostInstallError(
            f"the server host upgrade journal could not be prepared: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    tmp = Path(tmp_name)
    identity: os.stat_result | None = None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            identity = os.fstat(handle.fileno())
        make_private(tmp)
        replace_file(tmp, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the server host upgrade journal could not be written: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    assert identity is not None
    return identity.st_dev, identity.st_ino


def _journal_with_phase(journal: _UpgradeJournal, phase: str) -> _UpgradeJournal:
    return dataclasses.replace(journal, phase=phase)


def _same_canonical(left: Path, right: Path) -> bool:
    return Path(os.path.realpath(left)) == Path(os.path.realpath(right))


def _journal_targets_are_this_mac(
    journal: _UpgradeJournal, bundle: Path, record_path: Path
) -> bool:
    return _same_canonical(journal.bundle_path, bundle) and _same_canonical(
        journal.record_path, record_path
    )


# ── directory-descriptor operations: prove the object, then act on it ───────
#
# Every path this transaction deletes or renames is inside a directory the user
# owns, but a pathname is not an object: a swap between "this is the candidate"
# and "delete it" would delete whatever is there later. So the mutating steps open
# the containing directory without following a link, stat the entry through that
# descriptor, compare the device/inode with the object that was verified, and only
# then rename through the same descriptor. A changed entry is refused, and neither
# image is removed in that case.


def _open_dir_no_follow(path: Path, *, what: str) -> int:
    """Open one directory for descriptor-relative operations, refusing a link."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        return open_fd(path, flags, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} could not be opened as a directory: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc


def _dir_entry(dir_fd: int, name: str) -> os.stat_result | None:
    """``lstat`` one entry through an open directory descriptor."""
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade entry {name!r} cannot be examined: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc


def _require_private_dir_entry(
    entry: os.stat_result, *, dev: int, inode: int, what: str
) -> None:
    """Refuse anything but the exact private directory this transaction created."""
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise ServerHostInstallError(
            f"{what} is not a directory", code=TRANSACTION_FAILED
        )
    if (entry.st_dev, entry.st_ino) != (dev, inode):
        raise ServerHostInstallError(
            f"{what} is not the directory this upgrade created", code=TRANSACTION_FAILED
        )
    if hasattr(os, "getuid") and entry.st_uid != _getuid():
        raise ServerHostInstallError(
            f"{what} is not owned by this user", code=TRANSACTION_FAILED
        )
    if stat.S_IMODE(entry.st_mode) & 0o077:
        raise ServerHostInstallError(
            f"{what} is group/other accessible", code=TRANSACTION_FAILED
        )


def _remove_tree_at(
    dir_fd: int,
    name: str,
    *,
    what: str,
    expected: tuple[int, int] | None = None,
) -> None:
    """Delete one entry inside an open directory, never following a link.

    The whole tree is drained through directory descriptors: a name is only ever
    resolved relative to the descriptor of the directory that was verified, so a
    swap at a pathname cannot redirect the deletion to a different tree. For the
    root, ``expected`` is the device/inode this transaction recorded, compared
    against the *opened* directory before anything inside it is drained - a
    substitute created between the caller's check and this open is refused with its
    contents intact. Each child is re-identified immediately before it is unlinked,
    and the final ``rmdir`` is bound to the directory that was opened.
    """
    entry = _dir_entry(dir_fd, name)
    if entry is None:
        return
    if expected is not None:
        # The root is named by the transaction's own device/inode. A regular file, a
        # link or a different directory at that name is refused *before* any type
        # branch can unlink it.
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise ServerHostInstallError(
                f"{what} is not the directory this upgrade created, so it was not "
                f"deleted",
                code=TRANSACTION_FAILED,
            )
        if (entry.st_dev, entry.st_ino) != expected:
            raise ServerHostInstallError(
                f"{what} is not the directory this upgrade created, so nothing inside "
                f"it was deleted",
                code=TRANSACTION_FAILED,
            )
    if stat.S_ISLNK(entry.st_mode):
        raise ServerHostInstallError(
            f"{what} contains a symlink this installer will not follow: {name}",
            code=TRANSACTION_FAILED,
        )
    if not stat.S_ISDIR(entry.st_mode):
        if not stat.S_ISREG(entry.st_mode):
            raise ServerHostInstallError(
                f"{what} contains an entry this installer will not delete: {name}",
                code=TRANSACTION_FAILED,
            )
        guarded = _dir_entry(dir_fd, name)
        if guarded is None or (guarded.st_dev, guarded.st_ino) != (
            entry.st_dev,
            entry.st_ino,
        ):
            raise ServerHostInstallError(
                f"{what} changed before {name!r} could be removed; nothing more was "
                f"deleted",
                code=TRANSACTION_FAILED,
            )
        try:
            os.unlink(name, dir_fd=dir_fd)
        except OSError as exc:
            raise ServerHostInstallError(
                f"{what} could not be removed ({exc})", code=TRANSACTION_FAILED
            ) from exc
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        child_fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} could not be opened ({exc})", code=TRANSACTION_FAILED
        ) from exc
    try:
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise ServerHostInstallError(
                f"{what} changed while it was being removed; nothing more was deleted",
                code=TRANSACTION_FAILED,
            )
        if expected is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != expected:
            raise ServerHostInstallError(
                f"{what} is not the directory this upgrade created, so nothing inside "
                f"it was deleted",
                code=TRANSACTION_FAILED,
            )
        for child in sorted(os.listdir(child_fd)):
            _remove_tree_at(child_fd, child, what=what)
        # The final rmdir names the entry again: require it to still be the
        # directory that was opened (and drained) before removing it.
        drained = _dir_entry(dir_fd, name)
        if drained is None or (drained.st_dev, drained.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ServerHostInstallError(
                f"{what} changed while it was being removed; its name was left alone",
                code=TRANSACTION_FAILED,
            )
    finally:
        os.close(child_fd)
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except OSError as exc:
        raise ServerHostInstallError(
            f"{what} could not be removed ({exc})", code=TRANSACTION_FAILED
        ) from exc


def _require_canonical_targets(journal: _UpgradeJournal) -> None:
    """Prove every path the journal names before anything is acted on.

    The bundle and record must be exactly their own ``realpath`` (so a swapped
    parent link cannot redirect the install), the bundle must still be the host,
    and the before-image directory must be the nonce-derived sibling of that
    bundle. This runs before every cleanup, terminal recovery included.
    """
    bundle = journal.bundle_path
    record = journal.record_path
    if bundle.name != APP_NAME:
        raise ServerHostInstallError(
            "the server host upgrade journal names a bundle that is not the host",
            code=TRANSACTION_FAILED,
        )
    if Path(os.path.realpath(bundle)) != bundle:
        raise ServerHostInstallError(
            f"the host bundle path {bundle} is not canonical any more; nothing was "
            f"changed",
            code=TRANSACTION_FAILED,
        )
    if Path(os.path.realpath(record)) != record:
        raise ServerHostInstallError(
            f"the host ownership record path {record} is not canonical any more; "
            f"nothing was changed",
            code=TRANSACTION_FAILED,
        )
    expected = bundle.parent / f"{UPGRADE_BACKUP_PREFIX}{journal.operation}"
    if journal.backup_dir != expected:
        raise ServerHostInstallError(
            f"the before-images at {journal.backup_dir} are not this transaction's; "
            f"nothing was changed",
            code=TRANSACTION_FAILED,
        )
    if Path(os.path.realpath(journal.backup_dir)) != journal.backup_dir:
        raise ServerHostInstallError(
            f"the before-image path {journal.backup_dir} is not canonical any more; "
            f"nothing was changed",
            code=TRANSACTION_FAILED,
        )


def _open_verified_backup(journal: _UpgradeJournal) -> tuple[int, int]:
    """Open the before-image directory after proving it is the one we created.

    Returns ``(parent_fd, backup_fd)``. The caller closes both. Anything at that
    path that is not the exact private directory whose device/inode the journal
    recorded is refused before a single entry inside it is touched.
    """
    bundle = journal.bundle_path
    parent_fd = _open_dir_no_follow(
        bundle.parent, what="the host bundle's directory"
    )
    try:
        entry = _dir_entry(parent_fd, journal.backup_dir.name)
        if entry is None:
            raise ServerHostInstallError(
                f"the before-images at {journal.backup_dir} are gone; nothing was "
                f"changed",
                code=TRANSACTION_FAILED,
            )
        _require_private_dir_entry(
            entry,
            dev=journal.backup_dev,
            inode=journal.backup_inode,
            what=f"the before-images at {journal.backup_dir}",
        )
        backup_fd = _open_dir_no_follow(
            journal.backup_dir, what=f"the before-images at {journal.backup_dir}"
        )
    except BaseException:
        os.close(parent_fd)
        raise
    try:
        opened = os.fstat(backup_fd)
        if (opened.st_dev, opened.st_ino) != (
            journal.backup_dev,
            journal.backup_inode,
        ):
            raise ServerHostInstallError(
                f"the before-images at {journal.backup_dir} changed while they were "
                f"opened; nothing was changed",
                code=TRANSACTION_FAILED,
            )
    except BaseException:
        os.close(backup_fd)
        os.close(parent_fd)
        raise
    return parent_fd, backup_fd


def _decode_retained_record(
    path: Path, *, old_record: Mapping[str, Any]
) -> HostOwnership:
    """Decode one retained record file with the contract's own reader.

    ``old_record`` is the snapshot the journal stored; the decoded document must
    describe exactly it, so a record that was swapped for a different (but
    structurally valid) one is refused rather than restored.
    """
    try:
        if old_record.get("host_revision") == HOST_REVISION:
            snapshot = read_host_ownership(path)
        else:
            snapshot = read_superseded_ownership(path)
    except ServerHostError as exc:
        raise ServerHostInstallError(
            f"the retained host ownership record at {path} cannot be decoded "
            f"({exc}); it is kept",
            code=TRANSACTION_FAILED,
        ) from exc
    if snapshot.to_record() != dict(old_record):
        raise ServerHostInstallError(
            f"the retained host ownership record at {path} does not describe the "
            f"host this upgrade replaced",
            code=TRANSACTION_FAILED,
        )
    return snapshot


def _retained_record_snapshot(
    journal: _UpgradeJournal,
) -> tuple[HostOwnership, bytes]:
    """Prove the retained record bytes are the journal's, then decode them.

    Returns the snapshot *and* the exact bytes it was decoded from. The caller
    restores those bytes: re-reading the pathname after the check would restore
    whatever is at that name later, which is exactly the window a swap needs. The
    bytes are read through the strict private-file checks (no follow, one inode
    from ``lstat`` to ``fstat``) and hashed against the journal, and they are
    decoded by :mod:`ciao.server_host`'s own reader, so the before-image is the
    record that was verified and never a ``to_record()`` reserialization of it.
    """
    retained = journal.backup_dir / _RETAINED_RECORD
    raw = _read_private_bytes(retained, what="the retained host ownership record")
    if hashlib.sha256(raw).hexdigest() != journal.old_record_sha256:
        raise ServerHostInstallError(
            f"the retained host ownership record at {retained} is not the one this "
            f"upgrade saved",
            code=TRANSACTION_FAILED,
        )
    return _decode_retained_record(retained, old_record=journal.old_record), raw


def _snapshot_policy(
    snapshot: Mapping[str, Any],
) -> tuple[int, int, DeveloperIDSigning | None]:
    """The inspection policy one ownership snapshot describes."""
    revision = snapshot.get("host_revision")
    assert isinstance(revision, int)
    signing_value = snapshot.get("signing")
    signing = (
        None
        if not isinstance(signing_value, Mapping)
        else DeveloperIDSigning(str(signing_value.get("team_id")))
    )
    if signing is not None:
        return SIGNED_HOST_REVISION, HOST_PROTOCOL, signing
    if revision == HOST_REVISION:
        return HOST_REVISION, HOST_PROTOCOL, None
    superseded = SUPERSEDED_HOST_PROTOCOLS.get(revision)
    if superseded is None:
        raise ServerHostInstallError(
            f"the host upgrade journal names an unsupported host revision {revision}",
            code=TRANSACTION_FAILED,
        )
    return revision, superseded, None


def _matches_snapshot(
    bundle: Path, snapshot: Mapping[str, Any], runner: Runner
) -> bool:
    """Whether the bundle's bytes are exactly the ones ``snapshot`` describes.

    Used to tell "the candidate this transaction installed" from "the host it
    replaced" from "something unrelated" before anything is removed. The
    ``bundle_path`` field is ignored here: the question is about the bytes, and
    the caller passes the canonical path the record names.
    """
    revision, protocol, signing = _snapshot_policy(snapshot)
    try:
        inspected = inspect_host_bundle(
            bundle, runner=runner, revision=revision, protocol=protocol, signing=signing
        )
    except ServerHostError:
        return False
    record = dict(inspected.to_record())
    # The bytes are the question; the path is not. A retained copy lives in the
    # before-image directory while the snapshot names the canonical target, so the
    # recorded path is substituted for the one this inspection reported (which the
    # caller already proved is the canonical target's own realpath).
    record["bundle_path"] = snapshot.get("bundle_path")
    return record == dict(snapshot)


def _verify_recorded_host(
    bundle: Path,
    record_path: Path,
    expected: HostOwnership,
    runner: Runner,
    *,
    refusal: str,
) -> HostOwnership:
    """Re-prove a target against the exact snapshot an earlier read returned."""
    try:
        if expected.host_revision == HOST_REVISION and expected.signing is None:
            current = verify_owned_host(bundle, ownership_path=record_path, runner=runner)
        else:
            current = verify_superseded_host(
                bundle, ownership_path=record_path, runner=runner
            )
    except ServerHostError as exc:
        raise ServerHostInstallError(f"{refusal} ({exc})", code=HOST_EXISTS) from exc
    if current != expected:
        raise ServerHostInstallError(refusal, code=HOST_EXISTS)
    return current


def _restore_old_pair(journal: _UpgradeJournal, runner: Runner) -> None:
    """Put the exact old app and its exact old record bytes back.

    Phase-independent on purpose: a kill can land between the rename, the record
    write and the journal update, so what is restored is decided from the retained
    material and the bytes at the canonical target, not from the phase alone.

    Both images are identified, verified and re-identified *before either is moved*,
    so a swap detected at that point leaves both of them exactly where they are.
    Each move is then descriptor-relative and its result is re-identified at the new
    name and re-inspected, so what is adopted or deleted is the object that was
    verified. A target this transaction cannot prove is neither the candidate it
    installed nor the host it replaced is left untouched: no unrelated bundle is
    ever deleted to make room for a rollback.

    Re-running it after a partial restore is a no-op for the bundle (an
    already-restored pair is recognised by its bytes), so the record write is the
    only step that ever repeats.
    """
    _require_canonical_targets(journal)
    bundle = journal.bundle_path
    record_path = journal.record_path
    backup = journal.backup_dir
    retained_path = backup / _RETAINED_BUNDLE
    # The retained record is proved before anything is moved: the exact bytes, the
    # owner/mode/no-follow checks and the contract's own decoder. Those bytes are
    # what gets restored.
    retained_snapshot, retained_record_bytes = _retained_record_snapshot(journal)
    parent_fd, backup_fd = _open_verified_backup(journal)
    try:
        # 1. Identify and verify both images before either move.
        retained_entry = _dir_entry(backup_fd, _RETAINED_BUNDLE)
        retained_identity: tuple[int, int] | None = None
        if retained_entry is not None:
            if stat.S_ISLNK(retained_entry.st_mode) or not stat.S_ISDIR(
                retained_entry.st_mode
            ):
                raise ServerHostInstallError(
                    f"the previous host this upgrade kept at {retained_path} is not a "
                    f"directory; nothing was changed",
                    code=TRANSACTION_FAILED,
                )
            retained_identity = (retained_entry.st_dev, retained_entry.st_ino)
            if not _matches_snapshot(retained_path, journal.old_record, runner):
                raise ServerHostInstallError(
                    f"the previous host kept at {retained_path} is not the host this "
                    f"upgrade replaced, so it is not put back and the candidate is "
                    f"left untouched; the before-images are kept",
                    code=TRANSACTION_FAILED,
                )
        candidate = _dir_entry(parent_fd, APP_NAME)
        candidate_identity: tuple[int, int] | None = None
        candidate_is_new = False
        if candidate is not None:
            if stat.S_ISLNK(candidate.st_mode) or not stat.S_ISDIR(candidate.st_mode):
                raise ServerHostInstallError(
                    f"the host at {bundle} is not a directory; it is left untouched and "
                    f"the previous host and record are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
            candidate_identity = (candidate.st_dev, candidate.st_ino)
            if _matches_snapshot(bundle, journal.new_record, runner):
                candidate_is_new = True
            elif not _matches_snapshot(bundle, journal.old_record, runner):
                raise ServerHostInstallError(
                    f"the host at {bundle} is neither the signed candidate this "
                    f"upgrade installed nor the host it replaced, so it is left "
                    f"untouched; the previous host and record are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
        # 2. Both verifications are done. If either entry changed while they ran,
        # stop here - with both images still in place - rather than move a swapped
        # one.
        if retained_identity is not None:
            current = _dir_entry(backup_fd, _RETAINED_BUNDLE)
            if current is None or (current.st_dev, current.st_ino) != retained_identity:
                raise ServerHostInstallError(
                    f"the previous host kept at {retained_path} changed while the "
                    f"rollback was checking it, so neither image was removed; the "
                    f"before-images are kept",
                    code=TRANSACTION_FAILED,
                )
        if candidate_identity is not None:
            current = _dir_entry(parent_fd, APP_NAME)
            if current is None or (current.st_dev, current.st_ino) != candidate_identity:
                raise ServerHostInstallError(
                    f"the host at {bundle} changed while the rollback was checking it, "
                    f"so neither image was removed; the previous host and record are "
                    f"kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
        # 3. Move the candidate this transaction installed into the verified
        # before-image directory, then prove what moved is what was verified.
        if candidate_is_new:
            retired = backup / _RETIRED_BUNDLE
            try:
                if _dir_entry(backup_fd, _RETIRED_BUNDLE) is None:
                    os.mkdir(_RETIRED_BUNDLE, 0o700, dir_fd=backup_fd)
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                retired_fd = os.open(_RETIRED_BUNDLE, flags, dir_fd=backup_fd)
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the directory for the retired signed host at {retired} could "
                    f"not be prepared ({exc}); nothing was removed",
                    code=TRANSACTION_FAILED,
                ) from exc
            try:
                if _dir_entry(retired_fd, APP_NAME) is not None:
                    raise ServerHostInstallError(
                        f"the retired-host directory at {retired} is not empty; "
                        f"nothing was removed and the before-images are kept",
                        code=TRANSACTION_FAILED,
                    )
                try:
                    os.rename(
                        APP_NAME,
                        APP_NAME,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=retired_fd,
                    )
                except OSError as exc:
                    raise ServerHostInstallError(
                        f"the signed host at {bundle} could not be moved aside "
                        f"({exc}); the previous host and record are kept at {backup}",
                        code=TRANSACTION_FAILED,
                    ) from exc
                moved = _dir_entry(retired_fd, APP_NAME)
                if moved is None or (moved.st_dev, moved.st_ino) != candidate_identity:
                    raise ServerHostInstallError(
                        f"the host at {bundle} did not move as it was verified, so "
                        f"neither image was removed; the previous host and record are "
                        f"kept at {backup}",
                        code=TRANSACTION_FAILED,
                    )
                if not _matches_snapshot(retired / APP_NAME, journal.new_record, runner):
                    raise ServerHostInstallError(
                        f"the host moved aside from {bundle} is not the signed "
                        f"candidate this upgrade installed, so neither image was "
                        f"removed; the before-images are kept at {backup}",
                        code=TRANSACTION_FAILED,
                    )
                # The object that was inspected has to be the object that moved: it
                # is re-identified through the open descriptor *after* the
                # inspection, so a swap during the inspection is refused rather than
                # left to be deleted later.
                inspected = _dir_entry(retired_fd, APP_NAME)
            finally:
                os.close(retired_fd)
            if inspected is None or (
                inspected.st_dev,
                inspected.st_ino,
            ) != candidate_identity:
                raise ServerHostInstallError(
                    f"the moved signed host at {retired} is not the object this "
                    f"rollback verified, so neither image was removed; the "
                    f"before-images are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
        # 4. Move the verified previous host back, then prove the same thing about
        # the object now at the canonical name.
        if candidate_is_new or _dir_entry(parent_fd, APP_NAME) is None:
            if retained_identity is None:
                raise ServerHostInstallError(
                    f"the previous host this upgrade kept at {retained_path} is gone; "
                    f"nothing else was removed",
                    code=TRANSACTION_FAILED,
                )
            current = _dir_entry(backup_fd, _RETAINED_BUNDLE)
            if current is None or (current.st_dev, current.st_ino) != retained_identity:
                raise ServerHostInstallError(
                    f"the previous host kept at {retained_path} changed while the "
                    f"rollback was checking it, so neither image was removed; the "
                    f"before-images are kept",
                    code=TRANSACTION_FAILED,
                )
            try:
                os.rename(
                    _RETAINED_BUNDLE,
                    APP_NAME,
                    src_dir_fd=backup_fd,
                    dst_dir_fd=parent_fd,
                )
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the previous host could not be moved back to {bundle} ({exc}); it "
                    f"is kept at {retained_path}",
                    code=TRANSACTION_FAILED,
                ) from exc
            moved = _dir_entry(parent_fd, APP_NAME)
            if moved is None or (moved.st_dev, moved.st_ino) != retained_identity:
                raise ServerHostInstallError(
                    f"the previous host did not move back as it was verified; the "
                    f"before-images are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
            if not _matches_snapshot(bundle, journal.old_record, runner):
                raise ServerHostInstallError(
                    f"the host now at {bundle} is not the one this upgrade replaced; "
                    f"the before-images are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
            # Same binding for the image that was put back: what was inspected is
            # re-identified through the directory descriptor before its record is
            # written over it.
            inspected = _dir_entry(parent_fd, APP_NAME)
            if inspected is None or (inspected.st_dev, inspected.st_ino) != (
                retained_identity
            ):
                raise ServerHostInstallError(
                    f"the previous host inspected at {bundle} is not the object this "
                    f"rollback moved, so its record is not written over it; the "
                    f"before-images are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
        _fsync_dir(bundle.parent)
        _fsync_dir(backup)
    finally:
        os.close(backup_fd)
        os.close(parent_fd)
    # The record goes back as the exact bytes that were verified, held from the
    # no-follow descriptor read - never re-read by pathname and never a
    # reserialization of the snapshot that was decoded from them.
    _write_private_bytes(
        record_path,
        retained_record_bytes,
        what="the previous host ownership record",
    )
    restored = _verify_recorded_host(
        bundle,
        record_path,
        retained_snapshot,
        runner,
        refusal=f"the previous host at {bundle} could not be re-verified after the "
        f"rollback",
    )
    if restored.to_record() != dict(journal.old_record):
        raise ServerHostInstallError(
            f"the host restored at {bundle} is not the one this upgrade replaced; "
            f"the before-images are kept at {backup}",
            code=TRANSACTION_FAILED,
        )


def _finish_journal(
    journal: _UpgradeJournal, *, journal_inode: tuple[int, int] | None = None
) -> str | None:
    """Release this transaction's own before-images after a terminal phase.

    Only the nonce-derived directory the journal names is removed, and only after
    the terminal phase has been written durably, the canonical targets have been
    re-proven and the directory's device/inode still matches what this upgrade
    created. The tree is drained through that directory's descriptor, so no
    pathname resolved after the check can redirect the deletion, and a swapped
    entry refuses instead of being deleted.

    The journal is then unlinked only when the file at that name is still the one
    this command wrote or read (``journal_inode``): a substituted journal is left
    alone and reported. A backup that is already gone is the normal state after an
    interrupted cleanup, so it is not a failure here; the journal is still removed
    and the retry stays idempotent. Nothing is ever reported as missing when it was
    actually substituted - the inode check is what separates the two.
    """
    try:
        _require_canonical_targets(journal)
    except ServerHostInstallError as exc:
        return str(exc)
    backup = journal.backup_dir
    try:
        parent_fd = _open_dir_no_follow(
            backup.parent, what="the host bundle's directory"
        )
    except ServerHostInstallError as exc:
        return str(exc)
    try:
        entry = _dir_entry(parent_fd, backup.name)
        if entry is not None:
            try:
                _require_private_dir_entry(
                    entry,
                    dev=journal.backup_dev,
                    inode=journal.backup_inode,
                    what=f"the before-images at {backup}",
                )
            except ServerHostInstallError as exc:
                return str(exc)
            try:
                _remove_tree_at(
                    parent_fd,
                    backup.name,
                    what=f"the before-images at {backup}",
                    expected=(journal.backup_dev, journal.backup_inode),
                )
            except ServerHostInstallError as exc:
                return f"{exc}; they are kept"
    finally:
        os.close(parent_fd)
    journal_path = upgrade_journal_path()
    expected = journal_inode if journal_inode is not None else journal.observed
    try:
        state_fd = _open_dir_no_follow(
            journal_path.parent, what="the host upgrade state directory"
        )
    except ServerHostInstallError as exc:
        return str(exc)
    try:
        entry = _dir_entry(state_fd, journal_path.name)
        if entry is None:
            return None
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            return f"the upgrade journal at {journal_path} is not a regular file; it is kept"
        if hasattr(os, "getuid") and entry.st_uid != _getuid():
            return f"the upgrade journal at {journal_path} is not owned by this user; it is kept"
        if stat.S_IMODE(entry.st_mode) & 0o077:
            return (
                f"the upgrade journal at {journal_path} is group/other accessible; it "
                f"is kept"
            )
        if expected is not None and (entry.st_dev, entry.st_ino) != expected:
            return (
                f"the upgrade journal at {journal_path} is not the one this command "
                f"settled, so it is kept"
            )
        try:
            os.unlink(journal_path.name, dir_fd=state_fd)
        except OSError as exc:
            return f"the upgrade journal could not be removed ({exc}); it is kept"
    finally:
        os.close(state_fd)
    return None


def _rollback_journal(journal: _UpgradeJournal, runner: Runner) -> str | None:
    """Restore the old pair, mark the journal terminal and release its material."""
    _restore_old_pair(journal, runner)
    terminal = _journal_with_phase(journal, "rolled_back")
    written = _write_upgrade_journal(terminal)
    return _finish_journal(terminal, journal_inode=written)


def install_server_host(
    archive_path: Path,
    entry: Mapping[str, Any],
    *,
    bundle_path: Path | None = None,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
    defer_commit: bool = False,
    owner_pid: int | None = None,
    require_absent: bool = False,
    trusted_root: Path | None = None,
) -> HostOwnership:
    """Verify, inspect and install the host archive, then record and re-verify it.

    ``entry`` is the selected ``server-host`` artifact of a manifest the caller
    has already verified; this function re-checks the archive against its digest
    and size and never trusts the archive on its own. The bundle and record paths
    default to :func:`ciao.server_host.default_bundle_path` and
    :func:`ciao.server_host.default_ownership_path`.

    The host identity the entry selects is read from the entry itself: the
    production revision-4 ad-hoc host, or the signed revision-5 host with the
    Developer ID team the release authenticated. An existing host whose record
    proves the *same* policy is returned untouched (a no-op) whatever CDHashes a
    release rebuild gave it; one this installer cannot prove it owns is refused
    and left untouched. A signed host is never replaced by the legacy one and a
    different team is never adopted — both are :data:`POLICY_REFUSED`.

    A **superseded** revision-2/3 host is replaced by the production host in one
    step, exactly as before (#1659), and restored with its record if the
    replacement fails. A **legacy to signed** replacement is a durable
    transaction instead: ``defer_commit=True`` (with the outer installer's
    ``owner_pid``) retains the old bundle and its exact record bytes, records the
    operation in the journal and returns with the journal at ``installed``; the
    caller commits after its engine-health success or rolls back. Without that
    mode the replacement is refused with :data:`DEFERRED_UPGRADE_REQUIRED` rather
    than taking the short-lived rollback path, whose before-image this module
    deletes before the engine install even starts.

    On a fresh install the staged bundle is inspected before the record is
    written and before the rename, so a foreign or tampered archive never reaches
    ``~/Applications``. The record is written *before* the rename, from the
    staged snapshot with the canonical target path, so a process killed after the
    rename leaves a consistent, verified host rather than a bundle no record
    vouches for; a Python failure removes both the bundle this run renamed into
    place and the record it wrote. When putting a replaced superseded host back
    fails, the rollback stops there: the staging copy and the record are kept and
    the error names where the host is. No service, launchd, plist or permission
    surface is touched.

    ``require_absent=True`` is the native fresh-install mode. Under the same
    upgrade lock it refuses *any* pre-existing host bundle, ownership record or
    upgrade journal - including a terminal journal and an orphan record with no
    bundle - before creating the host bundle or its record. It never returns a
    current host, never replaces a superseded host, and never settles or deletes a
    journal. The legacy no-op/replacement behavior is unchanged with the default
    ``False``. Fresh mode also requires canonical absolute paths with no ``.``,
    ``..``, trailing slash or symlinked ancestor; the bundle must be named
    :data:`APP_NAME`, the record must not live inside the bundle, and each
    existing ancestor from ``trusted_root`` (home by default) must be a real
    directory owned by this user and not group/other writable. The record's
    parent and the journal's parent must already be private when they exist;
    this path never chmods a pre-existing directory in fresh mode. A missing tail
    is created privately by the install. The legacy default does not apply this
    path policy.
    As with the rest of this module, a same-UID writer that ignores the lock can
    still interleave between a check and the write it guards; these are refusal
    points, not proof. In that window a fresh attempt may leave the ownership
    record it wrote when a concurrent bundle wins the rename (the existing
    ``keep_record`` path); the run refuses and never reports that host as its
    own, and it preserves the concurrent bundle and record.
    """
    _require_macos()
    raw_bundle = None if bundle_path is None else os.fspath(bundle_path)
    raw_record = None if ownership_path is None else os.fspath(ownership_path)
    if require_absent and (defer_commit or owner_pid is not None):
        raise ServerHostInstallError(
            "a fresh host install cannot also defer a commit or name an "
            "installer process id",
            code=INSTALL_FAILED,
        )
    path = Path(archive_path)
    verify_host_archive(path, entry)
    policy = _entry_policy(entry)

    bundle = default_bundle_path() if bundle_path is None else Path(bundle_path)
    record_path = (
        default_ownership_path() if ownership_path is None else Path(ownership_path)
    )
    if not bundle.is_absolute():
        raise ServerHostInstallError(
            f"the host bundle path must be absolute: {bundle}", code=INSTALL_FAILED
        )
    if bundle.name != APP_NAME:
        raise ServerHostInstallError(
            f"the host bundle must be named {APP_NAME!r}: {bundle}", code=INSTALL_FAILED
        )
    fresh_root: Path | None = None
    if require_absent:
        fresh_root = Path.home() if trusted_root is None else Path(trusted_root)
        _require_fresh_install_paths(
            bundle,
            record_path,
            fresh_root,
            raw_bundle=raw_bundle if raw_bundle is not None else os.fspath(bundle),
            raw_record=raw_record if raw_record is not None else os.fspath(record_path),
        )
        # Read-only surface check before the upgrade lock creates any directory.
        # The same checks repeat under the lock after path preparation. An
        # outstanding journal is the first evidence to report, even when a host
        # and record are also present.
        _refuse_present_journal()
        _refuse_present_record(record_path)
        if os.path.lexists(bundle):
            raise ServerHostInstallError(
                f"a host bundle already exists at {bundle}; a fresh install "
                f"never adopts or replaces it",
                code=HOST_EXISTS,
            )
    if defer_commit:
        if owner_pid is None:
            raise ServerHostInstallError(
                "a deferred host upgrade needs the installer's own process id",
                code=INSTALL_FAILED,
            )
        owner_pid = _strict_positive_pid(owner_pid, what="the installer process id")
        if not _pid_alive(owner_pid):
            raise ServerHostInstallError(
                f"the installer process {owner_pid} that would commit this host "
                f"upgrade is not running; nothing was installed",
                code=INSTALL_FAILED,
            )
    elif owner_pid is not None:
        raise ServerHostInstallError(
            "an installer process id is only meaningful with a deferred host upgrade",
            code=INSTALL_FAILED,
        )

    with _upgrade_lock(
        preserve_existing=require_absent, trusted_root=fresh_root
    ):
        if require_absent:
            # Fresh native creation is stricter than the legacy installer: every
            # known surface must be absent, and a terminal journal is evidence to
            # refuse rather than material this run may settle.
            _refuse_present_journal()
            _refuse_present_record(record_path)
        else:
            _refuse_unfinished_journal(bundle, record_path)
        kind, existing = _classify_host(bundle, record_path, runner, policy)
        if require_absent and kind != _ABSENT:
            # A same-policy host the legacy path would return as a no-op is a
            # refusal here: a fresh run never adopts a host it did not create.
            raise ServerHostInstallError(
                f"a host bundle already exists at {bundle}; a fresh install "
                f"never adopts or replaces it",
                code=HOST_EXISTS,
            )
        if kind == _CURRENT:
            assert existing is not None
            return existing
        if kind == _REPLACE and policy.signed:
            assert existing is not None
            if not defer_commit:
                raise ServerHostInstallError(
                    f"the host at {bundle} is the ad-hoc revision-{existing.host_revision} "
                    f"host, and this release selects the signed one. That replacement "
                    f"is only done as a recoverable transaction, after the new engine "
                    f"is healthy; use --defer-commit --owner-pid",
                    code=DEFERRED_UPGRADE_REQUIRED,
                )
            assert owner_pid is not None
            return _install_deferred(
                path, entry, policy, bundle, record_path, existing, runner, owner_pid
            )
        return _install_now(
            path,
            policy,
            bundle,
            record_path,
            existing,
            runner,
            require_absent=require_absent,
            trusted_root=fresh_root,
        )


def _refuse_present_journal() -> None:
    """Refuse any journal evidence, including a terminal one, without touching it.

    The legacy check may finish a terminal journal by deleting its before-images.
    A fresh native install must not: an outstanding journal is evidence that
    another host operation ran or is running, so this reads it and refuses. An
    unsafe, unreadable or malformed journal is a refusal from the strict reader,
    never an absent answer.
    """
    journal = _read_upgrade_journal()
    if journal is None:
        return
    raise ServerHostInstallError(
        f"a Ciaobot Server host upgrade journal is present at phase "
        f"{journal.phase}; a fresh install never settles, deletes or reuses it",
        code=TRANSACTION_CONFLICT,
    )


def _refuse_present_record(record_path: Path) -> None:
    """Refuse any ownership record, without following a link or reading it.

    A record with no bundle is exactly the partial state a fresh install must
    not silently replace. This is a name check only: a symlink, directory, special
    file or regular file at the record path is present, and presence is enough to
    refuse. An inaccessible path is also a refusal, never an absent answer.
    """
    try:
        info = os.lstat(record_path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host ownership record at {record_path} cannot be examined: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ServerHostInstallError(
            f"an ownership record at {record_path} is a symlink; a fresh install "
            f"never follows or replaces it",
            code=HOST_EXISTS,
        )
    raise ServerHostInstallError(
        f"an ownership record already exists at {record_path}; a fresh install "
        f"never reuses or replaces it",
        code=HOST_EXISTS,
    )


def _require_canonical_install_path(
    path: Path, *, raw: str, what: str
) -> None:
    """Require a canonical absolute name, before any mutating step.

    ``raw`` is the caller's spelling before :class:`~pathlib.Path` normalized it,
    so a ``./`` or ``//`` component is still visible here. ``..`` is preserved by
    ``Path`` and is caught by ``normpath``; a symlinked ancestor is caught by
    ``realpath``. The final path may not exist yet.
    """
    if not isinstance(raw, str) or not raw:
        raise ServerHostInstallError(
            f"the {what} path is not a canonical absolute path", code=INSTALL_FAILED
        )
    try:
        resolved = os.path.realpath(raw)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} path cannot be resolved: {exc}", code=INSTALL_FAILED
        ) from exc
    if (
        not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or resolved != raw
        or os.fspath(path) != raw
    ):
        raise ServerHostInstallError(
            f"the {what} path is not canonical, absolute and symlink-free: {raw}",
            code=INSTALL_FAILED,
        )


def _validate_dir_descriptor(
    descriptor: int,
    *,
    what: str,
    private_final: bool,
    expected: tuple[int, int] | None = None,
) -> None:
    """Validate one opened directory without changing it.

    The descriptor must be a real directory, owned by this user, not
    group/other-writable, and its extended ACL must not grant another principal.
    With ``private_final`` it must be exactly owner-only private as well. An ACL
    API that cannot answer is a refusal, never a pass.
    """
    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} could not be inspected: {exc}", code=INSTALL_FAILED
        ) from exc
    if not stat.S_ISDIR(opened.st_mode):
        raise ServerHostInstallError(
            f"the {what} is not a directory", code=INSTALL_FAILED
        )
    if expected is not None and (opened.st_dev, opened.st_ino) != expected:
        raise ServerHostInstallError(
            f"the {what} changed while it was being checked", code=INSTALL_FAILED
        )
    if hasattr(os, "getuid") and opened.st_uid != _getuid():
        raise ServerHostInstallError(
            f"the {what} is not owned by this user", code=INSTALL_FAILED
        )
    mode = stat.S_IMODE(opened.st_mode)
    if mode & 0o022:
        raise ServerHostInstallError(
            f"the {what} is group/other writable", code=INSTALL_FAILED
        )
    try:
        acl_grant = descriptor_acl_grants_another_account(descriptor)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} extended ACL could not be inspected: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    if acl_grant:
        raise ServerHostInstallError(
            f"the {what} grants an extended ACL to another account",
            code=INSTALL_FAILED,
        )
    if private_final:
        try:
            private = is_private_descriptor_strict(descriptor)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the {what} privacy could not be inspected: {exc}",
                code=INSTALL_FAILED,
            ) from exc
        if not private:
            raise ServerHostInstallError(
                f"the {what} is not private", code=INSTALL_FAILED
            )


def _check_existing_ancestor(
    path: Path, *, what: str, private_final: bool
) -> bool:
    """Check one existing directory in a fresh path's ancestry.

    Returns ``False`` only when the component is missing (a tail the install may
    create). A symlink, non-directory, foreign owner, group/other-writable
    component, or an unreadable path is a refusal. When ``private_final`` is set
    and this is the final parent, group/other access is refused as well. Nothing
    is chmod'ed, created or repaired.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} at {path} cannot be examined: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ServerHostInstallError(
            f"the {what} at {path} is a symlink; a fresh install never follows it",
            code=INSTALL_FAILED,
        )
    if not stat.S_ISDIR(info.st_mode):
        raise ServerHostInstallError(
            f"the {what} at {path} is not a directory", code=INSTALL_FAILED
        )
    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = open_fd(path, open_flags, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} at {path} could not be opened: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    try:
        _validate_dir_descriptor(
            descriptor,
            what=f"{what} at {path}",
            private_final=private_final,
            expected=(info.st_dev, info.st_ino),
        )
        return True
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _require_safe_ancestry(
    parent: Path, root: Path, *, what: str, private: bool
) -> None:
    """Validate every existing component from ``root`` down to ``parent``.

    The root is required to exist. A missing tail is allowed (the install creates
    it privately). Existing components must be real directories, owned by this
    user, not group/other writable; the final record/journal parent must also be
    private. No component is chmod'ed or repaired.
    """
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise ServerHostInstallError(
            f"the {what} is outside the trusted root {root}", code=INSTALL_FAILED
        ) from exc
    if not _check_existing_ancestor(
        root, what="trusted root", private_final=(private and root == parent)
    ):
        raise ServerHostInstallError(
            f"the trusted root does not exist: {root}", code=INSTALL_FAILED
        )
    current = root
    for part in relative.parts:
        current = current / part
        if not _check_existing_ancestor(
            current, what=what, private_final=(private and current == parent)
        ):
            return


def _prepare_fresh_directory_chain(
    target: Path, root: Path, *, private_final: bool
) -> None:
    """Create only missing controlled directories, privately and descriptor-relative.

    Existing components are opened with no-follow directory descriptors and
    validated in place; they are never chmod'ed. Missing components are created
    one at a time relative to the held parent descriptor, opened the same way,
    given exactly ``0700`` with ``fchmod`` on that descriptor, and re-validated
    for ownership and extended ACL. No process-global umask is touched.

    A umask that strips the owner bits can make a just-created directory
    unopenable. That is a fail-closed refusal with the created name left as
    evidence; this helper never falls back to a path-based chmod. A symlink,
    foreign owner, group/other-writable component, ACL grant, missing ACL API,
    or a name that appears after the missing check is refused. The final
    ``private_final`` component must be exactly private when it already exists.
    """
    target = Path(target)
    root = Path(root)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ServerHostInstallError(
            f"the fresh directory {target} is outside the trusted root {root}",
            code=INSTALL_FAILED,
        ) from exc
    _require_canonical_install_path(root, raw=os.fspath(root), what="trusted root")
    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        current_fd = open_fd(root, open_flags, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the trusted root {root} could not be opened: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    current_path = root
    try:
        _validate_dir_descriptor(
            current_fd,
            what=f"trusted root {root}",
            private_final=private_final and target == root,
        )
        for part in relative.parts:
            current_path = current_path / part
            is_final = current_path == target
            try:
                child_fd = os.open(part, open_flags, dir_fd=current_fd)
            except FileNotFoundError:
                child_fd = _create_fresh_directory_component(
                    current_fd, part, current_path
                )
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the directory {current_path} could not be opened safely: {exc}",
                    code=INSTALL_FAILED,
                ) from exc
            else:
                try:
                    _validate_dir_descriptor(
                        child_fd,
                        what=f"directory {current_path}",
                        private_final=private_final and is_final,
                    )
                except BaseException:
                    os.close(child_fd)
                    raise
            try:
                os.close(current_fd)
            except OSError:
                pass
            current_fd = child_fd
    finally:
        try:
            os.close(current_fd)
        except OSError:
            pass


def _create_fresh_directory_component(
    parent_fd: int, part: str, path: Path
) -> int:
    """Create and prove one missing directory relative to a held parent descriptor."""
    open_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        os.mkdir(part, 0o700, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise ServerHostInstallError(
            f"the directory {path} appeared while this fresh install was creating it",
            code=INSTALL_FAILED,
        ) from exc
    except OSError as exc:
        raise ServerHostInstallError(
            f"the directory {path} could not be created: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    try:
        descriptor = os.open(part, open_flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the newly created directory {path} could not be opened safely "
            f"({exc}); it is left as evidence",
            code=INSTALL_FAILED,
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ServerHostInstallError(
                f"the newly created directory {path} is not a directory; it is "
                f"left as evidence",
                code=INSTALL_FAILED,
            )
        fchmod: Any = getattr(os, "fchmod", None)
        if fchmod is None:
            raise ServerHostInstallError(
                f"the newly created directory {path} cannot have its mode set "
                f"on its descriptor; it is left as evidence",
                code=INSTALL_FAILED,
            )
        try:
            fchmod(descriptor, 0o700)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the newly created directory {path} could not be made private "
                f"({exc}); it is left as evidence",
                code=INSTALL_FAILED,
            ) from exc
        _validate_dir_descriptor(
            descriptor,
            what=f"newly created directory {path}",
            private_final=True,
            expected=(opened.st_dev, opened.st_ino),
        )
        try:
            named = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the newly created directory {path} could not be re-checked "
                f"({exc}); it is left as evidence",
                code=INSTALL_FAILED,
            ) from exc
        if (
            not stat.S_ISDIR(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise ServerHostInstallError(
                f"the newly created directory {path} changed while it was being "
                f"verified; it is left as evidence",
                code=INSTALL_FAILED,
            )
        return descriptor
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _require_fresh_install_paths(
    bundle: Path,
    record_path: Path,
    root: Path,
    *,
    raw_bundle: str,
    raw_record: str,
) -> None:
    """The full pre-mutation path policy for a fresh native host install."""
    if os.path.islink(bundle):
        raise ServerHostInstallError(
            f"the host bundle at {bundle} is a symlink; a fresh install never "
            f"follows it",
            code=TARGET_UNSAFE,
        )
    if os.path.islink(record_path):
        raise ServerHostInstallError(
            f"the ownership record at {record_path} is a symlink; a fresh install "
            f"never follows or replaces it",
            code=HOST_EXISTS,
        )
    _require_canonical_install_path(bundle, raw=raw_bundle, what="host bundle")
    _require_canonical_install_path(
        record_path, raw=raw_record, what="ownership record"
    )
    try:
        real_record = Path(os.path.realpath(record_path))
        real_bundle = Path(os.path.realpath(bundle))
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host paths cannot be resolved: {exc}", code=INSTALL_FAILED
        ) from exc
    if real_record.is_relative_to(real_bundle):
        raise ServerHostInstallError(
            "the ownership record must live outside the host bundle it describes",
            code=INSTALL_FAILED,
        )
    _require_canonical_install_path(
        root, raw=os.fspath(root), what="trusted root"
    )
    _require_safe_ancestry(
        bundle.parent, root, what="host bundle parent", private=False
    )
    _require_safe_ancestry(
        record_path.parent, root, what="ownership record parent", private=True
    )
    journal_parent = upgrade_journal_path().parent
    if journal_parent != record_path.parent:
        _require_safe_ancestry(
            journal_parent,
            root,
            what="host upgrade state directory",
            private=True,
        )


def _remove_own_record(
    record_path: Path,
    snapshot: HostOwnership,
    identity: tuple[int, int] | None,
) -> bool:
    """Remove the record only when it is still the exact file this call wrote.

    ``identity`` is the device/inode captured immediately after this run's write;
    the bytes are the exact serialization of the same snapshot. A missing file is
    already clean. A substituted inode, changed bytes, a link, an extra hard link
    or an unreadable path is left in place and reports ``False`` - a concurrent
    writer may own it, and a fresh cleanup must not delete pre-existing state.
    The check-then-unlink gap remains a same-UID race; this narrows the deletion
    to the object that was proved ours at cleanup time.
    """
    expected = _record_bytes(snapshot)
    try:
        os.lstat(record_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if identity is None:
        return False
    try:
        data, observed = _read_private_bytes_and_identity(
            record_path, what="the freshly written ownership record"
        )
    except (OSError, ServerHostInstallError):
        return False
    if observed != identity or data != expected:
        return False
    try:
        os.unlink(record_path)
    except OSError:
        return False
    return True


def _refuse_unfinished_journal(bundle: Path, record_path: Path) -> None:
    """Refuse to install while any earlier host upgrade is unsettled.

    ``install`` never rolls an earlier operation back itself. Quiescing the
    candidate's service belongs to the shell, and a rollback driven from here
    could revert the identity under a candidate that is still running - most
    sharply in the race where the journal's former owner dies between the shell's
    status check and this call. So an unfinished operation of any owner is a
    refusal: the shell quiesces, calls ``rollback-upgrade`` and retries. A
    *terminal* journal is finished here (that only deletes this transaction's own
    material), and a cleanup that still fails refuses too, so a new transaction can
    never overwrite a journal whose retained material is still on disk.
    """
    journal = _read_upgrade_journal()
    if journal is None:
        return
    if not _journal_targets_are_this_mac(journal, bundle, record_path):
        raise ServerHostInstallError(
            f"the unfinished host upgrade journal at {upgrade_journal_path()} names "
            f"another target; it is left untouched",
            code=TRANSACTION_CONFLICT,
        )
    if journal.phase in UPGRADE_TERMINAL_PHASES:
        problem = _finish_journal(journal)
        if problem is not None:
            raise ServerHostInstallError(
                f"{problem}. The earlier host upgrade is settled, but its retained "
                f"material must be removed before another one is started",
                code=TRANSACTION_CLEANUP,
            )
        return
    if journal.owner_pid != os.getpid() and _pid_alive(journal.owner_pid):
        raise ServerHostInstallError(
            f"installer process {journal.owner_pid} is still upgrading the Ciaobot "
            f"Server host; this run will not touch it",
            code=TRANSACTION_BUSY,
        )
    raise ServerHostInstallError(
        f"a previous Ciaobot Server host upgrade (operation {journal.operation}, "
        f"phase {journal.phase}) did not finish. Stop the Ciaobot Server service, "
        f"roll it back with rollback-upgrade, and install again",
        code=TRANSACTION_CONFLICT,
    )


def _install_deferred(
    path: Path,
    entry: Mapping[str, Any],
    policy: _EntryPolicy,
    bundle: Path,
    record_path: Path,
    old: HostOwnership,
    runner: Runner,
    owner_pid: int,
) -> HostOwnership:
    """Replace a legacy host with the signed one as a durable transaction."""
    assert policy.signing is not None
    _, archive_sha256, _ = _entry_digest_and_size(entry)
    parent = bundle.parent
    operation = secrets.token_hex(16)
    backup = parent / f"{UPGRADE_BACKUP_PREFIX}{operation}"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host staging directory could not be created: {exc}",
            code=INSTALL_FAILED,
        ) from exc

    journal: _UpgradeJournal | None = None
    try:
        staged_app = extract_host_archive(path, stage)
        staged = inspect_host_bundle(
            staged_app,
            runner=runner,
            revision=policy.revision,
            protocol=policy.protocol,
            signing=policy.signing,
        )
        if staged.signing != policy.signing:
            raise ServerHostInstallError(
                "the staged host is not signed by the expected Developer ID team",
                code=POLICY_REFUSED,
            )
        canonical = os.path.join(os.path.realpath(parent), APP_NAME)
        record_snapshot = dataclasses.replace(staged, bundle_path=canonical)
        new_bytes = _record_bytes(record_snapshot)
        _fsync_tree(staged_app)
        # The exact bytes the verified record was read from, retained before the
        # first canonical rename. They are decoded by the contract's own reader
        # below, so the before-image is provably the record that was verified,
        # not a reserialization of it - and a record an older installer wrote with
        # different whitespace is kept exactly as it is.
        old_bytes = _read_private_bytes(
            record_path, what="the existing host ownership record"
        )
        if os.path.islink(backup) or (os.path.lexists(backup) and not backup.is_dir()):
            raise ServerHostInstallError(
                f"the host upgrade before-image path is not a directory: {backup}",
                code=INSTALL_FAILED,
            )
        try:
            backup.mkdir(mode=0o700)
            make_private_dir(backup)
            backup_info = os.lstat(backup)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the host upgrade before-image directory could not be created: {exc}",
                code=INSTALL_FAILED,
            ) from exc
        if stat.S_IMODE(backup_info.st_mode) & 0o077:
            raise ServerHostInstallError(
                f"the host upgrade before-image directory is not private: {backup}",
                code=INSTALL_FAILED,
            )
        _write_private_bytes(
            backup / _RETAINED_RECORD,
            old_bytes,
            what="the retained host ownership record",
        )
        _fsync_dir(backup)
        _decode_retained_record(
            backup / _RETAINED_RECORD, old_record=old.to_record()
        )
        journal = _UpgradeJournal(
            operation=operation,
            phase="prepared",
            owner_pid=owner_pid,
            bundle_path=Path(canonical),
            record_path=Path(os.path.realpath(record_path)),
            backup_dir=backup,
            backup_dev=backup_info.st_dev,
            backup_inode=backup_info.st_ino,
            archive_sha256=archive_sha256,
            old_record_sha256=hashlib.sha256(old_bytes).hexdigest(),
            new_record_sha256=hashlib.sha256(new_bytes).hexdigest(),
            old_record=old.to_record(),
            new_record=record_snapshot.to_record(),
            signing=policy.signing,
        )
        _write_upgrade_journal(journal)
        # The canonical identity is captured *before* the pair is verified, and the
        # same entry is re-read after it: a swap between the two is refused instead
        # of being treated as verified.
        parent_fd, backup_fd = _open_verified_backup(journal)
        try:
            canonical_entry = _dir_entry(parent_fd, APP_NAME)
            if canonical_entry is None or stat.S_ISLNK(canonical_entry.st_mode):
                raise ServerHostInstallError(
                    f"the host at {bundle} disappeared while this install was staging; "
                    f"it is left untouched",
                    code=HOST_EXISTS,
                )
            canonical_identity = (canonical_entry.st_dev, canonical_entry.st_ino)
            # Re-prove the pair immediately before the canonical rename, and prove the
            # retained bytes are still the record that was just verified.
            _verify_recorded_host(
                bundle,
                record_path,
                old,
                runner,
                refusal=f"the host at {bundle} changed while this install was staging "
                f"and is no longer proven ours",
            )
            if hashlib.sha256(
                _read_private_bytes(
                    record_path, what="the existing host ownership record"
                )
            ).hexdigest() != journal.old_record_sha256:
                raise ServerHostInstallError(
                    f"the host ownership record at {record_path} changed while this "
                    f"install was staging; it is left untouched",
                    code=HOST_EXISTS,
                )
            verified = _dir_entry(parent_fd, APP_NAME)
            if verified is None or (
                verified.st_dev,
                verified.st_ino,
            ) != canonical_identity:
                raise ServerHostInstallError(
                    f"the host at {bundle} changed while it was being verified, so it "
                    f"was not moved; the before-images are kept at {backup}",
                    code=HOST_EXISTS,
                )
            try:
                os.rename(
                    APP_NAME,
                    _RETAINED_BUNDLE,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=backup_fd,
                )
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the host at {bundle} could not be moved aside: {exc}",
                    code=INSTALL_FAILED,
                ) from exc
            moved = _dir_entry(backup_fd, _RETAINED_BUNDLE)
            if moved is None or (moved.st_dev, moved.st_ino) != canonical_identity:
                raise ServerHostInstallError(
                    f"the host at {bundle} changed while it was being moved aside; the "
                    f"before-images are kept at {backup}",
                    code=TRANSACTION_FAILED,
                )
        finally:
            os.close(backup_fd)
            os.close(parent_fd)
        _fsync_dir(backup)
        _fsync_dir(parent)
        journal = _journal_with_phase(journal, "old_moved")
        _write_upgrade_journal(journal)
        _write_ownership_record(record_snapshot, record_path)
        if hashlib.sha256(
            _read_private_bytes(record_path, what="the new host ownership record")
        ).hexdigest() != journal.new_record_sha256:
            raise ServerHostInstallError(
                f"the host ownership record at {record_path} is not the one this "
                f"install wrote",
                code=INSTALL_FAILED,
            )
        # The staged app is renamed in through the staging and target directory
        # descriptors: both are already open and neither name is followed.
        stage_fd = _open_dir_no_follow(stage, what="the host staging directory")
        parent_fd = _open_dir_no_follow(parent, what="the host bundle's directory")
        try:
            os.rename(APP_NAME, APP_NAME, src_dir_fd=stage_fd, dst_dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise ServerHostInstallError(
                    f"a host bundle appeared at {bundle} while this install was "
                    f"staging: {exc}",
                    code=HOST_EXISTS,
                ) from exc
            raise ServerHostInstallError(
                f"the staged host could not be moved to {bundle}: {exc}",
                code=INSTALL_FAILED,
            ) from exc
        finally:
            os.close(parent_fd)
            os.close(stage_fd)
        _fsync_dir(parent)
        journal = _journal_with_phase(journal, "installed")
        _write_upgrade_journal(journal)
        installed = verify_owned_host(bundle, ownership_path=record_path, runner=runner)
        if installed != record_snapshot:
            raise ServerHostInstallError(
                f"the host installed at {bundle} does not match its ownership record",
                code=INSTALL_FAILED,
            )
        if installed.signing != policy.signing:
            raise ServerHostInstallError(
                "the installed host is not signed by the expected Developer ID team",
                code=POLICY_REFUSED,
            )
        return installed
    except BaseException as failure:
        if journal is None:
            # Nothing was retained and nothing moved: the target is untouched.
            raise
        # The transaction exists, so the old pair comes back before this run
        # reports anything. A rollback that cannot finish keeps its material and
        # says where, together with the failure that started it.
        try:
            problem = _rollback_journal(journal, runner)
        except (ServerHostInstallError, ServerHostError, OSError) as rollback_error:
            if isinstance(failure, Exception):
                raise ServerHostInstallError(
                    f"the host upgrade failed ({failure}) and the previous host could "
                    f"NOT be restored ({rollback_error}). The previous host and its "
                    f"ownership record are kept at {journal.backup_dir} and the "
                    f"journal at {upgrade_journal_path()}",
                    code=INSTALL_FAILED,
                ) from failure
            failure.add_note(
                f"the previous host could not be restored: {rollback_error}; it is "
                f"kept at {journal.backup_dir}"
            )
            raise
        if problem is not None:
            print(f"warning: {problem}", file=sys.stderr)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _install_now(
    path: Path,
    policy: _EntryPolicy,
    bundle: Path,
    record_path: Path,
    superseded_snapshot: HostOwnership | None,
    runner: Runner,
    *,
    require_absent: bool = False,
    trusted_root: Path | None = None,
) -> HostOwnership:
    """The established single-step install: a fresh host or a superseded one."""

    if require_absent:
        assert trusted_root is not None
        _require_fresh_install_paths(
            bundle,
            record_path,
            trusted_root,
            raw_bundle=os.fspath(bundle),
            raw_record=os.fspath(record_path),
        )
        if os.path.lexists(bundle):
            raise ServerHostInstallError(
                f"a host bundle already exists at {bundle}; a fresh install "
                f"never adopts or replaces it",
                code=HOST_EXISTS,
            )
        _refuse_present_record(record_path)
        _refuse_present_journal()
        parent = bundle.parent
        # Create only missing controlled directories, descriptor-relative and
        # privately. Existing safe parents are validated and never chmod'ed.
        _prepare_fresh_directory_chain(
            parent, trusted_root, private_final=False
        )
        _prepare_fresh_directory_chain(
            record_path.parent, trusted_root, private_final=True
        )
        _prepare_fresh_directory_chain(
            upgrade_journal_path().parent, trusted_root, private_final=True
        )
        # The directory preparation is a mutation; re-check every host surface
        # immediately before the first staging temp is created.
        _require_fresh_install_paths(
            bundle,
            record_path,
            trusted_root,
            raw_bundle=os.fspath(bundle),
            raw_record=os.fspath(record_path),
        )
        if os.path.lexists(bundle):
            raise ServerHostInstallError(
                f"a host bundle appeared at {bundle} while this fresh install "
                f"was preparing its directories",
                code=HOST_EXISTS,
            )
        _refuse_present_record(record_path)
        _refuse_present_journal()
    else:
        parent = bundle.parent
    try:
        if not require_absent:
            parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host staging directory could not be created: {exc}",
            code=INSTALL_FAILED,
        ) from exc

    renamed = False
    recorded = False
    recorded_identity: tuple[int, int] | None = None
    installed_identity: tuple[int, int] | None = None
    keep_record = False
    superseded_record: HostOwnership | None = superseded_snapshot
    moved_aside = False
    keep_stage = False
    aside = stage / "superseded"
    try:
        staged_app = extract_host_archive(path, stage)
        # Prove the bytes before they reach ~/Applications. This runs the plist
        # identity checks and the native codesign probes under the policy the
        # selected entry names; a foreign or tampered bundle is refused here,
        # with the target still untouched.
        staged = inspect_host_bundle(
            staged_app,
            runner=runner,
            revision=policy.revision,
            protocol=policy.protocol,
            signing=policy.signing,
        )
        staged_identity: tuple[int, int] | None = None
        if require_absent:
            try:
                staged_info = os.lstat(staged_app)
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the staged host could not be examined before the rename: {exc}",
                    code=INSTALL_FAILED,
                ) from exc
            staged_identity = (staged_info.st_dev, staged_info.st_ino)
        if os.path.lexists(bundle):
            if superseded_record is None:
                # Lost the race with another installer between the check above and
                # the rename. Refuse rather than replace what appeared.
                raise ServerHostInstallError(
                    f"a host bundle appeared at {bundle} while this install was staging",
                    code=HOST_EXISTS,
                )
            # The superseded host is replaced, so its record and bytes are proven
            # again right before it is moved aside. Its record is kept in memory
            # so a failed replacement can put it back.
            superseded_record = _verify_recorded_host(
                bundle,
                record_path,
                superseded_record,
                runner,
                refusal=f"the host at {bundle} changed while this install was "
                f"staging and is no longer proven ours",
            )
        # The record names the canonical target, not the staging directory, and
        # is written before the rename. A crash before the rename leaves a record
        # with no bundle (the next fresh install overwrites it); a crash after
        # the rename leaves a consistent, verified host. Writing it after the
        # rename would put a full native re-inspection (seconds) in the window
        # where a kill leaves a bundle no record vouches for.
        #
        # The record is built from the staged snapshot, so it is identical for
        # two racing installers of the same signed archive - which is what makes
        # keeping it on a concurrent-creation refusal below safe.
        canonical = os.path.join(os.path.realpath(parent), APP_NAME)
        record_snapshot = dataclasses.replace(staged, bundle_path=canonical)
        _fsync_tree(staged_app)
        if require_absent:
            # Re-check immediately before the first write. A bundle, record,
            # journal or symlinked/foreign parent that appeared after the
            # caller's preflight and the initial check is a refusal, not something
            # to overwrite or settle. A record that appears after this check but
            # before the write below is the documented same-UID race; the run
            # still refuses on a lost rename and preserves the concurrent state.
            assert trusted_root is not None
            _require_fresh_install_paths(
                bundle,
                record_path,
                trusted_root,
                raw_bundle=os.fspath(bundle),
                raw_record=os.fspath(record_path),
            )
            if os.path.lexists(bundle):
                raise ServerHostInstallError(
                    f"a host bundle appeared at {bundle} while this fresh install "
                    f"was staging",
                    code=HOST_EXISTS,
                )
            _refuse_present_record(record_path)
            _refuse_present_journal()
        if superseded_record is not None:
            # Move the superseded bundle out of the way first: a crash after this
            # leaves no bundle, and the next run installs fresh. It lives inside
            # the staging directory, so the cleanup below removes it on success.
            try:
                os.rename(bundle, aside)
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the superseded host at {bundle} could not be moved aside: {exc}",
                    code=INSTALL_FAILED,
                ) from exc
            moved_aside = True
        if require_absent:
            recorded_identity = _write_ownership_record(
                record_snapshot,
                record_path,
                return_identity=True,
                preserve_parent_mode=True,
            )
        else:
            _write_ownership_record(record_snapshot, record_path)
            recorded_identity = None
        recorded = True
        try:
            os.rename(staged_app, bundle)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                # A concurrent one-liner won the race and renamed a host into
                # place first. Its bundle is byte-identical to this one (both
                # came from the same signed archive), so this run's record can
                # only ever verify that same host - a foreign or different
                # bundle still gets `host_exists` on the next read, so nothing is
                # weakened. Leave the record; unlinking it here would strip the
                # winner's bundle of the record this run just wrote and leave it
                # recordless. This is the one refusal that must not roll back the
                # record. `require_absent` keeps the same ordering: the caller
                # gets HOST_EXISTS, never the winner's snapshot, and the winner's
                # bundle and record are preserved.
                keep_record = True
                raise ServerHostInstallError(
                    f"a host bundle appeared at {bundle} while this install was "
                    f"staging: {exc}",
                    code=HOST_EXISTS,
                ) from exc
            raise ServerHostInstallError(
                f"the staged host could not be moved to {bundle}: {exc}",
                code=INSTALL_FAILED,
            ) from exc
        renamed = True
        installed_identity = staged_identity
        _fsync_dir(parent)

        return verify_owned_host(bundle, ownership_path=record_path, runner=runner)
    except BaseException as failure:
        # Nothing of this run survives a failure: the staging directory is
        # removed either way, and a bundle that was renamed into place is taken
        # away again so a recordless host is never left behind, together with a
        # record this run may have written for it. A bundle that was already
        # there is never touched - `renamed` is the fact that separates "this
        # run put it there" from "this run found it" - and `keep_record` is the
        # fact that separates a concurrent creation (where the record verifies
        # the winner's identical host) from an install failure this run caused.
        #
        # A superseded host that was moved aside is put back, with its old
        # record, so a failed replacement leaves the Mac exactly as it was.
        # The rollback stops at its first failure: whatever it could not put
        # back stays where it is - the superseded bundle in the staging
        # directory, the record as it is - and the error says exactly where.
        problem = ""
        if renamed:
            if require_absent:
                # Delete only the directory this call renamed into place. A
                # same-UID writer can substitute the name after the rename; that
                # object is not ours to remove, so leave it and report the
                # unproven cleanup instead.
                if installed_identity is None:
                    problem = (
                        f"the freshly installed host at {bundle} could not be "
                        f"examined for cleanup; it is left in place"
                    )
                else:
                    try:
                        current = os.lstat(bundle)
                    except FileNotFoundError:
                        current = None
                    except OSError:
                        current = None
                        problem = (
                            f"the freshly installed host at {bundle} could not be "
                            f"examined for cleanup; it is left in place"
                        )
                    if current is not None:
                        if (current.st_dev, current.st_ino) != installed_identity:
                            problem = (
                                f"the freshly installed host at {bundle} was "
                                f"replaced during the failed install; the "
                                f"substitute is left in place"
                            )
                        else:
                            shutil.rmtree(bundle, ignore_errors=True)
            else:
                shutil.rmtree(bundle, ignore_errors=True)
        if moved_aside:
            if os.path.lexists(bundle):
                problem = (
                    f"the new host could not be removed from {bundle}; the "
                    f"superseded host is kept at {aside}"
                )
            else:
                try:
                    os.rename(aside, bundle)
                except OSError as exc:
                    # The bundle stays in the staging directory and the record
                    # names the new revision, so for as long as this ordering
                    # survives, the record does not match what is installed.
                    # The rename is the same filesystem and the same directory
                    # tree that already succeeded once, so it does not fail
                    # in practice; if it ever does, the next install writes a
                    # fresh record over this one.
                    problem = (
                        f"the superseded host could not be moved back to {bundle} "
                        f"({exc}); it is kept at {aside}"
                    )
            if problem:
                keep_stage = True
            elif superseded_record is not None and recorded and not keep_record:
                try:
                    _write_ownership_record(superseded_record, record_path)
                    recorded = False
                except (ServerHostError, OSError) as exc:
                    # `_write_ownership_record` translates `OSError` into a
                    # `ServerHostInstallError`, so catching only `OSError` here
                    # would let a wrapped restore failure replace the original
                    # error and lose the fact that the host came back without its
                    # record. Both are reported, and the kept bundle stays named.
                    problem = (
                        f"the superseded host was restored at {bundle}, but its "
                        f"ownership record could not be written back to "
                        f"{record_path} ({exc}); the host is kept at {bundle}"
                    )
        if recorded and not keep_record and not problem:
            if require_absent:
                if not _remove_own_record(
                    record_path, record_snapshot, recorded_identity
                ):
                    problem = (
                        f"the ownership record at {record_path} was not removed "
                        f"because it is no longer the exact file this run wrote; "
                        f"it is left in place"
                    )
            else:
                try:
                    record_path.unlink(missing_ok=True)
                except OSError:
                    pass
        if problem and isinstance(failure, Exception):
            raise ServerHostInstallError(
                f"{problem}; the install failed first: {failure}",
                code=INSTALL_FAILED,
            ) from failure
        if problem:
            failure.add_note(problem)
        raise
    finally:
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)


def _require_settled_material(
    journal: _UpgradeJournal, *, require_record: bool
) -> bool:
    """Prove the before-image directory before a terminal command acts on it.

    A committed or rolled-back operation may still own a directory inside the
    bundle's parent, and this installer may delete it. When it is there, its
    identity is checked - the exact nonce-derived name, device/inode, owner, mode
    and no link. A directory that is *absent* is the normal state after an
    interrupted cleanup (the before-images were removed, then the journal unlink
    failed), so it is not an error: the caller may finish the journal and the retry
    stays idempotent.

    ``require_record`` says whether the retained record still has to be there. For a
    terminal phase it need not be - a cleanup that removed the record and then
    failed must be resumable - but anything that *is* still there is validated, so
    substituted material is refused either way. An unfinished rollback passes
    ``True``: it has to restore from that record, so its absence is a refusal.

    Returns whether the before-image directory was present.
    """
    bundle = journal.bundle_path
    try:
        parent_fd = _open_dir_no_follow(
            bundle.parent, what="the host bundle's directory"
        )
    except ServerHostInstallError as exc:
        raise ServerHostInstallError(
            f"{exc}. The before-images must be proved before they are touched",
            code=TRANSACTION_FAILED,
        ) from exc
    try:
        entry = _dir_entry(parent_fd, journal.backup_dir.name)
    finally:
        os.close(parent_fd)
    if entry is None:
        if require_record:
            raise ServerHostInstallError(
                f"the before-images at {journal.backup_dir} are gone, so the previous "
                f"host cannot be restored; the journal is kept",
                code=TRANSACTION_FAILED,
            )
        return False
    parent_fd, backup_fd = _open_verified_backup(journal)
    try:
        record_entry = _dir_entry(backup_fd, _RETAINED_RECORD)
        if record_entry is None:
            if require_record:
                raise ServerHostInstallError(
                    f"the retained host ownership record at "
                    f"{journal.backup_dir / _RETAINED_RECORD} is gone; nothing was "
                    f"changed",
                    code=TRANSACTION_FAILED,
                )
        else:
            if stat.S_ISLNK(record_entry.st_mode) or not stat.S_ISREG(
                record_entry.st_mode
            ):
                raise ServerHostInstallError(
                    f"the retained host ownership record at "
                    f"{journal.backup_dir / _RETAINED_RECORD} is not a regular file; "
                    f"nothing was changed",
                    code=TRANSACTION_FAILED,
                )
            if hasattr(os, "getuid") and record_entry.st_uid != _getuid():
                raise ServerHostInstallError(
                    f"the retained host ownership record at "
                    f"{journal.backup_dir / _RETAINED_RECORD} is not owned by this "
                    f"user; nothing was changed",
                    code=TRANSACTION_FAILED,
                )
            if stat.S_IMODE(record_entry.st_mode) & 0o077:
                raise ServerHostInstallError(
                    f"the retained host ownership record at "
                    f"{journal.backup_dir / _RETAINED_RECORD} is group/other "
                    f"accessible; nothing was changed",
                    code=TRANSACTION_FAILED,
                )
    finally:
        os.close(backup_fd)
        os.close(parent_fd)
    return True


def _raise_on_leftover(problem: str | None) -> None:
    """Turn a failed terminal cleanup into a distinct, non-fatal-to-the-operation refusal."""
    if problem is None:
        return
    raise ServerHostInstallError(
        f"{problem}. The host upgrade itself is settled; the retained material must "
        f"be removed before another one is started",
        code=TRANSACTION_CLEANUP,
    )


def commit_host_upgrade(
    *,
    operation: str,
    owner_pid: int,
    runner: Runner = subprocess.run,
) -> None:
    """Commit one deferred host upgrade, after the new engine is proven healthy.

    The commit is bound to the operation nonce and to the installer process that
    started it: a different process — including one that has just proved the old
    owner is gone — can never commit it, and an abandoned operation can only be
    rolled back. The installed signed pair is re-verified here, so a healthy HTTP
    answer alone cannot earn a commit for a tampered or replaced host. Only after
    that verification is the terminal phase written; only after the terminal
    phase are the before-images released. A cleanup that fails is *reported* -
    the operation stays committed and the journal is kept - and the command exits
    with :data:`TRANSACTION_CLEANUP` so no later transaction can overwrite the
    retained material before a later run removes it.
    """
    pid = _strict_positive_pid(owner_pid, what="the installer process id")
    nonce = _strict_operation(operation)
    with _upgrade_lock():
        journal = _read_upgrade_journal()
        if journal is None:
            raise ServerHostInstallError(
                "there is no Ciaobot Server host upgrade to commit",
                code=TRANSACTION_CONFLICT,
            )
        if journal.operation != nonce:
            raise ServerHostInstallError(
                "the Ciaobot Server host upgrade this run would commit is a "
                "different operation",
                code=TRANSACTION_CONFLICT,
            )
        if journal.owner_pid != pid:
            raise ServerHostInstallError(
                f"the Ciaobot Server host upgrade belongs to installer process "
                f"{journal.owner_pid}; only that process can commit it",
                code=TRANSACTION_CONFLICT,
            )
        if not _pid_alive(pid):
            raise ServerHostInstallError(
                f"installer process {pid} is not running; a host upgrade is never "
                f"committed on behalf of a process that is gone",
                code=TRANSACTION_CONFLICT,
            )
        bundle = default_bundle_path()
        record_path = default_ownership_path()
        if not _journal_targets_are_this_mac(journal, bundle, record_path):
            raise ServerHostInstallError(
                f"the Ciaobot Server host upgrade journal at {upgrade_journal_path()} "
                f"names another target; it is left untouched",
                code=TRANSACTION_CONFLICT,
            )
        # Every path this command may release is proved before anything is
        # verified, written or deleted - terminal recovery included.
        _require_canonical_targets(journal)
        _require_settled_material(journal, require_record=False)
        if journal.phase == "committed":
            # Already settled: only the cleanup is outstanding, and the unlink is
            # bound to the journal file that was just read.
            _raise_on_leftover(_finish_journal(journal))
            return
        if journal.phase in UPGRADE_TERMINAL_PHASES:
            raise ServerHostInstallError(
                "the Ciaobot Server host upgrade was already rolled back and "
                "cannot be committed",
                code=TRANSACTION_CONFLICT,
            )
        raw = _read_private_bytes(
            record_path, what="the installed host ownership record"
        )
        if hashlib.sha256(raw).hexdigest() != journal.new_record_sha256:
            raise ServerHostInstallError(
                f"the host ownership record at {record_path} is not the one this "
                f"upgrade installed",
                code=TRANSACTION_FAILED,
            )
        installed = verify_owned_host(bundle, ownership_path=record_path, runner=runner)
        if installed.to_record() != dict(journal.new_record):
            raise ServerHostInstallError(
                f"the host at {bundle} is not the signed host this upgrade installed",
                code=TRANSACTION_FAILED,
            )
        if installed.signing != journal.signing:
            raise ServerHostInstallError(
                "the installed host is not signed by the Developer ID team this "
                "upgrade expects",
                code=POLICY_REFUSED,
            )
        terminal = _journal_with_phase(journal, "committed")
        written = _write_upgrade_journal(terminal)
        _raise_on_leftover(_finish_journal(terminal, journal_inode=written))


def rollback_host_upgrade(
    *,
    operation: str,
    owner_pid: int,
    runner: Runner = subprocess.run,
) -> None:
    """Restore the exact old host pair of one deferred upgrade.

    The caller must name the operation nonce. The starting installer's own pid
    rolls its transaction back at any time; a *different* pid may do it only once
    the journal's owner is proven absent under the shared lock, which is the
    abandoned-operation recovery path. A committed operation is never rolled
    back, and an already-rolled-back one is a no-op. Restoring is refused rather
    than guessed when the canonical target holds bytes neither snapshot proves:
    the before-images stay where they are and the error names them. A cleanup that
    fails after the pair is back is reported as :data:`TRANSACTION_CLEANUP`, so
    the caller knows the rollback itself succeeded.
    """
    pid = _strict_positive_pid(owner_pid, what="the installer process id")
    nonce = _strict_operation(operation)
    with _upgrade_lock():
        journal = _read_upgrade_journal()
        if journal is None:
            return
        if journal.operation != nonce:
            raise ServerHostInstallError(
                "the Ciaobot Server host upgrade this run would roll back is a "
                "different operation",
                code=TRANSACTION_CONFLICT,
            )
        if journal.phase == "committed":
            raise ServerHostInstallError(
                "the Ciaobot Server host upgrade is already committed; it cannot be "
                "rolled back",
                code=TRANSACTION_CONFLICT,
            )
        if not _journal_targets_are_this_mac(
            journal, default_bundle_path(), default_ownership_path()
        ):
            raise ServerHostInstallError(
                f"the Ciaobot Server host upgrade journal at {upgrade_journal_path()} "
                f"names another target; it is left untouched",
                code=TRANSACTION_CONFLICT,
            )
        _require_canonical_targets(journal)
        settled = journal.phase == "rolled_back"
        _require_settled_material(journal, require_record=not settled)
        if settled:
            # Settled and possibly already cleaned: finish the journal only.
            _raise_on_leftover(_finish_journal(journal))
            return
        if journal.owner_pid != pid and _pid_alive(journal.owner_pid):
            raise ServerHostInstallError(
                f"installer process {journal.owner_pid} is still upgrading the "
                f"Ciaobot Server host; this run will not roll its operation back",
                code=TRANSACTION_BUSY,
            )
        _raise_on_leftover(_rollback_journal(journal, runner))


def transaction_status() -> dict[str, Any]:
    """Whether this Mac has an unfinished host upgrade, in the shell's shape.

    Exactly ``{"pending": False}`` when there is nothing to settle, otherwise the
    operation nonce, phase, owner pid and whether that owner is still alive —
    never a credential and never an inferred target. Corrupt, unreadable or
    unsafe state is a refusal, not ``pending: False``.
    """
    with _upgrade_lock():
        journal = _read_upgrade_journal()
        if journal is None or journal.phase in UPGRADE_TERMINAL_PHASES:
            return {"pending": False}
        return {
            "pending": True,
            "operation": journal.operation,
            "phase": journal.phase,
            "owner_pid": journal.owner_pid,
            "owner_alive": _pid_alive(journal.owner_pid),
        }


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m ciao.server_host_install``.

    ``select`` re-verifies the signed manifest and prints ``filename sha256
    size`` for its one supported ``server-host`` entry, or nothing and exit 0
    when the manifest is a wheel-only release. It is how the shell installer
    learns what to download without a second verifier of its own: any malformed
    manifest, wrong signature or unsupported host identity is exit 1. With
    ``--describe`` two more fields follow — the selected host revision and either
    ``legacy`` or the Developer ID team — so the shell can warn about a
    migration before it starts one; the three fields it already reads are
    unchanged and stay first.

    ``install`` re-verifies the manifest and its selected host entry (the shell
    installer has already read the same bytes, and re-verifying here closes the
    gap between the two reads) and installs the archive. A refusal in
    :data:`WARN_AND_CONTINUE` — everything except a manifest that did not verify
    and an ``ARCHIVE_MISMATCH`` — is :data:`HOST_WARNING_EXIT`, not 1, so the
    shell installer warns and carries on with the engine instead of aborting the
    one-liner for an optional, inert host. :data:`DEFERRED_UPGRADE_REQUIRED` is
    :data:`HOST_DEFERRED_EXIT`, its own status, because the shell can report the
    deferral truthfully instead of calling it a failure. Standard output stays the
    canonical installed bundle path and nothing else.

    ``transaction-status`` prints the strict JSON of :func:`transaction_status`.
    ``commit-upgrade`` and ``rollback-upgrade`` settle one operation by its nonce
    and the installer pid that owns it; both print nothing on success. When the
    operation settled but its retained material could not be removed, they exit
    :data:`HOST_TRANSACTION_LEFTOVER_EXIT` - a distinct status, because rolling a
    committed or already restored host back over a cleanup failure would be worse
    than the leftover.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ciao.server_host_install", description=__doc__
    )
    sub = parser.add_subparsers(dest="command", required=True)

    select = sub.add_parser(
        "select", help="Print the signed server-host entry, if the release has one"
    )
    select.add_argument("manifest", type=Path, help="signed manifest")
    select.add_argument("signature", type=Path, help="manifest signature")
    select.add_argument(
        "--public-key", default=RELEASE_PUBLIC_KEY, help="minisign public key"
    )
    select.add_argument(
        "--describe",
        action="store_true",
        help="also print the selected host revision and signing policy",
    )

    install = sub.add_parser("install", help="Install the verified server host")
    install.add_argument("--manifest", required=True, type=Path, help="signed manifest")
    install.add_argument(
        "--signature", required=True, type=Path, help="manifest signature"
    )
    install.add_argument("--archive", required=True, type=Path, help="host archive")
    install.add_argument(
        "--public-key", default=RELEASE_PUBLIC_KEY, help="minisign public key"
    )
    install.add_argument(
        "--bundle-path", type=Path, default=None, help="target bundle path"
    )
    install.add_argument(
        "--ownership-path", type=Path, default=None, help="ownership record path"
    )
    install.add_argument(
        "--defer-commit",
        action="store_true",
        help="keep a replaceable host recoverable until commit-upgrade",
    )
    install.add_argument(
        "--owner-pid",
        type=int,
        default=None,
        help="the installer process that will commit a deferred upgrade",
    )

    sub.add_parser(
        "transaction-status", help="Print the pending host upgrade, as strict JSON"
    )

    commit = sub.add_parser("commit-upgrade", help="Commit a deferred host upgrade")
    commit.add_argument("--operation", required=True, help="operation nonce")
    commit.add_argument("--owner-pid", required=True, type=int, help="installer pid")

    rollback = sub.add_parser(
        "rollback-upgrade", help="Restore the host a deferred upgrade replaced"
    )
    rollback.add_argument("--operation", required=True, help="operation nonce")
    rollback.add_argument("--owner-pid", required=True, type=int, help="installer pid")

    args = parser.parse_args(argv)

    try:
        if args.command == "transaction-status":
            print(json.dumps(transaction_status(), sort_keys=True))
            return 0
        if args.command == "commit-upgrade":
            commit_host_upgrade(operation=args.operation, owner_pid=args.owner_pid)
            return 0
        if args.command == "rollback-upgrade":
            rollback_host_upgrade(operation=args.operation, owner_pid=args.owner_pid)
            return 0
        raw = args.manifest.read_bytes()
        signature_text = args.signature.read_text(encoding="utf-8")
        manifest = verify_manifest(raw, signature_text, args.public_key)
        if args.command == "select":
            artifacts = manifest.get("artifacts", [])
            if not any(
                isinstance(entry, dict) and entry.get("kind") == SERVER_HOST_KIND
                for entry in artifacts
            ):
                # A wheel-only release: nothing to install, and that is not an
                # error.
                return 0
            try:
                entry = select_server_host_artifact(manifest)
            except ValueError as exc:
                # The manifest is authentic - the signature already proved that -
                # but it names a host this engine cannot install: a later host
                # revision, two host entries during a transition, or the host
                # filename reused under another kind. The engine updater's own
                # rule is that such a manifest must not refuse the wheel update
                # on an engine that predates it, so the engine installs and only
                # the host is skipped. Saying so is the difference between a skip
                # and a silent one.
                print(
                    f"warning: the release names a server host this engine cannot "
                    f"install, so it is skipped ({exc}); the engine install "
                    f"continues",
                    file=sys.stderr,
                )
                return 0
            fields = [entry["filename"], entry["sha256"], str(entry["size"])]
            if args.describe:
                policy = _entry_policy(entry)
                fields.append(str(policy.revision))
                fields.append(
                    "legacy" if policy.signing is None else policy.signing.team_id
                )
            print(*fields)
            return 0
        entry = select_server_host_artifact(manifest)
        try:
            snapshot = install_server_host(
                args.archive,
                entry,
                bundle_path=args.bundle_path,
                ownership_path=args.ownership_path,
                defer_commit=args.defer_commit,
                owner_pid=args.owner_pid,
            )
        except ServerHostError as exc:
            # The shell has already checked the manifest signature and the
            # archive's digest and size, so a refusal here is local or a
            # signed-build defect. Only a manifest/selection error or an
            # ARCHIVE_MISMATCH is about the release and stops the engine install;
            # everything else warns and the shell continues. A legacy-to-signed
            # replacement this run did not defer is its own status so the shell
            # can say the migration completes on a later normal run.
            if exc.code == DEFERRED_UPGRADE_REQUIRED:
                print(f"warning: {exc}", file=sys.stderr)
                return HOST_DEFERRED_EXIT
            if exc.code not in WARN_AND_CONTINUE:
                raise
            print(f"warning: {exc}", file=sys.stderr)
            return HOST_WARNING_EXIT
    except (ServerHostError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if isinstance(exc, ServerHostError) and exc.code == TRANSACTION_CLEANUP:
            return HOST_TRANSACTION_LEFTOVER_EXIT
        return 1
    print(snapshot.bundle_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
