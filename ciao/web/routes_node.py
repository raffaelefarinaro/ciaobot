"""Node and device web API routes.

Multi-device host/client mode (``/api/node/*``) plus the package status and
update surface, which app.py registers twice: the tunneled ``/api/package/*``
paths (which report and update the host) and the never-proxied
``/api/device/*`` twins for this machine's own install. The ``/api/update/*``
routes sit in the same file: they are the PWA's window onto the engine update
coordinator's own durable job (``ciao update stage|apply|status``).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import engine_update
from ciao.config import RESTART_EXIT_CODE
from ciao.network_addresses import is_loopback_url, server_addresses
from ciao.package_version import (
    detect_install_mode,
    package_changelog,
    package_status,
    update_package,
)
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
    """Return update guidance; packaged app updates are owned by Ciaobot.app."""
    res = await asyncio.to_thread(update_package)
    if res.get("ok"):
        async def _do_restart():
            await asyncio.sleep(2)
            fn = getattr(request.app.state, "request_restart", None)
            if callable(fn):
                fn(RESTART_EXIT_CODE)
            else:
                from ciao.signals import RestartRequested
                raise RestartRequested(RESTART_EXIT_CODE)

        asyncio.create_task(_do_restart())
        return JSONResponse(res)
    else:
        status_code = 400 if res.get("mode") in {"bundled_app", "editable", "installer", "unknown"} else 500
        return JSONResponse(res, status_code=status_code)


# Phases after which no update is running, and a new one may replace the record.
# Everything else — including a record left mid-download by an engine that was
# killed rather than drained — still describes a run that is not known to have
# finished, and a second run would race the first over the same record, lock
# and staged env.
_TERMINAL_PHASES = {"applied", "rolled_back", "rollback_failed", "failed"}
_NOT_INSTALLER = (
    "in-app updates are only for engines installed with the Ciaobot engine installer"
)
_ALREADY_RUNNING = "an engine update is already in progress"
_NOTHING_STAGED = "nothing staged; run: ciao update stage"


def _engine_update_state(request: Request) -> dict[str, Any]:
    """The persisted job + install mode, as the Settings card needs it.

    Fail-safe on the read as well as on the parse: the card polls this, and a
    card that flips into an error state because the record is unreadable is
    worse than one that reports "no update in progress" and says why.
    """
    mode_fn: Callable[[], str] = (
        getattr(request.app.state, "engine_update_mode", None) or detect_install_mode
    )
    read_fn: Callable[[], engine_update.Operation | None] = (
        getattr(request.app.state, "engine_update_read", None) or engine_update.read_operation
    )
    mode = mode_fn()
    state: dict[str, Any] = {
        "install_mode": mode,
        # In-app updates swap the install the receipt describes, so there is
        # nothing to update for an editable checkout or a bundled app.
        "can_update": mode == "installer",
    }
    try:
        operation = read_fn()
    except Exception as exc:
        state["operation"] = None
        state["error"] = str(exc)
        return state
    state["operation"] = asdict(operation) if operation is not None else None
    return state


def _operation_phase(operation: dict[str, Any] | None) -> str:
    """The phase of a served record, or "" when there is no record."""
    if not isinstance(operation, dict):
        return ""
    return str(operation.get("phase") or "")


def _active_update_task(request: Request) -> asyncio.Task[None] | None:
    """The stage/apply run this engine has in flight, if any.

    Kept on ``app.state`` for two reasons: the event loop needs a strong
    reference for the whole run (a bare ``create_task`` result is collectable
    mid-run), and the next request has to be able to see that a job is running
    before the record catches up with it.
    """
    task = getattr(request.app.state, "engine_update_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        return task
    return None


def _update_in_progress(operation: dict[str, Any] | None) -> bool:
    """True when the persisted record still describes a run in flight."""
    if not isinstance(operation, dict):
        return False
    return _operation_phase(operation) not in _TERMINAL_PHASES


def _update_refusal(
    request: Request, state: dict[str, Any], *, record_blocks: bool
) -> JSONResponse | None:
    """The 400/409 this engine must answer with, or None when it may proceed.

    Two questions are common to both halves: an engine installed any other way
    has no install to swap, and a run already in flight has to be allowed to
    finish rather than have a second one started behind it. The third is each
    half's own — a new *stage* may only replace a record that reached a
    terminal phase, while an *apply* starts precisely from a staged record, so
    the record check there is "is there something staged", below.
    """
    if not state["can_update"]:
        return api_error(_NOT_INSTALLER, 400)
    if _active_update_task(request) is not None:
        return api_error(_ALREADY_RUNNING, 409)
    if record_blocks and _update_in_progress(state["operation"]):
        return api_error(_ALREADY_RUNNING, 409)
    return None


def _start_update_task(request: Request, fn: Callable[..., Any], *args: Any) -> None:
    """Run a long update half in a thread, holding a reference to the task.

    ``UpdateError`` — and the ``UpdateInProgress`` that subclasses it — is the
    coordinator's own way of reporting a failure: it leaves a ``failed`` record
    naming the phase that broke, which is where the card reads the truth. So
    the thread only has to not take the event loop down with it.
    """

    async def _run() -> None:
        with contextlib.suppress(engine_update.UpdateError):
            await asyncio.to_thread(fn, *args)

    task = asyncio.create_task(_run())
    request.app.state.engine_update_task = task

    def _release(finished: asyncio.Task[None]) -> None:
        if getattr(request.app.state, "engine_update_task", None) is finished:
            request.app.state.engine_update_task = None

    task.add_done_callback(_release)


async def update_status_endpoint(request: Request) -> JSONResponse:
    """Install mode and the persisted update operation, for the Settings card."""
    return JSONResponse(_engine_update_state(request))


async def update_stage_endpoint(request: Request) -> JSONResponse:
    """Start staging a release for an installer-managed engine.

    Body: optional ``{"version": "x.y.z"}``; absent or unreadable means the
    latest release, as on the CLI. Staging downloads a wheel and builds a whole
    environment, so it runs in the background and the card polls
    ``/api/update/status`` for progress.
    """
    state = _engine_update_state(request)
    refusal = _update_refusal(request, state, record_blocks=True)
    if refusal is not None:
        return refusal

    version: str | None = None
    with contextlib.suppress(ValueError):
        body = await request.json()
        if isinstance(body, dict):
            version = str(body.get("version") or "").strip() or None

    stage_fn: Callable[..., Any] = (
        getattr(request.app.state, "engine_update_stage", None) or engine_update.stage_update
    )
    _start_update_task(request, stage_fn, version)
    return JSONResponse({"started": True, "operation": state["operation"]}, status_code=202)


async def update_apply_endpoint(request: Request) -> JSONResponse:
    """Start the apply half in the background: drain, swap, restart, rollback.

    The apply stops admitting new turns, waits for running ones and hands the
    swap to a detached job, so the connection this came in on would die
    mid-flight even if it were held open.
    """
    state = _engine_update_state(request)
    refusal = _update_refusal(request, state, record_blocks=False)
    if refusal is not None:
        return refusal

    if _operation_phase(state["operation"]) != "staged":
        # The CLI's wording, because it is the same situation: the record has to
        # be staged again before there is anything to apply.
        return api_error(_NOTHING_STAGED, 400)

    apply_fn: Callable[..., Any] = (
        getattr(request.app.state, "engine_update_apply", None) or engine_update.apply_update
    )
    _start_update_task(request, apply_fn)
    return JSONResponse({"started": True}, status_code=202)


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
            from ciao.node_state import peer_url_is_allowed

            if not peer_url_is_allowed(str(host_url), str(request.url.scheme or "")):
                raise ValueError("peer transport is not allowed")
            import httpx
            headers = {}
            session = node_mgr.get_host_session()
            if session:
                from ciao.web.auth import SESSION_COOKIE
                headers["cookie"] = f"{SESSION_COOKIE}={session}"
            async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as client:
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

    from ciao.node_state import _normalize_peer_url, peer_url_is_allowed

    host_url = _normalize_peer_url(host_url)
    if not host_url or not peer_url_is_allowed(host_url, str(request.url.scheme or "")):
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
                            "Settings → PWA password, set a password, then connect again."
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
    from ciao.web.routes_auth import _clear_auth_bridges, client_session_response

    _clear_auth_bridges(request.app)
    return client_session_response(request, {"ok": True, "status": status})


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

    from ciao.node_state import _normalize_peer_url, peer_url_is_allowed

    force = bool(body.get("force", False))
    host_url = node_mgr.get_host_url() or ""
    requested_url = str(body.get("target_node_url") or "").strip()
    if requested_url and _normalize_peer_url(requested_url) != _normalize_peer_url(host_url):
        return api_error("target_node_url does not match the connected host", 400)
    target_url = host_url.rstrip("/")
    if target_url and not peer_url_is_allowed(target_url, str(request.url.scheme or "")):
        return api_error("connected host transport is not allowed", 400)
    local_session = getattr(request.app.state, "local_session_manager", None)

    if target_url and not force:
        try:
            import httpx
            from ciao.web.auth import SESSION_COOKIE

            headers = {}
            session = node_mgr.get_host_session()
            if session:
                headers["cookie"] = f"{SESSION_COOKIE}={session}"
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
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

    from ciao.web.routes_auth import _clear_auth_bridges

    _clear_auth_bridges(request.app)
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
