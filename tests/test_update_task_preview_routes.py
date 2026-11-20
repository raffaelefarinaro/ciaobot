import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import ciao
from ciao import update_task_preview
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.learning_records import LearningRecord, allocate_learning_id, render_learning
from ciao.update_task_catalog import TaskCatalog, UpdateTask, load_catalog
from ciao.web import routes_api, update_task_launch
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware, _PUBLIC_API

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


def test_preview_requires_workspace_and_supported_packaged_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(update_task_preview, "prepare", lambda *a, **kw: calls.append(a))
    client = client_for(config_for(tmp_path))
    response = client.get(URL)
    assert response.status_code == 400
    assert response.json() == {"error": "workspace is required"}
    response = client.get("/api/update-tasks/secret-sentinel/preview?workspace=personal")
    assert response.status_code == 409
    assert response.json() == {"error": "This update task cannot be previewed for this workspace."}
    monkeypatch.setattr(ciao, "__version__", "0.0.1")
    future = replace(load_catalog().by_id["learnings-cleanup"], since_version="999.0.0")
    monkeypatch.setattr(update_task_launch, "load_catalog", lambda: TaskCatalog(tasks=(future,)))
    response = client.get(URL + "?workspace=personal")
    assert response.status_code == 409
    assert response.json() == {"error": "This update task cannot be previewed for this workspace."}
    assert not calls


def test_preview_requires_signed_session_and_is_not_public(tmp_path: Path) -> None:
    client = client_for(config_for(tmp_path))
    assert URL not in _PUBLIC_API
    signed = client.headers.pop("Cookie")
    assert client.get(URL + "?workspace=personal").status_code == 401
    client.headers["Cookie"] = signed
    assert client.get(URL + "?workspace=personal").status_code == 200


def test_preview_uses_registered_workspace_and_returns_actual_adapter_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = client_for(config_for(tmp_path))
    actual_prepare = update_task_preview.prepare
    calls: list[tuple[str, int, str]] = []

    def spy(task: UpdateTask, *, config: CiaoConfig, workspace: str) -> dict[str, Any]:
        calls.append((task.id, task.revision, workspace))
        return actual_prepare(task, config=config, workspace=workspace)

    monkeypatch.setattr(update_task_preview, "prepare", spy)
    actual_planner = update_task_preview.learnings_cleanup.plan_cleanup
    planner_calls: list[tuple[Any, ...]] = []

    def planner_spy(*args: Any, **kwargs: Any) -> Any:
        planner_calls.append(args)
        return actual_planner(*args, **kwargs)

    monkeypatch.setattr(update_task_preview.learnings_cleanup, "plan_cleanup", planner_spy)
    response = client.get(URL + "?workspace=personal")
    data = response.json()
    assert response.status_code == 200
    assert set(data) == {
        "task_id", "revision", "workspace", "state", "summary", "plan", "checked_at"
    }
    assert calls == [("learnings-cleanup", data["revision"], "personal")]
    stamp = datetime.fromisoformat(data["checked_at"])
    assert stamp.utcoffset() == timedelta(0)
    assert len(planner_calls) == 1
    response = client.get(URL + "?workspace=missing")
    assert response.status_code == 409
    assert response.json() == {"error": "This update task cannot be previewed for this workspace."}
    assert len(planner_calls) == 1

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sensitive registry details")

    monkeypatch.setattr(CiaoConfig, "workspace_names", fail)
    response = client.get(URL + "?workspace=personal")
    assert response.status_code == 200
    assert response.json()["state"] == "unknown"
    assert response.json()["plan"] is None
    assert "sensitive registry details" not in response.text


