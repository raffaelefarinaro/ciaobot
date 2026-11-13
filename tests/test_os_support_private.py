"""ciao.os_support.private: owner-only files on POSIX (mode bits) and Windows (DACL).

The behavioural tests run on every OS the CI matrix covers and assert through
``is_private``. The platform tests pin each branch: POSIX to the exact modes
and calls the call sites made before the module existed, Windows to a DACL
that ``icacls`` reports as the user and SYSTEM only.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ciao.os_support import private
from ciao.os_support.files import open_fd
from ciao.os_support.private import (
    carry_mode,
    is_private,
    make_private,
    make_private_dir,
    mkstemp_private,
    open_private,
)


def _open_private_new(folder: Path) -> tuple[int, Path]:
    path = folder / "created"
    return open_private(path, os.O_WRONLY | os.O_TRUNC), path


def _open_fd_no_follow(folder: Path) -> tuple[int, Path]:
    path = folder / "created"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    return open_fd(path, flags, 0o600, follow_symlinks=False), path


def _mkstemp(folder: Path) -> tuple[int, Path]:
    fd, name = mkstemp_private(dir=folder, prefix=".t.", suffix=".tmp")
    return fd, Path(name)


_CREATORS = [_open_private_new, _mkstemp, _open_fd_no_follow]


@pytest.mark.parametrize("create", _CREATORS)
def test_every_creator_stores_bytes_exactly(tmp_path: Path, create) -> None:  # type: ignore[no-untyped-def]
    """No descriptor these return may be in text mode, which rewrites `\\n` as `\\r\\n`."""
    fd, path = create(tmp_path)
    with os.fdopen(fd, "wb") as out:
        out.write(b"a\nb")
    with open(path, "rb") as check:
        assert check.read() == b"a\nb"


@pytest.mark.parametrize("create", _CREATORS)
def test_every_creator_stores_text_exactly_with_newline_unset(
    tmp_path: Path, create
) -> None:  # type: ignore[no-untyped-def]
    """The call sites wrap the descriptor as text; `newline=""` must mean no rewriting."""
    fd, path = create(tmp_path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
        out.write("a\nb\r\nc")
    with open(path, "rb") as check:
        assert check.read() == b"a\nb\r\nc"


def test_a_fresh_file_is_not_private_until_it_is_made_so(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    assert not is_private(path)
    make_private(path)
    assert is_private(path)
    assert path.read_text(encoding="utf-8") == "token"


def test_a_private_directory_makes_what_is_created_in_it_private(tmp_path: Path) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir()
    assert not is_private(folder)
    make_private_dir(folder)
    assert is_private(folder)
    child = folder / "client_secret.json"
    fd = open_private(child, os.O_WRONLY)
    os.close(fd)
    assert is_private(child)


def test_a_descriptor_answers_for_the_opened_file(tmp_path: Path) -> None:
    """The descriptor-bound sibling of ``is_private`` follows the same rule."""
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert private.is_private_descriptor(fd) is False
    finally:
        os.close(fd)
    make_private(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        assert private.is_private_descriptor(fd) is True
        assert private.is_private_descriptor_strict(fd) is True
        assert private.is_owned_descriptor(fd) is True
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name != "posix", reason="uid ownership is POSIX")
def test_the_strict_descriptor_answer_requires_the_effective_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mode-0600 file owned by someone else is private to *them*, not to us."""
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    make_private(path)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        assert private.is_private_descriptor(descriptor) is True
        real_geteuid = os.geteuid
        monkeypatch.setattr(
            private.os, "geteuid", lambda: real_geteuid() + 1
        )
        assert private.is_private_descriptor(descriptor) is True, (
            "the legacy mode-only answer must not change"
        )
        assert private.is_private_descriptor_strict(descriptor) is False
        assert private.is_owned_descriptor(descriptor) is False
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_the_strict_descriptor_answer_refuses_a_granting_acl(tmp_path: Path) -> None:
    """Mode 0600 does not stop an extended ACL from allowing everyone to read."""
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    make_private(path)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(path)],
        capture_output=True,
        check=True,
    )
    descriptor = os.open(path, os.O_RDONLY)
    try:
        assert private.is_private_descriptor(descriptor) is True
        assert private.is_private_descriptor_strict(descriptor) is False
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_the_strict_descriptor_answer_ignores_a_deny_only_acl(tmp_path: Path) -> None:
    """macOS home directories carry a harmless ``everyone deny delete`` entry."""
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    make_private(path)
    subprocess.run(
        ["chmod", "+a", "everyone deny delete", str(path)],
        capture_output=True,
        check=True,
    )
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            assert private.is_private_descriptor_strict(descriptor) is True
        finally:
            os.close(descriptor)
    finally:
        subprocess.run(
            ["chmod", "-a", "everyone deny delete", str(path)],
            capture_output=True,
            check=False,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX path-vs-inode semantics")
def test_a_descriptor_is_not_fooled_by_the_path_being_replaced(tmp_path: Path) -> None:
    """A path check after the open can describe a different inode than the fd."""
    opened = tmp_path / "opened.txt"
    opened.write_text("kept", encoding="utf-8")
    make_private(opened)
    descriptor = os.open(opened, os.O_RDONLY)
    try:
        replacement = tmp_path / "replacement.txt"
        replacement.write_text("loose", encoding="utf-8")
        replacement.chmod(0o644)
        os.replace(replacement, opened)
        assert not is_private(opened)
        assert private.is_private_descriptor(descriptor)
        assert os.read(descriptor, 4) == b"kept"
    finally:
        os.close(descriptor)


def _returns_promptly(seconds: float, call):
    """Run ``call`` in a daemon thread; fail rather than hang the suite on a FIFO."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the strict path check did not return promptly"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFOs")
def test_the_strict_path_answer_does_not_block_on_a_fifo(tmp_path: Path) -> None:
    """A private FIFO is answered without waiting for a writer forever.

    The mode/owner/ACL question is unchanged; the open is merely non-blocking.
    A caller that needs a regular file still checks that itself.
    """
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo, 0o600)
    assert stat.S_IMODE(fifo.stat().st_mode) == 0o600
    assert _returns_promptly(5.0, lambda: private.is_private_strict(fifo)) is True


def test_open_private_creates_private_and_leaves_an_existing_file_alone(tmp_path: Path) -> None:
    created = tmp_path / "journal.jsonl"
    fd = open_private(created, os.O_WRONLY | os.O_APPEND)
    os.write(fd, b"row\n")
    os.close(fd)
    assert is_private(created)

    shared = tmp_path / "shared.jsonl"
    shared.write_bytes(b"old\n")
    assert not is_private(shared)
    fd = open_private(shared, os.O_WRONLY | os.O_APPEND)
    os.write(fd, b"new\n")
    os.close(fd)
    assert not is_private(shared)
    assert shared.read_bytes().endswith(b"new\n")


def test_a_new_file_is_private_before_anything_is_written(tmp_path: Path) -> None:
    """No gap: the file is owner-only the moment `open_private` returns."""
    path = tmp_path / ".env"
    fd = open_private(path, os.O_WRONLY | os.O_TRUNC)
    try:
        assert path.stat().st_size == 0
        assert is_private(path)
    finally:
        os.close(fd)


def test_mkstemp_private_creates_a_unique_private_temp(tmp_path: Path) -> None:
    first_fd, first = mkstemp_private(dir=tmp_path, prefix=".receipt.", suffix=".tmp")
    second_fd, second = mkstemp_private(dir=tmp_path, prefix=".receipt.", suffix=".tmp")
    try:
        assert first != second
        for name in (first, second):
            assert os.path.isabs(name)
            assert Path(name).parent == tmp_path
            assert Path(name).name.startswith(".receipt.") and name.endswith(".tmp")
            assert is_private(name)
        os.write(first_fd, b"a\nb\r\n")
    finally:
        os.close(first_fd)
        os.close(second_fd)
    assert Path(first).read_bytes() == b"a\nb\r\n"


def test_truncating_an_existing_file_keeps_it_and_its_permissions(tmp_path: Path) -> None:
    path = tmp_path / "shared.txt"
    path.write_bytes(b"old contents")
    fd = open_private(path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, b"new")
    os.close(fd)
    assert path.read_bytes() == b"new"
    assert not is_private(path)


def test_open_private_with_o_excl_refuses_an_existing_name(tmp_path: Path) -> None:
    path = tmp_path / "temp"
    path.write_bytes(b"")
    with pytest.raises(FileExistsError):
        open_private(path, os.O_WRONLY | os.O_EXCL)


def test_failing_to_make_a_file_private_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        make_private(tmp_path / "missing")
    with pytest.raises(OSError):
        make_private_dir(tmp_path / "missing-dir")


def test_carry_mode_keeps_a_private_original_private(tmp_path: Path) -> None:
    original = tmp_path / "note.md"
    original.write_text("before", encoding="utf-8")
    make_private(original)
    mode = stat.S_IMODE(original.stat().st_mode)
    temp = tmp_path / ".note.md.tmp"
    with temp.open("w", encoding="utf-8") as handle:
        carry_mode(handle.fileno(), mode, temp=temp, original=original)
        handle.write("after")
    os.replace(temp, original)
    assert is_private(original)
    assert original.read_text(encoding="utf-8") == "after"


def test_carry_mode_does_not_narrow_a_shared_original(tmp_path: Path) -> None:
    original = tmp_path / "note.md"
    original.write_text("before", encoding="utf-8")
    os.chmod(original, 0o644)
    assert not is_private(original)
    mode = stat.S_IMODE(original.stat().st_mode)
    temp = tmp_path / ".note.md.tmp"
    with temp.open("w", encoding="utf-8") as handle:
        carry_mode(handle.fileno(), mode, temp=temp, original=original)
        handle.write("after")
    os.replace(temp, original)
    assert not is_private(original)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_sets_and_checks_the_same_modes_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"")
    make_private(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    folder = tmp_path / "d"
    folder.mkdir()
    make_private_dir(folder)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    os.chmod(path, 0o640)
    assert not is_private(path)

    opens: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "open", lambda p, flags, mode=0o777: opens.append((p, flags, mode)) or 7)
    open_private("p", os.O_WRONLY | os.O_TRUNC)
    assert opens == [("p", os.O_WRONLY | os.O_TRUNC | os.O_CREAT, 0o600)]

    chmods: list[tuple[int, int]] = []
    monkeypatch.setattr(private.os, "fchmod", lambda fd, mode: chmods.append((fd, mode)))
    carry_mode(9, 0o640, temp="t", original="o")
    assert chmods == [(9, 0o640)]


def _icacls(path: Path) -> str:
    return subprocess.run(
        ["icacls", str(path)], capture_output=True, text=True, check=True
    ).stdout


@pytest.mark.skipif(sys.platform != "win32", reason="DACLs are the Windows branch")
def test_windows_grants_the_user_and_system_only(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    make_private(path)
    report = _icacls(path)
    entries = [line for line in report.splitlines() if ":(" in line]
    assert len(entries) == 2, report
    assert "NT AUTHORITY\\SYSTEM:(F)" in report
    assert "(I)" not in report, "a private file carries no inherited entries"


@pytest.mark.skipif(sys.platform != "win32", reason="DACLs are the Windows branch")
def test_windows_notices_a_grant_to_anyone_else(tmp_path: Path) -> None:
    path = tmp_path / "secret.txt"
    path.write_text("token", encoding="utf-8")
    make_private(path)
    # S-1-5-32-545 is BUILTIN\\Users, whatever the display language.
    subprocess.run(["icacls", str(path), "/grant", "*S-1-5-32-545:R"], capture_output=True, check=True)
    assert not is_private(path)


@pytest.mark.skipif(sys.platform != "win32", reason="DACLs are the Windows branch")
def test_windows_counts_an_inherit_only_grant_on_a_directory(tmp_path: Path) -> None:
    """It grants nothing on the directory itself, but every new child gets it."""
    folder = tmp_path / "secrets"
    folder.mkdir()
    make_private_dir(folder)
    subprocess.run(
        ["icacls", str(folder), "/grant", "*S-1-5-32-545:(OI)(CI)(IO)R"],
        capture_output=True,
        check=True,
    )
    assert not is_private(folder)

def _windows_open_for_security(path: Path, directory: bool) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 7, None, 3, 0x02000000 if directory else 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(handle, os.O_RDONLY)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows security descriptors")
@pytest.mark.parametrize("directory", [False, True])
def test_windows_strict_requires_protected_dacl(tmp_path: Path, directory: bool) -> None:
    # The parent grants only the same principals, even after inheritance is enabled.
    make_private_dir(tmp_path)
    path = tmp_path / "owned"
    if directory:
        path.mkdir()
        make_private_dir(path)
    else:
        path.write_bytes(b"secret")
        make_private(path)
    subprocess.run(["icacls", str(path), "/inheritance:e"], capture_output=True, check=True)
    import ctypes
    from ctypes import wintypes

    descriptor = private._PSECURITY_DESCRIPTOR()
    dacl = private._PACL()
    error = private._GetNamedSecurityInfoW(
        str(path), private._SE_FILE_OBJECT, private._DACL_SECURITY_INFORMATION,
        None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    assert not error
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        assert private._GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        assert not control.value & private._SE_DACL_PROTECTED
    finally:
        private._LocalFree(descriptor)
    assert private.is_owned_path(path)
    assert private.is_private(path)
    assert not private.is_private_strict(path)
    fd = _windows_open_for_security(path, directory)
    try:
        assert private.is_owned_descriptor(fd)
        assert private.is_private_descriptor(fd)
        assert not private.is_private_descriptor_strict(fd)
        (make_private_dir if directory else make_private)(path)
        assert private.is_private_strict(path)
        assert private.is_private_descriptor_strict(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows security descriptors")
def test_windows_control_read_failure_refuses_and_frees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "owned"
    path.write_bytes(b"")
    make_private(path)
    freed = []
    real_free = private._LocalFree
    def free(descriptor):
        freed.append(descriptor.value)
        return real_free(descriptor)
    monkeypatch.setattr(private, "_LocalFree", free)
    monkeypatch.setattr(private, "_GetSecurityDescriptorControl", lambda *args: 0)
    with pytest.raises(OSError):
        private.is_private_strict(path)
    assert freed and all(freed)
    fd = os.open(path, os.O_RDONLY)
    try:
        freed.clear()
        with pytest.raises(OSError):
            private.is_private_descriptor_strict(fd)
        assert freed and all(freed)
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows handle semantics")
def test_windows_strict_descriptor_inspects_open_handle_not_replaced_path(tmp_path: Path) -> None:
    make_private_dir(tmp_path)
    path = tmp_path / "opened"
    path.write_bytes(b"original")
    make_private(path)
    subprocess.run(["icacls", str(path), "/inheritance:e"], capture_output=True, check=True)
    fd = _windows_open_for_security(path, False)
    try:
        assert private.is_private_descriptor(fd)
        assert not private.is_private_descriptor_strict(fd)
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replacement")
        make_private(replacement)
        os.replace(replacement, path)
        assert private.is_private_strict(path)
        assert not private.is_private_descriptor_strict(fd)
    finally:
        os.close(fd)
