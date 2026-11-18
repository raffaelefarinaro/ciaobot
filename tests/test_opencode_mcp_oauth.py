"""The OpenCode provider's MCP sign-in calls: the V2 integration API, nothing else."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from ciao.models import AgentRequest
from ciao.providers.base import ActiveHandle, BaseProvider, McpOAuthUnavailable
from tests.test_chat_actions_runtime import (
    ATTEMPT_ID,
    ATTEMPT_PATH,
    AUTH_URL,
    CONNECT_PATH,
    INTEGRATION_ID,
    INTEGRATION_PATH,
    FakeClient,
    FakeResponse,
    live_opencode,
    sign_in_routes,
)


def _mcp_rows(*rows: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"location": {"directory": "/w"}, "data": list(rows)})


@pytest.mark.asyncio
async def test_start_mcp_oauth_follows_the_v2_integration_flow(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    provider = live_opencode(tmp_path, client)

    attempt = await provider.start_mcp_oauth("linear")

    assert client.calls == [
        ("GET", "/api/mcp", None),
        ("GET", INTEGRATION_PATH, None),
        ("POST", CONNECT_PATH, {"methodID": INTEGRATION_ID}),
    ]
    assert attempt.integration_id == INTEGRATION_ID
    assert attempt.attempt_id == ATTEMPT_ID
    assert attempt.url == AUTH_URL
    assert attempt.mode == "auto"


@pytest.mark.asyncio
async def test_start_mcp_oauth_needs_a_live_server(tmp_path: Path) -> None:
    client = FakeClient(sign_in_routes())
    provider = live_opencode(tmp_path, client)
    provider._process = None

    with pytest.raises(McpOAuthUnavailable) as info:
        await provider.start_mcp_oauth("linear")

    assert info.value.reason == "not_running"
    assert client.calls == []


@pytest.mark.asyncio
async def test_start_mcp_oauth_for_an_unknown_server_has_no_integration(tmp_path: Path) -> None:
    client = FakeClient({("GET", "/api/mcp"): _mcp_rows()})
    provider = live_opencode(tmp_path, client)

    with pytest.raises(McpOAuthUnavailable) as info:
        await provider.start_mcp_oauth("linear")

    assert info.value.reason == "no_integration"


@pytest.mark.asyncio
async def test_start_mcp_oauth_on_a_404_integration_has_no_integration(tmp_path: Path) -> None:
    routes = sign_in_routes()
    routes[("GET", INTEGRATION_PATH)] = FakeResponse(404, {})
    provider = live_opencode(tmp_path, FakeClient(routes))

    with pytest.raises(McpOAuthUnavailable) as info:
        await provider.start_mcp_oauth("linear")

    assert info.value.reason == "no_integration"


@pytest.mark.asyncio
async def test_start_mcp_oauth_without_an_oauth_method(tmp_path: Path) -> None:
    routes = sign_in_routes()
    routes[("GET", INTEGRATION_PATH)] = FakeResponse(200, {
        "data": {"id": INTEGRATION_ID, "methods": [{"type": "key"}]},
    })
    provider = live_opencode(tmp_path, FakeClient(routes))

    with pytest.raises(McpOAuthUnavailable) as info:
        await provider.start_mcp_oauth("linear")

    assert info.value.reason == "no_oauth_method"


@pytest.mark.asyncio
async def test_start_mcp_oauth_rejects_a_malformed_attempt(tmp_path: Path) -> None:
    routes = sign_in_routes()
    routes[("POST", CONNECT_PATH)] = FakeResponse(200, {"data": {"attemptID": ATTEMPT_ID}})
    provider = live_opencode(tmp_path, FakeClient(routes))

    with pytest.raises(ValueError):
        await provider.start_mcp_oauth("linear")


@pytest.mark.asyncio
async def test_integration_ids_are_quoted_in_the_path(tmp_path: Path) -> None:
    client = FakeClient({
        ("GET", "/api/mcp"): _mcp_rows({
            "name": "linear",
            "status": {"status": "needs_auth"},
            "integrationID": "a/b",
        }),
    })
    provider = live_opencode(tmp_path, client)

    with pytest.raises(McpOAuthUnavailable):
        await provider.start_mcp_oauth("linear")

    assert ("GET", "/api/integration/a%2Fb", None) in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse(200, {"data": {"status": "pending"}}), ("pending", "")),
        (FakeResponse(200, {"data": {"status": "complete"}}), ("complete", "")),
        (FakeResponse(200, {"data": {"status": "expired"}}), ("expired", "")),
        (FakeResponse(200, {"data": {"status": "failed", "message": "boom"}}), ("failed", "boom")),
        (FakeResponse(404, {}), ("expired", "")),
    ],
)
async def test_read_mcp_oauth_attempt_maps_each_status(
    tmp_path: Path, response: FakeResponse, expected: tuple[str, str]
) -> None:
    client = FakeClient({("GET", ATTEMPT_PATH): response})
    provider = live_opencode(tmp_path, client)

    status = await provider.read_mcp_oauth_attempt(INTEGRATION_ID, ATTEMPT_ID)

    assert status == expected


@pytest.mark.asyncio
async def test_read_mcp_oauth_attempt_refuses_an_unknown_status(tmp_path: Path) -> None:
    client = FakeClient({("GET", ATTEMPT_PATH): FakeResponse(200, {"data": {"status": "weird"}})})
    provider = live_opencode(tmp_path, client)

    with pytest.raises(ValueError):
        await provider.read_mcp_oauth_attempt(INTEGRATION_ID, ATTEMPT_ID)


@pytest.mark.asyncio
async def test_cancel_mcp_oauth_deletes_the_attempt(tmp_path: Path) -> None:
    client = FakeClient({("DELETE", ATTEMPT_PATH): FakeResponse(204)})
    provider = live_opencode(tmp_path, client)

    await provider.cancel_mcp_oauth(INTEGRATION_ID, ATTEMPT_ID)

    assert client.calls == [("DELETE", ATTEMPT_PATH, None)]


class _NoOAuthProvider(BaseProvider):
    def run_streaming(
        self, request: Any, register_handle: Callable[[ActiveHandle | None], None]
    ) -> AsyncGenerator[Any, None]:
        raise AssertionError("defaults must not start a turn")

    async def disconnect(self) -> None:
        raise AssertionError("defaults must not disconnect")


@pytest.mark.asyncio
async def test_base_mcp_oauth_defaults(tmp_path: Path) -> None:
    provider = _NoOAuthProvider(tmp_path)

    with pytest.raises(McpOAuthUnavailable) as started:
        await provider.start_mcp_oauth("linear")
    with pytest.raises(McpOAuthUnavailable) as read:
        await provider.read_mcp_oauth_attempt(INTEGRATION_ID, ATTEMPT_ID)

    assert started.value.reason == "unsupported"
    assert read.value.reason == "unsupported"
    await provider.cancel_mcp_oauth(INTEGRATION_ID, ATTEMPT_ID)
    await provider.ensure_server(cast(AgentRequest, object()))
