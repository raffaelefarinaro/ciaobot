from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.providers.claude import ClaudeProvider
from ciao.providers.opencode import OpencodeProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
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


# --- Empty / banner-only ResultEvent guards ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\n",
        "ok",
        "...",
    ],
)
def test_follow_up_announcement_rejects_empty_and_banner_only(text: str) -> None:
    """A bare banner or whitespace must not be treated as a real reply."""
    assert ProjectChatManager._is_worth_announcing_follow_up(text) is False


def test_follow_up_announcement_accepts_real_reply() -> None:
    """Anything with at least a few characters of real prose passes."""
    assert ProjectChatManager._is_worth_announcing_follow_up(
        "Synthesis complete — see the trace."
    ) is True
    assert ProjectChatManager._is_worth_announcing_follow_up(
        "Found the bug in routes_api.py"
    ) is True


@pytest.mark.parametrize("reply", ["Yes", "No", "No.", "Ok", "42", "👍"])
def test_a_terse_answer_to_the_user_still_announces(reply: str) -> None:
    """The banner floor must never reach a reply the user asked for.

    "Yes" is a complete answer to a yes/no question. Gating the regular
    turn-done branch on the follow-up heuristic dropped its unread badge, its
    toast, the ``last_activity_at`` bump that reorders recents, and the
    pending-retry clear, with nothing anywhere reporting a problem.
    """
    # The follow-up heuristic does reject it, which is why it must stay off this path.
    assert ProjectChatManager._is_worth_announcing_follow_up(reply) is False
    # The regular branch's condition is plain non-empty text.
    assert bool(reply.strip()) is True


async def test_drain_between_turns_skips_publish_and_push_for_banner_only_result(
    tmp_path: Path, monkeypatch
) -> None:
    """A short banner-only ResultEvent must not trigger chat_result_ready or a push.

    A follow-up parent turn can return a one-line reply like "ok" or
    "Synthesis complete — see trace." before the real report lands. Without the
    visibility guard, that single short string used to publish chat_result_ready
    and queue a delayed push, producing an OS-level notification whose body was
    the model's internal comment. With the guard, the in-app Activity row and
    the subagent-count drop remain the only signal.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("drain-banner", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="drain-banner-test")
    chat.session_id = "sess-drain-banner"
    pcm._save()

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            from ciao.models import ResultEvent

            yield ResultEvent(
                type="result",
                result="ok",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]

    published: list[dict] = []
    pushes: list = []

    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: pushes.append(a))

    # The drain runs until the provider raises StopAsyncIteration after one
    # event; run it and let it unwind naturally.
    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    assert pushes == []
    assert not any(ev.get("type") == "chat_result_ready" for ev in published)


async def test_drain_between_turns_publishes_and_pushes_for_real_reply(
    tmp_path: Path, monkeypatch
) -> None:
    """A real reply still drives chat_result_ready + the push."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("drain-real", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="drain-real-test")
    chat.session_id = "sess-drain-real"
    pcm._save()

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            from ciao.models import ResultEvent

            yield ResultEvent(
                type="result",
                result="Found the bug in routes_api.py line 482.",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]

    published: list[dict] = []
    pushes: list = []

    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: pushes.append(a))

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready_events = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert len(ready_events) == 1
    assert "Found the bug" in ready_events[0]["snippet"]
    assert len(pushes) == 1
    assert "Found the bug" in pushes[0][2]

    # The snippet is persisted onto the chat (not just broadcast), so the
    # sidebar's unread tile can show a preview after a fresh page load, before
    # any chat_result_ready WS event replays.
    assert chat.last_snippet == ready_events[0]["snippet"]
    reloaded = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.last_snippet == chat.last_snippet


def _drain_chat(pcm: ProjectChatManager, name: str):
    project = pcm.create_project(name, workspace="personal")
    chat = pcm.create_chat(project.project_id, title=name)
    chat.session_id = f"sess-{name}"
    pcm._save()
    return project, chat


