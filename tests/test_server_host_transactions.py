"""The durable legacy-to-signed host upgrade transaction (#2330).

The production host install is one atomic step, so its before-image can live in
the staging directory the call deletes on return. Migrating an owned legacy host
to the explicitly signed one is not: the old app and the *exact* old record bytes
have to survive until the engine the shell installs has answered healthily, which
is after this module has returned. These tests drive that transaction with fake
native probes and isolated homes only: no launchd, no service, no keychain, no
TCC and no operator home. A spawned subprocess plays the killed installer, so the
durability seams are real process exits rather than simulated exceptions.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao import server_host, server_host_install
from ciao.release_manifest import SERVER_HOST_FILENAME
from tests.test_server_host_install import (
    APP_NAME,
    _ICON_REL,
    ROOT,
    _FakeRunner,
    _fixture,
    _make_superseded_bundle,
    _SignedRunner,
    _signed_fixture,
    _write_superseded_record,
)

requires_posix_uid = pytest.mark.skipif(
    sys.platform == "win32", reason="the host installer is a POSIX/macOS path"
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _bundle_path() -> Path:
    return server_host.default_bundle_path()


def _record_path() -> Path:
    return server_host.default_ownership_path()


def _journal_path() -> Path:
    return server_host_install.upgrade_journal_path()


def _tree(path: Path) -> dict[str, str]:
    """Every file under ``path``, by relative name, as its SHA-256."""
    return {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _install_legacy(
    tmp_path: Path, revision: int, *, runner: Any | None = None
) -> tuple[dict[str, str], bytes]:
    """Install a legacy host of ``revision`` at the default paths and snapshot it."""
    if revision == server_host.HOST_REVISION:
        archive, _bundle, entry = _fixture(tmp_path)
        server_host_install.install_server_host(
            archive,
            entry,
            runner=_FakeRunner() if runner is None else runner,
        )
    else:
        bundle = _bundle_path()
        bundle.parent.mkdir(parents=True, exist_ok=True)
        old = _make_superseded_bundle(bundle.parent, revision)
        _write_superseded_record(_record_path(), old, revision)
    return _tree(_bundle_path()), _record_path().read_bytes()


def _start_upgrade(tmp_path: Path, *, label: str = "signed") -> dict[str, Any]:
    """Begin a deferred legacy-to-signed upgrade owned by this test process."""
    archive, _bundle, entry = _signed_fixture(tmp_path, label=label)
    server_host_install.install_server_host(
        archive,
        entry,
        runner=_SignedRunner(),
        defer_commit=True,
        owner_pid=os.getpid(),
    )
    status = server_host_install.transaction_status()
    assert status["pending"] is True
    return status


def _finished_process_pid() -> int:
    """The pid of a process that has certainly exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


# ── the before-images ───────────────────────────────────────────────────────


