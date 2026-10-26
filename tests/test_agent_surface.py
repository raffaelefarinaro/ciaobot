"""The agent CLI surface: dispatcher, route, CLI mapping and prompt variant."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import io
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import agent_cli
from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV
from ciao.core_prompt import system_prompt_payload
from ciao.web.routes_agent import agent_dispatch_endpoint
from tests.test_mcp_server import _service


def _client(service, *, peer: tuple[str, int] = ("127.0.0.1", 5555)) -> TestClient:
    app = Starlette(routes=[Route("/agent/v1/{op}", agent_dispatch_endpoint, methods=["POST"])])
    app.state.mcp_service = service
    return TestClient(app, base_url="http://127.0.0.1:18443", client=peer)


def _token(service, chat_id: str = "chat-1", workspace: str = "personal") -> str:
    token, _ = service.registry.issue(
        chat_id=chat_id, project_id="project-1", workspace=workspace, provider="claude"
    )
    return token


def _post(client: TestClient, token: str, op: str, arguments: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/agent/v1/{op}", headers=headers, json=arguments if arguments is not None else {})


def test_dispatch_requires_a_valid_bearer_token(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    with _client(service) as client:
        assert _post(client, "", "context_get").status_code == 401
        bad = _post(client, "not-a-token", "context_get")
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"


def test_dispatch_runs_the_registered_tool_and_tags_telemetry_cli(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        response = _post(client, token, "context_get")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "data": {"chat_id": "chat-1", "workspace": "personal", "system": {"server": "ok"}},
    }
    record = json.loads(service._telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["surface"] == "cli"
    assert record["tool"] == "context_get"
    assert record["chat_id"] == "chat-1"
    assert record["status"] == "ok"


def test_unknown_operation_and_bad_arguments_are_envelopes_with_telemetry(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        unknown = _post(client, token, "frobnicate")
        bad = _post(client, token, "vault_search", {"query": "x", "limit": "many"})
        not_object = client.post(
            "/agent/v1/vault_search", headers={"Authorization": f"Bearer {token}"}, content=b"[1,2]"
        )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "unknown_operation"
    assert bad.status_code == 400
    assert bad.json()["ok"] is False
    assert bad.json()["error"]["code"] == "invalid_request"
    assert not_object.status_code == 400
    records = [json.loads(line) for line in service._telemetry_path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["tool"] == "vault_search"
    assert records[-1]["status"] == "error"
    assert records[-1]["error_code"] == "invalid_request"
    assert records[-1]["surface"] == "cli"


def test_plan_mode_gate_applies_through_the_cli_surface(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, mode="plan")
    token = _token(service)
    with _client(service) as client:
        response = _post(client, token, "memory_update", {"region": "memory", "action": "add", "entry": "x"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_mode_read_only"


def test_context_logs_is_a_read() -> None:
    from ciao import mcp_server

    assert mcp_server.OPERATIONS_BY_NAME["context_logs"].annotations == mcp_server._READ


def test_rare_admin_operations_dispatch_on_cli_only(tmp_path: Path) -> None:
    """S2 removed the rare-admin group from the MCP catalog but the dispatcher
    must still run them as `ciao <noun> <verb>` commands."""
    removed = {
        "context_get", "gws_status", "projects_list", "project_get",
        "project", "project_action", "workspaces_list",
    }
    service, _ = _service(tmp_path)
    assert removed <= set(service.operation_table)


def test_chat_operations_dispatch_on_cli_only(tmp_path: Path) -> None:
    """S3 removed the chat-lifecycle group from the MCP catalog but the
    dispatcher must still run them as `ciao chat …` commands."""
    chat_group = {
        "chats_list", "chat_get", "chat_create", "chat_update", "chat_send",
        "chat_continue", "chat_retry", "chat_handover", "chat_reload", "chat_archive",
        "chat_delete", "chat_stop",
    }
    service, _ = _service(tmp_path)
    assert chat_group <= set(service.operation_table)


def test_note_verification_dispatches_on_cli_only_and_is_write_gated(
    tmp_path: Path,
) -> None:
    """The managed operation that replaced hand-editing stale notes.

    Two properties beyond registration. It is `mutating` — the annotations are
    what `service._invoke`'s plan-mode gate reads, so a `_READ` entry here would
    let a plan-mode chat write a note. And its whole schema is one path: there is
    no argument through which a note's text could arrive, which is the property
    the CLI cannot express and the server has to hold.
    """
    from ciao import mcp_server

    service, _ = _service(tmp_path)
    assert "verify_note" in service.operation_table
    operation = mcp_server.OPERATIONS_BY_NAME["verify_note"]
    assert operation.annotations.readOnlyHint is False
    assert operation.annotations.destructiveHint is False
    assert set(inspect.signature(operation.fn).parameters) == {"service", "payload_file"}


def test_note_related_is_a_read(tmp_path: Path) -> None:
    from ciao import mcp_server

    service, _ = _service(tmp_path)
    assert "note_related" in service.operation_table
    assert mcp_server.OPERATIONS_BY_NAME["note_related"].annotations == mcp_server._READ


def test_a_plan_mode_chat_cannot_verify_a_note(tmp_path: Path) -> None:
    """The gate, end to end, through the CLI surface."""
    service, _ = _service(tmp_path, mode="plan")
    token = _token(service)
    with _client(service) as client:
        response = _post(client, token, "verify_note", {"payload_file": "verify.json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_mode_read_only"


def test_run_and_schedule_operations_dispatch_on_cli_only(tmp_path: Path) -> None:
    """S4 removed the background-run and schedule groups from the MCP catalog
    but the dispatcher must still run them as `ciao run …` / `ciao schedule …`
    commands."""
    group = {
        "background_run_start", "background_run_status", "background_run_cancel",
        "schedules_list", "schedule", "schedule_action",
    }
    service, _ = _service(tmp_path)
    assert group <= set(service.operation_table)


# ── The task board surface (#1021, B3) ────────────────────────────────────
#
# One workspace-scoped service behind both `ciao task …` and `/api/tasks*`, so
# what an agent can do to a task is a property of the operation table and the
# store, not of whichever surface asked.


def _task_service(tmp_path: Path, *, mode: str = "auto"):
    """A real control plane over two sibling workspaces, plus a dispatcher."""
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.control_plane import CiaoControlPlane

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

    class _Pcm:
        """Only what the task service reads: one chat's mode, and the roots."""

        def __init__(self) -> None:
            self.chat = SimpleNamespace(
                mode=mode, user_turn_count=1, user_turn_unattended={},
                user_turn_origin={}, helper={},
            )

        def _workspace_vault_root(self, workspace: str) -> Path:
            return config.workspace_vault_root(workspace)

        def get_chat(self, _chat_id: str):
            return self.chat

        def get_active_stream(self, _chat_id: str):
            return None

        def get_project(self, _project_id: str):
            return None

        def list_projects(self, _workspace: str | None = None):
            return []

    service, _fake = _service(tmp_path)
    service.bind(
        CiaoControlPlane(
            config, project_chat_manager=_Pcm(), schedule_manager=SimpleNamespace()
        )
    )
    return service


def test_task_operations_register_reads_writes_and_destructive_delete(
    tmp_path: Path,
) -> None:
    """Reads are `_READ`, ordinary writes `_WRITE`, permanent deletion `_DESTRUCTIVE`.

    The annotations are what the plan-mode gate and the approval split read, so
    a `_READ` entry on a write would let a plan-mode chat file a task. And
    `task_action` is `_WRITE`, not `_DESTRUCTIVE`: the store refuses an agent's
    completion outright, so it carries no reachable destructive effect.
    `task_delete` *is* `_DESTRUCTIVE`, because it unlinks the user's record with
    no trash; the control plane separately refuses it on an unattended turn.
    `task_attempt_action` is also `_DESTRUCTIVE`, because its `stop` ends a turn
    irreversibly — the same reason `chat_stop` is ask-class.
    """
    from ciao import mcp_server

    service = _task_service(tmp_path)
    assert {
        "task_list", "task_get", "task_create", "task_update", "task_delete",
        "task_action", "task_delegate", "task_attempt_action",
    } <= set(service.operation_table)
    for name in ("task_list", "task_get"):
        assert mcp_server.OPERATIONS_BY_NAME[name].annotations == mcp_server._READ
    for name in ("task_create", "task_update", "task_action", "task_delegate"):
        annotations = mcp_server.OPERATIONS_BY_NAME[name].annotations
        assert annotations == mcp_server._WRITE, name
        assert annotations.readOnlyHint is False
    for name in ("task_delete", "task_attempt_action"):
        assert (
            mcp_server.OPERATIONS_BY_NAME[name].annotations
            == mcp_server._DESTRUCTIVE
        ), name


