"""The native install classifier: one read-only state answer, no caller.

The classifier is an independent primitive in this issue. It does not gate the
legacy bootstrap, ``ciao setup``, the CLI or any route, and its ``fresh`` answer
must never be treated as permission to activate: #2340 owns the current-run
verification that a native entry point needs, on both the setup-child and the
installer-parent sides. These tests pin its evidence, including the host-upgrade
journal and the ownership/liveness probe of the permanent sibling lock, and the
bounded no-follow read of ``.env`` and the receipt: oversize, symlinked,
hard-linked, undecodable, replaced and unreadable files are unknown, and a FIFO
where either belongs must finish (not block) in a child process.

Every filesystem root and every service probe is faked. Nothing here reads the
operator's home, launchd, a scheduled task or a live service.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import (
    install_discovery,
    install_receipt,
    macos_service,
    server_host_install,
    windows_service,
)
from ciao.os_support.files import open_fd
from ciao.os_support.locks import lock_exclusive, unlock

#: The durable host-upgrade lock is a Darwin fact: the installer writes it
#: ``0600`` and the probe judges that mode before it takes a non-blocking
#: ``flock``. Windows has neither bit, so a case that models the lock's mode or
#: its lock state is a POSIX assertion and is not made there.
POSIX_MODES = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file modes and flock on the host-upgrade lock"
)
#: Creating a symlink on Windows needs a privilege the default account does not
#: have, so a test whose subject is link semantics is not asserted there.
SYMLINK_ONLY = pytest.mark.skipif(os.name != "posix", reason="symlink semantics")


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    """Isolate the classifier and answer a fresh, quiet macOS machine."""
    home = _home(monkeypatch, tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tool_dir = tmp_path / "uv-tools"
    bin_dir = tmp_path / "bin"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(install_discovery, "_platform", lambda: "darwin")
    monkeypatch.setattr(macos_service, "live_launch_agents_dir", lambda: agents)
    monkeypatch.setattr(macos_service, "server_job_absent", lambda **_kwargs: True)
    monkeypatch.setattr(
        macos_service, "launchd_override_disabled", lambda **_kwargs: False
    )
    return cwd, tool_dir, bin_dir


def _windows_world(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, Path, Path]:
    """Isolate the classifier and answer a fresh, quiet Windows machine."""
    _home(monkeypatch, tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    tool_dir = tmp_path / "uv-tools"
    bin_dir = tmp_path / "bin"
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    monkeypatch.setattr(install_discovery, "_platform", lambda: "win32")
    monkeypatch.setattr(windows_service, "live_task_dir", lambda: task_dir)
    monkeypatch.setattr(windows_service, "engine_task_registered", lambda: False)
    return cwd, tool_dir, bin_dir, task_dir


def _classify(cwd: Path, tool_dir: Path, bin_dir: Path, *, environ=None, suggestion=None):
    return install_discovery.classify_native_install(
        environ={} if environ is None else environ,
        cwd=cwd,
        suggestion=suggestion,
        tool_dir=tool_dir,
        bin_dir=bin_dir,
    )


#: A child process is the only honest way to test "cannot hang": a blocking
#: ``read_text`` on a FIFO never returns, so an in-process call would hang the
#: suite instead of failing it. The child patches the same seams as ``_world``
#: and touches no launchd, task or real service.
_CLASSIFIER_CHILD = """
import sys
from pathlib import Path

from ciao import install_discovery, macos_service

