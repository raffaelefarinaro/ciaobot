"""Session-file lookup helpers must stay off the event loop's hot path.

The cross-cwd fallback used to run an uncached ``Path.glob(f"*/<sid>.jsonl")``
over every directory in ``~/.claude/projects``. On machines with hundreds of
stale slug dirs (CI evals, pytest scratch workspaces) each probe costs
hundreds of opendir calls, and polling endpoints re-run it per request — a
scandir storm on the event loop that stalls every in-flight stream. These
tests pin the caching contract that fixes that:

- the slug-directory list is cached (one iterdir per TTL window), and
- each lookup resolves ``<slug>/<sid>.jsonl`` with a direct stat, so a
  session created mid-window under a known cwd is found immediately, and
- a total miss refreshes the slug list (rate-limited) so a brand-new cwd is
  picked up — a miss must never be served from a list that cannot contain
  the session (callers such as the subagent completion watcher treat a miss
  as final), while absent-session polling stays at one refresh per interval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao import transcripts
from ciao.transcripts import (
    _global_session_matches,
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


def _sid(n: int) -> str:
    return f"{n:08d}-1111-5111-8111-111111111111"


def test_find_prefers_workspace_slug_dir(
    tmp_path: Path, projects_root: Path
) -> None:
    sid = _sid(1)
    near = _slug(projects_root, "/Users/me/proj") / f"{sid}.jsonl"
    near.write_text("{}")
    far = _slug(projects_root, "/elsewhere") / f"{sid}.jsonl"
    far.write_text("{}")

    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == near


def test_find_falls_back_to_other_cwd(
    tmp_path: Path, projects_root: Path
) -> None:
    sid = _sid(2)
    far = _slug(projects_root, "/elsewhere") / f"{sid}.jsonl"
    far.write_text("{}")

    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == far


def test_find_returns_none_when_absent(
    tmp_path: Path, projects_root: Path
) -> None:
    assert find_claude_session_file(_sid(3), Path("/Users/me/proj")) is None


def test_slug_list_cached_across_probes(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing-session probes within the TTL window walk the root once."""
    sid = _sid(4)
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
    # Another session id, same cached slug list — no second walk.
    other = _sid(5)
    ( _slug(projects_root, "/elsewhere") / f"{other}.jsonl" ).write_text("{}")
    find_claude_session_file(other, Path("/Users/me/proj"))
    assert calls["n"] == 1

    # Cache expiry forces exactly one more walk.
    monkeypatch.setattr(
        transcripts,
        "_global_session_scan_cache",
        (projects_root, 0.0, []),
    )
    find_claude_session_file(sid, Path("/Users/me/proj"))
    assert calls["n"] == 2


