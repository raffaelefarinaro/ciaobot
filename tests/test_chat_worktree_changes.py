from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao import repo_location
from ciao.sessions import StateStore

from ciao.repo_location import (
    WORKTREE_CHANGES_MAX_BYTES,
    WORKTREE_CHANGES_MAX_FILES,
    read_worktree_changes,
)
from ciao.transcripts import TranscriptStore
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_worktree_changes
from ciao.git_proc import GitCapture, run_git_bounded


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "tracked.txt").write_text("base\n")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "base")


def _manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(pwa_auth_token="test-token", workspace_root=tmp_path,
                        state_path=runtime / "state.json", media_root=runtime / "media")
    return ProjectChatManager(config, state_store=StateStore(config.state_path, tmp_path, config.media_root),
                              transcript_store=TranscriptStore(runtime, runtime / "transcripts"),
                              path=runtime / "web_projects.json")


def _api_client(manager: ProjectChatManager, *, signed_in: bool) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/worktree-changes", chat_worktree_changes, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.project_chat_manager = manager
    client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    if signed_in:
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


@pytest.mark.asyncio
async def test_changes_only_read_the_recorded_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-b", "feature", str(worktree))
    (worktree / "tracked.txt").write_text("staged value\n")
    _git(worktree, "add", "tracked.txt")
    (worktree / "tracked\nname.txt").write_text("tracked newline name\n")
    _git(worktree, "add", "tracked\nname.txt")
    (worktree / "tracked.txt").write_text("unstaged value\n")
    untracked = "line\nbreak.txt"
    (worktree / untracked).write_text("private contents stay out of the response")

    result = await read_worktree_changes(worktree, repo, dedicated=True)

    assert result["state"] == "ready"
    assert result["source"] == "worktree"
    assert "staged value" in result["staged"]
    assert "unstaged value" in result["unstaged"]
    assert untracked in result["untracked"]
    assert "private contents" not in str(result)


@pytest.mark.asyncio
async def test_linked_checkout_may_itself_be_a_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    linked = tmp_path / "linked-checkout"
    _git(repo, "worktree", "add", "--detach", str(linked))
    (linked / "tracked.txt").write_text("change\n")

    result = await read_worktree_changes(linked, linked, dedicated=False)

    assert result["state"] == "ready"
    assert result["source"] == "linked_checkout"
    assert "change" in result["unstaged"]


@pytest.mark.asyncio
async def test_unrelated_and_replaced_worktrees_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "--detach", str(worktree))
    unrelated = tmp_path / "unrelated"
    _repository(unrelated)

    assert (await read_worktree_changes(unrelated, repo, dedicated=True))["state"] == "unavailable"

    original = repo_location.run_git_bounded
    source_identity_reads = 0

    async def replace_registration(cwd: Path, *args: str, **kwargs: object) -> GitCapture:
        nonlocal source_identity_reads
        if "--show-object-format" in args:
            source_identity_reads += 1
            if source_identity_reads == 2:
                _git(repo, "worktree", "remove", "--force", str(worktree))
                _git(unrelated, "worktree", "add", "--detach", str(worktree))
        return await original(cwd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo_location, "run_git_bounded", replace_registration)
    replaced = await read_worktree_changes(worktree, repo, dedicated=True)
    assert replaced["state"] == "unavailable"


@pytest.mark.asyncio
async def test_partial_final_untracked_record_is_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    original = repo_location.run_git_bounded

    async def partial(cwd: Path, *args: str, **kwargs: object) -> GitCapture:
        if "--others" in args:
            return GitCapture(-1, b"unfinished-path", b"", truncated=True)
        return await original(cwd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo_location, "run_git_bounded", partial)
    result = await read_worktree_changes(repo, repo, dedicated=False)
    assert result["state"] == "ready"
    assert result["untracked"] == []
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_linked_checkout_is_supported_and_filter_callbacks_are_not_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.marker.clean", f"touch {marker} && cat")
    (repo / ".gitattributes").write_text("tracked.txt filter=marker\n")

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result == {"state": "unavailable", "reason": "unsupported_checkout"}
    assert not marker.exists()


@pytest.mark.parametrize("metadata", ["alternates", "exclude"])
@pytest.mark.asyncio
async def test_fifo_repository_metadata_is_rejected_without_blocking(
    tmp_path: Path, metadata: str,
) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    relative = ".git/objects/info/alternates" if metadata == "alternates" else ".git/info/exclude"
    pipe = repo / relative
    pipe.unlink(missing_ok=True)
    os.mkfifo(pipe)

    # Git can open alternates during repository discovery, before our explicit
    # metadata inspection. This guards that discovery path as well as the
    # bounded metadata reader.
    result = await asyncio.wait_for(read_worktree_changes(repo, repo, dedicated=False), timeout=2)

    assert result == {"state": "unavailable", "reason": "unsupported_checkout"}


@pytest.mark.parametrize("metadata", ["alternates", "exclude"])
@pytest.mark.asyncio
async def test_oversized_repository_metadata_is_rejected(
    tmp_path: Path, metadata: str,
) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    relative = ".git/objects/info/alternates" if metadata == "alternates" else ".git/info/exclude"
    (repo / relative).write_bytes(b"x" * (WORKTREE_CHANGES_MAX_BYTES + 1))

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result == {"state": "unavailable", "reason": "unsupported_checkout"}


@pytest.mark.asyncio
async def test_external_diff_and_fsmonitor_callbacks_are_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    marker = tmp_path / "callback-ran"
    callback = f"touch {marker}"
    _git(repo, "config", "filter.unused.clean", callback)
    _git(repo, "config", "diff.external", callback)
    _git(repo, "config", "core.fsmonitor", callback)
    (repo / "tracked.txt").write_text("changed\n")

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result["state"] == "ready"
    assert not marker.exists()


