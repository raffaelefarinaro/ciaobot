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
shell could not already rule out — a manifest that did not verify, or an
``ARCHIVE_MISMATCH``. Everything else (``INSTALL_FAILED``, ``INSPECTION_FAILED``,
an ``ARCHIVE_UNSAFE`` archive the release signed, an existing
``HOST_EXISTS``/``TARGET_UNSAFE`` target) returns :data:`HOST_WARNING_EXIT` from
``install`` so the shell warns and carries on: the host is optional and inert,
and it must not abort the one-liner. :data:`WARN_AND_CONTINUE` is that set.
"""

from __future__ import annotations

import argparse
import dataclasses
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ciao.os_support.files import open_fd, replace_file
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
    INSPECTION_FAILED,
    UNSUPPORTED_PLATFORM,
    HostOwnership,
    Runner,
    ServerHostError,
    default_bundle_path,
    default_ownership_path,
    inspect_host_bundle,
    verify_owned_host,
    verify_superseded_host,
)

__all__ = [
    "ARCHIVE_MISMATCH",
    "ARCHIVE_UNSAFE",
    "HOST_EXISTS",
    "HOST_WARNING_EXIT",
    "INSTALL_FAILED",
    "STAGING_PREFIX",
    "TARGET_UNSAFE",
    "WARN_AND_CONTINUE",
    "ServerHostInstallError",
    "extract_host_archive",
    "install_server_host",
    "main",
    "verify_host_archive",
]

#: Stable refusal codes. They extend :mod:`ciao.server_host`'s vocabulary so a
#: caller can match on one set of strings whichever layer refused.
ARCHIVE_MISMATCH = "archive_mismatch"
ARCHIVE_UNSAFE = "archive_unsafe"
HOST_EXISTS = "host_exists"
INSTALL_FAILED = "install_failed"
TARGET_UNSAFE = "target_unsafe"

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

_CHUNK = 1 << 20
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
            handle.write(
                json.dumps(snapshot.to_record(), indent=2, sort_keys=True) + "\n"
            )
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


def _existing_host(
    bundle: Path, record_path: Path, runner: Runner
) -> HostOwnership | None:
    """The verified existing host, or None when there is nothing to keep at the target.

    A target that is a symlink is never followed. Anything else already there is
    only this installer's to leave alone when an existing private record proves
    it is ours; a foreign, tampered or recordless bundle is a refusal rather than
    something to replace, because replacing it would overwrite a host the user
    may be running.

    MIGRATION (#1249): a revision-1 ``CiaobotServerHost`` bundle that its own
    record proves ours answers None, so the caller replaces it. Delete this
    branch with :func:`ciao.server_host.verify_superseded_host`.
    """
    if os.path.islink(bundle):
        raise ServerHostInstallError(
            f"the host bundle must not be a symlink: {bundle}", code=TARGET_UNSAFE
        )
    if not os.path.lexists(bundle):
        return None
    try:
        return verify_owned_host(bundle, ownership_path=record_path, runner=runner)
    except ServerHostError as current_error:
        try:
            verify_superseded_host(bundle, ownership_path=record_path, runner=runner)
        except ServerHostError as exc:
            raise ServerHostInstallError(
                f"there is already a host at {bundle} that this installer cannot "
                f"prove it owns ({current_error}); it is left untouched",
                code=HOST_EXISTS,
            ) from exc
        return None


def install_server_host(
    archive_path: Path,
    entry: Mapping[str, Any],
    *,
    bundle_path: Path | None = None,
    ownership_path: Path | None = None,
    runner: Runner = subprocess.run,
) -> HostOwnership:
    """Verify, inspect and install the host archive, then record and re-verify it.

    ``entry`` is the selected ``server-host`` artifact of a manifest the caller
    has already verified; this function re-checks the archive against its digest
    and size and never trusts the archive on its own. The bundle and record paths
    default to :func:`ciao.server_host.default_bundle_path` and
    :func:`ciao.server_host.default_ownership_path`.

    An existing, verified host is returned untouched (a no-op); an existing
    bundle this installer cannot prove it owns is refused. The one exception is
    a revision-1 ``CiaobotServerHost`` bundle its own record proves ours (#1249):
    it is replaced by the current revision, and restored with its record if the
    replacement fails. On a fresh install the
    staged bundle is inspected before the record is written and before the
    rename, so a foreign or tampered archive never reaches ``~/Applications``.
    The record is written *before* the rename, from the staged snapshot with the
    canonical target path, so a process killed after the rename leaves a
    consistent, verified host rather than a bundle no record vouches for; a
    Python failure removes both the bundle this run renamed into place and the
    record it wrote. No service, launchd, plist or permission surface is touched.
    """
    _require_macos()
    path = Path(archive_path)
    verify_host_archive(path, entry)

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

    existing = _existing_host(bundle, record_path, runner)
    if existing is not None:
        return existing
    # Only a proven revision-1 host can leave the target occupied here; it is
    # re-proven below, immediately before it is moved aside.
    superseding = os.path.lexists(bundle)

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
    superseded_record: HostOwnership | None = None
    moved_aside = False
    aside = stage / "superseded"
    try:
        staged_app = extract_host_archive(path, stage)
        # Prove the bytes before they reach ~/Applications. This runs the plist
        # identity checks and the native codesign probes; a foreign or tampered
        # bundle is refused here, with the target still untouched.
        staged = inspect_host_bundle(staged_app, runner=runner)
        if os.path.lexists(bundle):
            if not superseding:
                # Lost the race with another installer between the check above and
                # the rename. Refuse rather than replace what appeared.
                raise ServerHostInstallError(
                    f"a host bundle appeared at {bundle} while this install was staging",
                    code=HOST_EXISTS,
                )
            # MIGRATION (#1249): the revision-1 host is replaced, so its record
            # and bytes are proven again right before it is moved aside. Its
            # record is kept in memory so a failed migration can put it back.
            try:
                superseded_record = verify_superseded_host(
                    bundle, ownership_path=record_path, runner=runner
                )
            except ServerHostError as exc:
                raise ServerHostInstallError(
                    f"the host at {bundle} changed while this install was staging "
                    f"and is no longer proven ours ({exc}); it is left untouched",
                    code=HOST_EXISTS,
                ) from exc
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
            # Move the revision-1 bundle out of the way first: a crash after this
            # leaves no bundle, and the next run installs fresh. It lives inside
            # the staging directory, so the cleanup below removes it on success.
            try:
                os.rename(bundle, aside)
            except OSError as exc:
                raise ServerHostInstallError(
                    f"the revision-1 host at {bundle} could not be moved aside: {exc}",
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
    except BaseException:
        # Nothing of this run survives a failure: the staging directory is
        # removed either way, and a bundle that was renamed into place is taken
        # away again so a recordless host is never left behind, together with a
        # record this run may have written for it. A bundle that was already
        # there is never touched - `renamed` is the fact that separates "this
        # run put it there" from "this run found it" - and `keep_record` is the
        # fact that separates a concurrent creation (where the record verifies
        # the winner's identical host) from an install failure this run caused.
        if renamed:
            shutil.rmtree(bundle, ignore_errors=True)
        if moved_aside and not os.path.lexists(bundle):
            # MIGRATION (#1249): put the revision-1 host back, with its record.
            try:
                os.rename(aside, bundle)
                if superseded_record is not None and recorded and not keep_record:
                    _write_ownership_record(superseded_record, record_path)
                    recorded = False
            except OSError:
                pass
        if recorded and not keep_record:
            try:
                record_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m ciao.server_host_install``.

    ``select`` re-verifies the signed manifest and prints ``filename sha256
    size`` for its one supported ``server-host`` entry, or nothing and exit 0
    when the manifest is a wheel-only release. It is how the shell installer
    learns what to download without a second verifier of its own: any malformed
    manifest, wrong signature or unsupported host identity is exit 1.

    ``install`` re-verifies the manifest and its selected host entry (the shell
    installer has already read the same bytes, and re-verifying here closes the
    gap between the two reads) and installs the archive. A refusal in
    :data:`WARN_AND_CONTINUE` — everything except a manifest that did not verify
    and an ``ARCHIVE_MISMATCH`` — is :data:`HOST_WARNING_EXIT`, not 1, so the
    shell installer warns and carries on with the engine instead of aborting the
    one-liner for an optional, inert host.
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
    args = parser.parse_args(argv)

    try:
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
            print(entry["filename"], entry["sha256"], entry["size"])
            return 0
        entry = select_server_host_artifact(manifest)
        try:
            snapshot = install_server_host(
                args.archive,
                entry,
                bundle_path=args.bundle_path,
                ownership_path=args.ownership_path,
            )
        except ServerHostError as exc:
            # The shell has already checked the manifest signature and the
            # archive's digest and size, so a refusal here is local or a
            # signed-build defect. Only a manifest/selection error or an
            # ARCHIVE_MISMATCH is about the release and stops the engine install;
            # everything else warns and the shell continues.
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
