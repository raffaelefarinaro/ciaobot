"""Contract tests for the idempotent update-task launch API (#761).

What is proved here is the part no shipped task can prove yet: ``ciao/stock/
update-tasks/catalog.json`` is ``[]`` (the first real task is #729-E), so a
catalog is injected through ``update_task_catalog.packaged_root`` — the same seam
``tests/test_update_tasks.py`` uses — and the launch is driven against a *fake*
manager. No real chat store, no engine, no vault, no model: the fake records
``create_chat``/``start_stream`` and nothing else, which is also how "no model
call in the launch path" is checked — a launch that reached for anything else
would have to go through the manager this stands in for.

The idempotency cases are the ones the plan names, one test each: a first start,
a second start (the double click, the second tab, the retry after a dropped
response), a restart (the record re-read from disk, nothing in process), a
deleted chat, a new revision, and the routes. The lifecycle cases ride on the
same machinery and are pinned the same way: a dispatch that failed, where the
point is that the *next* start sends the prompt rather than reporting a resume
it never performed; a start on a task the operator had dismissed, where the
point is that the record stops saying ``dismissed`` while the chat it keeps is
still the one that holds the work; a dismissal of a *failed* record, which must
not carry the empty chat forward, since that chat is the only other proof the
prompt never left; and a record write that fails after the turn started, which
must not turn a running chat into a refusal with no ``chat_id`` in it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import async_reads, update_task_catalog, update_tasks
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.update_task_catalog import CATALOG_FILENAME, UpdateTask
from ciao.web import chat_service, routes_api, update_task_launch
from ciao.web.routes_api import dismiss_update_task
from ciao.web.routes_api import list_update_tasks
from ciao.web.routes_api import reopen_update_task
from ciao.web.routes_api import start_update_task
from ciao.web.update_task_launch import UPDATE_TASK_KIND
from ciao.web.update_task_launch import launch_task as launch

DETECTOR = "has-legacy-rows"
CHECK = "no-legacy-rows"
VERSION = "1.2.0"
WORKSPACE = "personal"
PROMPT_R1 = "# Review the legacy rows\n\nOne at a time.\n"
PROMPT_R2 = "# Review the legacy rows\n\nRewritten instructions.\n"


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Project:
    def __init__(self, project_id: str, name: str, workspace: str) -> None:
        self.project_id = project_id
        self.name = name
        self.workspace = workspace

    @property
    def is_auto(self) -> bool:
        # The same rule `ProjectInfo.is_auto` uses, so a fake project answers
        # the same question a real one does.
        return self.name == "General" or self.name == "Claude Code CLI"


class _Chat:
    def __init__(self, chat_id: str, project_id: str, title: str, helper: Any) -> None:
        self.chat_id = chat_id
        self.project_id = project_id
        self.title = title
        self.helper = helper
        self.archived = False


class _FakePCM:
    """The chat manager, as far as a launch touches it — and no further.

    Records every call, so a test can assert the *number* of chats created and
    the exact prompt dispatched. ``fail_stream`` makes the dispatch raise, which
    is the one recoverable half-failure a launch has to survive. ``on_stream``
    runs *inside* the dispatch, before it can fail, which is how a test sees the
    record as the launch's own code sees it mid-dispatch — reading it there is
    safe, because ``read_task_state`` takes no lock and the launch holds the
    write lock, not the file.
    """

    def __init__(self, workspace: str = WORKSPACE, *, with_general: bool = True) -> None:
        self.projects: list[_Project] = (
            [_Project("proj-general", "General", workspace)] if with_general else []
        )
        self.chats: dict[str, _Chat] = {}
        self.created: list[_Chat] = []
        self.dispatched: list[tuple[str, str]] = []
        self.fail_stream = False
        self.on_stream: Any = None

    def list_projects(self, workspace: str | None = None) -> list[_Project]:
        return [p for p in self.projects if not workspace or p.workspace == workspace]

    def create_project(self, name: str, workspace: str) -> _Project:
        project = _Project(f"proj-{name.lower()}", name, workspace)
        self.projects.append(project)
        return project

    def create_chat(
        self, project_id: str, title: str = "New Chat", helper: dict | None = None
    ) -> _Chat:
        # The real manager validates the helper on the way in; the fake does too,
        # so a helper the store would reject cannot pass a launch test.
        chat = _Chat(
            f"chat-{len(self.created) + 1}",
            project_id,
            title,
            chat_service._normalize_chat_helper(helper),
        )
        self.chats[chat.chat_id] = chat
        self.created.append(chat)
        return chat

    def get_chat(self, chat_id: str) -> _Chat | None:
        return self.chats.get(chat_id)

    def delete_chat(self, chat_id: str) -> None:
        self.chats.pop(chat_id, None)

    def start_stream(self, chat_id: str, prompt: str) -> None:
        if self.on_stream is not None:
            self.on_stream(chat_id, prompt)
        if self.fail_stream:
            raise RuntimeError("the bridge is down")
        self.dispatched.append((chat_id, prompt))


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_caches() -> Iterator[None]:
    """The applicability cache and the read executor are process-wide."""
    update_tasks.clear_applicability_cache()
    yield
    update_tasks.clear_applicability_cache()
    async_reads.reset_vault_read_executor()


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            WORKSPACE: WorkspaceConfig(
                name=WORKSPACE, vault_root=f"memory-vault/{WORKSPACE}"
            )
        },
    )


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "review-legacy-rows",
        "revision": 1,
        "since_version": "1.0.0",
        "scope": "workspace",
        "title": "Review legacy rows",
        "why": "Older entries need a decision before they can be indexed.",
        "detector": DETECTOR,
        "completion_check": CHECK,
        "prompt_resource": "prompts/review-legacy-rows-1.md",
        "depends_on": [],
    }
    row.update(overrides)
    return row


@pytest.fixture
def packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A packaged catalog root holding one workspace-scoped task.

    ``packaged_root`` is the seam, so ``load_catalog()`` and ``read_prompt()``
    are the real functions reading a real file — the path an installed wheel
    runs. The returned ``publish`` swaps in another revision mid-test, which is
    how an engine update is modelled.
    """
    root = tmp_path / "packaged"
    (root / "prompts").mkdir(parents=True)
    monkeypatch.setattr(update_task_catalog, "packaged_root", lambda: root)
    monkeypatch.setattr(update_task_catalog, "DETECTORS", frozenset({DETECTOR}))
    monkeypatch.setattr(update_task_catalog, "COMPLETION_CHECKS", frozenset({CHECK}))

    def publish(revision: int = 1, **overrides: Any) -> None:
        overrides.setdefault(
            "prompt_resource", f"prompts/review-legacy-rows-{revision}.md"
        )
        (root / CATALOG_FILENAME).write_text(
            json.dumps([_row(revision=revision, **overrides)]), encoding="utf-8"
        )
        (root / "prompts" / f"review-legacy-rows-{revision}.md").write_text(
            PROMPT_R1 if revision == 1 else PROMPT_R2, encoding="utf-8"
        )

    publish()
    return publish


