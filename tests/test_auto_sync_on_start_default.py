"""Tests for the `auto_sync_on_start` default and its `.env` parsing fallback.

Issue #440: the dataclass default was flipped to `False` for secure-by-default,
but the `.env` fallback still treated an unset `CIAO_AUTO_SYNC_ON_START` as
enabled, so a fresh install ran `git pull --rebase` on boot. The two defaults
must stay equal.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ciao.config import CiaoConfig


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """A `from_env` source rooted in `tmp_path`.

    Without `CIAO_WORKSPACE` these configs take the bootstrap branch, and
    `_read_or_create_secret` then mints a token under the *real*
    `~/.ciao/bootstrap/.runtime/` — `conftest.py` isolates `CIAO_MEMORY_DIR`
    and `CIAO_LAUNCH_AGENTS_DIR`, but not `HOME`. Pinning the bootstrap
    workspace keeps the developer's own `~/.ciao` untouched.
    """
    return {"CIAO_BOOTSTRAP_WORKSPACE": str(tmp_path), **extra}


def test_auto_sync_on_start_default_is_disabled(tmp_path: Path):
    assert CiaoConfig.from_env(_env(tmp_path)).auto_sync_on_start is False


def test_auto_sync_on_start_dataclass_default_matches_env_fallback(tmp_path: Path):
    # CiaoConfig has many required fields; compare the dataclass default via
    # dataclasses.fields instead of constructing an instance.
    field = next(
        f for f in dataclasses.fields(CiaoConfig) if f.name == "auto_sync_on_start"
    )
    assert field.default == CiaoConfig.from_env(_env(tmp_path)).auto_sync_on_start


def test_auto_sync_on_start_env_opt_in_is_honoured(tmp_path: Path):
    for value in ("true", "TRUE", " true ", "1", "yes", "y", "on"):
        assert CiaoConfig.from_env(
            _env(tmp_path, CIAO_AUTO_SYNC_ON_START=value)
        ).auto_sync_on_start


def test_auto_sync_on_start_falsey_values_disable(tmp_path: Path):
    for value in ("0", "false", "no", "off", "FALSE", "Off"):
        assert not CiaoConfig.from_env(
            _env(tmp_path, CIAO_AUTO_SYNC_ON_START=value)
        ).auto_sync_on_start


def test_auto_sync_on_start_invalid_or_blank_values_fail_closed(tmp_path: Path):
    # An unset, blank, padded-false, or misspelled value must not enable the
    # boot-time `git pull --rebase`. Only an explicit affirmative opts in.
    # The blank cases are the regression: a *set but empty* variable
    # (`CIAO_AUTO_SYNC_ON_START=` in `.env`) once slipped through a
    # `not in {"0", "false", ...}` denylist and enabled the boot-time pull.
    for value in ("", "   ", "false ", " false", "\tfalse\n", "flase", "maybe"):
        assert not CiaoConfig.from_env(
            _env(tmp_path, CIAO_AUTO_SYNC_ON_START=value)
        ).auto_sync_on_start


def test_auto_sync_on_start_unset_variable_stays_disabled(tmp_path: Path):
    """The *unset* case, distinct from the set-but-blank one above."""
    assert CiaoConfig.from_env(_env(tmp_path)).auto_sync_on_start is False
