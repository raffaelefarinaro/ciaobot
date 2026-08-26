"""Tests for ``ciao.memory_audit`` bounded-memory rot detection."""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path
from typing import Any

from ciao.memory_audit import (
    audit_entries,
    find_event_shaped,
    find_stale_notes,
    find_stale_paths,
    find_superseded_state,
    parse_verified_date,
)


# --- event-shaped entries ---------------------------------------------------


def test_event_shaped_flags_quoted_user_turn() -> None:
    findings = find_event_shaped(
        "memory",
        ['User said: "Do it yes" -> assistant bumped timeout_s from 120.0 to 300.0.'],
    )

    assert len(findings) == 1
    assert findings[0]["region"] == "memory"
    assert "quoted-user-turn" in findings[0]["markers"]
    assert "assistant-action" in findings[0]["markers"]


def test_event_shaped_flags_transcript_citation() -> None:
    findings = find_event_shaped(
        "memory", ["Trajectories are 401/415 Claude sessions. [idx=56,77]"]
    )

    assert [f["markers"] for f in findings] == [["transcript-citation"]]


def test_event_shaped_flags_explicit_correction_language() -> None:
    findings = find_event_shaped(
        "memory",
        [
            "The user requested terse output; the assistant reformatted the answer.",
            "The user pushed back on the draft, and the assistant rewrote it.",
        ],
    )

    assert len(findings) == 2
    assert all("quoted-user-turn" in finding["markers"] for finding in findings)
    assert all("assistant-action" in finding["markers"] for finding in findings)


def test_event_shaped_flags_em_dash_assistant_correction() -> None:
    findings = find_event_shaped(
        "memory", ["Re the stale docstring — assistant confirmed it is an example."]
    )

    assert len(findings) == 1


def test_event_shaped_ignores_em_dash_as_prose_punctuation() -> None:
    """An em dash before "assistant" is punctuation, not an event arrow.

    The arrow alternation used to accept `—` and `--`, so ordinary durable
    state matched and the audit sat at needs_attention with nothing the user
    could correct. A real event written with an em dash still matches, via the
    verb pattern — see the test above.
    """
    findings = find_event_shaped(
        "memory",
        [
            "Prefers terse replies — assistant should skip preamble.",
            "Tone: direct -- assistant avoids hedging.",
        ],
    )

    assert findings == []


def test_event_shaped_ignores_durable_state() -> None:
    """Precision guard: a fact *about* the user is state, not a chat event."""
    findings = find_event_shaped(
        "memory",
        [
            "User prefers concise answers and no em-dashes.",
            "The user runs macOS 27 with Homebrew at /opt/homebrew.",
            "Raffa is a PM at Scandit and owns SparkScan.",
            "Assistant behaviour is configured through skills, not prompts.",
            "The assistant uses concise language for code reviews.",
        ],
    )

    assert findings == []


# --- stale paths ------------------------------------------------------------


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "memory-vault" / "personal").mkdir(parents=True)
    (tmp_path / "memory-vault" / "personal" / "MEMORY.md").write_text("x", "utf-8")
    return tmp_path


