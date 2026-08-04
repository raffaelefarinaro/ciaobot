"""Node and device web API routes.

Multi-device host/client mode (``/api/node/*``) plus the package status and
update surface, which app.py registers twice: the tunneled ``/api/package/*``
paths (which report and update the host) and the never-proxied
``/api/device/*`` twins for this machine's own install.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.network_addresses import is_loopback_url, server_addresses
from ciao.package_version import package_changelog, package_status, update_package
from ciao.web.routes_helpers import _parse_set_cookie_session, api_error


async def node_addresses_endpoint(request: Request) -> JSONResponse:
    """URLs this engine is reachable at, for sharing with another device.

    Session-protected rather than loopback-public like the tray endpoints: it
    enumerates LAN interface addresses, which is more than an unauthenticated
    caller on this machine needs to know.
    """

    config = getattr(request.app.state, "config", None)
    port = int(getattr(config, "pwa_port", 8443) or 8443)
    urls = await asyncio.to_thread(server_addresses, port)
    return JSONResponse(
        {
            "port": port,
            "addresses": [
                {"url": url, "loopback": is_loopback_url(url)} for url in urls
            ],
        }
    )


async def package_status_endpoint(request: Request) -> JSONResponse:
    """Return installed package version and best-effort update status."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    if callable(fetcher):
        return JSONResponse(await asyncio.to_thread(fetcher))
    return JSONResponse(await asyncio.to_thread(package_status))


