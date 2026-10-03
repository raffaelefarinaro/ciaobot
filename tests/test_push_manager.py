from __future__ import annotations

from pathlib import Path

import pywebpush

from ciao.web.push import PushManager

SUBJECT = "mailto:ciaobot@users.noreply.github.com"


def _pushed(monkeypatch) -> list[str]:
    endpoints: list[str] = []
    monkeypatch.setattr(
        pywebpush,
        "webpush",
        lambda **kwargs: endpoints.append(kwargs["subscription_info"]["endpoint"]),
    )
    return endpoints


def _manager(tmp_path: Path, subject: str = SUBJECT) -> PushManager:
    manager = PushManager(tmp_path, subject=subject)
    manager.add({"endpoint": "https://push.example/mac"})
    manager.add({"endpoint": "https://push.example/phone"})
    return manager


def test_send_pushes_to_every_subscription(tmp_path: Path, monkeypatch) -> None:
    endpoints = _pushed(monkeypatch)

    _manager(tmp_path).send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert endpoints == ["https://push.example/mac", "https://push.example/phone"]


def test_send_with_empty_subject_skips_webpush_and_keeps_subscriptions(
    tmp_path: Path, monkeypatch
) -> None:
    endpoints = _pushed(monkeypatch)
    manager = _manager(tmp_path, subject="")

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert endpoints == []
    assert manager.count() == 2


def test_clear_chat_pushes_to_every_subscription(tmp_path: Path, monkeypatch) -> None:
    endpoints = _pushed(monkeypatch)

    _manager(tmp_path).clear_chat("c1")

    assert endpoints == ["https://push.example/mac", "https://push.example/phone"]


def test_transient_push_failure_does_not_prune_subscriptions(
    tmp_path: Path, monkeypatch
) -> None:
    def boom(**kwargs):
        raise RuntimeError("transient push failure")

    monkeypatch.setattr(pywebpush, "webpush", boom)
    manager = _manager(tmp_path)

    manager.send({"title": "t", "body": "hi", "chat_id": "c1"})

    assert manager.count() == 2
