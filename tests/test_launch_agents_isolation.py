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


def test_setup_refuses_to_repoint_real_launch_agent_without_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct library call must not silently repoint the live LaunchAgent.

    The CLI guard (`_setup_command`) already refused, but `setup_workspace`
    itself had no check — a dev loop calling it with a pytest tmpdir rewrote
    `~/Library/LaunchAgents/com.ciao.server.plist` at 09:33 and 09:50 CEST on
    2026-09-02, orphaning the operator's real workspace.
    """
    import plistlib

    fake_home = tmp_path / "fake-home"
    real_agents = fake_home / "Library" / "LaunchAgents"
    real_agents.mkdir(parents=True)
    real_workspace = fake_home / "real-workspace"
    real_workspace.mkdir()

    data = {
        "EnvironmentVariables": {
            "CIAO_WORKSPACE": str(real_workspace),
            "CIAO_RUNTIME_ROOT": str(real_workspace / ".runtime"),
            "CIAO_PORT": "8443",
            "CIAO_PATH": "/usr/bin",
        },
        "ProgramArguments": ["/fake/python", "run"],
    }
    plist = real_agents / "com.ciao.server.plist"
    with plist.open("wb") as handle:
        plistlib.dump(data, handle)
    before = plist.read_bytes()

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    monkeypatch.delenv("CIAO_ALLOW_LAUNCH_AGENT_REPOINT", raising=False)

    other = tmp_path / "other-ws"
    with pytest.raises(RuntimeError, match="Refusing to repoint"):
        setup_workspace(other, auth_token="t", auth_required=False)

    assert plist.read_bytes() == before, "real plist was mutated despite refusal"

    # Explicit opt-in must still allow an intentional move.
    setup_workspace(other, auth_token="t", auth_required=False, confirm_repoint=True)
    with plist.open("rb") as handle:
        after_data = plistlib.load(handle)
    assert str(other.resolve()) in after_data["EnvironmentVariables"]["CIAO_WORKSPACE"]

    # Idempotent rerun on the same workspace must not require confirm.
    setup_workspace(other, auth_token="t", auth_required=False)

    # Moving back to the original workspace without confirm must still be blocked.
    with pytest.raises(RuntimeError, match="Refusing to repoint"):
        setup_workspace(real_workspace, auth_token="t", auth_required=False)

    setup_workspace(
        real_workspace, auth_token="t", auth_required=False, confirm_repoint=True
    )
    with plist.open("rb") as handle:
        restored = plistlib.load(handle)
    assert str(real_workspace.resolve()) in restored["EnvironmentVariables"]["CIAO_WORKSPACE"]
    # Now a same-workspace rerun without confirm must not raise.
    setup_workspace(real_workspace, auth_token="t", auth_required=False)
