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
from types import ModuleType
from typing import Any

import pytest

from ciao import server_host, server_host_install
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


# ── the digest and size gate ─────────────────────────────────────────────────


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
def test_a_matching_record_survives_a_failed_record_write(
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
        host_revision=2,
        host_protocol=2,
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
