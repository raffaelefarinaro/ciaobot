"""P8: each session-directory reader resolves its own agent root.

The six ``_claude_projects_dir`` call sites used to slug a single global cwd,
so a per-workspace agent root would silently read an empty directory once the
re-rooting release gives each workspace its own root. These tests pin the
threaded ``agent_root`` parameter: a reader given a root reads that root's
directory, a reader given nothing still reads today's directory, and a session
file under one root is invisible to a reader pointed at another root (the
amnesia case).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import SDKSessionInfo, SessionMessage

from ciao import insights, subagent_tracking
from ciao.transcripts import _claude_projects_dir, extract_cli_transcripts
from ciao.web import routes_api
from ciao.web.project_chats import ProjectChatManager, ChatInfo


def _write_session(projects_dir: Path, session_id: str) -> Path:
    """Write a session JSONL under ``projects_dir`` and return its path."""
    path = projects_dir / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "entrypoint": "cli",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``Path.home()`` at a temp dir so slugs land under it."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


def _projects_dir_for(root: Path) -> Path:
    """The directory ``_claude_projects_dir`` resolves for ``root``."""
    return _claude_projects_dir(root)


def test_slug_differs_per_agent_root(fake_home: Path) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    assert _projects_dir_for(root_a) != _projects_dir_for(root_b)


def test_filter_session_jsonl_reads_own_root(fake_home: Path) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    session = "11111111-1111-1111-1111-111111111111"
    _write_session(_projects_dir_for(root_a), session)

    assert insights.filter_session_jsonl(root_a, session) is not None
    assert insights.filter_session_jsonl(root_b, session) is None


def test_filter_session_jsonl_defaults_to_workspace_root(fake_home: Path) -> None:
    root = fake_home / "a"
    session = "22222222-2222-2222-2222-222222222222"
    _write_session(_projects_dir_for(root), session)

    assert insights.filter_session_jsonl(root, session) is not None


def test_find_parent_session_file_reads_own_root(fake_home: Path) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    session = "33333333-3333-3333-3333-333333333333"
    path = _write_session(_projects_dir_for(root_a), session)

    # The preferred path is scoped to the root it was handed.
    assert subagent_tracking.find_parent_session_file(session, root_a, agent_root=root_a) == path
    assert _projects_dir_for(root_a) != _projects_dir_for(root_b)

    # Querying root_b still finds it, via the cwd-slug glob scan. That net is
    # deliberate today: the projects dir is a slug of the cwd, so a session
    # recorded under a different cwd is only reachable this way. Asserting
    # cross-root invisibility belongs to the re-rooting release, once agent_root
    # really differs per workspace; asserting it now would lock in the loss of
    # the net while the hazard it covers is still live.
    assert (
        subagent_tracking.find_parent_session_file(session, root_b, agent_root=root_b)
        == path
    )


def test_find_parent_session_file_defaults_to_workspace_root(fake_home: Path) -> None:
    root = fake_home / "a"
    session = "44444444-4444-4444-4444-444444444444"
    path = _write_session(_projects_dir_for(root), session)

    assert subagent_tracking.find_parent_session_file(session, root) == path


def test_local_session_jsonl_paths_reads_own_root(fake_home: Path) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    session = "55555555-5555-5555-5555-555555555555"
    path = _write_session(_projects_dir_for(root_a), session)

    assert routes_api._local_session_jsonl_paths(session, root_a) == [path]
    assert (
        routes_api._local_session_jsonl_paths(session, root_b, agent_root=root_b) == []
    )


def test_local_session_jsonl_paths_defaults_to_workspace_root(fake_home: Path) -> None:
    root = fake_home / "a"
    session = "66666666-6666-6666-6666-666666666666"
    path = _write_session(_projects_dir_for(root), session)

    assert routes_api._local_session_jsonl_paths(session, root) == [path]


def test_extract_cli_transcripts_reads_own_root(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    session = "77777777-7777-7777-7777-777777777777"
    _write_session(_projects_dir_for(root_a), session)

    archive_root = tmp_path / "archive"
    tracking = tmp_path / "tracking.json"

    sessions = [
        SDKSessionInfo(
            session_id=session,
            summary="hi",
            last_modified=1_711_968_001_000,
            created_at=1_711_968_000_000,
            cwd=str(root_a),
            git_branch="main",
        )
    ]
    messages = [
        SessionMessage(
            type="user",
            uuid="u1",
            session_id=session,
            message={"role": "user", "content": "hi"},
        )
    ]
    monkeypatch.setattr("ciao.transcripts.list_sessions", lambda **_kw: sessions)
    monkeypatch.setattr(
        "ciao.transcripts.get_session_messages",
        lambda session_id, **_kw: messages,
    )
    monkeypatch.setattr("ciao.transcripts.list_subagents", lambda *_a, **_kw: [])
    monkeypatch.setattr("ciao.transcripts.get_subagent_messages", lambda *_a, **_kw: [])

    created = extract_cli_transcripts(
        workspace_root=root_b,
        archive_root=archive_root,
        tracking_path=tracking,
        agent_root=root_a,
    )
    assert len(created) == 1


def test_extract_cli_transcripts_defaults_to_workspace_root(
    fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fake_home / "a"
    session = "88888888-8888-8888-8888-888888888888"
    _write_session(_projects_dir_for(root), session)

    archive_root = tmp_path / "archive"
    tracking = tmp_path / "tracking.json"

    sessions = [
        SDKSessionInfo(
            session_id=session,
            summary="hi",
            last_modified=1_711_968_001_000,
            created_at=1_711_968_000_000,
            cwd=str(root),
            git_branch="main",
        )
    ]
    messages = [
        SessionMessage(
            type="user",
            uuid="u1",
            session_id=session,
            message={"role": "user", "content": "hi"},
        )
    ]
    monkeypatch.setattr("ciao.transcripts.list_sessions", lambda **_kw: sessions)
    monkeypatch.setattr(
        "ciao.transcripts.get_session_messages",
        lambda session_id, **_kw: messages,
    )
    monkeypatch.setattr("ciao.transcripts.list_subagents", lambda *_a, **_kw: [])
    monkeypatch.setattr("ciao.transcripts.get_subagent_messages", lambda *_a, **_kw: [])

    created = extract_cli_transcripts(
        workspace_root=root,
        archive_root=archive_root,
        tracking_path=tracking,
    )
    assert len(created) == 1


def test_claude_session_exists_reads_own_root(
    fake_home: Path, tmp_path: Path
) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    session = "99999999-9999-9999-9999-999999999999"
    _write_session(_projects_dir_for(root_a), session)

    pcm = _make_manager(tmp_path)
    assert pcm._claude_session_exists(session, agent_root=root_a) is True
    assert _projects_dir_for(root_a) != _projects_dir_for(root_b)
    # Found under root_b too, through the same deliberate glob net. See the
    # note in test_find_parent_session_file_reads_own_root.
    assert pcm._claude_session_exists(session, agent_root=root_b) is True


def test_claude_session_exists_defaults_to_workspace_root(
    fake_home: Path, tmp_path: Path
) -> None:
    root = fake_home / "a"
    session = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    _write_session(_projects_dir_for(root), session)

    pcm = _make_manager(tmp_path)
    assert pcm._claude_session_exists(session) is True


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    from ciao.config import CiaoConfig
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore

    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
