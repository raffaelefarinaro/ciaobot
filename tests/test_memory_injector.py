"""Tests for ``ciao.memory_injector``."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import memory_injector as mi
from ciao import memory_tool as mt


def test_empty_files_produce_empty_block(tmp_path: Path) -> None:
    block = mi.build_memory_block(memory_dir=tmp_path)
    assert block == ""


def test_block_renders_both_sections(tmp_path: Path) -> None:
    mt.add_entry(tmp_path / "memory.md", "fact one", char_limit=200)
    mt.add_entry(tmp_path / "user.md", "user note", char_limit=200)

    block = mi.build_memory_block(
        memory_dir=tmp_path,
        memory_char_limit=200,
        user_char_limit=200,
    )

    assert "MEMORY (your personal notes)" in block
    assert "USER PROFILE" in block
    assert "fact one" in block
    assert "user note" in block
    # Usage % included.
    assert "/200 chars]" in block


def test_block_skips_empty_section(tmp_path: Path) -> None:
    mt.add_entry(tmp_path / "memory.md", "only memory", char_limit=200)
    block = mi.build_memory_block(
        memory_dir=tmp_path,
        memory_char_limit=200,
        user_char_limit=200,
    )
    assert "MEMORY (your personal notes)" in block
    assert "USER PROFILE" not in block


def test_system_prompt_payload_returns_instructions_for_empty() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    assert payload["type"] == "preset"
    assert payload["preset"] == "claude_code"
    assert "CiaoBot System Instructions" in payload["append"]


def test_system_prompt_payload_appends_to_claude_code_preset() -> None:
    payload = mi.system_prompt_payload("hello memory")
    assert payload is not None
    assert payload["type"] == "preset"
    assert payload["preset"] == "claude_code"
    assert "CiaoBot System Instructions" in payload["append"]
    assert "hello memory" in payload["append"]


def test_system_prompt_payload_preserves_existing_append() -> None:
    base = {
        "type": "preset",
        "preset": "claude_code",
        "append": "operator hint",
    }
    payload = mi.system_prompt_payload("memory block", base_system_prompt=base)
    assert payload is not None
    assert payload["append"].startswith("operator hint")
    assert "CiaoBot System Instructions" in payload["append"]
    assert "memory block" in payload["append"]


def test_block_handles_load_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from load_entries should produce an empty string, not crash."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mi, "load_entries", boom)
    assert mi.build_memory_block(memory_dir=tmp_path) == ""
