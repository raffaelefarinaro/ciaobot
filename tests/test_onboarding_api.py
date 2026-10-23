from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.onboarding import (
    OnboardingConflictError,
    OnboardingProgress,
    OnboardingReceipt,
    ensure_receipt,
    read_receipt,
    write_receipt,
)
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.setup_marker import RUNTIME_DIR_NAME
from ciao.onboarding_progress import change_step, complete_progress, get_progress
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_onboarding import complete_onboarding, get_onboarding, patch_onboarding

_LOOPBACK_PEER = ("127.0.0.1", 50000)


@pytest.fixture
def onboarding_client(tmp_path: Path) -> tuple[TestClient, CiaoConfig, URLSafeTimedSerializer]:
    runtime = tmp_path / RUNTIME_DIR_NAME
    config = CiaoConfig(pwa_auth_token="test-token", workspace_root=tmp_path, state_path=runtime / "state.json", media_root=runtime / "media")
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/onboarding", get_onboarding, methods=["GET"]),
            Route("/api/onboarding", patch_onboarding, methods=["PATCH"]),
            Route("/api/onboarding/complete", complete_onboarding, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    client = TestClient(app, base_url="http://localhost:8443", client=_LOOPBACK_PEER)
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client, config, serializer


def _seed(client: TestClient, config: CiaoConfig, step: str = "location", status: str = "in_progress") -> dict[str, Any]:
    runtime = Path(config.workspace_root) / RUNTIME_DIR_NAME
    ensure_receipt(runtime, established_install=False)
    receipt = OnboardingReceipt(0, OnboardingProgress(status, step), {})
    write_receipt(runtime, receipt, expected_revision=0)
    return {"expected_revision": 1, "target": {"kind": "installation"}}


def _config(root: Path) -> object:
    class Config:
        workspace_root = root
        state_path = root / RUNTIME_DIR_NAME / "state.json"

    return Config()


def test_progress_uses_the_configured_runtime_root_not_the_workspace(tmp_path: Path) -> None:
    """Bootstrap and external runtimes put the receipt outside the workspace."""
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "external-runtime"
    config = type("Config", (), {
        "workspace_root": workspace,
        "state_path": runtime / "state.json",
    })()
    ensure_receipt(runtime, established_install=False)
    write_receipt(runtime, OnboardingReceipt(0, OnboardingProgress("in_progress", "location"), {}), expected_revision=0)
    changed = change_step(config, target={"kind": "installation"}, expected_revision=1, step="runtime")
    assert changed.installation.step == "runtime"
    assert read_receipt(runtime) == changed
    assert not (workspace / RUNTIME_DIR_NAME / "onboarding.json").exists()


def test_progress_advances_global_revision_and_preserves_other_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    receipt = OnboardingReceipt(0, OnboardingProgress("in_progress", "location"), {})
    ensure_receipt(tmp_path / ".runtime", established_install=False)
    write_receipt(tmp_path / ".runtime", receipt, expected_revision=0)
    changed = change_step(config, target={"kind": "installation"}, expected_revision=1, step="runtime")
    assert changed.revision == 2
    assert changed.installation.step == "runtime"


def test_same_step_does_not_write_and_stale_revision_conflicts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ensure_receipt(tmp_path / ".runtime", established_install=False)
    write_receipt(tmp_path / ".runtime", OnboardingReceipt(0, OnboardingProgress("in_progress", "runtime"), {}), expected_revision=0)
    before = read_receipt(tmp_path / ".runtime")
    assert change_step(config, target={"kind": "installation"}, expected_revision=1, step="runtime") == before
    with pytest.raises(OnboardingConflictError):
        change_step(config, target={"kind": "installation"}, expected_revision=0, step="care")


def test_completion_requires_finish_and_completed_replay_is_idempotent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ensure_receipt(tmp_path / ".runtime", established_install=False)
    write_receipt(tmp_path / ".runtime", OnboardingReceipt(0, OnboardingProgress("in_progress", "finish"), {}), expected_revision=0)
    done = complete_progress(config, target={"kind": "installation"}, expected_revision=1)
    assert done.installation.status == "completed"
    assert complete_progress(config, target={"kind": "installation"}, expected_revision=0) == done


def test_get_requires_signed_session_even_from_loopback(onboarding_client) -> None:
    client, _config, _serializer = onboarding_client
    client.cookies.clear()
    response = client.get("/api/onboarding")
    assert response.status_code == 401


def test_mutations_reject_cross_origin(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    body = _seed(client, config)
    response = client.patch("/api/onboarding", json={**body, "step": "care"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_agent_bearer_token_does_not_authorize_progress(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    ensure_receipt(Path(config.workspace_root) / RUNTIME_DIR_NAME, established_install=False)
    client.cookies.clear()
    response = client.get("/api/onboarding", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 401


def test_missing_receipt_returns_503_without_initializing(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    response = client.get("/api/onboarding")
    assert response.status_code == 503
    assert response.json()["code"] == "onboarding_not_initialized"
    assert not (Path(config.workspace_root) / RUNTIME_DIR_NAME / "onboarding.json").exists()


def test_corrupt_receipt_returns_sanitized_500_without_rewrite(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    path = Path(config.workspace_root) / RUNTIME_DIR_NAME / "onboarding.json"
    path.parent.mkdir(parents=True)
    raw = b"private-path-and-corrupt"
    path.write_bytes(raw)
    response = client.get("/api/onboarding")
    assert response.status_code == 500
    assert response.json()["code"] == "onboarding_state_unavailable"
    assert str(path) not in response.text and "private-path" not in response.text
    assert path.read_bytes() == raw


@pytest.mark.parametrize("extra", [{"ready": True}, {"readiness": "ready"}])
def test_patch_rejects_unknown_fields_and_bool_revision(onboarding_client, extra: dict[str, Any]) -> None:
    client, config, _serializer = onboarding_client
    body = _seed(client, config)
    response = client.patch("/api/onboarding", json={**body, "step": "care", **extra})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_onboarding_request"
    response = client.patch("/api/onboarding", json={**body, "step": "care", "expected_revision": True})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_onboarding_request"


def test_workspace_target_requires_registration_and_existing_progress(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config)
    config.workspaces = {"work": WorkspaceConfig(name="work", vault_root="work")}
    body = {"expected_revision": 1, "target": {"kind": "workspace", "name": "work"}, "step": "care"}
    response = client.patch("/api/onboarding", json=body)
    assert response.status_code == 404
    assert response.json()["code"] == "onboarding_target_not_found"
    config.workspaces = {}
    response = client.patch("/api/onboarding", json=body)
    assert response.status_code == 404
    assert response.json()["code"] == "onboarding_target_not_found"


def test_workspace_cannot_enter_location(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config)
    config.workspaces = {"work": WorkspaceConfig(name="work", vault_root="work")}
    runtime = Path(config.workspace_root) / RUNTIME_DIR_NAME
    write_receipt(runtime, OnboardingReceipt(1, OnboardingProgress("in_progress", "location"), {"work": OnboardingProgress("in_progress", "runtime")}), expected_revision=1)
    response = client.patch("/api/onboarding", json={"expected_revision": 2, "target": {"kind": "workspace", "name": "work"}, "step": "location"})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_onboarding_request"


def test_closed_targets_cannot_reopen(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    body = _seed(client, config, status="legacy_exempt")
    response = client.patch("/api/onboarding", json={**body, "step": "care"})
    assert response.status_code == 409
    assert response.json()["code"] == "onboarding_target_closed"


def test_completion_before_finish(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    body = _seed(client, config)
    response = client.post("/api/onboarding/complete", json=body)
    assert response.status_code == 409
    assert response.json()["code"] == "onboarding_finish_required"


def test_completed_replay_with_old_revision_does_not_rewrite(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config, step="finish")
    first = client.post("/api/onboarding/complete", json={"expected_revision": 1, "target": {"kind": "installation"}})
    assert first.status_code == 200
    path = Path(config.workspace_root) / RUNTIME_DIR_NAME / "onboarding.json"
    previous_bytes = path.read_bytes()
    replay = client.post("/api/onboarding/complete", json={"expected_revision": 0, "target": {"kind": "installation"}})
    assert replay.status_code == 200
    assert replay.json()["revision"] == first.json()["revision"]
    assert path.read_bytes() == previous_bytes


@pytest.mark.parametrize("extra", [{"readiness": True}, {"path": "/tmp"}, {"command": "run"}, {"settings": {"x": 1}}])
def test_completion_rejects_readiness_paths_commands_and_settings(onboarding_client, extra: dict[str, Any]) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config, step="finish")
    response = client.post("/api/onboarding/complete", json={"expected_revision": 1, "target": {"kind": "installation"}, **extra})
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_onboarding_request"


@pytest.mark.parametrize("body", [[], [{"expected_revision": 1}], "text", 7])
def test_non_object_body_is_rejected_as_invalid(onboarding_client, body: Any) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config)
    response = client.patch("/api/onboarding", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_onboarding_request"


def test_workspace_entry_for_an_unregistered_name_is_not_found(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config)
    runtime = Path(config.state_path.parent)
    write_receipt(
        runtime,
        OnboardingReceipt(
            1,
            OnboardingProgress("in_progress", "location"),
            {"ghost": OnboardingProgress("in_progress", "runtime")},
        ),
        expected_revision=1,
    )
    config.workspaces = {}
    response = client.patch(
        "/api/onboarding",
        json={"expected_revision": 2, "target": {"kind": "workspace", "name": "ghost"}, "step": "care"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "onboarding_target_not_found"


def test_completion_accepts_deferred_provider_without_readiness_claim(onboarding_client) -> None:
    client, config, _serializer = onboarding_client
    _seed(client, config, step="finish")
    response = client.post("/api/onboarding/complete", json={"expected_revision": 1, "target": {"kind": "installation"}})
    assert response.status_code == 200
    assert response.json()["installation"]["status"] == "completed"
