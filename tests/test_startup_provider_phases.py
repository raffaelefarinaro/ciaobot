"""The Claude startup phase reports the same truth as the Settings provider card.

The phase and the card both come from ``claude_status_probe``; these tests pin
each install state so the phase cannot read "connected" while the card says the
ACP adapter is missing, and never fail with an empty detail. A CLI the user never
installed is skipped, not failed.
"""

from __future__ import annotations

import asyncio
from functools import partial

import pytest

from ciao import adapter_install, provider_registry
from ciao.main import StartupTracker, run_provider_startup_check
from ciao.setup_status import claude_status_probe

# The phase has no install button, so it points at Settings; the card keeps its button wording.
_PHASE_ADAPTER_MESSAGE = (
    "The ACP adapter claude-agent-acp 0.108.1 is not installed for Ciaobot yet. "
    "Open Settings → Providers to install it."
)
_CARD_ADAPTER_MESSAGE = (
    "The ACP adapter claude-agent-acp 0.108.1 is not installed for Ciaobot yet. "
    "Install it with the button below, or run: "
    f"{adapter_install.install_command(adapter_install.CLAUDE_ADAPTER)}"
)


@pytest.fixture
def claude_probe(monkeypatch):
    """The registry's Claude probe, called the way startup calls it: env only."""
    monkeypatch.setattr("ciao.provider_versions.release_check", lambda *a, **k: {"state": "current"})
    monkeypatch.setattr("ciao.setup_status.discover_claude_system_skills", lambda: [])
    monkeypatch.setattr("ciao.setup_status._cli_version", lambda binary: "2.1.326 (Claude Code)")
    descriptor = next(d for d in provider_registry.descriptors() if d.id == "claude")
    return partial(descriptor.status_probe, {})


def _run(probe) -> StartupTracker:
    tracker = StartupTracker()
    tracker.start("connect_claude_code")
    asyncio.run(run_provider_startup_check(tracker, "connect_claude_code", probe))
    return tracker


def _phase(tracker: StartupTracker):
    return tracker.phase("connect_claude_code")


def test_cli_missing_fails_the_phase_with_the_card_detail(monkeypatch, claude_probe) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)

    phase = _phase(_run(claude_probe))

    # Not installed is a choice, not a fault: neutral, never failed.
    assert phase.status == "skipped"
    assert phase.message == "Claude Code is not installed on this machine."
    assert phase.message == claude_status_probe({}, config_path=None)["detail"]


def test_cli_present_but_adapter_missing_fails_with_the_adapter_message(
    monkeypatch, claude_probe
) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "adapter_missing")

    phase = _phase(_run(claude_probe))

    assert phase.status == "failed"
    assert phase.message == _PHASE_ADAPTER_MESSAGE
    assert "button below" not in phase.message
    # The card renders the same status from the same probe, with its own wording.
    card = claude_status_probe({}, config_path=None)
    assert card["detail"] == _CARD_ADAPTER_MESSAGE
    assert card["state"] == "adapter_missing"


def test_cli_and_adapter_present_and_signed_in_is_done(monkeypatch, claude_probe) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "ready")
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda *args, **kwargs: {"logged_in": True, "email": "user@example.test", "org_name": ""},
    )

    phase = _phase(_run(claude_probe))

    assert phase.status == "done"
    assert phase.message == "Signed in as user@example.test"


def test_adapter_missing_is_never_reported_connected(monkeypatch, claude_probe) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "adapter_missing")
    monkeypatch.setattr(
        "ciao.setup_status.claude_auth_status",
        lambda *args, **kwargs: {"logged_in": True, "email": "user@example.test", "org_name": ""},
    )

    phase = _phase(_run(claude_probe))

    assert phase.status == "failed"
    assert "connected" not in phase.message


def test_the_startup_phase_follows_the_explicit_state_not_the_detail_text() -> None:
    def not_installed_with_other_words() -> dict:
        return {"ok": False, "state": "not_installed", "detail": "gone"}

    phase = _phase(_run(not_installed_with_other_words))

    assert phase.status == "skipped"
    assert phase.message == "gone"


def test_a_probe_that_raises_never_leaves_an_empty_detail() -> None:
    def timed_out() -> dict:
        raise TimeoutError()

    phase = _phase(_run(timed_out))

    assert phase.status == "failed"
    assert phase.message == "check failed: TimeoutError"


def test_claude_keeps_its_ui_phase_id_and_other_providers_get_a_connect_phase() -> None:
    from ciao.main import provider_startup_phase

    assert provider_startup_phase("claude") == "connect_claude_code"
    assert provider_startup_phase("opencode") == "connect_opencode"
    assert provider_startup_phase("codex") == "connect_codex"


def test_adapter_install_started_in_background_settles_the_phase_as_done(monkeypatch, claude_probe) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "adapter_missing")

    tracker = StartupTracker()
    tracker.start("connect_claude_code")
    asyncio.run(
        run_provider_startup_check(tracker, "connect_claude_code", claude_probe, installing=True)
    )

    phase = _phase(tracker)
    assert phase.status == "done"
    assert phase.message == "The ACP adapter claude-agent-acp is installing in the background."


def test_node_too_old_fails_the_phase_with_the_adapter_facts(monkeypatch, claude_probe) -> None:
    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "/fake/claude")
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "node_old")

    phase = _phase(_run(claude_probe))

    assert phase.status == "failed"
    assert phase.message.startswith("Node 22 or newer is needed to run claude-agent-acp")
    assert "Settings → Providers" not in phase.message


def test_an_absent_cli_never_starts_the_adapter_install(monkeypatch, claude_probe) -> None:
    from ciao import adapter_install
    from ciao.main import check_provider_startup

    monkeypatch.setattr("ciao.setup_status.claude_cli_path", lambda: "")
    monkeypatch.setattr("ciao.setup_status.claude_app_path", lambda: "")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    monkeypatch.setattr("ciao.adapter_install.detect", lambda name: "cli_missing")
    installs: list[str] = []
    monkeypatch.setattr(adapter_install, "install", lambda name: installs.append(name))
    descriptor = next(d for d in provider_registry.descriptors() if d.id == "claude")

    tracker = StartupTracker()
    tracker.start("connect_claude_code")
    asyncio.run(check_provider_startup(tracker, "connect_claude_code", descriptor))

    assert installs == []
    assert _phase(tracker).status == "skipped"
