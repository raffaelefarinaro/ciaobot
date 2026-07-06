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
import subprocess
import sys
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

    face = icon_path("face.png")
    face_scared = icon_path("face_scared.png")

    app = rumps.App("Ciaobot", icon=face, quit_button=None)
    status_item = rumps.MenuItem(status_label(ServerStatus(False, False)))
    status_item.set_callback(None)

    def refresh(_timer=None) -> None:
        status = fetch_server_status(port)
        status_item.title = status_label(status)
        app.icon = face if status.reachable else face_scared

    def on_open(_sender) -> None:
        subprocess.run(open_app_command(workspace, port), check=False)

    def on_restart(_sender) -> None:
        subprocess.run(restart_server_command(), check=False)
        refresh()

    def on_logs(_sender) -> None:
        subprocess.run(view_logs_command(workspace), check=False)

    app.menu = [
        rumps.MenuItem("Open Ciaobot", callback=on_open),
        status_item,
        None,
        rumps.MenuItem("Restart Server", callback=on_restart),
        rumps.MenuItem("View Logs", callback=on_logs),
        None,
        rumps.MenuItem(
            "Quit Menu Bar Icon",
            callback=lambda _sender: rumps.quit_application(),
        ),
    ]

    rumps.Timer(refresh, POLL_SECONDS).start()
    refresh()
    app.run()
    return 0
