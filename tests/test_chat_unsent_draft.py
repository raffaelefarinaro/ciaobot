"""An unsent composer draft keeps a chat out of the empty-chat sweeps.

The draft text lives in the browser that typed it, so the server cannot see it.
But the server owns the emptiness rule — `user_turn_count` never reaches the
client — and three separate paths act on it: the `only_if_empty` delete behind
closing a chat, the cleanup `create_chat` runs, and the cleanup at startup.
Before `has_unsent_draft` existed, typing a prompt into a New Chat and then
creating another chat deleted the first one and stranded the draft in a
localStorage key nothing could reach again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import _DRAFT_CLAIM_TTL_S, ProjectChatManager
from ciao.web.routes_api import chat_detail, chat_draft_claim


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


def test_a_chat_with_an_unsent_draft_is_not_empty(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    assert manager.is_empty_chat(chat.chat_id) is True

    manager.set_draft_claim(chat.chat_id, "client-a", True)
    assert manager.is_empty_chat(chat.chat_id) is False

    manager.set_draft_claim(chat.chat_id, "client-a", False)
    assert manager.is_empty_chat(chat.chat_id) is True


def test_creating_another_chat_does_not_sweep_away_a_draft(tmp_path: Path) -> None:
    """The path a user actually hits: type, then start a second chat."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    drafted = manager.create_chat(project.project_id, title="New Chat")
    manager.set_draft_claim(drafted.chat_id, "client-a", True)

    manager.create_chat(project.project_id, title="New Chat")

    assert drafted.chat_id in manager._chats


def test_an_untouched_new_chat_is_still_swept(tmp_path: Path) -> None:
    """The flag must not turn the cleanup off for genuinely abandoned drafts."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    abandoned = manager.create_chat(project.project_id, title="New Chat")

    manager.create_chat(project.project_id, title="New Chat")

    assert abandoned.chat_id not in manager._chats


def test_the_draft_flag_survives_a_restart(tmp_path: Path) -> None:
    """The startup sweep runs against reloaded state, so the flag must persist."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    manager.set_draft_claim(chat.chat_id, "client-a", True)

    reloaded = _make_manager(tmp_path)

    assert reloaded._chats[chat.chat_id].draft_claims != {}
    assert reloaded.is_empty_chat(chat.chat_id) is False


def test_state_written_before_the_field_existed_loads_as_no_draft(
    tmp_path: Path,
) -> None:
    """Titled so the startup sweep keeps it: what is under test is the default
    the loader applies to state that predates the field, not the sweep."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="Kept by its title")
    manager.set_draft_claim(chat.chat_id, "client-a", True)

    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["draft_claims"]
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)

    assert reloaded._chats[chat.chat_id].draft_claims == {}


def test_setting_the_same_value_twice_is_idempotent(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    assert manager.set_draft_claim(chat.chat_id, "client-a", True) is not None
    assert manager.set_draft_claim(chat.chat_id, "client-a", True) is not None
    assert manager._chats[chat.chat_id].draft_claims != {}


def test_setting_a_draft_on_a_missing_chat_returns_none(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)

    assert manager.set_draft_claim("nope", "client-a", True) is None


def _make_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}", chat_detail, methods=["PATCH", "DELETE"]),
            Route(
                "/api/chats/{chat_id}/draft-claim",
                chat_draft_claim,
                methods=["POST"],
            ),
        ]
    )
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    return TestClient(app, raise_server_exceptions=False)


def test_claiming_then_releasing_round_trips_through_the_route(
    tmp_path: Path,
) -> None:
    """The round trip the composer actually performs."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    res = client.post(
        f"/api/chats/{chat.chat_id}/draft-claim",
        json={"client_id": "browser-1", "active": True},
    )
    assert res.status_code == 200
    assert res.json()["has_unsent_draft"] is True

    # Closing the chat must now leave it alone.
    res = client.delete(f"/api/chats/{chat.chat_id}?only_if_empty=1")
    assert res.json() == {"ok": False, "deleted": False, "reason": "not empty"}
    assert chat.chat_id in manager._chats

    # Clearing the composer hands it back to the sweep.
    client.post(
        f"/api/chats/{chat.chat_id}/draft-claim",
        json={"client_id": "browser-1", "active": False},
    )
    res = client.delete(f"/api/chats/{chat.chat_id}?only_if_empty=1")
    assert res.json()["deleted"] is True


def test_the_response_exposes_the_flag_not_the_claim_internals(
    tmp_path: Path,
) -> None:
    """One browser has no business seeing another's client id."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    payload = client.post(
        f"/api/chats/{chat.chat_id}/draft-claim",
        json={"client_id": "browser-1", "active": True},
    ).json()

    assert payload["has_unsent_draft"] is True
    assert "draft_claims" not in payload


def test_the_route_rejects_a_missing_client_or_non_boolean_active(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    assert client.post(
        f"/api/chats/{chat.chat_id}/draft-claim", json={"active": True}
    ).status_code == 400
    assert client.post(
        f"/api/chats/{chat.chat_id}/draft-claim",
        json={"client_id": "browser-1", "active": "yes"},
    ).status_code == 400
    assert manager._chats[chat.chat_id].draft_claims == {}


def test_an_unknown_chat_is_a_404(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    res = client.post(
        "/api/chats/nope/draft-claim", json={"client_id": "browser-1", "active": True}
    )

    assert res.status_code == 404


def test_one_client_releasing_leaves_another_clients_claim(tmp_path: Path) -> None:
    """The failure the shared boolean had: phone holds a draft, desktop types a
    character and deletes it, and the phone's prompt is swept away."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    manager.set_draft_claim(chat.chat_id, "phone", True)
    manager.set_draft_claim(chat.chat_id, "desktop", True)
    manager.set_draft_claim(chat.chat_id, "desktop", False)

    assert manager.is_empty_chat(chat.chat_id) is False

    manager.set_draft_claim(chat.chat_id, "phone", False)
    assert manager.is_empty_chat(chat.chat_id) is True


