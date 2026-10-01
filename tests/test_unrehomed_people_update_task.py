"""The install-scoped ``unrehomed-people`` update task (#833).

`unrehomed_people` was the one migration notice with no Home surface at all: the
OS audit reported it, nothing else did, and the catalog that #737 introduced had
no row for it. It is now a catalog task — the first notice whose remedy writes a
receipt a completion check can read — and this file pins the seven properties that
make it honest. Two of them exist because the first pass got them wrong, and both
failures were silent:

1. **A fresh install gets no task.** The notice's own condition is a *receipt
   check*, and a fresh conforming install passes it for free — more than one
   workspace, no receipt, no legacy data at all. A card offered there is the
   false positive #800's table rules out by name, so applicability comes from the
   migration's own plan instead: only a tag-obvious cross-workspace **mechanical**
   candidate is evidence, and untagged contacts are not.
2. **The audit stays broad and advisory.** The task is *narrower* than the
   notice, never wider, and a completed receipt silences both because both read
   the same accessor.
3. **Completion is the completed receipt and nothing else**: missing, partial,
   failed, unreadable and unparseable receipts are unfinished work, and the
   truthful no-op receipt counts only as a record of an *inspection* the operator
   already approved — never as the proof that the task applies.
4. **The audit keeps reporting** whatever the task's record says, because a record
   is not an input to the notice.
5. **A per-workspace-root layout gets no task**, and does not pay for a walk to
   learn that: `ciao vault-rehome` plans `<vault>/<workspace>/People`, so there is
   no move it can make there and no managed remedy that can make one.
6. **The one walk this probe does costs what the layer promises** — off the event
   loop, through the bounded read executor, at most once per freshness window —
   and every install where the answer can be proved without reading a note never
   reaches it.
7. **Home grows exactly one card** for this notice, not a tile beside the task.

Every fixture is a temp directory. No real vault, no runtime, no service, no
installer, and nothing is migrated for real outside the temp tree.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ciao import __version__, async_reads, update_tasks, vault_rehome
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.migration_notices import (
    REHOME_ALREADY_COMPLETED,
    REHOME_NO_CANDIDATES,
    REHOME_NO_ROLE_BINDING,
    REHOME_NO_SHARED_VAULT,
    REHOME_SINGLE_WORKSPACE,
    UNREHOMED_PEOPLE_NOTICE,
    completed_rehome,
    rehome_legacy_candidates,
    rehomed_people_finding,
    reset_links_cache,
)
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.os_audit import audit_upgrade_notices
from ciao.update_task_catalog import DETECTORS, COMPLETION_CHECKS, load_catalog, read_prompt
from ciao.update_tasks import APPLICABLE, NOT_APPLICABLE, UNKNOWN

TASK_ID = "unrehomed-people"


@pytest.fixture(autouse=True)
def _clean_process_state():
    """Both module-level caches are process-wide, so neither may leak between tests."""
    update_tasks.clear_applicability_cache()
    reset_links_cache()
    yield
    update_tasks.clear_applicability_cache()
    reset_links_cache()
    async_reads.reset_vault_read_executor()


@dataclass(frozen=True)
class Install:
    """A two-workspace synthetic install on the shared layout."""

    root: Path
    config: CiaoConfig

    @property
    def vault(self) -> Path:
        return self.root / "memory-vault"

    @property
    def runtime(self) -> Path:
        return self.root / ".runtime"


def _install(tmp_path: Path, *, workspaces: tuple[str, ...] = ("personal", "work")) -> Install:
    """One install with the given registered workspaces and an empty shared vault.

    Registered explicitly rather than derived from vault directories, because the
    detector passes ``config.workspace_names()`` to the plan — ``plan_rehome``'s
    own docstring says a caller with a config should, and that a stray directory
    is not a workspace.
    """
    root = tmp_path / "install"
    vault = root / "memory-vault"
    runtime = root / ".runtime"
    for name in workspaces:
        (vault / name / "People").mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "state.json").write_text("{}\n", encoding="utf-8")
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=root,
        vault_root=vault,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in workspaces
        },
    )
    return Install(root=root, config=config)


def _rerooted(install: Install) -> Install:
    """The same install after `ciao workspace-reroot`: no shared vault at the root.

    What the migration leaves is a directory that does not exist and per-workspace
    roots that hold their own vaults — `CiaoConfig.vault_scan_targets` says exactly
    this in its docstring, and `operator_actions._detect_workspace_unmigrated`
    gates the same way. Only the directory matters here, so that is what moves.
    """
    import shutil

    shutil.rmtree(install.vault)
    for name in install.config.workspace_names():
        (install.root / name / "memory-vault" / "People").mkdir(parents=True)
    return install


def _note(vault: Path, relative: str, body: str) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _misfiled_person(vault: Path) -> Path:
    """One work contact sitting in the personal workspace's `People/`, referenced.

    The shape the whole migration exists for: `colleague` names the work
    workspace, so the move is mechanical, and the inbound link is what makes the
    move more than a file rename.
    """
    _note(
        vault,
        "personal/People/Mo.md",
        "---\ntype: person\ntags: [person, colleague]\n---\n# Mo\n",
    )
    return _note(
        vault,
        "personal/Projects/Foo.md",
        "---\ntype: project\nrelated:\n  - personal/People/Mo\n---\n"
        "# Foo\n\nOwner [[personal/People/Mo|Mo]].\n",
    )


def _person(vault: Path, relative: str, tags: str = "[person]") -> Path:
    """A person note with the given tags."""
    return _note(vault, relative, f"---\ntype: person\ntags: {tags}\n---\n# Someone\n")


def _write_receipt(runtime: Path, payload: dict[str, Any]) -> Path:
    path = vault_rehome.receipt_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _receipt(status: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "rehomed_at": "2026-08-19T00:00:00Z",
        "moves": [{"from": "personal/People/Mo.md", "to": "work/People/Mo.md"}],
        "rewrites": [],
    }
    if status is not None:
        payload["status"] = status
    return payload


def _task() -> Any:
    """The packaged row, or a loud failure if it stopped shipping."""
    task = load_catalog().by_id.get(TASK_ID)
    assert task is not None, f"{TASK_ID} is not in the packaged catalog"
    return task


def _detector(install: Install) -> Any:
    return update_tasks.apply_detector(_task(), config=install.config, workspace="personal")


def _check(install: Install) -> Any:
    check = update_tasks.COMPLETION_FUNCTIONS[_task().completion_check]
    return check(config=install.config, workspace="personal", today=None)


def _audit_notices(install: Install) -> list[dict[str, Any]]:
    return audit_upgrade_notices(install.config, runtime_dir=install.runtime)["notices"]


def _rehome_notices(install: Install) -> list[dict[str, Any]]:
    return [n for n in _audit_notices(install) if n["type"] == UNREHOMED_PEOPLE_NOTICE]


def _rehome(install: Install, *, apply: bool = True) -> dict[str, Any]:
    """The managed command, as the CLI calls it: the shared vault, registry names."""
    return vault_rehome.rehome_people(
        install.vault,
        install.runtime,
        apply=apply,
        workspaces=list(install.config.workspace_names()),
    )


def _evaluate(install: Install) -> list[Any]:
    """One listing through the public path, from a clean applicability cache.

    The cache is cleared between the two listings in the settlement test on
    purpose: that is the one thing the production window cannot do for a record
    whose evidence landed after the previous check, and the point here is the
    settlement, not the clock.
    """
    update_tasks.clear_applicability_cache()
    return asyncio.run(
        update_tasks.evaluate(
            install.config,
            workspace="personal",
            installed_version=__version__,
        )
    )


# ── 1. a fresh install is not an install with work in it ─────────────────────


def test_a_fresh_conforming_install_is_offered_nothing(tmp_path: Path) -> None:
    """Two workspaces, no receipt, an empty vault: no task, and the audit still says so.

    This is the regression the review caught and the reason the detector changed.
    The notice's condition is satisfied here for free — the install never needed a
    migration — so a task built on it was offered on exactly this install. The
    card now needs a plan with a move in it, and there is nothing to move.
    """
    install = _install(tmp_path)

    result = _detector(install)

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == REHOME_NO_CANDIDATES
    assert result.evidence["mechanical"] == []
    # The advisory half is unchanged: the notice is about the receipt, not the vault.
    assert _rehome_notices(install)


def test_an_ordinary_contacts_folder_is_not_evidence_of_legacy_damage(
    tmp_path: Path,
) -> None:
    """Contacts whose tags name no workspace are normal, and prove nothing.

    Untagged notes bucket as `needs_judgement` — a question about a relationship,
    which the command never moves — and a personal note tagged `personal` in the
    personal workspace is not a candidate at all. A detector that treated either
    as evidence would offer the task on any install with a populated `People/`,
    which is the false positive with a new justification.
    """
    install = _install(tmp_path)
    _person(install.vault, "personal/People/Ida.md")
    _person(install.vault, "work/People/Kai.md", tags="[person, colleague]")
    _person(install.vault, "personal/People/Lou.md", tags="[person, friend]")

    result = _detector(install)

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == REHOME_NO_CANDIDATES
    # The judgement bucket is reported as a count and offered as nothing.
    assert result.evidence["needs_judgement"] >= 1
    assert result.evidence["mechanical"] == []


def test_a_real_legacy_candidate_offers_the_task(tmp_path: Path) -> None:
    """One tag-obvious cross-workspace move is the evidence the task needs.

    The candidate list is vault-relative, so it is portable evidence and the
    fingerprint moves with it.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    result = _detector(install)

    assert result.status == APPLICABLE
    assert result.evidence["reason"] != REHOME_NO_CANDIDATES
    assert result.evidence["mechanical"] == ["personal/People/Mo.md"]
    assert result.evidence["scanned"] is True
    assert _rehome_notices(install)


