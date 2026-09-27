"""The Other devices card's data source: where a second device can open Ciaobot.

The trusted HTTPS origin is what a phone can actually install and get
notifications from; the raw LAN HTTP URLs still work in a browser, so they are
reported but labelled. The endpoint enumerates LAN interfaces, so it sits
behind a session, and no URL may carry a password or setup token.

The `ciao.network_addresses` helpers the card and the trusted-URL setter are
built on are covered here too, so the module's behaviour is pinned whether or
not it arrives through the route.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.network_addresses import (
    is_loopback_url,
    normalize_trusted_url,
    parse_inet_addresses,
    server_addresses,
)
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


def test_loopback_detection_covers_localhost_and_127() -> None:
    assert is_loopback_url("http://localhost:8443/")
    assert is_loopback_url("http://127.0.0.1:8443/")
    assert not is_loopback_url("http://mac.local:8443/")
    assert not is_loopback_url("http://192.168.1.20:8443/")
    # A Tailscale address is shareable, not loopback.
    assert not is_loopback_url("http://100.94.1.5:8443/")


def test_address_discovery_keeps_order_and_drops_loopback_interfaces() -> None:
    ifconfig = """
lo0: flags=8049
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863
\tinet 192.168.1.20 netmask 0xffffff00
utun3: flags=8051
\tinet 100.94.1.5 --> 100.94.1.5 netmask 0xffffffff
en1: flags=8863
\tinet 192.168.1.20 netmask 0xffffff00
"""
    assert parse_inet_addresses(ifconfig) == ["192.168.1.20", "100.94.1.5"]

    urls = server_addresses(8443, ifconfig_text=ifconfig, local_hostname="mac")
    assert urls == [
        "http://localhost:8443/",
        "http://mac.local:8443/",
        "http://192.168.1.20:8443/",
        "http://100.94.1.5:8443/",
    ]


def test_address_discovery_without_a_bonjour_name() -> None:
    urls = server_addresses(8443, ifconfig_text="", local_hostname="")
    assert urls == ["http://localhost:8443/"]


def test_normalize_trusted_url_accepts_https_origins() -> None:
    # Canonical stored form: lowercase host, one trailing slash, port kept.
    assert normalize_trusted_url("https://Mini.Tailnet.ts.net") == "https://mini.tailnet.ts.net/"
    assert normalize_trusted_url("https://host:8443/") == "https://host:8443/"
    # An IPv6 literal keeps its brackets, or the result is not a valid URL.
    assert normalize_trusted_url("https://[FD7A::1]:8443") == "https://[fd7a::1]:8443/"
    # Empty (or whitespace) clears the setting.
    assert normalize_trusted_url(" ") == ""


def test_normalize_trusted_url_rejects_unsafe_values() -> None:
    # Anything a copied URL could smuggle past a QR code is refused rather
    # than stored: a scheme that is not HTTPS, no host, credentials, or a
    # path/query/fragment that could carry a token. A space, a backslash, an
    # angle bracket, a comma and a non-numeric port all slip past urlsplit, so
    # they are refused explicitly too. "https://host\evil.com" is the dangerous
    # one: a browser reads "\" as "/", so the code would open host/evil.com.
    for bad in (
        "http://host",
        "https://",
        "https://u:p@host",
        "https://host/path",
        "https://host/?token=x",
        "https://host/#x",
        "https://ho st",
        "https://host\\evil.com",
        "https://exa<mple>",
        "https://a,b",
        "https://host:abc",
        "ftp://host",
    ):
        with pytest.raises(ValueError):
            normalize_trusted_url(bad)
