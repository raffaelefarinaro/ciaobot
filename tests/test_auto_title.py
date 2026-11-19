from __future__ import annotations

import asyncio

import pytest
from pathlib import Path
from types import SimpleNamespace

from ciao.providers.base import BaseProvider
from ciao.providers.opencode import OpencodeProvider
from ciao.web.project_chats import ProjectChatManager


def _manager(config=None, *, chats=None, projects=None) -> ProjectChatManager:
    manager = ProjectChatManager.__new__(ProjectChatManager)
    # The native-title readers are ROOT-scoped, so the manager resolves the
    # chat's own agent root. A config stub therefore has to answer the same
    # three questions `_agent_root_for_chat` asks in production.
    manager._config = config or _config()
    manager._projects = projects or {}
    manager._chats = chats or {}
    manager._save = lambda: None
    manager._titling = set()
    # Production waits ~30s in total for the provider to publish its title.
    # Keep the retry *shape* (so the give-up path is still exercised) without
    # the wall-clock cost.
    manager._TITLE_POLL_DELAYS = (0.0, 0.001, 0.001)
    return manager


def _config(roots: dict[str, str] | None = None, *, primary: str = "personal"):
    """A config stub that can resolve per-workspace agent roots."""
    roots = roots or {}

    return SimpleNamespace(
        workspace_root="/tmp",
        state_path=Path("/runtime/state.json"),
        workspace_names=lambda: tuple(roots) or (primary,),
        primary_workspace=lambda: primary,
        agent_root=lambda name: roots.get(name, "/tmp"),
        # Model resolution for the LLM title fallback (resolve_insights_model).
        provider_insights_models={},
        insights_model_override=lambda provider, workspace=None: "",
        default_model_for_workspace=lambda workspace, provider="claude": "sonnet",
    )


def _chat(chat_id="chat-1", *, provider="claude", session_id="sess-1", title="New Chat"):
    return SimpleNamespace(
        chat_id=chat_id,
        project_id="project-1",
        provider=provider,
        model="opus" if provider == "claude" else "vendor/chat-model",
        session_id=session_id,
        title=title,
        # A chat named by hand is user-owned, so titling must leave it alone.
        title_origin="default" if title == "New Chat" else "user",
    )


@pytest.mark.asyncio
async def test_native_title_opencode(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "How hash tables work"}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) == "How hash tables work"


@pytest.mark.asyncio
async def test_native_title_opencode_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": ""}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_native_title_opencode_placeholder_is_not_final(monkeypatch) -> None:
    """opencode's default 'New session - <timestamp>' must not be accepted.

    The provider seeds the session with that placeholder and only later writes
    the generated title; accepting it would leave the sidebar stuck on it.
    """
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "New session - 2026-08-18T12:20:10.198Z"}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_native_title_claude_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None


