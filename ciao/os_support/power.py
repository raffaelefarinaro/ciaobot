"""Host OS power assertions used by the engine's keep-awake setting.

Only system idle sleep is prevented: display sleep and user-directed sleep
remain under the operating system's control. The macOS child watches the
engine PID so an abnormal engine exit also releases its assertion.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class PowerAssertion(Protocol):
    """One acquired/released host power assertion."""

    def acquire(self) -> None: ...

    def release(self) -> None: ...


CAFFEINATE_PATH = "/usr/bin/caffeinate"
_CAFFEINATE_STOP_TIMEOUT_S = 1.0
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_SYSTEM_REQUIRED = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def keep_awake_platform() -> str:
    """Return the supported engine-host platform, or an empty string."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return ""


class CaffeinateAssertion:
    """Prevent macOS idle sleep until released or the engine PID exits."""

    def __init__(
        self,
        *,
        pid: int | None = None,
        popen: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._pid = os.getpid() if pid is None else pid
        self._popen = subprocess.Popen if popen is None else popen
        self._process: subprocess.Popen[bytes] | None = None

    def acquire(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            return
        self._process = self._popen(
            [CAFFEINATE_PATH, "-i", "-w", str(self._pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def release(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=_CAFFEINATE_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_CAFFEINATE_STOP_TIMEOUT_S)


class _ReasonContextDetailed(ctypes.Structure):
    """The detailed union arm, required to size REASON_CONTEXT correctly."""

    _fields_ = [
        ("LocalizedReasonModule", ctypes.c_void_p),
        ("LocalizedReasonId", ctypes.c_uint32),
        ("ReasonStringCount", ctypes.c_uint32),
        ("ReasonStrings", ctypes.POINTER(ctypes.c_wchar_p)),
    ]


class _ReasonContextUnion(ctypes.Union):
    _fields_ = [
        ("Detailed", _ReasonContextDetailed),
        ("SimpleReasonString", ctypes.c_wchar_p),
    ]


class _ReasonContext(ctypes.Structure):
    """Win32 REASON_CONTEXT, with the simple-string reason selected."""

    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("Flags", ctypes.c_uint32),
        ("Reason", _ReasonContextUnion),
    ]


@dataclass(slots=True)
class _WindowsPowerApi:
    create_request: Callable[..., Any]
    set_request: Callable[..., Any]
    clear_request: Callable[..., Any]
    close_handle: Callable[..., Any]


def _windows_power_api() -> _WindowsPowerApi:
    if sys.platform != "win32":
        raise OSError("Windows power requests are only available on Windows")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def declare(name: str, restype: Any, *argtypes: Any) -> Any:
        function = getattr(kernel32, name)
        function.argtypes = list(argtypes)
        function.restype = restype
        return function

    return _WindowsPowerApi(
        create_request=declare(
            "PowerCreateRequest", wintypes.HANDLE, ctypes.POINTER(_ReasonContext)
        ),
        set_request=declare(
            "PowerSetRequest", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int
        ),
        clear_request=declare(
            "PowerClearRequest", wintypes.BOOL, wintypes.HANDLE, ctypes.c_int
        ),
        close_handle=declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE),
    )


class WindowsPowerRequest:
    """Hold a Windows system-required request in the engine process."""

    def __init__(self, *, api: _WindowsPowerApi | None = None) -> None:
        self._api = _windows_power_api() if api is None else api
        self._handle: Any | None = None
        self._reason: _ReasonContext | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        reason = _ReasonContext()
        reason.Version = _POWER_REQUEST_CONTEXT_VERSION
        reason.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
        reason.Reason.SimpleReasonString = "Ciaobot agents are working"
        handle = self._api.create_request(ctypes.byref(reason))
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            raise OSError("PowerCreateRequest failed")
        if not self._api.set_request(handle, _POWER_REQUEST_SYSTEM_REQUIRED):
            self._api.close_handle(handle)
            raise OSError("PowerSetRequest failed")
        self._reason = reason
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if not self._api.clear_request(handle, _POWER_REQUEST_SYSTEM_REQUIRED):
                raise OSError("PowerClearRequest failed")
        finally:
            # Closing the handle also drops the request if clearing it fails,
            # and Windows closes it automatically if the engine exits.
            self._api.close_handle(handle)
            self._reason = None


def create_power_assertion() -> PowerAssertion | None:
    """Build the OS assertion backend for this engine host, if supported."""
    platform = keep_awake_platform()
    if platform == "macos":
        return CaffeinateAssertion()
    if platform == "windows":
        return WindowsPowerRequest()
    return None
