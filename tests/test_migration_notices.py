"""The migration notices, probed once and shared by both surfaces (#816).

`ciao/migration_notices.py` owns the condition, the applicability rule and the
wording for the two notices Home and the OS audit used to keep separate copies
of. The contracts under test, one per acceptance item in #816:

1. the vault-location condition and its wording are the same on both surfaces,
   and the remedy names the ways `vault-relocate --apply` can refuse;
2. the audit reports a links finding the Home card cannot yet know about, and
   nothing Home does can silence it;
3. the two surfaces ask one question about the wikilink dialect and cannot
   answer it differently — including for a **scratch** vault holding a
   hand-written wikilink, which is the diagnostic the audit must not lose;
4. neither notice is an update-task catalog row, because neither has an honest
   completion receipt yet;
5. a Home poll opens no file under the vault, at any vault size, and the scan
   that establishes the verdict runs off the event loop.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ciao import migration_notices
from ciao.async_reads import run_read
from ciao.migration_notices import (
    LINKS_SCAN_TTL_S,
    UNMIGRATED_LINKS_NOTICE,
    VAULT_LOCATION_NOTICE,
    LinksFinding,
    cached_links,
    links_scan_is_stale,
    refresh_links,
    reset_links_cache,
    resolve_links,
    vault_location_findings,
)
from ciao.operator_actions import DetectionContext, detect_actions, dismiss_action
from ciao.os_audit import audit_upgrade_notices


@pytest.fixture(autouse=True)
def _no_shared_verdicts():
    """The wikilink cache is process-wide, so a verdict must not leak between tests."""
    reset_links_cache()
    yield
    reset_links_cache()


# -- fixtures ----------------------------------------------------------------


def _cfg(
    tmp_path: Path,
    *,
    vault_mode: str = "existing",
    roots: dict[str, Path] | None = None,
) -> SimpleNamespace:
    """A config stub with a registry, an adopted (or scratch) vault, and a mode."""
    vault = tmp_path / "memory-vault"
    registered = roots if roots is not None else {"personal": vault / "personal"}
    return SimpleNamespace(
        vault_root=vault,
        workspace_root=tmp_path,
        vault_mode=vault_mode,
        state_path=tmp_path / ".runtime" / "state.json",
        workspace_names=lambda: list(registered),
        workspace_vault_root=lambda name: registered.get(name, vault / name),
        canonical_workspace_vault_root=lambda name: vault / name,
    )


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _notes(root: Path, count: int, *, wikilink: str | None = None) -> Path:
    """A vault of `count` markdown notes, one of them carrying a wikilink."""
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"note-{index:04d}.md").write_text(
            f"---\ntype: note\n---\n# Note {index}\n", encoding="utf-8"
        )
    if wikilink is not None:
        (root / "wikilinked.md").write_text(
            f"---\ntype: note\n---\nSee [[{wikilink}]].\n", encoding="utf-8"
        )
    return root


def _audit_types(config: object, runtime: Path | None) -> set[str]:
    notices = audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
    return {n["type"] for n in notices}


def _tiles(config: object, runtime: Path) -> list[Any]:
    context = DetectionContext(config=config, runtime_dir=runtime)
    return [a for a in detect_actions(context) if a.kind == "unmigrated-links"]


# -- (1) one condition, one wording, for the vault location ------------------


def test_home_and_the_audit_agree_on_the_misplaced_vault_and_its_wording(
    tmp_path: Path,
) -> None:
    """The duplicated pair resolves to one implementation and one sentence.

    Both surfaces read the same findings, so the detail, the title and the remedy
    are the same strings — a mismatch here means one of them still holds a copy.
    """
    elsewhere = tmp_path / "elsewhere" / "personal"
    elsewhere.mkdir(parents=True)
    config = _cfg(tmp_path, roots={"personal": elsewhere})
    runtime = _runtime(tmp_path)

    tile = next(
        a for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
        if a.kind == "vault-location"
    )
    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == VAULT_LOCATION_NOTICE
    )

    assert tile.title == "The personal vault is not in its standard folder"
    assert tile.detail == notice["detail"]
    # The audit's remedy used to describe moving the folder and hand-editing the
    # registry by hand. The Home card's chat seed carries the shared sentence, so
    # the command is in both and the hand path is in neither.
    assert "ciao vault-relocate personal --apply" in tile.chat_prompt
    assert "ciao vault-relocate personal --undo" in notice["remedy"]


def test_the_shared_remedy_names_the_ways_apply_refuses(tmp_path: Path) -> None:
    """Both surfaces state the refusals, not just the one that opens a chat.

    `--apply` refuses rather than guessing: a symlink source or destination, a
    top-level entry it cannot classify, a vault holding another workspace's root,
    and a vault outside the install's git worktree, where there is no `git mv` and
    no automatic undo at all. That last one is why the remedy points at the
    refusal instead of restating a route — the command itself tells the operator
    to finish that case by hand, so a remedy saying "never move it by hand" would
    be wrong exactly where it matters.
    """
    finding = vault_location_findings(_moved(tmp_path))
    remedy = finding[0].remedy

    assert "symlink" in remedy
    assert "classify" in remedy
    assert "another workspace" in remedy
    assert "outside the install's git worktree" in remedy
    assert "read it" in remedy


def _moved(tmp_path: Path) -> SimpleNamespace:
    elsewhere = tmp_path / "elsewhere" / "personal"
    elsewhere.mkdir(parents=True, exist_ok=True)
    return _cfg(tmp_path, roots={"personal": elsewhere})


def test_one_finding_per_misplaced_workspace(tmp_path: Path) -> None:
    """Both workspaces are misplaced, so both surfaces report both."""
    config = _cfg(
        tmp_path,
        roots={
            "personal": tmp_path / "a" / "personal",
            "work": tmp_path / "b" / "work",
        },
    )
    for root in (tmp_path / "a" / "personal", tmp_path / "b" / "work"):
        root.mkdir(parents=True)

    assert [f.workspace for f in vault_location_findings(config)] == ["personal", "work"]
    assert {
        a.id for a in detect_actions(DetectionContext(config=config))
        if a.kind == "vault-location"
    } == {"vault-location:personal", "vault-location:work"}


def test_a_root_that_is_not_there_is_not_a_misplaced_vault(tmp_path: Path) -> None:
    """A missing root is a different notice, and there is nothing to relocate."""
    config = _cfg(tmp_path, roots={"personal": tmp_path / "gone" / "personal"})

    assert vault_location_findings(config) == []


def test_a_registry_that_cannot_be_resolved_is_skipped_not_raised(
    tmp_path: Path,
) -> None:
    """One broken entry must not take the strip or the audit down with it."""
    elsewhere = tmp_path / "elsewhere" / "personal"
    elsewhere.mkdir(parents=True)
    config = _cfg(
        tmp_path,
        roots={"personal": elsewhere, "work": tmp_path / "nope" / "work"},
    )

    def _resolve(name: str) -> Path:
        if name == "work":
            raise RuntimeError("registry is half-written")
        return elsewhere

    config.workspace_vault_root = _resolve

    assert [f.workspace for f in vault_location_findings(config)] == ["personal"]
    assert "vault-location:personal" in {
        a.id for a in detect_actions(DetectionContext(config=config))
    }


def test_a_config_without_a_registry_reports_nothing(tmp_path: Path) -> None:
    """Advisory only: no registry means nothing to compare, on either surface."""
    config = SimpleNamespace(vault_root=tmp_path / "v", vault_mode="existing")

    assert vault_location_findings(config) == []
    assert VAULT_LOCATION_NOTICE not in _audit_types(config, _runtime(tmp_path))


# -- (2) the audit's finding outlives the Home card --------------------------


def test_the_audit_reports_a_scratch_vault_with_a_hand_written_wikilink(
    tmp_path: Path,
) -> None:
    """The diagnostic the mode gate would have deleted (R0/R1).

    A `scratch` vault is created conformant, so it is clean unless somebody puts
    a wikilink in it — and an operator pasting one from another tool is the whole
    reason this notice exists. Gating the notice on `vault_mode` made the audit
    agree with Home by going blind here, which is the one thing a diagnostic
    interface must not do to remove a disagreement.
    """
    config = _cfg(tmp_path, vault_mode="scratch")
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == UNMIGRATED_LINKS_NOTICE
    )

    assert "wikilinked.md" in notice["detail"]
    assert "may still" not in notice["detail"]


def test_home_reports_the_same_finding_once_a_scan_has_established_it(
    tmp_path: Path,
) -> None:
    """Parity is not a rule both surfaces apply; it is one answer both read.

    Home cannot walk the vault, so the answer reaches it through a bounded
    off-loop scan. Once that has run, Home and the audit are quoting the same
    sentence about the same note — which is what a shared probe is for.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    # Before a scan: nothing. The tile is not a guess from a receipt's absence.
    assert _tiles(config, runtime) == []

    assert resolve_links(config, runtime) is not None
    tile = _tiles(config, runtime)[0]
    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == UNMIGRATED_LINKS_NOTICE
    )

    assert tile.detail == notice["detail"]
    assert "wikilinked.md" in tile.detail


