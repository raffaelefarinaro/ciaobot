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


@pytest.fixture(autouse=True)
def _never_download_the_desktop_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `ciao setup` from reaching GitHub and writing to /Applications.

    setup_workspace installs Ciaobot.app when no app_dir is pinned, which is
    right on a user's machine and wrong in a test: it would download 13 MB and
    drop a real bundle into the developer's own /Applications. Tests that want to
    exercise the install call desktop_install directly, or unset this.
    """
    monkeypatch.setenv("CIAO_SKIP_DESKTOP_APP", "1")
