"""Configuration loading for the Ciaobot server."""

from __future__ import annotations

import logging
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

from ciao.execution_modes import HARNESS_DISABLED_SKILLS, normalize_claude_mode
from ciao.models import BridgeMode
from ciao.providers.codex import CodexSettings
from ciao.providers.opencode import OpencodeSettings


# Harness tools that are irrelevant inside the Ciaobot PWA regardless of
# workspace. The PWA is the notification, plan-approval, and scheduling
# surface, so the CLI's own plan-mode, cron, /loop wakeup, routine-trigger,
# desktop/phone push, notebook-edit, and design-system-sync tools just
# duplicate it (or have no UI to render them). Denied as bare tool names so
# their definitions drop out of the model payload, shrinking every request.
# See "What I'd do next" in ciao-improvements and the aihero.dev
# "kill the bloat in Claude Code's system prompt" guide for the rationale.
_DEFAULT_HARNESS_DISALLOWED_TOOLS: tuple[str, ...] = (
    "EnterPlanMode",
    "ExitPlanMode",
    "DesignSync",
    "NotebookEdit",
    "CronCreate",
    "CronDelete",
    "CronList",
    "ScheduleWakeup",
    "PushNotification",
    "RemoteTrigger",
    # The CLI's bundled `schedule` / `loop` skills. They're already hidden
    # from the model by the `skillOverrides` settings layer (see
    # HARNESS_DISABLED_SKILLS in ciao/execution_modes.py for why); these
    # `Skill(<name>)` deny rules are the second lever, blocking execution
    # ("Skill execution blocked by permission rules") if one is re-enabled
    # downstream.
    *(f"Skill({name})" for name in HARNESS_DISABLED_SKILLS),
)

# The self-hosted n8n MCP used to be denied by default in a workspace literally
# named ``personal``. Ciaobot no longer ships an opinion about it: n8n is
# project-scoped in ``.mcp.json``, so it exists only where someone configured it
# deliberately, and *which* workspaces should see it is a per-workspace
# preference — which is exactly what the "Extra disallowed tools" field is for.
#
# Keying it on a name meant any other private workspace went unprotected, and
# universalising the deny instead gave users a documented escape hatch that did
# not work: clearing the field sends null, which restores the defaults, and the
# value that does clear them drops the harness denies too.


