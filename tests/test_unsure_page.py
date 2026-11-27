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
    assert caught.value.status == 409
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
        assert caught.value.status == 409
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
    assert stale.json() == {"error": "the list changed; reload it"}
    assert "Mo now works" not in caplog.text


def test_route_takes_no_path_and_rejects_bad_bodies(setup: SimpleNamespace) -> None:
    _write(setup.vault, LIST)
    post = setup.client.post
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "erase", "id": ul.line_id(PLAIN)}).status_code == 400
    assert post("/api/memory/unsure", json={"workspace": "personal", "op": "tick", "id": "../etc"}).status_code == 400
    assert post("/api/memory/unsure", content=b"[1]").status_code == 400
    assert post("/api/memory/unsure", content=b"{" + b" " * 2048 + b"}").status_code == 413
    # A path in the body is ignored: the file is fixed under the workspace's vault.
    other = setup.root / "elsewhere.md"
    other.write_text(PLAIN + "\n", encoding="utf-8")
    resp = post("/api/memory/unsure", json={"workspace": "personal", "op": "remove", "id": ul.line_id(PLAIN), "path": str(other)})
    assert resp.status_code == 200
    assert other.read_text(encoding="utf-8") == PLAIN + "\n"
