from __future__ import annotations

import pytest
from pathlib import Path
from ciao import job_runs as jr


@pytest.fixture(autouse=True)
def _isolate_job_runs(tmp_path: Path) -> None:
    """Isolate job runs recording by pointing to a temp directory for every test."""
    jr.configure(tmp_path)
    yield
    jr._runtime_dir_override = None