def test_nothing_home_does_silences_the_audit_finding(tmp_path: Path) -> None:
    """The card cannot be dismissed at all, and the audit does not consult it.

    A machine condition is only ever fixed, never dismissed, so `dismiss_action`
    refuses the id and the strip re-renders the same tile afterwards. The audit's
    answer comes from the condition either way. This is the guard for #800's
    catalog step: when this notice does become an optional, dismissible card, the
    dismissal must reach Home and *only* Home, and the assertions below are what
    that change has to keep.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    context = DetectionContext(config=config, runtime_dir=runtime)
    resolve_links(config, runtime)

    with pytest.raises(ValueError):
        dismiss_action("vault-unmigrated-links", context)

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
    assert "vault-unmigrated-links" in {a.id for a in detect_actions(context)}


def test_the_audit_resolves_its_own_verdict_rather_than_reading_the_cache(
    tmp_path: Path,
) -> None:
    """A report that reused a stored verdict could report a stale one.

    Home's answer is allowed to be minutes old — that is what the window is for.
    An audit run is not: it is the surface an operator opens to find out what is
    true, so it walks. The cache is a convenience for Home, never an authority
    over the audit.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    # A stored "clean" verdict, published as if a scan had just found nothing.
    resolve_links(config, runtime)
    _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
    (config.vault_root / "personal" / "late.md").write_text(
        "---\ntype: note\n---\nSee [[People/Peter]].\n", encoding="utf-8"
    )

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)


