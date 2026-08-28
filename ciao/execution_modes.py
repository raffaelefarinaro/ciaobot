"""Provider-specific mappings and approval policies."""

from __future__ import annotations

# ── Bundled harness skills Ciaobot replaces ──────────────────────────────
# The CLI ships a bundle of skills and workflows. Some expose surfaces that
# bypass Ciaobot entirely (cloud routines, harness cron loops, design-system
# sync, etc.) or duplicate a Ciaobot-managed surface (settings, permissions,
# diagnostics, project-specific run skills). Hiding them keeps the model from
# reaching for the wrong surface, and saves the per-turn context cost of their
# descriptions. Full rationale is in:
#   memory-vault/personal/Workspace/bundled-skills-evaluation.md
#
# Two independent levers, both applied:
#   * `skillOverrides` (CLI settings layer, via ``harness_skill_overrides``)
#     removes them from the model's context — verified against the bundled CLI:
#     they drop out of the init payload's `skills` and `slash_commands` lists
#     while every workspace skill and agent still resolves.
#   * `Skill(<name>)` deny rules (see ``_DEFAULT_HARNESS_DISALLOWED_TOOLS`` in
#     ciao/config.py, built from this tuple) block execution if one is somehow
#     re-enabled downstream. Deny alone was not enough: a denied skill still
#     shows up in the listing, so the model burns a turn on an approval card
#     for a tool it can't run.
HARNESS_DISABLED_SKILLS: tuple[str, ...] = (
    # Bundled surfaces that bypass Ciaobot's scheduler / UI.
    "schedule",  # claude.ai cloud routines (CronCreate, CronDelete, CronList)
    "loop",      # harness-local interval loop (ScheduleWakeup)
    # Bundled surfaces whose backend tool is already denied.
    "design-sync",  # DesignSync tool is in _DEFAULT_HARNESS_DISALLOWED_TOOLS.
    # Bundled surfaces that manage settings / permissions / diagnostics the
    # PWA already owns through a different path.
    "update-config",           # writes to settings.json; PWA Settings is the surface
    "fewer-permission-prompts",  # same, plus it derives from local transcripts
    "doctor",                  # duplicates PWA diagnostics; also survives kill switch
    # Bundled project-run surfaces. Ciaobot uses per-project .claude/skills/
    # equivalents, and these bundled stubs route the model away from them.
    "run",                  # no-op in most Ciaobot projects
    "run-skill-generator",  # creates per-project run skills we don't need here
    # Bundled dataviz guidance is for Claude Artifacts, while Ciaobot already
    # has its own .claude/skills/dataviz/ skill.
    "dataviz",
)

# Plugin skills (e.g. skill-creator@claude-plugins-official) are NOT affected by
# skillOverrides — both `disableBundledSkills` and per-skill overrides leave them
# in the skills listing. If one competes with a Ciao skill, the fix is either to
# uninstall the plugin or to shadow it with a same-named workspace skill.


def harness_skill_overrides() -> dict[str, str]:
    """``skillOverrides`` map hiding the skills Ciaobot supersedes.

    ``"off"`` removes a skill from both the model's listing and the user's
    slash-command picker. The CLI also accepts ``"user-invocable-only"``
    (hidden from the model, still typable by hand) — not used here, because a
    routine created that way is invisible to Ciaobot either way.
    """
    return {name: "off" for name in HARNESS_DISABLED_SKILLS}


# ── Ciaobot control-plane approval policy ────────────────────────────────
# Auto mode's SDK classifier escalates every MCP tool that isn't marked
# ``readOnlyHint`` to a manual approval card. For a third-party server that's
# the right default; for Ciaobot's own control plane it isn't. Each tool below
# is the programmatic twin of a button in the PWA, is bearer-token scoped to
# this instance, and lands in a UI where its effect is visible and reversible.
# Prompting "Approve use of mcp__ciaobot__loop_create?" one line after the user
# asked for a loop is friction with no safety value, so these names are handed
# to ``ClaudeAgentOptions.allowed_tools`` and never reach the PermissionGate.
#
# The cut is the ``_DESTRUCTIVE`` annotation in ``ciao/mcp_server.py``: deletes
# and lifecycle actions (``chat_delete``, ``project_delete``, ``chat_stop``,
# ``schedule_action``, ``loop_action``, ``project_complete``), plus
# ``background_run_start`` / ``background_run_cancel``, which execute and kill
# real commands, are deliberately absent and still raise a card.
# ``tests/test_mcp_server.py``
# cross-checks this tuple against the annotations declared on the tools, so a
# new tool fails the suite until its policy is decided here.
MCP_SERVER_NAME = "ciaobot"

AUTO_APPROVED_MCP_TOOLS: tuple[str, ...] = (
    # The lazy-discovery pair. tools_call only ever dispatches to the rest of
    # this tuple: it refuses every _DESTRUCTIVE tool, so its reach is exactly
    # the set already auto-approved here. See ciao/mcp_server.py.
    "tools_search",
    "tools_call",
    "context_get",
    "memory_status",
    "memory_update",
    "vault_search",
    "gws_status",
    "projects_list",
    "project_get",
    "project",
    "workspaces_list",
    "workspace_create",
    "chats_list",
    "chat_get",
    "chat_create",
    "chat_update",
    "chat_send",
    "chat_continue",
    "chat_retry",
    "chat_handover",
    "chat_fork",
    "chat_archive",
    # Only the read half of the background_run trio. Starting and cancelling a
    # command are ``_DESTRUCTIVE`` and still raise an approval card: an
    # auto-approved arbitrary-command tool would bypass the very classifier a
    # plain Bash call has to pass.
    "background_run_status",
    "schedules_list",
    "schedule",
    # Deprecated aliases onto interval schedules; still auto-approved while
    # they exist so a model reaching for the old name is not a friction wall.
    "loops_list",
    "loop",
    "file_surface",
)


def auto_approved_mcp_tool_names(server: str = MCP_SERVER_NAME) -> list[str]:
    """Fully-qualified SDK tool names for the auto-approved control plane."""
    return [f"mcp__{server}__{name}" for name in AUTO_APPROVED_MCP_TOOLS]
