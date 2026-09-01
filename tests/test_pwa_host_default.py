"""Tests for the `pwa_host` default and its `.env` parsing fallback.

Issue #389: the dataclass default said loopback while the `.env` fallback (and
the actual server bind) said all interfaces. Both must stay equal.
"""

from __future__ import annotations

import dataclasses

from ciao.config import CiaoConfig


def test_pwa_host_default_binds_all_interfaces():
    assert CiaoConfig.from_env({}).pwa_host == "0.0.0.0"


def test_pwa_host_dataclass_default_matches_env_fallback():
    # CiaoConfig has many required fields; compare the dataclass default via
    # dataclasses.fields instead of constructing an instance.
    field = next(
        f for f in dataclasses.fields(CiaoConfig) if f.name == "pwa_host"
    )
    assert field.default == CiaoConfig.from_env({}).pwa_host


def test_pwa_host_env_override_is_honoured():
    assert CiaoConfig.from_env({"PWA_HOST": " 127.0.0.1 "}).pwa_host == "127.0.0.1"


def test_pwa_host_empty_env_value_falls_back_to_default():
    assert CiaoConfig.from_env({"PWA_HOST": ""}).pwa_host == "0.0.0.0"