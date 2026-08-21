"""The runtime-provider registry is the single enumeration of providers.

These tests guard the contract the rest of the app now relies on: every
descriptor's dotted paths resolve, and the enumerations that used to be
``{"claude", "codex"}`` literals derive from the registry.
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
    if descriptor.upgrade_path:
        assert callable(descriptor.upgrade_callable())


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
    chat picker while Settings shows no connection row and `ciao upgrade`
    silently skips it.
    """
    assert descriptor.auth_command_path, f"{descriptor.id} has no login command"
    assert descriptor.status_probe_path, f"{descriptor.id} has no readiness probe"
    assert descriptor.upgrade_path, f"{descriptor.id} has no upgrade hook"


@pytest.mark.parametrize("descriptor", provider_registry.descriptors(), ids=lambda d: d.id)
def test_default_model_settings_attr_exists_on_the_real_config(descriptor):
    """A declared default-model attribute must actually be on CiaoConfig."""
    if not descriptor.default_model_settings_attr:
        return
    from dataclasses import fields

    from ciao.config import CiaoConfig

    names = {field.name for field in fields(CiaoConfig)}
    assert descriptor.default_model_settings_attr in names
