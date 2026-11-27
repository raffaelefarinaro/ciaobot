"""Workspace care report: the parser and `GET /api/care-report`.

The report is the checklist the daily care run writes into
`<vault>/Workspace/Care-Report.md`. The reader is read-only, bounded and never
raises: every state the Home card and the report page show comes from here.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from ciao import care_report
from ciao.care_report import MAX_REPORT_BYTES, STEP_FILES, parse_care_report, read_care_report
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from tests.session import signed_in

COMPLETE = """# Care report 2026-11-26
Weekly done: 2026-11-26
- [x] unsure: apply ticked Unsure lines (2 ticked) \u2014 applied 2, left 0 ticked; verify: 0 `- [x]` lines left
- [x] regions: consolidate regions (memory 91%) \u2014 memory 91% -> 72%; verify: memory.pct 72, profile.pct 40
"""

BLOCKED = """# Care report 2026-11-26
Weekly done: 2026-11-19
Interrupted last run: guide-review, stale
- [x] unsure: apply ticked Unsure lines \u2014 applied 2; verify: 0 left
- [!] hygiene: run the vault index \u2014 blocked: cannot write INDEX.md
- [!] guide-review: review the AGENTS.md body (weekly due) \u2014 blocked: AGENTS.md unreadable

## What needs you
1. **Hygiene is blocked**: the vault index command exited 2.
2. Cross-project fact waiting for approval: "Invoices go out on the 1st." Remember it
   for every project? It is on the Unsure list.
3. The memory region is at 91% of its limit.
"""

# The items of a real run, as the needs-you standard asks for them
# (curation-report-example.md): one decision each, plain words, an explicit path.
STANDARD = """# Care report 2026-11-27
- [x] unsure: apply ticked lines \\u2014 applied 1; verify: 0 left
- [!] stale: re-check stale notes (weekly due) \\u2014 blocked: audit still lists the notes

## What needs you
1. A proposal would trim your guide: it removes an outdated status section and a repeated folder path. Open the proposal and approve or reject it. (path: Workspace/Proposals/guide-trim-duplicates.md)
2. Your always-loaded memory is nearly full (98% and 95%) and everything in it still matters. Ask a chat to trim it with you. (path: AGENTS.md)
3. 13 suggestions are waiting for your tick in Unsure, such as which brief template to keep. Tick the ones you agree with. (path: `Workspace/Unsure.md`)
4. needs you: The vault index lists 63 issues, such as notes nothing links to. Open a chat to fix them with you. (path: Workspace/Care-Report.md).

