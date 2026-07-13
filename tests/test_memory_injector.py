"""Tests for ``ciao.memory_injector``."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import memory_injector as mi
from ciao import memory_tool as mt


def test_empty_files_produce_seeding_nudge(tmp_path: Path) -> None:
    """Cold start: with no entries the block must still nudge the model to
    seed memory, otherwise a fresh install never surfaces the feature."""
    block = mi.build_memory_block(memory_dir=tmp_path)
    assert "ciao memory add" in block
    assert "--target user" in block
    # No section headers — there is nothing to render yet.
    assert "MEMORY (your personal notes)" not in block
    assert "USER PROFILE" not in block


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
    assert "Ciaobot System Instructions" in payload["append"]


def test_system_prompt_includes_gws_operational_notes() -> None:
    """The gws integration notes moved out of the gws-shared skill and must
    live in the system prompt so the agent gets them every turn."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Google Workspace (gws)" in append
    assert "scripts/gws-profile.sh" in append
    # Key operational gotchas that used to live in gws-shared.
    assert "GWS_PROFILE" in append
    assert "supportsAllDrives" in append


def test_system_prompt_includes_ciaobot_diagnostics_notes() -> None:
    """Installed agents should know which local logs to inspect for support."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Ciaobot Diagnostics and Issue Reports" in append
    assert ".runtime/server_errors.log" in append
    assert ".runtime/job_runs.jsonl" in append
    assert ".runtime/ciao.stderr.log" in append
    assert "GitHub issue" in append


def test_system_prompt_includes_project_canonical_doc_notes() -> None:
    """Vault-backed project chats should instruct agents to maintain canonical docs."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Project canonical docs" in append
    assert "[Canonical doc:" in append
    assert "log.md" in append


def test_system_prompt_includes_memory_and_vault_notes() -> None:
    """Agents should know bounded memory, vault search, and recall CLIs every session."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Memory and vault" in append
    assert "ciao memory" in append
    assert "ciao vault-search" in append
    assert "ciao vault-index" in append
    assert "ciao vault-lint" in append
    assert "ciao sync-skills" in append
    assert "ciao create-chat" in append
    assert "Memory-Proposals.md" in append


def test_system_prompt_payload_appends_to_claude_code_preset() -> None:
    payload = mi.system_prompt_payload("hello memory")
    assert payload is not None
    assert payload["type"] == "preset"
    assert payload["preset"] == "claude_code"
    assert "Ciaobot System Instructions" in payload["append"]
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
    assert "Ciaobot System Instructions" in payload["append"]
    assert "memory block" in payload["append"]


def test_block_handles_load_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from load_entries should produce an empty string, not crash."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(mi, "load_entries", boom)
    assert mi.build_memory_block(memory_dir=tmp_path) == ""
