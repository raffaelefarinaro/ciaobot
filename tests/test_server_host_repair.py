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
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao import server_host, server_host_install
from ciao.release_manifest import SERVER_HOST_FILENAME, server_host_artifact_entry
from ciao.server_host import (
    APP_NAME,
    BUNDLE_ID,
    ServerHostError,
    read_host_ownership,
    verify_owned_host,
    verify_superseded_host,
)
from tests.test_server_host_install import (
    _CDHASH,
    _CODESIGN_REL,
    _EXE_REL,
    _ICON_REL,
    _OTHER_TEAM,
    _PLIST_REL,
    _SIGNED_TEAM,
    _FakeRunner,
    _SignedRunner,
    _archive_of,
    _deferred,
    _fixture,
    _install_production_host,
    _make_signed_bundle,
    _make_superseded_bundle,
    _signed_fixture,
    _target,
    _write_superseded_record,
    requires_posix_uid,
)

# The stapled ticket a candidate built for the other Developer ID team carries.
# _SignedRunner answers every bundle with one team; this lets one run tell the
# installed host (team _SIGNED_TEAM) from a candidate (team _OTHER_TEAM).
_OTHER_TICKET = b"stapled-ticket-other"


class _TeamByTicketRunner(_SignedRunner):
    """A signed-host runner whose team follows each bundle's stapled ticket."""

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        ticket = self._bundle_of(argv[-1]) / "Contents" / "CodeResources"
        try:
            other = ticket.read_bytes() == _OTHER_TICKET
        except OSError:
            other = False
        self.team = _OTHER_TEAM if other else _SIGNED_TEAM
        return super().__call__(argv, **kwargs)


def _loose_superseded_host(tmp_path: Path, target: Path, record: Path) -> bytes:
    """A superseded host whose own record would prove it, but is group-readable.

    The loose mode is the only reason it is unprovable, so the repair is the one
    that has to replace it. Returns the record's bytes for later checks.
    """
    _make_superseded_bundle(target.parent, 3)
    _write_superseded_record(record, target, 3)
    os.chmod(record, 0o644)
    with pytest.raises(ServerHostError):
        verify_superseded_host(target, ownership_path=record, runner=_FakeRunner())
    return record.read_bytes()


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


@requires_posix_uid
def test_repair_replaces_an_unprovable_host_with_a_loose_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 1: a record that is group-readable is not provable, so the host is
    # replaced; reading that record for the restore must not refuse the repair.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    _loose_superseded_host(tmp_path, target, record)

    snapshot = server_host_install.repair_server_host(
        archive, entry, bundle_path=target, ownership_path=record, runner=_FakeRunner()
    )

    assert snapshot.host_revision == 4
    assert (target / _EXE_REL).read_bytes() == b"universal-host-binary"
    assert read_host_ownership(record) == snapshot
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_restores_a_loose_record_when_the_swap_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 1: the same setup with the swap failing. The original record bytes
    # (group-readable, so unprovable) come back exactly.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record_before = _loose_superseded_host(tmp_path, target, record)
    old_exe = (target / _EXE_REL).read_bytes()
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

    assert (target / _EXE_REL).read_bytes() == old_exe
    assert record.read_bytes() == record_before
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_refuses_an_unfinished_journal_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance criterion: an unfinished journal still refuses, nothing touched.
    # A legacy host upgraded to the signed one as a deferred transaction leaves
    # exactly such a journal at phase "installed".
    monkeypatch.setattr(sys, "platform", "darwin")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    _install_production_host(
        tmp_path, target=target, record=record, runner=_FakeRunner()
    )
    signed_archive, _bundle, signed_entry = _signed_fixture(tmp_path)
    _deferred(
        signed_archive,
        signed_entry,
        target=target,
        record=record,
        runner=_SignedRunner(),
    )
    journal = server_host_install.upgrade_journal_path()
    assert journal.exists()
    exe_before = (target / _EXE_REL).read_bytes()
    record_before = record.read_bytes()
    journal_before = journal.read_bytes()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            signed_archive,
            signed_entry,
            bundle_path=target,
            ownership_path=record,
            runner=_SignedRunner(),
        )
    assert caught.value.code in (
        server_host_install.TRANSACTION_CONFLICT,
        server_host_install.TRANSACTION_BUSY,
    )
    assert (target / _EXE_REL).read_bytes() == exe_before
    assert record.read_bytes() == record_before
    assert journal.read_bytes() == journal_before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