## Notes
- Re-checking older notes did not finish; it tries again on the next run.
- note: 17 memory passes were not re-read this time; the next run continues.
- [ ] About 200 older patterns are still to be moved to Unsure; the next run continues.
"""

INTERRUPTED = """# Care report 2026-11-26
Weekly done: 2026-11-19
- [x] unsure: apply ticked lines \u2014 applied 1; verify: 0 left
- [x] regions: consolidate \u2014 done; verify: pct 70
- [ ] learnings: promote entries (3 at x3)
- [ ] stale: re-check stale notes (weekly due)
"""


def test_complete_report_reads_every_field() -> None:
    body = parse_care_report(COMPLETE)
    assert body["updated"] == "2026-11-26"
    assert body["weekly_done"] == "2026-11-26"
    assert body["interrupted_ids"] == []
    assert body["counts"] == {"total": 2, "done": 2, "blocked": 0, "open": 0}
    first = body["steps"][0]
    assert first == {
        "id": "unsure",
        "state": "done",
        "what": "apply ticked Unsure lines (2 ticked)",
        "evidence": "applied 2, left 0 ticked",
        "verify": "0 `- [x]` lines left",
    }
    assert body["needs_you"] == []
    assert body["malformed"] is False
    assert body["nothing_due"] is False


def test_blocked_report_reads_reasons_interrupted_ids_and_needs_you() -> None:
    body = parse_care_report(BLOCKED)
    assert body["interrupted_ids"] == ["guide-review", "stale"]
    assert body["counts"] == {"total": 3, "done": 1, "blocked": 2, "open": 0}
    hygiene = body["steps"][1]
    assert hygiene["state"] == "blocked"
    assert hygiene["evidence"] == "cannot write INDEX.md"
    assert hygiene["verify"] == ""
    needs = body["needs_you"]
    assert [n["title"] for n in needs] == [
        "Hygiene is blocked",
        'Cross-project fact waiting for approval: "Invoices go out on the 1st." Remember it for every project?',
        "The memory region is at 91% of its limit.",
    ]
    # A continuation line belongs to the item above it.
    assert needs[1]["detail"].endswith("It is on the Unsure list.")
    # A report from before the needs-you standard names no path: no file button.
    assert [n["path"] for n in needs] == ["", "", ""]
    assert body["notes"] == []


def test_standard_items_read_title_detail_and_path() -> None:
    body = parse_care_report(STANDARD)
    needs = body["needs_you"]
    assert [n["path"] for n in needs] == [
        "Workspace/Proposals/guide-trim-duplicates.md", "AGENTS.md", "Workspace/Unsure.md", "Workspace/Care-Report.md",
    ]
    assert needs[0]["title"] == "A proposal would trim your guide: it removes an outdated status section and a repeated folder path."
    assert needs[0]["detail"] == "Open the proposal and approve or reject it."
    # The `needs you:` lead a step wrote is not part of what the person reads.
    assert needs[3]["title"] == "The vault index lists 63 issues, such as notes nothing links to."
    assert all("(path:" not in n["title"] + n["detail"] for n in needs)
    assert body["notes"] == [
        "Re-checking older notes did not finish; it tries again on the next run.",
        "17 memory passes were not re-read this time; the next run continues.",
        "About 200 older patterns are still to be moved to Unsure; the next run continues.",
    ]
    # A checkbox under Notes is a note, never a step.
    assert [s["id"] for s in body["steps"]] == ["unsure", "stale"]


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd", "~/secrets.md", "../outside.md", "Workspace/../../x.md", "Workspace/./Unsure.md",
        "C:/Users/x.md", "c:x.md", "Workspace\\Unsure.md", "Workspace//Unsure.md", "Workspace/Unsure.md/",
        "Workspace/\x00.md", "Workspace/a\x1b.md", " ", "x" * 300 + ".md",
        # Hidden files are never a note, and a bidi override reads a path backwards.
        ".env", ".git/config", "Workspace/.care-drained", "People/Rae\u202egnp.md", "People/Rae\u200b.md",
        # The `(path: …)` tail cannot carry parentheses, so a path never holds them.
        "Projects/Atlas (2026).md",
    ],
)
def test_an_unsafe_path_is_dropped_not_followed(raw: str) -> None:
    assert care_report.vault_relative_path(raw) == ""
    item = parse_care_report(f"- [x] a: b\n## What needs you\n1. Look at it. (path: {raw})\n")["needs_you"][0]
    assert item["path"] == ""
    assert item["title"] == "Look at it."


@pytest.mark.parametrize("raw", ["Workspace/Unsure.md", "People/Rae Smith.md", "`AGENTS.md`", "Projects/Atlas 2026.md", "People/Zoë Ruiz.md"])
def test_a_vault_relative_path_is_kept(raw: str) -> None:
    assert care_report.vault_relative_path(raw) == raw.strip("`")


@pytest.mark.parametrize(
    ("path", "proposal"),
    [
        ("Workspace/Proposals/trim-guide.md", True),
        ("Workspace/Proposals/Trim.md", False),
        ("Workspace/Proposals/sub/trim.md", False),
        ("Workspace/Proposals/trim.txt", False),
        ("Workspace/Unsure.md", False),
        ("", False),
    ],
)
def test_proposal_paths_are_the_proposal_check_slug(path: str, proposal: bool) -> None:
    assert care_report.is_proposal_path(path) is proposal


def _write_proposal(vault: Path, slug: str, status: str) -> Path:
    path = vault / "Workspace" / "Proposals" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: guide-edit\ntarget: AGENTS.md\ntitle: Trim\nsummary: s\nrationale: r\nevidence:\n  - Workspace/Care-Report.md\n"
        f"proposed_change: c\ncreated: 2026-11-26\nstatus: {status}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("status", ["draft", "ready", "applied", "rejected"])
def test_proposal_status_is_read_from_the_file(tmp_path: Path, either_branch: str, status: str) -> None:
    _write_proposal(tmp_path, "trim-guide", status)
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/trim-guide.md") == status


def test_proposal_status_is_empty_when_it_cannot_be_trusted(tmp_path: Path, either_branch: str) -> None:
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/missing.md") == ""
    _write_proposal(tmp_path, "odd", "maybe")
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/odd.md") == ""
    # Two status keys, or a BOM before the front matter, make no status: the app reads both the same way.
    twice = _write_proposal(tmp_path, "twice", "draft")
    twice.write_text(twice.read_text().replace("status: draft", "status: draft\nstatus: applied"))
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/twice.md") == ""
    bom = _write_proposal(tmp_path, "bom", "draft")
    bom.write_text("\ufeff" + bom.read_text())
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/bom.md") == ""
    # Only a proposal path is read: never a file the path merely names.
    _write_proposal(tmp_path, "trim", "applied")
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/../Proposals/trim.md") == ""
    # A symlinked proposal is not followed.
    outside = _write_proposal(tmp_path / "elsewhere", "real", "applied")
    (tmp_path / "Workspace" / "Proposals" / "linked.md").symlink_to(outside)
    assert care_report.read_proposal_status(tmp_path, "Workspace/Proposals/linked.md") == ""


def test_open_lines_are_open_steps() -> None:
    body = parse_care_report(INTERRUPTED)
    assert body["counts"] == {"total": 4, "done": 2, "blocked": 0, "open": 2}
    assert body["steps"][2] == {
        "id": "learnings", "state": "open", "what": "promote entries (3 at x3)", "evidence": "", "verify": "",
    }


def test_nothing_due_is_its_own_flag() -> None:
    body = parse_care_report("# Care report 2026-11-26\nWeekly done: 2026-11-20\nNothing due\n")
    assert body["nothing_due"] is True
    assert body["counts"]["total"] == 0
    assert body["malformed"] is False


@pytest.mark.parametrize("text", ["", "hello world\nno checklist here", "\x00\x01 binary", "- [?] odd: line"])
def test_unrecognised_text_is_malformed_not_an_error(text: str) -> None:
    body = parse_care_report(text)
    assert body["malformed"] is True
    assert body["steps"] == []


def test_ascii_separators_and_stray_lines_are_tolerated() -> None:
    body = parse_care_report(
        "2026-11-26\nsome prose\n* [X] Learnings: promote -- promoted 2; verify: active 7\n- [!] rules: digest\n"
    )
    assert body["updated"] == "2026-11-26"
    assert body["steps"][0]["id"] == "learnings"
    assert body["steps"][0]["evidence"] == "promoted 2"
    # A blocked line without a reason still reads as blocked, with nothing invented.
    assert body["steps"][1] == {"id": "rules", "state": "blocked", "what": "digest", "evidence": "", "verify": ""}


def test_counts_and_fields_are_bounded() -> None:
    lines = "\n".join(f"- [x] s{i}: {'w' * 2000} \u2014 e; verify: v" for i in range(200))
    needs = "\n".join(f"{i}. item {i}" for i in range(1, 100))
    body = parse_care_report(f"# Care report 2026-11-26\n{lines}\n## What needs you\n{needs}\n")
    assert len(body["steps"]) == care_report.MAX_STEPS
    assert len(body["steps"][0]["what"]) == care_report.MAX_FIELD_CHARS
    assert len(body["needs_you"]) == care_report.MAX_NEEDS
    notes = "\n".join(f"- note {i}" for i in range(100))
    assert len(parse_care_report(f"- [x] a: b\n## Notes\n{notes}\n")["notes"]) == care_report.MAX_NOTES


def _write_report(vault: Path, text: str) -> Path:
    path = vault / "Workspace" / "Care-Report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(params=["fd-relative", "by-path"])
def either_branch(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Run a read test through the POSIX descriptor path and the Windows path-based one."""
    monkeypatch.setattr(care_report, "_fd_relative_supported", lambda: request.param == "fd-relative")
    return str(request.param)


