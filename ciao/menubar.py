"""macOS menu bar companion for the Ciaobot server.

Puts the Ciaobot face in the status bar (the scared face when the local
server is unreachable) with quick actions: open the PWA, restart the
launchd-managed server, and view logs. The Cocoa dependency (rumps) installs
automatically on macOS (platform marker in pyproject); on other platforms —
or if the install is missing it — the command degrades gracefully.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ciao.menubar_prefs import (
    notifications_enabled,
    set_notifications_enabled,
)
from ciao.package_version import _github_repo, make_cached_package_status, update_package


SERVER_LAUNCHD_LABEL = "com.ciao.server"
MENUBAR_LAUNCHD_LABEL = "com.ciao.menubar"
LAUNCHD_LABELS = (SERVER_LAUNCHD_LABEL, MENUBAR_LAUNCHD_LABEL)
POLL_SECONDS = 10.0

# Spinning-head animation played while a chat is working. Frame count matches
# the PNGs emitted by scripts/make_menubar_template_icons.py.
SPIN_FRAME_COUNT = 12
SPIN_INTERVAL_SECONDS = 0.12
# Pulsing dot icon beside working chats in the dropdown menu.
DOT_PULSE_FRAME_COUNT = 8
CHAT_ACTIVITY_ICON_SIZE = (14, 14)
# How often the background poller asks the server which chats are working.
# Decoupled from POLL_SECONDS so the icon reacts quickly without doing HTTP on
# the main run loop (which would stutter the animation).
WORKING_POLL_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ServerStatus:
    reachable: bool
    ready: bool


@dataclass(frozen=True, slots=True)
class StartAtLoginStatus:
    state: str

    @property
    def available(self) -> bool:
        return self.state in {"on", "off"}

    @property
    def enabled(self) -> bool:
        return self.state == "on"


def fetch_server_status(port: int, *, timeout: float = 2.0) -> ServerStatus:
    """Poll the unauthenticated startup-status endpoint of the local server."""

    url = f"http://localhost:{port}/api/startup-status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return ServerStatus(reachable=False, ready=False)
    if not isinstance(payload, dict):
        return ServerStatus(reachable=True, ready=False)
    return ServerStatus(reachable=True, ready=bool(payload.get("overall_ready", True)))


def fetch_active_chat_ids(port: int, *, timeout: float = 2.0) -> set[str]:
    """Chat IDs the server reports as working (streaming or running subagents)."""

    url = f"http://localhost:{port}/api/active-chats"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return set()
    if not isinstance(payload, dict):
        return set()
    ids = payload.get("active_chat_ids")
    return {str(chat_id) for chat_id in ids} if isinstance(ids, list) else set()


def notify_open_chat(port: int, chat_id: str, *, timeout: float = 2.0) -> bool:
    """Tell an already-open PWA to navigate to ``chat_id`` via /ws/events."""

    if not chat_id:
        return False
    url = f"http://localhost:{port}/api/open-chat/{urllib.parse.quote(chat_id, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("ok"))


def status_label(status: ServerStatus) -> str:
    if not status.reachable:
        return "Server: not running"
    if not status.ready:
        return "Server: starting…"
    return "Server: running"


def server_recovery_label(status: ServerStatus) -> str:
    """Action label for starting a stopped server or restarting a live one."""

    return "Restart Server" if status.reachable else "Start Server"


def open_url(workspace: Path, port: int) -> str:
    """URL the menu bar opens; reuses the Ciaobot.app setup token when present."""

    token = ""
    token_path = workspace / ".runtime" / "setup-token"
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    base = f"http://localhost:{port}/"
    return f"{base}?setup={token}" if token else base


# Bundle IDs of the native launcher this project installs itself (see
# cli.py's _write_app_shortcut / _OUR_BUNDLE_IDS). A browser-installed PWA can
# share the launcher's "Ciaobot.app" name, so browser_pwa_duplicate_paths()
# excludes these IDs to avoid removing our own shell-script wrapper.
_OUR_LAUNCHER_BUNDLE_IDS = frozenset({"local.ciao.app", "local.ciaobot.app"})


def _bundle_identifier(app_bundle: Path) -> str:
    try:
        with (app_bundle / "Contents" / "Info.plist").open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, ValueError):
        return ""
    return str(plist.get("CFBundleIdentifier") or "")


_BROWSER_APP_MODE_CANDIDATES = (
    "Google Chrome",
    "Google Chrome Canary",
    "Chromium",
    "Microsoft Edge",
    "Brave Browser",
)


def _installed_browser_app_names() -> list[str]:
    applications = Path("/Applications")
    return [
        name
        for name in _BROWSER_APP_MODE_CANDIDATES
        if (applications / f"{name}.app").is_dir()
    ]


def browser_app_mode_command(url: str) -> list[str] | None:
    """Open ``url`` in a browser app window without a separate PWA install."""

    if sys.platform != "darwin":
        return None
    names = _installed_browser_app_names()
    if not names:
        return None
    return ["open", "-a", names[0], "--args", f"--app={url}"]


def browser_pwa_duplicate_paths(app_name: str = "Ciaobot") -> list[Path]:
    """Browser-installed PWAs (and legacy helpers) that duplicate ``Ciaobot.app``."""

    home = Path.home()
    roots = [
        home / "Applications",
        Path("/Applications"),
        home / "Applications" / "Chrome Apps.localized",
        Path("/Applications") / "Chrome Apps.localized",
    ]
    seen: set[Path] = set()
    paths: list[Path] = []
    for root in roots:
        candidate = (root / f"{app_name}.app").resolve()
        if candidate in seen or not candidate.is_dir():
            continue
        seen.add(candidate)
        if _bundle_identifier(candidate) in _OUR_LAUNCHER_BUNDLE_IDS:
            continue
        paths.append(candidate)
    for root in (Path("/Applications"), home / "Applications"):
        legacy = (root / "Ciaobot Menu Bar.app").resolve()
        if legacy.is_dir() and legacy not in seen:
            paths.append(legacy)
            seen.add(legacy)
    return paths


def unregister_app_bundle(app_root: Path) -> None:
    """Drop a bundle from Launch Services so Launchpad stops listing it."""

    if sys.platform != "darwin":
        return
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if not lsregister.is_file():
        return
    try:
        subprocess.run(
            [str(lsregister), "-u", str(app_root)],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def remove_browser_pwa_duplicates(app_name: str = "Ciaobot") -> list[Path]:
    """Remove duplicate browser PWAs so only the native ``Ciaobot.app`` remains."""

    removed: list[Path] = []
    for path in browser_pwa_duplicate_paths(app_name):
        unregister_app_bundle(path)
        try:
            shutil.rmtree(path)
        except OSError:
            continue
        removed.append(path)
    return removed


def open_command(url: str) -> list[str]:
    """``open`` argv for a URL in an app-like window when possible."""

    app_mode = browser_app_mode_command(url)
    if app_mode is not None:
        return app_mode
    return ["open", url]


def _python_with_ciao() -> str:
    """Return a Python interpreter that can import ``ciao``.

    In the packaged menu-bar app ``sys.executable`` can point at a stock
    system Python that has no ``ciao`` on its path — observed as
    ``python3.12: No module named 'ciao'`` when the app bundle launches a
    plain interpreter, so ``python -m ciao.window`` (and the older
    ``-m ciao.cli``) failed silently. This process already imported ``ciao``
    from *some* environment, so derive that environment's interpreter from
    the package location and prefer it; fall back to ``sys.executable``.
    """

    try:
        import ciao as _ciao_pkg

        pkg_dir = Path(_ciao_pkg.__file__).resolve().parent  # .../site-packages/ciao
        # venv/keg layout: <prefix>/lib/pythonX.Y/site-packages/ciao, so the
        # interpreter lives at <prefix>/bin/python (parents[3]). Also probe
        # parents[2] for flatter layouts.
        for prefix in (pkg_dir.parents[3], pkg_dir.parents[2]):
            for name in ("python", "python3"):
                candidate = prefix / "bin" / name
                if candidate.exists():
                    return str(candidate)
    except Exception:
        pass
    return sys.executable


def _bundle_python() -> str | None:
    """Path to Ciaobot.app's bundle-local ``python`` symlink, if installed.

    Launching the window through the interpreter that lives inside
    ``Ciaobot.app/Contents/MacOS`` makes macOS attribute the process to the
    app bundle, so the Dock shows "Ciaobot" and its icon instead of a bare
    "Python" rocket. The symlink targets the real interpreter (written by
    cli.py's _write_menubar_helper), so ``ciao``/``webview`` still import.
    """

    for base in (Path.home() / "Applications", Path("/Applications")):
        candidate = base / "Ciaobot.app" / "Contents" / "MacOS" / "python"
        if candidate.exists():
            return str(candidate)
    return None


def _window_launch_command(url: str, workspace: Path | None) -> list[str]:
    """argv that opens ``url`` in the native window (single-instance aware).

    Prefer the app-bundle interpreter so the window carries Ciaobot's Dock
    identity; fall back to any interpreter that can import ``ciao``.
    """

    cmd = [_bundle_python() or _python_with_ciao(), "-m", "ciao.window", url]
    if workspace is not None:
        cmd += ["--workspace", str(workspace)]
    return cmd


def launch_ui(url: str, workspace: Path | None = None) -> None:
    """Open the Ciaobot UI without blocking the caller (menu bar callbacks).

    On macOS the URL opens in the native WebKit window (``ciao.window``);
    passing ``workspace`` lets it focus an already-open window instead of
    stacking a duplicate.
    """

    if sys.platform == "darwin":
        subprocess.Popen(
            _window_launch_command(url, workspace),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.run(open_command(url), check=False)


def restart_server_command(uid: int | None = None) -> list[str]:
    resolved = os.getuid() if uid is None else uid
    return ["launchctl", "kickstart", "-k", f"gui/{resolved}/{SERVER_LAUNCHD_LABEL}"]


def restart_menubar_command(uid: int | None = None) -> list[str]:
    resolved = os.getuid() if uid is None else uid
    return ["launchctl", "kickstart", "-k", f"gui/{resolved}/{MENUBAR_LAUNCHD_LABEL}"]


def default_launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def launch_agent_paths(launch_agents_dir: Path | None = None) -> dict[str, Path]:
    base = (
        default_launch_agents_dir()
        if launch_agents_dir is None
        else Path(launch_agents_dir).expanduser()
    )
    return {label: base / f"{label}.plist" for label in LAUNCHD_LABELS}


_DISABLED_LINE_RE = re.compile(r'^\s*"([^"]+)"\s*=>\s*(true|false)\s*$', re.MULTILINE)


def parse_launchctl_disabled(output: str) -> dict[str, bool]:
    """Parse ``launchctl print-disabled`` output into label -> disabled."""

    return {label: value == "true" for label, value in _DISABLED_LINE_RE.findall(output)}


def read_launchctl_disabled(uid: int | None = None) -> dict[str, bool] | None:
    resolved = os.getuid() if uid is None else uid
    try:
        completed = subprocess.run(
            ["launchctl", "print-disabled", f"gui/{resolved}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return parse_launchctl_disabled(completed.stdout)


def start_at_login_status(
    *,
    launch_agents_dir: Path | None = None,
    disabled_labels: dict[str, bool] | None = None,
    uid: int | None = None,
) -> StartAtLoginStatus:
    paths = launch_agent_paths(launch_agents_dir)
    if not all(path.is_file() for path in paths.values()):
        return StartAtLoginStatus("missing")

    disabled = disabled_labels if disabled_labels is not None else read_launchctl_disabled(uid)
    if disabled is None:
        return StartAtLoginStatus("unknown")

    enabled = all(not disabled.get(label, False) for label in LAUNCHD_LABELS)
    return StartAtLoginStatus("on" if enabled else "off")


def start_at_login_menu_label(status: StartAtLoginStatus) -> str:
    if status.state == "on":
        return "Start Ciao at Login: On"
    if status.state == "off":
        return "Start Ciao at Login: Off"
    if status.state == "missing":
        return "Start at Login: not installed"
    return "Start at Login: unknown"


def start_at_login_commands(enabled: bool, uid: int | None = None) -> list[list[str]]:
    resolved = os.getuid() if uid is None else uid
    action = "enable" if enabled else "disable"
    return [["launchctl", action, f"gui/{resolved}/{label}"] for label in LAUNCHD_LABELS]


def set_start_at_login_enabled(
    enabled: bool,
    *,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
) -> tuple[bool, str]:
    paths = launch_agent_paths(launch_agents_dir)
    if not all(path.is_file() for path in paths.values()):
        return False, "Ciaobot's login items are not installed. Run setup again to recreate them."

    errors: list[str] = []
    for command in start_at_login_commands(enabled, uid):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as exc:
            errors.append(str(exc))
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            errors.append(detail or " ".join(command))
    return not errors, "\n".join(errors)


def relaunch_stale_process(uid: int | None = None) -> None:
    """Ask launchd to restart this menu bar process onto the current install.

    A bundled resource (e.g. the status icon) going missing at runtime means
    Homebrew swapped the installed version out from under this already-running
    process — for example a bare `brew upgrade ciaobot` outside the app's own
    Update button, followed by `brew cleanup` removing the old keg. The
    process's already-imported ``ciao`` module keeps resolving resource paths
    into that now-deleted directory for the rest of its life, so retrying
    in-process can never succeed. `launchctl kickstart -k` re-resolves the
    launchd plist's `/opt/homebrew/opt/ciaobot/...` symlink, which always
    points at the current keg, so the relaunched process picks up the
    current install.
    """

    subprocess.run(restart_menubar_command(uid), check=False)


def stop_server_command(uid: int | None = None) -> list[str]:
    """Fully stop the server agent when quitting the menu bar.

    The server plist sets KeepAlive=true, so a plain kill would be relaunched
    immediately; `bootout` removes it from the launchd domain so it stays
    down. Ciaobot.app reloads it (launchctl load -w) on the next launch.
    """

    resolved = os.getuid() if uid is None else uid
    return ["launchctl", "bootout", f"gui/{resolved}/{SERVER_LAUNCHD_LABEL}"]


def view_logs_command(workspace: Path) -> list[str]:
    return ["open", str(workspace / ".runtime" / "ciao.stderr.log")]


def github_repo_url() -> str:
    return f"https://github.com/{_github_repo()}"


def github_new_issue_url() -> str:
    return f"{github_repo_url()}/issues/new"


def open_url_command(url: str) -> list[str]:
    return ["open", url]


def icon_path(name: str) -> str:
    # Menu bar assets ship in ciao.stock/deploy: the PWA build empties
    # ciao/web/static, so nothing committed may live there.
    return str(resources.files("ciao.stock").joinpath("deploy", name))


def _menubar_app_candidates() -> list[Path]:
    """Installed menu-bar app bundles, newest path first."""

    home_apps = Path.home() / "Applications"
    system_apps = Path("/Applications")
    names = ("Ciaobot.app",)
    candidates: list[Path] = []
    for root in (system_apps, home_apps):
        for name in names:
            candidate = root / name
            if candidate.is_dir():
                candidates.append(candidate)
    return candidates


def _ensure_notification_bundle() -> None:
    """Make Notification Center attribute alerts to Ciaobot, not Python.

    When launchd starts ``python -m ciao.cli menubar`` directly, macOS has no
    app identity and groups alerts under "Python". Setup writes a
    ``CiaobotMenuBar`` helper inside ``Ciaobot.app`` and the LaunchAgent
    should exec the bundle-local ``python`` symlink so alerts stay under the
    existing Ciaobot app. As a fallback, load that bundle explicitly when we
    can find it.
    """

    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
    except Exception:
        return

    main = NSBundle.mainBundle()
    if main is not None:
        ident = main.bundleIdentifier()
        if ident and ident not in ("org.python.python", "com.apple.python"):
            return

    for app_root in _menubar_app_candidates():
        bundle = NSBundle.bundleWithPath_(str(app_root))
        if bundle is not None and bundle.load():
            return


def spin_icon_paths() -> list[str]:
    """Ordered spinning-head frame paths that actually exist on disk.

    Returns an empty list on installs predating the spin frames so the menu
    bar falls back to the static icon instead of crashing.
    """

    paths = [icon_path(f"face_spin_{index:02d}.png") for index in range(SPIN_FRAME_COUNT)]
    return [path for path in paths if os.path.isfile(path)]


def dot_pulse_icon_paths() -> list[str]:
    """Ordered pulsing-dot frame paths for working chats in the dropdown menu."""

    paths = [icon_path(f"dot_pulse_{index:02d}.png") for index in range(DOT_PULSE_FRAME_COUNT)]
    return [path for path in paths if os.path.isfile(path)]


def _keep_timer_running_while_menu_open(timer) -> None:
    """Re-register a started rumps.Timer in NSRunLoopCommonModes.

    rumps schedules its NSTimer in the default run-loop mode only. While the
    status-item menu is open, the run loop sits in the event-tracking mode,
    so the timer stops firing and the spinning head plus the pulsing
    working-chat dots freeze mid-frame. Common modes include menu tracking,
    which keeps both animations running with the menu expanded.
    """

    try:
        from Foundation import NSRunLoop, NSRunLoopCommonModes

        nstimer = getattr(timer, "_nstimer", None)
        if nstimer is not None:
            NSRunLoop.currentRunLoop().addTimer_forMode_(nstimer, NSRunLoopCommonModes)
    except Exception:
        # Best-effort against rumps/pyobjc internals changing: the fallback
        # is the old behavior (animation pauses while the menu is open).
        pass


def workspace_menu_label(name: str) -> str:
    """Human-readable workspace name for menu labels (matches the PWA sidebar)."""

    text = name.strip()
    if not text:
        return "Workspace"
    parts = re.split(r"[-_\s]+", text)
    return " ".join(part.capitalize() for part in parts if part)


def chat_menu_title(
    title: str,
    *,
    unread: bool,
    needs_input: bool = False,
    working: bool = False,
    working_has_icon: bool = False,
    workspace: str = "",
    show_workspace: bool = False,
    max_length: int = 58,
) -> str:
    """Menu label for an open chat.

    Working chats get a pulsing template icon when frames are packaged;
    otherwise a static ◌ prefix. Chats blocked on AskUserQuestion get a ?
    prefix (even when already read). Unread chats get a ● prefix and can show
    both signals when a working chat is also unread (matching the PWA).
    """

    text = title.strip() or "Untitled chat"
    if needs_input:
        prefix = "? "
    elif working and not working_has_icon:
        prefix = "◌ "
    elif unread:
        prefix = "● "
    else:
        prefix = ""
    suffix = ""
    if show_workspace and workspace.strip():
        suffix = f" [{workspace_menu_label(workspace)}]"
    budget = max_length - len(prefix) - len(suffix)
    if len(text) > budget:
        text = text[: max(0, budget - 1)] + "…"
    return f"{prefix}{text}{suffix}"


def menubar_badge_title(attention_count: int) -> str:
    """Short count shown beside the menu bar icon (empty when nothing needs attention)."""

    if attention_count <= 0:
        return ""
    if attention_count > 99:
        return "99+"
    return str(attention_count)


def chat_url(workspace: Path, port: int, chat_id: str) -> str:
    base = f"http://localhost:{port}"
    return f"{base}/chat/{chat_id}" if chat_id else open_url(workspace, port)


@dataclass(frozen=True, slots=True)
class OpenChat:
    chat_id: str
    title: str
    last_activity_at: str
    workspace: str = ""


def _notification_entry_id(entry: dict[str, object]) -> str:
    """Stable identity for one append-only local notification-log entry."""

    return json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)


def read_notification_log(workspace: Path) -> list[dict[str, object]]:
    """Read valid entries from the local notification log.

    The PushManager appends this file from a background thread, so a reader
    can occasionally see a partial last line. Ignore that line and retry on
    the next refresh rather than letting a transient write break the tray.
    """

    path = workspace / ".runtime" / "notifications.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    entries: list[dict[str, object]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


@dataclass(slots=True)
class NotificationLogTail:
    """Track local push entries already observed by this menu-bar process."""

    seen_entry_ids: set[str]

    @classmethod
    def at_end(cls, workspace: Path) -> "NotificationLogTail":
        """Start at the current end, so relaunching never replays old alerts."""

        return cls({_notification_entry_id(entry) for entry in read_notification_log(workspace)})

    def read_new(self, workspace: Path) -> list[dict[str, object]]:
        """Return entries appended since the last read and advance the tail."""

        entries = read_notification_log(workspace)
        current_ids = {_notification_entry_id(entry) for entry in entries}
        new_entries = [
            entry for entry in entries if _notification_entry_id(entry) not in self.seen_entry_ids
        ]
        # PushManager retains a bounded log. Discard IDs it trimmed so the
        # tracker stays bounded as well.
        self.seen_entry_ids = current_ids
        return new_entries


def _load_web_state(workspace: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Projects and chats from the server's persisted PWA state."""

    state_path = workspace / ".runtime" / "web_projects.json"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    projects_raw = data.get("projects")
    chats_raw = data.get("chats")
    projects = (
        {str(project_id): project for project_id, project in projects_raw.items() if isinstance(project, dict)}
        if isinstance(projects_raw, dict)
        else {}
    )
    chats = (
        {str(chat_id): chat for chat_id, chat in chats_raw.items() if isinstance(chat, dict)}
        if isinstance(chats_raw, dict)
        else {}
    )
    return projects, chats


def _chat_workspace(chat: dict, projects: dict[str, dict]) -> str:
    project_id = str(chat.get("project_id") or "")
    project = projects.get(project_id, {})
    return str(project.get("workspace") or "") if isinstance(project, dict) else ""


def chat_is_unread(chat: dict) -> bool:
    """Match the PWA bell: unread when ``last_activity_at > last_read_at``."""

    if chat.get("archived"):
        return False
    activity = str(chat.get("last_activity_at") or "")
    read = str(chat.get("last_read_at") or "")
    return bool(activity) and activity > read


def chat_needs_input(chat: dict) -> bool:
    """True when the model paused on AskUserQuestion and awaits an answer."""

    if chat.get("archived"):
        return False
    raw = chat.get("pending_question")
    if not raw or not isinstance(raw, str):
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    questions = parsed.get("questions")
    return isinstance(questions, list) and len(questions) > 0


def _open_chat_from_state(
    chat_id: str, chat: dict, *, projects: dict[str, dict]
) -> OpenChat:
    return OpenChat(
        chat_id=chat_id,
        title=str(chat.get("title") or "Untitled chat"),
        last_activity_at=str(chat.get("last_activity_at") or ""),
        workspace=_chat_workspace(chat, projects),
    )


def read_open_chats(workspace: Path, *, limit: int = 10) -> list[OpenChat]:
    """Return non-archived chats from the server's persisted PWA state,
    most recently active first."""

    projects, chats = _load_web_state(workspace)
    open_chats: list[OpenChat] = []
    for chat_id, chat in chats.items():
        if chat.get("archived"):
            continue
        open_chats.append(_open_chat_from_state(chat_id, chat, projects=projects))
    open_chats.sort(key=lambda chat: chat.last_activity_at, reverse=True)
    return open_chats[:limit]


def read_unread_chats(workspace: Path, *, limit: int = 10) -> list[OpenChat]:
    """Unread non-archived chats, most recently active first — mirrors the PWA bell."""

    projects, chats = _load_web_state(workspace)
    unread: list[OpenChat] = []
    for chat_id, chat in chats.items():
        if not chat_is_unread(chat):
            continue
        unread.append(_open_chat_from_state(chat_id, chat, projects=projects))
    unread.sort(key=lambda chat: chat.last_activity_at, reverse=True)
    return unread[:limit]


def count_unread_chats(workspace: Path) -> int:
    """Total unread chat count, without the menu's display limit."""

    _, chats = _load_web_state(workspace)
    return sum(1 for chat in chats.values() if chat_is_unread(chat))


def count_attention_chats(workspace: Path) -> int:
    """Chats that are unread and/or blocked on AskUserQuestion."""

    _, chats = _load_web_state(workspace)
    return sum(
        1
        for chat in chats.values()
        if chat_is_unread(chat) or chat_needs_input(chat)
    )


def needs_input_chat_ids(workspace: Path) -> set[str]:
    """Non-archived chats waiting on AskUserQuestion."""

    _, chats = _load_web_state(workspace)
    return {
        chat_id
        for chat_id, chat in chats.items()
        if chat_needs_input(chat)
    }


_INET_RE = re.compile(r"^\s*inet (\d+\.\d+\.\d+\.\d+)", re.MULTILINE)


def parse_inet_addresses(ifconfig_text: str) -> list[str]:
    """IPv4 addresses from `ifconfig` output, loopback excluded, order kept."""

    seen: list[str] = []
    for address in _INET_RE.findall(ifconfig_text):
        if address.startswith("127.") or address in seen:
            continue
        seen.append(address)
    return seen


def server_addresses(
    port: int,
    *,
    ifconfig_text: str | None = None,
    local_hostname: str | None = None,
) -> list[str]:
    """URLs the PWA is reachable at: localhost, Bonjour name, LAN IPv4s.

    The server binds 0.0.0.0 (see CiaoConfig.pwa_host), so every interface
    address genuinely serves the app.
    """

    if ifconfig_text is None:
        try:
            ifconfig_text = subprocess.run(
                ["ifconfig", "-a"], capture_output=True, text=True, check=False
            ).stdout
        except OSError:
            ifconfig_text = ""
    if local_hostname is None:
        try:
            local_hostname = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:
            local_hostname = ""

    urls = [f"http://localhost:{port}/"]
    if local_hostname:
        urls.append(f"http://{local_hostname}.local:{port}/")
    urls.extend(f"http://{address}:{port}/" for address in parse_inet_addresses(ifconfig_text))
    return urls


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)


