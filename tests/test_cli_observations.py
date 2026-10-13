"""``ciao observation-*`` and ``ciao observations`` (#1338)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import cli

WORKSPACE = "personal"


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))
    workspace = tmp_path / "workspace"
    vault = workspace / "memory-vault" / WORKSPACE
    (vault / "Workspace").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CIAO_WORKSPACE", str(workspace))
    monkeypatch.setenv("CIAO_VAULT_ROOT", "memory-vault")
    return vault


def _run(command: str, *args: str) -> list[str]:
    return [command, *args, "--vault-root", f"memory-vault/{WORKSPACE}"]


def _add(tmp_path: Path, source: str, text: str = "prefers short answers") -> int:
    text_file = tmp_path / "text.txt"
    text_file.write_text(text, encoding="utf-8")
    return cli.main(
        _run("observation-add", "--about", "user", "--source", source,
             "--text-file", str(text_file), "--json")
    )


def test_add_list_propose_round_trip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], _workspace: Path
) -> None:
    for chat in ("chat-a", "chat-b", "chat-c"):
        assert _add(tmp_path, chat) == 0
    capsys.readouterr()

    assert cli.main(_run("observations", "--json")) == 0
    (row,) = json.loads(capsys.readouterr().out)["observations"]
    assert row["about"] == "the user"
    assert row["statement"] == "prefers short answers"
    assert row["section"] == "active"
    assert row["chats"] == ["chat-a", "chat-b", "chat-c"]
    assert row["count"] == 3 and row["due"] == "propose"

    assert cli.main(_run("observation-propose", row["id"], "--json")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["queued"] is True and result["destination"] == "profile"
    queue = (_workspace / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert "- [profile] prefers short answers" in queue

    assert cli.main(_run("observations", "--json")) == 0
    (row,) = json.loads(capsys.readouterr().out)["observations"]
    assert row["section"] == "promoted" and row["due"] is None


def test_text_file_lands_verbatim(tmp_path: Path, _workspace: Path) -> None:
    text = "says `whoami` and $(rm -rf /) out loud"
    assert _add(tmp_path, "chat-a", text) == 0
    saved = (_workspace / "Workspace" / "Observations.md").read_text(encoding="utf-8")
    assert f"About the user: {text}" in saved


def test_about_file_names_a_person(tmp_path: Path, _workspace: Path) -> None:
    about = tmp_path / "about.txt"
    about.write_text('Anna "Ann" Rossi', encoding="utf-8")
    text = tmp_path / "text.txt"
    text.write_text("reschedules Friday meetings", encoding="utf-8")
    assert cli.main(
        _run("observation-add", "--about-file", str(about), "--source", "chat-a",
             "--text-file", str(text))
    ) == 0
    saved = (_workspace / "Workspace" / "Observations.md").read_text(encoding="utf-8")
    assert 'About Anna "Ann" Rossi: reschedules Friday meetings' in saved


def test_missing_source_exits_2(tmp_path: Path) -> None:
    text = tmp_path / "text.txt"
    text.write_text("prefers short answers", encoding="utf-8")
    assert cli.main(
        _run("observation-add", "--about", "user", "--text-file", str(text))
    ) == 2


def test_propose_below_threshold_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _add(tmp_path, "chat-a") == 0
    capsys.readouterr()
    assert cli.main(_run("observations", "--json")) == 0
    row = json.loads(capsys.readouterr().out)["observations"][0]
    assert cli.main(_run("observation-propose", row["id"])) == 2


def test_prune_reports_archived_ids(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(_run("observation-prune", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == {"archived": []}
