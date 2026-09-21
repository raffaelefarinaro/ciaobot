"""The agent CLI surface: dispatcher, route, CLI mapping and prompt variant."""
from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import agent_cli
from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV, surface_for_chat
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


def test_mcp_surface_telemetry_is_still_tagged_mcp(tmp_path: Path) -> None:
    from tests.test_mcp_server import _client as _mcp_client, _rpc

    service, _ = _service(tmp_path)
    token = _token(service)
    with _mcp_client(service) as client:
        _rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        _rpc(client, token, "tools/call", {"name": "context_get", "arguments": {}}, request_id=2)
    record = json.loads(service._telemetry_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["surface"] == "mcp"


def test_surface_for_chat_reads_the_runtime_file(tmp_path: Path) -> None:
    assert surface_for_chat(tmp_path, "chat-1") == "mcp"
    (tmp_path / "agent_surface.json").write_text(json.dumps({"chat-1": "cli", "chat-2": "bogus"}), encoding="utf-8")
    assert surface_for_chat(tmp_path, "chat-1") == "cli"
    assert surface_for_chat(tmp_path, "chat-2") == "mcp"
    assert surface_for_chat(tmp_path, "chat-3") == "mcp"
    (tmp_path / "agent_surface.json").write_text(json.dumps({"*": "cli"}), encoding="utf-8")
    assert surface_for_chat(tmp_path, "anything") == "mcp"  # no wildcard on purpose
    (tmp_path / "agent_surface.json").write_text("not json", encoding="utf-8")
    assert surface_for_chat(tmp_path, "chat-1") == "mcp"


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
        (["chat", "archive"], ("chat_archive", {"chat_id": ""})),
        (["chat", "delete", "--chat", "c9"], ("chat_delete", {"chat_id": "c9"})),
        (["schedule", "list"], ("schedules_list", {})),
        (
            ["schedule", "create", "--prompt", "digest", "--frequency", "weekly", "--days-of-week", "mon,tue", "--daily-time", "09:00", "--interval-minutes", "0"],
            ("schedule", {"action": "create", "prompt": "digest", "frequency": "weekly", "days_of_week": ["mon", "tue"], "daily_time": "09:00", "interval_minutes": 0}),
        ),
        (["schedule", "update", "s1", "--title", "T"], ("schedule", {"action": "update", "schedule_id": "s1", "title": "T"})),
        (["schedule", "pause", "s1"], ("schedule_action", {"schedule_id": "s1", "action": "pause"})),
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
    mcp = system_prompt_payload("")["append"]
    cli = system_prompt_payload("", surface="cli")["append"]
    assert "MCP tools" in mcp and "`memory_update`" in mcp
    assert "ciao help" in cli and "ciao memory update" in cli and "ciao vault search" in cli
    for name in ("memory_update", "vault_search", "vault_expand", "file_surface", "background_run_start", "MCP tools", "CLAUDE.md`, with"):
        assert name not in cli, name
    assert "`AGENTS.md`" in cli


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


def test_provider_reuse_key_changes_when_the_surface_flips() -> None:
    from ciao.models import agent_control_token, provider_reuse_key

    mcp = SimpleNamespace(mcp_token="tok", extra_env={}, agent_surface="mcp")
    cli = SimpleNamespace(mcp_token="", extra_env={AGENT_TOKEN_ENV: "tok"}, agent_surface="cli")
    none = SimpleNamespace(mcp_token="", extra_env={}, agent_surface="mcp")
    assert agent_control_token(mcp) == agent_control_token(cli) == "tok"
    assert provider_reuse_key(mcp) != provider_reuse_key(cli)
    assert provider_reuse_key(none) == ""


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
