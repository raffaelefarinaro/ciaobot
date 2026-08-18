from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao import native_sidecar


def _patch_respond_transport(monkeypatch: pytest.MonkeyPatch, fake) -> None:
    """Stub whichever transport `respond` reaches for on this platform.

    On macOS generation crosses into the user session; elsewhere it runs the
    helper directly. Patching both keeps these tests platform-independent.
    """
    monkeypatch.setattr(
        native_sidecar, "sidecar_path", lambda: Path("/tmp/ciaobot-native")
    )
    monkeypatch.setattr(native_sidecar, "_respond_in_user_session", fake)
    monkeypatch.setattr(native_sidecar, "_run_process", fake)


def test_foundation_models_runtime_failure_overrides_probe_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_sidecar.reset_probe_cache()
    monkeypatch.setattr(
        native_sidecar,
        "probe",
        lambda: {"model": {"available": True}},
    )

    async def failing_run(*args, **kwargs):
        return (
            native_sidecar.EXIT_FAILURE,
            b"",
            "the on-device model failed: ModelManagerServices.ModelManagerError error 1008",
        )

    _patch_respond_transport(monkeypatch, failing_run)
    assert native_sidecar.apple_model_available() is True

    with pytest.raises(native_sidecar.SidecarError, match="ModelManagerError error 1008"):
        asyncio.run(native_sidecar.respond("Say hello"))

    assert native_sidecar.apple_model_available() is False
    assert "FoundationModels" in native_sidecar.apple_model_unavailable_reason()

    native_sidecar.reset_probe_cache()
    assert native_sidecar.apple_model_available() is True


def test_successful_foundation_models_response_clears_runtime_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native_sidecar.reset_probe_cache()
    monkeypatch.setattr(
        native_sidecar,
        "probe",
        lambda: {"model": {"available": True}},
    )

    async def successful_run(*args, **kwargs):
        return 0, b"Hello from Apple\n", ""

    _patch_respond_transport(monkeypatch, successful_run)
    result = asyncio.run(native_sidecar.respond("Say hello"))

    assert result == "Hello from Apple"
    assert native_sidecar.apple_model_available() is True


def test_on_device_model_is_gated_by_hardware_support_not_a_beta_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The "apple" model is available purely from machine support: when the
    probe reports the model present, no app-side opt-in is required."""
    native_sidecar.reset_probe_cache()
    monkeypatch.setattr(
        native_sidecar,
        "probe",
        lambda: {"model": {"available": True}},
    )

    assert native_sidecar.apple_model_available() is True
    assert native_sidecar.apple_model_unavailable_reason() == ""

    async def successful_run(*args, **kwargs):
        return 0, b"Hello from Apple\n", ""

    _patch_respond_transport(monkeypatch, successful_run)
    assert asyncio.run(native_sidecar.respond("Say hello")) == "Hello from Apple"


def test_on_device_model_unavailable_reports_machine_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A machine without the model gets a reason that names the hardware gap,
    not an app-side opt-in flag."""
    native_sidecar.reset_probe_cache()
    monkeypatch.setattr(
        native_sidecar,
        "probe",
        lambda: {"model": {"available": False}},
    )

    assert native_sidecar.apple_model_available() is False
    reason = native_sidecar.apple_model_unavailable_reason()
    assert reason
    # It names the hardware gap, not an app-side opt-in flag.
    assert "beta" not in reason
    assert "Settings" not in reason


def test_user_session_bridge_keeps_the_prompt_off_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt is a chat transcript; a command line is world-readable.

    The first version piped the prompt through `printf %s '<transcript>' | …`,
    so every local user could read the conversation out of `ps` for as long as
    the helper ran. It goes through a 0600 temp file instead.
    """
    captured: dict[str, object] = {}
    secret = "Say O'Reilly; my password is hunter2"

    async def fake_process(argv, *, stdin=None, timeout=0):
        captured["argv"] = argv
        captured["timeout"] = timeout
        return 0, b"hello", ""

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    asyncio.run(
        native_sidecar._respond_in_user_session(
            secret,
            "Use one word.",
            binary=Path("/Applications/Ciaobot.app/Contents/MacOS/ciaobot-native"),
            timeout=12.0,
        )
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0:2] == ["/usr/bin/osascript", "-e"]
    assert secret not in " ".join(argv)
    assert "hunter2" not in " ".join(argv)
    assert captured["timeout"] == 12.0


def test_user_session_bridge_recovers_the_helper_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`do shell script` reports any non-zero exit as its own generic error.

    That flattened EXIT_UNSUPPORTED_OS / EXIT_MODEL_UNAVAILABLE /
    EXIT_EMPTY_RESULT into one code, so `respond` could no longer tell them
    apart and users saw a raw AppleScript error instead of the curated
    message. The status is printed and parsed back out.
    """

    async def fake_process(argv, *, stdin=None, timeout=0):
        marker = native_sidecar._EXIT_MARKER
        return 0, f"partial output{marker}{native_sidecar.EXIT_MODEL_UNAVAILABLE}".encode(), ""

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    code, out, _ = asyncio.run(
        native_sidecar._respond_in_user_session(
            "hi", "", binary=Path("/tmp/ciaobot-native"), timeout=5.0
        )
    )

    assert code == native_sidecar.EXIT_MODEL_UNAVAILABLE
    assert out == b"partial output"


def test_user_session_bridge_reports_osascript_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No marker means osascript never reached the helper at all."""

    async def fake_process(argv, *, stdin=None, timeout=0):
        return 1, b"", "not authorized to send Apple events"

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    code, out, err = asyncio.run(
        native_sidecar._respond_in_user_session(
            "hi", "", binary=Path("/tmp/ciaobot-native"), timeout=5.0
        )
    )

    assert code != 0
    assert out == b""
    assert "Apple events" in err


def test_user_session_bridge_restores_line_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`do shell script` rewrites newlines to lone carriage returns.

    That text is appended straight into the vault archive's `## Session
    insights` block and rendered in Settings, where a lone CR is a control
    character, not a line break — the whole section arrived as one line.
    """

    async def fake_process(argv, *, stdin=None, timeout=0):
        marker = native_sidecar._EXIT_MARKER
        return 0, f"## Decisions\r- one\r- two{marker}0".encode(), ""

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    code, out, _ = asyncio.run(
        native_sidecar._respond_in_user_session(
            "hi", "", binary=Path("/tmp/ciaobot-native"), timeout=5.0
        )
    )

    assert code == 0
    assert out.decode() == "## Decisions\n- one\n- two"


def test_user_session_bridge_bounds_the_apple_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `with timeout of`, AppleScript's own default governs the run."""
    captured: dict[str, object] = {}

    async def fake_process(argv, *, stdin=None, timeout=0):
        captured["script"] = argv[2]
        return 0, f"ok{native_sidecar._EXIT_MARKER}0".encode(), ""

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    asyncio.run(
        native_sidecar._respond_in_user_session(
            "hi", "", binary=Path("/tmp/ciaobot-native"), timeout=45.0
        )
    )

    assert "with timeout of 45 seconds" in str(captured["script"])
