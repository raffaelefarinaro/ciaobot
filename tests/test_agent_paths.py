"""ciao.agent_paths against what the provider CLIs were observed to write (D-04, #696)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ciao import agent_paths
from ciao.transcripts import _claude_projects_dir

_FIXTURE = Path(__file__).parent / "fixtures" / "agent_discovery" / "claude_project_slugs.json"
_ROWS = [
    pytest.param(cwd, slug, id=f"{os_name}:{cwd}")
    for os_name, rows in json.loads(_FIXTURE.read_text(encoding="utf-8")).items()
    if not os_name.startswith("_")
    for cwd, slug in rows
]


@pytest.mark.parametrize(("cwd", "slug"), _ROWS)
def test_the_claude_slug_matches_what_claude_code_wrote(cwd: str, slug: str) -> None:
    # A string, not a Path: every row is checked on every OS, one rule for all.
    assert agent_paths.claude_project_slug(cwd) == slug


def test_the_transcript_lookup_uses_that_slug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    workspace = tmp_path / "work.space" / "my_vault"
    expected = tmp_path / ".claude" / "projects" / agent_paths.claude_project_slug(workspace)
    assert _claude_projects_dir(workspace) == expected
    assert "." not in expected.name and "_" not in expected.name


def _norm(p: Path) -> str:
    return str(p).replace("\\", "/") if os.name == "nt" else str(p)


def _assume_no_git_above(tmp_path: Path) -> None:
    if any((parent / ".git").exists() for parent in tmp_path.resolve().parents):
        pytest.skip("tmp_path is inside a git repo")


def test_claude_config_project_key_is_the_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    key = agent_paths.claude_config_project_key(repo / "a" / "b")
    assert key == _norm(repo.resolve())


def test_claude_config_project_key_counts_a_git_file(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "sub").mkdir(parents=True)
    (wt / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert agent_paths.claude_config_project_key(wt / "sub") == _norm(wt.resolve())


def test_claude_config_project_key_outside_git_is_the_folder(tmp_path: Path) -> None:
    _assume_no_git_above(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert agent_paths.claude_config_project_key(plain) == _norm(plain.resolve())


def test_claude_global_config_path_order(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_PATH": str(tmp_path / "x.json"), "CLAUDE_CONFIG_DIR": str(tmp_path / "d")}
    assert agent_paths.claude_global_config_path(env) == tmp_path / "x.json"
    assert agent_paths.claude_global_config_path({"CLAUDE_CONFIG_DIR": str(tmp_path / "d")}) == (
        tmp_path / "d" / ".claude.json"
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert agent_paths.claude_global_config_path({}) == tmp_path / "home" / ".claude.json"
