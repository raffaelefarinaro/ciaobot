"""Provider-specific mappings and approval policies."""

from __future__ import annotations

from pathlib import Path

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
# static allow-list are gone. No `ciao …` argv prefix is pre-approved: an
# allow rule is a prefix that a shell suffix (``ciao help >/dev/null; <cmd>``)
# could ride past, so auto mode keeps a card on every shell command and users
# who want no cards switch to `bypass`. The security floor stays server-side in
# the control plane (mode gates, ``unattended_forbidden``, workspace
# confinement).


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
# release pipeline copies it (``ciao/public_release.py``), and the existing
# ``.env.*`` guards in this codebase carve the template names out
# (``local_session.py``, ``public_release.py``) — a template is not a
# secret is an established convention here. A glob deny cannot express "all of
# ``.env.*`` except these four", and since this list is prepended outside the
# workspace extras there would be no operator opt-out either. The credential
# that motivates this list lives in exactly one file, ``<workspace_root>/.env``
# (``mcp_server._workspace_env_path``), so that is what is denied.
#
# What this does NOT cover, stated plainly. First, the shell: Claude's
# ``Bash(cmd:*)`` and OpenCode 2's ``shell`` resources match the command, not a
# path, so no glob can path-scope a shell. Closing that needs a sandbox, not a
# denylist. Second,
# ``**/.runtime/**`` is a *name*, while the real runtime root is operator-
# configurable through ``CIAO_RUNTIME_ROOT``. Callers that can resolve it pass
# it in (see the ``runtime_root`` argument below); the static pattern alone
# only covers the default layout.
CREDENTIAL_DENY_PATTERNS: tuple[str, ...] = (
    # V2 read/edit resources are location-relative; keep root-relative forms
    # alongside recursive forms because its matcher does not treat ** as a
    # match for the root itself.
    ".env",
    "./.env",
    "**/.env",
    ".runtime",
    "./.runtime",
    "**/.runtime",
    ".runtime/**",
    "./.runtime/**",
    "**/.runtime/**",
    "secrets",
    "./secrets",
    "**/secrets",
    "secrets/**",
    "./secrets/**",
    "**/secrets/**",
)


def _runtime_root_patterns(
    runtime_root: object,
    workspace_root: object = None,
) -> tuple[str, ...]:
    """Return absolute and, when known, V2-relative runtime deny patterns.

    ``CIAO_RUNTIME_ROOT`` can put ``state.json`` and the chat/project/schedule
    stores anywhere, including outside the workspace tree, where no
    ``**/.runtime/**`` pattern reaches them. A caller that has resolved the
    real root hands it over and gets a pattern that matches it exactly, so the
    guarantee stops depending on the directory being *named* ``.runtime``.
    OpenCode 2 represents internal resources relative to the session location,
    so a root inside the workspace also needs the corresponding relative
    spellings; absolute rules alone do not match those resources.
    """
    if not runtime_root:
        return ()
    root = str(runtime_root).rstrip("/")
    if not root:
        return ()
    patterns = [root, f"{root}/**"]
    if not workspace_root:
        return tuple(patterns)

    try:
        root_path = Path(root).expanduser()
        resolved_root = root_path.resolve()
        workspace_path = Path(str(workspace_root)).expanduser().resolve()
        candidates = (root_path, resolved_root)
    except (OSError, RuntimeError, ValueError):
        return tuple(patterns)

    relative_patterns: list[str] = []
    for candidate in candidates:
        try:
            relative = candidate.relative_to(workspace_path).as_posix()
        except ValueError:
            continue
        if relative in {"", "."}:
            # If the runtime root is the session location itself, fail closed
            # for every internal resource rather than leave a spelling gap.
            relative_patterns.extend((".", "**"))
        else:
            relative_patterns.extend((relative, f"{relative}/**"))
    if relative_patterns:
        patterns.extend(relative_patterns)
    return tuple(dict.fromkeys(patterns))

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

# The same coverage under OpenCode 2's permission action names. V2
# consolidated write/patch into edit. Its glob/grep resources are search
# patterns rather than file paths, so these rules are defense-in-depth for
# targeted searches; they are not a shell or arbitrary-regex sandbox.
OPENCODE_CREDENTIAL_DENY_PERMISSIONS: tuple[str, ...] = (
    "read",
    "edit",
    "glob",
    "grep",
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


def opencode_credential_deny_rules(
    runtime_root: object = None,
    workspace_root: object = None,
) -> list[dict[str, str]]:
    """The same denies as OpenCode session permission rules.

    Appended last by ``ciao.providers.opencode.mode_settings``: OpenCode
    resolves rules last-match-wins, so placed first the wildcard ``allow`` in
    ``auto``/``bypass`` would override them. ``workspace_root`` enables the
    location-relative aliases required by OpenCode 2's internal resources.
    """
    patterns = (
        *CREDENTIAL_DENY_PATTERNS,
        *_runtime_root_patterns(runtime_root, workspace_root),
    )
    return [
        {"action": action, "resource": resource, "effect": "deny"}
        for action in OPENCODE_CREDENTIAL_DENY_PERMISSIONS
        for resource in patterns
    ]
