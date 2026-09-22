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
# Since S6 every Ciaobot operation runs as `ciao <noun> <verb>` through the
# harness's own shell tool. The per-tool MCP annotations that used to feed a
# static allow-list are gone; the allow/ask split is now expressed as
# command-line prefix rules in AGENT_CLI_ALLOW_PATTERNS / AGENT_CLI_ASK_PATTERNS
# below, and the security floor stays server-side in the control plane (mode
# gates, `unattended_forbidden`, workspace confinement).
#
# The modes whose contract allows acting without asking, and therefore the
# only modes where the CLI allow-rules apply. `plan` is "propose, don't act" —
# an allow rule would punch a hole in it. `normal` is what the PWA labels
# "Manual — ask for every action", and pre-approving the list quietly broke
# that promise. Shared here so the Claude and opencode providers cannot drift
# apart on the carve-out.
CONTROL_PLANE_PREAPPROVED_MODES: frozenset[str] = frozenset({"auto", "bypass"})


# ── Agent CLI (`ciao <noun> <verb>`) harness rules ───────────────────────
# On the CLI surface there is no MCP server and therefore no per-tool
# annotation for the harness to read: every control-plane call is the harness's
# own shell tool. Claude's auto-mode classifier and opencode's `("bash", "ask")`
# auto default both then raise an approval card for work that used to be
# pre-approved — measured at 2–5 cards per opencode session in auto mode
# against 0–2 on MCP.
#
# These two tuples put the annotation back where the harness can see it: a
# command-line prefix per operation, split exactly the way the MCP-era
# ``AUTO_APPROVED_MCP_TOOLS`` used to split the tools. The ask class is every
# command whose operation carries the ``_DESTRUCTIVE`` annotation in
# ``ciao/mcp_server.py`` — ``chat_delete``, ``chat_stop``, ``project_action``
# (complete/delete), ``schedule_action``, ``background_run_start``,
# ``background_run_cancel`` — plus the deciding half of ``vault_review``, whose
# list/inspect verbs are reads while keep/trash/restore/delete are not.
#
# They are UX, not the security boundary. An argv prefix is bypassable (`sh -c`,
# a Python subprocess), so the floor stays server-side in the control plane
# (mode gates, ``unattended_forbidden``, workspace confinement). What these buy
# is that a cooperative agent doing routine work does not raise a card, and that
# a destructive verb still does.
#
# Entries are bare command prefixes; each provider adds its own glob syntax
# (:* for Claude's Bash rules, a trailing * for opencode's bash patterns).
# ``tests/test_agent_surface.py`` asserts every command in the ``ciao-cli``
# skill's ``commands.json`` is matched by exactly one entry across both tuples,
# so a new command fails the suite until its policy is decided here.
AGENT_CLI_ALLOW_PATTERNS: tuple[str, ...] = (
    # Verb-level, not `ciao memory`: the operator CLI also owns
    # `ciao memory-proposal-add` / `ciao memory-proposal-dismiss` (writes to the
    # proposals queue), and a bare `ciao memory` prefix glob covers those too.
    "ciao memory status",
    "ciao memory update",
    "ciao vault search",
    "ciao vault review list",
    "ciao vault review show",
    "ciao file",
    "ciao chat list",
    "ciao chat get",
    "ciao chat create",
    "ciao chat update",
    "ciao chat send",
    "ciao chat continue",
    "ciao chat retry",
    "ciao chat handover",
    "ciao chat archive",
    "ciao project list",
    "ciao project get",
    "ciao project create",
    "ciao project update",
    "ciao project restore",
    "ciao schedule list",
    "ciao schedule preview",
    "ciao schedule create",
    "ciao schedule update",
    "ciao run status",
    "ciao context",
    "ciao workspace list",
    "ciao gws status",
    # Not an operation: `ciao help` prints the bundled skill document without
    # touching the dispatcher, and asking for the reference is pure friction.
    "ciao help",
)

AGENT_CLI_ASK_PATTERNS: tuple[str, ...] = (
    "ciao vault review keep",
    "ciao vault review trash",
    "ciao vault review restore",
    "ciao vault review delete",
    "ciao chat delete",
    "ciao chat stop",
    "ciao project complete",
    "ciao project delete",
    "ciao schedule pause",
    "ciao schedule resume",
    "ciao schedule run",
    "ciao schedule delete",
    "ciao run start",
    "ciao run cancel",
)


def agent_cli_allowed_tool_rules() -> list[str]:
    """Claude ``allowed_tools`` entries pre-approving the non-destructive CLI.

    ``Bash(<prefix>:*)`` is the CLI's prefix-match rule form; an entry with a
    real specifier does not allow the whole ``Bash`` tool, so every other shell
    command still reaches the classifier. The SDK exposes only allow and deny
    lists, so the ask class is simply left out and falls through to the
    classifier, which is what raises the card.
    """
    return [f"Bash({pattern}:*)" for pattern in AGENT_CLI_ALLOW_PATTERNS]


def agent_cli_permission_rules() -> list[dict[str, str]]:
    """The same split as opencode session ``bash`` permission rules.

    Emitted after the mode's own ``("bash", "ask")`` row: opencode resolves
    rules last-match-wins, so a rule that must win goes later, not earlier.
    Ask rows come after the allow rows for the same reason, even though the
    prefixes are disjoint.

    The pattern is ``<prefix>*`` against the command line. If opencode's
    matcher turns out to be path-aware (``*`` not crossing ``/``), a command
    carrying a path — ``ciao file surface out/report.md`` — would miss its
    allow row and fall through to the generic ``("bash", "ask")``: one extra
    approval card, which is exactly today's behaviour, not a widened grant.
    The failure direction is safe either way; which one it is has to be read
    off a dev build, and the S1 re-measure is where that happens.
    """
    return [
        {"permission": "bash", "pattern": f"{pattern}*", "action": action}
        for action, patterns in (
            ("allow", AGENT_CLI_ALLOW_PATTERNS),
            ("ask", AGENT_CLI_ASK_PATTERNS),
        )
        for pattern in patterns
    ]


