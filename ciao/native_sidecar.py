"""Shared plumbing for the ``ciaobot-native`` sidecar bundled in Ciaobot.app.

The sidecar exists because several macOS frameworks are unreachable from
Python: Apple's on-device dictation and its on-device LLM are Swift-only APIs,
and the speech synthesizer would otherwise need a pyobjc dependency just to
pick a voice. See ``desktop/native/main.swift`` for the full reasoning.

This module owns finding the binary, probing what the machine supports, and
running one subcommand. The callers on top of it are ``ciao/voice.py`` (hear /
speak) and ``respond`` below (chat titles, replacing the ``apfel`` CLI).

Everything here fails soft. The probe is called on every Settings load and from
availability checks all over the app, so a missing binary, an old macOS, or a
broken sidecar must resolve to "not available" rather than an exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SIDECAR_NAME = "ciaobot-native"

# Exit codes, mirrored from desktop/native/main.swift so a failure can be
# reported as something the user can act on.
EXIT_USAGE = 64
EXIT_UNSUPPORTED_OS = 65
EXIT_LOCALE_UNAVAILABLE = 66
EXIT_AUDIO_UNREADABLE = 67
EXIT_EMPTY_RESULT = 68
EXIT_FAILURE = 69
EXIT_MODEL_UNAVAILABLE = 70

# Transcribing a long recording is the slow path; synthesis and generation are
# bounded by their input. Generous enough that only a wedged process trips it.
DEFAULT_TIMEOUT_S = 300.0
# Titles run inline while a chat is being saved, so they get a short leash and
# fall back to a cloud model rather than holding the request open.
RESPOND_TIMEOUT_S = 30.0

_EMPTY_PROBE: dict[str, Any] = {
    "hear": {"available": False},
    "speak": {"available": False},
    "model": {"available": False},
}


class SidecarError(Exception):
    """The sidecar could not produce a result."""


def _app_bundle_roots() -> tuple[Path, ...]:
    """Where Ciaobot.app may live, per-user first (mirrors cli._app_search_roots)."""
    return (Path.home() / "Applications", Path("/Applications"))


def sidecar_path() -> Path | None:
    """Locate the sidecar binary, or None when it is not installed.

    Tauri places an ``externalBin`` beside the app executable, so the bundled
    copy is the normal case. ``CIAO_NATIVE_SIDECAR`` overrides for development,
    where the binary sits in the checkout rather than in an installed app.
    """
    override = (os.environ.get("CIAO_NATIVE_SIDECAR") or "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    for root in _app_bundle_roots():
        candidate = root / "Ciaobot.app" / "Contents" / "MacOS" / SIDECAR_NAME
        if candidate.is_file():
            return candidate
    found = shutil.which(SIDECAR_NAME)
    return Path(found) if found else None


@lru_cache(maxsize=1)
def probe() -> dict[str, Any]:
    """Ask the sidecar what this machine supports. Cached: it shells out."""
    if sys.platform != "darwin":
        return _EMPTY_PROBE
    binary = sidecar_path()
    if binary is None:
        return _EMPTY_PROBE
    try:
        result = subprocess.run(
            [str(binary), "probe"], capture_output=True, timeout=30, text=True
        )
    except Exception as exc:  # noqa: BLE001 - the probe must never raise
        logger.debug("native sidecar probe failed: %s", exc)
        return _EMPTY_PROBE
    if result.returncode != 0:
        return _EMPTY_PROBE
    try:
        parsed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return _EMPTY_PROBE
    return parsed if isinstance(parsed, dict) else _EMPTY_PROBE


def reset_probe_cache() -> None:
    """Forget the cached probe, for tests and after installing the app."""
    probe.cache_clear()


def section(name: str) -> dict[str, Any]:
    """One section of the probe (``hear``, ``speak``, or ``model``)."""
    value = probe().get(name)
    return value if isinstance(value, dict) else {"available": False}


def unavailable_reason(name: str, *, subject: str) -> str:
    """Why ``name`` is unavailable, phrased for Settings. Empty when available."""
    data = section(name)
    if data.get("available"):
        return ""
    reason = str(data.get("reason") or "").strip()
    if reason:
        return reason
    if sys.platform != "darwin":
        return f"{subject} is only available on macOS"
    if sidecar_path() is None:
        return "Ciaobot.app is not installed; run `ciao desktop install`"
    return f"{subject} is unavailable on this machine"


async def run(
    args: list[str], *, stdin: bytes | None = None, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[int, bytes, str]:
    """Run one sidecar subcommand. Returns (exit code, stdout, stderr text)."""
    binary = sidecar_path()
    if binary is None:
        raise SidecarError(
            "the ciaobot-native helper is not installed; "
            "install the desktop app with `ciao desktop install`"
        )
    process = await asyncio.create_subprocess_exec(
        str(binary),
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            process.communicate(input=stdin), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise SidecarError("the native helper timed out") from None
    return process.returncode or 0, out, (err or b"").decode("utf-8", "replace").strip()


# ── On-device language model (chat titles) ───────────────────────────────


def apple_model_available() -> bool:
    """True when Apple's on-device model can be used here.

    False on non-macOS, without the app bundle, before macOS 26, when Apple
    Intelligence is switched off, and while the model is still downloading.
    """
    return bool(section("model").get("available"))


def apple_model_unavailable_reason() -> str:
    """Why the on-device model is off, phrased for Settings."""
    return unavailable_reason("model", subject="the on-device model")


async def respond(
    prompt: str, *, instructions: str = "", timeout: float = RESPOND_TIMEOUT_S
) -> str:
    """One-shot generation with Apple's on-device model.

    Calls ``FoundationModels`` through the sidecar rather than Apple's ``fm``
    CLI: ``fm`` only ships with macOS 27, while the framework behind it has been
    there since macOS 26, so this works an OS release earlier and needs no
    external binary.

    Raises ``SidecarError`` for every failure so callers can fall back to a
    cloud model on one exception type.
    """
    args = ["respond"]
    if instructions:
        args += ["--instructions", instructions]
    code, out, err = await run(args, stdin=prompt.encode("utf-8"), timeout=timeout)
    if code == EXIT_UNSUPPORTED_OS:
        raise SidecarError("the on-device model requires macOS 26 or newer")
    if code == EXIT_MODEL_UNAVAILABLE:
        raise SidecarError(err or "the on-device model is unavailable")
    if code == EXIT_EMPTY_RESULT:
        raise SidecarError("the on-device model returned no text")
    if code != 0:
        raise SidecarError(err or "the on-device model failed")
    text = out.decode("utf-8", "replace").strip()
    if not text:
        raise SidecarError("the on-device model returned no text")
    return text
