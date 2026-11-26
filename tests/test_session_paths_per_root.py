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

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from ciao import claude_sessions
from ciao.models import AgentRequest, ChatContext
from ciao.providers.base import BaseProvider, ProviderCapabilities
from ciao.providers.acp.agents import OpencodeAcpProvider
from ciao.transcripts import _claude_projects_dir
from ciao.web.project_chats import ProjectChatManager, ChatInfo


# -- persisted-session reclamation seam ---------------------------------------
#
# The reclaim path used to be manager-owned and two-armed (a Claude SDK blob
# delete or an OpenCode HTTP thread delete), so a third registered adapter
# could not reclaim its sessions. These tests pin the shared
# ``BaseProvider.delete_session`` classmethod contract — its tri-state
# confirmation default, each built-in adapter's transport, the manager's
# registry-mediated dispatch, and the disconnect-before-reclaim ordering the
# delete/archive/reset lifecycle paths rely on.

_RECLAIM_LOGGER = "ciao.web.project_chats"


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
    return home


def _projects_dir_for(root: Path) -> Path:
    """The directory ``_claude_projects_dir`` resolves for ``root``."""
    return _claude_projects_dir(root)


def test_slug_differs_per_agent_root(fake_home: Path) -> None:
    root_a = fake_home / "a"
    root_b = fake_home / "b"
    assert _projects_dir_for(root_a) != _projects_dir_for(root_b)


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
    # Found under root_b too: the projects dir is a slug of the cwd, so a
    # session recorded under a different cwd is only reachable through the
    # glob scan, which is deliberate.
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


# -- archiving reclaims the chat's OWN root -----------------------------------
#
# A workspace chat's session blob is keyed by the agent root it ran in, not by
# `config.workspace_root`, so reclaim has to target that root.


def _per_root_manager(tmp_path: Path, workspace: str) -> ProjectChatManager:
    """A manager on a re-rooted install with one registered workspace."""
    from ciao.config import WorkspaceConfig


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


def test_reclaim_targets_the_chats_agent_root(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The blob lives under the root the chat ran in, so that is the directory
    reclaim must hand the SDK. Pointing it at ``workspace_root`` deleted
    nothing and leaked a session blob per archived workspace chat."""
    pcm = _per_root_manager(tmp_path, "work")
    session = "55555555-5555-5555-5555-555555555555"
    chat = _archived_chat(pcm, "work", session)

    seen: list[tuple[str, str]] = []

    def recording_delete(session_id: str, directory: str | None) -> bool:
        seen.append((session_id, directory or ""))
        return False

    monkeypatch.setattr(claude_sessions, "delete_session_file", recording_delete)

    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(chat, [session]))
    expected = pcm._agent_root_for_chat(chat.chat_id)
    assert seen == [(session, str(expected))]
    assert str(expected) != str(pcm._config.workspace_root)
    # Claude's confirmation value carries no signal, so the host cleanup
    # warning never fires for its reclaims.
    assert [record for record in caplog.records] == []


def test_delete_reclaims_against_the_chats_root_not_the_primary(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``delete_chat`` pops the row before scheduling the reclaim.

    ``_agent_root_for_chat`` resolves through ``self._chats``, so resolving it
    inside the (async) cleanup found no chat, fell back to ``primary_workspace``
    and reclaimed the wrong root — leaving the deleted chat's blob on disk while
    reporting success. The root has to be captured before the pop.
    """
    pcm = _per_root_manager(tmp_path, "work")
    pcm._config.workspaces["personal"] = pcm._config.workspaces["work"].__class__(
        name="personal", vault_root=str(tmp_path / "personal" / "memory-vault")
    )
    session = "66666666-6666-6666-6666-666666666666"
    chat = _archived_chat(pcm, "work", session)
    expected = pcm._agent_root_for_chat(chat.chat_id)

    seen: list[tuple[str, str]] = []
    # The cleanup runs the file delete in a worker thread, so the signal is a
    # threading.Event, awaited off the loop.
    cleanup_done = threading.Event()

    def recording_delete(session_id: str, directory: str | None) -> bool:
        seen.append((session_id, directory or ""))
        cleanup_done.set()
        return False

    monkeypatch.setattr(claude_sessions, "delete_session_file", recording_delete)

    async def run() -> None:
        assert pcm.delete_chat(chat.chat_id) is True
        # The fire-and-forget cleanup ends at the file delete call.
        assert await asyncio.wait_for(asyncio.to_thread(cleanup_done.wait, 5), 10)

    asyncio.run(run())
    assert seen == [(session, str(expected))]
    assert str(expected) != str(pcm._config.agent_root("personal"))


def _make_no_start_adapter(provider_id: str) -> Any:
    """Build an adapter class whose live-turn entry points count before raising.

    ``_reclaim_provider_sessions_async`` is fail-open by design, so an
    assertion raised inside it would be swallowed into a log record. The
    counters make every forbidden invocation independently observable after
    the awaited cleanup returns.
    """
    counts: dict[str, int] = {"constructor": 0, "stream": 0}

    class _NoStartAdapter(BaseProvider):
        def __init__(
            self, workspace_root: Path, *, config: object | None = None
        ) -> None:
            _NoStartAdapter.invocation_counts["constructor"] += 1
            raise AssertionError("reclamation must not construct a live provider")

        def run_streaming(self, request: AgentRequest, register_handle: Any) -> Any:
            _NoStartAdapter.invocation_counts["stream"] += 1
            raise AssertionError("reclamation must not start a streaming turn")

    _NoStartAdapter.invocation_counts = counts
    return _NoStartAdapter


class _SyntheticDescriptor:
    """Registry look-alike handing out a test adapter class."""

    def __init__(self, provider_id: str, factory: Any) -> None:
        self.id = provider_id
        self._factory = factory
        self.factory_calls: list[str] = []

    def factory(self) -> Any:
        self.factory_calls.append(self.id)
        return self._factory


def _install_registry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_id: str,
    descriptor: Any,
) -> None:
    """Route ``provider_registry.get`` for *provider_id* to a synthetic descriptor.

    Other ids keep resolving through the real registry, so the test observes
    only the id it names and a path that never touches the registry stays red.
    """
    from ciao import provider_registry

    real_get = provider_registry.get

    def synthetic_get(pid: str) -> Any:
        if pid == provider_id:
            return descriptor
        return real_get(pid)

    monkeypatch.setattr(provider_registry, "get", synthetic_get)


