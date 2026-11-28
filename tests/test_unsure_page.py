"""The Unsure page: the list parser and `/api/memory/unsure`.

`<vault>/Workspace/Unsure.md` is the list a care run or memory pass writes when
it is not sure enough to write something down. The page reads it parsed and
ticks, unticks or removes one line, named by a hash of its exact text.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ciao import unsure_list as ul
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.file_kernel import queue_lock
from ciao.web.app import create_app
from tests.session import signed_in

PLAIN = (
    "- [ ] Should I remember that Mo now works at Acme? He said he was leaving. "
    "— unsure because two notes disagree; source: People/Mo.md; 2026-11-23"
)
DROP = '- [ ] drop memory: "Prefers terse replies" — unsure because it may be stale; source: care run; 2026-11-20'
OBSERVATION = (
    "- [x] observation: You like short status updates. — seen in: c1, c2 "
    "— unsure because seen in two chats; source: c2; 2026-11-21"
)
RESTORE = '- [ ] restore to profile: "Lives in Zurich" — removed because no typed turn said it; source: pass-1; 2026-11-22'
REPO = "- [ ] repo ~/repos/app: Run the tests with uv. — unsure because not confirmed; source: chat-9; 2026-11-22"
RULE = "- [ ] rule flip memory/stale-days: 30 -> 45 — unsure because the operator has not approved the change; source: care run; 2026-11-22"
CATEGORY = "- [ ] category: Add a category for recipes? — unsure because none fits; source: chat-3; 2026-11-24"
LIST = "\n".join([
    "# Unsure",
    "",
    "Some prose the person wrote.",
    PLAIN,
    DROP,
    OBSERVATION,
    "```",
    "- [ ] inside a fence, not an item",
    "```",
    "- a plain bullet someone typed",
    RESTORE,
    REPO,
    RULE,
    CATEGORY,
    "",
])


def test_parser_reads_kinds_titles_reasons_sources_and_dates() -> None:
    items = ul.parse_list(LIST)
    assert [i["kind"] for i in items] == ["", "drop", "observation", "", "restore", "repo", "rule", "category"]
    plain = items[0]
    assert plain["title"] == "Should I remember that Mo now works at Acme?"
    assert plain["text"].endswith("He said he was leaving.")
    assert (plain["reason"], plain["source"], plain["date"]) == ("two notes disagree", "People/Mo.md", "2026-11-23")
    assert plain["checked"] is False and plain["malformed"] is False
    assert plain["id"] == ul.line_id(PLAIN)
    assert items[1]["qualifier"] == "memory"
    # A machine-style line with no sentence keeps its whole first clause as the title.
    assert items[1]["title"] == '"Prefers terse replies"'
    assert items[2]["checked"] is True
    assert items[2]["title"] == "You like short status updates."
    assert items[4]["reason"] == "no typed turn said it"
    assert items[5]["qualifier"] == "~/repos/app"
    assert items[6]["qualifier"] == "memory/stale-days"


def test_parser_keeps_a_reason_that_holds_semicolons_and_reads_plural_sources() -> None:
    line = (
        "- [ ] Did the summary change the decision? — unsure because the note records one default; "
        "the later review reports no new decision; sources: chat-a and chat-b; 2026-11-27"
    )
    item = ul.parse_item(line)
    assert item["reason"] == "the note records one default; the later review reports no new decision"
    assert item["source"] == "chat-a and chat-b"
    assert item["date"] == "2026-11-27"


def test_parser_shows_malformed_lines_and_skips_fenced_code() -> None:
    items = ul.parse_list(LIST)
    assert not any("inside a fence" in i["raw"] for i in items)
    bullet = items[3]
    assert bullet["malformed"] is True
    assert bullet["title"] == "a plain bullet someone typed"
    assert bullet["raw"] == "- a plain bullet someone typed"
    no_tail = ul.parse_item("- [ ] Just a sentence with no tail")
    assert no_tail["title"] == "Just a sentence with no tail"
    assert no_tail["reason"] == "" and no_tail["malformed"] is False


def _write(vault: Path, text: str) -> Path:
    path = vault / "Workspace" / "Unsure.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_tick_untick_and_remove_change_only_that_line(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    body = ul.change_line(tmp_path, "tick", ul.line_id(PLAIN))
    assert path.read_text(encoding="utf-8") == LIST.replace(PLAIN, PLAIN.replace("- [ ]", "- [x]", 1))
    assert body["counts"] == {"waiting": 6, "ticked": 2}
    ticked = PLAIN.replace("- [ ]", "- [x]", 1)
    ul.change_line(tmp_path, "untick", ul.line_id(ticked))
    assert path.read_text(encoding="utf-8") == LIST
    body = ul.change_line(tmp_path, "remove", ul.line_id(DROP))
    assert path.read_text(encoding="utf-8") == LIST.replace(DROP + "\n", "")
    assert all(i["raw"] != DROP for i in body["items"])


def test_a_changed_line_is_a_conflict_and_nothing_is_written(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    stale = ul.line_id(PLAIN + " edited")
    with pytest.raises(ul.UnsureError) as caught:
        ul.change_line(tmp_path, "tick", stale)
    assert (caught.value.status, caught.value.code) == (409, "line_changed")
    assert path.read_text(encoding="utf-8") == LIST


def test_a_plain_bullet_cannot_be_ticked_but_can_be_removed(tmp_path: Path) -> None:
    _write(tmp_path, LIST)
    bullet = "- a plain bullet someone typed"
    with pytest.raises(ul.UnsureError) as caught:
        ul.change_line(tmp_path, "tick", ul.line_id(bullet))
    assert caught.value.status == 400
    ul.change_line(tmp_path, "remove", ul.line_id(bullet))
    assert bullet not in (tmp_path / "Workspace" / "Unsure.md").read_text(encoding="utf-8")


def test_a_failed_write_leaves_the_file_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, LIST)

    def boom(target: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(ul, "write_queue_atomically", boom)
    with pytest.raises(OSError):
        ul.change_line(tmp_path, "remove", ul.line_id(PLAIN))
    assert path.read_text(encoding="utf-8") == LIST


def test_a_change_waits_for_an_append_holding_the_lock_and_keeps_its_line(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    done = threading.Event()
    with queue_lock(path):
        worker = threading.Thread(target=lambda: (ul.change_line(tmp_path, "tick", ul.line_id(PLAIN)), done.set()))
        worker.start()
        time.sleep(0.2)
        # The page's write is waiting on the lock this append holds.
        assert not done.is_set()
        assert ul.append_unsure(tmp_path, [ul.UnsureItem("A new item.", reason="new")], today="2026-11-27")
    worker.join(5)
    text = path.read_text(encoding="utf-8")
    assert "- [x] Should I remember" in text
    assert "A new item. — unsure because new" in text


def test_links_and_oversized_lists_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(PLAIN + "\n", encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    os.symlink(outside, vault / "Workspace" / "Unsure.md")
    for call in (lambda: ul.read_list(vault), lambda: ul.change_line(vault, "remove", ul.line_id(PLAIN))):
        with pytest.raises(ul.UnsureError) as caught:
            call()
        assert (caught.value.status, caught.value.code) == (422, "not_plain_file")
    assert outside.read_text(encoding="utf-8") == PLAIN + "\n"

    linked = tmp_path / "linked"
    linked.mkdir()
    os.symlink(tmp_path / "vault" / "Workspace", linked / "Workspace")
    with pytest.raises(ul.UnsureError):
        ul.read_list(linked)

    big = tmp_path / "big"
    _write(big, "# Unsure\n" + (PLAIN + "\n") * (ul.MAX_FILE_BYTES // len(PLAIN) + 2))
    with pytest.raises(ul.UnsureError) as caught:
        ul.read_list(big)
    assert caught.value.status == 413


def test_no_list_is_found_false(tmp_path: Path) -> None:
    assert ul.read_list(tmp_path) == {"found": False, "items": [], "counts": {"waiting": 0, "ticked": 0}}


@pytest.fixture
def setup(tmp_path: Path) -> SimpleNamespace:
    cfg = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )
    app = create_app(cfg)
    return SimpleNamespace(
        app=app,
        client=signed_in(TestClient(app)),
        vault=tmp_path / "memory-vault" / "personal",
        root=tmp_path,
    )


def test_route_reads_the_named_workspace_list(setup: SimpleNamespace) -> None:
    _write(setup.vault, LIST)
    _write(setup.root / "memory-vault" / "work", "# Unsure\n" + CATEGORY + "\n")
    body = setup.client.get("/api/memory/unsure?workspace=personal").json()
    assert body["found"] is True
    assert body["workspace"] == "personal"
    assert body["path"] == "memory-vault/personal/Workspace/Unsure.md"
    assert body["counts"] == {"waiting": 7, "ticked": 1}
    assert body["items"][0]["title"] == "Should I remember that Mo now works at Acme?"
    work = setup.client.get("/api/memory/unsure?workspace=work").json()
    assert [i["kind"] for i in work["items"]] == ["category"]


def test_route_requires_a_session_and_a_known_workspace(setup: SimpleNamespace) -> None:
    anonymous = TestClient(setup.app)
    assert anonymous.get("/api/memory/unsure?workspace=personal").status_code == 401
    assert anonymous.post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": "0" * 16}).status_code in (401, 403)
    assert setup.client.get("/api/memory/unsure").status_code == 400
    assert setup.client.get("/api/memory/unsure?workspace=nope").status_code == 400
    missing = setup.client.get("/api/memory/unsure?workspace=personal").json()
    assert missing["found"] is False and missing["items"] == []


def test_route_changes_one_line_and_answers_the_list(setup: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    path = _write(setup.vault, LIST)
    caplog.set_level(logging.DEBUG)
    resp = setup.client.post("/api/memory/unsure", json={"workspace": "personal", "op": "tick", "id": ul.line_id(PLAIN)})
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"waiting": 6, "ticked": 2}
    assert "- [x] Should I remember" in path.read_text(encoding="utf-8")
    stale = setup.client.post("/api/memory/unsure", json={"workspace": "personal", "op": "tick", "id": ul.line_id(PLAIN)})
    assert stale.status_code == 409
    assert stale.json() == {"error": "the list changed; reload it", "code": "line_changed"}
    assert "Mo now works" not in caplog.text


def test_route_takes_no_path_and_rejects_bad_bodies(setup: SimpleNamespace) -> None:
    _write(setup.vault, LIST)
    post = setup.client.post
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "erase", "id": ul.line_id(PLAIN)}).status_code == 400
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "tick", "id": "../etc"}).status_code == 400
    assert post("/api/memory/unsure", content=b"[1]").status_code == 400
    assert post("/api/memory/unsure", content=b"{" + b" " * (40 * 1024) + b"}").status_code == 413
    # A path in the body is ignored: the file is fixed under the workspace's vault.
    other = setup.root / "elsewhere.md"
    other.write_text(PLAIN + "\n", encoding="utf-8")
    resp = post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": ul.line_id(PLAIN), "path": str(other)})
    assert resp.status_code == 200
    assert other.read_text(encoding="utf-8") == PLAIN + "\n"


@pytest.mark.parametrize("separator", ["\u2028", "\u0085", "\x0b", "\x0c", "\x1c", "\r"])
def test_only_newlines_end_a_line_for_the_reader_and_the_writer(tmp_path: Path, separator: str) -> None:
    # A separator inside a line keeps it one line: it can be acted on, it
    # cannot hide a second item, and a tick lands on its own box only.
    hidden = f"- [ ] Is this one item?{separator}- [ ] Or a hidden second one? — unsure because test; source: chat-1; 2026-11-27"
    text = f"# Unsure\n\n{hidden}\n{PLAIN}\n"
    path = _write(tmp_path, text)
    items = ul.parse_list(text)
    assert len(items) == 2
    assert items[0]["raw"].startswith("- [ ] Is this one item?")
    body = ul.change_line(tmp_path, "tick", items[0]["id"])
    after = path.read_bytes().decode("utf-8")
    assert after == text.replace("- [ ] Is this one item?", "- [x] Is this one item?", 1)
    assert after.count("- [x]") == 1
    assert body["counts"] == {"waiting": 1, "ticked": 1}


def test_crlf_lines_keep_their_ending(tmp_path: Path) -> None:
    text = f"# Unsure\r\n\r\n{PLAIN}\r\n{DROP}\r\n"
    path = _write(tmp_path, "")
    path.write_bytes(text.encode("utf-8"))
    ul.change_line(tmp_path, "tick", ul.line_id(PLAIN))
    assert path.read_bytes().decode("utf-8") == text.replace("- [ ] Should", "- [x] Should", 1)


def test_identical_lines_are_told_apart_and_only_one_changes(tmp_path: Path) -> None:
    text = f"# Unsure\n\n{PLAIN}\n{DROP}\n{PLAIN}\n"
    path = _write(tmp_path, text)
    ids = [i["id"] for i in ul.parse_list(text)]
    assert ids[0] != ids[2]
    assert ids[0] == ul.line_id(PLAIN, 0) and ids[2] == ul.line_id(PLAIN, 1)
    ul.change_line(tmp_path, "remove", ids[2])
    assert path.read_text(encoding="utf-8") == f"# Unsure\n\n{PLAIN}\n{DROP}\n"
    # The second copy is gone: its id no longer names anything.
    with pytest.raises(ul.UnsureError):
        ul.change_line(tmp_path, "tick", ids[2])


def test_the_heading_and_prose_can_never_be_named(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    for line in ("# Unsure", "Some prose the person wrote.", "- [ ] inside a fence, not an item"):
        with pytest.raises(ul.UnsureError) as caught:
            ul.change_line(tmp_path, "remove", ul.line_id(line))
        assert caught.value.code == "line_changed"
    assert path.read_text(encoding="utf-8") == LIST


def test_kinds_are_lower_case_only() -> None:
    assert ul.parse_item("- [ ] Observation: Not a kind. — unsure because x")["kind"] == ""
    assert ul.parse_item("- [ ] observation: A kind. — unsure because x")["kind"] == "observation"


def test_a_hard_linked_list_is_refused_and_its_twin_untouched(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    twin = tmp_path / "twin.md"
    os.link(path, twin)
    with pytest.raises(ul.UnsureError) as caught:
        ul.change_line(tmp_path, "tick", ul.line_id(PLAIN))
    assert caught.value.code == "not_plain_file"
    assert twin.read_text(encoding="utf-8") == LIST


def test_route_answers_busy_quickly_while_the_lock_is_held(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(setup.vault, LIST)
    monkeypatch.setattr(ul.change_line, "__kwdefaults__", {"lock_timeout_s": 0.3})
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with queue_lock(path):
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        started = time.monotonic()
        resp = setup.client.post("/api/memory/unsure", json={"workspace": "personal", "op": "tick", "id": ul.line_id(PLAIN)})
        assert resp.status_code == 503
        assert resp.json()["code"] == "busy"
        assert time.monotonic() - started < 5
    finally:
        release.set()
        holder.join(5)
    assert path.read_text(encoding="utf-8") == LIST


class _Schedules:
    def __init__(self, status: str) -> None:
        self.status = status

    def get_with_revision(self, schedule_id: str) -> tuple[SimpleNamespace, str]:
        return (SimpleNamespace(schedule_id=schedule_id, last_status=self.status), "r1")


def test_route_refuses_writes_while_care_runs(setup: SimpleNamespace) -> None:
    path = _write(setup.vault, LIST)
    setup.app.state.schedule_manager = _Schedules("running")
    resp = setup.client.post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": ul.line_id(PLAIN)})
    assert resp.status_code == 423
    assert resp.json()["code"] == "care_running"
    assert path.read_text(encoding="utf-8") == LIST
    # Reading is still fine, and a finished run lets the write through.
    assert setup.client.get("/api/memory/unsure?workspace=personal").status_code == 200
    setup.app.state.schedule_manager = _Schedules("ok")
    ok = setup.client.post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": ul.line_id(PLAIN)})
    assert ok.status_code == 200


def test_route_refuses_a_large_declared_body_without_reading_it(setup: SimpleNamespace) -> None:
    resp = setup.client.post(
        "/api/memory/unsure", content=b"{}", headers={"content-length": "999999", "content-type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "too_large"


def test_route_names_a_link_with_its_own_code(setup: SimpleNamespace, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(PLAIN + "\n", encoding="utf-8")
    (setup.vault / "Workspace").mkdir(parents=True)
    os.symlink(outside, setup.vault / "Workspace" / "Unsure.md")
    resp = setup.client.get("/api/memory/unsure?workspace=personal")
    assert resp.status_code == 422
    assert resp.json()["code"] == "not_plain_file"


def test_parser_reads_the_destination_and_the_quoted_line() -> None:
    line = (
        '- [ ] Should I remember that Mo now works at Acme? He said so. "Mo works at Acme since November 2026." '
        "— unsure because two notes disagree; to: People/Mo.md; source: chat-1a2b3c4d and chat-5e6f7a8b; 2026-11-26"
    )
    item = ul.parse_item(line)
    assert item["title"] == "Should I remember that Mo now works at Acme?"
    assert item["proposal"] == "Mo works at Acme since November 2026."
    assert item["destination"] == "People/Mo.md"
    assert item["source"] == "chat-1a2b3c4d and chat-5e6f7a8b"
    assert item["reason"] == "two notes disagree"
    # Either order closes the tail, and a reason with "; " in it is kept whole.
    swapped = ul.parse_item("- [ ] Q? — unsure because a; b; source: chat-1; to: profile; 2026-11-20")
    assert (swapped["reason"], swapped["source"], swapped["destination"]) == ("a; b", "chat-1", "profile")
    # A drop or restore line's quoted entry is its proposal, even when it is the whole text.
    assert ul.parse_item(DROP)["proposal"] == "Prefers terse replies"
    assert ul.parse_item(RESTORE)["proposal"] == "Lives in Zurich"
    # Lines written before `to:` existed carry no destination and no invented line.
    plain = ul.parse_item(PLAIN)
    assert (plain["destination"], plain["proposal"]) == ("", "")


def test_an_item_renders_its_destination_before_its_source() -> None:
    item = ul.UnsureItem("Mo works at Acme?", reason="one chat", source="chat-1", destination="People/Mo.md")
    line = ul.render_line(item, today="2026-11-26")
    assert line == "- [ ] Mo works at Acme? — unsure because one chat; to: People/Mo.md; source: chat-1; 2026-11-26"
    assert ul.parse_item(line)["destination"] == "People/Mo.md"


def test_restore_puts_a_removed_line_back_where_it_was(tmp_path: Path) -> None:
    path = _write(tmp_path, LIST)
    ul.change_line(tmp_path, "remove", ul.line_id(DROP))
    body = ul.restore_line(tmp_path, DROP, before=ul.line_id(OBSERVATION))
    assert path.read_text(encoding="utf-8") == LIST
    assert [i["raw"] for i in body["items"]].count(DROP) == 1
    # Again: the list already holds it, so nothing changes.
    ul.restore_line(tmp_path, DROP, before=ul.line_id(OBSERVATION))
    assert path.read_text(encoding="utf-8") == LIST


def test_restore_without_its_neighbours_goes_to_the_end(tmp_path: Path) -> None:
    path = _write(tmp_path, "# Unsure\n\n" + PLAIN + "\n")
    ul.restore_line(tmp_path, DROP, before=ul.line_id("- [ ] gone since"))
    assert path.read_text(encoding="utf-8") == "# Unsure\n\n" + PLAIN + "\n" + DROP + "\n"
    (tmp_path / "Workspace" / "Unsure.md").unlink()
    ul.restore_line(tmp_path, PLAIN)
    assert path.read_text(encoding="utf-8") == "# Unsure\n\n" + PLAIN + "\n"


def test_restore_goes_back_under_its_own_heading(tmp_path: Path) -> None:
    a, b, c = (f"- [ ] Item {n}? — unsure because x; source: chat-1; 2026-11-20" for n in "abc")
    original = "\n".join(["# Unsure", "", "## People", a, b, "", "## Projects", c, ""])
    path = _write(tmp_path, original)
    assert [i["section"] for i in ul.parse_list(original)] == ["People", "People", "Projects"]
    # b was the last line under People: it goes back after a, not before c under Projects.
    ul.change_line(tmp_path, "remove", ul.line_id(b))
    ul.restore_line(tmp_path, b, before=ul.line_id(c), after=ul.line_id(a), section="People")
    assert path.read_text(encoding="utf-8") == original
    # c was the only line under Projects: it goes back under that heading, not after b.
    ul.change_line(tmp_path, "remove", ul.line_id(c))
    ul.restore_line(tmp_path, c, after=ul.line_id(b), section="Projects")
    assert path.read_text(encoding="utf-8") == original
    # a was the first under People, with b after it.
    ul.change_line(tmp_path, "remove", ul.line_id(a))
    ul.restore_line(tmp_path, a, before=ul.line_id(b), section="People")
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "line",
    [
        OBSERVATION,  # ticked: a care run would act on it
        "- a plain bullet",
        "- [ ] ",
        PLAIN + "\n- [x] smuggled",
        PLAIN + "\r",
        "- [ ] api_key: sk-live-abcdef0123456789 — unsure because x; source: y; 2026-11-20",
        "- [ ] " + "w" * ul.MAX_RESTORE_CHARS,
        "- [ ] q\u2028- [x] evil",  # line separator
        "- [ ] q\u2029- [x] evil",  # paragraph separator
        "- [ ] q\x85- [x] evil",  # NEL
        "- [ ] q\x0b- [x] evil",  # VT
        "- [ ] q\x0c- [x] evil",  # FF
        "- [ ] q\x00 nul",
        "- [ ] q\u202e bidi override",
        "- [ ] q\u200b zero width",
        "- [ ] q\tTab",
        "\t- [ ] indented with a tab",
        "  - [ ] indented with spaces",
        "-\u00a0[ ] no-break space before the box",
        "- [ ]\u00a0no-break space after the box",
        "- [\u00a0] no-break space in the box",
    ],
)
def test_restore_refuses_anything_but_one_unticked_item_line(tmp_path: Path, line: str) -> None:
    path = _write(tmp_path, LIST)
    with pytest.raises(ul.UnsureError) as caught:
        ul.restore_line(tmp_path, line)
    assert (caught.value.status, caught.value.code) == (400, "bad_request")
    assert path.read_text(encoding="utf-8") == LIST


def test_route_restores_a_removed_line(setup: SimpleNamespace) -> None:
    path = _write(setup.vault, LIST)
    post = setup.client.post
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": ul.line_id(DROP)}).status_code == 200
    resp = post("/api/memory/unsure", json={"workspace": "personal", "op": "restore", "line": DROP, "before": ul.line_id(OBSERVATION)})
    assert resp.status_code == 200
    assert path.read_text(encoding="utf-8") == LIST
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "restore"}).status_code == 400
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "restore", "line": OBSERVATION}).json()["code"] == "bad_request"
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "restore", "line": DROP, "before": "../x"}).status_code == 400
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "restore", "line": DROP, "after": 3}).status_code == 400
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "restore", "line": DROP, "section": "a\u2028b"}).status_code == 400


@pytest.mark.parametrize(
    ("destination", "allowed"),
    [
        ("People/Mo.md", True),
        ("projects/active/Northwind/Northwind.md", True),
        ("profile", True),
        ("memory", True),
        ("", False),
        ("../../.ssh/authorized_keys", False),
        ("../People/Mo.md", False),
        ("/etc/passwd.md", False),
        ("~/notes/x.md", False),
        ("People/./Mo.md", False),
        ("People//Mo.md", False),
        ("People/.hidden.md", False),
        (".ciaoignore", False),
        ("People\\Mo.md", False),
        ("C:/x.md", False),
        ("People/Mo.txt", False),
        ("People/Mo", False),
        ("Workspace/Care-Report.md", False),
        ("Workspace/Unsure.md", False),
        ("workspace/Proposals/x.md", False),
        ("automations/daily/PROMPT.md", False),
        ("AGENTS.md", False),
        ("agents.md", False),
        ("People/Mo\u202e.md", False),
        ("regions", False),
        # Case and compatibility tricks a case-insensitive disk would resolve to a protected file.
        ("AGENTſ.md", False),
        ("Workſpace/Unsure.md", False),
        ("automationſ/x/PROMPT.md", False),
        ("WORKSPACE/Unsure.md", False),
        ("wor\u212aspace/Unsure.md", False),  # Kelvin sign
        ("\uff37orkspace/Unsure.md", False),  # fullwidth W
        ("\uff21\uff27\uff25\uff2e\uff34\uff33.md", False),  # fullwidth AGENTS
        ("Agents.MD", False),
        ("Workspace./Unsure.md", False),
        ("Workspace /Unsure.md", False),
        ("People/Mo.md.", False),
        ("People /Mo.md", False),
        ("Profile", False),
    ],
)
def test_the_parser_flags_a_destination_an_approval_may_not_write(destination: str, allowed: bool) -> None:
    line = f"- [ ] Mo works at Acme? \"Mo works at Acme.\" — unsure because x; to: {destination}; source: chat-1; 2026-11-20"
    item = ul.parse_item(line)
    if destination:
        assert item["destination"] == destination
    assert item["destination_ok"] is allowed
    assert ul.destination_allowed(destination) is allowed


def test_the_route_hands_the_page_the_destination_flag(setup: SimpleNamespace) -> None:
    hostile = '- [ ] Mo works at Acme? "x" — unsure because y; to: ../../.ssh/authorized_keys; source: chat-1; 2026-11-20'
    fine = '- [ ] Mo works at Acme too? "x" — unsure because y; to: People/Mo.md; source: chat-1; 2026-11-20'
    _write(setup.vault, "# Unsure\n" + hostile + "\n" + fine + "\n")
    items = setup.client.get("/api/memory/unsure?workspace=personal").json()["items"]
    assert [(i["destination"], i["destination_ok"]) for i in items] == [
        ("../../.ssh/authorized_keys", False),
        ("People/Mo.md", True),
    ]


def test_a_line_id_has_nothing_after_it() -> None:
    good = ul.line_id(PLAIN)
    assert ul.is_line_id(good)
    assert not ul.is_line_id(good + "\n")
    assert not ul.is_line_id(good + " ")
