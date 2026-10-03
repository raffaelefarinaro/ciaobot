"""The workspace-scoped ``vault-relocate`` update task (#800, last slice).

``vault-location:{workspace}`` was the last Home tile in #800's evidence table
whose premise turned out to be wrong. The table said its remedy wrote no receipt,
which would have made it shape 3 — "completion is the condition's absence
recomputed each render", a tautological check — and kept it a tile. Source
inspection against merged develop says otherwise: ``vault_relocate`` has written a
per-workspace ``vault-relocate-<workspace>.json`` with ``status: relocated`` on
every successful run for some time, and ``--undo`` **removes** it.

That makes this the notice in the table that is promotable, and it makes it a
shape of its own: a real receipt on a **reversible** remedy. The reversibility is
the whole design constraint, because the task lifecycle suppresses a completed
record for the rest of its revision — so a check that saw only "a receipt once
existed" would leave an undone relocation permanently finished, with nothing on
Home saying the vault is outside its folder again. Reading the receipt *and* the
current layout is what makes re-offer behaviour possible without changing
generic task semantics.

The properties pinned here, each of which a plausible implementation gets wrong:

1. **Applicability is the current, existing, misplaced vault** — the same
   predicate the audit reads, so the card and the report cannot name different
   workspaces. A missing vault is a different notice and gets no card.
2. **The Home tile is gone.** One condition, one card. Re-adding the tile beside
   the task is the duplicate this test exists to prevent.
3. **Completion is the receipt *and* the current layout.** A missing, `refused`,
   unreadable or foreign receipt is unfinished work; a `relocated` receipt with a
   live mismatch is unfinished work too, and that state is reachable.
4. **Evidence is portable.** No absolute path reaches a state file, on either
   side.
5. **A dismissed or completed task does not silence the audit.** The audit reads
   no update-task record.
6. **The run that writes the receipt is the real remedy**, and `--undo` deletes
   it, so undoing the move brings the work back.
7. **The prompt teaches the managed command, a preview, an approval gate, the
   restart and the undo** — and no manual route.

Every fixture is a temp directory with a synthetic git install. No real vault,
no runtime, no service, no installer, and no live relocation outside the temp
tree.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ciao import __version__, update_tasks, vault_relocate
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.migration_notices import (
    RELOCATION_DONE,
    RELOCATION_MISMATCHED,
    RELOCATION_MISPLACED,
    RELOCATION_NOT_DONE,
    RELOCATION_STANDARD,
    RELOCATION_VAULT_MISSING,
    VAULT_LOCATION_NOTICE,
    completed_relocation,
    relocation_mismatch,
    vault_location_findings,
)
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.os_audit import audit_upgrade_notices
from ciao.update_task_catalog import (
    COMPLETION_CHECKS,
    DETECTORS,
    load_catalog,
    read_prompt,
)
from ciao.update_tasks import APPLICABLE, NOT_APPLICABLE, UNKNOWN

TASK_ID = "vault-relocate"


# ── the synthetic install ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Install:
    """A one-workspace git install whose vault sits outside its standard folder.

    The shape #800's table is about, and the one the audit's notice has always
    reported: the registry pins an absolute path rather than the standard
    ``memory-vault/<name>``, the directory is really there, and
    ``ciao vault-relocate`` has a managed move for it.
    """

    root: Path
    config: CiaoConfig

    @property
    def runtime(self) -> Path:
        return self.root / ".runtime"

    @property
    def vault(self) -> Path:
        return self.root / "elsewhere" / "personal"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return (proc.stdout + proc.stderr).strip()


def _install(tmp_path: Path, *, misplaced: bool = True) -> Install:
    """A git install with one registered workspace, pinned or standard.

    ``misplaced=False`` gives the same install with its vault already where the
    layout wants it, which is the fixture every "nothing to do" case is asserted
    against — a detector that cannot answer there is a detector that cannot say
    no.
    """
    root = tmp_path / "install"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    runtime = root / ".runtime"
    runtime.mkdir()
    if misplaced:
        vault = root / "elsewhere" / "personal"
        stored = str(vault)
    else:
        vault = root / "memory-vault" / "personal"
        stored = "memory-vault/personal"
    (vault / "People").mkdir(parents=True)
    (vault / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (vault / "People" / "Ida.md").write_text("# Ida\n", encoding="utf-8")
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": stored}], indent=2) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root=stored)},
    )
    return Install(root=root, config=config)


def _delete_vault(install: Install) -> None:
    """The registry still points here, but the directory is gone."""
    import shutil

    shutil.rmtree(install.vault)


def _undo(install: Install) -> dict[str, Any]:
    """The reverse, as the CLI calls it."""
    return vault_relocate.undo(install.config, "personal", install.runtime)


def _receipt(status: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workspace": "personal",
        "source": "elsewhere/personal",
        "destination": "memory-vault/personal",
        "recorded_at": "2026-10-01T12:00:00Z",
        "applied": [
            {
                "source": "elsewhere/personal/People",
                "destination": "memory-vault/personal/People",
            }
        ],
        "whole_directory": True,
    }
    if status is not None:
        payload["status"] = status
    return payload


def _seed_receipt(install: Install, payload: Any) -> None:
    path = vault_relocate.receipt_path(install.runtime, "personal")
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _task() -> Any:
    task = load_catalog().by_id.get(TASK_ID)
    assert task is not None, f"{TASK_ID} is not in the packaged catalog"
    return task


def _detector(install: Install, workspace: str = "personal") -> Any:
    return update_tasks.apply_detector(
        _task(), config=install.config, workspace=workspace
    )


def _check(install: Install, workspace: str = "personal") -> Any:
    check = update_tasks.COMPLETION_FUNCTIONS[_task().completion_check]
    return check(config=install.config, workspace=workspace, today=None)


def _notices(install: Install) -> list[dict[str, Any]]:
    return audit_upgrade_notices(install.config, runtime_dir=install.runtime)["notices"]


def _location_notices(install: Install) -> list[dict[str, Any]]:
    return [n for n in _notices(install) if n["type"] == VAULT_LOCATION_NOTICE]


def _evaluate(install: Install, workspace: str = "personal") -> list[Any]:
    update_tasks.clear_applicability_cache()
    return asyncio.run(
        update_tasks.evaluate(
            install.config, workspace=workspace, installed_version=__version__
        )
    )


# ── 1. applicability is the current, existing, misplaced vault ──────────────


def test_a_misplaced_vault_offers_the_card(tmp_path: Path) -> None:
    """The positive: there is a vault, it is there, and it is not where it belongs.

    The condition is the audit's own predicate applied to one workspace, so the
    card and the report cannot disagree about *which* workspaces are misplaced —
    only about what the operator does about it.
    """
    install = _install(tmp_path)

    result = _detector(install)

    assert result.status == APPLICABLE
    assert result.evidence["reason"] == RELOCATION_MISPLACED
    assert result.evidence["workspace"] == "personal"
    assert _location_notices(install), "the audit must report the same condition"


def test_a_standard_vault_is_not_offered(tmp_path: Path) -> None:
    """The registry and the layout agree, so there is nothing to offer.

    Asserted against a *real* standard install rather than a hand-built
    `Detection(False, ...)`: a detector that cannot reach zero on the ordinary
    case is the "offered forever" shape #788's upkeep row is about.
    """
    install = _install(tmp_path, misplaced=False)

    result = _detector(install)

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == RELOCATION_STANDARD
    assert _location_notices(install) == []
    assert vault_location_findings(install.config) == []


def test_a_missing_vault_is_a_different_notice_and_gets_no_card(tmp_path: Path) -> None:
    """There is no misplaced vault to relocate, so nothing is offered.

    A root that does not exist is `workspace-root-missing`'s notice on Home and a
    setup finding in the audit. Offering a move for a path that is not there is
    the unactionable tile operators learn to ignore, and `vault_location_findings`
    has always skipped it — so the task inherits the skip rather than
    re-deciding it, and says so in its evidence.
    """
    install = _install(tmp_path)
    _delete_vault(install)

    result = _detector(install)

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == RELOCATION_VAULT_MISSING
    assert _location_notices(install) == []


def test_an_unregistered_workspace_gets_no_card(tmp_path: Path) -> None:
    """A name the registry does not name is not this install's vault to move.

    `vault_location_findings` iterates the registry, so an unregistered name has no
    finding at all, and `relocation_state` agrees: `workspace_vault_root` resolves
    an unknown name into the same folder the layout puts a workspace of that name
    in, so the two paths match and nothing is offered. On a re-rooted install the
    same fallback lands elsewhere and reads as `vault_missing` — a different
    reason for the same answer, which is why this pins the *status* rather than
    the label.

    What matters is that the stranger's non-existence cannot invent or silence work
    on a workspace that really is misplaced: that one's own answer is untouched.

    An empty name is a different answer and a raise: it names nothing at all, so
    answering "nothing to do" would be a claim about some workspace.
    """
    install = _install(tmp_path)

    unknown = _detector(install, workspace="clientA")
    assert unknown.status == NOT_APPLICABLE
    assert unknown.evidence["reason"] in (RELOCATION_STANDARD, RELOCATION_VAULT_MISSING)
    assert _detector(install, workspace="personal").status == APPLICABLE

    # An empty name names nothing at all, so the probe raises and the layer turns
    # that into `unknown` rather than letting it read as a clean workspace.
    probe = update_tasks.DETECTOR_FUNCTIONS[_task().detector]
    with pytest.raises(ValueError, match="workspace-scoped"):
        probe(config=install.config, workspace="", today=None)
    assert _detector(install, workspace="").status == UNKNOWN


def test_evidence_the_install_cannot_read_is_unknown(tmp_path: Path) -> None:
    """A registry that will not resolve is an unknown, never "nothing to do".

    ``not_applicable`` here would be a positive claim that the vault is where it
    belongs, made by a probe that never managed to look — the direction where an
    operator would act on it. The layer is what turns the raise into ``unknown``;
    ``apply_detector`` is the layer, so this is asserted through it rather than by
    catching the raise.
    """
    from unittest.mock import patch

    install = _install(tmp_path)

    with patch.object(
        CiaoConfig, "workspace_vault_root", side_effect=OSError("registry unreadable")
    ):
        assert _detector(install).status == UNKNOWN
    with patch.object(
        CiaoConfig, "canonical_workspace_vault_root", side_effect=OSError("no layout")
    ):
        assert _detector(install).status == UNKNOWN
    # The audit stays a report on the same fault: it drops the notice rather than
    # failing a whole report.
    with patch.object(
        CiaoConfig, "workspace_vault_root", side_effect=OSError("registry unreadable")
    ):
        assert _location_notices(install) == []


# ── 2. the Home tile is gone, and only one card exists ──────────────────────


def test_home_grows_no_tile_for_a_misplaced_vault(tmp_path: Path) -> None:
    """The card replaces the tile rather than joining it.

    Duplicated *logic* is what `migration_notices` removed when the predicate was
    deduped; duplicated *cards* are what would make an operator be asked about one
    condition twice — and the tile could not be dismissed or completed, so it
    would reappear on every 60s poll for ever while the card sat beside it
    offering to be dismissed. This fails if `_detect_vault_location` ever comes
    back.
    """
    install = _install(tmp_path)

    actions = detect_actions(
        DetectionContext(config=install.config, runtime_dir=install.runtime)
    )

    assert [a for a in actions if "vault-location" in a.id] == []
    assert [a for a in actions if a.kind == "vault-location"] == []


def test_the_card_is_offered_where_the_tile_used_to_be(tmp_path: Path) -> None:
    """The offer exists at all — a missing tile is only half the change.

    Asserted through `evaluate`, the public listing path, so it is the same
    answer Home renders rather than a direct probe call.
    """
    install = _install(tmp_path)

    statuses = _evaluate(install)

    row = next(s for s in statuses if s.task.id == TASK_ID)
    assert row.applicability.status == APPLICABLE
    assert row.offered is True


def test_the_audit_notice_survives_the_tile_being_removed(tmp_path: Path) -> None:
    """Removing a Home surface must not remove the diagnostic with it.

    The notice is the one surface an operator can still reach after dismissing
    the card, and `audit_upgrade_notices` reads `vault_location_findings` itself.
    """
    install = _install(tmp_path)

    notices = _location_notices(install)

    assert [n["workspace"] for n in notices] == ["personal"]
    assert "ciao vault-relocate personal" in notices[0]["remedy"]


# ── 3. completion is the receipt AND the current layout ─────────────────────


def test_a_fresh_vault_is_not_complete(tmp_path: Path) -> None:
    """No receipt at all is the ordinary answer, and it is not completion."""
    install = _install(tmp_path)

    result = _check(install)

    assert result.applicable is False
    assert result.evidence["reason"] == RELOCATION_NOT_DONE
    assert completed_relocation(install.runtime, "personal") is None


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("refused", _receipt("refused")),
        ("no status field", _receipt(None)),
        ("unreadable", "{ not json at all"),
        ("not an object", "[1, 2, 3]"),
    ],
)
def test_only_a_relocated_receipt_counts(
    tmp_path: Path, case: str, payload: Any
) -> None:
    """Four receipt shapes that are all the same answer: nothing was done.

    The `refused` case is the ordinary one, not an edge: most of the shapes
    `vault_relocate.plan` refuses are permanent properties of an install, so
    retrying `--apply` writes one every time and none of them moved a note. A
    receipt-shaped file is not proof of a move.

    The missing-`status` case is where this deliberately differs from
    `vault_rehome.read_receipt`, which counts a status-less receipt as a
    completed run because that remedy predates its own status field. Every
    `vault_relocate` run has named its status, so a file without one is not this
    engine's record of anything.
    """
    install = _install(tmp_path)
    _seed_receipt(install, payload)

    result = _check(install)

    assert result.applicable is False, case
    assert result.evidence["reason"] == RELOCATION_NOT_DONE, case
    # And the work is still outstanding, so the card is still offered.
    assert _detector(install).status == APPLICABLE, case


def test_a_relocated_receipt_with_a_live_mismatch_is_not_complete(
    tmp_path: Path,
) -> None:
    """The state a receipt-only check reports as finished, pinned as unfinished.

    A `relocated` receipt is a record of a **past** run. A registry edited by
    hand afterwards, a restore from an older copy, or any change that did not go
    through the receipt leaves a workspace that has a completed relocation
    recorded and is still outside its folder. This is the case that makes the
    check a conjunction rather than a receipt read, and it is reachable without
    any bug in the engine — a hand edit is a supported way to end up here.
    """
    install = _install(tmp_path)
    _seed_receipt(install, _receipt("relocated"))
    # The receipt says it moved; the layout says it did not. Believe the layout.
    assert completed_relocation(install.runtime, "personal") is not None

    result = _check(install)

    assert result.applicable is False
    assert result.evidence["reason"] == RELOCATION_MISMATCHED
    assert result.evidence["recorded_at"] == "2026-10-01T12:00:00Z"
    assert _detector(install).status == APPLICABLE


def test_the_check_also_refuses_to_answer_about_no_workspace(
    tmp_path: Path,
) -> None:
    """Both halves of the pair refuse an empty name, not just the detector.

    With one, the other would answer about *no* workspace: the check would look
    for `vault-relocate-.json`, find nothing, and report "nothing was done" —
    which `record_completion` would happily turn into `completed`. That is a
    positive claim about an unnamed workspace, and the launch route's refusal of
    a workspace-scoped task with no workspace is not enough to stop it.
    """
    install = _install(tmp_path)
    check = update_tasks.COMPLETION_FUNCTIONS[_task().completion_check]

    with pytest.raises(ValueError, match="workspace-scoped"):
        check(config=install.config, workspace="", today=None)


def test_a_receipt_for_another_workspace_is_not_this_workspace_evidence(
    tmp_path: Path,
) -> None:
    """The receipt is keyed by workspace, and this task is workspace-scoped.

    Not hypothetical: `receipt_path` interpolates the name into the filename, so
    a second registered workspace has its own file, and a fixture that wrote the
    wrong one would settle the wrong task. The check asks for *this* workspace's
    receipt by name and reads the current layout for *this* workspace, so neither
    half can be answered by the other.
    """
    install = _install(tmp_path)
    other = install.runtime / "migration" / "vault-relocate-work.json"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text(json.dumps(_receipt("relocated")), encoding="utf-8")

    result = _check(install)

    assert result.applicable is False
    assert result.evidence["reason"] == RELOCATION_NOT_DONE


def test_a_relocated_and_standard_workspace_is_complete(tmp_path: Path) -> None:
    """Both halves true: the run is recorded and the layout agrees.

    The positive asserted against a **real** relocation rather than a
    hand-written receipt, so the check is proven to read the evidence the remedy
    writes. `test_a_relocated_receipt_with_a_live_mismatch_is_not_complete`
    deliberately seeds the same receipt on a vault that was never moved, and this
    is the half that proves the two are not the same test.
    """
    install = _install(tmp_path)

    summary = vault_relocate.apply(install.config, "personal", install.runtime)

    assert summary["status"] == "relocated", summary.get("refusals")
    assert (install.root / "memory-vault" / "personal" / "MEMORY.md").is_file()
    assert not install.vault.exists()
    # The receipt exists and the layout agrees.
    assert completed_relocation(install.runtime, "personal") is not None
    assert relocation_mismatch(install.config, "personal") is False

    result = _check(install)

    assert result.applicable is True
    assert result.evidence["reason"] == RELOCATION_DONE
    assert result.evidence["moved"] == 1
    assert _detector(install).status == NOT_APPLICABLE


def test_a_started_task_settles_from_the_receipt_it_produces(
    tmp_path: Path,
) -> None:
    """#788's settlement, over the real remedy.

    A started task whose registered check now says the postcondition holds
    becomes `completed` on the next listing, and the card goes with it — with
    nothing asking twice.
    """
    install = _install(tmp_path)
    update_tasks.write_task_state(
        update_tasks.TaskState(
            task_id=TASK_ID,
            revision=1,
            scope="workspace",
            lifecycle="in_progress",
            updated_at="2026-10-01T12:00:00+00:00",
        ),
        config=install.config,
        workspace="personal",
    )

    row = next(s for s in _evaluate(install) if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "in_progress"

    vault_relocate.apply(install.config, "personal", install.runtime)

    row = next(s for s in _evaluate(install) if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "completed"
    assert row.offered is False


# ── 4. the evidence a state file may hold ───────────────────────────────────


def test_neither_side_puts_an_absolute_path_in_a_state_record(
    tmp_path: Path,
) -> None:
    """State files sync, back up and travel; the paths do not.

    Asserted as *both* directions because each has its own trap: the detector
    starts from two resolved paths and the check reads a receipt whose `source`
    and `destination` are absolute. `_portable_evidence` drops such an entry, so
    smuggling one in would lose evidence silently rather than fail.
    """
    install = _install(tmp_path)
    vault_relocate.apply(install.config, "personal", install.runtime)

    detected = _detector(install).evidence
    completed = _check(install).evidence

    assert not any(str(v).startswith("/") for v in detected.values()), detected
    assert not any(str(v).startswith("/") for v in completed.values()), completed
    # The workspace name is what identifies the situation, and it travels.
    assert detected["workspace"] == "personal"
    # A record written from either answer round-trips through the real writer.
    state = update_tasks.TaskState(
        task_id=TASK_ID,
        revision=1,
        scope="workspace",
        lifecycle="offered",
        updated_at="2026-10-01T12:00:00+00:00",
        evidence=completed,
    )
    path = update_tasks.write_task_state(
        state, config=install.config, workspace="personal"
    )
    stored = json.loads(path.read_text(encoding="utf-8"))["tasks"][f"{TASK_ID}@1"]
    assert stored["evidence"]["reason"] == RELOCATION_DONE


def test_the_record_lives_in_the_workspace_own_vault(tmp_path: Path) -> None:
    """Workspace-scoped means the vault's own file, so a sync carries the decision.

    The mirror of the install-scoped re-home task, and the reason this task's
    scope is `workspace` at all: the condition is one workspace's vault, so two
    workspaces that are each misplaced hold two independent records and neither
    suppresses the other.
    """
    install = _install(tmp_path)

    update_tasks.record_dismissal(
        _task(), config=install.config, workspace="personal", reason="not now"
    )

    path = (
        install.vault
        / update_tasks.WORKSPACE_STATE_DIR
        / update_tasks.WORKSPACE_STATE_FILENAME
    )
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == update_tasks.STATE_SCHEMA
    assert payload["tasks"][f"{TASK_ID}@1"]["scope"] == "workspace"
    # Not the install-wide record.
    assert not (install.runtime / update_tasks.INSTALL_STATE_FILENAME).exists()


# ── 5. the record is the operator's, and never the audit's ───────────────────


def test_a_dismissed_task_never_silences_the_audit(tmp_path: Path) -> None:
    """The finding survives the operator hiding the card.

    The asymmetry this catalog exists for: the card is an offer with a decision
    attached, the audit's report is not, and one surface's state must never become
    the other's silence. It holds because `migration_notices` reads no
    update-task record — and it is the property the removed tile did *not* have,
    which is half of why the tile went.
    """
    install = _install(tmp_path)

    update_tasks.record_dismissal(
        _task(), config=install.config, workspace="personal", reason="not now"
    )

    assert _location_notices(install), "a dismissal silenced the diagnostic"
    state = update_tasks.read_task_state(
        _task(), config=install.config, workspace="personal"
    )
    assert state is not None and state.lifecycle == "dismissed"
    # And the card is gone, which is the half that is the operator's.
    assert next(s for s in _evaluate(install) if s.task.id == TASK_ID).offered is False


def test_a_completed_record_with_no_receipt_still_leaves_the_audit_reporting(
    tmp_path: Path,
) -> None:
    """Even a record claiming the work is finished is not evidence of it.

    `record_completion` could not have written this — it asks the check, and the
    check reads the receipt and the layout — so the fixture is a hand-written
    `Workspace/Update-Tasks.json` of the kind a restore or a hand edit leaves
    behind. The notice must still fire, because it never read the file.
    """
    install = _install(tmp_path)
    state_file = (
        install.vault
        / update_tasks.WORKSPACE_STATE_DIR
        / update_tasks.WORKSPACE_STATE_FILENAME
    )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                "schema": update_tasks.STATE_SCHEMA,
                "tasks": {
                    f"{TASK_ID}@1": {
                        "task_id": TASK_ID,
                        "revision": 1,
                        "scope": "workspace",
                        "lifecycle": "completed",
                        "updated_at": "2026-10-01T12:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert _location_notices(install)


def test_no_notice_function_reads_an_update_task_record() -> None:
    """The structural half: that dependency does not exist in this direction.

    Asserted over the module's *imports and globals* rather than its source text,
    because a docstring that explains this very rule legitimately mentions the
    module by name — and a source scan would therefore forbid the sentence that
    documents the guarantee, which is how such a guard quietly stops being one.
    The re-home task's own parity test is the precedent; this notice needs it more,
    because the Home tile it replaced was a surface an operator had no way to
    silence at all.
    """
    import ciao.migration_notices as notices

    for name, value in vars(notices).items():
        if name.startswith("__"):
            continue
        module = getattr(value, "__module__", None)
        assert module != "ciao.update_tasks", name
        assert getattr(value, "__name__", "") != "ciao.update_tasks", name
    # And nothing in the module's own namespace *is* the task layer.
    assert not hasattr(notices, "update_tasks")


# ── 6. the remedy is reversible, and that is what re-offers the task ─────────


def test_undo_removes_the_receipt_and_the_work_comes_back(tmp_path: Path) -> None:
    """The property a reversible remedy has to have to carry a task at all.

    The lifecycle suppresses a `completed` record for the rest of its revision,
    so a check that could only see "a receipt once existed" would hide the card
    for ever after an undo. Here the undo deletes the receipt, the check stops
    claiming completion, and the detector — which never consulted the receipt for
    applicability — sees the mismatch the undo put back and offers the work
    again. The generic task semantics are untouched; the evidence moves instead.
    """
    install = _install(tmp_path)
    vault_relocate.apply(install.config, "personal", install.runtime)
    assert _check(install).applicable is True

    undone = _undo(install)

    assert undone["status"] == "undone", undone
    assert not vault_relocate.receipt_path(install.runtime, "personal").exists()
    assert _check(install).applicable is False
    assert _detector(install).status == APPLICABLE
    assert _location_notices(install), "the audit reports the restored mismatch too"


def test_a_reopen_puts_the_work_back_on_offer(tmp_path: Path) -> None:
    """The operator's own way back, and it needs no receipt to have been removed.

    `reopen_task` reverses a dismissal at the same revision. A completed record is
    deliberately *not* reopened by it — that is a check's verdict, not an
    operator's decision — which is why the undo path above is the one that has to
    work, and why it is asserted end to end.
    """
    install = _install(tmp_path)
    update_tasks.record_dismissal(
        _task(), config=install.config, workspace="personal", reason="later"
    )
    assert next(s for s in _evaluate(install) if s.task.id == TASK_ID).offered is False

    reopened = update_tasks.reopen_task(
        _task(), config=install.config, workspace="personal"
    )

    assert reopened.lifecycle == "offered"
    assert next(s for s in _evaluate(install) if s.task.id == TASK_ID).offered is True


def test_a_refused_apply_records_a_refusal_and_moves_nothing(tmp_path: Path) -> None:
    """The refusal rail is the guarantee `git checkout` stays an undo.

    A run that moved nothing and recorded `relocated` would settle this task over a
    vault that is still in the wrong place — which is the exact claim the
    conjunction in the check exists to prevent, so it is asserted against the real
    command rather than a seeded receipt.
    """
    install = _install(tmp_path)
    (install.vault / "MEMORY.md").write_text(
        "# Memory\n\nEdited, uncommitted.\n", encoding="utf-8"
    )

    summary = vault_relocate.apply(install.config, "personal", install.runtime)

    assert summary["status"] == "refused", summary.get("refusals")
    assert (install.vault / "MEMORY.md").is_file()
    assert _check(install).applicable is False
    assert _detector(install).status == APPLICABLE


# ── 7. the packaged row, its prompt and the registries ──────────────────────


def test_the_row_is_registered_on_both_sides(tmp_path: Path) -> None:
    """A row may only name probes this engine implements, on both registries.

    And the registries must be *exactly* the two implementations: a name in one
    and not the other is the shape where a row loads and then answers `unknown`
    forever.
    """
    catalog = load_catalog()

    assert catalog.diagnostics == (), [(d.code, d.message) for d in catalog.diagnostics]
    task = catalog.by_id[TASK_ID]
    assert task.scope == "workspace"
    assert task.detector in DETECTORS
    assert task.completion_check in COMPLETION_CHECKS
    assert DETECTORS == set(update_tasks.DETECTOR_FUNCTIONS)
    assert COMPLETION_CHECKS == set(update_tasks.COMPLETION_FUNCTIONS)


def test_the_prompt_teaches_the_managed_command_and_nothing_else() -> None:
    """Preview, approval, restart, undo — and no manual route.

    The failure modes a prompt for this task can have, each asserted here:
    applying without asking (this moves the operator's own notes out from under
    a running engine), omitting the restart (the running server keeps writing to
    the old path), presenting `--undo` as a re-derivation, and teaching the manual
    move the engine refuses.
    """
    prompt = read_prompt(_task())

    # The exact command, with the workspace named — the placeholder is the
    # instruction, since the prompt ships as one static file per task.
    assert "ciao vault-relocate {workspace}" in prompt
    assert "ciao vault-relocate {workspace} --apply" in prompt
    assert "ciao vault-relocate {workspace} --undo" in prompt
    # Preview first, and the operator's approval before the write.
    assert "writes nothing" in prompt
    assert (
        "until you have shown the operator this preview" in prompt
    ), "the approval gate is missing"
    # The restart is not optional on either direction.
    assert prompt.count("restart") >= 2
    assert "Settings -> Restart" in prompt
    # The undo removes the receipt, which is why the work comes back.
    assert "removes the receipt" in prompt
    # The rail, and the refusal being information rather than an obstacle.
    assert "uncommitted changes" in prompt
    assert "--force" in prompt
    # The audit's independence, stated rather than discovered.
    assert "os-audit" in prompt
    assert "does not silence it" in prompt
    # No manual route, and no hand-edited registry.
    for forbidden in (
        "mv ",
        "git mv",
        "move the folder by hand",
        "edit the registry",
        "mv <",
    ):
        assert forbidden not in prompt, forbidden
    # The restart is the operator's press, not ours. Matched on whitespace-folded
    # text: a prompt is prose, and a rewrap must not be able to fail this guard.
    flat = " ".join(prompt.split())
    assert "Do not restart it for them." in flat


def test_the_packaged_row_carries_no_absolute_path_or_workspace_literal() -> None:
    """The prompt ships as one file for every workspace, so it must name none.

    A workspace name or an install path written into the packaged prompt would be
    this developer's install, served to every other one. The `{workspace}`
    placeholder is the mechanism that keeps it out, and asserting the absence of
    the obvious spellings keeps a future edit from inlining a real one.
    """
    prompt = read_prompt(_task())

    assert "/Users/" not in prompt
    assert "/home/" not in prompt
    assert "CIAO_VAULT_ROOT" not in prompt
    # Nothing that looks like a concrete workspace name either.
    for name in ("personal", "clientA", "scandit", "work"):
        assert f"ciao vault-relocate {name}" not in prompt, name
