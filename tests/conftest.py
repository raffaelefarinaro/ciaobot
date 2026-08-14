from __future__ import annotations

import pytest
from pathlib import Path
from ciao import job_runs as jr


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
