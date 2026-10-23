"""Tests for GET /api/system-prompt: the text Ciaobot appends to every provider prompt."""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.core_prompt import system_prompt_payload
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE


def test_system_prompt_returns_appended_text(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")
        },
    )
    app = create_app(config)
    client = TestClient(app, base_url="https://ciao.example")

    assert client.get("/api/system-prompt").status_code == 401

    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    resp = client.get("/api/system-prompt")
    assert resp.status_code == 200
    payload = system_prompt_payload("")
    assert payload
    body = resp.json()
    assert body["text"] == payload["append"]
    assert body["chars"] == len(payload["append"])
