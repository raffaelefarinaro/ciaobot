from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.request_limits import ApiRequestBodyLimitMiddleware, DEFAULT_BODY_LIMIT, _route_limit
from ciao.web.request_limits import SKILL_IMPORT_BODY_LIMIT
from ciao.web.auth import AuthMiddleware, make_serializer
from ciao.web.security import SecurityHeadersMiddleware
from ciao.web.app import create_app


def _config(tmp_path: Path):
    from ciao.config import CiaoConfig, WorkspaceConfig

    return CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )


def _app() -> Starlette:
    async def receive_body(request):
        body = await request.body()
        return Response(json.dumps({"size": len(body)}), media_type="application/json")

    async def echo_body(request):
        return Response(await request.body(), media_type="application/octet-stream")

    return Starlette(routes=[
        Route("/api/write", receive_body, methods=["POST"]),
        Route("/api/projects/{project_id}/files", receive_body, methods=["POST"]),
        Route("/api/skills/import", receive_body, methods=["POST"]),
        Route("/api/workspace-file", receive_body, methods=["POST"]),
        Route("/api/echo", echo_body, methods=["POST"]),
    ])


def test_declared_oversize_is_rejected_without_reading_body() -> None:
    async def run() -> tuple[list[dict], bool]:
        app = ApiRequestBodyLimitMiddleware(_app())
        read = False
        sent: list[dict] = []

        async def receive():
            nonlocal read
            read = True
            raise AssertionError("body must not be consumed")

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "POST", "path": "/api/write", "headers": [(b"content-length", str(DEFAULT_BODY_LIMIT + 1).encode())]}, receive, send)
        return sent, read

    messages, consumed = asyncio.run(run())
    assert not consumed
    assert next(m["status"] for m in messages if m["type"] == "http.response.start") == 413


