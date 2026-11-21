"""`/api/care/cycle`: the Settings dry run and host-only switch to the weekly care cycle (#3061).

Real ``AuthMiddleware`` and real handlers over a real B1 vault under tmp_path.
The library is never mocked except where a spy checks what it is handed.
Nothing touches the live engine, a real vault or a LaunchAgent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import curation_cycle, curation_run
from ciao.web import auth
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_api import care_cycle_apply, care_cycle_status

SECRET = "test-secret"
BASE = "http://localhost:8443"
PATH = "/api/care/cycle"
WORKSPACE = "personal"
MARKER = b"---\nlast_full_pass: 2026-11-09\n---\n\n# Curation log\n"
BACKUP = "Workspace/Curation-State.v1.bak"


def _v1_bytes() -> bytes:
    body = {
        "version": 1,
        "done_keys": {},
        "lease": {},
        "last_run": {},
    }
    return (json.dumps(body, indent=2) + "\n").encode("utf-8")


def _vault(tmp_path: Path, *, state: bytes | None = None) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / curation_run.CURATION_LOG_RELATIVE).write_bytes(MARKER)
    if state is None:
        state = _v1_bytes()
    curation_run.state_path(vault).write_bytes(state)
    return vault


def _live_lease_state() -> bytes:
    now = datetime.now(UTC)
    body = {
        "version": 1,
        "done_keys": {},
        "lease": {
            "holder": "a care run",
            "started_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expires_at": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "last_run": {},
    }
    return (json.dumps(body) + "\n").encode("utf-8")


def _client(
    vault: Path,
    *,
    peer: str = "127.0.0.1",
    workspace: str = WORKSPACE,
) -> TestClient:
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route(PATH, care_cycle_status, methods=["GET"]),
            Route(PATH, care_cycle_apply, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace=lambda name: object() if name == workspace else None,
        workspace_vault_root=lambda name: vault,
    )
    return TestClient(app, base_url=BASE, client=(peer, 50000))


def _signed(client: TestClient, origin: str = BASE) -> dict[str, str]:
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": origin}


def _digest(client: TestClient) -> str:
    response = client.get(f"{PATH}?workspace={WORKSPACE}", headers=_signed(client))
    assert response.status_code == 200, response.text
    return str(response.json()["digest"])


def _apply_body(digest: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"workspace": WORKSPACE, "confirm_quiesced": True, "digest": digest}
    body.update(changes)
    return body


def _state(vault: Path) -> bytes:
    return curation_run.state_path(vault).read_bytes()


# --- the dry run -----------------------------------------------------------


def test_status_requires_the_signed_session(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    assert client.get(f"{PATH}?workspace={WORKSPACE}").status_code == 401
    assert client.post(PATH, json=_apply_body("x"), headers={"Origin": BASE}).status_code == 401


def test_status_unknown_or_missing_workspace(tmp_path: Path) -> None:
    client = _client(_vault(tmp_path))
    assert client.get(PATH, headers=_signed(client)).status_code == 400
    response = client.get(f"{PATH}?workspace=nope", headers=_signed(client))
    assert response.status_code == 404


def test_status_on_host_returns_the_preview_and_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    before = sorted(p.relative_to(vault) for p in vault.rglob("*"))
    client = _client(vault)
    response = client.get(f"{PATH}?workspace={WORKSPACE}", headers=_signed(client))
    assert response.status_code == 200
    body = response.json()
    assert body["on_host"] is True
    assert body["workspace"] == WORKSPACE
    assert body["status"] == "v1"
    assert body["lease_live"] is False
    assert body["backup_path"].endswith(BACKUP)
    assert body["blockers"] == []
    assert sorted(p.relative_to(vault) for p in vault.rglob("*")) == before


def test_status_from_a_remote_browser_gets_no_report(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault, peer="100.64.0.7")
    response = client.get(f"{PATH}?workspace={WORKSPACE}", headers=_signed(client))
    assert response.status_code == 200
    body = response.json()
    assert body["on_host"] is False
    assert body["reason"] == "not_on_host"
    assert "digest" not in body
    assert "backup_path" not in body


def test_status_reports_a_live_lease(tmp_path: Path) -> None:
    vault = _vault(tmp_path, state=_live_lease_state())
    client = _client(vault)
    body = client.get(f"{PATH}?workspace={WORKSPACE}", headers=_signed(client)).json()
    assert body["lease_live"] is True
    assert body["live_lease"]["holder"] == "a care run"
    assert any("live curation lease" in blocker for blocker in body["blockers"])


# --- the switch ------------------------------------------------------------


def test_apply_from_a_remote_browser_is_403_and_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    before = _state(vault)
    host = _client(vault)
    digest = _digest(host)
    remote = _client(vault, peer="100.64.0.7")
    response = remote.post(PATH, json=_apply_body(digest), headers=_signed(remote))
    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert _state(vault) == before
    assert not (vault / BACKUP).exists()


@pytest.mark.parametrize(
    "body",
    [
        {"workspace": WORKSPACE, "confirm_quiesced": True},
        {"workspace": WORKSPACE, "confirm_quiesced": True, "digest": "", "extra": 1},
        {"workspace": WORKSPACE, "confirm_quiesced": "true", "digest": "d"},
        {"workspace": WORKSPACE, "confirm_quiesced": False, "digest": "d"},
        {"workspace": WORKSPACE, "confirm_quiesced": 1, "digest": "d"},
        {"workspace": "", "confirm_quiesced": True, "digest": "d"},
        {"workspace": WORKSPACE, "confirm_quiesced": True, "digest": 7},
        ["not", "an", "object"],
    ],
)
def test_apply_rejects_bodies_that_are_not_exact(tmp_path: Path, body: Any) -> None:
    vault = _vault(tmp_path)
    before = _state(vault)
    client = _client(vault)
    response = client.post(PATH, json=body, headers=_signed(client))
    assert response.status_code == 400
    assert _state(vault) == before
    assert not (vault / BACKUP).exists()


def test_apply_rejects_invalid_json(tmp_path: Path) -> None:
    client = _client(_vault(tmp_path))
    response = client.post(
        PATH, content=b"{not json", headers={**_signed(client), "Content-Type": "application/json"}
    )
    assert response.status_code == 400


def test_apply_unknown_workspace_is_404(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    response = client.post(PATH, json=_apply_body("d", workspace="nope"), headers=_signed(client))
    assert response.status_code == 404
    assert not (vault / BACKUP).exists()


def test_apply_refuses_a_live_lease_and_leaves_the_state_bytes(tmp_path: Path) -> None:
    live = _live_lease_state()
    vault = _vault(tmp_path, state=live)
    client = _client(vault)
    digest = _digest(client)
    response = client.post(PATH, json=_apply_body(digest), headers=_signed(client))
    assert response.status_code == 409
    body = response.json()
    assert body["error"]
    assert any("live curation lease" in blocker for blocker in body["blockers"])
    assert _state(vault) == live
    assert not (vault / BACKUP).exists()


def test_apply_refuses_a_stale_digest(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    # The vault changes after the preview: the digest no longer names it.
    curation_run.state_path(vault).write_bytes(_v1_bytes().replace(b"{", b"{ ", 1))
    before = _state(vault)
    response = client.post(PATH, json=_apply_body(digest), headers=_signed(client))
    assert response.status_code == 409
    assert any("digest" in blocker for blocker in response.json()["blockers"])
    assert _state(vault) == before
    assert not (vault / BACKUP).exists()


def test_apply_migrates_and_returns_the_backup_path(tmp_path: Path) -> None:
    v1 = _v1_bytes()
    vault = _vault(tmp_path, state=v1)
    client = _client(vault)
    digest = _digest(client)
    response = client.post(PATH, json=_apply_body(digest), headers=_signed(client))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "migrated"
    assert body["backup_path"] == str(vault / BACKUP)
    assert (vault / BACKUP).read_bytes() == v1
    assert isinstance(curation_cycle.decode_state(_state(vault).decode("utf-8")), curation_cycle.CycleState)


def test_a_repeat_apply_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    assert client.post(PATH, json=_apply_body(digest), headers=_signed(client)).status_code == 200
    migrated = _state(vault)
    backup = (vault / BACKUP).read_bytes()
    response = client.post(PATH, json=_apply_body(digest), headers=_signed(client))
    assert response.status_code == 409
    assert _state(vault) == migrated
    assert (vault / BACKUP).read_bytes() == backup


def test_apply_hands_the_library_only_the_proven_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam: the route calls the library with the literal confirmation and the preview's digest."""
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    calls: list[tuple[Path, dict[str, Any]]] = []
    real = curation_run.migrate_state_to_cycle

    def spy(root: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append((root, kwargs))
        return real(root, **kwargs)

    monkeypatch.setattr(curation_run, "migrate_state_to_cycle", spy)
    assert client.post(PATH, json=_apply_body(digest), headers=_signed(client)).status_code == 200
    assert calls == [(vault, {"confirm_quiesced": True, "expected_digest": digest})]
    assert calls[0][1]["confirm_quiesced"] is True


def test_refused_requests_never_reach_the_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _vault(tmp_path)
    calls: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(curation_run, "migrate_state_to_cycle", spy)
    client = _client(vault)
    remote = _client(vault, peer="100.64.0.7")
    assert remote.post(PATH, json=_apply_body("d"), headers=_signed(remote)).status_code == 403
    assert client.post(PATH, json=_apply_body("d", confirm_quiesced="true"), headers=_signed(client)).status_code == 400
    assert calls == []


def test_the_route_is_host_only_and_not_loopback_allowlisted() -> None:
    assert PATH not in auth._LOOPBACK_ONLY_API
    assert PATH not in auth._PUBLIC_API


# --- browser-only POST (F1) -------------------------------------------------


def _session_only(client: TestClient) -> dict[str, str]:
    """A session cookie with no Origin or Referer: what a shell curl would send."""
    return {"Cookie": f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"}


def test_curl_style_apply_with_a_valid_session_and_no_origin_is_403_and_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    before = _state(vault)
    response = client.post(PATH, json=_apply_body(digest), headers=_session_only(client))
    assert response.status_code == 403
    assert response.json()["reason"] == "browser_request_required"
    assert _state(vault) == before
    assert not (vault / BACKUP).exists()


def test_a_same_origin_referer_alone_is_still_a_browser_request(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    headers = {**_session_only(client), "Referer": f"{BASE}/settings/workspace"}
    response = client.post(PATH, json=_apply_body(digest), headers=headers)
    assert response.status_code == 200, response.text


def test_a_cross_origin_referer_without_origin_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    client = _client(vault)
    digest = _digest(client)
    before = _state(vault)
    headers = {**_session_only(client), "Referer": "http://localhost:3000/page"}
    response = client.post(PATH, json=_apply_body(digest), headers=headers)
    assert response.status_code == 403
    assert _state(vault) == before