def test_a_section_title_before_the_steps_keeps_the_checklist() -> None:
    body = parse_care_report("## Care report 2026-11-27\nWeekly done: 2026-11-20\n- [x] unsure: a \u2014 b; verify: c\n- [!] hygiene: run \u2014 blocked: no index\n")
    assert body["updated"] == "2026-11-27"
    assert body["malformed"] is False
    assert [(s["id"], s["state"]) for s in body["steps"]] == [("unsure", "done"), ("hygiene", "blocked")]


def test_a_checklist_heading_before_the_steps_keeps_the_checklist() -> None:
    body = parse_care_report("# Care report 2026-11-27\n## Checklist\n- [!] hygiene: run \u2014 blocked: no index\n## Hygiene\n- [ ] dead_link: a\n")
    assert body["counts"] == {"total": 1, "done": 0, "blocked": 1, "open": 0}


def test_the_platform_switch_picks_the_path_based_read_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(care_report.sys, "platform", "win32")
    assert care_report._fd_relative_supported() is False


@pytest.mark.usefixtures("either_branch")
def test_missing_report_is_never_run(tmp_path: Path) -> None:
    body = read_care_report(tmp_path)
    assert body["status"] == "missing"
    assert body["steps"] == [] and body["modified_at"] == "" and body["malformed"] is False


