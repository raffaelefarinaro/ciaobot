"""Configuration loading for the Ciaobot server."""

from __future__ import annotations

import logging
import json
import os
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path

from ciao.execution_modes import normalize_claude_mode
from ciao.models import BridgeMode
from ciao.providers.ollama import OllamaSettings
from ciao.providers.openrouter import OpenRouterSettings


# claude.ai account-OAuth connector MCPs (Airtable, Atlassian, Slack, Asana,
# BigQuery, incident.io, Salesforce, Sentry). These are gated per workspace
# by the ``claude_ai_mcps`` toggle on ``WorkspaceConfig`` (default on). The
# toggle expands to this set in ``CiaoConfig.disallowed_tools_for_workspace``.
CLAUDE_AI_CONNECTORS: tuple[str, ...] = (
    "mcp__claude_ai_Airtable",
    "mcp__claude_ai_Asana",
    "mcp__claude_ai_Atlassian",
    "mcp__claude_ai_Google_Cloud_BigQuery",
    "mcp__claude_ai_Salesforce",
    "mcp__claude_ai_Sentry",
    "mcp__claude_ai_Slack",
    "mcp__claude_ai_incident_io",
)

# Non-connector tools blocked by default in the personal workspace. The
# self-hosted n8n MCP (project-scoped in .mcp.json) is work-only, so it stays
# blocked even when the claude.ai MCP toggle is flipped on. Operators add or
# remove entries via the per-workspace "Extra disallowed tools" field (PWA) or
# ``CIAO_DISALLOWED_TOOLS_PERSONAL`` (CSV; literal ``none`` clears).
_DEFAULT_EXTRA_DISALLOWED_TOOLS_PERSONAL: tuple[str, ...] = (
    "mcp__n8n_mcp",
)

# Back-compat alias: the full personal default denylist (connectors + extras).
# Kept for any caller that still wants the combined set.
_DEFAULT_DISALLOWED_TOOLS_PERSONAL: tuple[str, ...] = (
    *CLAUDE_AI_CONNECTORS,
    *_DEFAULT_EXTRA_DISALLOWED_TOOLS_PERSONAL,
)


def coerce_claude_ai_mcps(raw: object) -> bool | None:
    """Parse the claude.ai MCPs toggle. ``None``/``"default"`` → unset (use the
    per-workspace default); booleans pass through; strings ``true/false/on/off``
    coerce. Anything else → None."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in {"", "default", "none"}:
            return None
        if cleaned in {"true", "1", "yes", "on"}:
            return True
        if cleaned in {"false", "0", "no", "off"}:
            return False
    return None


@dataclass(slots=True)
class WorkspaceConfig:
    """Config for one logical chat workspace."""

    name: str
    vault_root: str
    default_provider: str = "claude"
    default_model: str = ""
    # Extra (non-connector) tools to deny. The claude.ai connector set is
    # controlled by ``claude_ai_mcps``; this field covers everything else
    # (e.g. ``mcp__n8n_mcp``, ``Bash``). ``None`` = use the per-workspace
    # default extras; ``[]`` = explicit opt-out (no extras).
    disallowed_tools: list[str] | None = None
    # Whether claude.ai account-OAuth connector MCPs are exposed in this
    # workspace. ``None`` = default (True).
    # When False/defaults-off, ``CLAUDE_AI_CONNECTORS`` is added to the
    # effective denylist in ``disallowed_tools_for_workspace``.
    claude_ai_mcps: bool | None = None
    gws_profile: str = ""
    model_bucket: str = ""


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
    return WorkspaceConfig(
        name=name,
        vault_root=vault_root,
        default_provider=str(data.get("default_provider", "claude")).strip() or "claude",
        default_model=str(data.get("default_model", "")).strip(),
        disallowed_tools=_coerce_workspace_disallowed(data.get("disallowed_tools")),
        claude_ai_mcps=coerce_claude_ai_mcps(data.get("claude_ai_mcps")),
        gws_profile=str(data.get("gws_profile", "")).strip(),
        model_bucket=str(data.get("model_bucket", "")).strip(),
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
    claude_ai_mcps_personal: bool | None = None,
    claude_ai_mcps_work: bool | None = None,
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
            claude_ai_mcps=claude_ai_mcps_personal,
            gws_profile=gws_default_profile or "personal",
            model_bucket="personal",
        ),
        "work": WorkspaceConfig(
            name="work",
            vault_root="work",
            default_provider="claude",
            default_model=default_model_work,
            disallowed_tools=disallowed_tools_work,
            claude_ai_mcps=claude_ai_mcps_work,
            gws_profile="work",
            model_bucket="work",
        ),
    }


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_disallowed_tools(raw: str) -> list[str] | None:
    """Parse a CSV denylist. Empty/missing → None (use defaults);
    ``"none"`` → ``[]`` (explicit opt-out); CSV → parsed list.

    The None vs []-empty distinction matters because the personal
    workspace has built-in defaults (block claude.ai connectors).
    Operators who want zero denylist set the literal ``"none"``.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    if cleaned.lower() == "none":
        return []
    return _split_csv(cleaned)


