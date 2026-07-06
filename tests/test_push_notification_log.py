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


def test_notification_log_is_trimmed(tmp_path: Path) -> None:
    manager = PushManager(tmp_path)

    for index in range(NOTIFICATION_LOG_MAX * 2 + 5):
        manager.send({"title": "t", "body": f"msg {index}", "chat_id": ""})

    entries = _read_log(tmp_path)
    assert len(entries) <= NOTIFICATION_LOG_MAX * 2
    assert entries[-1]["body"] == f"msg {NOTIFICATION_LOG_MAX * 2 + 4}"
