from __future__ import annotations

import pytest
from pathlib import Path
from ciao import config as ciao_config
from ciao import job_runs as jr
from ciao import transcripts
from types import SimpleNamespace

# Captured before any fixture runs, so the guard test can prove no test sees it.
REAL_HOME = Path.home()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every test's home at ``tmp_path / "home"``, on every OS.

    ``Path.home()`` reads ``HOME`` on POSIX and ``USERPROFILE`` on Windows
    (``HOMEDRIVE``/``HOMEPATH`` only when that is unset), so a test that faked
    the home with ``HOME`` alone read and wrote the developer's real
    ``~/.claude``, ``~/Applications`` and gws config on Windows (#696), and one
    run's leftovers satisfied the next run's assertions. Autouse and
    unconditional, like ``_isolate_ciao_home``: remembering it per test is what
    failed. The directory is not created; a test that needs files in the home
    uses the ``home_dir`` fixture (same path) and makes them.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


@pytest.fixture
def home_dir(tmp_path: Path) -> Path:
    """This test's isolated home (``_isolate_home``), created."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture(autouse=True)
def _no_installed_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never find the developer's own ``opencode`` on ``PATH``.

    A setup-status or Settings test that reaches the CLI would read the real
    home's OpenCode config through it, the leak ``_isolate_home`` closes; and
    under the isolated home the CLI starts a ``serve --service`` of its own
    that holds the output pipes, so on Windows the call never returns.
    CI has no OpenCode installed, so this is what CI already sees. A test that
    wants a binary sets ``CIAO_OPENCODE_BIN``, which is resolved first.
    """
    monkeypatch.setattr("ciao.providers.opencode.resolve_tool", lambda name: None)


@pytest.fixture(autouse=True)
def _git_stores_exact_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git a test spawns stores exact bytes, as macOS and Linux git do.

    Git for Windows ships ``core.autocrlf=true`` in its system config, so a
    fixture repo a test committed with plain ``git`` held LF blobs under CRLF
    files, and the engine (which passes ``git_proc.EXACT_BYTES``) then saw every
    such file as modified. Environment config overrides every config file and
    is overridden by an explicit ``git -c``; ``tests/test_git_exact_bytes.py``
    sets it back to ``true`` to prove the engine does not rely on this.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


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
def _drop_inherited_bundled_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore ``CIAO_BUNDLED_APP`` inherited from a Ciaobot.app agent shell.

    The bundled launcher exports it, so every command a Ciaobot chat runs -
    including this suite - inherits it, and ``detect_install_mode`` would call
    the test process a packaged app (``admin_deploy`` then refuses up front).
    Tests that need it set it explicitly.
    """
    monkeypatch.delenv("CIAO_BUNDLED_APP", raising=False)


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
def _isolate_install_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test read or write the machine's real install receipt.

    `detect_install_mode()` now consults the installer receipt, so a developer's
    own terminal install would classify the test process as `installer` and a
    test that exercised the receipt would rewrite the receipt their real engine
    depends on. Autouse for the same reason as the fixtures above.
    """
    monkeypatch.setattr(
        "ciao.install_receipt.default_receipt_path",
        lambda: tmp_path / "state" / "install-receipt.json",
    )


@pytest.fixture(autouse=True)
def _isolate_update_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test touch the machine's real update staging area.

    ``engine_update`` stages whole environments (a ``uv venv`` plus a full
    ``pip install``) under ``~/.local/state/ciaobot/updates``, so an
    un-isolated test would download and install a release into the developer's
    own machine. Autouse and unconditional for the same reason as its siblings.
    """
    monkeypatch.setattr(
        "ciao.engine_update.default_state_dir", lambda: tmp_path / "update-state"
    )


@pytest.fixture(autouse=True)
def _isolate_bootstrap_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test mint a token under the developer's own `~/.ciao`.

    A config built without `CIAO_WORKSPACE` takes the bootstrap branch, and
    `_read_or_create_secret` then writes
    `~/.ciao/bootstrap/.runtime/bootstrap-auth-token`. `CiaoConfig.from_env({})`
    and the common `from_env({"PWA_AUTH_TOKEN": "t"})` idiom both do this, so 51
    tests across 7 files were fabricating a bootstrap install under the
    developer's home — on a fresh checkout, creating one that was never
    installed.

    Autouse and unconditional, for the same reason as its two siblings above:
    the per-test argument is exactly the step that gets forgotten, and the
    blast radius is the developer's own install.
    """
    monkeypatch.setenv("CIAO_BOOTSTRAP_WORKSPACE", str(tmp_path / "bootstrap"))


@pytest.fixture(autouse=True)
def _isolate_queue_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep proposal-queue lock files out of the shared system temp directory.

    Queue locks live outside the vault by design; without this they would
    accumulate in ``/tmp/ciao-queue-locks`` across the suite and could collide
    between tests that reuse a path.
    """
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "queue-locks"))


@pytest.fixture(autouse=True)
def _isolate_job_runs(tmp_path: Path) -> None:
    """Isolate job runs recording by pointing to a temp directory for every test."""
    jr.configure(tmp_path)
    yield
    jr._runtime_dir_override = None


def attach_stub_mcp(manager):
    """Give a test-built ``ProjectChatManager`` a stub agent control plane.

    The Ciaobot agent surface is mandatory: ``build_agent_request`` raises
    ``AgentSurfaceUnavailableError`` without one, so any test that dispatches a
    turn needs a service the way a running server always has one.
    """
    manager._mcp_service = SimpleNamespace(
        agent_url="http://127.0.0.1:8443/agent/v1/",
        credentials_for_chat=lambda chat, project: (
            "http://127.0.0.1:8443/agent/v1/",
            "tok-test",
        )
    )
    return manager
