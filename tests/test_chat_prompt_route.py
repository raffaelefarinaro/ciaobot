from __future__ import annotations

from types import SimpleNamespace
import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import chat_prompt

_ORIGIN = "https://ciao.example"


class _FakeChat:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.title = "Test Chat"


class _FakePCM:
    def __init__(self, chat_exists=True):
        self.chat_exists = chat_exists
        self.stream_started = None
        self.resolved_images = []

    def get_chat(self, chat_id):
        if self.chat_exists:
            return _FakeChat(chat_id)
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


def _client(*, pcm=None, auth_required: bool = False):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/prompt", chat_prompt, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer, auth_required=auth_required)],
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
    """With auth enabled (non-default since auth-off-by-default), a missing
    session cookie is rejected."""
    pcm = _FakePCM()
    client, _ = _client(pcm=pcm, auth_required=True)
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
