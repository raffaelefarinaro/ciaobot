"""Webhook trigger management routes: session-authenticated CRUD plus rotate.

Covers #1001 (child A2 of #974) against a real ``CiaoConfig`` over a tmp
runtime, behind the signed session cookie. Management routes only; the receiver
is ``routes_hooks.py``, so nothing in this surface is authorized by a bearer
secret.
"""

from __future__ import annotations

from pathlib import Path

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import archive_workspace_setting
from ciao.web.routes_webhooks import (
    webhook_create,
    webhook_delete,
    webhook_list,
    webhook_rotate,
    webhook_update,
)
from ciao.webhooks import WebhookStore


class _PCM:
    """Only what the archive route asks a chat manager: nothing is busy."""

    def workspace_scope(self, workspace: str) -> tuple[set[str], set[str]]:
        return set(), set()

    def workspace_busy_chat_ids(self, workspace: str) -> list[str]:
        return []


def _world(tmp_path: Path, monkeypatch) -> tuple[TestClient, dict[str, str], CiaoConfig]:
    """A session-guarded app over a real config with `personal` and `work`."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    reset_reroot_cache()
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    for name in ("personal", "work"):
        vault = Path(config.workspace_vault_root(name))
        (vault / "People").mkdir(parents=True, exist_ok=True)
        (vault / "People" / f"{name}-friend.md").write_text(
            f"---\ntype: person\n---\n# Friend of {name}\n",
            encoding="utf-8",
        )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            Route("/api/webhooks", webhook_list, methods=["GET"]),
            Route("/api/webhooks", webhook_create, methods=["POST"]),
            Route(
                "/api/webhooks/{trigger_id}/rotate",
                webhook_rotate,
                methods=["POST"],
            ),
            Route(
                "/api/webhooks/{trigger_id}", webhook_update, methods=["PATCH"]
            ),
            Route(
                "/api/webhooks/{trigger_id}", webhook_delete, methods=["DELETE"]
            ),
            Route(
                "/api/workspaces/{name}/archive",
                archive_workspace_setting,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.project_chat_manager = _PCM()
    client = TestClient(app)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}
    return client, cookies, config


def _store(config: CiaoConfig) -> WebhookStore:
    return WebhookStore(config.state_path.parent / "webhooks.json")


def _create(client: TestClient, cookies: dict[str, str]) -> tuple[dict, str]:
    resp = client.post(
        "/api/webhooks",
        json={
            "workspace": "personal",
            "name": "CI push",
            "instructions": "File the intake note",
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["trigger"], body["secret"]


def test_create_returns_secret_once_and_list_never_shows_it(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch)

    trigger, secret = _create(client, cookies)
    assert trigger["revision"] == 1
    assert trigger["enabled"] is False
    assert secret

    listed = client.get("/api/webhooks?workspace=personal", cookies=cookies)
    assert listed.status_code == 200
    rows = listed.json()["triggers"]
    assert [row["trigger_id"] for row in rows] == [trigger["trigger_id"]]
    assert rows[0] == trigger
    assert secret not in listed.text


def test_update_is_revision_checked(tmp_path: Path, monkeypatch) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch)
    trigger, _secret = _create(client, cookies)

    stale = client.patch(
        f"/api/webhooks/{trigger['trigger_id']}",
        json={"expected_revision": trigger["revision"] + 1, "name": "stale rename"},
        cookies=cookies,
    )
    assert stale.status_code == 409

    renamed = client.patch(
        f"/api/webhooks/{trigger['trigger_id']}",
        json={"expected_revision": trigger["revision"], "name": "CI push v2"},
        cookies=cookies,
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["trigger"]["name"] == "CI push v2"
    assert renamed.json()["trigger"]["revision"] == trigger["revision"] + 1

    missing = client.patch(
        f"/api/webhooks/{'0' * 32}",
        json={"expected_revision": 1, "name": "ghost"},
        cookies=cookies,
    )
    assert missing.status_code == 404


def test_rotate_invalidates_the_old_secret(tmp_path: Path, monkeypatch) -> None:
    client, cookies, config = _world(tmp_path, monkeypatch)
    trigger, secret = _create(client, cookies)
    store = _store(config)

    enabled = client.patch(
        f"/api/webhooks/{trigger['trigger_id']}",
        json={"expected_revision": trigger["revision"], "enabled": True},
        cookies=cookies,
    )
    assert enabled.status_code == 200
    assert store.authenticate(trigger["trigger_id"], secret) is not None

    rotated = client.post(
        f"/api/webhooks/{trigger['trigger_id']}/rotate",
        json={"expected_revision": enabled.json()["trigger"]["revision"]},
        cookies=cookies,
    )
    assert rotated.status_code == 200, rotated.text
    fresh = rotated.json()["secret"]
    assert fresh and fresh != secret

    assert store.authenticate(trigger["trigger_id"], secret) is None
    assert store.authenticate(trigger["trigger_id"], fresh) is not None


def test_delete_removes_the_trigger(tmp_path: Path, monkeypatch) -> None:
    client, cookies, config = _world(tmp_path, monkeypatch)
    trigger, secret = _create(client, cookies)
    store = _store(config)

    deleted = client.delete(
        f"/api/webhooks/{trigger['trigger_id']}?expected_revision={trigger['revision']}",
        cookies=cookies,
    )
    assert deleted.status_code == 204

    assert client.get("/api/webhooks?workspace=personal", cookies=cookies).json()[
        "triggers"
    ] == []
    assert store.authenticate(trigger["trigger_id"], secret) is None

    again = client.delete(
        f"/api/webhooks/{trigger['trigger_id']}?expected_revision={trigger['revision']}",
        cookies=cookies,
    )
    assert again.status_code == 404


def test_unknown_workspace_is_rejected(tmp_path: Path, monkeypatch) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch)

    assert (
        client.get("/api/webhooks?workspace=nope", cookies=cookies).status_code
        == 400
    )
    assert (
        client.get("/api/webhooks", cookies=cookies).status_code == 400
    )
    created = client.post(
        "/api/webhooks",
        json={
            "workspace": "nope",
            "name": "CI push",
            "instructions": "File the intake note",
        },
        cookies=cookies,
    )
    assert created.status_code == 400


def test_a_body_that_is_not_a_dict_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch)
    trigger, _secret = _create(client, cookies)

    assert client.post("/api/webhooks", json=["not", "a", "dict"], cookies=cookies).status_code == 400
    assert (
        client.patch(
            f"/api/webhooks/{trigger['trigger_id']}",
            json=["not", "a", "dict"],
            cookies=cookies,
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/webhooks/{trigger['trigger_id']}/rotate",
            json=["not", "a", "dict"],
            cookies=cookies,
        ).status_code
        == 400
    )


def test_archiving_a_workspace_revokes_its_triggers(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, config = _world(tmp_path, monkeypatch)
    store = _store(config)

    created = client.post(
        "/api/webhooks",
        json={
            "workspace": "work",
            "name": "Work intake",
            "instructions": "File the intake note",
        },
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    trigger, secret = created.json()["trigger"], created.json()["secret"]
    enabled = client.patch(
        f"/api/webhooks/{trigger['trigger_id']}",
        json={"expected_revision": trigger["revision"], "enabled": True},
        cookies=cookies,
    )
    assert enabled.status_code == 200
    assert store.authenticate(trigger["trigger_id"], secret) is not None

    archived = client.post("/api/workspaces/work/archive", cookies=cookies)
    assert archived.status_code == 200, archived.text

    assert store.authenticate(trigger["trigger_id"], secret) is None
    assert store.get(trigger["trigger_id"]).enabled is False


def test_restoring_the_name_cannot_reactivate_a_revoked_secret(
    tmp_path: Path, monkeypatch
) -> None:
    """The verifier is gone, so the archived workspace's name comes back empty."""
    client, cookies, config = _world(tmp_path, monkeypatch)
    store = _store(config)

    def _enabled(workspace: str) -> tuple[dict, str]:
        created = client.post(
            "/api/webhooks",
            json={
                "workspace": workspace,
                "name": f"{workspace} intake",
                "instructions": "File the intake note",
            },
            cookies=cookies,
        )
        assert created.status_code == 201, created.text
        trigger, secret = created.json()["trigger"], created.json()["secret"]
        enabled = client.patch(
            f"/api/webhooks/{trigger['trigger_id']}",
            json={"expected_revision": trigger["revision"], "enabled": True},
            cookies=cookies,
        )
        assert enabled.status_code == 200, enabled.text
        return trigger, secret

    work, work_secret = _enabled("work")
    personal, personal_secret = _enabled("personal")
    assert store.authenticate(work["trigger_id"], work_secret) is not None

    archived = client.post("/api/workspaces/work/archive", cookies=cookies)
    assert archived.status_code == 200, archived.text

    revoked = store.get(work["trigger_id"])
    assert store.authenticate(work["trigger_id"], work_secret) is None
    # Another workspace's triggers are not this archive's business.
    assert store.authenticate(personal["trigger_id"], personal_secret) is not None

    reenable = client.patch(
        f"/api/webhooks/{work['trigger_id']}",
        json={"expected_revision": revoked.revision, "enabled": True},
        cookies=cookies,
    )
    assert reenable.status_code == 400, reenable.text
    assert "rotate its secret before enabling it" in reenable.json()["error"]


