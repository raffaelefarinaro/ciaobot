"""ciao.agent_paths against what the provider CLIs were observed to write (D-04, #696)."""

from __future__ import annotations

import json
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
