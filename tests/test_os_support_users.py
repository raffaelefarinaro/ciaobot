"""ciao.os_support.users.user_key: one stable key per local account."""

from __future__ import annotations

import os
import re

import pytest

from ciao import file_kernel as fk
from ciao.os_support.users import user_key


def test_the_key_is_stable_and_safe_in_a_file_name() -> None:
    key = user_key()
    assert key == user_key()
    assert re.fullmatch(r"[A-Za-z0-9-]+", key), key


def test_the_lock_directory_is_keyed_by_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIAO_QUEUE_LOCK_DIR", raising=False)
    lock = fk.lock_path_for("/vault/queue.md")
    assert lock.parent.name == f"ciao-queue-locks-{user_key()}"


def test_posix_keys_by_the_same_uid_as_before() -> None:
    assert user_key() == str(os.getuid())
