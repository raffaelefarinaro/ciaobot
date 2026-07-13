from __future__ import annotations

import json
import plistlib
import sys
import urllib.error
from pathlib import Path

import pytest

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


def test_fetch_active_chat_ids_parses_list(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float):
        calls["url"] = url
        return _FakeResponse({"active_chat_ids": ["a", "b", "b"]})

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.fetch_active_chat_ids(9443) == {"a", "b"}
    assert calls["url"] == "http://localhost:9443/api/active-chats"


def test_fetch_active_chat_ids_handles_unreachable(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.fetch_active_chat_ids(8443) == set()


def test_fetch_active_chat_ids_ignores_malformed_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        menubar.urllib.request,
        "urlopen",
        lambda url, timeout: _FakeResponse({"active_chat_ids": "nope"}),
    )

    assert menubar.fetch_active_chat_ids(8443) == set()


def test_status_labels() -> None:
    assert menubar.status_label(menubar.ServerStatus(True, True)) == "Server: running"
    assert menubar.status_label(menubar.ServerStatus(True, False)) == "Server: starting…"
    assert (
        menubar.status_label(menubar.ServerStatus(False, False))
        == "Server: not running"
    )


def test_server_recovery_label_matches_reachability() -> None:
    assert menubar.server_recovery_label(menubar.ServerStatus(True, True)) == "Restart Server"
    assert menubar.server_recovery_label(menubar.ServerStatus(True, False)) == "Restart Server"
    assert menubar.server_recovery_label(menubar.ServerStatus(False, False)) == "Start Server"


def test_open_url_uses_setup_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(menubar, "_installed_browser_app_names", lambda: [])
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok123\n", encoding="utf-8")

    url = menubar.open_url(tmp_path, 9443)
    assert menubar.open_command(url) == [
        "open",
        "http://localhost:9443/?setup=tok123",
    ]


def test_open_url_without_token_falls_back_to_plain_url(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(menubar, "_installed_browser_app_names", lambda: [])

    url = menubar.open_url(tmp_path, 8443)
    assert menubar.open_command(url) == [
        "open",
        "http://localhost:8443/",
    ]


def test_open_command_prefers_browser_app_mode(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(menubar, "_installed_browser_app_names", lambda: ["Google Chrome"])

    assert menubar.open_command("http://localhost:8443/") == [
        "open",
        "-a",
        "Google Chrome",
        "--args",
        "--app=http://localhost:8443/",
    ]


def test_window_launch_command_includes_workspace(tmp_path: Path) -> None:
    cmd = menubar._window_launch_command("http://localhost:8443/", tmp_path)
    assert cmd[1:] == ["-m", "ciao.window", "http://localhost:8443/", "--workspace", str(tmp_path)]
    assert menubar._window_launch_command("http://localhost:8443/", None) == [
        sys.executable,
        "-m",
        "ciao.window",
        "http://localhost:8443/",
    ]


def _write_bundle(path: Path, *, bundle_id: str) -> None:
    contents = path / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id})
    )


def test_remove_browser_pwa_duplicates_skips_native_launcher(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(menubar.Path, "home", lambda: tmp_path)
    native = tmp_path / "Applications" / "Ciaobot.app"
    _write_bundle(native, bundle_id="local.ciaobot.app")
    duplicate = tmp_path / "Applications" / "Chrome Apps.localized" / "Ciaobot.app"
    _write_bundle(duplicate, bundle_id="org.chromium.Chromium.app.abc123")

    removed = menubar.remove_browser_pwa_duplicates()

    assert removed == [duplicate.resolve()]
    assert native.is_dir()
    assert not duplicate.exists()


def test_restart_server_command_targets_launchd_label() -> None:
    assert menubar.restart_server_command(uid=501) == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.ciao.server",
    ]


def test_restart_menubar_command_targets_launchd_label() -> None:
    assert menubar.restart_menubar_command(uid=501) == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/com.ciao.menubar",
    ]


def test_parse_launchctl_disabled_reads_label_states() -> None:
    output = """
disabled services = {
    "com.ciao.server" => false
    "com.ciao.menubar" => true
}
"""

    assert menubar.parse_launchctl_disabled(output) == {
        "com.ciao.server": False,
        "com.ciao.menubar": True,
    }


def test_start_at_login_status_is_on_when_launch_agents_are_enabled(tmp_path: Path) -> None:
    for path in menubar.launch_agent_paths(tmp_path).values():
        path.write_text("<plist />", encoding="utf-8")

    status = menubar.start_at_login_status(
        launch_agents_dir=tmp_path,
        disabled_labels={
            menubar.SERVER_LAUNCHD_LABEL: False,
            menubar.MENUBAR_LAUNCHD_LABEL: False,
        },
    )

    assert status.state == "on"
    assert status.available
    assert status.enabled
    assert menubar.start_at_login_menu_label(status) == "Start Ciao at Login: On"


