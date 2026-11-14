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
    # repos. Exact URL->command recipes belong in help, not the core.
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
    assert "scoped evidence" in append
    assert "do not interpret omitted logs as a clean bill of health" in append
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
    assert "Do not probe excluded paths or retry denied access" in append
    assert "Settings → Overview → File access" in append
    assert "do not assume integrations are signed out" in append


def test_system_prompt_includes_project_canonical_doc_notes() -> None:
    """Vault-backed project chats should instruct agents to maintain canonical docs."""
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "canonical document" in append
    assert "meaningful decisions" in append
    assert "do not duplicate or rename the supplied canonical path" in append
    assert "only within authorized document or implementation work" in append
    assert "otherwise offer and wait" in append


def test_system_prompt_includes_native_memory_and_vault_routing() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert "`AGENTS.md` is the authoritative native workspace guide and bounded-memory source" in append
    assert "Do not duplicate its memory" in append
    assert "competing `CLAUDE.md`" in append
    assert "ciao vault search" in append
    assert "private working evidence" in append
    assert "internal sentinels" in append
    assert "memory update" in append
    assert "read `ciao-memory`" in append
    assert "memory_update" not in append


def test_the_core_prompt_carries_no_cli_recipes() -> None:
    """The core routes managed operations to the current CLI, not old recipes."""
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
    assert "agent surface enforces" in bullet("route around permission checks")
    assert "ordinary note content" in bullet("route around permission checks")
    assert "outside Ciaobot's schedule list" in bullet("Provider-native recurring automations")
    assert "irreversible or visible to others" in bullet("destructive git operations")
    assert "output lands in transcripts and replies" in bullet("never quote credentials")
    assert "public and permanent" in bullet("Public GitHub issues")
    # Secret handling moves with the webhook procedure, reachable via help.
    from pathlib import Path

    reference = (Path(mi.__file__).parent / "stock/skills/ciao-cli/SKILL.md").read_text(encoding="utf-8")
    assert "never save a copy in notes, memory or source files" in reference


def test_system_prompt_task_bullet_runs_task_rules_before_filing() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    append = payload["append"]
    assert append.count("ciao task-rules") == 1
    assert "Workspace/Task-Rules.md" not in append


def test_core_uses_guard_boundaries_not_a_curl_or_github_login_ban() -> None:
    text = mi._system_instructions()
    assert "Do not use curl" not in text
    assert "route around permission checks or refusals" in text
    assert "ordinary note content" in text
    assert "`gh` is not signed in" not in text
    assert "check its authentication" in text


def test_core_routes_write_procedures_out_of_the_always_loaded_prompt() -> None:
    text = mi._system_instructions()
    assert "Before durable writes or proposal curation, read `ciao-memory`" in text
    assert "Separate entries with" not in text
    assert "[expires:" not in text
    assert "A note without frontmatter cannot be indexed" not in text
    assert "Every Ciaobot operation is a `ciao <noun> <verb>" not in text
    assert "run_in_background" not in text
    assert "`Agent`/`Task`" not in text


def test_core_synopsis_only_offers_agent_allowed_task_move_columns() -> None:
    text = mi._system_instructions()
    move = next(line for line in text.splitlines() if line.startswith("task move "))
    assert "backlog|in_progress|in_review" in move
    assert "|done" not in move


def test_disclosed_memory_procedures_remain_available() -> None:
    from pathlib import Path

    root = Path(mi.__file__).parent / "stock/skills/ciao-memory"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    assert "Before document writes or bounded-region edits, read" in skill
    assert "(references/writing.md)" in skill
    procedures = (root / "references/writing.md").read_text(encoding="utf-8")
    for instruction in (
        "Separate entries with `§`",
        "[expires: YYYY-MM-DD]",
        "`README.md` first, then `<folder-name>.md`",
        "relative Markdown links",
        "preserve its frontmatter",
        "`ciao file surface",
    ):
        assert instruction in procedures, instruction


def test_system_prompt_points_operator_steps_to_sidebar_terminal() -> None:
    payload = mi.system_prompt_payload("")
    assert payload is not None
    text = payload["append"]
    for fact in ("Chat details → Open → Terminal", "engine's computer", "cannot drive", "bypass permissions", "dedicated sign-in", "`/mcp`", "navigation", "provider TUI"):
        assert fact in text