def test_a_conflict_is_counted_but_never_offered(tmp_path: Path) -> None:
    """A destination already occupied is a content decision, so nothing is offered.

    ``plan_rehome`` reports the candidate under `conflicts` rather than
    `mechanical`. Offering it would be offering a move the command refuses, and
    the refusal is the right one: which of two people keeps that filename is not
    this engine's decision.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    _note(
        install.vault,
        "work/People/Mo.md",
        "---\ntype: person\ntags: [person]\n---\n# Someone else entirely\n",
    )

    evidence = rehome_legacy_candidates(install.config, install.runtime)

    assert evidence.mechanical == ()
    assert evidence.conflicts == 1
    assert _detector(install).status == NOT_APPLICABLE


def test_the_task_is_never_offered_on_a_single_workspace_install(tmp_path: Path) -> None:
    """One workspace has no counterpart for a note to be misfiled *from*.

    ``detect_misfiled_people`` still buckets an untagged note as needing
    judgement with one workspace, but its destination comes back empty: there is
    no move to offer, and offering one is how a fresh install learns the strip is
    noise.
    """
    install = _install(tmp_path, workspaces=("personal",))
    _person(install.vault, "personal/People/Ida.md")

    assert _detector(install).evidence["reason"] == REHOME_SINGLE_WORKSPACE
    assert _detector(install).status == NOT_APPLICABLE


def test_workspaces_that_bind_no_tag_roles_cost_no_vault_read(tmp_path: Path) -> None:
    """One bound role means no mechanical candidate is reachable, so nothing is walked.

    ``detect_misfiled_people`` can only reach ``bucket == "mechanical"`` when a
    note's tag names a role bound to a workspace *other than* the note's own, so
    it needs two distinct roles bound to two distinct workspaces. An install named
    anything else cannot produce one whatever its notes say, and the common
    convention here is `clientA`/`clientB` — which is why the gate is a proof and
    not a heuristic. A test that puts a misfiled note in the vault and asserts the
    *scan did not happen* is what pins that; asserting only the absence of a task
    would pass just as well if the walk ran every time.

    The proof is checked against the command rather than asserted: the plan this
    gate skips finds no mechanical candidate either, so nothing the remedy could
    have done is being hidden. That is the difference between a gate and a
    narrowing, and it is the whole reason #800's rule is not satisfied by looking
    harder at the receipt.
    """
    install = _install(tmp_path, workspaces=("clientA", "clientB"))
    _misfiled_person(install.vault)

    assert _accesses_under(install.vault, lambda: _detector(install)) == []
    result = _detector(install)
    assert result.evidence["reason"] == REHOME_NO_ROLE_BINDING
    assert result.evidence["scanned"] is False

    plan = vault_rehome.plan_rehome(
        install.vault, workspaces=list(install.config.workspace_names())
    )
    assert plan["mechanical"] == [], "the gate hid a move the command would make"
    # Not a judgement case either: with no registered workspace playing that role
    # there is nowhere to put the note, so the command does not even queue it. The
    # gate is therefore losing nothing at all — there is no candidate of any kind.
    assert plan["needs_judgement"] == []


def test_a_rerooted_install_gets_no_task_and_no_walk(tmp_path: Path) -> None:
    """A per-workspace root has no workspace segment, so no managed move exists there.

    ``ciao vault-rehome`` plans ``<vault>/<workspace>/People``; after the
    re-rooting the notes are already per-workspace with no such segment, so the
    plan finds nothing and ``--workspace-name`` changes nothing. Advertising the
    task there would be offering a command that cannot act, permanently — the
    "offered forever" shape the #788 upkeep row is about.
    """
    install = _rerooted(_install(tmp_path))
    # Even a person note with a work tag, in the root that now holds it.
    _person(install.root / "personal" / "memory-vault", "People/Mo.md",
            tags="[person, colleague]")

    assert _accesses_under(install.vault, lambda: _detector(install)) == []
    result = _detector(install)
    assert result.evidence["reason"] == REHOME_NO_SHARED_VAULT
    assert result.status == NOT_APPLICABLE


# ── 2. the task is narrower than the notice, and only one way ────────────────


def test_a_completed_receipt_silences_both_surfaces(tmp_path: Path) -> None:
    """The one place the two answers must agree, and why they do.

    The notice is silent because a re-homing is recorded; the task is not
    applicable because the remedy refuses a second pass without ``--force``, so a
    card there would be work the operator cannot ask for. Both read
    :func:`completed_rehome`, so they cannot drift.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    assert _detector(install).status == APPLICABLE

    _write_receipt(install.runtime, _receipt("migrated"))

    result = _detector(install)
    assert result.evidence["reason"] == REHOME_ALREADY_COMPLETED
    assert result.status == NOT_APPLICABLE
    assert _rehome_notices(install) == []


