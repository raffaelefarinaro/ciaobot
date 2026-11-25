from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.claude_trim import trim_tool_denies
from ciao.config import CiaoConfig
from ciao.web.routes_api import (
    archive_workspace_setting,
    list_workspaces,
    provider_config_settings,
    reorder_workspaces,
    upsert_workspace_setting,
    workspace_shared_brains,
)


@pytest.fixture(autouse=True)
def _stub_auth_list(monkeypatch):
    monkeypatch.setattr("ciao.opencode_cli.bounded_output", lambda *a, **k: (0, b"[]"))


def test_settings_and_verify_expose_safe_opencode_inventory(tmp_path, monkeypatch):
    from itsdangerous import URLSafeTimedSerializer
    from starlette.middleware import Middleware
    from types import SimpleNamespace
    from ciao.web import routes_api
    from ciao.web.auth import AuthMiddleware, SESSION_COOKIE

    monkeypatch.setattr(routes_api, "_provider_config_payload", lambda config: {
        "connections": {"opencode": {"ready": True}, "claude": {"ready": False}}
    })
    monkeypatch.setattr("ciao.opencode_cli.resolve_opencode_binary", lambda: "fake")
    rows = [{"id": "ollama", "name": "Ollama", "connections": [
        {"type": "credential", "method": "key", "token": "hidden-sentinel"}]}]
    calls = []
    monkeypatch.setattr("ciao.opencode_cli.bounded_output",
                        lambda *a, **k: (calls.append(1) or (0, json.dumps(rows).encode())))
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(routes=[
        Route("/api/settings/providers", routes_api.provider_config_settings),
        Route("/api/settings/providers/{provider}/{action}", routes_api.provider_connection_action, methods=["POST"]),
    ], middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.config = SimpleNamespace(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    client = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000))
    path = "/api/settings/providers"
    assert client.get(path).status_code == 401
    assert client.post(path + "/opencode/verify", headers={"Origin": "http://localhost:8443"}).status_code == 401
    assert calls == []
    headers = {"Cookie": f"{SESSION_COOKIE}={serializer.dumps({'user': 'owner'})}",
               "Origin": "http://localhost:8443"}
    get = client.get(path, headers=headers)
    verify = client.post(path + "/opencode/verify", headers=headers)
    assert get.status_code == verify.status_code == 200
    assert get.headers["cache-control"] == verify.headers["cache-control"] == "no-store"
    assert get.json()["connections"]["claude"] == {"ready": False}
    inventory = get.json()["connections"]["opencode"]["credential_inventory"]
    assert inventory == verify.json()["credential_inventory"]
    assert inventory["providers"][0]["auth_types"] == ["api_key"]
    assert "hidden-sentinel" not in get.text + verify.text
    assert len(calls) == 2


def test_verify_inventory_is_fresh_during_older_inflight_get(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from itsdangerous import URLSafeTimedSerializer
    from starlette.middleware import Middleware
    from ciao.web import routes_api
    from ciao.web.auth import AuthMiddleware, SESSION_COOKIE

    entered, release = threading.Event(), threading.Event()
    calls = []

    def collector(*args, **kwargs):
        number = len(calls)
        calls.append(number)
        if number == 0:
            entered.set()
            assert release.wait(5)
        name = "old" if number == 0 else "new"
        return 0, json.dumps([{"id": name, "name": name, "connections": [{"type": "env"}]}]).encode()

    monkeypatch.setattr(routes_api, "_provider_config_payload", lambda config: {"connections": {"opencode": {}}})
    monkeypatch.setattr("ciao.opencode_cli.resolve_opencode_binary", lambda: "fake")
    monkeypatch.setattr("ciao.opencode_cli.bounded_output", collector)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(routes=[
        Route("/api/settings/providers", routes_api.provider_config_settings),
        Route("/api/settings/providers/{provider}/{action}", routes_api.provider_connection_action, methods=["POST"]),
    ], middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.config = SimpleNamespace(workspace_root=tmp_path, state_path=tmp_path / "state.json")
    headers = {"Cookie": f"{SESSION_COOKIE}={serializer.dumps({'user': 'owner'})}",
               "Origin": "http://localhost:8443"}
    path = "/api/settings/providers"
    responses = []
    errors = []

    def old_get():
        try:
            with TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000)) as client:
                responses.append(client.get(path, headers=headers))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=old_get)
    thread.start()
    try:
        assert entered.wait(5)
        with TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000)) as client:
            verify = client.post(path + "/opencode/verify", headers=headers)
            release.set()
            thread.join(5)
            fresh = client.get(path, headers=headers)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert responses[0].json()["connections"]["opencode"]["credential_inventory"]["providers"][0]["id"] == "old"
    assert verify.json()["credential_inventory"]["providers"][0]["id"] == "new"
    assert fresh.json()["connections"]["opencode"]["credential_inventory"]["providers"][0]["id"] == "new"


def test_opencode_verify_refreshes_models_for_default_and_named_workspaces(tmp_path, monkeypatch):
    """Verify drops the ACP catalog cache, so the next /api/models probes each workspace again."""
    from types import SimpleNamespace
    from itsdangerous import URLSafeTimedSerializer
    from starlette.middleware import Middleware
    from ciao.providers.acp.agents import OpencodeAcpProvider
    from ciao.providers.acp.provider import invalidate_acp_catalog as acp_invalidate
    from ciao.web import routes_api
    from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
    import ciao.opencode_cli as mod

    roots = [tmp_path / "default", tmp_path / "named"]
    versions = {root: "old" for root in roots}
    counts = {root: 0 for root in roots}

    async def fake_probe(cls, workspace_root):
        # The real model_catalog cache sits in front of this probe.
        assert workspace_root in roots
        counts[workspace_root] += 1
        return [{"model": f"opencode/{workspace_root.name}-{versions[workspace_root]}",
                 "label": "m", "variants": []}]

    monkeypatch.setattr(OpencodeAcpProvider, "_probe_catalog", classmethod(fake_probe))
    config = SimpleNamespace(
        workspace_root=roots[0], state_path=tmp_path / "state.json",
        workspace=lambda name: object() if name == "named" else None,
        agent_root=lambda name: roots[1],
        default_model_for_provider=lambda provider: "",
        claude_default_model="",
    )
    monkeypatch.setattr(routes_api, "_provider_config_payload", lambda config: {"connections": {"opencode": {}}})
    monkeypatch.setattr(mod, "opencode_credential_inventory", lambda: {"status": "complete", "providers": []})
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(routes=[
        Route("/api/models", routes_api.list_models),
        Route("/api/settings/providers/{provider}/{action}", routes_api.provider_connection_action, methods=["POST"]),
    ], middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.config = config
    headers = {"Cookie": f"{SESSION_COOKIE}={serializer.dumps({'user': 'owner'})}",
               "Origin": "http://localhost:8443"}
    urls = ["/api/models", "/api/models?workspace=named"]
    try:
        with TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 50000)) as client:
            for url, root in zip(urls, roots):
                body = client.get(url, headers=headers).json()
                assert body["provider_models"]["opencode"] == [f"opencode/{root.name}-old"]
            for root in roots:
                versions[root] = "new"
            assert client.post("/api/settings/providers/opencode/verify", headers=headers).status_code == 200
            for url, root in zip(urls, roots):
                body = client.get(url, headers=headers).json()
                expected = [f"opencode/{root.name}-new"]
                assert body["provider_models"]["opencode"] == expected
        assert counts == {roots[0]: 2, roots[1]: 2}
    finally:
        acp_invalidate("opencode")


class _PCM:
    def __init__(self) -> None:
        self.refresh_count = 0
        self.archived: list[str] = []

    def refresh_workspaces(self) -> None:
        self.refresh_count += 1

    def workspace_scope(self, workspace: str) -> tuple[set[str], set[str]]:
        return set(), set()

    def workspace_busy_chat_ids(self, workspace: str) -> list[str]:
        return []

    def refuse_workspace_teardown(self, workspace: str) -> None:
        return None

    def archive_workspace_projects(self, workspace: str) -> dict[str, int]:
        self.archived.append(workspace)
        return {"projects": 2, "chats": 1}


