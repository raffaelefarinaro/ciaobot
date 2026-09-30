"""The migration notices, probed once and shared by both surfaces (#816).

`ciao/migration_notices.py` owns the condition, the applicability rule and the
wording for the two notices Home and the OS audit used to keep separate copies
of. The contracts under test, one per acceptance item in #816:

1. the vault-location condition and its wording are the same on both surfaces;
2. the audit keeps reporting a links finding the Home card does not carry, and
   nothing Home does can silence it;
3. the two surfaces cannot disagree about whether the wikilink dialect is in
   scope;
4. neither notice is an update-task catalog row, because neither has an honest
   completion receipt yet;
5. a Home poll touches no file under the vault, at any vault size.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from ciao import migration_notices
from ciao.migration_notices import (
    UNMIGRATED_LINKS_NOTICE,
    VAULT_LOCATION_NOTICE,
    LinksFinding,
    unmigrated_links,
    vault_location_findings,
)
from ciao.operator_actions import DetectionContext, detect_actions, dismiss_action
from ciao.os_audit import audit_upgrade_notices


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
    return {n["type"] for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]}


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


def test_the_audit_reports_a_finding_the_home_card_does_not_carry(
    tmp_path: Path,
) -> None:
    """Sharing a probe must not turn the audit into a mirror of the strip.

    Home's card is a pointer: the walk that establishes a wikilink is not
    available on the poll path, so the card says the dialect *may* be in use. The
    audit runs the same probe with the example established and names a note. The
    temptation this pins is making the shared probe's applicability depend on
    which surface asked — the cheapest way to stop the two surfaces disagreeing
    is to let the surface that cannot afford the work declare the condition out of
    scope, and that deletes a true finding from a diagnostic.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 3, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    tile = next(
        a for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
        if a.kind == "unmigrated-links"
    )
    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == UNMIGRATED_LINKS_NOTICE
    )

    assert "may still" in tile.title
    assert "wikilinked.md" not in tile.detail
    assert "wikilinked.md" in notice["detail"]


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

    with pytest.raises(ValueError):
        dismiss_action("vault-unmigrated-links", context)

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
    assert "vault-unmigrated-links" in {a.id for a in detect_actions(context)}


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
    assert "vault-unmigrated-links" in {
        a.id for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
    }


# -- (3) one applicability rule for the wikilink dialect ---------------------


def test_a_completed_receipt_retires_the_notice_on_both_surfaces(
    tmp_path: Path,
) -> None:
    from ciao.vault_migrate_links import write_receipt

    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    write_receipt(runtime, {"vault_root": str(config.vault_root), "files_rewritten": 2})

    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, runtime)
    assert "vault-unmigrated-links" not in {
        a.id for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
    }


def test_a_config_without_a_declared_mode_is_out_of_scope(tmp_path: Path) -> None:
    """An undeclared mode means a vault Ciaobot created, so the dialect never applied.

    A real `CiaoConfig` always declares it, so this only decides how a stub or a
    programmatic caller is read — and it has to be decided the same way on both
    surfaces, which is the whole point.
    """
    config = _cfg(tmp_path)
    del config.vault_mode
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")

    assert unmigrated_links(config, _runtime(tmp_path)) is None
    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, _runtime(tmp_path))


def test_without_a_runtime_root_nothing_guesses(tmp_path: Path) -> None:
    """The receipt lives under the runtime root; no root, no answer either way."""
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")

    assert unmigrated_links(config, None) is None
    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, None)


def test_a_scratch_vault_reaches_zero_on_both_surfaces(tmp_path: Path) -> None:
    """A conformant vault is not nagged, and its card cannot be permanent."""
    config = _cfg(tmp_path, vault_mode="scratch")
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)

    assert unmigrated_links(config, runtime) is None
    assert UNMIGRATED_LINKS_NOTICE not in _audit_types(config, runtime)
    assert "vault-unmigrated-links" not in {
        a.id for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
    }


def test_an_empty_example_is_an_unanswered_question_not_a_clean_vault(
    tmp_path: Path,
) -> None:
    """The wording is the guard against a receipt's absence becoming a claim."""
    unanswered = LinksFinding(vault_root=tmp_path)
    answered = LinksFinding(vault_root=tmp_path, example="personal/a.md")

    assert unanswered.established is False
    assert "may still" in unanswered.title
    assert "may still contain" in unanswered.detail
    assert answered.established is True
    assert "still uses" in answered.title
    assert "personal/a.md" in answered.detail
    # One remedy either way: the preview finds nothing on a clean vault, so the
    # way back and the way forward are the same sentence in both cases.
    assert unanswered.remedy == answered.remedy


