"""The ACP model catalog: probed from the agent's config options, cached, and served on /api/models (#3137)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from ciao import provider_registry
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.provider_registry import ProviderDescriptor
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.spec import AgentSpec
from ciao.providers.opencode import OpencodeProvider
from ciao.web.routes_api import list_models
from tests.acp_helpers import read_calls, write_scenario

_OPTIONS = [
    {
        "id": "model", "name": "Model", "category": "model", "type": "select",
        "currentValue": "m-a",
        "options": [{"value": "m-a", "name": "Model A"}, {"value": "m-b", "name": "Model B"}],
    },
    {
        "id": "thought", "name": "Thinking", "category": "thought_level", "type": "select",
        "currentValue": "low",
        "options": [{"value": "low", "name": "Low"}, {"value": "high", "name": "High"}],
    },
]
_ROWS = [
    {"model": "m-a", "label": "Model A", "variants": ["low", "high"]},
    {"model": "m-b", "label": "Model B", "variants": ["low", "high"]},
]


SPEC: AgentSpec | None = None  # target of the fake descriptor's acp_spec_path


class FakeCatalogProvider(AcpProvider):
    spec: ClassVar[AgentSpec]


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **scenario: Any) -> Path:
    """Register the fake agent as provider ``fake`` with ``scenario``; return the calls log."""
    spec, calls = write_scenario(tmp_path, spec_thought_config_id="thought", **scenario)
    descriptor = ProviderDescriptor(
        id="fake",
        label="Fake agent",
        short_label="Fake",
        cli_label="Fake",
        factory_path="tests.test_acp_model_catalog:FakeCatalogProvider",
        acp_spec_path="tests.test_acp_model_catalog:SPEC",
    )
    monkeypatch.setattr(sys.modules[__name__], "SPEC", spec)
    monkeypatch.setattr(FakeCatalogProvider, "spec", spec, raising=False)
    # Added to the real registry: the models route still reads the opencode descriptor.
    monkeypatch.setattr(
        provider_registry, "_DESCRIPTORS", (*provider_registry.descriptors(), descriptor)
    )
    monkeypatch.setattr(
        provider_registry, "_BY_ID", {**provider_registry._BY_ID, "fake": descriptor}
    )
    return calls


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return workspace


def _session_news(calls: Path) -> int:
    return sum(1 for call in read_calls(calls) if call["method"] == "session/new")


@pytest.mark.asyncio
async def test_catalog_reads_model_and_thought_options(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS)

    rows = await FakeCatalogProvider.model_catalog(_workspace(tmp_path), force=True)

    assert rows == _ROWS
    new = next(call for call in read_calls(calls) if call["method"] == "session/new")
    assert new["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_catalog_cached_until_force(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS)
    workspace = _workspace(tmp_path)

    first = await FakeCatalogProvider.model_catalog(workspace)
    second = await FakeCatalogProvider.model_catalog(workspace)
    assert first == second == _ROWS
    assert _session_news(calls) == 1  # the second call is served from the cache

    await FakeCatalogProvider.model_catalog(workspace, force=True)
    assert _session_news(calls) == 2


@pytest.mark.asyncio
async def test_catalog_failure_yields_empty_list(monkeypatch, tmp_path: Path, caplog) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS, new_error="secret-token boom")
    workspace = _workspace(tmp_path)
    caplog.set_level(logging.WARNING)

    assert await FakeCatalogProvider.model_catalog(workspace) == []
    # The adapter's error text never reaches the log, and a failure is not cached.
    assert "secret-token" not in caplog.text
    assert await FakeCatalogProvider.model_catalog(workspace) == []
    assert _session_news(calls) == 2


def _route_config(tmp_path: Path) -> CiaoConfig:
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    config.workspaces = {"work": WorkspaceConfig(name="work", vault_root="work")}
    return config


def _models_request(config: CiaoConfig):
    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": "/api/models",
        "headers": [], "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        "path_params": {}, "query_string": b"",
    })


def test_list_models_includes_acp_provider_catalog(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, config_options=_OPTIONS)
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)

    body = json.loads(asyncio.run(list_models(_models_request(config))).body)

    assert body["provider_models"]["fake"] == ["m-a", "m-b"]
    assert body["provider_defaults"]["fake"] == "m-a"
    assert body["provider_catalog_evidence"]["fake"] == {
        "status": "observed",
        "source": "acp_config_options",
        "models": [{"id": "m-a", "label": "Model A"}, {"id": "m-b", "label": "Model B"}],
        "reason": None,
    }
    assert body["model_reasoning_levels"]["m-a"] == ["low", "high"]
    assert {item["id"] for item in body["providers"]} >= {"fake"}


def test_list_models_keeps_an_acp_provider_empty_when_its_probe_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, config_options=_OPTIONS, new_error="boom")
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)

    body = json.loads(asyncio.run(list_models(_models_request(config))).body)

    assert body["provider_models"]["fake"] == []
    assert body["provider_catalog_evidence"]["fake"]["status"] == "unknown"
    assert "opencode" in body["provider_models"]  # the route still answers for the others
