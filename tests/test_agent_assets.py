from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.os_support.links import is_link, link_source
from ciao.web.agent_assets import (
    agent_assets_endpoint,
    create_command_endpoint,
    create_subagent_endpoint,
    delete_command_endpoint,
    delete_subagent_endpoint,
    os_audit_endpoint,
    update_command_endpoint,
    update_subagent_endpoint,
    workspace_health,
    workspace_health_endpoint,
    workspace_health_fix_endpoint,
)
from tests.test_sync_skills import _dangling_dir_link


TEST_WORKSPACE = "personal"


def _config(root: Path, *, state_path: Path | None = None) -> SimpleNamespace:
    vault = root / "memory-vault"
    vault.mkdir(parents=True, exist_ok=True)
    state_path = state_path or (root / ".runtime" / "state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # A write must name a registered workspace, so the fixture registers one
    # that resolves to the install root — the pre-re-rooting shape, where
    # `agent_root(name)` is the root itself.
    registry = {
        TEST_WORKSPACE: SimpleNamespace(
            name=TEST_WORKSPACE, vault_root=str(vault), default_provider="claude"
        )
    }
    return SimpleNamespace(
        workspace_root=root,
        vault_root=vault,
        state_path=state_path,
        memory_char_limit=2200,
        user_char_limit=1375,
        workspaces=registry,
        workspace=lambda name: registry.get(str(name)),
        agent_root=lambda name: root,
        agent_vault_root=lambda name: vault,
        primary_workspace=lambda: TEST_WORKSPACE,
    )


def _client(root: Path, *, state_path: Path | None = None) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/agent-assets", agent_assets_endpoint, methods=["GET"]),
            Route("/api/agent-assets/audit", os_audit_endpoint, methods=["GET"]),
            Route("/api/workspace-health", workspace_health_endpoint, methods=["GET"]),
            Route("/api/workspace-health/fix", workspace_health_fix_endpoint, methods=["POST"]),
            Route("/api/agent-assets/subagents", create_subagent_endpoint, methods=["POST"]),
            Route("/api/agent-assets/subagents/{name}", update_subagent_endpoint, methods=["PATCH"]),
            Route("/api/agent-assets/subagents/{name}", delete_subagent_endpoint, methods=["DELETE"]),
            Route("/api/agent-assets/commands", create_command_endpoint, methods=["POST"]),
            Route("/api/agent-assets/commands/{name}", update_command_endpoint, methods=["PATCH"]),
            Route("/api/agent-assets/commands/{name}", delete_command_endpoint, methods=["DELETE"]),
        ]
    )
    app.state.config = _config(root, state_path=state_path)
    return TestClient(app)


def test_os_audit_endpoint_uses_configured_runtime_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "AGENTS.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    bounded = tmp_path / "bounded"
    bounded.mkdir()
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(bounded))
    runtime = tmp_path / "custom-runtime"
    runtime.mkdir()
    (runtime / "job_runs_latest.json").write_text(
        """{
  "broken_job": {
    "job": "broken_job",
    "status": "error",
    "error": "boom",
    "started_at": "2026-07-26T10:00:00+00:00",
    "ended_at": "2026-07-26T10:01:00+00:00"
  }
}
""",
        encoding="utf-8",
    )

    response = _client(
        tmp_path,
        state_path=runtime / "state.json",
    ).get("/api/agent-assets/audit")

    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "needs_attention"
    assert report["job_runs_audit"]["failed_runs"] == 1
    assert report["job_runs_audit"]["recent_failures"][0]["job"] == "broken_job"


def test_create_subagent_writes_canonical_file_vault_mirror_and_claude_link(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/api/agent-assets/subagents",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "Doc Helper",
            "description": "Maintain docs after code changes.",
            "prompt": "Read changed files and update the relevant docs.",
        },
    )

    assert resp.status_code == 201
    target = tmp_path / "subagents" / "doc-helper.md"
    mirror = tmp_path / "memory-vault" / "Workspace" / "Subagents" / "doc-helper.md"
    link = tmp_path / ".claude" / "agents" / "doc-helper.md"
    assert target.read_text(encoding="utf-8").startswith("---\nname: doc-helper\n")
    assert "canonical_path: subagents/doc-helper.md" in mirror.read_text(encoding="utf-8")
    assert is_link(link)
    assert link_source(link) == target.resolve()


