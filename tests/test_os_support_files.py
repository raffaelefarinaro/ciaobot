"""ciao.os_support.files keeps POSIX open semantics on POSIX and Windows.

The behavioural tests run on every OS the CI matrix covers. The two flag tests
pin each branch to the exact ``os.open`` call it makes, the POSIX one to the
calls the call sites made before the module existed.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from ciao.os_support.files import open_fd

# Every byte a text-mode descriptor would rewrite or stop at on Windows.
_AWKWARD = b"a\nb\r\nc\x1ad\n"


def test_bytes_written_are_bytes_stored(tmp_path: Path) -> None:
    raw = tmp_path / "raw.bin"
    fd = open_fd(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, _AWKWARD)
    os.close(fd)
    assert raw.read_bytes() == _AWKWARD

    wrapped = tmp_path / "wrapped.bin"
    with os.fdopen(open_fd(wrapped, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb") as out:
        out.write(_AWKWARD)
    assert wrapped.read_bytes() == _AWKWARD


def test_bytes_read_are_bytes_stored(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(_AWKWARD)
    fd = open_fd(path, os.O_RDONLY)
    try:
        assert os.read(fd, 64) == _AWKWARD
    finally:
        os.close(fd)


def test_not_following_links_still_creates_and_opens_regular_files(tmp_path: Path) -> None:
    path = tmp_path / "new.txt"
    fd = open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, follow_symlinks=False)
    os.write(fd, b"one")
    os.close(fd)
    fd = open_fd(path, os.O_WRONLY | os.O_TRUNC, follow_symlinks=False)
    os.write(fd, b"two")
    os.close(fd)
    assert path.read_bytes() == b"two"


def test_not_following_links_is_binary_and_honours_append(tmp_path: Path) -> None:
    path = tmp_path / "journal.bin"
    for chunk in (_AWKWARD, _AWKWARD):
        fd = open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600, follow_symlinks=False)
        os.write(fd, chunk)
        os.close(fd)
    assert path.read_bytes() == _AWKWARD + _AWKWARD


def test_not_following_links_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):  # EISDIR, as `os.open` gives on POSIX
        open_fd(tmp_path, os.O_WRONLY, follow_symlinks=False)


def test_a_symlink_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"keep")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without Developer Mode or admin
        pytest.skip(f"cannot create a symlink here: {exc}")
    with pytest.raises(OSError) as raised:
        open_fd(link, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, follow_symlinks=False)
    assert raised.value.errno == errno.ELOOP
    assert target.read_bytes() == b"keep"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows link type")
def test_a_junction_is_refused_as_a_link(tmp_path: Path) -> None:
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "junction"
    _winapi.CreateJunction(str(real), str(junction))
    with pytest.raises(OSError) as raised:
        open_fd(junction, os.O_WRONLY | os.O_CREAT, 0o600, follow_symlinks=False)
    assert raised.value.errno == errno.ELOOP


def _record_os_open(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        calls.append((path, flags, mode))
        return 7

    monkeypatch.setattr(os, "open", fake_open)
    return calls


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_makes_the_same_os_open_calls_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_os_open(monkeypatch)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    open_fd("p", flags, 0o600)
    open_fd("p", flags, 0o600, follow_symlinks=False)
    open_fd("p", os.O_RDONLY)
    assert calls == [
        ("p", flags, 0o600),
        ("p", flags | os.O_NOFOLLOW, 0o600),
        ("p", os.O_RDONLY, 0o777),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows branch is not defined elsewhere")
def test_windows_opens_every_descriptor_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A followed open is `os.open` + `O_BINARY`; a no-follow one never calls it.

    The no-follow path goes through `CreateFileW` (`create_fd`), which is binary
    by construction; `test_not_following_links_is_binary_and_honours_append`
    checks its bytes.
    """
    missing = str(tmp_path / "missing")
    calls = _record_os_open(monkeypatch)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    open_fd(missing, flags, 0o600)
    assert calls == [(missing, flags | os.O_BINARY, 0o600)]
    monkeypatch.undo()
    fd = open_fd(missing, flags, 0o600, follow_symlinks=False)
    os.close(fd)
    assert calls == [(missing, flags | os.O_BINARY, 0o600)]


# ── replace_file ───────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="the retry is the Windows branch")
def test_windows_replace_retries_the_refusal_a_concurrent_replace_causes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support import files

    src, dst = tmp_path / "new", tmp_path / "target"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    real = os.replace
    refusals = {"left": 2}

    def busy_then_free(a, b):
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "Access is denied", str(b))
        real(a, b)

    monkeypatch.setattr(files.os, "replace", busy_then_free)
    files.replace_file(src, dst)
    assert dst.read_bytes() == b"new"
    assert not src.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the retry is the Windows branch")
def test_windows_replace_gives_up_after_its_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support import files

    def always_busy(a, b):
        raise PermissionError(13, "Access is denied", str(b))

    monkeypatch.setattr(files.os, "replace", always_busy)
    monkeypatch.setattr(files, "_REPLACE_DEADLINE_S", 0.05)
    with pytest.raises(PermissionError):
        files.replace_file(tmp_path / "a", tmp_path / "b")


@pytest.mark.skipif(sys.platform != "win32", reason="the retry is the Windows branch")
def test_windows_read_waits_out_a_writer_holding_the_file(tmp_path: Path) -> None:
    # A lock-free store read races the writer's replace; while the target is
    # held without sharing, as during `MoveFileEx`, the open is refused with a
    # sharing violation and has to be retried, not reported as a corrupt store.
    import ctypes
    import threading
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    target = tmp_path / "store.json"
    target.write_bytes(b"{}")
    exclusive = kernel32.CreateFileW(str(target), 0x80000000, 0, None, 3, 0x80, None)
    assert exclusive not in (None, wintypes.HANDLE(-1).value)
    releaser = threading.Timer(0.1, kernel32.CloseHandle, args=(exclusive,))
    releaser.start()
    try:
        fd = open_fd(target, os.O_RDONLY, follow_symlinks=False)
    finally:
        releaser.join()
    with os.fdopen(fd, "rb") as handle:
        assert handle.read() == b"{}"


@pytest.mark.skipif(sys.platform != "win32", reason="the retry is the Windows branch")
def test_windows_read_does_not_retry_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support import files

    monkeypatch.setattr(files.time, "sleep", lambda _s: pytest.fail("retried a missing file"))
    with pytest.raises(FileNotFoundError):
        open_fd(tmp_path / "absent", os.O_RDONLY, follow_symlinks=False)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_replace_is_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import files

    calls = []
    monkeypatch.setattr(files.os, "replace", lambda a, b: calls.append((a, b)))
    files.replace_file("a", "b")
    assert calls == [("a", "b")]