_FORBIDDEN_START_KEYS = (
    "service_constructed",
    "service_started",
    "service_provider_built",
    "builtin_reader",
    "server_spawned",
)


def _install_forbidden_start_counters(
    monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]
) -> None:
    """Count every forbidden live-provider start, built-in reader and SDK call.

    ``_reclaim_provider_sessions_async`` swallows ordinary exceptions on
    purpose, so the counters, not a raised AssertionError, are the assert
    surface: each wrapper increments before raising, and the test checks the
    counters after the awaited cleanup path has fully returned.
    """
    import ciao.provider_service

    counts.update(dict.fromkeys(_FORBIDDEN_START_KEYS, 0))

    def counting_service_init(self, *args: Any, **kwargs: Any) -> None:
        counts["service_constructed"] += 1
        raise AssertionError("cleanup must not construct a ProviderService")

    monkeypatch.setattr(
        ciao.provider_service.ProviderService, "__init__", counting_service_init
    )

    def counting_service_start(self, *args: Any, **kwargs: Any) -> Any:
        counts["service_started"] += 1
        raise AssertionError("cleanup must not start a streaming turn")

    monkeypatch.setattr(
        ciao.provider_service.ProviderService,
        "execute_streaming",
        counting_service_start,
    )

    def counting_service_build(self, *args: Any, **kwargs: Any) -> Any:
        counts["service_provider_built"] += 1
        raise AssertionError("cleanup must not build a live provider instance")

    monkeypatch.setattr(
        ciao.provider_service.ProviderService,
        "ensure_provider",
        counting_service_build,
    )

    def counting_read_thread(self, *args: Any, **kwargs: Any) -> Any:
        counts["builtin_reader"] += 1
        raise AssertionError("cleanup must not fall back to built-in readers")

    monkeypatch.setattr(OpencodeAcpProvider, "read_thread", counting_read_thread)



