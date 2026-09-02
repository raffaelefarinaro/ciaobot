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

from ciao import insights, subagent_tracking
from ciao.transcripts import _claude_projects_dir
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


def test_local_session_jsonl_paths_keeps_every_cross_cwd_match(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: a session present under several cwd slugs returns them all.

    The interim implementation returned after the first match, so subagent
    progress records stored in the other file were dropped by
    _local_subagent_transcripts.
    """
    from ciao import transcripts

    session = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    root = fake_home / "a"
    preferred = _write_session(_projects_dir_for(root), session)
    elsewhere_dir = fake_home / ".claude" / "projects" / "-elsewhere"
    elsewhere = _write_session(elsewhere_dir, session)
    third_dir = fake_home / ".claude" / "projects" / "-third"
    third = _write_session(third_dir, session)

    # A stale cache from a prior lookup must not hide the other matches:
    # _global_session_matches stats each slug live. Seed the cache to prove
    # freshness comes from the per-slug stat, not the cache age.
    transcripts._global_session_scan_cache = None
    paths = routes_api._local_session_jsonl_paths(session, root)
    assert paths == [preferred, elsewhere, third]

    # Order: preferred (workspace root) first, then cross-cwd matches.
    assert paths[0] == preferred
    assert set(paths[1:]) == {elsewhere, third}


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


# -- archiving reads and reclaims the chat's OWN root -------------------------
#
# `_read_archive_inputs` resolved the Claude session blob against
# `config.workspace_root`, but a workspace chat's blob is keyed by the agent
# root it ran in. On a re-rooted install that lookup returned None for every
# workspace-scoped chat, and a None there is indistinguishable from "nothing to
# extract": `run_archive_postprocess` gates on `outcome.filtered_jsonl`, so
# insights, the project-doc fold, the trajectory and memory proposals were all
# skipped in silence — no job run, no log line. Observed live: 26 of 26 Claude
# archives over two days had no insights section while every opencode archive
# (which uses the provider-neutral transcript instead) had one.


def _rerooted_manager(tmp_path: Path, workspace: str) -> ProjectChatManager:
    """A manager on a re-rooted install with one registered workspace."""
    from ciao.config import WorkspaceConfig, reset_reroot_cache

    receipt = tmp_path / ".runtime" / "migration" / "workspace-rooting.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    reset_reroot_cache()

    pcm = _make_manager(tmp_path)
    pcm._config.workspaces = {
        workspace: WorkspaceConfig(
            name=workspace, vault_root=str(tmp_path / workspace / "memory-vault")
        )
    }
    return pcm


def _archived_chat(pcm: ProjectChatManager, workspace: str, session: str) -> ChatInfo:
    project = pcm.create_project("General", workspace)
    chat = pcm.create_chat(project.project_id, title="t", model="sonnet")
    chat.session_id = session
    chat.provider = "claude"
    return chat


def test_archive_inputs_read_the_chats_own_agent_root(
    tmp_path: Path, fake_home: Path
) -> None:
    from ciao.models import ChatContext

    pcm = _rerooted_manager(tmp_path, "work")
    session = "33333333-3333-3333-3333-333333333333"
    chat = _archived_chat(pcm, "work", session)
    agent_root = pcm._agent_root_for_chat(chat.chat_id)
    assert agent_root != pcm._config.workspace_root
    _write_session(_projects_dir_for(agent_root), session)

    _, filtered, _ = pcm._read_archive_inputs(
        chat.chat_id, ChatContext.for_web(chat.chat_id), chat, agent_root
    )
    assert filtered, "the chat's own agent root holds the blob"


def test_archive_inputs_miss_a_blob_under_the_install_root(
    tmp_path: Path, fake_home: Path
) -> None:
    """The bug's shape: a blob written under the install root is not this
    chat's, and must not be read as if it were."""
    from ciao.models import ChatContext

    pcm = _rerooted_manager(tmp_path, "work")
    session = "44444444-4444-4444-4444-444444444444"
    chat = _archived_chat(pcm, "work", session)
    _write_session(_projects_dir_for(pcm._config.workspace_root), session)

    _, filtered, _ = pcm._read_archive_inputs(
        chat.chat_id,
        ChatContext.for_web(chat.chat_id),
        chat,
        pcm._agent_root_for_chat(chat.chat_id),
    )
    assert not filtered


def test_reclaim_targets_the_chats_agent_root(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The blob lives under the root the chat ran in, so that is the directory
    reclaim must hand the SDK. Pointing it at ``workspace_root`` deleted
    nothing and leaked a session blob per archived workspace chat."""
    import asyncio

    pcm = _rerooted_manager(tmp_path, "work")
    session = "55555555-5555-5555-5555-555555555555"
    chat = _archived_chat(pcm, "work", session)

    seen: list[Path] = []
    monkeypatch.setattr(
        pcm._transcripts,
        "delete_sdk_session_blob",
        lambda root, sid: (seen.append(Path(root)), True)[1],
    )

    asyncio.run(pcm._reclaim_provider_sessions_async(chat, [session]))
    assert seen == [pcm._agent_root_for_chat(chat.chat_id)]
    assert seen != [pcm._config.workspace_root]


def test_delete_reclaims_against_the_chats_root_not_the_primary(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``delete_chat`` pops the row before scheduling the reclaim.

    ``_agent_root_for_chat`` resolves through ``self._chats``, so resolving it
    inside the (async) cleanup found no chat, fell back to ``primary_workspace``
    and reclaimed the wrong root — leaving the deleted chat's blob on disk while
    reporting success. The root has to be captured before the pop.
    """
    import asyncio

    pcm = _rerooted_manager(tmp_path, "work")
    pcm._config.workspaces["personal"] = pcm._config.workspaces["work"].__class__(
        name="personal", vault_root=str(tmp_path / "personal" / "memory-vault")
    )
    session = "66666666-6666-6666-6666-666666666666"
    chat = _archived_chat(pcm, "work", session)
    expected = pcm._agent_root_for_chat(chat.chat_id)

    seen: list[Path] = []
    monkeypatch.setattr(
        pcm._transcripts,
        "delete_sdk_session_blob",
        lambda root, sid: (seen.append(Path(root)), True)[1],
    )

    async def run() -> None:
        assert pcm.delete_chat(chat.chat_id) is True
        # Let the fire-and-forget cleanup task run.
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert seen == [expected]