def test_a_failed_revocation_refuses_the_archive(
    tmp_path: Path, monkeypatch
) -> None:
    """An archive that cannot destroy the verifiers is not an archive."""
    client, cookies, config = _world(tmp_path, monkeypatch)
    store = _store(config)
    vault = Path(config.workspace_vault_root("work"))
    assert vault.is_dir()

    created = client.post(
        "/api/webhooks",
        json={
            "workspace": "work",
            "name": "Work intake",
            "instructions": "File the intake note",
        },
        cookies=cookies,
    )
    assert created.status_code == 201, created.text
    trigger, secret = created.json()["trigger"], created.json()["secret"]
    enabled = client.patch(
        f"/api/webhooks/{trigger['trigger_id']}",
        json={"expected_revision": trigger["revision"], "enabled": True},
        cookies=cookies,
    )
    assert enabled.status_code == 200, enabled.text

    real_revoke = WebhookStore.revoke_workspace
    failing = {"on": True}

    def _disk_full(self: WebhookStore, workspace: str) -> int:
        if failing["on"]:
            raise OSError(28, "No space left on device")
        return real_revoke(self, workspace)

    monkeypatch.setattr(WebhookStore, "revoke_workspace", _disk_full)
    response = client.post("/api/workspaces/work/archive", cookies=cookies)

    assert response.status_code == 500, response.text
    assert "webhook triggers could not be revoked" in response.json()["error"]
    # Rolled back like every other failure after the move: still registered,
    # folder in place, and nothing archived - so the archive can be retried.
    assert "work" in config.workspaces
    assert vault.is_dir()
    # And the trigger it refused to revoke still authorizes, rather than the
    # archive having quietly claimed it did.
    assert store.authenticate(trigger["trigger_id"], secret) is not None

    failing["on"] = False
    retried = client.post("/api/workspaces/work/archive", cookies=cookies)
    assert retried.status_code == 200, retried.text
    assert store.authenticate(trigger["trigger_id"], secret) is None


def test_the_five_routes_need_the_session_cookie(
    tmp_path: Path, monkeypatch
) -> None:
    client, cookies, _config = _world(tmp_path, monkeypatch)
    # (verb, path, state-changing) for list, create, update, rotate, delete.
    calls = (
        ("get", "/api/webhooks?workspace=personal", False),
        ("post", "/api/webhooks", True),
        ("patch", f"/api/webhooks/{'0' * 32}", True),
        ("post", f"/api/webhooks/{'0' * 32}/rotate", True),
        ("delete", f"/api/webhooks/{'0' * 32}?expected_revision=1", True),
    )

    for verb, path, changes_state in calls:
        request = getattr(client, verb)
        # No cookie at all, and a cookie nobody signed.
        assert request(path).status_code == 401, path
        assert request(path, cookies={SESSION_COOKIE: "forged"}).status_code == 401, path
        if changes_state:
            # Signed, but asked for by a page on another site.
            cross = request(
                path, cookies=cookies, headers={"origin": "https://elsewhere.example"}
            )
            assert cross.status_code == 403, path