def test_an_agent_can_file_and_move_a_task_but_completes_only_with_a_reason(
    tmp_path: Path,
) -> None:
    """The store's completion rule, end to end through the CLI surface.

    Filing and moving work; completing does not unless the user's reason comes
    with it, however it is spelled, and a refusal leaves the record exactly
    where it was.
    """
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Review the diff", "due": "2026-10-20"})
        assert created.status_code == 200, created.text
        task = created.json()["data"]
        assert task["title"] == "Review the diff"
        assert task["due"] == "2026-10-20"

        listed = _post(client, token, "task_list", {})
        assert [row["id"] for row in listed.json()["data"]] == [task["id"]]

        moved = _post(
            client,
            token,
            "task_action",
            {"action": "move", "task_id": task["id"], "status": "in_progress",
             "expected_revision": task["revision"]},
        )
        assert moved.status_code == 200, moved.text
        revision = moved.json()["data"]["revision"]

        refused = _post(
            client, token, "task_action",
            {"action": "complete", "task_id": task["id"], "expected_revision": revision},
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "task_completion_requires_user"

        # Spelling it as a plain status edit is the same refusal, not a way in.
        edited = _post(
            client, token, "task_update",
            {"task_id": task["id"], "expected_revision": revision, "status": "done"},
        )
        assert edited.json()["error"]["code"] == "task_completion_requires_user"
        assert _post(client, token, "task_get", {"task_id": task["id"]}).json()["data"]["status"] == "in_progress"

        # With the user's reason, `complete` is the one route that closes it.
        done = _post(
            client, token, "task_action",
            {"action": "complete", "task_id": task["id"], "expected_revision": revision,
             "reason": "The user said it shipped."},
        )
        assert done.status_code == 200, done.text
        assert done.json()["data"]["status"] == "done"


def test_a_stale_task_revision_is_a_retryable_refusal_over_the_cli(tmp_path: Path) -> None:
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Plan the launch"}).json()["data"]
        first = _post(
            client, token, "task_update",
            {"task_id": created["id"], "expected_revision": created["revision"], "due": "2026-10-05"},
        )
        assert first.status_code == 200, first.text
        stale = _post(
            client, token, "task_update",
            {"task_id": created["id"], "expected_revision": created["revision"], "title": "stale"},
        )
        assert stale.status_code == 422
        error = stale.json()["error"]
        assert error["code"] == "task_revision_conflict"
        assert error["retryable"] is True
        assert _post(client, token, "task_get", {"task_id": created["id"]}).json()["data"]["due"] == "2026-10-05"


def test_a_task_in_another_workspace_is_not_reachable_from_this_chat(tmp_path: Path) -> None:
    service = _task_service(tmp_path)
    personal, work = _token(service), _token(service, "chat-w", workspace="work")
    with _client(service) as client:
        mine = _post(client, personal, "task_create", {"title": "Personal"}).json()["data"]
        theirs = _post(client, work, "task_create", {"title": "Work"}).json()["data"]
        assert [row["id"] for row in _post(client, personal, "task_list").json()["data"]] == [mine["id"]]
        assert [row["id"] for row in _post(client, work, "task_list").json()["data"]] == [theirs["id"]]
        cross = _post(client, work, "task_get", {"task_id": mine["id"]})
    assert cross.status_code == 422
    assert cross.json()["error"]["code"] == "task_not_found"


def test_plan_mode_gates_every_task_write(tmp_path: Path) -> None:
    """The gate the annotations claim, checked on the operations it applies to."""
    service = _task_service(tmp_path, mode="plan")
    token = _token(service)
    with _client(service) as client:
        listed = _post(client, token, "task_list", {})
        filed = _post(client, token, "task_create", {"title": "Not in plan mode"})
        moved = _post(
            client, token, "task_action",
            {"action": "move", "task_id": "a" * 32, "status": "in_progress", "expected_revision": "r"},
        )
    assert listed.status_code == 200
    for response in (filed, moved):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "plan_mode_read_only"


def test_task_delete_cli_mapping(tmp_path: Path) -> None:
    """One task id, the revision read with it and the user's reason; nothing that widens the blast radius."""
    parser = agent_cli.build_parser()
    reason = tmp_path / "why.txt"
    reason.write_text("I never use it.", encoding="utf-8")
    assert agent_cli.resolve(
        parser.parse_args(
            ["task", "delete", "a" * 32, "--revision", "rev1", "--reason-file", str(reason)]
        )
    ) == (
        "task_delete",
        {"task_id": "a" * 32, "expected_revision": "rev1", "reason": "I never use it."},
    )

    for argv in (
        ["task", "delete", "a" * 32, "--reason-file", str(reason)],
        ["task", "delete", "--revision", "rev1", "--reason-file", str(reason)],
        # The reason is required on the agent surface.
        ["task", "delete", "a" * 32, "--revision", "rev1"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(argv)
        assert excinfo.value.code == 2
    for flag in ("--force", "--workspace", "--path"):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(
                ["task", "delete", "a" * 32, "--revision", "rev1",
                 "--reason-file", str(reason), flag, "x"]
            )
        assert excinfo.value.code == 2


def test_task_complete_cli_requires_a_reason(tmp_path: Path) -> None:
    parser = agent_cli.build_parser()
    reason = tmp_path / "why.txt"
    reason.write_text("The user said it shipped.", encoding="utf-8")
    assert agent_cli.resolve(
        parser.parse_args(
            ["task", "complete", "a" * 32, "--revision", "rev1", "--reason-file", str(reason)]
        )
    ) == (
        "task_action",
        {
            "action": "complete",
            "task_id": "a" * 32,
            "expected_revision": "rev1",
            "reason": "The user said it shipped.",
        },
    )
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["task", "complete", "a" * 32, "--revision", "rev1"])
    assert excinfo.value.code == 2


def test_task_delete_is_destructive_and_mutating(tmp_path: Path) -> None:
    from ciao import mcp_server

    service = _task_service(tmp_path)
    assert "task_delete" in service.operation_table
    assert (
        mcp_server.OPERATIONS_BY_NAME["task_delete"].annotations
        == mcp_server._DESTRUCTIVE
    )

    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Remove me"}).json()["data"]
        response = _post(
            client,
            token,
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "data": {"id": created["id"], "deleted": True},
        }
        gone = _post(client, token, "task_get", {"task_id": created["id"]})
    assert gone.status_code == 422
    assert gone.json()["error"]["code"] == "task_not_found"


@pytest.mark.parametrize("reason", [None, "", "  \n"])
def test_task_delete_without_a_reason_is_refused_and_keeps_the_task(
    tmp_path: Path, reason: str | None
) -> None:
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Keep me"}).json()["data"]
        path = (
            service.control_plane.config.workspace_vault_root("personal")
            / "Workspace" / "Tasks" / f"{created['id']}.md"
        )
        arguments: dict[str, Any] = {
            "task_id": created["id"], "expected_revision": created["revision"],
        }
        if reason is not None:
            arguments["reason"] = reason
        refused = _post(client, token, "task_delete", arguments)
        assert refused.status_code in (400, 422), refused.text
        assert refused.json()["ok"] is False
        assert path.is_file()


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_task_complete_without_a_reason_is_refused_and_leaves_the_task_open(
    tmp_path: Path, reason: str | None
) -> None:
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Still open"}).json()["data"]
        arguments: dict[str, Any] = {
            "action": "complete", "task_id": created["id"],
            "expected_revision": created["revision"],
        }
        if reason is not None:
            arguments["reason"] = reason
        refused = _post(client, token, "task_action", arguments)
        assert refused.status_code in (400, 422), refused.text
        assert refused.json()["ok"] is False
        got = _post(client, token, "task_get", {"task_id": created["id"]}).json()["data"]
        assert got["status"] == "backlog"


def test_an_empty_reason_file_reaches_the_refusal_instead_of_dropping_the_key(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    parser = agent_cli.build_parser()
    for verb in ("delete", "complete"):
        _name, arguments = agent_cli.resolve(
            parser.parse_args(
                ["task", verb, "a" * 32, "--revision", "rev1", "--reason-file", str(empty)]
            )
        )
        assert arguments["reason"] == ""


def test_an_unattended_turn_cannot_delete_task(tmp_path: Path) -> None:
    """A scheduled turn may not permanently remove a task; the current turn decides.

    Only an attended request can authorize deleting the user's record. Once the
    same chat's current turn is attended again, deletion is available.
    """
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Not from a schedule"}).json()["data"]
        path = (
            service.control_plane.config.workspace_vault_root("personal")
            / "Workspace"
            / "Tasks"
            / f"{created['id']}.md"
        )
        before = path.read_bytes()
        service.control_plane.pcm.chat.user_turn_count = 2
        service.control_plane.pcm.chat.user_turn_unattended = {"1": True}

        refused = _post(
            client,
            token,
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "unattended_forbidden"
        assert path.read_bytes() == before

        # Turn 1 was the unattended one; the current turn is attended again.
        service.control_plane.pcm.chat.user_turn_unattended = {}
        allowed = _post(
            client,
            token,
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["data"] == {"id": created["id"], "deleted": True}
        assert not path.exists()


def test_plan_mode_cannot_delete_task(tmp_path: Path) -> None:
    """The normal read-only refusal, plus the token and scope refusals, write nothing."""
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        created = _post(client, token, "task_create", {"title": "Still here"}).json()["data"]
        path = (
            service.control_plane.config.workspace_vault_root("personal")
            / "Workspace"
            / "Tasks"
            / f"{created['id']}.md"
        )
        before = path.read_bytes()
        service.control_plane.pcm.chat.mode = "plan"

        denied = _post(
            client,
            token,
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == "plan_mode_read_only"
        assert path.read_bytes() == before

        unauthenticated = _post(
            client,
            "",
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "unauthorized"

        # Leave plan mode so the next refusal is the scope, not the gate.
        service.control_plane.pcm.chat.mode = "auto"
        other_workspace = _token(service, "chat-w", workspace="work")
        cross = _post(
            client,
            other_workspace,
            "task_delete",
            {"task_id": created["id"], "expected_revision": created["revision"], "reason": "user asked"},
        )
        assert cross.status_code == 422
        assert cross.json()["error"]["code"] == "task_not_found"
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["context", "get"], ("context_get", {})),
        (["context", "logs"], ("context_logs", {"lines": 200})),
        (["context", "logs", "--lines", "50"], ("context_logs", {"lines": 50})),
        (["memory", "status"], ("memory_status", {})),
        (
            ["memory", "update", "--region", "profile", "--action", "replace", "--match", "old", "--entry", "new"],
            ("memory_update", {"region": "profile", "action": "replace", "entry": "new", "match": "old"}),
        ),
        (["vault", "search", "who is Sofia", "--limit", "3"], ("vault_search", {"query": "who is Sofia", "limit": 3})),
        (["vault", "review", "list"], ("vault_review", {"action": "list"})),
        (["vault", "review", "keep", "--candidate", "c1"], ("vault_review", {"action": "decide", "candidate_id": "c1", "disposition": "keep"})),
        (["vault", "review", "complete", "--candidate", "c1"], ("vault_review", {"action": "complete", "candidate_id": "c1"})),
        # The verb is hyphenated on the command line and snake_cased on the wire.
        (["vault", "review", "restore-completed", "--candidate", "c1"], ("vault_review", {"action": "restore_completed", "candidate_id": "c1"})),
        (["file", "surface", "out/report.md"], ("file_surface", {"path": "out/report.md"})),
        # The payload crosses as a path; the verdict's prose never does.
        (["note", "related", "People/Mo.md"], ("note_related", {"path": "People/Mo.md", "limit": 15})),
        (["note", "verify", "--payload-file", "out/verify.json"], ("verify_note", {"payload_file": "out/verify.json"})),
        (
            ["chat", "list", "--project", "p1"],
            ("chats_list", {"project_id": "p1", "limit": 20, "offset": 0, "compact": True}),
        ),
        (
            ["chat", "list", "--limit", "5", "--offset", "10", "--full"],
            ("chats_list", {"project_id": "", "limit": 5, "offset": 10, "compact": False}),
        ),
        (
            ["chat", "get", "--chat", "c2"],
            ("chat_get", {"chat_id": "c2", "messages": 10, "message_chars": 2000}),
        ),
        (["chat", "get", "--chat", "c2", "--full"], ("chat_get", {"chat_id": "c2", "messages": 0})),
        (["chat", "create", "--title", "New", "--prompt", "hi"], ("chat_create", {"title": "New", "prompt": "hi"})),
        (["chat", "send", "--chat", "c5", "--prompt", "run"], ("chat_send", {"chat_id": "c5", "prompt": "run"})),
        (["chat", "stop", "--chat", "c7"], ("chat_stop", {"chat_id": "c7"})),
        (["chat", "archive"], ("chat_archive", {"chat_id": ""})),
        (["chat", "delete", "--chat", "c9"], ("chat_delete", {"chat_id": "c9"})),
        (["schedule", "list"], ("schedules_list", {})),
        (
            ["schedule", "create", "--prompt", "digest", "--frequency", "weekly", "--days-of-week", "mon,tue", "--daily-time", "09:00", "--interval-minutes", "0"],
            ("schedule", {"action": "create", "prompt": "digest", "frequency": "weekly", "days_of_week": ["mon", "tue"], "daily_time": "09:00", "interval_minutes": 0}),
        ),
        (["schedule", "update", "s1", "--title", "T"], ("schedule", {"action": "update", "schedule_id": "s1", "title": "T"})),
        (["schedule", "preview", "--prompt", "draft", "--frequency", "weekly"], ("schedule", {"action": "preview", "prompt": "draft", "frequency": "weekly"})),
        (["schedule", "pause", "s1"], ("schedule_action", {"schedule_id": "s1", "action": "pause"})),
        (["schedule", "resume", "s1"], ("schedule_action", {"schedule_id": "s1", "action": "resume"})),
        (["schedule", "run", "s1"], ("schedule_action", {"schedule_id": "s1", "action": "run"})),
        (["schedule", "delete", "s1"], ("schedule_action", {"schedule_id": "s1", "action": "delete"})),
        (["webhook", "list"], ("webhook_list", {})),
        # The prose travels as a file; only `--name` is required.
        (["webhook", "create", "--name", "CI push"], ("webhook_create", {"name": "CI push"})),
        (
            ["webhook", "update", "a" * 32, "--revision", "3", "--name", "CI", "--disable"],
            ("webhook_update", {"trigger_id": "a" * 32, "expected_revision": "3", "name": "CI", "enabled": False}),
        ),
        (
            ["webhook", "update", "a" * 32, "--revision", "3", "--enable"],
            ("webhook_update", {"trigger_id": "a" * 32, "expected_revision": "3", "enabled": True}),
        ),
        (
            ["webhook", "rotate", "a" * 32, "--revision", "3"],
            ("webhook_rotate", {"trigger_id": "a" * 32, "expected_revision": "3"}),
        ),
        (
            ["webhook", "delete", "a" * 32, "--revision", "3"],
            ("webhook_delete", {"trigger_id": "a" * 32, "expected_revision": "3"}),
        ),
        (["chat", "continue", "--chat", "c3"], ("chat_continue", {"chat_id": "c3"})),
        (["chat", "retry"], ("chat_retry", {"chat_id": "", "action": "try_now", "prompt": ""})),
        (["chat", "retry", "--action", "set", "--kind", "network"], ("chat_retry", {"chat_id": "", "action": "set", "prompt": "", "kind": "network"})),
        (["chat", "update", "--model", "opus", "--thinking-level", "high"], ("chat_update", {"chat_id": "", "model": "opus", "thinking_level": "high"})),
        (["chat", "handover", "--provider", "opencode"], ("chat_handover", {"chat_id": "", "provider": "opencode", "model": ""})),
        (["chat", "reload"], ("chat_reload", {"chat_id": ""})),
        (["project", "list", "--include-completed"], ("projects_list", {"include_completed": True})),
        (["project", "get"], ("project_get", {"project_id": ""})),
        (["project", "create", "--name", "Q4", "--context", "ctx"], ("project", {"action": "create", "name": "Q4", "context": "ctx"})),
        (["project", "update", "p1", "--vault-folder", "Projects/Q4"], ("project", {"action": "update", "project_id": "p1", "vault_folder": "Projects/Q4"})),
        (["project", "create", "--name", "Q4", "--repo", ""], ("project", {"action": "create", "name": "Q4", "repo_path": ""})),
        (["project", "update", "p1", "--repo", ""], ("project", {"action": "update", "project_id": "p1", "repo_path": ""})),
        (["project", "restore", "q4-launch"], ("project", {"action": "restore", "stem": "q4-launch"})),
        (["project", "complete", "p1"], ("project_action", {"action": "complete", "project_id": "p1"})),
        (["task", "list"], ("task_list", {})),
        (
            ["task", "list", "--completed-since", "2026-10-08", "--completed-before", "2026-10-09T00:00:00Z"],
            ("task_list", {"completed_since": "2026-10-08", "completed_before": "2026-10-09T00:00:00Z"}),
        ),
        (["task", "get", "a1b2"], ("task_get", {"task_id": "a1b2"})),
        (["task", "create", "--title", "Ship the board"], ("task_create", {"title": "Ship the board"})),
        (
            ["task", "create", "--title", "Ship it", "--project", "p1", "--due", "2026-10-20"],
            ("task_create", {"title": "Ship it", "project_id": "p1", "due": "2026-10-20"}),
        ),
        (
            ["task", "update", "a1b2", "--revision", "rev1", "--title", "Ship the board"],
            ("task_update", {"task_id": "a1b2", "expected_revision": "rev1", "title": "Ship the board"}),
        ),
        (
            ["task", "update", "a1b2", "--revision", "rev1", "--status", "in_review"],
            ("task_update", {"task_id": "a1b2", "expected_revision": "rev1", "status": "in_review"}),
        ),
        (
            ["task", "move", "a1b2", "--to", "in_progress", "--revision", "rev1"],
            ("task_action", {"action": "move", "task_id": "a1b2", "status": "in_progress", "expected_revision": "rev1"}),
        ),
        (
            ["task", "link-chat", "a" * 32, "--revision", "r"],
            ("task_link_chat", {"task_id": "a" * 32, "expected_revision": "r", "remove": False}),
        ),
        (
            ["task", "link-chat", "a" * 32, "--revision", "r", "--chat", "c", "--remove"],
            ("task_link_chat", {"task_id": "a" * 32, "expected_revision": "r", "chat_id": "c", "remove": True}),
        ),
        (
            ["run", "start", "--label", "report", "--timeout-s", "900", "--env", "A=1", "--", "bash", "-lc", "a && b"],
            ("background_run_start", {"cmd": ["bash", "-lc", "a && b"], "env": {"A": "1"}, "timeout_s": 900, "label": "report"}),
        ),
        (["run", "status", "run-1", "--lines", "20"], ("background_run_status", {"run_id": "run-1", "lines": 20})),
        (["run", "cancel", "run-1"], ("background_run_cancel", {"run_id": "run-1"})),
        (["gws", "status"], ("gws_status", {})),
        (["workspace", "list"], ("workspaces_list", {})),
        (["repo", "candidates"], ("repo_candidates", {})),
    ],
)
def test_cli_arguments_map_to_operations(argv: list[str], expected: tuple[str, dict]) -> None:
    parser = agent_cli.build_parser()
    assert agent_cli.resolve(parser.parse_args(argv)) == expected


def test_chat_update_code_flag_maps_to_code_mode() -> None:
    parser = agent_cli.build_parser()
    assert agent_cli.resolve(parser.parse_args(["chat", "update", "--code", "on"])) == (
        "chat_update",
        {"chat_id": "", "code_mode": True},
    )
    assert agent_cli.resolve(parser.parse_args(["chat", "update", "--code", "off"])) == (
        "chat_update",
        {"chat_id": "", "code_mode": False},
    )
    with pytest.raises(SystemExit) as excinfo:
        agent_cli.main(["chat", "update", "--code", "maybe"])
    assert excinfo.value.code == 2


def test_plan_mode_refuses_a_code_mode_switch(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, mode="plan")
    token = _token(service)
    with _client(service) as client:
        response = _post(client, token, "chat_update", {"code_mode": True})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "plan_mode_read_only"


def test_cli_repo_flag_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    parser = agent_cli.build_parser()
    _op, arguments = agent_cli.resolve(parser.parse_args(["project", "update", "p1", "--repo", "."]))
    assert arguments["repo_path"] == os.path.abspath(str(tmp_path))
    assert agent_cli.is_agent_invocation(["repo", "candidates"])


def test_repo_candidates_is_a_read_operation() -> None:
    from ciao import mcp_server

    assert mcp_server.OPERATIONS_BY_NAME["repo_candidates"].annotations == mcp_server._READ


def test_repo_candidates_dispatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    (root / "app" / ".git").mkdir(parents=True)
    monkeypatch.setattr("ciao.repo_link.REPO_ROOTS", (str(root),))
    service, _plane, _pcm = _names_service(tmp_path)
    with _client(service) as client:
        res = _post(client, _token(service), "repo_candidates")
    body = res.json()
    assert body["ok"] is True
    assert [c["name"] for c in body["data"]["candidates"]] == ["app"]


def test_every_documented_command_parses(tmp_path: Path) -> None:
    """The skill's telemetry table is the contract: each row must be a real command."""
    table = json.loads((Path(agent_cli._SKILL_PATH).parent / "commands.json").read_text(encoding="utf-8"))
    parser = agent_cli.build_parser()
    # `task report` reads its summary at parse time, so it needs a real file
    # rather than the placeholder names the flag-only rows get.
    summary = tmp_path / "summary.md"
    summary.write_text("did the thing\n", encoding="utf-8")
    # `chat terminal-offer` reads its command at parse time too.
    command = tmp_path / "command.txt"
    command.write_text("ls -la\n", encoding="utf-8")
    required = {
        "memory update": ["--region", "memory", "--action", "add"],
        "vault search": ["q"], "vault review show": ["p"],
        "vault review keep": ["--candidate", "c"], "vault review trash": ["--candidate", "c"],
        "vault review restore": ["--candidate", "c"], "vault review delete": ["--candidate", "c", "--confirm", "c"],
        "vault review complete": ["--candidate", "c"], "vault review restore-completed": ["--candidate", "c"],
        "note related": ["People/Mo.md"], "note verify": ["--payload-file", "p.json"],
        "file surface": ["p"], "chat send": ["--chat", "c", "--prompt", "p"], "chat stop": ["--chat", "c"],
        "chat terminal-offer": ["--command-file", str(command)],
        "chat continue": ["--chat", "c"], "project create": ["--name", "n"], "project restore": ["s"],
        "project complete": ["p"], "project delete": ["p"], "task create": ["--title", "t"],
        "task get": ["a" * 32], "task update": ["a" * 32, "--revision", "r"],
        "task delete": ["a" * 32, "--revision", "r", "--reason-file", str(summary)],
        "task move": ["a" * 32, "--to", "in_progress", "--revision", "r"],
        "task complete": ["a" * 32, "--revision", "r", "--reason-file", str(summary)],
        "task delegate": ["a" * 32, "--revision", "r"],
        "task add-update": ["a" * 32, "--revision", "r", "--text-file", str(summary)],
        "task link-chat": ["a" * 32, "--revision", "r"],
        "task report": ["a" * 32, "--outcome", "done", "--summary-file", str(summary)],
        "task attempt": ["a" * 32, "stop"],
        "schedule update": ["s"], "schedule pause": ["s"],
        "schedule resume": ["s"], "schedule run": ["s"], "schedule delete": ["s"],
        "webhook create": ["--name", "n"], "webhook update": ["a" * 32, "--revision", "1"],
        "webhook rotate": ["a" * 32, "--revision", "1"],
        "webhook delete": ["a" * 32, "--revision", "1"],
        "run start": ["--", "true"], "run status": ["r"], "run cancel": ["r"],
    }
    for command, operation in table.items():
        argv = command.split() + required.get(command, [])
        assert agent_cli.is_agent_invocation(argv), command
        op, _arguments = agent_cli.resolve(parser.parse_args(argv))
        assert op == operation, command


def test_every_agent_command_is_in_the_telemetry_table() -> None:
    """The other direction: a command the parser offers is a command we name.

    `test_every_documented_command_parses` walks the table into the parser, so
    a verb the CLI dispatches but the table never listed passed it — and
    `task report` (#1064) was exactly that for the life of the operation. The
    cost is quiet and lands on the eval, not the CLI: `behavioral_eval`
    normalizes a `ciao <noun> <verb>` invocation to its operation *through this
    table*, by longest matching prefix, so an unlisted verb never becomes
    `task_report` and a probe whose agent reported a delegated task scored as a
    wrong tool. `ciao help` is the one deliberate exclusion: it is the long
    reference `AGENT_CLI.md` points at, not an operation.
    """

    def subparsers(parser: argparse.ArgumentParser) -> list[dict[str, argparse.ArgumentParser]]:
        return [
            action.choices
            for action in parser._actions
            if isinstance(getattr(action, "choices", None), dict) and action.dest != "help"
        ]

    def leaves(
        parser: argparse.ArgumentParser, prefix: list[str]
    ) -> list[str]:
        groups = subparsers(parser)
        if not groups:
            return [" ".join(prefix)]
        return [
            leaf for choices in groups for name, sub in choices.items()
            for leaf in leaves(sub, [*prefix, name])
        ]

    table = json.loads((Path(agent_cli._SKILL_PATH).parent / "commands.json").read_text(encoding="utf-8"))
    unlisted = sorted(set(leaves(agent_cli.build_parser(), [])) - set(table) - {"help"})
    assert unlisted == [], f"agent commands missing from ciao-cli/commands.json: {unlisted}"


def test_shared_nouns_route_only_their_agent_verbs() -> None:
    assert agent_cli.is_agent_invocation(["run", "start", "--", "true"])
    assert agent_cli.is_agent_invocation(["run", "status", "r1"])
    assert agent_cli.is_agent_invocation(["gws", "status"])
    # `ciao run` is the operator's server launcher; `ciao gws <profile> …` the passthrough.
    assert not agent_cli.is_agent_invocation(["run"])
    assert not agent_cli.is_agent_invocation(["run", "--port", "8443"])
    assert not agent_cli.is_agent_invocation(["gws", "status", "gmail"])
    assert not agent_cli.is_agent_invocation(["gws", "work", "gmail", "list"])
    assert not agent_cli.is_agent_invocation([])


def test_cli_usage_errors_exit_2_before_any_request(capsys: pytest.CaptureFixture[str]) -> None:
    # A noun without its verb is argparse's own usage error, not a misleading hint.
    with pytest.raises(SystemExit) as excinfo:
        agent_cli.main(["vault", "review"])
    assert excinfo.value.code == 2
    assert "<action>" in capsys.readouterr().err
    assert agent_cli.main(["run", "start", "--label", "x"]) == 2
    assert "needs a command" in capsys.readouterr().err
    assert agent_cli.main(["run", "start", "--env", "NOEQUALS", "--", "true"]) == 2
    assert "K=V" in capsys.readouterr().err
    assert agent_cli.main(["chat", "handover", "--messages", "{not json"]) == 2
    assert "--messages" in capsys.readouterr().err


def test_cli_json_flag_is_accepted_anywhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    # Parsing succeeds (the run reaches the session check) with --json after the verb.
    assert agent_cli.main(["vault", "search", "x", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_agent_session"


def test_cli_rejects_bad_enums_before_any_request(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        agent_cli.main(["memory", "update", "--region", "memory", "--action", "append", "--entry", "x"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'append'" in capsys.readouterr().err


def test_cli_without_a_session_prints_an_envelope_and_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(AGENT_URL_ENV, raising=False)
    assert agent_cli.main(["vault", "search", "x"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == "no_agent_session"


def test_cli_posts_the_operation_with_the_bearer_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict = {}

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return _Response(json.dumps({"ok": True, "data": [{"path": "People/Sofia.md"}]}).encode())

    monkeypatch.setattr(agent_cli, "_LOCAL_OPENER", SimpleNamespace(open=fake_urlopen))
    monkeypatch.setenv(AGENT_TOKEN_ENV, "tok-1")
    monkeypatch.setenv(AGENT_URL_ENV, "http://127.0.0.1:8443/agent/v1/")
    assert agent_cli.main(["vault", "search", "Sofia", "--limit", "2"]) == 0
    assert seen == {
        "url": "http://127.0.0.1:8443/agent/v1/vault_search",
        "auth": "Bearer tok-1",
        "body": {"query": "Sofia", "limit": 2},
    }
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_cli_help_prints_the_skill_document(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("---\nname: ciao-cli")
    assert "vault search" in out


def test_cli_surface_prompt_variant_names_ciao_commands() -> None:
    # S5 collapsed the prompt variants: there is one `system_prompt.md` and it
    # is the CLI wording for every surface.
    append = system_prompt_payload("")["append"]
    assert "ciao help" in append and "ciao memory update" in append and "ciao vault search" in append
    for name in ("memory_update", "vault_search", "vault_expand", "file_surface", "background_run_start", "MCP tools", "CLAUDE.md`, with"):
        assert name not in append, name
    assert "`AGENTS.md`" in append
    assert "ciao chat reload" in append
    # `/mcp` appears only inside the prohibition, never as an instruction.
    assert append.count("/mcp") == append.count("Never tell the user to run `/mcp`") == 1


def test_cli_surface_prompt_carries_a_compact_synopsis() -> None:
    """The prompt keeps a compact synopsis; the full reference is ciao-cli (#1464).

    An eval showed routing accuracy fell (0.823 -> 0.726, recall answered
    without `ciao vault search`) once the synopsis left the prompt, so the
    common commands, one line each with key flags, stay in
    `<ciaobot_cli_reference>`. `tests/test_cli_docs_drift.py` checks them
    against the parsers; every command must also be in the reference `ciao help`
    prints. The cap stays at 12000, unchanged: measured as the payload length
    the prompt is 11995 with the synopsis (#1599), so no raise was needed. A
    raise should move a section out rather than buy more room.
    """
    cli = system_prompt_payload("")["append"]
    reference = (Path(agent_cli._SKILL_PATH)).read_text(encoding="utf-8")
    missing = [command for command in _commands() if command not in reference]
    assert missing == []
    assert "ciao help" in cli and "ciao <noun> --help" in cli
    assert "vault search       QUERY" in cli and "task report" in cli
    assert "vault review restore-completed" not in cli
    assert len(cli) < 12000


def test_ciao_entrypoint_routes_agent_nouns_before_the_operator_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao import cli

    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    assert cli.main(["memory", "status"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_agent_session"


def test_ciao_cli_skill_is_not_synced_into_workspaces_yet(tmp_path: Path) -> None:
    from ciao.sync_skills import TRANSITIONAL_SKILLS, _install_stock_skills

    assert "ciao-cli" in TRANSITIONAL_SKILLS
    _install_stock_skills(tmp_path, gws_profile="")
    installed = {p.name for p in (tmp_path / ".claude" / "skills").iterdir()}
    assert "ciao-cli" not in installed
    assert "ciao-capabilities" in installed


def test_dispatch_requires_the_ciaobot_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.auth.provider import AccessToken

    service, _ = _service(tmp_path)
    token = _token(service)

    async def scopeless(_token: str):
        return AccessToken(token=_token, client_id="x", scopes=["other"], expires_at=None, claims={"token_id": "t", "chat_id": "chat-1", "workspace": "personal"})

    monkeypatch.setattr(service.registry, "verify_token", scopeless)
    with _client(service) as client:
        response = _post(client, token, "context_get")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_provider_reuse_key_is_the_agent_token() -> None:
    from ciao.models import agent_control_token, provider_reuse_key

    with_token = SimpleNamespace(extra_env={AGENT_TOKEN_ENV: "tok"})
    without = SimpleNamespace(extra_env={})
    assert agent_control_token(with_token) == provider_reuse_key(with_token) == "tok"
    assert provider_reuse_key(without) == ""


def test_json_flag_is_not_stripped_from_the_run_command_payload() -> None:
    from ciao.agent_cli import _strip_json_flag

    assert _strip_json_flag(["--json", "vault", "search", "x", "--json"]) == ["vault", "search", "x"]
    payload = ["run", "start", "--json", "--", "python", "report.py", "--json"]
    assert _strip_json_flag(payload) == ["run", "start", "--", "python", "report.py", "--json"]
    parser = agent_cli.build_parser()
    op, arguments = agent_cli.resolve(parser.parse_args(_strip_json_flag(payload)))
    assert op == "background_run_start"
    assert arguments["cmd"] == ["python", "report.py", "--json"]


def test_leading_json_flag_still_routes_to_the_agent_parser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ciao import cli

    monkeypatch.delenv(AGENT_TOKEN_ENV, raising=False)
    assert agent_cli.is_agent_invocation(["--json", "vault", "search", "x"])
    assert cli.main(["--json", "vault", "search", "x"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_agent_session"


def test_route_rejects_before_reading_an_unauthenticated_or_oversized_body(tmp_path: Path) -> None:
    from ciao.web import routes_agent

    service, _ = _service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        anonymous = client.post("/agent/v1/context_get", content=b"x" * 64)
        assert anonymous.status_code == 401
        huge = client.post(
            "/agent/v1/vault_search",
            headers={"Authorization": f"Bearer {token}"},
            content=b'{"query": "' + b"a" * (routes_agent.MAX_BODY_BYTES + 10) + b'"}',
        )
        assert huge.status_code == 413
        assert huge.json()["error"]["code"] == "payload_too_large"
        ok = _post(client, token, "context_get")
        assert ok.status_code == 200


# ── D-11: names and titles resolve like ids, inside the workspace ──────────
#
# The surface documents ids, but an agent that has just read a chat or project
# list back reaches for the name it saw. On the CLI surface that was the most
# common argument mistake of the S0.5 sessions, so the shared operation code
# (the control plane, which both surfaces run) resolves a name or title the
# same way `chat_create` already resolved a project name — and never outside
# the principal's own workspace.


class _NamesPcm:
    """Projects and chats in two workspaces, with the fields recall needs."""

    def __init__(self) -> None:
        self.projects = {
            "project-1": SimpleNamespace(project_id="project-1", name="Ciaobot Improvements", workspace="personal"),
            "project-2": SimpleNamespace(project_id="project-2", name="Research", workspace="personal"),
            "project-w": SimpleNamespace(project_id="project-w", name="Research", workspace="work"),
        }
        self.chats = {
            "chat-1": self._chat("chat-1", "project-1", "Morning briefing"),
            "chat-2": self._chat("chat-2", "project-2", "Weekly digest"),
            "chat-3": self._chat("chat-3", "project-1", "Duplicate"),
            "chat-4": self._chat("chat-4", "project-2", "Duplicate"),
            "chat-5": self._chat("chat-5", "project-1", "Weekly digest", archived=True),
            "chat-w": self._chat("chat-w", "project-w", "Work only"),
        }
        self.updated: list[tuple[str, dict]] = []
        self.created: list[dict] = []

    def create_project(self, name, workspace, context="", *, scaffold=False, repo_path=""):
        self.created.append(
            {"name": name, "workspace": workspace, "context": context, "scaffold": scaffold, "repo_path": repo_path}
        )
        return SimpleNamespace(to_dict=lambda: {"name": name})

    @staticmethod
    def _chat(chat_id: str, project_id: str, title: str, *, archived: bool = False):
        chat = SimpleNamespace(
            chat_id=chat_id,
            project_id=project_id,
            title=title,
            archived=archived,
            mode="auto",
            last_response="",
            last_response_status="",
        )
        chat.to_dict = lambda local=True, _c=chat: {"chat_id": _c.chat_id, "title": _c.title}
        return chat

    def get_project(self, project_id: str):
        return self.projects.get(project_id)

    def list_projects(self, workspace: str | None = None):
        return [p for p in self.projects.values() if workspace is None or p.workspace == workspace]

    def get_chat(self, chat_id: str):
        return self.chats.get(chat_id)

    def list_chats(self, project_id: str | None = None):
        return [c for c in self.chats.values() if project_id is None or c.project_id == project_id]

    def is_session_local(self, _chat) -> bool:
        return True

    def get_active_stream(self, _chat_id):
        return None

    def update_chat(self, chat_id: str, **changes):
        self.updated.append((chat_id, {k: v for k, v in changes.items() if v is not None}))
        return self.chats[chat_id]

    def chat_mode(self, _chat_id: str) -> str:
        return "auto"


def _names_service(tmp_path: Path):
    """The real control plane over `_NamesPcm`, bound to a dispatcher service."""
    from ciao.control_plane import CiaoControlPlane

    service, _fake = _service(tmp_path)
    pcm = _NamesPcm()
    plane = CiaoControlPlane(
        SimpleNamespace(workspace=lambda name: object() if name in {"personal", "work"} else None),
        project_chat_manager=pcm,
        schedule_manager=SimpleNamespace(),
    )
    service.bind(plane)
    return service, plane, pcm


def test_project_create_scaffolds_the_vault_note(tmp_path: Path) -> None:
    service, plane, pcm = _names_service(tmp_path)
    principal = SimpleNamespace(workspace="personal")
    plane.project_create(principal, "Q4", "ctx")
    assert pcm.created[-1]["scaffold"] is True


def test_project_op_forwards_repo_path(tmp_path: Path) -> None:
    service, _plane, pcm = _names_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        res = _post(client, token, "project", {"action": "create", "name": "Q4", "repo_path": "/x"})
    assert res.json()["ok"] is True
    assert pcm.created[-1]["repo_path"] == "/x"


def test_chats_list_accepts_a_project_name(tmp_path: Path) -> None:
    service, _plane, _pcm = _names_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        by_id = _post(client, token, "chats_list", {"project_id": "project-2"})
        by_name = _post(client, token, "chats_list", {"project_id": "rEsEaRcH"})
        unknown = _post(client, token, "chats_list", {"project_id": "nope"})
    assert by_id.json() == by_name.json()
    assert {row["chat_id"] for row in by_name.json()["data"]} == {"chat-2", "chat-4"}
    assert unknown.json()["error"]["code"] == "project_not_found"


def test_a_project_name_never_resolves_outside_the_workspace(tmp_path: Path) -> None:
    """Two workspaces own a "Research" project; each principal sees only its own."""
    service, _plane, _pcm = _names_service(tmp_path)
    with _client(service) as client:
        personal = _post(client, _token(service, "chat-1"), "chats_list", {"project_id": "Research"})
        work = _post(client, _token(service, "chat-w", workspace="work"), "chats_list", {"project_id": "Research"})
    assert {row["chat_id"] for row in personal.json()["data"]} == {"chat-2", "chat-4"}
    assert {row["chat_id"] for row in work.json()["data"]} == {"chat-w"}


def test_chat_arguments_accept_an_unambiguous_active_title(tmp_path: Path) -> None:
    service, _plane, _pcm = _names_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        by_title = _post(client, token, "chat_get", {"chat_id": "morning briefing"})
        # Two active chats share "Duplicate"; an archived chat shares the title
        # of an active one, and only the active one may win.
        ambiguous = _post(client, token, "chat_get", {"chat_id": "Duplicate"})
        archived_twin = _post(client, token, "chat_get", {"chat_id": "Weekly digest"})
        unknown = _post(client, token, "chat_get", {"chat_id": "no such chat"})
    assert by_title.json()["data"]["chat_id"] == "chat-1"
    assert ambiguous.json()["error"]["code"] == "chat_not_found"
    assert archived_twin.json()["data"]["chat_id"] == "chat-2"
    assert unknown.json()["error"]["code"] == "chat_not_found"


def test_a_chat_title_never_resolves_outside_the_workspace(tmp_path: Path) -> None:
    service, _plane, _pcm = _names_service(tmp_path)
    with _client(service) as client:
        response = _post(client, _token(service), "chat_get", {"chat_id": "Work only"})
    assert response.json()["error"]["code"] == "chat_not_found"


def test_chat_update_resolves_both_the_chat_title_and_the_project_name(tmp_path: Path) -> None:
    service, _plane, pcm = _names_service(tmp_path)
    with _client(service) as client:
        response = _post(
            client,
            _token(service),
            "chat_update",
            {"chat_id": "Morning briefing", "project_id": "research", "title": "Renamed"},
        )
    assert response.status_code == 200
    # The stored change carries the resolved id, not the name the agent typed.
    assert pcm.updated == [("chat-1", {"title": "Renamed", "project_id": "project-2"})]


def test_chat_archive_archives_the_chat_a_title_resolved_to(tmp_path: Path) -> None:
    """`chat_archive` echoes and archives an id, so a title must be resolved first."""
    from ciao.control_plane import McpPrincipal

    _service_, plane, pcm = _names_service(tmp_path)
    archived: list[str] = []

    async def _archive(chat_id: str):
        archived.append(chat_id)
        return SimpleNamespace(path=Path("/tmp/chat.md"))

    pcm.archive_chat = _archive
    pcm.run_archive_postprocess = lambda *args: None
    principal = McpPrincipal(
        token_id="t", chat_id="chat-2", project_id="project-2", workspace="personal", provider="claude"
    )
    result = asyncio.run(plane.chat_archive(principal, "Morning briefing"))
    assert result["data"]["chat_id"] == "chat-1"
    assert archived == ["chat-1"]


# ── Approval policy: no argv auto-approval ────────────────────────────────
# Since S6 every Ciaobot operation runs as `ciao <noun> <verb>` and no `ciao …`
# prefix is pre-approved (an allow prefix is a shell-suffix bypass risk). Auto
# mode keeps a card on every shell command; users who want no cards switch to
# `bypass`. These tests assert there is no argv allow-list to regress into.


def _commands() -> dict[str, str]:
    return json.loads((Path(agent_cli._SKILL_PATH).parent / "commands.json").read_text(encoding="utf-8"))


def test_no_argv_allow_or_ask_patterns_remain() -> None:
    """The argv allow/ask machinery is gone; there is nothing to enumerate."""
    import ciao.execution_modes as em

    assert not hasattr(em, "AGENT_CLI_ALLOW_PATTERNS")
    assert not hasattr(em, "AGENT_CLI_ASK_PATTERNS")
    assert not hasattr(em, "agent_cli_allowed_tool_rules")
    assert not hasattr(em, "agent_cli_permission_rules")


@pytest.mark.asyncio
async def test_claude_cli_surface_does_not_pre_approve_ciao_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `ciao …` argv prefix is pre-approved (an allow prefix is a shell-suffix
    bypass risk); auto mode keeps a card on every shell command."""
    from ciao.models import AgentRequest
    from ciao.providers.claude import ClaudeProvider

    captured: dict = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    provider = ClaudeProvider(
        tmp_path,
        config=SimpleNamespace(memory_char_limit=2200, user_char_limit=1375, vault_root=tmp_path / "v"),
    )
    monkeypatch.setattr("ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)

    await provider._ensure_connected(
        AgentRequest(prompt="t", model="sonnet", mode="auto", provider="claude")
    )
    allowed = captured["options"].allowed_tools or []
    assert not [entry for entry in allowed if entry.startswith("Bash(ciao ")]
    assert not [entry for entry in allowed if entry.startswith("mcp__ciaobot__")]
    assert not captured["options"].mcp_servers  # no MCP server is attached


def test_opencode_cli_auto_mode_keeps_shell_ask_for_ciao_commands() -> None:
    """Auto mode emits one generic V2 `shell: ask` rule."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings("auto")
    shell = [rule for rule in rules if rule["action"] == "shell"]
    assert shell == [{"action": "shell", "resource": "*", "effect": "ask"}]
    assert rules[-1]["effect"] == "deny"


@pytest.mark.parametrize("mode", ["plan", "normal", "bypass"])
def test_opencode_cli_rules_are_auto_mode_only(mode: str) -> None:
    from ciao.providers.opencode import mode_settings

    rules = mode_settings(mode)[1]
    assert not any(rule["resource"].startswith("ciao ") for rule in rules)


# ── release-gate fixes (v0.18.0) ───────────────────────────────────────────
#
# The release review found real issues in the new surface; each fix below
# carries the regression that proves it, and each test fails on the code as
# it was before the fix.


def test_agent_route_rejects_non_loopback_peers_before_auth(tmp_path: Path) -> None:
    """`agent_url` is hardcoded to 127.0.0.1: the only legitimate caller is on
    this machine, so a token presented from anywhere else is refused before it
    is even checked — a leaked token must not become a remote control plane."""
    service, _ = _service(tmp_path)
    token = _token(service)
    remote = _client(service, peer=("203.0.0.1", 9))
    with remote:
        denied = _post(remote, token, "context_get")
        anonymous = _post(remote, "", "context_get")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert anonymous.status_code == 403


def _two_workspace_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real control plane over two sibling workspaces, post-reroot shape."""
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.control_plane import AgentPrincipal, CiaoControlPlane

    config = CiaoConfig(        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    # The seam the re-rooting migration flips: per-workspace directories.
    monkeypatch.setattr(CiaoConfig, "agent_root", lambda self, name: tmp_path / name)
    # `file_surface` records its intent through the manager for a chat-scoped
    # principal (#1118), so this fake declares the method rather than relying on
    # a production-side getattr shim that does not exist.
    pcm = SimpleNamespace(
        get_active_stream=lambda chat_id: None,
        surface_chat_file=lambda chat_id, path: None,
    )
    plane = CiaoControlPlane(
        config, project_chat_manager=pcm, schedule_manager=SimpleNamespace()
    )
    principal = AgentPrincipal(
        token_id="t", chat_id="c", project_id="p", workspace="personal", provider="claude"
    )
    return plane, principal


def test_file_surface_is_scoped_to_the_principal_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`file_surface` used to root at the install root, so a chat in one
    workspace could surface — and get filename suggestions from — a sibling
    workspace. Both the existence check and the suggestion walk stay inside
    the principal's own agent root now."""
    from ciao.control_plane import ControlPlaneError

    (tmp_path / "personal").mkdir()
    (tmp_path / "personal" / "notes.md").write_text("hi", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "secret-plan.md").write_text("x", encoding="utf-8")
    plane, principal = _two_workspace_plane(tmp_path, monkeypatch)

    assert plane.file_surface(principal, "notes.md")["ok"] is True

    # Served before the fix (the file exists under the old install root);
    # refused after, because it is outside the principal's root.
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.file_surface(principal, "work/secret-plan.md")
    assert excinfo.value.code == "file_not_found"
    # The message echoes the caller's own path; the suggestions must not name
    # anything outside the principal's workspace.
    suggestions = excinfo.value.payload().get("suggestions", [])
    assert not [s for s in suggestions if s.startswith("work")]

    # The miss path walks for near matches: before the fix that walk covered
    # the sibling workspace and disclosed its filenames.
    with pytest.raises(ControlPlaneError) as miss:
        plane.file_surface(principal, "secret-plan.md")
    assert miss.value.code == "file_not_found"
    leaked = miss.value.payload().get("suggestions", [])
    assert not [s for s in leaked if "secret-plan" in s and s.startswith("work")]


def test_project_lookup_hides_foreign_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign-but-existent project id used to read `workspace_forbidden`
    while a nonexistent one read `project_not_found` — an existence oracle
    over other workspaces' project ids. Both read as not-found now."""
    from ciao.control_plane import ControlPlaneError

    foreign = SimpleNamespace(project_id="w1", name="Work", workspace="work")
    pcm = SimpleNamespace(
        get_project=lambda pid: foreign if pid == "w1" else None,
        get_active_stream=lambda chat_id: None,
    )
    plane, principal = _two_workspace_plane(tmp_path, monkeypatch)
    plane.pcm = pcm

    with pytest.raises(ControlPlaneError) as excinfo:
        plane._project(principal, "w1")
    assert excinfo.value.code == "project_not_found"
    assert "forbidden" not in str(excinfo.value).lower()

    with pytest.raises(ControlPlaneError) as missing:
        plane._project(principal, "no-such-project")
    assert missing.value.code == "project_not_found"


def test_handover_messages_refuse_credential_paths(tmp_path: Path) -> None:
    """`--messages @file` needs only one generic `ciao …` approval, so without
    this a workspace `.env` could be embedded into a vault-persisted handover
    with no more consent than the command itself."""
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(agent_cli.UsageError, match="credential"):
        agent_cli._handover_messages(f"@{secret}")

    plain = tmp_path / "notes.json"
    plain.write_text('[{"role": "user"}]', encoding="utf-8")
    assert agent_cli._handover_messages(f"@{plain}") == [{"role": "user"}]


def test_task_body_refuses_nested_runtime_and_env_variant(tmp_path: Path) -> None:
    """`Path.match` only caught a direct child of `.runtime/`, so a deeper file
    or a `.env.*` variant could be read into a vault-persisted task body (#1746)."""
    nested = tmp_path / ".runtime" / "a" / "b" / "file.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("token", encoding="utf-8")
    with pytest.raises(agent_cli.UsageError, match="credential"):
        agent_cli._task_body(f"@{nested}")

    env_local = tmp_path / ".env.local"
    env_local.write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(agent_cli.UsageError, match="credential"):
        agent_cli._task_body(f"@{env_local}")


# ── Decision reasons (#1726) ─────────────────────────────────────────


def test_task_delete_reason_file_is_read_and_sent_as_reason(tmp_path: Path) -> None:
    reason = tmp_path / "why.txt"
    reason.write_text("dismissed: not mine\n", encoding="utf-8")
    parser = agent_cli.build_parser()
    name, arguments = agent_cli.resolve(
        parser.parse_args(
            ["task", "delete", "a" * 32, "--revision", "rev1", "--reason-file", str(reason)]
        )
    )
    assert name == "task_delete"
    assert arguments == {
        "task_id": "a" * 32,
        "expected_revision": "rev1",
        "reason": "dismissed: not mine\n",
    }


def test_task_attempt_reason_file_is_passed_for_detach(tmp_path: Path) -> None:
    reason = tmp_path / "why.txt"
    reason.write_text("not a task", encoding="utf-8")
    parser = agent_cli.build_parser()
    name, arguments = agent_cli.resolve(
        parser.parse_args(["task", "attempt", "b" * 32, "detach", "--reason-file", f"@{reason}"])
    )
    assert name == "task_attempt_action"
    assert arguments == {"attempt_id": "b" * 32, "action": "detach", "reason": "not a task"}


def test_reason_file_refuses_a_credential_adjacent_path(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=1", encoding="utf-8")
    parser = agent_cli.build_parser()
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli.resolve(
            parser.parse_args(
                ["task", "delete", "a" * 32, "--revision", "rev1", "--reason-file", str(secret)]
            )
        )
    assert "--reason-file" in str(excinfo.value)


def test_attempt_without_a_reason_file_sends_no_reason_key() -> None:
    parser = agent_cli.build_parser()
    _name, attempt = agent_cli.resolve(parser.parse_args(["task", "attempt", "b" * 32, "detach"]))
    assert "reason" not in attempt


def test_mcp_task_attempt_action_refuses_a_reason_on_non_detach(tmp_path: Path) -> None:
    service = _task_service(tmp_path)
    token = _token(service)
    with _client(service) as client:
        response = _post(
            client,
            token,
            "task_attempt_action",
            {"attempt_id": "b" * 32, "action": "stop", "reason": "x"},
        )
    # A refusal raised inside the op itself reaches the agent as a tool error.
    assert response.status_code == 400
    assert "reason is only recorded by detach" in response.json()["error"]["message"]


# ── The suggested terminal command (#1842) ─────────────────────────────────
#
# An offer is *guidance*: the command the assistant proposes so the user can
# read it, copy it and run it themselves. It is never executed, never typed
# into a shell and never sent to a host terminal, and the engine holds it in
# memory only (#1840).
#
# Two properties decide the shape of this surface. First, the offer belongs to
# the chat that asked for it: the operation has no chat argument at all, so
# there is no way to put a command on a screen the user did not ask for. The
# chat is resolved from the token's principal by its exact id — never by a
# title, because a title is a name the caller chose and an offer is not
# something a caller may aim at another chat.
#
# Second, the command is untrusted, model-authored text. It can carry a
# credential, so every refusal the agent *sees* is static and never quotes
# the text back. A malformed *type* is the schema's refusal, and the raw
# FastMCP error does carry the value that was submitted — what keeps it out
# of the engine's HTTP envelope, its log and its telemetry is the
# dispatcher's first-line sanitizer. A schema *constraint* on the text is
# deliberately not part of that schema, because a constraint would put a
# rejected command inside the validation error itself; the engine validates
# the text instead.


def _offer_config(tmp_path: Path):
    from ciao.config import CiaoConfig, WorkspaceConfig

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


def _offer_service(tmp_path: Path):
    """A real control plane over a real chat manager, plus a dispatcher.

    The manager is the real one on purpose: the offer's id, the "same text is
    not a change" rule and the published events are its behaviour, and a stub
    would only agree with itself about them.
    """
    from ciao.control_plane import CiaoControlPlane
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    config = _offer_config(tmp_path)
    runtime = tmp_path / ".runtime"
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    service, _fake = _service(tmp_path)
    service.bind(
        CiaoControlPlane(
            config, project_chat_manager=manager, schedule_manager=SimpleNamespace()
        )
    )
    return service, manager, config


def _offer_chat(manager, workspace: str = "personal"):
    project = manager.create_project(f"Offers {workspace}", workspace=workspace)
    chat = manager.create_chat(project.project_id, title="Offered")
    return project, chat


def _offer_token(service, chat, project, workspace: str = "personal") -> str:
    token, _principal = service.registry.issue(
        chat_id=chat.chat_id,
        project_id=project.project_id,
        workspace=workspace,
        provider="claude",
    )
    return token


def _telemetry(service) -> list[dict]:
    path = Path(service._telemetry_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _marker_command(marker: Path) -> str:
    """A command that would create *marker* if anything ever ran it.

    Portable by construction: it names the interpreter that is running this
    test, so it needs no shell, no ``touch`` and nothing installed, and it
    behaves the same on Windows and on a POSIX box. The quoting is the
    platform's own, so the marker path survives the round trip through a
    command line.

    The command is never executed here. It is text: the point is that it is
    a command whose *effect* would be visible if some code decided to run it.
    """
    script = "import pathlib,sys;pathlib.Path(sys.argv[1]).touch()"
    argv = [sys.executable, "-c", script, str(marker)]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(part) for part in argv)


def test_a_terminal_offer_is_a_read_with_no_caller_target() -> None:
    """The operation takes the command and nothing else.

    A chat argument would be a target: an offer is a command on a screen, and
    the screen is the chat that asked. `_READ` says what it is — guidance, not
    an execution — and the default is not mutating, so a plan-mode chat may
    still offer (the same classification `file surface` has).
    """
    from ciao import mcp_server

    operation = mcp_server.OPERATIONS_BY_NAME["chat_terminal_offer"]
    assert operation.annotations == mcp_server._READ
    assert set(inspect.signature(operation.fn).parameters) == {"service", "command"}


def test_a_terminal_offer_lands_in_the_calling_chat(tmp_path: Path) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": "npm install -g ciao"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["chat_id"] == chat.chat_id
    offer = body["data"]["terminal_offer"]
    assert offer["command"] == "npm install -g ciao"
    assert len(offer["id"]) == 32
    # The state the manager holds is the state the caller was told about.
    assert manager.get_chat(chat.chat_id).terminal_offer.id == offer["id"]


def test_a_terminal_offer_cannot_target_another_chat(tmp_path: Path) -> None:
    """A sibling chat id in the arguments changes nothing.

    The schema has no chat argument, so an extra one is ignored and the offer
    lands on the calling chat — the only chat the caller may offer to.
    """
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    sibling = manager.create_chat(project.project_id, title="Sibling")
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(
            client,
            token,
            "chat_terminal_offer",
            {"command": "ls -la", "chat_id": sibling.chat_id, "path": "/etc/passwd"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["chat_id"] == chat.chat_id
    assert manager.get_chat(sibling.chat_id).terminal_offer is None


def test_a_terminal_offer_refuses_an_archived_chat(tmp_path: Path) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    manager.get_chat(chat.chat_id).archived = True
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "chat_archived"


def test_a_terminal_offer_refuses_an_unattended_turn(tmp_path: Path) -> None:
    """A schedule must not put a command in front of a user who is not there."""
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    chat.user_turn_count = 1
    chat.user_turn_unattended = {"0": True}
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unattended_forbidden"
    assert manager.get_chat(chat.chat_id).terminal_offer is None


def test_a_terminal_offer_refuses_a_chat_in_another_workspace(tmp_path: Path) -> None:
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager, workspace="work")
    token = _offer_token(service, chat, project, workspace="personal")
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_not_found"


def test_a_terminal_offer_resolves_only_an_exact_chat_id(tmp_path: Path) -> None:
    """A title is a name the caller chose; an offer is not aimed by name.

    The principal's chat id is resolved exactly. `_chat_scope` would fall back
    to an unambiguous title, which is right for `chat get` and wrong here: the
    offer belongs to the chat that asked, so a principal whose id is a title
    finds no chat.
    """
    from ciao.control_plane import ControlPlaneError

    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    title = manager.get_chat(chat.chat_id).title
    principal = SimpleNamespace(
        token_id="t",
        chat_id=title,
        project_id=project.project_id,
        workspace="personal",
        provider="claude",
    )
    with pytest.raises(ControlPlaneError) as excinfo:
        service.control_plane.chat_terminal_offer(principal, "ls -la")
    assert excinfo.value.code == "chat_not_found"


def test_a_terminal_offer_needs_a_calling_chat(tmp_path: Path) -> None:
    from ciao.control_plane import ControlPlaneError

    service, manager, _config = _offer_service(tmp_path)
    principal = SimpleNamespace(
        token_id="t", chat_id="", project_id="", workspace="personal", provider="claude"
    )
    with pytest.raises(ControlPlaneError) as excinfo:
        service.control_plane.chat_terminal_offer(principal, "ls -la")
    assert excinfo.value.code == "chat_required"


def test_a_terminal_offer_is_allowed_in_plan_mode(tmp_path: Path) -> None:
    """Guidance is not mutation: a plan-mode chat may still suggest a command.

    The operation is a read, so the plan-mode gate does not fire — the same
    classification `file surface` has, and the same reason: the user reviews
    the text and runs it themselves.
    """
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    manager.get_chat(chat.chat_id).mode = "plan"
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
    assert response.status_code == 200
    assert response.json()["data"]["terminal_offer"]["command"] == "ls -la"


def test_an_invalid_command_is_refused_without_echoing_it(tmp_path: Path) -> None:
    """A refused command is not quoted back: it can carry a credential."""
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    sentinel = "s3cr3t-token-value"
    with _client(service) as client:
        response = _post(
            client,
            token,
            "chat_terminal_offer",
            {"command": f"curl -H 'Authorization: {sentinel}'\x00"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_terminal_command"
    assert sentinel not in json.dumps(body)
    assert manager.get_chat(chat.chat_id).terminal_offer is None
    # The telemetry names the operation and the reason, not the text.
    records = _telemetry(service)
    assert records[-1]["tool"] == "chat_terminal_offer"
    assert records[-1]["error_code"] == "invalid_terminal_command"
    assert sentinel not in json.dumps(records)


def test_a_successful_terminal_offer_leaves_no_command_in_the_telemetry(
    tmp_path: Path,
) -> None:
    """The command is absent from the telemetry on the *success* path too.

    The failure row is the easy one: a refused command never became state.
    This one is the real case — the offer was accepted, the chat carries the
    text, and the row the service wrote at the dispatcher boundary still must
    not quote it. A command can carry a credential, and
    `mcp_tool_calls.jsonl` is a file that gets copied into backups.

    The row is read from the file the service actually writes, after a real
    HTTP dispatch through `/agent/v1/{op}`.
    """
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    sentinel = "npm install -g ciao-SENTINEL-4f2a9c"
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": sentinel})
    assert response.status_code == 200
    # The user does get the text: the offer is the point.
    assert response.json()["data"]["terminal_offer"]["command"] == sentinel

    path = Path(service._telemetry_path)
    assert path.name == "mcp_tool_calls.jsonl"
    text = path.read_text(encoding="utf-8")
    assert sentinel not in text
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    row = records[-1]
    assert row["tool"] == "chat_terminal_offer"
    assert row["status"] == "ok"
    assert row["chat_id"] == chat.chat_id
    assert row["surface"] == "cli"
    # The row names the operation, the chat and the outcome — never the
    # argument, under any key.
    assert not [key for key in row if "command" in key.lower()]
    assert not [key for key in row if "result" in key.lower()]
    assert not [
        key
        for key in row
        if key in {"arguments", "args", "value", "input", "payload", "text", "offer"}
    ]


def test_a_wrong_type_command_never_echoes_the_value(tmp_path: Path) -> None:
    """A schema constraint would put the rejected text in pydantic's error.

    The argument is a plain string, so the only validation that ever sees the
    value is the engine's — and the answer it gives is static.
    """
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": 12345})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert "12345" not in json.dumps(body)
    assert manager.get_chat(chat.chat_id).terminal_offer is None


def test_the_same_command_offered_twice_keeps_the_offer_id(tmp_path: Path) -> None:
    """Re-offering the same text is not a change: the id stays and no event
    is published, so a dismissal the user is holding stays valid."""
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    published: list[dict] = []
    hub = manager._events
    original = hub.publish
    hub.publish = published.append  # type: ignore[method-assign]
    try:
        with _client(service) as client:
            first = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
            second = _post(client, token, "chat_terminal_offer", {"command": "ls -la"})
    finally:
        hub.publish = original  # type: ignore[method-assign]
    assert first.json()["data"]["terminal_offer"]["id"] == second.json()["data"]["terminal_offer"]["id"]
    offers = [event for event in published if event["type"] == "chat_terminal_offer_changed"]
    assert len(offers) == 1


def test_a_terminal_offer_never_touches_the_host_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is launched, unlocked, typed or read back.

    The offer is a suggestion the user runs themselves. The terminal surface
    — the host launcher, the session manager, the grant store — must not be
    reachable from it at all.

    The managed surface is not the only way to run a command, though. A
    stray ``subprocess.run(command, shell=True)``, an ``os.system`` or an
    ``asyncio.create_subprocess_shell`` would execute the suggestion and
    leave no trace in the managed modules at all. So the process entry
    points are trapped as well, and the suggested command is one that
    *would* leave a marker file behind if it ever ran. Nothing is executed
    and no shell has to be installed: the traps refuse before anything is
    spawned, and the marker is the independent check that nothing slipped
    past them.
    """
    import ciao.host_terminal as host_terminal
    import ciao.terminal_auth as terminal_auth
    import ciao.terminal_sessions as terminal_sessions

    reached: list[str] = []

    def _refuse(name: str):
        def _inner(*_args, **_kwargs):
            reached.append(name)
            raise AssertionError(f"{name} was reached from a terminal offer")

        return _inner

    monkeypatch.setattr(host_terminal, "launch", _refuse("host_terminal.launch"))
    monkeypatch.setattr(
        terminal_sessions.TerminalSessionManager, "create", _refuse("session.create")
    )
    monkeypatch.setattr(
        terminal_sessions.TerminalSessionManager,
        "write_input",
        _refuse("session.write_input"),
    )
    monkeypatch.setattr(
        terminal_auth.TerminalGrantStore, "issue", _refuse("grant.issue")
    )
    # The unmanaged ways to run a string.
    monkeypatch.setattr(subprocess, "run", _refuse("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", _refuse("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "call", _refuse("subprocess.call"))
    monkeypatch.setattr(subprocess, "check_call", _refuse("subprocess.check_call"))
    monkeypatch.setattr(subprocess, "check_output", _refuse("subprocess.check_output"))
    monkeypatch.setattr(subprocess, "getoutput", _refuse("subprocess.getoutput"))
    monkeypatch.setattr(os, "system", _refuse("os.system"))
    monkeypatch.setattr(os, "popen", _refuse("os.popen"))
    monkeypatch.setattr(
        asyncio, "create_subprocess_shell", _refuse("asyncio.create_subprocess_shell")
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _refuse("asyncio.create_subprocess_exec")
    )

    marker = tmp_path / "executed.marker"
    command = _marker_command(marker)
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    with _client(service) as client:
        response = _post(client, token, "chat_terminal_offer", {"command": command})
    assert not reached, f"a terminal offer reached {reached}"
    assert response.status_code == 200
    # The suggestion was stored for the user to read — and never run.
    assert response.json()["data"]["terminal_offer"]["command"] == command
    assert not marker.exists()


# ── The CLI surface of the suggested terminal command (#1842) ──────────────
#
# The command travels in a file, never as a shell argument: it is text the
# shell would mangle (`$()`, backticks, quotes) and it would sit in the
# process table. Every refusal is static — neither the path nor the text
# appears in it, because a command can carry a credential and an error string
# ends up in a log line.


def _command_file(tmp_path: Path, text: str, *, name: str = "command.txt") -> Path:
    path = tmp_path / name
    # `newline=""` keeps the on-disk bytes equal to the string: the default
    # translates every "\n" to `os.linesep`, so a fixture built to an exact
    # byte count would be longer on Windows and the bound would refuse it.
    path.write_text(text, encoding="utf-8", newline="")
    return path


def test_chat_terminal_offer_takes_only_a_command_file(tmp_path: Path) -> None:
    """One required flag, and no way to name a chat.

    The offer belongs to the chat that asked, so there is no `--chat`; and the
    command is text, so it travels in a file rather than inline.
    """
    parser = agent_cli.build_parser()
    path = _command_file(tmp_path, "npm install -g ciao")
    assert agent_cli.resolve(parser.parse_args(["chat", "terminal-offer", "--command-file", str(path)])) == (
        "chat_terminal_offer",
        {"command": "npm install -g ciao"},
    )
    for forbidden in ("--chat", "--command"):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["chat", "terminal-offer", forbidden, "x"])
        assert excinfo.value.code == 2
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["chat", "terminal-offer"])
    assert excinfo.value.code == 2


def test_the_command_file_is_read_literally(tmp_path: Path) -> None:
    """The text crosses byte for byte: no shell parsing, no interpolation.

    `$()`, backticks, quotes and a Unicode command are exactly what a shell
    would eat, and a tab or a line feed inside a multi-line command is
    ordinary.
    """
    parser = agent_cli.build_parser()
    command = "echo \"$HOME\" `whoami` 'quoted' — ünïcode\n\tindented"
    path = _command_file(tmp_path, command)
    _op, arguments = agent_cli.resolve(
        parser.parse_args(["chat", "terminal-offer", "--command-file", str(path)])
    )
    assert arguments == {"command": command}


def test_the_command_file_accepts_the_at_prefix(tmp_path: Path) -> None:
    parser = agent_cli.build_parser()
    path = _command_file(tmp_path, "ls -la")
    _op, arguments = agent_cli.resolve(
        parser.parse_args(["chat", "terminal-offer", "--command-file", f"@{path}"])
    )
    assert arguments == {"command": "ls -la"}


#: The static reasons the CLI answers a bad ``--command-file`` with. They are
#: a closed set on purpose: a refusal is a *reason*, never a quotation of the
#: path or of the text it refused. A command can carry a credential, and the
#: refusal ends up in a log line and in the agent's transcript.
_TERMINAL_OFFER_REFUSALS = {
    "unreadable": "--command-file cannot be read.",
    "credential": "--command-file refuses a credential-adjacent path.",
    "not_regular": "--command-file must be an ordinary file.",
    "oversize": "--command-file holds more text than a command may.",
    "encoding": "--command-file must be UTF-8 text.",
    "characters": "--command-file holds text a command may not contain.",
    "empty": "--command-file holds no command text.",
}


def _assert_static_refusal(
    excinfo: pytest.ExceptionInfo,
    reason: str,
    *,
    secrets: tuple[str, ...] = (),
) -> None:
    """The refusal is the static reason, and it quotes nothing.

    ``secrets`` are the strings that must not appear: the path that was
    refused, the text that was refused, a token inside it. The literal
    invisible characters are checked as themselves — a control character, a
    bidi override — because a word that stands for one proves nothing.
    """
    message = str(excinfo.value)
    assert message == reason
    # The exception carries the reason and nothing else.
    assert excinfo.value.args == (reason,)
    for secret in secrets:
        assert secret not in message
    for character in ("\x00", "\u202e", "\u202c", "\n", "\r"):
        assert character not in message


def test_an_unreadable_command_file_is_a_usage_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.txt"
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(missing))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["unreadable"], secrets=(str(missing),)
    )


def test_a_credential_adjacent_command_file_is_refused(tmp_path: Path) -> None:
    """A `.env` (or anything under `.runtime/`/`secrets/`) is a credential
    store, not a place to keep a command to suggest."""
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(secret))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(secret), "TOKEN")
    )

    nested = tmp_path / ".runtime" / "a" / "b" / "command.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("ls -la", encoding="utf-8")
    with pytest.raises(agent_cli.UsageError) as nested_info:
        agent_cli._terminal_offer_command(str(nested))
    _assert_static_refusal(
        nested_info,
        _TERMINAL_OFFER_REFUSALS["credential"],
        secrets=(str(nested),),
    )


def test_a_credential_adjacent_command_file_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential check comes before the file is read.

    A `.env` is refused as a credential store, not as a file that happens to
    be unreadable, and the refusal must not depend on the file's contents.
    The file must not be *read* at all: a check that came after the read
    would already have pulled the secret into the process. That the check
    also comes before the stat is pinned portably by the missing-`.env`
    case below — a stat-first ordering answers "cannot be read" there.
    """
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=x", encoding="utf-8")
    resolved = secret.resolve()
    opened = _record_path_calls(monkeypatch, "open", resolved)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(secret))
    assert not opened, "the credential file was opened"
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(secret), "TOKEN")
    )


def test_a_missing_credential_adjacent_path_is_still_a_credential_refusal(
    tmp_path: Path,
) -> None:
    """The refusal is about the *name*, not about the file being there.

    A `.env` that does not exist is refused as a credential store. If the
    check came after the stat or the open, this path would answer "cannot
    be read" instead — which is exactly how a stat-first or open-first
    ordering shows itself.
    """
    missing = tmp_path / ".env"
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(missing))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(missing),)
    )


def test_a_command_path_that_aliases_a_credential_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check is on the *resolved* path, not on the one that was typed.

    A path that looks harmless can still name a credential store: a symlink
    to a `.env`, or an alias that resolves there. The check follows the
    resolution, so the file the command would be read from is the file that
    was judged.

    A real symlink is used where the OS allows one, and a deterministic
    ``resolve`` spy covers the same property everywhere else, so the test
    never skips and never depends on a platform privilege.
    """
    protected = tmp_path / ".env"
    protected.write_text("TOKEN=1", encoding="utf-8")
    # The alias does not exist yet: a symlink has to be created at a free
    # name, and the refusal happens before anything is read anyway.
    safe = tmp_path / "offer.txt"
    try:
        os.symlink(protected, safe)
    except (OSError, NotImplementedError, AttributeError):
        pass
    else:
        with pytest.raises(agent_cli.UsageError) as excinfo:
            agent_cli._terminal_offer_command(str(safe))
        _assert_static_refusal(
            excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(safe),)
        )

    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == safe:
            return protected
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(safe))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(safe),)
    )


def test_an_aliased_credential_path_is_refused_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias resolves to the credential, and nothing is read.

    The check follows the resolution *and* comes first: the file the alias
    names is never opened, so a secret behind a harmless-looking name is
    never pulled into the process. The missing-`.env` case above is what
    pins the check ahead of the stat, which resolution itself may touch.
    """
    protected = tmp_path / ".env"
    protected.write_text("TOKEN=1", encoding="utf-8")
    target = protected.resolve()
    # Created at a free name so the real-symlink branch actually runs; the
    # deterministic resolve spy below covers platforms that refuse it.
    safe = tmp_path / "offer.txt"
    try:
        os.symlink(protected, safe)
    except (OSError, NotImplementedError, AttributeError):
        pass
    else:
        opened = _record_path_calls(monkeypatch, "open", target)
        with pytest.raises(agent_cli.UsageError) as excinfo:
            agent_cli._terminal_offer_command(str(safe))
        assert not opened, "the aliased credential was opened"
        _assert_static_refusal(
            excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(safe),)
        )

    # And deterministically, where the OS will not make a symlink.
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == safe:
            return target
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    opened = _record_path_calls(monkeypatch, "open", target)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(safe))
    assert not opened, "the aliased credential was opened"
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["credential"], secrets=(str(safe),)
    )


def test_an_invalid_utf8_command_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "command.txt"
    path.write_bytes(b"TOKEN-SENTINEL-\xff\xfe-secret")
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo,
        _TERMINAL_OFFER_REFUSALS["encoding"],
        secrets=(str(path), "TOKEN-SENTINEL", "\ufffd"),
    )


def test_an_oversize_command_file_is_refused(tmp_path: Path) -> None:
    from ciao.terminal_offer import TERMINAL_OFFER_MAX_BYTES

    path = tmp_path / "command.txt"
    path.write_text(
        "OVERSIZE-SENTINEL-" + "a" * TERMINAL_OFFER_MAX_BYTES, encoding="utf-8"
    )
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo,
        _TERMINAL_OFFER_REFUSALS["oversize"],
        secrets=(str(path), "OVERSIZE-SENTINEL"),
    )


def test_a_command_file_at_the_byte_bound_is_accepted(tmp_path: Path) -> None:
    """Exactly the bound is inside it, and one byte more is not.

    The bound is counted on the bytes read, before the text is trimmed, so a
    file that is exactly at the limit is accepted even when the whitespace
    around the command is what fills it. The limit is not off by one.
    """
    from ciao.terminal_offer import TERMINAL_OFFER_MAX_BYTES

    body = "npm install -g ciao"
    filler = TERMINAL_OFFER_MAX_BYTES - len(body)
    raw = " " * (filler // 2) + body + "\n " * ((filler - filler // 2) // 2)
    raw += " " * (TERMINAL_OFFER_MAX_BYTES - len(raw.encode("utf-8")))
    assert len(raw.encode("utf-8")) == TERMINAL_OFFER_MAX_BYTES
    path = _command_file(tmp_path, raw)
    assert agent_cli._terminal_offer_command(str(path)) == body

    over = _command_file(tmp_path, raw + " ", name="over.txt")
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(over))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["oversize"], secrets=(str(over),)
    )


def test_a_command_file_with_an_invisible_character_is_refused(tmp_path: Path) -> None:
    """A control character or a bidi override is invisible on the card and
    changes what the user copies into their shell."""
    for text in ("ls -la\x00", "ls \u202e -la", "ls \u202c -la"):
        path = _command_file(tmp_path, text)
        with pytest.raises(agent_cli.UsageError) as excinfo:
            agent_cli._terminal_offer_command(str(path))
        _assert_static_refusal(
            excinfo,
            _TERMINAL_OFFER_REFUSALS["characters"],
            secrets=(str(path), text),
        )


def test_an_empty_command_file_is_refused(tmp_path: Path) -> None:
    path = _command_file(tmp_path, "   \n")
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["empty"], secrets=(str(path), "   \n")
    )


def test_the_command_file_is_read_with_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is bounded, exactly, and it is not `read_text`.

    An unbounded read is how a 2 GB file becomes a 2 GB argument on the way to
    the engine, and `read_text` decodes the whole thing before anything can
    refuse it. The bound is the command's own byte limit *plus the one byte
    that says there is more*: reading exactly that much is what lets an
    oversize file be refused without being read.
    """
    from pathlib import Path as _Path

    from ciao.terminal_offer import TERMINAL_OFFER_MAX_BYTES

    def _refuse(*_args, **_kwargs):
        raise AssertionError("the command file was read without a bound")

    real_open = _Path.open
    reads: list[int] = []

    class _Recording:
        """The real file, with every read size written down."""

        def __init__(self, handle) -> None:
            self._handle = handle

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            return self._handle.read(size)

        def fileno(self) -> int:
            return self._handle.fileno()

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *exc):
            return self._handle.__exit__(*exc)

    def _recording_open(self, *args, **kwargs):
        return _Recording(real_open(self, *args, **kwargs))

    path = _command_file(tmp_path, "ls -la")
    monkeypatch.setattr(_Path, "open", _recording_open)
    monkeypatch.setattr(_Path, "read_text", _refuse)
    monkeypatch.setattr(_Path, "read_bytes", _refuse)

    assert agent_cli._terminal_offer_command(str(path)) == "ls -la"
    # One read, of exactly the bound plus the byte that says "there is more".
    # An unbounded read (no size) or a larger one would show up here.
    assert reads == [TERMINAL_OFFER_MAX_BYTES + 1]


# ── A path that fails before it is a path (#1842, R1 security) ─────────────
#
# Three ways a `--command-file` can be a bad name rather than a bad file, and
# one way a file can be a bad *file*. `expanduser` raises `RuntimeError` for a
# `~user` that names nobody; `resolve`, `stat` and `open` raise `ValueError`
# for an embedded NUL. And a FIFO, a device or a socket answers an open with a
# read that never ends — the byte bound does not help, because the read
# blocks before it counts anything.


def _patch_path_operation(
    monkeypatch: pytest.MonkeyPatch, name: str, target: Path, replacement
) -> None:
    """Patch one ``Path`` method for one path only.

    pytest asks the filesystem about its own files while it renders a
    failure, so a patch that answers *every* path breaks the reporting
    rather than the code under test. The target is the path the operation
    would really be asked about.
    """
    real = getattr(Path, name)

    def _wrapper(self, *args, **kwargs):
        if self == target:
            return replacement(self, *args, **kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, _wrapper)


def _record_path_calls(
    monkeypatch: pytest.MonkeyPatch, name: str, target: Path
) -> list[str]:
    """Record every call to one ``Path`` method made about one path.

    The call is still made — the real method runs — so what is recorded is
    that the code *reached* the file, not that the file refused to open.
    Only the path under test is recorded: pytest asks the filesystem about
    its own files while it renders a failure.
    """
    real = getattr(Path, name)
    calls: list[str] = []

    def _wrapper(self, *args, **kwargs):
        if self == target:
            calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, _wrapper)
    return calls


def test_an_unknown_home_user_command_file_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A ``~user`` that names nobody is a path that cannot be resolved.

    The expansion fails with a `RuntimeError` on POSIX; on Windows the name
    is left alone and the file simply does not exist. Either way the answer
    is the same static refusal, and the name never comes back.
    """
    raw = "~no-such-user-1842/command.txt"
    assert agent_cli.main(["chat", "terminal-offer", "--command-file", raw]) == 2
    captured = capsys.readouterr()
    assert f"error: {_TERMINAL_OFFER_REFUSALS['unreadable']}" in captured.err
    assert raw not in captured.err
    assert "no-such-user-1842" not in captured.err
    assert captured.out == ""


def test_a_nul_in_the_command_file_path_is_refused(tmp_path: Path) -> None:
    """A NUL in the path is refused by the OS, not by us — with a reason.

    `resolve`, `stat` and `open` all raise `ValueError` for an embedded NUL.
    The refusal is the static one, and it quotes nothing: not the path, not
    the NUL.
    """
    raw = str(tmp_path / "command.txt") + "\x00"
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(raw)
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["unreadable"], secrets=(raw, "\x00")
    )


@pytest.mark.parametrize("operation", ["expanduser", "resolve", "stat", "open"])
@pytest.mark.parametrize("failure", [OSError, RuntimeError, ValueError])
def test_a_path_operation_failure_is_a_static_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, failure: type[Exception]
) -> None:
    """Whatever a path operation raises, the answer is the same reason.

    The three families are the ones a path can produce: an I/O error, the
    `RuntimeError` an unknown ``~user`` raises, and the `ValueError` an
    embedded NUL raises. None of them may escape as a traceback, and none of
    them may name the path.
    """
    path = _command_file(tmp_path, "ls -la")
    target = path if operation in {"expanduser", "resolve"} else path.resolve()

    def _fail(*_args, **_kwargs):
        raise failure("path operation failed")

    _patch_path_operation(monkeypatch, operation, target, _fail)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo,
        _TERMINAL_OFFER_REFUSALS["unreadable"],
        secrets=(str(path), "path operation failed"),
    )


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        ("fifo", stat.S_IFIFO | 0o600),
        ("character device", stat.S_IFCHR | 0o600),
        ("directory", stat.S_IFDIR | 0o755),
        ("socket", stat.S_IFSOCK | 0o600),
    ],
)
def test_a_non_regular_command_file_is_refused_without_being_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mode: int
) -> None:
    """A FIFO or a device answers an open with a read that never ends.

    The byte bound does not save it: the read blocks before it counts
    anything. So the mode is checked first and the file is never opened —
    which is the whole point, so the test asserts that no open happened.
    """
    path = _command_file(tmp_path, "ls -la")
    resolved = path.resolve()
    opened: list[str] = []

    def _mode(*_args, **_kwargs):
        return SimpleNamespace(st_mode=mode)

    def _open(self, *args, **kwargs):
        opened.append(str(self))
        raise AssertionError(f"a {kind} command file was opened")

    _patch_path_operation(monkeypatch, "stat", resolved, _mode)
    _patch_path_operation(monkeypatch, "open", resolved, _open)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    assert not opened, f"the {kind} was opened"
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["not_regular"], secrets=(str(path),)
    )