def test_the_notice_keeps_reporting_where_the_task_stays_quiet(tmp_path: Path) -> None:
    """The asymmetry, in the direction that matters and the only one allowed.

    Task offered implies the audit reports; audit silent implies the task is not
    offered. The converse is deliberately false — an install whose notes are all
    correctly filed gets the notice and no card — because widening a task to match
    a diagnostic is what put this card on every fresh install.
    """
    install = _install(tmp_path)
    _person(install.vault, "personal/People/Ida.md")

    assert _detector(install).status == NOT_APPLICABLE
    assert _rehome_notices(install), "the advisory notice must stay broad"

    _misfiled_person(install.vault)
    assert _detector(install).status == APPLICABLE
    assert _rehome_notices(install)


def test_a_config_without_a_registry_has_nothing_to_offer() -> None:
    """A surface that cannot name the workspaces has nothing to say about them.

    The same rule `vault_location_findings` follows and the same one
    `audit_upgrade_notices` follows in its own early return: a config with no
    workspace registry is not a fault on a notice path, it is a surface with no
    answer. The `reason` is one bucket for "nothing to offer here", so it is
    asserted as a status and not read as a statement about any particular install.
    """
    from types import SimpleNamespace

    result = update_tasks.apply_detector(
        _task(), config=SimpleNamespace(state_path="/nonexistent/.runtime/state.json")
    )

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == REHOME_SINGLE_WORKSPACE


