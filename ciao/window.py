"""Native Ciaobot window for macOS (WebKit via pywebview).

Opens the local PWA inside a proper app window so ``Ciaobot.app`` is the UI,
not a separate Chrome install or ``--app`` frame.
"""

from __future__ import annotations

import subprocess
import sys


def run_window(url: str) -> int:
    """Show ``url`` in a native window. Falls back to browser app mode."""

    if not url.startswith(("http://", "https://")):
        print(f"Refusing to open non-http URL: {url}", file=sys.stderr)
        return 1
    try:
        import webview
    except ImportError:
        from ciao.menubar import browser_app_mode_command

        cmd = browser_app_mode_command(url)
        if cmd is None:
            cmd = ["open", url]
        return subprocess.call(cmd)

    webview.create_window(
        "Ciaobot",
        url,
        width=1280,
        height=840,
        min_size=(720, 520),
    )
    webview.start()
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m ciao.window <url>", file=sys.stderr)
        return 2
    return run_window(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