# ── Credential and runtime-state path denies ─────────────────────────────
# The one carve-out no permission mode may buy its way out of, shared here so
# the Claude and opencode providers cannot drift apart on it (same reason as
# CONTROL_PLANE_PREAPPROVED_MODES above).
#
# Why these paths. ``<workspace_root>/.env`` holds ``PWA_AUTH_TOKEN``; a model
# that reads it can POST it to ``/api/auth/login`` (ciao/web/routes_auth.py)
# and get an owner session cookie, which carries the full PWA REST API —
# strictly wider than the chat-scoped MCP bearer token the same process was
# handed, and it reaches every route the MCP catalog deliberately withholds
# (workspace delete, deploy, browser-session admin). ``.runtime/`` holds the
# chat/project/schedule stores the control plane guards behind
# ``runtime_file_forbidden`` (ciao/control_plane.py), so a direct write walks
# around the control plane rather than through it. ``secrets/`` is OAuth
# material.
#
# Anchored with ``**`` rather than at the workspace root. opencode resolves a
# tool's path to an absolute one before matching a rule, so a relative
# ``.runtime/**`` would never match; and for a deny, also catching a nested
# ``.env`` deeper in the tree is the conservative direction. The cost is that
# an agent asked to inspect some *other* project's ``.env`` is refused too.
#
# ``.env.*`` is deliberately NOT here. This repo ships ``.env.example`` and the
# release pipeline copies it (``ciao/public_release.py``), and the two existing
# ``.env.*`` guards in this codebase both carve the template names out
# (``git_sync._protected_path``, ``local_session.py``) — a template is not a
# secret is an established convention here. A glob deny cannot express "all of
# ``.env.*`` except these four", and since this list is prepended outside the
# workspace extras there would be no operator opt-out either. The credential
# that motivates this list lives in exactly one file, ``<workspace_root>/.env``
# (``mcp_server._workspace_env_path``), so that is what is denied.
#
# What this does NOT cover, stated plainly. First, the shell: Claude's
# ``Bash(cmd:*)`` rules are prefix matches on the command line and opencode's
# ``bash`` pattern likewise matches the command, not a path — no glob can
# path-scope a shell. Closing that needs a sandbox, not a denylist. Second,
# ``**/.runtime/**`` is a *name*, while the real runtime root is operator-
# configurable through ``CIAO_RUNTIME_ROOT``. Callers that can resolve it pass
# it in (see the ``runtime_root`` argument below); the static pattern alone
# only covers the default layout.
CREDENTIAL_DENY_PATTERNS: tuple[str, ...] = (
    "**/.env",
    "**/.runtime/**",
    "**/secrets/**",
)


def _runtime_root_patterns(runtime_root: object) -> tuple[str, ...]:
    """Absolute deny patterns for a resolved, non-default runtime root.

    ``CIAO_RUNTIME_ROOT`` can put ``state.json`` and the chat/project/schedule
    stores anywhere, including outside the workspace tree, where no
    ``**/.runtime/**`` pattern reaches them. A caller that has resolved the
    real root hands it over and gets a pattern that matches it exactly, so the
    guarantee stops depending on the directory being *named* ``.runtime``.
    """
    if not runtime_root:
        return ()
    root = str(runtime_root).rstrip("/")
    if not root:
        return ()
    return (root, f"{root}/**")

# Claude's native file tools, as ``disallowed_tools`` names them. ``Glob`` and
# ``Grep`` are included so the paths do not leak through a listing either.
CLAUDE_CREDENTIAL_DENY_TOOLS: tuple[str, ...] = (
    "Read",
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookRead",
    "Glob",
    "Grep",
)

# The same tools under opencode's lowercase permission names.
OPENCODE_CREDENTIAL_DENY_PERMISSIONS: tuple[str, ...] = (
    "read",
    "edit",
    "write",
    "patch",
    "glob",
    "grep",
    "list",
)


def credential_path_deny_rules(runtime_root: object = None) -> tuple[str, ...]:
    """``Tool(pattern)`` deny rules for Claude's ``disallowed_tools``.

    Unconditional: unlike the harness denylist these are NOT part of the
    per-workspace ``disallowed_tools`` default, so neither an operator's custom
    "Extra disallowed tools" list nor the literal ``none`` opt-out clears them.
    A denylist escape hatch that also unlocks ``PWA_AUTH_TOKEN`` is not an
    escape hatch anyone asked for.

    ``runtime_root`` is the resolved runtime directory when the caller knows
    it; see :func:`_runtime_root_patterns`.
    """
    patterns = (*CREDENTIAL_DENY_PATTERNS, *_runtime_root_patterns(runtime_root))
    return tuple(
        f"{tool}({pattern})"
        for tool in CLAUDE_CREDENTIAL_DENY_TOOLS
        for pattern in patterns
    )


def opencode_credential_deny_rules(runtime_root: object = None) -> list[dict[str, str]]:
    """The same denies as opencode session permission rules.

    Appended last by ``ciao.providers.opencode.mode_settings``: opencode
    resolves rules last-match-wins, so placed first the wildcard ``allow`` in
    ``auto``/``bypass`` would override them.
    """
    patterns = (*CREDENTIAL_DENY_PATTERNS, *_runtime_root_patterns(runtime_root))
    return [
        {"permission": permission, "pattern": pattern, "action": "deny"}
        for permission in OPENCODE_CREDENTIAL_DENY_PERMISSIONS
        for pattern in patterns
    ]
