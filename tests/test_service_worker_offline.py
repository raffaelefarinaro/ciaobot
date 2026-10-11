"""The packaged copy of the service worker matches the PWA source.

Runtime coverage for offline navigation lives in
``web/src/lib/serviceWorkerOffline.test.ts``. This check pins the packaged
artifact to that tested PWA source.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SW_SOURCE = REPO_ROOT / "web" / "public" / "sw.js"
SW_PACKAGED = REPO_ROOT / "ciao" / "web" / "static" / "sw.js"


def test_service_worker_copies_are_identical() -> None:
    # web/public/sw.js is the source and ciao/web/static/sw.js is what the
    # packaged wheel actually serves; if they drift, the offline behaviour is
    # only present in the dev build.
    assert SW_SOURCE.read_bytes() == SW_PACKAGED.read_bytes()
