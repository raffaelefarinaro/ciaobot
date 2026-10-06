"""``/api/tasks*``: the session-authenticated board routes (#1021 B3, #1033 B5).

A real config with two workspaces, a real control plane, and the signed session
cookie the ``AuthMiddleware`` checks — so these are the routes as the browser
calls them, not the handlers called directly. What is asserted is the transport
contract and the properties only this surface can be asked about: a request may
name a workspace but never a root, a write must present the revision it read,
completing works here because this is the user's session, and delegation launches
one ordinary attended chat with nothing a request could have put in the prompt.

The delegation tests are ``async``: ``start_stream`` creates an asyncio task and is
only legal on a loop, so the delegation route is deliberately not called through a
worker thread (see ``routes_tasks.task_delegate``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_tasks import (
    task_attempt_action,
    task_attempts,
    task_complete,
    task_create,
    task_delegate,
    task_delete,
    task_get,
    task_list,
    task_send_update,
    task_update,
)


class _Stream:
    """A turn the test ends by hand, which is the shape `start_stream` answers with.

    Gated *shut* by default: `subscribe()` waits before yielding, exactly as a real
    turn waits for the provider. A fake that answered instantly would settle every
    attempt to `ready_for_review` before the next line of a test ran, so every
    assertion about a running turn — the badge, a stop, a refused completion — would
    be about a turn that had already ended. `_settle(pcm)` ends them on purpose.
    """

    def __init__(self) -> None:
        self.finish = asyncio.Event()

    async def subscribe(self):
        await self.finish.wait()
        yield {"type": "result", "text": "done", "is_error": False}


class _Pcm:
    """Projects in two workspaces, chats on request, and a vault-root resolver.

    `start_stream` records the exact kwargs it was handed, which is what pins the
    no-escalation property at the transport boundary too: a delegation that named
    `unattended=True` fails here rather than silently in production.
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
        self.start_stream_kwargs: list[dict] = []
        self.start_stream_calls: list[tuple[str, str]] = []
        self.create_chat_calls: list[tuple[str, ...]] = []
        self.stopped: list[str] = []
        self.streams: list[_Stream] = []
        self._next = 0

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

    def create_chat(
        self, project_id, title="New Chat", model=None, mode=None, provider=None, helper=None
    ):
        self._next += 1
        chat_id = f"chat-{self._next}"
        self.chats[chat_id] = SimpleNamespace(
            chat_id=chat_id,
            project_id=project_id,
            title=title,
            mode=mode,
            helper=helper or {},
            pending_question="",
            pending_permission="",
        )
        self.create_chat_calls.append((project_id,))
        return self.chats[chat_id]

    def update_chat(self, chat_id, **fields):
        chat = self.chats[chat_id]
        for key, value in fields.items():
            if value is not None:
                setattr(chat, key, value)
        return chat

    def get_chat(self, chat_id: str):
        return self.chats.get(chat_id)

    def get_active_stream(self, _chat_id: str):
        return None

    def queue_message(self, _chat_id: str, _text: str, images=None, entry_id=None):
        """No turn is ever running here, so every send starts one instead.

        The manager's own refusal: ``False`` means "there was nothing to queue this
        into", which is the branch that falls through to `start_stream`.
        """
        return False

    async def stop_chat(self, chat_id: str):
        """The manager's Stop, which is `async` as the real one is.

        The recording happens inside the coroutine, so `stopped` is empty unless the
        caller awaited it — which is the assertion that matters at this boundary. An
        un-awaited `stop_chat` builds a coroutine nobody runs, so the turn keeps going
        while the attempt is recorded `stopped` over it, and a synchronous fake
        cannot see that at all.
        """
        self.stopped.append(chat_id)
        return True

    def start_stream(self, chat_id, prompt, *args, **kwargs):
        self.start_stream_kwargs.append({"args": args, **kwargs})
        self.start_stream_calls.append((chat_id, prompt))
        stream = _Stream()
        self.streams.append(stream)
        return stream


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A session-guarded app over a real config with `personal` and `work`."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr(
        "ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None
    )
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        pwa_auth_required=True,
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    pcm = _Pcm(config)
    plane = CiaoControlPlane(
        config,
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[
            # Literal `complete` first, exactly as app.py registers it.
            Route("/api/tasks", task_list, methods=["GET"]),
            Route("/api/tasks", task_create, methods=["POST"]),
            Route("/api/tasks/{task_id}/complete", task_complete, methods=["POST"]),
            Route("/api/tasks/{task_id}/delegate", task_delegate, methods=["POST"]),
            Route("/api/tasks/{task_id}/attempts", task_attempts, methods=["GET"]),
            Route(
                "/api/tasks/{task_id}/attempt/{attempt_id}/update",
                task_send_update,
                methods=["POST"],
            ),
            Route(
                "/api/tasks/{task_id}/attempt/{attempt_id}/{action}",
                task_attempt_action,
                methods=["POST"],
            ),
            Route("/api/tasks/{task_id}", task_get, methods=["GET"]),
            Route("/api/tasks/{task_id}", task_update, methods=["PATCH"]),
            Route("/api/tasks/{task_id}", task_delete, methods=["DELETE"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.mcp_service = SimpleNamespace(control_plane=plane)
    with TestClient(app) as client:
        yield (
            client,
            {SESSION_COOKIE: serializer.dumps({"user": "owner"})},
            config,
            pcm,
        )


def _create(client: TestClient, cookies: dict[str, str], **body) -> dict:
    payload = {"workspace": "personal", **body}
    response = client.post("/api/tasks", json=payload, cookies=cookies)
    assert response.status_code == 201, response.text
    return response.json()["task"]


# ── Authorization and the workspace check ───────────────────────────────


def test_the_routes_need_the_session_cookie(world) -> None:
    client, _cookies, _config, _pcm = world
    anonymous = client.get("/api/tasks?workspace=personal")
    assert anonymous.status_code == 401


def test_an_unknown_or_missing_workspace_is_a_400(world) -> None:
    client, cookies, _config, _pcm = world
    for query in ("", "?workspace=", "?workspace=somewhere-else", "?workspace=../etc"):
        response = client.get(f"/api/tasks{query}", cookies=cookies)
        assert response.status_code == 400, query
        assert response.json()["error"]["code"] == "workspace_required"

    # A write refuses it in the body, the same way.
    assert client.post("/api/tasks", json={"title": "x"}, cookies=cookies).status_code == 400
    assert (
        client.post(
            "/api/tasks",
            json={"workspace": "somewhere-else", "title": "x"},
            cookies=cookies,
        ).status_code
        == 400
    )


def test_the_board_is_per_workspace(world) -> None:
    client, cookies, _config, _pcm = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    personal = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    work = client.get("/api/tasks?workspace=work", cookies=cookies).json()
    assert [row["id"] for row in personal["tasks"]] == [mine["id"]]
    assert [row["id"] for row in work["tasks"]] == [theirs["id"]]


def test_a_task_id_from_another_workspace_is_a_404(world) -> None:
    client, cookies, _config, _pcm = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    response = client.patch(
        f"/api/tasks/{mine['id']}",
        json={"workspace": "work", "expected_revision": mine["revision"], "title": "edited"},
        cookies=cookies,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"

    unknown = client.patch(
        f"/api/tasks/{'f' * 32}",
        json={"workspace": "personal", "expected_revision": "x", "title": "edited"},
        cookies=cookies,
    )
    assert unknown.status_code == 404
    assert theirs["id"] != mine["id"]


# ── create → update → complete ──────────────────────────────────────────


def test_a_create_update_complete_round_trip(world) -> None:
    client, cookies, config, _pcm = world
    created = _create(
        client,
        cookies,
        title="Ship the board",
        body="Describe it.",
        project_id="Home",
        due="2026-10-20",
    )
    assert created["status"] == "backlog"
    assert created["project_id"] == "project-home"
    assert created["due"] == "2026-10-20"
    assert created["body"] == "Describe it."
    on_disk = (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Tasks"
        / f"{created['id']}.md"
    )
    assert on_disk.is_file()

    updated = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "title": "Ship the task board",
            "body": "Describe the board.",
        },
        cookies=cookies,
    )
    assert updated.status_code == 200, updated.text
    revised = updated.json()["task"]
    assert revised["title"] == "Ship the task board"
    assert revised["body"] == "Describe the board."
    assert revised["revision"] != created["revision"]
    # A field nobody sent is untouched.
    assert revised["due"] == "2026-10-20"

    # This is the user's own session, so completion is theirs to make.
    done = client.post(
        f"/api/tasks/{created['id']}/complete",
        json={"workspace": "personal", "expected_revision": revised["revision"]},
        cookies=cookies,
    )
    assert done.status_code == 200, done.text
    assert done.json()["task"]["status"] == "done"

    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    assert [row["status"] for row in listed["tasks"]] == ["done"]


def test_delete_removes_the_record(world) -> None:
    client, cookies, config, _pcm = world
    created = _create(client, cookies, title="A throwaway task")
    response = client.request(
        "DELETE",
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"]},
        cookies=cookies,
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert not (
        config.workspace_vault_root("personal")
        / "Workspace"
        / "Tasks"
        / f"{created['id']}.md"
    ).exists()
    assert client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"] == []


# ── Reading one task ─────────────────────────────────────────────────────


def test_a_read_carries_the_description_the_list_leaves_out(world) -> None:
    """The list rows carry no `body`, so a read is the only place a description
    comes from — without it an editor could only ever show the prose for a task
    it happened to create this session."""
    client, cookies, _config, _pcm = world
    created = _create(
        client,
        cookies,
        title="Describe me",
        body="Steps, links, acceptance criteria.",
    )

    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]
    assert "body" not in listed[0]

    response = client.get(
        f"/api/tasks/{created['id']}?workspace=personal", cookies=cookies
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["workspace"] == "personal"
    assert payload["task"]["body"] == "Steps, links, acceptance criteria."
    # The revision this read carried is the one a following edit presents.
    assert payload["task"]["revision"] == created["revision"]

    # And an edit replaces the description the read showed.
    revised = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": payload["task"]["revision"],
            "body": "Rewritten.",
        },
        cookies=cookies,
    )
    assert revised.status_code == 200, revised.text
    assert (
        client.get(f"/api/tasks/{created['id']}?workspace=personal", cookies=cookies)
        .json()["task"]["body"]
        == "Rewritten."
    )


def test_a_read_still_requires_a_workspace(world) -> None:
    client, cookies, _config, _pcm = world
    created = _create(client, cookies, title="Scoped")
    for query in ("", "?workspace=", "?workspace=somewhere-else"):
        response = client.get(f"/api/tasks/{created['id']}{query}", cookies=cookies)
        assert response.status_code == 400, query
        assert response.json()["error"]["code"] == "workspace_required"


def test_an_unknown_or_foreign_id_is_a_404_on_a_read(world) -> None:
    client, cookies, _config, _pcm = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    unknown = client.get(f"/api/tasks/{'f' * 32}?workspace=personal", cookies=cookies)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "task_not_found"

    # Another workspace's task is not readable from here either: naming the
    # workspace is the whole of the caller's authority, and it resolves to that
    # workspace's own vault.
    foreign = client.get(f"/api/tasks/{theirs['id']}?workspace=personal", cookies=cookies)
    assert foreign.status_code == 404
    assert client.get(
        f"/api/tasks/{theirs['id']}?workspace=work", cookies=cookies
    ).json()["task"]["title"] == "Work task"
    assert theirs["id"] != mine["id"]


def test_a_read_needs_the_session_cookie(world) -> None:
    client, cookies, _config, _pcm = world
    created = _create(client, cookies, title="Private")
    assert (
        client.get(f"/api/tasks/{created['id']}?workspace=personal").status_code == 401
    )


# ── Revisions are required, and a stale one writes nothing ──────────────


def test_a_write_without_an_expected_revision_is_a_400(world) -> None:
    client, cookies, _config, _pcm = world
    created = _create(client, cookies, title="Read before you write")

    patch = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "title": "written without a read"},
        cookies=cookies,
    )
    assert patch.status_code == 400
    assert patch.json()["error"]["code"] == "expected_revision_required"

    delete = client.request(
        "DELETE",
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert delete.status_code == 400
    assert delete.json()["error"]["code"] == "expected_revision_required"

    complete = client.post(
        f"/api/tasks/{created['id']}/complete",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert complete.status_code == 400
    assert complete.json()["error"]["code"] == "expected_revision_required"

    # Nothing was written or removed by any of the three.
    rows = client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]
    assert len(rows) == 1
    assert rows[0]["revision"] == created["revision"]
    assert rows[0]["title"] == "Read before you write"
    assert rows[0]["status"] == "backlog"


def test_a_stale_revision_is_a_409_and_writes_nothing(world) -> None:
    client, cookies, _config, _pcm = world
    created = _create(client, cookies, title="Original title")
    fresh = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"], "due": "2026-10-05"},
        cookies=cookies,
    ).json()["task"]

    stale = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "title": "written from a stale read",
        },
        cookies=cookies,
    )
    assert stale.status_code == 409
    body = stale.json()["error"]
    assert body["code"] == "task_revision_conflict"
    assert body["retryable"] is True

    rows = client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]
    assert rows[0]["revision"] == fresh["revision"]
    assert rows[0]["title"] == "Original title"

    assert (
        client.request(
            "DELETE",
            f"/api/tasks/{created['id']}",
            json={"workspace": "personal", "expected_revision": created["revision"]},
            cookies=cookies,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/tasks/{created['id']}/complete",
            json={"workspace": "personal", "expected_revision": created["revision"]},
            cookies=cookies,
        ).status_code
        == 409
    )
    assert len(client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"]) == 1


# ── Body shape ──────────────────────────────────────────────────────────


def test_a_bad_body_is_refused_before_anything_is_written(world) -> None:
    client, cookies, _config, _pcm = world
    not_json = client.post(
        "/api/tasks",
        content=b"[1, 2]",
        headers={"content-type": "application/json"},
        cookies=cookies,
    )
    assert not_json.status_code == 400
    assert not_json.json()["error"]["code"] == "invalid_json"

    untitled = client.post(
        "/api/tasks", json={"workspace": "personal", "title": "  "}, cookies=cookies
    )
    assert untitled.status_code == 400
    assert untitled.json()["error"]["code"] == "title_required"

    created = _create(client, cookies, title="A task")
    unknown_field = client.patch(
        f"/api/tasks/{created['id']}",
        json={
            "workspace": "personal",
            "expected_revision": created["revision"],
            "chat_id": "chat-1",
        },
        cookies=cookies,
    )
    assert unknown_field.status_code == 400
    assert unknown_field.json()["error"]["code"] == "invalid_task_field"

    nothing = client.patch(
        f"/api/tasks/{created['id']}",
        json={"workspace": "personal", "expected_revision": created["revision"]},
        cookies=cookies,
    )
    assert nothing.status_code == 400
    assert nothing.json()["error"]["code"] == "nothing_to_change"


def test_a_foreign_project_is_a_400(world) -> None:
    client, cookies, _config, _pcm = world
    response = client.post(
        "/api/tasks",
        json={"workspace": "personal", "title": "Bound elsewhere", "project_id": "project-work"},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "project_not_found"
    assert client.get("/api/tasks?workspace=personal", cookies=cookies).json()["tasks"] == []


def test_an_unavailable_control_plane_is_a_503(tmp_path: Path, monkeypatch) -> None:
    """First-run bootstrap has no control plane; that is a 503, not a 400."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[Route("/api/tasks", task_list, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.mcp_service = None
    with TestClient(app) as client:
        response = client.get(
            "/api/tasks?workspace=personal",
            cookies={SESSION_COOKIE: serializer.dumps({"user": "owner"})},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"

# ── Delegation (#1033, B5) ──────────────────────────────────────────────
#
# The transport half of delegation: the routes as the board calls them. What only
# this surface can be asked about is the request shape — a delegation presents the
# revision and carries no prompt, the gesture routes need no revision because they
# act on an attempt rather than editing a record, and everything is behind the same
# signed session cookie and the same workspace check as the board.


def _delegate(
    client: TestClient,
    cookies: dict[str, str],
    task: dict,
    pcm: _Pcm,
    workspace: str = "personal",
    **body,
):
    response = client.post(
        f"/api/tasks/{task['id']}/delegate",
        json={"workspace": workspace, "expected_revision": task["revision"], **body},
        cookies=cookies,
    )
    return response


def _settle(
    client: TestClient,
    cookies: dict[str, str],
    pcm: _Pcm,
    task_id: str,
    *,
    report: str = "",
) -> dict:
    """End every fake turn and return the task row once the watcher has settled it.

    A test calls this only when it wants a *finished* turn. A test about a running
    one deliberately does not: the attempt's `running` state, the badge and the
    refusals that depend on a live linkage are all the state a delegation is in
    between a start and its end.

    The watcher is a task on the **app's** event loop, which `TestClient` runs in
    another thread — so the test's own `asyncio.sleep` would never drive it. Opening
    the gates and then re-reading the row does: each request is submitted to that
    loop, so the watcher is already scheduled ahead of the read that waits on it.
    The read is repeated a bounded number of times rather than assumed, so a
    refusal here is a real failure instead of a timing accident.
    """
    if report:
        # The agent's own report, from the chat holding the attempt, the way a
        # delegated turn makes it before it ends (#1064).
        current = client.get(
            f"/api/tasks/{task_id}?workspace=personal", cookies=cookies
        ).json()["task"]
        client.app.state.mcp_service.control_plane.workspace_task_report(
            "personal", task_id, outcome=report, summary="Did the work.",
            chat_id=current["chat_id"],
        )
    for stream in pcm.streams:
        stream.finish.set()
    for _ in range(50):
        row = client.get(
            f"/api/tasks/{task_id}?workspace=personal", cookies=cookies
        ).json()["task"]
        if row["attempt_state"] not in ("", "running"):
            pcm.streams.clear()
            return row
    raise AssertionError(f"the turn for {task_id} never settled")


async def test_delegation_launches_one_attended_chat_and_no_prompt_body(
    world,
) -> None:
    """No `unattended`, no caller-supplied prompt, one chat.

    The kwargs are asserted at the boundary: `ProjectChatManager._effective_mode_for_chat`
    turns `unattended=True` into `bypass`, so an approval card the delegated turn
    raises would be unanswerable. And the prompt comes from the record, because a
    route key carrying prompt text would let a request rewrite the instruction.
    """
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Draft the runbook", body="Steps and links.")

    response = _delegate(client, cookies, task, pcm)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["workspace"] == "personal"
    assert payload["created"] is True
    assert payload["chat_id"] == "chat-1"
    assert payload["project_id"] == "project-general"
    assert payload["attempt"]["state"] == "running"
    assert payload["attempt"]["chat_id"] == "chat-1"
    # The task row comes back with the attempt facts a board draws from one answer.
    assert payload["task"]["attempt_state"] == "running"
    assert payload["task"]["live_attempt_id"] == payload["attempt"]["attempt_id"]
    assert payload["task"]["changed_since_delegated"] is False

    assert len(pcm.create_chat_calls) == 1
    assert len(pcm.start_stream_calls) == 1
    assert pcm.start_stream_kwargs == [{"args": ()}], pcm.start_stream_kwargs
    _chat_id, prompt = pcm.start_stream_calls[0]
    assert "Draft the runbook" in prompt
    assert "Steps and links." in prompt


async def test_a_delegation_carries_the_users_hand_over_instructions(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Draft the runbook", body="Steps and links.")

    response = _delegate(
        client, cookies, task, pcm, instructions="Keep it to one page; done = a PR link."
    )

    assert response.status_code == 200, response.text
    _chat_id, prompt = pcm.start_stream_calls[0]
    assert "Steps and links." in prompt
    assert "Keep it to one page; done = a PR link." in prompt
    assert prompt.index("Steps and links.") < prompt.index("Keep it to one page")


async def test_overlong_instructions_are_refused_before_anything_starts(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Bounded note")

    response = _delegate(client, cookies, task, pcm, instructions="x" * 4001)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_task"
    assert pcm.create_chat_calls == []
    assert pcm.start_stream_calls == []


async def test_non_text_instructions_are_refused_rather_than_stringified(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Typed note")

    response = _delegate(client, cookies, task, pcm, instructions=["do", "this"])

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_task_field"
    assert pcm.create_chat_calls == []
    assert pcm.start_stream_calls == []


async def test_a_delegation_body_cannot_carry_prompt_or_task_fields(world) -> None:
    """Only `project_id` and the hand-over `instructions` are delegation inputs
    besides the workspace and the revision, so a typo is a 400 rather than a store refusal reported as a broken
    task."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Bounded input")

    for key, value in (
        ("prompt", "do something else entirely"),
        ("body", "replaced description"),
        ("status", "done"),
        ("assignee", "user"),
    ):
        response = _delegate(client, cookies, task, pcm, **{key: value})
        assert response.status_code == 400, key
        assert response.json()["error"]["code"] == "invalid_task_field", key
    assert pcm.create_chat_calls == []
    assert pcm.start_stream_calls == []


async def test_a_delegation_needs_the_session_cookie_and_the_revision(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Guarded")

    anonymous = client.post(
        f"/api/tasks/{task['id']}/delegate",
        json={"workspace": "personal", "expected_revision": task["revision"]},
    )
    assert anonymous.status_code == 401

    for body in (
        {"workspace": "personal"},
        {"workspace": "somewhere-else", "expected_revision": task["revision"]},
        {"expected_revision": task["revision"]},
    ):
        response = client.post(
            f"/api/tasks/{task['id']}/delegate", json=body, cookies=cookies
        )
        assert response.status_code == 400, body
    assert pcm.start_stream_calls == []


async def test_a_stale_revision_is_a_409_and_starts_nothing(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Planned against an older read")
    fresh = client.patch(
        f"/api/tasks/{task['id']}",
        json={
            "workspace": "personal",
            "expected_revision": task["revision"],
            "due": "2026-10-05",
        },
        cookies=cookies,
    ).json()["task"]

    response = client.post(
        f"/api/tasks/{task['id']}/delegate",
        json={"workspace": "personal", "expected_revision": task["revision"]},
        cookies=cookies,
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "task_revision_conflict"
    assert error["retryable"] is True
    assert pcm.create_chat_calls == []
    assert pcm.start_stream_calls == []
    # And nothing on the task changed.
    assert client.get(
        f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
    ).json()["task"]["revision"] == fresh["revision"]


async def test_a_foreign_task_is_a_404_and_starts_nothing(world) -> None:
    client, cookies, _config, pcm = world
    mine = _create(client, cookies, title="Personal task")
    theirs = _create(client, cookies, title="Work task", workspace="work")

    response = client.post(
        f"/api/tasks/{mine['id']}/delegate",
        json={"workspace": "work", "expected_revision": mine["revision"]},
        cookies=cookies,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"

    unknown = client.post(
        f"/api/tasks/{'f' * 32}/delegate",
        json={"workspace": "personal", "expected_revision": "x"},
        cookies=cookies,
    )
    assert unknown.status_code == 404
    assert pcm.start_stream_calls == []
    assert theirs["id"] != mine["id"]


async def test_a_second_delegation_returns_the_attempt_and_makes_no_chat(
    world,
) -> None:
    """The double click, over HTTP: one delegation, one attempt, and a reply that
    says which happened."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Only once")

    first = _delegate(client, cookies, task, pcm).json()
    second = _delegate(
        client, cookies, client.get(
            f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
        ).json()["task"], pcm
    ).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["attempt"]["attempt_id"] == first["attempt"]["attempt_id"]
    assert second["chat_id"] == first["chat_id"]
    assert len(pcm.create_chat_calls) == 1
    assert len(pcm.start_stream_calls) == 1


async def test_the_attempt_gestures_are_reachable_and_needs_no_revision(
    world) -> None:
    """The gesture routes act on an attempt, not on a record, so they take no
    `expected_revision`: there is no field for a caller to guess and no edit being
    planned that could go stale."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Gestures")
    delegated = _delegate(client, cookies, task, pcm).json()
    attempt_id = delegated["attempt"]["attempt_id"]
    stopped = client.post(
        f"/api/tasks/{task['id']}/attempt/{attempt_id}/stop",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["attempt"]["state"] == "stopped"
    # `stopped` is only appended from inside the coroutine, so this is the stop
    # having been *awaited* and not merely built.
    assert pcm.stopped == ["chat-1"]
    # The stop reply carries the task, so the board's row updates from one answer
    # rather than reading "Running" over a turn that has ended.
    assert stopped.json()["task"]["attempt_state"] == "stopped"
    assert stopped.json()["task"]["live_attempt_id"] == ""

    detached = client.post(
        f"/api/tasks/{task['id']}/attempt/{attempt_id}/detach",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert detached.status_code == 200, detached.text
    body = detached.json()
    assert body["task"]["chat_id"] is None
    assert body["task"]["attempt_id"] is None
    # Which is what makes the task completable at all.
    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"workspace": "personal", "expected_revision": body["task"]["revision"]},
        cookies=cookies,
    )
    assert done.status_code == 200, done.text
    assert done.json()["task"]["status"] == "done"


async def test_an_unknown_attempt_verb_or_id_is_refused(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Refusals")
    attempt_id = _delegate(client, cookies, task, pcm).json()["attempt"]["attempt_id"]

    unknown = client.post(
        f"/api/tasks/{task['id']}/attempt/{'f' * 32}/stop",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "task_attempt_not_found"

    # The path pattern matches any segment, so an unknown verb reaches the handler
    # and is this surface's own refusal — 400 `invalid_action`, named as such.
    unknown_verb = client.post(
        f"/api/tasks/{task['id']}/attempt/{attempt_id}/cancel",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert unknown_verb.status_code == 400
    assert unknown_verb.json()["error"]["code"] == "invalid_action"

    anonymous = client.post(
        f"/api/tasks/{task['id']}/attempt/{attempt_id}/stop",
        json={"workspace": "personal"},
    )
    assert anonymous.status_code == 401

    unscoped = client.post(
        f"/api/tasks/{task['id']}/attempt/{attempt_id}/stop", json={}, cookies=cookies
    )
    assert unscoped.status_code == 400
    assert unscoped.json()["error"]["code"] == "workspace_required"


async def test_a_send_update_posts_one_message_and_rebinds_the_attempt(world) -> None:
    """The transport half of **Send update** (#1047).

    One ordinary message into the attempt's own chat and a rebind, over HTTP: the
    revision is presented because the message claims to carry the task as it stands,
    and no second chat, no second attempt and no keyword on the launch.
    """
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Edited under the agent", body="Original.")
    delegated = _delegate(client, cookies, task, pcm).json()
    settled = _settle(client, cookies, pcm, task["id"])
    edited = client.patch(
        f"/api/tasks/{task['id']}",
        json={
            "workspace": "personal",
            "expected_revision": settled["revision"],
            "body": "A different scope.",
        },
        cookies=cookies,
    ).json()["task"]
    assert edited["changed_since_delegated"] is True

    response = client.post(
        f"/api/tasks/{task['id']}/attempt/{delegated['attempt']['attempt_id']}/update",
        json={
            "workspace": "personal",
            "expected_revision": edited["revision"],
            "message": "The task now reads differently.",
        },
        cookies=cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] is True
    assert body["queued"] is False, "no turn was running, so one was started"
    assert body["chat_id"] == delegated["chat_id"]
    assert body["task"]["changed_since_delegated"] is False, (
        "the rebind is what retires the flag the button reads"
    )
    # One ordinary attended message in the attempt's own chat — and nothing else.
    assert pcm.start_stream_calls[-1] == (delegated["chat_id"], "The task now reads differently.")
    assert pcm.start_stream_kwargs[-1] == {"args": ()}, pcm.start_stream_kwargs
    assert len(pcm.create_chat_calls) == 1


async def test_a_send_update_needs_the_revision_the_message_and_the_session(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Guarded")
    attempt_id = _delegate(client, cookies, task, pcm).json()["attempt"]["attempt_id"]
    turns = len(pcm.start_stream_calls)
    url = f"/api/tasks/{task['id']}/attempt/{attempt_id}/update"

    def _post(**body):
        return client.post(url, json=body, cookies=cookies)

    assert _post(workspace="personal", message="No revision.").status_code == 400
    assert _post(workspace="personal", expected_revision=task["revision"]).status_code == 400
    assert _post(
        workspace="personal", expected_revision=task["revision"], message="  "
    ).status_code == 400
    assert _post(
        workspace="personal", expected_revision="0" * 64, message="Stale."
    ).status_code == 409
    # An attempt id sent under another task's URL, and an unknown one, are 404s: the
    # update acts on nothing rather than on a card the URL did not name.
    other = _create(client, cookies, title="Not this one")
    assert client.post(
        f"/api/tasks/{other['id']}/attempt/{attempt_id}/update",
        json={"workspace": "personal", "expected_revision": other["revision"], "message": "x"},
        cookies=cookies,
    ).status_code == 404
    assert client.post(
        f"/api/tasks/{task['id']}/attempt/{'f' * 32}/update",
        json={
            "workspace": "personal",
            "expected_revision": client.get(
                f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
            ).json()["task"]["revision"],
            "message": "x",
        },
        cookies=cookies,
    ).status_code == 404
    # Nothing reached the chat through any of them.
    assert len(pcm.start_stream_calls) == turns


async def test_a_send_update_carries_only_its_own_body_keys(world) -> None:
    """A typo in a body is not reported as a broken task, and a request cannot
    smuggle a task field, a prompt or a delegation in through this route."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Strict")
    attempt_id = _delegate(client, cookies, task, pcm).json()["attempt"]["attempt_id"]
    turns = len(pcm.start_stream_calls)

    for extra in ({"status": "done"}, {"chat_id": "chat-9"}, {"prompt": "do this"}):
        response = client.post(
            f"/api/tasks/{task['id']}/attempt/{attempt_id}/update",
            json={
                "workspace": "personal",
                "expected_revision": task["revision"],
                "message": "The task now reads differently.",
                **extra,
            },
            cookies=cookies,
        )
        assert response.status_code == 400, extra
        assert response.json()["error"]["code"] == "invalid_task_field"
    assert len(pcm.start_stream_calls) == turns


def test_the_attempt_route_will_not_act_on_another_tasks_attempt(world) -> None:
    """`task_id` is the route's own path segment, and it is checked.

    An attempt id is 32 hex the user never sees, so a URL naming one task with
    another's attempt id would otherwise stop a turn on a card nobody was looking
    at. The mismatch is `task_attempt_not_found` — a 404 — because from that URL
    there is no such attempt.
    """
    client, cookies, _config, _pcm = world
    mine = _create(client, cookies, title="My task")
    theirs = _create(client, cookies, title="Someone else's")
    attempt_id = _delegate(client, cookies, mine, _pcm).json()["attempt"]["attempt_id"]

    response = client.post(
        f"/api/tasks/{theirs['id']}/attempt/{attempt_id}/stop",
        json={"workspace": "personal"},
        cookies=cookies,
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "task_attempt_not_found"
    # Nothing happened on either side: my attempt is still running.
    assert (
        client.get(f"/api/tasks/{mine['id']}?workspace=personal", cookies=cookies)
        .json()["task"]["attempt_state"]
        == "running"
    )


def test_a_done_task_is_refused_by_the_delegate_route(world) -> None:
    """The refusal reaches the browser too, rather than reopening the card."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Shipped")
    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"workspace": "personal", "expected_revision": task["revision"]},
        cookies=cookies,
    ).json()["task"]

    response = client.post(
        f"/api/tasks/{task['id']}/delegate",
        json={"workspace": "personal", "expected_revision": done["revision"]},
        cookies=cookies,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_task"
    assert pcm.create_chat_calls == []
    after = client.get(f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies).json()
    assert after["task"]["status"] == "done"


def test_the_complete_route_approves_a_review_ready_result(world) -> None:
    """The review, over HTTP, as one POST — the gesture the Done button makes.

    The attempt is still *live* at this point, so this is the one case where
    `complete` reaches past the linkage. Routing it through detach first would
    settle the reviewed attempt as `stopped` and lose the result, so the route
    releases and closes in one revision-checked gesture and the attempt stays as
    `ready_for_review` history.
    """
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Ready for review")
    attempt_id = _delegate(client, cookies, task, pcm).json()["attempt"]["attempt_id"]
    before = _settle(client, cookies, pcm, task["id"], report="done")
    assert before["status"] == "in_review"

    done = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"workspace": "personal", "expected_revision": before["revision"]},
        cookies=cookies,
    )

    assert done.status_code == 200, done.text
    body = done.json()["task"]
    assert body["status"] == "done"
    assert body["chat_id"] is None
    assert body["attempt_id"] is None
    history = client.get(
        f"/api/tasks/{task['id']}/attempts?workspace=personal", cookies=cookies
    ).json()["attempts"]
    assert [row["attempt_id"] for row in history] == [attempt_id]
    assert [row["state"] for row in history] == ["ready_for_review"]


def test_the_complete_route_still_refuses_a_turn_still_running(world) -> None:
    """The narrowness is the point: a turn in flight has no result to approve."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Still running")
    _delegate(client, cookies, task, pcm)
    running = client.get(
        f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
    ).json()["task"]

    refused = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={"workspace": "personal", "expected_revision": running["revision"]},
        cookies=cookies,
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "task_invalid"
    assert client.get(
        f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
    ).json()["task"]["status"] == "in_progress"


async def test_the_attempt_history_reads_the_whole_story(world) -> None:
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="History")
    first = _delegate(client, cookies, task, pcm).json()
    stopped = client.post(
        f"/api/tasks/{task['id']}/attempt/{first['attempt']['attempt_id']}/stop",
        json={"workspace": "personal"},
        cookies=cookies,
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["attempt"]["state"] == "stopped"
    retried = _delegate(
        client, cookies, client.get(
            f"/api/tasks/{task['id']}?workspace=personal", cookies=cookies
        ).json()["task"], pcm
    ).json()

    response = client.get(
        f"/api/tasks/{task['id']}/attempts?workspace=personal", cookies=cookies
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["workspace"] == "personal"
    # The live attempt leads, and the stopped one it replaced is still there.
    assert [row["attempt_id"] for row in payload["attempts"]] == [
        retried["attempt"]["attempt_id"],
        first["attempt"]["attempt_id"],
    ]
    assert payload["attempts"][0]["state"] == "running"
    assert payload["attempts"][1]["state"] == "stopped"
    assert payload["task"]["id"] == task["id"]

    for query in ("", "?workspace=", "?workspace=somewhere-else"):
        refused = client.get(
            f"/api/tasks/{task['id']}/attempts{query}", cookies=cookies
        )
        assert refused.status_code == 400, query
        assert refused.json()["error"]["code"] == "workspace_required"
    assert client.get(
        f"/api/tasks/{task['id']}/attempts?workspace=personal"
    ).status_code == 401


async def test_an_edit_after_delegation_is_reported_on_the_row(world) -> None:
    """The flag has to survive the round trip the board actually makes: read the
    task, edit it, and the answer says the description the agent was handed is no
    longer the one on disk."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Edited after", body="Original scope.")
    delegated = _delegate(client, cookies, task, pcm).json()

    edited = client.patch(
        f"/api/tasks/{task['id']}",
        json={
            "workspace": "personal",
            "expected_revision": delegated["task"]["revision"],
            "body": "A different scope.",
        },
        cookies=cookies,
    )

    assert edited.status_code == 200, edited.text
    row = edited.json()["task"]
    assert row["attempt_state"] == "running"
    assert row["changed_since_delegated"] is True
    # And the list row agrees, so the badge and the warning cannot disagree.
    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    assert listed["tasks"][0]["changed_since_delegated"] is True


async def test_a_detach_clears_the_warning_it_itself_would_have_raised(world) -> None:
    """A detach clears the linkage, and clearing it moves the task's revision.

    Comparing the attempt's revision against the record unconditionally would
    therefore make the gesture the user just performed read as "the result was
    reached against an older description" — on the very card that has just stopped
    delegating anything. An unlinked task says nothing about it.
    """
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="Let go")
    delegated = _delegate(client, cookies, task, pcm).json()
    assert delegated["task"]["changed_since_delegated"] is False

    detached = client.post(
        f"/api/tasks/{task['id']}/attempt/{delegated['attempt']['attempt_id']}/detach",
        json={"workspace": "personal"},
        cookies=cookies,
    )

    assert detached.status_code == 200, detached.text
    row = detached.json()["task"]
    assert row["attempt_id"] is None
    assert row["changed_since_delegated"] is False
    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    assert listed["tasks"][0]["changed_since_delegated"] is False


async def test_a_delegated_task_cannot_be_completed_while_the_attempt_is_live(
    world,
) -> None:
    """The board's own completion route refuses a linked task: a turn in flight is
    not something the user closes behind its back, and this is the surface where
    that used to be the one gesture that worked."""
    client, cookies, _config, pcm = world
    task = _create(client, cookies, title="In flight")
    delegated = _delegate(client, cookies, task, pcm).json()

    response = client.post(
        f"/api/tasks/{task['id']}/complete",
        json={
            "workspace": "personal",
            "expected_revision": delegated["task"]["revision"],
        },
        cookies=cookies,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "task_invalid"
    listed = client.get("/api/tasks?workspace=personal", cookies=cookies).json()
    assert listed["tasks"][0]["status"] == "in_progress"


async def test_an_unavailable_control_plane_is_a_503_on_delegation(
    tmp_path: Path, monkeypatch
) -> None:
    """First-run bootstrap has no control plane. Delegation starts a turn, so a 503
    is the honest answer — a 400 would read as a malformed request the user could
    fix by editing the body."""
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".runtime"))
    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", lambda *_a, **_kw: None)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    serializer = URLSafeTimedSerializer("test-token")
    app = Starlette(
        routes=[Route("/api/tasks/{task_id}/delegate", task_delegate, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = config
    app.state.mcp_service = None
    with TestClient(app) as client:
        response = client.post(
            f"/api/tasks/{'a' * 32}/delegate",
            json={"workspace": "personal", "expected_revision": "x"},
            cookies={SESSION_COOKIE: serializer.dumps({"user": "owner"})},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"
