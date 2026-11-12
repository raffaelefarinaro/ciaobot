"""Browser-only bounded lookup of files a completed chat reply mentions (#2118).

Every fixture is a real `CiaoConfig` with a real registry, a real
`ProjectChatManager` and, for the re-rooted layout, a real migration receipt.
Nothing monkeypatches `agent_root` or `workspace_vault_root`: a stub could agree
with itself about a layout the engine never produces.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.fs_sandbox import AGENT_FS_SCOPES
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import request_limits, routes_api, routes_helpers
from ciao.web.auth import AuthMiddleware, make_serializer
from ciao.web.project_chats import ProjectChatManager
from ciao.web.request_limits import ApiRequestBodyLimitMiddleware
from ciao.web.routes_api import resolve_chat_file_paths
from ciao.web.security import SecurityHeadersMiddleware
from ciao.workspace_reroot import write_receipt

ROUTE = "/api/chats/{chat_id}/file-paths/resolve"
URL = ROUTE
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths, symlinks and modes")
WIN32_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="native Windows drive paths")


@pytest.fixture(autouse=True)
def _fresh_reroot_cache():
    reset_reroot_cache()
    yield
    reset_reroot_cache()


def _legacy_config(tmp_path: Path) -> CiaoConfig:
    """Two workspaces, no receipt: the pre-reroot layout, all vaults under one shared vault."""
    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    (install / "memory-vault" / "work").mkdir(parents=True)
    (install / "memory-vault" / "other").mkdir(parents=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=install,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=install / "memory-vault",
        workspaces={
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
            "other": WorkspaceConfig(name="other", vault_root="memory-vault/other"),
        },
    )


def _rerooted_config(tmp_path: Path) -> CiaoConfig:
    """The same two workspaces after the re-rooting: each has its own agent root with a nested vault."""
    install = tmp_path / "install"
    runtime = install / ".runtime"
    runtime.mkdir(parents=True)
    for name in ("work", "other"):
        (install / name / "memory-vault").mkdir(parents=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=install,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=install / "memory-vault",
        workspaces={
            "work": WorkspaceConfig(name="work", vault_root="work/memory-vault"),
            "other": WorkspaceConfig(name="other", vault_root="other/memory-vault"),
        },
    )
    write_receipt(runtime, {"status": "migrated", "workspaces": ["work", "other"]})
    reset_reroot_cache()
    return config


def _manager(config: CiaoConfig) -> ProjectChatManager:
    runtime = config.state_path.parent
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(runtime, runtime / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _chat(manager: ProjectChatManager, workspace: str = "work", repo_path: str = ""):
    project = manager.create_project("Notes", workspace=workspace, repo_path=repo_path)
    return manager.create_chat(project.project_id, title="Mentions")


def _app(manager: ProjectChatManager, config: CiaoConfig, *, middleware=None) -> Starlette:
    app = Starlette(
        routes=[Route(ROUTE, resolve_chat_file_paths, methods=["POST"])],
        middleware=middleware or [],
    )
    app.state.project_chat_manager = manager
    app.state.config = config
    return app


def _client(manager: ProjectChatManager, config: CiaoConfig) -> TestClient:
    return TestClient(_app(manager, config), raise_server_exceptions=False)


def _post(client: TestClient, chat_id: str, paths: list[str]):
    return client.post(URL.format(chat_id=chat_id), json={"paths": paths})


def _results(response) -> list[str | None]:
    assert response.status_code == 200, response.text
    return [row["path"] for row in response.json()["results"]]


def _canon(path: Path) -> str:
    return path.resolve().as_posix()


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _case_insensitive(tmp: Path) -> bool:
    probe = tmp / "CaseProbe.tmp"
    probe.write_text("x")
    try:
        return (tmp / "caseprobe.tmp").exists()
    finally:
        probe.unlink()


def test_real_legacy_config_only_discovers_owning_vault(tmp_path):
    config = _legacy_config(tmp_path)
    assert config.agent_root("work") == config.workspace_root
    vault = config.workspace_vault_root("work")
    note = _write(vault / "notes" / "a.md")
    _write(config.workspace_root / "README.md")
    other_note = _write(config.workspace_vault_root("other") / "other.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    rows = _client(manager, config).post(
        URL.format(chat_id=chat.chat_id),
        json={"paths": [
            "notes/a.md",
            str(note),
            str(config.workspace_root / "README.md"),
            str(other_note),
            "../other/other.md",
            "memory-vault/other/other.md",
            "memory-vault/work/notes/a.md",
            "../README.md",
        ]},
    )
    assert _results(rows) == [
        _canon(note), _canon(note), None, None, None, None, None, None,
    ]


def test_real_rerooted_config_discovers_workspace_and_vault(tmp_path):
    config = _rerooted_config(tmp_path)
    assert config.agent_root("work") == config.workspace_root / "work"
    agent_file = _write(config.workspace_root / "work" / "AGENTS.md")
    vault_note = _write(config.workspace_vault_root("work") / "notes" / "a.md")
    sibling_agent = _write(config.workspace_root / "other" / "AGENTS.md")
    sibling_note = _write(config.workspace_vault_root("other") / "o.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    response = _post(client, chat.chat_id, [
        "AGENTS.md",
        "notes/a.md",
        "memory-vault/notes/a.md",
        str(vault_note),
        str(sibling_agent),
        str(sibling_note),
    ])
    assert _results(response) == [
        _canon(agent_file), _canon(vault_note), _canon(vault_note), _canon(vault_note), None, None,
    ]


@POSIX_ONLY
def test_rerooted_symlink_escape_fails(tmp_path):
    config = _rerooted_config(tmp_path)
    outside = _write(tmp_path / "outside.md")
    link = config.workspace_vault_root("work") / "escape.md"
    link.symlink_to(outside)
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, ["escape.md"])) == [None]


def test_membership_never_falls_back_to_primary(tmp_path):
    config = _legacy_config(tmp_path)
    primary = config.primary_workspace()
    _write(config.workspace_vault_root(primary) / "notes.md")
    manager = _manager(config)
    orphan_workspace = manager.create_project("Empty", workspace="")
    unknown = manager.create_project("Ghost", workspace="ghost")
    chats = [
        manager.create_chat(orphan_workspace.project_id, title="empty"),
        manager.create_chat(unknown.project_id, title="ghost"),
    ]
    client = _client(manager, config)
    for chat in chats:
        assert _results(_post(client, chat.chat_id, ["notes.md"])) == [None]
    assert _post(client, "missing", ["notes.md"]).status_code == 404


def test_unavailable_or_shared_root_fails_closed(tmp_path):
    config = _legacy_config(tmp_path)
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    vault = config.workspace_vault_root("work")
    _write(vault / "notes.md")
    _write(config.workspace_root / "install.md")

    # A registered vault that is missing gives no fallback, and never a global search.
    shutil.rmtree(vault)
    assert _results(_post(client, chat.chat_id, ["notes.md", "install.md"])) == [None, None]

    # A vault that contains the install root is the whole filesystem for this install.
    config.workspaces["work"] = WorkspaceConfig(name="work", vault_root=str(config.workspace_root))
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, ["install.md"])) == [None]

    # Two workspaces sharing one vault: the root is not private to either.
    config = _legacy_config(tmp_path / "shared")
    config.workspaces["other"] = WorkspaceConfig(name="other", vault_root="memory-vault/work")
    _write(config.workspace_vault_root("work") / "notes.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, ["notes.md"])) == [None]

    # A vault that contains another registered workspace's vault.
    config = _legacy_config(tmp_path / "nested")
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root=str(config.workspace_root / "memory-vault")
    )
    _write(config.workspace_root / "memory-vault" / "notes.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, ["notes.md"])) == [None]


def test_batch_order_duplicates_empty_and_ambiguity(tmp_path):
    config = _rerooted_config(tmp_path)
    vault_note = _write(config.workspace_vault_root("work") / "notes" / "a.md")
    _write(config.workspace_root / "work" / "README.md")
    _write(config.workspace_vault_root("work") / "README.md")
    hardlink = config.workspace_vault_root("work") / "notes" / "b.md"
    os.link(vault_note, hardlink)
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)

    assert _results(_post(client, chat.chat_id, ["notes/a.md", "README.md", "notes/a.md"])) == [
        _canon(vault_note), None, _canon(vault_note),
    ]
    empty = _post(client, chat.chat_id, [])
    assert empty.status_code == 200
    assert empty.json() == {"results": []}
    # Nested aliases (agent root -> vault) deduplicate to the one canonical file.
    assert _results(_post(client, chat.chat_id, ["memory-vault/notes/a.md"])) == [_canon(vault_note)]
    # A hardlink is a different path, so it keeps its own row rather than merging with a.md.
    assert _results(_post(client, chat.chat_id, ["notes/b.md"])) == [_canon(hardlink)]


def test_exact_lookup_never_calls_fuzzy_helpers(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("fuzzy helper called")

    monkeypatch.setattr(routes_api, "_resolve_chat_file", forbidden)
    monkeypatch.setattr(routes_api, "_resolve_in_chat", forbidden)
    monkeypatch.setattr(routes_api, "_resolve_workspace_path", forbidden)
    monkeypatch.setattr(routes_helpers, "_find_fuzzy_match", forbidden)
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    exact = _write(vault / "notes" / "a.md")
    _write(vault / "deep" / "x" / "a.md")
    _write(vault / "notes" / "b.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    assert _results(_post(client, chat.chat_id, ["notes/a.md"])) == [_canon(exact)]
    # Same basename in another folder, a wrong stem, and a missing path never substitute.
    assert _results(_post(client, chat.chat_id, ["a.md", "other/a.md", "notes/c.md"])) == [None, None, None]
    if not _case_insensitive(tmp_path):
        assert _results(_post(client, chat.chat_id, ["notes/A.md"])) == [None]


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
}


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, env=_GIT_ENV, capture_output=True)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(path))
    _write(path / "docs" / "guide.md", "main")
    _git("-C", str(path), "add", ".")
    _git("-C", str(path), "commit", "-qm", "init")
    return path


def test_repo_policy_and_real_worktree_identity(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    worktree = tmp_path / "wt"
    _git("-C", str(repo), "worktree", "add", "-q", "-b", "feat", str(worktree))
    _write(worktree / "docs" / "feature.md", "branch only")

    config = _legacy_config(tmp_path)
    manager = _manager(config)
    chat = _chat(manager, "work", repo_path=str(repo))
    client = _client(manager, config)

    # Code mode off: the repo is not a root at all.
    no_repo = _chat(manager, "work")
    assert _results(_post(client, no_repo.chat_id, ["docs/guide.md"])) == [None]

    # Code mode on: the main checkout is searched once a location is recorded.
    asyncio.run(manager.refresh_repo_location(chat.chat_id, []))
    assert _results(_post(client, chat.chat_id, ["docs/guide.md"])) == [_canon(repo / "docs" / "guide.md")]

    # A memory-pass chat never sees the repo.
    project = manager.get_project(chat.project_id)
    project.kind = "memory"
    assert _results(_post(client, chat.chat_id, ["docs/guide.md"])) == [None]
    project.kind = ""

    # A valid active worktree replaces the main checkout.
    asyncio.run(manager.refresh_repo_location(chat.chat_id, [str(worktree)]))
    assert chat.repo_location.get("path")
    assert _results(_post(client, chat.chat_id, ["docs/feature.md"])) == [_canon(worktree / "docs" / "feature.md")]

    # A recorded location that belongs to an unrelated directory fails closed: no main-checkout substitute.
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    chat.repo_location = {"path": str(unrelated), "exists": True, "is_worktree": True, "branch": "x"}
    assert _results(_post(client, chat.chat_id, ["docs/guide.md"])) == [None]

    # A missing recorded worktree fails closed too.
    chat.repo_location = {"path": str(tmp_path / "gone"), "exists": True, "is_worktree": True, "branch": "feat"}
    assert _results(_post(client, chat.chat_id, ["docs/guide.md"])) == [None]


def test_absolute_home_parent_and_native_path_policy(tmp_path, monkeypatch):
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    note = _write(vault / "notes" / "a.md")
    _write(config.workspace_vault_root("other") / "other.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    monkeypatch.setenv("HOME", str(vault))
    accepted = _results(_post(client, chat.chat_id, [str(note), "~/notes/a.md", "./notes/a.md", "notes/../notes/a.md"]))
    assert accepted == [_canon(note)] * 4
    assert _results(_post(client, chat.chat_id, [
        "../other/other.md", "~otheruser/notes/a.md", "\\\\server\\share\\a.md", "//server/share/a.md",
        "\\\\?\\C:\\x.md", "C:\\Windows\\win.ini", "notes/a.md/x",
    ])) == [None] * 7
    if sys.platform != "win32":
        assert _results(_post(client, chat.chat_id, ["C:\\Users\\x\\a.md"])) == [None]


@WIN32_ONLY
def test_native_win32_drive_path_is_accepted(tmp_path):
    config = _legacy_config(tmp_path)
    note = _write(config.workspace_vault_root("work") / "notes" / "a.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, [str(note)])) == [_canon(note)]


def test_agent_grants_do_not_expand_discovery(tmp_path):
    config = _legacy_config(tmp_path)
    credentials = _write(config.workspace_root / "gws" / "token.json", "{}")
    data_root = _write(config.workspace_root / "data" / "secret.md")
    vault_note = _write(config.workspace_vault_root("work") / "n.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    # The agent FS scope is set per workspace (CiaoConfig has no scope field).
    # Each scope must be in effect when the lookup runs, and neither may widen it.
    for scope in AGENT_FS_SCOPES:
        config.workspaces["work"] = WorkspaceConfig(
            name="work", vault_root="memory-vault/work", agent_fs_scope=scope
        )
        assert config.workspace("work").effective_agent_fs_scope() == scope
        result = _results(_post(client, chat.chat_id, [str(credentials), str(data_root), "n.md"]))
        assert result == [None, None, _canon(vault_note)], scope


def test_line_suffix_and_file_type_size_rules(tmp_path):
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    note = _write(vault / "notes" / "a.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)

    response = _post(client, chat.chat_id, ["notes/a.md:17", f"{note}:17", "notes/a.md:2147483647"])
    assert _results(response) == [_canon(note), _canon(note), _canon(note)]
    assert [row["input"] for row in response.json()["results"]] == ["notes/a.md:17", f"{note}:17", "notes/a.md:2147483647"]
    assert _results(_post(client, chat.chat_id, [
        "notes/a.md:0", "notes/a.md:2147483648", "notes/a.md:01", "notes/a.md:1x", "notes/a.md:-1",
    ])) == [None] * 5

    text = _write(vault / "big.md")
    with text.open("r+b") as f:
        f.truncate(2 * 1024 * 1024)
    over_text = _write(vault / "over.md")
    with over_text.open("r+b") as f:
        f.truncate(2 * 1024 * 1024 + 1)
    image = _write(vault / "pic.png")
    with image.open("r+b") as f:
        f.truncate(15 * 1024 * 1024)
    over_image = _write(vault / "over.png")
    with over_image.open("r+b") as f:
        f.truncate(15 * 1024 * 1024 + 1)
    binary = _write(vault / "doc.pdf")
    with binary.open("r+b") as f:
        f.truncate(50 * 1024 * 1024)
    over_binary = _write(vault / "over.pdf")
    with over_binary.open("r+b") as f:
        f.truncate(50 * 1024 * 1024 + 1)
    _write(vault / ".env")
    _write(vault / "sample.example")
    _write(vault / "Makefile-no-ext")
    _write(vault / "tool.exe")
    (vault / "folder.md").mkdir()
    assert _results(_post(client, chat.chat_id, [
        "big.md", "over.md", "pic.png", "over.png", "doc.pdf", "over.pdf",
        ".env", "sample.example", "Makefile-no-ext", "tool.exe", "folder.md",
    ])) == [
        _canon(text), None, _canon(image), None, _canon(binary), None,
        None, None, None, None, None,
    ]


@POSIX_ONLY
def test_fifo_and_symlink_loop_are_null(tmp_path):
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    note = _write(vault / "keep.md")
    os.mkfifo(vault / "pipe.md")
    (vault / "loop.md").symlink_to(vault / "loop.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    assert _results(_post(_client(manager, config), chat.chat_id, ["pipe.md", "loop.md", "keep.md"])) == [
        None, None, _canon(note),
    ]


def test_malformed_json_and_surrogates_reject_before_filesystem(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("filesystem work ran for a malformed body")

    monkeypatch.setattr(routes_api, "_snapshot_file_mention_context", forbidden)
    monkeypatch.setattr(routes_api, "_resolve_file_mention_batch", forbidden)
    config = _legacy_config(tmp_path)
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    url = URL.format(chat_id=chat.chat_id)
    bodies = [
        b"[" * 100 + b"]" * 100,
        b'{"paths":["\\ud800"]}',
        b'{"paths":["\\udc00"]}',
        b'{"paths":["\xff"]}',
        b"{",
        b'{"paths":["a"],"extra":1}',
        b'{"paths":["a\\u0000b"]}',
        b'{"paths":["a\\u0007"]}',
        json.dumps({"paths": ["a" * 4097]}).encode(),
        b'{"paths":"notalist"}',
        b'{"paths":[1]}',
        b"[]",
    ]
    for body in bodies:
        response = client.post(url, content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400, body[:40]
        assert response.headers["cache-control"] == "no-store"
        assert "error" in response.json()


def test_malformed_body_is_rejected_by_middleware_before_handler(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("handler ran for a deeply nested body")

    monkeypatch.setattr(routes_api, "_snapshot_file_mention_context", forbidden)
    config = _legacy_config(tmp_path)
    manager = _manager(config)
    chat = _chat(manager, "work")
    serializer = make_serializer("test-secret")
    app = _app(manager, config, middleware=_production_middleware(serializer))
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    deep = '{"paths":' + "[" * 64 + "]" * 64 + "}"
    response = client.post(URL.format(chat_id=chat.chat_id), content=deep.encode(), headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"error": "bad json"}


def _production_middleware(serializer) -> list[Middleware]:
    return [
        Middleware(SecurityHeadersMiddleware),
        Middleware(AuthMiddleware, serializer=serializer),
        Middleware(ApiRequestBodyLimitMiddleware),
    ]


def test_body_count_and_stream_limits(tmp_path, monkeypatch):
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    _write(vault / "notes" / "a.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    serializer = make_serializer("test-secret")
    app = _app(manager, config, middleware=_production_middleware(serializer))
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    url = URL.format(chat_id=chat.chat_id)

    too_many = client.post(url, json={"paths": ["notes/a.md"] * 51})
    assert too_many.status_code == 413
    assert too_many.headers["cache-control"] == "no-store"
    assert _results(client.post(url, json={"paths": ["notes/a.md"] * 50})) == [_canon(vault / "notes" / "a.md")] * 50

    exact_body = b'{"paths":["notes/a.md"]}'
    exact = exact_body + b" " * (request_limits.FILE_MENTION_BODY_LIMIT - len(exact_body))
    assert len(exact) == request_limits.FILE_MENTION_BODY_LIMIT
    assert _results(client.post(url, content=exact, headers={"content-type": "application/json"})) == [
        _canon(vault / "notes" / "a.md"),
    ]

    over = b'{"paths":["notes/a.md"]}' + b" " * request_limits.FILE_MENTION_BODY_LIMIT
    monkeypatch.setattr(routes_api, "_snapshot_file_mention_context", _forbid)
    declared = client.post(url, content=over, headers={"content-type": "application/json"})
    assert declared.status_code == 413
    assert declared.json() == {"error": "request body too large"}

    assert request_limits._route_limit(
        create_app_for(config, manager), {"type": "http", "method": "POST", "path": url, "headers": []}
    ) == (request_limits.FILE_MENTION_BODY_LIMIT, "")


def _forbid(*_args, **_kwargs):
    raise AssertionError("handler ran")


def create_app_for(config: CiaoConfig, manager: ProjectChatManager):
    from ciao.web.app import create_app

    return create_app(config)


def test_stream_limit_stops_at_the_crossing_chunk_and_rejects_bad_length(tmp_path):
    config = _legacy_config(tmp_path)
    manager = _manager(config)
    chat = _chat(manager, "work")
    inner = _app(manager, config)
    url = URL.format(chat_id=chat.chat_id)
    asgi = ApiRequestBodyLimitMiddleware(inner)

    async def run(headers, chunks):
        calls = {"n": 0}
        sent: list[dict] = []
        queue = list(chunks)

        async def receive():
            calls["n"] += 1
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": url, "headers": headers, "query_string": b"", "root_path": "", "scheme": "http", "server": ("testserver", 80), "client": ("127.0.0.1", 1)}
        await asgi(scope, receive, send)
        return calls["n"], sent

    chunk = b"x" * 16384
    calls, sent = asyncio.run(run([(b"content-type", b"application/json")], [chunk] * 10))
    assert calls == 3
    assert sent[0]["status"] == 413
    calls, sent = asyncio.run(run([(b"content-length", b"abc")], [b"{}"]))
    assert sent[0]["status"] == 413


def test_delayed_vault_reconfiguration_same_workspace_discards_result(tmp_path, monkeypatch):
    config = _legacy_config(tmp_path)
    old_vault = config.workspace_vault_root("work")
    _write(old_vault / "notes.md")
    moved = _write(config.workspace_root / "memory-vault" / "moved" / "notes.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    original = routes_api._resolve_file_mention_batch

    def reconfigure_then_resolve(roots, paths):
        config.workspaces["work"] = WorkspaceConfig(name="work", vault_root="memory-vault/moved")
        return original(roots, paths)

    monkeypatch.setattr(routes_api, "_resolve_file_mention_batch", reconfigure_then_resolve)
    response = _post(_client(manager, config), chat.chat_id, ["notes.md"])
    assert _results(response) == [None]
    assert _canon(old_vault / "notes.md") not in response.text
    assert moved.exists()


@pytest.mark.parametrize("mutation", ["reroot", "project_move", "code_mode", "repo_location", "chat_deleted"])
def test_delayed_reroot_membership_and_repo_changes_discard_result(tmp_path, monkeypatch, mutation):
    config = _legacy_config(tmp_path)
    _write(config.workspace_vault_root("work") / "notes.md")
    (config.workspace_root / "work").mkdir()
    repo = _git_repo(tmp_path / "repo")
    manager = _manager(config)
    chat = _chat(manager, "work", repo_path=str(repo))
    project = manager.get_project(chat.project_id)
    original = routes_api._resolve_file_mention_batch

    def mutate_then_resolve(roots, paths):
        if mutation == "reroot":
            write_receipt(config.state_path.parent, {"status": "migrated", "workspaces": ["work", "other"]})
            reset_reroot_cache()
        elif mutation == "project_move":
            project.workspace = "other"
        elif mutation == "code_mode":
            project.repo_path = ""
        elif mutation == "repo_location":
            chat.repo_location = {"path": str(tmp_path / "gone"), "exists": True, "is_worktree": True, "branch": "x"}
        else:
            manager.delete_chat(chat.chat_id)
        return original(roots, paths)

    monkeypatch.setattr(routes_api, "_resolve_file_mention_batch", mutate_then_resolve)
    response = _post(_client(manager, config), chat.chat_id, ["notes.md"])
    if mutation == "chat_deleted":
        assert response.status_code == 404
    else:
        assert _results(response) == [None]


def test_metadata_failures_are_null_and_no_store(tmp_path):
    config = _legacy_config(tmp_path)
    vault = config.workspace_vault_root("work")
    good = _write(vault / "good.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    response = _post(_client(manager, config), chat.chat_id, ["a" * 300 + ".md", "good.md"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"results"}
    assert all(set(row) == {"input", "path"} for row in response.json()["results"])
    assert [row["path"] for row in response.json()["results"]] == [None, _canon(good)]


def test_unicode_narrow_no_break_space_is_not_rejected(tmp_path):
    config = _legacy_config(tmp_path)
    name = "Screenshot 2026\u202f11.md"
    note = _write(config.workspace_vault_root("work") / name)
    manager = _manager(config)
    chat = _chat(manager, "work")
    client = _client(manager, config)
    assert _results(_post(client, chat.chat_id, [name, "Screenshot 2026\u202f12.md"])) == [_canon(note), None]


def test_browser_auth_origin_and_remote_session_boundary(tmp_path):
    config = _legacy_config(tmp_path)
    note = _write(config.workspace_vault_root("work") / "notes" / "a.md")
    manager = _manager(config)
    chat = _chat(manager, "work")
    serializer = make_serializer("test-secret")
    app = _app(manager, config, middleware=_production_middleware(serializer))
    url = URL.format(chat_id=chat.chat_id)
    body = {"paths": ["notes/a.md"]}

    anonymous = TestClient(app, raise_server_exceptions=False).post(url, json=body)
    assert anonymous.status_code in (401, 403)

    signed = TestClient(app, raise_server_exceptions=False)
    signed.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    foreign = signed.post(url, json=body, headers={"origin": "http://evil.example"})
    assert foreign.status_code == 403
    assert _results(signed.post(url, json=body, headers={"origin": "http://testserver"})) == [_canon(note)]

    remote = TestClient(app, raise_server_exceptions=False, client=("203.0.113.9", 5000))
    remote.cookies.set("ciao_session", serializer.dumps({"user": "owner"}))
    assert _results(remote.post(url, json=body, headers={"origin": "http://testserver"})) == [_canon(note)]


def test_linked_repo_containing_another_workspace_is_not_a_root(tmp_path):
    # The repo is the parent of the install root, so it contains every workspace vault.
    outer = _git_repo(tmp_path / "outer")
    config = _legacy_config(outer)
    vault_note = _write(config.workspace_vault_root("work") / "notes" / "a.md")
    manager = _manager(config)
    chat = _chat(manager, "work", repo_path=str(outer))
    asyncio.run(manager.refresh_repo_location(chat.chat_id, []))
    client = _client(manager, config)
    # Committed only in the repo: it must not be reachable through a repo root that contains other vaults.
    assert _results(_post(client, chat.chat_id, ["docs/guide.md"])) == [None]
    # The owning vault is unaffected by the rejected repo root.
    assert _results(_post(client, chat.chat_id, ["notes/a.md"])) == [_canon(vault_note)]
