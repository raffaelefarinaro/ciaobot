from types import SimpleNamespace

from itsdangerous import URLSafeTimedSerializer
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import admin_restart, local_status


def test_restart_requires_auth_and_calls_drain_hook_without_a_checkout():
    serializer = URLSafeTimedSerializer("restart-test")
    app = Starlette(
        routes=[Route("/api/admin/restart", admin_restart, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = SimpleNamespace(
        restart_exit_code=75, pwa_auth_required=True, pwa_auth_token="restart-test",
    )
    calls = []
    app.state.request_restart = calls.append
    with TestClient(app, base_url="https://ciao.example") as client:
        assert client.post("/api/admin/restart").status_code == 401
        assert calls == []
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
        response = client.post("/api/admin/restart", headers={"Origin": "https://ciao.example"})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert calls == [75]
        del app.state.request_restart
        assert client.post("/api/admin/restart", headers={"Origin": "https://ciao.example"}).status_code == 503


async def test_restart_hook_runs_only_after_response_is_sent():
    calls = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(restart_exit_code=42), request_restart=calls.append,
    )))
    response = await admin_restart(request)
    assert calls == []
    await response.background()
    assert calls == [42]


@pytest.mark.parametrize("platform,dev_mode,expected", [
    ("linux", False, True), ("linux", True, False), ("darwin", False, False),
])
async def test_local_status_advertises_linux_restart_only(monkeypatch, platform, dev_mode, expected):
    import json
    import sys

    monkeypatch.delenv("CIAO_BUNDLED_APP", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        local_session_manager=SimpleNamespace(status=lambda: {"dev_mode": dev_mode}),
    )))
    response = await local_status(request)
    assert json.loads(response.body)["restart_only"] is expected


def _checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
    return repo


async def _status_restart_only(monkeypatch, *, platform, dev_mode, app_repo, bundled):
    import json
    import sys

    if bundled:
        monkeypatch.setenv("CIAO_BUNDLED_APP", "1")
    else:
        monkeypatch.delenv("CIAO_BUNDLED_APP", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(app_repo=str(app_repo)),
        local_session_manager=SimpleNamespace(status=lambda: {"dev_mode": dev_mode}),
    )))
    response = await local_status(request)
    return json.loads(response.body)["restart_only"]


@pytest.mark.parametrize("dev_mode", [False, True])
async def test_bundled_mac_app_is_restart_only(tmp_path, monkeypatch, dev_mode):
    # Even with CIAO_APP_REPO naming a real checkout: pip cannot replace the
    # embedded runtime, so redeploy can never succeed from a packaged app.
    assert await _status_restart_only(
        monkeypatch, platform="darwin", dev_mode=dev_mode,
        app_repo=_checkout(tmp_path), bundled=True,
    ) is True


async def test_mac_dev_checkout_keeps_redeploy(tmp_path, monkeypatch):
    assert await _status_restart_only(
        monkeypatch, platform="darwin", dev_mode=True,
        app_repo=_checkout(tmp_path), bundled=False,
    ) is False


async def test_mac_install_without_checkout_is_restart_only(tmp_path, monkeypatch):
    assert await _status_restart_only(
        monkeypatch, platform="darwin", dev_mode=False,
        app_repo=tmp_path / "site-packages", bundled=False,
    ) is True


async def test_linux_dev_mode_without_checkout_is_restart_only(tmp_path, monkeypatch):
    # Deploy would only stop at "locate checkout", so offer the restart.
    assert await _status_restart_only(
        monkeypatch, platform="linux", dev_mode=True,
        app_repo=tmp_path / "site-packages", bundled=False,
    ) is True


async def test_linux_dev_checkout_keeps_redeploy(tmp_path, monkeypatch):
    assert await _status_restart_only(
        monkeypatch, platform="linux", dev_mode=True,
        app_repo=_checkout(tmp_path), bundled=False,
    ) is False


async def test_deploy_refuses_bundled_app_before_any_step(tmp_path, monkeypatch):
    import json

    from ciao.web import routes_api

    monkeypatch.setenv("CIAO_BUNDLED_APP", "1")

    async def must_not_run(*args, **kwargs):
        raise AssertionError("deploy steps must not run on a bundled app")

    monkeypatch.setattr(routes_api, "_commit_and_push", must_not_run)
    monkeypatch.setattr(routes_api, "_git_pull_with_retry", must_not_run)

    class Manager:
        async def preflight(self):
            raise AssertionError("preflight must not run on a bundled app")

    calls: list = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        config=SimpleNamespace(
            app_repo=str(_checkout(tmp_path)), workspace_root=tmp_path, restart_exit_code=75,
        ),
        local_session_manager=Manager(),
        request_restart=calls.append,
    )))
    response = await routes_api.admin_deploy(request)
    payload = json.loads(response.body)
    assert response.status_code == 400
    assert payload["ok"] is False
    assert payload["steps"] == []
    assert "Ciaobot.app" in payload["error"] and "Restart" in payload["error"]
    assert calls == []