def test_update_and_delete_custom_subagent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    create = client.post(
        "/api/agent-assets/subagents",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "doc-helper",
            "description": "Maintain docs.",
            "prompt": "Old instructions.",
        },
    )
    assert create.status_code == 201

    update = client.patch(
        "/api/agent-assets/subagents/doc-helper",
        json={
            "workspace": TEST_WORKSPACE,
            "description": "Maintain docs after code changes.",
            "content": "# Doc Helper\n\nNew instructions.",
        },
    )

    assert update.status_code == 200
    target = tmp_path / "subagents" / "doc-helper.md"
    assert "New instructions." in target.read_text(encoding="utf-8")
    assert "Maintain docs after code changes." in target.read_text(encoding="utf-8")
    mirror = tmp_path / "memory-vault" / "Workspace" / "Subagents" / "doc-helper.md"
    assert "New instructions." in mirror.read_text(encoding="utf-8")

    delete = client.delete(
        f"/api/agent-assets/subagents/doc-helper?workspace={TEST_WORKSPACE}"
    )

    assert delete.status_code == 200
    assert not target.exists()
    assert not mirror.exists()
    assert not (tmp_path / ".claude" / "agents" / "doc-helper.md").exists()


def test_create_command_writes_canonical_file_vault_mirror_and_claude_link(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/api/agent-assets/commands",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "Summarize Decision",
            "description": "Summarize a decision into the vault.",
            "argument_hint": "<decision notes>",
            "prompt": "Turn $ARGUMENTS into a concise decision record.",
        },
    )

    assert resp.status_code == 201
    target = tmp_path / "commands" / "summarize-decision.md"
    mirror = tmp_path / "memory-vault" / "Workspace" / "Commands" / "summarize-decision.md"
    link = tmp_path / ".claude" / "commands" / "summarize-decision.md"
    text = target.read_text(encoding="utf-8")
    assert "description: Summarize a decision into the vault." in text
    assert "argument-hint: <decision notes>" in text
    assert "canonical_path: commands/summarize-decision.md" in mirror.read_text(encoding="utf-8")
    assert is_link(link)
    assert link_source(link) == target.resolve()


def test_update_and_delete_custom_command(tmp_path: Path) -> None:
    client = _client(tmp_path)
    create = client.post(
        "/api/agent-assets/commands",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "summarize-decision",
            "description": "Summarize a decision.",
            "argument_hint": "<notes>",
            "prompt": "Old prompt.",
        },
    )
    assert create.status_code == 201

    update = client.patch(
        "/api/agent-assets/commands/summarize-decision",
        json={
            "workspace": TEST_WORKSPACE,
            "description": "Summarize a decision into the vault.",
            "argument_hint": "<decision notes>",
            "content": "# Summarize Decision: $ARGUMENTS\n\nNew prompt.",
        },
    )

    assert update.status_code == 200
    target = tmp_path / "commands" / "summarize-decision.md"
    text = target.read_text(encoding="utf-8")
    assert "argument-hint: <decision notes>" in text
    assert "New prompt." in text
    mirror = tmp_path / "memory-vault" / "Workspace" / "Commands" / "summarize-decision.md"
    assert "New prompt." in mirror.read_text(encoding="utf-8")

    delete = client.delete(
        f"/api/agent-assets/commands/summarize-decision?workspace={TEST_WORKSPACE}"
    )

    assert delete.status_code == 200
    assert not target.exists()
    assert not mirror.exists()
    assert not (tmp_path / ".claude" / "commands" / "summarize-decision.md").exists()


