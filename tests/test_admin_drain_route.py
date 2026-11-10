from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import auth
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import admin_drain, admin_drain_cancel


class _Manager:
    """The chat manager, with the one flag the drain routes read."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._restart_draining = False
        self.owner = ""

    def active_chat_ids(self) -> list[str]:
        return ["c-1", "c-2"]

    @property
    def restart_draining(self) -> bool:
        return self._restart_draining

    @property
    def drain_owner(self) -> str:
        return self.owner

    def begin_restart_drain(self, owner: str = "") -> None:
        self.calls.append("begin")
        self._restart_draining = True
        self.owner = owner

    def cancel_restart_drain(self, owner: str | None = None) -> bool:
        self.calls.append("cancel")
        if owner is not None and owner != self.owner:
            return False
        self._restart_draining = False
        self.owner = ""
        return True


def _manager() -> _Manager:
    return _Manager()


def _restarting() -> _Manager:
    """A manager a Settings restart has already put into a drain."""
    pcm = _Manager()
    pcm.calls.append("restart-begin")
    pcm._restart_draining = True
    return pcm


def _client(pcm: object) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/admin/drain", admin_drain, methods=["POST"]),
            Route("/api/admin/drain/cancel", admin_drain_cancel, methods=["POST"]),
        ]
    )
    app.state.project_chat_manager = pcm
    # The real caller addresses the engine as `localhost`; the routes refuse
    # any other Host, and TestClient's own default is `testserver`.
    return TestClient(app, base_url="http://localhost")


def test_drain_closes_admission_and_reports_active_chats() -> None:
    pcm = _manager()
    client = _client(pcm)

    resp = client.post("/api/admin/drain")

    assert resp.status_code == 200
    assert resp.json() == {"draining": True, "active_chat_ids": ["c-1", "c-2"]}
    # The caller polls by asking again rather than reading a separate chat list,
    # so the drain has to have actually happened rather than merely be
    # acknowledged — and asking again must stay idempotent.
    again = client.post("/api/admin/drain")
    assert again.status_code == 200
    assert again.json() == {"draining": True, "active_chat_ids": ["c-1", "c-2"]}
    assert pcm.calls == ["begin", "begin"]
    assert pcm.restart_draining is True


def test_drain_cancel_reopens_admission() -> None:
    pcm = _manager()
    client = _client(pcm)
    client.post("/api/admin/drain")

    resp = client.post("/api/admin/drain/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"draining": False}
    assert pcm.calls == ["begin", "cancel"]
    assert pcm.restart_draining is False


def test_drain_without_manager_is_a_clear_failure() -> None:
    resp = _client(pcm=None).post("/api/admin/drain")

    # 503 rather than a silent success: the updater's foreground half must not
    # think admission is closed when there is no chat manager to close it on.
    assert resp.status_code == 503


# The updater runs `ciao update apply` from a terminal, before any PWA session
# exists, and it has to be refused from anywhere but this machine. Both facts
# live in `_LOOPBACK_ONLY_API`: in that set, a request is let through only when
# the peer is loopback, and the session check below is never reached.
def test_drain_routes_are_loopback_only_and_session_free() -> None:
    for path in ("/api/admin/drain", "/api/admin/drain/cancel"):
        assert path in auth._LOOPBACK_ONLY_API
        # Public would mean "from anywhere, no session" — the opposite promise.
        assert path not in auth._PUBLIC_API


# A loopback peer whose Host is a name it does not serve is a rebound page: the
# name is the one part of the request the attacker controls, and these routes
# need no session, so without this a page that rebinds DNS to 127.0.0.1 can
# close admission on the operator's engine indefinitely.
def test_drain_routes_refuse_a_foreign_host() -> None:
    pcm = _manager()
    client = _client(pcm)

    for path in ("/api/admin/drain", "/api/admin/drain/cancel"):
        resp = client.post(path, headers={"Host": "evil.example"})
        assert resp.status_code == 403, path
        assert resp.json() == {"error": "forbidden host"}

    # Nothing was changed on the way through.
    assert pcm.calls == []
    assert pcm.restart_draining is False


def test_drain_refuses_while_a_settings_restart_is_draining() -> None:
    pcm = _restarting()
    client = _client(pcm)

    resp = client.post("/api/admin/drain")

    # 409: taking the shared flag over would let the update's cancel reopen
    # admission under a restart that is still waiting for its chats.
    assert resp.status_code == 409
    assert resp.json() == {"error": "a restart is already draining"}
    # The restart's own drain is untouched.
    assert pcm.calls == ["restart-begin"]
    assert pcm.restart_draining is True


def test_drain_cancel_leaves_a_settings_restart_alone() -> None:
    pcm = _restarting()
    client = _client(pcm)

    resp = client.post("/api/admin/drain/cancel")

    # Not the update's drain to cancel: reported as still draining, and left
    # that way.
    assert resp.status_code == 200
    assert resp.json() == {"draining": True}
    assert pcm.calls == ["restart-begin"]
    assert pcm.restart_draining is True


# ── The engine-owned claim ──────────────────────────────────────────────
#
# An automatic update closes admission with `claim_for_update`, which takes the
# idle snapshot and the close as one synchronous step. These tests run the real
# manager, because the claim's idle proof and the refusal the route answers for
# a competing caller are the behaviour under test.


def _real_manager(tmp_path: Path) -> ProjectChatManager:
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


def test_auto_claim_is_atomic_against_new_turn(tmp_path: Path) -> None:
    pcm = _real_manager(tmp_path)
    client = _client(pcm)

    # An idle engine is claimed in one synchronous call: the close *is* the
    # claim, so no coroutine can see an open gate after the snapshot.
    assert pcm.claim_for_update("auto-1") is True
    assert pcm.restart_draining is True

    # A manual drain cannot take the claim. It gets the same 409 a Settings
    # restart gets, and admission stays exactly as the claim left it.
    resp = client.post("/api/admin/drain")
    assert resp.status_code == 409
    assert resp.json() == {"error": "a restart is already draining"}
    assert pcm.restart_draining is True

    # Released by its owner, admission reopens and a drain succeeds: the claim
    # never leaked past the operation that made it.
    assert pcm.cancel_restart_drain("auto-1") is True
    assert pcm.restart_draining is False
    resp = client.post("/api/admin/drain")
    assert resp.status_code == 200
    assert resp.json() == {"draining": True, "active_chat_ids": []}
    # A body that is not JSON is "no owner", not a refusal: the manual caller
    # never sent one and must keep working.
    assert client.post("/api/admin/drain", content=b"{oops").status_code == 200


def test_auto_claim_owner_cannot_be_stolen_or_cancelled(tmp_path: Path) -> None:
    pcm = _real_manager(tmp_path)
    client = _client(pcm)

    # The update's own drain names the operation that owns it.
    resp = client.post("/api/admin/drain", json={"owner": "auto-7"})
    assert resp.status_code == 200
    assert pcm.restart_draining is True
    assert pcm._drain_owner == "auto-7"

    # Another caller's cancel is refused and changes nothing: reopening here
    # would drop a claim newer work is waiting on.
    resp = client.post("/api/admin/drain/cancel", json={"owner": "someone-else"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "drain owned by another operation"}
    assert pcm.restart_draining is True
    assert pcm._drain_owner == "auto-7"

    # The owner reopens it.
    resp = client.post("/api/admin/drain/cancel", json={"owner": "auto-7"})
    assert resp.status_code == 200
    assert resp.json() == {"draining": False}
    assert pcm.restart_draining is False
    assert pcm._drain_owner == ""


def test_bodyless_cancel_cannot_reopen_an_owned_claim(tmp_path: Path) -> None:
    """The manual cancel has no owner, and an owned claim is not its to reopen.

    A bodyless cancel is what the operator path sends; during a claimed update
    it must be the 409 the mismatch already earns, not a silent steal.
    """
    pcm = _real_manager(tmp_path)
    client = _client(pcm)

    resp = client.post("/api/admin/drain", json={"owner": "auto-9"})
    assert resp.status_code == 200
    assert pcm.restart_draining is True
    assert pcm.drain_owner == "auto-9"

    resp = client.post("/api/admin/drain/cancel")
    assert resp.status_code == 409
    assert resp.json() == {"error": "drain owned by another operation"}
    assert pcm.restart_draining is True
    assert pcm.drain_owner == "auto-9"

    # A body that is not JSON is still "no owner", so it cannot reopen either.
    assert client.post("/api/admin/drain/cancel", content=b"{oops").status_code == 409
    assert pcm.restart_draining is True

    resp = client.post("/api/admin/drain/cancel", json={"owner": "auto-9"})
    assert resp.status_code == 200
    assert resp.json() == {"draining": False}
    assert pcm.restart_draining is False