def test_a_command_file_that_cannot_be_inspected_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file the OS will not stat is a file we will not read."""
    path = _command_file(tmp_path, "ls -la")

    def _deny(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    _patch_path_operation(monkeypatch, "stat", path.resolve(), _deny)
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo,
        _TERMINAL_OFFER_REFUSALS["unreadable"],
        secrets=(str(path), "Permission denied"),
    )


def test_a_command_file_whose_handle_is_not_ordinary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handle has to be an ordinary file too.

    The mode is checked before the open and again on the handle that came
    back. The second check does not close the window between the two — it
    only refuses a handle that is not an ordinary file — so it is worth
    having and worth pinning.
    """
    path = _command_file(tmp_path, "ls -la")
    monkeypatch.setattr(
        os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFCHR | 0o600)
    )
    with pytest.raises(agent_cli.UsageError) as excinfo:
        agent_cli._terminal_offer_command(str(path))
    _assert_static_refusal(
        excinfo, _TERMINAL_OFFER_REFUSALS["not_regular"], secrets=(str(path),)
    )


def test_a_bad_command_file_exits_2_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A usage error is caught before the engine is contacted."""
    monkey = tmp_path / "missing.txt"
    assert agent_cli.main(["chat", "terminal-offer", "--command-file", str(monkey)]) == 2
    captured = capsys.readouterr()
    assert "--command-file" in captured.err
    assert captured.out == ""


def test_a_terminal_offer_crosses_from_the_file_to_the_chat(tmp_path: Path) -> None:
    """The whole path, once: a file, the CLI, the engine, the chat.

    The command is read from the file here, travels as one argument, and
    lands on the calling chat — and it lands nowhere else. The engine's own
    registry never sees it: a command can name a credential, and a registry
    file is the wrong place for that.
    """
    service, manager, _config = _offer_service(tmp_path)
    project, chat = _offer_chat(manager)
    token = _offer_token(service, chat, project)
    path = _command_file(tmp_path, "npm install -g ciao\n")
    parser = agent_cli.build_parser()
    operation, arguments = agent_cli.resolve(
        parser.parse_args(["chat", "terminal-offer", "--command-file", str(path)])
    )
    with _client(service) as client:
        response = _post(client, token, operation, arguments)
    assert response.status_code == 200
    assert response.json()["data"]["terminal_offer"]["command"] == "npm install -g ciao"
    registry = (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    assert "npm install -g ciao" not in registry