def _client(
    tmp_path: Path,
    env_extra: dict[str, str] | None = None,
    *,
    peer: tuple[str, int] = ("127.0.0.1", 50000),
):
    """`peer` is the SOCKET address the request appears to come from.

    Starlette defaults it to the literal "testclient", which is neither
    loopback nor routable, so a real local request has to look like one.
    """
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    env.update(env_extra or {})
    config = CiaoConfig.from_env(env)
    pcm = _PCM()
    app = Starlette(
        routes=[
            Route("/api/workspaces", list_workspaces, methods=["GET"]),
            Route("/api/workspaces", upsert_workspace_setting, methods=["POST"]),
            Route("/api/workspaces/reorder", reorder_workspaces, methods=["POST"]),
            Route(
                "/api/workspaces/{name}",
                upsert_workspace_setting,
                methods=["PATCH"],
            ),
            Route(
                "/api/workspaces/{name}/shared-brains",
                workspace_shared_brains,
                methods=["GET"],
            ),
            Route(
                "/api/workspaces/{name}",
                archive_workspace_setting,
                methods=["DELETE"],
            ),
            Route(
                "/api/settings/providers",
                provider_config_settings,
                methods=["GET"],
            ),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    return TestClient(app, client=peer), config, pcm


def test_post_workspace_persists_runtime_registry_and_updates_live_config(tmp_path):
    client, config, pcm = _client(tmp_path)

    resp = client.post(
        "/api/workspaces",
        json={
            "name": "client-a",
            "vault_root": "client-a",
            "default_provider": "claude",
            "gws_profile": "work",
            "disallowed_tools": ["mcp__claude_ai_Slack", "Bash"],
        },
    )

    assert resp.status_code == 201
    data = resp.json()
    names = [workspace["name"] for workspace in data["workspaces"]]
    assert "client-a" in names
    assert config.workspace("client-a").default_provider == "claude"
    # The workspace no longer pins a model; new chats inherit the provider
    # default (claude_default_model).
    assert config.default_model_for_workspace("client-a") == "opus"
    # extras are added on top of the trim defaults
    assert config.disallowed_tools_for_workspace("client-a") == [
        *trim_tool_denies({}),
        "mcp__claude_ai_Slack",
        "Bash",
    ]
    assert pcm.refresh_count == 1
    assert data["provider_options"] == [
        {"value": "claude", "label": "Anthropic (via Claude Code)"},
        {"value": "opencode", "label": "OpenCode"},
        {"value": "codex", "label": "OpenAI (via Codex)"},
    ]

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    client_workspace = next(item for item in stored if item["name"] == "client-a")
    assert client_workspace == {
        "name": "client-a",
        "vault_root": "memory-vault/client-a",
        "default_provider": "claude",
        "disallowed_tools": ["mcp__claude_ai_Slack", "Bash"],
        "color": "amber",
        "memory_handover": "all",
    }


def test_patch_and_delete_workspace_update_runtime_registry(tmp_path):
    client, config, pcm = _client(tmp_path)
    client.post(
        "/api/workspaces",
        json={"name": "client-a", "vault_root": "client-a"},
    )

    patch = client.patch(
        "/api/workspaces/client-a",
        json={"disallowed_tools": "mcp__example,Bash"},
    )
    assert patch.status_code == 200
    assert config.disallowed_tools_for_workspace("client-a") == [*trim_tool_denies({}), "mcp__example", "Bash"]

    delete = client.delete("/api/workspaces/client-a")
    assert delete.status_code == 200
    assert config.workspace("client-a") is None
    # One entry, not two: the bootstrap registry is read off the vault, and this
    # fixture's vault has no `work/` directory. It used to manufacture `work`
    # regardless, which is what left an install unable to migrate — a registered
    # workspace with no vault directory refuses the whole plan.
    assert json.loads((tmp_path / ".runtime" / "workspaces.json").read_text()) == [
        {
            "name": "personal",
            "vault_root": "memory-vault/personal",
            "default_provider": "claude",
            "disallowed_tools": None,
            "color": "amber",
        },
    ]
    assert pcm.refresh_count == 3


def test_workspace_patch_ignores_a_gws_profile_field(tmp_path):
    client, _, _pcm = _client(tmp_path)
    client.post("/api/workspaces", json={"name": "client-a"})
    registry = tmp_path / ".runtime" / "workspaces.json"
    before = next(w for w in json.loads(registry.read_text()) if w["name"] == "client-a")

    patched = client.patch("/api/workspaces/client-a", json={"gws_profile": "x\ncurl"})

    assert patched.status_code == 200
    stored = next(w for w in json.loads(registry.read_text()) if w["name"] == "client-a")
    assert stored["color"] == before["color"]
    assert stored["default_provider"] == before["default_provider"]
    assert "gws_profile" not in stored


def test_workspace_save_never_accepts_a_request_body_vault_path(tmp_path):
    client, config, _pcm = _client(tmp_path)

    created = client.post(
        "/api/workspaces",
        json={"name": "research", "vault_root": "/"},
    )
    assert created.status_code == 201
    assert (
        config.workspace_vault_root("research")
        == tmp_path / "memory-vault" / "research"
    )

    existing = config.workspace("research")
    assert existing is not None
    existing.vault_root = str(tmp_path / "external-vault")
    patched = client.patch(
        "/api/workspaces/research",
        json={"vault_root": "../outside", "gws_profile": "work"},
    )
    assert patched.status_code == 200
    assert config.workspace("research").vault_root == str(
        tmp_path / "external-vault"
    )


def test_workspace_creation_rejects_a_symlinked_vault_folder(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    vault = tmp_path / "memory-vault"
    vault.mkdir()
    (vault / "research").symlink_to(target, target_is_directory=True)
    client, config, pcm = _client(tmp_path)

    response = client.post("/api/workspaces", json={"name": "research"})

    assert response.status_code == 400
    assert "symlink" in response.json()["error"]
    assert config.workspace("research") is None
    assert pcm.refresh_count == 0


def test_workspace_creation_persists_or_refuses_memory_handover(tmp_path):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"

    created = client.post("/api/workspaces", json={"name": "research", "memory_handover": "dismiss"})
    assert created.status_code == 201
    assert config.workspace("research").memory_handover == "dismiss"
    stored = next(w for w in json.loads(registry.read_text()) if w["name"] == "research")
    assert stored["memory_handover"] == "dismiss"

    before = registry.read_bytes()
    refused = client.post("/api/workspaces", json={"name": "other", "memory_handover": "everything"})
    assert refused.status_code == 400
    assert config.workspace("other") is None
    assert registry.read_bytes() == before


def test_new_workspace_defaults_to_all_memory_handover_without_changing_existing(tmp_path):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"

    created = client.post("/api/workspaces", json={"name": "research"})

    assert created.status_code == 201
    assert config.workspace("personal").memory_handover == "off"
    assert config.workspace("research").memory_handover == "all"
    stored = {w["name"]: w for w in json.loads(registry.read_text())}
    assert stored["research"]["memory_handover"] == "all"
    listed = client.get("/api/workspaces").json()
    assert next(w for w in listed["workspaces"] if w["name"] == "research")["memory_handover"] == "all"

    disabled = client.patch("/api/workspaces/research", json={"memory_handover": "off"})
    assert disabled.status_code == 200
    assert config.workspace("research").memory_handover == "off"
    stored = {w["name"]: w for w in json.loads(registry.read_text())}
    assert "memory_handover" not in stored["research"]


def test_workspace_creation_rejects_case_and_vault_owner_collisions(tmp_path):
    client, config, _pcm = _client(tmp_path)
    assert client.post("/api/workspaces", json={"name": "Research"}).status_code == 201

    case_collision = client.post(
        "/api/workspaces",
        json={"name": "research"},
    )
    assert case_collision.status_code == 400
    assert "conflicts" in case_collision.json()["error"]

    # `personal`, not `work`: the bootstrap registry is read off the vault now, so
    # only workspaces that actually have a vault directory exist. Any registered
    # workspace serves here — the point is that its vault folder is already owned.
    existing = config.workspace("personal")
    assert existing is not None
    existing.vault_root = str(tmp_path / "memory-vault" / "client")
    root_collision = client.post(
        "/api/workspaces",
        json={"name": "client"},
    )
    assert root_collision.status_code == 400
    assert "already owned" in root_collision.json()["error"]


def test_workspace_validation_rejects_bad_name_and_provider(tmp_path):
    client, _config, _pcm = _client(tmp_path)

    bad_name = client.post("/api/workspaces", json={"name": "../bad"})
    assert bad_name.status_code == 400
    assert "name" in bad_name.json()["error"]

    bad_provider = client.post(
        "/api/workspaces",
        json={"name": "client-a", "default_provider": "telepathy"},
    )
    assert bad_provider.status_code == 400
    assert "provider" in bad_provider.json()["error"]


def test_workspace_provider_options_are_the_runtime_providers(tmp_path):
    """The registry is the only source: no backend keys widen this list."""
    client, _config, _pcm = _client(tmp_path)

    data = client.get("/api/workspaces").json()
    assert data["provider_options"] == [
        {"value": "claude", "label": "Anthropic (via Claude Code)"},
        {"value": "opencode", "label": "OpenCode"},
        {"value": "codex", "label": "OpenAI (via Codex)"},
    ]

    resp = client.post(
        "/api/workspaces",
        json={"name": "client-a", "default_provider": "opencode"},
    )
    assert resp.status_code == 201
    assert resp.json()["workspaces"][-1]["default_provider"] == "opencode"


def test_stale_stored_provider_serializes_coerced_and_saves(tmp_path):
    """A registry written by a pre-refactor release (provider "ollama", legacy
    ``model_bucket`` key) must list with a registered provider — the PWA
    renders it into a <select> limited to ``provider_options`` — and a PATCH
    must round-trip instead of 400ing on the stale stored value."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps(
            [
                {
                    "name": "personal",
                    "vault_root": "memory-vault/personal",
                    "default_provider": "ollama",
                    "model_bucket": "big",
                },
                {
                    "name": "work",
                    "vault_root": "memory-vault/work",
                    "default_provider": "claude",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client, config, _pcm = _client(tmp_path)
    assert config.workspace("personal").default_provider == "ollama"

    listed = client.get("/api/workspaces").json()
    personal = next(w for w in listed["workspaces"] if w["name"] == "personal")
    option_values = {option["value"] for option in listed["provider_options"]}
    # Coerced to the effective provider, mirroring default_provider_for_workspace.
    assert personal["default_provider"] == "claude"
    assert personal["default_provider"] in option_values

    # A save that omits the provider keeps working despite the stale record.
    untouched = client.patch(
        "/api/workspaces/personal",
        json={"color": "amber"},
    )
    assert untouched.status_code == 200

    # Saving the coerced value back round-trips and rewrites a clean record.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"default_provider": personal["default_provider"]},
    )
    assert resp.status_code == 200
    assert config.workspace("personal").default_provider == "claude"

    stored = json.loads((runtime / "workspaces.json").read_text())
    personal_stored = next(w for w in stored if w["name"] == "personal")
    assert personal_stored["default_provider"] == "claude"
    assert "model_bucket" not in personal_stored

    # Explicitly invalid writes are still rejected.
    bad = client.patch(
        "/api/workspaces/personal",
        json={"default_provider": "ollama"},
    )
    assert bad.status_code == 400


def test_persist_workspace_registry_normalizes_stale_provider(tmp_path):
    """Direct config persistence must not resurrect a removed provider."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "workspaces.json").write_text(
        json.dumps([{
            "name": "personal",
            "vault_root": "memory-vault/personal",
            "default_provider": "ollama",
        }]) + "\n",
        encoding="utf-8",
    )

    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(runtime),
    })
    assert config.workspace("personal").default_provider == "ollama"

    config.persist_workspace_registry()

    stored = json.loads((runtime / "workspaces.json").read_text())
    assert stored[0]["default_provider"] == "claude"


