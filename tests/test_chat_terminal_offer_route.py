"""The browser dismissal of a chat's suggested terminal command (#1843).

The dismissal rides the route the PWA already has — the signed-session
`PATCH /api/chats/{id}` — with a body of exactly
`{"terminal_offer": {"expected_id": "<32 lowercase hex>"}}`. These tests
build a real `ProjectChatManager` and a real `TestClient` over the same
routes the PWA uses (the construction pattern from
`tests/test_chat_pins.py`) rather than stubbing the seam: a stub could
agree with itself about a field the engine never stored.

Half of the contract is what the route must *not* do. Dropping a
suggestion is not a privileged act: it needs no terminal grant, it opens
no provider session, it touches no host terminal and it runs nothing. The
offered command is text the user reads and copies; the dismissal is text
the user throws away.

The dismissal also has to be exact. A body that names an id the caller
never saw, a body that smuggles a second update along, a body that tries
to hand the engine a command of its own: none of them is a dismissal,
and none of them may change the chat.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import host_terminal
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
    project = manager.create_project("Offers", workspace="work")
    return manager.create_chat(project.project_id, title="Offered")


def _offered(manager: ProjectChatManager, command: str = "npm install -g ciao"):
    """A chat that carries a suggested command, and the payload of that offer."""
    chat = _one_chat(manager)
    payload = manager.set_terminal_offer(chat.chat_id, command)
    assert payload is not None
    return chat, payload


def _dismiss(
    client: TestClient,
    chat_id: str,
    expected_id: str,
):
    return client.patch(
        f"/api/chats/{chat_id}",
        json={"terminal_offer": {"expected_id": expected_id}},
        headers={"Origin": _ORIGIN},
    )


def _patch_raw(client: TestClient, chat_id: str, raw: bytes):
    """A PATCH whose body is sent verbatim, so the bytes are the test's."""
    return client.patch(
        f"/api/chats/{chat_id}",
        content=raw,
        headers={"Origin": _ORIGIN, "content-type": "application/json"},
    )


@contextmanager
def _record_events(manager: ProjectChatManager) -> Iterator[list[dict]]:
    """Collect the events the manager publishes while the block runs."""
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]
    try:
        yield published
    finally:
        hub.publish = original  # type: ignore[method-assign]


def _offer_events(published: list[dict]) -> list[dict]:
    return [e for e in published if e["type"] == "chat_terminal_offer_changed"]


# ── The happy path ─────────────────────────────────────────────────────────