async def package_changelog_endpoint(request: Request) -> JSONResponse:
    """Return the commits between the installed and latest release for the update modal."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    status = await asyncio.to_thread(fetcher if callable(fetcher) else package_status)
    current = str(status.get("current_version") or "")
    latest = str(status.get("latest_version") or "")
    changelog = await asyncio.to_thread(
        package_changelog,
        current_version=current,
        latest_version=latest,
    )
    return JSONResponse(
        {
            "current_version": current,
            "latest_version": latest,
            "update_available": bool(status.get("update_available")),
            **changelog,
        }
    )


async def package_update_endpoint(request: Request) -> JSONResponse:
    """Perform package update and restart the server on success."""
    res = await asyncio.to_thread(update_package)
    if res.get("ok"):
        config = request.app.state.config
        async def _do_restart():
            await asyncio.sleep(2)
            fn = getattr(request.app.state, "request_restart", None)
            if callable(fn):
                fn(config.restart_exit_code)
            else:
                from ciao.signals import RestartRequested
                raise RestartRequested(config.restart_exit_code)

        asyncio.create_task(_do_restart())
        return JSONResponse(res)
    else:
        status_code = 400 if res.get("mode") in {"editable", "unknown"} else 500
        return JSONResponse(res, status_code=status_code)


async def node_status_endpoint(request: Request) -> JSONResponse:
    """Return node status (node_id, role, host connection, peers)."""
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return api_error("node_state_manager not initialized", 500)
    local_session = getattr(request.app.state, "local_session_manager", None)
    git_status = local_session.status() if local_session is not None else {}
    status = node_mgr.get_status()
    status["git"] = git_status

    if status.get("role") == "client" and status.get("host_url"):
        host_url = status["host_url"]
        try:
            import httpx
            headers = {}
            session = node_mgr.get_host_session()
            if session:
                from ciao.web.auth import SESSION_COOKIE
                headers["cookie"] = f"{SESSION_COOKIE}={session}"
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get(f"{host_url}/api/startup-status", headers=headers)
                status["host_reachable"] = res.status_code == 200
                status["active_peer_reachable"] = status["host_reachable"]
                # Name and version of the machine the UI is mirroring, so the
                # client can label host-scoped screens instead of leaving the
                # user guessing whose settings they are editing.
                if status["host_reachable"]:
                    try:
                        payload = res.json()
                    except ValueError:
                        payload = {}
                    if isinstance(payload, dict):
                        status["host_node_id"] = str(payload.get("node_id") or "")
                        status["host_version"] = str(payload.get("version") or "")
        except Exception:
            status["host_reachable"] = False
            status["active_peer_reachable"] = False
    else:
        status["host_reachable"] = None
        status["active_peer_reachable"] = None

    return JSONResponse(status)


async def node_connect_endpoint(request: Request) -> JSONResponse:
    """Connect this node as a client tunnel to a remote host.

    Body: ``{ "host_url": "...", "password": "..." }``.
    The host must have PWA auth enabled; password is its auth token.
    """
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return api_error("node_state_manager not initialized", 500)

    try:
        body = await request.json()
    except Exception:
        body = {}

    host_url = str(body.get("host_url") or body.get("url") or "").strip()
    password = str(body.get("password") or body.get("token") or "")
    if not host_url:
        return api_error("host_url is required", 400)
    if not password.strip():
        return JSONResponse(
            {
                "error": "Password is required to connect as a client",
                "auth_required": True,
            },
            status_code=400,
        )

    from ciao.node_state import _normalize_peer_url

    host_url = _normalize_peer_url(host_url)
    if not host_url:
        return api_error("invalid host_url", 400)

    import httpx

    host_session: str | None = None
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            status_res = await client.get(f"{host_url}/api/startup-status")
            if status_res.status_code != 200:
                return JSONResponse(
                    {
                        "error": f"Host unreachable (HTTP {status_res.status_code})",
                        "peer_unreachable": True,
                    },
                    status_code=400,
                )
            host_status = (
                status_res.json()
                if status_res.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            auth_required = (
                bool(host_status.get("auth_required"))
                if isinstance(host_status, dict)
                else False
            )
            if not auth_required:
                return JSONResponse(
                    {
                        "error": (
                            "Host has no password set. On that machine open "
                            "Settings → PWA password, enable protection, then connect again."
                        ),
                        "auth_required": False,
                        "password_required_on_host": True,
                    },
                    status_code=400,
                )

            login_res = await client.post(
                f"{host_url}/api/auth",
                json={"token": password},
            )
            if login_res.status_code != 200:
                detail = ""
                try:
                    payload = login_res.json()
                    if isinstance(payload, dict) and payload.get("error"):
                        detail = str(payload["error"])
                except Exception:
                    detail = (login_res.text or "").strip()[:120]
                if login_res.status_code in {401, 403}:
                    return JSONResponse(
                        {"error": "Invalid password for host", "auth_required": True},
                        status_code=401,
                    )
                return JSONResponse(
                    {
                        "error": (
                            f"Host login failed (HTTP {login_res.status_code}"
                            + (f": {detail}" if detail else "")
                            + ")"
                        ),
                        "peer_unreachable": login_res.status_code >= 500,
                    },
                    status_code=400,
                )
            cookies = []
            try:
                cookies = login_res.headers.get_list("set-cookie")
            except Exception:
                raw = login_res.headers.get("set-cookie")
                if raw:
                    cookies = [raw]
            host_session = _parse_set_cookie_session(cookies)
            if not host_session:
                from ciao.web.auth import SESSION_COOKIE

                host_session = login_res.cookies.get(SESSION_COOKIE)
            if not host_session:
                return api_error(
                    "Host login succeeded but no session cookie was returned", 502
                )
    except Exception as exc:
        return JSONResponse(
            {"error": f"Failed to reach host at {host_url}: {exc}", "peer_unreachable": True},
            status_code=400,
        )

    status = node_mgr.connect_as_client(host_url, host_session=host_session)
    return JSONResponse({"ok": True, "status": status})


async def node_handover_endpoint(request: Request) -> JSONResponse:
    """Become host: ask the connected host to push, pull locally, then promote.

    Body: ``{ "force": bool }``. When not force, the remote host is demoted
    (commit + push) first. Session matching is not required — git sync only.

    ``target_node_url`` may only name the host this node is already connected
    to. The demote call carries the stored host session cookie, so accepting an
    arbitrary URL here would hand that session to whoever supplied it.
    """
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return api_error("node_state_manager not initialized", 500)

    try:
        body = await request.json()
    except Exception:
        body = {}

    from ciao.node_state import _normalize_peer_url

    force = bool(body.get("force", False))
    host_url = node_mgr.get_host_url() or ""
    requested_url = str(body.get("target_node_url") or "").strip()
    if requested_url and _normalize_peer_url(requested_url) != _normalize_peer_url(host_url):
        return api_error("target_node_url does not match the connected host", 400)
    target_url = host_url.rstrip("/")
    local_session = getattr(request.app.state, "local_session_manager", None)

    if target_url and not force:
        try:
            import httpx
            from ciao.web.auth import SESSION_COOKIE

            headers = {}
            session = node_mgr.get_host_session()
            if session:
                headers["cookie"] = f"{SESSION_COOKIE}={session}"
            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.post(f"{target_url}/api/node/demote", headers=headers)
                if res.status_code != 200:
                    return JSONResponse(
                        {
                            "error": (
                                f"Failed to ask host at {target_url} to push "
                                f"(HTTP {res.status_code})"
                            ),
                            "peer_unreachable": True,
                        },
                        status_code=400,
                    )
        except Exception as exc:
            return JSONResponse(
                {
                    "error": f"Failed to reach host at {target_url}: {exc}",
                    "peer_unreachable": True,
                },
                status_code=400,
            )

    resync_result = None
    if local_session is not None:
        resync_result = await local_session.resync()

    status = node_mgr.promote()
    if resync_result:
        status["resync"] = resync_result

    return JSONResponse({"ok": True, "status": status})


async def node_demote_endpoint(request: Request) -> JSONResponse:
    """Push local changes and leave host mode (become client without a tunnel)."""
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return api_error("node_state_manager not initialized", 500)

    local_session = getattr(request.app.state, "local_session_manager", None)
    if local_session is not None:
        await local_session.commit_and_sync()

    status = node_mgr.demote()
    return JSONResponse({"ok": True, "demoted": True, "status": status})


async def node_peers_endpoint(request: Request) -> JSONResponse:
    """Manage registered peer nodes (add/remove). Prefer ``/api/node/connect``."""
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return api_error("node_state_manager not initialized", 500)

    try:
        body = await request.json()
    except Exception:
        return api_error("invalid JSON body", 400)

    action = str(body.get("action", "add")).strip().lower()
    url = str(body.get("url", "")).strip()
    node_id = str(body.get("node_id", "")).strip()

    if not url:
        return api_error("url is required", 400)

    if action == "remove":
        status = node_mgr.remove_peer(url)
    else:
        status = node_mgr.add_peer(url, peer_id=node_id)

    return JSONResponse({"ok": True, "status": status})


async def node_connected_clients_endpoint(request: Request) -> JSONResponse:
    """Live WebSocket clients connected to this node.

    Useful on a host: it shows phones/laptops that currently have an open
    Ciaobot tab or tunneled client. Local loopback sockets are excluded so the
    list only surfaces remote/secondary-device connections.
    """
    tracker = getattr(request.app.state, "connection_tracker", None)
    if tracker is None:
        return JSONResponse({"ok": True, "clients": []})
    return JSONResponse({"ok": True, "clients": tracker.list_clients(remote_only=True)})
