"""macOS menu bar companion for the Ciaobot server.

Puts the Ciaobot face in the status bar (the scared face when the local
server is unreachable) with quick actions: open the PWA, restart the
launchd-managed server, and view logs. The Cocoa dependency (rumps) is
optional so the base package stays slim; install with
``pip install 'ciao[menubar]'``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


SERVER_LAUNCHD_LABEL = "com.ciao.server"
POLL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ServerStatus:
    reachable: bool
    ready: bool


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


def status_label(status: ServerStatus) -> str:
    if not status.reachable:
        return "Server: not running"
    if not status.ready:
        return "Server: starting…"
    return "Server: running"


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


def open_app_command(workspace: Path, port: int) -> list[str]:
    return ["open", open_url(workspace, port)]


def restart_server_command(uid: int | None = None) -> list[str]:
    resolved = os.getuid() if uid is None else uid
    return ["launchctl", "kickstart", "-k", f"gui/{resolved}/{SERVER_LAUNCHD_LABEL}"]


def view_logs_command(workspace: Path) -> list[str]:
    return ["open", str(workspace / ".runtime" / "ciao.stderr.log")]


def icon_path(name: str) -> str:
    return str(resources.files("ciao.web").joinpath("static", name))


@dataclass(frozen=True, slots=True)
class Notification:
    ts: float
    title: str
    body: str
    chat_id: str


def read_notifications(workspace: Path, *, limit: int = 10) -> list[Notification]:
    """Read the newest entries of the server's notification log, newest first."""

    log_path = workspace / ".runtime" / "notifications.jsonl"
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[Notification] = []
    for line in lines[-limit * 2 :]:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        entries.append(
            Notification(
                ts=float(data.get("ts", 0.0)),
                title=str(data.get("title", "Ciaobot")),
                body=str(data.get("body", "")),
                chat_id=str(data.get("chat_id", "")),
            )
        )
    entries.sort(key=lambda entry: entry.ts, reverse=True)
    return entries[:limit]


def notification_menu_title(notification: Notification, *, max_length: int = 60) -> str:
    text = f"{notification.title}: {notification.body}".strip().rstrip(":")
    if len(text) > max_length:
        text = text[: max_length - 1] + "…"
    return text


def chat_url(workspace: Path, port: int, chat_id: str) -> str:
    base = f"http://localhost:{port}"
    return f"{base}/chat/{chat_id}" if chat_id else open_url(workspace, port)


def notify_command(title: str, body: str) -> list[str]:
    """Native macOS notification via osascript (no app signing required)."""

    script = (
        f"display notification {json.dumps(body)} "
        f"with title {json.dumps(title)}"
    )
    return ["osascript", "-e", script]


@dataclass(frozen=True, slots=True)
class OpenChat:
    chat_id: str
    title: str
    last_activity_at: float


def read_open_chats(workspace: Path, *, limit: int = 10) -> list[OpenChat]:
    """Return non-archived chats from the server's persisted PWA state,
    most recently active first."""

    state_path = workspace / ".runtime" / "web_projects.json"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, dict):
        return []
    open_chats: list[OpenChat] = []
    for chat_id, chat in chats.items():
        if not isinstance(chat, dict) or chat.get("archived"):
            continue
        try:
            last_activity = float(chat.get("last_activity_at") or 0.0)
        except (TypeError, ValueError):
            last_activity = 0.0
        open_chats.append(
            OpenChat(
                chat_id=str(chat_id),
                title=str(chat.get("title") or "Untitled chat"),
                last_activity_at=last_activity,
            )
        )
    open_chats.sort(key=lambda chat: chat.last_activity_at, reverse=True)
    return open_chats[:limit]


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


def _settings_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "menubar_settings.json"


def read_banners_muted(workspace: Path) -> bool:
    try:
        data = json.loads(_settings_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(data.get("banners_muted")) if isinstance(data, dict) else False


def write_banners_muted(workspace: Path, muted: bool) -> None:
    path = _settings_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"banners_muted": muted}) + "\n", encoding="utf-8")


def _disabled_item(rumps, title: str):
    item = rumps.MenuItem(title)
    item.set_callback(None)
    return item