def test_a_dismissal_clears_the_offer_and_returns_the_chat(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    with _record_events(manager) as published:
        response = _dismiss(client, chat.chat_id, payload["id"])

    assert response.status_code == 200
    body = response.json()
    # The full chat payload, with the field present and empty.
    assert body["chat_id"] == chat.chat_id
    assert body["title"] == "Offered"
    assert body["terminal_offer"] is None
    assert manager.get_chat(chat.chat_id).terminal_offer is None
    # The one event a clear publishes: the offer, now null.
    assert _offer_events(published) == [
        {
            "type": "chat_terminal_offer_changed",
            "chat_id": chat.chat_id,
            "terminal_offer": None,
        }
    ]


def test_a_repeated_dismissal_is_idempotent_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    """A retry of the same dismissal is a no-op, not a second event.

    The browser may retry a write it never saw the answer to. The second
    attempt finds no offer, changes nothing and says so with the same
    200 the first one gave.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    first = _dismiss(client, chat.chat_id, payload["id"])
    assert first.status_code == 200
    assert first.json()["terminal_offer"] is None

    with _record_events(manager) as published:
        again = _dismiss(client, chat.chat_id, payload["id"])

    assert again.status_code == 200
    assert again.json()["terminal_offer"] is None
    assert _offer_events(published) == []


def test_a_dismissal_of_a_chat_without_an_offer_is_a_no_op(tmp_path: Path) -> None:
    """Nothing to dismiss is still a successful dismissal."""
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    client = _make_client(manager)

    with _record_events(manager) as published:
        response = _dismiss(client, chat.chat_id, "0" * 32)

    assert response.status_code == 200
    assert response.json()["terminal_offer"] is None
    assert _offer_events(published) == []


# ── The conflict ───────────────────────────────────────────────────────────


def test_a_stale_id_is_a_conflict_and_the_offer_survives(tmp_path: Path) -> None:
    """A device that raced a newer offer is refused, not obeyed.

    The other device dismissed the old offer and the assistant offered a
    new command. This tab still holds the old id; quoting it must not
    remove the command the user is looking at now.
    """
    manager = _make_manager(tmp_path)
    chat, first = _offered(manager)
    client = _make_client(manager)

    stale = _dismiss(client, chat.chat_id, "0" * 32)

    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "terminal_offer_conflict"
    # The conflict carries the authoritative current offer, so the client
    # can re-apply against it.
    assert body["terminal_offer"] == first
    # And the offer is untouched.
    assert manager.get_chat(chat.chat_id).terminal_offer is not None
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == first


def test_a_dismissal_cannot_remove_a_command_offered_after_it(tmp_path: Path) -> None:
    """The stale id stays stale: a re-offer is a different offer."""
    manager = _make_manager(tmp_path)
    chat, first = _offered(manager)
    client = _make_client(manager)

    assert _dismiss(client, chat.chat_id, first["id"]).status_code == 200
    second = manager.set_terminal_offer(chat.chat_id, "brew install ciao")
    assert second is not None
    assert second["id"] != first["id"]

    late = _dismiss(client, chat.chat_id, first["id"])

    assert late.status_code == 409
    assert late.json()["terminal_offer"] == second
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == second


# ── Existence ──────────────────────────────────────────────────────────────


def test_an_unknown_chat_is_a_404(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    response = _dismiss(client, "chat-nope", "0" * 32)

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_a_malformed_body_is_refused_before_the_chat_is_looked_up(
    tmp_path: Path,
) -> None:
    """Shape first: an invalid body is a 400 even for a chat that is not there.

    The other order would let a caller probe which chat ids exist by
    sending a body the engine would have refused anyway.
    """
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    response = client.patch(
        "/api/chats/chat-nope",
        json={"terminal_offer": {"expected_id": "not-an-id"}},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 400


# ── The shape of the body ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"terminal_offer": {"expected_id": "0" * 32}, "title": "Renamed"},
                     id="with-title"),
        pytest.param({"terminal_offer": {"expected_id": "0" * 32}, "provider": "claude"},
                     id="with-provider"),
        pytest.param({"terminal_offer": {"expected_id": "0" * 32}, "mode": "plan"},
                     id="with-mode"),
        pytest.param({"terminal_offer": {"expected_id": "0" * 32},
                      "pin": {"path": "", "expected_revision": 0}},
                     id="with-pin"),
        pytest.param({"terminal_offer": {"expected_id": "0" * 32}, "code_mode": True},
                     id="with-code-mode"),
    ],
)
def test_a_dismissal_cannot_be_combined_with_another_update(
    tmp_path: Path, body: dict
) -> None:
    """A dismissal is its own body. It never brings a second change along.

    A combined write could land the other change and then fail, leaving
    the chat half-updated by a request that was refused.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json=body,
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 400
    # The refused write changed nothing at all.
    assert manager.get_chat(chat.chat_id).title == "Offered"
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload


@pytest.mark.parametrize(
    "expected_id",
    [
        pytest.param("", id="empty"),
        pytest.param(123, id="number"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="null"),
        pytest.param([], id="list"),
        pytest.param({"id": "0" * 32}, id="object"),
        pytest.param("0" * 31, id="too-short"),
        pytest.param("0" * 33, id="too-long"),
        pytest.param("A" * 32, id="uppercase"),
        pytest.param("g" * 32, id="not-hex"),
        pytest.param("0" * 31 + " ", id="trailing-space"),
        pytest.param("0" * 31 + "\n", id="trailing-newline"),
    ],
)
def test_an_expected_id_that_is_not_an_offer_id_is_refused(
    tmp_path: Path, expected_id: object
) -> None:
    """The id is exactly 32 lowercase hex characters, and nothing else.

    A bool is not read as a number and a number is not read as an id:
    the shape is checked, not coerced.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = _dismiss(client, chat.chat_id, expected_id)

    assert response.status_code == 400
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload


@pytest.mark.parametrize(
    "offer",
    [
        pytest.param("a string", id="not-an-object"),
        pytest.param([], id="a-list"),
        pytest.param(None, id="null"),
        pytest.param(7, id="a-number"),
    ],
)
def test_a_dismissal_body_that_is_not_an_object_is_refused(
    tmp_path: Path, offer: object
) -> None:
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"terminal_offer": offer},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 400
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload


def test_a_dismissal_may_not_carry_a_command_of_its_own(tmp_path: Path) -> None:
    """The body names an offer; it never supplies the text of one.

    A caller that could write the command would be a caller that could
    put arbitrary text on the user's card, and the engine would have no
    way to tell that text from the assistant's.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={
            "terminal_offer": {
                "expected_id": payload["id"],
                "command": "rm -rf ~",
            }
        },
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 400
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload


def test_a_refusal_never_echoes_the_body(tmp_path: Path) -> None:
    """The error says what is wrong, not what was sent.

    A body can carry a credential, a private path or a command. The
    refusal is a static string, so nothing the caller wrote can end up
    in a log line or a browser console.
    """
    manager = _make_manager(tmp_path)
    chat, _ = _offered(manager)
    client = _make_client(manager)
    secret = "hunter2-credential"

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"terminal_offer": {"expected_id": secret, "command": secret}},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 400
    assert secret not in response.text


@pytest.mark.parametrize(
    "placement, expected_error",
    [
        pytest.param(
            "top-level",
            "terminal_offer cannot be combined with other chat updates",
            id="unexpected-top-level-key",
        ),
        pytest.param(
            "nested",
            "terminal_offer has unknown keys (allowed: expected_id)",
            id="unexpected-nested-key",
        ),
    ],
)
def test_a_refusal_never_echoes_an_unknown_key_name(
    tmp_path: Path, monkeypatch, placement: str, expected_error: str
) -> None:
    """The refusal names the rule, not the key the caller invented.

    The privacy test above covers the *value*; this one covers the
    *name*. An unexpected key can be a credential in disguise, and a
    caller that sees its own key come back in the error has learned that
    the engine echoes what it was sent. The refusal is a static string,
    so neither the name nor the value ever leaves.

    The `expected_id` is the current offer's, so the only thing that can
    refuse this request is the shape: if the extra key were accepted,
    the offer would be dismissed instead.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)
    sentinel = "hunter2-credential"
    secret = "sk-live-2f9c-do-not-echo"

    if placement == "top-level":
        body = {
            "terminal_offer": {"expected_id": payload["id"]},
            sentinel: secret,
        }
    else:
        body = {
            "terminal_offer": {
                "expected_id": payload["id"],
                sentinel: secret,
            }
        }

    looked_up: list[str] = []
    original = manager.get_chat

    def _record(chat_id):
        looked_up.append(chat_id)
        return original(chat_id)

    monkeypatch.setattr(manager, "get_chat", _record)

    with _record_events(manager) as published:
        response = client.patch(
            f"/api/chats/{chat.chat_id}",
            json=body,
            headers={"Origin": _ORIGIN},
        )

    assert response.status_code == 400
    assert response.json() == {"error": expected_error}
    # Neither the key the caller invented nor its value comes back.
    assert sentinel not in response.text
    assert secret not in response.text
    # The offer is untouched, nothing was published, and the chat was
    # never read: a shape error is refused before the lookup.
    assert original(chat.chat_id).to_dict()["terminal_offer"] == payload
    assert _offer_events(published) == []
    assert looked_up == []


# ── The gate the route inherits ────────────────────────────────────────────


def test_an_unauthenticated_dismissal_is_refused(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager, signed_in=False)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"terminal_offer": {"expected_id": payload["id"]}},
        headers={"Origin": _ORIGIN},
    )

    assert response.status_code == 401
    assert manager.get_chat(chat.chat_id).terminal_offer is not None


def test_a_dismissal_from_a_foreign_origin_is_refused(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = client.patch(
        f"/api/chats/{chat.chat_id}",
        json={"terminal_offer": {"expected_id": payload["id"]}},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert manager.get_chat(chat.chat_id).terminal_offer is not None


# ── A body the route cannot read ───────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"{", id="truncated"),
        pytest.param(b'{"command": "hunter2-credential"', id="truncated-with-text"),
        pytest.param(b"", id="empty"),
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
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = _patch_raw(client, chat.chat_id, raw)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid JSON body"}
    assert "hunter2-credential" not in response.text
    # The chat and its offer are untouched.
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload
    assert manager.get_chat(chat.chat_id).title == "Offered"


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
    chat, payload = _offered(manager)
    client = _make_client(manager)

    response = _patch_raw(client, chat.chat_id, raw)

    assert response.status_code == 400
    assert response.json() == {"error": "body must be a JSON object"}
    # The chat and its offer are untouched.
    assert manager.get_chat(chat.chat_id).to_dict()["terminal_offer"] == payload
    assert manager.get_chat(chat.chat_id).title == "Offered"


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


# ── What a dismissal is not ────────────────────────────────────────────────


def test_a_dismissal_needs_no_grant_and_opens_nothing(tmp_path: Path,
                                                       monkeypatch) -> None:
    """Dropping a suggestion unlocks nothing and runs nothing.

    The dismissal is a read of one id and the forgetting of one string.
    It creates no provider session, opens no host terminal, executes no
    command and mints no grant.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)
    # The engine's own terminal grant store, so "no grant" is a fact
    # about the engine's state and not a guess from the response.
    grants = client.app.state.terminal_grants
    assert grants.retained_grant_count() == 0
    # A chat that has never run a turn has no provider session, and the
    # dismissal must not give it one.
    assert not manager.get_chat(chat.chat_id).session_id

    def _forbidden(*args, **kwargs):
        raise AssertionError("a dismissal must not reach the host terminal")

    monkeypatch.setattr(host_terminal, "launch", _forbidden)
    monkeypatch.setattr(host_terminal, "action_command", _forbidden)
    monkeypatch.setattr(host_terminal, "resolve_folder", _forbidden)
    monkeypatch.setattr(manager, "new_session", _forbidden)
    monkeypatch.setattr(manager, "start_stream", _forbidden)

    response = _dismiss(client, chat.chat_id, payload["id"])

    assert response.status_code == 200
    assert response.json()["terminal_offer"] is None
    # No provider session was created, and no grant was minted or needed.
    assert not manager.get_chat(chat.chat_id).session_id
    assert grants.retained_grant_count() == 0
    assert "ciao_terminal" not in response.cookies