def test_new_session_under_known_cwd_found_immediately(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: a miss must never be served from a stale file listing.

    The old design cached enumerated *.jsonl paths, so a session created
    under a different cwd inside the TTL window stayed invisible and the
    subagent completion watcher treated it as permanently absent. The slug
    list is what is cached now; the file itself is stat-probed per probe,
    so a session landing in an existing slug dir is found with no re-walk.
    """
    sid = _sid(6)
    # The watcher's real scenario: the cwd's slug dir already exists (the
    # workspace has been used before); only the session file is new.
    elsewhere = _slug(projects_root, "/elsewhere")
    # Populate the slug list; sid does not exist yet.
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None

    # The session appears under that known cwd mid-window.
    far = elsewhere / f"{sid}.jsonl"
    far.write_text("{}")

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == far
    # No re-walk needed: the slug list already contains the cwd.
    assert calls["n"] == 0


def test_miss_refreshes_slug_list_for_new_cwd(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session in a brand-new cwd is found after one rate-limited refresh.

    The slug list is cached for the TTL, so a first-ever session under a
    cwd the list has never seen is only reachable after a refresh. The
    miss triggers exactly one, coalesced behind the lock.
    """
    sid = _sid(7)
    # Populate the slug list without the /new cwd.
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None

    # The session appears under a cwd the cached list has never seen.
    new_slug = _slug(projects_root, "/new")
    (new_slug / f"{sid}.jsonl").write_text("{}")

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    # Inside the rescan interval the refresh is deferred (rate-limit), so
    # the miss holds for now...
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None

    # ...and once the slug list ages past the interval, the next probe
    # refreshes and finds it. A capture time of 0.0 is TTL-expired, which
    # also opens the rescan gate.
    monkeypatch.setattr(
        transcripts,
        "_global_session_scan_cache",
        (projects_root, 0.0, []),
    )
    found = find_claude_session_file(sid, Path("/Users/me/proj"))
    assert found == new_slug / f"{sid}.jsonl"
    assert calls["n"] == 1


def test_absent_session_polling_rate_limited(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent-session probes refresh the slug list at most once per interval."""
    sid = _sid(8)
    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    for _ in range(10):
        assert find_claude_session_file(sid, Path("/Users/me/proj")) is None
    # First probe walks (cold cache); later misses inside the rescan
    # interval reuse the fresh slug list without walking again.
    assert calls["n"] == 1


def test_forced_refresh_beats_rate_limit(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: a final-result lookup must not be suppressed by the gate.

    The watcher and schedule wait call the lookup exactly once; the 2s
    rescan gate would swallow their decisive probe when a brand-new slug
    dir appeared moments after the last refresh, abandoning completion
    tracking or archiving the chat early. force_refresh must re-walk.
    """
    sid = _sid(13)
    # Cold probe populates the slug list without /fresh.
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None

    # Session lands in a brand-new cwd seconds later.
    fresh_slug = _slug(projects_root, "/fresh-cwd")
    (fresh_slug / f"{sid}.jsonl").write_text("{}")

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    # Default lookup: gate suppresses the refresh (rate-limited pollers).
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None
    assert calls["n"] == 0
    # Forced lookup: the gate is bypassed and the session is found.
    found = find_claude_session_file(
        sid, Path("/Users/me/proj"), force_refresh=True
    )
    assert found == fresh_slug / f"{sid}.jsonl"
    assert calls["n"] == 1


def test_concurrent_misses_coalesce_to_one_refresh(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N misses rate-limit to one slug-list refresh, not N."""
    sid = _sid(9)
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is None
    # Age the slug list so the rescan gate is open, then hammer lookups.
    monkeypatch.setattr(
        transcripts,
        "_global_session_scan_cache",
        (projects_root, 0.0, []),
    )

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    for _ in range(8):
        assert _global_session_matches(sid) == []
    # One refresh total: the first miss takes the gate, the rest serve its
    # (now fresh) slug list.
    assert calls["n"] == 1


def test_scan_cache_survives_missing_projects_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _global_session_matches(_sid(10)) == []
    # A later-created tree is picked up once the cache is cleared.
    monkeypatch.setattr(transcripts, "_global_session_scan_cache", None)
    root = tmp_path / ".claude" / "projects"
    sid = _sid(11)
    (root / "-elsewhere").mkdir(parents=True)
    (root / "-elsewhere" / f"{sid}.jsonl").write_text("{}")
    assert find_claude_session_file(sid, Path("/Users/me/proj")) is not None


def test_scan_cache_empty_when_projects_root_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # home() has no .claude/projects at all; must not raise.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _global_session_matches(_sid(12)) == []


def test_forced_refresh_via_subagent_tracking(
    tmp_path: Path, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """find_parent_session_file threads force_refresh through to the scan.

    The wrapper's miss path also runs an uncached glob net, so the file is
    found either way; the forced path reaches it through the shared cache
    refresh (cheap, shared) instead of the per-caller net.
    """
    from ciao import subagent_tracking

    sid = _sid(14)
    # Cold probe populates the slug list without /late-cwd.
    assert (
        subagent_tracking.find_parent_session_file(sid, Path("/Users/me/proj"))
        is None
    )
    late_slug = _slug(projects_root, "/late-cwd")
    (late_slug / f"{sid}.jsonl").write_text("{}")

    calls = {"n": 0}
    real_iterdir = Path.iterdir

    def counting_iterdir(self: Path):
        if self == projects_root:
            calls["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    # Forced lookup refreshes the shared slug list and finds the file —
    # exactly one walk, no per-caller uncached net.
    found = subagent_tracking.find_parent_session_file(
        sid, Path("/Users/me/proj"), force_refresh=True
    )
    assert found == late_slug / f"{sid}.jsonl"
    assert calls["n"] == 1