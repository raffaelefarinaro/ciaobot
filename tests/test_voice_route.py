"""POST /api/voice: transcription without a chat (the home composer)."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import chat_voice, voice_transcribe


class _FakeManager:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.saved: list[Path] = []

    def get_chat(self, chat_id: str) -> object | None:
        return None

    def save_voice_upload(self, data: bytes, filename: str) -> Path:
        path = self.tmp_path / filename
        path.write_bytes(data)
        self.saved.append(path)
        return path

    async def transcribe_voice(self, path: Path) -> tuple[str, float]:
        return "draft the launch brief", 0.0


def _client(tmp_path: Path) -> tuple[TestClient, _FakeManager]:
    app = Starlette(routes=[
        Route("/api/voice", voice_transcribe, methods=["POST"]),
        Route("/api/chats/{chat_id}/voice", chat_voice, methods=["POST"]),
    ])
    manager = _FakeManager(tmp_path)
    app.state.project_chat_manager = manager
    return TestClient(app), manager


def test_transcribes_without_a_chat_and_removes_the_upload(tmp_path: Path) -> None:
    client, manager = _client(tmp_path)
    response = client.post("/api/voice", files={"audio": ("voice.webm", b"fake-audio", "audio/webm")})
    assert response.status_code == 200
    assert response.json()["text"] == "draft the launch brief"
    assert manager.saved and not manager.saved[0].exists()


def test_rejects_a_request_without_audio(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    response = client.post("/api/voice", data={"other": "x"})
    assert response.status_code == 400


def test_the_per_chat_route_still_requires_the_chat(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    response = client.post("/api/chats/missing/voice", files={"audio": ("voice.webm", b"x", "audio/webm")})
    assert response.status_code == 404
