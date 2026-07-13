"""Route-level tests for the workspace git-sync flow: status reports the
current branch (or that the workspace isn't a git repo), and the merge
endpoint opens a chat with the conflict prompt carrying the branch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.local_session import LocalSessionManager
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import (
    handover_merge,
    local_handback,
    local_preflight,
    local_resync,
    local_status,
    list_workspaces,
)

_ORIGIN = "https://ciao.example"


def _routes():
    return [
        Route("/api/local/status", local_status, methods=["GET"]),
        Route("/api/local/preflight", local_preflight, methods=["GET"]),
        Route("/api/local/handback", local_handback, methods=["POST"]),
        Route("/api/local/resync", local_resync, methods=["POST"]),
        Route("/api/handover/merge", handover_merge, methods=["POST"]),
        Route("/api/workspaces", list_workspaces, methods=["GET"]),
    ]


def _git_init(repo: Path, *, branch: str = "main") -> None:
    """Turn ``repo`` into a git checkout on ``branch`` with one commit."""
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
    }

    def run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(repo), check=True, capture_output=True, env=env
        )

    run("init", "-q", "-b", branch)
    run("config", "user.name", "T")
    run("config", "user.email", "t@e.com")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-q", "-m", "seed")


def _client(*, pcm=None, tmp_path: Path | None = None):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        claude_default_model="opus",
        workspaces={
            "personal": SimpleNamespace(
                name="personal",
                vault_root="personal",
                default_model="",
                disallowed_tools=None,
                gws_profile="personal",
                model_bucket="personal",
            ),
            "work": SimpleNamespace(
                name="work",
                vault_root="work",
                default_model="opus",
                disallowed_tools=[],
                gws_profile="work",
                model_bucket="work",
            ),
        },
    )
    app.state.project_chat_manager = pcm
    if tmp_path is not None:
        app.state.local_session_manager = LocalSessionManager(
            workspace=tmp_path / "ws", runtime_root=tmp_path / "rt",
        )
    client = TestClient(app, base_url=_ORIGIN)
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def test_local_status_non_git_workspace(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    client, cookies = _client(tmp_path=tmp_path)
    resp = client.get("/api/local/status", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["git_repo"] is False
    assert data["branch"] is None


def test_local_status_reports_current_branch(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    _git_init(tmp_path / "ws", branch="feature-x")
    client, cookies = _client(tmp_path=tmp_path)
    resp = client.get("/api/local/status", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["git_repo"] is True
    assert data["branch"] == "feature-x"
    assert data["dirty"] is False


def test_local_handback_rejects_non_git_workspace(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    client, cookies = _client(tmp_path=tmp_path)
    resp = client.post("/api/local/handback", json={}, cookies=cookies)
    assert resp.status_code == 400
    data = resp.json()
    assert data["ok"] is False
    assert "not a git repository" in data["error"]


def test_local_preflight_endpoint(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    _git_init(tmp_path / "ws")
    client, cookies = _client(tmp_path=tmp_path)
    resp = client.get("/api/local/preflight", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert "branch" in data
    assert "dirty" in data
    assert "changed_files" in data
    assert "deploy_needed" in data
    assert "blockers" in data
    assert "warnings" in data


def test_workspaces_endpoint_lists_configured_workspaces(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    client, cookies = _client(tmp_path=tmp_path)

    resp = client.get("/api/workspaces", cookies=cookies)

    assert resp.status_code == 200
    assert resp.json() == {
        "workspaces": [
            {
                "name": "personal",
                "vault_root": "personal",
                "default_provider": "claude",
                "default_model": "",
                "gws_profile": "personal",
                "model_bucket": "personal",
                "disallowed_tools": None,
                "claude_ai_mcps": None,
            },
            {
                "name": "work",
                "vault_root": "work",
                "default_provider": "claude",
                "default_model": "opus",
                "gws_profile": "work",
                "model_bucket": "work",
                "disallowed_tools": [],
                "claude_ai_mcps": None,
            },
        ],
        "active": "personal",
        "app_default_model": "opus",
        "provider_options": [
            {"value": "claude", "label": "Anthropic (via Claude Code)"},
            {"value": "codex", "label": "OpenAI (via Codex)"},
        ],
        "claude_ai_connectors": [
            "mcp__claude_ai_Airtable",
            "mcp__claude_ai_Asana",
            "mcp__claude_ai_Atlassian",
            "mcp__claude_ai_Google_Cloud_BigQuery",
            "mcp__claude_ai_Salesforce",
            "mcp__claude_ai_Sentry",
            "mcp__claude_ai_Slack",
            "mcp__claude_ai_incident_io",
        ],
    }


def test_handback_blocks_on_secrets(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ws").mkdir()
    _git_init(tmp_path / "ws")
    client, cookies = _client(tmp_path=tmp_path)

    # Mock preflight to return blockers
    async def mock_preflight(self):
        return {
            "branch": "main",
            "dirty": True,
            "changed_files": {},
            "deploy_needed": False,
            "blockers": ["File 'secrets.key' is a cryptographic key."],
            "warnings": [],
        }
    monkeypatch.setattr(LocalSessionManager, "preflight", mock_preflight)

    resp = client.post("/api/local/handback", json={}, cookies=cookies)
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "Blocked by secrets check"
    assert "secrets.key" in data["blockers"][0]


def test_handback_warns_on_suspicious_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ws").mkdir()
    _git_init(tmp_path / "ws")
    client, cookies = _client(tmp_path=tmp_path)

    async def mock_preflight(self):
        return {
            "branch": "main",
            "dirty": True,
            "changed_files": {},
            "deploy_needed": False,
            "blockers": [],
            "warnings": ["File 'config.json' is suspicious."],
        }
    monkeypatch.setattr(LocalSessionManager, "preflight", mock_preflight)

    # Try handback without confirmation -> should fail
    resp = client.post("/api/local/handback", json={}, cookies=cookies)
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "Warnings exist, require confirmation"

    # Mock commit_and_sync so we can test confirmation bypass
    async def mock_sync(self):
        return {"ok": True, "merged": True, "deploy_needed": False}
    monkeypatch.setattr(LocalSessionManager, "commit_and_sync", mock_sync)

    # Try handback with confirmation -> should succeed
    resp = client.post("/api/local/handback", json={"confirm_warnings": True}, cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


class _FakeProject:
    def __init__(self, name, pid):
        self.name = name
        self.project_id = pid


class _FakeChat:
    chat_id = "chat-xyz"


class _FakePCM:
    """Captures the chat creation + dispatched prompt."""

    def __init__(self):
        self.created = None
        self.streamed = None

    def list_projects(self, workspace):
        assert workspace == "personal"
        return [_FakeProject("General", "p-gen")]

    def create_chat(self, project_id, title=None, model=None, **kw):
        self.created = {"project_id": project_id, "title": title, "model": model}
        return _FakeChat()

    def start_stream(self, chat_id, prompt, images=None):
        self.streamed = {"chat_id": chat_id, "prompt": prompt}
        return SimpleNamespace()


def test_handover_merge_opens_chat_with_conflict_prompt() -> None:
    pcm = _FakePCM()
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/handover/merge",
        json={"branch": "feature-x"},
        cookies=cookies, headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and data["chat_id"] == "chat-xyz"
    # Hosted in the personal General project; prompt carries branch + resolve intent.
    assert pcm.created["project_id"] == "p-gen"
    assert pcm.streamed["chat_id"] == "chat-xyz"
    assert "feature-x" in pcm.streamed["prompt"]
    assert "resolve" in pcm.streamed["prompt"].lower()
    # The prompt must never instruct a branch checkout or creation.
    assert "checkout" not in pcm.streamed["prompt"].lower()


def test_handover_merge_without_branch_or_repo_is_rejected() -> None:
    pcm = _FakePCM()
    client, cookies = _client(pcm=pcm)
    resp = client.post(
        "/api/handover/merge", json={}, cookies=cookies, headers={"Origin": _ORIGIN},
    )
    assert resp.status_code == 400
    assert "not a git repository" in resp.json()["error"]
