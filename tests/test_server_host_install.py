"""Checks for the server-host installer (#1050, child E1 of #1008).

The pure checks drive :mod:`ciao.server_host_install` with a fake native runner
and a fixture archive: the digest/size gate, the safe extraction, the atomic
install, the owner-only record and each refusal. They run everywhere. The
macOS-only integration builds the real universal host with
``scripts/build-server-host.py``, installs it into the isolated scratch home and
proves the record, the no-op reinstall and the changed-bundle refusal. Nothing
here touches the real ``~/Applications``, ``launchctl``, a running engine or TCC.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from types import ModuleType
from typing import Any

import pytest

from ciao import server_host, server_host_install
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.release_manifest import (
    SERVER_HOST_FILENAME,
    artifact_entry,
    build_manifest,
    server_host_artifact_entry,
)
from ciao.server_host import (
    APP_NAME,
    ARCHITECTURES,
    BUNDLE_ID,
    EXECUTABLE_NAME,
    ICON_NAME,
    HostOwnership,
    read_host_ownership,
    verify_owned_host,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build-server-host.py"

_PLIST_REL = "Contents/Info.plist"
_EXE_REL = f"Contents/MacOS/{EXECUTABLE_NAME}"
_ICON_REL = f"Contents/Resources/{ICON_NAME}"
_CODESIGN_REL = "Contents/_CodeSignature/CodeResources"

# The uid/mode half of the record reader is darwin-only in production; these
# tests mock ``sys.platform`` and so cannot run on a native Windows interpreter.
requires_posix_uid = pytest.mark.skipif(
    sys.platform == "win32", reason="mocking darwin needs a POSIX uid"
)

HAS_TOOLS = (
    sys.platform == "darwin"
    and shutil.which("xcrun") is not None
    and shutil.which("lipo") is not None
    and os.path.exists(server_host.CODESIGN)
)
requires_macos_tools = pytest.mark.skipif(
    not HAS_TOOLS, reason="the native host build requires macOS Command Line Tools"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = _load("build_server_host_install", BUILD_SCRIPT)

_CDHASH = {arch: hashlib.sha1(arch.encode()).hexdigest() for arch in ARCHITECTURES}
_CODESIGN = server_host.CODESIGN


class _FakeRunner:
    """Answers the fixed ``codesign`` probes without a real host bundle.

    It mirrors ``codesign -dv --verbose=4`` output from a built host and records
    every probe, so a test can assert the exact calls the installer made.
    """

    def __init__(
        self,
        *,
        verify_rc: int = 0,
        identifier: str = BUNDLE_ID,
        archs: str = "x86_64 arm64",
    ) -> None:
        self.commands: list[list[str]] = []
        self.verify_rc = verify_rc
        self.identifier = identifier
        self.format = f"app bundle with Mach-O universal ({archs})"

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(argv))
        args = list(argv)
        if args[:2] == [_CODESIGN, "--verify"]:
            return subprocess.CompletedProcess(args, self.verify_rc, "", "")
        if args[:2] == [_CODESIGN, "-dv"]:
            if "--arch" in args:
                arch = args[args.index("--arch") + 1]
                return subprocess.CompletedProcess(
                    args, 0, "", f"CDHash={_CDHASH[arch]}\n"
                )
            stderr = (
                f"Identifier={self.identifier}\nFormat={self.format}\nSignature=adhoc\n"
            )
            return subprocess.CompletedProcess(args, 0, "", stderr)
        raise AssertionError(f"unexpected native probe: {args}")


def _make_bundle(root: Path, *, name: str = APP_NAME) -> Path:
    bundle = root / name
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"universal-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(BUILD.bundle_info(), handle)
    return bundle


def _archive_of(bundle: Path, archive: Path) -> Path:
    """Tar ``bundle`` (an ``APP_NAME`` directory) into ``archive``.

    The arcnames are relative to the bundle's parent, exactly as
    ``scripts/build-server-host.py`` writes them, so ``extract_host_archive``
    sees the same one-app root a real release archive holds.
    """
    with tarfile.open(archive, "w:gz") as tar:
        for current, dirs, files in os.walk(bundle):
            dirs.sort()
            files.sort()
            root = Path(current)
            arcname = str(root.relative_to(bundle.parent))
            info = tar.gettarinfo(str(root), arcname=arcname)
            tar.addfile(info)
            for name in files:
                path = root / name
                info = tar.gettarinfo(
                    str(path), arcname=str(root.relative_to(bundle.parent) / name)
                )
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    return archive


def _fixture(
    tmp_path: Path, *, name: str = APP_NAME
) -> tuple[Path, Path, dict[str, Any]]:
    """A valid archive, its bundle, and the signed manifest entry describing it."""
    bundle = _make_bundle(tmp_path / "src", name=name)
    archive = _archive_of(bundle, tmp_path / SERVER_HOST_FILENAME)
    entry = server_host_artifact_entry(archive)
    return archive, bundle, entry


def _target(tmp_path: Path) -> Path:
    return tmp_path / "Applications" / APP_NAME


def _make_superseded_bundle(root: Path, revision: int = 3) -> Path:
    """A host bundle of the superseded revision, with the old identity fields."""
    bundle = root / APP_NAME
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    (bundle / "Contents" / "_CodeSignature").mkdir(parents=True)
    (bundle / _EXE_REL).write_bytes(b"revision-three-host-binary")
    (bundle / _ICON_REL).write_bytes(b"indigo-icns-bytes")
    (bundle / _CODESIGN_REL).write_bytes(b"sealed-resources")
    info = BUILD.bundle_info()
    info["CFBundleVersion"] = str(revision)
    info["CFBundleShortVersionString"] = str(revision)
    info[BUILD.HOST_PROTOCOL_KEY] = server_host.SUPERSEDED_HOST_PROTOCOLS[revision]
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(info, handle)
    return bundle


def _write_superseded_record(path: Path, bundle: Path, revision: int = 3) -> Path:
    """The owner-only record a revision-3 installation would have written."""
    document: dict[str, Any] = {
        "schema": 1,
        "bundle_path": os.fspath(bundle),
        "bundle_id": BUNDLE_ID,
        "host_revision": revision,
        "host_protocol": server_host.SUPERSEDED_HOST_PROTOCOLS[revision],
        "executable_sha256": hashlib.sha256(
            (bundle / _EXE_REL).read_bytes()
        ).hexdigest(),
        "per_arch_cdhashes": dict(_CDHASH),
        "bundle_files": {
            _PLIST_REL: hashlib.sha256((bundle / _PLIST_REL).read_bytes()).hexdigest(),
            _EXE_REL: hashlib.sha256((bundle / _EXE_REL).read_bytes()).hexdigest(),
            _ICON_REL: hashlib.sha256((bundle / _ICON_REL).read_bytes()).hexdigest(),
            _CODESIGN_REL: hashlib.sha256(
                (bundle / _CODESIGN_REL).read_bytes()
            ).hexdigest(),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# ── the signed revision-5 fixtures (#2329, #2330) ───────────────────────────

_SIGNED_TEAM = "TESTTEAM01"
_OTHER_TEAM = "OTHERTEAM1"


class _SignedRunner:
    """Answers the native probes a real ``codesign`` would, per bundle policy.

    The bundle a probe names decides the answer: a revision-5 bundle is reported
    as Developer ID signed by this object's (fake) team, a revision-4 one as the
    production ad-hoc host. That is what a real ``codesign`` does for two bundles
    on one Mac, and it lets one runner drive both the policy an installed record
    selects and the signed candidate a release selects. Only fake teams:
    ``TESTTEAM01`` and ``OTHERTEAM1``.
    """

    def __init__(self, *, team: str = _SIGNED_TEAM, verify_rc: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.team = team
        self.verify_rc = verify_rc

    @staticmethod
    def _bundle_of(target: str) -> Path:
        path = Path(target)
        while path.name != APP_NAME and path.parent != path:
            path = path.parent
        return path

    def _signed(self, target: str) -> bool:
        plist = self._bundle_of(target) / _PLIST_REL
        try:
            with plist.open("rb") as handle:
                info = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            return False
        return str(info.get("CFBundleVersion")) == str(
            server_host.SIGNED_HOST_REVISION
        )

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        self.commands.append(args)
        if args[:2] == [_CODESIGN, "--verify"]:
            return subprocess.CompletedProcess(args, self.verify_rc, "", "")
        target = args[-1]
        signed = self._signed(target)
        if args[1:2] == ["-d"] and "--entitlements" in args:
            payload = plistlib.dumps(
                {"com.apple.security.automation.apple-events": True}
            ).decode("utf-8")
            return subprocess.CompletedProcess(args, 0, payload, "")
        if args[1:2] == ["-d"] and "-r-" in args:
            requirement = server_host.developer_id_requirement(self.team)
            return subprocess.CompletedProcess(
                args, 0, "", f"designated => {requirement}\n"
            )
        if args[1:2] == ["-dv"]:
            if "--arch" not in args:
                signature = "Developer ID Application" if signed else "adhoc"
                stderr = (
                    f"Identifier={BUNDLE_ID}\n"
                    "Format=app bundle with Mach-O universal (x86_64 arm64)\n"
                    f"Signature={signature}\n"
                )
                return subprocess.CompletedProcess(args, 0, "", stderr)
            arch = args[args.index("--arch") + 1]
            fields = f"Identifier={BUNDLE_ID}\nCDHash={_CDHASH[arch]}\n"
            if signed:
                fields += (
                    f"TeamIdentifier={self.team}\n"
                    "Runtime Version=14.0.0\n"
                    "Timestamp=Sep 25, 2026 at 10:00:00\n"
                    "CodeDirectory v=20500 size=400 flags=0x10000(runtime) "
                    "hashes=7+3 location=embedded\n"
                )
            return subprocess.CompletedProcess(args, 0, "", fields)
        raise AssertionError(f"unexpected native probe: {args}")


def _make_signed_bundle(root: Path, *, name: str = APP_NAME) -> Path:
    """A revision-5 signed host bundle: the sealed files plus its stapled ticket."""
    bundle = _make_bundle(root, name=name)
    info = BUILD.bundle_info()
    info["CFBundleVersion"] = str(server_host.SIGNED_HOST_REVISION)
    info["CFBundleShortVersionString"] = str(server_host.SIGNED_HOST_REVISION)
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(info, handle)
    (bundle / "Contents" / "CodeResources").write_bytes(b"stapled-ticket")
    return bundle


def _signed_fixture(
    tmp_path: Path, *, team: str = _SIGNED_TEAM, label: str = "signed"
) -> tuple[Path, Path, dict[str, Any]]:
    """A signed archive, its bundle, and the authenticated entry naming its team."""
    bundle = _make_signed_bundle(tmp_path / f"{label}-src")
    (tmp_path / label).mkdir(parents=True, exist_ok=True)
    archive = _archive_of(bundle, tmp_path / label / SERVER_HOST_FILENAME)
    entry = server_host_artifact_entry(
        archive, signing=server_host.DeveloperIDSigning(team)
    )
    return archive, bundle, entry


def _deferred(
    archive: Path,
    entry: dict[str, Any],
    *,
    target: Path,
    record: Path,
    runner: Any,
    owner_pid: int | None = None,
) -> HostOwnership:
    return server_host_install.install_server_host(
        archive,
        entry,
        bundle_path=target,
        ownership_path=record,
        runner=runner,
        defer_commit=True,
        owner_pid=os.getpid() if owner_pid is None else owner_pid,
    )


def _install_production_host(
    tmp_path: Path, *, target: Path, record: Path, runner: Any
) -> tuple[Path, dict[str, Any]]:
    archive, _bundle, entry = _fixture(tmp_path)
    server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )
    return archive, entry


@requires_posix_uid
def test_signed_install_uses_manifest_revision_and_expected_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The staged bundle is inspected under the revision and team the *entry*
    # names, and the record it writes carries that team. A team the entry names
    # but the bundle does not carry is a rejection, with nothing installed.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _signed_fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    runner = _SignedRunner()

    snapshot = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )

    assert snapshot.host_revision == server_host.SIGNED_HOST_REVISION
    assert snapshot.schema == server_host.SIGNED_SCHEMA_VERSION
    assert snapshot.signing == server_host.DeveloperIDSigning(_SIGNED_TEAM)
    assert read_host_ownership(record) == snapshot
    # The expected team reached the native verification as a requirement, never
    # as a value read back from the bundle.
    requirement = server_host.developer_id_requirement(_SIGNED_TEAM)
    assert any(
        command[:2] == [_CODESIGN, "--verify"] and f"={requirement}" in command
        for command in runner.commands
    )

    # An entry that names a team the bundle was not signed by is refused before
    # anything reaches the target.
    other_archive, _bundle, other_entry = _signed_fixture(
        tmp_path, team=_OTHER_TEAM, label="other"
    )
    fresh_target = tmp_path / "Applications-other" / APP_NAME
    with pytest.raises(server_host.ServerHostError) as caught:
        server_host_install.install_server_host(
            other_archive,
            other_entry,
            bundle_path=fresh_target,
            ownership_path=tmp_path / "other-record.json",
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host.SIGNATURE_REJECTED
    assert not fresh_target.exists()


@requires_posix_uid
def test_signed_entry_revision_without_its_policy_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A signed revision without its signing object, and a policy under the legacy
    # revision, are both entries this installer must not guess at. They are
    # release defects, refused before the target is touched.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _signed_fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    for broken in (
        {k: v for k, v in entry.items() if k != "signing"},
        {**entry, "host_revision": server_host.HOST_REVISION},
    ):
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                broken,
                bundle_path=target,
                ownership_path=record,
                runner=_SignedRunner(),
            )
        assert caught.value.code == server_host_install.ARCHIVE_MISMATCH
        assert not target.exists()
        assert not record.exists()


@requires_posix_uid
def test_legacy_four_to_signed_requires_deferred_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The production host is owned and usable, and a release that selects the
    # signed host replaces it — but only as a recoverable transaction. The exact
    # old pair is what has to survive a later engine failure, so a one-step
    # replacement is refused and the target and record are untouched.
    monkeypatch.setattr(sys, "platform", "darwin")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    _install_production_host(
        tmp_path, target=target, record=record, runner=_FakeRunner()
    )
    inode = target.stat().st_ino
    record_before = record.read_bytes()
    signed_archive, _bundle, signed_entry = _signed_fixture(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            signed_archive,
            signed_entry,
            bundle_path=target,
            ownership_path=record,
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.DEFERRED_UPGRADE_REQUIRED
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before

    # A deferred request without the outer installer's own process id is refused
    # rather than committed by nobody.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            signed_archive,
            signed_entry,
            bundle_path=target,
            ownership_path=record,
            runner=_SignedRunner(),
            defer_commit=True,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before

    # And the deferred request is what turns it into a transaction.
    snapshot = _deferred(
        signed_archive,
        signed_entry,
        target=target,
        record=record,
        runner=_SignedRunner(),
    )
    assert snapshot.host_revision == server_host.SIGNED_HOST_REVISION
    status = server_host_install.transaction_status()
    assert status["pending"] is True
    assert status["phase"] == "installed"
    assert status["owner_pid"] == os.getpid()
    assert status["owner_alive"] is True


@requires_posix_uid
def test_signed_current_host_is_noop_across_release_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same revision, same team, different rebuilt bytes: the record proves the
    # installed host is the one this policy speaks to, so nothing is replaced and
    # no native probe runs at all - a rebuild must not churn the permission macOS
    # granted to the host.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _signed_fixture(tmp_path, label="first")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    first = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_SignedRunner()
    )
    inode = target.stat().st_ino
    record_before = record.read_bytes()

    rebuild_bundle = _make_signed_bundle(tmp_path / "rebuild-src")
    (rebuild_bundle / _ICON_REL).write_bytes(b"a rebuilt icon")
    (tmp_path / "rebuild").mkdir()
    rebuild = _archive_of(rebuild_bundle, tmp_path / "rebuild" / SERVER_HOST_FILENAME)
    rebuild_entry = server_host_artifact_entry(
        rebuild, signing=server_host.DeveloperIDSigning(_SIGNED_TEAM)
    )
    quiet = _SignedRunner()

    second = server_host_install.install_server_host(
        rebuild,
        rebuild_entry,
        bundle_path=target,
        ownership_path=record,
        runner=quiet,
    )

    assert second == first
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before
    # The rebuild was never staged: every probe this run made was against the
    # installed host, which is a no-op, not a replacement.
    assert quiet.commands
    for command in quiet.commands:
        assert Path(command[-1]).is_relative_to(target)
    assert not list(target.parent.glob(f"{server_host_install.STAGING_PREFIX}*"))
    assert not server_host_install.transaction_status()["pending"]


@requires_posix_uid
def test_signed_host_refuses_team_rotation_and_legacy_downgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Never automatic: a different Developer ID team, and the legacy ad-hoc host
    # over a signed one. Both leave the installed bundle and record exactly as
    # they were.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _signed_fixture(tmp_path, label="installed")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_SignedRunner()
    )
    inode = target.stat().st_ino
    record_before = record.read_bytes()

    rotation, _bundle, rotation_entry = _signed_fixture(
        tmp_path, team=_OTHER_TEAM, label="rotation"
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            rotation,
            rotation_entry,
            bundle_path=target,
            ownership_path=record,
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.POLICY_REFUSED
    assert caught.value.code not in server_host_install.WARN_AND_CONTINUE
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before

    legacy_archive, _bundle, legacy_entry = _fixture(tmp_path)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            legacy_archive,
            legacy_entry,
            bundle_path=target,
            ownership_path=record,
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.POLICY_REFUSED
    assert caught.value.code not in server_host_install.WARN_AND_CONTINUE
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before


def test_signature_rejection_is_never_a_warn_and_continue_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A completed probe that disproved the signed identity, and a policy the
    # release may not select here, are both fatal. Only an *unavailable* optional
    # probe keeps its existing warn-and-continue status, and only because the
    # target was never changed.
    assert server_host.SIGNATURE_REJECTED not in server_host_install.WARN_AND_CONTINUE
    assert (
        server_host_install.POLICY_REFUSED not in server_host_install.WARN_AND_CONTINUE
    )
    assert (
        server_host_install.DEFERRED_UPGRADE_REQUIRED
        not in server_host_install.WARN_AND_CONTINUE
    )

    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _signed_fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")
    argv = [
        "install",
        "--manifest",
        str(manifest),
        "--signature",
        str(signature),
        "--archive",
        str(archive),
        "--public-key",
        public_key,
    ]

    def reject(*_args: Any, **_kwargs: Any) -> HostOwnership:
        raise server_host.ServerHostError("not signed by us", code=server_host.SIGNATURE_REJECTED)

    monkeypatch.setattr(server_host_install, "install_server_host", reject)
    assert server_host_install.main(argv) == 1
    assert "Error:" in capsys.readouterr().err

    def unavailable(*_args: Any, **_kwargs: Any) -> HostOwnership:
        raise server_host.ServerHostError("codesign timed out", code=server_host.INSPECTION_FAILED)

    monkeypatch.setattr(server_host_install, "install_server_host", unavailable)
    assert server_host_install.main(argv) == server_host_install.HOST_WARNING_EXIT
    assert "warning:" in capsys.readouterr().err


@requires_posix_uid
def test_wrapped_record_restore_failure_keeps_backup_and_reports_both_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record restore in the superseded-host rollback goes through
    # `_write_ownership_record`, which translates `OSError` into a
    # `ServerHostInstallError`. That wrapped refusal must not escape and replace
    # the original failure: both are reported, and the host that was put back is
    # named so the user can find it.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle
    )
    old_exe = (old_bundle / _EXE_REL).read_bytes()
    real_rename = os.rename
    real_write_record = server_host_install._write_ownership_record
    writes: list[int] = []

    def refuse_the_new_bundle(src: Any, dst: Any) -> None:
        if os.fspath(dst) == os.fspath(target) and os.path.basename(
            os.fspath(src)
        ) == APP_NAME:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    def refuse_the_restored_record(snapshot: HostOwnership, path: Path) -> None:
        writes.append(1)
        if len(writes) == 2:
            raise server_host_install.ServerHostInstallError(
                "the ownership record could not be written: No space left on device",
                code=server_host_install.INSTALL_FAILED,
            )
        real_write_record(snapshot, path)

    monkeypatch.setattr(server_host_install.os, "rename", refuse_the_new_bundle)
    monkeypatch.setattr(
        server_host_install, "_write_ownership_record", refuse_the_restored_record
    )

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    message = str(caught.value)
    # The wrapped restore refusal is reported, not swallowed by the original one.
    assert "No space left on device" in message
    assert "the install failed first" in message
    # The restored host is named and kept, whole.
    assert "the host is kept at" in message
    assert (target / _EXE_REL).read_bytes() == old_exe
    # And the superseded host was not erased to make the message tidy.
    assert (target / _EXE_REL).read_bytes() == old_exe




def test_verify_host_archive_accepts_the_signed_bytes(tmp_path: Path) -> None:
    archive, _bundle, entry = _fixture(tmp_path)

    server_host_install.verify_host_archive(archive, entry)


def test_verify_host_archive_rejects_a_wrong_digest(tmp_path: Path) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    entry = {**entry, "sha256": "ab" * 32}

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(archive, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


def test_verify_host_archive_rejects_a_wrong_size(tmp_path: Path) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    entry = {**entry, "size": entry["size"] + 1}

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(archive, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


def test_verify_host_archive_rejects_a_truncated_archive(tmp_path: Path) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    archive.write_bytes(archive.read_bytes()[:-10])

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(archive, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


@pytest.mark.parametrize(
    "entry",
    [
        {
            "kind": "wheel",
            "filename": SERVER_HOST_FILENAME,
            "sha256": "ab" * 32,
            "size": 1,
        },
        {
            "kind": "server-host",
            "filename": "other.tar.gz",
            "sha256": "ab" * 32,
            "size": 1,
        },
        {
            "kind": "server-host",
            "filename": SERVER_HOST_FILENAME,
            "sha256": "AB" * 32,
            "size": 1,
        },
        {
            "kind": "server-host",
            "filename": SERVER_HOST_FILENAME,
            "sha256": "ab" * 32,
            "size": True,
        },
        {
            "kind": "server-host",
            "filename": SERVER_HOST_FILENAME,
            "sha256": "ab" * 32,
            "size": 0,
        },
    ],
)
def test_verify_host_archive_rejects_malformed_entries(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    archive, _bundle, _entry = _fixture(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(archive, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_verify_host_archive_rejects_a_symlink(tmp_path: Path) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    link = tmp_path / SERVER_HOST_FILENAME
    link.unlink()
    link.symlink_to(archive)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(link, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


def test_verify_host_archive_rejects_a_missing_file(tmp_path: Path) -> None:
    _archive, _bundle, entry = _fixture(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_host_archive(tmp_path / "absent.tar.gz", entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH


# ── safe extraction ──────────────────────────────────────────────────────────


def test_extract_host_archive_matches_the_builder_layout(tmp_path: Path) -> None:
    archive, _bundle, _entry = _fixture(tmp_path)
    destination = tmp_path / "stage"
    destination.mkdir()

    app = server_host_install.extract_host_archive(archive, destination)

    assert app == destination / APP_NAME
    shipped = sorted(
        path.relative_to(app).as_posix() for path in app.rglob("*") if path.is_file()
    )
    assert set(shipped) == {
        _CODESIGN_REL,
        "Contents/Info.plist",
        _EXE_REL,
        _ICON_REL,
    }
    assert sorted(item.name for item in destination.iterdir()) == [APP_NAME]


def _tar_of(
    members: dict[str, bytes], *, symlink: str | None = None, absolute: bool = False
) -> bytes:
    """A host archive holding ``members`` as ``<APP_NAME>/<path>``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(f"{'/' if absolute else ''}{APP_NAME}/{name}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        if symlink is not None:
            info = tarfile.TarInfo(f"{APP_NAME}/{symlink}")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buffer.getvalue()


def test_extract_host_archive_rejects_a_symlink_member(tmp_path: Path) -> None:
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(_tar_of({_PLIST_REL: b"x"}, symlink="Contents/link"))
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.extract_host_archive(archive, destination)
    assert caught.value.code == server_host_install.ARCHIVE_UNSAFE
    assert sorted(destination.iterdir()) == []


def test_extract_host_archive_rejects_an_absolute_member(tmp_path: Path) -> None:
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(_tar_of({_PLIST_REL: b"x"}, absolute=True))
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.extract_host_archive(archive, destination)
    assert caught.value.code == server_host_install.ARCHIVE_UNSAFE


def test_extract_host_archive_rejects_a_second_root_entry(tmp_path: Path) -> None:
    # A bundle beside a stray file is not the one-app archive the builder writes.
    archive = tmp_path / SERVER_HOST_FILENAME
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{APP_NAME}/Contents")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        payload = b"stray"
        stray = tarfile.TarInfo("stray.txt")
        stray.size = len(payload)
        tar.addfile(stray, io.BytesIO(payload))
    archive.write_bytes(buffer.getvalue())
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.extract_host_archive(archive, destination)
    assert caught.value.code == server_host_install.ARCHIVE_UNSAFE


def test_extract_host_archive_rejects_a_fifo_member(tmp_path: Path) -> None:
    archive = tmp_path / SERVER_HOST_FILENAME
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{APP_NAME}/{_ICON_REL}")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)
    archive.write_bytes(buffer.getvalue())
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.extract_host_archive(archive, destination)
    assert caught.value.code == server_host_install.ARCHIVE_UNSAFE


def test_extract_host_archive_rejects_a_non_archive(tmp_path: Path) -> None:
    archive = tmp_path / SERVER_HOST_FILENAME
    archive.write_bytes(b"not a gzip archive")
    destination = tmp_path / "stage"
    destination.mkdir()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.extract_host_archive(archive, destination)
    assert caught.value.code == server_host_install.ARCHIVE_UNSAFE


# ── the atomic install, the record and the refusals ─────────────────────────


@requires_posix_uid
def test_install_writes_the_bundle_and_the_private_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    runner = _FakeRunner()

    snapshot = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )

    assert target.is_dir()
    assert snapshot.bundle_path == os.path.realpath(target)
    assert (
        snapshot.bundle_files[_EXE_REL]
        == hashlib.sha256(b"universal-host-binary").hexdigest()
    )
    assert read_host_ownership(record) == snapshot
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700
    # The staging directory is gone: nothing but the target is left in the parent.
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]
    # The staged bundle was proven before it was renamed.
    assert any(command[1] == "--verify" for command in runner.commands)


