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
from pathlib import Path

import pytest

from ciao.os_support import private
from ciao.os_support.private import (
    carry_mode,
    is_private,
    make_private,
    make_private_dir,
    open_private,
)


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