def test_connectors_always_allowed_and_extras_honored(tmp_path):
    """claude.ai connectors are always allowed; harness tools blocked by
    default; an explicit per-workspace extras list is added on top of the
    trim defaults."""
    client, config, _pcm = _client(tmp_path)

    # Default: connectors allowed, harness tools blocked.
    personal = config.disallowed_tools_for_workspace("personal")
    assert "mcp__claude_ai_Airtable" not in personal
    assert "EnterPlanMode" in personal

    # Extras are added on top of the trim defaults, and never include the
    # connectors.
    resp = client.patch(
        "/api/workspaces/personal",
        json={"disallowed_tools": "mcp__n8n_mcp"},
    )
    assert resp.status_code == 200
    effective = config.disallowed_tools_for_workspace("personal")
    assert effective == [*trim_tool_denies({}), "mcp__n8n_mcp"]
    assert "mcp__claude_ai_Airtable" not in effective

    # Persisted to disk without the (removed) claude_ai_mcps field.
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    personal_stored = next(w for w in stored if w["name"] == "personal")
    assert "claude_ai_mcps" not in personal_stored
    assert personal_stored["disallowed_tools"] == ["mcp__n8n_mcp"]


def test_retired_connectors_field_is_not_read_on_save(tmp_path):
    """A PATCH that still sends the old field does not trim connectors or persist it."""
    client, config, _pcm = _client(tmp_path)
    client.post("/api/workspaces", json={"name": "client-a", "vault_root": "client-a"})

    ignored = client.patch("/api/workspaces/client-a", json={"claude_ai_connectors": False})
    assert ignored.status_code == 200
    assert "setting:claude_ai_connectors" not in config.workspace("client-a").claude_trim
    assert all(
        "claude_ai_connectors" not in w for w in ignored.json()["workspaces"]
    )

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    row = next(w for w in stored if w["name"] == "client-a")
    assert "claude_ai_connectors" not in row
    assert "setting:claude_ai_connectors" not in row.get("claude_trim", {})


def test_claude_trim_saves_persists_and_validates(tmp_path):
    client, config, _pcm = _client(tmp_path)
    client.post("/api/workspaces", json={"name": "client-a", "vault_root": "client-a"})

    on = client.patch(
        "/api/workspaces/client-a",
        json={"claude_trim": {"tool:ReportFindings": True}},
    )
    assert on.status_code == 200
    listed = client.get("/api/workspaces").json()
    assert next(w for w in listed["workspaces"] if w["name"] == "client-a")["claude_trim"] == {
        "tool:ReportFindings": True
    }
    assert config.workspace("client-a").claude_trim == {"tool:ReportFindings": True}

    # Routine triggers are trimmed by default, and the user can un-trim them.
    assert "RemoteTrigger" in config.disallowed_tools_for_workspace("client-a")
    untrimmed = client.patch(
        "/api/workspaces/client-a",
        json={"claude_trim": {"tool:RemoteTrigger": False}},
    )
    assert untrimmed.status_code == 200
    assert config.workspace("client-a").claude_trim == {"tool:RemoteTrigger": False}
    assert "RemoteTrigger" not in config.disallowed_tools_for_workspace("client-a")

    # The loop family has no switch: a stale client's retired key is ignored, the valid
    # toggle in the same map still applies, and the loop family stays denied.
    stale = client.patch(
        "/api/workspaces/client-a",
        json={"claude_trim": {"tool:CronCreate": False, "tool:RemoteTrigger": False, "skill:loop": False}},
    )
    assert stale.status_code == 200
    assert config.workspace("client-a").claude_trim == {"tool:RemoteTrigger": False}
    assert "CronCreate" in config.disallowed_tools_for_workspace("client-a")
    assert "CronDelete" in config.disallowed_tools_for_workspace("client-a")

    # A key the catalog never had is still a 400.
    unknown = client.patch(
        "/api/workspaces/client-a",
        json={"claude_trim": {"tool:Nope": False}},
    )
    assert unknown.status_code == 400
    assert config.workspace("client-a").claude_trim == {"tool:RemoteTrigger": False}

    # An empty object resets to the catalog defaults.
    reset = client.patch("/api/workspaces/client-a", json={"claude_trim": {}})
    assert reset.status_code == 200
    assert config.workspace("client-a").claude_trim == {}

    # A save that leaves claude_trim out keeps the stored map.
    client.patch("/api/workspaces/client-a", json={"claude_trim": {"tool:ReportFindings": True}})
    kept = client.patch("/api/workspaces/client-a", json={"color": "cyan"})
    assert kept.status_code == 200
    assert config.workspace("client-a").claude_trim == {"tool:ReportFindings": True}


def test_workspaces_payload_includes_trim_catalog(tmp_path):
    client, _config, _pcm = _client(tmp_path)

    data = client.get("/api/workspaces").json()
    options = {option["key"]: option for option in data["claude_trim_options"]}
    assert options["tool:NotebookEdit"]["default_trimmed"] is True
    assert options["tool:RemoteTrigger"]["default_trimmed"] is True
    assert not ({"tool:CronCreate", "tool:ScheduleWakeup", "skill:loop"} & set(options))
    assert all("locked" not in option for option in options.values())


def test_workspace_color_defaults_persists_and_validates(tmp_path):
    """Accent color defaults to amber, persists on write, and rejects unknowns."""
    client, config, _pcm = _client(tmp_path)

    listed = client.get("/api/workspaces").json()
    personal = next(w for w in listed["workspaces"] if w["name"] == "personal")
    assert personal["color"] == "amber"

    create = client.post(
        "/api/workspaces",
        json={"name": "client-a", "color": "cyan"},
    )
    assert create.status_code == 201
    created = next(w for w in create.json()["workspaces"] if w["name"] == "client-a")
    assert created["color"] == "cyan"
    assert config.workspace("client-a").color == "cyan"

    patch = client.patch(
        "/api/workspaces/client-a",
        json={"color": "emerald"},
    )
    assert patch.status_code == 200
    updated = next(w for w in patch.json()["workspaces"] if w["name"] == "client-a")
    assert updated["color"] == "emerald"
    assert config.workspace("client-a").color == "emerald"

    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    assert next(w for w in stored if w["name"] == "client-a")["color"] == "emerald"

    # Other fields can update without resetting color.
    keep = client.patch(
        "/api/workspaces/client-a",
        json={"disallowed_tools": "Bash"},
    )
    assert keep.status_code == 200
    assert config.workspace("client-a").color == "emerald"

    bad = client.patch("/api/workspaces/client-a", json={"color": "neon"})
    assert bad.status_code == 400
    assert "color" in bad.json()["error"]


def test_new_accent_ids_accepted():
    from ciao.config import coerce_workspace_color

    for color in ("blue", "teal", "lime", "fuchsia"):
        assert coerce_workspace_color(color) == color
    with pytest.raises(ValueError):
        coerce_workspace_color("red")


def test_provider_config_offers_no_api_keys(tmp_path, monkeypatch):
    """Settings -> Models & providers has no key fields left to type into.

    Every provider authenticates through its own CLI, so the route is
    read-only and reports connection rows only.
    """
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path))
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PWA_AUTH_TOKEN=t\nANTHROPIC_API_KEY=sk-anthropic\n",
        encoding="utf-8",
    )
    client, _config, _pcm = _client(tmp_path, {"ANTHROPIC_API_KEY": "sk-anthropic"})

    data = client.get("/api/settings/providers").json()
    # Only the CLI connection rows: every provider signs in through its own
    # CLI, so there are no API keys to report or edit.
    assert set(data) == {"connections"}
    assert "sk-anthropic" not in json.dumps(data)
    assert set(data["connections"]) == {"claude", "opencode", "codex"}
    assert client.patch("/api/settings/providers", json={}).status_code == 405


def test_creating_a_workspace_seeds_its_agent_root(tmp_path, monkeypatch):
    """A new workspace's chats run from its own root immediately.

    Persisting the registry and refreshing the manager only creates the General
    vault document. Nothing seeded `CLAUDE.md`/`AGENTS.md`, commands, agents or
    the skill mirrors, so a chat opened right after creation ran with no
    workspace guide and none of the packaged capabilities — the startup task
    repairs it, but only after a restart.
    """
    client, config, _pcm = _client(tmp_path)
    seeded: list[tuple[Path, bool]] = []

    def fake_sync(workspace, *, refresh_upstream=True, **_kw):
        seeded.append((Path(workspace), refresh_upstream))
        return None

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", fake_sync)

    created = client.post(
        "/api/workspaces", json={"name": "client-b", "vault_root": "client-b"}
    )

    assert created.status_code == 201
    assert created.json()["bootstrapped"] is True
    assert seeded == [(Path(config.agent_root("client-b")), False)]