def _clean_relative_path(raw: str) -> str:
    """Normalize a safe relative path so equivalent spellings resolve alike.

    ``research``, ``research/`` and ``./research`` all name one segment; without
    this they took different branches and two of them put the vault outside the
    vault root. The free-text "Vault name" field in Settings invites exactly
    that trailing slash.

    Absolute paths are preserved for setup-created external vaults. Relative
    paths may never contain ``..``: a workspace registry is trusted
    configuration, but resolving a malformed value outside ``workspace_root``
    would turn one bad setting into filesystem-wide reads and writes.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    # Setup uses "." deliberately when the selected notes folder is both the
    # operational workspace and the vault. It is a safe, exact location (the
    # resolved workspace root), not an empty or traversal path.
    if cleaned in {".", "./"}:
        return "."
    path = Path(cleaned)
    if path.is_absolute():
        return str(path)
    parts = [part for part in path.parts if part not in {".", ""}]
    if ".." in parts:
        raise ValueError("relative vault_root must not contain '..'")
    return str(Path(*parts)) if parts else ""


def _looks_like_vault(path: Path) -> bool:
    """Whether a directory is plausibly a workspace vault, not a stray folder."""
    try:
        if not path.is_dir():
            return False
    except OSError:
        return False
    return any(
        (
            (path / "MEMORY.md").is_file(),
            (path / "INDEX.md").is_file(),
            (path / "Workspace").is_dir(),
            (path / "projects").is_dir(),
            (path / "Logs").is_dir(),
        )
    )


def _vault_evidence_score(path: Path) -> int:
    """Rank vault evidence when both legacy and standard locations exist."""
    if not _looks_like_vault(path):
        return 0
    score = 0
    score += 8 if (path / "MEMORY.md").is_file() else 0
    score += 4 if (path / "INDEX.md").is_file() else 0
    score += 4 if (path / "projects").is_dir() else 0
    score += 2 if (path / "Logs").is_dir() else 0
    score += 1 if (path / "Workspace").is_dir() else 0
    return score


# Accent presets for the PWA. Missing/unknown values resolve to pink
# (Ciao brand). Only accents shift; canvas tokens stay stable.
WORKSPACE_COLOR_IDS = ("pink", "cyan", "amber", "emerald", "violet")
DEFAULT_WORKSPACE_COLOR = "pink"


def coerce_workspace_color(raw: object) -> str:
    """Normalize a workspace accent id. Empty/missing → pink."""
    if raw is None:
        return DEFAULT_WORKSPACE_COLOR
    cleaned = str(raw).strip().lower()
    if not cleaned:
        return DEFAULT_WORKSPACE_COLOR
    if cleaned in WORKSPACE_COLOR_IDS:
        return cleaned
    raise ValueError(
        f"color must be one of: {', '.join(WORKSPACE_COLOR_IDS)}"
    )


@dataclass(slots=True)
class WorkspaceConfig:
    """Config for one logical chat workspace."""

    name: str
    vault_root: str
    default_provider: str = "claude"
    default_model: str = ""
    # Extra tools to deny (e.g. ``mcp__n8n_mcp``, ``Bash``). ``None`` = use
    # the per-workspace default extras; ``[]`` = explicit opt-out (no extras).
    disallowed_tools: list[str] | None = None
    gws_profile: str = ""
    # PWA accent preset id. Defaults to Ciao pink.
    color: str = DEFAULT_WORKSPACE_COLOR


def _coerce_workspace_disallowed(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return _parse_disallowed_tools(raw)
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return None


def _workspace_from_mapping(data: dict) -> WorkspaceConfig | None:
    name = str(data.get("name", "")).strip()
    if not name:
        return None
    vault_root = str(data.get("vault_root", name)).strip() or name
    try:
        color = coerce_workspace_color(data.get("color"))
    except ValueError:
        color = DEFAULT_WORKSPACE_COLOR
    return WorkspaceConfig(
        name=name,
        vault_root=vault_root,
        default_provider=str(data.get("default_provider", "claude")).strip() or "claude",
        default_model=str(data.get("default_model", "")).strip(),
        disallowed_tools=_coerce_workspace_disallowed(data.get("disallowed_tools")),
        gws_profile=str(data.get("gws_profile", "")).strip(),
        color=color,
    )


def _parse_workspaces_json(raw: str) -> dict[str, WorkspaceConfig]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logging.getLogger(__name__).warning("CIAO_WORKSPACES is not valid JSON")
        return {}
    items: list[dict]
    if isinstance(parsed, dict):
        items = [
            {"name": name, **value}
            for name, value in parsed.items()
            if isinstance(value, dict)
        ]
    elif isinstance(parsed, list):
        items = [item for item in parsed if isinstance(item, dict)]
    else:
        return {}
    out: dict[str, WorkspaceConfig] = {}
    for item in items:
        workspace = _workspace_from_mapping(item)
        if workspace is not None:
            out[workspace.name] = workspace
    return out


def _legacy_workspaces(
    *,
    default_model_personal: str = "",
    default_model_work: str = "",
    disallowed_tools_personal: list[str] | None = None,
    disallowed_tools_work: list[str] | None = None,
    gws_default_profile: str = "personal",
) -> dict[str, WorkspaceConfig]:
    """Current private-layout defaults until callers fully support N workspaces."""
    return {
        "personal": WorkspaceConfig(
            name="personal",
            vault_root="personal",
            default_provider="claude",
            default_model=default_model_personal,
            disallowed_tools=disallowed_tools_personal,
            gws_profile=gws_default_profile or "personal",
        ),
        "work": WorkspaceConfig(
            name="work",
            vault_root="work",
            default_provider="claude",
            default_model=default_model_work,
            disallowed_tools=disallowed_tools_work,
            gws_profile="work",
        ),
    }


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_disallowed_tools(raw: str) -> list[str] | None:
    """Parse a CSV denylist. Empty/missing → None (use defaults);
    ``"none"`` → ``[]`` (explicit opt-out); CSV → parsed list.

    The None vs []-empty distinction matters because every workspace has
    built-in defaults (the harness tool denylist). Operators who want zero
    denylist set the literal ``"none"``.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.lower() == "none":
        return []
    return _split_csv(cleaned)


def _env(source: Mapping[str, str], new_name: str, old_name: str, default: str = "") -> str:
    """Read env var with fallback to old TELEGRAM_BRIDGE_* name for migration."""
    return source.get(new_name, "").strip() or source.get(old_name, "").strip() or default