@requires_posix_uid
def test_before_images_are_private_exact_and_retained_until_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old bundle and the exact old record bytes have to survive the whole
    # engine install, so they live outside the staging tree in a private,
    # nonce-derived directory beside the target, and only a commit releases them.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    # A record written by an older installer may be formatted differently. The
    # upgrade must keep *those* bytes, not a reserialization of them.
    document = json.loads(record_before.decode("utf-8"))
    exact = json.dumps(document, indent=4, sort_keys=False).encode("utf-8") + b"\n  "
    assert exact != record_before
    _record_path().write_bytes(exact)
    os.chmod(_record_path(), 0o600)

    status = _start_upgrade(tmp_path)

    journal = json.loads(_journal_path().read_text(encoding="utf-8"))
    assert journal["operation"] == status["operation"]
    assert journal["phase"] == "installed"
    assert journal["owner_pid"] == os.getpid()
    archive = tmp_path / "signed" / SERVER_HOST_FILENAME
    assert journal["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    # Owner-only journal in an owner-only state directory.
    assert stat.S_IMODE(_journal_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(_journal_path().parent.stat().st_mode) == 0o700
    assert _journal_path().stat().st_uid == os.getuid()

    backup = Path(journal["backup_dir"])
    assert backup.name == (
        f"{server_host_install.UPGRADE_BACKUP_PREFIX}{status['operation']}"
    )
    assert backup.parent == _bundle_path().parent
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    # The old app is retained whole, byte for byte, and the record exactly - the
    # odd indentation and trailing bytes included.
    assert _tree(backup / server_host_install._RETAINED_BUNDLE) == tree
    assert (backup / "record").read_bytes() == exact
    assert stat.S_IMODE((backup / "record").stat().st_mode) == 0o600
    # The signed host is installed and recorded meanwhile.
    installed = server_host.verify_owned_host(_bundle_path(), runner=_SignedRunner())
    assert installed.host_revision == server_host.SIGNED_HOST_REVISION

    server_host_install.commit_host_upgrade(
        operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
    )

    assert not _journal_path().exists()
    assert not backup.exists()
    assert server_host_install.transaction_status() == {"pending": False}
    assert (
        server_host.verify_owned_host(_bundle_path(), runner=_SignedRunner())
        == installed
    )


# ── commit ──────────────────────────────────────────────────────────────────


@requires_posix_uid
def test_commit_requires_nonce_and_verified_installed_signed_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    operation = status["operation"]
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    installed_record = _record_path().read_bytes()

    # A different operation, and an installer that is not the one that started
    # it, are both refused - even when that other process is alive.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation="0" * 32, owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=operation,
            owner_pid=_finished_process_pid(),
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    assert _journal_path().exists() and backup.exists()

    # A record that is not the one this upgrade wrote cannot earn a commit, and
    # a tampered bundle cannot earn one either - a healthy engine answer is not
    # evidence about the host's bytes.
    _record_path().write_bytes(b'{"schema": 1}\n')
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert _journal_path().exists() and backup.exists()

    _record_path().write_bytes(installed_record)
    icon = _bundle_path() / _ICON_REL
    icon.write_bytes(b"tampered after install")
    with pytest.raises(server_host.ServerHostError) as caught:
        server_host_install.commit_host_upgrade(
            operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code in {server_host.SIGNATURE_REJECTED, server_host.NOT_OWNED}
    assert caught.value.code not in server_host_install.WARN_AND_CONTINUE
    assert _journal_path().exists() and backup.exists()

    # The untouched pair commits, and only then releases the before-images.
    icon.write_bytes(b"indigo-icns-bytes")
    server_host_install.commit_host_upgrade(
        operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert not _journal_path().exists()
    assert not backup.exists()
    assert server_host_install.transaction_status() == {"pending": False}

    # A committed upgrade is terminal: a rollback request has nothing to act on
    # and must never put the replaced host back.
    server_host_install.rollback_host_upgrade(
        operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert (
        server_host.verify_owned_host(
            _bundle_path(), runner=_SignedRunner()
        ).host_revision
        == server_host.SIGNED_HOST_REVISION
    )


# ── rollback ────────────────────────────────────────────────────────────────


@requires_posix_uid
@pytest.mark.parametrize("revision", [2, 3, server_host.HOST_REVISION])
def test_rollback_restores_exact_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: int
) -> None:
    # Every legacy revision this engine may own, back exactly as it was: the same
    # app bytes, the same record bytes, and a pair that still verifies.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, revision)
    status = _start_upgrade(tmp_path, label=f"signed-{revision}")
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )

    server_host_install.rollback_host_upgrade(
        operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
    )

    assert _tree(_bundle_path()) == tree
    assert _record_path().read_bytes() == record_before
    assert not _journal_path().exists()
    assert not backup.exists()
    if revision == server_host.HOST_REVISION:
        restored = server_host.verify_owned_host(_bundle_path(), runner=_SignedRunner())
    else:
        restored = server_host.verify_superseded_host(
            _bundle_path(), runner=_SignedRunner()
        )
    assert restored.host_revision == revision
    # And nothing of the failed upgrade survives beside the host.
    assert not [
        item
        for item in _bundle_path().parent.iterdir()
        if item.name.startswith(server_host_install.STAGING_PREFIX)
    ]


# ── durability: a killed installer, recovered by a fresh process ────────────

_KILLING_CHILD = """
import os
import sys

sys.platform = "darwin"

from pathlib import Path

from ciao import server_host_install

from tests.test_server_host_install import _SignedRunner, _signed_fixture

trigger = sys.argv[1]
when = sys.argv[2]
root = Path(sys.argv[3])
archive, _bundle, entry = _signed_fixture(root, label="child-signed")

real_journal = server_host_install._write_upgrade_journal
real_record = server_host_install._write_ownership_record


def killing_write(journal):
    if journal.phase == trigger and when == "before":
        os._exit(0)
    real_journal(journal)
    if journal.phase == trigger and when == "after":
        os._exit(0)


def killing_record(snapshot, path):
    # The new record is written between the `old_moved` phase and the candidate
    # rename; a kill there leaves a canonical record for a bundle that is not in
    # place yet, which the next process has to be able to settle.
    real_record(snapshot, path)
    current = server_host_install._read_upgrade_journal()
    if trigger == "record" and current is not None and current.phase == "old_moved":
        os._exit(0)


server_host_install._write_upgrade_journal = killing_write
server_host_install._write_ownership_record = killing_record
try:
    server_host_install.install_server_host(
        archive,
        entry,
        runner=_SignedRunner(),
        defer_commit=True,
        owner_pid=os.getpid(),
    )
except BaseException:
    os._exit(70)
os._exit(71)
"""

_CLEANUP_KILL_CHILD = """
import os
import sys

sys.platform = "darwin"

from ciao import server_host_install

from tests.test_server_host_install import _SignedRunner

operation = sys.argv[1]
owner_pid = int(sys.argv[2])
journal_path = os.fspath(server_host_install.upgrade_journal_path())

real_unlink = os.unlink
journal_name = os.path.basename(journal_path)


def killing_unlink(path, *args, **kwargs):
    # The transaction's before-images are already gone at this point; the kill lands
    # just before the journal is unlinked, which is the state a real interrupted
    # cleanup leaves behind. Matching the journal's own name (it is unlinked through
    # the state directory's descriptor) leaves the before-image drain untouched.
    if os.path.basename(os.fspath(path)) == journal_name:
        os._exit(0)
    return real_unlink(path, *args, **kwargs)


os.unlink = killing_unlink
try:
    server_host_install.commit_host_upgrade(
        operation=operation, owner_pid=owner_pid, runner=_SignedRunner()
    )
except BaseException:
    os._exit(70)
os._exit(71)
"""


_TERMINAL_KILL_CHILD = """
import os
import sys

sys.platform = "darwin"

from ciao import server_host_install

from tests.test_server_host_install import _SignedRunner

command = sys.argv[1]
operation = sys.argv[2]
owner_pid = int(sys.argv[3])
phase = "committed" if command == "commit" else "rolled_back"
runner = _SignedRunner()

real_write = server_host_install._write_upgrade_journal


def killing_write(journal):
    real_write(journal)
    if journal.phase == phase:
        os._exit(0)


server_host_install._write_upgrade_journal = killing_write
try:
    if command == "commit":
        server_host_install.commit_host_upgrade(
            operation=operation, owner_pid=owner_pid, runner=runner
        )
    else:
        server_host_install.rollback_host_upgrade(
            operation=operation, owner_pid=owner_pid, runner=runner
        )
except BaseException:
    os._exit(70)
os._exit(71)
"""


_LOCK_HOLDER = """
import sys
import time

sys.platform = "darwin"

from pathlib import Path

from ciao import server_host_install

marker = Path(sys.argv[1])
with server_host_install._upgrade_lock():
    marker.write_text("held", encoding="utf-8")
    while True:
        time.sleep(0.05)
"""


#: (the phase whose journal write is the seam, whether the kill lands before or
#: after that write, the phase a fresh process then reads from the journal).
SEAMS = [
    ("prepared", "after", "prepared"),
    ("old_moved", "before", "prepared"),
    # The new record is on disk and the candidate has not been renamed in yet.
    ("record", "after", "old_moved"),
    ("installed", "before", "old_moved"),
    ("installed", "after", "installed"),
]


def _kill_at(tmp_path: Path, trigger: str, when: str) -> None:
    child = subprocess.run(
        [sys.executable, "-c", _KILLING_CHILD, trigger, when, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr


@requires_posix_uid
@pytest.mark.parametrize("trigger, when, recorded", SEAMS)
def test_kill_recovery_at_each_durable_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    when: str,
    recorded: str,
) -> None:
    # Kill the installer right at a durable seam, then let a *fresh* process take
    # over. What it may do is restore the known old pair from the journal and the
    # retained material - never adopt, commit or delete anything it cannot prove.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    _kill_at(tmp_path, trigger, when)

    journal = server_host_install._read_upgrade_journal()
    assert journal is not None
    assert journal.phase == recorded
    assert journal.owner_pid != os.getpid()
    status = server_host_install.transaction_status()
    assert status["pending"] is True
    assert status["owner_alive"] is False
    assert status["operation"] == journal.operation

    # A fresh process may not commit what it did not start.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=journal.operation, owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    assert _journal_path().exists()

    # It may roll the abandoned operation back, and that restores the exact pair.
    server_host_install.rollback_host_upgrade(
        operation=journal.operation, owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert _tree(_bundle_path()) == tree
    assert _record_path().read_bytes() == record_before
    assert not _journal_path().exists()
    assert not journal.backup_dir.exists()
    assert not server_host_install.transaction_status()["pending"]


@requires_posix_uid
def test_install_refuses_an_abandoned_operation_until_it_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `install` never rolls an earlier operation back itself: quiescing the
    # candidate's service is the shell's job, and a rollback driven from here could
    # revert the identity under a process that is still running. The abandoned
    # operation is a refusal, and only an explicit rollback (then a new install)
    # moves it forward.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    _kill_at(tmp_path, "installed", "after")
    abandoned = server_host_install._read_upgrade_journal()
    assert abandoned is not None
    installed = _tree(_bundle_path())

    archive, _bundle, entry = _signed_fixture(tmp_path, label="retrying-signed")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            runner=_SignedRunner(),
            defer_commit=True,
            owner_pid=os.getpid(),
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    assert "did not finish" in str(caught.value)
    # Nothing was touched by the refused install.
    assert _tree(_bundle_path()) == installed
    assert server_host_install._read_upgrade_journal() == abandoned

    server_host_install.rollback_host_upgrade(
        operation=abandoned.operation, owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert _tree(_bundle_path()) == tree
    assert _record_path().read_bytes() == record_before

    status = _start_upgrade(tmp_path, label="retried-signed")
    assert status["operation"] != abandoned.operation
    assert status["owner_pid"] == os.getpid()


@requires_posix_uid
def test_a_status_race_where_the_owner_dies_still_refuses_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shell checked the status, saw a live owner, and the owner died before the
    # install call. The install must not adopt that fact as permission to roll back
    # a candidate the dead process may have left running.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    archive, _bundle, entry = _signed_fixture(tmp_path, label="raced")
    try:
        server_host_install.install_server_host(
            archive, entry, runner=_SignedRunner(), defer_commit=True, owner_pid=live.pid
        )
        operation = server_host_install.transaction_status()["operation"]
        assert server_host_install.transaction_status()["owner_alive"] is True
    finally:
        live.terminate()
        live.wait()

    # The owner is gone now; the journal is still unfinished, and installing over
    # it is refused exactly as it was while the owner was alive.
    for _ in range(2):
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                runner=_SignedRunner(),
                defer_commit=True,
                owner_pid=os.getpid(),
            )
        assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    assert server_host_install._read_upgrade_journal() is not None
    assert server_host_install._read_upgrade_journal().operation == operation


@requires_posix_uid
def test_the_status_race_between_two_installers_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dangerous interleaving: this installer observes no pending journal, a
    # second installer creates one and dies before this one calls `install`. There
    # is no rollback here - the candidate that second run started may be live - and
    # nothing at the target is touched.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    assert server_host_install.transaction_status() == {"pending": False}
    quiet = _tree(_bundle_path())

    # Another installer, in its own process: it begins the upgrade and exits at
    # once, so its pid is gone and its journal is abandoned.
    _kill_at(tmp_path, "installed", "after")
    other = server_host_install._read_upgrade_journal()
    assert other is not None
    assert other.owner_pid != os.getpid()
    installed = _tree(_bundle_path())

    archive, _bundle, entry = _signed_fixture(tmp_path, label="raced-signed")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.install_server_host(
            archive,
            entry,
            runner=_SignedRunner(),
            defer_commit=True,
            owner_pid=os.getpid(),
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    # No rollback: the other operation is untouched and the candidate is still what
    # is installed. Nothing at the target changed at all.
    after = server_host_install._read_upgrade_journal()
    assert after is not None
    assert after.operation == other.operation
    assert after.phase == other.phase
    assert _tree(_bundle_path()) == installed
    assert installed != quiet
    assert _record_path().read_bytes() != record_before


@pytest.mark.parametrize("target", ["retained", "canonical"])
@pytest.mark.parametrize("substitute", ["different", "byte-identical"])
@requires_posix_uid
def test_a_swap_between_verification_and_the_move_deletes_neither_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, substitute: str
) -> None:
    # Verification says "this is the retained host" (or "this is the candidate"), and
    # the directory is replaced before the rename. The **verified image is moved
    # aside**, not destroyed, so the test can prove every object survives: the
    # original, the substitute, the other transaction image and the journal. With a
    # byte-identical substitute, content verification cannot be what refuses - the
    # recorded inode must be.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    watched = (
        backup / server_host_install._RETAINED_BUNDLE
        if target == "retained"
        else _bundle_path()
    )
    other = (
        _bundle_path()
        if target == "retained"
        else backup / server_host_install._RETAINED_BUNDLE
    )
    source = tmp_path / f"substitute-source-{target}-{substitute}"
    server_host_install.shutil.copytree(watched, source, symlinks=True)
    if substitute == "different":
        (source / _ICON_REL).write_bytes(b"someone else's icon")
    aside = tmp_path / f"verified-image-{target}"
    real_matches = server_host_install._matches_snapshot
    swapped: list[int] = []

    def swapping_matches(bundle: Path, snapshot: Any, runner: Any) -> bool:
        matched = real_matches(bundle, snapshot, runner)
        if not swapped and Path(bundle) == watched and matched:
            swapped.append(1)
            # Preserve the verified image: move it aside, never delete it.
            os.rename(watched, aside)
            server_host_install.shutil.copytree(source, watched, symlinks=True)
        return matched

    monkeypatch.setattr(server_host_install, "_matches_snapshot", swapping_matches)

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert swapped, "the test never swapped the watched image"
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    # Every object survives: the verified original, the substitute at the watched
    # name, the other transaction image and the journal.
    assert aside.is_dir()
    assert (aside / _ICON_REL).read_bytes() == b"indigo-icns-bytes"
    assert watched.is_dir()
    if substitute == "different":
        assert (watched / _ICON_REL).read_bytes() == b"someone else's icon"
    else:
        assert (watched / _ICON_REL).read_bytes() == b"indigo-icns-bytes"
    assert watched.stat().st_ino != aside.stat().st_ino
    assert other.is_dir()
    assert _journal_path().exists()
    assert server_host_install.transaction_status()["pending"] is True


@requires_posix_uid
def test_unsafe_terminal_cleanup_targets_are_refused_before_anything_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A terminal journal only deletes material it can prove. A before-image path
    # replaced by a symlink, or by a fresh private directory this upgrade did not
    # create, is refused - and the substituted object survives to be inspected.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("not ours", encoding="utf-8")

    # (a) the before-images replaced by a symlink to another directory
    server_host_install.shutil.rmtree(backup)
    backup.symlink_to(outside)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert backup.is_symlink()
    assert (outside / "keep").read_text(encoding="utf-8") == "not ours"

    # (b) the before-images replaced by a fresh private directory with another inode
    backup.unlink()
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    (backup / "keep").write_text("not ours either", encoding="utf-8")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert (backup / "keep").read_text(encoding="utf-8") == "not ours either"

    # (c) the before-images replaced by a *copy* with the same contents: every
    # content check would pass, so only the recorded device/inode can tell that this
    # is not the directory the upgrade created.
    server_host_install.shutil.rmtree(backup)
    backup.mkdir(mode=0o700)
    os.chmod(backup, 0o700)
    (backup / "record").write_bytes(
        (outside / "keep").read_bytes() * 0 + b'{"schema": 1}\n'
    )
    os.chmod(backup / "record", 0o600)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert (backup / "record").exists()

    # (d) the journal itself replaced by a symlink
    server_host_install.shutil.rmtree(backup)
    backup.mkdir(mode=0o700)
    journal_text = _journal_path().read_text(encoding="utf-8")
    _journal_path().unlink()
    _journal_path().symlink_to(outside / "keep")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.transaction_status()
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    _journal_path().unlink()
    _journal_path().write_text(journal_text, encoding="utf-8")
    os.chmod(_journal_path(), 0o600)
    assert (outside / "keep").read_text(encoding="utf-8") == "not ours"


@requires_posix_uid
def test_the_journal_unlink_is_bound_to_the_journal_that_was_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cleanup runs against the file this command settled. A journal substituted
    # after that read/write is not unlinked, because deleting it would erase another
    # operation's only record.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    journal = server_host_install._read_upgrade_journal()
    assert journal is not None and journal.observed is not None
    observed = journal.observed

    # Another writer replaces the journal with a different file (a new inode).
    document = json.loads(_journal_path().read_text(encoding="utf-8"))
    server_host_install._write_private_bytes(
        _journal_path(),
        (json.dumps({**document, "phase": "rolled_back"}, indent=2) + "\n").encode(),
        what="a substituted journal",
    )

    problem = server_host_install._finish_journal(journal, journal_inode=observed)
    assert problem is not None
    assert "not the one this command settled" in problem
    assert _journal_path().exists()


@requires_posix_uid
def test_a_real_interrupted_terminal_cleanup_is_settled_by_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real commit is killed after the before-images are removed and before the
    # journal is unlinked - the exact state an interrupted cleanup leaves. The next
    # run finishes that journal and leaves the committed pair alone.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    committed = _tree(_bundle_path())
    committed_record = _record_path().read_bytes()

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CLEANUP_KILL_CHILD,
            status["operation"],
            str(os.getpid()),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    # The real interrupted state: committed journal, before-images gone.
    journal = json.loads(_journal_path().read_text(encoding="utf-8"))
    assert journal["phase"] == "committed"
    assert not backup.exists()
    assert server_host_install.transaction_status() == {"pending": False}

    # The next run settles it, and the committed pair is untouched.
    archive, _bundle, entry = _signed_fixture(tmp_path, label="after-interruption")
    server_host_install.install_server_host(
        archive, entry, runner=_SignedRunner(), defer_commit=True, owner_pid=os.getpid()
    )
    assert not _journal_path().exists()
    assert _tree(_bundle_path()) == committed
    assert _record_path().read_bytes() == committed_record
    assert (
        server_host.verify_owned_host(_bundle_path(), runner=_SignedRunner()).host_revision
        == server_host.SIGNED_HOST_REVISION
    )


@requires_posix_uid
def test_a_partially_drained_terminal_cleanup_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cleanup that removed the retained record and then failed leaves a directory
    # and a terminal journal. A later run must finish it: the entries that are left
    # are validated, the ones already gone are not required again.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _TERMINAL_KILL_CHILD,
            "commit",
            status["operation"],
            str(os.getpid()),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(_journal_path().read_text(encoding="utf-8"))["phase"] == (
        "committed"
    )
    committed = _tree(_bundle_path())
    committed_record = _record_path().read_bytes()

    # The partial state: the retained record is gone, the directory and journal stay.
    (backup / "record").unlink()
    assert backup.is_dir()

    server_host_install.commit_host_upgrade(
        operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
    )

    assert not _journal_path().exists()
    assert not backup.exists()
    # The committed pair was not altered by the cleanup.
    assert _tree(_bundle_path()) == committed
    assert _record_path().read_bytes() == committed_record


@requires_posix_uid
def test_a_byte_identical_substitute_of_the_backup_is_refused_in_terminal_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A settled transaction whose before-images were replaced by a *byte-identical*
    # copy at another inode. Content checks cannot tell the difference; the recorded
    # device/inode can. Cleanup refuses and neither the substitute, the original nor
    # the journal is deleted.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    # A real terminal transaction, with its real before-images still in place.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _TERMINAL_KILL_CHILD,
            "commit",
            status["operation"],
            str(os.getpid()),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(_journal_path().read_text(encoding="utf-8"))["phase"] == (
        "committed"
    )
    assert backup.is_dir()
    committed = _tree(_bundle_path())

    # Keep the original where it is and put a byte-identical copy at its name, at a
    # different inode.
    duplicate = tmp_path / "before-images-copy"
    server_host_install.shutil.copytree(backup, duplicate, symlinks=True)
    original = tmp_path / "before-images-original"
    os.rename(backup, original)
    server_host_install.shutil.copytree(duplicate, backup, symlinks=True)
    assert (backup / "record").read_bytes() == (duplicate / "record").read_bytes()
    assert (original / "record").read_bytes() == (duplicate / "record").read_bytes()
    assert backup.stat().st_ino != original.stat().st_ino

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code in (
        server_host_install.TRANSACTION_CLEANUP,
        server_host_install.TRANSACTION_FAILED,
    )
    # Not deleted: the substitute, the original and the journal all remain.
    assert (backup / "record").exists()
    assert (original / "record").exists()
    assert _journal_path().exists()
    assert _tree(_bundle_path()) == committed


@requires_posix_uid
def test_the_upgrade_lock_is_held_by_a_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock is an advisory lock on a real file, so a second process holding it
    # is a real conflict - not a flag this module sets for itself. The refusal
    # leaves the target, the record and the journal exactly as they were.
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    marker = tmp_path / "lock-held"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(marker)],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    try:
        import time as _time

        for _ in range(200):
            if marker.exists():
                break
            _time.sleep(0.05)
        assert marker.exists(), "the lock holder never started"

        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.transaction_status()
        assert caught.value.code == server_host_install.TRANSACTION_BUSY

        archive, _bundle, entry = _signed_fixture(tmp_path, label="locked")
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                runner=_SignedRunner(),
                defer_commit=True,
                owner_pid=os.getpid(),
            )
        assert caught.value.code == server_host_install.TRANSACTION_BUSY
        assert _tree(_bundle_path()) == tree
        assert _record_path().read_bytes() == record_before
        assert server_host_install._read_upgrade_journal() is None
    finally:
        holder.terminate()
        holder.wait()


# ── untrusted state ─────────────────────────────────────────────────────────


@requires_posix_uid
def test_corrupt_foreign_or_unsafe_journal_never_mutates_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    journal_text = _journal_path().read_text(encoding="utf-8")
    document = json.loads(journal_text)
    backup = Path(document["backup_dir"])
    installed = _tree(_bundle_path())

    def refuse(document_text: str, code: str) -> None:
        _journal_path().write_text(document_text, encoding="utf-8")
        os.chmod(_journal_path(), 0o600)
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.rollback_host_upgrade(
                operation=document["operation"],
                owner_pid=os.getpid(),
                runner=_SignedRunner(),
            )
        assert caught.value.code == code
        # Nothing at the target was mutated, and the material is still there.
        assert _tree(_bundle_path()) == installed
        assert backup.exists()

    # A duplicate key anywhere is contradictory state, not a journal.
    refuse(
        journal_text.replace(
            '"phase": "installed"', '"phase": "installed", "phase": "installed"'
        ),
        server_host_install.TRANSACTION_FAILED,
    )
    # A before-image path this installer would never have created.
    refuse(
        json.dumps({**document, "backup_dir": str(tmp_path / "elsewhere")}, indent=2),
        server_host_install.TRANSACTION_FAILED,
    )
    # A nonce that does not name the before-image directory.
    refuse(
        json.dumps({**document, "operation": "f" * 32}, indent=2),
        server_host_install.TRANSACTION_FAILED,
    )
    # An unknown phase.
    refuse(
        json.dumps({**document, "phase": "almost"}, indent=2),
        server_host_install.TRANSACTION_FAILED,
    )
    # A group/other readable journal is not trusted state.
    _journal_path().write_text(journal_text, encoding="utf-8")
    os.chmod(_journal_path(), 0o644)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.transaction_status()
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    os.chmod(_journal_path(), 0o600)

    # A symlinked retained record is refused rather than followed.
    retained = backup / "record"
    retained_bytes = retained.read_bytes()
    retained.unlink()
    retained.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"],
            owner_pid=os.getpid(),
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    retained.unlink()
    retained.write_bytes(retained_bytes)
    os.chmod(retained, 0o600)

    # A journal that names another target is not this Mac's operation.
    foreign_bundle = tmp_path / "Applications" / APP_NAME
    elsewhere = {
        **document,
        "bundle_path": str(foreign_bundle),
        "record_path": str(tmp_path / "state" / "server-host.json"),
        "backup_dir": str(
            foreign_bundle.parent
            / f"{server_host_install.UPGRADE_BACKUP_PREFIX}{document['operation']}"
        ),
    }
    _journal_path().write_text(json.dumps(elsewhere, indent=2), encoding="utf-8")
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=document["operation"],
            owner_pid=os.getpid(),
            runner=_SignedRunner(),
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    _journal_path().write_text(journal_text, encoding="utf-8")

    # With the journal repaired, the rollback works again and restores the pair.
    server_host_install.rollback_host_upgrade(
        operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert _tree(_bundle_path()) == tree
    assert _record_path().read_bytes() == record_before


@requires_posix_uid
def test_an_unrelated_target_is_never_deleted_by_a_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    server_host_install.shutil.rmtree(_bundle_path())
    foreign = _bundle_path()
    (foreign / "Contents").mkdir(parents=True)
    (foreign / "Contents" / "marker").write_text("someone else's host", encoding="utf-8")

    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert str(backup) in str(caught.value)
    assert (foreign / "Contents" / "marker").read_text(
        encoding="utf-8"
    ) == "someone else's host"
    assert backup.exists() and _journal_path().exists()


# ── one operation, one owner ────────────────────────────────────────────────


@requires_posix_uid
def test_second_installer_cannot_mutate_unfinished_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    tree, record_before = _install_legacy(tmp_path, server_host.HOST_REVISION)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        archive, _bundle, entry = _signed_fixture(tmp_path, label="owned")
        server_host_install.install_server_host(
            archive, entry, runner=_SignedRunner(), defer_commit=True, owner_pid=live.pid
        )
        operation = server_host_install.transaction_status()["operation"]
        installed = _tree(_bundle_path())

        # A different live installer neither commits nor rolls it back.
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.commit_host_upgrade(
                operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
            )
        assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.rollback_host_upgrade(
                operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
            )
        assert caught.value.code == server_host_install.TRANSACTION_BUSY

        # A same-host no-op does not bypass the pending transaction either.
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            server_host_install.install_server_host(
                archive,
                entry,
                runner=_SignedRunner(),
                defer_commit=True,
                owner_pid=os.getpid(),
            )
        assert caught.value.code == server_host_install.TRANSACTION_BUSY
        assert _tree(_bundle_path()) == installed
    finally:
        live.terminate()
        live.wait()

    # With the owner gone, a new installer may roll it back - and only that.
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.commit_host_upgrade(
            operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
    server_host_install.rollback_host_upgrade(
        operation=operation, owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert _tree(_bundle_path()) == tree
    assert _record_path().read_bytes() == record_before


@requires_posix_uid
def test_failed_rollback_retains_recoverable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A filesystem failure during the restore must not erase the only way back:
    # the durable before-images and the journal stay, and the error names them.
    monkeypatch.setattr(sys, "platform", "darwin")
    _install_legacy(tmp_path, server_host.HOST_REVISION)
    status = _start_upgrade(tmp_path)
    backup = Path(
        json.loads(_journal_path().read_text(encoding="utf-8"))["backup_dir"]
    )
    real_write = server_host_install._write_private_bytes

    def refuse_the_record(path: Path, data: bytes, *, what: str) -> None:
        if Path(path).name == "server-host.json" and b"host_revision" in data:
            raise server_host_install.ServerHostInstallError(
                "the previous host ownership record could not be written: "
                "No space left on device",
                code=server_host_install.TRANSACTION_FAILED,
            )
        real_write(path, data, what=what)

    monkeypatch.setattr(server_host_install, "_write_private_bytes", refuse_the_record)
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        server_host_install.rollback_host_upgrade(
            operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
        )
    assert caught.value.code == server_host_install.TRANSACTION_FAILED
    assert "No space left on device" in str(caught.value)
    assert backup.exists() and _journal_path().exists()

    # With the failure gone the same rollback finishes from the same material.
    monkeypatch.setattr(server_host_install, "_write_private_bytes", real_write)
    server_host_install.rollback_host_upgrade(
        operation=status["operation"], owner_pid=os.getpid(), runner=_SignedRunner()
    )
    assert not _journal_path().exists()
    assert not backup.exists()
    assert server_host.verify_owned_host(_bundle_path(), runner=_SignedRunner())


@requires_posix_uid
def test_status_is_exactly_pending_false_without_a_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert server_host_install.transaction_status() == {"pending": False}
