"""The workspace-scoped task service in ``ciao.control_plane`` (#1021, B3).

The store in ``ciao.task_board.py`` is inert file handling and owns the record
contract; these tests are about the layer this child adds over it and about the
two properties a shared service has to hold no matter which surface called it:

- **The root is never supplied.** Every method resolves the workspace's own
  vault, so a task can only ever be written where the registry says that
  workspace's vault is, and a workspace's board is invisible from another one.
- **The actor decides what it may do.** The agent wrappers act as
  ``actor="agent"``, so completion is refused; the workspace-scoped methods the
  session routes call act as the user. ``project_id`` is checked against the
  workspace's live projects here, because a file store has no project registry.

Real config and a real control plane over throwaway vaults: no live install, no
models, no services.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, ControlPlaneError, CiaoControlPlane

TASK_ID_CHARS = "0123456789abcdef"


class _Pcm:
    """Projects in two workspaces, and the vault-root resolver the real one has."""

    def __init__(self, config: CiaoConfig) -> None:
        self.config = config
        self.projects = {
            "project-home": SimpleNamespace(
                project_id="project-home", name="Home", workspace="personal"
            ),
            "project-work": SimpleNamespace(
                project_id="project-work", name="Work", workspace="work"
            ),
            # A name only `work` owns: a `personal` task may not name it.
            "project-dupe": SimpleNamespace(
                project_id="project-dupe", name="Shared", workspace="work"
            ),
        }

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

    def get_chat(self, _chat_id: str):
        return None

    def get_active_stream(self, _chat_id: str):
        return None


def _world(tmp_path: Path) -> CiaoControlPlane:
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
    return CiaoControlPlane(
        config,
        project_chat_manager=_Pcm(config),
        schedule_manager=SimpleNamespace(),
    )


def _principal(workspace: str = "personal") -> AgentPrincipal:
    return AgentPrincipal(
        token_id="t",
        chat_id="chat-1",
        project_id="project-home",
        workspace=workspace,
        provider="claude",
    )


def _tasks_dir(plane: CiaoControlPlane, workspace: str) -> Path:
    return plane.config.workspace_vault_root(workspace) / "Workspace" / "Tasks"


# The four workspace-scoped methods the session routes and the agent wrappers
# both go through. Spelled once so a test body reads as the operation it is.
def _create(plane: CiaoControlPlane, workspace: str, **fields: Any) -> dict[str, Any]:
    return plane.workspace_task_create(workspace, **fields)


def _get(plane: CiaoControlPlane, workspace: str, task_id: str) -> dict[str, Any]:
    return plane.workspace_task_get(workspace, task_id)


def _update(
    plane: CiaoControlPlane, workspace: str, task_id: str, **fields: Any
) -> dict[str, Any]:
    return plane.workspace_task_update(workspace, task_id, **fields)


def _action(
    plane: CiaoControlPlane, workspace: str, action: str, task_id: str, **fields: Any
) -> dict[str, Any]:
    return plane.workspace_task_action(workspace, action, task_id, **fields)


# ── create / get / list / update ──────────────────────────────────────────


def test_create_get_and_list_round_trip_through_the_service(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    created = _create(
        plane,
        "personal",
        title="Draft the migration runbook",
        body="Steps, links and acceptance criteria.",
        due="2026-10-20",
    )
    assert len(created["id"]) == 32
    assert all(char in TASK_ID_CHARS for char in created["id"])
    assert created["title"] == "Draft the migration runbook"
    assert created["status"] == "backlog"
    assert created["assignee"] == "user"
    assert created["body"] == "Steps, links and acceptance criteria."
    assert created["relative_path"] == f"Workspace/Tasks/{created['id']}.md"
    # Every write has to present this back, so it is on every payload.
    assert created["revision"]

    fetched = _get(plane, "personal", created["id"])
    assert fetched["revision"] == created["revision"]
    assert fetched["body"] == "Steps, links and acceptance criteria."

    rows = plane.workspace_task_list("personal")
    assert [row["id"] for row in rows] == [created["id"]]
    # A list row carries no body: a board draws many cards, not many documents.
    assert "body" not in rows[0]

    updated = _update(
        plane,
        "personal",
        created["id"],
        expected_revision=created["revision"],
        changes={"title": "Draft the rollback runbook"},
    )
    assert updated["title"] == "Draft the rollback runbook"
    assert updated["revision"] != created["revision"]
    assert _get(plane, "personal", created["id"])["title"] == "Draft the rollback runbook"


def test_a_stale_revision_is_a_retryable_refusal_that_writes_nothing(
    tmp_path: Path,
) -> None:
    plane = _world(tmp_path)
    created = _create(plane, "personal", title="Plan the launch")
    _update(
        plane,
        "personal",
        created["id"],
        expected_revision=created["revision"],
        changes={"due": "2026-10-05"},
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        _update(
            plane,
            "personal",
            created["id"],
            expected_revision=created["revision"],
            changes={"title": "written from a stale read"},
        )
    assert excinfo.value.code == "task_revision_conflict"
    # Retryable means "re-read and re-plan", not "resend the same revision".
    assert excinfo.value.retryable is True
    assert excinfo.value.payload()["retryable"] is True
    assert _get(plane, "personal", created["id"])["due"] == "2026-10-05"
    assert _get(plane, "personal", created["id"])["title"] == "Plan the launch"


def test_an_unknown_task_is_not_found_and_a_malformed_id_is_invalid(tmp_path: Path) -> None:
    plane = _world(tmp_path)

    with pytest.raises(ControlPlaneError) as excinfo:
        _get(plane, "personal", "f" * 32)
    assert excinfo.value.code == "task_not_found"

    # A malformed id never becomes a path: it is refused as an invalid task,
    # not answered as a missing one.
    with pytest.raises(ControlPlaneError) as excinfo:
        _get(plane, "personal", "../../etc/passwd")
    assert excinfo.value.code == "task_invalid"


def test_an_unreadable_file_is_a_row_not_a_missing_task(tmp_path: Path) -> None:
    """A malformed file must not read as an empty or healthy board."""
    plane = _world(tmp_path)
    good = _create(plane, "personal", title="A readable task")

    broken_id = "e" * 32
    tasks = _tasks_dir(plane, "personal")
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{broken_id}.md").write_text(
        f"---\nschema: 2\nid: {broken_id}\ntitle: no status here\n---\n", encoding="utf-8"
    )

    rows = plane.workspace_task_list("personal")
    assert [row["id"] for row in rows if "code" not in row] == [good["id"]]
    unreadable = [row for row in rows if "code" in row]
    assert [row["id"] for row in unreadable] == [broken_id]
    assert unreadable[0]["code"] == "invalid_task"
    assert unreadable[0]["path"] == f"Workspace/Tasks/{broken_id}.md"


# ── The actor decides completion ──────────────────────────────────────────


def test_an_agent_cannot_complete_a_task_by_any_route(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    principal = _principal()
    created = plane.task_create(principal, title="Review the diff")
    assert created["ok"] is True
    task = created["data"]

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_action(
            principal, "complete", task["id"], expected_revision=task["revision"]
        )
    assert excinfo.value.code == "task_completion_requires_user"
    assert excinfo.value.retryable is False

    # Spelling it as a plain status edit is the same refusal, not a way round.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_update(
            principal,
            task["id"],
            expected_revision=task["revision"],
            changes={"status": "done"},
        )
    assert excinfo.value.code == "task_completion_requires_user"

    # And a move to Done is the same thing: the rule is about the status, not
    # about the verb the caller used to ask for it.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_action(
            principal, "move", task["id"], expected_revision=task["revision"], status="done"
        )
    assert excinfo.value.code == "task_completion_requires_user"
    assert _get(plane, "personal", task["id"])["status"] == "backlog"


def test_a_user_completes_a_task_through_the_workspace_scoped_method(
    tmp_path: Path,
) -> None:
    """The same service with `actor="user"` — which is what the session routes pass."""
    plane = _world(tmp_path)
    created = _create(plane, "personal", title="Review the diff")

    done = _action(
        plane, "personal", "complete", created["id"],
        expected_revision=created["revision"], actor="user",
    )
    assert done["status"] == "done"
    assert _get(plane, "personal", created["id"])["status"] == "done"


def test_move_and_reassign_are_ordinary_status_and_assignee_edits(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    created = _create(plane, "personal", title="Wire the board")

    started = _action(
        plane, "personal", "move", created["id"],
        expected_revision=created["revision"], status="in_progress",
    )
    assert started["status"] == "in_progress"

    with pytest.raises(ControlPlaneError) as excinfo:
        _action(plane, "personal", "move", created["id"], expected_revision=started["revision"])
    assert excinfo.value.code == "invalid_action"

    reassigned = _action(
        plane, "personal", "reassign", created["id"],
        expected_revision=started["revision"], assignee="agent",
    )
    assert reassigned["assignee"] == "agent"

    for action, fields in (
        ("archive", {}),
        ("reassign", {}),
        ("move", {"status": "done"}),
    ):
        with pytest.raises(ControlPlaneError) as excinfo:
            if action == "archive":
                _action(
                    plane, "personal", action, created["id"],
                    expected_revision=reassigned["revision"],
                )
            else:
                _action(
                    plane, "personal", action, created["id"],
                    expected_revision=reassigned["revision"], **fields,
                )
    # `move --to done` is the completion rule, not an invalid action.
    assert excinfo.value.code in ("invalid_action", "task_completion_requires_user")


# ── Live project membership ───────────────────────────────────────────────


def test_a_task_may_name_a_project_of_its_own_workspace(tmp_path: Path) -> None:
    plane = _world(tmp_path)

    by_id = _create(plane, "personal", title="Bound by id", project_id="project-home")
    assert by_id["project_id"] == "project-home"
    # The surface takes a name as readily as an id.
    by_name = _create(plane, "personal", title="Bound by name", project_id="Home")
    assert by_name["project_id"] == "project-home"

    moved = _update(
        plane, "personal", by_id["id"],
        expected_revision=by_id["revision"], changes={"project_id": "Home"},
    )
    assert moved["project_id"] == "project-home"


def test_a_foreign_or_unknown_project_is_refused(tmp_path: Path) -> None:
    plane = _world(tmp_path)

    for ref in ("project-work", "Shared", "no-such-project"):
        with pytest.raises(ControlPlaneError) as excinfo:
            _create(plane, "personal", title="Wrong project", project_id=ref)
        assert excinfo.value.code in ("project_not_found", "project_ambiguous"), ref

    # The same rule applies to an edit, and it is refused before the store is
    # ever asked to write.
    created = _create(plane, "personal", title="No project")
    with pytest.raises(ControlPlaneError) as excinfo:
        _update(
            plane, "personal", created["id"],
            expected_revision=created["revision"], changes={"project_id": "project-work"},
        )
    assert excinfo.value.code == "project_not_found"
    assert _get(plane, "personal", created["id"])["project_id"] is None


def test_an_ambiguous_project_name_is_named_as_such(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    plane.pcm.projects["project-home-dupe"] = SimpleNamespace(
        project_id="project-home-dupe", name="Home", workspace="personal"
    )

    with pytest.raises(ControlPlaneError) as excinfo:
        _create(plane, "personal", title="Ambiguous", project_id="Home")
    assert excinfo.value.code == "project_ambiguous"


# ── The root is never supplied ────────────────────────────────────────────


def test_the_store_is_built_from_the_authoritative_workspace_vault(
    tmp_path: Path,
) -> None:
    plane = _world(tmp_path)
    created = _create(plane, "personal", title="Where does this live?")

    expected = Path(plane.config.workspace_vault_root("personal")).resolve()
    store = plane._task_store("personal")
    assert store._workspace == "personal"
    assert store._vault_root == expected
    assert store._runtime_dir == tmp_path / ".runtime"
    # The record is in that root's own Tasks directory, and nowhere else.
    assert (expected / "Workspace" / "Tasks" / f"{created['id']}.md").is_file()
    assert created["relative_path"] == f"Workspace/Tasks/{created['id']}.md"


def test_a_task_is_invisible_and_unreachable_from_another_workspace(
    tmp_path: Path,
) -> None:
    plane = _world(tmp_path)
    mine = _create(plane, "personal", title="Private to personal")
    theirs = _create(plane, "work", title="Private to work")

    assert [row["id"] for row in plane.workspace_task_list("personal")] == [mine["id"]]
    assert [row["id"] for row in plane.workspace_task_list("work")] == [theirs["id"]]

    # The id is valid and the file exists — in the *other* workspace. Scope, not
    # existence, is what refuses it.
    with pytest.raises(ControlPlaneError) as excinfo:
        _get(plane, "work", mine["id"])
    assert excinfo.value.code == "task_not_found"
    with pytest.raises(ControlPlaneError) as excinfo:
        _update(
            plane, "work", mine["id"],
            expected_revision=mine["revision"], changes={"title": "edited"},
        )
    assert excinfo.value.code == "task_not_found"
    assert _get(plane, "personal", mine["id"])["title"] == "Private to personal"


def test_a_workspace_that_is_not_registered_never_becomes_a_path(tmp_path: Path) -> None:
    plane = _world(tmp_path)

    for name in ("somewhere-else", "", "../../etc", "personal/../work"):
        with pytest.raises(ControlPlaneError) as excinfo:
            plane.workspace_task_list(name)
        assert excinfo.value.code == "workspace_not_found", name
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_vault_root("somewhere-else")
    assert excinfo.value.code == "workspace_not_found"


def test_an_agents_tasks_are_its_own_workspaces_and_nothing_else(tmp_path: Path) -> None:
    """The principal's own claim is the scope; a request body never widens it."""
    plane = _world(tmp_path)
    personal = _principal("personal")
    work = _principal("work")
    mine = plane.task_create(personal, title="Personal work")
    theirs = plane.task_create(work, title="Work")

    assert [row["id"] for row in plane.task_list(personal)] == [mine["data"]["id"]]
    assert [row["id"] for row in plane.task_list(work)] == [theirs["data"]["id"]]
    with pytest.raises(ControlPlaneError) as excinfo:
        _get(plane, "work", mine["data"]["id"])
    assert excinfo.value.code == "task_not_found"
    with pytest.raises(ControlPlaneError) as excinfo:
        plane._workspace(personal, "work")
    assert excinfo.value.code == "workspace_forbidden"