@requires_posix_uid
def test_install_is_a_noop_when_the_same_host_is_already_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    runner = _FakeRunner()

    first = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )
    inode = target.stat().st_ino
    record_bytes = record.read_bytes()

    second = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )

    assert second == first
    # The bundle was not replaced and the record was not rewritten.
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_bytes


@requires_posix_uid
def test_require_absent_refuses_a_current_host_and_never_returns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The legacy no-op is a feature, not a fresh-install answer. With the opt-in
    # flag the same host is a refusal: a native run must never return a host it
    # did not create. The record check refuses first; the classify-time check is
    # the second line of defence when the record appears after that check.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    first = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )
    inode = target.stat().st_ino
    record_bytes = record.read_bytes()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_bytes

    # A record/bundle that appeared after the name check reaches ``_classify_host``
    # as a current host; the flag still turns that into a refusal.
    monkeypatch.setattr(server_host_install, "_refuse_present_record", lambda _path: None)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_bytes
    assert first.bundle_path == os.path.realpath(target)


@requires_posix_uid
def test_require_absent_refuses_an_orphan_record_and_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record with no bundle is the partial state a retry must not silently
    # replace. It is refused before any staging or record write.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )
    record_bytes = record.read_bytes()
    shutil.rmtree(target)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert not os.path.lexists(target)
    assert record.read_bytes() == record_bytes


