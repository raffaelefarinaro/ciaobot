"""Tests for `scripts/work_chat_transcripts.py`.

This helper backs the `sched-workdaily` CHATS subagent. The Work daily summary
must never see personal-workspace chat transcripts (Ciao infra, Faraman,
Wedding, etc.); the helper enforces that filter by joining
`.runtime/web_projects.json` (chat→project→workspace) to the archived
transcripts on disk.

Each test builds a tiny fake repo root under `tmp_path` so the script's
behaviour can be exercised without touching the real runtime state.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the `scripts/` dir importable so we can call `main()` directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

work_chat_transcripts = pytest.importorskip("work_chat_transcripts")


def _setup_fake_root(
    root: Path,
    *,
    projects: dict,
    chats: dict,
    transcripts: dict[str, list[str]],
) -> None:
    """Lay out `.runtime/web_projects.json` and `memory-vault/Logs/Chats/...`.

    `transcripts` maps chat_id -> list of transcript filenames to create
    inside that chat's `claude/` folder.
    """
    runtime = root / ".runtime"
    runtime.mkdir(parents=True)
    (runtime / "web_projects.json").write_text(
        json.dumps({"projects": projects, "chats": chats}),
        encoding="utf-8",
    )
    chats_root = root / "memory-vault" / "Logs" / "Chats"
    chats_root.mkdir(parents=True)
    for chat_id, files in transcripts.items():
        provider_dir = chats_root / chat_id / "claude"
        provider_dir.mkdir(parents=True)
        for name in files:
            (provider_dir / name).write_text("transcript body", encoding="utf-8")


def test_filters_personal_chats_out_of_work_run(tmp_path, capsys):
    """Regression bug: personal-workspace chats appearing in the
    Work daily log. With `--workspace work`, only work transcripts come back."""
    _setup_fake_root(
        tmp_path,
        projects={
            "p-work": {"workspace": "work", "name": "WorkProj"},
            "p-personal": {"workspace": "personal", "name": "Ciao-Improvements"},
        },
        chats={
            "chat-work1": {"project_id": "p-work"},
            "chat-personal1": {"project_id": "p-personal"},
        },
        transcripts={
            "chat-work1": ["2026-05-03T08-00-00Z-aaa.md"],
            "chat-personal1": ["2026-05-03T09-00-00Z-bbb.md"],
        },
    )

    rc = work_chat_transcripts.main(
        ["--repo-root", str(tmp_path), "--date", "2026-05-03", "--workspace", "work"]
    )

    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert "chat-work1" in out[0]
    assert "chat-personal1" not in out[0]


def test_workspace_personal_returns_only_personal(tmp_path, capsys):
    """Symmetric: `--workspace personal` excludes work chats."""
    _setup_fake_root(
        tmp_path,
        projects={
            "p-work": {"workspace": "work", "name": "WorkProj"},
            "p-personal": {"workspace": "personal", "name": "Ciao"},
        },
        chats={
            "chat-w": {"project_id": "p-work"},
            "chat-p": {"project_id": "p-personal"},
        },
        transcripts={
            "chat-w": ["2026-05-03T08-00-00Z-w.md"],
            "chat-p": ["2026-05-03T09-00-00Z-p.md"],
        },
    )

    work_chat_transcripts.main(
        [
            "--repo-root",
            str(tmp_path),
            "--date",
            "2026-05-03",
            "--workspace",
            "personal",
        ]
    )
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert "chat-p" in out[0]


def test_filters_by_date_prefix(tmp_path, capsys):
    """Transcripts whose filename does not start with the date prefix are skipped."""
    _setup_fake_root(
        tmp_path,
        projects={"p": {"workspace": "work"}},
        chats={"chat-x": {"project_id": "p"}},
        transcripts={
            "chat-x": [
                "2026-05-03T08-00-00Z-today.md",
                "2026-05-02T08-00-00Z-yesterday.md",
            ],
        },
    )

    work_chat_transcripts.main(
        ["--repo-root", str(tmp_path), "--date", "2026-05-03", "--workspace", "work"]
    )
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    assert "today.md" in out[0]


def test_orphan_chat_without_known_project_dropped(tmp_path, capsys):
    """A chat referencing an unknown project_id is treated as not-work and dropped."""
    _setup_fake_root(
        tmp_path,
        projects={"p-work": {"workspace": "work"}},
        chats={
            "chat-known": {"project_id": "p-work"},
            "chat-orphan": {"project_id": "p-missing"},
        },
        transcripts={
            "chat-known": ["2026-05-03T08-00-00Z-known.md"],
            "chat-orphan": ["2026-05-03T08-00-00Z-orphan.md"],
        },
    )

    work_chat_transcripts.main(
        ["--repo-root", str(tmp_path), "--date", "2026-05-03", "--workspace", "work"]
    )
    out = capsys.readouterr().out.splitlines()
    assert any("chat-known" in line for line in out)
    assert all("chat-orphan" not in line for line in out)


def test_missing_runtime_state_is_silent(tmp_path, capsys):
    """If `.runtime/web_projects.json` is missing the helper exits 0 with no output."""
    rc = work_chat_transcripts.main(
        ["--repo-root", str(tmp_path), "--date", "2026-05-03", "--workspace", "work"]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_corrupt_runtime_state_returns_error(tmp_path, capsys):
    """A malformed JSON file should return a non-zero exit and a stderr note."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "web_projects.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "memory-vault" / "Logs" / "Chats").mkdir(parents=True)

    rc = work_chat_transcripts.main(
        ["--repo-root", str(tmp_path), "--date", "2026-05-03", "--workspace", "work"]
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert captured.out == ""
