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
    update_command_endpoint,
    update_subagent_endpoint,
    workspace_health_endpoint,
    workspace_health_fix_endpoint,
)


def _config(root: Path) -> SimpleNamespace:
    vault = root / "memory-vault"
    vault.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        workspace_root=root,
        vault_root=vault,
        memory_enabled=False,
        memory_char_limit=2200,
        user_char_limit=1800,
    )


def _client(root: Path) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/agent-assets", agent_assets_endpoint, methods=["GET"]),
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
    app.state.config = _config(root)
    return TestClient(app)


def test_agent_assets_lists_instruction_sources(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Local instructions\n", encoding="utf-8")
    (tmp_path / "memory-vault").mkdir()
    (tmp_path / "memory-vault" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    data = resp.json()
    titles = {item["title"] for item in data["context"]}
    assert "Claude Code project instructions" in titles
    assert "Workspace memory" in titles
    assert "Ciaobot system prompt append" in titles
    assert "Agent memory" in titles
    assert "User profile" in titles
    assert "Per-turn runtime context hook" in titles


def test_agent_assets_lists_codex_global_and_project_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("# Global Codex\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Project Codex\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    by_title = {item["title"]: item for item in resp.json()["context"]}
    assert by_title["Codex global instructions"]["content"] == "# Global Codex\n"
    assert by_title["Codex project instructions"]["content"] == "# Project Codex\n"


def test_agent_assets_respects_codex_override_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("# Global default\n", encoding="utf-8")
    (codex_home / "AGENTS.override.md").write_text("# Global override\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Project default\n", encoding="utf-8")
    (tmp_path / "AGENTS.override.md").write_text("# Project override\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    codex = [item for item in resp.json()["context"] if item["title"].startswith("Codex ")]
    assert {item["content"] for item in codex} == {"# Global override\n", "# Project override\n"}


def test_agent_assets_lists_bounded_memory_and_splits_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mem_dir = tmp_path / ".ciao"
    mem_dir.mkdir()
    (mem_dir / "memory.md").write_text("prefers bullet lists\n", encoding="utf-8")
    (mem_dir / "user.md").write_text("name: Alice\n", encoding="utf-8")
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(mem_dir))
    (tmp_path / "CLAUDE.md").write_text("# Local\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    by_id = {item["id"]: item for item in resp.json()["context"]}
    assert by_id["ciaobot-memory"]["content"] == "prefers bullet lists\n"
    assert by_id["ciaobot-user"]["content"] == "name: Alice\n"
    assert "prefers bullet lists" not in by_id["ciaobot-system-prompt"]["content"]
    assert "Ciaobot System Instructions" in by_id["ciaobot-system-prompt"]["content"]


def test_agent_assets_lists_memory_proposals(tmp_path: Path) -> None:
    proposals = tmp_path / "memory-vault" / "personal" / "Workspace" / "Memory-Proposals.md"
    proposals.parent.mkdir(parents=True)
    proposals.write_text(
        "# Memory Proposals\n\n- [memory] prefers async reviews  _(from: Decisions)_\n",
        encoding="utf-8",
    )
    (tmp_path / "CLAUDE.md").write_text("# Local\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    review = [item for item in resp.json()["context"] if item["scope"] == "review"]
    assert len(review) == 1
    assert review[0]["title"] == "Memory proposals"
    assert "1 pending proposal" in review[0]["description"]


def test_agent_assets_lists_instruction_import_children(tmp_path: Path) -> None:
    (tmp_path / "extra.md").write_text("# Extra\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Local\n\n@extra.md\n@missing.md\n", encoding="utf-8")

    resp = _client(tmp_path).get("/api/agent-assets")

    assert resp.status_code == 200
    imports = [item for item in resp.json()["context"] if item["source"] == "file-import"]
    paths = {item["path"] for item in imports}
    assert {"extra.md", "missing.md"}.issubset(paths)
    assert any(item["status"] == "ok" and item["parent_id"].startswith("file:") for item in imports)
    assert any(item["status"] == "missing" for item in imports)


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
