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
            "The user is a PM at Acme and owns the Falcon project.",
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


def test_stale_paths_checks_a_workspace_path_that_holds_a_space(tmp_path: Path) -> None:
    # Every "C:\Users\First Last" profile puts a space in the workspace path.
    workspace = _workspace(tmp_path / "First Last")

    findings, checked, _ = find_stale_paths(
        "memory",
        [f"Config sits at `{workspace}/secrets/absent.json` now."],
        workspace_dir=workspace,
    )

    assert checked == 1
    assert findings[0]["path"] == f"{workspace}/secrets/absent.json"


def test_stale_paths_still_skips_a_backticked_command_line(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    findings, checked, _ = find_stale_paths(
        "memory",
        ["Run `cat ~/notes/absent.md --plain` to see it."],
        workspace_dir=workspace,
    )

    assert (findings, checked) == ([], 0)


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
            "profile": ["The user writes without em-dashes."],
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

    report = find_stale_notes(entries, mtimes={e.path_key: old for e in entries}, today=today)

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

    report = find_stale_notes(entries, mtimes={e.path_key: mtime for e in entries}, today=today)

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

    The report is the only surface many users see; "over cap" without a fix
    reads as a dead end.
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
        [f"Durable lesson {index}: " + "x" * 500 for index in range(7)],
    )

    # _memory_audit_command reads the whole process environment, and several
    # code paths under test elsewhere (_load_env_file, from_env()'s dotenv
    # load, the setup wizard) write os.environ directly, so leaks survive
    # their tests. Anything that redirects the audit — a runtime root holding
    # a workspaces.json — flips this test's outcome, so scrub
    # every variable it consumes instead of just the workspace trio.
    for name in (
        "CIAO_VAULT_ROOT",
        "CIAO_WORKSPACE",
        "CIAO_VAULT_MODE",
        "CIAO_RUNTIME_ROOT",
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
    assert "CIAO_MEMORY_CHAR_LIMIT" not in out


def test_memory_audit_with_vault_marks_retrieved_stale_notes(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    """Decay-by-disuse: a stale note in the hits log reads retrieved_recently.

    The hits are stored in the index's key space (workspace-relative, real
    vault directory name); the audit must rebuild that key from the rendered
    finding path rather than suffix-matching, and mark nothing when no hit
    carries this vault's prefix.
    """
    import argparse
    import json as jsonlib
    from datetime import datetime, timezone

    from ciao.cli import _memory_audit_command
    from ciao.memory_tool import ensure_regions

    for name in (
        "CIAO_VAULT_ROOT",
        "CIAO_WORKSPACE",
        "CIAO_VAULT_MODE",
        "CIAO_RUNTIME_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    vault = tmp_path / "memory-vault"
    people = vault / "People"
    people.mkdir(parents=True)
    stale_note = "---\ntype: person\nupdated: 2024-01-01\n---\n# {n}\nOld facts.\n"
    (people / "Retrieved.md").write_text(stale_note.format(n="Retrieved"), encoding="utf-8")
    (people / "Forgotten.md").write_text(stale_note.format(n="Forgotten"), encoding="utf-8")

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "q": "retrieved",
        "paths": ["memory-vault/People/Retrieved.md"],
    }
    (runtime / "vault_search_hits.jsonl").write_text(
        jsonlib.dumps(record) + "\n", encoding="utf-8"
    )

    _memory_audit_command(
        argparse.Namespace(
            workspace=tmp_path, vault_root=vault, json=True, with_vault=True
        )
    )

    report = jsonlib.loads(capsys.readouterr().out)
    by_path = {
        finding["path"]: finding
        for finding in report["stale_notes"]["stale_notes"]
    }
    assert by_path["memory-vault/People/Retrieved.md"]["retrieved_recently"] is True
    assert by_path["memory-vault/People/Forgotten.md"]["retrieved_recently"] is False


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


# ---- Temporal validity -------------------------------------------------


def test_find_aging_state_flags_old_as_of_and_learned_stamps() -> None:
    import datetime as _dt

    from ciao.memory_audit import find_aging_state

    today = _dt.date(2026, 8, 29)
    entries = [
        "Quota data unavailable [as-of: 2026-04-01].",  # 150d: aged
        "New pricing applies [as-of: 2026-08-01].",  # 28d: fresh
        "Prefers terse replies. [2026-01-01]",  # learned 240d ago: aged
        "Prefers tables for stats. [2026-08-01]",  # learned 28d ago: fresh
        "Undated standing rule with no stamps.",
        "Temporary flag [expires: 2026-12-01].",  # expiry is os-audit's job
    ]
    findings = find_aging_state("memory", entries, today=today)
    kinds = {(f["kind"], f["date"]) for f in findings}
    assert kinds == {("as-of", "2026-04-01"), ("learned", "2026-01-01")}
    for finding in findings:
        assert finding["age_days"] >= finding["threshold_days"]


def test_find_aging_state_skips_impossible_dates() -> None:
    from ciao.memory_audit import find_aging_state

    assert find_aging_state("memory", ["Fact [as-of: 2026-02-30]."]) == []


def test_strip_learned_stamp_round_trip() -> None:
    from ciao.memory_audit import strip_learned_stamp

    assert strip_learned_stamp("Rule text. [2026-08-29]") == "Rule text."
    assert strip_learned_stamp("Rule text.") == "Rule text."
    # An as-of tag is world time, not a learned stamp; it stays.
    assert strip_learned_stamp("Fact [as-of: 2026-04-01].") == "Fact [as-of: 2026-04-01]."


def test_audit_entries_reports_aging_state(tmp_path) -> None:
    import datetime as _dt

    from ciao.memory_audit import audit_entries

    report = audit_entries(
        {"memory": ["Old snapshot [as-of: 2025-01-01]."], "profile": []},
        workspace_dir=tmp_path,
        today=_dt.date(2026, 8, 29),
    )
    assert len(report["aging_state_entries"]) == 1
    assert report["aging_state_entries"][0]["kind"] == "as-of"


# --- shared staleness predicate ---------------------------------------------


def test_note_verification_prefers_frontmatter_and_reports_horizon() -> None:
    from ciao.memory_audit import note_verification

    today = datetime.date(2026, 9, 25)
    result = note_verification(
        "person", "2026-06-01", _days_ago(1, today=today), today=today
    )

    assert result is not None
    assert result.source == "frontmatter"
    assert result.age_days == 116
    assert result.threshold_days == 90
    assert result.stale is True
    assert result.as_evidence() == {
        "age_days": 116,
        "threshold_days": 90,
        "last_verified": "2026-06-01",
        "source": "frontmatter",
    }


def test_note_verification_falls_back_to_mtime_and_clamps_future_dates() -> None:
    from ciao.memory_audit import note_verification

    today = datetime.date(2026, 9, 25)
    by_mtime = note_verification("project", "", _days_ago(31, today=today), today=today)
    assert by_mtime is not None
    assert (by_mtime.source, by_mtime.age_days, by_mtime.stale) == ("mtime", 31, True)

    future = note_verification("note", "2099-01-01", None, today=today)
    assert future is not None
    assert future.age_days == 0 and future.stale is False

    assert note_verification("note", "not-a-date", None, today=today) is None


def test_note_verification_exempts_event_types_including_aliases_and_case() -> None:
    from ciao.memory_audit import is_stale_exempt_type, note_verification

    today = datetime.date(2026, 9, 25)
    for note_type in ("log", "journal", "workspace", "Journal", "hackathon-log", "project-log"):
        assert is_stale_exempt_type(note_type), note_type
        result = note_verification(note_type, "2020-01-01", None, today=today)
        assert result is not None
        # Still aged (the map shows it), never stale.
        assert result.exempt is True and result.stale is False
        assert result.age_days > 2000

    # A capitalised type ages on its canonical horizon, not the default one.
    person = note_verification("Person", "2026-06-01", None, today=today)
    assert person is not None and person.threshold_days == 90 and person.stale


def test_find_stale_notes_uses_the_shared_predicate_for_aliased_exempt_types() -> None:
    today = datetime.date(2026, 9, 25)
    entries = [
        _entry("memory-vault/Journal/hack.md", "hackathon-log"),
        _entry("memory-vault/People/Mo.md", "Person"),
    ]
    report = find_stale_notes(
        entries, mtimes={e.path_key: _days_ago(120, today=today) for e in entries}, today=today
    )

    assert [f["path"] for f in report["stale_notes"]] == ["memory-vault/People/Mo.md"]
    assert report["stale_notes"][0]["threshold_days"] == 90
    assert report["notes_exempt"] == 1


def test_the_curation_stale_pass_agrees_with_the_audit_on_aliased_exempt_types(
    tmp_path: Path,
) -> None:
    """The nightly plan calls this predicate, so it inherits its answers.

    A fourth copy of the threshold table would be a fourth thing to keep in
    agreement with the other three surfaces that already share this one
    (`os-audit`, the Memory Map's `stale` flag, the review queue's `unverified`
    signal). The aliased exempt type is the case that would break first: a
    `hackathon-log` is a dated record that never ages, and a plan that flagged
    it would send the nightly run to re-verify a log entry every month forever.

    Run through `build_worklist` rather than the private helper, because what
    matters is the plan the nightly run acts on, not the selection function.
    """
    from ciao import curation_run as cr
    from ciao.entity_types import load_entity_types
    from ciao.vault_index import scan_vault

    today = datetime.date(2026, 9, 25)
    vault = tmp_path / "memory-vault"
    (vault / "Journal").mkdir(parents=True)
    (vault / "People").mkdir(parents=True)
    (vault / "Workspace").mkdir(parents=True)
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        f"---\nlast_full_pass: {today.isoformat()}\n---\n\n# Curation log\n", encoding="utf-8"
    )
    (vault / "Journal" / "hack.md").write_text(
        "---\ntype: hackathon-log\nupdated: 2020-01-01\n---\n\n# Hack\n\nA dated record.\n",
        encoding="utf-8",
    )
    (vault / "People" / "Mo.md").write_text(
        "---\ntype: Person\nupdated: 2024-01-01\n---\n\n# Mo\n\nDurable fact.\n",
        encoding="utf-8",
    )
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# Workspace\n", encoding="utf-8")

    worklist = cr.build_worklist(
        vault_root=vault,
        workspace=vault.name,
        guide_path=guide,
        category_registry=load_entity_types(vault),
        workspace_dir=tmp_path,
        today=today,
    )
    stale = [item for item in worklist.items if item.pass_id == cr.PASS_STALE_NOTE]

    # The same note the audit lists, keyed by the same vault-relative path it
    # resolves to — and the aliased exempt type is not among them.
    audit = find_stale_notes(
        [e for e in scan_vault(vault)],
        vault_root=vault,
        today=today,
        registry=load_entity_types(vault),
    )
    assert [f["path"] for f in audit["stale_notes"]] == ["memory-vault/People/Mo.md"]
    assert [item.keys[0] for item in stale] == [cr.item_key(cr.PASS_STALE_NOTE, "People/Mo.md")]


def test_a_vault_category_sets_its_own_staleness(tmp_path: Path) -> None:
    """A category's `stale_after_days` is the horizon its notes age against,
    reached through that category's aliases and dropped when it is disabled.

    The alias-typed note is the load-bearing half: `client` claims no id of its
    own, so without the registry's alias table it would age on its own spelling,
    take the default horizon and sit unflagged at 20 days. Both notes sharing
    the 10-day horizon is the assertion that it resolved.
    """
    from ciao import entity_types
    from ciao.memory_audit import (
        STALE_NOTE_DEFAULT_DAYS,
        is_stale_exempt_type,
        note_threshold_days,
    )

    today = datetime.date(2026, 8, 23)
    vault = tmp_path / "memory-vault"
    (vault / "Clients").mkdir(parents=True)
    categories = vault / "entity-types.yaml"
    customer = (
        "- id: customer\n"
        "  label: Customer\n"
        "  kind: entity\n"
        "  folder: Clients\n"
        "  aliases: [client]\n"
        "  stale_after_days: 10\n"
    )
    entries = [
        _entry("memory-vault/Clients/Acme.md", "customer"),
        _entry("memory-vault/Clients/Beta.md", "client"),
        _entry("memory-vault/Journal/day.md", "journal"),
    ]
    mtimes = {e.path_key: _days_ago(20, today=today) for e in entries}

    categories.write_text(customer, encoding="utf-8")
    entity_types.clear_entity_types_cache()
    report = find_stale_notes(entries, vault_root=vault, mtimes=mtimes, today=today)

    assert {f["path"] for f in report["stale_notes"]} == {
        "memory-vault/Clients/Acme.md",
        "memory-vault/Clients/Beta.md",
    }
    assert {f["threshold_days"] for f in report["stale_notes"]} == {10}
    assert report["notes_checked"] == 2
    assert report["notes_exempt"] == 1, "the exempt set is this module's, not the file's"

    # A caller holding the registry gets the same horizon with no vault in hand.
    registry = entity_types.load_entity_types(vault)
    assert note_threshold_days("customer", registry=registry) == 10
    assert not is_stale_exempt_type("client", registry=registry)

    # Disabled: no opinion, so the default — and 20 days is nowhere near it.
    categories.write_text(customer + "  enabled: false\n", encoding="utf-8")
    entity_types.clear_entity_types_cache()
    disabled = find_stale_notes(entries, vault_root=vault, mtimes=mtimes, today=today)

    assert disabled["stale_notes"] == []
    assert disabled["notes_checked"] == 2
    assert note_threshold_days(
        "customer", registry=entity_types.load_entity_types(vault)
    ) == STALE_NOTE_DEFAULT_DAYS


# --- the entry excerpt and its context are prose, not source ---------------


def _entry_verdicts(text: str, *, today: datetime.date | None = None):
    """Every selected entry verdict for one note body."""
    from ciao.memory_audit import note_entry_coverage

    _cov, selected, _doc = note_entry_coverage(
        text,
        note_type="person",
        updated="2019-01-01",
        mtime=0.0,
        note_path="People/Alice.md",
        rendered="memory-vault/People/Alice.md",
        workspace="work",
        today=today or datetime.date(2026, 9, 30),
    )
    return selected


def test_entry_excerpt_strips_markdown_markers_to_prose() -> None:
    """A card renders the excerpt as text, so the source markers are noise.

    `1. **Learn.** People…` used to reach the boxed fact exactly as written,
    `**`, marker and all. The excerpt is what the reader sees; the entry text
    stays the identity key, so this is display only.
    """
    selected = _entry_verdicts(
        "- Lives in Porto [verified: 2026-09-28]\n"
        "1. **Learn.** People leave each session knowing more.\n"
    )
    learned = next(v for v in selected if "Learn" in v.excerpt)
    assert learned.excerpt == "Learn. People leave each session knowing more."
    assert "**" not in learned.excerpt
    assert not learned.excerpt.startswith("1.")


def test_entry_excerpt_drops_backticks_link_urls_and_stays_capped() -> None:
    selected = _entry_verdicts(
        "- See [the docs](https://example.dev/deep/path) for `timeout_s`.\n"
        "- " + "word " * 80 + "\n"
    )
    linked = next(v for v in selected if "docs" in v.excerpt)
    assert linked.excerpt == "See the docs for timeout_s."
    long = next(v for v in selected if v.excerpt.startswith("word"))
    assert long.excerpt.endswith(" …")


def test_entry_identity_and_fingerprint_ignore_the_excerpt_strip() -> None:
    """The strip is display only: the same source text mints the same identity."""
    from ciao import note_entries as ne

    source = "1. **Learn.** People leave each session knowing more.\n"
    selected = _entry_verdicts(source)
    entry = ne.parse_note_entries(
        source, note_path="People/Alice.md", workspace="work"
    ).entries[0]
    assert selected[0].identity == entry.identity
    assert selected[0].fingerprint == entry.fingerprint


def test_sibling_bullets_are_not_reprinted_as_context() -> None:
    """A dense list has no prose between bullets, so a neighbour is the next fact.

    Reprinting it in grey made one fact read as two or three claims; the walk
    now skips any line inside another entry's span and stops at the prose above.
    """
    text = (
        "# Address\n"
        "\n"
        "Intro line.\n"
        "\n"
        "- First fact [verified: 2020-01-01]\n"
        "- Second fact [verified: 2020-01-01]\n"
        "- Third fact [verified: 2020-01-01]\n"
    )
    selected = _entry_verdicts(text)
    assert len(selected) == 3
    excerpts = {v.excerpt for v in selected}
    for verdict in selected:
        assert all(line not in excerpts for line in verdict.context)
    assert all(verdict.context == ("Intro line.",) for verdict in selected)


def test_same_section_prose_shows_while_the_heading_does_not() -> None:
    """Skipping siblings must not empty the context of a fact that has real prose.

    The heading above the fact is the section the row already names, so it is not
    context: reprinting it made the same heading appear twice on one card. The
    heading is still a boundary, so the walk stops there rather than reaching
    into the section above.
    """
    text = (
        "# Address\n"
        "\n"
        "## Landlord\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "Verified with the agency.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.section == "Landlord"
    assert "## Landlord" not in verdict.context
    assert "# Address" not in verdict.context
    assert verdict.context == ("Verified with the agency.",)


def test_a_heading_is_a_boundary_from_the_next_section() -> None:
    """A dense list beside another section must not borrow that section's heading.

    The reproduction for this issue: the fact's grey context read
    ``('## Address', '## Employment')``, so the card showed the next section's
    heading as if it described this fact. The heading is a hard boundary even
    when blank lines and several sibling facts separate it from the entry.
    """
    text = (
        "## Address\n"
        "- First fact\n"
        "- Second fact\n"
        "- Third fact\n"
        "\n"
        "## Employment\n"
        "- Fourth fact\n"
    )
    selected = _entry_verdicts(text)
    assert len(selected) == 4
    excerpts = {verdict.excerpt for verdict in selected}
    for verdict in selected:
        assert all(line not in excerpts for line in verdict.context)
        assert all("Address" not in line for line in verdict.context)
        assert all("Employment" not in line for line in verdict.context)
    first = next(v for v in selected if v.excerpt == "First fact")
    fourth = next(v for v in selected if v.excerpt == "Fourth fact")
    assert first.context == ()
    assert fourth.context == ()


def test_prose_either_side_of_a_dense_list_stays_useful_context() -> None:
    """Same-section prose on both sides survives, and the heading does not.

    The useful before/after lines are the whole point of the context slot, and a
    walk that stopped at the *first* heading it found would be right for this
    note but only because the prose happens to sit on the entry's own side.
    """
    text = (
        "## Address\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "- First fact\n"
        "- Second fact\n"
        "- Third fact\n"
        "\n"
        "A closing sentence.\n"
        "\n"
        "## Employment\n"
        "- Fourth fact\n"
    )
    selected = _entry_verdicts(text)
    first = next(v for v in selected if v.excerpt == "First fact")
    third = next(v for v in selected if v.excerpt == "Third fact")
    fourth = next(v for v in selected if v.excerpt == "Fourth fact")
    assert first.context == ("Intro paragraph.", "A closing sentence.")
    assert third.context == ("Intro paragraph.", "A closing sentence.")
    # The next section's entry has its own heading as a boundary above it and
    # nothing of the previous section's prose crosses it.
    assert fourth.context == ()


def test_heading_like_text_in_a_fence_is_not_a_boundary_or_context() -> None:
    """Code is neither prose nor a section heading, so the walk passes over it.

    A note that documents Markdown keeps `## Something` inside a fence. Reading
    it as a heading would end the walk at a code sample; showing it as context
    would put code in the grey prose slot. Both are wrong: the fenced block is
    skipped, and the same-section prose on the other side of it still shows.
    """
    text = (
        "# Address\n"
        "\n"
        "Same section prose.\n"
        "\n"
        "```text\n"
        "## Employment\n"
        "- Not a fact, the sibling of nobody\n"
        "```\n"
        "\n"
        "- First fact [verified: 2019-05-01]\n"
        "\n"
        "After the fact.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ("Same section prose.", "After the fact.")
    assert all("## Employment" not in line for line in verdict.context)
    assert all("```" not in line for line in verdict.context)


def test_frontmatter_is_never_context_on_a_note_without_a_heading() -> None:
    """A heading-free note's first fact must not inherit the frontmatter.

    The parser recognises frontmatter as structure, not prose; a context walk
    that only knew about headings would show the closing ``---`` and the
    ``type:``/``updated:`` lines above the first bullet. The same parser boundary
    (``frontmatter_span``) is what decides it here, including when real prose
    follows.
    """
    frontmatter = (
        "---\n"
        "type: person\n"
        "updated: 2026-01-01\n"
        "---\n"
        "\n"
    )
    bare = _entry_verdicts(frontmatter + "- First fact\n- Second fact\n")
    assert len(bare) == 2
    assert all(verdict.context == () for verdict in bare)

    with_prose = _entry_verdicts(
        frontmatter + "Tenancy notes from the letting agency.\n\n"
        "- First fact\n- Second fact\n"
    )
    assert [verdict.context for verdict in with_prose] == [
        ("Tenancy notes from the letting agency.",),
        ("Tenancy notes from the letting agency.",),
    ]
    for verdict in with_prose:
        assert all("---" not in line for line in verdict.context)
        assert all("type:" not in line for line in verdict.context)
        assert all("updated:" not in line for line in verdict.context)


def test_single_cell_table_row_is_not_fact_context() -> None:
    text = "Field | Value\n--- | ---\nOnly one cell\n\n- fact\n"
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ()
    assert verdict.identity == "5ce2f5e411be4d650f0298b40cee96558337d8989f45442f77417ae07b0be4b3"
    assert verdict.fingerprint == "0a8092a15e7b800d9be128d3745818b5a993b7bfb4b91ad76cbbca5ba906dd30"


def test_a_thematic_break_is_not_context_but_prose_beyond_it_is() -> None:
    """A rule is structure, not a sentence somebody wrote beside the fact.

    ``# A`` / ``- fact`` / ``---`` / ``More prose.`` used to put the bare rule in
    the grey context slot. The break is skipped in both directions, while the
    same-section prose on the far side of it still names the move.
    """
    after = _entry_verdicts(
        "# Address\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "---\n"
        "\n"
        "Verified with the agency.\n"
    )[0]
    assert after.context == ("Verified with the agency.",)
    assert all("---" not in line for line in after.context)

    before = _entry_verdicts(
        "# Address\n"
        "\n"
        "Notes from the tenancy file.\n"
        "\n"
        "---\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
    )[0]
    assert before.context == ("Notes from the tenancy file.",)

    bare = _entry_verdicts(
        "# Address\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "---\n"
    )[0]
    assert bare.context == ()


def test_table_rows_are_structure_not_context() -> None:
    """A table beside a fact is not the prose that explains it.

    The rows and the delimiter used to be eligible lines; the parser files them
    as a table, so the walk passes over the whole block and reaches the real
    paragraphs on either side.
    """
    text = (
        "# Address\n"
        "\n"
        "Before the table.\n"
        "\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| Phone | 912 |\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "After the table.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ("Before the table.", "After the table.")
    assert all("|" not in line for line in verdict.context)
    assert all("---" not in line for line in verdict.context)


def test_a_table_without_leading_pipes_is_not_context() -> None:
    """The delimiter owns the header and body even when no row starts with `|`.

    GFM tables need not use a leading pipe. ``Field | Value`` and
    ``Phone | 912`` used to reach the prose slot as if somebody had written them
    as sentences. The fact after the block keeps the real paragraphs on either
    side and none of the rows.
    """
    text = (
        "# Address\n"
        "\n"
        "Before the table.\n"
        "\n"
        "Field | Value\n"
        "--- | ---\n"
        "Phone number | Home\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "After the table.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ("Before the table.", "After the table.")
    assert all("|" not in line for line in verdict.context)
    assert all("Phone number" not in line for line in verdict.context)


def test_quotes_are_not_context_and_real_prose_is() -> None:
    """A quoted line is somebody else's words, not the note's own explanation."""
    text = (
        "# Address\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "> The agency said the boiler was due.\n"
        "\n"
        "The repair was booked afterwards.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ("The repair was booked afterwards.",)
    assert all(not line.startswith(">") for line in verdict.context)


def test_structure_between_sections_does_not_let_prose_cross_a_heading() -> None:
    """Skipping structure must not turn a heading into a soft boundary.

    A rule before the next section is a rule; the heading after it is still the
    boundary that keeps the next section's prose out of this fact's context.
    """
    text = (
        "# Address\n"
        "\n"
        "- Landlord is Mr Silva [verified: 2019-05-01]\n"
        "\n"
        "---\n"
        "\n"
        "## Employment\n"
        "\n"
        "Employed at Radix since March.\n"
    )
    verdict = _entry_verdicts(text)[0]
    assert verdict.context == ()
    assert all("Radix" not in line for line in verdict.context)
    assert all("Employment" not in line for line in verdict.context)


def test_identity_and_fingerprint_baseline_for_a_representative_note() -> None:
    """The display changes are not allowed to move a fact's identity.

    These are fixed digests, not a comparison with another call in this module:
    the same source text, workspace and note path must keep minting the same
    keys, because check state and pending proposals are keyed by them. A change
    here is a migration, not a display tweak.
    """
    from ciao import note_entries as ne

    source = (
        "---\n"
        "type: person\n"
        "updated: 2026-01-01\n"
        "---\n"
        "# Alice\n"
        "\n"
        "- Lives in Porto\n"
        "- Landlord is Mr Silva\n"
        "\n"
        "Tenancy notes from the letting agency.\n"
    )
    document = ne.parse_note_entries(
        source, note_path="People/Alice.md", workspace="work"
    )
    assert [
        (entry.text, entry.identity, entry.fingerprint) for entry in document.entries
    ] == [
        (
            "- Lives in Porto",
            "1ccd42ea26f881ed6b6d6ac70d1c42868a84bfe4e7b365a16a2ffe69c696b0e6",
            "648fe46b4e4dcec3762e3306af803d111986dfdd81c18d4ac07ddb8de57bff99",
        ),
        (
            "- Landlord is Mr Silva",
            "bd2daf068e6d3f7a89254599b4e99274e6c4400afbf65ea5ca9d70bec1f83fc3",
            "435110450cdd5cb5e0b37b1621a254b144e0850938baee488563bd5f058c8b14",
        ),
    ]
    assert document.headings == ((source.index("# Alice"), source.index("# Alice") + 7),)

    # A thematic break and a table around the facts change neither their
    # identity nor their fingerprints: the display walk never takes part in the
    # key. The prose still shows, and the structure does not.
    decorated = source.replace(
        "Tenancy notes from the letting agency.\n",
        "---\n"
        "\n"
        "| Field | Value |\n"
        "| --- | --- |\n"
        "| Phone | 912 |\n"
        "\n"
        "Tenancy notes from the letting agency.\n",
    )
    decorated_doc = ne.parse_note_entries(
        decorated, note_path="People/Alice.md", workspace="work"
    )
    assert [
        (entry.identity, entry.fingerprint) for entry in decorated_doc.entries
    ] == [(entry.identity, entry.fingerprint) for entry in document.entries]
    assert [verdict.context for verdict in _entry_verdicts(decorated)] == [
        ("Tenancy notes from the letting agency.",),
        ("Tenancy notes from the letting agency.",),
    ]


def test_heading_boundaries_leave_identity_and_fingerprint_untouched() -> None:
    """Context is display only; the same source text still mints the same fact."""
    from ciao import note_entries as ne

    source = (
        "## Address\n"
        "- First fact\n"
        "- Second fact\n"
        "\n"
        "## Employment\n"
        "- Third fact\n"
    )
    selected = _entry_verdicts(source)
    document = ne.parse_note_entries(
        source, note_path="People/Alice.md", workspace="work"
    )
    assert [(v.identity, v.fingerprint) for v in selected] == [
        (entry.identity, entry.fingerprint) for entry in document.entries
    ]
    assert [v.start for v in selected] == [entry.start for entry in document.entries]
    assert [v.excerpt for v in selected] == ["First fact", "Second fact", "Third fact"]
