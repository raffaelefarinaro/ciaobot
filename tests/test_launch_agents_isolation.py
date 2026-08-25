"""Setup must not write the operator's real LaunchAgents directory.

`setup_workspace` defaults `launch_agents_dir` to `~/Library/LaunchAgents`, so
running the suite rewrote the live `com.ciao.server.plist` to point at a pytest
tmpdir. Nothing failed: the engine simply came back up against a temp workspace
and indexed that fixture vault over the operator's real search database.

The guard is the default itself, not the call sites, so the test asserts on the
default — a new caller that forgets the argument stays safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.cli import setup_workspace
from ciao.macos_service import default_launch_agents_dir


def test_the_default_launch_agents_dir_is_redirectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path / "elsewhere"))
    assert default_launch_agents_dir() == tmp_path / "elsewhere"

    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR")
    assert default_launch_agents_dir() == Path.home() / "Library" / "LaunchAgents"


def test_setup_does_not_touch_the_real_launch_agents_dir(tmp_path: Path) -> None:
    # The autouse `_isolate_launch_agents` fixture is the only thing standing
    # between this call and the developer's live install.
    real = Path.home() / "Library" / "LaunchAgents" / "com.ciao.server.plist"
    before = real.read_bytes() if real.exists() else None

    setup_workspace(tmp_path / "ws", auth_token="t", auth_required=False)

    after = real.read_bytes() if real.exists() else None
    assert after == before, "setup rewrote the operator's real LaunchAgent plist"


def test_setup_writes_the_plist_into_the_redirected_dir(tmp_path: Path) -> None:
    setup_workspace(tmp_path / "ws", auth_token="t", auth_required=False)

    redirected = tmp_path / "LaunchAgents" / "com.ciao.server.plist"
    assert redirected.exists(), "the plist went somewhere other than the override"
    assert str(tmp_path / "ws") in redirected.read_text(encoding="utf-8")
