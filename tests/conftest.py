from __future__ import annotations

import pytest
from pathlib import Path
from ciao import job_runs as jr


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
