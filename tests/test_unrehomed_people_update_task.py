"""The install-scoped ``unrehomed-people`` update task (#833).

`unrehomed_people` was the one migration notice with no Home surface at all: the
OS audit reported it, nothing else did, and the catalog that #737 introduced had
no row for it. It is now the first *shape-2* notice in the catalog — one with a
real, completed-only receipt written by its own remedy — so this file pins the
five things that makes it honest:

1. the task's applicability is the audit's condition, not a stricter or looser
   copy of it, over every receipt shape;
2. completion is the **completed** receipt and nothing else: missing, partial,
   failed, unreadable and unparseable receipts are all unfinished work;
3. the truthful no-op receipt a first ``--apply`` over a root with nothing to do
   writes really does settle it, which is the only route that settles the notice
   on a re-rooted install;
4. the audit keeps reporting the finding whatever the task's record says, because
   the record is not an input to the notice;
5. nothing here walks the vault to decide anything, and Home grows exactly one
   card for the notice rather than a tile beside the task.

Every fixture is a temp directory. No real vault, no runtime, no service, no
installer, and nothing is migrated for real outside the temp tree.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ciao import __version__, async_reads, update_tasks, vault_rehome
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.migration_notices import (
    UNREHOMED_PEOPLE_NOTICE,
    completed_rehome,
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
    """One install with the given registered workspaces and an empty vault.

    Registered explicitly rather than derived from vault directories, because the
    condition under test turns on the *registry's* count: the re-home module is
    explicit that a caller with a config should pass `config.workspace_names()`
    and that a stray directory is not a workspace.
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


def _person(vault: Path, relative: str) -> Path:
    """A person note that is not a candidate: no tag names another workspace."""
    return _note(vault, relative, "---\ntype: person\ntags: [person]\n---\n# Someone\n")


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


# ── 1. one condition, two surfaces ──────────────────────────────────────────


def test_the_task_applies_when_the_notice_fires_and_stops_when_it_does_not(
    tmp_path: Path,
) -> None:
    """Two registered workspaces and no completed receipt, on both surfaces.

    The first pairing of this condition in the app: the audit's report and Home's
    offer, one of which the operator may dismiss and one of which they may not.
    """
    install = _install(tmp_path)

    assert _detector(install).status == APPLICABLE
    assert _rehome_notices(install), "the audit was quiet while the task offered the work"

    _write_receipt(install.runtime, _receipt("migrated"))

    assert _detector(install).status == NOT_APPLICABLE
    assert _rehome_notices(install) == []


#: Every receipt shape, and what both surfaces have to say about it. `None` is
#: no file at all; a string is written verbatim, so the unparseable and
#: non-object cases are as reachable as the well-formed ones.
RECEIPTS: tuple[tuple[str, Any, bool], ...] = (
    ("missing", None, False),
    ("migrated", _receipt("migrated"), True),
    ("legacy without a status field", _receipt(None), True),
    ("partial", _receipt("partial"), False),
    ("failed", _receipt("failed"), False),
    ("unreadable", "{ not json at all", False),
    ("not an object", "[1, 2, 3]", False),
)