def _env(source: dict[str, str], new_name: str, old_name: str, default: str = "") -> str:
    """Read env var with fallback to old TELEGRAM_BRIDGE_* name for migration."""
    return source.get(new_name, "").strip() or source.get(old_name, "").strip() or default


def _bootstrap_workspace(source: dict[str, str]) -> Path:
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
    pwa_auth_required: bool = True
    dev_mode: bool = False
    vault_mode: str = "scratch"
    bootstrap_mode: bool = False
    vault_root: Path = Path("memory-vault")
    max_image_size_bytes: int = 10 * 1024 * 1024
    max_voice_size_bytes: int = 25 * 1024 * 1024
    media_ttl_hours: int = 72
    openai_api_key: str | None = None
    # Titling uses the Claude Agent SDK's one-shot query(); default Haiku.
    title_model: str = "haiku"
    # Operator override for the titling model, set from the PWA Settings →
    # Models tab (runtime settings store) or ``CIAO_TITLE_MODEL_OVERRIDE``.
    # Empty = automatic routing (Ollama title_model when Ollama is
    # configured, else ``title_model``).
    title_model_override: str = ""
    # Voice transcription engine: ``cloud`` (OpenAI API, needs
    # OPENAI_API_KEY) or ``local`` (mlx-whisper on Apple Silicon).
    # Runtime-overridable from the PWA Settings → Models tab.
    transcription_engine: str = "cloud"
    transcription_local_model: str = "mlx-community/whisper-large-v3-turbo"
    # Speech synthesis (read a message aloud): ``cloud`` (OpenAI
    # ``gpt-4o-mini-tts``, needs OPENAI_API_KEY) or ``local`` (Kokoro via
    # kokoro-onnx, free/offline). Runtime-overridable from the PWA
    # Settings → Models tab.
    tts_engine: str = "cloud"
    tts_cloud_voice: str = "nova"
    tts_local_voice: str = "af_heart"
    claude_models: list[str] = field(default_factory=lambda: ["opus", "sonnet", "haiku"])
    claude_default_model: str = "opus"
    # Per-workspace default models. Empty string falls back to
    # claude_default_model. Lets the personal workspace default to a
    # cheap Ollama model while work stays on Anthropic.
    default_model_personal: str = ""
    default_model_work: str = ""
    # Per-workspace tool denylists (the "extra" tools beyond claude.ai
    # connectors). Forwarded to ``ClaudeAgentOptions.disallowed_tools`` for the
    # spawned CLI subprocess, so a personal chat can't accidentally touch a
    # work-only MCP (and vice versa). ``None`` = "unset, use built-in
    # defaults"; explicit ``[]`` = "operator opted out of the defaults".
    disallowed_tools_personal: list[str] | None = None
    disallowed_tools_work: list[str] | None = None
    # Per-workspace claude.ai connector MCP toggle. ``None`` = default (on).
    # When off, ``CLAUDE_AI_CONNECTORS`` is added to the effective denylist.
    # Set via ``CIAO_CLAUDE_AI_MCPS_PERSONAL`` / ``_WORK`` (true/false;
    # ``default``/unset → default on).
    claude_ai_mcps_personal: bool | None = None
    claude_ai_mcps_work: bool | None = None
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    claude_mode: BridgeMode = "auto"
    restart_exit_code: int = 75
    auto_sync_on_start: bool = True
    auto_vault_index: bool = True
    auto_update_github_skills: bool = False
    pwa_port: int = 8443
    pwa_host: str = "0.0.0.0"
    gws_default_profile: str = "personal"
    # Models in `ollama.models` get rerouted to a local Ollama daemon via
    # the Anthropic-compatible API. Empty allowlist disables the routing.
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    # Auto-discover models installed on the local Ollama daemon at startup
    # (GET /api/tags against ``ollama.local_url``) and surface them in the
    # model pickers. Disable with ``CIAO_OLLAMA_LOCAL_DISCOVERY=0``.
    ollama_local_discovery: bool = True
    # Post-archive insights extraction: when a chat is archived, run the raw
    # Claude Code session JSONL through a fast cheap model and append a
    # `## Session insights` section to the archived markdown.
    insights_enabled: bool = True
    insights_size_gate_turns: int = 5
    insights_model: str = "deepseek-v4-flash:cloud"
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

    # Comma-separated list of models for the critique / adversarial-review skill.
    # Empty string defaults to the script's built-in panel.
    critique_models: str = ""
    # OpenRouter (Anthropic-compatible) routing. Available when
    # OPENROUTER_API_KEY is set; aliases resolve to per-tier models.
    openrouter: OpenRouterSettings = field(default_factory=OpenRouterSettings)
    # Bounded agent-managed memory files at ``~/.ciao/memory.md`` and
    # ``~/.ciao/user.md``. Injected as a frozen snapshot into the Claude
    # system prompt at session start; edited via the ``memory`` MCP tool.
    # See ``ciao/memory_injector.py`` and ``ciao/memory_tool.py``.
    memory_enabled: bool = True
    memory_char_limit: int = 2200
    user_char_limit: int = 1375

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.state_path = Path(self.state_path).expanduser().resolve()
        self.media_root = Path(self.media_root).expanduser().resolve()
        vault_root = Path(self.vault_root).expanduser()
        if not vault_root.is_absolute():
            vault_root = self.workspace_root / vault_root
        self.vault_root = vault_root.resolve()
        if not self.workspaces:
            self.workspaces = _legacy_workspaces(
                default_model_personal=self.default_model_personal,
                default_model_work=self.default_model_work,
                disallowed_tools_personal=self.disallowed_tools_personal,
                disallowed_tools_work=self.disallowed_tools_work,
                claude_ai_mcps_personal=self.claude_ai_mcps_personal,
                claude_ai_mcps_work=self.claude_ai_mcps_work,
                gws_default_profile=self.gws_default_profile,
            )

    def workspace(self, name: str | None) -> WorkspaceConfig | None:
        if not name:
            return None
        return self.workspaces.get(name)

    def workspace_names(self) -> list[str]:
        return list(self.workspaces.keys())

    def default_model_for_workspace(self, workspace: str | None) -> str:
        """Pick the new-chat / new-schedule default for a workspace.

        Falls back to ``claude_default_model`` when the per-workspace
        knob is empty or the workspace is unknown.
        """
        workspace_config = self.workspace(workspace)
        if workspace_config and workspace_config.default_model:
            return workspace_config.default_model
        return self.claude_default_model

    def default_provider_for_workspace(self, workspace: str | None) -> str:
        workspace_config = self.workspace(workspace)
        if workspace_config and workspace_config.default_provider == "claude":
            return workspace_config.default_provider
        return "claude"

    def claude_ai_mcps_for_workspace(self, workspace: str | None) -> bool:
        """Whether claude.ai connector MCPs are exposed in this workspace.

        ``None`` on the workspace config resolves to the default: True.
        """
        workspace_config = self.workspace(workspace)
        if workspace_config is None:
            return True
        value = workspace_config.claude_ai_mcps
        if value is None:
            return True
        return value

    def disallowed_tools_for_workspace(self, workspace: str | None) -> list[str]:
        """Tools to deny for a chat in this workspace.

        The effective denylist is the union of:

        * the claude.ai connector set (``CLAUDE_AI_CONNECTORS``) when
          ``claude_ai_mcps`` resolves to False, and
        * the workspace's extra tools (``disallowed_tools``), which defaults to
          ``_DEFAULT_EXTRA_DISALLOWED_TOOLS_PERSONAL`` (n8n) for personal and
          ``[]`` for every other workspace.

        So a personal chat defaults to blocking all 8 claude.ai connectors plus
        n8n; a work chat defaults to no denylist. Both are overridable: the
        toggle via ``CIAO_CLAUDE_AI_MCPS_PERSONAL`` / ``CIAO_CLAUDE_AI_MCPS_WORK``
        / the PWA switch, the extras via ``CIAO_DISALLOWED_TOOLS_PERSONAL`` /
        ``CIAO_DISALLOWED_TOOLS_WORK`` / the "Extra disallowed tools" field.
        """
        workspace_config = self.workspace(workspace)
        if workspace_config is None:
            return []
        connectors = (
            list(CLAUDE_AI_CONNECTORS)
            if not self.claude_ai_mcps_for_workspace(workspace)
            else []
        )
        extras = workspace_config.disallowed_tools
        if extras is None:
            extras = (
                list(_DEFAULT_EXTRA_DISALLOWED_TOOLS_PERSONAL)
                if workspace_config.name == "personal"
                else []
            )
        # Union, preserving order, deduped.
        seen: set[str] = set()
        effective: list[str] = []
        for tool in (*connectors, *extras):
            if tool not in seen:
                seen.add(tool)
                effective.append(tool)
        return effective

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CiaoConfig":
        if env is None:
            workspace_env_val = os.environ.get("CIAO_WORKSPACE", "").strip() or os.environ.get("TELEGRAM_BRIDGE_WORKSPACE", "").strip() or "."
            dotenv_path = Path(workspace_env_val).expanduser().resolve() / ".env"
            if dotenv_path.exists():
                from dotenv import load_dotenv
                load_dotenv(dotenv_path)

        source = env if env is not None else os.environ

        pwa_auth_required_raw = source.get("PWA_AUTH_REQUIRED", "").strip().lower()
        pwa_auth_required = pwa_auth_required_raw not in {"false", "0", "no", "n"}

        pwa_auth_token = source.get("PWA_AUTH_TOKEN", "").strip()
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
                pwa_auth_token = "ciao-insecure-fallback-secret-key"

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
        ollama_models = tuple(_split_csv(source.get("CIAO_OLLAMA_MODELS", "")))
        ollama_local_models = tuple(
            _split_csv(source.get("CIAO_OLLAMA_LOCAL_MODELS", ""))
        )
        # Append Ollama models (cloud allowlist + manually pinned local ones)
        # to the picker so the PWA shows them. Keeps claude_models as the
        # single source of truth for the API. Auto-discovered local models
        # are appended later in main() once the daemon has been probed.
        claude_models = list(claude_models) + [
            m
            for m in (*ollama_models, *ollama_local_models)
            if m not in claude_models
        ]
        claude_default_model = claude_models[0] if claude_models else "opus"
        # API key flips the upstream from device-linked daemon to direct
        # cloud. When the operator sets a key without an explicit URL,
        # default to ollama.com so the spawned CLI never has to round-trip
        # through a daemon that wasn't started.
        ollama_api_key = source.get("CIAO_OLLAMA_API_KEY", "").strip() or "ollama"
        ollama_url = source.get("CIAO_OLLAMA_URL", "").strip()
        if not ollama_url:
            ollama_url = (
                "https://ollama.com"
                if ollama_api_key != "ollama"
                else "http://localhost:11434"
            )
        ollama_title_model = (
            source.get("CIAO_OLLAMA_TITLE_MODEL", "").strip() or "gemma4:e2b-it-qat"
        )
        ollama_haiku_model = (
            source.get("CIAO_OLLAMA_HAIKU_MODEL", "").strip()
            or "deepseek-v4-flash:cloud"
        )
        ollama_sonnet_model = (
            source.get("CIAO_OLLAMA_SONNET_MODEL", "").strip()
            or "kimi-k2.7-code:cloud"
        )
        ollama_opus_model = (
            source.get("CIAO_OLLAMA_OPUS_MODEL", "").strip()
            or "glm-5.2:cloud"
        )
        ollama_settings = OllamaSettings(
            models=ollama_models,
            base_url=ollama_url,
            api_key=ollama_api_key,
            cookie=source.get("CIAO_OLLAMA_COOKIE", "").strip(),
            title_model=ollama_title_model,
            haiku_model=ollama_haiku_model,
            sonnet_model=ollama_sonnet_model,
            opus_model=ollama_opus_model,
            local_models=ollama_local_models,
            local_url=source.get("CIAO_OLLAMA_LOCAL_URL", "").strip()
            or "http://localhost:11434",
        )

        openrouter_settings = OpenRouterSettings(
            api_key=source.get("OPENROUTER_API_KEY", "").strip(),
            base_url=source.get("CIAO_OPENROUTER_BASE_URL", "").strip()
            or "https://openrouter.ai/api",
            haiku_model=source.get("CIAO_OPENROUTER_HAIKU_MODEL", "").strip()
            or "anthropic/claude-haiku-latest",
            sonnet_model=source.get("CIAO_OPENROUTER_SONNET_MODEL", "").strip()
            or "anthropic/claude-sonnet-latest",
            opus_model=source.get("CIAO_OPENROUTER_OPUS_MODEL", "").strip()
            or "anthropic/claude-opus-latest",
            models=tuple(_split_csv(source.get("CIAO_OPENROUTER_MODELS", ""))),
        )

        default_model_personal = source.get("CLAUDE_DEFAULT_MODEL_PERSONAL", "").strip()
        default_model_work = source.get("CLAUDE_DEFAULT_MODEL_WORK", "").strip()
        disallowed_tools_personal = _parse_disallowed_tools(
            source.get("CIAO_DISALLOWED_TOOLS_PERSONAL", "")
        )
        disallowed_tools_work = _parse_disallowed_tools(
            source.get("CIAO_DISALLOWED_TOOLS_WORK", "")
        )
        claude_ai_mcps_personal = coerce_claude_ai_mcps(
            source.get("CIAO_CLAUDE_AI_MCPS_PERSONAL", "")
        )
        claude_ai_mcps_work = coerce_claude_ai_mcps(
            source.get("CIAO_CLAUDE_AI_MCPS_WORK", "")
        )
        gws_default_profile = source.get("GWS_PROFILE", "personal").strip() or "personal"
        workspaces = _parse_workspaces_json(workspaces_json) or _legacy_workspaces(
            default_model_personal=default_model_personal,
            default_model_work=default_model_work,
            disallowed_tools_personal=disallowed_tools_personal,
            disallowed_tools_work=disallowed_tools_work,
            claude_ai_mcps_personal=claude_ai_mcps_personal,
            claude_ai_mcps_work=claude_ai_mcps_work,
            gws_default_profile=gws_default_profile,
        )

        dev_mode_raw = source.get("CIAO_DEV_MODE", "").strip().lower()
        dev_mode = dev_mode_raw in {"true", "1", "yes", "y"}

        vault_mode = source.get("CIAO_VAULT_MODE", "scratch").strip().lower()
        if vault_mode not in {"existing", "scratch"}:
            vault_mode = "scratch"

        return cls(
            pwa_auth_token=pwa_auth_token,
            workspace_root=workspace_root,
            state_path=state_path,
            media_root=media_root,
            pwa_auth_required=pwa_auth_required,
            dev_mode=dev_mode,
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
            openai_api_key=source.get("OPENAI_API_KEY", "").strip() or None,
            title_model=source.get("CIAO_TITLE_MODEL", "").strip() or "haiku",
            title_model_override=source.get("CIAO_TITLE_MODEL_OVERRIDE", "").strip(),
            transcription_engine=(
                source.get("CIAO_TRANSCRIPTION_ENGINE", "").strip().lower()
                if source.get("CIAO_TRANSCRIPTION_ENGINE", "").strip().lower()
                in {"cloud", "local"}
                else "cloud"
            ),
            transcription_local_model=source.get(
                "CIAO_TRANSCRIPTION_LOCAL_MODEL", ""
            ).strip()
            or "mlx-community/whisper-large-v3-turbo",
            tts_engine=(
                source.get("CIAO_TTS_ENGINE", "").strip().lower()
                if source.get("CIAO_TTS_ENGINE", "").strip().lower()
                in {"cloud", "local"}
                else "cloud"
            ),
            tts_cloud_voice=source.get("CIAO_TTS_CLOUD_VOICE", "").strip() or "nova",
            tts_local_voice=source.get("CIAO_TTS_LOCAL_VOICE", "").strip()
            or "af_heart",
            ollama_local_discovery=source.get(
                "CIAO_OLLAMA_LOCAL_DISCOVERY", ""
            ).strip().lower()
            not in {"0", "false", "no", "off"},
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
            ollama=ollama_settings,
            openrouter=openrouter_settings,
            default_model_personal=default_model_personal,
            default_model_work=default_model_work,
            disallowed_tools_personal=disallowed_tools_personal,
            disallowed_tools_work=disallowed_tools_work,
            claude_ai_mcps_personal=claude_ai_mcps_personal,
            claude_ai_mcps_work=claude_ai_mcps_work,
            workspaces=workspaces,
            insights_enabled=source.get("CIAO_INSIGHTS_DISABLED", "").strip().lower()
            in {"", "0", "false", "no", "off"},
            insights_size_gate_turns=int(
                source.get("CIAO_INSIGHTS_MIN_TURNS", "5") or "5"
            ),
            insights_model=source.get("CIAO_INSIGHTS_MODEL", "").strip()
            or "deepseek-v4-flash:cloud",
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
            memory_enabled=source.get("CIAO_MEMORY_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            memory_char_limit=int(
                source.get("CIAO_MEMORY_CHAR_LIMIT", "").strip() or "2200"
            ),
            user_char_limit=int(
                source.get("CIAO_USER_CHAR_LIMIT", "").strip() or "1375"
            ),
        )