# -- (3) one question, two surfaces ------------------------------------------


def test_a_completed_receipt_retires_the_notice_on_both_surfaces(
    tmp_path: Path,
) -> None:
    from ciao.vault_migrate_links import write_receipt

    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    write_receipt(runtime, {"vault_root": str(config.vault_root), "files_rewritten": 2})

    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, runtime)
    assert _tiles(config, runtime) == []
    # Nothing to wake a scan for: the receipt says the work is done.
    assert links_scan_is_stale(config, runtime) is False


def test_a_partial_migration_leaves_the_finding_on_both_surfaces(
    tmp_path: Path,
) -> None:
    """A refusal is still a finding until the condition is gone.

    The receipt is what retires the notice, so a run that could not write every
    note leaves it raised on both surfaces rather than half-silenced.
    """
    from ciao.vault_migrate_links import write_receipt

    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    write_receipt(
        runtime,
        {
            "vault_root": str(config.vault_root),
            "rewrites": [{"path": "other.md", "offset": 0, "from": "x", "to": "y"}],
            "failed": [{"path": "personal/wikilinked.md", "error": "denied"}],
        },
    )

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
    assert [t.id for t in _tiles(config, runtime)] == ["vault-unmigrated-links"]


def test_a_clean_vault_raises_nothing_on_either_surface(tmp_path: Path) -> None:
    """The reason this notice can now reach zero is not "a migration ran".

    A vault written in markdown links from the start satisfies every old
    applicability condition and has nothing to convert. The old card fired on it
    anyway, forever; the audit was right and the card was not. Both are now
    silent because a walk established there is nothing to do.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 3)
    runtime = _runtime(tmp_path)

    assert resolve_links(config, runtime) is None
    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, runtime)
    assert _tiles(config, runtime) == []


def test_a_vault_mode_never_decides_whether_a_wikilink_is_reported(
    tmp_path: Path,
) -> None:
    """Both modes are scanned, so the mode is not a rule with two readings."""
    for mode in ("existing", "scratch", "", None):
        config = _cfg(tmp_path, vault_mode=mode or "existing")
        if mode is None:
            del config.vault_mode
        _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
        runtime = _runtime(tmp_path)

        assert resolve_links(config, runtime) is not None, mode
        assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime), mode
        reset_links_cache()


def test_without_a_runtime_root_nothing_guesses(tmp_path: Path) -> None:
    """The receipt lives under the runtime root; no root, no answer either way."""
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")

    assert resolve_links(config, None) is None
    assert cached_links(config, None) is None
    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, None)


def test_a_receipt_the_install_cannot_read_is_scanned_not_trusted(
    tmp_path: Path,
) -> None:
    """Unreadable is not "migrated", and both surfaces go and look."""
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text("{ not json", encoding="utf-8")

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
    assert [t.id for t in _tiles(config, runtime)] == ["vault-unmigrated-links"]


def test_a_finding_names_a_note_because_one_was_found() -> None:
    """The wording is the guard against a receipt's absence becoming a claim."""
    finding = LinksFinding(vault_root=Path("/v"), example="personal/a.md")

    assert "still uses" in finding.title
    assert "personal/a.md" in finding.detail
    assert "vault-unmigrate-links --apply" in finding.remedy


