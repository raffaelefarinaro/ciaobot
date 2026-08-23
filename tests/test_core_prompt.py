"""Tests for ``ciao.core_prompt``.

The injector appends only the Ciaobot core instructions; bounded-memory
regions reach every provider through its native guide load, so their
rendering has no code left here to test. Region storage semantics are
covered in ``test_memory_tool.py``.
"""

from __future__ import annotations

from ciao import core_prompt as mi


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
    assert "private working evidence" in append
    assert "internal sentinels" in append
    assert "memory_update" in append
    assert "ciao memory" not in append
    assert "ciao vault-search" not in append


def test_mcp_and_legacy_prompts_are_identical() -> None:
    """The core is transport-agnostic: no CLI/curl recipes in either arm."""
    legacy = mi.system_prompt_payload("memory block")
    mcp = mi.system_prompt_payload("memory block", control_surface="mcp")
    assert legacy is not None and mcp is not None

    for append in (legacy["append"], mcp["append"]):
        assert "ciao create-chat" not in append
        assert "ciao provider-chat" not in append
        assert "ciao vault-search" not in append
        assert "ciao vault-lint" not in append
        assert "ciao sync-skills" not in append

    # The surfaces are byte-identical; there is nothing left to strip.
    assert legacy["append"] == mcp["append"]
    assert "Prefer the managed Ciaobot MCP tools" in mcp["append"]
    assert "memory_update" in mcp["append"]


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


def test_system_prompt_payload_includes_expertise_header() -> None:
    payload = mi.system_prompt_payload("memory block")
    assert payload is not None
    assert "[SYSTEM EXPERTISE: Ciaobot core]" in payload["append"]
