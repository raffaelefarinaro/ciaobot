"""The account the engine runs as.

``user_key`` is a short, stable string that tells local accounts apart in a
path shared between them (per-user lock directories under the system temp
root). POSIX uses the numeric uid, as the call sites did before this module
existed. Windows has no uid; it uses the account's SID string
(``S-1-5-21-…``), which is unique on the machine and survives a rename,
unlike ``%USERNAME%``.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import Any

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1

    def _declare(dll: Any, name: str, restype: Any, *argtypes: Any) -> Any:
        func = getattr(dll, name)
        func.argtypes = list(argtypes)
        func.restype = restype
        return func

    _GetCurrentProcess = _declare(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
    _CloseHandle = _declare(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _LocalFree = _declare(_kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
    _OpenProcessToken = _declare(
        _advapi32, "OpenProcessToken", wintypes.BOOL,
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    )
    _GetTokenInformation = _declare(
        _advapi32, "GetTokenInformation", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _GetLengthSid = _declare(_advapi32, "GetLengthSid", wintypes.DWORD, ctypes.c_void_p)
    _ConvertSidToStringSidW = _declare(
        _advapi32, "ConvertSidToStringSidW", wintypes.BOOL,
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    )

    def _check(ok: object) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def current_user_sid() -> bytes:
        """The binary SID of the account this process runs as."""
        token = wintypes.HANDLE()
        _check(_OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)))
        try:
            needed = wintypes.DWORD()
            _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            _check(_GetTokenInformation(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)))
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID.
            sid = ctypes.c_void_p.from_buffer(buffer).value
            if not sid:
                raise OSError("the process token names no user SID")
            return ctypes.string_at(sid, _GetLengthSid(sid))
        finally:
            _CloseHandle(token)

    def user_key() -> str:
        """The account's SID string; see the module docstring."""
        sid = ctypes.create_string_buffer(current_user_sid())
        text = wintypes.LPWSTR()
        _check(_ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return str(text.value)
        finally:
            _LocalFree(text)

else:

    def user_key() -> str:
        """The numeric uid; see the module docstring."""
        return str(os.getuid())
