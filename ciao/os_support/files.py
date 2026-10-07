"""Opening files by descriptor with POSIX semantics on every OS.

``open_fd`` is ``os.open`` for the call sites that need a descriptor (a file
created with an explicit mode, ``O_EXCL``, or refusing to write through a
link). POSIX calls ``os.open`` exactly as those call sites did before this
module existed. Windows differs in two ways:

- ``os.open`` opens in text mode unless ``O_BINARY`` is given. The C runtime
  then rewrites every ``\\n`` byte to ``\\r\\n`` on write, even through
  ``os.fdopen(fd, "wb")``, so an uploaded image comes out corrupted. Every
  descriptor is binary: bytes written are bytes stored, as on POSIX.
- There is no ``O_NOFOLLOW``. ``follow_symlinks=False`` opens through
  ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT``, so a link at the
  final component is opened as itself rather than followed, and then refuses
  it on the handle with ``ELOOP``, the errno POSIX gives: one step, like
  ``O_NOFOLLOW``, with no window between a check and the open. A truncating
  open is only truncated after that check. Other reparse points (OneDrive
  placeholders, dedup) are ordinary files to the user and are opened.

``create_fd`` is that ``CreateFileW`` path, and on Windows it also takes the
``SECURITY_ATTRIBUTES`` a new file is created with; ``private.open_private``
uses it to create a file owner-only in the same call, as ``O_CREAT`` with mode
``0o600`` does on POSIX.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import ctypes
    import errno
    import msvcrt
    import stat
    import time
    from ctypes import wintypes
    from typing import Any

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
    _GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _GetFileInformationByHandleEx.restype = wintypes.BOOL
    _SetEndOfFile = _kernel32.SetEndOfFile
    _SetEndOfFile.argtypes = [wintypes.HANDLE]
    _SetEndOfFile.restype = wintypes.BOOL
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    # What the C runtime's `_wopen` shares (`_SH_DENYNO`).
    _FILE_SHARE_READ_WRITE = 0x1 | 0x2
    _CREATE_NEW = 1
    _CREATE_ALWAYS = 2
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _TRUNCATE_EXISTING = 5
    _FILE_ATTRIBUTE_READONLY = 0x1
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _LINK_TAGS = frozenset({stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT})
    _ERROR_SHARING_VIOLATION = 32
    # As long as `replace_file` keeps retrying the writer's side of the race.
    _SHARING_DEADLINE_S = 2.0

    def _disposition(flags: int) -> int:
        """``CreateFileW``'s creation disposition for ``os.open`` flags, as the CRT maps them."""
        create, exclusive, truncate = flags & os.O_CREAT, flags & os.O_EXCL, flags & os.O_TRUNC
        if create and exclusive:
            return _CREATE_NEW
        if create and truncate:
            return _CREATE_ALWAYS
        if create:
            return _OPEN_ALWAYS
        if truncate:
            return _TRUNCATE_EXISTING
        return _OPEN_EXISTING

    def _error(code: int, path: str) -> OSError:
        # OSError maps the Windows code to the errno and the subclass
        # (FileExistsError, FileNotFoundError, PermissionError, ...).
        return OSError(None, ctypes.FormatError(code), path, code)

    def create_fd(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        follow_symlinks: bool = True,
        security_attributes: Any = None,
    ) -> int:
        """``os.open`` through ``CreateFileW``; see the module docstring.

        ``security_attributes`` (a ``SECURITY_ATTRIBUTES`` structure) applies
        only when this call creates the file: an existing file keeps its own
        security, exactly as an existing file keeps its mode under ``O_CREAT``.
        """
        name = os.fspath(path)
        access = {os.O_RDONLY: _GENERIC_READ, os.O_WRONLY: _GENERIC_WRITE}.get(
            flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR), _GENERIC_READ | _GENERIC_WRITE
        )
        disposition = _disposition(flags)
        truncate_after_check = False
        attributes = _FILE_ATTRIBUTE_NORMAL
        if flags & os.O_CREAT and not mode & stat.S_IWRITE:
            attributes = _FILE_ATTRIBUTE_READONLY  # what os.open does with such a mode
        if not follow_symlinks:
            attributes |= _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
            # Never truncate what might turn out to be the link itself.
            if disposition == _CREATE_ALWAYS:
                disposition, truncate_after_check = _OPEN_ALWAYS, True
            elif disposition == _TRUNCATE_EXISTING:
                disposition, truncate_after_check = _OPEN_EXISTING, True
        deadline = time.monotonic() + _SHARING_DEADLINE_S
        delay = 0.005
        while True:
            handle = _CreateFileW(
                name,
                access,
                _FILE_SHARE_READ_WRITE,
                None if security_attributes is None else ctypes.byref(security_attributes),
                disposition,
                attributes,
                None,
            )
            if handle != _INVALID_HANDLE_VALUE and handle:
                break
            code = ctypes.get_last_error()
            # The stores read without their lock and trust the writer's atomic
            # replace; while `MoveFileEx` is swapping the target in, opening it
            # fails with a sharing violation, the reader's half of the refusal
            # `replace_file` retries. POSIX never refuses here.
            if code != _ERROR_SHARING_VIOLATION or time.monotonic() >= deadline:
                raise _error(code, name)
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
        try:
            if not follow_symlinks:
                info = _FileAttributeTagInfo()
                if not _GetFileInformationByHandleEx(
                    handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)
                ):
                    raise _error(ctypes.get_last_error(), name)
                is_reparse = info.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
                if is_reparse and info.ReparseTag in _LINK_TAGS:
                    raise OSError(errno.ELOOP, "refusing to open a link", name)
                if info.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
                    raise IsADirectoryError(errno.EISDIR, "is a directory", name)
                if truncate_after_check and not _SetEndOfFile(handle):
                    raise _error(ctypes.get_last_error(), name)
            # Binary said explicitly, not left to the CRT's default mode: a
            # text-mode descriptor rewrites `\n` as `\r\n` (see the docstring).
            return msvcrt.open_osfhandle(
                handle, (flags & (os.O_APPEND | os.O_RDONLY)) | os.O_BINARY
            )
        except BaseException:
            _CloseHandle(handle)
            raise

    def open_fd(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        follow_symlinks: bool = True,
    ) -> int:
        """``os.open`` with POSIX semantics; see the module docstring."""
        if not follow_symlinks:
            return create_fd(path, flags, mode, follow_symlinks=False)
        return os.open(path, flags | os.O_BINARY, mode)

else:

    def open_fd(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        follow_symlinks: bool = True,
    ) -> int:
        """``os.open`` with POSIX semantics; see the module docstring."""
        if not follow_symlinks:
            flags |= os.O_NOFOLLOW
        return os.open(path, flags, mode)


# ── Replacing a file that another writer may be replacing too ─────────────
# POSIX rename(2) is atomic against a concurrent rename onto the same target,
# so `os.replace` is the whole story there and stays exactly that. On Windows
# MoveFileEx with MOVEFILE_REPLACE_EXISTING fails with ERROR_ACCESS_DENIED
# (PermissionError) while another process or thread is in the middle of its own
# replace of the same target; the refusal is transient, so it is retried for a
# short, bounded time and then raised. Every other error is raised at once.
_REPLACE_DEADLINE_S = 2.0

if sys.platform == "win32":

    def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        """``os.replace``, retrying the transient refusal a concurrent replace causes."""
        deadline = time.monotonic() + _REPLACE_DEADLINE_S
        delay = 0.005
        while True:
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.1)

else:

    def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        """``os.replace``: atomic on POSIX even against a concurrent replace."""
        os.replace(src, dst)
