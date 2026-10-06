"""Route-level tests for the workspace git-sync flow: status reports the
current branch (or that the workspace isn't a git repo), and the merge
endpoint opens a chat with the conflict prompt carrying the branch.

The last section covers the unattended memory backup's three routes, which sit
on the same engine: they are thin wrappers over one service instance, and what
they have to get right is which status is an answer (200) and which is a
failure the caller can act on (400).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.app_settings import AppSettingsStore
from ciao.backup_service import SETUP_CHAT_TITLE, BackupService
from ciao.config import CiaoConfig
from ciao.legacy_node_state import LegacyNodeState
from ciao.local_session import LocalSessionManager
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import (
    handover_merge,
    local_backup_run,
    local_backup_settings,
    local_backup_setup_chat,
    local_backup_setup_prompt,
    local_backup_status,
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
        Route("/api/local/backup", local_backup_status, methods=["GET"]),
        Route("/api/local/backup", local_backup_settings, methods=["PATCH"]),
        Route("/api/local/backup/run", local_backup_run, methods=["POST"]),
        Route(
            "/api/local/backup/setup-prompt", local_backup_setup_prompt, methods=["GET"]
        ),
        Route(
            "/api/local/backup/setup-chat", local_backup_setup_chat, methods=["POST"]
        ),
        Route("/api/handover/merge", handover_merge, methods=["POST"]),
        Route("/api/workspaces", list_workspaces, methods=["GET"]),
    ]


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        env={
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo),
        },
    )
    return proc.stdout.strip()


def _git_init(repo: Path, *, branch: str = "main", seed: bool = True, bare: bool = False) -> None:
    """Turn ``repo`` into a git checkout on ``branch``, with one commit unless
    asked otherwise (a bare origin has no working tree to commit)."""
    repo.mkdir(parents=True, exist_ok=True)
    if bare:
        _run_git(repo, "init", "-q", "--bare", "-b", branch, ".")
        return
    _run_git(repo, "init", "-q", "-b", branch, ".")
    _run_git(repo, "config", "user.name", "T")
    _run_git(repo, "config", "user.email", "t@e.com")
    if not seed:
        return
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed")


def _client(*, pcm=None, tmp_path: Path | None = None):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        claude_default_model="opus",
        primary_workspace=lambda: "personal",
        # Mirrors CiaoConfig.default_provider_for_workspace for these
        # workspaces (no stored provider -> the default backend).
        default_provider_for_workspace=lambda name: "claude",
        workspaces={
            "personal": SimpleNamespace(
                name="personal",
                vault_root="personal",
                default_model="",
                disallowed_tools=None,
                gws_profile="personal",
            ),
            "work": SimpleNamespace(
                name="work",
                vault_root="work",
                default_model="opus",
                disallowed_tools=[],
                gws_profile="work",
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
                "gws_profile": "personal",
                "disallowed_tools": None,
                "allowed_mcp_servers": None,
                "color": "pink",
            },
            {
                "name": "work",
                "vault_root": "work",
                "default_provider": "claude",
                "gws_profile": "work",
                "disallowed_tools": [],
                "allowed_mcp_servers": None,
                "color": "pink",
            },
        ],
        "active": "personal",
        "primary": "personal",
        "provider_options": [
            {"value": "claude", "label": "Anthropic (via Claude Code)"},
            {"value": "opencode", "label": "opencode"},
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

    def __init__(self, title="", archived=False):
        self.title = title
        self.archived = archived


class _FakePCM:
    """Captures the chat creation + dispatched prompt.

    Also keeps what it created, because the setup route's whole claim is that a
    second click re-enters the chat it already opened: that is a statement
    about how many chats exist, and a fake that only remembers the last one
    could not falsify it.
    """

    def __init__(self):
        self.created = None
        self.streamed = None
        self.chats: list[_FakeChat] = []
        self.sent: list[dict] = []

    def list_projects(self, workspace):
        assert workspace == "personal"
        return [_FakeProject("General", "p-gen")]

    def list_chats(self, project_id=None):
        return list(self.chats)

    def create_chat(self, project_id, title=None, model=None, **kw):
        self.created = {"project_id": project_id, "title": title, "model": model}
        chat = _FakeChat(title=title or "")
        self.chats.append(chat)
        return chat

    def start_stream(self, chat_id, prompt, images=None):
        self.streamed = {"chat_id": chat_id, "prompt": prompt}
        self.sent.append(self.streamed)
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


# ── the unattended memory backup ──


def _backup_world(tmp_path: Path) -> CiaoConfig:
    """An install whose durable data is a git repository with an origin."""
    workspace = tmp_path / "install"
    (workspace / "memory-vault" / "Notes").mkdir(parents=True)
    (workspace / ".runtime").mkdir()
    (workspace / "memory-vault" / "Notes" / "day-1.md").write_text("hi\n", encoding="utf-8")
    _git_init(workspace, seed=False)
    _run_git(workspace, "add", "-A")
    _run_git(workspace, "commit", "-q", "-m", "seed")
    origin = tmp_path / "origin.git"
    _git_init(origin, bare=True)
    _run_git(workspace, "remote", "add", "origin", str(origin))
    _run_git(workspace, "push", "-q", "-u", "origin", "main")
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        vault_root=workspace / "memory-vault",
    )


def _backup_client(
    config: CiaoConfig, pcm=None
) -> tuple[TestClient, dict, BackupService]:
    """A client whose app state carries a real backup service over ``config``.

    ``pcm`` is the chat manager the setup routes open their chat through; the
    other backup routes never touch it, so it stays optional.
    """
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.project_chat_manager = pcm
    app.state.app_settings = AppSettingsStore(
        config.state_path.parent / "app_settings.json"
    )
    app.state.backup_service = BackupService(
        config,
        app.state.app_settings,
        node_state=lambda: LegacyNodeState(kind="host"),
        now=lambda: datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    )
    return (
        TestClient(app, base_url=_ORIGIN),
        {SESSION_COOKIE: serializer.dumps({"user": "owner"})},
        app.state.backup_service,
    )


def test_backup_status_reports_the_scope_and_never_mutates(tmp_path: Path) -> None:
    config = _backup_world(tmp_path)
    (config.workspace_root / "memory-vault" / "Notes" / "day-2.md").write_text(
        "two\n", encoding="utf-8"
    )
    client, cookies, _service = _backup_client(config)

    resp = client.get("/api/local/backup", cookies=cookies)

    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "pending"
    assert data["pending_changes"] == 1
    assert data["interval_s"] == 300
    assert "memory-vault" in data["scope"]
    # Read-only: a status call stages nothing and commits nothing.
    assert "?? memory-vault/Notes/day-2.md" in _run_git(
        config.workspace_root, "status", "--porcelain"
    )


def test_backup_pause_persists_and_reports_through_the_same_service(
    tmp_path: Path,
) -> None:
    config = _backup_world(tmp_path)
    client, cookies, _service = _backup_client(config)

    paused = client.patch(
        "/api/local/backup", json={"paused": True}, cookies=cookies,
    )

    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    # Persisted, not just held in the process: a fresh store reads it back.
    reread = AppSettingsStore(config.state_path.parent / "app_settings.json")
    assert reread.settings.backup_paused is True


def test_backup_settings_rejects_a_body_it_cannot_honour(tmp_path: Path) -> None:
    config = _backup_world(tmp_path)
    client, cookies, _service = _backup_client(config)

    empty = client.patch("/api/local/backup", json={}, cookies=cookies)
    unknown = client.patch("/api/local/backup", json={"nope": True}, cookies=cookies)
    wrong_type = client.patch("/api/local/backup", json={"paused": "yes"}, cookies=cookies)

    assert empty.status_code == 400
    assert unknown.status_code == 400
    assert wrong_type.status_code == 400


def test_a_manual_backup_commits_the_scope_and_pushes(tmp_path: Path) -> None:
    config = _backup_world(tmp_path)
    (config.workspace_root / "memory-vault" / "Notes" / "day-2.md").write_text(
        "two\n", encoding="utf-8"
    )
    client, cookies, _service = _backup_client(config)

    resp = client.post("/api/local/backup/run", cookies=cookies)

    assert resp.status_code == 200
    assert resp.json()["state"] == "ready"
    committed = _run_git(
        config.workspace_root, "show", "--name-only", "--format=", "HEAD"
    )
    assert "memory-vault/Notes/day-2.md" in committed
    # The service's own runtime state (written by the PATCH-free status call's
    # store) is not durable data, so it is neither committed nor swept in.
    assert ".runtime" not in committed
    assert "memory-vault" not in _run_git(
        config.workspace_root, "status", "--porcelain"
    )


def test_a_manual_backup_reports_an_unreachable_root_as_a_failure(
    tmp_path: Path,
) -> None:
    """A data root with no remote is not something the caller can fix by
    retrying, so it is a 400 with the reason in the body — never a bare 200."""
    config = _backup_world(tmp_path)
    _run_git(config.workspace_root, "remote", "remove", "origin")
    client, cookies, _service = _backup_client(config)

    resp = client.post("/api/local/backup/run", cookies=cookies)

    assert resp.status_code == 400
    assert resp.json()["state"] == "not_configured"
    assert "no 'origin' remote" in resp.json()["reason"]


def test_a_paused_backup_never_touches_the_repository(tmp_path: Path) -> None:
    config = _backup_world(tmp_path)
    note = config.workspace_root / "memory-vault" / "Notes" / "day-2.md"
    note.write_text("two\n", encoding="utf-8")
    client, cookies, _service = _backup_client(config)
    client.patch("/api/local/backup", json={"paused": True}, cookies=cookies)

    resp = client.post("/api/local/backup/run", cookies=cookies)

    assert resp.status_code == 200  # a paused run is an answer, not a failure
    assert resp.json()["state"] == "paused"
    assert _run_git(config.workspace_root, "status", "--porcelain") != ""
    assert note.is_file()


def test_the_backup_routes_need_a_session(tmp_path: Path) -> None:
    """These run git against the user's own repository, so they sit behind the
    signed session cookie like every other `/api` route — not in the
    loopback-only set, and not session-free."""
    config = _backup_world(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[
            Middleware(
                AuthMiddleware, serializer=serializer
            )
        ],
    )
    app.state.serializer = serializer
    # The middleware reads the requirement off the config, so that is what has
    # to carry it (a duck-typed stand-in is enough: these routes never read
    # anything else off the config).
    app.state.config = SimpleNamespace()
    app.state.backup_service = _backup_client(config)[2]
    client = TestClient(app, base_url=_ORIGIN)

    assert client.get("/api/local/backup").status_code == 401
    assert client.post("/api/local/backup/run").status_code == 401
    assert client.patch("/api/local/backup", json={"paused": True}).status_code == 401


def test_the_backup_routes_report_a_missing_service(tmp_path: Path) -> None:
    config = _backup_world(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    client = TestClient(app, base_url=_ORIGIN)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}

    assert client.get("/api/local/backup", cookies=cookies).status_code == 500
    assert client.post("/api/local/backup/run", cookies=cookies).status_code == 500


# ── the canonical setup prompt (one prompt, two actions) ────────────────────
#
# The two actions a user with no Git knowledge gets — copy the prompt, or set
# up in a chat that sends it — must be the same instructions, and the second
# must not spawn a second agent every time it is clicked.


def test_the_setup_prompt_route_returns_the_context_and_the_prompt(
    tmp_path: Path,
) -> None:
    config = _backup_world(tmp_path)
    client, cookies, _service = _backup_client(config)

    resp = client.get("/api/local/backup/setup-prompt", cookies=cookies)

    assert resp.status_code == 200
    data = resp.json()
    # The context is served beside the prompt so a Settings page can show
    # "backing up <folder>" without parsing prose.
    assert data["context"]["folder"] == str(config.workspace_root.resolve())
    assert "memory-vault" in data["context"]["scope"]
    assert data["context"]["branch"] == "main"
    assert data["context"]["interval_s"] == 300
    assert f'"{data["context"]["folder"]}"' in data["prompt"]
    # Read-only: rendering the prompt runs no git that changes anything.
    assert _run_git(config.workspace_root, "status", "--porcelain") == ""


def test_the_setup_chat_sends_the_prompt_and_a_second_call_reuses_it(
    tmp_path: Path,
) -> None:
    """The in-app action has to actually send the prompt, and clicking it twice
    must not put two agents on the same repository."""
    config = _backup_world(tmp_path)
    pcm = _FakePCM()
    client, cookies, _service = _backup_client(config, pcm=pcm)
    prompt = client.get("/api/local/backup/setup-prompt", cookies=cookies).json()[
        "prompt"
    ]

    first = client.post("/api/local/backup/setup-chat", cookies=cookies)
    second = client.post("/api/local/backup/setup-chat", cookies=cookies)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["reused"] is False
    # A repeated click is not an error and not a new chat: it re-enters the
    # one that is already live, and says so.
    assert second.json()["reused"] is True
    assert second.json()["chat_id"] == first.json()["chat_id"]
    assert len(pcm.chats) == 1
    # Sent, not drafted: exactly one turn, carrying exactly the copied text.
    assert len(pcm.sent) == 1
    assert pcm.sent[0]["prompt"] == prompt
    assert pcm.created["title"] == SETUP_CHAT_TITLE


def test_an_archived_setup_chat_is_replaced_not_re_entered(tmp_path: Path) -> None:
    """The reuse rule is a live chat, not a chat that ever existed. An owner who
    archived a finished setup is starting over, and handing them the old
    conversation would be a silent no-op."""
    config = _backup_world(tmp_path)
    pcm = _FakePCM()
    client, cookies, _service = _backup_client(config, pcm=pcm)
    client.post("/api/local/backup/setup-chat", cookies=cookies)
    pcm.chats[0].archived = True

    resp = client.post("/api/local/backup/setup-chat", cookies=cookies)

    assert resp.json()["reused"] is False
    assert len(pcm.chats) == 2
    assert len(pcm.sent) == 2


def test_a_guided_setup_is_detected_without_a_restart(tmp_path: Path) -> None:
    """The point of the guided flow is that it does not need one. The status is
    re-derived from the repository, so an agent (or a person) that adds the
    remote while the engine runs flips the install to ready on the next read."""
    config = _backup_world(tmp_path)
    _run_git(config.workspace_root, "remote", "remove", "origin")
    pcm = _FakePCM()
    client, cookies, service = _backup_client(config, pcm=pcm)

    before = client.get("/api/local/backup", cookies=cookies).json()
    assert before["state"] == "not_configured"

    client.post("/api/local/backup/setup-chat", cookies=cookies)
    # The agent's work: a private remote, connected and pushed.
    origin = tmp_path / "new-origin.git"
    _git_init(origin, bare=True)
    _run_git(config.workspace_root, "remote", "add", "origin", str(origin))
    _run_git(config.workspace_root, "push", "-q", "-u", "origin", "main")

    after = client.get("/api/local/backup", cookies=cookies).json()

    assert after["state"] == "ready", after["reason"]
    assert after["enabled"] is True


def test_a_paused_owner_is_not_enabled_by_a_guided_setup(tmp_path: Path) -> None:
    """Pausing is the owner's own hold, and a setup chat is not permission to
    lift it — the install is still reported paused, which is the truth."""
    config = _backup_world(tmp_path)
    _run_git(config.workspace_root, "remote", "remove", "origin")
    pcm = _FakePCM()
    client, cookies, _service = _backup_client(config, pcm=pcm)
    client.patch("/api/local/backup", json={"paused": True}, cookies=cookies)

    client.post("/api/local/backup/setup-chat", cookies=cookies)
    origin = tmp_path / "new-origin.git"
    _git_init(origin, bare=True)
    _run_git(config.workspace_root, "remote", "add", "origin", str(origin))
    _run_git(config.workspace_root, "push", "-q", "-u", "origin", "main")

    status = client.get("/api/local/backup", cookies=cookies).json()

    # The pause is untouched by the setup, and it is the pause — not the missing
    # remote — that the status reports, which is what the owner must see.
    assert status["state"] == "paused"
    assert "paused" in status["reason"]
    reread = AppSettingsStore(config.state_path.parent / "app_settings.json")
    assert reread.settings.backup_paused is True


def test_the_setup_routes_report_a_missing_service(tmp_path: Path) -> None:
    """A missing service is a broken install, and a prompt rendered without one
    would describe a backup nothing is going to perform."""
    config = _backup_world(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = config
    app.state.project_chat_manager = _FakePCM()
    client = TestClient(app, base_url=_ORIGIN)
    cookies = {SESSION_COOKIE: serializer.dumps({"user": "owner"})}

    assert client.get("/api/local/backup/setup-prompt", cookies=cookies).status_code == 500
    assert client.post("/api/local/backup/setup-chat", cookies=cookies).status_code == 500


def test_the_setup_routes_need_a_session(tmp_path: Path) -> None:
    """The setup chat dispatches an agent against the user's own repository, so
    it sits behind the signed session cookie like every other `/api` route."""
    config = _backup_world(tmp_path)
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=_routes(),
        middleware=[
            Middleware(
                AuthMiddleware, serializer=serializer
            )
        ],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace()
    app.state.project_chat_manager = _FakePCM()
    app.state.backup_service = _backup_client(config)[2]
    client = TestClient(app, base_url=_ORIGIN)

    assert client.get("/api/local/backup/setup-prompt").status_code == 401
    assert client.post("/api/local/backup/setup-chat").status_code == 401
