"""The migration notices, probed once and shared by every surface (#816, #833, #800).

`ciao/migration_notices.py` owns the condition, the applicability rule and the
wording for the migration conditions Home and the OS audit used to keep separate
copies of. The contracts under test, one per acceptance item in #816:

1. the vault-location condition and its wording are the same on every surface,
   and the remedy names the ways `vault-relocate --apply` can refuse;
2. the audit reports a links finding the Home card cannot yet know about, and
   nothing Home does can silence it;
3. the two surfaces ask one question about the wikilink dialect and cannot
   answer it differently — including for a **scratch** vault holding a
   hand-written wikilink, which is the diagnostic the audit must not lose;
4. the wikilink notice is not an update-task catalog row, because it has no
   honest completion receipt — its receipt is per install, not per vault;
5. a Home poll opens no file under the vault, at any vault size, and the scan
   that establishes the verdict runs off the event loop.

The vault-location notice became a catalog row in #800's last slice, which is why
item 4 is one notice rather than two. Its own parity is pinned in
`tests/test_vault_relocate_update_task.py`, and the two places the sides
deliberately differ — an unresolvable registry entry (here: skipped, there:
`unknown`) and the Home tile that no longer exists — are asserted below.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ciao import migration_notices, update_tasks
from ciao.async_reads import run_read
from ciao.migration_notices import (
    LINKS_CLEAN,
    LINKS_FAILED,
    LINKS_FOUND,
    LINKS_SCAN_TTL_S,
    RELOCATION_MISPLACED,
    UNMIGRATED_LINKS_NOTICE,
    UNREHOMED_PEOPLE_NOTICE,
    VAULT_LOCATION_NOTICE,
    LinksFinding,
    cached_links,
    links_scan_is_stale,
    refresh_links,
    relocation_state,
    reset_links_cache,
    resolve_links,
    rehomed_people_finding,
    start_links_scan,
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


def test_the_card_and_the_audit_agree_on_the_misplaced_vault(
    tmp_path: Path,
) -> None:
    """The duplicated pair resolves to one implementation and one sentence.

    Both surfaces read the same findings, so the detail and the title come from
    one object — a mismatch here means one of them still holds a copy.

    The Home **tile** that used to be the second surface is gone since #800's last
    slice (it became the `vault-relocate` catalog task), so the parity to pin is
    between the audit's report and the task's detector: the same predicate, one
    per workspace, so they cannot name different workspaces as misplaced.
    """
    elsewhere = tmp_path / "elsewhere" / "personal"
    elsewhere.mkdir(parents=True)
    config = _cfg(tmp_path, roots={"personal": elsewhere})
    runtime = _runtime(tmp_path)

    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == VAULT_LOCATION_NOTICE
    )
    finding = vault_location_findings(config)[0]
    assert notice["detail"] == finding.detail
    assert relocation_state(config, "personal") == RELOCATION_MISPLACED
    # The audit's remedy used to describe moving the folder and hand-editing the
    # registry by hand. The managed sentence is what both the report and the task's
    # packaged prompt carry now, so the command is in both and the hand path in
    # neither.
    assert "ciao vault-relocate personal --apply" in notice["remedy"]
    assert "ciao vault-relocate personal --undo" in notice["remedy"]


def test_the_shared_remedy_names_every_shape_apply_can_refuse(tmp_path: Path) -> None:
    """Both surfaces state the refusals, not just the one that opens a chat.

    The list is read off `vault_relocate.plan` and `relocate`, and a shape added
    there without being named here would leave an operator on one surface reading
    a command that refuses them with a reason the other surface never mentioned.
    """
    remedy = vault_location_findings(_moved(tmp_path))[0].remedy

    for shape in (
        "symlink",
        "not empty",
        "install root",
        "more than one workspace",
        "another workspace's root",
        "nested under the vault",
        "uncommitted changes",
        "git worktree",
    ):
        assert shape in remedy, shape


def test_the_remedy_does_not_promise_a_way_forward_it_cannot_give(
    tmp_path: Path,
) -> None:
    """Most of those refusals only report what would not be done.

    `vault_relocate` names a route for three of them — the install-root shape and
    the outside-the-worktree one ("relocate it by hand"), and a destination nested
    under the vault ("repoint the workspace's vault_root first"). The rest only say
    what would not happen, so a remedy claiming they all say what to do next is a
    promise the command does not keep. This test is the guard on that sentence: an
    earlier version of it made exactly that claim.
    """
    remedy = vault_location_findings(_moved(tmp_path))[0].remedy

    assert "states what to do next" not in remedy
    assert "finished by hand" in remedy
    assert "report only what would not be done" in remedy
    # The preview is the authority on which shape this is, and the copy says so
    # rather than pre-empting it.
    assert "the preview says which shape applies" in remedy


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
    # And the audit reports exactly those two, from the same predicate.
    assert {
        n["workspace"]
        for n in audit_upgrade_notices(config, runtime_dir=_runtime(tmp_path))["notices"]
        if n["type"] == VAULT_LOCATION_NOTICE
    } == {"personal", "work"}


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
    assert {
        n["workspace"]
        for n in audit_upgrade_notices(config, runtime_dir=_runtime(tmp_path))["notices"]
        if n["type"] == VAULT_LOCATION_NOTICE
    } == {"personal"}
    # The `vault-relocate` task asks the same registry and does NOT inherit the
    # skip: for a card, an entry it could not read is `unknown`, not "nothing to
    # do". Asserted here because it is the one place the two sides' error
    # handling deliberately differs, and the difference is invisible otherwise.
    assert relocation_state(config, "personal") == RELOCATION_MISPLACED
    with pytest.raises(ValueError, match="does not resolve"):
        relocation_state(config, "work")
    result = update_tasks.apply_detector(
        _vault_relocate_task(), config=config, workspace="work"
    )
    assert result.status == update_tasks.UNKNOWN


def _vault_relocate_task() -> Any:
    """The packaged `vault-relocate` row, or a loud failure if it stopped shipping."""
    from ciao.update_task_catalog import load_catalog

    task = load_catalog().by_id.get("vault-relocate")
    assert task is not None, "vault-relocate is not in the packaged catalog"
    return task


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


# -- a failed scan is an unknown, and an unknown is not retried per poll ------


def test_a_failed_walk_is_cached_as_failed_and_never_as_clean(
    tmp_path: Path,
) -> None:
    """The three states are not interchangeable, and this is the one that matters.

    A vault that could not be walked is not a vault with nothing in it. Recording
    the failure as ``clean`` would make the notice go quiet on the one thing it
    exists to catch, so it is recorded as its own state: no card, and no claim.
    """
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    with patch(
        "ciao.vault_migrate_links.has_unmigrated_links",
        side_effect=OSError("vault is unreadable"),
    ):
        assert resolve_links(config, runtime) is None

    assert cached_links(config, runtime) is None, "a failure is not a finding"
    assert _stored_state(config.vault_root) == LINKS_FAILED
    assert _stored_state(config.vault_root) != LINKS_CLEAN


def test_a_failed_walk_is_not_retried_on_every_poll(tmp_path: Path) -> None:
    """The window is what stops a broken vault costing a walk every 60 seconds.

    Without a recorded failure the strip would re-walk and re-log on every poll
    for as long as the vault stayed unreadable, which is the one cost this
    design exists to avoid.
    """
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    with patch(
        "ciao.vault_migrate_links.has_unmigrated_links",
        side_effect=OSError("vault is unreadable"),
    ) as walk:
        assert resolve_links(config, runtime, now=1000.0) is None
        assert links_scan_is_stale(config, runtime, now=1000.0) is False
        assert links_scan_is_stale(config, runtime, now=1000.0 + 60) is False
        assert links_scan_is_stale(config, runtime, now=1000.0 + 300) is True
    # Two walks in two windows, not one per poll.
    assert walk.call_count == 1


def test_an_unreadable_receipt_is_a_failure_not_a_clean_vault(tmp_path: Path) -> None:
    """`read_receipt` blowing up is unknown, and the token still identifies it.

    The scope is known — the runtime root and the vault are both there — so the
    failure is cacheable, and caching it is what stops the same exception being
    logged on every poll.
    """
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text("{ not json", encoding="utf-8")

    with patch(
        "ciao.vault_migrate_links.read_receipt",
        side_effect=RuntimeError("receipt read exploded"),
    ):
        assert resolve_links(config, runtime) is None
        assert links_scan_is_stale(config, runtime) is False
    assert _stored_state(config.vault_root) == LINKS_FAILED


def _stored_state(vault: Path) -> str:
    with migration_notices._CACHE_LOCK:
        entry = migration_notices._CACHE.get(str(vault))
    assert entry is not None, "nothing was published for this vault"
    return entry.state


# -- (4) the wikilink notice is still not a catalog task ----------------------


def test_the_links_notice_is_still_not_an_update_task() -> None:
    """No catalog row without an honest completion receipt (#800, step 3).

    This notice is still shape 3: the only "evidence" that the work was done is the
    condition's absence recomputed each render, which makes a completion check a
    tautology. `vault-relocate` was promoted out of shape 3 in #800's last slice
    because it turned out to write a real receipt — this test's premise about it
    was wrong, and that is the correction the sibling test file records — but the
    link migration's receipt is per **install**, not per vault, so a
    per-workspace loop over its remedy is not available and promoting it would
    still be claiming completion from a card. When that changes, the row's
    completion check must name a receipt that records the run.
    """
    from ciao.update_task_catalog import load_catalog

    catalog = load_catalog()
    assert not [
        task
        for task in catalog.tasks
        if UNMIGRATED_LINKS_NOTICE in {task.id, task.detector}
        or "links" in task.id
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


# -- the detached task's whole life ------------------------------------------


class _State:
    """Whatever holds the task handle — Starlette's `app.state` in the app."""

    def __init__(self) -> None:
        self.links_scan_task: asyncio.Task[None] | None = None


async def test_a_cold_poll_starts_one_detached_scan_and_finishes_with_a_card(
    tmp_path: Path,
) -> None:
    """The whole cold path, in the order the PWA will meet it.

    Nothing established yet, so the strip answers with no card and a scan running;
    the scan lands; the next poll answers with a card that names the note. That
    sequence is the design, so it is asserted end to end rather than by asserting
    that a helper was called.
    """
    config = _install_at(tmp_path, 5, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    state = _State()
    context = DetectionContext(config=config, runtime_dir=runtime)

    assert start_links_scan(state, config, runtime) is True
    assert _cards(context) == [], "the first poll cannot have the verdict yet"

    await state.links_scan_task  # type: ignore[union-attr]

    card = _cards(context)
    assert [c.id for c in card] == ["vault-unmigrated-links"]
    assert "wikilinked.md" in card[0].detail
    # And the answer is stored, so the next poll starts nothing.
    assert start_links_scan(state, config, runtime) is False


def _cards(context: DetectionContext) -> list[Any]:
    return [a for a in detect_actions(context) if a.kind == "unmigrated-links"]


async def test_a_second_poll_does_not_start_a_second_scan(tmp_path: Path) -> None:
    """One in flight at a time, however fast the polls arrive."""
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    state = _State()
    release = asyncio.Event()

    async def _slow_scan(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()
        resolve_links(config, runtime)

    with patch("ciao.migration_notices.refresh_links", side_effect=_slow_scan):
        assert start_links_scan(state, config, runtime) is True
        await asyncio.sleep(0)
        assert start_links_scan(state, config, runtime) is False
        release.set()
        await state.links_scan_task  # type: ignore[union-attr]

    assert cached_links(config, runtime) is not None


async def test_a_failing_scan_is_observed_rather_than_left_for_the_gc(
    tmp_path: Path,
) -> None:
    """A detached task's exception is retrieved here, or it surfaces nowhere.

    An executor closed under a restart is the realistic cause. Left alone it would
    be reported by the garbage collector at some unrelated moment as "Task
    exception was never retrieved", which is the failure mode
    `async_reads._observe_failure` exists to prevent for its own workers.
    """
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    state = _State()

    async def _broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("executor is closed")

    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        with patch("ciao.migration_notices.refresh_links", side_effect=_broken):
            with patch.object(migration_notices.logger, "warning") as warned:
                start_links_scan(state, config, runtime)
                task = state.links_scan_task
                assert task is not None
                # `asyncio.wait` rather than `await task`: awaiting would re-raise
                # here, which says nothing about what the loop does with a task
                # nobody awaits.
                await asyncio.wait([task])
                await asyncio.sleep(0)  # let the done-callback run
    finally:
        loop.set_exception_handler(previous)

    # The failure was logged, and it was retrieved, so the loop's own "Task
    # exception was never retrieved" report has nothing left to say.
    assert warned.called
    assert task.done() and not task.cancelled()
    assert unhandled == []


async def test_shutdown_cancels_a_scan_in_flight_and_waits_for_it(
    tmp_path: Path,
) -> None:
    """A restart must not leave a task pending on a closing loop.

    Cancelling detaches the awaiter only — `run_read`'s worker runs to completion
    and stays joinable by a later caller — so what this asserts is that the task
    is finished and acknowledged, and that a shutdown with nothing in flight is a
    no-op rather than an error.
    """
    config = _install_at(tmp_path, 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    state = _State()
    release = asyncio.Event()

    async def _slow_scan(*_args: Any, **_kwargs: Any) -> None:
        await release.wait()
        resolve_links(config, runtime)

    with patch("ciao.migration_notices.refresh_links", side_effect=_slow_scan):
        start_links_scan(state, config, runtime)
        task = state.links_scan_task
        assert task is not None
        await asyncio.sleep(0)

        await migration_notices.shutdown_links_scan(state)
        assert task.done()
        assert task.cancelled()

        # Nothing in flight, and a task that failed: neither may raise.
        await migration_notices.shutdown_links_scan(state)
        state.links_scan_task = asyncio.create_task(_fails())
        await asyncio.sleep(0)
        await migration_notices.shutdown_links_scan(state)


async def _fails() -> None:
    raise RuntimeError("boom")


async def test_the_shutdown_callback_is_registered_before_the_executor_closes(
    tmp_path: Path,
) -> None:
    """The scan waits on a worker; closing the pool under it is the leak avoided.

    `ciao/main.py` owns the teardown order, so the order is asserted here rather
    than left to a reader of two files.
    """
    source = (Path(__file__).resolve().parents[1] / "ciao" / "main.py").read_text(
        encoding="utf-8"
    )
    block = source.split("app.state.shutdown_callbacks = [", 1)[1].split("]", 1)[0]
    assert "_shutdown_links_scan" in block
    assert block.index("_shutdown_links_scan") < block.index("_shutdown_vault_reads")
    assert "shutdown_links_scan(app.state)" in source


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
    # The vault-location predicate has two callers since #800's last slice: the
    # audit, and the `vault-relocate` catalog task that replaced the Home tile.
    # Both name this module's object rather than re-deriving the condition, and
    # the tile is gone — so there is one report, one card, and one predicate.
    assert os_audit.vault_location_findings is vault_location_findings
    assert not hasattr(operator_actions, "vault_location_findings")
    # The tile is gone, so the Home strip cannot have kept a private copy either.
    assert "vault_location_findings" not in Path(operator_actions.__file__).read_text(
        encoding="utf-8"
    )
    assert os_audit.VAULT_LOCATION_NOTICE == VAULT_LOCATION_NOTICE
    assert os_audit.UNMIGRATED_LINKS_NOTICE == UNMIGRATED_LINKS_NOTICE
    assert os_audit.resolve_links is resolve_links
    # The re-home notice is the third shared probe, and it is named the same way.
    assert os_audit.UNREHOMED_PEOPLE_NOTICE == UNREHOMED_PEOPLE_NOTICE
    assert os_audit.rehomed_people_finding is rehomed_people_finding
    # And the relocation task asks this module too, rather than restating the
    # condition in `update_tasks` — which is what keeps the card and the report
    # from naming different workspaces.
    source = inspect.getsource(update_tasks)
    assert "relocation_state" in source
    assert "canonical_workspace_vault_root(" not in source

    package = Path(migration_notices.__file__).parent
    for module in ("operator_actions.py", "os_audit.py"):
        text = (package / module).read_text(encoding="utf-8")
        assert "canonical_workspace_vault_root(" not in text, module
        assert "has_unmigrated_links" not in text, module
        # The wikilink receipt belongs to the shared applicability, not to a
        # surface. (`read_receipt(runtime)` also appears in the re-rooting gate,
        # which is a mandatory notice and deliberately not part of this pair.)
        assert "vault_migrate_links import read_receipt" not in text, module


def test_only_the_task_probe_may_plan_the_re_home() -> None:
    """The planning probe is off limits to everything a poll or a report can reach.

    The re-home task's applicability costs a `plan_rehome` walk, which is
    affordable exactly once per `APPLICABILITY_TTL_S` because `update_tasks`
    runs it off the loop and only `update_tasks` calls it. The Home strip polls
    every 60s and the audit runs on every app open, so if either ever reached the
    walking probe the cost class of this module would change with nothing failing.
    Checked through the modules rather than by reading them, so a detector added
    to either surface fails here.
    """
    from ciao import operator_actions, os_audit

    for module in (operator_actions, os_audit):
        symbols = _module_symbols(Path(module.__file__))
        assert "plan_rehome" not in symbols, module.__name__
        assert "rehome_legacy_candidates" not in symbols, module.__name__
        assert "resolve_role_workspaces" not in symbols, module.__name__

    from ciao import update_tasks

    symbols = _module_symbols(Path(update_tasks.__file__))
    assert "rehome_legacy_candidates" in symbols
    assert "rehomed_people_finding" not in symbols, (
        "the task must ask the harder question; reading the notice's condition "
        "here is the false positive #833's review found"
    )


def test_the_shared_module_cannot_see_a_task_record() -> None:
    """No probe here may read an update-task lifecycle, or a dismissal silences the audit.

    The one structural guarantee behind "a dismissed card silences the card and
    never the report": a suppression flag added to a shared probe would make the
    two surfaces agree by making the diagnostic lie. So the shared module must not
    reference the update-task layer at all — not even lazily. Read through the AST
    rather than as text, so a docstring saying the same thing does not trip it.
    """
    symbols = _module_symbols(Path(migration_notices.__file__))

    assert not [s for s in symbols if s.startswith("update_tasks")], sorted(symbols)
    assert "INSTALL_STATE_FILENAME" not in symbols
    assert "record_dismissal" not in symbols
    assert "read_task_state" not in symbols


def _module_symbols(path: Path) -> set[str]:
    """Every name a module's *code* imports, reads or calls. Docstrings excluded.

    `ast.Constant` is not collected, so prose — including the sentence in this
    module's own docstring that says why it must not do this — cannot make the
    assertion pass or fail for the wrong reason.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
            out.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


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
