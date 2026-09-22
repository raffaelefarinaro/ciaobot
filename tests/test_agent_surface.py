"""The agent CLI surface: dispatcher, route, CLI mapping and prompt variant."""
from __future__ import annotations

import asyncio
import io
import json
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


def _client(service) -> TestClient:
    app = Starlette(routes=[Route("/agent/v1/{op}", agent_dispatch_endpoint, methods=["POST"])])
    app.state.mcp_service = service
    return TestClient(app, base_url="http://127.0.0.1:18443")


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
        "chat_continue", "chat_retry", "chat_handover", "chat_archive",
        "chat_delete", "chat_stop",
    }
    service, _ = _service(tmp_path)
    assert chat_group <= set(service.operation_table)


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


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["context", "get"], ("context_get", {})),
        (["memory", "status"], ("memory_status", {})),
        (
            ["memory", "update", "--region", "profile", "--action", "replace", "--match", "old", "--entry", "new"],
            ("memory_update", {"region": "profile", "action": "replace", "entry": "new", "match": "old"}),
        ),
        (["vault", "search", "who is Sofia", "--limit", "3"], ("vault_search", {"query": "who is Sofia", "limit": 3})),
        (["vault", "review", "list"], ("vault_review", {"action": "list"})),
        (["vault", "review", "keep", "--candidate", "c1"], ("vault_review", {"action": "decide", "candidate_id": "c1", "disposition": "keep"})),
        (["file", "surface", "out/report.md"], ("file_surface", {"path": "out/report.md"})),
        (["chat", "list", "--project", "p1"], ("chats_list", {"project_id": "p1"})),
        (["chat", "get", "--chat", "c2"], ("chat_get", {"chat_id": "c2"})),
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
        (["chat", "continue", "--chat", "c3"], ("chat_continue", {"chat_id": "c3"})),
        (["chat", "retry"], ("chat_retry", {"chat_id": "", "action": "try_now", "prompt": ""})),
        (["chat", "update", "--model", "opus", "--thinking-level", "high"], ("chat_update", {"chat_id": "", "model": "opus", "thinking_level": "high"})),
        (["chat", "handover", "--provider", "opencode"], ("chat_handover", {"chat_id": "", "provider": "opencode", "model": ""})),
        (["project", "list", "--include-completed"], ("projects_list", {"include_completed": True})),
        (["project", "get"], ("project_get", {"project_id": ""})),
        (["project", "create", "--name", "Q4", "--context", "ctx"], ("project", {"action": "create", "name": "Q4", "context": "ctx"})),
        (["project", "update", "p1", "--vault-folder", "Projects/Q4"], ("project", {"action": "update", "project_id": "p1", "vault_folder": "Projects/Q4"})),
        (["project", "restore", "q4-launch"], ("project", {"action": "restore", "stem": "q4-launch"})),
        (["project", "complete", "p1"], ("project_action", {"action": "complete", "project_id": "p1"})),
        (
            ["run", "start", "--label", "report", "--timeout-s", "900", "--env", "A=1", "--", "bash", "-lc", "a && b"],
            ("background_run_start", {"cmd": ["bash", "-lc", "a && b"], "env": {"A": "1"}, "timeout_s": 900, "label": "report"}),
        ),
        (["run", "status", "run-1", "--lines", "20"], ("background_run_status", {"run_id": "run-1", "lines": 20})),
        (["run", "cancel", "run-1"], ("background_run_cancel", {"run_id": "run-1"})),
        (["gws", "status"], ("gws_status", {})),
        (["workspace", "list"], ("workspaces_list", {})),
    ],
)
def test_cli_arguments_map_to_operations(argv: list[str], expected: tuple[str, dict]) -> None:
    parser = agent_cli.build_parser()
    assert agent_cli.resolve(parser.parse_args(argv)) == expected


def test_every_documented_command_parses() -> None:
    """The skill's telemetry table is the contract: each row must be a real command."""
    table = json.loads((Path(agent_cli._SKILL_PATH).parent / "commands.json").read_text(encoding="utf-8"))
    parser = agent_cli.build_parser()
    required = {
        "memory update": ["--region", "memory", "--action", "add"],
        "vault search": ["q"], "vault review show": ["p"],
        "vault review keep": ["--candidate", "c"], "vault review trash": ["--candidate", "c"],
        "vault review restore": ["--candidate", "c"], "vault review delete": ["--candidate", "c", "--confirm", "c"],
        "file surface": ["p"], "chat send": ["--chat", "c", "--prompt", "p"], "chat stop": ["--chat", "c"],
        "chat continue": ["--chat", "c"], "project create": ["--name", "n"], "project restore": ["s"],
        "project complete": ["p"], "project delete": ["p"], "schedule update": ["s"], "schedule pause": ["s"],
        "schedule resume": ["s"], "schedule run": ["s"], "schedule delete": ["s"],
        "run start": ["--", "true"], "run status": ["r"], "run cancel": ["r"],
    }
    for command, operation in table.items():
        argv = command.split() + required.get(command, [])
        assert agent_cli.is_agent_invocation(argv), command
        op, _arguments = agent_cli.resolve(parser.parse_args(argv))
        assert op == operation, command


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

    monkeypatch.setattr(agent_cli.urllib.request, "urlopen", fake_urlopen)
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


def test_cli_surface_prompt_carries_the_whole_command_table() -> None:
    """D-12: the prompt is the reference, so it cannot drift from the CLI.

    Every session on the CLI surface opened with a `ciao help` call (1.56 per
    session on claude, 1.08 on opencode), which is most of the +4.5 s / +8.3 s
    the surface cost per turn. The table is in the prompt so that call is not
    needed; this test is what keeps it true when a command is added.
    """
    cli = system_prompt_payload("")["append"]
    missing = [command for command in _commands() if command not in cli]
    assert missing == []
    # It has to stay cheap: this text is prepended to every turn of every
    # CLI-surface chat.
    assert len(cli) < 9000


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


def test_opencode_cli_auto_mode_keeps_bash_ask_for_ciao_commands() -> None:
    """Auto mode emits only the generic `bash: ask` — no `ciao …` allow row —
    so every shell command, including `ciao …`, keeps a card."""
    from ciao.providers.opencode import mode_settings

    _agent, rules = mode_settings("auto")
    bash = [rule for rule in rules if rule["permission"] == "bash"]
    assert bash[0] == {"permission": "bash", "pattern": "*", "action": "ask"}
    assert not any(rule.get("pattern", "").startswith("ciao ") for rule in bash)
    # The credential denies still come last, after everything.
    assert rules[-1]["action"] == "deny"


@pytest.mark.parametrize("mode", ["plan", "normal", "bypass"])
def test_opencode_cli_rules_are_auto_mode_only(mode: str) -> None:
    """`plan`/`normal` ask on purpose; `bypass` already allows everything, and a
    trailing `ask` row would narrow it under last-match-wins."""
    from ciao.providers.opencode import mode_settings

    rules = mode_settings(mode)[1]
    bash = [rule for rule in rules if rule["permission"] == "bash"]
    assert not any(rule.get("pattern", "").startswith("ciao ") for rule in bash)