def _launch(
    tmp_path: Path, pcm: Any, task_id: str = "review-legacy-rows", **kw: Any
) -> dict[str, Any]:
    return launch(
        task_id,
        config=_config(tmp_path),
        pcm=pcm,
        workspace=kw.pop("workspace", WORKSPACE),
        installed_version=kw.pop("installed_version", VERSION),
        **kw,
    )


def _task(revision: int = 1) -> UpdateTask:
    """The task as the catalog defines it, addressed by revision."""
    return UpdateTask(
        **_row(revision=revision, prompt_resource=f"prompts/review-legacy-rows-{revision}.md")
    )


def _state(tmp_path: Path, revision: int = 1) -> Any:
    """The record as the store reads it back from disk — a restart, in effect."""
    return update_tasks.read_task_state(
        _task(revision), config=_config(tmp_path), workspace=WORKSPACE
    )


# ── The first start ──────────────────────────────────────────────────────────


def test_first_start_creates_one_chat_with_the_packaged_prompt(
    tmp_path: Path, packaged: Any
) -> None:
    """One chat, the packaged prompt, a stamped record, and no second turn.

    The launch service takes no prompt argument at all, so "the prompt is the
    packaged one" is asserted the only way it can be: what reached
    ``start_stream`` is the file ``read_prompt`` returns, and the digest in the
    record and in the helper is that prompt's.
    """
    pcm = _FakePCM()
    digest = update_task_launch.prompt_digest(PROMPT_R1)

    outcome = _launch(tmp_path, pcm)

    assert len(pcm.created) == 1
    chat = pcm.created[0]
    assert outcome["chat_id"] == chat.chat_id
    assert outcome["resumed"] is False
    assert outcome["created"] is True
    # Hosted in the workspace's own General project, titled by the task.
    assert chat.project_id == "proj-general"
    assert chat.title == "Review legacy rows"
    # The helper the store kept is the launch's own, recognised rather than
    # dropped: this is what makes the chat findable after a restart.
    assert chat.helper == {
        "kind": UPDATE_TASK_KIND,
        "task_id": "review-legacy-rows",
        "revision": 1,
        "scope": "workspace",
        "prompt_digest": digest,
    }
    assert pcm.dispatched == [(chat.chat_id, PROMPT_R1)]

    state = _state(tmp_path)
    assert state.lifecycle == "in_progress"
    assert state.chat_id == chat.chat_id
    assert state.prompt_digest == digest
    assert state.attempted_fingerprint
    assert state.evidence == {"actor": "operator"}


