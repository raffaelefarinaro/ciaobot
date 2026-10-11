from __future__ import annotations

from types import SimpleNamespace
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import chat_compact, chat_prompt

_ORIGIN = "https://ciao.example"


class _FakeChat:
    def __init__(self, chat_id, archived=False):
        self.chat_id = chat_id
        self.title = "Test Chat"
        self.archived = archived


class _FakePCM:
    def __init__(self, chat_exists=True, archived=False):
        self.chat_exists = chat_exists
        self.archived = archived
        self.stream_started = None
        self.resolved_images = []

    def get_chat(self, chat_id):
        if self.chat_exists:
            return _FakeChat(chat_id, archived=self.archived)
        return None

    def resolve_image_ref(self, ref):
        if ref in self.resolved_images:
            return SimpleNamespace(original_filename=ref)
        return None

    def start_stream(self, chat_id, prompt, images=None):
        self.stream_started = {
            "chat_id": chat_id,
            "prompt": prompt,
            "images": images,
        }
        return SimpleNamespace()


def _client(*, pcm=None):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/prompt", chat_prompt, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(claude_default_model="opus")
    app.state.project_chat_manager = pcm
    client = TestClient(app, base_url=_ORIGIN)
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def test_chat_prompt_success() -> None:
    pcm = _FakePCM()
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/chats/chat-123/prompt",
        json={"prompt": "Hello world!"},
        cookies=cookies,
        headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chat_id": "chat-123"}
    assert pcm.stream_started["chat_id"] == "chat-123"
    assert pcm.stream_started["prompt"] == "Hello world!"
    assert pcm.stream_started["images"] is None


def test_chat_prompt_unauthorized() -> None:
    """A missing session cookie is rejected."""
    pcm = _FakePCM()
    client, _ = _client(pcm=pcm)
    resp = client.post(
        "/api/chats/chat-123/prompt",
        json={"prompt": "Hello world!"},
        headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 401


def test_chat_prompt_missing_prompt() -> None:
    pcm = _FakePCM()
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/chats/chat-123/prompt",
        json={"prompt": "  "},
        cookies=cookies,
        headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 400
    assert "prompt is required" in resp.json()["error"]


def test_chat_prompt_chat_not_found() -> None:
    pcm = _FakePCM(chat_exists=False)
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/chats/chat-123/prompt",
        json={"prompt": "Hello world!"},
        cookies=cookies,
        headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


def test_chat_prompt_archived_rejected_before_stream() -> None:
    """An archived chat returns 409 without starting a stream, so
    stream_chat never raises "Cannot send messages to an archived chat"
    from the background task."""
    pcm = _FakePCM(chat_exists=True, archived=True)
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/chats/chat-123/prompt",
        json={"prompt": "Hello world!"},
        cookies=cookies,
        headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 409
    assert resp.json()["archived"] is True
    assert pcm.stream_started is None


# ── POST /api/chats/{chat_id}/compact ───────────────────────────────────


class _CompactChat(_FakeChat):
    def __init__(self, chat_id, *, archived=False, session_id="native-session"):
        super().__init__(chat_id, archived=archived)
        self.session_id = session_id


class _FakeCompactPCM:
    def __init__(self, *, chat=None, active=None):
        self.chat = chat
        self.active = active
        self.stream_started = None

    def get_chat(self, chat_id):
        return self.chat

    def get_active_stream(self, chat_id):
        return self.active

    def start_stream(self, chat_id, prompt, images=None, compact=False):
        self.stream_started = {"chat_id": chat_id, "prompt": prompt, "compact": compact}
        return SimpleNamespace()


def _compact_client(pcm):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/compact", chat_compact, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.project_chat_manager = pcm
    client = TestClient(app, base_url=_ORIGIN)
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def _post_compact(pcm):
    client, cookies = _compact_client(pcm)
    return client.post(
        "/api/chats/chat-123/compact",
        cookies=cookies,
        headers={"Origin": _ORIGIN},
    )


def test_chat_compact_starts_a_compaction_run() -> None:
    pcm = _FakeCompactPCM(chat=_CompactChat("chat-123"))
    resp = _post_compact(pcm)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chat_id": "chat-123"}
    assert pcm.stream_started == {
        "chat_id": "chat-123",
        "prompt": "/compact",
        "compact": True,
    }


def test_chat_compact_unknown_chat_is_not_found() -> None:
    pcm = _FakeCompactPCM(chat=None)
    resp = _post_compact(pcm)
    assert resp.status_code == 404
    assert pcm.stream_started is None


def test_chat_compact_archived_chat_is_rejected() -> None:
    pcm = _FakeCompactPCM(chat=_CompactChat("chat-123", archived=True))
    resp = _post_compact(pcm)
    assert resp.status_code == 409
    assert resp.json()["archived"] is True
    assert pcm.stream_started is None


def test_chat_compact_refuses_while_a_turn_runs() -> None:
    busy = SimpleNamespace(background=False)
    pcm = _FakeCompactPCM(chat=_CompactChat("chat-123"), active=busy)
    resp = _post_compact(pcm)
    assert resp.status_code == 409
    assert resp.json() == {"error": "chat is busy"}
    assert pcm.stream_started is None


def test_chat_compact_replaces_a_between_turns_drain() -> None:
    drain = SimpleNamespace(background=True)
    pcm = _FakeCompactPCM(chat=_CompactChat("chat-123"), active=drain)
    resp = _post_compact(pcm)
    assert resp.status_code == 200
    assert pcm.stream_started["compact"] is True


def test_chat_compact_needs_a_provider_session() -> None:
    pcm = _FakeCompactPCM(chat=_CompactChat("chat-123", session_id=""))
    resp = _post_compact(pcm)
    assert resp.status_code == 409
    assert resp.json() == {"error": "nothing to compact yet"}
    assert pcm.stream_started is None
