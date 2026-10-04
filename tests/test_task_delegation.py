"""The control-plane delegation lifecycle in ``ciao.control_plane`` (#1033, B5).

``ciao.task_attempts.py`` holds the durable records; this file is about the
decisions only the service can make, and the four properties the plan binds:

- **No implicit bypass.** Delegation calls ``pcm.start_stream(chat_id, prompt)``
  and never names ``unattended``. The recording fake asserts the call carries
  *exactly* chat and prompt — so an approval card the delegated turn raises is an
  ordinary Needs-you card, and a later "just this once" cannot add the flag
  quietly.
- **Durable before the side effect.** The attempt and the task's linkage are both
  written before the turn starts, and the recording fake proves it by reading the
  store and the task file at the moment ``start_stream`` is called.
- **One attempt, one chat.** A double click returns the attempt that already
  exists and creates no second chat, and the reply says which happened.
- **A finished turn is a review, not a completion.** The task settles
  ``ready_for_review`` and the store still refuses an agent's completion.

Real config, real control plane, real stores over throwaway vaults; only the chat
manager is a fake, because a model turn is the one thing these must not do.

Every test is ``async`` because that is where the service runs. ``start_stream``
creates an asyncio task and is only legal on a loop, so the service settles an
attempt from a background watcher task on the same loop — exactly as it does in
the engine. :func:`_end_turns` is how a test ends a turn: it opens the fake
stream's gate and lets the watcher drain, rather than sleeping for it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, ControlPlaneError, CiaoControlPlane


class _RecordingStream:
    """A turn the test ends by hand, replaying the events it published.

    `subscribe()` waits on :attr:`finish` before yielding, which is what a real
    turn does: `ProjectChatManager` returns a stream that runs until the provider
    ends it. A test that leaves the gate shut is looking at a running turn, which
    is the state most of these assertions are about; a test that opens it ends the
    turn with the events it published.

    The shape the service depends on is unchanged either way: an async iterator
    that yields stream events and then returns.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.subscribed = 0
        self.finish = asyncio.Event()

    async def subscribe(self):
        self.subscribed += 1
        await self.finish.wait()
        for event in self.events:
            yield event


async def _end_turns(pcm: _RecordingPcm, *, index: int | None = None) -> None:
    """End running fake turns and let the service's watchers drain.

    The watcher is a background task on the loop, so a test that wants the settled
    state opens the gate and yields until the tasks are done rather than sleeping
    for a guess. Yielding is enough because the watcher does no real I/O beyond the
    store write, which is synchronous.

    ``index`` ends one turn and leaves the rest running, which is how a test asks
    "what does the *old* watcher do now" without also settling the attempt that
    replaced it. ``streams`` is only cleared when every turn was ended, so an
    indexed call leaves the list an addressable record of which turn is which.
    """
    streams = pcm.streams if index is None else [pcm.streams[index]]
    for stream in streams:
        stream.finish.set()
    for _ in range(50):
        await asyncio.sleep(0)
    if index is None:
        pcm.streams.clear()


class _RecordingPcm:
    """The chat manager, recorded.

    Every call these tests care about is appended to `calls`, and `start_stream`
    snapshots what the attempt store and the task file said *at that moment* — the
    only honest way to assert that persistence happens before the side effect,
    rather than after it happened to be written.
    """

    def __init__(self, config: CiaoConfig) -> None:
        self.config = config
        self.projects = {
            "project-home": SimpleNamespace(
                project_id="project-home", name="Home", workspace="personal"
            ),
            "project-general": SimpleNamespace(
                project_id="project-general", name="General", workspace="personal"
            ),
            "project-work": SimpleNamespace(
                project_id="project-work", name="Work", workspace="work"
            ),
        }
        self.chats: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.start_stream_kwargs: list[dict[str, Any]] = []
        self.streams: list[_RecordingStream] = []
        #: What the next ``start_stream`` publishes, and what it answers.
        self.next_events: list[dict[str, Any]] = [
            {"type": "result", "text": "done", "is_error": False}
        ]
        self.raise_on_start: Exception | None = None
        self.raise_on_create: Exception | None = None
        #: How many times ``stop_chat`` was actually awaited. Zero after a stop
        #: means the coroutine was built and dropped, which is the bug.
        self.stop_awaited = 0
        #: Snapshots taken inside ``start_stream``, in call order.
        self.at_start: list[dict[str, Any]] = []
        self._next_chat = 0

    # -- workspace resolution and projects ----------------------------

    def _workspace_vault_root(self, workspace: str) -> Path:
        return self.config.workspace_vault_root(workspace)

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [
            project
            for project in self.projects.values()
            if workspace is None or project.workspace == workspace
        ]

    def create_project(self, name: str, workspace: str):
        project = SimpleNamespace(
            project_id=f"project-{name.lower()}", name=name, workspace=workspace
        )
        self.projects[project.project_id] = project
        return project

    # -- chats --------------------------------------------------------

    def create_chat(
        self, project_id, title="New Chat", model=None, mode=None, provider=None, helper=None
    ):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        self._next_chat += 1
        chat_id = f"chat-{self._next_chat}"
        chat = SimpleNamespace(
            chat_id=chat_id,
            project_id=project_id,
            title=title,
            mode=mode,
            helper=helper or {},
            pending_question="",
            pending_permission="",
        )
        self.chats[chat_id] = chat
        self.calls.append(
            ("create_chat", (project_id,), {"title": title, "mode": mode, "helper": helper})
        )
        return chat

    def update_chat(
        self,
        chat_id,
        *,
        title=None,
        model=None,
        provider=None,
        mode=None,
        project_id=None,
        thinking_level=None,
    ):
        """The real manager's keyword-only signature, field for field.

        The service never calls this — the provenance stamp goes in at
        ``create_chat``, because the real ``update_chat`` has no ``helper``
        parameter and a call naming one raises ``TypeError``. A fake taking
        ``**fields`` would have swallowed that and hidden it: every delegated chat
        carried ``attempt_id: "000…0"``, linking back to nothing. The signature is
        pinned here so the next caller of a parameter that does not exist fails in
        this fake rather than in production.
        """
        chat = self.chats.get(chat_id)
        if chat is None:
            return None
        for key, value in (
            ("title", title),
            ("mode", mode),
            ("project_id", project_id),
        ):
            if value is not None:
                setattr(chat, key, value)
        self.calls.append(
            ("update_chat", (chat_id,), {"title": title, "mode": mode, "project_id": project_id})
        )
        return chat

    def get_chat(self, chat_id):
        return self.chats.get(chat_id)

    async def stop_chat(self, chat_id):
        """The manager's Stop, which is ``async`` — as the real one is.

        The flag is set only *inside* the coroutine, so a caller that built one and
        never awaited it leaves :attr:`stop_awaited` false. That is the assertion
        that matters: an un-awaited ``stop_chat`` returns a coroutine nobody runs, so
        the provider turn keeps running while the attempt is recorded ``stopped``
        over it, and a synchronous fake cannot see that at all.
        """
        self.calls.append(("stop_chat", (chat_id,), {}))
        self.stop_awaited += 1
        return True

    def start_stream(self, chat_id, prompt, *args, **kwargs):
        # The recording that matters: exactly what the turn was handed, and what
        # was already on disk when it was handed it.
        self.start_stream_kwargs.append({"args": args, **kwargs})
        self.calls.append(("start_stream", (chat_id, prompt), dict(kwargs)))
        self.at_start.append(self._snapshot(chat_id))
        if self.raise_on_start is not None:
            raise self.raise_on_start
        stream = _RecordingStream(self.next_events)
        self.streams.append(stream)
        return stream

    # -- the assertion helper ----------------------------------------

    def _snapshot(self, chat_id: str) -> dict[str, Any]:
        """What a turn could see at the instant it started."""
        attempts_path = self.config.state_path.parent / "task-attempts-personal.json"
        attempts: dict[str, Any] = {}
        if attempts_path.is_file():
            attempts = json.loads(attempts_path.read_text(encoding="utf-8"))["attempts"]
        task_files = sorted(
            (self.config.workspace_vault_root("personal") / "Workspace" / "Tasks").glob(
                "*.md"
            )
        )
        linked = None
        linked_revision = ""
        for path in task_files:
            if f"chat_id: {chat_id}" in path.read_text(encoding="utf-8"):
                from ciao.task_board import parse_task

                linked = path.stem
                linked_revision = parse_task(
                    path.read_bytes(), expected_id=path.stem
                ).revision
                break
        return {
            "attempts": attempts,
            "linked_task_id": linked,
            "linked_task_revision": linked_revision,
        }


