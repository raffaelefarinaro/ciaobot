"""Tests for ciao.skills_sync: the change-detection that lets
install-custom-skills.sh run `skills update` only on skills whose upstream
actually moved (instead of re-downloading all 31 every run)."""

from __future__ import annotations

from ciao import skills_sync as sync


def _lock(*pairs):
    return {"skills": {n: {"source": s, "computedHash": "x"} for n, s in pairs}}


def test_desired_sources_extracts_name_to_source() -> None:
    lock = _lock(("a", "owner/r1"), ("b", "owner/r2"))
    assert sync.desired_sources(lock) == {"a": "owner/r1", "b": "owner/r2"}


def test_plan_skips_when_heads_unchanged_and_installed() -> None:
    lock = _lock(("a", "owner/r1"), ("b", "owner/r1"))
    cache = {"heads": {"owner/r1": "sha1"}, "skills": {"a": "owner/r1", "b": "owner/r1"}}
    heads = {"owner/r1": "sha1"}
    out = sync.plan(lock, cache, heads, installed={"a", "b"})
    assert out == {"to_update": [], "to_prune": [], "skip": True}


def test_plan_updates_only_skills_from_moved_repo() -> None:
    lock = _lock(("a", "owner/r1"), ("b", "owner/r2"), ("c", "owner/r2"))
    cache = {
        "heads": {"owner/r1": "sha1", "owner/r2": "sha2"},
        "skills": {"a": "owner/r1", "b": "owner/r2", "c": "owner/r2"},
    }
    heads = {"owner/r1": "sha1", "owner/r2": "SHA2-NEW"}  # only r2 moved
    out = sync.plan(lock, cache, heads, installed={"a", "b", "c"})
    assert out["to_update"] == ["b", "c"]
    assert out["skip"] is False


def test_plan_updates_new_skill_not_yet_installed() -> None:
    lock = _lock(("a", "owner/r1"), ("new", "owner/r1"))
    cache = {"heads": {"owner/r1": "sha1"}, "skills": {"a": "owner/r1"}}
    heads = {"owner/r1": "sha1"}  # repo unchanged, but `new` isn't installed yet
    out = sync.plan(lock, cache, heads, installed={"a"})
    assert out["to_update"] == ["new"]


def test_plan_prunes_skill_removed_from_lockfile() -> None:
    lock = _lock(("a", "owner/r1"))
    cache = {"heads": {"owner/r1": "sha1"}, "skills": {"a": "owner/r1", "gone": "owner/r1"}}
    heads = {"owner/r1": "sha1"}
    out = sync.plan(lock, cache, heads, installed={"a", "gone"})
    assert out["to_prune"] == ["gone"]
    assert out["skip"] is False


def test_plan_does_not_refetch_when_head_unknown_but_installed() -> None:
    # ls-remote failed for the repo (not in heads). An installed skill should
    # NOT be re-fetched on a transient failure -> only a KNOWN move fetches.
    # This keeps a network blip from triggering a slow full re-download.
    lock = _lock(("a", "owner/r1"))
    cache = {"heads": {"owner/r1": "sha1"}, "skills": {"a": "owner/r1"}}
    heads: dict[str, str] = {}  # ls-remote failed
    out = sync.plan(lock, cache, heads, installed={"a"})
    assert out == {"to_update": [], "to_prune": [], "skip": True}


def test_plan_fetches_uninstalled_even_when_head_unknown() -> None:
    # A not-yet-installed skill must still be fetched even if ls-remote failed.
    lock = _lock(("a", "owner/r1"))
    out = sync.plan(lock, {}, heads={}, installed=set())
    assert out["to_update"] == ["a"]


def test_plan_full_update_on_empty_cache() -> None:
    lock = _lock(("a", "owner/r1"), ("b", "owner/r2"))
    heads = {"owner/r1": "s1", "owner/r2": "s2"}
    out = sync.plan(lock, {}, heads, installed={"a", "b"})
    assert out["to_update"] == ["a", "b"]


def test_build_cache_records_heads_and_sources() -> None:
    lock = _lock(("a", "owner/r1"), ("b", "owner/r2"))
    heads = {"owner/r1": "s1", "owner/r2": "s2", "owner/unused": "s3"}
    cache = sync.build_cache(lock, heads)
    assert cache == {
        "heads": {"owner/r1": "s1", "owner/r2": "s2"},
        "skills": {"a": "owner/r1", "b": "owner/r2"},
    }


def test_build_cache_preserves_old_head_when_resolve_failed() -> None:
    # r2's ls-remote failed this run (not in heads), but it was cached before.
    # The merged cache must keep r2's old head so we don't lose the snapshot
    # and force a re-fetch next run.
    lock = _lock(("a", "owner/r1"), ("b", "owner/r2"))
    heads = {"owner/r1": "s1-new"}  # only r1 resolved
    old = {"heads": {"owner/r1": "s1-old", "owner/r2": "s2-old"}, "skills": {}}
    cache = sync.build_cache(lock, heads, old_cache=old)
    assert cache["heads"] == {"owner/r1": "s1-new", "owner/r2": "s2-old"}


def test_build_cache_drops_repos_no_longer_desired() -> None:
    lock = _lock(("a", "owner/r1"))
    old = {"heads": {"owner/r1": "s1", "owner/gone": "sx"}, "skills": {}}
    cache = sync.build_cache(lock, {"owner/r1": "s1"}, old_cache=old)
    assert cache["heads"] == {"owner/r1": "s1"}
