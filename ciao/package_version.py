"""Installed package version and update-check helpers."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ciao import __version__


DEFAULT_PACKAGE_INDEX_URL = "https://pypi.org/pypi/ciao/json"
DEFAULT_GITHUB_REPO = "raffaelefarinaro/ciaobot"


def _github_repo() -> str:
    """Return the GitHub repo (owner/name) used for changelog lookups."""
    return (os.environ.get("CIAO_GITHUB_REPO") or "").strip() or DEFAULT_GITHUB_REPO


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


def package_changelog(
    *,
    current_version: str = __version__,
    latest_version: str = "",
    repo: str | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 4.0,
) -> dict[str, object]:
    """Return the commit subjects between the installed and latest release tags.

    Uses the GitHub compare API (``v{current}...v{latest}``). Commits are
    returned newest-first. Any failure is reported via ``error`` and yields an
    empty commit list so the caller can still offer the update.
    """
    repo = (repo or _github_repo()).strip("/")
    commits: list[dict[str, str]] = []
    error = ""
    compare_url = ""

    if not latest_version:
        return {
            "commits": commits,
            "compare_url": compare_url,
            "repo": repo,
            "error": "No newer version is available.",
        }

    base = f"v{current_version}"
    head = f"v{latest_version}"
    compare_url = f"https://github.com/{repo}/compare/{base}...{head}"
    api_url = f"https://api.github.com/repos/{repo}/compare/{base}...{head}"
    request = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ciaobot-package-updater",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        raw_commits = payload.get("commits") if isinstance(payload, dict) else None
        if isinstance(raw_commits, list):
            for entry in raw_commits:
                if not isinstance(entry, dict):
                    continue
                commit = entry.get("commit")
                message = ""
                if isinstance(commit, dict) and isinstance(commit.get("message"), str):
                    message = commit["message"]
                lines = [line for line in message.strip().splitlines() if line.strip()]
                subject = lines[0].strip() if lines else ""
                sha = entry.get("sha") if isinstance(entry.get("sha"), str) else ""
                if subject:
                    commits.append({"sha": sha[:7], "subject": subject})
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)

    commits.reverse()  # GitHub returns oldest-first; show newest changes first.
    return {
        "commits": commits,
        "compare_url": compare_url,
        "repo": repo,
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

