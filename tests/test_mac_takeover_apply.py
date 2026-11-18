"""``ciao mac-takeover apply``/``finish``/``rollback``: the journalled takeover (#3129 S4b).

Fake launchd (recorded argv, per-verb hooks), fake clocks and real temp files:
nothing here talks to launchd, the live engine or the real home. The fixtures
come from the plan tests (``tests.test_mac_takeover_plan``).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

import ciao
from ciao import app_engine, cli, mac_takeover
from ciao.macos_service import LAUNCHCTL
from tests.test_mac_takeover_plan import LABEL, Machine, installer_install, machine  # noqa: F401

TAKEOVER_ID = "20261118T100000Z"


class Launchd:
    """A fake launchd: records every argv, answers per verb, hooks verbs.

    ``on_verb`` runs inside the runner call — the moment a test asserts on the
    state of the disk while the takeover is mid-step.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.statuses: dict[str, int] = {"print": 113}
        self.on_verb: dict[str, Callable[[], None]] = {}
        self.print_stdout = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        verb = argv[1] if len(argv) > 1 else ""
        self.calls.append(list(argv))
        hook = self.on_verb.get(verb)
        if hook is not None:
            hook()
        code = self.statuses.get(verb, 0)
        out = self.print_stdout if verb == "print" and code == 0 else ""
        return subprocess.CompletedProcess(argv, code, out, "")

    def argv_of(self, verb: str) -> list[str]:
        matches = [call for call in self.calls if len(call) > 1 and call[1] == verb]
        assert matches, f"launchd was never called with {verb}: {self.calls}"
        return matches[-1]

    def destructive(self) -> list[str]:
        return [call[1] for call in self.calls if call[1:2] in (["bootout"], ["bootstrap"])]


def ticking(step: float = 60.0) -> Callable[[], float]:
    """A clock that jumps ``step`` seconds per call: every wait ends promptly."""
    value = 0.0

    def tick() -> float:
        nonlocal value
        value += step
        return value

    return tick


def no_sleep(_seconds: float) -> None:
    return None


def answer_engine(monkeypatch: pytest.MonkeyPatch, machine: Machine) -> dict[str, bool]:
    """An installer-shaped install whose engine answers on the port until stopped.

    The returned flag flips the patched probes, so the tests decide whether the
    old engine is still answering after the bootout.
    """
    installer_install(machine)
    stopped = {"stopped": False}
    identity = {"startup_id": "abc", "pid": 77, "version": "1.10.0"}
    owner = {
        "pid": 77,
        "workspace": str(machine.workspace),
        "runtime": str(machine.workspace / ".runtime"),
        "port": 8443,
        "status": "running",
        "startup_id": "abc",
    }

    def engine_identity(port: int) -> dict[str, Any] | None:
        return None if stopped["stopped"] else dict(identity)

    monkeypatch.setattr(mac_takeover, "engine_identity", engine_identity)
    monkeypatch.setattr(mac_takeover, "read_lock_owner", lambda runtime: dict(owner))
    return stopped


def old_engine_answers(
    monkeypatch: pytest.MonkeyPatch,
    machine: Machine,
    launchd: Launchd,
    *,
    pid: int = 5,
    job_pid: int | None = None,
    owner_pid: int | None = None,
    startup_id: str = "old",
    loaded_after_bootstrap: bool = True,
) -> None:
    """The restored old job: loaded with ``job_pid``, answering on the port, owning the lock record."""
    monkeypatch.setattr(mac_takeover, "_pid_alive", lambda check: True)
    identity = {"startup_id": startup_id, "pid": pid, "version": "1.10.0"}
    owner = {
        "pid": pid if owner_pid is None else owner_pid,
        "workspace": str(machine.workspace),
        "runtime": str(machine.workspace / ".runtime"),
        "port": 8443,
        "status": "running",
        "startup_id": startup_id,
    }
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: dict(identity))
    monkeypatch.setattr(mac_takeover, "read_lock_owner", lambda runtime: dict(owner))
    launchd.print_stdout = f"pid = {pid if job_pid is None else job_pid}\n"
    if loaded_after_bootstrap:
        launchd.on_verb["bootstrap"] = lambda: launchd.statuses.__setitem__("print", 0)


def journal_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sequence of journal states, as they are written to disk."""
    seen: list[str] = []
    real = mac_takeover._write_journal

    def spy(entry: Path, journal: dict[str, Any]) -> None:
        seen.append(str(journal.get("state")))
        real(entry, journal)

    monkeypatch.setattr(mac_takeover, "_write_journal", spy)
    return seen


def _plist_path(machine: Machine) -> Path:
    return machine.agents / f"{LABEL}.plist"


def _plist_sha(machine: Machine) -> str:
    return mac_takeover._sha256_file(_plist_path(machine))


def run_apply(
    machine: Machine,
    launchd: Launchd,
    *,
    sha: str | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    return mac_takeover.apply(
        expect_sha256=sha if sha is not None else _plist_sha(machine),
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=clock if clock is not None else ticking(),
        sleep=no_sleep,
    )


def newest_entry() -> Path:
    entries = sorted(
        entry for entry in (mac_takeover.state_dir() / "takeover").iterdir() if entry.is_dir()
    )
    assert entries, "the takeover entry does not exist"
    return entries[-1]


def seed_takeover(
    machine: Machine,
    state: str,
    *,
    with_plist: bool = True,
    with_backup: bool = True,
    with_pointer: bool = False,
) -> str:
    """A takeover journal at ``state``, shaped exactly as apply would write it."""
    entry = mac_takeover.state_dir() / "takeover" / TAKEOVER_ID
    entry.mkdir(parents=True, exist_ok=True)
    entry.parent.chmod(0o700)
    entry.chmod(0o700)
    # The journal names a real plist's sha; the fixture alone writes no plist.
    if not _plist_path(machine).exists():
        machine.write_plist()
    journal: dict[str, Any] = {
        "schema": 1,
        "id": TAKEOVER_ID,
        "state": state,
        "created": "20261118T100000Z",
        "label": LABEL,
        "plist": str(_plist_path(machine)),
        "plist_sha256": _plist_sha(machine),
        "workspace": str(machine.workspace),
        "runtime_root": str(machine.workspace / ".runtime"),
        "port": 8443,
        "program_kind": "installer_env",
        "old_version": "1.10.0",
        "active_chats": 0,
        "trash": [{"path": str(machine.env_root), "proof": "receipt_env"}],
        "left_in_place": [],
        "dropped_env": [],
        "protected": [str(machine.workspace)],
    }
    (entry / mac_takeover.JOURNAL_NAME).write_text(json.dumps(journal, indent=2))
    (entry / mac_takeover.JOURNAL_NAME).chmod(0o600)
    if with_backup:
        shutil.copy2(_plist_path(machine), mac_takeover._backup_path(entry, LABEL))
    if not with_plist:
        _plist_path(machine).unlink()
    if with_pointer:
        app_engine.write_pointer(machine.workspace, machine.workspace / ".runtime")
    return TAKEOVER_ID


def apply_to_pointer_written(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> str:
    """A complete apply, from the installer-shaped install to ``pointer_written``."""
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)
    result = run_apply(machine, launchd)
    assert result["ok"] is True, result
    return str(result["id"])


def healthy_engine(
    monkeypatch: pytest.MonkeyPatch,
    machine: Machine,
    *,
    pid: int = 77,
    startup_id: str = "abc",
    port: int = 8443,
    owner_pid: int | None = None,
) -> None:
    """The bundled engine's post-takeover proofs: ready record and listener."""
    monkeypatch.setattr(mac_takeover, "_pid_alive", lambda check: True)
    app_engine.write_ready_record(
        pid=pid, port=port, startup_id=startup_id, workspace=machine.workspace, bootstrap=False
    )
    identity = {"startup_id": startup_id, "pid": pid, "version": ciao.__version__}
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda check: dict(identity))
    owner = {
        "pid": pid if owner_pid is None else owner_pid,
        "workspace": str(machine.workspace),
        "runtime": str(machine.workspace / ".runtime"),
        "port": port,
        "status": "running",
        "startup_id": startup_id,
    }
    monkeypatch.setattr(mac_takeover, "read_lock_owner", lambda runtime: dict(owner))
    HELD_LOCKS.append(hold_server_lock(machine))


