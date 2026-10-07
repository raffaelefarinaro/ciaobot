"""The webhook ingress route: ``POST /hooks/v1/{trigger_id}`` over HTTP.

Covers #1010 (child A3 of #974) against a real ``CiaoConfig`` over a tmp
runtime, with **no session cookie anywhere**: this is a machine surface, and the
properties pinned here are the ones that make it one — the trigger's own bearer
secret authorizes it, the dashboard cookie authorizes nothing, every control the
middleware does not run for a ``/hooks/`` path is owned by the route, and a
``202`` carries a durable receipt and no model output.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_hooks import webhook_method_not_allowed, webhook_receive
from ciao.webhooks import (
    MAX_BODY_BYTES,
    RATE_LIMIT_PER_MINUTE,
    RATE_WINDOW_SECONDS,
    RECEIPTS_NAME,
    WebhookStore,
    reset_rate_limits,
)

_BODY = {"text": "the nightly build failed on main"}


@pytest.fixture(autouse=True)
def _clear_shared_window():
    """The per-trigger rate window is module-level; each test starts empty."""
    reset_rate_limits()
    yield
    reset_rate_limits()


async def _spa_like(request):
    """A stand-in for the SPA catch-all: full match for every ``GET``.

    The real one (``Route("/{path:path}", _spa_catchall)``) answers the shell for
    anything, and Starlette stops at the first full match — so this is what
    decides whether the receiver's own 405 survives the route order.
    """
    return PlainTextResponse("<html>spa shell</html>")


def _world(
    tmp_path: Path, monkeypatch
) -> tuple[TestClient, CiaoConfig, dict[str, str]]:
    """A session-guarded app over a real config, with no ``/api/*`` route at all."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            # The registration order app.py uses: the receiver, its own 405, and
            # then the catch-all that must not swallow either.
            Route("/hooks/v1/{trigger_id}", webhook_receive, methods=["POST"]),
            Route(
                "/hooks/v1/{trigger_id}",
                webhook_method_not_allowed,
                methods=["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"],
            ),
            Route("/{path:path}", _spa_like),
        ],
        # The real middleware, so the test proves the point: it protects
        # `/api/*` and `/ws/*` and leaves a `/hooks/` path alone, which is
        # exactly why the route owns its own checks.
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    return (
        TestClient(app),
        config,
        {SESSION_COOKIE: serializer.dumps({"user": "owner"})},
    )


def _store(config: CiaoConfig) -> WebhookStore:
    return WebhookStore(config.state_path.parent / "webhooks.json")


def _trigger(
    config: CiaoConfig, *, name: str = "Nightly build", enabled: bool = True
) -> tuple[str, str]:
    """One configured trigger and its one-time secret."""
    store = _store(config)
    created, secret = store.create(
        name=name, workspace="personal", instructions="File the intake note"
    )
    if enabled:
        store.update(
            created.trigger_id, expected_revision=created.revision, enabled=True
        )
    return created.trigger_id, secret


def _journal(config: CiaoConfig) -> Path:
    return config.state_path.parent / RECEIPTS_NAME


def _post(
    client: TestClient,
    trigger_id: str,
    *,
    secret: str,
    key: str | None = "k1",
    body: Any = None,
    **kwargs: Any,
):
    headers = dict(kwargs.pop("headers", {}))
    headers["Authorization"] = f"Bearer {secret}"
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(
        f"/hooks/v1/{trigger_id}",
        json=_BODY if body is None else body,
        headers=headers,
        **kwargs,
    )


# ── The credential boundary ────────────────────────────────────────────────


def test_a_request_without_a_bearer_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    resp = client.post(
        f"/hooks/v1/{trigger_id}",
        json=_BODY,
        headers={"Idempotency-Key": "k1"},
    )
    wrong_scheme = client.post(
        f"/hooks/v1/{trigger_id}",
        json=_BODY,
        headers={"Idempotency-Key": "k1", "Authorization": secret},
    )
    wrong_secret = _post(client, trigger_id, secret=f"{secret}-wrong")

    for answer in (resp, wrong_scheme, wrong_secret):
        assert answer.status_code == 401
        assert answer.json()["error"]["code"] == "unauthorized"
        assert answer.json()["error"]["retryable"] is False
    assert secret not in resp.text
    assert not _journal(config).exists()


def test_the_session_cookie_grants_nothing_and_is_not_needed(
    tmp_path: Path, monkeypatch
) -> None:
    """A valid dashboard cookie is not a webhook credential, and is not required."""
    client, config, cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    # A logged-in browser with a valid cookie and no bearer secret: refused.
    with_cookie = client.post(
        f"/hooks/v1/{trigger_id}",
        json=_BODY,
        cookies=cookies,
        headers={"Idempotency-Key": "k1"},
    )
    assert with_cookie.status_code == 401
    assert not _journal(config).exists()

    # The trigger's own secret, with no cookie at all: accepted.
    without_cookie = _post(client, trigger_id, secret=secret)
    assert without_cookie.status_code == 202


def test_a_secret_authorizes_only_its_own_trigger(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    first, secret = _trigger(config, name="Nightly build")
    second, _other_secret = _trigger(config, name="Release")

    assert _post(client, first, secret=secret).status_code == 202
    assert _post(client, second, secret=secret).status_code == 401
    assert _post(client, "0" * 32, secret=secret).status_code == 401
    assert len(_journal(config).read_text(encoding="utf-8").splitlines()) == 1


def test_a_disabled_or_revoked_trigger_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    disabled, secret = _trigger(config, enabled=False)

    assert _post(client, disabled, secret=secret).status_code == 401
    assert not _journal(config).exists()

    _store(config).revoke_workspace("personal")
    assert _post(client, disabled, secret=secret).status_code == 401


def test_a_store_this_engine_cannot_read_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """A corrupt store is 500, never 401: a sender must not blame its own secret."""
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)
    store_path = config.state_path.parent / "webhooks.json"
    store_path.write_text("{not a store", encoding="utf-8")

    resp = _post(client, trigger_id, secret=secret)

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "store_unavailable"
    assert not _journal(config).exists()


# ── The controls the middleware does not run here ──────────────────────────


def test_a_cross_origin_browser_request_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    for origin in ("https://evil.example", "null"):
        resp = _post(client, trigger_id, secret=secret, headers={"Origin": origin})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden_origin"
    # Same-origin is not refused, so the check is the origin and not the bearer.
    same_origin = _post(
        client, trigger_id, secret=secret, headers={"Origin": "http://testserver"}
    )
    assert same_origin.status_code == 202


def test_only_post_is_registered(tmp_path: Path, monkeypatch) -> None:
    """A stray method is refused, not answered by the SPA catch-all below it."""
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    for method in ("get", "put", "patch", "delete"):
        answer = getattr(client, method)(f"/hooks/v1/{trigger_id}")
        assert answer.status_code == 405, method
        assert answer.headers["Allow"] == "POST"
        assert "spa shell" not in answer.text

    # A GET carrying the right secret is still a 405: the method is refused
    # before anything is read.
    with_secret = client.get(
        f"/hooks/v1/{trigger_id}",
        headers={"Authorization": f"Bearer {secret}", "Idempotency-Key": "k1"},
    )
    assert with_secret.status_code == 405
    assert not _journal(config).exists()

    # And an unrelated path still reaches the shell, so the app is a real one.
    assert client.get("/automations").status_code == 200
    assert _post(client, trigger_id, secret=secret).status_code == 202


def test_a_body_that_is_not_json_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    for body in (b"not json", b"[]", b'{"text": 7}'):
        resp = client.post(
            f"/hooks/v1/{trigger_id}",
            content=body,
            headers={
                "Authorization": f"Bearer {secret}",
                "Idempotency-Key": "k1",
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_event"
    assert not _journal(config).exists()


def test_a_missing_idempotency_key_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    resp = _post(client, trigger_id, secret=secret, key=None)

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_idempotency_key"
    assert not _journal(config).exists()


def test_an_over_cap_body_is_refused(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)
    oversized = json.dumps({"text": "x" * (MAX_BODY_BYTES + 100)}).encode()
    headers = {
        "Authorization": f"Bearer {secret}",
        "Idempotency-Key": "k1",
        "Content-Type": "application/json",
    }

    declared = client.post(
        f"/hooks/v1/{trigger_id}", content=oversized, headers=headers
    )
    # A chunked body declares nothing, so the cap has to be enforced again while
    # reading: an unauthenticated flood must not be buffered just because it
    # left out the header.
    chunked = client.post(
        f"/hooks/v1/{trigger_id}",
        content=iter([b'{"text": "' + b"x" * 4096 + b'"}'] * 40),
        headers=headers,
    )

    for answer in (declared, chunked):
        assert answer.status_code == 413
        assert answer.json()["error"]["code"] == "payload_too_large"
    assert not _journal(config).exists()


def test_the_same_key_with_a_different_body_conflicts(
    tmp_path: Path, monkeypatch
) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)
    assert _post(client, trigger_id, secret=secret).status_code == 202

    resp = _post(client, trigger_id, secret=secret, body={"text": "something else"})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_conflict"
    assert len(_journal(config).read_text(encoding="utf-8").splitlines()) == 1


def test_a_rate_limited_sender_gets_429(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    for index in range(RATE_LIMIT_PER_MINUTE):
        assert (
            _post(client, trigger_id, secret=secret, key=f"k{index}").status_code == 202
        )

    resp = _post(client, trigger_id, secret=secret, key="one-too-many")

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert resp.json()["error"]["retryable"] is True
    assert resp.headers["Retry-After"] == str(RATE_WINDOW_SECONDS)
    assert (
        len(_journal(config).read_text(encoding="utf-8").splitlines())
        == RATE_LIMIT_PER_MINUTE
    )


# ── What a 202 says, and does not ───────────────────────────────────────────


def test_a_202_carries_a_durable_receipt_and_no_output(
    tmp_path: Path, monkeypatch
) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    resp = _post(client, trigger_id, secret=secret)

    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"receipt_id", "trigger_id", "status"}
    assert body["trigger_id"] == trigger_id
    # Nothing has been dispatched: no chat, no output, and never "completed".
    assert body["status"] == "accepted"
    assert "chat" not in resp.text.lower()

    # The receipt is durable and readable back, beside the trigger store.
    rows = [
        json.loads(line)
        for line in _journal(config).read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in rows] == [body["receipt_id"]]
    assert rows[0]["status"] == "accepted"
    assert rows[0]["idempotency_key"] == "k1"
    assert rows[0]["event_text"] == _BODY["text"]
    assert secret not in resp.text


def test_the_receiver_answers_202_without_waiting_for_the_launch(
    tmp_path: Path, monkeypatch
) -> None:
    """The response is a receipt, not a launch result.

    The launch is scheduled off the request path (#1020), so a turn that takes
    minutes cannot hold a sender's connection open, and the sender learns
    nothing about it from the response. What it *does* learn is the receipt: the
    launch allocation is on disk before the model turn starts, so a chat exists
    by the time ``start_stream`` is called even though the response has already
    gone out.
    """
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class _Pcm:
        """A chat manager that records its calls and blocks inside the turn."""

        def list_projects(self, workspace: str | None = None) -> list[Any]:
            del workspace
            return [
                SimpleNamespace(
                    project_id="proj-general", name="General", workspace="personal"
                )
            ]

        def create_chat(self, project_id, title="New Chat", **kwargs) -> Any:
            calls.append(("create_chat", project_id, kwargs))
            return SimpleNamespace(chat_id="chat-webhook-1", project_id=project_id)

        def start_stream(self, chat_id, prompt, **kwargs) -> None:
            calls.append(("start_stream", chat_id, kwargs))
            entered.set()
            # Stands in for a turn that runs for minutes. The 202 must already
            # have been delivered while this is blocked.
            release.wait(timeout=30)
            finished.set()

    client.app.state.project_chat_manager = _Pcm()
    try:
        with client:
            resp = _post(client, trigger_id, secret=secret)
            assert resp.status_code == 202
            assert resp.json()["status"] == "accepted"
            assert entered.wait(timeout=30), "the launch was never scheduled"
            # The response is out while the turn is still inside start_stream.
            assert not finished.is_set()
            rows = [
                json.loads(line)
                for line in _journal(config).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            # The allocation is durable before the turn, so a crash here would be
            # `interrupted` rather than a receipt that looks un-run.
            assert [row["status"] for row in rows] == ["accepted", "launching"]
            release.set()
            assert finished.wait(timeout=30)
    finally:
        release.set()
    assert [name for name, _target, _kwargs in calls] == ["create_chat", "start_stream"]
    # No `unattended`: the turn is an ordinary one, so an approval card raised in
    # it is an ordinary approval card.
    assert calls[1][2] == {}


def test_a_retry_is_answered_with_the_same_receipt(tmp_path: Path, monkeypatch) -> None:
    client, config, _cookies = _world(tmp_path, monkeypatch)
    trigger_id, secret = _trigger(config)

    first = _post(client, trigger_id, secret=secret)
    retry = _post(client, trigger_id, secret=secret)

    assert retry.status_code == 202
    assert retry.json() == first.json()
    assert len(_journal(config).read_text(encoding="utf-8").splitlines()) == 1