def _disabled_item(rumps, title: str):
    item = rumps.MenuItem(title)
    item.set_callback(None)
    return item


def update_menu_label(latest_version: str) -> str:
    version = latest_version.strip()
    return f"Update to {version}" if version else "Update available"


def package_update_fingerprint(status: dict[str, object]) -> tuple[bool, str]:
    return (
        bool(status.get("update_available")),
        str(status.get("latest_version") or ""),
    )


def run_menubar(workspace: Path, port: int) -> int:
    """Run the rumps status-bar app. Returns 1 if rumps is not installed."""

    try:
        import rumps
    except ImportError:
        print(
            "The Ciaobot menu bar app needs the 'rumps' dependency (installed\n"
            "automatically with ciao on macOS). Reinstall ciao or run:\n"
            "pip install rumps",
            file=sys.stderr,
        )
        return 1

    # Menu-bar-only process: without this the interpreter shows up in the
    # Dock and app switcher as "Python". Accessory policy = status bar only.
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(1)  # Accessory
    except Exception:
        pass

    def _on_main_thread(func, *args, **kwargs) -> None:
        """Run a UI callable on the Cocoa main thread.

        rumps/AppKit objects (NSAlert, NSWindow, notifications, menu items)
        may only be touched from the main thread; calling them from a worker
        thread raises "NSWindow should only be instantiated on the main
        thread!" and crashes the thread — which is exactly what the update
        flow's ``rumps.alert``/``rumps.notification``/``refresh`` calls did.
        Background work marshals its UI through here. Falls back to a direct
        call if PyObjC's AppHelper is unavailable.
        """

        try:
            from PyObjCTools import AppHelper
        except Exception:
            func(*args, **kwargs)
            return
        if args or kwargs:
            AppHelper.callAfter(lambda: func(*args, **kwargs))
        else:
            AppHelper.callAfter(func)

    _ensure_notification_bundle()

    # Monochrome template images (black + alpha): macOS tints them to
    # match the menu bar, like the built-in status icons. Regenerate
    # with scripts/make_menubar_template_icons.py.
    face = icon_path("face_template.png")
    face_scared = icon_path("face_scared_template.png")
    spin_frames = spin_icon_paths()
    dot_frames = dot_pulse_icon_paths()

    app = rumps.App("Ciaobot", icon=face, template=True, quit_button=None)

    @rumps.notifications
    def on_notification_click(info: object) -> None:
        chat_id = ""
        if isinstance(info, dict):
            chat_id = str(info.get("chat_id") or "")
        else:
            data = getattr(info, "data", None)
            if isinstance(data, dict):
                chat_id = str(data.get("chat_id") or "")
        if chat_id:
            notify_open_chat(port, chat_id)
            launch_ui(chat_url(workspace, port, chat_id), workspace)
        else:
            launch_ui(open_url(workspace, port), workspace)

    # Only notify for entries newer than launch, not the whole backlog.
    notification_log = NotificationLogTail.at_end(workspace)
    status_fetcher = make_cached_package_status()
    state = {
        "fingerprint": None,
        "package_status": status_fetcher(),
        "updating": False,
        # Live work state, kept fresh by a background poller so the icon can
        # spin without blocking the run loop on HTTP.
        "reachable": False,
        "working_ids": set(),
        "spin_index": 0,
        "pulse_index": 0,
        "current_icon": None,
        "chat_items": {},
    }

    def _set_icon(path: str) -> None:
        # Avoid reassigning the same icon 8x/second when idle; only the spin
        # frames actually change frame-to-frame.
        if state["current_icon"] != path:
            state["current_icon"] = path
            try:
                app.icon = path
            except OSError:
                # The icon file is gone from disk: see relaunch_stale_process.
                # Fix it by relaunching rather than crash-looping on every
                # animation frame for the rest of this process's life.
                relaunch_stale_process()
                os._exit(1)

    def _open_chat_callback(chat_id: str):
        def _callback(_sender) -> None:
            notify_open_chat(port, chat_id)
            launch_ui(chat_url(workspace, port, chat_id), workspace)

        return _callback

    def _copy_address_callback(url: str):
        def _callback(_sender) -> None:
            copy_to_clipboard(url)

        return _callback

    def on_open(_sender) -> None:
        launch_ui(open_url(workspace, port), workspace)

    def on_toggle_notifications(_sender) -> None:
        enabled = not notifications_enabled(workspace)
        set_notifications_enabled(workspace, enabled)
        try:
            _sender.state = 1 if enabled else 0
        except Exception:
            pass

    def on_recover_server(_sender) -> None:
        current = fetch_server_status(port)
        if current.reachable:
            active_chat_ids = fetch_active_chat_ids(port)
            if active_chat_ids:
                count = len(active_chat_ids)
                rumps.alert(
                    title="Ciaobot is still working",
                    message=(
                        f"{count} chat{' is' if count == 1 else 's are'} still active. "
                        "Wait for the work to finish, then restart the server."
                    ),
                    ok="OK",
                    icon_path=icon_path("Ciaobot.icns"),
                )
                return
            if not rumps.alert(
                title="Restart Ciaobot server?",
                message="No active chats are running. Restart the local server now?",
                ok="Restart",
                cancel="Cancel",
                icon_path=icon_path("Ciaobot.icns"),
            ):
                return
        try:
            completed = subprocess.run(
                restart_server_command(), capture_output=True, text=True, check=False
            )
        except OSError as exc:
            rumps.alert(title="Could not start server", message=str(exc), ok="OK")
            return
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            rumps.alert(
                title="Could not start server",
                message=detail or "launchctl did not accept the request.",
                ok="OK",
                icon_path=icon_path("Ciaobot.icns"),
            )
        refresh()

    def on_logs(_sender) -> None:
        subprocess.run(view_logs_command(workspace), check=False)

    def on_view_github(_sender) -> None:
        subprocess.run(open_url_command(github_repo_url()), check=False)

    def on_report_issue(_sender) -> None:
        subprocess.run(open_url_command(github_new_issue_url()), check=False)

    def on_toggle_start_at_login(_sender) -> None:
        current = start_at_login_status()
        if not current.available:
            rumps.alert(
                title="Login item unavailable",
                message=(
                    "Ciaobot's login items are not installed yet. "
                    "Run setup again to recreate them."
                ),
                ok="OK",
                icon_path=icon_path("Ciaobot.icns"),
            )
            refresh()
            return

        ok, error = set_start_at_login_enabled(not current.enabled)
        if not ok:
            rumps.alert(
                title="Could not update login item",
                message=error or "launchctl did not accept the change.",
                ok="OK",
                icon_path=icon_path("Ciaobot.icns"),
            )
        refresh()

    def on_update(_sender) -> None:
        if state["updating"]:
            return
        pkg = state["package_status"]
        latest = str(pkg.get("latest_version") or "the latest version")
        if not rumps.alert(
            title="Update Ciaobot?",
            message=(
                f"This installs version {latest} and restarts the server. "
                "Scheduled tasks pause briefly during the restart."
            ),
            ok="Update",
            cancel="Cancel",
            icon_path=icon_path("Ciaobot.icns"),
        ):
            return

        state["updating"] = True
        refresh()

        def _do_update() -> None:
            # Runs on a worker thread: the blocking install and launchctl
            # calls are safe here, but every rumps/AppKit call MUST be
            # marshaled to the main thread via _on_main_thread — otherwise
            # NSAlert/NSWindow instantiation crashes the thread.
            try:
                res = update_package()
                if res.get("ok"):
                    subprocess.run(restart_server_command(), check=False)
                    # NSUserNotificationCenter, not osascript: a detached
                    # `display notification` process has no bundle icon of
                    # its own, so macOS shows a generic placeholder. Posting
                    # through rumps with an explicit icon renders the actual
                    # Ciaobot face instead.
                    def _notify_installed() -> None:
                        try:
                            rumps.notification(
                                "Ciaobot updated",
                                "",
                                f"Version {latest} is installed.",
                                icon=icon_path("Ciaobot.icns"),
                            )
                        except Exception:
                            pass

                    _on_main_thread(_notify_installed)
                    # Relaunch via launchd so this process loads the new wheel.
                    subprocess.run(restart_menubar_command(), check=False)
                    return
                error = str(res.get("error") or "Update failed.")
                command = str(res.get("command") or "")
                message = error + (f"\n\nTry manually:\n{command}" if command else "")
                _on_main_thread(
                    rumps.alert, title="Update failed", message=message, ok="OK"
                )
            finally:
                state["updating"] = False
                state["package_status"] = status_fetcher()
                _on_main_thread(refresh)

        threading.Thread(target=_do_update, daemon=True).start()

    def on_quit(_sender) -> None:
        # The menu bar is the visible presence of a running Ciaobot: quitting
        # it stops the server too. Confirm first, since that halts scheduled
        # tasks and any in-flight agent work until Ciaobot is opened again.
        if not rumps.alert(
            title="Quit Ciaobot?",
            message=(
                "This also stops the Ciaobot server. Scheduled tasks and any "
                "running agents won't run until you open Ciaobot again."
            ),
            ok="Quit",
            cancel="Cancel",
            icon_path=icon_path("Ciaobot.icns"),
        ):
            return
        subprocess.run(stop_server_command(), check=False)
        rumps.quit_application()

    def _rebuild_menu(
        status: ServerStatus,
        chats: list[OpenChat],
        unread_ids: set[str],
        needs_input_ids: set[str],
        working_ids: set[str],
        addresses: list[str],
        pkg: dict[str, object],
        login_status: StartAtLoginStatus,
    ) -> None:
        addresses_menu = rumps.MenuItem("Addresses (click to copy)")
        for url in addresses:
            addresses_menu.add(rumps.MenuItem(url, callback=_copy_address_callback(url)))

        # Open chats live directly in the menu, no header — just the list.
        show_workspace = len({chat.workspace for chat in chats if chat.workspace}) > 1
        chat_items_by_id: dict[str, object] = {}
        chat_items = []
        for chat in chats:
            working = chat.chat_id in working_ids
            unread = chat.chat_id in unread_ids
            needs_input = chat.chat_id in needs_input_ids
            working_has_icon = working and bool(dot_frames)
            item = rumps.MenuItem(
                chat_menu_title(
                    chat.title,
                    unread=unread,
                    needs_input=needs_input,
                    working=working,
                    working_has_icon=working_has_icon,
                    workspace=chat.workspace,
                    show_workspace=show_workspace,
                ),
                callback=_open_chat_callback(chat.chat_id),
                icon=dot_frames[0] if working_has_icon else None,
                dimensions=CHAT_ACTIVITY_ICON_SIZE,
                template=True if working_has_icon else None,
            )
            chat_items.append(item)
            chat_items_by_id[chat.chat_id] = item
        state["chat_items"] = chat_items_by_id
        update_items: list[object] = []
        if pkg.get("update_available"):
            label = (
                "Updating…"
                if state["updating"]
                else update_menu_label(str(pkg.get("latest_version") or ""))
            )
            if state["updating"]:
                update_items = [_disabled_item(rumps, label)]
            else:
                update_items = [rumps.MenuItem(label, callback=on_update)]

        login_item = rumps.MenuItem(
            start_at_login_menu_label(login_status),
            callback=on_toggle_start_at_login if login_status.available else None,
        )
        if not login_status.available:
            login_item.set_callback(None)
        else:
            try:
                login_item.state = 1 if login_status.enabled else 0
            except Exception:
                pass

        # Rarely-touched items live behind one "Advanced" submenu so the
        # top-level menu stays focused on open/status/chats.
        version = str(pkg.get("current_version") or "")
        advanced_menu = rumps.MenuItem("Advanced")
        advanced_menu.add(_disabled_item(rumps, f"Version {version}" if version else "Version unknown"))
        advanced_menu.add(None)
        advanced_menu.add(rumps.MenuItem("View on GitHub", callback=on_view_github))
        advanced_menu.add(rumps.MenuItem("Report an Issue", callback=on_report_issue))
        advanced_menu.add(None)
        advanced_menu.add(rumps.MenuItem("View Logs", callback=on_logs))
        advanced_menu.add(addresses_menu)
        notifications_item = rumps.MenuItem(
            "Notifications",
            callback=on_toggle_notifications,
        )
        try:
            notifications_item.state = 1 if notifications_enabled(workspace) else 0
        except Exception:
            pass
        advanced_menu.add(notifications_item)
        advanced_menu.add(None)
        advanced_menu.add(login_item)

        # Each group is rendered as its own section; separators are inserted
        # between non-empty groups so there are never doubled or trailing lines.
        groups = [
            [rumps.MenuItem("Open Ciaobot", callback=on_open),
             _disabled_item(rumps, status_label(status)),
             rumps.MenuItem(server_recovery_label(status), callback=on_recover_server)],
            update_items,
            chat_items,
            [advanced_menu],
            [rumps.MenuItem("Quit Ciaobot", callback=on_quit)],
        ]

        menu: list[object] = []
        for group in groups:
            if not group:
                continue
            if menu:
                menu.append(None)
            menu.extend(group)

        app.menu.clear()
        app.menu = menu

    def _animate_working_chat_icons() -> None:
        if not dot_frames or not state["working_ids"]:
            return
        frame = dot_frames[state["pulse_index"] % len(dot_frames)]
        for chat_id in state["working_ids"]:
            item = state["chat_items"].get(chat_id)
            if item is not None:
                item.set_icon(frame, dimensions=CHAT_ACTIVITY_ICON_SIZE, template=True)
        state["pulse_index"] = (state["pulse_index"] + 1) % len(dot_frames)

    def animate_icon(_timer=None) -> None:
        # Owns the status bar icon: spins the head while a chat is working or
        # a self-update is in flight, otherwise shows the resting face (or the
        # scared face when the server is unreachable). Pure state read — no
        # HTTP — so it stays smooth.
        if not state["reachable"]:
            _set_icon(face_scared)
            return
        if (state["working_ids"] or state["updating"]) and spin_frames:
            state["spin_index"] = (state["spin_index"] + 1) % len(spin_frames)
            _set_icon(spin_frames[state["spin_index"]])
        else:
            _set_icon(face)
        _animate_working_chat_icons()

    def poll_working() -> None:
        # Background loop so the working HTTP probe never blocks the run loop.
        while True:
            state["working_ids"] = fetch_active_chat_ids(port)
            time.sleep(WORKING_POLL_SECONDS)

    def refresh(_timer=None) -> None:
        native_enabled = notifications_enabled(workspace)
        for notification in notification_log.read_new(workspace):
            if not native_enabled:
                continue
            chat_id = str(notification.get("chat_id") or "")
            payload = {"chat_id": chat_id} if chat_id else None
            try:
                rumps.notification(
                    str(notification.get("title") or "Ciaobot"),
                    "",
                    str(notification.get("body") or "New notification"),
                    data=payload,
                    icon=icon_path("Ciaobot.icns"),
                )
            except Exception:
                # A notification failure should not prevent the normal menu
                # refresh (or leave a stale unread count) behind.
                pass

        status = fetch_server_status(port)
        state["reachable"] = status.reachable

        chats = read_open_chats(workspace)
        unread_chats = read_unread_chats(workspace)
        unread_ids = {chat.chat_id for chat in unread_chats}
        needs_input_ids = needs_input_chat_ids(workspace)
        attention_count = count_attention_chats(workspace)
        working_ids = set(state["working_ids"])
        addresses = server_addresses(port)
        pkg = status_fetcher()
        login_status = start_at_login_status()
        state["package_status"] = pkg
        app.title = menubar_badge_title(attention_count)

        # Rebuild only when content changed so an open menu doesn't flicker.
        show_workspace = len({chat.workspace for chat in chats if chat.workspace}) > 1
        fingerprint = (
            status,
            tuple(
                (
                    c.chat_id,
                    c.title,
                    c.workspace,
                    c.chat_id in unread_ids,
                    c.chat_id in needs_input_ids,
                    c.chat_id in working_ids,
                    show_workspace,
                )
                for c in chats
            ),
            attention_count,
            tuple(addresses),
            package_update_fingerprint(pkg),
            state["updating"],
            login_status.state,
        )
        if fingerprint != state["fingerprint"]:
            state["fingerprint"] = fingerprint
            _rebuild_menu(status, chats, unread_ids, needs_input_ids, working_ids, addresses, pkg, login_status)

    threading.Thread(target=poll_working, daemon=True).start()
    rumps.Timer(refresh, POLL_SECONDS).start()
    animate_timer = rumps.Timer(animate_icon, SPIN_INTERVAL_SECONDS)
    animate_timer.start()
    _keep_timer_running_while_menu_open(animate_timer)
    refresh()
    animate_icon()
    app.run()
    return 0