@pytest.mark.parametrize(("case", "payload", "completed"), RECEIPTS)
def test_completion_is_the_completed_receipt_and_nothing_else(
    tmp_path: Path, case: str, payload: Any, completed: bool
) -> None:
    """The check reads the receipt, and only the canonical reader's answer counts.

    The four cases that matter to the operator are here rather than in prose: a
    ``partial`` run left the vault half re-homed, a receipt with no ``status``
    field records an install that had already done the work, and a file this
    install cannot parse is not proof of anything.
    """
    install = _install(tmp_path)
    if isinstance(payload, str):
        path = vault_rehome.receipt_path(install.runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    elif payload is not None:
        _write_receipt(install.runtime, payload)

    assert _check(install).applicable is completed, case
    assert (completed_rehome(install.runtime) is not None) is completed, case


@pytest.mark.parametrize(("case", "payload", "completed"), RECEIPTS)
def test_the_audit_and_the_task_never_disagree_about_a_receipt(
    tmp_path: Path, case: str, payload: Any, completed: bool
) -> None:
    """One rule, read through one accessor, on both surfaces.

    This is the whole point of moving the condition into `migration_notices`: a
    notice that goes quiet on a half-finished run while its card still offers the
    work would be two answers to one question, and the operator could not tell
    which one the engine meant.
    """
    install = _install(tmp_path)
    if isinstance(payload, str):
        path = vault_rehome.receipt_path(install.runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    elif payload is not None:
        _write_receipt(install.runtime, payload)

    offered = _detector(install).status == APPLICABLE
    assert offered is not completed, case
    assert bool(_rehome_notices(install)) is offered, case


def test_the_task_is_never_offered_on_a_single_workspace_install(tmp_path: Path) -> None:
    """One workspace has no counterpart for a note to be misfiled *from*.

    `detect_misfiled_people` still buckets an untagged note as needing judgement
    with one workspace, but its destination comes back empty: there is no move to
    offer, and offering one is how a fresh install learns the strip is noise.
    """
    install = _install(tmp_path, workspaces=("personal",))

    assert _detector(install).status == NOT_APPLICABLE
    assert _rehome_notices(install) == []


def test_a_config_without_a_registry_is_not_applicable_rather_than_an_offer() -> None:
    """A surface that cannot name the workspaces has nothing to say about them.

    The same rule `vault_location_findings` follows, and the same one
    `audit_upgrade_notices` follows in its own early return: a config with no
    workspace registry is not a fault on a notice path, it is a surface with no
    answer. The `reason` is one bucket for "nothing to offer here" — there is no
    receipt and no registry — which is why it is asserted as a status and not
    read as a statement about any particular install.
    """
    from types import SimpleNamespace

    result = update_tasks.apply_detector(
        _task(), config=SimpleNamespace(state_path="/nonexistent/.runtime/state.json")
    )

    assert result.status == NOT_APPLICABLE
    assert result.evidence["reason"] == "no_rehome_needed"


def test_a_config_that_cannot_name_its_workspaces_is_unknown(tmp_path: Path) -> None:
    """A raise is an unknown, and an unknown must never be reported as "nothing to do".

    ``not_applicable`` here would be a positive claim that this install has no
    re-homing to review, made by a probe that never learned how many workspaces
    it has.
    """
    install = _install(tmp_path)

    with patch.object(
        CiaoConfig, "workspace_names", side_effect=RuntimeError("registry unreadable")
    ):
        result = _detector(install)

    assert result.status == UNKNOWN
    assert result.evidence["reason"] == "detector_failed"


def test_an_unreadable_receipt_is_an_unknown_for_the_task_and_a_dropped_notice_for_the_audit(
    tmp_path: Path,
) -> None:
    """One fault, two answers, and neither of them a claim about the vault.

    ``os_audit`` drops the notice rather than failing a whole report — a
    diagnostic that cannot read one receipt is a report with one fewer notice.
    The task records ``unknown``, which is not offered and not suppressed, so the
    work stays on the table instead of being declared finished.
    """
    install = _install(tmp_path)

    with patch(
        "ciao.vault_rehome.read_receipt", side_effect=OSError("receipt unreadable")
    ):
        assert _detector(install).status == UNKNOWN
        assert _rehome_notices(install) == []


def test_the_finding_carries_the_workspace_count_it_was_decided_on(tmp_path: Path) -> None:
    """The evidence is the count, so a registry that grows brings the task back."""
    install = _install(tmp_path)
    assert _detector(install).evidence["workspaces"] == 2

    finding = rehomed_people_finding(install.config, install.runtime)
    assert finding is not None
    assert finding.workspaces == ("personal", "work")


def test_no_runtime_root_cannot_apply_the_notice(tmp_path: Path) -> None:
    """No runtime root means nowhere to read the evidence from, which is not a clean answer."""
    install = _install(tmp_path)

    assert rehomed_people_finding(install.config, None) is None


# ── 2. the truthfulness of the receipts themselves ──────────────────────────


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
    names = list(install.config.workspace_names())

    summary = vault_rehome.rehome_people(
        install.vault, install.runtime, apply=True, workspaces=names
    )

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


def test_an_honest_no_op_run_settles_the_task(tmp_path: Path) -> None:
    """A first ``--apply`` with nothing to move still records that it looked.

    This is the route that settles the notice on a re-rooted install, where the
    notes are already per-workspace and the plan legitimately finds nothing. The
    receipt is real: `should_record` includes `recorded is None`, the run refuses
    on a dirty tree, and `status: migrated` is what the audit reads.
    """
    install = _install(tmp_path)
    # The per-root shape: `People/Mo.md` has no workspace segment, so
    # `detect_misfiled_people` cannot examine it and the plan finds nothing.
    _person(install.vault, "People/Mo.md")

    summary = vault_rehome.rehome_people(
        install.vault, install.runtime, apply=True, workspaces=["personal", "work"]
    )

    assert summary["moves"] == [] and summary["rewrites"] == []
    assert summary["applied"] is True
    assert json.loads(
        vault_rehome.receipt_path(install.runtime).read_text(encoding="utf-8")
    )["status"] == "migrated"

    assert _check(install).applicable is True
    assert _detector(install).status == NOT_APPLICABLE
    assert _rehome_notices(install) == []


def test_a_dirty_vault_refuses_the_apply_and_records_nothing(tmp_path: Path) -> None:
    """The refusal is the guarantee `git checkout` stays an undo, and no receipt follows.

    A run that persisted a receipt for a refused apply would settle this task on
    the strength of work that never happened, which is the failure the dirty-tree
    rail exists to prevent.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    names = list(install.config.workspace_names())
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

    summary = vault_rehome.rehome_people(
        install.vault, install.runtime, apply=True, workspaces=names
    )

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
    _write_receipt(
        install.runtime,
        {**_receipt("partial"), "failed": [{"path": "personal/People/Mo.md"}]},
    )

    assert _check(install).applicable is False
    assert _detector(install).status == APPLICABLE
    assert _rehome_notices(install)


# ── 3. the record is the operator's, and never the audit's ───────────────────


def test_the_record_lives_in_the_runtime_directory_and_holds_no_path(
    tmp_path: Path,
) -> None:
    """Install-scoped means the runtime record, and a record that travels.

    The evidence is the receipt's own timestamp and two counts. Its `vault_root`
    is deliberately left out: it is an absolute path, and this file may be read
    on a machine that has never seen this install.
    """
    install = _install(tmp_path)
    _write_receipt(install.runtime, _receipt("migrated"))

    update_tasks.record_dismissal(_task(), config=install.config, workspace="personal")

    path = install.runtime / "update-tasks.json"
    assert path.is_file()
    assert not list(install.vault.rglob("Update-Tasks.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == update_tasks.STATE_SCHEMA
    assert payload["tasks"][f"{TASK_ID}@1"]["scope"] == "install"


def test_a_dismissed_task_never_silences_the_audit(tmp_path: Path) -> None:
    """The finding survives the operator hiding the card.

    This is the asymmetry #833 introduces, and the reason the shared condition
    reads a receipt and never a lifecycle: the card is an offer with a decision
    attached to it, the audit's report is not, and one surface's state must never
    become the other surface's silence.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)

    update_tasks.record_dismissal(
        _task(), config=install.config, workspace="personal", reason="not now"
    )

    assert _rehome_notices(install), "a dismissal silenced the diagnostic"
    # The card itself is suppressed, which is the half that is the operator's.
    state = update_tasks.read_task_state(_task(), config=install.config, workspace="personal")
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


def test_a_started_task_settles_from_the_receipt_it_produces(tmp_path: Path) -> None:
    """The record completes when the evidence lands, without anything asking twice.

    #788's settlement, over the real remedy: a started task whose registered
    #check now says the postcondition holds becomes `completed` on the next
    listing, and the card goes with it.
    """
    install = _install(tmp_path)
    _misfiled_person(install.vault)
    names = list(install.config.workspace_names())

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

    statuses = _evaluate(install)
    row = next(s for s in statuses if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "in_progress"

    vault_rehome.rehome_people(
        install.vault, install.runtime, apply=True, workspaces=names
    )

    statuses = _evaluate(install)
    row = next(s for s in statuses if s.task.id == TASK_ID)
    assert row.state is not None and row.state.lifecycle == "completed"
    assert row.offered is False
    assert _rehome_notices(install) == []


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


# ── 4. cheap, read-only, and the only card for this notice ───────────────────


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


def test_the_re_home_condition_walks_the_vault_on_neither_surface(tmp_path: Path) -> None:
    """The condition is a receipt check, at any vault size, wherever it is asked.

    The vault below holds nine re-homable person notes. `plan_rehome` walks every
    one of them, and it is exactly what this task asks a person to run
    deliberately — it must never be what decides whether to ask. Measured on the
    shared probe rather than on the whole audit section, because the *wikilink*
    notice in that section legitimately walks the vault (see
    `tests/test_migration_notices.py`), and counting that here would fail the
    wrong thing.
    """
    install = _install(tmp_path)
    for index in range(9):
        _note(
            install.vault,
            f"personal/People/Note{index}.md",
            "---\ntype: person\ntags: [person, colleague]\n---\n# X\n",
        )

    assert _accesses_under(install.vault, lambda: _detector(install)) == []
    assert (
        _accesses_under(
            install.vault,
            lambda: rehomed_people_finding(install.config, install.runtime),
        )
        == []
    )
    # And the audit still reports it, which is what makes the bound worth keeping.
    assert _rehome_notices(install)


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


# ── 5. the packaged row, its prompt, and the registries ─────────────────────


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


def test_the_prompt_teaches_the_managed_commands_and_nothing_else() -> None:
    """Managed commands, preview first, approval before `--apply`, an exact undo.

    The three failure modes a prompt for this task can have, each asserted here:
    teaching a manual move (which the engine refuses and no receipt can undo), a
    chat that applies without asking (this moves the user's own notes between
    their own workspaces), and an undo that is not the receipt's inverse.
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
    # The truthful no-op is what settles a re-rooted install.
    assert "status: migrated" in prompt
    # No manual route.
    for forbidden in ("mv ", "git mv", "move the file by hand", "edit the registry"):
        assert forbidden not in prompt, forbidden
    # The hedge the notice itself carries: absence of a receipt is not a finding.
    assert "Nothing is known to be misfiled" in prompt