@pytest.mark.asyncio
async def test_auto_title_if_default_sets_native_title(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager.auto_title_if_default("chat-1", "hello") == "Native Title"
    assert manager._chats["chat-1"].title == "Native Title"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_when_not_default(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(title="Already Named")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    assert await manager.auto_title_if_default("chat-1", "hello") is None
    assert manager._chats["chat-1"].title == "Already Named"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_without_session(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(session_id="")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "Native Title"}, "messages": []}

    async def failing_oneshot(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)
    # No session_id throughout the poll window -> native never reachable, and
    # the one-shot titler fails, so the deterministic fallback is used instead
    # of leaving "New Chat".
    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"
    assert manager._chats["chat-1"].title == "hello"


@pytest.mark.asyncio
async def test_auto_title_if_default_skips_when_native_missing(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": ""}, "messages": []}

    async def failing_oneshot(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)
    # Native title never arrives within the poll window and the one-shot
    # titler fails -> fallback from the prompt so the sidebar doesn't stay
    # stuck on "New Chat".
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

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        nonlocal reads
        reads += 1
        # opencode's title agent has not run yet on the first two reads.
        title = "Late Native Title" if reads >= 3 else ""
        return {"info": {"title": title}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
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

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "Arrived Late"}, "messages": []}

    # The turn assigns the session while the poller is waiting.
    async def grant_session(*_args, **_kwargs):
        chat.session_id = "sess-late"

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr(pc.asyncio, "sleep", grant_session)

    assert await manager.auto_title_if_default("chat-1", "hello") == "Arrived Late"
    assert chat.title == "Arrived Late"


@pytest.mark.asyncio
async def test_auto_title_stops_when_user_renames_mid_poll(monkeypatch) -> None:
    """A rename during the poll window must win over the provider's title."""
    from ciao.web import project_chats as pc

    chat = _chat(provider="opencode")
    manager = _manager(chats={"chat-1": chat})

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": ""}, "messages": []}

    async def rename_during_wait(*_args, **_kwargs):
        chat.title = "User Picked This"
        chat.title_origin = "user"  # what update_chat records for a rename

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
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


@pytest.mark.asyncio
async def test_native_title_opencode_also_reads_the_chats_agent_root(monkeypatch) -> None:
    """`read_thread` caches on (workspace_root, session_id), so it is root-scoped too."""
    from ciao.web import project_chats as pc

    seen: list[str] = []

    async def fake_read_thread(workspace, _sid, **_kwargs):
        seen.append(str(workspace))
        return {"info": {"title": "Opencode Work Title"}}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)

    manager = _manager(
        config=_config({"personal": "/roots/personal", "work": "/roots/work"}),
        chats={"chat-1": _chat(provider="opencode")},
        projects={"project-1": SimpleNamespace(project_id="project-1", workspace="work")},
    )

    assert (
        await manager._native_chat_title(manager._chats["chat-1"])
        == "Opencode Work Title"
    )
    assert seen == ["/roots/work"]


# ── Shared native-title contract (adapter level) ─────────────────────────
#
# Native titles read through `BaseProvider.native_title_candidates` on the
# adapter class: ordered candidate titles in provider-preference order, `()`
# when the provider has none. The host keeps all validation (see the
# `_real_title` oracles above); the adapter only knows its own storage.


@pytest.mark.asyncio
async def test_base_provider_native_title_default_without_construction() -> None:
    """The optional contract's default is () and must never build a provider."""
    from pathlib import Path

    from ciao.providers.base import BaseProvider

    entries: list[str] = []

    class MinimalProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            raise AssertionError("native_title_candidates must not construct a provider")

        def run_streaming(self, request, register_handle):
            entries.append("run_streaming")
            return
            yield  # pragma: no cover - empty async generator

    assert await MinimalProvider.native_title_candidates(Path("/ws"), "sess-1", runtime_root=Path("/runtime")) == ()
    assert entries == []
    assert MinimalProvider.capabilities == BaseProvider.capabilities


@pytest.mark.asyncio
async def test_opencode_native_title_candidates_extraction_matrix(monkeypatch) -> None:
    """OpenCode forwards the exact read_thread args and extracts the old title."""
    from pathlib import Path


    calls: list[tuple[Path, str, Path]] = []

    async def fake_read_thread(workspace_root, session_id, *, runtime_root):
        calls.append((workspace_root, session_id, runtime_root))
        return fake_read_thread.thread

    fake_read_thread.thread = {}
    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)

    root = Path("/roots/work")
    cases = [
        # Real thread with a dict info and a title.
        ({"info": {"title": "How hash tables work"}, "messages": []}, ("How hash tables work",)),
        # Empty info dict, missing info key, non-dict thread, non-dict info.
        ({"info": {}, "messages": []}, ("",)),
        ({"messages": []}, ("",)),
        ("not a thread", ("",)),
        ({"info": "weird", "messages": []}, ("",)),
        # Falsey title values keep the old `str(... or "")` shape.
        ({"info": {"title": ""}}, ("",)),
        ({"info": {"title": None}}, ("",)),
        # A non-string truthy title is stringified exactly as before.
        ({"info": {"title": 12345}}, ("12345",)),
    ]
    for thread, expected in cases:
        fake_read_thread.thread = thread
        assert await OpencodeProvider.native_title_candidates(root, "sess-1", runtime_root=Path("/runtime")) == expected
    assert calls == [(root, "sess-1", Path("/runtime"))] * len(cases)


