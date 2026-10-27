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
    assert "<ciaobot_core>" in payload["append"]


def test_system_prompt_points_to_gws_wrapper() -> None:
    """The core keeps only the routing rule; service detail lives in skills."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "Google Workspace calls go through" in append
    assert "ciao gws" in append
    assert "GWS_PROFILE" in append
    assert "supportsAllDrives" not in append


def test_system_prompt_keeps_provider_sandbox_detail_out_of_the_core() -> None:
    """Provider-specific retry mechanics belong in the GWS skill."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "no native root CA certificates found" not in append
    assert "sandbox_permissions: require_escalated" not in append


def test_system_prompt_routes_urls_to_provider_fetch_and_gh() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "installed skills" in append
    assert "When the user provides a URL" in append
    # URL reading is the provider's own web-fetch, not a bundled reader: the
    # engine ships no npm CLI, so the prompt must not name one.
    assert "web-fetch tool" in append
    assert "defuddle" not in append
    # GitHub goes through gh, which the model already knows how to drive; what
    # the prompt has to add is that it beats web-fetch and works on private
    # repos. The full URL->command mapping is deliberately NOT here: this text
    # loads every turn and the payload budget is 9000 chars
    # (test_agent_surface.py::test_cli_surface_prompt_carries_the_whole_command_table).
    assert "For GitHub URLs, use `gh` instead" in append
    assert "web pages, email and connector content" in append


def test_system_prompt_delegates_issue_labeling_to_skills() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "installed skills" in append
    assert "Issue labeling" not in append


def test_system_prompt_keeps_diagnostic_entrypoint_compact() -> None:
    """Installed agents learn the workspace-scoped entry point, not log paths."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "When diagnosing Ciaobot itself" in append
    assert "engine-wide error, debug and service logs are omitted" in append
    assert "`scope`/`omitted`" in append
    # The engine-wide runtime files are not advertised as readable evidence.
    assert ".runtime/server_errors.log" not in append
    assert ".runtime/job_runs.jsonl" not in append
    assert ".runtime/ciao.stderr.log" not in append
    assert "Public GitHub issues" in append
    assert "ciao context logs" in append


def test_system_prompt_states_workspace_scope_limits() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "file_access=workspace only" in append
    assert "Do not probe them" in append
    assert "Settings → Overview → File access" in append
    assert "`gh` is not signed in" in append


def test_system_prompt_includes_project_canonical_doc_notes() -> None:
    """Vault-backed project chats should instruct agents to maintain canonical docs."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "canonical document" in append
    assert "meaningful decisions" in append
    assert "`README.md` first, then `<folder-name>.md`" in append
    assert "Use the supplied canonical path" in append
    assert "do not duplicate or rename it" in append
    assert "not a folder-naming rule" in append


def test_system_prompt_includes_native_memory_and_vault_routing() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "ciao:memory" in append
    assert "ciao:profile" in append
    assert "ciao vault search" in append
    assert "private working evidence" in append
    assert "internal sentinels" in append
    assert "ciao memory update" in append
    assert "memory_update" not in append


def test_the_core_prompt_carries_no_cli_recipes() -> None:
    """The core is the single CLI-first prompt: only `ciao <noun> <verb>`."""
    payload = mi.system_prompt_payload("memory block")
    assert payload is not None

    append = payload["append"]
    assert "ciao create-chat" not in append
    assert "ciao provider-chat" not in append
    assert "ciao vault-search" not in append
    assert "ciao vault-lint" not in append
    assert "ciao sync-skills" not in append

    assert "Prefer the managed Ciaobot MCP tools" not in append
    assert "ciao vault search" in append


def test_system_prompt_payload_appends_to_claude_code_preset() -> None:
    payload = mi.system_prompt_payload("hello memory")
    assert payload is not None
    assert payload["type"] == "preset"
    assert payload["preset"] == "claude_code"
    assert payload["exclude_dynamic_sections"] is True
    assert "<ciaobot_core>" in payload["append"]
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
    assert "<ciaobot_core>" in payload["append"]
    assert "memory block" in payload["append"]


def test_conversational_scope_rule_reaches_provider_append() -> None:
    """The chat-first rule reaches every provider payload (#1599).

    The rule lives in the always-loaded core, so the ordinary payload and the
    payload that already carries an operator append must both keep it, and
    neither may lose the append that was already there.
    """
    instructions = mi._system_instructions()
    assert "stay in the chat" in instructions

    ordinary = mi.system_prompt_payload("")
    assert ordinary is not None
    assert "stay in the chat" in ordinary["append"]
    assert ordinary["type"] == "preset"
    assert ordinary["preset"] == "claude_code"

    with_append = mi.system_prompt_payload(
        "memory block",
        base_system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "operator hint",
        },
    )
    assert with_append is not None
    append = with_append["append"]
    assert append.startswith("operator hint")
    assert "stay in the chat" in append
    assert "memory block" in append


def test_core_sections_are_tag_delimited() -> None:
    payload = mi.system_prompt_payload("memory block")
    assert payload is not None
    append = payload["append"]
    assert "[SYSTEM EXPERTISE" not in append
    for tag in (
        "ciaobot_core",
        "ciaobot_operating_contract",
        "ciaobot_retrieval",
        "ciaobot_deliverables",
        "ciaobot_response_quality",
        "ciaobot_cli_reference",
    ):
        assert append.count(f"<{tag}>") == 1, tag
        assert append.count(f"</{tag}>") == 1, tag
        assert append.index(f"<{tag}>") < append.index(f"</{tag}>")
    assert "\n## " not in append and not append.startswith("# ")
    assert "ciao vault review" not in append  # the synopsis lives in ciao-cli


def test_rules_carry_a_reason_in_the_same_bullet() -> None:
    """Each formerly bare prohibition says why (#1464)."""
    text = mi._system_instructions()
    bullets = {line for line in text.splitlines() if line.startswith("- ")}

    def bullet(needle: str) -> str:
        return next(line for line in bullets if needle in line)

    assert "cuts off the PWA session" in bullet("Never restart Ciaobot")
    assert "bypasses those guards" in bullet("Do not use curl")
    assert "agent surface" in bullet("Do not use curl")
    assert "outside Ciaobot's schedule list" in bullet("provider-native recurring automations")
    assert "cannot be undone or are seen by others" in bullet("destructive git operations")
    assert "output lands in transcripts and replies" in bullet("never quote or repeat credentials")
    assert "public and permanent" in bullet("Public GitHub issues")
    assert "land in notes and transcripts" in bullet("webhook create|rotate")


def test_system_prompt_task_bullet_runs_task_rules_before_filing() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert append.count("ciao task-rules") == 1
    assert "Workspace/Task-Rules.md" not in append
