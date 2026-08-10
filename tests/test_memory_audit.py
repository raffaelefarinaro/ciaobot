"""Tests for ``ciao.memory_audit`` bounded-memory rot detection."""

from __future__ import annotations

from pathlib import Path

from ciao.memory_audit import (
    audit_entries,
    find_event_shaped,
    find_stale_paths,
    find_superseded_state,
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
