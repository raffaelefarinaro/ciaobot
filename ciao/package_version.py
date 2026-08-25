"""Installed package version and update-check helpers."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    pass

from ciao import __version__


DEFAULT_GITHUB_REPO = "raffaelefarinaro/ciaobot"


def _github_repo() -> str:
    """Return the GitHub repo (owner/name) used for release lookups."""
    return (os.environ.get("CIAO_GITHUB_REPO") or "").strip() or DEFAULT_GITHUB_REPO


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
    opener: Callable[..., AbstractContextManager[Any]] = urllib.request.urlopen,
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
        "mode": detect_install_mode(),
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
            return cast("dict[str, object]", current)

        result = fetch()
        if result.get("error") and state["good"] is not None:
            # Serve the last successful answer; retry again soon.
            state["value"] = state["good"]
            state["expires"] = now + ttl_error
            return cast("dict[str, object]", state["good"])

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
    opener: Callable[..., AbstractContextManager[Any]] = urllib.request.urlopen,
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
                raw_sha = entry.get("sha")
                sha = raw_sha if isinstance(raw_sha, str) else ""
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
    """Return the runtime distribution mode used by the current process."""
    import sys
    from pathlib import Path

    # The bundled marker is authoritative. The embedded runtime imports the
    # same ``ciao`` package as a source checkout, so checking the checkout
    # first misclassifies a bundled app during development and in tests.
    try:
        executable = Path(sys.executable).resolve()
        if os.environ.get("CIAO_BUNDLED_APP") or "Ciaobot.app/Contents/Resources/ciao-runtime" in str(executable):
            return "bundled_app"
    except Exception:
        pass

    try:
        import ciao

        ciao_file = Path(ciao.__file__).resolve()
        project_root = ciao_file.parent.parent
        git_marker = project_root / ".git"
        if (
            (project_root / "pyproject.toml").is_file()
            and (git_marker.is_dir() or git_marker.is_file())
        ):
            return "editable"
    except Exception:
        ciao_file = None

    return "unknown"


_VERSION_OUTPUT_RE = re.compile(r"[0-9][0-9A-Za-z.\-+]*")


def installed_version(timeout_s: float = 10.0) -> str | None:
    """Version of the package currently on disk, probed in a fresh process.

    The running process pins ``ciao.__version__`` at import time; a fresh
    interpreter is the only reliable way to observe a development install that
    changed on disk. Returns ``None`` whenever the probe cannot answer.
    """
    import subprocess

    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", "import ciao; print(ciao.__version__)"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out if _VERSION_OUTPUT_RE.fullmatch(out) else None


class InstallWatcher:
    """Decide when a running server should restart onto a newer install.

    Two independent signals, checked by the caller on its own schedule:

    - :meth:`check_files` — the package directory this process imported from
      no longer exists. Definitive; restart immediately.
    - :meth:`check_version` — an optional development probe reports a
      different version on **two consecutive readings**.

    Both return a human-readable restart reason, or ``None``.
    """

    def __init__(
        self,
        running_version: str = __version__,
        *,
        probe: Callable[[], str | None] | None = None,
        present: Callable[[], bool] | None = None,
    ) -> None:
        self._running = running_version
        self._probe = probe or installed_version
        self._present = present or running_install_present
        self._pending: str | None = None

    def check_files(self) -> str | None:
        if not self._present():
            return "Package files vanished (install swapped by an upgrade)"
        return None

    def check_version(self) -> str | None:
        probed = self._probe()
        if not probed or probed == self._running:
            self._pending = None
            return None
        if probed == self._pending:
            return f"Installed package is now {probed} (running {self._running})"
        self._pending = probed
        return None


def running_install_present() -> bool:
    """False when the running install's files were swapped out from under it.

    A missing ``ciao/__init__.py`` at the imported path means a development
    checkout or runtime was removed while the server was running. The guard is
    intentionally conservative and fails open if the import location cannot be
    inspected.
    """
    from pathlib import Path

    try:
        import ciao

        return Path(ciao.__file__).exists()
    except Exception:
        return True


def update_package(
    *,
    opener: Callable[..., AbstractContextManager[Any]] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Explain how the current distribution is updated.

    Production installs are updated atomically by the Tauri app updater or by
    re-running the signed one-line installer. There is no package-manager
    branch here anymore.
    """
    mode = detect_install_mode()
    if mode == "editable":
        return {
            "ok": False,
            "mode": mode,
            "error": "Editable checkouts must be updated manually via 'git pull'.",
            "command": "git pull",
        }
    if mode == "bundled_app":
        return {
            "ok": False,
            "already_current": True,
            "mode": mode,
            "error": "The bundled app and engine update together through Ciaobot.app.",
            "command": "curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh",
        }
    return {
        "ok": False,
        "mode": mode,
        "error": "This checkout is not a supported production installation.",
        "command": "curl -fsSL https://github.com/raffaelefarinaro/ciaobot/releases/latest/download/install.sh | sh",
    }
