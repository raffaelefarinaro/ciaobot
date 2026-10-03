"""ciao.os_support.paths: one spelling for a resolved path, whatever the race."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ciao.os_support.paths import resolve_path

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the extended-length prefix is Windows'")


def test_resolve_path_is_path_resolve_for_an_existing_directory(tmp_path: Path) -> None:
    assert resolve_path(tmp_path) == tmp_path.resolve()


@windows_only
def test_resolve_path_drops_the_extended_length_prefix_on_a_drive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What ntpath.realpath returns when the folder is being created by another
    # thread while it checks: the final path, still prefixed.
    plain = tmp_path.resolve()
    monkeypatch.setattr(type(tmp_path), "resolve", lambda self, strict=False: Path("\\\\?\\" + str(plain)))

    assert resolve_path(tmp_path) == plain


@windows_only
def test_resolve_path_keeps_a_unc_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unc = Path("\\\\?\\UNC\\server\\share\\vault")
    monkeypatch.setattr(type(tmp_path), "resolve", lambda self, strict=False: unc)

    assert resolve_path(tmp_path) == unc


@windows_only
def test_vault_root_guard_does_not_call_a_racing_folder_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
            "CIAO_VAULT_ROOT": "memory-vault",
        }
    )
    real = type(tmp_path).resolve

    def racing(self: Path, strict: bool = False) -> Path:
        out = real(self, strict)
        return Path("\\\\?\\" + str(out)) if "memory-vault" in str(self) else out

    monkeypatch.setattr(type(tmp_path), "resolve", racing)

    resolved = config._resolve_vault_root("memory-vault/personal")

    assert resolved == (config.workspace_root / "memory-vault" / "personal")