def test_launch_refuses_what_it_cannot_run(tmp_path: Path, packaged: Any) -> None:
    """An unknown id, an unsupported id, a nameless workspace: a refusal each.

    All three are ``ValueError`` because all three are the caller's problem, not
    a fault: there is no task to start, this engine cannot run it, or there is
    nowhere to put its chat. An install-scoped task refuses in its own words —
    its *state* needs no workspace, so the one it cannot name is the host of its
    chat.
    """
    pcm = _FakePCM()
    with pytest.raises(ValueError, match="unknown update task"):
        _launch(tmp_path, pcm, task_id="no-such-task")
    with pytest.raises(ValueError, match="needs engine"):
        _launch(tmp_path, pcm, installed_version="0.9.0")
    with pytest.raises(ValueError, match="workspace-scoped"):
        _launch(tmp_path, pcm, workspace="")
    with pytest.raises(ValueError, match="chat manager is not running"):
        _launch(tmp_path, None)
    packaged(scope="install")
    with pytest.raises(ValueError, match="install-scoped"):
        _launch(tmp_path, pcm, workspace="")
    assert pcm.created == [], "a refused launch creates nothing"


# ── Idempotency ──────────────────────────────────────────────────────────────


def test_second_start_resumes_the_same_chat(tmp_path: Path, packaged: Any) -> None:
    """A double click, a second tab and a retry all get the one chat back.

    The second start reads the record rather than any in-process state, so this
    holds across a lost response, a second device and a restart — all of which
    are the same call twice. The assertions that matter are the counts: one
    chat, and one dispatch, however many times Start is pressed.
    """
    pcm = _FakePCM()
    first = _launch(tmp_path, pcm)
    second = _launch(tmp_path, pcm)
    third = _launch(tmp_path, pcm)

    assert len(pcm.created) == 1
    assert len(pcm.dispatched) == 1, "the packaged prompt ran once, not three times"
    assert first["chat_id"] == second["chat_id"] == third["chat_id"]
    assert first["resumed"] is False
    assert second["resumed"] is third["resumed"] is True
    # `resume_task` is the same read without the create, and it is what a
    # client asking "open the work" rather than "start it" uses.
    resumed = update_task_launch.resume_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        pcm=pcm,
        workspace=WORKSPACE,
        installed_version=VERSION,
    )
    assert resumed["chat_id"] == first["chat_id"]
    assert resumed["resumed"] is True


def test_concurrent_starts_create_exactly_one_chat(
    tmp_path: Path, packaged: Any
) -> None:
    """The race the lock exists for: four starts at once, one chat.

    A double click and two tabs are the same thing on the wire, and neither is a
    polite caller. Every thread enters the launch at the same moment, so the
    only thing separating them is the state file's own write lock: the thread
    that gets it stamps the record, and the three that wait behind it find a
    live chat where they expected none.
    """
    pcm = _FakePCM()
    config = _config(tmp_path)
    threads = 4
    start = threading.Barrier(threads)
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            start.wait(timeout=10)
            results.append(
                launch(
                    "review-legacy-rows",
                    config=config,
                    pcm=pcm,
                    workspace=WORKSPACE,
                    installed_version=VERSION,
                )
            )
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)

    assert errors == []
    assert len(results) == threads
    assert len(pcm.created) == 1, "one chat, however many buttons were pressed"
    assert len(pcm.dispatched) == 1, "the packaged prompt ran once"
    assert {outcome["chat_id"] for outcome in results} == {pcm.created[0].chat_id}
    assert sum(1 for outcome in results if outcome["resumed"]) == threads - 1


def test_a_restart_still_resumes(tmp_path: Path, packaged: Any) -> None:
    """A fresh manager and a fresh config, the same chat back: the state is it.

    Nothing about a launch may live in the process. The second call here builds
    its own config object and its own manager, so the only thing that can answer
    it is the record on disk.
    """
    first = _launch(tmp_path, _FakePCM())
    rebuilt = _FakePCM()
    # A manager that has to be told about the chat, the way a restarted engine
    # reloads it from its own state file.
    rebuilt.chats[first["chat_id"]] = _Chat(
        first["chat_id"],
        "proj-general",
        "Review legacy rows",
        {"kind": UPDATE_TASK_KIND},
    )
    outcome = _launch(tmp_path, rebuilt)

    assert outcome["chat_id"] == first["chat_id"]
    assert outcome["resumed"] is True
    assert rebuilt.created == []
    assert rebuilt.dispatched == [], "a resume sends nothing into the chat"


def test_a_deleted_chat_is_recoverable(tmp_path: Path, packaged: Any) -> None:
    """A stamped record whose chat is gone gets a fresh one, not a dead end.

    The operator deleted the chat, so the record is stale. Starting again must
    create a new chat and re-stamp the record to it — otherwise the task is
    offered forever with an id nobody can open, which is the failure a durable
    binding was supposed to prevent.
    """
    pcm = _FakePCM()
    first = _launch(tmp_path, pcm)
    pcm.delete_chat(first["chat_id"])

    second = _launch(tmp_path, pcm)

    assert len(pcm.created) == 2
    assert second["chat_id"] != first["chat_id"]
    assert second["resumed"] is False
    assert _state(tmp_path).chat_id == second["chat_id"]
    assert [chat_id for chat_id, _ in pcm.dispatched] == [
        first["chat_id"],
        second["chat_id"],
    ], "the new chat got the prompt; the deleted one is not touched again"


