"""Installed package version and update-check helpers."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ciao import __version__


DEFAULT_GITHUB_REPO = "raffaelefarinaro/ciaobot"


def _github_repo() -> str:
    """Return the GitHub repo (owner/name) used for release lookups."""
    return (os.environ.get("CIAO_GITHUB_REPO") or "").strip() or DEFAULT_GITHUB_REPO


def latest_release_url(repo: str | None = None) -> str:
    """Return the GitHub API URL for the latest release of the app repo.

    Used where the JSON payload is needed (e.g. resolving the wheel asset to
    download for an update). This hits ``api.github.com`` and is therefore
    subject to the REST API rate limit, so it is only used on demand — never
    for the recurring update check (see ``latest_release_redirect_url``).
    """
    repo = (repo or _github_repo()).strip("/")
    return f"https://api.github.com/repos/{repo}/releases/latest"


def latest_release_redirect_url(repo: str | None = None) -> str:
    """Return the public github.com URL that redirects to the latest release.

    Unlike the REST API, this is served by the github.com web host and is not
    subject to the unauthenticated 60 req/hr per-IP rate limit that surfaced as
    "Update check failed: HTTP Error 403: rate limit exceeded" on shared/NAT
    egress IPs. Following the redirect lands on ``/releases/tag/<tag>``, and it
    resolves the latest *stable* (non-prerelease) release, matching the REST
    endpoint's semantics. No token is required.
    """
    repo = (repo or _github_repo()).strip("/")
    return f"https://github.com/{repo}/releases/latest"


def _release_page_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"User-Agent": "ciaobot-package-updater"}
    )


def _tag_from_url(url: str) -> str:
    """Extract the release version from a ``/releases/tag/<tag>`` URL."""
    match = re.search(r"/releases/tag/([^/?#]+)", url or "")
    if match:
        return match.group(1).strip().removeprefix("v")
    return ""


def _github_token() -> str:
    """Return a GitHub API token from the environment, if any.

    Authenticated requests raise GitHub's rate limit from 60 to 5000 req/hr,
    which matters on shared/NAT egress IPs where the unauthenticated pool is
    easily exhausted by other clients.
    """
    for name in ("CIAO_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _github_request(url: str) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ciaobot-package-updater",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _version_key(value: str) -> tuple:
    parts: list[tuple[int, object]] = []
    for part in re.findall(r"\d+|[A-Za-z]+", value):
        if part.isdigit():
            parts.append((1, int(part)))
        else:
            parts.append((0, part.lower()))
    return tuple(parts)


def package_status(
    *,
    current_version: str = __version__,
    repo: str | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 2.5,
) -> dict[str, object]:
    """Return installed and latest (GitHub release) package versions.

    Resolves the latest version by following the public ``/releases/latest``
    redirect on github.com rather than calling the REST API, so the recurring
    update check is not subject to the API's unauthenticated rate limit.
    """
    source = latest_release_redirect_url(repo)
    latest = ""
    error = ""
    try:
        with opener(_release_page_request(source), timeout=timeout) as response:
            # urlopen follows the 302 to /releases/tag/<tag>; the final URL
            # carries the version, so the response body is never read.
            if hasattr(response, "geturl"):
                final_url = response.geturl()
            else:
                final_url = getattr(response, "url", "") or source
        latest = _tag_from_url(final_url)
        if not latest:
            error = "Could not determine the latest release."
    except urllib.error.HTTPError as exc:
        # GitHub returns 403 (and sometimes 429) with a "rate limit exceeded"
        # reason under heavy load. Report it as a transient condition rather
        # than a raw HTTP error.
        if exc.code in (403, 429):
            error = "GitHub rate limit reached; the update check will retry later."
        else:
            error = f"HTTP {exc.code}: {exc.reason}"
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)

    update_available = bool(
        latest and _version_key(latest) > _version_key(current_version)
    )
    return {
        "current_version": current_version,
        "latest_version": latest,
        "update_available": update_available,
        "source": source,
        "error": error,
    }


def make_cached_package_status(
    *,
    fetch: Callable[[], dict[str, object]] = package_status,
    ttl_ok: float = 6 * 3600.0,
    ttl_error: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], dict[str, object]]:
    """Return a zero-arg callable that caches ``fetch`` results in-process.

    Successful lookups are cached for ``ttl_ok`` seconds so the update check
    contacts GitHub only a few times a day instead of on every Settings open —
    the main reason the shared-IP rate limit gets hit at all. When a refresh
    fails (e.g. a transient ``403 rate limit exceeded``), the last known-good
    result is served instead and a fresh attempt is retried after
    ``ttl_error`` seconds, so an intermittent rate limit never surfaces as an
    "Update check failed" banner once a good version has been seen.
    """
    state: dict[str, Any] = {"value": None, "good": None, "expires": 0.0}

    def cached() -> dict[str, object]:
        now = clock()
        current = state["value"]
        if current is not None and now < state["expires"]:
            return current

        result = fetch()
        if result.get("error") and state["good"] is not None:
            # Serve the last successful answer; retry again soon.
            state["value"] = state["good"]
            state["expires"] = now + ttl_error
            return state["good"]

        state["value"] = result
        state["expires"] = now + (ttl_ok if not result.get("error") else ttl_error)
        if not result.get("error"):
            state["good"] = result
        return result

    return cached


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
    request = _github_request(api_url)
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


def _resolve_brew() -> str | None:
    import shutil
    from pathlib import Path

    brew = shutil.which("brew")
    if brew:
        return brew
    for path in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(path).is_file():
            return path
    return None


def _is_homebrew_cellar_path(path: Path) -> bool:
    text = str(path)
    return "Cellar/ciaobot" in text or "Cellar/ciao/" in text


def _brew_installed_version(brew: str) -> str:
    """Return the Cellar version of ``ciaobot`` per Homebrew, or "" if unknown."""
    import subprocess

    try:
        result = subprocess.run(
            [brew, "list", "--versions", "ciaobot"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception:
        return ""
    parts = result.stdout.strip().split()
    return parts[-1] if len(parts) >= 2 else ""


def _pip_show_version(python_exe: str) -> str:
    """Return the installed ``ciaobot`` version per ``pip show``, or "" if unknown."""
    import subprocess

    try:
        result = subprocess.run(
            [python_exe, "-m", "pip", "show", "ciaobot"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except Exception:
        return ""
    for line in result.stdout.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    return ""


def detect_install_mode() -> str:
    """Return "homebrew", "pip_venv", "editable", or "unknown"."""
    import sys
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
        if _is_homebrew_cellar_path(executable):
            return "homebrew"
        if ciao_file is not None and _is_homebrew_cellar_path(ciao_file):
            return "homebrew"
    except Exception:
        pass

    if sys.prefix != sys.base_prefix:
        return "pip_venv"

    return "unknown"


def _latest_wheel_url(
    *,
    opener: Callable[..., object],
    timeout: float,
) -> tuple[str, str]:
    """Return (wheel_url, error) for the latest GitHub release."""
    source = latest_release_url()
    try:
        with opener(_github_request(source), timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        return "", f"Could not fetch the latest release from {source}: {exc}"

    assets = payload.get("assets") if isinstance(payload, dict) else None
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            url = asset.get("browser_download_url")
            if isinstance(url, str) and str(asset.get("name", url)).endswith(".whl"):
                return url, ""
    return "", "The latest release has no .whl asset."


def update_package(
    *,
    opener: Callable[..., object] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Perform package upgrade based on active install mode.

    Upgrades install the wheel asset of the latest GitHub release. The
    package is intentionally never upgraded from the PyPI index: the
    ``ciao`` name there belongs to an unrelated project.
    """
    import sys
    import subprocess

    manual_command = (
        "pip install -U <wheel from "
        f"https://github.com/{_github_repo()}/releases/latest>"
    )
    mode = detect_install_mode()
    if mode == "editable":
        return {
            "ok": False,
            "mode": mode,
            "error": "Editable checkouts must be updated manually via 'git pull'.",
            "command": "git pull",
        }

    before_version = ""
    check_version: Callable[[], str] = lambda: ""
    if mode == "homebrew":
        brew = _resolve_brew()
        if not brew:
            return {
                "ok": False,
                "mode": mode,
                "error": "Homebrew 'brew' command not found in PATH.",
                "command": "brew upgrade ciaobot",
            }
        # `brew upgrade` auto-updates tap metadata itself, but that's
        # throttled (HOMEBREW_AUTO_UPDATE_SECS, default 24h). Right after a
        # release goes out, the local formula cache can still resolve to the
        # old version, making the upgrade a silent no-op. Force a refresh
        # first; best-effort, since a failed/offline refresh shouldn't block
        # the upgrade attempt itself.
        try:
            subprocess.run(
                [brew, "update", "--quiet"],
                capture_output=True, text=True, timeout=60, check=False,
            )
        except Exception:
            pass
        cmd = [brew, "upgrade", "ciaobot"]
        check_version = lambda: _brew_installed_version(brew)
        before_version = check_version()
    elif mode == "pip_venv":
        wheel_url, error = _latest_wheel_url(opener=opener, timeout=timeout)
        if not wheel_url:
            return {
                "ok": False,
                "mode": mode,
                "error": f"{error} Please upgrade manually.",
                "command": manual_command,
            }
        cmd = [sys.executable, "-m", "pip", "install", "-U", wheel_url]
        check_version = lambda: _pip_show_version(sys.executable)
        before_version = check_version()
    else:
        return {
            "ok": False,
            "mode": mode,
            "error": f"Unknown install mode '{mode}'. Please upgrade manually.",
            "command": manual_command,
        }

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        if result.returncode == 0:
            after_version = check_version()
            if before_version and after_version and before_version == after_version:
                # Exit 0 doesn't mean an upgrade happened: e.g. Homebrew reports
                # success and no-ops when the tap formula hasn't caught up yet
                # with the GitHub release the update banner is checking against,
                # so the version never advances and the banner never clears.
                return {
                    "ok": False,
                    "mode": mode,
                    "error": (
                        f"Still on {after_version} after running the upgrade — the "
                        "package source has no newer version available yet. If "
                        "this keeps happening, the release may not have "
                        "propagated to it; try again shortly."
                    ),
                    "output": output,
                    "command": " ".join(cmd),
                }
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