@pytest.mark.usefixtures("either_branch")
def test_oversize_report_is_refused_without_parsing(tmp_path: Path) -> None:
    _write_report(tmp_path, "- [x] unsure: a \u2014 b\n" * (MAX_REPORT_BYTES // 10))
    body = read_care_report(tmp_path)
    assert body["status"] == "too_large"
    assert body["steps"] == []


@pytest.mark.usefixtures("either_branch")
def test_symlink_out_of_the_vault_is_not_followed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True)
    secret = tmp_path / "elsewhere.md"
    secret.write_text(COMPLETE, encoding="utf-8")
    (vault / "Workspace" / "Care-Report.md").symlink_to(secret)
    body = read_care_report(vault)
    assert body["status"] == "unreadable"
    assert body["steps"] == []


@pytest.mark.usefixtures("either_branch")
def test_a_directory_at_the_report_path_is_unreadable(tmp_path: Path) -> None:
    (tmp_path / "Workspace" / "Care-Report.md").mkdir(parents=True)
    assert read_care_report(tmp_path)["status"] == "unreadable"


@pytest.mark.usefixtures("either_branch")
def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    (tmp_path / "Workspace").mkdir()
    os.mkfifo(tmp_path / "Workspace" / "Care-Report.md")
    assert read_care_report(tmp_path)["status"] == "unreadable"


@pytest.mark.usefixtures("either_branch")
def test_a_symlinked_workspace_folder_is_not_followed(tmp_path: Path) -> None:
    """A swap of the folder for a link to elsewhere is refused at the open itself."""
    vault = tmp_path / "vault"
    vault.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _write_report(elsewhere, COMPLETE)
    (vault / "Workspace").symlink_to(elsewhere / "Workspace")
    assert read_care_report(vault)["status"] == "unreadable"


def test_a_symlink_swapped_in_after_a_check_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever is at the path when it is opened is what is judged: no check-then-open gap."""
    vault = tmp_path / "vault"
    report = _write_report(vault, COMPLETE)
    secret = tmp_path / "secret.md"
    secret.write_text(BLOCKED, encoding="utf-8")
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == "Care-Report.md" and report.is_file() and not report.is_symlink():
            report.unlink()
            report.symlink_to(secret)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(care_report.os, "open", swapping_open)
    assert read_care_report(vault)["status"] == "unreadable"


@pytest.mark.usefixtures("either_branch")
def test_a_hard_linked_report_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(COMPLETE, encoding="utf-8")
    (tmp_path / "Workspace").mkdir()
    os.link(elsewhere, tmp_path / "Workspace" / "Care-Report.md")
    assert read_care_report(tmp_path)["status"] == "unreadable"


def test_a_plain_report_reads_on_both_branches(tmp_path: Path, either_branch: str) -> None:
    _write_report(tmp_path, BLOCKED)
    body = read_care_report(tmp_path)
    assert body["status"] == "ok"
    assert body["counts"]["blocked"] == 2


def _fake_reparse(monkeypatch: pytest.MonkeyPatch, target: Path, tag: int) -> list[str]:
    """Give `target` the reparse-point attribute and `tag` in lstat, as Windows reports them."""
    real_lstat = os.lstat
    resolved = target.resolve()
    hits: list[str] = []

    def lstat(path):  # type: ignore[no-untyped-def]
        info = real_lstat(path)
        if Path(path) == resolved:
            hits.append(str(path))
            return SimpleNamespace(
                st_mode=info.st_mode, st_ino=info.st_ino, st_dev=info.st_dev,
                st_file_attributes=0x400, st_reparse_tag=tag,
            )
        return info

    monkeypatch.setattr(care_report.os, "lstat", lstat)
    return hits


@pytest.mark.parametrize("tag", [0xA000000C, 0xA0000003], ids=["symlink", "junction"])
@pytest.mark.parametrize("where", ["report", "folder"])
def test_the_path_branch_refuses_symlink_and_junction_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: int, where: str,
) -> None:
    monkeypatch.setattr(care_report, "_fd_relative_supported", lambda: False)
    report = _write_report(tmp_path, COMPLETE)
    hits = _fake_reparse(monkeypatch, report if where == "report" else report.parent, tag)
    assert read_care_report(tmp_path)["status"] == "unreadable"
    assert hits, "the reparse check never saw the faked entry"


@pytest.mark.parametrize("where", ["report", "folder"])
def test_the_path_branch_reads_a_onedrive_cloud_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str) -> None:
    """OneDrive Files On-Demand tags synced files and folders even when downloaded."""
    monkeypatch.setattr(care_report, "_fd_relative_supported", lambda: False)
    report = _write_report(tmp_path, COMPLETE)
    hits = _fake_reparse(monkeypatch, report if where == "report" else report.parent, 0x9000001A)
    body = read_care_report(tmp_path)
    assert hits
    assert body["status"] == "ok"
    assert body["counts"]["total"] == 2


def test_a_link_count_of_zero_is_tolerated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Some filesystems report st_nlink 0; only a count above one is a hard link."""
    _write_report(tmp_path, COMPLETE)
    real_fstat = os.fstat

    def fstat(fd):  # type: ignore[no-untyped-def]
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_nlink=0, st_dev=info.st_dev, st_ino=info.st_ino,
                               st_size=info.st_size, st_mtime=info.st_mtime)

    monkeypatch.setattr(care_report.os, "fstat", fstat)
    assert read_care_report(tmp_path)["status"] == "ok"


