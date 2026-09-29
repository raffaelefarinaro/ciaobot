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
    parse_tailscale_serve,
    server_addresses,
)
from ciao.web import auth
from ciao.web.routes_api import addresses_endpoint

_URLS = [
    "http://localhost:9443/",
    "http://mac.local:9443/",
    "http://192.168.1.20:9443/",
]


def _client() -> TestClient:
    app = Starlette(routes=[Route("/api/addresses", addresses_endpoint)])
    app.state.config = SimpleNamespace(pwa_port=9443)
    return TestClient(app)


def _patch_addresses(monkeypatch, tailscale: list[str] | None = None) -> None:
    # The handler imports inside the function, so the patch has to land on the
    # module attribute rather than on a name the module already bound.
    monkeypatch.setattr(
        "ciao.network_addresses.server_addresses", lambda port: list(_URLS)
    )
    # Never shell out to a real `tailscale` from the suite.
    monkeypatch.setattr(
        "ciao.network_addresses.tailscale_serve_urls", lambda port: list(tailscale or [])
    )


def test_addresses_list_tailscale_first_then_lan_then_loopback(monkeypatch) -> None:
    _patch_addresses(monkeypatch, tailscale=["https://mini.ts.net/"])
    body = _client().get("/api/addresses").json()

    assert [entry["kind"] for entry in body["addresses"]] == [
        "trusted",
        "lan",
        "lan",
        "loopback",
    ]
    assert body["addresses"][0]["url"] == "https://mini.ts.net/"
    assert body["addresses"][0]["secure"] is True
    assert body["port"] == 9443


def test_addresses_without_tailscale_have_no_trusted_entry(monkeypatch) -> None:
    _patch_addresses(monkeypatch)
    body = _client().get("/api/addresses").json()

    assert "trusted" not in [entry["kind"] for entry in body["addresses"]]


def test_addresses_never_include_credentials(monkeypatch) -> None:
    _patch_addresses(monkeypatch)
    body = _client().get("/api/addresses").json()

    for entry in body["addresses"]:
        url = entry["url"]
        assert "@" not in url
        assert "?" not in url
        assert "token" not in url


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


def test_addresses_include_tailscale_serve_origin(monkeypatch) -> None:
    _patch_addresses(monkeypatch, tailscale=["https://mini.tail1.ts.net/"])
    body = _client().get("/api/addresses").json()

    first = body["addresses"][0]
    assert first == {
        "url": "https://mini.tail1.ts.net/",
        "kind": "trusted",
        "source": "tailscale",
        "secure": True,
        "loopback": False,
    }


def test_parse_tailscale_serve_keeps_only_root_proxies_to_our_port() -> None:
    # Shape of `tailscale serve status --json` (1.98): Web keys are host:port.
    status = {
        "TCP": {"443": {"HTTPS": True}, "8444": {"HTTPS": True}},
        "Web": {
            "Mini.tail1.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:8443"}}},
            "mini.tail1.ts.net:8444": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8443"}}},
            "mini.tail1.ts.net:8445": {"Handlers": {"/": {"Proxy": "http://[::1]:8443"}}},
            # Another app on the same machine.
            "mini.tail1.ts.net:8446": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:4599"}}},
            # Mounted under a path: the PWA's absolute URLs would not load.
            "mini.tail1.ts.net:8447": {"Handlers": {"/ciao": {"Proxy": "http://127.0.0.1:8443"}}},
            # Proxying to another machine is not this engine.
            "mini.tail1.ts.net:8448": {"Handlers": {"/": {"Proxy": "http://10.0.0.5:8443"}}},
            # Static files, not a proxy.
            "mini.tail1.ts.net:8449": {"Handlers": {"/": {"Path": "/tmp/site"}}},
        },
    }
    assert parse_tailscale_serve(status, 8443) == [
        "https://mini.tail1.ts.net/",
        "https://mini.tail1.ts.net:8444/",
        "https://mini.tail1.ts.net:8445/",
    ]


def test_parse_tailscale_serve_ignores_malformed_status() -> None:
    assert parse_tailscale_serve(None, 8443) == []
    assert parse_tailscale_serve({}, 8443) == []
    assert parse_tailscale_serve({"Web": []}, 8443) == []
    assert parse_tailscale_serve({"Web": {"x:443": {"Handlers": "no"}}}, 8443) == []
    # A host the trusted-URL gate would refuse never reaches the QR code.
    bad = {"Web": {"ho st:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8443"}}}}}
    assert parse_tailscale_serve(bad, 8443) == []


def test_tailscale_serve_urls_survives_a_failing_cli(monkeypatch) -> None:
    import subprocess

    from ciao import network_addresses

    monkeypatch.setattr(network_addresses, "_tailscale_cli", lambda: "/bin/tailscale")

    def run(returncode: int = 0, stdout: str = "", exc: Exception | None = None):
        def fake(*args, **kwargs):
            if exc:
                raise exc
            return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

        return fake

    for fake in (
        run(exc=subprocess.TimeoutExpired("tailscale", 3)),
        run(exc=OSError("gone")),
        run(returncode=1),
        run(stdout="not json"),
    ):
        monkeypatch.setattr(network_addresses.subprocess, "run", fake)
        assert network_addresses.tailscale_serve_urls(8443) == []

    ok = '{"Web": {"mini.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8443"}}}}}'
    monkeypatch.setattr(network_addresses.subprocess, "run", run(stdout=ok))
    assert network_addresses.tailscale_serve_urls(8443) == ["https://mini.ts.net/"]

    monkeypatch.setattr(network_addresses, "_tailscale_cli", lambda: None)
    assert network_addresses.tailscale_serve_urls(8443) == []
