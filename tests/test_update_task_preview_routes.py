from pathlib import Path

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web import routes_api
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware

PATH = "/api/update-tasks/{task_id}/preview"
URL = "/api/update-tasks/learnings-cleanup/preview"


def config_for(root: Path) -> CiaoConfig:
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=root,
        state_path=root / ".runtime/state.json",
        media_root=root / ".runtime/media",
        vault_root=root / "memory-vault",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    for name in ("personal", "work"):
        Path(config.workspace_vault_root(name)).mkdir(parents=True, exist_ok=True)
    return config


def client_for(config: CiaoConfig) -> TestClient:
    async def heartbeat(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route(PATH, routes_api.preview_update_task, methods=["GET"]),
            Route("/api/test-heartbeat", heartbeat, methods=["GET"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    client = TestClient(app, base_url="http://localhost:8443")
    cookie = serializer.dumps({"user": "owner"})
    client.headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
    return client


def test_real_app_registers_preview_route(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    route = next(r for r in app.routes if isinstance(r, Route) and r.path == PATH)
    assert route.endpoint is routes_api.preview_update_task
    assert "GET" in route.methods