def test_a_new_revision_does_not_reuse_the_old_chat(
    tmp_path: Path, packaged: Any
) -> None:
    """Revision 2 launches its own chat and leaves revision 1's alone.

    A record is keyed by ``"<id>@<revision>"``, so a launch of the revised task
    does not even see the record the old one wrote: it cannot resume, and it must
    not. Substituting new instructions into a chat that is already running the
    old ones is the substitution this rule exists to prevent.
    """
    pcm = _FakePCM()
    first = _launch(tmp_path, pcm)
    packaged(2)

    second = _launch(tmp_path, pcm)

    assert len(pcm.created) == 2
    assert second["resumed"] is False
    assert second["chat_id"] != first["chat_id"]
    assert second["revision"] == 2
    assert second["prompt_digest"] != first["prompt_digest"]
    # The old record is untouched, not overwritten, and each revision's chat got
    # exactly one dispatch of its own prompt.
    assert _state(tmp_path, revision=1).chat_id == first["chat_id"]
    assert _state(tmp_path, revision=2).chat_id == second["chat_id"]
    assert [prompt for _, prompt in pcm.dispatched] == [PROMPT_R1, PROMPT_R2]


def test_the_record_says_failed_until_the_prompt_is_dispatched(
    tmp_path: Path, packaged: Any
) -> None:
    """The lifecycle in the file means what it says, at every moment of a launch.

    The record is written before ``start_stream`` and the ``in_progress`` write
    happens after it, so the file can be read from inside the dispatch — which is
    the only way to pin the order rather than infer it from the ends. A record
    claiming ``in_progress`` while the turn is still a request on its way out is
    what made a failed dispatch look like a running one.
    """
    pcm = _FakePCM()
    seen: list[str] = []
    pcm.on_stream = lambda _chat_id, _prompt: seen.append(_state(tmp_path).lifecycle)

    outcome = _launch(tmp_path, pcm)

    assert seen == ["failed"], "at dispatch time the prompt was not in the chat yet"
    assert _state(tmp_path).lifecycle == "in_progress"
    assert outcome["lifecycle"] == "in_progress"


def test_a_failed_dispatch_keeps_the_chat_and_names_it(
    tmp_path: Path, packaged: Any
) -> None:
    """A dispatch that raises is recoverable, and the retry actually sends it.

    The record is written before the turn starts, so a failure cannot orphan the
    chat: the error carries its id and the record marks the attempt ``failed``.
    What the retry has to do is the part that used to be missing — the prompt
    never left, so handing the chat back as a "resume" left a task the record
    called running and the chat had never been told about. Here the retry sends
    it into the same chat, exactly once, and a start *after* that sends nothing
    at all.
    """
    pcm = _FakePCM()
    pcm.fail_stream = True
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as raised:
        _launch(tmp_path, pcm)
    assert raised.value.chat_id == pcm.created[0].chat_id

    state = _state(tmp_path)
    assert state.chat_id == raised.value.chat_id
    assert state.lifecycle == "failed"
    assert pcm.dispatched == [], "the prompt never reached the chat"

    # A retry while the bridge is still down is the same recoverable answer:
    # same chat, still one chat, still marked as a failure a later start can act
    # on, and nothing dispatched twice.
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as again:
        _launch(tmp_path, pcm)
    assert again.value.chat_id == raised.value.chat_id
    assert len(pcm.created) == 1
    assert pcm.dispatched == []
    assert _state(tmp_path).lifecycle == "failed"

    pcm.fail_stream = False
    retried = _launch(tmp_path, pcm)
    assert retried["chat_id"] == raised.value.chat_id
    assert retried["resumed"] is True
    assert retried["created"] is False, "the retry resumed; it minted nothing"
    assert retried["lifecycle"] == "in_progress"
    assert len(pcm.created) == 1
    assert pcm.dispatched == [(raised.value.chat_id, PROMPT_R1)], (
        "the packaged prompt ran exactly once across the failure and the retry"
    )
    after = _state(tmp_path)
    assert after.lifecycle == "in_progress"
    assert after.chat_id == raised.value.chat_id

    # A plain second start after all that is a resume and nothing more.
    third = _launch(tmp_path, pcm)
    assert third["resumed"] is True
    assert third["chat_id"] == raised.value.chat_id
    assert len(pcm.created) == 1
    assert pcm.dispatched == [(raised.value.chat_id, PROMPT_R1)]


