"""Self-heal optional local voice packages after upgrades.

``brew upgrade ciaobot`` builds a new keg with a fresh private
virtualenv, which silently drops optional packages the user installed
from Settings -> Voice (mlx-whisper for local dictation, kokoro-onnx
for local read-aloud). The saved settings still select the local
engines, so the UI shows "selected but not installed" banners after
every upgrade until the user clicks Install again.

At startup, when a local engine is selected but its package is missing,
this module re-runs the same pip install the Settings button performs
and requests a restart so the fresh process can import it. A marker
file keyed on the app version makes the attempt once-per-version:
a failing install (offline, unsupported platform) degrades to the
existing banners instead of an install/restart loop. The downloaded
model files live outside the app environment (Hugging Face cache and
``~/.cache/ciaobot/kokoro``), so healing never re-downloads them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable

from ciao import __version__
from ciao.voice import kokoro_available, mlx_whisper_available

logger = logging.getLogger(__name__)

# Single source of truth for the optional voice requirements; the
# Settings install endpoints use these too.
VOICE_LOCAL_REQUIREMENT = "mlx-whisper>=0.4.0"
TTS_LOCAL_REQUIREMENT = "kokoro-onnx>=0.5.0"

_PIP_TIMEOUT_S = 600.0


def missing_voice_requirements(config) -> list[str]:
    """Requirements for local engines selected in settings but not importable."""
    missing: list[str] = []
    if (
        getattr(config, "transcription_engine", "") == "local"
        and not mlx_whisper_available()
    ):
        missing.append(VOICE_LOCAL_REQUIREMENT)
    if getattr(config, "tts_engine", "") == "local" and not kokoro_available():
        missing.append(TTS_LOCAL_REQUIREMENT)
    return missing


def _marker_path() -> Path:
    return Path.home() / ".cache" / "ciaobot" / "voice-extras-autoinstall.json"


def _attempted_requirements() -> set[str]:
    """Requirements already attempted for the currently running version."""
    try:
        data = json.loads(_marker_path().read_text(encoding="utf-8"))
    except Exception:
        return set()
    if data.get("version") != __version__:
        return set()
    reqs = data.get("requirements", [])
    return set(reqs) if isinstance(reqs, list) else set()


def _record_attempt(requirements: list[str]) -> None:
    marker = _marker_path()
    merged = sorted(_attempted_requirements() | set(requirements))
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"version": __version__, "requirements": merged}),
            encoding="utf-8",
        )
    except Exception:
        # Best-effort: without the marker the worst case is one extra
        # install attempt on the next startup.
        logger.debug("Could not write voice-extras marker %s", marker, exc_info=True)


async def heal_voice_extras(
    config,
    request_restart: Callable[[int], None],
) -> bool:
    """Reinstall missing local voice packages once per version.

    Returns True when everything installed and a restart was requested.
    """
    from ciao.package_version import detect_install_mode

    missing = missing_voice_requirements(config)
    if not missing:
        return False

    if detect_install_mode() == "editable":
        # Never mutate a development environment behind the developer's
        # back; the Settings banner and `ciao upgrade` cover this case.
        logger.info(
            "Local voice packages missing (%s) but install is editable; "
            "leaving the Settings install banner to handle it",
            ", ".join(missing),
        )
        return False

    attempted = _attempted_requirements()
    todo = [req for req in missing if req not in attempted]
    if not todo:
        logger.info(
            "Local voice packages still missing (%s) after an install attempt "
            "for v%s; not retrying automatically",
            ", ".join(missing),
            __version__,
        )
        return False

    # Record before installing so a crash mid-install cannot loop.
    _record_attempt(todo)

    ok = True
    for req in todo:
        logger.info(
            "Local voice engine selected but %s is not installed "
            "(likely removed by an app upgrade); reinstalling",
            req,
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", req],
                capture_output=True,
                text=True,
                timeout=_PIP_TIMEOUT_S,
            )
        except Exception as exc:
            ok = False
            logger.warning("Auto-install of %s failed to run: %s", req, exc)
            continue
        if result.returncode != 0:
            ok = False
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
            logger.warning(
                "Auto-install of %s exited with %d: %s",
                req,
                result.returncode,
                " | ".join(tail),
            )

    if not ok:
        return False

    logger.info(
        "Reinstalled local voice packages (%s); restarting to load them",
        ", ".join(todo),
    )
    request_restart(config.restart_exit_code)
    return True