# ── The field the client reads ─────────────────────────────────────────────


def test_the_chat_payload_always_carries_the_field(tmp_path: Path) -> None:
    """The field is present whether the chat has an offer or not.

    A client never has to guess whether the key is missing or empty, and
    the list and the detail agree.
    """
    manager = _make_manager(tmp_path)
    offered, payload = _offered(manager)
    plain = _one_chat(manager)
    client = _make_client(manager)

    detail = client.get(
        f"/api/chats/{offered.chat_id}", headers={"Origin": _ORIGIN}
    )
    assert detail.status_code == 200
    assert detail.json()["terminal_offer"] == payload

    listed = client.get("/api/chats", headers={"Origin": _ORIGIN})
    assert listed.status_code == 200
    rows = {row["chat_id"]: row for row in listed.json()}
    assert rows[offered.chat_id]["terminal_offer"] == payload
    assert rows[plain.chat_id]["terminal_offer"] is None


def test_the_dismissed_chat_reads_as_empty_everywhere(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)

    assert _dismiss(client, chat.chat_id, payload["id"]).status_code == 200

    detail = client.get(
        f"/api/chats/{chat.chat_id}", headers={"Origin": _ORIGIN}
    )
    assert detail.json()["terminal_offer"] is None
    listed = client.get("/api/chats", headers={"Origin": _ORIGIN})
    rows = {row["chat_id"]: row for row in listed.json()}
    assert rows[chat.chat_id]["terminal_offer"] is None


def test_a_dismissal_writes_nothing_to_the_registry(tmp_path: Path) -> None:
    """The dismissal route does not write the registry at all.

    The offer is transient, and so is the forgetting of it: the chat
    registry is a file that gets copied into backups, and a command can
    name a credential. The manager-level contract (that an offer and a
    dismissal are not registry mutations) is pinned by the parent
    suite; this is the route-level half — the HTTP request writes
    nothing, not even something unrelated.
    """
    manager = _make_manager(tmp_path)
    chat, payload = _offered(manager)
    client = _make_client(manager)
    registry = tmp_path / ".runtime" / "web_projects.json"
    before = registry.read_bytes()

    assert _dismiss(client, chat.chat_id, payload["id"]).status_code == 200

    assert registry.read_bytes() == before