def _world(tmp_path: Path) -> tuple[CiaoControlPlane, _RecordingPcm]:
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    pcm = _RecordingPcm(config)
    plane = CiaoControlPlane(
        config, project_chat_manager=pcm, schedule_manager=SimpleNamespace()
    )
    return plane, pcm


def _principal(workspace: str = "personal") -> AgentPrincipal:
    return AgentPrincipal(
        token_id="t",
        chat_id="chat-agent",
        project_id="project-home",
        workspace=workspace,
        provider="claude",
    )


def _create(
    plane: CiaoControlPlane, workspace: str = "personal", **fields: Any
) -> dict[str, Any]:
    return plane.workspace_task_create(workspace, **fields)


def _tasks_dir(plane: CiaoControlPlane, workspace: str = "personal") -> Path:
    return plane.config.workspace_vault_root(workspace) / "Workspace" / "Tasks"


def _attempt_store(plane: CiaoControlPlane, workspace: str = "personal"):
    from ciao.task_attempts import TaskAttemptStore

    return TaskAttemptStore(
        workspace=workspace,
        runtime_dir=Path(plane.config.state_path).parent,
        clock=lambda: datetime.now(UTC),
    )


def _delegate(
    plane: CiaoControlPlane, task: dict[str, Any], **fields: Any
) -> dict[str, Any]:
    return plane.workspace_task_delegate(
        "personal", task["id"], expected_revision=task["revision"], **fields
    )


async def _act(
    plane: CiaoControlPlane, attempt_id: str, action: str, **fields: Any
) -> dict[str, Any]:
    """One attempt gesture, awaited — ``stop`` and ``detach`` call an async Stop."""
    return await plane.workspace_task_attempt_action(
        "personal", attempt_id, action, **fields
    )


def _get_task(
    plane: CiaoControlPlane, task_id: str, workspace: str = "personal"
) -> dict[str, Any]:
    return plane.workspace_task_get(workspace, task_id)


def _starts(pcm: _RecordingPcm) -> list[tuple[Any, ...]]:
    return [call[1] for call in pcm.calls if call[0] == "start_stream"]


def _creates(pcm: _RecordingPcm) -> list[tuple[Any, ...]]:
    return [call[1] for call in pcm.calls if call[0] == "create_chat"]


# ── One ordinary chat, no bypass ────────────────────────────────────────


