"""The Ciaobot core reaches ACP chats once per session, in the stable block (#3137, A4).

Non-ACP drivers keep sending the core as a system prompt, so their capsule
carries none of it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao import provider_registry
from ciao.core_prompt import unified_core
from ciao.models import ContextCompactedEvent, ResultEvent
from ciao.web.chat_broker import ChatStream
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost
from tests.test_chat_streaming_boundary import _DriveHost
from tests.test_context_capsule import _make_manager


def _as_acp(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    """Make ``provider`` look like an ACP provider to the registry; nothing is spawned."""
    real_get = provider_registry.get

    def fake_get(name: str):  # type: ignore[no-untyped-def]
        descriptor = real_get(name)
        if name == provider and descriptor is not None:
            return dataclasses.replace(descriptor, acp_spec_path="ciao.providers.acp.spec:UNUSED")
        return descriptor

    monkeypatch.setattr(provider_registry, "get", fake_get)


def _commit_first_turn(manager, chat, session_id: str = "ses_1") -> None:  # type: ignore[no-untyped-def]
    """Send the first turn's capsule and record it as delivered in ``session_id``."""
    chat.session_id = session_id
    manager._build_prompt_prefix(chat)
    digest = manager._stable_context_marker(chat)[0]
    manager._commit_context_marker(chat, SimpleNamespace(context_digest=digest), session_id)


def test_acp_chat_first_turn_prompt_starts_with_context_and_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)
    _as_acp(monkeypatch, chat.provider)

    prefix = manager._build_prompt_prefix(chat)

    assert prefix.startswith("[CIAO_CONTEXT_BEGIN]")
    # The core is the first stable fact, ahead of the workspace line.
    assert "<ciao-context>\n" + unified_core() in prefix
    assert prefix.index(unified_core()) < prefix.index("workspace=work")


def test_acp_chat_second_turn_omits_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)
    _as_acp(monkeypatch, chat.provider)
    _commit_first_turn(manager, chat)

    prefix = manager._build_prompt_prefix(chat)

    assert "today=" in prefix
    assert unified_core() not in prefix


def test_editing_the_core_resends_the_stable_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)
    _as_acp(monkeypatch, chat.provider)
    monkeypatch.setattr("ciao.core_prompt._system_instructions", lambda: "core a")
    _commit_first_turn(manager, chat)
    assert not manager._needs_stable_context(chat)

    monkeypatch.setattr("ciao.core_prompt._system_instructions", lambda: "core b")

    assert manager._needs_stable_context(chat)


def test_non_acp_chat_capsule_has_no_core(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)

    prefix = manager._build_prompt_prefix(chat)

    assert "workspace=work" in prefix
    assert unified_core() not in prefix


@pytest.mark.asyncio
async def test_context_compacted_event_resets_marker(tmp_path: Path) -> None:
    """The drive loop forgets the stable-context marker when the agent compacts mid-turn."""
    host = _DriveHost(
        [
            ContextCompactedEvent(type="context_compacted"),
            ResultEvent(type="result", result="done"),
        ],
        tmp_path,
    )
    host.chat.context_digest = "digest"
    host.chat.context_session_id = "ses_1"
    host._providers["chat-1"] = SimpleNamespace(
        provider=SimpleNamespace(current_session_id="ses_1")
    )
    host._notify_question = lambda *_a, **_k: None  # type: ignore[attr-defined]
    host._notify_permission = lambda *_a, **_k: None  # type: ignore[attr-defined]
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hello",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert host.chat.context_digest == ""
    assert host.chat.context_session_id == ""


@pytest.mark.asyncio
async def test_compaction_between_turns_resets_marker(tmp_path: Path) -> None:
    """A compaction the agent reports between turns clears the marker the same way."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Ciaobot", workspace="work")
    chat = manager.create_chat(project.project_id)
    chat.context_digest = "digest"
    chat.context_session_id = "ses_1"

    class _Service:
        provider = None
        background_work_outstanding = False

        async def drain_events(self):  # type: ignore[no-untyped-def]
            yield ContextCompactedEvent(type="context_compacted")

    manager._providers[chat.chat_id] = _Service()  # type: ignore[assignment]
    await manager._streaming.drain_between_turns(chat.chat_id, project.project_id)

    assert chat.context_digest == ""
    assert chat.context_session_id == ""