def _bootstrap_workspace(source: Mapping[str, str]) -> Path:
    raw = source.get("CIAO_BOOTSTRAP_WORKSPACE", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    home = Path(source.get("HOME", str(Path.home()))).expanduser()
    return (home / ".ciao" / "bootstrap").resolve()


def _read_or_create_secret(path: Path) -> str:
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n", encoding="utf-8")
    return token


@dataclass(slots=True)
class CiaoConfig:
    """Environment-backed configuration."""

    pwa_auth_token: str
    workspace_root: Path
    state_path: Path
    media_root: Path
    # Real installs get this from ``from_env``, which defaults it to True (see
    # there); the field default only covers configs built directly in code.
    pwa_auth_required: bool = False
    # Extra origins accepted for state-changing HTTP + WebSocket handshakes when
    # the app is reached under a host it doesn't bind to (reverse proxy / tunnel
    # / host alias). Bare hostnames or full origins; from CIAO_ALLOWED_ORIGINS.
    # A proxy-supplied X-Forwarded-Host is honored too (browsers can't forge it
    # on a handshake, so it's safe against cross-site WS hijacking).
    pwa_allowed_origins: tuple[str, ...] = ()
    dev_mode: bool = False
    # Path to the Ciaobot source checkout for developer-only deploy/restart
    # workflows. Packaged apps update the app bundle atomically instead. From
    # CIAO_APP_REPO.
    app_repo: Path | None = None
    vault_mode: str = "scratch"
    bootstrap_mode: bool = False
    vault_root: Path = Path("memory-vault")
    max_image_size_bytes: int = 10 * 1024 * 1024
    max_voice_size_bytes: int = 25 * 1024 * 1024
    media_ttl_hours: int = 72
    # Titling uses the Claude Agent SDK's one-shot query(); default Haiku.
    title_model: str = "haiku"
    # Operator override for the titling model, set from the PWA Settings →
    # Models tab (runtime settings store) or ``CIAO_TITLE_MODEL_OVERRIDE``.
    # Empty = automatic routing: the workspace's haiku-tier model.
    title_model_override: str = ""
    # Apple Intelligence (the "Local (free)" on-device model) is a beta feature,
    # off by default. Opt-in from Settings → Models, or an operator can flip the
    # default from the env with ``CIAO_APPLE_INTELLIGENCE=1``. When off, the
    # "apple" sentinel is treated as an unavailable backend and every routine
    # falls back to its cloud model.
    apple_intelligence_enabled: bool = False
    # BCP-47 language for the on-device voice engines. Dictation needs a
    # matching language installed in System Settings → Keyboard → Dictation;
    # the synthesizer uses it to choose a voice.
    transcription_locale: str = "en-US"
    # macOS voice identifier or name for read-aloud. Empty means "the best
    # installed voice for transcription_locale" -- the right default when the
    # available voices differ on every machine.
    tts_local_voice: str = ""
    claude_models: list[str] = field(default_factory=lambda: ["opus", "sonnet", "haiku"])
    claude_default_model: str = "opus"
    # Per-workspace default models. Empty string falls back to
    # claude_default_model, so one workspace can prefer a cheaper tier
    # than another.
    default_model_personal: str = ""
    default_model_work: str = ""
    # Per-workspace tool denylists (the "extra" tools beyond the default
    # harness set). Forwarded to ``ClaudeAgentOptions.disallowed_tools`` for the
    # spawned CLI subprocess, so a personal chat can't accidentally touch a
    # work-only MCP (and vice versa). ``None`` = "unset, use built-in
    # defaults"; explicit ``[]`` = "operator opted out of the defaults".
    disallowed_tools_personal: list[str] | None = None
    disallowed_tools_work: list[str] | None = None
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    _workspace_registry_changed: bool = field(
        init=False, default=False, repr=False
    )
    claude_mode: BridgeMode = "auto"
    # Per-provider default execution mode for new chats, set from the PWA
    # Settings → Providers tab (runtime settings store). A missing entry uses
    # the built-in default: normal for opencode (approval-enforcing),
    # otherwise ``claude_mode``.
    provider_default_modes: dict[str, str] = field(default_factory=dict)
    restart_exit_code: int = 75
    auto_sync_on_start: bool = False
    auto_vault_index: bool = True
    auto_update_github_skills: bool = False
    pwa_port: int = 8443
    pwa_host: str = "127.0.0.1"
    gws_default_profile: str = "personal"
    # Per-tier Codex model pins set from the PWA Settings → Providers tab.
    # Empty means automatic: tiers derive from the signed-in account's
    # model catalog (luna→haiku, terra→sonnet, sol→opus/fable).
    codex: CodexSettings = field(default_factory=CodexSettings)
    # Per-tier opencode model pins, same shape and meaning as the Codex ones.
    # Empty means the tier falls through to the session provider's own model.
    opencode: OpencodeSettings = field(default_factory=OpencodeSettings)
    # Post-archive insights extraction: when a chat is archived, run the raw
    # Claude Code session JSONL through a fast cheap model and append a
    # `## Session insights` section to the archived markdown.
    insights_enabled: bool = True
    # Skip only single-shot chats by default; multi-turn chats have enough
    # context to be useful for durable insight extraction.
    insights_size_gate_turns: int = 2
    # Fallback when session insights run without workspace context (e.g.
    # ``scripts/backfill_insights.py``). Live archives use
    # :func:`ciao.insights.resolve_insights_model` instead.
    insights_model: str = "sonnet"
    # Operator override for the insights model, set from the PWA Settings →
    # Models tab (runtime settings store) or ``CIAO_INSIGHTS_MODEL``.
    # Empty = automatic routing: the workspace's sonnet-tier model.
    insights_model_override: str = ""
    # Asynchronously backfill missing insights on server startup.
    # Enable with ``CIAO_INSIGHTS_BACKFILL_ON_STARTUP=1``.
    insights_backfill_on_startup: bool = False
    # Trajectory capture: when a chat is archived, also write a structured
    # JSON record of skills loaded, tools used, errors, decisions, and the
    # outcome to ``~/.ciao/trajectories/YYYY-MM/<session-id>.json``. The
    # weekly ``ciao.skill_evolution`` pass mines this directory.
    # Disable with ``CIAO_TRAJECTORIES_DISABLED=1``.
    trajectories_enabled: bool = True
    trajectory_retention_months: int = 6
    # Skill evolution scheduled pass. The schedule entry itself is the
    # primary on/off switch; this flag exists so ops can hard-disable from
    # the env (``CIAO_SKILL_EVOLUTION_DISABLED=1``) without editing
    # schedules.json.
    skill_evolution_enabled: bool = True

    # Comma-separated list of models for the adversarial_review MCP tool.
    # Empty string defaults to the script's built-in panel.
    critique_models: str = ""
    # Advisory caps for the ``ciao:memory`` / ``ciao:profile`` regions in
    # the workspace CLAUDE.md. Injected as a frozen snapshot into Claude and
    # Codex system prompts at session start; edited with Edit on the guide.
    # See ``ciao/memory_injector.py`` and ``ciao/memory_tool.py``.
    memory_char_limit: int = 2200
    user_char_limit: int = 1375
    # Ciaobot's managed agent control plane. MCP is the only control surface;
    # the legacy CLI path survives only as a runtime degrade when the MCP
    # server is unavailable at request time.
    mcp_enabled: bool = True
    control_surface: str = "mcp"
    # Internal evaluation mode.  It keeps the HTTP/chat stack identical while
    # suppressing autonomous background work that would contaminate paired
    # legacy-vs-MCP measurements.  Manual schedules/loops remain available.
    benchmark_mode: bool = False

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.state_path = Path(self.state_path).expanduser().resolve()
        self.media_root = Path(self.media_root).expanduser().resolve()
        vault_root = Path(self.vault_root).expanduser()
        if not vault_root.is_absolute():
            vault_root = self.workspace_root / vault_root
        self.vault_root = vault_root.resolve()
        # "auto" was the user-facing A/B benchmark option and is gone. The
        # runtime MCP-degrade path still sets the legacy literal internally,
        # so legacy stays a valid config value alongside the mcp default.
        if self.control_surface not in {"legacy", "mcp"}:
            self.control_surface = "legacy"
        if not self.workspaces:
            self.workspaces = _legacy_workspaces(
                default_model_personal=self.default_model_personal,
                default_model_work=self.default_model_work,
                disallowed_tools_personal=self.disallowed_tools_personal,
                disallowed_tools_work=self.disallowed_tools_work,
                gws_default_profile=self.gws_default_profile,
            )
        self._workspace_registry_changed = self._normalize_workspace_vault_roots()

    def workspace(self, name: str | None) -> WorkspaceConfig | None:
        if not name:
            return None
        return self.workspaces.get(name)

    def workspace_names(self) -> list[str]:
        return list(self.workspaces.keys())

    def primary_workspace(self) -> str:
        """The workspace to use when a caller has no better idea.

        Prefers one literally named ``personal`` for continuity with installs
        that predate a configurable registry, then falls back to whatever is
        registered first. Callers must not hardcode ``"personal"`` themselves:
        workspace names are the user's, and an install may have none by that
        name at all.
        """
        if "personal" in self.workspaces:
            return "personal"
        names = self.workspace_names()
        return names[0] if names else ""

    def legacy_entity_workspace(self) -> str:
        """Workspace that owns unprefixed entries in the global vault index.

        First-run setup historically pointed the user's chosen logical
        workspace at ``CIAO_VAULT_ROOT`` itself. If a workspace literally named
        ``personal`` is added later, it must not steal those legacy entities
        merely because :meth:`primary_workspace` prefers that name.
        """
        owners: list[str] = []
        for name in self.workspace_names():
            try:
                if self.workspace_vault_root(name) == self.vault_root:
                    owners.append(name)
            except ValueError:
                continue
        if len(owners) == 1:
            return owners[0]
        if len(owners) > 1:
            logger.warning(
                "Legacy entity ownership is ambiguous across workspaces: %s",
                ", ".join(owners),
            )
            return ""
        return self.primary_workspace()

    def workspace_vault_root(self, workspace: str | None) -> Path:
        """Absolute vault directory for one logical workspace.

        Pure: the answer depends only on the registry, never on what happens to
        exist on disk right now. An earlier version chose between two candidate
        paths by probing the filesystem on every call, which meant an install's
        vault silently relocated the moment the other candidate appeared —
        stranding everything written at the first one. Legacy layouts are
        reconciled once, at load (see ``_normalize_workspace_vault_roots``).

        Registered ``vault_root`` shapes:

        - absolute — preserved setup/external roots and pinned legacy vaults.
        - ``.`` — existing-folder setup where the operational workspace itself
          is the vault.
        - one path segment — a legacy ambiguous value, normalized and persisted
          at load.
        - several segments (normally ``memory-vault/clientA``) — the standard
          named workspace path, resolved against ``workspace_root`` so it is not
          nested twice.
        """
        name = workspace or ""
        workspace_config = self.workspace(name)
        raw_root = (workspace_config.vault_root if workspace_config else name) or name
        return self._resolve_vault_root(raw_root)

    def canonical_workspace_vault_root(self, workspace: str) -> Path:
        """Standard location for a user-named workspace under the vault."""
        name = _clean_relative_path(workspace)
        if not name or len(Path(name).parts) != 1:
            raise ValueError("workspace name must identify one vault folder")
        candidate = self.vault_root / name
        if candidate.is_symlink():
            raise ValueError("workspace vault folder must not be a symlink")
        resolved = candidate.resolve()
        if resolved.parent != self.vault_root:
            raise ValueError("workspace vault folder must stay inside the vault root")
        if resolved.exists() and not resolved.is_dir():
            raise ValueError("workspace vault folder must be a directory")
        return resolved

    def stored_workspace_vault_root(self, workspace: str) -> str:
        """Portable registry value for a workspace's standard vault folder."""
        root = self.canonical_workspace_vault_root(workspace)
        try:
            return str(root.relative_to(self.workspace_root))
        except ValueError:
            return str(root)

    def _resolve_vault_root(self, raw_root: str) -> Path:
        cleaned = _clean_relative_path(raw_root)
        if not cleaned:
            raise ValueError("vault_root must not be empty")
        root = Path(cleaned).expanduser()
        if root.is_absolute():
            resolved = root.resolve()
            if resolved == Path(resolved.anchor):
                raise ValueError("vault_root must not be the filesystem root")
            # Absolute compatibility roots are canonicalized when the registry
            # loads. If that pinned path (or one of its descendants below the
            # already-resolved workspace root) later becomes a symlink, do not
            # silently redirect agent reads and writes to a different vault.
            if resolved != root:
                raise ValueError("workspace vault path must not contain symlinks")
            return resolved
        if len(root.parts) == 1:
            candidate = self.vault_root / root
            if candidate.is_symlink():
                raise ValueError("workspace vault folder must not be a symlink")
            resolved = candidate.resolve()
            if resolved.parent != self.vault_root:
                raise ValueError(
                    "workspace vault folder must stay inside the vault root"
                )
            return resolved
        candidate = self.workspace_root / root
        resolved = candidate.resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("vault_root must not be the filesystem root")
        if resolved != candidate:
            raise ValueError("workspace vault path must not contain symlinks")
        if not resolved.is_relative_to(self.workspace_root):
            raise ValueError("relative vault_root must stay inside workspace_root")
        return resolved

    def _normalize_workspace_vault_roots(self) -> bool:
        """Pin a legacy vault's location into the registry, once.

        Before vault nesting applied to every workspace, a workspace named
        anything but ``personal``/``work`` kept its vault beside ``memory-vault/``
        rather than inside it. Such an install must not appear to lose its data,
        but it also must not have its location re-decided on every call. So the
        one-segment ``vault_root`` is rewritten to the absolute legacy path here:
        resolution downstream stays pure, and the change is visible in
        ``.runtime/workspaces.json`` the next time it is written.

        The legacy directory has to actually look like a vault. Gating on mere
        existence would capture an unrelated sibling — naming a workspace after
        a ``clients/`` or ``skills/`` folder that already sits in the workspace
        root would silently adopt it, and the agent would then write memory into
        someone's document folder.
        """
        changed = False
        for name, workspace_config in list(self.workspaces.items()):
            try:
                raw_root = _clean_relative_path(workspace_config.vault_root or name)
                if not raw_root:
                    raise ValueError("vault_root must not be empty")
                canonical = self.canonical_workspace_vault_root(name)
            except ValueError:
                logger.warning(
                    "Workspace %s has an unsafe vault_root %r; using its "
                    "standard folder under %s",
                    name,
                    workspace_config.vault_root,
                    self.vault_root,
                )
                try:
                    canonical = self.canonical_workspace_vault_root(name)
                except ValueError:
                    continue
                workspace_config.vault_root = self.stored_workspace_vault_root(name)
                changed = True
                continue
            root = Path(raw_root).expanduser()
            if root.is_absolute():
                absolute = root.resolve()
                if absolute == Path(absolute.anchor):
                    workspace_config.vault_root = (
                        self.stored_workspace_vault_root(name)
                    )
                    changed = True
                    continue
                # Preserve a setup-selected symlink by pinning its current
                # target. Resolution can then fail closed if the pinned path
                # itself is replaced by a symlink after startup/restart.
                if workspace_config.vault_root != str(absolute):
                    workspace_config.vault_root = str(absolute)
                    changed = True
                # Backward compatibility for an older/manual sibling move:
                # when the pinned source disappeared and the standard folder
                # now has vault evidence, follow the completed move. The
                # current guided migration updates the registry explicitly.
                if (
                    absolute != self.vault_root
                    and absolute.parent == self.workspace_root
                    and not absolute.exists()
                    and _looks_like_vault(canonical)
                ):
                    workspace_config.vault_root = self.stored_workspace_vault_root(name)
                    changed = True
                continue
            if len(root.parts) != 1:
                candidate = self.workspace_root / root
                absolute = candidate.resolve()
                if absolute == Path(absolute.anchor):
                    workspace_config.vault_root = (
                        self.stored_workspace_vault_root(name)
                    )
                    changed = True
                    continue
                # A relative setup path may run through the configured vault
                # alias (for example memory-vault -> an external notes
                # directory). Pin the alias's current target exactly once;
                # later resolver calls can then reject any replacement without
                # breaking an intentional symlink that existed at setup time.
                if absolute != candidate:
                    workspace_config.vault_root = str(absolute)
                    changed = True
                continue
            legacy = (self.workspace_root / root).resolve()
            if legacy == self.vault_root:
                # A one-segment value equal to CIAO_VAULT_ROOT is the
                # setup-era "this workspace owns the whole vault" shape. Pin
                # it absolutely; leaving it as ``memory-vault`` would make the
                # generic one-segment resolver nest it a second time.
                stored = str(legacy)
                if workspace_config.vault_root != stored:
                    workspace_config.vault_root = stored
                    changed = True
                continue
            if legacy == canonical:
                continue
            if (
                _looks_like_vault(legacy)
                and _vault_evidence_score(legacy)
                >= _vault_evidence_score(canonical)
            ):
                workspace_config.vault_root = str(legacy)
                changed = True
                logger.info(
                    "Workspace %s keeps its vault at the pre-nesting location "
                    "%s; pinned in the registry until an interactive migration "
                    "moves it under %s.",
                    name,
                    legacy,
                    self.vault_root,
                )
                continue

            # New workspaces and already-nested legacy workspaces get an
            # explicit registry path. That removes the old ambiguous
            # one-segment shape, so later filesystem changes cannot relocate
            # the workspace.
            stored = self.stored_workspace_vault_root(name)
            if workspace_config.vault_root != stored:
                workspace_config.vault_root = stored
                changed = True
        return changed

    def persist_workspace_registry(self) -> None:
        """Atomically persist the live workspace registry."""
        path = self.state_path.parent / "workspaces.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "name": workspace.name,
                "vault_root": workspace.vault_root,
                # Persist the effective provider, not a stale registry value
                # left behind after a provider was removed.  The route layer
                # already serializes this way; keeping the config registry
                # consistent prevents the next restart from resurrecting the
                # invalid id (#292).
                "default_provider": self.default_provider_for_workspace(
                    workspace.name
                ),
                "default_model": workspace.default_model,
                "disallowed_tools": workspace.disallowed_tools,
                "gws_profile": workspace.gws_profile,
                "color": workspace.color,
            }
            for workspace in self.workspaces.values()
        ]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        self._workspace_registry_changed = False

    def default_model_for_workspace(self, workspace: str | None) -> str:
        """Pick the new-chat / new-schedule default for a workspace.

        Falls back to ``claude_default_model`` when the per-workspace
        knob is empty or the workspace is unknown.
        """
        from ciao import provider_registry

        workspace_config = self.workspace(workspace)
        if workspace_config:
            descriptor = provider_registry.get(workspace_config.default_provider)
            if descriptor is not None:
                if workspace_config.default_model:
                    return workspace_config.default_model
                if descriptor.default_model_config_key:
                    return str(getattr(self, descriptor.default_model_config_key, "") or "")
                # No operator setting and no descriptor default means "use that
                # provider account's current catalog default": the provider
                # resolves it and the chat records the effective model.
                return descriptor.default_model
        return self.claude_default_model

    def sonnet_model_for_workspace(self, workspace: str | None) -> str:
        """The sonnet-tier model for a workspace's routines.

        A bare tier alias: whichever provider runs the workspace resolves it
        against its own catalog. Kept as a method rather than inlining the
        literal so the routines that ask "what does Automatic mean here?"
        (`resolve_title_model`, `resolve_insights_model`) keep one answer, and
        so a future per-workspace tier pin has somewhere to live.
        """
        return "sonnet"

    def haiku_model_for_workspace(self, workspace: str | None) -> str:
        """The haiku-tier model for a workspace's routines. See above."""
        return "haiku"

    def default_provider_for_workspace(self, workspace: str | None) -> str:
        from ciao import provider_registry

        workspace_config = self.workspace(workspace)
        # An unregistered value (a stale reference to a removed backend, a
        # typo) leaves the answer at the default provider.
        if workspace_config and provider_registry.is_provider(
            workspace_config.default_provider
        ):
            return workspace_config.default_provider
        return "claude"

    def default_mode_for_provider(self, provider: str) -> BridgeMode:
        """The default execution mode for new chats on ``provider``.

        An operator pin (Settings → Providers → default mode) wins. Otherwise
        the built-in default: opencode runs in normal mode so tool calls
        require operator approval. Every other provider falls back to
        ``claude_mode``.
        """
        mode = (self.provider_default_modes or {}).get(provider, "")
        if mode in {"normal", "plan", "auto", "bypass"}:
            return cast(BridgeMode, mode)
        if provider == "opencode":
            return "normal"
        return self.claude_mode

    def disallowed_tools_for_workspace(self, workspace: str | None) -> list[str]:
        """Tools to deny for a chat in this workspace.

        The effective denylist is the workspace's extra tools
        (``disallowed_tools``), which defaults to the harness set
        (``_DEFAULT_HARNESS_DISALLOWED_TOOLS``) for every workspace. Every chat
        blocks the PWA-irrelevant harness tools (plan mode, cron, /loop wakeup,
        routine trigger, push, notebook edit, design-system sync). claude.ai
        connector MCPs are always allowed: with multiple providers Ciaobot no
        longer ships an opinion on them. The extras are overridable via the
        per-workspace disallowed-tools env var or the "Extra disallowed tools"
        field (the literal ``none`` denies nothing at all).

        An unregistered workspace name — a stale reference, or a renamed or
        deleted workspace — gets the defaults rather than an empty denylist. It
        was the one input that reached the model with nothing denied.
        """
        workspace_config = self.workspace(workspace)
        extras = workspace_config.disallowed_tools if workspace_config else None
        if extras is None:
            extras = list(_DEFAULT_HARNESS_DISALLOWED_TOOLS)
        return list(dict.fromkeys(extras))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CiaoConfig":
        if env is None:
            workspace_env_val = os.environ.get("CIAO_WORKSPACE", "").strip() or os.environ.get("TELEGRAM_BRIDGE_WORKSPACE", "").strip() or "."
            dotenv_path = Path(workspace_env_val).expanduser().resolve() / ".env"
            if dotenv_path.exists():
                from dotenv import load_dotenv
                load_dotenv(dotenv_path)
            # Default to disabling Claude Code's auto memory inside Ciaobot
            os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
            # Artifacts publish to claude.ai; ciaobot has no use for that surface
            os.environ.setdefault("CLAUDE_CODE_DISABLE_ARTIFACT", "1")

        source = env if env is not None else os.environ

        pwa_allowed_origins = tuple(
            o.strip()
            for o in source.get("CIAO_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        )

        pwa_auth_token = source.get("PWA_AUTH_TOKEN", "").strip()
        pwa_auth_required_raw = source.get("PWA_AUTH_REQUIRED", "").strip().lower()
        if pwa_auth_required_raw:
            pwa_auth_required = pwa_auth_required_raw in {"true", "1", "yes", "y"}
        else:
            # Password protection is the default: setup asks for a password and
            # writes PWA_AUTH_REQUIRED explicitly, so an unset value means either
            # a workspace .env that predates the default or a hand-rolled one.
            # Those are protected as soon as a token exists — the token *is* the
            # password, readable in the workspace .env, and `ciao setup-url`
            # mints a one-time localhost login for whoever no longer knows it.
            # Without a token there is nothing a human could type, and enforcing
            # would lock the owner out of their own install (the session secret
            # is machine-generated), so protection stays off until a password is
            # set in Settings.
            pwa_auth_required = bool(pwa_auth_token)
        bootstrap_mode = not (
            (bool(pwa_auth_token) or not pwa_auth_required)
            and (bool(source.get("CIAO_WORKSPACE")) or bool(source.get("TELEGRAM_BRIDGE_WORKSPACE")))
        )
        if bootstrap_mode:
            workspace_root = _bootstrap_workspace(source)
            runtime_default = workspace_root / ".runtime"
            pwa_auth_token = _read_or_create_secret(
                runtime_default / "bootstrap-auth-token"
            )
        else:
            workspace_root = Path(
                _env(source, "CIAO_WORKSPACE", "TELEGRAM_BRIDGE_WORKSPACE", ".")
            ).expanduser().resolve()
            runtime_default = Path(".runtime")
            if not pwa_auth_token:
                # No token configured (auth is typically off on this branch).
                # Persist a random per-workspace secret instead of a shared
                # constant, so the session-signing key is never a publicly
                # known value baked into the source on any install.
                pwa_auth_token = _read_or_create_secret(
                    workspace_root / ".runtime" / "session-secret"
                )

        vault_root_raw = source.get("CIAO_VAULT_ROOT", "").strip()
        if vault_root_raw:
            vault_root = Path(vault_root_raw).expanduser()
            if not vault_root.is_absolute():
                vault_root = workspace_root / vault_root
            vault_root = vault_root.resolve()
        else:
            vault_root = (workspace_root / "memory-vault").resolve()
        runtime_root = Path(
            _env(
                source,
                "CIAO_RUNTIME_ROOT",
                "TELEGRAM_BRIDGE_RUNTIME_ROOT",
                str(runtime_default),
            )
        ).expanduser()
        if bootstrap_mode and not runtime_root.is_absolute():
            runtime_root = workspace_root / runtime_root
        runtime_root = runtime_root.resolve()
        state_path = runtime_root / "state.json"
        media_root = runtime_root / "telegram_media"  # keep old path for existing media
        workspaces_json = source.get("CIAO_WORKSPACES", "").strip()
        if not workspaces_json:
            workspaces_path = runtime_root / "workspaces.json"
            try:
                if workspaces_path.is_file():
                    workspaces_json = workspaces_path.read_text(encoding="utf-8")
            except OSError:
                workspaces_json = ""

        claude_models = _split_csv(source.get("CLAUDE_MODELS", "opus,sonnet,haiku"))
        claude_default_model = claude_models[0] if claude_models else "opus"
        default_model_personal = source.get("CLAUDE_DEFAULT_MODEL_PERSONAL", "").strip()
        default_model_work = source.get("CLAUDE_DEFAULT_MODEL_WORK", "").strip()
        disallowed_tools_personal = _parse_disallowed_tools(
            source.get("CIAO_DISALLOWED_TOOLS_PERSONAL", "")
        )
        disallowed_tools_work = _parse_disallowed_tools(
            source.get("CIAO_DISALLOWED_TOOLS_WORK", "")
        )
        gws_default_profile = source.get("GWS_PROFILE", "personal").strip() or "personal"
        workspaces = _parse_workspaces_json(workspaces_json) or _legacy_workspaces(
            default_model_personal=default_model_personal,
            default_model_work=default_model_work,
            disallowed_tools_personal=disallowed_tools_personal,
            disallowed_tools_work=disallowed_tools_work,
            gws_default_profile=gws_default_profile,
        )

        dev_mode_raw = source.get("CIAO_DEV_MODE", "").strip().lower()
        dev_mode = dev_mode_raw in {"true", "1", "yes", "y"}

        app_repo_raw = source.get("CIAO_APP_REPO", "").strip()
        app_repo = Path(app_repo_raw).expanduser().resolve() if app_repo_raw else None

        vault_mode = source.get("CIAO_VAULT_MODE", "scratch").strip().lower()
        if vault_mode not in {"existing", "scratch"}:
            vault_mode = "scratch"

        return cls(
            pwa_auth_token=pwa_auth_token,
            workspace_root=workspace_root,
            state_path=state_path,
            media_root=media_root,
            pwa_auth_required=pwa_auth_required,
            pwa_allowed_origins=pwa_allowed_origins,
            dev_mode=dev_mode,
            app_repo=app_repo,
            vault_mode=vault_mode,
            bootstrap_mode=bootstrap_mode,
            vault_root=vault_root,
            max_image_size_bytes=int(
                _env(source, "CIAO_MAX_IMAGE_BYTES", "TELEGRAM_BRIDGE_MAX_IMAGE_BYTES", str(10 * 1024 * 1024))
            ),
            max_voice_size_bytes=int(
                _env(source, "CIAO_MAX_VOICE_BYTES", "TELEGRAM_BRIDGE_MAX_VOICE_BYTES", str(25 * 1024 * 1024))
            ),
            media_ttl_hours=int(
                _env(source, "CIAO_MEDIA_TTL_HOURS", "TELEGRAM_BRIDGE_MEDIA_TTL_HOURS", "72")
            ),
            title_model=source.get("CIAO_TITLE_MODEL", "").strip() or "haiku",
            title_model_override=source.get("CIAO_TITLE_MODEL_OVERRIDE", "").strip(),
            apple_intelligence_enabled=source.get(
                "CIAO_APPLE_INTELLIGENCE", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            transcription_locale=source.get("CIAO_TRANSCRIPTION_LOCALE", "").strip()
            or "en-US",
            tts_local_voice=source.get("CIAO_TTS_LOCAL_VOICE", "").strip(),
            claude_models=list(claude_models or ["opus", "sonnet", "haiku"]),
            claude_default_model=claude_default_model,
            claude_mode=normalize_claude_mode(
                source.get("CLAUDE_EXECUTION_MODE", "")
                or source.get("CLAUDE_PERMISSION_MODE", "auto")
            ),
            restart_exit_code=int(
                _env(source, "CIAO_RESTART_EXIT_CODE", "TELEGRAM_BRIDGE_RESTART_EXIT_CODE", "75")
            ),
            auto_sync_on_start=_env(
                source, "CIAO_AUTO_SYNC_ON_START", "TELEGRAM_BRIDGE_AUTO_SYNC_ON_START", "true"
            ).lower() not in {"0", "false", "no", "off"},
            auto_vault_index=source.get("CIAO_AUTO_VAULT_INDEX", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            auto_update_github_skills=source.get("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "false").strip().lower()
            not in {"0", "false", "no", "off"},
            pwa_port=int(source.get("PWA_PORT", "8443")),
            pwa_host=source.get("PWA_HOST", "0.0.0.0").strip(),
            gws_default_profile=gws_default_profile,
            default_model_personal=default_model_personal,
            default_model_work=default_model_work,
            disallowed_tools_personal=disallowed_tools_personal,
            disallowed_tools_work=disallowed_tools_work,
            workspaces=workspaces,
            insights_enabled=source.get("CIAO_INSIGHTS_DISABLED", "").strip().lower()
            in {"", "0", "false", "no", "off"},
            insights_size_gate_turns=int(
                source.get("CIAO_INSIGHTS_MIN_TURNS", "2") or "2"
            ),
            insights_model_override=source.get("CIAO_INSIGHTS_MODEL", "").strip(),
            insights_backfill_on_startup=source.get(
                "CIAO_INSIGHTS_BACKFILL_ON_STARTUP", "false"
            ).strip().lower()
            not in {"0", "false", "no", "off"},
            trajectories_enabled=source.get(
                "CIAO_TRAJECTORIES_DISABLED", ""
            ).strip().lower()
            in {"", "0", "false", "no", "off"},
            trajectory_retention_months=int(
                source.get("CIAO_TRAJECTORY_RETENTION_MONTHS", "").strip() or "6"
            ),
            skill_evolution_enabled=source.get(
                "CIAO_SKILL_EVOLUTION_DISABLED", ""
            ).strip().lower()
            in {"", "0", "false", "no", "off"},

            critique_models=source.get("CIAO_REVIEW_MODELS", "").strip()
            or source.get("CIAO_ADVERSARIAL_MODELS", "").strip(),
            memory_char_limit=int(
                source.get("CIAO_MEMORY_CHAR_LIMIT", "").strip() or "2200"
            ),
            user_char_limit=int(
                source.get("CIAO_USER_CHAR_LIMIT", "").strip() or "1375"
            ),
            mcp_enabled=source.get("CIAO_MCP_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            control_surface=(
                source.get("CIAO_CONTROL_SURFACE", "mcp").strip().lower()
                if source.get("CIAO_CONTROL_SURFACE", "mcp").strip().lower()
                in {"legacy", "mcp"}
                else "mcp"
            ),
            benchmark_mode=source.get("CIAO_BENCHMARK_MODE", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
        )


logger = logging.getLogger(__name__)




# Backward-compatible alias used by project_chats.py and other modules
BridgeConfig = CiaoConfig