def test_start_at_login_status_is_off_when_either_agent_is_disabled(tmp_path: Path) -> None:
    for path in menubar.launch_agent_paths(tmp_path).values():
        path.write_text("<plist />", encoding="utf-8")

    status = menubar.start_at_login_status(
        launch_agents_dir=tmp_path,
        disabled_labels={menubar.SERVER_LAUNCHD_LABEL: True},
    )

    assert status.state == "off"
    assert status.available
    assert not status.enabled
    assert menubar.start_at_login_menu_label(status) == "Start Ciao at Login: Off"


def test_start_at_login_status_is_missing_without_launch_agent_plists(
    tmp_path: Path,
) -> None:
    status = menubar.start_at_login_status(
        launch_agents_dir=tmp_path,
        disabled_labels={},
    )

    assert status.state == "missing"
    assert not status.available
    assert menubar.start_at_login_menu_label(status) == "Start at Login: not installed"


def test_start_at_login_commands_target_both_launch_agents() -> None:
    assert menubar.start_at_login_commands(True, uid=501) == [
        ["launchctl", "enable", "gui/501/com.ciao.server"],
        ["launchctl", "enable", "gui/501/com.ciao.menubar"],
    ]
    assert menubar.start_at_login_commands(False, uid=501) == [
        ["launchctl", "disable", "gui/501/com.ciao.server"],
        ["launchctl", "disable", "gui/501/com.ciao.menubar"],
    ]


def test_set_start_at_login_enabled_runs_launchctl_for_both_agents(
    tmp_path: Path, monkeypatch
) -> None:
    for path in menubar.launch_agent_paths(tmp_path).values():
        path.write_text("<plist />", encoding="utf-8")

    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], **kwargs):
        calls.append(cmd)
        return Completed()

    monkeypatch.setattr(menubar.subprocess, "run", fake_run)

    ok, error = menubar.set_start_at_login_enabled(
        False,
        launch_agents_dir=tmp_path,
        uid=501,
    )

    assert ok
    assert error == ""
    assert calls == [
        ["launchctl", "disable", "gui/501/com.ciao.server"],
        ["launchctl", "disable", "gui/501/com.ciao.menubar"],
    ]


def test_relaunch_stale_process_kicks_launchd(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        menubar.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd)
    )
    menubar.relaunch_stale_process(uid=501)
    assert calls == [["launchctl", "kickstart", "-k", "gui/501/com.ciao.menubar"]]


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
    assert Path(menubar.icon_path("Ciaobot.icns")).is_file()


def test_menubar_template_icons_are_packaged() -> None:
    # The status bar uses monochrome template variants of the faces.
    assert Path(menubar.icon_path("face_template.png")).is_file()
    assert Path(menubar.icon_path("face_scared_template.png")).is_file()


def test_spin_icon_frames_are_packaged() -> None:
    # The spinning-head animation frames must ship so the icon can spin while
    # a chat is working.
    frames = menubar.spin_icon_paths()
    assert len(frames) == menubar.SPIN_FRAME_COUNT
    assert all(Path(frame).is_file() for frame in frames)


def test_dot_pulse_frames_are_packaged() -> None:
    frames = menubar.dot_pulse_icon_paths()
    assert len(frames) == menubar.DOT_PULSE_FRAME_COUNT
    assert all(Path(frame).is_file() for frame in frames)


def test_chat_menu_title_marks_unread_with_dot() -> None:
    assert menubar.chat_menu_title("Test", unread=True) == "● Test"
    assert menubar.chat_menu_title("Test", unread=False) == "Test"
    assert menubar.chat_menu_title("x" * 80, unread=True, max_length=10).endswith("…")
    assert menubar.chat_menu_title("x" * 80, unread=True, max_length=10).startswith("● ")


def test_chat_menu_title_marks_needs_input_with_question_mark() -> None:
    assert menubar.chat_menu_title("Test", unread=False, needs_input=True) == "? Test"
    assert menubar.chat_menu_title("Test", unread=True, needs_input=True) == "? Test"
    assert menubar.chat_menu_title(
        "Test",
        unread=False,
        needs_input=True,
        working=True,
    ) == "? Test"


def test_chat_menu_title_marks_working_and_takes_precedence() -> None:
    assert menubar.chat_menu_title("Test", unread=False, working=True) == "◌ Test"
    assert menubar.chat_menu_title("Test", unread=True, working=True) == "◌ Test"
    assert menubar.chat_menu_title("Test", unread=True, working=True, working_has_icon=True) == "● Test"
    assert menubar.chat_menu_title("Test", unread=False, working=True, working_has_icon=True) == "Test"