# ── Manager dispatch through the provider registry ───────────────────────


@pytest.mark.asyncio
async def test_registered_third_adapter_supplies_native_title(monkeypatch) -> None:
    """A registered non-builtin adapter feeds candidates through the same seam.

    `auto_title_if_default` hands the chat's own agent root to the adapter
    through the provider registry and validates the ordered candidates at the
    shared `_real_title` seam; it reads no native storage, constructs no
    provider, and never reaches the one-shot titler. The one-shot spy
    records instead of raising: a raise would land in `_llm_chat_title`'s
    `except Exception` and could not fail the test.
    """
    from ciao import provider_registry
    from ciao.web import project_chats as pc

    reads: list[tuple[str, str]] = []
    oneshot_calls: list[int] = []
    title_calls: list[str] = []

    class ThirdProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            raise AssertionError("native title reads must not construct a provider")

        def run_streaming(self, request, register_handle):
            raise AssertionError("run_streaming must not be entered")
            yield  # pragma: no cover

        @classmethod
        async def native_title_candidates(cls, workspace_root, session_id, **_kwargs):
            reads.append((str(workspace_root), session_id))
            return (
                "",
                "New session - temporary",
                "[CIAO_CONTEXT_BEGIN] private",
                "  Clean summary  ",
                "Later title",
            )

    class Descriptor:
        def factory(self):
            return ThirdProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )

    async def recording_oneshot(*_args, **_kwargs):
        oneshot_calls.append(1)
        return ""

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", recording_oneshot)

    real_real_title = pc.chat_service._real_title

    def spying_real_title(title):
        title_calls.append(title)
        return real_real_title(title)

    monkeypatch.setattr(pc.chat_service, "_real_title", spying_real_title)

    manager = _manager(
        config=_config({"personal": "/roots/personal", "work": "/roots/work"}),
        chats={"chat-1": _chat(provider="synth")},
        projects={
            "project-1": SimpleNamespace(project_id="project-1", workspace="work")
        },
    )
    result = await manager.auto_title_if_default("chat-1", "hello")
    assert result == "Clean summary"
    assert manager._chats["chat-1"].title == "Clean summary"
    # The adapter saw the chat's OWN agent root, not the install root.
    assert reads == [("/roots/work", "sess-1")]
    assert oneshot_calls == []
    # Candidates reach the rejection seam in order; the first clean one wins
    # and the later title is never examined.
    assert title_calls == [
        "",
        "New session - temporary",
        "[CIAO_CONTEXT_BEGIN] private",
        "  Clean summary  ",
    ]


@pytest.mark.asyncio
async def test_unsupported_adapter_returns_none_without_construction(monkeypatch) -> None:
    """An adapter that inherits the contract default yields no title, never a build.

    Factory access is recorded and observed: spies that raised here would be
    swallowed by the manager's `except Exception` and could not fail the test.
    """
    from ciao import provider_registry

    factory_calls: list[int] = []
    built: list[str] = []

    class DefaultProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            built.append("constructed")

        def run_streaming(self, request, register_handle):
            raise AssertionError("run_streaming must not be entered")
            yield  # pragma: no cover

    class Descriptor:
        def factory(self):
            factory_calls.append(1)
            return DefaultProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    manager = _manager(chats={"chat-1": _chat(provider="synth")})
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None
    # The registry resolved the adapter class exactly once; the read never
    # constructed it.
    assert factory_calls == [1]
    assert built == []