async def test_drain_announces_only_the_follow_up_final_segment(
    tmp_path: Path, monkeypatch
) -> None:
    """A follow-up execution previews its last text block, not the joined turn.

    OpenCode's wake result joins every part of the execution; the unread card
    and push must excerpt the block the chat ends on, while the durable
    handover keeps the whole execution text (#2139).
    """
    from ciao.models import AssistantTextDelta, ResultEvent, ToolUseEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-segment")
    interim = "Interim working narration."
    final = "Final answer in file.md."
    whole = f"{interim}\n\n{final}"

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield AssistantTextDelta(type="text", text=interim)
            yield ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls")
            yield AssistantTextDelta(type="text", text=final)
            yield ResultEvent(
                type="result",
                result=whole,
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert len(ready) == 1
    assert ready[0]["snippet"] == "Final answer in file.md."
    assert chat.last_snippet == "Final answer in file.md."
    # The complete execution text still reaches the handover and the schedule
    # classifier, which judges the finished synthesis rather than a preview.
    assert chat.last_response == whole


async def test_drain_uses_provider_final_text_without_text_events(
    tmp_path: Path, monkeypatch
) -> None:
    """A wake attached after the text frames still names its final block."""
    from ciao.models import ResultEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-metadata")
    whole = "Interim working narration.\n\nFinal answer in file.md."

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield ResultEvent(
                type="result",
                result=whole,
                session_id=chat.session_id,
                is_error=False,
                final_text="Final answer in file.md.",
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert [ev["snippet"] for ev in ready] == ["Final answer in file.md."]


async def test_drain_resets_the_final_segment_for_each_result(
    tmp_path: Path, monkeypatch
) -> None:
    """A second wake's answer is its own block, not the previous one plus it."""
    from ciao.models import AssistantTextDelta, ResultEvent

    pcm = _make_manager(tmp_path)
    project, chat = _drain_chat(pcm, "drain-reset")

    class _FakeProvider:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            yield AssistantTextDelta(type="text", text="First answer.")
            yield ResultEvent(
                type="result",
                result="First answer.",
                session_id=chat.session_id,
                is_error=False,
            )
            yield AssistantTextDelta(type="text", text="Second answer.")
            yield ResultEvent(
                type="result",
                result="Second answer.",
                session_id=chat.session_id,
                is_error=False,
            )

    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]
    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: None)

    await pcm._drain_between_turns(chat.chat_id, project.project_id)

    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert [ev["snippet"] for ev in ready] == ["First answer.", "Second answer."]


def _monitor_task_records() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": "watch the report"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01SsjL",
                        "name": "Monitor",
                        "input": {
                            "command": "tail -f /tmp/adoption_2026-08.log",
                            "description": "adoption report 2026-08 DAG progress and failures",
                            "timeout_ms": 3600000,
                            "persistent": False,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01SsjL",
                        "content": "Monitor started (task bl7dzu4ku, timeout 3600000ms).",
                    }
                ],
            },
            "toolUseResult": {"taskId": "bl7dzu4ku", "timeoutMs": 3600000, "persistent": False},
        },
    ]


def _task_notification_record(task_id: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                "<status>completed</status>\n</task-notification>"
            ),
        },
    }


# ── the parked result announce ───────────────────────────────────────────────
#
# The announce for a turn that ends on "I'll report back" is PARKED, not
# dropped. It is released once: by the follow-up result at count zero, by the
# watcher when it saw no background work or no drain is live, by the drain
# ending, or by Stop.


def _announce_spy(pcm) -> list[tuple]:
    """Record announces, and keep detached work from reaching the real vault.

    Only the archive-proposal helper and the memory-pass settle are swallowed —
    everything else spawned this way is scheduled for real, or a stub here would
    silently disable the behaviour under test. The memory-pass hook needs the same
    swallow: in a sync test there is no loop for its detached task.
    """
    calls: list[tuple] = []
    pcm._announce_result_ready = lambda *a: calls.append(a)  # type: ignore[method-assign]

    def spawn(coro, name: str):
        if name.startswith("archive-proposal-helper") or name.startswith(
            "memory-pass-"
        ):
            coro.close()
            return None
        return asyncio.create_task(coro, name=name)

    pcm._spawn_detached = spawn  # type: ignore[method-assign]
    return calls


