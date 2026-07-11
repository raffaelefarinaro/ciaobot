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

def test_list_models_exposes_thinking_levels(monkeypatch) -> None:
    import asyncio

    from starlette.requests import Request

    from ciao.web.routes_api import list_models

    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    config = CiaoConfig.from_env()
    scope = {"type": "http", "method": "GET", "path": "/api/models", "headers": []}

    class _App:
        class state:
            pass

    _App.state.config = config
    request = Request(scope)
    scope["app"] = _App

    data = json.loads(asyncio.run(list_models(request)).body)
    assert data["thinking_levels"]["claude"] == list(THINKING_LEVELS["claude"])


# ── per-model codex level validation ─────────────────────────────────────

_CATALOG = [
    {
        "model": "gpt-5.6-terra",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
            {"reasoningEffort": "xhigh"},
        ],
    },
]


def _codex_chat(tmp_path: Path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("codex-levels", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="gpt-5.6-terra", provider="codex")
    return pcm, chat


def test_codex_level_rejected_when_model_lacks_it(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from ciao.web.routes_api import _unsupported_codex_level_error

    async def _catalog(workspace_root):
        return list(_CATALOG)

    monkeypatch.setattr(
        "ciao.web.routes_api.CodexProvider.model_catalog", staticmethod(_catalog)
    )
    pcm, chat = _codex_chat(tmp_path)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )

    # "ultra" is in the union fallback but not in this model's catalog entry.
    error = asyncio.run(
        _unsupported_codex_level_error(
            config, pcm, chat.chat_id, {"thinking_level": "ultra"}
        )
    )
    assert error is not None
    assert error.status_code == 400
    assert b"ultra" in error.body

    # A catalog-supported level passes through.
    ok = asyncio.run(
        _unsupported_codex_level_error(
            config, pcm, chat.chat_id, {"thinking_level": "xhigh"}
        )
    )
    assert ok is None


def test_codex_level_check_fails_open_without_catalog(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from ciao.web.routes_api import _unsupported_codex_level_error

    async def _broken(workspace_root):
        raise RuntimeError("codex app-server unavailable")

    monkeypatch.setattr(
        "ciao.web.routes_api.CodexProvider.model_catalog", staticmethod(_broken)
    )
    pcm, chat = _codex_chat(tmp_path)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )

    # No catalog -> fall back to the union validation in update_chat.
    error = asyncio.run(
        _unsupported_codex_level_error(
            config, pcm, chat.chat_id, {"thinking_level": "ultra"}
        )
    )
    assert error is None


def test_codex_level_check_ignores_claude_chats(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from ciao.web.routes_api import _unsupported_codex_level_error

    called = []

    async def _catalog(workspace_root):
        called.append(True)
        return list(_CATALOG)

    monkeypatch.setattr(
        "ciao.web.routes_api.CodexProvider.model_catalog", staticmethod(_catalog)
    )
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("claude-levels", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )

    error = asyncio.run(
        _unsupported_codex_level_error(
            config, pcm, chat.chat_id, {"thinking_level": "max"}
        )
    )
    assert error is None
    assert called == []
