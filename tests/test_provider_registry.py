"""The runtime-provider registry is the single enumeration of providers.

These tests guard the contract the rest of the app now relies on: every
descriptor's dotted paths resolve, and the enumerations that used to be
``{"claude", "opencode"}`` literals derive from the registry.
"""

from __future__ import annotations

import pytest

from ciao import provider_registry
from ciao.models import THINKING_LEVELS
from ciao.provider_service import capabilities_for, supported_providers


def test_registry_is_not_empty():
    assert provider_registry.provider_ids()


@pytest.mark.parametrize("descriptor", provider_registry.descriptors(), ids=lambda d: d.id)
def test_descriptor_paths_resolve(descriptor):
    """Every dotted path names something importable and callable."""
    assert descriptor.factory() is not None
    if descriptor.system_skills_path:
        module, _, attr = descriptor.system_skills_path.partition(":")
        assert attr and module


@pytest.mark.parametrize("descriptor", provider_registry.descriptors(), ids=lambda d: d.id)
def test_descriptor_labels_are_distinct_and_present(descriptor):
    assert descriptor.label
    assert descriptor.short_label
    assert descriptor.cli_label


def test_supported_providers_matches_registry():
    assert supported_providers() == provider_registry.provider_ids()


def test_thinking_levels_derive_from_registry():
    for provider, levels in THINKING_LEVELS.items():
        assert provider_registry.require(provider).thinking_levels == levels


def test_capabilities_for_unknown_provider_is_all_false():
    """A stale chat record degrades to 'supports nothing', never a crash."""
    capabilities = capabilities_for("no-such-provider")
    assert not capabilities.resume
    assert not capabilities.background_subagents


def test_require_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown provider"):
        provider_registry.require("no-such-provider")


def test_label_falls_back_to_the_raw_id():
    assert provider_registry.label("no-such-provider") == "no-such-provider"


@pytest.mark.parametrize("descriptor", provider_registry.descriptors(), ids=lambda d: d.id)
def test_every_provider_is_installable_and_inspectable(descriptor):
    """A provider users can pick must also be one they can set up.

    Without this, a provider can be added to the registry and appear in the
    chat picker while Settings shows no connection row.
    """
    assert descriptor.auth_command_path, f"{descriptor.id} has no login command"
    assert descriptor.status_probe_path, f"{descriptor.id} has no readiness probe"


@pytest.mark.parametrize("descriptor", provider_registry.descriptors(), ids=lambda d: d.id)
def test_default_model_settings_attr_exists_on_the_real_config(descriptor):
    """A declared default-model attribute must actually be on CiaoConfig."""
    if not descriptor.default_model_settings_attr:
        return
    from dataclasses import fields

    from ciao.config import CiaoConfig

    names = {field.name for field in fields(CiaoConfig)}
    assert descriptor.default_model_settings_attr in names


def test_codex_descriptor_resolves_spec_and_factory() -> None:
    descriptor = provider_registry.require("codex")
    spec = descriptor.acp_spec()
    assert spec is not None and spec.id == "codex"
    assert descriptor.factory().spec is spec
    assert descriptor.label == "OpenAI (via Codex)"
    assert descriptor.short_label == "Codex"
    assert descriptor.thinking_levels == ("minimal", "low", "medium", "high", "xhigh")


def test_opencode_label_reads_opencode_capitalised() -> None:
    descriptor = provider_registry.require("opencode")
    assert descriptor.label == "OpenCode"
    assert descriptor.short_label == "OpenCode"
    assert descriptor.cli_label == "opencode"


def test_codex_thinking_levels_are_marked_unverified() -> None:
    from pathlib import Path

    source = Path(provider_registry.__file__).read_text(encoding="utf-8")
    codex_block = source[source.index('id="codex"') : source.index('acp_spec_path="ciao.providers.acp.agents:CODEX"')]
    assert "UNVERIFIED DEFAULT" in codex_block
    assert provider_registry.require("codex").thinking_levels == (
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    )
