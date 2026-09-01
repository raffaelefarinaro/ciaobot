"""Session-file lookup helpers must stay off the event loop's hot path.

The cross-cwd fallback used to run an uncached ``Path.glob(f"*/<sid>.jsonl")``
over every directory in ``~/.claude/projects``. On machines with hundreds of
stale slug dirs (CI evals, pytest scratch workspaces) each probe costs
hundreds of opendir calls, and polling endpoints re-run it per request — a
scandir storm on the event loop that stalls every in-flight stream. These
tests pin the caching contract that fixes that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import transcripts
from ciao.transcripts import (
    _global_session_candidates,
    find_claude_session_file,
)


@pytest.fixture()
def projects_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the scanner at a throwaway ~/.claude/projects tree."""
    root = tmp_path / ".claude" / "projects"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return root


@pytest.fixture(autouse=True)
def _reset_scan_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcripts, "_global_session_scan_cache", None)


def _slug(root: Path, cwd: str) -> Path:
    d = root / ("-" + cwd.replace("/", "-").lstrip("-"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_find_prefers_workspace_slug_dir(
    tmp_path: Path, projects_root: Path
) -> None:
    sid = "11111111-1111-5111-8111-111111111111"
    near = _slug(projects_root, "/Users/me/proj") / f"{sid}.jsonl"
    near.write_text("{}")
    far = _slug(projects_root, "/elsewhere") / f"{sid}.jsonl"
    far.write_text("{}")

    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == near


def test_find_falls_back_to_other_cwd(
    tmp_path: Path, projects_root: Path
) -> None:
    sid = "22222222-2222-5222-8222-222222222222"
    far = _slug(projects_root, "/elsewhere") / f"{sid}.jsonl"
    far.write_text("{}")

    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == far


def test_find_returns_none_when_absent(
    tmp_path: Path, projects_root: Path
) -> None:
    assert find_claude_session_file(
        "33333333-3333-5333-8333-333333333333", Path("/Users/me/proj")
    ) is None


def test_scan_cache_one_walk_per_ttl(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second probe inside the TTL window must not re-walk the tree."""
    sid = "44444444-4444-5444-8444-444444444444"
    _slug(projects_root, "/elsewhere") / f"{sid}.jsonl"
    ( _slug(projects_root, "/elsewhere") / f"{sid}.jsonl" ).write_text("{}")

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    find_claude_session_file(sid, Path("/Users/me/proj"))
    assert calls["n"] == 1
    # A different session id reuses the same cached listing.
    find_claude_session_file(
        "55555555-5555-5555-8555-555555555555", Path("/Users/me/proj")
    )
    assert calls["n"] == 1

    # Cache expiry forces exactly one more walk.
    monkeypatch.setattr(
        transcripts,
        "_global_session_scan_cache",
        (projects_root, 0.0, []),
    )
    find_claude_session_file(sid, Path("/Users/me/proj"))
    assert calls["n"] == 2


def test_scan_cache_survives_missing_projects_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _global_session_candidates() == []
    # A later-created tree is picked up once the cache is cleared.
    monkeypatch.setattr(transcripts, "_global_session_scan_cache", None)
    root = tmp_path / ".claude" / "projects"
    sid = "66666666-6666-5666-8666-666666666666"
    (root / "-elsewhere").mkdir(parents=True)
    (root / "-elsewhere" / f"{sid}.jsonl").write_text("{}")
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is not None


def test_scan_cache_empty_when_projects_root_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # home() has no .claude/projects at all; must not raise.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _global_session_candidates() == []