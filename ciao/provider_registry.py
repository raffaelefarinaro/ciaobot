"""Descriptors for the runtime agent providers Ciaobot can run.

A *runtime provider* is a CLI or SDK that executes a whole agentic turn:
``claude``, ``codex``, and ``opencode`` today. Ollama, OpenRouter, and
user-defined custom endpoints are not providers in this sense — they are
model-routing backends that run *through* one of these runners via environment
injection (see ``ciao/providers/routing.py`` and ``ciao/custom_providers.py``).

This module is the single enumeration of that set. It deliberately holds only
data plus dotted import paths, and imports nothing from the rest of the app, so
that ``config``, ``cli``, ``setup_status``, ``upgrade``, and the web layer can
all read it without an import cycle and without pulling a provider SDK into
processes that never start a turn.

``ciao/provider_service.py`` resolves the factories and owns the live provider
instance; everything that merely needs to *enumerate* or *label* providers
should read from here instead of repeating a ``{"claude", "codex"}`` literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.providers.base import BaseProvider


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """Static facts about one runtime provider."""

    id: str
    # Long form for settings rows and workspace pickers, naming the vendor:
    # "Anthropic (via Claude Code)".
    label: str
    # Compact form for chat headers and badges: "Claude".
    short_label: str
    # The CLI product's own name, for text about what the tool loaded:
    # "Claude Code".
    cli_label: str
    # ``module:attr`` for the provider class. Resolved on first use so that
    # importing this registry never imports an agent SDK.
    factory_path: str
    # ``module:attr`` for ``auth_command(*, device_auth: bool) -> list[str]``.
    auth_command_path: str = ""
    # ``module:attr`` for ``() -> Iterable[str]`` listing the skills the
    # provider's own CLI loads (its bundled/system skills, not Ciaobot's).
    system_skills_path: str = ""
    # ``module:attr`` for the install/auth status probe. Signature is
    # ``(env, *, config, credentials_path, config_path, workspace_root,
    # **unused) -> dict``; see ``ciao/setup_status.py``. Empty means the
    # provider contributes no Settings connection row.
    status_probe_path: str = ""
    # ``module:attr`` for ``async () -> UpgradeResult``. Empty means the
    # provider's CLI is not upgraded by ``ciao upgrade``.
    upgrade_path: str = ""
    # Name of the ``CiaoConfig`` attribute holding this provider's per-tier
    # model pins (a dataclass with ``{haiku,sonnet,opus,fable}_model`` fields).
    # Empty means the provider has no operator-settable tier pins.
    tier_settings_attr: str = ""
    # Native thinking/reasoning levels, surfaced as-is in the PWA model picker.
    # Empty tuple = the provider has no level control.
    thinking_levels: tuple[str, ...] = ()
    # Default model for a workspace that pins this provider without naming a
    # model. Empty means "let the provider's own account catalog decide", and
    # the chat records the effective model once the provider resolves it.
    default_model: str = ""
    # Name of the ``CiaoConfig`` attribute holding the operator-configured
    # default model for this provider, when it has one. Takes precedence over
    # ``default_model``; empty means the provider has no such setting.
    default_model_config_key: str = ""
    # Model-routing bucket for such a workspace. Empty means "no bucket": the
    # provider serves its own models and the Anthropic/Ollama/OpenRouter
    # bucket vocabulary does not apply.
    model_bucket: str = ""
    # Whether a user-defined OpenAI/Anthropic-compatible endpoint can name this
    # provider as its runner (see ``ciao/custom_providers.py``).
    custom_runner: bool = False

    def factory(self) -> type["BaseProvider"]:
        """Import and return the provider class."""
        module_name, _, attr = self.factory_path.partition(":")
        return getattr(import_module(module_name), attr)  # type: ignore[no-any-return]

    def auth_command(self, *, device_auth: bool = False) -> list[str]:
        """Build the interactive login command for this provider.

        Raises ``FileNotFoundError`` when the provider's binary is missing, and
        ``NotImplementedError`` when the provider has no interactive login.
        """
        if not self.auth_command_path:
            raise NotImplementedError(f"provider '{self.id}' has no login command")
        module_name, _, attr = self.auth_command_path.partition(":")
        builder = getattr(import_module(module_name), attr)
        return list(builder(device_auth=device_auth))

    def system_skills(self) -> list[str]:
        """Skills the provider's own CLI loads. Empty when it exposes none."""
        if not self.system_skills_path:
            return []
        module_name, _, attr = self.system_skills_path.partition(":")
        return list(getattr(import_module(module_name), attr)())

    def status_probe(self, env: object, **context: object) -> dict:
        """Run this provider's install/auth probe.

        Resolved through ``getattr`` on the module rather than a direct import
        so tests that monkeypatch the probe by name still take effect.
        """
        module_name, _, attr = self.status_probe_path.partition(":")
        probe = getattr(import_module(module_name), attr)
        return dict(probe(env, **context))

    def upgrade_callable(self):
        """Return this provider's CLI upgrade coroutine function."""
        module_name, _, attr = self.upgrade_path.partition(":")
        return getattr(import_module(module_name), attr)


