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


def test_system_prompt_retries_gws_keychain_failures_outside_codex_sandbox() -> None:
    """A Codex sandbox cannot always read macOS Keychain CA roots."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "no native root CA certificates found" in append
    assert "sandbox_permissions: require_escalated" in append


def test_system_prompt_includes_url_reading_notes() -> None:
    """PWA turns get their instructions from system_prompt.md, not the repo's
    CLAUDE.md, so the defuddle-first rule has to live here or it never fires."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Reading URLs" in append
    assert "defuddle parse <url> --md" in append
    # WebFetch must stay described as the fallback, not an equal option.
    assert "WebFetch" in append
    assert "web-research" in append


def test_system_prompt_includes_issue_labeling_notes() -> None:
    """Every gh issue create from a chat turn must apply the convention;
    a drift test pins the table so a future edit cannot silently drop it."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Issue labeling" in append
    # Title-prefix -> label mapping is the rule. Each row must stay.
    for prefix, label in (
        ("[Bug]", "bug"),
        ("[Feature]", "enhancement"),
        ("[Docs]", "documentation"),
        ("[Chore]", "chore"),
    ):
        assert prefix in append, f"missing title prefix {prefix}"
        assert label in append, f"missing label {label}"
    # The anonymous bug-report form was retired on 2026-07-30, so nothing can
    # open a `[Report]` issue any more. The prompt must say so rather than
    # listing it as a live option.
    assert "retired `[Report]` prefix" in append


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
    assert "delegate_spawn" in append
    assert "Memory-Proposals.md" in append


def test_mcp_system_prompt_strips_legacy_control_recipes() -> None:
    memory_block = (
        "Edit them with `ciao memory read|add|replace|remove (--target memory|user); "
        "CLI edits persist immediately but only appear in this block on the next session."
    )
    legacy = mi.system_prompt_payload(memory_block)
    mcp = mi.system_prompt_payload(memory_block, control_surface="mcp")
    assert legacy is not None and mcp is not None
    legacy_append = legacy["append"]
    mcp_append = mcp["append"]

    # The MCP path strips the legacy CLI/curl transport recipes entirely.
    assert "ciao create-chat" not in mcp_append
    assert "ciao provider-chat" not in mcp_append
    assert "ciao vault-search" not in mcp_append
    assert "ciao vault-lint" not in mcp_append
    assert "ciao sync-skills" not in mcp_append
    # Those recipes remain on the legacy path.
    assert "ciao create-chat" in legacy_append
    assert "ciao vault-lint" in legacy_append

    # It no longer injects specific MCP tool-name equivalents; a single nudge to
    # prefer the typed tools replaces the recipe block.
    assert "prefer them over curl, the ciao CLI" in mcp_append
    assert "Ciaobot MCP control plane" not in mcp_append
    assert "memory_read" not in mcp_append

    # The bounded-memory block preamble is rewritten to the MCP memory tools.
    assert "Edit them with the Ciaobot MCP memory tools;" in mcp_append
    assert "Tool edits persist immediately" in mcp_append

    # Stripping recipes makes the MCP prompt strictly shorter than legacy.
    assert len(mcp_append) < len(legacy_append)


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


def test_expired_memory_entries_filtered(tmp_path: Path) -> None:
    import datetime

    mt.add_entry(tmp_path / "memory.md", "durable fact", char_limit=200)
    mt.add_entry(tmp_path / "memory.md", "temporary note [expires: 2026-01-01]", char_limit=200)

    # With reference date 2026-07-26, the 2026-01-01 entry is expired
    today = datetime.date(2026, 7, 26)
    block = mi.build_memory_block(memory_dir=tmp_path, today=today)

    assert "durable fact" in block
    assert "temporary note" not in block
    assert "1 expired" in block
    assert "stored" in block


def test_expired_only_memory_is_not_described_as_empty(tmp_path: Path) -> None:
    import datetime

    mt.add_entry(
        tmp_path / "memory.md",
        ("x" * 150) + " [expires: 2020-01-01]",
        char_limit=200,
    )

    block = mi.build_memory_block(
        memory_dir=tmp_path,
        memory_char_limit=200,
        today=datetime.date(2026, 7, 26),
    )
    stored_chars = mt.total_chars(mt.load_entries(tmp_path / "memory.md"))

    assert "are empty" not in block
    assert "1 expired" in block
    assert f"stored {stored_chars:,}/200 chars" in block
    assert "remove expired entries" in block
    assert "x" * 20 not in block


def test_entry_expires_after_its_stated_date() -> None:
    import datetime

    entry = "temporary note [expires: 2026-07-26]"
    assert mi.is_entry_expired(entry, datetime.date(2026, 7, 26)) is False
    assert mi.is_entry_expired(entry, datetime.date(2026, 7, 27)) is True


def test_noncanonical_or_ambiguous_expiration_tags_stay_visible(
    tmp_path: Path,
) -> None:
    import datetime

    mt.add_entry(
        tmp_path / "memory.md",
        "compact [expires: 20260720]",
        char_limit=400,
    )
    mt.add_entry(
        tmp_path / "memory.md",
        "multiple [expires: 2026-07-20] [expires: someday]",
        char_limit=400,
    )

    block = mi.build_memory_block(
        memory_dir=tmp_path,
        today=datetime.date(2026, 7, 26),
    )

    assert "compact" in block
    assert "multiple" in block


def test_system_prompt_payload_includes_expertise_header() -> None:
    payload = mi.system_prompt_payload("memory block")
    assert payload is not None
    assert "[SYSTEM EXPERTISE: SOPs & Durable Memory]" in payload["append"]
