"""Exclusive advisory file locks with ``flock`` semantics on every OS.

POSIX calls ``fcntl.flock`` exactly as the call sites did before this module
existed. Windows has no ``flock``; ``LockFileEx`` gives the same contract when
it locks a single byte far beyond any real file content:

- the lock is whole-file and advisory in effect, because no reader or writer
  ever touches that byte, so another process can still read the lock file
  (``instance_lock`` reports the owner's metadata from it);
- a second handle conflicts even inside the same process, like a second
  ``open()`` under ``flock``;
- it is released when the handle is closed.

In both implementations a non-blocking attempt on a held lock raises
:class:`BlockingIOError`, so callers keep a single ``except`` clause.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    import errno
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _UnlockFileEx.restype = wintypes.BOOL

    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_NOT_LOCKED = 158
    # The locked byte: offset 2**63 - 2, far past any file we write.
    _LOCK_OFFSET_LOW = 0xFFFFFFFE
    _LOCK_OFFSET_HIGH = 0x7FFFFFFF

    def _lock_region() -> _Overlapped:
        return _Overlapped(0, 0, _LOCK_OFFSET_LOW, _LOCK_OFFSET_HIGH, None)

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        """Take the exclusive lock on ``fd``; see the module docstring."""
        flags = _LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= _LOCKFILE_FAIL_IMMEDIATELY
        region = _lock_region()
        if _LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(region)):
            return
        code = ctypes.get_last_error()
        if code == _ERROR_LOCK_VIOLATION:
            raise BlockingIOError(errno.EAGAIN, "file lock is held by another handle")
        raise ctypes.WinError(code)

    def unlock(fd: int) -> None:
        """Release the lock on ``fd``; a no-op when it is not held, like ``flock``."""
        region = _lock_region()
        if _UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(region)):
            return
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_LOCKED:
            return
        raise ctypes.WinError(code)

else:
    import fcntl

    def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
        """Take the exclusive lock on ``fd``; see the module docstring."""
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

    def unlock(fd: int) -> None:
        """Release the lock on ``fd``; a no-op when it is not held, like ``flock``."""
        fcntl.flock(fd, fcntl.LOCK_UN)
