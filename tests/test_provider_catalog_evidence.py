from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import asyncio

from ciao.providers.catalog_evidence import (
    claude_catalog_evidence,
    invalidate_claude_catalog,
    parse_claude_models,
    record_claude_models,
)


def test_claude_initialize_models_are_parsed_without_account_metadata(tmp_path: Path) -> None:
    data = {"models": [{"value": "m1", "displayName": "Model 1", "description": "private"}],
            "account": {"email": "private@example.com"}}
    assert [(m.id, m.label) for m in parse_claude_models(data) or ()] == [("m1", "Model 1")]
    record_claude_models(tmp_path, data)
    assert [(m.id, m.label) for m in claude_catalog_evidence(tmp_path).models] == [("m1", "Model 1")]


def test_malformed_model_rows_do_not_become_available(tmp_path: Path) -> None:
    info = {"models": [{"value": "ok", "displayName": "OK"}, {"value": "bad"}]}
    assert parse_claude_models(info) is None
    record_claude_models(tmp_path, info)
    assert claude_catalog_evidence(tmp_path).status == "unknown"


def test_empty_models_and_missing_models_remain_distinct() -> None:
    assert parse_claude_models({"models": []}) == ()
    assert parse_claude_models({}) is None


def test_duplicate_identifiers_preserve_runtime_order() -> None:
    assert [(m.id, m.label) for m in parse_claude_models({"models": [
        {"value": "a", "displayName": "A"}, {"value": "b", "displayName": "B"},
        {"value": "a", "displayName": "Other"},
    ]}) or ()] == [("a", "A"), ("b", "B")]


def test_control_characters_and_oversized_payloads_are_rejected() -> None:
    assert parse_claude_models({"models": [{"value": "a\nb", "displayName": "A"}]}) is None
    assert parse_claude_models({"models": [{"value": "a" * 257, "displayName": "A"}]}) is None
    assert parse_claude_models({"models": [{"value": str(i), "displayName": "A"} for i in range(257)]}) is None


def test_evidence_is_scoped_to_exact_runtime_directory(tmp_path: Path) -> None:
    invalidate_claude_catalog()
    record_claude_models(tmp_path / "one", {"models": [{"value": "m", "displayName": "M"}]})
    assert claude_catalog_evidence(tmp_path / "one").status == "observed"
    assert claude_catalog_evidence(tmp_path / "two").status == "unknown"


def test_invalid_new_observation_clears_previous_evidence(tmp_path: Path) -> None:
    record_claude_models(tmp_path, {"models": [{"value": "m", "displayName": "M"}]})
    record_claude_models(tmp_path, {})
    assert claude_catalog_evidence(tmp_path).status == "unknown"


def test_invalidation_clears_all_or_selected_root(tmp_path: Path) -> None:
    record_claude_models(tmp_path / "one", {"models": []})
    record_claude_models(tmp_path / "two", {"models": [{"value": "m", "displayName": "M"}]})
    invalidate_claude_catalog(tmp_path / "one")
    assert claude_catalog_evidence(tmp_path / "one").status == "unknown"
    assert claude_catalog_evidence(tmp_path / "two").status == "observed"
    invalidate_claude_catalog()


def test_capture_uses_existing_client_without_connect_or_query(tmp_path: Path) -> None:
    from ciao.providers.claude import ClaudeProvider

    async def run() -> None:
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.workspace_root = tmp_path
        provider._connected = True
        client = type("Client", (), {"get_server_info": AsyncMock(return_value={
            "models": [{"value": "m", "displayName": "M"}],
        })})()
        await provider._capture_catalog_evidence(client)
        client.get_server_info.assert_awaited_once()
        assert claude_catalog_evidence(tmp_path).status == "observed"

    asyncio.run(run())


def _models_request(config, query: bytes = b""):
    from types import SimpleNamespace

    from starlette.requests import Request

    return Request({
        "type": "http", "method": "GET", "path": "/api/models",
        "headers": [], "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        "path_params": {}, "query_string": query,
    })


def _route_config(tmp_path: Path):
    from ciao.config import CiaoConfig, WorkspaceConfig

    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    config.workspaces = {"work": WorkspaceConfig(name="work", vault_root="work")}
    return config