async def test_a_delegation_creates_exactly_one_ordinary_chat(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Draft the migration runbook", body="Steps and links.")

    outcome = _delegate(plane, task)

    assert outcome["created"] is True
    assert outcome["chat_id"] == "chat-1"
    assert outcome["project_id"] == "project-general"
    assert outcome["project_origin"] == "general"
    assert len(_creates(pcm)) == 1
    assert _creates(pcm)[0] == ("project-general",)
    # The chat is titled for the task and takes no mode of its own: an ordinary
    # chat in the resolved project, on the operator's own defaults.
    created = [call for call in pcm.calls if call[0] == "create_chat"][0]
    assert created[2]["title"] == "Draft the migration runbook"
    assert created[2]["mode"] is None


async def test_start_stream_is_called_with_no_unattended_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The whole no-escalation property, asserted on the call itself.

    ``ProjectChatManager._effective_mode_for_chat`` returns ``bypass`` for any
    non-plan chat when ``unattended=True``, so a delegation that named it would run
    a turn nobody could approve anything in. The recording fake pins the exact
    kwargs, so adding the flag later fails here rather than in production.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Wire the board", body="Do the thing.")

    _delegate(plane, task)

    assert len(pcm.start_stream_kwargs) == 1
    assert pcm.start_stream_kwargs[0] == {"args": ()}, pcm.start_stream_kwargs
    assert [call for call in pcm.calls if call[0] == "start_stream"][0][2] == {}, (
        "start_stream was given keyword arguments"
    )


async def test_the_prompt_is_built_from_the_record_and_carries_no_caller_text(
    tmp_path: Path,
) -> None:
    """There is no body key through which a request could rewrite the instruction:
    a delegation hands over the task the user filed, or nothing."""
    plane, pcm = _world(tmp_path)
    task = _create(
        plane, title="Draft the migration runbook", body="Steps, links, criteria."
    )
    _delegate(plane, task)

    _chat_id, prompt = _starts(pcm)[0]
    assert "Draft the migration runbook" in prompt
    assert "Steps, links, criteria." in prompt
    assert task["revision"] in prompt
    assert "<task-board-task>" in prompt
    # And it says the one thing a finishing agent gets wrong.
    assert "do not mark the task done" in prompt.lower()


async def test_the_chat_carries_provenance_naming_the_task_and_the_attempt(
    tmp_path: Path,
) -> None:
    """The stamp is what makes the chat findable again after a reload or a
    restart: nothing else records which task and which attempt a chat is for."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Wire the board")

    outcome = _delegate(plane, task)

    stamp = pcm.get_chat(outcome["chat_id"]).helper
    assert stamp["kind"] == "task_delegation"
    assert stamp["task_id"] == task["id"]
    assert stamp["task_revision"] == task["revision"]
    assert stamp["attempt_id"] == outcome["attempt"]["attempt_id"]
    # A real attempt id, not a placeholder: the store mints one for the chat and
    # records *that same* one, so the stamp names something a reader can look up.
    assert stamp["attempt_id"] == _attempt_store(plane).get(
        stamp["attempt_id"]
    ).attempt_id


async def test_the_provenance_is_stamped_once_at_creation_and_never_restamped(
    tmp_path: Path,
) -> None:
    """The stamp is exact or the chat does not exist.

    The real ``ProjectChatManager.update_chat`` has no ``helper`` parameter, so the
    old "create with a placeholder, restamp afterwards" wrote
    ``attempt_id: "000…0"`` on every delegated chat and swallowed the ``TypeError``
    that told it so. The id is now minted before the chat is created, so the stamp
    and the attempt store can only ever agree — and nothing writes it twice.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stamped once")

    _delegate(plane, task)

    assert [call for call in pcm.calls if call[0] == "update_chat"] == [], (
        "the provenance was written by a second call rather than at creation"
    )
    created = [call for call in pcm.calls if call[0] == "create_chat"][0]
    assert created[2]["helper"]["attempt_id"] != "0" * 32


async def test_a_done_task_cannot_be_delegated(tmp_path: Path) -> None:
    """Delegation hands the task over as ``in_progress``/``agent``, so delegating a
    finished one would reopen it — and an agent could undo a completion the user
    made by hand. Reopening is the user's own move out of *Done*."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Shipped")
    done = plane.workspace_task_action(
        "personal",
        "complete",
        task["id"],
        expected_revision=task["revision"],
        actor="user",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, done)

    assert excinfo.value.code == "invalid_task"
    assert "Done" in str(excinfo.value), "the refusal must say what to do about it"
    assert _creates(pcm) == []
    assert _starts(pcm) == []
    assert _attempt_store(plane).live_by_task() == {}
    assert _get_task(plane, task["id"])["status"] == "done"


async def test_an_agent_cannot_reopen_a_done_task_by_delegating_it(
    tmp_path: Path,
) -> None:
    """The same refusal over MCP, where there is no user session to catch it."""
    plane, _pcm = _world(tmp_path)
    principal = _principal()
    created = plane.task_create(principal, title="Shipped")["data"]
    done = plane.workspace_task_action(
        "personal",
        "complete",
        created["id"],
        expected_revision=created["revision"],
        actor="user",
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_delegate(principal, created["id"], expected_revision=done["revision"])

    assert excinfo.value.code == "invalid_task"
    assert _get_task(plane, created["id"])["status"] == "done"
    assert _attempt_store(plane).live_by_task() == {}


async def test_a_done_task_is_delegatable_again_once_moved_out_of_done(
    tmp_path: Path,
) -> None:
    """The refusal is about the column, not a one-way door: moving it back is the
    user's own gesture and delegation works from there."""
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Revisit")
    done = plane.workspace_task_action(
        "personal", "complete", task["id"], expected_revision=task["revision"], actor="user"
    )
    reopened = plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=done["revision"],
        changes={"status": "backlog"},
        actor="user",
    )

    outcome = _delegate(plane, reopened)

    assert outcome["created"] is True


async def test_the_tasks_own_project_is_the_host_when_it_has_one(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Bound", project_id="Home")

    outcome = _delegate(plane, task)

    assert outcome["project_id"] == "project-home"
    assert outcome["project_origin"] == "task"
    assert pcm.get_chat(outcome["chat_id"]).project_id == "project-home"


async def test_an_explicit_project_overrides_the_tasks_own(tmp_path: Path) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Bound", project_id="Home")

    outcome = _delegate(plane, task, project_id="project-general")

    assert outcome["project_id"] == "project-general"
    assert outcome["project_origin"] == "requested"


async def test_a_foreign_or_deleted_project_is_refused_not_fallen_back(
    tmp_path: Path,
) -> None:
    """Deleting a project must stop work arriving in it, so a named project that
    cannot be resolved fails rather than quietly running in General."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Bound")

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, task, project_id="project-work")

    assert excinfo.value.code == "project_not_found"
    assert _creates(pcm) == []
    assert _starts(pcm) == []
    assert _attempt_store(plane).live_by_task() == {}


async def test_a_workspace_with_no_general_project_is_refused(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    del pcm.projects["project-general"]
    task = _create(plane, title="Nowhere to run")

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, task)

    assert excinfo.value.code == "project_not_found"
    assert _creates(pcm) == []


# ── Durable before the side effect ──────────────────────────────────────


async def test_the_attempt_and_the_linkage_are_persisted_before_the_turn(
    tmp_path: Path,
) -> None:
    """The order is the contract. Written after, a crash in between would leave an
    orphan chat nothing points at — and a board that cannot tell a delegated task
    from an undelegated one."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Durable first")

    _delegate(plane, task)

    assert len(pcm.at_start) == 1
    seen = pcm.at_start[0]
    assert seen["linked_task_id"] == task["id"]
    # One attempt, already on disk, naming the chat the turn is about to run in.
    assert len(seen["attempts"]) == 1
    attempt = next(iter(seen["attempts"].values()))
    assert attempt["state"] == "running"
    assert attempt["chat_id"] == "chat-1"
    # Bound to the revision the linkage write left behind, which is the record the
    # agent is now working against.
    assert attempt["task_revision"] == seen["linked_task_revision"]