def run_menubar(workspace: Path, port: int) -> int:
    """Run the rumps status-bar app. Returns 1 if rumps is not installed."""

    try:
        import rumps
    except ImportError:
        print(
            "The Ciaobot menu bar app needs the optional 'rumps' dependency.\n"
            "Install it with: pip install 'ciao[menubar]'",
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

    # Monochrome template images (black + alpha): macOS tints them to
    # match the menu bar, like the built-in status icons. Regenerate
    # with scripts/make_menubar_template_icons.py.
    face = icon_path("face_template.png")
    face_scared = icon_path("face_scared_template.png")

    app = rumps.App("Ciaobot", icon=face, template=True, quit_button=None)

    # Only notify for entries newer than launch, not the whole backlog.
    state = {
        "last_seen_ts": time.time(),
        "fingerprint": None,
        "banners_muted": read_banners_muted(workspace),
    }

    def on_toggle_mute(sender) -> None:
        muted = not state["banners_muted"]
        state["banners_muted"] = muted
        write_banners_muted(workspace, muted)
        sender.state = 1 if muted else 0

    def _open_chat_callback(chat_id: str):
        def _callback(_sender) -> None:
            subprocess.run(["open", chat_url(workspace, port, chat_id)], check=False)

        return _callback

    def _copy_address_callback(url: str):
        def _callback(_sender) -> None:
            copy_to_clipboard(url)

        return _callback

    def on_open(_sender) -> None:
        subprocess.run(open_app_command(workspace, port), check=False)

    def on_restart(_sender) -> None:
        subprocess.run(restart_server_command(), check=False)
        refresh()

    def on_logs(_sender) -> None:
        subprocess.run(view_logs_command(workspace), check=False)

    def _rebuild_menu(
        status: ServerStatus,
        chats: list[OpenChat],
        entries: list[Notification],
        addresses: list[str],
    ) -> None:
        notifications_menu = rumps.MenuItem("Notifications")
        if not entries:
            notifications_menu.add(_disabled_item(rumps, "No notifications yet"))
        for entry in entries:
            notifications_menu.add(
                rumps.MenuItem(
                    notification_menu_title(entry),
                    callback=_open_chat_callback(entry.chat_id),
                )
            )

        addresses_menu = rumps.MenuItem("Addresses (click to copy)")
        for url in addresses:
            addresses_menu.add(rumps.MenuItem(url, callback=_copy_address_callback(url)))

        # Open chats live directly in the menu, no header — just the list.
        chat_items = [
            rumps.MenuItem(
                chat.title if len(chat.title) <= 60 else chat.title[:59] + "…",
                callback=_open_chat_callback(chat.chat_id),
            )
            for chat in chats
        ]
        chat_section = [*chat_items, None] if chat_items else []

        app.menu.clear()
        app.menu = [
            rumps.MenuItem("Open Ciaobot", callback=on_open),
            _disabled_item(rumps, status_label(status)),
            None,
            *chat_section,
            notifications_menu,
            addresses_menu,
            None,
            _mute_item(),
            None,
            rumps.MenuItem("Restart Server", callback=on_restart),
            rumps.MenuItem("View Logs", callback=on_logs),
            None,
            rumps.MenuItem("Quit", callback=lambda _sender: rumps.quit_application()),
        ]

    def _mute_item():
        item = rumps.MenuItem("Mute Banners", callback=on_toggle_mute)
        item.state = 1 if state["banners_muted"] else 0
        return item

    def refresh(_timer=None) -> None:
        status = fetch_server_status(port)
        app.icon = face if status.reachable else face_scared

        entries = read_notifications(workspace)
        chats = read_open_chats(workspace)
        addresses = server_addresses(port)

        # Rebuild only when content changed so an open menu doesn't flicker.
        fingerprint = (
            status,
            tuple((c.chat_id, c.title) for c in chats),
            tuple((e.ts, e.title, e.body) for e in entries),
            tuple(addresses),
        )
        if fingerprint != state["fingerprint"]:
            state["fingerprint"] = fingerprint
            _rebuild_menu(status, chats, entries, addresses)

        fresh = [e for e in entries if e.ts > state["last_seen_ts"]]
        if fresh:
            # Advance the cursor even when muted so unmuting doesn't replay
            # the backlog; the submenu still lists everything.
            state["last_seen_ts"] = max(e.ts for e in fresh)
            if not state["banners_muted"]:
                for entry in fresh[:3]:
                    subprocess.run(notify_command(entry.title, entry.body), check=False)

    rumps.Timer(refresh, POLL_SECONDS).start()
    refresh()
    app.run()
    return 0
