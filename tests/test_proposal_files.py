"""The proposal-file validator and `ciao proposal-check`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import cli, proposal_files

VALID = """---
type: skill-edit
target: ciao-memory
title: Tighten the stale-note step
summary: The stale-note step asks for a check that nothing performs.
rationale: Three care runs skipped it for the same reason.
evidence:
  - Workspace/Care-Report.md
  - chat-1234
proposed_change: Replace step 3 with a one-line instruction to re-read the note.
created: 2026-11-26
status: draft
---

Longer explanation of the change.
"""


def _problems(text: str, name: str = "tighten-step.md", parent: str = "Proposals") -> list[str]:
    return proposal_files.check(text, filename=name, parent=parent)


def test_a_complete_proposal_is_valid() -> None:
    assert _problems(VALID) == []


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("skill-edit", "ciao-memory"),
        ("upstream-issue", "owner/repo"),
        ("guide-edit", "AGENTS.md"),
        ("note-retire", "People/Old-Note.md"),
    ],
)
def test_every_type_accepts_its_own_target_shape(kind: str, target: str) -> None:
    text = VALID.replace("type: skill-edit", f"type: {kind}").replace("target: ciao-memory", f"target: {target}")
    assert _problems(text) == []


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("skill-edit", "Not A Skill"),
        ("upstream-issue", "no-slash"),
        ("guide-edit", "../outside.md"),
        ("note-retire", "People/Old-Note.txt"),
        ("note-retire", "/abs/Note.md"),
    ],
)
def test_a_target_of_the_wrong_shape_names_the_rule(kind: str, target: str) -> None:
    text = VALID.replace("type: skill-edit", f"type: {kind}").replace("target: ciao-memory", f"target: {target}")
    found = _problems(text)
    assert len(found) == 1 and "target" in found[0]


def test_every_missing_field_is_listed_at_once() -> None:
    text = "---\ntype: skill-edit\ntarget: ciao-memory\n---\nbody\n"
    found = _problems(text)
    assert len(found) == 1
    for field in ("title", "summary", "rationale", "evidence", "proposed_change", "created", "status"):
        assert field in found[0]
    assert "type," not in found[0]


def test_front_matter_is_required() -> None:
    assert "front matter" in _problems("just a body\n")[0]


def test_invalid_yaml_is_reported_not_raised() -> None:
    assert "not valid YAML" in _problems("---\ntitle: a: b: [\n---\nbody\n")[0]


def test_values_are_checked() -> None:
    text = (
        VALID.replace("status: draft", "status: maybe")
        .replace("type: skill-edit", "type: whim")
        .replace("created: 2026-11-26", "created: yesterday")
        .replace("  - chat-1234\n", "")
        .replace("evidence:\n  - Workspace/Care-Report.md", "evidence: just a string")
    )
    joined = "\n".join(_problems(text))
    assert "status" in joined and "type" in joined and "created" in joined and "evidence" in joined


def test_unknown_fields_and_an_empty_body_are_problems() -> None:
    text = VALID.replace("status: draft", "status: draft\nowner: me").replace("Longer explanation of the change.\n", "")
    joined = "\n".join(_problems(text))
    assert "unknown front matter fields: owner" in joined
    assert "body is empty" in joined


def test_title_and_summary_have_a_length_bound() -> None:
    text = VALID.replace("Tighten the stale-note step", "x" * (proposal_files.MAX_TITLE_CHARS + 1))
    assert any("title" in problem for problem in _problems(text))


def test_file_name_and_folder_are_checked() -> None:
    joined = "\n".join(_problems(VALID, name="Bad Name.md", parent="Elsewhere"))
    assert "lowercase slug" in joined and "Workspace/Proposals/" in joined


def test_credential_shaped_text_is_refused_without_echoing_it() -> None:
    text = VALID.replace("Longer explanation", "api_key: sk-live-abcdef0123456789\nLonger explanation")
    found = _problems(text)
    assert len(found) == 1 and "credential-shaped" in found[0]
    assert "sk-live" not in found[0]


def test_an_oversized_file_is_refused_before_parsing() -> None:
    found = _problems(VALID + "x" * proposal_files.MAX_FILE_BYTES)
    assert len(found) == 1 and "KB" in found[0]


def _write(tmp_path: Path, text: str, name: str = "tighten-step.md") -> Path:
    folder = tmp_path / "Workspace" / "Proposals"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_the_command_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _write(tmp_path, VALID)
    assert cli.main(["proposal-check", str(good)]) == 0
    assert capsys.readouterr().out.strip() == "tighten-step.md: ok"

    bad = _write(tmp_path, "---\ntype: skill-edit\n---\nbody\n", name="bad.md")
    assert cli.main(["proposal-check", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "problem(s) to fix" in out and "missing or empty" in out

    assert cli.main(["proposal-check", str(tmp_path / "missing.md")]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_the_command_json_and_credential_path_guard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _write(tmp_path, VALID)
    assert cli.main(["proposal-check", str(good), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["problems"] == []

    secret = tmp_path / ".env"
    secret.write_text(VALID, encoding="utf-8")
    assert cli.main(["proposal-check", str(secret)]) == 2
    assert "credential-adjacent" in capsys.readouterr().err
