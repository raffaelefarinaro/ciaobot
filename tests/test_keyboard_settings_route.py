from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.app_settings import AppSettingsStore
from ciao.web.routes_api import settings_keyboard


def test_keyboard_settings_endpoint_persists_and_broadcasts(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")

    class Events:
        published = []

        def publish(self, payload):
            self.published.append(payload)

    class Manager:
        events = Events()

    app = Starlette(routes=[Route("/api/settings/keyboard", settings_keyboard, methods=["GET", "PATCH"])])
    app.state.app_settings = store
    app.state.project_chat_manager = Manager()
    client = TestClient(app)

    revision = client.get("/api/settings/keyboard").json()["revision"]
    response = client.patch("/api/settings/keyboard", json={
        "keyboard_shortcuts": {"archiveChat": "Mod+KeyK"},
        "keyboard_send_mode": "enter",
        "revision": revision,
    })

    assert response.status_code == 200
    assert response.json()["keyboard_shortcuts"] == {"archiveChat": "Mod+KeyK"}
    assert response.json()["keyboard_send_mode"] == "enter"
    assert isinstance(response.json()["revision"], str)
    assert client.get("/api/settings/keyboard").json() == response.json()
    assert Manager.events.published[-1] == {
        "type": "keyboard_settings_changed",
        "keyboard_shortcuts": {"archiveChat": "Mod+KeyK"},
        "keyboard_send_mode": "enter",
        "revision": response.json()["revision"],
    }


def test_keyboard_settings_endpoint_rejects_invalid_patch(tmp_path):
    app = Starlette(routes=[Route("/api/settings/keyboard", settings_keyboard, methods=["GET", "PATCH"])])
    app.state.app_settings = AppSettingsStore(tmp_path / "app_settings.json")
    client = TestClient(app)

    revision = client.get("/api/settings/keyboard").json()["revision"]
    response = client.patch("/api/settings/keyboard", json={"keyboard_send_mode": "arbitrary", "revision": revision})

    assert response.status_code == 400
    assert AppSettingsStore(tmp_path / "app_settings.json").settings.keyboard_send_mode == ""


def test_keyboard_settings_endpoint_rejects_stale_revision(tmp_path):
    app = Starlette(routes=[Route("/api/settings/keyboard", settings_keyboard, methods=["GET", "PATCH"])])
    app.state.app_settings = AppSettingsStore(tmp_path / "app_settings.json")
    client = TestClient(app)
    stale = client.get("/api/settings/keyboard").json()["revision"]
    client.patch("/api/settings/keyboard", json={"keyboard_send_mode": "enter", "revision": stale})

    response = client.patch("/api/settings/keyboard", json={"keyboard_send_mode": "modifier", "revision": stale})

    assert response.status_code == 409
    assert client.get("/api/settings/keyboard").json()["keyboard_send_mode"] == "enter"


def test_keyboard_patch_rejects_unrelated_fields_without_writes_or_events(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    published = []

    class Events:
        def publish(self, payload):
            published.append(payload)

    class Manager:
        events = Events()

    app = Starlette(routes=[Route("/api/settings/keyboard", settings_keyboard, methods=["GET", "PATCH"])])
    app.state.app_settings = store
    app.state.project_chat_manager = Manager()
    client = TestClient(app)
    initial = client.get("/api/settings/keyboard").json()

    response = client.patch("/api/settings/keyboard", json={
        "revision": initial["revision"],
        "keyboard_send_mode": "enter",
        "log_level": "debug",
    })

    assert response.status_code == 400
    assert client.get("/api/settings/keyboard").json() == initial
    assert store.settings.log_level == ""
    assert published == []


def test_mid_turn_delivery_is_an_unknown_key(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")

    class Events:
        published = []

        def publish(self, payload):
            self.published.append(payload)

    class Manager:
        events = Events()

    app = Starlette(routes=[Route("/api/settings/keyboard", settings_keyboard, methods=["GET", "PATCH"])])
    app.state.app_settings = store
    app.state.project_chat_manager = Manager()
    client = TestClient(app)

    initial = client.get("/api/settings/keyboard").json()
    assert "mid_turn_delivery" not in initial

    response = client.patch("/api/settings/keyboard", json={
        "mid_turn_delivery": "steer",
        "revision": initial["revision"],
    })

    assert response.status_code == 400
    assert "mid_turn_delivery" not in client.get("/api/settings/keyboard").json()
    assert Manager.events.published == []
