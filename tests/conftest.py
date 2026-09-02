from __future__ import annotations

import pytest
from pathlib import Path
from ciao import config as ciao_config
from ciao import job_runs as jr
from ciao import proposal_outcomes as po
from ciao import transcripts
from types import SimpleNamespace


@pytest.fixture(autouse=True)
def _reset_exported_dotenv() -> None:
    """Undo any workspace ``.env`` a test exported into ``os.environ``.

    ``CiaoConfig.from_env()`` applies the workspace's ``.env`` through
    ``load_dotenv``, which is deliberate — provider subprocesses read
    ``${NAME}`` credentials out of the environment they inherit. But it sets
    keys the process has never seen and nothing restores them, and
    ``monkeypatch`` cannot undo a key it never saw set, so one test's fixture
    workspace leaked into every test after it.

    That is not merely untidy. A ``.env`` sets ``CIAO_RUNTIME_ROOT=.runtime``,
    which is relative; leaked into a later test whose ``CIAO_WORKSPACE`` is
    unset, it resolved against the cwd and sent that test's outcome log into
    the repository checkout's own ``.runtime`` instead of its ``tmp_path``.
    """
    ciao_config.reset_exported_dotenv()
    yield
    ciao_config.reset_exported_dotenv()


@pytest.fixture(autouse=True)
def _reset_claude_session_scan_cache() -> None:
    """Drop the cross-test ``~/.claude/projects`` listing cache.

    The cache in ``transcripts`` is module-level and keyed by the projects
    root; tests that monkeypatch ``Path.home`` would otherwise serve the
    previous test's directory listing within the TTL window.
    """
    transcripts._global_session_scan_cache = None
    yield
    transcripts._global_session_scan_cache = None


@pytest.fixture(autouse=True)
def _isolate_ciao_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the developer's own `~/.ciao`.

    `fts_search.get_db_path()` falls back to `~/.ciao/vault-fts.db` when
    `CIAO_MEMORY_DIR` is unset, and anything that rebuilds the search index
    without an explicit `db_path` lands there. A test that migrated a fixture
    install did exactly that: it wiped the real database and refilled it with
    four fixture notes, so vault search on this machine returned almost nothing
    until it was rebuilt.

    Autouse and unconditional on purpose. Remembering to set it per test is the
    thing that failed, and the blast radius is the developer's own data.
    """
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / ".ciao"))


@pytest.fixture(autouse=True)
def _isolate_launch_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test write the developer's own `~/Library/LaunchAgents`.

    `setup_workspace()` defaults `launch_agents_dir` to the real one, so every
    test that called it rewrote `com.ciao.server.plist` — repointing the live
    LaunchAgent at a pytest tmpdir. It was silent: the suite passed while the
    operator's engine was relaunched against a temp workspace and reindexed
    their vault database with fixture notes.

    Autouse and unconditional, for the same reason as `_isolate_ciao_home`:
    passing the argument per test is precisely the step that gets forgotten.
    """
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path / "LaunchAgents"))


@pytest.fixture(autouse=True)
def _isolate_job_runs(tmp_path: Path) -> None:
    """Isolate job runs recording by pointing to a temp directory for every test.

    Also resets the live-state globals: the publisher and the in-flight registry
    are module-level, so a test that installs a sink or leaves a run open would
    otherwise leak into every test after it.
    """
    jr.configure(tmp_path)
    jr.set_publisher(None)
    jr._inflight.clear()
    yield
    jr._runtime_dir_override = None
    jr.set_publisher(None)
    jr._inflight.clear()


@pytest.fixture(autouse=True)
def _isolate_proposal_outcomes(tmp_path: Path) -> None:
    """Isolate proposal-outcome recording the same way ``_isolate_job_runs``
    isolates the job-run log: without this, any route test exercising an
    accept/dismiss would append to the developer's real ``.runtime``."""
    po.configure(tmp_path)
    po.reset_tally_cache()
    yield
    po._runtime_dir_override = None
    po.reset_tally_cache()


def attach_stub_mcp(manager):
    """Give a test-built ``ProjectChatManager`` a stub MCP control plane.

    The Ciaobot MCP server is mandatory: ``build_agent_request`` raises
    ``McpUnavailableError`` without one, so any test that dispatches a turn
    needs a service the way a running server always has one.
    """
    manager._mcp_service = SimpleNamespace(
        credentials_for_chat=lambda chat, project: (
            "http://127.0.0.1:8443/mcp/",
            "tok-test",
        )
    )
    return manager
