"""The PWA lists the URLs the engine is reachable at, for sharing with a phone.

The native app dropped the tray's address submenu, so this endpoint is the only
place those addresses surface. It enumerates LAN interfaces, so unlike the
tray-facing endpoints it must sit behind a session rather than be
loopback-public.
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
from ciao.web.routes_node import node_addresses_endpoint


def _client(port: int | None = 8443) -> TestClient:
    app = Starlette(routes=[Route("/api/node/addresses", node_addresses_endpoint)])
    app.state.config = SimpleNamespace(pwa_port=port)
    return TestClient(app)


def test_addresses_are_returned_with_loopback_flagged(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.web.routes_node.server_addresses",
        lambda port: [
            f"http://localhost:{port}/",
            f"http://mac.local:{port}/",
            f"http://192.168.1.20:{port}/",
        ],
    )
    body = _client(9443).get("/api/node/addresses").json()

    assert body["port"] == 9443
    assert [entry["url"] for entry in body["addresses"]] == [
        "http://localhost:9443/",
        "http://mac.local:9443/",
        "http://192.168.1.20:9443/",
    ]
    # A phone scanning the loopback URL would hit its own device, so the PWA
    # needs to be able to mark it.
    assert [entry["loopback"] for entry in body["addresses"]] == [True, False, False]


def test_missing_port_config_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setattr(
        "ciao.web.routes_node.server_addresses", lambda port: [f"http://localhost:{port}/"]
    )
    body = _client(None).get("/api/node/addresses").json()
    assert body["port"] == 8443


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
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863
	inet 192.168.1.20 netmask 0xffffff00
utun3: flags=8051
	inet 100.94.1.5 --> 100.94.1.5 netmask 0xffffffff
en1: flags=8863
	inet 192.168.1.20 netmask 0xffffff00
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
    # Empty (or whitespace) clears the setting.
    assert normalize_trusted_url(" ") == ""


def test_normalize_trusted_url_rejects_unsafe_values() -> None:
    # Anything a copied URL could smuggle past a QR code is refused rather
    # than stored: a scheme that is not HTTPS, no host, credentials, or a
    # path/query/fragment that could carry a token.
    for bad in (
        "http://host",
        "https://",
        "https://u:p@host",
        "https://host/path",
        "https://host/?token=x",
        "https://host/#x",
        "ftp://host",
    ):
        with pytest.raises(ValueError):
            normalize_trusted_url(bad)