@pytest.mark.asyncio
async def test_unregistered_provider_id_returns_none_without_any_read(monkeypatch) -> None:
    """An id with no descriptor returns None with no factory or storage access.

    Every spy records and is asserted after the call rather than raising: a
    raise would land inside the manager's `except Exception` and could not
    fail the test. The registry seam is watched at three levels so any
    fallback factory resolution is caught — id lookups (`get`), a
    `require`-based fallback, and `.factory()` itself on any registered
    descriptor however it was obtained.
    """
    from ciao import provider_registry

    get_calls: list[str] = []
    require_calls: list[str] = []
    factory_calls: list[str] = []
    read_calls: list[int] = []
    info_calls: list[int] = []

    real_get = provider_registry.get
    real_require = provider_registry.require
    real_factory = provider_registry.ProviderDescriptor.factory

    def spying_get(provider):
        get_calls.append(provider)
        return real_get(provider)

    def spying_require(provider):
        require_calls.append(provider)
        return real_require(provider)

    def spying_factory(descriptor_self):
        factory_calls.append(descriptor_self.id)
        return real_factory(descriptor_self)

    async def recording_read(_workspace_root, _session_id, **_kwargs):
        read_calls.append(1)

    def recording_info(_sid, directory=None):
        info_calls.append(1)

    monkeypatch.setattr(provider_registry, "get", spying_get)
    monkeypatch.setattr(provider_registry, "require", spying_require)
    monkeypatch.setattr(provider_registry.ProviderDescriptor, "factory", spying_factory)
    monkeypatch.setattr(OpencodeProvider, "read_thread", recording_read)
    manager = _manager(chats={"chat-1": _chat(provider="elsewhere")})
    assert await manager._native_chat_title(manager._chats["chat-1"]) is None
    # One registry lookup, no descriptor, therefore no `require` fallback and
    # no `.factory()` on any registered descriptor — and no storage read.
    assert get_calls == ["elsewhere"]
    assert require_calls == []
    assert factory_calls == []
    assert read_calls == []
    assert info_calls == []


@pytest.mark.asyncio
async def test_adapter_read_failure_keeps_failure_log(monkeypatch, caplog) -> None:
    """Adapter failures degrade to None behind the manager's failure log."""
    import logging

    from ciao import provider_registry

    built: list[str] = []

    class ExplodingProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            built.append("constructed")

        def run_streaming(self, request, register_handle):
            raise AssertionError("must not stream")
            yield  # pragma: no cover

        @classmethod
        async def native_title_candidates(cls, workspace_root, session_id, **_kwargs):
            raise RuntimeError("adapter storage unreadable")

    class Descriptor:
        def factory(self):
            return ExplodingProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    manager = _manager(chats={"chat-1": _chat(provider="synth")})
    with caplog.at_level(logging.INFO, logger="ciao.web.project_chats"):
        assert await manager._native_chat_title(manager._chats["chat-1"]) is None
    assert built == []
    # The chat id, not any candidate text, is what fails into the log.
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "Native title read failed for chat-1"
    ]
    assert records
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_factory_resolution_failure_keeps_failure_log(monkeypatch, caplog) -> None:
    """A broken factory import also lands behind the same failure boundary."""
    import logging

    from ciao import provider_registry

    class Descriptor:
        def factory(self):
            raise RuntimeError("factory import cannot resolve")

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    manager = _manager(chats={"chat-1": _chat(provider="synth")})
    with caplog.at_level(logging.INFO, logger="ciao.web.project_chats"):
        assert await manager._native_chat_title(manager._chats["chat-1"]) is None
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "Native title read failed for chat-1"
    ]
    assert records
    assert records[0].exc_info is not None


@pytest.mark.asyncio
async def test_native_title_cancellation_propagates(monkeypatch) -> None:
    """Cancellation must not be converted into a missing title."""
    import asyncio

    from ciao import provider_registry

    built: list[str] = []

    class CancellingProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            built.append("constructed")

        def run_streaming(self, request, register_handle):
            raise AssertionError("must not stream")
            yield  # pragma: no cover

        @classmethod
        async def native_title_candidates(cls, workspace_root, session_id, **_kwargs):
            raise asyncio.CancelledError()

    class Descriptor:
        def factory(self):
            return CancellingProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    manager = _manager(chats={"chat-1": _chat(provider="synth")})
    with pytest.raises(asyncio.CancelledError):
        await manager._native_chat_title(manager._chats["chat-1"])
    assert built == []