home, cwd, tool_dir, bin_dir = (Path(part) for part in sys.argv[1:5])
Path.home = classmethod(lambda cls: home)
install_discovery._platform = lambda: "darwin"
agents = home / "Library" / "LaunchAgents"
macos_service.live_launch_agents_dir = lambda: agents
macos_service.server_job_absent = lambda **_kwargs: True
macos_service.launchd_override_disabled = lambda **_kwargs: False
print(
    install_discovery.classify_native_install(
        environ={},
        cwd=cwd,
        suggestion=None,
        tool_dir=tool_dir,
        bin_dir=bin_dir,
    ).value
)
"""


def _classify_in_child(
    home: Path, cwd: Path, tool_dir: Path, bin_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Run the classifier in a fresh interpreter, bounded by a hard timeout."""
    environment = dict(os.environ)
    # The child must import this checkout even when the suite is run from a
    # worktree that is not the installed editable source.
    root = str(Path(__file__).resolve().parent.parent)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (root, environment.get("PYTHONPATH")) if part
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _CLASSIFIER_CHILD,
            str(home),
            str(cwd),
            str(tool_dir),
            str(bin_dir),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=environment,
    )


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, int, bytes | None]]:
    """Every path under ``root`` with the facts a classifier must not change."""
    snapshot: dict[str, tuple[int, int, int, int, bytes | None]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = None
        if stat.S_ISREG(info.st_mode):
            content = path.read_bytes()
        snapshot[str(path.relative_to(root))] = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            content,
        )
    return snapshot