def test_the_path_branch_refuses_a_swap_between_check_and_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(care_report, "_fd_relative_supported", lambda: False)
    report = _write_report(tmp_path / "vault", COMPLETE)
    secret = tmp_path / "secret.md"
    secret.write_text(BLOCKED, encoding="utf-8")
    real_open = os.open

    def swapping_open(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path).endswith("Care-Report.md") and not report.is_symlink():
            report.unlink()
            report.symlink_to(secret)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(care_report.os, "open", swapping_open)
    assert read_care_report(tmp_path / "vault")["status"] == "unreadable"


def test_a_file_that_grows_past_the_cap_after_fstat_is_still_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_report(tmp_path, COMPLETE)
    monkeypatch.setattr(care_report.os, "read", lambda fd, n: b"x" * n)
    assert read_care_report(tmp_path)["status"] == "too_large"


@pytest.mark.usefixtures("either_branch")
def test_invalid_utf8_is_replaced_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "Workspace" / "Care-Report.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Care report 2026-11-26\n- [x] unsure: a \xe2\x80 \xff \xe2\x80\x94 ok; verify: v\n")
    body = read_care_report(tmp_path)
    assert body["status"] == "ok"
    assert body["steps"][0]["id"] == "unsure"


# The three items of a real run (2026-11-27) whose orchestrator copied the
# steps' old lines: no `(path: …)` tail, backticks, a step named.
VERBATIM = """# Care report 2026-11-27
- [x] regions: consolidate regions \u2014 memory 96% -> 94.8%; verify: memory.pct 94.8

## What needs you
1. Memory regions are still high (memory 94.8%, profile 92.4%) and every entry is a durable rule: choose in an attended chat what leaves.
2. Open lines in `Workspace/Unsure.md`: 17 (4 added by the stale step). Tick `- [x]` to apply.
3. Approve or reject `Workspace/Proposals/agents-md-trim-duplicates.md` and the other proposals filed today.
"""


def test_an_item_without_a_tail_takes_its_first_backticked_vault_path() -> None:
    needs = parse_care_report(VERBATIM)["needs_you"]
    # Item 1 names no file: no path, so the page offers only Fix with a chat. No keyword routing.
    assert [n["path"] for n in needs] == ["", "Workspace/Unsure.md", "Workspace/Proposals/agents-md-trim-duplicates.md"]
    assert needs[0]["title"].startswith("Memory regions are still high")
    # The page shows the text without the raw backticks.
    assert needs[1]["title"] == "Open lines in Workspace/Unsure.md: 17 (4 added by the stale step)."
    assert needs[1]["detail"] == "Tick - [x] to apply."
    assert all("`" not in n["title"] + n["detail"] for n in needs)


def test_verbatim_items_route_to_unsure_and_the_proposal(setup) -> None:
    _write_report(setup.vault, VERBATIM)
    _write_proposal(setup.vault, "agents-md-trim-duplicates", "draft")
    needs = setup.client.get("/api/care-report?workspace=personal").json()["needs_you"]
    assert [(n["path"], n["open_path"], n["proposal_status"]) for n in needs] == [
        ("", "", ""),
        ("Workspace/Unsure.md", "memory-vault/personal/Workspace/Unsure.md", ""),
        (
            "Workspace/Proposals/agents-md-trim-duplicates.md",
            "memory-vault/personal/Workspace/Proposals/agents-md-trim-duplicates.md",
            "draft",
        ),
    ]


