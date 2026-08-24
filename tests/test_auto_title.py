from __future__ import annotations

import pytest
from types import SimpleNamespace

from ciao.web.project_chats import ProjectChatManager


def _manager(config=None, *, chats=None, projects=None) -> ProjectChatManager:
    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._config = config or SimpleNamespace(workspace_root="/tmp")
    manager._projects = projects or {}
    manager._chats = chats or {}
    manager._save = lambda: None
    manager._titling = set()
    # Production waits ~30s in total for the provider to publish its title.
    # Keep the retry *shape* (so the give-up path is still exercised) without
    # the wall-clock cost.
    manager._TITLE_POLL_DELAYS = (0.0, 0.001, 0.001)
    return manager


def _chat(chat_id="chat-1", *, provider="claude", session_id="sess-1", title="New Chat"):
    return SimpleNamespace(
        chat_id=chat_id,
        project_id="project-1",
        provider=provider,
        session_id=session_id,
        title=title,
    )


@pytest.mark.asyncio
async def test_native_title_opencode(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "How hash tables work"}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) == "How hash tables work"


@pytest.mark.asyncio
async def test_native_title_opencode_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": ""}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_native_title_opencode_placeholder_is_not_final(monkeypatch) -> None:
    """opencode's default 'New session - <timestamp>' must not be accepted.

    The provider seeds the session with that placeholder and only later writes
    the generated title; accepting it would leave the sidebar stuck on it.
    """
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "New session - 2026-08-18T12:20:10.198Z"}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_native_title_claude(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})

    class FakeInfo:
        custom_title = "Claude AI Title"
        summary = "Claude Summary"

    monkeypatch.setattr(pc, "get_session_info", lambda _sid, directory=None: FakeInfo())
    assert await manager._native_chat_title(manager._chats["chat-1"]) == "Claude AI Title"


@pytest.mark.asyncio
async def test_native_title_claude_falls_back_to_summary(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})

    class FakeInfo:
        custom_title = None
        summary = "Claude Summary"

    monkeypatch.setattr(pc, "get_session_info", lambda _sid, directory=None: FakeInfo())
    assert await manager._native_chat_title(manager._chats["chat-1"]) == "Claude Summary"