HELD_LOCKS: list[Any] = []


@pytest.fixture(autouse=True)
def no_health_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac_takeover, "_HEALTH_RECHECK_S", 0.0)


@pytest.fixture(autouse=True)
def release_held_locks() -> Any:
    yield
    while HELD_LOCKS:
        HELD_LOCKS.pop().close()


def hold_server_lock(machine: Machine) -> Any:
    """Open and hold the runtime's ``server.lock`` the way the engine does."""
    from ciao.os_support.locks import lock_exclusive

    lock_path = machine.workspace / ".runtime" / "server.lock"
    lock_path.touch()
    handle = lock_path.open("rb")
    lock_exclusive(handle.fileno())
    return handle


# --- apply: the happy path ------------------------------------------------------


def test_apply_journals_states_and_backs_up_before_bootout(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    seen = journal_spy(monkeypatch)
    backups: list[bool] = []
    original = _plist_path(machine).read_bytes()

    def at_bootout() -> None:
        stopped["stopped"] = True
        backups.append(mac_takeover._backup_path(newest_entry(), LABEL).is_file())

    launchd.on_verb["bootout"] = at_bootout

    result = run_apply(machine, launchd)

    assert result["ok"] is True, result
    assert seen == ["created", "started", "stopping", "plist_removed", "pointer_written"]
    # The backup was already on disk the moment launchd was asked to stop the
    # old engine, and it is byte-identical to the definition it replaced.
    assert backups == [True]
    assert mac_takeover._backup_path(newest_entry(), LABEL).read_bytes() == original
    assert launchd.argv_of("bootout") == [
        LAUNCHCTL,
        "bootout",
        f"gui/{mac_takeover._getuid()}/{LABEL}",
    ]
    assert launchd.destructive() == ["bootout"]
    assert not _plist_path(machine).exists()
    journal = mac_takeover._load_journal(str(result["id"]))
    assert journal is not None and journal["state"] == "pointer_written"
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == machine.workspace.resolve()
    assert pointer.runtime_root == (machine.workspace / ".runtime").resolve()


def test_entry_holds_private_journal_and_backup(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_to_pointer_written(machine, monkeypatch)
    entry = newest_entry()
    assert os.stat(entry).st_mode & 0o777 == 0o700
    assert os.stat(entry / mac_takeover.JOURNAL_NAME).st_mode & 0o777 == 0o600
    payload = json.loads((entry / mac_takeover.JOURNAL_NAME).read_text())
    assert payload["program_kind"] == "installer_env"
    assert {item["proof"] for item in payload["trash"]} == {
        "receipt_env",
        "receipt",
        "installer_shim",
    }


def test_bootout_timeout_rebootstraps_and_leaves_the_plist(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The old engine keeps answering and launchd keeps reporting the job: the
    # wait must give up, put the job back and refuse, touching nothing else.
    answer_engine(monkeypatch, machine)
    launchd = Launchd()
    launchd.statuses["print"] = 0

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "old_engine_did_not_stop",
        "rolled_back",
    )
    journal = mac_takeover._load_journal(str(result["id"]))
    assert journal is not None and journal["state"] == "rolled_back"
    assert launchd.argv_of("bootstrap") == [
        LAUNCHCTL,
        "bootstrap",
        f"gui/{mac_takeover._getuid()}",
        str(_plist_path(machine)),
    ]
    assert _plist_path(machine).exists()
    assert app_engine.read_pointer() is None


def test_a_lock_still_held_blocks_even_when_launchctl_says_absent(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    stopped["stopped"] = True  # port silent, job absent — but the lock is held
    launchd = Launchd()
    handle = hold_server_lock(machine)
    try:
        result = run_apply(machine, launchd)
    finally:
        handle.close()

    assert (result["ok"], result["reason"]) == (False, "old_engine_did_not_stop")
    assert launchd.destructive() == ["bootout", "bootstrap"]
    assert _plist_path(machine).exists()
    assert app_engine.read_pointer() is None


# --- apply: the transaction gate ------------------------------------------------


def test_a_sha_mismatch_refuses_before_anything_destructive(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)
    launchd = Launchd()

    result = run_apply(machine, launchd, sha="0" * 64)

    assert (result["ok"], result["reason"]) == (False, "sha_mismatch")
    assert launchd.destructive() == []
    root = mac_takeover.state_dir() / "takeover"
    assert not root.exists() or not any(entry.is_dir() for entry in root.iterdir())


def test_a_resume_sha_mismatch_never_even_probes(machine: Machine) -> None:
    seed_takeover(machine, "started")
    launchd = Launchd()

    result = run_apply(machine, launchd, sha="0" * 64)

    assert (result["ok"], result["reason"]) == (False, "sha_mismatch")
    # The journal is resumed before plan() reads a plist or probes anything.
    assert launchd.calls == []


def test_apply_refuses_a_plan_that_is_not_a_takeover(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchd = Launchd()

    # No plist exists, so plan() says "none"; the hash is one the app once saw.
    result = run_apply(machine, launchd, sha="0" * 64)

    assert (result["ok"], result["reason"], result.get("action")) == (
        False,
        "not_takeover",
        "none",
    )
    assert launchd.calls == []


def test_a_plist_that_changed_under_the_plan_is_refused(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)
    changed = _plist_path(machine).read_bytes() + b"\x00"
    launchd = Launchd()

    def swap_after_the_plan_read_it() -> None:
        _plist_path(machine).write_bytes(changed)

    # plan() read the plist at step 3; the hash the copy verifies is checked
    # after plan() has returned, so this swap must not slip through.
    launchd.on_verb["print"] = swap_after_the_plan_read_it

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"]) == (False, "sha_mismatch")
    assert launchd.destructive() == []
    root = mac_takeover.state_dir() / "takeover"
    assert not root.exists() or not any(entry.is_dir() for entry in root.iterdir())


# --- apply: resume --------------------------------------------------------------


@pytest.mark.parametrize("state", ["started", "stopping", "plist_removed", "pointer_written"])
def test_resume_from_each_state_reaches_pointer_written(
    machine: Machine, state: str
) -> None:
    machine.write_plist()  # seed_takeover records this file's sha
    sha = _plist_sha(machine)
    seed_takeover(
        machine,
        state,
        with_plist=state in ("started", "stopping"),
        with_pointer=state == "pointer_written",
    )
    launchd = Launchd()

    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )

    assert result["ok"] is True, result
    journal = mac_takeover._load_journal(str(result["id"]))
    assert journal is not None and journal["state"] == "pointer_written"
    bootouts = [call for call in launchd.calls if call[1:2] == ["bootout"]]
    if state == "started":
        assert len(bootouts) == 1  # the crashed attempt never stopped the engine
    else:
        assert bootouts == []
    assert not _plist_path(machine).exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == machine.workspace.resolve()


def test_resume_skips_the_stop_once_the_plist_is_gone(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()  # seed_takeover records this file's sha
    sha = _plist_sha(machine)
    seed_takeover(machine, "plist_removed", with_plist=False)
    launchd = Launchd()
    launchd.statuses["print"] = 113

    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )

    assert result["ok"] is True, result
    assert launchd.destructive() == []


def test_apply_never_unlinks_the_plist_without_a_verified_backup(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "stopping")
    entry = mac_takeover.state_dir() / "takeover" / TAKEOVER_ID
    mac_takeover._backup_path(entry, LABEL).write_text("tampered")
    launchd = Launchd()

    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )

    assert (result["ok"], result["reason"]) == (False, "backup_unusable")
    assert _plist_path(machine).exists()
    assert app_engine.read_pointer() is None
    journal = mac_takeover._load_journal(TAKEOVER_ID)
    assert journal is not None and journal["state"] == "stopping"


# --- finish ---------------------------------------------------------------------


def test_finish_proves_health_then_names_the_candidates(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert result["ok"] is True
    assert sorted((item["proof"], Path(item["path"]).name) for item in result["trash"]) == [
        ("installer_shim", "ciao"),
        ("receipt", "install-receipt.json"),
        ("receipt_env", "ciaobot"),
    ]
    assert result["left_in_place"] == []
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "healthy"

    # A lost first answer can be re-fetched; --done closes the journal.
    again = mac_takeover.finish(takeover_id=takeover_id)
    assert again["ok"] is True
    assert [item["path"] for item in again["trash"]] == [item["path"] for item in result["trash"]]
    done = mac_takeover.finish(takeover_id=takeover_id, done=True)
    assert done["ok"] is True
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "finished"
    after = mac_takeover.finish(takeover_id=takeover_id)
    assert after["ok"] is True and after["trash"] == []


@pytest.mark.parametrize(
    ("target", "check"),
    [
        ("listener_names_another_startup_id", "listener"),
        ("ready_pid_is_dead", "ready"),
        ("another_pid_owns_the_lock", "lock"),
        ("pointer_names_another_workspace", "pointer"),
    ],
)
def test_finish_refuses_on_a_broken_health_proof(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, target: str, check: str
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    if target == "listener_names_another_startup_id":
        healthy_engine(monkeypatch, machine)
        other = {"startup_id": "elsewhere", "pid": 77, "version": "1.10.0"}
        monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: dict(other))
    elif target == "ready_pid_is_dead":
        healthy_engine(monkeypatch, machine)
        monkeypatch.setattr(mac_takeover, "_pid_alive", lambda pid: False)
    elif target == "another_pid_owns_the_lock":
        healthy_engine(monkeypatch, machine, owner_pid=9999)
    else:
        healthy_engine(monkeypatch, machine)
        other_workspace = machine.home / "other-workspace"
        (other_workspace / ".runtime").mkdir(parents=True)
        app_engine.write_pointer(other_workspace, other_workspace / ".runtime")

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert (result["ok"], result["reason"]) == (False, "not_healthy")
    assert result.get("check") == check
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"


def test_finish_refuses_a_takeover_that_never_reached_the_pointer(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_takeover(machine, "stopping")
    healthy_engine(monkeypatch, machine)

    result = mac_takeover.finish(takeover_id=TAKEOVER_ID, done=True)

    assert (result["ok"], result["reason"]) == (False, "not_healthy")
    journal = mac_takeover._load_journal(TAKEOVER_ID)
    assert journal is not None and journal["state"] == "stopping"


def test_finish_refuses_an_unknown_takeover(machine: Machine) -> None:
    assert mac_takeover.finish(takeover_id="20261118T090000Z") == {
        "ok": False,
        "reason": "no_journal",
        "id": "20261118T090000Z",
    }


def test_finish_relists_only_what_is_still_there(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    # The app already moved the env to the Trash on a lost first call.
    shutil.rmtree(machine.env_root)

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert result["ok"] is True
    assert sorted(Path(item["path"]).name for item in result["trash"]) == [
        "ciao",
        "install-receipt.json",
    ]


def test_finish_drops_candidates_that_shelter_the_workspace(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.workspace = machine.env_root / "ws"
    (machine.workspace / ".runtime").mkdir(parents=True)
    (machine.workspace / ".runtime" / "workspaces.json").write_text("{}")
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    # The journal (as only a tampered journal could) also names the env root
    # that now contains the workspace: finish re-runs the same safety filter.
    entry = mac_takeover._takeover_root() / takeover_id
    journal = json.loads((entry / mac_takeover.JOURNAL_NAME).read_text())
    journal["trash"].append({"path": str(machine.env_root), "proof": "receipt_env"})
    (entry / mac_takeover.JOURNAL_NAME).write_text(json.dumps(journal))
    (entry / mac_takeover.JOURNAL_NAME).chmod(0o600)

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert result["ok"] is True
    names = [Path(item["path"]).name for item in result["trash"]]
    assert "ciaobot" not in names
    assert "install-receipt.json" in names and "ciao" in names


# --- rollback -------------------------------------------------------------------


def test_rollback_restores_the_plist_and_clears_the_pointer(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = machine.write_plist().read_bytes()
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    assert app_engine.read_pointer() is not None
    assert not _plist_path(machine).exists()
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert result["ok"] is True, result
    assert result["state"] == "rolled_back"
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "rolled_back"
    assert launchd.argv_of("bootstrap") == [
        LAUNCHCTL,
        "bootstrap",
        f"gui/{mac_takeover._getuid()}",
        str(_plist_path(machine)),
    ]
    assert _plist_path(machine).read_bytes() == original
    assert app_engine.read_pointer() is None


def test_rollback_works_before_the_plist_was_removed(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = machine.write_plist().read_bytes()
    seed_takeover(machine, "started")
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)

    result = mac_takeover.rollback(
        takeover_id=TAKEOVER_ID, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert result["ok"] is True, result
    assert result["state"] == "rolled_back"
    # No bootout ran, no rewrite happened: the definition was never unlinked.
    assert launchd.destructive() == ["bootstrap"]
    assert _plist_path(machine).read_bytes() == original
    assert app_engine.read_pointer() is None


def test_rollback_refuses_while_a_bundled_engine_holds_the_lock(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    launchd = Launchd()
    handle = hold_server_lock(machine)
    try:
        result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    finally:
        handle.close()

    assert (result["ok"], result["reason"]) == (False, "bundled_engine_running")
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"
    assert launchd.destructive() == []
    assert not _plist_path(machine).exists()
    # Nothing was undone before the refusal: the pointer still stands.
    assert app_engine.read_pointer() is not None


def test_rollback_refuses_plist_content_it_did_not_write(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    _plist_path(machine).write_text("someone else's definition")
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"]) == (False, "plist_conflict")
    assert _plist_path(machine).read_text() == "someone else's definition"
    assert launchd.calls == []
    # A refused rollback has changed nothing, the pointer included.
    assert app_engine.read_pointer() is not None


def test_rollback_refuses_a_tampered_backup(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_takeover(machine, "plist_removed", with_plist=False, with_pointer=True)
    entry = mac_takeover.state_dir() / "takeover" / TAKEOVER_ID
    mac_takeover._backup_path(entry, LABEL).write_text("tampered")
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=TAKEOVER_ID, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"]) == (False, "backup_unusable")
    assert not _plist_path(machine).exists()
    assert launchd.calls == []
    assert app_engine.read_pointer() is not None


@pytest.mark.parametrize("state", ["finished", "rolled_back"])
def test_rollback_refuses_a_settled_journal(
    machine: Machine, state: str
) -> None:
    seed_takeover(machine, state)
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=TAKEOVER_ID, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"]) == (False, "already_settled")
    assert launchd.calls == []
    assert _plist_path(machine).exists()  # untouched either way


def test_rollback_keeps_the_pointer_until_the_old_engine_answers(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    # engine_identity stays None (the machine fixture default): the old engine
    # never answers within the rollback's wait.
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(100.0), sleep=no_sleep
    )

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "old_engine_did_not_start",
        "pointer_written",
    )
    # Nothing is settled or cleared on a guess: the pointer and the journal
    # stand, and the plist is back for the retry.
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"
    assert app_engine.read_pointer() is not None
    assert _plist_path(machine).exists()

    # The retry finds the job loaded, does not bootstrap again, and settles
    # once the old engine answers.
    launchd.calls.clear()
    old_engine_answers(monkeypatch, machine, launchd)
    launchd.statuses["print"] = 0
    again = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert again["ok"] is True, again
    assert launchd.destructive() == []
    assert app_engine.read_pointer() is None


def test_rollback_refuses_an_unknown_takeover(machine: Machine) -> None:
    result = mac_takeover.rollback(
        takeover_id="20261118T090000Z", runner=Launchd(), clock=ticking(), sleep=no_sleep
    )
    assert (result["ok"], result["reason"]) == (False, "no_journal")


# --- CLI ------------------------------------------------------------------------


def test_cli_apply_finish_and_rollback(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    sha = _plist_sha(machine)
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)
    # The CLI passes no runner of its own: route apply's default to the fake.
    monkeypatch.setattr(mac_takeover, "_default_runner", lambda: launchd)

    code = cli.main(
        [
            "mac-takeover",
            "apply",
            "--json",
            "--expect-sha256",
            sha,
            "--launch-agents-dir",
            str(machine.agents),
            "--label",
            LABEL,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert (payload["ok"], payload["state"]) == (True, "pointer_written")
    takeover_id = payload["id"]

    healthy_engine(monkeypatch, machine)
    code = cli.main(["mac-takeover", "finish", "--json", "--id", takeover_id])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True and payload["trash"]

    code = cli.main(["mac-takeover", "finish", "--json", "--id", takeover_id, "--done"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True

    code = cli.main(["mac-takeover", "rollback", "--json", "--id", takeover_id])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert (payload["ok"], payload["reason"]) == (False, "already_settled")


def test_cli_apply_refusal_exits_three(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answer_engine(monkeypatch, machine)
    launchd = Launchd()
    # The CLI passes no runner of its own: route apply's default to the fake.
    monkeypatch.setattr(mac_takeover, "_default_runner", lambda: launchd)

    code = cli.main(
        [
            "mac-takeover",
            "apply",
            "--json",
            "--expect-sha256",
            "0" * 64,
            "--launch-agents-dir",
            str(machine.agents),
            "--label",
            LABEL,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert (payload["ok"], payload["reason"]) == (False, "sha_mismatch")


def test_cli_finish_and_rollback_refusals_exit_three(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["mac-takeover", "finish", "--json", "--id", "no-such-takeover"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and (payload["ok"], payload["reason"]) == (False, "no_journal")

    code = cli.main(["mac-takeover", "rollback", "--json", "--id", "no-such-takeover"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3 and (payload["ok"], payload["reason"]) == (False, "no_journal")


# --- review fixes: mutual exclusion, young entries, strict journal -----------------


def edit_journal(takeover_id: str, **changes: Any) -> None:
    """Rewrite a journal the way a tampering (or confused) writer would."""
    path = mac_takeover._journal_path(takeover_id)
    document = json.loads(path.read_text())
    document.update(changes)
    path.write_text(json.dumps(document))
    path.chmod(0o600)


def test_a_second_apply_while_the_first_runs_answers_busy(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    second: list[dict[str, Any]] = []
    original = _plist_path(machine).read_bytes()

    def at_bootout() -> None:
        stopped["stopped"] = True
        second.append(run_apply(machine, launchd))
        second.append(mac_takeover.rollback(takeover_id=TAKEOVER_ID, runner=launchd))
        second.append(mac_takeover.finish(takeover_id=TAKEOVER_ID))

    launchd.on_verb["bootout"] = at_bootout

    result = run_apply(machine, launchd)

    assert result["ok"] is True, result
    assert [item["reason"] for item in second] == ["busy", "busy", "busy"]
    assert launchd.destructive() == ["bootout"]
    # The loser touched nothing: the winner's entry and backup are whole.
    assert mac_takeover._backup_path(newest_entry(), LABEL).read_bytes() == original
    assert len([entry for entry in newest_entry().parent.iterdir() if entry.is_dir()]) == 1


def test_two_concurrent_applies_boot_the_old_engine_out_once(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    original = _plist_path(machine).read_bytes()
    inside = threading.Event()
    release = threading.Event()

    def at_bootout() -> None:
        stopped["stopped"] = True
        inside.set()
        release.wait(5)

    launchd.on_verb["bootout"] = at_bootout
    results: list[dict[str, Any]] = []
    first = threading.Thread(target=lambda: results.append(run_apply(machine, launchd)))
    first.start()
    assert inside.wait(5)
    losing = run_apply(machine, launchd)
    release.set()
    first.join(10)

    assert losing["reason"] == "busy"
    assert results and results[0]["ok"] is True
    assert launchd.destructive() == ["bootout"]
    assert mac_takeover._backup_path(newest_entry(), LABEL).read_bytes() == original


def test_an_id_collision_never_removes_another_entry(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    monkeypatch.setattr(mac_takeover, "_utc_stamp", lambda: "20261118T100000Z")
    taken = mac_takeover._takeover_root() / "20261118T100000Z"
    mac_takeover._make_entry(taken)
    (taken / "keep.txt").write_text("a finished run's file")
    edit_stub = {"schema": 1, "state": "finished"}
    (taken / mac_takeover.JOURNAL_NAME).write_text(json.dumps(edit_stub))
    (taken / mac_takeover.JOURNAL_NAME).chmod(0o600)
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)

    result = run_apply(machine, launchd)

    assert result["ok"] is True, result
    assert result["id"] == "20261118T100000Z-2"
    assert (taken / "keep.txt").read_text() == "a finished run's file"


def test_the_journal_never_carries_a_stale_updated_stamp(machine: Machine) -> None:
    entry = mac_takeover._takeover_root() / TAKEOVER_ID
    mac_takeover._make_entry(entry)
    mac_takeover._write_journal(entry, {"state": "started", "updated": "19700101T000000Z"})
    written = json.loads((entry / mac_takeover.JOURNAL_NAME).read_text())
    assert written["updated"] != "19700101T000000Z"


def test_a_transition_never_writes_back_a_stale_state(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()

    def settled_meanwhile() -> None:
        stopped["stopped"] = True
        edit_journal(newest_entry().name, state="rolled_back")

    launchd.on_verb["bootout"] = settled_meanwhile

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"]) == (False, "already_settled")
    journal = mac_takeover._load_journal(newest_entry().name)
    assert journal is not None and journal["state"] == "rolled_back"
    assert _plist_path(machine).exists()


def _create_skeleton(*, with_journal: bool, backup: bool = True) -> Path:
    entry = mac_takeover._takeover_root() / "20261118T090000Z"
    mac_takeover._make_entry(entry)
    if backup:
        (entry / f"{LABEL}.plist").write_text("half a copy")
    if with_journal:
        mac_takeover._write_journal(entry, {"state": "created", "id": entry.name})
    return entry


@pytest.mark.parametrize("with_journal", [False, True])
def test_a_journal_less_or_skeleton_entry_is_discarded_and_apply_continues(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, with_journal: bool
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    entry = _create_skeleton(with_journal=with_journal)
    # plan() does not treat it as unfinished work.
    assert (
        mac_takeover.plan(launch_agents_dir=machine.agents, label=LABEL, runner=Launchd()).action
        == "takeover"
    )
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)

    result = run_apply(machine, launchd)

    assert result["ok"] is True, result
    assert not entry.exists()
    assert result["id"] != entry.name


def test_a_crash_between_the_entry_and_the_backup_leaves_a_journal(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)

    def crash(source: Path, target: Path) -> None:
        raise KeyboardInterrupt  # the process dies; nothing cleans up after it

    monkeypatch.setattr(mac_takeover, "_fsync_copy", crash)
    monkeypatch.setattr(shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(KeyboardInterrupt):
        run_apply(machine, Launchd())

    entry = newest_entry()
    skeleton = json.loads((entry / mac_takeover.JOURNAL_NAME).read_text())
    assert skeleton["state"] == "created"  # never an entry without a journal
    assert _plist_path(machine).exists()


@pytest.mark.parametrize("how", ["edited_live_plist", "journal_only"])
def test_apply_never_unlinks_a_plist_that_changed_since_the_journal(
    machine: Machine, how: str
) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "stopping")
    entry = mac_takeover._takeover_root() / TAKEOVER_ID
    _plist_path(machine).write_bytes(_plist_path(machine).read_bytes() + b"\n<!-- user edit -->")
    edited = _plist_path(machine).read_bytes()
    launchd = Launchd()

    if how == "journal_only":
        # The helper in isolation, as a resumed run would reach it.
        journal = mac_takeover._load_journal(TAKEOVER_ID)
        assert journal is not None
        assert mac_takeover._remove_plist(entry, journal) == "plist_changed"
    else:
        result = mac_takeover.apply(
            expect_sha256=sha,
            launch_agents_dir=machine.agents,
            label=LABEL,
            runner=launchd,
            clock=ticking(),
            sleep=no_sleep,
        )
        assert (result["ok"], result["reason"]) == (False, "plist_changed")
        assert launchd.calls == []
    assert _plist_path(machine).read_bytes() == edited
    assert app_engine.read_pointer() is None


def test_a_resume_rehashes_the_live_plist_before_the_stop(machine: Machine) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "started")
    _plist_path(machine).write_bytes(_plist_path(machine).read_bytes() + b" ")
    launchd = Launchd()

    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )

    assert (result["ok"], result["reason"]) == (False, "plist_changed")
    assert launchd.calls == []


def test_a_started_resume_reverifies_the_backup_before_bootout(machine: Machine) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "started")
    entry = mac_takeover._takeover_root() / TAKEOVER_ID
    mac_takeover._backup_path(entry, LABEL).write_text("tampered")
    launchd = Launchd()

    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )

    assert (result["ok"], result["reason"]) == (False, "backup_unusable")
    assert launchd.calls == []
    assert _plist_path(machine).exists()


@pytest.mark.parametrize(
    "forgery",
    ["plist_elsewhere", "other_label", "live_label", "workspace", "runtime_root"],
)
def test_a_forged_journal_is_not_acted_on(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, forgery: str
) -> None:
    machine.write_plist()
    seed_takeover(machine, "plist_removed", with_plist=False, with_pointer=True)
    elsewhere = machine.home / "elsewhere"
    elsewhere.mkdir()
    changes: dict[str, Any] = {
        "plist_elsewhere": {"plist": str(elsewhere / f"{LABEL}.plist")},
        "other_label": {"label": "com.ciao.server.other"},
        "live_label": {"label": mac_takeover.SERVER_LABEL},
        "workspace": {"workspace": str(elsewhere)},
        "runtime_root": {"runtime_root": str(elsewhere)},
    }[forgery]
    edit_journal(TAKEOVER_ID, **changes)
    launchd = Launchd()

    assert mac_takeover._load_journal(TAKEOVER_ID) is None
    assert mac_takeover.rollback(takeover_id=TAKEOVER_ID, runner=launchd)["reason"] == "no_journal"
    assert mac_takeover.finish(takeover_id=TAKEOVER_ID)["reason"] == "no_journal"
    assert launchd.calls == []
    assert app_engine.read_pointer() is not None
    assert not _plist_path(machine).exists()


def test_a_journal_for_another_label_than_the_plans_is_not_resumed(machine: Machine) -> None:
    machine.write_plist()
    seed_takeover(machine, "started")
    assert mac_takeover._load_journal(TAKEOVER_ID, label=LABEL) is not None
    assert mac_takeover._load_journal(TAKEOVER_ID, label="com.ciao.server.t0") is None


def test_a_journal_that_is_not_owner_only_is_not_trusted(machine: Machine) -> None:
    seed_takeover(machine, "started")
    path = mac_takeover._journal_path(TAKEOVER_ID)
    path.chmod(0o644)
    assert mac_takeover._load_journal(TAKEOVER_ID) is None
    path.chmod(0o600)
    assert mac_takeover._load_journal(TAKEOVER_ID) is not None
    (mac_takeover._takeover_root() / TAKEOVER_ID).chmod(0o755)
    assert mac_takeover._load_journal(TAKEOVER_ID) is None


def test_a_symlinked_takeover_directory_is_refused_everywhere(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)
    real = machine.home / "attacker-dir"
    real.mkdir(mode=0o700)
    root = mac_takeover._takeover_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(real)
    launchd = Launchd()

    planned = mac_takeover.plan(launch_agents_dir=machine.agents, label=LABEL, runner=launchd)
    results = [
        run_apply(machine, launchd),
        mac_takeover.finish(takeover_id=TAKEOVER_ID),
        mac_takeover.rollback(takeover_id=TAKEOVER_ID, runner=launchd),
    ]

    assert (planned.action, planned.reason) == ("refuse", "takeover_state_unsafe")
    assert [item["reason"] for item in results] == ["takeover_state_unsafe"] * 3
    assert launchd.destructive() == []
    assert list(real.iterdir()) == []  # nothing was written through the link


def test_a_symlinked_entry_is_never_followed(machine: Machine) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "started")
    entry = mac_takeover._takeover_root() / TAKEOVER_ID
    moved = machine.home / "moved-entry"
    entry.rename(moved)
    entry.symlink_to(moved)
    launchd = Launchd()

    assert mac_takeover._load_journal(TAKEOVER_ID) is None
    result = mac_takeover.apply(
        expect_sha256=sha,
        launch_agents_dir=machine.agents,
        label=LABEL,
        runner=launchd,
        clock=ticking(),
        sleep=no_sleep,
    )
    assert (result["ok"], result["reason"]) == (False, "no_journal")
    assert mac_takeover.rollback(takeover_id=TAKEOVER_ID, runner=launchd)["reason"] == "no_journal"
    assert launchd.calls == []
    assert (moved / mac_takeover.JOURNAL_NAME).exists()


# --- launchctl failures ----------------------------------------------------------


class TimingOut(Launchd):
    """A launchctl that never answers the verbs in ``hang``."""

    def __init__(self, *hang: str) -> None:
        super().__init__()
        self.hang = set(hang)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(argv) > 1 and argv[1] in self.hang:
            self.calls.append(list(argv))
            raise subprocess.TimeoutExpired(argv, 30)
        return super().__call__(argv, **kwargs)


def test_a_bootout_that_times_out_still_completes_when_the_job_is_gone(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    stopped["stopped"] = True  # the engine did stop, launchctl just never said so
    launchd = TimingOut("bootout")

    result = run_apply(machine, launchd)

    assert result["ok"] is True, result


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "oserror"])
def test_a_failed_rebootstrap_is_reported_with_the_command_and_recorded(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    answer_engine(monkeypatch, machine)  # the old engine never stops answering
    launchd: Launchd
    if failure == "timeout":
        launchd = TimingOut("bootstrap")
    else:
        launchd = Launchd()
        launchd.statuses["bootstrap"] = 5
        if failure == "oserror":

            def missing() -> None:
                raise OSError("launchctl vanished")

            launchd.on_verb["bootstrap"] = missing
    launchd.statuses["print"] = 113 if failure != "oserror" else 0

    result = run_apply(machine, launchd)

    if failure == "oserror":
        # print says loaded: "already loaded" is as good as a clean bootstrap.
        assert result["reason"] == "old_engine_did_not_stop"
        return
    assert (result["ok"], result["reason"]) == (False, "old_engine_not_restarted")
    assert result["command"] == (
        f"{LAUNCHCTL} bootstrap gui/{mac_takeover._getuid()} {_plist_path(machine)}"
    )
    assert result["command"] in result["message"]
    journal = mac_takeover._load_journal(str(result["id"]))
    assert journal is not None
    assert journal["state"] == "started" and journal["problem"] == "old_engine_not_restarted"
    assert _plist_path(machine).exists()
    assert app_engine.read_pointer() is None


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_a_failed_rollback_bootstrap_keeps_the_pointer_and_the_journal(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    launchd: Launchd = TimingOut("bootstrap") if failure == "timeout" else Launchd()
    launchd.statuses["bootstrap"] = 5

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"]) == (False, "old_engine_not_restarted")
    assert "bootstrap" in result["command"] and str(_plist_path(machine)) in result["command"]
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"
    assert app_engine.read_pointer() is not None


# --- rollback: the atomic restore --------------------------------------------------


def test_a_crash_mid_restore_leaves_a_retryable_state(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = machine.write_plist().read_bytes()
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    real_copy = shutil.copy2

    def half_copy(source: Any, target: Any, **kwargs: Any) -> Any:
        Path(target).write_bytes(Path(source).read_bytes()[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copy2", half_copy)
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"]) == (False, "plist_restore_failed")
    # No partial definition is ever visible at the real name, and no temp
    # file is left in LaunchAgents.
    assert not _plist_path(machine).exists()
    assert list(machine.agents.iterdir()) == []
    assert launchd.calls == []
    assert app_engine.read_pointer() is not None
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"

    monkeypatch.setattr(shutil, "copy2", real_copy)
    old_engine_answers(monkeypatch, machine, launchd)
    retry = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert retry["ok"] is True, retry
    assert _plist_path(machine).read_bytes() == original


# --- finish: the health proof and the trash filter ----------------------------------


def test_finish_refuses_when_the_runtime_lock_is_not_actually_held(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    HELD_LOCKS.pop().close()  # the owner record says running; nobody holds the lock

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert (result["ok"], result["reason"], result.get("check")) == (False, "not_healthy", "lock")


def test_finish_refuses_a_pointer_with_another_runtime_root(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    other = machine.workspace / "other-runtime"
    other.mkdir()
    app_engine.write_pointer(machine.workspace, other)

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert (result["ok"], result["reason"], result.get("check")) == (False, "not_healthy", "pointer")


def test_finish_filters_with_the_plans_protected_paths(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A vault configured in the .env under the installer env: plan() protects it.
    vault = machine.env_root / "vault"
    vault.mkdir()
    (machine.workspace / ".env").write_text(f"CIAO_VAULT_ROOT={vault}\n")
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and str(vault) in journal["protected"]
    # Only a tampered journal lists the env that shelters the vault.
    edit_journal(
        takeover_id,
        trash=[{"path": str(machine.env_root), "proof": "receipt_env"}],
    )

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert result["ok"] is True
    assert result["trash"] == []


def test_finish_rechecks_the_shim_marker_and_the_receipt(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    # Since the plan the shim became somebody else's script and the receipt a link.
    machine.shim("#!/bin/sh\necho mine\n")
    receipt = machine.state / "install-receipt.json"
    receipt.unlink()
    receipt.symlink_to(machine.home / "elsewhere.json")
    (machine.home / "elsewhere.json").write_text("{}")

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert result["ok"] is True
    assert [item["proof"] for item in result["trash"]] == ["receipt_env"]


def test_finish_refuses_a_journal_without_a_protected_list(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    for value in ([], None):
        journal = json.loads(mac_takeover._journal_path(takeover_id).read_text())
        journal.pop("protected", None)
        if value is not None:
            journal["protected"] = value
        mac_takeover._journal_path(takeover_id).write_text(json.dumps(journal))
        mac_takeover._journal_path(takeover_id).chmod(0o600)

        result = mac_takeover.finish(takeover_id=takeover_id)

        assert (result["ok"], result["reason"]) == (False, "protected_missing")
        assert "trash" not in result
        loaded = mac_takeover._load_journal(takeover_id)
        assert loaded is not None and loaded["state"] == "pointer_written"


def test_run_cli_does_not_swallow_the_real_runner_guard(
    machine: Machine, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.conftest import RealRunnerReached

    # No runner is injected: the shared guard fires and run_cli must not turn
    # it into a polite error document.
    installer_install(machine)
    with pytest.raises(RealRunnerReached):
        cli.main(
            ["mac-takeover", "plan", "--json", "--launch-agents-dir", str(machine.agents), "--label", LABEL]
        )


# --- second security review ---------------------------------------------------------


def test_the_journal_never_reads_the_live_dotenv(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    # A later PWA_PORT edit, then an oversized and unreadable .env: the plist is
    # gone, yet the journal stays readable and finish and rollback still work.
    env = machine.workspace / ".env"
    for content in ("PWA_PORT=9191\n", "X=" + "a" * (2 * 1024 * 1024), None):
        if content is None:
            env.unlink()
            env.symlink_to(machine.workspace / "nowhere")
        else:
            env.write_text(content)
        assert mac_takeover._load_journal(takeover_id) is not None
    env.unlink()
    healthy_engine(monkeypatch, machine)
    assert mac_takeover.finish(takeover_id=takeover_id)["ok"] is True


def release_held_locks_now() -> None:
    while HELD_LOCKS:
        HELD_LOCKS.pop().close()


def test_rollback_works_after_the_dotenv_changed_and_grew_unreadable(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    (machine.workspace / ".env").write_text("PWA_PORT=9191\n" + "#" * (2 * 1024 * 1024))
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)
    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert result["ok"] is True, result


def test_a_stopping_resume_with_the_old_engine_back_refuses_and_changes_nothing(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)  # the old engine answers on the port
    seed_takeover(machine, "stopping")
    launchd = Launchd()

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "old_engine_back",
        "stopping",
    )
    assert result["id"] == TAKEOVER_ID
    assert _plist_path(machine).exists()
    assert launchd.destructive() == []
    journal = mac_takeover._load_journal(TAKEOVER_ID)
    assert journal is not None and journal["state"] == "stopping"
    assert app_engine.read_pointer() is None


def test_a_stopping_resume_with_the_job_loaded_again_refuses(machine: Machine) -> None:
    seed_takeover(machine, "stopping")
    launchd = Launchd()
    launchd.statuses["print"] = 0

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"]) == (False, "old_engine_back")
    assert _plist_path(machine).exists()
    assert launchd.destructive() == []


@pytest.mark.parametrize("state", ["plist_removed", "pointer_written"])
def test_a_resume_after_an_interrupted_rollback_refuses(machine: Machine, state: str) -> None:
    # Rollback restored the plist and the job, then died before rolled_back.
    seed_takeover(machine, state, with_plist=True, with_pointer=state == "pointer_written")
    launchd = Launchd()

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"], result["state"]) == (False, "old_engine_back", state)
    assert _plist_path(machine).exists()
    assert launchd.destructive() == []
    journal = mac_takeover._load_journal(TAKEOVER_ID)
    assert journal is not None and journal["state"] == state
    # The takeover is not wedged: a rollback is still possible.
    assert (
        mac_takeover.rollback(
            takeover_id=TAKEOVER_ID, runner=launchd, clock=ticking(), sleep=no_sleep
        )["reason"]
        == "old_engine_did_not_start"
    )


@pytest.mark.parametrize("state", ["plist_removed", "pointer_written"])
def test_a_resume_with_the_old_job_loaded_but_no_plist_refuses(
    machine: Machine, state: str
) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, state, with_plist=False, with_pointer=state == "pointer_written")
    launchd = Launchd()
    launchd.statuses["print"] = 0

    result = run_apply(machine, launchd, sha=sha)

    assert (result["ok"], result["reason"]) == (False, "old_engine_back")
    assert (app_engine.read_pointer() is None) == (state == "plist_removed")


def test_apply_on_a_healthy_journal_succeeds_with_its_state(machine: Machine) -> None:
    machine.write_plist()
    sha = _plist_sha(machine)
    seed_takeover(machine, "healthy", with_plist=False, with_pointer=True)

    result = run_apply(machine, Launchd(), sha=sha)

    assert result == {"ok": True, "id": TAKEOVER_ID, "state": "healthy"}


def test_rollback_tells_the_old_engine_lock_from_the_bundled_one(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)
    launchd.statuses["print"] = 0  # the old job is loaded again and holds the lock
    HELD_LOCKS.append(hold_server_lock(machine))

    # The ready record names the bundled engine's pid: that is the engine to stop.
    app_engine.write_ready_record(
        pid=5, port=8443, startup_id="old", workspace=machine.workspace, bootstrap=False
    )
    bundled = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert (bundled["ok"], bundled["reason"]) == (False, "bundled_engine_running")

    # With the ready record naming another pid, pid 5 is the old engine's own.
    app_engine.write_ready_record(
        pid=99, port=8443, startup_id="new", workspace=machine.workspace, bootstrap=False
    )
    old = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert old["ok"] is True, old


@pytest.mark.parametrize(
    "variant",
    ["another_engine_answers", "owner_pid_differs", "job_without_pid", "other_startup"],
)
def test_rollback_needs_the_restored_job_to_be_the_listener_and_lock_owner(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    launchd = Launchd()
    old_engine_answers(
        monkeypatch,
        machine,
        launchd,
        pid=5,
        job_pid=6 if variant == "another_engine_answers" else 5,
        owner_pid=8 if variant == "owner_pid_differs" else None,
    )
    if variant == "job_without_pid":
        launchd.print_stdout = ""
    if variant == "other_startup":
        monkeypatch.setattr(
            mac_takeover,
            "engine_identity",
            lambda port: {"startup_id": "someone-else", "pid": 5, "version": "1.10.0"},
        )

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(100.0), sleep=no_sleep
    )

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "old_engine_did_not_start",
        "pointer_written",
    )
    assert app_engine.read_pointer() is not None


def test_rollback_refuses_when_the_pointer_cannot_be_cleared(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    pointer = mac_takeover.state_dir() / app_engine.WORKSPACE_POINTER_NAME
    pointer.chmod(0o644)  # no longer a private record: clear_pointer cannot read it
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "pointer_not_cleared",
        "pointer_written",
    )
    assert pointer.exists()
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"

    # An already-cleared pointer (a crash between the clear and the journal) is fine.
    pointer.unlink()
    again = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )
    assert again["ok"] is True, again


def test_rollback_refuses_after_the_trash_list_was_handed_out(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    offered = mac_takeover.finish(takeover_id=takeover_id)
    assert offered["ok"] is True and offered["trash"]
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["trash_offered"] is True
    release_held_locks_now()
    launchd = Launchd()
    old_engine_answers(monkeypatch, machine, launchd)

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(), sleep=no_sleep
    )

    assert (result["ok"], result["reason"], result["state"]) == (False, "trash_offered", "healthy")
    assert launchd.destructive() == []
    assert not _plist_path(machine).exists()
    assert app_engine.read_pointer() is not None


def test_the_trash_mark_is_journalled_before_the_list_leaves(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    marks: list[Any] = []
    real = mac_takeover._finish_trash

    def spy(journal: dict[str, Any]) -> list[dict[str, str]]:
        found = real(journal)
        current = json.loads(mac_takeover._journal_path(takeover_id).read_text())
        marks.append(current.get("trash_offered"))  # not yet written when the list is built
        return found

    monkeypatch.setattr(mac_takeover, "_finish_trash", spy)
    mac_takeover.finish(takeover_id=takeover_id)
    final = json.loads(mac_takeover._journal_path(takeover_id).read_text())
    assert marks == [None] and final["trash_offered"] is True


def test_a_finish_with_nothing_to_trash_leaves_rollback_open(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    monkeypatch.setattr(mac_takeover, "_finish_trash", lambda journal: [])
    assert mac_takeover.finish(takeover_id=takeover_id)["trash"] == []
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and "trash_offered" not in journal


def test_an_oserror_writing_the_pointer_is_answered_with_id_and_state(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(app_engine, "write_pointer", boom)

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "takeover_error",
        "plist_removed",
    )
    assert result["id"] and "No space" in result["message"]
    assert not _plist_path(machine).exists()


def test_an_oserror_advancing_the_journal_is_answered_with_id_and_state(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = answer_engine(monkeypatch, machine)
    launchd = Launchd()
    launchd.on_verb["bootout"] = lambda: stopped.__setitem__("stopped", True)
    real = mac_takeover._advance

    def advance(entry: Path, expected: str, to: str, **extra: Any) -> None:
        if to == "plist_removed":
            raise OSError(5, "I/O error")
        real(entry, expected, to, **extra)

    monkeypatch.setattr(mac_takeover, "_advance", advance)

    result = run_apply(machine, launchd)

    assert (result["reason"], result["state"]) == ("takeover_error", "stopping")
    assert result["id"]


def test_the_cli_names_the_takeover_when_finish_or_rollback_errors(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(mac_takeover, "_health_problem", boom)
    code = cli.main(["mac-takeover", "finish", "--json", "--id", takeover_id])
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert (payload["id"], payload["state"]) == (takeover_id, "pointer_written")


def test_a_journal_too_large_to_read_back_is_refused_before_any_stop(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer_engine(monkeypatch, machine)
    monkeypatch.setattr(app_engine, "_MAX_RECORD_BYTES", 4096 + 1024)
    launchd = Launchd()

    result = run_apply(machine, launchd)

    assert (result["ok"], result["reason"]) == (False, "journal_too_large")
    assert launchd.destructive() == []
    assert _plist_path(machine).exists()
    root = mac_takeover.state_dir() / "takeover"
    assert all(entry.name.startswith(".") for entry in root.iterdir())


def test_the_journal_writer_itself_refuses_past_the_readers_limit(machine: Machine) -> None:
    entry = mac_takeover.state_dir() / "takeover" / TAKEOVER_ID
    entry.mkdir(parents=True)
    huge = {"state": "started", "blob": "x" * app_engine._MAX_RECORD_BYTES}
    with pytest.raises(mac_takeover._JournalTooLarge):
        mac_takeover._write_journal(entry, huge)
    assert list(entry.iterdir()) == []


def test_a_healthy_engine_of_another_version_is_not_healthy(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    monkeypatch.setattr(
        mac_takeover,
        "engine_identity",
        lambda port: {"startup_id": "abc", "pid": 77, "version": "0.0.1"},
    )

    result = mac_takeover.finish(takeover_id=takeover_id)

    assert (result["ok"], result["reason"], result["check"]) == (False, "not_healthy", "version")


@pytest.mark.parametrize("change", ["other_pid", "gone", "other_owner"])
def test_health_must_be_stable_across_two_reads(
    machine: Machine, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    healthy_engine(monkeypatch, machine)
    base = {"startup_id": "abc", "pid": 77, "version": ciao.__version__}
    owner = {
        "pid": 77,
        "workspace": str(machine.workspace),
        "runtime": str(machine.workspace / ".runtime"),
        "port": 8443,
        "status": "running",
        "startup_id": "abc",
    }
    pauses: list[float] = []

    def pause(seconds: float) -> None:
        pauses.append(seconds)
        if change == "gone":
            monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: None)
        elif change == "other_owner":
            monkeypatch.setattr(
                mac_takeover, "read_lock_owner", lambda runtime: {**owner, "startup_id": "zzz"}
            )
        else:
            monkeypatch.setattr(mac_takeover, "_pid_alive", lambda pid: True)
            monkeypatch.setattr(
                mac_takeover, "engine_identity", lambda port: {**base, "startup_id": "abc2"}
            )

    result = mac_takeover.finish(takeover_id=takeover_id, sleep=pause)

    assert len(pauses) == 1
    assert (result["ok"], result["reason"]) == (False, "not_healthy")
    assert result["check"] in ("unstable", "listener")
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "pointer_written"


def test_a_helper_that_builds_its_own_runner_hits_the_guard(machine: Machine) -> None:
    from tests.conftest import RealRunnerReached

    with pytest.raises(RealRunnerReached):
        mac_takeover._default_runner()(["/bin/launchctl", "print", "x"])
    # The public entry points and helpers fall back to the same factory.
    with pytest.raises(RealRunnerReached):
        mac_takeover._job_state(LABEL, mac_takeover._default_runner())
