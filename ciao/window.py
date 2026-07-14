"""Native Ciaobot window for macOS (WebKit via pywebview).

Opens the local PWA inside a proper app window so ``Ciaobot.app`` is the UI,
not a separate Chrome install or ``--app`` frame.

Single instance: pywebview runs its own event loop on the main thread, so each
``python -m ciao.window`` invocation is a fresh process with its own window.
The menu bar, notifications, and the app launcher can all fire a launch, so
without coordination the windows stack up. A lock file under the workspace
holds the live window's PID; a second launch focuses that window instead of
opening a duplicate (chat navigation still happens over ``/ws/events`` via
``menubar.notify_open_chat``).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _lock_path(workspace: Path) -> Path:
    return workspace / ".runtime" / "window.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _active_window_pid(workspace: Path) -> int | None:
    """PID of a live Ciaobot window, or ``None`` if none is running."""

    try:
        text = _lock_path(workspace).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    return pid if _pid_alive(pid) else None


def _write_lock(workspace: Path) -> None:
    lock = _lock_path(workspace)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _clear_lock(workspace: Path) -> None:
    lock = _lock_path(workspace)
    try:
        if lock.read_text(encoding="utf-8").strip() == str(os.getpid()):
            lock.unlink()
    except OSError:
        pass


def _focus_running_window(pid: int) -> bool:
    """Bring the existing window's process to the foreground (best effort)."""

    if sys.platform != "darwin":
        return False
    try:
        from AppKit import (
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except Exception:
        return False
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return False
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    except Exception:
        return False
    return True


def _set_app_identity() -> None:
    """Present the window as "Ciaobot" with its icon, not "Python"/rocket.

    The menu bar spawns ``python -m ciao.window``, a process with no app bundle
    of its own, so macOS labels it "Python" with the generic rocket icon. Set
    the running application's icon to the packaged ``Ciaobot.icns`` and override
    the display name in the main bundle's info dictionary. Both are best-effort
    (the name override in particular is a bundle-less-process workaround macOS
    doesn't always honor) and must never break window startup.
    """

    if sys.platform != "darwin":
        return
    try:
        from importlib import resources

        from AppKit import NSApplication, NSImage

        ref = resources.files("ciao.stock").joinpath("deploy", "Ciaobot.icns")
        with resources.as_file(ref) as icns:
            image = NSImage.alloc().initWithContentsOfFile_(str(icns))
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        pass
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Ciaobot"
            info["CFBundleDisplayName"] = "Ciaobot"
    except Exception:
        pass


def run_window(url: str, workspace: str | os.PathLike[str] | None = None) -> int:
    """Show ``url`` in a native window. Falls back to browser app mode.

    When ``workspace`` is given and a Ciaobot window is already open, focus it
    and return instead of opening a second window.
    """

    if not url.startswith(("http://", "https://")):
        print(f"Refusing to open non-http URL: {url}", file=sys.stderr)
        return 1

    ws = Path(workspace) if workspace is not None else None
    if ws is not None:
        existing = _active_window_pid(ws)
        if existing is not None:
            _focus_running_window(existing)
            return 0

    try:
        import webview
    except ImportError:
        from ciao.menubar import browser_app_mode_command

        cmd = browser_app_mode_command(url)
        if cmd is None:
            cmd = ["open", url]
        return subprocess.call(cmd)

    # pywebview defaults to private_mode=True, which gives the WebKit view an
    # ephemeral data store: localStorage and cookies are wiped on every launch.
    # That resets client-only state each time the window opens — most visibly
    # the "Welcome to Ciaobot" tour, whose seen-flag lives in localStorage.
    # Persist the store under the workspace so it survives across launches.
    storage_path = None
    if ws is not None:
        candidate = ws / ".runtime" / "webview"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            storage_path = str(candidate)
        except OSError:
            storage_path = None

    if ws is not None:
        _write_lock(ws)
    _set_app_identity()
    try:
        webview.create_window(
            "Ciaobot",
            url,
            width=1280,
            height=840,
            min_size=(720, 520),
        )
        webview.start(private_mode=False, storage_path=storage_path)
    finally:
        if ws is not None:
            _clear_lock(ws)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ciao.window")
    parser.add_argument("url")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)
    return run_window(args.url, args.workspace)


if __name__ == "__main__":
    raise SystemExit(main())
