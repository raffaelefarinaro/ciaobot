from __future__ import annotations

import json
from pathlib import Path

from ciao.web.push import NOTIFICATION_LOG_MAX, PushManager


def _read_log(runtime: Path) -> list[dict]:
    text = (runtime / "notifications.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_send_logs_notification_even_without_subscriptions(tmp_path: Path) -> None:
    manager = PushManager(tmp_path)

    manager.send({"title": "Ciaobot", "body": "Turn finished", "chat_id": "c1"})

    entries = _read_log(tmp_path)
    assert len(entries) == 1
    assert entries[0]["title"] == "Ciaobot"
    assert entries[0]["body"] == "Turn finished"
    assert entries[0]["chat_id"] == "c1"
    assert entries[0]["ts"] > 0


def test_send_with_empty_subject_skips_webpush_but_still_logs(tmp_path: Path) -> None:
    """No CIAO_PUSH_CONTACT: Web Push delivery is skipped without errors and
    the local notification log (menu bar companion) still gets the entry."""
    manager = PushManager(tmp_path, subject="")
    manager.add({"endpoint": "https://push.example/sub-1"})

    manager.send({"title": "Ciaobot", "body": "Turn finished", "chat_id": "c1"})

    entries = _read_log(tmp_path)
    assert len(entries) == 1
    assert entries[0]["body"] == "Turn finished"
    # the subscription is kept (not pruned) for when a contact is configured
    assert manager.count() == 1


def test_local_notification_always_uses_native_banner(tmp_path: Path, monkeypatch) -> None:
    """The local machine's notification always goes through the menu bar's
    native banner, never Web Push — the push service's 2xx cannot confirm the
    browser displayed it, so relying on it silently loses notifications."""
    import pywebpush

    endpoints: list[str] = []
    monkeypatch.setattr(
        pywebpush,
        "webpush",
        lambda **kwargs: endpoints.append(kwargs["subscription_info"]["endpoint"]),
    )
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/local"}, local=True)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert len(_read_log(tmp_path)) == 1  # native banner queued for the menu bar
    assert endpoints == []  # local subscription is NOT pushed to


def test_remote_subscription_is_pushed_and_local_still_logs(tmp_path: Path, monkeypatch) -> None:
    """A subscription on another device (e.g. a phone) IS delivered via Web
    Push, while this Mac still gets its native banner via the log."""
    import pywebpush

    endpoints: list[str] = []
    monkeypatch.setattr(
        pywebpush,
        "webpush",
        lambda **kwargs: endpoints.append(kwargs["subscription_info"]["endpoint"]),
    )
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/phone"}, local=False)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert len(_read_log(tmp_path)) == 1  # local machine still gets a banner
    assert endpoints == ["https://push.example/phone"]  # remote device pushed


def test_send_test_pushes_only_to_local_subscriptions(tmp_path: Path, monkeypatch) -> None:
    """``send_test`` verifies the local browser displays a push, so it targets
    local subscriptions (and reports how many the push service accepted)."""
    import pywebpush

    endpoints: list[str] = []
    monkeypatch.setattr(
        pywebpush,
        "webpush",
        lambda **kwargs: endpoints.append(kwargs["subscription_info"]["endpoint"]),
    )
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/local"}, local=True)
    manager.add({"endpoint": "https://push.example/phone"}, local=False)

    result = manager.send_test({"title": "t", "body": "hi", "chat_id": ""})

    assert endpoints == ["https://push.example/local"]  # only the local sub
    assert result == {"local_subscriptions": 1, "accepted": 1}


def test_remote_push_failure_does_not_lose_local_notification(tmp_path: Path, monkeypatch) -> None:
    """A remote push that fails must not affect the local native banner, and a
    non-404 failure must not prune the subscription."""
    import pywebpush

    def boom(**kwargs):
        raise RuntimeError("transient push failure")

    monkeypatch.setattr(pywebpush, "webpush", boom)
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/phone"}, local=False)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert len(_read_log(tmp_path)) == 1  # local banner unaffected
    assert manager.count() == 1  # non-404 failure does not prune the sub


def test_notification_log_is_trimmed(tmp_path: Path) -> None:
    manager = PushManager(tmp_path)

    for index in range(NOTIFICATION_LOG_MAX * 2 + 5):
        manager.send({"title": "t", "body": f"msg {index}", "chat_id": ""})

    entries = _read_log(tmp_path)
    assert len(entries) <= NOTIFICATION_LOG_MAX * 2
    assert entries[-1]["body"] == f"msg {NOTIFICATION_LOG_MAX * 2 + 4}"
