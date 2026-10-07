"""Test helper: an owner session for a test client.

Password protection is always on, so every `/api/*` and `/ws/*` request a test
makes through the auth gate needs the signed session cookie a login would set.
"""

from __future__ import annotations

from starlette.testclient import TestClient

from ciao.web.auth import SESSION_COOKIE


def signed_in(client: TestClient) -> TestClient:
    """Give ``client`` an owner session signed by its app's serializer."""
    serializer = client.app.state.serializer  # type: ignore[attr-defined]
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client
