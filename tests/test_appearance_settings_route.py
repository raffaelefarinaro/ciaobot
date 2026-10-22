from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.app_settings import AppSettingsStore
from ciao.web.routes_api import settings_appearance


class _Events:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload):
        self.published.append(payload)


class _Manager:
    def __init__(self) -> None:
        self.events = _Events()


def _client(tmp_path) -> tuple[TestClient, _Manager]:
    app = Starlette(routes=[Route("/api/settings/appearance", settings_appearance, methods=["GET", "PATCH"])])
    app.state.app_settings = AppSettingsStore(tmp_path / "app_settings.json")
    manager = _Manager()
    app.state.project_chat_manager = manager
    return TestClient(app), manager


def test_get_defaults(tmp_path):
    client, _ = _client(tmp_path)
    body = client.get("/api/settings/appearance").json()
    assert body["text_contrast"] == "normal"
    assert body["theme"] is None
    assert body["font_scale"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", body["revision"])


def test_patch_publishes_event_and_persists(tmp_path):
    client, manager = _client(tmp_path)
    revision = client.get("/api/settings/appearance").json()["revision"]
    response = client.patch(
        "/api/settings/appearance",
        json={"text_contrast": "soft", "theme": "dark", "font_scale": 1.3, "revision": revision},
    )
    assert response.status_code == 200
    assert response.json()["text_contrast"] == "soft"
    assert response.json()["theme"] == "dark"
    assert response.json()["font_scale"] == 1.3
    assert client.get("/api/settings/appearance").json() == response.json()
    assert manager.events.published[-1] == {"type": "appearance_settings_changed", **response.json()}


def test_stale_revision_is_409(tmp_path):
    client, _ = _client(tmp_path)
    stale = client.get("/api/settings/appearance").json()["revision"]
    client.patch("/api/settings/appearance", json={"text_contrast": "soft", "revision": stale})
    response = client.patch("/api/settings/appearance", json={"text_contrast": "normal", "revision": stale})
    assert response.status_code == 409
    assert "another device" in response.json()["error"]
    assert client.get("/api/settings/appearance").json()["text_contrast"] == "soft"


def test_missing_revision_is_400(tmp_path):
    client, _ = _client(tmp_path)
    response = client.patch("/api/settings/appearance", json={"text_contrast": "soft"})
    assert response.status_code == 400


@pytest.mark.parametrize("patch", [
    {"text_contrast": "loud"},
    {"theme": "blue"},
    {"font_scale": 3},
])
def test_invalid_value_is_400(tmp_path, patch):
    client, _ = _client(tmp_path)
    revision = client.get("/api/settings/appearance").json()["revision"]
    response = client.patch("/api/settings/appearance", json={**patch, "revision": revision})
    assert response.status_code == 400
    assert client.get("/api/settings/appearance").json()["revision"] == revision


def test_huge_integer_font_scale_is_400(tmp_path):
    client, _ = _client(tmp_path)
    revision = client.get("/api/settings/appearance").json()["revision"]
    response = client.patch(
        "/api/settings/appearance",
        content='{"font_scale": 1' + "0" * 400 + f', "revision": "{revision}"' + "}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_unrelated_settings_keys_are_refused(tmp_path):
    client, _ = _client(tmp_path)
    store = client.app.state.app_settings
    revision = client.get("/api/settings/appearance").json()["revision"]
    response = client.patch(
        "/api/settings/appearance",
        json={"text_contrast": "soft", "dev_mode": True, "pwa_host": "0.0.0.0", "revision": revision},
    )
    assert response.status_code == 400
    assert "dev_mode" in response.json()["error"] and "pwa_host" in response.json()["error"]
    assert store.settings.dev_mode is False
    assert store.settings.text_contrast == ""