@pytest.mark.parametrize(
    "span",
    [
        "../../etc/passwd", ".env", "/etc/passwd", "~/notes.md", "C:/Users/x.md", "Workspace\\Unsure.md",
        "Workspace/../Unsure.md", "Workspace/.care-drained", "People/Rae\u202egnp.md", "Workspace//Unsure.md",
        # Not a location: a bare file name, a command, a marker, a field name.
        "Unsure.md", "ciao memory status", "- [x]", "updated:", "Workspace/Unsure",
    ],
)
def test_a_backticked_span_that_is_not_a_safe_vault_file_gives_no_path(span: str) -> None:
    item = parse_care_report(f"## What needs you\n1. Look at `{span}` today.\n")["needs_you"][0]
    assert item["path"] == ""
    assert care_report.backticked_path(f"see `{span}`") == ""


def test_backticked_fallback_picks_the_first_valid_path_and_never_bare_words() -> None:
    long = "After a long run that read " + "many notes and " * 20 + "`People/Rae Smith.md` in the middle, decide."
    assert care_report.backticked_path(long) == "People/Rae Smith.md"
    assert care_report.backticked_path("`../x.md` or `.env` or `Workspace/Unsure.md` or `AGENTS.md`") == "Workspace/Unsure.md"
    assert care_report.backticked_path("`AGENTS.md` then `Workspace/Unsure.md`") == "AGENTS.md"
    assert care_report.backticked_path("`/etc/passwd` and `../../.ssh/id_rsa`") == ""
    # Unquoted words are never a path.
    assert care_report.backticked_path("Open Workspace/Unsure.md and AGENTS.md") == ""
    # An explicit tail wins, even an unsafe one: the fallback is only for a missing tail.
    item = parse_care_report("## What needs you\n1. See `Workspace/Unsure.md`. (path: ../outside.md)\n")["needs_you"][0]
    assert item["path"] == ""


def test_a_backticked_proposal_is_read_with_the_same_confinement(tmp_path: Path) -> None:
    _write_proposal(tmp_path, "trim", "applied")
    outside = _write_proposal(tmp_path / "elsewhere", "real", "applied")
    (tmp_path / "Workspace" / "Proposals" / "linked.md").symlink_to(outside)
    _write_report(
        tmp_path,
        "# Care report 2026-11-27\n- [x] a: b \u2014 c; verify: d\n## What needs you\n"
        "1. Review `Workspace/Proposals/trim.md`.\n"
        "2. Review `Workspace/Proposals/linked.md`.\n"
        "3. Review `Workspace/Proposals/../Proposals/trim.md`.\n",
    )
    needs = read_care_report(tmp_path)["needs_you"]
    assert [(n["path"], n["proposal_status"]) for n in needs] == [
        ("Workspace/Proposals/trim.md", "applied"),
        ("Workspace/Proposals/linked.md", ""),
        ("", ""),
    ]


def test_step_files_match_the_care_prompt() -> None:
    """The report page links each step to the file the prompt dispatches it with."""
    schedules = json.loads((Path(care_report.__file__).parent / "stock" / "schedules.json").read_text())
    prompt = next(s["prompt"] for s in schedules["schedules"] if s["schedule_id"] == "system-memory-curation")
    files_line = next(line for line in prompt.splitlines() if line.startswith("Files: "))
    pairs = dict(re.findall(r"([a-z-]+) `([a-z0-9-]+\.md)`", files_line))
    assert pairs == STEP_FILES


def test_the_prompt_retries_only_the_blocked_weekly_steps() -> None:
    schedules = json.loads((Path(care_report.__file__).parent / "stock" / "schedules.json").read_text())
    prompt = next(s["prompt"] for s in schedules["schedules"] if s["schedule_id"] == "system-memory-curation")
    assert "`Weekly retry: <old value>` when present" in prompt
    assert "each id in the old `Weekly retry:` line gets its weekly line again, and no other weekly step" in prompt
    assert "3 runs" in prompt


def test_checkboxes_after_the_first_section_are_not_steps() -> None:
    body = parse_care_report(
        COMPLETE + "\n## Hygiene\n- index rebuilt; audit exit 1\n- [ ] dead_link: Projects/A.md -> People/B.md\n- [x] other: thing\n"
    )
    assert [s["id"] for s in body["steps"]] == ["unsure", "regions"]
    assert body["counts"]["open"] == 0


@pytest.mark.parametrize("heading", ["## What needs you", "### What needs you:", "What needs you", "**What needs you**", "**What needs you:**"])
@pytest.mark.parametrize("marker", ["1.", "2)", "-", "*"])
def test_needs_you_accepts_heading_and_list_variants(heading: str, marker: str) -> None:
    body = parse_care_report(
        f"# Care report 2026-11-26\n- [x] unsure: a \u2014 b; verify: c\n\n{heading}\n{marker} New line in Unsure: check it\n"
        f"{marker} Memory guide at 91%: pick\n\n## Notes\n- not an item\n"
    )
    assert [n["title"] for n in body["needs_you"]] == ["New line in Unsure: check it", "Memory guide at 91%: pick"]
    assert body["notes"] == ["not an item"]