async def test_the_linked_task_says_so_on_disk(tmp_path: Path) -> None:
    """The task file is the board's own record, so the linkage has to be in the
    file: a card that cannot say which chat is working on it cannot be stopped."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Linked")
    outcome = _delegate(plane, task)

    raw = (_tasks_dir(plane) / f"{task['id']}.md").read_text(encoding="utf-8")
    assert f"chat_id: {outcome['chat_id']}" in raw
    assert f"attempt_id: {outcome['attempt']['attempt_id']}" in raw
    # Delegation hands the task over: in progress, for the agent, which is what
    # makes `review_state: ready` reachable when the turn finishes.
    assert "status: in_progress" in raw
    assert "assignee: agent" in raw


async def test_the_task_row_carries_the_attempt_facts_a_board_draws(
    tmp_path: Path,
) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Badged")
    outcome = _delegate(plane, task)

    row = outcome["task"]
    assert row["attempt_state"] == "running"
    assert row["live_attempt_id"] == outcome["attempt"]["attempt_id"]
    assert row["chat_id"] == outcome["chat_id"]
    assert row["changed_since_delegated"] is False
    # And the list rows carry them too, so a board draws a badge without a second
    # request per card.
    listed = plane.workspace_task_list("personal")
    assert listed[0]["attempt_state"] == "running"
    assert listed[0]["live_attempt_id"] == outcome["attempt"]["attempt_id"]


async def test_a_delegation_hands_the_task_over_and_the_revision_moves(
    tmp_path: Path,
) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Handed over")

    outcome = _delegate(plane, task)

    assert outcome["task"]["status"] == "in_progress"
    assert outcome["task"]["assignee"] == "agent"
    assert outcome["task"]["revision"] != task["revision"]


# ── One live attempt, one chat ──────────────────────────────────────────


async def test_a_double_delegation_returns_the_same_attempt_and_creates_no_chat(
    tmp_path: Path,
) -> None:
    """The race the whole store exists for: two callers, one delegation."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Only once")

    first = _delegate(plane, task)
    second = _delegate(plane, _get_task(plane, task["id"]))

    assert first["created"] is True
    assert second["created"] is False
    assert second["attempt"]["attempt_id"] == first["attempt"]["attempt_id"]
    assert second["chat_id"] == first["chat_id"]
    assert len(_creates(pcm)) == 1
    # And nothing was re-sent: a second turn for one delegation is the outcome
    # this module exists to prevent.
    assert len(_starts(pcm)) == 1


async def test_a_stale_revision_starts_nothing(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Planned against an older read")
    plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=task["revision"],
        changes={"due": "2026-10-05"},
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, task)

    assert excinfo.value.code == "task_revision_conflict"
    assert excinfo.value.retryable is True
    assert pcm.calls == [], "a stale delegation created something"
    assert _attempt_store(plane).live_by_task() == {}


async def test_a_foreign_task_is_not_found_and_starts_nothing(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    mine = _create(plane, title="Private to personal")
    _create(plane, "work", title="Private to work")

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_delegate("work", mine["id"], expected_revision=mine["revision"])

    assert excinfo.value.code == "task_not_found"
    assert pcm.calls == []


async def test_a_task_cannot_be_delegated_again_while_its_attempt_is_live(
    tmp_path: Path,
) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Still running")
    _delegate(plane, task)
    assert _attempt_store(plane).get_live(task["id"]) is not None

    outcome = _delegate(plane, _get_task(plane, task["id"]))

    assert outcome["created"] is False
    assert len(_starts(pcm)) == 1


# ── A dead allocation or chat leaves `interrupted` ───────────────────────


async def test_a_chat_that_cannot_be_created_starts_nothing_and_records_nothing(
    tmp_path: Path,
) -> None:
    """No chat means no attempt to record: an attempt naming a chat that does not
    exist would be a record the board could draw and no gesture could act on."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="No chat for me")
    pcm.raise_on_create = RuntimeError("no such project")

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, task)

    assert excinfo.value.code == "chat_create_failed"
    assert excinfo.value.retryable is True
    assert _attempt_store(plane).live_by_task() == {}
    assert _get_task(plane, task["id"])["chat_id"] is None


async def test_a_dead_allocation_leaves_the_attempt_interrupted(tmp_path: Path) -> None:
    """The chat exists and nothing will report the turn's outcome. ``interrupted``
    is the honest word for that, and it is the state a crash in the same window
    leaves — so a retry never guesses which of the two happened."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="The turn never started")
    pcm.raise_on_start = RuntimeError("provider unreachable")

    with pytest.raises(ControlPlaneError) as excinfo:
        _delegate(plane, task)

    assert excinfo.value.code == "task_launch_failed"
    assert excinfo.value.retryable is True
    attempt = _attempt_store(plane).list_for_task(task["id"])[0]
    assert attempt.state == "interrupted"
    assert attempt.ended_at
    assert "provider unreachable" in attempt.detail
    # It names the chat that exists, so the operator can open it.
    assert attempt.chat_id == "chat-1"
    assert _get_task(plane, task["id"])["attempt_id"] == attempt.attempt_id


async def test_an_interrupted_attempt_is_resumable_in_its_own_chat(
    tmp_path: Path,
) -> None:
    """The way forward after a dead turn is the chat that already exists, not a
    second one."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Retry the turn")
    # Stop the first turn so the task has no live attempt: `ready_for_review` is
    # itself live, so a second delegation would correctly return it rather than
    # start anything, and the test would never reach the dead launch it is about.
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    pcm.raise_on_start = RuntimeError("provider unreachable")
    with pytest.raises(ControlPlaneError):
        _delegate(plane, _get_task(plane, task["id"]))
    pcm.raise_on_start = None

    attempt = _attempt_store(plane).list_for_task(task["id"])[0]
    outcome = await _act(plane, attempt.attempt_id, "resume")

    assert outcome["resumed"] is True
    assert outcome["attempt"]["attempt_id"] == attempt.attempt_id
    assert outcome["attempt"]["state"] == "running"
    # The chat that exists is the one the dead turn was launched in: two chats
    # exist by now, and the resume continues the second — the one that owns the
    # task — rather than minting a third.
    assert outcome["chat_id"] == attempt.chat_id
    assert outcome["chat_id"] == "chat-2"
    assert len(_creates(pcm)) == 2
    assert len(_starts(pcm)) == 3
    # And the resumed turn is ordinary too: no `unattended` on it either.
    assert pcm.start_stream_kwargs[-1] == {"args": ()}


async def test_an_unknown_attempt_is_not_found_and_an_unknown_verb_is_invalid(
    tmp_path: Path,
) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Nothing to act on")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]

    with pytest.raises(ControlPlaneError) as excinfo:
        await _act(plane, "f" * 32, "stop")
    assert excinfo.value.code == "task_attempt_not_found"

    with pytest.raises(ControlPlaneError) as excinfo:
        await _act(plane, attempt_id, "cancel")
    assert excinfo.value.code == "invalid_action"


# ── Changed since delegated ─────────────────────────────────────────────


async def test_an_edit_after_delegation_is_reported_not_resolved(tmp_path: Path) -> None:
    """The result the agent is about to produce was reached against a description
    the user has since changed. The reviewer has to be told; nothing here decides
    for them."""
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Edited after the handoff", body="Original scope.")
    _delegate(plane, task)

    edited = plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=_get_task(plane, task["id"])["revision"],
        changes={},
        body="A different scope.",
    )

    assert edited["attempt_state"] == "running"
    assert edited["changed_since_delegated"] is True
    assert edited["revision"] != task["revision"]
    # The attempt still records the revision it was handed, which is what makes
    # the comparison possible at all.
    attempt = _attempt_store(plane).get_live(task["id"])
    assert attempt is not None
    assert attempt.task_revision != edited["revision"]


async def test_an_edit_before_delegation_is_not_changed_since_delegated(
    tmp_path: Path,
) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Edited first")
    edited = plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=task["revision"],
        changes={"title": "Edited first, then delegated"},
    )

    outcome = _delegate(plane, edited)

    assert outcome["task"]["changed_since_delegated"] is False


async def test_a_re_delegation_after_an_edit_reports_no_change(tmp_path: Path) -> None:
    """A new attempt is handed the *current* revision, so its badge is clean even
    though the previous attempt was made against an older one."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Second time")
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    pcm.raise_on_start = RuntimeError("provider unreachable")
    with pytest.raises(ControlPlaneError):
        _delegate(plane, _get_task(plane, task["id"]))
    pcm.raise_on_start = None
    await _end_turns(pcm)

    assert _attempt_store(plane).get_live(task["id"]) is None
    current = _get_task(plane, task["id"])
    plane.workspace_task_update(
        "personal",
        current["id"],
        expected_revision=current["revision"],
        changes={"title": "Second time, edited"},
    )

    outcome = _delegate(plane, _get_task(plane, task["id"]))

    assert outcome["task"]["changed_since_delegated"] is False