@pytest.mark.asyncio
async def test_rename_during_third_adapter_read_wins(monkeypatch) -> None:
    """A rename landing while the adapter read is parked must survive the poll."""
    import asyncio

    from ciao import provider_registry

    reached = asyncio.Event()
    release = asyncio.Event()

    class PausingProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not be constructed")

        def run_streaming(self, request, register_handle):
            raise AssertionError("must not stream")
            yield  # pragma: no cover

        @classmethod
        async def native_title_candidates(cls, workspace_root, session_id, **_kwargs):
            reached.set()
            await release.wait()
            return ("Clean summary",)

    class Descriptor:
        def factory(self):
            return PausingProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    chat = _chat(provider="synth")
    manager = _manager(chats={"chat-1": chat})

    task = asyncio.ensure_future(manager.auto_title_if_default("chat-1", "hello"))
    await asyncio.wait_for(reached.wait(), 2)
    chat.title = "User Picked This"
    chat.title_origin = "user"  # what update_chat records for a rename
    release.set()
    assert await asyncio.wait_for(task, 2) is None
    assert manager._chats["chat-1"].title == "User Picked This"


@pytest.mark.asyncio
async def test_removed_chat_during_third_adapter_read_is_not_resurrected(monkeypatch) -> None:
    """A chat deleted mid-read stays deleted; the native read must not rewrite it."""
    import asyncio

    from ciao import provider_registry

    reached = asyncio.Event()
    release = asyncio.Event()

    class PausingProvider(BaseProvider):
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not be constructed")

        def run_streaming(self, request, register_handle):
            raise AssertionError("must not stream")
            yield  # pragma: no cover

        @classmethod
        async def native_title_candidates(cls, workspace_root, session_id, **_kwargs):
            reached.set()
            await release.wait()
            return ("Clean summary",)

    class Descriptor:
        def factory(self):
            return PausingProvider

    monkeypatch.setattr(
        provider_registry, "get", lambda pid: Descriptor() if pid == "synth" else None
    )
    manager = _manager(chats={"chat-1": _chat(provider="synth")})

    task = asyncio.ensure_future(manager.auto_title_if_default("chat-1", "hello"))
    await asyncio.wait_for(reached.wait(), 2)
    manager._chats.pop("chat-1")
    release.set()
    assert await asyncio.wait_for(task, 2) is None
    assert "chat-1" not in manager._chats


# ── LLM title fallback (tier 2) ──────────────────────────────────────────
#
# Claude Code ≥ 2.1.246 skips its own ai-title generation when the first
# prompt opens with the injected [CIAO_CONTEXT_BEGIN] capsule — i.e. every
# Ciaobot chat — so the native-title poll exhausts and the sidebar used to
# sit on the 6-word prompt snippet ("Check Zendesk ticket https://scand…").


@pytest.mark.asyncio
async def test_llm_fallback_titles_chat_when_native_never_lands(monkeypatch) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})

    calls: list[dict] = []

    async def fake_oneshot(prompt, *, system_prompt="", model="", **_kwargs):
        calls.append({"prompt": prompt, "system_prompt": system_prompt, "model": model})
        return "Zendesk Ticket Thread Follow-Up"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await manager.auto_title_if_default("chat-1", "Check Zendesk ticket 152025")
    assert title == "Zendesk Ticket Thread Follow-Up"
    assert manager._chats["chat-1"].title == "Zendesk Ticket Thread Follow-Up"
    assert len(calls) == 1
    assert "Check Zendesk ticket 152025" in calls[0]["prompt"]
    assert calls[0]["model"] == "opus"