def test_weekly_retry_is_read_and_bounded() -> None:
    body = parse_care_report("# Care report 2026-11-26\nWeekly retry: hygiene=2, `guide-review`=1, junk, x=\n- [!] hygiene: run \u2014 blocked: no\n")
    assert body["weekly_retry"] == [{"id": "hygiene", "runs": 2}, {"id": "guide-review", "runs": 1}]
    assert parse_care_report(COMPLETE)["weekly_retry"] == []


@pytest.mark.parametrize("raw", ["none", "None.", "n/a", "-", "\u2014", "empty", "`none`"])
def test_interrupted_placeholders_are_not_ids(raw: str) -> None:
    body = parse_care_report(f"# Care report 2026-11-26\nInterrupted last run: {raw}\n- [x] unsure: a \u2014 b\n")
    assert body["interrupted_ids"] == []
    assert parse_care_report("Interrupted last run: stale, none\n- [x] a: b")["interrupted_ids"] == ["stale"]


# ── GET /api/care-report ───────────────────────────────────────────────────


class _Schedules:
    """The schedule manager's read the route uses, with one care entry."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries
        self.asked: list[str] = []

    def get_with_revision(self, schedule_id: str):
        self.asked.append(schedule_id)
        entry = self.entries.get(schedule_id)
        return (entry, "rev") if entry is not None else None


def _entry(status: str = "ok", dispatched: str = "2026-11-26T06:00:00+00:00", enabled: bool = True):
    return SimpleNamespace(
        schedule_id="system-memory-curation@personal",
        enabled=enabled,
        last_status=status,
        last_dispatched_at=dispatched,
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
    schedules = _Schedules({})
    app.state.schedule_manager = schedules
    client = signed_in(TestClient(app))
    return SimpleNamespace(client=client, vault=tmp_path / "memory-vault" / "personal", schedules=schedules, root=tmp_path)


def test_route_reads_the_active_workspace_report(setup) -> None:
    _write_report(setup.vault, BLOCKED)
    _write_report(setup.root / "memory-vault" / "work", COMPLETE)
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["status"] == "ok"
    assert body["workspace"] == "personal"
    assert body["counts"]["blocked"] == 2
    assert body["report_path"] == "memory-vault/personal/Workspace/Care-Report.md"
    assert body["unsure_path"] == "memory-vault/personal/Workspace/Unsure.md"
    assert body["modified_at"].endswith("Z")
    hygiene = body["steps"][1]
    assert hygiene["step_file"].endswith("/references/curation-pass-6-hygiene.md")
    assert Path(hygiene["step_file"]).is_file()
    assert body["schedule"] is None and body["running"] is None
    assert setup.schedules.asked == ["system-memory-curation@personal"]


def test_route_gives_each_item_its_file_and_proposal_status(setup) -> None:
    _write_report(setup.vault, STANDARD)
    _write_proposal(setup.vault, "guide-trim-duplicates", "rejected")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    needs = body["needs_you"]
    assert [n["open_path"] for n in needs] == [
        "memory-vault/personal/Workspace/Proposals/guide-trim-duplicates.md",
        "",
        "memory-vault/personal/Workspace/Unsure.md",
        "memory-vault/personal/Workspace/Care-Report.md",
    ]
    assert [n["proposal_status"] for n in needs] == ["rejected", "", "", ""]
    assert body["notes"][0].startswith("Re-checking older notes")


def test_the_example_report_follows_the_needs_you_standard() -> None:
    """The worked example the care run copies: every item carries a path, leftovers are notes."""
    example = (
        Path(care_report.__file__).parent / "stock" / "skills" / "ciao-capabilities" / "references"
        / "memory-operations" / "references" / "curation-report-example.md"
    ).read_text(encoding="utf-8")
    report = example.split("```\n", 2)[1]
    body = parse_care_report(report)
    assert body["needs_you"] and all(n["path"] for n in body["needs_you"])
    assert any(care_report.is_proposal_path(n["path"]) for n in body["needs_you"])
    assert body["notes"]
    for item in body["needs_you"]:
        text = item["title"] + " " + item["detail"]
        assert len(re.findall(r"[.?!](?:\s|$)", text)) <= 2, text
        for jargon in ("frontmatter", "updated:", "verify", "audit", "retry", "[!]", "- [x]"):
            assert jargon not in text, (jargon, text)


def test_route_says_missing_for_a_workspace_care_never_ran_in(setup) -> None:
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["status"] == "missing"
    assert body["schedule"] == {
        "id": "system-memory-curation@personal", "enabled": True, "last_status": "", "last_run_at": "2026-11-26T06:00:00+00:00",
    }


@pytest.mark.parametrize("query", ["", "?workspace=", "?workspace=nope", "?workspace=../personal"])
def test_route_refuses_an_unknown_workspace(setup, query: str) -> None:
    response = setup.client.get(f"/api/care-report{query}")
    assert response.status_code == 400


def test_route_is_read_only(setup) -> None:
    path = _write_report(setup.vault, INTERRUPTED)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert setup.client.get("/api/care-report?workspace=personal").status_code == 200
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert setup.client.post("/api/care-report?workspace=personal").status_code == 405


def test_running_names_the_open_step_of_a_report_written_after_dispatch(setup) -> None:
    path = _write_report(setup.vault, INTERRUPTED)
    stamp = datetime(2026, 11, 26, 6, 5, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="running", dispatched="2026-11-26T06:00:00+00:00")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["running"] == {"since": "2026-11-26T06:00:00+00:00", "step": "learnings", "index": 3, "total": 4}


def test_running_ignores_the_previous_runs_report(setup) -> None:
    """Before the new run rewrites the checklist, the open lines on disk are the old run's."""
    path = _write_report(setup.vault, INTERRUPTED)
    stamp = datetime(2026, 11, 25, 6, 5, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    setup.schedules.entries["system-memory-curation@personal"] = _entry(status="running", dispatched="2026-11-26T06:00:00+00:00")
    body = setup.client.get("/api/care-report?workspace=personal").json()
    assert body["running"] == {"since": "2026-11-26T06:00:00+00:00", "step": "", "index": 0, "total": 0}


def test_route_does_not_leak_the_vault_path_when_it_is_unavailable(setup, monkeypatch, caplog) -> None:
    config = setup.client.app.state.config

    def broken(workspace):  # type: ignore[no-untyped-def]
        raise OSError(f"cannot reach {setup.root}/secret/vault")

    monkeypatch.setattr(type(config), "workspace_vault_root", lambda self, ws: broken(ws))
    response = setup.client.get("/api/care-report?workspace=personal")
    assert response.status_code == 409
    assert response.json() == {"error": "vault unavailable"}
    assert str(setup.root) not in response.text
    assert "secret/vault" in caplog.text


def test_route_needs_a_session(setup) -> None:
    anonymous = TestClient(setup.client.app)
    assert anonymous.get("/api/care-report?workspace=personal").status_code in (401, 403)


# ── Run now: one care run at a time ───────────────────────────────────────


def _run_now_client(entry: Any) -> tuple[TestClient, list[str]]:
    from starlette.applications import Starlette
    from starlette.routing import Route

    from ciao.web.routes_api import run_schedule_now

    dispatched: list[str] = []

    class _Manager:
        _store = SimpleNamespace(get=lambda schedule_id: entry)

        async def dispatch_now(self, schedule_id: str) -> dict[str, Any]:
            dispatched.append(schedule_id)
            return {"schedule_id": schedule_id, "status": "ok"}

    app = Starlette(routes=[Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"])])
    app.state.schedule_manager = _Manager()
    app.state.project_chat_manager = SimpleNamespace(schedule_target_refusal=lambda e: "")
    return TestClient(app), dispatched


def test_care_run_now_refuses_while_a_run_is_in_progress(monkeypatch) -> None:
    monkeypatch.setattr("ciao.web.routes_api.publish_automations_changed", lambda pcm: None)
    client, dispatched = _run_now_client(_entry(status="running"))
    response = client.post("/api/schedule-run/system-memory-curation@personal")
    assert response.status_code == 409
    assert response.json() == {"error": "already running"}
    assert dispatched == []


def test_care_run_now_dispatches_when_idle(monkeypatch) -> None:
    monkeypatch.setattr("ciao.web.routes_api.publish_automations_changed", lambda pcm: None)
    client, dispatched = _run_now_client(_entry(status="ok"))
    assert client.post("/api/schedule-run/system-memory-curation@personal").status_code == 201
    assert dispatched == ["system-memory-curation@personal"]


def test_other_schedules_keep_their_run_now_behaviour(monkeypatch) -> None:
    monkeypatch.setattr("ciao.web.routes_api.publish_automations_changed", lambda pcm: None)
    entry = SimpleNamespace(schedule_id="briefing", last_status="running")
    client, dispatched = _run_now_client(entry)
    assert client.post("/api/schedule-run/briefing").status_code == 201
    assert dispatched == ["briefing"]
