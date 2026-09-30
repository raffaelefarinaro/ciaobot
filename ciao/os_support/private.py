"""Owner-only files and directories on every OS.

``make_private`` / ``make_private_dir`` restrict a path to its owner, and
``is_private`` answers whether a path is restricted that way.

POSIX is the mode bits exactly as the call sites set and checked them before
this module existed: ``0o600`` for a file, ``0o700`` for a directory.

Windows has no mode bits (``os.chmod`` only toggles the read-only flag), so
"private" is a protected DACL that grants full control to the current user
and to SYSTEM and to nobody else, with no inherited entries. A directory's
two entries are inheritable, so files created in it later are private
without being touched. The DACL is set through ``SetNamedSecurityInfoW``
rather than ``SetSecurityInfo`` on a descriptor: a CRT descriptor is opened
without ``WRITE_DAC``, and the path form opens its own handle with it.
Failing to set it raises ``OSError``: a file that cannot be made private
must not be written as if it had been.

``open_private`` opens a descriptor, creating the file private when it does
not exist yet; an existing file keeps its permissions. POSIX is ``open_fd``
with ``O_CREAT`` and mode ``0o600``, as before. Windows passes the protected
DACL to ``CreateFileW`` as the new file's security descriptor
(``files.create_fd``), so the file is private from the instant it exists:
there is no moment in which another account could open a handle to it, and
Windows checks access only when a handle is opened. ``mkstemp_private`` is
``tempfile.mkstemp`` on POSIX and the same create on Windows, for the
temp-then-``os.replace`` writes of secrets.

``make_private`` stays for directories and for tightening a file that already
exists (the POSIX ``chmod`` repair); a new secret is created through
``open_private`` or ``mkstemp_private`` before anything is written to it.

``carry_mode`` is the temp-file-then-``os.replace`` half: it gives a temp the
permissions of the file it is about to replace. POSIX applies the caller's
mode with ``fchmod``, as before. On Windows the temp already inherits the
directory's ACL, which is what the original had, unless the original was made
private; then the temp is made private too.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from typing import Any

from ciao.os_support.files import open_fd

if sys.platform == "win32":
    import ctypes
    import errno
    import secrets
    from ctypes import wintypes

    from ciao.os_support.files import create_fd

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _ACL_REVISION = 2
    _ACL_HEADER_SIZE = 8  # sizeof(ACL)
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _ACCESS_DENIED_ACE_TYPE = 1
    _INHERIT_ONLY_ACE = 0x8
    _SECURITY_DESCRIPTOR_REVISION = 1
    _SECURITY_DESCRIPTOR_MIN_LENGTH = 64  # 40 on x64; generous is harmless
    _SE_DACL_PROTECTED = 0x1000
    _OBJECT_INHERIT_ACE = 0x1
    _CONTAINER_INHERIT_ACE = 0x2
    _FILE_ALL_ACCESS = 0x1F01FF
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1
    _WIN_LOCAL_SYSTEM_SID = 22
    _SECURITY_MAX_SID_SIZE = 68

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class _AccessAllowedAce(ctypes.Structure):
        # SidStart is the first DWORD of the SID, which runs past the struct.
        _fields_ = [
            ("Header", _AceHeader),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    _PSID = ctypes.c_void_p
    _PACL = ctypes.c_void_p
    _PSECURITY_DESCRIPTOR = ctypes.c_void_p

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
    _CreateWellKnownSid = _declare(
        _advapi32, "CreateWellKnownSid", wintypes.BOOL,
        ctypes.c_int, _PSID, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
    )
    _GetLengthSid = _declare(_advapi32, "GetLengthSid", wintypes.DWORD, _PSID)
    _EqualSid = _declare(_advapi32, "EqualSid", wintypes.BOOL, _PSID, _PSID)
    _InitializeAcl = _declare(
        _advapi32, "InitializeAcl", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD
    )
    _AddAccessAllowedAceEx = _declare(
        _advapi32, "AddAccessAllowedAceEx", wintypes.BOOL,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, _PSID,
    )
    _GetAclInformation = _declare(
        _advapi32, "GetAclInformation", wintypes.BOOL,
        _PACL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int,
    )
    _GetAce = _declare(
        _advapi32, "GetAce", wintypes.BOOL, _PACL, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)
    )
    _SetNamedSecurityInfoW = _declare(
        _advapi32, "SetNamedSecurityInfoW", wintypes.DWORD,
        wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, _PSID, _PSID, _PACL, _PACL,
    )
    _GetNamedSecurityInfoW = _declare(
        _advapi32, "GetNamedSecurityInfoW", wintypes.DWORD,
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(_PSID), ctypes.POINTER(_PSID), ctypes.POINTER(_PACL),
        ctypes.POINTER(_PACL), ctypes.POINTER(_PSECURITY_DESCRIPTOR),
    )
    _InitializeSecurityDescriptor = _declare(
        _advapi32, "InitializeSecurityDescriptor", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD
    )
    _SetSecurityDescriptorDacl = _declare(
        _advapi32, "SetSecurityDescriptorDacl", wintypes.BOOL,
        ctypes.c_void_p, wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL,
    )
    _SetSecurityDescriptorControl = _declare(
        _advapi32, "SetSecurityDescriptorControl", wintypes.BOOL,
        ctypes.c_void_p, wintypes.WORD, wintypes.WORD,
    )
    _ACL_SIZE_INFORMATION_CLASS = 2

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    def _check(ok: object) -> None:
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def _address(pointer: ctypes.c_void_p) -> int:
        if not pointer.value:
            raise OSError("Windows security API returned a null pointer")
        return pointer.value

    def _current_user_sid() -> bytes:
        token = wintypes.HANDLE()
        _check(_OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)))
        try:
            needed = wintypes.DWORD()
            _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
            buffer = ctypes.create_string_buffer(needed.value)
            _check(_GetTokenInformation(token, _TOKEN_USER, buffer, needed, ctypes.byref(needed)))
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID.
            sid = _address(ctypes.c_void_p.from_buffer(buffer))
            return ctypes.string_at(sid, _GetLengthSid(sid))
        finally:
            _CloseHandle(token)

    def _system_sid() -> bytes:
        buffer = ctypes.create_string_buffer(_SECURITY_MAX_SID_SIZE)
        size = wintypes.DWORD(_SECURITY_MAX_SID_SIZE)
        _check(_CreateWellKnownSid(_WIN_LOCAL_SYSTEM_SID, None, buffer, ctypes.byref(size)))
        return buffer.raw[: size.value]

    _owner_sids: tuple[bytes, bytes] | None = None

    def _allowed_sids() -> tuple[bytes, bytes]:
        global _owner_sids
        if _owner_sids is None:
            _owner_sids = (_current_user_sid(), _system_sid())
        return _owner_sids

    def _private_acl(ace_flags: int) -> Any:
        """An ACL granting full control to the user and SYSTEM, and to nobody else."""
        sids = [ctypes.create_string_buffer(sid, len(sid)) for sid in _allowed_sids()]
        size = _ACL_HEADER_SIZE + sum(
            ctypes.sizeof(_AccessAllowedAce) - ctypes.sizeof(wintypes.DWORD) + len(sid.raw)
            for sid in sids
        )
        acl = ctypes.create_string_buffer(size)
        _check(_InitializeAcl(acl, size, _ACL_REVISION))
        for sid in sids:
            _check(_AddAccessAllowedAceEx(acl, _ACL_REVISION, ace_flags, _FILE_ALL_ACCESS, sid))
        return acl

    class _PrivateCreate:
        """``SECURITY_ATTRIBUTES`` for a new owner-only file, and the buffers it points into."""

        def __init__(self) -> None:
            self.acl = _private_acl(0)
            self.descriptor = ctypes.create_string_buffer(_SECURITY_DESCRIPTOR_MIN_LENGTH)
            _check(_InitializeSecurityDescriptor(self.descriptor, _SECURITY_DESCRIPTOR_REVISION))
            _check(_SetSecurityDescriptorDacl(self.descriptor, True, self.acl, False))
            _check(
                _SetSecurityDescriptorControl(
                    self.descriptor, _SE_DACL_PROTECTED, _SE_DACL_PROTECTED
                )
            )
            self.attributes = _SecurityAttributes(
                ctypes.sizeof(_SecurityAttributes),
                ctypes.cast(self.descriptor, ctypes.c_void_p),
                False,
            )

    def _protect(path: str | os.PathLike[str], ace_flags: int) -> None:
        acl = _private_acl(ace_flags)
        error = _SetNamedSecurityInfoW(
            os.fspath(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.cast(acl, ctypes.c_void_p),
            None,
        )
        if error:
            raise ctypes.WinError(error)

    def make_private(path: str | os.PathLike[str]) -> None:
        """Restrict a file to its owner; see the module docstring."""
        _protect(path, 0)

    def make_private_dir(path: str | os.PathLike[str]) -> None:
        """Restrict a directory, and what is created in it, to its owner."""
        _protect(path, _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)

    def is_private(path: str | os.PathLike[str]) -> bool:
        """Whether the DACL grants nothing to anyone but the user and SYSTEM.

        Only a deny entry can be passed over: every other entry type (object,
        callback and compound allows included) may grant access, so one naming
        anyone else makes the path not private. An inherit-only entry does not
        apply to a file itself, but on a directory it is what the directory's
        new children get, so there it counts too.
        """
        is_dir = os.path.isdir(path)
        dacl = _PACL()
        descriptor = _PSECURITY_DESCRIPTOR()
        error = _GetNamedSecurityInfoW(
            os.fspath(path), _SE_FILE_OBJECT, _DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if error:
            raise ctypes.WinError(error)
        try:
            if not dacl.value:
                return False  # a NULL DACL grants everyone everything
            info = _AclSizeInformation()
            _check(
                _GetAclInformation(
                    dacl, ctypes.byref(info), ctypes.sizeof(info), _ACL_SIZE_INFORMATION_CLASS
                )
            )
            allowed = [ctypes.create_string_buffer(sid, len(sid)) for sid in _allowed_sids()]
            for index in range(info.AceCount):
                ace = ctypes.c_void_p()
                _check(_GetAce(dacl, index, ctypes.byref(ace)))
                entry = _AccessAllowedAce.from_address(_address(ace))
                if entry.Header.AceType == _ACCESS_DENIED_ACE_TYPE:
                    continue  # a deny entry only takes access away
                if entry.Header.AceType != _ACCESS_ALLOWED_ACE_TYPE:
                    return False  # a type that may grant, to a principal not read here
                if entry.Header.AceFlags & _INHERIT_ONLY_ACE and not is_dir:
                    continue  # applies to children only, and a file has none
                sid = _address(ace) + _AccessAllowedAce.SidStart.offset
                if not any(_EqualSid(sid, known) for known in allowed):
                    return False
            return True
        finally:
            _LocalFree(descriptor)

    def open_private(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o600,
        *,
        follow_symlinks: bool = True,
    ) -> int:
        """``open_fd`` that creates ``path`` private; see the module docstring."""
        create = _PrivateCreate()
        return create_fd(
            path,
            flags | os.O_CREAT,
            mode,
            follow_symlinks=follow_symlinks,
            security_attributes=create.attributes,
        )

    def mkstemp_private(
        *, dir: str | os.PathLike[str], prefix: str = "tmp", suffix: str = ""
    ) -> tuple[int, str]:
        """``tempfile.mkstemp``, created private; see the module docstring."""
        create = _PrivateCreate()
        folder = os.path.abspath(dir)
        for _ in range(tempfile.TMP_MAX):
            name = os.path.join(folder, f"{prefix}{secrets.token_hex(4)}{suffix}")
            try:
                fd = create_fd(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                    follow_symlinks=False,
                    security_attributes=create.attributes,
                )
            except FileExistsError:
                continue
            return fd, name
        raise FileExistsError(errno.EEXIST, "no usable temporary file name found", folder)

    def carry_mode(
        fd: int, mode: int, *, temp: str | os.PathLike[str], original: str | os.PathLike[str]
    ) -> None:
        """Give ``temp`` the permissions of ``original``; see the module docstring."""
        if os.path.exists(original) and is_private(original):
            make_private(temp)

else:

    def make_private(path: str | os.PathLike[str]) -> None:
        """Restrict a file to its owner; see the module docstring."""
        os.chmod(path, 0o600)

    def make_private_dir(path: str | os.PathLike[str]) -> None:
        """Restrict a directory, and what is created in it, to its owner."""
        os.chmod(path, 0o700)

    def is_private(path: str | os.PathLike[str]) -> bool:
        """Whether ``path`` is ``0o600`` (a file) or ``0o700`` (a directory)."""
        st = os.stat(path)
        return stat.S_IMODE(st.st_mode) == (0o700 if stat.S_ISDIR(st.st_mode) else 0o600)

    def open_private(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o600,
        *,
        follow_symlinks: bool = True,
    ) -> int:
        """``open_fd`` that creates ``path`` private; see the module docstring."""
        return open_fd(path, flags | os.O_CREAT, mode, follow_symlinks=follow_symlinks)

    def mkstemp_private(
        *, dir: str | os.PathLike[str], prefix: str = "tmp", suffix: str = ""
    ) -> tuple[int, str]:
        """``tempfile.mkstemp``, created private; see the module docstring."""
        return tempfile.mkstemp(dir=dir, prefix=prefix, suffix=suffix)

    def carry_mode(
        fd: int, mode: int, *, temp: str | os.PathLike[str], original: str | os.PathLike[str]
    ) -> None:
        """Give ``temp`` the permissions of ``original``; see the module docstring."""
        os.fchmod(fd, mode)
