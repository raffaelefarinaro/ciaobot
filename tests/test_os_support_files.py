"""ciao.os_support.files: POSIX open and replace semantics."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from ciao.os_support.files import open_fd

# CR, LF and Ctrl-Z bytes that must round-trip unchanged.
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
    link.symlink_to(target)
    with pytest.raises(OSError) as raised:
        open_fd(link, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, follow_symlinks=False)
    assert raised.value.errno == errno.ELOOP
    assert target.read_bytes() == b"keep"


def _record_os_open(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []

    def fake_open(path: str, flags: int, mode: int = 0o777) -> int:
        calls.append((path, flags, mode))
        return 7

    monkeypatch.setattr(os, "open", fake_open)
    return calls


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

# ── replace_file ───────────────────────────────────────────────────────────


def test_posix_replace_is_os_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import files

    calls = []
    monkeypatch.setattr(files.os, "replace", lambda a, b: calls.append((a, b)))
    files.replace_file("a", "b")
    assert calls == [("a", "b")]
