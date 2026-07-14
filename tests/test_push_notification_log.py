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


def test_local_delivery_suppresses_native_fallback(tmp_path: Path, monkeypatch) -> None:
    """A successful push to a *local* subscription means this machine already
    got the banner via the PWA, so nothing is logged for the menu bar."""
    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: None)  # succeeds
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/local"}, local=True)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert not (tmp_path / "notifications.jsonl").exists()  # no native fallback


def test_remote_only_subscription_still_logs_for_local_menu_bar(tmp_path: Path, monkeypatch) -> None:
    """A subscription on another device (e.g. a phone) must NOT stop this Mac's
    native banner — the log entry is still written."""
    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: None)  # succeeds
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/phone"}, local=False)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert len(_read_log(tmp_path)) == 1  # local machine still gets a fallback


def test_failed_local_push_falls_back_to_log(tmp_path: Path, monkeypatch) -> None:
    """A local subscription that exists but whose delivery fails must not
    suppress the native fallback — otherwise the notification is lost."""
    import pywebpush

    def boom(**kwargs):
        raise RuntimeError("transient push failure")

    monkeypatch.setattr(pywebpush, "webpush", boom)
    manager = PushManager(tmp_path, subject="mailto:ciaobot@localhost")
    manager.add({"endpoint": "https://push.example/local"}, local=True)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert len(_read_log(tmp_path)) == 1  # failed delivery → native fallback
    assert manager.count() == 1  # non-404 failure does not prune the sub


def test_notification_log_is_trimmed(tmp_path: Path) -> None:
    manager = PushManager(tmp_path)

    for index in range(NOTIFICATION_LOG_MAX * 2 + 5):
        manager.send({"title": "t", "body": f"msg {index}", "chat_id": ""})

    entries = _read_log(tmp_path)
    assert len(entries) <= NOTIFICATION_LOG_MAX * 2
    assert entries[-1]["body"] == f"msg {NOTIFICATION_LOG_MAX * 2 + 4}"
