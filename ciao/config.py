"""Configuration loading for the Ciao server."""

from __future__ import annotations

import logging
import json
import os
import re
import secrets
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path

from ciao.execution_modes import normalize_claude_mode
from ciao.models import BridgeMode
from ciao.providers.ollama import OllamaSettings
from ciao.providers.pi import PiSettings


# Default denylist for personal-workspace chats: every claude.ai connector MCP,
# plus the self-hosted n8n MCP (project-scoped in .mcp.json). Those are work-only
# tools (Airtable, Atlassian, Slack, Asana, BigQuery, incident.io, Salesforce,
# Sentry, n8n). Listed at the server level so the CLI denies every tool the server
# exposes without enumerating them. Override with ``CIAO_DISALLOWED_TOOLS_PERSONAL``
# (CSV; literal ``none`` clears).
_DEFAULT_DISALLOWED_TOOLS_PERSONAL: tuple[str, ...] = (
    "mcp__claude_ai_Airtable",
    "mcp__claude_ai_Asana",
    "mcp__claude_ai_Atlassian",
    "mcp__claude_ai_Google_Cloud_BigQuery",
    "mcp__claude_ai_Salesforce",
    "mcp__claude_ai_Sentry",
    "mcp__claude_ai_Slack",
    "mcp__claude_ai_incident_io",
    "mcp__n8n_mcp",
)


