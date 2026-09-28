"""The service worker's offline story.

A reverse proxy in front of Ciaobot answers 502/503/504 while the engine behind
it is down. That is a live HTTP response, so the network-first branch's
``.catch`` never fires and the browser shows the proxy's raw error page instead
of the cached app shell. These two tests pin the fix and the invariant that the
packaged copy of the worker is the same file the PWA ships.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SW_SOURCE = REPO_ROOT / "web" / "public" / "sw.js"
SW_PACKAGED = REPO_ROOT / "ciao" / "web" / "static" / "sw.js"


def test_navigation_falls_back_to_shell_on_proxy_errors() -> None:
    source = SW_SOURCE.read_text(encoding="utf-8")
    # The fallback has to be keyed on a navigation, so /api and /ws answers are
    # left alone, and on the exact statuses a proxy returns while the engine is
    # down.
    assert "event.request.mode === 'navigate'" in source
    assert "[502, 503, 504].includes(response.status)" in source


def test_service_worker_copies_are_identical() -> None:
    # web/public/sw.js is the source and ciao/web/static/sw.js is what the
    # packaged wheel actually serves; if they drift, the offline behaviour is
    # only present in the dev build.
    assert SW_SOURCE.read_bytes() == SW_PACKAGED.read_bytes()