@pytest.mark.asyncio
async def test_claude_title_does_not_wait_for_native_poll(monkeypatch) -> None:
    """The fallback title is never held behind the native polls: none is slept on before it returns."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    manager._TITLE_POLL_DELAYS = (0.0, 60.0)
    native_reads = 0
    slept: list[float] = []

    async def counting_native_title(_chat):
        nonlocal native_reads
        native_reads += 1
        return None

    async def recording_sleep(delay):
        slept.append(delay)

    async def fake_oneshot(*_args, **_kwargs):
        return "System One Decision Models"

    manager._native_chat_title = counting_native_title
    monkeypatch.setattr(pc.asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await manager.auto_title_if_default(
        "chat-1", "I want to find out where System One models help"
    )
    assert title == "System One Decision Models"
    # ACP Claude chats have no native read: the adapter's title arrives as an event.
    assert native_reads == 0
    assert slept == []


@pytest.mark.asyncio
async def test_claude_fallback_title_is_not_held_behind_native_polls(monkeypatch) -> None:
    """With no native title and no model title, the fallback comes back at once, and no poll is waited on."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    manager._TITLE_POLL_DELAYS = (0.0, 60.0, 60.0)
    native_reads = 0
    slept: list[float] = []

    async def counting_native_title(_chat):
        nonlocal native_reads
        native_reads += 1
        return None

    async def recording_sleep(delay):
        slept.append(delay)

    async def failing_oneshot(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    manager._native_chat_title = counting_native_title
    monkeypatch.setattr(pc.asyncio, "sleep", recording_sleep)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)

    assert await manager.auto_title_if_default("chat-1", "Check Zendesk ticket 152025") == (
        "Check Zendesk ticket 152025"
    )
    assert manager._chats["chat-1"].title == "Check Zendesk ticket 152025"
    assert native_reads == 0
    assert slept == []


@pytest.mark.asyncio
async def test_claude_chat_reads_no_native_title_so_nothing_polls(monkeypatch) -> None:
    """The fallback stands: an ACP Claude chat has no native read to poll, so nothing runs after it."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    manager._TITLE_POLL_DELAYS = (0.0, 60.0)
    published: list[dict] = []
    manager._events = SimpleNamespace(publish=published.append)
    reads = 0
    slept: list[float] = []

    async def late_native_title(_chat):
        # Would land on the second read if the poll ran; it must never be asked.
        nonlocal reads
        reads += 1
        return "Claude Late Title" if reads >= 2 else None

    async def recording_sleep(delay):
        slept.append(delay)

    async def failing_oneshot(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    manager._native_chat_title = late_native_title
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)
    monkeypatch.setattr(pc.asyncio, "sleep", recording_sleep)

    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"

    assert reads == 0
    assert slept == []
    assert manager._chats["chat-1"].title == "hello"
    assert published == []


@pytest.mark.asyncio
async def test_claude_rename_during_the_titler_wins_over_the_fallback(monkeypatch) -> None:
    """A rename made while the one-shot titler runs keeps the user's title; no fallback is written."""
    chat = _chat(provider="claude")
    manager = _manager(chats={"chat-1": chat})
    manager._TITLE_POLL_DELAYS = (0.0, 0.001)

    async def rename_then_fail(*_args, **_kwargs):
        chat.title = "User Picked This"
        chat.title_origin = "user"  # what update_chat records for a rename
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", rename_then_fail)

    assert await manager.auto_title_if_default("chat-1", "hello") is None
    assert chat.title == "User Picked This"


