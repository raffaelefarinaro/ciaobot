from __future__ import annotations

import ctypes
import subprocess
from typing import Any

import pytest

from ciao.os_support import power


class FakeProcess:
    def __init__(self, *, exit_on_wait: bool = True) -> None:
        self.returncode: int | None = None
        self.exit_on_wait = exit_on_wait
        self.terminated = False
        self.killed = False
        self.waits: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if self.exit_on_wait:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired("caffeinate", timeout)


def test_keep_awake_platform_reports_only_supported_hosts(monkeypatch) -> None:
    monkeypatch.setattr(power.sys, "platform", "darwin")
    assert power.keep_awake_platform() == "macos"

    monkeypatch.setattr(power.sys, "platform", "win32")
    assert power.keep_awake_platform() == "windows"

    monkeypatch.setattr(power.sys, "platform", "linux")
    assert power.keep_awake_platform() == ""


def test_caffeinate_assertion_watches_engine_pid_and_stops_normally() -> None:
    process = FakeProcess()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        calls.append((argv, kwargs))
        return process

    assertion = power.CaffeinateAssertion(pid=1234, popen=fake_popen)  # type: ignore[arg-type]
    assertion.acquire()
    assertion.acquire()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/usr/bin/caffeinate", "-i", "-w", "1234"]
    assert "-w" in argv and argv[-1] == "1234"
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }

    assertion.release()
    assertion.release()
    assert process.terminated
    assert process.waits == [power._CAFFEINATE_STOP_TIMEOUT_S]


def test_caffeinate_assertion_kills_a_helper_that_will_not_exit() -> None:
    process = FakeProcess(exit_on_wait=False)
    calls = 0

    def fake_popen(_argv: list[str], **_kwargs: Any) -> FakeProcess:
        nonlocal calls
        calls += 1
        return process

    assertion = power.CaffeinateAssertion(pid=99, popen=fake_popen)  # type: ignore[arg-type]
    assertion.acquire()
    assertion.release()

    assert calls == 1
    assert process.terminated
    assert process.killed
    assert process.waits == [
        power._CAFFEINATE_STOP_TIMEOUT_S,
        power._CAFFEINATE_STOP_TIMEOUT_S,
    ]


class FakePowerApi:
    def __init__(self, *, set_succeeds: bool = True) -> None:
        self.set_succeeds = set_succeeds
        self.calls: list[tuple[str, object, object | None]] = []

    def create_request(self, _context: object) -> int:
        reason = ctypes.cast(
            _context, ctypes.POINTER(power._ReasonContext)
        ).contents
        self.reason = (
            reason.Version,
            reason.Flags,
            reason.Reason.SimpleReasonString,
        )
        self.calls.append(("create", _context, None))
        return 42

    def set_request(self, handle: int, request_type: int) -> bool:
        self.calls.append(("set", handle, request_type))
        return self.set_succeeds

    def clear_request(self, handle: int, request_type: int) -> bool:
        self.calls.append(("clear", handle, request_type))
        return True

    def close_handle(self, handle: int) -> bool:
        self.calls.append(("close", handle, None))
        return True


def test_windows_request_acquires_clears_and_closes_one_handle() -> None:
    api = FakePowerApi()
    assertion = power.WindowsPowerRequest(api=api)  # type: ignore[arg-type]

    assertion.acquire()
    assertion.acquire()
    assertion.release()
    assertion.release()

    assert [call[0] for call in api.calls] == ["create", "set", "clear", "close"]
    assert api.calls[1] == ("set", 42, power._POWER_REQUEST_SYSTEM_REQUIRED)
    assert api.calls[2] == ("clear", 42, power._POWER_REQUEST_SYSTEM_REQUIRED)
    assert api.reason == (
        power._POWER_REQUEST_CONTEXT_VERSION,
        power._POWER_REQUEST_CONTEXT_SIMPLE_STRING,
        "Ciaobot agents are working",
    )
    assert ctypes.sizeof(power._ReasonContext) == (
        ctypes.sizeof(ctypes.c_uint32) * 2
        + ctypes.sizeof(power._ReasonContextUnion)
    )


def test_windows_request_closes_handle_when_setting_it_fails() -> None:
    api = FakePowerApi(set_succeeds=False)
    assertion = power.WindowsPowerRequest(api=api)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="PowerSetRequest"):
        assertion.acquire()

    assert [call[0] for call in api.calls] == ["create", "set", "close"]


def test_factory_selects_supported_backends(monkeypatch) -> None:
    mac_assertion = object()
    windows_assertion = object()
    monkeypatch.setattr(power, "keep_awake_platform", lambda: "macos")
    monkeypatch.setattr(power, "CaffeinateAssertion", lambda: mac_assertion)
    assert power.create_power_assertion() is mac_assertion

    monkeypatch.setattr(power, "keep_awake_platform", lambda: "windows")
    monkeypatch.setattr(power, "WindowsPowerRequest", lambda: windows_assertion)
    assert power.create_power_assertion() is windows_assertion

    monkeypatch.setattr(power, "keep_awake_platform", lambda: "")
    assert power.create_power_assertion() is None
