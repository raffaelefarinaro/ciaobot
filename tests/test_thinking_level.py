"""Tests for per-chat thinking/reasoning level dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import THINKING_LEVELS
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


# ── update_chat validation ───────────────────────────────────────────────

def test_update_chat_sets_valid_thinking_level(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")

    updated = pcm.update_chat(chat.chat_id, thinking_level="high")
    assert updated is not None
    assert updated.thinking_level == "high"

    persisted = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert persisted["chats"][chat.chat_id]["thinking_level"] == "high"


def test_update_chat_rejects_unknown_thinking_level(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")

    with pytest.raises(ValueError, match="Unknown thinking level"):
        pcm.update_chat(chat.chat_id, thinking_level="bogus")


def test_update_chat_empty_thinking_level_resets_to_default(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    pcm.update_chat(chat.chat_id, thinking_level="max")

    updated = pcm.update_chat(chat.chat_id, thinking_level="")
    assert updated is not None
    assert updated.thinking_level == ""


def test_update_chat_thinking_level_allowed_mid_chat(tmp_path: Path) -> None:
    # Unlike provider/model switches, a thinking change never invalidates
    # the provider session, so it must work on a started chat.
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    chat.session_id = "sess-1"
    chat.user_turn_count = 3
    pcm._save()

    updated = pcm.update_chat(chat.chat_id, thinking_level="low")
    assert updated is not None
    assert updated.thinking_level == "low"


def test_handover_resets_thinking_level(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    pcm.update_chat(chat.chat_id, thinking_level="high")
    chat.session_id = "sess-1"
    chat.user_turn_count = 1
    pcm._save()

    updated = pcm.handover_chat(
        chat.chat_id, provider="claude", model="sonnet", messages=[]
    )
    assert updated is not None
    assert updated.thinking_level == ""


def test_stale_thinking_level_falls_back_to_default(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    # Simulate stale persisted data: an unknown level on a Claude chat.
    chat.thinking_level = "off"
    assert pcm._thinking_level_for_chat(chat) == ""
    chat.thinking_level = "high"
    assert pcm._thinking_level_for_chat(chat) == "high"


# ── provider command construction ────────────────────────────────────────

def test_claude_levels_match_sdk_effort_literal() -> None:
    # Guard against SDK upgrades renaming/narrowing the effort values we
    # surface in the picker. ``effort`` is typed as Optional[Literal[...]].
    import typing

    from claude_agent_sdk import ClaudeAgentOptions

    hints = typing.get_type_hints(ClaudeAgentOptions)
    literal = typing.get_args(hints["effort"])[0]
    sdk_levels = set(typing.get_args(literal))
    assert set(THINKING_LEVELS["claude"]) <= sdk_levels


# ── models endpoint ──────────────────────────────────────────────────────

def _models_body(monkeypatch, claude_catalog: list[dict]) -> dict:
    import asyncio
    from unittest.mock import AsyncMock

    from starlette.requests import Request

    from ciao.providers.acp.agents import ClaudeAcpProvider, CodexAcpProvider
    from ciao.providers.opencode import OpencodeProvider
    from ciao.web.routes_api import list_models

    # A unit test must never start a real `opencode serve` (#803) or an ACP adapter:
    # the catalogs are stubbed, so the assertions are about what /api/models builds
    # from them, not about what the installed CLIs happen to report.
    monkeypatch.setattr(OpencodeProvider, "model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setattr(ClaudeAcpProvider, "model_catalog", AsyncMock(return_value=claude_catalog))
    monkeypatch.setattr(CodexAcpProvider, "model_catalog", AsyncMock(return_value=[]))
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    config = CiaoConfig.from_env()
    config.claude_default_model = "opus"
    config.provider_default_models = {"claude": "sonnet"}
    scope = {"type": "http", "method": "GET", "path": "/api/models", "headers": [], "query_string": b""}

    class _App:
        class state:
            pass

    _App.state.config = config
    request = Request(scope)
    scope["app"] = _App

    return json.loads(asyncio.run(list_models(request)).body)


def test_list_models_exposes_thinking_levels(monkeypatch) -> None:
    catalog = [
        {"model": "sonnet", "label": "Sonnet", "variants": ["low", "high"]},
        {"model": "opus", "label": "Opus", "variants": ["low", "medium", "high", "max"]},
    ]
    data = _models_body(monkeypatch, catalog)

    # The static per-provider ladder is still what the PWA gets for Claude...
    assert data["thinking_levels"]["claude"] == list(THINKING_LEVELS["claude"])
    # ...and the per-model levels come from the adapter's own catalog, keyed by bare id.
    assert data["model_reasoning_levels"]["sonnet"] == ["low", "high"]
    assert data["model_reasoning_levels"]["opus"] == ["low", "medium", "high", "max"]
    # The operator default is used when the adapter reports it.
    assert data["provider_models"]["claude"] == ["sonnet", "opus"]
    assert data["provider_defaults"]["claude"] == "sonnet"
    assert data["default"] == "sonnet"


def test_list_models_falls_back_to_the_tier_default_when_the_adapter_reports_nothing(
    monkeypatch,
) -> None:
    data = _models_body(monkeypatch, [])

    # A failed or empty adapter probe leaves no per-model levels and no catalog default,
    # while the Claude tier default still reaches the PWA through the top-level fields.
    assert data["thinking_levels"]["claude"] == list(THINKING_LEVELS["claude"])
    assert data["model_reasoning_levels"] == {}
    assert data["provider_defaults"]["claude"] == ""
    assert data["default"] == "sonnet"