def test_a_receipt_the_install_cannot_read_is_raised_on_both_surfaces(
    tmp_path: Path,
) -> None:
    """Unreadable is not "migrated", and both surfaces have to say so alike.

    `read_receipt` returns `None` for a receipt it cannot parse, so an
    unreadable one leaves the notice raised — the fail-safe direction, since a
    migration that is reported as done is the failure that leaves wikilinks in a
    vault for good. What matters here is that both surfaces reach that reading
    through the one function, rather than one treating unreadable as complete and
    the other as absent.
    """
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text("{ not json", encoding="utf-8")

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
    assert "vault-unmigrated-links" in {
        a.id for a in detect_actions(DetectionContext(config=config, runtime_dir=runtime))
    }


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


# -- (5) a Home poll never walks the vault -----------------------------------


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


def _install_at(root: Path, count: int) -> SimpleNamespace:
    """A config whose one adopted workspace holds `count` notes and a wikilink."""
    config = _cfg(root)
    _notes(config.vault_root / "personal", count, wikilink="People/Peter")
    return config


def test_the_read_recorder_sees_a_walk(tmp_path: Path) -> None:
    """The performance test below is only worth anything if this holds.

    If the recorder stopped watching the accessor a walk uses, the poll count
    would stay zero for the wrong reason and the bound would be untested.
    """
    config = _install_at(tmp_path, 3)
    vault = config.vault_root / "personal"

    from ciao.vault_migrate_links import has_unmigrated_links

    assert _vault_reads_under(vault, lambda: has_unmigrated_links(vault))


def test_a_home_poll_opens_nothing_under_the_vault(tmp_path: Path) -> None:
    """The bound is a count, not a duration, and it does not scale with the vault.

    The wikilink notice is the one condition whose accurate answer needs a walk,
    so this is where the poll would have paid for it. A registry value, a receipt
    and an `is_dir()` are the only things a pass may read, and none of them
    touches a note. The small and large vaults are both checked because "cheap"
    has to mean independent of the vault, not merely small enough to measure.
    """
    small = tmp_path / "small"
    _install_at(small, 1)
    big = tmp_path / "big"
    _install_at(big, 400)

    for root, count in ((small, 1), (big, 400)):
        config = _cfg(root)
        runtime = _runtime(root)
        context = DetectionContext(config=config, runtime_dir=runtime)

        def _poll() -> None:
            # One pass per poll tick, plus the window-focus pass on top of it.
            for _ in range(3):
                detect_actions(context)

        assert _vault_reads_under(config.vault_root / "personal", _poll) == [], count


def test_the_links_walk_is_the_audit_halfs_to_pay_for(tmp_path: Path) -> None:
    """One walk, on the surface that can afford it, and only where it was asked for."""
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 2, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    context = DetectionContext(config=config, runtime_dir=runtime)

    with patch(
        "ciao.vault_migrate_links.has_unmigrated_links",
        side_effect=AssertionError("the poll path walked the vault"),
    ) as walk:
        detect_actions(context)
    walk.assert_not_called()

    # The audit is the caller that establishes the example, so it is the one that
    # walks, and having walked it names the note it found.
    notice = next(
        n for n in audit_upgrade_notices(config, runtime_dir=runtime)["notices"]
        if n["type"] == UNMIGRATED_LINKS_NOTICE
    )
    assert "wikilinked.md" in notice["detail"]


def test_neither_surface_keeps_a_second_implementation() -> None:
    """A duplicate predicate is the exact regression #816 was filed for.

    `operator_actions` and `os_audit` both name this module's probes and its
    notice types — same objects, not re-declared strings — and neither resolves a
    canonical vault root or walks for wikilinks of its own. Duplicated *logic* is
    what this removes; duplicated *cards* were never the problem.
    """
    from ciao import operator_actions, os_audit

    assert operator_actions.unmigrated_links is unmigrated_links
    assert operator_actions.vault_location_findings is vault_location_findings
    assert os_audit.VAULT_LOCATION_NOTICE == VAULT_LOCATION_NOTICE
    assert os_audit.UNMIGRATED_LINKS_NOTICE == UNMIGRATED_LINKS_NOTICE

    package = Path(migration_notices.__file__).parent
    for module in ("operator_actions.py", "os_audit.py"):
        text = (package / module).read_text(encoding="utf-8")
        assert "canonical_workspace_vault_root(" not in text, module
        assert "has_unmigrated_links" not in text, module
        # The wikilink receipt belongs to the shared applicability, not to a
        # surface. (`read_receipt(runtime)` also appears in the re-rooting gate,
        # which is a mandatory notice and deliberately not part of this pair.)
        assert "vault_migrate_links import read_receipt" not in text, module


def test_a_json_receipt_that_is_not_an_object_does_not_silence_the_notice(
    tmp_path: Path,
) -> None:
    """`read_receipt` gates on shape as well as status; a list is not a migration."""
    config = _cfg(tmp_path)
    _notes(config.vault_root / "personal", 1, wikilink="People/Peter")
    runtime = _runtime(tmp_path)
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    (runtime / "migration" / "vault-links.json").write_text(
        json.dumps(["not", "a", "receipt"]), encoding="utf-8"
    )

    assert UNMIGRATED_LINKS_NOTICE in _audit_types(config, runtime)
