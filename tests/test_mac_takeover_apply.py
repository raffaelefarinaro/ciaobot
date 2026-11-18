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

from ciao import app_engine, cli, mac_takeover
from ciao.macos_service import LAUNCHCTL
from tests.test_mac_takeover_plan import LABEL, Machine, installer_install, machine  # noqa: F401

TAKEOVER_ID = "20261118T100000Z"


@pytest.fixture(autouse=True)
def never_the_real_launchctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that forgets to inject its runner must fail, not drive launchd.

    The runners are keyword-only defaults bound at import time, so patching
    ``subprocess.run`` would not reach them.
    """

    def refuse(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"a test reached the real subprocess runner: {args}")

    for function in (mac_takeover.plan, mac_takeover.apply, mac_takeover.finish, mac_takeover.rollback):
        defaults = function.__kwdefaults__
        if defaults is not None and "runner" in defaults:
            monkeypatch.setitem(defaults, "runner", refuse)


class Launchd:
    """A fake launchd: records every argv, answers per verb, hooks verbs.

    ``on_verb`` runs inside the runner call — the moment a test asserts on the
    state of the disk while the takeover is mid-step.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.statuses: dict[str, int] = {"print": 113}
        self.on_verb: dict[str, Callable[[], None]] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        verb = argv[1] if len(argv) > 1 else ""
        self.calls.append(list(argv))
        hook = self.on_verb.get(verb)
        if hook is not None:
            hook()
        return subprocess.CompletedProcess(argv, self.statuses.get(verb, 0), "", "")

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
    entries = sorted(entry for entry in (mac_takeover.state_dir() / "takeover").iterdir())
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
    }
    (entry / mac_takeover.JOURNAL_NAME).write_text(json.dumps(journal, indent=2))
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
    identity = {"startup_id": startup_id, "pid": pid, "version": "1.10.0"}
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
    assert seen == ["started", "stopping", "plist_removed", "pointer_written"]
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
    assert not root.exists() or not any(root.iterdir())


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
    assert not root.exists() or not any(root.iterdir())


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
    launchd.statuses["print"] = 0  # even a "loaded" launchd answer is not consulted

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
    identity = {"startup_id": "old", "pid": 5, "version": "1.10.0"}
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: dict(identity))

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
    identity = {"startup_id": "old", "pid": 5, "version": "1.10.0"}
    monkeypatch.setattr(mac_takeover, "engine_identity", lambda port: dict(identity))

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
    assert launchd.calls == []
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


def test_rollback_journals_rolled_back_even_when_the_old_engine_lingers(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    takeover_id = apply_to_pointer_written(machine, monkeypatch)
    # engine_identity stays None (the machine fixture default): the old engine
    # never answers within the rollback's wait.
    launchd = Launchd()

    result = mac_takeover.rollback(
        takeover_id=takeover_id, runner=launchd, clock=ticking(100.0), sleep=no_sleep
    )

    # The job is bootstrapped and the journal settled either way; what is
    # refused is the claim that the old engine is back.
    assert (result["ok"], result["reason"], result["state"]) == (
        False,
        "old_engine_did_not_start",
        "rolled_back",
    )
    journal = mac_takeover._load_journal(takeover_id)
    assert journal is not None and journal["state"] == "rolled_back"
    assert launchd.argv_of("bootstrap") == [
        LAUNCHCTL,
        "bootstrap",
        f"gui/{mac_takeover._getuid()}",
        str(_plist_path(machine)),
    ]
    assert _plist_path(machine).exists()


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
    monkeypatch.setitem(mac_takeover.apply.__kwdefaults__, "runner", launchd)

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
    monkeypatch.setitem(mac_takeover.apply.__kwdefaults__, "runner", launchd)

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