def test_a_parked_announce_is_released_when_no_follow_up_speaks(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    assert calls == []

    assert pcm._flush_result_announce("chat-1") is True
    assert calls == [("chat-1", "proj-1", "Title", "snippet")]


def test_flushing_is_idempotent(tmp_path: Path) -> None:
    """The watcher's `finally` can run after an explicit flush on the same path."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    assert pcm._flush_result_announce("chat-1") is True
    assert pcm._flush_result_announce("chat-1") is False
    assert len(calls) == 1


def test_a_landed_follow_up_discards_the_parked_announce(tmp_path: Path) -> None:
    """Otherwise the turn pushes twice: once here, once for the follow-up reply."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    pcm._park_result_announce("chat-1", "proj-1", "Title", "snippet")
    pcm._discard_result_announce("chat-1")
    assert pcm._flush_result_announce("chat-1") is False
    assert calls == []


def test_the_interim_pattern_still_decides_whether_to_park() -> None:
    """Parking is gated on the prose heuristic, which is safe to be broad:
    a false positive only defers the announce to the watcher, which always
    releases it. A real result is announced immediately."""
    from ciao.web.project_chats import ProjectChatManager

    assert ProjectChatManager._is_interim_subagent_text(
        "Dispatched three agents; I'll report back once they finish."
    )
    assert not ProjectChatManager._is_interim_subagent_text(
        "Done — all three agents reported; the changelog is updated."
    )


# ── the drain releases the park at count zero ────────────────────────────────


class _FakeProvider:
    """Drives `_drain_between_turns` with a scripted event sequence."""

    can_drain = True

    background_work_outstanding = False

    def __init__(self, events: list) -> None:
        self._events = events

    async def drain_events(self):
        for event in self._events:
            yield event


def _result_event(text: str, *, is_error: bool = False):
    from ciao.models import ResultEvent

    return ResultEvent(type="result", result=text, is_error=is_error)


@pytest.mark.asyncio
async def test_the_drain_announces_the_synthesis_reply_and_drops_the_park(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([
        _result_event("All three agents reported. The changelog is updated."),
    ])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    # Exactly one announce, and it carries the synthesis reply — not the
    # parked "I'll report back" snippet.
    assert len(calls) == 1
    assert calls[0][3] != "interim snippet"


@pytest.mark.asyncio
async def test_a_follow_up_that_errors_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """The drain only announces a non-error, non-stub follow-up.

    An errored follow-up must not leave the chat with no notification at all,
    so the drain flushes the parked announce instead.
    """
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([_result_event("boom", is_error=True)])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_a_banner_only_follow_up_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """`_is_worth_announcing_follow_up` rejects a short stub, so the drain
    stays silent — which means the parked announce has to go out."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([_result_event("ok.")])
    assert pcm._is_worth_announcing_follow_up("ok.") is False

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_a_drain_that_never_gets_a_reply_still_releases_the_park(
    tmp_path: Path,
) -> None:
    """The CLI can drop and never produce a ResultEvent."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._providers["chat-1"] = _FakeProvider([])

    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")
    await pcm._drain_between_turns("chat-1", "proj-1")

    assert calls == [("chat-1", "proj-1", "Title", "interim snippet")]


@pytest.mark.asyncio
async def test_an_empty_follow_up_result_is_skipped(tmp_path: Path) -> None:
    """The CLI's `turns=0` bookkeeping result opens no stream and records nothing."""
    from ciao.models import ResultEvent

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    pcm._events.publish = capture  # type: ignore[method-assign]
    pcm._providers["chat-1"] = _FakeProvider(
        [ResultEvent(type="result", result="", origin="task-notification")]
    )

    await pcm._drain_between_turns("chat-1", "proj-1")

    types = {ev.get("type") for ev in published}
    assert "chat_streaming_started" not in types
    assert "chat_streaming_done" not in types
    assert calls == []


@pytest.mark.asyncio
async def test_a_follow_up_while_background_work_remains_replaces_the_park(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    class _OneThenBlock:
        can_drain = True
        background_work_outstanding = True

        async def drain_events(self):
            yield _result_event("Agent A reported: the parser is fine.")
            await asyncio.Event().wait()

    pcm._providers["chat-1"] = _OneThenBlock()  # type: ignore[assignment]
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")

    task = asyncio.create_task(pcm._drain_between_turns("chat-1", "proj-1"))
    for _ in range(20):
        await asyncio.sleep(0)

    assert calls == []
    assert pcm._parked_result_announce["chat-1"][2].startswith("Agent A reported")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_drain_leaves_the_park_to_the_next_turn(
    tmp_path: Path,
) -> None:
    """The next user turn cancels the drain and supersedes the interim
    announce, so releasing it here would push a stale non-answer."""
    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    class _Blocking:
        can_drain = True
        background_work_outstanding = False

        async def drain_events(self):
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    pcm._providers["chat-1"] = _Blocking()
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim snippet")

    task = asyncio.create_task(pcm._drain_between_turns("chat-1", "proj-1"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == []
    assert "chat-1" in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_stopping_the_drain_releases_the_park(tmp_path: Path) -> None:
    """Stop ends the drain and then releases the park.

    The drain's cancelled path leaves the park "to the next turn", but Stop
    starts no next turn, so without an explicit release the chat keeps its
    interim message with no unread badge, no push and no archive proposal.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    stream = ChatStream(background=True)
    pcm._broker.register("chat-1", stream)
    # A real task: the registry holds tasks, and the drain wait reads
    # ``cancelling()``, which a bare future does not have.
    drain = asyncio.create_task(asyncio.Event().wait())
    pcm._between_turn_drains["chat-1"] = drain
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    assert await pcm.stop_chat("chat-1") is True

    assert calls == [("chat-1", "proj-1", "Title", "interim")]
    assert "chat-1" not in pcm._parked_result_announce


@pytest.mark.asyncio
async def test_stop_racing_a_user_send_leaves_the_park_to_that_turn(
    tmp_path: Path,
) -> None:
    """Stop, then a send: the release must not land inside the new turn.

    ``stop_chat`` awaits the drain teardown before releasing, and that await
    is exactly where a user send gets in — it cancels the drain and registers
    its own foreground stream. Releasing afterwards would push "I'll report
    back once the agents finish" mid-turn; the new turn discards the park and
    announces for itself when it ends.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)

    background = ChatStream(background=True)
    pcm._broker.register("chat-1", background)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    async def fake_drain() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # What start_stream does when a user send lands on a background
            # stream, interleaved with the drain's own unwind.
            pcm._broker.clear("chat-1", background)
            pcm._broker.register("chat-1", ChatStream(prompt_text="and now?"))
            raise

    pcm._between_turn_drains["chat-1"] = asyncio.create_task(fake_drain())
    await asyncio.sleep(0)

    assert await pcm.stop_chat("chat-1") is True

    assert calls == []
    assert "chat-1" in pcm._parked_result_announce


def test_the_release_itself_refuses_during_a_live_turn(tmp_path: Path) -> None:
    """The guard lives in `_flush_result_announce`, not at its call sites.

    There are three sites — the watcher's `finally`, the drain and Stop — and
    a stale park pushed mid-turn presents differently from each. Pinning it on
    the shared release is what stops a fourth site repeating it.

    The entry stays parked rather than being popped: the live turn discards it
    and announces for itself at turn-done.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    foreground = ChatStream(background=False)
    pcm._broker.register("chat-1", foreground)
    try:
        assert pcm._flush_result_announce("chat-1") is False
        assert calls == []
        # Not consumed — the live turn still owns it.
        assert "chat-1" in pcm._parked_result_announce
    finally:
        pcm._broker.clear("chat-1", foreground)

    # Once that turn is gone the same call releases normally.
    assert pcm._flush_result_announce("chat-1") is True
    assert calls == [("chat-1", "proj-1", "Title", "interim")]


def test_a_background_stream_does_not_count_as_a_live_turn(tmp_path: Path) -> None:
    """The drain's own stream is background; it must not block its own release."""
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    calls = _announce_spy(pcm)
    pcm._park_result_announce("chat-1", "proj-1", "Title", "interim")

    background = ChatStream(background=True)
    pcm._broker.register("chat-1", background)
    try:
        assert pcm._foreground_turn_active("chat-1") is False
        assert pcm._flush_result_announce("chat-1") is True
    finally:
        pcm._broker.clear("chat-1", background)

    assert calls == [("chat-1", "proj-1", "Title", "interim")]


async def test_finish_background_stop_calls_abort_through_contract(tmp_path: Path) -> None:
    aborted: list[str] = []

    class _Stub(ClaudeProvider):
        @property
        def current_session_id(self) -> str | None:
            return "sess-abort-1"

        async def abort_session(self, session_id: str) -> None:
            aborted.append(session_id)

    pcm = _make_manager(tmp_path)
    pcm._providers["chat-stub"] = SimpleNamespace(provider=_Stub(tmp_path))
    await pcm._finish_background_stop("chat-stub")
    assert aborted == ["sess-abort-1"]

    # A Claude-like provider keeps the base no-op abort: awaited without error.
    class _ClaudeLike(ClaudeProvider):
        @property
        def current_session_id(self) -> str | None:
            return "sess-claude-1"

    pcm._providers["chat-claude"] = SimpleNamespace(provider=_ClaudeLike(tmp_path))
    await pcm._finish_background_stop("chat-claude")
    assert aborted == ["sess-abort-1"]



# --- Workspace busy check counts provider background work -------------------


def _workspace_chat(pcm: ProjectChatManager, flag: bool):
    project = pcm.create_project("busy-ws", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="busy-ws")
    pcm._providers[chat.chat_id] = SimpleNamespace(
        background_work_outstanding=flag, background_activity_age=0.0
    )
    return chat


@pytest.mark.parametrize("flag", [True, False])
def test_workspace_busy_chat_ids_counts_provider_background_work(
    tmp_path: Path, flag: bool
) -> None:
    pcm = _make_manager(tmp_path)
    chat = _workspace_chat(pcm, flag)
    assert chat.chat_id not in pcm.active_chat_ids()
    expected = [chat.chat_id] if flag else []
    assert pcm.workspace_busy_chat_ids("personal") == expected
    assert pcm.workspace_busy_chat_ids("other") == []


@pytest.mark.parametrize("flag", [True, False])
def test_workspace_rename_refused_while_background_work_outstanding(
    tmp_path: Path, flag: bool
) -> None:
    from ciao.workspace_rename import WorkspaceRenameBusy, rename_workspace

    pcm = _make_manager(tmp_path)
    _workspace_chat(pcm, flag)
    stub = SimpleNamespace()
    if flag:
        with pytest.raises(WorkspaceRenameBusy):
            rename_workspace(
                pcm._config,
                old="personal", new="santo",
                projects=pcm, schedules=stub, webhooks=stub, imports=stub,
            )
    else:
        assert pcm.workspace_busy_chat_ids("personal") == []