# ── A finished turn is a review, not a completion ───────────────────────


async def test_a_finished_turn_settles_ready_for_review_and_never_done(
    tmp_path: Path,
) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Finish the work")
    outcome = _delegate(plane, task)

    await _end_turns(pcm)

    attempt = _attempt_store(plane).get(outcome["attempt"]["attempt_id"])
    assert attempt.state == "ready_for_review"
    assert attempt.ended_at

    row = _get_task(plane, task["id"])
    assert row["status"] == "in_progress"
    assert row["status"] != "done"
    # The result is flagged for review, which the store only allows for an
    # agent-assigned task in progress — both written by the link.
    assert row["review_state"] == "ready"
    assert row["assignee"] == "agent"


async def test_a_turn_waiting_on_an_approval_card_settles_needs_you(
    tmp_path: Path,
) -> None:
    """A paused turn is not a finished one. The badge says the result is not there
    yet rather than asking the user to review nothing — which is the whole reason
    delegation never passes ``unattended``."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Needs an approval")
    _delegate(plane, task)
    # The chat is paused on a permission card, exactly as `_drive` leaves it.
    pcm.get_chat("chat-1").pending_permission = "Write /notes.md"

    await _end_turns(pcm)

    row = _get_task(plane, task["id"])
    assert row["attempt_state"] == "needs_you"
    assert row["status"] != "done"
    # And no Review badge: there is no result yet, so there is nothing to review.
    assert row["review_state"] == "none"


async def test_a_stream_with_no_result_is_interrupted_not_a_review(
    tmp_path: Path,
) -> None:
    """A turn whose stream ends without a result has no answer to show.

    ``ready_for_review`` on an empty result would put nothing in front of the user
    dressed as a finished piece of work, and the only way to tell it from a real one
    afterwards is to open the chat — which is the one thing the badge exists to save
    them from. ``interrupted`` is the honest word, and it is resumable.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="The stream just ended")
    pcm.next_events = [{"type": "text", "text": "half an answer"}]
    _delegate(plane, task)

    await _end_turns(pcm)

    row = _get_task(plane, task["id"])
    assert row["attempt_state"] == "interrupted"
    assert row["review_state"] == "none"
    attempt = _attempt_store(plane).list_for_task(task["id"])[0]
    assert attempt.state == "interrupted"
    assert attempt.detail == "the turn ended without a result"
    # Which is also what makes it resumable: the user can continue that chat.
    assert _attempt_store(plane).get_live(task["id"]) is None


async def test_a_clean_settle_is_not_reported_as_changed_since_delegated(
    tmp_path: Path,
) -> None:
    """The flag means the user edited the task under the agent, and nothing else.

    The watcher's own review badge is a write to the task, so it moves the
    revision. Without rebinding the attempt to the revision that write left behind,
    *every* finished turn read "changed since delegated" — a warning that would
    appear on every card and mean nothing.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Reviewed cleanly", body="The original scope.")
    _delegate(plane, task)

    await _end_turns(pcm)

    settled = _get_task(plane, task["id"])
    assert settled["review_state"] == "ready", "the badge is what moves the revision"
    assert settled["changed_since_delegated"] is False
    # And only a real edit trips it.
    edited = plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=settled["revision"],
        changes={},
        body="A different scope.",
        actor="user",
    )
    assert edited["changed_since_delegated"] is True


async def test_a_detached_watcher_may_not_flag_a_task_it_no_longer_holds(
    tmp_path: Path,
) -> None:
    """Detach releases the task, and the provider turn ends afterwards anyway.

    The watcher is still holding that stream when it does, so it settles as if
    nothing happened: the task would end up carrying a Review badge for an attempt
    that no longer holds it, over work the user has already taken back.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Taken back")
    outcome = _delegate(plane, task)

    await _act(plane, outcome["attempt"]["attempt_id"], "detach")
    await _end_turns(pcm)

    row = _get_task(plane, task["id"])
    assert row["review_state"] == "none"
    assert row["chat_id"] is None
    assert row["attempt_id"] is None
    # The attempt keeps what the user's gesture said about it, not what a late
    # stream event said afterwards.
    assert row["attempt_state"] == "stopped"
    assert _attempt_store(plane).get(outcome["attempt"]["attempt_id"]).state == "stopped"


