"""ciao.workspace_access: a macOS privacy (TCC) denial is recorded, not crash-looped."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

import ciao.install_receipt as install_receipt
from ciao.workspace_access import (
    DENIAL_FILE_NAME,
    check_workspace_access,
    denial_path,
)


def _eperm(*_args: object) -> str:
    raise PermissionError(errno.EPERM, "Operation not permitted")


def _eacces(*_args: object) -> str:
    raise PermissionError(errno.EACCES, "Permission denied")


class _Scan:
    def __enter__(self) -> "_Scan":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _scandir_ok(_path: str) -> _Scan:
    return _Scan()


def test_non_darwin_never_probes(tmp_path: Path) -> None:
    record = tmp_path / "x.txt"

    result = check_workspace_access(platform="linux", getcwd=_eperm, path=record)

    assert result is None
    assert not record.exists()


def test_eperm_on_getcwd_records_and_returns_the_message(tmp_path: Path) -> None:
    record = tmp_path / "x.txt"
    workspace = str(Path("/Users/u/Documents/ws"))

    message = check_workspace_access(
        platform="darwin",
        getcwd=_eperm,
        environ={"CIAO_WORKSPACE": workspace},
        path=record,
    )

    assert message is not None
    assert workspace in message
    assert "Files and Folders" in message
    assert "Ciaobot Server" in message
    assert record.read_text(encoding="utf-8") == message + "\n"


def test_eperm_on_scandir_is_also_a_denial(tmp_path: Path) -> None:
    record = tmp_path / "x.txt"

    def scandir(_path: str) -> _Scan:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    message = check_workspace_access(
        platform="darwin",
        getcwd=lambda: str(tmp_path),
        scandir=scandir,
        environ={"CIAO_WORKSPACE": "/w"},
        path=record,
    )

    assert message is not None
    assert record.is_file()


def test_eacces_is_not_a_tcc_denial(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        check_workspace_access(
            platform="darwin", getcwd=_eacces, environ={}, path=tmp_path / "x.txt"
        )


def test_success_clears_a_stale_record(tmp_path: Path) -> None:
    record = tmp_path / "x.txt"
    record.write_text("old\n", encoding="utf-8")

    result = check_workspace_access(
        platform="darwin",
        getcwd=lambda: str(tmp_path),
        scandir=_scandir_ok,
        environ={},
        path=record,
    )

    assert result is None
    assert not record.exists()


def test_missing_workspace_env_uses_a_generic_name(tmp_path: Path) -> None:
    message = check_workspace_access(
        platform="darwin", getcwd=_eperm, environ={}, path=tmp_path / "x.txt"
    )

    assert message is not None
    assert "your Ciaobot workspace folder" in message


def test_denial_path_is_outside_the_workspace() -> None:
    assert denial_path() == install_receipt.default_receipt_path().parent / DENIAL_FILE_NAME