# -- the freshness window, and what invalidates an answer ---------------------


def test_a_stored_verdict_is_reused_inside_its_window_and_not_outside(
    tmp_path: Path,
) -> None:
    """One clock for how fast this engine looks at somebody's notes.

    Home polls every 60s, so a window shorter than the poll would buy nothing and
    a window with no end would let a converted note keep the card for ever. The
    value is a named constant, and this is the test that has to change with it.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    resolve_links(config, runtime, now=1000.0)

    assert cached_links(config, runtime, now=1000.0) is not None
    assert cached_links(config, runtime, now=1000.0 + LINKS_SCAN_TTL_S - 1) is not None
    assert cached_links(config, runtime, now=1000.0 + LINKS_SCAN_TTL_S) is None
    assert links_scan_is_stale(config, runtime, now=1000.0) is False
    assert links_scan_is_stale(config, runtime, now=1000.0 + LINKS_SCAN_TTL_S) is True
    # A clock that has not reached the window is not inside it.
    assert cached_links(config, runtime, now=999.0) is None


def test_a_changed_receipt_invalidates_the_stored_verdict(tmp_path: Path) -> None:
    """The token is the receipt's identity, because the receipt is the evidence.

    A migration writes and archives one and an un-migration replaces it, so either
    one lands a token the stored answer was not computed for — which is what
    stops a verdict established before an un-migration from outliving it.
    """
    from ciao.vault_migrate_links import write_receipt

    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    resolve_links(config, runtime)
    assert cached_links(config, runtime) is not None

    # A partial receipt is a *different* receipt, so the answer it was computed
    # against no longer applies and Home has to look again — which it does, and
    # the notice stays raised, because the migration is not complete.
    write_receipt(
        runtime,
        {"vault_root": str(config.vault_root), "failed": [{"path": "x", "error": "no"}]},
    )
    assert cached_links(config, runtime) is None
    assert links_scan_is_stale(config, runtime) is True
    assert resolve_links(config, runtime) is not None
    assert cached_links(config, runtime) is not None

    # A completed one puts the notice out of scope, and the stored answer with it.
    write_receipt(runtime, {"vault_root": str(config.vault_root), "files_rewritten": 2})
    assert cached_links(config, runtime) is None
    assert resolve_links(config, runtime) is None


def test_two_installs_in_one_process_do_not_share_a_verdict(tmp_path: Path) -> None:
    """A test, or a dev checkout beside a real engine, is two installs."""
    one = _cfg(tmp_path / "one")
    two = _cfg(tmp_path / "two")
    _notes(one.vault_root / "personal", 1, wikilink="People/Peter")
    _notes(two.vault_root / "personal", 1)

    resolve_links(one, _runtime(tmp_path / "one"))

    assert cached_links(one, _runtime(tmp_path / "one")) is not None
    assert cached_links(two, _runtime(tmp_path / "two")) is None


# -- (4) neither notice is a catalog task yet --------------------------------


def test_neither_notice_is_an_update_task() -> None:
    """No catalog row without an honest completion receipt (#800, step 3).

    Both notices are shape 3: the only "evidence" that the work was done is the
    condition's absence recomputed each render, which makes a completion check a
    tautology. `vault-relocate` has an apply/undo cycle and could hang a real
    receipt on its registry update, and the link migration's receipt is per install
    rather than per vault — so promoting either now would be claiming completion
    from a card. When that changes, the row's completion check must name a receipt
    that records the run, and this test is what has to be revisited.
    """
    from ciao.update_task_catalog import load_catalog

    catalog = load_catalog()
    notices = {VAULT_LOCATION_NOTICE, UNMIGRATED_LINKS_NOTICE}
    assert not [task for task in catalog.tasks if notices & {task.id, task.detector}]
    assert not [
        task
        for task in catalog.tasks
        if any(word in task.id for word in ("vault-location", "links"))
    ]


# -- (5) a Home poll never walks the vault, and the scan is off-loop ----------


def _vault_reads_under(root: Path, call: Any) -> list[str]:
    """Every filesystem access `call()` makes whose path is inside `root`.

    Recorded rather than timed: the claim under test is which files a pass reads,
    not how long it takes, and a timing assertion passes on a fast tmpfs and fails
    in CI. A walk is exactly the access this records, so
    `test_the_read_recorder_sees_a_walk` proves the recorder is not a no-op that
    makes the count trivially zero.
    """
    accesses: list[str] = []
    watched = (
        "open",
        "read_text",
        "read_bytes",
        "rglob",
        "glob",
        "iterdir",
        "walk",
    )
    original = {name: getattr(Path, name) for name in watched}

    def _record(name: str, original_fn: Any) -> Any:
        def _wrapped(self: Path, *args: Any, **kwargs: Any) -> Any:
            try:
                inside = Path(self).resolve().is_relative_to(root)
            except (OSError, ValueError):
                inside = False
            if inside:
                accesses.append(f"{Path(self)}:{name}")
            return original_fn(self, *args, **kwargs)

        return _wrapped

    with patch.multiple(
        Path, **{name: _record(name, fn) for name, fn in original.items()}
    ):
        call()
    return accesses


def _install_at(root: Path, count: int, *, wikilink: str | None = None) -> SimpleNamespace:
    """A config whose one workspace holds `count` notes and maybe a wikilink."""
    config = _cfg(root)
    _notes(config.vault_root / "personal", count, wikilink=wikilink)
    return config


def test_the_read_recorder_sees_a_walk(tmp_path: Path) -> None:
    """The performance test below is only worth anything if this holds.

    If the recorder stopped watching the accessor a walk uses, the poll count
    would stay zero for the wrong reason and the bound would be untested.
    """
    from ciao.vault_migrate_links import has_unmigrated_links

    config = _install_at(tmp_path, 3)
    vault = config.vault_root / "personal"

    assert _vault_reads_under(vault, lambda: has_unmigrated_links(vault))


def test_a_home_poll_opens_nothing_under_the_vault(tmp_path: Path) -> None:
    """The bound is a count, not a duration, and it does not scale with the vault.

    The wikilink notice is the one condition whose accurate answer needs a walk,
    so this is where the poll would have paid for it — with a card in the strip,
    without one, and after a scan established one. A registry value, a receipt
    and a stored verdict are the only things a pass may read. The small and large
    vaults are both checked because "cheap" has to mean independent of the vault,
    not merely small enough to measure.
    """
    for count in (1, 400):
        root = tmp_path / f"vault-{count}"
        config = _install_at(root, count, wikilink="People/Peter")
        runtime = _runtime(root)
        vault = config.vault_root / "personal"
        context = DetectionContext(config=config, runtime_dir=runtime)
        resolve_links(config, runtime)

        def _poll() -> None:
            # One pass per poll tick, plus the window-focus pass on top of it.
            for _ in range(3):
                detect_actions(context)

        # Before a scan as well: the cold path must be as cheap as the warm one.
        assert _vault_reads_under(vault, _poll) == [], count


def test_the_scan_runs_on_another_thread_and_through_the_bounded_executor(
    tmp_path: Path,
) -> None:
    """A vault walk may not hold the event loop, or the heartbeats behind it.

    `async_reads.run_read` is the bounded, coalesced executor the rest of the
    engine uses for exactly this, so the probe goes through it rather than
    inventing a second one. Both halves are asserted: the walk ran on a worker
    thread, and the call went through `run_read`.
    """
    config = _install_at(tmp_path, 5, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    seen: list[str] = []
    real = migration_notices.resolve_links

    def _note_thread(*args: Any, **kwargs: Any) -> LinksFinding | None:
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    async def _run() -> None:
        with patch("ciao.migration_notices.run_read", wraps=run_read) as reader:
            with patch("ciao.migration_notices.resolve_links", side_effect=_note_thread):
                await refresh_links(config, runtime)
        reader.assert_called_once()
        assert cached_links(config, runtime) is not None

    asyncio.run(_run())

    assert seen and all(name != "MainThread" for name in seen), seen


async def test_a_second_scan_inside_the_window_does_no_work(tmp_path: Path) -> None:
    """Two polls can pass the route's gate; only one walk is worth doing."""
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    await refresh_links(config, runtime, now=500.0)
    with patch(
        "ciao.migration_notices.resolve_links",
        side_effect=AssertionError("the window was ignored"),
    ):
        await refresh_links(config, runtime, now=500.0)
        assert await refresh_links(config, runtime, now=500.0 + LINKS_SCAN_TTL_S - 1) is None

    # Past the window it walks again — the vault changed under it, or may have.
    with patch("ciao.migration_notices.resolve_links", wraps=resolve_links) as scan:
        await refresh_links(config, runtime, now=500.0 + LINKS_SCAN_TTL_S)
    assert scan.call_count == 1


# -- neither surface keeps a copy --------------------------------------------


def test_neither_surface_keeps_a_second_implementation() -> None:
    """A duplicate predicate is the exact regression #816 was filed for.

    `operator_actions` and `os_audit` both name this module's probes and its
    notice types — same objects, not re-declared strings — and neither walks for
    wikilinks or resolves a canonical vault root of its own. Duplicated *logic* is
    what this removes; duplicated *cards* were never the problem.
    """
    from ciao import operator_actions, os_audit

    assert operator_actions.cached_links is cached_links
    assert operator_actions.vault_location_findings is vault_location_findings
    assert os_audit.VAULT_LOCATION_NOTICE == VAULT_LOCATION_NOTICE
    assert os_audit.UNMIGRATED_LINKS_NOTICE == UNMIGRATED_LINKS_NOTICE
    assert os_audit.resolve_links is resolve_links

    package = Path(migration_notices.__file__).parent
    for module in ("operator_actions.py", "os_audit.py"):
        text = (package / module).read_text(encoding="utf-8")
        assert "canonical_workspace_vault_root(" not in text, module
        assert "has_unmigrated_links" not in text, module
        # The wikilink receipt belongs to the shared applicability, not to a
        # surface. (`read_receipt(runtime)` also appears in the re-rooting gate,
        # which is a mandatory notice and deliberately not part of this pair.)
        assert "vault_migrate_links import read_receipt" not in text, module


def test_a_json_receipt_that_is_not_an_object_does_not_retire_the_notice(
    tmp_path: Path,
) -> None:
    """`read_receipt` gates on shape as well as status; a list is not a migration."""
    config = _install_at(tmp_path, 1, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text(
        json.dumps(["not", "a", "receipt"]), encoding="utf-8"
    )

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