def test_models_route_reports_evidence_without_constructing_a_claude_client(tmp_path: Path, monkeypatch) -> None:
    """The route may only read what an authorized turn already observed."""
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from ciao.providers import claude as claude_module
    from ciao.providers.opencode import OpencodeProvider
    from ciao.web.routes_api import list_models

    def _explode(*_args, **_kwargs):
        raise AssertionError("the models route must not construct a Claude client")

    import claude_agent_sdk

    monkeypatch.setattr(claude_module, "ClaudeSDKClient", _explode, raising=False)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _explode, raising=False)
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)
    invalidate_claude_catalog()

    body = json.loads(asyncio.run(list_models(_models_request(config))).body)

    evidence = body["provider_catalog_evidence"]
    assert evidence["claude"] == {
        "status": "unknown", "source": None, "models": [], "reason": "not_observed",
    }
    # An empty opencode catalog is "not reported", never "signed out".
    assert evidence["opencode"]["status"] == "unknown"
    assert evidence["opencode"]["reason"] == "no_models_reported"
    # Configured aliases stay available as choices, labelled as unverified.
    from ciao.config import CLAUDE_MODELS

    assert body["provider_models"]["claude"] == list(CLAUDE_MODELS)


def test_models_route_reports_observed_evidence_and_configured_aliases(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import json
    from unittest.mock import AsyncMock

    from ciao.providers.opencode import OpencodeProvider
    from ciao.web.routes_api import list_models

    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[
        {"model": "provider/model", "label": "Provider Model"},
    ]))
    config = _route_config(tmp_path)
    root = config.agent_root("work")
    record_claude_models(root, {"models": [{"value": "claude-x", "displayName": "Claude X"}]})

    body = json.loads(asyncio.run(list_models(_models_request(config, b"workspace=work"))).body)
    evidence = body["provider_catalog_evidence"]

    assert evidence["claude"]["status"] == "observed"
    assert evidence["claude"]["models"] == [{"id": "claude-x", "label": "Claude X"}]
    assert evidence["opencode"]["status"] == "observed"
    assert evidence["opencode"]["models"] == [{"id": "provider/model", "label": "Provider Model"}]
    assert "provider/model" in body["provider_models"]["opencode"]


def test_models_route_rejects_an_unknown_workspace_name_and_ignores_paths(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from ciao.providers.opencode import OpencodeProvider
    from ciao.web.routes_api import list_models

    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)

    unknown = asyncio.run(list_models(_models_request(config, b"workspace=missing")))
    assert unknown.status_code == 400
    # A filesystem path is just an unknown workspace name, never a lookup.
    rejected = asyncio.run(list_models(_models_request(config, b"workspace=/etc")))
    assert rejected.status_code == 400
    # A valid call resolves through the config seam, never the raw query value.
    asyncio.run(list_models(_models_request(config, b"workspace=work")))
    assert OpencodeProvider.model_catalog.await_args.args[0] == config.agent_root("work")