def test_evidence_this_install_cannot_read_is_unknown_never_not_applicable(
    tmp_path: Path,
) -> None:
    """A raise is an unknown, and an unknown must never be reported as "nothing to do".

    ``not_applicable`` here would be a positive claim that there is no legacy
    misfiling, made by a probe that never managed to look.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    with patch(
        "ciao.vault_rehome.read_receipt", side_effect=OSError("receipt unreadable")
    ):
        assert _detector(install).status == UNKNOWN

    with patch.object(
        CiaoConfig, "workspace_names", side_effect=RuntimeError("registry unreadable")
    ):
        assert _detector(install).status == UNKNOWN

    # And the audit stays a report: it drops the notice rather than failing.
    with patch(
        "ciao.vault_rehome.read_receipt", side_effect=OSError("receipt unreadable")
    ):
        assert _rehome_notices(install) == []


def test_a_config_with_no_vault_root_is_unknown_not_an_empty_answer(tmp_path: Path) -> None:
    """There is nowhere to look, which is an unknown rather than a clean install."""
    install = _install(tmp_path)
    del install.config.vault_root

    assert _detector(install).status == UNKNOWN


def test_no_runtime_root_cannot_answer_the_question(tmp_path: Path) -> None:
    """Nowhere to read the receipt from is not a clean answer either."""
    install = _install(tmp_path)

    with pytest.raises(ValueError, match="runtime root"):
        rehome_legacy_candidates(install.config, None)


def test_the_finding_carries_the_workspace_count_it_was_decided_on(tmp_path: Path) -> None:
    """The notice's own evidence is the count, and the notice still reads it."""
    install = _install(tmp_path)

    finding = rehomed_people_finding(install.config, install.runtime)
    assert finding is not None
    assert finding.workspaces == ("personal", "work")


# ── 3. completion is the completed receipt, and nothing else ─────────────────


#: Every receipt shape, and what completion has to say about it. `None` is no file
#: at all; a string is written verbatim, so the unparseable and non-object cases
#: are as reachable as the well-formed ones.
RECEIPTS: tuple[tuple[str, Any, bool], ...] = (
    ("missing", None, False),
    ("migrated", _receipt("migrated"), True),
    ("legacy without a status field", _receipt(None), True),
    ("partial", _receipt("partial"), False),
    ("failed", _receipt("failed"), False),
    ("unreadable", "{ not json at all", False),
    ("not an object", "[1, 2, 3]", False),
)