@pytest.mark.asyncio
async def test_native_title_claude_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    monkeypatch.setattr(pc, "get_session_info", lambda _sid, directory=None: None)
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_auto_title_if_default_sets_native_title(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager.auto_title_if_default("chat-1", "hello") == "Native Title"
    assert manager._chats["chat-1"].title == "Native Title"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_when_not_default(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(title="Already Named")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager.auto_title_if_default("chat-1", "hello") is None
    assert manager._chats["chat-1"].title == "Already Named"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_without_session(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(session_id="")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    # No session_id throughout the poll window -> native never reachable, so
    # the deterministic fallback is used instead of leaving "New Chat".
    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"
    assert manager._chats["chat-1"].title == "hello"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_when_native_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": ""}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    # Native title never arrives within the poll window -> fallback from the
    # prompt so the sidebar doesn't stay stuck on "New Chat".
    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"
    assert manager._chats["chat-1"].title == "hello"


@pytest.mark.asyncio
async def test_start_stream_fires_title_immediately_for_question_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression of #176's overcorrection: question-shaped prompts used to defer
    title generation until the first reply, which left the sidebar blank for
    the full first turn. The title must fire on the user echo regardless of
    question shape.
    """
    from ciao.web import project_chats as pc

    fired: list[tuple[str, str, str]] = []

    async def fake_auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None:
        fired.append((chat_id, user_text, assistant_text))

    monkeypatch.setattr(
        ProjectChatManager, "_auto_title_and_publish", fake_auto_title_and_publish
    )

    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._events = SimpleNamespace(publish=lambda *_args, **_kwargs: None)  # type: ignore[assignment,misc]
    manager._save = lambda: None  # type: ignore[assignment,method-assign,misc]
    manager._chats = {}  # type: ignore[assignment]

    chat_info = pc.ChatInfo(
        chat_id="chat-q1",
        project_id="proj-test",
        title="New Chat",
        created_at="2026-08-14T00:00:00Z",
        last_activity_at="2026-08-14T00:00:00Z",
        last_read_at="2026-08-14T00:00:00Z",
        title_status="ready",
    )
    manager._chats["chat-q1"] = chat_info  # type: ignore[index]

    prompt = "why no recent sessions?"
    async def _expect_immediate() -> None:
        chat = manager._chats.get("chat-q1")
        assert chat is not None
        if chat.title == "New Chat" and prompt.strip():
            chat.title_status = "pending"
            await manager._auto_title_and_publish("chat-q1", prompt, "")

    await _expect_immediate()

    assert len(fired) == 1, "title must fire on the user echo, not after the reply"
    assert fired[0] == ("chat-q1", "why no recent sessions?", "")


@pytest.mark.asyncio
async def test_drive_finally_runs_second_title_pass_for_meta_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-reply path always re-runs the titleer with both sides of the
    exchange. The publish step is a no-op when the new title matches the live
    one, so the sidebar only sees a second chat_title event when the late
    title actually differs.
    """
    from ciao.web import project_chats as pc
    from ciao.web.project_chats import ProjectChatManager

    class FakeChat:
        title = "Live Early Title"
        title_status = "ready"

    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._events = SimpleNamespace(publish=lambda *_args, **_kwargs: None)  # type: ignore[assignment,misc]
    manager._save = lambda: None  # type: ignore[assignment,method-assign,misc]
    manager._chats = {"chat-q2": FakeChat()}  # type: ignore[assignment,dict-item]
    manager._titling = set()

    calls = []

    async def fake_auto_title(self, chat_id, user_text, assistant_text):
        calls.append((chat_id, user_text, assistant_text))
        return "Live Early Title"

    monkeypatch.setattr(
        ProjectChatManager, "auto_title_if_default", fake_auto_title
    )

    published = []

    def fake_publish(event):
        published.append(event)

    manager._events = SimpleNamespace(publish=fake_publish)  # type: ignore[assignment]

    # Same call the finally block in _drive() makes.
    await manager._auto_title_and_publish("chat-q2", "why no recent sessions?", "Assistant reply body")

    assert calls == [("chat-q2", "why no recent sessions?", "Assistant reply body")]
    # matching title means no second publish.
    assert published == []


@pytest.mark.asyncio
async def test_drive_overwrites_title_when_assistant_framing_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-reply titleer yields a different label than the early one,
    a second chat_title event is published so the sidebar can update."""
    from ciao.web import project_chats as pc
    from ciao.web.project_chats import ProjectChatManager

    class FakeChat:
        title = "Why No Recent Sessions"
        title_status = "ready"

    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._chats = {"chat-q3": FakeChat()}  # type: ignore[assignment,dict-item]
    manager._titling = set()

    async def fake_auto_title(self, chat_id, user_text, assistant_text):
        return "Automation Page Job Log"

    monkeypatch.setattr(
        ProjectChatManager, "auto_title_if_default", fake_auto_title
    )

    published = []

    def fake_publish(event):
        published.append(event)

    manager._events = SimpleNamespace(publish=fake_publish)  # type: ignore[assignment]

    await manager._auto_title_and_publish(
        "chat-q3", "why no recent sessions?", "Assistant reply body"
    )

    assert published == [
        {
            "type": "chat_title",
            "chat_id": "chat-q3",
            "title": "Automation Page Job Log",
            "status": "ready",
        }
    ]
    # Title is rewritten on the chat record too.
    assert manager._chats["chat-q3"].title == "Automation Page Job Log"  # type: ignore[index]


@pytest.mark.asyncio
async def test_auto_title_waits_for_late_provider_title(monkeypatch) -> None:
    """The provider writes its title *after* the turn, so the first read finds
    nothing. Every previous test handed the title back on read #1, which is why
    the suite stayed green while every real chat sat on "New Chat"."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    reads = 0

    async def fake_read_thread(_workspace, _sid):
        nonlocal reads
        reads += 1
        # opencode's title agent has not run yet on the first two reads.
        title = "Late Native Title" if reads >= 3 else ""
        return {"info": {"title": title}, "messages": []}

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager.auto_title_if_default("chat-1", "hello") == "Late Native Title"
    assert manager._chats["chat-1"].title == "Late Native Title"
    assert reads == 3


@pytest.mark.asyncio
async def test_auto_title_polls_until_session_exists(monkeypatch) -> None:
    """The first-message trigger fires before the provider session exists. A
    missing session_id must retry, not abandon titling for the whole chat."""
    from ciao.web import project_chats as pc

    chat = _chat(provider="opencode", session_id="")
    manager = _manager(chats={"chat-1": chat})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": "Arrived Late"}, "messages": []}

    # The turn assigns the session while the poller is waiting.
    async def grant_session(*_args, **_kwargs):
        chat.session_id = "sess-late"

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr(pc.asyncio, "sleep", grant_session)

    assert await manager.auto_title_if_default("chat-1", "hello") == "Arrived Late"
    assert chat.title == "Arrived Late"


@pytest.mark.asyncio
async def test_auto_title_stops_when_user_renames_mid_poll(monkeypatch) -> None:
    """A rename during the poll window must win over the provider's title."""
    from ciao.web import project_chats as pc

    chat = _chat(provider="opencode")
    manager = _manager(chats={"chat-1": chat})

    async def fake_read_thread(_workspace, _sid):
        return {"info": {"title": ""}, "messages": []}

    async def rename_during_wait(*_args, **_kwargs):
        chat.title = "User Picked This"

    monkeypatch.setattr(pc.OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr(pc.asyncio, "sleep", rename_during_wait)

    assert await manager.auto_title_if_default("chat-1", "hello") is None
    assert chat.title == "User Picked This"


@pytest.mark.asyncio
async def test_auto_title_and_publish_runs_once_per_chat(monkeypatch) -> None:
    """Both triggers fire for the same chat; only one poll may run."""
    from ciao.web.project_chats import ProjectChatManager
    import asyncio as _asyncio

    class FakeChat:
        title = "New Chat"
        title_status = "pending"

    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._chats = {"c": FakeChat()}  # type: ignore[assignment,dict-item]
    manager._titling = set()
    manager._save = lambda: None  # type: ignore[assignment,method-assign,misc]
    manager._events = SimpleNamespace(publish=lambda *_a, **_k: None)  # type: ignore[assignment]

    started = 0

    async def slow_title(self, chat_id, user_text, assistant_text):
        nonlocal started
        started += 1
        await _asyncio.sleep(0)
        return "Only Once"

    monkeypatch.setattr(ProjectChatManager, "auto_title_if_default", slow_title)

    await _asyncio.gather(
        manager._auto_title_and_publish("c", "prompt", ""),
        manager._auto_title_and_publish("c", "prompt", "reply"),
    )
    assert started == 1
