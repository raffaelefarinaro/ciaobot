from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import ciao.voice_extras as voice_extras
from ciao.voice_extras import (
    TTS_LOCAL_REQUIREMENT,
    VOICE_LOCAL_REQUIREMENT,
    heal_voice_extras,
    missing_voice_requirements,
)


def _config(transcription: str = "local", tts: str = "local") -> SimpleNamespace:
    return SimpleNamespace(
        transcription_engine=transcription,
        tts_engine=tts,
        restart_exit_code=42,
    )


@pytest.fixture(autouse=True)
def marker_in_tmp(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        voice_extras, "_marker_path", lambda: tmp_path / "marker.json"
    )


@pytest.fixture
def packages_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(voice_extras, "mlx_whisper_available", lambda: False)
    monkeypatch.setattr(voice_extras, "kokoro_available", lambda: False)


def _fake_pip(calls: list[list[str]], returncode: int = 0):
    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    return run


def test_missing_requirements_follow_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(voice_extras, "mlx_whisper_available", lambda: False)
    monkeypatch.setattr(voice_extras, "kokoro_available", lambda: True)

    assert missing_voice_requirements(_config("local", "local")) == [
        VOICE_LOCAL_REQUIREMENT
    ]
    # Cloud engines never require the local packages.
    assert missing_voice_requirements(_config("cloud", "cloud")) == []


def test_missing_requirements_empty_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_extras, "mlx_whisper_available", lambda: True)
    monkeypatch.setattr(voice_extras, "kokoro_available", lambda: True)
    assert missing_voice_requirements(_config()) == []


@pytest.mark.asyncio
async def test_heal_installs_and_restarts(
    monkeypatch: pytest.MonkeyPatch, packages_missing
) -> None:
    monkeypatch.setattr(
        "ciao.package_version.detect_install_mode", lambda: "homebrew"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    restarts: list[int] = []

    healed = await heal_voice_extras(_config(), restarts.append)

    assert healed is True
    assert restarts == [42]
    installed = {cmd[-1] for cmd in calls}
    assert installed == {VOICE_LOCAL_REQUIREMENT, TTS_LOCAL_REQUIREMENT}


@pytest.mark.asyncio
async def test_heal_attempts_once_per_version(
    monkeypatch: pytest.MonkeyPatch, packages_missing
) -> None:
    monkeypatch.setattr(
        "ciao.package_version.detect_install_mode", lambda: "homebrew"
    )
    calls: list[list[str]] = []
    # Failing install: no restart, and the marker blocks a second attempt.
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls, returncode=1))
    restarts: list[int] = []

    assert await heal_voice_extras(_config(), restarts.append) is False
    assert restarts == []
    first_round = len(calls)
    assert first_round == 2

    assert await heal_voice_extras(_config(), restarts.append) is False
    assert len(calls) == first_round
    assert restarts == []


@pytest.mark.asyncio
async def test_heal_skips_editable_installs(
    monkeypatch: pytest.MonkeyPatch, packages_missing
) -> None:
    monkeypatch.setattr(
        "ciao.package_version.detect_install_mode", lambda: "editable"
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    restarts: list[int] = []

    assert await heal_voice_extras(_config(), restarts.append) is False
    assert calls == []
    assert restarts == []


@pytest.mark.asyncio
async def test_heal_noop_when_nothing_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_extras, "mlx_whisper_available", lambda: True)
    monkeypatch.setattr(voice_extras, "kokoro_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_pip(calls))
    restarts: list[int] = []

    assert await heal_voice_extras(_config(), restarts.append) is False
    assert calls == []
    assert restarts == []