def test_refresh_invalidates_claude_evidence_without_losing_other_roots(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from ciao.providers.opencode import OpencodeProvider
    from ciao.web.routes_api import list_models

    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    config = _route_config(tmp_path)
    root = config.agent_root("work")
    record_claude_models(root, {"models": [{"value": "m", "displayName": "M"}]})
    record_claude_models(tmp_path / "elsewhere", {"models": [{"value": "o", "displayName": "O"}]})

    asyncio.run(list_models(_models_request(config, b"workspace=work&refresh=1")))

    assert claude_catalog_evidence(root).status == "unknown"
    assert claude_catalog_evidence(tmp_path / "elsewhere").status == "observed"


def test_capture_failure_and_cancellation_keep_the_turn_behaviour(tmp_path: Path) -> None:
    """Metadata is best-effort; cancellation is not swallowed."""
    import asyncio

    import pytest

    from ciao.providers.claude import ClaudeProvider

    async def run_failure() -> None:
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.workspace_root = tmp_path
        record_claude_models(tmp_path, {"models": [{"value": "m", "displayName": "M"}]})

        async def boom():
            raise RuntimeError("controls unavailable")

        from types import SimpleNamespace

        await provider._capture_catalog_evidence(SimpleNamespace(get_server_info=boom))
        assert claude_catalog_evidence(tmp_path).status == "unknown"

    async def run_cancel() -> None:
        provider = ClaudeProvider.__new__(ClaudeProvider)
        provider.workspace_root = tmp_path

        async def cancel():
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            from types import SimpleNamespace

            await provider._capture_catalog_evidence(SimpleNamespace(get_server_info=cancel))

    asyncio.run(run_failure())
    asyncio.run(run_cancel())


def test_expired_evidence_returns_unknown_without_stale_models(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from ciao.providers import catalog_evidence as ce

    now = [0.0]
    monkeypatch.setattr(ce, "time", SimpleNamespace(monotonic=lambda: now[0]))
    record_claude_models(tmp_path, {"models": [{"value": "m", "displayName": "M"}]})
    assert claude_catalog_evidence(tmp_path).status == "observed"

    now[0] = ce._TTL_SECONDS
    expired = claude_catalog_evidence(tmp_path)
    assert expired.status == "unknown"
    assert expired.models == ()
    assert expired.reason == "expired"


def test_claude_logout_and_verify_drop_earlier_observations(tmp_path: Path, monkeypatch) -> None:
    """Account identity is shared, so a re-auth must not keep old evidence.

    The CLI and the provider probes are stubbed: this asserts the invalidation
    contract, never the operator's real authentication state.
    """
    import asyncio
    from types import SimpleNamespace

    from ciao.web import routes_api

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    commands: list[list[str]] = []

    def _fake_run(command, **_kwargs):
        commands.append(list(command))
        return _Completed()

    monkeypatch.setattr(routes_api.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        routes_api, "_provider_config_payload",
        lambda _config: {"connections": {"claude": {"ok": True}}},
    )

    for action in ("verify", "logout"):
        record_claude_models(tmp_path, {"models": [{"value": "m", "displayName": "M"}]})
        assert claude_catalog_evidence(tmp_path).status == "observed"
        request = SimpleNamespace(
            path_params={"provider": "claude", "action": action},
            app=SimpleNamespace(state=SimpleNamespace(config=_route_config(tmp_path))),
        )
        asyncio.run(routes_api.provider_connection_action(request))
        assert claude_catalog_evidence(tmp_path).status == "unknown", action

    # The bundled CLI path varies by install; the subcommand is the contract.
    assert [command[-2:] for command in commands] == [["auth", "logout"]]


@pytest.mark.asyncio
async def test_authorized_turn_captures_from_the_same_connection(tmp_path: Path, monkeypatch) -> None:
    """The capture must hang off the turn's own client, once, before query.

    Calling the helper directly does not prove the hook is wired: removing the
    call in `consume_sdk` would leave every other test green.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ciao.models import AgentRequest
    from ciao.providers import claude as claude_module
    from ciao.providers.claude import ClaudeProvider

    class FakeClient:
        connects = 0
        queries = 0

        async def connect(self, _payload=None):
            FakeClient.connects += 1

        async def query(self, _payload):
            FakeClient.queries += 1

        async def receive_response(self):
            if False:  # pragma: no cover - an empty turn
                yield None

    fake = FakeClient()
    provider = ClaudeProvider(tmp_path, config=SimpleNamespace(
        memory_char_limit=2200, user_char_limit=1375, vault_root=tmp_path / "memory-vault",
    ))
    provider._connected = False

    async def fake_ensure(_request):
        return fake

    monkeypatch.setattr(provider, "_ensure_connected", fake_ensure)
    connect_with_deadline = AsyncMock()
    monkeypatch.setattr(claude_module, "_connect_with_deadline", connect_with_deadline)
    capture = AsyncMock()
    monkeypatch.setattr(provider, "_capture_catalog_evidence", capture)

    _events = [
        event async for event in provider._run_streaming_once(
            AgentRequest(prompt="hello", model="sonnet", mode="normal"),
            lambda _handle: None,
        )
    ]

    capture.assert_awaited_once()
    assert capture.await_args.args[0] is fake, "capture must use the turn's client"
    assert capture.await_args.kwargs["generation"] is not None
    # One connect for the turn, then query; capture reuses that client.
    connect_with_deadline.assert_awaited_once_with(fake)
    assert FakeClient.queries == 1
