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
from ciao.os_support.private import make_private, make_private_dir, mkstemp_private
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
    "TRANSACTION_BUSY",
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

#: ``install``'s exit status for :data:`DEFERRED_UPGRADE_REQUIRED`. Distinct from
#: :data:`HOST_WARNING_EXIT` so the shell can say "this completes on a later run"
#: rather than "the host was not installed" for every other local refusal.
HOST_DEFERRED_EXIT = 4

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
        "archive_sha256",
        "old_record_sha256",
        "new_record_sha256",
        "old",
        "new",
        "policy",
    }
)
_POLICY_FIELDS = frozenset({"revision", "protocol", "schema", "team_id"})
_RETAINED_BUNDLE = "bundle"
_RETAINED_RECORD = "record"


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


def _write_ownership_record(snapshot: HostOwnership, path: Path) -> None:
    """Write the record owner-only, through a temp file and an atomic replace.

    The parent is created ``0700`` and the file ``0600``, matching the reader's
    macOS owner/mode contract. The JSON is written before the rename and fsynced,
    so a reader never observes a half-written record and a crash cannot leave a
    truncated one in its place.
    """
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
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
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(_record_bytes(snapshot).decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        make_private(tmp)
        replace_file(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the ownership record could not be written: {exc}", code=INSTALL_FAILED
        ) from exc
    finally:
        tmp.unlink(missing_ok=True)


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


def _ensure_state_dir(directory: Path) -> None:
    """Create and tighten the fixed state directory, refusing anything else."""
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
def _upgrade_lock() -> Iterator[None]:
    """Hold the short-lived upgrade lock, refusing rather than waiting.

    The lock serializes the check-and-prepare step of every mutating command. It
    is deliberately *not* the thing that keeps a second installer away from a
    first one's transaction between the shell's subprocess calls: that is the
    journal's owner pid, which outlives this descriptor.
    """
    journal = upgrade_journal_path()
    _ensure_state_dir(journal.parent)
    lock = upgrade_lock_path()
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        descriptor = open_fd(lock, flags, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host upgrade lock could not be opened: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    try:
        try:
            make_private(lock)
            lock_exclusive(descriptor, blocking=False)
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


def _read_private_bytes(path: Path, *, what: str) -> bytes:
    """Read a private file's exact bytes, refusing a symlink, special file or swap.

    The same checks :mod:`ciao.server_host` applies to the ownership record (its
    owner, no group/other bit, no follow, one inode from ``lstat`` to ``fstat``)
    because the retained record has to be the bytes the verified snapshot was
    read from, not whatever is there later.
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
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
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
    archive_sha256: str
    old_record_sha256: str
    new_record_sha256: str
    old_record: Mapping[str, Any]
    new_record: Mapping[str, Any]
    signing: DeveloperIDSigning

    def document(self) -> dict[str, Any]:
        return {
            "schema": UPGRADE_SCHEMA_VERSION,
            "operation": self.operation,
            "phase": self.phase,
            "owner_pid": self.owner_pid,
            "bundle_path": os.fspath(self.bundle_path),
            "record_path": os.fspath(self.record_path),
            "backup_dir": os.fspath(self.backup_dir),
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
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ServerHostInstallError(
            f"the server host upgrade journal cannot be examined: {exc}",
            code=TRANSACTION_FAILED,
        ) from exc
    raw = _read_private_bytes(path, what="the server host upgrade journal")
    return _decode_journal(_strict_document(raw, what="the server host upgrade journal"))


def _write_upgrade_journal(journal: _UpgradeJournal) -> None:
    """Persist one journal phase durably, owner-only, through an atomic replace."""
    data = (json.dumps(journal.document(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    _write_private_bytes(
        upgrade_journal_path(), data, what="the server host upgrade journal"
    )


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


def _retained_record_snapshot(journal: _UpgradeJournal) -> HostOwnership:
    """Prove the retained record bytes are the journal's, then decode them.

    The bytes are read through the same strict private-file checks, hashed
    against the journal and decoded by :mod:`ciao.server_host`'s own reader: the
    before-image is exactly the record that was verified, never a
    ``to_record()`` reserialization of it.
    """
    retained = journal.backup_dir / _RETAINED_RECORD
    raw = _read_private_bytes(retained, what="the retained host ownership record")
    if hashlib.sha256(raw).hexdigest() != journal.old_record_sha256:
        raise ServerHostInstallError(
            f"the retained host ownership record at {retained} is not the one this "
            f"upgrade saved",
            code=TRANSACTION_FAILED,
        )
    return _decode_retained_record(retained, old_record=journal.old_record)


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
    return dict(inspected.to_record()) == dict(snapshot)


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
    material and the bytes at the canonical target, not from the phase alone. A
    target this transaction cannot prove is either the candidate it installed or
    the host it replaced is left where it is, with the before-images kept: no
    unrelated bundle is ever deleted to make room for a rollback.
    """
    bundle = journal.bundle_path
    record_path = journal.record_path
    backup = journal.backup_dir
    retained_bundle = backup / _RETAINED_BUNDLE
    # The retained record is proved before its bundle is moved back into place.
    _retained_record_snapshot(journal)
    if os.path.islink(bundle):
        raise ServerHostInstallError(
            f"the host bundle at {bundle} is now a symlink; the before-images are "
            f"kept at {backup}",
            code=TRANSACTION_FAILED,
        )
    if os.path.lexists(bundle):
        if _matches_snapshot(bundle, journal.new_record, runner):
            try:
                shutil.rmtree(bundle)
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the signed host at {bundle} could not be removed ({exc}); the "
                    f"previous host is kept at {retained_bundle}",
                    code=TRANSACTION_FAILED,
                ) from exc
            _fsync_dir(bundle.parent)
        elif not _matches_snapshot(bundle, journal.old_record, runner):
            raise ServerHostInstallError(
                f"the host at {bundle} is neither the signed candidate this upgrade "
                f"installed nor the host it replaced, so it is left untouched; the "
                f"previous host and record are kept at {backup}",
                code=TRANSACTION_FAILED,
            )
    if not os.path.lexists(bundle):
        if retained_bundle.is_symlink() or not retained_bundle.is_dir():
            raise ServerHostInstallError(
                f"the previous host this upgrade kept at {retained_bundle} is gone; "
                f"nothing was removed",
                code=TRANSACTION_FAILED,
            )
        try:
            os.rename(retained_bundle, bundle)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the previous host could not be moved back to {bundle} ({exc}); it "
                f"is kept at {retained_bundle}",
                code=TRANSACTION_FAILED,
            ) from exc
        _fsync_dir(bundle.parent)
    # The record goes back as the exact bytes that were saved, never a
    # reserialization of the snapshot that was decoded from them.
    retained_record = backup / _RETAINED_RECORD
    _write_private_bytes(
        record_path,
        _read_private_bytes(retained_record, what="the retained host ownership record"),
        what="the previous host ownership record",
    )
    restored = _verify_recorded_host(
        bundle,
        record_path,
        _retained_record_snapshot(journal),
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


def _finish_journal(journal: _UpgradeJournal) -> str | None:
    """Release this transaction's own before-images after a terminal phase.

    Only the nonce-derived directory the journal names is removed, and only once
    the terminal phase has been written durably. A failure is reported and the
    journal kept, so a later run finishes the cleanup instead of treating the
    leftover as a new transaction.
    """
    backup = journal.backup_dir
    expected = journal.bundle_path.parent / (
        f"{UPGRADE_BACKUP_PREFIX}{journal.operation}"
    )
    if backup != expected:
        return f"the before-images at {backup} are not this transaction's; they are kept"
    if os.path.lexists(backup):
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            return (
                f"the before-images at {backup} could not be removed ({exc}); they "
                f"are kept"
            )
    try:
        upgrade_journal_path().unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"the upgrade journal could not be removed ({exc}); it is kept"
    return None


def _rollback_journal(journal: _UpgradeJournal, runner: Runner) -> str | None:
    """Restore the old pair, mark the journal terminal and release its material."""
    _restore_old_pair(journal, runner)
    terminal = _journal_with_phase(journal, "rolled_back")
    _write_upgrade_journal(terminal)
    return _finish_journal(terminal)


def install_server_host(
    archive_path: Path,
    entry: Mapping[str, Any],
    *,
    bundle_path: Path | None = None,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
    defer_commit: bool = False,
    owner_pid: int | None = None,
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
    """
    _require_macos()
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

    with _upgrade_lock():
        _recover_before_install(bundle, record_path, runner)
        kind, existing = _classify_host(bundle, record_path, runner, policy)
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
        return _install_now(path, policy, bundle, record_path, existing, runner)


def _recover_before_install(bundle: Path, record_path: Path, runner: Runner) -> None:
    """Settle a journal the previous installer left, before any new mutation.

    An unfinished journal whose owner is still running is a refusal: a second
    installer must not roll back the transaction a live first one is in the middle
    of. An abandoned one (its owner is provably gone) is rolled back from its own
    retained material, so this run starts from a known state rather than from
    whatever a kill left. A terminal journal only needs its cleanup finished.
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
        _finish_journal(journal)
        return
    if journal.owner_pid != os.getpid() and _pid_alive(journal.owner_pid):
        raise ServerHostInstallError(
            f"installer process {journal.owner_pid} is still upgrading the Ciaobot "
            f"Server host; this run will not touch it",
            code=TRANSACTION_BUSY,
        )
    problem = _rollback_journal(journal, runner)
    if problem is not None:
        print(f"warning: {problem}", file=sys.stderr)


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
    retained_bundle = backup / _RETAINED_BUNDLE
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
        except OSError as exc:
            raise ServerHostInstallError(
                f"the host upgrade before-image directory could not be created: {exc}",
                code=INSTALL_FAILED,
            ) from exc
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
            record_path=record_path,
            backup_dir=backup,
            archive_sha256=archive_sha256,
            old_record_sha256=hashlib.sha256(old_bytes).hexdigest(),
            new_record_sha256=hashlib.sha256(new_bytes).hexdigest(),
            old_record=old.to_record(),
            new_record=record_snapshot.to_record(),
            signing=policy.signing,
        )
        _write_upgrade_journal(journal)
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
            _read_private_bytes(record_path, what="the existing host ownership record")
        ).hexdigest() != journal.old_record_sha256:
            raise ServerHostInstallError(
                f"the host ownership record at {record_path} changed while this "
                f"install was staging; it is left untouched",
                code=HOST_EXISTS,
            )
        try:
            os.rename(bundle, retained_bundle)
        except OSError as exc:
            raise ServerHostInstallError(
                f"the host at {bundle} could not be moved aside: {exc}",
                code=INSTALL_FAILED,
            ) from exc
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
        try:
            os.rename(staged_app, bundle)
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
) -> HostOwnership:
    """The established single-step install: a fresh host or a superseded one."""

    parent = bundle.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=parent))
    except OSError as exc:
        raise ServerHostInstallError(
            f"the host staging directory could not be created: {exc}",
            code=INSTALL_FAILED,
        ) from exc

    renamed = False
    recorded = False
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
        _write_ownership_record(record_snapshot, record_path)
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
                # record.
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
    phase are the before-images released. A cleanup that fails is reported and
    left recoverable, never turned into a failed commit.
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
        if journal.phase == "committed":
            problem = _finish_journal(journal)
            if problem is not None:
                print(f"warning: {problem}", file=sys.stderr)
            return
        if journal.phase in UPGRADE_TERMINAL_PHASES:
            raise ServerHostInstallError(
                "the Ciaobot Server host upgrade was already rolled back and "
                "cannot be committed",
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
        _write_upgrade_journal(terminal)
        problem = _finish_journal(terminal)
        if problem is not None:
            print(f"warning: {problem}", file=sys.stderr)


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
    the before-images stay where they are and the error names them.
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
        if journal.phase == "rolled_back":
            problem = _finish_journal(journal)
            if problem is not None:
                print(f"warning: {problem}", file=sys.stderr)
            return
        if journal.owner_pid != pid and _pid_alive(journal.owner_pid):
            raise ServerHostInstallError(
                f"installer process {journal.owner_pid} is still upgrading the "
                f"Ciaobot Server host; this run will not roll its operation back",
                code=TRANSACTION_BUSY,
            )
        if not _journal_targets_are_this_mac(
            journal, default_bundle_path(), default_ownership_path()
        ):
            raise ServerHostInstallError(
                f"the Ciaobot Server host upgrade journal at {upgrade_journal_path()} "
                f"names another target; it is left untouched",
                code=TRANSACTION_CONFLICT,
            )
        problem = _rollback_journal(journal, runner)
        if problem is not None:
            print(f"warning: {problem}", file=sys.stderr)


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
    and the installer pid that owns it; both print nothing on success.
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
        return 1
    print(snapshot.bundle_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
