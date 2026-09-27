"""The Tauri shell must not grant remote content the update window.

`desktop/src-tauri/capabilities/main.json` is the whole boundary: it names the
windows remote pages may live in and the permissions they hold. With node mode
gone there is no "remote" window left to fence, so this asserts the fence is
actually gone — a stray `"remote"` entry or a `trigger-app-update` permission
would hand any embedded page the updater.
"""

from __future__ import annotations

import json
from pathlib import Path


def test_tauri_capability_does_not_grant_remote_content() -> None:
    root = Path(__file__).parents[1]
    capability = json.loads(
        (root / "desktop/src-tauri/capabilities/main.json").read_text(encoding="utf-8")
    )
    assert "remote" not in capability
    assert capability["windows"] == ["update"]
    assert "trigger-app-update" not in capability["permissions"]
    assert not (root / "desktop/src-tauri/permissions/trigger-app-update.toml").exists()