async def test_a_superseded_watcher_may_not_flag_the_attempt_that_replaced_it(
    tmp_path: Path,
) -> None:
    """The same race through Retry, which is the likelier one.

    Stop the first turn, retry into a new one, and only then let the old stream
    end: the old watcher settles an attempt that is already settled (so it records
    nothing), and it must not reach round to flag the task the *new* attempt now
    owns — that would put a finished result on a task with a turn still running.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Tried once, then again")
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    retried = await _act(plane, first["attempt"]["attempt_id"], "retry")

    # Only the first turn's stream ends; the retry's is still in flight.
    await _end_turns(pcm, index=0)

    row = _get_task(plane, task["id"])
    assert row["attempt_state"] == "running"
    assert row["review_state"] == "none"
    assert row["attempt_id"] == retried["attempt"]["attempt_id"]
    assert _attempt_store(plane).get(first["attempt"]["attempt_id"]).state == "stopped"


async def test_a_turn_that_ended_in_an_error_settles_failed(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="The provider gave up")
    pcm.next_events = [{"type": "result", "text": "the provider gave up", "is_error": True}]

    _delegate(plane, task)
    await _end_turns(pcm)

    assert _get_task(plane, task["id"])["attempt_state"] == "failed"


async def test_a_stopped_turn_settles_stopped(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="The user pressed stop")
    pcm.next_events = [{"type": "result", "text": "", "is_error": False, "stopped": True}]

    _delegate(plane, task)
    await _end_turns(pcm)

    row = _get_task(plane, task["id"])
    assert row["attempt_state"] == "stopped"
    assert row["status"] != "done"


async def test_a_delegated_task_is_uncompletable_and_unreassignable_by_the_agent(
    tmp_path: Path,
) -> None:
    """The store's rules still hold, and delegation adds no way round them. A live
    linkage refuses both outright, before the agent/user distinction comes into it."""
    plane, _pcm = _world(tmp_path)
    principal = _principal()
    created = plane.task_create(principal, title="Review the diff")["data"]
    outcome = plane.task_delegate(
        principal, created["id"], expected_revision=created["revision"]
    )
    assert outcome["data"]["created"] is True
    task = _get_task(plane, created["id"])

    # The agent's completions are refused as agent completions, which is the store's
    # standing rule and the first thing it checks.
    for route in (
        lambda: plane.task_action(
            principal, "complete", created["id"], expected_revision=task["revision"]
        ),
        lambda: plane.task_action(
            principal,
            "move",
            created["id"],
            expected_revision=task["revision"],
            status="done",
        ),
        lambda: plane.task_update(
            principal,
            created["id"],
            expected_revision=task["revision"],
            changes={"status": "done"},
        ),
    ):
        with pytest.raises(ControlPlaneError) as excinfo:
            route()
        assert excinfo.value.code == "task_completion_requires_user"

    # The live linkage refuses the rest, whoever is asking — including the user's
    # own session, because a task with a turn in flight is not one the user closes
    # behind its back.
    for route in (
        lambda: plane.workspace_task_action(
            "personal",
            "complete",
            created["id"],
            expected_revision=task["revision"],
            actor="user",
        ),
        lambda: plane.workspace_task_update(
            "personal",
            created["id"],
            expected_revision=task["revision"],
            changes={"assignee": "user"},
            actor="user",
        ),
        lambda: plane.task_update(
            principal,
            created["id"],
            expected_revision=task["revision"],
            changes={"assignee": "user"},
        ),
    ):
        with pytest.raises(ControlPlaneError) as excinfo:
            route()
        assert excinfo.value.code == "task_invalid"

    after = _get_task(plane, created["id"])
    assert after["status"] == "in_progress"
    assert after["assignee"] == "agent"


async def test_a_detached_task_can_be_completed_by_the_user(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Reviewed and released")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]

    detached = await _act(plane, attempt_id, "detach")

    assert detached["task"]["chat_id"] is None
    assert detached["task"]["attempt_id"] is None
    assert detached["attempt"]["state"] == "stopped"
    assert "chat-1" in pcm.chats, "the chat itself is left alone"

    done = plane.workspace_task_action(
        "personal",
        "complete",
        task["id"],
        expected_revision=detached["task"]["revision"],
        actor="user",
    )
    assert done["status"] == "done"
    # And the agent still cannot do it, after the detach.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_action(
            _principal(), "complete", task["id"], expected_revision=done["revision"]
        )
    assert excinfo.value.code == "task_completion_requires_user"


async def test_detaching_a_running_turn_stops_it_before_releasing_the_task(
    tmp_path: Path,
) -> None:
    """Releasing a task while an agent is still writing to it is the race the
    linkage exists to prevent, so a detach stops the turn first."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stop before release")
    outcome = _delegate(plane, task)
    # The fake turn is still gated shut, so the attempt is live.
    assert _attempt_store(plane).get_live(task["id"]).state == "running"

    await _act(plane, outcome["attempt"]["attempt_id"], "detach")
    await _end_turns(pcm)

    assert pcm.stop_awaited == 1, "a detach of a running turn did not stop it"
    row = _get_task(plane, task["id"])
    assert row["chat_id"] is None
    assert _attempt_store(plane).get(outcome["attempt"]["attempt_id"]).state == "stopped"


async def test_a_stop_awaits_the_chat_managers_stop(tmp_path: Path) -> None:
    """``ProjectChatManager.stop_chat`` is ``async``, and that is the whole of it.

    It waits for the provider to acknowledge the interrupt before force-closing.
    Called without awaiting, the coroutine is built and never run: the turn keeps
    going while the attempt is recorded ``stopped`` over it, which is precisely the
    state a user pressing Stop is trying to leave. The fake counts *awaits*, so a
    stop that is called without one fails here.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stop it properly")
    outcome = _delegate(plane, task)
    assert pcm.stop_awaited == 0

    await _act(plane, outcome["attempt"]["attempt_id"], "stop")

    assert pcm.stop_awaited == 1
    assert [call for call in pcm.calls if call[0] == "stop_chat"] == [
        ("stop_chat", ("chat-1",), {})
    ]


async def test_a_stop_that_cannot_reach_the_manager_still_settles_the_attempt(
    tmp_path: Path,
) -> None:
    """A refused Stop is not a failed gesture.

    Losing the chat manager would be the wrong trade for the record: the user asked
    for the turn to end and the attempt has to stop claiming the turn is running,
    so the settlement goes ahead and the failure is logged.
    """

    async def _refusing_stop(chat_id: str) -> bool:
        raise RuntimeError("the provider is gone")

    plane, pcm = _world(tmp_path)
    task = _create(plane, title="The manager refuses")
    outcome = _delegate(plane, task)
    pcm.stop_chat = _refusing_stop  # type: ignore[method-assign]

    stopped = await _act(plane, outcome["attempt"]["attempt_id"], "stop")

    assert stopped["attempt"]["state"] == "stopped"
    # The linkage is untouched, so the card still points at the attempt that ran it.
    assert stopped["task"]["live_attempt_id"] == ""
    assert stopped["task"]["attempt_id"] == outcome["attempt"]["attempt_id"]
    assert stopped["task"]["attempt_state"] == "stopped"


async def test_the_stop_reply_carries_the_task_so_the_card_updates(tmp_path: Path) -> None:
    """A Stop leaves the linkage in place but still moves the record.

    The board draws the badge, Stop and Detach off that record, and the store adopts
    the row from the reply's ``task`` — so a reply without it left a card reading
    "Running" over a turn the user had just ended until they reloaded by hand.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stop refreshes the card")
    outcome = _delegate(plane, task)

    stopped = await _act(plane, outcome["attempt"]["attempt_id"], "stop")

    assert stopped["task"]["id"] == task["id"]
    assert stopped["task"]["attempt_state"] == "stopped"
    # No live attempt: Stop released the turn, and the badge has to say so.
    assert stopped["task"]["live_attempt_id"] == ""
    assert stopped["task"]["chat_id"] == "chat-1"


