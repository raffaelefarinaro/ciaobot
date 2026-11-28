"""Memory · Changes: the feed reader (:mod:`ciao.changes_feed`) and `GET /api/memory/changes`.

Everything the feed shows is read from files a model wrote: the undo log, the
care report and proposal files. The reader is bounded, confined to the vault,
never follows a link and never raises.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ciao import changes_feed
from ciao.changes_feed import build_feed, parse_undo_log, read_skill_suggestions, read_undo_log
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from tests.session import signed_in

D = chr(0x2014)
LQ, RQ = chr(0x201C), chr(0x201D)
LOG = f"""# Memory consolidations

## 2026-11-26
- People/Tom.md {D} by: memory pass chat-aaa {D} "Tom prefers tea" {D} superseded: he switched to coffee

## 2026-11-28
- memory {D} by: care regions {D} {LQ}Meeting at 10 on Fridays{RQ} {D} expired
- People/Tom.md {D} by: care second-look {D} "Tom lives in Bern" {D} merged into People/Tomas Berg.md
- Projects/Atlas.md -- by: chat chat-bbb -- "Launch in May" -- moved to March
"""


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    return vault


def _log(vault: Path, text: str) -> Path:
    path = vault / "Workspace" / "Memory-Consolidations.md"
    path.write_text(text, encoding="utf-8")
    return path


def _proposal(vault: Path, slug: str, kind: str = "skill-edit", created: str = "2026-11-28", target: str = "weekly-summary") -> Path:
    folder = vault / "Workspace" / "Proposals"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{slug}.md"
    path.write_text(
        f"---\ntype: {kind}\ntarget: {target}\ntitle: Tighten the weekly summary\nsummary: s\nrationale: r\n"
        f"evidence: [a]\nproposed_change: p\ncreated: {created}\nstatus: ready\n---\nbody\n",
        encoding="utf-8",
    )
    return path


# ── Parsing the undo log ────────────────────────────────────────────────────


def test_parses_each_entry_with_its_day_writer_original_and_reason() -> None:
    entries = parse_undo_log(LOG)
    assert [(e["date"], e["kind"], e["by"]["kind"], e["by"]["id"]) for e in entries] == [
        ("2026-11-26", "note-edit", "memory_pass", "chat-aaa"),
        ("2026-11-28", "memory-remove", "care", "regions"),
        ("2026-11-28", "merge", "care", "second-look"),
        ("2026-11-28", "note-edit", "chat", "chat-bbb"),
    ]
    region, merged, ascii_dashes = entries[1], entries[2], entries[3]
    assert region["region"] == "memory" and region["path"] == "" and region["area"] == "memory"
    assert region["quote"] == "Meeting at 10 on Fridays" and region["why"] == "expired"
    assert merged["path"] == "People/Tom.md" and merged["quote"] == "Tom lives in Bern"
    assert ascii_dashes["quote"] == "Launch in May" and ascii_dashes["why"] == "moved to March"
    assert all(e["undo"] for e in entries)
    assert len({e["id"] for e in entries}) == 4


def test_a_quote_holding_a_dash_keeps_it_and_the_reason_is_the_last_part() -> None:
    [entry] = parse_undo_log(f'## 2026-11-28\n- People/A.md {D} by: care stale {D} "one {D} two" {D} why\n')
    assert entry["quote"] == f"one {D} two"
    assert entry["why"] == "why"


@pytest.mark.parametrize("target", ["../outside.md", "/etc/passwd", "~/x.md", "C:/x.md", "a\\b.md", ".git/config", "People/../x.md"])
def test_a_target_outside_the_vault_gives_no_path(target: str) -> None:
    [entry] = parse_undo_log(f'## 2026-11-28\n- {target} {D} by: care stale {D} "x" {D} y\n')
    assert entry["path"] == ""
    assert entry["region"] == ""


def test_profile_region_written_in_the_guides_words() -> None:
    [entry] = parse_undo_log(f'## 2026-11-28\n- AGENTS.md ciao:profile {D} by: chat c1 {D} "Lives in Zurich" {D} moved\n')
    assert entry["region"] == "profile" and entry["kind"] == "memory-remove"


def test_lines_that_are_not_entries_are_skipped() -> None:
    text = (
        "intro line\n"
        f"- before any day {D} by: care x {D} \"q\" {D} w\n"
        "## 2026-11-28\n"
        "- a plain bullet with no writer\n"
        "~~~\n"
        f"- People/In-fence.md {D} by: care x {D} \"q\" {D} w\n"
        "~~~\n"
        "## Notes\n"
        f"- People/After-notes.md {D} by: care x {D} \"q\" {D} w\n"
    )
    entries = parse_undo_log(text)
    assert [(e["date"], e["target"]) for e in entries] == [("", "before any day"), ("", "People/After-notes.md")]


def test_two_identical_lines_get_two_ids() -> None:
    line = f'- People/A.md {D} by: care stale {D} "same" {D} why\n'
    entries = parse_undo_log("## 2026-11-28\n" + line + line)
    assert len({e["id"] for e in entries}) == 2


def test_an_entry_without_an_original_offers_no_undo() -> None:
    [entry] = parse_undo_log(f"## 2026-11-28\n- People/A.md {D} by: care stale\n")
    assert entry["quote"] == "" and entry["undo"] is False


def test_model_text_is_flattened_clipped_and_redacted() -> None:
    long_why = "word " * 400
    [entry] = parse_undo_log(f'## 2026-11-28\n- People/A.md {D} by: care stale {D} "password: hunter2hunter2" {D} {long_why}\n')
    assert "hunter2" not in entry["quote"]
    assert "[redacted]" in entry["quote"]
    assert len(entry["why"]) == changes_feed.MAX_TEXT_CHARS


# ── Reading files from the vault ────────────────────────────────────────────


def test_missing_log_is_missing(tmp_path: Path) -> None:
    assert read_undo_log(_vault(tmp_path)) == ("missing", [])


def test_a_symlinked_log_or_folder_is_not_followed(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(LOG, encoding="utf-8")
    (vault / "Workspace" / "Memory-Consolidations.md").symlink_to(outside)
    assert read_undo_log(vault) == ("unreadable", [])

    other = tmp_path / "other"
    (other / "Workspace").mkdir(parents=True)
    (other / "Workspace" / "Memory-Consolidations.md").write_text(LOG, encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "Workspace").symlink_to(other / "Workspace")
    assert read_undo_log(linked) == ("unreadable", [])


def test_a_hard_linked_log_is_refused(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(LOG, encoding="utf-8")
    os.link(outside, vault / "Workspace" / "Memory-Consolidations.md")
    assert read_undo_log(vault)[0] == "unreadable"


def test_a_large_log_reads_only_its_newest_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(changes_feed, "MAX_LOG_BYTES", 2048)
    vault = _vault(tmp_path)
    old = "".join(f'- People/Old-{i}.md {D} by: care stale {D} "old {i}" {D} w\n' for i in range(200))
    _log(vault, "## 2026-11-01\n" + old + "## 2026-11-28\n" + f'- People/New.md {D} by: care stale {D} "new" {D} w\n')
    status, entries = read_undo_log(vault)
    assert status == "tail"
    assert entries[-1]["target"] == "People/New.md" and entries[-1]["date"] == "2026-11-28"
    # The cut-off head has no heading left, so its entries carry no date.
    assert len(entries) > 1 and all(e["date"] == "" for e in entries[:-1])


def test_entries_are_capped_to_the_newest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(changes_feed, "MAX_UNDO_ENTRIES", 3)
    vault = _vault(tmp_path)
    _log(vault, "## 2026-11-28\n" + "".join(f'- People/N{i}.md {D} by: care stale {D} "q{i}" {D} w\n' for i in range(10)))
    _status, entries = read_undo_log(vault)
    assert [e["target"] for e in entries] == ["People/N7.md", "People/N8.md", "People/N9.md"]


def test_skill_suggestions_are_skill_edit_proposals_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _proposal(vault, "tighten-weekly", created="2026-11-27")
    _proposal(vault, "retire-tom", kind="note-retire", target="People/Tom.md")
    _proposal(vault, "bad-target", target="../../etc")
    folder = vault / "Workspace" / "Proposals"
    (folder / "broken.md").write_text("---\ntype: [skill-edit\n---\n", encoding="utf-8")
    (folder / "Not A Slug.md").write_text("---\ntype: skill-edit\n---\n", encoding="utf-8")
    found = read_skill_suggestions(vault)
    assert [(p["path"], p["date"], p["target"]) for p in found] == [
        ("Workspace/Proposals/bad-target.md", "2026-11-28", ""),
        ("Workspace/Proposals/tighten-weekly.md", "2026-11-27", "weekly-summary"),
    ]
    assert found[1]["title"] == "Tighten the weekly summary" and found[1]["status"] == "ready"


def test_a_symlinked_proposals_folder_or_file_is_not_followed(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    _proposal(elsewhere, "outside")
    (vault / "Workspace" / "Proposals").symlink_to(elsewhere / "Workspace" / "Proposals")
    assert read_skill_suggestions(vault) == []

    vault2 = _vault(tmp_path / "two")
    (vault2 / "Workspace" / "Proposals").mkdir()
    (vault2 / "Workspace" / "Proposals" / "linked.md").symlink_to(elsewhere / "Workspace" / "Proposals" / "outside.md")
    assert read_skill_suggestions(vault2) == []


def test_proposal_files_read_are_capped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(changes_feed, "MAX_PROPOSALS", 2)
    vault = _vault(tmp_path)
    for i in range(5):
        _proposal(vault, f"p{i}")
    assert len(read_skill_suggestions(vault)) == 2


# ── Grouping and counting ───────────────────────────────────────────────────

REPORT: dict[str, Any] = {
    "status": "ok",
    "updated": "2026-11-28",
    "modified_at": "2026-11-28T10:30:00Z",
    "steps": [
        {"id": "hygiene", "state": "done", "what": "rebuild the index", "evidence": "fixed 10 of 10 findings, left 0"},
        {"id": "closed-tasks", "state": "done", "what": "learn from tasks", "evidence": "read 2 tasks, wrote 1 fact (People/User.md)"},
        {"id": "guide-review", "state": "blocked", "what": "review", "evidence": "AGENTS.md unreadable"},
    ],
}


def _pass(chat_id: str, finished: str, title: str = "Thailand booking") -> dict[str, Any]:
    return changes_feed.pass_entries([{"chat_id": chat_id, "source_chat_id": "src-" + chat_id, "source_title": title, "finished_at": finished}])[0]


def test_groups_the_care_run_and_the_days_newest_first() -> None:
    undo = parse_undo_log(LOG)
    proposals = [{"id": "p-1", "date": "2026-11-28", "kind": "skill-suggestion", "area": "skills", "path": "Workspace/Proposals/x.md", "cites_care_report": True}]
    feed = build_feed(undo=undo, report=REPORT, proposals=proposals, passes=[_pass("pass-1", "2026-11-27T12:00:00")], today=date(2026, 11, 28))
    assert [(g["kind"], g["date"]) for g in feed["groups"]] == [
        ("care", "2026-11-28"), ("day", "2026-11-28"), ("day", "2026-11-27"), ("day", "2026-11-26"),
    ]
    care = feed["groups"][0]
    # The run's own undo-log lines (newest first), its finished steps, then the suggestion of its day.
    assert [e["kind"] for e in care["entries"]] == ["merge", "memory-remove", "care-step", "task-closed", "skill-suggestion"]
    assert care["steps"] == 2
    assert [e["by"]["kind"] for e in feed["groups"][1]["entries"]] == ["chat"]
    assert feed["groups"][2]["entries"][0]["kind"] == "chat-processed"
    assert feed["care_run"] == {"date": "2026-11-28", "at": "2026-11-28T10:30:00Z", "lines_changed": 2, "skill_suggestions": 1}


def test_counts_cover_only_the_last_seven_days() -> None:
    days = ["2026-11-20", "2026-11-21", "2026-11-22", "2026-11-25", "2026-11-28", "2026-11-29"]
    text = "".join(f'## {d}\n- People/A.md {D} by: care stale {D} "q {d}" {D} w\n' for d in days)
    text += f'## 2026-11-28\n- memory {D} by: care regions {D} "m" {D} w\n'
    proposals = [{"id": f"p-{d}", "date": d, "kind": "skill-suggestion"} for d in days]
    passes = [_pass(f"c-{d}", f"{d}T12:00:00") for d in days]
    feed = build_feed(undo=parse_undo_log(text), report=None, proposals=proposals, passes=passes, today=date(2026, 11, 28))
    assert feed["week"] == {
        "since": "2026-11-22", "lines_changed": 4, "memory_lines": 1, "note_lines": 3, "skill_suggestions": 3, "chats_processed": 3,
    }
    assert feed["today"] == {"date": "2026-11-28", "lines_changed": 2, "skill_suggestions": 1, "chats_processed": 1}
    assert feed["care_run"] is None


def test_a_report_with_no_finished_step_makes_no_care_group() -> None:
    report = {**REPORT, "steps": [{"id": "stale", "state": "blocked", "what": "x", "evidence": "y"}]}
    feed = build_feed(undo=parse_undo_log(LOG), report=report, proposals=[], passes=[], today=date(2026, 11, 28))
    assert all(g["kind"] == "day" for g in feed["groups"])
    assert feed["care_run"] is None


def test_the_answer_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(changes_feed, "MAX_ENTRIES", 5)
    text = "".join(f'## 2026-11-{d:02d}\n' + "".join(f'- People/N{i}.md {D} by: care stale {D} "q" {D} w\n' for i in range(3)) for d in range(1, 29))
    feed = build_feed(undo=parse_undo_log(text), report=None, proposals=[], passes=[], today=date(2026, 11, 28))
    assert sum(len(g["entries"]) for g in feed["groups"]) == 5
    assert feed["groups"][0]["date"] == "2026-11-28"


# ── The route ───────────────────────────────────────────────────────────────


class _Chats:
    def __init__(self, chats: list[Any], projects: dict[str, Any]) -> None:
        self.chats = chats
        self.projects = projects

    def list_chats(self) -> list[Any]:
        return self.chats

    def get_project(self, project_id: str) -> Any:
        return self.projects.get(project_id)


def _chat(chat_id: str, project_id: str, state: str = "done", kind: str = "memory_pass") -> Any:
    return SimpleNamespace(
        chat_id=chat_id, project_id=project_id, created_at="2026-11-28T08:00:00Z", last_activity_at="2026-11-28T09:12:00Z",
        helper={"kind": kind, "state": state, "source_chat_id": "src-" + chat_id, "source_title": "Thailand booking"},
    )


@pytest.fixture
def setup(tmp_path: Path):
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
    projects = {"mem-p": SimpleNamespace(workspace="personal"), "mem-w": SimpleNamespace(workspace="work")}
    app.state.project_chat_manager = _Chats(
        [_chat("pass-1", "mem-p"), _chat("pass-2", "mem-p", state="running"), _chat("pass-3", "mem-w"), _chat("plain", "mem-p", kind="")],
        projects,
    )
    vault = tmp_path / "memory-vault" / "personal"
    (vault / "Workspace").mkdir(parents=True)
    return SimpleNamespace(client=signed_in(TestClient(app)), vault=vault, root=tmp_path)


def test_route_answers_the_feed_with_viewer_paths(setup) -> None:
    _log(setup.vault, LOG)
    (setup.vault / "People").mkdir()
    (setup.vault / "People" / "Tom.md").write_text("# Tom\n", encoding="utf-8")
    _proposal(setup.vault, "tighten-weekly")
    body = setup.client.get("/api/memory/changes?workspace=personal").json()
    assert body["workspace"] == "personal"
    assert body["undo_log"] == "ok"
    assert body["undo_log_path"] == "memory-vault/personal/Workspace/Memory-Consolidations.md"
    entries = [e for g in body["groups"] for e in g["entries"]]
    by_target = {e.get("target"): e for e in entries if e["kind"] != "chat-processed"}
    assert by_target["People/Tom.md"]["open_path"] == "memory-vault/personal/People/Tom.md"
    assert by_target["memory"]["open_path"] == ""
    assert by_target["weekly-summary"]["open_path"] == "memory-vault/personal/Workspace/Proposals/tighten-weekly.md"
    processed = [e for e in entries if e["kind"] == "chat-processed"]
    # Only this workspace's finished passes.
    assert [(e["pass_chat_id"], e["chat_id"], e["title"]) for e in processed] == [("pass-1", "src-pass-1", "Thailand booking")]


def test_route_with_nothing_recorded_is_empty(setup) -> None:
    body = setup.client.get("/api/memory/changes?workspace=work").json()
    assert body["undo_log"] == "missing"
    assert body["week"]["lines_changed"] == 0


@pytest.mark.parametrize("query", ["", "?workspace=", "?workspace=nope", "?workspace=../personal"])
def test_route_refuses_an_unknown_workspace(setup, query: str) -> None:
    assert setup.client.get(f"/api/memory/changes{query}").status_code == 400


def test_route_is_read_only(setup) -> None:
    path = _log(setup.vault, LOG)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert setup.client.get("/api/memory/changes?workspace=personal").status_code == 200
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert setup.client.post("/api/memory/changes?workspace=personal").status_code == 405


def test_route_needs_a_session(setup) -> None:
    anonymous = TestClient(setup.client.app)
    assert anonymous.get("/api/memory/changes?workspace=personal").status_code in (401, 403)


def test_route_does_not_leak_the_vault_path_when_it_is_unavailable(setup, monkeypatch) -> None:
    config = setup.client.app.state.config

    def broken(workspace):  # type: ignore[no-untyped-def]
        raise OSError(f"cannot reach {setup.root}/secret/vault")

    monkeypatch.setattr(type(config), "workspace_vault_root", lambda self, ws: broken(ws))
    response = setup.client.get("/api/memory/changes?workspace=personal")
    assert response.status_code == 409
    assert str(setup.root) not in response.text


# ── Review fixes: bounded parsing, honest credit, strict paths ─────────────


ADVERSARIAL = {
    "whitespace in a bullet": "- a" + " " * 500_000 + "b",
    "whitespace in a heading": "## x" + " " * 500_000 + "y",
    "a run of hashes": "#" * 500_000,
    "hashes after a heading": "## 2026-11-28 " + "#" * 3000 + " " * 3000 + "y",
    "repeated by:": "- " + " by: " * 100_000,
    "repeated separators": "- x " + D + " by: " + (D + " ") * 100_000,
    "repeated quotes and separators": f'- x {D} by: care y {D} "' + (f'" {D} ' * 60_000) + "w",
}


@pytest.mark.parametrize("shape", sorted(ADVERSARIAL))
def test_a_hostile_line_is_read_quickly(shape: str) -> None:
    import time

    line = ADVERSARIAL[shape]
    text = "## 2026-11-28\n" + line + "\n" + f'- People/A.md {D} by: care stale {D} "q" {D} w\n'
    started = time.perf_counter()
    entries = parse_undo_log(text)
    assert time.perf_counter() - started < 0.5, shape
    assert entries[-1]["target"] == "People/A.md"


def test_hostile_lines_under_the_line_cap_are_still_fast() -> None:
    import time

    cap = changes_feed.MAX_LINE_CHARS - 10
    shapes = [
        "- a" + " " * cap + "b",
        "## x" + " " * cap + "y",
        "- " + " by: " * (cap // 5),
        "- x " + D + " by: " + (D + " ") * (cap // 2),
        f'- x {D} by: care y {D} "' + (f'" {D} ' * (cap // 4)) + "w",
    ]
    started = time.perf_counter()
    for line in shapes * 50:
        parse_undo_log("## 2026-11-28\n" + line[: changes_feed.MAX_LINE_CHARS] + "\n")
    assert time.perf_counter() - started < 1.0


def test_hostile_proposal_front_matter_is_read_quickly(tmp_path: Path) -> None:
    import time

    vault = _vault(tmp_path)
    folder = vault / "Workspace" / "Proposals"
    folder.mkdir()
    (folder / "spaces.md").write_text("---\ntype: skill-edit\ntitle: x" + " " * 60_000 + "y\n---\n", encoding="utf-8")
    (folder / "open.md").write_text("---\ntype: skill-edit\n" + "evidence:\n" + "  - a\n" * 9000, encoding="utf-8")
    (folder / "anchors.md").write_text("---\ntype: skill-edit\na: &a [x, x]\nb: [*a, *a, *a]\n---\n", encoding="utf-8")
    started = time.perf_counter()
    found = read_skill_suggestions(vault)
    assert time.perf_counter() - started < 0.5
    # An unclosed front matter is not one; the over-long title line is skipped.
    assert sorted(p["path"] for p in found) == ["Workspace/Proposals/anchors.md", "Workspace/Proposals/spaces.md"]


def test_front_matter_is_read_without_a_yaml_loader(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    folder = vault / "Workspace" / "Proposals"
    folder.mkdir()
    (folder / "block.md").write_text(
        "---\ntype: 'skill-edit'\ntarget: \"weekly-summary\"\ntitle: |\n  Shorten the\n  weekly summary\n"
        "evidence:\n  - Workspace/Care-Report.md\n  - chat-1\ncreated: 2026-11-28\nstatus: draft\n---\nbody\n",
        encoding="utf-8",
    )
    [found] = read_skill_suggestions(vault)
    assert (found["target"], found["title"], found["date"], found["status"]) == ("weekly-summary", "Shorten the weekly summary", "2026-11-28", "draft")
    assert found["cites_care_report"] is True


def _suggestion(path: str, day: str, cites: bool = False) -> dict[str, Any]:
    return {"id": "p-" + path, "date": day, "kind": "skill-suggestion", "area": "skills", "path": path, "cites_care_report": cites}


def test_a_care_run_is_credited_only_with_proposals_it_names() -> None:
    report = {**REPORT, "steps": [*REPORT["steps"], {
        "id": "learnings", "state": "done", "what": "route learnings",
        "evidence": "routed 1 entries (Workspace/Proposals/named-by-step.md), merged 0, pruned 0",
    }]}
    proposals = [
        _suggestion("Workspace/Proposals/named-by-step.md", "2026-11-28"),
        _suggestion("Workspace/Proposals/cites-the-report.md", "2026-11-28", cites=True),
        # Same day, written by a memory pass or a chat: not the run's.
        _suggestion("Workspace/Proposals/from-a-chat.md", "2026-11-28"),
        # Cites the report but from another day: not this run's.
        _suggestion("Workspace/Proposals/older.md", "2026-11-20", cites=True),
    ]
    feed = build_feed(undo=[], report=report, proposals=proposals, passes=[], today=date(2026, 11, 28))
    care, *days = feed["groups"]
    assert care["kind"] == "care"
    assert [e["path"] for e in care["entries"] if e["kind"] == "skill-suggestion"] == [
        "Workspace/Proposals/named-by-step.md", "Workspace/Proposals/cites-the-report.md",
    ]
    assert feed["care_run"]["skill_suggestions"] == 2
    filed = {g["date"]: [e["path"] for e in g["entries"]] for g in days}
    assert filed == {"2026-11-28": ["Workspace/Proposals/from-a-chat.md"], "2026-11-20": ["Workspace/Proposals/older.md"]}


@pytest.mark.parametrize("target", ["memory region", "the memory region of AGENTS.md", "Profile region"])
def test_a_region_named_in_words_is_a_region(target: str) -> None:
    [entry] = parse_undo_log(f'## 2026-11-28\n- {target} {D} by: care regions {D} "x" {D} y\n')
    assert entry["region"] in ("memory", "profile") and entry["path"] == ""


@pytest.mark.parametrize("target", ["Tom said he likes tea", "People/Tom", "removed a line from the note about tea in People"])
def test_a_sentence_is_never_a_path(target: str) -> None:
    [entry] = parse_undo_log(f'## 2026-11-28\n- {target} {D} by: care stale {D} "x" {D} y\n')
    assert entry["path"] == ""


def test_a_note_that_is_not_there_gets_no_path(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    (vault / "People").mkdir()
    (vault / "People" / "Here.md").write_text("x", encoding="utf-8")
    (vault / "People" / "Linked.md").symlink_to(vault / "People" / "Here.md")
    _log(vault, "## 2026-11-28\n" + "".join(
        f'- People/{n}.md {D} by: care stale {D} "q" {D} w\n' for n in ("Here", "Gone", "Linked")
    ))
    feed = changes_feed.read_changes(vault, [], today=date(2026, 11, 28))
    paths = {e["target"]: e["path"] for g in feed["groups"] for e in g["entries"]}
    assert paths == {"People/Here.md": "People/Here.md", "People/Gone.md": "", "People/Linked.md": ""}
