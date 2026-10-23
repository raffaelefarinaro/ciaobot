"""Tests for GET /api/system-prompt: the text Ciaobot appends to every provider prompt."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import ciao
from ciao.core_prompt import system_prompt_payload
from ciao.web.routes_api import system_prompt


def test_system_prompt_returns_appended_text() -> None:
    app = Starlette(routes=[Route("/api/system-prompt", system_prompt, methods=["GET"])])
    resp = TestClient(app).get("/api/system-prompt")
    assert resp.status_code == 200
    body = resp.json()
    expected = (system_prompt_payload("") or {})["append"]
    assert body["text"] == expected
    assert body["chars"] == len(expected)
    source = Path(ciao.__file__).parent / "system_prompt.md"
    first_line = next(
        line.strip()
        for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert first_line in body["text"]