def test_a_restart_after_a_failed_dispatch_sends_the_prompt_once(
    tmp_path: Path, packaged: Any
) -> None:
    """A crash between the record and the dispatch is the case this closes.

    A process that dies after writing the record but before ``start_stream``
    returns leaves exactly what a failed dispatch leaves: a live chat the prompt
    never reached, and a record that says so. Nothing in the process is
    available to recover the intent, so the record has to carry it — which is
    why the next start, from a rebuilt manager with no memory of the attempt,
    dispatches into the chat the record names rather than resuming it silently.
    """
    pcm = _FakePCM()
    pcm.fail_stream = True
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as raised:
        _launch(tmp_path, pcm)

    rebuilt = _FakePCM()
    # A manager that has to be told about the chat, the way a restarted engine
    # reloads it from its own state file.
    rebuilt.chats[raised.value.chat_id] = _Chat(
        raised.value.chat_id,
        "proj-general",
        "Review legacy rows",
        {"kind": UPDATE_TASK_KIND},
    )
    outcome = _launch(tmp_path, rebuilt)

    assert outcome["chat_id"] == raised.value.chat_id
    assert outcome["resumed"] is True
    assert outcome["lifecycle"] == "in_progress"
    assert rebuilt.created == [], "a retry sends the prompt, it does not mint a chat"
    assert rebuilt.dispatched == [(raised.value.chat_id, PROMPT_R1)]
    assert _state(tmp_path).lifecycle == "in_progress"


def test_a_start_on_a_dismissed_task_reopens_it_in_the_same_chat(
    tmp_path: Path, packaged: Any
) -> None:
    """Start on a dismissal is a reopen: same chat, record no longer says no.

    A dismissal keeps the chat the task was in, so a start afterwards found a
    live chat and a record that still said the operator had declined it — state
    and chat disagreeing, with the list route reporting ``dismissed`` and
    suppressed for a task that was just started. The operator pressing Start is
    the reopen, so the record is written ``in_progress`` over the same chat and
    the reply says ``resumed``.

    Nothing is dispatched. A chat a dismissal carried is a chat the prompt was
    already sent into — ``dismiss_task`` drops the one exception, a chat whose
    prompt never left — and this module's whole reason for existing is that the
    packaged prompt does not get sent into it twice.
    """
    pcm = _FakePCM()
    first = _launch(tmp_path, pcm)
    dismissed = update_task_launch.dismiss_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        workspace=WORKSPACE,
        installed_version=VERSION,
        reason="looked at them",
    )
    assert dismissed["lifecycle"] == "dismissed"
    assert _state(tmp_path).chat_id == first["chat_id"]

    reopened = _launch(tmp_path, pcm)

    assert reopened["chat_id"] == first["chat_id"]
    assert reopened["resumed"] is True
    assert reopened["created"] is False
    assert reopened["lifecycle"] == "in_progress"
    assert reopened["state"]["chat_id"] == first["chat_id"]
    assert len(pcm.created) == 1, "a reopen keeps the chat the dismissal carried"
    assert pcm.dispatched == [(first["chat_id"], PROMPT_R1)], (
        "the reopen does not run the task again in the chat it kept"
    )
    state = _state(tmp_path)
    assert state.lifecycle == "in_progress"
    assert state.chat_id == first["chat_id"]
    assert state.evidence == {"actor": "operator"}, "the launch is a new attempt"


def test_a_dismissed_task_that_never_ran_starts_fresh(
    tmp_path: Path, packaged: Any
) -> None:
    """A dismissal with no chat behind it is an ordinary start, not a reopen.

    The reopen case above is only reachable with a live chat, and this is the
    other half: an operator who dismissed a task they never launched has no chat
    to keep, so the start does the ordinary thing — a fresh chat and the
    packaged prompt — and the old dismissal is gone.
    """
    pcm = _FakePCM()
    dismissed = update_task_launch.dismiss_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        workspace=WORKSPACE,
        installed_version=VERSION,
    )
    assert dismissed["lifecycle"] == "dismissed"
    assert _state(tmp_path).chat_id == ""

    outcome = _launch(tmp_path, pcm)

    assert outcome["resumed"] is False
    assert outcome["created"] is True
    assert outcome["lifecycle"] == "in_progress"
    assert outcome["chat_id"] == pcm.created[0].chat_id
    assert pcm.dispatched == [(pcm.created[0].chat_id, PROMPT_R1)]