logger = logging.getLogger(__name__)


def refresh_local_ollama_models(config: CiaoConfig) -> bool:
    """Re-discover local Ollama daemon models and merge into the live config.

    Called at startup (ciao.main) and when the Settings → Models tab loads,
    so a freshly ``ollama pull``-ed model shows up without a restart.
    Additive while running: models removed from the daemon drop out on the
    next restart. Models already in the cloud allowlist keep their cloud
    routing. Returns True when the local set changed.
    """
    if not config.ollama_local_discovery:
        return False
    from ciao.providers.ollama import discover_local_models

    discovered = discover_local_models(config.ollama.local_url)
    merged = tuple(
        dict.fromkeys(
            [
                *config.ollama.local_models,
                *(m for m in discovered if m not in config.ollama.models),
            ]
        )
    )
    if merged == config.ollama.local_models:
        return False
    config.ollama = replace(config.ollama, local_models=merged)
    for m in merged:
        if m not in config.claude_models:
            config.claude_models.append(m)
    logger.info("Local Ollama models available: %s", ", ".join(merged))
    return True



def refresh_openrouter_models(config: "CiaoConfig") -> bool:
    """Discover models from OpenRouter and merge into the picker.

    Called at startup and when the Settings tab loads, so the OpenRouter
    model list is populated dynamically from the live catalogue rather than
    requiring a static allowlist. Returns True when the set changed.
    """
    from ciao.providers.openrouter import discover_models, merge_discovered

    if not config.openrouter.available:
        return False
    discovered = discover_models(config.openrouter, anthropic_only=False)
    if not discovered:
        return False
    before = config.openrouter.models
    config.openrouter = merge_discovered(config.openrouter, discovered)
    for m in config.openrouter.models:
        if m not in config.claude_models:
            config.claude_models.append(m)
    return config.openrouter.models != before


def refresh_cloud_ollama_models(config: "CiaoConfig") -> bool:
    """Discover models from Ollama Cloud and merge into the live config.

    Called at startup and when the Settings tab loads, so the Ollama Cloud
    model list is populated dynamically from the live catalogue. Returns True
    when the set changed.
    """
    if not config.ollama.api_key or config.ollama.api_key == "ollama":
        return False
    from ciao.providers.ollama import discover_cloud_models
    discovered = discover_cloud_models(config.ollama)
    if not discovered:
        return False
    before = config.ollama.models
    merged = tuple(dict.fromkeys([*config.ollama.models, *discovered]))
    if merged == before:
        return False
    config.ollama = replace(config.ollama, models=merged)
    for m in merged:
        if m not in config.claude_models:
            config.claude_models.append(m)
    logger.info("Ollama Cloud models available: %s", ", ".join(merged))
    return True


# Backward-compatible alias used by project_chats.py and other modules
BridgeConfig = CiaoConfig