@requires_posix_uid
def test_require_absent_rechecks_a_record_that_appears_while_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller's preflight and the install's first check both saw no record.
    # A same-UID writer creates one after the staging inspection; the pre-write
    # check must refuse instead of replacing it, and the stage is removed.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True)
    os.chmod(record.parent, 0o700)
    real_fsync_tree = server_host_install._fsync_tree

    def interleave(root: Path) -> None:
        real_fsync_tree(root)
        record.write_bytes(b"a record that appeared during staging")

    monkeypatch.setattr(server_host_install, "_fsync_tree", interleave)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert not os.path.lexists(target)
    assert record.read_bytes() == b"a record that appeared during staging"
    assert sorted(item.name for item in target.parent.iterdir()) == []


@requires_posix_uid
def test_require_absent_refuses_an_unsafe_bundle_or_record_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    target.parent.mkdir(parents=True)
    elsewhere = tmp_path / "not-a-host"
    elsewhere.mkdir()
    target.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.TARGET_UNSAFE
    assert target.is_symlink()
    assert not record.exists()

    target.unlink()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.symlink_to(elsewhere / "record.json")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert record.is_symlink()
    assert not os.path.lexists(target)


@requires_posix_uid
def test_require_absent_refuses_deferred_upgrade_flags_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=_target(tmp_path),
            ownership_path=tmp_path / "records" / "server-host.json",
            runner=_FakeRunner(),
            defer_commit=True,
            owner_pid=os.getpid(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED


@requires_posix_uid
def test_install_refuses_a_foreign_bundle_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    (target / "Contents").mkdir(parents=True)
    (target / "marker").write_text("someone else's host")
    record = tmp_path / "records" / "server-host.json"
    runner = _FakeRunner()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=runner
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert (target / "marker").read_text() == "someone else's host"
    assert not record.exists()
    # A foreign bundle never reaches a native probe.
    assert runner.commands == []


@requires_posix_uid
def test_install_refuses_a_revision_one_host_without_touching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A revision-1 bundle is named CiaobotServerHost. Its own record says
    # revision 1, which the revision-2 installer does not accept, so the install
    # is refused and neither the bundle nor the record is replaced.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    legacy_exe = target / "Contents" / "MacOS" / "CiaobotServerHost"
    legacy_exe.parent.mkdir(parents=True)
    legacy_exe.write_bytes(b"revision-one-host-binary")
    info = BUILD.bundle_info()
    info["CFBundleExecutable"] = "CiaobotServerHost"
    info["CFBundleVersion"] = "1"
    info["CFBundleShortVersionString"] = "1"
    with (target / _PLIST_REL).open("wb") as handle:
        plistlib.dump(info, handle)
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"host_revision": 1, "bundle_path": str(target)}), encoding="utf-8"
    )
    exe_before = legacy_exe.read_bytes()
    record_before = record.read_bytes()
    runner = _FakeRunner()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=runner
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert legacy_exe.read_bytes() == exe_before
    assert record.read_bytes() == record_before
    assert runner.commands == []


@requires_posix_uid
@pytest.mark.parametrize("revision", [2, 3])
def test_install_replaces_a_superseded_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: int
) -> None:
    # The upgrade path for the revision bump (#1659). Without it the bump
    # silently de-installs the host on every existing Mac: a superseded
    # record is refused, the installer says "a host already exists", and the
    # engine install continues without a host at all.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent, revision)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle, revision
    )
    old_exe = (old_bundle / _EXE_REL).read_bytes()
    runner = _FakeRunner()

    snapshot = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=runner
    )

    # The new host is in place, and the record vouches for it.
    assert snapshot.host_revision == 4
    assert snapshot.bundle_path == os.path.realpath(target)
    assert (target / _EXE_REL).read_bytes() == b"universal-host-binary"
    assert read_host_ownership(record) == snapshot
    # The old bundle is gone, not left lying beside the new one.
    assert not (target.parent / f"{APP_NAME}.old").exists()
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_install_restores_the_superseded_host_when_the_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A replacement that fails must leave the Mac exactly as it was: the old
    # bundle and the old record, together, so the host still verifies.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle
    )
    old_exe = (old_bundle / _EXE_REL).read_bytes()
    record_before = record.read_bytes()
    runner = _FakeRunner()
    real_rename = os.rename

    def refuse_the_new_bundle(src: Any, dst: Any) -> None:
        # Moving the superseded host aside succeeds; the rename of the new
        # bundle onto the target fails, which is the point at which the old
        # host and its record have to come back. The rollback's own rename
        # (the moved-aside copy back onto the target) is not refused.
        if os.fspath(dst) == os.fspath(target) and os.path.basename(
            os.fspath(src)
        ) == APP_NAME:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(server_host_install.os, "rename", refuse_the_new_bundle)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=runner
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED

    # The old bundle is back, byte for byte, and so is its record.
    assert target.is_dir()
    assert (target / _EXE_REL).read_bytes() == old_exe
    assert record.read_bytes() == record_before
    # The old host still verifies against its own record: the pair was
    # restored, not half of it.
    assert server_host.verify_superseded_host(
        target, ownership_path=record, runner=_FakeRunner()
    ) is not None
    # The staging directory is gone: nothing of this run survives.
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_a_failed_rollback_names_where_the_superseded_host_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A rollback that cannot put the old host back stops there and says where
    # the host is. The old bundle is kept, not deleted: the user must be able
    # to find it and put it back by hand.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle
    )
    old_exe = (old_bundle / _EXE_REL).read_bytes()
    runner = _FakeRunner()
    real_rename = os.rename

    def refuse_every_rename_to_the_target(src: Any, dst: Any) -> None:
        # Both the new bundle's rename and the rollback's own rename fail.
        if os.fspath(dst) == os.fspath(target):
            raise OSError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(
        server_host_install.os, "rename", refuse_every_rename_to_the_target
    )

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=runner
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    # The error names where the old host is, so the user can find it.
    assert "it is kept at" in str(caught.value)
    assert "superseded host" in str(caught.value)

    # The old bundle is kept somewhere on disk, whole, and nowhere
    # half-installed: the staging copy named in the error is still there.
    kept = [
        candidate
        for candidate in target.parent.glob("**/superseded")
        if (candidate / _EXE_REL).is_file()
        and (candidate / _EXE_REL).read_bytes() == old_exe
    ]
    assert kept, "the superseded host was not kept anywhere"
    # The error names that path, so the user can find it.
    assert str(kept[0]) in str(caught.value)
    # The target itself holds no half-installed bundle.
    assert not target.exists() or not (target / _EXE_REL).exists()


