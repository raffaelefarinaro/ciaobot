from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

from ciao import menubar


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_fetch_server_status_ready(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResponse({"phases": [], "overall_ready": True})

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    status = menubar.fetch_server_status(9443)

    assert calls["url"] == "http://localhost:9443/api/startup-status"
    assert status == menubar.ServerStatus(reachable=True, ready=True)


def test_fetch_server_status_starting(monkeypatch) -> None:
    monkeypatch.setattr(
        menubar.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse({"phases": [], "overall_ready": False}),
    )

    assert menubar.fetch_server_status(8443) == menubar.ServerStatus(
        reachable=True, ready=False
    )


def test_fetch_server_status_unreachable(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.fetch_server_status(8443) == menubar.ServerStatus(
        reachable=False, ready=False
    )


def test_status_labels() -> None:
    assert menubar.status_label(menubar.ServerStatus(True, True)) == "Server: running"
    assert menubar.status_label(menubar.ServerStatus(True, False)) == "Server: starting…"
    assert (
        menubar.status_label(menubar.ServerStatus(False, False))
        == "Server: not running"
    )


def test_open_app_command_uses_setup_token(tmp_path: Path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok123\n", encoding="utf-8")

    assert menubar.open_app_command(tmp_path, 9443) == [
        "open",
        "http://localhost:9443/?setup=tok123",
    ]


def test_open_app_command_without_token_falls_back_to_plain_url(tmp_path: Path) -> None:
    assert menubar.open_app_command(tmp_path, 8443) == [
        "open",
        "http://localhost:8443/",
    ]


def test_restart_server_command_targets_launchd_label() -> None:
    assert menubar.restart_server_command(uid=501) == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.ciao.server",
    ]


def test_stop_server_command_boots_out_launchd_label() -> None:
    # bootout (not kill) because the server plist is KeepAlive=true, so a
    # plain kill would be relaunched; bootout takes it out of the domain.
    assert menubar.stop_server_command(uid=501) == [
        "launchctl",
        "bootout",
        "gui/501/com.ciao.server",
    ]


def test_view_logs_command_opens_stderr_log(tmp_path: Path) -> None:
    assert menubar.view_logs_command(tmp_path) == [
        "open",
        str(tmp_path / ".runtime" / "ciao.stderr.log"),
    ]


def test_icon_paths_resolve_to_packaged_faces() -> None:
    # The menu bar uses only the monochrome template icons, shipped in
    # ciao.stock/deploy (never in web/static, which the PWA build empties).
    assert Path(menubar.icon_path("face_template.png")).is_file()
    assert Path(menubar.icon_path("face_scared_template.png")).is_file()


def test_menubar_template_icons_are_packaged() -> None:
    # The status bar uses monochrome template variants of the faces.
    assert Path(menubar.icon_path("face_template.png")).is_file()
    assert Path(menubar.icon_path("face_scared_template.png")).is_file()


def _write_log(workspace: Path, entries: list[dict]) -> None:
    log = workspace / ".runtime" / "notifications.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )


def test_read_notifications_missing_file_returns_empty(tmp_path: Path) -> None:
    assert menubar.read_notifications(tmp_path) == []


def test_read_notifications_newest_first_with_limit(tmp_path: Path) -> None:
    _write_log(
        tmp_path,
        [
            {"ts": float(index), "title": "t", "body": f"msg {index}", "chat_id": ""}
            for index in range(15)
        ],
    )

    entries = menubar.read_notifications(tmp_path, limit=10)

    assert len(entries) == 10
    assert entries[0].body == "msg 14"
    assert entries[-1].body == "msg 5"


def test_read_notifications_skips_corrupt_lines(tmp_path: Path) -> None:
    log = tmp_path / ".runtime" / "notifications.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        'not json\n{"ts": 1.0, "title": "ok", "body": "b", "chat_id": "c"}\n',
        encoding="utf-8",
    )

    entries = menubar.read_notifications(tmp_path)

    assert [entry.title for entry in entries] == ["ok"]


def test_notification_menu_title_truncates() -> None:
    entry = menubar.Notification(ts=1.0, title="Ciaobot", body="x" * 100, chat_id="")

    title = menubar.notification_menu_title(entry, max_length=30)

    assert len(title) == 30
    assert title.endswith("…")


def test_chat_url_deep_links_to_chat(tmp_path: Path) -> None:
    assert menubar.chat_url(tmp_path, 8443, "abc") == "http://localhost:8443/chat/abc"
    assert menubar.chat_url(tmp_path, 8443, "") == "http://localhost:8443/"


def test_notify_command_builds_osascript_invocation() -> None:
    cmd = menubar.notify_command('He said "hi"', "body text")

    assert cmd[:2] == ["osascript", "-e"]
    assert 'display notification "body text"' in cmd[2]
    assert '\\"hi\\"' in cmd[2]


def test_read_open_chats_filters_archived_and_sorts_by_activity(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "chats": {
                    "old": {"title": "Old", "archived": False, "last_activity_at": 1.0},
                    "new": {"title": "New", "archived": False, "last_activity_at": 9.0},
                    "gone": {"title": "Gone", "archived": True, "last_activity_at": 99.0},
                }
            }
        ),
        encoding="utf-8",
    )

    chats = menubar.read_open_chats(tmp_path)

    assert [chat.chat_id for chat in chats] == ["new", "old"]
    assert chats[0].title == "New"


def test_read_open_chats_missing_state_returns_empty(tmp_path: Path) -> None:
    assert menubar.read_open_chats(tmp_path) == []


def test_parse_inet_addresses_excludes_loopback_and_dupes() -> None:
    text = (
        "lo0: flags\n\tinet 127.0.0.1 netmask 0xff000000\n"
        "en0: flags\n\tinet 192.168.1.20 netmask 0xffffff00\n"
        "en5: flags\n\tinet 192.168.1.20 netmask 0xffffff00\n"
        "utun3: flags\n\tinet 100.94.1.5 --> 100.94.1.5 netmask 0xffffffff\n"
    )

    assert menubar.parse_inet_addresses(text) == ["192.168.1.20", "100.94.1.5"]


def test_server_addresses_lists_localhost_bonjour_and_lan() -> None:
    urls = menubar.server_addresses(
        9443,
        ifconfig_text="\tinet 127.0.0.1\n\tinet 10.0.0.7\n",
        local_hostname="raffas-mini",
    )

    assert urls == [
        "http://localhost:9443/",
        "http://raffas-mini.local:9443/",
        "http://10.0.0.7:9443/",
    ]


def test_banners_muted_defaults_false_and_round_trips(tmp_path: Path) -> None:
    assert menubar.read_banners_muted(tmp_path) is False

    menubar.write_banners_muted(tmp_path, True)
    assert menubar.read_banners_muted(tmp_path) is True

    menubar.write_banners_muted(tmp_path, False)
    assert menubar.read_banners_muted(tmp_path) is False


def test_banners_muted_tolerates_corrupt_settings(tmp_path: Path) -> None:
    path = tmp_path / ".runtime" / "menubar_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    assert menubar.read_banners_muted(tmp_path) is False


def test_run_menubar_without_rumps_explains_the_dependency(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setitem(sys.modules, "rumps", None)

    assert menubar.run_menubar(tmp_path, 8443) == 1
    err = capsys.readouterr().err
    assert "rumps" in err
    assert "pip install rumps" in err