async def test_stopping_keeps_the_linkage_so_completion_stays_refused(
    tmp_path: Path,
) -> None:
    """A Stop ends the turn; it does not release the task. Only detach does, so
    "stopped" never quietly becomes "the user may close this"."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stopped, still linked")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]

    stopped = await _act(plane, attempt_id, "stop")

    assert stopped["attempt"]["state"] == "stopped"
    assert pcm.stop_awaited == 1
    row = _get_task(plane, task["id"])
    assert row["chat_id"] == "chat-1"
    with pytest.raises(ControlPlaneError):
        plane.workspace_task_action(
            "personal",
            "complete",
            task["id"],
            expected_revision=row["revision"],
            actor="user",
        )


async def test_the_user_approves_done_on_a_review_ready_card_in_one_gesture(
    tmp_path: Path,
) -> None:
    """The review is the case the issue describes, and it has to be one click.

    ``ready_for_review`` is a *live* attempt, so the store refuses a completion over
    a live linkage — right in general, and wrong for a result that is already there
    waiting for a decision. Routing that through Detach first reads as "discard the
    attempt": it settles the reviewed attempt as ``stopped`` and loses how the turn
    ended. So approving Done releases the linkage and completes in one gesture, and
    the attempt stays as the ``ready_for_review`` record of what the agent did.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Ready for the user")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]
    await _end_turns(pcm)
    settled = _get_task(plane, task["id"])
    assert settled["review_state"] == "ready"
    assert settled["attempt_state"] == "ready_for_review"

    done = plane.workspace_task_action(
        "personal",
        "complete",
        task["id"],
        expected_revision=settled["revision"],
        actor="user",
    )

    assert done["status"] == "done"
    assert done["chat_id"] is None, "the linkage was released"
    assert done["attempt_id"] is None
    # The attempt keeps the outcome the user actually reviewed.
    assert _attempt_store(plane).get(attempt_id).state == "ready_for_review"
    # And the agent still cannot complete anything, on this one or any other.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_action(_principal(), "complete", task["id"], expected_revision=done["revision"])
    assert excinfo.value.code == "task_completion_requires_user"


async def test_done_on_a_review_ready_card_at_a_stale_revision_writes_nothing(
    tmp_path: Path,
) -> None:
    """The two writes are held together by one revision check.

    A board drawn before the review badge was written holds the older revision, so
    approving Done from it is refused before either write — rather than releasing
    the linkage and then failing to close the card it just unlinked.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Reviewed, then edited")
    outcome = _delegate(plane, task)
    # A board drawn before the turn ended: it holds the pre-badge revision.
    before = _get_task(plane, task["id"])
    await _end_turns(pcm)
    assert _get_task(plane, task["id"])["revision"] != before["revision"], (
        "the review badge did not move the revision, so nothing is being tested"
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_action(
            "personal",
            "complete",
            task["id"],
            expected_revision=before["revision"],
            actor="user",
        )

    assert excinfo.value.code == "task_revision_conflict"
    row = _get_task(plane, task["id"])
    assert row["status"] == "in_progress", "nothing was released"
    assert row["attempt_id"] == outcome["attempt"]["attempt_id"]


async def test_a_live_attempt_still_needs_stop_or_detach_before_done(
    tmp_path: Path,
) -> None:
    """The review gesture is narrow on purpose.

    A turn still writing, or paused on a question, has no result to review — so
    closing it is not approving anything, and the old refusal stands for both.
    """
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Still running")
    outcome = _delegate(plane, task)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_action(
            "personal",
            "complete",
            task["id"],
            expected_revision=_get_task(plane, task["id"])["revision"],
            actor="user",
        )

    assert excinfo.value.code == "task_invalid"
    assert _get_task(plane, task["id"])["status"] == "in_progress"
    assert _attempt_store(plane).get(outcome["attempt"]["attempt_id"]).state == "running"


async def test_detaching_a_reviewed_attempt_leaves_the_review_alone(
    tmp_path: Path,
) -> None:
    """Detach says the task no longer belongs to this attempt, not that the attempt
    did not get there. Rewriting a reviewed result to ``stopped`` threw away the one
    record of how the turn ended, because the user released the card."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Reviewed then released")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]
    await _end_turns(pcm)

    detached = await _act(plane, attempt_id, "detach")

    assert detached["attempt"]["state"] == "ready_for_review"
    assert detached["attempt"]["detail"] == "", "no stop was invented for it"
    assert detached["task"]["chat_id"] is None
    assert detached["task"]["review_state"] == "ready"
    # And it did not stop a turn that had already ended.
    assert pcm.stop_awaited == 0


async def test_stopping_a_settled_attempt_is_refused_rather_than_a_no_op(
    tmp_path: Path,
) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Already settled")
    outcome = _delegate(plane, task)
    attempt_id = outcome["attempt"]["attempt_id"]
    # `ready_for_review` is itself a live state — the task is still linked and still
    # waiting for the user — so stopping once is a real gesture and settles it.
    first_stop = await _act(plane, attempt_id, "stop")
    assert first_stop["attempt"]["state"] == "stopped"

    with pytest.raises(ControlPlaneError) as excinfo:
        await _act(plane, attempt_id, "stop")

    assert excinfo.value.code == "invalid_action"
    assert "stopped" in str(excinfo.value), "the refusal must name the state it found"


# ── Resume vs retry ─────────────────────────────────────────────────────