def test_a_dismissal_of_a_failed_attempt_drops_the_empty_chat(
    tmp_path: Path, packaged: Any
) -> None:
    """Fail, dismiss, start: the prompt goes out once, into a chat that ran it.

    ``failed`` is the only thing in the file that says the prompt never left, and
    a dismissal that carried that record's chat forward would discard it. The
    empty chat is live, so the next start would take the reopen branch, write
    ``in_progress`` over it and send nothing — a task reported as running that
    no prompt was ever dispatched into, and with no ``failed`` left there is no
    later start that would retry it. So the dismissal writes no chat, and the
    start takes the ordinary path: a fresh chat and the packaged prompt.
    """
    pcm = _FakePCM()
    pcm.fail_stream = True
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as raised:
        _launch(tmp_path, pcm)
    empty_chat = raised.value.chat_id
    assert pcm.dispatched == []

    dismissed = update_task_launch.dismiss_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        workspace=WORKSPACE,
        installed_version=VERSION,
        reason="the bridge was down",
    )

    assert dismissed["lifecycle"] == "dismissed"
    assert dismissed["chat_id"] == "", (
        "a chat that never got the prompt must not be carried into a dismissal"
    )
    assert _state(tmp_path).chat_id == ""

    pcm.fail_stream = False
    outcome = _launch(tmp_path, pcm)

    assert len(pcm.created) == 2
    assert outcome["chat_id"] == pcm.created[1].chat_id != empty_chat
    assert outcome["resumed"] is False
    assert outcome["lifecycle"] == "in_progress"
    assert pcm.dispatched == [(outcome["chat_id"], PROMPT_R1)], (
        "the packaged prompt ran exactly once, into the chat that is reported"
    )
    state = _state(tmp_path)
    assert state.lifecycle == "in_progress"
    assert state.chat_id == outcome["chat_id"], (
        "the empty chat is not the one the record says is running"
    )


def test_a_reopen_of_a_failed_attempt_carries_no_chat_either(
    tmp_path: Path, packaged: Any
) -> None:
    """Fail, dismiss, reopen, start: the same guarantee one decision further on.

    A reopen is a decision about the dismissal, so it carries whatever the
    dismissal carried — including its absence of a chat. The record is
    ``offered`` again, and this is the branch that a start could not tell from
    the reopen of a task that really did run: a live chat and a lifecycle that
    promises nothing was dispatched. With no chat carried, the start creates one
    and dispatches into it exactly once, and the empty chat the failed attempt
    left behind is still nobody's running task.
    """
    pcm = _FakePCM()
    pcm.fail_stream = True
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as raised:
        _launch(tmp_path, pcm)
    empty_chat = raised.value.chat_id

    update_task_launch.dismiss_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        workspace=WORKSPACE,
        installed_version=VERSION,
    )
    reopened = update_task_launch.reopen_task(
        "review-legacy-rows",
        config=_config(tmp_path),
        workspace=WORKSPACE,
        installed_version=VERSION,
    )
    assert reopened["lifecycle"] == "offered"
    assert reopened["chat_id"] == "", "a reopen cannot resurrect the dropped chat"

    pcm.fail_stream = False
    outcome = _launch(tmp_path, pcm)

    assert len(pcm.created) == 2
    assert outcome["chat_id"] == pcm.created[1].chat_id != empty_chat
    assert outcome["resumed"] is False
    assert outcome["lifecycle"] == "in_progress"
    assert pcm.dispatched == [(outcome["chat_id"], PROMPT_R1)], (
        "the prompt ran once across the failure, the dismissal and the reopen"
    )
    state = _state(tmp_path)
    assert state.lifecycle == "in_progress"
    assert state.chat_id == outcome["chat_id"]

    # A start after that is a resume of a chat that did run it, and sends
    # nothing into it a second time.
    again = _launch(tmp_path, pcm)
    assert again["chat_id"] == outcome["chat_id"]
    assert again["resumed"] is True
    assert pcm.dispatched == [(outcome["chat_id"], PROMPT_R1)]


def _fail_the_nth_write(monkeypatch: pytest.MonkeyPatch, nth: int) -> list[str]:
    """Make the ``nth`` record write of a launch fail, and log every lifecycle.

    The first write in a launch is the pre-dispatch ``failed`` one, so the second
    is exactly the write that records a prompt already running. A retry has no
    pre-dispatch write, so its only write — the one that would record the prompt
    it just sent — is the first.
    """
    real = update_tasks._write_record
    lifecycles: list[str] = []

    def write(path: Path, state: Any) -> None:
        lifecycles.append(state.lifecycle)
        if len(lifecycles) == nth:
            raise update_tasks.UpdateTaskStateError("the state file said no")
        real(path, state)

    monkeypatch.setattr(update_tasks, "_write_record", write)
    return lifecycles


