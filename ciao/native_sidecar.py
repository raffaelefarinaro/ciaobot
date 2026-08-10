"""Shared plumbing for the ``ciaobot-native`` sidecar bundled in Ciaobot.app.

The sidecar exists because several macOS frameworks are unreachable from
Python: Apple's on-device dictation and its on-device LLM are Swift-only APIs,
and the speech synthesizer would otherwise need a pyobjc dependency just to
pick a voice. See ``desktop/native/main.swift`` for the full reasoning.

This module owns finding the binary, probing what the machine supports, and
running one subcommand. The callers on top of it are ``ciao/voice.py`` (hear /
speak) and ``respond`` below (chat titles, replacing the ``apfel`` CLI).

On macOS, the server normally runs as a launchd agent. FoundationModels can
report availability from that process but its model-manager connection is only
usable from the logged-in user session; direct generation there returns
ModelManagerServices error 1008. The respond bridge therefore asks
``osascript``'s user-session ``do shell script`` to launch this same bundled
Swift helper. No external model CLI is involved.

Everything here fails soft. The probe backs availability checks all over the
app, so a missing binary, an old macOS, or a broken sidecar must resolve to
"not available" rather than an exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
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


def app_bundle_roots() -> tuple[Path, ...]:
    """Directories macOS installs app bundles into, per-user first.

    A single definition so the sidecar lookup, the launcher search, and the
    desktop-app check can never disagree about where to look; tests point it at
    a temporary tree. Lives here rather than in cli.py because this module is a
    leaf -- cli imports it, not the other way round.
    """
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
    for root in app_bundle_roots():
        candidate = root / "Ciaobot.app" / "Contents" / "MacOS" / SIDECAR_NAME
        if candidate.is_file():
            return candidate
    # resolve_tool, not shutil.which: the engine runs under launchd with a
    # stripped PATH, which is exactly the case this helper exists for.
    from ciao.tool_path import resolve_tool

    found = resolve_tool(SIDECAR_NAME)
    return Path(found) if found else None


# Cached probe, keyed on the resolved binary. `ciao desktop install` runs in a
# separate CLI process, so a server that started before Ciaobot.app existed
# would otherwise answer "not installed" until it was restarted -- a cache miss
# on the path appearing is what lets the install be picked up live. Locating the
# binary is a couple of stat calls; the subprocess is what the cache is for.
_probe_cache: dict[str | None, tuple[float, dict[str, Any]]] = {}

# Voice downloads change the positive answer too: a Premium voice can become
# available while the server is running. Re-probe periodically so Settings and
# the automatic voice selector see it without requiring an app restart.
_PROBE_TTL_S = 60.0

# Foundation Models has a much smaller context window than the cloud models
# used by the rest of the app. Keep enough of the newest transcript for a
# useful summary while leaving room for the prompt and instructions.
APPLE_MAX_INPUT_CHARS = 8_000

# `SystemLanguageModel.Availability` can report `.available` while the first
# session still fails in ModelManagerServices (for example while the model
# service is recovering). Remember that runtime failure briefly so callers do
# not keep launching a known-broken process and, more importantly, so Settings
# does not claim Apple Intelligence is usable when generation is not.
_MODEL_FAILURE_TTL_S = 60.0
_model_failure: tuple[float, str] | None = None

_USER_SESSION_RESPOND_SCRIPT = "\n".join(
    (
        "on run argv",
        "set helperPath to item 1 of argv",
        "set promptText to item 2 of argv",
        "set instructionsText to item 3 of argv",
        'set shellCommand to "printf %s " & quoted form of promptText & '
        '" | " & quoted form of helperPath & " respond"',
        'if instructionsText is not "" then',
        'set shellCommand to shellCommand & " --instructions " & quoted form of instructionsText',
        "end if",
        "return do shell script shellCommand",
        "end run",
    )
)


def probe() -> dict[str, Any]:
    """Ask the sidecar what this machine supports. Cached: it shells out."""
    if sys.platform != "darwin":
        return _EMPTY_PROBE
    binary = sidecar_path()
    if binary is None:
        return _EMPTY_PROBE
    key = str(binary)
    cached = _probe_cache.get(key)
    if cached is not None:
        cached_at, payload = cached
        if (time.monotonic() - cached_at) < _PROBE_TTL_S:
            return payload
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
    # Only a good answer is cached. A transient failure (spawn error, non-zero
    # exit, bad JSON) stays uncached so the next caller retries instead of
    # inheriting the failure for the life of the process.
    resolved = parsed if isinstance(parsed, dict) else _EMPTY_PROBE
    _probe_cache[key] = (time.monotonic(), resolved)
    return resolved


def reset_probe_cache() -> None:
    """Forget the cached probe, for tests and after installing the app."""
    global _model_failure
    _probe_cache.clear()
    _model_failure = None


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


async def _run_process(
    argv: list[str], *, stdin: bytes | None = None, timeout: float = DEFAULT_TIMEOUT_S
) -> tuple[int, bytes, str]:
    """Run a process and return (exit code, stdout, stderr text)."""
    process = await asyncio.create_subprocess_exec(
        *argv,
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


async def _run_respond_in_user_session(
    args: list[str], *, binary: Path, stdin: bytes | None, timeout: float
) -> tuple[int, bytes, str]:
    """Launch the FM sidecar from the logged-in macOS user session.

    launchd agents can invoke the helper and read its capability probe, but
    FoundationModels' generation service rejects direct daemon-context calls
    with ModelManagerServices error 1008. ``do shell script`` runs as the same
    logged-in user while keeping the prompt in argv, where AppleScript's
    ``quoted form of`` safely shell-quotes arbitrary user text.
    """
    if stdin is None:
        raise SidecarError("respond expects the prompt on stdin")
    instructions = ""
    if len(args) == 3 and args[1] == "--instructions":
        instructions = args[2]
    elif len(args) != 1:
        raise SidecarError("invalid native respond arguments")
    return await _run_process(
        [
            "/usr/bin/osascript",
            "-e",
            _USER_SESSION_RESPOND_SCRIPT,
            "--",
            str(binary),
            stdin.decode("utf-8", "replace"),
            instructions,
        ],
        timeout=timeout,
    )


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
    if (
        sys.platform == "darwin"
        and args
        and args[0] == "respond"
        and Path("/usr/bin/osascript").is_file()
    ):
        return await _run_respond_in_user_session(
            args, binary=binary, stdin=stdin, timeout=timeout
        )
    return await _run_process(
        [str(binary), *args], stdin=stdin, timeout=timeout
    )


# ── On-device language model (chat titles) ───────────────────────────────


def apple_model_available() -> bool:
    """True when Apple's on-device model can be used here.

    False on non-macOS, without the app bundle, before macOS 26, when Apple
    Intelligence is switched off, and while the model is still downloading.
    """
    return not _model_failure_reason() and bool(section("model").get("available"))


def apple_model_unavailable_reason() -> str:
    """Why the on-device model is off, phrased for Settings."""
    runtime_reason = _model_failure_reason()
    if runtime_reason:
        return runtime_reason
    return unavailable_reason("model", subject="the on-device model")


def _model_failure_reason() -> str:
    cached = _model_failure
    if cached is None:
        return ""
    failed_at, reason = cached
    if (time.monotonic() - failed_at) >= _MODEL_FAILURE_TTL_S:
        return ""
    return reason


def _remember_model_failure(reason: str, *, force: bool = False) -> None:
    """Cache only failures that indicate the FM runtime itself is unhealthy."""
    global _model_failure
    text = reason.strip()
    lowered = text.lower()
    if not text or (
        not force
        and not (
            "modelmanager" in lowered
            or "on-device model failed" in lowered
            or "foundationmodels" in lowered
        )
    ):
        return
    _model_failure = (
        time.monotonic(),
        "Apple FoundationModels is unavailable right now: " + text,
    )


def fit_apple_input(text: str, *, max_chars: int = APPLE_MAX_INPUT_CHARS) -> tuple[str, int]:
    """Keep the newest complete lines within Apple's small input budget.

    Returns ``(text, dropped_lines)``. The line-oriented transcript formats
    used by callers stay valid unless one individual record is itself larger
    than the budget, in which case its newest suffix is retained.
    """
    if len(text) <= max_chars:
        return text, 0
    lines = text.splitlines()
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        line_size = len(line) + 1
        if total + line_size > max_chars:
            if not kept and max_chars > 0:
                kept.append(line[-max_chars:])
            break
        kept.append(line)
        total += line_size
    kept.reverse()
    return "\n".join(kept), len(lines) - len(kept)


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
    global _model_failure
    cached_reason = _model_failure_reason()
    if cached_reason:
        raise SidecarError(cached_reason)

    args = ["respond"]
    if instructions:
        args += ["--instructions", instructions]
    code, out, err = await run(args, stdin=prompt.encode("utf-8"), timeout=timeout)
    if code == EXIT_UNSUPPORTED_OS:
        raise SidecarError("the on-device model requires macOS 26 or newer")
    if code == EXIT_MODEL_UNAVAILABLE:
        message = err or "the on-device model is unavailable"
        _remember_model_failure(message, force=True)
        raise SidecarError(message)
    if code == EXIT_EMPTY_RESULT:
        raise SidecarError("the on-device model returned no text")
    if code != 0:
        message = err or "the on-device model failed"
        _remember_model_failure(message)
        raise SidecarError(message)
    text = out.decode("utf-8", "replace").strip()
    if not text:
        raise SidecarError("the on-device model returned no text")
    _model_failure = None
    return text