async def test_resume_continues_the_same_chat_under_the_same_attempt(
    tmp_path: Path,
) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Pick it up again")
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    pcm.raise_on_start = RuntimeError("provider unreachable")
    with pytest.raises(ControlPlaneError):
        _delegate(plane, _get_task(plane, task["id"]))
    pcm.raise_on_start = None

    interrupted = _attempt_store(plane).list_for_task(task["id"])[0]
    resumed = await _act(plane, interrupted.attempt_id, "resume")

    # The same attempt and the same chat — that is what `resume` means, and it is
    # the whole difference from `retry`.
    assert resumed["attempt"]["attempt_id"] == interrupted.attempt_id
    assert resumed["chat_id"] == interrupted.chat_id
    assert len(_creates(pcm)) == 2
    _chat_id, prompt = _starts(pcm)[-1]
    # A continuation works from the chat, which already holds the task: it names
    # the attempt, never re-quotes a possibly-stale description.
    assert "Pick it up again" in prompt
    assert "<task-board-task>" not in prompt


async def test_a_ready_for_review_attempt_is_not_resumable(tmp_path: Path) -> None:
    """A finished turn is waiting for the user's decision. Continuing it is the
    reviewer's call, not a retry of an attempt that did not finish."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Finished")
    outcome = _delegate(plane, task)
    await _end_turns(pcm)

    with pytest.raises(ControlPlaneError) as excinfo:
        await _act(plane, outcome["attempt"]["attempt_id"], "resume")

    assert excinfo.value.code == "invalid_action"
    assert "ready_for_review" in str(excinfo.value)


async def test_retry_starts_a_new_attempt_in_a_new_chat_and_keeps_the_old_one(
    tmp_path: Path,
) -> None:
    """The difference the pair exists to make: ``resume`` continues one chat,
    ``retry`` starts another attempt, and the previous attempt stays as history."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Try it again")
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    pcm.raise_on_start = RuntimeError("provider unreachable")
    with pytest.raises(ControlPlaneError):
        _delegate(plane, _get_task(plane, task["id"]))
    pcm.raise_on_start = None

    interrupted = _attempt_store(plane).list_for_task(task["id"])[0]
    outcome = await _act(plane, interrupted.attempt_id, "retry")

    assert outcome["retried"] is True
    assert outcome["created"] is True
    assert outcome["attempt"]["attempt_id"] != interrupted.attempt_id
    # The third chat: the retry is a fresh delegation, and the second chat is the
    # interrupted attempt's, left exactly as it was.
    assert outcome["chat_id"] == "chat-3"
    assert len(_creates(pcm)) == 3
    history = plane.workspace_task_attempts("personal", task["id"])["attempts"]
    # Three attempts: the live retry, then the interrupted one it replaced, then the
    # stopped first one. A retry adds a row rather than rewriting the last, which is
    # what makes "what did we try" answerable.
    assert [row["attempt_id"] for row in history] == [
        outcome["attempt"]["attempt_id"],
        interrupted.attempt_id,
        first["attempt"]["attempt_id"],
    ]
    assert [row["state"] for row in history] == [
        "running",
        "interrupted",
        "stopped",
    ]


async def test_a_live_attempt_must_be_stopped_before_a_retry(tmp_path: Path) -> None:
    plane, _pcm = _world(tmp_path)
    task = _create(plane, title="Still running")
    outcome = _delegate(plane, task)

    with pytest.raises(ControlPlaneError) as excinfo:
        await _act(plane, outcome["attempt"]["attempt_id"], "retry")

    assert excinfo.value.code == "invalid_action"


async def test_the_attempt_history_puts_the_live_attempt_first(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="History")
    first = _delegate(plane, task)
    await _act(plane, first["attempt"]["attempt_id"], "stop")
    pcm.raise_on_start = RuntimeError("provider unreachable")
    with pytest.raises(ControlPlaneError):
        _delegate(plane, _get_task(plane, task["id"]))
    pcm.raise_on_start = None
    outcome = _delegate(plane, _get_task(plane, task["id"]))

    history = plane.workspace_task_attempts("personal", task["id"])["attempts"]

    assert history[0]["attempt_id"] == outcome["attempt"]["attempt_id"]
    assert history[0]["state"] == "running"
    assert len(history) == 3
    assert first["attempt"]["attempt_id"] in {row["attempt_id"] for row in history}


# ── Scope ───────────────────────────────────────────────────────────────


async def test_an_agents_delegation_is_scoped_to_its_own_workspace(
    tmp_path: Path,
) -> None:
    """The principal's own claim is the scope; the attempt store inherits it."""
    plane, _pcm = _world(tmp_path)
    mine = _create(plane, "personal", title="Personal work")
    theirs = _create(plane, "work", title="Work")

    outcome = plane.task_delegate(
        _principal("personal"), mine["id"], expected_revision=mine["revision"]
    )
    assert outcome["ok"] is True
    assert outcome["data"]["created"] is True

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_delegate(
            _principal("personal"), theirs["id"], expected_revision=theirs["revision"]
        )
    assert excinfo.value.code == "task_not_found"

    # And the attempt store kept two workspaces apart: nothing of `work` leaked in.
    assert _attempt_store(plane, "personal").live_by_task() != {}
    assert _attempt_store(plane, "work").live_by_task() == {}


async def test_the_agent_envelopes_are_the_shape_the_cli_prints(tmp_path: Path) -> None:
    plane, _pcm = _world(tmp_path)
    principal = _principal()
    created = plane.task_create(principal, title="Envelope check")["data"]
    delegated = plane.task_delegate(
        principal, created["id"], expected_revision=created["revision"]
    )
    assert delegated["ok"] is True
    attempt_id = delegated["data"]["attempt"]["attempt_id"]

    acted = await plane.task_attempt_action(principal, attempt_id, "detach")

    assert acted["ok"] is True
    assert acted["data"]["attempt"]["attempt_id"] == attempt_id
    assert acted["data"]["task"]["id"] == created["id"]


def test_the_operation_annotations_keep_the_auto_approved_split() -> None:
    """``task_delegate`` is allow-class like every other task write; the attempt
    gestures are ask-class because ``stop`` is irreversible."""
    from ciao import behavioral_eval, mcp_server

    declared = {op.name: op.annotations for op in mcp_server.OPERATIONS}
    assert declared["task_delegate"] == mcp_server._WRITE
    assert declared["task_delegate"].readOnlyHint is False
    assert declared["task_attempt_action"] == mcp_server._DESTRUCTIVE
    # And it is genuinely in the destructive set, so the ask-class split the
    # approval check reads is not cosmetic.
    assert "task_attempt_action" in behavioral_eval.destructive_mcp_tool_names()
