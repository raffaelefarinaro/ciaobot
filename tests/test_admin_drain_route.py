from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web import auth
from ciao.web.routes_api import admin_drain, admin_drain_cancel


class _Manager:
    """The chat manager, with the one flag the drain routes read."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._restart_draining = False

    def active_chat_ids(self) -> list[str]:
        return ["c-1", "c-2"]

    @property
    def restart_draining(self) -> bool:
        return self._restart_draining

    def begin_restart_drain(self) -> None:
        self.calls.append("begin")
        self._restart_draining = True

    def cancel_restart_drain(self) -> None:
        self.calls.append("cancel")
        self._restart_draining = False


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
    # The caller polls by asking again rather than reading /api/active-chats,
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
