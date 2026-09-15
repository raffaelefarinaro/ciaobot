"""Tests for the `auto_sync_on_start` default and its `.env` parsing fallback.

Issue #440: the dataclass default was flipped to `False` for secure-by-default,
but the `.env` fallback still treated an unset `CIAO_AUTO_SYNC_ON_START` as
enabled, so a fresh install ran `git pull --rebase` on boot. The two defaults
must stay equal.
"""

from __future__ import annotations

import dataclasses

from ciao.config import CiaoConfig


def test_auto_sync_on_start_default_is_disabled():
    assert CiaoConfig.from_env({}).auto_sync_on_start is False


def test_auto_sync_on_start_dataclass_default_matches_env_fallback():
    # CiaoConfig has many required fields; compare the dataclass default via
    # dataclasses.fields instead of constructing an instance.
    field = next(
        f for f in dataclasses.fields(CiaoConfig) if f.name == "auto_sync_on_start"
    )
    assert field.default == CiaoConfig.from_env({}).auto_sync_on_start


def test_auto_sync_on_start_env_opt_in_is_honoured():
    assert CiaoConfig.from_env({"CIAO_AUTO_SYNC_ON_START": "true"}).auto_sync_on_start


def test_auto_sync_on_start_falsey_values_disable():
    for value in ("0", "false", "no", "off", "FALSE", "Off"):
        assert not CiaoConfig.from_env(
            {"CIAO_AUTO_SYNC_ON_START": value}
        ).auto_sync_on_start
