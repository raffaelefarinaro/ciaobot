"""Bound and replay HTTP API request bodies before route handlers run."""

from __future__ import annotations

import json
import tempfile
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from ciao.web.auth import _setup_finish_refusal

DEFAULT_BODY_LIMIT = 1024 * 1024
UPLOAD_BODY_LIMIT = 512 * 1024 * 1024 + 1024 * 1024
IMAGE_BODY_LIMIT = 100 * 10 * 1024 * 1024 + 1024 * 1024
SKILL_IMPORT_BODY_LIMIT = 10 * 1024 * 1024
WORKSPACE_WRITE_BODY_LIMIT = 13 * 1024 * 1024
SPOOL_MEMORY_LIMIT = 1024 * 1024
REPLAY_CHUNK_SIZE = 64 * 1024


def _route_limit(app: Any, scope: dict[str, Any]) -> tuple[int, str]:
    path = scope.get("path", "")
    method = scope.get("method", "").upper()
    if not path.startswith("/api/"):
        return 0, ""
    current = app
    routes = getattr(getattr(current, "router", None), "routes", None)
    while routes is None and hasattr(current, "app"):
        current = current.app
        routes = getattr(getattr(current, "router", None), "routes", None)
        if routes is None:
            routes = getattr(current, "routes", None)
    if routes is None:
        return DEFAULT_BODY_LIMIT, ""
    for route in routes:
        match, _child_scope = route.matches(scope)
        if match is Match.FULL:
            route_path = getattr(route, "path", "")
            if method == "POST" and route_path in {
                "/api/projects/{project_id}/files",
                "/api/chats/{chat_id}/attachments",
            }:
                return UPLOAD_BODY_LIMIT, "multipart_only"
            if method == "POST" and route_path == "/api/chats/{chat_id}/images":
                return IMAGE_BODY_LIMIT, "multipart_only"
            if method == "POST" and route_path == "/api/workspace-file":
                return WORKSPACE_WRITE_BODY_LIMIT, ""
            if method == "POST" and route_path == "/api/skills/import":
                return SKILL_IMPORT_BODY_LIMIT, "skill_import"
            return DEFAULT_BODY_LIMIT, ""
    return DEFAULT_BODY_LIMIT, ""


class ApiRequestBodyLimitMiddleware:
    """Buffer one bounded API body to a spooled file before dispatch."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        limit, policy = _route_limit(self.app, scope)
        if not limit:
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/api/setup/finish" and scope.get("method", "").upper() == "POST":
            refusal = _setup_finish_refusal(Request(scope))
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared < 0 or declared > limit:
                await self._reject(scope, receive, send, policy)
                return

        spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_LIMIT, mode="w+b")
        size = 0
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > limit:
                    await self._reject(scope, receive, send, policy)
                    return
                spool.write(chunk)
                if not message.get("more_body", False):
                    break

            media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
            # The PWA labels every request JSON, bodiless GETs included, so an
            # empty body is no body rather than malformed JSON.
            if size and policy != "multipart_only" and (
                media_type == b"application/json" or media_type.endswith(b"+json")
            ):
                spool.seek(0)
                try:
                    json.loads(spool.read())
                except (ValueError, RecursionError, UnicodeDecodeError):
                    await JSONResponse({"error": "bad json"}, status_code=400)(scope, receive, send)
                    return
                spool.seek(0)

            # Non-JSON bodies need the same rewind as parsed JSON bodies before
            # a handler starts consuming the replay stream.
            spool.seek(0)

            async def replay() -> dict[str, Any]:
                data = spool.read(REPLAY_CHUNK_SIZE)
                return {"type": "http.request", "body": data, "more_body": bool(data) or spool.tell() < size}

            await self.app(scope, replay, send)
        finally:
            spool.close()

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any, policy: str) -> None:
        if policy == "skill_import":
            response = JSONResponse(
                {"ok": False, "error": "Zip too large (max 10 MB)."},
                status_code=400,
            )
        else:
            response = JSONResponse({"error": "request body too large"}, status_code=413)
        await response(scope, receive, send)