def _write_receipt() -> Path:
    receipt = install_receipt.default_receipt_path()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps(
            {
                "schema": 1,
                "version": "1.5.0",
                "executable": "/tmp/bin/ciao",
                "python": "/tmp/bin/python",
                "service_backend": "launchd",
                "service_label": macos_service.SERVER_LABEL,
                "installed_at": "2026-10-31T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return receipt


def _write_host_journal() -> Path:
    journal = server_host_install.upgrade_journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}", encoding="utf-8")
    return journal


def _write_lock(*, content: bytes = b"", mode: int = 0o600) -> Path:
    lock = server_host_install.upgrade_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(content)
    lock.chmod(mode)
    return lock


def test_a_quiet_machine_is_fresh(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh


def test_a_live_definition_is_configured(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    agents = macos_service.live_launch_agents_dir()
    (agents / f"{macos_service.SERVER_LABEL}.plist").write_text("plist", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_a_completed_workspace_is_configured(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".runtime").mkdir()
    (cwd / ".runtime" / "workspaces.json").write_text("{}", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


@pytest.mark.parametrize("case", ["dangling-symlink", "symlink-to-file"])
@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_a_symlinked_workspace_registry_is_unknown(
    monkeypatch, tmp_path, case: str
) -> None:
    """Only the regular file ``ciao setup`` writes proves a completed setup.

    A link to nowhere and a link to a file both fail that test, so neither may
    answer configured.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    registry = cwd / ".runtime" / "workspaces.json"
    registry.parent.mkdir()
    if case == "dangling-symlink":
        registry.symlink_to(tmp_path / "nowhere")
    else:
        target = tmp_path / "elsewhere.json"
        target.write_text("{}", encoding="utf-8")
        registry.symlink_to(target)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_a_directory_where_the_registry_belongs_is_unknown(
    monkeypatch, tmp_path
) -> None:
    """A directory at the registry name is not a registry."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".runtime" / "workspaces.json").mkdir(parents=True)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_workspace_registry_is_unknown(monkeypatch, tmp_path) -> None:
    """A FIFO is not the registry file, and reading it would be a trap."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".runtime").mkdir()
    os.mkfifo(cwd / ".runtime" / "workspaces.json")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_an_explicit_disabled_override_is_configured(monkeypatch, tmp_path) -> None:
    """A label launchd will not start is existing service configuration."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    monkeypatch.setattr(
        macos_service, "launchd_override_disabled", lambda **_kwargs: True
    )

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


@pytest.mark.parametrize("marker", ["uv-tool", "shim", "handoff", "receipt", "migration"])
def test_install_evidence_without_a_complete_setup_is_partial(
    monkeypatch, tmp_path, marker: str
) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    if marker == "uv-tool":
        (tool_dir / "ciaobot").mkdir(parents=True)
    elif marker == "shim":
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "ciao").write_text("#!/bin/sh\n", encoding="utf-8")
    elif marker == "handoff":
        from ciao import installer_handoff

        installer_handoff.result_path().parent.mkdir(parents=True)
        installer_handoff.result_path().write_text("{}", encoding="utf-8")
    elif marker == "receipt":
        _write_receipt()
    elif marker == "migration":
        migration = install_receipt.default_receipt_path().parent / "migration"
        (migration / "before").mkdir(parents=True)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


def test_a_host_upgrade_journal_is_partial_evidence(monkeypatch, tmp_path) -> None:
    """The durable journal is what a deferred host swap writes.

    The paths come from ``server_host_install``, not a second copy of the
    ``~/.local/state/ciaobot`` spelling. The journal is never deleted or rolled
    back by this classifier; recovery is the operator's explicit action.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    assert server_host_install.upgrade_journal_path().name == "server-host-upgrade.json"
    assert server_host_install.upgrade_lock_path().name == "server-host-upgrade.lock"
    assert (
        server_host_install.upgrade_journal_path().parent
        == server_host_install.upgrade_lock_path().parent
        == Path.home() / ".local" / "state" / "ciaobot"
    )
    _write_host_journal()

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


@pytest.mark.parametrize(
    "case",
    [
        "non-empty",
        "directory",
        "hardlink",
        pytest.param("empty", marks=POSIX_MODES),
        pytest.param("group-writable", marks=POSIX_MODES),
        pytest.param("symlink", marks=SYMLINK_ONLY),
    ],
)
def test_a_lock_is_not_partial_evidence_and_an_ambiguous_one_is_unknown(
    monkeypatch, tmp_path, case: str
) -> None:
    """The permanent lock is a mutex, not a historical install marker.

    An unlocked, empty, correctly owned lock says only that some host command
    once ran; it is not proof of a prior install. Anything this probe cannot
    account for — content, a link, a directory, another writer's mode — is
    unknown, never fresh.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    if case == "empty":
        _write_lock()
        expected = install_discovery.NativeInstallState.fresh
    elif case == "non-empty":
        _write_lock(content=b"stale\n")
        expected = install_discovery.NativeInstallState.unknown
    elif case == "symlink":
        target = tmp_path / "somewhere-else"
        target.write_text("", encoding="utf-8")
        lock = server_host_install.upgrade_lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.symlink_to(target)
        expected = install_discovery.NativeInstallState.unknown
    elif case == "directory":
        lock = server_host_install.upgrade_lock_path()
        lock.mkdir(parents=True)
        expected = install_discovery.NativeInstallState.unknown
    elif case == "hardlink":
        lock = _write_lock()
        os.link(lock, tmp_path / "another-name")
        expected = install_discovery.NativeInstallState.unknown
    else:
        _write_lock(mode=0o664)
        expected = install_discovery.NativeInstallState.unknown

    assert _classify(cwd, tool_dir, bin_dir) is expected


@POSIX_MODES
def test_a_held_lock_is_concurrent_and_unknown(monkeypatch, tmp_path) -> None:
    """A held lock is a host mutation in flight; a native install must refuse."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    lock = _write_lock()
    descriptor = open_fd(lock, os.O_RDWR)
    try:
        lock_exclusive(descriptor, blocking=False)
        assert (
            _classify(cwd, tool_dir, bin_dir)
            is install_discovery.NativeInstallState.unknown
        )
        # The probe must not have released the holder's lock.
        with pytest.raises(BlockingIOError):
            second = open_fd(lock, os.O_RDWR)
            try:
                lock_exclusive(second, blocking=False)
            finally:
                os.close(second)
    finally:
        unlock(descriptor)
        os.close(descriptor)


@POSIX_MODES
def test_the_lock_probe_leaves_an_idle_lock_released_and_unmodified(
    monkeypatch, tmp_path
) -> None:
    """One pre-mutation scan may take and release the lock, never write it."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    lock = _write_lock()
    before = _snapshot(tmp_path)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh
    assert _snapshot(tmp_path) == before

    descriptor = open_fd(lock, os.O_RDWR)
    try:
        lock_exclusive(descriptor, blocking=False)
    finally:
        unlock(descriptor)
        os.close(descriptor)


def test_the_scan_never_creates_the_state_directory(monkeypatch, tmp_path) -> None:
    """The probe's parent ``~/.local/state/ciaobot`` is checked, not created.

    The lock lives directly inside that directory, so asserting the directory
    is still absent after the scan is the state-dir check, and it covers the
    lock with it. The snapshot catches any sibling the scan might have made.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    state = server_host_install.upgrade_lock_path().parent
    before = _snapshot(tmp_path)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh
    assert not state.exists()
    assert _snapshot(tmp_path) == before


@SYMLINK_ONLY
def test_a_lock_under_a_dangling_symlink_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    state = server_host_install.upgrade_lock_path().parent
    state.parent.mkdir(parents=True)
    state.symlink_to(tmp_path / "nowhere")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_a_result_written_by_this_run_is_still_evidence(monkeypatch, tmp_path) -> None:
    """Presence is not ownership: there is no current-run or run-id exemption."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    from ciao import installer_handoff

    result = installer_handoff.result_path()
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps({"schema": 1, "run_id": "this-run"}), encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


def test_a_real_uv_tool_layout_is_partial(monkeypatch, tmp_path) -> None:
    """The caller's uv roots are inspected at their real names, not guessed."""

    cwd, _, _ = _world(monkeypatch, tmp_path)
    home = Path.home()
    tool_dir = home / ".local" / "share" / "uv" / "tools"
    bin_dir = home / ".local" / "bin"
    (tool_dir / "ciaobot").mkdir(parents=True)
    # A uv shim is a link to the tool environment's entry point on Unix and a
    # copied launcher on Windows; the classifier reads the name, so a regular
    # file models both without needing link privileges.
    bin_dir.mkdir(parents=True)
    (bin_dir / "ciao").write_text("#!/bin/sh\n", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


def test_a_custom_suggestion_is_searched(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    suggestion = tmp_path / "suggested"
    (suggestion / ".runtime").mkdir(parents=True)

    assert (
        _classify(cwd, tool_dir, bin_dir, suggestion=suggestion)
        is install_discovery.NativeInstallState.partial
    )
    (suggestion / ".runtime" / "workspaces.json").write_text("{}", encoding="utf-8")
    assert (
        _classify(cwd, tool_dir, bin_dir, suggestion=suggestion)
        is install_discovery.NativeInstallState.configured
    )


def test_a_candidate_the_os_refuses_is_unknown_not_an_exception(
    monkeypatch, tmp_path
) -> None:
    """A pathname ``lstat`` rejects outright is unseen, not absent.

    ``os.lstat`` raises ``ValueError`` for an embedded NUL, which a
    caller-supplied suggestion can carry. That used to escape the classifier
    and turn a read-only state question into a crash; it must answer unknown.
    """
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)

    assert (
        _classify(
            cwd, tool_dir, bin_dir, suggestion=tmp_path / "bad\x00suggestion"
        )
        is install_discovery.NativeInstallState.unknown
    )


def test_the_workspace_search_is_finite_and_does_not_walk_the_disk(
    monkeypatch, tmp_path
) -> None:
    """A complete workspace outside cwd/~/Ciaobot/suggestion is not found.

    This is a documented limit, not proof that no workspace exists elsewhere;
    #2340 has to combine the scan with the surfaces it already knows.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    other = tmp_path / "elsewhere"
    (other / ".runtime").mkdir(parents=True)
    (other / ".runtime" / "workspaces.json").write_text("{}", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh


def test_an_unreadable_receipt_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    receipt = install_receipt.default_receipt_path()
    receipt.parent.mkdir(parents=True)
    receipt.write_text("not json", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_a_legacy_workspace_env_is_configured(monkeypatch, tmp_path) -> None:
    """The pre-1.0 ``CIAO_WORKSPACES`` record is still a complete setup."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".env").write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_an_env_with_a_ciao_redirect_is_partial(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".env").write_text("CIAO_WORKSPACE=/somewhere\n", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


def test_an_oversize_env_is_unknown(monkeypatch, tmp_path) -> None:
    """A file over the native read bound is not a ``.env`` this run can trust."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".env").write_text(
        "CIAO_WORKSPACES=main\n" + "x" * install_discovery._NATIVE_TEXT_LIMIT,
        encoding="utf-8",
    )

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_an_oversize_receipt_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    receipt = install_receipt.default_receipt_path()
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"{" + b" " * install_discovery._NATIVE_TEXT_LIMIT)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_an_undecodable_env_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".env").write_bytes(b"CIAO_WORKSPACES=main\xff\xfe\n")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are not available")
def test_a_hard_linked_env_is_unknown(monkeypatch, tmp_path) -> None:
    """A second name can change the bytes this read judges; it is not read."""
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    target = tmp_path / "elsewhere.env"
    target.write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
    os.link(target, cwd / ".env")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links are not available")
def test_a_hard_linked_receipt_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    receipt = _write_receipt()
    os.link(receipt, tmp_path / "another-name.json")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(os.name != "posix", reason="symlink and permission semantics")
def test_a_symlinked_env_is_unknown(monkeypatch, tmp_path) -> None:
    """The native reader does not follow a link where the workspace env belongs."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    target = tmp_path / "elsewhere.env"
    target.write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
    (cwd / ".env").symlink_to(target)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX permissions and a non-root user",
)
def test_an_unreadable_env_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    env_path = cwd / ".env"
    env_path.write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
    env_path.chmod(0)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_a_replaced_env_is_unknown(monkeypatch, tmp_path) -> None:
    """A swap between the open and the final recheck is not read as this file."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    env_path = cwd / ".env"
    env_path.write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
    real_open = install_discovery.open_fd

    def open_then_replace(path, flags, mode=0o777, *, follow_symlinks=True):
        descriptor = real_open(path, flags, mode, follow_symlinks=follow_symlinks)
        replacement = cwd / "replacement.env"
        replacement.write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
        os.replace(replacement, env_path)
        return descriptor

    monkeypatch.setattr(install_discovery, "open_fd", open_then_replace)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
def test_a_symlinked_state_directory_is_unknown(monkeypatch, tmp_path) -> None:
    """The receipt, migration, journal and lock all live under one state dir.

    A link where that directory belongs makes every one of those surfaces
    somebody else's, so none of them is read and the machine is not called
    fresh.
    """

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    state = install_receipt.default_receipt_path().parent
    elsewhere = tmp_path / "elsewhere-state"
    elsewhere.mkdir()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.symlink_to(elsewhere)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_env_cannot_hang_the_classifier(monkeypatch, tmp_path) -> None:
    """A FIFO where ``.env`` belongs used to block ``read_text`` forever."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    os.mkfifo(cwd / ".env")

    completed = _classify_in_child(Path.home(), cwd, tool_dir, bin_dir)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == install_discovery.NativeInstallState.unknown.value


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_a_fifo_receipt_cannot_hang_the_classifier(monkeypatch, tmp_path) -> None:
    """The receipt is read through the same bounded descriptor as ``.env``."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    # The suite isolates the real receipt path in-process; the child runs
    # without that patch, so place the FIFO where its own home resolves it.
    receipt = (
        Path.home() / ".local" / "state" / "ciaobot" / "install-receipt.json"
    )
    receipt.parent.mkdir(parents=True)
    os.mkfifo(receipt)

    completed = _classify_in_child(Path.home(), cwd, tool_dir, bin_dir)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == install_discovery.NativeInstallState.unknown.value


def test_a_failed_service_probe_is_unknown_not_fresh(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)

    def unavailable(**_kwargs):
        raise RuntimeError("launchd did not answer")

    monkeypatch.setattr(macos_service, "server_job_absent", unavailable)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_a_failed_home_lookup_is_unknown_not_fresh(monkeypatch, tmp_path) -> None:
    """A home that cannot be resolved leaves ``~/Ciaobot`` unseen, not absent."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)

    def no_home(cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", classmethod(no_home))

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_configured_cwd_beats_a_failed_home_lookup(monkeypatch, tmp_path) -> None:
    """The home failure is one unknown signal; cwd's proof still wins."""

    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (cwd / ".runtime").mkdir()
    (cwd / ".runtime" / "workspaces.json").write_text("{}", encoding="utf-8")

    def no_home(cls):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "home", classmethod(no_home))

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_an_inherited_redirect_is_unknown_not_fresh(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)

    assert (
        _classify(cwd, tool_dir, bin_dir, environ={"CIAO_WORKSPACE": str(tmp_path)})
        is install_discovery.NativeInstallState.unknown
    )


def test_configured_beats_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    agents = macos_service.live_launch_agents_dir()
    (agents / f"{macos_service.SERVER_LABEL}.plist").write_text("plist", encoding="utf-8")

    assert (
        _classify(cwd, tool_dir, bin_dir, environ={"CIAO_WORKSPACE": str(tmp_path)})
        is install_discovery.NativeInstallState.configured
    )


def test_configured_beats_partial(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    agents = macos_service.live_launch_agents_dir()
    (agents / f"{macos_service.SERVER_LABEL}.plist").write_text("plist", encoding="utf-8")
    (tool_dir / "ciaobot").mkdir(parents=True)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_unknown_beats_partial(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (tool_dir / "ciaobot").mkdir(parents=True)

    assert (
        _classify(cwd, tool_dir, bin_dir, environ={"CIAO_WORKSPACE": str(tmp_path)})
        is install_discovery.NativeInstallState.unknown
    )


def test_partial_beats_fresh(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    (tool_dir / "ciaobot").mkdir(parents=True)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.partial


def test_a_non_directory_workspace_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    cwd.rmdir()
    cwd.write_text("not a directory", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


@SYMLINK_ONLY
def test_a_dangling_symlink_ancestor_is_not_absence(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir = _world(monkeypatch, tmp_path)
    suggestion = tmp_path / "gone" / "workspace"
    (tmp_path / "gone").symlink_to(tmp_path / "nowhere")

    assert (
        _classify(cwd, tool_dir, bin_dir, suggestion=suggestion)
        is install_discovery.NativeInstallState.unknown
    )


def test_windows_a_task_definition_is_configured(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir, task_dir = _windows_world(monkeypatch, tmp_path)
    (task_dir / windows_service.TASK_FILE_NAME).write_text("task", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_windows_a_registered_task_is_configured(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir, _task_dir = _windows_world(monkeypatch, tmp_path)
    monkeypatch.setattr(windows_service, "engine_task_registered", lambda: True)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.configured


def test_windows_a_failed_task_query_is_unknown(monkeypatch, tmp_path) -> None:
    cwd, tool_dir, bin_dir, _task_dir = _windows_world(monkeypatch, tmp_path)

    def unavailable():
        raise windows_service.WindowsServiceError("schtasks did not answer")

    monkeypatch.setattr(windows_service, "engine_task_registered", unavailable)

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.unknown


def test_windows_ignores_the_macos_only_host_upgrade_state(monkeypatch, tmp_path) -> None:
    """``server_host_install`` is macOS-only; its state is not Windows evidence."""

    cwd, tool_dir, bin_dir, _task_dir = _windows_world(monkeypatch, tmp_path)
    _write_host_journal()

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh


def test_windows_the_shim_marker_is_ciao_exe(monkeypatch, tmp_path) -> None:
    """uv and pip write the Windows console script as ``ciao.exe``.

    The extensionless ``ciao`` is the POSIX entry point; on Windows it is not
    the installed shim, so it is not install evidence, while a shim-only
    install (``ciao.exe`` and nothing else) is partial.
    """

    cwd, tool_dir, bin_dir, _task_dir = _windows_world(monkeypatch, tmp_path)
    bin_dir.mkdir(parents=True)
    (bin_dir / "ciao").write_text("#!/bin/sh\n", encoding="utf-8")

    assert _classify(cwd, tool_dir, bin_dir) is install_discovery.NativeInstallState.fresh

    (bin_dir / "ciao.exe").write_bytes(b"MZ")

    assert (
        _classify(cwd, tool_dir, bin_dir)
        is install_discovery.NativeInstallState.partial
    )
