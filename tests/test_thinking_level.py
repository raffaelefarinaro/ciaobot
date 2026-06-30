"""Tests for per-chat thinking/reasoning level dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import THINKING_LEVELS
from ciao.providers.pi import PiProvider, PiSettings
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
        pi=PiSettings(models=("qwen3-coder",)),
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

    # "off" is a Pi level, not a Claude one.
    with pytest.raises(ValueError, match="Unknown thinking level"):
        pcm.update_chat(chat.chat_id, thinking_level="off")


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
        chat.chat_id, provider="pi", model="qwen3-coder", messages=[]
    )
    assert updated is not None
    assert updated.thinking_level == ""


def test_stale_thinking_level_falls_back_to_default(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("thinking", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    # Simulate stale persisted data: a Pi-only level on a Claude chat.
    chat.thinking_level = "off"
    assert pcm._thinking_level_for_chat(chat) == ""
    chat.thinking_level = "high"
    assert pcm._thinking_level_for_chat(chat) == "high"


# ── provider command construction ────────────────────────────────────────

def test_pi_args_include_thinking_when_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))
    provider = PiProvider(tmp_path, config=PiSettings(models=("qwen3-coder",)))
    args = provider._build_pi_args("qwen3-coder", "chat-1", "xhigh")
    idx = args.index("--thinking")
    assert args[idx + 1] == "xhigh"


def test_pi_args_omit_thinking_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CIAO_PI_SESSION_DIR", str(tmp_path / "pi-sessions"))
    provider = PiProvider(tmp_path, config=PiSettings(models=("qwen3-coder",)))
    args = provider._build_pi_args("qwen3-coder", "chat-1")
    assert "--thinking" not in args


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
    assert data["thinking_levels"]["pi"] == list(THINKING_LEVELS["pi"])
