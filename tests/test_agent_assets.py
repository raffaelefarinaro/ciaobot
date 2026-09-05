from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

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


def _config(root: Path, *, state_path: Path | None = None) -> SimpleNamespace:
    vault = root / "memory-vault"
    vault.mkdir(parents=True, exist_ok=True)
    state_path = state_path or (root / ".runtime" / "state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        workspace_root=root,
        vault_root=vault,
        state_path=state_path,
        memory_char_limit=2200,
        user_char_limit=1375,
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
    (tmp_path / "CLAUDE.md").write_text("- Use rtk for shell commands.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
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
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_update_and_delete_custom_subagent(tmp_path: Path) -> None:
    client = _client(tmp_path)
    create = client.post(
        "/api/agent-assets/subagents",
        json={
            "name": "doc-helper",
            "description": "Maintain docs.",
            "prompt": "Old instructions.",
        },
    )
    assert create.status_code == 201

    update = client.patch(
        "/api/agent-assets/subagents/doc-helper",
        json={
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

    delete = client.delete("/api/agent-assets/subagents/doc-helper")

    assert delete.status_code == 200
    assert not target.exists()
    assert not mirror.exists()
    assert not (tmp_path / ".claude" / "agents" / "doc-helper.md").exists()


def test_create_command_writes_canonical_file_vault_mirror_and_claude_link(tmp_path: Path) -> None:
    resp = _client(tmp_path).post(
        "/api/agent-assets/commands",
        json={
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
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_update_and_delete_custom_command(tmp_path: Path) -> None:
    client = _client(tmp_path)
    create = client.post(
        "/api/agent-assets/commands",
        json={
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

    delete = client.delete("/api/agent-assets/commands/summarize-decision")

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


def test_workspace_health_reports_linked_workspace_guides(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")

    data = _client(tmp_path).get("/api/workspace-health").json()

    check = next(c for c in data["checks"] if c["id"] == "guides-linked")
    assert check["status"] == "ok"
    assert check["path"] == "AGENTS.md"


def test_workspace_health_warns_when_workspace_guides_diverge(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Custom runtime guide\n", encoding="utf-8")

    data = _client(tmp_path).get("/api/workspace-health").json()

    check = next(c for c in data["checks"] if c["id"] == "guides-linked")
    assert check["status"] == "warn"
    assert "different workspace instructions" in check["detail"]
    assert "sync-skills" in check["action"]


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
    assert (tmp_path / "CLAUDE.md").is_file()
    assert (tmp_path / "memory-vault" / "MEMORY.md").is_file()
    assert (tmp_path / ".claude" / "agents" / "orphan.md").is_symlink()
    # ...and the endpoint returns the fresh (now clean) report.
    assert after["status"] == "ok"
    assert not any(c["status"] != "ok" for c in after["checks"])


def test_workspace_health_ignores_broken_agents_skills_links(tmp_path: Path) -> None:
    """`.agents/skills` is unmanaged since the Codex removal: sync neither
    writes nor prunes it, so a broken link there must not surface as an
    error with a "Run sync-skills" remedy sync cannot honor."""
    agents_skills = tmp_path / ".agents" / "skills"
    agents_skills.mkdir(parents=True)
    (agents_skills / "stale").symlink_to("../../skills/gone")

    data = workspace_health(_config(tmp_path))

    assert not any(
        check["id"].startswith("broken-provider skill-") for check in data["checks"]
    )


def test_workspace_health_still_reports_broken_claude_skill_links(tmp_path: Path) -> None:
    claude_skills = tmp_path / ".claude" / "skills"
    claude_skills.mkdir(parents=True)
    (claude_skills / "stale").symlink_to("../../skills/gone")

    data = workspace_health(_config(tmp_path))

    assert any(check["id"] == "broken-skill-stale" for check in data["checks"])
