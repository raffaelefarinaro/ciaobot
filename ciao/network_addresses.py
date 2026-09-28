"""URLs the PWA is reachable at on the local network.

Extracted from ``ciao.menubar`` so the PWA can list them too: the native app
dropped the tray's address submenu, and typing a LAN address (or scanning it)
from a phone is the only way to reach a host that isn't on localhost. The
legacy menu bar still imports these, so there is one implementation, not two.
"""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlsplit

_INET_RE = re.compile(r"^\s*inet (\d+\.\d+\.\d+\.\d+)", re.MULTILINE)


def parse_inet_addresses(ifconfig_text: str) -> list[str]:
    """IPv4 addresses from `ifconfig` output, loopback excluded, order kept."""

    seen: list[str] = []
    for address in _INET_RE.findall(ifconfig_text):
        if address.startswith("127.") or address in seen:
            continue
        seen.append(address)
    return seen


def server_addresses(
    port: int,
    *,
    ifconfig_text: str | None = None,
    local_hostname: str | None = None,
) -> list[str]:
    """URLs the PWA is reachable at: localhost, Bonjour name, LAN IPv4s.

    The server binds 0.0.0.0 (see CiaoConfig.pwa_host), so every interface
    address genuinely serves the app.
    """

    if ifconfig_text is None:
        try:
            ifconfig_text = subprocess.run(
                ["ifconfig", "-a"], capture_output=True, text=True, check=False
            ).stdout
        except OSError:
            ifconfig_text = ""
    if local_hostname is None:
        try:
            local_hostname = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:
            local_hostname = ""

    urls = [f"http://localhost:{port}/"]
    if local_hostname:
        urls.append(f"http://{local_hostname}.local:{port}/")
    urls.extend(f"http://{address}:{port}/" for address in parse_inet_addresses(ifconfig_text))
    return urls


def is_loopback_url(url: str) -> bool:
    """Whether *url* only works on the machine running the engine.

    The PWA labels these, because a phone scanning a `localhost` QR code lands
    on its own device and silently fails.
    """

    return "//localhost:" in url or "//127." in url


def normalize_trusted_url(raw: str) -> str:
    """Validate a trusted HTTPS origin for other devices; "" clears it.

    Only an origin is accepted (scheme + host[:port]) so a copied URL can
    never smuggle a path, query token or credentials into a QR code.
    Raises ValueError with a user-facing message.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme.lower() != "https":
        raise ValueError("trusted_url must start with https://")
    if not parts.hostname:
        raise ValueError("trusted_url needs a host name")
    # urlsplit is permissive: a backslash, a space, an angle bracket or a comma
    # all survive as part of the host, and a browser reads "\" as "/", so such a
    # value would be stored and put in a QR code pointing somewhere else. Allow
    # only what a real host name is made of, and accept a bracketed IPv6 literal
    # (urlsplit has already stripped the brackets) on its own terms. A
    # non-numeric port also survives parsing, so it gets a message of our own
    # rather than leaking Python's port ValueError.
    host_raw = parts.hostname
    if ":" in host_raw:
        import ipaddress

        try:
            ipaddress.IPv6Address(host_raw)
        except ValueError:
            raise ValueError("trusted_url has an invalid IPv6 address") from None
    elif not re.fullmatch(r"[A-Za-z0-9.-]+", host_raw):
        raise ValueError(
            "trusted_url host name may only contain letters, digits, dots and hyphens"
        )
    try:
        port_num = parts.port
    except ValueError:
        raise ValueError("trusted_url has an invalid port") from None
    if parts.username or parts.password:
        raise ValueError("trusted_url must not contain a user name or password")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("trusted_url must be just the address, without a path or query")
    host = parts.hostname.lower()
    # urlsplit strips the brackets off an IPv6 literal, which would otherwise
    # rebuild an unparseable "https://fd7a::1:8443/".
    if ":" in host:
        host = f"[{host}]"
    # 443 is the https default, so "host:443" and "host" are one origin and
    # must compare equal when a typed address is matched against a detected one.
    port = f":{port_num}" if port_num and port_num != 443 else ""
    return f"https://{host}{port}/"


# `tailscale serve` proxies to whatever loopback spelling the user typed, so
# any of these pointing at our port is this engine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# The Mac App Store and standalone Tailscale apps do not put the CLI on PATH;
# it ships inside the bundle instead.
_TAILSCALE_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

# `serve status` answers from the local daemon. A wedged daemon must not hang
# the Other devices card.
_TAILSCALE_TIMEOUT_S = 3.0


def parse_tailscale_serve(status: object, port: int) -> list[str]:
    """HTTPS origins in `tailscale serve status --json` that proxy to *port*.

    Only a root ("/") handler counts: the PWA uses absolute paths, so an engine
    mounted under a sub-path would not load. Each origin goes through
    :func:`normalize_trusted_url`, the same gate a typed address passes.
    """

    if not isinstance(status, dict):
        return []
    web = status.get("Web")
    if not isinstance(web, dict):
        return []
    urls: list[str] = []
    for host_port, config in web.items():
        if not isinstance(host_port, str) or not isinstance(config, dict):
            continue
        handlers = config.get("Handlers")
        root = handlers.get("/") if isinstance(handlers, dict) else None
        proxy = root.get("Proxy") if isinstance(root, dict) else None
        if not isinstance(proxy, str) or not _proxies_to(proxy, port):
            continue
        host, _, serve_port = host_port.rpartition(":")
        origin = f"https://{host}" if serve_port == "443" else f"https://{host_port}"
        try:
            url = normalize_trusted_url(origin)
        except ValueError:
            continue
        if url and url not in urls:
            urls.append(url)
    return urls


def _proxies_to(proxy: str, port: int) -> bool:
    # Tailscale stores a bare port as "http://127.0.0.1:<port>", so a proxy
    # target is always a URL here.
    try:
        parts = urlsplit(proxy)
        return parts.hostname in _LOOPBACK_HOSTS and parts.port == port
    except ValueError:
        return False


def _tailscale_cli() -> str | None:
    from pathlib import Path

    from ciao.tool_path import resolve_tool

    found = resolve_tool("tailscale")
    if found:
        return found
    return _TAILSCALE_APP_CLI if Path(_TAILSCALE_APP_CLI).is_file() else None


def tailscale_serve_urls(port: int) -> list[str]:
    """HTTPS addresses Tailscale Serve publishes for this engine, or []."""

    cli = _tailscale_cli()
    if not cli:
        return []
    try:
        result = subprocess.run(
            [cli, "serve", "status", "--json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TAILSCALE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    try:
        status = json.loads(result.stdout)
    except ValueError:
        return []
    return parse_tailscale_serve(status, port)