def test_the_agent_methods_answer_in_the_envelope_the_cli_prints(tmp_path: Path) -> None:
    """`task_list` is a bare list; every other agent method is `_ok(...)`.

    `_invoke` passes a dict straight through and wraps anything else, so a task
    operation that returned a bare payload would put the record where the
    envelope's `data` belongs and break every consumer of `ok`.
    """
    plane = _world(tmp_path)
    principal = _principal()

    created = plane.task_create(principal, title="Envelope check")
    assert created["ok"] is True
    task_id = created["data"]["id"]
    for envelope in (
        plane.task_get(principal, task_id),
        plane.task_update(
            principal, task_id, expected_revision=created["data"]["revision"],
            changes={"due": "2026-10-05"},
        ),
        plane.task_action(
            principal, "move", task_id,
            expected_revision=plane.task_get(principal, task_id)["data"]["revision"],
            status="in_progress",
        ),
    ):
        assert envelope["ok"] is True, envelope
        assert envelope["data"]["id"] == task_id
    rows = plane.task_list(principal)
    assert isinstance(rows, list) and rows[0]["id"] == task_id


# ── Removal ───────────────────────────────────────────────────────────────


def test_removing_a_task_needs_the_revision_it_was_read_at(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    created = _create(plane, "personal", title="A throwaway task")
    tasks = _tasks_dir(plane, "personal")
    assert (tasks / f"{created['id']}.md").is_file()

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_delete("personal", created["id"], expected_revision="0" * 64)
    assert excinfo.value.code == "task_revision_conflict"
    assert excinfo.value.retryable is True
    assert (tasks / f"{created['id']}.md").is_file()

    removed = plane.workspace_task_delete(
        "personal", created["id"], expected_revision=created["revision"]
    )
    assert removed == {"id": created["id"], "deleted": True}
    assert not (tasks / f"{created['id']}.md").exists()
    assert plane.workspace_task_list("personal") == []

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_delete("personal", created["id"], expected_revision=created["revision"])
    assert excinfo.value.code == "task_not_found"


def test_a_malformed_id_is_never_removed_as_a_path(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    outside = tmp_path / "keep-me.md"
    outside.write_text("not a task\n", encoding="utf-8")

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_delete("personal", "../keep-me", expected_revision="x")
    assert excinfo.value.code == "task_invalid"
    assert outside.read_text(encoding="utf-8") == "not a task\n"

# ── Resolutions, the read shape and the delegation log ─────────────────────


def test_a_read_separates_the_description_history_and_log(tmp_path: Path) -> None:
    from ciao.task_log import LOG_CLOSE, LOG_HEADING, LOG_OPEN

    plane = _world(tmp_path)
    created = _create(plane, "personal", title="Separate me", body="The work.")
    done = _action(
        plane, "personal", "complete", created["id"],
        expected_revision=created["revision"], actor="user",
        resolution="Merged after review.",
    )
    assert done["resolution"] == "Merged after review."
    assert done["has_resolution"] is True
    assert done["body"] == "The work."
    assert done["delegation_log"] == ""

    # The log is written by the attempt store's own path: a body carrying it.
    logged = _get(plane, "personal", created["id"])["body"]
    logged_body = (
        f"{logged}\n\n{LOG_OPEN}\n{LOG_HEADING}\n\n"
        f"- 2026-10-08T09:00:00+00:00 · Attempt one\n{LOG_CLOSE}\n"
    )
    _update(
        plane, "personal", created["id"],
        expected_revision=done["revision"], changes={}, body=logged_body, actor="user",
    )
    read = _get(plane, "personal", created["id"])
    assert "Attempt one" in read["delegation_log"]
    assert LOG_OPEN not in read["body"]
    assert LOG_HEADING not in read["body"]
    assert "Attempt one" not in read["body"]
    assert [item["resolution"] for item in read["completions"]] == ["Merged after review."]

    # A description save that leaves the log out keeps the stored log.
    after_save = _update(
        plane, "personal", created["id"],
        expected_revision=read["revision"], changes={}, body="The work, reworded.", actor="user",
    )
    assert after_save["body"] == "The work, reworded."
    assert "Attempt one" in _get(plane, "personal", created["id"])["delegation_log"]
    assert [item["resolution"] for item in _get(plane, "personal", created["id"])["completions"]] == [
        "Merged after review."
    ]


def test_a_resolution_is_the_users_and_only_on_a_done_task(tmp_path: Path) -> None:
    plane = _world(tmp_path)
    principal = _principal()
    task = _create(plane, "personal", title="Mine to close")

    # An agent cannot record a resolution by any route.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.task_action(
            principal, "complete", task["id"],
            expected_revision=task["revision"], resolution="I did it.",
        )
    assert excinfo.value.code == "task_completion_requires_user"
    with pytest.raises(ControlPlaneError) as excinfo:
        _update(
            plane, "personal", task["id"],
            expected_revision=task["revision"], changes={}, resolution="I did it.",
            actor="agent",
        )
    assert excinfo.value.code == "task_completion_requires_user"
    assert _get(plane, "personal", task["id"])["status"] == "backlog"

    # A resolution on a gesture that is not a completion is refused.
    with pytest.raises(ControlPlaneError) as excinfo:
        _action(
            plane, "personal", "move", task["id"],
            expected_revision=task["revision"], status="in_progress",
            actor="user", resolution="Not a completion.",
        )
    assert excinfo.value.code == "invalid_action"

    # A non-string is refused before the write, and nothing is recorded.
    with pytest.raises(ControlPlaneError) as excinfo:
        _action(
            plane, "personal", "complete", task["id"],
            expected_revision=task["revision"], actor="user", resolution=7,
        )
    assert excinfo.value.code == "invalid_task"
    assert _get(plane, "personal", task["id"])["status"] == "backlog"
    assert _get(plane, "personal", task["id"])["completions"] == []

    # A done task with no completion record has nothing to reword.
    legacy = _create(plane, "personal", title="Legacy")
    path = _tasks_dir(plane, "personal") / f"{legacy['id']}.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: backlog", "status: done", 1),
        encoding="utf-8",
    )
    legacy_now = _get(plane, "personal", legacy["id"])
    with pytest.raises(ControlPlaneError) as excinfo:
        _update(
            plane, "personal", legacy["id"],
            expected_revision=legacy_now["revision"], changes={}, resolution="Late note.",
            actor="user",
        )
    assert excinfo.value.code == "invalid_task"


def test_a_completion_window_is_inclusive_and_refuses_a_bad_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from ciao.task_board import TaskBoardStore

    plane = _world(tmp_path)
    moment = datetime(2026, 10, 7, 23, 59, 59, tzinfo=UTC)
    monkeypatch.setattr(TaskBoardStore, "_now", lambda self: moment)
    task = _create(plane, "personal", title="Boundary")
    _action(
        plane, "personal", "complete", task["id"],
        expected_revision=task["revision"], actor="user",
    )

    ids = lambda **kw: [row["id"] for row in plane.workspace_task_list("personal", **kw)]
    assert ids(completed_since="2026-10-07T23:59:59+00:00") == [task["id"]]
    assert ids(completed_since="2026-10-08T00:00:00+00:00") == []
    assert ids(completed_before="2026-10-07T23:59:59+00:00") == []
    assert ids(completed_before="2026-10-08T00:00:00+00:00") == [task["id"]]
    # A naive bound is read as UTC, the store's own convention.
    assert ids(completed_since="2026-10-07T23:59:59") == [task["id"]]

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.workspace_task_list("personal", completed_since="yesterday")
    assert excinfo.value.code == "invalid_task"
