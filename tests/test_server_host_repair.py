"""Native repair of the server host (#2491, part of #2485).

``repair_server_host`` makes the canonical host the manifest-verified host of the
release being repaired to. These tests drive it through the same fixtures and
fake probes as ``tests/test_server_host_install.py`` and pass the bundle and
record paths explicitly, so nothing touches the real HOME or ~/Applications.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao import server_host_install
from ciao.server_host import (
    APP_NAME,
    BUNDLE_ID,
    read_host_ownership,
    verify_owned_host,
)
from tests.test_server_host_install import (
    _CDHASH,
    _CODESIGN_REL,
    _EXE_REL,
    _ICON_REL,
    _PLIST_REL,
    _FakeRunner,
    _SignedRunner,
    _fixture,
    _install_production_host,
    _make_superseded_bundle,
    _signed_fixture,
    _target,
    _write_superseded_record,
    requires_posix_uid,
)


def _foreign_host(target: Path) -> bytes:
    """A bundle at ``target`` that no record proves is ours; returns its marker."""
    (target / "Contents").mkdir(parents=True)
    (target / "marker").write_text("someone else's host")
    return (target / "marker").read_bytes()


def _foreign_record(record: Path, tmp_path: Path) -> bytes:
    """A record that names a different bundle: it proves nothing about the target."""
    record.parent.mkdir(parents=True, exist_ok=True)
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
    return record.read_bytes()


@requires_posix_uid
def test_repair_keeps_a_current_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    installed = server_host_install.install_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )
    inode = target.stat().st_ino
    record_before = record.read_bytes()

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert snapshot == installed
    assert target.stat().st_ino == inode
    assert record.read_bytes() == record_before


@requires_posix_uid
def test_repair_installs_an_absent_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert verify_owned_host(
        target, ownership_path=record, runner=_FakeRunner()
    ) == snapshot


@requires_posix_uid
def test_repair_installs_over_an_orphan_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    old_bundle = _make_superseded_bundle(tmp_path / "scratch")
    _write_superseded_record(record, old_bundle)
    assert not target.exists()

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert target.is_dir()
    assert read_host_ownership(record) == snapshot


@requires_posix_uid
def test_repair_replaces_a_superseded_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent, 3)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle, 3
    )

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert snapshot.host_revision == 4
    assert (target / _EXE_REL).read_bytes() == b"universal-host-binary"
    assert read_host_ownership(record) == snapshot
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_replaces_a_host_it_cannot_prove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _foreign_host(target)
    record = tmp_path / "records" / "server-host.json"
    _foreign_record(record, tmp_path)

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert not (target / "marker").exists()
    assert verify_owned_host(
        target, ownership_path=record, runner=_FakeRunner()
    ) == snapshot
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_replaces_a_legacy_host_when_the_release_is_signed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    _install_production_host(
        tmp_path, target=target, record=record, runner=_FakeRunner()
    )
    signed_archive, _bundle, signed_entry = _signed_fixture(tmp_path)

    snapshot = server_host_install.repair_server_host(
        signed_archive,
        signed_entry,
        bundle_path=target,
        ownership_path=record,
        runner=_SignedRunner(),
    )

    assert snapshot.signing is not None
    assert not server_host_install.upgrade_journal_path().exists()


@requires_posix_uid
def test_repair_restores_an_unproven_host_and_its_record_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = _foreign_host(target)
    record = tmp_path / "records" / "server-host.json"
    record_before = _foreign_record(record, tmp_path)
    real_rename = os.rename

    def refuse_the_new_bundle(src: Any, dst: Any) -> None:
        # The move aside succeeds; the rename of the new bundle onto the target
        # fails. The rollback's own rename (the moved-aside copy back onto the
        # target) is not refused.
        if os.fspath(dst) == os.fspath(target) and os.path.basename(
            os.fspath(src)
        ) == APP_NAME:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(server_host_install.os, "rename", refuse_the_new_bundle)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED

    assert (target / "marker").read_bytes() == marker
    assert record.read_bytes() == record_before
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_restores_absence_of_a_record_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = _foreign_host(target)
    record = tmp_path / "records" / "server-host.json"
    assert not record.exists()
    real_rename = os.rename

    def refuse_the_new_bundle(src: Any, dst: Any) -> None:
        if os.fspath(dst) == os.fspath(target) and os.path.basename(
            os.fspath(src)
        ) == APP_NAME:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_rename(src, dst)

    monkeypatch.setattr(server_host_install.os, "rename", refuse_the_new_bundle)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED

    assert (target / "marker").read_bytes() == marker
    assert not record.exists()
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@requires_posix_uid
def test_repair_refuses_a_symlinked_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(bundle)
    link_before = os.readlink(target)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=tmp_path / "records" / "server-host.json",
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.TARGET_UNSAFE
    assert target.is_symlink()
    assert os.readlink(target) == link_before
    assert (bundle / _EXE_REL).read_bytes() == b"universal-host-binary"


@requires_posix_uid
def test_repair_refuses_a_tampered_archive_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    old_bundle = _make_superseded_bundle(target.parent, 3)
    record = _write_superseded_record(
        tmp_path / "records" / "server-host.json", old_bundle, 3
    )
    exe_before = (target / _EXE_REL).read_bytes()
    record_before = record.read_bytes()
    archive.write_bytes(archive.read_bytes() + b"tampered")

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH
    assert (target / _EXE_REL).read_bytes() == exe_before
    assert record.read_bytes() == record_before
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_install_server_host_still_refuses_an_unproven_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = _foreign_host(target)
    record = tmp_path / "records" / "server-host.json"
    _foreign_record(record, tmp_path)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.HOST_EXISTS
    assert (target / "marker").read_bytes() == marker
