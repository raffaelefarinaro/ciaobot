"""The Other devices card's data source: where a second device can open Ciaobot.

The trusted HTTPS origin is what a phone can actually install and get
notifications from; the raw LAN HTTP URLs still work in a browser, so they are
reported but labelled. The endpoint enumerates LAN interfaces, so it sits
behind a session, and no URL may carry a password or setup token.
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web import auth
from ciao.web.routes_api import addresses_endpoint

_URLS = [
    "http://localhost:9443/",
    "http://mac.local:9443/",
    "http://192.168.1.20:9443/",
]


def _client(trusted_url: str) -> TestClient:
    app = Starlette(routes=[Route("/api/addresses", addresses_endpoint)])
    app.state.config = SimpleNamespace(pwa_port=9443)
    app.state.app_settings = SimpleNamespace(
        settings=SimpleNamespace(trusted_url=trusted_url)
    )
    return TestClient(app)


def _patch_addresses(monkeypatch) -> None:
    # The handler imports inside the function, so the patch has to land on the
    # module attribute rather than on a name the module already bound.
    monkeypatch.setattr(
        "ciao.network_addresses.server_addresses", lambda port: list(_URLS)
    )


def test_addresses_list_trusted_first_then_lan_then_loopback(monkeypatch) -> None:
    _patch_addresses(monkeypatch)
    body = _client("https://mini.ts.net/").get("/api/addresses").json()

    assert [entry["kind"] for entry in body["addresses"]] == [
        "trusted",
        "lan",
        "lan",
        "loopback",
    ]
    assert body["addresses"][0]["url"] == "https://mini.ts.net/"
    assert body["addresses"][0]["secure"] is True
    assert body["trusted_url"] == "https://mini.ts.net/"
    assert body["port"] == 9443


def test_addresses_without_trusted_url(monkeypatch) -> None:
    _patch_addresses(monkeypatch)
    body = _client("").get("/api/addresses").json()

    assert "trusted" not in [entry["kind"] for entry in body["addresses"]]
    assert body["trusted_url"] is None


def test_addresses_never_include_credentials(monkeypatch) -> None:
    _patch_addresses(monkeypatch)
    body = _client("https://mini.ts.net/").get("/api/addresses").json()

    for entry in body["addresses"]:
        url = entry["url"]
        assert "@" not in url
        assert "?" not in url
        assert "token" not in url


def test_addresses_drop_invalid_stored_trusted_url(monkeypatch) -> None:
    # app_settings.json can be hand-edited, so a stored value the setter would
    # have refused must not be handed to the card: a query token here would end
    # up printed as the "Full app" address and encoded into the QR code.
    _patch_addresses(monkeypatch)
    body = _client("https://host/?token=x").get("/api/addresses").json()

    assert "trusted" not in [entry["kind"] for entry in body["addresses"]]
    assert body["trusted_url"] is None


def test_addresses_route_is_session_protected() -> None:
    # It enumerates LAN interfaces, so it must not sit in the loopback-public
    # allowlist next to /api/auth.
    assert "/api/addresses" not in auth._PUBLIC_API