def test_agent_assets_labels_unmodified_stock_command_as_built_in(tmp_path: Path) -> None:
    from importlib import resources

    stock_text = (
        resources.files("ciao.stock").joinpath("commands", "remember.md").read_text(encoding="utf-8")
    )
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "remember.md").write_text(stock_text, encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    remember = next(c for c in resp.json()["commands"] if c["name"] == "remember")
    assert remember["scope"] == "built-in"
    assert remember["source"] == "ciaobot"
    assert remember["editable"] is True


def test_agent_assets_labels_edited_stock_command_as_custom(tmp_path: Path) -> None:
    from importlib import resources

    stock_text = (
        resources.files("ciao.stock").joinpath("commands", "remember.md").read_text(encoding="utf-8")
    )
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir()
    (commands_dir / "remember.md").write_text(stock_text + "\nExtra local instruction.\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    remember = next(c for c in resp.json()["commands"] if c["name"] == "remember")
    assert remember["scope"] == "custom"
    assert remember["source"] == "workspace"
    assert remember["editable"] is True


def test_create_subagent_rejects_installed_name_collision(tmp_path: Path) -> None:
    installed = tmp_path / ".claude" / "agents" / "researcher.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("# System researcher\n", encoding="utf-8")

    resp = _client(tmp_path).post(
        "/api/agent-assets/subagents",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "researcher",
            "description": "Replacement.",
            "prompt": "Do something else.",
        },
    )

    assert resp.status_code == 409
    assert "conflicts" in resp.json()["error"]


def test_create_command_rejects_installed_name_collision(tmp_path: Path) -> None:
    installed = tmp_path / ".claude" / "commands" / "remember.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("# System remember\n", encoding="utf-8")

    resp = _client(tmp_path).post(
        "/api/agent-assets/commands",
        json={
            "workspace": TEST_WORKSPACE,
            "name": "remember",
            "description": "Replacement.",
            "prompt": "Do something else.",
        },
    )

    assert resp.status_code == 409
    assert "conflicts" in resp.json()["error"]


def test_workspace_health_reports_unsynced_custom_assets(tmp_path: Path) -> None:
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "orphan.md").write_text("# Orphan\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/workspace-health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in {"warn", "error"}
    assert any(check["id"] == "unsynced-subagent-orphan" for check in data["checks"])


def test_workspace_health_reports_the_workspace_guide(tmp_path: Path) -> None:
    """One guide, and no link to check. The `guides-linked` check is gone with
    the symlink it described (ciao/workspace_guide.py)."""
    (tmp_path / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")

    data = _client(tmp_path).get("/api/workspace-health").json()

    assert not any(c["id"] == "guides-linked" for c in data["checks"])
    assert not any(c["id"] == "legacy-guide" for c in data["checks"])
    guide = next(c for c in data["checks"] if c["path"].endswith("AGENTS.md"))
    assert guide["status"] == "ok"


def test_workspace_health_warns_about_a_surviving_legacy_guide(tmp_path: Path) -> None:
    """A CLAUDE.md beside the guide means Claude Code reads it instead of
    AGENTS.md, so the rename has not taken effect for this root."""
    (tmp_path / "AGENTS.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Legacy guide\n", encoding="utf-8")

    data = _client(tmp_path).get("/api/workspace-health").json()

    check = next(c for c in data["checks"] if c["id"] == "legacy-guide")
    assert check["status"] == "warn"
    assert "reads it instead of" in check["detail"]
    # Renaming, never copying: the bounded memory regions live in that file.
    assert "never copy it" in check["action"]


def test_workspace_health_fix_applies_the_suggested_remedies(tmp_path: Path) -> None:
    """The Fix button covers what the checks suggest in prose: missing
    scaffold files are created and custom assets get linked into .claude."""
    (tmp_path / "subagents").mkdir()
    (tmp_path / "subagents" / "orphan.md").write_text("# Orphan\n", encoding="utf-8")
    client = _client(tmp_path)

    before = client.get("/api/workspace-health").json()
    assert before["status"] in {"warn", "error"}

    resp = client.post("/api/workspace-health/fix")
    assert resp.status_code == 200
    after = resp.json()

    # The remedies were applied...
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "memory-vault" / "MEMORY.md").is_file()
    assert is_link(tmp_path / ".claude" / "agents" / "orphan.md")
    # ...and the endpoint returns the fresh (now clean) report.
    assert after["status"] == "ok"
    assert not any(c["status"] != "ok" for c in after["checks"])


def test_workspace_health_ignores_broken_agents_skills_links(tmp_path: Path) -> None:
    """`.agents/skills` is unmanaged since the Codex removal: sync neither
    writes nor prunes it, so a broken link there must not surface as an
    error with a "Run sync-skills" remedy sync cannot honor."""
    agents_skills = tmp_path / ".agents" / "skills"
    agents_skills.mkdir(parents=True)
    _dangling_dir_link(tmp_path / "skills" / "gone", agents_skills / "stale", relative_to=agents_skills)

    data = workspace_health(_config(tmp_path))

    assert not any(
        check["id"].startswith("broken-provider skill-") for check in data["checks"]
    )


def test_workspace_health_still_reports_broken_claude_skill_links(tmp_path: Path) -> None:
    claude_skills = tmp_path / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    _dangling_dir_link(tmp_path / "skills" / "gone", claude_skills / "stale", relative_to=claude_skills)

    data = workspace_health(_config(tmp_path))

    assert any(check["id"] == "broken-skill-stale" for check in data["checks"])


def _rerooted_client(root: Path) -> TestClient:
    """A client on a re-rooted install: one real agent root per workspace.

    `agent_root` returns a per-workspace SUBDIRECTORY here, which is the only
    shape where the install root and a workspace root are different places.
    Without it a test cannot tell "scoped to the workspace" apart from "always
    wrote to the install root".
    """
    roots = {name: root / name for name in ("personal", "work")}
    for path in roots.values():
        (path / "memory-vault" / "Workspace").mkdir(parents=True)
    config = SimpleNamespace(
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=root / ".runtime" / "state.json",
        memory_char_limit=2200,
        user_char_limit=1375,
        workspaces={
            name: SimpleNamespace(name=name, vault_root=str(path / "memory-vault"))
            for name, path in roots.items()
        },
        workspace=lambda name: {
            n: w for n, w in zip(roots, [
                SimpleNamespace(name=n, vault_root=str(roots[n] / "memory-vault"))
                for n in roots
            ])
        }.get(str(name)),
        agent_root=lambda name: roots[name],
        agent_vault_root=lambda name: roots[name] / "memory-vault",
        primary_workspace=lambda: "personal",
    )
    app = Starlette(
        routes=[
            Route("/api/agent-assets", agent_assets_endpoint, methods=["GET"]),
            Route("/api/agent-assets/subagents", create_subagent_endpoint, methods=["POST"]),
            Route("/api/agent-assets/subagents/{name}", update_subagent_endpoint, methods=["PATCH"]),
            Route("/api/agent-assets/subagents/{name}", delete_subagent_endpoint, methods=["DELETE"]),
            Route("/api/agent-assets/commands", create_command_endpoint, methods=["POST"]),
            Route("/api/agent-assets/commands/{name}", update_command_endpoint, methods=["PATCH"]),
            Route("/api/agent-assets/commands/{name}", delete_command_endpoint, methods=["DELETE"]),
        ]
    )
    app.state.config = config
    return TestClient(app)


def test_asset_lists_are_scoped_to_the_named_workspace(tmp_path: Path) -> None:
    """A workspace's tab lists that workspace's own assets.

    Two workspaces each own a command of the same name. Scoping the read is
    what makes the write safe: an edit reaches the file the row came from.
    """
    client = _rerooted_client(tmp_path)
    for workspace in ("personal", "work"):
        assert client.post(
            "/api/agent-assets/commands",
            json={
                "workspace": workspace,
                "name": "brief",
                "description": f"{workspace} brief",
                "prompt": "Write it.",
            },
        ).status_code == 201

    personal = client.get("/api/agent-assets?workspace=personal").json()
    work = client.get("/api/agent-assets?workspace=work").json()

    assert [c["description"] for c in personal["commands"] if c["name"] == "brief"] == [
        "personal brief"
    ]
    assert [c["description"] for c in work["commands"] if c["name"] == "brief"] == [
        "work brief"
    ]


def test_creating_an_asset_writes_into_the_named_workspaces_root(tmp_path: Path) -> None:
    """The file is created under the workspace, not the install root."""
    client = _rerooted_client(tmp_path)

    resp = client.post(
        "/api/agent-assets/commands",
        json={
            "workspace": "work",
            "name": "brief",
            "description": "Work brief.",
            "prompt": "Write it.",
        },
    )

    assert resp.status_code == 201
    assert (tmp_path / "work" / "commands" / "brief.md").is_file()
    assert not (tmp_path / "personal" / "commands").exists()
    # And the other workspace's list does not show it.
    personal = client.get("/api/agent-assets?workspace=personal").json()
    assert "brief" not in [c["name"] for c in personal["commands"]]


def test_a_write_naming_an_unknown_workspace_is_refused(tmp_path: Path) -> None:
    """An unregistered name is a 400, never a silent redirect.

    Falling back to the install root would file the asset somewhere the user
    did not ask for, and a delete would remove a different workspace's file.
    """
    client = _rerooted_client(tmp_path)

    created = client.post(
        "/api/agent-assets/commands",
        json={
            "workspace": "ghost",
            "name": "brief",
            "description": "Nowhere.",
            "prompt": "Write it.",
        },
    )
    deleted = client.delete("/api/agent-assets/commands/brief?workspace=ghost")

    assert created.status_code == 400
    assert deleted.status_code == 400
    assert not (tmp_path / "commands").exists()
    assert not (tmp_path / "ghost").exists()


def test_updating_an_asset_reaches_the_workspaces_own_copy(tmp_path: Path) -> None:
    """An edit updates the file behind the row, not the install root's."""
    client = _rerooted_client(tmp_path)
    client.post(
        "/api/agent-assets/commands",
        json={
            "workspace": "work",
            "name": "brief",
            "description": "Old.",
            "prompt": "Old prompt.",
        },
    )

    resp = client.patch(
        "/api/agent-assets/commands/brief",
        json={"workspace": "work", "description": "New.", "content": "New prompt."},
    )

    assert resp.status_code == 200
    assert "New prompt." in (tmp_path / "work" / "commands" / "brief.md").read_text(
        encoding="utf-8"
    )


def test_deleting_an_asset_removes_only_that_workspaces_copy(tmp_path: Path) -> None:
    client = _rerooted_client(tmp_path)
    for workspace in ("personal", "work"):
        client.post(
            "/api/agent-assets/commands",
            json={
                "workspace": workspace,
                "name": "brief",
                "description": f"{workspace} brief",
                "prompt": "Write it.",
            },
        )

    resp = client.delete("/api/agent-assets/commands/brief?workspace=work")

    assert resp.status_code == 200
    assert not (tmp_path / "work" / "commands" / "brief.md").exists()
    assert (tmp_path / "personal" / "commands" / "brief.md").is_file()
