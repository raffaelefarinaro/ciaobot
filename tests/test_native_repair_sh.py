"""Shell tests for `install.sh --native-repair` (#2485).

Repair reuses the native-install shell half: the same verified uv bootstrap, the
same signed release, the same events descriptor. Its preflight is read-only and
answers "is there an installed engine to repair", and the handover goes to
`ciao.native_repair`. Every test runs the real script against the fake tools and
the fake coordinator from `tests.test_native_install_sh`, a fake `$HOME`, and no
network. Nothing touches the real launchd, the operator's home or `~/.local`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from ciao import native_installer as native
from ciao import native_repair as repair
from tests.test_engine_installer import SCRIPT_TEXT, VERSION, runs_the_sh_installer, needs_local_tools
from tests.test_native_installer import RUN_ID
from tests.test_native_install_sh import (
    LEGACY_USAGE,
    _FAKE_COORDINATOR,
    _VENV_PYTHON,
    _PROBE,
    _argv_record,
    _events,
    _help,
    _line,
    _native_harness,
    _read,
    _run,
    _snapshot,
    _trace,
    _write,
    OTHER_RUN_ID,
)

pytestmark = [runs_the_sh_installer, needs_local_tools]

# The coordinator's success line, on the port the repair fake reports.
REPAIR_ORIGIN = "http://127.0.0.1:9123"

# Lines the install-mode fake writes that repair must never emit.
_WIZARD_LINES = (
    'emit({"type": "phase", "phase": "wizard"})\n',
    'emit({"type": "wizard_ready", "origin": "http://127.0.0.1:54321"})\n',
    'emit({"type": "setup_saved"})\n',
)


def _repair_harness(tmp_path: Path, *, installed: bool = True) -> dict[str, Any]:
    """The native harness, pointed at the repair coordinator.

    The coordinator's venv python must run `ciao.native_repair` where install mode
    runs `ciao.native_installer`, and the fake coordinator must not write the
    wizard events. When `installed`, the one workspace repair reads is planted.
    """
    h = _native_harness(tmp_path)
    venv_python = tmp_path / "venv-python.sh"
    venv_python.write_text(
        _VENV_PYTHON.replace("ciao.native_installer", "ciao.native_repair")
        .replace("__PROBE__", _PROBE)
        .replace("__SYS_PY__", sys.executable)
        .replace("__FAKE_COORD__", str(tmp_path / "fake_coordinator.py"))
        .replace("__REPO__", str(Path(__file__).parents[1])),
        encoding="utf-8",
    )
    coordinator = _FAKE_COORDINATOR
    for line in _WIZARD_LINES:
        assert coordinator.count(line) == 1, line
        coordinator = coordinator.replace(line, "")
    assert coordinator.count('"http://127.0.0.1:8443"') == 1
    coordinator = coordinator.replace('"http://127.0.0.1:8443"', f'"{REPAIR_ORIGIN}"')
    (tmp_path / "fake_coordinator.py").write_text(coordinator, encoding="utf-8")
    if installed:
        _write(h["home"] / "Ciaobot/.runtime/workspaces.json", "[]\n")
    return h


def _repair_argv(fd: int) -> list[str]:
    return ["--version", VERSION, "--native-repair", "--events-fd", str(fd), "--run-id", RUN_ID]


def _terminal(payload: bytes) -> list[dict[str, Any]]:
    return [e for e in _events(payload) if e["type"] in native.TERMINAL_TYPES]


# --- the success path ----------------------------------------------------------


def test_repair_streams_shell_phases_then_hands_over_to_the_repair_coordinator(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    completed, payload = _run(h, _repair_argv)
    assert completed.returncode == 0, completed.stderr

    # Seq is contiguous, every line stays under the 1024-byte bound, and at most 32 events.
    raw_lines = payload.splitlines(keepends=True)
    assert all(len(line) <= 1024 for line in raw_lines)
    events = _events(payload)
    assert len(events) <= 32
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs)) == list(range(1, len(events) + 1))

    assert raw_lines[:3] == [
        _line(1, {"type": "phase", "phase": "preflight"}),
        _line(2, {"type": "phase", "phase": "bootstrap"}),
        _line(3, {"type": "phase", "phase": "download"}),
    ]
    coordinator = events[3:]
    assert [e["seq"] for e in coordinator] == list(range(4, 8))
    assert [e.get("phase") for e in coordinator[:3]] == ["install", "activation", "readiness"]
    assert coordinator[-1] == {
        "schema": 1,
        "run_id": RUN_ID,
        "seq": 7,
        "type": "service_ready",
        "origin": REPAIR_ORIGIN,
    }
    assert not [e for e in events if e["type"] in {"wizard_ready", "setup_saved"}]
    assert not [e for e in events if e.get("phase") == "wizard"]

    argv = _argv_record(h)
    assert argv is not None
    assert argv[0] == "run"
    pairs = dict(zip(argv[1::2], argv[2::2]))
    assert pairs["--first-seq"] == "4"
    assert pairs["--prior-events"] == "3"
    assert pairs["--run-id"] == RUN_ID
    assert pairs["--version"] == VERSION
    assert "--workspace" not in argv
    assert any(line.startswith("coordinator ") for line in _read(h["home"], "fd.log").splitlines())


# --- refusals that name the flag or the state ---------------------------------


def test_repair_refuses_the_workspace_flag(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    workspace = h["home"] / "Ciaobot"
    completed, payload = _run(h, _repair_argv, extra_args=("--workspace", str(workspace)))
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "invalid_arguments"
    assert b"--workspace cannot be combined with --native-repair" in completed.stderr
    assert "curl" not in _trace(h)


def test_nothing_installed_is_not_installed_and_changes_nothing(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path, installed=False)
    before = _snapshot(h["home"])
    completed, payload = _run(h, _repair_argv)
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "not_installed"
    assert "curl" not in _trace(h)
    assert _snapshot(h["home"]) == before


_UNKNOWN_REPAIR_CASES: dict[str, Any] = {
    "plist-dangling": ("plist", None),
    "workspace-redirect": ("env", {"CIAO_WORKSPACE": "{home}/redirected"}),
    "state-file": ("state-file", None),
    "env-fifo": ("env-fifo", None),
}


@pytest.mark.parametrize("case", sorted(_UNKNOWN_REPAIR_CASES))
def test_repair_unknown_state_changes_nothing(tmp_path: Path, case: str) -> None:
    h = _repair_harness(tmp_path, installed=False)
    kind, env = _UNKNOWN_REPAIR_CASES[case]
    if kind == "plist":
        path = h["home"] / "Library/LaunchAgents/com.ciao.server.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(h["home"] / "nowhere.plist")
    elif kind == "state-file":
        _write(h["home"] / ".local/state/ciaobot")
    elif kind == "env-fifo":
        fifo = h["home"] / "Ciaobot/.env"
        fifo.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(fifo)
    before = _snapshot(h["home"])
    if env is not None:
        env = {name: value.format(home=h["home"]) for name, value in env.items()}
    completed, payload = _run(h, _repair_argv, env=env)
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "unknown_state"
    assert "curl" not in _trace(h)
    assert _snapshot(h["home"]) == before


def test_repair_refuses_a_held_lock_and_keeps_it(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    lock = h["home"] / ".local/state/ciaobot/native-install.lock"
    lock.mkdir(parents=True)
    record = lock / "run.json"
    record.write_text(
        '{"schema":1,"run_id":"%s","pid":999999}' % OTHER_RUN_ID,
        encoding="utf-8",
    )
    before = record.read_bytes()
    completed, payload = _run(h, _repair_argv)
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "install_in_progress"
    assert b"never removed automatically" in completed.stderr
    assert record.read_bytes() == before
    assert "curl" not in _trace(h)


# --- installed evidence proceeds ----------------------------------------------


def _plant_plist(h: dict[str, Any]) -> None:
    _write(h["home"] / "Library/LaunchAgents/com.ciao.server.plist")


def _plant_loaded_job(h: dict[str, Any]) -> None:
    _write(h["home"] / "loaded-com.ciao.server", "")


def _plant_legacy_env(h: dict[str, Any]) -> None:
    _write(h["home"] / "Ciaobot/.env", "CIAO_WORKSPACES=x\n")


@pytest.mark.parametrize("plant", [_plant_plist, _plant_loaded_job, _plant_legacy_env])
def test_installed_evidence_variants_proceed(tmp_path: Path, plant: Any) -> None:
    h = _repair_harness(tmp_path, installed=False)
    plant(h)
    completed, payload = _run(h, _repair_argv)
    assert completed.returncode == 0, completed.stderr
    assert _events(payload)[-1]["type"] == "service_ready"
    assert _argv_record(h) is not None


# --- argument validation ------------------------------------------------------


def test_both_native_flags_are_invalid_arguments(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    completed, payload = _run(h, lambda fd: _repair_argv(fd) + ["--native-install"])
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events] == ["phase", "failed"]
    assert events[1]["code"] == "invalid_arguments"
    assert b"--native-install and --native-repair are mutually exclusive" in completed.stderr
    assert "curl" not in _trace(h)


@pytest.mark.parametrize("flag", ["--migrate", "--as-host", "--no-start"])
def test_repair_refuses_legacy_flags(tmp_path: Path, flag: str) -> None:
    h = _repair_harness(tmp_path)
    completed, payload = _run(h, lambda fd: _repair_argv(fd) + [flag])
    assert completed.returncode != 0
    events = _events(payload)
    assert [e["type"] for e in events][-1] == "failed"
    assert events[-1]["code"] == "invalid_arguments"
    assert f"{flag} cannot be combined with --native-repair".encode() in completed.stderr
    assert "curl" not in _trace(h)


def test_repair_help_is_the_legacy_text(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    completed = _help(h, "--native-repair", "--help")
    assert completed.returncode == 0
    assert completed.stderr == LEGACY_USAGE


# --- the release and the signals -----------------------------------------------


def test_a_release_without_the_repair_coordinator_is_invalid_arguments(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    _write(h["home"] / "old-release", "")
    completed, payload = _run(h, _repair_argv)
    assert completed.returncode != 0
    terminal = _terminal(payload)
    assert len(terminal) == 1
    assert terminal[0]["code"] == "invalid_arguments"
    assert _events(payload)[-1] == terminal[0]
    assert b"does not support --native-repair" in completed.stderr
    assert _argv_record(h) is None


def test_a_signal_before_handover_reports_cancelled_once(tmp_path: Path) -> None:
    h = _repair_harness(tmp_path)
    _write(h["home"] / "term-on-manifest", "")
    completed, payload = _run(h, _repair_argv)
    assert completed.returncode != 0
    terminal = _terminal(payload)
    assert len(terminal) == 1
    assert terminal[0]["code"] == "cancelled"
    assert _events(payload)[-1] == terminal[0]
    assert _argv_record(h) is None


# --- the shell and the repair coordinator agree --------------------------------


def test_repair_shell_constants_match_the_coordinator() -> None:
    lines = SCRIPT_TEXT.splitlines()
    assert "NATIVE_REPAIR_PROTOCOL=1" in lines
    assert repair.NATIVE_REPAIR_PROTOCOL == 1
    assert "not_installed" in native.FAILURE_CODES
    assert "native_code=not_installed" in SCRIPT_TEXT