@requires_posix_uid
def test_repair_refuses_a_symlinked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 3: an existing host whose ownership record is a symlink is refused,
    # with the link, its target and the host untouched.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = _foreign_host(target)
    elsewhere = tmp_path / "elsewhere" / "record.json"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("not a record", encoding="utf-8")
    os.chmod(elsewhere, 0o600)
    record = tmp_path / "records" / "server-host.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.symlink_to(elsewhere)
    link_before = os.readlink(record)
    elsewhere_before = elsewhere.read_bytes()

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.TARGET_UNSAFE
    assert (target / "marker").read_bytes() == marker
    assert record.is_symlink()
    assert os.readlink(record) == link_before
    assert elsewhere.read_bytes() == elsewhere_before
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_replaces_a_policy_refused_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 4: a host signed by another Developer ID team classifies as
    # POLICY_REFUSED for this release. Repair replaces it with the verified host
    # of the release, and the new record verifies.
    monkeypatch.setattr(sys, "platform", "darwin")
    target = _target(tmp_path)
    record = tmp_path / "records" / "server-host.json"
    installed_archive, _bundle, installed_entry = _signed_fixture(tmp_path)
    server_host_install.install_server_host(
        installed_archive,
        installed_entry,
        bundle_path=target,
        ownership_path=record,
        runner=_SignedRunner(),
    )
    other_src = _make_signed_bundle(tmp_path / "other-src")
    (other_src / "Contents" / "CodeResources").write_bytes(_OTHER_TICKET)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_archive = _archive_of(other_src, other_dir / SERVER_HOST_FILENAME)
    other_entry = server_host_artifact_entry(
        other_archive, signing=server_host.DeveloperIDSigning(_OTHER_TEAM)
    )
    runner = _TeamByTicketRunner()
    inode = target.stat().st_ino

    # The fixture really is a policy refusal, not an unproven host: the legacy
    # installer refuses it and leaves it untouched.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            other_archive,
            other_entry,
            bundle_path=target,
            ownership_path=record,
            runner=runner,
        )
    assert caught.value.code == server_host_install.POLICY_REFUSED
    assert target.stat().st_ino == inode

    snapshot = server_host_install.repair_server_host(
        other_archive,
        other_entry,
        bundle_path=target,
        ownership_path=record,
        runner=runner,
    )

    assert snapshot.signing is not None
    assert snapshot.signing.team_id == _OTHER_TEAM
    assert read_host_ownership(record) == snapshot
    assert verify_owned_host(target, ownership_path=record, runner=runner) == snapshot
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]


@requires_posix_uid
def test_repair_restores_the_record_when_the_record_write_fails_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Finding 5: the record write replaces the record and then fails at the
    # directory fsync. The old record bytes and the old bundle must come back.
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = _foreign_host(target)
    record = tmp_path / "records" / "server-host.json"
    record_before = _foreign_record(record, tmp_path)
    real_fsync_dir = server_host_install._fsync_dir
    injected: list[Path] = []

    def fail_once_after_the_record_replace(path: Path) -> None:
        # The first directory flush of the record's own parent comes from the
        # record write itself, after its replace. Later calls are real.
        if not injected and Path(path) == record.parent:
            injected.append(Path(path))
            raise OSError(errno.EIO, "directory flush failed")
        real_fsync_dir(path)

    monkeypatch.setattr(
        server_host_install, "_fsync_dir", fail_once_after_the_record_replace
    )

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.repair_server_host(
            archive,
            entry,
            bundle_path=target,
            ownership_path=record,
            runner=_FakeRunner(),
        )
    assert caught.value.code == server_host_install.INSTALL_FAILED
    assert injected, "the late failure was never injected"

    assert (target / "marker").read_bytes() == marker
    assert record.read_bytes() == record_before
    assert sorted(item.name for item in target.parent.iterdir()) == [APP_NAME]