def test_a_stale_claim_stops_protecting_the_chat(tmp_path: Path) -> None:
    """The other failure: a browser that claimed and never came back must not
    pin an empty chat in the sidebar forever."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    long_ago = datetime.now(UTC) - timedelta(seconds=_DRAFT_CLAIM_TTL_S + 60)
    manager._chats[chat.chat_id].draft_claims = {"ghost": long_ago.isoformat()}

    assert manager.is_empty_chat(chat.chat_id) is True


def test_a_fresh_claim_survives_beside_a_stale_one(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    long_ago = datetime.now(UTC) - timedelta(seconds=_DRAFT_CLAIM_TTL_S + 60)
    manager._chats[chat.chat_id].draft_claims = {"ghost": long_ago.isoformat()}
    manager.set_draft_claim(chat.chat_id, "phone", True)

    assert manager.is_empty_chat(chat.chat_id) is False
    # Writing a claim also prunes the dead one, so the map cannot grow forever.
    assert list(manager._chats[chat.chat_id].draft_claims) == ["phone"]


def test_an_unparseable_timestamp_is_treated_as_stale(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    manager._chats[chat.chat_id].draft_claims = {"corrupt": "not-a-timestamp"}

    # Dropping it sweeps one chat early; keeping it would make the chat
    # unreclaimable forever.
    assert manager.is_empty_chat(chat.chat_id) is True


def test_sending_clears_every_claim(tmp_path: Path) -> None:
    """The server-authoritative half: once the text is a turn, no claim stands,
    even one made by a client that has since gone away."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    manager.set_draft_claim(chat.chat_id, "phone", True)
    manager.set_draft_claim(chat.chat_id, "desktop", True)

    async def drain() -> None:
        with contextlib.suppress(Exception):
            async for _ in manager.stream_chat(chat.chat_id, "hello"):
                break

    asyncio.run(drain())

    assert manager._chats[chat.chat_id].draft_claims == {}


def test_a_legacy_boolean_flag_becomes_a_claim(tmp_path: Path) -> None:
    """The first cut of this feature stored a boolean. Dropping it on upgrade
    would sweep away the very draft it was protecting."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["draft_claims"]
    data["chats"][chat.chat_id]["has_unsent_draft"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)

    assert reloaded.is_empty_chat(chat.chat_id) is False


def test_a_malformed_claims_value_degrades_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """_load catches only JSON and OS errors, so a bad shape here would take the
    engine down over one chat record."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="Kept by its title")

    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["chats"][chat.chat_id]["draft_claims"] = ["not", "a", "dict"]
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)

    assert reloaded._chats[chat.chat_id].draft_claims == {}


def test_reasserting_a_fresh_claim_does_not_rewrite_state(tmp_path: Path) -> None:
    """Clients re-assert on every open and wake; that must not rewrite the whole
    state file each time."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    manager.set_draft_claim(chat.chat_id, "browser-1", True)

    stamp = manager._chats[chat.chat_id].draft_claims["browser-1"]
    mtime = manager._path.stat().st_mtime_ns

    manager.set_draft_claim(chat.chat_id, "browser-1", True)

    assert manager._chats[chat.chat_id].draft_claims["browser-1"] == stamp
    assert manager._path.stat().st_mtime_ns == mtime


def test_an_ageing_claim_is_refreshed_on_reassertion(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    manager._chats[chat.chat_id].draft_claims = {"browser-1": two_days_ago.isoformat()}

    manager.set_draft_claim(chat.chat_id, "browser-1", True)

    refreshed = manager._chats[chat.chat_id].draft_claims["browser-1"]
    assert refreshed != two_days_ago.isoformat()
    assert manager.is_empty_chat(chat.chat_id) is False


def test_the_retired_patch_field_is_rejected(tmp_path: Path) -> None:
    """A client on the old bundle must fail loudly rather than believe it
    reported a draft that nothing applied."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")
    client = _make_client(manager)

    res = client.patch(f"/api/chats/{chat.chat_id}", json={"has_unsent_draft": True})

    assert res.status_code == 400
    assert "draft-claim" in res.json()["error"]


def test_a_legacy_claim_is_aged_from_the_chat_not_from_load(tmp_path: Path) -> None:
    """Dating it 'now' would hand a months-old abandoned draft a fresh 14 days
    of protection, and re-date it on every restart."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["draft_claims"]
    data["chats"][chat.chat_id]["has_unsent_draft"] = True
    data["chats"][chat.chat_id]["last_activity_at"] = long_ago
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)

    # Stale on arrival, so the startup sweep reclaims the row instead of the
    # chat lingering with a fresh 14 days of protection.
    assert chat.chat_id not in reloaded._chats


def test_any_client_release_retires_the_legacy_claim(tmp_path: Path) -> None:
    """No browser owns the migrated claim, so a real client reporting "nothing
    here" is the only thing that can ever retire it."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Drafts", workspace="work")
    chat = manager.create_chat(project.project_id, title="New Chat")

    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["draft_claims"]
    data["chats"][chat.chat_id]["has_unsent_draft"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _make_manager(tmp_path)
    assert reloaded.is_empty_chat(chat.chat_id) is False

    reloaded.set_draft_claim(chat.chat_id, "browser-1", False)

    assert reloaded.is_empty_chat(chat.chat_id) is True
