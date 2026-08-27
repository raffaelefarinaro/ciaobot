"""Tests for validated skill zip import (``ciao/skill_import.py``)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from ciao.skill_import import (
    MAX_SKILL_ASSET_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    extract_skill_zip,
    validate_skill_zip,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _skill_zip(name: str = "demo", extra: dict[str, bytes] | None = None) -> bytes:
    entries: dict[str, bytes] = {
        f"{name}/SKILL.md": (
            f"---\nname: {name}\ndescription: Demo skill\n---\n\n# {name}\n"
        ).encode()
    }
    if extra:
        entries.update(extra)
    return _zip_bytes(entries)


def test_validate_accepts_valid_skill_zip() -> None:
    name, errors = validate_skill_zip(_skill_zip())
    assert errors == []
    assert name == "demo"


def test_validate_rejects_dot_folder_name() -> None:
    # A zip whose top-level folder is "." would extract SKILL.md into the
    # skills/ root, where inventory and sync never look.
    name, errors = validate_skill_zip(_skill_zip(name="."))
    assert name is None
    assert any("not a valid skill directory name" in e for e in errors)


def test_validate_rejects_dotdot_folder_name() -> None:
    # ".." is caught by the zip-slip check before the folder-name check.
    name, errors = validate_skill_zip(_skill_zip(name=".."))
    assert name is None
    assert errors


def test_validate_rejects_leading_dot_folder_name() -> None:
    name, errors = validate_skill_zip(_skill_zip(name=".hidden"))
    assert name is None
    assert any("not a valid skill directory name" in e for e in errors)


def test_validate_rejects_oversized_member() -> None:
    big = b"x" * (MAX_SKILL_ASSET_BYTES + 1)
    name, errors = validate_skill_zip(_skill_zip(extra={"demo/asset.bin": big}))
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)


def test_validate_rejects_oversized_total() -> None:
    # Five members each under the per-file cap but over the total cap.
    per = MAX_SKILL_ASSET_BYTES
    extra = {f"demo/{i}.bin": bytes([i]) * per for i in range(5)}
    name, errors = validate_skill_zip(_skill_zip(extra=extra))
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)


def test_extract_streams_through_caps(tmp_path: Path) -> None:
    # A zip that lies about its declared uncompressed size must still be
    # bounded during extraction.
    big = b"y" * (MAX_SKILL_ASSET_BYTES + 1)
    name, errors = extract_skill_zip(_skill_zip(extra={"demo/asset.bin": big}), tmp_path)
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)
    # Nothing should have been left behind from the aborted extraction.
    assert not (tmp_path / "demo").exists()


def test_extract_writes_valid_skill(tmp_path: Path) -> None:
    name, errors = extract_skill_zip(_skill_zip(), tmp_path)
    assert errors == []
    assert name == "demo"
    assert (tmp_path / "demo" / "SKILL.md").is_file()