def test_actual_chunked_body_is_limited_and_handler_is_not_called() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    response = client.post("/api/write", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    assert response.status_code == 413


def test_false_small_content_length_does_not_bypass_limit() -> None:
    async def run() -> tuple[list[dict], bool]:
        app = ApiRequestBodyLimitMiddleware(_app())
        chunks = [b"x" * (DEFAULT_BODY_LIMIT + 1), b""]
        sent: list[dict] = []

        async def receive():
            chunk = chunks.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/api/write", "headers": [(b"content-length", b"1")]}
        await app(scope, receive, send)
        return sent, bool(chunks)

    messages, _ = asyncio.run(run())
    assert next(m["status"] for m in messages if m["type"] == "http.response.start") == 413


def test_large_upload_route_gets_exception_but_generic_multipart_does_not() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    admitted = client.post("/api/projects/p/files", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    refused = client.post("/api/write", content=b"x" * (DEFAULT_BODY_LIMIT + 1), headers={"content-type": "multipart/form-data; boundary=x"})
    assert admitted.status_code == 200
    assert refused.status_code == 413


def test_deep_json_is_rejected_before_handler() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    response = client.post("/api/write", content=b'{"x":' * 20000 + b"0" + b"}" * 20000, headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_oversized_json_integer_is_rejected_before_handler() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    response = client.post(
        "/api/write",
        content=b'{"value":' + b"9" * 5000 + b"}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_skill_import_keeps_its_existing_400_limit_contract() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    response = client.post("/api/skills/import", content=b"x" * (SKILL_IMPORT_BODY_LIMIT + 1))
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Zip too large (max 10 MB)."}


def test_workspace_write_accepts_two_mib_text_with_worst_case_json_escaping() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    payload = json.dumps({"content": "\x01" * (2 * 1024 * 1024)}).encode()
    assert len(payload) > DEFAULT_BODY_LIMIT
    response = client.post(
        "/api/workspace-file",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["size"] == len(payload)


def test_raw_multipart_body_replays_byte_for_byte() -> None:
    client = TestClient(ApiRequestBodyLimitMiddleware(_app()))
    body = (
        b"--boundary\r\n"
        b'Content-Disposition: form-data; name="field"\r\n\r\n'
        b"opaque\x00payload\r\n"
        b"--boundary--\r\n"
    )
    response = client.post(
        "/api/echo",
        content=body,
        headers={"content-type": "multipart/form-data; boundary=boundary"},
    )
    assert response.content == body


def test_starlette_middleware_topology_matches_route_exceptions_and_default() -> None:
    serializer = make_serializer("test-secret")
    app = Starlette(
        routes=_app().router.routes,
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            Middleware(AuthMiddleware, serializer=serializer),
            Middleware(ApiRequestBodyLimitMiddleware),
        ],
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    oversized_upload = client.post(
        "/api/projects/p/files", content=b"x" * (DEFAULT_BODY_LIMIT + 1)
    )
    oversized_default = client.post("/api/write", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    assert oversized_upload.status_code == 200
    assert oversized_default.status_code == 413


def _setup_finish_asgi_app(peer: tuple[str, int], origin: str | None = None) -> tuple[Starlette, dict[str, str]]:
    from ciao.config import CiaoConfig
    from ciao.web.routes_api import setup_finish_endpoint

    config = CiaoConfig.from_env({"CIAO_BOOTSTRAP_WORKSPACE": "/tmp/ciao-setup-body-limit-test"})
    app = Starlette(
        routes=[Route("/api/setup/finish", setup_finish_endpoint, methods=["POST"])],
        middleware=[
            Middleware(SecurityHeadersMiddleware),
            Middleware(AuthMiddleware, serializer=make_serializer("test-secret")),
            Middleware(ApiRequestBodyLimitMiddleware),
        ],
    )
    app.state.config = config
    request_headers = {"host": "localhost:8543", "content-type": "application/json"}
    if origin is not None:
        request_headers["origin"] = origin
    return app, request_headers


def test_setup_remote_peer_is_refused_before_body_read() -> None:
    _assert_setup_refusal_precedes_body_read(("192.0.2.4", 12345), None)


def test_setup_forbidden_origin_is_refused_before_body_read() -> None:
    _assert_setup_refusal_precedes_body_read(("127.0.0.1", 12345), "https://attacker.example")


def _assert_setup_refusal_precedes_body_read(peer: tuple[str, int], origin: str | None) -> None:
    async def run() -> tuple[list[dict], bool]:
        app, headers = _setup_finish_asgi_app(peer, origin)
        consumed = False
        sent: list[dict] = []

        async def receive():
            nonlocal consumed
            consumed = True
            return {"type": "http.request", "body": b'{"workspace":"/tmp/nope"}', "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/setup/finish",
            "raw_path": b"/api/setup/finish",
            "query_string": b"",
            "root_path": "",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "client": peer,
            "server": ("127.0.0.1", 8543),
        }
        await app(scope, receive, send)
        return sent, consumed

    messages, consumed = asyncio.run(run())
    assert not consumed
    assert next(message["status"] for message in messages if message["type"] == "http.response.start") == 403
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert b"setup finish is localhost-only" in response_body


def test_auth_refusal_precedes_body_consumption() -> None:
    async def endpoint(request):
        await request.body()
        return Response("unexpected")

    app = Starlette(
        routes=[Route("/api/write", endpoint, methods=["POST"])],
        middleware=[
            Middleware(AuthMiddleware, serializer=make_serializer("test-secret")),
            Middleware(ApiRequestBodyLimitMiddleware),
        ],
    )
    response = TestClient(app).post("/api/write", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    assert response.status_code == 401


def test_forbidden_origin_precedes_body_consumption() -> None:
    serializer = make_serializer("test-secret")
    app = Starlette(
        routes=[Route("/api/write", lambda _request: Response("unexpected"), methods=["POST"])],
        middleware=[
            Middleware(AuthMiddleware, serializer=serializer),
            Middleware(ApiRequestBodyLimitMiddleware),
        ],
    )
    client = TestClient(app)
    client.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    response = client.post(
        "/api/write",
        content=b"x" * (DEFAULT_BODY_LIMIT + 1),
        headers={"origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_create_app_registers_limit_middleware_and_matches_real_routes(tmp_path: Path) -> None:
    from ciao.web.request_limits import IMAGE_BODY_LIMIT, UPLOAD_BODY_LIMIT, WORKSPACE_WRITE_BODY_LIMIT

    app = create_app(_config(tmp_path))
    assert any(item.cls is ApiRequestBodyLimitMiddleware for item in app.user_middleware)
    paths = {route.path: route for route in app.routes if hasattr(route, "path")}
    middleware = ApiRequestBodyLimitMiddleware(app)
    for method, path, limit in (
        ("POST", "/api/chats/c/attachments", UPLOAD_BODY_LIMIT),
        ("POST", "/api/projects/p/files", UPLOAD_BODY_LIMIT),
        ("POST", "/api/chats/c/images", IMAGE_BODY_LIMIT),
        ("POST", "/api/workspace-file", WORKSPACE_WRITE_BODY_LIMIT),
        ("POST", "/api/chats/c/attachments/near-miss", DEFAULT_BODY_LIMIT),
        ("GET", "/api/chats/c/attachments", DEFAULT_BODY_LIMIT),
    ):
        scope = {"type": "http", "method": method, "path": path, "headers": []}
        assert _route_limit(app, scope)[0] == limit
    assert "/api/chats/{chat_id}/attachments" in paths


def test_documented_numeric_body_limits_are_pinned() -> None:
    from ciao.web.request_limits import IMAGE_BODY_LIMIT, UPLOAD_BODY_LIMIT, WORKSPACE_WRITE_BODY_LIMIT

    assert DEFAULT_BODY_LIMIT == 1 * 1024 * 1024
    assert UPLOAD_BODY_LIMIT == 513 * 1024 * 1024
    assert IMAGE_BODY_LIMIT == 1001 * 1024 * 1024
    assert SKILL_IMPORT_BODY_LIMIT == 10 * 1024 * 1024
    assert WORKSPACE_WRITE_BODY_LIMIT == 13 * 1024 * 1024


def test_exact_route_limits_are_admitted_without_allocating_the_body(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    middleware = ApiRequestBodyLimitMiddleware(app)

    async def status_for(path: str, limit: int) -> tuple[int, bool]:
        sent = []
        consumed = False
        async def receive():
            nonlocal consumed
            consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await middleware(
            {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 8543), "method": "POST",
             "headers": [(b"content-length", str(limit).encode())]},
            receive,
            send,
        )
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        return status, consumed

    for path, cap in (
        ("/api/chats/c/attachments", 513 * 1024 * 1024),
        ("/api/projects/p/files", 513 * 1024 * 1024),
        ("/api/chats/c/images", 1001 * 1024 * 1024),
        ("/api/workspace-file", 13 * 1024 * 1024),
        ("/api/skills/import", 10 * 1024 * 1024),
        ("/api/workspace-open", 1 * 1024 * 1024),
    ):
        status, consumed = asyncio.run(status_for(path, cap))
        assert status != 413
        assert consumed


def test_method_and_path_near_misses_get_default_cap(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    for method, path in (
        ("GET", "/api/chats/c/images"),
        ("PUT", "/api/chats/c/attachments"),
        ("POST", "/api/chats/c/images/extra"),
        ("POST", "/api/projects/p/files/extra"),
        ("POST", "/api/workspace-file/extra"),
    ):
        scope = {"type": "http", "method": method, "path": path, "headers": []}
        assert _route_limit(app, scope)[0] == 1 * 1024 * 1024


def test_documented_limit_plus_one_is_rejected_without_reading(tmp_path: Path) -> None:
    app = ApiRequestBodyLimitMiddleware(create_app(_config(tmp_path)))

    async def check(path: str, cap: int) -> tuple[int, bool]:
        consumed = False
        sent = []
        async def receive():
            nonlocal consumed
            consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await app({"type": "http", "method": "POST", "path": path,
                   "headers": [(b"content-length", str(cap + 1).encode())]}, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start"), consumed

    for path, cap in (
        ("/api/chats/c/attachments", 513 * 1024 * 1024),
        ("/api/projects/p/files", 513 * 1024 * 1024),
        ("/api/chats/c/images", 1001 * 1024 * 1024),
        ("/api/workspace-file", 13 * 1024 * 1024),
        ("/api/skills/import", 10 * 1024 * 1024),
        ("/api/workspace-open", 1 * 1024 * 1024),
    ):
        status, consumed = asyncio.run(check(path, cap))
        assert status in (400, 413)
        assert not consumed


def test_factory_route_ceilings_and_plus_one_are_checked_from_length_only(tmp_path: Path) -> None:
    from ciao.web.request_limits import IMAGE_BODY_LIMIT, UPLOAD_BODY_LIMIT, WORKSPACE_WRITE_BODY_LIMIT

    factory_app = create_app(_config(tmp_path))
    middleware = ApiRequestBodyLimitMiddleware(factory_app)

    async def check(path: str, limit: int) -> tuple[int, bool]:
        consumed = False
        sent = []
        async def receive():
            nonlocal consumed
            consumed = True
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": path, "headers": [(b"content-length", str(limit + 1).encode())]}
        await middleware(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start"), consumed

    for path, cap in (
        ("/api/chats/c/attachments", UPLOAD_BODY_LIMIT),
        ("/api/projects/p/files", UPLOAD_BODY_LIMIT),
        ("/api/chats/c/images", IMAGE_BODY_LIMIT),
        ("/api/workspace-file", WORKSPACE_WRITE_BODY_LIMIT),
    ):
        assert asyncio.run(check(path, cap)) == (413, False)


def test_actual_factory_middleware_handles_route_exception_and_default(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    app.state.project_chat_manager = SimpleNamespace(get_chat=lambda _chat_id: None)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("ciao_session", make_serializer("test-secret").dumps({"user": "owner"}))
    # Exempt chat attachment route: admitted above the generic ceiling.
    admitted = client.post("/api/chats/unknown/attachments", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    assert admitted.status_code == 404
    # Generic route is still capped, before any downstream route work.
    rejected = client.post("/api/workspace-open", content=b"x" * (DEFAULT_BODY_LIMIT + 1))
    assert rejected.status_code == 413


def test_multi_chunk_no_length_and_false_small_length_overflow_do_not_dispatch() -> None:
    async def exercise(content_length: bytes | None) -> tuple[int, bool]:
        called = False

        async def endpoint(request):
            nonlocal called
            called = True
            return Response("mutated")

        app = ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/write", endpoint, methods=["POST"])]))
        chunks = [b"x" * (DEFAULT_BODY_LIMIT // 2), b"y" * (DEFAULT_BODY_LIMIT // 2), b"z"]
        consumed = 0
        async def receive():
            nonlocal consumed
            chunk = chunks[consumed]
            consumed += 1
            return {"type": "http.request", "body": chunk, "more_body": consumed < len(chunks)}
        messages = []
        async def send(message):
            messages.append(message)
        headers = [] if content_length is None else [(b"content-length", content_length)]
        await app({"type": "http", "method": "POST", "path": "/api/write", "headers": headers}, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        return status, called

    assert asyncio.run(exercise(None)) == (413, False)
    assert asyncio.run(exercise(b"1")) == (413, False)


def test_default_exact_limit_multi_chunk_body_reaches_handler_unchanged() -> None:
    expected = b"x" * (DEFAULT_BODY_LIMIT // 2) + b"y" * (DEFAULT_BODY_LIMIT - DEFAULT_BODY_LIMIT // 2)
    received = bytearray()

    async def endpoint(request):
        received.extend(await request.body())
        return Response(str(len(received)))

    app = ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/write", endpoint, methods=["POST"])]))
    chunks = [expected[:DEFAULT_BODY_LIMIT // 2], expected[DEFAULT_BODY_LIMIT // 2:]]
    index = 0

    async def receive():
        nonlocal index
        chunk = chunks[index]
        index += 1
        return {"type": "http.request", "body": chunk, "more_body": index < len(chunks)}

    messages = []
    async def send(message):
        messages.append(message)

    asyncio.run(app(
        {"type": "http", "method": "POST", "path": "/api/write", "headers": [(b"content-length", str(DEFAULT_BODY_LIMIT).encode())]},
        receive, send,
    ))
    assert next(m["status"] for m in messages if m["type"] == "http.response.start") == 200
    assert bytes(received) == expected
    assert len(received) == DEFAULT_BODY_LIMIT


def test_exact_upload_policy_body_is_admitted_through_factory_app(monkeypatch, tmp_path: Path) -> None:
    from ciao.web import request_limits

    cap = 32 * 1024
    monkeypatch.setattr(request_limits, "UPLOAD_BODY_LIMIT", cap)
    factory_app = create_app(_config(tmp_path))
    received = bytearray()

    class BodyReceiver:
        # Use the factory's production route table for policy matching, while
        # recording exactly what the middleware replays to its downstream app.
        router = factory_app.router

        async def __call__(self, scope, receive, send):
            while True:
                message = await receive()
                received.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await Response("admitted")(scope, receive, send)

    middleware = ApiRequestBodyLimitMiddleware(BodyReceiver())
    body = b"u" * cap
    consumed = False

    async def receive():
        nonlocal consumed
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []
    async def send(message):
        sent.append(message)

    asyncio.run(middleware(
        {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
         "scheme": "http", "method": "POST", "path": "/api/chats/c/attachments",
         "raw_path": b"/api/chats/c/attachments", "query_string": b"", "root_path": "",
         "headers": [(b"host", b"localhost:8543")], "client": ("127.0.0.1", 1),
         "server": ("127.0.0.1", 8543)},
        receive, send,
    ))
    assert consumed
    assert bytes(received) == body
    assert next(message["status"] for message in sent if message["type"] == "http.response.start") == 200


def test_malformed_json_and_invalid_encoding_are_400_without_dispatch() -> None:
    for body in (b'{"broken":', b'{"value":"\xff"}'):
        called = False
        async def endpoint(request):
            nonlocal called
            called = True
            return Response("unexpected")
        app = TestClient(ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/write", endpoint, methods=["POST"])])))
        response = app.post("/api/write", content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert called is False


def test_empty_json_labelled_body_reaches_handler() -> None:
    async def endpoint(request):
        return Response(json.dumps({"size": len(await request.body())}), media_type="application/json")
    app = TestClient(ApiRequestBodyLimitMiddleware(Starlette(routes=[
        Route("/api/read", endpoint, methods=["GET", "POST"]),
    ])))
    for method in ("GET", "POST"):
        response = app.request(method, "/api/read", headers={"content-type": "application/json"})
        assert response.status_code == 200
        assert response.json() == {"size": 0}


def test_factory_admits_bodiless_get_with_the_pwa_json_header(tmp_path: Path) -> None:
    # web/src/lib/api.ts sends Content-Type: application/json on every call.
    client = TestClient(create_app(_config(tmp_path)), raise_server_exceptions=False)
    client.cookies.set("ciao_session", make_serializer("test-secret").dumps({"user": "owner"}))
    response = client.get("/api/workspaces", headers={"content-type": "application/json"})
    assert response.status_code == 200


def test_auth_denials_do_not_call_receive(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    async def run(origin: str | None, cookie: str | None) -> tuple[int, bool]:
        consumed = False
        sent = []
        async def receive():
            nonlocal consumed
            consumed = True
            return {"type": "http.request", "body": b"secret", "more_body": False}
        async def send(message):
            sent.append(message)
        headers = [(b"host", b"localhost:8543"), (b"content-type", b"application/json")]
        if origin:
            headers.append((b"origin", origin.encode()))
        if cookie:
            headers.append((b"cookie", f"ciao_session={cookie}".encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/workspace-open", "raw_path": b"/api/workspace-open", "query_string": b"", "root_path": "", "headers": headers, "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8543)}
        await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start"), consumed
    assert asyncio.run(run(None, None)) == (401, False)
    signed = make_serializer("test-secret").dumps({"user": "owner"})
    assert asyncio.run(run("https://attacker.example", signed)) == (403, False)


def test_spool_rolls_over_replays_multipart_and_closes_on_all_paths(monkeypatch) -> None:
    import tempfile
    from ciao.web import request_limits

    opened = []
    real_spool = tempfile.SpooledTemporaryFile
    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool
    monkeypatch.setattr(request_limits.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(request_limits, "SPOOL_MEMORY_LIMIT", 32 * 1024)

    async def run(body: bytes, *, content_type: bytes = b"application/octet-stream", fail=False, disconnect=False):
        received = 0
        replayed = bytearray()
        handled = False
        async def endpoint(request):
            nonlocal handled
            handled = True
            if fail:
                raise RuntimeError("downstream failure")
            while True:
                message = await request.receive()
                replayed.extend(message.get("body", b""))
                if not message.get("more_body"):
                    break
            return Response("ok")
        app = ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/echo", endpoint, methods=["POST"])]))
        chunks = [body[i:i + 128 * 1024] for i in range(0, len(body), 128 * 1024)]
        async def receive():
            nonlocal received
            if disconnect:
                return {"type": "http.disconnect"}
            chunk = chunks[received]
            received += 1
            return {"type": "http.request", "body": chunk, "more_body": received < len(chunks)}
        sent = []
        async def send(message):
            sent.append(message)
        scope = {"type": "http", "method": "POST", "path": "/api/echo", "headers": [(b"content-type", content_type)]}
        if fail:
            try:
                await app(scope, receive, send)
            except RuntimeError:
                pass
        else:
            await app(scope, receive, send)
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        return status, bytes(replayed), handled

    boundary = b"--b\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\n" + b"a" * (200 * 1024) + b"\r\n--b--\r\n"
    assert asyncio.run(run(boundary, content_type=b"multipart/form-data; boundary=b")) == (200, boundary, True)
    assert opened[-1]._rolled and opened[-1].closed
    assert asyncio.run(run(b"x" * (DEFAULT_BODY_LIMIT + 1)))[0] == 413
    assert opened[-1].closed
    assert asyncio.run(run(b"{", content_type=b"application/json"))[0] == 400
    assert opened[-1].closed
    before = len(opened)
    disconnect_result = asyncio.run(run(b"x", disconnect=True))
    assert disconnect_result[0] is None and not disconnect_result[2]
    assert opened[-1].closed and len(opened) == before + 1
    before = len(opened)
    assert asyncio.run(run(b"x", fail=True))[0] == 500
    assert opened[-1].closed and len(opened) == before + 1


def test_rolled_multipart_is_parsed_by_request_form(monkeypatch) -> None:
    import tempfile
    from ciao.web import request_limits

    opened = []
    real_spool = tempfile.SpooledTemporaryFile
    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool
    monkeypatch.setattr(request_limits.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(request_limits, "SPOOL_MEMORY_LIMIT", 32 * 1024)
    file_bytes = b"file-data\x00" + b"z" * (64 * 1024)
    body = (
        b"--form-boundary\r\nContent-Disposition: form-data; name=\"title\"\r\n\r\nhello\r\n"
        b"--form-boundary\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"x.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n" + file_bytes + b"\r\n--form-boundary--\r\n"
    )

    async def endpoint(request):
        async with request.form() as form:
            assert form["title"] == "hello"
            assert await form["upload"].read() == file_bytes
        return Response("ok")

    app = ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/form", endpoint, methods=["POST"])]))
    response = TestClient(app).post(
        "/api/form", content=body,
        headers={"content-type": "multipart/form-data; boundary=form-boundary"},
    )
    assert response.status_code == 200
    assert opened[0]._rolled and opened[0].closed


@pytest.mark.parametrize("path", [
    "/api/projects/p/files",
    "/api/chats/c/attachments",
    "/api/chats/c/images",
])
@pytest.mark.parametrize("content_type", ["application/json", "application/vnd.example+json"])
def test_multipart_upload_routes_skip_json_preparse_and_replay_rolled_body(
    monkeypatch, path: str, content_type: str
) -> None:
    import tempfile
    from ciao.web import request_limits

    opened = []
    real_spool = tempfile.SpooledTemporaryFile

    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool

    monkeypatch.setattr(request_limits.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(request_limits, "SPOOL_MEMORY_LIMIT", 32 * 1024)
    body = b"{" + b"a" * (128 * 1024)
    replayed = bytearray()
    replay_chunks: list[int] = []

    async def endpoint(request):
        while True:
            message = await request.receive()
            chunk = message.get("body", b"")
            replay_chunks.append(len(chunk))
            replayed.extend(chunk)
            if not message.get("more_body"):
                break
        return Response("ok")

    route_path = {
        "/api/projects/p/files": "/api/projects/{project_id}/files",
        "/api/chats/c/attachments": "/api/chats/{chat_id}/attachments",
        "/api/chats/c/images": "/api/chats/{chat_id}/images",
    }[path]
    app = ApiRequestBodyLimitMiddleware(
        Starlette(routes=[Route(route_path, endpoint, methods=["POST"])])
    )
    response = TestClient(app).post(path, content=body, headers={"content-type": content_type})

    assert response.status_code == 200
    assert bytes(replayed) == body
    assert len(replay_chunks) > 1
    assert max(replay_chunks) <= request_limits.REPLAY_CHUNK_SIZE
    assert opened[0]._rolled and opened[0].closed


def test_partial_disconnect_and_receive_exception_close_rolled_spool_without_dispatch(monkeypatch) -> None:
    import tempfile
    from ciao.web import request_limits

    opened = []
    real_spool = tempfile.SpooledTemporaryFile
    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool
    monkeypatch.setattr(request_limits.tempfile, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(request_limits, "SPOOL_MEMORY_LIMIT", 32 * 1024)
    handled = False
    async def endpoint(_request):
        nonlocal handled
        handled = True
        return Response("unexpected")
    app = ApiRequestBodyLimitMiddleware(Starlette(routes=[Route("/api/write", endpoint, methods=["POST"])]))

    async def exercise(raises: bool) -> None:
        calls = 0
        async def receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "http.request", "body": b"a" * 64 * 1024, "more_body": True}
            if raises:
                raise OSError("receive failed")
            return {"type": "http.disconnect"}
        async def send(_message):
            pass
        scope = {"type": "http", "method": "POST", "path": "/api/write", "headers": []}
        if raises:
            try:
                await app(scope, receive, send)
            except OSError:
                pass
            else:
                raise AssertionError("receive exception should propagate")
        else:
            await app(scope, receive, send)

    for raises in (False, True):
        handled = False
        asyncio.run(exercise(raises))
        assert not handled
        assert opened[-1]._rolled and opened[-1].closed