@pytest.mark.asyncio
async def test_api_requires_session_and_disables_caching(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _repository(root)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "--detach", str(linked))
    manager = _manager(tmp_path / "install")
    project = manager.create_project("Repository", workspace="personal")
    project.repo_path = str(linked)
    chat = manager.create_chat(project.project_id, title="C")

    with _api_client(manager, signed_in=False) as client:
        assert client.get(f"/api/chats/{chat.chat_id}/worktree-changes").status_code == 401
    with _api_client(manager, signed_in=True) as client:
        response = client.get(f"/api/chats/{chat.chat_id}/worktree-changes")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["source"] == "linked_checkout"
        missing = client.get("/api/chats/unknown/worktree-changes")
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("feature", ["sparse", "sparse_index", "split", "gitlink", "alternates", "partial"])
@pytest.mark.asyncio
async def test_unsupported_repository_features_are_refused(tmp_path: Path, feature: str) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    if feature == "sparse":
        _git(repo, "config", "core.sparseCheckout", "true")
    elif feature == "sparse_index":
        _git(repo, "config", "index.sparse", "true")
    elif feature == "split":
        _git(repo, "update-index", "--split-index")
    elif feature == "gitlink":
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
    elif feature == "alternates":
        alternate = tmp_path / "alternate"
        _repository(alternate)
        alternates = repo / ".git/objects/info/alternates"
        alternates.write_text(str(alternate / ".git/objects"))
    else:
        _git(repo, "config", "extensions.partialClone", "origin")

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result == {"state": "unavailable", "reason": "unsupported_checkout"}


@pytest.mark.asyncio
async def test_worktree_capture_is_bounded_and_caps_untracked_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    index = repo / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    (repo / "tracked.txt").write_text("x" * (WORKTREE_CHANGES_MAX_BYTES + 16) + "\n")

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result["state"] == "ready"
    assert result["truncated"] is True
    assert len(result["unstaged"].encode()) <= WORKTREE_CHANGES_MAX_BYTES
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before

    (repo / "tracked.txt").write_text("base\n")
    for number in range(WORKTREE_CHANGES_MAX_FILES + 1):
        (repo / f"untracked-{number:03}.txt").touch()
    names = await read_worktree_changes(repo, repo, dedicated=False)
    assert len(names["untracked"]) == WORKTREE_CHANGES_MAX_FILES
    assert names["truncated"] is True


@pytest.mark.asyncio
async def test_clean_repository_with_thousands_of_tracked_files_is_supported(tmp_path: Path) -> None:
    repo = tmp_path / "large-repo"
    _repository(repo)
    many = repo / "many"
    many.mkdir()
    for number in range(4500):
        (many / f"file-{number:04}.txt").write_text("content\n")
    _git(repo, "add", "many")
    _git(repo, "commit", "-m", "many tracked files")

    result = await read_worktree_changes(repo, repo, dedicated=False)

    assert result == {
        "state": "ready", "source": "linked_checkout", "staged": "", "unstaged": "",
        "untracked": [], "truncated": False,
    }


@pytest.mark.asyncio
async def test_bounded_reader_marks_overflow_even_when_git_exits_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _repository(repo)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    capture = await run_git_bounded(
        repo, "ls-files", "-z", timeout=5, max_output=1, env=env
    )

    assert capture.truncated is True
    assert len(capture.stdout) + len(capture.stderr) <= 1


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_bounded_child(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "child.pid"
    fake_git = bin_dir / "git"
    fake_git.write_text(f"#!/bin/sh\necho $$ > '{pid_file}'\nexec /bin/sleep 60\n")
    fake_git.chmod(0o755)
    task = asyncio.create_task(run_git_bounded(
        tmp_path, "unused", timeout=30, max_output=128,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
    ))
    for _ in range(200):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_chat_scope_uses_linked_checkout_and_rejects_stale_or_missing_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    repo = tmp_path / "linked"
    repo.mkdir()
    project = manager.create_project("Repository", workspace="personal")
    project.repo_path = str(repo)
    chat = manager.create_chat(project.project_id, title="C")
    assert manager.get_chat(chat.chat_id) is chat
    assert manager._is_known_workspace(project.workspace)
    assert manager.linked_repo_for_chat(chat) == str(repo)
    seen: list[tuple[Path, Path, bool]] = []

    async def read(checkout: Path, linked_repo: Path, *, dedicated: bool) -> dict[str, object]:
        seen.append((checkout, linked_repo, dedicated))
        return {"state": "ready", "source": "linked_checkout"}

    monkeypatch.setattr(repo_location, "read_worktree_changes", read)
    assert await manager.worktree_changes_for_chat(chat.chat_id) == {
        "state": "ready", "source": "linked_checkout"
    }
    assert seen == [(repo, repo, False)]

    chat.archived = True
    assert await manager.worktree_changes_for_chat(chat.chat_id) == {"state": "unavailable"}
    chat.archived = False

    chat.repo_location = {"path": str(tmp_path / "removed"), "is_worktree": True, "exists": False}
    before = len(seen)
    assert await manager.worktree_changes_for_chat(chat.chat_id) == {"state": "unavailable"}
    assert len(seen) == before

    chat.repo_location = {}

    async def relink_during_read(checkout: Path, linked_repo: Path, *, dedicated: bool) -> dict[str, object]:
        project.repo_path = str(tmp_path / "other")
        return {"state": "ready", "source": "linked_checkout"}

    monkeypatch.setattr(repo_location, "read_worktree_changes", relink_during_read)
    assert await manager.worktree_changes_for_chat(chat.chat_id) == {"state": "unavailable"}
    assert await manager.worktree_changes_for_chat("deleted-chat") is None
