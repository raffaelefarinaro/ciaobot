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
from ciao.config import CLAUDE_MODELS
from ciao.providers.acp import provider as provider_module
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.spec import AgentSpec
from ciao.providers.acp.agents import OpencodeAcpProvider
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


@pytest.mark.asyncio
async def test_catalog_probe_that_never_answers_session_new_is_bounded(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS, new_hang=True)
    monkeypatch.setattr(provider_module, "_CATALOG_PROBE_TIMEOUT_S", 5.0)
    workspace = _workspace(tmp_path)
    caplog.set_level(logging.WARNING)

    # The bound must outlast a slow CI runner reaching session/new (a 0.5 s bound timed out in initialize there). The outer guard turns a missing bound into a failed test instead of a hung run.
    first = await asyncio.wait_for(FakeCatalogProvider.model_catalog(workspace), 30)
    assert first == []
    assert "timed out" in caplog.text
    # The per-key lock is released and a timed-out probe is not cached: a second call probes again.
    second = await asyncio.wait_for(FakeCatalogProvider.model_catalog(workspace), 30)
    assert second == []
    assert _session_news(calls) == 2


@pytest.mark.asyncio
async def test_catalog_keeps_rows_when_closing_the_probe_session_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, config_options=_OPTIONS)

    async def broken_close(self) -> None:
        raise RuntimeError("adapter close text")

    monkeypatch.setattr(AcpSession, "close", broken_close)

    assert await FakeCatalogProvider.model_catalog(_workspace(tmp_path), force=True) == _ROWS


@pytest.mark.asyncio
async def test_catalog_probes_again_once_the_ttl_has_passed(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS)
    workspace = _workspace(tmp_path)
    clock = {"now": 1000.0}
    monkeypatch.setattr(provider_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    await FakeCatalogProvider.model_catalog(workspace)
    clock["now"] += provider_module._CATALOG_TTL_S - 1
    await FakeCatalogProvider.model_catalog(workspace)
    assert _session_news(calls) == 1  # still inside the TTL: served from the cache

    clock["now"] += 2  # now past the TTL since the first probe
    assert await FakeCatalogProvider.model_catalog(workspace) == _ROWS
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
    monkeypatch.setattr(OpencodeAcpProvider, "model_catalog", AsyncMock(return_value=[]))
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


def test_list_models_takes_the_opencode_catalog_from_the_acp_probe_only(
    monkeypatch, tmp_path: Path
) -> None:
    """The HTTP opencode catalog is no longer read: /api/models asks the ACP provider."""
    from ciao.providers.acp.agents import OpencodeAcpProvider

    http_catalog = AsyncMock(side_effect=AssertionError("the HTTP opencode catalog must not run"))
    acp_catalog = AsyncMock(return_value=[{"model": "oc/a", "label": "OC A", "variants": []}])
    monkeypatch.setattr(OpencodeAcpProvider, "model_catalog", http_catalog)
    monkeypatch.setattr(OpencodeAcpProvider, "model_catalog", acp_catalog)
    config = _route_config(tmp_path)

    body = json.loads(asyncio.run(list_models(_models_request(config))).body)

    assert body["provider_models"]["opencode"] == ["oc/a"]
    assert body["backends"]["opencode"] is True
    http_catalog.assert_not_awaited()
    acp_catalog.assert_awaited()


def test_list_models_keeps_an_acp_provider_empty_when_its_probe_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, config_options=_OPTIONS, new_error="boom")
    monkeypatch.setattr(OpencodeAcpProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)

    body = json.loads(asyncio.run(list_models(_models_request(config))).body)

    assert body["provider_models"]["fake"] == []
    assert body["provider_catalog_evidence"]["fake"]["status"] == "unknown"
    assert "opencode" in body["provider_models"]  # the route still answers for the others


_DELETE_ADVERTISED = {"sessionCapabilities": {"delete": {}, "close": {}}}


@pytest.mark.asyncio
async def test_catalog_probe_session_is_deleted_when_the_agent_advertises_delete(
    monkeypatch, tmp_path: Path
) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS, initialize=_DELETE_ADVERTISED)

    assert await FakeCatalogProvider.model_catalog(_workspace(tmp_path), force=True) == _ROWS
    new_id = _session_ids(calls, "session/new")[0]
    # The probe leaves nothing in the user's CLI history: deleted, not merely closed.
    assert _session_ids(calls, "session/delete") == [new_id]
    assert _session_ids(calls, "session/close") == []


@pytest.mark.asyncio
async def test_catalog_probe_session_is_closed_when_delete_is_not_advertised(
    monkeypatch, tmp_path: Path
) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS, initialize={"sessionCapabilities": {"close": {}}})

    assert await FakeCatalogProvider.model_catalog(_workspace(tmp_path), force=True) == _ROWS
    assert _session_ids(calls, "session/delete") == []
    assert _session_ids(calls, "session/close") == _session_ids(calls, "session/new")


@pytest.mark.asyncio
async def test_catalog_keeps_rows_when_the_probe_delete_fails_and_logs_no_adapter_text(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    _install(
        monkeypatch, tmp_path, config_options=_OPTIONS, initialize=_DELETE_ADVERTISED,
        delete_error="secret-token /private/path boom",
    )
    caplog.set_level(logging.WARNING)

    assert await FakeCatalogProvider.model_catalog(_workspace(tmp_path), force=True) == _ROWS
    assert "secret-token" not in caplog.text and "/private/path" not in caplog.text


@pytest.mark.asyncio
async def test_verify_invalidates_the_acp_catalog_cache(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, config_options=_OPTIONS)
    workspace = _workspace(tmp_path)
    await FakeCatalogProvider.model_catalog(workspace)
    assert _session_news(calls) == 1

    provider_module.invalidate_acp_catalog(FakeCatalogProvider.spec.id)
    await FakeCatalogProvider.model_catalog(workspace)
    assert _session_news(calls) == 2


def _session_ids(calls: Path, method: str) -> list[str]:
    return [call["params"]["sessionId"] for call in read_calls(calls) if call["method"] == method]


@pytest.mark.asyncio
async def test_verify_drops_the_cached_acp_catalog_so_the_next_read_probes(
    monkeypatch, tmp_path: Path
) -> None:
    """The opencode verify action clears the ACP catalog cache, not only the HTTP one."""
    from starlette.requests import Request

    from ciao.web import routes_api

    workspace = _workspace(tmp_path)
    key = ("opencode", str(workspace))
    provider_module._catalog_cache[key] = (provider_module.time.monotonic(), list(_ROWS))
    monkeypatch.setattr(
        routes_api, "_provider_config_payload_with_inventory",
        lambda _config: {"connections": {"opencode": {}}},
    )
    request = Request({
        "type": "http", "method": "POST",
        "path": "/api/settings/providers/opencode/verify",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(workspace_root=workspace))),
        "path_params": {"provider": "opencode", "action": "verify"},
        "query_string": b"",
    })

    response = await routes_api.provider_connection_action(request)

    assert response.status_code == 200
    assert key not in provider_module._catalog_cache