def test_base_delete_session_default_supplies_no_signal(tmp_path: Path) -> None:
    """The base default is a quiet no-reclaim: side-effect-free, no turn.

    An adapter inheriting the default intentionally does no reclaim, so the
    host must not warn on it, construct a provider or start a stream.
    """
    counts: dict[str, int] = {"constructor": 0, "stream": 0}

    class _MinimalAdapter(BaseProvider):
        def __init__(
            self, workspace_root: Path, *, config: object | None = None
        ) -> None:
            counts["constructor"] += 1
            raise AssertionError("the default seam must not construct a provider")

        def run_streaming(self, request: AgentRequest, register_handle: Any) -> Any:
            counts["stream"] += 1
            raise AssertionError("the default seam must not start a turn")

    root = tmp_path / "agent-root"
    assert counts == {"constructor": 0, "stream": 0}
    result = asyncio.run(_MinimalAdapter.delete_session(root, "sid-1", runtime_root=Path("/runtime")))
    assert result is None
    # Both forbidden entry points stayed uninvoked through the await.
    assert counts == {"constructor": 0, "stream": 0}
    # No capability flag was added for the operation.
    assert _MinimalAdapter.capabilities == ProviderCapabilities()


class _RecordingServer:
    """Async context-manager stand-in for ``_EphemeralServer``."""

    contexts: list[dict[str, Any]] = []

    def __init__(self, workspace_root: Path) -> None:
        _RecordingServer.last_root = str(workspace_root)

    async def __aenter__(self) -> Any:
        self.state: dict[str, Any] = {
            "entered": True,
            "client": _RecordingServer.client,
        }
        _RecordingServer.contexts.append(self.state)
        return self.state["client"]

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.state["exit_error"] = exc


def _acme_chat(pcm: ProjectChatManager, session_id: str, lineage: list[str]) -> ChatInfo:
    """A work-workspace chat owned by the synthetic third adapter."""
    chat = _archived_chat(pcm, "work", session_id)
    chat.provider = "acme"
    chat.previous_session_ids = lineage
    return chat


