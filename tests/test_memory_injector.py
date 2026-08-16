"""Tests for ``ciao.memory_injector``."""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import memory_injector as mi
from ciao import memory_tool as mt


def write_guide(
    path: Path,
    memory_entries: list[str] | None = None,
    profile_entries: list[str] | None = None,
    body: str = "# Guide\n\n",
) -> Path:
    """Seed a workspace guide with bounded-memory regions for a test."""
    path.write_text(body, encoding="utf-8")
    mt.ensure_regions(path)
    if memory_entries:
        mt.write_region(path, "memory", memory_entries)
    if profile_entries:
        mt.write_region(path, "profile", profile_entries)
    return path


def test_empty_files_produce_seeding_nudge(tmp_path: Path) -> None:
    """Cold start: with no entries the block must still nudge the model to
    seed memory, otherwise a fresh install never surfaces the feature."""
    guide = write_guide(tmp_path / "CLAUDE.md")
    block = mi.build_memory_block(guide_path=guide)
    assert "Edit" in block
    assert "ciao:memory" in block
    assert "ciao:profile" in block
    # There is no memory CLI any more — bounded memory is edited in place.
    assert "ciao memory add" not in block
    # No section headers — there is nothing to render yet.
    assert "MEMORY (your personal notes)" not in block
    assert "USER PROFILE" not in block


def test_block_renders_both_sections(tmp_path: Path) -> None:
    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["fact one"],
        profile_entries=["user note"],
    )

    block = mi.build_memory_block(
        guide_path=guide,
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
    guide = write_guide(tmp_path / "CLAUDE.md", memory_entries=["only memory"])
    block = mi.build_memory_block(
        guide_path=guide,
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
    assert "Ciaobot core instructions" in payload["append"]


def test_system_prompt_points_to_gws_wrapper() -> None:
    """The core keeps only the routing rule; service detail lives in skills."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Google Workspace calls go through" in append
    assert "scripts/gws-profile.sh" in append
    assert "GWS_PROFILE" in append
    assert "supportsAllDrives" not in append


def test_system_prompt_keeps_provider_sandbox_detail_out_of_the_core() -> None:
    """Provider-specific retry mechanics belong in the GWS skill."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "no native root CA certificates found" not in append
    assert "sandbox_permissions: require_escalated" not in append


def test_system_prompt_delegates_url_detail_to_skills() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "installed skills" in append
    assert "Reading URLs" not in append
    assert "defuddle parse" not in append


def test_system_prompt_delegates_issue_labeling_to_skills() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "installed skills" in append
    assert "Issue labeling" not in append


def test_system_prompt_keeps_diagnostic_entrypoint_compact() -> None:
    """Installed agents should know which local logs to inspect for support."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "When diagnosing Ciaobot itself" in append
    assert ".runtime/server_errors.log" in append
    assert ".runtime/job_runs.jsonl" in append
    assert ".runtime/ciao.stderr.log" not in append
    assert "Public GitHub issues" in append


def test_system_prompt_includes_project_canonical_doc_notes() -> None:
    """Vault-backed project chats should instruct agents to maintain canonical docs."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "canonical document" in append
    assert "meaningful decisions" in append


def test_system_prompt_includes_native_memory_and_vault_routing() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "ciao:memory" in append
    assert "ciao:profile" in append
    assert "vault_search" in append
    assert "memory_update" in append
    assert "ciao memory" not in append
    assert "ciao vault-search" not in append


def test_mcp_system_prompt_strips_legacy_control_recipes(tmp_path: Path) -> None:
    guide = write_guide(tmp_path / "CLAUDE.md", memory_entries=["fact one"])
    memory_block = mi.build_memory_block(guide_path=guide)

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
    # Legacy transport recipes are no longer copied into either arm.
    assert "ciao create-chat" not in legacy_append
    assert "ciao vault-lint" not in legacy_append
    assert "ciao create-chat" not in mcp_append
    assert "Prefer the managed Ciaobot MCP tools" in mcp_append
    assert "memory_update" in mcp_append
    assert "there is no `ciao memory` command" not in mcp_append


def test_system_prompt_payload_appends_to_claude_code_preset() -> None:
    payload = mi.system_prompt_payload("hello memory")
    assert payload is not None
    assert payload["type"] == "preset"
    assert payload["preset"] == "claude_code"
    assert payload["exclude_dynamic_sections"] is True
    assert "Ciaobot core instructions" in payload["append"]
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
    assert "Ciaobot core instructions" in payload["append"]
    assert "memory block" in payload["append"]


def test_block_handles_load_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from read_region should produce an empty string, not crash."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    guide = write_guide(tmp_path / "CLAUDE.md")
    monkeypatch.setattr(mi, "read_region", boom)
    assert mi.build_memory_block(guide_path=guide) == ""


def test_expired_memory_entries_filtered(tmp_path: Path) -> None:
    import datetime

    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=["durable fact", "temporary note [expires: 2026-01-01]"],
    )

    # With reference date 2026-07-26, the 2026-01-01 entry is expired
    today = datetime.date(2026, 7, 26)
    block = mi.build_memory_block(guide_path=guide, memory_char_limit=200, today=today)

    assert "durable fact" in block
    assert "temporary note" not in block
    assert "1 expired" in block
    assert "stored" in block


def test_expired_only_memory_is_not_described_as_empty(tmp_path: Path) -> None:
    import datetime

    entry = ("x" * 150) + " [expires: 2020-01-01]"
    guide = write_guide(tmp_path / "CLAUDE.md", memory_entries=[entry])

    block = mi.build_memory_block(
        guide_path=guide,
        memory_char_limit=200,
        today=datetime.date(2026, 7, 26),
    )
    stored_entries, _diags = mt.read_region(guide, "memory")
    stored_chars = mt.total_chars(stored_entries)

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

    guide = write_guide(
        tmp_path / "CLAUDE.md",
        memory_entries=[
            "compact [expires: 20260720]",
            "multiple [expires: 2026-07-20] [expires: someday]",
        ],
    )

    block = mi.build_memory_block(
        guide_path=guide,
        today=datetime.date(2026, 7, 26),
    )

    assert "compact" in block
    assert "multiple" in block


def test_system_prompt_payload_includes_expertise_header() -> None:
    payload = mi.system_prompt_payload("memory block")
    assert payload is not None
    assert "[SYSTEM EXPERTISE: Ciaobot core]" in payload["append"]