def test_stale_paths_flags_missing_file_under_existing_dir(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, unverifiable = find_stale_paths(
        "memory",
        ["Durable notes live in `memory-vault/personal/Gone.md` these days."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert unverifiable == 0
    assert len(findings) == 1
    assert findings[0]["path"] == "memory-vault/personal/Gone.md"
    assert "does not exist" in findings[0]["message"]


def test_stale_paths_keeps_leading_dots(tmp_path: Path) -> None:
    """`./`, `../` and dotfile paths must survive punctuation trimming.

    Trimming used `str.strip(_PATH_TRAILING)`, which trims *both* ends over a
    set containing `.`. So `./x` became `/x` — absolute, resolving outside the
    workspace, and written off as unverifiable — and `.claude/x` became
    `claude/x`, which is not path-shaped and was dropped entirely. Both made
    the detector silently stop checking the paths it exists to check.
    """
    workspace = _workspace(tmp_path)
    (workspace / ".claude").mkdir()

    findings, checked, unverifiable = find_stale_paths(
        "memory",
        [
            "Run `./memory-vault/personal/gone.sh` first.",
            "Settings live in `.claude/absent.json`.",
        ],
        workspace_dir=workspace,
    )

    assert unverifiable == 0
    assert checked == 2
    assert {f["path"] for f in findings} == {
        "./memory-vault/personal/gone.sh",
        ".claude/absent.json",
    }


def test_stale_paths_still_trims_trailing_sentence_punctuation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["Notes moved to memory-vault/personal/Gone.md."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert findings[0]["path"] == "memory-vault/personal/Gone.md"


def test_stale_paths_accepts_existing_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["Durable notes live in `memory-vault/personal/MEMORY.md`."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert findings == []


def test_stale_paths_strips_line_number_reference(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["The stale docstring is at memory-vault/personal/absent.py:9 still."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert findings[0]["path"] == "memory-vault/personal/absent.py:9"


def test_stale_paths_flags_missing_absolute_path_inside_workspace(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        [f"Config sits at `{workspace}/secrets/absent.json` now."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert len(findings) == 1


def test_stale_paths_leaves_foreign_absolute_path_unverified(tmp_path: Path) -> None:
    """A path on another machine is not evidence of rot, so do not claim it is."""
    workspace = _workspace(tmp_path)

    findings, checked, unverifiable = find_stale_paths(
        "memory",
        ["The old box mounted /mnt/nonexistent-xyz/data/notes.md for this."],
        workspace_dir=workspace,
    )

    assert findings == []
    assert checked == 0
    assert unverifiable == 1


def test_stale_paths_ignores_prose_and_placeholders(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, unverifiable = find_stale_paths(
        "memory",
        [
            "Route each fact by scope and/or by owner, 24/7, state/event either way.",
            "Inline comments need `gh api repos/<owner>/<repo>/pulls/<n>/comments`.",
            "Wrappers live under `some-vault/scripts/*.py` as globs.",
        ],
        workspace_dir=workspace,
    )

    assert findings == []
    assert checked == 0
    assert unverifiable == 0


def test_stale_paths_ignores_cross_repo_reference(tmp_path: Path) -> None:
    """Regression: the engine lives in a sibling repo.

    ``ciao/cli.py`` is a correct reference from the vault repo, where no
    ``ciao/`` directory exists. Flagging it would push curation to rewrite a
    true entry.
    """
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["The real implementation lives in `ciao/cli.py` and `ciao/job_runs.py`."],
        workspace_dir=workspace,
    )

    assert findings == []
    assert checked == 0


def test_stale_paths_flags_missing_home_relative_path(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["Sessions are under `~/.claude/projects/-home-ubuntu-absent-xyz/` there."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert len(findings) == 1


def test_stale_paths_ignores_urls(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, unverifiable = find_stale_paths(
        "memory",
        ["Open issues at https://github.com/raffaelefarinaro/ciaobot/issues/new."],
        workspace_dir=workspace,
    )

    assert findings == []
    assert checked == 0
    assert unverifiable == 0


# --- superseded state -------------------------------------------------------


def test_superseded_state_groups_entries_sharing_a_subject(tmp_path: Path) -> None:
    findings = find_superseded_state(
        "memory",
        [
            "Set `ollama_haiku_model` to deepseek-v4-flash:0731-cloud.",
            "Changed `ollama_haiku_model` to the bare slug deepseek-v4-flash:cloud.",
            "Unrelated: the GWS wrapper must never be sourced.",
        ],
        workspace_dir=tmp_path,
    )

    assert len(findings) == 1
    assert findings[0]["subject"] == "ollama_haiku_model"
    assert len(findings[0]["entries"]) == 2


def test_superseded_state_quiet_for_distinct_subjects(tmp_path: Path) -> None:
    findings = find_superseded_state(
        "memory",
        [
            "Set `ollama_haiku_model` to deepseek-v4-flash:cloud.",
            "Set `timeout_s` to 300.0.",
        ],
        workspace_dir=tmp_path,
    )

    assert findings == []


def test_superseded_state_ignores_generic_subjects(tmp_path: Path) -> None:
    """Two entries both saying "vault" are not two values for one subject."""
    findings = find_superseded_state(
        "memory",
        [
            "The `vault` is the durable memory layer.",
            "Search the `vault` before writing duplicates.",
        ],
        workspace_dir=tmp_path,
    )

    assert findings == []


# --- combined ---------------------------------------------------------------


def test_audit_entries_reports_every_region(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    report = audit_entries(
        {
            "memory": ["User said: \"switch that\" -> assistant changed the slug."],
            "profile": ["Raffa writes without em-dashes."],
        },
        workspace_dir=workspace,
    )

    assert len(report["event_shaped_entries"]) == 1
    assert report["event_shaped_entries"][0]["region"] == "memory"
    assert report["stale_path_entries"] == []
    assert report["superseded_state_candidates"] == []
    assert report["paths_checked"] == 0
    assert report["paths_unverifiable"] == 0


def test_audit_entries_handles_empty_regions(tmp_path: Path) -> None:
    report = audit_entries({"memory": [], "profile": []}, workspace_dir=tmp_path)

    assert report["event_shaped_entries"] == []
    assert report["stale_path_entries"] == []
    assert report["superseded_state_candidates"] == []


# --- vault-note aging -------------------------------------------------------


def _entry(path: str, note_type: str, updated: str = "", title: str = "") -> Any:
    from ciao.vault_index import Entry

    return Entry(
        path=Path(path), title=title or Path(path).stem, type=note_type, updated=updated
    )


def _days_ago(days: int, *, today: datetime.date) -> float:
    stamp = today - datetime.timedelta(days=days)
    return datetime.datetime(
        stamp.year, stamp.month, stamp.day, tzinfo=datetime.timezone.utc
    ).timestamp()


def test_stale_notes_flags_old_mtime_person_note() -> None:
    today = datetime.date(2026, 8, 23)
    report = find_stale_notes(
        [_entry("memory-vault/People/Mo.md", "person")],
        mtimes={"memory-vault/People/Mo.md": _days_ago(200, today=today)},
        today=today,
    )

    assert len(report["stale_notes"]) == 1
    finding = report["stale_notes"][0]
    assert finding["path"] == "memory-vault/People/Mo.md"
    assert finding["age_days"] == 200
    assert finding["threshold_days"] == 90
    assert finding["source"] == "mtime"
    assert report["notes_checked"] == 1


def test_stale_notes_frontmatter_updated_overrides_mtime() -> None:
    """`updated:` is a claim that the facts were re-checked; mtime is not."""
    today = datetime.date(2026, 8, 23)
    # Recently written but never verified: still stale.
    recent_file_old_facts = find_stale_notes(
        [_entry("memory-vault/People/Mo.md", "person", updated="2025-01-01")],
        mtimes={"memory-vault/People/Mo.md": _days_ago(2, today=today)},
        today=today,
    )
    assert [f["source"] for f in recent_file_old_facts["stale_notes"]] == ["frontmatter"]

    # Old file that was explicitly re-verified yesterday: not stale.
    old_file_recent_check = find_stale_notes(
        [_entry("memory-vault/People/Mo.md", "person", updated="2026-08-22")],
        mtimes={"memory-vault/People/Mo.md": _days_ago(400, today=today)},
        today=today,
    )
    assert old_file_recent_check["stale_notes"] == []


def test_stale_notes_exempts_event_and_queue_types() -> None:
    """Logs record events — age cannot rot them. Workspace queue files are
    lifecycle-managed by curation; flagging an inbox for being an inbox is
    noise."""
    today = datetime.date(2026, 8, 23)
    old = _days_ago(400, today=today)
    entries = [
        _entry("memory-vault/Logs/Chats/x.md", "log"),
        _entry("memory-vault/Journal/day.md", "journal"),
        _entry("memory-vault/Workspace/Learnings.md", "workspace"),
    ]

    report = find_stale_notes(entries, mtimes={str(e.path): old for e in entries}, today=today)

    assert report["stale_notes"] == []
    assert report["notes_checked"] == 0
    assert report["notes_exempt"] == 3


def test_stale_notes_thresholds_vary_by_type() -> None:
    """An active project rots in weeks; a plain note has months."""
    today = datetime.date(2026, 8, 23)
    mtime = _days_ago(40, today=today)
    entries = [
        _entry("memory-vault/projects/active/alpha.md", "project"),
        _entry("memory-vault/Idea/shower-thought.md", "idea"),
    ]

    report = find_stale_notes(entries, mtimes={str(e.path): mtime for e in entries}, today=today)

    flagged = {f["type"]: f for f in report["stale_notes"]}
    assert set(flagged) == {"project"}
    assert flagged["project"]["threshold_days"] == 30
    assert report["notes_checked"] == 2


def test_stale_notes_skips_notes_without_usable_dates() -> None:
    """Unverifiable is not stale — calling it so would be a guess."""
    report = find_stale_notes(
        [
            _entry("memory-vault/People/Ghost.md", "person"),
            _entry("memory-vault/People/Bad.md", "person", updated="2025-13-99"),
            _entry("memory-vault/People/Old.md", "person", updated="not-a-date"),
        ],
        mtimes={
            "memory-vault/People/Ghost.md": 0.0,
            "memory-vault/People/Bad.md": 0.0,
            "memory-vault/People/Old.md": 0.0,
        },
    )

    assert report["stale_notes"] == []
    assert report["notes_checked"] == 0


def test_stale_notes_stats_files_when_no_mtime_map(tmp_path: Path) -> None:
    from ciao.vault_index import scan_vault

    vault = tmp_path / "memory-vault"
    (vault / "People").mkdir(parents=True)
    (vault / "People" / "Mo.md").write_text(
        "---\ntype: person\n---\n# Mo\n", encoding="utf-8"
    )
    old = time.time() - 200 * 86400
    os.utime(vault / "People" / "Mo.md", (old, old))

    entries = scan_vault(vault, workspace="personal")
    report = find_stale_notes(entries, vault_root=vault)

    assert len(report["stale_notes"]) == 1
    assert report["stale_notes"][0]["age_days"] >= 199


def test_parse_verified_date_accepts_only_iso_dates() -> None:
    assert parse_verified_date("2026-08-23") == datetime.date(2026, 8, 23)
    assert parse_verified_date(" 2026-08-23 ") == datetime.date(2026, 8, 23)
    assert parse_verified_date("2026-13-01") is None
    assert parse_verified_date("August 2026") is None
    assert parse_verified_date("") is None


# --- CLI output -------------------------------------------------------------


def test_memory_audit_command_tells_the_user_how_to_fix_over_cap(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    """Over-cap findings must name the region and the actions that shrink it.

    The nightly curator may not edit regions, so its report is the only thing
    the user sees; "over cap" without a fix reads as a dead end.
    """
    import argparse

    from ciao.cli import _memory_audit_command
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    write_region(
        guide,
        "memory",
        [f"Durable lesson {index}: " + "x" * 380 for index in range(6)],
    )

    # _memory_audit_command reads the whole process environment, and several
    # code paths under test elsewhere (_load_env_file, from_env()'s dotenv
    # load, the setup wizard) write os.environ directly, so leaks survive
    # their tests. Anything that redirects the audit — a runtime root holding
    # a workspaces.json, raised caps — flips this test's outcome, so scrub
    # every variable it consumes instead of just the workspace trio.
    for name in (
        "CIAO_WORKSPACES",
        "CIAO_VAULT_ROOT",
        "CIAO_WORKSPACE",
        "CIAO_VAULT_MODE",
        "CIAO_RUNTIME_ROOT",
        "CIAO_MEMORY_CHAR_LIMIT",
        "CIAO_USER_CHAR_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = _memory_audit_command(
        argparse.Namespace(
            workspace=tmp_path,
            vault_root=tmp_path / "memory-vault",
            json=False,
            with_vault=False,
        )
    )

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "ciao:memory over cap: " in out
    assert 'consolidate the region (e.g. "consolidate my ciao:memory' in out
    assert "CIAO_MEMORY_CHAR_LIMIT / CIAO_USER_CHAR_LIMIT in .env" in out


def test_memory_audit_command_stays_quiet_when_under_cap(
    tmp_path: Path,
    capsys: Any,
) -> None:
    """No over-cap findings, no fix hint: the hint must track the finding."""
    import argparse

    from ciao.cli import _memory_audit_command
    from ciao.memory_tool import ensure_regions, write_region

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    write_region(guide, "memory", ["one small durable lesson"])

    exit_code = _memory_audit_command(
        argparse.Namespace(
            workspace=tmp_path,
            vault_root=tmp_path / "memory-vault",
            json=False,
            with_vault=False,
        )
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "over cap" not in out
    assert "consolidate" not in out
