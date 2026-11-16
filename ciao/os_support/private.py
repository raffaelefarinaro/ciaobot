"""Owner-only files and directories.

``make_private`` / ``make_private_dir`` restrict a path to its owner, and
``is_private`` answers whether a path is restricted that way. ``is_private_descriptor``
answers the same question for an already-open descriptor, so a caller that has
checked the open file is not re-answering it from a pathname another writer can
swap.

POSIX is the mode bits exactly as the call sites set and checked them before
this module existed: ``0o600`` for a file, ``0o700`` for a directory.

``is_private_descriptor`` and ``is_private`` answer the legacy question "are the
bits private?" They do not ask who owns the inode, so a file owned by another
account with ``0o600`` passes them. The strict verifiers
``is_private_descriptor_strict`` / ``is_private_strict`` add the ownership and
platform-ACL questions a caller needs before treating a path as its own private
state; the legacy functions keep their existing answers for the call sites that
already depend on them.

On POSIX the strict verifiers require the inode's owner to be the process's
effective uid, not just the mode. On macOS they also read the file's extended
ACL through ``acl_get_fd_np`` and refuse any allow entry whose principal is not
the effective user (deny entries are harmless and ignored). This closes the
case where a mode ``0o600`` file still has ``everyone allow read``. Reading that
ACL is a native dependency: if the macOS ACL API is unavailable the strict
verifiers raise ``OSError`` rather than answering "no extended ACL", so their
callers refuse the file instead of treating an unverified ACL as permission.
Linux POSIX ACLs (``system.posix_acl_access``) are *not* inspected; the strict
verifier there checks owner and mode only.

``is_owned_descriptor`` and ``descriptor_acl_grants_another_account`` expose
those two pieces for callers that need a different mode rule (for example an
ancestor directory that must be owned and not group/other-writable but is not
itself ``0o700``).

``open_private`` opens a descriptor, creating the file private when it does
not exist yet; an existing file keeps its permissions. POSIX is ``open_fd``
with ``O_CREAT`` and mode ``0o600``, as before. ``mkstemp_private`` is
``tempfile.mkstemp``, for the temp-then-``os.replace`` writes of secrets.

``make_private`` stays for directories and for tightening a file that already
exists (the POSIX ``chmod`` repair); a new secret is created through
``open_private`` or ``mkstemp_private`` before anything is written to it.

``carry_mode`` is the temp-file-then-``os.replace`` half: it gives a temp the
permissions of the file it is about to replace. POSIX applies the caller's
mode with ``fchmod``, as before.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from typing import Any

from ciao.os_support.files import open_fd

if sys.platform == "darwin":
    import ctypes
    import errno

    _ACL_TYPE_EXTENDED = 0x100
    _ACL_FIRST_ENTRY = 0
    _ACL_NEXT_ENTRY = -1
    _ACL_EXTENDED_ALLOW = 1
    _ACL_EXTENDED_DENY = 2
    _ACL_GUID_SIZE = 16

    def _bind(libc: Any, name: str, argtypes: list[Any], restype: Any) -> Any:
        """``libc``'s ``name``, or ``OSError`` if this host has no such call.

        The error is an ``OSError`` because every caller already turns one
        into "could not inspect" rather than into a private verdict: a
        missing ACL API must fail the check closed, never answer "no ACL".
        """
        try:
            function = getattr(libc, name)
        except AttributeError as exc:
            raise OSError(
                errno.ENOSYS,
                f"the native ACL API is unavailable: libc has no {name}",
            ) from exc
        function.argtypes = argtypes
        function.restype = restype
        return function

    class _MacosAcl:
        """libc's extended-ACL calls, bound on the first strict check.

        Binding is deferred so importing this module never requires the
        macOS ACL API. A test that models Darwin on another host imports
        cleanly and replaces :func:`_acl_grants_another_account` with a
        hermetic reader (see ``tests/macos_acl_simulation.py``); on a real
        Mac the first strict check binds the real calls, and a missing call
        raises ``OSError`` so that check fails closed.
        """

        def __init__(self) -> None:
            libc = ctypes.CDLL(None, use_errno=True)
            self.get_fd_np = _bind(
                libc, "acl_get_fd_np", [ctypes.c_int, ctypes.c_int], ctypes.c_void_p
            )
            self.get_entry = _bind(
                libc,
                "acl_get_entry",
                [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                ctypes.c_int,
            )
            self.get_tag_type = _bind(
                libc,
                "acl_get_tag_type",
                [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)],
                ctypes.c_int,
            )
            self.get_qualifier = _bind(
                libc, "acl_get_qualifier", [ctypes.c_void_p], ctypes.c_void_p
            )
            self.free = _bind(libc, "acl_free", [ctypes.c_void_p], ctypes.c_int)
            self.uid_to_uuid = _bind(
                libc,
                "mbr_uid_to_uuid",
                [ctypes.c_uint, ctypes.POINTER(ctypes.c_ubyte * _ACL_GUID_SIZE)],
                ctypes.c_int,
            )

    _macos_acl: _MacosAcl | None = None

    def _macos_acl_calls() -> _MacosAcl:
        """The bound ACL calls, or ``OSError`` if this host has no ACL API."""
        global _macos_acl
        if _macos_acl is None:
            _macos_acl = _MacosAcl()
        return _macos_acl

    def _effective_user_guid(acl: _MacosAcl) -> bytes:
        guid = (ctypes.c_ubyte * _ACL_GUID_SIZE)()
        if acl.uid_to_uuid(os.geteuid(), ctypes.byref(guid)) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return bytes(guid)

    def _acl_grants_another_account(fd: int) -> bool:
        """Whether the extended ACL allows any principal but the effective user.

        A deny entry takes access away and is ignored; every allow entry
        naming another principal is a grant. An unrecognised tag is refused
        because it may grant. No extended ACL at all is the common case and
        is not an error.

        This is the native seam: a test that models macOS on a host without
        these calls replaces it with a hermetic answer instead of letting
        the host's libc decide.
        """
        acl_api = _macos_acl_calls()
        acl = acl_api.get_fd_np(fd, _ACL_TYPE_EXTENDED)
        if not acl:
            error = ctypes.get_errno()
            if error == errno.ENOENT:
                return False
            raise OSError(error, os.strerror(error))
        try:
            owner = _effective_user_guid(acl_api)
            entry = ctypes.c_void_p()
            selector = _ACL_FIRST_ENTRY
            while True:
                if acl_api.get_entry(acl, selector, ctypes.byref(entry)) != 0:
                    error = ctypes.get_errno()
                    if error == errno.EINVAL:
                        return False  # no more entries
                    raise OSError(error, os.strerror(error))
                selector = _ACL_NEXT_ENTRY
                tag = ctypes.c_int()
                if acl_api.get_tag_type(entry, ctypes.byref(tag)) != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
                if tag.value == _ACL_EXTENDED_DENY:
                    continue
                if tag.value != _ACL_EXTENDED_ALLOW:
                    return True
                qualifier = acl_api.get_qualifier(entry)
                if not qualifier:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
                try:
                    if ctypes.string_at(qualifier, _ACL_GUID_SIZE) != owner:
                        return True
                finally:
                    acl_api.free(qualifier)
        finally:
            acl_api.free(acl)

else:

    def _acl_grants_another_account(fd: int) -> bool:
        """Linux POSIX ACLs are not inspected; see the module docstring."""
        return False

def is_owned_descriptor(fd: int) -> bool:
    """Whether the already-open file is owned by the effective uid."""
    return os.fstat(fd).st_uid == os.geteuid()

def is_owned_path(path: str | os.PathLike[str]) -> bool:
    """Whether the named file is owned by the effective uid."""
    return os.lstat(path).st_uid == os.geteuid()

def descriptor_acl_grants_another_account(fd: int) -> bool:
    """Whether the descriptor's platform ACL allows anyone but this user.

    macOS reads the extended ACL. Other POSIX systems answer ``False``;
    their POSIX ACLs are not inspected (see the module docstring). On macOS
    an unavailable ACL API raises ``OSError`` instead of answering ``False``:
    callers must treat that as "could not inspect" and refuse the path.
    """
    return _acl_grants_another_account(fd)

def is_private_descriptor_strict(fd: int) -> bool:
    """Whether the open file belongs to this user and is privately accessible.

    It is ``0o600`` (or ``0o700`` for a directory), owned by the effective
    uid, and — on macOS — carries no extended ACL allow entry for another
    principal. Read from the descriptor, so it describes the inode the
    caller opened. An unavailable native ACL API raises ``OSError`` rather
    than passing the file as private.
    """
    st = os.fstat(fd)
    if st.st_uid != os.geteuid():
        return False
    if not is_private_descriptor(fd):
        return False
    return not _acl_grants_another_account(fd)

def is_private_strict(path: str | os.PathLike[str]) -> bool:
    """Path-bound strict variant of :func:`is_private_descriptor_strict`.

    The final component is opened with ``O_NOFOLLOW`` but a same-UID writer
    can still replace it after the check; prefer the descriptor form for
    anything that is then read or written.

    The open is non-blocking where the platform has it. A FIFO whose mode
    looks private is still not a file the caller can read as state, but
    opening it read-only without ``O_NONBLOCK`` waits for a writer forever;
    the mode/owner answer for the inode is unchanged, and a caller that
    needs a regular file checks that itself.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if stat.S_ISDIR(os.lstat(path).st_mode):
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        return is_private_descriptor_strict(descriptor)
    finally:
        os.close(descriptor)

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

def is_private_descriptor(fd: int) -> bool:
    """Whether the already-open file is ``0o600`` (or ``0o700`` if a directory).

    Read from the descriptor, not the pathname, so a path swapped after the
    caller's checks cannot be what is judged.
    """
    st = os.fstat(fd)
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
