"""The chat PATCH body checks every update shares, and the retired terminal-offer body (#2581).

The PATCH on `/api/chats/{id}` reads named fields off a JSON object. These
tests build a real `ProjectChatManager` and a real `TestClient` over the same
routes the PWA uses, rather than stubbing the seam: a stub could agree with
itself about a field the engine never stored.

The `{"terminal_offer": ...}` body that dismissed a suggested command is gone.
It is no longer special: it falls through to the ordinary update, which has
no field to change, so it is a no-op and the chat is returned unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.terminal_auth import TerminalGrantStore
from ciao.transcripts import TranscriptStore
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_detail, list_all_chats

_ORIGIN = "https://ciao.example"


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _make_client(
    manager: ProjectChatManager, *, signed_in: bool = True
) -> TestClient:
    """The real route behind the real session gate, as the browser sees it."""
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/chats", list_all_chats, methods=["GET"]),
            Route(
                "/api/chats/{chat_id}",
                chat_detail,
                methods=["GET", "PATCH"],
            ),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = manager._config
    app.state.project_chat_manager = manager
    # The real terminal grant store, wired the way `ciao/web/app.py` wires
    # it, so a test can see whether a request minted or needed a grant.
    app.state.terminal_grants = TerminalGrantStore()
    client = TestClient(app, base_url=_ORIGIN, raise_server_exceptions=False)
    if signed_in:
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


def _one_chat(manager: ProjectChatManager):
    project = manager.create_project("Patch body", workspace="work")
    return manager.create_chat(project.project_id, title="Original")


def _patch_raw(client: TestClient, chat_id: str, raw: bytes):
    """A PATCH whose body is sent verbatim, so the bytes are the test's."""
    return client.patch(
        f"/api/chats/{chat_id}",
        content=raw,
        headers={"Origin": _ORIGIN, "content-type": "application/json"},
    )


# ── The shared body checks ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{", id="truncated"),
        pytest.param(b'{"command": "hunter2-credential"', id="truncated-with-text"),
        pytest.param(b"", id="empty"),
        # A byte that is not text: the decoder fails on the *encoding*,
        # not on the syntax, and that failure is a `UnicodeDecodeError` —
        # a `ValueError`, but not a `JSONDecodeError`. A body like
        # `b"\xff\xfe"` would not test that at all: it is a UTF-16LE BOM,
        # so the decoder reads it as an empty document and fails on the
        # syntax instead.
        pytest.param(b"\xff", id="not-utf8"),
    ],
)
def test_a_body_that_is_not_json_is_refused(tmp_path: Path, raw: bytes) -> None:
    """A body that is not JSON is a 400, not a crash.

    The route reads the body, so the body has to be a body. Malformed
    JSON is refused with a static message instead of reaching the handler
    as an exception, and the refusal never repeats what was sent: a body
    can carry a command or a credential.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    response = _patch_raw(client, chat.chat_id, raw)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid JSON body"}
    assert "hunter2-credential" not in response.text
    assert manager.get_chat(chat.chat_id).title == "Original"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"[]", id="array"),
        pytest.param(b"null", id="null"),
        pytest.param(b'"a string"', id="string"),
        pytest.param(b"123", id="number"),
        pytest.param(b"true", id="bool"),
    ],
)
def test_a_body_that_is_not_an_object_is_refused(tmp_path: Path, raw: bytes) -> None:
    """JSON that is not an object is a 400, not a crash.

    A chat update is a set of named fields. A body that is a list or a
    string has no fields, and reading them off it is what used to turn
    such a request into a 500.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    response = _patch_raw(client, chat.chat_id, raw)

    assert response.status_code == 400
    assert response.json() == {"error": "body must be a JSON object"}
    assert manager.get_chat(chat.chat_id).title == "Original"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{", id="truncated"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"null", id="null"),
        pytest.param(b'"a string"', id="string"),
        pytest.param(b"123", id="number"),
        pytest.param(b"true", id="bool"),
    ],
)
def test_a_refused_body_is_refused_before_the_chat_is_looked_up(
    tmp_path: Path, monkeypatch, raw: bytes
) -> None:
    """A body the route cannot read is refused without reading anything.

    The chat is never looked up, so a request the engine would refuse
    anyway cannot be used to find out which chat ids exist — the refusal
    is 400 whether the chat is there or not.
    """
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    looked_up: list[str] = []
    original = manager.get_chat

    def _record(chat_id):
        looked_up.append(chat_id)
        return original(chat_id)

    monkeypatch.setattr(manager, "get_chat", _record)

    response = _patch_raw(client, "chat-nope", raw)

    assert response.status_code == 400
    assert response.json()["error"] in {
        "invalid JSON body",
        "body must be a JSON object",
    }
    assert looked_up == []


def test_an_ordinary_chat_update_still_works(tmp_path: Path) -> None:
    """The guard is on the body, not on the update: a plain PATCH is unchanged."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"title": "Renamed"},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"
    assert manager.get_chat(chat.chat_id).title == "Renamed"


# ── The retired terminal-offer body ────────────────────────────────────────


def test_a_terminal_offer_body_is_no_longer_special(tmp_path: Path) -> None:
    """The old dismissal body is an unknown key now: an ordinary no-op update.

    No field is read off it, so every update field is `None`, the chat is
    returned as it was, and nothing is written.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"terminal_offer": {"expected_id": "a" * 32}},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 200
    assert "terminal_offer" not in response.json()
    assert response.json()["title"] == "Original"
    assert manager.get_chat(chat.chat_id).title == "Original"


def test_a_legacy_terminal_offer_key_in_the_registry_is_ignored(tmp_path: Path) -> None:
    """A registry row written before the offer was removed still loads.

    The loader builds each chat from named fields and ignores the rest, so
    a leftover `terminal_offer` key in an old registry is not an error and
    does not come back as a field on the chat.
    """
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    registry = tmp_path / ".runtime" / "web_projects.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["chats"][chat.chat_id]["terminal_offer"] = {
        "id": "ab" * 16,
        "command": "rm -rf /",
    }
    registry.write_text(json.dumps(payload), encoding="utf-8")

    restored = ProjectChatManager(
        manager._config,
        state_store=StateStore(
            manager._config.state_path,
            tmp_path,
            manager._config.media_root,
        ),
        transcript_store=TranscriptStore(
            tmp_path / ".runtime", tmp_path / "transcripts"
        ),
        path=manager._path,
    )

    loaded = restored.get_chat(chat.chat_id)
    assert loaded is not None
    assert loaded.title == "Original"
    assert "terminal_offer" not in loaded.to_dict()

