"""A dev deploy rebuilds the engine and the PWA, and nothing else.

`admin_deploy` used to add a desktop-shell stage that rebuilt and swapped the
macOS app bundle. That tree is gone (#656) and the release is the engine, so
the stage went with it: what a deploy reports now is the same on every
platform. These tests keep the deploy path itself honest — a Linux dev deploy
runs the snapshot/git/pip/npm/restart choreography to completion and offers no
step that could shell out to a build of something that no longer exists.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ciao.web import routes_api
from ciao.web.routes_api import admin_deploy


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
    return repo


def _completed() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["ok"], returncode=0, stdout="ok", stderr="")


async def _deploy(tmp_path, monkeypatch: pytest.MonkeyPatch, platform: str):
    monkeypatch.setattr(sys, "platform", platform)
    config = SimpleNamespace(
        dev_mode=True,
        workspace_root=tmp_path / "ws",
        app_repo=str(_repo(tmp_path)),
    )

    async def ok_push(*args, **kwargs):
        return (True, "pushed")

    async def ok_pull(*args, **kwargs):
        return (0, "Already up to date.")

    monkeypatch.setattr(routes_api, "_commit_and_push", ok_push)
    monkeypatch.setattr(routes_api, "_git_pull_with_retry", ok_pull)
    monkeypatch.setattr(routes_api, "_run_root_npm_install", lambda root: _completed())
    monkeypatch.setattr(routes_api, "run_step", lambda *a, **k: _completed())

    async def fake_json():
        return {}

    calls: list = []
    request = SimpleNamespace(
        json=fake_json,
        app=SimpleNamespace(state=SimpleNamespace(
            config=config, local_session_manager=None, request_restart=calls.append,
        )),
    )
    response = await admin_deploy(request)
    # The handler spawns a 2s delayed restart; cancel it — the restart
    # lifecycle itself is covered elsewhere.
    current = asyncio.current_task()
    for task in [t for t in asyncio.all_tasks() if t is not current]:
        task.cancel()
    return json.loads(response.body)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
async def test_dev_deploy_reports_the_same_steps_on_every_platform(
    tmp_path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    payload = await _deploy(tmp_path, monkeypatch, platform)

    assert payload["ok"] is True
    assert [step["step"] for step in payload["steps"]] == [
        "locate checkout",
        "snapshot",
        "git pull",
        "pip install",
        "npm install (root)",
        "npm build",
        "restart",
    ]