def test_seeding_failure_does_not_undo_the_created_workspace(tmp_path, monkeypatch):
    """Creation already succeeded, so a sync failure is reported, not unwound."""
    client, config, _pcm = _client(tmp_path)

    def boom(workspace, **_kw):
        raise RuntimeError("no packaged skills here")

    monkeypatch.setattr("ciao.sync_skills.sync_workspace_skills", boom)

    created = client.post(
        "/api/workspaces", json={"name": "client-c", "vault_root": "client-c"}
    )

    assert created.status_code == 201
    assert created.json()["bootstrapped"] is False
    assert config.workspace("client-c") is not None


def test_proposal_action_rejects_an_unknown_action_before_touching_the_queue(tmp_path):
    """An unrecognised action must never discard a queued fact.

    `POST /api/proposals/{id}/{action}` did not validate `action`: anything that
    was not `accept` skipped the promotion block but still fell through to the
    bullet removal and returned the dismiss-shaped success payload, so a typo or
    a stale client silently threw away a fact nobody had asked to dismiss. The
    batch endpoint already validates; this follows its shape.
    """
    from ciao.web.routes_api import list_proposals, proposal_action

    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    config = CiaoConfig.from_env(env)
    queue = config.workspace_vault_root("personal") / "Workspace" / "Memory-Proposals.md"
    queue.parent.mkdir(parents=True, exist_ok=True)
    bullet = "- [memory] Remember the lesson about check-first.  _(from: Decisions)_"
    queue.write_text(
        f"# Memory Proposals\n\n## 2026-08-19 curation pass\n\n{bullet}\n",
        encoding="utf-8",
    )
    app = Starlette(
        routes=[
            Route("/api/proposals", list_proposals, methods=["GET"]),
            Route("/api/proposals/{id}/{action}", proposal_action, methods=["POST"]),
        ]
    )
    app.state.config = config
    client = TestClient(app)

    listed = client.get("/api/proposals")
    assert listed.status_code == 200, listed.text
    pid = listed.json()["rows"][0]["id"]

    resp = client.post(f"/api/proposals/{pid}/accpet")

    assert resp.status_code == 400, resp.json()
    assert "accept|dismiss" in resp.json()["error"]
    # The fact is still queued.
    assert bullet in queue.read_text(encoding="utf-8")


# ---- vault ownership overlap ------------------------------------------------


def _owner_config(roots: dict[str, str], *, shared: str = ""):
    """Config stub answering the three questions `vault_root_owner` asks."""
    from types import SimpleNamespace

    return SimpleNamespace(
        vault_root=Path(shared) if shared else None,
        workspace_names=lambda: tuple(roots),
        workspace_vault_root=lambda name: Path(roots[name]),
    )


def test_vault_ownership_is_overlap_not_equality(tmp_path):
    """A target that merely CONTAINS or sits inside another vault still collides.

    Two registry entries covering the same notes let chats and vault writes in
    either workspace read and rewrite the other's data. Equality alone would
    miss both nestings.

    This is covered directly because the overlap cases were previously only
    exercised through the vault-relocation endpoint, which has been cut from
    this release — the guard itself predates relocation and still runs on every
    workspace creation, so it must not lose its coverage with the feature.
    """
    from ciao.workspaces import vault_root_owner

    clients = tmp_path / "clients"
    config = _owner_config({"alpha": str(clients / "alpha")})

    # Exact.
    assert vault_root_owner(config, clients / "alpha") == "alpha"
    # The target is an ANCESTOR of alpha's vault.
    assert vault_root_owner(config, clients) == "alpha"
    # The target sits INSIDE alpha's vault.
    assert vault_root_owner(config, clients / "alpha" / "People") == "alpha"
    # Unrelated.
    assert vault_root_owner(config, tmp_path / "elsewhere") is None


def test_the_shared_vault_root_is_not_treated_as_an_owner(tmp_path):
    """One nesting is legitimate and must stay so.

    Where setup pointed a workspace at `CIAO_VAULT_ROOT` itself, every standard
    `<shared>/<name>` folder is inside that workspace's vault by design.
    Counting it as a conflict would refuse every new workspace on those
    installs.
    """
    from ciao.workspaces import vault_root_owner

    shared = tmp_path / "memory-vault"
    config = _owner_config({"legacy": str(shared)}, shared=str(shared))

    assert vault_root_owner(config, shared / "newcomer") is None
    # The shared root itself is still owned — that is equality, not nesting.
    assert vault_root_owner(config, shared) == "legacy"


class _RenameEvents:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


class _RenamePCM:
    """The slice of ProjectChatManager a workspace rename touches."""

    def __init__(self) -> None:
        from ciao.web.project_chats import ProjectInfo

        self._projects = {
            "proj-1": ProjectInfo(
                project_id="proj-1", name="Work", workspace="alpha"
            )
        }
        self.events = _RenameEvents()
        self.refresh_count = 0

    def refresh_workspaces(self) -> None:
        self.refresh_count += 1

    def workspace_busy_chat_ids(self, workspace: str) -> list[str]:
        return []

    def evict_workspace_providers(self, workspace: str) -> list[str]:
        return []

    def _save(self, *, reason: str = "registry_mutation") -> None:
        return None