@requires_posix_uid
def test_install_refuses_a_host_it_cannot_prove_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The replacement path must not weaken the refusal: a bundle at the target
    # whose record does not prove it is ours is still refused and left
    # untouched.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    foreign = target.parent / APP_NAME
    (foreign / "Contents").mkdir(parents=True)
    (foreign / "marker").write_text("someone else's host")
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    # A record that names a different bundle: it proves nothing about this one.
    record.write_text(
        json.dumps(
            {
                "schema": 1,
                "bundle_path": os.fspath(tmp_path / "elsewhere" / APP_NAME),
                "bundle_id": BUNDLE_ID,
                "host_revision": 3,
                "host_protocol": 2,
                "executable_sha256": "a" * 64,
                "per_arch_cdhashes": dict(_CDHASH),
                "bundle_files": {
                    _PLIST_REL: "a" * 64,
                    _EXE_REL: "a" * 64,
                    _ICON_REL: "a" * 64,
                    _CODESIGN_REL: "a" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(record, 0o600)
    marker = (foreign / "marker").read_text()
    runner = _FakeRunner()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive, entry, bundle_path=target, ownership_path=record, runner=runner
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert (foreign / "marker").read_text() == marker
    # A foreign bundle never reaches a native probe.
    assert runner.commands == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@requires_posix_uid
def test_install_refuses_a_symlinked_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(bundle)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.TARGET_UNSAFE
    assert target.is_symlink()


@requires_posix_uid
def test_install_refuses_a_tampered_archive_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    # A second bundle is built and archived, but the entry still names the first
    # archive's digest: the bytes extracted are not the signed ones.
    bundle = _make_bundle(tmp_path / "src", name=APP_NAME)
    (bundle / _ICON_REL).write_bytes(b"a different icon")
    tampered = _archive_of(bundle, tmp_path / SERVER_HOST_FILENAME)
    entry = server_host_artifact_entry(tampered)
    entry = {**entry, "sha256": "ab" * 32}
    target = _target(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            tampered,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH
    assert not target.exists()


@requires_posix_uid
def test_install_refuses_a_foreign_bundle_inside_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The archive's digest matches its entry, but the bundle inside is not the
    # signed identity: the plist check refuses it before the rename.
    monkeypatch.setattr(sys, "platform", "darwin")
    bundle = _make_bundle(tmp_path / "src")
    document = BUILD.bundle_info()
    document["CFBundleIdentifier"] = "local.other"
    with (bundle / _PLIST_REL).open("wb") as handle:
        plistlib.dump(document, handle)
    archive = _archive_of(bundle, tmp_path / SERVER_HOST_FILENAME)
    entry = server_host_artifact_entry(archive)
    target = _target(tmp_path)

    with pytest.raises(server_host.ServerHostError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host.INSPECTION_FAILED
    assert not target.exists()
    # The staging directory was cleaned up and nothing reached ~/Applications.
    assert (
        not target.parent.exists()
        or sorted(item.name for item in target.parent.iterdir()) == []
    )


@requires_posix_uid
def test_install_refuses_a_failing_signature_probe_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)

    with pytest.raises(server_host.ServerHostError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(verify_rc=1),
        )
    assert caught.value.code == server_host.INSPECTION_FAILED
    assert not target.exists()


@requires_posix_uid
def test_install_refuses_a_relative_or_misnamed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=Path("relative") / APP_NAME,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=tmp_path / "Other.app",
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED


@pytest.mark.parametrize("platform_name", ["linux", "win32"])
def test_install_refuses_a_non_macos_platform_before_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_name: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "record.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host.UNSUPPORTED_PLATFORM
    assert not target.exists()


@requires_posix_uid
def test_a_failed_record_write_leaves_no_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A record write that fails after the rename removes the bundle this run
    # placed, so a recordless host is never left behind.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    def refuse_record(_snapshot: HostOwnership, _path: Path) -> None:
        raise server_host_install.ServerHostInstallError(
            "disk full", code="install_failed"
        )

    monkeypatch.setattr(server_host_install, "_write_ownership_record", refuse_record)

    with pytest.raises(server_host_install.ServerHostInstallError):
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert not target.exists()
    assert not record.exists()


@requires_posix_uid
def test_a_failed_final_verification_leaves_neither_bundle_nor_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is written, then the re-verification refuses: the bundle this
    # run placed and the record it wrote for it are both taken away, so no
    # record survives naming a bundle that is gone.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    def refuse(*_args: Any, **_kwargs: Any) -> HostOwnership:
        raise server_host.ServerHostError("mismatch", code="not_owned")

    monkeypatch.setattr(server_host_install, "verify_owned_host", refuse)

    with pytest.raises(server_host.ServerHostError):
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert not target.exists()
    assert not record.exists()


@requires_posix_uid
def test_the_record_is_written_before_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The window between the rename and a record write contains a full native
    # re-inspection, so the record is written from the staged snapshot first. At
    # the instant of the rename the record is already on disk.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    real_rename = os.rename
    seen: dict[str, bool] = {}

    def observing_rename(src: Any, dst: Any) -> None:
        seen["record_before_rename"] = record.exists()
        real_rename(src, dst)

    monkeypatch.setattr(server_host_install.os, "rename", observing_rename)

    server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert seen["record_before_rename"] is True


@requires_posix_uid
def test_a_concurrent_creation_is_host_exists_and_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two one-liners race: B writes the record before its rename, then finds A
    # already renamed an identical host into place (ENOTEMPTY). That is
    # host_exists, the warn-and-continue code - and B must leave the record: it
    # can only verify A's byte-identical bundle, so unlinking it would strip the
    # winner of the record it needs and leave it recordless.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    def racing_rename(src: Any, dst: Any) -> None:
        # The winner A lands its identical bundle at the target first, then B's
        # own rename finds it there and fails the way a concurrent one would.
        if not os.path.lexists(dst):
            shutil.copytree(src, dst, symlinks=True)
        raise OSError(errno.ENOTEMPTY, "Directory not empty")

    monkeypatch.setattr(server_host_install.os, "rename", racing_rename)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert caught.value.code in server_host_install.WARN_AND_CONTINUE

    # The winner's host and B's record both survive, and the host reads as ours.
    assert target.is_dir()
    assert record.exists()
    assert verify_owned_host(
        target, ownership_path=record, runner=_FakeRunner()
    ) == read_host_ownership(record)


@requires_posix_uid
def test_an_eexist_rename_is_host_exists_not_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same code mapping with nothing actually landing at the target: EEXIST
    # is "not ours to replace", the warn-and-continue code, not a local install
    # failure. The record is still kept - it names the canonical path and can
    # only ever verify a bundle byte-identical to this run's.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    def refuse_rename(_src: Any, _dst: Any) -> None:
        raise OSError(errno.EEXIST, "File exists")

    monkeypatch.setattr(server_host_install.os, "rename", refuse_rename)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert caught.value.code in server_host_install.WARN_AND_CONTINUE
    assert not os.path.lexists(target)
    assert record.exists()


@requires_posix_uid
def test_a_non_eexist_rename_failure_is_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    def refuse_rename(_src: Any, _dst: Any) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(server_host_install.os, "rename", refuse_rename)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert not record.exists()


# ── a killed run leaves a consistent host, not a recordless bundle ───────────


#: A whole separate interpreter that installs the host with a rename that kills
#: the process *at the instant the bundle lands*, before any rollback or
#: ``finally`` can run — the SIGKILL/SIGTERM window R2 is about. It runs in a
#: fresh process rather than a fork so it is safe under the parallel test runner.
_KILL_AFTER_RENAME_CHILD = """
import os
import sys

# The host is a macOS bundle; the child models that on any CI platform. The
# extended-ACL question is native: on a host without the macOS API the harness
# supplies its own answer rather than reaching for a missing symbol.
sys.platform = "darwin"

from tests.macos_acl_simulation import install_hermetic_macos_acl

install_hermetic_macos_acl()

from pathlib import Path

from ciao import server_host_install
from ciao.release_manifest import server_host_artifact_entry

if os.environ.get("CIAO_KILL_FAKE_RUNNER"):
    # The fixture bundle carries no real signature; drive the native probes with
    # the test's fake runner. The real-build variant leaves this unset and uses
    # the real subprocess runner against the real signed host.
    from tests.test_server_host_install import _FakeRunner

    runner = _FakeRunner()
else:
    import subprocess

    runner = subprocess.run

archive = Path(sys.argv[1])
bundle = Path(sys.argv[2])
record = Path(sys.argv[3])
entry = server_host_artifact_entry(archive)

_real_rename = server_host_install.os.rename


def _killing_rename(src, dst):
    _real_rename(src, dst)
    # The kill: no exception, no rollback, no `finally`. Exactly a SIGKILL.
    os._exit(0)


server_host_install.os.rename = _killing_rename
try:
    server_host_install.install_server_host(
        archive, entry, bundle_path=bundle, ownership_path=record, runner=runner
    )
except BaseException:
    os._exit(70)
os._exit(71)
"""


@requires_posix_uid
def test_a_kill_after_the_rename_leaves_a_verified_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kill the process the exact instant after os.rename and before the final
    # verification. Rollback never runs, so the stage dir is stranded - but the
    # record was written first, so the bundle at the target is a consistent,
    # verifiable host and the NEXT install reports it as verified (a no-op),
    # rather than host_exists until the user deletes it by hand.
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _KILL_AFTER_RENAME_CHILD,
            str(archive),
            str(target),
            str(record),
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "CIAO_KILL_FAKE_RUNNER": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    # The parent's own follow-up install runs in *this* interpreter, so it needs
    # the same platform patch the child applies to itself; without it
    # `install_server_host` raises unsupported_platform off macOS. The child is a
    # separate process and does not inherit this.
    monkeypatch.setattr(sys, "platform", "darwin")

    # The kill landed after the rename: the bundle and its record are both there.
    assert target.is_dir()
    assert read_host_ownership(record).bundle_path == os.path.realpath(target)

    # The next install recognises the host as its own and is a no-op.
    inode = target.stat().st_ino
    snapshot = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )
    assert snapshot.bundle_path == os.path.realpath(target)
    assert target.stat().st_ino == inode


# ── the record reader sees exactly what was written ──────────────────────────


@requires_posix_uid
def test_record_written_by_install_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "server-host.json"

    snapshot = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert json.loads(record.read_text(encoding="utf-8")) == snapshot.to_record()
    assert (
        verify_owned_host(target, ownership_path=record, runner=_FakeRunner())
        == snapshot
    )


# ── the CLI the shell installer drives ───────────────────────────────────────


def _signed_manifest(tmp_path: Path, *entries: dict[str, Any]) -> tuple[Path, Path]:
    """Write a signed manifest and its signature; return both paths."""
    from tests.test_release_manifest import _keypair, _sign

    private_key, public_key, key_id = _keypair()
    raw = json.dumps(
        build_manifest("1.2.3", list(entries), created="2026-09-25T10:00:00+00:00")
    ).encode()
    manifest = tmp_path / "ciaobot-engine-manifest.json"
    manifest.write_bytes(raw)
    signature = tmp_path / "ciaobot-engine-manifest.json.sig"
    signature.write_text(_sign(raw, private_key, key_id), encoding="utf-8")
    (tmp_path / "public-key.txt").write_text(public_key, encoding="utf-8")
    return manifest, signature


def test_select_prints_nothing_for_a_wheel_only_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wheel = tmp_path / "ciaobot-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    manifest, signature = _signed_manifest(
        tmp_path, artifact_entry(wheel, kind="wheel")
    )
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    assert (
        server_host_install.main(
            ["select", str(manifest), str(signature), "--public-key", public_key]
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_select_prints_the_signed_host_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    assert (
        server_host_install.main(
            ["select", str(manifest), str(signature), "--public-key", public_key]
        )
        == 0
    )
    assert capsys.readouterr().out.split() == [
        entry["filename"],
        entry["sha256"],
        str(entry["size"]),
    ]


def test_select_rejects_a_tampered_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")
    raw = manifest.read_bytes().replace(b'"1.2.3"', b'"9.9.9"')
    manifest.write_bytes(raw)

    assert (
        server_host_install.main(
            ["select", str(manifest), str(signature), "--public-key", public_key]
        )
        == 1
    )
    assert capsys.readouterr().out == ""


def test_select_skips_a_host_this_engine_cannot_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A future host revision, authenticated by the same release key, must not
    # refuse the wheel install on an engine that predates it - the same rule
    # verify_manifest follows. The selector's refusal becomes a spoken skip.
    future = _host_entry(
        filename="ciaobot-server-host-macos-universal-v2.tar.gz",
        host_revision=5,
        host_protocol=3,
    )
    manifest, signature = _signed_manifest(tmp_path, future)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    assert (
        server_host_install.main(
            ["select", str(manifest), str(signature), "--public-key", public_key]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot install" in captured.err


def test_select_refuses_an_ambiguous_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two host entries are ambiguous, and which bytes to trust is exactly what
    # the selector refuses to guess. The install is skipped, not guessed at.
    manifest, signature = _signed_manifest(tmp_path, _host_entry(), _host_entry())
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    assert (
        server_host_install.main(
            ["select", str(manifest), str(signature), "--public-key", public_key]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot install" in captured.err


def _host_entry(**overrides: Any) -> dict[str, Any]:
    from ciao.release_manifest import (
        SERVER_HOST_ARCH,
        SERVER_HOST_BUNDLE_ID,
        SERVER_HOST_PLATFORM,
        SERVER_HOST_PROTOCOL,
        SERVER_HOST_REVISION,
    )

    entry: dict[str, Any] = {
        "filename": SERVER_HOST_FILENAME,
        "kind": "server-host",
        "platform": SERVER_HOST_PLATFORM,
        "arch": SERVER_HOST_ARCH,
        "sha256": "cd" * 32,
        "size": 4096,
        "bundle_id": SERVER_HOST_BUNDLE_ID,
        "host_revision": SERVER_HOST_REVISION,
        "host_protocol": SERVER_HOST_PROTOCOL,
    }
    entry.update(overrides)
    return entry


@requires_posix_uid
def test_install_cli_installs_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")
    target = _target(tmp_path)
    record = tmp_path / "record.json"

    # The CLI has no runner seam, and the fixture bundle carries no real
    # signature, so the module's two native-probing calls are answered by the
    # contract functions with the fake runner. Everything else - the manifest
    # verification, the archive gate, the extraction, the atomic rename and the
    # record write - is the code under test.
    def fake_inspect(path: Path, **_: Any) -> HostOwnership:
        return server_host.inspect_host_bundle(path, runner=_FakeRunner())

    def fake_verify(path: Path, *, ownership_path: Path, **_: Any) -> HostOwnership:
        expected = read_host_ownership(ownership_path)
        assert server_host.inspect_host_bundle(path, runner=_FakeRunner()) == expected
        return expected

    monkeypatch.setattr(server_host_install, "inspect_host_bundle", fake_inspect)
    monkeypatch.setattr(server_host_install, "verify_owned_host", fake_verify)

    assert (
        server_host_install.main(
            [
                "install",
                "--manifest",
                str(manifest),
                "--signature",
                str(signature),
                "--archive",
                str(archive),
                "--public-key",
                public_key,
                "--bundle-path",
                str(target),
                "--ownership-path",
                str(record),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == os.path.realpath(target)
    assert read_host_ownership(record).bundle_path == os.path.realpath(target)
    assert target.is_dir()


@requires_posix_uid
def test_install_cli_exits_3_for_a_host_it_cannot_prove_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A bundle already at the target without a matching record is left alone,
    # and the CLI says so with its own status so the shell installer can carry
    # on with the engine instead of failing every later update.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")
    target = _target(tmp_path)
    target.mkdir(parents=True)
    (target / "foreign").write_text("not ours", encoding="utf-8")
    record = tmp_path / "record.json"

    status = server_host_install.main(
        [
            "install",
            "--manifest",
            str(manifest),
            "--signature",
            str(signature),
            "--archive",
            str(archive),
            "--public-key",
            public_key,
            "--bundle-path",
            str(target),
            "--ownership-path",
            str(record),
        ]
    )

    assert status == server_host_install.HOST_WARNING_EXIT == 3
    assert "warning:" in capsys.readouterr().err
    assert (target / "foreign").read_text(encoding="utf-8") == "not ours"
    assert not record.exists()


@requires_posix_uid
def test_install_cli_exits_1_for_an_archive_that_does_not_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")
    archive.write_bytes(b"nobody signed this")
    target = _target(tmp_path)

    status = server_host_install.main(
        [
            "install",
            "--manifest",
            str(manifest),
            "--signature",
            str(signature),
            "--archive",
            str(archive),
            "--public-key",
            public_key,
            "--bundle-path",
            str(target),
            "--ownership-path",
            str(tmp_path / "record.json"),
        ]
    )

    assert status == 1
    assert not os.path.lexists(target)


@requires_posix_uid
@pytest.mark.parametrize(
    "code",
    [
        server_host_install.INSTALL_FAILED,
        server_host.INSPECTION_FAILED,
        server_host_install.ARCHIVE_UNSAFE,
        server_host_install.HOST_EXISTS,
        server_host_install.TARGET_UNSAFE,
    ],
)
def test_install_cli_warns_and_exits_3_for_a_non_release_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    code: str,
) -> None:
    # Every refusal the shell cannot already rule out is exit 3, so the shell
    # warns and carries on with the engine rather than aborting the one-liner.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    def refuse(*_args: Any, **_kwargs: Any) -> HostOwnership:
        raise server_host.ServerHostError(f"refused: {code}", code=code)

    monkeypatch.setattr(server_host_install, "install_server_host", refuse)

    status = server_host_install.main(
        [
            "install",
            "--manifest",
            str(manifest),
            "--signature",
            str(signature),
            "--archive",
            str(archive),
            "--public-key",
            public_key,
        ]
    )

    assert status == server_host_install.HOST_WARNING_EXIT == 3
    assert "warning:" in capsys.readouterr().err


@requires_posix_uid
def test_install_cli_is_fatal_for_an_archive_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ARCHIVE_MISMATCH is the one install refusal that is about the release: the
    # downloaded bytes are not the signed ones. It stays exit 1 and stops the
    # engine install.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    manifest, signature = _signed_manifest(tmp_path, entry)
    public_key = (tmp_path / "public-key.txt").read_text(encoding="utf-8")

    def refuse(*_args: Any, **_kwargs: Any) -> HostOwnership:
        raise server_host_install.ServerHostInstallError(
            "the downloaded server host does not match the signed manifest",
            code=server_host_install.ARCHIVE_MISMATCH,
        )

    monkeypatch.setattr(server_host_install, "install_server_host", refuse)

    status = server_host_install.main(
        [
            "install",
            "--manifest",
            str(manifest),
            "--signature",
            str(signature),
            "--archive",
            str(archive),
            "--public-key",
            public_key,
        ]
    )

    assert status == 1
    assert "Error:" in capsys.readouterr().err


# ── macOS-only integration: the real universal host ──────────────────────────


@requires_macos_tools
def test_real_build_installs_records_and_reinstalls_as_a_noop(tmp_path: Path) -> None:
    """Build the real host, install it into the scratch home, and re-verify.

    The default paths are used, so this proves the home-anchored
    ``~/Applications/Ciaobot Server.app`` and
    ``~/.local/state/ciaobot/server-host.json`` are where the install lands. A
    second install of the same bytes is a no-op, and a changed bundle at the
    target is refused and left in place.
    """
    result = BUILD.build(tmp_path / "build")
    archive: Path = result["paths"]["archive"]
    entry = server_host_artifact_entry(archive)
    bundle = server_host.default_bundle_path()
    record = server_host.default_ownership_path()

    snapshot = server_host_install.install_server_host(archive, entry)

    assert bundle.is_dir()
    assert snapshot.bundle_path == os.path.realpath(bundle)
    assert set(snapshot.bundle_files) == server_host.REQUIRED_BUNDLE_FILES
    assert read_host_ownership(record) == snapshot
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700

    inode = bundle.stat().st_ino
    record_bytes = record.read_bytes()
    again = server_host_install.install_server_host(archive, entry)
    assert again == snapshot
    assert bundle.stat().st_ino == inode
    assert record.read_bytes() == record_bytes

    (bundle / _ICON_REL).write_bytes(b"tampered icon")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(archive, entry)
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert (bundle / _ICON_REL).read_bytes() == b"tampered icon"


@requires_macos_tools
def test_real_build_and_a_second_archive_with_different_bytes_refuse(
    tmp_path: Path,
) -> None:
    # The first archive installs; an unrelated archive whose entry names a
    # different digest is refused by the archive gate, and the installed host is
    # untouched.
    first = BUILD.build(tmp_path / "one")
    archive: Path = first["paths"]["archive"]
    entry = server_host_artifact_entry(archive)
    bundle = server_host.default_bundle_path()
    snapshot = server_host_install.install_server_host(archive, entry)

    other = tmp_path / "other.tar.gz"
    other.write_bytes(b"not the signed archive")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(other, entry)
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH
    assert verify_owned_host(bundle, runner=subprocess.run) == snapshot


@requires_macos_tools
def test_real_build_killed_after_the_rename_is_verified_next_run(
    tmp_path: Path,
) -> None:
    # The R2 window on real signed bytes: build the universal host, then run the
    # install in a fresh process that exits the instant the bundle lands at its
    # canonical path, before the final verify. Nothing rolled back, but the
    # record was written first, so the next install verifies the host as ours
    # rather than reporting host_exists.
    result = BUILD.build(tmp_path / "build")
    archive: Path = result["paths"]["archive"]
    bundle = server_host.default_bundle_path()
    record = server_host.default_ownership_path()

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _KILL_AFTER_RENAME_CHILD,
            str(archive),
            str(bundle),
            str(record),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    # The kill landed after the rename: a consistent host and its record exist.
    assert bundle.is_dir()
    assert read_host_ownership(record).bundle_path == os.path.realpath(bundle)

    inode = bundle.stat().st_ino
    again = server_host_install.install_server_host(
        archive, server_host_artifact_entry(archive)
    )
    assert again.bundle_path == os.path.realpath(bundle)
    assert bundle.stat().st_ino == inode


@requires_posix_uid
def test_require_absent_keeps_the_record_it_wrote_if_a_bundle_wins_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The last narrow window, with the real rename: after this call writes its
    # record, another installer materializes an actual bundle at the target. The
    # real os.rename then fails EEXIST/ENOTEMPTY; the run refuses with HOST_EXISTS
    # and, like the legacy path, does not unlink the record it wrote. The caller
    # never receives the winner's snapshot; the bundle and record are preserved.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, source_bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    real_write = server_host_install._write_ownership_record

    def write_then_competing_bundle(
        snapshot: server_host.HostOwnership, path: Path, **kwargs: object
    ) -> object:
        identity = real_write(snapshot, path, **kwargs)  # type: ignore[arg-type]
        shutil.copytree(source_bundle, target)
        return identity

    monkeypatch.setattr(
        server_host_install, "_write_ownership_record", write_then_competing_bundle
    )

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.HOST_EXISTS
    assert target.is_dir()
    assert record.is_file()
    # The preserved record is the one this call wrote, with the canonical target.
    assert read_host_ownership(record).bundle_path == os.path.realpath(target)
    assert not [
        item.name
        for item in target.parent.iterdir()
        if item.name.startswith(server_host_install.STAGING_PREFIX)
    ]


@requires_posix_uid
def test_require_absent_does_not_delete_a_record_replaced_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed fresh install cleans up only the record it wrote. If a same-UID
    # writer replaces it before cleanup, the substitute is left in place and the
    # failure is reported as an unproven cleanup, not as a successful removal.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    substitute = b"a record another writer put here"

    def replace_record_then_fail(*args: object, **kwargs: object) -> object:
        record.write_bytes(substitute)
        raise server_host.ServerHostError(
            "the host changed after it was installed", code=server_host.NOT_OWNED
        )

    monkeypatch.setattr(server_host_install, "verify_owned_host", replace_record_then_fail)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert record.read_bytes() == substitute
    # The bundle this call created was still uniquely ours, so it was removed.
    assert not os.path.lexists(target)


@requires_posix_uid
def test_require_absent_does_not_delete_a_bundle_replaced_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same rule for the bundle: once its inode no longer matches what this
    # call renamed into place, cleanup leaves the substitute and the record alone.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    ours = tmp_path / "our-installed-host"

    def replace_bundle_then_fail(*args: object, **kwargs: object) -> object:
        os.rename(target, ours)
        target.mkdir()
        (target / "marker").write_text("a substitute at the target")
        raise server_host.ServerHostError(
            "the host changed after it was installed", code=server_host.NOT_OWNED
        )

    monkeypatch.setattr(server_host_install, "verify_owned_host", replace_bundle_then_fail)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert (target / "marker").read_text() == "a substitute at the target"
    assert ours.is_dir()
    assert record.is_file()


def test_require_absent_rollback_renames_then_removes_only_its_own_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed fresh install never hands the canonical path to rmtree. It renames
    # the bundle it placed into a private name inside its staging directory, then
    # removes that entry.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    renames: list[tuple[Path, Path]] = []
    real_rename = os.rename

    def spy(src: str, dst: str) -> None:
        renames.append((Path(src), Path(dst)))
        real_rename(src, dst)

    def refuse(*args: object, **kwargs: object) -> object:
        raise server_host.ServerHostError(
            "the host is not proven ours", code=server_host.NOT_OWNED
        )

    monkeypatch.setattr(server_host_install.os, "rename", spy)
    monkeypatch.setattr(server_host_install, "verify_owned_host", refuse)

    # A clean rollback re-raises the original failure unchanged; only a leftover
    # problem is wrapped as INSTALL_FAILED.
    with pytest.raises(server_host.ServerHostError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host.NOT_OWNED
    assert not target.exists()
    assert not record.exists()
    assert not list(target.parent.glob(f"{server_host_install.STAGING_PREFIX}*"))
    moved = [
        dst
        for src, dst in renames
        if src == target
        and dst.name.startswith(server_host_install._FRESH_ROLLBACK_PREFIX)
    ]
    assert len(moved) == 1
    assert moved[0].parent.name.startswith(server_host_install.STAGING_PREFIX)


def test_require_absent_rollback_keeps_a_substitute_swapped_in_before_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A same-UID writer swaps the canonical path between the install's rename and
    # the rollback's move-aside. The object the rollback moved is not ours: it is
    # kept in the staging directory and named in the error, and our own bundle
    # is never deleted.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    ours = tmp_path / "our-installed-host"
    real_rename = os.rename

    def swap_before_move(src: str, dst: str) -> None:
        if Path(dst).name.startswith(server_host_install._FRESH_ROLLBACK_PREFIX):
            real_rename(target, ours)
            target.mkdir()
            (target / "marker").write_text("substitute")
        real_rename(src, dst)

    def refuse(*args: object, **kwargs: object) -> object:
        raise server_host.ServerHostError(
            "the host is not proven ours", code=server_host.NOT_OWNED
        )

    monkeypatch.setattr(server_host_install.os, "rename", swap_before_move)
    monkeypatch.setattr(server_host_install, "verify_owned_host", refuse)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert "it is kept at" in str(caught.value)
    assert ours.is_dir()
    kept = list(
        target.parent.glob(
            f"{server_host_install.STAGING_PREFIX}*/"
            f"{server_host_install._FRESH_ROLLBACK_PREFIX}*/marker"
        )
    )
    assert [path.read_text() for path in kept] == ["substitute"]
    assert not target.exists()


def test_require_absent_fresh_surface_checks_run_three_times_in_order(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fresh-surface check is one helper, called before the directories are
    # prepared, after they are prepared, and again before the first write.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(home_dir)
    real_check = server_host_install._require_fresh_surface
    moments: list[str] = []

    def spy(
        bundle: Path, record_path: Path, trusted_root: Path, *, moment: str
    ) -> None:
        moments.append(moment)
        real_check(bundle, record_path, trusted_root, moment=moment)

    monkeypatch.setattr(server_host_install, "_require_fresh_surface", spy)

    server_host_install.install_server_host(
        archive,
        entry,
        bundle_path=server_host.default_bundle_path(),
        ownership_path=server_host.default_ownership_path(),
        runner=_FakeRunner(),
        require_absent=True,
        trusted_root=home_dir,
    )

    assert moments == ["before", "preparing", "staging"]


def test_fresh_surface_rejects_an_unknown_moment(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown fresh-surface moment"):
        server_host_install._require_fresh_surface(
            _target(tmp_path),
            tmp_path / "records" / "server-host.json",
            tmp_path,
            moment="later",
        )


@requires_posix_uid
def test_require_absent_rejects_dotdot_dot_and_relative_paths_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    cases: list[object] = [
        tmp_path / "Applications" / ".." / "Applications" / APP_NAME,
        f"{tmp_path}/./Applications/{APP_NAME}",
        Path(".") / "Applications" / APP_NAME,
    ]

    for bundle_path in cases:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                bundle_path=bundle_path,  # type: ignore[arg-type]
                ownership_path=record,
                runner=_FakeRunner(),
                require_absent=True,
                trusted_root=tmp_path,
            )
        assert caught.value.code == server_host_install.INSTALL_FAILED
        assert not os.path.lexists(record)
        assert not os.path.lexists(tmp_path / "Applications")


@requires_posix_uid
def test_require_absent_rejects_a_record_inside_the_bundle_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = target / "server-host.json"

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


@requires_posix_uid
def test_require_absent_rejects_symlinked_bundle_or_record_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    real_apps = tmp_path / "real-apps"
    real_apps.mkdir()
    apps = tmp_path / "Applications"
    apps.symlink_to(real_apps, target_is_directory=True)
    record = tmp_path / "records" / "server-host.json"

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert not any(real_apps.iterdir())
    assert not os.path.lexists(record)

    apps.unlink()
    apps.mkdir()
    real_records = tmp_path / "real-records"
    real_records.mkdir()
    records = tmp_path / "records"
    records.symlink_to(real_records, target_is_directory=True)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=records / "server-host.json",
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert not any(real_records.iterdir())
    assert not os.path.lexists(apps / APP_NAME)


@requires_posix_uid
def test_require_absent_leaves_preexisting_directory_modes_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    apps = tmp_path / "Applications"
    apps.mkdir()
    os.chmod(apps, 0o755)
    records = tmp_path / "records"
    records.mkdir()
    os.chmod(records, 0o755)
    record = records / "server-host.json"

    # A pre-existing record parent that is not private is refused, not chmod'ed.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE(records.stat().st_mode) == 0o755
    assert not os.path.lexists(apps / APP_NAME)
    assert not os.path.lexists(record)

    # A private record parent is used as-is; neither pre-existing directory is
    # chmod'ed by the fresh path.
    os.chmod(records, 0o700)
    snapshot = server_host_install.install_server_host(
        archive,
        entry,
        bundle_path=apps / APP_NAME,
        ownership_path=record,
        runner=_FakeRunner(),
        require_absent=True,
        trusted_root=tmp_path,
    )
    assert snapshot.bundle_path == os.path.realpath(apps / APP_NAME)
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE(records.stat().st_mode) == 0o700
    assert record.is_file()


@requires_posix_uid
def test_require_absent_rechecks_a_bundle_that_arrives_while_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, source_bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    real_fsync_tree = server_host_install._fsync_tree

    def interleave(root: Path) -> None:
        real_fsync_tree(root)
        shutil.copytree(source_bundle, target)

    monkeypatch.setattr(server_host_install, "_fsync_tree", interleave)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.HOST_EXISTS
    assert target.is_dir()
    assert not os.path.lexists(record)


@requires_posix_uid
def test_require_absent_rechecks_a_journal_that_arrives_while_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    journal = server_host_install.upgrade_journal_path()
    real_fsync_tree = server_host_install._fsync_tree

    def interleave(root: Path) -> None:
        real_fsync_tree(root)
        journal.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(journal.parent, 0o700)
        journal.write_bytes(b"journal evidence that arrived during staging")

    monkeypatch.setattr(server_host_install, "_fsync_tree", interleave)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert journal.read_bytes() == b"journal evidence that arrived during staging"
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


def _upgrade_state() -> Path:
    state = server_host_install.upgrade_lock_path().parent
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    return state


@requires_posix_uid
def test_fresh_upgrade_lock_refuses_an_existing_nonprivate_file_without_chmod(
    home_dir: Path,
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o644)
    inode = lock.stat().st_ino
    before = lock.read_bytes()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        with server_host_install._upgrade_lock(preserve_existing=True):
            pass

    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644
    assert lock.stat().st_ino == inode
    assert lock.read_bytes() == before
    assert lock.stat().st_nlink == 1


@requires_posix_uid
def test_fresh_upgrade_lock_refuses_an_existing_hard_link_without_chmod(
    home_dir: Path,
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    alias = lock.parent / "lock-alias"
    os.link(lock, alias)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        with server_host_install._upgrade_lock(preserve_existing=True):
            pass

    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert lock.stat().st_nlink == 2
    assert alias.stat().st_nlink == 2
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(alias.stat().st_mode) == 0o600


@requires_posix_uid
def test_fresh_upgrade_lock_creates_exactly_0600_even_under_a_hostile_umask(
    home_dir: Path,
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    previous = os.umask(0o777)
    try:
        with server_host_install._upgrade_lock(preserve_existing=True):
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
            assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
            assert lock.stat().st_nlink == 1
    finally:
        os.umask(previous)


@requires_posix_uid
def test_fresh_upgrade_lock_refuses_acl_or_unavailable_acl_without_chmod(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    inode = lock.stat().st_ino
    lock_ino = inode

    def lock_not_private(fd: int) -> bool:
        return os.fstat(fd).st_ino != lock_ino

    monkeypatch.setattr(
        server_host_install, "is_private_descriptor_strict", lock_not_private
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        with server_host_install._upgrade_lock(preserve_existing=True):
            pass
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert lock.stat().st_ino == inode

    def no_acl(fd: int) -> bool:
        if os.fstat(fd).st_ino == lock_ino:
            raise OSError("the extended ACL API is unavailable")
        return True

    monkeypatch.setattr(
        server_host_install, "is_private_descriptor_strict", no_acl
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        with server_host_install._upgrade_lock(preserve_existing=True):
            pass
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert lock.stat().st_ino == inode


@requires_posix_uid
def test_fresh_upgrade_lock_busy_refusal_preserves_mode(home_dir: Path) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    holder = os.open(lock, os.O_RDWR)
    try:
        lock_exclusive(holder, blocking=True)
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            with server_host_install._upgrade_lock(preserve_existing=True):
                pass
        assert caught.value.code == server_host_install.TRANSACTION_BUSY
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    finally:
        unlock(holder)
        os.close(holder)


@requires_posix_uid
def test_require_absent_refuses_group_writable_owner_ancestry_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    apps = tmp_path / "Applications"
    apps.mkdir()
    os.chmod(apps, 0o775)
    records = tmp_path / "records"
    records.mkdir()
    os.chmod(records, 0o700)
    record = records / "server-host.json"

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert stat.S_IMODE(apps.stat().st_mode) == 0o775
    assert stat.S_IMODE(records.stat().st_mode) == 0o700
    assert not os.path.lexists(apps / APP_NAME)
    assert not os.path.lexists(record)


@requires_posix_uid
def test_require_absent_refuses_a_foreign_acl_or_missing_acl_api_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    apps = tmp_path / "Applications"
    apps.mkdir()
    os.chmod(apps, 0o755)
    records = tmp_path / "records"
    records.mkdir()
    os.chmod(records, 0o700)
    record = records / "server-host.json"
    apps_ino = apps.stat().st_ino

    def apps_grants(fd: int) -> bool:
        return os.fstat(fd).st_ino == apps_ino

    monkeypatch.setattr(
        server_host_install, "descriptor_acl_grants_another_account", apps_grants
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE(records.stat().st_mode) == 0o700
    assert not os.path.lexists(apps / APP_NAME)
    assert not os.path.lexists(record)

    def missing_acl(fd: int) -> bool:
        raise OSError("the extended ACL API is unavailable")

    records_ino = records.stat().st_ino

    def records_grant(fd: int) -> bool:
        return os.fstat(fd).st_ino == records_ino

    monkeypatch.setattr(
        server_host_install, "descriptor_acl_grants_another_account", records_grant
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE(records.stat().st_mode) == 0o700
    assert not os.path.lexists(apps / APP_NAME)
    assert not os.path.lexists(record)

    monkeypatch.setattr(
        server_host_install, "descriptor_acl_grants_another_account", missing_acl
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=apps / APP_NAME,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=tmp_path,
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE(records.stat().st_mode) == 0o700
    assert not os.path.lexists(apps / APP_NAME)
    assert not os.path.lexists(record)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_require_absent_refuses_a_real_macos_acl_grant_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    apps = tmp_path / "Applications"
    apps.mkdir()
    os.chmod(apps, 0o755)
    records = tmp_path / "records"
    records.mkdir()
    os.chmod(records, 0o700)
    record = records / "server-host.json"
    added = subprocess.run(
        ["chmod", "+a", "everyone allow write", str(apps)],
        capture_output=True,
        check=False,
    )
    if added.returncode != 0:
        pytest.skip(f"cannot add a macOS ACL here: {added.stderr!r}")
    try:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                bundle_path=apps / APP_NAME,
                ownership_path=record,
                runner=_FakeRunner(),
                require_absent=True,
                trusted_root=tmp_path,
            )
        assert caught.value.code == server_host_install.INSTALL_FAILED
        assert stat.S_IMODE(apps.stat().st_mode) == 0o755
        assert stat.S_IMODE(records.stat().st_mode) == 0o700
        assert not os.path.lexists(apps / APP_NAME)
        assert not os.path.lexists(record)
    finally:
        subprocess.run(
            ["chmod", "-a", "everyone allow write", str(apps)],
            capture_output=True,
            check=False,
        )


@requires_posix_uid
def test_fresh_upgrade_lock_refuses_a_name_swapped_after_open(
    home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    real_lock_exclusive = server_host_install.lock_exclusive

    def swap_then_lock(fd: int, *, blocking: bool) -> None:
        os.unlink(lock)
        lock.write_bytes(b"a different lock")
        os.chmod(lock, 0o600)
        real_lock_exclusive(fd, blocking=blocking)

    monkeypatch.setattr(server_host_install, "lock_exclusive", swap_then_lock)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        with server_host_install._upgrade_lock(preserve_existing=True):
            pass

    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert lock.read_bytes() == b"a different lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_fresh_upgrade_lock_refuses_a_real_macos_acl_grant_without_chmod(
    home_dir: Path,
) -> None:
    _upgrade_state()
    lock = server_host_install.upgrade_lock_path()
    lock.write_bytes(b"")
    os.chmod(lock, 0o600)
    added = subprocess.run(
        ["chmod", "+a", "everyone allow read", str(lock)],
        capture_output=True,
        check=False,
    )
    if added.returncode != 0:
        pytest.skip(f"cannot add a macOS ACL here: {added.stderr!r}")
    try:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            with server_host_install._upgrade_lock(preserve_existing=True):
                pass
        assert caught.value.code == server_host_install.TRANSACTION_FAILED
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    finally:
        subprocess.run(
            ["chmod", "-a", "everyone allow read", str(lock)],
            capture_output=True,
            check=False,
        )


@requires_posix_uid
def test_require_absent_creates_missing_private_components_under_umask_002(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real blocker: mkdir(parents=True) under umask 002 made .local/state and
    # Applications 0775, then the fresh checks rejected this run's own dirs. The
    # fresh path must create only the missing chain, descriptor-relative at 0700,
    # and leave the pre-existing home alone.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = server_host.default_bundle_path()
    record = server_host.default_ownership_path()
    assert not os.path.lexists(home_dir / ".local")
    assert not os.path.lexists(home_dir / "Applications")
    home_mode = stat.S_IMODE(home_dir.stat().st_mode)

    previous = os.umask(0o002)
    try:
        snapshot = server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
            require_absent=True,
            trusted_root=home_dir,
        )
    finally:
        os.umask(previous)

    assert snapshot.bundle_path == os.path.realpath(target)
    assert target.is_dir()
    assert record.is_file()
    assert (
        server_host.verify_owned_host(target, ownership_path=record, runner=_FakeRunner())
        == snapshot
    )
    for created in (
        home_dir / ".local",
        home_dir / ".local" / "state",
        home_dir / ".local" / "state" / "ciaobot",
        home_dir / "Applications",
    ):
        assert stat.S_IMODE(created.stat().st_mode) == 0o700
    assert stat.S_IMODE(home_dir.stat().st_mode) == home_mode


@requires_posix_uid
def test_require_absent_leaves_legitimate_0755_parents_unchanged(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    local = home_dir / ".local"
    local.mkdir()
    os.chmod(local, 0o755)
    state = local / "state"
    state.mkdir()
    os.chmod(state, 0o755)
    apps = home_dir / "Applications"
    apps.mkdir()
    os.chmod(apps, 0o755)
    target = server_host.default_bundle_path()
    record = server_host.default_ownership_path()

    snapshot = server_host_install.install_server_host(
        archive,
        entry,
        bundle_path=target,
        ownership_path=record,
        runner=_FakeRunner(),
        require_absent=True,
        trusted_root=home_dir,
    )

    assert snapshot.bundle_path == os.path.realpath(target)
    assert target.is_dir()
    assert record.is_file()
    assert stat.S_IMODE(local.stat().st_mode) == 0o755
    assert stat.S_IMODE(state.stat().st_mode) == 0o755
    assert stat.S_IMODE(apps.stat().st_mode) == 0o755
    assert stat.S_IMODE((local / "state" / "ciaobot").stat().st_mode) == 0o700


@requires_posix_uid
def test_require_absent_refuses_unsafe_parents_before_any_host_or_lock(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    local = home_dir / ".local"
    local.mkdir()
    os.chmod(local, 0o775)
    target = server_host.default_bundle_path()
    record = server_host.default_ownership_path()
    runner = _FakeRunner()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=runner,
            require_absent=True,
            trusted_root=home_dir,
        )

    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)
    assert not os.path.lexists(local / "state")
    assert not os.path.lexists(home_dir / "Applications")
    assert not os.path.lexists(server_host_install.upgrade_lock_path())
    assert runner.commands == []


@requires_posix_uid
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root can open a 0000 directory",
)
def test_require_absent_hostile_umask_fails_closed_with_evidence(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = server_host.default_bundle_path()
    record = server_host.default_ownership_path()
    local = home_dir / ".local"
    previous = os.umask(0o777)
    try:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                bundle_path=target,
                ownership_path=record,
                runner=_FakeRunner(),
                require_absent=True,
                trusted_root=home_dir,
            )
    finally:
        os.umask(previous)

    assert caught.value.code == server_host_install.INSTALL_FAILED
    # The created component is left as evidence, not repaired with a path chmod.
    assert stat.S_IMODE(local.stat().st_mode) == 0o000
    assert not os.path.lexists(local / "state")
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)
    os.chmod(local, 0o700)


def _spawn_with_spoofed_argv0(spoof: Path) -> subprocess.Popen[bytes]:
    """A harmless child whose argv[0] names a different existing file.

    It runs this interpreter, so the kernel executable must be the interpreter
    even though every user-space string the process can choose says ``spoof``.
    No service, signed host or real user file is involved.
    """
    return subprocess.Popen(
        [str(spoof), "-c", "import time; time.sleep(60)"],
        executable=sys.executable,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.mark.skipif(
    sys.platform != "darwin", reason="proc_pidpath is a macOS libproc call"
)
def test_kernel_executable_identity_ignores_a_spoofed_argv0(tmp_path: Path) -> None:
    # The process is really this interpreter, but its argv[0] is a different file
    # that exists. `proc_pidpath` must name the interpreter; the expected host is
    # compared as the same canonical path, and the spoofed name is refused. The
    # expected path is canonical: a venv's `sys.executable` is a symlink, which
    # the installer refuses as an expected path.
    expected = Path(os.path.realpath(sys.executable))
    spoof = tmp_path / "Ciaobot Server"
    spoof.write_text("", encoding="utf-8")
    child = _spawn_with_spoofed_argv0(spoof)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                proven = server_host_install.verify_process_executable(
                    child.pid, expected
                )
                break
            except server_host_install.ServerHostInstallError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert Path(proven) == Path(os.path.realpath(expected, strict=True))
        assert proven != str(spoof)
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.verify_process_executable(child.pid, spoof)
        assert caught.value.code == server_host_install.EXECUTABLE_MISMATCH
    finally:
        child.terminate()
        child.wait(timeout=10)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="proc_pidpath is a macOS libproc call"
)
def test_kernel_executable_identity_fails_closed_for_a_missing_pid() -> None:
    # A pid the kernel cannot name is not a pass: a pid outside the kernel's
    # range and a non-positive id may not authorize the host replacement.
    canonical = Path(os.path.realpath(sys.executable))
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(2**31 - 1, canonical)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(0, canonical)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


def test_executable_cli_prints_nothing_on_stdout_when_it_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The shell treats a zero exit as the proof and a nonzero as "not proven", so
    # a refusal must not put a path on stdout that another parser could mistake
    # for an answer. stderr stays one bounded line.
    status = server_host_install.main(
        [
            "executable",
            "--pid",
            "0",
            "--expect",
            str(tmp_path / "Ciaobot Server"),
        ]
    )
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("Error: ")


# --- executable identity, platform-independent --------------------------------
#
# These run on every platform: `sys.platform` is patched to "darwin" where the
# kernel path is read, and `ctypes.CDLL` is replaced by a fake libproc whose
# `proc_pidpath` writes a chosen payload into the caller's buffer and returns a
# chosen count. Nothing here loads the real libproc or needs a macOS host.


class _FakeProcPidpath:
    """A `proc_pidpath` stand-in: writes ``payload`` and returns ``written``."""

    def __init__(self, payload: bytes, written: int) -> None:
        self.payload = payload
        self.written = written
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, pid: int, buffer: Any, size: int) -> int:
        data = self.payload[:size]
        ctypes.memmove(buffer, data, len(data))
        return self.written


class _FakeLibproc:
    def __init__(self, proc_pidpath: _FakeProcPidpath) -> None:
        self.proc_pidpath = proc_pidpath


def _kernel_reports(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, written: int | None = None
) -> None:
    """Make the kernel-path read see a libproc that returns ``payload``."""
    proc = _FakeProcPidpath(payload, len(payload) if written is None else written)
    libproc = _FakeLibproc(proc)
    monkeypatch.setattr(
        server_host_install.ctypes, "CDLL", lambda *args, **kwargs: libproc
    )


def _canonical_file(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("", encoding="utf-8")
    return Path(os.path.realpath(path))


@pytest.mark.parametrize("platform_name", ["darwin", "linux", "win32"])
@pytest.mark.parametrize(
    "bad_pid", [2**32 + os.getpid(), 0, -1, True], ids=["overflow", "zero", "neg", "bool"]
)
def test_a_pid_outside_the_32_bit_range_is_refused_on_every_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform_name: str,
    bad_pid: Any,
) -> None:
    # The range check runs before the platform check, so a non-darwin host with a
    # hostile pid is refused for the pid, not reported as merely unsupported.
    monkeypatch.setattr(sys, "platform", platform_name)
    expected = _canonical_file(tmp_path, "Ciaobot Server")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(bad_pid, expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE
    assert str(caught.value) == "process id is not a positive 32-bit integer"


@pytest.mark.parametrize(
    ("payload_size", "written_size"),
    [
        (server_host_install._KERNEL_PATH_LIMIT - 1, server_host_install._KERNEL_PATH_LIMIT - 1),
        (server_host_install._KERNEL_PATH_LIMIT, server_host_install._KERNEL_PATH_LIMIT),
    ],
    ids=["limit_minus_one", "limit"],
)
def test_kernel_path_that_fills_the_buffer_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload_size: int,
    written_size: int,
) -> None:
    # proc_pidpath truncates into the fixed buffer; a path that reaches the limit
    # may be a prefix of the real one, so it is never read as complete.
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = _canonical_file(tmp_path, "Ciaobot Server")
    _kernel_reports(monkeypatch, b"/" * payload_size, written=written_size)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


@pytest.mark.parametrize(
    ("payload", "written"),
    [
        (b"/a\n/b", 5),
        (b"/a\x7f", 3),
        (b"/a\x00/b", 5),
        (b"/a/b", 2),
    ],
    ids=["newline", "del", "embedded_nul", "written_differs_from_length"],
)
def test_kernel_path_with_control_byte_or_embedded_nul_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
    written: int,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = _canonical_file(tmp_path, "Ciaobot Server")
    _kernel_reports(monkeypatch, payload, written=written)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


def test_kernel_path_zero_return_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = _canonical_file(tmp_path, "Ciaobot Server")
    _kernel_reports(monkeypatch, b"", written=0)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


def test_unloadable_libproc_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    expected = _canonical_file(tmp_path, "Ciaobot Server")

    def _missing(*args: Any, **kwargs: Any) -> Any:
        raise OSError("libproc is not installed")

    monkeypatch.setattr(server_host_install.ctypes, "CDLL", _missing)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


def test_non_darwin_platform_is_unsupported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    expected = _canonical_file(tmp_path, "Ciaobot Server")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.UNSUPPORTED_PLATFORM


@pytest.mark.parametrize("kind", ["directory", "missing"])
def test_directory_or_missing_expected_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    kernel_file = _canonical_file(tmp_path, "running")
    _kernel_reports(monkeypatch, str(kernel_file).encode())
    if kind == "directory":
        expected = Path(os.path.realpath(tmp_path / "bundle"))
        expected.mkdir()
    else:
        expected = Path(os.path.realpath(tmp_path)) / "missing"
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), expected)
    assert caught.value.code == server_host_install.EXECUTABLE_UNREADABLE


def test_different_file_at_a_different_path_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = _canonical_file(tmp_path, "Ciaobot Server")
    other = _canonical_file(tmp_path, "other")
    monkeypatch.setattr(
        server_host_install, "_kernel_executable_path", lambda pid: str(other)
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), host)
    assert caught.value.code == server_host_install.EXECUTABLE_MISMATCH


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_expected_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The executable itself must not be a symlink. A link that resolves to the
    # running file is still refused: the installer names the file it trusts.
    host = _canonical_file(tmp_path, "Ciaobot Server")
    link = tmp_path / "Ciaobot Server link"
    os.symlink(host, link)
    monkeypatch.setattr(
        server_host_install, "_kernel_executable_path", lambda pid: str(host)
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.verify_process_executable(os.getpid(), link)
    assert caught.value.code == server_host_install.EXECUTABLE_MISMATCH
    assert str(caught.value) == (
        "the installed host executable is a symlink, not the file it names"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_a_symlinked_parent_directory_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A relocated ~/Applications is a symlinked parent. The shell passes the
    # non-canonical $HOME path; the parent is canonicalised and the real file
    # matches, so the migration is not refused.
    real_dir = Path(os.path.realpath(tmp_path)) / "Applications"
    real_dir.mkdir()
    host = _canonical_file(real_dir, "Ciaobot Server")
    linked_dir = Path(os.path.realpath(tmp_path)) / "Applications link"
    os.symlink(real_dir, linked_dir)
    monkeypatch.setattr(
        server_host_install, "_kernel_executable_path", lambda pid: str(host)
    )
    proven = server_host_install.verify_process_executable(
        os.getpid(), linked_dir / "Ciaobot Server"
    )
    assert proven == str(host)


def test_executable_cli_prints_only_the_canonical_path_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = _canonical_file(tmp_path, "Ciaobot Server")
    monkeypatch.setattr(
        server_host_install, "_kernel_executable_path", lambda pid: str(host)
    )
    status = server_host_install.main(
        ["executable", "--pid", str(os.getpid()), "--expect", str(host)]
    )
    captured = capsys.readouterr()
    assert status == 0
    assert captured.out == f"{host}\n"


def test_executable_cli_refuses_a_mismatch_with_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = _canonical_file(tmp_path, "Ciaobot Server")
    other = _canonical_file(tmp_path, "other")
    monkeypatch.setattr(
        server_host_install, "_kernel_executable_path", lambda pid: str(other)
    )
    status = server_host_install.main(
        ["executable", "--pid", str(os.getpid()), "--expect", str(host)]
    )
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("Error: ")


def test_executable_cli_refuses_a_path_that_is_not_printable_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A non-UTF-8 path name is read as surrogate escapes; it cannot be written as
    # text. That is a clean unreadable refusal with nothing on stdout, not a raw
    # codec error.
    host = _canonical_file(tmp_path, "Ciaobot Server")
    monkeypatch.setattr(
        server_host_install,
        "verify_process_executable",
        lambda pid, expected: "/tmp/caf\udce9",
    )
    status = server_host_install.main(
        ["executable", "--pid", str(os.getpid()), "--expect", str(host)]
    )
    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == (
        "Error: the kernel executable path is not printable text\n"
    )