def test_third_registered_adapter_reclaims_through_the_registry(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered non-built-in adapter reclaims its own sessions.

    The manager resolves the descriptor per provider id and awaits the shared
    classmethod seam with the chat's own agent root; first-seen nonempty
    lineage ids are passed once, in original order. No live provider is ever
    constructed or streamed.
    """
    pcm = _per_root_manager(tmp_path, "work")
    chat = _acme_chat(pcm, "current-old", ["old-a", "", "old-b", "old-a"])

    deletes: list[tuple[str, str]] = []
    runtime_roots: list[Path] = []

    class _AcmeAdapter(_make_no_start_adapter("acme")):
        @classmethod
        async def delete_session(
            cls, workspace_root: Path, session_id: str, *, runtime_root: Path
        ) -> bool | None:
            deletes.append((str(workspace_root), session_id))
            runtime_roots.append(runtime_root)
            return True

    descriptor = _SyntheticDescriptor("acme", _AcmeAdapter)
    _install_registry(monkeypatch, provider_id="acme", descriptor=descriptor)

    counts: dict[str, int] = {}
    _install_forbidden_start_counters(monkeypatch, counts)

    asyncio.run(pcm._reclaim_provider_sessions_async(chat))

    root = str(pcm._agent_root_for_chat(chat.chat_id))
    assert deletes == [
        (root, "old-a"),
        (root, "old-b"),
        (root, "current-old"),
    ]
    # The chat's runtime root is the config's state directory, never the workspace.
    assert runtime_roots == [pcm._config.state_path.parent] * 3
    # The seam was actually reached once per reclaimed id: a skipped path
    # cannot satisfy the positive counts alongside the zero no-start counts.
    assert descriptor.factory_calls == ["acme", "acme", "acme"]
    assert _AcmeAdapter.invocation_counts == {"constructor": 0, "stream": 0}
    # No ProviderService was constructed or started, no built-in reader or
    # SDK call fired, and no subprocess ran — observable even though the
    # fail-open boundary would swallow any raising spy's error.
    assert counts == dict.fromkeys(_FORBIDDEN_START_KEYS, 0)


def test_reclaim_confirms_only_a_literal_false(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Only ``False`` requests the unchanged cleanup confirmation warning.

    ``True`` is confirmation, ``None`` supplies no signal (including from the
    base default), and an unknown provider id never reaches an adapter —
    neither may produce a warning or start a provider.
    """
    pcm = _per_root_manager(tmp_path, "work")
    # The current id repeats the lineage's last entry: first-seen dedup keeps
    # the reclaim at exactly the three successive confirmations.
    chat = _acme_chat(pcm, "s-none", ["s-true", "s-false", "s-none"])

    counts: dict[str, int] = {}
    _install_forbidden_start_counters(monkeypatch, counts)

    confirmations = [True, False, None]
    warnings_seen: list[str] = []

    class _ConfirmAdapter(_make_no_start_adapter("acme")):
        @classmethod
        async def delete_session(
            cls, workspace_root: Path, session_id: str, *, runtime_root: Path
        ) -> bool | None:
            return confirmations.pop(0)

    descriptor = _SyntheticDescriptor("acme", _ConfirmAdapter)
    _install_registry(monkeypatch, provider_id="acme", descriptor=descriptor)

    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(chat))
    warnings_seen = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings_seen) == 1
    assert chat.provider in warnings_seen[0]
    assert "s-false" in warnings_seen[0]
    assert chat.chat_id in warnings_seen[0]

    # The base default (a descriptor whose adapter never overrides the
    # operation) is quiet: no warning, no provider started.
    class _BareAdapter(_make_no_start_adapter("bare")):
        pass

    bare_project = pcm.create_project("Bare", "work")
    bare = pcm.create_chat(bare_project.project_id, title="bare", model="sonnet")
    bare.provider = "bare"
    bare.session_id = "bare-session"
    caplog.clear()
    descriptor = _SyntheticDescriptor("bare", _BareAdapter)
    _install_registry(monkeypatch, provider_id="bare", descriptor=descriptor)
    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(bare))
    assert [r.levelno for r in caplog.records] == []
    assert descriptor.factory_calls == ["bare"]
    assert _BareAdapter.invocation_counts == {"constructor": 0, "stream": 0}

    # An unknown provider id stays quiet and never resolves a descriptor.
    ghost = _acme_chat(pcm, "ghost-session", [])
    ghost.provider = "ghost"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(ghost))
    assert [r.levelno for r in caplog.records] == []
    assert descriptor.factory_calls == ["bare"]  # no new factory resolutions
    # No path — confirmation, unsupported default or unknown id — started a
    # service, reader or subprocess anywhere in the awaited cleanups.
    assert counts == dict.fromkeys(_FORBIDDEN_START_KEYS, 0)