def test_workspace_menu_label_formats_names() -> None:
    assert menubar.workspace_menu_label("personal") == "Personal"
    assert menubar.workspace_menu_label("my-work") == "My Work"
    assert menubar.workspace_menu_label("") == "Workspace"


def test_chat_menu_title_adds_workspace_tag_when_requested() -> None:
    assert menubar.chat_menu_title(
        "Morning briefing",
        unread=False,
        workspace="personal",
        show_workspace=True,
    ) == "Morning briefing [Personal]"
    assert menubar.chat_menu_title(
        "Needs attention",
        unread=True,
        workspace="work",
        show_workspace=True,
    ) == "● Needs attention [Work]"


def test_read_open_chats_resolves_workspace_from_project(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "projects": {
                    "proj-personal": {"name": "General", "workspace": "personal"},
                    "proj-work": {"name": "General", "workspace": "work"},
                },
                "chats": {
                    "personal-chat": {
                        "project_id": "proj-personal",
                        "title": "Personal chat",
                        "archived": False,
                        "last_activity_at": "2026-01-02T10:00:00",
                    },
                    "work-chat": {
                        "project_id": "proj-work",
                        "title": "Work chat",
                        "archived": False,
                        "last_activity_at": "2026-01-01T10:00:00",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    chats = menubar.read_open_chats(tmp_path)

    assert [(chat.chat_id, chat.workspace) for chat in chats] == [
        ("personal-chat", "personal"),
        ("work-chat", "work"),
    ]


def test_menubar_badge_title() -> None:
    assert menubar.menubar_badge_title(0) == ""
    assert menubar.menubar_badge_title(3) == "3"
    assert menubar.menubar_badge_title(100) == "99+"


def test_chat_url_deep_links_to_chat(tmp_path: Path) -> None:
    assert menubar.chat_url(tmp_path, 8443, "abc") == "http://localhost:8443/chat/abc"
    assert menubar.chat_url(tmp_path, 8443, "") == "http://localhost:8443/"


def test_notify_open_chat_hits_local_endpoint(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_urlopen(url: str, timeout: float):
        calls["url"] = url
        calls["timeout"] = timeout
        return _FakeResponse({"ok": True, "chat_id": "abc"})

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.notify_open_chat(8443, "abc") is True
    assert calls["url"] == "http://localhost:8443/api/open-chat/abc"
    assert calls["timeout"] == 2.0


def test_notify_open_chat_returns_false_when_unreachable(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: float):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(menubar.urllib.request, "urlopen", fake_urlopen)

    assert menubar.notify_open_chat(8443, "abc") is False


def test_read_open_chats_filters_archived_and_sorts_by_activity(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "chats": {
                    "old": {
                        "title": "Old",
                        "archived": False,
                        "last_activity_at": "2026-01-01T10:00:00",
                    },
                    "new": {
                        "title": "New",
                        "archived": False,
                        "last_activity_at": "2026-01-02T10:00:00",
                    },
                    "gone": {
                        "title": "Gone",
                        "archived": True,
                        "last_activity_at": "2026-12-31T10:00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    chats = menubar.read_open_chats(tmp_path)

    assert [chat.chat_id for chat in chats] == ["new", "old"]
    assert chats[0].title == "New"


def test_chat_is_unread_matches_pwa_logic() -> None:
    assert menubar.chat_is_unread(
        {"last_activity_at": "2026-01-02T10:00:00", "last_read_at": "2026-01-01T10:00:00"}
    )
    assert not menubar.chat_is_unread(
        {"last_activity_at": "2026-01-01T10:00:00", "last_read_at": "2026-01-02T10:00:00"}
    )
    assert not menubar.chat_is_unread(
        {
            "archived": True,
            "last_activity_at": "2026-01-02T10:00:00",
            "last_read_at": "",
        }
    )


def test_chat_needs_input_matches_pending_question() -> None:
    assert menubar.chat_needs_input(
        {
            "pending_question": json.dumps(
                {"questions": [{"question": "Pick one", "options": [{"label": "A"}]}]}
            ),
        }
    )
    assert not menubar.chat_needs_input({"pending_question": ""})
    assert not menubar.chat_needs_input({"pending_question": "{not json"})
    assert not menubar.chat_needs_input(
        {"pending_question": json.dumps({"questions": []})}
    )
    assert not menubar.chat_needs_input(
        {
            "archived": True,
            "pending_question": json.dumps({"questions": [{"question": "Still blocked?"}]}),
        }
    )


def test_count_attention_chats_includes_read_questions(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "chats": {
                    "read-question": {
                        "title": "Answer me",
                        "last_activity_at": "2026-01-02T10:00:00",
                        "last_read_at": "2026-01-02T11:00:00",
                        "pending_question": json.dumps(
                            {"questions": [{"question": "Which tier?"}]}
                        ),
                    },
                    "unread": {
                        "title": "Unread",
                        "last_activity_at": "2026-01-03T10:00:00",
                        "last_read_at": "",
                    },
                    "idle": {
                        "title": "Idle",
                        "last_activity_at": "2026-01-01T10:00:00",
                        "last_read_at": "2026-01-01T11:00:00",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert menubar.needs_input_chat_ids(tmp_path) == {"read-question"}
    assert menubar.count_attention_chats(tmp_path) == 2


def test_read_unread_chats_filters_read_and_sorts_by_activity(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "chats": {
                    "read": {
                        "title": "Read",
                        "last_activity_at": "2026-01-03T10:00:00",
                        "last_read_at": "2026-01-03T11:00:00",
                    },
                    "unread-old": {
                        "title": "Unread old",
                        "last_activity_at": "2026-01-01T10:00:00",
                        "last_read_at": "2026-01-01T09:00:00",
                    },
                    "unread-new": {
                        "title": "Unread new",
                        "last_activity_at": "2026-01-02T10:00:00",
                        "last_read_at": "",
                    },
                    "archived": {
                        "title": "Archived",
                        "archived": True,
                        "last_activity_at": "2026-12-31T10:00:00",
                        "last_read_at": "",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    unread = menubar.read_unread_chats(tmp_path)

    assert [chat.chat_id for chat in unread] == ["unread-new", "unread-old"]
    assert [chat.title for chat in unread] == ["Unread new", "Unread old"]


def test_count_unread_chats_is_not_limited_to_menu_size(tmp_path: Path) -> None:
    state = tmp_path / ".runtime" / "web_projects.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "chats": {
                    f"chat-{index}": {
                        "title": f"Unread {index}",
                        "last_activity_at": f"2026-01-01T10:{index:02d}:00",
                        "last_read_at": "",
                    }
                    for index in range(14)
                }
            }
        ),
        encoding="utf-8",
    )

    assert len(menubar.read_unread_chats(tmp_path)) == 10
    assert menubar.count_unread_chats(tmp_path) == 14


def test_read_open_chats_missing_state_returns_empty(tmp_path: Path) -> None:
    assert menubar.read_open_chats(tmp_path) == []


def test_notification_log_tail_only_returns_entries_after_startup(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    log = runtime / "notifications.jsonl"
    old_entry = {"ts": 1.0, "title": "Ciaobot", "body": "Old", "chat_id": "old"}
    log.write_text(json.dumps(old_entry) + "\n", encoding="utf-8")

    tail = menubar.NotificationLogTail.at_end(tmp_path)
    assert tail.read_new(tmp_path) == []

    new_entry = {"ts": 2.0, "title": "Ciaobot", "body": "New", "chat_id": "new"}
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(new_entry) + "\n")
        handle.write("{partial")

    assert tail.read_new(tmp_path) == [new_entry]
    assert tail.read_new(tmp_path) == []


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


def test_update_menu_label_includes_version() -> None:
    assert menubar.update_menu_label("1.2.3") == "Update to 1.2.3"
    assert menubar.update_menu_label("  ") == "Update available"


def test_package_update_fingerprint_tracks_availability() -> None:
    assert menubar.package_update_fingerprint(
        {"update_available": True, "latest_version": "2.0.0"}
    ) == (True, "2.0.0")
    assert menubar.package_update_fingerprint(
        {"update_available": False, "latest_version": "1.0.0"}
    ) == (False, "1.0.0")


def test_run_menubar_without_rumps_explains_the_dependency(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setitem(sys.modules, "rumps", None)

    assert menubar.run_menubar(tmp_path, 8443) == 1
    err = capsys.readouterr().err
    assert "rumps" in err
    assert "pip install rumps" in err


def test_github_repo_url_uses_default_repo(monkeypatch) -> None:
    monkeypatch.delenv("CIAO_GITHUB_REPO", raising=False)
    assert menubar.github_repo_url() == "https://github.com/raffaelefarinaro/ciaobot"
    assert menubar.github_new_issue_url() == (
        "https://github.com/raffaelefarinaro/ciaobot/issues/new"
    )


def test_github_repo_url_respects_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CIAO_GITHUB_REPO", "someone/fork")
    assert menubar.github_repo_url() == "https://github.com/someone/fork"


def test_open_url_command_wraps_open() -> None:
    assert menubar.open_url_command("https://example.com") == ["open", "https://example.com"]


def test_keep_timer_running_while_menu_open_tolerates_any_object() -> None:
    # No _nstimer attribute (and no Foundation off-macOS): must be a no-op.
    menubar._keep_timer_running_while_menu_open(object())


def test_keep_timer_running_while_menu_open_registers_common_modes() -> None:
    rumps = pytest.importorskip("rumps")
    timer = rumps.Timer(lambda _timer: None, 0.5)
    timer.start()
    try:
        menubar._keep_timer_running_while_menu_open(timer)
    finally:
        timer.stop()
