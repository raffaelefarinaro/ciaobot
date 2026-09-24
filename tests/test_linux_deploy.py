"""Linux dev deploys must not attempt the macOS desktop rebuild.

`admin_deploy` gated its desktop-shell stage on dev mode alone, so a Linux
host with `CIAO_DEV_MODE=true` ran `needs_rebuild`/`build_and_stage` — a
Tauri/macOS build that fails on Linux after git/pip/npm had already mutated
the install, aborting before the restart. The stage is macOS-only now.
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
    monkeypatch.setattr(routes_api.desktop_build, "run_step", lambda *a, **k: _completed())

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


async def test_linux_dev_deploy_skips_desktop_shell(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_desktop(*args, **kwargs):
        raise AssertionError("desktop rebuild must not run on Linux")

    monkeypatch.setattr(routes_api.desktop_build, "needs_rebuild", no_desktop)
    monkeypatch.setattr(routes_api.desktop_build, "build_and_stage", no_desktop)

    payload = await _deploy(tmp_path, monkeypatch, "linux")

    assert payload["ok"] is True
    steps = {step["step"]: step for step in payload["steps"]}
    assert steps["desktop app"]["ok"] is True
    assert steps["desktop app"]["output"].startswith("skipped:")


async def test_darwin_dev_deploy_still_consults_desktop_shell(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []

    def fake_needs_rebuild(*args, **kwargs):
        seen.append("needs_rebuild")
        return (False, "fresh")

    monkeypatch.setattr(routes_api.desktop_build, "needs_rebuild", fake_needs_rebuild)

    payload = await _deploy(tmp_path, monkeypatch, "darwin")

    assert payload["ok"] is True
    assert seen == ["needs_rebuild"]