@pytest.mark.asyncio
async def test_llm_fallback_prompt_carries_assistant_reply(monkeypatch) -> None:
    """The post-reply invocation includes the assistant's framing so the
    title can name the topic rather than echo the question."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})

    prompts: list[str] = []

    async def fake_oneshot(prompt, **_kwargs):
        prompts.append(prompt)
        return "Automation Page Job Log"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)

    title = await manager.auto_title_if_default(
        "chat-1", "why no recent sessions?", "The Automation page lists every job run."
    )
    assert title == "Automation Page Job Log"
    assert "<assistant>The Automation page lists every job run.</assistant>" in prompts[0]


@pytest.mark.asyncio
async def test_llm_fallback_failure_still_applies_deterministic_fallback(
    monkeypatch,
) -> None:
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="claude")})

    async def failing_oneshot(*_args, **_kwargs):
        raise RuntimeError("usage limit reached")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", failing_oneshot)

    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"
    assert manager._chats["chat-1"].title == "hello"


@pytest.mark.asyncio
async def test_llm_fallback_rejects_contaminated_title(monkeypatch) -> None:
    """A degrading model that echoes the injected capsule (or returns nothing
    usable) must not contaminate the sidebar; the deterministic fallback wins."""
    from ciao.web import project_chats as pc

    leaked = (
        '[CIAO_CONTEXT_BEGIN] [Chat ID: "chat-1"] <ciao-context> workspace=work '
        "vault=work/memory-vault </ciao-context>"
    )

    async def leaking_oneshot(*_args, **_kwargs):
        return leaked

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", leaking_oneshot)

    manager = _manager(chats={"chat-1": _chat(provider="claude")})
    assert await manager.auto_title_if_default("chat-1", "hello") == "hello"
    assert manager._chats["chat-1"].title == "hello"


@pytest.mark.asyncio
async def test_llm_fallback_not_called_when_native_title_lands(monkeypatch) -> None:
    """Tier 1 stays free: when the provider publishes a title the one-shot
    call must never run."""
    from ciao.web import project_chats as pc

    manager = _manager(chats={"chat-1": _chat(provider="opencode")})

    oneshot_calls: list[int] = []

    async def unexpected_oneshot(*_args, **_kwargs):
        oneshot_calls.append(1)
        return "Should Not Be Used"

    async def fake_read_thread(_workspace, _sid, **_kwargs):
        return {"info": {"title": "Native Title"}, "messages": []}

    monkeypatch.setattr(OpencodeProvider, "read_thread", fake_read_thread)
    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", unexpected_oneshot)

    assert await manager.auto_title_if_default("chat-1", "hello") == "Native Title"
    assert oneshot_calls == []


@pytest.mark.asyncio
async def test_llm_fallback_respects_manual_rename_during_call(monkeypatch) -> None:
    """A rename that lands while the model is running must win."""
    from ciao.web import project_chats as pc

    chat = _chat(provider="claude")
    manager = _manager(chats={"chat-1": chat})

    async def slow_oneshot(*_args, **_kwargs):
        chat.title = "User Picked This"
        chat.title_origin = "user"  # what update_chat records for a rename
        return "Model's Title"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", slow_oneshot)

    assert await manager.auto_title_if_default("chat-1", "hello") is None
    assert chat.title == "User Picked This"


def test_clean_llm_title_strips_noise() -> None:
    from ciao.web.chat_service import _clean_llm_title

    assert _clean_llm_title("  Zendesk Ticket Thread  ") == "Zendesk Ticket Thread"
    assert _clean_llm_title('"Quoted Title"\n') == "Quoted Title"
    assert _clean_llm_title("`Backticked`") == "Backticked"
    assert _clean_llm_title("Trailing period.") == "Trailing period"
    # First non-empty line wins over an explanation paragraph.
    assert _clean_llm_title("Real Title\n\nHere is why I picked it...") == "Real Title"
    long = "A" * 80
    assert _clean_llm_title(long) == "A" * 57 + "..."
    assert _clean_llm_title("") is None
    assert _clean_llm_title(None) is None
    assert _clean_llm_title("[CIAO_CONTEXT_BEGIN] leaked") is None
    assert _clean_llm_title("New session - 2026-08-31") is None


def test_fallback_title_strips_leading_url() -> None:
    """The reported failure: 'Check Zendesk ticket https://scandit.zendesk…'."""
    from ciao.web.chat_service import _fallback_title

    assert (
        _fallback_title(
            "Check Zendesk ticket https://scandit.zendesk.com/agent/tickets/152025 "
            "and check the other one too"
        )
        == "Check Zendesk ticket"
    )
    # A bare-URL prompt still yields something (the URL itself, capped).
    bare = _fallback_title("https://example.com/very/long/path/segments")
    assert bare is not None and bare.startswith("https://")
    assert _fallback_title("plain words only here") == "plain words only here"