def test_patch_rename_returns_renamed_and_publishes_from_to(tmp_path, monkeypatch):
    """PATCH with a different name renames the workspace and reports the pair.

    The response carries ``renamed`` as ``{from, to}``, every reference
    follows the new name, and a second client event — beside the response —
    carries both names so other tabs can follow the workspace.
    """
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.config import SharedBrainLink
    from ciao.import_store import ImportStore, engine_store_path
    from ciao.schedules import ScheduleStore
    from ciao.webhooks import WebhookStore

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "alpha": WorkspaceConfig(
                name="alpha", vault_root="memory-vault/alpha"
            ),
            "work": WorkspaceConfig(
                name="work", vault_root="memory-vault/work"
            ),
        },
    )
    config.persist_workspace_registry()
    link = SharedBrainLink(
        name="notes", path=str(tmp_path.parent / f"rename-brain-{tmp_path.name}"),
        identity="github.com/acme/notes",
    )
    config.workspaces["alpha"].shared_brains = [link]
    from ciao import shared_brain, workspace_rename
    original_rename = workspace_rename.rename_workspace
    claimed: list[bool] = []

    def assert_claim(*args, **kwargs):
        claimed.append(shared_brain._checkout_lock(link.path).locked())
        return original_rename(*args, **kwargs)

    monkeypatch.setattr(workspace_rename, "rename_workspace", assert_claim)
    runtime = tmp_path / ".runtime"

    schedules = ScheduleStore(runtime)
    user = schedules.create(
        daily_time_utc="10:00",
        prompt="morning brief",
        model="",
        chat_id=0,
        workspace="alpha",
        title="Brief",
    )
    webhooks = WebhookStore(runtime / "webhooks.json")
    trigger, secret = webhooks.create(
        name="hook", workspace="alpha", instructions="do the thing"
    )
    imports = ImportStore(engine_store_path(config))
    batch = imports.create(
        workspace="alpha",
        sources=[{"provider": "claude_code", "source_id": "sess-1"}],
    )
    pcm = _RenamePCM()
    app = Starlette(
        routes=[
            Route(
                "/api/workspaces/{name}",
                upsert_workspace_setting,
                methods=["PATCH"],
            ),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    client = TestClient(app, client=("127.0.0.1", 50000))

    resp = client.patch(
        "/api/workspaces/alpha", json={"name": "beta", "color": "cyan"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["renamed"] == {"from": "alpha", "to": "beta"}
    assert sorted(entry["name"] for entry in body["workspaces"]) == [
        "beta",
        "work",
    ]
    beta = next(entry for entry in body["workspaces"] if entry["name"] == "beta")
    assert beta["color"] == "cyan"
    assert pcm.events.published == [
        {"type": "workspaces_changed", "from": "alpha", "to": "beta"}
    ]
    assert pcm._projects["proj-1"].workspace == "beta"
    assert schedules.get(user.schedule_id) is not None
    assert schedules.get(user.schedule_id).workspace == "beta"  # type: ignore[union-attr]
    assert webhooks.get(trigger.trigger_id).workspace == "beta"
    assert imports.get(batch.batch_id).workspace == "beta"
    assert claimed == [True]
    assert config.workspaces["beta"].shared_brains[0].lifetime_id == link.lifetime_id


def test_patch_rename_without_the_key_keeps_provider_overrides(tmp_path):
    """A rename PATCH that omits provider_overrides carries the stored map to the new name."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.config import CiaoConfig, WorkspaceConfig

    overrides = {"claude": {"model": "opus", "permission_mode": "manual"}}
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "alpha": WorkspaceConfig(
                name="alpha", vault_root="memory-vault/alpha", provider_overrides=overrides
            ),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    config.persist_workspace_registry()
    app = Starlette(
        routes=[Route("/api/workspaces/{name}", upsert_workspace_setting, methods=["PATCH"])]
    )
    app.state.config = config
    app.state.project_chat_manager = _RenamePCM()
    client = TestClient(app, client=("127.0.0.1", 50000))

    resp = client.patch("/api/workspaces/alpha", json={"name": "beta"})

    assert resp.status_code == 200, resp.json()
    beta = next(w for w in resp.json()["workspaces"] if w["name"] == "beta")
    assert beta["provider_overrides"] == overrides
    assert config.workspace("beta").provider_overrides == overrides
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    assert next(r for r in stored if r["name"] == "beta")["provider_overrides"] == overrides


@pytest.mark.parametrize(
    "extra",
    [
        {"default_provider": "invalid"},
        {"color": "chartreuse"},
        {"disallowed_tools": 123},
        {"provider_overrides": {"opencode": {"model": 7}}},
    ],
)
def test_patch_rename_with_invalid_setting_writes_nothing(tmp_path, extra):
    """A rename carrying an invalid setting is refused before any write.

    The 400 leaves the registry, every reference, the agent directory and
    the emitted events exactly as they were: validation runs against the
    proposed renamed record before the rename itself runs.
    """
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.import_store import ImportStore, engine_store_path
    from ciao.schedules import ScheduleStore
    from ciao.webhooks import WebhookStore

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "alpha": WorkspaceConfig(
                name="alpha", vault_root="memory-vault/alpha"
            ),
            "work": WorkspaceConfig(
                name="work", vault_root="memory-vault/work"
            ),
        },
    )
    config.persist_workspace_registry()
    runtime = tmp_path / ".runtime"

    schedules = ScheduleStore(runtime)
    user = schedules.create(
        daily_time_utc="10:00",
        prompt="morning brief",
        model="",
        chat_id=0,
        workspace="alpha",
        title="Brief",
    )
    webhooks = WebhookStore(runtime / "webhooks.json")
    trigger, secret = webhooks.create(
        name="hook", workspace="alpha", instructions="do the thing"
    )
    trigger = webhooks.update(
        trigger.trigger_id, expected_revision=trigger.revision, enabled=True
    )
    imports = ImportStore(engine_store_path(config))
    batch = imports.create(
        workspace="alpha",
        sources=[{"provider": "claude_code", "source_id": "sess-1"}],
    )
    pcm = _RenamePCM()
    app = Starlette(
        routes=[
            Route(
                "/api/workspaces/{name}",
                upsert_workspace_setting,
                methods=["PATCH"],
            ),
        ]
    )
    app.state.config = config
    app.state.project_chat_manager = pcm
    client = TestClient(app, client=("127.0.0.1", 50000))

    before = {
        path.name: path.read_bytes()
        for path in (
            runtime / "workspaces.json",
            runtime / "schedules.json",
            runtime / "webhooks.json",
            engine_store_path(config),
        )
    }

    resp = client.patch("/api/workspaces/alpha", json={"name": "beta", **extra})

    assert resp.status_code == 400
    assert sorted(config.workspace_names()) == ["alpha", "work"]
    assert config.workspace("beta") is None
    for path in (
        runtime / "workspaces.json",
        runtime / "schedules.json",
        runtime / "webhooks.json",
        engine_store_path(config),
    ):
        assert path.read_bytes() == before[path.name]
    assert pcm.events.published == []
    assert pcm._projects["proj-1"].workspace == "alpha"
    assert schedules.get(user.schedule_id).workspace == "alpha"  # type: ignore[union-attr]
    assert webhooks.get(trigger.trigger_id).workspace == "alpha"
    assert webhooks.authenticate(trigger.trigger_id, secret) is not None
    assert imports.get(batch.batch_id).workspace == "alpha"
    assert not (tmp_path / "beta").exists()


def test_workspace_memory_handover_defaults_off_persists_and_validates(tmp_path):
    """The handover is operator-set per workspace, stored only when not off."""
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"

    def stored_row() -> dict:
        return next(w for w in json.loads(registry.read_text()) if w["name"] == "personal")

    assert config.workspace("personal").memory_handover == "off"
    listed = client.get("/api/workspaces").json()
    assert next(w for w in listed["workspaces"] if w["name"] == "personal")["memory_handover"] == "off"

    patched = client.patch("/api/workspaces/personal", json={"memory_handover": "dismiss"})
    assert patched.status_code == 200
    updated = next(w for w in patched.json()["workspaces"] if w["name"] == "personal")
    assert updated["memory_handover"] == "dismiss"
    assert stored_row()["memory_handover"] == "dismiss"

    # A save that does not mention it keeps it.
    kept = client.patch("/api/workspaces/personal", json={"color": "cyan"})
    assert kept.status_code == 200
    assert config.workspace("personal").memory_handover == "dismiss"
    assert stored_row()["memory_handover"] == "dismiss"

    # Back to off removes the key from the file.
    off = client.patch("/api/workspaces/personal", json={"memory_handover": "off"})
    assert off.status_code == 200
    assert "memory_handover" not in stored_row()

    bad = client.patch("/api/workspaces/personal", json={"memory_handover": "everything"})
    assert bad.status_code == 400
    assert config.workspace("personal").memory_handover == "off"


def test_workspace_provider_overrides_persist_validate_and_clear(tmp_path):
    """Overrides are operator-set per workspace and provider, sparse on disk, replaced whole."""
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"

    def stored_row() -> dict:
        return next(w for w in json.loads(registry.read_text()) if w["name"] == "personal")

    def listed_overrides() -> dict:
        listed = client.get("/api/workspaces").json()
        return next(w for w in listed["workspaces"] if w["name"] == "personal")["provider_overrides"]

    assert listed_overrides() == {}

    body = {"claude": {"model": "sonnet", "permission_mode": "manual"}}
    patched = client.patch("/api/workspaces/personal", json={"provider_overrides": body})
    assert patched.status_code == 200, patched.json()
    updated = next(w for w in patched.json()["workspaces"] if w["name"] == "personal")
    assert updated["provider_overrides"] == body
    assert stored_row()["provider_overrides"] == body
    assert config.workspace("personal").provider_overrides == body
    assert listed_overrides() == body

    # A save that omits the key keeps it.
    kept = client.patch("/api/workspaces/personal", json={"color": "cyan"})
    assert kept.status_code == 200
    assert config.workspace("personal").provider_overrides == body

    # A save that carries the key replaces the whole map; opencode takes any model id.
    replaced = client.patch(
        "/api/workspaces/personal",
        json={"provider_overrides": {"opencode": {"model": "vendor/x", "thinking": "high"}}},
    )
    assert replaced.status_code == 200
    assert stored_row()["provider_overrides"] == {
        "opencode": {"model": "vendor/x", "thinking": "high"}
    }

    for bad in (
        {"nope": {"model": "sonnet"}},
        {"claude": {"default_model": "sonnet"}},
        {"claude": {"permission_mode": "plan"}},
        {"claude": {"thinking": "extreme"}},
        [],
    ):
        refused = client.patch("/api/workspaces/personal", json={"provider_overrides": bad})
        assert refused.status_code == 400, bad
        assert stored_row()["provider_overrides"] == {
            "opencode": {"model": "vendor/x", "thinking": "high"}
        }
        assert config.workspace("personal").provider_overrides == {
            "opencode": {"model": "vendor/x", "thinking": "high"}
        }

    # {} clears it and the key leaves the file.
    cleared = client.patch("/api/workspaces/personal", json={"provider_overrides": {}})
    assert cleared.status_code == 200
    assert "provider_overrides" not in stored_row()
    assert listed_overrides() == {}

    created = client.post(
        "/api/workspaces",
        json={"name": "fresh", "provider_overrides": {"claude": {"thinking": "low"}}},
    )
    assert created.status_code in (200, 201), created.json()
    assert config.workspace("fresh").provider_overrides == {"claude": {"thinking": "low"}}


def _stored_workspace(tmp_path: Path, name: str) -> dict:
    stored = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    return next(w for w in stored if w["name"] == name)


def _reload_config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )


def test_a_stored_scope_is_ignored_and_dropped_on_save(tmp_path: Path) -> None:
    """A stored file-access scope is not a setting: load ignores it and the next save drops it."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps([{
            "name": "personal",
            "vault_root": "memory-vault/personal",
            "agent_fs_scope": "workspace",
        }]),
        encoding="utf-8",
    )
    client, _config, _pcm = _client(tmp_path)
    listed = client.get("/api/workspaces")
    assert listed.status_code == 200
    payload = listed.json()
    assert "default_agent_fs_scope" not in payload
    assert "agent_fs_scope" not in payload["workspaces"][0]

    color = client.patch("/api/workspaces/personal", json={"color": "cyan"})
    assert color.status_code == 200
    assert "agent_fs_scope" not in _stored_workspace(tmp_path, "personal")

    bogus = client.patch("/api/workspaces/personal", json={"agent_fs_scope": "bogus"})
    assert bogus.status_code == 200
    stored = _stored_workspace(tmp_path, "personal")
    assert "agent_fs_scope" not in stored
    assert stored.get("agent_fs_scope") is None


def test_workspace_color_saved_as_pink_stays_pink():
    """The default moved to amber; a workspace that stored pink keeps it."""
    from ciao.config import DEFAULT_WORKSPACE_COLOR, _workspace_from_mapping

    assert DEFAULT_WORKSPACE_COLOR == "amber"
    stored = _workspace_from_mapping({"name": "personal", "color": "pink"})
    unset = _workspace_from_mapping({"name": "work"})
    assert stored is not None and stored.color == "pink"
    assert unset is not None and unset.color == "amber"


