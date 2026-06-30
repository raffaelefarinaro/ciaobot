"""Installed package version and update-check helpers."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ciao import __version__


DEFAULT_PACKAGE_INDEX_URL = "https://pypi.org/pypi/ciao/json"


def _version_key(value: str) -> tuple:
    parts: list[tuple[int, object]] = []
    for part in re.findall(r"\d+|[A-Za-z]+", value):
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part.lower()))
    return tuple(parts)


def _latest_from_payload(payload: dict[str, Any]) -> str:
    info = payload.get("info")
    if isinstance(info, dict) and isinstance(info.get("version"), str):
        return info["version"].strip()
    version = payload.get("version")
    if isinstance(version, str):
        return version.strip()
    return ""


def package_status(
    *,
    current_version: str = __version__,
    index_url: str = DEFAULT_PACKAGE_INDEX_URL,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 2.5,
) -> dict[str, object]:
    """Return installed and latest package versions."""
    latest = ""
    error = ""
    try:
        with opener(index_url, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            latest = _latest_from_payload(payload)
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)

    update_available = bool(
        latest and _version_key(latest) > _version_key(current_version)
    )
    return {
        "current_version": current_version,
        "latest_version": latest,
        "update_available": update_available,
        "source": index_url,
        "error": error,
    }


def detect_install_mode() -> str:
    """Return "homebrew", "pip_venv", "editable", or "unknown"."""
    import sys
    import shutil
    from pathlib import Path

    try:
        import ciao
        ciao_file = Path(ciao.__file__).resolve()
        project_root = ciao_file.parent.parent
        if (project_root / "pyproject.toml").is_file() and (project_root / ".git").is_dir():
            return "editable"
    except Exception:
        ciao_file = None

    try:
        executable = Path(sys.executable).resolve()
        if "Cellar/ciao" in str(executable):
            return "homebrew"
        if ciao_file and "Cellar/ciao" in str(ciao_file):
            return "homebrew"
    except Exception:
        pass

    if sys.prefix != sys.base_prefix:
        return "pip_venv"

    return "unknown"


def update_package() -> dict[str, Any]:
    """Perform package upgrade based on active install mode."""
    import sys
    import shutil
    import subprocess
    from pathlib import Path
    from typing import Any

    mode = detect_install_mode()
    if mode == "editable":
        return {
            "ok": False,
            "mode": mode,
            "error": "Editable checkouts must be updated manually via 'git pull'.",
            "command": "git pull",
        }

    if mode == "homebrew":
        brew = shutil.which("brew")
        if not brew:
            for path in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
                if Path(path).exists():
                    brew = path
                    break
        if not brew:
            return {
                "ok": False,
                "mode": mode,
                "error": "Homebrew 'brew' command not found in PATH.",
                "command": "brew upgrade ciao",
            }
        cmd = [brew, "upgrade", "ciao"]
    elif mode == "pip_venv":
        cmd = [sys.executable, "-m", "pip", "install", "-U", "ciao"]
    else:
        return {
            "ok": False,
            "mode": mode,
            "error": f"Unknown install mode '{mode}'. Please upgrade manually.",
            "command": "pip install -U ciao",
        }

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        if result.returncode == 0:
            return {
                "ok": True,
                "mode": mode,
                "output": output,
                "command": " ".join(cmd),
            }
        else:
            return {
                "ok": False,
                "mode": mode,
                "error": f"Command exited with code {result.returncode}.",
                "output": output,
                "command": " ".join(cmd),
            }
    except Exception as exc:
        return {
            "ok": False,
            "mode": mode,
            "error": str(exc),
            "command": " ".join(cmd),
        }