def test_reclaim_is_fail_open_but_cancellation_propagates(
    tmp_path: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A failing session is logged and the next one is still attempted.

    Factory resolution failures share the boundary; a cancelled cleanup is
    not an ordinary failure and escapes the reclaim entirely.
    """
    pcm = _per_root_manager(tmp_path, "work")
    chat = _acme_chat(pcm, "good", ["bad", "good"])

    counts: dict[str, int] = {}
    _install_forbidden_start_counters(monkeypatch, counts)

    deletes: list[str] = []

    class _FlakyAdapter(_make_no_start_adapter("acme")):
        @classmethod
        async def delete_session(
            cls, workspace_root: Path, session_id: str, *, runtime_root: Path
        ) -> bool | None:
            deletes.append(session_id)
            if session_id == "bad":
                raise RuntimeError("cleanup refused")
            return True

    descriptor = _SyntheticDescriptor("acme", _FlakyAdapter)
    _install_registry(monkeypatch, provider_id="acme", descriptor=descriptor)

    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(chat))
    assert deletes == ["bad", "good"]
    failures = [
        record
        for record in caplog.records
        if record.getMessage() == f"Failed to reclaim acme session bad for chat {chat.chat_id}"
    ]
    assert len(failures) == 1
    assert failures[0].exc_info is not None
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    # A factory that cannot resolve its adapter lands on the same boundary.
    class _BrokenDescriptor:
        id = "acme"

        def factory(self) -> Any:
            raise ImportError("adapter module vanished")

    _install_registry(monkeypatch, provider_id="acme", descriptor=_BrokenDescriptor())
    deletes.clear()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_RECLAIM_LOGGER):
        asyncio.run(pcm._reclaim_provider_sessions_async(chat))
    assert deletes == []
    assert len([r for r in caplog.records if r.exc_info]) == 2  # both sessions logged

    # Cancellation must propagate rather than become a swallowed failure.
    class _CancellingAdapter(_make_no_start_adapter("acme")):
        @classmethod
        async def delete_session(
            cls, workspace_root: Path, session_id: str, *, runtime_root: Path
        ) -> bool | None:
            deletes.append(session_id)
            raise asyncio.CancelledError()

    descriptor = _SyntheticDescriptor("acme", _CancellingAdapter)
    _install_registry(monkeypatch, provider_id="acme", descriptor=descriptor)
    deletes.clear()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(pcm._reclaim_provider_sessions_async(chat))
    assert deletes == ["bad"]
    # Fail-open swallowing never hid a forbidden start: no ProviderService,
    # reader or subprocess was touched across all three sub-cases.
    assert counts == dict.fromkeys(_FORBIDDEN_START_KEYS, 0)


@pytest.mark.parametrize("failure_mode", ["false", "raise"])
def test_reset_reclaims_captured_old_lineage_after_disconnect(
    tmp_path: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    failure_mode: str,
) -> None:
    """Reset's scheduled cleanup deletes only the captured old lineage ids.

    The old lineage/current-id snapshot is taken while the reset is still
    under way; disconnect gates reclaim; and a failing old-id deletion — or a
    literal ``False`` — neither stops the remaining ids, rolls back the
    persisted reset/replacement state, nor touches the archived transcript.
    Replacement ids assigned after the reset are invisible to the cleanup.
    """
    pcm = _per_root_manager(tmp_path, "work")
    pcm._config.workspaces["personal"] = pcm._config.workspaces["work"].__class__(
        name="personal", vault_root=str(tmp_path / "personal" / "memory-vault")
    )
    project = pcm.create_project("Reset reclaim", "work")
    chat = pcm.create_chat(project.project_id, title="reset", model="sonnet")
    chat.provider = "opencode"
    chat.previous_session_ids = ["old-a", "", "old-b", "old-a", "current-old", ""]
    chat.session_id = "current-old"
    chat.pending_question = "q"
    chat.pending_permission = "p"
    chat.pending_queue = [{"pending": 1}]
    chat.helper = {"helper": 1}
    chat.context_digest = "old-digest"
    chat.context_session_id = "context-old"

    ctx = ChatContext.for_web(chat.chat_id)
    request = AgentRequest(
        prompt="hello",
        model="sonnet",
        mode="auto",
        provider="opencode",
        display_prompt="hello",
    )
    pcm._transcripts.record_turn(
        request,
        ctx=ctx,
        response_text="archived body",
        effective_model="sonnet",
        session_id="current-old",
        usage={},
        quota={},
        input_kind="text",
        provider="opencode",
    )
    expected_root = str(pcm._agent_root_for_chat(chat.chat_id))

    events: list[str] = []
    deletes: list[tuple[str, str]] = []
    archive = {"path": None, "bytes": b""}

    disconnect_entered = asyncio.Event()
    release_disconnect = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def fake_delete(workspace_root, session_id, *, runtime_root):
        deletes.append((str(workspace_root), session_id))
        events.append(f"delete:{session_id}")
        if session_id == "old-a":
            if failure_mode == "false":
                return False
            raise RuntimeError("cleanup refused")
        if session_id == "current-old":
            cleanup_done.set()
        return None

    class _BlockingDisconnect:
        async def disconnect(self) -> None:
            events.append("disconnect-enter")
            disconnect_entered.set()
            await release_disconnect.wait()
            events.append("disconnect-done")

    real_archive = pcm._transcripts.archive_session

    def recording_archive(*args: Any, **kwargs: Any):
        result = real_archive(*args, **kwargs)
        archive["path"] = result
        events.append("archive-done")
        assert result is not None
        archive["bytes"] = result.read_bytes()
        return result

    monkeypatch.setattr(pcm._transcripts, "archive_session", recording_archive)
    monkeypatch.setattr(OpencodeAcpProvider, "delete_session", fake_delete)
    # The live provider is what reset pops and disconnects before reclaiming;
    # a blocking disconnect gates every deletion deterministically.
    pcm._providers[chat.chat_id] = _BlockingDisconnect()
    counts: dict[str, int] = {"constructor": 0, "stream": 0}
    _install_forbidden_start_counters(monkeypatch, counts)

    real_init = OpencodeAcpProvider.__init__

    def counting_init(self, *args: Any, **kwargs: Any):
        counts.setdefault("constructor", 0)
        counts["constructor"] += 1
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(OpencodeAcpProvider, "__init__", counting_init)

    def counting_stream(self, *args: Any, **kwargs: Any):
        counts.setdefault("stream", 0)
        counts["stream"] += 1
        raise AssertionError("reset cleanup must not stream a turn")

    monkeypatch.setattr(OpencodeAcpProvider, "run_streaming", counting_stream)

    def chat_row() -> dict[str, Any]:
        payload = json.loads(pcm._path.read_text(encoding="utf-8"))
        return payload["chats"][chat.chat_id]

    def persisted_state_session_id() -> str:
        payload = json.loads(pcm._config.state_path.read_text(encoding="utf-8"))
        return payload["contexts"][chat.chat_id]["session"]["session_id"]

    async def run() -> None:
        updated = pcm.new_session(chat.chat_id)
        assert updated is chat
        assert updated.archived is False
        # The cleanup task is scheduled; reach it deterministically up to its
        # disconnect, which is still the gate in front of every deletion.
        await asyncio.wait_for(disconnect_entered.wait(), 10)
        assert deletes == []
        # Archive completed before the reset returned and its bytes are on
        # disk; the reset persisted cleared session/lineage/interaction state.
        assert archive["bytes"].decode("utf-8").find("archived body") != -1
        row = chat_row()
        assert row["session_id"] == ""
        assert row["previous_session_ids"] == []
        assert row["context_digest"] == ""
        assert row["context_session_id"] == ""
        assert row["pending_question"] == ""
        assert row["pending_permission"] == ""
        assert row["pending_queue"] == []
        assert row["helper"] == {}
        assert persisted_state_session_id() == ""
        # A newer session is installed while the old one is still disconnecting.
        updated.session_id = "current-new"
        updated.previous_session_ids = ["new-root"]
        pcm._save()
        row = chat_row()
        assert row["session_id"] == "current-new"
        assert row["previous_session_ids"] == ["new-root"]
        # Still no deletion: disconnect has not finished.
        assert deletes == []
        release_disconnect.set()
        # Explicit completion signal for the whole scheduled cleanup.
        await asyncio.wait_for(cleanup_done.wait(), 10)

    with caplog.at_level(logging.INFO, logger=_RECLAIM_LOGGER):
        asyncio.run(run())

    # Only the captured old lineage, once each, in original order, at the
    # chat's own agent root — never the replacement ids.
    assert deletes == [
        (expected_root, "old-a"),
        (expected_root, "old-b"),
        (expected_root, "current-old"),
    ]
    assert "current-new" not in {sid for _, sid in deletes}
    assert events == [
        "archive-done",
        "disconnect-enter",
        "disconnect-done",
        "delete:old-a",
        "delete:old-b",
        "delete:current-old",
    ]
    # Late cleanup neither rolled back the replacement nor disturbed the
    # archived transcript bytes.
    row = chat_row()
    assert row["session_id"] == "current-new"
    assert row["previous_session_ids"] == ["new-root"]
    assert archive["path"] is not None
    assert archive["path"].read_bytes() == archive["bytes"]
    # The old-id confirmation policy: exactly one warning for a literal
    # False, exactly one logged failure for the raised exception.
    if failure_mode == "false":
        assert [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ] == [
            f"Provider returned no cleanup confirmation for opencode session "
            f"old-a (chat {chat.chat_id})"
        ]
    else:
        failures = [
            r
            for r in caplog.records
            if r.getMessage()
            == f"Failed to reclaim opencode session old-a for chat {chat.chat_id}"
        ]
        assert len(failures) == 1
        assert failures[0].exc_info is not None
    # No provider was constructed, streamed, served, read or connected: the
    # forbidden-seam counters on the shared helper plus the adapter wrappers
    # all stayed at zero through the complete scheduled cleanup.
    expected_zero = dict.fromkeys(
        (*_FORBIDDEN_START_KEYS, "constructor", "stream"), 0
    )
    assert counts == expected_zero
