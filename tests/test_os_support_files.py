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
    missing = str(tmp_path / "missing")
    calls = _record_os_open(monkeypatch)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    open_fd(missing, flags, 0o600)
    open_fd(missing, flags, 0o600, follow_symlinks=False)
    assert calls == [
        (missing, flags | os.O_BINARY, 0o600),
        (missing, flags | os.O_BINARY, 0o600),
    ]
