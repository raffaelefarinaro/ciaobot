"""Tests for routing selected models to a local Ollama daemon.

Ollama exposes an Anthropic-compatible API on the local host, so the
Claude CLI subprocess can target an Ollama-hosted model by overriding
three environment variables on its way in. The helper under test decides
when to inject those overrides and what to fill them with.
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.providers.ollama import (
    OllamaSettings,
    is_ollama_model,
    ollama_env_for_model,
)
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def test_is_ollama_model_matches_dynamic_shape() -> None:
    # Cloud configured: any :tag/:cloud-shaped id routes dynamically.
    cloud = OllamaSettings(
        models=("kimi-k2.7-code:cloud", "deepseek-v4-pro:cloud"),
        base_url="https://ollama.com", api_key="sk-cloud",
    )
    assert is_ollama_model("kimi-k2.7-code:cloud", cloud)
    assert is_ollama_model("deepseek-v4-pro:cloud", cloud)
    assert is_ollama_model("some-other:cloud", cloud)  # not in allowlist but shaped
    assert not is_ollama_model("opus", cloud)
    assert not is_ollama_model("anthropic/claude-haiku-4.5", cloud)  # OpenRouter shape
    assert not is_ollama_model("", cloud)
    # Local daemon only (no cloud key): only local_models route.
    local = OllamaSettings(local_models=("gemma4:12b-it-qat",), base_url="http://localhost:11434")
    assert is_ollama_model("gemma4:12b-it-qat", local)
    assert not is_ollama_model("kimi-k2.7-code:cloud", local)


def test_ollama_env_returns_three_overrides_for_cloud_model() -> None:
    settings = OllamaSettings(
        models=("kimi-k2.7-code:cloud",),
        base_url="https://ollama.com", api_key="sk-cloud",
    )
    env = ollama_env_for_model("kimi-k2.7-code:cloud", settings)
    assert env == {
        "ANTHROPIC_AUTH_TOKEN": "sk-cloud",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "https://ollama.com",
    }


def test_ollama_env_uses_explicit_api_key_when_set() -> None:
    """Direct cloud auth: api_key replaces the literal 'ollama' token.

    Anthropic SDK turns ANTHROPIC_AUTH_TOKEN into an `Authorization: Bearer`
    header, which is what ollama.com accepts. Without this override the
    daemon-relay flow stays in effect.
    """
    settings = OllamaSettings(
        models=("kimi-k2.7-code:cloud",),
        base_url="https://ollama.com",
        api_key="cdc44447bdb94a6fa498ee88be7ae8cc._67fJsgQ5yNkr2kY4de0hIhv",
    )
    env = ollama_env_for_model("kimi-k2.7-code:cloud", settings)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "cdc44447bdb94a6fa498ee88be7ae8cc._67fJsgQ5yNkr2kY4de0hIhv"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"


def test_ollama_env_returns_empty_for_anthropic_model() -> None:
    settings = OllamaSettings(
        models=("kimi-k2.7-code:cloud",),
        base_url="http://localhost:11434",
    )
    assert ollama_env_for_model("opus", settings) == {}
    assert ollama_env_for_model("claude-sonnet-4-6", settings) == {}


def test_ollama_env_honours_custom_base_url() -> None:
    settings = OllamaSettings(
        models=("kimi-k2.7-code:cloud",),
        base_url="http://ollama.internal:11434", api_key="sk-cloud",
    )
    env = ollama_env_for_model("kimi-k2.7-code:cloud", settings)
    assert env["ANTHROPIC_BASE_URL"] == "http://ollama.internal:11434"


def test_ollama_settings_disabled_when_no_cloud_key() -> None:
    """No cloud key and not a local model -> no routing, even with a base URL."""
    settings = OllamaSettings(models=(), base_url="http://localhost:11434")
    assert not is_ollama_model("kimi-k2.7-code:cloud", settings)
    assert ollama_env_for_model("kimi-k2.7-code:cloud", settings) == {}


def test_ciao_config_parses_ollama_env(monkeypatch) -> None:
    """CIAO_OLLAMA_MODELS / CIAO_OLLAMA_URL feed an OllamaSettings on config."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud, deepseek-v4-pro:cloud")
    monkeypatch.setenv("CIAO_OLLAMA_URL", "http://ollama.box:11434")
    monkeypatch.delenv("CIAO_OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_TITLE_MODEL", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_OPUS_MODEL", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_SONNET_MODEL", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_HAIKU_MODEL", raising=False)
    config = CiaoConfig.from_env()
    assert config.ollama.models == ("kimi-k2.7-code:cloud", "deepseek-v4-pro:cloud")
    assert config.ollama.base_url == "http://ollama.box:11434"
    assert config.ollama.api_key == "ollama"  # default daemon-relay token
    # Default cheap free-tier title model when nothing is set.
    assert config.ollama.title_model == "ministral-3:3b"
    assert config.ollama.opus_model == "minimax-m3:cloud"
    assert config.ollama.sonnet_model == "kimi-k2.7-code:cloud"
    assert config.ollama.haiku_model == "deepseek-v4-flash:cloud"


def test_ciao_config_reads_ollama_title_model(monkeypatch) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CIAO_OLLAMA_TITLE_MODEL", "gemma3:4b")
    config = CiaoConfig.from_env()
    assert config.ollama.title_model == "gemma3:4b"


def test_ciao_config_api_key_implies_cloud_base_url(monkeypatch) -> None:
    """When CIAO_OLLAMA_API_KEY is set without an explicit URL, default to ollama.com.

    Skips the local daemon and goes straight at the cloud endpoint, since
    a bare API key is the cloud auth path.
    """
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CIAO_OLLAMA_API_KEY", "abc123")
    monkeypatch.delenv("CIAO_OLLAMA_URL", raising=False)
    config = CiaoConfig.from_env()
    assert config.ollama.api_key == "abc123"
    assert config.ollama.base_url == "https://ollama.com"


def test_ciao_config_explicit_url_wins_over_api_key_default(monkeypatch) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CIAO_OLLAMA_API_KEY", "abc123")
    monkeypatch.setenv("CIAO_OLLAMA_URL", "http://my-relay:11434")
    config = CiaoConfig.from_env()
    assert config.ollama.api_key == "abc123"
    assert config.ollama.base_url == "http://my-relay:11434"


def test_ciao_config_ollama_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_URL", raising=False)
    config = CiaoConfig.from_env()
    assert config.ollama.models == ()


def _make_manager(
    tmp_path: Path,
    ollama_models: tuple[str, ...] = (),
    *,
    ollama_api_key: str = "sk-cloud",
    ollama_base_url: str = "https://ollama.com",
) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OllamaSettings(
            models=ollama_models, base_url=ollama_base_url, api_key=ollama_api_key,
            haiku_model="deepseek-v4-flash:cloud",
            sonnet_model="kimi-k2.7-code:cloud",
            opus_model="minimax-m3:cloud",
        ),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_ollama_alias_uses_configured_tier_model(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path, ollama_models=("minimax-m3:cloud",))
    project = pcm.create_project("ollama-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="ollama-chat", model="opus")

    assert pcm._runtime_model_for_chat(chat) == "minimax-m3:cloud"
    env = pcm._build_extra_env(chat)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"


def test_build_extra_env_injects_ollama_overrides_for_allowlisted_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("ollama-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="ollama-chat")
    chat.model = "kimi-k2.7-code:cloud"

    env = pcm._build_extra_env(chat)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    # Existing GWS_PROFILE wiring must be preserved.
    assert env["GWS_PROFILE"] == "personal"


def test_build_extra_env_leaves_anthropic_chat_alone(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("anthropic-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="anthropic-chat")
    chat.model = "opus"
    chat.model_bucket = "work"  # pin Anthropic; personal would resolve to Ollama

    env = pcm._build_extra_env(chat)
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["GWS_PROFILE"] == "personal"


async def test_auto_title_uses_dedicated_ollama_title_model(
    tmp_path: Path, monkeypatch
) -> None:
    """When the chat is on an Ollama model, the title call uses the
    cheap dedicated ``OllamaSettings.title_model`` (free-tier-friendly,
    e.g. ``ministral-3:3b``) instead of the chat's own model — which
    may be subscription-gated and is overkill for 50-token titles.

    The env injection still happens so the call hits Ollama, not
    Anthropic.
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("ollama-title", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "kimi-k2.7-code:cloud"

    captured: dict = {}

    async def fake_generate(user, assistant, *, model, cwd, env=None, pi_settings=None, timeout_s=15.0):
        captured["model"] = model
        captured["env"] = env
        return "ollama-titled"

    monkeypatch.setattr(
        "ciao.web.project_chats._generate_chat_title", fake_generate
    )

    new_title = await pcm.auto_title_if_default(
        chat.chat_id, "what's the weather", "sunny"
    )
    assert new_title == "ollama-titled"
    # Dedicated cheap title model, not the chat's own (potentially gated) one.
    assert captured["model"] == "ministral-3:3b"
    assert captured["env"] is not None
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"


def test_create_chat_uses_personal_workspace_default(
    tmp_path: Path, monkeypatch
) -> None:
    """A new chat in a personal project picks ``CLAUDE_DEFAULT_MODEL_PERSONAL``."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_WORK", "opus")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("personal-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    assert chat.model == "kimi-k2.7-code:cloud"


def test_create_chat_uses_work_workspace_default(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_WORK", "opus")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("2026-q2-work-test", workspace="work")
    chat = pcm.create_chat(project.project_id, title="t")
    assert chat.model == "opus"


def test_provider_env_keeps_workspace_root_and_derives_active_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("GWS_PROFILE", "personal")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "test-token", "CIAO_WORKSPACE": str(tmp_path)})
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("2026-q2-work-test", workspace="work")
    chat = pcm.create_chat(project.project_id, title="t")

    env = pcm._build_extra_env(chat)

    assert env["CIAO_WORKSPACE"] == str(tmp_path.resolve())
    assert env["CIAO_ACTIVE_WORKSPACE"] == "work"
    assert env["CIAO_ACTIVE_PROJECT"] == project.project_id
    assert env["GWS_PROFILE"] == "work"


def test_provider_env_uses_configured_workspace_profile(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
                gws_profile="work",
                model_bucket="work",
            ),
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("client-test", workspace="client")
    chat = pcm.create_chat(project.project_id, title="t")

    env = pcm._build_extra_env(chat)

    assert env["CIAO_ACTIVE_WORKSPACE"] == "client"
    assert env["CIAO_ACTIVE_PROJECT"] == project.project_id
    assert env["GWS_PROFILE"] == "work"


def test_create_chat_explicit_model_wins_over_workspace_default(
    tmp_path: Path, monkeypatch
) -> None:
    """Caller-provided ``model`` always overrides the workspace default."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("explicit-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t", model="haiku")
    assert chat.model == "haiku"


def test_personal_workspace_denies_claude_ai_mcps_by_default(monkeypatch) -> None:
    """Personal chats should not see work-only claude.ai connector MCPs.

    Defaults block all 8 currently-known claude.ai connectors. Override
    with ``CIAO_DISALLOWED_TOOLS_PERSONAL=`` to restore them.
    """
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_DISALLOWED_TOOLS_PERSONAL", raising=False)
    monkeypatch.delenv("CIAO_DISALLOWED_TOOLS_WORK", raising=False)
    config = CiaoConfig.from_env()
    personal = config.disallowed_tools_for_workspace("personal")
    assert "mcp__claude_ai_Airtable" in personal
    assert "mcp__claude_ai_Atlassian" in personal
    assert "mcp__claude_ai_Slack" in personal
    assert "mcp__claude_ai_Salesforce" in personal
    assert "mcp__claude_ai_Sentry" in personal
    assert "mcp__claude_ai_incident_io" in personal
    assert "mcp__claude_ai_Asana" in personal
    assert "mcp__claude_ai_Google_Cloud_BigQuery" in personal
    # Self-hosted n8n MCP (project-scoped in .mcp.json) is work-only too.
    assert "mcp__n8n_mcp" in personal
    # Work workspace: defaults to empty (workspace-specific tools available).
    assert config.disallowed_tools_for_workspace("work") == []


def test_disallowed_tools_env_override_unions_with_toggle(monkeypatch) -> None:
    """CIAO_DISALLOWED_TOOLS_* sets the extra denylist. It unions with the
    claude.ai connector set when the toggle is off (the personal default), and
    stands alone when the toggle is on (the work default)."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_DISALLOWED_TOOLS_PERSONAL", "Bash,mcp__custom_tool")
    monkeypatch.setenv("CIAO_DISALLOWED_TOOLS_WORK", "mcp__claude_ai_Sentry")
    config = CiaoConfig.from_env()
    personal = config.disallowed_tools_for_workspace("personal")
    # Toggle defaults off for personal → connectors blocked, plus the extras.
    assert "mcp__claude_ai_Airtable" in personal
    assert "Bash" in personal
    assert "mcp__custom_tool" in personal
    # Work toggle defaults on → only the extra is blocked.
    assert config.disallowed_tools_for_workspace("work") == ["mcp__claude_ai_Sentry"]


def test_claude_ai_mcps_toggle_env_override(monkeypatch) -> None:
    """CIAO_CLAUDE_AI_MCPS_* controls the connector set independently of extras."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    # Flip personal on → connectors allowed, only the n8n extra stays.
    monkeypatch.setenv("CIAO_CLAUDE_AI_MCPS_PERSONAL", "true")
    config = CiaoConfig.from_env()
    assert config.disallowed_tools_for_workspace("personal") == ["mcp__n8n_mcp"]
    assert config.claude_ai_mcps_for_workspace("personal") is True
    # Flip work off → connectors blocked on top of any extras.
    monkeypatch.setenv("CIAO_CLAUDE_AI_MCPS_WORK", "false")
    monkeypatch.setenv("CIAO_DISALLOWED_TOOLS_WORK", "")
    config = CiaoConfig.from_env()
    work = config.disallowed_tools_for_workspace("work")
    assert "mcp__claude_ai_Airtable" in work
    assert config.claude_ai_mcps_for_workspace("work") is False


def test_disallowed_tools_personal_can_be_disabled(monkeypatch) -> None:
    """Fully clearing the personal denylist needs the toggle on AND extras
    cleared. An empty CIAO_DISALLOWED_TOOLS_PERSONAL still applies defaults
    (unset == empty); the literal ``none`` clears the extras only."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_DISALLOWED_TOOLS_PERSONAL", "")
    config = CiaoConfig.from_env()
    # Empty string still applies the defaults (since unset == empty).
    assert "mcp__claude_ai_Airtable" in config.disallowed_tools_for_workspace("personal")

    # "none" clears the extras, but the toggle default (off) still blocks
    # the connectors.
    monkeypatch.setenv("CIAO_DISALLOWED_TOOLS_PERSONAL", "none")
    config = CiaoConfig.from_env()
    assert "mcp__claude_ai_Airtable" in config.disallowed_tools_for_workspace("personal")
    assert "mcp__n8n_mcp" not in config.disallowed_tools_for_workspace("personal")

    # Flip the toggle on too → fully empty denylist.
    monkeypatch.setenv("CIAO_CLAUDE_AI_MCPS_PERSONAL", "true")
    config = CiaoConfig.from_env()
    assert config.disallowed_tools_for_workspace("personal") == []


def test_build_extra_env_does_not_carry_disallowed_tools(tmp_path: Path) -> None:
    """``disallowed_tools`` lives on AgentRequest, not on the env dict."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("env-vs-tools", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    env = pcm._build_extra_env(chat)
    assert "DISALLOWED_TOOLS" not in env


def test_effective_mode_downgrades_auto_to_bypass_for_ollama(tmp_path: Path) -> None:
    """Auto mode requires Anthropic's classifier API. Ollama doesn't expose
    one, so the SDK would prompt via ``can_use_tool`` for *every* tool
    call and the PWA would render an Approve/Deny card per call. Mapping
    auto → bypass for Ollama keeps tool execution flowing without prompts.
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("ollama-auto", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.model = "kimi-k2.7-code:cloud"
    chat.mode = "auto"

    assert pcm._effective_mode_for_chat(chat) == "bypass"


def test_effective_mode_leaves_anthropic_auto_untouched(tmp_path: Path) -> None:
    """Anthropic models keep ``auto`` so the server-side classifier runs."""
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("anthropic-auto", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.model = "opus"
    chat.model_bucket = "work"  # pin Anthropic
    chat.mode = "auto"

    assert pcm._effective_mode_for_chat(chat) == "auto"


def test_effective_mode_passes_non_auto_modes_through_for_ollama(
    tmp_path: Path,
) -> None:
    """Plan/normal/bypass don't depend on the classifier — pass through."""
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("ollama-modes", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.model = "kimi-k2.7-code:cloud"

    chat.mode = "plan"
    assert pcm._effective_mode_for_chat(chat) == "plan"
    chat.mode = "bypass"
    assert pcm._effective_mode_for_chat(chat) == "bypass"
    chat.mode = "normal"
    assert pcm._effective_mode_for_chat(chat) == "normal"


def test_pcm_disallowed_tools_for_chat_routes_by_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    pcm = _make_manager(tmp_path)
    personal_proj = pcm.create_project("p", workspace="personal")
    work_proj = pcm.create_project("2026-q2-w", workspace="work")
    p_chat = pcm.create_chat(personal_proj.project_id, title="p")
    w_chat = pcm.create_chat(work_proj.project_id, title="w")

    p_disallowed = pcm.disallowed_tools_for_chat(p_chat)
    w_disallowed = pcm.disallowed_tools_for_chat(w_chat)
    assert "mcp__claude_ai_Airtable" in p_disallowed
    assert w_disallowed == []


def test_default_model_for_workspace_falls_back_to_global(monkeypatch) -> None:
    """When per-workspace defaults aren't set, use ``claude_default_model``."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CLAUDE_DEFAULT_MODEL_PERSONAL", raising=False)
    monkeypatch.delenv("CLAUDE_DEFAULT_MODEL_WORK", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    config = CiaoConfig.from_env()
    assert config.default_model_for_workspace("personal") == config.claude_default_model
    assert config.default_model_for_workspace("work") == config.claude_default_model


def test_resolve_schedule_default_model_uses_personal_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    """When a schedule is being created against a personal project,
    the default model resolves to ``CLAUDE_DEFAULT_MODEL_PERSONAL``.
    """
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_WORK", "opus")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("personal-sched", workspace="personal")
    assert pcm.schedule_default_model(project.project_id) == "kimi-k2.7-code:cloud"


def test_resolve_schedule_default_model_uses_work_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_WORK", "opus")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("2026-q2-sched", workspace="work")
    assert pcm.schedule_default_model(project.project_id) == "opus"


def test_update_chat_rejects_cross_provider_switch_with_history(
    tmp_path: Path, monkeypatch
) -> None:
    """A chat with conversation history can't be flipped between Anthropic
    and Ollama mid-stream: the spawned CLI subprocess has the wrong
    `ANTHROPIC_*` env vars baked in. Surface a clear error instead of
    silently failing on the next message.
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("cross-provider", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus")
    chat.model_bucket = "work"  # opus = Anthropic; kimi = Ollama -> cross-provider
    # Simulate a chat that has had a turn (history exists on disk).
    chat.user_turn_count = 1
    chat.session_id = "sess-existing"

    try:
        pcm.update_chat(chat.chat_id, model="kimi-k2.7-code:cloud")
    except ValueError as exc:
        assert "close this chat" in str(exc).lower()
        assert "fresh" in str(exc).lower() or "new" in str(exc).lower()
        return
    raise AssertionError("expected ValueError on cross-provider switch")


def test_update_chat_rejects_ollama_to_anthropic_with_history(
    tmp_path: Path, monkeypatch
) -> None:
    """Symmetric: Ollama → Anthropic is also a cross-provider swap."""
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("reverse", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="kimi-k2.7-code:cloud")
    chat.model_bucket = "work"  # target opus = Anthropic; kimi = Ollama
    chat.user_turn_count = 3

    try:
        pcm.update_chat(chat.chat_id, model="opus")
    except ValueError as exc:
        assert "close this chat" in str(exc).lower()
        return
    raise AssertionError("expected ValueError on cross-provider switch")


def test_update_chat_allows_cross_provider_on_fresh_chat(
    tmp_path: Path, monkeypatch
) -> None:
    """Empty chat (no turns, no session) → no spawned subprocess → safe to swap."""
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("fresh", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus")
    # Fresh chat: no user_turn_count, no session_id.

    updated = pcm.update_chat(chat.chat_id, model="kimi-k2.7-code:cloud")
    assert updated is not None
    assert updated.model == "kimi-k2.7-code:cloud"


def test_update_chat_allows_same_provider_swap_with_history(
    tmp_path: Path, monkeypatch
) -> None:
    """opus → sonnet (both Anthropic) is fine via SDK ``set_model()`` —
    same env, same subprocess, no spawn needed.

    Uses a work-workspace chat so both aliases genuinely stay on the
    Anthropic upstream. (In a personal chat with only the sonnet tier
    allowlisted, opus → sonnet would cross Anthropic → Ollama and is now
    correctly rejected by the bucket-aware guard.)
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("same-provider", workspace="work")
    chat = pcm.create_chat(project.project_id, model="opus")
    chat.user_turn_count = 5
    chat.session_id = "sess-x"

    updated = pcm.update_chat(chat.chat_id, model="sonnet")
    assert updated is not None
    assert updated.model == "sonnet"


def test_schedule_default_model_falls_back_for_unknown_project(
    tmp_path: Path, monkeypatch
) -> None:
    """No project_id (or unknown) → fall back to claude_default_model."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL_PERSONAL", "kimi-k2.7-code:cloud")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig.from_env()
    config.workspace_root = tmp_path
    config.state_path = runtime / "state.json"
    config.media_root = runtime / "media"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    assert pcm.schedule_default_model(None) == config.claude_default_model
    assert pcm.schedule_default_model("does-not-exist") == config.claude_default_model


async def test_auto_title_honours_custom_ollama_title_model(
    tmp_path: Path, monkeypatch
) -> None:
    """``CIAO_OLLAMA_TITLE_MODEL`` overrides the default title model."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OllamaSettings(
            models=("kimi-k2.7-code:cloud",),
            base_url="http://localhost:11434",
            title_model="qwen3:8b",
        ),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("custom-title", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "kimi-k2.7-code:cloud"

    captured: dict = {}

    async def fake_generate(user, assistant, *, model, cwd, env=None, pi_settings=None, timeout_s=15.0):
        captured["model"] = model
        captured["env"] = env
        return "ok"

    monkeypatch.setattr("ciao.web.project_chats._generate_chat_title", fake_generate)
    await pcm.auto_title_if_default(chat.chat_id, "hi", "hello")
    assert captured["model"] == "qwen3:8b"


async def test_auto_title_routes_anthropic_chats_through_ollama_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    """When Ollama is configured, *every* chat's title call goes through
    the cheap free-tier Ollama title model — including Anthropic chats.
    Titles are short and frequent; this keeps them off the Anthropic
    subscription rate-limit budget entirely.
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("anthropic-title", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "opus"

    captured: dict = {}

    async def fake_generate(user, assistant, *, model, cwd, env=None, pi_settings=None, timeout_s=15.0):
        captured["model"] = model
        captured["env"] = env
        return "ollama-titled"

    monkeypatch.setattr(
        "ciao.web.project_chats._generate_chat_title", fake_generate
    )

    await pcm.auto_title_if_default(chat.chat_id, "hello", "hi")
    assert captured["model"] == "ministral-3:3b"  # Ollama default title model
    assert captured["env"] is not None
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"


async def test_auto_title_falls_back_to_haiku_when_ollama_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """No Ollama allowlist = no Ollama title call. Falls back to the
    Haiku-via-subscription path so the feature still works out of the
    box on a fresh install with only Anthropic configured.
    """
    pcm = _make_manager(tmp_path, ollama_models=(), ollama_api_key="", ollama_base_url="http://localhost:11434")  # Ollama disabled
    project = pcm.create_project("anthropic-only", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "opus"

    captured: dict = {}

    async def fake_generate(user, assistant, *, model, cwd, env=None, pi_settings=None, timeout_s=15.0):
        captured["model"] = model
        captured["env"] = env
        return "haiku-titled"

    monkeypatch.setattr(
        "ciao.web.project_chats._generate_chat_title", fake_generate
    )

    await pcm.auto_title_if_default(chat.chat_id, "hello", "hi")
    assert captured["model"] == "haiku"  # config.title_model default
    assert captured["env"] in (None, {})


async def test_auto_title_works_with_user_text_only(
    tmp_path: Path, monkeypatch
) -> None:
    """The early-fire path passes assistant_text="" — verify the titler
    still runs (calling site fires right after the user echo, well
    before any assistant reply).
    """
    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("early-title", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "kimi-k2.7-code:cloud"

    captured: dict = {}

    async def fake_generate(user, assistant, *, model, cwd, env=None, pi_settings=None, timeout_s=15.0):
        captured["user"] = user
        captured["assistant"] = assistant
        return "early-titled"

    monkeypatch.setattr(
        "ciao.web.project_chats._generate_chat_title", fake_generate
    )

    new_title = await pcm.auto_title_if_default(chat.chat_id, "draft a haiku")
    assert new_title == "early-titled"
    assert captured["user"] == "draft a haiku"
    assert captured["assistant"] == ""


async def test_auto_title_uses_claude_sdk(tmp_path: Path, monkeypatch) -> None:
    """The title call uses the Claude SDK path (the only path now)."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    pcm = _make_manager(tmp_path, ollama_models=("kimi-k2.7-code:cloud",))
    project = pcm.create_project("title", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    chat.model = "kimi-k2.7-code:cloud"

    from dataclasses import dataclass as _dc

    @_dc
    class _Block:
        text: str

    @_dc
    class _AsstMsg:
        content: list

    async def fake_query(prompt, options):
        yield _AsstMsg(content=[_Block(text="SDK titled chat")])

    import ciao.providers.oneshot as _oneshot
    import claude_agent_sdk as _sdk
    monkeypatch.setattr(_oneshot, "query", fake_query, raising=True)
    monkeypatch.setattr(_oneshot, "AssistantMessage", _AsstMsg, raising=True)
    monkeypatch.setattr(_oneshot, "TextBlock", _Block, raising=True)
    monkeypatch.setattr(_sdk, "query", fake_query, raising=False)
    monkeypatch.setattr(_sdk, "AssistantMessage", _AsstMsg, raising=False)
    monkeypatch.setattr(_sdk, "TextBlock", _Block, raising=False)

    new_title = await pcm.auto_title_if_default(chat.chat_id, "user prompt")
    assert new_title == "SDK titled chat"