# ── reorder ─────────────────────────────────────────────────────────────


class _ReorderEvents:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, payload: dict) -> None:
        self.published.append(payload)


def _reorder_client(tmp_path: Path, names=("a", "b", "c")):
    client, config, pcm = _client(tmp_path)
    for name in names:
        resp = client.post(
            "/api/workspaces", json={"name": name, "vault_root": name}
        )
        assert resp.status_code == 201, resp.json()
    pcm.events = _ReorderEvents()
    return client, config, pcm


def _registry_file(tmp_path: Path) -> Path:
    return tmp_path / ".runtime" / "workspaces.json"


def _stored_names(tmp_path: Path) -> list[str]:
    return [item["name"] for item in json.loads(_registry_file(tmp_path).read_text())]


def _payload_names(payload: dict) -> list[str]:
    return [item["name"] for item in payload["workspaces"]]


def test_reorder_workspaces_persists_and_returns_order(tmp_path):
    client, config, _pcm = _reorder_client(tmp_path)
    before = [name for name in config.workspaces if name in ("a", "b", "c")]
    assert before == ["a", "b", "c"]
    order = [name for name in config.workspaces if name not in ("a", "b", "c")]
    order += ["c", "a", "b"]

    resp = client.post("/api/workspaces/reorder", json={"order": order})

    assert resp.status_code == 200, resp.json()
    assert _payload_names(resp.json()) == order
    assert list(config.workspaces) == order
    assert _stored_names(tmp_path) == order
    reloaded = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )
    assert list(reloaded.workspaces) == order
    assert _payload_names(client.get("/api/workspaces").json()) == order


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda cur: cur[:-1], id="missing-name"),
        pytest.param(lambda cur: [*cur, "ghost"], id="unknown-name"),
        pytest.param(lambda cur: [*cur[:-1], cur[0]], id="duplicate"),
        pytest.param(lambda cur: "abc", id="not-a-list"),
        pytest.param(lambda cur: [*cur[:-1], 7], id="non-string-item"),
        pytest.param(lambda cur: [name.upper() for name in cur], id="case-sensitive"),
    ],
)
def test_reorder_workspaces_rejects_non_permutations(tmp_path, build):
    client, config, _pcm = _reorder_client(tmp_path)
    current = list(config.workspaces)
    stored = _registry_file(tmp_path).read_text()

    resp = client.post("/api/workspaces/reorder", json={"order": build(current)})

    assert resp.status_code == 400
    assert "error" in resp.json()
    assert list(config.workspaces) == current
    assert _registry_file(tmp_path).read_text() == stored


def test_reorder_workspaces_rejects_invalid_json_and_missing_order(tmp_path):
    client, config, _pcm = _reorder_client(tmp_path)
    current = list(config.workspaces)

    bad = client.post(
        "/api/workspaces/reorder",
        content=b"{nope",
        headers={"content-type": "application/json"},
    )
    assert bad.status_code == 400
    assert bad.json() == {"error": "invalid JSON"}
    for body in ({}, [], {"order": None}):
        resp = client.post("/api/workspaces/reorder", json=body)
        assert resp.status_code == 400
    assert list(config.workspaces) == current


def test_reorder_workspaces_rejects_an_archived_name(tmp_path):
    client, config, _pcm = _reorder_client(tmp_path)
    current = list(config.workspaces)
    # An archived workspace is not in the registry, so it is unknown here.
    resp = client.post("/api/workspaces/reorder", json={"order": [*current, "archived-one"]})
    assert resp.status_code == 400
    resp = client.post(
        "/api/workspaces/reorder", json={"order": [*current[:-1], "archived-one"]}
    )
    assert resp.status_code == 400
    assert list(config.workspaces) == current


def test_reorder_workspaces_identical_order_is_a_noop(tmp_path):
    client, config, pcm = _reorder_client(tmp_path)
    registry = _registry_file(tmp_path)
    before_mtime = registry.stat().st_mtime_ns
    before_text = registry.read_text()
    current = list(config.workspaces)
    refreshes = pcm.refresh_count

    resp = client.post("/api/workspaces/reorder", json={"order": current})

    assert resp.status_code == 200
    assert _payload_names(resp.json()) == current
    assert registry.stat().st_mtime_ns == before_mtime
    assert registry.read_text() == before_text
    assert pcm.refresh_count == refreshes
    assert pcm.events.published == []