def test_real_preview_changes_no_notes_receipts_state_or_chats(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    note = Path(config.workspace_vault_root("personal")) / "Workspace/Learnings.md"
    note.parent.mkdir(parents=True)
    record = LearningRecord(
        learning_id=allocate_learning_id("personal", "- [example] Keep the source."),
        key="example",
        text="Keep the source.",
    )
    note.write_text(
        "---\ntype: document\nupdated: 2026-01-01\ntags: [learnings]\n---\n"
        "# Learnings\n\n## Active\n\n"
        + render_learning(record)
        + "\n\n## Promoted / Resolved\n",
        encoding="utf-8",
    )

    def snapshot() -> dict[str, bytes | None]:
        return {
            p.relative_to(tmp_path).as_posix(): p.read_bytes() if p.is_file() else None
            for p in tmp_path.rglob("*")
        }

    before = snapshot()
    response = client_for(config).get(URL + "?workspace=personal")
    assert response.status_code == 200
    result = response.json()
    assert result["state"] == "review"
    assert {k: result["plan"]["counts"][k] for k in ("active", "remove", "keep", "conflict")} == {
        "active": 1,
        "remove": 0,
        "keep": 1,
        "conflict": 0,
    }
    assert snapshot() == before


def test_preview_unknown_preserves_null_plan_without_sensitive_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(task: UpdateTask, *, config: CiaoConfig, workspace: str) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "revision": task.revision,
            "workspace": workspace,
            "state": "unknown",
            "summary": "Could not prepare this task; no changes were made.",
            "plan": None,
        }

    monkeypatch.setattr(update_task_preview, "prepare", unknown)
    result = client_for(config_for(tmp_path)).get(URL + "?workspace=personal").json()
    assert result["state"] == "unknown" and result["plan"] is None
    assert "tasks" not in result


def test_real_app_registers_preview_route(tmp_path: Path) -> None:
    app = create_app(config_for(tmp_path))
    route = next(r for r in app.routes if isinstance(r, Route) and r.path == PATH)
    assert route.endpoint is routes_api.preview_update_task
    assert "GET" in route.methods


def test_preview_coalesces_and_does_not_block_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []
    entries: list[datetime] = []
    original_run_read = routes_api.run_read
    submitted = 0
    both_submitted = asyncio.Event()

    async def observe(key: str, operation: Callable[[], Any]) -> Any:
        nonlocal submitted
        submitted += 1
        if submitted == 2:
            both_submitted.set()
        return await original_run_read(key, operation)

    monkeypatch.setattr(routes_api, "run_read", observe)

    def slow(task: UpdateTask, *, config: CiaoConfig, workspace: str) -> dict[str, Any]:
        calls.append(threading.get_ident())
        entries.append(datetime.now(UTC))
        entered.set()
        assert release.wait(15)
        return {"state": "review", "plan": {}}

    monkeypatch.setattr(update_task_preview, "prepare", slow)
    client = client_for(config_for(tmp_path))

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://localhost:8443",
            headers=client.headers,
        ) as ac:
            first = asyncio.create_task(ac.get(URL + "?workspace=personal"))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                second = asyncio.create_task(ac.get(URL + "?workspace=personal"))
                await asyncio.wait_for(both_submitted.wait(), 10)
                assert not first.done() and not second.done()
                heartbeat = await asyncio.wait_for(ac.get("/api/test-heartbeat"), 10)
                assert heartbeat.status_code == 200
                assert heartbeat.json() == {"ok": True}
                assert not first.done() and not second.done()
            finally:
                release.set()
            a, b = await asyncio.gather(first, second)
            assert a.json() == b.json()
            assert datetime.fromisoformat(a.json()["checked_at"]) <= entries[0]
        assert calls and calls[0] != threading.get_ident()
        assert len(calls) == 1

    asyncio.run(run())


def test_fresh_preview_reads_after_invalidation_instead_of_joining_old_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("A")
    entered, release = threading.Event(), threading.Event()
    calls: list[str] = []

    def scan(task: UpdateTask, *, config: CiaoConfig, workspace: str) -> dict[str, Any]:
        value = fixture.read_text()
        calls.append(value)
        if value == "A":
            entered.set()
            assert release.wait(15)
        return {"state": "review", "plan": {"value": value}}

    monkeypatch.setattr(update_task_preview, "prepare", scan)
    client = client_for(config_for(tmp_path))

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://localhost:8443",
            headers=client.headers,
        ) as ac:
            first = asyncio.create_task(ac.get(URL + "?workspace=personal"))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                fixture.write_text("B")
                fresh = await asyncio.wait_for(ac.get(URL + "?workspace=personal&fresh=1"), 10)
                assert fresh.json()["plan"] == {"value": "B"}
            finally:
                release.set()
            assert (await first).json()["plan"] == {"value": "A"}
            for bad in ("", "true", "2", "-1"):
                response = await ac.get(URL, params={"workspace": "personal", "fresh": bad})
                assert response.status_code == 400
                assert response.json() == {"error": "fresh must be 0 or 1"}
            assert (await ac.get(URL + "?workspace=personal&fresh=0")).status_code == 200
        assert calls == ["A", "B", "B"]

    asyncio.run(run())


