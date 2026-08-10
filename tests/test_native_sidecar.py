from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao import native_sidecar


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

    monkeypatch.setattr(native_sidecar, "run", failing_run)
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

    monkeypatch.setattr(native_sidecar, "run", successful_run)
    result = asyncio.run(native_sidecar.respond("Say hello"))

    assert result == "Hello from Apple"
    assert native_sidecar.apple_model_available() is True


def test_respond_bridge_passes_prompt_and_instructions_to_user_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_process(argv, *, stdin=None, timeout=0):
        captured["argv"] = argv
        captured["stdin"] = stdin
        captured["timeout"] = timeout
        return 0, b"hello", ""

    monkeypatch.setattr(native_sidecar, "_run_process", fake_process)
    result = asyncio.run(
        native_sidecar._run_respond_in_user_session(
            ["respond", "--instructions", "Use one word."],
            binary=Path("/Applications/Ciaobot.app/Contents/MacOS/ciaobot-native"),
            stdin=b"Say O'Reilly\n",
            timeout=12.0,
        )
    )

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0:2] == ["/usr/bin/osascript", "-e"]
    assert argv[3:] == [
        "--",
        "/Applications/Ciaobot.app/Contents/MacOS/ciaobot-native",
        "Say O'Reilly\n",
        "Use one word.",
    ]
    assert "quoted form of promptText" in argv[2]
    assert captured["timeout"] == 12.0
    assert result == (0, b"hello", "")
