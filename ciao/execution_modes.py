"""Provider-specific mappings and approval policies."""

from __future__ import annotations

import re
from pathlib import Path

# Permission-mode strength, weakest first: ``plan`` is read-only, ``normal``
# asks before acting, ``auto`` acts with safer defaults, ``bypass`` skips
# approvals. Mirrors ``BridgeMode`` in ciao.models. Shared by the control plane
# (an agent's ceiling) and the schedule dispatcher (a saved routine mode).
MODE_RANK: dict[str, int] = {"plan": 0, "normal": 1, "auto": 2, "bypass": 3}


def cap_schedule_mode(saved: str, default: str) -> str:
    """The mode a schedule run uses: its saved mode, never above ``default``.

    A saved mode is an upper limit, not a pin: when the workspace/engine default
    is lowered later, a routine an agent saved at a higher mode drops with it.
    An empty (or unrecognised) saved mode inherits ``default``.
    """
    if saved not in MODE_RANK or default not in MODE_RANK:
        return default
    return saved if MODE_RANK[saved] <= MODE_RANK[default] else default


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

_CREDENTIAL_DIR_NAMES = frozenset({".runtime", "secrets"})
# `.env`, `.env.local`, `.env-local`, `.env_prod`, `.envrc`; not `.envelope`.
_ENV_FILE_NAME = re.compile(r"\.env(rc)?([._-].*)?")


def is_credential_adjacent(path: Path) -> bool:
    """True when *path* (already resolved) names a credential store: a `.env`
    file or variant, or anything at any depth under a `.runtime` or `secrets`
    directory. Comparison is case-insensitive, matching `Path.match` on Windows
    and on case-insensitive macOS volumes. `Path.match` cannot express this: it
    is right-anchored and treats `**` as one segment (#1746)."""
    if _ENV_FILE_NAME.fullmatch(path.name.casefold()):
        return True
    return any(part.casefold() in _CREDENTIAL_DIR_NAMES for part in path.parts)


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
