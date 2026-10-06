"""Tests for the `pwa_host` default and where the bind address comes from.

Issue #389: the dataclass default said loopback while the fallback (and the
actual server bind) said all interfaces. Both must stay equal. The bind address
is a Setting now (`app_settings.json`), not the retired `PWA_HOST` variable.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from ciao.app_settings import DEFAULT_PWA_HOST
from ciao.config import CiaoConfig


def _workspace(tmp_path: Path, settings: dict | None = None) -> dict[str, str]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (runtime / "app_settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return {"CIAO_WORKSPACE": str(tmp_path), "PWA_AUTH_TOKEN": "t"}


def test_pwa_host_default_binds_all_interfaces():
    assert CiaoConfig.from_env({}).pwa_host == "0.0.0.0" == DEFAULT_PWA_HOST


def test_pwa_host_dataclass_default_matches_the_settings_default():
    # CiaoConfig has many required fields; compare the dataclass default via
    # dataclasses.fields instead of constructing an instance.
    field = next(
        f for f in dataclasses.fields(CiaoConfig) if f.name == "pwa_host"
    )
    assert field.default == DEFAULT_PWA_HOST == CiaoConfig.from_env({}).pwa_host


def test_pwa_host_comes_from_settings(tmp_path: Path):
    env = _workspace(tmp_path, {"pwa_host": " 127.0.0.1 "})
    assert CiaoConfig.from_env(env).pwa_host == "127.0.0.1"


def test_the_retired_env_variable_is_not_read_but_kept_for_the_import(tmp_path: Path):
    env = {**_workspace(tmp_path), "PWA_HOST": "127.0.0.1"}
    config = CiaoConfig.from_env(env)
    assert config.pwa_host == "0.0.0.0"
    assert config.legacy_env_settings == {"PWA_HOST": "127.0.0.1"}


def test_an_invalid_stored_host_falls_back_to_the_default(tmp_path: Path):
    env = _workspace(tmp_path, {"pwa_host": "not a host!"})
    assert CiaoConfig.from_env(env).pwa_host == "0.0.0.0"