@dataclass(slots=True)
class WorkspaceConfig:
    """Config for one logical chat workspace."""

    name: str
    vault_root: str
    default_provider: str = "claude"
    default_model: str = ""
    disallowed_tools: list[str] | None = None
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
            model_bucket="personal",
        ),
        "work": WorkspaceConfig(
            name="work",
            vault_root="work",
            default_provider="claude",
            default_model=default_model_work,
            disallowed_tools=disallowed_tools_work,
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


def _sanitize_device_name(raw: str) -> str:
    """Make a string safe to use as a git branch segment.

    Lowercases, collapses any run of non-alphanumeric chars to a single dash,
    and trims leading/trailing dashes. Empty input returns "".
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return cleaned


def _default_device_name() -> str:
    """Sanitized machine hostname, used when CIAO_DEVICE_NAME is unset."""
    return _sanitize_device_name(socket.gethostname().split(".")[0]) or "device"


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
    git_direct_main: bool = False
    dev_mode: bool = False
    vault_mode: str = "scratch"
    bootstrap_mode: bool = False
    vault_root: Path = Path("memory-vault")
    extra_workspace_roots: list[Path] = field(default_factory=list)
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
    # OPENAI_API_KEY) or ``local`` (mlx-whisper on Apple Silicon; falls
    # back to cloud when the package is missing). Runtime-overridable from
    # the PWA Settings → Models tab.
    transcription_engine: str = "cloud"
    transcription_local_model: str = "mlx-community/whisper-large-v3-turbo"
    claude_models: list[str] = field(default_factory=lambda: ["opus", "sonnet", "haiku"])
    claude_default_model: str = "opus"
    # Per-workspace default models. Empty string falls back to
    # claude_default_model. Lets the personal workspace default to a
    # cheap Ollama model while work stays on Anthropic.
    default_model_personal: str = ""
    default_model_work: str = ""
    # Per-workspace tool denylists. Forwarded to ``ClaudeAgentOptions.disallowed_tools``
    # for the spawned CLI subprocess, so a personal chat can't accidentally
    # touch a work-only MCP (and vice versa). ``None`` = "unset, use built-in
    # defaults"; explicit ``[]`` = "operator opted out of the defaults".
    disallowed_tools_personal: list[str] | None = None
    disallowed_tools_work: list[str] | None = None
    workspaces: dict[str, WorkspaceConfig] = field(default_factory=dict)
    claude_mode: BridgeMode = "auto"
    restart_exit_code: int = 75
    auto_sync_on_start: bool = True
    auto_vault_index: bool = True
    auto_update_github_skills: bool = True
    pwa_port: int = 8443
    pwa_host: str = "0.0.0.0"
    gws_default_profile: str = "personal"
    # Device identity for the per-device working-branch flow. Every instance
    # works on its own ``dev/<device_name>`` branch (cut from origin/main) and
    # hands work back to ``main`` via the Settings "commit" button. Set via
    # ``CIAO_DEVICE_NAME``; defaults to a sanitized machine hostname.
    device_name: str = "device"
    # Only the always-on "main" device dispatches scheduled automations, so
    # schedules never double-fire when an occasional dev box is also running.
    # Off by default (opt-in); the main device sets ``CIAO_DISPATCH_SCHEDULES=1``.
    dispatch_schedules: bool = False
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
    # Reasoning-heavy task: a 3B model can't parse a 15 KB SKILL.md plus a
    # trajectory dump and produce a coherent edit. Default to the same
    # sonnet/opus-tier Ollama model the other admin schedules use
    # (memory curation, weekly review, dependency check).
    skill_evolution_model: str = "kimi-k2.7-code:cloud"
    # Pi (coding agent) provider configuration.
    pi: PiSettings = field(default_factory=PiSettings)
    # Comma-separated list of models for the critique / adversarial-review skill.
    # Empty string defaults to the script's built-in panel.
    critique_models: str = ""
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
        if workspace_config and workspace_config.default_provider in {"claude", "pi"}:
            return workspace_config.default_provider
        return "claude"

    def disallowed_tools_for_workspace(self, workspace: str | None) -> list[str]:
        """Tools to deny for a chat in this workspace.

        Personal chats default to denying every claude.ai connector MCP
        (Airtable, Atlassian, Slack, Asana, BigQuery, incident.io,
        Salesforce, Sentry) since those are work tools. Work
        chats default to no denylist. Both are overridable via env.
        """
        workspace_config = self.workspace(workspace)
        if workspace_config is None:
            return []
        if workspace_config.name == "personal":
            if workspace_config.disallowed_tools is None:
                return list(_DEFAULT_DISALLOWED_TOOLS_PERSONAL)
            return list(workspace_config.disallowed_tools)
        if workspace_config.disallowed_tools is None:
            return []
        return list(workspace_config.disallowed_tools)

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
            source.get("CIAO_OLLAMA_TITLE_MODEL", "").strip() or "ministral-3:3b"
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
            or "minimax-m3:cloud"
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

        pi_models = tuple(_split_csv(source.get("CIAO_PI_MODELS", "")))
        pi_provider = source.get("CIAO_PI_PROVIDER", "ollama").strip()
        pi_base_url = source.get("CIAO_PI_OLLAMA_URL", "").strip()
        if not pi_base_url:
            pi_base_url = "http://localhost:11434"
        pi_default_model = source.get("CIAO_PI_DEFAULT_MODEL", "").strip()
        pi_settings = PiSettings(
            models=pi_models,
            provider=pi_provider,
            base_url=pi_base_url,
            default_model=pi_default_model,
            local_models=ollama_local_models,
        )

        # Extra read-only roots the workspace-file/image/binary viewers may
        # serve. Accepts a CSV override for additional paths (e.g. `~/.claude`
        # if you want to inspect it from the PWA).
        extra_workspace_roots: list[Path] = []
        for raw in _split_csv(source.get("CIAO_WORKSPACE_EXTRA_ROOTS", "")):
            try:
                p = Path(raw).expanduser().resolve()
            except (OSError, ValueError):
                continue
            if p != workspace_root and p.exists() and p not in extra_workspace_roots:
                extra_workspace_roots.append(p)

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

        git_direct_main_raw = source.get("CIAO_GIT_DIRECT_MAIN", "").strip().lower()
        git_direct_main = git_direct_main_raw in {"true", "1", "yes", "y"}

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
            git_direct_main=git_direct_main,
            dev_mode=dev_mode,
            vault_mode=vault_mode,
            bootstrap_mode=bootstrap_mode,
            vault_root=vault_root,
            extra_workspace_roots=extra_workspace_roots,
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
            ollama_local_discovery=source.get(
                "CIAO_OLLAMA_LOCAL_DISCOVERY", ""
            ).strip().lower()
            not in {"0", "false", "no", "off"},
            claude_models=list(claude_models or ["opus", "sonnet", "haiku"])
            + [m for m in pi_models if m not in claude_models],
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
            auto_update_github_skills=source.get("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "true").strip().lower()
            not in {"0", "false", "no", "off"},
            pwa_port=int(source.get("PWA_PORT", "8443")),
            pwa_host=source.get("PWA_HOST", "0.0.0.0").strip(),
            gws_default_profile=gws_default_profile,
            device_name=(
                _sanitize_device_name(source.get("CIAO_DEVICE_NAME", ""))
                or _default_device_name()
            ),
            dispatch_schedules=source.get("CIAO_DISPATCH_SCHEDULES", "").strip().lower()
            in {"1", "true", "yes", "on"},
            ollama=ollama_settings,
            pi=pi_settings,
            default_model_personal=default_model_personal,
            default_model_work=default_model_work,
            disallowed_tools_personal=disallowed_tools_personal,
            disallowed_tools_work=disallowed_tools_work,
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
            skill_evolution_model=source.get(
                "CIAO_SKILL_EVOLUTION_MODEL", ""
            ).strip()
            or "kimi-k2.7-code:cloud",
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
    routing. Returns True when the local set changed (and Pi's models.json
    was rewritten to match).
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
    # Keep Pi's view of the local daemon in sync so its model resolver
    # dispatches these under --provider ollama-local.
    config.pi = replace(config.pi, local_models=merged)
    for m in merged:
        if m not in config.claude_models:
            config.claude_models.append(m)
    logger.info("Local Ollama models available: %s", ", ".join(merged))
    try:
        from ciao.providers.pi import ensure_models_json

        ensure_models_json(
            config.pi,
            ollama_base_url=config.ollama.base_url,
            ollama_api_key=config.ollama.api_key,
            extra_models=config.ollama.models,
            local_models=config.ollama.local_models,
            local_url=config.ollama.local_url,
        )
    except Exception:
        logger.warning("Pi models.json refresh failed", exc_info=True)
    return True


# Backward-compatible alias used by project_chats.py and other modules
BridgeConfig = CiaoConfig