def _seed_receipt(runtime: Path, payload: Any) -> None:
    if isinstance(payload, str):
        path = vault_rehome.receipt_path(runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    elif payload is not None:
        _write_receipt(runtime, payload)


@pytest.mark.parametrize(("case", "payload", "completed"), RECEIPTS)
def test_completion_is_the_completed_receipt_and_nothing_else(
    tmp_path: Path, case: str, payload: Any, completed: bool
) -> None:
    """The check reads the receipt, and only the canonical reader's answer counts.

    The cases that matter to the operator are here rather than in prose: a
    ``partial`` run left the vault half re-homed, a receipt with no ``status``
    field records an install that had already done the work, and a file this
    install cannot parse is not proof of anything.
    """
    install = _install(tmp_path)
    _seed_receipt(install.runtime, payload)

    assert _check(install).applicable is completed, case
    assert (completed_rehome(install.runtime) is not None) is completed, case


@pytest.mark.parametrize(("case", "payload", "completed"), RECEIPTS)
def test_an_unfinished_receipt_never_hides_a_real_candidate(
    tmp_path: Path, case: str, payload: Any, completed: bool
) -> None:
    """Only a completed receipt removes the work; every other shape leaves it offered.

    A refused, partial, unreadable or absent receipt means the re-homing has not
    finished, so an install that still has a candidate must still be offered it —
    the inverse of the previous test, and the direction a "silence everything on
    any receipt" check would get wrong.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    _seed_receipt(install.runtime, payload)

    assert _check(install).applicable is completed, case
    if not completed:
        assert _detector(install).status == APPLICABLE, case


def test_the_receipt_evidence_holds_no_path(tmp_path: Path) -> None:
    """The receipt names its own run; its ``vault_root`` is an absolute path.

    State files travel between machines, so the completion evidence is the
    receipt's timestamp and two counts. A ``vault_root`` smuggled in here would be
    dropped by `_portable_evidence` and better never offered.
    """
    install = _install(tmp_path)
    _write_receipt(install.runtime, _receipt("migrated"))

    evidence = _check(install).evidence

    assert evidence["moved"] == 1
    assert evidence["receipt"] == "2026-08-19T00:00:00Z"
    assert not any(str(value).startswith("/") for value in evidence.values())


# ── 4. the run that satisfies the check is the run that writes the receipt ────


def test_a_real_rehome_run_settles_the_task_and_repoints_the_references(
    tmp_path: Path,
) -> None:
    """The managed command, run for real over a temp vault, is what completes it.

    Not a hand-written receipt: the point of the check is that it reads the
    evidence the remedy writes, so the remedy has to be the thing that writes it.
    The judgement case stays put and is queued, which is the restraint that makes
    the mechanical half trustworthy.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    _person(install.vault, "personal/People/Ida.md")

    summary = _rehome(install)

    assert summary["moves"], summary
    assert not summary["failed"], summary
    assert (install.vault / "work/People/Mo.md").is_file()
    assert not (install.vault / "personal/People/Mo.md").exists()
    # The judgement case is queued, never moved.
    assert (install.vault / "personal/People/Ida.md").is_file()
    # And the link that pointed at the old path followed the note.
    foo = (install.vault / "personal/Projects/Foo.md").read_text(encoding="utf-8")
    assert "work/People/Mo" in foo and "personal/People/Mo" not in foo

    receipt = _check(install)
    assert receipt.applicable is True
    assert receipt.evidence["moved"] == 1
    assert _detector(install).status == NOT_APPLICABLE
    assert _rehome_notices(install) == []


def test_an_inspection_that_moves_nothing_still_settles_an_applicable_task(
    tmp_path: Path,
) -> None:
    """The no-op receipt, in its only honest role: a record of a review.

    The task applied, the operator previewed, and by the time the apply ran the
    candidates were gone — so the command writes ``status: migrated`` with an
    empty move list, which is the truth about the vault and settles both surfaces.
    The order matters and is the point: this only settles a task that was
    *already* applicable. It is not how one becomes applicable, and the fresh
    install above is the proof — nothing moved there and nothing was recorded.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    assert _detector(install).status == APPLICABLE

    for path in (
        install.vault / "personal" / "People" / "Mo.md",
        install.vault / "personal" / "Projects" / "Foo.md",
    ):
        path.unlink()

    summary = _rehome(install)

    assert summary["moves"] == [] and summary["rewrites"] == []
    assert summary["applied"] is True
    assert json.loads(
        vault_rehome.receipt_path(install.runtime).read_text(encoding="utf-8")
    )["status"] == "migrated"
    assert _check(install).applicable is True
    assert _detector(install).status == NOT_APPLICABLE


def test_a_dirty_vault_refuses_the_apply_and_records_nothing(tmp_path: Path) -> None:
    """The refusal is the guarantee `git checkout` stays an undo, and no receipt follows.

    A run that persisted a receipt for a refused apply would settle this task on
    the strength of work that never happened, which is the failure the dirty-tree
    rail exists to prevent — and the card would then stay hidden while the notes
    were still misfiled.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    vault_rehome.run_git(install.vault, "init", "-q")
    vault_rehome.run_git(install.vault, "add", "-A")
    vault_rehome.run_git(install.vault, "commit", "-qm", "seed")
    # A tracked note this run would move, edited and not committed. The rail is
    # scoped to the plan's own paths, so this is exactly what it must catch.
    _note(
        install.vault,
        "personal/People/Mo.md",
        "---\ntype: person\ntags: [person, colleague]\n---\n# Mo\n\nEdited.\n",
    )

    summary = _rehome(install)

    assert summary.get("skipped") == "vault has uncommitted changes", summary
    assert not vault_rehome.receipt_path(install.runtime).exists()
    assert _check(install).applicable is False
    assert _detector(install).status == APPLICABLE


def test_a_half_finished_run_keeps_the_task_open(tmp_path: Path) -> None:
    """`status: partial` is what a run that could not move a note records.

    Every reference to a note the run never moved already points at a path it is
    not at, so neither the notice nor the card may call that done.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    _write_receipt(
        install.runtime,
        {**_receipt("partial"), "failed": [{"path": "personal/People/Mo.md"}]},
    )

    assert _check(install).applicable is False
    assert _detector(install).status == APPLICABLE
    assert _rehome_notices(install)


def test_a_started_task_settles_from_the_receipt_it_produces(tmp_path: Path) -> None:
    """The record completes when the evidence lands, without anything asking twice.

    #788's settlement, over the real remedy: a started task whose registered
    check now says the postcondition holds becomes `completed` on the next
    listing, and the card goes with it.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    # The attempt, and a listing while nothing has been recorded yet.
    assert (
        update_tasks.record_completion(
            _task(), config=install.config, workspace="personal"
        )
        is None
    )
    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=TASK_ID,
            revision=1,
            scope="install",
            lifecycle="in_progress",
            updated_at="2026-08-19T00:00:00+00:00",
        ),
        config=install.config,
    )

    row = next(s for s in _evaluate(install) if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "in_progress"

    _rehome(install)

    row = next(s for s in _evaluate(install) if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "completed"
    assert row.offered is False
    assert _rehome_notices(install) == []


# ── 5. the record is the operator's, and never the audit's ───────────────────


def test_the_record_lives_in_the_runtime_directory(tmp_path: Path) -> None:
    """Install-scoped means the runtime record, beside the other install records."""
    install = _install(tmp_path)

    update_tasks.record_dismissal(_task(), config=install.config, workspace="personal")

    path = install.runtime / "update-tasks.json"
    assert path.is_file()
    assert not list(install.vault.rglob("Update-Tasks.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == update_tasks.STATE_SCHEMA
    assert payload["tasks"][f"{TASK_ID}@1"]["scope"] == "install"


def test_a_dismissed_task_never_silences_the_audit(tmp_path: Path) -> None:
    """The finding survives the operator hiding the card.

    This is the asymmetry #833 introduces, and the reason the notice reads a
    receipt and never a lifecycle: the card is an offer with a decision attached
    to it, the audit's report is not, and one surface's state must never become
    the other surface's silence.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    update_tasks.record_dismissal(
        _task(), config=install.config, workspace="personal", reason="not now"
    )

    assert _rehome_notices(install), "a dismissal silenced the diagnostic"
    # The card itself is suppressed, which is the half that is the operator's.
    state = update_tasks.read_task_state(
        _task(), config=install.config, workspace="personal"
    )
    assert state is not None and state.lifecycle == "dismissed"


def test_a_completed_record_with_no_receipt_still_leaves_the_audit_reporting(
    tmp_path: Path,
) -> None:
    """Even a record claiming the work is finished is not evidence of it.

    `record_completion` could not have written this record — it asks the check,
    and the check reads the receipt — so the fixture is a hand-written
    `<runtime>/update-tasks.json` of the kind a restore or a hand edit leaves
    behind. The notice must still fire, because it never read the file.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    (install.runtime / "update-tasks.json").write_text(
        json.dumps(
            {
                "schema": update_tasks.STATE_SCHEMA,
                "tasks": {
                    f"{TASK_ID}@1": {
                        "task_id": TASK_ID,
                        "revision": 1,
                        "scope": "install",
                        "lifecycle": "completed",
                        "updated_at": "2026-08-19T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert _rehome_notices(install)


# ── 6. the walk this probe does, and the layer that pays for it ──────────────


def _accesses_under(root: Path, call: Any) -> list[str]:
    """Every filesystem access `call()` makes whose path is inside `root`.

    Counted rather than timed: the claim under test is which files a pass reads,
    not how long it takes, and a timing assertion passes on a fast tmpfs and fails
    in CI. A walk is exactly the access this records.
    """
    watched = ("open", "read_text", "read_bytes", "rglob", "glob", "iterdir", "walk")
    original = {name: getattr(Path, name) for name in watched}
    accesses: list[str] = []

    def _record(name: str, wrapped: Any) -> Any:
        def _watched(self: Path, *args: Any, **kwargs: Any) -> Any:
            try:
                inside = Path(self).resolve().is_relative_to(root.resolve())
            except (OSError, ValueError):
                inside = False
            if inside:
                accesses.append(f"{name}:{Path(self)}")
            return wrapped(self, *args, **kwargs)

        return _watched

    with patch.multiple(Path, **{name: _record(name, fn) for name, fn in original.items()}):
        call()
    return accesses


def test_the_notice_opens_no_file_under_the_vault_where_the_task_does(tmp_path: Path) -> None:
    """The two halves have different cost classes, and the cheap one is the polled one.

    The audit and the Home strip run on every app open and every 60s poll, so
    ``rehomed_people_finding`` must stay a receipt read — this fails if a vault
    access creeps into it. The task's detector does read the vault, which is the
    cost this catalog now admits to and the next two tests pin. A fixture claim is
    only checkable by running the detector over it, so both halves run over the
    same nine re-homable notes.

    The whole ``upgrade_notices`` section is deliberately *not* measured here: its
    wikilink notice legitimately walks the vault (`resolve_links`), so counting
    that would fail the wrong thing. What the audit still must produce is asserted
    directly instead.
    """
    install = _install(tmp_path)
    for index in range(9):
        _note(
            install.vault,
            f"personal/People/Note{index}.md",
            "---\ntype: person\ntags: [person, colleague]\n---\n# X\n",
        )

    assert _accesses_under(
        install.vault,
        lambda: rehomed_people_finding(install.config, install.runtime),
    ) == []
    assert _rehome_notices(install), "the notice half must still report"

    assert _detector(install).status == APPLICABLE
    assert _accesses_under(install.vault, lambda: _detector(install))


async def test_the_walk_runs_off_the_event_loop_through_the_read_executor(
    tmp_path: Path,
) -> None:
    """The window and the executor are the only reason this probe is affordable.

    One plan walk over the whole vault per freshness window, on the bounded
    executor, coalesced per install — and never on the loop a Home poll is
    answering. Recorded rather than timed, like the access count above: the claim
    is *which thread*, and it holds however fast the vault is.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    seen: list[str] = []
    original = vault_rehome.plan_rehome

    def _recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.current_thread().name)
        return original(*args, **kwargs)

    # Patched on the module the probe imports from, not on the caller: the import
    # is local to the function precisely so this module pays nothing for it.
    with patch.object(vault_rehome, "plan_rehome", _recording):
        await update_tasks.evaluate(
            install.config,
            workspace="personal",
            installed_version=__version__,
        )

    assert seen, "the probe never reached the plan, so nothing was proved"
    assert threading.current_thread().name not in seen


def test_one_listing_runs_the_plan_once_and_the_window_reuses_it(tmp_path: Path) -> None:
    """The bound is one plan per window per task, not one per listing.

    Two evaluations inside one window, both through the public route with no cache
    clearing between them, and the count of plan calls is the assertion. The
    window is what makes the walk affordable; a layer that re-planned on every
    poll would be a false economy — the walk would happen and the claim would
    not.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    calls: list[int] = []
    original = vault_rehome.plan_rehome

    def _counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return original(*args, **kwargs)

    async def _twice() -> tuple[Any, Any]:
        # The same injected clock twice: an age of zero is inside the window, so
        # the second call must be served from it rather than re-derived.
        first = await update_tasks.evaluate(
            install.config,
            workspace="personal",
            installed_version=__version__,
            now=1000.0,
        )
        second = await update_tasks.evaluate(
            install.config,
            workspace="personal",
            installed_version=__version__,
            now=1000.0,
        )
        return (
            next(s.applicability for s in first if s.task.id == TASK_ID),
            next(s.applicability for s in second if s.task.id == TASK_ID),
        )

    with patch.object(vault_rehome, "plan_rehome", _counting):
        first, second = asyncio.run(_twice())

    assert len(calls) == 1, "the plan ran more than once inside one window"
    assert first.status == APPLICABLE and second.status == APPLICABLE
    assert second.fingerprint == first.fingerprint


async def test_the_plan_is_not_reachable_from_a_polled_surface(tmp_path: Path) -> None:
    """Nothing on the Home strip or in the audit walks the vault for this task.

    The structural guarantee behind the cost class: ``operator_actions`` and
    ``os_audit`` call the *notice*, and only ``update_tasks`` calls the probe that
    plans. Asserted through the modules rather than by reading them, so a
    detector added to either one fails here.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    called: list[str] = []
    original = vault_rehome.plan_rehome

    with patch.object(
        vault_rehome,
        "plan_rehome",
        lambda *a, **k: (called.append("plan"), original(*a, **k))[1],
    ):
        detect_actions(
            DetectionContext(config=install.config, runtime_dir=install.runtime)
        )
        _audit_notices(install)

    assert called == [], "a polled surface planned the re-home"


def test_home_grows_one_card_for_this_notice_and_no_second_tile(tmp_path: Path) -> None:
    """The notice had no Home surface before #833, so the task is the only one.

    Duplicated *logic* is what `migration_notices` removed; duplicated *cards*
    are what would make an operator dismiss the same finding twice. This fails if
    a re-home tile is ever added beside the task.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    actions = detect_actions(
        DetectionContext(config=install.config, runtime_dir=install.runtime)
    )

    assert [a for a in actions if "rehome" in a.id or "rehomed" in a.id] == []


# ── 7. the packaged row, its prompt, and the registries ──────────────────────


def test_the_row_is_registered_on_both_sides_and_names_a_real_receipt() -> None:
    """A row may only name probes this engine implements, on both registries."""
    catalog = load_catalog()

    assert catalog.diagnostics == (), [
        (d.code, d.message) for d in catalog.diagnostics
    ]
    task = catalog.by_id[TASK_ID]
    assert task.scope == "install"
    assert task.detector in DETECTORS
    assert task.completion_check in COMPLETION_CHECKS
    assert DETECTORS == set(update_tasks.DETECTOR_FUNCTIONS)
    assert COMPLETION_CHECKS == set(update_tasks.COMPLETION_FUNCTIONS)


def test_the_prompt_teaches_the_managed_commands_and_nothing_else() -> None:
    """Managed commands, preview first, approval before `--apply`, an exact undo.

    The failure modes a prompt for this task can have, each asserted here: teaching
    a manual move (which the engine refuses and no receipt can undo), a chat that
    applies without asking (this moves the user's own notes between their own
    workspaces), an undo that is not the receipt's inverse, and — the one this
    prompt has to be most careful about — teaching a way to clear the card that is
    not a record of an inspection.
    """
    prompt = read_prompt(_task())

    assert "ciao vault-rehome" in prompt
    assert "ciao vault-rehome --apply" in prompt
    assert "ciao vault-unrehome" in prompt
    assert "--vault-root" in prompt, "the default root is gone on a re-rooted install"
    # Preview first, and the operator's approval before the write.
    assert "dry-run by default" in prompt or "writes nothing" in prompt
    assert "until you have shown the operator the preview" in prompt
    # Both rails, because both protect the operator.
    assert "uncommitted changes" in prompt
    assert "--force" in prompt
    # The shared-layout limit, stated rather than discovered.
    assert "per-workspace roots" in prompt
    # The asymmetry, stated: the card needs a candidate, the notice does not.
    assert "broader" in prompt
    assert "would move something" in prompt
    # No manual route.
    for forbidden in ("mv ", "git mv", "move the file by hand", "edit the registry"):
        assert forbidden not in prompt, forbidden


def test_the_packaged_row_launches_as_an_install_scoped_task(tmp_path: Path) -> None:
    """The whole path a Home Start button takes, over the real packaged row.

    An install-scoped task is the first one this catalog ships, so the two things
    that make it work are exercised here rather than assumed: the chat is hosted
    in a named workspace even though the *record* needs none, and the prompt
    dispatched is the packaged file's bytes, because the record says the task ran
    and a chat told to do something else would be worse than no chat.
    """
    from ciao.web import update_task_launch

    install = _install(tmp_path)
    _misfiled_person(install.vault)
    manager = _FakeChatManager()

    outcome = update_task_launch.launch_task(
        TASK_ID,
        config=install.config,
        pcm=manager,
        workspace="personal",
        installed_version=__version__,
    )

    assert outcome["created"] is True
    assert outcome["scope"] == "install"
    assert outcome["lifecycle"] == "in_progress"
    # Hosted in the named workspace, remembered in the install's own record.
    assert outcome["project_id"] == "proj-general"
    assert [p.workspace for p in manager.projects] == ["personal"]
    record = install.runtime / "update-tasks.json"
    assert record.is_file()
    stored = json.loads(record.read_text(encoding="utf-8"))["tasks"][f"{TASK_ID}@1"]
    assert stored["lifecycle"] == "in_progress"
    assert stored["chat_id"] == outcome["chat_id"]
    # And the prompt that went out is the packaged prompt, not a caller's text.
    assert manager.dispatched == [(outcome["chat_id"], read_prompt(_task()))]


class _Chat:
    def __init__(self, chat_id: str, project_id: str, title: str, helper: dict) -> None:
        self.chat_id = chat_id
        self.project_id = project_id
        self.title = title
        self.helper = helper
        self.archived = False


class _Project:
    def __init__(self, project_id: str, name: str, workspace: str) -> None:
        self.project_id = project_id
        self.name = name
        self.workspace = workspace


class _FakeChatManager:
    """The chat manager as far as a launch touches it: one chat, one dispatch.

    The helper is normalised on the way in through the real store's own function,
    so a helper the chat store would reject cannot pass this test.
    """

    def __init__(self) -> None:
        self.chats: dict[str, _Chat] = {}
        self.created: list[_Chat] = []
        self.projects: list[_Project] = []
        self.dispatched: list[tuple[str, str]] = []

    def list_projects(self, workspace: str | None = None) -> list[_Project]:
        return [
            project
            for project in self.projects
            if not workspace or project.workspace == workspace
        ]

    def create_project(self, name: str, workspace: str) -> _Project:
        project = _Project(f"proj-{name.lower()}", name, workspace)
        self.projects.append(project)
        return project

    def create_chat(
        self, project_id: str, title: str = "New Chat", helper: dict | None = None
    ) -> _Chat:
        from ciao.web import chat_service

        chat = _Chat(
            f"chat-{len(self.created) + 1}",
            project_id,
            title,
            chat_service._normalize_chat_helper(helper),
        )
        self.chats[chat.chat_id] = chat
        self.created.append(chat)
        return chat

    def get_chat(self, chat_id: str) -> _Chat | None:
        return self.chats.get(chat_id)

    def start_stream(self, chat_id: str, prompt: str) -> None:
        self.dispatched.append((chat_id, prompt))