def test_a_record_that_will_not_take_the_started_write_still_returns_the_chat(
    tmp_path: Path, packaged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails after the turn started is logged, not refused.

    ``UpdateTaskStateError`` is a ``ValueError``, so the write that records a
    dispatched prompt used to be able to end a launch as a refusal: the reply
    carried no ``chat_id`` and read "not started", which is the answer that
    sends the operator back to press Start and dispatch the same task into the
    same chat a second time. The chat is running either way, so the launch
    answers with it — and the record keeps saying ``failed``, which is what a
    crash in the same window leaves behind.
    """
    pcm = _FakePCM()
    lifecycles = _fail_the_nth_write(monkeypatch, 2)

    outcome = _launch(tmp_path, pcm)

    assert lifecycles == ["failed", "in_progress"]
    assert outcome["chat_id"] == pcm.created[0].chat_id
    assert outcome["resumed"] is False
    assert pcm.dispatched == [(outcome["chat_id"], PROMPT_R1)]
    assert _state(tmp_path).lifecycle == "failed", (
        "the file keeps the one marker a later start can act on"
    )


def test_the_same_holds_for_a_retry_that_cannot_record_its_dispatch(
    tmp_path: Path, packaged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry path has the same post-dispatch write, and the same answer.

    A retry is the branch that matters most here: its record is ``failed``, its
    prompt has just been sent into the chat the operator was already told about,
    and a refusal would report no chat at all for a turn that is running. So it
    answers ``resumed: true`` with that chat, and the record stays ``failed`` —
    the state a later start retries from, which is the same one a crash between
    ``start_stream`` and the write would leave.
    """
    pcm = _FakePCM()
    pcm.fail_stream = True
    with pytest.raises(update_task_launch.UpdateTaskLaunchError) as raised:
        _launch(tmp_path, pcm)
    chat_id = raised.value.chat_id
    pcm.fail_stream = False
    lifecycles = _fail_the_nth_write(monkeypatch, 1)

    outcome = _launch(tmp_path, pcm)

    assert lifecycles == ["in_progress"]
    assert outcome["chat_id"] == chat_id
    assert outcome["resumed"] is True
    assert pcm.dispatched == [(chat_id, PROMPT_R1)]
    assert _state(tmp_path).lifecycle == "failed"


# ── Dismiss, reopen and the check ────────────────────────────────────────────


def test_dismiss_and_reopen_route_through_update_tasks(
    tmp_path: Path, packaged: Any
) -> None:
    """The two decisions are ``update_tasks``' to record, and they round-trip.

    A dismissal suppresses the offer at this revision only and keeps the chat the
    task was in — this one was launched, so that chat is one the prompt really
    reached; a reopen clears the attempt fingerprint and puts it back. Both write
    through ``update_tasks``' own record lock and primitives, so a card and a
    direct call cannot produce two different records for the same decision.
    """
    config = _config(tmp_path)
    chat_id = _launch(tmp_path, _FakePCM())["chat_id"]

    dismissed = update_task_launch.dismiss_task(
        "review-legacy-rows",
        config=config,
        workspace=WORKSPACE,
        installed_version=VERSION,
        reason="looked at them",
    )
    assert dismissed["lifecycle"] == "dismissed"
    state = update_tasks.read_task_state(_task(), config=config, workspace=WORKSPACE)
    assert state is not None
    assert state.lifecycle == "dismissed"
    assert state.chat_id == chat_id, "a dismissal does not lose the chat"
    assert state.evidence == {"dismiss_reason": "looked at them"}

    reopened = update_task_launch.reopen_task(
        "review-legacy-rows",
        config=config,
        workspace=WORKSPACE,
        installed_version=VERSION,
    )
    assert reopened["lifecycle"] == "offered"
    after = update_tasks.read_task_state(_task(), config=config, workspace=WORKSPACE)
    assert after is not None
    assert after.lifecycle == "offered"
    assert after.chat_id == chat_id
    assert after.attempted_fingerprint == ""


def test_record_check_proves_nothing_without_a_registered_check(
    tmp_path: Path, packaged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch is not completion, and a check nobody implemented proves nothing.

    The check name is registered — so the catalog row loads — but has no
    implementation, so the wrapper reports ``None`` rather than marking the task
    done. This is the rule that keeps "the chat was opened" from ever reading as
    "the work was finished".
    """
    monkeypatch.setattr(update_tasks, "COMPLETION_FUNCTIONS", {})
    assert (
        update_task_launch.record_check(
            "review-legacy-rows",
            config=_config(tmp_path),
            workspace=WORKSPACE,
            installed_version=VERSION,
        )
        is None
    )


# ── The routes ───────────────────────────────────────────────────────────────


def _client(config: CiaoConfig, pcm: _FakePCM) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/update-tasks", list_update_tasks, methods=["GET"]),
            Route("/api/update-tasks/{task_id}/start", start_update_task, methods=["POST"]),
            Route(
                "/api/update-tasks/{task_id}/dismiss", dismiss_update_task, methods=["POST"]
            ),
            Route(
                "/api/update-tasks/{task_id}/reopen", reopen_update_task, methods=["POST"]
            ),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_get_lists_tasks_with_their_state(tmp_path: Path, packaged: Any) -> None:
    """The list is a detector pass, and a row says everything a card needs."""
    client = _client(_config(tmp_path), _FakePCM())
    listed = client.get(f"/api/update-tasks?workspace={WORKSPACE}")
    assert listed.status_code == 200
    body = listed.json()
    rows = body["tasks"]
    assert [row["id"] for row in rows] == ["review-legacy-rows"]
    row = rows[0]
    assert row["revision"] == 1
    assert row["scope"] == "workspace"
    assert row["title"] == "Review legacy rows"
    assert row["why"]
    # No detector is registered for this name, so applicability is `unknown` and
    # nothing is offered. The row is still listed — a caller has to be able to
    # see *why* a task it knows about is not being asked for — and the payload
    # says so in words rather than with an empty list.
    assert row["applicability"] == "unknown"
    assert row["offered"] is False
    assert row["status"] == "offered"
    assert row["chat_id"] == ""
    assert body["coverage_gap"]["reason"] == "not_substantiated"

    # A missing workspace is a bad request, not a guess at one.
    assert client.get("/api/update-tasks").status_code == 400


def test_start_route_answers_the_housekeeping_envelope(
    tmp_path: Path, packaged: Any
) -> None:
    """POST start replies in the envelope, resumes, and re-lists the tasks."""
    pcm = _FakePCM()
    client = _client(_config(tmp_path), pcm)
    url = f"/api/update-tasks/review-legacy-rows/start?workspace={WORKSPACE}"

    first = client.post(url)
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["resumed"] is False
    assert body["chat_id"] == pcm.created[0].chat_id
    assert body["result"]["prompt_digest"] == update_task_launch.prompt_digest(PROMPT_R1)
    # The fresh list travels with the reply, so the client cannot render a card
    # for a task whose state this very call just changed.
    listed = {row["id"]: row for row in body["tasks"]}
    assert listed["review-legacy-rows"]["status"] == "in_progress"
    assert listed["review-legacy-rows"]["chat_id"] == body["chat_id"]

    again = client.post(url).json()
    assert again["resumed"] is True
    assert again["chat_id"] == body["chat_id"]
    assert len(pcm.created) == 1

    dismissed = client.post(
        f"/api/update-tasks/review-legacy-rows/dismiss?workspace={WORKSPACE}",
        json={"reason": "not now"},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["ok"] is True
    assert dismissed.json()["result"]["lifecycle"] == "dismissed"
    reopened = client.post(f"/api/update-tasks/review-legacy-rows/reopen?workspace={WORKSPACE}")
    assert reopened.json()["result"]["lifecycle"] == "offered"


def test_the_start_route_answers_with_the_chat_even_when_the_listing_fails(
    tmp_path: Path, packaged: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detector pass that cannot be listed does not undo a launch that landed.

    The fresh list is the last thing a state-changing reply computes, and a
    failure there used to propagate out of the handler: a chat that existed with
    the packaged prompt running in it became a bare 500 with no ``chat_id``,
    which is the only answer that sends the operator back to press Start. The
    key is left off instead, because ``[]`` would claim the workspace has no
    tasks — a different statement, and the one a card would believe.
    """

    def rows(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the detector pass is unhappy")

    monkeypatch.setattr(routes_api, "_update_task_rows", rows)
    pcm = _FakePCM()
    client = _client(_config(tmp_path), pcm)

    started = client.post(f"/api/update-tasks/review-legacy-rows/start?workspace={WORKSPACE}")

    assert started.status_code == 200
    body = started.json()
    assert body["ok"] is True
    assert body["chat_id"] == pcm.created[0].chat_id
    assert body["resumed"] is False
    assert pcm.dispatched == [(body["chat_id"], PROMPT_R1)]
    assert "tasks" not in body, "an unbuildable list is absent, not empty"


def test_unknown_task_id_is_409_not_500(tmp_path: Path, packaged: Any) -> None:
    """Every state-changing route refuses an unknown id the same way."""
    client = _client(_config(tmp_path), _FakePCM())
    for verb in ("start", "dismiss", "reopen"):
        resp = client.post(f"/api/update-tasks/no-such-task/{verb}?workspace={WORKSPACE}")
        assert resp.status_code == 409, verb
        assert resp.json()["ok"] is False
        assert resp.json()["error"]


# ── The helper shape, against the store that validates it ────────────────────


def test_update_task_helper_survives_the_store_normalizer(packaged: Any) -> None:
    """The launch's helper and the store's validator are the same shape.

    Written out rather than imported, because the point is that changing one
    side without the other fails here: the digest is this module's function, the
    rest is the literal dict a launched chat carries.
    """
    helper = {
        "kind": UPDATE_TASK_KIND,
        "task_id": "review-legacy-rows",
        "revision": 1,
        "scope": "workspace",
        "prompt_digest": update_task_launch.prompt_digest(PROMPT_R1),
    }
    assert chat_service._normalize_chat_helper(helper) == helper


def test_real_app_registers_the_update_task_routes() -> None:
    """Every update-task route must exist in the real app.py route table.

    A route registered nowhere does not exist however well it is unit-tested.
    """
    repo = Path(__file__).resolve().parents[1]
    app_source = (repo / "ciao" / "web" / "app.py").read_text(encoding="utf-8")
    assert 'Route("/api/update-tasks", list_update_tasks, methods=["GET"])' in app_source
    assert (
        'Route("/api/update-tasks/{task_id}/start", start_update_task, methods=["POST"])'
        in app_source
    )
    assert (
        'Route("/api/update-tasks/{task_id}/dismiss", dismiss_update_task, methods=["POST"])'
        in app_source
    )
    assert (
        'Route("/api/update-tasks/{task_id}/reopen", reopen_update_task, methods=["POST"])'
        in app_source
    )