def test_preview_scopes_coalescing_by_workspace_install_and_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = load_catalog().by_id["learnings-cleanup"]
    revision = [task.revision]
    keys: list[str] = []
    monkeypatch.setattr(
        update_task_launch,
        "resolve_task",
        lambda *args, **kwargs: replace(task, revision=revision[0]),
    )

    async def spy(key: str, operation: Callable[[], Any]) -> dict[str, Any]:
        keys.append(key)
        return {"state": "unknown", "plan": None}

    monkeypatch.setattr(routes_api, "run_read", spy)
    a = client_for(config_for(tmp_path / "one"))
    b = client_for(config_for(tmp_path / "two"))
    for client, workspace in [(a, "personal"), (a, "work"), (b, "personal")]:
        assert client.get(URL, params={"workspace": workspace}).status_code == 200
    revision[0] += 1
    assert a.get(URL + "?workspace=personal").status_code == 200
    assert len(set(keys)) == 4
    assert keys[0] == (
        f"update-task-preview:{a.app.state.config.state_path.parent}:"
        f"personal:{task.id}@{task.revision}"
    )
    a.get(URL + "?workspace=personal&fresh=1&key=attacker")
    a.get(URL + "?workspace=personal&fresh=1&key=attacker")
    assert keys[-1] != keys[-2]
    assert "attacker" not in keys[-1]


def test_fresh_preview_respects_real_executor_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import async_reads

    class ObservedExecutor(async_reads._VaultReadExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1, max_backlog=2)
            self.second_admitted = asyncio.Event()
            self.waiting_for_slot = asyncio.Event()
            self.admissions = 0

        def acquire(
            self, key: str, operation: Callable[[], Any], *, coalesce: bool
        ) -> Any:
            future = super().acquire(key, operation, coalesce=coalesce)
            if future is not None:
                self.admissions += 1
                if self.admissions == 2:
                    self.second_admitted.set()
            return future

        async def wait_for_slot(self, loop: asyncio.AbstractEventLoop) -> None:
            self.waiting_for_slot.set()
            await super().wait_for_slot(loop)

    executor = ObservedExecutor()
    monkeypatch.setattr(async_reads, "_EXECUTOR", executor)
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def scan(task: UpdateTask, *, config: CiaoConfig, workspace: str) -> dict[str, Any]:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            entered.set()
            assert release.wait(30)
        return {"state": "review", "plan": {}}

    monkeypatch.setattr(update_task_preview, "prepare", scan)
    client = client_for(config_for(tmp_path))

    async def run() -> None:
        requests: list[asyncio.Task[httpx.Response]] = []
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://localhost:8443",
            headers=client.headers,
        ) as ac:
            try:
                requests.append(asyncio.create_task(ac.get(URL + "?workspace=personal&fresh=1")))
                assert await asyncio.to_thread(entered.wait, 10)
                requests.append(asyncio.create_task(ac.get(URL + "?workspace=personal&fresh=1")))
                await asyncio.wait_for(executor.second_admitted.wait(), 10)
                requests.append(asyncio.create_task(ac.get(URL + "?workspace=personal&fresh=1")))
                await asyncio.wait_for(executor.waiting_for_slot.wait(), 10)
                assert executor.outstanding() == executor.max_backlog == 2
                assert executor.admissions == 2
                assert calls == [1]
                assert all(not request.done() for request in requests)
            finally:
                release.set()
                results = await asyncio.gather(*requests, return_exceptions=True)
            assert len(results) == 3
            assert all(
                isinstance(result, httpx.Response) and result.status_code == 200
                for result in results
            )
            assert calls == [1, 2, 3]
            assert executor.admissions == 3

    try:
        asyncio.run(run())
    finally:
        release.set()
        executor.close()
