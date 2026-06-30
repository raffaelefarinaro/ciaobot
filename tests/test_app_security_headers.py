from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.security import SecurityHeadersMiddleware


async def _ok(_request):
    return JSONResponse({"ok": True})


def test_security_headers_are_added_to_http_responses() -> None:
    app = Starlette(
        routes=[Route("/", _ok)],
        middleware=[Middleware(SecurityHeadersMiddleware)],
    )

    resp = TestClient(app).get("/")

    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "same-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    csp = resp.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
