"""Configuration loading for the Ciaobot server."""

from __future__ import annotations

import logging
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ciao.execution_modes import HARNESS_DISABLED_SKILLS
from ciao.models import BridgeMode
from ciao.providers.codex import CodexSettings
from ciao.providers.opencode import OpencodeSettings

if TYPE_CHECKING:
    from ciao.provider_registry import ProviderDescriptor


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


_REROOTED_CACHE: dict[str, bool] = {}


def reset_reroot_cache() -> None:
    """Forget whether an install has re-rooted.

    Called by the migration after it writes its receipt, and by tests that build
    several installs in one process. Without it, the first install's answer would
    be reused for a later one that has a different runtime directory only if the
    path matched, so this is mostly a same-path correctness hook.
    """
    _REROOTED_CACHE.clear()


def agent_roots_for(workspace_root: Path, runtime_root: Path) -> list[tuple[Path, str]]:
    """Every agent root, from explicit paths. See ``CiaoConfig.agent_root_targets``.

    The standalone form, for `ciao setup`, which builds paths before any config
    exists. Reads the registry off disk rather than through ``CiaoConfig``, and
    deliberately does NOT apply the bootstrap fallback: setup runs on installs
    that have no registry yet, and manufacturing two workspace names there would
    scaffold two roots for an install that has none.
    """
    from ciao.workspace_reroot import read_receipt, registry_file  # noqa: PLC0415

    workspace_root = Path(workspace_root)
    if read_receipt(Path(runtime_root)) is None:
        return [(workspace_root, "")]
    try:
        entries = json.loads(registry_file(Path(runtime_root)).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return [(workspace_root, "")]
    names = [
        str(e.get("name", "")).strip()
        for e in entries
        if isinstance(e, dict) and str(e.get("name", "")).strip()
    ]
    if not names:
        return [(workspace_root, "")]
    return [(workspace_root / name, name) for name in sorted(names)]


def logs_root_for(workspace_root: Path, vault_root: Path, runtime_root: Path) -> Path:
    """Where the derived transcript archive lives, from explicit paths.

    The same receipt gate ``CiaoConfig.logs_root`` uses, exposed as a function so
    a CLI holding only paths cannot end up with a second copy of the rule. One
    definition of "has this install re-rooted" is the whole point of the seam.
    """
    from ciao.workspace_reroot import read_receipt  # noqa: PLC0415

    if read_receipt(Path(runtime_root)) is not None:
        return Path(workspace_root) / "Logs"
    return Path(vault_root) / "Logs"


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
    # The ``.mcp.json`` servers this workspace may reach, by name. A list names
    # exactly those servers; every other declared server is denied. ``[]`` means
    # reach nothing. ``None`` means "not yet decided" and, after the load-time
    # migration seeds existing workspaces, only happens for a workspace created
    # since — and ``None`` denies every declared server, the fail-closed default
    # for anything new. ``mcp__<server>`` deny entries are derived from this at
    # request time; this field is the source, not the deny list.
    allowed_mcp_servers: list[str] | None = None
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


def _coerce_allowed_mcp_servers(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        # A CSV list, like the disallowed-tools env var, so the field round-trips
        # through a string form as easily as a JSON list.
        return _split_csv(raw)
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
        allowed_mcp_servers=_coerce_allowed_mcp_servers(
            data.get("allowed_mcp_servers")
        ),
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


# A vault directory is a workspace when it CONTAINS one of these, not when it is
# one. `memory-vault/personal/People/` makes `personal` a workspace;
# `memory-vault/People/` is a note folder in a single-workspace vault and must
# not become a workspace called "People". The nesting is what separates them.
_WORKSPACE_EVIDENCE_DIRS: frozenset[str] = frozenset(
    {"People", "Projects", "Places", "Ideas", "Resources", "Workspace", "journal", "projects"}
)


def _looks_like_workspace_dir(path: Path) -> bool:
    """Whether ``path`` is a workspace folder inside a shared vault."""
    if not path.is_dir() or path.name.startswith("."):
        return False
    if (path / "MEMORY.md").is_file():
        return True
    try:
        return any(
            child.is_dir() and child.name in _WORKSPACE_EVIDENCE_DIRS
            for child in path.iterdir()
        )
    except OSError:
        return False


def _bootstrap_registry(
    vault_root: Path | None = None, *, gws_default_profile: str = "personal"
) -> dict[str, WorkspaceConfig]:
    """The registry an install gets before it has one, read off the vault.

    This is the bootstrap default, not a fallback — nothing else seeds a registry
    for an install that skipped ``ciao setup``, so returning nothing would yield
    zero workspaces. It used to manufacture BOTH ``personal`` and ``work``
    unconditionally, which was harmless while every workspace shared one vault
    directory and is not harmless now:

    - The re-rooting plan refuses when a registered workspace has no vault
      directory, so a phantom ``work`` entry left such an install permanently
      unable to migrate while the blocking gate kept telling it to.
    - Hardcoding ONE instead, as the work order proposed, moves the same problem:
      an install whose vault really does hold ``personal/`` and ``work/`` would
      have ``work`` unregistered, and the plan refuses on an unregistered vault
      directory. Also stuck, from the other side.

    Neither guess is right because the answer is on disk, so read it: one
    workspace per vault directory that looks like one, and ``personal`` when none
    do (a fresh or single-workspace vault). The evidence test is deliberately
    nested — see ``_WORKSPACE_EVIDENCE_DIRS`` — so a vault whose notes sit
    directly under ``People/`` yields one workspace, not a workspace per folder.

    The four ``*_PERSONAL`` / ``*_WORK`` environment variables that fed the
    two-entry shape (default model and extra disallowed tools, one pair each)
    went with it: they could only ever describe those two hardcoded names, and an
    install that wants per-workspace settings puts them in ``workspaces.json``,
    which is the supported path and works for any name.
    """
    names: list[str] = []
    if vault_root is not None and Path(vault_root).is_dir():
        names = sorted(
            child.name
            for child in Path(vault_root).iterdir()
            if _looks_like_workspace_dir(child)
        )
    if not names:
        names = ["personal"]
    profile = gws_default_profile or "personal"
    return {
        name: WorkspaceConfig(
            name=name,
            vault_root=name,
            default_provider="claude",
            # The configured profile belongs to the workspace it was configured
            # for; anything else derived from a directory name would be a guess
            # about someone's Google account.
            gws_profile=profile if name == "personal" else name,
        )
        for name in names
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
    # BCP-47 language for the on-device voice engines. Dictation needs a
    # matching language installed in System Settings → Keyboard → Dictation;
    # the synthesizer uses it to choose a voice.
    transcription_locale: str = "en-US"
    # macOS voice identifier or name for read-aloud. Empty means "the best
    # installed voice for transcription_locale" -- the right default when the
    # available voices differ on every machine.
    tts_local_voice: str = ""
    claude_models: list[str] = field(default_factory=lambda: ["opus", "sonnet", "haiku", "fable"])
    claude_default_model: str = "opus"
    # Per-workspace default models and tool denylists live on the WorkspaceConfig
    # in this registry, set through `workspaces.json`. The former top-level
    # `*_personal` / `*_work` pairs are gone: they existed only to furnish the
    # two-entry bootstrap registry, and that now returns one workspace.
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    _workspace_registry_changed: bool = field(
        init=False, default=False, repr=False
    )
    claude_mode: BridgeMode = "auto"
    # Per-provider default model for new chats, set from the PWA Settings →
    # Models tab (runtime settings store). A missing entry uses the provider's
    # own catalog default.
    provider_default_models: dict[str, str] = field(default_factory=dict)
    # Per-provider default thinking level for new chats, set from the PWA
    # Settings → Models tab. A missing entry uses the provider's own default.
    provider_default_thinking: dict[str, str] = field(default_factory=dict)
    # Per-provider session-insights model, set from the PWA Settings → Models
    # tab. A missing entry uses the provider's balanced default.
    provider_insights_models: dict[str, str] = field(default_factory=dict)
    restart_exit_code: int = 75
    auto_sync_on_start: bool = False
    auto_vault_index: bool = True
    auto_update_github_skills: bool = False
    pwa_port: int = 8443
    pwa_host: str = "127.0.0.1"
    gws_default_profile: str = "personal"
    # Per-provider default model for new chats, set from the PWA Settings →
    # Models tab. A missing entry uses the provider's own catalog default.
    codex: CodexSettings = field(default_factory=CodexSettings)
    # Per-provider default model for new chats, same shape and meaning as the
    # Codex one. Empty means the provider's own default applies.
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
            self.workspaces = _bootstrap_registry(
                self.vault_root,
                gws_default_profile=self.gws_default_profile,
            )
        self._workspace_registry_changed = self._normalize_workspace_vault_roots()
        # Migrate pre-existing workspaces onto the allowlist now, so deny
        # resolution never sees a ``None`` allowlist on a workspace that existed
        # before this field. Runs only when the registry actually exists on
        # disk, so a brand-new install (legacy fallback, no file) keeps its
        # ``None`` fail-closed default for anything created since.
        self._seed_allowed_mcp_servers()

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
        """Standard location for a user-named workspace's notes, in the layout
        this install is actually in.

        Two layouts, one question. Shared: a folder per workspace under the one
        vault (``memory-vault/<name>``). Per-root, after the re-rooting: the
        whole vault of that workspace's agent root — which is what
        :meth:`agent_vault_root` already derives from the same receipt.

        Answering "shared" unconditionally made this a claim about the past.
        ``_detect_vault_location`` compares the resolved vault against this and
        raised "The personal vault is not in its standard folder" on a correctly
        migrated install, for every workspace, permanently — with a chat prompt
        telling the operator to move the vault back to where the migration had
        just moved it from. A standard location that disagrees with the layout
        is worse than no check at all.
        """
        name = _clean_relative_path(workspace)
        if not name or len(Path(name).parts) != 1:
            raise ValueError("workspace name must identify one vault folder")
        if self.agent_root(name) != self.workspace_root:
            return self.agent_vault_root(name)
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

    def agent_root(self, name: str) -> Path:
        """Per-workspace directory that will hold that workspace's agent assets.

        The future home of a workspace's own ``CLAUDE.md``, ``.mcp.json``,
        ``.claude/`` assets, and ``memory-vault/``. The derivation is
        ``workspace_root / name``, but the per-workspace subdirectory only
        exists after the re-rooting release, so every workspace still resolves
        to ``workspace_root`` itself for now. A later phase flips the return to
        the derived path; this method is the single seam for that change.
        """
        name = _clean_relative_path(name)
        if not name or len(Path(name).parts) != 1 or any(
            sep in name for sep in ("/", "\\")
        ):
            raise ValueError("workspace name must identify one folder")
        # Flipped per install by the re-rooting migration, not by a release date.
        # Gating on the migration's own receipt is what makes the change atomic:
        # an install that has not re-rooted keeps resolving to workspace_root, so
        # a half-flipped state cannot exist. Deleting the legacy filters before
        # this returns a real subdirectory would leave no filter over a still
        # prefixed index, which is fail-open and strictly worse than today.
        if self._rerooted():
            return self.workspace_root / name
        return self.workspace_root

    def agent_vault_root(self, name: str) -> Path:
        """The vault whose ``INDEX.md`` this workspace's agent root owns.

        Distinct from :meth:`workspace_vault_root`, and the difference matters:

        ``workspace_vault_root`` is where a workspace's NOTES live. Before the
        re-rooting that is a subtree of one shared vault
        (``memory-vault/<name>``); after it, the whole vault of that root.

        This is where the aggregate FILES about those notes live — ``INDEX.md``
        and ``VOCABULARY.md``. Before the re-rooting there is exactly one such
        pair for the whole install, so every workspace resolves to the shared
        vault root; after it, each root owns its own pair. Derived from
        :meth:`agent_root`, so it flips on the same receipt and cannot disagree
        with it.

        Written as ``agent_root / <vault dir name>`` because that is literally
        what the migration produces: ``workspace_reroot.plan`` moves each vault
        to ``<name>/<vault dir name>``.
        """
        return self.agent_root(name) / self.vault_root.name

    @property
    def logs_root(self) -> Path:
        """Where the derived transcript archive lives. See :func:`logs_root_for`.

        D5: ``Logs/`` holds roughly 72% of the vault's notes, is derived output
        rather than curated content, and its chat ids cannot each be resolved
        back to one workspace, so the re-rooting PROMOTES it to
        ``<install>/Logs/`` unmoved rather than splitting it per root. Before the
        migration it is ``<vault>/Logs``; after, ``<install>/Logs``.

        Receipt-gated through the same ``_rerooted`` check as
        :meth:`agent_root`, so the two can never disagree about which layout the
        install is on. Without this seam the readers keep computing
        ``vault_root / "Logs"``, which after the migration is a path that does
        not exist — so chat archiving would recreate it and write new
        transcripts into a fresh empty tree, silently orphaning them from the
        promoted archive and making the old ones invisible.
        """
        return (
            self.workspace_root / "Logs" if self._rerooted() else self.vault_root / "Logs"
        )

    def agent_root_targets(self) -> list[tuple[Path, str]]:
        """Every agent root in this install, as ``(root, workspace name)``.

        One target before the re-rooting — the install root itself, unnamed,
        because that is where the single set of provider assets lives. One per
        workspace afterwards, because each root then owns its own ``CLAUDE.md``,
        ``.claude/``, ``skills/`` and mirrors.

        The seam for anything inspecting or listing agent assets. Reading
        ``workspace_root`` directly still finds the install root's stale
        ``.claude/`` after the migration, whose links point at a catalog that
        moved — which is why the health panel reported every custom skill as
        broken on a correctly migrated install.
        """
        if not self._rerooted():
            return [(self.workspace_root, "")]
        return [
            (self.agent_root(name), name) for name in self.workspace_names() if name
        ]

    def vault_scan_targets(self) -> list[tuple[Path, str, Path]]:
        """Every vault in this install, as ``(root, workspace, path prefix)``.

        One target before the re-rooting — the shared vault, unstamped, so the
        first-path-segment inference labels the workspaces exactly as it does
        today. One target PER ROOT afterwards, each stamped with its workspace
        and rendering paths under ``<name>/memory-vault`` so two roots holding a
        note of the same name do not render the same path.

        This is the seam for anything that means "all the notes in this install":
        reading ``vault_root`` directly gives a directory that does not exist
        after the migration, which is why the Memory Map came back empty.
        """
        if not self._rerooted():
            return [(self.vault_root, "", Path("memory-vault"))]
        targets: list[tuple[Path, str, Path]] = []
        for name in self.workspace_names():
            if not name:
                continue
            root = self.agent_vault_root(name)
            targets.append((root, name, Path(name) / root.name))
        return targets

    def _rerooted(self) -> bool:
        """Whether this install has completed the per-workspace re-rooting.

        Cached in a module-level map keyed by runtime directory, because
        ``CiaoConfig`` uses slots and ``agent_root`` is called on hot paths: a
        stat per call would be wasteful for a value that changes exactly once in
        an install's life. ``reset_reroot_cache`` clears it, which the migration
        itself calls after writing its receipt.
        """
        key = str(self.state_path.parent)
        cached = _REROOTED_CACHE.get(key)
        if cached is None:
            from ciao.workspace_reroot import read_receipt

            cached = read_receipt(self.state_path.parent) is not None
            _REROOTED_CACHE[key] = cached
        return cached

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
                "allowed_mcp_servers": workspace.allowed_mcp_servers,
                "gws_profile": workspace.gws_profile,
                "color": workspace.color,
            }
            for workspace in self.workspaces.values()
        ]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        self._workspace_registry_changed = False

    def default_model_for_workspace(
        self, workspace: str | None, provider: str | None = None
    ) -> str:
        """Pick the new-chat / new-schedule default for a workspace.

        Falls back to ``claude_default_model`` when the per-workspace
        knob is empty or the workspace is unknown.

        ``provider``, when given, is the provider the chat/schedule will
        actually run on. It can differ from the workspace's own
        ``default_provider`` (an explicit provider override); in that case
        resolve against that provider's own operator default instead of the
        workspace's default-provider model, since a workspace-level model
        override is only meaningful for the workspace's own default
        provider (#see create_chat/schedule_effective_routing callers).
        """
        from ciao import provider_registry

        workspace_config = self.workspace(workspace)
        workspace_provider = (
            workspace_config.default_provider if workspace_config else None
        )
        effective_provider = provider or workspace_provider
        if effective_provider:
            descriptor = provider_registry.get(effective_provider)
            if descriptor is not None:
                uses_workspace_default_provider = provider is None or provider == workspace_provider
                if (
                    workspace_config is not None
                    and uses_workspace_default_provider
                    and workspace_config.default_model
                ):
                    return workspace_config.default_model
                if descriptor.default_model_config_key:
                    return str(getattr(self, descriptor.default_model_config_key, "") or "")
                # A provider with an operator-settable default model (Codex,
                # opencode) uses it; otherwise "use that provider account's
                # current catalog default": the provider resolves it and the
                # chat records the effective model.
                operator_default = self._operator_default_model(descriptor)
                if operator_default:
                    return operator_default
                return descriptor.default_model
        return self.claude_default_model

    def _operator_default_model(self, descriptor: "ProviderDescriptor") -> str:
        """The operator-settable default model for a provider, or ``""`` when unset."""
        if not descriptor.default_model_settings_attr:
            return ""
        settings = getattr(self, descriptor.default_model_settings_attr, None)
        return getattr(settings, "default_model", "") if settings else ""

    def default_model_for_provider(self, provider: str) -> str:
        """The operator's default model for a provider, or ``""`` when unset.

        Used when a chat is created on a provider that is not the workspace's
        default provider, so the per-provider default still applies.
        """
        from ciao import provider_registry

        descriptor = provider_registry.get(provider)
        if descriptor is None:
            return ""
        return self._operator_default_model(descriptor)

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

        Every provider runs in auto mode by default. Auto's classifier gates
        risky actions (destructive shell, unapproved control-plane mutations)
        while allowing safe reads and edits, so there is no safer default and
        no per-provider override.
        """
        return self.claude_mode

    def _declared_mcp_server_names(self) -> list[str] | None:
        """Names of servers declared in the project ``.mcp.json``.

        Returns the union of server names across the same candidate files the
        MCP status panel discovers, ``[]`` when no ``.mcp.json`` exists (nothing
        is declared, so nothing to deny), and ``None`` when a file exists but
        cannot be parsed. A parse failure cannot be mapped to names, so the
        caller fails closed against the known allowlist universe instead.
        """
        workspace_root = self.workspace_root
        candidates = [
            workspace_root / ".mcp.json",
            workspace_root.parent / ".mcp.json",
            workspace_root.parent / "ciao" / ".mcp.json",
        ]
        names: list[str] = []
        seen: set[str] = set()
        for path in candidates:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            if not isinstance(data, dict):
                return None
            mcp_dict = data.get("mcpServers") or data.get("mcp_servers") or {}
            if not isinstance(mcp_dict, dict):
                return None
            for raw_name in mcp_dict:
                name = str(raw_name)
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def _known_mcp_server_names(self) -> list[str]:
        """Every server name any workspace's allowlist can reach.

        This is the known universe of servers on this install, used to fail
        closed by name when ``.mcp.json`` is unreadable. The limitation is
        honest: a server that was never seeded and lives only in the corrupt
        file cannot be denied here, because nothing knows it exists.
        """
        known: list[str] = []
        for workspace_config in self.workspaces.values():
            for name in workspace_config.allowed_mcp_servers or ():
                if name and name not in known:
                    known.append(name)
        return known

    def disallowed_tools_for_workspace(self, workspace: str | None) -> list[str]:
        """Tools to deny for a chat in this workspace.

        The effective denylist is the workspace's extra tools
        (``disallowed_tools``), which defaults to the harness set
        (``_DEFAULT_HARNESS_DISALLOWED_TOOLS``) for every workspace, plus a
        derived ``mcp__<server>`` deny for every server declared in ``.mcp.json``
        that the workspace's ``allowed_mcp_servers`` does not name. Every chat
        blocks the PWA-irrelevant harness tools (plan mode, cron, /loop wakeup,
        routine trigger, push, notebook edit, design-system sync). A workspace
        that predates the allowlist is seeded at load; a brand new one has
        ``None``, which denies every declared server (the fail-closed default).

        Two limits stated plainly. First, this scopes REACHABILITY, not
        authority: a shared account behind a reachable server still holds that
        account's full authority. Second, ``disallowed_tools`` is only applied
        when the chat's provider is ``claude`` (see the ``if chat.provider !=
        "claude": return []`` guard in project_chats); it does NOT constrain
        codex or opencode chats at all. Closing that non-Claude gap needs a
        per-provider mechanism and is out of scope here.

        When ``.mcp.json`` exists but cannot be parsed, every server any
        workspace's allowlist names is denied by explicit name: the known
        universe of servers on this install, which fails closed with names the
        SDK can actually match instead of a glob it may ignore.

        An unregistered workspace name — a stale reference, or a renamed or
        deleted workspace — gets the defaults rather than an empty denylist. It
        was the one input that reached the model with nothing denied.
        """
        workspace_config = self.workspace(workspace)
        extras = workspace_config.disallowed_tools if workspace_config else None
        if extras is None:
            extras = list(_DEFAULT_HARNESS_DISALLOWED_TOOLS)
        allowlist = (
            workspace_config.allowed_mcp_servers
            if workspace_config is not None
            else None
        )
        allow_set = set(allowlist or ())
        declared = self._declared_mcp_server_names()
        if declared is None:
            denied = [f"mcp__{name}" for name in self._known_mcp_server_names()]
        else:
            denied = [
                f"mcp__{name}" for name in declared if name not in allow_set
            ]
        return list(dict.fromkeys([*extras, *denied]))

    def _seed_allowed_mcp_servers(self) -> None:
        """Migrate pre-existing workspaces onto the allowlist, losslessly.

        Auto-apply under decision D1 of the agent-roots work order: the change
        is confined to metadata Ciaobot generates (the workspace registry), and
        there is exactly one correct outcome per workspace — what it can reach
        right now. A registered workspace whose allowlist is ``None`` and that
        exists in the registry file is seeded with every server declared in
        ``.mcp.json`` that its ``disallowed_tools`` does not already deny. A
        brand-new workspace created in code (legacy fallback, no file) is not
        touched and keeps its ``None`` fail-closed default.

        This reads and rewrites the raw file so unrelated or unknown keys (e.g.
        a future field this release does not know) survive; the normal
        ``persist_workspace_registry`` path intentionally rewrites a clean
        payload and would drop them. The write is atomic (tmp then replace), the
        same discipline as the regular persistence path.
        """
        path = self.state_path.parent / "workspaces.json"
        if not path.is_file():
            return
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(entries, list):
            return
        declared = self._declared_mcp_server_names()
        if declared is None:
            # A corrupt .mcp.json cannot be mapped to names; leave the
            # allowlist untouched and let deny resolution fail closed by name
            # against the known universe instead.
            return
        if not declared:
            # Nothing is declared, so the effective set is empty and ``None``
            # already denies nothing to reach (both fail closed). Persisting
            # ``[]`` here would rewrite the registry on every fresh install and
            # break a setup-rerun's idempotency for no behavioural change.
            return
        changed = False
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            workspace_config = self.workspace(name)
            if workspace_config is None or workspace_config.allowed_mcp_servers is not None:
                continue
            denied = set(workspace_config.disallowed_tools or ())
            seed = [s for s in declared if f"mcp__{s}" not in denied]
            entry["allowed_mcp_servers"] = seed
            workspace_config.allowed_mcp_servers = seed
            changed = True
        if not changed:
            return
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CiaoConfig":
        if env is None:
            workspace_env_val = os.environ.get("CIAO_WORKSPACE", "").strip() or "."
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
            and bool(source.get("CIAO_WORKSPACE"))
        )
        if bootstrap_mode:
            workspace_root = _bootstrap_workspace(source)
            runtime_default = workspace_root / ".runtime"
            pwa_auth_token = _read_or_create_secret(
                runtime_default / "bootstrap-auth-token"
            )
        else:
            workspace_root = Path(
                source.get("CIAO_WORKSPACE", ".")
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
            source.get("CIAO_RUNTIME_ROOT", str(runtime_default))
        ).expanduser()
        if not runtime_root.is_absolute():
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

        claude_models = _split_csv(source.get("CLAUDE_MODELS", "opus,sonnet,haiku,fable"))
        claude_default_model = claude_models[0] if claude_models else "opus"
        gws_default_profile = source.get("GWS_PROFILE", "personal").strip() or "personal"
        workspaces = _parse_workspaces_json(workspaces_json) or _bootstrap_registry(
            vault_root,
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
                source.get("CIAO_MAX_IMAGE_BYTES", str(10 * 1024 * 1024))
            ),
            max_voice_size_bytes=int(
                source.get("CIAO_MAX_VOICE_BYTES", str(25 * 1024 * 1024))
            ),
            media_ttl_hours=int(
                source.get("CIAO_MEDIA_TTL_HOURS", "72")
            ),
            transcription_locale=source.get("CIAO_TRANSCRIPTION_LOCALE", "").strip()
            or "en-US",
            tts_local_voice=source.get("CIAO_TTS_LOCAL_VOICE", "").strip(),
            claude_models=list(claude_models or ["opus", "sonnet", "haiku", "fable"]),
            claude_default_model=claude_default_model,
            claude_mode="auto",
            restart_exit_code=int(
                source.get("CIAO_RESTART_EXIT_CODE", "75")
            ),
            auto_sync_on_start=source.get("CIAO_AUTO_SYNC_ON_START", "true").lower()
            not in {"0", "false", "no", "off"},
            auto_vault_index=source.get("CIAO_AUTO_VAULT_INDEX", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            auto_update_github_skills=source.get("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "false").strip().lower()
            not in {"0", "false", "no", "off"},
            pwa_port=int(source.get("PWA_PORT", "8443")),
            pwa_host=source.get("PWA_HOST", "0.0.0.0").strip(),
            gws_default_profile=gws_default_profile,
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