_DESCRIPTORS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        id="claude",
        label="Anthropic (via Claude Code)",
        short_label="Claude",
        cli_label="Claude Code",
        factory_path="ciao.providers.claude:ClaudeProvider",
        auth_command_path="ciao.providers.claude:auth_command",
        system_skills_path="ciao.setup_status:discover_claude_system_skills",
        status_probe_path="ciao.setup_status:claude_status_probe",
        upgrade_path="ciao.upgrade:upgrade_claude_code",
        thinking_levels=("low", "medium", "high", "xhigh", "max"),
        default_model_config_key="claude_default_model",
        model_bucket="work",
        custom_runner=True,
    ),
    ProviderDescriptor(
        id="codex",
        label="OpenAI (via Codex)",
        short_label="Codex",
        cli_label="OpenAI Codex",
        factory_path="ciao.providers.codex:CodexProvider",
        auth_command_path="ciao.providers.codex:auth_command",
        system_skills_path="ciao.providers.codex:codex_system_skills",
        status_probe_path="ciao.setup_status:codex_status_probe",
        upgrade_path="ciao.upgrade:upgrade_codex",
        tier_settings_attr="codex",
        # The model catalog is authoritative and the API narrows this per
        # model. This union is the validation fallback when discovery is
        # unavailable.
        thinking_levels=("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
        custom_runner=True,
    ),
    ProviderDescriptor(
        id="opencode",
        label="opencode",
        short_label="opencode",
        cli_label="opencode",
        factory_path="ciao.providers.opencode:OpencodeProvider",
        auth_command_path="ciao.providers.opencode:auth_command",
        system_skills_path="ciao.providers.opencode:opencode_system_skills",
        status_probe_path="ciao.setup_status:opencode_status_probe",
        upgrade_path="ciao.upgrade:upgrade_opencode",
        tier_settings_attr="opencode",
        # opencode calls reasoning effort a model `variant`. The catalog is
        # authoritative and narrows this per model; the union is the validation
        # fallback when discovery is unavailable.
        thinking_levels=("low", "medium", "high", "max"),
        # Models come from the signed-in accounts' catalog; an empty default
        # lets opencode pick, and the chat records what it resolved.
        custom_runner=False,
    ),
)

_BY_ID: dict[str, ProviderDescriptor] = {item.id: item for item in _DESCRIPTORS}


def descriptors() -> tuple[ProviderDescriptor, ...]:
    """Every runtime provider, in stable presentation order."""
    return _DESCRIPTORS


def provider_ids() -> tuple[str, ...]:
    """Provider ids accepted by chats, schedules, and the CLI."""
    return tuple(_BY_ID)


def get(provider: str) -> ProviderDescriptor | None:
    """Look up a descriptor, or ``None`` for an unknown id."""
    return _BY_ID.get(provider)


def is_provider(provider: str) -> bool:
    """Whether ``provider`` names a runtime provider."""
    return provider in _BY_ID


def require(provider: str) -> ProviderDescriptor:
    """Look up a descriptor, raising ``ValueError`` for an unknown id."""
    descriptor = _BY_ID.get(provider)
    if descriptor is None:
        raise ValueError(f"Unknown provider '{provider}'")
    return descriptor


def label(provider: str, *, short: bool = False) -> str:
    """Human-facing name for a provider, falling back to the raw id."""
    descriptor = _BY_ID.get(provider)
    if descriptor is None:
        return provider
    return descriptor.short_label if short else descriptor.label


def thinking_levels() -> dict[str, tuple[str, ...]]:
    """Native thinking/reasoning levels keyed by provider id."""
    return {item.id: item.thinking_levels for item in _DESCRIPTORS if item.thinking_levels}


def custom_runner_ids() -> tuple[str, ...]:
    """Providers a custom OpenAI/Anthropic-compatible endpoint may run through."""
    return tuple(item.id for item in _DESCRIPTORS if item.custom_runner)