def test_reorder_workspaces_rolls_back_when_persist_fails(tmp_path, monkeypatch):
    client, config, pcm = _reorder_client(tmp_path)
    current = list(config.workspaces)
    stored = _registry_file(tmp_path).read_text()

    def boom(_config, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ciao.web.routes_api.persist_workspaces", boom)

    resp = client.post("/api/workspaces/reorder", json={"order": list(reversed(current))})

    assert resp.status_code == 500
    assert "disk full" in resp.json()["error"]
    assert list(config.workspaces) == current
    assert _registry_file(tmp_path).read_text() == stored
    assert pcm.events.published == []


def test_reorder_workspaces_publishes_workspaces_changed(tmp_path):
    client, config, pcm = _reorder_client(tmp_path)
    resp = client.post(
        "/api/workspaces/reorder", json={"order": list(reversed(list(config.workspaces)))}
    )
    assert resp.status_code == 200
    assert pcm.events.published == [{"type": "workspaces_changed"}]


def test_rename_keeps_position_after_reorder(tmp_path):
    client, config, _pcm = _reorder_client(tmp_path)
    pcm = _RenamePCM()
    client.app.state.project_chat_manager = pcm  # type: ignore[attr-defined]
    order = ["c", *[n for n in config.workspaces if n != "c"]]
    assert client.post("/api/workspaces/reorder", json={"order": order}).status_code == 200

    resp = client.patch("/api/workspaces/c", json={"name": "z"})

    assert resp.status_code == 200, resp.json()
    expected = ["z", *order[1:]]
    assert list(config.workspaces) == expected
    assert _stored_names(tmp_path) == expected


def _no_personal_client(tmp_path: Path, names=("work", "clientA", "other")):
    """A registry with no workspace named ``personal``: the first is the main one."""
    from ciao.config import WorkspaceConfig

    client, config, pcm = _client(tmp_path)
    config.workspaces.clear()
    for name in names:
        config.workspaces[name] = WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
    config.persist_workspace_registry()
    pcm.events = _ReorderEvents()
    return client, config, pcm


def test_reorder_never_moves_the_main_workspace_off_the_top(tmp_path):
    client, config, pcm = _no_personal_client(tmp_path)
    assert config.primary_workspace() == "work"
    stored = _registry_file(tmp_path).read_text()
    refreshes = pcm.refresh_count

    resp = client.post(
        "/api/workspaces/reorder", json={"order": ["clientA", "work", "other"]}
    )

    assert resp.status_code == 409
    assert resp.json() == {"error": "'work' is the main workspace and stays first"}
    assert list(config.workspaces) == ["work", "clientA", "other"]
    assert config.primary_workspace() == "work"
    assert _registry_file(tmp_path).read_text() == stored
    assert pcm.refresh_count == refreshes
    assert pcm.events.published == []


def test_reorder_below_the_main_workspace_is_allowed(tmp_path):
    client, config, _pcm = _no_personal_client(tmp_path)

    resp = client.post(
        "/api/workspaces/reorder", json={"order": ["work", "other", "clientA"]}
    )

    assert resp.status_code == 200, resp.json()
    assert list(config.workspaces) == ["work", "other", "clientA"]
    assert _stored_names(tmp_path) == ["work", "other", "clientA"]
    assert resp.json()["primary"] == "work"


def test_reorder_with_a_personal_workspace_moves_anything_anywhere(tmp_path):
    client, config, _pcm = _no_personal_client(tmp_path, names=("work", "personal", "other"))
    assert config.primary_workspace() == "personal"

    for order in (
        ["personal", "work", "other"],
        ["other", "work", "personal"],
        ["work", "other", "personal"],
    ):
        resp = client.post("/api/workspaces/reorder", json={"order": order})
        assert resp.status_code == 200, resp.json()
        assert list(config.workspaces) == order
        assert resp.json()["primary"] == "personal"


def test_reorder_workspaces_waits_for_the_registry_lock(tmp_path):
    import asyncio

    import httpx

    client, config, _pcm = _reorder_client(tmp_path)
    app = client.app
    current = list(config.workspaces)
    wanted = list(reversed(current))

    async def scenario() -> None:
        lock = asyncio.Lock()
        app.state.workspace_archive_lock = lock  # type: ignore[attr-defined]
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            await lock.acquire()
            task = asyncio.create_task(
                http.post("/api/workspaces/reorder", json={"order": wanted})
            )
            await asyncio.sleep(0.05)
            assert not task.done()
            assert list(config.workspaces) == current
            lock.release()
            resp = await task
        assert resp.status_code == 200
        assert list(config.workspaces) == wanted

    asyncio.run(scenario())


def test_memory_approval_patch_merges_one_kind_and_keeps_the_other(tmp_path):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"
    assert client.post("/api/workspaces", json={"name": "research"}).status_code == 201

    first = client.patch("/api/workspaces/research", json={"memory_approval": {"profile": "auto"}})
    second = client.patch("/api/workspaces/research", json={"memory_approval": {"memory": "approve"}})
    assert first.status_code == second.status_code == 200, second.json()
    assert config.workspace("research").memory_approval == {"profile": "auto", "memory": "approve"}
    stored = {w["name"]: w for w in json.loads(registry.read_text())}
    assert stored["research"]["memory_approval"] == {"profile": "auto", "memory": "approve"}
    listed = client.get("/api/workspaces").json()
    row = next(w for w in listed["workspaces"] if w["name"] == "research")
    assert row["memory_approval"] == {"profile": "auto", "memory": "approve"}
    # No silent propagation to another workspace.
    assert config.workspace("personal").memory_approval == {}


def test_memory_approval_null_for_one_kind_resets_only_that_kind(tmp_path):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"
    client.post("/api/workspaces", json={"name": "research"})
    client.patch("/api/workspaces/research", json={"memory_approval": {"memory": "approve", "profile": "auto"}})

    reset = client.patch("/api/workspaces/research", json={"memory_approval": {"profile": None}})

    assert reset.status_code == 200, reset.json()
    assert config.workspace("research").memory_approval == {"memory": "approve"}
    stored = {w["name"]: w for w in json.loads(registry.read_text())}
    assert stored["research"]["memory_approval"] == {"memory": "approve"}


def test_a_patch_that_leaves_memory_approval_out_keeps_it(tmp_path):
    client, config, _pcm = _client(tmp_path)
    client.post("/api/workspaces", json={"name": "research"})
    client.patch("/api/workspaces/research", json={"memory_approval": {"memory": "auto"}})

    saved = client.patch("/api/workspaces/research", json={"color": "amber"})

    assert saved.status_code == 200, saved.json()
    assert config.workspace("research").memory_approval == {"memory": "auto"}


@pytest.mark.parametrize(
    "body",
    [
        {"memory_approval": None},  # whole-field null is refused; reset per kind
        {"memory_approval": "auto"},
        {"memory_approval": {"project": "auto"}},
        {"memory_approval": {"memory": "everything"}},
        {"memory_approval": {"memory": "approve", "profile": "Auto"}},  # one bad entry refuses all
    ],
)
def test_a_bad_memory_approval_refuses_the_whole_save(tmp_path, body):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"
    client.post("/api/workspaces", json={"name": "research"})
    client.patch("/api/workspaces/research", json={"memory_approval": {"profile": "auto"}})
    before_registry = registry.read_bytes()

    refused = client.patch("/api/workspaces/research", json={**body, "color": "amber"})

    assert refused.status_code == 400, refused.json()
    assert config.workspace("research").memory_approval == {"profile": "auto"}
    assert registry.read_bytes() == before_registry


def test_creating_a_workspace_with_a_kind_choice_stores_it_sparse(tmp_path):
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"

    created = client.post("/api/workspaces", json={"name": "research", "memory_approval": {"memory": "approve"}})

    assert created.status_code == 201, created.json()
    assert config.workspace("research").memory_approval == {"memory": "approve"}
    stored = {w["name"]: w for w in json.loads(registry.read_text())}
    assert stored["research"]["memory_approval"] == {"memory": "approve"}
    assert "memory_approval" not in stored["personal"]


def _away(tmp_path: Path) -> Path:
    """A folder outside the test install (its root is tmp_path), where a shared brain may live."""
    return tmp_path.parent / f"away-{tmp_path.name}"


def _checkout(path: Path, origin: str) -> Path:
    import subprocess

    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def test_workspace_shared_brains_default_empty_persist_validate_and_unlink(tmp_path, monkeypatch):
    """The list is operator-set per workspace, whole-list, stored sparsely and validated."""
    from ciao.web import routes_api  # noqa: F401  (route module loaded by the helper)
    from ciao import shared_brain

    scheduled: list[str] = []
    monkeypatch.setattr(shared_brain, "schedule_update", lambda link, config: scheduled.append(link.path))
    client, config, _pcm = _client(tmp_path)
    registry = tmp_path / ".runtime" / "workspaces.json"
    acme = _checkout(_away(tmp_path) / "acme", "https://github.com/acme/notes")
    other = _checkout(_away(tmp_path) / "other", "https://github.com/other/notes")

    assert config.workspace("personal").shared_brains == []
    listed = client.get("/api/workspaces").json()
    assert next(w for w in listed["workspaces"] if w["name"] == "personal")["shared_brains"] == []

    saved = client.patch(
        "/api/workspaces/personal",
        json={"shared_brains": [{"path": str(acme), "lifetime_id": "browser-forgery"}, {"path": str(other), "name": "team"}]},
    )
    assert saved.status_code == 200, saved.text
    rows = next(w for w in json.loads(registry.read_text()) if w["name"] == "personal")["shared_brains"]
    assert [(r["name"], r["identity"]) for r in rows] == [
        ("notes", "github.com/acme/notes"),
        ("team", "github.com/other/notes"),
    ]
    first_lifetime = rows[0]["lifetime_id"]
    assert first_lifetime != "browser-forgery"
    public_brains = next(w for w in saved.json()["workspaces"] if w["name"] == "personal")["shared_brains"]
    assert all("lifetime_id" not in link for link in public_brains)
    assert len(scheduled) == 2

    reloaded = _client(tmp_path)[1]
    assert [link.name for link in reloaded.workspace("personal").shared_brains] == ["notes", "team"]
    assert reloaded.workspace("personal").shared_brains[0].lifetime_id == first_lifetime

    original_persist = routes_api.persist_workspaces
    saved_registry = registry.read_text()
    def fail_persist(_config, *_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(routes_api, "persist_workspaces", fail_persist)
    with pytest.raises(OSError, match="disk full"):
        client.patch("/api/workspaces/personal", json={"shared_brains": []})
    assert config.workspace("personal").shared_brains[0].lifetime_id == first_lifetime
    assert registry.read_text() == saved_registry
    monkeypatch.setattr(routes_api, "persist_workspaces", original_persist)

    bad = client.patch(
        "/api/workspaces/personal",
        json={"shared_brains": [{"path": str(acme)}, {"path": str(tmp_path / "missing")}]},
    )
    assert bad.status_code == 400
    assert [link.name for link in config.workspace("personal").shared_brains] == ["notes", "team"]

    removed_one = client.patch("/api/workspaces/personal", json={"shared_brains": [{"path": str(other)}]})
    assert removed_one.status_code == 200
    assert [link.name for link in config.workspace("personal").shared_brains] == ["team"]
    assert acme.is_dir() and (acme / ".git").is_dir()

    cleared = client.patch("/api/workspaces/personal", json={"shared_brains": []})
    assert cleared.status_code == 200
    assert config.workspace("personal").shared_brains == []
    row = next(w for w in json.loads(registry.read_text()) if w["name"] == "personal")
    assert "shared_brains" not in row
    relinked = client.patch("/api/workspaces/personal", json={"shared_brains": [{"path": str(acme)}]})
    assert relinked.status_code == 200
    assert config.workspace("personal").shared_brains[0].lifetime_id != first_lifetime


def test_shared_brain_folder_paths_preserve_a_trailing_space(tmp_path):
    """Two existing siblings must stay distinct; trim must not link the wrong one."""
    client, config, _pcm = _client(tmp_path)
    parent = _away(tmp_path) / "trailing-space"
    plain = parent / "notes"
    spaced = parent / "notes "
    plain.mkdir(parents=True)
    spaced.mkdir()

    saved = client.patch(
        "/api/workspaces/personal",
        json={"shared_brains": [
            {"path": str(plain), "kind": "folder"},
            {"path": str(spaced), "kind": "folder"},
        ]},
    )
    assert saved.status_code == 200, saved.text
    assert [link.path for link in config.workspace("personal").shared_brains] == [str(plain), str(spaced)]

    states = client.get("/api/workspaces/personal/shared-brains").json()["shared_brains"]
    assert [state["path"] for state in states] == [str(plain), str(spaced)]
    assert {state["identity"] for state in states} == {f"folder:{plain}", f"folder:{spaced}"}
    assert plain.is_dir() and spaced.is_dir()


def test_saving_a_new_path_schedules_one_update(tmp_path, monkeypatch):
    from ciao import shared_brain

    scheduled: list[str] = []
    monkeypatch.setattr(shared_brain, "schedule_update", lambda link, config: scheduled.append(link.path))
    client, _config, _pcm = _client(tmp_path)
    acme = _checkout(_away(tmp_path) / "acme", "https://github.com/acme/notes")
    body = {"shared_brains": [{"path": str(acme)}]}

    assert client.patch("/api/workspaces/personal", json=body).status_code == 200
    assert scheduled == [str(acme.resolve())]
    assert client.patch("/api/workspaces/personal", json=body).status_code == 200
    assert scheduled == [str(acme.resolve())]
    assert client.patch("/api/workspaces/personal", json={"shared_brains": []}).status_code == 200
    assert scheduled == [str(acme.resolve())]


def test_workspace_save_retries_when_registry_changes_while_claims_wait(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from ciao import shared_brain
    from ciao.workspaces import workspace_registry_lock

    monkeypatch.setattr(shared_brain, "schedule_update", lambda link, config: None)
    client, config, _pcm = _client(tmp_path)
    acme = _checkout(_away(tmp_path) / "acme", "https://github.com/acme/notes")
    other = _checkout(_away(tmp_path) / "other", "https://github.com/other/notes")
    link_response = client.patch(
        "/api/workspaces/personal", json={"shared_brains": [{"path": str(acme)}]}
    )
    assert link_response.status_code == 200
    original_link = config.workspace("personal").shared_brains[0]
    original_claim = shared_brain._checkout_claim
    claim_paths: list[str] = []
    unlinked = False

    @asynccontextmanager
    async def interleaved_claim(path: str, claim_config):
        nonlocal unlinked
        claim_paths.append(path)
        if path == str(acme.resolve()) and not unlinked:
            # The other process owns the file claim first. Its process-local
            # asyncio lock is independent of the requesting server's lock.
            async with original_claim(path, claim_config):
                with workspace_registry_lock(claim_config):
                    current = claim_config.workspace("personal")
                    claim_config.workspaces["personal"] = replace(current, shared_brains=[])
                    claim_config.persist_workspace_registry()
            unlinked = True
        async with original_claim(path, claim_config):
            yield

    monkeypatch.setattr(shared_brain, "_checkout_claim", interleaved_claim)
    saved = client.patch(
        "/api/workspaces/personal",
        json={"shared_brains": [{"path": str(acme)}, {"path": str(other)}]},
    )

    assert saved.status_code == 200, saved.text
    links = config.workspace("personal").shared_brains
    assert {link.path for link in links} == {str(acme.resolve()), str(other.resolve())}
    refreshed_link = next(link for link in links if link.path == str(acme.resolve()))
    assert refreshed_link.lifetime_id != original_link.lifetime_id
    assert claim_paths.count(str(acme.resolve())) >= 1
    assert claim_paths.count(str(other.resolve())) >= 2
    registry = json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    saved_links = next(row for row in registry if row["name"] == "personal")["shared_brains"]
    assert next(link for link in saved_links if link["path"] == refreshed_link.path)["lifetime_id"] == refreshed_link.lifetime_id


def test_workspace_shared_brains_route_lists_states_empty_and_404(tmp_path, monkeypatch):
    from ciao import shared_brain

    monkeypatch.setattr(shared_brain, "schedule_update", lambda link, config: None)
    client, _config, _pcm = _client(tmp_path)
    empty = client.get("/api/workspaces/personal/shared-brains")
    assert empty.status_code == 200 and empty.json() == {"shared_brains": []}
    assert client.get("/api/workspaces/nope/shared-brains").status_code == 404

    acme = _checkout(_away(tmp_path) / "acme", "https://github.com/acme/notes")
    assert client.patch(
        "/api/workspaces/personal", json={"shared_brains": [{"path": str(acme)}]}
    ).status_code == 200
    states = client.get("/api/workspaces/personal/shared-brains").json()["shared_brains"]
    assert [s["name"] for s in states] == ["notes"]
    assert states[0]["identity"] == "github.com/acme/notes"
    assert states[0]["missing"] is False and states[0]["last_update"] is None



def _link_folder_in_peer(install, folder):
    client, _, _ = _client(install)
    response = client.patch("/api/workspaces/personal", json={
        "shared_brains": [{"name": "team", "kind": "folder", "path": str(folder)}],
    })
    assert response.status_code == 200, response.text


def test_git_validation_claims_canonical_path_before_checking_peer_folder(tmp_path, monkeypatch):
    import multiprocessing
    from contextlib import asynccontextmanager
    from ciao import repo_link, shared_brain

    stale, _, _ = _client(tmp_path)
    folder = _away(tmp_path)
    folder.mkdir()
    alias = tmp_path.parent / f"alias-{tmp_path.name}"
    alias.symlink_to(folder, target_is_directory=True)
    peer = multiprocessing.get_context("spawn").Process(target=_link_folder_in_peer, args=(tmp_path, folder))
    original_claim = shared_brain._checkout_claim
    claimed = []

    @asynccontextmanager
    async def after_peer_commit(path, config):
        claimed.append(path)
        if peer.pid is None:
            peer.start()
            peer.join(10)
            assert peer.exitcode == 0
        async with original_claim(path, config):
            yield

    def unexpected_git(*args, **kwargs):
        pytest.fail("Git validation ran on an ordinary folder")

    monkeypatch.setattr(shared_brain, "_checkout_claim", after_peer_commit)
    for module, seam in ((repo_link, "_git"), (shared_brain, "validate_repo_path"),
                         (shared_brain, "_link_origin"), (shared_brain, "_git"),
                         (shared_brain, "_git_async"), (shared_brain, "run_git")):
        monkeypatch.setattr(module, seam, unexpected_git)

    payload = {"shared_brains": [{"path": str(alias)}]}
    response = stale.patch("/api/workspaces/work", json=payload)
    assert response.status_code == 409, response.text
    current, _, _ = _client(tmp_path)
    refused = current.patch("/api/workspaces/work", json=payload)
    assert refused.status_code == 400, refused.text
    assert claimed == [str(folder.resolve())] * 2


def test_git_discovery_runs_under_checkout_and_registry_ownership(tmp_path, monkeypatch):
    from ciao import repo_link, shared_brain
    from ciao.workspaces import WorkspaceRegistryBusy, acquire_workspace_registry

    client, config, _ = _client(tmp_path)
    checkout = _checkout(_away(tmp_path) / "git", "https://github.com/team/notes")
    original = repo_link._git
    observed = []

    def discovery(path, *args):
        observed.append(path)
        with pytest.raises(WorkspaceRegistryBusy):
            with shared_brain.checkout_claims_nowait([str(path)], config):
                pytest.fail("checkout claim was free at Git discovery")
        with pytest.raises(WorkspaceRegistryBusy):
            with acquire_workspace_registry(config):
                pytest.fail("registry claim was free at Git discovery")
        return original(path, *args)

    monkeypatch.setattr(repo_link, "_git", discovery)
    response = client.patch("/api/workspaces/personal", json={"shared_brains": [{"path": str(checkout)}]})
    assert response.status_code == 200, response.text
    assert observed == [checkout.resolve()]


@pytest.mark.parametrize("surface", ["settings", "preview"])
def test_status_refuses_git_after_independent_folder_relink(tmp_path, monkeypatch, surface):
    import concurrent.futures
    import threading
    from contextlib import contextmanager

    from ciao import repo_link, share_proposals, shared_brain
    from ciao.web import proposal_service

    reader, stale, _ = _client(tmp_path)
    checkout = _checkout(_away(tmp_path) / "git", "https://github.com/team/notes")
    monkeypatch.setattr(shared_brain, "schedule_update", lambda *_: None)
    settings = "/api/workspaces/personal"
    assert reader.patch(settings, json={"shared_brains": [{"path": str(checkout)}]}).status_code == 200
    link = stale.workspace("personal").shared_brains[0]
    peer, _, _ = _client(tmp_path)
    waiting, proceed = threading.Event(), threading.Event()
    original = shared_brain.checkout_claims_nowait

    @contextmanager
    def interleave(paths, config):
        waiting.set()
        assert proceed.wait(5)
        with original(paths, config):
            yield

    monkeypatch.setattr(shared_brain, "checkout_claims_nowait", interleave)
    monkeypatch.setattr(share_proposals, "load_share", lambda *args, **kwargs: {
        "immutable": {"brain": link.name, "link_lifetime_id": link.lifetime_id},
        "review_token": "revision",
    })

    def unexpected(*args, **kwargs):
        pytest.fail("Git ran after the checkout was relinked as a folder")

    for module, seam in ((repo_link, "_git"), (shared_brain, "validate_repo_path"),
                         (shared_brain, "_link_origin"), (shared_brain, "_git"),
                         (shared_brain, "_git_async"), (shared_brain, "run_git"),
                         (shared_brain, "fetched_at")):
        monkeypatch.setattr(module, seam, unexpected)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        if surface == "settings":
            future = pool.submit(reader.get, settings + "/shared-brains")
        else:
            future = pool.submit(proposal_service.preview_row, stale, {
                "workspace": "personal", "row": {"kind": "share", "id": "draft"},
            })
        try:
            assert waiting.wait(5)
            assert peer.patch(settings, json={"shared_brains": []}).status_code == 200
            assert peer.patch(settings, json={"shared_brains": [
                {"path": str(checkout), "kind": "folder"}
            ]}).status_code == 200
        finally:
            proceed.set()
        result = future.result(timeout=5)
    if surface == "settings":
        assert result.status_code == 200
        assert result.json()["shared_brains"][0]["problem"] == "registry_unavailable"
    else:
        assert result["blocked_reason"] == "brain_unavailable"


def test_status_holds_checkout_and_registry_during_git(tmp_path, monkeypatch):
    from ciao import shared_brain
    from ciao.workspaces import WorkspaceRegistryBusy, workspace_registry_claim

    client, _, _ = _client(tmp_path)
    checkout = _checkout(_away(tmp_path) / "git", "https://github.com/team/notes")
    monkeypatch.setattr(shared_brain, "schedule_update", lambda *_: None)
    url = "/api/workspaces/personal"
    assert client.patch(url, json={"shared_brains": [{"path": str(checkout)}]}).status_code == 200
    _, peer, _ = _client(tmp_path)
    git = shared_brain._git
    observed = []

    def check_claims(path, *args):
        with pytest.raises(WorkspaceRegistryBusy), shared_brain.checkout_claims_nowait([path], peer):
            pytest.fail("checkout was free during status")
        with pytest.raises(WorkspaceRegistryBusy), workspace_registry_claim(peer):
            pytest.fail("registry was free during status")
        observed.append(args)
        return git(path, *args)

    monkeypatch.setattr(shared_brain, "_git", check_claims)
    response = client.get(url + "/shared-brains")
    assert response.status_code == 200
    assert response.json()["shared_brains"][0]["problem"] == ""
    assert observed
